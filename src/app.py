# app.py
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, unquote

import discord
import psutil
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.types import MessageMediaUnsupported
from telethon.utils import get_peer_id
from telethon.sessions import StringSession

from .config import Settings, load_settings
from .db import Store
from .discord_scraper import DiscordScraper
from .filters import should_forward_message
from .formatter import MessageFormatter
from .models import ContentFilter, SourceInfo
from .transformer import MediaTransformer, is_image, is_video
from .utils import content_hash_for_message, sanitize_filename, unlink_quiet
from .webui import WebDashboard
from .telegram_scraper import TelegramScraper

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Unicode-safe logging helper
# ---------------------------------------------------------------------------
def log_safe_app(logger_func, message: str):
    if sys.platform == "win32":
        replacements = {
            '\u2713': '[OK]', '\u2714': '[OK]',
            '\u2717': '[FAIL]', '\u274c': '[FAIL]',
            '\u26a0': '[WARN]', '\u2757': '[!]',
            '\U0001f4e8': '[MSG]', '\U0001f504': '[POLL]',
            '\U0001f4ca': '[STATS]', '\U0001f4da': '[BACKFILL]',
            '\U0001f6d1': '[STOP]', '\u2139': '[INFO]',
            '\u2705': '[OK]', '\U0001f501': '[RETRY]',
        }
        for k, v in replacements.items():
            message = message.replace(k, v)
    try:
        logger_func(message)
    except UnicodeEncodeError:
        logger_func(message.encode('ascii', errors='replace').decode('ascii'))


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------
class ProtectedContentError(RuntimeError):
    """Telegram returned protected / unsupported media."""


# ---------------------------------------------------------------------------
# Main forwarder class
# ---------------------------------------------------------------------------
class MediaForwarder:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.data_dir / "forwarder.db")

        # ---- Telegram client with session string support ----
        session_string = os.environ.get("TELEGRAM_SESSION_STRING")
        if session_string:
            self.telegram = TelegramClient(
                StringSession(session_string),
                settings.telegram_api_id,
                settings.telegram_api_hash,
            )
            logger.info("Using TELEGRAM_SESSION_STRING for authentication.")
        else:
            self.telegram = TelegramClient(
                settings.telegram_session,
                settings.telegram_api_id,
                settings.telegram_api_hash,
            )
            logger.info("Using file-based session: %s", settings.telegram_session)

        self.formatter = MessageFormatter(
            settings.include_source,
            settings.include_author,
            settings.include_timestamp,
            settings.include_link,
        )
        self.transformer = MediaTransformer(
            settings.compress_images,
            settings.max_image_size_mb,
            settings.convert_webp_to_jpg,
            settings.generate_video_thumbnails,
            settings.transcode_videos,
            settings.watermark_text,
        )

        # ---- Shared lock for scrapers (prevents simultaneous runs) ----
        self._scraper_lock = asyncio.Lock()

        # ---- Discord bot (optional) ----
        self.discord = None
        if settings.discord_token:
            intents = discord.Intents.default()
            intents.message_content = True
            intents.messages = True
            intents.guilds = True
            self.discord = discord.Client(intents=intents)
            self._register_discord_handlers()

        # ---- Discord scraper ----
        self.discord_scraper: DiscordScraper | None = None
        self._scraper_task = None
        if settings.discord_email and settings.discord_channels:
            channels = [
                ch.strip()
                for ch in settings.discord_channels.split(",")
                if ch.strip()
            ]
            self.discord_scraper = DiscordScraper(
                email=settings.discord_email,
                password=settings.discord_password,
                channels=channels,
                transformer=self.transformer,
                on_message_callback=self._on_scraped_discord_message,
                data_dir=settings.data_dir,
                headless=not settings.discord_show_browser,
                start_date=settings.discord_start_date,
                store=self.store,
                run_lock=self._scraper_lock,
            )
            for ch in channels:
                if not self.store.get_source(ch):
                    self.store.add_source(
                        "discord", ch, {}, settings.discord_start_date
                    )

        # ---- Telegram scraper ----
        self.telegram_scraper: TelegramScraper | None = None
        self._telegram_scraper_task = None
        if settings.telegram_scraper_enabled and settings.telegram_phone:
            self.telegram_scraper = TelegramScraper(
                phone=settings.telegram_phone,
                transformer=self.transformer,
                on_message_callback=self._on_scraped_telegram_message,
                data_dir=settings.data_dir,
                store=self.store,
                headless=not settings.telegram_show_browser,
                poll_interval=settings.telegram_scraper_poll_interval,
                run_lock=self._scraper_lock,
            )
            logger.info("Telegram scraper initialized.")

        self.started_at = time.time()
        self.forwarding_enabled = True
        self.web_dashboard: WebDashboard | None = None
        self._dest_entity = None

        # Dynamic destination channel ID (set via web UI)
        self.destination_channel_id = None

        self._register_telegram_handlers()

    # ------------------------------------------------------------------
    # Discord bot handlers
    # ------------------------------------------------------------------
    def _register_discord_handlers(self):
        if not self.discord:
            return

        @self.discord.event
        async def on_ready():
            logger.info("Discord bot logged in as %s", self.discord.user)

        @self.discord.event
        async def on_message(message: discord.Message):
            if message.author.bot or not self.forwarding_enabled:
                return
            channel_id = str(message.channel.id)
            source = self.store.get_source(channel_id)
            if not source or source.platform != "discord" or not source.enabled:
                return
            guild_name = getattr(message.guild, "name", "DM")
            channel_name = getattr(message.channel, "name", channel_id)
            info = SourceInfo(
                platform="discord",
                channel_id=channel_id,
                channel_name=f"{guild_name} #{channel_name}",
                author=str(message.author),
                guild_id=str(message.guild.id) if message.guild else None,
            )
            await self.forward_message(message, info, update_progress=True)

    # ------------------------------------------------------------------
    # Telegram handlers
    # ------------------------------------------------------------------
    def _register_telegram_handlers(self):
        @self.telegram.on(events.NewMessage())
        async def telegram_handler(event):
            if not self.forwarding_enabled:
                return
            chat_id = str(event.chat_id)
            source = self.store.get_source(chat_id)
            if not source or source.platform != "telegram" or not source.enabled:
                return
            try:
                chat = await event.get_chat()
                name = (
                    getattr(chat, "title", None)
                    or getattr(chat, "username", None)
                    or chat_id
                )
            except Exception:
                name = chat_id
            info = SourceInfo("telegram", chat_id, str(name), "Channel")
            await self.forward_message(event.message, info, update_progress=True)

        self._register_admin_commands()

    def _admin_pattern(self, pattern: str):
        return events.NewMessage(
            pattern=pattern, from_users=self.settings.admin_user_id
        )

    def _register_admin_commands(self):
        @self.telegram.on(self._admin_pattern(r"^/(start|help)$"))
        async def help_cmd(event):
            await event.reply(
                "\U0001f916 <b>Media Forwarder</b>\n\n"
                "Commands:\n"
                "<code>/add_source telegram &lt;chat_id&gt; [json]</code>\n"
                "<code>/add_source discord &lt;channel_id&gt; [json]</code>\n"
                "<code>/remove_source &lt;id&gt;</code>\n"
                "<code>/enable_source &lt;id&gt;</code> / "
                "<code>/disable_source &lt;id&gt;</code>\n"
                "<code>/list_sources</code>\n"
                "<code>/backfill telegram &lt;src&gt; [limit]</code>\n"
                "<code>/backfill discord &lt;id&gt; [limit]</code>\n"
                "<code>/stats</code> / <code>/status</code>\n"
                "<code>/retry_failed</code>\n"
                "<code>/clear_queue</code> – delete ALL queued items\n"
                "<code>/pause</code> / <code>/resume</code>",
                parse_mode="html",
            )

        @self.telegram.on(self._admin_pattern(r"^/add_source\b"))
        async def add_source_cmd(event):
            try:
                parts = event.raw_text.split(maxsplit=3)
                if len(parts) < 3:
                    raise ValueError(
                        "Usage: /add_source <telegram|discord> <channel> [json]"
                    )
                platform, raw_channel = parts[1].lower(), parts[2]
                filters = json.loads(parts[3]) if len(parts) > 3 else {}
                channel_id = await self.normalize_channel_id(platform, raw_channel)
                self.store.add_source(platform, channel_id, filters)
                await event.reply(
                    f"\u2705 Added {platform} source: <code>{channel_id}</code>",
                    parse_mode="html",
                )
            except Exception as exc:
                await event.reply(f"\u274c {exc}")

        @self.telegram.on(self._admin_pattern(r"^/remove_source\b"))
        async def remove_source_cmd(event):
            try:
                channel_id = event.raw_text.split(maxsplit=1)[1]
                self.store.remove_source(channel_id)
                await event.reply(
                    f"\U0001f5d1\ufe0f Removed: <code>{channel_id}</code>",
                    parse_mode="html",
                )
            except Exception as exc:
                await event.reply(f"\u274c {exc}")

        @self.telegram.on(
            self._admin_pattern(r"^/(enable_source|disable_source)\b")
        )
        async def toggle_source_cmd(event):
            try:
                cmd, channel_id = event.raw_text.split(maxsplit=1)
                enabled = cmd == "/enable_source"
                self.store.set_source_enabled(channel_id, enabled)
                await event.reply(
                    ("\u2705 Enabled " if enabled else "\u23f8\ufe0f Disabled ")
                    + f"<code>{channel_id}</code>",
                    parse_mode="html",
                )
            except Exception as exc:
                await event.reply(f"\u274c {exc}")

        @self.telegram.on(self._admin_pattern(r"^/list_sources$"))
        async def list_sources_cmd(event):
            sources = self.store.get_sources()
            if not sources:
                await event.reply("No sources configured.")
                return
            lines = ["\U0001f4cb <b>Sources</b>"]
            for s in sources:
                st = "\u2705" if s.enabled else "\u23f8\ufe0f"
                lines.append(
                    f"{st} {s.platform}: <code>{s.channel_id}</code> "
                    f"start={s.start_date or 'None'}"
                )
            await event.reply("\n".join(lines), parse_mode="html")

        @self.telegram.on(self._admin_pattern(r"^/stats$"))
        async def stats_cmd(event):
            stats = self.store.get_stats()
            ok = stats.get("forwarded", 0)
            failed = stats.get("failed", 0)
            total = ok + failed
            pct = (ok / total * 100) if total else 0
            lines = [
                "\U0001f4ca <b>Stats</b>",
                f"\u2705 Forwarded: {ok}",
                f"\u274c Failed: {failed}",
                f"\U0001f4c8 Success: {pct:.1f}%",
                f"\u23f1\ufe0f Uptime: {(time.time()-self.started_at)/3600:.1f}h",
                f"Queue: {self.store.queue_size('queued')} queued / "
                f"{self.store.queue_size('failed')} failed",
            ]
            by_media = {
                k[6:]: v for k, v in stats.items() if k.startswith("media:")
            }
            if by_media:
                lines.append("\n<b>By media:</b>")
                lines += [f"• {k}: {v}" for k, v in sorted(by_media.items())]
            await event.reply("\n".join(lines), parse_mode="html")

        @self.telegram.on(self._admin_pattern(r"^/status$"))
        async def status_cmd(event):
            proc = psutil.Process(os.getpid())
            recent = self.store.recent_failures(5)
            lines = [
                "\U0001f50d <b>Status</b>",
                f"Telegram: {'\u2705' if self.telegram.is_connected() else '\u274c'}",
                f"Forwarding: {'\u25b6\ufe0f' if self.forwarding_enabled else '\u23f8\ufe0f'}",
                f"Active sources: {len(self.store.get_sources(enabled=True))}",
                f"Queue: {self.store.queue_size('queued')} queued / "
                f"{self.store.queue_size('failed')} failed",
                f"Memory: {proc.memory_info().rss/1024/1024:.1f} MB",
            ]
            if recent:
                lines.append("\n\u26a0\ufe0f <b>Recent failures:</b>")
                for f in recent:
                    lines.append(
                        f"• {f['platform']} {f['channel_id']}/{f['message_id']}: "
                        f"{f['error'][:120]}"
                    )
            await event.reply("\n".join(lines), parse_mode="html")

        @self.telegram.on(self._admin_pattern(r"^/backfill\b"))
        async def backfill_cmd(event):
            try:
                parts = event.raw_text.split(maxsplit=3)
                if len(parts) < 3:
                    raise ValueError(
                        "Usage: /backfill <telegram|discord> <source> [limit]"
                    )
                platform, source = parts[1].lower(), parts[2]
                limit = int(parts[3]) if len(parts) > 3 else 100
                await event.reply(
                    f"\u23f3 Starting {platform} backfill from {source} "
                    f"(limit={limit})\u2026"
                )
                if platform == "telegram":
                    count, failed = await self.backfill_telegram(source, limit)
                elif platform == "discord":
                    count, failed = await self.backfill_discord(source, limit)
                else:
                    raise ValueError("platform must be telegram or discord")
                await event.reply(
                    f"\u2705 Backfill done. Forwarded={count}, failed={failed}"
                )
            except Exception as exc:
                logger.exception("backfill command failed")
                await event.reply(f"\u274c {exc}")

        @self.telegram.on(self._admin_pattern(r"^/retry_failed$"))
        async def retry_failed_cmd(event):
            n = self.store.retry_failed_queue()
            await event.reply(f"\U0001f501 Requeued {n} failed items.")

        @self.telegram.on(self._admin_pattern(r"^/clear_queue$"))
        async def clear_queue_cmd(event):
            """Delete every item in the queue so the worker stops spinning."""
            try:
                n = self.store.clear_queue()
                await event.reply(f"\U0001f5d1\ufe0f Cleared {n} queue items.")
            except Exception as exc:
                await event.reply(f"\u274c {exc}")

        @self.telegram.on(self._admin_pattern(r"^/pause$"))
        async def pause_cmd(event):
            self.forwarding_enabled = False
            await event.reply("\u23f8\ufe0f Forwarding paused.")

        @self.telegram.on(self._admin_pattern(r"^/resume$"))
        async def resume_cmd(event):
            self.forwarding_enabled = True
            await event.reply("\u25b6\ufe0f Forwarding resumed.")

    async def normalize_channel_id(self, platform: str, raw: str) -> str:
        if platform == "telegram":
            entity = await self.telegram.get_entity(raw)
            return str(get_peer_id(entity))
        if platform == "discord":
            return str(int(raw))
        raise ValueError("platform must be telegram or discord")

    # ------------------------------------------------------------------
    # Destination entity cache
    # ------------------------------------------------------------------
    async def _get_destination(self):
        if self._dest_entity is not None:
            return self._dest_entity

        # Use dynamic destination if set, otherwise fallback to settings
        raw_id = self.destination_channel_id or str(self.settings.destination_channel_id)

        # Try with -100 prefix for supergroups / channels
        if 0 < int(raw_id) < 10 ** 15 if isinstance(raw_id, (int, str)) and str(raw_id).lstrip('-').isdigit() else False:
            try:
                self._dest_entity = await self.telegram.get_entity(
                    int(f"-100{raw_id}")
                )
                log_safe_app(
                    logger.info,
                    f"Destination resolved with -100 prefix: {self._dest_entity}"
                )
                return self._dest_entity
            except Exception:
                pass

        # Direct resolution (works for @usernames, numeric IDs, etc.)
        self._dest_entity = await self.telegram.get_entity(raw_id)
        log_safe_app(logger.info, f"Destination entity resolved: {self._dest_entity}")
        return self._dest_entity

    # ------------------------------------------------------------------
    # Core forward logic
    # ------------------------------------------------------------------
    async def forward_message(
        self, message: Any, source_info: SourceInfo, update_progress: bool
    ) -> bool:
        source = self.store.get_source(source_info.channel_id)
        if not source or not source.enabled:
            return False

        mid = str(message.id)

        # Source must have a start date configured
        if not source.start_date:
            logger.debug(
                "Skipping %s/%s – no start date set for source",
                source_info.channel_id, mid,
            )
            return False

        # Must have attachments (media messages only)
        attachments = getattr(message, "attachments", []) or []
        if not attachments:
            logger.debug("Skipping %s/%s – text-only", source_info.channel_id, mid)
            self.store.inc_stat("filtered")
            return False

        try:
            # ---- per-source date filter ----
            msg_date: datetime | None = None
            for attr in ("timestamp", "date", "created_at"):
                val = getattr(message, attr, None)
                if not val:
                    continue
                if isinstance(val, str):
                    try:
                        msg_date = datetime.fromisoformat(
                            val.replace("Z", "+00:00")
                        )
                    except Exception:
                        pass
                elif isinstance(val, datetime):
                    msg_date = val
                    if msg_date.tzinfo is None:
                        msg_date = msg_date.replace(tzinfo=timezone.utc)
                if msg_date:
                    break

            if msg_date and source.start_date:
                src_dt = datetime.fromisoformat(
                    source.start_date
                ).replace(tzinfo=timezone.utc)
                if msg_date < src_dt:
                    logger.debug(
                        "Skipping %s/%s – before start date %s",
                        source_info.channel_id, mid, source.start_date,
                    )
                    self.store.inc_stat("filtered")
                    return False

            # ---- checkpoint: already processed? ----
            if update_progress:
                last = self.store.get_last_processed(source_info.channel_id)
                try:
                    msg_id_int = int(message.id)
                except (ValueError, TypeError):
                    msg_id_int = 0
                if last and msg_id_int <= last:
                    logger.debug(
                        "Already processed %s/%s", source_info.channel_id, mid
                    )
                    return False

            # ---- content filters ----
            filters = ContentFilter(**(source.filters or {}))
            allowed, reason = should_forward_message(
                message, filters, self.settings.max_file_size_mb
            )
            if not allowed:
                logger.info(
                    "Filtered %s/%s: %s", source_info.channel_id, mid, reason
                )
                self.store.inc_stat("filtered")
                return False

            # ---- duplicate check ----
            if filters.check_duplicates:
                h, size = content_hash_for_message(message)
                if self.store.is_duplicate_and_record(
                    h, source_info.channel_id, mid,
                    size, filters.duplicate_window_hours,
                ):
                    logger.info(
                        "Duplicate %s/%s", source_info.channel_id, mid
                    )
                    self.store.inc_stat("duplicate")
                    return False

            # ---- send ----
            media_type = await self._send_to_telegram(message, source_info)

            # Update checkpoint ONLY after confirmed successful send
            if update_progress:
                self.store.update_last_processed(
                    source_info.channel_id, message.id
                )

            self.store.inc_stat("forwarded")
            self.store.inc_stat(f"source:{source_info.channel_id}")
            self.store.inc_stat(f"media:{media_type}")
            logger.info(
                "Forwarded %s %s/%s",
                source_info.platform, source_info.channel_id, mid,
            )
            return True

        except FloodWaitError as exc:
            logger.warning(
                "FloodWait %ss on %s/%s",
                exc.seconds, source_info.channel_id, mid,
            )
            self.store.enqueue(
                source_info.platform, source_info.channel_id, mid,
                f"FloodWait:{exc.seconds}",
            )
            await asyncio.sleep(exc.seconds)
            return False

        except ProtectedContentError as exc:
            logger.warning(
                "Protected %s/%s: %s", source_info.channel_id, mid, exc
            )
            await self.notify_protected_content(message, source_info, str(exc))
            self.store.record_failure(
                source_info.platform, source_info.channel_id, mid,
                "Protected: " + str(exc),
            )
            self.store.inc_stat("protected")
            if update_progress:
                self.store.update_last_processed(
                    source_info.channel_id, message.id
                )
            # Mark source as requiring scraping (if Telegram)
            if source_info.platform == "telegram" and self.telegram_scraper:
                self.store.set_scrape_required(source_info.channel_id, True)
                logger.info(f"Marked {source_info.channel_id} as scrape_required")
            return False

        except Exception as exc:
            logger.exception(
                "Failed forwarding %s/%s", source_info.channel_id, mid
            )
            self.store.enqueue(
                source_info.platform, source_info.channel_id, mid, str(exc)
            )
            self.store.record_failure(
                source_info.platform, source_info.channel_id, mid,
                type(exc).__name__ + ": " + str(exc),
            )
            return False

    # ------------------------------------------------------------------
    # Send to Telegram
    # ------------------------------------------------------------------
    async def _send_to_telegram(
        self, message: Any, source_info: SourceInfo
    ) -> str:
        dest = await self._get_destination()

        # ================================================================
        # DISCORD SCRAPER PATH
        # ================================================================
        if source_info.platform == "discord":
            attachments: list = getattr(message, "attachments", []) or []
            if not attachments:
                return "text"

            downloaded: list[Path] = []
            failed_urls: list[str] = []

            for i, attachment in enumerate(attachments):
                # --- resolve URL ---
                if isinstance(attachment, str):
                    url = attachment
                elif isinstance(attachment, dict):
                    url = attachment.get("url", "")
                else:
                    # discord.py Attachment object
                    try:
                        fname = sanitize_filename(
                            getattr(attachment, "filename", f"att_{i}.bin")
                        )
                        dest_path = (
                            self.settings.tmp_dir
                            / f"discord_{message.id}_{i}_{fname}"
                        )
                        await attachment.save(dest_path)
                        downloaded.append(dest_path)
                    except Exception as e:
                        logger.error(
                            "Could not save discord.py attachment %d: %s", i, e
                        )
                        failed_urls.append(f"<discord.py attachment {i}>")
                    continue

                if not url:
                    continue

                # --- build local filename ---
                parsed = urlparse(url)
                raw_name = Path(unquote(parsed.path)).name
                if not raw_name or "." not in raw_name:
                    raw_name = f"attachment_{i}.bin"
                fname = sanitize_filename(raw_name)
                dest_path = (
                    self.settings.tmp_dir
                    / f"discord_{message.id}_{i}_{fname}"
                )

                if self.discord_scraper:
                    result = await self.discord_scraper._download_attachment(
                        url, dest_path, retries=5
                    )
                else:
                    logger.error("No scraper available to download attachment")
                    result = None

                if result:
                    downloaded.append(result)
                else:
                    failed_urls.append(url)
                    logger.warning(
                        "[FAIL] Attachment %d of message %s could not be "
                        "downloaded: %s", i, message.id, url,
                    )

            if failed_urls:
                logger.warning(
                    "Message %s: %d/%d attachments failed: %s",
                    message.id, len(failed_urls), len(attachments), failed_urls,
                )

            if not downloaded:
                logger.warning(
                    "Message %s: all attachments failed – skipping send",
                    message.id,
                )
                return "text"

            # Send in batches of 10 (Telegram album limit)
            media_type = "album"
            batch_size = 10
            for batch_start in range(0, len(downloaded), batch_size):
                batch = downloaded[batch_start: batch_start + batch_size]
                log_safe_app(
                    logger.info,
                    f"Uploading {len(batch)} file(s) for message "
                    f"{message.id} (batch {batch_start // batch_size + 1})",
                )
                sent_as_album = False
                try:
                    await self.telegram.send_file(
                        dest,
                        file=batch,
                        caption=None,
                        parse_mode="html",
                        album=True,
                        force_document=False,
                    )
                    sent_as_album = True
                except Exception as e:
                    logger.warning(
                        "Album send failed for %s: %s – "
                        "falling back to individual sends",
                        message.id, e,
                    )

                if not sent_as_album:
                    for path in batch:
                        try:
                            await self.telegram.send_file(
                                dest,
                                file=path,
                                caption=None,
                                force_document=False,
                            )
                        except Exception as e2:
                            logger.error(
                                "Individual send failed for %s: %s",
                                path.name, e2,
                            )
                    media_type = "document"

            for p in downloaded:
                unlink_quiet(p)

            return media_type

        # ================================================================
        # TELEGRAM SOURCE PATH
        # ================================================================
        source = self.store.get_source(source_info.channel_id)
        if not source:
            return "text"

        # Determine forwarding method from source filters (default: 'auto')
        forwarding_method = source.filters.get("forwarding_method", "auto")
        # Also check scrape_required flag
        use_scraper = source.scrape_required or forwarding_method == "scrape"

        # ---------- 1. If force scrape, use scraper only ----------
        if use_scraper:
            if not self.telegram_scraper:
                raise ProtectedContentError("Scraper required but not available")
            username = source.filters.get("username")  # optional
            msg_data = await self.telegram_scraper.fetch_message_by_id(
                source_info.channel_id, str(message.id), username
            )
            if msg_data and msg_data.get("attachments"):
                # Build a dummy message object with the scraped data
                # We'll pass it to _send_scraped_telegram_message
                return await self._send_scraped_telegram_message(msg_data, source_info, dest)
            else:
                logger.warning(f"Scraper could not fetch message {message.id} from {source_info.channel_id}")
                raise ProtectedContentError("Scraper returned no media")

        # ---------- 2. Try API first (auto or api) ----------
        # Check if media is unsupported right away
        if isinstance(getattr(message, "media", None), MessageMediaUnsupported):
            raise ProtectedContentError("MessageMediaUnsupported from API")

        text = self.formatter.format(message, source_info)
        caption = text[:1024]

        if getattr(message, "media", None):
            try:
                # Attempt to download via API
                path = await self.telegram.download_media(
                    message, file=str(self.settings.tmp_dir) + os.sep
                )
                if not path:
                    raise ProtectedContentError("download_media returned None")
                path = Path(path)
                try:
                    media_type = await self._send_file_path(dest, path, caption)
                    # If we reach here, API succeeded.
                    # If auto mode and we had previously marked scrape_required? We could clear it? Usually we keep it.
                    return media_type
                finally:
                    unlink_quiet(path)
            except ProtectedContentError:
                # API failed due to protected content
                if forwarding_method == "api":
                    # API-only mode: re-raise to be handled upstream
                    raise
                # For 'auto' mode: fallback to scraper
                if not self.telegram_scraper:
                    raise  # no scraper available
                # Mark source as scrape_required for future messages
                self.store.set_scrape_required(source_info.channel_id, True)
                logger.info(f"API failed for {source_info.channel_id}/{message.id}, falling back to scraper")
                # Now fetch via scraper
                username = source.filters.get("username")
                msg_data = await self.telegram_scraper.fetch_message_by_id(
                    source_info.channel_id, str(message.id), username
                )
                if msg_data and msg_data.get("attachments"):
                    return await self._send_scraped_telegram_message(msg_data, source_info, dest)
                else:
                    raise ProtectedContentError("Scraper fallback failed")
            except Exception as e:
                logger.error("Unexpected error during API download: %s", e)
                self.store.inc_stat("media_download_failed")
                # Depending on mode, we could fallback or re-raise
                raise

        # If no media at all (unlikely because we filtered earlier)
        await self.telegram.send_message(dest, text or " ", parse_mode="html")
        return "text"

    async def _send_scraped_telegram_message(
        self, msg_data: dict, source_info: SourceInfo, dest: Any
    ) -> str:
        """Send media from a scraped message dict."""
        attachments = msg_data.get("attachments", [])
        if not attachments:
            return "text"

        downloaded: list[Path] = []
        for idx, url in enumerate(attachments):
            # Build a filename from URL
            filename = url.split("/")[-1] or f"scraped_{idx}.bin"
            dest_path = self.settings.tmp_dir / f"tg_scraped_{msg_data['id']}_{idx}_{sanitize_filename(filename)}"
            result = await self.telegram_scraper.download_attachment(url, dest_path)
            if result:
                downloaded.append(result)

        if not downloaded:
            raise ProtectedContentError("No attachments could be downloaded from scraper")

        # Send as album or individually
        media_type = "album"
        try:
            await self.telegram.send_file(
                dest,
                file=downloaded,
                caption=msg_data.get("text", "")[:1024],
                parse_mode="html",
                album=True,
                force_document=False,
            )
        except Exception:
            # Fallback to individual sends
            media_type = "document"
            for path in downloaded:
                await self.telegram.send_file(dest, file=path, force_document=False)

        # Cleanup
        for p in downloaded:
            unlink_quiet(p)
        return media_type

    async def _send_file_path(
        self, dest: Any, path: Path, caption: str | None
    ) -> str:
        media_type = "document"
        cleanup: list[Path] = []
        send_path = path
        thumb = None
        try:
            if is_image(path):
                send_path = await self.transformer.transform_image(path)
                media_type = "photo"
                if send_path != path:
                    cleanup.append(send_path)
            elif is_video(path):
                result = await self.transformer.transform_video(path)
                send_path = result["video"] or path
                thumb = result["thumbnail"]
                media_type = "video"
                if send_path != path:
                    cleanup.append(send_path)
                if thumb:
                    cleanup.append(thumb)

            kwargs: dict = {
                "file": str(send_path),
                "caption": caption,
                "parse_mode": "html",
                "thumb": str(thumb) if thumb else None,
                "force_document": False,
            }
            if media_type == "video":
                kwargs["supports_streaming"] = True

            await self.telegram.send_file(dest, **kwargs)
            return media_type
        finally:
            for p in cleanup:
                unlink_quiet(p)

    async def notify_protected_content(
        self, message: Any, source_info: SourceInfo, reason: str
    ) -> None:
        if not self.settings.notify_protected_content:
            return
        try:
            link = self.formatter.link_for(message, source_info)
            lines = [
                "\u26a0\ufe0f <b>Protected content skipped</b>",
                f"Platform: <code>{html.escape(source_info.platform)}</code>",
                f"Source: <code>{html.escape(source_info.channel_name)}</code>",
                f"Channel: <code>{html.escape(source_info.channel_id)}</code>",
                f"Message: <code>{html.escape(str(message.id))}</code>",
                f"Reason: <code>{html.escape(reason[:500])}</code>",
            ]
            if link:
                lines.append(
                    f'Original: <a href="{html.escape(link, quote=True)}">open</a>'
                )
            await self.telegram.send_message(
                self.settings.admin_user_id,
                "\n".join(lines),
                parse_mode="html",
            )
        except Exception:
            logger.exception("Failed to send protected-content notification")

    # ------------------------------------------------------------------
    # Backfill
    # ------------------------------------------------------------------
    async def backfill_telegram(
        self, source: str, limit: int
    ) -> tuple[int, int]:
        entity = await self.telegram.get_entity(source)
        channel_id = str(get_peer_id(entity))
        if not self.store.get_source(channel_id):
            self.store.add_source("telegram", channel_id, {})
        name = (
            getattr(entity, "title", None)
            or getattr(entity, "username", None)
            or channel_id
        )
        info = SourceInfo("telegram", channel_id, str(name), "Channel")
        ok = failed = 0
        async for msg in self.telegram.iter_messages(
            entity, limit=limit, reverse=True
        ):
            if await self.forward_message(msg, info, update_progress=False):
                ok += 1
            else:
                failed += 1
            await asyncio.sleep(0.5)
        return ok, failed

    async def backfill_discord(
        self, channel_id: str, limit: int
    ) -> tuple[int, int]:
        if self.discord and self.discord.is_ready():
            channel = self.discord.get_channel(
                int(channel_id)
            ) or await self.discord.fetch_channel(int(channel_id))
            ok = failed = 0
            messages = [
                m async for m in channel.history(limit=limit, oldest_first=True)
            ]
            for msg in messages:
                if msg.author.bot:
                    continue
                info = SourceInfo(
                    "discord",
                    channel_id,
                    f"{getattr(getattr(msg,'guild',None),'name','Discord')} "
                    f"#{getattr(channel,'name',channel_id)}",
                    str(msg.author),
                    str(msg.guild.id) if msg.guild else None,
                )
                if await self.forward_message(msg, info, update_progress=False):
                    ok += 1
                else:
                    failed += 1
                await asyncio.sleep(0.5)
            return ok, failed
        elif self.discord_scraper:
            await self.discord_scraper.backfill(channel_id, limit)
            return 0, 0
        raise RuntimeError("No Discord backfill method available")

    # ------------------------------------------------------------------
    # Queue worker (with scraper-aware retries)
    # ------------------------------------------------------------------
    async def queue_worker(self) -> None:
        while True:
            item = self.store.next_queue_item()
            if not item:
                await asyncio.sleep(2)
                continue

            # Discord items: only retry if bot available
            if item.platform == "discord" and (
                self.discord is None or not self.discord.is_ready()
            ):
                logger.debug(
                    "Dropping un-retryable Discord queue item %s "
                    "(no bot available)",
                    item.id,
                )
                self.store.mark_queue_done(item.id)
                await asyncio.sleep(0)
                continue

            # Telegram items that require scraping: try scraper first
            if item.platform == "telegram":
                source = self.store.get_source(item.channel_id)
                if source and (source.scrape_required or source.filters.get("forwarding_method") == "scrape"):
                    if self.telegram_scraper:
                        try:
                            username = source.filters.get("username")
                            msg_data = await self.telegram_scraper.fetch_message_by_id(
                                item.channel_id, item.message_id, username
                            )
                            if msg_data and msg_data.get("attachments"):
                                info = SourceInfo(
                                    platform="telegram",
                                    channel_id=item.channel_id,
                                    channel_name=source.channel_id,
                                    author="Scraped User",
                                )
                                await self._send_scraped_telegram_message(
                                    msg_data, info, await self._get_destination()
                                )
                                self.store.mark_queue_done(item.id)
                                continue
                        except Exception as e:
                            logger.exception("Scraper retry failed for queue item")
                            self.store.requeue_or_fail(item, str(e), self.settings.queue_max_retries)
                            await asyncio.sleep(5)
                            continue
                    else:
                        # No scraper – drop the item (should not happen if scrape_required is set)
                        self.store.mark_queue_done(item.id)
                        continue

            # Normal retry (API) for all other items
            try:
                msg, info = await self.refetch_message(
                    item.platform, item.channel_id, item.message_id
                )
                # forward_message will handle scraping again if needed
                await self.forward_message(msg, info, update_progress=False)
                self.store.mark_queue_done(item.id)
            except FloodWaitError as exc:
                self.store.requeue_or_fail(
                    item,
                    f"FloodWait:{exc.seconds}",
                    self.settings.queue_max_retries,
                )
                await asyncio.sleep(exc.seconds)
            except Exception as exc:
                logger.exception("Queue item failed")
                self.store.requeue_or_fail(
                    item,
                    type(exc).__name__ + ": " + str(exc),
                    self.settings.queue_max_retries,
                )
                await asyncio.sleep(5)

            await asyncio.sleep(0.05)

    async def refetch_message(
        self, platform: str, channel_id: str, message_id: str
    ) -> tuple[Any, SourceInfo]:
        if platform == "telegram":
            msg = await self.telegram.get_messages(
                int(channel_id), ids=int(message_id)
            )
            if not msg:
                raise RuntimeError("Telegram message no longer available")
            try:
                chat = await self.telegram.get_entity(int(channel_id))
                name = (
                    getattr(chat, "title", None)
                    or getattr(chat, "username", None)
                    or channel_id
                )
            except Exception:
                name = channel_id
            return msg, SourceInfo("telegram", channel_id, str(name), "Channel")

        if self.discord and self.discord.is_ready():
            channel = self.discord.get_channel(
                int(channel_id)
            ) or await self.discord.fetch_channel(int(channel_id))
            msg = await channel.fetch_message(int(message_id))
            return msg, SourceInfo(
                "discord",
                channel_id,
                f"{getattr(getattr(msg,'guild',None),'name','Discord')} "
                f"#{getattr(channel,'name',channel_id)}",
                str(msg.author),
                str(msg.guild.id) if msg.guild else None,
            )
        raise RuntimeError("No Discord client available for refetch")

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    async def periodic_cleanup(self) -> None:
        while True:
            await asyncio.sleep(3600)
            self.store.cleanup_seen(days=7)
            for p in self.settings.tmp_dir.glob("*"):
                try:
                    if p.is_file() and (time.time() - p.stat().st_mtime) > 3600:
                        p.unlink(missing_ok=True)
                except Exception:
                    pass
            logger.info("Periodic cleanup complete")

    # ------------------------------------------------------------------
    # Scraper callbacks
    # ------------------------------------------------------------------
    async def _on_scraped_discord_message(
        self, msg: Any, source_info: SourceInfo
    ) -> bool:
        return await self.forward_message(
            msg, source_info, update_progress=True
        )

    async def _on_scraped_telegram_message(
        self, msg: Any, source_info: SourceInfo
    ) -> bool:
        return await self.forward_message(msg, source_info, update_progress=True)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    async def run(self) -> None:
        await self.telegram.start(phone=self.settings.telegram_phone or None)
        logger.info("Telegram client started")

        try:
            await self._get_destination()
        except Exception as e:
            logger.error("Could not resolve destination channel: %s", e)

        if self.discord and self.settings.discord_token:
            asyncio.create_task(self.discord.start(self.settings.discord_token))
        else:
            logger.info("Discord bot token not set – skipping bot client")

        if self.discord_scraper:
            await self.discord_scraper.start()
            self._scraper_task = asyncio.create_task(
                self.discord_scraper.poll_channels()
            )

        # Start Telegram scraper if enabled
        if self.telegram_scraper:
            await self.telegram_scraper.start()
            # Poll channels that have scrape_required = 1
            sources = self.store.get_sources_with_scrape_required()
            if sources:
                to_poll = []
                for s in sources:
                    username = s.filters.get("username")
                    to_poll.append((s.channel_id, username))
                self._telegram_scraper_task = asyncio.create_task(
                    self.telegram_scraper.poll_channels(to_poll)
                )
                logger.info(f"Telegram scraper polling {len(to_poll)} channels")
            else:
                logger.info("No Telegram sources require scraping; scraper will idle until needed.")

        asyncio.create_task(self.queue_worker())
        asyncio.create_task(self.periodic_cleanup())

        if self.settings.web_ui_enabled:
            self.web_dashboard = WebDashboard(self)
            await self.web_dashboard.start()

        logger.info("Forwarder is running")

        while True:
            try:
                await self.telegram.run_until_disconnected()
            except Exception:
                logger.exception("Telegram disconnected – reconnecting in 5 s")
                await asyncio.sleep(5)
                try:
                    await self.telegram.start(
                        phone=self.settings.telegram_phone or None
                    )
                    self._dest_entity = None
                    await self._get_destination()
                    logger.info("Reconnected to Telegram")
                except Exception as e:
                    logger.error("Reconnect failed: %s", e)
                    await asyncio.sleep(10)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def setup_logging(settings: Settings) -> None:
    (settings.data_dir / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                settings.data_dir / "logs" / "forwarder.log",
                encoding="utf-8",
            ),
        ],
    )


async def amain() -> None:
    settings = load_settings()
    setup_logging(settings)
    app = MediaForwarder(settings)
    await app.run()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
# telegram_scraper.py
from __future__ import annotations

import asyncio
import gc
import logging
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

from playwright.async_api import async_playwright, BrowserContext, Page

from .models import SourceInfo
from .transformer import MediaTransformer
from .utils import sanitize_filename, unlink_quiet
from .db import Store

# ---------- Unicode safe logging (same as discord_scraper) ----------
logger = logging.getLogger(__name__)

# JavaScript to extract messages from Telegram Web
_EXTRACT_JS = r"""
() => {
    const messages = [];
    const seenIds = new Set();

    // Find all message containers
    document.querySelectorAll('.message-container').forEach(el => {
        // Extract message ID from data attribute or from a link
        let msgId = el.getAttribute('data-message-id');
        if (!msgId) {
            const link = el.querySelector('a[href^="https://t.me/"]');
            if (link) {
                const parts = link.href.split('/');
                msgId = parts[parts.length - 1];
            }
        }
        if (!msgId || seenIds.has(msgId)) return;
        seenIds.add(msgId);

        // Timestamp: look for <time> element
        let ts = '';
        const timeEl = el.querySelector('time');
        if (timeEl) ts = timeEl.getAttribute('datetime') || '';

        // Text content (caption)
        let text = '';
        const textEl = el.querySelector('.message-text, .text-content, .caption');
        if (textEl) text = textEl.innerText || '';

        // Media URLs: images, videos, documents
        const urls = new Set();

        // Images: img tags with src
        el.querySelectorAll('img[src]').forEach(img => {
            const src = img.getAttribute('src');
            if (src && src.startsWith('http')) urls.add(src);
        });

        // Videos: video tags
        el.querySelectorAll('video[src]').forEach(video => {
            const src = video.getAttribute('src');
            if (src && src.startsWith('http')) urls.add(src);
        });

        // Documents: links with file download
        el.querySelectorAll('a[href^="https://t.me/"]').forEach(a => {
            const href = a.getAttribute('href');
            if (href && href.includes('?file=')) {
                // Construct full download URL (may need to add ?file=...)
                // Some links are direct to t.me with file ID
                urls.add(href);
            }
        });

        // Also check for data-* attributes with file URLs
        el.querySelectorAll('[data-file-url]').forEach(e => {
            const url = e.getAttribute('data-file-url');
            if (url) urls.add(url);
        });

        messages.push({
            id: msgId,
            timestamp: ts,
            text: text,
            attachments: [...urls]
        });
    });

    return messages;
}
"""

class TelegramScraper:
    def __init__(
        self,
        phone: str,
        transformer: MediaTransformer,
        on_message_callback: Callable[[Any, SourceInfo], Coroutine[Any, Any, bool]],
        data_dir: Path,
        store: Store,
        headless: bool = False,
        poll_interval: int = 20,
        run_lock: Optional[asyncio.Lock] = None,   # <-- NEW
    ):
        self.phone = phone
        self.transformer = transformer
        self.on_message = on_message_callback
        self.data_dir = data_dir
        self.store = store
        self.headless = headless
        self.poll_interval = poll_interval
        self.run_lock = run_lock or asyncio.Lock()   # <-- NEW

        self.context: BrowserContext | None = None
        self._page: Page | None = None
        self._running = False
        self._last_poll: dict[str, float] = {}
        self.user_data_dir = data_dir / "telegram_user_data"
        self.user_data_dir.mkdir(exist_ok=True)

        # Stats
        self._download_stats = {"success": 0, "failed": 0, "total_bytes": 0}

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------
    def _get_browser_args(self) -> list[str]:
        return [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-web-security",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--window-size=1280,800",
            "--disable-automation",
        ]

    async def _create_browser_context(self, playwright_instance) -> BrowserContext:
        return await playwright_instance.chromium.launch_persistent_context(
            user_data_dir=str(self.user_data_dir),
            headless=self.headless,
            args=self._get_browser_args(),
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/119.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
        )

    async def start(self):
        """Launch browser and ensure login."""
        logger.info("Starting Telegram scraper...")
        p = await async_playwright().start()
        self.context = await self._create_browser_context(p)
        self._page = await self.context.new_page()

        # Go to Telegram Web and check if logged in
        await self._page.goto("https://web.telegram.org/k/", wait_until="networkidle")
        if not await self._is_logged_in():
            logger.info("Not logged in. Performing login...")
            await self._perform_login()
        else:
            logger.info("Already logged in.")
        self._running = True
        logger.info("Telegram scraper started.")

    async def _is_logged_in(self) -> bool:
        try:
            # Check for presence of a sidebar or chat list
            if await self._page.locator('.chat-list').count() > 0:
                return True
            if await self._page.locator('div.sidebar').count() > 0:
                return True
            return False
        except Exception:
            return False

    async def _perform_login(self):
        # Phone number input
        await self._page.fill('input[name="phone"]', self.phone)
        await self._page.click('button[type="submit"]')
        await asyncio.sleep(2)

        # Wait for code input (maybe)
        logger.warning("Please complete the login (enter SMS code/2FA) in the browser window. Waiting up to 60 seconds...")
        for _ in range(60):
            await asyncio.sleep(1)
            if await self._is_logged_in():
                logger.info("Login successful.")
                return
        raise RuntimeError("Login timed out. Please restart the scraper and complete login manually.")

    async def stop(self):
        self._running = False
        if self._page:
            await self._page.close()
        if self.context:
            await self.context.close()
        logger.info("Telegram scraper stopped.")

    # ------------------------------------------------------------------
    # Navigation & Extraction
    # ------------------------------------------------------------------
    async def _ensure_page(self) -> Page:
        if self._page is None or not self._page.is_closed():
            return self._page
        # Re-create page if closed
        self._page = await self.context.new_page()
        return self._page

    async def _navigate_to_chat(self, channel_id: str, username: str | None = None):
        page = await self._ensure_page()
        if username:
            url = f"https://web.telegram.org/k/#@{username}"
        else:
            # Try to use channel ID as a numeric ID (works for private channels)
            # Format: https://web.telegram.org/k/#-1001234567890 (for supergroups)
            url = f"https://web.telegram.org/k/#{channel_id}"
        logger.debug(f"Navigating to {url}")
        await page.goto(url, wait_until="networkidle")
        await asyncio.sleep(2)   # Allow messages to load
        return page

    async def _wait_for_messages(self, page: Page, timeout=10):
        try:
            await page.wait_for_selector('.message-container', timeout=timeout*1000)
        except Exception:
            logger.warning("No message containers found; chat may be empty or inaccessible.")

    async def _extract_messages(self, page: Page, after_id: int = 0) -> list[dict]:
        """Scroll up to load old messages and extract those with ID > after_id."""
        # Scroll to bottom first (to load latest)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)

        # Now scroll up step by step, collecting messages
        collected = []
        seen_ids = set()
        scroll_step = 800
        max_scrolls = 50   # limit to avoid infinite loop
        no_new_streak = 0

        for _ in range(max_scrolls):
            # Scroll up
            await page.evaluate(f"window.scrollBy(0, -{scroll_step})")
            await asyncio.sleep(0.8)

            # Extract messages via JS
            try:
                msgs: list[dict] = await page.evaluate(_EXTRACT_JS)
            except Exception as e:
                logger.warning(f"Extraction error: {e}")
                continue

            new_count = 0
            for m in msgs:
                msg_id = m.get("id")
                if not msg_id or msg_id in seen_ids:
                    continue
                try:
                    if int(msg_id) <= after_id:
                        continue
                except ValueError:
                    continue
                seen_ids.add(msg_id)
                new_count += 1
                collected.append(m)

            if new_count == 0:
                no_new_streak += 1
                if no_new_streak >= 10:   # 10 scrolls with no new messages => likely reached top
                    break
            else:
                no_new_streak = 0

            # Check if we are at top (scrollTop ~0)
            scroll_top = await page.evaluate("window.scrollY")
            if scroll_top <= 10:
                # Give a little time to load older messages
                await asyncio.sleep(1)
                scroll_top = await page.evaluate("window.scrollY")
                if scroll_top <= 10:
                    break

        logger.debug(f"Extracted {len(collected)} new messages (after_id={after_id})")
        # Sort by ID ascending (oldest first)
        collected.sort(key=lambda m: int(m.get("id", 0)))
        return collected

    async def fetch_new_messages(self, channel_id: str, username: str | None = None) -> list[dict]:
        """Get messages from the source that are newer than last_scraped_id."""
        last_id = self.store.get_last_scraped_id(channel_id) or "0"
        try:
            after_id = int(last_id)
        except ValueError:
            after_id = 0

        page = await self._navigate_to_chat(channel_id, username)
        await self._wait_for_messages(page)
        msgs = await self._extract_messages(page, after_id)

        # Update last_scraped_id to the maximum found
        if msgs:
            max_id = max(int(m.get("id", 0)) for m in msgs)
            self.store.update_last_scraped_id(channel_id, str(max_id))
        return msgs

    async def fetch_message_by_id(self, channel_id: str, message_id: str, username: str | None = None) -> dict | None:
        """Fetch a specific message by ID. Not trivial on web; we scroll until we find it.
           This can be slow, so we only call when needed (fallback).
        """
        page = await self._navigate_to_chat(channel_id, username)
        await self._wait_for_messages(page)
        # Scroll up in a loop until we either find the message or hit the top
        for _ in range(100):   # 100 scrolls max
            msgs = await self._extract_messages(page, after_id=0)
            for m in msgs:
                if m.get("id") == message_id:
                    return m
            # Scroll up more
            await page.evaluate(f"window.scrollBy(0, -800)")
            await asyncio.sleep(0.8)
        return None

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------
    async def download_attachment(self, url: str, dest_path: Path, retries: int = 3) -> Path | None:
        """Download a file using the browser context (handles cookies)."""
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, retries + 1):
            try:
                response = await self.context.request.get(url, timeout=60000)
                if response.status == 200:
                    body = await response.body()
                    if body:
                        dest_path.write_bytes(body)
                        self._download_stats["success"] += 1
                        self._download_stats["total_bytes"] += len(body)
                        return dest_path
                elif response.status in (403, 404, 410):
                    logger.warning(f"Permanent error {response.status} for {url}")
                    break
                else:
                    logger.warning(f"HTTP {response.status} attempt {attempt}")
            except Exception as e:
                logger.warning(f"Download attempt {attempt} failed: {e}")
            await asyncio.sleep(min(2 ** attempt, 10))
        self._download_stats["failed"] += 1
        logger.error(f"Failed to download {url}")
        return None

    # ------------------------------------------------------------------
    # Polling loop (with locking)
    # ------------------------------------------------------------------
    async def poll_channels(self, sources: list[tuple[str, str | None]]):
        """Poll a list of (channel_id, username) sources that require scraping."""
        if not self._running:
            raise RuntimeError("Scraper not started.")
        logger.info(f"Polling {len(sources)} Telegram channels for scraping.")
        poll_count = 0

        while self._running:
            # Acquire the shared lock – only one scraper at a time
            async with self.run_lock:
                poll_count += 1
                logger.info(f"=== Telegram Poll cycle #{poll_count} ===")

                for channel_id, username in sources:
                    # Rate limit per channel
                    now = time.time()
                    if now - self._last_poll.get(channel_id, 0) < self.poll_interval:
                        continue
                    self._last_poll[channel_id] = now

                    try:
                        msgs = await self.fetch_new_messages(channel_id, username)
                        if msgs:
                            logger.info(f"Got {len(msgs)} new scraped messages from {channel_id}")
                            for msg in msgs:
                                info = SourceInfo(
                                    platform="telegram",
                                    channel_id=channel_id,
                                    channel_name=username or channel_id,
                                    author="Scraped User"
                                )
                                # Wrap the message dict
                                class MsgWrapper:
                                    def __init__(self, d):
                                        self.id = d["id"]
                                        self.attachments = d["attachments"]
                                        self.text = d["text"]
                                wrapper = MsgWrapper(msg)
                                await self.on_message(wrapper, info)
                    except Exception as e:
                        logger.exception(f"Error polling channel {channel_id}: {e}")
                        await asyncio.sleep(5)

                # Periodically restart page to free memory
                if poll_count % 5 == 0 and self._page:
                    try:
                        await self._page.close()
                    except Exception:
                        pass
                    self._page = await self.context.new_page()
                    gc.collect()
                    logger.info("Telegram page restarted to free memory")

            # Sleep outside the lock so other scrapers can run
            await asyncio.sleep(5)  # small pause between poll cycles

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------
    async def stop(self):
        self._running = False
        if self._page:
            await self._page.close()
        if self.context:
            await self.context.close()
        logger.info("Telegram scraper stopped.")
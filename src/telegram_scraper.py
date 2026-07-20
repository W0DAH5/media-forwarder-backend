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

logger = logging.getLogger(__name__)

# ---------- JavaScript extraction (unchanged) ----------
_EXTRACT_JS = r"""
() => {
    const messages = [];
    const seenIds = new Set();

    document.querySelectorAll('.message-container').forEach(el => {
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

        let ts = '';
        const timeEl = el.querySelector('time');
        if (timeEl) ts = timeEl.getAttribute('datetime') || '';

        let text = '';
        const textEl = el.querySelector('.message-text, .text-content, .caption');
        if (textEl) text = textEl.innerText || '';

        const urls = new Set();
        el.querySelectorAll('img[src]').forEach(img => {
            const src = img.getAttribute('src');
            if (src && src.startsWith('http')) urls.add(src);
        });
        el.querySelectorAll('video[src]').forEach(video => {
            const src = video.getAttribute('src');
            if (src && src.startsWith('http')) urls.add(src);
        });
        el.querySelectorAll('a[href^="https://t.me/"]').forEach(a => {
            const href = a.getAttribute('href');
            if (href && href.includes('?file=')) urls.add(href);
        });
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
        run_lock: Optional[asyncio.Lock] = None,
    ):
        self.phone = phone
        self.transformer = transformer
        self.on_message = on_message_callback
        self.data_dir = data_dir
        self.store = store
        self.headless = headless
        self.poll_interval = poll_interval
        self.run_lock = run_lock or asyncio.Lock()

        self.context: BrowserContext | None = None
        self._page: Page | None = None
        self._running = False
        self._last_poll: dict[str, float] = {}
        self.user_data_dir = data_dir / "telegram_user_data"
        self.user_data_dir.mkdir(exist_ok=True)

        self._download_stats = {"success": 0, "failed": 0, "total_bytes": 0}
        self._browser_ready = False
        self._playwright = None

    # ------------------------------------------------------------------
    # Browser lifecycle (deferred)
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

    async def _ensure_browser_and_login(self):
        """Launch browser and ensure login – called only when we hold the lock."""
        if self._browser_ready and self.context is not None:
            return

        logger.info("Launching Telegram browser...")
        self._playwright = await async_playwright().start()
        self.context = await self._create_browser_context(self._playwright)
        self._page = await self.context.new_page()

        await self._page.goto("https://web.telegram.org/k/", wait_until="networkidle")
        if not await self._is_logged_in():
            logger.info("Not logged in. Performing login...")
            await self._perform_login()
        else:
            logger.info("Already logged in.")
        self._browser_ready = True
        logger.info("Telegram browser ready.")

    async def _close_browser(self):
        """Close browser to free memory."""
        if self.context is not None:
            try:
                if self._page:
                    await self._page.close()
                await self.context.close()
            except Exception as e:
                logger.warning(f"Error closing Telegram browser: {e}")
            self.context = None
            self._page = None
            self._browser_ready = False
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            gc.collect()
            logger.info("Telegram browser closed")

    # ------------------------------------------------------------------
    # Login helpers
    # ------------------------------------------------------------------
    async def _is_logged_in(self) -> bool:
        try:
            if await self._page.locator('.chat-list').count() > 0:
                return True
            if await self._page.locator('div.sidebar').count() > 0:
                return True
            return False
        except Exception:
            return False

    async def _perform_login(self):
        await self._page.fill('input[name="phone"]', self.phone)
        await self._page.click('button[type="submit"]')
        await asyncio.sleep(2)
        logger.warning("Please complete login (SMS code/2FA) in the browser window. Waiting up to 60s...")
        for _ in range(60):
            await asyncio.sleep(1)
            if await self._is_logged_in():
                logger.info("Login successful.")
                return
        raise RuntimeError("Login timed out. Please restart and complete login manually.")

    # ------------------------------------------------------------------
    # Navigation & Extraction
    # ------------------------------------------------------------------
    async def _ensure_page(self) -> Page:
        if self._page is None or self._page.is_closed():
            self._page = await self.context.new_page()
        return self._page

    async def _navigate_to_chat(self, channel_id: str, username: str | None = None):
        page = await self._ensure_page()
        if username:
            url = f"https://web.telegram.org/k/#@{username}"
        else:
            url = f"https://web.telegram.org/k/#{channel_id}"
        logger.debug(f"Navigating to {url}")
        await page.goto(url, wait_until="networkidle")
        await asyncio.sleep(2)
        return page

    async def _wait_for_messages(self, page: Page, timeout=10):
        try:
            await page.wait_for_selector('.message-container', timeout=timeout*1000)
        except Exception:
            logger.warning("No message containers found; chat may be empty or inaccessible.")

    async def _extract_messages(self, page: Page, after_id: int = 0) -> list[dict]:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)

        collected = []
        seen_ids = set()
        scroll_step = 800
        max_scrolls = 50
        no_new_streak = 0

        for _ in range(max_scrolls):
            await page.evaluate(f"window.scrollBy(0, -{scroll_step})")
            await asyncio.sleep(0.8)

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
                if no_new_streak >= 10:
                    break
            else:
                no_new_streak = 0

            scroll_top = await page.evaluate("window.scrollY")
            if scroll_top <= 10:
                await asyncio.sleep(1)
                scroll_top = await page.evaluate("window.scrollY")
                if scroll_top <= 10:
                    break

        logger.debug(f"Extracted {len(collected)} new messages (after_id={after_id})")
        collected.sort(key=lambda m: int(m.get("id", 0)))
        return collected

    async def fetch_new_messages(self, channel_id: str, username: str | None = None) -> list[dict]:
        last_id = self.store.get_last_scraped_id(channel_id) or "0"
        try:
            after_id = int(last_id)
        except ValueError:
            after_id = 0

        page = await self._navigate_to_chat(channel_id, username)
        await self._wait_for_messages(page)
        msgs = await self._extract_messages(page, after_id)

        if msgs:
            max_id = max(int(m.get("id", 0)) for m in msgs)
            self.store.update_last_scraped_id(channel_id, str(max_id))
        return msgs

    async def fetch_message_by_id(self, channel_id: str, message_id: str, username: str | None = None) -> dict | None:
        page = await self._navigate_to_chat(channel_id, username)
        await self._wait_for_messages(page)
        for _ in range(100):
            msgs = await self._extract_messages(page, after_id=0)
            for m in msgs:
                if m.get("id") == message_id:
                    return m
            await page.evaluate("window.scrollBy(0, -800)")
            await asyncio.sleep(0.8)
        return None

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------
    async def download_attachment(self, url: str, dest_path: Path, retries: int = 3) -> Path | None:
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
    # Public methods: start, poll_channels, stop
    # ------------------------------------------------------------------
    async def start(self):
        """Initialize scraper – does NOT launch browser."""
        self._running = True
        logger.info("Telegram scraper initialized (browser will launch on first poll)")

    async def poll_channels(self, sources: list[tuple[str, str | None]]):
        """Polling loop – acquires lock, launches browser, processes, then closes browser."""
        if not self._running:
            raise RuntimeError("Scraper not started.")
        logger.info(f"Polling {len(sources)} Telegram channels.")
        poll_count = 0

        while self._running:
            async with self.run_lock:
                poll_count += 1
                logger.info(f"=== Telegram Poll cycle #{poll_count} ===")

                try:
                    await self._ensure_browser_and_login()

                    for channel_id, username in sources:
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

                except Exception as e:
                    logger.exception(f"Error in poll cycle: {e}")

                finally:
                    await self._close_browser()

            await asyncio.sleep(5)  # pause between cycles

    async def stop(self):
        self._running = False
        await self._close_browser()
        logger.info("Telegram scraper stopped")
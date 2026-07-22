# db.py
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import SourceChannel, QueueItem, utcnow_iso


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self.lock, self.conn:
            self.conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL CHECK(platform IN ('telegram', 'discord')),
                    channel_id TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    filters TEXT NOT NULL DEFAULT '{}',
                    start_date TEXT,
                    created_at TEXT NOT NULL,
                    forwarding_method TEXT DEFAULT 'auto',
                    last_scraped_id TEXT,
                    scrape_required INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS progress (
                    channel_id TEXT PRIMARY KEY,
                    last_message_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS seen_content (
                    content_hash TEXT PRIMARY KEY,
                    channel_id TEXT,
                    message_id TEXT,
                    file_size INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_seen_created ON seen_content(created_at);
                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'queued',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, channel_id, message_id, status)
                );
                CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status, created_at);
                CREATE TABLE IF NOT EXISTS stats (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT,
                    channel_id TEXT,
                    message_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            # Add columns if missing (idempotent)
            for col in ['forwarding_method', 'last_scraped_id', 'scrape_required']:
                try:
                    self.conn.execute(f"ALTER TABLE sources ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass

    def add_source(
        self,
        platform: str,
        channel_id: str,
        filters: dict[str, Any] | None = None,
        start_date: str | None = None,
        forwarding_method: str = "auto",
        scrape_required: bool = False,
    ) -> None:
        if platform not in {"telegram", "discord"}:
            raise ValueError("platform must be 'telegram' or 'discord'")

        # Preserve last_scraped_id if source already exists
        existing = self.get_source(channel_id)
        last_scraped_id = existing.last_scraped_id if existing else None

        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO sources(
                    platform, channel_id, enabled, filters, start_date,
                    created_at, forwarding_method, last_scraped_id, scrape_required
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    platform,
                    str(channel_id),
                    json.dumps(filters or {}),
                    start_date,
                    utcnow_iso(),
                    forwarding_method,
                    last_scraped_id,
                    1 if scrape_required else 0,
                ),
            )

    def update_source_start_date(self, channel_id: str, start_date: str | None) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE sources SET start_date = ? WHERE channel_id = ?",
                (start_date, str(channel_id)),
            )

    def remove_source(self, channel_id: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM sources WHERE channel_id = ?", (str(channel_id),))

    def set_source_enabled(self, channel_id: str, enabled: bool) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE sources SET enabled = ? WHERE channel_id = ?",
                (1 if enabled else 0, str(channel_id)),
            )

    def get_source(self, channel_id: str) -> SourceChannel | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM sources WHERE channel_id = ?", (str(channel_id),)).fetchone()
        return self._source_from_row(row) if row else None

    def get_sources(self, platform: str | None = None, enabled: bool | None = None) -> list[SourceChannel]:
        q = "SELECT * FROM sources WHERE 1=1"
        args: list[Any] = []
        if platform:
            q += " AND platform = ?"
            args.append(platform)
        if enabled is not None:
            q += " AND enabled = ?"
            args.append(1 if enabled else 0)
        q += " ORDER BY platform, channel_id"
        with self.lock:
            rows = self.conn.execute(q, args).fetchall()
        return [self._source_from_row(r) for r in rows]

    def get_sources_with_scrape_required(self) -> list[SourceChannel]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM sources WHERE scrape_required = 1 AND enabled = 1"
            ).fetchall()
        return [self._source_from_row(r) for r in rows]

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> SourceChannel:
        # Convert to dict to safely access fields (row has .get() but this is safer)
        d = dict(row)
        return SourceChannel(
            id=d["id"],
            platform=d["platform"],
            channel_id=d["channel_id"],
            enabled=bool(d["enabled"]),
            filters=json.loads(d.get("filters", "{}")),
            start_date=d.get("start_date"),
            created_at=d["created_at"],
            forwarding_method=d.get("forwarding_method", "auto"),
            last_scraped_id=d.get("last_scraped_id"),
            scrape_required=bool(d.get("scrape_required", 0)),
        )

    # ----- progress -----
    def get_last_processed(self, channel_id: str) -> int:
        with self.lock:
            row = self.conn.execute(
                "SELECT last_message_id FROM progress WHERE channel_id = ?", (str(channel_id),)
            ).fetchone()
        if not row:
            return 0
        try:
            return int(row["last_message_id"])
        except ValueError:
            return 0

    def update_last_processed(self, channel_id: str, message_id: int | str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO progress(channel_id, last_message_id, updated_at) VALUES (?, ?, ?)",
                (str(channel_id), str(message_id), utcnow_iso()),
            )

    # ----- last_scraped_id -----
    def get_last_scraped_id(self, channel_id: str) -> str | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT last_scraped_id FROM sources WHERE channel_id = ?", (str(channel_id),)
            ).fetchone()
        return row["last_scraped_id"] if row else None

    def update_last_scraped_id(self, channel_id: str, message_id: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE sources SET last_scraped_id = ? WHERE channel_id = ?",
                (str(message_id), str(channel_id)),
            )

    def set_scrape_required(self, channel_id: str, required: bool) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE sources SET scrape_required = ? WHERE channel_id = ?",
                (1 if required else 0, str(channel_id)),
            )

    # ----- duplicate detection -----
    def is_duplicate_and_record(
        self, content_hash: str, channel_id: str, message_id: str, file_size: int, window_hours: int
    ) -> bool:
        cutoff = (datetime.utcnow() - timedelta(hours=window_hours)).replace(microsecond=0).isoformat() + "Z"
        with self.lock, self.conn:
            row = self.conn.execute(
                "SELECT 1 FROM seen_content WHERE content_hash = ? AND created_at > ? LIMIT 1",
                (content_hash, cutoff),
            ).fetchone()
            if row:
                return True
            self.conn.execute(
                "INSERT OR REPLACE INTO seen_content(content_hash, channel_id, message_id, file_size, created_at) VALUES (?, ?, ?, ?, ?)",
                (content_hash, str(channel_id), str(message_id), int(file_size or 0), utcnow_iso()),
            )
            return False

    def cleanup_seen(self, days: int = 7) -> None:
        cutoff = (datetime.utcnow() - timedelta(days=days)).replace(microsecond=0).isoformat() + "Z"
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM seen_content WHERE created_at < ?", (cutoff,))

    # ----- queue -----
    def enqueue(self, platform: str, channel_id: str, message_id: str, error: str | None = None) -> None:
        now = utcnow_iso()
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO queue(platform, channel_id, message_id, attempts, status, error, created_at, updated_at)
                VALUES (?, ?, ?, 0, 'queued', ?, ?, ?)
                """,
                (platform, str(channel_id), str(message_id), error, now, now),
            )

    def next_queue_item(self) -> QueueItem | None:
        with self.lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM queue WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            self.conn.execute(
                "UPDATE queue SET status = 'processing', updated_at = ? WHERE id = ?", (utcnow_iso(), row["id"])
            )
            return self._queue_from_row(row)

    def mark_queue_done(self, item_id: int) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM queue WHERE id = ?", (item_id,))

    def requeue_or_fail(self, item: QueueItem, error: str, max_retries: int) -> None:
        attempts = item.attempts + 1
        now = utcnow_iso()
        with self.lock, self.conn:
            if attempts >= max_retries:
                self.conn.execute(
                    "UPDATE queue SET attempts = ?, status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                    (attempts, error, now, item.id),
                )
                self.record_failure(item.platform, item.channel_id, item.message_id, error)
            else:
                self.conn.execute(
                    "UPDATE queue SET attempts = ?, status = 'queued', error = ?, updated_at = ? WHERE id = ?",
                    (attempts, error, now, item.id),
                )

    def retry_failed_queue(self) -> int:
        with self.lock, self.conn:
            cur = self.conn.execute(
                "UPDATE queue SET status='queued', updated_at=? WHERE status='failed'", (utcnow_iso(),)
            )
            return cur.rowcount

    def clear_queue(self) -> int:
        with self.lock, self.conn:
            cur = self.conn.execute("DELETE FROM queue")
            return cur.rowcount

    def queue_size(self, status: str = "queued") -> int:
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM queue WHERE status = ?", (status,)).fetchone()
        return int(row["n"])

    @staticmethod
    def _queue_from_row(row: sqlite3.Row) -> QueueItem:
        return QueueItem(
            id=row["id"],
            platform=row["platform"],
            channel_id=row["channel_id"],
            message_id=row["message_id"],
            attempts=row["attempts"],
            status=row["status"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ----- stats -----
    def inc_stat(self, key: str, amount: int = 1) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT OR IGNORE INTO stats(key, value) VALUES (?, 0)", (key,))
            self.conn.execute("UPDATE stats SET value = value + ? WHERE key = ?", (amount, key))

    def get_stats(self) -> dict[str, int]:
        with self.lock:
            rows = self.conn.execute("SELECT key, value FROM stats ORDER BY key").fetchall()
        return {r["key"]: int(r["value"]) for r in rows}

    def record_failure(self, platform: str, channel_id: str, message_id: str, error: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO failures(platform, channel_id, message_id, error, created_at) VALUES (?, ?, ?, ?, ?)",
                (platform, str(channel_id), str(message_id), error[:1000], utcnow_iso()),
            )
            self.inc_stat("failed")
            self.inc_stat(f"error:{error.split(':')[0][:80]}")

    def recent_failures(self, limit: int = 5) -> list[dict[str, str]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM failures ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
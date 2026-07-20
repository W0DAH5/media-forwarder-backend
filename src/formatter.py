from __future__ import annotations

import html
from datetime import timezone
from typing import Any

from .models import SourceInfo
from .utils import message_text


class MessageFormatter:
    def __init__(self, include_source=True, include_author=True, include_timestamp=True, include_link=True):
        self.include_source = include_source
        self.include_author = include_author
        self.include_timestamp = include_timestamp
        self.include_link = include_link

    def format(self, message: Any, source: SourceInfo) -> str:
        original = html.escape(message_text(message))
        lines = [original] if original else []

        meta = []
        if self.include_source:
            meta.append(f"📌 Source: {html.escape(source.channel_name)}")
        if self.include_author:
            meta.append(f"👤 Author: {html.escape(source.author or 'Unknown')}")
        if self.include_timestamp:
            dt = getattr(message, "date", None) or getattr(message, "created_at", None)
            if dt:
                if getattr(dt, "tzinfo", None):
                    dt = dt.astimezone(timezone.utc)
                meta.append(f"🕒 Posted: {html.escape(dt.strftime('%Y-%m-%d %H:%M:%S UTC'))}")
        if self.include_link:
            link = self.link_for(message, source)
            if link:
                safe = html.escape(link, quote=True)
                meta.append(f'🔗 Link: <a href="{safe}">original</a>')

        if meta:
            lines.append("━━━━━━━━━━━━━━━")
            lines.extend(meta)
        return "\n".join(lines).strip()[:4096]

    @staticmethod
    def link_for(message: Any, source: SourceInfo) -> str | None:
        if source.platform == "discord":
            guild_id = source.guild_id or getattr(getattr(message, "guild", None), "id", None)
            chan_id = getattr(getattr(message, "channel", None), "id", None) or source.channel_id
            if guild_id:
                return f"https://discord.com/channels/{guild_id}/{chan_id}/{message.id}"
            return None
        chat = getattr(message, "chat", None)
        username = getattr(chat, "username", None)
        if username:
            return f"https://t.me/{username}/{message.id}"
        chat_id = str(source.channel_id)
        if chat_id.startswith("-100"):
            return f"https://t.me/c/{chat_id[4:]}/{message.id}"
        return None

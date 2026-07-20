from __future__ import annotations

import re
from typing import Any

from .models import ContentFilter
from .utils import get_file_size, get_media_type, message_text


def should_forward_message(message: Any, filters: ContentFilter, default_max_file_size_mb: int) -> tuple[bool, str]:
    media_type = get_media_type(message)
    text = message_text(message)
    size = get_file_size(message)

    if filters.allowed_media_types and media_type not in filters.allowed_media_types:
        return False, f"media type '{media_type}' not allowed"

    max_mb = filters.max_file_size_mb or default_max_file_size_mb
    if max_mb and size and size > max_mb * 1024 * 1024:
        return False, f"file too large ({size / 1024 / 1024:.1f}MB > {max_mb}MB)"

    if filters.min_file_size_mb and size and size < filters.min_file_size_mb * 1024 * 1024:
        return False, f"file too small ({size / 1024 / 1024:.1f}MB < {filters.min_file_size_mb}MB)"

    if filters.keyword_blacklist:
        low = text.lower()
        for kw in filters.keyword_blacklist:
            if kw.lower() in low:
                return False, f"blacklisted keyword '{kw}'"

    if filters.keyword_whitelist:
        low = text.lower()
        if not any(kw.lower() in low for kw in filters.keyword_whitelist):
            return False, "no whitelisted keyword"

    if filters.regex_pattern and not re.search(filters.regex_pattern, text, flags=re.IGNORECASE | re.MULTILINE):
        return False, "regex not matched"

    return True, "OK"

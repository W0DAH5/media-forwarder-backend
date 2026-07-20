# config.py
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int = 0) -> int:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    telegram_api_id: int
    telegram_api_hash: str
    telegram_phone: str
    telegram_session: str
    destination_channel_id: int
    discord_token: str
    admin_user_id: int
    data_dir: Path
    tmp_dir: Path
    log_level: str
    max_file_size_mb: int
    compress_images: bool
    max_image_size_mb: int
    discord_email: str
    discord_password: str
    discord_channels: str
    discord_show_browser: bool
    discord_start_date: str | None
    convert_webp_to_jpg: bool
    generate_video_thumbnails: bool
    transcode_videos: bool
    watermark_text: str | None
    include_source: bool
    include_author: bool
    include_timestamp: bool
    include_link: bool
    queue_max_retries: int
    notify_protected_content: bool
    web_ui_enabled: bool
    web_ui_host: str
    web_ui_port: int
    web_ui_token: str
    # New:
    telegram_scraper_enabled: bool
    telegram_show_browser: bool
    telegram_scraper_poll_interval: int


def load_settings() -> Settings:
    load_dotenv()
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    tmp_dir = Path(os.getenv("TMP_DIR", "tmp"))
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    settings = Settings(
        telegram_api_id=_int("TELEGRAM_API_ID"),
        telegram_api_hash=os.getenv("TELEGRAM_API_HASH", ""),
        telegram_phone=os.getenv("TELEGRAM_PHONE", ""),
        telegram_session=os.getenv("TELEGRAM_SESSION", str(data_dir / "telegram_user")),
        destination_channel_id=_int("DESTINATION_CHANNEL_ID"),
        discord_token=os.getenv("DISCORD_TOKEN", ""),
        admin_user_id=_int("ADMIN_USER_ID"),
        data_dir=data_dir,
        tmp_dir=tmp_dir,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        max_file_size_mb=_int("MAX_FILE_SIZE_MB", 45),
        compress_images=_bool("COMPRESS_IMAGES", True),
        max_image_size_mb=_int("MAX_IMAGE_SIZE_MB", 5),
        discord_email=os.getenv("DISCORD_EMAIL", ""),
        discord_password=os.getenv("DISCORD_PASSWORD", ""),
        discord_channels=os.getenv("DISCORD_CHANNELS", ""),
        discord_show_browser=_bool("DISCORD_SHOW_BROWSER", False),
        discord_start_date=os.getenv("DISCORD_START_DATE") or None,
        convert_webp_to_jpg=_bool("CONVERT_WEBP_TO_JPG", True),
        generate_video_thumbnails=_bool("GENERATE_VIDEO_THUMBNAILS", True),
        transcode_videos=_bool("TRANSCODE_VIDEOS", True),
        watermark_text=os.getenv("WATERMARK_TEXT") or None,
        include_source=_bool("INCLUDE_SOURCE", True),
        include_author=_bool("INCLUDE_AUTHOR", True),
        include_timestamp=_bool("INCLUDE_TIMESTAMP", True),
        include_link=_bool("INCLUDE_LINK", True),
        queue_max_retries=_int("QUEUE_MAX_RETRIES", 3),
        notify_protected_content=_bool("NOTIFY_PROTECTED_CONTENT", True),
        web_ui_enabled=_bool("WEB_UI_ENABLED", True),
        web_ui_host=os.getenv("WEB_UI_HOST", "127.0.0.1"),
        web_ui_port=_int("WEB_UI_PORT", 8080),
        web_ui_token=os.getenv("WEB_UI_TOKEN", ""),
        # New:
        telegram_scraper_enabled=_bool("TELEGRAM_SCRAPER_ENABLED", False),
        telegram_show_browser=_bool("TELEGRAM_SHOW_BROWSER", False),
        telegram_scraper_poll_interval=_int("TELEGRAM_SCRAPER_POLL_INTERVAL", 20),
    )

    missing = []
    if not settings.telegram_api_id:
        missing.append("TELEGRAM_API_ID")
    if not settings.telegram_api_hash:
        missing.append("TELEGRAM_API_HASH")
    if not settings.destination_channel_id:
        missing.append("DESTINATION_CHANNEL_ID")
    if not settings.admin_user_id:
        missing.append("ADMIN_USER_ID")
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    return settings
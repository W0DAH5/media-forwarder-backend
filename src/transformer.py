from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class MediaTransformer:
    def __init__(self, compress_images: bool, max_image_size_mb: int, convert_webp_to_jpg: bool, generate_video_thumbnails: bool, transcode_videos: bool, watermark_text: str | None):
        self.compress_images = compress_images
        self.max_image_size_mb = max_image_size_mb
        self.convert_webp_to_jpg = convert_webp_to_jpg
        self.generate_video_thumbnails = generate_video_thumbnails
        self.transcode_videos = transcode_videos
        self.watermark_text = watermark_text

    async def transform_image(self, file_path: str | Path) -> Path:
        from PIL import Image, ImageDraw, ImageFont

        path = Path(file_path)
        img = Image.open(path)
        if img.mode not in {"RGB", "RGBA"}:
            img = img.convert("RGB")

        out = path
        if self.convert_webp_to_jpg and path.suffix.lower() == ".webp":
            out = path.with_suffix(".jpg")
            img = img.convert("RGB")

        if self.watermark_text:
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            overlay = Image.new("RGBA", img.size, (255, 255, 255, 0))
            draw = ImageDraw.Draw(overlay)
            try:
                font = ImageFont.truetype("DejaVuSans.ttf", max(16, img.width // 40))
            except Exception:
                font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), self.watermark_text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x, y = img.width - tw - 24, img.height - th - 24
            draw.rectangle((x - 10, y - 8, x + tw + 10, y + th + 8), fill=(0, 0, 0, 120))
            draw.text((x, y), self.watermark_text, font=font, fill=(255, 255, 255, 230))
            img = Image.alpha_composite(img, overlay).convert("RGB")
            out = out.with_name(out.stem + "_watermarked.jpg")

        quality = 90
        if self.compress_images and path.exists():
            size_mb = path.stat().st_size / 1024 / 1024
            if size_mb > self.max_image_size_mb:
                quality = max(35, min(85, int(85 * (self.max_image_size_mb / size_mb))))
                out = out.with_name(out.stem + "_compressed.jpg")

        save_kwargs = {"quality": quality, "optimize": True} if out.suffix.lower() in {".jpg", ".jpeg"} else {}
        img.save(out, **save_kwargs)
        if out != path:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        return out

    async def transform_video(self, file_path: str | Path) -> dict[str, Path | None]:
        path = Path(file_path)
        result: dict[str, Path | None] = {"video": path, "thumbnail": None}
        try:
            import ffmpeg
        except Exception:
            return result

        if self.generate_video_thumbnails:
            thumb = path.with_name(path.stem + "_thumb.jpg")
            try:
                ffmpeg.input(str(path), ss=1).filter("scale", 320, -1).output(str(thumb), vframes=1).overwrite_output().run(quiet=True)
                result["thumbnail"] = thumb
            except Exception as exc:
                logger.warning("thumbnail generation failed: %s", exc)

        if self.transcode_videos:
            transcoded = path.with_name(path.stem + "_transcoded.mp4")
            try:
                ffmpeg.input(str(path)).output(str(transcoded), vcodec="libx264", crf=23, preset="medium", acodec="aac").overwrite_output().run(quiet=True)
                if transcoded.stat().st_size < path.stat().st_size:
                    path.unlink(missing_ok=True)
                    result["video"] = transcoded
                else:
                    transcoded.unlink(missing_ok=True)
            except Exception as exc:
                logger.warning("video transcoding failed: %s", exc)
        return result


def is_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def is_video(path: str | Path) -> bool:
    return Path(path).suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".avi"}
# webui.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import psutil
from aiohttp import web

logger = logging.getLogger(__name__)


class WebDashboard:
    """Small self-contained aiohttp control panel for the forwarder."""

    def __init__(self, forwarder: Any):
        self.forwarder = forwarder
        self.settings = forwarder.settings
        self.tasks: dict[str, dict[str, Any]] = {}
        self.app = web.Application(middlewares=[self.auth_middleware])
        self.runner: web.AppRunner | None = None
        self._routes()

    @web.middleware
    async def auth_middleware(self, request: web.Request, handler):
        if request.path in {"/health"}:
            return await handler(request)
        token = self.settings.web_ui_token
        if token:
            provided = (
                request.headers.get("X-Forwarder-Token")
                or request.query.get("token")
                or request.cookies.get("forwarder_token")
            )
            if provided != token:
                if request.path.startswith("/api/"):
                    return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
                return web.Response(text=self.login_html(), content_type="text/html")
        return await handler(request)

    def _routes(self) -> None:
        self.app.router.add_get("/", self.index)
        self.app.router.add_get("/health", self.health)
        self.app.router.add_get("/api/status", self.api_status)
        self.app.router.add_get("/api/sources", self.api_sources)
        self.app.router.add_post("/api/sources", self.api_add_source)
        self.app.router.add_delete(r"/api/sources/{channel_id:.+}", self.api_remove_source)
        self.app.router.add_post(r"/api/sources/{channel_id:.+}/enable", self.api_enable_source)
        self.app.router.add_post(r"/api/sources/{channel_id:.+}/disable", self.api_disable_source)
        self.app.router.add_post(r"/api/sources/{channel_id:.+}/filters", self.api_update_filters)
        self.app.router.add_post(r"/api/sources/{channel_id:.+}/start_date", self.api_update_source_start_date)
        self.app.router.add_post(r"/api/sources/{channel_id:.+}/method", self.api_update_forwarding_method)   # <-- NEW
        self.app.router.add_post(r"/api/sources/{channel_id:.+}/username", self.api_set_username)             # <-- NEW
        self.app.router.add_post("/api/forwarding", self.api_forwarding)
        self.app.router.add_post("/api/backfill", self.api_backfill)
        self.app.router.add_get("/api/tasks", self.api_tasks)
        self.app.router.add_post("/api/retry_failed", self.api_retry_failed)
        self.app.router.add_get("/api/failures", self.api_failures)
        self.app.router.add_get("/api/logs", self.api_logs)
        self.app.router.add_post("/api/send_text", self.api_send_text)
        self.app.router.add_get("/api/config", self.api_config)
        self.app.router.add_post("/api/config/discord_start_date", self.api_update_start_date)
        self.app.router.add_post("/api/scraper/toggle", self.api_toggle_scraper)
        self.app.router.add_get("/api/discovery/telegram", self.api_discover_telegram)
        self.app.router.add_get("/api/discovery/discord", self.api_discover_discord)

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.settings.web_ui_host, self.settings.web_ui_port)
        await site.start()
        logger.info("Web UI running at http://%s:%s", self.settings.web_ui_host, self.settings.web_ui_port)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def index(self, request: web.Request) -> web.Response:
        return web.Response(text=self.dashboard_html(), content_type="text/html")

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    # ---------- API endpoints ----------
    async def api_status(self, request: web.Request) -> web.Response:
        f = self.forwarder
        proc = psutil.Process(os.getpid())
        stats = f.store.get_stats()
        total_ok = stats.get("forwarded", 0)
        total_failed = stats.get("failed", 0)
        total = total_ok + total_failed
        scraper_running = f.discord_scraper is not None and f.discord_scraper._running
        return web.json_response(
            {
                "ok": True,
                "telegram_connected": f.telegram.is_connected(),
                "discord_connected": f.discord.is_ready() if f.discord else False,
                "forwarding_enabled": f.forwarding_enabled,
                "active_sources": len(f.store.get_sources(enabled=True)),
                "total_sources": len(f.store.get_sources()),
                "queue_queued": f.store.queue_size("queued"),
                "queue_failed": f.store.queue_size("failed"),
                "uptime_seconds": int(time.time() - f.started_at),
                "memory_mb": round(proc.memory_info().rss / 1024 / 1024, 1),
                "cpu_percent": psutil.cpu_percent(interval=0.05),
                "disk_percent": psutil.disk_usage(".").percent,
                "stats": stats,
                "success_rate": round(total_ok / total * 100, 1) if total else 0,
                "discord_scraper_running": scraper_running,
                "discord_start_date": f.settings.discord_start_date,
            }
        )

    async def api_sources(self, request: web.Request) -> web.Response:
        """Return all sources with additional fields (scrape_required, last_scraped_id, forwarding_method)."""
        sources = self.forwarder.store.get_sources()
        result = []
        for s in sources:
            d = s.__dict__.copy()
            d["scrape_required"] = s.scrape_required
            d["last_scraped_id"] = s.last_scraped_id
            # forwarding_method is stored inside filters
            d["forwarding_method"] = s.filters.get("forwarding_method", "auto")
            result.append(d)
        return web.json_response({"ok": True, "sources": result})

    async def api_add_source(self, request: web.Request) -> web.Response:
        data = await self.json_body(request)
        platform = str(data.get("platform", "")).lower()
        raw_channel = str(data.get("channel", "")).strip()
        filters = data.get("filters") or {}
        if isinstance(filters, str):
            filters = json.loads(filters or "{}")
        start_date = data.get("start_date") or None
        # Allow passing forwarding_method and username in filters
        if "forwarding_method" in data:
            filters["forwarding_method"] = data["forwarding_method"]
        if "username" in data:
            filters["username"] = data["username"]
        channel_id = await self.forwarder.normalize_channel_id(platform, raw_channel)
        self.forwarder.store.add_source(platform, channel_id, filters, start_date)
        # If forwarding_method is 'scrape', set scrape_required
        if filters.get("forwarding_method") == "scrape":
            self.forwarder.store.set_scrape_required(channel_id, True)
        return web.json_response({"ok": True, "channel_id": channel_id})

    async def api_remove_source(self, request: web.Request) -> web.Response:
        channel_id = unquote(request.match_info["channel_id"])
        self.forwarder.store.remove_source(channel_id)
        return web.json_response({"ok": True})

    async def api_enable_source(self, request: web.Request) -> web.Response:
        channel_id = unquote(request.match_info["channel_id"])
        self.forwarder.store.set_source_enabled(channel_id, True)
        return web.json_response({"ok": True})

    async def api_disable_source(self, request: web.Request) -> web.Response:
        channel_id = unquote(request.match_info["channel_id"])
        self.forwarder.store.set_source_enabled(channel_id, False)
        return web.json_response({"ok": True})

    async def api_update_filters(self, request: web.Request) -> web.Response:
        channel_id = unquote(request.match_info["channel_id"])
        data = await self.json_body(request)
        filters = data.get("filters") or {}
        if isinstance(filters, str):
            filters = json.loads(filters or "{}")
        src = self.forwarder.store.get_source(channel_id)
        if not src:
            return web.json_response({"ok": False, "error": "source not found"}, status=404)
        self.forwarder.store.add_source(src.platform, channel_id, filters, src.start_date)
        self.forwarder.store.set_source_enabled(channel_id, src.enabled)
        return web.json_response({"ok": True})

    async def api_update_source_start_date(self, request: web.Request) -> web.Response:
        channel_id = unquote(request.match_info["channel_id"])
        data = await self.json_body(request)
        date_str = data.get("date", "").strip() or None
        if date_str:
            try:
                datetime.fromisoformat(date_str)
            except ValueError:
                return web.json_response({"ok": False, "error": "Invalid date format. Use YYYY-MM-DD"}, status=400)
        self.forwarder.store.update_source_start_date(channel_id, date_str)
        # Reset seen messages for this channel so old messages become eligible
        if self.forwarder.discord_scraper:
            self.forwarder.discord_scraper.reset_seen(channel_id)
        return web.json_response({"ok": True, "start_date": date_str})

    # ---------- New endpoints for forwarding method and username ----------
    async def api_update_forwarding_method(self, request: web.Request) -> web.Response:
        channel_id = unquote(request.match_info["channel_id"])
        data = await self.json_body(request)
        method = data.get("method")  # 'auto', 'api', 'scrape'
        if method not in ("auto", "api", "scrape"):
            return web.json_response({"ok": False, "error": "method must be auto, api, or scrape"}, status=400)
        src = self.forwarder.store.get_source(channel_id)
        if not src:
            return web.json_response({"ok": False, "error": "source not found"}, status=404)
        # Update filters with new method
        filters = src.filters or {}
        filters["forwarding_method"] = method
        self.forwarder.store.add_source(src.platform, channel_id, filters, src.start_date)
        # If method is 'scrape', also set scrape_required to True
        if method == "scrape":
            self.forwarder.store.set_scrape_required(channel_id, True)
        return web.json_response({"ok": True})

    async def api_set_username(self, request: web.Request) -> web.Response:
        channel_id = unquote(request.match_info["channel_id"])
        data = await self.json_body(request)
        username = data.get("username")
        if not username:
            return web.json_response({"ok": False, "error": "username required"}, status=400)
        src = self.forwarder.store.get_source(channel_id)
        if not src:
            return web.json_response({"ok": False, "error": "source not found"}, status=404)
        filters = src.filters or {}
        filters["username"] = username.strip()
        self.forwarder.store.add_source(src.platform, channel_id, filters, src.start_date)
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------

    async def api_forwarding(self, request: web.Request) -> web.Response:
        data = await self.json_body(request)
        action = data.get("action")
        if action == "pause":
            self.forwarder.forwarding_enabled = False
        elif action == "resume":
            self.forwarder.forwarding_enabled = True
        else:
            return web.json_response({"ok": False, "error": "action must be pause or resume"}, status=400)
        return web.json_response({"ok": True, "forwarding_enabled": self.forwarder.forwarding_enabled})

    async def api_backfill(self, request: web.Request) -> web.Response:
        data = await self.json_body(request)
        platform = str(data.get("platform", "")).lower()
        source = str(data.get("source", "")).strip()
        limit = int(data.get("limit") or 100)
        task_id = f"backfill-{int(time.time())}-{len(self.tasks) + 1}"
        self.tasks[task_id] = {"id": task_id, "type": "backfill", "platform": platform, "source": source, "limit": limit, "status": "running", "started_at": time.time()}

        async def run_task():
            try:
                if platform == "telegram":
                    ok, failed = await self.forwarder.backfill_telegram(source, limit)
                elif platform == "discord":
                    ok, failed = await self.forwarder.backfill_discord(source, limit)
                else:
                    raise ValueError("platform must be telegram or discord")
                self.tasks[task_id].update({"status": "done", "forwarded": ok, "failed": failed, "finished_at": time.time()})
            except Exception as exc:
                logger.exception("UI backfill failed")
                self.tasks[task_id].update({"status": "failed", "error": str(exc), "finished_at": time.time()})

        asyncio.create_task(run_task())
        return web.json_response({"ok": True, "task_id": task_id})

    async def api_tasks(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "tasks": list(reversed(list(self.tasks.values())))[:50]})

    async def api_retry_failed(self, request: web.Request) -> web.Response:
        n = self.forwarder.store.retry_failed_queue()
        return web.json_response({"ok": True, "requeued": n})

    async def api_failures(self, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", 20))
        return web.json_response({"ok": True, "failures": self.forwarder.store.recent_failures(limit)})

    async def api_logs(self, request: web.Request) -> web.Response:
        path = self.settings.data_dir / "logs" / "forwarder.log"
        lines = int(request.query.get("lines", 200))
        text = tail_file(path, lines)
        return web.json_response({"ok": True, "logs": text})

    async def api_send_text(self, request: web.Request) -> web.Response:
        data = await self.json_body(request)
        text = str(data.get("text", "")).strip()
        if not text:
            return web.json_response({"ok": False, "error": "text is required"}, status=400)
        await self.forwarder.telegram.send_message(self.settings.destination_channel_id, text[:4096])
        return web.json_response({"ok": True})

    async def api_config(self, request: web.Request) -> web.Response:
        s = self.settings
        return web.json_response(
            {
                "ok": True,
                "config": {
                    "destination_channel_id": s.destination_channel_id,
                    "max_file_size_mb": s.max_file_size_mb,
                    "compress_images": s.compress_images,
                    "max_image_size_mb": s.max_image_size_mb,
                    "convert_webp_to_jpg": s.convert_webp_to_jpg,
                    "generate_video_thumbnails": s.generate_video_thumbnails,
                    "transcode_videos": s.transcode_videos,
                    "watermark_enabled": bool(s.watermark_text),
                    "notify_protected_content": s.notify_protected_content,
                    "web_ui_host": s.web_ui_host,
                    "web_ui_port": s.web_ui_port,
                    "discord_start_date": s.discord_start_date,
                },
            }
        )

    async def api_update_start_date(self, request: web.Request) -> web.Response:
        data = await self.json_body(request)
        date_str = data.get("date", "").strip() or None
        if date_str:
            try:
                datetime.fromisoformat(date_str)
            except ValueError:
                return web.json_response({"ok": False, "error": "Invalid date format. Use YYYY-MM-DD"}, status=400)
        # Update .env
        env_path = Path(".env")
        if env_path.exists():
            lines = env_path.read_text().splitlines()
            new_lines = []
            found = False
            for line in lines:
                if line.startswith("DISCORD_START_DATE="):
                    new_lines.append(f"DISCORD_START_DATE={date_str or ''}")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f"DISCORD_START_DATE={date_str or ''}")
            env_path.write_text("\n".join(new_lines))
        if self.forwarder.discord_scraper:
            if date_str:
                dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
                self.forwarder.discord_scraper.start_date = dt
            else:
                self.forwarder.discord_scraper.start_date = None
            logger.info(f"Updated global Discord start date to: {date_str or 'None'}")
        return web.json_response({"ok": True, "date": date_str})

    async def api_toggle_scraper(self, request: web.Request) -> web.Response:
        data = await self.json_body(request)
        action = data.get("action")
        scraper = self.forwarder.discord_scraper
        if not scraper:
            return web.json_response({"ok": False, "error": "Scraper not configured"}, status=400)
        if action == "stop":
            scraper._running = False
            await scraper.stop()
            return web.json_response({"ok": True, "running": False})
        elif action == "start":
            if not scraper._running:
                scraper._running = True
                if not hasattr(self.forwarder, '_scraper_task') or self.forwarder._scraper_task.done():
                    self.forwarder._scraper_task = asyncio.create_task(scraper.poll_channels())
                return web.json_response({"ok": True, "running": True})
        return web.json_response({"ok": False, "error": "Invalid action"}, status=400)

    async def api_discover_telegram(self, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", 200))
        items = []
        async for dialog in self.forwarder.telegram.iter_dialogs(limit=limit):
            if dialog.is_channel or dialog.is_group:
                entity = dialog.entity
                username = getattr(entity, "username", None)
                items.append(
                    {
                        "id": str(dialog.id),
                        "name": dialog.name,
                        "username": username,
                        "type": "channel" if dialog.is_channel else "group",
                        "input": f"@{username}" if username else str(dialog.id),
                    }
                )
        return web.json_response({"ok": True, "items": items})

    async def api_discover_discord(self, request: web.Request) -> web.Response:
        sources = self.forwarder.store.get_sources(platform="discord")
        items = []
        for s in sources:
            items.append({
                "id": s.channel_id,
                "name": f"Discord #{s.channel_id}",
                "type": "discord",
                "input": s.channel_id,
                "start_date": s.start_date or "",
                "enabled": s.enabled,
            })
        return web.json_response({"ok": True, "items": items})

    @staticmethod
    async def json_body(request: web.Request) -> dict[str, Any]:
        try:
            return await request.json()
        except Exception:
            data = await request.post()
            return dict(data)

    @staticmethod
    def login_html() -> str:
        return """<!doctype html><html><head><meta charset='utf-8'><title>Forwarder Login</title>
<style>body{font-family:Inter,system-ui,sans-serif;background:#0f172a;color:#e5e7eb;display:grid;place-items:center;height:100vh;margin:0}.box{background:#111827;border:1px solid #334155;border-radius:18px;padding:28px;box-shadow:0 20px 60px #0008;width:min(420px,90vw)}input,button{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #475569;background:#020617;color:#e5e7eb;margin-top:10px}button{background:#2563eb;border:0;font-weight:700;cursor:pointer}</style></head>
<body><div class='box'><h1>Media Forwarder</h1><p>Enter your Web UI token.</p><input id='t' type='password' placeholder='Token'><button onclick='login()'>Open Dashboard</button></div>
<script>function login(){document.cookie='forwarder_token='+encodeURIComponent(document.getElementById('t').value)+';path=/;SameSite=Lax';location.reload()}</script></body></html>"""

    @staticmethod
    def dashboard_html() -> str:
        return DASHBOARD_HTML


def tail_file(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        block = 4096
        data = b""
        while size > 0 and data.count(b"\n") <= lines:
            step = min(block, size)
            size -= step
            f.seek(size)
            data = f.read(step) + data
        return b"\n".join(data.splitlines()[-lines:]).decode("utf-8", errors="replace")


DASHBOARD_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Media Forwarder Control Panel</title>
<style>
:root{--bg:#0b1120;--card:#111827;--muted:#94a3b8;--text:#e5e7eb;--line:#243044;--blue:#3b82f6;--green:#22c55e;--red:#ef4444;--amber:#f59e0b;--purple:#a855f7}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top left,#1e3a8a55,transparent 35%),var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}.app{display:grid;grid-template-columns:260px 1fr;min-height:100vh}.side{border-right:1px solid var(--line);background:#020617aa;backdrop-filter:blur(10px);padding:22px;position:sticky;top:0;height:100vh}.brand{font-size:22px;font-weight:800;margin-bottom:4px}.sub{color:var(--muted);font-size:13px;margin-bottom:24px}.nav button{display:block;width:100%;text-align:left;background:transparent;color:var(--text);border:1px solid transparent;border-radius:12px;padding:12px;margin:6px 0;cursor:pointer}.nav button.active,.nav button:hover{background:#1f2937;border-color:#334155}.main{padding:24px;max-width:1400px}.top{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:20px}.pill{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:999px;padding:8px 12px;background:#020617aa;color:var(--muted)}.dot{width:10px;height:10px;border-radius:50%;background:var(--red)}.dot.ok{background:var(--green)}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.card{background:#111827cc;border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:0 12px 40px #0004}.metric{font-size:28px;font-weight:800;margin-top:8px}.label{color:var(--muted);font-size:13px}.section{display:none}.section.active{display:block}.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}.form{display:grid;gap:10px}input,select,textarea{width:100%;border:1px solid #334155;border-radius:12px;background:#020617;color:var(--text);padding:11px}textarea{min-height:110px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.btn{border:0;border-radius:12px;padding:11px 14px;background:var(--blue);color:white;font-weight:750;cursor:pointer}.btn.secondary{background:#334155}.btn.green{background:var(--green);color:#052e16}.btn.red{background:var(--red)}.btn.amber{background:var(--amber);color:#451a03}.btn.purple{background:var(--purple)}table{width:100%;border-collapse:collapse}th,td{text-align:left;border-bottom:1px solid var(--line);padding:10px;vertical-align:top}th{color:var(--muted);font-size:12px}code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}pre{background:#020617;border:1px solid var(--line);border-radius:14px;padding:14px;overflow:auto;max-height:520px}.actions{display:flex;gap:8px;flex-wrap:wrap}.toast{position:fixed;right:18px;bottom:18px;background:#020617;border:1px solid #334155;border-radius:14px;padding:14px;display:none}.muted{color:var(--muted)}@media(max-width:900px){.app{grid-template-columns:1fr}.side{position:relative;height:auto}.grid,.row{grid-template-columns:1fr}.top{display:block}}
.source-card{background:#111827cc;border:1px solid var(--line);border-radius:18px;padding:16px;margin-bottom:12px}.source-card .header{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}.source-card .status{display:flex;align-items:center;gap:6px}.source-card .status .dot{width:10px;height:10px;border-radius:50%}.source-card .status .dot.enabled{background:var(--green)}.source-card .status .dot.disabled{background:var(--red)}.source-card .details{display:flex;flex-wrap:wrap;gap:12px;margin-top:10px;align-items:center}.source-card .details label{font-size:13px;color:var(--muted)}.source-card .details input[type=date]{width:160px}.source-card .filters-collapse{margin-top:10px;border-top:1px solid var(--line);padding-top:10px;display:none}.source-card .filters-collapse.open{display:block}.source-card .actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand">Media Forwarder</div>
    <div class="sub">Telegram + Discord control panel</div>
    <div class="nav">
      <button class="active" onclick="show('overview',this)">📊 Overview</button>
      <button onclick="show('sources',this)">📡 Sources</button>
      <button onclick="show('backfill',this)">📦 Backfill</button>
      <button onclick="show('queue',this)">🔁 Queue &amp; Failures</button>
      <button onclick="show('send',this)">✉️ Send Text</button>
      <button onclick="show('logs',this)">🧾 Logs</button>
      <button onclick="show('settings',this)">⚙️ Settings</button>
    </div>
  </aside>
  <main class="main">
    <div class="top">
      <div><h1 id="title">Overview</h1><div class="muted">Manage all forwarding functions without Telegram commands.</div></div>
      <div class="actions"><span class="pill"><span id="tgDot" class="dot"></span>Telegram</span><span class="pill"><span id="dcDot" class="dot"></span>Discord</span><button id="pauseBtn" class="btn amber" onclick="toggleForwarding()">Pause</button><button id="scraperBtn" class="btn secondary" onclick="toggleScraper()">Scraper Running</button></div>
    </div>

    <section id="overview" class="section active">
      <div class="grid">
        <div class="card"><div class="label">Forwarded</div><div id="mForwarded" class="metric">0</div></div>
        <div class="card"><div class="label">Failed</div><div id="mFailed" class="metric">0</div></div>
        <div class="card"><div class="label">Success Rate</div><div id="mSuccess" class="metric">0%</div></div>
        <div class="card"><div class="label">Queue</div><div id="mQueue" class="metric">0</div></div>
      </div>
      <div class="row" style="margin-top:14px"><div class="card"><h3>System</h3><div id="systemBox"></div></div><div class="card"><h3>Stats</h3><pre id="statsBox">{}</pre></div></div>
    </section>

    <section id="sources" class="section">
      <div class="card">
        <h3>➕ Add Source</h3>
        <div class="form" style="grid-template-columns:1fr 1fr auto; align-items:end">
          <label>Platform<select id="addPlatform"><option value="telegram">Telegram</option><option value="discord">Discord</option></select></label>
          <label>Channel ID / Username<input id="addChannel" placeholder="@channel or -100..."></label>
          <label>Start Date (optional)<input type="date" id="addStartDate"></label>
          <label style="grid-column:1/-1">Filters (JSON)<textarea id="addFilters" rows="2" style="min-height:60px">{}</textarea></label>
          <button class="btn green" onclick="addSource()" style="grid-column:1">Add Source</button>
          <button class="btn secondary" onclick="loadTelegramSuggestions()" style="grid-column:2">Telegram Suggestions</button>
          <button class="btn secondary" onclick="loadDiscordSuggestions()" style="grid-column:3">My Discord Sources</button>
        </div>
        <div id="suggestionsBox" style="margin-top:10px;display:none"></div>
      </div>
      <div id="sourcesContainer"></div>
    </section>

    <section id="backfill" class="section">
      <div class="row"><div class="card"><h3>📦 Historical Backfill</h3><div class="form"><label>Platform<select id="bfPlatform"><option value="telegram">Telegram</option><option value="discord">Discord</option></select></label><label>Source<input id="bfSource" placeholder="Channel ID / username"></label><label>Limit<input id="bfLimit" type="number" value="100" min="1"></label><button class="btn purple" onclick="startBackfill()">Start Backfill</button></div></div><div class="card"><h3>Tasks</h3><div id="tasksBox">No tasks.</div></div></div>
    </section>

    <section id="queue" class="section">
      <div class="card"><div class="actions"><button class="btn" onclick="retryFailed()">Retry Failed Queue</button><button class="btn secondary" onclick="loadFailures()">Refresh Failures</button></div><h3>Recent failures</h3><div id="failuresBox">None.</div></div>
    </section>

    <section id="send" class="section">
      <div class="card"><h3>✉️ Send a text message to destination</h3><textarea id="sendText" placeholder="Message text"></textarea><button class="btn green" onclick="sendTextMsg()">Send to Destination</button></div>
    </section>

    <section id="logs" class="section">
      <div class="card"><div class="actions"><button class="btn secondary" onclick="loadLogs()">Refresh Logs</button></div><pre id="logsBox"></pre></div>
    </section>

    <section id="settings" class="section">
      <div class="card"><h3>🌐 Global Discord Start Date</h3><p class="muted">Default start date for new sources. Leave empty to forward all.</p><div class="form" style="grid-template-columns:auto 1fr auto"><label>Date<input type="date" id="startDateInput" value=""></label><button class="btn" onclick="updateStartDate()">Set Global Date</button></div></div>
      <div class="card"><h3>🔧 Scraper Control</h3><button class="btn amber" onclick="toggleScraper()">Stop Scraper</button></div>
      <div class="card"><h3>⚙️ Runtime Config</h3><pre id="configBox">{}</pre></div>
    </section>
  </main>
</div>
<div id="toast" class="toast"></div>
<script>
let statusCache={};
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.style.display='block';setTimeout(()=>t.style.display='none',3500)}
async function api(path, opts={}){opts.headers=Object.assign({'Content-Type':'application/json'}, opts.headers||{});const r=await fetch(path,opts);const j=await r.json().catch(()=>({ok:false,error:'bad json'}));if(!r.ok||j.ok===false)throw new Error(j.error||r.statusText);return j}
function show(id,btn){document.querySelectorAll('.section').forEach(s=>s.classList.remove('active'));document.getElementById(id).classList.add('active');document.querySelectorAll('.nav button').forEach(b=>b.classList.remove('active'));btn.classList.add('active');document.getElementById('title').textContent=btn.textContent.trim(); if(id==='sources')loadSources(); if(id==='queue')loadFailures(); if(id==='logs')loadLogs(); if(id==='settings')loadConfig(); if(id==='backfill')loadTasks();}
async function refresh(){try{const s=await api('/api/status');statusCache=s;tgDot.className='dot '+(s.telegram_connected?'ok':'');dcDot.className='dot '+(s.discord_connected?'ok':'');pauseBtn.textContent=s.forwarding_enabled?'Pause':'Resume';pauseBtn.className='btn '+(s.forwarding_enabled?'amber':'green');scraperBtn.textContent=s.discord_scraper_running?'Scraper Running':'Scraper Stopped';scraperBtn.className='btn '+(s.discord_scraper_running?'green':'secondary');mForwarded.textContent=s.stats.forwarded||0;mFailed.textContent=s.stats.failed||0;mSuccess.textContent=s.success_rate+'%';mQueue.textContent=s.queue_queued+'/'+s.queue_failed;systemBox.innerHTML=`<p>Forwarding: <b>${s.forwarding_enabled?'Enabled':'Paused'}</b></p><p>Sources: <b>${s.active_sources}</b> active / ${s.total_sources} total</p><p>Uptime: <b>${Math.floor(s.uptime_seconds/3600)}h ${Math.floor(s.uptime_seconds%3600/60)}m</b></p><p>Memory: <b>${s.memory_mb}MB</b> CPU: <b>${s.cpu_percent}%</b> Disk: <b>${s.disk_percent}%</b></p><p>Global discord start date: <b>${s.discord_start_date||'None'}</b></p>`;statsBox.textContent=JSON.stringify(s.stats,null,2);if(document.getElementById('startDateInput')){document.getElementById('startDateInput').value=s.discord_start_date||''}}catch(e){toast(e.message)}}
async function toggleForwarding(){const action=statusCache.forwarding_enabled?'pause':'resume';await api('/api/forwarding',{method:'POST',body:JSON.stringify({action})});toast('Forwarding '+action+'d');refresh()}
async function toggleScraper(){const running=statusCache.discord_scraper_running;const action=running?'stop':'start';await api('/api/scraper/toggle',{method:'POST',body:JSON.stringify({action})});toast('Scraper '+action+'ed');refresh()}
async function updateStartDate(){const date=document.getElementById('startDateInput').value;await api('/api/config/discord_start_date',{method:'POST',body:JSON.stringify({date})});toast('Global start date updated');refresh()}
async function setSourceDate(id){const date=document.getElementById('sd_'+decodeURIComponent(id)).value;await api('/api/sources/'+id+'/start_date',{method:'POST',body:JSON.stringify({date})});toast('Source start date updated');loadSources()}
async function setDateToday(id){const today=new Date().toISOString().split('T')[0];document.getElementById('sd_'+decodeURIComponent(id)).value=today;await setSourceDate(id);}
async function toggleFilters(id){const el=document.getElementById('filters_'+decodeURIComponent(id));el.classList.toggle('open');}
async function loadSources(){const j=await api('/api/sources');const container=document.getElementById('sourcesContainer');if(!j.sources.length){container.innerHTML='<p class="muted">No sources configured. Add one above.</p>';return;}let html='';for(const s of j.sources){const dateVal=s.start_date||'';const enabled=s.enabled;html+=`
<div class="source-card">
  <div class="header">
    <div class="status"><span class="dot ${enabled?'enabled':'disabled'}"></span> <strong>${s.platform}</strong> <code>${s.channel_id}</code> ${enabled?'✅':'⏸️'}</div>
    <div class="actions">
      <button class="btn ${enabled?'amber':'green'}" onclick="toggleSource('${encodeURIComponent(s.channel_id)}','${enabled?'disable':'enable'}')">${enabled?'Disable':'Enable'}</button>
      <button class="btn red" onclick="removeSource('${encodeURIComponent(s.channel_id)}')">Remove</button>
    </div>
  </div>
  <div class="details">
    <label>Start Date <input type="date" id="sd_${s.channel_id}" value="${dateVal}"></label>
    <button class="btn secondary" onclick="setSourceDate('${encodeURIComponent(s.channel_id)}')">Set Date</button>
    <button class="btn green" onclick="setDateToday('${encodeURIComponent(s.channel_id)}')">Set Today</button>
    <button class="btn secondary" onclick="toggleFilters('${encodeURIComponent(s.channel_id)}')">Filters</button>
  </div>
  <div class="filters-collapse" id="filters_${s.channel_id}">
    <label>Filters (JSON)</label>
    <textarea id="f_${s.channel_id}" style="min-height:70px;width:100%">${escapeHtml(JSON.stringify(s.filters||{},null,2))}</textarea>
    <button class="btn secondary" onclick="saveFilters('${encodeURIComponent(s.channel_id)}')">Save Filters</button>
  </div>
</div>`;}container.innerHTML=html;}
async function toggleSource(id,action){await api('/api/sources/'+id+'/'+action,{method:'POST'});toast(action);loadSources();refresh()}
async function removeSource(id){if(!confirm('Remove source?'))return;await api('/api/sources/'+id,{method:'DELETE'});toast('Removed');loadSources();refresh()}
async function saveFilters(id){const raw=document.getElementById('f_'+decodeURIComponent(id)).value;await api('/api/sources/'+id+'/filters',{method:'POST',body:JSON.stringify({filters:JSON.parse(raw||'{}')})});toast('Filters saved');loadSources()}
async function addSource(){const startDate=document.getElementById('addStartDate').value;const filters=document.getElementById('addFilters').value;await api('/api/sources',{method:'POST',body:JSON.stringify({platform:addPlatform.value,channel:addChannel.value,filters:JSON.parse(filters||'{}'),start_date:startDate||null})});toast('Source added');addChannel.value='';addStartDate.value='';loadSources();refresh()}
async function loadTelegramSuggestions(){const j=await api('/api/discovery/telegram?limit=300');renderSuggestions('telegram',j.items)}
async function loadDiscordSuggestions(){const j=await api('/api/discovery/discord');renderSuggestions('discord',j.items)}
function renderSuggestions(platform,items){const box=document.getElementById('suggestionsBox');if(!items.length){box.innerHTML='<p class="muted">No sources found.</p>';box.style.display='block';return;}let h='<table><tr><th>Name</th><th>ID/Input</th><th>Action</th></tr>';for(const it of items){h+=`<tr><td>${escapeHtml(it.name||'')}</td><td><code>${escapeHtml(it.input||it.id)}</code></td><td><button class="btn green" onclick="pickSuggestion('${platform}','${escapeHtml(String(it.input||it.id)).replace(/'/g,'&#39;')}')">Use</button></td></tr>`}h+='</table>';box.innerHTML=h;box.style.display='block';}
function pickSuggestion(platform,input){addPlatform.value=platform;addChannel.value=input;toast('Selected '+input);document.getElementById('suggestionsBox').style.display='none';}
async function startBackfill(){const j=await api('/api/backfill',{method:'POST',body:JSON.stringify({platform:bfPlatform.value,source:bfSource.value,limit:Number(bfLimit.value||100)})});toast('Started '+j.task_id);loadTasks()}
async function loadTasks(){const j=await api('/api/tasks');let h='';for(const t of j.tasks){h+=`<div>${t.id} – ${t.status} ${t.status==='done'?`✅ ${t.forwarded} / ❌ ${t.failed}`:t.error||''}</div>`}tasksBox.innerHTML=h||'No tasks.'}
async function retryFailed(){const j=await api('/api/retry_failed',{method:'POST'});toast('Requeued '+j.requeued);refresh()}
async function loadFailures(){const j=await api('/api/failures?limit=50');let h='';for(const f of j.failures){h+=`<div>${f.created_at} – ${f.platform} ${f.channel_id}/${f.message_id} – ${escapeHtml(f.error||'')}</div>`}failuresBox.innerHTML=h||'No failures.'}
async function loadLogs(){const j=await api('/api/logs?lines=300');logsBox.textContent=j.logs||''}
async function loadConfig(){const j=await api('/api/config');configBox.textContent=JSON.stringify(j.config,null,2);if(j.config.discord_start_date)document.getElementById('startDateInput').value=j.config.discord_start_date}
async function sendTextMsg(){await api('/api/send_text',{method:'POST',body:JSON.stringify({text:sendText.value})});toast('Sent');sendText.value=''}
function escapeHtml(s){return String(s).replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]))}
refresh();loadSources();setInterval(refresh,5000);setInterval(()=>{if(document.getElementById('backfill').classList.contains('active'))loadTasks()},4000);
</script>
</body>
</html>
"""
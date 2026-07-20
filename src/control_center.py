from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from aiohttp import web
from dotenv import dotenv_values
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

ROOT = Path.cwd()
ENV_PATH = ROOT / ".env"
PROCESS: subprocess.Popen | None = None
LOGIN_CLIENT: TelegramClient | None = None
LOGIN_PHONE: str | None = None

DEFAULTS = {
    "TELEGRAM_API_ID": "",
    "TELEGRAM_API_HASH": "",
    "TELEGRAM_PHONE": "",
    "TELEGRAM_SESSION": "data/telegram_user",
    "DESTINATION_CHANNEL_ID": "",
    "DISCORD_TOKEN": "",
    "ADMIN_USER_ID": "",
    "DATA_DIR": "data",
    "TMP_DIR": "tmp",
    "LOG_LEVEL": "INFO",
    "MAX_FILE_SIZE_MB": "45",
    "COMPRESS_IMAGES": "true",
    "MAX_IMAGE_SIZE_MB": "5",
    "CONVERT_WEBP_TO_JPG": "true",
    "GENERATE_VIDEO_THUMBNAILS": "true",
    "TRANSCODE_VIDEOS": "false",
    "WATERMARK_TEXT": "",
    "INCLUDE_SOURCE": "true",
    "INCLUDE_AUTHOR": "true",
    "INCLUDE_TIMESTAMP": "true",
    "INCLUDE_LINK": "true",
    "QUEUE_MAX_RETRIES": "3",
    "NOTIFY_PROTECTED_CONTENT": "true",
    "WEB_UI_ENABLED": "true",
    "WEB_UI_HOST": "127.0.0.1",
    "WEB_UI_PORT": "8080",
    "WEB_UI_TOKEN": "change-me-strong-token",
}


def load_env() -> dict[str, str]:
    vals = DEFAULTS.copy()
    if ENV_PATH.exists():
        vals.update({k: v or "" for k, v in dotenv_values(ENV_PATH).items()})
    return vals


def write_env(values: dict[str, Any]) -> None:
    merged = load_env()
    for k in DEFAULTS:
        if k in values:
            merged[k] = str(values[k]).strip()
    lines = [f"{k}={merged.get(k, '')}" for k in DEFAULTS]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    Path(merged.get("DATA_DIR", "data")).mkdir(exist_ok=True)
    Path(merged.get("TMP_DIR", "tmp")).mkdir(exist_ok=True)


async def index(request: web.Request) -> web.Response:
    return web.Response(text=HTML, content_type="text/html")


async def api_config(request: web.Request) -> web.Response:
    vals = load_env()
    safe = vals.copy()
    if safe.get("DISCORD_TOKEN"):
        safe["DISCORD_TOKEN_MASKED"] = safe["DISCORD_TOKEN"][:8] + "..." + safe["DISCORD_TOKEN"][-4:]
    return web.json_response({"ok": True, "config": vals, "process_running": PROCESS is not None and PROCESS.poll() is None})


async def api_save(request: web.Request) -> web.Response:
    data = await request.json()
    write_env(data)
    return web.json_response({"ok": True})


async def api_telegram_send_code(request: web.Request) -> web.Response:
    global LOGIN_CLIENT, LOGIN_PHONE
    data = await request.json()
    write_env(data)
    vals = load_env()
    api_id = int(vals.get("TELEGRAM_API_ID") or 0)
    api_hash = vals.get("TELEGRAM_API_HASH") or ""
    phone = vals.get("TELEGRAM_PHONE") or ""
    if not api_id or not api_hash or not phone:
        return web.json_response({"ok": False, "error": "Telegram API ID, API Hash, and phone are required"}, status=400)
    if LOGIN_CLIENT:
        await LOGIN_CLIENT.disconnect()
    LOGIN_CLIENT = TelegramClient(vals.get("TELEGRAM_SESSION") or "data/telegram_user", api_id, api_hash)
    await LOGIN_CLIENT.connect()
    if await LOGIN_CLIENT.is_user_authorized():
        await LOGIN_CLIENT.disconnect()
        LOGIN_CLIENT = None
        return web.json_response({"ok": True, "already_authorized": True})
    await LOGIN_CLIENT.send_code_request(phone)
    LOGIN_PHONE = phone
    return web.json_response({"ok": True, "sent": True})


async def api_telegram_sign_in(request: web.Request) -> web.Response:
    global LOGIN_CLIENT, LOGIN_PHONE
    data = await request.json()
    code = str(data.get("code", "")).strip()
    password = str(data.get("password", "")).strip()
    if not LOGIN_CLIENT or not LOGIN_PHONE:
        return web.json_response({"ok": False, "error": "Send code first"}, status=400)
    try:
        if password:
            await LOGIN_CLIENT.sign_in(password=password)
        else:
            await LOGIN_CLIENT.sign_in(LOGIN_PHONE, code)
        me = await LOGIN_CLIENT.get_me()
        await LOGIN_CLIENT.disconnect()
        LOGIN_CLIENT = None
        return web.json_response({"ok": True, "authorized": True, "user_id": getattr(me, "id", None), "username": getattr(me, "username", None)})
    except SessionPasswordNeededError:
        return web.json_response({"ok": True, "needs_password": True})


async def api_launch(request: web.Request) -> web.Response:
    global PROCESS
    if PROCESS is not None and PROCESS.poll() is None:
        return web.json_response({"ok": True, "already_running": True, "pid": PROCESS.pid})
    env = os.environ.copy()
    env.update(load_env())
    src = str(ROOT / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    PROCESS = subprocess.Popen([sys.executable, "-m", "media_forwarder.app"], cwd=str(ROOT), env=env)
    return web.json_response({"ok": True, "pid": PROCESS.pid, "url": f"http://{env.get('WEB_UI_HOST','127.0.0.1')}:{env.get('WEB_UI_PORT','8080')}"})


async def api_stop(request: web.Request) -> web.Response:
    global PROCESS
    if PROCESS is not None and PROCESS.poll() is None:
        PROCESS.terminate()
        return web.json_response({"ok": True, "stopped": True})
    return web.json_response({"ok": True, "stopped": False})


HTML = r"""
<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Forwarder Setup</title>
<style>:root{--bg:#0b1120;--card:#111827;--line:#243044;--text:#e5e7eb;--muted:#94a3b8;--blue:#3b82f6;--green:#22c55e;--red:#ef4444}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top left,#1d4ed855,transparent 35%),var(--bg);color:var(--text);font-family:Inter,system-ui,Segoe UI,Arial,sans-serif}.wrap{max-width:1180px;margin:0 auto;padding:28px}.hero{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}.card{background:#111827dd;border:1px solid var(--line);border-radius:18px;padding:20px;box-shadow:0 15px 50px #0005;margin:14px 0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.form{display:grid;grid-template-columns:1fr 1fr;gap:12px}label{display:grid;gap:6px;color:var(--muted);font-size:13px}input,select{background:#020617;border:1px solid #334155;border-radius:12px;color:var(--text);padding:12px;width:100%}.btn{border:0;border-radius:12px;padding:12px 16px;background:var(--blue);color:#fff;font-weight:800;cursor:pointer}.btn.green{background:var(--green);color:#052e16}.btn.red{background:var(--red)}.actions{display:flex;gap:10px;flex-wrap:wrap}.muted{color:var(--muted)}code{background:#020617;border:1px solid #334155;border-radius:8px;padding:2px 6px}a{color:#93c5fd}ul{line-height:1.7}.full{grid-column:1/-1}@media(max-width:900px){.grid,.form,.hero{display:block}}</style></head><body><div class="wrap">
<div class="hero"><div><h1>Media Forwarder Setup</h1><p class="muted">Enter credentials here, save, then launch the full control panel for selecting channels, filters, backfill, queue, logs, and forwarding.</p></div><div class="actions"><button class="btn" onclick="save()">Save Credentials</button><button class="btn green" onclick="launch()">Launch Forwarder UI</button><button class="btn red" onclick="stopApp()">Stop Forwarder</button></div></div>
<div class="card"><h2>Required credentials</h2><div class="form">
<label>Telegram API ID<input id="TELEGRAM_API_ID" placeholder="12345678"></label>
<label>Telegram API Hash<input id="TELEGRAM_API_HASH" placeholder="abcdef..."></label>
<label>Telegram phone<input id="TELEGRAM_PHONE" placeholder="+1234567890"></label>
<label>Destination Telegram Channel ID<input id="DESTINATION_CHANNEL_ID" placeholder="-1001234567890"></label>
<label>Discord Bot Token<input id="DISCORD_TOKEN" placeholder="Only works for bot-visible servers/channels"></label>
<label>Admin Telegram User ID<input id="ADMIN_USER_ID" placeholder="123456789"></label>
<label>Web UI Host<input id="WEB_UI_HOST" value="127.0.0.1"></label>
<label>Web UI Port<input id="WEB_UI_PORT" value="8080"></label>
<label class="full">Web UI Token<input id="WEB_UI_TOKEN" placeholder="set a strong token"></label>
</div></div>
<div class="card"><h2>Telegram login session</h2><p class="muted">After saving Telegram credentials, create the Telegram session from the GUI so the forwarder does not ask for a code in the terminal.</p><div class="form"><label>Login code from Telegram<input id="TG_CODE" placeholder="12345"></label><label>2FA password, only if enabled<input id="TG_PASSWORD" type="password" placeholder="optional"></label></div><div class="actions" style="margin-top:12px"><button class="btn" onclick="sendCode()">Send Telegram Login Code</button><button class="btn green" onclick="signInTelegram()">Verify Code / Password</button></div></div>
<div class="grid"><div class="card"><h2>Where to find these</h2><ul>
<li><b>Telegram API ID/API Hash:</b> go to <a href="https://my.telegram.org" target="_blank">my.telegram.org</a> → API development tools → create an app.</li>
<li><b>Telegram phone:</b> your own Telegram account phone. The app logs in as your user account via MTProto.</li>
<li><b>Destination Channel ID:</b> add your user account to the destination channel, then after launch use the UI's Telegram dialog suggestions to find the channel ID. You can also forward a post to ID bots such as <code>@userinfobot</code>/<code>@getidsbot</code>.</li>
<li><b>Admin User ID:</b> your numeric Telegram user ID. Find it from <code>@userinfobot</code> or similar ID bots.</li>
<li><b>Discord Bot Token:</b> Discord Developer Portal → Application → Bot → Reset/Copy Token. Important: this only sees servers/channels where that bot is invited and permitted.</li>
</ul></div><div class="card"><h2>Important Discord limitation</h2><p>Discord does not provide a compliant API for automating your normal user account to save/forward media from servers you merely joined. The supported method is a Discord bot that is invited to the server/channel with read permissions.</p><p>I will not add self-bot/user-token automation or desktop scraping to bypass platform restrictions. For Telegram, the user-client flow can read channels your Telegram account joined, except protected/restricted media.</p><p>After the forwarder launches, open the control panel and use <b>Suggestions</b> under Sources to select Telegram dialogs and Discord bot-visible channels.</p></div></div>
<div class="card"><h2>Status</h2><pre id="status" class="muted">Loading...</pre></div>
</div><script>
const keys=['TELEGRAM_API_ID','TELEGRAM_API_HASH','TELEGRAM_PHONE','DESTINATION_CHANNEL_ID','DISCORD_TOKEN','ADMIN_USER_ID','WEB_UI_HOST','WEB_UI_PORT','WEB_UI_TOKEN'];
async function api(p,o={}){o.headers=Object.assign({'Content-Type':'application/json'},o.headers||{});const r=await fetch(p,o);const j=await r.json();if(!r.ok||j.ok===false)throw new Error(j.error||r.statusText);return j}
async function load(){const j=await api('/api/config');for(const k of keys){if(document.getElementById(k))document.getElementById(k).value=j.config[k]||''}status.textContent=JSON.stringify({env_saved:true,process_running:j.process_running},null,2)}
function formData(){const d={};for(const k of keys)d[k]=document.getElementById(k).value;return d}
async function save(){await api('/api/save',{method:'POST',body:JSON.stringify(formData())});alert('Saved .env')}
async function sendCode(){const j=await api('/api/telegram/send_code',{method:'POST',body:JSON.stringify(formData())});alert(j.already_authorized?'Telegram is already authorized':'Login code sent in Telegram')}
async function signInTelegram(){const j=await api('/api/telegram/sign_in',{method:'POST',body:JSON.stringify({code:TG_CODE.value,password:TG_PASSWORD.value})});if(j.needs_password)alert('This account has 2FA. Enter your password and click Verify again.');else alert('Telegram authorized. User ID: '+(j.user_id||''));}
async function launch(){await save();const j=await api('/api/launch',{method:'POST'});alert('Forwarder launched. Open its UI on WEB_UI_PORT, usually http://127.0.0.1:8080');setTimeout(load,500)}
async function stopApp(){const j=await api('/api/stop',{method:'POST'});alert(j.stopped?'Stopped':'Not running');load()}
load();setInterval(load,4000)
</script></body></html>
"""


def main() -> None:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/config", api_config)
    app.router.add_post("/api/save", api_save)
    app.router.add_post("/api/telegram/send_code", api_telegram_send_code)
    app.router.add_post("/api/telegram/sign_in", api_telegram_sign_in)
    app.router.add_post("/api/launch", api_launch)
    app.router.add_post("/api/stop", api_stop)
    web.run_app(app, host="127.0.0.1", port=8079)


if __name__ == "__main__":
    main()

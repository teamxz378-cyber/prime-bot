import json
import os
import time
import shutil
import secrets
import asyncio
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Header, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from collections import deque

app = FastAPI(title="PRIME Subscription API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_FILE = DATA_DIR / "subscribers.json"
MOD_DIR = DATA_DIR / "mod"
API_KEY = os.environ.get("API_KEY", "prime-secret-key-change-me")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin")
MOD_META_FILE = DATA_DIR / "mod_meta.json"
BUNDLED_MOD = Path(__file__).parent / "data" / "mod" / "PRIME-1.0.0-protected.jar"


def load_mod_meta() -> dict:
    if MOD_META_FILE.exists():
        return json.loads(MOD_META_FILE.read_text())
    return {"version": "1.0.0", "filename": "PRIME-1.0.0-protected.jar", "changelog": ""}


def save_mod_meta(meta: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MOD_META_FILE.write_text(json.dumps(meta, indent=2))


@app.on_event("startup")
def init_data():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MOD_DIR.mkdir(parents=True, exist_ok=True)
    if not MOD_META_FILE.exists():
        save_mod_meta({"version": "1.0.0", "filename": "PRIME-1.0.0-protected.jar", "changelog": ""})
    meta = load_mod_meta()
    dest = MOD_DIR / meta["filename"]
    if not dest.exists() and BUNDLED_MOD.exists():
        shutil.copy2(BUNDLED_MOD, dest)
        print(f"[INIT] Copied bundled mod to {dest}")


# ─── DB ───────────────────────────────────────────────────────
def load_db() -> dict:
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text())
    return {}


def save_db(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_FILE.write_text(json.dumps(data, indent=2))


def verify_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")


# ─── Models ───────────────────────────────────────────────────
class SubRequest(BaseModel):
    discord_id: str
    username: str = ""
    days: int = 30


class TokenClaimRequest(BaseModel):
    user_token: str
    hwid: str


class UnsubRequest(BaseModel):
    discord_id: str


class HwidRequest(BaseModel):
    discord_id: str
    hwid: str


class LogEntry(BaseModel):
    type: str  # chat, kill, coords, connect, disconnect, command, death, item
    player: str
    server: str = ""
    message: str = ""
    extra: dict = {}
    timestamp: float = 0


class SayCommand(BaseModel):
    account: str
    message: str


# In-memory queues for logs and commands
log_queue: deque = deque(maxlen=1000)
say_command_queue: deque = deque(maxlen=100)


# ─── LOGS API ─────────────────────────────────────────────────

@app.post("/api/logs")
async def receive_log(entry: LogEntry, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    if entry.timestamp == 0:
        entry.timestamp = time.time()
    log_queue.append(entry.model_dump())
    return {"ok": True}


@app.post("/api/logs/batch")
async def receive_logs_batch(entries: List[LogEntry], x_api_key: str = Header(None)):
    verify_key(x_api_key)
    now = time.time()
    for entry in entries:
        if entry.timestamp == 0:
            entry.timestamp = now
        log_queue.append(entry.model_dump())
    return {"ok": True, "count": len(entries)}


@app.get("/api/logs/poll")
async def poll_logs(x_api_key: str = Header(None)):
    verify_key(x_api_key)
    logs = list(log_queue)
    log_queue.clear()
    return {"logs": logs}


@app.post("/api/logs/say")
async def add_say_command(cmd: SayCommand, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    say_command_queue.append({"account": cmd.account, "message": cmd.message, "timestamp": time.time()})
    return {"ok": True}


@app.get("/api/logs/say/poll")
async def poll_say_commands(x_api_key: str = Header(None)):
    verify_key(x_api_key)
    cmds = list(say_command_queue)
    say_command_queue.clear()
    return {"commands": cmds}


# ─── API ──────────────────────────────────────────────────────
@app.get("/")
def root():
    return RedirectResponse("/panel")


@app.get("/api/mod/version")
def mod_version():
    """Returns current mod version and filename."""
    meta = load_mod_meta()
    return {"version": meta["version"], "filename": meta["filename"], "changelog": meta.get("changelog", "")}


@app.get("/api/mod/download/{hwid}")
def mod_download(hwid: str):
    """Download the mod jar. Requires valid HWID with active subscription."""
    db = load_db()
    now = time.time()
    found = False
    for did, info in db.items():
        if info.get("hwid", "") == hwid:
            expires = info.get("expires", 0)
            if expires > 0 and now > expires:
                raise HTTPException(status_code=403, detail="subscription_expired")
            found = True
            break
    if not found:
        raise HTTPException(status_code=403, detail="hwid_not_found")
    meta = load_mod_meta()
    mod_path = MOD_DIR / meta["filename"]
    if not mod_path.exists():
        raise HTTPException(status_code=404, detail="mod_file_not_found")
    return FileResponse(mod_path, filename=meta["filename"], media_type="application/java-archive")


@app.post("/api/mod/upload")
async def mod_upload(
    file: UploadFile = File(...),
    version: str = Form(""),
    changelog: str = Form(""),
    x_api_key: str = Header(None),
):
    """Upload a new mod .jar. Requires API key."""
    verify_key(x_api_key)
    if not file.filename.endswith(".jar"):
        raise HTTPException(status_code=400, detail="File must be a .jar")
    content = await file.read()
    if len(content) < 1000:
        raise HTTPException(status_code=400, detail="File too small")
    meta = load_mod_meta()
    old_file = MOD_DIR / meta["filename"]
    if old_file.exists():
        old_file.unlink()
    filename = file.filename
    new_version = version if version else meta["version"]
    dest = MOD_DIR / filename
    dest.write_bytes(content)
    new_meta = {
        "version": new_version,
        "filename": filename,
        "changelog": changelog,
        "updated_at": time.time(),
    }
    save_mod_meta(new_meta)
    return {"ok": True, "version": new_version, "filename": filename, "size": len(content)}


@app.get("/api/check/hwid/{hwid}")
def check_sub_by_hwid(hwid: str):
    """Check subscription by HWID — finds which Discord ID owns this HWID."""
    db = load_db()
    now = time.time()
    for did, info in db.items():
        if info.get("hwid", "") == hwid:
            expires = info.get("expires", 0)
            if expires > 0 and now > expires:
                return {
                    "subscribed": False,
                    "discord_id": did,
                    "hwid": hwid,
                    "expired": True,
                    "username": info.get("username", ""),
                }
            return {
                "subscribed": True,
                "discord_id": did,
                "hwid": hwid,
                "username": info.get("username", ""),
                "expires": expires,
            }
    return {"subscribed": False, "hwid": hwid, "not_found": True}


@app.get("/api/check/{discord_id}")
def check_sub(discord_id: str, hwid: str = ""):
    db = load_db()
    sub = db.get(discord_id)
    if not sub:
        return {"subscribed": False, "discord_id": discord_id}
    expires = sub.get("expires", 0)
    if expires > 0 and time.time() > expires:
        return {"subscribed": False, "discord_id": discord_id, "expired": True}
    stored_hwid = sub.get("hwid", "")
    if hwid and stored_hwid and hwid != stored_hwid:
        return {"subscribed": False, "discord_id": discord_id, "hwid_mismatch": True}
    if hwid and not stored_hwid:
        sub["hwid"] = hwid
        db[discord_id] = sub
        save_db(db)
    
    # Double check if HWID was updated but not saved in some edge case
    if hwid and stored_hwid and hwid != stored_hwid:
         return {"subscribed": False, "discord_id": discord_id, "hwid_mismatch": True}

    return {
        "subscribed": True,
        "discord_id": discord_id,
        "username": sub.get("username", ""),
        "expires": expires,
        "hwid": sub.get("hwid", "")
    }


@app.post("/api/hwid/auto-claim")
def auto_claim_hwid(hwid: str = Form(...)):
    """Auto-claim: find a subscription with empty HWID and bind this HWID to it.
    Used by the launcher so users don't need to copy HWID manually."""
    db = load_db()
    now = time.time()
    # Check if HWID already bound
    for did, info in db.items():
        if info.get("hwid", "") == hwid:
            expires = info.get("expires", 0)
            if expires > 0 and now > expires:
                return {"ok": False, "error": "expired", "discord_id": did}
            return {"ok": True, "already_bound": True, "discord_id": did}
    # Find first subscription with empty HWID that is still active
    for did, info in db.items():
        if info.get("hwid", "") == "":
            expires = info.get("expires", 0)
            if expires > 0 and now > expires:
                continue  # skip expired
            # Bind HWID to this subscription
            info["hwid"] = hwid
            db[did] = info
            save_db(db)
            return {
                "ok": True,
                "claimed": True,
                "discord_id": did,
                "username": info.get("username", ""),
            }
    return {"ok": False, "error": "no_free_subscription"}


@app.get("/api/hwid/auto-claim/{hwid}")
def auto_claim_hwid_get(hwid: str):
    """GET version of auto-claim for launcher convenience."""
    db = load_db()
    now = time.time()
    for did, info in db.items():
        if info.get("hwid", "") == hwid:
            expires = info.get("expires", 0)
            if expires > 0 and now > expires:
                return {"ok": False, "error": "expired", "discord_id": did}
            return {"ok": True, "already_bound": True, "discord_id": did}
    for did, info in db.items():
        if info.get("hwid", "") == "":
            expires = info.get("expires", 0)
            if expires > 0 and now > expires:
                continue
            info["hwid"] = hwid
            db[did] = info
            save_db(db)
            return {
                "ok": True,
                "claimed": True,
                "discord_id": did,
                "username": info.get("username", ""),
            }
    return {"ok": False, "error": "no_free_subscription"}


@app.post("/api/hwid/claim-by-token")
def claim_hwid_by_token(req: TokenClaimRequest):
    """Launcher sends user_token + HWID. API finds the subscription by token and binds HWID."""
    db = load_db()
    now = time.time()
    # Check if HWID already bound
    for did, info in db.items():
        if info.get("hwid", "") == req.hwid:
            expires = info.get("expires", 0)
            if expires > 0 and now > expires:
                return {"ok": False, "error": "expired", "discord_id": did}
            return {"ok": True, "already_bound": True, "discord_id": did,
                    "username": info.get("username", "")}
    # Find subscription by user_token
    for did, info in db.items():
        if info.get("user_token", "") == req.user_token:
            expires = info.get("expires", 0)
            if expires > 0 and now > expires:
                return {"ok": False, "error": "expired", "discord_id": did}
            info["hwid"] = req.hwid
            db[did] = info
            save_db(db)
            return {"ok": True, "claimed": True, "discord_id": did,
                    "username": info.get("username", "")}
    return {"ok": False, "error": "token_not_found"}


@app.get("/api/hwid/claim-by-token/{user_token}/{hwid}")
def claim_hwid_by_token_get(user_token: str, hwid: str):
    """GET version for launcher convenience."""
    db = load_db()
    now = time.time()
    for did, info in db.items():
        if info.get("hwid", "") == hwid:
            expires = info.get("expires", 0)
            if expires > 0 and now > expires:
                return {"ok": False, "error": "expired", "discord_id": did}
            return {"ok": True, "already_bound": True, "discord_id": did,
                    "username": info.get("username", "")}
    for did, info in db.items():
        if info.get("user_token", "") == user_token:
            expires = info.get("expires", 0)
            if expires > 0 and now > expires:
                return {"ok": False, "error": "expired", "discord_id": did}
            info["hwid"] = hwid
            db[did] = info
            save_db(db)
            return {"ok": True, "claimed": True, "discord_id": did,
                    "username": info.get("username", "")}
    return {"ok": False, "error": "token_not_found"}


@app.post("/api/hwid/bind")
def bind_hwid(req: HwidRequest, x_api_key: str = Header(None)):
    """Bind HWID to a Discord ID (used by bot when approving HWID)."""
    verify_key(x_api_key)
    db = load_db()
    if req.discord_id not in db:
        return {"ok": False, "error": "subscription not found"}
    # Check if HWID already bound to someone else
    for did, info in db.items():
        if did != req.discord_id and info.get("hwid", "") == req.hwid:
            return {"ok": False, "error": "hwid_already_bound", "bound_to": did}
    db[req.discord_id]["hwid"] = req.hwid
    save_db(db)
    return {"ok": True, "discord_id": req.discord_id, "hwid": req.hwid}


@app.post("/api/subscribe")
def subscribe(req: SubRequest, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    db = load_db()
    now = time.time()
    expires = now + req.days * 86400 if req.days > 0 else 0
    existing = db.get(req.discord_id)
    if existing and existing.get("expires", 0) > now:
        expires = existing["expires"] + req.days * 86400
    user_token = existing.get("user_token", "") if existing else ""
    if not user_token:
        user_token = secrets.token_hex(16)
    db[req.discord_id] = {
        "username": req.username,
        "added": existing.get("added", now) if existing else now,
        "expires": expires,
        "days": req.days,
        "hwid": existing.get("hwid", "") if existing else "",
        "user_token": user_token,
    }
    save_db(db)
    return {"ok": True, "discord_id": req.discord_id, "expires": expires, "user_token": user_token}


@app.post("/api/unsubscribe")
def unsubscribe(req: UnsubRequest, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    db = load_db()
    if req.discord_id in db:
        del db[req.discord_id]
        save_db(db)
        return {"ok": True, "removed": True}
    return {"ok": True, "removed": False}


@app.post("/api/hwid/reset")
def reset_hwid(req: UnsubRequest, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    db = load_db()
    if req.discord_id in db:
        db[req.discord_id]["hwid"] = ""
        save_db(db)
        return {"ok": True, "reset": True}
    return {"ok": False, "error": "not found"}


@app.get("/api/subscribers")
def list_subs(x_api_key: str = Header(None)):
    verify_key(x_api_key)
    db = load_db()
    now = time.time()
    result = []
    for did, info in db.items():
        exp = info.get("expires", 0)
        active = exp == 0 or exp > now
        result.append({
            "discord_id": did,
            "username": info.get("username", ""),
            "expires": exp,
            "active": active,
            "hwid": info.get("hwid", ""),
        })
    return {"subscribers": result, "total": len(result)}


# ─── Admin Panel ──────────────────────────────────────────────
PANEL_CSS = """
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0c0a10;color:#e0dce8;font-family:'Segoe UI',sans-serif;min-height:100vh}
.wrap{max-width:960px;margin:0 auto;padding:24px}
h1{color:#a94cef;font-size:28px;margin-bottom:8px}
h2{color:#c794ed;font-size:18px;margin:24px 0 12px}
.subtitle{color:#8a7e94;font-size:13px;margin-bottom:24px}
.card{background:#16101e;border:1px solid #352a3d;border-radius:12px;padding:20px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;margin-top:12px}
th{text-align:left;color:#8a7e94;font-size:12px;text-transform:uppercase;padding:8px 12px;border-bottom:1px solid #352a3d}
td{padding:10px 12px;border-bottom:1px solid #1e1628;font-size:14px}
tr:hover{background:#1a1226}
.active{color:#50dc8c;font-weight:600}
.expired{color:#ff5555;font-weight:600}
.hwid{color:#8a7e94;font-size:11px;font-family:monospace;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
input,select{background:#1e1628;border:1px solid #352a3d;color:#e0dce8;padding:8px 14px;border-radius:8px;font-size:14px;outline:none}
input:focus{border-color:#a94cef}
.btn{background:#a94cef;color:#fff;border:none;padding:10px 20px;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600}
.btn:hover{background:#bf6ff7}
.btn-sm{padding:4px 12px;font-size:12px;border-radius:6px}
.btn-red{background:#cc3355}
.btn-red:hover{background:#ee4466}
.btn-yellow{background:#b8860b}
.btn-yellow:hover{background:#daa520}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.stat{display:inline-block;background:#1e1628;border:1px solid #352a3d;border-radius:8px;padding:12px 20px;margin-right:10px;margin-bottom:8px}
.stat b{color:#a94cef;font-size:22px;display:block}
.stat span{color:#8a7e94;font-size:12px}
.login-box{max-width:360px;margin:120px auto;text-align:center}
.login-box h1{margin-bottom:20px}
.login-box input{width:100%;margin-bottom:12px}
.login-box .btn{width:100%}
.msg{background:#1a3a1a;border:1px solid #2a5a2a;color:#50dc8c;padding:10px;border-radius:8px;margin-bottom:16px}
.msg-err{background:#3a1a1a;border-color:#5a2a2a;color:#ff5555}
</style>
"""

def get_session(request: Request):
    return request.cookies.get("session", "")


@app.get("/panel/login", response_class=HTMLResponse)
def login_page(msg: str = ""):
    msg_html = f'<div class="msg msg-err">{msg}</div>' if msg else ""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>PRIME Admin</title>{PANEL_CSS}</head>
<body><div class="login-box"><h1>🔒 PRIME Admin</h1>{msg_html}
<form method="post" action="/panel/login">
<input name="password" type="password" placeholder="Пароль администратора">
<button class="btn" type="submit">Войти</button>
</form></div></body></html>"""


@app.post("/panel/login")
def login_post(password: str = Form("")):
    if password == ADMIN_PASS:
        token = secrets.token_hex(32)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "session.txt").write_text(token)
        resp = RedirectResponse("/panel", status_code=302)
        resp.set_cookie("session", token, max_age=86400)
        return resp
    return RedirectResponse("/panel/login?msg=Неверный+пароль", status_code=302)


def check_auth(request: Request):
    token = get_session(request)
    session_file = DATA_DIR / "session.txt"
    if not session_file.exists() or not token:
        return False
    return session_file.read_text().strip() == token


@app.get("/panel", response_class=HTMLResponse)
def panel(request: Request, msg: str = ""):
    if not check_auth(request):
        return RedirectResponse("/panel/login")
    db = load_db()
    now = time.time()
    total = len(db)
    active = sum(1 for v in db.values() if v.get("expires", 0) == 0 or v.get("expires", 0) > now)
    expired = total - active
    with_hwid = sum(1 for v in db.values() if v.get("hwid"))

    rows = ""
    for did, info in sorted(db.items(), key=lambda x: x[1].get("added", 0), reverse=True):
        exp = info.get("expires", 0)
        is_active = exp == 0 or exp > now
        status = '<span class="active">Активна</span>' if is_active else '<span class="expired">Истекла</span>'
        exp_str = time.strftime("%d.%m.%Y %H:%M", time.gmtime(exp)) if exp > 0 else "Бессрочно"
        hwid = info.get("hwid", "") or "—"
        hwid_short = hwid[:16] + "..." if len(hwid) > 16 else hwid
        added = time.strftime("%d.%m.%Y", time.gmtime(info.get("added", 0)))
        rows += f"""<tr>
<td>{info.get('username','—')}</td>
<td style="font-family:monospace;font-size:12px">{did}</td>
<td>{status}</td><td>{exp_str}</td>
<td class="hwid" title="{hwid}">{hwid_short}</td>
<td>{added}</td>
<td>
<form method="post" action="/panel/action" style="display:inline">
<input type="hidden" name="discord_id" value="{did}">
<input type="hidden" name="action" value="hwid_reset">
<button class="btn btn-sm btn-yellow" type="submit">Сброс HWID</button>
</form>
<form method="post" action="/panel/action" style="display:inline;margin-left:4px">
<input type="hidden" name="discord_id" value="{did}">
<input type="hidden" name="action" value="delete">
<button class="btn btn-sm btn-red" type="submit">Удалить</button>
</form>
</td></tr>"""

    msg_html = f'<div class="msg">{msg}</div>' if msg else ""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>PRIME Admin Panel</title>{PANEL_CSS}</head>
<body><div class="wrap">
<h1>⚡ PRIME Admin Panel</h1>
<p class="subtitle">Управление подписками и айдишниками</p>
{msg_html}
<div>
<div class="stat"><b>{total}</b><span>Всего</span></div>
<div class="stat"><b>{active}</b><span>Активных</span></div>
<div class="stat"><b>{expired}</b><span>Истекших</span></div>
<div class="stat"><b>{with_hwid}</b><span>С HWID</span></div>
</div>

<div class="card">
<h2>➕ Добавить подписку</h2>
<form method="post" action="/panel/action">
<input type="hidden" name="action" value="add">
<div class="row">
<input name="discord_id" placeholder="Discord ID" required style="width:200px">
<input name="username" placeholder="Имя (опционально)" style="width:160px">
<input name="days" type="number" value="30" min="0" style="width:100px" placeholder="Дней">
<button class="btn" type="submit">Добавить</button>
</div>
</form>
</div>

<div class="card">
<h2>📋 Подписчики ({total})</h2>
<table>
<tr><th>Имя</th><th>Discord ID</th><th>Статус</th><th>Истекает</th><th>HWID</th><th>Добавлен</th><th>Действия</th></tr>
{rows}
</table>
</div>

<div class="card" style="margin-top:24px">
<h2>🔑 API Info</h2>
<p style="color:#8a7e94;font-size:13px">API Key: <code style="color:#a94cef">{API_KEY[:8]}...{API_KEY[-4:]}</code></p>
<p style="color:#8a7e94;font-size:13px;margin-top:4px">Эндпоинты: GET /api/check/hwid/{{hwid}}, GET /api/check/{{discord_id}}, POST /api/subscribe, POST /api/unsubscribe, POST /api/hwid/reset, POST /api/hwid/bind</p>
</div>

</div></body></html>"""


@app.post("/panel/action")
def panel_action(request: Request, action: str = Form(""), discord_id: str = Form(""),
                 username: str = Form(""), days: int = Form(30)):
    if not check_auth(request):
        return RedirectResponse("/panel/login")
    db = load_db()
    msg = ""
    if action == "add" and discord_id:
        now = time.time()
        expires = now + days * 86400 if days > 0 else 0
        existing = db.get(discord_id)
        if existing and existing.get("expires", 0) > now:
            expires = existing["expires"] + days * 86400
        db[discord_id] = {
            "username": username,
            "added": existing.get("added", now) if existing else now,
            "expires": expires,
            "days": days,
            "hwid": existing.get("hwid", "") if existing else "",
        }
        save_db(db)
        msg = f"✔ Подписка добавлена: {discord_id}"
    elif action == "delete" and discord_id:
        if discord_id in db:
            del db[discord_id]
            save_db(db)
        msg = f"✔ Удалён: {discord_id}"
    elif action == "hwid_reset" and discord_id:
        if discord_id in db:
            db[discord_id]["hwid"] = ""
            save_db(db)
        msg = f"✔ HWID сброшен: {discord_id}"
    return RedirectResponse(f"/panel?msg={msg}", status_code=302)

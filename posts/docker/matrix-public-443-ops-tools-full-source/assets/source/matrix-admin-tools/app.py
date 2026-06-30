#!/usr/bin/env python3
import datetime as dt
import base64
import cgi
import html
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_TITLE = "Matrix 运维面板"
BASE_URL = os.environ.get("SYNAPSE_URL", "http://127.0.0.1:8008")
TOKEN_FILE = os.environ.get("ADMIN_TOKEN_FILE", "/run/secrets/synapse-admin-access-token")
DB_PATH = os.environ.get("DB_PATH", "/data/app.db")
LOG_PATH = os.environ.get("LOG_PATH", "/logs/app.log")
AUTH_USER_FILE = os.environ.get("AUTH_USER_FILE", "")
AUTH_USER = os.environ.get("AUTH_USER", "")
AUTH_PASSWORD_FILE = os.environ.get("AUTH_PASSWORD_FILE", "")
TELEGRAM_BOT_TOKEN_FILE = os.environ.get("TELEGRAM_BOT_TOKEN_FILE", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
MAX_LOG_BYTES = int(os.environ.get("MAX_LOG_BYTES", str(1024 * 1024)))
MAX_LOG_FILES = int(os.environ.get("MAX_LOG_FILES", "3"))
MAX_JOBS = int(os.environ.get("MAX_JOBS", "200"))
MAX_JOB_OUTPUT = int(os.environ.get("MAX_JOB_OUTPUT", "12000"))
SERVER_NAME = os.environ.get("SERVER_NAME", "example.com")
PURGE_SCRIPT = os.environ.get("PURGE_SCRIPT", "/opt/matrix-tools/purge-staged-media.sh")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8190"))
LEGACY_STICKER_DB_PATH = os.environ.get(
    "LEGACY_STICKER_DB_PATH",
    "",
)
STICKER_WIDGET_PUBLIC_URL = os.environ.get(
    "STICKER_WIDGET_PUBLIC_URL", "https://stickers.example.com/stickerpicker/"
)
STICKER_WEB_ROOT = os.environ.get("STICKER_WEB_ROOT", "/opt/matrix-tools/stickerpicker-web")
STICKER_ROUTE_PREFIX = os.environ.get("STICKER_ROUTE_PREFIX", "/stickerpicker").rstrip("/")
STICKER_PACKS_DIR = os.path.join(STICKER_WEB_ROOT, "packs")
STICKER_PACK_MEDIA_DIR = os.path.join(STICKER_PACKS_DIR, "media")
STICKER_SEND_MAX_DIMENSION = int(os.environ.get("STICKER_SEND_MAX_DIMENSION", "240"))
SYNAPSE_DB_HOST = os.environ.get("SYNAPSE_DB_HOST", "127.0.0.1")
SYNAPSE_DB_PORT = os.environ.get("SYNAPSE_DB_PORT", "5432")
SYNAPSE_DB_USER = os.environ.get("SYNAPSE_DB_USER", "synapse_user")
SYNAPSE_DB_PASSWORD = os.environ.get("SYNAPSE_DB_PASSWORD", "")
SYNAPSE_DB_NAME = os.environ.get("SYNAPSE_DB_NAME", "synapse")
SYNAPSE_CONTAINER = os.environ.get("SYNAPSE_CONTAINER", "matrix-synapse")
STICKER_AUTO_SYNC_SECONDS = int(os.environ.get("STICKER_AUTO_SYNC_SECONDS", "300"))
# 贴纸下发面向真实用户账号；默认排除常见服务号，避免新系统落地时把运维号/机器人也一起注入。
# 如果你还有历史迁移账号，可以通过环境变量额外补充排除名单。
STICKER_SYNC_EXCLUDE_LOCALPARTS = {
    item.strip().lower()
    for item in os.environ.get(
        "STICKER_SYNC_EXCLUDE_LOCALPARTS",
        "bot,musicbot,media_purge_admin",
    ).split(",")
    if item.strip()
}

# 三层附件清理策略：
#   不清理区：<= medium_size_gt 的小文件长期保留
#   常规清理：medium_size_gt < 文件大小 < large_size_gt，保留 medium_days 天后删除
#   紧急清理：>= large_size_gt 的文件仅保留 large_days 天
# 通过两层 shell purge_local_media 调用的先后次序和参数差异实现三层语义。
DEFAULT_PURGE = {
    "small_keep_size": 3 * 1024 * 1024,
    "medium_days": 7,
    "medium_size_gt": 3 * 1024 * 1024,
    "large_days": 3,
    "large_size_gt": 15 * 1024 * 1024,
    "remote_days": 7,
    "keep_profiles": "true",
    "schedule_hhmm": "0423",
}

DEFAULT_NOTICE_BODY = ""
NOTICE_EXCLUDE_HINTS = (
    "server",
    "bot",
    "music",
    "notice",
    "media",
    "purge",
    "monitor",
)

DEFAULT_STICKER_CONFIG = {
    "sticker_delivery_mode": "all_users",
    "sticker_target_users": "",
    # 真实用户列表只作为下发辅助数据，平时缓存一份，避免 Synapse 管理接口短暂抖动时把页面和同步流程打断。
    "sticker_user_cache": "",
    "sticker_user_cache_updated_at": "",
}

CONFIG_LOCK = threading.Lock()
STICKER_SYNC_LOCK = threading.Lock()


def ensure_dirs():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    os.makedirs(STICKER_PACKS_DIR, exist_ok=True)
    os.makedirs(STICKER_PACK_MEDIA_DIR, exist_ok=True)


def sticker_asset_version():
    candidates = [
        os.path.join(STICKER_WEB_ROOT, "index.html"),
        os.path.join(STICKER_WEB_ROOT, "src", "index.js"),
        os.path.join(STICKER_WEB_ROOT, "src", "widget-api.js"),
        os.path.join(STICKER_WEB_ROOT, "style", "index.css"),
        os.path.join(STICKER_WEB_ROOT, "packs", "index.json"),
    ]
    latest = 0
    for path in candidates:
        try:
            latest = max(latest, int(os.path.getmtime(path)))
        except OSError:
            continue
    return str(latest or int(time.time()))


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma foreign_keys=on")
    conn.execute(
        "create table if not exists config (key text primary key, value text not null)"
    )
    conn.execute(
        """create table if not exists jobs (
            id text primary key,
            kind text not null,
            status text not null,
            created_at text not null,
            finished_at text,
            summary text,
            output text
        )"""
    )
    conn.execute(
        """create table if not exists sticker_packs (
            id integer primary key autoincrement,
            slug text not null unique,
            title text not null,
            description text not null,
            is_enabled integer not null default 1,
            is_public integer not null default 1,
            avatar_sticker_id integer,
            author_name text,
            author_reference text,
            license text,
            source_type text,
            source_ref text,
            created_at text not null,
            updated_at text not null,
            foreign key(avatar_sticker_id) references stickers(id) on delete set null
        )"""
    )
    conn.execute(
        """create table if not exists stickers (
            id integer primary key autoincrement,
            pack_id integer not null,
            name text not null,
            body text not null,
            image_mxc text not null,
            preview_image_mxc text,
            preview_image_mimetype text,
            send_image_mxc text,
            send_image_mimetype text,
            source_ref text,
            mimetype text not null,
            width integer not null,
            height integer not null,
            size_bytes integer not null default 0,
            created_at text not null,
            updated_at text not null,
            foreign key(pack_id) references sticker_packs(id) on delete cascade
        )"""
    )
    columns = {row["name"] for row in conn.execute("pragma table_info(stickers)").fetchall()}
    pack_columns = {row["name"] for row in conn.execute("pragma table_info(sticker_packs)").fetchall()}
    # 旧版本 app.db 没有视频贴纸缩略图字段，这里按需补列，避免新老环境切换时要求手工迁库。
    if "source_type" not in pack_columns:
        conn.execute("alter table sticker_packs add column source_type text")
    if "source_ref" not in pack_columns:
        conn.execute("alter table sticker_packs add column source_ref text")
    if "preview_image_mxc" not in columns:
        conn.execute("alter table stickers add column preview_image_mxc text")
    if "preview_image_mimetype" not in columns:
        conn.execute("alter table stickers add column preview_image_mimetype text")
    if "send_image_mxc" not in columns:
        conn.execute("alter table stickers add column send_image_mxc text")
    if "send_image_mimetype" not in columns:
        conn.execute("alter table stickers add column send_image_mimetype text")
    if "source_ref" not in columns:
        conn.execute("alter table stickers add column source_ref text")
    conn.commit()
    return conn


def now_iso():
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def append_log(message):
    rotate_log_if_needed()
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{now_iso()}] {message}\n")


def rotate_log_if_needed():
    try:
        if os.path.getsize(LOG_PATH) < MAX_LOG_BYTES:
            return
    except FileNotFoundError:
        return
    for index in range(MAX_LOG_FILES - 1, 0, -1):
        src = f"{LOG_PATH}.{index}"
        dst = f"{LOG_PATH}.{index + 1}"
        if os.path.exists(src):
            if index + 1 > MAX_LOG_FILES:
                os.remove(src)
            else:
                os.replace(src, dst)
    os.replace(LOG_PATH, f"{LOG_PATH}.1")


def prune_jobs(conn):
    # 任务记录只留最近一段，避免面板日志型数据库无限增长。
    conn.execute(
        "delete from jobs where id not in (select id from jobs order by created_at desc limit ?)",
        (MAX_JOBS,),
    )


def get_config():
    with CONFIG_LOCK:
        conn = db()
        rows = conn.execute("select key, value from config").fetchall()
        conn.close()
    values = dict(DEFAULT_PURGE)
    values.update(DEFAULT_STICKER_CONFIG)
    values.update({row["key"]: row["value"] for row in rows})
    return values


def save_config(values):
    with CONFIG_LOCK:
        conn = db()
        for key, value in values.items():
            conn.execute(
                "insert into config(key, value) values(?, ?) "
                "on conflict(key) do update set value=excluded.value",
                (key, str(value)),
            )
        conn.commit()
        conn.close()


def cached_sticker_user_list():
    cfg = get_config()
    return split_mxids(cfg.get("sticker_user_cache", ""))


def save_sticker_user_cache(users):
    save_config(
        {
            "sticker_user_cache": "\n".join(users),
            "sticker_user_cache_updated_at": now_iso(),
        }
    )


def create_job(kind):
    job_id = str(uuid.uuid4())
    conn = db()
    conn.execute(
        "insert into jobs(id, kind, status, created_at) values(?, ?, ?, ?)",
        (job_id, kind, "running", now_iso()),
    )
    conn.commit()
    conn.close()
    return job_id


def finish_job(job_id, status, summary, output):
    conn = db()
    conn.execute(
        "update jobs set status=?, finished_at=?, summary=?, output=? where id=?",
        (status, now_iso(), summary, output[-MAX_JOB_OUTPUT:], job_id),
    )
    prune_jobs(conn)
    conn.commit()
    conn.close()
    append_log(f"{status} {job_id} {summary}")


def recent_jobs(limit=12):
    conn = db()
    rows = conn.execute(
        "select * from jobs order by created_at desc limit ?", (limit,)
    ).fetchall()
    conn.close()
    return rows


def token():
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def read_secret_file(path):
    if not path:
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def auth_credentials():
    user = AUTH_USER or read_secret_file(AUTH_USER_FILE)
    password = read_secret_file(AUTH_PASSWORD_FILE)
    return user, password


def check_basic_auth(header):
    user, password = auth_credentials()
    if not user and not password:
        return True
    if not user or not password:
        return False
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except Exception:
        return False
    provided_user, separator, provided_password = decoded.partition(":")
    return bool(separator) and provided_user == user and provided_password == password


def request_json(method, url, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {token()}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw}
        return e.code, parsed


def request_json_noauth(method, url, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw}
        return e.code, parsed


def request_bytes(method, url, data, content_type):
    headers = {
        "Authorization": f"Bearer {token()}",
        "Content-Type": content_type,
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw}
        return e.code, parsed


def request_binary_noauth(url):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.headers.get("Content-Type", "application/octet-stream"), resp.read()


def normalize_slug(text):
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")
    return slug or "pack"


def telegram_bot_token():
    value = TELEGRAM_BOT_TOKEN.strip()
    if value:
        return value
    return read_secret_file(TELEGRAM_BOT_TOKEN_FILE)


def detect_binary_mimetype(filename, data, fallback="application/octet-stream"):
    header = bytes(data[:64] if data else b"")
    safe_name = str(filename or "").lower()
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"RIFF") and b"WEBP" in header[8:16]:
        return "image/webp"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "video/mp4"
    guessed = mimetypes.guess_type(safe_name)[0]
    if guessed:
        return guessed
    return fallback or "application/octet-stream"


def parse_mxc(mxc):
    if not str(mxc).startswith("mxc://"):
        raise ValueError(f"无效的 MXC 地址：{mxc}")
    server_and_media = str(mxc)[6:]
    if "/" not in server_and_media:
        raise ValueError(f"无效的 MXC 地址：{mxc}")
    server_name, media_id = server_and_media.split("/", 1)
    return server_name, media_id


def sticker_config():
    cfg = get_config()
    return {
        "delivery_mode": cfg.get("sticker_delivery_mode", "all_users"),
        "target_users": split_mxids(cfg.get("sticker_target_users", "")),
    }


def sticker_widget_url():
    widget_url = STICKER_WIDGET_PUBLIC_URL.rstrip("/") + "/"
    version = sticker_asset_version()
    if "theme=" not in widget_url:
        joiner = "&" if "?" in widget_url else "?"
        widget_url = f"{widget_url}{joiner}theme=$theme"
    if "v=" not in widget_url:
        joiner = "&" if "?" in widget_url else "?"
        # Element 桌面端会强缓存 widget 页面和其静态资源，这里给入口地址加版本戳，强制它拉新页面。
        widget_url = f"{widget_url}{joiner}v={version}"
    return widget_url


def widget_account_data(sender):
    # 这里按 maunium/stickerpicker 官方账号级 m.widgets 结构下发，
    # sender 和 creatorUserId 都写成目标用户自己，Element Web/Desktop/Android 的兼容性更稳定。
    # 这里只保留贴纸组件本身，不再沿用旧集成管理器的语义。
    widget_id = "stickerpicker"
    return {
        widget_id: {
            "type": "m.widget",
            "id": widget_id,
            "sender": sender,
            "state_key": widget_id,
            "content": {
                "url": sticker_widget_url(),
                "data": {},
                "name": "Stickerpicker",
                "type": "m.stickerpicker",
                "creatorUserId": sender,
            },
        }
    }


def is_legacy_sticker_widget(widget_id, widget):
    content = widget.get("content") or {}
    widget_type = content.get("type")
    widget_url = content.get("url") or ""
    # 只保留当前统一下发的 stickerpicker 键；其它贴纸 widget 都视为历史入口，避免 Element 客户端优先打开旧 Scalar/Dimension 地址。
    if widget_id == "stickerpicker":
        return False
    return widget_type == "m.stickerpicker" or "stickers.html" in widget_url or "stickerpicker" in widget_url


def list_sticker_packs():
    conn = db()
    rows = conn.execute(
        """select p.*,
                  count(s.id) as sticker_count,
                  sum(case when s.mimetype like 'video/%' or s.mimetype in ('image/gif', 'image/webp')
                           then 1 else 0 end) as animated_count
           from sticker_packs p
           left join stickers s on s.pack_id = p.id
           group by p.id
           order by p.is_enabled desc, p.updated_at desc, p.id desc"""
    ).fetchall()
    packs = []
    for row in rows:
        stickers = conn.execute(
            "select * from stickers where pack_id=? order by id asc",
            (row["id"],),
        ).fetchall()
        packs.append({"pack": row, "stickers": stickers})
    conn.close()
    return packs


def list_public_sticker_packs():
    conn = db()
    rows = conn.execute(
        """select p.* from sticker_packs p
           where p.is_enabled=1 and p.is_public=1
           order by p.updated_at desc, p.id desc"""
    ).fetchall()
    result = []
    for row in rows:
        stickers = conn.execute(
            "select * from stickers where pack_id=? order by id asc",
            (row["id"],),
        ).fetchall()
        if stickers:
            result.append({"pack": row, "stickers": stickers})
    conn.close()
    return result


def ensure_unique_pack_slug(conn, title, current_pack_id=None):
    base = normalize_slug(title)
    slug = base
    index = 2
    while True:
        row = conn.execute("select id from sticker_packs where slug=?", (slug,)).fetchone()
        if not row or (current_pack_id and int(row["id"]) == int(current_pack_id)):
            return slug
        slug = f"{base}-{index}"
        index += 1


def normalize_pack_form(form):
    title = form.get("name", [""])[0].strip()
    if not title:
        raise ValueError("贴纸包名称不能为空")
    return {
        "title": title,
        "description": form.get("description", [""])[0].strip() or title,
        "is_enabled": 1 if form.get("isEnabled", ["1"])[0] == "1" else 0,
        "is_public": 1 if form.get("isPublic", ["1"])[0] == "1" else 0,
        "author_name": form.get("authorName", [""])[0].strip() or None,
        "author_reference": form.get("authorReference", [""])[0].strip() or None,
        "license": form.get("license", ["Custom"])[0].strip() or "Custom",
    }


def normalize_telegram_pack_ref(raw):
    value = str(raw or "").strip()
    if not value:
        raise ValueError("Telegram 贴纸包链接或短名不能为空")
    parsed = urllib.parse.urlsplit(value)
    candidate = value
    if parsed.scheme or parsed.netloc:
        path = parsed.path.strip("/")
        if path.startswith("addstickers/"):
            candidate = path.split("/", 1)[1]
        elif path.startswith("addemoji/"):
            candidate = path.split("/", 1)[1]
        elif path:
            candidate = path.split("/")[-1]
        else:
            raise ValueError("无法从 Telegram 链接里识别贴纸包短名")
    candidate = candidate.strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_]{3,128}", candidate):
        raise ValueError("Telegram 贴纸包短名格式无效")
    return candidate


def telegram_api_url(method_name, **params):
    token_value = telegram_bot_token()
    if not token_value:
        raise ValueError("Telegram Bot Token 未配置，无法导入 Telegram 贴纸包")
    query = urllib.parse.urlencode(params)
    base = TELEGRAM_API_BASE.rstrip("/")
    url = f"{base}/bot{token_value}/{method_name}"
    if query:
        url = f"{url}?{query}"
    return url


def telegram_file_url(file_path):
    token_value = telegram_bot_token()
    if not token_value:
        raise ValueError("Telegram Bot Token 未配置，无法下载 Telegram 贴纸文件")
    safe_path = "/".join(urllib.parse.quote(part, safe="") for part in str(file_path).split("/"))
    return f"{TELEGRAM_API_BASE.rstrip('/')}/file/bot{token_value}/{safe_path}"


def telegram_api_call(method_name, **params):
    status, payload = request_json_noauth("GET", telegram_api_url(method_name, **params))
    if not 200 <= status < 300:
        raise RuntimeError(f"Telegram API {method_name} 调用失败：HTTP {status} {payload}")
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API {method_name} 返回失败：{payload}")
    return payload.get("result")


def detect_image_size(data, fallback_mimetype):
    content_type = str(fallback_mimetype or "").lower()
    if content_type in ("image/webp", "image/gif", "image/png", "image/jpeg"):
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "json", "pipe:0"],
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=20,
                check=False,
            )
            if probe.returncode == 0:
                parsed = json.loads(probe.stdout.decode("utf-8", errors="replace") or "{}")
                for stream in parsed.get("streams", []):
                    width = int(stream.get("width") or 0)
                    height = int(stream.get("height") or 0)
                    if width > 0 and height > 0:
                        return width, height
        except Exception:
            pass
    return 512, 512


def create_sticker_pack_record(conn, values, source_type=None, source_ref=None):
    slug = ensure_unique_pack_slug(conn, values["title"])
    cur = conn.execute(
        """insert into sticker_packs
           (slug, title, description, is_enabled, is_public, author_name, author_reference, license, source_type, source_ref, created_at, updated_at)
           values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            slug,
            values["title"],
            values["description"],
            values["is_enabled"],
            values["is_public"],
            values["author_name"],
            values["author_reference"],
            values["license"],
            source_type,
            source_ref,
            now_iso(),
            now_iso(),
        ),
    )
    return int(cur.lastrowid)


def create_sticker_pack(form):
    values = normalize_pack_form(form)
    conn = db()
    pack_id = create_sticker_pack_record(conn, values)
    conn.commit()
    conn.close()
    return pack_id


def update_sticker_pack(form):
    pack_id = int(form.get("pack_id", ["0"])[0])
    values = normalize_pack_form(form)
    conn = db()
    slug = ensure_unique_pack_slug(conn, values["title"], pack_id)
    conn.execute(
        """update sticker_packs
           set slug=?, title=?, description=?, is_enabled=?, is_public=?, author_name=?, author_reference=?, license=?, updated_at=?
           where id=?""",
        (
            slug,
            values["title"],
            values["description"],
            values["is_enabled"],
            values["is_public"],
            values["author_name"],
            values["author_reference"],
            values["license"],
            now_iso(),
            pack_id,
        ),
    )
    conn.commit()
    conn.close()
    return pack_id


def delete_sticker_pack(pack_id):
    conn = db()
    conn.execute("delete from sticker_packs where id=?", (pack_id,))
    conn.commit()
    conn.close()


def refresh_pack_avatar(conn, pack_id):
    first_sticker = conn.execute(
        "select id from stickers where pack_id=? order by id asc limit 1",
        (pack_id,),
    ).fetchone()
    conn.execute(
        "update sticker_packs set avatar_sticker_id=?, updated_at=? where id=?",
        (int(first_sticker["id"]) if first_sticker else None, now_iso(), pack_id),
    )


def delete_sticker(sticker_id):
    conn = db()
    row = conn.execute("select pack_id from stickers where id=?", (sticker_id,)).fetchone()
    conn.execute("delete from stickers where id=?", (sticker_id,))
    if row:
        refresh_pack_avatar(conn, int(row["pack_id"]))
    conn.commit()
    conn.close()


def upload_matrix_media(filename, data, mimetype):
    safe_name = os.path.basename(filename or "sticker")
    query = urllib.parse.urlencode({"filename": safe_name})
    url = f"{BASE_URL}/_matrix/media/v3/upload?{query}"
    status, payload = request_bytes("POST", url, data, mimetype)
    if not 200 <= status < 300:
        raise RuntimeError(f"媒体上传失败：HTTP {status} {payload}")
    content_uri = payload.get("content_uri")
    if not content_uri:
        raise RuntimeError("媒体上传成功但没有返回 content_uri")
    return content_uri


def transcode_video_to_animated_webp(filename, data):
    with tempfile.TemporaryDirectory(prefix="sticker-video-", dir="/tmp") as tmpdir:
        src_path = os.path.join(tmpdir, os.path.basename(filename or "sticker.webm"))
        dst_path = os.path.join(tmpdir, "sticker.webp")
        with open(src_path, "wb") as f:
            f.write(data)
        # 用 animated webp 作为发送格式兼容层：Element 在房间消息里对它的动态显示通常比 webm sticker 更稳定。
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                src_path,
                "-loop",
                "0",
                "-an",
                "-vf",
                "fps=15,scale='min(512,iw)':-1:flags=lanczos",
                dst_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
            check=False,
        )
        if proc.returncode != 0 or not os.path.exists(dst_path):
            raise RuntimeError(f"视频贴纸转 animated webp 失败：{proc.stdout.decode('utf-8', errors='replace')}")
        with open(dst_path, "rb") as f:
            return f.read()


def fit_dimensions(width, height, max_dimension):
    width = max(1, int(width or 1))
    height = max(1, int(height or 1))
    max_dimension = max(1, int(max_dimension or 1))
    scale = min(max_dimension / width, max_dimension / height, 1.0)
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def insert_sticker_record(
    conn,
    pack_id,
    name,
    body,
    image_mxc,
    mimetype,
    width,
    height,
    size_bytes,
    preview_image_mxc=None,
    preview_image_mimetype=None,
    send_image_mxc=None,
    send_image_mimetype=None,
    source_ref=None,
):
    return conn.execute(
        """insert into stickers
           (pack_id, name, body, image_mxc, preview_image_mxc, preview_image_mimetype, send_image_mxc, send_image_mimetype, source_ref, mimetype, width, height, size_bytes, created_at, updated_at)
           values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            pack_id,
            name,
            body,
            image_mxc,
            preview_image_mxc,
            preview_image_mimetype,
            send_image_mxc,
            send_image_mimetype,
            source_ref,
            mimetype,
            int(width or 512),
            int(height or 512),
            int(size_bytes or 0),
            now_iso(),
            now_iso(),
        ),
    )


def sticker_uploads(files):
    uploads = files.get("file", [])
    if isinstance(uploads, dict):
        uploads = [uploads]
    return [item for item in uploads if item and item.get("data")]


def sticker_name_from_upload(upload, fallback):
    filename = upload.get("filename") or ""
    base = os.path.basename(filename).rsplit(".", 1)[0].strip()
    return base or fallback or "sticker"


def add_sticker(form, files):
    pack_id = int(form.get("pack_id", ["0"])[0])
    uploads = sticker_uploads(files)
    if not uploads:
        raise ValueError("请选择要上传的贴纸文件")
    name = form.get("name", [""])[0].strip()
    description = form.get("description", [""])[0].strip()
    width = parse_int(form, "thumbnailWidth", 1, 4096)
    height = parse_int(form, "thumbnailHeight", 1, 4096)
    uploaded = 0
    failed = []
    conn = db()
    for upload in uploads:
        sticker_name = name if len(uploads) == 1 and name else sticker_name_from_upload(upload, name)
        try:
            add_single_sticker(conn, pack_id, sticker_name, description or sticker_name, upload, width, height, form)
            uploaded += 1
        except Exception as e:
            # 批量上传时单个文件失败不应拖垮整批；页面会提示失败文件，已成功的贴纸仍入库。
            failed.append(f"{upload.get('filename') or sticker_name}: {e}")
    if uploaded:
        refresh_pack_avatar(conn, pack_id)
        conn.commit()
    else:
        conn.rollback()
    conn.close()
    if not uploaded:
        raise ValueError("没有成功上传任何贴纸：" + "；".join(failed[:3]))
    return {"uploaded": uploaded, "failed": failed, "pack_id": pack_id}


def add_single_sticker(conn, pack_id, name, body, upload, width, height, form):
    filename = upload.get("filename") or name
    guessed = mimetypes.guess_type(filename)[0]
    mimetype = form.get("mimetype", [""])[0].strip() or upload.get("mimetype") or guessed or "application/octet-stream"
    image_mxc = upload_matrix_media(filename, upload["data"], mimetype)
    preview_image_mxc = None
    preview_image_mimetype = None
    send_image_mxc = None
    send_image_mimetype = None
    if mimetype.startswith("video/"):
        animated_webp = transcode_video_to_animated_webp(filename, upload["data"])
        send_image_mxc = upload_matrix_media(os.path.splitext(filename)[0] + ".webp", animated_webp, "image/webp")
        send_image_mimetype = "image/webp"
    insert_sticker_record(
        conn=conn,
        pack_id=pack_id,
        name=name,
        body=body,
        image_mxc=image_mxc,
        preview_image_mxc=preview_image_mxc,
        preview_image_mimetype=preview_image_mimetype,
        send_image_mxc=send_image_mxc,
        send_image_mimetype=send_image_mimetype,
        source_ref=None,
        mimetype=mimetype,
        width=width,
        height=height,
        size_bytes=len(upload["data"]),
    )


def import_telegram_sticker_pack(form):
    short_name = normalize_telegram_pack_ref(form.get("telegram_pack_ref", [""])[0])
    result = telegram_api_call("getStickerSet", name=short_name)
    stickers = result.get("stickers") or []
    if not stickers:
        raise ValueError("这个 Telegram 贴纸包没有可导入的贴纸")
    unsupported = [item.get("type") for item in stickers if item.get("is_animated")]
    if unsupported:
        raise ValueError("当前导入器暂不支持 Telegram TGS/Lottie 动画贴纸，请先导入图片包或视频贴纸包")
    author_name = form.get("authorName", [""])[0].strip() or "Telegram"
    values = {
        "title": form.get("name", [""])[0].strip() or result.get("title") or short_name,
        "description": form.get("description", [""])[0].strip() or result.get("title") or short_name,
        "is_enabled": 1 if form.get("isEnabled", ["1"])[0] == "1" else 0,
        "is_public": 1 if form.get("isPublic", ["1"])[0] == "1" else 0,
        "author_name": author_name,
        "author_reference": form.get("authorReference", [""])[0].strip() or f"https://t.me/addstickers/{short_name}",
        "license": form.get("license", ["Telegram Imported"])[0].strip() or "Telegram Imported",
    }
    conn = db()
    exists = conn.execute(
        "select id, title from sticker_packs where source_type=? and source_ref=? limit 1",
        ("telegram", short_name),
    ).fetchone()
    if exists:
        conn.close()
        raise ValueError(f"这个 Telegram 贴纸包已经导入过了：#{int(exists['id'])} {exists['title']}")
    pack_id = create_sticker_pack_record(conn, values, source_type="telegram", source_ref=short_name)
    imported = 0
    skipped = 0
    try:
        for item in stickers:
            file_id = item.get("file_id")
            unique_ref = item.get("file_unique_id") or file_id
            if not file_id or not unique_ref:
                skipped += 1
                continue
            file_info = telegram_api_call("getFile", file_id=file_id)
            file_path = file_info.get("file_path")
            if not file_path:
                skipped += 1
                continue
            original_name = os.path.basename(file_path) or unique_ref
            mimetype = mimetypes.guess_type(original_name)[0] or "application/octet-stream"
            content_type, binary = request_binary_noauth(telegram_file_url(file_path))
            if content_type:
                mimetype = content_type.split(";", 1)[0].strip()
            mimetype = detect_binary_mimetype(original_name, binary, mimetype)
            image_mxc = upload_matrix_media(original_name, binary, mimetype)
            preview_image_mxc = None
            preview_image_mimetype = None
            send_image_mxc = None
            send_image_mimetype = None
            if mimetype.startswith("video/"):
                animated_webp = transcode_video_to_animated_webp(original_name, binary)
                send_image_mxc = upload_matrix_media(os.path.splitext(original_name)[0] + ".webp", animated_webp, "image/webp")
                send_image_mimetype = "image/webp"
            width = int(item.get("width") or 0)
            height = int(item.get("height") or 0)
            if width <= 0 or height <= 0:
                width, height = detect_image_size(binary, mimetype)
            name = item.get("emoji") or item.get("set_name") or f"telegram-{imported + 1}"
            body = item.get("emoji") or name
            insert_sticker_record(
                conn=conn,
                pack_id=pack_id,
                name=name,
                body=body,
                image_mxc=image_mxc,
                preview_image_mxc=preview_image_mxc,
                preview_image_mimetype=preview_image_mimetype,
                send_image_mxc=send_image_mxc,
                send_image_mimetype=send_image_mimetype,
                source_ref=unique_ref,
                mimetype=mimetype,
                width=width,
                height=height,
                size_bytes=len(binary),
            )
            imported += 1
        if imported == 0:
            raise ValueError("这个 Telegram 贴纸包没有成功导入任何贴纸")
        refresh_pack_avatar(conn, pack_id)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.execute("delete from sticker_packs where id=?", (pack_id,))
        conn.commit()
        conn.close()
        raise
    conn.close()
    return {"pack_id": pack_id, "imported": imported, "skipped": skipped, "short_name": short_name}


def postgres_env():
    env = os.environ.copy()
    env["PGPASSWORD"] = SYNAPSE_DB_PASSWORD
    return env


def run_psql(sql):
    proc = subprocess.run(
        [
            "psql",
            "-h",
            SYNAPSE_DB_HOST,
            "-p",
            SYNAPSE_DB_PORT,
            "-U",
            SYNAPSE_DB_USER,
            "-d",
            SYNAPSE_DB_NAME,
            "-Atc",
            sql,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=postgres_env(),
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout)
    return proc.stdout


def sql_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def load_user_widget_map():
    raw = run_psql(
        "select user_id || E'\\x1f' || content::text from account_data where account_data_type='m.widgets' order by user_id"
    )
    current = {}
    for line in raw.splitlines():
        if not line.strip() or "\x1f" not in line:
            continue
        user_id, content = line.split("\x1f", 1)
        try:
            current[user_id] = json.loads(content)
        except Exception:
            current[user_id] = {}
    return current


def resolve_sticker_target_users():
    cfg = sticker_config()
    mode = cfg["delivery_mode"]
    if mode == "disabled":
        return []
    if mode == "selected_users":
        selected = []
        allowed = set(list_sticker_target_users())
        for mxid in cfg["target_users"]:
            if mxid in allowed:
                selected.append(mxid)
        return selected
    return list_sticker_target_users()


def save_sticker_delivery_settings(form):
    mode = form.get("delivery_mode", ["all_users"])[0].strip() or "all_users"
    if mode not in ("all_users", "selected_users", "disabled"):
        raise ValueError("贴纸下发模式无效")
    targets = split_mxids("\n".join(form.get("target_users", [])))
    invalid = [mxid for mxid in targets if not mxid.endswith(f":{SERVER_NAME}")]
    if invalid:
        raise ValueError("指定用户必须是本服 MXID：" + ", ".join(invalid))
    save_config(
        {
            "sticker_delivery_mode": mode,
            "sticker_target_users": "\n".join(targets),
        }
    )
    return {"mode": mode, "targets": targets}


def safe_sticker_user_list():
    try:
        users = list_sticker_target_users()
        save_sticker_user_cache(users)
        return users, ""
    except Exception as e:
        # 用户列表只是下发同步和 UI 辅助信息；Synapse 管理 API 暂时抖动时不应阻塞包管理本身。
        cached = cached_sticker_user_list()
        if cached:
            return cached, f"用户列表暂时读取失败，已使用上次缓存：{e}"
        return [], f"用户列表读取失败：{e}"


def sync_sticker_widgets(restart_synapse=True):
    with STICKER_SYNC_LOCK:
        local_users = list_sticker_target_users()
        target_users = resolve_sticker_target_users()
        current_widgets = load_user_widget_map()
        current_stream = int((run_psql("select coalesce(max(stream_id), 0) from account_data;").strip() or "0"))
        values = []
        changed_count = 0
        target_set = set(target_users)
        for offset, mxid in enumerate(local_users, start=1):
            widgets = dict(current_widgets.get(mxid) or {})
            before = json.dumps(widgets, ensure_ascii=False, sort_keys=True)
            # 兼容清理旧贴纸时代遗留的 widget 定义，避免同一账号里同时残留两套贴纸组件。
            for widget_id, widget in list(widgets.items()):
                if is_legacy_sticker_widget(widget_id, widget):
                    widgets.pop(widget_id, None)
            if mxid in target_set:
                widgets["stickerpicker"] = widget_account_data(mxid)["stickerpicker"]
            else:
                widgets.pop("stickerpicker", None)
            after = json.dumps(widgets, ensure_ascii=False, sort_keys=True)
            if before == after:
                continue
            values.append(
                "({user}, 'm.widgets', {stream}, {content})".format(
                    user=sql_quote(mxid),
                    stream=current_stream + offset,
                    content=sql_quote(after),
                )
            )
            changed_count += 1
        if values:
            upsert_sql = (
                "insert into account_data(user_id, account_data_type, stream_id, content) values "
                + ",".join(values)
                + " on conflict(user_id, account_data_type) do update "
                  "set stream_id=excluded.stream_id, content=excluded.content;"
            )
            run_psql(upsert_sql)
        if restart_synapse and changed_count:
            # Synapse 会缓存 account_data；贴纸下发策略有变化时重启一次，让客户端尽快读到新的账号级组件。
            subprocess.run(["docker", "restart", SYNAPSE_CONTAINER], timeout=120, check=False)
        return {
            "users": len(target_users),
            "local_users": len(local_users),
            "packs": len(list_public_sticker_packs()),
            "changed": bool(changed_count),
            "changed_users": changed_count,
        }


def sticker_auto_sync_loop():
    while True:
        try:
            if safe_sticker_user_list()[0]:
                sync_sticker_widgets(restart_synapse=False)
        except Exception as e:
            append_log(f"sticker auto sync error: {e}")
        time.sleep(max(60, STICKER_AUTO_SYNC_SECONDS))


def list_users():
    users = []
    start = 0
    while True:
        url = f"{BASE_URL}/_synapse/admin/v2/users?from={start}&limit=100&guests=false"
        status, data = request_json("GET", url)
        if status != 200:
            raise RuntimeError(f"list users failed: HTTP {status} {data}")
        for item in data.get("users", []):
            mxid = item.get("name", "")
            if not mxid.endswith(f":{SERVER_NAME}"):
                continue
            if item.get("deactivated") or item.get("is_guest"):
                continue
            users.append(mxid)
        next_token = data.get("next_token")
        if next_token is None:
            break
        start = int(next_token)
    return users


def list_sticker_target_users():
    users = []
    seen = set()
    for mxid in list_users_for_stickers():
        localpart = mxid.split(":", 1)[0].lstrip("@").lower()
        if localpart in STICKER_SYNC_EXCLUDE_LOCALPARTS:
            continue
        if mxid not in seen:
            seen.add(mxid)
            users.append(mxid)
    return users


def list_users_from_synapse_db():
    raw = run_psql(
        "select name from users "
        f"where name like {sql_quote('%:' + SERVER_NAME)} "
        "and coalesce(deactivated, 0) = 0 "
        "and coalesce(is_guest, 0) = 0 "
        "and appservice_id is null "
        "order by name asc;"
    )
    return [line.strip() for line in raw.splitlines() if line.strip()]


def list_users_for_stickers():
    try:
        # 贴纸下发不应依赖 Synapse Admin HTTP 接口的临时连接状态；
        # 运维工具与 Postgres 同机时优先读本地 users 表，取消/新增用户时更稳定。
        return list_users_from_synapse_db()
    except Exception as e:
        append_log(f"sticker user db list failed, fallback to admin api: {e}")
        return list_users()


def migrate_legacy_sticker_packs_if_needed():
    conn = db()
    existing = conn.execute("select count(*) as c from sticker_packs").fetchone()
    if int(existing["c"]) > 0:
        conn.close()
        return
    if not LEGACY_STICKER_DB_PATH or not os.path.exists(LEGACY_STICKER_DB_PATH):
        conn.close()
        return
    legacy = sqlite3.connect(LEGACY_STICKER_DB_PATH)
    legacy.row_factory = sqlite3.Row
    try:
        pack_rows = legacy.execute(
            "select * from dimension_sticker_packs order by id asc"
        ).fetchall()
    except Exception:
        legacy.close()
        conn.close()
        return
    for legacy_pack in pack_rows:
        slug = ensure_unique_pack_slug(conn, legacy_pack["name"])
        cur = conn.execute(
            """insert into sticker_packs
               (slug, title, description, is_enabled, is_public, author_name, author_reference, license, created_at, updated_at)
               values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                slug,
                legacy_pack["name"],
                legacy_pack["description"] or legacy_pack["name"],
                int(legacy_pack["isEnabled"] or 0),
                int(legacy_pack["isPublic"] or 0),
                legacy_pack["authorName"],
                legacy_pack["authorReference"],
                legacy_pack["license"] or "Custom",
                now_iso(),
                now_iso(),
            ),
        )
        new_pack_id = int(cur.lastrowid)
        sticker_rows = legacy.execute(
            "select * from dimension_stickers where packId=? order by id asc",
            (legacy_pack["id"],),
        ).fetchall()
        for legacy_sticker in sticker_rows:
            conn.execute(
                """insert into stickers
                   (pack_id, name, body, image_mxc, preview_image_mxc, preview_image_mimetype, send_image_mxc, send_image_mimetype, mimetype, width, height, size_bytes, created_at, updated_at)
                   values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_pack_id,
                    legacy_sticker["name"],
                    legacy_sticker["description"] or legacy_sticker["name"],
                    legacy_sticker["imageMxc"],
                    legacy_sticker["thumbnailMxc"] if legacy_sticker["thumbnailMxc"] != legacy_sticker["imageMxc"] else None,
                    "image/png" if legacy_sticker["thumbnailMxc"] and legacy_sticker["thumbnailMxc"] != legacy_sticker["imageMxc"] else None,
                    None,
                    None,
                    legacy_sticker["mimetype"] or "application/octet-stream",
                    int(legacy_sticker["thumbnailWidth"] or 512),
                    int(legacy_sticker["thumbnailHeight"] or 512),
                    0,
                    now_iso(),
                    now_iso(),
                ),
            )
        refresh_pack_avatar(conn, new_pack_id)
    conn.commit()
    legacy.close()
    conn.close()
    append_log("legacy sticker packs imported into local stickerpicker store")


def backfill_legacy_video_thumbnails():
    if not LEGACY_STICKER_DB_PATH or not os.path.exists(LEGACY_STICKER_DB_PATH):
        return
    conn = db()
    pending = conn.execute(
        """select id, image_mxc, mimetype from stickers
           where mimetype like 'video/%' and (preview_image_mxc is null or preview_image_mxc='')"""
    ).fetchall()
    if not pending:
        conn.close()
        return
    legacy = sqlite3.connect(LEGACY_STICKER_DB_PATH)
    legacy.row_factory = sqlite3.Row
    changed = 0
    for row in pending:
        legacy_row = legacy.execute(
            "select thumbnailMxc from dimension_stickers where imageMxc=? limit 1",
            (row["image_mxc"],),
        ).fetchone()
        if not legacy_row:
            continue
        thumbnail_mxc = legacy_row["thumbnailMxc"]
        if not thumbnail_mxc or thumbnail_mxc == row["image_mxc"]:
            continue
        conn.execute(
            "update stickers set preview_image_mxc=?, preview_image_mimetype=? where id=?",
            (thumbnail_mxc, "image/png", int(row["id"])),
        )
        changed += 1
    conn.commit()
    legacy.close()
    conn.close()
    if changed:
        append_log(f"backfilled {changed} legacy video sticker thumbnails")


def backfill_video_send_assets():
    conn = db()
    rows = conn.execute(
        """select id, image_mxc, mimetype from stickers
           where mimetype like 'video/%' and (send_image_mxc is null or send_image_mxc='')"""
    ).fetchall()
    if not rows:
        conn.close()
        return
    changed = 0
    for row in rows:
        try:
            content_type, payload = proxy_matrix_media(row["image_mxc"])
            animated_webp = transcode_video_to_animated_webp(f"sticker-{row['id']}.webm", payload)
            send_mxc = upload_matrix_media(f"sticker-{row['id']}.webp", animated_webp, "image/webp")
            conn.execute(
                "update stickers set send_image_mxc=?, send_image_mimetype=? where id=?",
                (send_mxc, "image/webp", int(row["id"])),
            )
            changed += 1
        except Exception as e:
            append_log(f"video send asset backfill failed for sticker #{row['id']}: {e}")
    conn.commit()
    conn.close()
    if changed:
        append_log(f"backfilled {changed} video sticker send assets")


def backfill_octet_stream_stickers():
    conn = db()
    rows = conn.execute(
        """select id, name, image_mxc, mimetype, send_image_mimetype
           from stickers
           where mimetype='application/octet-stream'
              or send_image_mimetype='application/octet-stream'"""
    ).fetchall()
    if not rows:
        conn.close()
        return
    changed = 0
    for row in rows:
        try:
            content_type, payload = proxy_matrix_media(row["image_mxc"])
            detected = detect_binary_mimetype(row["name"], payload, content_type.split(";", 1)[0].strip() if content_type else "application/octet-stream")
            if detected == "application/octet-stream":
                continue
            send_mimetype = row["send_image_mimetype"] or row["mimetype"]
            if send_mimetype == "application/octet-stream":
                send_mimetype = detected
            conn.execute(
                "update stickers set mimetype=?, send_image_mimetype=? where id=?",
                (detected, send_mimetype, int(row["id"])),
            )
            changed += 1
        except Exception as e:
            append_log(f"octet-stream sticker backfill failed for sticker #{row['id']}: {e}")
    conn.commit()
    conn.close()
    if changed:
        append_log(f"backfilled {changed} octet-stream sticker mimetypes")


def stable_extension_for_mimetype(mimetype, default_ext=""):
    normalized = str(mimetype or "").split(";", 1)[0].strip().lower()
    # 贴纸公开资源文件名不能依赖系统 mime.types 猜后缀；
    # Alpine/不同运行环境下对 webp 等类型的映射并不稳定，这里显式收敛成固定规则。
    mapping = {
        "image/webp": ".webp",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "video/webm": ".webm",
        "video/mp4": ".mp4",
    }
    if normalized in mapping:
        return mapping[normalized]
    guessed = mimetypes.guess_extension(normalized) or ""
    if guessed:
        return guessed
    return default_ext


def stable_mimetype_for_filename(path, default_type="application/octet-stream"):
    lowered = os.path.basename(str(path or "")).lower()
    for ext, mimetype in (
        (".webp", "image/webp"),
        (".png", "image/png"),
        (".jpg", "image/jpeg"),
        (".jpeg", "image/jpeg"),
        (".gif", "image/gif"),
        (".webm", "video/webm"),
        (".mp4", "video/mp4"),
        (".json", "application/json; charset=utf-8"),
        (".js", "text/javascript; charset=utf-8"),
        (".css", "text/css; charset=utf-8"),
        (".html", "text/html; charset=utf-8"),
    ):
        if lowered.endswith(ext):
            return mimetype
    guessed = mimetypes.guess_type(lowered)[0]
    if guessed:
        return guessed
    return default_type


def sticker_media_filename(sticker):
    mxc = sticker["image_mxc"]
    _, media_id = parse_mxc(mxc)
    ext = stable_extension_for_mimetype(sticker["mimetype"])
    safe_name = normalize_slug(sticker["name"]) or "sticker"
    return f"{safe_name}-{media_id}{ext}"


def proxy_matrix_media(mxc):
    server_name, media_id = parse_mxc(mxc)
    # 当前生产 Synapse 对带鉴权的媒体下载走 client/v1 端点，media/v3 在这个部署上会直接 404。
    url = f"{BASE_URL}/_matrix/client/v1/media/download/{urllib.parse.quote(server_name, safe='')}/{urllib.parse.quote(media_id, safe='')}"
    headers = {"Authorization": f"Bearer {token()}"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.headers.get("Content-Type", "application/octet-stream"), resp.read()


def sticker_preview_url(sticker):
    sticker_id = int(sticker["id"]) if isinstance(sticker, sqlite3.Row) or isinstance(sticker, dict) else int(sticker)
    return f"{STICKER_ROUTE_PREFIX}/media/{sticker_id}"


def sticker_pack_media_filename(sticker, preview_mimetype=None):
    ext = stable_extension_for_mimetype(preview_mimetype or sticker["mimetype"], ".bin")
    return f"{int(sticker['id'])}{ext}"


def sticker_pack_media_web_path(sticker, preview_mimetype=None):
    return f"media/{sticker_pack_media_filename(sticker, preview_mimetype)}"


def exported_sticker_preview_url(sticker):
    _, preview_mimetype = resolve_preview_export_source(sticker)
    return f"{STICKER_ROUTE_PREFIX}/packs/{sticker_pack_media_web_path(sticker, preview_mimetype)}"


def resolve_preview_export_source(sticker):
    # Element 的部分桌面/WebView 环境对导航栏这种小尺寸 video 预览并不稳定，
    # 这里把“视频贴纸在 picker 里的预览资源”统一切到 animated webp 兼容层。
    # 这样房间发送仍走 send_image_mxc，而 picker 顶部入口和贴纸格子都能稳定显示动态预览。
    if str(sticker["mimetype"] or "").startswith("video/"):
        send_mxc = sticker["send_image_mxc"] or ""
        send_mimetype = sticker["send_image_mimetype"] or ""
        if send_mxc and send_mimetype:
            return send_mxc, send_mimetype
    return sticker["image_mxc"], sticker["mimetype"]


def write_bytes_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp-{uuid.uuid4().hex}"
    with open(tmp_path, "wb") as f:
        f.write(data)
    os.replace(tmp_path, path)


def write_json_atomic(path, payload):
    write_bytes_atomic(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def rebuild_stickerpicker_static_assets():
    packs = list_public_sticker_packs()
    os.makedirs(STICKER_PACKS_DIR, exist_ok=True)
    # 这里保留官方 web/packs 目录结构，但数据源仍是后台数据库；
    # 每次贴纸包有变更时都整体重导出，避免删包、改名后留下过期静态文件。
    for entry in os.listdir(STICKER_PACKS_DIR):
        target = os.path.join(STICKER_PACKS_DIR, entry)
        if entry == "media":
            shutil.rmtree(target, ignore_errors=True)
        elif entry.endswith(".json"):
            try:
                os.remove(target)
            except FileNotFoundError:
                pass
    os.makedirs(STICKER_PACK_MEDIA_DIR, exist_ok=True)
    for item in packs:
        pack = item["pack"]
        stickers = item["stickers"]
        exported_stickers = []
        for sticker in stickers:
            preview_mxc, preview_mimetype = resolve_preview_export_source(sticker)
            export_target = os.path.join(STICKER_PACKS_DIR, sticker_pack_media_web_path(sticker, preview_mimetype))
            try:
                _, payload = proxy_matrix_media(preview_mxc)
                write_bytes_atomic(export_target, payload)
                exported_stickers.append(sticker)
            except Exception as e:
                # 单个历史媒体损坏或 Synapse 临时断连时，只跳过这一张贴纸；
                # 不能让整个运维工具启动失败，否则下发策略和后续修复入口都会不可用。
                append_log(f"sticker export skipped sticker #{sticker['id']} from pack #{pack['id']}: {e}")
        pack_payload = build_pack_json(
            pack,
            exported_stickers,
            preview_url_builder=exported_sticker_preview_url,
            preview_source_resolver=resolve_preview_export_source,
        )
        write_json_atomic(os.path.join(STICKER_PACKS_DIR, f"{pack['slug']}.json"), pack_payload)
    write_json_atomic(os.path.join(STICKER_PACKS_DIR, "index.json"), sticker_index_payload())


def build_pack_json(pack, stickers, preview_url_builder=sticker_preview_url, preview_source_resolver=None):
    sticker_list = []
    preview_source_resolver = preview_source_resolver or (lambda current: (current["image_mxc"], current["mimetype"]))
    for sticker in stickers:
        send_mxc = sticker["send_image_mxc"] or sticker["image_mxc"]
        send_mimetype = sticker["send_image_mimetype"] or sticker["mimetype"]
        _preview_mxc, preview_mimetype = preview_source_resolver(sticker)
        send_width, send_height = fit_dimensions(
            sticker["width"],
            sticker["height"],
            STICKER_SEND_MAX_DIMENSION,
        )
        info = {
            "w": send_width,
            "h": send_height,
            "size": int(sticker["size_bytes"] or 0),
            "mimetype": send_mimetype,
        }
        # 视频贴纸在 picker 里继续走动态预览，但发送到房间时补上缩略图字段，
        # 让 Element 至少能拿到可显示的封面，而不是只退化成一个 emoji 占位。
        if sticker["preview_image_mxc"]:
            info["thumbnail_url"] = sticker["preview_image_mxc"]
            info["thumbnail_info"] = {
                "w": int(sticker["width"]),
                "h": int(sticker["height"]),
                "mimetype": sticker["preview_image_mimetype"] or "image/png",
                "size": 0,
            }
        content = {
            "id": f"sticker-{sticker['id']}",
            "body": sticker["body"],
            "msgtype": "m.sticker",
            "url": send_mxc,
            "filename": sticker_media_filename({"name": sticker["name"], "mimetype": send_mimetype, "image_mxc": send_mxc}),
            "preview_url": preview_url_builder(sticker),
            "preview_mimetype": preview_mimetype,
            "info": info,
        }
        sticker_list.append(content)
    return {
        "id": pack["slug"],
        "title": pack["title"],
        "description": pack["description"],
        "stickers": sticker_list,
    }


def sticker_index_payload():
    packs = list_public_sticker_packs()
    return {
        "generated_at": now_iso(),
        "packs": [f"{item['pack']['slug']}.json" for item in packs],
    }


def read_pack_by_slug(slug):
    conn = db()
    pack = conn.execute(
        "select * from sticker_packs where slug=? and is_enabled=1 and is_public=1",
        (slug,),
    ).fetchone()
    if not pack:
        conn.close()
        return None, None
    stickers = conn.execute(
        "select * from stickers where pack_id=? order by id asc",
        (pack["id"],),
    ).fetchall()
    conn.close()
    return pack, stickers


def sticker_static_file(path):
    base = os.path.abspath(STICKER_WEB_ROOT)
    target = os.path.abspath(os.path.join(base, path.lstrip("/")))
    if not target.startswith(base + os.sep) and target != base:
        return None
    if not os.path.isfile(target):
        return None
    return target


def split_mxids(raw):
    mxids = []
    seen = set()
    for item in str(raw or "").replace(",", "\n").splitlines():
        mxid = item.strip()
        if mxid and mxid not in seen:
            seen.add(mxid)
            mxids.append(mxid)
    return mxids


def suggested_notice_excludes(users):
    suggestions = []
    for mxid in users:
        localpart = mxid.split(":", 1)[0].lstrip("@").lower()
        if any(hint in localpart for hint in NOTICE_EXCLUDE_HINTS):
            suggestions.append(mxid)
    return suggestions


def normalize_notice_targets(form):
    target_mode = form.get("target_mode", ["single"])[0]
    if target_mode == "all":
        return target_mode, []

    raw_values = []
    raw_values.extend(form.get("single_users", []))
    raw_values.extend(form.get("single_user", []))
    targets = split_mxids("\n".join(raw_values))
    if not targets:
        raise ValueError("单用户目标不能为空")
    invalid = [mxid for mxid in targets if not mxid.endswith(f":{SERVER_NAME}")]
    if invalid:
        raise ValueError("单用户目标必须是本服 MXID：" + ", ".join(invalid))
    return "single", targets


def parse_int(form, key, minimum, maximum):
    try:
        value = int(form.get(key, [""])[0])
    except Exception:
        raise ValueError(f"{key} 必须是整数")
    if value < minimum or value > maximum:
        raise ValueError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
    return value


def normalize_purge_form(form):
    medium_days = parse_int(form, "medium_days", 1, 3650)
    large_days = parse_int(form, "large_days", 1, 3650)
    remote_days = parse_int(form, "remote_days", 1, 3650)
    medium_size_mib = parse_int(form, "medium_size_mib", 1, 102400)
    large_size_mib = parse_int(form, "large_size_mib", 1, 102400)
    # 紧急清理阈值必须 >= 不清理上限，否则中等区语义颠倒混乱。
    # 等于时中等区退化为空（不清理和紧急清理一致），属于可用但无意义的配置。
    if large_size_mib < medium_size_mib:
        large_size_mib = medium_size_mib
    keep_profiles = "true" if form.get("keep_profiles", ["false"])[0] == "true" else "false"
    schedule_hhmm = form.get("schedule_hhmm", ["0423"])[0].strip()
    if len(schedule_hhmm) != 4 or not schedule_hhmm.isdigit():
        raise ValueError("schedule_hhmm 必须是 4 位 HHMM")
    hh = int(schedule_hhmm[:2])
    mm = int(schedule_hhmm[2:])
    if hh > 23 or mm > 59:
        raise ValueError("schedule_hhmm 时间无效")
    return {
        "small_keep_size": medium_size_mib * 1024 * 1024,
        "medium_days": medium_days,
        "medium_size_gt": medium_size_mib * 1024 * 1024,
        "large_days": large_days,
        "large_size_gt": large_size_mib * 1024 * 1024,
        "remote_days": remote_days,
        "keep_profiles": keep_profiles,
        "schedule_hhmm": schedule_hhmm,
    }


def run_purge_job(job_id, dry_run, config):
    env = os.environ.copy()
    env.update(
        {
            "SYNAPSE_URL": BASE_URL,
            "TOKEN_FILE": TOKEN_FILE,
            "SMALL_KEEP_SIZE": str(config["small_keep_size"]),
            "MEDIUM_DAYS": str(config["medium_days"]),
            "MEDIUM_SIZE_GT": str(config["medium_size_gt"]),
            "LARGE_DAYS": str(config["large_days"]),
            "LARGE_SIZE_GT": str(config["large_size_gt"]),
            "REMOTE_DAYS": str(config["remote_days"]),
            "KEEP_PROFILES": str(config["keep_profiles"]),
            "DRY_RUN": "true" if dry_run else "false",
        }
    )
    # 媒体删除必须通过显式按钮触发；dry-run 只预览 Admin API，不删除任何媒体。
    proc = subprocess.run(
        [PURGE_SCRIPT],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        timeout=900,
        check=False,
    )
    status = "success" if proc.returncode == 0 else "failed"
    mode = "dry-run" if dry_run else "executed"
    finish_job(job_id, status, f"media purge {mode} rc={proc.returncode}", proc.stdout)


def send_notice_job(job_id, form):
    target_mode, single_targets = normalize_notice_targets(form)
    body = form.get("body", [""])[0].strip()
    txn_prefix = "web-notice-" + dt.datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8] + "-"
    exclude_raw = form.get("exclude", [""])[0].strip()
    if not body:
        raise ValueError("通知内容不能为空")
    exclude = set()
    for item in exclude_raw.replace(",", "\n").splitlines():
        item = item.strip()
        if item:
            exclude.add(item)
    if target_mode == "all":
        targets = [u for u in list_users() if u not in exclude]
    else:
        # 单用户模式允许一次给少量指定账号发送；仍限制为本服 MXID，避免误把外服用户交给 Server Notice。
        targets = [u for u in single_targets if u not in exclude]
    if not targets:
        raise ValueError("没有可发送的目标用户")
    content = {"msgtype": "m.notice", "body": body}
    sent = []
    failed = []
    for mxid in targets:
        txn = txn_prefix + urllib.parse.quote(mxid, safe="")
        url = f"{BASE_URL}/_synapse/admin/v1/send_server_notice/{txn}"
        status, data = request_json("PUT", url, {"user_id": mxid, "content": content})
        if 200 <= status < 300:
            sent.append({"user_id": mxid, "event_id": data.get("event_id")})
        else:
            failed.append({"user_id": mxid, "http_code": status, "response": data})
        time.sleep(0.1)
    output = json.dumps(
        {
            "target_mode": target_mode,
            "target_count": len(targets),
            "targets": targets,
            "body": body,
            "txn_prefix": txn_prefix,
            "sent": sent,
            "failed": failed,
        },
        ensure_ascii=False,
        indent=2,
    )
    status = "success" if not failed else "failed"
    finish_job(job_id, status, f"notice targets={len(targets)} failed={len(failed)}", output)


def scheduler_loop():
    last_date = None
    initialized = False
    while True:
        try:
            cfg = get_config()
            hhmm = cfg.get("schedule_hhmm", "0423")
            today = dt.date.today().isoformat()
            now_hhmm = dt.datetime.now().strftime("%H%M")
            if not initialized:
                # 每日清理是生产删除动作；服务启动时若已过当天时间点，不补跑，避免重启后立刻误删。
                if now_hhmm >= hhmm:
                    last_date = today
                initialized = True
            if now_hhmm >= hhmm and last_date != today:
                job_id = create_job("scheduled-media-purge")
                run_purge_job(job_id, False, cfg)
                last_date = today
        except Exception as e:
            append_log(f"scheduler error: {e}")
        time.sleep(60)


def esc(value):
    return html.escape(str(value), quote=True)


def short_text(value, limit=160):
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def compact_id(value, head=12, tail=8):
    text = str(value or "").strip()
    if len(text) <= head + tail + 3:
        return text
    return f"{text[:head]}...{text[-tail:]}"


def parse_job_output(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def render_job_detail(row):
    kind = row["kind"]
    output = row["output"] or ""
    parsed = parse_job_output(output)
    if kind == "server-notice" and isinstance(parsed, dict):
        targets = parsed.get("targets") or [item.get("user_id") for item in parsed.get("sent", []) if item.get("user_id")]
        sent = parsed.get("sent", [])
        failed = parsed.get("failed", [])
        event_ids = [item.get("event_id") for item in sent if item.get("event_id")]
        body = parsed.get("body", "")
        target_items = "".join(f"<li>{esc(target)}</li>" for target in targets) or "<li>-</li>"
        event_items = "".join(
            f"<li><span class=\"mono\" title=\"{esc(event_id)}\">{esc(compact_id(event_id))}</span></li>"
            for event_id in event_ids
        ) or "<li>-</li>"
        failed_detail = ""
        if failed:
            failed_detail = (
                "<details class=\"wide\"><summary>查看失败详情</summary>"
                f"<pre>{esc(json.dumps(failed, ensure_ascii=False, indent=2))}</pre></details>"
            )
        return f"""
<div class="job-detail">
  <div><strong>发送范围</strong><br>{esc(parsed.get('target_mode', ''))}</div>
  <div><strong>目标数量</strong><br>{len(targets)}</div>
  <div><strong>成功/失败</strong><br>{len(sent)} / {len(failed)}</div>
  <div><strong>正文摘要</strong><br><span class="long-text">{esc(short_text(body, 80) or '-')}</span></div>
  <details class="wide"><summary>查看目标用户</summary><ul class="compact-list long-text">{target_items}</ul></details>
  <details class="wide"><summary>查看 Event ID</summary><ul class="compact-list long-text">{event_items}</ul></details>
  <details class="wide"><summary>查看通知正文</summary><pre>{esc(body)}</pre></details>
  {failed_detail}
</div>"""
    if kind in ("media-purge-dry-run", "media-purge-execute", "scheduled-media-purge"):
        lines = [line for line in output.splitlines() if line.strip()]
        summary_lines = []
        for line in lines:
            if "DRY_RUN=" in line or "purge " in line or "Policy:" in line:
                summary_lines.append(line)
        summary_text = "\n".join(summary_lines[:12]) or output
        return f"""
<div class="job-detail">
  <div><strong>模式</strong><br>{esc(kind)}</div>
  <div><strong>结果</strong><br>{esc(row['status'])}</div>
  <details class="wide" open><summary>查看执行摘要</summary><pre>{esc(summary_text)}</pre></details>
</div>"""
    return f"<details><summary>查看详情</summary><pre>{esc(output)}</pre></details>"


def status_class(status):
    if status == "success":
        return "ok"
    if status == "failed":
        return "bad"
    return "run"


def mib(bytes_value):
    return int(int(bytes_value) / 1024 / 1024)


def render_policy_summary(cfg):
    medium_mib = mib(cfg["medium_size_gt"])
    large_mib = mib(cfg["large_size_gt"])
    return f"""
<div class="policy-box">
  <strong>当前自动清理规则</strong>
  <ul>
    <li>≤{medium_mib} MiB：小文件，长期保留，不会自动删除。</li>
    <li>{medium_mib}~{large_mib} MiB：中等文件，超过 {esc(cfg['medium_days'])} 天自动删除。</li>
    <li>&gt;{large_mib} MiB：大文件，超过 {esc(cfg['large_days'])} 天自动紧急清理。</li>
    <li>远端缓存超过 {esc(cfg['remote_days'])} 天会清理；头像和资料引用媒体按下面开关决定是否保留。</li>
    <li>头像包括用户头像、房间头像这类被 Matrix 资料引用的图片；资料引用媒体指用户资料、房间资料等当前资料状态引用的媒体，不是普通聊天附件。</li>
    <li>每天 {esc(cfg['schedule_hhmm'])} 自动检查一次；不是每天全部删除，只删除命中以上条件的媒体。</li>
  </ul>
</div>"""


def render_notice_user_picker(users, error=""):
    if error:
        return f"""
      <div>
        <label>从现有账号选择</label>
        <div class="muted">暂时无法读取用户列表：{esc(error)}</div>
      </div>"""
    if not users:
        return """
      <div>
        <label>从现有账号选择</label>
        <div class="muted">没有可选用户。</div>
      </div>"""
    buttons = "".join(
        f"<button type=\"button\" class=\"chip notice-user-chip\" data-mxid=\"{esc(mxid)}\">{esc(mxid)}</button>"
        for mxid in users
    )
    return f"""
      <div>
        <label>从现有账号选择</label>
        <div id="notice-user-picker" class="chip-box" data-users="{esc(json.dumps(users, ensure_ascii=False))}">
          {buttons}
        </div>
        <div class="muted">点账号加入指定用户，再点一次取消。</div>
      </div>"""


def render_notice_exclude_summary(suggestions):
    if not suggestions:
        return """
    <div class="quick-pick">
      <label>自动排除账号</label>
      <div class="muted">没有从现有账号里自动识别到服务号或机器人；下面可手动填写。</div>
    </div>"""
    return f"""
    <div class="quick-pick">
      <label>自动排除账号</label>
      <div class="muted">已自动识别并预填 {len(suggestions)} 个服务号/机器人账号；下面仍可手动增删。</div>
    </div>"""


def render_sticker_manager():
    cfg = sticker_config()
    users, user_error = safe_sticker_user_list()
    try:
        packs = list_sticker_packs()
        error = ""
    except Exception as e:
        packs = []
        error = str(e)
    enabled_packs = [item for item in packs if int(item["pack"]["is_enabled"]) == 1]
    public_packs = [item for item in packs if int(item["pack"]["is_public"]) == 1]
    total_stickers = sum(len(item["stickers"]) for item in packs)
    target_users = resolve_sticker_target_users() if not user_error else []
    selected_users = cfg["target_users"]
    if cfg["delivery_mode"] == "all_users":
        # 全员模式下页面也按“实际会下发的用户”展示，避免仍显示旧的指定名单造成误解。
        displayed_selected_users = target_users
        selected_hint = "当前是全员开启模式，下面绿色用户都会拿到贴纸组件；新用户注册后也会自动补齐。"
    elif cfg["delivery_mode"] == "disabled":
        displayed_selected_users = []
        selected_hint = "当前已关闭贴纸组件下发，保存同步后会回收真实用户账号里的贴纸入口。"
    else:
        displayed_selected_users = selected_users
        selected_hint = "点一下加入，再点一下移除。切到“只给指定用户开启”时更适合使用这一栏。"
    pack_options = "".join(
        f"<option value=\"{esc(item['pack']['id'])}\">#{esc(item['pack']['id'])} {esc(item['pack']['title'])}</option>"
        for item in packs
    ) or "<option value=\"\">请先创建贴纸包</option>"
    user_chips = "".join(
        f"<button type=\"button\" class=\"chip sticker-user-chip{' selected' if mxid in displayed_selected_users else ''}\" data-mxid=\"{esc(mxid)}\">{esc(mxid)}</button>"
        for mxid in users
    ) or "<div class='muted'>暂无可选用户。</div>"
    pack_cards = []
    for item in packs:
        pack = item["pack"]
        stickers = item["stickers"]
        source_type = str(pack["source_type"] or "").strip() or "manual"
        source_label = {
            "manual": "手动维护",
            "telegram": "Telegram 导入",
            "dimension": "历史迁移",
        }.get(source_type, source_type)
        source_ref = pack["source_ref"] or ""
        animated_count = int(pack["animated_count"] or 0)
        sticker_rows = []
        for sticker in stickers:
            animated = "动态" if str(sticker["mimetype"]).startswith("video/") or sticker["mimetype"] in ("image/gif", "image/webp") else "静态"
            sticker_rows.append(
                f"""<tr>
  <td>
    <div class="sticker-mini">
      <div class="sticker-mini__preview">
        {"<video src='" + esc(sticker_preview_url(sticker["id"])) + "' muted loop autoplay playsinline></video>" if str(sticker["mimetype"]).startswith("video/") else "<img src='" + esc(sticker_preview_url(sticker["id"])) + "' alt='" + esc(sticker["name"]) + "'>"}
      </div>
      <div>
        <strong>#{esc(sticker['id'])} {esc(sticker['name'])}</strong><br>
        <span class="muted">{esc(animated)} · {esc(sticker['mimetype'])} · {esc(sticker['width'])}x{esc(sticker['height'])}</span>
      </div>
    </div>
  </td>
  <td class="mono long-text">{esc(sticker['image_mxc'])}</td>
  <td class="actions-cell">
    <form method="post" action="/stickers" onsubmit="return confirm('确认删除这个贴纸？')">
      <input type="hidden" name="action" value="delete_sticker">
      <input type="hidden" name="sticker_id" value="{esc(sticker['id'])}">
      <button class="danger">删除</button>
    </form>
  </td>
</tr>"""
            )
        sticker_table = (
            "<table class=\"sticker-table\"><thead><tr><th>贴纸</th><th>MXC</th><th>操作</th></tr></thead><tbody>"
            + ("".join(sticker_rows) or "<tr><td colspan='3' class='muted'>这个包还没有贴纸。</td></tr>")
            + "</tbody></table>"
        )
        pack_cards.append(
            f"""
<details class="sticker-pack-card">
  <summary>
    <div class="sticker-pack-summary">
      <div>
        <div class="sticker-pack-title">#{esc(pack['id'])} {esc(pack['title'])}</div>
        <div class="muted">共 {len(stickers)} 个贴纸，动态 {animated_count} 个</div>
      </div>
      <div class="row pack-badges">
        <span class="inline-badge">{esc(source_label)}</span>
        <span class="inline-badge {'ok' if pack['is_enabled'] else 'warn'}">{'启用' if pack['is_enabled'] else '停用'}</span>
        <span class="inline-badge {'info' if pack['is_public'] else 'warn'}">{'公开' if pack['is_public'] else '私有'}</span>
      </div>
    </div>
  </summary>
  <div class="sticker-pack-body">
    <div class="pack-overview-grid">
      <div class="overview-card">
        <div class="overview-label">包标识</div>
        <div class="mono">{esc(pack['slug'])}</div>
      </div>
      <div class="overview-card">
        <div class="overview-label">来源引用</div>
        <div class="mono long-text">{esc(source_ref or '手动创建')}</div>
      </div>
      <div class="overview-card">
        <div class="overview-label">公开 JSON</div>
        <div class="mono long-text">{esc(STICKER_WIDGET_PUBLIC_URL.rstrip('/') + '/packs/' + pack['slug'] + '.json')}</div>
      </div>
      <div class="overview-card">
        <div class="overview-label">作者 / 许可</div>
        <div>{esc(pack['author_name'] or '未填写')}<span class="muted"> · {esc(pack['license'] or 'Custom')}</span></div>
      </div>
    </div>
    <details class="sticker-items-toggle">
      <summary>
        <div class="section-head compact-head">
          <h3>这组贴纸</h3>
          <span class="muted">默认收起，按需展开查看这一组的图片内容。</span>
        </div>
      </summary>
      {sticker_table}
    </details>
    <div class="pack-ops-grid">
      <form method="post" action="/stickers" class="panel-block">
        <h3>包设置 / 改名</h3>
        <input type="hidden" name="action" value="update_pack">
        <input type="hidden" name="pack_id" value="{esc(pack['id'])}">
        <label>包名</label><input name="name" value="{esc(pack['title'])}" required>
        <label>描述</label><input name="description" value="{esc(pack['description'])}">
        <div class="grid2 compact-form">
          <div><label>启用</label><select name="isEnabled"><option value="1" {'selected' if pack['is_enabled'] else ''}>启用</option><option value="0" {'selected' if not pack['is_enabled'] else ''}>停用</option></select></div>
          <div><label>公开</label><select name="isPublic"><option value="1" {'selected' if pack['is_public'] else ''}>公开</option><option value="0" {'selected' if not pack['is_public'] else ''}>私有</option></select></div>
          <div><label>作者名</label><input name="authorName" value="{esc(pack['author_name'] or '')}"></div>
          <div><label>作者链接</label><input name="authorReference" value="{esc(pack['author_reference'] or '')}"></div>
          <div><label>许可证</label><input name="license" value="{esc(pack['license'] or 'Custom')}"></div>
        </div>
        <button>保存包设置</button>
      </form>
      <form method="post" action="/stickers" enctype="multipart/form-data" class="panel-block">
        <h3>给这组添加贴纸</h3>
        <input type="hidden" name="action" value="add_sticker">
        <input type="hidden" name="pack_id" value="{esc(pack['id'])}">
        <label>贴纸名称</label><input name="name" placeholder="单个文件可自定义；批量上传默认用文件名">
        <label>描述</label><input name="description">
        <label>贴纸文件</label><input name="file" type="file" multiple>
        <label>或选择整个文件夹</label><input name="file" type="file" webkitdirectory directory multiple>
        <div class="grid2 compact-form">
          <div><label>宽度</label><input name="thumbnailWidth" type="number" value="512" min="1" max="4096"></div>
          <div><label>高度</label><input name="thumbnailHeight" type="number" value="512" min="1" max="4096"></div>
        </div>
        <label>可选：MIME 类型</label><input name="mimetype">
        <button>上传到当前包</button>
      </form>
      <form method="post" action="/stickers" onsubmit="return confirm('确认删除整个贴纸包及其中所有贴纸？')" class="panel-block danger-panel">
        <input type="hidden" name="action" value="delete_pack">
        <input type="hidden" name="pack_id" value="{esc(pack['id'])}">
        <h3>删除这组贴纸</h3>
        <p class="muted">会同时删除这个包里的所有贴纸，公开索引会立即移除。</p>
        <button class="danger">删除整包</button>
      </form>
    </div>
  </div>
</details>"""
        )
    error_html = f"<div class='msg'>读取贴纸数据失败：{esc(error)}</div>" if error else ""
    user_error_html = f"<div class='muted'>用户列表读取失败：{esc(user_error)}</div>" if user_error else ""
    return f"""
<section id="stickers" class="app-panel" data-panel="stickers">
  <div class="panel-hero">
    <div>
      <h2>贴纸管理</h2>
      <p class="muted">这里管理的是自托管的 maunium/stickerpicker 贴纸包。视频贴纸走动态预览，不再额外生成静态预览图；下发策略可覆盖全部真实用户、指定用户，或整体关闭。</p>
    </div>
  </div>
  {error_html}
  <div class="sticker-entry-row">
    <div class="stat-card sticker-entry-card">
      <span class="muted">组件入口</span>
      <strong class="mono">{esc(STICKER_WIDGET_PUBLIC_URL)}</strong>
      <span class="muted">客户端统一读取这个地址</span>
    </div>
  </div>
  <div class="stats-grid">
    <div class="stat-card"><span class="muted">贴纸包总数</span><strong>{len(packs)}</strong><span class="muted">启用 {len(enabled_packs)} / 公开 {len(public_packs)}</span></div>
    <div class="stat-card"><span class="muted">贴纸总数</span><strong>{total_stickers}</strong><span class="muted">公开索引自动生成</span></div>
    <div class="stat-card"><span class="muted">当前下发目标</span><strong>{len(target_users)}</strong><span class="muted">模式：{'全部真实用户' if cfg['delivery_mode']=='all_users' else '指定用户' if cfg['delivery_mode']=='selected_users' else '关闭'}</span></div>
  </div>
  <div class="stickers-stack">
    <form method="post" action="/stickers" class="panel-block" id="sticker-delivery">
      <h3>下发策略</h3>
      <p class="muted">控制哪些真实用户会自动拿到 `stickerpicker` 组件，新用户注册后也会按当前策略自动补齐。</p>
      <input type="hidden" name="action" value="save_delivery">
      <label>贴纸功能开关</label>
      <select name="delivery_mode" id="sticker-delivery-mode">
        <option value="all_users" {'selected' if cfg['delivery_mode']=='all_users' else ''}>给所有真实用户开启</option>
        <option value="selected_users" {'selected' if cfg['delivery_mode']=='selected_users' else ''}>只给指定用户开启</option>
        <option value="disabled" {'selected' if cfg['delivery_mode']=='disabled' else ''}>关闭贴纸组件下发</option>
      </select>
      <label>指定用户列表</label>
      <textarea id="sticker-target-users" name="target_users" placeholder="@user:example.com，每行一个">{esc(chr(10).join(displayed_selected_users))}</textarea>
      <div>
        <label>从现有真实用户中选择</label>
        <div id="sticker-user-picker" class="chip-box" data-users="{esc(json.dumps(users, ensure_ascii=False))}">
          {user_chips}
        </div>
        <div id="sticker-delivery-hint" class="muted">{esc(selected_hint)}</div>
        {user_error_html}
      </div>
      <div class="row">
        <button name="sync_now" value="true">保存并立即同步</button>
      </div>
    </form>
    <div class="panel-stack" id="sticker-imports">
      <form method="post" action="/stickers" class="panel-block">
        <h3>导入中心</h3>
        <p class="muted">支持 Telegram 贴纸包链接或短名导入。当前可导入图片包和视频贴纸包；TGS/Lottie 动画贴纸暂不支持。</p>
        <input type="hidden" name="action" value="import_telegram_pack">
        <label>Telegram 贴纸包链接或短名</label><input name="telegram_pack_ref" placeholder="例如 https://t.me/addstickers/xxxx 或 xxxxx" required>
        <div class="grid2 compact-form">
          <div><label>导入后的包名</label><input name="name" placeholder="留空则使用 Telegram 原包名"></div>
          <div><label>描述</label><input name="description" placeholder="留空则使用 Telegram 原标题"></div>
          <div><label>启用</label><select name="isEnabled"><option value="1">启用</option><option value="0">停用</option></select></div>
          <div><label>公开</label><select name="isPublic"><option value="1">公开</option><option value="0">私有</option></select></div>
          <div><label>作者名</label><input name="authorName" placeholder="默认 Telegram"></div>
          <div><label>作者链接</label><input name="authorReference" placeholder="默认回填 t.me/addstickers 链接"></div>
          <div><label>许可证</label><input name="license" value="Telegram Imported"></div>
        </div>
        <button>开始导入 Telegram 贴纸包</button>
      </form>
      <form method="post" action="/stickers" class="panel-block">
        <h3>新建贴纸包</h3>
        <p class="muted">手动维护的包适合自定义上传、分组管理和后续单个增删。</p>
        <input type="hidden" name="action" value="create_pack">
        <label>包名</label><input name="name" required>
        <label>描述</label><input name="description">
        <div class="grid2 compact-form">
          <div><label>启用</label><select name="isEnabled"><option value="1">启用</option><option value="0">停用</option></select></div>
          <div><label>公开</label><select name="isPublic"><option value="1">公开</option><option value="0">私有</option></select></div>
          <div><label>作者名</label><input name="authorName"></div>
          <div><label>作者链接</label><input name="authorReference"></div>
          <div><label>许可证</label><input name="license" value="Custom"></div>
        </div>
        <button>创建贴纸包</button>
      </form>
    </div>
  </div>
  <div class="stickers-stack stickers-top-grid">
    <form method="post" action="/stickers" enctype="multipart/form-data" class="panel-block" id="sticker-upload">
      <h3>快速上传贴纸</h3>
      <input type="hidden" name="action" value="add_sticker">
      <label>目标贴纸包</label><select name="pack_id" required>{pack_options}</select>
        <label>贴纸名称</label><input name="name" placeholder="单个文件可自定义；批量上传默认用文件名">
        <label>描述</label><input name="description">
      <label>贴纸文件，支持图片/动图/视频</label><input name="file" type="file" multiple>
      <label>或选择整个文件夹</label><input name="file" type="file" webkitdirectory directory multiple>
      <div class="grid2 compact-form">
        <div><label>宽度</label><input name="thumbnailWidth" type="number" value="512" min="1" max="4096"></div>
        <div><label>高度</label><input name="thumbnailHeight" type="number" value="512" min="1" max="4096"></div>
      </div>
      <label>可选：MIME 类型。留空按文件名自动识别。</label><input name="mimetype">
      <button>上传并加入包</button>
    </form>
    <div class="panel-block">
      <h3>同步说明</h3>
      <div class="policy-box slim">
        <ul>
          <li>新用户会按当前下发策略自动补齐账号级贴纸组件。</li>
          <li>关闭某个贴纸包后，会自动从公开索引移除，但不会删除已上传媒体。</li>
          <li>切换到“关闭贴纸组件下发”后，会把所有真实用户账号里的 `stickerpicker` 组件移除。</li>
          <li>今后新系统部署时，只要配置 `STICKER_WIDGET_PUBLIC_URL` 和 Synapse 管理凭据即可复用，不依赖旧集成管理器。</li>
        </ul>
      </div>
    </div>
  </div>
  <section class="pack-list-panel" id="sticker-catalog">
    <div class="section-head">
      <h3>贴纸包列表</h3>
      <span class="muted">按包分组管理，来源、状态、上传入口和删除入口都在包内聚合。</span>
    </div>
    {''.join(pack_cards) or "<div class='muted'>还没有贴纸包。先在上方创建一个贴纸包。</div>"}
  </section>
</section>"""


def layout(body, message=""):
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{APP_TITLE}</title>
  <style>
    :root {{ color-scheme: light; --bg:#f3f5f9; --panel:#fff; --line:#d8dee8; --text:#172033; --muted:#647084; --accent:#0f766e; --accent-2:#2563eb; --danger:#b42318; --soft:#eef7f6; --soft-blue:#eef4ff; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:var(--bg); color:var(--text); }}
    header {{ padding:22px 28px; background:#0f172a; color:#fff; }}
    header h1 {{ margin:0; font-size:22px; letter-spacing:0; }}
    main {{ max-width:1320px; margin:0 auto; padding:24px; }}
    .app-shell {{ display:grid; grid-template-columns:260px minmax(0, 1fr); gap:18px; align-items:start; }}
    .app-sidebar {{ position:sticky; top:24px; display:grid; gap:14px; }}
    .sidebar-panel {{ background:#0f172a; color:#e5eefc; border-radius:10px; border:1px solid #1e293b; padding:16px; }}
    .sidebar-panel h2 {{ margin:0 0 8px; font-size:16px; color:#fff; }}
    .sidebar-panel p {{ margin:0; color:#94a3b8; font-size:13px; line-height:1.5; }}
    .sidebar-nav {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px; display:grid; gap:6px; }}
    .sidebar-nav button {{ display:block; width:100%; text-align:left; text-decoration:none; color:var(--text); border-radius:8px; padding:10px 12px; border:1px solid transparent; background:transparent; cursor:pointer; }}
    .sidebar-nav button strong {{ display:block; font-size:14px; }}
    .sidebar-nav button span {{ display:block; margin-top:4px; color:var(--muted); font-size:12px; }}
    .sidebar-nav button:hover {{ background:#f8fafc; border-color:#dbe5f0; }}
    .sidebar-nav button.active {{ background:#eef4ff; border-color:#bfdbfe; }}
    .content-stack {{ display:grid; gap:18px; }}
    .app-panel {{ display:none; }}
    .app-panel.active {{ display:block; }}
    section {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:18px; }}
    h2 {{ margin:0 0 14px; font-size:22px; }}
    h3 {{ margin:20px 0 10px; font-size:15px; }}
    label {{ display:block; font-size:13px; color:var(--muted); margin-bottom:6px; }}
    input, textarea, select {{ width:100%; padding:10px 11px; border:1px solid var(--line); border-radius:6px; font:inherit; background:#fff; }}
    textarea {{ min-height:190px; resize:vertical; }}
    #notice-exclude {{ min-height:110px; }}
    .grid {{ display:grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap:12px; }}
    .grid2 {{ display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:12px; }}
    .row {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
    .quick-pick {{ margin:14px 0 10px; }}
    .chip-box {{ display:flex; gap:8px; flex-wrap:wrap; max-height:260px; overflow:auto; border:1px solid var(--line); border-radius:6px; padding:10px; background:#fff; }}
    button.chip {{ border:1px solid var(--line); background:#f8fafc; color:var(--text); border-radius:999px; padding:7px 10px; font-size:13px; font-weight:600; }}
    button.chip:hover {{ border-color:var(--accent); color:var(--accent); }}
    button.chip.selected {{ border-color:var(--accent); background:#e6fffb; color:#0f766e; }}
    .chip-box.is-disabled button.chip {{ opacity:.48; cursor:not-allowed; border-color:var(--line); background:#f3f4f6; color:var(--muted); }}
    textarea:disabled {{ background:#f3f4f6; color:var(--muted); cursor:not-allowed; }}
    .check {{ display:flex; align-items:center; gap:8px; color:var(--text); }}
    .check input {{ width:auto; }}
    button {{ border:0; border-radius:6px; padding:10px 14px; font-weight:700; cursor:pointer; background:var(--accent); color:#fff; }}
    button.secondary {{ background:#334155; }}
    button.danger {{ background:var(--danger); }}
    .msg {{ padding:11px 13px; border:1px solid #a7d8c9; background:#ecfdf5; border-radius:6px; }}
    .msg strong {{ display:block; margin-bottom:3px; }}
    .policy-box {{ border:1px solid #bfdbfe; background:#eff6ff; border-radius:8px; padding:12px 14px; margin:12px 0 14px; font-size:14px; }}
    .policy-box ul {{ margin:8px 0 0; padding-left:20px; }}
    .policy-box li {{ margin:4px 0; }}
    .muted {{ color:var(--muted); font-size:13px; }}
    .stats-grid {{ display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:12px; margin:14px 0; }}
    .stat-card {{ border:1px solid var(--line); border-radius:8px; padding:14px; background:#fbfcfd; display:grid; gap:8px; min-width:0; }}
    .stat-card strong {{ font-size:20px; overflow-wrap:anywhere; }}
    .sticker-entry-row {{ margin:14px 0 0; }}
    .sticker-entry-card {{ padding:12px 14px; gap:6px; background:#fcfdff; }}
    .sticker-entry-card strong {{ font-size:14px; line-height:1.35; font-weight:700; color:#334155; }}
    .panel-block {{ border:1px solid var(--line); border-radius:10px; padding:16px; background:#fbfcfd; box-shadow:0 1px 0 rgba(15,23,42,0.03); }}
    .panel-block > h3 {{ margin:0 0 12px; font-size:17px; color:var(--text); font-weight:800; padding-left:10px; border-left:3px solid #cbd5e1; }}
    .panel-block > .muted:first-of-type {{ font-size:14px; line-height:1.6; margin-bottom:14px; }}
    .panel-stack {{ display:grid; gap:12px; }}
    .subpanel {{ border:1px solid var(--line); border-radius:8px; padding:14px; background:#fff; }}
    .danger-panel {{ background:#fff7f7; }}
    .slim {{ margin-bottom:0; }}
    .pack-head {{ display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:12px; margin:10px 0 14px; padding:10px 12px; border:1px solid var(--line); border-radius:8px; background:#f8fafc; }}
    .section-head {{ display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:10px; flex-wrap:wrap; }}
    .stickers-top-grid {{ align-items:start; }}
    .stickers-stack {{ display:grid; grid-template-columns:1fr; gap:12px; margin-top:12px; }}
    .sticker-mini {{ display:flex; gap:12px; align-items:flex-start; }}
    .sticker-mini__preview {{ width:60px; height:60px; border-radius:6px; overflow:hidden; background:#e5e7eb; flex:0 0 60px; display:flex; align-items:center; justify-content:center; }}
    .sticker-mini__preview img, .sticker-mini__preview video {{ width:100%; height:100%; object-fit:cover; display:block; }}
    .actions-cell form {{ display:flex; justify-content:flex-start; }}
    .pack-list-panel {{ padding-top:12px; }}
    .panel-hero {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-start; flex-wrap:wrap; margin-bottom:8px; }}
    .sticker-pack-card {{ margin-top:14px; border-radius:8px; padding:0; overflow:hidden; background:#fff; }}
    .sticker-pack-card summary {{ list-style:none; padding:0; }}
    .sticker-pack-card summary::-webkit-details-marker {{ display:none; }}
    .sticker-pack-summary {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; padding:16px; background:#fcfdff; }}
    .sticker-pack-title {{ font-size:15px; font-weight:800; }}
    .sticker-pack-body {{ padding:0 16px 16px; border-top:1px solid var(--line); }}
    .pack-overview-grid {{ display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:12px; margin:14px 0; }}
    .overview-card {{ border:1px solid var(--line); border-radius:8px; padding:12px; background:#f8fafc; display:grid; gap:6px; }}
    .overview-label {{ color:var(--muted); font-size:12px; font-weight:700; }}
    .pack-ops-grid {{ display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:12px; margin-top:14px; align-items:start; }}
    .sticker-items-toggle {{ margin-top:14px; padding:0; }}
    .sticker-items-toggle > summary {{ padding:14px 16px; background:#f8fafc; }}
    .sticker-items-toggle[open] > summary {{ border-bottom:1px solid var(--line); }}
    .compact-head {{ margin-top:16px; }}
    .pack-badges {{ justify-content:flex-end; }}
    .inline-badge {{ display:inline-flex; align-items:center; border-radius:999px; padding:4px 10px; font-size:12px; font-weight:700; border:1px solid var(--line); background:#f8fafc; color:var(--text); }}
    .inline-badge.ok {{ background:#dcfce7; color:#166534; border-color:#bbf7d0; }}
    .inline-badge.info {{ background:#dbeafe; color:#1d4ed8; border-color:#bfdbfe; }}
    .inline-badge.warn {{ background:#fff7ed; color:#c2410c; border-color:#fed7aa; }}
    .badge {{ display:inline-block; min-width:68px; text-align:center; border-radius:999px; padding:3px 8px; font-size:12px; font-weight:700; }}
    .badge.ok {{ background:#dcfce7; color:#166534; }}
    .badge.bad {{ background:#fee2e2; color:#991b1b; }}
    .badge.run {{ background:#e0f2fe; color:#075985; }}
    .job-detail {{ display:grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap:10px; font-size:13px; min-width:0; }}
    .job-detail > *, .long-text, details, summary {{ min-width:0; overflow-wrap:anywhere; word-break:break-word; }}
    .job-detail .wide {{ grid-column: 1 / -1; }}
    .compact-list {{ margin:8px 0 0; padding-left:18px; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    details {{ border:1px solid var(--line); border-radius:6px; padding:8px 10px; background:#fbfcfe; }}
    summary {{ cursor:pointer; font-weight:700; }}
    pre {{ overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; word-break:break-word; background:#0f172a; color:#dbeafe; padding:14px; border-radius:8px; max-height:320px; }}
    table {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
    td, th {{ text-align:left; border-bottom:1px solid var(--line); padding:8px; vertical-align:top; }}
    .sticker-pack {{ margin-top:14px; }}
    .sticker-table th:nth-child(1) {{ width:24%; }}
    .sticker-table th:nth-child(2) {{ width:56%; }}
    .sticker-table th:nth-child(3) {{ width:20%; }}
    .compact-form {{ margin:10px 0; }}
    .sync-box {{ margin:14px 0; border:1px solid var(--line); border-radius:8px; padding:12px; background:#fbfcfe; }}
    @media (max-width: 1080px) {{ .app-shell {{ grid-template-columns:1fr; }} .app-sidebar {{ position:static; }} .pack-ops-grid {{ grid-template-columns:1fr; }} }}
    @media (max-width: 860px) {{ .grid, .grid2, .job-detail, .stats-grid, .pack-head, .pack-overview-grid {{ grid-template-columns: 1fr; }} .sticker-pack-summary {{ flex-direction:column; }} .pack-badges {{ justify-content:flex-start; }} main {{ padding:14px; }} }}
  </style>
</head>
<body>
<header><h1>{APP_TITLE}</h1><div class="muted">媒体清理、Server Notice 和自托管贴纸管理共用这套受保护运维入口</div></header>
<main id="top">
{f'<div class="msg">{esc(message)}</div>' if message else ''}
<div class="app-shell">
  <aside class="app-sidebar">
    <div class="sidebar-panel">
      <h2>运维分类</h2>
      <p>把高频操作收进固定分类，左侧只负责定位，右侧只负责处理内容。</p>
    </div>
    <nav class="sidebar-nav" aria-label="运维面板分类">
      <button type="button" class="panel-tab active" data-panel="notice"><strong>发送通知</strong><span>Server Notice 下发、单用户测试、全站发送</span></button>
      <button type="button" class="panel-tab" data-panel="purge"><strong>附件管理</strong><span>附件大小策略、保留周期、立即清理</span></button>
      <button type="button" class="panel-tab" data-panel="stickers"><strong>贴纸管理</strong><span>贴纸下发、导入、上传、按包维护</span></button>
      <button type="button" class="panel-tab" data-panel="jobs"><strong>最近任务</strong><span>查看最近执行结果和错误摘要</span></button>
    </nav>
  </aside>
  <div class="content-stack">
    {body}
  </div>
</div>
</main>
<script>
(() => {{
  const picker = document.getElementById("notice-user-picker");
  const targets = document.getElementById("notice-single-users");
  const excludeInput = document.getElementById("notice-exclude");
  const mode = document.getElementById("notice-target-mode");
  const form = document.getElementById("notice-form");
  const stickerPicker = document.getElementById("sticker-user-picker");
  const stickerTargets = document.getElementById("sticker-target-users");
  const stickerMode = document.getElementById("sticker-delivery-mode");
  const panelTabs = Array.from(document.querySelectorAll(".panel-tab"));
  const panels = Array.from(document.querySelectorAll(".app-panel"));
  const activatePanel = (panelId) => {{
    panelTabs.forEach((button) => {{
      button.classList.toggle("active", button.dataset.panel === panelId);
    }});
    panels.forEach((panel) => {{
      panel.classList.toggle("active", panel.dataset.panel === panelId);
    }});
    try {{
      localStorage.setItem("matrixOpsActivePanel", panelId);
    }} catch (e) {{}}
  }};
  if (panelTabs.length && panels.length) {{
    let preferredPanel = "notice";
    try {{
      preferredPanel = localStorage.getItem("matrixOpsActivePanel") || preferredPanel;
    }} catch (e) {{}}
    if (!panels.some((panel) => panel.dataset.panel === preferredPanel)) {{
      preferredPanel = "notice";
    }}
    panelTabs.forEach((button) => {{
      button.addEventListener("click", () => activatePanel(button.dataset.panel));
    }});
    activatePanel(preferredPanel);
  }}
  if (picker && targets) {{
    const allUsers = JSON.parse(picker.dataset.users || "[]");
    const targetList = () => targets.value.split(/[\\n,]/).map((item) => item.trim()).filter(Boolean);
    const excludeList = () => excludeInput
      ? excludeInput.value.split(/[\\n,]/).map((item) => item.trim()).filter(Boolean)
      : [];
    const deliverableUsers = () => {{
      const excluded = new Set(excludeList());
      return allUsers.filter((mxid) => !excluded.has(mxid));
    }};
    const writeTargets = (items) => {{
      const excluded = new Set(excludeList());
      targets.value = Array.from(new Set(items)).filter((mxid) => !excluded.has(mxid)).join("\\n");
      document.querySelectorAll(".notice-user-chip").forEach((button) => {{
        button.classList.toggle("selected", targetList().includes(button.dataset.mxid));
      }});
    }};
    document.querySelectorAll(".notice-user-chip").forEach((button) => {{
      button.addEventListener("click", () => {{
        const mxid = button.dataset.mxid.trim();
        const current = targetList();
        if (current.includes(mxid)) {{
          writeTargets(current.filter((item) => item !== mxid));
        }} else {{
          writeTargets(current.concat(mxid));
        }}
        if (mode) {{
          mode.value = "single";
        }}
      }});
    }});
    targets.addEventListener("input", () => writeTargets(targetList()));
    if (mode) {{
      mode.addEventListener("change", () => {{
        if (mode.value === "all") {{
          writeTargets(deliverableUsers());
        }} else {{
          writeTargets([]);
        }}
      }});
    }}
    if (excludeInput) {{
      excludeInput.addEventListener("input", () => {{
        if (mode && mode.value === "all") {{
          writeTargets(deliverableUsers());
        }} else {{
          writeTargets(targetList());
        }}
      }});
    }}
    writeTargets(targetList());
  }}
  if (form && mode && targets) {{
    form.addEventListener("submit", (event) => {{
      const isAll = mode.value === "all";
      const selectedTargets = targets.value.split(/[\\n,]/).map((item) => item.trim()).filter(Boolean);
      const message = isAll
        ? "确认发送 Server Notice？全站发送会通知所有未排除的本地用户。"
        : `确认发送 Server Notice？本次只会发送给 ${{selectedTargets.length || 0}} 个指定用户。`;
      if (!window.confirm(message)) {{
        event.preventDefault();
      }}
    }});
  }}
  if (stickerPicker && stickerTargets) {{
    const stickerHint = document.getElementById("sticker-delivery-hint");
    const allStickerUsers = () => {{
      try {{
        return JSON.parse(stickerPicker.dataset.users || "[]");
      }} catch (_error) {{
        return [];
      }}
    }};
    const targetList = () => stickerTargets.value.split(/[\\n,]/).map((item) => item.trim()).filter(Boolean);
    const writeTargets = (items) => {{
      stickerTargets.value = Array.from(new Set(items)).join("\\n");
      document.querySelectorAll(".sticker-user-chip").forEach((button) => {{
        button.classList.toggle("selected", targetList().includes(button.dataset.mxid));
      }});
    }};
    const syncStickerModeView = () => {{
      if (!stickerMode) {{
        writeTargets(targetList());
        return;
      }}
      if (stickerMode.value === "all_users") {{
        // 全员模式下直接把真实用户列表写入页面展示，全绿才符合“实际会全部下发”的管理直觉。
        writeTargets(allStickerUsers());
        stickerTargets.disabled = false;
        stickerPicker.classList.remove("is-disabled");
        if (stickerHint) {{
          stickerHint.textContent = "当前是全员开启模式，下面绿色用户都会拿到贴纸组件；新用户注册后也会自动补齐。";
        }}
      }} else if (stickerMode.value === "disabled") {{
        // 关闭模式必须明确展示“无人下发”，同时禁用手动选择，避免看起来还保留旧名单。
        writeTargets([]);
        stickerTargets.disabled = true;
        stickerPicker.classList.add("is-disabled");
        if (stickerHint) {{
          stickerHint.textContent = "当前已关闭贴纸组件下发，保存同步后会回收所有真实用户账号里的贴纸入口。";
        }}
      }} else {{
        stickerTargets.disabled = false;
        stickerPicker.classList.remove("is-disabled");
        writeTargets(targetList());
        if (stickerHint) {{
          stickerHint.textContent = "点一下加入，再点一下移除。切到“只给指定用户开启”时更适合使用这一栏。";
        }}
      }}
    }};
    document.querySelectorAll(".sticker-user-chip").forEach((button) => {{
      button.addEventListener("click", () => {{
        if (stickerMode && stickerMode.value === "disabled") {{
          return;
        }}
        const mxid = button.dataset.mxid.trim();
        const current = targetList();
        if (current.includes(mxid)) {{
          writeTargets(current.filter((item) => item !== mxid));
        }} else {{
          writeTargets(current.concat(mxid));
        }}
        if (stickerMode) {{
          stickerMode.value = "selected_users";
        }}
      }});
    }});
    stickerTargets.addEventListener("input", () => {{
      if (stickerMode && stickerMode.value !== "selected_users") {{
        stickerMode.value = "selected_users";
      }}
      writeTargets(targetList());
    }});
    if (stickerMode) {{
      stickerMode.addEventListener("change", syncStickerModeView);
    }}
    syncStickerModeView();
  }}
}})();
</script>
</body>
</html>"""


def render_home(message=""):
    cfg = get_config()
    notice_users = []
    notice_user_error = ""
    try:
        notice_users = list_users()
    except Exception as e:
        # 用户列表只是单用户选择辅助；不能因为 Admin API 短暂失败阻断媒体清理等其它面板功能。
        notice_user_error = str(e)
    notice_excludes = suggested_notice_excludes(notice_users)
    notice_selectable_users = [mxid for mxid in notice_users if mxid not in set(notice_excludes)]
    jobs = recent_jobs()
    rows = []
    for row in jobs:
        finished = row["finished_at"] or ""
        rows.append(
            f"<tr><td>{esc(row['created_at'])}<br><span class='muted'>{esc(finished)}</span></td>"
            f"<td>{esc(row['kind'])}</td>"
            f"<td><span class='badge {status_class(row['status'])}'>{esc(row['status'])}</span></td>"
            f"<td>{esc(row['summary'] or '')}</td></tr>"
            f"<tr><td colspan='4'>{render_job_detail(row)}</td></tr>"
        )
    body = f"""
<section id="notice" class="app-panel active" data-panel="notice">
  <div class="panel-hero">
    <div>
      <h2>发送通知</h2>
      <p class="muted">用于发送 Server Notice。支持先单用户测试，再切换到全站本地用户发送。</p>
    </div>
  </div>
  <form id="notice-form" method="post" action="/notice">
    <div class="grid2">
      <div>
        <label>发送范围</label>
        <select id="notice-target-mode" name="target_mode">
          <option value="single">单个用户</option>
          <option value="all">全站本地用户</option>
        </select>
      </div>
      {render_notice_user_picker(notice_selectable_users, notice_user_error)}
    </div>
    <p><label>指定用户帐号，每行一个或用逗号分隔</label><textarea id="notice-single-users" name="single_users"></textarea></p>
    {render_notice_exclude_summary(notice_excludes)}
    <p><label>排除账号，每行一个或用逗号分隔</label><textarea id="notice-exclude" name="exclude">{esc(chr(10).join(notice_excludes))}</textarea></p>
    <p><label>通知正文</label><textarea name="body" placeholder="在这里写要发送的通知内容。建议先选择单个用户测试。">{esc(DEFAULT_NOTICE_BODY)}</textarea></p>
    <button class="danger">发送通知</button>
  </form>
</section>
<section id="purge" class="app-panel" data-panel="purge">
  <div class="panel-hero">
    <div>
      <h2>附件管理</h2>
      <p class="muted">管理附件大小和保留周期。保存只更新策略，红色按钮才会触发真实清理。</p>
    </div>
  </div>
  <h2>媒体清理</h2>
  <p class="muted">这块只有红色按钮会立刻删除媒体；保存参数不会删除。每日自动任务只是到点检查，命中下面规则才删除。</p>
  {render_policy_summary(cfg)}
  <form method="post" action="/purge">
    <div class="grid">
      <div><label>不清理上限 MiB</label><input name="medium_size_mib" type="number" value="{mib(cfg['medium_size_gt'])}" min="1"></div>
      <div><label>常规清理天数</label><input name="medium_days" type="number" value="{esc(cfg['medium_days'])}" min="1"></div>
      <div><label>紧急清理阈值 MiB</label><input name="large_size_mib" type="number" value="{mib(cfg['large_size_gt'])}" min="1"></div>
      <div><label>紧急清理天数</label><input name="large_days" type="number" value="{esc(cfg['large_days'])}" min="1"></div>
      <div><label>远端缓存保留天数</label><input name="remote_days" type="number" value="{esc(cfg['remote_days'])}" min="1"></div>
      <div><label>每天检查时间 HHMM</label><input name="schedule_hhmm" value="{esc(cfg['schedule_hhmm'])}" pattern="[0-9]{{4}}"></div>
    </div>
    <p class="row">
      <label class="check"><input type="checkbox" name="keep_profiles" value="true" {'checked' if cfg['keep_profiles'] == 'true' else ''}> 保留头像和资料引用媒体</label>
      <span class="muted">每日检查固定启用：到上面的时间自动跑一次，达到条件才删除。</span>
    </p>
    <p class="muted">保存参数：只更新上面的规则，不删除媒体。立即执行一次：现在按当前规则真实清理。</p>
    <button name="action" value="save" class="secondary">保存参数</button>
    <button name="action" value="execute" class="danger" onclick="return confirm('确认立即执行真实媒体删除？')">立即执行一次</button>
  </form>
</section>
{render_sticker_manager()}
<section id="jobs" class="app-panel" data-panel="jobs">
  <h2>最近任务</h2>
  <table><thead><tr><th>创建/完成时间</th><th>类型</th><th>状态</th><th>摘要</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</section>
"""
    return layout(body, message)


class Handler(BaseHTTPRequestHandler):
    def path_only(self):
        return urllib.parse.urlsplit(self.path).path

    def require_auth(self):
        # 老内网机直接绑定管理端口时必须启用应用内 Basic Auth；公网 VPS 仍依赖 Caddy Basic Auth。
        if check_basic_auth(self.headers.get("Authorization", "")):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Matrix Ops"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return False

    def send_bytes(self, data, content_type, status=200, cache_control="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_public_stickerpicker_get(self):
        path = self.path_only()
        relative = path[len(STICKER_ROUTE_PREFIX):] or "/"
        if relative in ("", "/"):
            relative = "/index.html"
        # 公开贴纸资源优先按官方静态目录结构提供，减少运行时动态拼 JSON / 回源媒体的环节。
        static_target = sticker_static_file(relative.lstrip("/"))
        if static_target:
            with open(static_target, "rb") as f:
                payload = f.read()
            guessed = stable_mimetype_for_filename(static_target)
            cache_control = "public, max-age=3600" if not static_target.endswith("index.html") else "no-store"
            self.send_bytes(payload, guessed, cache_control=cache_control)
            return True
        # 下面这层动态分支只给旧缓存链接和导出缺口兜底，避免迁移期间客户端直接白屏。
        if relative == "/packs/index.json":
            payload = json.dumps(sticker_index_payload(), ensure_ascii=False).encode("utf-8")
            self.send_bytes(payload, "application/json; charset=utf-8", cache_control="public, max-age=30")
            return True
        if relative.startswith("/packs/") and relative.endswith(".json"):
            slug = relative.rsplit("/", 1)[-1][:-5]
            pack, stickers = read_pack_by_slug(slug)
            if not pack:
                self.send_response(404)
                self.end_headers()
                return True
            payload = json.dumps(build_pack_json(pack, stickers), ensure_ascii=False).encode("utf-8")
            self.send_bytes(payload, "application/json; charset=utf-8", cache_control="public, max-age=30")
            return True
        if relative.startswith("/media/"):
            try:
                sticker_id = int(relative.rsplit("/", 1)[-1])
            except Exception:
                self.send_response(404)
                self.end_headers()
                return True
            conn = db()
            sticker = conn.execute("select * from stickers where id=?", (sticker_id,)).fetchone()
            conn.close()
            if not sticker:
                self.send_response(404)
                self.end_headers()
                return True
            content_type, payload = proxy_matrix_media(sticker["image_mxc"])
            self.send_bytes(payload, content_type, cache_control="public, max-age=3600")
            return True
        self.send_response(404)
        self.end_headers()
        return True

    def do_HEAD(self):
        path = self.path_only()
        if path.startswith(STICKER_ROUTE_PREFIX + "/") or path == STICKER_ROUTE_PREFIX:
            self.send_response(200)
            self.end_headers()
            return
        if not self.require_auth():
            return
        if path not in ("/", "/notice", "/purge", "/stickers"):
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def send_html(self, content, status=200):
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path_only()
        if path.startswith(STICKER_ROUTE_PREFIX + "/") or path == STICKER_ROUTE_PREFIX:
            self.handle_public_stickerpicker_get()
            return
        if not self.require_auth():
            return
        if path == "/notice":
            # GET /notice 是给浏览器直接打开通知页用的；真正发信仍必须走 POST /notice。
            self.send_html(render_home("Server Notice 表单在本页下方，请先单用户测试。"))
            return
        if path == "/purge":
            self.send_html(render_home("媒体清理表单在本页上方；红色按钮才会真实删除媒体。"))
            return
        if path == "/stickers":
            self.send_html(render_home("贴纸管理表单在本页上方。"))
            return
        if path != "/":
            self.send_response(404)
            self.end_headers()
            return
        self.send_html(render_home())

    def do_POST(self):
        if not self.require_auth():
            return
        path = self.path_only()
        form, files = self.read_form()
        try:
            if path == "/purge":
                cfg = normalize_purge_form(form)
                save_config(cfg)
                action = form.get("action", ["save"])[0]
                if action == "dry_run":
                    job_id = create_job("media-purge-dry-run")
                    run_purge_job(job_id, True, cfg)
                    self.send_html(render_home(f"参数检查完成：任务 {job_id} 没有删除媒体。页面上已直接显示当前自动清理规则。"))
                elif action == "execute":
                    job_id = create_job("media-purge-dry-run" if action == "dry_run" else "media-purge-execute")
                    threading.Thread(target=run_purge_job, args=(job_id, False, cfg), daemon=True).start()
                    self.send_html(render_home(f"已启动真实清理任务 {job_id}，完成后会出现在最近任务。"))
                else:
                    self.send_html(render_home("媒体清理参数已保存"))
                return
            if path == "/notice":
                job_id = create_job("server-notice")
                threading.Thread(target=lambda: self.notice_thread(job_id, form), daemon=True).start()
                self.send_html(render_home(f"已启动通知任务 {job_id}"))
                return
            if path == "/stickers":
                self.handle_stickers(form, files)
                return
            self.send_response(404)
            self.end_headers()
        except Exception as e:
            self.send_html(render_home(f"错误：{e}"), 400)

    def read_form(self):
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", "0"))
        if content_type.startswith("multipart/form-data"):
            env = {
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": str(length),
            }
            parsed = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ=env, keep_blank_values=True)
            form = {}
            files = {}
            for key in parsed:
                item = parsed[key]
                if isinstance(item, list):
                    for part in item:
                        self.collect_form_part(form, files, key, part)
                else:
                    self.collect_form_part(form, files, key, item)
            return form, files
        return urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8")), {}

    def collect_form_part(self, form, files, key, item):
        if item.filename:
            data = item.file.read()
            files.setdefault(key, []).append({
                "filename": item.filename,
                "mimetype": item.type,
                "data": data,
            })
        else:
            form.setdefault(key, []).append(item.value)

    def handle_stickers(self, form, files):
        action = form.get("action", [""])[0]
        if action == "create_pack":
            pack_id = create_sticker_pack(form)
            rebuild_stickerpicker_static_assets()
            sync_sticker_widgets(restart_synapse=False)
            self.send_html(render_home(f"已创建贴纸包 #{pack_id}"))
            return
        if action == "update_pack":
            pack_id = update_sticker_pack(form)
            rebuild_stickerpicker_static_assets()
            sync_sticker_widgets(restart_synapse=False)
            self.send_html(render_home(f"已保存贴纸包 #{pack_id}"))
            return
        if action == "delete_pack":
            pack_id = int(form.get("pack_id", ["0"])[0])
            delete_sticker_pack(pack_id)
            rebuild_stickerpicker_static_assets()
            sync_sticker_widgets(restart_synapse=False)
            self.send_html(render_home(f"已删除贴纸包 #{pack_id}"))
            return
        if action == "add_sticker":
            result = add_sticker(form, files)
            rebuild_stickerpicker_static_assets()
            sync_sticker_widgets(restart_synapse=False)
            failed = result.get("failed") or []
            failed_text = f"，跳过 {len(failed)} 个失败文件" if failed else ""
            self.send_html(render_home(f"已上传 {result['uploaded']} 个贴纸并加入贴纸包{failed_text}"))
            return
        if action == "import_telegram_pack":
            result = import_telegram_sticker_pack(form)
            rebuild_stickerpicker_static_assets()
            sync_sticker_widgets(restart_synapse=False)
            self.send_html(
                render_home(
                    f"已导入 Telegram 贴纸包 #{result['pack_id']}，短名 {result['short_name']}，成功 {result['imported']} 个，跳过 {result['skipped']} 个"
                )
            )
            return
        if action == "delete_sticker":
            sticker_id = int(form.get("sticker_id", ["0"])[0])
            delete_sticker(sticker_id)
            rebuild_stickerpicker_static_assets()
            self.send_html(render_home(f"已删除单个贴纸 #{sticker_id}"))
            return
        if action == "save_delivery":
            result = save_sticker_delivery_settings(form)
            try:
                sync_result = sync_sticker_widgets(restart_synapse=True)
                self.send_html(render_home(f"已保存下发策略，并同步 {sync_result['users']} 个目标用户"))
            except Exception as e:
                # 同步失败时先把策略保存住，避免用户改完配置却因为 Synapse 或数据库短暂故障而丢设置。
                self.send_html(render_home(f"已保存下发策略，但立即同步失败：{e}"))
            return
        raise ValueError("未知贴纸管理动作")

    def notice_thread(self, job_id, form):
        try:
            send_notice_job(job_id, form)
        except Exception as e:
            finish_job(job_id, "failed", f"notice failed: {e}", str(e))

    def log_message(self, fmt, *args):
        append_log(fmt % args)


def main():
    ensure_dirs()
    save_config({})
    migrate_legacy_sticker_packs_if_needed()
    backfill_legacy_video_thumbnails()
    backfill_video_send_assets()
    # Telegram 静态贴纸若曾被错误记成 application/octet-stream，这里启动时自动回填真实图片 MIME，避免客户端把它当附件。
    backfill_octet_stream_stickers()
    # 启动时总是从数据库完整导出一遍官方 packs 目录，保证前台静态资源与后台状态一致。
    rebuild_stickerpicker_static_assets()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    # 新注册用户会按当前贴纸下发策略自动补齐账号级 m.widgets；关闭模式下也会自动回收旧组件。
    threading.Thread(target=sticker_auto_sync_loop, daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    append_log(f"start {HOST}:{PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()

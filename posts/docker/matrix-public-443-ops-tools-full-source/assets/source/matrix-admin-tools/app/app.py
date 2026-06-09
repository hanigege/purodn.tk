#!/usr/bin/env python3
import datetime as dt
import html
import json
import os
import sqlite3
import subprocess
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
MAX_LOG_BYTES = int(os.environ.get("MAX_LOG_BYTES", str(1024 * 1024)))
MAX_LOG_FILES = int(os.environ.get("MAX_LOG_FILES", "3"))
MAX_JOBS = int(os.environ.get("MAX_JOBS", "200"))
MAX_JOB_OUTPUT = int(os.environ.get("MAX_JOB_OUTPUT", "12000"))
SERVER_NAME = os.environ.get("SERVER_NAME", "jgaga.tk")
PURGE_SCRIPT = os.environ.get("PURGE_SCRIPT", "/opt/matrix-tools/purge-staged-media.sh")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8190"))

DEFAULT_PURGE = {
    "small_keep_size": 5 * 1024 * 1024,
    "medium_days": 90,
    "medium_size_gt": 5 * 1024 * 1024,
    "large_days": 7,
    "large_size_gt": 100 * 1024 * 1024,
    "remote_days": 7,
    "keep_profiles": "true",
    "report_limit": 30,
    "schedule_hhmm": "0423",
}

DEFAULT_NOTICE_BODY = ""

CONFIG_LOCK = threading.Lock()


def ensure_dirs():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
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
    small_keep_mib = parse_int(form, "small_keep_mib", 1, 102400)
    medium_size_mib = parse_int(form, "medium_size_mib", 1, 102400)
    large_size_mib = parse_int(form, "large_size_mib", 1, 102400)
    report_limit = parse_int(form, "report_limit", 1, 500)
    keep_profiles = "true" if form.get("keep_profiles", ["false"])[0] == "true" else "false"
    schedule_hhmm = form.get("schedule_hhmm", ["0423"])[0].strip()
    if len(schedule_hhmm) != 4 or not schedule_hhmm.isdigit():
        raise ValueError("schedule_hhmm 必须是 4 位 HHMM")
    hh = int(schedule_hhmm[:2])
    mm = int(schedule_hhmm[2:])
    if hh > 23 or mm > 59:
        raise ValueError("schedule_hhmm 时间无效")
    return {
        "small_keep_size": small_keep_mib * 1024 * 1024,
        "medium_days": medium_days,
        "medium_size_gt": medium_size_mib * 1024 * 1024,
        "large_days": large_days,
        "large_size_gt": large_size_mib * 1024 * 1024,
        "remote_days": remote_days,
        "keep_profiles": keep_profiles,
        "report_limit": report_limit,
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
            "REPORT_LIMIT": str(config["report_limit"]),
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
    target_mode = form.get("target_mode", ["single"])[0]
    single_user = form.get("single_user", [""])[0].strip()
    body = form.get("body", [""])[0].strip()
    txn_prefix = "web-notice-" + dt.datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8] + "-"
    exclude_raw = form.get("exclude", [""])[0].strip()
    if not body:
        raise ValueError("通知内容不能为空")
    exclude = {f"@server:{SERVER_NAME}", f"@media_purge_admin:{SERVER_NAME}"}
    for item in exclude_raw.replace(",", "\n").splitlines():
        item = item.strip()
        if item:
            exclude.add(item)
    if target_mode == "all":
        targets = [u for u in list_users() if u not in exclude]
    else:
        if not single_user.endswith(f":{SERVER_NAME}"):
            raise ValueError("单用户目标必须是本服 MXID")
        targets = [single_user]
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
        return f"""
<div class="job-detail">
  <div><strong>模式</strong><br>{esc(kind)}</div>
  <div><strong>结果</strong><br>{esc(row['status'])}</div>
  <details class="wide" open><summary>查看执行摘要</summary><pre>{esc('\\n'.join(summary_lines[:12]) or output)}</pre></details>
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
    small_mib = mib(cfg["small_keep_size"])
    return f"""
<div class="policy-box">
  <strong>当前自动清理规则</strong>
  <ul>
    <li>{small_mib} MiB 以内的小文件长期保留。</li>
    <li>超过 {medium_mib} MiB 且超过 {esc(cfg['medium_days'])} 天的本地媒体，会在每日检查或手动执行时删除。</li>
    <li>超过 {large_mib} MiB 且超过 {esc(cfg['large_days'])} 天的本地媒体，会在每日检查或手动执行时删除。</li>
    <li>远端缓存超过 {esc(cfg['remote_days'])} 天会清理；头像和资料引用媒体按下面开关决定是否保留。</li>
    <li>头像包括用户头像、房间头像这类被 Matrix 资料引用的图片；资料引用媒体指用户资料、房间资料等当前资料状态引用的媒体，不是普通聊天附件。</li>
    <li>每天 {esc(cfg['schedule_hhmm'])} 自动检查一次；不是每天全部删除，只删除命中以上条件的媒体。</li>
  </ul>
</div>"""


def layout(body, message=""):
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{APP_TITLE}</title>
  <style>
    :root {{ color-scheme: light; --bg:#f5f7fa; --panel:#fff; --line:#d8dee8; --text:#172033; --muted:#647084; --accent:#116d6e; --danger:#b42318; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:var(--bg); color:var(--text); }}
    header {{ padding:22px 28px; background:#111827; color:#fff; }}
    header h1 {{ margin:0; font-size:22px; letter-spacing:0; }}
    main {{ max-width:1180px; margin:0 auto; padding:24px; display:grid; gap:18px; }}
    section {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:18px; }}
    h2 {{ margin:0 0 14px; font-size:18px; }}
    h3 {{ margin:20px 0 10px; font-size:15px; }}
    label {{ display:block; font-size:13px; color:var(--muted); margin-bottom:6px; }}
    input, textarea, select {{ width:100%; padding:10px 11px; border:1px solid var(--line); border-radius:6px; font:inherit; background:#fff; }}
    textarea {{ min-height:190px; resize:vertical; }}
    .grid {{ display:grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap:12px; }}
    .grid2 {{ display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:12px; }}
    .row {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
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
    th:nth-child(1) {{ width:22%; }}
    th:nth-child(2) {{ width:18%; }}
    th:nth-child(3) {{ width:12%; }}
    th:nth-child(4) {{ width:48%; }}
    @media (max-width: 860px) {{ .grid, .grid2, .job-detail {{ grid-template-columns: 1fr; }} main {{ padding:14px; }} }}
  </style>
</head>
<body>
<header><h1>{APP_TITLE}</h1><div class="muted">媒体清理和 Server Notice 发信都走本机 Synapse Admin API</div></header>
<main id="top">
{f'<div class="msg">{esc(message)}</div>' if message else ''}
{body}
</main>
</body>
</html>"""


def render_home(message=""):
    cfg = get_config()
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
<section>
  <h2>媒体清理</h2>
  <p class="muted">这块只有红色按钮会立刻删除媒体；保存参数不会删除。每日自动任务只是到点检查，命中下面规则才删除。</p>
  {render_policy_summary(cfg)}
  <form method="post" action="/purge">
    <div class="grid">
      <div><label>小文件长期保留 MiB</label><input name="small_keep_mib" type="number" value="{mib(cfg['small_keep_size'])}" min="1"></div>
      <div><label>中等文件保留天数</label><input name="medium_days" type="number" value="{esc(cfg['medium_days'])}" min="1"></div>
      <div><label>中等文件阈值 MiB</label><input name="medium_size_mib" type="number" value="{mib(cfg['medium_size_gt'])}" min="1"></div>
      <div><label>候选报告条数</label><input name="report_limit" type="number" value="{esc(cfg['report_limit'])}" min="1"></div>
      <div><label>大文件保留天数</label><input name="large_days" type="number" value="{esc(cfg['large_days'])}" min="1"></div>
      <div><label>大文件阈值 MiB</label><input name="large_size_mib" type="number" value="{mib(cfg['large_size_gt'])}" min="1"></div>
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
<section>
  <h2>Server Notice</h2>
  <form method="post" action="/notice">
    <div class="grid2">
      <div>
        <label>发送范围</label>
        <select name="target_mode">
          <option value="single">单个用户</option>
          <option value="all">全站本地用户</option>
        </select>
      </div>
      <div><label>单用户 MXID</label><input name="single_user" value="@admin2:{esc(SERVER_NAME)}"></div>
    </div>
    <p><label>排除账号，每行一个或用逗号分隔</label><input name="exclude" value="@server:{esc(SERVER_NAME)}, @media_purge_admin:{esc(SERVER_NAME)}"></p>
    <p><label>通知正文</label><textarea name="body" placeholder="在这里写要发送的通知内容。建议先选择单个用户测试。">{esc(DEFAULT_NOTICE_BODY)}</textarea></p>
    <button class="danger" onclick="return confirm('确认发送 Server Notice？全站发送会通知所有本地用户。')">发送通知</button>
  </form>
</section>
<section id="jobs">
  <h2>最近任务</h2>
  <table><thead><tr><th>创建/完成时间</th><th>类型</th><th>状态</th><th>摘要</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</section>
"""
    return layout(body, message)


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        if self.path != "/":
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
        if self.path != "/":
            self.send_response(404)
            self.end_headers()
            return
        self.send_html(render_home())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        try:
            if self.path == "/purge":
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
            if self.path == "/notice":
                job_id = create_job("server-notice")
                threading.Thread(target=lambda: self.notice_thread(job_id, form), daemon=True).start()
                self.send_html(render_home(f"已启动通知任务 {job_id}"))
                return
            self.send_response(404)
            self.end_headers()
        except Exception as e:
            self.send_html(render_home(f"错误：{e}"), 400)

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
    threading.Thread(target=scheduler_loop, daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    append_log(f"start {HOST}:{PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()

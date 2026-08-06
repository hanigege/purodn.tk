#!/usr/bin/env python3
import datetime as dt
import hashlib
import html
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_PATH = "/root/data/docker_data/synapse/scripts/matrix-bot-monitor.json"
DEFAULT_CONFIG = {
    "homeserver": "http://127.0.0.1:8008",
    "token_path": "/root/data/docker_data/_secrets/matrix-bot/access_token",
    "state_path": "/root/data/docker_data/synapse/scripts/matrix-bot-monitor.state.json",
    "target_users": ["@admin:jgaga.tk"],
    "target_rooms": [],
    "disk": {
        "mounts": ["/", "/var/lib/docker"],
        "warn_percent": 80,
        "critical_percent": 90,
        "min_free_gb": 10
    },
    "docker": {
        "expected_containers": [
            "matrix-admin-tools",
            "caddy",
            "element-web",
            "synapse-admin",
            "matrix-musicbot",
            "matrix-synapse",
            "postgres16",
            "ntfy",
            "element-call",
            "matrix-livekit-jwt-service",
            "livekit-server-test",
            "coturn",
        ]
    },
    "realtime": {
        # 这里必须跟真实容器名保持一致；旧 web-call 已迁到 element-call，保留旧名会让报警失真。
        "containers": ["coturn", "livekit-server-test", "matrix-livekit-jwt-service", "element-call"],
        # TURN/LiveKit/Element Call 是语音稳定性边界；端口变化要和 compose/Caddy 一起核对。
        "tcp_ports": [3478, 7880, 8170, 8081],
        "udp_ports": [3478],
        "http_probes": [
            {"name": "LiveKit JWT", "url": "http://127.0.0.1:8170/healthz", "ok": [200]},
            {"name": "LiveKit API", "url": "http://127.0.0.1:7880/", "ok": [200, 404]},
            {"name": "Element Call Web", "url": "http://127.0.0.1:8081/", "ok": [200, 301, 302]}
        ]
    }
}


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data, mode=0o600):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def request(method, url, token=None, data=None, timeout=20):
    headers = {}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw:
                return resp.status, {}
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"raw": raw[:200]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw}
        return e.code, payload
    except Exception as e:
        return 0, {"error": str(e)}


def quote(value):
    return urllib.parse.quote(value, safe="")


def room_is_encrypted(config, token, room_id):
    url = f"{config['homeserver']}/_matrix/client/v3/rooms/{quote(room_id)}/state/m.room.encryption/"
    status, _ = request("GET", url, token)
    return status == 200


def create_or_get_dm(config, token, user_id, state):
    dms = state.setdefault("direct_rooms", {})
    old_room = dms.get(user_id)
    if old_room and not room_is_encrypted(config, token, old_room):
        return old_room
    if old_room:
        request("POST", f"{config['homeserver']}/_matrix/client/v3/rooms/{quote(old_room)}/leave", token, {})
        dms.pop(user_id, None)
    status, payload = request("POST", f"{config['homeserver']}/_matrix/client/v3/createRoom", token, {
        "preset": "trusted_private_chat",
        "is_direct": True,
        "invite": [user_id],
        "name": "服务器监控提醒",
        "topic": "磁盘、Docker、TURN/LiveKit 异常提醒",
    })
    if status not in (200, 201) or "room_id" not in payload:
        raise RuntimeError(f"create DM for {user_id} failed: HTTP {status} {payload}")
    dms[user_id] = payload["room_id"]
    return payload["room_id"]


def send_room(config, token, room_id, body):
    txn = "server-monitor-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    txn += "-" + hashlib.sha1((room_id + body).encode("utf-8")).hexdigest()[:10]
    html_body = "<br>".join(html.escape(line) for line in body.splitlines())
    content = {"msgtype": "m.text", "body": body, "format": "org.matrix.custom.html", "formatted_body": html_body}
    url = f"{config['homeserver']}/_matrix/client/v3/rooms/{quote(room_id)}/send/m.room.message/{txn}"
    status, payload = request("PUT", url, token, content)
    if status not in (200, 201):
        raise RuntimeError(f"send to {room_id} failed: HTTP {status} {payload}")


def send_alert(config, token, state, body):
    rooms = set(config.get("target_rooms", []))
    for user_id in config.get("target_users", []):
        rooms.add(create_or_get_dm(config, token, user_id, state))
    for room_id in sorted(rooms):
        send_room(config, token, room_id, body)
        time.sleep(0.2)


def run(cmd):
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def check_disks(config):
    issues = []
    seen = set()
    mounts = config["disk"].get("mounts", [])
    warn = int(config["disk"].get("warn_percent", 80))
    crit = int(config["disk"].get("critical_percent", 90))
    min_free = float(config["disk"].get("min_free_gb", 10))
    for mount in mounts:
        if mount in seen:
            continue
        seen.add(mount)
        proc = run(["df", "-P", "-B1", mount])
        if proc.returncode != 0:
            issues.append(f"[disk] {mount}: df failed: {proc.stderr.strip()}")
            continue
        lines = proc.stdout.strip().splitlines()
        if len(lines) < 2:
            issues.append(f"[disk] {mount}: df output invalid")
            continue
        parts = lines[-1].split()
        if len(parts) < 6:
            issues.append(f"[disk] {mount}: df output invalid")
            continue
        used_percent = int(parts[4].rstrip("%"))
        avail_gb = int(parts[3]) / 1024 / 1024 / 1024
        level = None
        if used_percent >= crit:
            level = "critical"
        elif used_percent >= warn or (mount != "/boot" and avail_gb < min_free):
            level = "warning"
        if level:
            issues.append(f"[disk:{level}] {parts[5]} used={used_percent}% free={avail_gb:.1f}GiB")
    return issues


def docker_inspect(names):
    info = {}
    for name in names:
        proc = run(["docker", "inspect", "--format", "{{json .}}", name])
        if proc.returncode != 0:
            info[name] = {"missing": True, "error": proc.stderr.strip()}
            continue
        try:
            data = json.loads(proc.stdout)
        except Exception as e:
            info[name] = {"missing": True, "error": str(e)}
            continue
        state = data.get("State", {})
        health = state.get("Health") or {}
        info[name] = {
            "missing": False,
            "running": bool(state.get("Running")),
            "status": state.get("Status", "unknown"),
            "health": health.get("Status", "none"),
            "restart_count": int(data.get("RestartCount", 0)),
        }
    return info


def check_docker(config, state):
    names = config["docker"].get("expected_containers", [])
    info = docker_inspect(names)
    issues = []
    prev = state.get("docker_restart_counts", {})
    current = {}
    for name in names:
        item = info.get(name, {})
        if item.get("missing"):
            issues.append(f"[docker] {name}: missing ({item.get('error', '').strip()})")
            continue
        current[name] = item.get("restart_count", 0)
        if not item.get("running"):
            issues.append(f"[docker] {name}: not running, status={item.get('status')}")
        if item.get("health") == "unhealthy":
            issues.append(f"[docker] {name}: health=unhealthy")
        old = prev.get(name)
        if old is not None and item.get("restart_count", 0) > old:
            issues.append(f"[docker] {name}: restart count {old} -> {item.get('restart_count')}")
    state["docker_restart_counts"] = current
    return issues


def tcp_port_open(host, port, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def udp_port_listening(port):
    proc = run(["ss", "-lun"])
    if proc.returncode != 0:
        return False
    marker = f":{port}"
    return any(marker in line for line in proc.stdout.splitlines())


def check_http_probe(probe):
    status, payload = request("GET", probe["url"], None, None, timeout=5)
    ok_codes = set(int(x) for x in probe.get("ok", [200]))
    if status in ok_codes:
        return None
    return f"[realtime] {probe['name']}: HTTP {status} {payload}"


def check_realtime(config):
    issues = []
    rt = config.get("realtime", {})
    for issue in check_container_subset(rt.get("containers", [])):
        issues.append(issue)
    for port in rt.get("tcp_ports", []):
        if not tcp_port_open("127.0.0.1", int(port)):
            issues.append(f"[realtime] tcp/{port}: not listening")
    for port in rt.get("udp_ports", []):
        if not udp_port_listening(int(port)):
            issues.append(f"[realtime] udp/{port}: not listening")
    for probe in rt.get("http_probes", []):
        issue = check_http_probe(probe)
        if issue:
            issues.append(issue)
    return issues


def check_container_subset(names):
    issues = []
    for name, item in docker_inspect(names).items():
        if item.get("missing"):
            issues.append(f"[realtime] {name}: missing")
        elif not item.get("running"):
            issues.append(f"[realtime] {name}: not running, status={item.get('status')}")
        elif item.get("health") == "unhealthy":
            issues.append(f"[realtime] {name}: health=unhealthy")
    return issues


def format_recovered_issue(issue):
    replacements = [
        (": missing", ": present"),
        (": not running, status=exited", ": running"),
        (": not running, status=created", ": running"),
        (": not running, status=dead", ": running"),
        (": not running", ": running"),
        (": health=unhealthy", ": healthy"),
        (": not listening", ": listening"),
    ]
    for old, new in replacements:
        if old in issue:
            return issue.replace(old, new)
    if "HTTP 0 " in issue or "Connection refused" in issue:
        return issue.split(": HTTP ", 1)[0] + ": healthy"
    if ": HTTP " in issue:
        return issue.split(": HTTP ", 1)[0] + ": healthy"
    return issue + " -> recovered"


def format_alert(new_issues, recovered):
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"服务器监控提醒 {now}", ""]
    if new_issues:
        lines.append("新增异常：")
        lines.extend(f"- {x}" for x in new_issues)
    if recovered:
        if new_issues:
            lines.append("")
        lines.append("已恢复：")
        lines.extend(f"- {format_recovered_issue(x)}" for x in recovered)
    return "\n".join(lines)


def ensure_config():
    if not os.path.exists(CONFIG_PATH):
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
        return DEFAULT_CONFIG
    config = load_json(CONFIG_PATH, {})
    merged = DEFAULT_CONFIG.copy()
    for key, value in config.items():
        merged[key] = value
    return merged


def main():
    config = ensure_config()
    state = load_json(config["state_path"], {})
    with open(config["token_path"], "r", encoding="utf-8") as f:
        token = f.read().strip()
    if "--test-alert" in sys.argv[1:]:
        # 用无损测试确认 Matrix 告警通道可用，不需要故意停止 LiveKit 或容器制造事故。
        send_alert(config, token, state, "服务器监控提醒测试：告警通道正常。")
        save_json(config["state_path"], state)
        print("monitor test alert sent")
        return
    issues = []
    issues.extend(check_disks(config))
    issues.extend(check_docker(config, state))
    issues.extend(check_realtime(config))
    issues = sorted(set(issues))
    previous = set(state.get("active_issues", []))
    current = set(issues)
    new_issues = sorted(current - previous)
    recovered = sorted(previous - current)
    state["active_issues"] = issues
    state["last_check"] = dt.datetime.now().isoformat(timespec="seconds")
    if new_issues or recovered:
        send_alert(config, token, state, format_alert(new_issues, recovered))
    save_json(config["state_path"], state)
    print(f"monitor ok: active={len(issues)} new={len(new_issues)} recovered={len(recovered)}")


if __name__ == "__main__":
    main()

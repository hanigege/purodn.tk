#!/usr/bin/env python3
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

HOMESERVER = os.environ.get("MATRIX_HOMESERVER", "http://127.0.0.1:8008")
TOKEN_FILE = os.environ.get("MATRIX_BOT_TOKEN_FILE", "/root/data/docker_data/_secrets/matrix-bot/access_token")
SERVER_NAME = os.environ.get("MATRIX_SERVER_NAME", "jgaga.tk")
POLL_SECONDS = int(os.environ.get("MATRIX_BOT_POLL_SECONDS", "20"))

def token():
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()

def req(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {token()}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(HOMESERVER + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}

def send(room_id, body):
    txn = str(int(time.time() * 1000))
    path = f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id, safe='')}/send/m.room.message/{txn}"
    return req("PUT", path, {"msgtype": "m.notice", "body": body})

def main():
    since = None
    print("matrix simple bot started", flush=True)
    while True:
        path = "/_matrix/client/v3/sync?timeout=30000"
        if since:
            path += "&since=" + urllib.parse.quote(since)
        status, data = req("GET", path)
        if status != 200:
            print(f"sync failed http={status} {data}", flush=True)
            time.sleep(POLL_SECONDS)
            continue
        since = data.get("next_batch", since)
        for room_id, room in data.get("rooms", {}).get("invite", {}).items():
            print(f"joining invited room {room_id}", flush=True)
            req("POST", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id, safe='')}/join", {})
        for room_id, room in data.get("rooms", {}).get("join", {}).items():
            events = room.get("timeline", {}).get("events", [])
            for event in events:
                if event.get("type") != "m.room.message":
                    continue
                content = event.get("content", {})
                body = (content.get("body") or "").strip()
                sender = event.get("sender", "")
                if sender.endswith(f":{SERVER_NAME}") and body in ("帮助", "help", "!help"):
                    send(room_id, "我是 Matrix 简单机器人。当前可用命令：帮助")
        time.sleep(1)

if __name__ == "__main__":
    main()

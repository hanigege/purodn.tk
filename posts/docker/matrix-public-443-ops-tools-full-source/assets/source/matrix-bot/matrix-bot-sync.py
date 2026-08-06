#!/usr/bin/env python3
import datetime as dt
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_PATH = "/root/data/docker_data/synapse/scripts/matrix-bot-weather.json"
CMA_CITY_STATIONS = {
    "上海": {"city": "中国上海市", "cma_station_id": "58367", "latitude": 31.22222, "longitude": 121.45806, "timezone": "Asia/Shanghai"},
    "上海市": {"city": "中国上海市", "cma_station_id": "58367", "latitude": 31.22222, "longitude": 121.45806, "timezone": "Asia/Shanghai"},
    "徐州": {"city": "中国江苏徐州", "cma_station_id": "58027", "latitude": 34.18045, "longitude": 117.15707, "timezone": "Asia/Shanghai"},
    "徐州市": {"city": "中国江苏徐州", "cma_station_id": "58027", "latitude": 34.18045, "longitude": 117.15707, "timezone": "Asia/Shanghai"},
}

WELCOME = """你好，我是天气机器人。

你可以这样使用我：

天气
  立即查看今天和未来两天天气

设置天气时间 07:30
  设置每天自动推送时间

天气时间
  查看当前推送时间

设置城市 上海
  修改你的天气城市

城市
  查看当前天气城市

关闭天气
  停止每日天气推送

开启天气
  恢复每日天气推送

帮助
  再次查看这份说明"""


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data, mode=0o600):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def request(method, url, token=None, data=None, timeout=30):
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
            return resp.status, json.loads(raw) if raw else {}
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


def token(config):
    with open(config["token_path"], "r", encoding="utf-8") as f:
        return f.read().strip()


def local_user(config, user_id):
    return user_id.endswith(":" + config.get("server_name", ""))


def send_room(config, token_value, room_id, body):
    txn = "bot-reply-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    txn += "-" + hashlib.sha1((room_id + body).encode("utf-8")).hexdigest()[:10]
    html_body = "<br>".join(html.escape(line) for line in body.splitlines())
    content = {"msgtype": "m.text", "body": body, "format": "org.matrix.custom.html", "formatted_body": html_body}
    status, payload = request("PUT", f"{config['homeserver']}/_matrix/client/v3/rooms/{quote(room_id)}/send/m.room.message/{txn}", token_value, content)
    if status not in (200, 201):
        print(f"send failed {room_id}: HTTP {status} {payload}")


def enable_user(config, user_id, room_id=None):
    settings = config.setdefault("weather_users", {}).setdefault(user_id, {})
    settings.setdefault("enabled", True)
    settings.setdefault("time", config.get("default_weather_time", "05:00"))
    if room_id:
        config.setdefault("direct_rooms", {})[user_id] = room_id


def import_weather_module():
    import importlib.util
    path = "/root/data/docker_data/synapse/scripts/matrix-bot-weather.py"
    spec = importlib.util.spec_from_file_location("matrix_bot_weather", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def display_city(item, fallback):
    country = item.get("country") or ""
    admin1 = item.get("admin1") or ""
    name = item.get("name") or fallback
    if admin1 and (admin1 == name or admin1.startswith(name) or name in admin1):
        return country + admin1
    return "".join(x for x in (country, admin1, name) if x) or fallback


def geocode_city(query):
    normalized = re.sub(r"\s+", "", query)
    if normalized in CMA_CITY_STATIONS:
        return dict(CMA_CITY_STATIONS[normalized])
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://weather.cma.cn/",
        "Accept": "application/json,text/plain,*/*",
    }
    cma_url = "https://weather.cma.cn/api/autocomplete?" + urllib.parse.urlencode({"q": query, "limit": 10})
    try:
        req = urllib.request.Request(cma_url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            cma = json.loads(resp.read().decode("utf-8"))
        for row in cma.get("data", []):
            station_id, name, _pinyin, country = row.split("|", 3)
            if name == query or name in query or query in name:
                return {"city": country + name, "cma_station_id": station_id, "timezone": "Asia/Shanghai"}
    except Exception as exc:
        print(f"CMA geocode failed for {query!r}: {exc}")
    params = urllib.parse.urlencode({
        "name": query,
        "count": 1,
        "language": "zh",
        "format": "json",
    })
    url = "https://geocoding-api.open-meteo.com/v1/search?" + params
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    results = data.get("results") or []
    if not results:
        return None
    item = results[0]
    return {
        "city": display_city(item, query),
        "latitude": item["latitude"],
        "longitude": item["longitude"],
        "timezone": item.get("timezone", "Asia/Shanghai"),
    }


def user_city_text(config, sender):
    weather = import_weather_module()
    profile = weather.weather_profile(config, sender)
    return profile["city"]


def handle_command(config, token_value, state, room_id, sender, body):
    text = body.strip()
    compact = re.sub(r"\s+", "", text)
    lower = compact.lower()
    enable_user(config, sender, room_id)
    if compact in ("帮助", "菜单") or lower == "help":
        send_room(config, token_value, room_id, WELCOME)
    elif compact == "天气":
        weather = import_weather_module()
        try:
            body = weather.get_weather(config, sender)
        except Exception as exc:
            print(f"weather query failed for {sender}: {exc}")
            body = "天气数据源暂时不可用，刚才没有查到天气。请稍后再发送“天气”重试。"
        send_room(config, token_value, room_id, body)
    elif compact == "天气时间":
        settings = config["weather_users"][sender]
        status = "已开启" if settings.get("enabled", True) else "已关闭"
        send_room(config, token_value, room_id, f"你的天气推送{status}。\n当前时间：每天 {settings.get('time', config.get('default_weather_time', '05:00'))}。\n城市：{user_city_text(config, sender)}。")
    elif compact in ("城市", "天气城市"):
        send_room(config, token_value, room_id, f"你当前的天气城市：{user_city_text(config, sender)}。\n发送“设置城市 上海”可以修改。")
    elif compact == "关闭天气":
        config["weather_users"][sender]["enabled"] = False
        send_room(config, token_value, room_id, "已关闭每日天气推送。发送“开启天气”可以恢复。")
    elif compact == "开启天气":
        config["weather_users"][sender]["enabled"] = True
        send_room(config, token_value, room_id, f"已开启每日天气推送。当前时间：每天 {config['weather_users'][sender].get('time', config.get('default_weather_time', '05:00'))}。")
    else:
        city_match = re.fullmatch(r"设置\s*城市\s*(.+)", text)
        time_match = re.fullmatch(r"设置\s*天气\s*时间\s*([0-2]?\d):([0-5]\d)", text)
        if city_match:
            query = city_match.group(1).strip()
            if not query:
                send_room(config, token_value, room_id, "城市不能为空。示例：设置城市 上海")
            else:
                try:
                    city = geocode_city(query)
                except Exception as exc:
                    city = None
                    print(f"geocode failed for {query!r}: {exc}")
                if not city:
                    send_room(config, token_value, room_id, f"没找到“{query}”。可以试试更完整的写法，比如：设置城市 中国上海")
                else:
                    settings = config["weather_users"][sender]
                    settings.update(city)
                    send_room(config, token_value, room_id, f"已设置成功：以后给你推送 {city['city']} 的天气预报。")
        elif time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            if hour > 23:
                send_room(config, token_value, room_id, "时间格式不对。示例：设置天气时间 07:30")
            else:
                config["weather_users"][sender]["time"] = f"{hour:02d}:{minute:02d}"
                config["weather_users"][sender]["enabled"] = True
                send_room(config, token_value, room_id, f"已设置成功：每天 {hour:02d}:{minute:02d} 给你推送天气预报。")
        elif lower in ("hi", "hello", "你好") or compact in ("您好",):
            send_room(config, token_value, room_id, WELCOME)
        else:
            send_room(config, token_value, room_id, "我没看懂。可以发送“帮助”查看全部命令。设置时间可以这样发：设置天气时间 07:30，也可以发：设置天气时间07:30。")
    save_json(CONFIG_PATH, config)


def invite_sender(config, invite):
    events = invite.get("invite_state", {}).get("events", [])
    for event in events:
        if event.get("type") == "m.room.member" and event.get("state_key") == config.get("bot_user_id"):
            return event.get("sender")
    return None


def main():
    config = load_json(CONFIG_PATH, None)
    if not config:
        raise SystemExit(f"missing config: {CONFIG_PATH}")
    state = load_json(config.get("sync_state_path"), {})
    token_value = token(config)
    params = {"timeout": 0}
    if state.get("since"):
        params["since"] = state["since"]
    status, payload = request("GET", f"{config['homeserver']}/_matrix/client/v3/sync?{urllib.parse.urlencode(params)}", token_value, timeout=35)
    if status != 200:
        raise SystemExit(f"sync failed: HTTP {status} {payload}")
    for room_id, invite in payload.get("rooms", {}).get("invite", {}).items():
        sender = invite_sender(config, invite)
        if config.get("allow_local_users_only", True) and (not sender or not local_user(config, sender)):
            print(f"skip non-local invite {room_id} from {sender}")
            continue
        join_status, join_payload = request("POST", f"{config['homeserver']}/_matrix/client/v3/rooms/{quote(room_id)}/join", token_value, {})
        if join_status in (200, 201):
            enable_user(config, sender, room_id)
            send_room(config, token_value, room_id, WELCOME)
        else:
            print(f"join failed {room_id}: HTTP {join_status} {join_payload}")
    first_sync = not state.get("since")
    if not first_sync:
        for room_id, room in payload.get("rooms", {}).get("join", {}).items():
            for event in room.get("timeline", {}).get("events", []):
                if event.get("type") != "m.room.message":
                    continue
                sender = event.get("sender")
                if sender == config.get("bot_user_id") or not sender or not local_user(config, sender):
                    continue
                content = event.get("content", {})
                if content.get("msgtype") != "m.text":
                    continue
                handle_command(config, token_value, state, room_id, sender, content.get("body", ""))
    state["since"] = payload.get("next_batch", state.get("since"))
    save_json(config.get("sync_state_path"), state)
    save_json(CONFIG_PATH, config)
    print("sync ok")

if __name__ == "__main__":
    main()

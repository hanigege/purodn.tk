#!/usr/bin/env python3
import datetime as dt
import hashlib
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_PATH = "/root/data/docker_data/synapse/scripts/matrix-bot-weather.json"
WEATHER_CODES = {
    0: "晴", 1: "大部晴朗", 2: "局部多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "较强毛毛雨", 56: "冻毛毛雨", 57: "较强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨", 85: "小阵雪", 86: "强阵雪",
    95: "雷雨", 96: "雷雨伴小冰雹", 99: "雷雨伴强冰雹",
}
HELP_HINT = "发送“设置天气时间 07:30”可修改推送时间，发送“帮助”可查询更多功能。"


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


def room_is_encrypted(config, token_value, room_id):
    url = f"{config['homeserver']}/_matrix/client/v3/rooms/{quote(room_id)}/state/m.room.encryption/"
    status, _ = request("GET", url, token_value)
    return status == 200


def default_weather_profile(config):
    return {
        "city": config.get("city", "中国江苏省徐州市"),
        "latitude": config.get("latitude", 34.2044),
        "longitude": config.get("longitude", 117.2841),
        "timezone": config.get("timezone", "Asia/Shanghai"),
    }


def weather_profile(config, user_id=None):
    profile = default_weather_profile(config)
    if user_id:
        profile.update(config.get("weather_users", {}).get(user_id, {}))
    return profile


def ensure_weather_users(config):
    users = config.setdefault("weather_users", {})
    default_time = config.get("default_weather_time", "05:00")
    # target_users 是管理员白名单；迁移时只补默认用户，不覆盖用户自己设置的城市和时间。
    for user_id in config.get("target_users", []):
        settings = users.setdefault(user_id, {})
        settings.setdefault("enabled", True)
        settings.setdefault("time", default_time)
    return users


def get_weather(config, user_id=None):
    profile = weather_profile(config, user_id)
    params = urllib.parse.urlencode({
        "latitude": profile["latitude"],
        "longitude": profile["longitude"],
        "timezone": profile.get("timezone", "Asia/Shanghai"),
        "forecast_days": 3,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max",
    })
    url = "https://api.open-meteo.com/v1/forecast?" + params
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    daily = data["daily"]
    lines = [f"{profile['city']}天气预报", ""]
    today = dt.date.today().isoformat()
    alerts = []
    advice = []
    for i, day in enumerate(daily["time"]):
        label = "今天" if day == today else ("明天" if i == 1 else "后天")
        code = daily["weather_code"][i]
        desc = WEATHER_CODES.get(code, f"天气代码 {code}")
        tmin = daily["temperature_2m_min"][i]
        tmax = daily["temperature_2m_max"][i]
        pop = daily["precipitation_probability_max"][i]
        rain = daily["precipitation_sum"][i]
        wind = daily["wind_speed_10m_max"][i]
        lines.append(
            f"{label} {day}: {desc}，{tmin:.0f}-{tmax:.0f}℃，"
            f"降水概率 {pop}% ，降水量 {rain:.1f} mm，最大风速 {wind:.0f} km/h"
        )
        if i == 0:
            if tmax >= 35:
                alerts.append("高温提醒：今天最高温达到 35℃ 以上，注意防暑补水。")
            if tmin <= 0:
                alerts.append("低温提醒：今天最低温 0℃ 以下，注意防寒防冻。")
            if rain >= 20 or pop >= 70:
                alerts.append("降雨提醒：今天降水概率较高或雨量较大，出门建议带伞。")
            if wind >= 40:
                alerts.append("大风提醒：今天风力偏强，骑车和户外活动注意安全。")
            if code in (95, 96, 99):
                alerts.append("雷雨提醒：今天可能有雷雨，尽量避开强对流时段外出。")
            if tmax >= 30:
                advice.append("穿衣建议：短袖、轻薄透气衣物为主，注意防晒。")
            elif tmin <= 8:
                advice.append("穿衣建议：建议外套或薄棉服，早晚注意保暖。")
            elif tmin <= 15:
                advice.append("穿衣建议：长袖加薄外套比较稳妥。")
            else:
                advice.append("穿衣建议：轻薄长袖或短袖均可，按体感调整。")
            if (tmax - tmin) >= 10:
                advice.append("温差提醒：昼夜温差较大，早晚可多备一件外套。")
            if pop >= 50 or rain > 0:
                advice.append("出行提醒：有降水可能，建议带伞。")
    if alerts:
        lines.extend(["", "天气提醒："] + [f"- {x}" for x in alerts])
    if advice:
        lines.extend(["", "生活建议："] + [f"- {x}" for x in advice])
    lines.extend(["", "数据源：Open-Meteo。", HELP_HINT])
    return "\n".join(lines)


def send_room(config, token_value, room_id, body):
    txn = "weather-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    txn += "-" + hashlib.sha1((room_id + body).encode("utf-8")).hexdigest()[:10]
    html_body = "<br>".join(html.escape(line) for line in body.splitlines())
    content = {"msgtype": "m.text", "body": body, "format": "org.matrix.custom.html", "formatted_body": html_body}
    url = f"{config['homeserver']}/_matrix/client/v3/rooms/{quote(room_id)}/send/m.room.message/{txn}"
    status, payload = request("PUT", url, token_value, content)
    if status not in (200, 201):
        raise RuntimeError(f"send to {room_id} failed: HTTP {status} {payload}")


def create_dm(config, token_value, user_id):
    status, payload = request("POST", f"{config['homeserver']}/_matrix/client/v3/createRoom", token_value, {
        "preset": "trusted_private_chat",
        "is_direct": True,
        "invite": [user_id],
        "name": "天气预报",
        "topic": "天气查询与每日自动推送",
    })
    if status not in (200, 201) or "room_id" not in payload:
        raise RuntimeError(f"create DM for {user_id} failed: HTTP {status} {payload}")
    return payload["room_id"]


def get_room(config, token_value, user_id):
    rooms = config.setdefault("direct_rooms", {})
    state = load_json(config.get("state_path"), {}) if config.get("state_path") else {}
    room_id = rooms.get(user_id) or state.get("direct_rooms", {}).get(user_id)
    if room_id and not room_is_encrypted(config, token_value, room_id):
        rooms[user_id] = room_id
        return room_id
    if room_id:
        # Matrix 加密房间不能原地关闭；天气机器人没有 E2EE 密钥能力，只能离开旧房间后重建不加密私聊。
        request("POST", f"{config['homeserver']}/_matrix/client/v3/rooms/{quote(room_id)}/leave", token_value, {})
    room_id = create_dm(config, token_value, user_id)
    rooms[user_id] = room_id
    save_json(CONFIG_PATH, config)
    return room_id


def parse_hhmm(value):
    hour_text, minute_text = str(value).split(":", 1)
    hour = int(hour_text)
    minute = int(minute_text)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid HH:MM value: {value}")
    return hour, minute


def normalized_hhmm(value):
    hour, minute = parse_hhmm(value)
    return f"{hour:02d}:{minute:02d}"


def sent_key(today, time_value):
    return f"{today} {normalized_hhmm(time_value)}"


def due_users(config, now=None):
    ensure_weather_users(config)
    now = now or dt.datetime.now()
    today = now.date().isoformat()
    state = load_json(config["state_path"], {})
    sent = state.setdefault("last_sent", {})
    grace_minutes = int(config.get("schedule_grace_minutes", 15))
    users = []
    for user_id, settings in config.get("weather_users", {}).items():
        if not settings.get("enabled", True):
            continue
        time_value = settings.get("time", config.get("default_weather_time", "05:00"))
        try:
            hour, minute = parse_hhmm(time_value)
        except Exception as exc:
            print(f"skip {user_id}: invalid weather time {time_value!r}: {exc}", file=sys.stderr)
            continue
        if sent.get(user_id) == sent_key(today, time_value):
            continue
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta_seconds = (now - scheduled).total_seconds()
        if 0 <= delta_seconds < grace_minutes * 60:
            users.append(user_id)
    save_json(CONFIG_PATH, config)
    return users, state, today


def send_weather_to_users(config, user_ids, mark_sent=False, state=None, today=None):
    ensure_weather_users(config)
    token_value = token(config)
    sent = 0
    for user_id in user_ids:
        body = get_weather(config, user_id)
        room_id = get_room(config, token_value, user_id)
        send_room(config, token_value, room_id, body)
        sent += 1
        if mark_sent and state is not None and today is not None:
            user_time = config.get("weather_users", {}).get(user_id, {}).get("time", config.get("default_weather_time", "05:00"))
            state.setdefault("last_sent", {})[user_id] = sent_key(today, user_time)
        time.sleep(0.2)
    save_json(CONFIG_PATH, config)
    if mark_sent and state is not None:
        save_json(config["state_path"], state)
    return sent


def main():
    config = load_json(CONFIG_PATH, None)
    if not config:
        raise SystemExit(f"missing config: {CONFIG_PATH}")
    if len(sys.argv) >= 2 and sys.argv[1] == "--send-now":
        users = sys.argv[2:] or list(ensure_weather_users(config).keys())
        print(f"sent weather forecast to {send_weather_to_users(config, users)} room(s)")
        return
    users, state, today = due_users(config)
    if not users:
        print("weather schedule: no due users")
        return
    print(f"sent weather forecast to {send_weather_to_users(config, users, True, state, today)} room(s)")


if __name__ == "__main__":
    main()

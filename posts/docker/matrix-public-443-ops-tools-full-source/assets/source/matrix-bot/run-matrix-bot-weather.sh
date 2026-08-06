#!/bin/sh
LOCK=/tmp/matrix-bot-weather.lock
STAMP=/tmp/matrix-bot-weather.$(date +%F).stamp
# 天气定时循环每 20 秒唤醒一次；日戳防止服务重启或循环重试时同一天重复推送。
if [ -e "$STAMP" ]; then
  exit 0
fi
if mkdir "$LOCK" 2>/dev/null; then
  trap 'rmdir "$LOCK"' EXIT
  /root/data/docker_data/synapse/scripts/matrix-bot-weather.py >> /var/log/matrix-bot-weather.log 2>&1 && touch "$STAMP"
fi

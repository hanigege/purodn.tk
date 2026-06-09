#!/bin/sh
LOCK=/tmp/matrix-bot-monitor.lock
if mkdir "$LOCK" 2>/dev/null; then
  # 避免上一轮检查没结束时重复发告警；锁目录变量不能丢，否则监控会静默不执行。
  trap 'rmdir "$LOCK"' EXIT INT TERM
  /root/data/docker_data/synapse/scripts/matrix-bot-monitor.py >> /var/log/matrix-bot-monitor.log 2>&1
fi

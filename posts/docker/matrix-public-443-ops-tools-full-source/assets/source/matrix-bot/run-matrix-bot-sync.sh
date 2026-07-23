#!/bin/sh
LOCK=/tmp/matrix-bot-sync.lock
if mkdir "$LOCK" 2>/dev/null; then
  trap 'rmdir "$LOCK"' EXIT
  /root/data/docker_data/synapse/scripts/matrix-bot-sync.py >> /var/log/matrix-bot-sync.log 2>&1
fi

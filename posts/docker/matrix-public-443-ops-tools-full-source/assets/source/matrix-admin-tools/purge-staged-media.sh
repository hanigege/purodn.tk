#!/bin/sh
set -eu

SYNAPSE_URL="${SYNAPSE_URL:-http://127.0.0.1:8008}"
TOKEN_FILE="${TOKEN_FILE:-/run/secrets/synapse-admin-access-token}"
REMOTE_DAYS="${REMOTE_DAYS:-7}"
KEEP_PROFILES="${KEEP_PROFILES:-true}"
REPORT_LIMIT="${REPORT_LIMIT:-30}"
SMALL_KEEP_SIZE="${SMALL_KEEP_SIZE:-5242880}"
MEDIUM_DAYS="${MEDIUM_DAYS:-90}"
MEDIUM_SIZE_GT="${MEDIUM_SIZE_GT:-5242880}"
LARGE_DAYS="${LARGE_DAYS:-7}"
LARGE_SIZE_GT="${LARGE_SIZE_GT:-104857600}"
DRY_RUN="${DRY_RUN:-false}"

TOKEN="$(cat "$TOKEN_FILE")"
TMP="/tmp/synapse-media-tiered-purge-response.json"

before_ms() {
  python3 - "$1" <<'PY'
import sys
import time

days = int(sys.argv[1])
print(int((time.time() - days * 86400) * 1000))
PY
}

api_post() {
  label="$1"
  url="$2"
  echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $label"
  if [ "$DRY_RUN" = "true" ]; then
    echo "DRY_RUN=true skip POST $url"
    return 0
  fi
  http_code="$(curl -sS -o "$TMP" -w '%{http_code}' -X POST -H "Authorization: Bearer $TOKEN" "$SYNAPSE_URL$url")"
  cat "$TMP"
  echo
  if [ "$http_code" -lt 200 ] || [ "$http_code" -ge 300 ]; then
    echo "$label: Admin API failed with HTTP $http_code" >&2
    rm -f "$TMP"
    exit 1
  fi
  rm -f "$TMP"
}

report_policy() {
  echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] local media policy report"
  echo "Policy: <=5MiB kept long-term; >5MiB deleted after ${MEDIUM_DAYS}d; >100MiB deleted after ${LARGE_DAYS}d; remote cache deleted after ${REMOTE_DAYS}d."
  echo "REPORT_LIMIT=${REPORT_LIMIT} SMALL_KEEP_SIZE=${SMALL_KEEP_SIZE}"
  echo "This container does not mount the PostgreSQL socket or Docker socket; it reports policy and Admin API calls only."
}

purge_local_media() {
  label="$1"
  days="$2"
  size_gt="$3"
  before_ts="$(before_ms "$days")"
  # 生产媒体清理边界：只按明确大小和时间窗口删除，keep_profiles 默认保留头像，避免误删用户资料图。
  api_post "$label older_than=${days}d size_gt=${size_gt} keep_profiles=${KEEP_PROFILES}" \
    "/_synapse/admin/v1/media/delete?before_ts=${before_ts}&size_gt=${size_gt}&keep_profiles=${KEEP_PROFILES}"
}

purge_remote_cache() {
  before_ts="$(before_ms "$REMOTE_DAYS")"
  # 远端缓存可重取，本地上传文件不可重取；所以远端缓存窗口单独配置，不能和本地媒体策略混在一起。
  api_post "purge remote media cache older_than=${REMOTE_DAYS}d" \
    "/_synapse/admin/v1/purge_media_cache?before_ts=${before_ts}"
}

report_policy
purge_local_media "purge local large media" "$LARGE_DAYS" "$LARGE_SIZE_GT"
purge_local_media "purge local medium media" "$MEDIUM_DAYS" "$MEDIUM_SIZE_GT"
purge_remote_cache

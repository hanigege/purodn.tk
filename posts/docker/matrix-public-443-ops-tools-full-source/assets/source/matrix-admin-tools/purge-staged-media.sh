#!/bin/sh
set -eu

SYNAPSE_URL="${SYNAPSE_URL:-http://127.0.0.1:8008}"
TOKEN_FILE="${TOKEN_FILE:-/run/secrets/synapse-admin-access-token}"
REMOTE_DAYS="${REMOTE_DAYS:-7}"
KEEP_PROFILES="${KEEP_PROFILES:-true}"
# SMALL_KEEP_SIZE 仅用于报告展示，实际删除边界由 MEDIUM_SIZE_GT 决定。
# 两层清理产生三层语义：不清理区(<=SMALL_KEEP_SIZE)、中等清理区(<LARGE_SIZE_GT)、紧急清理区(>=LARGE_SIZE_GT)。
# 必须先跑最激进的（大文件紧急清理），再跑松的（中等清理），确保中等清理不会在上一轮已被删光的文件上重复报错。
MIB=$((1024 * 1024))
DAY_SECONDS=86400
SMALL_KEEP_SIZE="${SMALL_KEEP_SIZE:-3145728}"
MEDIUM_DAYS="${MEDIUM_DAYS:-7}"
MEDIUM_SIZE_GT="${MEDIUM_SIZE_GT:-3145728}"
LARGE_DAYS="${LARGE_DAYS:-3}"
LARGE_SIZE_GT="${LARGE_SIZE_GT:-15728640}"
DRY_RUN="${DRY_RUN:-false}"

TOKEN="$(cat "$TOKEN_FILE")"
TMP="/tmp/synapse-media-tiered-purge-response.json"

before_ms() {
  # $1 是 shell 传入的天数，DAY_SECONDS 是脚本顶部定义的常数
  python3 -c "import time; print(int((time.time() - ${1} * ${DAY_SECONDS}) * 1000))"
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
  # 所有数值都来自环境变量（由 app.py 表单配置传入），无任何写死阈值。
  # 实际删除只用 MEDIUM_SIZE_GT 和 LARGE_SIZE_GT 两个 size_gt 参数，
  # 先跑紧急清理(size_gt=LARGE_SIZE_GT, days=LARGE_DAYS)、后跑常规清理(size_gt=MEDIUM_SIZE_GT, days=MEDIUM_DAYS)。
  # SMALL_KEEP_SIZE = MEDIUM_SIZE_GT，只是为了在日志里显示"不清理上限"。
  small_mib=$((SMALL_KEEP_SIZE / MIB))
  medium_mib=$((MEDIUM_SIZE_GT / MIB))
  large_mib=$((LARGE_SIZE_GT / MIB))
  echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] local media policy report"
  echo "Policy: <=${small_mib}MiB kept long-term; >${medium_mib}MiB deleted after ${MEDIUM_DAYS}d; >${large_mib}MiB deleted after ${LARGE_DAYS}d; remote cache deleted after ${REMOTE_DAYS}d."
  echo "SMALL_KEEP_SIZE=${SMALL_KEEP_SIZE} MEDIUM_SIZE_GT=${MEDIUM_SIZE_GT}"
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
# 必须先跑紧急清理（大文件最短天数），再跑常规清理（含大文件和中等文件），
# 因为两个 size_gt 参数不同：紧急清理的 size_gt 更大、天数更短，
# 常规清理的 size_gt 更小、天数更长。顺序颠倒会导致常规清理误删本该紧急清理区保留到短天数的文件。
urgent_mib=$((LARGE_SIZE_GT / MIB))
normal_mib=$((MEDIUM_SIZE_GT / MIB))
purge_local_media "purge urgent (files > ${urgent_mib}MiB, >=${LARGE_DAYS}d)" "$LARGE_DAYS" "$LARGE_SIZE_GT"
purge_local_media "purge normal (files > ${normal_mib}MiB, >=${MEDIUM_DAYS}d)" "$MEDIUM_DAYS" "$MEDIUM_SIZE_GT"
purge_remote_cache

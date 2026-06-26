#!/usr/bin/env bash
set -euo pipefail

# 在新机器执行：从离线包恢复 sing-box 网关。
# 默认会先备份新机器现有文件，再覆盖；不会修改上游路由器配置。

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "请用 root 执行：sudo -i 后再运行本脚本。" >&2
    exit 1
  fi
}

confirm() {
  local message="$1"
  if [ "${ASSUME_YES:-0}" = "1" ]; then
    return 0
  fi
  printf "%s [yes/NO]: " "$message"
  read -r answer
  [ "$answer" = "yes" ]
}

detect_new_v4() {
  ip route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}'
}

detect_old_v4_from_package() {
  local root="$1"
  python3 - "$root" <<'PY'
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
candidates = []
for rel in ("files/etc/sing-box/config.json", "files/etc/sing-box/manager/base.json"):
    path = root / rel
    if not path.exists():
        continue
    text = path.read_text(encoding="utf-8", errors="ignore")
    for value in re.findall(r'(?<![\d.])(?:10|172|192)\.(?:\d{1,3}\.){2}\d{1,3}(?![\d.])', text):
        if value not in candidates and not value.startswith("127."):
            candidates.append(value)
if candidates:
    print(candidates[0])
PY
}

extract_archive() {
  local archive="$1"
  local sha_file sha_dir
  sha_file="${SHA256_FILE:-$archive.sha256}"
  if [ -r "$sha_file" ]; then
    # sha256 文件里只写包文件名；切到同目录校验，避免从其它目录执行脚本时找不到包。
    sha_dir="$(cd "$(dirname "$sha_file")" && pwd)"
    (cd "$sha_dir" && sha256sum -c "$(basename "$sha_file")")
  else
    echo "WARN: 没找到 sha256 文件：$sha_file，跳过哈希校验。" >&2
  fi
  tar -xzpf "$archive" -C /root
  tar -tzf "$archive" | sed -n '1p' | cut -d/ -f1
}

install_packages_if_possible() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y python3 iproute2 nftables tar gzip coreutils rsync util-linux dnsutils openssl
  else
    echo "WARN: 当前系统没有 apt-get，请自行确认 python3/iproute2/nftables/rsync/dnsutils 已安装。" >&2
  fi
}

backup_existing_files() {
  local backup_dir="$1"
  mkdir -p "$backup_dir"
  [ -e /etc/sing-box ] && cp -a /etc/sing-box "$backup_dir/"
  [ -e /opt/singbox-rule-ui ] && cp -a /opt/singbox-rule-ui "$backup_dir/"
  [ -e /usr/local/bin/sing-box ] && cp -a /usr/local/bin/sing-box "$backup_dir/"
  [ -e /usr/local/bin/sing-box-gateway-info ] && cp -a /usr/local/bin/sing-box-gateway-info "$backup_dir/"
  [ -e /usr/local/bin/sing-box-gateway-uninstall ] && cp -a /usr/local/bin/sing-box-gateway-uninstall "$backup_dir/"
  for f in \
    sing-box.service \
    sing-box-tproxy.service \
    singbox-rule-ui.service \
    update-sing-box-rules-jsdelivr.service \
    update-sing-box-rules-jsdelivr.timer \
    monitor-sing-box-runtime.service \
    monitor-sing-box-runtime.timer
  do
    [ -e /etc/systemd/system/$f ] && cp -a /etc/systemd/system/$f "$backup_dir/"
  done
}

stop_old_services() {
  systemctl stop monitor-sing-box-runtime.timer 2>/dev/null || true
  systemctl stop update-sing-box-rules-jsdelivr.timer 2>/dev/null || true
  systemctl stop singbox-rule-ui.service 2>/dev/null || true
  systemctl stop sing-box.service 2>/dev/null || true
  systemctl stop sing-box-tproxy.service 2>/dev/null || true
  nft delete table inet singbox_tproxy 2>/dev/null || true
}

disable_resolved_stub_if_needed() {
  if ss -ltnup 2>/dev/null | grep -E ':53\b' | grep -q 'systemd-resolve'; then
    mkdir -p /etc/systemd/resolved.conf.d
    cat >/etc/systemd/resolved.conf.d/sing-box-disable-stub.conf <<'EOF'
[Resolve]
DNSStubListener=no
EOF
    systemctl restart systemd-resolved.service || true
    sleep 2
  fi
}

copy_restored_files() {
  local root="$1"
  cp -a "$root/files/etc/sing-box" /etc/
  cp -a "$root/files/opt/singbox-rule-ui" /opt/
  cp -a "$root/files/usr/local/bin/"* /usr/local/bin/
  cp -a "$root/files/usr/local/sbin/"* /usr/local/sbin/
  cp -a "$root/files/etc/systemd/system/"* /etc/systemd/system/
  [ -d "$root/files/etc/sysctl.d" ] && cp -a "$root/files/etc/sysctl.d/"* /etc/sysctl.d/ 2>/dev/null || true
  if [ -d "$root/files/etc/systemd/journald.conf.d" ]; then
    mkdir -p /etc/systemd/journald.conf.d
    cp -a "$root/files/etc/systemd/journald.conf.d/"* /etc/systemd/journald.conf.d/ 2>/dev/null || true
  fi
  chown -R root:root /etc/sing-box /opt/singbox-rule-ui
  chmod 755 /usr/local/bin/sing-box /usr/local/bin/sing-box-gateway-info /usr/local/bin/sing-box-gateway-uninstall 2>/dev/null || true
  chmod 755 /usr/local/sbin/refresh-sing-box-runtime-config /usr/local/sbin/refresh-sing-box-tproxy-setup /usr/local/sbin/sing-box-tproxy-setup /usr/local/sbin/update-sing-box-rules-jsdelivr /usr/local/sbin/monitor-sing-box-runtime 2>/dev/null || true
  chmod 600 /etc/sing-box/rule-ui/token 2>/dev/null || true
}

replace_addresses() {
  local old_v4="$1" new_v4="$2" old_v6="${3:-}" new_v6="${4:-}"
  python3 - "$old_v4" "$new_v4" "$old_v6" "$new_v6" <<'PY'
from pathlib import Path
import sys

old_v4, new_v4, old_v6, new_v6 = sys.argv[1:5]
paths = [
    Path("/etc/sing-box/config.json"),
    Path("/etc/sing-box/manager/base.json"),
    Path("/usr/local/sbin/sing-box-tproxy-setup"),
]
for path in paths:
    if not path.exists():
        continue
    text = path.read_text(encoding="utf-8", errors="ignore")
    if old_v4 and new_v4:
        text = text.replace(old_v4, new_v4)
    if old_v6 and new_v6:
        text = text.replace(old_v6, new_v6)
    path.write_text(text, encoding="utf-8")
PY
}

set_fakeip_ipv6() {
  local enabled="$1"
  python3 - "$enabled" <<'PY'
import json
import sys
from pathlib import Path

enabled = sys.argv[1] == "1"
path = Path("/etc/sing-box/manager/groups.json")
if not path.exists():
    raise SystemExit(0)
data = json.loads(path.read_text(encoding="utf-8"))
data.setdefault("fakeip", {})
data["fakeip"]["ipv6_enabled"] = enabled
data["fakeip"]["block_quic"] = True
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

rotate_token_if_requested() {
  if [ "${ROTATE_RULE_UI_TOKEN:-0}" = "1" ]; then
    install -d -m 755 /etc/sing-box/rule-ui
    openssl rand -hex 24 > /etc/sing-box/rule-ui/token
    chmod 600 /etc/sing-box/rule-ui/token
  fi
}

start_services() {
  systemctl daemon-reload
  systemctl restart systemd-journald.service 2>/dev/null || true
  systemctl enable sing-box-tproxy.service sing-box.service singbox-rule-ui.service update-sing-box-rules-jsdelivr.timer monitor-sing-box-runtime.timer
  systemctl restart sing-box-tproxy.service
  systemctl restart sing-box.service
  systemctl restart singbox-rule-ui.service
  systemctl restart update-sing-box-rules-jsdelivr.timer
  systemctl restart monitor-sing-box-runtime.timer
}

need_root

archive="${1:-${RESTORE_ARCHIVE:-}}"
if [ -z "$archive" ]; then
  archive="$(ls -1t /root/sing-box-production-manual-restore-*.tar.gz 2>/dev/null | sed -n '1p' || true)"
fi
if [ -z "$archive" ] || [ ! -r "$archive" ]; then
  echo "用法：ASSUME_YES=1 $0 /root/sing-box-production-manual-restore-xxx.tar.gz" >&2
  exit 1
fi

install_packages_if_possible
root_name="$(extract_archive "$archive")"
restore_root="/root/$root_name"

if [ ! -d "$restore_root/files/etc/sing-box" ] || [ ! -d "$restore_root/files/opt/singbox-rule-ui" ]; then
  echo "恢复包不完整：缺少 files/etc/sing-box 或 files/opt/singbox-rule-ui。" >&2
  exit 1
fi

new_v4="${NEW_LAN_IPV4:-$(detect_new_v4)}"
old_v4="${OLD_LAN_IPV4:-$(detect_old_v4_from_package "$restore_root")}"
old_v6="${OLD_LAN_IPV6_DNS:-}"
new_v6="${NEW_LAN_IPV6_DNS:-}"

echo "恢复包目录：$restore_root"
echo "旧 LAN IPv4：${old_v4:-未识别}"
echo "新 LAN IPv4：${new_v4:-未识别}"
echo "IPv6 DNS 替换：${old_v6:-未设置} -> ${new_v6:-未设置}"
echo "Rule UI token：$([ "${ROTATE_RULE_UI_TOKEN:-0}" = "1" ] && echo 将重新生成 || echo 保留备份包内的 token)"
echo "IPv6 FakeIP：$([ "${DISABLE_IPV6_FAKEIP:-0}" = "1" ] && echo 启动前关闭 || echo 保持备份包设置)"
echo

if [ -z "$new_v4" ]; then
  echo "无法自动识别新机器 LAN IPv4，请设置 NEW_LAN_IPV4=你的新IP 后重试。" >&2
  exit 1
fi
if [ -z "$old_v4" ]; then
  echo "无法自动识别旧 LAN IPv4，请设置 OLD_LAN_IPV4=旧生产机IP 后重试。" >&2
  exit 1
fi

confirm "确认把备份包恢复到当前机器，并把 $old_v4 替换为 $new_v4？" || exit 1

backup_dir="/root/pre-sing-box-restore-$(date +%Y%m%d-%H%M%S)"
backup_existing_files "$backup_dir"
stop_old_services
disable_resolved_stub_if_needed
copy_restored_files "$restore_root"
replace_addresses "$old_v4" "$new_v4" "$old_v6" "$new_v6"

if [ "${DISABLE_IPV6_FAKEIP:-0}" = "1" ]; then
  set_fakeip_ipv6 0
fi
rotate_token_if_requested

/usr/local/sbin/refresh-sing-box-tproxy-setup
/usr/local/sbin/refresh-sing-box-runtime-config
/usr/local/bin/sing-box check -c /etc/sing-box/config.json

if [ "${SKIP_START:-0}" != "1" ]; then
  start_services
fi

echo
echo "恢复完成。恢复前备份目录：$backup_dir"
echo
systemctl --no-pager --full status sing-box.service sing-box-tproxy.service singbox-rule-ui.service | sed -n '1,160p' || true
echo
ss -ltnup | grep -E ':(53|9090|9091|9888)\b' || true
echo
echo "Rule UI token："
cat /etc/sing-box/rule-ui/token 2>/dev/null || true
echo
echo "访问地址通常是：http://$new_v4:9091/"

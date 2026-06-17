#!/usr/bin/env bash
set -euo pipefail

# 在生产机执行：生成一个不依赖 GitHub 的 sing-box 网关离线恢复包。
# 注意：备份包通常包含节点、token、secret 和真实网络地址，只能放在可信位置。

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "请用 root 执行：sudo -i 后再运行本脚本。" >&2
    exit 1
  fi
}

copy_if_exists() {
  local src="$1" dst="$2"
  if [ -e "$src" ]; then
    cp -a "$src" "$dst"
  fi
}

need_root

stamp="${BACKUP_STAMP:-$(date +%Y%m%d-%H%M%S)}"
output_dir="${BACKUP_OUTPUT_DIR:-/root}"
work="${BACKUP_WORK_DIR:-/tmp/sing-box-production-manual-restore-$stamp}"
archive="$output_dir/sing-box-production-manual-restore-$stamp.tar.gz"

mkdir -p \
  "$work/manifest" \
  "$work/files/etc" \
  "$work/files/opt" \
  "$work/files/usr/local/bin" \
  "$work/files/usr/local/sbin" \
  "$work/files/etc/systemd/system" \
  "$work/files/etc/systemd/journald.conf.d" \
  "$work/files/etc/sysctl.d" \
  "$output_dir"

{
  echo "backup_created=$stamp"
  echo "source_host=$(hostname)"
  echo "source_arch=$(uname -m)"
  echo "source_kernel=$(uname -r)"
  echo "sing_box_version_begin"
  /usr/local/bin/sing-box version 2>/dev/null || true
  echo "sing_box_version_end"
} > "$work/manifest/host-info.txt"

# manifest 只用于恢复前核对和排障，不会在恢复时覆盖新机器。
systemctl cat \
  sing-box.service \
  sing-box-tproxy.service \
  singbox-rule-ui.service \
  update-sing-box-rules-jsdelivr.service \
  update-sing-box-rules-jsdelivr.timer \
  monitor-sing-box-runtime.service \
  monitor-sing-box-runtime.timer \
  > "$work/manifest/systemd-units-expanded.txt" 2>&1 || true

systemctl status --no-pager \
  sing-box.service \
  sing-box-tproxy.service \
  singbox-rule-ui.service \
  update-sing-box-rules-jsdelivr.timer \
  monitor-sing-box-runtime.timer \
  > "$work/manifest/systemd-status.txt" 2>&1 || true

ss -ltnup > "$work/manifest/listeners.txt" 2>&1 || true
ip -o -4 addr show scope global > "$work/manifest/ip-v4.txt" 2>&1 || true
ip -o -6 addr show scope global > "$work/manifest/ip-v6.txt" 2>&1 || true
ip route show table all > "$work/manifest/ip-route-all.txt" 2>&1 || true
ip rule show > "$work/manifest/ip-rule.txt" 2>&1 || true
nft list ruleset > "$work/manifest/nft-ruleset.txt" 2>&1 || true

# 核心配置、UI、二进制和 helper 脚本。
copy_if_exists /etc/sing-box "$work/files/etc/"
copy_if_exists /opt/singbox-rule-ui "$work/files/opt/"

for f in \
  /usr/local/bin/sing-box \
  /usr/local/bin/sing-box-gateway-info \
  /usr/local/bin/sing-box-gateway-uninstall
do
  copy_if_exists "$f" "$work/files/usr/local/bin/"
done

for f in \
  /usr/local/sbin/refresh-sing-box-runtime-config \
  /usr/local/sbin/refresh-sing-box-tproxy-setup \
  /usr/local/sbin/sing-box-tproxy-setup \
  /usr/local/sbin/update-sing-box-rules-jsdelivr \
  /usr/local/sbin/monitor-sing-box-runtime
do
  copy_if_exists "$f" "$work/files/usr/local/sbin/"
done

for f in \
  /etc/systemd/system/sing-box.service \
  /etc/systemd/system/sing-box-tproxy.service \
  /etc/systemd/system/singbox-rule-ui.service \
  /etc/systemd/system/update-sing-box-rules-jsdelivr.service \
  /etc/systemd/system/update-sing-box-rules-jsdelivr.timer \
  /etc/systemd/system/monitor-sing-box-runtime.service \
  /etc/systemd/system/monitor-sing-box-runtime.timer
do
  copy_if_exists "$f" "$work/files/etc/systemd/system/"
done

copy_if_exists /etc/sysctl.d/99-sing-box-tproxy.conf "$work/files/etc/sysctl.d/"
copy_if_exists /etc/systemd/journald.conf.d/90-sing-box-gateway.conf "$work/files/etc/systemd/journald.conf.d/"

find "$work/files" -printf "%M %u %g %s %TY-%Tm-%Td %TH:%TM %p\n" | sed "s#$work/##" | sort > "$work/manifest/file-list.txt"

cat > "$work/README-BACKUP.txt" <<'EOF'
这是 sing-box 生产网关的离线恢复包。
恢复时不依赖 GitHub 仓库；按文章流程或 restore-sing-box-production.sh 把 files/ 下的路径复制回新机器。
本包通常包含代理节点、Rule UI token、Clash secret、自定义规则和真实网络地址，请只放在可信机器。
EOF

if [ ! -d "$work/files/etc/sing-box" ] || [ ! -d "$work/files/opt/singbox-rule-ui" ]; then
  echo "备份内容不完整：缺少 /etc/sing-box 或 /opt/singbox-rule-ui。" >&2
  exit 1
fi

tar -czpf "$archive" -C "$(dirname "$work")" "$(basename "$work")"
# sha256 文件只写包文件名，不写绝对路径；复制到新机器同一目录后可直接校验。
(cd "$output_dir" && sha256sum "$(basename "$archive")" > "$(basename "$archive").sha256")
chmod 600 "$archive" "$archive.sha256"

echo
echo "备份完成："
ls -lh "$archive" "$archive.sha256"
echo
cat "$archive.sha256"

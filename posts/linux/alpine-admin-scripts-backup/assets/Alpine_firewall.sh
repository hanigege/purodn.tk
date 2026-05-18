#!/bin/sh
# ============================================================
# Alpine Linux 严谨版防火墙脚本 (IPv4 专用)
# 针对 sing-box 性能优化 & 安全加固
# ============================================================

set -e

# 颜色输出
info='\033[32m[INFO]\033[0m'
warn='\033[33m[WARN]\033[0m'

echo -e "$info 安装 iptables 及其服务组件..."
apk add --no-cache iptables iptables-openrc

echo -e "$info 清空旧规则并预设安全策略..."
# 关键：先设为 ACCEPT，防止清理规则瞬间 SSH 掉线
iptables -P INPUT ACCEPT
iptables -F
iptables -X

# 默认策略：严防死守
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT ACCEPT

echo -e "$info 注入严谨优先级规则..."

# 1. 第一优先级：已建立的连接（确保 sing-box 现有流量直接通过，性能最高）
iptables -A INPUT -m state --state RELATED,ESTABLISHED -j ACCEPT

# 2. 第二优先级：本地回环 (127.0.0.1)
iptables -A INPUT -i lo -j ACCEPT

# 3. 开放 TCP 业务端口
for port in 22 80 443 8000 10521; do
  iptables -A INPUT -p tcp --dport $port -j ACCEPT
done

# 4. 开放 sing-box UDP 监听端口
iptables -A INPUT -p udp --dport 443 -j ACCEPT

# 5. ICMP (Ping) 控制：允许有限度的探测，拒绝洪水攻击
iptables -A INPUT -p icmp --icmp-type echo-request -m limit --limit 1/s --limit-burst 5 -j ACCEPT
iptables -A INPUT -p icmp -j DROP

echo -e "$info 正在持久化并启动服务..."
# Alpine 必须先保存到文件，否则 start 可能会加载旧配置
/etc/init.d/iptables save

# 设置开机自启
rc-update add iptables default

# 重启服务以确保内存规则与文件同步
rc-service iptables restart

echo -e "\n\033[32m✅ IPv4 防火墙加固完成！\033[0m"
echo "-------------------------------------------"
iptables -L INPUT -n -v
echo "-------------------------------------------"


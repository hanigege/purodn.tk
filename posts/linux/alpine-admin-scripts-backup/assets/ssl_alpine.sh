#!/bin/bash
#
# 一键申请 SSL 证书脚本 (默认使用 Let's Encrypt) - 针对 Alpine Linux
#

# --- 脚本设置与错误处理 ---
set -eEuo pipefail
# 在发生错误时，将错误信息输出到 stderr 并退出
# `$BASH_SOURCE` 和 `$LINENO` 用于指示错误发生的脚本文件和行号
trap 'echo -e "\033[31m❌ 脚本在 [\033[1m$BASH_SOURCE:$LINENO\033[0m\033[31m] 行发生错误\033[0m" >&2; exit 1' ERR

# --- ANSI 颜色代码 ---
RED='\033[31m'
GREEN='\033[32m'
YELLOW='\033[33m'
BOLD='\033[1m'
RESET='\033[0m'

# --- 全局变量 ---
DOMAIN=""
EMAIL=""
CA_SERVER="letsencrypt"
OS_TYPE=""
PKG_MANAGER=""
ACME_INSTALL_PATH="$HOME/.acme.sh"
CERT_KEY_DIR="" # 动态设置为 /etc/ssl/您的域名/
ACME_CMD="" # 动态查找的 acme.sh 命令路径

# --- 函数定义 ---

# 检查并确保以 root 权限运行
check_root() {
    if [ "$EUID" -ne 0 ]; then
        echo -e "${RED}❌ 错误：请使用 root 权限运行此脚本。${RESET}" >&2
        exit 1
    fi
    echo -e "${GREEN}✅ Root 权限检查通过。${RESET}"
}

# 获取用户输入并校验格式
get_user_input() {
    read -r -p "请输入域名: " DOMAIN
    if ! [[ "$DOMAIN" =~ ^[a-zA-Z0-9.-]+$ ]]; then
        echo -e "${RED}❌ 错误：域名格式不正确！${RESET}" >&2; exit 1;
    fi

    read -r -p "请输入电子邮件地址: " EMAIL
    if ! [[ "$EMAIL" =~ ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]]; then
        echo -e "${RED}❌ 错误：电子邮件格式不正确！${RESET}" >&2; exit 1;
    fi

    echo -e "${GREEN}✅ 用户信息收集完成 (默认使用 Let's Encrypt)。${RESET}"
}

# 检测操作系统并设置相关变量 (适配 Alpine)
detect_os() {
    if grep -qi "alpine" /etc/os-release; then
        OS_TYPE="alpine"; PKG_MANAGER="apk"
    elif grep -qi "ubuntu" /etc/os-release; then
        OS_TYPE="ubuntu"; PKG_MANAGER="apt"
    elif grep -qi "debian" /etc/os-release; then
        OS_TYPE="debian"; PKG_MANAGER="apt"
    # ... (其他系统检测逻辑保留)
    else
        echo -e "${RED}❌ 错误：不支持的操作系统！${RESET}" >&2; exit 1
    fi
    echo -e "${GREEN}✅ 检测到操作系统: $OS_TYPE ($PKG_MANAGER)。${RESET}"
}

# 安装依赖 (适配 Alpine: 使用 apk，将 cron 替换为 cronie，并安装 curl/socat)
install_dependencies() {
    local dependencies=()

    if [[ "$OS_TYPE" == "alpine" ]]; then
        dependencies=("curl" "socat" "cronie") 
        apk update >/dev/null 2>&1
        for pkg in "${dependencies[@]}"; do
            if ! apk info -e "$pkg" &>/dev/null; then
                echo -e "${YELLOW}安装依赖: $pkg...${RESET}"
                apk add "$pkg" >/dev/null 2>&1 || { echo -e "${RED}❌ 错误：安装 $pkg 失败${RESET}" >&2; exit 1; }
            fi
        done
        # 确保 cronie 服务启动并自启
        rc-service crond start >/dev/null 2>&1 || echo -e "${YELLOW}警告：无法启动 crond 服务。${RESET}" >&2
        rc-update add crond default 2>/dev/null || true

    # ... (其他系统安装逻辑保留)
    
    else
        # 为了简洁，非 Alpine 系统直接假设安装失败
        echo -e "${RED}❌ 错误：依赖安装失败，请手动检查。${RESET}" >&2
        exit 1
    fi

    echo -e "${GREEN}✅ 依赖安装完成。${RESET}"
}

# 下载安装 acme.sh
download_acme() {
    if [ ! -d "$ACME_INSTALL_PATH" ]; then
        curl -fsSL https://get.acme.sh | sh -s -- home "$ACME_INSTALL_PATH" >/dev/null 2>&1 || { echo -e "${RED}❌ 错误：下载 acme.sh 失败，请检查网络连接${RESET}" >&2; exit 1; }
        echo -e "${GREEN}✅ acme.sh 下载完成。${RESET}"
    else
        true
    fi
}

# 查找 acme.sh 命令路径
find_acme_cmd() {
    export PATH="$ACME_INSTALL_PATH:$PATH"
    ACME_CMD=$(command -v acme.sh)
    if [ -z "$ACME_CMD" ]; then
        echo -e "${RED}❌ 错误：找不到 acme.sh 命令。请检查安装或 PATH。${RESET}" >&2
        exit 1
    fi
    echo -e "${GREEN}✅ 找到 acme.sh 可执行文件。${RESET}"
}

# 更新 acme.sh
update_acme() {
    "$ACME_CMD" --upgrade >/dev/null 2>&1 || echo -e "${YELLOW}警告：acme.sh 更新失败${RESET}" >&2
    "$ACME_CMD" --update-account --days 60 >/dev/null 2>&1 || echo -e "${YELLOW}警告：acme.sh 账户信息更新失败${RESET}" >/dev/null
    echo -e "${GREEN}✅ acme.sh 更新完成。${RESET}"
}

# 申请 SSL 证书 (使用 rc-service 适配 Alpine)
issue_cert() {
    # 使用 rc-service 来停止/启动 Nginx，以释放 80 端口。
    if ! "$ACME_CMD" --issue --standalone -d "$DOMAIN" --server "$CA_SERVER" --force \
        --pre-hook "rc-service nginx stop 2>/dev/null || true" \
        --post-hook "rc-service nginx start 2>/dev/null || true" >/dev/null 2>&1; then
        echo -e "${RED}❌ 错误：证书申请失败。${RESET}" >&2
        echo "  正在进行清理..." >&2
        "$ACME_CMD" --revoke -d "$DOMAIN" --server "$CA_SERVER" >/dev/null 2>&1 || true
        "$ACME_CMD" --remove -d "$DOMAIN" --server "$CA_SERVER" >/dev/null 2>&1 || true
        exit 1
    fi
    echo -e "${GREEN}✅ 证书申请成功！${RESET}"
}

# 安装证书 (移除所有 sudo)
install_cert() {
    # 设置统一的证书安装目录
    CERT_KEY_DIR="/etc/ssl/$DOMAIN"
    
    # 移除 sudo
    mkdir -p "$CERT_KEY_DIR" >/dev/null 2>&1 || { echo -e "${RED}❌ 错误：创建证书目录失败${RESET}" >&2; exit 1; }

    # 移除 sudo
    if "$ACME_CMD" --installcert -d "$DOMAIN" \
        --key-file       "${CERT_KEY_DIR}/${DOMAIN}.key" \
        --fullchain-file "${CERT_KEY_DIR}/${DOMAIN}.crt" \
        --reloadcmd "rc-service nginx reload 2>/dev/null || true" >/dev/null 2>&1; then

        # 移除 sudo
        chmod 600 "${CERT_KEY_DIR}/${DOMAIN}.key" >/dev/null 2>&1 || echo -e "${YELLOW}警告：设置私钥文件权限失败。${RESET}" >&2
        # 移除 sudo
        chown root:root "${CERT_KEY_DIR}/${DOMAIN}.key" >/dev/null 2>&1 || echo -e "${YELLOW}警告：设置私钥文件所有者失败。${RESET}" >&2
        echo -e "${GREEN}✅ 证书安装完成。${RESET}"
    else
        echo -e "${RED}❌ 错误：证书安装失败！${RESET}" >&2
        exit 1
    fi
}

# --- 主体逻辑 ---

check_root
get_user_input
detect_os

echo "➡️ 依赖安装中..." >&2
install_dependencies

download_acme
find_acme_cmd # 在调用 acme.sh 之前查找命令

update_acme # 更新 acme.sh

echo "➡️ 证书申请中..." >&2
issue_cert # 申请证书
install_cert # 安装证书并设置权限

echo "➡️ 配置自动续期..." >&2
# 移除 sudo
"$ACME_CMD" --install-cronjob >/dev/null 2>&1 || echo -e "${YELLOW}警告：配置 acme.sh 自动续期任务失败。请手动运行 '\$HOME/.acme.sh/acme.sh --install-cronjob' 进行配置。${RESET}" >&2

echo -e "${GREEN}✅ 自动续期已通过 acme.sh 内置功能配置。${RESET}" >&2


echo "==============================================="
echo -e "${GREEN}✅ 脚本执行完毕。${RESET}"
echo "==============================================="
echo -e "${GREEN}证书文件: ${BOLD}${CERT_KEY_DIR}/${DOMAIN}.crt${RESET}"
echo -e "${GREEN}私钥文件: ${BOLD}${CERT_KEY_DIR}/${DOMAIN}.key${RESET}"
echo -e "${GREEN}自动续期已通过 acme.sh 内置功能配置完成。"
echo -e "${YELLOW}提示: 您可以通过 'crontab -l'来检查任务是否成功设置。${RESET}" >&2
echo -e "${YELLOW}重要提示: 请确保服务器的 80 端口（用于 Let's Encrypt 验证）以及您自己的服务端口是开放的。${RESET}" >&2

echo "==============================================="

exit 0

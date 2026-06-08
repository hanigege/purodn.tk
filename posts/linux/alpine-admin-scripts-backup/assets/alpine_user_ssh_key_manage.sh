#!/bin/ash

# 动态获取当前用户家目录。普通用户版只管理自己的 ~/.ssh，不改系统 sshd_config。
USER_HOME=$HOME

# 颜色定义
gl_lv='\033[32m'
gl_huang='\033[33m'
gl_hong='\033[31m'
gl_bai='\033[0m'
gl_hui='\e[37m'

# 检查依赖。普通用户无权使用 apk 安装，仅提示管理员补齐依赖。
init_check() {
    if ! command -v curl >/dev/null 2>&1 || ! command -v nano >/dev/null 2>&1; then
        echo -e "${gl_huang}警告: 系统缺失 curl 或 nano，部分功能受限，请联系管理员安装。${gl_bai}"
        sleep 2
    fi
}

# 获取 IP 地址
ip_address() {
    ipv4_address=$(curl -s --connect-timeout 5 https://ipinfo.io/ip)
    [ -z "$ipv4_address" ] && ipv4_address="VPS_IP"
}

# 确保目录权限正确
ensure_ssh_dir() {
    [ ! -d "$USER_HOME/.ssh" ] && mkdir -p "$USER_HOME/.ssh"
    chmod 700 "$USER_HOME/.ssh"
    [ ! -f "$USER_HOME/.ssh/authorized_keys" ] && touch "$USER_HOME/.ssh/authorized_keys"
    chmod 600 "$USER_HOME/.ssh/authorized_keys"
}

# 1. 生成新密钥对
add_sshkey() {
    ensure_ssh_dir
    local key_path="$USER_HOME/.ssh/sshkey"

    ssh-keygen -t ed25519 -C "${USER}@vps" -f "$key_path" -N ""
    cat "${key_path}.pub" >> "$USER_HOME/.ssh/authorized_keys"

    ip_address
    echo -e "\n${gl_lv}密钥对生成成功！${gl_bai}"
    echo -e "请保存私钥，建议文件名: ${gl_huang}${ipv4_address}_${USER}_ssh.key${gl_bai}"
    echo "------------------------------------------------"
    cat "$key_path"
    echo "------------------------------------------------"
    printf "\n按回车继续..."
    read dummy
}

# 2. 手动导入公钥
import_sshkey() {
    ensure_ssh_dir
    printf "${gl_hui}请粘贴公钥内容: ${gl_bai}"
    read pub_content
    if [ -z "$pub_content" ]; then
        echo -e "${gl_hong}错误：输入内容为空${gl_bai}"
        sleep 1
        return 1
    fi
    echo "$pub_content" >> "$USER_HOME/.ssh/authorized_keys"
    echo -e "${gl_lv}公钥导入完成${gl_bai}"
    sleep 1
}

# 3. 从 GitHub 导入
import_github() {
    ensure_ssh_dir
    printf "${gl_hui}请输入 GitHub 用户名: ${gl_bai}"
    read username
    if [ -n "$username" ]; then
        curl -fsSL "https://github.com/${username}.keys" >> "$USER_HOME/.ssh/authorized_keys"
        echo -e "${gl_lv}GitHub 公钥导入尝试完成${gl_bai}"
        sleep 1
    fi
}

# 主菜单
sshkey_panel() {
    init_check
    while true; do
        clear
        echo -e "Alpine SSH 密钥管理面板 (当前用户: ${gl_lv}${USER}${gl_bai})"
        echo "------------------------------------------------"
        echo "1. 生成新密钥对 (ED25519)"
        echo "2. 手动输入已有公钥"
        echo "3. 从 GitHub 导入公钥"
        echo "4. 编辑公钥文件 (authorized_keys)"
        echo "5. 查看当前密钥信息"
        echo "0. 退出"
        echo "------------------------------------------------"
        printf "请输入选择: "
        read choice
        case $choice in
            1) add_sshkey ;;
            2) import_sshkey ;;
            3) import_github ;;
            4) nano "$USER_HOME/.ssh/authorized_keys" ;;
            5)
                echo -e "\n${gl_huang}--- 已授权公钥 ---${gl_bai}"
                [ -s "$USER_HOME/.ssh/authorized_keys" ] && cat "$USER_HOME/.ssh/authorized_keys" || echo "为空"
                echo -e "\n${gl_huang}--- 本地私钥 (如有) ---${gl_bai}"
                [ -f "$USER_HOME/.ssh/sshkey" ] && cat "$USER_HOME/.ssh/sshkey" || echo "未找到"
                printf "\n按回车继续..."
                read dummy ;;
            0) exit 0 ;;
            *) echo "无效选择"; sleep 1 ;;
        esac
    done
}

sshkey_panel

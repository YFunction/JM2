#!/bin/bash
# ============================================================
#  JM2 Bot 一键安装脚本
#  自动完成: 系统依赖 → Python 环境 → jmcomic → NapCat → Bot → Dashboard
#  适用: Ubuntu 20.04+ / Debian 11+ / CentOS 8+
# ============================================================

set -e

# ── 颜色 ──
RED='\033[0;1;31m'
GREEN='\033[0;1;32m'
YELLOW='\033[0;1;33m'
BLUE='\033[0;1;34m'
CYAN='\033[0;1;36m'
NC='\033[0m'

# ── 默认配置 ──
INSTALL_DIR="${INSTALL_DIR:-$HOME/JM2}"
BOT_PORT="${BOT_PORT:-9001}"
DASHBOARD_PORT="${DASHBOARD_PORT:-9002}"
ONEBOT_PORT="${ONEBOT_PORT:-3000}"
BOT_QQ="${BOT_QQ:-}"
ALLOWED_GROUPS="${ALLOWED_GROUPS:-}"
NAPCAT_DIR="${NAPCAT_DIR:-$HOME/Napcat}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo -e "${CYAN}"
echo " ╔══════════════════════════════════════════════╗"
echo " ║        JM2 Bot 一键安装脚本 v1.0             ║"
echo " ║   JMComic QQ Bot + NapCat + Dashboard       ║"
echo " ╚══════════════════════════════════════════════╝"
echo -e "${NC}"

# ── 步骤 1: 检查系统 ──
step=1
total=7

echo -e "\n${BLUE}[${step}/${total}] 检查系统环境...${NC}"

if [ "$(uname -s)" != "Linux" ]; then
    echo -e "${RED}❌ 仅支持 Linux 系统${NC}"
    exit 1
fi

# 检测包管理器
if command -v apt-get &>/dev/null; then
    PKG_MGR="apt-get"
    PKG_INSTALL="apt-get install -y"
elif command -v dnf &>/dev/null; then
    PKG_MGR="dnf"
    PKG_INSTALL="dnf install -y"
elif command -v yum &>/dev/null; then
    PKG_MGR="yum"
    PKG_INSTALL="yum install -y"
else
    echo -e "${RED}❌ 未检测到 apt-get/dnf/yum，请手动安装依赖${NC}"
    exit 1
fi

echo -e "  ${GREEN}✓${NC} 系统: $(uname -sr)"
echo -e "  ${GREEN}✓${NC} 包管理器: ${PKG_MGR}"

# ── 交互式配置 ──
step=$((step + 1))
echo -e "\n${BLUE}[${step}/${total}] 配置参数...${NC}"

if [ -z "$BOT_QQ" ]; then
    read -p "  请输入机器人 QQ 号: " BOT_QQ
fi
echo -e "  ${GREEN}✓${NC} Bot QQ: ${BOT_QQ}"

if [ -z "$ALLOWED_GROUPS" ]; then
    read -p "  请输入允许使用的群号（多个用逗号分隔，回车=不限制）: " ALLOWED_GROUPS
fi
if [ -n "$ALLOWED_GROUPS" ]; then
    echo -e "  ${GREEN}✓${NC} 白名单群: ${ALLOWED_GROUPS}"
else
    echo -e "  ${YELLOW}⚠${NC} 白名单未设置（所有群可用）"
fi

read -p "  请输入安装目录 [${INSTALL_DIR}]: " input_dir
INSTALL_DIR="${input_dir:-$INSTALL_DIR}"
echo -e "  ${GREEN}✓${NC} 安装目录: ${INSTALL_DIR}"

# ── 步骤 3: 安装系统依赖 ──
step=$((step + 1))
echo -e "\n${BLUE}[${step}/${total}] 安装系统依赖...${NC}"

SYSTEM_DEPS="python3 python3-pip python3-venv curl wget git screen xvfb xauth"
# 特定包管理器的额外依赖
case $PKG_MGR in
    apt-get)
        SYSTEM_DEPS="$SYSTEM_DEPS libgbm1 libasound2 libnss3 libxss1 libxtst6 libgtk-3-0 libx11-xcb1 libxcb-dri3-0 libdrm2 libxshmfence1"
        ;;
    dnf|yum)
        SYSTEM_DEPS="$SYSTEM_DEPS libgbm alsa-lib nss libXScrnSaver libXtst gtk3 libX11-xcb libxcb libdrm"
        ;;
esac

echo "  正在安装: ${SYSTEM_DEPS}..."
if [ "$EUID" -eq 0 ]; then
    $PKG_INSTALL $SYSTEM_DEPS 2>&1 | tail -3
else
    sudo $PKG_INSTALL $SYSTEM_DEPS 2>&1 | tail -3
fi
echo -e "  ${GREEN}✓${NC} 系统依赖安装完成"

# ── 步骤 4: 安装 Python 依赖 ──
step=$((step + 1))
echo -e "\n${BLUE}[${step}/${total}] 安装 Python 依赖...${NC}"

cd "$INSTALL_DIR" 2>/dev/null || {
    echo -e "  ${YELLOW}⚠${NC} 目录 $INSTALL_DIR 不存在，正在创建..."
    mkdir -p "$INSTALL_DIR"
    cd "$INSTALL_DIR"
}

# 创建虚拟环境（可选）
if [ "${USE_VENV:-yes}" = "yes" ]; then
    VENV_DIR="$INSTALL_DIR/.venv"
    if [ ! -d "$VENV_DIR" ]; then
        echo "  创建 Python 虚拟环境..."
        $PYTHON_BIN -m venv "$VENV_DIR"
    fi
    source "$VENV_DIR/bin/activate"
    echo -e "  ${GREEN}✓${NC} 虚拟环境: ${VENV_DIR}"
fi

# 升级 pip
echo "  升级 pip..."
pip install --upgrade pip -q 2>&1 | tail -1

# 安装 jmcomic
echo "  安装 jmcomic..."
if [ -f "$INSTALL_DIR/setup.py" ]; then
    pip install -e "$INSTALL_DIR" --no-build-isolation -q 2>&1 | tail -1
else
    pip install jmcomic -q 2>&1 | tail -1
fi

# 安装 Bot 依赖
echo "  安装 Bot 依赖..."
pip install flask requests img2pdf psutil waitress -q 2>&1 | tail -1

echo -e "  ${GREEN}✓${NC} Python 依赖安装完成"

# ── 步骤 5: 安装/配置 NapCat ──
step=$((step + 1))
echo -e "\n${BLUE}[${step}/${total}] 配置 NapCat (QQ Bot 框架)...${NC}"

NEED_NAPCAT_INSTALL=false

if [ -f "$NAPCAT_DIR/opt/QQ/qq" ]; then
    echo -e "  ${GREEN}✓${NC} NapCat 已安装在: ${NAPCAT_DIR}"
else
    NEED_NAPCAT_INSTALL=true
fi

if [ "$NEED_NAPCAT_INSTALL" = true ]; then
    echo -e "  ${YELLOW}⚠${NC} NapCat 未安装"
    if [ -f "$INSTALL_DIR/napcat.sh" ]; then
        echo "  运行 napcat.sh 安装 NapCat..."
        read -p "  是否自动安装 NapCat？[Y/n] " yn
        yn="${yn:-Y}"
        if [[ "$yn" =~ ^[Yy] ]]; then
            bash "$INSTALL_DIR/napcat.sh"
        else
            echo -e "  ${YELLOW}⚠${NC} 请手动安装 NapCat 后重新运行此脚本"
        fi
    else
        echo -e "  ${YELLOW}⚠${NC} 未找到 napcat.sh"
        echo "  请参考: https://github.com/NapNeko/NapCatQQ"
        echo "  安装命令: curl -fsSL https://ncl.ink/napcat.sh | bash"
    fi
fi

# 配置 NapCat OneBot HTTP
NAPCAT_CONFIG_DIR=$(find "$HOME" -maxdepth 3 -path "*/NapCat/config" -type d 2>/dev/null | head -1)
if [ -z "$NAPCAT_CONFIG_DIR" ]; then
    NAPCAT_CONFIG_DIR="$HOME/.napcat/config"
fi

ONEBOT_CONFIG="$NAPCAT_CONFIG_DIR/onebot11_${BOT_QQ}.json"
echo "  OneBot 配置: ${ONEBOT_CONFIG}"

if [ ! -f "$ONEBOT_CONFIG" ]; then
    mkdir -p "$NAPCAT_CONFIG_DIR"
    cat > "$ONEBOT_CONFIG" << ONEBOTEOF
{
    "network": {
        "httpServers": [
            {
                "name": "JM2Bot",
                "enable": true,
                "port": ${ONEBOT_PORT},
                "host": "0.0.0.0",
                "enableCors": true,
                "enableWebsocket": true,
                "enableHttpPost": true,
                "enableHttpHeart": true,
                "httpPostUrls": ["http://127.0.0.1:${BOT_PORT}/onebot"],
                "accessToken": ""
            }
        ],
        "httpClients": [],
        "websocketServers": [],
        "websocketClients": []
    }
}
ONEBOTEOF
    echo -e "  ${GREEN}✓${NC} OneBot 配置已创建"
else
    echo -e "  ${GREEN}✓${NC} OneBot 配置已存在"
fi

# ── 步骤 6: 创建 systemd 服务 ──
step=$((step + 1))
echo -e "\n${BLUE}[${step}/${total}] 创建 systemd 服务...${NC}"

SYSTEMD_DIR="/etc/systemd/system"
HAS_SYSTEMD=false
if [ -d "$SYSTEMD_DIR" ]; then
    HAS_SYSTEMD=true
fi

if [ "$HAS_SYSTEMD" = true ]; then
    VENV_PYTHON="$INSTALL_DIR/.venv/bin/python3"
    if [ "${USE_VENV:-yes}" != "yes" ] || [ ! -f "$VENV_PYTHON" ]; then
        VENV_PYTHON="$($PYTHON_BIN -c 'import sys; print(sys.executable)')"
    fi

    # jm2bot 服务
    cat > /tmp/jm2bot.service << SVCEOF
[Unit]
Description=JM2 QQ Bot Server
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${INSTALL_DIR}
Environment="PYTHONUNBUFFERED=1"
Environment="PORT=${BOT_PORT}"
Environment="BOT_QQ=${BOT_QQ}"
Environment="ALLOWED_GROUPS=${ALLOWED_GROUPS}"
Environment="ONEBOT_BASE_URL=http://127.0.0.1:${ONEBOT_PORT}"
Environment="DOWNLOAD_OUTPUT_DIR=${INSTALL_DIR}/downloads"
ExecStart=${VENV_PYTHON} -u ${INSTALL_DIR}/bot_download_server.py
Restart=always
RestartSec=10
StandardOutput=append:/tmp/bot.log
StandardError=append:/tmp/bot.log

[Install]
WantedBy=multi-user.target
SVCEOF
    sudo mv /tmp/jm2bot.service "$SYSTEMD_DIR/jm2bot.service"

    # jm2dashboard 服务
    cat > /tmp/jm2dashboard.service << SVCEOF
[Unit]
Description=JM2 Bot Dashboard
After=network.target jm2bot.service

[Service]
Type=simple
User=${USER}
WorkingDirectory=${INSTALL_DIR}
Environment="DASHBOARD_PORT=${DASHBOARD_PORT}"
Environment="BOT_PORT=${BOT_PORT}"
Environment="BOT_LOG=/tmp/bot.log"
ExecStart=${VENV_PYTHON} -u ${INSTALL_DIR}/bot_dashboard.py
Restart=always
RestartSec=5
StandardOutput=append:/tmp/dashboard.log
StandardError=append:/tmp/dashboard.log

[Install]
WantedBy=multi-user.target
SVCEOF
    sudo mv /tmp/jm2dashboard.service "$SYSTEMD_DIR/jm2dashboard.service"

    # NapCat 服务
    cat > /tmp/napcat.service << SVCEOF
[Unit]
Description=NapCat QQ Bot
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${NAPCAT_DIR}/opt/QQ
Environment="DISPLAY=:99"
ExecStartPre=/usr/bin/Xvfb :99 -screen 0 1024x768x16 &
ExecStart=${NAPCAT_DIR}/opt/QQ/qq --no-sandbox -q ${BOT_QQ}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SVCEOF
    sudo mv /tmp/napcat.service "$SYSTEMD_DIR/napcat.service"

    sudo systemctl daemon-reload
    echo -e "  ${GREEN}✓${NC} 已创建 3 个 systemd 服务:"
    echo "     - jm2bot.service      (Bot 主服务, 端口 ${BOT_PORT})"
    echo "     - jm2dashboard.service (监控面板, 端口 ${DASHBOARD_PORT})"
    echo "     - napcat.service      (NapCat QQ 框架)"
else
    echo -e "  ${YELLOW}⚠${NC} 未检测到 systemd，将使用启动脚本"
    cat > "$INSTALL_DIR/start_all.sh" << 'STARTALL'
#!/bin/bash
cd "$(dirname "$0")"

echo "启动 NapCat..."
screen -dmS napcat bash -c "xvfb-run -a $HOME/Napcat/opt/QQ/qq --no-sandbox -q $BOT_QQ 2>&1 | tee -a /tmp/napcat.log"

echo "启动 JM2 Bot..."
PYTHONUNBUFFERED=1 nohup python3 -u bot_download_server.py > /tmp/bot.log 2>&1 &

echo "启动监控面板..."
nohup python3 -u bot_dashboard.py > /tmp/dashboard.log 2>&1 &

sleep 3
echo "✅ 全部已启动"
echo "   Bot: http://127.0.0.1:${BOT_PORT:-9001}"
echo "   面板: http://127.0.0.1:${DASHBOARD_PORT:-9002}"
STARTALL
    chmod +x "$INSTALL_DIR/start_all.sh"
    echo -e "  ${GREEN}✓${NC} 已创建 start_all.sh"
fi

# ── 创建必要目录 ──
mkdir -p "$INSTALL_DIR/downloads" "$INSTALL_DIR/logs/chat"

# ── 保存配置 ──
cat > "$INSTALL_DIR/.env" << ENVEOF
# JM2 Bot 配置 — 由 install.sh 自动生成
# 修改后重启服务生效: sudo systemctl restart jm2bot

BOT_QQ=${BOT_QQ}
ALLOWED_GROUPS=${ALLOWED_GROUPS}
PORT=${BOT_PORT}
DASHBOARD_PORT=${DASHBOARD_PORT}
ONEBOT_PORT=${ONEBOT_PORT}
ONEBOT_BASE_URL=http://127.0.0.1:${ONEBOT_PORT}
DOWNLOAD_OUTPUT_DIR=${INSTALL_DIR}/downloads
WORKER_THREADS=4
ENVEOF
echo -e "  ${GREEN}✓${NC} 配置已保存到 .env"

# ── 步骤 7: 启动服务 ──
step=$((step + 1))
echo -e "\n${BLUE}[${step}/${total}] 启动服务...${NC}"

if [ "$HAS_SYSTEMD" = true ]; then
    echo "  正在启动 NapCat (首次需要扫码登录)..."
    sudo systemctl start napcat 2>/dev/null || {
        echo -e "  ${YELLOW}⚠${NC} napcat.service 可能启动失败，请手动启动 QQ 并扫码登录"
    }

    echo "  启动 JM2 Bot..."
    sudo systemctl enable --now jm2bot 2>/dev/null
    sudo systemctl enable --now jm2dashboard 2>/dev/null

    sleep 2

    echo ""
    echo -e "  ${GREEN}✓${NC} 服务已启动并设为开机自启"
    echo ""
    echo -e "  ${CYAN}管理命令:${NC}"
    echo "    sudo systemctl status jm2bot       # 查看 Bot 状态"
    echo "    sudo systemctl status jm2dashboard  # 查看面板状态"
    echo "    sudo systemctl status napcat        # 查看 NapCat 状态"
    echo "    sudo journalctl -u jm2bot -f        # 实时查看 Bot 日志"
    echo "    sudo systemctl restart jm2bot       # 重启 Bot"
    echo "    sudo systemctl stop jm2bot          # 停止 Bot"
fi

# ── 完成 ──
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║        🎉 JM2 Bot 安装完成！                 ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${CYAN}📊 监控面板:${NC}  http://${LOCAL_IP:-127.0.0.1}:${DASHBOARD_PORT}"
echo -e "  ${CYAN}🤖 Bot API:${NC}   http://127.0.0.1:${BOT_PORT}"
echo -e "  ${CYAN}📋 配置文件:${NC}   ${INSTALL_DIR}/.env"
echo -e "  ${CYAN}📜 Bot 日志:${NC}  /tmp/bot.log"
echo ""
echo -e "  ${YELLOW}⚠️  首次使用需要在 NapCat WebUI 扫码登录 QQ${NC}"
echo -e "  ${YELLOW}    NapCat WebUI: http://127.0.0.1:6099/webui${NC}"
echo -e "  ${YELLOW}    登录后执行: sudo systemctl restart napcat${NC}"
echo ""
echo -e "  ${CYAN}使用帮助:${NC} 在 QQ 群中发送 /help 即可查看全部指令"

#!/bin/bash
# 紧急重启 Bot（保留白名单和QQ号配置）
cd /root/JM2

# 读取当前进程的环境变量
BOT_QQ="${BOT_QQ:-2837430647}"
ALLOWED_GROUPS="${ALLOWED_GROUPS:-983855437,376921790}"

# 停止
fuser -k 9001/tcp 2>/dev/null
sleep 1

# 启动
PYTHONUNBUFFERED=1 BOT_QQ="$BOT_QQ" ALLOWED_GROUPS="$ALLOWED_GROUPS" \
  nohup python3 -u bot_download_server.py > /tmp/bot.log 2>&1 &

sleep 2
if pgrep -f bot_download_server > /dev/null; then
    echo "✅ Bot 已重启 (PID: $(pgrep -f bot_download_server | head -1))"
    echo "   白名单: $ALLOWED_GROUPS"
else
    echo "❌ Bot 启动失败，查看日志: tail -20 /tmp/bot.log"
fi

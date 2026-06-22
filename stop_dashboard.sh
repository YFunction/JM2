#!/bin/bash
# 停止 JM2 Bot 监控面板
if pgrep -f "bot_dashboard.py" > /dev/null; then
    pkill -f "bot_dashboard.py"
    sleep 1
    if pgrep -f "bot_dashboard.py" > /dev/null; then
        pkill -9 -f "bot_dashboard.py"
    fi
    echo "✅ 监控面板已停止"
else
    echo "⚠️ 监控面板未在运行"
fi

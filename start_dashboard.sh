#!/bin/bash
# 启动 JM2 Bot 监控面板
cd /root/JM2
DASHBOARD_PORT="${DASHBOARD_PORT:-9002}"
DASHBOARD_THREADS="${DASHBOARD_THREADS:-2}"

# 检查是否已经在运行
if pgrep -f "bot_dashboard.py" > /dev/null; then
    echo "⚠️ 监控面板已在运行 (PID: $(pgrep -f bot_dashboard.py | head -1))"
    echo "   访问: http://$(hostname -I 2>/dev/null | awk '{print $1}'):$DASHBOARD_PORT"
    exit 0
fi

# 启动
DASHBOARD_PORT="$DASHBOARD_PORT" DASHBOARD_THREADS="$DASHBOARD_THREADS" \
  nohup python3 -u bot_dashboard.py > /tmp/dashboard.log 2>&1 &

sleep 1
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if pgrep -f "bot_dashboard.py" > /dev/null; then
    echo "✅ 监控面板已启动"
    echo "   PID: $(pgrep -f bot_dashboard.py | head -1)"
    echo "   端口: $DASHBOARD_PORT"
    echo "   本地: http://127.0.0.1:$DASHBOARD_PORT"
    [ -n "$LOCAL_IP" ] && echo "   局域网: http://$LOCAL_IP:$DASHBOARD_PORT"
else
    echo "❌ 启动失败，查看日志: tail -20 /tmp/dashboard.log"
    exit 1
fi

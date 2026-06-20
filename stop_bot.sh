#!/bin/bash
# 紧急停止 Bot
fuser -k 9001/tcp 2>/dev/null && echo "✅ Bot 已停止" || echo "⚠️ 未找到运行中的 Bot"

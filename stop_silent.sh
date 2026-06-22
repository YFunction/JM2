#!/bin/bash
# 终止所有静默下载任务
curl -s -X POST http://127.0.0.1:9001/admin/stop-silent | python3 -m json.tool 2>/dev/null
pkill -f "download_album_to_pdf" 2>/dev/null
echo "✅ 静默任务已全部终止"

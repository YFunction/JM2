#!/bin/bash
# 开启/恢复静默下载任务
curl -s -X POST http://127.0.0.1:9001/admin/start-silent | python3 -m json.tool 2>/dev/null
echo "✅ 静默任务已恢复"

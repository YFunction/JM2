#!/bin/bash
# 重启 JM2 Bot 监控面板
cd /root/JM2
bash stop_dashboard.sh
sleep 1
bash start_dashboard.sh

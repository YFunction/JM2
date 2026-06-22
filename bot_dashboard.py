#!/usr/bin/env python3
"""JM2 Bot 监控面板 — 独立 Web 服务，查看服务器状态、控制台输出、下载队列等。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)

# ── 配置 ──
BOT_PORT = int(os.getenv("BOT_PORT", "9001"))
BOT_URL = f"http://127.0.0.1:{BOT_PORT}"
BOT_LOG_PATH = Path(os.getenv("BOT_LOG", "/tmp/bot.log"))
PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "downloads"
LOGS_DIR = PROJECT_DIR / "logs"

# NapCat 相关
NAPCAT_PATH = Path(os.path.expanduser("~/Napcat"))


# ── 工具函数 ──
def _bot_alive() -> bool:
    """检测 Bot 进程是否存活。"""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "bot_download_server"],
            capture_output=True, text=True, timeout=5
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def _bot_health() -> dict:
    """调用 Bot 的 /health 端点。"""
    try:
        resp = requests.get(f"{BOT_URL}/health", timeout=5)
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _bot_queue_status() -> dict:
    """获取下载调度器状态（通过执行内联 Python 获取）。"""
    try:
        # 通过 Bot 进程直接查询状态（避免新增 API 端点）
        code = """
import sys, json
sys.path.insert(0, '/root/JM2')
from bot_download_server import _downloader, _load_downloaded_ids, OUTPUT_DIR, _get_pending_albums
status = _downloader.get_status()
pending = _get_pending_albums()
pdfs = [p.name for p in OUTPUT_DIR.glob('[JM*]*.pdf')]
total_size = sum(p.stat().st_size for p in OUTPUT_DIR.glob('[JM*]*.pdf'))
print(json.dumps({
    'active': [{'id': aid, 'priority': p} for aid, p in status['active']],
    'priority_queue': status['priority'][:20],
    'silent_queue': status['silent'][:20],
    'downloaded_count': len(status['downloaded']),
    'pending_count': len(pending),
    'pdf_count': len(pdfs),
    'total_size_mb': round(total_size / 1024 / 1024, 1),
    'silent_queue_total': len(status['silent']),
    'priority_queue_total': len(status['priority']),
}))
"""
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=15,
            cwd=str(PROJECT_DIR)
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout.strip())
        return {"error": proc.stderr[:500] if proc.stderr else "no output"}
    except Exception as e:
        return {"error": str(e)}


def _get_system_info() -> dict:
    """获取系统资源信息。"""
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        boot = datetime.fromtimestamp(psutil.boot_time()).strftime("%Y-%m-%d %H:%M")
        net = psutil.net_io_counters()
        return {
            "cpu_percent": cpu,
            "mem_percent": mem.percent,
            "mem_used_gb": round(mem.used / 1024**3, 2),
            "mem_total_gb": round(mem.total / 1024**3, 2),
            "disk_percent": disk.percent,
            "disk_free_gb": round(disk.free / 1024**3, 1),
            "disk_total_gb": round(disk.total / 1024**3, 1),
            "boot_time": boot,
            "net_sent_mb": round(net.bytes_sent / 1024**2, 1),
            "net_recv_mb": round(net.bytes_recv / 1024**2, 1),
        }
    except ImportError:
        return {"error": "psutil 未安装"}
    except Exception as e:
        return {"error": str(e)}


def _tail_log(lines: int = 100) -> str:
    """读取日志尾部 N 行。"""
    if not BOT_LOG_PATH.exists():
        return "[日志文件不存在]"
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), str(BOT_LOG_PATH)],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout or "(空)"
    except Exception as e:
        return f"[读取日志失败: {e}]"


def _tail_chat_log(chat_type: str = "all", lines: int = 50) -> list:
    """读取最近的聊天记录。chat_type: group/private/all"""
    chat_dir = LOGS_DIR / "chat"
    if not chat_dir.exists():
        return []
    entries = []
    pattern = "*.log" if chat_type == "all" else f"{chat_type}_*.log"
    log_files = sorted(chat_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
    for lf in log_files:
        try:
            with open(lf, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
            for line in all_lines[-lines:]:
                entries.append(line.strip())
        except Exception:
            pass
    entries.sort(reverse=True)
    return entries[-lines:]


def _napcat_status() -> dict:
    """检测 NapCat 进程状态。"""
    try:
        result = subprocess.run(
            ["pgrep", "-af", "napcat|NapCat|qq"],
            capture_output=True, text=True, timeout=5
        )
        processes = [l.strip() for l in result.stdout.strip().split("\n") if l.strip() and "pgrep" not in l]
        return {"running": len(processes) > 0, "processes": processes[:5], "count": len(processes)}
    except Exception as e:
        return {"running": False, "error": str(e)}


def _get_settings() -> dict:
    """获取 Bot 的设置。"""
    info = {}
    try:
        code = """
import json, os
from pathlib import Path
info = {
    'bot_qq': os.getenv('BOT_QQ', ''),
    'allowed_groups': os.getenv('ALLOWED_GROUPS', ''),
    'napcat_url': os.getenv('ONEBOT_BASE_URL', 'http://127.0.0.1:3000'),
    'output_dir': str(os.getenv('DOWNLOAD_OUTPUT_DIR', '/root/JM2/downloads')),
    'port': os.getenv('PORT', '9001'),
    'worker_threads': os.getenv('WORKER_THREADS', '4'),
}
print(json.dumps(info))
"""
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=10
        )
        if proc.stdout.strip():
            return json.loads(proc.stdout.strip())
    except Exception:
        pass
    return info


# ── API 路由 ──

@app.route("/api/status")
def api_status():
    """综合状态 API。"""
    bot_alive = _bot_alive()
    health = _bot_health() if bot_alive else {"error": "Bot 未运行"}
    queue = _bot_queue_status() if bot_alive else {}
    system = _get_system_info()
    napcat = _napcat_status()
    settings = _get_settings()

    return jsonify({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "bot_alive": bot_alive,
        "bot_health": health,
        "queue": queue,
        "system": system,
        "napcat": napcat,
        "settings": settings,
    })


@app.route("/api/log")
def api_log():
    """获取 Bot 日志尾部。"""
    lines = request.args.get("lines", 100, type=int)
    lines = max(1, min(lines, 500))
    content = _tail_log(lines)
    return jsonify({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "log_file": str(BOT_LOG_PATH),
        "lines": lines,
        "content": content,
    })


@app.route("/api/chat")
def api_chat():
    """获取最近聊天记录。"""
    chat_type = request.args.get("type", "all")
    lines = request.args.get("lines", 30, type=int)
    entries = _tail_chat_log(chat_type, lines)
    return jsonify({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "entries": entries,
    })


@app.route("/api/action/<action>", methods=["POST"])
def api_action(action):
    """执行管理操作。"""
    actions = {
        "stop-silent": {
            "url": f"{BOT_URL}/admin/stop-silent",
            "desc": "停止静默下载"
        },
        "start-silent": {
            "url": f"{BOT_URL}/admin/start-silent",
            "desc": "启动静默下载"
        },
    }
    if action == "restart":
        script = PROJECT_DIR / "restart_bot.sh"
        try:
            result = subprocess.run(
                ["bash", str(script)],
                capture_output=True, text=True, timeout=30,
                cwd=str(PROJECT_DIR),
                env={**os.environ, "BOT_QQ": os.getenv("BOT_QQ", ""),
                     "ALLOWED_GROUPS": os.getenv("ALLOWED_GROUPS", "")}
            )
            return jsonify({"ok": True, "action": "restart",
                            "stdout": result.stdout, "stderr": result.stderr})
        except Exception as e:
            return jsonify({"ok": False, "action": "restart", "error": str(e)}), 500

    if action not in actions:
        return jsonify({"ok": False, "error": f"未知操作: {action}"}), 400

    act = actions[action]
    try:
        resp = requests.post(act["url"], timeout=10)
        return jsonify({"ok": True, "action": action, "desc": act["desc"],
                        "bot_response": resp.json() if resp.text else {}})
    except Exception as e:
        return jsonify({"ok": False, "action": action, "desc": act["desc"],
                        "error": str(e)}), 500


@app.route("/api/downloaded")
def api_downloaded():
    """获取已下载列表。"""
    try:
        code = """
import json, os
from pathlib import Path
output = Path('/root/JM2/downloads')
downloaded_log = Path('/root/JM2/logs/downloaded.txt')
ids_set = set()
if downloaded_log.exists():
    with open(downloaded_log) as f:
        for line in f:
            line = line.strip()
            if line.isdigit():
                ids_set.add(line)
pdfs = []
for pdf in sorted(output.glob('[JM*]*.pdf'), key=lambda p: p.stat().st_mtime, reverse=True):
    pdfs.append({
        'name': pdf.name,
        'size_mb': round(pdf.stat().st_size / 1024 / 1024, 1),
        'mtime': pdf.stat().st_mtime,
    })
print(json.dumps({'count': len(pdfs), 'pdfs': pdfs[:50], 'total_ids': len(ids_set)}))
"""
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=10
        )
        return jsonify(json.loads(proc.stdout.strip()) if proc.stdout.strip() else {})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 前端页面 ──

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JM2 Bot 监控面板</title>
<style>
  :root {
    --bg: #0d1117;
    --bg-card: #161b22;
    --bg-hover: #1c2129;
    --border: #30363d;
    --text: #c9d1d9;
    --text-dim: #8b949e;
    --green: #3fb950;
    --red: #f85149;
    --yellow: #d2991d;
    --blue: #58a6ff;
    --purple: #bc8cff;
    --orange: #f0883e;
    --cyan: #39d2c0;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans SC', sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    line-height: 1.5;
  }
  .header {
    background: var(--bg-card);
    border-bottom: 1px solid var(--border);
    padding: 12px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 100;
  }
  .header h1 {
    font-size: 18px;
    font-weight: 600;
    color: var(--text);
  }
  .header .status-dot {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    margin-right: 8px;
    animation: pulse 2s infinite;
  }
  .status-dot.online { background: var(--green); }
  .status-dot.offline { background: var(--red); animation: none; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }
  .header-right { display:flex; gap:12px; align-items:center; font-size:13px; color:var(--text-dim); }
  .main {
    max-width: 1400px;
    margin: 0 auto;
    padding: 20px 24px;
    display: grid;
    grid-template-columns: 1fr 1fr;
    grid-template-rows: auto auto auto;
    gap: 16px;
  }
  .card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    overflow: hidden;
  }
  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
  }
  .card-header h2 { font-size: 14px; font-weight: 600; }
  .card-header .badge {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 500;
  }
  .badge-green { background: rgba(63,185,80,0.15); color: var(--green); }
  .badge-red { background: rgba(248,81,73,0.15); color: var(--red); }
  .badge-yellow { background: rgba(210,153,29,0.15); color: var(--yellow); }
  .badge-blue { background: rgba(88,166,255,0.15); color: var(--blue); }
  .full-width { grid-column: 1 / -1; }
  .log-viewer {
    background: #0a0e13;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px;
    font-family: 'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace;
    font-size: 12px;
    line-height: 1.6;
    max-height: 400px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
    color: #a5b3c6;
  }
  .log-viewer .highlight { color: var(--yellow); font-weight: bold; }
  .log-viewer .error { color: var(--red); }
  .log-viewer .info { color: var(--blue); }
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 10px;
  }
  .stat-item {
    background: var(--bg);
    border-radius: 6px;
    padding: 12px;
    text-align: center;
  }
  .stat-value {
    font-size: 24px;
    font-weight: 700;
    line-height: 1.2;
  }
  .stat-label {
    font-size: 11px;
    color: var(--text-dim);
    margin-top: 4px;
  }
  .stat-value.green { color: var(--green); }
  .stat-value.red { color: var(--red); }
  .stat-value.yellow { color: var(--yellow); }
  .stat-value.blue { color: var(--blue); }
  .stat-value.cyan { color: var(--cyan); }
  .btn-group { display:flex; gap:8px; flex-wrap:wrap; }
  .btn {
    padding: 6px 14px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg);
    color: var(--text);
    cursor: pointer;
    font-size: 12px;
    font-weight: 500;
    transition: all 0.15s;
  }
  .btn:hover { background: var(--bg-hover); border-color: #555; }
  .btn-danger { border-color: rgba(248,81,73,0.4); color: var(--red); }
  .btn-danger:hover { background: rgba(248,81,73,0.1); }
  .btn-primary { border-color: rgba(88,166,255,0.4); color: var(--blue); }
  .btn-primary:hover { background: rgba(88,166,255,0.1); }
  .btn-success { border-color: rgba(63,185,80,0.4); color: var(--green); }
  .btn-success:hover { background: rgba(63,185,80,0.1); }
  .btn:disabled { opacity:0.5; cursor:not-allowed; }
  .queue-list {
    list-style: none;
    max-height: 300px;
    overflow-y: auto;
  }
  .queue-list li {
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 12px;
    font-family: monospace;
  }
  .queue-list li:hover { background: var(--bg-hover); }
  .queue-list .pri { color: var(--red); }
  .queue-list .silent { color: var(--text-dim); }
  .queue-list .active { color: var(--yellow); }
  .info-table {
    width: 100%;
    font-size: 12px;
  }
  .info-table td {
    padding: 3px 8px;
    vertical-align: top;
  }
  .info-table td:first-child {
    color: var(--text-dim);
    white-space: nowrap;
    width: 100px;
  }
  .info-table td:last-child {
    font-family: monospace;
    word-break: break-all;
  }
  .toast {
    position: fixed;
    top: 60px;
    right: 20px;
    padding: 10px 20px;
    border-radius: 6px;
    font-size: 13px;
    z-index: 200;
    animation: slideIn 0.3s ease;
    opacity: 0.95;
  }
  .toast-success { background: var(--green); color: #000; }
  .toast-error { background: var(--red); color: #fff; }
  @keyframes slideIn { from{transform:translateX(100%)} to{transform:translateX(0)} }
  .refresh-indicator {
    font-size: 11px;
    color: var(--text-dim);
    animation: fadeInOut 3s;
  }
  @keyframes fadeInOut { 0%{opacity:0} 50%{opacity:1} 100%{opacity:0.3} }
  @media (max-width: 900px) {
    .main { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>

<div class="header">
  <div>
    <span class="status-dot online" id="statusDot"></span>
    <h1 style="display:inline;">JM2 Bot 监控面板</h1>
  </div>
  <div class="header-right">
    <span id="clock">--</span>
    <span style="color:var(--text-dim)">|</span>
    <span id="uptime">--</span>
    <span style="color:var(--text-dim)">|</span>
    <button class="btn btn-primary" onclick="location.reload()" title="强制刷新">🔄 刷新</button>
  </div>
</div>

<div class="main">

  <!-- 左侧列 -->
  <div class="card full-width">
    <div class="card-header">
      <h2>📊 系统概览</h2>
      <span id="badgeBot" class="badge badge-red">检测中...</span>
    </div>
    <div class="stats-grid" id="systemStats">
      <div class="stat-item"><div class="stat-value">--</div><div class="stat-label">Bot 状态</div></div>
      <div class="stat-item"><div class="stat-value blue">--</div><div class="stat-label">CPU</div></div>
      <div class="stat-item"><div class="stat-value blue">--</div><div class="stat-label">内存</div></div>
      <div class="stat-item"><div class="stat-value blue">--</div><div class="stat-label">磁盘</div></div>
      <div class="stat-item"><div class="stat-value green">--</div><div class="stat-label">已下载</div></div>
      <div class="stat-item"><div class="stat-value yellow">--</div><div class="stat-label">待下载</div></div>
      <div class="stat-item"><div class="stat-value cyan">--</div><div class="stat-label">PDF 文件</div></div>
      <div class="stat-item"><div class="stat-value">--</div><div class="stat-label">下载总量</div></div>
    </div>
  </div>

  <!-- 控制台日志 -->
  <div class="card full-width">
    <div class="card-header">
      <h2>📜 控制台输出 (Bot 日志)</h2>
      <div style="display:flex; gap:8px; align-items:center;">
        <span id="logRefresh" class="refresh-indicator"></span>
        <select id="logLines" onchange="refreshLog()" style="background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:2px 8px;font-size:11px;">
          <option value="50">50 行</option>
          <option value="100" selected>100 行</option>
          <option value="200">200 行</option>
          <option value="500">500 行</option>
        </select>
        <button class="btn" onclick="refreshLog()">🔄</button>
      </div>
    </div>
    <div class="log-viewer" id="logViewer">加载中...</div>
  </div>

  <!-- 下载队列 & 操作按钮 -->
  <div class="card">
    <div class="card-header">
      <h2>📥 下载队列</h2>
      <span id="queueBadge" class="badge badge-blue">--</span>
    </div>
    <div style="margin-bottom:12px;">
      <div class="btn-group">
        <button class="btn btn-danger" onclick="doAction('stop-silent')" id="btnStop">⏹ 停止静默</button>
        <button class="btn btn-success" onclick="doAction('start-silent')" id="btnStart">▶ 启动静默</button>
        <button class="btn btn-danger" onclick="doAction('restart')" id="btnRestart" style="border-color:rgba(248,81,73,0.6);">🔄 重启 Bot</button>
      </div>
    </div>
    <ul class="queue-list" id="queueList">
      <li style="color:var(--text-dim);">加载中...</li>
    </ul>
  </div>

  <!-- 设置信息 & NapCat -->
  <div class="card">
    <div class="card-header">
      <h2>⚙️ 设置 & 连接</h2>
    </div>
    <table class="info-table" id="infoTable">
      <tr><td>Bot QQ</td><td id="infoBotQQ">--</td></tr>
      <tr><td>NapCat API</td><td id="infoNapcatURL">--</td></tr>
      <tr><td>白名单群</td><td id="infoGroups">--</td></tr>
      <tr><td>输出目录</td><td id="infoOutput">--</td></tr>
      <tr><td>端口</td><td id="infoPort">--</td></tr>
      <tr><td>线程</td><td id="infoThreads">--</td></tr>
      <tr><td>NapCat 进程</td><td id="infoNapcat">--</td></tr>
      <tr><td>系统启动</td><td id="infoBoot">--</td></tr>
    </table>
  </div>

  <!-- 最近聊天 -->
  <div class="card full-width">
    <div class="card-header">
      <h2>💬 最近聊天记录</h2>
      <div style="display:flex; gap:8px; align-items:center;">
        <select id="chatType" onchange="refreshChat()" style="background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:2px 8px;font-size:11px;">
          <option value="all">全部</option>
          <option value="group">群聊</option>
          <option value="private">私聊</option>
        </select>
        <button class="btn" onclick="refreshChat()">🔄</button>
      </div>
    </div>
    <div class="log-viewer" id="chatViewer" style="max-height:250px;">加载中...</div>
  </div>

</div>

<script>
  const API = '';
  let logTimer, statusTimer, chatTimer;

  // ── 时钟 ──
  function updateClock() {
    const now = new Date();
    document.getElementById('clock').textContent = now.toLocaleString('zh-CN');
  }
  setInterval(updateClock, 1000);
  updateClock();

  // ── Toast ──
  function showToast(msg, type) {
    const t = document.createElement('div');
    t.className = 'toast toast-' + type;
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 3000);
  }

  // ── 状态刷新 ──
  async function refreshStatus() {
    try {
      const r = await fetch(API + '/api/status');
      const d = await r.json();

      // Bot 状态
      const dot = document.getElementById('statusDot');
      const badge = document.getElementById('badgeBot');
      if (d.bot_alive) {
        dot.className = 'status-dot online';
        badge.className = 'badge badge-green';
        badge.textContent = '🟢 运行中';
      } else {
        dot.className = 'status-dot offline';
        badge.className = 'badge badge-red';
        badge.textContent = '🔴 已停止';
      }

      // 系统统计卡片
      const stats = document.getElementById('systemStats');
      const s = d.system;
      const q = d.queue;
      stats.innerHTML = `
        <div class="stat-item">
          <div class="stat-value ${d.bot_alive?'green':'red'}">${d.bot_alive?'🟢 在线':'🔴 离线'}</div>
          <div class="stat-label">Bot 状态</div>
        </div>
        <div class="stat-item">
          <div class="stat-value blue">${s.cpu_percent ?? '--'}%</div>
          <div class="stat-label">CPU 使用</div>
        </div>
        <div class="stat-item">
          <div class="stat-value ${(s.mem_percent||0)>80?'yellow':'blue'}">${s.mem_percent ?? '--'}%</div>
          <div class="stat-label">内存 (${s.mem_used_gb ?? '-'}/${s.mem_total_gb ?? '-'}GB)</div>
        </div>
        <div class="stat-item">
          <div class="stat-value ${(s.disk_percent||0)>80?'yellow':'blue'}">${s.disk_percent ?? '--'}%</div>
          <div class="stat-label">磁盘 (剩余 ${s.disk_free_gb ?? '-'}GB)</div>
        </div>
        <div class="stat-item">
          <div class="stat-value green">${q.downloaded_count ?? '--'}</div>
          <div class="stat-label">已下载 ID</div>
        </div>
        <div class="stat-item">
          <div class="stat-value yellow">${q.pending_count ?? '--'}</div>
          <div class="stat-label">待下载</div>
        </div>
        <div class="stat-item">
          <div class="stat-value cyan">${q.pdf_count ?? '--'}</div>
          <div class="stat-label">PDF 文件</div>
        </div>
        <div class="stat-item">
          <div class="stat-value">${q.total_size_mb ?? '--'}MB</div>
          <div class="stat-label">PDF 总大小</div>
        </div>
      `;

      // 队列
      const ql = document.getElementById('queueList');
      const qBadge = document.getElementById('queueBadge');
      let active = q.active || [];
      let priQ = q.priority_queue || [];
      let silQ = q.silent_queue || [];
      let totalSil = q.silent_queue_total || silQ.length;
      qBadge.textContent = `${active.length} 活跃 | ${priQ.length} 优先 | ${totalSil} 静默`;
      qBadge.className = active.length > 0 ? 'badge badge-yellow' : 'badge badge-blue';

      let html = '';
      if (active.length > 0) {
        html += '<li style="color:var(--yellow);font-weight:600;margin-bottom:4px;">🔄 活跃下载：</li>';
        for (const a of active) {
          html += `<li class="active">  ${a.priority?'🔴 优先':'⚪ 静默'} JM${a.id} ⏳</li>`;
        }
      }
      if (priQ.length > 0) {
        html += '<li style="color:var(--red);font-weight:600;margin-top:8px;margin-bottom:4px;">🔴 优先队列：</li>';
        for (const aid of priQ.slice(0, 10)) {
          html += `<li class="pri">  JM${aid}</li>`;
        }
        if (q.priority_queue_total > 10) html += `<li style="color:var(--text-dim);">  ... 还有 ${q.priority_queue_total - 10} 个</li>`;
      }
      if (silQ.length > 0) {
        html += '<li style="color:var(--text-dim);font-weight:600;margin-top:8px;margin-bottom:4px;">⚪ 静默队列：</li>';
        for (const aid of silQ.slice(0, 15)) {
          html += `<li class="silent">  JM${aid}</li>`;
        }
        if (totalSil > 15) html += `<li style="color:var(--text-dim);">  ... 还有 ${totalSil - 15} 个</li>`;
      }
      if (!html) html = '<li style="color:var(--text-dim);">💤 队列为空</li>';
      ql.innerHTML = html;

      // 信息表
      document.getElementById('infoBotQQ').textContent = d.settings.bot_qq || '--';
      document.getElementById('infoNapcatURL').textContent = d.settings.napcat_url || '--';
      document.getElementById('infoGroups').textContent = d.settings.allowed_groups || '(不限)';
      document.getElementById('infoOutput').textContent = d.settings.output_dir || '--';
      document.getElementById('infoPort').textContent = d.settings.port || '--';
      document.getElementById('infoThreads').textContent = d.settings.worker_threads || '--';
      const n = d.napcat;
      document.getElementById('infoNapcat').innerHTML = n.running
        ? `<span style="color:var(--green);">🟢 运行中 (${n.count} 进程)</span>`
        : `<span style="color:var(--red);">🔴 未检测到</span>`;
      document.getElementById('infoBoot').textContent = s.boot_time || '--';

      // 运行时间
      if (s.boot_time) {
        document.getElementById('uptime').textContent = '系统启动: ' + s.boot_time;
      }

      document.getElementById('logRefresh').textContent = '';
    } catch(e) {
      console.error('status error:', e);
      document.getElementById('statusDot').className = 'status-dot offline';
      document.getElementById('badgeBot').className = 'badge badge-red';
      document.getElementById('badgeBot').textContent = '🔴 连接失败';
    }
  }

  // ── 日志刷新 ──
  async function refreshLog() {
    const lines = document.getElementById('logLines').value;
    try {
      const r = await fetch(API + '/api/log?lines=' + lines);
      const d = await r.json();
      const viewer = document.getElementById('logViewer');
      let text = d.content || '';
      // 简单高亮
      text = text.replace(/\[bot\]/g, '<span class="highlight">[bot]</span>');
      text = text.replace(/error|ERROR|fail|FAIL|FATAL|Traceback/gi, m => `<span class="error">${m}</span>`);
      text = text.replace(/success|done|完成|ok|started/gi, m => `<span class="info">${m}</span>`);
      viewer.innerHTML = text || '(空)';
      viewer.scrollTop = viewer.scrollHeight;
      const now = new Date();
      document.getElementById('logRefresh').textContent = '已更新 ' + now.toLocaleTimeString('zh-CN');
    } catch(e) {
      document.getElementById('logViewer').textContent = '加载日志失败: ' + e;
    }
  }

  // ── 聊天记录 ──
  async function refreshChat() {
    const type = document.getElementById('chatType').value;
    try {
      const r = await fetch(API + '/api/chat?type=' + type + '&lines=30');
      const d = await r.json();
      const viewer = document.getElementById('chatViewer');
      viewer.innerHTML = d.entries && d.entries.length > 0
        ? d.entries.map(e => e.replace(/</g,'&lt;').replace(/>/g,'&gt;')).join('\n')
        : '(暂无聊天记录)';
      viewer.scrollTop = viewer.scrollHeight;
    } catch(e) {
      document.getElementById('chatViewer').textContent = '加载失败: ' + e;
    }
  }

  // ── 操作按钮 ──
  async function doAction(action) {
    const btns = {
      'stop-silent': document.getElementById('btnStop'),
      'start-silent': document.getElementById('btnStart'),
      'restart': document.getElementById('btnRestart'),
    };
    const btn = btns[action];
    if (btn) btn.disabled = true;

    // 重启需要确认
    if (action === 'restart' && !confirm('确定要重启 Bot 吗？重启期间服务会短暂中断。')) {
      if (btn) btn.disabled = false;
      return;
    }

    try {
      const r = await fetch(API + '/api/action/' + action, { method: 'POST' });
      const d = await r.json();
      if (d.ok) {
        showToast('✅ ' + (d.desc || action) + ' 成功', 'success');
      } else {
        showToast('❌ ' + (d.error || '操作失败'), 'error');
      }
      // 延迟刷新状态
      setTimeout(refreshStatus, 1000);
      setTimeout(refreshLog, 1500);
    } catch(e) {
      showToast('❌ 请求失败: ' + e, 'error');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── 初始化 ──
  refreshStatus();
  refreshLog();
  refreshChat();

  // 自动刷新
  statusTimer = setInterval(refreshStatus, 10000);  // 每10秒刷新状态
  logTimer = setInterval(refreshLog, 5000);          // 每5秒刷新日志
  chatTimer = setInterval(refreshChat, 30000);        // 每30秒刷新聊天

  // 页面关闭时清理
  window.addEventListener('beforeunload', () => {
    clearInterval(statusTimer);
    clearInterval(logTimer);
    clearInterval(chatTimer);
  });
</script>

</body>
</html>"""


@app.route("/")
def index():
    """主页面。"""
    return render_template_string(DASHBOARD_HTML)


# ── 启动 ──
if __name__ == "__main__":
    from waitress import serve as wsgi_serve

    port = int(os.getenv("DASHBOARD_PORT", "9002"))
    threads = int(os.getenv("DASHBOARD_THREADS", "2"))

    print(f"[dashboard] JM2 Bot Dashboard starting on http://0.0.0.0:{port}")
    print(f"[dashboard] Monitoring Bot on port {BOT_PORT}")
    print(f"[dashboard] Log file: {BOT_LOG_PATH}")
    print(f"[dashboard] Press Ctrl+C to stop", flush=True)

    try:
        wsgi_serve(app, host="0.0.0.0", port=port, threads=threads)
    except KeyboardInterrupt:
        print("\n[dashboard] Stopped.")
    except Exception as e:
        print(f"[dashboard] FATAL: {e}", file=sys.stderr)
        sys.exit(1)

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests
from flask import Flask, request, jsonify
from waitress import serve as wsgi_serve

app = Flask(__name__)

SCRIPT_PATH = Path(__file__).resolve().parent / "download_album_to_pdf.py"
OUTPUT_DIR = Path(os.getenv("DOWNLOAD_OUTPUT_DIR", str((Path(__file__).resolve().parent / "downloads"))))
PYTHON_EXE = os.getenv("PYTHON_EXE", sys.executable)
ONEBOT_BASE_URL = os.getenv("ONEBOT_BASE_URL", "http://127.0.0.1:3000")
ONEBOT_ACCESS_TOKEN = os.getenv("ONEBOT_ACCESS_TOKEN", "")

SEARCH_SCRIPT = Path(__file__).resolve().parent / "search_album_info.py"

# 日志路径
_LOG_DIR = Path(__file__).resolve().parent / "logs"
_USAGE_LOG = _LOG_DIR / "usage.log"
_ALBUM_LOG = _LOG_DIR / "albums.log"
_CHAT_LOG_DIR = _LOG_DIR / "chat"
_FAV_FILE = _LOG_DIR / "favorites.json"
_PREFS_FILE = _LOG_DIR / "preferences.json"
_RATING_FILE = _LOG_DIR / "ratings.json"

# 搜索上下文记忆（按群存储，支持 n/p 翻页和 d 快捷下载）
_search_context: dict = {}  # {group_id: {query, sort, top_n, page, ids}}
_search_context_lock = threading.Lock()

# ── NLP 自然语言解析（纯正则，零依赖）──

def _nlp_parse(text: str):
    """纯正则 NLP：意图分类 + 槽位提取。"""
    if len(text) < 2:
        return None

    # 意图匹配（加权）
    _INTENTS = [
        ("search", [r"查[一下]?", r"搜[索]?", r"找[一下个]?", r"有没有", r"看看", r"想看", r"[给来]个"], 1.0),
        ("download", [r"下载", r"下[一个]?", r"来[一]?份", r"要这个"], 1.0),
        ("random", [r"推荐", r"[随来]一个", r"随便", r"有什么好"], 1.0),
        ("top", [r"排行", r"热门", r"最火", r"[日周月]榜"], 1.0),
        ("info", [r"详情", r"信息", r"介绍", r"能干什么", r"怎么用", r"帮助", r"功能"], 0.8),
    ]
    scores = {}
    for intent, patterns, w in _INTENTS:
        for p in patterns:
            if re.search(p, text):
                scores[intent] = scores.get(intent, 0) + w
    if not scores:
        return None
    best = max(scores, key=scores.get)

    # 提取车号
    car = re.search(r'(?:JM)?(\d{6,})', text)
    album_id = car.group(1) if car else None

    # 提取排序
    sort = None
    for sk, pats in [("最新", [r"最新", r"新出", r"最近", r"刚出"]),
                      ("收藏", [r"收藏", r"点赞", r"喜欢", r"最火", r"热门"]),
                      ("观看", [r"观看", r"看过", r"热度"]),
                      ("长度", [r"最长", r"页数"])]:
        if any(re.search(p, text) for p in pats):
            sort = sk
            break

    # 提取数量
    top_n = 20
    nm = re.search(r'(\d+)\s*[个条张]', text)
    if nm:
        top_n = min(int(nm.group(1)), 50)

    # 提取搜索关键词
    query = text
    # 去掉意图词、排序词、语气词
    noise = r"有没有|帮我|给我|查一下|搜索|找一下|找个|找|看看|来个|想看|推荐|下载|一个|一下|本子|漫画|的|什么|有|和|了|是|吗|男性|女性|位|生殖器|要用|想找|\([^)]*\)|@\S+"
    for w in noise.split("|"):
        query = re.sub(w, "", query)
    # 拆分标签为独立关键词
    query = re.sub(r'([系的子位鬼新])', r'\1 ', query)
    parts = [p.strip() for p in re.split(r'\s+', query) if len(p.strip()) >= 2 and not p.strip().isdigit()]
    query = " ".join(parts[:5]) if parts else query
    query = re.sub(r'\s+', ' ', query).strip() or text

    return {"intent": best, "album_id": album_id, "query": query,
            "sort": sort, "top_n": top_n, "score": scores[best]}

# JM 地址常量（模块级导入，避免 Flask 线程中 asyncio 冲突）
_JM_WEB_URL = "https://jmcomicgo(dot)org"
_JM_REDIRECT_URL = "https://jm365(dot)work/3YeBdF"

lock = threading.Lock()
COMMAND_DL_RE = re.compile(r"/download\s+(\d{6,}(?:\s+\d{6,})*)", re.IGNORECASE)
# /download 后可选 -s / nosend / --store 表示仅存储不发送
COMMAND_DL_NOSEND_RE = re.compile(r"/download\s+(\d{6,})\b.*?(-s|nosend|--store)", re.IGNORECASE)

# 已下载记录
_DOWNLOADED_LOG = _LOG_DIR / "downloaded.txt"
# 后台下载间隔（秒）
_BG_DOWNLOAD_INTERVAL = 15
# 子进程重试次数
_SUBPROCESS_RETRIES = 3
# 优先下载队列（/download 指令插入队首）
_priority_queue: list = []
_priority_lock = threading.Lock()
# 当前正在下载的车号（用于 /queue 进度显示）
_current_download = ""
_current_download_lock = threading.Lock()
# 频率限制：窗口(秒) / 最大请求数
_RATE_LIMIT_WINDOW = 10
_RATE_LIMIT_MAX = 5
# 群白名单（逗号分隔，空=不限制）
_ALLOWED_GROUPS = set(
    g.strip() for g in os.getenv("ALLOWED_GROUPS", "").split(",") if g.strip()
)
# 机器人 QQ 号（从环境变量读取，或启动时从 NapCat 获取）
_BOT_QQ = os.getenv("BOT_QQ", "")
_BOT_QQ_LOCK = threading.Lock()
# 频率限制状态
_rate_limit_state: dict = {}
_rate_limit_lock = threading.Lock()
COMMAND_SE_RE = re.compile(r"/search\s+(.+)", re.IGNORECASE)
COMMAND_PING_RE = re.compile(r"/ping", re.IGNORECASE)
COMMAND_HELP_RE = re.compile(r"/help", re.IGNORECASE)
COMMAND_JMURL_RE = re.compile(r"/jmurl", re.IGNORECASE)
COMMAND_STATS_RE = re.compile(r"/stats", re.IGNORECASE)
COMMAND_TOP_RE = re.compile(r"/top\s*(日榜|周榜|月榜)?", re.IGNORECASE)
COMMAND_INFO_RE = re.compile(r"/info\s+(\d{6,})", re.IGNORECASE)
COMMAND_RECENT_RE = re.compile(r"/recent", re.IGNORECASE)
COMMAND_RANDOM_RE = re.compile(r"/random", re.IGNORECASE)
COMMAND_FAV_RE = re.compile(r"/fav\s*(add|list|remove|del)?\s*(\d{6,})?", re.IGNORECASE)
COMMAND_SYSINFO_RE = re.compile(r"/sysinfo", re.IGNORECASE)
COMMAND_QUEUE_RE = re.compile(r"/queue", re.IGNORECASE)
COMMAND_PREFS_RE = re.compile(r"/prefs(?:\s+set\s+sort=(\S+))?(?:\s+top=(\d+))?", re.IGNORECASE)
COMMAND_COVER_RE = re.compile(r"/cover\s+(\d{6,})", re.IGNORECASE)
COMMAND_RATING_RE = re.compile(r"/rating\s+(\d{6,})\s+(\d|10)", re.IGNORECASE)
# 自然语言中的车号: JM350234 或 纯6-8位数字（排除 QQ 号）
_CAR_NUMBER_RE = re.compile(r'(?:JM)?(\d{6,8})\b')

# CQ 码（@mention、图片等）
_CQ_CODE_RE = re.compile(r'\[CQ:[^\]]+\]')
# 开头/结尾的 @mention 和 (昵称)
_AT_RE = re.compile(r'^\s*@\S+\s*')
_TAIL_AT_RE = re.compile(r'\s*@\S+\s*$')
_PAREN_NAME_RE = re.compile(r'^\s*\([^)]+\)\s*')

# 消息自动撤回延迟（秒）
RECALL_DELAY = 110
RECALL_NOTICE = "\n\n⏰ 此消息将在1分50秒后自动撤回"


def log(msg: str) -> None:
    print(f"[bot] {msg}", flush=True)


def _ensure_log_dir() -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def _check_rate_limit(user_id, group_id) -> bool:
    """Check rate limit, return True if allowed."""
    key = f"{group_id}:{user_id}"
    now = time.time()
    with _rate_limit_lock:
        if key not in _rate_limit_state:
            _rate_limit_state[key] = []
        _rate_limit_state[key] = [t for t in _rate_limit_state[key] if now - t < _RATE_LIMIT_WINDOW]
        if len(_rate_limit_state[key]) >= _RATE_LIMIT_MAX:
            return False
        _rate_limit_state[key].append(now)
        return True


def _check_group_allowed(group_id) -> bool:
    """Check if group is allowed (empty whitelist = allow all)."""
    if not _ALLOWED_GROUPS:
        return True
    return str(group_id) in _ALLOWED_GROUPS


def _run_subprocess_with_retry(cmd: list, timeout: int = 600):
    """Run subprocess with retry and exponential backoff."""
    last_err = None
    for attempt in range(1, _SUBPROCESS_RETRIES + 1):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        except subprocess.TimeoutExpired as e:
            last_err = e
            log(f"subprocess timeout (attempt {attempt}/{_SUBPROCESS_RETRIES})")
        except Exception as e:
            last_err = e
            log(f"subprocess error (attempt {attempt}/{_SUBPROCESS_RETRIES}): {e}")
        if attempt < _SUBPROCESS_RETRIES:
            time.sleep(2 ** attempt)
    raise last_err


def log_chat(event: dict[str, Any], text: str) -> None:
    """将收到的聊天消息按群/私聊分文件保存。"""
    _CHAT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg_type = event.get("message_type", "unknown")
    chat_id = event.get("group_id") or event.get("user_id") or "unknown"
    sender_id = event.get("user_id") or "unknown"
    # 尝试获取发送者昵称
    sender_name = ""
    sender_info = event.get("sender", {})
    if isinstance(sender_info, dict):
        sender_name = sender_info.get("nickname") or sender_info.get("card") or ""

    prefix = "group" if msg_type == "group" else "private"
    filename = f"{prefix}_{chat_id}.log"
    filepath = _CHAT_LOG_DIR / filename

    # 格式化为可读的单行（多行消息用 ⏎ 连接）
    text_one_line = text.replace("\n", " ⏎ ")
    sender_display = f"{sender_id}"
    if sender_name:
        sender_display += f"({sender_name})"

    line = f"[{ts}] {sender_display}: {text_one_line}"
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── 后台下载器 ──

def _load_downloaded_ids() -> set:
    """加载已下载的本子 ID 集合（从 downloaded.txt 和已有 PDF 文件）。"""
    ids = set()
    # 1. 从 downloaded.txt 读取
    if _DOWNLOADED_LOG.exists():
        with open(_DOWNLOADED_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and line.isdigit():
                    ids.add(line)
    # 2. 从已有 PDF 文件名提取 ID
    for pdf in OUTPUT_DIR.glob("[JM*]*.pdf"):
        m = re.match(r'\[JM(\d+)\]', pdf.name)
        if m:
            ids.add(m.group(1))
    return ids


def _mark_downloaded(album_id: str) -> None:
    """标记本子为已下载。"""
    _ensure_log_dir()
    with open(_DOWNLOADED_LOG, "a", encoding="utf-8") as f:
        f.write(album_id + "\n")


def _get_pending_albums() -> list[str]:
    """从 albums.log 中提取尚未下载的本子 ID（去重，按出现顺序）。"""
    if not _ALBUM_LOG.exists():
        return []
    downloaded = _load_downloaded_ids()
    seen = set()
    pending = []
    with open(_ALBUM_LOG, "r", encoding="utf-8") as f:
        for line in f:
            # 格式: [时间] JM123456 | ...
            m = re.search(r'JM(\d{6,})', line)
            if m:
                aid = m.group(1)
                if aid not in downloaded and aid not in seen:
                    seen.add(aid)
                    pending.append(aid)
    return pending


def _background_downloader() -> None:
    """后台线程：静默下载 albums.log 中未下载过的本子，优先队列有内容时暂停。"""
    log("background downloader started")
    while True:
        try:
            # ── 优先队列有任务时，暂停后台下载 ──
            with _priority_lock:
                has_priority = bool(_priority_queue)
            if has_priority:
                time.sleep(5)
                continue

            # ── 常规静默下载 ──
            pending = _get_pending_albums()
            if pending:
                log(f"background: {len(pending)} pending albums to download")
                for album_id in pending:
                    # 每个本子下载前检查优先队列
                    with _priority_lock:
                        if _priority_queue:
                            log("background: paused for priority queue")
                            break
                    if album_id in _load_downloaded_ids():
                        continue
                    log(f"background: downloading JM{album_id}")
                    with _current_download_lock:
                        _current_download = album_id
                    try:
                        with lock:
                            result_text, pdf_path = run_download(album_id, keep_existing=True)
                        if pdf_path and pdf_path.is_file():
                            _mark_downloaded(album_id)
                            log(f"background: JM{album_id} done")
                        else:
                            log(f"background: JM{album_id} no pdf generated")
                    except Exception as e:
                        log(f"background: JM{album_id} failed: {e}")
                    finally:
                        with _current_download_lock:
                            _current_download = ""
                    time.sleep(_BG_DOWNLOAD_INTERVAL)
            else:
                log("background: no pending albums, sleeping 30s")
        except Exception as e:
            log(f"background downloader error: {e}")
        time.sleep(30)


def _health_monitor() -> None:
    """后台线程：定期检查 NapCat 连接状态，并同步 Bot QQ。"""
    global _BOT_QQ
    fail_count = 0
    while True:
        time.sleep(60)
        try:
            resp = send_onebot_api("get_login_info", {})
            if isinstance(resp, dict) and resp.get("status") == "ok":
                if fail_count > 0:
                    log(f"health: NapCat reconnected after {fail_count} failures")
                fail_count = 0
                # 同步 Bot QQ 号
                data = resp.get("data", {})
                if isinstance(data, dict):
                    qq = str(data.get("user_id", ""))
                    if qq and qq != _BOT_QQ:
                        with _BOT_QQ_LOCK:
                            _BOT_QQ = qq
                        log(f"health: Bot QQ = {qq}")
            else:
                fail_count += 1
                log(f"health: NapCat unhealthy (fail={fail_count})")
        except Exception as e:
            fail_count += 1
            log(f"health: NapCat unreachable (fail={fail_count}): {e}")


def _rotate_log_file(filepath: Path, max_size_mb: int = 5, keep: int = 3) -> None:
    """日志轮转：超 max_size_mb 时重命名为 .1/.2/...，保留 keep 份。"""
    if not filepath.exists():
        return
    size_mb = filepath.stat().st_size / (1024 * 1024)
    if size_mb < max_size_mb:
        return
    # 删除最旧的
    oldest = filepath.parent / f"{filepath.name}.{keep}"
    if oldest.exists():
        oldest.unlink()
    # 轮转: .2 → .3, .1 → .2, 当前 → .1
    for i in range(keep - 1, 0, -1):
        src = filepath.parent / f"{filepath.name}.{i}"
        dst = filepath.parent / f"{filepath.name}.{i + 1}"
        if src.exists():
            src.rename(dst)
    filepath.rename(filepath.parent / f"{filepath.name}.1")


def _disk_cleanup() -> None:
    """清理旧 PDF（保留最近 50 个）。"""
    pdfs = sorted(OUTPUT_DIR.glob("[JM*]*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in pdfs[50:]:
        try:
            old.unlink()
            log(f"cleanup: removed old pdf {old.name}")
        except Exception as e:
            log(f"cleanup: failed to remove {old.name}: {e}")


def _maintenance_worker() -> None:
    """后台线程：定期日志轮转 + 磁盘清理。"""
    while True:
        time.sleep(3600)  # 每小时
        try:
            for logfile in [_USAGE_LOG, _ALBUM_LOG]:
                _rotate_log_file(logfile)
            _disk_cleanup()
        except Exception as e:
            log(f"maintenance error: {e}")


def start_background_downloader() -> None:
    """启动后台下载线程和健康监控线程。"""
    t = threading.Thread(target=_background_downloader, daemon=True, name="bg-downloader")
    t.start()
    t2 = threading.Thread(target=_health_monitor, daemon=True, name="health-monitor")
    t2.start()
    t3 = threading.Thread(target=_maintenance_worker, daemon=True, name="maintenance")
    t3.start()
    log("background threads started")


def log_usage(user_id, group_id, command: str, detail: str = "") -> None:
    """记录使用日志。"""
    _ensure_log_dir()
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] user={user_id} group={group_id} cmd={command}"
    if detail:
        line += f" {detail}"
    with open(_USAGE_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def log_album(album_id: str, title: str, tags: str = "", source: str = "search") -> None:
    """记录访问的本子信息。"""
    _ensure_log_dir()
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] JM{album_id} | {title}"
    if tags:
        line += f" | 标签: {tags}"
    line += f" | 来源: {source}"
    with open(_ALBUM_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def log_album_from_search(stdout: str, source: str = "search") -> None:
    """从搜索脚本的 stdout 中提取 JM ID、标题和标签，写入 album 日志。"""
    # 格式1: 关键词搜索结果 — 行首 "JM123456  标题 [tag] [tag] ..."
    for m in re.finditer(r'^JM(\d{6,})\s{2,}(.+)$', stdout, re.MULTILINE):
        full_text = m.group(2).strip()
        # 提取行末的标签（所有 [...] 括号内容）
        tag_list = re.findall(r'\[([^\]]+)\]', full_text)
        tags = ", ".join(tag_list) if tag_list else ""
        log_album(m.group(1), full_text, tags=tags, source=source)
    # 格式2: ID 精确查询 — 行首 "[标题]" 紧接着下一行行首 "JM123456"
    for m in re.finditer(r'^\[([^\]]+)\]\nJM(\d{6,})', stdout, re.MULTILINE):
        log_album(m.group(2), m.group(1).strip(), source=source)


def schedule_recall(message_id: int | str) -> None:
    """在 RECALL_DELAY 秒后撤回指定消息。"""
    def _recall():
        try:
            resp = send_onebot_api("delete_msg", {"message_id": int(message_id)})
            log(f"recalled message {message_id}: {resp}")
        except Exception as e:
            log(f"recall failed for {message_id}: {e}")

    timer = threading.Timer(RECALL_DELAY, _recall)
    timer.daemon = True
    timer.start()
    log(f"scheduled recall for message {message_id} in {RECALL_DELAY}s")


# 搜索排序关键词（按长词优先匹配）
_SEARCH_SORT_KEYS = ['发布时间', '观看次数', '收藏', '点赞', '喜欢', '最新', '观看', '长度', '页数', '图片']


def parse_search_args(raw_query: str):
    """
    从搜索指令中解析 关键词、排序方式、显示数量、页码。

    支持两种格式:
      旧格式: "无修正 最新 10"         → 位置参数
      新格式: "无修正 sort=最新 top=20 page=2 type=author" → key=value 参数

    返回: (query, sort, top_n, page, search_type)
    """
    text = raw_query.strip()
    sort = None
    top_n = 20
    page = 1
    search_type = None  # None=normal, "author", "tag"

    # ── 新格式: key=value ──
    kv_pattern = re.compile(r'\b(sort|top|page_size|page|type)=(\S+)', re.IGNORECASE)
    kv_found = list(kv_pattern.finditer(text))
    if kv_found:
        for m in kv_found:
            key = m.group(1).lower()
            val = m.group(2)
            if key == 'sort':
                for sk in _SEARCH_SORT_KEYS:
                    if sk == val:
                        sort = sk
                        break
                if sort is None:
                    log(f"unknown sort value: {val}")
            elif key in ('top', 'page_size'):
                try:
                    top_n = int(val)
                except ValueError:
                    pass
            elif key == 'page':
                try:
                    page = int(val)
                except ValueError:
                    pass
            elif key == 'type':
                if val in ('author', 'tag', 'normal'):
                    search_type = val
                else:
                    log(f"unknown search type: {val}")
        # 移除所有 kv 参数，剩余为关键词
        text = kv_pattern.sub('', text).strip()
        top_n = max(1, min(top_n, 80))   # 每页最多 80
        page = max(1, min(page, 1000))   # 页码 1-1000
        return text, sort, top_n, page, search_type

    # ── 旧格式: 位置参数 ──
    for sk in sorted(_SEARCH_SORT_KEYS, key=len, reverse=True):
        m = re.search(rf'\s+{re.escape(sk)}(?:\s+(\d{{1,3}}))?\s*$', text)
        if m:
            sort = sk
            if m.group(1):
                top_n = int(m.group(1))
            text = text[:m.start()].strip()
            break
    else:
        m_num = re.search(r'\s+(\d{1,3})\s*$', text)
        if m_num:
            top_n = int(m_num.group(1))
            text = text[:m_num.start()].strip()

    top_n = max(1, min(top_n, 50))
    return text, sort, top_n, page, search_type


def get_event_text(event: dict[str, Any]) -> str:
    raw = event.get("raw_message") or event.get("message") or ""
    if isinstance(raw, list):
        text_parts = []
        for item in raw:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("data", {}).get("text", ""))
        return "".join(text_parts)
    return str(raw)


def send_onebot_api(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{ONEBOT_BASE_URL.rstrip('/')}/{action}"
    headers = {}
    if ONEBOT_ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {ONEBOT_ACCESS_TOKEN}"
    resp = requests.post(url, json=payload, headers=headers, timeout=120)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}

    if resp.status_code >= 400:
        log(f"OneBot API failed: action={action} status={resp.status_code} payload={payload} response={data}")
        resp.raise_for_status()

    if isinstance(data, dict):
        status = data.get("status")
        retcode = data.get("retcode")
        if status is not None and str(status).lower() == "failed":
            raise RuntimeError(f"OneBot API reported failure: action={action} response={data}")
        if retcode not in (None, 0, "0", "ok", "success"):
            if isinstance(retcode, (int, float)) and retcode != 0:
                raise RuntimeError(f"OneBot API returned retcode={retcode}: action={action} response={data}")
            if isinstance(retcode, str) and retcode not in {"0", "ok", "success"}:
                raise RuntimeError(f"OneBot API returned retcode={retcode}: action={action} response={data}")

    return data


def extract_file_ref(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    for key in ("file_id", "file", "id", "url", "path"):
        value = response.get(key)
        if isinstance(value, str) and value:
            return value
    data = response.get("data")
    if isinstance(data, dict):
        for key in ("file_id", "file", "id", "url", "path"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def send_file_to_target(event: dict[str, Any], file_path: Path) -> tuple[bool, str]:
    """
    NapCat 专用文件发送，返回 (成功?, 错误消息)。
    - 私聊: /send_online_file
    - 群聊: /create_flash_task → /send_flash_msg
    """
    if not file_path.is_file():
        msg = f"文件不存在: {file_path}"
        log(msg)
        return False, msg

    if not file_path.exists():
        msg = f"文件无法访问: {file_path}"
        log(msg)
        return False, msg

    message_type = event.get("message_type")
    log(f"send_file_to_target: type={message_type} file={file_path} ({file_path.stat().st_size} bytes)")

    try:
        if message_type == "private":
            payload = {
                "user_id": str(event.get("user_id")),
                "file_path": str(file_path.resolve()),
                "file_name": file_path.name,
            }
            log(f"calling send_online_file: {payload}")
            resp = send_onebot_api("send_online_file", payload)
            log(f"send_online_file response: {resp}")
            return True, ""

        elif message_type == "group":
            # Step 1: 创建闪传任务（必须用绝对路径！）
            abs_path = str(file_path.resolve())
            payload1 = {"files": abs_path, "name": file_path.name}
            log(f"calling create_flash_task with abs path: {abs_path}")
            flash_resp = send_onebot_api("create_flash_task", payload1)
            log(f"create_flash_task response: {flash_resp}")

            task_id = None
            if isinstance(flash_resp, dict):
                data = flash_resp.get("data", {})
                if isinstance(data, dict):
                    # NapCat 把 fileset_id 放在 data.createFlashTransferResult.fileSetId
                    transfer = data.get("createFlashTransferResult", {})
                    if isinstance(transfer, dict):
                        task_id = transfer.get("fileSetId")
                    # fallback: 尝试其他常见字段
                    if not task_id:
                        task_id = data.get("task_id") or data.get("fileset_id")
            if not task_id:
                msg = f"闪传任务创建失败，响应: {flash_resp}"
                log(msg)
                return False, msg

            # Step 2: 发送闪传消息
            payload2 = {"fileset_id": task_id, "group_id": str(event.get("group_id"))}
            log(f"calling send_flash_msg: {payload2}")
            msg_resp = send_onebot_api("send_flash_msg", payload2)
            log(f"send_flash_msg response: {msg_resp}")
            return True, ""

        else:
            msg = f"不支持的消息类型: {message_type}"
            log(msg)
            return False, msg
    except requests.exceptions.ConnectionError as e:
        msg = f"无法连接 NapCat ({ONEBOT_BASE_URL}): {e}"
        log(msg)
        return False, msg
    except Exception as e:
        msg = f"文件发送异常: {type(e).__name__}: {e}"
        log(msg)
        return False, msg


def reply_to_event(
    event: dict[str, Any],
    text: str,
    file_path: Path | None = None,
    keyboard: dict | None = None,
) -> None:
    message_type = event.get("message_type")

    # Step 1: Send the file via NapCat's dedicated file APIs
    file_err = ""
    if file_path is not None and file_path.is_file():
        file_sent, file_err = send_file_to_target(event, file_path)
    else:
        file_sent = False

    # Step 2: Build the appropriate text message (追加撤回提示)
    if file_sent:
        reply_text = f"✅ {text}"
    else:
        reply_text = f"{text}"
        if file_err:
            reply_text += f"\n⚠️ 文件发送失败: {file_err}"
    if not keyboard:
        reply_text += RECALL_NOTICE

    # Step 3: Send text message and schedule recall
    try:
        payload: dict = {"message": reply_text}
        if keyboard:
            payload["keyboard"] = keyboard
        if message_type == "private":
            payload["user_id"] = event.get("user_id")
            resp = send_onebot_api("send_private_msg", payload)
        elif message_type == "group":
            payload["group_id"] = event.get("group_id")
            resp = send_onebot_api("send_group_msg", payload)
        else:
            log(f"unsupported message_type={message_type}")
            return

        # 提取 message_id 并调度撤回
        msg_id = None
        if isinstance(resp, dict):
            data = resp.get("data", {})
            if isinstance(data, dict):
                msg_id = data.get("message_id")
        if msg_id is not None:
            schedule_recall(msg_id)
        else:
            log(f"could not extract message_id from response: {resp}")
    except Exception as e:
        log(f"text reply failed: {e}")


def _get_album_page_count(album_id: str) -> int:
    """获取本子总页数（用于进度显示），失败返回0。"""
    try:
        cmd = [PYTHON_EXE, "-c", f"""
import sys
sys.stdout = open('/dev/null', 'w')
from jmcomic import JmOption
o = JmOption.default()
c = o.build_jm_client()
album = c.get_album_detail('{album_id}')
sys.stdout = sys.__stdout__
print(album.page_count)
"""]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return int(proc.stdout.strip()) if proc.stdout.strip().isdigit() else 0
    except Exception:
        return 0


def find_latest_pdf(dir_path: Path) -> Path | None:
    if not dir_path.exists():
        return None
    pdfs = [p for p in dir_path.glob("*.pdf") if p.is_file()]
    if not pdfs:
        return None
    return max(pdfs, key=lambda p: p.stat().st_mtime)


def run_search(query: str, sort: str = None, top_n: int = None, page: int = None, search_type: str = None) -> str:
    """查询本子信息（支持 ID 精确查询 或 关键词搜索），通过子进程调用 jmcomic。"""
    cmd = [PYTHON_EXE, str(SEARCH_SCRIPT), query]
    if sort:
        cmd += ["-s", sort]
    if top_n is not None:
        cmd += ["-n", str(top_n)]
    if page is not None and page > 1:
        cmd += ["--page", str(page)]
    if search_type and search_type != "normal":
        cmd += ["-t", search_type]
    log(f"running search: {' '.join(cmd)}")
    try:
        proc = _run_subprocess_with_retry(cmd, timeout=45)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        log(f"search done: rc={proc.returncode} out={len(stdout)} err={len(stderr)}")
    except subprocess.TimeoutExpired:
        log("search timed out after retries")
        return "查询超时，请稍后重试。"
    except Exception as e:
        log(f"search subprocess exception: {type(e).__name__}: {e}")
        return f"查询异常: {e}"

    if proc.returncode != 0:
        err_detail = (stderr + stdout)[:800]
        # 友好化常见错误
        if '本子不存在' in err_detail:
            return f"❌ 本子 JM{query} 不存在，请检查 ID 是否正确"
        if '查询失败' in err_detail:
            # 提取 "查询失败: xxx" 中的核心信息
            m = re.search(r'查询失败:\s*(.+?)(?:\n|$)', err_detail)
            if m:
                return f"❌ {m.group(1).strip()}"
        return f"❌ 查询失败\n{err_detail[:400]}"

    # 过滤掉 JMComic 库的内部日志行（以 [时间戳] 或 [线程名] 开头）
    log_pattern = re.compile(r'^\[(20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}|MainThread|Thread-\d+|api\.)')
    filtered_lines = [line for line in stdout.split("\n") if line.strip() and not log_pattern.match(line.strip())]
    result = "\n".join(filtered_lines).strip()
    return result or "(查询返回空)"


def run_download(album_id: str, keep_existing: bool = False, output_subdir: str = "") -> tuple[str, Path | None]:
    # 安全检查：album_id 必须是纯数字
    if not re.fullmatch(r'\d{6,}', album_id):
        return "❌ 无效的本子 ID", None
    # 优先下载使用独立目录，避免与后台下载冲突
    if output_subdir:
        out_dir = OUTPUT_DIR / output_subdir
    else:
        out_dir = OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    # 删除旧的 PDF 文件，避免 find_latest_pdf 返回过期结果
    # keep_existing=True 时跳过清理（用于后台批量下载）
    if not keep_existing:
        for old_pdf in out_dir.glob("*.pdf"):
            try:
                old_pdf.unlink()
                log(f"cleaned old pdf: {old_pdf.name}")
            except Exception as e:
                log(f"failed to clean old pdf {old_pdf.name}: {e}")
    cmd = [PYTHON_EXE, str(SCRIPT_PATH), album_id, "-o", str(out_dir)]
    log(f"running: {' '.join(cmd)}")
    proc = _run_subprocess_with_retry(cmd, timeout=600)
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    log_output = (stdout + "\n" + stderr).strip()

    if proc.returncode != 0:
        return f"下载失败\n{log_output[-1500:]}", None

    pdf = find_latest_pdf(out_dir)
    if pdf is not None:
        # 如果用了子目录，把 PDF 移到主目录
        if output_subdir and pdf.parent != OUTPUT_DIR:
            dest = OUTPUT_DIR / pdf.name
            pdf.rename(dest)
            pdf = dest
        return f"下载完成：{pdf.name}", pdf
    return "下载完成，但未找到 PDF 文件。", None


@app.route("/health", methods=["GET"])
def health():
    info = {
        "ok": True,
        "script": str(SCRIPT_PATH),
        "output_dir": str(OUTPUT_DIR),
        "napcat_url": ONEBOT_BASE_URL,
    }
    # Quick NapCat connectivity test
    try:
        resp = requests.post(f"{ONEBOT_BASE_URL.rstrip('/')}/get_login_info", json={}, timeout=5)
        info["napcat_status"] = resp.status_code
        info["napcat_response"] = str(resp.json())[:200]
    except Exception as e:
        info["napcat_error"] = str(e)
    return jsonify(info)


@app.route("/diag", methods=["GET"])
def diag():
    """详细诊断端点"""
    result = {
        "napcat_url": ONEBOT_BASE_URL,
        "output_dir": str(OUTPUT_DIR),
        "output_dir_exists": OUTPUT_DIR.exists(),
        "pdfs": [p.name for p in OUTPUT_DIR.glob("*.pdf")] if OUTPUT_DIR.exists() else [],
    }
    # Test NapCat connectivity
    for action in ["get_login_info", "get_version_info"]:
        try:
            resp = send_onebot_api(action, {})
            result[f"api_{action}"] = {"ok": True, "response": resp}
        except Exception as e:
            result[f"api_{action}"] = {"ok": False, "error": str(e)}
    return jsonify(result)


@app.route("/onebot", methods=["POST"])
def onebot_handler():
    try:
        event = request.get_json(silent=True) or {}
    except Exception:
        event = {}

    log(f"POST /onebot | post_type={event.get('post_type')} message_type={event.get('message_type')} user_id={event.get('user_id')} group_id={event.get('group_id')}")

    event_type = event.get("post_type")
    if event_type != "message":
        return jsonify({"ok": True})

    message_text = get_event_text(event)
    log(f"message received: type={event_type} text={message_text!r}")
    if not message_text:
        return jsonify({"ok": True})

    # 输入长度限制（防 DoS）
    if len(message_text) > 2000:
        log(f"message too long: {len(message_text)} chars")
        return jsonify({"ok": True})

    # 保存聊天记录（分群/私聊文件）
    log_chat(event, message_text)

    user_id = event.get("user_id")
    group_id = event.get("group_id")

    # 群白名单检查
    if group_id and not _check_group_allowed(group_id):
        log(f"blocked: group {group_id} not in whitelist")
        return jsonify({"ok": False, "error": "group not allowed"}), 403

    msg = message_text.strip()
    # 清理 CQ 码（防止 QQ 号被误检测为车号）
    msg = _CQ_CODE_RE.sub('', msg)
    # 去除开头/结尾的 @mention 和 (昵称)
    msg = _AT_RE.sub('', msg).strip()
    msg = _TAIL_AT_RE.sub('', msg).strip()
    msg = _PAREN_NAME_RE.sub('', msg).strip()
    log(f"msg={msg!r}")

    # ── / 指令始终响应（无需 @mention）──
    is_slash_cmd = msg.startswith("/")
    
    if is_slash_cmd:
        # 频率限制检查（仅对命令生效）
        is_command = any(r.search(msg) for r in [COMMAND_PING_RE, COMMAND_JMURL_RE, COMMAND_HELP_RE, COMMAND_SE_RE, COMMAND_DL_RE, COMMAND_STATS_RE, COMMAND_TOP_RE, COMMAND_INFO_RE, COMMAND_RECENT_RE, COMMAND_RANDOM_RE, COMMAND_FAV_RE, COMMAND_SYSINFO_RE, COMMAND_QUEUE_RE, COMMAND_PREFS_RE, COMMAND_COVER_RE, COMMAND_RATING_RE])
        if is_command and not _check_rate_limit(user_id, group_id):
            log(f"rate limited: user={user_id} group={group_id}")
            return jsonify({"ok": False, "error": "rate limited"}), 429
    else:
        # ── 非 / 指令：群聊需要 @机器人（支持 CQ码 和手打 @bot/@JMBot）──
        if event.get("message_type") == "group":
            with _BOT_QQ_LOCK:
                bot_qq = _BOT_QQ
            if bot_qq:
                # 方式1: QQ @提及 = [CQ:at,qq=2837430647]
                has_cq_at = f"[CQ:at,qq={bot_qq}]" in message_text
                # 方式2: 手打 @bot / @JMBot（NapCat 会保留纯文本）
                has_text_at = bool(re.search(r'@(bot|JMBot|jmBot|jm bot)', message_text, re.IGNORECASE))
                if not has_cq_at and not has_text_at:
                    log(f"skipped: bot not @mentioned")
                    return jsonify({"ok": True})

    # /ping - 快速诊断：验证消息回路通畅
    if COMMAND_PING_RE.search(msg):
        log("ping received, replying...")
        log_usage(event.get("user_id"), event.get("group_id"), "ping")
        reply_to_event(event, "pong!  Bot 运行正常。")
        return jsonify({"ok": True})

    # /jmurl - 输出 JM 的网页版和下载地址（点替换为 (dot)）
    if COMMAND_JMURL_RE.search(msg):
        log("jmurl requested, replying...")
        log_usage(event.get("user_id"), event.get("group_id"), "jmurl")
        jmurl_text = (
            "🌐 JM 禁漫地址\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"📖 网页版: {_JM_WEB_URL}\n"
            f"🔗 永久入口: {_JM_REDIRECT_URL}\n\n"
            "⚠️ 请将 (dot) 替换为 . 后访问"
        )
        reply_to_event(event, jmurl_text)
        return jsonify({"ok": True})

    # /stats - 显示统计信息
    if COMMAND_STATS_RE.search(msg):
        log("stats requested, replying...")
        log_usage(event.get("user_id"), event.get("group_id"), "stats")
        pdf_count = len(list(OUTPUT_DIR.glob("[JM*]*.pdf")))
        downloaded = len(_load_downloaded_ids())
        total_size = sum(p.stat().st_size for p in OUTPUT_DIR.glob("[JM*]*.pdf"))
        size_str = f"{total_size/1024/1024:.0f}MB" if total_size else "0MB"
        pending = len(_get_pending_albums())
        stats_text = (
            "📊 Bot 统计\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"📥 已下载: {downloaded} 个本子\n"
            f"📄 PDF 文件: {pdf_count} 个（{size_str}）\n"
            f"⏳ 待下载: {pending} 个\n"
            f"💾 磁盘: {size_str}"
        )
        reply_to_event(event, stats_text)
        return jsonify({"ok": True})

    # /top [日榜|周榜|月榜]
    if COMMAND_TOP_RE.search(msg):
        m = COMMAND_TOP_RE.search(msg)
        period = (m.group(1) or "日榜").strip()
        log(f"top requested: {period}")
        log_usage(event.get("user_id"), event.get("group_id"), "top", period)
        period_map = {"日榜": "day_ranking", "周榜": "week_ranking", "月榜": "month_ranking"}
        method = period_map.get(period, "day_ranking")
        try:
            # 用子进程调用排行榜
            cmd = [PYTHON_EXE, "-c", f"""
import sys, os
sys.stdout = open(os.devnull, 'w')
from jmcomic import JmOption
o = JmOption.default(); c = o.build_jm_client()
page = c.{method}(page=1)
sys.stdout = sys.__stdout__
print(f"\\u0014{period} TOP10\\u0014")
for i, (aid, title) in enumerate(page):
    if i >= 10: break
    print(f"JM{{aid}}  {{title}}")
"""]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            stdout = proc.stdout or ""
            if proc.returncode != 0:
                reply_to_event(event, "❌ 获取排行榜失败")
            else:
                reply_to_event(event, stdout.strip())
        except Exception as e:
            log(f"top error: {e}")
            reply_to_event(event, f"❌ {e}")
        return jsonify({"ok": True})

    # /info <ID> - 精简本子信息
    info_match = COMMAND_INFO_RE.search(msg)
    if info_match:
        album_id = info_match.group(1)
        log_usage(event.get("user_id"), event.get("group_id"), "info", album_id)
        log(f"info requested: {album_id}")
        result = run_search(album_id)  # ID 查询
        # 精简输出：取前 5 行
        lines = result.split("\n")
        brief = "\n".join(lines[:6])
        if len(lines) > 6:
            brief += f"\n... 共 {len(lines)} 行，/search {album_id} 查看完整"
        reply_to_event(event, brief)
        return jsonify({"ok": True})

    # /help - 显示全部指令帮助
    if COMMAND_HELP_RE.search(msg):
        log("help requested, replying...")
        log_usage(event.get("user_id"), event.get("group_id"), "help")
        help_text = (
            "📖 JMComic Bot 完整指令帮助\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🔍 /search — 搜索本子\n"
            "  /search <ID>                → 按车号查询详情\n"
            "  /search <关键词> [排序] [数量] → 旧格式\n"
            "  /search <关键词> sort=排序 top=数量 page=页码 type=类型 → 新格式\n"
            "  排序: 收藏/最新/观看/长度 (默认收藏)\n"
            "  类型: normal/author/tag\n"
            "  示例:\n"
            "    /search 350234\n"
            "    /search 原神 最新 10\n"
            "    /search 原神 sort=观看 top=10 page=2\n"
            "    /search MANA type=author top=5\n\n"
            "📥 /download — 下载本子\n"
            "  /download <ID> [ID...] [-s]\n"
            "  支持多ID批量下载，-s 仅存储不发送\n"
            "  示例:\n"
            "    /download 350234\n"
            "    /download 350234 350235 350236 -s\n\n"
            "📊 /stats — Bot 统计（已下载/待下载/磁盘）\n\n"
            "🔝 /top — 排行榜\n"
            "  /top 日榜  |  /top 周榜  |  /top 月榜\n\n"
            "ℹ️ /info <ID> — 精简本子信息\n"
            "  示例: /info 350234\n\n"
            "🖼️ /cover <ID> — 查看封面图片\n"
            "  示例: /cover 350234\n\n"
            "🕐 /recent — 最近查询过的本子（最近10个）\n\n"
            "🎲 /random — 随机推荐一个本子\n\n"
            "⭐ /fav — 收藏管理\n"
            "  /fav add 350234    → 添加收藏\n"
            "  /fav list          → 查看收藏\n"
            "  /fav remove 350234 → 取消收藏\n\n"
            "⭐ /rating <ID> <1-10> — 评分\n"
            "  示例: /rating 350234 8\n\n"
            "⚙️ /prefs — 搜索偏好\n"
            "  /prefs                    → 查看偏好\n"
            "  /prefs set sort=最新 top=10 → 设置偏好\n"
            "  (设置后 /search 自动应用)\n\n"
            "🖥️ /sysinfo — 系统信息（CPU/内存/磁盘）\n\n"
            "🌐 /jmurl — 禁漫地址（防和谐）\n"
            "💓 /ping — 存活检测\n"
            "❓ /help — 本帮助\n\n"
            "⏰ 消息1分50秒后自动撤回\n"
            "💡 聊天中提及 JM车号 自动识别查询"
        )
        reply_to_event(event, help_text)
        return jsonify({"ok": True})

    # /search <query> [排序] [数量] — 支持 ID 精确查询 或 关键词搜索
    search_match = COMMAND_SE_RE.search(msg)
    log(f"/search match: {bool(search_match)}")
    if search_match:
        raw_query = search_match.group(1).strip()
        query, sort, top_n, page, search_type = parse_search_args(raw_query)
        # 自动应用用户偏好（如果用户没有显式指定）
        if sort is None and top_n == 20 and page == 1 and search_type is None:
            try:
                prefs = {}
                if _PREFS_FILE.exists():
                    prefs = json.loads(_PREFS_FILE.read_text(encoding="utf-8"))
                up = prefs.get(str(event.get("user_id")), {})
                if up.get("sort"):
                    sort = up["sort"]
                if up.get("top"):
                    top_n = up["top"]
            except Exception:
                pass
        log_usage(event.get("user_id"), event.get("group_id"), "search", f"query={query!r} sort={sort} top={top_n} page={page} type={search_type}")
        log(f"received command: /search query={query!r} sort={sort} top={top_n} page={page} type={search_type} from user={event.get('user_id')}")
        try:
            result_text = run_search(query, sort=sort, top_n=top_n, page=page, search_type=search_type)
            # 记录搜索结果中的本子
            log_album_from_search(result_text, source="search")
            # 提取本子 ID 并保存搜索上下文
            ids = re.findall(r'JM(\d{6,})', result_text)
            if group_id and ids and search_type is None:
                with _search_context_lock:
                    _search_context[str(group_id)] = {
                        "query": query, "sort": sort, "top_n": top_n,
                        "page": page, "ids": ids
                    }
            # 添加快捷操作提示（仅关键词搜索）
            hint = ""
            if ids and search_type is None:
                hint = f"\n\n💡 回复 n 下一页 | p 上一页 | d1 下载第1个 | d1-3 下载前3个"
            reply_to_event(event, result_text + hint)
        except Exception as e:
            log(f"search error: {e}")
            reply_to_event(event, f"搜索失败：{e}")
        return jsonify({"ok": True})

    # /download <id1> [id2...] [-s|nosend]
    dl_match = COMMAND_DL_RE.search(msg)
    log(f"/download match: {bool(dl_match)}")
    if dl_match:
        ids_str = dl_match.group(1)
        album_ids = re.findall(r'\d{6,}', ids_str)
        nosend = bool(COMMAND_DL_NOSEND_RE.search(msg))
        log_usage(event.get("user_id"), event.get("group_id"), "download", f"ids={album_ids}" + (" nosend" if nosend else ""))
        log(f"received command: /download {album_ids} nosend={nosend}")
        # 插入优先队列队首
        with _priority_lock:
            for aid in reversed(album_ids):
                if aid not in _priority_queue:
                    _priority_queue.insert(0, aid)
        count = len(album_ids)
        reply_to_event(event, f"⏳ 优先下载 {count} 个本子（{', '.join(album_ids)}）...")
        # 异步下载（独立目录，无需等锁，立刻开始）
        def _async_dl():
            results = []
            for aid in album_ids:
                try:
                    with _current_download_lock:
                        _current_download = aid
                    # 获取总页数用于进度
                    total_pages = _get_album_page_count(aid)
                    # 独立目录，不与后台下载冲突
                    subdir = f"priority_{aid}"
                    # 启动进度监控线程
                    progress_info = {"done": 0, "total": total_pages}
                    def _monitor_progress():
                        out_dir = OUTPUT_DIR / subdir
                        while progress_info["done"] < progress_info["total"]:
                            time.sleep(2)
                            count = len(list(out_dir.glob("*"))) if out_dir.exists() else 0
                            progress_info["done"] = min(count, progress_info["total"])
                    if total_pages > 0:
                        threading.Thread(target=_monitor_progress, daemon=True).start()
                    # 下载（独立目录无需锁，立刻并行）
                    rt, pdf_path = run_download(aid, output_subdir=subdir)
                    if pdf_path and pdf_path.is_file():
                        _mark_downloaded(aid)
                        m = re.match(r'\[JM\d+\](.+)\.pdf', pdf_path.name)
                        title = m.group(1).strip() if m else pdf_path.stem
                        log_album(aid, title, source="download")
                    # 从优先队列移除
                    with _priority_lock:
                        if aid in _priority_queue:
                            _priority_queue.remove(aid)
                    # 进度
                    pct = f" ({progress_info['done']}/{total_pages}页)" if total_pages > 0 else ""
                    if nosend:
                        results.append(f"✅ JM{aid}{pct}")
                    else:
                        results.append(rt)
                        if pdf_path and pdf_path.is_file():
                            send_file_to_target(event, pdf_path)
                except Exception as e:
                    results.append(f"❌ JM{aid}: {e}")
                finally:
                    with _current_download_lock:
                        if _current_download == aid:
                            _current_download = ""
            reply_to_event(event, "\n".join(results))
        threading.Thread(target=_async_dl, daemon=True).start()
        return jsonify({"ok": True})

    # /recent - 最近查询的本子
    if COMMAND_RECENT_RE.search(msg):
        log("recent requested")
        log_usage(event.get("user_id"), event.get("group_id"), "recent")
        try:
            if _ALBUM_LOG.exists():
                lines = []
                seen = set()
                with open(_ALBUM_LOG, "r", encoding="utf-8") as f:
                    for line in reversed(list(f)):
                        m = re.search(r'JM(\d{6,})\s*\|\s*(.+?)\s*\|', line)
                        if m and m.group(1) not in seen:
                            seen.add(m.group(1))
                            lines.append(f"JM{m.group(1)}  {m.group(2).strip()}")
                            if len(lines) >= 10:
                                break
                text = "🕐 最近查询\n━━━━━━━━━━━━━━━━━━\n\n" + "\n".join(lines) if lines else "暂无记录"
            else:
                text = "暂无记录"
        except Exception as e:
            text = f"读取失败: {e}"
        reply_to_event(event, text)
        return jsonify({"ok": True})

    # /random - 随机推荐
    if COMMAND_RANDOM_RE.search(msg):
        log("random requested")
        log_usage(event.get("user_id"), event.get("group_id"), "random")
        try:
            import random
            cmd = [PYTHON_EXE, "-c", f"""
import sys, random
sys.stdout = open('/dev/null', 'w')
from jmcomic import JmOption
o = JmOption.default(); c = o.build_jm_client()
page = c.day_ranking(page=random.randint(1, 10))
album = random.choice(list(page.iter_id_title()))
sys.stdout = sys.__stdout__
print(f"JM{{album[0]}}  {{album[1]}}")
"""]
            proc = _run_subprocess_with_retry(cmd, timeout=30)
            reply_to_event(event, f"🎲 {proc.stdout.strip()}")
        except Exception as e:
            reply_to_event(event, f"❌ {e}")
        return jsonify({"ok": True})

    # /fav [add|list|remove] [ID]
    fav_match = COMMAND_FAV_RE.search(msg)
    if fav_match:
        action = fav_match.group(1) or "list"
        fav_id = fav_match.group(2)
        log(f"fav requested: {action} {fav_id}")
        log_usage(event.get("user_id"), event.get("group_id"), "fav", f"{action} {fav_id or ''}")
        user_key = str(event.get("user_id"))
        try:
            favs = {}
            if _FAV_FILE.exists():
                favs = json.loads(_FAV_FILE.read_text(encoding="utf-8"))
            if action in ("add", None) and fav_id:
                favs.setdefault(user_key, [])
                if fav_id not in favs[user_key]:
                    favs[user_key].append(fav_id)
                _FAV_FILE.write_text(json.dumps(favs, ensure_ascii=False, indent=2), encoding="utf-8")
                reply_to_event(event, f"⭐ 已收藏 JM{fav_id}")
            elif action in ("remove", "del") and fav_id:
                if user_key in favs and fav_id in favs[user_key]:
                    favs[user_key].remove(fav_id)
                _FAV_FILE.write_text(json.dumps(favs, ensure_ascii=False, indent=2), encoding="utf-8")
                reply_to_event(event, f"🗑️ 已取消收藏 JM{fav_id}")
            elif action == "list":
                user_favs = favs.get(user_key, [])
                if user_favs:
                    text = "⭐ 我的收藏\n━━━━━━━━━━━━━━━━━━\n" + "\n".join(f"JM{fid}" for fid in user_favs[-20:])
                else:
                    text = "暂无收藏"
                reply_to_event(event, text)
        except Exception as e:
            reply_to_event(event, f"❌ {e}")
        return jsonify({"ok": True})

    # /sysinfo - 系统信息
    if COMMAND_SYSINFO_RE.search(msg):
        log("sysinfo requested")
        log_usage(event.get("user_id"), event.get("group_id"), "sysinfo")
        try:
            import platform, psutil
            from datetime import datetime
            cpu = psutil.cpu_percent(interval=0.5)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            boot = datetime.fromtimestamp(psutil.boot_time()).strftime("%m-%d %H:%M")
            info = (
                "🖥️ 系统信息\n━━━━━━━━━━━━━━━━━━\n\n"
                f"🐍 Python: {sys.version.split()[0]}\n"
                f"💻 OS: {platform.system()} {platform.release()}\n"
                f"🕐 运行时间: {boot} 起\n\n"
                f"📊 CPU: {cpu}%\n"
                f"🧠 内存: {mem.percent}% ({mem.used//1024//1024}MB/{mem.total//1024//1024}MB)\n"
                f"💾 磁盘: {disk.percent}% ({disk.free//1024//1024//1024}GB 可用)\n\n"
                f"📦 jmcomic: 2.7.0\n"
                f"🔄 waitress threads: {os.getenv('WORKER_THREADS', '4')}\n"
                f"📥 PDF 文件: {len(list(OUTPUT_DIR.glob('[JM*]*.pdf')))} 个"
            )
            reply_to_event(event, info)
        except ImportError:
            reply_to_event(event, "❌ 缺少 psutil 模块")
        except Exception as e:
            reply_to_event(event, f"❌ {e}")
        return jsonify({"ok": True})

    # /queue - 查看下载队列（优先队列在前，含进度）
    if COMMAND_QUEUE_RE.search(msg):
        log("queue requested")
        log_usage(event.get("user_id"), event.get("group_id"), "queue")
        downloaded = _load_downloaded_ids()
        pending = _get_pending_albums()
        with _priority_lock:
            pri = list(_priority_queue)
        with _current_download_lock:
            cur = _current_download

        def _status(aid):
            if aid in downloaded:
                return "✅"
            if aid == cur:
                # 尝试获取进度
                pct = ""
                try:
                    import re as _re
                    subdir = OUTPUT_DIR / f"priority_{aid}"
                    if subdir.exists():
                        files = list(subdir.glob("*"))
                        if files:
                            latest = max(files, key=lambda f: f.stat().st_mtime)
                            match = _re.search(r'(\d+)', latest.name)
                            if match:
                                pct = f" {match.group(1)}p"
                            else:
                                pct = f" {len(files)}f"
                except Exception:
                    pass
                return f"⏳{pct}"
            return "⏸"

        lines = []
        if pri:
            lines.append("🔴 优先队列（/download 指令）：")
            for i, aid in enumerate(pri[:10], 1):
                lines.append(f"  {i:2d}. JM{aid} {_status(aid)}")
            if len(pri) > 10:
                lines.append(f"  ... 还有 {len(pri) - 10} 个")
        if pending:
            pending = [a for a in pending if a not in pri]
            if pending:
                if lines:
                    lines.append("")
                lines.append(f"⚪ 静默队列（共 {len(pending)} 个）：")
                for i, aid in enumerate(pending[:20 - len(pri)], len(pri) + 1):
                    lines.append(f"  {i:2d}. JM{aid} {_status(aid)}")
                if len(pending) > 20 - len(pri):
                    lines.append(f"  ... 还有 {len(pending) - (20 - len(pri))} 个")
        if not lines:
            text = "📥 下载队列为空"
        else:
            legend = "\n✅已下载 ⏳下载中 ⏸排队  数字=已下载页数"
            text = "📥 下载队列\n━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines) + legend
        reply_to_event(event, text)
        return jsonify({"ok": True})

    # /prefs [set sort=xxx top=xx]
    prefs_match = COMMAND_PREFS_RE.search(msg)
    if prefs_match:
        sort_val = prefs_match.group(1)
        top_val = prefs_match.group(2)
        log(f"prefs: sort={sort_val} top={top_val}")
        log_usage(event.get("user_id"), event.get("group_id"), "prefs", f"sort={sort_val} top={top_val}")
        user_key = str(event.get("user_id"))
        try:
            prefs = {}
            if _PREFS_FILE.exists():
                prefs = json.loads(_PREFS_FILE.read_text(encoding="utf-8"))
            if sort_val or top_val:
                prefs.setdefault(user_key, {})
                if sort_val and sort_val in _SEARCH_SORT_KEYS:
                    prefs[user_key]["sort"] = sort_val
                if top_val:
                    prefs[user_key]["top"] = int(top_val)
                _PREFS_FILE.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
                cur = prefs.get(user_key, {})
                reply_to_event(event, f"⚙️ 偏好已保存: sort={cur.get('sort','默认')} top={cur.get('top','20')}")
            else:
                cur = prefs.get(user_key, {})
                if cur:
                    reply_to_event(event, f"⚙️ 你的偏好: sort={cur.get('sort','默认')} top={cur.get('top','20')}")
                else:
                    reply_to_event(event, "⚙️ 未设置偏好，使用 /prefs set sort=最新 top=10 设置")
        except Exception as e:
            reply_to_event(event, f"❌ {e}")
        return jsonify({"ok": True})

    # /cover <ID> - 封面预览
    cover_match = COMMAND_COVER_RE.search(msg)
    if cover_match:
        album_id = cover_match.group(1)
        log(f"cover requested: {album_id}")
        log_usage(event.get("user_id"), event.get("group_id"), "cover", album_id)
        cover_url = f"https://cdn-msp.jmapiproxy1.cc/media/photos/{album_id}/00001.webp"
        try:
            # 发送图片消息并调度撤回
            if event.get("message_type") == "group":
                img_resp = send_onebot_api("send_group_msg", {
                    "group_id": event.get("group_id"),
                    "message": f"[CQ:image,file={cover_url},type=show,id=40000]\nJM{album_id} 封面",
                })
            else:
                img_resp = send_onebot_api("send_private_msg", {
                    "user_id": event.get("user_id"),
                    "message": f"[CQ:image,file={cover_url},type=show,id=40000]\nJM{album_id} 封面",
                })
            if isinstance(img_resp, dict):
                img_msg_id = (img_resp.get("data") or {}).get("message_id")
                if img_msg_id is not None:
                    schedule_recall(img_msg_id)
            reply_to_event(event, f"🖼️ JM{album_id} 封面已发送")
        except Exception as e:
            reply_to_event(event, f"❌ 封面发送失败: {e}")
        return jsonify({"ok": True})

    # /rating <ID> <1-10>
    rating_match = COMMAND_RATING_RE.search(msg)
    if rating_match:
        album_id = rating_match.group(1)
        score = int(rating_match.group(2))
        log(f"rating: {album_id} {score}")
        log_usage(event.get("user_id"), event.get("group_id"), "rating", f"{album_id}={score}")
        try:
            ratings = {}
            if _RATING_FILE.exists():
                ratings = json.loads(_RATING_FILE.read_text(encoding="utf-8"))
            ratings.setdefault(album_id, [])
            ratings[album_id].append(score)
            avg = sum(ratings[album_id]) / len(ratings[album_id])
            _RATING_FILE.write_text(json.dumps(ratings, ensure_ascii=False, indent=2), encoding="utf-8")
            reply_to_event(event, f"⭐ JM{album_id} 评分: {score}/10 (平均 {avg:.1f}, {len(ratings[album_id])}人)")
        except Exception as e:
            reply_to_event(event, f"❌ {e}")
        return jsonify({"ok": True})

    # 智能车号识别：聊天中提及 JM123456 自动查
    car_match = _CAR_NUMBER_RE.search(msg)
    if car_match and not any(r.search(msg) for r in [COMMAND_SE_RE, COMMAND_DL_RE, COMMAND_INFO_RE]):
        album_id = car_match.group(1)
        log(f"car number detected: {album_id}")
        try:
            result = run_search(album_id)
            # 只取前3行
            brief = "\n".join(result.split("\n")[:3])
            reply_to_event(event, f"🔍 检测到车号 JM{album_id}：\n{brief}")
        except Exception:
            pass
        return jsonify({"ok": True})

    # 快捷翻页: n (下一页) / p (上一页)
    quick_nav = re.match(r'^[nNpP]$', msg)
    if quick_nav and group_id:
        nav = msg.lower()
        with _search_context_lock:
            ctx = _search_context.get(str(group_id))
        if ctx:
            new_page = ctx["page"] + 1 if nav == 'n' else max(1, ctx["page"] - 1)
            log(f"quick nav: {nav} -> page {new_page}")
            result_text = run_search(ctx["query"], sort=ctx["sort"], top_n=ctx["top_n"], page=new_page)
            # 更新上下文
            ids = re.findall(r'JM(\d{6,})', result_text)
            with _search_context_lock:
                _search_context[str(group_id)] = {**ctx, "page": new_page, "ids": ids}
            hint = f"\n\n💡 回复 n 下一页 | p 上一页 | d1 下载第1个 | d1-3 下载前3个"
            reply_to_event(event, result_text + hint)
        return jsonify({"ok": True})

    # 快捷下载: d1 / d3 / d1-3 / d1,3,5
    quick_dl = re.match(r'^[dD](\d+)(?:[-~](\d+))?$', msg)
    if quick_dl and group_id:
        start = int(quick_dl.group(1))
        end = int(quick_dl.group(2)) if quick_dl.group(2) else start
        with _search_context_lock:
            ctx = _search_context.get(str(group_id))
        if ctx and ctx.get("ids"):
            all_ids = ctx["ids"]
            selected = []
            for i in range(start, min(end, len(all_ids)) + 1):
                if 1 <= i <= len(all_ids):
                    selected.append(all_ids[i - 1])
            if selected:
                log(f"quick dl: {start}-{end} -> {selected}")
                reply_to_event(event, f"⏳ 下载 {len(selected)} 个: {', '.join(f'JM{i}' for i in selected)}")
                def _quick_dl():
                    results = []
                    for aid in selected:
                        try:
                            with lock:
                                rt, pdf_path = run_download(aid)
                            if pdf_path and pdf_path.is_file():
                                _mark_downloaded(aid)
                                send_file_to_target(event, pdf_path)
                            results.append(f"✅ JM{aid}")
                        except Exception as e:
                            results.append(f"❌ JM{aid}: {e}")
                    reply_to_event(event, "\n".join(results))
                threading.Thread(target=_quick_dl, daemon=True).start()
            else:
                reply_to_event(event, "❌ 序号超出范围")
        return jsonify({"ok": True})

    # ── NLP 自然语言兜底 ──
    try:
        parsed = _nlp_parse(msg)
        if parsed:
            score = parsed.get("score", 0)
            intent = parsed.get("intent", "")
            # info 意图降低阈值（允许"能干什么"等触发）
            min_score = 0.8 if intent == "info" else 1.0
            if score < min_score:
                log(f"nlp below threshold: {parsed}")
            else:
                log(f"nlp: {parsed}")
                if intent == "search":
                    q = parsed["album_id"] or parsed["query"]
                    rt = run_search(q, sort=parsed.get("sort"), top_n=parsed.get("top_n", 20))
                    reply_to_event(event, f"🤖 {rt}")
                elif intent == "download" and parsed.get("album_id"):
                    def _nlp_dl():
                        try:
                            with lock:
                                r, p = run_download(parsed["album_id"])
                            reply_to_event(event, r, file_path=p)
                        except Exception as e:
                            reply_to_event(event, f"❌ {e}")
                    threading.Thread(target=_nlp_dl, daemon=True).start()
                elif intent == "random":
                    from random import choice
                    cmd = [PYTHON_EXE, "-c",
                           "from jmcomic import JmOption;"+
                           "o=JmOption.default();c=o.build_jm_client();"+
                           "import random;"+
                           "p=c.day_ranking(page=random.randint(1,10));"+
                           "a=random.choice(list(p.iter_id_title()));"+
                           "print(f'JM{a[0]} {a[1]}')"]
                    proc = _run_subprocess_with_retry(cmd, timeout=20)
                    reply_to_event(event, f"🎲 {proc.stdout.strip()}")
                elif intent == "top":
                    cmd = [PYTHON_EXE, "-c",
                           "from jmcomic import JmOption;"+
                           "o=JmOption.default();c=o.build_jm_client();"+
                           "p=c.day_ranking(page=1);"+
                           "print('\\n'.join(f'JM{a} {t}' for a,t in list(p.iter_id_title())[:5]))"]
                    proc = _run_subprocess_with_retry(cmd, timeout=20)
                    reply_to_event(event, f"🔝 日榜 TOP5\n{proc.stdout.strip()}")
                elif intent == "info":
                    if parsed.get("album_id"):
                        rt = run_search(parsed["album_id"])
                        reply_to_event(event, rt.split("\n")[0] if rt else "未找到")
                    else:
                        reply_to_event(event, "🤖 发送 /help 查看全部指令，或 @我 直接说你想找什么本子~")
                return jsonify({"ok": True})
    except Exception as e:
        log(f"nlp error: {e}")

    log(f"no command matched for msg={msg!r}")
    return jsonify({"ok": True})


if __name__ == "__main__":
    import sys
    print("[bot] Starting...", flush=True)
    # 启动时获取 Bot QQ 号
    if not _BOT_QQ:
        try:
            resp = send_onebot_api("get_login_info", {})
            data = resp.get("data", {}) if isinstance(resp, dict) else {}
            qq = str(data.get("user_id", ""))
            if qq:
                _BOT_QQ = qq
                print(f"[bot] Bot QQ = {qq}", flush=True)
        except Exception as e:
            print(f"[bot] WARNING: could not get bot QQ: {e}", flush=True)
    start_background_downloader()
    port = int(os.getenv("PORT", "9001"))
    threads = int(os.getenv("WORKER_THREADS", "4"))
    try:
        print(f"[bot] waitress serving on 0.0.0.0:{port} (threads={threads})", flush=True)
        wsgi_serve(app, host="0.0.0.0", port=port, threads=threads)
    except Exception as e:
        print(f"[bot] FATAL: {e}", file=sys.stderr)
        sys.exit(1)

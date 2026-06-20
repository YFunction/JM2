from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

SCRIPT_PATH = Path(__file__).resolve().parent / "download_album_to_pdf.py"
OUTPUT_DIR = Path(os.getenv("DOWNLOAD_OUTPUT_DIR", str((Path(__file__).resolve().parent / "my_pdf_output"))))
PYTHON_EXE = os.getenv("PYTHON_EXE", sys.executable)
ONEBOT_BASE_URL = os.getenv("ONEBOT_BASE_URL", "http://127.0.0.1:3000")
ONEBOT_ACCESS_TOKEN = os.getenv("ONEBOT_ACCESS_TOKEN", "")

SEARCH_SCRIPT = Path(__file__).resolve().parent / "search_album_info.py"

# 日志路径
_LOG_DIR = Path(__file__).resolve().parent / "logs"
_USAGE_LOG = _LOG_DIR / "usage.log"
_ALBUM_LOG = _LOG_DIR / "albums.log"
_CHAT_LOG_DIR = _LOG_DIR / "chat"

# JM 地址常量（模块级导入，避免 Flask 线程中 asyncio 冲突）
_JM_WEB_URL = "https://jmcomicgo(dot)org"
_JM_REDIRECT_URL = "https://jm365(dot)work/3YeBdF"

lock = threading.Lock()
COMMAND_DL_RE = re.compile(r"/download\s+(\d{6,})", re.IGNORECASE)
# /download 后可选 -s / nosend / --store 表示仅存储不发送
COMMAND_DL_NOSEND_RE = re.compile(r"/download\s+(\d{6,})\b.*?(-s|nosend|--store)", re.IGNORECASE)

# 已下载记录
_DOWNLOADED_LOG = _LOG_DIR / "downloaded.txt"
# 后台下载间隔（秒）
_BG_DOWNLOAD_INTERVAL = 30
COMMAND_SE_RE = re.compile(r"/search\s+(.+)", re.IGNORECASE)
COMMAND_PING_RE = re.compile(r"/ping", re.IGNORECASE)
COMMAND_HELP_RE = re.compile(r"/help", re.IGNORECASE)
COMMAND_JMURL_RE = re.compile(r"/jmurl", re.IGNORECASE)

# 去除消息开头的 @mention（如 @bot、@JMBot）
_AT_RE = re.compile(r'^\s*@\S+\s*')

# 消息自动撤回延迟（秒）
RECALL_DELAY = 110
RECALL_NOTICE = "\n\n⏰ 此消息将在1分50秒后自动撤回"


def log(msg: str) -> None:
    print(f"[bot] {msg}", flush=True)


def _ensure_log_dir() -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


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
    """后台线程：静默下载 albums.log 中未下载过的本子。"""
    log("background downloader started")
    while True:
        try:
            pending = _get_pending_albums()
            if pending:
                log(f"background: {len(pending)} pending albums to download")
                for album_id in pending:
                    # 再次检查（可能被用户抢先下载了）
                    if album_id in _load_downloaded_ids():
                        continue
                    log(f"background: downloading JM{album_id}")
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
                    # 限速间隔
                    time.sleep(_BG_DOWNLOAD_INTERVAL)
            else:
                log("background: no pending albums, sleeping 60s")
        except Exception as e:
            log(f"background downloader error: {e}")
        time.sleep(60)


def start_background_downloader() -> None:
    """启动后台下载线程。"""
    t = threading.Thread(target=_background_downloader, daemon=True, name="bg-downloader")
    t.start()
    log("background downloader thread started")


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
      新格式: "无修正 sort=最新 top=20 page=2" → key=value 参数

    返回: (query, sort, top_n, page)
    """
    text = raw_query.strip()
    sort = None
    top_n = 20
    page = 1

    # ── 新格式: key=value ──
    kv_pattern = re.compile(r'\b(sort|top|page_size|page)=(\S+)', re.IGNORECASE)
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
        # 移除所有 kv 参数，剩余为关键词
        text = kv_pattern.sub('', text).strip()
        top_n = max(1, min(top_n, 80))   # 每页最多 80
        page = max(1, min(page, 1000))   # 页码 1-1000
        return text, sort, top_n, page

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
    return text, sort, top_n, page


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
        elif file_path is not None and file_path.is_file():
            reply_text += f"\n📎 文件: http://127.0.0.1:9001/files/{file_path.name}"
    reply_text += RECALL_NOTICE

    # Step 3: Send text message and schedule recall
    try:
        if message_type == "private":
            resp = send_onebot_api("send_private_msg", {
                "user_id": event.get("user_id"),
                "message": reply_text,
            })
        elif message_type == "group":
            resp = send_onebot_api("send_group_msg", {
                "group_id": event.get("group_id"),
                "message": reply_text,
            })
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


def find_latest_pdf(dir_path: Path) -> Path | None:
    if not dir_path.exists():
        return None
    pdfs = [p for p in dir_path.glob("*.pdf") if p.is_file()]
    if not pdfs:
        return None
    return max(pdfs, key=lambda p: p.stat().st_mtime)


def run_search(query: str, sort: str = None, top_n: int = None, page: int = None) -> str:
    """查询本子信息（支持 ID 精确查询 或 关键词搜索），通过子进程调用 jmcomic。"""
    cmd = [PYTHON_EXE, str(SEARCH_SCRIPT), query]
    if sort:
        cmd += ["-s", sort]
    if top_n is not None:
        cmd += ["-n", str(top_n)]
    if page is not None and page > 1:
        cmd += ["--page", str(page)]
    log(f"running search: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=45)
        stdout = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
        stderr = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
        log(f"search done: rc={proc.returncode} out={len(stdout)} err={len(stderr)}")
    except subprocess.TimeoutExpired:
        log("search timed out after 45s")
        return "查询超时（45秒），请稍后重试。"
    except FileNotFoundError:
        log(f"Python not found: {PYTHON_EXE}")
        return "查询服务未就绪（Python 路径错误）。"
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


def run_download(album_id: str, keep_existing: bool = False) -> tuple[str, Path | None]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # 删除旧的 PDF 文件，避免 find_latest_pdf 返回过期结果
    # keep_existing=True 时跳过清理（用于后台批量下载）
    if not keep_existing:
        for old_pdf in OUTPUT_DIR.glob("*.pdf"):
            try:
                old_pdf.unlink()
                log(f"cleaned old pdf: {old_pdf.name}")
            except Exception as e:
                log(f"failed to clean old pdf {old_pdf.name}: {e}")
    cmd = [PYTHON_EXE, str(SCRIPT_PATH), album_id, "-o", str(OUTPUT_DIR)]
    log(f"running: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=600)
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    log_output = (stdout + "\n" + stderr).strip()

    if proc.returncode != 0:
        return f"下载失败\n{log_output[-1500:]}", None

    pdf = find_latest_pdf(OUTPUT_DIR)
    if pdf is not None:
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


@app.route("/files/<path:filename>", methods=["GET"])
def serve_pdf(filename: str):
    candidate = OUTPUT_DIR / filename
    if not candidate.is_file():
        return jsonify({"ok": False, "error": "file not found"}), 404
    return send_file(candidate, as_attachment=True)


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

    # 保存聊天记录（分群/私聊文件）
    log_chat(event, message_text)

    msg = message_text.strip()
    # 去除开头的 @mention
    msg = _AT_RE.sub('', msg).strip()
    log(f"msg={msg!r}")

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

    # /help - 显示全部指令帮助
    if COMMAND_HELP_RE.search(msg):
        log("help requested, replying...")
        log_usage(event.get("user_id"), event.get("group_id"), "help")
        help_text = (
            "📖 JMComic Bot 指令帮助\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🔍 /search <ID|关键词> [排序] [数量]\n"
            "  /search <关键词> sort=排序 top=数量 page=页码\n"
            "  查询本子信息。纯数字按ID精确查询，文字按关键词搜索。\n"
            "  排序: 收藏/最新/观看/长度 (默认: 收藏)\n"
            "  数量: 旧格式 1-50 (默认20), 新格式 1-80\n"
            "  示例:\n"
            "    /search 350234              → ID精确查询\n"
            "    /search 无修正 最新 5        → 发布时间排序，前5条\n"
            "    /search 原神 sort=观看 top=10 page=2 → 第2页，观看排序\n\n"
            "📥 /download <ID> [-s|nosend]\n"
            "  下载指定本子并生成 PDF 文件。加 -s/nosend 仅存储不发送。\n"
            "  示例:\n"
            "    /download 350234\n"
            "    /download 350234 -s     → 仅存储，不发送文件\n\n"
            "🌐 /jmurl\n"
            "  获取禁漫网页版和永久入口地址（防和谐格式）\n\n"
            "💓 /ping\n"
            "  Bot 存活检测\n\n"
            "❓ /help\n"
            "  显示本帮助\n\n"
            "⏰ 所有消息将在发送后1分50秒自动撤回"
        )
        reply_to_event(event, help_text)
        return jsonify({"ok": True})

    # /search <query> [排序] [数量] — 支持 ID 精确查询 或 关键词搜索
    search_match = COMMAND_SE_RE.search(msg)
    log(f"/search match: {bool(search_match)}")
    if search_match:
        raw_query = search_match.group(1).strip()
        query, sort, top_n, page = parse_search_args(raw_query)
        log_usage(event.get("user_id"), event.get("group_id"), "search", f"query={query!r} sort={sort} top={top_n} page={page}")
        log(f"received command: /search query={query!r} sort={sort} top={top_n} page={page} from user={event.get('user_id')}")
        try:
            result_text = run_search(query, sort=sort, top_n=top_n, page=page)
            # 记录搜索结果中的本子
            log_album_from_search(result_text, source="search")
            reply_to_event(event, result_text)
        except Exception as e:
            log(f"search error: {e}")
            reply_to_event(event, f"搜索失败：{e}")
        return jsonify({"ok": True})

    # /download <album_id> [-s|nosend]
    dl_match = COMMAND_DL_RE.search(msg)
    log(f"/download match: {bool(dl_match)}")
    if dl_match:
        album_id = dl_match.group(1)
        nosend = bool(COMMAND_DL_NOSEND_RE.search(msg))
        log_usage(event.get("user_id"), event.get("group_id"), "download", f"id={album_id}" + (" nosend" if nosend else ""))
        log(f"received command: /download {album_id} nosend={nosend} from user={event.get('user_id')}")

        try:
            with lock:
                result_text, pdf_path = run_download(album_id)
            # 记录已下载
            if pdf_path and pdf_path.is_file():
                _mark_downloaded(album_id)
                m = re.match(r'\[JM\d+\](.+)\.pdf', pdf_path.name)
                title = m.group(1).strip() if m else pdf_path.stem
                log_album(album_id, title, source="download")
            if nosend:
                reply_to_event(event, f"✅ 下载完成（未发送）: JM{album_id}")
            else:
                reply_to_event(event, result_text, file_path=pdf_path)
        except Exception as e:
            log(f"error: {e}")
            reply_to_event(event, f"处理失败：{e}")

        return jsonify({"ok": True})

    log(f"no command matched for msg={msg!r}")
    return jsonify({"ok": True})


if __name__ == "__main__":
    import sys
    print("[bot] Starting...", flush=True)
    start_background_downloader()
    try:
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "9001")), debug=False)
    except Exception as e:
        print(f"[bot] FATAL: {e}", file=sys.stderr)
        sys.exit(1)

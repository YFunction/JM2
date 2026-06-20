from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
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

# JM 地址常量（模块级导入，避免 Flask 线程中 asyncio 冲突）
_JM_WEB_URL = "https://jmcomicgo(dot)org"
_JM_REDIRECT_URL = "https://jm365(dot)work/3YeBdF"

lock = threading.Lock()
COMMAND_DL_RE = re.compile(r"/download\s+(\d{6,})", re.IGNORECASE)
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
    """从搜索脚本的 stdout 中提取 JM ID 和标题，写入 album 日志。"""
    # 格式1: 关键词搜索结果 — "JM123456  标题..."
    for m in re.finditer(r'JM(\d{6,})\s{2,}(.+?)(?:\s{2,}|$)', stdout):
        log_album(m.group(1), m.group(2).strip(), source=source)
    # 格式2: ID 精确查询 — "[标题]\nJM123456"
    for m in re.finditer(r'\[([^\]]+)\]\nJM(\d{6,})', stdout):
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
    从搜索指令中解析 关键词、排序方式、显示数量。

    示例:
      "无修正"              → ("无修正", None, 20)
      "无修正 最新 10"      → ("无修正", "最新", 10)
      "无修正 观看 5"       → ("无修正", "观看", 5)
      "无修正 收藏"         → ("无修正", "收藏", 20)
      "无修正 15"           → ("无修正", None, 15)
      "关键词 带 空格 最新 8" → ("关键词 带 空格", "最新", 8)
    """
    text = raw_query.strip()
    sort = None
    top_n = 20

    # 尝试从末尾提取 "排序词 [数字]"  — 长词优先匹配
    for sk in sorted(_SEARCH_SORT_KEYS, key=len, reverse=True):
        m = re.search(rf'\s+{re.escape(sk)}(?:\s+(\d{{1,3}}))?\s*$', text)
        if m:
            sort = sk
            if m.group(1):
                top_n = int(m.group(1))
            text = text[:m.start()].strip()
            break
    else:
        # 没有排序词，尝试匹配末尾纯数字
        m_num = re.search(r'\s+(\d{1,3})\s*$', text)
        if m_num:
            top_n = int(m_num.group(1))
            text = text[:m_num.start()].strip()

    return text, sort, min(top_n, 50)  # 最多 50 条


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


def run_search(query: str, sort: str = None, top_n: int = None) -> str:
    """查询本子信息（支持 ID 精确查询 或 关键词搜索），通过子进程调用 jmcomic。"""
    cmd = [PYTHON_EXE, str(SEARCH_SCRIPT), query]
    if sort:
        cmd += ["-s", sort]
    if top_n is not None:
        cmd += ["-n", str(top_n)]
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


def run_download(album_id: str) -> tuple[str, Path | None]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # 删除旧的 PDF 文件，避免 find_latest_pdf 返回过期结果
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
            "  查询本子信息。纯数字按ID精确查询，文字按关键词搜索。\n"
            "  排序: 收藏/最新/观看/长度 (默认: 收藏)\n"
            "  数量: 1-50 (默认: 20)\n"
            "  示例:\n"
            "    /search 350234        → ID精确查询\n"
            "    /search 无修正          → 收藏排序，前20条\n"
            "    /search 无修正 最新 5   → 发布时间排序，前5条\n"
            "    /search 无修正 观看 10  → 观看数排序，前10条\n\n"
            "📥 /download <ID>\n"
            "  下载指定本子并生成 PDF 文件。\n"
            "  示例:\n"
            "    /download 350234\n\n"
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
        query, sort, top_n = parse_search_args(raw_query)
        log_usage(event.get("user_id"), event.get("group_id"), "search", f"query={query!r} sort={sort} top={top_n}")
        log(f"received command: /search query={query!r} sort={sort} top={top_n} from user={event.get('user_id')}")
        try:
            result_text = run_search(query, sort=sort, top_n=top_n)
            # 记录搜索结果中的本子
            log_album_from_search(result_text, source="search")
            reply_to_event(event, result_text)
        except Exception as e:
            log(f"search error: {e}")
            reply_to_event(event, f"搜索失败：{e}")
        return jsonify({"ok": True})

    # /download <album_id>
    dl_match = COMMAND_DL_RE.search(msg)
    log(f"/download match: {bool(dl_match)}")
    if dl_match:
        album_id = dl_match.group(1)
        log_usage(event.get("user_id"), event.get("group_id"), "download", f"id={album_id}")
        log(f"received command: /download {album_id} from user={event.get('user_id')}")

        try:
            with lock:
                result_text, pdf_path = run_download(album_id)
            # 记录下载的本子
            if pdf_path and pdf_path.is_file():
                # 从文件名提取标题: "[JM123456]标题.pdf"
                m = re.match(r'\[JM\d+\](.+)\.pdf', pdf_path.name)
                title = m.group(1).strip() if m else pdf_path.stem
                log_album(album_id, title, source="download")
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
    try:
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "9001")), debug=False)
    except Exception as e:
        print(f"[bot] FATAL: {e}", file=sys.stderr)
        sys.exit(1)

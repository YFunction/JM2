from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

import requests
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

SCRIPT_PATH = Path(__file__).resolve().parent / "download_album_to_pdf.py"
OUTPUT_DIR = Path(os.getenv("DOWNLOAD_OUTPUT_DIR", str((Path(__file__).resolve().parent / "my_pdf_output"))))
PYTHON_EXE = os.getenv("PYTHON_EXE", r"D:\ProgramData\anaconda3\python.exe")
ONEBOT_BASE_URL = os.getenv("ONEBOT_BASE_URL", "http://127.0.0.1:9000")
ONEBOT_ACCESS_TOKEN = os.getenv("ONEBOT_ACCESS_TOKEN", "")

SEARCH_SCRIPT = Path(__file__).resolve().parent / "search_album_info.py"

lock = threading.Lock()
COMMAND_DL_RE = re.compile(r"^/download\s+(\d{6,})\s*$", re.IGNORECASE)
COMMAND_SE_RE = re.compile(r"^/search\s+(\d{3,})\s*$", re.IGNORECASE)


def log(msg: str) -> None:
    print(f"[bot] {msg}")


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

    # Step 2: Build the appropriate text message
    if file_sent:
        reply_text = f"✅ {text}"
    else:
        reply_text = f"{text}"
        if file_err:
            reply_text += f"\n⚠️ 文件发送失败: {file_err}"
        elif file_path is not None and file_path.is_file():
            reply_text += f"\n📎 文件: http://127.0.0.1:9001/files/{file_path.name}"

    # Step 3: Send text message
    try:
        if message_type == "private":
            send_onebot_api("send_private_msg", {
                "user_id": event.get("user_id"),
                "message": reply_text,
            })
        elif message_type == "group":
            send_onebot_api("send_group_msg", {
                "group_id": event.get("group_id"),
                "message": reply_text,
            })
        else:
            log(f"unsupported message_type={message_type}")
    except Exception as e:
        log(f"text reply failed: {e}")


def find_latest_pdf(dir_path: Path) -> Path | None:
    if not dir_path.exists():
        return None
    pdfs = [p for p in dir_path.glob("*.pdf") if p.is_file()]
    if not pdfs:
        return None
    return max(pdfs, key=lambda p: p.stat().st_mtime)


def run_search(album_id: str) -> str:
    """查询本子信息，通过 conda Python 子进程调用 jmcomic。"""
    cmd = [PYTHON_EXE, str(SEARCH_SCRIPT), album_id]
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
        return f"查询失败 (rc={proc.returncode})\n{err_detail}"

    # 过滤掉 JMComic 库的内部日志行（以 [时间戳] 或 [线程名] 开头）
    log_pattern = re.compile(r'^\[(20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}|MainThread|Thread-\d+|api\.)')
    filtered_lines = [line for line in stdout.split("\n") if line.strip() and not log_pattern.match(line.strip())]
    result = "\n".join(filtered_lines).strip()
    return result or "(查询返回空)"


def run_download(album_id: str) -> tuple[str, Path | None]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [PYTHON_EXE, str(SCRIPT_PATH), album_id, "-o", str(OUTPUT_DIR)]
    log(f"running: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
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
    log(f"msg={msg!r}")

    # /ping - 快速诊断：验证消息回路通畅
    if msg.lower() == "/ping":
        log("ping received, replying...")
        reply_to_event(event, "pong!  Bot 运行正常。")
        return jsonify({"ok": True})

    # /search <album_id>
    search_match = COMMAND_SE_RE.fullmatch(msg)
    log(f"/search match: {bool(search_match)}")
    if search_match:
        album_id = search_match.group(1)
        log(f"received command: /search {album_id} from user={event.get('user_id')}")
        try:
            result_text = run_search(album_id)
            reply_to_event(event, result_text)
        except Exception as e:
            log(f"search error: {e}")
            reply_to_event(event, f"搜索失败：{e}")
        return jsonify({"ok": True})

    # /download <album_id>
    dl_match = COMMAND_DL_RE.fullmatch(msg)
    log(f"/download match: {bool(dl_match)}")
    if dl_match:
        album_id = dl_match.group(1)
        log(f"received command: /download {album_id} from user={event.get('user_id')}")

        try:
            with lock:
                result_text, pdf_path = run_download(album_id)
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

"""لایه AI با معماری async + گزارش پیشرفت زنده
- پیام: prompt_async (فوری برمی‌گردد)
- نظارت: polling وضعیت سشن + گزارش درصد پیشرفت به تلگرام
- بدون timeout منفی: تا هر چقدر طول بکشد دنبال می‌کند
"""
import json
import os
import time
import threading

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)

OC_URL = CFG.get("opencode_server", "http://127.0.0.1:4097").rstrip("/")
OC_USER = CFG.get("opencode_server_user", "opencode")
OC_PASS = CFG.get("opencode_server_pass", "")
OC_MODEL = CFG.get("opencode_model", "1/glm-5.3-flash")
OC_WORKSPACE = CFG.get("opencode_workspace", os.path.expanduser("~"))
PROXY = CFG.get("proxy", "")

AUTH = (OC_USER, OC_PASS) if OC_PASS else None
HTTP = requests.Session()
HTTP.auth = AUTH
HTTP.trust_env = False   # مهم: سرور محلی است — هرگز از پروکسی نگذر
# (پروکسی فقط موقع launch سرور به محیطش داده می‌شود، برای تماس‌های LLM خودش)

_session_id = None
_sess_lock = threading.Lock()


def _server_alive() -> bool:
    try:
        return HTTP.get(f"{OC_URL}/global/health", timeout=4).status_code == 200
    except Exception:
        return False


def ensure_server() -> bool:
    if _server_alive():
        return True
    import subprocess as sp
    exe = CFG.get("opencode_exe", os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming", "npm",
        "node_modules", "opencode-ai", "bin", "opencode.exe"))
    port = OC_URL.rsplit(":", 1)[-1]
    env = os.environ.copy()
    env["OPENCODE_SERVER_PASSWORD"] = OC_PASS
    env["OPENCODE_SERVER_USERNAME"] = OC_USER
    env["HTTP_PROXY"] = env["HTTPS_PROXY"] = PROXY
    sp.Popen([exe, "serve", "--port", port, "--hostname", "127.0.0.1"],
             cwd=OC_WORKSPACE, env=env, creationflags=0x08000000)
    for _ in range(45):
        time.sleep(1)
        if _server_alive():
            return True
    return False


def new_session() -> str:
    r = HTTP.post(f"{OC_URL}/session", json={"title": "telegram-bot"}, timeout=15)
    r.raise_for_status()
    return r.json()["id"]


def reset():
    global _session_id
    with _sess_lock:
        if _session_id:
            try:
                HTTP.post(f"{OC_URL}/session/{_session_id}/abort", timeout=10)
            except Exception:
                pass
        _session_id = None


def abort_current():
    """توقف کار فعلی (دستور /stop)"""
    global _session_id
    if _session_id:
        try:
            HTTP.post(f"{OC_URL}/session/{_session_id}/abort", timeout=10)
        except Exception:
            pass


def _status() -> dict:
    """وضعیت همه سشن‌ها"""
    try:
        r = HTTP.get(f"{OC_URL}/session/status", timeout=8)
        return r.json()
    except Exception:
        return {}


def chat_async(message: str, on_progress=None) -> str:
    """
    ارسال پیام در thread جدا + مانیتورینگ زنده.
    - کار کوتاه: جواب همان لحظه که آماده شد برگردانده می‌شود (بدون تأخیر مانیتورینگ)
    - کار طولانی: on_progress(percent, note) صدا زده می‌شود تا تمام شود
    """
    global _session_id
    if not ensure_server():
        return "⚠ سرور AI بالا نیامد — بات را restart کن."
    with _sess_lock:
        if _session_id is None:
            _session_id = new_session()
    sid = _session_id

    body = {
        "parts": [{"type": "text", "text": message}],
        "model": {"providerID": OC_MODEL.split("/")[0],
                  "modelID": OC_MODEL.split("/", 1)[1]},
    }

    result = {}

    def worker():
        try:
            # این endpoint تا پایان کار (شامل اجرای ابزارها) منتظر می‌ماند
            r = HTTP.post(f"{OC_URL}/session/{sid}/message",
                          json=body, timeout=1800)
            result["data"] = r
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    t0 = time.time()
    last_pct = 0
    while t.is_alive():
        time.sleep(2)
        elapsed = time.time() - t0
        # کار طولانی‌تر از 8 ثانیه → گزارش پیشرفت
        if on_progress and elapsed > 8:
            pct = min(92, 10 + int(elapsed / 30 * 8))
            if pct > last_pct:
                last_pct = pct
                on_progress(pct, f"در حال اجرا ({int(elapsed)}s)")
        if elapsed > 3600:
            abort_current()
            return "⏱ کار بیش از 60 دقیقه طول کشید — متوقف شد."
    t.join()

    if "error" in result:
        return f"⚠ خطا: {result['error']}"

    # پاسخ مستقیم از پاسخ POST (سریع‌ترین مسیر)
    try:
        data = result["data"].json()
        texts = []
        for part in data.get("parts", []):
            if part.get("type") == "text" and part.get("text", "").strip():
                texts.append(part["text"].strip())
        if texts:
            return "\n".join(texts)
    except Exception:
        pass

    # fallback: خواندن از تاریخچه با retry
    for _ in range(5):
        time.sleep(2)
        try:
            r = HTTP.get(f"{OC_URL}/session/{sid}/message?limit=3", timeout=20)
            for item in reversed(r.json()):
                if item.get("info", {}).get("role") == "assistant":
                    joined = "\n".join(
                        p.get("text", "").strip()
                        for p in item.get("parts", [])
                        if p.get("type") == "text" and p.get("text", "").strip())
                    if joined:
                        return joined
        except Exception:
            continue
    return "✅ کار تمام شد (بدون گزارش متنی)."


def _count_messages(sid: str) -> int:
    try:
        r = HTTP.get(f"{OC_URL}/session/{sid}/message?limit=1", timeout=10)
        return len(r.json())
    except Exception:
        return 0

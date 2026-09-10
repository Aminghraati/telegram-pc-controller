"""Remote Assistant Bot — کنترل لپ‌تاپ از طریق تلگرام
فقط مالک (owner_id) اجازه استفاده دارد.
"""
import os
import re
import sys
import json
import time
import shutil
import socket
import subprocess
import threading
import logging
from datetime import datetime

import requests
import psutil

BASE = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE, "bot.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("bot")

with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)

TOKEN = CFG["token"]
OWNER = int(CFG["owner_id"])
API = f"https://api.telegram.org/bot{TOKEN}"
DL_DIR = CFG.get("downloads_dir") or os.path.join(os.path.expanduser("~"), "Downloads", "from_telegram")
MAX_MB = int(CFG.get("max_upload_mb", 50))
PROXY = CFG.get("proxy") or None   # مثال: http://10.159.167.160:8080
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None

# پوشه ذخیره متن‌های دریافتی (پل اندروید↔ویندوز)
TEXTS_DIR = os.path.join(DL_DIR, "texts")

# --- opencode bridge (سرور دائمی) ---
import ai as oc

OC_WORKSPACE = CFG.get("opencode_workspace", os.path.expanduser("~"))

SYSTEM_HINT = (
    "تو دستیار اجرایی روی ویندوز هستی و دستورها را از طریق بات تلگرام دریافت می‌کنی. "
    "قوانین مهم:\n"
    "1. دستور کاربر را کامل اجرا کن، نه فقط بخشی از آن. مثلاً اگر گفت «فایل منیجر را در پوشه عکس‌ها باز کن» "
    "دستور: Start-Process explorer.exe 'shell:My Pictures' را اجرا کن.\n"
    "2. برای باز کردن GUI از Start-Process استفاده کن.\n"
    "3. جواب نهایی فقط یک گزارش کوتاه فارسی از نتیجه باشد؛ مسیرهای داخلی، لاگ و جزئیات فنی را ننویس مگر پرسیده شود.\n"
    "4. اگر فایلی ساختی، مسیر کاملش را در یک خط جدا بنویس (شروع با C:\\) تا بات آن را برای کاربر بفرستد.\n"
    "5. پاسخ حداکثر 8 خط."
)

_ai_lock = threading.Lock()


def tg(method: str, **payload):
    """POST به API تلگرام؛ خروجی JSON"""
    try:
        r = requests.post(f"{API}/{method}", json=payload, timeout=60, proxies=PROXIES)
        return r.json()
    except Exception as e:
        log.error("tg %s failed: %s", method, e)
        return {"ok": False, "description": str(e)}


def send(chat_id, text: str):
    text = str(text)
    log.info("SEND → %s", text[:150].replace("\n", " ⏎ "))   # لاگ خروجی
    # تلگرام محدودیت 4096 کاراکتر دارد
    for i in range(0, len(text), 4000):
        tg("sendMessage", chat_id=chat_id, text=text[i:i + 4000])


def send_file(chat_id, path: str, caption: str = ""):
    """ارسال فایل (سند) با آپلود chunked"""
    log.info("SEND FILE → %s", path)
    size = os.path.getsize(path)
    if size > MAX_MB * 1024 * 1024:
        send(chat_id, f"فایل بزرگ‌تر از {MAX_MB}MB است ({size/1e6:.1f}MB)")
        return
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"{API}/sendDocument",
                data={"chat_id": chat_id, "caption": caption[:1000]},
                files={"document": (os.path.basename(path), f)},
                timeout=300, proxies=PROXIES,
            )
        if not r.json().get("ok"):
            send(chat_id, f"خطا در ارسال: {r.json().get('description', '?')}")
    except Exception as e:
        log.error("send_file failed: %s", e)
        send(chat_id, f"خطا در ارسال فایل: {e}")


def send_photo(chat_id, path: str, caption: str = "", delete_after: bool = False):
    log.info("SEND PHOTO → %s", path)
    sent_ok = False
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"{API}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption[:1000]},
                files={"photo": (os.path.basename(path), f)},
                timeout=300, proxies=PROXIES,
            )
        sent_ok = bool(r.json().get("ok"))
        if not sent_ok:
            send(chat_id, f"خطا در ارسال عکس: {r.json().get('description', '?')}")
    except Exception as e:
        log.error("send_photo failed: %s", e)
        send(chat_id, f"خطا در ارسال عکس: {e}")
    # پاک‌سازی پشت‌صحنه: فقط اگر ارسال موفق بود
    if delete_after and sent_ok and os.path.exists(path):
        try:
            os.remove(path)
            log.info("CLEANED (auto-delete) → %s", path)
        except Exception as e:
            log.error("auto-delete failed: %s", e)
    return sent_ok


# ---------------- System actions ----------------

def take_screenshot() -> str:
    import mss
    out = os.path.join(DL_DIR, f"shot_{datetime.now():%Y%m%d_%H%M%S}.png")
    with mss.mss() as s:
        s.shot(mon=-1, output=out)
    return out


def screenshot_all() -> list:
    """اسکرین‌شات از همه مانیتورها"""
    import mss
    paths = []
    with mss.mss() as s:
        for i, mon in enumerate(s.monitors[1:] if len(s.monitors) > 1 else s.monitors):
            out = os.path.join(DL_DIR, f"shot_{i+1}_{datetime.now():%Y%m%d_%H%M%S}.png")
            s.shot(mon=s.monitors.index(mon), output=out)
            paths.append(out)
    return paths


def sys_status() -> str:
    batt = psutil.sensors_battery()
    b = f"{batt.percent:.0f}% {'(شارژ)' if batt.power_plugged else '(روی باتری)'}" if batt else "ناموجود"
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disk = shutil.disk_usage("C:\\")
    boot = datetime.fromtimestamp(psutil.boot_time())
    up = datetime.now() - boot
    hrs, rem = divmod(int(up.total_seconds()), 3600)
    return (
        f"💻 وضعیت سیستم\n"
        f"────────────────\n"
        f"🧠 CPU: {cpu}%\n"
        f"💾 RAM: {mem.percent}% ({mem.used//2**30}GB / {mem.total//2**30}GB)\n"
        f"🗄 دیسک C: {100-disk.free/disk.total*100:.0f}% پر\n"
        f"🔋 باتری: {b}\n"
        f"⏱ روشن از: {hrs} ساعت پیش\n"
        f"📡 نام سیستم: {socket.gethostname()}"
    )


def run_powershell(cmd: str) -> str:
    """اجرای دستور با فیلتر امنیتی"""
    banned = ["format ", "del /f /s /q c:", "rd /s /q c:\\", "remove-item c:\\windows",
              "cipher /w", "vssadmin delete"]
    # نکته: shutdown/restart از مسیر /shutdown و /restart با تأیید دو مرحله‌ای مجاز است؛
    # از طریق /run خاموش مسدود می‌ماند تا کسی اشتباهی سیستم را نکشد
    low = cmd.lower()
    for b in banned:
        if b in low:
            return "⛔ این دستور در لیست سیاه است."
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=90,
            encoding="utf-8", errors="replace",
        )
        out = (r.stdout or "") + (("\n[STDERR]\n" + r.stderr) if r.stderr.strip() else "")
        return out.strip() or "(بدون خروجی)"
    except subprocess.TimeoutExpired:
        return "⏱ دستور بیش از 90 ثانیه طول کشید — قطع شد."
    except Exception as e:
        return f"خطا: {e}"


def list_dir(path: str) -> str:
    path = os.path.expanduser(path)
    if not os.path.isdir(path):
        return f"پوشه پیدا نشد: {path}"
    items = []
    try:
        entries = sorted(os.listdir(path))[:60]
        for name in entries:
            full = os.path.join(path, name)
            if os.path.isdir(full):
                items.append(f"📁 {name}")
            else:
                kb = os.path.getsize(full) / 1024
                items.append(f"📄 {name} ({kb:,.0f} KB)" if kb > 1 else f"📄 {name}")
    except PermissionError:
        return "⛔ دسترسی به این پوشه ندارم."
    return f"📂 {path}\n" + "\n".join(items)


def find_file(root: str, pattern: str, limit: int = 20) -> list:
    """جستجوی بازگشتی فایل با نام شامل pattern"""
    hits = []
    root = os.path.expanduser(root)
    pat = pattern.lower()
    for dirpath, dirs, files in os.walk(root):
        # پرش از پوشه‌های سیستمی سنگین
        dirs[:] = [d for d in dirs if d not in
                   ("$Recycle.Bin", "Windows", "Program Files",
                    "Program Files (x86)", "AppData", ".git",
                    "__pycache__", "node_modules")]
        for fn in files:
            if pat in fn.lower():
                hits.append(os.path.join(dirpath, fn))
                if len(hits) >= limit:
                    return hits
    return hits


# ---------------- Screen Recorder ----------------

class ScreenRecorder:
    """ضبط ویدیویی صفحه — بدون ffmpeg، فقط mss + OpenCV"""
    def __init__(self):
        self.recording = False
        self.path = None
        self.start_time = None
        self._thread = None
        self._writer = None

    def start(self):
        if self.recording:
            return None, "already"
        import cv2  # noqa
        self.path = os.path.join(DL_DIR, f"rec_{datetime.now():%Y%m%d_%H%M%S}.mp4")
        self.recording = True
        self.start_time = datetime.now()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self.path, "started"

    def _run(self):
        import mss
        import cv2
        import numpy as np
        try:
            with mss.mss() as s:
                mon = s.monitors[0]   # کل صفحه‌ها ترکیبی
                w = mon["width"] - (mon["width"] % 2)
                h = mon["height"] - (mon["height"] % 2)
                region = {"left": mon["left"], "top": mon["top"],
                          "width": w, "height": h}
                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                self._writer = cv2.VideoWriter(self.path, fourcc, 12, (w, h))
                while self.recording:
                    raw = np.array(s.grab(region))          # BGRA
                    frame = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)
                    self._writer.write(frame)
                    time.sleep(1 / 12)                      # ~12 fps
        except Exception as e:
            log.error("recorder error: %s", e)
        finally:
            if self._writer:
                self._writer.release()
            self._writer = None

    def stop(self):
        if not self.recording:
            return None
        self.recording = False
        if self._thread:
            self._thread.join(timeout=15)
        dur = datetime.now() - self.start_time
        secs = int(dur.total_seconds())
        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        return {"path": self.path, "seconds": secs, "size_mb": size / 1e6}


recorder = ScreenRecorder()


def send_video(chat_id, path: str, caption: str = "", delete_after: bool = False):
    log.info("SEND VIDEO → %s", path)
    sent_ok = False
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"{API}/sendVideo",
                data={"chat_id": chat_id, "caption": caption[:1000],
                      "supports_streaming": "true"},
                files={"video": (os.path.basename(path), f)},
                timeout=600, proxies=PROXIES,
            )
        sent_ok = bool(r.json().get("ok"))
        if not sent_ok:
            send(chat_id, f"خطا در ارسال ویدیو: {r.json().get('description', '?')}")
    except Exception as e:
        log.error("send_video failed: %s", e)
        send(chat_id, f"خطا در ارسال ویدیو: {e}")
    if delete_after and sent_ok and os.path.exists(path):
        try:
            os.remove(path)
            log.info("CLEANED (auto-delete) → %s", path)
        except Exception as e:
            log.error("auto-delete failed: %s", e)
    return sent_ok


# ---------------- Command handlers ----------------

def save_text_message(text: str) -> str:
    """متن دریافتی را به‌صورت فایل txt ذخیره می‌کند — پل اندروید↔ویندوز"""
    os.makedirs(TEXTS_DIR, exist_ok=True)
    fname = "text_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".txt"
    path = os.path.join(TEXTS_DIR, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def save_marked_note(chat_id: int, text: str):
    """پیام‌هایی که علامت ذخیره دارند → بدون آن علامت، تمیز ذخیره می‌شوند"""
    try:
        import re as _re
        clean = _re.sub(r"[\{\(\[«]\s*ذخیره\s*شود\s*[\}\)\]»]", "", text)
        clean = _re.sub(r"ذخیره\s*شود[:،,.!]?\s*$", "", clean).strip()
        if not clean:
            send(chat_id, "⚠ متن خالی بود — چیزی ذخیره نشد.")
            return
        saved = save_text_message(clean)
        send(chat_id, f"✅ ذخیره شد: {os.path.basename(saved)}")
    except Exception as e:
        log.error("save_marked_note failed: %s", e)
        send(chat_id, f"خطا در ذخیره: {e}")


def list_texts(limit: int = 10) -> str:
    """آخرین متن‌های ذخیره‌شده"""
    if not os.path.isdir(TEXTS_DIR):
        return "هنوز متنی ذخیره نشده."
    files = sorted(os.listdir(TEXTS_DIR), reverse=True)[:limit]
    if not files:
        return "هنوز متنی ذخیره نشده."
    return ("📝 آخرین متن‌ها:\n" +
            "\n".join("• " + f for f in files) +
            f"\n\nپوشه: {TEXTS_DIR}")


def handle_command(chat_id: int, text: str):
    global OC_WORKSPACE
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/start" or cmd == "/help":
        send(chat_id, (
            "🤖 دستیار هوشمند (مبتنی بر opencode)\n"
            "────────────────\n"
            "💬 هر متن معمولی بفرستی → مستقیم به هوش مصنوعی می‌رسد\n"
            "   مثال: «فایل منیجر را باز کن»\n"
            "   مثال: «پوشه X را بساز و فایل Y را داخلش بگذار»\n"
            "   مثال: «اسکرین‌شات بگیر» / «بگو رم چقدر خالی است»\n"
            "────────────────\n"
            "دستورات سریع:\n"
            "📸 /shot — اسکرین‌شات\n"
            "🎥 /rec — شروع ضبط ویدیوی صفحه\n"
            "⏹ /recstop — قطع ضبط\n"
            "📤 /recsend — ارسال ویدیو ضبط‌شده\n"
            "📄 /file مسیر — ارسال فایل\n"
            "📂 /ls مسیر — لیست پوشه\n"
            "🔍 /find اسم — جستجوی فایل\n"
            "⚡ /run دستور — اجرای PowerShell\n"
            "💻 /status — وضعیت سیستم\n"
            "📝 /texts — لیست متن‌های ذخیره‌شده\n"
            "   (برای ذخیره: آخر پیام بنویس {ذخیره شود})\n"
            "🆕 /new — پاک کردن حافظه AI\n"
            "🛑 /stop — توقف کار در حال اجرا\n"
            "⛔ /shutdown ok — خاموش کردن (تأیید لازم)\n"
            "🔄 /restart ok — ری‌استارت (تأیید لازم)\n"
            "🌙 /sleep — حالت خواب (S0)\n"
            "💾 /hibernate — نمدار کامل (بیداری با کلید پاور / Magic Packet)\n"
            "🔒 /lock — قفل ویندوز\n"
            "✅ /abort — لغو خاموشی در انتظار\n"
            "📁 /cd مسیر — تغییر پوشه کاری AI\n"
            "📥 فایل/عکس بفرستی → ذخیره در\n"
            f"   {DL_DIR}\n"
            f"📂 پوشه کاری فعلی AI: {OC_WORKSPACE}"
        ))

    elif cmd == "/cd":
        if not arg:
            send(chat_id, f"📂 پوشه کاری فعلی: {OC_WORKSPACE}")
            return
        p = os.path.expanduser(arg.strip().strip('"'))
        if os.path.isdir(p):
            OC_WORKSPACE = p
            send(chat_id, f"✅ پوشه کاری AI تغییر کرد به: {p}")
        else:
            send(chat_id, f"پوشه پیدا نشد: {p}")


    elif cmd == "/new":
        oc.reset()
        send(chat_id, "🆕 حافظه مکالمه پاک شد — سشن جدید.")

    elif cmd == "/stop":
        oc.abort_current()
        send(chat_id, "🛑 دستور توقف برای AI ارسال شد.")

    elif cmd == "/texts":
        send(chat_id, list_texts())

    elif cmd == "/shutdown":
        # خاموش کردن سیستم — نیاز به تأیید دو مرحله‌ای
        if arg.lower() in ("ok", "تایید", "تأیید", "yes"):
            send(chat_id, "⛔ سیستم تا 10 ثانیه دیگر خاموش می‌شود... "
                          "(برای لغو: /abort)")
            log.info("SHUTDOWN requested")
            run_powershell("shutdown /s /t 10 /c 'بات تلگرام: خاموشی'")
        else:
            send(chat_id, "⚠ مطمئنی؟ برای تأیید خاموشی بفرست:\n/shutdown ok\n"
                          "(برای لغو بعد از تأیید: /abort)")

    elif cmd == "/restart":
        if arg.lower() in ("ok", "تایید", "تأیید", "yes"):
            send(chat_id, "🔄 سیستم تا 10 ثانیه دیگر ری‌استارت می‌شود... "
                          "(برای لغو: /abort)")
            log.info("RESTART requested")
            run_powershell("shutdown /r /t 10 /c 'بات تلگرام: ری‌استارت'")
        else:
            send(chat_id, "⚠ مطمئنی؟ برای تأیید ری‌استارت بفرست:\n/restart ok\n"
                          "(برای لغو: /abort)")

    elif cmd == "/sleep":
        send(chat_id, "🌙 سیستم به حالت خواب رفت. (بیدار کردن: فقط با کلید/ماوس یا Wake-on-LAN)")
        run_powershell("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")

    elif cmd == "/abort":
        run_powershell("shutdown /a")
        send(chat_id, "✅ خاموشی/ری‌استارت در انتظار، لغو شد.")

    elif cmd == "/lock":
        send(chat_id, "🔒 ویندوز قفل شد.")
        run_powershell("rundll32.exe user32.dll,LockWorkStation")

    elif cmd == "/hibernate":
        send(chat_id, "💾 سیستم در حال Hibernate (نمدار)...\n"
                      "مصرف برق ≈ صفر. بیدار کردن: کلید پاور لپ‌تاپ، یا "
                      "Magic Packet از اپ گوشی (Wake On LAN) روی همان WiFi.")
        log.info("HIBERNATE requested")
        run_powershell("shutdown /h")

    elif cmd == "/rec":
        path, status = recorder.start()
        if status == "already":
            send(chat_id, "🔴 ضبط از قبل در حال اجراست! اول /recstop بزن.")
        else:
            send(chat_id, "🔴 ضبط ویدیو از صفحه شروع شد!\n"
                          "⏹ قطع: /recstop\n"
                          "📤 ارسال بعد از قطع: /recsend")

    elif cmd == "/recstop":
        info = recorder.stop()
        if info is None:
            send(chat_id, "⚠ ضبطی در حال اجرا نیست. شروع: /rec")
        else:
            send(chat_id, f"⏹ ضبط تمام شد!\n"
                          f"⏱ مدت: {info['seconds']} ثانیه\n"
                          f"💾 حجم: {info['size_mb']:.1f} MB\n"
                          f"📤 ارسال: /recsend")

    elif cmd == "/recsend":
        if recorder.recording:
            send(chat_id, "⚠ هنوز داری ضبط می‌کنی! اول /recstop بزن.")
            return
        p = recorder.path
        if p and os.path.exists(p):
            size_mb = os.path.getsize(p) / 1e6
            if size_mb > MAX_MB:
                send(chat_id, f"⚠ ویدیو {size_mb:.0f}MB است — بالاتر از سقف "
                              f"{MAX_MB}MB تلگرام. ضبط کوتاه‌تر کن.")
                return
            send(chat_id, "📤 در حال ارسال ویدیو…")
            send_video(chat_id, p, "🎬 ویدیوی صفحه", delete_after=True)
        else:
            send(chat_id, "⚠ ویدیویی برای ارسال نیست. اول ضبط کن: /rec")

    elif cmd == "/shot":
        send(chat_id, "📸 در حال گرفتن اسکرین‌شات…")
        try:
            for p in screenshot_all():
                send_photo(chat_id, p, delete_after=True)
        except Exception as e:
            log.exception("screenshot failed")
            send(chat_id, f"خطا: {e}")

    elif cmd == "/file":
        if not arg:
            send(chat_id, "مسیر بده: /file C:\\Users\\atlas\\file.txt")
            return
        p = os.path.expanduser(arg.strip().strip('"'))
        if os.path.isfile(p):
            send(chat_id, f"📤 در حال ارسال {os.path.basename(p)}…")
            send_file(chat_id, p)
        else:
            send(chat_id, f"فایل پیدا نشد: {p}")

    elif cmd == "/ls":
        send(chat_id, list_dir(arg or os.path.expanduser("~")))

    elif cmd == "/find":
        if not arg:
            send(chat_id, "اسم فایل را بده: /find گزارش")
            return
        send(chat_id, "🔍 در حال جستجو…")
        hits = find_file(os.path.expanduser("~"), arg)
        if hits:
            send(chat_id, "نتایج:\n" + "\n".join(
                f"{i+1}. {h}" for i, h in enumerate(hits)) +
                "\n\nبرای ارسال: /file <مسیر کامل>")
        else:
            send(chat_id, "چیزی پیدا نشد.")

    elif cmd == "/run":
        if not arg:
            send(chat_id, "دستور بده: /run Get-Date")
            return
        log.info("RUN: %s", arg)
        send(chat_id, f"⚡ اجرا: {arg}")
        send(chat_id, run_powershell(arg))

    elif cmd == "/status":
        send(chat_id, sys_status())

    else:
        send(chat_id, "دستور ناشناس. /help را بزن.")


# ---------------- File receiving ----------------

def save_incoming(msg: dict) -> str:
    """ذخیره فایل/عکس دریافتی از تلگرام"""
    file_id = None
    fname = None
    if "document" in msg:
        file_id = msg["document"]["file_id"]
        fname = msg["document"].get("file_name", "file.bin")
    elif "photo" in msg:
        file_id = msg["photo"][-1]["file_id"]  # بزرگ‌ترین سایز
        fname = f"photo_{datetime.now():%Y%m%d_%H%M%S}.jpg"
    elif "video" in msg:
        file_id = msg["video"]["file_id"]
        fname = msg["video"].get("file_name", f"video_{datetime.now():%H%M%S}.mp4")
    elif "audio" in msg:
        file_id = msg["audio"]["file_id"]
        fname = msg["audio"].get("file_name", f"audio_{datetime.now():%H%M%S}.mp3")
    if not file_id:
        return ""

    r = tg("getFile", file_id=file_id)
    if not r.get("ok"):
        return ""
    fp = r["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{TOKEN}/{fp}"
    dest = os.path.join(DL_DIR, fname)
    with requests.get(url, stream=True, timeout=300, proxies=PROXIES) as resp:
        with open(dest, "wb") as out:
            shutil.copyfileobj(resp.raw, out)
    return dest


# ---------------- Main loop (long polling) ----------------

offset = {"v": 0}


def poll():
    while True:
        try:
            r = requests.get(f"{API}/getUpdates",
                             params={"offset": offset["v"], "timeout": 30},
                             timeout=40, proxies=PROXIES)
            data = r.json()
            if not data.get("ok"):
                log.error("getUpdates error: %s", data)
                continue
            for upd in data.get("result", []):
                offset["v"] = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]
                user_id = msg["from"]["id"]
                if user_id != OWNER:
                    log.warning("UNAUTHORIZED from %s", user_id)
                    send(chat_id, "⛔ تو مالک این بات نیستی.")
                    continue

                if "text" in msg:
                    text = msg["text"].strip()
                    log.info("MSG from owner: %s", text[:80])
                    if text.startswith("/"):
                        threading.Thread(target=handle_command,
                                         args=(chat_id, text), daemon=True).start()
                    elif _re.search(r"[\{\(\[«]\s*ذخیره\s*شود\s*[\}\)\]»]", text) or \
                            _re.search(r"ذخیره\s*شود", text[-25:]):
                        # پیام‌هایی که علامت ذخیره دارند → تمیز ذخیره می‌شوند
                        threading.Thread(target=save_marked_note,
                                         args=(chat_id, text), daemon=True).start()
                    else:
                        # اول: لایه فوری (بدون AI)
                        quick = quick_match(text)
                        if quick:
                            log.info("QUICK MATCH: %s", quick[:60])
                            send(chat_id, quick)
                        else:
                            # غیر آن: به AI
                            threading.Thread(target=reply_with_ai,
                                             args=(chat_id, text), daemon=True).start()
                else:
                    threading.Thread(target=save_and_reply,
                                     args=(chat_id, msg), daemon=True).start()
        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            log.error("poll error: %s", e)
            import time
            time.sleep(3)


# ---------- لایه دستورات فوری (بدون AI) ----------

import re as _re


def quick_match(text: str):
    """دستورات پرتکرار را بدون AI فوری اجرا می‌کند؛ None یعنی مطابقت نداشت"""
    t = text.strip()

    # نور / روشنایی: «نور رو 30 کن» / «روشنایی 50 درصد»
    m = _re.search(r"(?:نور|روشنایی)[^\d]{0,20}(\d{1,3})", t)
    if m and any(w in t for w in ("کن", "بزار", "بذار", "تنظیم", "درصد", "بالا", "کم")):
        val = int(m.group(1))
        if 0 <= val <= 100:
            out = run_powershell(
                "(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods)"
                f".WmiSetBrightness(1, {val}) | Out-Null; 'OK'")
            if "OK" in out:
                return f"⚡ نور صفحه روی {val}٪ (فوری)"
            return "⛔ تنظیم نور ناموفق (مانیتور مجازی؟)"

    # صدا: «صدا رو 40 کن»
    m = _re.search(r"(?:صدا|ولوم|ولیوم)[^\d]{0,15}(\d{1,3})", t)
    if m and any(w in t for w in ("کن", "بزار", "بذار", "تنظیم", "درصد", "بالا", "کم")):
        val = int(m.group(1))
        if 0 <= val <= 100:
            out = run_powershell(f"Set-Volume -Max {val}; 'OK'")
            if "OK" in out:
                return f"⚡ صدا روی {val}٪ (فوری)"

    # باز کردن برنامه‌های معروف
    apps = {
        "ماشین حساب": "calc", "حساب": "calc",
        "نوت پد": "notepad", "نوتپد": "notepad",
        "تقویم": "explorer.exe shell:CalendarFolder",
        "عکس": "explorer.exe shell:My Pictures",
        "تصاویر": "explorer.exe shell:My Pictures",
        "دانلود": "explorer.exe shell:Downloads",
        "فایل منیجر": "explorer.exe",
        "مای اسناد": "explorer.exe shell:Personal",
    }
    for key, target in apps.items():
        if key in t and ("باز" in t or "بخش" in t or "رو" in t):
            run_powershell(f"Start-Process '{target}'")
            return f"⚡ باز شد: {key} (فوری)"

    # راهنما برای خاموش/ری‌استارت با جمله فارسی
    if any(w in t for w in ("خاموش", "شات داون", "shutdown")) and \
            any(w in t for w in ("کامپیوتر", "سیستم", "لپ", "پی سی", "pc")):
        return "⛔ برای خاموش کردن از دستور تأییددار استفاده کن:\n/shutdown ok"
    if any(w in t for w in ("ریستارت", "ری استارت", "restart")) and \
            any(w in t for w in ("کامپیوتر", "سیستم", "لپ", "پی سی", "pc")):
        return "⛔ برای ری‌استارت از دستور تأییددار استفاده کن:\n/restart ok"

    return None


def save_and_reply(chat_id: int, msg: dict):
    try:
        send(chat_id, "📥 در حال دریافت…")
        dest = save_incoming(msg)
        if dest:
            size = os.path.getsize(dest) / 1024
            send(chat_id, f"✅ ذخیره شد: {dest} ({size:,.0f} KB)")
        else:
            send(chat_id, "نشد ذخیره کنم.")
    except Exception as e:
        log.exception("save failed")
        send(chat_id, f"خطا: {e}")


def reply_with_ai(chat_id: int, text: str):
    """اجرای کار با AI + گزارش پیشرفت زنده"""
    prog = {"last": 0, "msg": None}

    def on_progress(pct, note):
        # هر 10% یا بیشتر از قبل، پیام بروزرسانی بفرست
        if pct >= prog["last"] + 10 or (pct == 8 and prog["last"] == 0):
            prog["last"] = pct
            try:
                if prog["msg"]:
                    tg("editMessageText", chat_id=chat_id,
                       message_id=prog["msg"],
                       text=f"⏳ پیشرفت: {pct}% — {note}")
                else:
                    r = tg("sendMessage", chat_id=chat_id,
                           text=f"⏳ پیشرفت: {pct}% — {note}")
                    if r.get("ok"):
                        prog["msg"] = r["result"]["message_id"]
            except Exception:
                pass

    try:
        answer = oc.chat_async(SYSTEM_HINT + "\n\nدرخواست کاربر: " + text,
                               on_progress=on_progress)
        # پاک کردن پیام پیشرفت و ارسال نتیجه نهایی
        if prog["msg"]:
            try:
                tg("deleteMessage", chat_id=chat_id, message_id=prog["msg"])
            except Exception:
                pass
        send(chat_id, answer)
        # اگر جواب مسیر فایل معتبری بود، خودکار بفرست
        for line in answer.splitlines():
            p = line.strip().strip("`").strip('"')
            if p.startswith(("C:\\", "/")) and os.path.isfile(p) and os.path.getsize(p) < MAX_MB * 1024 * 1024:
                if p.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                    send_photo(chat_id, p, "🖼 خروجی:")
                else:
                    send_file(chat_id, p, "📎 خروجی AI:")
                break
    except Exception as e:
        log.exception("ai reply failed")
        send(chat_id, f"خطا: {e}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    log.info("=== Remote Assistant starting ===")
    log.info("Owner: %s | Downloads: %s", OWNER, DL_DIR)
    send(OWNER, "🟢 بات آنلاین شد. /help برای راهنما.")
    poll()

"""تست سلامت نصب — قبل از اجرای بات این را اجرا کن"""
import json
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))
ok = True


def step(name, fn):
    global ok
    try:
        fn()
        print(f"  [OK] {name}")
    except Exception as e:
        ok = False
        print(f"  [FAIL] {name}: {e}")


print("=== بررسی نصب ===\n")

# 1) پایتون
step("پایتون نصب است", lambda: sys.version_info >= (3, 9) or (_ for _ in ()).throw(RuntimeError("پایتون قدیمی است")))

# 2) کتابخانه‌ها
step("کتابخانه requests", lambda: __import__("requests"))
step("کتابخانه psutil", lambda: __import__("psutil"))
step("کتابخانه mss", lambda: __import__("mss"))

# 3) config.json
def check_config():
    with open("config.json", encoding="utf-8") as f:
        c = json.load(f)
    assert "token" in c and c["token"], "token خالی است"
    assert "اینجا" not in c["token"], "توکن را از BotFather نگذاشتی!"
    assert c["owner_id"] and c["owner_id"] != 123456789, "owner_id را نگذاشتی!"
step("config.json درست پر شده", check_config)

# 4) اتصال به تلگرام
def check_telegram():
    import requests as rq
    with open("config.json", encoding="utf-8") as f:
        c = json.load(f)
    proxies = {"http": c["proxy"], "https": c["proxy"]} if c.get("proxy") else None
    r = rq.get(f"https://api.telegram.org/bot{c['token']}/getMe",
               proxies=proxies, timeout=15).json()
    assert r.get("ok"), f"تلگرام جواب نداد: {r}"
    print(f"        → بات: @{r['result']['username']}")
step("اتصال به تلگرام و اعتبار توکن", check_telegram)

# 5) opencode (اختیاری)
def check_opencode():
    import shutil
    found = shutil.which("opencode") or os.path.exists(
        os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "npm",
                     "node_modules", "opencode-ai"))
    assert found, "opencode نصب نیست (اختیاری — فقط دستورات ساده کار می‌کنند)"
step("opencode (اختیاری)", check_opencode)

print()
if ok:
    print("✅ همه چیز آماده است! حالا start.bat را اجرا کن.")
else:
    print("⚠ یکی از مراحل مشکل دارد — پیام FAIL را بخوان و README را ببین.")
    sys.exit(1)

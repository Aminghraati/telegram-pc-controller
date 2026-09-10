"""نصب اجرای خودکار بات هنگام روشن شدن ویندوز (Task Scheduler)"""
import subprocess
import sys

task_name = "RemoteAssistantBot"
bat = r"C:\Users\atlas\remote_assistant\start.bat"

cmd = [
    "schtasks", "/Create", "/F",
    "/TN", task_name,
    "/TR", f'"{bat}"',
    "/SC", "ONLOGON",      # با هر ورود به ویندوز اجرا شود
    "/RL", "LIMITED",
]
r = subprocess.run(cmd, capture_output=True, text=True)
print(r.stdout or r.stderr)
if "SUCCESS" in (r.stdout + r.stderr).upper():
    print("AUTOSTART INSTALLED")
else:
    print("FAILED")
    sys.exit(1)

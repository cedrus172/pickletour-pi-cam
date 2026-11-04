"""PiTailscale — web UI (Windows .exe entry point)."""
import argparse
import atexit
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import urllib3
from flask import Flask, jsonify, render_template, request

# Windows console mặc định cp1252 — in tiếng Việt sẽ UnicodeEncodeError.
# Ép stdout/stderr UTF-8 trước bất cứ print nào (argparse --help đọc sớm lắm).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import forwarder
import scanner

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Khi đóng gói bằng PyInstaller (onefile), templates/static được bundle và
# giải nén vào sys._MEIPASS tạm thời; dev mode thì vẫn cạnh app.py.
BASE_DIR = Path(getattr(sys, "_MEIPASS", str(Path(__file__).resolve().parent)))
app = Flask(__name__,
            template_folder=str(BASE_DIR / "templates"),
            static_folder=str(BASE_DIR / "static"))

_last_scan: dict = {}
_scan_lock = threading.Lock()


@app.route("/")
def index():
    return render_template("index.html")


@app.get("/api/network")
def api_network():
    return jsonify({
        "subnet": scanner.detect_local_subnet(),
        "tailscale_ip": scanner.get_tailscale_ip(),
        "hostname": socket.gethostname(),
    })


@app.post("/api/scan")
def api_scan():
    data = request.get_json(silent=True) or {}
    subnet = (data.get("subnet") or "").strip()
    with _scan_lock:
        result = scanner.scan(subnet)
    global _last_scan
    _last_scan = result
    return jsonify(result)


@app.get("/api/scan/last")
def api_scan_last():
    return jsonify(_last_scan or {"cameras": [], "others": []})


@app.post("/api/probe")
def api_probe():
    data = request.get_json(silent=True) or {}
    ip = data.get("ip")
    port = int(data.get("port") or 80)
    user = data.get("user", "")
    password = data.get("password", "")
    vendor = data.get("vendor", "")
    if not ip:
        return jsonify({"error": "ip required"}), 400
    result = scanner.probe_credentials(ip, port, user, password, vendor)
    result["ip"] = ip
    result["port"] = port
    return jsonify(result)


@app.get("/api/forwards")
def api_forwards_list():
    return jsonify({
        "tailscale_ip": scanner.get_tailscale_ip(),
        "forwards": forwarder.list_forwards(),
    })


@app.post("/api/forwards")
def api_forwards_create():
    data = request.get_json(silent=True) or {}
    for k in ("target_ip", "target_port"):
        if not data.get(k):
            return jsonify({"error": f"{k} required"}), 400
    ts_ip = scanner.get_tailscale_ip()
    if not ts_ip:
        return jsonify({"error": "Tailscale not available on this host"}), 500
    try:
        fw = forwarder.create_forward(
            tailscale_ip=ts_ip,
            name=data.get("name") or f"{data['target_ip']}:{data['target_port']}",
            target_ip=data["target_ip"],
            target_port=int(data["target_port"]),
            protocol=(data.get("protocol") or "tcp").lower(),
            notes=data.get("notes", ""),
        )
        return jsonify(fw)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.delete("/api/forwards/<int:fid>")
def api_forwards_delete(fid):
    return jsonify({"ok": forwarder.delete_forward(fid)})


@app.get("/api/system-forwards")
def api_system_forwards():
    ts_ip = scanner.get_tailscale_ip()
    system = scanner.detect_existing_forwards(ts_ip)
    managed = {(f["protocol"], f["listen_port"]) for f in forwarder.list_forwards()}
    for f in system:
        f["managed"] = (f["proto"], f["port"]) in managed
    return jsonify({"tailscale_ip": ts_ip, "forwards": system})


atexit.register(forwarder.shutdown_all)


# =============================================================================
# Install / uninstall — chạy trực tiếp từ .exe, không cần script ngoài
# =============================================================================

TASK_NAME = "PiTailscale"
FW_RULE = f"PiTailscale {os.environ.get('PT_PORT', '9999')}"


def _is_admin() -> bool:
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return os.name != "nt"  # dev trên Mac/Linux skip check


def _exe_path() -> str:
    """Đường dẫn chính xác tới .exe/script để Task Scheduler dùng."""
    if getattr(sys, "frozen", False):
        return sys.executable  # PyInstaller onefile → full path tới .exe
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def cmd_install(port: int):
    if os.name != "nt":
        print("install chỉ chạy trên Windows"); sys.exit(1)
    if not _is_admin():
        print("✗ Cần chạy as Administrator để đăng ký Task Scheduler.")
        print("  Chuột phải .exe → Run as administrator, rồi chạy lại: pitailscale.exe --install")
        sys.exit(1)

    exe = _exe_path()
    print(f"» Đăng ký task '{TASK_NAME}' chạy khi boot...")
    # Xoá task cũ nếu có
    subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                   capture_output=True)
    r = subprocess.run([
        "schtasks", "/Create", "/TN", TASK_NAME,
        "/TR", exe, "/SC", "ONSTART", "/RU", "SYSTEM", "/RL", "HIGHEST", "/F",
    ], capture_output=True, text=True)
    if r.returncode != 0:
        print("✗ schtasks fail:", r.stderr); sys.exit(1)
    print(f"✓ Task created")

    print(f"» Mở firewall TCP {port}...")
    subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule",
                    f"name={FW_RULE}"], capture_output=True)
    subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule",
                    f"name={FW_RULE}", "dir=in", "action=allow",
                    "protocol=TCP", f"localport={port}"], capture_output=True)
    print(f"✓ Firewall rule added")

    print(f"» Start task luôn...")
    subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True)
    print(f"✓ Đã start")
    print()
    print(f"  UI local      : http://localhost:{port}")
    print(f"  UI Tailscale  : http://<tailscale-ip>:{port}")
    print(f"  Stop          : schtasks /End /TN {TASK_NAME}")
    print(f"  Restart       : schtasks /End /TN {TASK_NAME} && schtasks /Run /TN {TASK_NAME}")
    print(f"  Uninstall     : pitailscale.exe --uninstall")


def cmd_uninstall():
    if os.name != "nt":
        print("uninstall chỉ chạy trên Windows"); sys.exit(1)
    if not _is_admin():
        print("✗ Cần chạy as Administrator"); sys.exit(1)
    print("» Stop task...")
    subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], capture_output=True)
    subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                   capture_output=True)
    subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule",
                    f"name={FW_RULE}"], capture_output=True)
    print("✓ Đã gỡ task + firewall rule")
    print("  (Data ở %USERPROFILE%\\PiTailscale\\data không bị xoá)")


def _startup():
    forwarder.restore_all(scanner.get_tailscale_ip())


def main():
    ap = argparse.ArgumentParser(prog="pitailscale",
                                 description="PiTailscale — camera gateway for Windows")
    ap.add_argument("--host", default=os.environ.get("PT_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PT_PORT", "9999")))
    ap.add_argument("--install", action="store_true",
                    help="Đăng ký Task Scheduler + mở firewall + start (cần admin)")
    ap.add_argument("--uninstall", action="store_true",
                    help="Gỡ task + firewall rule (cần admin)")
    args = ap.parse_args()

    if args.install:
        cmd_install(args.port); return
    if args.uninstall:
        cmd_uninstall(); return

    _startup()
    print(f"PiTailscale running on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()

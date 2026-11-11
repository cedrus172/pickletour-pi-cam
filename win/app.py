"""PiTailscale — unified Windows entry point. Multi-mode .exe.

Modes:
  --mode gateway  (default) → cổng scan+forward UDP/TCP (port 9999)
  --mode recorder            → admin NVR recorder (port 9997, cần ffmpeg)
  --mode clip                → public clip SPA cho khách (port 9996, cần ffmpeg)

Install (as Administrator):
  pitailscale.exe --install
    → đăng ký 3 Task Scheduler entries + 3 firewall rules
      (PiTailscale-Gateway / -Recorder / -Clip)
    → tự cài nmap qua winget nếu chưa có
    → bundled ffmpeg.exe trong .exe tự dùng được (prepend PATH)

Uninstall:
  pitailscale.exe --uninstall
"""
import argparse
import atexit
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

# Windows console mặc định cp1252 — in tiếng Việt sẽ UnicodeEncodeError.
# Ép stdout/stderr UTF-8 trước bất cứ print nào (argparse --help đọc sớm lắm).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# BASE_DIR: khi đóng gói PyInstaller onefile, templates/static/bin extract
# vào sys._MEIPASS tạm thời; dev mode thì vẫn cạnh app.py.
BASE_DIR = Path(getattr(sys, "_MEIPASS", str(Path(__file__).resolve().parent)))

# Prepend bundled ffmpeg.exe vào PATH để recorder/clip tìm thấy.
_ffbin = BASE_DIR / "bin"
if _ffbin.exists():
    os.environ["PATH"] = str(_ffbin) + os.pathsep + os.environ.get("PATH", "")

# Default data dir trên Windows nếu người dùng không set PT_DATA_DIR.
os.environ.setdefault(
    "PT_DATA_DIR",
    str(Path.home() / "PiTailscale" / "data"),
)

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# =============================================================================
# GATEWAY MODE (scanner + forwarder) — port 9999
# =============================================================================
def make_gateway_app():
    from flask import Flask, jsonify, render_template, request
    import forwarder
    import scanner

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
        nonlocal_last = result
        _last_scan.clear(); _last_scan.update(result)
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
        result["ip"] = ip; result["port"] = port
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

    forwarder.restore_all(scanner.get_tailscale_ip())
    atexit.register(forwarder.shutdown_all)
    return app


# =============================================================================
# RECORDER MODE (NVR admin) — port 9997
# =============================================================================
def make_recorder_app():
    # recorder.py đã tự tạo Flask `app` + endpoints, thumbnail worker, v.v.
    import recorder as _rec
    import rec_shared
    rec_shared.start_thumbnail_worker()
    return _rec.app


# =============================================================================
# CLIP MODE (public khách) — port 9996
# =============================================================================
def make_clip_app():
    import clipapp as _clip
    import rec_shared
    rec_shared.start_thumbnail_worker()
    return _clip.app


MODES = {
    "gateway":  (make_gateway_app,  9999, "PiTailscale-Gateway",  "gateway (scan LAN + forward UDP/TCP)"),
    "recorder": (make_recorder_app, 9997, "PiTailscale-Recorder", "recorder (admin NVR, cần ffmpeg)"),
    "clip":     (make_clip_app,     9996, "PiTailscale-Clip",     "clip SPA public cho khách (cần ffmpeg)"),
}


# =============================================================================
# Install / uninstall
# =============================================================================
def _is_admin() -> bool:
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return os.name != "nt"


def _exe_path() -> str:
    if getattr(sys, "frozen", False):
        return sys.executable
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def _check_bin(name: str, *extra_paths: str) -> bool:
    try:
        r = subprocess.run([name, "-version" if name in ("ffmpeg", "ffprobe") else "--version"],
                           capture_output=True, timeout=5)
        if r.returncode == 0:
            return True
    except Exception:
        pass
    for p in extra_paths:
        if os.path.exists(p):
            return True
    return False


def _check_nmap() -> bool:
    return _check_bin("nmap",
                      r"C:\Program Files (x86)\Nmap\nmap.exe",
                      r"C:\Program Files\Nmap\nmap.exe")


def _check_ffmpeg() -> bool:
    # ffmpeg bundled trong .exe sẽ có trong PATH do _ffbin ở trên.
    return _check_bin("ffmpeg")


def _find_winget() -> str:
    for name in ("winget.exe", "winget"):
        try:
            r = subprocess.run([name, "--version"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return name
        except Exception:
            pass
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        import glob
        pattern = os.path.join(local, "Microsoft", "WindowsApps", "winget.exe")
        if os.path.exists(pattern):
            return pattern
        for p in glob.glob(os.path.join(
            local, "Microsoft", "WindowsApps",
            "Microsoft.DesktopAppInstaller_*", "winget.exe")):
            return p
    return ""


def _install_nmap():
    print("» Nmap chưa có → thử cài qua winget...")
    winget = _find_winget()
    if winget:
        try:
            subprocess.run([winget, "install", "-e", "--id", "Insecure.Nmap",
                            "--accept-source-agreements", "--accept-package-agreements",
                            "--silent"], timeout=300)
            if _check_nmap():
                print("✓ Nmap installed qua winget"); return True
        except Exception as e:
            print(f"! winget fail: {e}")

    print("» Thử lại qua PowerShell...")
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-Command", "winget install -e --id Insecure.Nmap "
                        "--accept-source-agreements --accept-package-agreements --silent"],
                       timeout=300)
        if _check_nmap():
            print("✓ Nmap installed qua PowerShell+winget"); return True
    except Exception as e:
        print(f"! PowerShell+winget fail: {e}")

    print("» Fallback: tải nmap installer trực tiếp...")
    try:
        import tempfile, urllib.request
        url = "https://nmap.org/dist/nmap-7.95-setup.exe"
        tmp = os.path.join(tempfile.gettempdir(), "nmap-setup.exe")
        urllib.request.urlretrieve(url, tmp)
        subprocess.run([tmp, "/S"], timeout=300)
        if _check_nmap():
            print("✓ Nmap installed qua installer"); return True
    except Exception as e:
        print(f"! Download/install fail: {e}")

    print()
    print("! KHÔNG tự cài được nmap. Cài thủ công:")
    print("  https://nmap.org/download.html")
    return False


def cmd_install():
    if os.name != "nt":
        print("install chỉ chạy trên Windows"); sys.exit(1)
    if not _is_admin():
        print("✗ Cần chạy as Administrator để đăng ký Task Scheduler.")
        print("  Chuột phải .exe → Run as administrator, rồi chạy lại: pitailscale.exe --install")
        sys.exit(1)

    if not _check_nmap():
        _install_nmap()
    else:
        print("✓ Nmap đã có sẵn")

    if _check_ffmpeg():
        print("✓ ffmpeg đã có (bundled trong .exe)")
    else:
        print("! ffmpeg không tìm thấy — recorder/clip sẽ không ghi clip được.")

    exe = _exe_path()

    for mode, (_fn, port, task_name, desc) in MODES.items():
        print()
        print(f"» [{mode}] {desc} — port {port}")
        subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"],
                       capture_output=True)
        # /TR phải escape path + arg trong 1 string.
        tr_cmd = f'{exe} --mode {mode} --port {port}'
        r = subprocess.run([
            "schtasks", "/Create", "/TN", task_name,
            "/TR", tr_cmd, "/SC", "ONSTART",
            "/RU", "SYSTEM", "/RL", "HIGHEST", "/F",
        ], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  ✗ schtasks fail: {r.stderr.strip()}")
            continue
        print(f"  ✓ Task '{task_name}' created")
        # Firewall rule
        fw_rule = f"{task_name} TCP {port}"
        subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule",
                        f"name={fw_rule}"], capture_output=True)
        subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule",
                        f"name={fw_rule}", "dir=in", "action=allow",
                        "protocol=TCP", f"localport={port}"], capture_output=True)
        print(f"  ✓ Firewall rule added (TCP {port})")
        subprocess.run(["schtasks", "/Run", "/TN", task_name], capture_output=True)
        print(f"  ✓ Started")

    print()
    print("=" * 60)
    print(f"  Gateway  : http://localhost:9999  (hoặc http://<ts-ip>:9999)")
    print(f"  Recorder : http://localhost:9997  (admin NVR)")
    print(f"  Clip     : http://localhost:9996  (public khách)")
    print()
    print(f"  Stop     : schtasks /End /TN PiTailscale-Gateway (hoặc -Recorder/-Clip)")
    print(f"  Uninstall: pitailscale.exe --uninstall")


def cmd_uninstall():
    if os.name != "nt":
        print("uninstall chỉ chạy trên Windows"); sys.exit(1)
    if not _is_admin():
        print("✗ Cần chạy as Administrator"); sys.exit(1)
    for mode, (_fn, port, task_name, _desc) in MODES.items():
        print(f"» [{mode}] gỡ task + firewall...")
        subprocess.run(["schtasks", "/End", "/TN", task_name], capture_output=True)
        subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"],
                       capture_output=True)
        subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule",
                        f"name={task_name} TCP {port}"], capture_output=True)
    print("✓ Đã gỡ 3 task + firewall rule")
    print("  (Data ở %USERPROFILE%\\PiTailscale\\data không bị xoá)")


def main():
    ap = argparse.ArgumentParser(prog="pitailscale",
                                 description="PiTailscale — camera gateway/recorder/clip for Windows")
    ap.add_argument("--host", default=os.environ.get("PT_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PT_PORT", "0")),
                    help="Override port; mặc định tuỳ theo --mode (9999/9997/9996)")
    ap.add_argument("--mode", default="gateway", choices=list(MODES.keys()),
                    help="gateway (default) | recorder | clip")
    ap.add_argument("--install", action="store_true",
                    help="Đăng ký 3 Task Scheduler + firewall + start (cần admin)")
    ap.add_argument("--uninstall", action="store_true",
                    help="Gỡ tất cả task + firewall (cần admin)")
    args = ap.parse_args()

    if args.install:
        cmd_install(); return
    if args.uninstall:
        cmd_uninstall(); return

    make_fn, default_port, task_name, desc = MODES[args.mode]
    port = args.port or default_port
    app = make_fn()
    print(f"[PiTailscale {args.mode}] {desc}")
    print(f"→ http://{args.host}:{port}")
    app.run(host=args.host, port=port, threaded=True)


if __name__ == "__main__":
    main()

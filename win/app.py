"""PiTailscale — unified Windows entry point. Multi-mode .exe.

Modes:
  --mode gateway  (default) → cổng scan+forward UDP/TCP (port 9999)
  --mode recorder            → admin NVR recorder (port 9997, cần ffmpeg)
  --mode clip                → public clip SPA cho khách (port 9996, cần ffmpeg)

Install (as Administrator):
  pitailscale.exe --install
    → đăng ký 4 Task Scheduler entries + 3 firewall rules
      (PiTailscale-Gateway / -Recorder / -Clip / -Updater)
    → tự cài nmap qua winget nếu chưa có
    → bundled ffmpeg.exe trong .exe tự dùng được (prepend PATH)

Auto-update:
  Task -Updater chạy 3h sáng mỗi ngày → check GitHub Release mới hơn →
  tự tải .exe mới, swap file, restart 3 service. Không cần thao tác tay.
  Manual trigger: pitailscale.exe --update

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
import time
from pathlib import Path

# BUILD_NUMBER: CI pyinstaller --add-data ghi file build_info.py với số
# github.run_number. Dev mode không có file → default '0' (local build).
try:
    from build_info import BUILD_NUMBER
except Exception:
    BUILD_NUMBER = os.environ.get("PT_BUILD_NUMBER", "0")

# Auto-update endpoint (GitHub public repo)
UPDATE_REPO = "cedrus172/pickletour-pi-cam"
UPDATE_API = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"

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

    # Auto-update task: chạy 3h sáng mỗi ngày, silent mode
    print()
    print(f"» [updater] đăng ký auto-update daily 03:00...")
    subprocess.run(["schtasks", "/Delete", "/TN", UPDATER_TASK, "/F"],
                   capture_output=True)
    r = subprocess.run([
        "schtasks", "/Create", "/TN", UPDATER_TASK,
        "/TR", f'{exe} --update-silent',
        "/SC", "DAILY", "/ST", "03:00",
        "/RU", "SYSTEM", "/RL", "HIGHEST", "/F",
    ], capture_output=True, text=True)
    if r.returncode == 0:
        print(f"  ✓ Task '{UPDATER_TASK}' created (daily 03:00)")
    else:
        print(f"  ! updater schtasks fail: {r.stderr.strip()}")

    print()
    print("=" * 60)
    print(f"  Build    : build-{BUILD_NUMBER}")
    print(f"  Gateway  : http://localhost:9999  (hoặc http://<ts-ip>:9999)")
    print(f"  Recorder : http://localhost:9997  (admin NVR)")
    print(f"  Clip     : http://localhost:9996  (public khách)")
    print()
    print(f"  Stop     : schtasks /End /TN PiTailscale-Gateway (hoặc -Recorder/-Clip)")
    print(f"  Update   : pitailscale.exe --update  (manual)")
    print(f"  Uninstall: pitailscale.exe --uninstall")


UPDATER_TASK = "PiTailscale-Updater"


def _parse_build_num(tag: str) -> int:
    """Parse 'build-15' → 15. Trả -1 nếu format khác."""
    if not tag:
        return -1
    s = tag.rsplit("-", 1)[-1] if "-" in tag else tag
    try:
        return int(s)
    except ValueError:
        return -1


def cmd_version():
    print(f"PiTailscale build-{BUILD_NUMBER}")


def cmd_update(silent: bool = False):
    """Tải .exe mới từ GitHub Release nếu có build-N cao hơn hiện tại.
    Self-replace qua Windows rename trick (có thể rename file .exe đang chạy).
    Sau khi swap xong, restart 3 service task — Task Scheduler tự launch lại
    qua .exe path, process cũ chết, process mới dùng binary mới.
    silent=True: không print + không fail nếu đã latest (dùng cho cron)."""
    # Dùng `requests` thay `urllib.request` vì PyInstaller onefile không
    # bundle Windows system CA store → urllib.request báo
    # CERTIFICATE_VERIFY_FAILED. requests tự dùng certifi CA (được bundle).
    import requests

    def say(msg):
        if not silent:
            print(msg)

    headers = {"User-Agent": "pitailscale-updater"}
    say(f"» current: build-{BUILD_NUMBER}")
    say(f"» checking {UPDATE_API}")
    try:
        r = requests.get(UPDATE_API, headers=headers, timeout=15)
        r.raise_for_status()
        rel = r.json()
    except Exception as e:
        say(f"✗ API fetch fail: {e}")
        return 1

    tag = rel.get("tag_name", "")
    new_num = _parse_build_num(tag)
    cur_num = _parse_build_num(f"build-{BUILD_NUMBER}")
    say(f"» latest: {tag} (build-{new_num})")

    if new_num <= cur_num:
        say(f"✓ already latest — no update needed.")
        return 0

    exe_asset = next((a for a in rel.get("assets", [])
                      if a["name"].lower().endswith(".exe")), None)
    if not exe_asset:
        say(f"✗ release {tag} không có .exe asset")
        return 2
    url = exe_asset["browser_download_url"]
    size_mb = exe_asset.get("size", 0) / 1024 / 1024
    say(f"» downloading {size_mb:.1f} MB ...")

    me = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    tmp = me.with_name(me.stem + ".new" + me.suffix)
    old = me.with_name(me.stem + ".old" + me.suffix)

    try:
        with requests.get(url, headers=headers, stream=True, timeout=300) as rr:
            rr.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in rr.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
    except Exception as e:
        try: tmp.unlink()
        except Exception: pass
        say(f"✗ download fail: {e}")
        return 3

    # Windows rename trick: file .exe đang chạy CÓ THỂ rename (khác với
    # Unix, nơi inode open vẫn tồn tại). Delete .old nếu còn từ lần trước.
    try: old.unlink()
    except FileNotFoundError: pass
    except Exception: pass

    if not getattr(sys, "frozen", False):
        # Dev mode (chạy từ .py): KHÔNG swap, chỉ report
        say(f"✓ dev mode: downloaded to {tmp}")
        return 0

    try:
        me.rename(old)
        tmp.rename(me)
    except Exception as e:
        say(f"✗ swap fail: {e}")
        try: tmp.unlink()
        except Exception: pass
        return 4

    # Restart 3 service. Task Scheduler sẽ launch .exe mới (cùng path).
    # Process updater này tự chết sau khi return (Windows không kill
    # runaway rename-ed exe process).
    say(f"» restart 3 service task...")
    for mode, (_fn, _port, task_name, _desc) in MODES.items():
        subprocess.run(["schtasks", "/End", "/TN", task_name], capture_output=True)
    time.sleep(1)
    for mode, (_fn, _port, task_name, _desc) in MODES.items():
        subprocess.run(["schtasks", "/Run", "/TN", task_name], capture_output=True)

    # Cleanup .old best-effort (có thể bị lock)
    time.sleep(2)
    try: old.unlink()
    except Exception: pass

    say(f"✓ UPDATED build-{cur_num} → build-{new_num}")
    return 0


def cmd_uninstall():
    if os.name != "nt":
        print("uninstall chỉ chạy trên Windows"); sys.exit(1)
    if not _is_admin():
        print("✗ Cần chạy as Administrator"); sys.exit(1)
    all_tasks = [t for _, _, t, _ in MODES.values()] + [UPDATER_TASK]
    for mode, (_fn, port, task_name, _desc) in MODES.items():
        print(f"» [{mode}] gỡ task + firewall...")
        subprocess.run(["schtasks", "/End", "/TN", task_name], capture_output=True)
        subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"],
                       capture_output=True)
        subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule",
                        f"name={task_name} TCP {port}"], capture_output=True)
    # Updater task riêng (không có firewall)
    print(f"» [updater] gỡ task...")
    subprocess.run(["schtasks", "/End", "/TN", UPDATER_TASK], capture_output=True)
    subprocess.run(["schtasks", "/Delete", "/TN", UPDATER_TASK, "/F"],
                   capture_output=True)

    # Kill mọi pitailscale.exe process còn sống (orphan sau schtasks /End).
    # Ngoại trừ chính process uninstall này — nhưng /IM filter + /F sẽ cover
    # luôn, OS tự skip self vì đang chạy phần code trước khi exit.
    print("» Kill mọi process pitailscale.exe còn sống...")
    me = os.getpid()
    try:
        # /FI "PID ne N" loại trừ chính mình; /T kill cây con luôn.
        r = subprocess.run(
            ["taskkill", "/IM", "pitailscale.exe", "/T", "/F",
             "/FI", f"PID ne {me}"],
            capture_output=True, text=True, timeout=10,
        )
        print(f"  {r.stdout.strip() or r.stderr.strip() or '(no orphan)'}")
    except Exception as e:
        print(f"  ! taskkill fail: {e}")

    print()
    print("✓ Đã gỡ 3 task + firewall rule + kill process")
    print("  (Data ở %USERPROFILE%\\PiTailscale\\data không bị xoá — xoá tay nếu muốn)")


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
    ap.add_argument("--update", action="store_true",
                    help="Check + tải bản mới từ GitHub Release, swap .exe, restart services")
    ap.add_argument("--update-silent", action="store_true",
                    help="Giống --update nhưng không in log + return 0 nếu đã latest (cron)")
    ap.add_argument("--version", action="store_true",
                    help="In build number hiện tại")
    args = ap.parse_args()

    if args.version:
        cmd_version(); return
    if args.update:
        sys.exit(cmd_update(silent=False))
    if args.update_silent:
        sys.exit(cmd_update(silent=True))
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

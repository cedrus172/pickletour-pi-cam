"""PiTailscale — web UI to discover LAN cameras/DVRs and expose them via Tailscale."""
import argparse
import atexit
import os
import socket
import threading
from pathlib import Path

import urllib3
from flask import Flask, jsonify, render_template, request

import forwarder
import scanner

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"),
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
    required = ["target_ip", "target_port"]
    for k in required:
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
    ok = forwarder.delete_forward(fid)
    return jsonify({"ok": ok})


@app.get("/api/system-forwards")
def api_system_forwards():
    """All TCP/UDP ports listening on tailscale/wildcard IPs (incl. external socat/nginx)."""
    ts_ip = scanner.get_tailscale_ip()
    system = scanner.detect_existing_forwards(ts_ip)
    managed = {(f["protocol"], f["listen_port"]) for f in forwarder.list_forwards()}
    for f in system:
        f["managed"] = (f["proto"], f["port"]) in managed
    return jsonify({"tailscale_ip": ts_ip, "forwards": system})


def _startup():
    forwarder.restore_all(scanner.get_tailscale_ip())


atexit.register(forwarder.shutdown_all)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("PT_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PT_PORT", "9999")))
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    _startup()
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()

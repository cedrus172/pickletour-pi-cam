"""PiTailscale system monitor — Flask app serving live CPU/RAM/process stats."""
import argparse
import os
import socket
import time
from pathlib import Path

import psutil
from flask import Flask, jsonify, render_template

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"),
            static_folder=str(BASE_DIR / "static"))

# Prime cpu_percent (first call returns 0.0)
psutil.cpu_percent(interval=None, percpu=False)
psutil.cpu_percent(interval=None, percpu=True)


def _read_temp() -> float:
    """Pi CPU temperature in °C, or 0 if not readable."""
    for p in ("/sys/class/thermal/thermal_zone0/temp",
              "/sys/devices/virtual/thermal/thermal_zone0/temp"):
        try:
            return int(Path(p).read_text().strip()) / 1000.0
        except Exception:
            continue
    return 0.0


def _net_speeds() -> dict:
    """Rough per-interface rx/tx bytes/sec sampled over ~1s between calls."""
    now = time.time()
    counters = psutil.net_io_counters(pernic=True)
    out = {}
    last = _net_speeds._last if hasattr(_net_speeds, "_last") else None
    for name, c in counters.items():
        if name in ("lo",):
            continue
        rx, tx = c.bytes_recv, c.bytes_sent
        if last and name in last[1]:
            dt = max(now - last[0], 0.001)
            prev = last[1][name]
            out[name] = {
                "rx_bps": max(0, int((rx - prev["rx"]) / dt)),
                "tx_bps": max(0, int((tx - prev["tx"]) / dt)),
                "rx_total": rx,
                "tx_total": tx,
            }
        else:
            out[name] = {"rx_bps": 0, "tx_bps": 0, "rx_total": rx, "tx_total": tx}
    _net_speeds._last = (now, {n: {"rx": c.bytes_recv, "tx": c.bytes_sent}
                               for n, c in counters.items()})
    return out


@app.route("/")
def index():
    return render_template("monitor.html")


@app.get("/api/stats")
def api_stats():
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    cpu_percpu = psutil.cpu_percent(interval=None, percpu=True)
    cpu_avg = sum(cpu_percpu) / len(cpu_percpu) if cpu_percpu else 0.0
    load1, load5, load15 = os.getloadavg()
    try:
        freq = psutil.cpu_freq()
        cpu_mhz = int(freq.current) if freq else 0
    except Exception:
        cpu_mhz = 0

    return jsonify({
        "ts": time.time(),
        "hostname": socket.gethostname(),
        "uptime": int(time.time() - psutil.boot_time()),
        "cpu": {
            "percent": round(cpu_avg, 1),
            "per_core": [round(x, 1) for x in cpu_percpu],
            "count": psutil.cpu_count(logical=True),
            "mhz": cpu_mhz,
            "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
        },
        "mem": {
            "total": vm.total, "used": vm.used, "free": vm.available,
            "percent": vm.percent,
        },
        "swap": {
            "total": sm.total, "used": sm.used, "percent": sm.percent,
        },
        "disk": {
            "total": disk.total, "used": disk.used, "free": disk.free,
            "percent": disk.percent,
        },
        "temp_c": round(_read_temp(), 1),
        "net": _net_speeds(),
    })


@app.get("/api/processes")
def api_processes():
    procs = []
    for p in psutil.process_iter(attrs=[
        "pid", "name", "username", "cpu_percent", "memory_percent",
        "memory_info", "status", "cmdline",
    ]):
        try:
            info = p.info
            mem_mb = info["memory_info"].rss / (1024 * 1024) if info.get("memory_info") else 0
            cmd = " ".join(info.get("cmdline") or [])[:200]
            procs.append({
                "pid": info["pid"],
                "name": info.get("name") or "",
                "user": info.get("username") or "",
                "cpu": round(info.get("cpu_percent") or 0.0, 1),
                "mem_pct": round(info.get("memory_percent") or 0.0, 1),
                "mem_mb": round(mem_mb, 1),
                "status": info.get("status") or "",
                "cmd": cmd,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda p: (p["cpu"], p["mem_pct"]), reverse=True)
    return jsonify({"ts": time.time(), "count": len(procs), "processes": procs[:50]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("PT_MONITOR_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PT_MONITOR_PORT", "9998")))
    args = ap.parse_args()
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()

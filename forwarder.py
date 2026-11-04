"""Manage socat-based port forwards bound to the Tailscale interface."""
import os
import signal
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("PT_DATA_DIR", "/opt/pitailscale/data")) / "forwards.db"
PORT_MIN = int(os.environ.get("PT_PORT_MIN", "10000"))
PORT_MAX = int(os.environ.get("PT_PORT_MAX", "19999"))

_lock = threading.Lock()


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS forwards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        target_ip TEXT NOT NULL,
        target_port INTEGER NOT NULL,
        protocol TEXT NOT NULL DEFAULT 'tcp',
        listen_port INTEGER NOT NULL UNIQUE,
        pid INTEGER,
        created_at REAL NOT NULL,
        notes TEXT DEFAULT ''
    )""")
    return conn


def _socat_running(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _next_free_port(conn, tailscale_ip: str) -> int:
    used = {r["listen_port"] for r in conn.execute("SELECT listen_port FROM forwards")}
    for p in range(PORT_MIN, PORT_MAX + 1):
        if p in used:
            continue
        with socket_test(tailscale_ip, p) as free:
            if free:
                return p
    raise RuntimeError("No free port in range")


class socket_test:
    """Context manager returning True if (ip,port) is free to bind."""
    def __init__(self, ip, port):
        self.ip = ip
        self.port = port
        self.free = False

    def __enter__(self):
        import socket as _s
        s = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        try:
            s.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
            s.bind((self.ip, self.port))
            self.free = True
        except OSError:
            self.free = False
        finally:
            s.close()
        return self.free

    def __exit__(self, *a):
        return False


def _start_socat(tailscale_ip: str, listen_port: int,
                 target_ip: str, target_port: int, protocol: str = "tcp") -> int:
    proto = protocol.lower()
    if proto == "udp":
        src = f"UDP4-LISTEN:{listen_port},bind={tailscale_ip},fork,reuseaddr"
        dst = f"UDP4:{target_ip}:{target_port}"
    else:
        src = f"TCP4-LISTEN:{listen_port},bind={tailscale_ip},fork,reuseaddr"
        dst = f"TCP4:{target_ip}:{target_port}"
    log = Path(os.environ.get("PT_DATA_DIR", "/opt/pitailscale/data")) / "forwards.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    f = open(log, "ab")
    p = subprocess.Popen(
        ["socat", "-d", src, dst],
        stdout=f, stderr=f, start_new_session=True,
    )
    time.sleep(0.3)
    if p.poll() is not None:
        raise RuntimeError(f"socat exited immediately (code {p.returncode}); check {log}")
    return p.pid


def list_forwards() -> list[dict]:
    with _lock, _db() as conn:
        rows = conn.execute("SELECT * FROM forwards ORDER BY id").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["alive"] = _socat_running(d["pid"] or 0)
            result.append(d)
        return result


def create_forward(tailscale_ip: str, name: str, target_ip: str,
                   target_port: int, protocol: str = "tcp",
                   notes: str = "") -> dict:
    if not tailscale_ip:
        raise ValueError("Tailscale IP not available")
    with _lock, _db() as conn:
        listen_port = _next_free_port(conn, tailscale_ip)
        pid = _start_socat(tailscale_ip, listen_port, target_ip, target_port, protocol)
        cur = conn.execute(
            "INSERT INTO forwards (name,target_ip,target_port,protocol,listen_port,pid,created_at,notes) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (name, target_ip, int(target_port), protocol, listen_port, pid, time.time(), notes),
        )
        conn.commit()
        fid = cur.lastrowid
        row = conn.execute("SELECT * FROM forwards WHERE id=?", (fid,)).fetchone()
    d = dict(row)
    d["alive"] = _socat_running(d["pid"])
    return d


def delete_forward(fid: int) -> bool:
    with _lock, _db() as conn:
        row = conn.execute("SELECT * FROM forwards WHERE id=?", (fid,)).fetchone()
        if not row:
            return False
        pid = row["pid"] or 0
        if _socat_running(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception:
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass
            time.sleep(0.3)
            if _socat_running(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
        conn.execute("DELETE FROM forwards WHERE id=?", (fid,))
        conn.commit()
    return True


def restore_all(tailscale_ip: str) -> None:
    """On startup: restart socat for any forward whose process died."""
    if not tailscale_ip:
        return
    with _lock, _db() as conn:
        rows = conn.execute("SELECT * FROM forwards").fetchall()
        for r in rows:
            if _socat_running(r["pid"] or 0):
                continue
            try:
                pid = _start_socat(tailscale_ip, r["listen_port"],
                                   r["target_ip"], r["target_port"], r["protocol"])
                conn.execute("UPDATE forwards SET pid=? WHERE id=?", (pid, r["id"]))
            except Exception:
                continue
        conn.commit()


def shutdown_all() -> None:
    for f in list_forwards():
        if f.get("alive"):
            try:
                os.killpg(os.getpgid(f["pid"]), signal.SIGTERM)
            except Exception:
                pass

"""Port forwards bằng Python socket (thay cho socat trên Pi).
Mỗi forward = 1 listener thread + per-connection pump thread, chạy trong
process của app. Lưu metadata vào SQLite; khi app restart, restore_all() tự
spawn listener lại từ DB. Không có PID — thay bằng object reference trong
_active[fid]."""
import os
import select
import socket
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("PT_DATA_DIR", str(Path.home() / "PiTailscale" / "data"))) / "forwards.db"
PORT_MIN = int(os.environ.get("PT_PORT_MIN", "10000"))
PORT_MAX = int(os.environ.get("PT_PORT_MAX", "19999"))

_lock = threading.Lock()
_active: dict = {}  # fid -> Forwarder instance


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


# -------- Core forwarders (TCP + UDP, pure-Python) --------

class TCPForwarder:
    """Bind trên bind_ip:listen_port, mỗi inbound mở connection ra target."""
    def __init__(self, bind_ip, listen_port, target_ip, target_port, name=""):
        self.bind_ip = bind_ip
        self.listen_port = listen_port
        self.target_ip = target_ip
        self.target_port = int(target_port)
        self.name = name
        self._server = None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.bind_ip, self.listen_port))
        s.listen(64)
        s.settimeout(1.0)
        self._server = s
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name=f"tcp-fwd-{self.listen_port}")
        self._thread.start()

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,),
                             daemon=True).start()

    def _handle(self, client):
        try:
            backend = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            backend.settimeout(5)
            backend.connect((self.target_ip, self.target_port))
            backend.settimeout(None)
        except Exception:
            try: client.close()
            except Exception: pass
            return
        self._pump(client, backend)

    @staticmethod
    def _pump(a, b):
        socks = [a, b]
        try:
            while True:
                r, _, _ = select.select(socks, [], [], 60)
                if not r:
                    break
                for s in r:
                    try:
                        data = s.recv(65536)
                    except OSError:
                        return
                    if not data:
                        return
                    other = b if s is a else a
                    try:
                        other.sendall(data)
                    except OSError:
                        return
        finally:
            for s in (a, b):
                try: s.close()
                except Exception: pass

    def stop(self):
        self._stop.set()
        try: self._server.close()
        except Exception: pass
        if self._thread:
            self._thread.join(timeout=2)

    def alive(self):
        return bool(self._thread and self._thread.is_alive())


class UDPForwarder:
    """UDP forward đơn giản: lưu client cuối cùng, forward 2 chiều.
    Đủ cho hầu hết camera (RTP một sender/receiver). Nếu cần multi-client
    đồng thời, chuyển sang bản session-table."""
    def __init__(self, bind_ip, listen_port, target_ip, target_port, name=""):
        self.bind_ip = bind_ip
        self.listen_port = listen_port
        self.target_ip = target_ip
        self.target_port = int(target_port)
        self.name = name
        self._front = None
        self._back = None
        self._client = None
        self._stop = threading.Event()
        self._thread_f = None
        self._thread_b = None

    def start(self):
        f = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        f.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        f.bind((self.bind_ip, self.listen_port))
        f.settimeout(1.0)
        b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        b.settimeout(1.0)
        self._front = f
        self._back = b
        self._thread_f = threading.Thread(target=self._loop_front, daemon=True,
                                          name=f"udp-fwd-f-{self.listen_port}")
        self._thread_b = threading.Thread(target=self._loop_back, daemon=True,
                                          name=f"udp-fwd-b-{self.listen_port}")
        self._thread_f.start()
        self._thread_b.start()

    def _loop_front(self):
        while not self._stop.is_set():
            try:
                data, addr = self._front.recvfrom(65536)
                self._client = addr
                self._back.sendto(data, (self.target_ip, self.target_port))
            except socket.timeout:
                continue
            except OSError:
                break

    def _loop_back(self):
        while not self._stop.is_set():
            try:
                data, _ = self._back.recvfrom(65536)
                if self._client:
                    self._front.sendto(data, self._client)
            except socket.timeout:
                continue
            except OSError:
                break

    def stop(self):
        self._stop.set()
        for s in (self._front, self._back):
            try: s.close()
            except Exception: pass
        for t in (self._thread_f, self._thread_b):
            if t: t.join(timeout=2)

    def alive(self):
        return bool(self._thread_f and self._thread_f.is_alive())


# -------- Port picking + app-facing API --------

def _port_free(ip: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((ip, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _next_free_port(conn, tailscale_ip: str) -> int:
    used = {r["listen_port"] for r in conn.execute("SELECT listen_port FROM forwards")}
    for p in range(PORT_MIN, PORT_MAX + 1):
        if p in used:
            continue
        if _port_free(tailscale_ip, p):
            return p
    raise RuntimeError("No free port in range")


def _spawn(tailscale_ip, listen_port, target_ip, target_port, protocol):
    cls = UDPForwarder if (protocol or "tcp").lower() == "udp" else TCPForwarder
    fw = cls(tailscale_ip, listen_port, target_ip, target_port)
    fw.start()
    time.sleep(0.1)
    if not fw.alive():
        raise RuntimeError(f"forwarder listener didn't start on {tailscale_ip}:{listen_port}")
    return fw


def list_forwards() -> list:
    with _lock, _db() as conn:
        rows = conn.execute("SELECT * FROM forwards ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            fw = _active.get(d["id"])
            d["alive"] = bool(fw and fw.alive())
            out.append(d)
        return out


def create_forward(tailscale_ip: str, name: str, target_ip: str,
                   target_port: int, protocol: str = "tcp",
                   notes: str = "") -> dict:
    if not tailscale_ip:
        raise ValueError("Tailscale IP not available")
    with _lock, _db() as conn:
        listen_port = _next_free_port(conn, tailscale_ip)
        fw = _spawn(tailscale_ip, listen_port, target_ip, target_port, protocol)
        cur = conn.execute(
            "INSERT INTO forwards (name,target_ip,target_port,protocol,"
            "listen_port,pid,created_at,notes) VALUES (?,?,?,?,?,?,?,?)",
            (name, target_ip, int(target_port), protocol, listen_port,
             0, time.time(), notes),
        )
        conn.commit()
        fid = cur.lastrowid
        _active[fid] = fw
        row = conn.execute("SELECT * FROM forwards WHERE id=?", (fid,)).fetchone()
    d = dict(row)
    d["alive"] = fw.alive()
    return d


def delete_forward(fid: int) -> bool:
    with _lock, _db() as conn:
        row = conn.execute("SELECT * FROM forwards WHERE id=?", (fid,)).fetchone()
        if not row:
            return False
        fw = _active.pop(fid, None)
        if fw:
            try: fw.stop()
            except Exception: pass
        conn.execute("DELETE FROM forwards WHERE id=?", (fid,))
        conn.commit()
    return True


def restore_all(tailscale_ip: str) -> None:
    """On startup: spawn listener cho mọi row trong DB."""
    if not tailscale_ip:
        return
    with _lock, _db() as conn:
        rows = conn.execute("SELECT * FROM forwards").fetchall()
        for r in rows:
            fid = r["id"]
            if fid in _active and _active[fid].alive():
                continue
            try:
                fw = _spawn(tailscale_ip, r["listen_port"],
                            r["target_ip"], r["target_port"], r["protocol"])
                _active[fid] = fw
            except Exception:
                continue


def shutdown_all() -> None:
    for fid, fw in list(_active.items()):
        try: fw.stop()
        except Exception: pass
    _active.clear()

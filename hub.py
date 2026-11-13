"""PiTailscale Hub — trang quản lý TẤT CẢ sân (Pi lẫn DDNS).

Chạy trên VPS (pickletour). Cổng mặc định 9995.

Hai loại sân:
  - ddns : sân KHÔNG có Pi. NVR truy cập qua DDNS. Chính VPS này chạy
           recorder(9997)+clip(9996) để xem live + cắt clip cho sân đó.
           Data NVR nằm trong recorder DB (bảng nvrs) — mỗi NVR = 1 sân.
  - pi   : sân CÓ Pi. Pi tự chạy recorder+clip. Hub chỉ link tới qua
           Tailscale IP. Lưu trong bảng pi_venues của hub.

Hub KHÔNG tự cắt clip — nó điều hướng sang recorder/clip tương ứng:
  - sân DDNS : /admin → http://<vps>:9997 (recorder, lọc theo nvr)
               /clip  → http://<vps>:9996/san/<slug>
  - sân Pi   : /admin → http://<pi>:9997 ; /clip → http://<pi>:9996
"""
import os
import re
import socket
import subprocess
import time
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request, Response

import rec_shared as rs
import vendors as _v

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"),
            static_folder=str(BASE_DIR / "static"))

# Cổng recorder/clip chạy NGAY TRÊN VPS này (phục vụ sân DDNS)
LOCAL_RECORDER_PORT = int(os.environ.get("PT_RECORDER_PORT", "9997"))
LOCAL_CLIP_PORT = int(os.environ.get("PT_CLIP_PORT", "9996"))
# Base URL cho link Admin/Clip của sân DDNS. Nếu đặt (vd qua domain HTTPS)
# thì dùng thẳng; nếu trống → fallback http://<host client đang vào>:<port>.
#   PT_ADMIN_BASE=https://nvr.pickletour.vn
#   PT_CLIP_BASE=https://clip.pickletour.vn
ADMIN_BASE = os.environ.get("PT_ADMIN_BASE", "").rstrip("/")
CLIP_BASE = os.environ.get("PT_CLIP_BASE", "").rstrip("/")
# Pi mặc định dùng port nào (đồng nhất toàn hệ thống)
PI_RECORDER_PORT = int(os.environ.get("PT_PI_RECORDER_PORT", "9997"))
PI_CLIP_PORT = int(os.environ.get("PT_PI_CLIP_PORT", "9996"))

try:
    from build_info import BUILD_NUMBER as _BV
    BUILD_LABEL = f"build-{_BV}"
except Exception:
    BUILD_LABEL = "dev"


# ---------------------------------------------------------------------------
# DB: pi_venues (sân có Pi). NVR DDNS lấy thẳng từ recorder DB (rs.db).
# ---------------------------------------------------------------------------
def _hub_db():
    conn = rs.db()  # cùng file recorder.db, thêm bảng pi_venues
    conn.execute("""CREATE TABLE IF NOT EXISTS pi_venues (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        host TEXT NOT NULL,              -- Tailscale IP/hostname của Pi
        recorder_port INTEGER DEFAULT 9997,
        clip_port INTEGER DEFAULT 9996,
        note TEXT DEFAULT '',
        created_at REAL NOT NULL
    )""")
    # URL công khai (vd Cloudflare qtrung.pickletour.vn). Trống → fallback
    # http://<tailscale-ip>:<port> (chỉ chạy trong tailnet).
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pi_venues)").fetchall()}
        if "public_clip_url" not in cols:
            conn.execute("ALTER TABLE pi_venues ADD COLUMN public_clip_url TEXT DEFAULT ''")
        if "public_admin_url" not in cols:
            conn.execute("ALTER TABLE pi_venues ADD COLUMN public_admin_url TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass
    return conn


def _slugify(name: str) -> str:
    s = name.lower().strip()
    s = (s.replace("đ", "d").replace("ă", "a").replace("â", "a")
          .replace("ê", "e").replace("ô", "o").replace("ơ", "o")
          .replace("ư", "u").replace("á", "a").replace("à", "a"))
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "san"


# ---------------------------------------------------------------------------
# Status probing (cache để không spam NVR/Pi mỗi lần load trang)
# ---------------------------------------------------------------------------
_status_cache: dict = {}
_status_lock = threading.Lock()
STATUS_TTL = 30  # giây


def _tcp_open(host: str, port: int, timeout: float = 4.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _probe_ddns(nvr: dict) -> dict:
    """Trạng thái 1 NVR DDNS: online + model + số cam online."""
    key = f"ddns:{nvr['id']}"
    now = time.time()
    with _status_lock:
        c = _status_cache.get(key)
        if c and now - c["ts"] < STATUS_TTL:
            return c["data"]
    info = _v.test_connection(nvr, timeout=6)
    data = {"online": bool(info.get("ok")),
            "model": info.get("model", info.get("info", "")),
            "error": info.get("error", "")}
    if data["online"]:
        try:
            chs = _v.list_channels(nvr, timeout=6)
            data["cams_total"] = len(chs)
            data["cams_online"] = sum(1 for x in chs if x.get("online", True))
        except Exception:
            data["cams_total"] = data["cams_online"] = None
    with _status_lock:
        _status_cache[key] = {"ts": now, "data": data}
    return data


def _probe_pi(pv: dict) -> dict:
    """Trạng thái 1 Pi: recorder port mở không."""
    key = f"pi:{pv['id']}"
    now = time.time()
    with _status_lock:
        c = _status_cache.get(key)
        if c and now - c["ts"] < STATUS_TTL:
            return c["data"]
    online = _tcp_open(pv["host"], pv["recorder_port"], timeout=4)
    data = {"online": online}
    with _status_lock:
        _status_cache[key] = {"ts": now, "data": data}
    return data


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.context_processor
def _inject():
    return {"build_label": BUILD_LABEL,
            "admin_base": ADMIN_BASE or ""}


@app.route("/")
def index():
    return render_template("hub.html")


@app.get("/api/venues")
def api_venues():
    """Danh sách tất cả sân (ddns + pi) kèm trạng thái."""
    host_for_links = request.host.split(":")[0]  # IP/hostname client dùng để vào hub
    venues = []

    # Sân DDNS = mỗi NVR trong recorder DB
    with rs.db() as conn:
        try:
            nvr_rows = conn.execute(
                "SELECT id, name, host, COALESCE(web_port,80) web_port, "
                "rtsp_port, user, password, COALESCE(vendor,'dahua') vendor, "
                "channels FROM nvrs ORDER BY id").fetchall()
        except Exception:
            nvr_rows = []
    for r in nvr_rows:
        nvr = dict(r)
        slug = _slugify(nvr["name"])
        st = _probe_ddns(nvr)
        venues.append({
            "type": "ddns", "id": nvr["id"], "name": nvr["name"],
            "slug": slug, "host": nvr["host"],
            "vendor": nvr["vendor"],
            "online": st["online"], "model": st.get("model", ""),
            "error": st.get("error", ""),
            "cams_total": st.get("cams_total"),
            "cams_online": st.get("cams_online"),
            "admin_url": (f"{ADMIN_BASE}/?nvr={nvr['id']}" if ADMIN_BASE
                          else f"http://{host_for_links}:{LOCAL_RECORDER_PORT}/?nvr={nvr['id']}"),
            "clip_url": (f"{CLIP_BASE}/san/{slug}" if CLIP_BASE
                         else f"http://{host_for_links}:{LOCAL_CLIP_PORT}/san/{slug}"),
            "thumb_url": f"/api/venue/ddns/{nvr['id']}/thumb",
        })

    # Sân Pi
    with _hub_db() as conn:
        pi_rows = conn.execute("SELECT * FROM pi_venues ORDER BY id").fetchall()
    for r in pi_rows:
        pv = dict(r)
        st = _probe_pi(pv)
        pub_clip = (pv.get("public_clip_url") or "").strip()
        pub_admin = (pv.get("public_admin_url") or "").strip()
        venues.append({
            "type": "pi", "id": pv["id"], "name": pv["name"],
            "slug": pv["slug"], "host": pv["host"],
            "online": st["online"], "note": pv.get("note", ""),
            # Ưu tiên URL công khai (Cloudflare…); fallback Tailscale IP
            "admin_url": pub_admin or f"http://{pv['host']}:{pv['recorder_port']}/",
            "clip_url": pub_clip or f"http://{pv['host']}:{pv['clip_port']}/",
            "has_public": bool(pub_clip or pub_admin),
            "thumb_url": "",
        })

    return jsonify({"venues": venues, "build": BUILD_LABEL,
                    "counts": {"total": len(venues),
                               "ddns": len(nvr_rows), "pi": len(pi_rows)}})


@app.get("/api/venue/ddns/<int:nid>/thumb")
def api_ddns_thumb(nid):
    """Proxy snapshot ch1 của NVR DDNS (cache ngắn)."""
    with rs.db() as conn:
        r = conn.execute(
            "SELECT host, COALESCE(web_port,80) web_port, rtsp_port, user, "
            "password, COALESCE(vendor,'dahua') vendor FROM nvrs WHERE id=?",
            (nid,)).fetchone()
    if not r:
        return Response(status=404)
    nvr = dict(r)
    ch = int(request.args.get("channel", 1))
    jpg = _v.snapshot_bytes(nvr, ch, timeout=6)
    if not jpg:
        return Response(status=503)
    return Response(jpg, mimetype="image/jpeg",
                    headers={"Cache-Control": "max-age=20"})


# ----- Pi venues CRUD -----
@app.get("/api/pi-venues")
def api_pi_list():
    with _hub_db() as conn:
        rows = conn.execute("SELECT * FROM pi_venues ORDER BY id").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/pi-venues")
def api_pi_add():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    host = (data.get("host") or "").strip()
    if not name or not host:
        return jsonify({"error": "name + host required"}), 400
    slug = _slugify(data.get("slug") or name)
    with _hub_db() as conn:
        # tránh trùng slug
        n = 1
        base = slug
        while conn.execute("SELECT 1 FROM pi_venues WHERE slug=?", (slug,)).fetchone():
            n += 1
            slug = f"{base}-{n}"
        cur = conn.execute(
            "INSERT INTO pi_venues (name,slug,host,recorder_port,clip_port,note,"
            "public_clip_url,public_admin_url,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, slug, host, int(data.get("recorder_port", PI_RECORDER_PORT)),
             int(data.get("clip_port", PI_CLIP_PORT)), data.get("note", ""),
             (data.get("public_clip_url") or "").strip(),
             (data.get("public_admin_url") or "").strip(),
             time.time()))
        conn.commit()
        vid = cur.lastrowid
    return jsonify({"id": vid, "slug": slug})


@app.patch("/api/pi-venues/<int:vid>")
def api_pi_edit(vid):
    data = request.get_json(silent=True) or {}
    sets, vals = [], []
    for k in ("name", "host", "recorder_port", "clip_port", "note",
              "public_clip_url", "public_admin_url"):
        if k in data:
            v = data[k]
            if k in ("recorder_port", "clip_port"):
                try: v = int(v)
                except Exception: continue
            sets.append(f"{k}=?"); vals.append(v)
    if not sets:
        return jsonify({"error": "nothing to update"}), 400
    vals.append(vid)
    with _hub_db() as conn:
        conn.execute(f"UPDATE pi_venues SET {','.join(sets)} WHERE id=?", vals)
        conn.commit()
    return jsonify({"ok": True})


@app.delete("/api/pi-venues/<int:vid>")
def api_pi_del(vid):
    with _hub_db() as conn:
        conn.execute("DELETE FROM pi_venues WHERE id=?", (vid,))
        conn.commit()
    return jsonify({"ok": True})


@app.get("/api/version")
def api_version():
    return jsonify({"build": BUILD_LABEL})


def main():
    import argparse
    ap = argparse.ArgumentParser(prog="pitailscale-hub")
    ap.add_argument("--host", default=os.environ.get("PT_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PT_HUB_PORT", "9995")))
    args = ap.parse_args()
    _hub_db().close()  # ensure table
    print(f"PiTailscale Hub on http://{args.host}:{args.port}  ({BUILD_LABEL})")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()

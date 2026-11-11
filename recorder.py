"""PiTailscale recorder — NVR live view, playback & clip extraction (Dahua)."""
import argparse
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth
from flask import Flask, abort, jsonify, render_template, request, send_from_directory, send_file, Response

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PT_DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "recorder.db"
STREAM_DIR = DATA_DIR / "streams"
CLIPS_DIR = DATA_DIR / "clips"

THUMBS_DIR = DATA_DIR / "thumbs"
for d in (DATA_DIR, STREAM_DIR, CLIPS_DIR, THUMBS_DIR):
    d.mkdir(parents=True, exist_ok=True)

THUMB_TTL = 60  # seconds

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"),
            static_folder=str(BASE_DIR / "static"))

# session_id -> {"pid": int, "dir": Path, "started": float, "last_access": float, "nvr_id": int}
_sessions: dict = {}
_sessions_lock = threading.Lock()
SESSION_IDLE_SECS = 120

# clip_id -> {"path": Path, "created": float, "status": "running"|"done"|"error",
#             "pid": int|None, "err": str, "size": int}
_clips: dict = {}
_clips_lock = threading.Lock()


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""CREATE TABLE IF NOT EXISTS nvrs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        host TEXT NOT NULL,
        rtsp_port INTEGER NOT NULL DEFAULT 554,
        user TEXT NOT NULL,
        password TEXT NOT NULL,
        channels INTEGER NOT NULL DEFAULT 16,
        created_at REAL NOT NULL
    )""")
    # Migration: clip_watermark column (optional text burned into exported clips)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nvrs)").fetchall()}
    if "clip_watermark" not in cols:
        conn.execute("ALTER TABLE nvrs ADD COLUMN clip_watermark TEXT DEFAULT ''")
    if "clip_wm_pos" not in cols:
        conn.execute("ALTER TABLE nvrs ADD COLUMN clip_wm_pos TEXT DEFAULT 'bl'")
    if "clip_wm_scroll" not in cols:
        conn.execute("ALTER TABLE nvrs ADD COLUMN clip_wm_scroll INTEGER DEFAULT 0")
    if "clip_wm_size" not in cols:
        # Tỷ lệ font theo chiều cao video: fontsize = h / clip_wm_size
        # 40 = nhỏ (≈27px @1080p), 28 = vừa (≈38px), 20 = to (≈54px)
        conn.execute("ALTER TABLE nvrs ADD COLUMN clip_wm_size INTEGER DEFAULT 40")
    if "clip_source" not in cols:
        # 'rtsp'    = RTSP playback (realtime, chạy OK mọi mode)
        # 'fastload'= loadfile.cgi (nhanh ~2x, chỉ áp dụng mode=playback)
        conn.execute("ALTER TABLE nvrs ADD COLUMN clip_source TEXT DEFAULT 'rtsp'")
    if "clip_encoder" not in cols:
        # 'libx264' = CPU, chậm nhưng ổn + chất lượng tốt (BẮT BUỘC nếu có watermark và HW hỏng)
        # 'h264_v4l2m2m' = HW encode trên Pi 4 VPU (nhanh 3-5×, có thể fail nếu firmware chưa setup)
        # 'copy' = không re-encode, nhanh nhất (nhưng KHÔNG BAKE được watermark)
        conn.execute("ALTER TABLE nvrs ADD COLUMN clip_encoder TEXT DEFAULT 'libx264'")
    if "vendor" not in cols:
        # 'dahua' | 'hikvision' — chọn API backend
        conn.execute("ALTER TABLE nvrs ADD COLUMN vendor TEXT DEFAULT 'dahua'")
    conn.execute("""CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nvr_id INTEGER NOT NULL,
        channel INTEGER NOT NULL,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        subtype INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        UNIQUE(nvr_id, channel),
        FOREIGN KEY (nvr_id) REFERENCES nvrs(id) ON DELETE CASCADE
    )""")
    return conn


def _rtsp_url(nvr: dict, channel: int, mode: str = "live",
              start: str = "", end: str = "", subtype: int = 0) -> str:
    """Vendor-aware RTSP URL builder. Dispatch sang vendors.rtsp_live /
    rtsp_playback. nvr dict phải có 'vendor' (fallback 'dahua' nếu thiếu)."""
    import vendors as _v
    nvr = dict(nvr); nvr.setdefault("vendor", "dahua")
    if mode == "live":
        return _v.rtsp_live(nvr, channel, subtype)
    # Playback: Dahua giữ 'YYYY_MM_DD_HH_MM_SS', Hik cần 'YYYYMMDDTHHMMSSZ'.
    if _v.vendor_of(nvr) == "hikvision":
        if "_" in start:
            start = _v.dahua_time_to_hik(start)
        if "_" in end:
            end = _v.dahua_time_to_hik(end)
    return _v.rtsp_playback(nvr, channel, start, end)


def _parse_dt(s: str) -> str:
    """Accept 'YYYY-MM-DDTHH:MM:SS' or 'YYYY-MM-DD HH:MM:SS' → Dahua 'YYYY_MM_DD_HH_MM_SS'."""
    s = s.strip().replace("T", " ").replace("-", "_").replace(":", "_").replace(" ", "_")
    return s


def _kill_pid(pid: int) -> None:
    import rec_shared as _rs
    _rs.kill_pid(pid)


# ---------------- NVR CRUD ----------------
@app.route("/")
def index():
    return render_template("recorder.html")


@app.get("/api/nvrs")
def api_list_nvrs():
    with _db() as conn:
        rows = conn.execute("SELECT * FROM nvrs ORDER BY id").fetchall()
    return jsonify([{**dict(r), "password": "***"} for r in rows])


@app.post("/api/nvrs")
def api_add_nvr():
    data = request.get_json(silent=True) or {}
    for k in ("name", "host", "user", "password"):
        if not data.get(k):
            return jsonify({"error": f"{k} required"}), 400
    vendor = (data.get("vendor") or "dahua").lower()
    if vendor not in ("dahua", "hikvision"):
        vendor = "dahua"
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO nvrs (name,host,rtsp_port,user,password,channels,vendor,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (data["name"], data["host"], int(data.get("rtsp_port", 554)),
             data["user"], data["password"], int(data.get("channels", 16)),
             vendor, time.time()),
        )
        conn.commit()
        nid = cur.lastrowid
    return jsonify({"id": nid})


@app.delete("/api/nvrs/<int:nid>")
def api_delete_nvr(nid):
    with _db() as conn:
        conn.execute("DELETE FROM nvrs WHERE id=?", (nid,))
        conn.commit()
    return jsonify({"ok": True})


@app.patch("/api/nvrs/<int:nid>")
def api_update_nvr(nid):
    """Update NVR fields. Empty-string password is skipped (keep old)."""
    data = request.get_json(silent=True) or {}
    sets, vals = [], []
    allow_empty = {"clip_watermark"}  # empty string = clear watermark
    int_fields = {"rtsp_port", "channels", "clip_wm_scroll", "clip_wm_size"}
    for k in ("name", "host", "rtsp_port", "user", "password", "channels",
              "clip_watermark", "clip_wm_pos", "clip_wm_scroll", "clip_wm_size",
              "clip_source", "clip_encoder", "vendor"):
        if k not in data:
            continue
        v = data[k]
        if k == "password" and (v is None or v == ""):
            continue  # keep existing
        if v is None:
            continue
        if v == "" and k not in allow_empty:
            continue
        if k in int_fields:
            try: v = int(v)
            except Exception: continue
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return jsonify({"error": "nothing to update"}), 400
    vals.append(nid)
    with _db() as conn:
        cur = conn.execute(f"UPDATE nvrs SET {','.join(sets)} WHERE id=?", vals)
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "nvr not found"}), 404
    return jsonify({"ok": True})


@app.post("/api/nvrs/<int:nid>/test")
def api_test_nvr(nid):
    import vendors as _v
    with _db() as conn:
        row = conn.execute("SELECT * FROM nvrs WHERE id=?", (nid,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "nvr not found"}), 404
    nvr = dict(row)
    nvr.setdefault("vendor", "dahua")
    # TCP connect test
    try:
        with socket.create_connection((nvr["host"], nvr["rtsp_port"]), timeout=5):
            pass
    except Exception as e:
        return jsonify({"ok": False, "error": f"TCP connect fail: {e}"})
    # 1. Vendor deviceInfo probe (phân biệt rõ cred sai vs network sai)
    dev = _v.test_connection(nvr, timeout=6)
    if not dev.get("ok"):
        return jsonify({"ok": False, "error": f"API probe fail: {dev.get('error', '')}"})
    # 2. ffprobe RTSP test
    channel = int(request.args.get("channel", 1))
    url = _v.rtsp_live(nvr, channel, subtype=1)
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
             "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height",
             "-of", "default=noprint_wrappers=1", "-timeout", "5000000", url],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            info_lines = [dev.get("info", ""), r.stdout.strip()]
            return jsonify({"ok": True, "info": " | ".join(x for x in info_lines if x),
                            "vendor": nvr.get("vendor")})
        return jsonify({"ok": False, "error": r.stderr.strip()[:400]})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "ffprobe timeout"})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "ffprobe not installed on server"})


@app.get("/api/nvrs/<int:nid>/encode")
def api_get_encode(nid):
    """Fetch encode config (codec/resolution/fps/bitrate) — vendor-aware."""
    import vendors as _v
    channel = int(request.args.get("channel", 1))
    with _db() as conn:
        row = conn.execute("SELECT * FROM nvrs WHERE id=?", (nid,)).fetchone()
    if not row:
        return jsonify({"error": "nvr not found"}), 404
    nvr = dict(row); nvr.setdefault("vendor", "dahua")
    return jsonify(_v.get_encode_info(nvr, channel, timeout=10))


@app.get("/api/nvrs/<int:nid>/osd")
def api_get_osd(nid):
    """Fetch OSD config cho 1 channel — vendor-aware (reuse bulk)."""
    import vendors as _v
    channel = int(request.args.get("channel", 1))
    with _db() as conn:
        row = conn.execute("SELECT * FROM nvrs WHERE id=?", (nid,)).fetchone()
    if not row:
        return jsonify({"error": "nvr not found"}), 404
    nvr = dict(row); nvr.setdefault("vendor", "dahua")
    all_osd = _v.fetch_osd_all(nvr, timeout=10)
    for d in all_osd:
        if d.get("channel") == channel:
            return jsonify(d)
    return jsonify({"channel": channel, "channel_title": None,
                    "time_title": None, "channel_name": ""})


@app.get("/api/nvrs/<int:nid>/osd/all")
def api_get_osd_all(nid):
    """Fetch OSD config cho TẤT CẢ channel — vendor-aware."""
    import vendors as _v
    with _db() as conn:
        row = conn.execute("SELECT * FROM nvrs WHERE id=?", (nid,)).fetchone()
    if not row:
        return jsonify({"error": "nvr not found"}), 404
    nvr = dict(row); nvr.setdefault("vendor", "dahua")
    try:
        channels = _v.fetch_osd_all(nvr, timeout=15)
    except Exception as e:
        return jsonify({"error": f"fetch_osd_all fail: {e}"}), 500
    return jsonify({"channels": channels, "count": len(channels),
                    "vendor": nvr.get("vendor", "dahua")})


@app.put("/api/nvrs/<int:nid>/osd/bulk")
def api_set_osd_bulk(nid):
    """Gửi nhiều channel OSD updates trong 1 request — vendor-aware.
    Body: {changes: [{channel, channel_title?, time_title?, channel_name?}, ...]}"""
    import vendors as _v
    data = request.get_json(silent=True) or {}
    changes = data.get("changes") or []
    if not isinstance(changes, list) or not changes:
        return jsonify({"error": "changes list required"}), 400
    with _db() as conn:
        row = conn.execute("SELECT * FROM nvrs WHERE id=?", (nid,)).fetchone()
    if not row:
        return jsonify({"error": "nvr not found"}), 404
    nvr = dict(row); nvr.setdefault("vendor", "dahua")
    try:
        result = _v.apply_osd_bulk(nvr, changes, timeout=30)
    except Exception as e:
        return jsonify({"ok": False, "error": f"apply_osd_bulk fail: {e}"}), 500
    return jsonify(result)


@app.put("/api/nvrs/<int:nid>/osd")
def api_set_osd(nid):
    """Single-channel OSD toggle — dispatch qua bulk apply."""
    import vendors as _v
    data = request.get_json(silent=True) or {}
    with _db() as conn:
        row = conn.execute("SELECT * FROM nvrs WHERE id=?", (nid,)).fetchone()
    if not row:
        return jsonify({"error": "nvr not found"}), 404
    nvr = dict(row); nvr.setdefault("vendor", "dahua")
    item = {"channel": int(data.get("channel", 1))}
    for k in ("channel_title", "time_title", "channel_name"):
        if k in data:
            item[k] = data[k]
    if len(item) == 1:
        return jsonify({"error": "nothing to set"}), 400
    return jsonify(_v.apply_osd_bulk(nvr, [item], timeout=15))


# ---- Dahua logo (text overlay as watermark) ----
LOGO_POSITIONS = {
    # Dahua coords 0..8192
    "tr": {"rect": (6400, 100, 8100, 400),  "align": 2},
    "br": {"rect": (6400, 7700, 8100, 7980), "align": 2},
    "tl": {"rect": (100, 100, 1800, 400),  "align": 0},
    "bl": {"rect": (100, 7700, 1800, 7980), "align": 0},
}
LOGO_SLOT = 0  # VideoWidget[ch].CustomTitle[0] — slot free, không đè time/channel title

def _logo_set_params(ch_idx, text, position, color):
    pos = LOGO_POSITIONS.get(position, LOGO_POSITIONS["br"])
    r = pos["rect"]
    enabled = bool(text and text.strip())
    v = "true" if enabled else "false"
    rgb = color or (255, 255, 255, 255)
    out = [
        f"VideoWidget[{ch_idx}].CustomTitle[{LOGO_SLOT}].EncodeBlend={v}",
        f"VideoWidget[{ch_idx}].CustomTitle[{LOGO_SLOT}].PreviewBlend={v}",
        f"VideoWidget[{ch_idx}].CustomTitle[{LOGO_SLOT}].Rect[0]={r[0]}",
        f"VideoWidget[{ch_idx}].CustomTitle[{LOGO_SLOT}].Rect[1]={r[1]}",
        f"VideoWidget[{ch_idx}].CustomTitle[{LOGO_SLOT}].Rect[2]={r[2]}",
        f"VideoWidget[{ch_idx}].CustomTitle[{LOGO_SLOT}].Rect[3]={r[3]}",
        f"VideoWidget[{ch_idx}].CustomTitle[{LOGO_SLOT}].TextAlign={pos['align']}",
        f"VideoWidget[{ch_idx}].CustomTitle[{LOGO_SLOT}].FrontColor[0]={rgb[0]}",
        f"VideoWidget[{ch_idx}].CustomTitle[{LOGO_SLOT}].FrontColor[1]={rgb[1]}",
        f"VideoWidget[{ch_idx}].CustomTitle[{LOGO_SLOT}].FrontColor[2]={rgb[2]}",
        f"VideoWidget[{ch_idx}].CustomTitle[{LOGO_SLOT}].FrontColor[3]={rgb[3]}",
        f"VideoWidget[{ch_idx}].CustomTitle[{LOGO_SLOT}].BackColor[3]=0",
    ]
    if enabled:
        import urllib.parse as _up
        out.insert(1, f"VideoWidget[{ch_idx}].CustomTitle[{LOGO_SLOT}].Text="
                      + _up.quote(text.strip(), safe=""))
    else:
        out.insert(1, f"VideoWidget[{ch_idx}].CustomTitle[{LOGO_SLOT}].Text=")
    return out

def _dahua_setconfig(host, user, password, params):
    import requests
    from requests.auth import HTTPDigestAuth
    encoded = [p.replace("[", "%5B").replace("]", "%5D") for p in params]
    url = (f"http://{host}/cgi-bin/configManager.cgi?action=setConfig&"
           + "&".join(encoded))
    r = requests.get(url, auth=HTTPDigestAuth(user, password), timeout=15, verify=False)
    body = r.text.strip()
    return r.status_code == 200 and ("OK" in body.upper() or body == ""), r.status_code, body[:200]

@app.get("/api/nvrs/<int:nid>/logo")
def api_get_logo(nid):
    """Read current logo (CustomTitle[0]) for a channel."""
    import requests
    from requests.auth import HTTPDigestAuth
    channel = int(request.args.get("channel", 1)) - 1
    with _db() as conn:
        row = conn.execute("SELECT * FROM nvrs WHERE id=?", (nid,)).fetchone()
    if not row:
        return jsonify({"error": "nvr not found"}), 404
    nvr = dict(row)
    try:
        r = requests.get(f"http://{nvr['host']}/cgi-bin/configManager.cgi?action=getConfig&name=VideoWidget[{channel}]",
                         auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                         timeout=10, verify=False)
    except Exception as e:
        return jsonify({"error": f"request fail: {e}"}), 500
    if r.status_code != 200:
        return jsonify({"error": f"HTTP {r.status_code}"}), 500
    out = {"channel": channel + 1, "enabled": False, "text": "", "rect": None, "color": None}
    prefix = f"table.VideoWidget[{channel}].CustomTitle[{LOGO_SLOT}]."
    rect = [None]*4
    color = [None]*4
    for line in r.text.splitlines():
        if not line.startswith(prefix):
            continue
        key, _, val = line[len(prefix):].partition("=")
        val = val.strip()
        if key == "EncodeBlend":
            out["enabled"] = val.lower() == "true"
        elif key == "Text":
            out["text"] = val
        elif key.startswith("Rect["):
            idx = int(key[5])
            try: rect[idx] = int(val)
            except: pass
        elif key.startswith("FrontColor["):
            idx = int(key[11])
            try: color[idx] = int(val)
            except: pass
    out["rect"] = rect
    out["color"] = color
    return jsonify(out)

@app.put("/api/nvrs/<int:nid>/logo")
def api_set_logo(nid):
    """Set/clear logo overlay for a single channel."""
    data = request.get_json(silent=True) or {}
    channel = int(data.get("channel", 1)) - 1
    text = data.get("text", "")
    position = data.get("position", "br")
    color = data.get("color")  # [r,g,b,a] 0..255, optional
    if color and (not isinstance(color, list) or len(color) != 4):
        return jsonify({"error": "color must be [r,g,b,a]"}), 400
    with _db() as conn:
        row = conn.execute("SELECT * FROM nvrs WHERE id=?", (nid,)).fetchone()
    if not row:
        return jsonify({"error": "nvr not found"}), 404
    nvr = dict(row)
    params = _logo_set_params(channel, text, position, tuple(color) if color else None)
    try:
        ok, status, body = _dahua_setconfig(nvr["host"], nvr["user"], nvr["password"], params)
    except Exception as e:
        return jsonify({"ok": False, "error": f"request fail: {e}"}), 500
    return jsonify({"ok": ok, "status": status, "response": body})

@app.post("/api/nvrs/<int:nid>/logo/apply-all")
def api_logo_apply_all(nid):
    """Apply logo to EVERY channel registered for this NVR."""
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    position = data.get("position", "br")
    color = data.get("color")
    if color and (not isinstance(color, list) or len(color) != 4):
        return jsonify({"error": "color must be [r,g,b,a]"}), 400
    with _db() as conn:
        nvr_row = conn.execute("SELECT * FROM nvrs WHERE id=?", (nid,)).fetchone()
        if not nvr_row:
            return jsonify({"error": "nvr not found"}), 404
        chs = [r["channel"] for r in conn.execute(
            "SELECT DISTINCT channel FROM channels WHERE nvr_id=? ORDER BY channel", (nid,)
        ).fetchall()]
    if not chs:
        return jsonify({"error": "no channels configured for this NVR"}), 400
    nvr = dict(nvr_row)
    results = []
    for ch in chs:
        params = _logo_set_params(ch - 1, text, position, tuple(color) if color else None)
        try:
            ok, status, body = _dahua_setconfig(nvr["host"], nvr["user"], nvr["password"], params)
            results.append({"channel": ch, "ok": ok, "status": status})
        except Exception as e:
            results.append({"channel": ch, "ok": False, "error": str(e)})
    done = sum(1 for r in results if r.get("ok"))
    return jsonify({"ok": done == len(results), "applied": done, "total": len(results), "results": results})


@app.put("/api/nvrs/<int:nid>/encode")
def api_set_encode(nid):
    """Change Dahua codec for a channel (main and/or sub)."""
    import requests
    from requests.auth import HTTPDigestAuth
    data = request.get_json(silent=True) or {}
    channel = int(data.get("channel", 1)) - 1
    main = data.get("main")  # H.264 / H.265 / MJPEG
    sub = data.get("sub")
    with _db() as conn:
        row = conn.execute("SELECT * FROM nvrs WHERE id=?", (nid,)).fetchone()
    if not row:
        return jsonify({"error": "nvr not found"}), 404
    nvr = dict(row)
    params = []
    if main:
        params.append(f"Encode[{channel}].MainFormat[0].Video.Compression={main}")
    if sub:
        params.append(f"Encode[{channel}].ExtraFormat[0].Video.Compression={sub}")
    if not params:
        return jsonify({"error": "nothing to set (give main or sub)"}), 400
    url = (f"http://{nvr['host']}/cgi-bin/configManager.cgi?action=setConfig&"
           + "&".join(params))
    try:
        r = requests.get(url, auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                         timeout=15, verify=False)
    except Exception as e:
        return jsonify({"ok": False, "error": f"request fail: {e}"}), 500
    body = r.text.strip()
    ok = r.status_code == 200 and "OK" in body.upper()
    return jsonify({"ok": ok, "status": r.status_code, "response": body[:300]})


# ---------------- Streaming (HLS) ----------------
def _cleanup_old_sessions():
    now = time.time()
    with _sessions_lock:
        for sid in list(_sessions.keys()):
            s = _sessions[sid]
            if now - s["last_access"] > SESSION_IDLE_SECS:
                _kill_pid(s["pid"])
                shutil.rmtree(s["dir"], ignore_errors=True)
                del _sessions[sid]


@app.post("/api/stream/start")
def api_stream_start():
    data = request.get_json(silent=True) or {}
    nid = int(data.get("nvr_id") or 0)
    channel = int(data.get("channel") or 1)
    mode = data.get("mode", "live")
    subtype = int(data.get("subtype", 0))
    start = _parse_dt(data.get("start", "")) if mode == "playback" else ""
    end = _parse_dt(data.get("end", "")) if mode == "playback" else ""

    with _db() as conn:
        row = conn.execute("SELECT * FROM nvrs WHERE id=?", (nid,)).fetchone()
        if not row:
            return jsonify({"error": "nvr not found"}), 404
        nvr = dict(row)
        # Merge per-channel cam direct settings (Hik proxy). Khi có
        # camera_host, _rtsp_url sẽ stream TRỰC TIẾP từ IP cam, bypass NVR.
        ch_row = conn.execute(
            "SELECT camera_host, camera_user, camera_password "
            "FROM channels WHERE nvr_id=? AND channel=?",
            (nid, channel)).fetchone()
    if ch_row and ch_row["camera_host"]:
        nvr["camera_host"] = ch_row["camera_host"]
        nvr["camera_user"] = ch_row["camera_user"] or ""
        nvr["camera_password"] = ch_row["camera_password"] or ""

    _cleanup_old_sessions()
    sid = uuid.uuid4().hex[:12]
    sdir = STREAM_DIR / sid
    sdir.mkdir(parents=True, exist_ok=True)

    rtsp = _rtsp_url(nvr, channel, mode, start, end, subtype)
    log = sdir / "ffmpeg.log"
    # analyzeduration + probesize PHẢI đứng trước -i, nếu không Hik h264
    # trả "non-existing PPS 0" và "dimensions not set" (SPS/PPS chưa đến
    # khi ffmpeg bắt đầu ghi HLS header). 10s/10MB buffer đủ cho mọi NVR.
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-analyzeduration", "10000000",
        "-probesize", "10000000",
        "-fflags", "+genpts+discardcorrupt",
        "-i", rtsp,
        "-c:v", "copy",
        "-an",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+append_list+independent_segments",
        "-hls_segment_type", "fmp4",  # H.265/HEVC compat
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", str(sdir / "seg_%04d.m4s"),
        str(sdir / "index.m3u8"),
    ]
    with open(log, "ab") as f:
        import rec_shared as _rs
        p = _rs.popen_detached(args, stdout=f, stderr=f)

    with _sessions_lock:
        _sessions[sid] = {
            "pid": p.pid, "dir": sdir, "started": time.time(),
            "last_access": time.time(), "nvr_id": nid, "mode": mode,
            "channel": channel, "rtsp": rtsp,
        }

    # Wait for manifest to appear (up to ~20s — analyzeduration=10s + encode).
    manifest = sdir / "index.m3u8"
    for _ in range(100):
        if manifest.exists() and manifest.stat().st_size > 50:
            break
        time.sleep(0.2)
        if p.poll() is not None:
            try:
                err = log.read_text()[-400:]
            except Exception:
                err = ""
            return jsonify({"error": "ffmpeg exited", "log": err}), 500

    return jsonify({
        "session_id": sid,
        "manifest": f"/stream/{sid}/index.m3u8",
        "mode": mode, "channel": channel,
    })


@app.post("/api/stream/stop")
def api_stream_stop():
    sid = (request.get_json(silent=True) or {}).get("session_id")
    with _sessions_lock:
        s = _sessions.pop(sid, None)
    if not s:
        return jsonify({"ok": False})
    _kill_pid(s["pid"])
    shutil.rmtree(s["dir"], ignore_errors=True)
    return jsonify({"ok": True})


@app.get("/stream/<sid>/<path:filename>")
def serve_stream(sid, filename):
    if not re.match(r"^[a-f0-9]{12}$", sid):
        abort(400)
    with _sessions_lock:
        s = _sessions.get(sid)
        if s:
            s["last_access"] = time.time()
    sdir = STREAM_DIR / sid
    if not sdir.exists():
        abort(404)
    resp = send_from_directory(sdir, filename)
    if filename.endswith(".m3u8"):
        resp.headers["Cache-Control"] = "no-cache, no-store"
    return resp


# ---------------- Clip ----------------
# Fonts for drawtext (first existing is used)
WM_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",  # macOS dev
]
WM_POS = {
    # ffmpeg x,y expressions (w,h = main video, tw,th = text width/height)
    "bl": ("24",       "h-th-24"),   # bottom-left
    "br": ("w-tw-24",  "h-th-24"),   # bottom-right
    "tl": ("24",       "24"),        # top-left
    "tr": ("w-tw-24",  "24"),        # top-right
}


def _wm_font() -> str:
    for p in WM_FONTS:
        if os.path.exists(p):
            return p
    return ""


def _render_wm_text(tpl: str, nvr_name: str, channel: int) -> str:
    now = datetime.now()
    return (tpl
            .replace("{date}",     now.strftime("%Y-%m-%d"))
            .replace("{time}",     now.strftime("%H:%M:%S"))
            .replace("{datetime}", now.strftime("%Y-%m-%d %H:%M:%S"))
            .replace("{ch}",       str(channel))
            .replace("{nvr}",      nvr_name))


def _dahua_fastload(nvr: dict, channel: int, start_dt: str, end_dt: str,
                    out_path: Path, log_path: Path, subtype: int = 0,
                    timeout: int = 600) -> bool:
    """Download a .dav segment directly via Dahua loadfile.cgi (much faster
    than RTSP playback — NVR pushes at disk speed, ~10-80 MB/s).
    Returns True on success. On any failure, caller should fall back to RTSP.
    """
    url = f"http://{nvr['host']}/cgi-bin/loadfile.cgi"
    params = {
        "action": "startLoad",
        "channel": channel,
        "startTime": start_dt,
        "endTime": end_dt,
        "subtype": subtype,
    }
    try:
        with requests.get(url, params=params,
                          auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                          stream=True, timeout=timeout) as r:
            if r.status_code != 200:
                with open(log_path, "ab") as lf:
                    lf.write(f"[fastload] HTTP {r.status_code}\n".encode())
                return False
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)
        # NVR trả body rỗng khi không có bản ghi trong range
        if out_path.stat().st_size < 1024:
            with open(log_path, "ab") as lf:
                lf.write(b"[fastload] empty response (no recording in range)\n")
            try: out_path.unlink(missing_ok=True)
            except Exception: pass
            return False
        return True
    except Exception as e:
        with open(log_path, "ab") as lf:
            lf.write(f"[fastload] {type(e).__name__}: {e}\n".encode())
        return False


# Giới hạn số clip encode song song — tránh làm sập Pi khi >3 user cùng cắt.
# Số luồng = 2 cho mọi mô hình Pi (an toàn), có thể tăng nếu Pi 5/8GB nhàn.
CLIP_ENCODE_LIMIT = int(os.environ.get("PT_CLIP_CONCURRENCY", "2"))
_clip_sema = threading.Semaphore(CLIP_ENCODE_LIMIT)


def _build_encoder_args(encoder: str, has_watermark: bool) -> tuple[list[str], str]:
    """Return (ffmpeg_args_for_video_encoder, effective_encoder_name).
    `copy` tự động downgrade về libx264 khi có watermark (vì copy không bake được).
    """
    enc = (encoder or "libx264").lower()
    if has_watermark and enc == "copy":
        enc = "libx264"  # không thể bake watermark khi copy
    if enc == "copy":
        return (["-c:v", "copy"], "copy")
    if enc == "h264_v4l2m2m":
        # HW encode trên Pi: không hỗ trợ -crf, dùng -b:v
        return (["-c:v", "h264_v4l2m2m", "-num_capture_buffers", "32",
                 "-b:v", "4M"], "h264_v4l2m2m")
    # Default: libx264 ultrafast
    return (["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"],
            "libx264")


def _run_clip(clip_id: str, rtsp: str, duration: int, out_path: Path,
              watermark: str = "", wm_pos: str = "bl",
              wm_scroll: int = 0, wm_size: int = 40,
              fast_input: str = "", encoder: str = "libx264"):
    """Run ffmpeg to produce the clip.
    If `fast_input` is set, use it as the -i source (local .dav from fastload)
    with no RTSP transport flag. Otherwise read from the RTSP URL.
    Semaphore limits concurrent encodes to avoid overloading Pi.
    """
    with _clip_sema:
        _run_clip_inner(clip_id, rtsp, duration, out_path,
                        watermark, wm_pos, wm_scroll, wm_size,
                        fast_input, encoder)


def _run_clip_inner(clip_id, rtsp, duration, out_path,
                    watermark, wm_pos, wm_scroll, wm_size,
                    fast_input, encoder):
    log = out_path.with_suffix(".log")
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if fast_input:
        args += ["-i", fast_input]
    else:
        args += ["-rtsp_transport", "tcp", "-i", rtsp]
    if duration > 0 and not fast_input:
        # Chỉ dùng -t cho RTSP live. File .dav đã có đúng độ dài rồi.
        args += ["-t", str(duration)]

    font = _wm_font() if watermark else ""
    enc_args, enc_name = _build_encoder_args(encoder, bool(watermark and font))
    if watermark and font:
        # Burn text via textfile (avoids drawtext escape hell)
        wm_txt_path = out_path.with_suffix(".wm.txt")
        wm_txt_path.write_text(watermark, encoding="utf-8")

        # Font size: h / wm_size (clamped so người ta không nhập bậy)
        size_div = max(12, min(80, int(wm_size or 40)))

        if wm_scroll:
            # Marquee: trượt phải → trái, tốc độ ~120 px/giây
            # Y chọn theo pos (trên vs dưới), bỏ qua trái/phải
            if wm_pos in ("tl", "tr"):
                y_expr = "24"
            else:
                y_expr = "h-th-24"
            x_expr = "w-mod(t*120\\,w+tw)"
            drawtext = (
                f"drawtext=fontfile={font}"
                f":textfile={wm_txt_path}"
                f":reload=0"
                f":fontcolor=white"
                f":fontsize=h/{size_div}"
                f":box=1:boxcolor=black@0.55:boxborderw=10"
                f":x={x_expr}:y={y_expr}"
            )
        else:
            x_expr, y_expr = WM_POS.get(wm_pos, WM_POS["bl"])
            drawtext = (
                f"drawtext=fontfile={font}"
                f":textfile={wm_txt_path}"
                f":reload=0"
                f":fontcolor=white"
                f":fontsize=h/{size_div}"
                f":box=1:boxcolor=black@0.55:boxborderw=10"
                f":x={x_expr}:y={y_expr}"
            )
        # h264_v4l2m2m cần yuv420p; prepend format filter cho an toàn
        vf_chain = drawtext
        if enc_name == "h264_v4l2m2m":
            vf_chain = "format=yuv420p," + drawtext
        args += [
            "-vf", vf_chain,
            *enc_args,
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-y", str(out_path),
        ]
    else:
        # Không có watermark → tôn trọng encoder setting (copy mặc định tốt nhất)
        no_wm_args, _ = _build_encoder_args(encoder or "copy", False)
        args += [
            *no_wm_args,
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-y", str(out_path),
        ]

    with open(log, "ab") as f:
        if watermark and not font:
            f.write(b"[warn] watermark requested but no font installed; "
                    b"falling back to copy\n")
        f.write(f"[encoder] {enc_name}\n".encode())
        p = subprocess.Popen(args, stdout=f, stderr=f)
    with _clips_lock:
        _clips[clip_id]["pid"] = p.pid
    rc = p.wait()

    # Auto-fallback: HW encoder fail (common khi firmware chưa setup) → thử lại libx264.
    # Chỉ cần rc != 0 là retry; HW thường để lại mp4 header rỗng, không dùng được.
    if rc != 0 and enc_name == "h264_v4l2m2m":
        try:
            out_path.unlink(missing_ok=True)  # xoá mp4 header rỗng từ HW fail
        except Exception:
            pass
        with open(log, "ab") as f:
            f.write(b"[fallback] h264_v4l2m2m failed, retrying with libx264\n")
        # Rebuild args với libx264
        libx264_args = [a for a in args]
        # Thay segment encoder trong args (giữa -vf drawtext và -c:a aac)
        # Dễ hơn: build lại từ đầu
        fallback_args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
        if fast_input:
            fallback_args += ["-i", fast_input]
        else:
            fallback_args += ["-rtsp_transport", "tcp", "-i", rtsp]
        if duration > 0 and not fast_input:
            fallback_args += ["-t", str(duration)]
        if watermark and font:
            fallback_args += ["-vf", drawtext,
                              "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"]
        else:
            fallback_args += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"]
        fallback_args += ["-c:a", "aac", "-b:a", "128k",
                          "-movflags", "+faststart", "-y", str(out_path)]
        with open(log, "ab") as f:
            p2 = subprocess.Popen(fallback_args, stdout=f, stderr=f)
        with _clips_lock:
            _clips[clip_id]["pid"] = p2.pid
            _clips[clip_id]["encoder_used"] = "libx264 (hw fallback)"
        rc = p2.wait()
    else:
        with _clips_lock:
            _clips[clip_id]["encoder_used"] = enc_name

    # Clean up temp textfile + fastload .dav
    try:
        if watermark and font:
            out_path.with_suffix(".wm.txt").unlink(missing_ok=True)
    except Exception:
        pass
    try:
        if fast_input:
            Path(fast_input).unlink(missing_ok=True)
    except Exception:
        pass
    with _clips_lock:
        c = _clips[clip_id]
        if rc == 0 and out_path.exists():
            c["status"] = "done"
            c["size"] = out_path.stat().st_size
        else:
            c["status"] = "error"
            try:
                c["err"] = log.read_text()[-600:]
            except Exception:
                c["err"] = f"rc={rc}"


@app.post("/api/clip")
def api_clip():
    data = request.get_json(silent=True) or {}
    nid = int(data.get("nvr_id") or 0)
    channel = int(data.get("channel") or 1)
    mode = data.get("mode", "playback")
    subtype = int(data.get("subtype", 0))
    duration = int(data.get("duration", 0))  # for live mode
    start_raw = data.get("start", "")
    end_raw = data.get("end", "")
    start = _parse_dt(start_raw) if mode == "playback" else ""
    end = _parse_dt(end_raw) if mode == "playback" else ""

    with _db() as conn:
        row = conn.execute("SELECT * FROM nvrs WHERE id=?", (nid,)).fetchone()
        if not row:
            return jsonify({"error": "nvr not found"}), 404
        nvr = dict(row)
        # Merge per-channel cam direct settings (Hik proxy)
        ch_row = conn.execute(
            "SELECT camera_host, camera_user, camera_password "
            "FROM channels WHERE nvr_id=? AND channel=?",
            (nid, channel)).fetchone()
    if ch_row and ch_row["camera_host"] and mode == "live":
        # Live: dùng direct cam. Playback: NVR giữ data → vẫn qua NVR.
        nvr["camera_host"] = ch_row["camera_host"]
        nvr["camera_user"] = ch_row["camera_user"] or ""
        nvr["camera_password"] = ch_row["camera_password"] or ""
    rtsp = _rtsp_url(nvr, channel, mode, start, end, subtype)

    # Per-clip override, else NVR default, else none
    wm_override = data.get("watermark", None)
    wm_tpl = wm_override if wm_override is not None else (nvr.get("clip_watermark") or "")
    wm_pos = data.get("wm_pos") or nvr.get("clip_wm_pos") or "bl"
    wm_scroll = int(data.get("wm_scroll", nvr.get("clip_wm_scroll") or 0))
    wm_size = int(data.get("wm_size", nvr.get("clip_wm_size") or 40))
    watermark = _render_wm_text(wm_tpl, nvr["name"], channel) if wm_tpl else ""

    clip_id = uuid.uuid4().hex[:12]
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    tag = (start_raw or "live").replace(":", "").replace(" ", "_")[:19]
    fname = f"{nvr['name']}_ch{channel}_{tag}_{ts}_{clip_id}.mp4"
    fname = re.sub(r"[^A-Za-z0-9._-]+", "_", fname)
    out_path = CLIPS_DIR / fname

    # Clip source: 'fastload' = tải trực tiếp .dav từ NVR (nhanh ~2×),
    # chỉ áp dụng mode=playback. Nếu fail → fallback RTSP.
    src = (data.get("source") or nvr.get("clip_source") or "rtsp").lower()
    # Fastload chỉ áp dụng cho Dahua (loadfile.cgi). Hikvision không có
    # equivalent → force RTSP playback path.
    use_fastload = (src == "fastload" and mode == "playback"
                    and start_raw and end_raw
                    and (nvr.get("vendor", "dahua") == "dahua"))
    # Encoder: libx264 (default) / h264_v4l2m2m (HW) / copy (no re-encode)
    encoder = (data.get("encoder") or nvr.get("clip_encoder") or "libx264").lower()

    with _clips_lock:
        _clips[clip_id] = {
            "id": clip_id, "path": out_path, "name": fname,
            "created": time.time(), "status": "running", "pid": None,
            "err": "", "size": 0, "nvr": nvr["name"], "channel": channel,
            "mode": mode, "watermark": watermark,
            "source": "fastload" if use_fastload else "rtsp",
            "encoder": encoder,
        }

    def _work():
        fast_dav = ""
        if use_fastload:
            dav_tmp = out_path.with_suffix(".src.dav")
            log_path = out_path.with_suffix(".log")
            # Dùng raw time strings (NVR expect "YYYY-MM-DD HH:MM:SS")
            ok = _dahua_fastload(nvr, channel,
                                 start_raw.replace("T", " "),
                                 end_raw.replace("T", " "),
                                 dav_tmp, log_path, subtype=subtype)
            if ok:
                fast_dav = str(dav_tmp)
                with _clips_lock:
                    _clips[clip_id]["dav_size"] = dav_tmp.stat().st_size
            else:
                # fallback: mark + continue with RTSP
                with _clips_lock:
                    _clips[clip_id]["source"] = "rtsp (fastload fallback)"
        _run_clip(clip_id, rtsp, duration, out_path,
                  watermark, wm_pos, wm_scroll, wm_size,
                  fast_input=fast_dav, encoder=encoder)

    threading.Thread(target=_work, daemon=True).start()
    return jsonify({"clip_id": clip_id, "name": fname,
                    "watermark": watermark,
                    "source": "fastload" if use_fastload else "rtsp",
                    "encoder": encoder})


@app.get("/api/clips")
def api_clips_list():
    with _clips_lock:
        out = []
        for c in sorted(_clips.values(), key=lambda x: -x["created"]):
            out.append({
                "id": c["id"], "name": c["name"],
                "status": c["status"], "err": c["err"],
                "size": c["size"], "created": c["created"],
                "nvr": c["nvr"], "channel": c["channel"], "mode": c["mode"],
                "download_url": f"/clips/{c['id']}" if c["status"] == "done" else "",
            })
    # Also include orphan files on disk (from past runs)
    seen_names = {c["name"] for c in out}
    for p in sorted(CLIPS_DIR.glob("*.mp4"), key=lambda x: -x.stat().st_mtime):
        if p.name in seen_names:
            continue
        out.append({
            "id": "", "name": p.name, "status": "done",
            "err": "", "size": p.stat().st_size,
            "created": p.stat().st_mtime,
            "nvr": "", "channel": 0, "mode": "",
            "download_url": f"/clips/_file/{p.name}",
        })
    return jsonify(out)


@app.get("/clips/<clip_id>")
def download_clip(clip_id):
    with _clips_lock:
        c = _clips.get(clip_id)
    if not c or c["status"] != "done":
        abort(404)
    return send_file(c["path"], as_attachment=True, download_name=c["name"])


@app.get("/clips/_file/<path:name>")
def download_clip_file(name):
    if "/" in name or ".." in name:
        abort(400)
    p = CLIPS_DIR / name
    if not p.exists():
        abort(404)
    return send_file(p, as_attachment=True, download_name=name)


@app.delete("/api/clips/<clip_id>")
def api_clip_delete(clip_id):
    with _clips_lock:
        c = _clips.pop(clip_id, None)
    if c:
        if c.get("pid") and c["status"] == "running":
            _kill_pid(c["pid"])
        try:
            c["path"].unlink()
        except Exception:
            pass
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 404


# ---------------- Channels (admin) ----------------
def _get_nvr(nid: int) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM nvrs WHERE id=?", (nid,)).fetchone()
    return dict(row) if row else None


def _get_channel(cid: int) -> dict | None:
    with _db() as conn:
        row = conn.execute("""
            SELECT c.*, n.name AS nvr_name, n.host AS nvr_host,
                   n.rtsp_port, n.user, n.password
            FROM channels c JOIN nvrs n ON c.nvr_id = n.id
            WHERE c.id=?
        """, (cid,)).fetchone()
    return dict(row) if row else None


@app.get("/api/settings")
def api_get_settings():
    import rec_shared as _rs
    s = _rs.get_settings()
    return jsonify({"settings": s, "defaults": _rs.DEFAULT_SETTINGS})


@app.put("/api/settings")
def api_put_settings():
    import rec_shared as _rs
    data = request.get_json(silent=True) or {}
    for k, v in data.items():
        if k not in _rs.DEFAULT_SETTINGS:
            continue
        _rs.set_setting(k, str(v))
    return jsonify({"ok": True})


@app.get("/api/channels")
def api_list_channels():
    with _db() as conn:
        rows = conn.execute("""
            SELECT c.*, n.name AS nvr_name, n.host AS nvr_host
            FROM channels c JOIN nvrs n ON c.nvr_id = n.id
            ORDER BY c.name
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/channels")
def api_add_channel():
    data = request.get_json(silent=True) or {}
    if not data.get("name") or not data.get("nvr_id") or data.get("channel") is None:
        return jsonify({"error": "name, nvr_id, channel required"}), 400
    try:
        with _db() as conn:
            cur = conn.execute(
                "INSERT INTO channels (nvr_id,channel,name,description,subtype,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (int(data["nvr_id"]), int(data["channel"]), data["name"],
                 data.get("description", ""), int(data.get("subtype", 0)), time.time()),
            )
            conn.commit()
        return jsonify({"id": cur.lastrowid})
    except sqlite3.IntegrityError as e:
        return jsonify({"error": f"duplicate channel for this NVR: {e}"}), 400


@app.patch("/api/channels/<int:cid>")
def api_update_channel(cid):
    data = request.get_json(silent=True) or {}
    sets, vals = [], []
    for k in ("name", "description", "subtype", "channel"):
        if k in data:
            sets.append(f"{k}=?")
            vals.append(data[k])
    if not sets:
        return jsonify({"error": "nothing to update"}), 400
    vals.append(cid)
    with _db() as conn:
        conn.execute(f"UPDATE channels SET {','.join(sets)} WHERE id=?", vals)
        conn.commit()
    return jsonify({"ok": True})


@app.delete("/api/channels/<int:cid>")
def api_delete_channel(cid):
    with _db() as conn:
        conn.execute("DELETE FROM channels WHERE id=?", (cid,))
        conn.commit()
    return jsonify({"ok": True})


def _dahua_fetch_channel_titles(host: str, user: str, password: str,
                                timeout: int = 10) -> list[dict]:
    """Gọi Dahua ChannelTitle API → trả về list [{channel, title}].
    channel là 1-based (hợp với format Pickletour đang dùng)."""
    url = f"http://{host}/cgi-bin/configManager.cgi"
    r = requests.get(url, params={"action": "getConfig", "name": "ChannelTitle"},
                     auth=HTTPDigestAuth(user, password), timeout=timeout)
    r.raise_for_status()
    out = {}
    for line in r.text.splitlines():
        m = re.match(r"table\.ChannelTitle\[(\d+)\]\.Name=(.*)", line.strip())
        if m:
            idx = int(m.group(1))
            out[idx] = m.group(2).strip()
    # Return as 1-based channels sorted
    return [{"channel": i + 1, "title": out[i]} for i in sorted(out.keys())]


@app.post("/api/nvrs/scan-channels")
def api_scan_channels_adhoc():
    """Scan channel titles cho NVR CHƯA được save (nhập host/user/pass raw).
    Dùng khi tạo mới NVR, trước khi lưu. Vendor-aware."""
    import vendors as _v
    data = request.get_json(silent=True) or {}
    host = (data.get("host") or "").strip()
    user = (data.get("user") or "").strip()
    pw = data.get("password") or ""
    vendor = (data.get("vendor") or "dahua").lower()
    if not host or not user or not pw:
        return jsonify({"error": "host, user, password required"}), 400
    nvr = {"host": host, "user": user, "password": pw, "vendor": vendor,
           "channels": 16, "rtsp_port": 554}
    try:
        titles = _v.list_channels(nvr, timeout=10)
        return jsonify({"ok": True, "channels": titles, "vendor": vendor})
    except Exception as e:
        return jsonify({"error": f"NVR không trả lời: {e}"}), 502


@app.get("/api/nvrs/<int:nid>/channels-remote")
def api_scan_channels(nid):
    """Scan channel titles cho NVR đã save (dùng creds trong DB).
    Merge với bảng channels local để biết sân nào đã add."""
    import vendors as _v
    with _db() as conn:
        row = conn.execute("SELECT * FROM nvrs WHERE id=?", (nid,)).fetchone()
        if not row:
            return jsonify({"error": "nvr not found"}), 404
        nvr = dict(row); nvr.setdefault("vendor", "dahua")
        existing = {r["channel"]: dict(r) for r in
                    conn.execute("SELECT * FROM channels WHERE nvr_id=?",
                                 (nid,)).fetchall()}
    try:
        titles = _v.list_channels(nvr, timeout=10)
    except Exception as e:
        return jsonify({"error": f"NVR không trả lời: {e}"}), 502
    for t in titles:
        local = existing.get(t["channel"])
        t["in_local"] = bool(local)
        t["local_id"] = local["id"] if local else None
        t["local_name"] = local["name"] if local else ""
        t["local_subtype"] = local["subtype"] if local else 0
        # Hik: scan trả cam_host; nếu local đã save khác → dùng local
        t.setdefault("camera_host", "")
        if local:
            if local.get("camera_host"):
                t["camera_host"] = local["camera_host"]
            # Trả về cred đã lưu để UI điền lại (password KHÔNG trả plain,
            # chỉ 1 flag — user để trống = giữ pass cũ, bỏ tick thì mới xoá).
            t["local_camera_user"] = local.get("camera_user", "") or ""
            t["local_has_password"] = bool(local.get("camera_password"))
    return jsonify({"ok": True, "nvr": nvr["name"],
                    "vendor": nvr.get("vendor"), "channels": titles})


# ---------------- Dahua operations: system/reboot/storage/image/record ----------------

def _dahua_cgi_get(nvr: dict, path: str, params: dict | None = None,
                   timeout: int = 10) -> tuple[int, str]:
    """GET /cgi-bin/<path> with Digest auth. Returns (status, text)."""
    url = f"http://{nvr['host']}/cgi-bin/{path}"
    r = requests.get(url, params=params or {},
                     auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                     timeout=timeout, verify=False)
    return r.status_code, r.text


def _parse_dahua_kv(text: str) -> dict:
    """Parse Dahua k=v response (one per line). Returns flat dict."""
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _get_nvr_or_404(nid: int):
    with _db() as conn:
        row = conn.execute("SELECT * FROM nvrs WHERE id=?", (nid,)).fetchone()
    if not row:
        return None, (jsonify({"error": "nvr not found"}), 404)
    return dict(row), None


@app.get("/api/nvrs/<int:nid>/system-info")
def api_nvr_system_info(nid):
    """Serial, firmware, device type, giờ NVR + chênh lệch so với Pi."""
    nvr, err = _get_nvr_or_404(nid)
    if err: return err
    try:
        info = {}
        for action in ("getSystemInfo", "getSerialNo", "getSoftwareVersion",
                       "getDeviceType"):
            s, t = _dahua_cgi_get(nvr, "magicBox.cgi", {"action": action})
            if s == 200:
                info.update(_parse_dahua_kv(t))
        s, t = _dahua_cgi_get(nvr, "global.cgi", {"action": "getCurrentTime"})
        nvr_time_str = ""
        drift_sec = None
        if s == 200:
            kv = _parse_dahua_kv(t)
            nvr_time_str = kv.get("result", "")
            try:
                nvr_dt = datetime.strptime(nvr_time_str, "%Y-%m-%d %H:%M:%S")
                drift_sec = int((datetime.now() - nvr_dt).total_seconds())
            except Exception:
                pass
    except Exception as e:
        return jsonify({"error": f"NVR không trả lời: {e}"}), 502
    return jsonify({
        "ok": True,
        "device_type": info.get("type") or info.get("updateSerial", ""),
        "serial": info.get("sn") or info.get("serialNumber", ""),
        "firmware": info.get("version", ""),
        "processor": info.get("processor", ""),
        "nvr_time": nvr_time_str,
        "pi_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "drift_seconds": drift_sec,
    })


@app.post("/api/nvrs/<int:nid>/sync-time")
def api_nvr_sync_time(nid):
    """Đồng bộ giờ NVR về giờ Pi hiện tại (Vietnam)."""
    nvr, err = _get_nvr_or_404(nid)
    if err: return err
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    s, t = _dahua_cgi_get(nvr, "global.cgi",
                          {"action": "setCurrentTime", "time": now})
    return jsonify({"ok": s == 200, "status": s, "set_to": now, "resp": t[:200]})


@app.post("/api/nvrs/<int:nid>/reboot")
def api_nvr_reboot(nid):
    """Reboot NVR từ xa. UI phải confirm 2 lần vì mất 60-120s."""
    nvr, err = _get_nvr_or_404(nid)
    if err: return err
    try:
        s, t = _dahua_cgi_get(nvr, "magicBox.cgi", {"action": "reboot"}, timeout=5)
        return jsonify({"ok": s == 200, "status": s, "resp": t[:200]})
    except Exception as e:
        # Reboot thường ngắt kết nối trước khi trả → coi như OK
        return jsonify({"ok": True, "note": f"NVR đã nhận lệnh (connection dropped: {e})"})


@app.get("/api/nvrs/<int:nid>/storage")
def api_nvr_storage(nid):
    """Thông tin HDD: dung lượng, used%, health."""
    nvr, err = _get_nvr_or_404(nid)
    if err: return err
    try:
        s, t = _dahua_cgi_get(nvr, "storageDevice.cgi",
                              {"action": "getDeviceAllInfo"}, timeout=15)
        if s != 200:
            return jsonify({"error": f"HTTP {s}", "resp": t[:200]}), 502
    except Exception as e:
        return jsonify({"error": f"NVR không trả lời: {e}"}), 502
    # Parse list.info[D].Detail[P].Field=Value → gom theo disk index
    disks = {}
    for k, v in _parse_dahua_kv(t).items():
        m = re.match(r"list\.info\[(\d+)\]\.(Detail\[(\d+)\]\.)?(\w+)", k)
        if not m: continue
        di = int(m.group(1))
        d = disks.setdefault(di, {"name": "", "state": "", "healthy": True,
                                  "partitions": {}})
        if m.group(2):  # Detail[P].Field
            pi = int(m.group(3))
            field = m.group(4)
            p = d["partitions"].setdefault(pi, {})
            if field == "TotalBytes": p["total"] = int(float(v))
            elif field == "UsedBytes": p["used"] = int(float(v))
            elif field == "Path": p["path"] = v
            elif field == "Type": p["type"] = v
            elif field == "IsError": p["error"] = v.lower() == "true"
        else:
            if m.group(4) == "Name": d["name"] = v
            elif m.group(4) == "State": d["state"] = v
            elif m.group(4) == "HealthDataFlag": d["healthy"] = v == "0"
    out = []
    for di in sorted(disks):
        d = disks[di]
        parts = [d["partitions"][p] for p in sorted(d["partitions"])]
        total = sum(p.get("total", 0) for p in parts)
        used = sum(p.get("used", 0) for p in parts)
        out.append({
            "index": di, "name": d["name"], "state": d["state"],
            "healthy": d["healthy"],
            "total_gb": round(total / 1e9, 1),
            "used_gb": round(used / 1e9, 1),
            "used_pct": round((used / total * 100), 1) if total else 0,
            "partitions": len(parts),
        })
    return jsonify({"ok": True, "disks": out})


@app.get("/api/nvrs/<int:nid>/image")
def api_nvr_image_get(nid):
    """Image settings cho 1 channel: brightness/contrast/saturation/hue/mode."""
    nvr, err = _get_nvr_or_404(nid)
    if err: return err
    channel = int(request.args.get("channel", 1)) - 1  # 0-based cho CGI
    try:
        _, t = _dahua_cgi_get(nvr, "configManager.cgi",
                              {"action": "getConfig", "name": "VideoColor"})
        _, tm = _dahua_cgi_get(nvr, "configManager.cgi",
                               {"action": "getConfig", "name": "VideoInMode"})
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    kv = _parse_dahua_kv(t)
    prefix = f"table.VideoColor[{channel}][0]."  # slot 0 = default time section
    out = {
        "channel": channel + 1,
        "brightness": int(kv.get(prefix + "Brightness", 50) or 50),
        "contrast":   int(kv.get(prefix + "Contrast", 50) or 50),
        "saturation": int(kv.get(prefix + "Saturation", 50) or 50),
        "hue":        int(kv.get(prefix + "Hue", 50) or 50),
        "gamma":      int(kv.get(prefix + "Gamma", 50) or 50),
        "style":      kv.get(prefix + "Style", "Standard"),
    }
    kvm = _parse_dahua_kv(tm)
    out["day_night_mode"] = int(kvm.get(f"table.VideoInMode[{channel}].Mode", 0) or 0)
    return jsonify(out)


@app.put("/api/nvrs/<int:nid>/image")
def api_nvr_image_set(nid):
    """Set image settings cho 1 channel. Body: {brightness, contrast, saturation,
    hue, gamma, day_night_mode}. Chỉ gửi field có trong body."""
    nvr, err = _get_nvr_or_404(nid)
    if err: return err
    data = request.get_json(silent=True) or {}
    channel = int(data.get("channel", 1)) - 1
    params = []
    for src, dst in [("brightness", "Brightness"), ("contrast", "Contrast"),
                     ("saturation", "Saturation"), ("hue", "Hue"),
                     ("gamma", "Gamma")]:
        if src in data:
            val = max(0, min(100, int(data[src])))
            # Set cho tất cả 4 time section (0..3)
            for slot in range(4):
                params.append(f"VideoColor[{channel}][{slot}].{dst}={val}")
    if "day_night_mode" in data:
        m = max(0, min(2, int(data["day_night_mode"])))
        params.append(f"VideoInMode[{channel}].Mode={m}")
    if not params:
        return jsonify({"error": "nothing to update"}), 400
    ok, status, body = _dahua_setconfig(nvr["host"], nvr["user"],
                                        nvr["password"], params)
    return jsonify({"ok": ok, "status": status, "resp": body, "applied": len(params)})


@app.get("/api/nvrs/<int:nid>/record")
def api_nvr_record_get(nid):
    """Record mode cho tất cả channel. Mode: 0=Auto, 1=Manual (ghi liên tục), 2=Off."""
    nvr, err = _get_nvr_or_404(nid)
    if err: return err
    try:
        _, t = _dahua_cgi_get(nvr, "configManager.cgi",
                              {"action": "getConfig", "name": "RecordMode"})
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    kv = _parse_dahua_kv(t)
    modes = {}
    for k, v in kv.items():
        # Phải anchor $ để không match "ModeExtra1" / "ModeExtra2"
        m = re.match(r"table\.RecordMode\[(\d+)\]\.Mode$", k)
        if m:
            try: modes[int(m.group(1)) + 1] = int(v)
            except Exception: pass
    return jsonify({"ok": True, "modes": modes})


@app.put("/api/nvrs/<int:nid>/record")
def api_nvr_record_set(nid):
    """Set record mode. Body: {channel, mode} (0/1/2) hoặc {all: 0/1/2}."""
    nvr, err = _get_nvr_or_404(nid)
    if err: return err
    data = request.get_json(silent=True) or {}
    params = []
    if "all" in data:
        m = max(0, min(2, int(data["all"])))
        for i in range(int(nvr["channels"])):
            params.append(f"RecordMode[{i}].Mode={m}")
    else:
        ch = int(data["channel"]) - 1
        m = max(0, min(2, int(data["mode"])))
        params.append(f"RecordMode[{ch}].Mode={m}")
    ok, status, body = _dahua_setconfig(nvr["host"], nvr["user"],
                                        nvr["password"], params)
    return jsonify({"ok": ok, "status": status, "resp": body, "applied": len(params)})


# ---------------- ONVIF support (channel discovery) ----------------

def _onvif_soap(url: str, action: str, body_xml: str,
                user: str, password: str, timeout: int = 10) -> str:
    """Gửi SOAP POST với WS-Security UsernameToken. Trả về response body text."""
    import hashlib, base64, secrets
    from datetime import datetime, timezone
    nonce = secrets.token_bytes(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pwd_digest = base64.b64encode(hashlib.sha1(
        nonce + created.encode() + password.encode()).digest()).decode()
    nonce_b64 = base64.b64encode(nonce).decode()
    envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
            xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
<s:Header>
  <wsse:Security s:mustUnderstand="1">
    <wsse:UsernameToken>
      <wsse:Username>{user}</wsse:Username>
      <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{pwd_digest}</wsse:Password>
      <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</wsse:Nonce>
      <wsu:Created>{created}</wsu:Created>
    </wsse:UsernameToken>
  </wsse:Security>
</s:Header>
<s:Body>{body_xml}</s:Body>
</s:Envelope>"""
    headers = {
        "Content-Type": f"application/soap+xml; charset=utf-8; action=\"{action}\"",
    }
    r = requests.post(url, data=envelope.encode(), headers=headers,
                      timeout=timeout, verify=False)
    return r.text


def _onvif_scan(host: str, user: str, password: str, port: int = 80) -> list[dict]:
    """GetProfiles via ONVIF → list of {token, name, encoding, w, h}.
    Then GetStreamUri cho mỗi profile → RTSP URL."""
    import xml.etree.ElementTree as ET
    base = f"http://{host}:{port}"
    media_url = f"{base}/onvif/media_service"
    # GetProfiles
    body = '<trt:GetProfiles xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/>'
    resp = _onvif_soap(media_url,
        "http://www.onvif.org/ver10/media/wsdl/GetProfiles", body, user, password)
    ns = {"trt": "http://www.onvif.org/ver10/media/wsdl",
          "tt":  "http://www.onvif.org/ver10/schema"}
    root = ET.fromstring(resp)
    profiles = []
    for p in root.iter("{http://www.onvif.org/ver10/media/wsdl}Profiles"):
        token = p.attrib.get("token", "")
        name = p.findtext("tt:Name", default="", namespaces=ns)
        enc = p.find(".//tt:Encoding", ns)
        w = p.find(".//tt:Resolution/tt:Width", ns)
        h = p.find(".//tt:Resolution/tt:Height", ns)
        profiles.append({
            "token": token, "name": name,
            "encoding": enc.text if enc is not None else "",
            "width": int(w.text) if w is not None else 0,
            "height": int(h.text) if h is not None else 0,
            "stream_url": "",
        })
    # Fetch stream URI per profile
    for p in profiles:
        body = f"""<trt:GetStreamUri xmlns:trt="http://www.onvif.org/ver10/media/wsdl">
          <trt:StreamSetup>
            <tt:Stream xmlns:tt="http://www.onvif.org/ver10/schema">RTP-Unicast</tt:Stream>
            <tt:Transport xmlns:tt="http://www.onvif.org/ver10/schema"><tt:Protocol>RTSP</tt:Protocol></tt:Transport>
          </trt:StreamSetup>
          <trt:ProfileToken>{p['token']}</trt:ProfileToken>
        </trt:GetStreamUri>"""
        try:
            resp = _onvif_soap(media_url,
                "http://www.onvif.org/ver10/media/wsdl/GetStreamUri",
                body, user, password)
            r2 = ET.fromstring(resp)
            uri = r2.find(".//tt:Uri", ns)
            if uri is not None and uri.text:
                p["stream_url"] = uri.text
        except Exception as e:
            p["stream_url"] = f"(err: {e})"
    return profiles


@app.post("/api/nvrs/scan-onvif")
def api_scan_onvif():
    """Scan channels qua ONVIF (fallback khi NVR không phải Dahua).
    Body: {host, user, password, port?}"""
    data = request.get_json(silent=True) or {}
    host = (data.get("host") or "").strip()
    user = (data.get("user") or "").strip()
    pw = data.get("password") or ""
    port = int(data.get("port") or 80)
    if not host or not user or not pw:
        return jsonify({"error": "host, user, password required"}), 400
    try:
        profiles = _onvif_scan(host, user, pw, port)
        return jsonify({"ok": True, "profiles": profiles})
    except Exception as e:
        return jsonify({"error": f"ONVIF scan fail: {type(e).__name__}: {e}"}), 502


@app.post("/api/nvrs/<int:nid>/channels-bulk")
def api_channels_bulk(nid):
    """Thêm/sửa nhiều channel cùng lúc. Body: {channels: [{channel, name, subtype}]}
    - Nếu đã có channel # trong NVR → UPDATE name/subtype
    - Nếu chưa → INSERT
    - Channel không có trong request → GIỮ NGUYÊN (không xoá)"""
    data = request.get_json(silent=True) or {}
    chans = data.get("channels") or []
    if not isinstance(chans, list) or not chans:
        return jsonify({"error": "channels[] required"}), 400
    added, updated, errors = [], [], []
    with _db() as conn:
        for c in chans:
            try:
                ch_num = int(c.get("channel"))
                name = (c.get("name") or "").strip()
                subtype = int(c.get("subtype") or 0)
                desc = c.get("description") or ""
                cam_host = (c.get("camera_host") or "").strip()
                cam_user = (c.get("camera_user") or "").strip()
                cam_pw = c.get("camera_password") or ""
                if not name:
                    errors.append({"channel": ch_num, "error": "name empty"})
                    continue
                existing = conn.execute(
                    "SELECT id, camera_password FROM channels WHERE nvr_id=? AND channel=?",
                    (nid, ch_num)).fetchone()
                if existing:
                    # Password trống = giữ pass cũ (giống NVR password UX)
                    final_pw = cam_pw if cam_pw else (existing["camera_password"] or "")
                    conn.execute(
                        "UPDATE channels SET name=?, subtype=?, description=?, "
                        "camera_host=?, camera_user=?, camera_password=? WHERE id=?",
                        (name, subtype, desc, cam_host, cam_user, final_pw, existing["id"]))
                    updated.append(ch_num)
                else:
                    conn.execute(
                        "INSERT INTO channels (nvr_id,channel,name,description,"
                        "subtype,camera_host,camera_user,camera_password,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (nid, ch_num, name, desc, subtype, cam_host, cam_user, cam_pw,
                         time.time()))
                    added.append(ch_num)
            except Exception as e:
                errors.append({"channel": c.get("channel"), "error": str(e)})
        conn.commit()
    return jsonify({"ok": True, "added": added, "updated": updated,
                    "errors": errors})


# ---------------- bootstrap ----------------
def _housekeeper():
    while True:
        time.sleep(60)
        try:
            _cleanup_old_sessions()
            # auto-delete clips older than 3h (env PT_CLIP_TTL_SECS)
            from pathlib import Path as _P
            now = time.time()
            ttl = int(os.environ.get("PT_CLIP_TTL_SECS", "10800"))
            for p in CLIPS_DIR.iterdir() if CLIPS_DIR.exists() else []:
                try:
                    if (now - p.stat().st_mtime) > ttl:
                        p.unlink()
                except Exception:
                    pass
            with _clips_lock:
                for cid in list(_clips.keys()):
                    if _clips[cid].get("status") == "done" and not _clips[cid]["path"].exists():
                        del _clips[cid]
        except Exception:
            pass


threading.Thread(target=_housekeeper, daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("PT_RECORDER_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PT_RECORDER_PORT", "9997")))
    args = ap.parse_args()
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()

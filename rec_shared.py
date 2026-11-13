"""Shared helpers between recorder.py (admin) and clipapp.py (public).

Cross-platform: chạy cả Pi (Linux) lẫn Windows .exe. Dùng IS_WIN để branch
các Unix-specific calls (killpg, start_new_session, fontfile paths)."""
import os
import signal
import sqlite3
import subprocess
import time
import urllib.parse
from pathlib import Path

IS_WIN = os.name == "nt"

# Windows mặc định data dir ở %USERPROFILE%\PiTailscale\data
# Linux/macOS: ./data cạnh file (dev) hoặc PT_DATA_DIR env (prod).
if IS_WIN:
    _DEFAULT_DATA = Path.home() / "PiTailscale" / "data"
else:
    _DEFAULT_DATA = Path(__file__).resolve().parent / "data"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PT_DATA_DIR", str(_DEFAULT_DATA)))
DB_PATH = DATA_DIR / "recorder.db"
STREAM_DIR = DATA_DIR / "streams"
CLIPS_DIR = DATA_DIR / "clips"
THUMBS_DIR = DATA_DIR / "thumbs"

for d in (DATA_DIR, STREAM_DIR, CLIPS_DIR, THUMBS_DIR):
    d.mkdir(parents=True, exist_ok=True)

THUMB_TTL = 60
SESSION_IDLE_SECS = 120
CLIP_TTL_SECS = int(os.environ.get("PT_CLIP_TTL_SECS", "10800"))  # 3 hours default

# Background thumbnail capture ---
# Chụp định kỳ ở nền; HTTP endpoint chỉ phục vụ từ cache, không bao giờ chờ ffmpeg.
THUMB_INTERVAL = int(os.environ.get("PT_THUMB_INTERVAL", "30"))      # giây giữa 2 lần chụp OK
THUMB_PARALLEL = int(os.environ.get("PT_THUMB_PARALLEL", "2"))       # số capture chạy song song
THUMB_CAPTURE_TIMEOUT = int(os.environ.get("PT_THUMB_CAPTURE_TIMEOUT", "12"))  # giây/lần
THUMB_WIDTH = int(os.environ.get("PT_THUMB_WIDTH", "854"))           # px chiều ngang JPEG
THUMB_BACKOFF_MAX = 600       # trần 10 phút nếu cam liên tục fail
THUMB_STALE_FACTOR = 2        # ảnh được coi "stale" nếu cũ hơn N × INTERVAL


def cleanup_old_clips() -> int:
    """Delete clip files + sidecar logs older than CLIP_TTL_SECS. Returns count removed."""
    now = time.time()
    removed = 0
    for p in CLIPS_DIR.iterdir():
        try:
            if (now - p.stat().st_mtime) > CLIP_TTL_SECS:
                p.unlink()
                removed += 1
        except Exception:
            pass
    return removed


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
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
    # Vendor migration (dahua|hikvision). Backfill existing rows = dahua.
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(nvrs)").fetchall()}
        if "vendor" not in cols:
            conn.execute("ALTER TABLE nvrs ADD COLUMN vendor TEXT DEFAULT 'dahua'")
        # web_port: cổng HTTP của ISAPI/CGI (DDNS NVR thường dùng 81, không 80).
        if "web_port" not in cols:
            conn.execute("ALTER TABLE nvrs ADD COLUMN web_port INTEGER DEFAULT 80")
        conn.commit()
    except Exception:
        pass
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
    # camera_host/user/password: cred RIÊNG của IP cam (Hik proxy cam).
    # Khi có, stream RTSP trực tiếp từ cam — bypass NVR transmux (cần vì
    # firmware Hik mới có bug PPS 0 trên NVR path, và direct = nhanh hơn).
    # Trống → fallback stream qua NVR (channel index).
    try:
        chan_cols = {r[1] for r in conn.execute("PRAGMA table_info(channels)").fetchall()}
        if "camera_host" not in chan_cols:
            conn.execute("ALTER TABLE channels ADD COLUMN camera_host TEXT DEFAULT ''")
        if "camera_user" not in chan_cols:
            conn.execute("ALTER TABLE channels ADD COLUMN camera_user TEXT DEFAULT ''")
        if "camera_password" not in chan_cols:
            conn.execute("ALTER TABLE channels ADD COLUMN camera_password TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass
    conn.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    return conn


DEFAULT_SETTINGS = {
    "brand_title": "PickClip",
    "brand_tagline": "Cắt highlight cam sân pickleball nhanh chóng",
    "brand_footer": "Công cụ được phát triển bởi Pickletour.vn",
    "brand_footer_url": "https://pickletour.vn",
}


def get_settings() -> dict:
    out = dict(DEFAULT_SETTINGS)
    try:
        with db() as conn:
            for r in conn.execute("SELECT key, value FROM settings").fetchall():
                out[r["key"]] = r["value"]
    except Exception:
        pass
    return out


def set_setting(key: str, value: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def rtsp_url(nvr: dict, channel: int, mode: str = "live",
             start: str = "", end: str = "", subtype: int = 0) -> str:
    """Vendor-aware RTSP URL. Dahua: /cam/realmonitor,/cam/playback.
    Hikvision: /Streaming/Channels/{ch}01, /Streaming/tracks/{ch}01."""
    import vendors as _v
    if mode == "live":
        return _v.rtsp_live(nvr, channel, subtype)
    # Playback: cho Hik cần Y/M/D format khác → convert nếu input là Dahua style
    if _v.vendor_of(nvr) == "hikvision":
        if "_" in start:
            start = _v.dahua_time_to_hik(start)
        if "_" in end:
            end = _v.dahua_time_to_hik(end)
    return _v.rtsp_playback(nvr, channel, start, end)


def parse_dt(s: str) -> str:
    """'YYYY-MM-DD HH:MM[:SS]' or ISO → Dahua 'YYYY_MM_DD_HH_MM_SS' (pads SS if missing)."""
    s = s.strip().replace("T", " ").replace("-", "_").replace(":", "_").replace(" ", "_")
    parts = [p for p in s.split("_") if p != ""]
    # Expected: year, month, day, hour, minute, [second]
    while len(parts) < 6:
        parts.append("00")
    return "_".join(parts[:6])


def kill_pid(pid: int) -> None:
    """Hard kill. Unix: SIGTERM cả process group. Windows: taskkill /T /F."""
    if IS_WIN:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=5)
        except Exception:
            pass
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


def stop_pid(pid: int) -> None:
    """Dừng NHẸ (SIGINT) để ffmpeg ghi trailer/faststart xong (clip phát được).
    Windows: ffmpeg nhận CTRL_BREAK_EVENT khi process được tạo với
    CREATE_NEW_PROCESS_GROUP (popen_detached đặt sẵn flag)."""
    if IS_WIN:
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        except Exception:
            kill_pid(pid)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGINT)
    except Exception:
        try:
            os.kill(pid, signal.SIGINT)
        except Exception:
            pass


def popen_detached(args, stdout=None, stderr=None, cwd=None):
    """subprocess.Popen cross-platform: Unix start_new_session=True,
    Windows CREATE_NEW_PROCESS_GROUP (để CTRL_BREAK_EVENT work).
    cwd optional (ffmpeg fmp4 HLS cần working dir để tạo init.mp4 đúng chỗ)."""
    kw = {"stdout": stdout, "stderr": stderr}
    if cwd is not None:
        kw["cwd"] = cwd
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(args, **kw)


def pid_alive(pid: int) -> bool:
    if IS_WIN:
        try:
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                               capture_output=True, text=True, timeout=5)
            return str(pid) in (r.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def get_channel(cid: int) -> dict | None:
    with db() as conn:
        # Dùng COALESCE để không vỡ nếu migration chưa chạy (column mới).
        try:
            row = conn.execute("""
                SELECT c.*, n.name AS nvr_name, n.host AS nvr_host,
                       n.rtsp_port, n.user, n.password,
                       COALESCE(c.camera_host, '') AS camera_host,
                       COALESCE(c.camera_user, '') AS camera_user,
                       COALESCE(c.camera_password, '') AS camera_password,
                       COALESCE(n.vendor, 'dahua') AS vendor,
                       COALESCE(n.web_port, 80) AS web_port,
                       COALESCE(n.clip_watermark, '') AS clip_watermark,
                       COALESCE(n.clip_wm_pos, 'bl')  AS clip_wm_pos,
                       COALESCE(n.clip_wm_scroll, 0)  AS clip_wm_scroll,
                       COALESCE(n.clip_wm_size, 40)   AS clip_wm_size,
                       COALESCE(n.clip_encoder, 'libx264') AS clip_encoder
                FROM channels c JOIN nvrs n ON c.nvr_id = n.id
                WHERE c.id=?
            """, (cid,)).fetchone()
        except sqlite3.OperationalError:
            # Fallback khi column chưa migrate
            row = conn.execute("""
                SELECT c.*, n.name AS nvr_name, n.host AS nvr_host,
                       n.rtsp_port, n.user, n.password
                FROM channels c JOIN nvrs n ON c.nvr_id = n.id
                WHERE c.id=?
            """, (cid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d.setdefault("clip_watermark", "")
    d.setdefault("clip_wm_pos", "bl")
    d.setdefault("clip_wm_scroll", 0)
    d.setdefault("clip_wm_size", 40)
    d.setdefault("clip_encoder", "libx264")
    d.setdefault("vendor", "dahua")
    d.setdefault("web_port", 80)
    d.setdefault("camera_host", "")
    d.setdefault("camera_user", "")
    d.setdefault("camera_password", "")
    return d


# -------- Watermark helpers (dùng chung với recorder.py) --------
from datetime import datetime as _dt
WM_FONTS = [
    # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    # Windows
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
]
WM_POS = {
    "bl": ("24",       "h-th-24"),
    "br": ("w-tw-24",  "h-th-24"),
    "tl": ("24",       "24"),
    "tr": ("w-tw-24",  "24"),
}


def wm_font() -> str:
    for p in WM_FONTS:
        if os.path.exists(p):
            return p
    return ""


def render_wm_text(tpl: str, nvr_name: str, channel: int) -> str:
    if not tpl:
        return ""
    now = _dt.now()
    return (tpl
            .replace("{date}",     now.strftime("%Y-%m-%d"))
            .replace("{time}",     now.strftime("%H:%M:%S"))
            .replace("{datetime}", now.strftime("%Y-%m-%d %H:%M:%S"))
            .replace("{ch}",       str(channel))
            .replace("{nvr}",      nvr_name))


def _ff_path(p) -> str:
    """ffmpeg drawtext filter cần forward-slash + escape colon trên Windows."""
    s = str(p).replace("\\", "/")
    # Windows: "C:/..." → ffmpeg parse "C:" là filter token; escape colon.
    if IS_WIN and len(s) > 2 and s[1] == ":":
        s = s[0] + "\\:" + s[2:]
    return s


def build_drawtext(text: str, out_path: Path,
                   wm_pos: str = "bl", wm_scroll: int = 0,
                   wm_size: int = 40, font: str = "") -> str:
    """Trả về chuỗi drawtext filter. Ghi text ra sidecar file (textfile=)
    để tránh escape hell. Caller phải xoá file này sau khi ffmpeg xong."""
    font = font or wm_font()
    if not font or not text:
        return ""
    wm_txt_path = out_path.with_suffix(".wm.txt")
    wm_txt_path.write_text(text, encoding="utf-8")
    size_div = max(12, min(80, int(wm_size or 40)))
    if wm_scroll:
        y_expr = "24" if wm_pos in ("tl", "tr") else "h-th-24"
        x_expr = "w-mod(t*120\\,w+tw)"
    else:
        x_expr, y_expr = WM_POS.get(wm_pos, WM_POS["bl"])
    return (
        f"drawtext=fontfile={_ff_path(font)}"
        f":textfile={_ff_path(wm_txt_path)}"
        f":reload=0"
        f":fontcolor=white"
        f":fontsize=h/{size_div}"
        f":box=1:boxcolor=black@0.55:boxborderw=10"
        f":x={x_expr}:y={y_expr}"
    )


def encoder_args(encoder: str, has_watermark: bool) -> tuple[list[str], str]:
    """Trả về (args_cho_-c:v, tên_encoder_thực). Nếu có watermark + encoder='copy'
    → tự nâng libx264. HW encoder (h264_v4l2m2m) không hỗ trợ -crf."""
    enc = (encoder or "libx264").lower()
    if has_watermark and enc == "copy":
        enc = "libx264"
    if enc == "copy":
        return (["-c:v", "copy"], "copy")
    if enc == "h264_v4l2m2m":
        return (["-c:v", "h264_v4l2m2m", "-num_capture_buffers", "32",
                 "-b:v", "4M"], "h264_v4l2m2m")
    return (["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"],
            "libx264")


def channel_rtsp(ch: dict, mode: str, start: str = "", end: str = "",
                 subtype: int = -1) -> str:
    nvr = {"host": ch["nvr_host"], "rtsp_port": ch["rtsp_port"],
           "user": ch["user"], "password": ch["password"],
           "vendor": ch.get("vendor", "dahua"),
           # Hik: cam riêng → stream TRỰC TIẾP, bypass NVR transmux.
           # camera_user/password override NVR cred nếu cam có cred riêng.
           "camera_host": ch.get("camera_host", ""),
           "camera_user": ch.get("camera_user", ""),
           "camera_password": ch.get("camera_password", "")}
    if subtype < 0:
        subtype = ch.get("subtype", 0)
    return rtsp_url(nvr, ch["channel"], mode, start, end, subtype)


def _probe_vcodec(src, rtsp: bool = False, fmt: str = "") -> str:
    """Tên codec video stream đầu tiên ('hevc'/'h264'/…), '' nếu lỗi."""
    args = ["ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1"]
    if fmt:
        args += ["-f", fmt]
    if rtsp:
        args += ["-rtsp_transport", "tcp", "-analyzeduration", "3000000", "-probesize", "3000000"]
    args += [str(src)]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=25)
        return (out.stdout or "").strip().lower()
    except Exception:
        return ""


def _is_hevc(codec: str) -> bool:
    return codec.startswith("hevc") or codec in ("h265", "hvc1", "hev1")


def run_clip_sync(rtsp: str, duration: int, out_path: Path,
                  watermark: str = "", wm_pos: str = "bl",
                  wm_scroll: int = 0, wm_size: int = 40,
                  encoder: str = "copy") -> tuple[bool, str]:
    """Run ffmpeg detached; wait for it; return (ok, err_log_tail).
    When `watermark` is non-empty, re-encodes with drawtext burn-in.
    When empty, defaults to `-c:v copy` (fast, no CPU)."""
    log = out_path.with_suffix(".log")
    progress = out_path.with_suffix(".progress")
    drawtext = build_drawtext(watermark, out_path, wm_pos, wm_scroll, wm_size) if watermark else ""
    enc_args, enc_name = encoder_args(encoder, bool(drawtext))

    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-progress", str(progress),
        "-rtsp_transport", "tcp",
        "-i", rtsp,
    ]
    if duration > 0:
        args += ["-t", str(duration)]

    if drawtext:
        vf = drawtext if enc_name != "h264_v4l2m2m" else "format=yuv420p," + drawtext
        args += ["-vf", vf, *enc_args]
    else:
        # Không watermark: tôn trọng encoder setting (copy mặc định).
        no_wm_args, no_wm_name = encoder_args(encoder or "copy", False)
        args += [*no_wm_args]
        enc_name = no_wm_name
        if no_wm_name == "copy":
            # HEVC copy vào MP4 mặc định tag 'hev1' → iOS KHÔNG phát. Ép 'hvc1'.
            vcodec = _probe_vcodec(rtsp, rtsp=True)
            if _is_hevc(vcodec):
                args += ["-tag:v", "hvc1"]
    args += [
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-y", str(out_path),
    ]
    try:
        with open(log, "ab") as f:
            f.write(f"[encoder] {enc_name}\n".encode())
            p = popen_detached(args, stdout=f, stderr=f)
        try:
            rc = p.wait(timeout=1200)
        except subprocess.TimeoutExpired:
            p.terminate()
            return False, "ffmpeg timeout (>20 min)"

        # Auto-fallback: HW encode fail → libx264
        if rc != 0 and enc_name == "h264_v4l2m2m":
            try: out_path.unlink(missing_ok=True)
            except Exception: pass
            with open(log, "ab") as f:
                f.write(b"[fallback] h264_v4l2m2m failed, retrying with libx264\n")
            fb = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                  "-rtsp_transport", "tcp", "-i", rtsp]
            if duration > 0: fb += ["-t", str(duration)]
            if drawtext: fb += ["-vf", drawtext]
            fb += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                   "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", "-y", str(out_path)]
            with open(log, "ab") as f:
                rc = subprocess.run(fb, stdout=f, stderr=f, timeout=1200).returncode

        # Cleanup sidecar textfile
        if drawtext:
            try: out_path.with_suffix(".wm.txt").unlink(missing_ok=True)
            except Exception: pass

        if rc == 0 and out_path.exists() and out_path.stat().st_size > 0:
            return True, ""
        try:
            tail = log.read_text()[-600:]
        except Exception:
            tail = f"rc={rc}"
        return False, tail
    except Exception as e:
        return False, str(e)


def dahua_fast_clip(nvr_host: str, user: str, password: str,
                    channel: int, start_str: str, end_str: str,
                    out_path: Path, progress_cb=None,
                    watermark: str = "", wm_pos: str = "bl",
                    wm_scroll: int = 0, wm_size: int = 40,
                    encoder: str = "copy",
                    vendor: str = "dahua",
                    rtsp_port: int = 554,
                    web_port: int = 80) -> tuple[bool, str]:
    """Vendor-aware fast clip extraction.
    Dahua: loadfile.cgi binary stream + ffmpeg remux (very fast, LAN speed).
    Hikvision: RTSP playback /Streaming/tracks/{ch}01?start=...&end=... (~realtime).
    start_str/end_str: 'YYYY-MM-DD HH:MM:SS'."""
    if (vendor or "").lower() in ("hikvision", "hik"):
        import vendors as _v
        nvr = {"host": nvr_host, "user": user, "password": password,
               "vendor": "hikvision", "rtsp_port": rtsp_port, "web_port": web_port}
        return _v.fast_clip_hik(nvr, channel, start_str, end_str, out_path,
                                progress_cb=progress_cb, watermark=watermark,
                                wm_pos=wm_pos, wm_scroll=wm_scroll,
                                wm_size=wm_size, encoder=encoder)
    import requests
    from requests.auth import HTTPDigestAuth

    url = (f"http://{nvr_host}:{web_port}/cgi-bin/loadfile.cgi?action=startLoad"
           f"&channel={channel}"
           f"&startTime={start_str.replace(' ', '%20')}"
           f"&endTime={end_str.replace(' ', '%20')}")
    tmp = out_path.with_suffix(".dav.tmp")
    received = 0
    try:
        with requests.get(url, auth=HTTPDigestAuth(user, password),
                          stream=True, timeout=180, verify=False) as r:
            if r.status_code != 200:
                return False, f"loadfile HTTP {r.status_code}: {r.text[:200]}"
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=131072):
                    if chunk:
                        f.write(chunk)
                        received += len(chunk)
                        if progress_cb:
                            try: progress_cb(received)
                            except Exception: pass
    except Exception as e:
        try: tmp.unlink()
        except Exception: pass
        return False, f"download fail: {e}"

    if received < 1024:
        try: tmp.unlink()
        except Exception: pass
        return False, f"download empty ({received} bytes)"

    log = out_path.with_suffix(".log")
    drawtext = build_drawtext(watermark, out_path, wm_pos, wm_scroll, wm_size) if watermark else ""
    enc_args, enc_name = encoder_args(encoder, bool(drawtext))

    # Ép demuxer DAV: file loadfile.cgi của một số NVR (vd. Lã Vọng) không bắt đầu
    # bằng chữ ký "DHAV" → ffmpeg tự nhận nhầm là H.264 thô → MẤT TIẾNG (pcm_alaw)
    # và sai timestamp (clip 5 phút ra ~10 phút, chạy chậm).
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "dhav", "-i", str(tmp),
    ]
    if drawtext:
        vf = drawtext if enc_name != "h264_v4l2m2m" else "format=yuv420p," + drawtext
        args += ["-vf", vf, *enc_args]
    else:
        no_wm_args, no_wm_name = encoder_args(encoder or "copy", False)
        args += [*no_wm_args]
        enc_name = no_wm_name
        if no_wm_name == "copy":
            # HEVC copy → tag 'hvc1' để iPhone phát/lưu được.
            vcodec = _probe_vcodec(tmp, fmt="dhav")
            if _is_hevc(vcodec):
                args += ["-tag:v", "hvc1"]
    args += [
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-y", str(out_path),
    ]
    # Re-encode (libx264/HW) có thể mất 3+ phút cho clip dài → nâng timeout
    enc_timeout = 180 if enc_name == "copy" else 1800
    try:
        with open(log, "ab") as f:
            f.write(f"[encoder] {enc_name}\n".encode())
            p = subprocess.run(args, stdout=f, stderr=f, timeout=enc_timeout)
        rc = p.returncode
        # Auto-fallback HW → libx264
        if rc != 0 and enc_name == "h264_v4l2m2m":
            try: out_path.unlink(missing_ok=True)
            except Exception: pass
            with open(log, "ab") as f:
                f.write(b"[fallback] h264_v4l2m2m failed, retrying with libx264\n")
            fb = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                  "-f", "dhav", "-i", str(tmp)]
            if drawtext: fb += ["-vf", drawtext]
            fb += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                   "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", "-y", str(out_path)]
            with open(log, "ab") as f:
                rc = subprocess.run(fb, stdout=f, stderr=f, timeout=1800).returncode
    except subprocess.TimeoutExpired:
        try: tmp.unlink()
        except Exception: pass
        if drawtext:
            try: out_path.with_suffix(".wm.txt").unlink(missing_ok=True)
            except Exception: pass
        return False, "remux timeout"
    finally:
        try: tmp.unlink()
        except Exception: pass
        if drawtext:
            try: out_path.with_suffix(".wm.txt").unlink(missing_ok=True)
            except Exception: pass

    if rc != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        try: tail = log.read_text()[-400:]
        except Exception: tail = f"rc={rc}"
        return False, f"remux fail: {tail}"
    return True, ""


def read_progress(path: Path) -> dict:
    """Parse ffmpeg -progress file. Return {out_time_sec, fps, speed, bitrate, size}."""
    import re as _re
    out = {"out_time_sec": 0.0, "fps": 0.0, "speed": 0.0, "bitrate": "", "size": 0}
    try:
        txt = path.read_text()
    except Exception:
        return out
    # ffmpeg writes blocks of key=value lines terminated by "progress=continue|end".
    # Take the LAST occurrence of each metric across the whole file.
    for pattern, key, cast in [
        (r"out_time_ms=(\d+)", "out_time_sec", lambda x: int(x) / 1_000_000.0),
        (r"out_time_us=(\d+)", "out_time_sec", lambda x: int(x) / 1_000_000.0),
        (r"fps=([\d.]+)", "fps", float),
        (r"speed=\s*([\d.]+)x?", "speed", float),
        (r"bitrate=\s*(\S+)", "bitrate", str),
        (r"total_size=(\d+)", "size", int),
    ]:
        matches = _re.findall(pattern, txt)
        if matches:
            try:
                out[key] = cast(matches[-1])
            except Exception:
                pass
    return out


def thumbnail(ch: dict, scale: str = "640:-2") -> Path | None:
    """Legacy helper (giữ cho tương thích): chỉ phục vụ từ cache đã sẵn trên
    đĩa, KHÔNG chụp đồng bộ. Worker nền chịu trách nhiệm cập nhật."""
    thumb = THUMBS_DIR / f"ch_{ch['id']}.jpg"
    return thumb if thumb.exists() and thumb.stat().st_size > 0 else None


# ===== Background thumbnail worker =========================================
# Thiết kế:
#   - Daemon thread duyệt bảng `channels` mỗi 2 giây; mỗi camera đến hạn
#     (last_run + max(INTERVAL, backoff)) thì enqueue 1 capture.
#   - Semaphore giới hạn song song (mặc định 2) → không gây quá tải Pi/NVR.
#   - Per-channel Lock chống trùng capture trên cùng camera.
#   - Ưu tiên /cgi-bin/snapshot.cgi của Dahua (nhẹ hơn mở stream);
#     fallback sang RTSP sub-stream + ffmpeg nếu CGI fail/không phải Dahua.
#   - Ghi file atomic: ch_<id>.jpg.tmp.<pid> → rename sang ch_<id>.jpg.
#     Người đang đọc ảnh cũ không bao giờ thấy file dở; os.replace() atomic
#     trên POSIX same-filesystem.
#   - Backoff lũy thừa (30s → 60s → … → 600s) khi 1 camera lỗi liên tiếp.
#   - mtime chính là capturedAt → tồn tại qua restart.
from threading import Lock as _Lock, Semaphore as _Sem, Thread as _Thread

_thumb_locks: dict = {}
_thumb_locks_guard = _Lock()
_thumb_backoff: dict = {}
_thumb_backoff_guard = _Lock()
_thumb_last_run: dict = {}
_thumb_sem = _Sem(THUMB_PARALLEL)
_thumb_worker_started = False
_thumb_worker_lock = _Lock()


def _thumb_path(cid: int) -> Path:
    return THUMBS_DIR / f"ch_{cid}.jpg"


def _channel_lock(cid: int) -> _Lock:
    with _thumb_locks_guard:
        lk = _thumb_locks.get(cid)
        if lk is None:
            lk = _Lock()
            _thumb_locks[cid] = lk
        return lk


def _dahua_snapshot_bytes(ch: dict, timeout: float) -> bytes:
    """Vendor-aware snapshot bytes. Hik với cam_host → snapshot TRỰC TIẾP
    từ cam IP (nhanh hơn, không qua NVR proxy). Fallback qua NVR nếu
    camera_host trống."""
    try:
        import vendors as _v
    except Exception:
        return b""
    nvr = {
        "host": ch.get("nvr_host") or ch.get("host", ""),
        "user": ch["user"],
        "password": ch["password"],
        "vendor": ch.get("vendor", "dahua"),
        "web_port": ch.get("web_port", 80),
    }
    cam_host = (ch.get("camera_host") or "").strip()
    if (cam_host and nvr["vendor"] == "hikvision"
            and _v._cam_reachable_from_nvr_lan(nvr["host"], cam_host)):
        cam_user = (ch.get("camera_user") or ch["user"]).strip() or ch["user"]
        cam_pw = ch.get("camera_password") or ch["password"]
        # cam direct dùng port 80 riêng của cam, không phải web_port của NVR
        cam_nvr = {**nvr, "host": cam_host, "user": cam_user,
                   "password": cam_pw, "web_port": 80}
        b = _v.snapshot_bytes(cam_nvr, 1, timeout)
        if b:
            return b
        # Fallback qua NVR nếu direct fail (cred sai hoặc cam offline)
    return _v.snapshot_bytes(nvr, ch["channel"], timeout)


def _ffmpeg_jpeg(input_bytes: bytes, input_url: str,
                 out_path: Path, width: int, timeout: float) -> bool:
    """Decode + scale → JPEG. Dùng input_bytes (JPEG raw, qua file tạm) HOẶC
    input_url (RTSP). Trả về True nếu ghi được JPEG hợp lệ."""
    src_tmp = None
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if input_url:
        args += ["-rtsp_transport", "tcp",
                 "-analyzeduration", "2000000", "-probesize", "2000000",
                 "-i", input_url]
    else:
        # Ghi JPEG raw ra file tạm rồi decode — pipe stdin kém tin cậy
        # với image2pipe + single JPEG trên 1 số bản ffmpeg.
        # Thêm hậu tố, không dùng with_suffix (sẽ ăn mất .jpg)
        src_tmp = out_path.parent / (out_path.stem + ".src.jpg")
        try:
            src_tmp.write_bytes(input_bytes)
        except Exception:
            return False
        args += ["-i", str(src_tmp)]
    args += ["-frames:v", "1", "-q:v", "6",
             "-vf", f"scale={width}:-2", str(out_path)]
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
        ok = (p.returncode == 0
              and out_path.exists() and out_path.stat().st_size > 0)
    except Exception:
        ok = False
    if src_tmp is not None:
        try: src_tmp.unlink(missing_ok=True)
        except Exception: pass
    if not ok:
        try: out_path.unlink(missing_ok=True)
        except Exception: pass
    return ok


def _capture_thumb(ch: dict) -> bool:
    """Chụp 1 ảnh thumbnail cho channel, ghi atomic. True nếu thành công."""
    cid = ch["id"]
    lk = _channel_lock(cid)
    if not lk.acquire(blocking=False):
        return False  # đã có capture khác đang chạy cho channel này
    try:
        with _thumb_sem:
            final = _thumb_path(cid)
            # Phải giữ đuôi .jpg để ffmpeg đoán đúng output format
            tmp = THUMBS_DIR / f"ch_{cid}.tmp.{os.getpid()}.jpg"
            try: tmp.unlink(missing_ok=True)
            except Exception: pass

            # 1) Dahua snapshot.cgi — nhẹ nhất, không mở stream
            jpg = _dahua_snapshot_bytes(ch, timeout=4)
            ok = False
            if jpg:
                ok = _ffmpeg_jpeg(jpg, "", tmp, THUMB_WIDTH,
                                  timeout=THUMB_CAPTURE_TIMEOUT)

            # 2) Fallback: RTSP sub-stream (độ phân giải thấp)
            if not ok:
                url = channel_rtsp(ch, "live", subtype=1)
                ok = _ffmpeg_jpeg(b"", url, tmp, THUMB_WIDTH,
                                  timeout=THUMB_CAPTURE_TIMEOUT)

            if ok and tmp.exists() and tmp.stat().st_size > 0:
                try:
                    os.replace(tmp, final)   # atomic trên cùng filesystem
                    return True
                except Exception:
                    pass
            try: tmp.unlink(missing_ok=True)
            except Exception: pass
            return False
    finally:
        lk.release()


def _schedule_capture(ch: dict):
    """Chạy 1 capture + cập nhật backoff."""
    cid = ch["id"]
    ok = _capture_thumb(ch)
    with _thumb_backoff_guard:
        if ok:
            _thumb_backoff.pop(cid, None)
        else:
            prev = _thumb_backoff.get(cid, 0)
            nxt = min(THUMB_BACKOFF_MAX, max(60, (prev or 30) * 2))
            _thumb_backoff[cid] = nxt


def _list_capture_targets() -> list:
    """Trả về danh sách channel dict đủ info để capture."""
    try:
        with db() as conn:
            try:
                rows = conn.execute("""
                    SELECT c.id, c.channel, c.nvr_id, c.subtype,
                           n.host AS nvr_host, n.rtsp_port,
                           n.user, n.password,
                           COALESCE(c.camera_host, '') AS camera_host,
                           COALESCE(n.vendor, 'dahua') AS vendor,
                           COALESCE(n.web_port, 80) AS web_port
                    FROM channels c JOIN nvrs n ON c.nvr_id = n.id
                """).fetchall()
            except sqlite3.OperationalError:
                rows = conn.execute("""
                    SELECT c.id, c.channel, c.nvr_id, c.subtype,
                           n.host AS nvr_host, n.rtsp_port,
                           n.user, n.password
                    FROM channels c JOIN nvrs n ON c.nvr_id = n.id
                """).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.setdefault("vendor", "dahua")
            d.setdefault("web_port", 80)
            out.append(d)
        return out
    except Exception:
        return []


def _thumbnail_loop():
    """Daemon loop: duyệt channel, enqueue capture khi tới hạn."""
    while True:
        try:
            targets = _list_capture_targets()
            now = time.time()
            for ch in targets:
                cid = ch["id"]
                with _thumb_backoff_guard:
                    bo = _thumb_backoff.get(cid, 0)
                lastt = _thumb_last_run.get(cid, 0)
                final = _thumb_path(cid)
                # Chưa từng có ảnh → chụp ngay, bỏ qua backoff
                if not final.exists():
                    due = True
                else:
                    due = (now - lastt) >= max(THUMB_INTERVAL, bo)
                if due:
                    _thumb_last_run[cid] = now
                    _Thread(target=_schedule_capture, args=(ch,),
                            daemon=True, name=f"thumb-cap-{cid}").start()
        except Exception:
            pass
        time.sleep(2)


def start_thumbnail_worker():
    """Idempotent — gọi 1 lần khi service start."""
    global _thumb_worker_started
    with _thumb_worker_lock:
        if _thumb_worker_started:
            return
        _thumb_worker_started = True
        _Thread(target=_thumbnail_loop, daemon=True,
                name="thumb-worker").start()


def thumbnail_meta(cid: int) -> dict:
    """Metadata ảnh cache. {} nếu chưa từng có."""
    p = _thumb_path(cid)
    try:
        st = p.stat()
    except OSError:
        return {}
    if st.st_size <= 0:
        return {}
    return {
        "path": p,
        "mtime": st.st_mtime,
        "size": st.st_size,
        "etag": f'W/"{int(st.st_mtime)}-{st.st_size}"',
    }

"""PiTailscale PickClip — customer-facing SPA + public API (separate port)."""
import argparse
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_file, send_from_directory

import rec_shared as rs

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"),
            static_folder=str(BASE_DIR / "static"))

# Cache-busting: a token that changes each time the service restarts
STATIC_VER = str(int(time.time()))


@app.context_processor
def inject_static_ver():
    return {"ver": STATIC_VER}


@app.after_request
def _no_cache_static(resp):
    from flask import request as _rq
    if _rq.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    return resp

_sessions: dict = {}
_sessions_lock = threading.Lock()
_clips: dict = {}
_clips_lock = threading.Lock()


# ---------- Public SPA ----------
@app.route("/")
@app.route("/clip")
@app.route("/clip/")
def spa():
    return render_template("clip.html")


# ---------- Public API: channels + thumbnails ----------
@app.get("/api/public/channels")
def api_channels():
    with rs.db() as conn:
        rows = conn.execute("SELECT id, name, description FROM channels ORDER BY name").fetchall()
    return jsonify([dict(r) for r in rows])


_EXPOSE_HEADERS = "X-Snapshot-Captured-At, X-Snapshot-Stale, ETag, Last-Modified"


@app.get("/api/public/channels/<int:cid>/thumbnail")
def api_thumb(cid):
    """Phục vụ thumbnail từ cache đĩa. KHÔNG BAO GIỜ chụp đồng bộ trong
    request — worker nền (rs.start_thumbnail_worker) chịu trách nhiệm làm
    mới ảnh ~30s/lần. Query ?t=... chỉ để bust cache phía client, không
    kích hoạt thêm tác vụ chụp."""
    ch = rs.get_channel(cid)
    if not ch:
        abort(404)
    meta = rs.thumbnail_meta(cid)
    if not meta:
        # Chưa từng có ảnh → 503 kèm Retry-After, không trả ảnh đen giả
        resp = Response("snapshot not ready", status=503,
                        mimetype="text/plain")
        resp.headers["Retry-After"] = "5"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    # Đọc atomic 1 lần — nếu worker vừa rotate giữa stat() và open(),
    # fallback về 503 thay vì nửa ảnh.
    try:
        with open(meta["path"], "rb") as f:
            data = f.read()
        import os as _os
        st = _os.stat(meta["path"])
        mtime = st.st_mtime
    except OSError:
        resp = Response("snapshot not ready", status=503,
                        mimetype="text/plain")
        resp.headers["Retry-After"] = "2"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    etag = f'W/"{int(mtime)}-{len(data)}"'
    # Hỗ trợ If-None-Match → 304 cho client đã có bản mới nhất
    inm = request.headers.get("If-None-Match", "")
    if inm and etag in inm:
        resp = Response(status=304)
    else:
        resp = Response(data, mimetype="image/jpeg")

    captured_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    stale = (time.time() - mtime) > (rs.THUMB_INTERVAL * rs.THUMB_STALE_FACTOR)

    resp.headers["X-Snapshot-Captured-At"] = captured_iso
    resp.headers["X-Snapshot-Stale"] = "true" if stale else "false"
    resp.headers["Last-Modified"] = formatdate(mtime, usegmt=True)
    resp.headers["ETag"] = etag
    # Cho client cache ngắn hơn 1 chu kỳ capture để không serve mãi 1 ảnh,
    # nhưng vẫn tiết kiệm được request burst.
    resp.headers["Cache-Control"] = "public, max-age=10"
    # CORS: cho browser (ví dụ pickletour.vn embed) đọc được metadata
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Expose-Headers"] = _EXPOSE_HEADERS
    return resp


# ---------- Public API: streaming (HLS) ----------
def _cleanup_sessions():
    now = time.time()
    with _sessions_lock:
        for sid in list(_sessions.keys()):
            s = _sessions[sid]
            if now - s["last_access"] > rs.SESSION_IDLE_SECS:
                rs.kill_pid(s["pid"])
                shutil.rmtree(s["dir"], ignore_errors=True)
                del _sessions[sid]


@app.post("/api/public/stream/start")
def api_stream_start():
    import subprocess
    data = request.get_json(silent=True) or {}
    ch = rs.get_channel(int(data.get("channel_id") or 0))
    if not ch:
        return jsonify({"error": "channel not found"}), 404

    _cleanup_sessions()
    sid = uuid.uuid4().hex[:12]
    sdir = rs.STREAM_DIR / f"pub_{sid}"
    sdir.mkdir(parents=True, exist_ok=True)

    mode = data.get("mode", "live")
    start = rs.parse_dt(data.get("start", "")) if mode == "playback" else ""
    end = rs.parse_dt(data.get("end", "")) if mode == "playback" else ""
    # subtype -1 → use channel's configured subtype (admin set 0=Main or 1=Sub)
    rtsp = rs.channel_rtsp(ch, mode, start, end, subtype=-1)

    log = sdir / "ffmpeg.log"
    # Live = cửa sổ trượt (nhẹ). Playback = event-VOD (giữ hết segment) để người xem
    # TUA được cả đoạn trước khi cắt.
    if mode == "playback":
        hls_opts = ["-hls_time", "2", "-hls_playlist_type", "event",
                    "-hls_flags", "append_list+independent_segments"]
    else:
        hls_opts = ["-hls_time", "2", "-hls_list_size", "6",
                    "-hls_flags", "delete_segments+append_list+independent_segments"]
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-fflags", "+genpts+discardcorrupt",
        "-i", rtsp,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k", "-ac", "1", "-ar", "44100",
        "-f", "hls",
        *hls_opts,
        "-hls_segment_type", "fmp4",  # H.265/HEVC compat
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", str(sdir / "seg_%04d.m4s"),
        str(sdir / "index.m3u8"),
    ]
    with open(log, "ab") as f:
        p = subprocess.Popen(args, stdout=f, stderr=f, start_new_session=True)

    with _sessions_lock:
        _sessions[sid] = {
            "pid": p.pid, "dir": sdir, "started": time.time(),
            "last_access": time.time(), "channel_id": ch["id"], "mode": mode,
        }

    manifest = sdir / "index.m3u8"
    for _ in range(50):
        if manifest.exists() and manifest.stat().st_size > 50:
            break
        time.sleep(0.2)
        if p.poll() is not None:
            try:
                err = log.read_text()[-400:]
            except Exception:
                err = ""
            return jsonify({"error": "stream failed", "log": err}), 500

    return jsonify({"session_id": sid, "manifest": f"/stream/{sid}/index.m3u8"})


@app.post("/api/public/stream/stop")
def api_stream_stop():
    sid = (request.get_json(silent=True) or {}).get("session_id")
    with _sessions_lock:
        s = _sessions.pop(sid, None)
    if not s:
        return jsonify({"ok": False})
    rs.kill_pid(s["pid"])
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
    sdir = rs.STREAM_DIR / f"pub_{sid}"
    if not sdir.exists():
        abort(404)
    resp = send_from_directory(sdir, filename)
    if filename.endswith(".m3u8"):
        resp.headers["Cache-Control"] = "no-cache, no-store"
    return resp


# ---------- Public API: clip ----------
def _run_clip_bg(clip_id: str, ch: dict, start_str_dahua: str, end_str_dahua: str,
                 out_path: Path):
    """Try Dahua fast download (loadfile.cgi); fall back to RTSP playback on fail."""
    # Convert Dahua "YYYY_MM_DD_HH_MM_SS" → "YYYY-MM-DD HH:MM:SS"
    def _h(s):
        p = s.split("_")
        return f"{p[0]}-{p[1]}-{p[2]} {p[3]}:{p[4]}:{p[5]}"
    http_start = _h(start_str_dahua)
    http_end = _h(end_str_dahua)

    def on_bytes(n):
        with _clips_lock:
            if clip_id in _clips:
                _clips[clip_id]["downloaded"] = n

    with _clips_lock:
        _clips[clip_id]["phase"] = "fast_download"

    # Watermark/encoder đọc từ NVR config (join trong rs.get_channel)
    wm_text = rs.render_wm_text(
        ch.get("clip_watermark") or "", ch.get("nvr_name") or "", ch["channel"])
    wm_pos = ch.get("clip_wm_pos") or "bl"
    wm_scroll = int(ch.get("clip_wm_scroll") or 0)
    wm_size = int(ch.get("clip_wm_size") or 40)
    encoder = ch.get("clip_encoder") or "libx264"

    ok, err = rs.dahua_fast_clip(
        ch["nvr_host"], ch["user"], ch["password"],
        ch["channel"], http_start, http_end,
        out_path, progress_cb=on_bytes,
        watermark=wm_text, wm_pos=wm_pos, wm_scroll=wm_scroll,
        wm_size=wm_size, encoder=encoder,
    )
    if ok:
        with _clips_lock:
            c = _clips.get(clip_id)
            if c:
                c["status"] = "done"
                c["size"] = out_path.stat().st_size
                c["phase"] = "done"
        return

    # Fallback: RTSP playback
    with _clips_lock:
        c = _clips.get(clip_id)
        if c:
            c["phase"] = "rtsp_fallback"
            c["fallback_reason"] = err[:200]
    rtsp = rs.channel_rtsp(ch, "playback", start_str_dahua, end_str_dahua, subtype=0)
    ok2, err2 = rs.run_clip_sync(
        rtsp, 0, out_path,
        watermark=wm_text, wm_pos=wm_pos, wm_scroll=wm_scroll,
        wm_size=wm_size, encoder=encoder,
    )
    with _clips_lock:
        c = _clips.get(clip_id)
        if not c:
            return
        if ok2:
            c["status"] = "done"
            c["size"] = out_path.stat().st_size
            c["phase"] = "done"
        else:
            c["status"] = "error"
            c["err"] = f"fast: {err}\n\nrtsp: {err2}"


@app.post("/api/public/clip")
def api_clip():
    data = request.get_json(silent=True) or {}
    ch = rs.get_channel(int(data.get("channel_id") or 0))
    if not ch:
        return jsonify({"error": "channel not found"}), 404

    quick_min = int(data.get("quick_min") or 0)
    start_raw = data.get("start", "")
    end_raw = data.get("end", "")

    if quick_min:
        if quick_min not in (5, 10, 15, 30):
            return jsonify({"error": "quick_min must be 5/10/15/30"}), 400
        now = datetime.now().replace(microsecond=0)
        end_dt = now - timedelta(seconds=10)
        start_dt = end_dt - timedelta(minutes=quick_min)
        start = start_dt.strftime("%Y_%m_%d_%H_%M_%S")
        end = end_dt.strftime("%Y_%m_%d_%H_%M_%S")
        tag = f"last{quick_min}min"
    else:
        if not start_raw or not end_raw:
            return jsonify({"error": "start+end or quick_min required"}), 400
        start = rs.parse_dt(start_raw)
        end = rs.parse_dt(end_raw)
        try:
            dt_s = datetime.strptime(start, "%Y_%m_%d_%H_%M_%S")
            dt_e = datetime.strptime(end, "%Y_%m_%d_%H_%M_%S")
            delta = (dt_e - dt_s).total_seconds()
            if delta <= 0:
                return jsonify({"error": "end phải lớn hơn start"}), 400
            if delta > 30 * 60:
                return jsonify({"error": "Tối đa 30 phút mỗi clip"}), 400
        except ValueError:
            return jsonify({"error": "invalid datetime format"}), 400
        tag = start_raw.replace(":", "").replace(" ", "_").replace("T", "_")[:19]

    clip_id = uuid.uuid4().hex[:12]
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", ch["name"])[:40]
    fname = f"{safe_name}_{tag}_{ts}.mp4"
    out_path = rs.CLIPS_DIR / fname

    try:
        dt_s = datetime.strptime(start, "%Y_%m_%d_%H_%M_%S")
        dt_e = datetime.strptime(end, "%Y_%m_%d_%H_%M_%S")
        expected_sec = int((dt_e - dt_s).total_seconds())
    except Exception:
        expected_sec = (quick_min * 60) if quick_min else 0

    with _clips_lock:
        _clips[clip_id] = {
            "id": clip_id, "path": out_path, "name": fname,
            "created": time.time(), "status": "running",
            "err": "", "size": 0,
            "channel_name": ch["name"],
            "expected_sec": expected_sec,
            "phase": "init",
            "downloaded": 0,
        }
    threading.Thread(target=_run_clip_bg,
                     args=(clip_id, ch, start, end, out_path),
                     daemon=True).start()
    return jsonify({"clip_id": clip_id, "name": fname})


@app.get("/api/public/clips/<clip_id>")
def api_clip_status(clip_id):
    with _clips_lock:
        c = _clips.get(clip_id)
    if not c:
        abort(404)
    # If marked running but file exists + stable, reconcile (handles service
    # restarts where ffmpeg finished detached but Python lost in-memory state).
    if c["status"] == "running":
        p = c["path"]
        if p.exists():
            try:
                sz = p.stat().st_size
                # consider "done" if size unchanged in last ~2s
                time.sleep(0.0)  # no-op; just a marker
                if sz > 0 and (time.time() - p.stat().st_mtime) > 3:
                    with _clips_lock:
                        c["status"] = "done"
                        c["size"] = sz
            except Exception:
                pass
    progress = {"out_time_sec": 0, "fps": 0, "speed": 0, "size": 0, "bitrate": ""}
    if c["status"] == "running":
        progress = rs.read_progress(c["path"].with_suffix(".progress"))
    return jsonify({
        "id": c["id"], "name": c["name"], "status": c["status"],
        "err": c["err"][:300], "size": c["size"], "created": c["created"],
        "expected_sec": c.get("expected_sec", 0),
        "phase": c.get("phase", ""),
        "downloaded": c.get("downloaded", 0),
        "progress": progress,
        "download_url": f"/clips/{c['id']}" if c["status"] == "done" else "",
    })


@app.get("/clips/<clip_id>")
def download_clip(clip_id):
    with _clips_lock:
        c = _clips.get(clip_id)
    if not c or c["status"] != "done":
        abort(404)
    if not c["path"].exists():
        with _clips_lock:
            _clips.pop(clip_id, None)
        abort(410)  # Gone — expired by auto-cleanup
    return send_file(c["path"], as_attachment=True, download_name=c["name"])


# ---------- Public API: GHI HÌNH TRỰC TIẾP (record live → MP4) ----------
@app.post("/api/public/record/start")
def api_record_start():
    data = request.get_json(silent=True) or {}
    ch = rs.get_channel(int(data.get("channel_id") or 0))
    if not ch:
        return jsonify({"error": "channel not found"}), 404
    rtsp = rs.channel_rtsp(ch, "live", subtype=0)  # main stream cho chất lượng tốt
    rec_id = uuid.uuid4().hex[:12]
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", ch["name"])[:40]
    fname = f"{safe_name}_rec_{ts}.mp4"
    out_path = rs.CLIPS_DIR / fname
    vcodec = rs._probe_vcodec(rtsp, rtsp=True)
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-fflags", "+genpts+discardcorrupt",
            "-i", rtsp, "-t", "1800",  # trần an toàn 30 phút
            "-c:v", "copy"]
    if rs._is_hevc(vcodec):
        args += ["-tag:v", "hvc1"]  # HEVC → iPhone phát được
    args += ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-y", str(out_path)]
    log = out_path.with_suffix(".log")
    with open(log, "ab") as f:
        p = subprocess.Popen(args, stdout=f, stderr=f, start_new_session=True)
    with _clips_lock:
        _clips[rec_id] = {
            "id": rec_id, "path": out_path, "name": fname, "created": time.time(),
            "status": "recording", "err": "", "size": 0, "channel_name": ch["name"],
            "expected_sec": 0, "phase": "recording", "downloaded": 0, "pid": p.pid,
        }
    return jsonify({"record_id": rec_id, "name": fname})


@app.post("/api/public/record/stop")
def api_record_stop():
    rec_id = (request.get_json(silent=True) or {}).get("record_id")
    with _clips_lock:
        c = _clips.get(rec_id)
    if not c:
        abort(404)
    pid = c.get("pid")
    if pid:
        rs.stop_pid(pid)  # SIGINT → ffmpeg ghi trailer + faststart
    # Chờ ffmpeg kết thúc + finalize (tối đa ~20s).
    for _ in range(80):
        if not (pid and rs.pid_alive(pid)) and c["path"].exists() and c["path"].stat().st_size > 0:
            break
        time.sleep(0.25)
    with _clips_lock:
        if c["path"].exists() and c["path"].stat().st_size > 0:
            c["status"] = "done"
            c["size"] = c["path"].stat().st_size
            c["phase"] = "done"
        else:
            c["status"] = "error"
            c["err"] = "record empty"
    return jsonify({
        "status": c["status"], "clip_id": rec_id, "name": c["name"],
        "size": c.get("size", 0),
        "download_url": f"/clips/{rec_id}" if c["status"] == "done" else "",
    })


@app.get("/api/public/branding")
def api_branding():
    s = rs.get_settings()
    return jsonify({
        "title": s.get("brand_title", "PickClip"),
        "tagline": s.get("brand_tagline", ""),
        "footer": s.get("brand_footer", ""),
        "footer_url": s.get("brand_footer_url", ""),
    })


@app.route("/clips/<clip_id>", methods=["HEAD"])
def head_clip(clip_id):
    """Lightweight existence check for history page."""
    with _clips_lock:
        c = _clips.get(clip_id)
    if not c or c["status"] != "done" or not c["path"].exists():
        abort(404)
    return ("", 200)


@app.get("/api/public/retention")
def api_retention():
    """Tell the UI how long clips are kept."""
    return jsonify({
        "ttl_secs": rs.CLIP_TTL_SECS,
        "ttl_human": f"{rs.CLIP_TTL_SECS // 3600} giờ"
                     if rs.CLIP_TTL_SECS >= 3600
                     else f"{rs.CLIP_TTL_SECS // 60} phút",
    })


# ---------- housekeeper ----------
def _cleanup_dead_clip_state():
    """Drop in-memory clip entries whose files have been purged on disk."""
    with _clips_lock:
        for cid in list(_clips.keys()):
            c = _clips[cid]
            if c["status"] == "done" and not c["path"].exists():
                del _clips[cid]


def _housekeeper():
    while True:
        time.sleep(60)
        try:
            _cleanup_sessions()
            rs.cleanup_old_clips()
            _cleanup_dead_clip_state()
        except Exception:
            pass


threading.Thread(target=_housekeeper, daemon=True).start()

# Khởi chạy worker chụp thumbnail nền ngay khi service start → mọi channel
# có ảnh "nạp sớm" trong vài giây đầu, request /thumbnail không bao giờ phải
# chờ ffmpeg/NVR.
rs.start_thumbnail_worker()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("PT_CLIP_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PT_CLIP_PORT", "9996")))
    args = ap.parse_args()
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()

"""Vendor abstraction cho NVR backends.

Hỗ trợ:
- Dahua (CGI: snapshot.cgi, configManager.cgi, loadfile.cgi)
- Hikvision (ISAPI: /ISAPI/Streaming, /ISAPI/System/Video, /ISAPI/ContentMgmt)

Thiết kế: KHÔNG wrap Dahua trong class — giữ code cũ nguyên. Chỉ thêm:
  - vendor_of(nvr) → "dahua" | "hikvision"
  - snapshot_bytes(nvr, ch, timeout) — dispatch
  - rtsp_live(nvr, ch, subtype) — dispatch
  - rtsp_playback(nvr, ch, start, end) — dispatch
  - fetch_osd_all(nvr) — list[dict] vendor-neutral format
  - apply_osd_bulk(nvr, changes) — áp dụng danh sách thay đổi
  - fast_clip_hik(...) — Hik playback→MP4 (gọi từ rec_shared.dahua_fast_clip dispatcher)
  - test_connection(nvr) — probe deviceInfo
  - list_channels_hik(nvr) — discover channels khi add NVR

Caller (recorder.py, clipapp.py, rec_shared.py) kiểm tra vendor_of(nvr) rồi
hoặc gọi vào module này, hoặc fallback code Dahua cũ. Rõ ràng, dễ debug.
"""
import re
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import requests
from requests.auth import HTTPDigestAuth

_HIK_NS = "{http://www.hikvision.com/ver20/XMLSchema}"
_HIK_NS2 = "{http://www.hikvision.com/ver10/XMLSchema}"
requests.packages.urllib3.disable_warnings()  # self-signed OK


def _hfind(parent, tag):
    """Hik XML namespace-aware find. CẢNH BÁO: KHÔNG dùng `a or b` với ET
    Element — leaf element (<model>X</model>) evaluate FALSE vì len(el)==0.
    Phải so sánh is None explicit."""
    if parent is None:
        return None
    el = parent.find(_HIK_NS + tag)
    if el is None:
        el = parent.find(_HIK_NS2 + tag)
    if el is None:
        el = parent.find(tag)  # fallback không namespace
    return el


def _htext(parent, tag, default=""):
    el = _hfind(parent, tag)
    if el is None or el.text is None:
        return default
    return el.text


def _hfindall(parent, tag):
    """findall với namespace, kết hợp 2 namespace."""
    if parent is None:
        return []
    out = parent.findall(_HIK_NS + tag)
    out += parent.findall(_HIK_NS2 + tag)
    return out


def vendor_of(nvr: dict) -> str:
    v = (nvr.get("vendor") or "dahua").lower().strip()
    if v in ("hik", "hikvision"):
        return "hikvision"
    return "dahua"


# ============================================================================
# SNAPSHOT (JPEG bytes)
# ============================================================================
def snapshot_bytes(nvr: dict, channel: int, timeout: float = 4.0) -> bytes:
    """Trả 1 JPEG của channel; b'' nếu fail."""
    if vendor_of(nvr) == "hikvision":
        return _hik_snapshot_bytes(nvr, channel, timeout)
    return _dahua_snapshot_bytes(nvr, channel, timeout)


def _dahua_snapshot_bytes(nvr: dict, channel: int, timeout: float) -> bytes:
    url = f"http://{nvr['host']}/cgi-bin/snapshot.cgi?channel={channel}&type=1"
    try:
        r = requests.get(url, auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                         timeout=timeout)
        if r.status_code == 200 and r.content[:3] == b"\xff\xd8\xff":
            return r.content
    except Exception:
        pass
    return b""


def _hik_snapshot_bytes(nvr: dict, channel: int, timeout: float) -> bytes:
    # Hik: ch 1 → "101" (channel 1, stream 01=main). Sub: "102".
    for stream_suffix in ("01", "02"):
        url = (f"http://{nvr['host']}/ISAPI/Streaming/channels/"
               f"{channel}{stream_suffix}/picture")
        try:
            r = requests.get(url, auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                             timeout=timeout)
            if r.status_code == 200 and r.content[:3] == b"\xff\xd8\xff":
                return r.content
        except Exception:
            continue
    return b""


# ============================================================================
# RTSP URLs
# ============================================================================
def _is_private_ipv4(host: str) -> bool:
    """True nếu host là RFC1918 IP (10/8, 172.16-31, 192.168/16) hoặc tên
    local (.local, localhost). False cho DDNS, public IP, public hostname."""
    import ipaddress
    h = host.strip().lower()
    if h in ("localhost", "127.0.0.1", "::1") or h.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(h).is_private
    except Exception:
        return False  # tên DDNS/hostname → mặc định không private


def _cam_reachable_from_nvr_lan(nvr_host: str, cam_host: str) -> bool:
    """Chỉ dùng cam direct khi: (a) nvr_host và cam_host cùng LAN /24, HOẶC
    (b) nvr_host là private IP (cùng LAN hạ tầng). Nếu nvr_host là DDNS/
    public → cam LAN private không reach được → fallback qua NVR."""
    if not cam_host:
        return False
    nvr_private = _is_private_ipv4(nvr_host)
    cam_private = _is_private_ipv4(cam_host)
    # Trường hợp phổ biến: NVR DDNS (public) → cam LAN → KHÔNG direct
    if cam_private and not nvr_private:
        return False
    return True


def rtsp_live(nvr: dict, channel: int, subtype: int = 0) -> str:
    """RTSP live URL. Hik: nếu nvr dict có 'camera_host' (IP riêng của cam)
    VÀ cam reachable từ context hiện tại (cùng LAN), stream TRỰC TIẾP từ
    cam qua /Streaming/Channels/101 — bypass NVR transmux (fix bug PPS 0
    trên firmware cũ; nhanh hơn, 1-cam-1-stream).
    NVR DDNS/public + cam LAN private → tự fallback qua NVR (cam không
    reach được từ internet). Dahua: luôn qua NVR."""
    port = nvr.get("rtsp_port", 554)
    host = nvr["host"]
    if vendor_of(nvr) == "hikvision":
        cam_host = (nvr.get("camera_host") or "").strip()
        if cam_host and _cam_reachable_from_nvr_lan(host, cam_host):
            # Direct cam: channel luôn = 1 từ góc nhìn cam riêng lẻ.
            # Dùng cred của cam nếu có, else dùng cred NVR (thường cam Hik
            # trong 1 cụm NVR có cùng cred 'admin', nhưng cài đặt lẻ có
            # thể khác → user phải nhập).
            cu = (nvr.get("camera_user") or nvr["user"]).strip() or nvr["user"]
            cp = nvr.get("camera_password") or nvr["password"]
            u = urllib.parse.quote(cu, safe="")
            p = urllib.parse.quote(cp, safe="")
            stream = "02" if subtype else "01"
            return f"rtsp://{u}:{p}@{cam_host}:554/Streaming/Channels/1{stream}"
        # Fallback: qua NVR với channel index
        user = urllib.parse.quote(nvr["user"], safe="")
        pw = urllib.parse.quote(nvr["password"], safe="")
        stream = "02" if subtype else "01"
        return f"rtsp://{user}:{pw}@{host}:{port}/Streaming/Channels/{channel}{stream}"
    # Dahua
    user = urllib.parse.quote(nvr["user"], safe="")
    pw = urllib.parse.quote(nvr["password"], safe="")
    return (f"rtsp://{user}:{pw}@{host}:{port}"
            f"/cam/realmonitor?channel={channel}&subtype={subtype}")


def rtsp_playback(nvr: dict, channel: int, start: str, end: str) -> str:
    """start/end: cho Dahua = 'YYYY_MM_DD_HH_MM_SS', cho Hik = 'YYYYMMDDTHHMMSSZ'.
    Playback LUÔN qua NVR vì cam IP không lưu lịch sử — chỉ NVR có ổ cứng."""
    user = urllib.parse.quote(nvr["user"], safe="")
    pw = urllib.parse.quote(nvr["password"], safe="")
    port = nvr.get("rtsp_port", 554)
    host = nvr["host"]
    if vendor_of(nvr) == "hikvision":
        return (f"rtsp://{user}:{pw}@{host}:{port}/Streaming/tracks/{channel}01"
                f"?starttime={start}&endtime={end}")
    return (f"rtsp://{user}:{pw}@{host}:{port}"
            f"/cam/playback?channel={channel}&starttime={start}&endtime={end}")


def dahua_time_to_hik(dahua_dt: str) -> str:
    """'YYYY_MM_DD_HH_MM_SS' → 'YYYYMMDDTHHMMSSZ' (Hik playback format)."""
    p = [x for x in dahua_dt.split("_") if x]
    while len(p) < 6:
        p.append("00")
    return f"{p[0]}{p[1]}{p[2]}T{p[3]}{p[4]}{p[5]}Z"


def human_to_hik(human_dt: str) -> str:
    """'YYYY-MM-DD HH:MM:SS' → 'YYYYMMDDTHHMMSSZ'."""
    s = human_dt.strip().replace("T", " ").replace("-", "").replace(":", "")
    s = s.replace(" ", "T")
    while len(s.replace("T", "")) < 14:
        s += "0"
    return s + "Z"


# ============================================================================
# CONNECTION TEST / DEVICE INFO
# ============================================================================
def test_connection(nvr: dict, timeout: float = 8.0) -> dict:
    """{ok, info|error, vendor_detected, model?, fw?}"""
    if vendor_of(nvr) == "hikvision":
        return _hik_device_info(nvr, timeout)
    return _dahua_device_info(nvr, timeout)


def _dahua_device_info(nvr: dict, timeout: float) -> dict:
    url = f"http://{nvr['host']}/cgi-bin/magicBox.cgi?action=getDeviceType"
    try:
        r = requests.get(url, auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                         timeout=timeout, verify=False)
        if r.status_code == 200 and "type=" in r.text:
            model = r.text.split("type=", 1)[1].strip().splitlines()[0]
            return {"ok": True, "vendor_detected": "dahua", "model": model,
                    "info": r.text.strip()[:200]}
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _hik_device_info(nvr: dict, timeout: float) -> dict:
    url = f"http://{nvr['host']}/ISAPI/System/deviceInfo"
    try:
        r = requests.get(url, auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                         timeout=timeout, verify=False)
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        root = ET.fromstring(r.text)
        info = {"vendor_detected": "hikvision"}
        for tag in ("deviceName", "model", "serialNumber", "firmwareVersion",
                    "deviceType", "manufacturer"):
            v = _htext(root, tag)
            if v:
                info[tag] = v
        return {"ok": True, "info": f"{info.get('model', '')} fw {info.get('firmwareVersion', '')}",
                **info}
    except ET.ParseError as e:
        return {"ok": False, "error": f"XML parse: {e}; body: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ============================================================================
# CHANNEL LIST (dùng khi add NVR mới để auto-discover)
# ============================================================================
def list_channels(nvr: dict, timeout: float = 10.0) -> list:
    """Trả list[{channel, name}]. Luôn cố gắng, fail trả []."""
    if vendor_of(nvr) == "hikvision":
        return _hik_list_channels(nvr, timeout)
    return _dahua_list_channel_titles(nvr["host"], nvr["user"], nvr["password"],
                                      timeout)


def _dahua_list_channel_titles(host, user, pw, timeout=10.0) -> list:
    """Dahua table.ChannelTitle[N].Name=..."""
    url = (f"http://{host}/cgi-bin/configManager.cgi"
           f"?action=getConfig&name=ChannelTitle")
    try:
        r = requests.get(url, auth=HTTPDigestAuth(user, pw), timeout=timeout,
                         verify=False)
        if r.status_code != 200:
            return []
        out = []
        rx = re.compile(r"table\.ChannelTitle\[(\d+)\]\.Name=(.*)")
        for line in r.text.splitlines():
            m = rx.match(line.strip())
            if m:
                out.append({"channel": int(m.group(1)) + 1,
                            "title": m.group(2).strip()})
        return out
    except Exception:
        return []


def _hik_list_channels(nvr: dict, timeout: float) -> list:
    """Hik NVR: GET /ISAPI/ContentMgmt/InputProxy/channels (NVR proxy cam IP).
    DVR fallback: /ISAPI/System/Video/inputs/channels (local input).
    Trả [{channel, title, camera_host}] — camera_host là IP riêng của cam
    (dùng cho RTSP direct, bypass NVR transmux)."""
    out = []
    for path, tag in (
        ("/ISAPI/ContentMgmt/InputProxy/channels", "InputProxyChannel"),
        ("/ISAPI/System/Video/inputs/channels", "VideoInputChannel"),
    ):
        url = f"http://{nvr['host']}{path}"
        try:
            r = requests.get(url, auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                             timeout=timeout, verify=False)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.text)
            for el in _hfindall(root, tag):
                id_text = _htext(el, "id")
                if not id_text:
                    continue
                try:
                    cid = int(id_text)
                except ValueError:
                    continue
                entry = {"channel": cid, "title": _htext(el, "name").strip(),
                         "camera_host": ""}
                # sourceInputPortDescriptor có ipAddress của cam
                src = _hfind(el, "sourceInputPortDescriptor")
                if src is not None:
                    ip = _htext(src, "ipAddress").strip()
                    if ip:
                        entry["camera_host"] = ip
                out.append(entry)
            if out:
                return out
        except Exception:
            continue
    return out


# ============================================================================
# OSD — fetch & apply (channel_title, time_title, channel_name)
# ============================================================================
def fetch_osd_all(nvr: dict, timeout: float = 15.0) -> list:
    """Trả list[{channel, channel_title, time_title, channel_name, display_name}]."""
    if vendor_of(nvr) == "hikvision":
        return _hik_fetch_osd_all(nvr, timeout)
    return _dahua_fetch_osd_all(nvr, timeout)


def _dahua_fetch_osd_all(nvr: dict, timeout: float) -> list:
    url = (f"http://{nvr['host']}/cgi-bin/configManager.cgi"
           f"?action=getConfig&name=VideoWidget")
    try:
        r = requests.get(url, auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                         timeout=timeout, verify=False)
        if r.status_code != 200:
            return []
    except Exception:
        return []
    rx = re.compile(r"table\.VideoWidget\[(\d+)\]\.(.+?)=(.*)$")
    out: dict = {}
    for line in r.text.splitlines():
        m = rx.match(line.strip())
        if not m:
            continue
        idx = int(m.group(1))
        key, val = m.group(2), m.group(3).strip()
        d = out.setdefault(idx, {"channel": idx + 1, "channel_title": None,
                                 "time_title": None, "channel_name": ""})
        if key == "ChannelTitle.EncodeBlend":
            d["channel_title"] = val.lower() == "true"
        elif key == "TimeTitle.EncodeBlend":
            d["time_title"] = val.lower() == "true"
        elif key == "ChannelTitle.ChannelName":
            d["channel_name"] = val
    titles = _dahua_list_channel_titles(nvr["host"], nvr["user"], nvr["password"],
                                        timeout=8)
    title_map = {t["channel"]: t["title"] for t in titles}
    res = []
    for i in sorted(out.keys()):
        d = out[i]
        d["display_name"] = d["channel_name"] or title_map.get(d["channel"], "")
        res.append(d)
    return res


def _hik_fetch_osd_all(nvr: dict, timeout: float) -> list:
    """Hik: phải gọi /overlays per-channel. Lấy channel list trước."""
    chans = _hik_list_channels(nvr, timeout)
    if not chans:
        # fallback: quét từ 1..nvr.channels
        chans = [{"channel": c, "title": ""}
                 for c in range(1, int(nvr.get("channels", 16)) + 1)]
    name_map = {c["channel"]: c.get("title", "") for c in chans}
    out = []
    for c in chans:
        ch = c["channel"]
        overlays = _hik_fetch_overlay(nvr, ch, timeout=5)
        d = {
            "channel": ch,
            "channel_title": overlays.get("channel_title_on"),
            "time_title": overlays.get("datetime_on"),
            "channel_name": overlays.get("channel_name") or name_map.get(ch, ""),
            "display_name": overlays.get("channel_name") or name_map.get(ch, ""),
        }
        out.append(d)
    return out


def _hik_fetch_overlay(nvr: dict, channel: int, timeout: float) -> dict:
    """GET /ISAPI/System/Video/inputs/channels/{ch}/overlays → dict."""
    url = (f"http://{nvr['host']}/ISAPI/System/Video/inputs/channels/"
           f"{channel}/overlays")
    out = {"channel_title_on": None, "datetime_on": None, "channel_name": ""}
    try:
        r = requests.get(url, auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                         timeout=timeout, verify=False)
        if r.status_code != 200:
            return out
        root = ET.fromstring(r.text)
    except Exception:
        return out
    # channelNameOverlay/enabled
    ch_el = _hfind(root, "channelNameOverlay")
    en_text = _htext(ch_el, "enabled")
    if en_text:
        out["channel_title_on"] = en_text.lower() == "true"
    # DateTimeOverlay/enabled
    dt_el = _hfind(root, "DateTimeOverlay")
    en_text = _htext(dt_el, "enabled")
    if en_text:
        out["datetime_on"] = en_text.lower() == "true"
    # Channel name từ /ISAPI/System/Video/inputs/channels/{ch} (input chan tag)
    try:
        r2 = requests.get(
            f"http://{nvr['host']}/ISAPI/System/Video/inputs/channels/{channel}",
            auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
            timeout=timeout, verify=False)
        if r2.status_code == 200:
            r2_root = ET.fromstring(r2.text)
            out["channel_name"] = _htext(r2_root, "name")
    except Exception:
        pass
    return out


def apply_osd_bulk(nvr: dict, changes: list, timeout: float = 30.0) -> dict:
    """changes: list[{channel, channel_title?, time_title?, channel_name?}].
    Trả {ok, applied, errors: []}."""
    if vendor_of(nvr) == "hikvision":
        return _hik_apply_osd_bulk(nvr, changes, timeout)
    return _dahua_apply_osd_bulk(nvr, changes, timeout)


def _dahua_apply_osd_bulk(nvr: dict, changes: list, timeout: float) -> dict:
    params = []
    for item in changes:
        try:
            ch = int(item.get("channel", 0)) - 1
        except Exception:
            continue
        if ch < 0:
            continue
        if "channel_title" in item:
            v = "true" if item["channel_title"] else "false"
            params.append(f"VideoWidget[{ch}].ChannelTitle.EncodeBlend={v}")
            params.append(f"VideoWidget[{ch}].ChannelTitle.PreviewBlend={v}")
        if "time_title" in item:
            v = "true" if item["time_title"] else "false"
            params.append(f"VideoWidget[{ch}].TimeTitle.EncodeBlend={v}")
            params.append(f"VideoWidget[{ch}].TimeTitle.PreviewBlend={v}")
        if item.get("channel_name"):
            name = urllib.parse.quote(str(item["channel_name"]), safe="")
            params.append(f"VideoWidget[{ch}].ChannelTitle.ChannelName={name}")
    if not params:
        return {"ok": True, "applied": 0, "errors": []}
    encoded = [p.replace("[", "%5B").replace("]", "%5D") for p in params]
    url = (f"http://{nvr['host']}/cgi-bin/configManager.cgi?action=setConfig&"
           + "&".join(encoded))
    try:
        r = requests.get(url, auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                         timeout=timeout, verify=False)
    except Exception as e:
        return {"ok": False, "applied": 0, "errors": [f"request fail: {e}"]}
    body = r.text.strip()
    ok = r.status_code == 200 and ("OK" in body.upper() or body == "")
    return {"ok": ok, "status": r.status_code, "applied": len(changes),
            "response": body[:400], "errors": [] if ok else [body[:200]]}


def _hik_apply_osd_bulk(nvr: dict, changes: list, timeout: float) -> dict:
    """Hik: per-channel PUT, không batch được. Loop từng channel."""
    applied = 0
    errors = []
    for item in changes:
        try:
            ch = int(item.get("channel", 0))
        except Exception:
            continue
        if ch < 1:
            continue
        ok, err = _hik_set_channel_osd(nvr, ch, item, timeout)
        if ok:
            applied += 1
        else:
            errors.append(f"ch{ch}: {err}")
    return {"ok": not errors, "applied": applied, "errors": errors}


def _hik_set_channel_osd(nvr: dict, channel: int, change: dict,
                         timeout: float) -> tuple:
    """Modify overlays XML for channel + PUT back. Đổi tên qua endpoint khác."""
    auth = HTTPDigestAuth(nvr["user"], nvr["password"])
    host = nvr["host"]
    # 1. Overlay toggles (channel_title, time_title)
    if "channel_title" in change or "time_title" in change:
        url = f"http://{host}/ISAPI/System/Video/inputs/channels/{channel}/overlays"
        try:
            r = requests.get(url, auth=auth, timeout=timeout, verify=False)
            if r.status_code != 200:
                return False, f"GET overlays HTTP {r.status_code}"
            # Strip namespace for easier edit
            xml = r.text
            if "channel_title" in change:
                want = "true" if change["channel_title"] else "false"
                xml = _toggle_xml_block(xml, "channelNameOverlay", want)
            if "time_title" in change:
                want = "true" if change["time_title"] else "false"
                xml = _toggle_xml_block(xml, "DateTimeOverlay", want)
            # PUT back
            r = requests.put(url, data=xml.encode("utf-8"),
                             headers={"Content-Type": "application/xml"},
                             auth=auth, timeout=timeout, verify=False)
            if r.status_code not in (200, 201):
                return False, f"PUT overlays HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, f"overlays: {e}"
    # 2. Channel name
    if change.get("channel_name"):
        new_name = str(change["channel_name"])
        # Thử input channel (DVR) trước
        url = f"http://{host}/ISAPI/System/Video/inputs/channels/{channel}"
        try:
            r = requests.get(url, auth=auth, timeout=timeout, verify=False)
            if r.status_code == 200 and "<name>" in r.text:
                xml = re.sub(r"<name>[^<]*</name>",
                             f"<name>{_xml_escape(new_name)}</name>",
                             r.text, count=1)
                r = requests.put(url, data=xml.encode("utf-8"),
                                 headers={"Content-Type": "application/xml"},
                                 auth=auth, timeout=timeout, verify=False)
                if r.status_code not in (200, 201):
                    return False, f"PUT name HTTP {r.status_code}: {r.text[:200]}"
            else:
                # NVR proxy path
                url2 = f"http://{host}/ISAPI/ContentMgmt/InputProxy/channels/{channel}"
                r = requests.get(url2, auth=auth, timeout=timeout, verify=False)
                if r.status_code != 200:
                    return False, f"GET channel HTTP {r.status_code}"
                xml = re.sub(r"<name>[^<]*</name>",
                             f"<name>{_xml_escape(new_name)}</name>",
                             r.text, count=1)
                r = requests.put(url2, data=xml.encode("utf-8"),
                                 headers={"Content-Type": "application/xml"},
                                 auth=auth, timeout=timeout, verify=False)
                if r.status_code not in (200, 201):
                    return False, f"PUT proxy name HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, f"name: {e}"
    return True, ""


def _toggle_xml_block(xml: str, block_tag: str, enabled_value: str) -> str:
    """Trong block <block_tag>...</block_tag>, replace <enabled>...</enabled>."""
    pattern = re.compile(
        rf"(<{block_tag}\b[^>]*>.*?<enabled>)[^<]*(</enabled>.*?</{block_tag}>)",
        re.DOTALL)
    return pattern.sub(rf"\g<1>{enabled_value}\g<2>", xml, count=1)


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ============================================================================
# FAST CLIP — Hikvision (ISAPI /ContentMgmt/download → fallback RTSP playback)
# ============================================================================
def _hik_isapi_download(nvr: dict, channel: int, start_human: str,
                        end_human: str, tmp_path: Path,
                        progress_cb=None, timeout: int = 1800) -> tuple:
    """Tải clip qua Hik ISAPI /ContentMgmt/download — NVR push file ở tốc độ
    ổ cứng (~10-20× realtime, tương đương Dahua loadfile.cgi).
    Response thường là MP4 binary trực tiếp.
    Trả (ok, err_msg_hoặc_'')."""
    start_hik = human_to_hik(start_human)
    end_hik = human_to_hik(end_human)
    host = nvr["host"]
    port = nvr.get("rtsp_port", 554)
    # NVR lưu record trackID = channel*100+1 (main). size=0 → NVR tự quyết.
    playback_uri = (f"rtsp://{host}:{port}/Streaming/tracks/{channel}01"
                    f"?starttime={start_hik}&endtime={end_hik}&name=clip&size=0")
    body = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<downloadRequest version="1.0" '
            f'xmlns="http://www.hikvision.com/ver20/XMLSchema">\n'
            f'  <playbackURI>{_xml_escape(playback_uri)}</playbackURI>\n'
            f'</downloadRequest>\n')
    url = f"http://{host}/ISAPI/ContentMgmt/download"
    received = 0
    try:
        with requests.post(url, data=body.encode("utf-8"),
                           headers={"Content-Type": "application/xml"},
                           auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                           stream=True, timeout=timeout, verify=False) as r:
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}: {r.text[:200]}"
            ct = (r.headers.get("Content-Type") or "").lower()
            if "xml" in ct or "html" in ct:
                return False, f"non-binary response ({ct}): {r.text[:200]}"
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=131072):
                    if chunk:
                        f.write(chunk)
                        received += len(chunk)
                        if progress_cb:
                            try: progress_cb(received)
                            except Exception: pass
    except Exception as e:
        try: tmp_path.unlink()
        except Exception: pass
        return False, f"{type(e).__name__}: {e}"
    if received < 1024:
        try: tmp_path.unlink()
        except Exception: pass
        return False, f"empty response ({received}B — có thể không có bản ghi trong khoảng)"
    return True, ""


def fast_clip_hik(nvr: dict, channel: int, start_human: str, end_human: str,
                  out_path: Path, progress_cb=None, watermark: str = "",
                  wm_pos: str = "bl", wm_scroll: int = 0, wm_size: int = 40,
                  encoder: str = "copy") -> tuple:
    """Hik clip extraction 2-tier:
    1. ISAPI /ContentMgmt/download (disk-speed ~10-20× realtime, tương đương
       Dahua loadfile.cgi) → nếu MP4 hợp lệ → remux faststart + bake watermark
    2. Fallback RTSP playback (1× realtime) nếu ISAPI không support firmware
    start_human/end_human: 'YYYY-MM-DD HH:MM:SS'. Trả (ok, err)."""
    import subprocess
    from rec_shared import build_drawtext, encoder_args, _probe_vcodec, _is_hevc

    log = out_path.with_suffix(".log")

    # === TIER 1: ISAPI download (disk-speed) ===
    isapi_tmp = out_path.with_suffix(".hik.mp4.tmp")
    with open(log, "ab") as f:
        f.write(b"[tier1] ISAPI /ContentMgmt/download\n")
    ok, err = _hik_isapi_download(nvr, channel, start_human, end_human,
                                  isapi_tmp, progress_cb=progress_cb,
                                  timeout=1800)
    if ok:
        with open(log, "ab") as f:
            f.write(f"[tier1] downloaded {isapi_tmp.stat().st_size}B\n".encode())
        # Remux để add faststart + watermark nếu cần
        drawtext = build_drawtext(watermark, out_path, wm_pos, wm_scroll,
                                  wm_size) if watermark else ""
        enc_args, enc_name = encoder_args(encoder, bool(drawtext))
        args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-i", str(isapi_tmp)]
        if drawtext:
            vf = drawtext if enc_name != "h264_v4l2m2m" else "format=yuv420p," + drawtext
            args += ["-vf", vf, *enc_args]
        else:
            no_wm_args, no_wm_name = encoder_args(encoder or "copy", False)
            args += [*no_wm_args]
            enc_name = no_wm_name
            if no_wm_name == "copy":
                vcodec = _probe_vcodec(isapi_tmp)
                if _is_hevc(vcodec):
                    args += ["-tag:v", "hvc1"]
        args += ["-c:a", "aac", "-b:a", "128k",
                 "-movflags", "+faststart", "-y", str(out_path)]
        try:
            with open(log, "ab") as f:
                f.write(f"[tier1 remux] {enc_name}\n".encode())
                rc = subprocess.run(args, stdout=f, stderr=f, timeout=1800).returncode
        except subprocess.TimeoutExpired:
            rc = -1
        finally:
            try: isapi_tmp.unlink()
            except Exception: pass
            if drawtext:
                try: out_path.with_suffix(".wm.txt").unlink(missing_ok=True)
                except Exception: pass
        if rc == 0 and out_path.exists() and out_path.stat().st_size > 1024:
            with open(log, "ab") as f:
                f.write(b"[tier1] OK\n")
            return True, ""
        with open(log, "ab") as f:
            f.write(f"[tier1 remux] fail rc={rc} → fallback RTSP\n".encode())
    else:
        with open(log, "ab") as f:
            f.write(f"[tier1] skip: {err} → fallback RTSP\n".encode())

    # === TIER 2: RTSP playback fallback (1× realtime) ===
    start = human_to_hik(start_human)
    end = human_to_hik(end_human)
    rtsp = rtsp_playback(nvr, channel, start, end)
    drawtext = build_drawtext(watermark, out_path, wm_pos, wm_scroll, wm_size) if watermark else ""
    enc_args, enc_name = encoder_args(encoder, bool(drawtext))

    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-analyzeduration", "10000000", "-probesize", "10000000",
        "-i", rtsp,
    ]
    if drawtext:
        vf = drawtext if enc_name != "h264_v4l2m2m" else "format=yuv420p," + drawtext
        args += ["-vf", vf, *enc_args]
    else:
        no_wm_args, no_wm_name = encoder_args(encoder or "copy", False)
        args += [*no_wm_args]
        enc_name = no_wm_name
        if no_wm_name == "copy":
            vcodec = _probe_vcodec(rtsp, rtsp=True)
            if _is_hevc(vcodec):
                args += ["-tag:v", "hvc1"]
    args += [
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-y", str(out_path),
    ]
    enc_timeout = 1800
    try:
        with open(log, "ab") as f:
            f.write(f"[tier2 RTSP] encoder={enc_name}\n".encode())
            p = subprocess.run(args, stdout=f, stderr=f, timeout=enc_timeout)
        rc = p.returncode
        if rc != 0 and enc_name == "h264_v4l2m2m":
            try: out_path.unlink(missing_ok=True)
            except Exception: pass
            with open(log, "ab") as f:
                f.write(b"[fallback] h264_v4l2m2m failed, retrying with libx264\n")
            fb = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                  "-rtsp_transport", "tcp",
                  "-analyzeduration", "10000000", "-probesize", "10000000",
                  "-i", rtsp]
            if drawtext: fb += ["-vf", drawtext]
            fb += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                   "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", "-y", str(out_path)]
            with open(log, "ab") as f:
                rc = subprocess.run(fb, stdout=f, stderr=f, timeout=1800).returncode
    except subprocess.TimeoutExpired:
        if drawtext:
            try: out_path.with_suffix(".wm.txt").unlink(missing_ok=True)
            except Exception: pass
        return False, "clip timeout"
    finally:
        if drawtext:
            try: out_path.with_suffix(".wm.txt").unlink(missing_ok=True)
            except Exception: pass

    if rc != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        try: tail = log.read_text()[-400:]
        except Exception: tail = f"rc={rc}"
        return False, f"hik clip fail: {tail}"
    return True, ""


# ============================================================================
# SYSTEM OPS — Hikvision (dashboard: info, time, reboot, storage)
# ============================================================================
def system_info_hik(nvr: dict, timeout: float = 8.0) -> dict:
    """Trả {ok, device_type, serial, firmware, nvr_time, pi_time, drift_seconds}."""
    from datetime import datetime
    import xml.etree.ElementTree as ET
    out = {"ok": False, "nvr_time": "", "drift_seconds": None,
           "pi_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        r = requests.get(f"http://{nvr['host']}/ISAPI/System/deviceInfo",
                         auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                         timeout=timeout, verify=False)
        if r.status_code != 200:
            return {**out, "error": f"deviceInfo HTTP {r.status_code}"}
        root = ET.fromstring(r.text)
        out["device_type"] = _htext(root, "deviceType")
        out["serial"] = _htext(root, "serialNumber")
        out["firmware"] = _htext(root, "firmwareVersion")
        out["processor"] = _htext(root, "model")
    except Exception as e:
        return {**out, "error": f"deviceInfo: {e}"}
    # Time
    try:
        r = requests.get(f"http://{nvr['host']}/ISAPI/System/time",
                         auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                         timeout=timeout, verify=False)
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            local = _htext(root, "localTime")
            if local:
                # Format: 2026-11-11T12:00:00+07:00 → strip tz
                local_clean = local.split("+")[0].split("Z")[0].replace("T", " ")
                out["nvr_time"] = local_clean
                try:
                    nvr_dt = datetime.strptime(local_clean, "%Y-%m-%d %H:%M:%S")
                    out["drift_seconds"] = int((datetime.now() - nvr_dt).total_seconds())
                except Exception:
                    pass
    except Exception:
        pass
    out["ok"] = True
    return out


def sync_time_hik(nvr: dict, timeout: float = 10.0) -> dict:
    """Set NVR time về giờ Pi hiện tại."""
    from datetime import datetime
    now = datetime.now()
    # Hik yêu cầu ISO 8601 local time (không UTC)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S") + "+07:00"
    body = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Time version="1.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">\n'
            f'  <timeMode>manual</timeMode>\n'
            f'  <localTime>{now_iso}</localTime>\n'
            f'  <timeZone>CST-7:00:00</timeZone>\n'
            f'</Time>\n')
    try:
        r = requests.put(f"http://{nvr['host']}/ISAPI/System/time",
                         data=body.encode("utf-8"),
                         headers={"Content-Type": "application/xml"},
                         auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                         timeout=timeout, verify=False)
        return {"ok": r.status_code in (200, 201),
                "status": r.status_code,
                "set_to": now.strftime("%Y-%m-%d %H:%M:%S"),
                "resp": r.text[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def reboot_hik(nvr: dict, timeout: float = 5.0) -> dict:
    """Reboot NVR qua ISAPI. Thường drop connection trước khi trả → treat OK."""
    try:
        r = requests.put(f"http://{nvr['host']}/ISAPI/System/reboot",
                         auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                         timeout=timeout, verify=False)
        return {"ok": r.status_code in (200, 201),
                "status": r.status_code, "resp": r.text[:200]}
    except Exception as e:
        return {"ok": True,
                "note": f"NVR đã nhận lệnh (connection dropped: {e})"}


def storage_hik(nvr: dict, timeout: float = 10.0) -> dict:
    """Thông tin HDD. Trả {ok, disks: [{index, name, state, healthy, total_gb,
    used_gb, used_pct}]}. Hik endpoint: /ISAPI/ContentMgmt/Storage."""
    import xml.etree.ElementTree as ET
    try:
        r = requests.get(f"http://{nvr['host']}/ISAPI/ContentMgmt/Storage",
                         auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                         timeout=timeout, verify=False)
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        root = ET.fromstring(r.text)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    disks = []
    # hddList/hdd/*
    hdd_list = _hfind(root, "hddList")
    if hdd_list is not None:
        for hdd in _hfindall(hdd_list, "hdd"):
            try:
                cap_mb = int(_htext(hdd, "capacity") or 0)
                free_mb = int(_htext(hdd, "freeSpace") or 0)
            except ValueError:
                cap_mb, free_mb = 0, 0
            used_mb = cap_mb - free_mb
            disks.append({
                "index": int(_htext(hdd, "id") or 0),
                "name": _htext(hdd, "hddName") or "HDD",
                "state": _htext(hdd, "status") or "",
                "healthy": (_htext(hdd, "status") or "").lower() in ("ok", "formatted", "normal"),
                "total_gb": round(cap_mb / 1024, 1),
                "used_gb": round(used_mb / 1024, 1),
                "used_pct": round((used_mb / cap_mb * 100), 1) if cap_mb else 0,
                "partitions": 1,
            })
    return {"ok": True, "disks": disks}


# ============================================================================
# IMAGE SETTINGS — Hikvision (color + ircut day/night)
# ============================================================================
def image_get_hik(nvr: dict, channel: int, timeout: float = 8.0) -> dict:
    """GET /ISAPI/Image/channels/{ch}/color + /ircutfilter."""
    import xml.etree.ElementTree as ET
    auth = HTTPDigestAuth(nvr["user"], nvr["password"])
    host = nvr["host"]
    out = {"channel": channel, "brightness": 50, "contrast": 50,
           "saturation": 50, "hue": 50, "gamma": 50, "style": "Standard",
           "day_night_mode": 0}
    try:
        r = requests.get(f"http://{host}/ISAPI/Image/channels/{channel}/color",
                         auth=auth, timeout=timeout, verify=False)
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            for k, tag in (("brightness", "brightnessLevel"),
                           ("contrast", "contrastLevel"),
                           ("saturation", "saturationLevel"),
                           ("hue", "hueLevel")):
                v = _htext(root, tag)
                if v:
                    try: out[k] = int(v)
                    except ValueError: pass
    except Exception:
        pass
    # IrcutFilter
    try:
        r = requests.get(f"http://{host}/ISAPI/Image/channels/{channel}/ircutfilter",
                         auth=auth, timeout=timeout, verify=False)
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            mode = (_htext(root, "IrcutFilterType") or "").lower()
            # 0=Auto, 1=Day (color), 2=Night (BW)
            out["day_night_mode"] = {"auto": 0, "day": 1, "night": 2}.get(mode, 0)
    except Exception:
        pass
    return out


def image_set_hik(nvr: dict, channel: int, changes: dict,
                  timeout: float = 10.0) -> dict:
    """PUT image settings. changes: {brightness, contrast, saturation, hue,
    gamma, day_night_mode} — chỉ gửi field có trong dict."""
    auth = HTTPDigestAuth(nvr["user"], nvr["password"])
    host = nvr["host"]
    applied = []
    errors = []
    # Color group
    color_keys = {"brightness": "brightnessLevel", "contrast": "contrastLevel",
                  "saturation": "saturationLevel", "hue": "hueLevel"}
    color_changes = {k: v for k, v in changes.items() if k in color_keys}
    if color_changes:
        try:
            r = requests.get(f"http://{host}/ISAPI/Image/channels/{channel}/color",
                             auth=auth, timeout=timeout, verify=False)
            if r.status_code != 200:
                errors.append(f"GET color HTTP {r.status_code}")
            else:
                xml = r.text
                for k, tag in color_keys.items():
                    if k in color_changes:
                        v = str(max(0, min(100, int(color_changes[k]))))
                        xml = re.sub(rf"<{tag}>[^<]*</{tag}>",
                                     f"<{tag}>{v}</{tag}>", xml)
                        applied.append(k)
                r = requests.put(f"http://{host}/ISAPI/Image/channels/{channel}/color",
                                 data=xml.encode("utf-8"),
                                 headers={"Content-Type": "application/xml"},
                                 auth=auth, timeout=timeout, verify=False)
                if r.status_code not in (200, 201):
                    errors.append(f"PUT color HTTP {r.status_code}: {r.text[:120]}")
        except Exception as e:
            errors.append(f"color: {e}")
    # IrcutFilter (day/night)
    if "day_night_mode" in changes:
        mode_map = {0: "auto", 1: "day", 2: "night"}
        try:
            mode_val = mode_map.get(int(changes["day_night_mode"]), "auto")
            body = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
                    f'<IrcutFilter version="1.0" '
                    f'xmlns="http://www.hikvision.com/ver20/XMLSchema">\n'
                    f'  <IrcutFilterType>{mode_val}</IrcutFilterType>\n'
                    f'</IrcutFilter>\n')
            r = requests.put(f"http://{host}/ISAPI/Image/channels/{channel}/ircutfilter",
                             data=body.encode("utf-8"),
                             headers={"Content-Type": "application/xml"},
                             auth=auth, timeout=timeout, verify=False)
            if r.status_code in (200, 201):
                applied.append("day_night_mode")
            else:
                errors.append(f"PUT ircut HTTP {r.status_code}")
        except Exception as e:
            errors.append(f"ircut: {e}")
    return {"ok": not errors, "applied": applied, "errors": errors}


# ============================================================================
# RECORD MODE — Hikvision (manual start/stop per channel)
# ============================================================================
def record_get_all_hik(nvr: dict, timeout: float = 10.0) -> dict:
    """Query recording status của mọi channel. Trả {modes: {channel: mode}}.
    mode 0 = Auto (theo schedule), 1 = Manual ON, 2 = Manual OFF (override)."""
    import xml.etree.ElementTree as ET
    auth = HTTPDigestAuth(nvr["user"], nvr["password"])
    host = nvr["host"]
    modes = {}
    try:
        r = requests.get(f"http://{host}/ISAPI/ContentMgmt/record/tracks",
                         auth=auth, timeout=timeout, verify=False)
        if r.status_code != 200:
            return {"ok": False, "modes": {},
                    "error": f"HTTP {r.status_code}"}
        root = ET.fromstring(r.text)
    except Exception as e:
        return {"ok": False, "modes": {}, "error": str(e)}
    # Mỗi <Track><id>N01</id><Description>...</Description><LoopEnable>...
    for tr in _hfindall(root, "Track"):
        tid = _htext(tr, "id")
        if not tid or not tid.endswith("01"):
            continue  # chỉ track main stream
        try:
            ch = int(tid[:-2]) if len(tid) > 2 else int(tid)
        except ValueError:
            continue
        enabled = (_htext(tr, "Enable") or _htext(tr, "LoopEnable") or "false").lower() == "true"
        modes[ch] = 0 if enabled else 2  # mặc định 0=Auto nếu schedule bật
    return {"ok": True, "modes": modes}


def record_set_hik(nvr: dict, channel: int, mode: int,
                   timeout: float = 10.0) -> dict:
    """Manual start/stop recording. mode 0=Auto(schedule), 1=Manual ON, 2=Off.
    Hik dùng /ISAPI/ContentMgmt/record/control/manual/{start,stop}/tracks/{ch}01."""
    auth = HTTPDigestAuth(nvr["user"], nvr["password"])
    host = nvr["host"]
    track = f"{channel}01"
    action = "stop" if int(mode) == 2 else "start" if int(mode) == 1 else "start"
    # mode 0 (auto) → đảm bảo manual stop để schedule control
    if int(mode) == 0:
        action = "stop"
    url = (f"http://{host}/ISAPI/ContentMgmt/record/control/manual/"
           f"{action}/tracks/{track}")
    try:
        r = requests.put(url, auth=auth, timeout=timeout, verify=False)
        return {"ok": r.status_code in (200, 201), "status": r.status_code,
                "action": action, "channel": channel,
                "resp": r.text[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============================================================================
# LOGO OVERLAY — Hikvision (TextOverlay slot)
# ============================================================================
_HIK_LOGO_SLOT = 1  # TextOverlay id — slot 0 thường dành cho channel name
_HIK_LOGO_POS = {
    # Phần trăm vị trí trên normalizedScreen (default 704x576)
    "tl": (24, 24),
    "tr": (520, 24),     # 704-184
    "bl": (24, 500),
    "br": (520, 500),
}


def logo_get_hik(nvr: dict, channel: int, timeout: float = 8.0) -> dict:
    """GET /ISAPI/System/Video/inputs/channels/{ch}/overlays, parse TextOverlay slot."""
    import xml.etree.ElementTree as ET
    auth = HTTPDigestAuth(nvr["user"], nvr["password"])
    host = nvr["host"]
    out = {"channel": channel, "enabled": False, "text": "",
           "position": "br", "color": [255, 255, 255, 255]}
    try:
        r = requests.get(f"http://{host}/ISAPI/System/Video/inputs/channels/{channel}/overlays",
                         auth=auth, timeout=timeout, verify=False)
        if r.status_code != 200:
            return out
        root = ET.fromstring(r.text)
    except Exception:
        return out
    tol = _hfind(root, "TextOverlayList")
    if tol is None:
        return out
    for t in _hfindall(tol, "TextOverlay"):
        tid = _htext(t, "id")
        if tid == str(_HIK_LOGO_SLOT):
            out["enabled"] = (_htext(t, "enabled") or "false").lower() == "true"
            out["text"] = _htext(t, "message") or ""
            break
    return out


def logo_set_hik(nvr: dict, channel: int, text: str, position: str = "br",
                 timeout: float = 10.0) -> dict:
    """Set/clear logo text overlay. text='' → tắt overlay."""
    import xml.etree.ElementTree as ET
    auth = HTTPDigestAuth(nvr["user"], nvr["password"])
    host = nvr["host"]
    try:
        r = requests.get(f"http://{host}/ISAPI/System/Video/inputs/channels/{channel}/overlays",
                         auth=auth, timeout=timeout, verify=False)
        if r.status_code != 200:
            return {"ok": False, "error": f"GET HTTP {r.status_code}"}
        xml = r.text
    except Exception as e:
        return {"ok": False, "error": f"GET: {e}"}
    enabled = "true" if text.strip() else "false"
    px, py = _HIK_LOGO_POS.get(position, _HIK_LOGO_POS["br"])
    text_esc = _xml_escape(text.strip())
    # Build TextOverlay XML cho slot 1
    new_overlay = (
        f'<TextOverlay>'
        f'<id>{_HIK_LOGO_SLOT}</id>'
        f'<enabled>{enabled}</enabled>'
        f'<positionX>{px}</positionX>'
        f'<positionY>{py}</positionY>'
        f'<message>{text_esc}</message>'
        f'</TextOverlay>'
    )
    # Pattern match existing TextOverlay with id=slot, replace; nếu không có → inject
    # trong TextOverlayList.
    slot_re = re.compile(
        rf'<TextOverlay\b[^>]*>\s*<id>{_HIK_LOGO_SLOT}</id>.*?</TextOverlay>',
        re.DOTALL)
    if slot_re.search(xml):
        xml = slot_re.sub(new_overlay, xml, count=1)
    else:
        # Inject vào cuối TextOverlayList
        xml = re.sub(r"</TextOverlayList>", new_overlay + "</TextOverlayList>",
                     xml, count=1)
    try:
        r = requests.put(f"http://{host}/ISAPI/System/Video/inputs/channels/{channel}/overlays",
                         data=xml.encode("utf-8"),
                         headers={"Content-Type": "application/xml"},
                         auth=auth, timeout=timeout, verify=False)
        return {"ok": r.status_code in (200, 201), "status": r.status_code,
                "resp": r.text[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============================================================================
# ENCODE SET — Hikvision (change codec H.264 ↔ H.265)
# ============================================================================
def encode_set_hik(nvr: dict, channel: int, main: str = "", sub: str = "",
                   timeout: float = 15.0) -> dict:
    """Change codec. main/sub: 'H.264' hoặc 'H.265'. '' = giữ nguyên.
    Hik: /ISAPI/Streaming/channels/{ch}01 (main) + {ch}02 (sub)."""
    auth = HTTPDigestAuth(nvr["user"], nvr["password"])
    host = nvr["host"]
    applied = []
    errors = []
    for suffix, bucket, new_codec in (("01", "main", main), ("02", "sub", sub)):
        if not new_codec:
            continue
        codec = new_codec.upper().replace(".", "")  # H264 / H265
        try:
            r = requests.get(f"http://{host}/ISAPI/Streaming/channels/{channel}{suffix}",
                             auth=auth, timeout=timeout, verify=False)
            if r.status_code != 200:
                errors.append(f"{bucket} GET HTTP {r.status_code}")
                continue
            xml = r.text
            xml = re.sub(r"<videoCodecType>[^<]*</videoCodecType>",
                         f"<videoCodecType>{codec}</videoCodecType>", xml)
            r = requests.put(f"http://{host}/ISAPI/Streaming/channels/{channel}{suffix}",
                             data=xml.encode("utf-8"),
                             headers={"Content-Type": "application/xml"},
                             auth=auth, timeout=timeout, verify=False)
            if r.status_code in (200, 201):
                applied.append(f"{bucket}={codec}")
            else:
                errors.append(f"{bucket} PUT HTTP {r.status_code}: {r.text[:120]}")
        except Exception as e:
            errors.append(f"{bucket}: {e}")
    return {"ok": not errors, "applied": applied, "errors": errors}


# ============================================================================
# ENCODE INFO — vendor dispatch
# ============================================================================
def get_encode_info(nvr: dict, channel: int, timeout: float = 10.0) -> dict:
    """{channel, main:{codec,resolution,fps,bitrate,mode}, sub:{...}}"""
    if vendor_of(nvr) == "hikvision":
        return _hik_get_encode(nvr, channel, timeout)
    return _dahua_get_encode(nvr, channel, timeout)


def _dahua_get_encode(nvr: dict, channel: int, timeout: float) -> dict:
    url = f"http://{nvr['host']}/cgi-bin/configManager.cgi?action=getConfig&name=Encode"
    out = {"channel": channel, "main": {}, "sub": {}}
    try:
        r = requests.get(url, auth=HTTPDigestAuth(nvr["user"], nvr["password"]),
                         timeout=timeout, verify=False)
        if r.status_code != 200:
            out["error"] = f"HTTP {r.status_code}"
            return out
    except Exception as e:
        out["error"] = str(e)
        return out
    ch = channel - 1
    map_ = {"Compression": "codec", "resolution": "resolution", "Resolution": "resolution",
            "FPS": "fps", "BitRate": "bitrate", "BitRateControl": "mode"}
    for line in r.text.splitlines():
        line = line.strip()
        for stream_key, bucket in (("MainFormat", "main"), ("ExtraFormat", "sub")):
            prefix = f"table.Encode[{ch}].{stream_key}[0]."
            if line.startswith(prefix):
                k_full, _, v = line[len(prefix):].partition("=")
                for suffix, short in map_.items():
                    if k_full.endswith(suffix):
                        out[bucket][short] = v
                        break
    return out


def _hik_get_encode(nvr: dict, channel: int, timeout: float) -> dict:
    """Hik: /ISAPI/Streaming/channels/{ch}01 (main) + {ch}02 (sub)."""
    auth = HTTPDigestAuth(nvr["user"], nvr["password"])
    out = {"channel": channel, "main": {}, "sub": {}}
    for suffix, bucket in (("01", "main"), ("02", "sub")):
        url = f"http://{nvr['host']}/ISAPI/Streaming/channels/{channel}{suffix}"
        try:
            r = requests.get(url, auth=auth, timeout=timeout, verify=False)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.text)
        except Exception:
            continue
        # Video element là child trực tiếp của StreamingChannel
        vid = _hfind(root, "Video")
        if vid is None:
            continue
        w = _htext(vid, "videoResolutionWidth")
        h = _htext(vid, "videoResolutionHeight")
        fps_raw = _htext(vid, "maxFrameRate")
        try:
            fps_int = int(fps_raw) // 100 if fps_raw else 0
        except ValueError:
            fps_int = 0
        out[bucket] = {
            "codec": _htext(vid, "videoCodecType"),
            "resolution": f"{w}x{h}" if w and h else "",
            "fps": str(fps_int) if fps_int else "",
            "bitrate": _htext(vid, "constantBitRate") or _htext(vid, "vbrUpperCap"),
            "mode": _htext(vid, "videoQualityControlType"),
        }
    return out

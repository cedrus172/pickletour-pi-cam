# PiTailscale — Dahua Camera Platform

Self-hosted platform chạy trên Raspberry Pi, cung cấp:

- **PickClip SPA** — khách tự cắt highlight từ camera Dahua NVR qua web
- **Admin recorder** — quản lý NVR, channel, chuyển codec, stream/clip
- **Monitor** — dashboard sức khoẻ Pi
- **Scanner + Tailscale forwarder** — tool nội bộ

Mỗi Pi expose 4 service (ports 9996–9999) qua Tailscale. Public endpoint (PickClip) qua Cloudflare Tunnel.

---

## Deployments hiện tại

| Pi | Tailscale IP | Public URL (PickClip) | LAN |
|----|--------------|-----------------------|-----|
| **lavo** | 100.126.99.23 | https://lavong.pickletour.vn | 192.168.1.0/24 |
| **cedrus** | 100.104.48.117 | https://quangtrung.pickletour.vn | 192.168.1.0/24 |
| **raspberrypi** | 100.76.29.104 | (chưa expose public) | 192.168.86.0/24 |

---

## Service ports (mỗi Pi)

| Port | Service | Purpose | Public? |
|------|---------|---------|---------|
| 9996 | **clipapp.py** | PickClip SPA + Public API cho khách | ✅ Cloudflare |
| 9997 | recorder.py | Admin: NVR/channel CRUD + codec editor + admin clip | ❌ Tailscale only |
| 9998 | monitor.py | Pi system monitor (CPU/RAM/disk/network/processes) | ❌ Tailscale only |
| 9999 | app.py | LAN scanner + socat Tailscale forwarder | ❌ Tailscale only |

---

## Flow làm việc

```
                       ┌─────────────────────────────────┐
                       │  Pickletour mobile app          │
                       │  (HTTP → PickClip public API)   │
                       └──────────────┬──────────────────┘
                                      │ HTTPS
                       ┌──────────────▼──────────────────┐
                       │  Cloudflare Tunnel (HTTP/2)     │
                       │  lavong.pickletour.vn           │
                       └──────────────┬──────────────────┘
                                      │
                       ┌──────────────▼──────────────────┐
                       │  Pi (Raspberry Pi 4/5)          │
                       │                                  │
                       │  :9996 clipapp.py               │
                       │       └─> rec_shared helpers    │
                       │       └─> SQLite recorder.db    │
                       │       └─> ffmpeg (subprocess)   │
                       └──────────────┬──────────────────┘
                                      │
                       ┌──────────────▼──────────────────┐
                       │  LAN: Dahua NVR/Camera          │
                       │   - RTSP (:554) stream/playback │
                       │   - HTTP (:80) CGI, loadfile    │
                       │   - SDK   (:37777) proprietary  │
                       └─────────────────────────────────┘
```

---

## Project structure

```
PiTailscale/
├── app.py              # Scanner + Tailscale forwarder (port 9999)
├── scanner.py          # nmap wrapper + fingerprint camera brands
├── forwarder.py        # socat subprocess manager
├── monitor.py          # psutil dashboard (port 9998)
├── recorder.py         # Admin recorder (port 9997)
├── clipapp.py          # Public PickClip SPA + API (port 9996)
├── rec_shared.py       # Shared helpers (DB, ffmpeg, Dahua loadfile)
├── templates/          # Jinja HTML
│   ├── clip.html       # PickClip SPA
│   ├── recorder.html   # Admin
│   └── monitor.html
├── static/             # CSS + JS
├── data/               # Runtime (SQLite + clips + streams + thumbs)
├── pitailscale.service          # systemd units (4 total)
├── pitailscale-monitor.service
├── pitailscale-recorder.service
├── pitailscale-clip.service
└── deploy.sh           # One-shot deploy (SSH + rsync + systemctl)
```

---

## Deploy

```bash
# Full stack to a Tailscale target
SSH_PASS='xxx' ./deploy.sh user@tailscale-ip 9999 9998 9997 9996
```

Script tự: cài apt deps (python3/socat/nmap/ffmpeg), rsync source, tạo venv, cài 4 systemd services. Idempotent — chạy lại sẽ update code + restart.

---

## Links

- **[API.md](API.md)** — toàn bộ endpoint cho mobile tích hợp
- **[MOBILE-INTEGRATION.md](MOBILE-INTEGRATION.md)** — guide code sample (iOS/Android/React Native)

---

© Pickletour · Công cụ được phát triển bởi [Pickletour.vn](https://pickletour.vn)

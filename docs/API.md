# PiTailscale Public API Reference

Dành cho **Pickletour mobile app** tích hợp. Base URL mỗi venue tương ứng với 1 Pi:

```
https://lavong.pickletour.vn       # Lã Vọng Pickleball
https://quangtrung.pickletour.vn   # Quang Trung Pickleball
```

**Protocol:** HTTPS (HTTP/2) · **Auth:** none (hiện tại public, xem [Security](#security))

Tất cả JSON unless noted. `Content-Type: application/json` cho POST.

---

## 1 · Channels (danh sách sân/camera)

### `GET /api/public/channels`

List các sân đã được admin cấu hình.

**Response 200:**
```json
[
  { "id": 1, "name": "Sân 1 - Trung tâm", "description": "Mái che, LED" },
  { "id": 2, "name": "Sân 2 - Ngoài trời", "description": "" },
  { "id": 3, "name": "Sân 3 - CLB",        "description": "" }
]
```

**Field spec:**

| Field | Type | Note |
|-------|------|------|
| `id` | int | ID bền (truyền vào mọi API khác) |
| `name` | string | Tên hiển thị (admin đặt) |
| `description` | string | Có thể rỗng |

Không bao giờ trả về credentials của NVR.

---

## 2 · Thumbnail (snapshot sân)

### `GET /api/public/channels/{id}/thumbnail`

Trả về 1 frame JPEG hiện tại của camera. Pi cache ~60s.

**Response:** `image/jpeg` (hoặc fallback `image/svg+xml` nếu camera không reach được).

```http
GET /api/public/channels/1/thumbnail HTTP/1.1
→ 200 OK
  Content-Type: image/jpeg
  Cache-Control: public, max-age=30
  <JPEG bytes>
```

Dùng trực tiếp làm `src` của `<Image>` component. Width 640px, aspect auto.

---

## 3 · Branding (title/tagline/footer)

### `GET /api/public/branding`

```json
{
  "title":      "PickClip",
  "tagline":    "Cắt highlight cam sân pickleball nhanh chóng",
  "footer":     "Công cụ được phát triển bởi Pickletour.vn",
  "footer_url": "https://pickletour.vn"
}
```

Admin có thể đổi 4 trường này qua UI (9997).

---

## 4 · Retention (thời gian giữ clip)

### `GET /api/public/retention`

```json
{ "ttl_secs": 10800, "ttl_human": "3 giờ" }
```

Clip tự xoá sau `ttl_secs` giây. App nên hiện warning *"Tải về máy trong N giờ"*.

---

## 5 · Live stream (HLS preview)

Live view qua **HLS fragmented MP4** (hls.js hoặc native video player trên iOS Safari).

### `POST /api/public/stream/start`

Start session ffmpeg transcode RTSP → HLS.

**Request body:**
```json
{
  "channel_id": 1,
  "mode": "live"
}
```

Hoặc playback với time range:
```json
{
  "channel_id": 1,
  "mode": "playback",
  "start": "2026-10-26 10:00:00",
  "end":   "2026-10-26 10:05:00"
}
```

**Response 200:**
```json
{
  "session_id": "a1b2c3d4e5f6",
  "manifest":   "/stream/a1b2c3d4e5f6/index.m3u8"
}
```

**Response 500** (NVR unreachable):
```json
{ "error": "stream failed", "log": "<ffmpeg stderr tail>" }
```

### `GET /stream/{session_id}/index.m3u8`

HLS manifest (fMP4). Pass full URL vào player.

### `GET /stream/{session_id}/init.mp4`
### `GET /stream/{session_id}/seg_NNNN.m4s`

Fragments. Player tự load, không cần gọi tay.

### `POST /api/public/stream/stop`

```json
{ "session_id": "a1b2c3d4e5f6" }
```

Kill ffmpeg + xoá tmp dir. **Luôn gọi** khi user rời khỏi màn hình để tiết kiệm CPU Pi. (Server tự dọn session idle sau 2 phút.)

---

## 6 · Create clip (cắt + lưu MP4)

### `POST /api/public/clip`

Giới hạn **tối đa 30 phút** mỗi clip (hard limit backend).

**Request — quick cut (last N phút):**
```json
{ "channel_id": 1, "quick_min": 5 }
```
`quick_min` phải thuộc {5, 10, 15, 30}.

**Request — custom time range:**
```json
{
  "channel_id": 1,
  "start": "2026-10-26 09:15:00",
  "end":   "2026-10-26 09:20:30"
}
```
Format accepted: `YYYY-MM-DD HH:MM:SS` hoặc `YYYY-MM-DDTHH:MM:SS` (ISO), hoặc không có giây `YYYY-MM-DD HH:MM`.

**Response 200:**
```json
{ "clip_id": "6912ca9ec0ad", "name": "Sân_1_last5min_20261026-091842.mp4" }
```

**Response 400:**
```json
{ "error": "Tối đa 30 phút mỗi clip" }
```

Pi chọn chiến lược tự động:
1. **Fast download** qua Dahua `/cgi-bin/loadfile.cgi` (download .dav gốc tốc độ mạng ~50 Mbps, remux sang MP4) — ~50x nhanh hơn RTSP
2. **Fallback RTSP playback** nếu loadfile fail — tốc độ ~1x realtime

---

## 7 · Poll clip status

### `GET /api/public/clips/{clip_id}`

Poll mỗi ~1-2 giây cho đến khi status != `running`.

**Response (running — fast mode):**
```json
{
  "id": "6912ca9ec0ad",
  "name": "Sân_1_last5min_20261026-091842.mp4",
  "status": "running",
  "phase": "fast_download",
  "downloaded": 15073280,
  "expected_sec": 300,
  "progress": { "out_time_sec": 0, "fps": 0, "speed": 0, "size": 0, "bitrate": "" },
  "size": 0,
  "created": 1792981122.55,
  "err": "",
  "download_url": ""
}
```

**Response (running — RTSP fallback):**
```json
{
  "status": "running",
  "phase": "rtsp_fallback",
  "progress": {
    "out_time_sec": 12.5,
    "fps": 25.0,
    "speed": 1.07,
    "bitrate": "1564.3kbits/s",
    "size": 5242880
  },
  "expected_sec": 300,
  ...
}
```

**Response (done):**
```json
{
  "status": "done",
  "phase": "done",
  "size": 37981546,
  "download_url": "/clips/6912ca9ec0ad",
  ...
}
```

**Response (error):**
```json
{
  "status": "error",
  "err": "fast: loadfile HTTP 404...\n\nrtsp: method DESCRIBE failed: 404",
  "download_url": ""
}
```

**Field spec:**

| Field | Type | Note |
|-------|------|------|
| `status` | enum | `running` / `done` / `error` |
| `phase` | enum | `init` / `fast_download` / `rtsp_fallback` / `done` |
| `downloaded` | int | Bytes đã download (chỉ fast mode) |
| `progress.out_time_sec` | float | Giây đã mux (chỉ RTSP mode) |
| `progress.speed` | float | Tốc độ ffmpeg (1.0 = realtime) |
| `expected_sec` | int | Tổng độ dài clip (giây) |
| `size` | int | Bytes của file MP4 cuối (chỉ khi done) |
| `download_url` | string | Path tải file (chỉ khi done) |

**Tính %:**
- Fast mode: `downloaded / (expected_sec × bitrate/8)` — ước lượng, nhảy tới 100% khi xong
- RTSP mode: `out_time_sec / expected_sec`

---

## 8 · Download clip

### `GET /clips/{clip_id}`

**Response:** `video/mp4` with `Content-Disposition: attachment; filename=...`.

Pi tự giải phóng file sau `retention.ttl_secs` (3 giờ mặc định). Sau đó endpoint trả **410 Gone**.

---

## 9 · HEAD check (verify còn file không)

### `HEAD /clips/{clip_id}`

- `200` — file sẵn sàng tải
- `404` — không tồn tại hoặc đã expire
- `410` — expired

Dùng cho local history view (user mở lại app sau 1 giờ, check xem clip cũ còn tải được không).

---

## Error format

Mọi error endpoint trả JSON:
```json
{ "error": "mô tả ngắn (tiếng Việt khi hướng tới user)" }
```

Status codes:
- `400` — input invalid (sai format, vượt 30 phút...)
- `404` — resource không tồn tại (channel_id/clip_id sai)
- `410` — clip đã expire
- `500` — ffmpeg/NVR fail (xem field `error` + `log`)

---

## Rate limits

**Hiện tại:** không có rate limit backend. Mỗi Pi có thể handle ~5-10 concurrent HLS sessions + 2-3 clip concurrent (giới hạn CPU).

**Khuyến nghị client:** không spam POST /api/public/clip (mỗi clip tạo ffmpeg process ~1-5s CPU). Debounce user clicks ít nhất 2s.

---

## Security

**Hiện tại public endpoints không yêu cầu auth.** Để tích hợp vào mobile app Pickletour, bạn có các lựa chọn:

### Phương án A — Giữ public (dễ nhất)

- Mobile gọi API trực tiếp qua base URL.
- Rủi ro: ai biết URL cũng cắt được clip → có thể bị spam. Clip auto-xoá sau 3h nên disk không đầy lâu dài.

### Phương án B — Cloudflare Access (khuyến nghị)

Bật Zero Trust Access ở Cloudflare dashboard:

- **Service token** cho mobile app (header `CF-Access-Client-Id` + `CF-Access-Client-Secret`).
- Pickletour backend giữ secret, mobile app gọi backend → backend proxy sang Pi.
- Hoặc giữ secret trong app sau khi obfuscate (ít an toàn).

### Phương án C — JWT từ Pickletour backend

Thêm 1 header `X-Pickletour-Token` → clipapp.py verify JWT từ backend trung tâm. Cần code thêm phía backend + Pi.

Mobile app PickClip integration mẫu: xem **[MOBILE-INTEGRATION.md](MOBILE-INTEGRATION.md)**.

---

## Admin APIs (reference only, chỉ Tailscale)

Các endpoint sau chạy trên **port 9997** của Pi, chỉ accessible qua Tailscale. Mobile app **không** gọi được. Dùng khi tích hợp admin tool vào dashboard Pickletour backend.

### NVR management

- `GET /api/nvrs` — list NVRs (password che `***`)
- `POST /api/nvrs` — add NVR `{name, host, rtsp_port, user, password, channels}`
- `DELETE /api/nvrs/{id}` — xoá NVR
- `POST /api/nvrs/{id}/test?channel=1` — ping test
- `GET /api/nvrs/{id}/encode?channel=1` — lấy codec/resolution/fps/bitrate hiện tại
- `PUT /api/nvrs/{id}/encode` — đổi codec `{channel, main: "H.264"|"H.265", sub: ...}`

### Channel management

- `GET /api/channels` — list channels (kèm NVR info)
- `POST /api/channels` — add `{nvr_id, channel, name, description, subtype}`
- `PATCH /api/channels/{id}` — update field
- `DELETE /api/channels/{id}` — xoá

### Branding settings

- `GET /api/settings` — lấy brand_title/tagline/footer
- `PUT /api/settings` — update `{brand_title, brand_tagline, ...}`

---

© Pickletour · Công cụ được phát triển bởi [Pickletour.vn](https://pickletour.vn)

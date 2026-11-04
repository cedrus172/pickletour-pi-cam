# Mobile Integration Guide

Hướng dẫn tích hợp PickClip vào app Pickletour (iOS/Android/React Native/Flutter).

Full API spec: **[API.md](API.md)**

---

## Flow khuyến nghị trong mobile app

```
┌─ 1. Chọn sân Pickleball ──────────────┐
│  Pickletour backend trả về danh sách  │
│  venue, mỗi venue có `pi_base_url`.   │
│  Ví dụ: https://lavong.pickletour.vn  │
└───────────────┬───────────────────────┘
                │
┌───────────────▼───────────────────────┐
│ 2. Màn hình "Chọn camera":            │
│    GET {pi_base_url}/api/public/      │
│        channels                       │
│    Hiển thị grid thumbnail            │
└───────────────┬───────────────────────┘
                │ (user tap camera)
┌───────────────▼───────────────────────┐
│ 3. Màn hình "Cam detail + Cắt":       │
│    POST /api/public/stream/start      │
│         → session_id + manifest       │
│    Player load HLS manifest           │
│    User chọn 5/10/15/30 phút hoặc    │
│    custom time                        │
└───────────────┬───────────────────────┘
                │ (user tap "Cắt")
┌───────────────▼───────────────────────┐
│ 4. POST /api/public/stream/stop       │
│    POST /api/public/clip              │
│         → clip_id                     │
│    Poll GET /api/public/clips/{id}    │
│         hiển thị progress             │
└───────────────┬───────────────────────┘
                │ (status = done)
┌───────────────▼───────────────────────┐
│ 5. GET /clips/{clip_id}               │
│    Download MP4 vào Photos/Files      │
│    Hoặc share lên Zalo/FB/TikTok      │
│    Lưu clip_id vào local history      │
└───────────────────────────────────────┘
```

---

## 1 · Load channels (danh sách sân)

```typescript
// React Native
const res = await fetch(`${baseUrl}/api/public/channels`);
const channels = await res.json();
// [{ id, name, description }]
```

```swift
// iOS Swift
struct Channel: Codable {
  let id: Int
  let name: String
  let description: String
}

func loadChannels() async throws -> [Channel] {
  let url = URL(string: "\(baseUrl)/api/public/channels")!
  let (data, _) = try await URLSession.shared.data(from: url)
  return try JSONDecoder().decode([Channel].self, from: data)
}
```

```kotlin
// Android Kotlin (Retrofit)
interface PickClipApi {
  @GET("api/public/channels")
  suspend fun getChannels(): List<Channel>
}
data class Channel(val id: Int, val name: String, val description: String)
```

---

## 2 · Thumbnail

Dùng trực tiếp URL làm image source. Server trả JPEG đã cache 60s.

```tsx
// React Native
<Image
  source={{ uri: `${baseUrl}/api/public/channels/${ch.id}/thumbnail` }}
  style={{ width: '100%', aspectRatio: 16/10 }}
/>
```

```swift
// SwiftUI
AsyncImage(url: URL(string: "\(baseUrl)/api/public/channels/\(ch.id)/thumbnail"))
```

---

## 3 · Live preview (HLS)

### iOS — native AVPlayer (support HLS native)
```swift
let startRes = try await startStream(channelId: ch.id, mode: "live")
let manifestURL = URL(string: "\(baseUrl)\(startRes.manifest)")!
let player = AVPlayer(url: manifestURL)
```

### Android — ExoPlayer
```kotlin
val mediaSource = HlsMediaSource.Factory(dataSourceFactory)
  .createMediaSource(MediaItem.fromUri("$baseUrl${startRes.manifest}"))
player.setMediaSource(mediaSource)
player.prepare()
```

### React Native — react-native-video
```tsx
<Video
  source={{ uri: `${baseUrl}${startRes.manifest}`, type: 'm3u8' }}
  controls
  style={{ aspectRatio: 16/9 }}
/>
```

### Flutter — video_player (iOS) hoặc flutter_vlc_player
```dart
final controller = VideoPlayerController.networkUrl(
  Uri.parse('$baseUrl${startRes.manifest}'),
);
```

**Luôn gọi `/api/public/stream/stop`** khi màn hình unmount để tiết kiệm Pi.

---

## 4 · Tạo clip + poll progress

### Full flow TypeScript (React Native)

```typescript
async function createAndWaitClip(baseUrl: string, channelId: number, quickMin: number) {
  // 1. Create clip (trả về ngay)
  const createRes = await fetch(`${baseUrl}/api/public/clip`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ channel_id: channelId, quick_min: quickMin }),
  });
  if (!createRes.ok) throw new Error((await createRes.json()).error);
  const { clip_id, name } = await createRes.json();

  // 2. Poll status mỗi 1.5s cho đến khi done/error
  while (true) {
    await new Promise(r => setTimeout(r, 1500));
    const sRes = await fetch(`${baseUrl}/api/public/clips/${clip_id}`);
    const s = await sRes.json();

    if (s.status === 'done') {
      return {
        name,
        size: s.size,
        downloadUrl: `${baseUrl}${s.download_url}`,
      };
    }
    if (s.status === 'error') {
      throw new Error(s.err || 'ffmpeg failed');
    }
    // Still running — update UI
    onProgress({
      phase: s.phase,
      downloaded: s.downloaded,
      expectedSec: s.expected_sec,
      progress: s.progress,
    });
  }
}
```

### Custom time range

```typescript
const body = {
  channel_id: 1,
  start: '2026-10-26 09:15:00',  // "YYYY-MM-DD HH:MM:SS"
  end:   '2026-10-26 09:20:00',
};
// giới hạn 30 phút tối đa
```

### Progress calculation

```typescript
function computeProgress(s) {
  if (s.phase === 'fast_download') {
    // Ước lượng: expected_bitrate ~1 Mbps → bytes
    const expectedBytes = s.expected_sec * 128 * 1024;
    return Math.min(99, (s.downloaded / expectedBytes) * 100);
  }
  if (s.phase === 'rtsp_fallback') {
    return s.expected_sec > 0
      ? (s.progress.out_time_sec / s.expected_sec) * 100
      : 0;
  }
  return 0;
}
```

---

## 5 · Download MP4

### iOS — tải vào Photos
```swift
let url = URL(string: "\(baseUrl)/clips/\(clipId)")!
let (localURL, _) = try await URLSession.shared.download(from: url)
// Save to Photos
PHPhotoLibrary.shared().performChanges {
  PHAssetChangeRequest.creationRequestForAssetFromVideo(atFileURL: localURL)
}
```

### Android — DownloadManager
```kotlin
val req = DownloadManager.Request(Uri.parse("$baseUrl/clips/$clipId"))
  .setDestinationInExternalPublicDir(Environment.DIRECTORY_MOVIES, "$name")
  .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
downloadManager.enqueue(req)
```

### React Native — react-native-blob-util
```typescript
const task = ReactNativeBlobUtil
  .config({ path: `${docDir}/${name}`, fileCache: true, addAndroidDownloads: {...} })
  .fetch('GET', `${baseUrl}/clips/${clipId}`);
```

---

## 6 · Share clip

### iOS — UIActivityViewController
```swift
let items = [URL(string: "\(baseUrl)/clips/\(clipId)")!]
let vc = UIActivityViewController(activityItems: items, applicationActivities: nil)
present(vc, animated: true)
```

### Android — Intent chooser
```kotlin
val intent = Intent(Intent.ACTION_SEND).apply {
  type = "video/mp4"
  putExtra(Intent.EXTRA_STREAM, Uri.parse("$baseUrl/clips/$clipId"))
}
startActivity(Intent.createChooser(intent, "Chia sẻ highlight"))
```

Deep links để pre-fill Zalo/FB:
```
Zalo:     https://zalo.me/share?url={encodedUrl}
Facebook: https://www.facebook.com/sharer/sharer.php?u={encodedUrl}
```

---

## 7 · Local history tracking

Clip tự xoá sau 3 giờ. Để user xem lại lịch sử của chính mình (trên device):

```typescript
// Save khi clip done
await AsyncStorage.setItem('pickclip_history', JSON.stringify([
  ...prev,
  { clipId, name, createdAt: Date.now(), venue, size, downloadUrl }
]));

// Check còn tồn tại không trước khi hiển thị
async function isAlive(entry) {
  if (Date.now() - entry.createdAt > 3 * 3600 * 1000) return false;
  const r = await fetch(`${baseUrl}/clips/${entry.clipId}`, { method: 'HEAD' });
  return r.ok;
}
```

---

## 8 · Branding

Load branding của mỗi venue để thay logo/tagline UI:

```typescript
const brand = await fetch(`${baseUrl}/api/public/branding`).then(r => r.json());
// { title, tagline, footer, footer_url }
```

Mobile app có thể override hoàn toàn — hoặc dùng branding mặc định của Pickletour.

---

## 9 · Error handling

Tất cả error trả JSON `{ "error": "mô tả" }`. Code:

| HTTP | Khi nào | UI nên hiện |
|------|---------|-------------|
| 400 | Input invalid, vượt 30 phút | Toast đỏ "Nhập thời gian hợp lệ, tối đa 30 phút" |
| 404 | Channel/clip không tồn tại | "Sân không tồn tại" / "Clip đã bị xoá" |
| 410 | Clip expired | "Clip đã quá 3 giờ, hãy cắt lại" |
| 500 | NVR fail / ffmpeg fail | "NVR tạm thời không phản hồi, thử lại sau" |
| 502 | Tunnel down | "Mất kết nối với sân, thử lại" |

Luôn wrap trong try/catch, không expose raw error cho user:

```typescript
try {
  const { downloadUrl } = await createAndWaitClip(baseUrl, 1, 5);
} catch (e) {
  Alert.alert('Có lỗi', 'Vui lòng thử lại sau');
  logToBackend(e); // debug
}
```

---

## 10 · Venue selection (gắn với Pickletour backend)

Pickletour backend lưu danh sách venue → mỗi venue 1 Pi base URL:

```json
POST https://api.pickletour.vn/venues/nearby?lat=10.8&lon=106.7
→
[
  {
    "venue_id": "lavong-hcm",
    "name": "Pickleball Lã Vọng - Q7",
    "pi_base_url": "https://lavong.pickletour.vn",
    "address": "...",
    "logo_url": "..."
  },
  {
    "venue_id": "quangtrung-hn",
    "name": "Pickleball Quang Trung - Hà Nội",
    "pi_base_url": "https://quangtrung.pickletour.vn",
    ...
  }
]
```

Mobile app chọn venue → cache `pi_base_url` → load channels của venue đó.

---

## 11 · Rate limiting + UX

- **Debounce** nút Cắt ít nhất 2 giây để tránh spam.
- **Pre-check retention** trước khi hiển thị lịch sử, ẩn clip expired.
- **Luôn kill stream session** khi màn hình background hoặc user navigate away.
- **Timeout polling** sau 20 phút nếu clip chưa done → coi là lỗi.
- **Hiển thị progress thực** từ API, không fake animation (gây hiểu lầm).

---

## 12 · Checklist tích hợp

- [ ] Thêm `pi_base_url` vào venue model backend
- [ ] Load branding + channels khi vào màn Chọn sân
- [ ] Thumbnail grid với placeholder loading
- [ ] HLS player live preview
- [ ] Quick cut chips 5/10/15/30 phút
- [ ] Custom datetime picker (hard limit 30 phút)
- [ ] Progress bar với phase + bytes/speed
- [ ] Download vào Photos (iOS) / Movies (Android)
- [ ] Share sheet
- [ ] Local history (AsyncStorage/UserDefaults/SharedPreferences)
- [ ] Error toast với wording tiếng Việt
- [ ] Analytics event: clip created, clip downloaded, clip shared

---

© Pickletour · Công cụ được phát triển bởi [Pickletour.vn](https://pickletour.vn)

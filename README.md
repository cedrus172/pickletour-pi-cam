# PiTailscale

Web UI chạy trên 1 Pi (hoặc máy Linux bất kỳ) có Tailscale, cho phép:

1. **Scan LAN** tìm IP camera và DVR/NVR (Hikvision, Dahua, Reolink, ONVIF generic...).
2. **Thử credentials** (HTTP Basic / Digest) để xác định camera đang hoạt động.
3. **Forward** port LAN của camera ra Tailscale IP, để bất kỳ máy nào trong tailnet đều truy cập được từ xa — không cần mở port router, không cần public IP.

## Deploy

Trên máy có SSH tới server đích (và máy đích đã join Tailscale):

```bash
# Deploy mặc định tới admin@100.126.99.23:9999
SSH_PASS=xxxx ./deploy.sh

# Hoặc chỉ định target khác
SSH_PASS=xxxx ./deploy.sh user@host 9999
```

Script sẽ:

- Cài `python3`, `socat`, `nmap` qua apt
- Rsync source tới `/opt/pitailscale`
- Tạo Python venv + cài deps
- Cài & start systemd unit `pitailscale.service`

Sau đó mở `http://<tailscale-ip>:9999` từ máy nào cũng trong tailnet.

## Cách dùng

1. **Scan**: nhập subnet (hoặc để auto) → click **Scan**. Mất 30s~2 phút tuỳ subnet.
2. **Credentials**: nhập user/pass → click **Probe tất cả camera đã scan** để xác nhận.
3. **Forward**: click vào chip port muốn forward (ví dụ `80` hoặc `554 · RTSP`) → modal mở → confirm → PiTailscale allocate 1 port ngẫu nhiên trong `10000-19999` trên Tailscale IP, khởi động `socat`, lưu DB.

Từ máy khác trong tailnet, truy cập camera như:
- HTTP admin: `http://100.126.99.23:10001/`
- RTSP stream: `rtsp://100.126.99.23:10002/<stream-path>`

## Kiến trúc

```
Browser ─── http://<ts-ip>:9999 ───→  Flask app (app.py)
                                        ├── /api/scan     → nmap wrapper
                                        ├── /api/probe    → requests + Digest/Basic auth
                                        └── /api/forwards → spawns `socat` processes
                                                            bound to Tailscale interface

Camera (192.168.1.X) ←── socat TCP forward ──── Tailscale IP:random-port
```

## Troubleshooting

```bash
# Log
ssh admin@100.126.99.23 'sudo journalctl -u pitailscale -f'

# Trạng thái socat đang chạy
ssh admin@100.126.99.23 'pgrep -af socat'

# Reset forwards (xoá DB)
ssh admin@100.126.99.23 'sudo systemctl stop pitailscale && sudo rm /opt/pitailscale/data/forwards.db && sudo systemctl start pitailscale'
```

## Security

- `socat` listener bind cụ thể vào Tailscale IP (`100.x.x.x`) → chỉ máy trong tailnet với route tới IP đó mới chạm được. LAN khác và Internet không thấy.
- Web UI (port 9999) hiện bind `0.0.0.0` để tiện access qua cả LAN; muốn chỉ expose qua Tailscale, sửa `app.py --host` trong systemd unit thành Tailscale IP.
- Credentials nhập trong UI chỉ dùng để probe, **không được lưu**.

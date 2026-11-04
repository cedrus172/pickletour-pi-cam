# PiTailscale — Windows

Chạy UI `:9999` scan + forward camera LAN qua Tailscale trên 1 PC Windows,
thay cho Raspberry Pi. Auto-start khi máy boot (Task Scheduler as SYSTEM).

## Khác bản Pi

| Phần | Pi (Linux) | Windows |
|---|---|---|
| Port forward backend | `socat` process | Python thread listener |
| Scan port local | `ss -tulnp` | `psutil.net_connections()` |
| Tìm subnet | `ip -4 -o addr` | `psutil.net_if_addrs()` |
| Auto-start | systemd unit | Task Scheduler ONSTART |
| Service user | `admin` | `SYSTEM` |

Không còn phụ thuộc `socat`. Forwarder chạy trong cùng process Python → khi
`app.py` dừng, mọi forward dừng theo; `restore_all()` ở lúc khởi động đọc
DB và spawn lại từng listener.

## Yêu cầu

- Windows 10/11
- Python 3.10+ (`winget install -e --id Python.Python.3.12`)
- Nmap (`winget install -e --id Insecure.Nmap`) — installer sẽ tự cài
- Tailscale desktop (`winget install -e --id Tailscale.Tailscale`), đã đăng nhập

## Cài

1. Copy cả folder `win\` sang PC Windows (ví dụ `C:\Users\admin\Desktop\PiTailscale-win\`)
2. Chuột phải `install.ps1` → **Run with PowerShell**, chọn **Yes** để nâng quyền admin
3. Nếu PS block, mở PowerShell admin, chạy:
   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass -Force
   cd C:\Users\admin\Desktop\PiTailscale-win
   .\install.ps1
   ```
4. Khi xong, mở trình duyệt → `http://localhost:9999`
5. Từ máy khác qua Tailscale → `http://<ts-ip>:9999`

## Vận hành

```powershell
# Dừng
schtasks /End /TN PiTailscale

# Chạy lại
schtasks /Run /TN PiTailscale

# Chạy tay (debug, có console log)
C:\PiTailscale\.venv\Scripts\python.exe C:\PiTailscale\app.py

# Xem log Flask (tự Flask không log ra file — chạy tay để xem)
# Hoặc redirect:
C:\PiTailscale\.venv\Scripts\python.exe C:\PiTailscale\app.py >> C:\PiTailscale\data\app.log 2>&1
```

## Gỡ

Chuột phải `uninstall.ps1` → Run with PowerShell (admin).

## Ghi chú limit

- UDP forwarder hiện tại chỉ nhớ **client cuối cùng** (đủ cho 1 viewer RTP).
  Multi-client đồng thời UDP cần phiên bản session-table — chưa cần trong
  use case camera.
- Task Scheduler chạy SYSTEM user → Tailscale phải cài sẵn và đã login
  (daemon chạy ở SYSTEM scope). Nếu Tailscale cài theo user → đổi install.ps1
  `/RU "SYSTEM"` → `/RU "<user>" /IT` (interactive, chỉ chạy khi user login).
- Firewall Windows đã mở TCP 9999 inbound; nếu muốn từ LAN (ngoài Tailscale)
  cũng truy cập được thì không cần sửa gì thêm. Không mở thì vẫn bind được
  localhost + Tailscale IP.

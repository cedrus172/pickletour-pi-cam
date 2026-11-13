#!/usr/bin/env bash
# Cài PiTailscale Hub trên VPS (Ubuntu/Debian).
# Chạy TRỰC TIẾP trên VPS với quyền root:
#   curl -fsSL <raw-url>/setup-vps.sh | bash
# hoặc: git clone … && cd … && sudo bash setup-vps.sh
#
# Dựng 3 service:
#   pitailscale-recorder (9997) — admin NVR cho sân DDNS
#   pitailscale-clip     (9996) — clip khách (link /san/<slug>)
#   pitailscale-hub      (9995) — dashboard tất cả sân
set -euo pipefail

REPO="${REPO:-https://github.com/cedrus172/pickletour-pi-cam.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/pitailscale}"
RUN_USER="${RUN_USER:-pitail}"
HUB_PORT="${HUB_PORT:-9995}"
RECORDER_PORT="${RECORDER_PORT:-9997}"
CLIP_PORT="${CLIP_PORT:-9996}"
# Base URL cho link Admin/Clip (để trống = dùng http://<ip>:<port>)
ADMIN_BASE="${ADMIN_BASE:-}"
CLIP_BASE="${CLIP_BASE:-}"

say(){ printf "\033[1;36m» %s\033[0m\n" "$*"; }
ok(){ printf "\033[1;32m✓ %s\033[0m\n" "$*"; }

[ "$(id -u)" = "0" ] || { echo "Cần chạy bằng root (sudo)"; exit 1; }

say "Cài gói hệ thống (python, ffmpeg, git)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ffmpeg git sqlite3 curl >/dev/null
ok "Gói hệ thống xong"

say "Tạo user chạy service: $RUN_USER"
id "$RUN_USER" &>/dev/null || useradd -r -s /usr/sbin/nologin -d "$INSTALL_DIR" "$RUN_USER"

say "Clone/cập nhật mã nguồn → $INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" fetch --depth=1 origin main
  git -C "$INSTALL_DIR" reset --hard origin/main
else
  rm -rf "$INSTALL_DIR"
  git clone --depth=1 "$REPO" "$INSTALL_DIR"
fi

say "Tạo build_info.py (commit hiện tại)…"
SHA=$(git -C "$INSTALL_DIR" rev-parse --short HEAD)
echo "BUILD_NUMBER = 'vps-$SHA'" > "$INSTALL_DIR/build_info.py"

say "Tạo virtualenv + cài thư viện Python…"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet flask requests
ok "Python deps xong"

mkdir -p "$INSTALL_DIR/data"
chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR"

install_unit(){ # $1 = service basename, $2 = extra sed
  local svc="$1"
  sed -e "s|__USER__|$RUN_USER|g" \
      -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
      -e "s|__HUB_PORT__|$HUB_PORT|g" \
      -e "s|__RECORDER_PORT__|$RECORDER_PORT|g" \
      -e "s|__CLIP_PORT__|$CLIP_PORT|g" \
      -e "s|__ADMIN_BASE__|$ADMIN_BASE|g" \
      -e "s|__CLIP_BASE__|$CLIP_BASE|g" \
      "$INSTALL_DIR/$svc.service" > "/etc/systemd/system/$svc.service"
}

say "Cài systemd service…"
install_unit pitailscale-recorder
install_unit pitailscale-clip
install_unit pitailscale-hub
systemctl daemon-reload
systemctl enable --now pitailscale-recorder pitailscale-clip pitailscale-hub
ok "3 service đã enable + start"

# Firewall (ufw nếu có)
if command -v ufw >/dev/null; then
  say "Mở firewall $HUB_PORT/$RECORDER_PORT/$CLIP_PORT…"
  ufw allow "$HUB_PORT"/tcp >/dev/null 2>&1 || true
  ufw allow "$RECORDER_PORT"/tcp >/dev/null 2>&1 || true
  ufw allow "$CLIP_PORT"/tcp >/dev/null 2>&1 || true
fi

IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo
ok "HOÀN TẤT"
echo "  Hub      : http://$IP:$HUB_PORT/"
echo "  Recorder : http://$IP:$RECORDER_PORT/   (thêm NVR DDNS ở đây)"
echo "  Clip     : http://$IP:$CLIP_PORT/san/<slug>"
echo
echo "  Cập nhật sau này: sudo bash $INSTALL_DIR/setup-vps.sh"
echo "  Xem log: journalctl -u pitailscale-hub -f"

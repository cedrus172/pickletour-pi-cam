#!/usr/bin/env bash
# PiTailscale deploy script.
# Usage:
#   ./deploy.sh [user@host] [port]
#   ./deploy.sh admin@100.126.99.23 9999
# Env:
#   SSH_PASS=xxx  — if set, uses sshpass (install: brew install sshpass / apt install sshpass)
#   INSTALL_DIR=/opt/pitailscale  — remote install path
set -euo pipefail

TARGET="${1:-admin@100.126.99.23}"
PORT="${2:-9999}"
MONITOR_PORT="${3:-9998}"
RECORDER_PORT="${4:-9997}"
CLIP_PORT="${5:-9996}"
INSTALL_DIR="${INSTALL_DIR:-/opt/pitailscale}"

HERE="$(cd "$(dirname "$0")" && pwd)"
HOST="${TARGET#*@}"
REMOTE_USER="${TARGET%@*}"

say() { printf "\033[1;36m» %s\033[0m\n" "$*"; }
ok()  { printf "\033[1;32m✓ %s\033[0m\n" "$*"; }
err() { printf "\033[1;31m✗ %s\033[0m\n" "$*" >&2; }

# SSH multiplexing — reuse one TCP connection for all ssh/rsync calls so
# password-only servers with low MaxAuthTries don't rate-limit us.
CTRL_DIR="${HOME}/.ssh/pt-ctl"
mkdir -p "$CTRL_DIR"
CTRL_PATH="$CTRL_DIR/%C"
SSH_COMMON="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 \
            -o ControlMaster=auto -o ControlPath=$CTRL_PATH -o ControlPersist=60"

trap 'ssh -O exit -o ControlPath=$CTRL_PATH "$TARGET" 2>/dev/null; true' EXIT

ssh_cmd() {
  if [[ -n "${SSH_PASS:-}" ]]; then
    sshpass -p "$SSH_PASS" ssh $SSH_COMMON "$TARGET" "$@"
  else
    ssh $SSH_COMMON "$TARGET" "$@"
  fi
}

rsync_cmd() {
  local rsh="ssh $SSH_COMMON"
  if [[ -n "${SSH_PASS:-}" ]]; then
    rsh="sshpass -p $SSH_PASS ssh $SSH_COMMON"
  fi
  rsync -az --delete \
    --exclude '__pycache__' --exclude '*.pyc' --exclude '.venv' \
    --exclude 'data' --exclude '.git' \
    -e "$rsh" \
    "$@"
}

say "Target: $TARGET (install at $INSTALL_DIR, app:$PORT monitor:$MONITOR_PORT recorder:$RECORDER_PORT clip:$CLIP_PORT)"

say "1/5 testing SSH..."
ssh_cmd 'echo "$(hostname) $(uname -m) $(lsb_release -ds 2>/dev/null || cat /etc/os-release | head -1)"' \
  || { err "SSH failed"; exit 1; }
ok "SSH ok"

say "2/5 ensuring remote system packages + sudoers rule..."
ssh_cmd "sudo -n true 2>/dev/null || { echo 'need sudo'; exit 1; }
         sudo apt-get update -q
         sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
             python3 python3-venv python3-pip socat nmap iproute2 ffmpeg \
             fonts-dejavu-core
         # Allow pitailscale to query listening sockets (needs root for process info)
         echo \"$REMOTE_USER ALL=(root) NOPASSWD: /usr/bin/ss\" | \
             sudo tee /etc/sudoers.d/pitailscale >/dev/null
         sudo chmod 440 /etc/sudoers.d/pitailscale"
ok "system deps installed + sudoers set"

say "3/5 creating install dir + syncing files..."
ssh_cmd "sudo mkdir -p $INSTALL_DIR/data \
         && sudo chown -R $REMOTE_USER:$REMOTE_USER $INSTALL_DIR"
rsync_cmd \
  "$HERE/app.py" "$HERE/scanner.py" "$HERE/forwarder.py" \
  "$HERE/monitor.py" "$HERE/recorder.py" "$HERE/clipapp.py" "$HERE/rec_shared.py" \
  "$HERE/requirements.txt" \
  "$HERE/pitailscale.service" "$HERE/pitailscale-monitor.service" \
  "$HERE/pitailscale-recorder.service" "$HERE/pitailscale-clip.service" \
  "$HERE/templates" "$HERE/static" \
  "$TARGET:$INSTALL_DIR/"
ok "files synced"

say "4/5 setting up venv + Python deps..."
ssh_cmd "cd $INSTALL_DIR \
         && [ -d .venv ] || python3 -m venv .venv \
         && .venv/bin/pip install -q --upgrade pip \
         && .venv/bin/pip install -q -r requirements.txt"
ok "venv ready"

say "5/5 installing & starting systemd services..."
ssh_cmd "sed -e 's|__USER__|$REMOTE_USER|g' \
             -e 's|__INSTALL_DIR__|$INSTALL_DIR|g' \
             -e 's|__PORT__|$PORT|g' \
             $INSTALL_DIR/pitailscale.service \
             | sudo tee /etc/systemd/system/pitailscale.service >/dev/null
         sed -e 's|__USER__|$REMOTE_USER|g' \
             -e 's|__INSTALL_DIR__|$INSTALL_DIR|g' \
             -e 's|__MONITOR_PORT__|$MONITOR_PORT|g' \
             $INSTALL_DIR/pitailscale-monitor.service \
             | sudo tee /etc/systemd/system/pitailscale-monitor.service >/dev/null
         sed -e 's|__USER__|$REMOTE_USER|g' \
             -e 's|__INSTALL_DIR__|$INSTALL_DIR|g' \
             -e 's|__RECORDER_PORT__|$RECORDER_PORT|g' \
             $INSTALL_DIR/pitailscale-recorder.service \
             | sudo tee /etc/systemd/system/pitailscale-recorder.service >/dev/null
         sed -e 's|__USER__|$REMOTE_USER|g' \
             -e 's|__INSTALL_DIR__|$INSTALL_DIR|g' \
             -e 's|__CLIP_PORT__|$CLIP_PORT|g' \
             $INSTALL_DIR/pitailscale-clip.service \
             | sudo tee /etc/systemd/system/pitailscale-clip.service >/dev/null
         sudo systemctl daemon-reload
         sudo systemctl enable pitailscale pitailscale-monitor pitailscale-recorder pitailscale-clip
         sudo systemctl restart pitailscale pitailscale-monitor pitailscale-recorder pitailscale-clip
         sleep 2
         for s in pitailscale pitailscale-monitor pitailscale-recorder pitailscale-clip; do
           if sudo systemctl is-active --quiet \$s; then
             echo \"\$s: ACTIVE\"
           else
             echo \"\$s: FAILED\"
             sudo journalctl -u \$s -n 15 --no-pager
             exit 1
           fi
         done"
ok "services running"

TS_IP=$(ssh_cmd "tailscale ip -4 2>/dev/null | head -1" || true)
TS_IP="${TS_IP:-$HOST}"

echo
ok "Deploy xong!"
echo
echo "  App UI       (Tailscale): http://$TS_IP:$PORT"
echo "  Monitor UI   (Tailscale): http://$TS_IP:$MONITOR_PORT"
echo "  Recorder UI  (Tailscale): http://$TS_IP:$RECORDER_PORT       (admin)"
echo "  PickClip SPA (Tailscale): http://$TS_IP:$CLIP_PORT       (customer)"
echo
echo "  Logs:    ssh $TARGET 'sudo journalctl -u pitailscale{,-monitor,-recorder,-clip} -f'"
echo "  Restart: ssh $TARGET 'sudo systemctl restart pitailscale{,-monitor,-recorder,-clip}'"
echo

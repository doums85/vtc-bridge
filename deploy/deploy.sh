#!/usr/bin/env bash
# Deploie le CODE sur le VPS. Ne touche jamais aux secrets, aux sessions Telegram
# ni a la base state/ : ces fichiers vivent uniquement sur le serveur apres first-sync.sh.
#   usage : VPS=root@1.2.3.4 ./deploy/deploy.sh
set -euo pipefail

VPS="${VPS:?definir VPS, ex: VPS=root@1.2.3.4}"
APP_DIR=/opt/vtc-bridge
APP_USER=vtc
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== envoi du code vers $VPS:$APP_DIR =="
rsync -az --delete \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '.git/' \
  --exclude 'logs/' \
  --exclude 'state/' \
  --exclude '.env' \
  --exclude '*.session' \
  --exclude '*.session-journal' \
  "$SRC/" "$VPS:$APP_DIR/"

ssh "$VPS" bash -euo pipefail <<REMOTE
chown -R $APP_USER:$APP_USER $APP_DIR
sudo -u $APP_USER $APP_DIR/.venv/bin/pip install --quiet --upgrade -r $APP_DIR/requirements.txt

install -m 644 $APP_DIR/deploy/vtc-bridge.service /etc/systemd/system/vtc-bridge.service
install -m 644 $APP_DIR/deploy/vtc-backup.service /etc/systemd/system/vtc-backup.service
install -m 644 $APP_DIR/deploy/vtc-backup.timer /etc/systemd/system/vtc-backup.timer
systemctl daemon-reload
systemctl enable vtc-bridge
systemctl enable --now vtc-backup.timer
systemctl restart vtc-bridge
sleep 3
systemctl is-active --quiet vtc-bridge && echo "vtc-bridge actif" || { echo "ECHEC au demarrage :"; journalctl -u vtc-bridge -n 40 --no-pager; exit 1; }
REMOTE

echo
echo "Logs en direct : ssh $VPS journalctl -u vtc-bridge -f"

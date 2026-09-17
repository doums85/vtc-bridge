#!/usr/bin/env bash
# A executer UNE FOIS sur le VPS Hetzner, en root.
#   scp deploy/server-setup.sh root@VPS:/tmp/ && ssh root@VPS bash /tmp/server-setup.sh
set -euo pipefail

APP_DIR=/opt/vtc-bridge
APP_USER=vtc

echo "== paquets =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-dev build-essential rsync tzdata

timedatectl set-timezone Europe/Paris

echo "== utilisateur $APP_USER =="
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/"$APP_USER" --shell /usr/sbin/nologin "$APP_USER"

echo "== arborescence $APP_DIR =="
mkdir -p "$APP_DIR"/state
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 750 "$APP_DIR"

echo "== venv =="
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip

echo "== journald : plafond disque =="
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/vtc-bridge.conf <<'CONF'
[Journal]
SystemMaxUse=500M
MaxRetentionSec=1month
CONF
systemctl restart systemd-journald

echo "== pare-feu : sortant libre, entrant SSH seul =="
if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH
  ufw --force enable
fi

echo
echo "OK. Suite : depuis le Mac, lancer deploy/first-sync.sh"

#!/usr/bin/env bash
# Deploie le CODE sur le VPS, et regenere le .env depuis Infisical (source de verite).
# Ne touche jamais aux sessions Telegram ni a la base state/ : ces fichiers vivent
# uniquement sur le serveur apres first-sync.sh.
#   usage : VPS=root@1.2.3.4 ./deploy/deploy.sh
set -euo pipefail

VPS="${VPS:?definir VPS, ex: VPS=root@1.2.3.4}"
APP_DIR=/opt/vtc-bridge
APP_USER=vtc
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Secrets : Infisical est la source de verite, le .env du serveur en est une copie
# regeneree a chaque deploiement. Le pont lit un fichier local, donc il demarre meme
# si Infisical est injoignable. SKIP_SECRETS=1 saute cette etape (deploiement de code seul).
push_secrets() {
  command -v infisical >/dev/null || {
    echo "ERREUR : CLI infisical absente. Installe-la, ou relance avec SKIP_SECRETS=1." >&2
    exit 1
  }
  local tmp
  tmp=$(mktemp -t vtc-env)
  chmod 600 "$tmp"
  trap 'rm -f "$tmp"' RETURN

  (cd "$SRC" && infisical export --env=prod --format=dotenv) > "$tmp" 2>/dev/null || {
    echo "ERREUR : export Infisical en echec. Verifie 'infisical login'." >&2
    exit 1
  }

  # Garde-fou : un export vide ou tronque ecraserait le .env de production par du vide.
  local n
  n=$(grep -cE '^[A-Z_]+=' "$tmp" || true)
  if [ "$n" -lt 20 ] || ! grep -q '^TG_API_HASH=' "$tmp"; then
    echo "ERREUR : export Infisical suspect ($n variables, TG_API_HASH absent). Rien n'est envoye." >&2
    exit 1
  fi

  scp -q "$tmp" "$VPS:$APP_DIR/.env"
  ssh "$VPS" "chown $APP_USER:$APP_USER $APP_DIR/.env && chmod 600 $APP_DIR/.env"
  echo "== secrets Infisical -> $APP_DIR/.env ($n variables) =="
}

if [ "${SKIP_SECRETS:-0}" = "1" ]; then
  echo "== secrets : etape sautee (SKIP_SECRETS=1) =="
else
  push_secrets
fi

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

#!/usr/bin/env bash
# TRANSFERT UNIQUE des secrets et de l'etat vers le VPS : .env, sessions Telegram, base courses.db.
# A lancer une seule fois, apres server-setup.sh et avant deploy.sh.
#   usage : VPS=root@1.2.3.4 ./deploy/first-sync.sh
set -euo pipefail

VPS="${VPS:?definir VPS, ex: VPS=root@1.2.3.4}"
APP_DIR=/opt/vtc-bridge
APP_USER=vtc
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SRC"

# 1. Le pont local doit etre arrete : sinon le WAL SQLite et la session Telegram
#    sont copies dans un etat incoherent.
if pgrep -f 'vtc_bridge\.py' >/dev/null; then
  echo "ERREUR : le pont tourne encore en local. Arrete-le, puis relance ce script." >&2
  exit 1
fi

# 2. Ne jamais ecraser un etat deja present sur le serveur.
if [ "${FORCE:-0}" != "1" ] && ssh "$VPS" "test -f $APP_DIR/state/courses.db"; then
  echo "ERREUR : $APP_DIR/state/courses.db existe deja sur le serveur." >&2
  echo "Ce script ecraserait la base de production. Relance avec FORCE=1 seulement si c'est voulu." >&2
  exit 1
fi

# 3. Replier le WAL dans le fichier principal pour n'avoir qu'un fichier a copier.
echo "== checkpoint SQLite =="
python3 - <<'PY'
import sqlite3
db = sqlite3.connect("state/courses.db")
db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
db.close()
PY

echo "== envoi des secrets et de l'etat =="
ssh "$VPS" "mkdir -p $APP_DIR/state"
scp .env "$VPS:$APP_DIR/.env"
scp vtc_session.session "$VPS:$APP_DIR/"
[ -f vtc_bot.session ] && scp vtc_bot.session "$VPS:$APP_DIR/"
scp state/courses.db "$VPS:$APP_DIR/state/courses.db"
[ -f state/position.json ] && scp state/position.json "$VPS:$APP_DIR/state/"

ssh "$VPS" bash -euo pipefail <<REMOTE
chown -R $APP_USER:$APP_USER $APP_DIR
chmod 600 $APP_DIR/.env $APP_DIR/*.session
chmod 700 $APP_DIR/state
REMOTE

cat <<'TXT'

Secrets en place. Etape suivante, obligatoire AVANT d'activer le service :
lancer le pont une fois a la main, en SSH interactif. Telegram voit une IP neuve
et peut demander un code de confirmation, qui se saisit au clavier -- impossible
sous systemd, ou le service tomberait en boucle de redemarrage.

  ssh VPS
  cd /opt/vtc-bridge
  sudo -u vtc .venv/bin/python vtc_bridge.py

Quand les logs montrent "Pont VTC demarre" et que les groupes repondent : Ctrl-C,
puis depuis le Mac : VPS=... ./deploy/deploy.sh
TXT

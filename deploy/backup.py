#!/usr/bin/env python3
"""Sauvegarde quotidienne de state/courses.db.

Copie cohérente par l'API de sauvegarde SQLite (pas un simple cp : le pont écrit
pendant ce temps), vérification d'intégrité de la copie, compression, rotation,
puis envoi hors du serveur vers le Storage Box Hetzner.

Lancé par vtc-backup.service ; le répertoire vient de StateDirectory=, donc il
existe toujours et appartient à l'utilisateur vtc. Sans BACKUP_REMOTE dans
l'environnement, l'envoi distant est simplement sauté : le script reste utilisable
à la main sur une machine sans Storage Box.
"""
from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SRC = Path(os.environ.get("BACKUP_SRC", "/opt/vtc-bridge/state/courses.db"))
DEST_DIR = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/vtc-bridge")) / "backups"
KEEP_DAYS = int(os.environ.get("BACKUP_KEEP_DAYS", "14"))

REMOTE = os.environ.get("BACKUP_REMOTE", "").strip()
REMOTE_PATH = os.environ.get("BACKUP_REMOTE_PATH", "vtc-bridge").strip()
REMOTE_PORT = os.environ.get("BACKUP_REMOTE_PORT", "23").strip()
SSH_KEY = os.environ.get("BACKUP_SSH_KEY", "").strip()
KNOWN_HOSTS = os.environ.get("BACKUP_KNOWN_HOSTS", "").strip()


def snapshot(src: Path, tmp: Path) -> None:
    """Copie à chaud, puis contrôle d'intégrité sur la copie."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(tmp)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    check = sqlite3.connect(tmp)
    try:
        verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if verdict != "ok":
        raise SystemExit(f"copie corrompue, PRAGMA integrity_check = {verdict}")


def prune(dest_dir: Path, keep_days: int) -> int:
    """Supprime les archives plus vieilles que keep_days. Retourne le nombre supprimé."""
    limit = time.time() - keep_days * 86400
    removed = 0
    for old in dest_dir.glob("courses-*.db.gz"):
        if old.stat().st_mtime < limit:
            old.unlink()
            removed += 1
    return removed


def upload(dest_dir: Path) -> str:
    """Miroir des archives vers le Storage Box, sans --delete.

    L'absence de --delete est voulue : la rotation locale efface au bout de
    KEEP_DAYS, le distant garde tout l'historique. À 131 Ko par jour, cela fait
    moins de 50 Mo par an sur un volume de 1 To.
    """
    if not REMOTE:
        return "envoi distant désactivé (BACKUP_REMOTE absent)"

    ssh = ["ssh", "-p", REMOTE_PORT, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
    if SSH_KEY:
        ssh += ["-i", SSH_KEY, "-o", "IdentitiesOnly=yes"]
    if KNOWN_HOSTS:
        ssh += ["-o", f"UserKnownHostsFile={KNOWN_HOSTS}"]

    # Le shell du Storage Box est restreint : il refuse les commandes composees,
    # donc pas de --rsync-path "mkdir -p ... && rsync". Le repertoire se cree par
    # un appel ssh separe, sans effet s'il existe deja.
    subprocess.run(ssh + [REMOTE, "mkdir", "-p", REMOTE_PATH],
                   capture_output=True, text=True, timeout=120)

    cmd = ["rsync", "-a", "--quiet", "-e", " ".join(ssh),
           f"{dest_dir}/", f"{REMOTE}:{REMOTE_PATH}/"]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if done.returncode != 0:
        raise SystemExit(
            f"envoi vers {REMOTE} en echec (code {done.returncode}) : "
            f"{(done.stderr or done.stdout).strip()[:300]}"
        )
    return f"envoye vers {REMOTE}:{REMOTE_PATH}/"


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"base absente : {SRC}")

    DEST_DIR.mkdir(parents=True, exist_ok=True)
    dest = DEST_DIR / f"courses-{time.strftime('%Y-%m-%d')}.db.gz"

    with tempfile.TemporaryDirectory(dir=DEST_DIR) as tmpdir:
        tmp_db = Path(tmpdir) / "snapshot.db"
        snapshot(SRC, tmp_db)

        tmp_gz = Path(tmpdir) / "snapshot.db.gz"
        with open(tmp_db, "rb") as raw, gzip.open(tmp_gz, "wb", compresslevel=6) as gz:
            shutil.copyfileobj(raw, gz)

        # Remplacement atomique : une archive du jour déjà présente est écrasée d'un bloc.
        os.replace(tmp_gz, dest)

    removed = prune(DEST_DIR, KEEP_DAYS)
    print(f"{dest} ({dest.stat().st_size} octets) · {removed} archive(s) expirée(s) supprimée(s)")
    print(upload(DEST_DIR))


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Gestion des chauffeurs du pont : un compte Telegram = un chauffeur.

  python add_driver.py add [--name Alice] [--groups -100123,-100456] [--owner]
        Connecte le compte Telegram du chauffeur (numéro, code, mot de passe 2FA éventuel),
        choisit ses groupes, l'enregistre. À lancer avec le chauffeur à côté : Telegram lui
        envoie le code de connexion.
  python add_driver.py list
  python add_driver.py groups  ID -100123,-100456     remplace les groupes lus pour ce chauffeur
  python add_driver.py set     ID min_price 30        règle un paramètre à sa place
  python add_driver.py disable ID                     coupe sa session (il ne reçoit plus rien)
  python add_driver.py enable  ID

Le pont en marche prend en compte les changements sous une minute, sans redémarrage.
La session créée (state/sessions/<id>.session) donne un accès COMPLET au compte Telegram du
chauffeur : droits 600, jamais versionnée, jamais copiée ailleurs.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import stat
import sys

from dotenv import load_dotenv

import drivers as D
import store

load_dotenv()
STATE_DIR = os.environ.get("STATE_DIR", "state")
SESSIONS_DIR = os.path.join(STATE_DIR, "sessions")
COURSES_DB = os.path.join(STATE_DIR, "courses.db")


def _ids(text: str) -> list[int]:
    try:
        return [int(x) for x in text.replace(" ", "").split(",") if x]
    except ValueError:
        sys.exit("Groupes : des identifiants numériques séparés par des virgules (ex. -1001234567890).")


def _row_or_exit(driver_id: int):
    row = store.get_driver_row(driver_id)
    if row is None:
        sys.exit(f"Chauffeur {driver_id} inconnu. `python add_driver.py list` pour voir les chauffeurs.")
    return row


async def _pick_groups(client) -> list[int]:
    """Liste les groupes du compte connecté et laisse choisir par numéros."""
    groups = []
    async for d in client.iter_dialogs():
        if d.is_group:
            groups.append((d.id, d.name or str(d.id)))
    if not groups:
        print("Aucun groupe trouvé sur ce compte.")
        return []
    print("\nGroupes de ce compte :")
    for i, (gid, name) in enumerate(groups, 1):
        print(f"  {i:3d}. {name[:50]:50s} {gid}")
    raw = input("\nNuméros des groupes de courses à surveiller (ex. 1,4,7 ; vide = aucun) : ").strip()
    picked = []
    for tok in raw.replace(" ", "").split(","):
        if tok.isdigit() and 1 <= int(tok) <= len(groups):
            picked.append(groups[int(tok) - 1][0])
    return picked


async def cmd_add(args) -> None:
    from telethon import TelegramClient

    api_id, api_hash = os.environ.get("TG_API_ID"), os.environ.get("TG_API_HASH")
    if not (api_id and api_hash):
        sys.exit("TG_API_ID / TG_API_HASH absents de l'environnement (.env).")
    os.makedirs(SESSIONS_DIR, mode=0o700, exist_ok=True)
    pending = os.path.join(SESSIONS_DIR, f"pending-{os.getpid()}")
    client = TelegramClient(pending, int(api_id), api_hash)
    try:
        await client.start()                       # interactif : numéro, code, 2FA
        me = await client.get_me()
        name = args.name or " ".join(x for x in (me.first_name, me.last_name) if x) or str(me.id)
        groups = _ids(args.groups) if args.groups is not None else await _pick_groups(client)
    finally:
        await client.disconnect()

    final = os.path.join(SESSIONS_DIR, str(me.id))
    os.replace(pending + ".session", final + ".session")
    os.chmod(final + ".session", stat.S_IRUSR | stat.S_IWUSR)          # 600
    for leftover in (pending + ".session-journal",):
        if os.path.exists(leftover):
            os.remove(leftover)

    store.init(COURSES_DB)
    existed = store.get_driver_row(me.id) is not None
    store.upsert_driver(me.id, name=name, session=final, groups=groups,
                        is_owner=True if args.owner else None)
    print(f"\n{'Mis à jour' if existed else 'Ajouté'} : {name} (id {me.id}), {len(groups)} groupe(s).")
    print("Dernière étape, côté chauffeur : ouvrir le bot Telegram, envoyer /start, puis partager "
          "sa position en direct (📎 → Position → en direct → 8 h).")
    print("Le pont le prend en compte sous une minute. Ses réglages : /reglages dans le bot.")


def cmd_list(_args) -> None:
    rows = store.list_driver_rows()
    if not rows:
        print("Aucun chauffeur.")
        return
    print(f"{'id':>12}  {'nom':20s} {'état':8s} {'groupes':>7}  {'session':8s} réglages modifiés")
    for r in rows:
        p = D.profile_from_row(r)
        has_session = os.path.exists((p.session or "") + ".session")
        changed = ", ".join(f"{k}={D.fmt_value(D.SETTINGS_BY_KEY[k], p.get(k))}"
                            for k in p.settings if k in D.SETTINGS_BY_KEY) or "—"
        print(f"{p.id:>12}  {(p.name or '?')[:20]:20s} {'actif' if p.active else 'coupé':8s} "
              f"{len(p.groups):>7}  {'ok' if has_session else 'ABSENTE':8s} {changed}"
              + ("  [propriétaire]" if p.is_owner else ""))


def cmd_groups(args) -> None:
    _row_or_exit(args.id)
    store.set_driver_groups(args.id, _ids(args.groups))
    print("Groupes mis à jour.")


def cmd_set(args) -> None:
    row = _row_or_exit(args.id)
    spec = D.SETTINGS_BY_KEY.get(args.key)
    if spec is None:
        sys.exit("Réglage inconnu. Disponibles : " + ", ".join(D.SETTINGS_BY_KEY))
    p = D.profile_from_row(row)
    try:
        val = p.set(args.key, args.value)
    except ValueError as e:
        sys.exit(f"Valeur refusée : {e}")
    store.save_driver_settings(args.id, p.settings)
    print(f"{spec.label} : {D.fmt_value(spec, val)}")


def cmd_active(args, active: bool) -> None:
    _row_or_exit(args.id)
    store.set_driver_active(args.id, active)
    print("Activé." if active else "Coupé : sa session sera fermée sous une minute.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Gestion des chauffeurs du pont VTC.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="connecter un compte Telegram et l'enregistrer")
    a.add_argument("--name")
    a.add_argument("--groups", help="ids séparés par des virgules (sinon choix interactif)")
    a.add_argument("--owner", action="store_true",
                   help="propriétaire du serveur : réservations ajoutées à son agenda macOS")
    sub.add_parser("list")
    g = sub.add_parser("groups")
    g.add_argument("id", type=int)
    g.add_argument("groups")
    s = sub.add_parser("set")
    s.add_argument("id", type=int)
    s.add_argument("key")
    s.add_argument("value")
    for name in ("enable", "disable"):
        sub.add_parser(name).add_argument("id", type=int)
    args = ap.parse_args()

    if args.cmd == "add":
        asyncio.run(cmd_add(args))
        return 0
    store.init(COURSES_DB)
    {"list": cmd_list, "groups": cmd_groups, "set": cmd_set,
     "enable": lambda x: cmd_active(x, True), "disable": lambda x: cmd_active(x, False)}[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Test manuel : un lien de connexion tg://login ouvert sur le MÊME téléphone suffit-il
à relier un compte Telegram ? (liaison de la session d'un chauffeur sans second écran)

À lancer sur ton Mac, avec TG_API_ID et TG_API_HASH dans le .env, et ton téléphone sur le
même Wi-Fi. Le script ne garde rien : session en mémoire, déconnectée à la fin.

    python test_qr_link.py              # test réel
    python test_qr_link.py --dry-run    # sans Telegram : vérifie seulement la page

Deux essais à faire, et à noter :
  A. Depuis le navigateur du téléphone (Safari ou Chrome, PAS Telegram) : ouvre l'adresse
     affichée, touche « Ouvrir dans Telegram ». Telegram doit proposer de confirmer.
  B. Depuis l'intérieur de Telegram : envoie-toi le lien affiché dans le terminal (Messages
     enregistrés), puis touche-le. La documentation de Telegram dit que ce cas est ignoré.
"""
from __future__ import annotations

import asyncio
import getpass
import json
import os
import socket
import sys
import time

from aiohttp import web
from dotenv import load_dotenv

load_dotenv()
PORT = int(os.environ.get("TEST_PORT", "8765"))
GIVE_UP_AFTER = 300                       # secondes : le script s'arrête seul
DRY = "--dry-run" in sys.argv

state = {"url": "", "status": "préparation", "done": False, "who": ""}

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Test lien Telegram</title>
<style>body{font:17px system-ui;margin:24px;max-width:480px}a.b{display:block;background:#0a7ea4;
color:#fff;padding:16px;border-radius:10px;text-align:center;text-decoration:none;font-weight:600;
margin:20px 0}#s{padding:10px;background:#eef2f6;border-radius:8px}</style></head><body>
<h1>Test du lien de connexion</h1>
<p>Ouvre cette page dans le navigateur du téléphone, puis touche le bouton.
Telegram doit s'ouvrir et demander de confirmer.</p>
<a class="b" id="go" href="#">Ouvrir dans Telegram</a>
<p id="s">…</p>
<script>
async function tick(){
  try{
    const r = await fetch('/state'); const s = await r.json();
    if(s.url) document.getElementById('go').href = s.url;
    document.getElementById('s').textContent = 'État : ' + s.status;
  }catch(e){ document.getElementById('s').textContent = 'Serveur arrêté'; }
}
tick(); setInterval(tick, 2000);
</script></body></html>"""


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def page(_request):
    return web.Response(text=PAGE, content_type="text/html")


async def get_state(_request):
    return web.json_response({"url": state["url"], "status": state["status"]})


async def telegram_flow() -> None:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    await client.connect()
    try:
        qr = await client.qr_login()
        deadline = time.time() + GIVE_UP_AFTER
        while time.time() < deadline:
            state["url"] = qr.url
            state["status"] = "en attente de ta confirmation dans Telegram"
            print(f"\nLien actuel (valable ~30 s) :\n{qr.url}\n", flush=True)
            try:
                user = await qr.wait(timeout=25)
            except asyncio.TimeoutError:
                await qr.recreate()          # le jeton expire vite : on en refait un
                continue
            except SessionPasswordNeededError:
                state["status"] = "mot de passe de double authentification demandé (terminal)"
                pw = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: getpass.getpass("Mot de passe 2FA (non conservé) : "))
                user = await client.sign_in(password=pw)
            state["who"] = getattr(user, "first_name", "") or "compte"
            state["status"] = "CONNECTÉ"
            state["done"] = True
            print(f"\nRÉSULTAT A : connexion réussie ({state['who']}). Le lien ouvert depuis le "
                  "navigateur du téléphone fonctionne.\n", flush=True)
            await client.log_out()          # révoque la session de test
            print("Session de test révoquée.", flush=True)
            return
        print("\nTemps écoulé sans confirmation.", flush=True)
    finally:
        if client.is_connected():
            await client.disconnect()


async def dry_flow() -> None:
    state["url"] = "tg://login?token=EXEMPLE_SANS_TELEGRAM"
    state["status"] = "mode essai : aucune connexion à Telegram"
    await asyncio.sleep(GIVE_UP_AFTER)


async def main() -> int:
    app = web.Application()
    app.add_routes([web.get("/", page), web.get("/state", get_state)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    ip = lan_ip()
    print(f"\nPage de test : http://{ip}:{PORT}   (ouvre-la dans le NAVIGATEUR du téléphone)")
    print(f"Le script s'arrête seul après {GIVE_UP_AFTER // 60} min. Ctrl+C pour couper avant.",
          flush=True)
    flow = dry_flow if DRY else telegram_flow
    try:
        await flow()
        await asyncio.sleep(3)              # laisse la page afficher l'état final
    finally:
        await runner.cleanup()
    return 0 if state["done"] or DRY else 1


if __name__ == "__main__":
    if not DRY and not (os.environ.get("TG_API_ID") and os.environ.get("TG_API_HASH")):
        sys.exit("TG_API_ID / TG_API_HASH absents du .env.")
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)

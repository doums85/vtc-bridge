#!/usr/bin/env python3
"""
Liste tous tes groupes/canaux Telegram avec leur ID numérique.
Sert à remplir VTC_GROUPS et FEED_GROUP dans le .env.

Usage :
  cd /Users/rrr/vtc-bridge
  source .venv/bin/activate
  TG_API_ID=... TG_API_HASH=... python list_ids.py
  (ou mets TG_API_ID / TG_API_HASH dans un .env — il est chargé automatiquement)

1er lancement : login interactif (numéro + code Telegram). Réutilise la session
'vtc_session' que le pont utilisera ensuite — pas besoin de se reconnecter.
"""
import os
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import Channel, Chat

load_dotenv()

api_id = int(os.environ["TG_API_ID"])
api_hash = os.environ["TG_API_HASH"]
session = os.environ.get("TG_SESSION", "vtc_session")


async def main():
    async with TelegramClient(session, api_id, api_hash) as client:
        print(f"{'ID':>16}  {'type':10}  nom")
        print("-" * 60)
        async for dialog in client.iter_dialogs():
            ent = dialog.entity
            if isinstance(ent, (Channel, Chat)):  # groupes, supergroupes, canaux
                kind = "canal" if getattr(ent, "broadcast", False) else "groupe"
                print(f"{dialog.id:>16}  {kind:10}  {dialog.name}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

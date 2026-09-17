"""Lecture des DMs avec les dispatchers pour trouver les montants de commission."""
import asyncio, os
from datetime import datetime, timezone
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, User

load_dotenv()
API_ID   = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION  = "/tmp/vtc_read_tmp"

SINCE = datetime(2026, 7, 20, 22, 0, 0, tzinfo=timezone.utc)

# Dispatchers identifiés dans les captures
DISPATCHER_NAMES = ["FMA", "BROLY", "Ousboy", "BOB", "Sirou", "Elyax", "Drop Me", "ALG", "Renta", "MARKHOR", "taxiline"]

async def main():
    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        async for dialog in client.iter_dialogs(limit=300):
            entity = dialog.entity
            if not isinstance(entity, User): continue
            name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
            
            # Filtre sur dispatchers connus ou numéros récents
            match = any(k.lower() in name.lower() for k in DISPATCHER_NAMES)
            if not match: continue

            print(f"\n{'='*60}")
            print(f"DM avec : {name} (id: {entity.id})")
            print(f"{'='*60}")
            
            async for msg in client.iter_messages(dialog.id, limit=80):
                if msg.date < SINCE: break
                dt = msg.date.astimezone().strftime("%d/%m %H:%M")
                direction = "→" if msg.out else "←"
                if msg.media:
                    print(f"  {dt} {direction} [PHOTO/FICHIER]")
                elif msg.text:
                    print(f"  {dt} {direction} {msg.text[:120]}")

asyncio.run(main())

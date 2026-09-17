"""
Scan EXHAUSTIF depuis lundi :
1. Toutes les photos envoyées (→) dans TOUS les dialogs
2. Tous les messages reçus autour pour identifier la course
"""
import asyncio, os
from datetime import datetime, timezone
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, User, Channel, Chat

load_dotenv()
API_ID   = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION  = "/tmp/vtc_read_tmp"
# Depuis dimanche 19/07 22h UTC = lundi 20/07 00h00 Paris
SINCE = datetime(2026, 7, 19, 22, 0, 0, tzinfo=timezone.utc)

async def main():
    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        me = await client.get_me()
        print(f"Compte : {me.first_name} — scan exhaustif depuis lundi 21/07\n")

        total_photos = []

        async for dialog in client.iter_dialogs(limit=500):
            entity = dialog.entity
            name = dialog.name or str(dialog.id)

            try:
                msgs = []
                async for msg in client.iter_messages(dialog.id, limit=300):
                    if msg.date < SINCE:
                        break
                    msgs.append(msg)

                if not msgs:
                    continue

                # Chercher les photos envoyées
                for msg in msgs:
                    if msg.out and isinstance(msg.media, (MessageMediaPhoto, MessageMediaDocument)):
                        # Contexte : ±5 messages autour
                        ctx = []
                        for m2 in sorted(msgs, key=lambda x: x.id):
                            if abs(m2.id - msg.id) <= 5 and m2.id != msg.id:
                                direction = "→" if m2.out else "←"
                                if m2.text:
                                    ctx.append(f"{direction} {m2.text[:80]}")
                                elif m2.media:
                                    ctx.append(f"{direction} [PHOTO]")
                        
                        total_photos.append({
                            "date": msg.date,
                            "dialog": name,
                            "type": type(entity).__name__,
                            "ctx": ctx,
                        })

            except Exception as e:
                pass

        # Trier par date
        total_photos.sort(key=lambda x: x["date"])

        print(f"{'='*70}")
        print(f"TOUTES LES PHOTOS ENVOYÉES DEPUIS LUNDI ({len(total_photos)} photos)")
        print(f"{'='*70}")

        for i, p in enumerate(total_photos, 1):
            dt = p["date"].astimezone().strftime("%a %d/%m %H:%M")
            print(f"\n[{i}] {dt} → {p['dialog']} ({p['type']})")
            for c in p["ctx"]:
                print(f"     {c}")

asyncio.run(main())

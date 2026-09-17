"""
Bilan semaine VTC — scan messages dans les groupes VTC uniquement
Depuis lundi 21/07 00h Paris (dimanche 20/07 22h UTC)
"""
import asyncio, os
from datetime import datetime, timezone
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, User, Channel, Chat

load_dotenv()
API_ID   = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION  = "/tmp/vtc_read_tmp"

# Depuis lundi 21/07 00h00 Paris
SINCE = datetime(2026, 7, 20, 22, 0, 0, tzinfo=timezone.utc)

# Groupes VTC configurés dans le pont
VTC_GROUPS = [g.strip() for g in os.environ.get("VTC_GROUPS", "").split(",") if g.strip()]

async def main():
    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        me = await client.get_me()
        print(f"Compte : {me.first_name} | Scan depuis lundi 21/07\n")
        print(f"Groupes VTC configurés : {len(VTC_GROUPS)}\n")
        print("=" * 60)

        async for dialog in client.iter_dialogs(limit=500):
            did = str(dialog.id)
            # Chercher dans tous les groupes (VTC ou pas)
            entity = dialog.entity
            name = dialog.name or str(dialog.id)
            
            # Filtrer : seulement groupes/canaux
            if not isinstance(entity, (Channel, Chat)):
                continue

            # Récupérer messages depuis lundi
            msgs = []
            try:
                async for msg in client.iter_messages(dialog.id, limit=200, offset_date=None, reverse=False):
                    if msg.date < SINCE:
                        break
                    if msg.date >= SINCE:
                        msgs.append(msg)
            except:
                continue

            if not msgs:
                continue

            # Compter mes messages + photos envoyées
            my_msgs = [m for m in msgs if m.out]
            my_photos = [m for m in my_msgs if m.media and isinstance(m.media, MessageMediaPhoto)]
            
            if not my_msgs and not my_photos:
                continue

            print(f"\n📍 {name}")
            print(f"   ID: {dialog.id} | Mes messages: {len(my_msgs)} | Mes photos: {len(my_photos)}")
            
            # Afficher mes photos avec contexte
            for p in my_photos:
                ts = p.date.astimezone().strftime("%a %d/%m %H:%M")
                # Message précédent pour contexte
                print(f"   📸 {ts} — photo envoyée")
            
            # Afficher mes derniers messages texte
            for m in my_msgs[-5:]:
                if m.text and m.text.strip():
                    ts = m.date.astimezone().strftime("%a %d/%m %H:%M")
                    txt = m.text[:80].replace('\n', ' ')
                    print(f"   ✉️  {ts} — {txt}")

        print("\n" + "=" * 60)
        print("Scan terminé.")

asyncio.run(main())

#!/usr/bin/env python3
"""
Cherche dans TOUS les dialogs (DM + groupes) les photos envoyées par moi depuis lundi.
"""
import os, asyncio
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, User, Channel, Chat

load_dotenv()

api_id   = int(os.environ["TG_API_ID"])
api_hash = os.environ["TG_API_HASH"]
session  = "/tmp/vtc_read_tmp"

SINCE = datetime(2026, 7, 20, 22, 0, 0, tzinfo=timezone.utc)

def fmt(d):
    return d.astimezone(timezone(timedelta(hours=2))).strftime("%a %d/%m %H:%M")

async def main():
    async with TelegramClient(session, api_id, api_hash) as client:
        me = await client.get_me()
        print(f"Connecté : {me.first_name} ({me.id})\n")

        print("=== DIALOGS ACTIFS DEPUIS LUNDI ===")
        active_dialogs = []
        async for dialog in client.iter_dialogs(limit=200):
            if dialog.date and dialog.date > SINCE:
                ent = dialog.entity
                kind = "DM" if isinstance(ent, User) else ("canal" if getattr(ent, "broadcast", False) else "groupe")
                active_dialogs.append((dialog, kind))
        
        print(f"{len(active_dialogs)} dialogs actifs\n")

        photos_sent = []
        for dialog, kind in active_dialogs:
            title = dialog.name or str(dialog.id)
            gid = dialog.id
            
            async for msg in client.iter_messages(gid, limit=300, from_user="me"):
                if msg.date < SINCE:
                    break
                has_photo = bool(msg.media and isinstance(msg.media, (MessageMediaPhoto, MessageMediaDocument)))
                if has_photo:
                    txt = (msg.text or "")[:80]
                    photos_sent.append({
                        "group": title,
                        "kind": kind,
                        "gid": gid,
                        "date": fmt(msg.date),
                        "text": txt,
                        "msg_id": msg.id,
                    })

        print(f"=== PHOTOS ENVOYÉES PAR MOI DEPUIS LUNDI ===")
        print(f"Total : {len(photos_sent)} photo(s)\n")

        if not photos_sent:
            print("Aucune photo envoyée depuis lundi dans les dialogs récents.")
            # Affiche les dialogs actifs pour debug
            print("\nDialogs actifs (derniers 30) :")
            for dialog, kind in active_dialogs[:30]:
                print(f"  [{kind}] {dialog.name} ({dialog.id})")
        else:
            for p in photos_sent:
                print(f"[{p['date']}] {p['kind']} : {p['group']}")
                raw_id = str(abs(p['gid']))
                if len(raw_id) > 10:
                    link_id = raw_id[3:]  # supprimer le 100 prefix
                    print(f"  🔗 https://t.me/c/{link_id}/{p['msg_id']}")
                if p['text']:
                    print(f"  Texte : {p['text']}")
                print()

if __name__ == "__main__":
    asyncio.run(main())

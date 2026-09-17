#!/usr/bin/env python3
"""
Cherche spécifiquement les messages ENVOYÉS PAR MOI avec une photo
ou avec le mot commission/paiement depuis lundi — dans tous les groupes VTC.
"""
import os, asyncio
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

load_dotenv()

api_id   = int(os.environ["TG_API_ID"])
api_hash = os.environ["TG_API_HASH"]
session  = "/tmp/vtc_read_tmp"

VTC_GROUPS = [int(x) for x in os.environ["VTC_GROUPS"].split(",") if x.strip()]
FEED_GROUP = int(os.environ["FEED_GROUP"])

SINCE = datetime(2026, 7, 20, 22, 0, 0, tzinfo=timezone.utc)  # lundi 21/07 00:00 Paris

def fmt(d):
    return d.astimezone(timezone(timedelta(hours=2))).strftime("%a %d/%m %H:%M")

async def main():
    async with TelegramClient(session, api_id, api_hash) as client:
        me = await client.get_me()
        print(f"Connecté : {me.first_name} {me.last_name or ''} (@{me.username or me.id})\n")

        found = []
        all_groups = VTC_GROUPS + [FEED_GROUP]

        for gid in all_groups:
            try:
                ent = await client.get_entity(gid)
                title = getattr(ent, "title", str(gid))
            except Exception as e:
                continue

            async for msg in client.iter_messages(gid, limit=500):
                if msg.date < SINCE:
                    break
                # Messages que j'ai envoyés (out=True) OU qui contiennent mots-clés commission
                txt = (msg.text or "").strip().lower()
                has_photo = bool(msg.media and isinstance(msg.media, (MessageMediaPhoto, MessageMediaDocument)))
                is_me = msg.out
                
                kw_match = any(k in txt for k in ["commission", "paiement", "payé", "virement", "reçu", "receipt", "cb", "capture"])
                
                if is_me and has_photo:
                    found.append({
                        "group": title,
                        "gid": gid,
                        "date": fmt(msg.date),
                        "type": "📤 MOI + 🖼️ PHOTO",
                        "text": (msg.text or "")[:100],
                        "msg_id": msg.id,
                    })
                elif is_me and kw_match:
                    found.append({
                        "group": title,
                        "gid": gid,
                        "date": fmt(msg.date),
                        "type": "📤 MOI + 💰 MOT-CLÉ",
                        "text": (msg.text or "")[:100],
                        "msg_id": msg.id,
                    })

        print(f"{'='*70}")
        print(f"RÉSUMÉ : {len(found)} messages trouvés (envoyés par moi avec photo ou mot-clé)")
        print(f"{'='*70}\n")

        if not found:
            print("Aucun message avec photo ou mot-clé commission trouvé depuis lundi.")
            print("\nVérification : mes messages envoyés depuis lundi (tous types) :")
            # Affiche tous mes messages dans les groupes pour voir ce qui existe
            for gid in all_groups[:3]:  # on check les 3 premiers groupes
                try:
                    ent = await client.get_entity(gid)
                    title = getattr(ent, "title", str(gid))
                    count_me = 0
                    async for msg in client.iter_messages(gid, limit=500, from_user="me"):
                        if msg.date < SINCE:
                            break
                        count_me += 1
                        has_photo = bool(msg.media and isinstance(msg.media, (MessageMediaPhoto, MessageMediaDocument)))
                        txt_short = (msg.text or "")[:60] or "[pas de texte]"
                        print(f"  [{fmt(msg.date)}] {title} | photo={has_photo} | {txt_short}")
                    print(f"  -> {count_me} msg de moi dans {title}")
                except Exception as e:
                    print(f"  Erreur {gid}: {e}")
        else:
            for item in found:
                print(f"[{item['date']}] {item['type']}")
                print(f"  Groupe : {item['group']}")
                print(f"  Lien   : https://t.me/c/{str(abs(item['gid']))[3:]}/{item['msg_id']}")
                if item['text']:
                    print(f"  Texte  : {item['text']}")
                print()

if __name__ == "__main__":
    asyncio.run(main())

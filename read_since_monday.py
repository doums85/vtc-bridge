#!/usr/bin/env python3
"""
Lit tous les messages depuis lundi dans les groupes VTC + le groupe feed.
Cherche les courses + mentions de commission/paiement.
"""
import os, asyncio, json
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto

load_dotenv()

api_id   = int(os.environ["TG_API_ID"])
api_hash = os.environ["TG_API_HASH"]
session  = "/tmp/vtc_read_tmp"  # copie temporaire pour lecture seule (session principale verrouillée par vtc_bridge)
VTC_GROUPS = [int(x) for x in os.environ["VTC_GROUPS"].split(",") if x.strip()]
FEED_GROUP = int(os.environ["FEED_GROUP"])

# Lundi 21 juillet 2026 à 00:00 Paris (UTC+2) = 22:00 UTC le 20
SINCE = datetime(2026, 7, 20, 22, 0, 0, tzinfo=timezone.utc)

def fmt_date(d):
    if d is None:
        return "?"
    paris = d.astimezone(timezone(timedelta(hours=2)))
    return paris.strftime("%a %d/%m %H:%M")

async def main():
    async with TelegramClient(session, api_id, api_hash) as client:
        all_groups = VTC_GROUPS + [FEED_GROUP]
        
        for gid in all_groups:
            try:
                ent = await client.get_entity(gid)
                title = getattr(ent, "title", str(gid))
            except Exception as e:
                print(f"\n### {gid} — impossible : {e}")
                continue

            messages_since = []
            async for msg in client.iter_messages(gid, limit=500, offset_date=None):
                if msg.date < SINCE:
                    break
                messages_since.append(msg)
            
            if not messages_since:
                continue
            
            print(f"\n{'='*70}")
            print(f"### {title}  ({gid})  — {len(messages_since)} msgs depuis lundi")
            print(f"{'='*70}")
            
            for msg in reversed(messages_since):
                txt = (msg.text or "").strip()
                has_photo = bool(msg.media and isinstance(msg.media, MessageMediaPhoto))
                is_me = msg.out
                
                # Flags
                flags = []
                if is_me:
                    flags.append("📤 MOI")
                if has_photo:
                    flags.append("🖼️ PHOTO")
                if any(kw in txt.lower() for kw in ["commission", "paiement", "payé", "paye", "virement", "€", "euro"]):
                    flags.append("💰 COMMISSION?")
                
                flag_str = " ".join(flags)
                one_line = " / ".join(l.strip() for l in txt.splitlines() if l.strip())[:150]
                
                print(f"  [{fmt_date(msg.date)}] {flag_str}")
                if one_line:
                    print(f"    {one_line}")
                elif has_photo:
                    print(f"    [image sans texte]")

if __name__ == "__main__":
    asyncio.run(main())

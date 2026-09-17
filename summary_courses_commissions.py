#!/usr/bin/env python3
"""
Récupère le contexte des DMs où j'ai envoyé des photos depuis lundi.
Aussi : résume mes réponses dans les groupes VTC (courses prises).
"""
import os, asyncio
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, User

load_dotenv()

api_id   = int(os.environ["TG_API_ID"])
api_hash = os.environ["TG_API_HASH"]
session  = "/tmp/vtc_read_tmp"
VTC_GROUPS = [int(x) for x in os.environ["VTC_GROUPS"].split(",") if x.strip()]

SINCE = datetime(2026, 7, 20, 22, 0, 0, tzinfo=timezone.utc)

def fmt(d):
    return d.astimezone(timezone(timedelta(hours=2))).strftime("%a %d/%m %H:%M")

# DMs avec photos identifiés
DM_TARGETS = ["Ousboy", "Drop Me", "Sirou", "BOB", "BROLY", "Elyax", "FMA"]

async def main():
    async with TelegramClient(session, api_id, api_hash) as client:

        # ──────────────────────────────────────────────
        # 1. COURSES PRISES dans les groupes VTC
        # ──────────────────────────────────────────────
        print("=" * 70)
        print("COURSES PRISES (mes réponses dans les groupes VTC depuis lundi)")
        print("=" * 70)

        all_my_replies = []
        for gid in VTC_GROUPS:
            try:
                ent = await client.get_entity(gid)
                title = getattr(ent, "title", str(gid))
            except:
                continue
            async for msg in client.iter_messages(gid, limit=500, from_user="me"):
                if msg.date < SINCE:
                    break
                txt = (msg.text or "").strip()
                # Filtre : messages courts qui ressemblent à des réponses de dispatch (SP, chiffre, ok, pv)
                if txt.lower() in ["sp", "ok", "v", "pv", "ok sp"] or txt.isdigit() or (len(txt) <= 4 and txt.isdigit()):
                    all_my_replies.append({
                        "date": fmt(msg.date),
                        "group": title,
                        "reply": txt,
                        "msg_id": msg.id,
                        "gid": gid,
                    })
                elif txt and len(txt) < 30:
                    all_my_replies.append({
                        "date": fmt(msg.date),
                        "group": title,
                        "reply": txt,
                        "msg_id": msg.id,
                        "gid": gid,
                    })

        all_my_replies.sort(key=lambda x: x["date"])
        for r in all_my_replies:
            gid_str = str(abs(r["gid"]))[3:]
            print(f"  [{r['date']}] {r['group'][:30]} → réponse : «{r['reply']}»")
            print(f"             🔗 https://t.me/c/{gid_str}/{r['msg_id']}")

        # ──────────────────────────────────────────────
        # 2. CONVERSATIONS DM avec photos (commissions)
        # ──────────────────────────────────────────────
        print(f"\n{'='*70}")
        print("COMMISSIONS — DMs avec photos envoyées depuis lundi")
        print("=" * 70)

        async for dialog in client.iter_dialogs(limit=200):
            if not dialog.date or dialog.date < SINCE:
                continue
            ent = dialog.entity
            if not isinstance(ent, User):
                continue
            
            name = dialog.name or ""
            # Check si ce dialog a des photos envoyées par moi
            has_my_photo = False
            msgs = []
            async for msg in client.iter_messages(dialog.id, limit=100):
                if msg.date < SINCE:
                    break
                has_photo = bool(msg.media and isinstance(msg.media, (MessageMediaPhoto, MessageMediaDocument)))
                if msg.out and has_photo:
                    has_my_photo = True
                msgs.append(msg)
            
            if not has_my_photo:
                continue
            
            print(f"\n─── DM avec : {name} ───")
            for msg in reversed(msgs):
                who = "📤 MOI" if msg.out else f"👤 {name[:20]}"
                has_photo = bool(msg.media and isinstance(msg.media, (MessageMediaPhoto, MessageMediaDocument)))
                txt = (msg.text or "").strip()
                media_str = " [📷 PHOTO]" if has_photo else ""
                one_line = " / ".join(l.strip() for l in txt.splitlines() if l.strip())[:120]
                print(f"  [{fmt(msg.date)}] {who}{media_str}")
                if one_line:
                    print(f"    {one_line}")

if __name__ == "__main__":
    asyncio.run(main())

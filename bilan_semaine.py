"""Bilan complet de la semaine : courses prises dans les groupes VTC + DMs dispatchers."""
import asyncio, os
from datetime import datetime, timezone
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, User, Channel, Chat

load_dotenv()
API_ID   = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION  = "/tmp/vtc_read_tmp"

# Lundi 21 juillet 2026 00:00 Paris = dimanche 20 juillet 22:00 UTC
SINCE = datetime(2026, 7, 20, 22, 0, 0, tzinfo=timezone.utc)

VTC_GROUPS = [int(x) for x in os.environ.get("VTC_GROUPS", "").split(",") if x.strip()]
FEED_GROUP = int(os.environ.get("FEED_GROUP", "-5468570143"))

REPLY_KEYWORDS = {"sp", "ok", "pv", "c1", "c2"}

def is_course_reply(text):
    if not text: return False
    t = text.strip().lower()
    if t in REPLY_KEYWORDS: return True
    if t.isdigit() and 1 <= int(t) <= 30: return True
    return False

async def main():
    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        me = await client.get_me()
        print(f"Compte : {me.first_name} {me.last_name or ''} (@{me.username or me.id})")
        print(f"Période : depuis le lundi 21 juillet 2026\n")

        # ── 1. Courses prises dans les groupes VTC ──────────────────────────
        print("=" * 70)
        print("COURSES PRISES DANS LES GROUPES VTC")
        print("=" * 70)

        all_groups = VTC_GROUPS + [-5330889567, -1001332694633]  # inclure nouveaux
        courses = []

        for gid in all_groups:
            try:
                entity = await client.get_entity(gid)
                name = getattr(entity, 'title', str(gid))
            except Exception:
                name = str(gid)

            try:
                async for msg in client.iter_messages(gid, offset_date=None, reverse=False, limit=500):
                    if msg.date < SINCE: break
                    if msg.out and msg.text and is_course_reply(msg.text):
                        # Récupérer le message original auquel il répond
                        context = ""
                        if msg.reply_to_msg_id:
                            try:
                                orig = await client.get_messages(gid, ids=msg.reply_to_msg_id)
                                if orig and orig.text:
                                    context = orig.text[:120].replace("\n", " ")
                            except Exception:
                                pass
                        courses.append({
                            "date": msg.date,
                            "group": name,
                            "reply": msg.text.strip(),
                            "context": context,
                        })
            except Exception as e:
                print(f"  [erreur groupe {gid}] {e}")

        courses.sort(key=lambda x: x["date"])
        for c in courses:
            dt = c["date"].astimezone().strftime("%a %d/%m %H:%M")
            print(f"  {dt}  [{c['reply']:>4}]  {c['group']}")
            if c["context"]:
                print(f"             └─ {c['context'][:100]}")

        print(f"\nTotal courses prises : {len(courses)}")

        # ── 2. DMs avec photos (captures commissions) ────────────────────────
        print("\n" + "=" * 70)
        print("CAPTURES DE COMMISSION (photos envoyées en DM)")
        print("=" * 70)

        photos_found = []
        async for dialog in client.iter_dialogs(limit=200):
            entity = dialog.entity
            # Uniquement DMs (User) ou petits groupes/canaux récents
            if not isinstance(entity, User): continue
            try:
                async for msg in client.iter_messages(dialog.id, limit=200):
                    if msg.date < SINCE: break
                    if msg.out and isinstance(msg.media, (MessageMediaPhoto, MessageMediaDocument)):
                        name = f"{entity.first_name or ''} {entity.last_name or ''}".strip() or str(entity.id)
                        # Contexte : messages autour
                        ctx_msgs = await client.get_messages(dialog.id, min_id=msg.id-3, max_id=msg.id+3, limit=10)
                        ctx_texts = [m.text for m in sorted(ctx_msgs, key=lambda x: x.id) if m.text and m.id != msg.id]
                        ctx = " | ".join(ctx_texts[:4])[:150]
                        photos_found.append({
                            "date": msg.date,
                            "contact": name,
                            "context": ctx,
                        })
            except Exception:
                pass

        photos_found.sort(key=lambda x: x["date"])
        for p in photos_found:
            dt = p["date"].astimezone().strftime("%a %d/%m %H:%M")
            print(f"  {dt}  → {p['contact']}")
            if p["context"]:
                print(f"             └─ {p['context'][:120]}")

        print(f"\nTotal captures envoyées : {len(photos_found)}")

asyncio.run(main())

#!/usr/bin/env python3
"""
Lit TOUS les DMs actifs depuis lundi et extrait les infos de course + commission.
"""
import os, asyncio, re
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, User

load_dotenv()

api_id   = int(os.environ["TG_API_ID"])
api_hash = os.environ["TG_API_HASH"]
session  = "/tmp/vtc_read_tmp"

SINCE = datetime(2026, 7, 19, 22, 0, 0, tzinfo=timezone.utc)  # lundi 20/07 00:00 Paris (UTC+2)

def fmt(d):
    return d.astimezone(timezone(timedelta(hours=2))).strftime("%a %d/%m %H:%M")

async def main():
    async with TelegramClient(session, api_id, api_hash) as client:
        print("=== TOUS LES DMs ACTIFS DEPUIS LUNDI — MESSAGES COMPLETS ===\n")

        async for dialog in client.iter_dialogs(limit=300):
            if not dialog.date or dialog.date < SINCE:
                continue
            ent = dialog.entity
            if not isinstance(ent, User):
                continue

            # Récupère tous les messages du dialog depuis lundi
            msgs = []
            async for msg in client.iter_messages(dialog.id, limit=200):
                if msg.date < SINCE:
                    break
                msgs.append(msg)

            if not msgs:
                continue

            name = dialog.name or str(dialog.id)
            print(f"\n{'='*70}")
            print(f"DM : {name}  (id={dialog.id})")
            print(f"{'='*70}")

            for msg in reversed(msgs):
                who = "📤 MOI" if msg.out else f"👤 {name[:25]}"
                has_photo = bool(msg.media and isinstance(msg.media, (MessageMediaPhoto, MessageMediaDocument)))
                txt = (msg.text or "").strip()
                media_str = " [📷]" if has_photo else ""
                lines = [l.strip() for l in txt.splitlines() if l.strip()]
                one_line = " / ".join(lines)[:200]
                print(f"  [{fmt(msg.date)}] {who}{media_str}")
                if one_line:
                    print(f"    {one_line}")

if __name__ == "__main__":
    asyncio.run(main())

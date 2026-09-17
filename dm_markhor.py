"""Lecture complète du DM MARKHOR 92."""
import asyncio, os
from datetime import datetime, timezone
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import User

load_dotenv()
API_ID   = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION  = "/tmp/vtc_read_tmp"
SINCE    = datetime(2026, 7, 20, 22, 0, 0, tzinfo=timezone.utc)

async def main():
    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        entity = await client.get_entity(6339626988)
        async for msg in client.iter_messages(entity, limit=60):
            if msg.date < SINCE: break
            dt = msg.date.astimezone().strftime("%d/%m %H:%M")
            direction = "→" if msg.out else "←"
            if msg.media:
                print(f"  {dt} {direction} [PHOTO/FICHIER]")
            elif msg.text:
                print(f"  {dt} {direction} {msg.text[:150]}")

asyncio.run(main())

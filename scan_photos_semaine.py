"""
Scan TOUTES les photos envoyées depuis lundi dans tous les dialogs
"""
import asyncio, os
from datetime import datetime, timezone
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto

load_dotenv()
API_ID   = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION  = "/tmp/vtc_read_tmp"
SINCE = datetime(2026, 7, 20, 22, 0, 0, tzinfo=timezone.utc)

async def main():
    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        me = await client.get_me()
        print(f"Compte : {me.first_name} | Photos envoyées depuis lundi 21/07\n")
        
        async for dialog in client.iter_dialogs(limit=500):
            name = dialog.name or str(dialog.id)
            try:
                async for msg in client.iter_messages(dialog.id, limit=300):
                    if msg.date < SINCE:
                        break
                    if msg.out and msg.media and isinstance(msg.media, MessageMediaPhoto):
                        ts = msg.date.astimezone().strftime("%a %d/%m %H:%M")
                        # Contexte : 2 messages précédents
                        context = []
                        async for prev in client.iter_messages(dialog.id, limit=3, max_id=msg.id):
                            if prev.text:
                                context.append(prev.text[:60].replace('\n',' '))
                        print(f"📸 [{ts}] dans '{name}'")
                        for c in reversed(context):
                            print(f"   contexte: {c}")
                        print()
            except:
                continue

asyncio.run(main())

#!/usr/bin/env python3
"""Échantillonne les derniers messages des groupes VTC pour comprendre le format."""
import os, asyncio
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()
api_id = int(os.environ["TG_API_ID"])
api_hash = os.environ["TG_API_HASH"]
session = os.environ.get("TG_SESSION", "vtc_session")
VTC_GROUPS = [int(x) for x in os.environ["VTC_GROUPS"].split(",") if x.strip()]
N = int(os.environ.get("SAMPLE_N", "20"))


async def main():
    async with TelegramClient(session, api_id, api_hash) as client:
        for gid in VTC_GROUPS:
            try:
                ent = await client.get_entity(gid)
                title = getattr(ent, "title", str(gid))
            except Exception as e:
                print(f"\n### {gid} — impossible d'ouvrir : {e}")
                continue
            print(f"\n{'='*70}\n### {title}  ({gid})\n{'='*70}")
            count = 0
            async for msg in client.iter_messages(gid, limit=120):
                txt = (msg.text or "").strip()
                if not txt:
                    continue
                one = " / ".join(l.strip() for l in txt.splitlines() if l.strip())
                print(f"• {one[:200]}")
                count += 1
                if count >= N:
                    break


if __name__ == "__main__":
    asyncio.run(main())

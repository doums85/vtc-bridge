"""Liste tous les groupes/canaux actifs dans le compte Telegram et compare avec VTC_GROUPS."""
import asyncio, os, json
from telethon import TelegramClient
from telethon.tl.types import Channel, Chat
from dotenv import load_dotenv

load_dotenv()

API_ID   = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
VTC_GROUPS = [int(x) for x in os.environ.get("VTC_GROUPS", "").split(",") if x.strip()]

SESSION = "/tmp/vtc_read_tmp"

async def main():
    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        dialogs = await client.get_dialogs(limit=200)
        groups = []
        for d in dialogs:
            entity = d.entity
            if not isinstance(entity, (Channel, Chat)):
                continue
            cid = d.id  # déjà négatif pour les groupes
            name = d.name or ""
            # Mots-clés VTC
            kw = any(k in name.lower() for k in [
                "vtc", "chauffeur", "dispatch", "course", "driver", "taxi",
                "uber", "bmf", "broly", "ousboy", "elyax", "sirou", "fma",
                "bob", "g7", "lti", "course", "transfer"
            ])
            groups.append({
                "id": cid,
                "name": name,
                "in_config": cid in VTC_GROUPS,
                "vtc_keyword": kw,
            })

        # Trier : keyword VTC d'abord
        groups.sort(key=lambda x: (not x["vtc_keyword"], x["name"].lower()))

        print(f"\n{'ID':>20}  {'Dans config':^11}  {'Mot-clé VTC':^11}  Nom")
        print("-" * 80)
        for g in groups:
            mark = "✅" if g["in_config"] else "  "
            kw   = "🚗" if g["vtc_keyword"] else "  "
            print(f"{g['id']:>20}  {mark:^11}  {kw:^11}  {g['name']}")

        # Groupes VTC potentiels NON dans config
        nouveaux = [g for g in groups if g["vtc_keyword"] and not g["in_config"]]
        if nouveaux:
            print(f"\n⚠️  GROUPES VTC POTENTIELS NON CONFIGURÉS ({len(nouveaux)}) :")
            for g in nouveaux:
                print(f"  {g['id']}  {g['name']}")
        else:
            print("\n✅ Tous les groupes VTC détectés sont déjà dans la config.")

asyncio.run(main())

#!/usr/bin/env python3
"""Géocodage : une course immédiate passe devant les réservations en attente.

Un pic de réservations fait la queue sur le verrou unique de Nominatim (1 requête/s pour
tout le service). Sans priorité, une immédiate arrivée derrière attend tout le monde.
Nominatim est remplacé par un faux qui note l'ordre des requêtes ; aucun réseau.
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="vtc-geo-")
os.environ.update(TG_API_ID="1", TG_API_HASH="x", STATE_DIR=TMP)
sys.path.insert(0, HERE)
os.chdir(TMP)

import vtc_bridge as v  # noqa: E402

v._GEO_MIN_INTERVAL = 0.01          # on teste l'ordre, pas la cadence


def check(label, got, want):
    ok = got == want
    print(f"{'OK ' if ok else 'ÉCHEC'} {label}" + ("" if ok else f" (attendu {want!r}, obtenu {got!r})"))
    return ok


class FakeNominatim:
    """Faux aiohttp.ClientSession : enregistre l'ordre des adresses interrogées."""

    def __init__(self, hold: "asyncio.Event | None" = None):
        self.calls: list[str] = []
        self.hold = hold                 # si fourni, la 1re requête reste « en vol » jusqu'à son feu vert

    def get(self, url, params=None, headers=None):
        return _Resp(self, params["q"])


class _Resp:
    def __init__(self, owner, q):
        self.owner, self.q = owner, q

    async def __aenter__(self):
        first = not self.owner.calls
        self.owner.calls.append(self.q)
        if first and self.owner.hold is not None:
            await self.owner.hold.wait()
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def json(self):
        return [{"lat": "48.85", "lon": "2.35", "display_name": self.q}]


async def test_immediate_passes_batch() -> bool:
    v._GEO_CACHE.clear()
    hold = asyncio.Event()
    s = FakeNominatim(hold)
    # r1 tient le verrou (requête en vol) ; r2, r3 et r4 font la queue ; l'immédiate arrive en dernier.
    tasks = [asyncio.create_task(v.geocode(s, "resa 1", v.GEO_BATCH))]
    await asyncio.sleep(0.05)
    for n in (2, 3, 4):
        tasks.append(asyncio.create_task(v.geocode(s, f"resa {n}", v.GEO_BATCH)))
        await asyncio.sleep(0)
    tasks.append(asyncio.create_task(v.geocode(s, "immediat", v.GEO_URGENT)))
    await asyncio.sleep(0.05)
    hold.set()
    await asyncio.gather(*tasks)
    return check("l'immédiate passe devant les réservations en attente (pas devant celle en cours)",
                 s.calls, ["resa 1", "immediat", "resa 2", "resa 3", "resa 4"])


async def test_fifo_same_priority() -> bool:
    v._GEO_CACHE.clear()
    hold = asyncio.Event()
    s = FakeNominatim(hold)
    tasks = [asyncio.create_task(v.geocode(s, "a", v.GEO_URGENT))]
    await asyncio.sleep(0.05)
    for q in ("b", "c", "d"):
        tasks.append(asyncio.create_task(v.geocode(s, q, v.GEO_URGENT)))
        await asyncio.sleep(0)
    hold.set()
    await asyncio.gather(*tasks)
    return check("même priorité : ordre d'arrivée", s.calls, ["a", "b", "c", "d"])


async def test_cancelled_waiter_does_not_block() -> bool:
    v._GEO_CACHE.clear()
    hold = asyncio.Event()
    s = FakeNominatim(hold)
    t1 = asyncio.create_task(v.geocode(s, "x1", v.GEO_BATCH))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(v.geocode(s, "x2", v.GEO_URGENT))
    t3 = asyncio.create_task(v.geocode(s, "x3", v.GEO_BATCH))
    await asyncio.sleep(0.01)
    t2.cancel()                                   # l'attente la plus prioritaire disparaît
    hold.set()
    await asyncio.gather(t1, t3)
    try:
        await asyncio.wait_for(v.geocode(s, "x4", v.GEO_BATCH), 2)
        free = True
    except asyncio.TimeoutError:
        free = False
    ok = check("attente annulée sautée, x3 servie", s.calls[:2], ["x1", "x3"])
    ok &= check("verrou libre ensuite", free, True)
    return ok


async def test_cache_hit_skips_queue() -> bool:
    """Une adresse déjà résolue se sert tout de suite, même avec la queue pleine."""
    v._GEO_CACHE.clear()
    hold = asyncio.Event()
    s = FakeNominatim(hold)
    await v.geocode(FakeNominatim(), "connue", v.GEO_URGENT)      # remplit le cache
    t1 = asyncio.create_task(v.geocode(s, "occupe", v.GEO_BATCH))
    await asyncio.sleep(0.05)
    got = await asyncio.wait_for(v.geocode(s, "connue", v.GEO_BATCH), 1)
    hold.set()
    await t1
    return check("cache servi sans attendre le verrou", got is not None, True)


async def main() -> int:
    ok = True
    for fn in (test_immediate_passes_batch, test_fifo_same_priority,
               test_cancelled_waiter_does_not_block, test_cache_hit_skips_queue):
        print(f"\n— {fn.__name__}")
        ok &= await fn()
    print("\nTout passe." if ok else "\nDes tests échouent.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

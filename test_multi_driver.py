#!/usr/bin/env python3
"""Multi-chauffeurs, de bout en bout, sans réseau ni compte Telegram.

Deux chauffeurs reçoivent le MÊME message de groupe. Chacun est jugé sur sa position et
ses réglages ; les boutons, les réglages et les bilans ne se mélangent jamais.
Le bot, les sessions Telethon, le géocodage et le routage sont remplacés par des faux.
"""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="vtc-multi-")
os.environ.update(TG_API_ID="1", TG_API_HASH="x", BOT_TOKEN="1:test", STATE_DIR=TMP)
sys.path.insert(0, HERE)
os.chdir(TMP)                       # Telethon crée vtc_bot.session dans le dossier courant

import drivers as D                  # noqa: E402
import store                         # noqa: E402
import vtc_bridge as v               # noqa: E402

GROUP = -1001234567890
A_ID, B_ID = 111, 222
GARE_DE_LYON = (48.8443, 2.3744)
ROISSY = (49.0097, 2.5479)


# ───────────────────────────── Faux Telegram ─────────────────────────────
class FakeBot:
    """Enregistre les envois. Les ids de messages sont comptés PAR CHAT, comme chez Telegram :
    deux chauffeurs reçoivent donc un message portant le même id (cas qui casse un état global)."""

    def __init__(self):
        self.sent = []                       # (chat, texte, boutons)
        self._seq = {}

    async def send_message(self, chat, text, buttons=None, **kw):
        self._seq[chat] = self._seq.get(chat, 99) + 1
        self.sent.append((chat, text, buttons))
        return SimpleNamespace(id=self._seq[chat])

    async def edit_message(self, *a, **kw):
        pass

    async def send_file(self, *a, **kw):
        pass

    def to(self, chat):
        return [s for s in self.sent if s[0] == chat]


class FakeClient:
    def __init__(self):
        self.posted = []                     # (groupe, texte)

    async def send_message(self, gid, text, reply_to=None):
        self.posted.append((gid, text))


class Ev:
    """Faux évènement : bouton (data) ou message (texte)."""

    def __init__(self, sender_id, data=None, text="", message_id=100):
        self.sender_id, self.message_id = sender_id, message_id
        self.data = data.encode() if isinstance(data, str) else data
        self.raw_text = text
        self.is_private = True
        self.message = SimpleNamespace(geo=None, reply_to=None, media=None)
        self.answers, self.edits, self.responses = [], [], []

    async def answer(self, text=None, alert=False):
        self.answers.append(text)

    async def edit(self, text, buttons=None, **kw):
        self.edits.append((text, buttons))

    async def respond(self, text, buttons=None, **kw):
        self.responses.append((text, buttons))


async def fake_geocode(session, query):
    q = query.lower()
    if "gare de lyon" in q or "75012" in q:
        return (*GARE_DE_LYON, "Gare de Lyon, 75012 Paris")
    if "roissy" in q or "cdg" in q:
        return (*ROISSY, "Aéroport Charles de Gaulle, 95700 Roissy")
    return (48.8566, 2.3522, "Paris")


async def fake_route(session, src, dst, when_hour=None):
    """30 km/h, route = 1,3 × vol d'oiseau. Déterministe : le temps dépend de la POSITION."""
    d = D.haversine_km(src["lat"], src["lon"], dst[0], dst[1]) * 1.3 * 1000
    return d / (30 / 3.6), d


# Le vrai bot (créé à l'import) porte les handlers de boutons et de commandes : on les
# récupère pour les appeler à la main, puis on remplace `v.bot` par un faux pour les envois.
REAL_BOT = v.bot
HANDLERS = {cb.__name__: cb for cb, _ in REAL_BOT.list_event_handlers()}
BOT = FakeBot()
v.bot = BOT
v.geocode, v.route_eta = fake_geocode, fake_route


def make_driver(did, name, pos) -> v.DriverRT:
    store.upsert_driver(did, name=name, groups=[GROUP])
    drv = v.DriverRT(D.profile_from_row(store.get_driver_row(did)))
    drv.client = FakeClient()
    drv.self_id = did
    v.DRIVERS[did] = drv
    v.save_position(drv, *pos)
    return drv


def reset_dedup(*drvs):
    for d in drvs:
        d.dup_seen.clear()
        d.seen_courses.clear()


def check(label, got, want):
    ok = got == want
    print(f"{'OK ' if ok else 'ÉCHEC'} {label}" + ("" if ok else f" (attendu {want!r}, obtenu {got!r})"))
    return ok


def alerts(chat):
    return [s for s in BOT.to(chat) if "Course proche" in s[1] or "Réservation" in s[1]]


async def run() -> bool:
    ok = True
    store.init(os.path.join(TMP, "courses.db"))
    on_button, on_command = HANDLERS["on_button"], HANDLERS["on_command"]

    a = make_driver(A_ID, "Alice", GARE_DE_LYON)          # sur place
    b = make_driver(B_ID, "Bruno", ROISSY)                # à 23 km
    imm = "Immédiat / Gare de Lyon / Paris 8 / 30€"

    print("\n— course immédiate : chacun selon sa position")
    for d in (a, b):
        await v._handle_course(d, imm, GROUP, 5000)
    ok &= check("Alice (sur place) alertée", len(alerts(A_ID)), 1)
    ok &= check("Bruno (à Roissy) pas alerté", len(alerts(B_ID)), 0)
    ok &= check("alerte inscrite au journal d'Alice", len(store.recent(A_ID)), 1)
    ok &= check("rien au journal de Bruno", len(store.recent(B_ID)), 0)

    print("\n— Bruno se rapproche : il reçoit à son tour")
    v.save_position(b, 48.8470, 2.3760)
    reset_dedup(a, b)
    await v._handle_course(b, imm, GROUP, 5001)
    ok &= check("Bruno alerté après déplacement", len(alerts(B_ID)), 1)

    print("\n— prix minimum")
    reset_dedup(a, b)
    a.p.set("min_price", 40)
    n = len(alerts(A_ID))
    await v._handle_course(a, "Immédiat / Gare de Lyon / Paris 8 / 30€", GROUP, 5002)
    ok &= check("30 € < 40 € : ignorée", len(alerts(A_ID)), n)
    await v._handle_course(a, "Immédiat / Gare de Lyon / Paris 9 / 45€", GROUP, 5003)
    ok &= check("45 € >= 40 € : alertée", len(alerts(A_ID)), n + 1)
    a.p.reset("min_price")

    print("\n— distance maximum jusqu'au départ")
    reset_dedup(a, b)
    v.save_position(a, 48.8700, 2.3744)                   # ≈ 2,9 km à vol d'oiseau, ~3,7 km de route
    a.p.set("threshold_min", 30)
    n = len(alerts(A_ID))
    a.p.set("max_pickup_km", 2)
    await v._handle_course(a, "Immédiat / Gare de Lyon / Paris 8 / 35€", GROUP, 5004)
    ok &= check("route > 2 km : ignorée", len(alerts(A_ID)), n)
    a.p.set("max_pickup_km", 10)
    reset_dedup(a)
    await v._handle_course(a, "Immédiat / Gare de Lyon / Paris 8 / 35€", GROUP, 5004)
    ok &= check("route <= 10 km : alertée", len(alerts(A_ID)), n + 1)
    a.p.reset("max_pickup_km")
    a.p.reset("threshold_min")
    v.save_position(a, *GARE_DE_LYON)

    print("\n— réservations : tarif, prix, rayon")
    resa = "Réservation 8h30 / Gare de Lyon / Roissy / 120€"
    reset_dedup(a, b)
    na, nb = len(alerts(A_ID)), len(alerts(B_ID))
    for d in (a, b):
        await v._handle_course(d, resa, GROUP, 6000)
    ok &= check("réservation 120 € / 30 km : Alice alertée", len(alerts(A_ID)), na + 1)
    ok &= check("réservation 120 € / 30 km : Bruno alerté (aucun rayon)", len(alerts(B_ID)), nb + 1)
    reset_dedup(a, b)
    v.save_position(b, *ROISSY)
    b.p.set("resa_radius_km", 10)                         # Roissy -> Gare de Lyon ≈ 23 km
    a.p.set("resa_radius_km", 10)                         # Alice est sur place
    na, nb = len(alerts(A_ID)), len(alerts(B_ID))
    for d in (a, b):
        await v._handle_course(d, resa, GROUP, 6001)
    ok &= check("rayon 10 km : Alice (0 km) alertée", len(alerts(A_ID)), na + 1)
    ok &= check("rayon 10 km : Bruno (23 km) pas alerté", len(alerts(B_ID)), nb)
    a.p.reset("resa_radius_km")
    b.p.reset("resa_radius_km")
    reset_dedup(a, b)
    a.p.set("min_price", 200)
    na = len(alerts(A_ID))
    await v._handle_course(a, resa, GROUP, 6002)
    ok &= check("réservation 120 € < prix min 200 € : ignorée", len(alerts(A_ID)), na)
    a.p.reset("min_price")
    a.p.set("eurkm_min", 9)
    reset_dedup(a)
    await v._handle_course(a, resa, GROUP, 6003)
    ok &= check("réservation sous le €/km minimum (9) : ignorée", len(alerts(A_ID)), na)
    a.p.reset("eurkm_min")

    print("\n— boutons : chaque chauffeur n'agit que sur SES alertes")
    reset_dedup(a, b)
    v.save_position(b, 48.8470, 2.3760)
    for d in (a, b):
        d.pending_alerts.clear()
        d.alert_ctx.clear()
    BOT.sent.clear()
    BOT._seq.clear()
    await v._handle_course(a, "Immédiat / Gare de Lyon / Paris 8 / 51€", GROUP, 7000)
    await v._handle_course(b, "Immédiat / Gare de Lyon / Paris 8 / 51€", GROUP, 7000)
    ida, idb = list(a.pending_alerts)[0], list(b.pending_alerts)[0]
    cida, cidb = a.pending_alerts[ida], b.pending_alerts[idb]
    ok &= check("même id de message chez les deux (collision réelle)", ida == idb, True)
    ok &= check("deux courses distinctes en base", cida != cidb, True)
    # Bruno refuse SA carte : celle d'Alice reste intacte.
    await on_button(Ev(B_ID, "x", message_id=idb))
    ok &= check("Bruno refuse : sa course passe à « passée »", store.get(cidb)["status"], "passée")
    ok &= check("Alice : sa course reste « proposée »", store.get(cida)["status"], "proposée")
    ok &= check("Alice : son alerte reste active", ida in a.pending_alerts, True)
    # Alice accepte, choisit « SP » : la réponse part de SON compte, dans SON groupe.
    ev = Ev(A_ID, f"a|{GROUP}|7000", message_id=ida)
    await on_button(ev)
    ok &= check("« Accepter » propose des temps", len(ev.edits) == 1 and "Meilleur temps" in ev.edits[0][0], True)
    await on_button(Ev(A_ID, f"s|{GROUP}|7000|SP", message_id=ida))
    ok &= check("réponse postée par le compte d'Alice", a.client.posted, [(GROUP, "SP")])
    ok &= check("rien posté par le compte de Bruno", b.client.posted, [])
    ok &= check("course d'Alice « répondue »", store.get(cida)["status"], "répondue")
    # Un inconnu ne peut rien faire.
    ev = Ev(999, f"a|{GROUP}|7000", message_id=ida)
    await on_button(ev)
    ok &= check("inconnu : « Non autorisé »", ev.answers, ["Non autorisé"])
    # Bruno ne peut pas valider la course d'Alice en devinant son id.
    ev = Ev(B_ID, f"o|{cida}|1", message_id=500)
    await on_button(ev)
    ok &= check("Bruno ne peut pas valider la course d'Alice", store.get(cida)["status"], "répondue")

    print("\n— réglages depuis le bot")
    await on_button(Ev(A_ID, "st|min_price|+"))
    ok &= check("➕ prix minimum : 0 -> 5 €", a.cfg("min_price"), 5)
    ok &= check("persisté en base", D.profile_from_row(store.get_driver_row(A_ID)).get("min_price"), 5)
    ok &= check("Bruno inchangé",
                (b.cfg("min_price"), D.profile_from_row(store.get_driver_row(B_ID)).get("min_price")), (0, 0))
    await on_command(Ev(A_ID, text="/prixmin 35"))
    ok &= check("/prixmin 35",
                (a.cfg("min_price"), D.profile_from_row(store.get_driver_row(A_ID)).get("min_price")), (35, 35))
    await on_button(Ev(A_ID, "st|max_pickup_km|in"))
    ok &= check("✍️ Saisir : prochain message = valeur", a.awaiting, (0, "set:max_pickup_km"))
    await on_command(Ev(A_ID, text="12,5 km"))
    ok &= check("saisie « 12,5 km » enregistrée", a.cfg("max_pickup_km"), 12.5)
    ok &= check("saisie consommée", a.awaiting, None)
    await on_button(Ev(A_ID, "st|max_pickup_km|in"))
    await on_command(Ev(A_ID, text="loin"))
    ok &= check("saisie illisible refusée, valeur gardée", a.cfg("max_pickup_km"), 12.5)
    ok &= check("saisie rouverte pour réessayer", a.awaiting, (0, "set:max_pickup_km"))
    ev = Ev(A_ID, text="/reglages")
    await on_command(ev)
    ok &= check("/reglages affiche l'écran", len(ev.responses) == 1 and "Réglages" in ev.responses[0][0], True)
    ok &= check("résumé des règles dans l'écran",
                "Immédiates" in ev.responses[0][0] and "35 €" in ev.responses[0][0], True)
    await on_button(Ev(A_ID, "st|min_price|rst"))
    ok &= check("↩︎ défaut", a.cfg("min_price"), 0)
    ev = Ev(A_ID, "st|nimporte_quoi")
    await on_button(ev)
    ok &= check("réglage inconnu refusé", ev.answers, ["Réglage inconnu"])
    await on_command(Ev(B_ID, text="/prixmin 9999"))
    ok &= check("valeur hors bornes refusée", b.cfg("min_price"), 0)

    print("\n— partage de position : durée limitée ou sans fin")
    on_location = HANDLERS["on_bot_location_new"]

    def geo_event(period):
        ev = Ev(A_ID)
        ev.message = SimpleNamespace(geo=SimpleNamespace(lat=48.85, long=2.35), reply_to=None,
                                     media=SimpleNamespace(period=period) if period else None)
        return ev

    ev = geo_event(8 * 3600)
    await on_location(ev)
    ok &= check("partage 8 h : rappel programmé", a.pos_expiry_task is not None, True)
    ok &= check("partage 8 h : message annonce la durée", "8 h" in ev.responses[0][0], True)
    ev = geo_event(v.LIVE_INDEFINITE)
    await on_location(ev)
    ok &= check("partage sans fin : ancien rappel annulé, aucun nouveau", a.pos_expiry_task, None)
    ok &= check("partage sans fin : message « sans limite »", "sans limite" in ev.responses[0][0], True)
    ev = geo_event(None)
    await on_location(ev)
    ok &= check("position ponctuelle : le chauffeur est prévenu", "ponctuelle" in ev.responses[0][0], True)
    ok &= check("position ponctuelle : aucun rappel", a.pos_expiry_task, None)
    ok &= check("position enregistrée", v.load_position(a)["lat"], 48.85)

    print("\n— inconnu en message privé")
    ev = Ev(777, text="/resume")
    await on_command(ev)
    ok &= check("réponse « bot privé » avec son id", len(ev.responses) == 1 and "777" in ev.responses[0][0], True)
    ev2 = Ev(777, text="/resume")
    await on_command(ev2)
    ok &= check("pas de spam : une seule réponse", ev2.responses, [])

    print("\n— bilans séparés")
    store.set_status(cida, "obtenue", A_ID)
    ra, rb = v.build_report(a, "tout"), v.build_report(b, "tout")
    ok &= check("bilan d'Alice : 51 € brut", "51 €" in ra, True)
    ok &= check("bilan de Bruno : aucune course réalisée", "Aucune course réalisée" in rb, True)
    return bool(ok)


def main() -> int:
    ok = asyncio.run(run())
    print("\n" + ("Tout passe." if ok else "Au moins un test échoue."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

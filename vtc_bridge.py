#!/usr/bin/env python3
"""
Pont VTC <-> Hermès (Telethon / compte utilisateur).

Rôle :
  1. Lit les groupes VTC où tu es simple membre (impossible avec le Bot API de Hermès).
  2. Suit ta position en direct (live location Telegram partagée dans le groupe-relais).
  3. Pour chaque course postée : extrait l'adresse de prise en charge, géocode (Nominatim),
     calcule l'ETA en voiture (OSRM). Si ETA <= SEUIL (8 min) -> escalade vers Hermès.
  4. Canal retour : quand Hermès poste "[SEND <group_id>] <texte>" dans le relais,
     le pont envoie ce texte dans le vrai groupe VTC (depuis TON compte).

Dépendances : pip install telethon aiohttp python-dotenv
Login : au 1er lancement, Telethon demande ton numéro + le code reçu par Telegram.

⚠️ Automatiser un compte utilisateur est en zone grise des CGU Telegram.
   Reste en lecture + envoi VALIDÉ manuellement (le point 4 n'envoie que ce que
   tu as approuvé via Hermès). N'en fais pas un auto-répondeur massif : c'est ça
   qui fait bannir un compte.
"""

from __future__ import annotations

import os
import re
import html
import json
import io
import time
import unicodedata
import asyncio
import logging

import aiohttp
from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault, DocumentAttributeFilename

import store

load_dotenv()

# ─────────────────────────── Config ───────────────────────────
API_ID   = int(os.environ["TG_API_ID"])            # my.telegram.org
API_HASH = os.environ["TG_API_HASH"]
SESSION  = os.environ.get("TG_SESSION", "vtc_session")

# ids des groupes VTC (négatifs, ex : -1001234567890). Sépare par des virgules.
VTC_GROUPS = [int(x) for x in os.environ["VTC_GROUPS"].split(",") if x.strip()]
# groupe privé relais où le bot Hermès est admin (privacy OFF) ET où TU partages ta live location.
FEED_GROUP = int(os.environ["FEED_GROUP"])
# mention du bot Hermès (ex "@mon_hermes_bot") pour déclencher son traitement des alertes.
BOT_MENTION = os.environ.get("BOT_MENTION", "").strip()

THRESHOLD_SECONDS = int(os.environ.get("THRESHOLD_SECONDS", 8 * 60))   # 8 min : au-delà, on ignore
SP_UNDER_SECONDS  = int(os.environ.get("SP_UNDER_SECONDS", 5 * 60))    # < 5 min : la réponse est "SP"
# Réservations (course pour plus tard) : on notifie sur la rentabilité, pas la proximité.
EURKM_MIN  = float(os.environ.get("EURKM_MIN", "1.5"))                 # seuil €/km d'une réservation
RESA_REPLY = os.environ.get("RESA_REPLY", "ok").strip()               # réponse suggérée pour une réservation
# Code couleur rentabilité : >= GOOD 🟢, >= MID 🟠, sinon 🔴
EURKM_GOOD = float(os.environ.get("EURKM_GOOD", "2.0"))
EURKM_MID  = float(os.environ.get("EURKM_MID", "1.5"))
# Boutons de réponse rapide proposés sous chaque alerte (en plus de la suggestion).
QUICK_REPLIES = [q.strip() for q in os.environ.get("QUICK_REPLIES", "SP,5,10,15,20").split(",") if q.strip()]
# Sans réponse au-delà de ce délai, l'alerte est marquée "expirée" (boutons neutralisés).
EXPIRE_SECONDS = int(os.environ.get("EXPIRE_SECONDS", 15 * 60))
# Fenêtre de déduplication inter-groupes : une même course repostée dans un autre groupe
# sous ce délai n'est notifiée qu'une fois (évite doubles alertes / doubles réponses).
DEDUP_WINDOW_SECONDS = int(os.environ.get("DEDUP_WINDOW_SECONDS", str(EXPIRE_SECONDS)))
# Repli quand l'empreinte géographique est muette (ni code postal, ni prix lisible) :
# même texte reposté dans un autre groupe sous ce délai = une seule alerte. Fenêtre
# volontairement courte : un repost inter-groupes arrive en quelques secondes, alors que
# deux courses distinctes peuvent être formulées à l'identique plus tard dans la journée.
DEDUP_TEXT_WINDOW_SECONDS = int(os.environ.get("DEDUP_TEXT_WINDOW_SECONDS", 120))
POSITION_STALE_SECONDS = int(os.environ.get("POSITION_STALE_SECONDS", 86400))  # 24h
# Rappel envoyé ce délai AVANT l'expiration de la position en direct (8 h) pour relancer le partage.
LIVE_REMIND_BEFORE = int(os.environ.get("LIVE_REMIND_BEFORE", 15 * 60))
# Après l'heure prévue d'une réservation + ce délai, le bot demande « effectuée ? » (course finie).
RESA_PROMPT_AFTER = int(os.environ.get("RESA_PROMPT_AFTER", 90 * 60))
# Heure (0-23) d'envoi automatique du récap des dernières 24 h. "off"/vide = désactivé.
_dsh = os.environ.get("DAILY_SUMMARY_HOUR", "5").strip().lower()
DAILY_SUMMARY_HOUR = int(_dsh) if _dsh.isdigit() else None

HERE_KEY = os.environ.get("HERE_KEY", "").strip()

import datetime as _dt

def _traffic_multiplier(hour: "float | None" = None, weekday: "int | None" = None) -> float:
    """Facteur de ralentissement estimé selon l'heure Paris.
    hour/weekday : pour une réservation, l'heure PRÉVUE de la course (sinon maintenant)."""
    now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=2)))
    h   = now.hour + now.minute / 60 if hour is None else hour
    wd  = now.weekday() if weekday is None else weekday
    if wd >= 5:           return 1.15
    if 7.5  <= h < 9.5:  return 1.8
    if 9.5  <= h < 11.0: return 1.4
    if 12.0 <= h < 14.0: return 1.3
    if 17.0 <= h < 20.0: return 1.9
    if 20.0 <= h < 22.0: return 1.3
    return 1.1


def _departure_iso(hour: float) -> str:
    """Datetime ISO (Europe/Paris) de la prochaine occurrence de `hour` — pour HERE (trafic prédictif)."""
    tz = _dt.timezone(_dt.timedelta(hours=2))
    now = _dt.datetime.now(tz)
    h, mn = int(hour), int(round((hour - int(hour)) * 60))
    dep = now.replace(hour=min(h, 23), minute=min(mn, 59), second=0, microsecond=0)
    if dep < now:
        dep += _dt.timedelta(days=1)     # heure déjà passée aujourd'hui -> demain
    return dep.isoformat(timespec="seconds")

OSRM_URL      = os.environ.get("OSRM_URL", "https://router.project-osrm.org")
NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org")
# Biais géographique du géocodage : "lon_min,lat_max,lon_max,lat_min" (ex. Île-de-France).
# Fortement recommandé pour éviter qu'une adresse floue matche à l'autre bout du monde.
GEO_VIEWBOX = os.environ.get("GEO_VIEWBOX", "").strip()
USER_AGENT  = os.environ.get("USER_AGENT", "vtc-bridge/1.0 (contact: you@example.com)")

STATE_DIR = os.environ.get("STATE_DIR", "state")
POS_FILE  = os.path.join(STATE_DIR, "position.json")
COURSES_DB = os.path.join(STATE_DIR, "courses.db")
os.makedirs(STATE_DIR, exist_ok=True)

# Part reversée à l'apporteur, en fraction (0.15 = 15%). Sert au calcul commission/net
# quand le message ne précise rien. Une commission explicite dans la course la remplace.
COMMISSION_RATE = float(os.environ.get("COMMISSION_RATE", "0") or "0")
# Agenda macOS (synchronisé iCloud -> iPhone) où ajouter les réservations obtenues.
CALENDAR_NAME = os.environ.get("CALENDAR_NAME", "Calendrier").strip()

# Boutons inline : bot DÉDIÉ (≠ bot Hermès) + ton id Telegram (DM des alertes).
# Si BOT_TOKEN vide -> fallback : alertes texte dans le relais pour Hermès (comportement historique).
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "").strip()
NOTIFY_USER_ID = int(os.environ.get("NOTIFY_USER_ID", "0") or "0")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vtc")
logging.getLogger("fontTools").setLevel(logging.ERROR)   # PDF : coupe le bruit du sous-échantillonnage

client = TelegramClient(SESSION, API_ID, API_HASH)          # session utilisateur (lit les groupes, poste les réponses)
bot = TelegramClient("vtc_bot", API_ID, API_HASH) if BOT_TOKEN else None  # bot dédié aux boutons
_seen_courses: set[tuple[int, int]] = set()   # dédup (groupe, message id) : ne traite pas 2× le même message
_pending_alerts: dict[int, int] = {}          # id message alerte DM -> id course en base (alertes encore actives)
_dup_seen: dict[str, float] = {}              # empreinte de course -> ts : dédup inter-groupes (même course, autre groupe)
GROUP_NAMES: dict[int, str] = {}              # id groupe -> nom lisible (résolu au démarrage)
BOT_SELF_ID: int = 0                           # id du bot (pour ne pas doubler /note dans son DM)
CLIENT_SELF_ID: int = 0                         # id de TON compte (= tes Messages sauvegardés)
_pending_note: dict[int, str] = {}            # id message sélecteur -> texte de note en attente de course
_awaiting: dict[int, tuple] = {}              # id user -> (id course, champ) : prochain message = édition (note/prix/comm)


def group_name(gid: int) -> str:
    return GROUP_NAMES.get(gid, str(gid))


# ─────────────────────── Position live ────────────────────────
def save_position(lat: float, lon: float) -> None:
    with open(POS_FILE, "w") as f:
        json.dump({"lat": lat, "lon": lon, "ts": time.time()}, f)
    log.info("Position mise à jour : %.5f, %.5f", lat, lon)


def load_position() -> dict | None:
    try:
        with open(POS_FILE) as f:
            pos = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if time.time() - pos["ts"] > POSITION_STALE_SECONDS:
        log.warning("Position périmée (%.0fs) — partage-tu bien ta position en direct ?",
                    time.time() - pos["ts"])
        return None
    return pos


# ─────────── Parsing des courses VTC (langage réel des groupes) ───────────
# Format type :  <statut/heure> / <DÉPART> / <ARRIVÉE> / <prix>
# On extrait le DÉPART (= prise en charge) pour mesurer la proximité.
# Ce qui reste ambigu part chez Hermès (LLM) via escalate_raw.

NOISE = {"ok", "ok sp", "sp", "pv", "pv stp", "privé", "privé stp", "prive",
         "prive stp", "v", "oui", "non", "dispo", "+", "sp stp"}

PRICE_RE  = re.compile(r"(\d+[.,]?\d*\s*€|\bnet\b|\bpab\b|\d+\s*(?:euros?|eur)\b)", re.I)
STATUT_RE = re.compile(
    r"\b(imm[ée]diate?ment?|imm[ée]diat|imediat|immediat|r[ée]servation|resa|relance|"
    r"dans\s+\d+\s*min|aujourd|demain|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|"
    r"van|break|mono|[ée]co|pax|si[èe]ge|bagage|paiement|carte|lien|paypal|\bcb\b)\b", re.I)
TIME_RE   = re.compile(r"^\s*\d{1,2}\s*[h:]\s*\d{0,2}\s*$")
# marqueur de départ en début de segment (après symboles/emoji éventuels)
DEP_MARK  = re.compile(r"^(?:adresse\s+(?:de\s+)?)?(?:d[ée]part|pec|prise\s+en\s+charge|de)\s*[:\-]\s*(.+)", re.I)

# Raccourcis / lieux fréquents -> adresse géocodable
LANDMARKS = [
    (re.compile(r"\bcdg\b|\broissy\b|charles\s*de\s*gaulle", re.I), "Aéroport Charles de Gaulle, 95700 Roissy"),
    (re.compile(r"\borly\b", re.I),                "Aéroport d'Orly, 94390 Orly"),
    (re.compile(r"\bgdl\b|\bgare de lyon\b", re.I), "Gare de Lyon, 75012 Paris"),
    (re.compile(r"\bmontparnasse\b", re.I),        "Gare Montparnasse, 75015 Paris"),
    (re.compile(r"\bgare du nord\b", re.I),        "Gare du Nord, 75010 Paris"),
    (re.compile(r"\bgare de l.?est\b", re.I),      "Gare de l'Est, 75010 Paris"),
    (re.compile(r"\baust?erlitz\b", re.I),         "Gare d'Austerlitz, 75013 Paris"),
    (re.compile(r"\bstade de france\b", re.I),     "Stade de France, 93200 Saint-Denis"),
    (re.compile(r"\bdisney\b", re.I),              "Disneyland Paris, 77700 Marne-la-Vallée"),
    (re.compile(r"\bvillepinte\b", re.I),          "Parc des Expositions, 93420 Villepinte"),
]

_SYMBOLS = " *•▪️➖\t✖️🟢🔴✅🚩➡️→📍🆔🚨🕰🕦💵💶💳🧍🧳👉⌚🔱🥇🏟🏁"


def _arr(m: "re.Match") -> str:
    n = int(m.group(1))
    return f"750{n:02d} Paris" if 1 <= n <= 20 else m.group(0)


def expand_shortcuts(seg: str) -> str:
    s = re.sub(r"\bP\s?0?(\d{1,2})\b(?!\d)", _arr, seg)               # P10 -> 75010 Paris
    s = re.sub(r"\bParis\s?0?(\d{1,2})\b(?!\d)", _arr, s, flags=re.I)  # Paris 14 -> 75014 Paris
    for rx, rep in LANDMARKS:
        s = rx.sub(rep, s)
    # Abréviations fréquentes dans les adresses françaises
    s = re.sub(r"\bDr\.?\b", "Docteur", s)
    s = re.sub(r"\bSt\.?\b", "Saint", s, flags=re.I)
    s = re.sub(r"\bAv\.?\b", "Avenue", s, flags=re.I)
    s = re.sub(r"\bBd\.?\b", "Boulevard", s, flags=re.I)
    s = re.sub(r"\bPl\.?\b", "Place", s, flags=re.I)
    return s.strip(_SYMBOLS + ".,-–—/()")


# tokens de tête à retirer d'un segment (statut, heure, date, code, véhicule…)
LEAD_JUNK = re.compile(
    r"^(?:"
    # tokens "mots" (avec frontière de mot pour ne pas rogner un nom de lieu : Vanves, Maison…)
    r"(?:imm[ée]diate?ment?|imm[ée]diat\w*|imediat\w*|immediat\w*|course\s+imm[ée]diate?|"
    r"r[ée]servation|resa|relance|dans\s+\d+\s*min\w*|heure|train\s+arriv\w*|à|"
    r"aujourd['’]?hui|demain|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|"
    r"janvier|février|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|décembre|"
    r"van|break|mono|[ée]co|c(?=[0-9a-z]*\d)[0-9a-z]{5,})\b"  # dernier = codes course type C2JR1085403 (exige un chiffre : n'efface pas Courbevoie, Clichy…)
    # tokens numériques / horaires / symboles (pas de frontière de mot)
    r"|\d{1,2}\s*[h:]\s*\d{0,2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{1,2}(?!\d)"
    r"|[\s❌✅🟢🔴🔥.,:;\-–—]"
    r")+", re.I)

# métadonnées (pax, bagages, distance) — jamais un lieu de départ
META_RE = re.compile(r"\b(personnes?|passagers?|pax|km|bagages?|valises?)\b", re.I)


def is_course(text: str) -> bool:
    """Filtre le bruit : ne garde que ce qui ressemble à une course."""
    t = text.strip().lower()
    if t in NOISE or len(t) < 8:
        return False
    return bool(PRICE_RE.search(text) or DEP_MARK.search(text) or STATUT_RE.search(text))


def _clean(seg: str) -> str:
    """Retire les tokens de tête (statut/heure/date/code) d'un segment."""
    s = seg.strip(_SYMBOLS)
    prev = None
    while prev != s:
        prev = s
        s = LEAD_JUNK.sub("", s).strip(_SYMBOLS + " ")
    return s


# Mots-étiquettes / bruit de mise en forme : JAMAIS un lieu (ex. « 📍 Départ » seul sur sa ligne).
_NON_LOC = re.compile(
    r"^(?:d[ée]part|arriv[ée]e?|destination|pec|prise\s+en\s+charge|"
    r"adresse(?:\s+(?:de\s+)?(?:d[ée]part|d[ée]but|fin|arriv[ée]e?))?|"
    r"de|[àa]|vers|course(?:\s+disponible)?|disponible|standard|berline|postulez.*|postuler.*)\s*$",
    re.I)
# Marqueur SEUL sur sa ligne -> l'adresse est le segment suivant.
_DEP_MARKER = re.compile(r"^(?:adresse\s+)?(?:de\s+)?(?:d[ée]part|pec|prise\s+en\s+charge)\s*[:\-]?\s*$", re.I)
_ARR_MARKER = re.compile(r"^(?:adresse\s+)?(?:d['e]\s*)?(?:arriv[ée]e?|destination|fin)\s*[:\-]?\s*$", re.I)


def _is_location(s: str) -> bool:
    s = s.strip()
    if len(s) < 3 or _NON_LOC.match(s) or PRICE_RE.search(s) or STATUT_RE.search(s) or META_RE.search(s):
        return False
    # accepte un code postal seul (ex. 75016) ou tout segment contenant des lettres
    return bool(re.search(r"\d{5}", s)) or any(c.isalpha() for c in s)


def extract_pickup(text: str) -> list[str]:
    """Adresses de PRISE EN CHARGE candidates, la meilleure d'abord."""
    raw = re.split(r"/|\n|->|➝|➡️?|→|>|--+|—", text)
    segs = [s.strip(_SYMBOLS) for s in raw if s.strip(_SYMBOLS)]
    candidates: list[str] = []

    # 0) marqueur de départ SEUL sur sa ligne (📍 Départ \n 92140 Clamart) -> adresse = ligne suivante
    for i, s in enumerate(segs):
        if _DEP_MARKER.match(s) and i + 1 < len(segs):
            candidates.append(segs[i + 1])

    # 1) marqueur de départ explicite (📍 DÉPART : / De :)
    for s in segs:
        m = DEP_MARK.search(s.strip(_SYMBOLS))
        if m and len(m.group(1).strip()) > 2:
            candidates.append(m.group(1).strip())

    # 2) motif "X pour Y" -> départ = X (dernier bout avant "pour")
    m = re.search(r"(.+?)\bpour\b", text, re.I)
    if m:
        candidates.append(re.split(r"/|\n", m.group(1))[-1])

    # 3) positionnel : chaque segment (départ = avant un éventuel "pour")
    candidates += segs

    # nettoyage (statut de tête) + coupe "pour Y" + expansion raccourcis + filtre + dédup
    seen, out = set(), []
    for c in candidates:
        c = re.split(r"\bpour\b", c, maxsplit=1)[0]      # garde le départ, coupe l'arrivée
        c = expand_shortcuts(_clean(c))
        if c and _is_location(c) and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out[:4]


# ─────────── Réservations : rentabilité (€/km) plutôt que proximité ───────────
IMMEDIATE_RE = re.compile(r"imm[ée]diat|imediat|immediat", re.I)
RESA_RE = re.compile(
    r"r[ée]serv|\bresa\b|aujourd|demain|"
    r"lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|"
    r"\b\d{1,2}/\d{1,2}\b|\b\d{1,2}\s?h\s?\d{0,2}\b|\b\d{1,2}:\d{2}\b", re.I)
ARR_MARK = re.compile(r"^(?:adresse\s+(?:d['e]\s*)?)?(?:arriv\w*|destination|fin|à)\s*[:\-]\s*(.+)", re.I)
KM_RE = re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*km\b", re.I)


def is_reservation(text: str) -> bool:
    """Réservation = course planifiée (heure/date/jour) et NON immédiate."""
    if IMMEDIATE_RE.search(text):
        return False
    return bool(RESA_RE.search(text))


# Un message contient parfois PLUSIEURS réservations (C1/C2, 1)/2), séparées par des
# runs de points/tirets). On les disloque pour émettre une alerte PAR course.
_SEP_RUN = re.compile(r"[.\-—–_·•]{4,}")                       # ..... ———— ____
_LIST_MARK = re.compile(r"(?=(?:^|\s)(?:[Cc]\s?[1-9]\b|[1-9]\s?\)))")   # C1, C 1, 1), 2)


# Marqueurs de début d'une nouvelle course (au-delà des séparateurs / listes).
_ANCHOR = re.compile(
    r"\b(?:imm[ée]diat\w*|imediat\w*|immediat\w*|d[eè]s\s+que\s+possible|demain|aujourd\w*|"
    r"r[ée]serv\w*|relance|\d{1,2}\s*[h:]\s*\d{0,2})\b", re.I)
# Jeton de prix EXIGEANT un chiffre (20net, 40€, 50 EURO) — pour le découpage.
_PRICE_TOK = re.compile(r"\d{1,3}(?:[.,]\d{1,2})?\s*(?:€|euros?|eur\b|net\b|pab\b)", re.I)


def _split_by_anchors(text: str) -> list[str]:
    """Découpe implicite : coupe avant un marqueur de nouvelle course (heure, 'immédiat',
    'des que possible', 'demain'…) s'il y a eu un PRIX depuis le précédent ET un prix après
    (⇒ deux vraies courses) — mais PAS pour un aller/retour (un seul prix)."""
    anchors = [m.start() for m in _ANCHOR.finditer(text)]
    if len(anchors) < 2:
        return [text]
    prices = [m.start() for m in _PRICE_TOK.finditer(text)]
    cuts = []
    for i in range(1, len(anchors)):
        before = any(anchors[i - 1] < p < anchors[i] for p in prices)
        after = any(p >= anchors[i] for p in prices)
        if before and after:
            cuts.append(anchors[i])
    if not cuts:
        return [text]
    bounds = [0] + cuts + [len(text)]
    return [text[bounds[i]:bounds[i + 1]].strip() for i in range(len(bounds) - 1)]


def split_courses(text: str) -> list[str]:
    """Liste des courses d'un message : >=2 si multi-réservations détectées, sinon [text]."""
    chunks = [c for c in _SEP_RUN.split(text) if c.strip()]        # 1) séparateurs explicites
    segs = []
    for c in chunks:                                              # 2) marqueurs de liste C1 / 1)
        subs = [s for s in _LIST_MARK.split(c) if s.strip()]
        subs = subs if len(subs) > 1 else [c]
        for s in subs:                                           # 3) découpe implicite (marqueurs + prix)
            segs.extend(_split_by_anchors(s))
    segs = [s.strip() for s in segs if _TIME_EXTRACT.search(s) or _PRICE_TOK.search(s)]
    return segs if len(segs) >= 2 else [text]


STREET_RE = re.compile(
    r"\b(rue|av|avenue|bd|boulevard|place|pl|impasse|all[ée]es?|chemin|quai|route|cours|square|passage)\b", re.I)
VAN_WORD = re.compile(r"\bvan\b", re.I)


def requires_van(text: str) -> bool:
    """True si la course exige un VAN (véhicule). Évite les faux positifs (Rue Van Gogh, Vanves)."""
    for m in VAN_WORD.finditer(text):
        after = text[m.end():m.end() + 15]
        if re.match(r"\s+[A-ZÀ-Þ][a-zà-ÿ]", after):          # 'Van Gogh' -> nom propre
            continue
        if STREET_RE.search(text[max(0, m.start() - 15):m.start()]):   # dans une adresse
            continue
        return True
    return False


def extract_price(text: str) -> "float | None":
    vals = []
    for m in re.finditer(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:€|euros?|eur\b|net\b|pab\b)", text, re.I):
        vals.append(float(m.group(1).replace(",", ".")))
    for m in re.finditer(r"(?:net|€)\s*(\d{1,3}(?:[.,]\d{1,2})?)", text, re.I):
        vals.append(float(m.group(1).replace(",", ".")))
    vals = [v for v in vals if 5 <= v <= 1000]
    return max(vals) if vals else None


def extract_km(text: str) -> "float | None":
    m = KM_RE.search(text)
    return float(m.group(1).replace(",", ".")) if m else None


_COMM_PCT  = re.compile(r"comm\w*[^\d%]{0,8}(\d{1,2})\s*%", re.I)   # "commission 15%"
_COMM_PCT2 = re.compile(r"(\d{1,2})\s*%\s*(?:de\s*)?comm", re.I)    # "15% comm"
_COMM_ABS  = re.compile(r"comm\w*[^\d]{0,8}(\d{1,3})(?:\s*€|\s*e\b|\b)", re.I)  # "comm 8€"
_NET_RE    = re.compile(r"\bnet\b", re.I)


def commission_for(text: str, price: "float | None") -> "tuple[float, float | None]":
    """(commission €, taux) pour une course. Priorité : commission explicite du message,
    puis 'net' (= 0), puis le taux par défaut COMMISSION_RATE."""
    if price is None:
        return 0.0, None
    m = _COMM_PCT.search(text) or _COMM_PCT2.search(text)
    if m:
        rate = int(m.group(1)) / 100
        return round(price * rate, 2), rate
    m = _COMM_ABS.search(text)
    if m:
        c = float(m.group(1))
        if 0 < c < price:
            return c, round(c / price, 3)
    if _NET_RE.search(text):                    # prix annoncé "net" -> pas de commission à déduire
        return 0.0, 0.0
    if COMMISSION_RATE:
        return round(price * COMMISSION_RATE, 2), COMMISSION_RATE
    return 0.0, None


def extract_dropoff(text: str) -> "str | None":
    """Adresse d'ARRIVÉE (destination), pour calculer la distance du trajet."""
    raw = re.split(r"/|\n|->|➝|➡️?|→|>|--+|—", text)
    segs = [s.strip(_SYMBOLS) for s in raw if s.strip(_SYMBOLS)]
    for i, s in enumerate(segs):                      # marqueur ARRIVÉE SEUL sur sa ligne -> ligne suivante
        if _ARR_MARKER.match(s) and i + 1 < len(segs):
            cand = expand_shortcuts(_clean(segs[i + 1]))
            if _is_location(cand):
                return cand
    for s in segs:                                   # marqueur explicite ARRIVÉE / À :
        m = ARR_MARK.search(s.strip(_SYMBOLS))
        if m and len(m.group(1).strip()) > 2:
            return expand_shortcuts(_clean(m.group(1)))
    m = re.search(r"\bpour\b(.+)", text, re.I)        # "X pour Y" -> arrivée = Y
    if m:
        right = expand_shortcuts(_clean(re.split(r"/|\n", m.group(1))[0]))
        if _is_location(right):
            return right
    locs = []                                         # positionnel : 2e lieu distinct
    for s in segs:
        for part in re.split(r"\bpour\b", s):
            c = expand_shortcuts(_clean(part))
            if c and _is_location(c) and c.lower() not in [x.lower() for x in locs]:
                locs.append(c)
    return locs[1] if len(locs) > 1 else None


# ─────────────────────── Géocodage / ETA ──────────────────────
async def geocode(session: aiohttp.ClientSession, query: str) -> tuple[float, float, str] | None:
    params = {"q": query, "format": "jsonv2", "limit": "1", "addressdetails": "0"}
    if GEO_VIEWBOX:
        params["viewbox"] = GEO_VIEWBOX
        params["bounded"] = "1"
    try:
        async with session.get(f"{NOMINATIM_URL}/search", params=params,
                               headers={"User-Agent": USER_AGENT}) as r:
            r.raise_for_status()
            data = await r.json()
    except Exception as e:                       # noqa: BLE001
        log.warning("Nominatim KO pour %r : %s", query, e)
        return None
    if not data:
        return None
    hit = data[0]
    return float(hit["lat"]), float(hit["lon"]), hit.get("display_name", query)


async def route_eta(session: aiohttp.ClientSession, src: dict, dst: tuple[float, float],
                    when_hour: "float | None" = None) -> tuple[float, float] | None:
    """Retourne (durée_secondes_avec_trafic, distance_mètres) en voiture, ou None.
    when_hour : heure prévue de la course (réservation) ; None = maintenant (immédiat).
    - Si HERE_KEY défini : HERE Routing API (trafic prédictif à l'heure prévue).
    - Sinon : OSRM × multiplicateur d'heure de pointe Paris (à l'heure prévue)."""
    if HERE_KEY:
        return await _route_eta_here(session, src, dst, when_hour)
    return await _route_eta_osrm(session, src, dst, when_hour)


async def _route_eta_here(session: aiohttp.ClientSession, src: dict, dst: tuple[float, float],
                          when_hour: "float | None" = None) -> tuple[float, float] | None:
    """HERE Routing API v8 — trafic (250 000 req/mois gratuites). Réservation -> departureTime."""
    params = {
        "transportMode": "car",
        "origin": f"{src['lat']},{src['lon']}",
        "destination": f"{dst[0]},{dst[1]}",
        "return": "summary",
        "apikey": HERE_KEY,
    }
    if when_hour is not None:
        params["departureTime"] = _departure_iso(when_hour)   # trafic prédictif à l'heure prévue
    try:
        async with session.get("https://router.hereapi.com/v8/routes", params=params) as r:
            r.raise_for_status()
            data = await r.json()
        section = data["routes"][0]["sections"][0]["summary"]
        return section["duration"], section["length"]
    except Exception as e:                       # noqa: BLE001
        log.warning("HERE KO, fallback OSRM : %s", e)
        return await _route_eta_osrm(session, src, dst, when_hour)


async def _route_eta_osrm(session: aiohttp.ClientSession, src: dict, dst: tuple[float, float],
                          when_hour: "float | None" = None) -> tuple[float, float] | None:
    """OSRM (sans trafic) × multiplicateur d'heure de pointe Paris (heure prévue si réservation)."""
    coords = f"{src['lon']},{src['lat']};{dst[1]},{dst[0]}"
    url = f"{OSRM_URL}/route/v1/driving/{coords}"
    try:
        async with session.get(url, params={"overview": "false"}) as r:
            r.raise_for_status()
            data = await r.json()
    except Exception as e:                       # noqa: BLE001
        log.warning("OSRM KO : %s", e)
        return None
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    route = data["routes"][0]
    dur  = route["duration"] * _traffic_multiplier(when_hour)
    dist = route["distance"]
    return dur, dist


# ────────────────────── Escalade vers Hermès ──────────────────
def course_link(gid: int, mid: int) -> str:
    """Deep link vers le message d'origine dans le groupe source (supergroupe privé -100…)."""
    if mid and abs(gid) > 1_000_000_000_000:
        return f"https://t.me/c/{abs(gid) - 1_000_000_000_000}/{mid}"
    return ""


def _short_loc(s: str) -> str:
    """Version courte d'un lieu (2 premiers segments) pour l'affichage."""
    return ", ".join(s.split(", ")[:2]) if s else "?"


def trip_line(depart: str, arrivee: str) -> str:
    """Prise en charge et destination, chacune sur sa ligne, bien visibles."""
    return (f"📍 <b>{html.escape(_short_loc(depart))}</b>\n"
            f"🏁 <b>{html.escape(_short_loc(arrivee))}</b>")


def rentability_dot(eurkm) -> str:
    """🟢 rentable · 🟠 moyen · 🔴 peu rentable · ⚪ inconnu."""
    if eurkm is None:
        return "⚪"
    if eurkm >= EURKM_GOOD:
        return "🟢"
    if eurkm >= EURKM_MID:
        return "🟠"
    return "🔴"


_TIME_EXTRACT = re.compile(r"\b(\d{1,2})\s*[h:]\s*(\d{2})?\b")


def depart_time(text: str) -> str:
    """Heure de départ : 'Immédiat' ou l'horaire trouvé (8h30, 11h00…)."""
    if IMMEDIATE_RE.search(text):
        return "Immédiat"
    m = _TIME_EXTRACT.search(text)
    return f"{int(m.group(1))}h{m.group(2) or '00'}" if m else "Immédiat"


def depart_hour(text: str) -> "float | None":
    """Heure prévue en décimal (8h30 -> 8.5) pour estimer le trafic à ce moment.
    None si immédiat ou horaire absent -> le routage utilise l'heure courante."""
    if IMMEDIATE_RE.search(text):
        return None
    m = _TIME_EXTRACT.search(text)
    if not m:
        return None
    h, mn = int(m.group(1)), int(m.group(2) or 0)
    if h > 23 or mn > 59:
        return None
    return h + mn / 60


# ─────────── Date/heure d'une réservation -> agenda (.ics) ───────────
_WEEKDAYS = {"lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3,
             "vendredi": 4, "samedi": 5, "dimanche": 6}
_DATE_DMY = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?\b")


def reservation_datetime(text: str) -> "object | None":
    """Datetime prévu d'une réservation (date + heure). None si pas d'heure lisible.
    Comprend : jj/mm(/aaaa), 'demain', jours de la semaine ; sinon aujourd'hui (ou +1 j si passé)."""
    m = _TIME_EXTRACT.search(text)
    if not m:
        return None
    h, mn = int(m.group(1)), int(m.group(2) or 0)
    if h > 23 or mn > 59:
        return None
    now = _dt.datetime.now()
    low = text.lower()
    date, explicit, roll = None, False, None
    dm = _DATE_DMY.search(text)
    if dm:
        d, mo = int(dm.group(1)), int(dm.group(2))
        y = int(dm.group(3)) if dm.group(3) else now.year
        if y < 100:
            y += 2000
        try:
            date, explicit = _dt.date(y, mo, d), True
        except ValueError:
            date = None
    if date is None:
        if "demain" in low:
            date, roll = (now + _dt.timedelta(days=1)).date(), 0
        else:
            for name, wd in _WEEKDAYS.items():
                if re.search(rf"\b{name}\b", low):
                    date = (now + _dt.timedelta(days=(wd - now.weekday()) % 7)).date()
                    roll = 7
                    break
    if date is None:
        date, roll = now.date(), 1
    dtm = _dt.datetime(date.year, date.month, date.day, h, mn)
    if not explicit and roll and dtm < now:      # heure déjà passée -> prochaine occurrence
        dtm += _dt.timedelta(days=roll)
    return dtm


def _ics_stamp(dt) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def build_ics(summary: str, dt, description: str = "", location: str = "") -> str:
    """Événement iCalendar (heure locale flottante) avec rappels 24 h et 1 h avant."""
    def esc(s):
        return (s or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    end = dt + _dt.timedelta(hours=1)
    uid = f"{_ics_stamp(dt)}-{abs(hash(summary + location)) % 100000}@proxia.vtc"
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//PROXIA//VTC//FR", "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT", f"UID:{uid}", f"DTSTART:{_ics_stamp(dt)}", f"DTEND:{_ics_stamp(end)}",
        f"SUMMARY:{esc(summary)}", f"DESCRIPTION:{esc(description)}", f"LOCATION:{esc(location)}",
        "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Rappel course VTC (24 h)", "TRIGGER:-PT24H", "END:VALARM",
        "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Rappel course VTC (1 h)", "TRIGGER:-PT1H", "END:VALARM",
        "END:VEVENT", "END:VCALENDAR", "",
    ])


def metrics_header(eurkm, price, dur_min, dep_time: str) -> str:
    """Ligne compacte en tête : rentabilité (couleur) · prix · durée · heure."""
    parts = [rentability_dot(eurkm) + (f" {eurkm:.2f} €/km" if eurkm is not None else "")]
    if price:
        parts.append(f"💶 {price:.0f}€")
    if dur_min:
        parts.append(f"⏳ {round(dur_min)} min")     # durée de la course
    if dep_time:
        parts.append(f"🕐 {dep_time}")               # heure de départ
    return " · ".join(parts)


# Éléments de design premium : filet fin structurant + séparateur milieu de ligne.
HR = "─────────────"
MID = "  ·  "


def rentability_line(eurkm) -> str:
    """Ligne de décision mise en avant : pastille + €/km en gras + niveau."""
    dot = rentability_dot(eurkm)
    if eurkm is None:
        return f"{dot}  <i>rentabilité à confirmer</i>"
    tier = "rentable" if eurkm >= EURKM_GOOD else ("correct" if eurkm >= EURKM_MID else "peu rentable")
    return f"{dot}  <b>{eurkm:.1f} €/km</b>{MID}{tier}"


def pretty_metrics(price, km, dur_min, dep_time: str) -> str:
    """Ligne de métriques secondaires (le €/km est porté par rentability_line)."""
    parts = []
    if price:
        parts.append(f"💶 {price:.0f} €")
    if km:
        parts.append(f"📏 {km:.0f} km")
    if dur_min:
        parts.append(f"⏱️ {round(dur_min)} min")
    if dep_time:
        parts.append(f"🕐 {dep_time}")
    return MID.join(parts)


async def _geo_route(session, from_coords, to_text: str):
    """Géocode une destination et route depuis from_coords. Retourne (km, durée_min) ou None."""
    if not to_text:
        return None
    g = await geocode(session, to_text)
    await asyncio.sleep(1.1)
    if not g:
        return None
    rt = await route_eta(session, {"lat": from_coords[0], "lon": from_coords[1]}, (g[0], g[1]))
    if not rt:
        return None
    return rt[1] / 1000, rt[0] / 60


async def _expire_alert(msg_id: int) -> None:
    """Passé EXPIRE_SECONDS sans réponse, neutralise l'alerte."""
    await asyncio.sleep(EXPIRE_SECONDS)
    cid = _pending_alerts.pop(msg_id, None)
    if cid is None:                            # déjà répondue / passée
        return
    store.set_status(cid, "expirée")
    try:
        await bot.edit_message(
            NOTIFY_USER_ID, msg_id,
            f"⏱️ <b>Course expirée</b>{MID}<i>sans réponse en {EXPIRE_SECONDS // 60} min</i>",
            buttons=None, parse_mode="html")
    except Exception:                          # noqa: BLE001
        pass


async def deliver_alert(dm_body: str, feed_body: str, reply: str, gid: int, mid: int,
                        course: "dict | None" = None) -> None:
    """Bot configuré -> DM propre (HTML) + boutons. Sinon -> texte technique dans le relais (Hermès).
    Si `course` est fourni, la course est journalisée (statut « proposée ») pour le résumé."""
    if bot and NOTIFY_USER_ID:
        # choix de réponse : la suggestion (marquée ✅) + les délais rapides, sans doublon
        choices = list(QUICK_REPLIES)
        if reply not in choices:
            choices.insert(0, reply)
        btns = [Button.inline(("✅ " if c == reply else "") + c, f"s|{gid}|{mid}|{c}".encode())
                for c in choices]
        rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]   # rangées de 3
        rows.append([Button.inline("❌ Passer", b"x")])
        link = course_link(gid, mid)
        if link:
            rows.append([Button.url("🔗 Voir la course", link)])
        sent = await bot.send_message(NOTIFY_USER_ID, dm_body, buttons=rows,
                                      parse_mode="html", link_preview=False)
        if course is not None:
            cid = store.record_course(alert_id=sent.id, reply=reply, **course)
            _pending_alerts[sent.id] = cid
        asyncio.create_task(_expire_alert(sent.id))
    else:
        body = feed_body + (f"\n{BOT_MENTION}" if BOT_MENTION else "")
        await client.send_message(FEED_GROUP, body)


async def escalate(session, text_original: str, dep_text: str, pickup_coords,
                   eta_s: float, dist_m: float, gid: int, mid: int) -> None:
    prox = round(eta_s / 60)                        # temps pour rejoindre le départ
    # Règle de réponse VTC : < 5 min -> "SP" (sur place), sinon le délai en minutes
    reply = "SP" if eta_s < SP_UNDER_SECONDS else str(prox)
    link = course_link(gid, mid)
    price = extract_price(text_original)
    arr_text = extract_dropoff(text_original)
    trip = await _geo_route(session, pickup_coords, arr_text)   # (km course, durée course)
    trip_km = trip[0] if trip else None
    dur_min = trip[1] if trip else None
    eurkm = (price / trip_km) if (price and trip_km) else None
    commission, comm_rate = commission_for(text_original, price)
    net = (price - commission) if price is not None else None
    header = metrics_header(eurkm, price, dur_min, depart_time(text_original))
    dm_body = (
        f"🚕 <b>Course proche</b>\n"
        f"{HR}\n"
        f"📍 <b>{html.escape(_short_loc(dep_text))}</b>\n"
        f"🏁 <b>{html.escape(_short_loc(arr_text))}</b>\n\n"
        f"🚗 <b>{prox} min</b> de toi{MID}{dist_m/1000:.1f} km\n"
        f"{rentability_line(eurkm)}\n"
        f"{pretty_metrics(price, trip_km, dur_min, depart_time(text_original))}\n\n"
        f"<blockquote expandable>{html.escape(text_original)}</blockquote>"
    )
    course = dict(kind="immediat", group_id=gid, group_name=group_name(gid), msg_id=mid,
                  pickup=dep_text, dropoff=arr_text, price=price, km=trip_km, eur_km=eurkm,
                  duration_min=dur_min, eta_min=prox, commission=commission,
                  commission_rate=comm_rate, net=net, raw=text_original)
    feed_body = (
        "🚕 [COURSE PROCHE]\n"
        f"{header}\n"
        f"📍 PEC : {_short_loc(dep_text)}\n"
        f"🏁 Dest : {_short_loc(arr_text or '?')}\n"
        f"🚗 à {prox} min · {dist_m/1000:.1f} km\n"
        f"💬 Réponse suggérée : {reply}\n"
        + (f"🔗 {link}\n" if link else "")
        + f"[GID {gid}]\n──────────\n{text_original}\n──────────\n"
        f"→ Hermès : propose d'envoyer « {reply} » (valide avant envoi)."
    )
    await deliver_alert(dm_body, feed_body, reply, gid, mid, course)
    log.info("Course proche escaladée (à %s min | PEC=%r | brut=%r).",
             prox, dep_text, text_original[:70].replace("\n", " "))


_last_pos_warn = 0.0
_pos_expiry_task = None


def schedule_live_expiry(period: int) -> None:
    """Programme un rappel LIVE_REMIND_BEFORE avant la fin d'un partage de position en direct."""
    global _pos_expiry_task
    if _pos_expiry_task and not _pos_expiry_task.done():
        _pos_expiry_task.cancel()
    _pos_expiry_task = asyncio.create_task(_live_expiry_reminder(max(0, period - LIVE_REMIND_BEFORE)))


async def _live_expiry_reminder(delay: float) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    if not (bot and NOTIFY_USER_ID):
        return
    try:
        await bot.send_message(
            NOTIFY_USER_ID,
            "⏰ <b>Ta position expire bientôt</b>\n"
            f"{HR}\n"
            "Relance le partage en direct pour rester couvert sans interruption :\n"
            "<i>📎 → Position → Partager ma position en direct → 8 h.</i>",
            parse_mode="html")
    except Exception:                       # noqa: BLE001
        pass


async def _reservation_reminder_loop() -> None:
    """Toutes les 10 min : pour chaque réservation dont l'heure est passée (+ RESA_PROMPT_AFTER),
    demande une seule fois « effectuée ? » afin de compter (ou non) le paiement."""
    await asyncio.sleep(30)
    while True:
        try:
            if bot and NOTIFY_USER_ID:
                for r in store.due_reservations(time.time() - RESA_PROMPT_AFTER):
                    store.set_prompted(r["id"])
                    rows = [[Button.inline("✅ Effectuée", f"eff|{r['id']}|1".encode()),
                             Button.inline("❌ Annulée", f"eff|{r['id']}|0".encode())]]
                    await bot.send_message(
                        NOTIFY_USER_ID,
                        f"📅 <b>Réservation — effectuée ?</b>\n{HR}\n{_recap_line(r)}\n\n"
                        f"<i>L'heure est passée. Valide pour compter le paiement.</i>",
                        buttons=rows, parse_mode="html")
        except Exception as e:                       # noqa: BLE001
            log.warning("Rappel réservation KO : %s", e)
        await asyncio.sleep(600)


async def _daily_summary_loop() -> None:
    """Envoie une fois par jour, vers DAILY_SUMMARY_HOUR, le récap des dernières 24 h
    (seulement s'il y a eu des courses réalisées ou des frais)."""
    if DAILY_SUMMARY_HOUR is None:
        return
    await asyncio.sleep(60)
    while True:
        try:
            if bot and NOTIFY_USER_ID:
                now = _dt.datetime.now()
                today = now.date().isoformat()
                if now.hour == DAILY_SUMMARY_HOUR and store.get_setting("last_daily") != today:
                    store.set_setting("last_daily", today)            # une seule fois par jour
                    s = store.summary("24h")
                    if s["done"] or s.get("frais"):                  # pas de spam les jours sans activité
                        await bot.send_message(
                            NOTIFY_USER_ID,
                            "🌙 <b>Bonne fin de service — voici ta journée.</b>\n\n" + build_report("24h"),
                            parse_mode="html", link_preview=False)
        except Exception as e:                       # noqa: BLE001
            log.warning("Bilan auto KO : %s", e)
        await asyncio.sleep(600)


async def warn_position() -> None:
    """Rappel throttlé (max 1 / 10 min) quand ta position n'est plus partagée."""
    global _last_pos_warn
    now = time.time()
    if now - _last_pos_warn < 600 or not (bot and NOTIFY_USER_ID):
        return
    _last_pos_warn = now
    try:
        await bot.send_message(
            NOTIFY_USER_ID,
            "📍 <b>Position introuvable</b>\n"
            f"{HR}\n"
            "Je ne reçois plus ta position — impossible de repérer les courses proches.\n\n"
            "👉 Partage ta <b>position en direct</b> ici, dans ce chat :\n"
            "<i>📎 → Position → Partager ma position en direct → 8 h.</i>",
            parse_mode="html")
    except Exception:                       # noqa: BLE001
        pass


async def escalate_raw(text_original: str, src: dict | None, reason: str,
                       gid: int, mid: int = 0) -> None:
    """Course non exploitable (non géocodée…) : on la LOGGE sans notifier (zéro bruit).
    Les alertes ne partent que pour les courses réellement jugées proches / rentables."""
    log.info("Course à confirmer (%s) | gid %s | brut=%r",
             reason, gid, text_original[:80].replace("\n", " "))


async def escalate_resa(text_original: str, price: float, km: float, eurkm: float,
                        dur_min, gid: int, mid: int) -> None:
    """Réservation rentable (>= EURKM_MIN €/km)."""
    link = course_link(gid, mid)
    deps = extract_pickup(text_original)
    depart = deps[0] if deps else "?"
    arrivee = extract_dropoff(text_original)
    commission, comm_rate = commission_for(text_original, price)
    net = (price - commission) if price is not None else None
    header = metrics_header(eurkm, price, dur_min, depart_time(text_original))
    dm_body = (
        f"📅 <b>Réservation</b>\n"
        f"{HR}\n"
        f"📍 <b>{html.escape(_short_loc(depart))}</b>\n"
        f"🏁 <b>{html.escape(_short_loc(arrivee))}</b>\n\n"
        f"{rentability_line(eurkm)}\n"
        f"{pretty_metrics(price, km, dur_min, depart_time(text_original))}\n\n"
        f"<blockquote expandable>{html.escape(text_original)}</blockquote>"
    )
    course = dict(kind="resa", group_id=gid, group_name=group_name(gid), msg_id=mid,
                  pickup=depart, dropoff=arrivee, price=price, km=km, eur_km=eurkm,
                  duration_min=dur_min, eta_min=None, commission=commission,
                  commission_rate=comm_rate, net=net, raw=text_original)
    feed_body = (
        "💰 [RÉSERVATION RENTABLE]\n"
        f"{header}\n"
        f"📍 {_short_loc(depart)}\n"
        f"🏁 {_short_loc(arrivee)}\n"
        f"💬 Réponse suggérée : {RESA_REPLY}\n"
        + (f"🔗 {link}\n" if link else "")
        + f"[GID {gid}]\n──────────\n{text_original}\n──────────\n"
        f"→ Hermès : réservation rentable, propose « {RESA_REPLY} »."
    )
    await deliver_alert(dm_body, feed_body, RESA_REPLY, gid, mid, course)
    log.info("Réservation escaladée (%.2f €/km, gid %s).", eurkm, gid)


async def handle_reservation(text: str, gid: int, mid: int) -> None:
    """Réservation : notifie si le tarif est >= EURKM_MIN €/km (proximité ignorée)."""
    price = extract_price(text)
    if price is None:
        return                                       # pas de prix lisible -> on ne peut pas juger
    km = extract_km(text)                            # km explicite dans le message ?
    dur_min = None
    if km is None:                                   # sinon on géocode dep->arr et on route
        deps = extract_pickup(text)
        dep = deps[0] if deps else None
        arr = extract_dropoff(text)
        if not dep or not arr:
            await escalate_raw(text, load_position(), "réservation : trajet incomplet", gid, mid)
            return
        async with aiohttp.ClientSession() as s:
            g1 = await geocode(s, dep)
            await asyncio.sleep(1.1)
            g2 = await geocode(s, arr)
            await asyncio.sleep(1.1)
            if not g1 or not g2:
                await escalate_raw(text, load_position(), "réservation : géocodage trajet KO", gid, mid)
                return
            # trafic estimé à l'HEURE PRÉVUE de la réservation (pas maintenant)
            rt = await route_eta(s, {"lat": g1[0], "lon": g1[1]}, (g2[0], g2[1]),
                                 when_hour=depart_hour(text))
        if not rt:
            return
        km = rt[1] / 1000
        dur_min = rt[0] / 60
    if km < 0.5:
        return
    eurkm = price / km
    if eurkm >= EURKM_MIN:
        await escalate_resa(text, price, km, eurkm, dur_min, gid, mid)
    else:
        log.info("Réservation ignorée : %.2f €/km (< %.2f).", eurkm, EURKM_MIN)


# ───────────── Déduplication inter-groupes ─────────────
# Une même course est souvent repostée dans plusieurs groupes, avec un formatage différent.
# On la reconnaît par une empreinte "floue" : prix + jeu de codes postaux (indépendant de
# l'ordre départ/arrivée) sur le texte EXPANSÉ, pour que P10 / Paris 10 / 75010 et
# CDG / Roissy convergent vers les mêmes codes. Une seule alerte -> une seule réponse.

_POSTAL_RE = re.compile(r"\b\d{5}\b")


def course_fingerprint(text: str) -> "str | None":
    """Empreinte stable d'une course pour repérer un repost dans un autre groupe.
    Retourne None si le message n'a pas assez de signal (on ne dédup pas dans le doute)."""
    price = extract_price(text)
    expanded = expand_shortcuts(text)                     # P10->75010, CDG/Roissy->95700, Orly->94390…
    codes = sorted(set(_POSTAL_RE.findall(expanded)))
    # Signal géo obligatoire : sans code postal (ou 1 seul sans prix) on NE dédup PAS,
    # sinon 2 courses distinctes "40€ immédiat" partageraient la même empreinte.
    if not codes or (len(codes) < 2 and price is None):
        return None
    price_key = f"{price:.0f}" if price is not None else "?"
    return f"{price_key}|{','.join(codes)}|{depart_time(text)}"


_TEXT_FP_NOISE = re.compile(r"[^a-z0-9]+")


def course_text_fingerprint(text: str) -> "str | None":
    """Empreinte de repli : le message lui-même, réduit à ses lettres et ses chiffres.

    Sert uniquement quand course_fingerprint() renvoie None. Un repost inter-groupes est
    un copier-coller : casse, accents, emoji et ponctuation varient, le fond non. On garde
    les chiffres, qui portent le prix et l'heure : deux courses au même départ mais à
    30 € et 45 € donnent deux empreintes distinctes.
    """
    flat = unicodedata.normalize("NFKD", text.lower())
    flat = _TEXT_FP_NOISE.sub("", flat.encode("ascii", "ignore").decode())
    if len(flat) < 12:                             # trop court -> pas assez de signal
        return None
    return "txt|" + flat


def is_duplicate_course(fp: "str | None", window: "float | None" = None) -> bool:
    """True si cette empreinte a déjà été vue dans la fenêtre (repost inter-groupes)."""
    if not fp:
        return False
    if window is None:
        window = DEDUP_WINDOW_SECONDS
    now = time.time()
    horizon = max(DEDUP_WINDOW_SECONDS, DEDUP_TEXT_WINDOW_SECONDS)
    for k, ts in list(_dup_seen.items()):        # purge -> borne la mémoire
        if now - ts > horizon:
            del _dup_seen[k]
    already = fp in _dup_seen and now - _dup_seen[fp] <= window
    _dup_seen[fp] = now                            # (ré)arme la fenêtre glissante
    return already


# ───────────── Résumé des courses (prix, commission, net) ─────────────
_PERIOD_LABEL = {"jour": "aujourd'hui", "24h": "dernières 24 h", "semaine": "cette semaine",
                 "mois": "ce mois-ci", "tout": "depuis le début"}


def _fmt_dur(mins) -> str:
    mins = int(mins or 0)
    h, m = divmod(mins, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def _short_grp(g: str) -> str:
    g = (g or "?").strip()
    return g if len(g) <= 26 else g[:25] + "…"


def _bar(pct: float, width: int = 10) -> str:
    """Barre de progression en blocs (▓░) pour l'objectif."""
    f = int(round(pct / 100 * width))
    return "▓" * f + "░" * (width - f)


def _receipt(s: dict) -> str:
    """Bloc financier aligné en monospace (effet « reçu »)."""
    rate = (s["commission"] / s["ca"] * 100) if s["ca"] else 0
    rows = [("Courses", f"{s['done']}"), ("Chiffre", f"{s['ca']:.0f} €")]
    if s["commission"]:
        rows.append(("Commission", f"{s['commission']:.0f} € · {rate:.0f}%"))
    rows.append(("Net", f"{s['net']:.0f} €"))
    if s.get("frais"):
        rows.append(("Frais", f"−{s['frais']:.0f} €"))
        rows.append(("Bénéfice", f"{s['benefice']:.0f} €"))
    if s["km"]:
        rows.append(("Distance", f"{s['km']:.0f} km"))
    if s["duration_min"]:
        rows.append(("Temps", _fmt_dur(s["duration_min"])))
    if s["eur_km_avg"]:
        rows.append(("Moyenne", f"{s['eur_km_avg']:.1f} €/km"))
    lw = max(len(l) for l, _ in rows)
    vw = max(len(v) for _, v in rows)
    body = "\n".join(f"{l:<{lw}}   {v:>{vw}}" for l, v in rows)
    return "<pre>" + html.escape(body) + "</pre>"


def _upcoming_block(s: dict) -> list:
    """Bloc « À venir » : réservations validées, pas encore effectuées (hors CA réalisé)."""
    if not s.get("upcoming_n"):
        return []
    out = ["", f"📅 <b>À venir</b>{MID}{s['upcoming_n']} résa · {s['upcoming_ca']:.0f} € "
               f"<i>(pas encore compté)</i>"]
    for r in store.pending_reservations(6):
        dtm = reservation_datetime(r["raw"] or "")
        when = dtm.strftime("%d/%m %Hh%M") if dtm else "date ?"
        trip = f"{_short_loc(r['pickup'] or '?')} → {_short_loc(r['dropoff'] or '?')}"
        price = f"{r['price']:.0f} €" if r["price"] else "?"
        out.append(f"▸ {when} · {html.escape(trip)} · {price}")
    return out


def build_report(period: str = "jour") -> str:
    s = store.summary(period)
    c = s["counts"]
    lbl = _PERIOD_LABEL.get(period, period)
    obj_set = store.get_setting("objectif_jour")
    out = [f"📊 <b>Bilan · {lbl}</b>", HR]

    if s["done"] or s.get("frais") or (obj_set and period in ("jour", "24h")):
        if s.get("frais"):
            head = (f"🏁 <b>{s['benefice']:.0f} €</b> de bénéfice{MID}{s['net']:.0f} € net"
                    f"{MID}{s['ca']:.0f} € brut")
        else:
            head = f"💚 <b>{s['net']:.0f} €</b> net{MID}{s['ca']:.0f} € brut"
        out += [head, "", _receipt(s), "",
                f"✅ {s['done']} réalisées{MID}💬 {c['répondue']}{MID}📤 {c['proposée']}"]
        obj = store.get_setting("objectif_jour")
        if obj and period in ("jour", "24h"):
            try:
                obj = float(obj)
                base = s["benefice"] if s.get("frais") else s["net"]
                pct = max(0.0, min(100.0, base / obj * 100)) if obj else 0
                out.append(f"🎯 Objectif {obj:.0f} €{MID}{pct:.0f}% {_bar(pct)}")
            except (ValueError, ZeroDivisionError):
                pass
        if s.get("payments"):
            out.append("💳 " + MID.join(f"{p} <b>{ca:.0f} €</b>" for p, n, ca in s["payments"]))
        if s["by_group"]:
            out += ["", "🏆 <b>Groupes</b>"]
            for g, n, ca in s["by_group"][:6]:
                out.append(f"▸ {html.escape(_short_grp(g))} — {n} · {ca:.0f} €")
    else:
        out += [
            "Aucune course réalisée sur la période.",
            "",
            f"📤 {c['proposée']} envoyées{MID}💬 {c['répondue']} à valider{MID}⏭️ {c['passée']} passées",
        ]

    out += _upcoming_block(s)          # réservations à venir (toujours affichées)

    done = store.list_done(period)
    if done:
        lines = []
        for i, r in enumerate(done, 1):
            hhmm = (r["created"] or "")[11:16]
            trip = f"{_short_loc(r['pickup'] or '?')} → {_short_loc(r['dropoff'] or '?')}"
            price = f"{r['price']:.0f} €" if r["price"] else "?"
            comm = f" · comm {r['commission']:.0f} €" if r["commission"] else ""
            net = f" · net {r['net']:.0f} €" if (r["net"] is not None and r["commission"]) else ""
            ek = f" · {r['eur_km']:.1f} €/km" if r["eur_km"] else ""
            pay = f" · {r['payment']}" if r["payment"] else ""
            lines.append(f"<b>{i}.</b>  {hhmm}{MID}{html.escape(trip)}{MID}{price}{comm}{net}{ek}{pay}")
            if r["note"]:
                lines.append(f"      📝 <i>{html.escape(r['note'])}</i>")
        out += ["", "🧾 <b>Détail</b>",
                "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"]

    out += ["", "<i>/resume · /semaine · /mois</i>"]
    return "\n".join(out)


_TABLE_CSS = """
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,system-ui,"Segoe UI",Roboto,sans-serif;background:#0c0f14;color:#e8ecf6;padding:16px}
@media(prefers-color-scheme:light){body{background:#f6f7f9;color:#14181f}}
h1{font-size:20px;margin:0 0 14px;display:flex;flex-direction:column;gap:2px}
h1 span{font-size:13px;font-weight:400;color:#8a94a6;text-transform:capitalize}
.cards{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.card{background:#161b24;border:1px solid #232a36;border-radius:12px;padding:9px 13px;min-width:82px}
@media(prefers-color-scheme:light){.card{background:#fff;border-color:#e3e7ee}}
.card span{display:block;font-size:11px;color:#8a94a6;text-transform:uppercase;letter-spacing:.04em}
.card b{font-size:18px}
.card.hl{border-color:#3ddc84}.card.hl b{color:#3ddc84}
.wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid #232a36;border-radius:12px}
@media(prefers-color-scheme:light){.wrap{border-color:#e3e7ee}}
table{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}
th,td{padding:9px 11px;text-align:left;border-bottom:1px solid #1c222c}
@media(prefers-color-scheme:light){th,td{border-color:#eceff3}}
thead th{position:sticky;top:0;background:#12161d;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#8a94a6}
@media(prefers-color-scheme:light){thead th{background:#eef1f5}}
tbody tr:nth-child(even){background:rgba(127,127,127,.06)}
.n{text-align:right;font-variant-numeric:tabular-nums}
.b{font-weight:700}.dt{color:#8a94a6}
.sm{font-size:12px;color:#9aa6ba;white-space:normal;max-width:190px}
.st{font-size:11px;padding:2px 7px;border-radius:6px;white-space:nowrap}
.st-obtenue,.st-effectuée{background:rgba(61,220,132,.16);color:#3ddc84}
.st-réservée{background:rgba(244,166,33,.16);color:#f4a621}
.empty{text-align:center;color:#8a94a6;padding:24px}
.ft{color:#8a94a6;font-size:12px;margin-top:12px}
"""


def build_table_html(period: str) -> str:
    """Page HTML autonome : tableau de toutes les courses de la période + totaux."""
    s = store.summary(period)
    rows = store.courses_detail(period)
    lbl = _PERIOD_LABEL.get(period, period)

    def esc(x):
        return html.escape(str(x)) if x not in (None, "") else ""

    trs = []
    for r in rows:
        cr = r["created"] or ""
        when = f"{cr[8:10]}/{cr[5:7]} {cr[11:16]}" if len(cr) >= 16 else cr
        prix = f"{r['price']:.0f}" if r["price"] else ""
        comm = f"{r['commission']:.0f}" if r["commission"] else ""
        net = f"{r['net']:.0f}" if r["net"] is not None else ""
        ekm = f"{r['eur_km']:.1f}" if r["eur_km"] else ""
        st = r["status"]
        trs.append(
            "<tr>"
            f"<td class='dt'>{when}</td>"
            f"<td>{esc(_short_loc(r['pickup'] or '?'))}</td>"
            f"<td>{esc(_short_loc(r['dropoff'] or '?'))}</td>"
            f"<td class='n'>{prix}</td><td class='n'>{comm}</td>"
            f"<td class='n b'>{net}</td><td class='n'>{ekm}</td>"
            f"<td>{esc(r['payment'])}</td>"
            f"<td><span class='st st-{st}'>{esc(STATUS_LABEL.get(st, st))}</span></td>"
            f"<td class='sm'>{esc(_short_grp(r['group_name'] or ''))}</td>"
            f"<td class='sm'>{esc(r['note'])}</td>"
            "</tr>"
        )
    body = "\n".join(trs) or "<tr><td colspan='11' class='empty'>Aucune course sur la période.</td></tr>"
    benef = s.get("benefice", s["net"])
    cards = (
        f"<div class='card'><span>Courses</span><b>{s['done']}</b></div>"
        f"<div class='card'><span>CA</span><b>{s['ca']:.0f} €</b></div>"
        + (f"<div class='card'><span>Commission</span><b>{s['commission']:.0f} €</b></div>" if s["commission"] else "")
        + f"<div class='card'><span>Net</span><b>{s['net']:.0f} €</b></div>"
        + (f"<div class='card'><span>Frais</span><b>−{s['frais']:.0f} €</b></div>" if s.get("frais") else "")
        + f"<div class='card hl'><span>Bénéfice</span><b>{benef:.0f} €</b></div>"
    )
    return (
        "<!doctype html><html lang='fr'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Courses — {esc(lbl)}</title><style>{_TABLE_CSS}</style></head><body>"
        f"<h1>🚕 Tableau des courses<span>{esc(lbl)}</span></h1>"
        f"<div class='cards'>{cards}</div>"
        "<div class='wrap'><table><thead><tr>"
        "<th>Date</th><th>Départ</th><th>Arrivée</th><th class='n'>Prix</th>"
        "<th class='n'>Comm</th><th class='n'>Net</th><th class='n'>€/km</th>"
        "<th>Paiement</th><th>Statut</th><th>Groupe</th><th>Note</th>"
        f"</tr></thead><tbody>{body}</tbody></table></div>"
        f"<p class='ft'>Généré le {_dt.datetime.now().strftime('%d/%m/%Y %Hh%M')} · {len(rows)} courses</p>"
        "</body></html>"
    )


_PDF_KEEP = set("€–—‘’“”…·")


def _pdf_safe(x) -> str:
    """Texte pour le PDF : garde latin + accents + €, retire emojis/symboles (glyphes absents)."""
    s = str(x) if x not in (None, "") else ""
    return "".join(c for c in s if ord(c) < 0x0250 or c in _PDF_KEEP).strip()


def build_table_pdf(period: str) -> bytes:
    """PDF A4 designé : en-tête, cartes de totaux, tableau paginé de toutes les courses."""
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    s = store.summary(period)
    rows = store.courses_detail(period)
    lbl = _PERIOD_LABEL.get(period, period).capitalize()
    benef = s.get("benefice", s["net"])
    INK, GREY, AMBER, GREEN = (26, 30, 38), (140, 148, 166), (178, 110, 12), (18, 154, 86)

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(True, margin=16)
    pdf.set_margins(12, 12, 12)
    # Police Unicode (Arial macOS) pour le vrai « € » ; repli Helvetica + « EUR ».
    FAM, EURO = "Helvetica", "EUR"
    try:
        pdf.add_font("Arial", "", "/System/Library/Fonts/Supplemental/Arial.ttf")
        pdf.add_font("Arial", "B", "/System/Library/Fonts/Supplemental/Arial Bold.ttf")
        FAM, EURO = "Arial", "€"
    except Exception:                            # noqa: BLE001
        pass
    pdf.add_page()

    # ── En-tête ──
    pdf.set_font(FAM, "B", 19)
    pdf.set_text_color(*INK)
    pdf.cell(0, 9, _pdf_safe("Relevé de courses"), new_x="LMARGIN", new_y="TOP")
    pdf.set_font(FAM, "", 8.5)
    pdf.set_text_color(*GREY)
    pdf.cell(0, 9, _pdf_safe(_dt.datetime.now().strftime("Généré le %d/%m/%Y à %Hh%M")),
             new_x="LMARGIN", new_y="NEXT", align="R")
    pdf.set_font(FAM, "B", 11)
    pdf.set_text_color(*AMBER)
    pdf.cell(0, 6, _pdf_safe(lbl), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_draw_color(*AMBER)
    pdf.set_line_width(0.6)
    y = pdf.get_y()
    pdf.line(12, y, pdf.w - 12, y)
    pdf.ln(5)

    # ── Cartes de totaux ──
    totals = [("Courses", str(s["done"])), ("Chiffre", f"{s['ca']:.0f} {EURO}")]
    if s["commission"]:
        totals.append(("Commission", f"{s['commission']:.0f} {EURO}"))
    totals.append(("Net", f"{s['net']:.0f} {EURO}"))
    if s.get("frais"):
        totals.append(("Frais", f"-{s['frais']:.0f} {EURO}"))
    totals.append(("Benefice", f"{benef:.0f} {EURO}"))
    n = len(totals)
    gap, bh = 3, 15
    usable = pdf.w - 24
    bw = (usable - gap * (n - 1)) / n
    x, y = 12, pdf.get_y()
    for label, val in totals:
        pdf.set_fill_color(246, 247, 249)
        pdf.set_draw_color(224, 228, 235)
        pdf.rect(x, y, bw, bh, style="DF")
        pdf.set_xy(x + 2, y + 2.5)
        pdf.set_font(FAM, "", 6.5)
        pdf.set_text_color(*GREY)
        pdf.cell(bw - 4, 3, label.upper())
        pdf.set_xy(x + 2, y + 6.5)
        pdf.set_font(FAM, "B", 12)
        pdf.set_text_color(*(GREEN if label == "Benefice" else INK))
        pdf.cell(bw - 4, 6, val)
        x += bw + gap
    pdf.set_y(y + bh + 6)

    # ── Tableau ──
    head = FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=(46, 52, 66))
    pdf.set_font(FAM, "", 8.5)
    pdf.set_text_color(*INK)
    with pdf.table(
        col_widths=(15, 27, 27, 12, 12, 14, 17, 16),
        text_align=("LEFT", "LEFT", "LEFT", "RIGHT", "RIGHT", "RIGHT", "LEFT", "LEFT"),
        headings_style=head, cell_fill_color=(245, 246, 248),
        cell_fill_mode="ROWS", line_height=6, first_row_as_headings=True,
    ) as table:
        hr = table.row()
        for h in ("Date", "Départ", "Arrivée", "Prix", "Comm", "Net", "Paiement", "Statut"):
            hr.cell(_pdf_safe(h))
        for r in rows:
            cr = r["created"] or ""
            when = f"{cr[8:10]}/{cr[5:7]} {cr[11:16]}" if len(cr) >= 16 else cr
            tr = table.row()
            tr.cell(when)
            tr.cell(_pdf_safe(_short_loc(r["pickup"] or "?")))
            tr.cell(_pdf_safe(_short_loc(r["dropoff"] or "?")))
            tr.cell(f"{r['price']:.0f}" if r["price"] else "")
            tr.cell(f"{r['commission']:.0f}" if r["commission"] else "")
            tr.cell(f"{r['net']:.0f}" if r["net"] is not None else "")
            tr.cell(_pdf_safe(r["payment"]))
            tr.cell(_pdf_safe(STATUS_LABEL.get(r["status"], r["status"])))
        if not rows:
            table.row().cell("Aucune course sur la periode.", colspan=8, align="CENTER")

    return bytes(pdf.output())


async def send_courses_table(period: str) -> None:
    """Envoie le tableau détaillé (PDF designé) dans le DM du bot."""
    if not (bot and NOTIFY_USER_ID):
        return
    lbl = _PERIOD_LABEL.get(period, period)
    try:
        data = build_table_pdf(period)
        name = f"releve_{period}.pdf"
    except Exception as e:                       # noqa: BLE001
        log.warning("PDF KO, repli HTML : %s", e)
        data = build_table_html(period).encode("utf-8")
        name = f"courses_{period}.html"
    bio = io.BytesIO(data)
    bio.name = name
    await bot.send_file(
        NOTIFY_USER_ID, bio, force_document=True,
        attributes=[DocumentAttributeFilename(name)],
        caption=(f"📄 <b>Relevé · {lbl}</b>\n"
                 f"<i>Toutes tes courses avec les totaux — prêt à imprimer / envoyer au comptable.</i>"),
        parse_mode="html")


# ─────────────────────────── Handlers ─────────────────────────
@client.on(events.NewMessage(chats=FEED_GROUP))
@client.on(events.MessageEdited(chats=FEED_GROUP))
async def on_feed(event):
    """Suit ta position live (partagée dans le relais) + canal retour [SEND ...]."""
    # 1) position en direct : les mises à jour arrivent en éditions de message
    if event.message.geo is not None:
        save_position(event.message.geo.lat, event.message.geo.long)
        return

    # 2) canal retour : Hermès approuve -> "[SEND -1001234567890] texte de la réponse"
    m = re.match(r"^\[SEND (-?\d+)\]\s*(.+)", event.raw_text or "", re.DOTALL)
    if m:
        gid, reply = int(m.group(1)), m.group(2).strip()
        if gid not in VTC_GROUPS:
            log.warning("SEND refusé : %s hors liste VTC.", gid)
            return
        await client.send_message(gid, reply)
        log.info("Réponse envoyée dans %s.", gid)


@client.on(events.NewMessage(outgoing=True))
async def on_self_note(event):
    """/note ou /courses depuis TES Messages sauvegardés (privé à toi seul).
    Dans la conversation de quelqu'un d'autre : on NE traite PAS (il te verrait) — on
    supprime et on te prévient, car un compte ne peut pas envoyer un message invisible."""
    if not event.is_private or not bot or not NOTIFY_USER_ID:
        return
    if BOT_SELF_ID and event.chat_id == BOT_SELF_ID:   # DM du bot -> géré par on_command
        return
    txt = (event.raw_text or "").strip()
    low = txt.lower()
    if not low.startswith(("/note", "/courses", "/course")):
        return

    # Messages sauvegardés (conversation avec toi-même) = sûr, personne d'autre ne voit
    if CLIENT_SELF_ID and event.chat_id == CLIENT_SELF_ID:
        try:
            await event.delete()
        except Exception:                   # noqa: BLE001
            pass
        note = txt[5:].lstrip(" :").strip() if low.startswith("/note") else ""
        if low.startswith("/note") and note:
            await send_note_picker(note)
        else:
            await send_courses_dashboard()
        log.info("Commande via Messages sauvegardés -> UI envoyée dans le DM du bot.")
        return

    # Conversation avec quelqu'un d'autre : il a PU la voir (notification). Supprimer + prévenir.
    try:
        await event.delete()
    except Exception:                       # noqa: BLE001
        pass
    try:
        await bot.send_message(
            NOTIFY_USER_ID,
            "⚠️ <b>Attention</b> — tu as tapé une commande dans une conversation avec quelqu'un.\n"
            "Il a <b>pu la voir</b> (notification), même si je l'ai supprimée.\n"
            "👉 Utilise le <b>DM du bot</b> ou tes <b>Messages sauvegardés</b> pour rester privé.",
            parse_mode="html")
    except Exception:                       # noqa: BLE001
        pass
    log.warning("Commande tapée dans une conversation tierce -> supprimée + alerte.")


# Modes de paiement rapides (code court -> libellé) proposés après « obtenue ».
PAYMENTS = {"esp": "💵 Espèces", "cb": "💳 CB", "app": "📱 App"}


def _recap_line(r) -> str:
    """Résumé compact d'une course : départ → arrivée · prix · net · paiement."""
    trip = f"{_short_loc(r['pickup'] or '?')} → {_short_loc(r['dropoff'] or '?')}"
    bits = [f"📍 {html.escape(trip)}"]
    money = []
    if r["price"]:
        money.append(f"{r['price']:.0f} €")
    if r["net"] is not None and r["commission"]:
        money.append(f"net {r['net']:.0f} €")
    if r["payment"]:
        money.append(r["payment"])
    if money:
        bits.append(MID.join(money))
    return "\n".join(bits)


# Statut d'une course -> pastille (envoyée / en attente de validation / exécutée…)
STATUS_ICON = {"proposée": "📤", "répondue": "💬", "obtenue": "✅", "réservée": "📅",
               "effectuée": "✅", "ratée": "✕", "annulée": "🚫", "passée": "⏭️", "expirée": "⏱️"}
STATUS_LABEL = {"proposée": "envoyée", "répondue": "à valider", "obtenue": "obtenue",
                "réservée": "à venir", "effectuée": "effectuée", "ratée": "ratée",
                "annulée": "annulée", "passée": "passée", "expirée": "expirée"}


def _tiny_loc(s: str) -> str:
    """1er segment d'un lieu, tronqué — pour tenir sur un bouton."""
    seg = (s or "?").split(",")[0].strip()
    return seg if len(seg) <= 16 else seg[:15] + "…"


def _course_btn_label(r) -> str:
    hhmm = (r["created"] or "")[11:16]
    price = f" {r['price']:.0f}€" if r["price"] else ""
    ic = STATUS_ICON.get(r["status"], "•")
    return f"{ic} {hhmm} {_tiny_loc(r['pickup'])}→{_tiny_loc(r['dropoff'])}{price}"


async def send_note_picker(note_text: str) -> None:
    """Envoie dans le DM du bot la liste des courses récentes : tape celle à annoter."""
    if not (bot and NOTIFY_USER_ID):
        return
    rs = store.recent(8)
    if not rs:
        await bot.send_message(NOTIFY_USER_ID, "Aucune course à annoter pour l'instant.")
        return
    rows = [[Button.inline(_course_btn_label(r), f"n|{r['id']}".encode())] for r in rs]
    rows.append([Button.inline("✖️ Annuler", b"nc")])
    sent = await bot.send_message(
        NOTIFY_USER_ID,
        f"📝 <b>Nouvelle note</b>\n« {html.escape(note_text)} »\n\n<i>Sur quelle course ?</i>",
        buttons=rows, parse_mode="html")
    _pending_note[sent.id] = note_text


async def send_courses_dashboard() -> None:
    """Tableau des courses récentes (par statut) — touche une course pour agir dessus."""
    if not (bot and NOTIFY_USER_ID):
        return
    rs = store.recent(8)
    if not rs:
        await bot.send_message(NOTIFY_USER_ID, "Aucune course enregistrée pour l'instant.")
        return
    rows = [[Button.inline(_course_btn_label(r), f"c|{r['id']}".encode())] for r in rs]
    await bot.send_message(
        NOTIFY_USER_ID,
        "🗂️ <b>Tes courses récentes</b>\n"
        f"{HR}\n"
        "📤 envoyée · 💬 à valider · ✅ obtenue · ✕ ratée\n"
        "<i>Touche une course pour la valider ou l'annoter.</i>",
        buttons=rows, parse_mode="html")


def _course_detail(r):
    """(texte, boutons) de la fiche d'une course : statut, chiffres, note + édition fiable."""
    ic = STATUS_ICON.get(r["status"], "•")
    lbl = STATUS_LABEL.get(r["status"], r["status"])
    trip = f"{_short_loc(r['pickup'] or '?')} → {_short_loc(r['dropoff'] or '?')}"
    lines = [f"{ic} <b>{lbl.capitalize()}</b>{MID}{(r['created'] or '')[11:16]}{MID}<code>#{r['id']}</code>",
             HR,
             f"📍 {html.escape(trip)}"]
    money = [f"💶 {r['price']:.0f} €" if r["price"] else "💶 prix ?"]
    if r["commission"]:
        money.append(f"🧾 comm {r['commission']:.0f} €")
        money.append(f"💚 net {r['net']:.0f} €")
    if r["payment"]:
        money.append(str(r["payment"]))
    lines.append(MID.join(money))
    if r["note"]:
        lines.append(f"📝 <i>{html.escape(r['note'])}</i>")
    btns = []
    if r["status"] in ("proposée", "répondue"):
        btns.append([Button.inline("✅ Obtenue", f"o|{r['id']}|1".encode()),
                     Button.inline("✕ Ratée", f"o|{r['id']}|0".encode())])
    elif r["status"] == "réservée":                  # réservation à venir -> valider après la course
        btns.append([Button.inline("✅ Effectuée", f"eff|{r['id']}|1".encode()),
                     Button.inline("❌ Annulée", f"eff|{r['id']}|0".encode())])
    # édition fiable — chaque bouton cible CETTE course précise (#id)
    btns.append([Button.inline("✏️ Prix", f"ed|{r['id']}|prix".encode()),
                 Button.inline("🧾 Commission", f"ed|{r['id']}|comm".encode())])
    btns.append([Button.inline("💳 Paiement", f"pk|{r['id']}".encode()),
                 Button.inline("📝 Note", f"ed|{r['id']}|note".encode())])
    return "\n".join(lines), btns


async def apply_edit(event, cid: int, field: str, txt: str) -> None:
    """Applique une édition (note/prix/comm) à la course #cid et confirme."""
    r = store.get(cid)
    if not r:
        await event.respond("Course introuvable.")
        return
    trip = f"{_short_loc(r['pickup'] or '?')} → {_short_loc(r['dropoff'] or '?')}"
    if field == "note":
        nr = store.append_note(cid, txt)
        await event.respond(f"📝 <b>Note</b> · {html.escape(trip)}\n« {html.escape(nr['note'])} »",
                            parse_mode="html")
        return
    m = re.search(r"(\d+(?:[.,]\d+)?)", txt)
    if not m:
        await event.respond("Écris un nombre (ex. 45).")
        return
    val = float(m.group(1).replace(",", "."))
    if field == "prix":
        nr = store.adjust_amount(cid, price=val, rate=(COMMISSION_RATE or None))
        extra = f" · net {nr['net']:.0f} €" if nr["commission"] else ""
        await event.respond(f"✏️ <b>Prix corrigé</b> · {html.escape(trip)}\n💶 {val:.0f} €{extra}",
                            parse_mode="html")
    else:  # comm
        nr = store.adjust_amount(cid, commission=val)
        await event.respond(f"🧾 <b>Commission corrigée</b> · {html.escape(trip)}\n"
                            f"{val:.0f} € · net {nr['net']:.0f} €", parse_mode="html")


def _osa_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


async def add_event_to_calendar(summary: str, dt, description: str = "", location: str = "") -> bool:
    """Ajoute DIRECTEMENT l'événement dans l'app Calendrier (macOS) -> sync iCloud -> iPhone.
    Rappels 24 h et 1 h avant. Retourne False si l'automatisation échoue (permission, etc.)."""
    script = (
        "set d to current date\n"
        "set time of d to 0\nset day of d to 1\n"
        f"set year of d to {dt.year}\nset month of d to {dt.month}\nset day of d to {dt.day}\n"
        f"set hours of d to {dt.hour}\nset minutes of d to {dt.minute}\n"
        "set d2 to d + (60 * 60)\n"
        'tell application "Calendar"\n'
        f'  tell calendar "{_osa_escape(CALENDAR_NAME)}"\n'
        f'    set ev to make new event with properties {{summary:"{_osa_escape(summary)}", '
        f'start date:d, end date:d2, description:"{_osa_escape(description)}", '
        f'location:"{_osa_escape(location)}"}}\n'
        "    tell ev\n"
        "      make new display alarm at end with properties {trigger interval:-1440}\n"
        "      make new display alarm at end with properties {trigger interval:-60}\n"
        "    end tell\n  end tell\nend tell\n"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(script.encode("utf-8")), timeout=20)
        if proc.returncode == 0:
            return True
        log.warning("Agenda KO : %s", (err or b"").decode("utf-8", "ignore")[:200])
        return False
    except Exception as e:                       # noqa: BLE001
        log.warning("Agenda exception : %s", e)
        return False


async def _calendar_selftest() -> None:
    """Vérifie au démarrage que le service peut ÉCRIRE dans l'agenda (crée + supprime une sonde)."""
    script = (
        "set d to current date\nset year of d to 2001\n"
        'tell application "Calendar"\n'
        f'  tell calendar "{_osa_escape(CALENDAR_NAME)}"\n'
        '    set ev to make new event with properties {summary:"__proxia_probe__", start date:d, end date:d}\n'
        "    delete ev\n  end tell\nend tell\nreturn \"ok\"\n"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(script.encode("utf-8")), timeout=20)
        if proc.returncode == 0:
            log.info("Agenda : écriture OK sur « %s » -> ajout auto des réservations actif.", CALENDAR_NAME)
        else:
            log.warning("Agenda : ÉCRITURE REFUSÉE sur « %s » (autorise Calendrier dans Réglages > "
                        "Confidentialité > Automatisation) : %s",
                        CALENDAR_NAME, (err or b"").decode("utf-8", "ignore")[:150])
    except Exception as e:                       # noqa: BLE001
        log.warning("Agenda : self-test écriture bloqué (timeout/permission) : %r", e)


async def add_reservation_to_calendar(cid: int) -> bool:
    """Ajoute une réservation (par id) à l'agenda, avec date/heure lues du texte."""
    r = store.get(cid)
    if not r:
        return False
    dt = reservation_datetime(r["raw"] or "")
    if dt is None:
        return False
    trip = f"{_short_loc(r['pickup'] or '?')} → {_short_loc(r['dropoff'] or '?')}"
    price = f"{r['price']:.0f} €" if r["price"] else ""
    desc = trip + (f" · {price}" if price else "")
    return await add_event_to_calendar(f"🚕 VTC · {trip}", dt, desc, r["pickup"] or "")


async def send_reservation_ics(cid: int) -> None:
    """Envoie un fichier .ics (agenda) pour une réservation, avec rappels 24 h et 1 h avant."""
    if not (bot and NOTIFY_USER_ID):
        return
    r = store.get(cid)
    if not r:
        return
    trip = f"{_short_loc(r['pickup'] or '?')} → {_short_loc(r['dropoff'] or '?')}"
    dt = reservation_datetime(r["raw"] or "")
    if dt is None:
        await bot.send_message(
            NOTIFY_USER_ID,
            f"⚠️ Je n'ai pas pu lire la date/heure de cette réservation ({html.escape(trip)}). "
            f"Ajoute-la à la main.", parse_mode="html")
        return
    price = f"{r['price']:.0f} €" if r["price"] else ""
    desc = trip + (f" · {price}" if price else "") + \
        (f" · net {r['net']:.0f} €" if (r["net"] is not None and r["commission"]) else "")
    ics = build_ics(f"🚕 VTC · {trip}", dt, desc, r["pickup"] or "")
    bio = io.BytesIO(ics.encode("utf-8"))
    bio.name = "reservation.ics"
    await bot.send_file(
        NOTIFY_USER_ID, bio, force_document=True,
        attributes=[DocumentAttributeFilename("reservation.ics")],
        caption=(f"📅 <b>{dt.strftime('%d/%m à %Hh%M')}</b> · {html.escape(trip)}\n"
                 f"<i>Ouvre le fichier pour l'ajouter à ton agenda — rappels 24 h et 1 h avant.</i>"),
        parse_mode="html")


if bot is not None:
    @bot.on(events.CallbackQuery)
    async def on_button(event):
        """Tap sur un bouton d'alerte -> poste la réponse, puis suit le résultat (obtenue/ratée)."""
        if NOTIFY_USER_ID and event.sender_id != NOTIFY_USER_ID:
            await event.answer("Non autorisé", alert=True)
            return
        data = event.data.decode("utf-8", "ignore")

        # ── ❌ Passer : la course n'est pas prise ──
        if data == "x":
            cid = _pending_alerts.pop(event.message_id, None)
            if cid:
                store.set_status(cid, "passée")
            await event.edit("⏭️ <b>Course passée</b>", parse_mode="html")
            return

        # ── 📄 Tableau détaillé (fichier HTML) ──
        if data.startswith("tbl|"):
            await event.answer("📄 Génération du tableau…")
            await send_courses_table(data.split("|", 1)[1] or "mois")
            return

        # ── ✅/❌ Résultat : le chauffeur confirme s'il a eu la course ──
        if data.startswith("o|"):
            try:
                _, scid, won = data.split("|", 2)
                cid = int(scid)
            except ValueError:
                await event.answer("Donnée invalide", alert=True)
                return
            if won == "1":
                r = store.get(cid)
                if r["kind"] == "resa":
                    # Réservation : validée mais À VENIR -> ne compte pas encore dans le CA.
                    store.set_status(cid, "réservée")
                    dtm = reservation_datetime(r["raw"] or "")
                    if dtm is not None:
                        store.set_sched(cid, dtm.timestamp())
                    cal_ok = await add_reservation_to_calendar(cid)
                    cal_line = "\n📅 <i>Ajouté à ton agenda — rappels 24 h et 1 h.</i>" if cal_ok else ""
                    rows = []
                    if not cal_ok:
                        rows.append([Button.inline("📅 Fichier agenda", f"cal|{cid}".encode())])
                    rows.append([Button.inline("✅ Effectuée", f"eff|{cid}|1".encode()),
                                 Button.inline("❌ Annulée", f"eff|{cid}|0".encode())])
                    when = dtm.strftime("%d/%m à %Hh%M") if dtm else "date ?"
                    await event.edit(
                        f"📅 <b>Réservation notée</b> · {when}\n{HR}\n{_recap_line(r)}{cal_line}\n\n"
                        f"<i>Je te redemanderai après la course de la valider — ou marque-la ici.</i>",
                        buttons=rows, parse_mode="html")
                else:
                    store.set_status(cid, "obtenue")
                    pay_row = [Button.inline(lbl, f"p|{cid}|{code}".encode())
                               for code, lbl in PAYMENTS.items()]
                    await event.edit(
                        f"✅ <b>Course obtenue</b>\n{HR}\n{_recap_line(r)}\n\n💳 <i>Mode de paiement ?</i>",
                        buttons=[pay_row], parse_mode="html")
            else:
                store.set_status(cid, "ratée")
                await event.edit("✕ <b>Course ratée</b>{}<i>enregistré</i>".format(MID), parse_mode="html")
            return

        # ── 📅 Réservation : effectuée (paiement) ou annulée ──
        if data.startswith("eff|"):
            try:
                _, scid, ok = data.split("|", 2)
                cid = int(scid)
            except ValueError:
                await event.answer("Donnée invalide", alert=True)
                return
            r = store.get(cid)
            if not r:
                await event.answer("Course introuvable", alert=True)
                return
            if ok == "1":
                store.set_status(cid, "effectuée")
                pay_row = [Button.inline(lbl, f"p|{cid}|{code}".encode())
                           for code, lbl in PAYMENTS.items()]
                await event.edit(
                    f"✅ <b>Réservation effectuée</b>\n{HR}\n{_recap_line(r)}\n\n💳 <i>Mode de paiement ?</i>",
                    buttons=[pay_row], parse_mode="html")
            else:
                store.set_status(cid, "annulée")
                await event.edit(f"🚫 <b>Réservation annulée</b>{MID}<i>ne compte pas</i>", parse_mode="html")
            return

        # ── 📅 Ajouter une réservation à l'agenda (envoi d'un .ics avec rappels) ──
        if data.startswith("cal|"):
            try:
                cid = int(data.split("|", 1)[1])
            except ValueError:
                await event.answer("Donnée invalide", alert=True)
                return
            await event.answer("📅 Fichier agenda envoyé")
            await send_reservation_ics(cid)
            return

        # ── 💳 Mode de paiement : tap après « obtenue » ──
        if data.startswith("p|"):
            try:
                _, scid, code = data.split("|", 2)
                cid = int(scid)
            except ValueError:
                await event.answer("Donnée invalide", alert=True)
                return
            pay = PAYMENTS.get(code, code)
            store.set_payment(cid, pay)
            r = store.get(cid)
            title = "Réservation effectuée" if r["status"] == "effectuée" else "Course obtenue"
            await event.edit(
                f"✅ <b>{title}</b>\n{HR}\n{_recap_line(r)}\n\n"
                f"<i>📝 /note pour ajouter un détail</i>",
                parse_mode="html")
            return

        # ── 📝 Sélecteur « sur quelle course ? » : tap sur une course pour y lier la note ──
        if data == "nc":
            _pending_note.pop(event.message_id, None)
            await event.edit("✖️ Annulé")
            return
        if data.startswith("n|"):
            try:
                cid = int(data.split("|", 1)[1])
            except ValueError:
                await event.answer("Donnée invalide", alert=True)
                return
            note = _pending_note.pop(event.message_id, None)
            if not note:
                await event.answer("Note expirée — retape /note.", alert=True)
                return
            nr = store.append_note(cid, note)
            trip = f"{_short_loc(nr['pickup'] or '?')} → {_short_loc(nr['dropoff'] or '?')}"
            await event.edit(f"📝 <b>Note ajoutée</b> · {html.escape(trip)}\n« {html.escape(nr['note'])} »",
                             parse_mode="html")
            return

        # ── 🗂️ Tableau de bord : ouvrir la fiche d'une course ──
        if data.startswith("c|"):
            try:
                cid = int(data.split("|", 1)[1])
            except ValueError:
                await event.answer("Donnée invalide", alert=True)
                return
            r = store.get(cid)
            if not r:
                await event.answer("Course introuvable", alert=True)
                return
            text, btns = _course_detail(r)
            await event.edit(text, buttons=btns, parse_mode="html")
            return

        # ── 📝 Bouton « Ajouter une note » d'une fiche : le prochain message DM = la note ──
        # ── ✏️ Éditer un champ d'une course (prix / commission / note) — cible CETTE course ──
        if data.startswith(("ed|", "nn|")):
            try:
                parts = data.split("|")
                cid = int(parts[1])
                field = parts[2] if len(parts) > 2 else "note"
            except (ValueError, IndexError):
                await event.answer("Donnée invalide", alert=True)
                return
            _awaiting[NOTIFY_USER_ID] = (cid, field)
            r = store.get(cid)
            trip = f"{_short_loc(r['pickup'] or '?')} → {_short_loc(r['dropoff'] or '?')}" if r else "?"
            ask = {"prix": "le nouveau <b>prix</b> (ex. 45)",
                   "comm": "la <b>commission</b> en € (ex. 8)",
                   "note": "la <b>note</b> (paiement, détail…)"}.get(field, "la valeur")
            await event.answer("✍️ Écris la valeur en message")
            await bot.send_message(
                NOTIFY_USER_ID, f"✍️ Écris {ask} pour <b>{html.escape(trip)}</b> <code>#{cid}</code>.",
                parse_mode="html")
            return

        # ── 💳 Ouvrir le choix du paiement depuis une fiche ──
        if data.startswith("pk|"):
            try:
                cid = int(data.split("|", 1)[1])
            except ValueError:
                await event.answer("Donnée invalide", alert=True)
                return
            pay_row = [Button.inline(lbl, f"p|{cid}|{code}".encode()) for code, lbl in PAYMENTS.items()]
            await event.edit("💳 <i>Mode de paiement ?</i>", buttons=[pay_row], parse_mode="html")
            return

        # ── Envoi de la réponse (SP / minutes / ok) dans le groupe source ──
        try:
            _, sgid, smid, reply = data.split("|", 3)
            gid, mid = int(sgid), int(smid)
        except ValueError:
            await event.answer("Donnée invalide", alert=True)
            return
        if gid not in VTC_GROUPS:
            await event.answer("Groupe non autorisé", alert=True)
            return
        cid = _pending_alerts.pop(event.message_id, None)
        if cid is None:                                            # expirée ou déjà répondue
            await event.answer("⏱️ Alerte expirée — rien envoyé.", alert=True)
            return
        try:
            await client.send_message(gid, reply, reply_to=mid)   # réponse citée à la course
        except Exception:                                          # course supprimée/expirée
            await client.send_message(gid, reply)
        store.set_status(cid, "répondue")
        store.set_reply(cid, reply)
        outcome = [[Button.inline("✅ Obtenue", f"o|{cid}|1".encode()),
                    Button.inline("❌ Ratée", f"o|{cid}|0".encode())]]
        link = course_link(gid, mid)
        if link:                                              # vérifier si on t'a répondu dans le groupe
            outcome.append([Button.url("🔗 Voir si on t'a répondu", link)])
        await event.edit(
            f"✅ <b>Réponse envoyée</b>{MID}« {html.escape(reply)} »\n\n"
            f"<i>Ouvre le lien pour voir si on t'a répondu, puis dis-moi :</i>",
            buttons=outcome, parse_mode="html")
        log.info("Réponse « %s » envoyée dans %s via bouton.", reply, gid)

    @bot.on(events.NewMessage(from_users=NOTIFY_USER_ID or None))
    async def on_bot_location_new(event):
        """Partage de position en direct DANS le DM du bot -> source de position (remplace le groupe)."""
        if event.message.geo is None:
            return
        was_missing = load_position() is None
        save_position(event.message.geo.lat, event.message.geo.long)
        period = getattr(event.message.media, "period", None)   # durée du partage live (ex. 28800 = 8 h)
        if period:
            schedule_live_expiry(period)
        if was_missing:
            await event.respond(
                "✅ <b>Position activée</b>\n"
                f"{HR}\n"
                "Je surveille les courses proches. 🚕 <i>(garde le partage en direct actif.)</i>",
                parse_mode="html")

    @bot.on(events.MessageEdited(from_users=NOTIFY_USER_ID or None))
    async def on_bot_location_edit(event):
        """Mises à jour de la position en direct (arrivent en éditions de message)."""
        if event.message.geo is not None:
            save_position(event.message.geo.lat, event.message.geo.long)

    @bot.on(events.NewMessage(from_users=NOTIFY_USER_ID or None))
    async def on_command(event):
        """Commandes DM : /resume, /courses, /note, /prix, /comm, /aide (+ saisie de note en attente)."""
        txt = (event.raw_text or "").strip()
        low = txt.lower()
        if not txt:                          # message sans texte (position, média…) -> ignore
            return
        # saisie en attente (bouton ✏️ Prix / 🧾 Commission / 📝 Note d'une fiche)
        aw = _awaiting.get(NOTIFY_USER_ID)
        if aw is not None:
            if txt.startswith("/"):
                _awaiting.pop(NOTIFY_USER_ID, None)             # une commande annule la saisie
            else:
                cid, field = aw
                _awaiting.pop(NOTIFY_USER_ID, None)
                await apply_edit(event, cid, field, txt)
                return
        if low.startswith(("/courses", "/course")):
            await send_courses_dashboard()
            return
        if low.startswith(("/resume", "/résumé", "/bilan", "/jour", "/semaine", "/mois", "/tout")):
            period = "jour"
            if low.startswith("/semaine") or "semaine" in low:
                period = "semaine"
            elif low.startswith("/mois") or "mois" in low:
                period = "mois"
            elif low.startswith("/tout") or "tout" in low or "total" in low:
                period = "tout"
            btn = [[Button.inline("📄 Tableau détaillé", f"tbl|{period}".encode())]]
            await event.respond(build_report(period), buttons=btn,
                                parse_mode="html", link_preview=False)
        elif low.startswith(("/tableau", "/table", "/export")):
            period = "mois"
            if "semaine" in low:
                period = "semaine"
            elif "jour" in low or "auj" in low:
                period = "jour"
            elif "tout" in low or "total" in low:
                period = "tout"
            await send_courses_table(period)
        elif low.startswith("/prix"):
            m = re.search(r"(\d+(?:[.,]\d+)?)", txt)
            r = store.last_realized_course()
            if m and r:
                await apply_edit(event, r["id"], "prix", txt)
                await event.respond("<i>Pas la bonne course ? → /courses pour choisir précisément.</i>",
                                    parse_mode="html")
            else:
                await event.respond("Usage : <b>/prix 55</b> (dernière course faite) — ou /courses pour choisir.",
                                    parse_mode="html")
        elif low.startswith("/comm"):
            m = re.search(r"(\d+(?:[.,]\d+)?)", txt)
            r = store.last_realized_course()
            if m and r:
                await apply_edit(event, r["id"], "comm", txt)
                await event.respond("<i>Pas la bonne course ? → /courses pour choisir précisément.</i>",
                                    parse_mode="html")
            else:
                await event.respond("Usage : <b>/comm 8</b> (dernière course faite) — ou /courses pour choisir.",
                                    parse_mode="html")
        elif low.startswith(("/depense", "/dépense", "/frais")):
            rest = txt.split(None, 1)[1] if len(txt.split(None, 1)) > 1 else ""
            m = re.search(r"(\d+(?:[.,]\d+)?)", rest)
            if not m:
                await event.respond("Usage : <b>/depense 40 essence</b> (essence, péage, lavage…)",
                                    parse_mode="html")
            else:
                amt = float(m.group(1).replace(",", "."))
                label = (rest[:m.start()] + rest[m.end():]).strip(" :-€").strip() or "frais"
                store.add_expense(amt, label)
                fr = store.expenses_total("jour")
                await event.respond(
                    f"⛽ <b>Frais ajouté</b> · {amt:.0f} € · {html.escape(label)}\n"
                    f"<i>Total frais du jour : {fr:.0f} € — /resume pour le bénéfice.</i>",
                    parse_mode="html")
        elif low.startswith(("/objectif", "/but")):
            m = re.search(r"(\d+(?:[.,]\d+)?)", txt)
            if not m:
                cur = store.get_setting("objectif_jour")
                await event.respond(
                    f"🎯 Objectif du jour actuel : <b>{cur or 'aucun'} €</b>.\n"
                    f"Usage : <b>/objectif 200</b>", parse_mode="html")
            else:
                obj = float(m.group(1).replace(",", "."))
                store.set_setting("objectif_jour", obj)
                await event.respond(f"🎯 <b>Objectif du jour : {obj:.0f} €</b> — visible dans /resume.",
                                    parse_mode="html")
        elif low.startswith("/note"):
            note = txt[5:].lstrip(" :").strip()
            if not note:
                await event.respond(
                    "Usage : <b>/note payé en espèces</b>\n"
                    "→ je te demande ensuite sur quelle course l'attacher.\n"
                    "<i>Astuce : réponds à une carte d'alerte avec /note … pour cibler direct.</i>",
                    parse_mode="html")
                return
            # réponse à une carte d'alerte -> ciblage direct ; sinon -> sélecteur de course
            rid = event.message.reply_to.reply_to_msg_id if event.message.reply_to else None
            target = store.find_by_alert(rid) if rid else None
            if target is not None:
                nr = store.append_note(target["id"], note)
                trip = f"{_short_loc(nr['pickup'] or '?')} → {_short_loc(nr['dropoff'] or '?')}"
                await event.respond(f"📝 Note ajoutée · {html.escape(trip)}\n« {html.escape(nr['note'])} »",
                                    parse_mode="html")
            else:
                await send_note_picker(note)
        elif low.startswith(("/aide", "/help", "/start")):
            await event.respond(
                "🚕 <b>Assistant courses</b>\n"
                f"{HR}\n"
                f"📊 <b>/resume</b>{MID}bilan &amp; bénéfice <i>· /semaine · /mois</i>\n"
                f"📄 <b>/tableau</b> <i>mois</i>{MID}toutes tes courses en tableau\n"
                f"🗂️ <b>/courses</b>{MID}valider, corriger prix/comm, annoter\n"
                f"⛽ <b>/depense</b> 40 essence{MID}frais → bénéfice réel\n"
                f"🎯 <b>/objectif</b> 200{MID}ton but du jour\n"
                f"📝 <b>/note</b> <i>texte</i>{MID}sur une course\n\n"
                "<blockquote>Corrige un prix de façon SÛRE via <b>/courses</b> → ouvre la course "
                "→ ✏️ Prix. Sous une alerte : 1 tap, puis ✅ / ✕ et le paiement 💵 💳 📱.\n"
                "🔒 Utilise le bot ou tes <b>Messages sauvegardés</b> — jamais le chat de quelqu'un.</blockquote>",
                parse_mode="html")


@client.on(events.NewMessage(chats=VTC_GROUPS, incoming=True))
async def on_course(event):
    key = (event.chat_id, event.id)
    if key in _seen_courses:
        return
    _seen_courses.add(key)

    text = event.raw_text or ""
    if not is_course(text):
        return

    # un message peut contenir PLUSIEURS réservations -> une alerte par course
    segments = split_courses(text)
    if len(segments) > 1:
        log.info("Message multi-courses -> %d segments (gid %s).", len(segments), event.chat_id)
    for seg in segments:
        await _handle_course(seg, event.chat_id, event.id)


async def _handle_course(text: str, gid: int, mid: int) -> None:
    """Traite UNE course (immédiate ou réservation) : filtres, dédup, décision, alerte."""
    if requires_van(text):                      # tu ne prends pas les courses van
        log.info("Course van ignorée.")
        return

    # même course repostée dans un autre groupe -> une seule alerte / une seule réponse
    fp = course_fingerprint(text)
    if fp is not None:
        if is_duplicate_course(fp):
            log.info("Doublon inter-groupes ignoré (gid %s).", gid)
            return
    elif is_duplicate_course(course_text_fingerprint(text), DEDUP_TEXT_WINDOW_SECONDS):
        # Pas de code postal exploitable : on retombe sur le texte, fenêtre courte.
        log.info("Repost identique ignoré (gid %s, fenêtre %d s).", gid, DEDUP_TEXT_WINDOW_SECONDS)
        return

    # Réservation -> logique rentabilité (€/km). Immédiat -> logique proximité (ETA).
    if is_reservation(text):
        await handle_reservation(text, gid, mid)
        return

    src = load_position()
    if src is None:
        await warn_position()          # rappel unique (throttlé), pas un message par course
        return

    async with aiohttp.ClientSession() as session:
        pickup = None
        pickup_cand = None
        for cand in extract_pickup(text):
            geo = await geocode(session, cand)
            await asyncio.sleep(1.1)          # respecte la limite Nominatim (~1 req/s)
            if geo:
                pickup = geo
                pickup_cand = cand            # texte propre du départ (pour l'affichage)
                break

        if pickup is None:
            await escalate_raw(text, src, "adresse non géocodée", gid, mid)
            return

        lat, lon, name = pickup
        eta = await route_eta(session, src, (lat, lon))
        if eta is None:
            await escalate_raw(text, src, "OSRM indisponible", gid, mid)
            return

        eta_s, dist_m = eta
        if eta_s <= THRESHOLD_SECONDS:
            await escalate(session, text, pickup_cand, (lat, lon), eta_s, dist_m, gid, mid)
        else:
            log.info("Course ignorée : ETA %.0f min > seuil | PEC=%r (%s) | brut=%r",
                     eta_s / 60, pickup_cand, name, text[:80].replace("\n", " "))


async def _set_bot_menu() -> None:
    """Enregistre les commandes -> menu « / » dans la conversation du bot."""
    cmds = [
        ("resume",  "Bilan du jour — CA, commission, net"),
        ("semaine", "Bilan de la semaine"),
        ("mois",    "Bilan du mois"),
        ("courses", "Tes courses — valider, corriger prix/comm, annoter"),
        ("tableau", "Tableau détaillé de toutes tes courses (fichier)"),
        ("depense", "Ajouter un frais — /depense 40 essence"),
        ("objectif", "Fixer ton objectif du jour — /objectif 200"),
        ("note",    "Ajouter une note — je demande sur quelle course"),
        ("prix",    "Corriger le prix de la dernière course faite"),
        ("comm",    "Corriger la commission de la dernière course"),
        ("aide",    "Aide et liste des commandes"),
    ]
    try:
        await bot(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(), lang_code="",
            commands=[BotCommand(c, d) for c, d in cmds]))
        log.info("Menu bot configuré (%d commandes).", len(cmds))
    except Exception as e:                       # noqa: BLE001
        log.warning("Menu bot KO : %s", e)


async def _resolve_group_names() -> None:
    """Nom lisible de chaque groupe (pour le résumé « par groupe »)."""
    for gid in VTC_GROUPS:
        try:
            ent = await client.get_entity(gid)
            GROUP_NAMES[gid] = getattr(ent, "title", None) or str(gid)
        except Exception:                     # noqa: BLE001
            GROUP_NAMES[gid] = str(gid)


async def _amain() -> None:
    store.init(COURSES_DB)            # journal des courses (persistant)
    await client.start()              # session déjà autorisée -> pas de prompt
    global CLIENT_SELF_ID
    CLIENT_SELF_ID = (await client.get_me()).id      # = tes Messages sauvegardés
    await _resolve_group_names()
    await _calendar_selftest()
    tasks = [client.run_until_disconnected()]
    if bot is not None:
        await bot.start(bot_token=BOT_TOKEN)
        global BOT_SELF_ID
        BOT_SELF_ID = (await bot.get_me()).id
        await _set_bot_menu()
        log.info("Bot boutons actif -> DM %s.", NOTIFY_USER_ID)
        tasks.append(bot.run_until_disconnected())
        tasks.append(_reservation_reminder_loop())
        tasks.append(_daily_summary_loop())
    await asyncio.gather(*tasks)


def main() -> None:
    log.info("Pont VTC démarré. Groupes : %s | relais : %s | seuil : %s min | boutons : %s",
             VTC_GROUPS, FEED_GROUP, THRESHOLD_SECONDS // 60, "oui" if bot else "non (fallback Hermès)")
    asyncio.run(_amain())


if __name__ == "__main__":
    main()

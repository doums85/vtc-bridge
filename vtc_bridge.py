#!/usr/bin/env python3
"""
Pont VTC <-> Telegram (Telethon), multi-chauffeurs.

Un chauffeur = un compte Telegram = un profil (table `drivers`) avec ses groupes, sa
position en direct, ses réglages et son journal de courses. Le pont ouvre une session
Telethon par chauffeur et un seul bot partagé pour les alertes.

Rôle, pour chaque chauffeur :
  1. Lit les groupes VTC où il est simple membre (impossible avec le Bot API).
  2. Suit sa position en direct (live location partagée dans le DM du bot).
  3. Pour chaque course postée : extrait l'adresse de prise en charge, géocode (Nominatim),
     calcule l'ETA en voiture. Il ne reçoit QUE ce qui passe SES réglages (temps, distance,
     prix minimum, €/km, rayon des réservations…) : /reglages dans le DM du bot.
  4. Ses taps sur « Accepter » postent la réponse dans le groupe depuis SON compte.

Ajouter un chauffeur : `python add_driver.py add` (connexion Telegram de son compte).

Dépendances : pip install telethon aiohttp python-dotenv fpdf2

⚠️ Automatiser un compte utilisateur est en zone grise des CGU Telegram.
   Le pont n'envoie que ce que le chauffeur a validé d'un tap. N'en fais pas un
   auto-répondeur massif : c'est ça qui fait bannir un compte.
"""

from __future__ import annotations

import os
import re
import sys
import html
import json
import math
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
import drivers as D

load_dotenv()

# ─────────────────────────── Config ───────────────────────────
API_ID   = int(os.environ["TG_API_ID"])            # my.telegram.org (commun à tous les comptes)
API_HASH = os.environ["TG_API_HASH"]
SESSION  = os.environ.get("TG_SESSION", "vtc_session")

# Chauffeur d'origine (avant le multi-chauffeurs). Au premier lancement sur une base sans
# aucun chauffeur, il est créé depuis ces variables : session TG_SESSION, id NOTIFY_USER_ID,
# groupes VTC_GROUPS, relais FEED_GROUP. Ensuite, la table `drivers` fait foi.
LEGACY_GROUPS = [int(x) for x in os.environ.get("VTC_GROUPS", "").split(",") if x.strip()]
LEGACY_FEED_GROUP = int(os.environ.get("FEED_GROUP", "0") or "0")
# mention du bot Hermès (ex "@mon_hermes_bot") pour déclencher son traitement des alertes.
BOT_MENTION = os.environ.get("BOT_MENTION", "").strip()

# Les seuils (temps, distance, prix min, €/km, commission, expiration…) sont des RÉGLAGES
# PAR CHAUFFEUR : définition, bornes et valeurs par défaut dans drivers.py. Les variables
# d'environnement THRESHOLD_SECONDS, EURKM_MIN, etc. ne servent plus que de valeurs par
# défaut pour un chauffeur qui n'a rien modifié.
EXPIRE_SECONDS = int(os.environ.get("EXPIRE_SECONDS", 15 * 60))   # défaut de la fenêtre de dédup
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
COURSES_DB = os.path.join(STATE_DIR, "courses.db")
SESSIONS_DIR = os.path.join(STATE_DIR, "sessions")      # sessions Telethon des chauffeurs ajoutés
os.makedirs(STATE_DIR, exist_ok=True)

# Agenda macOS (synchronisé iCloud -> iPhone) où ajouter les réservations obtenues.
# Réservé au chauffeur « propriétaire » : l'agenda est celui de la machine du serveur.
CALENDAR_NAME = os.environ.get("CALENDAR_NAME", "Calendrier").strip()

# Boutons inline : bot DÉDIÉ (≠ bot Hermès), partagé par tous les chauffeurs ; chaque alerte
# part en DM vers le chauffeur concerné (son id Telegram = son chat avec le bot).
# Si BOT_TOKEN vide -> fallback : alertes texte dans le relais du chauffeur pour Hermès.
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "").strip()
NOTIFY_USER_ID = int(os.environ.get("NOTIFY_USER_ID", "0") or "0")   # chauffeur d'origine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vtc")
logging.getLogger("fontTools").setLevel(logging.ERROR)   # PDF : coupe le bruit du sous-échantillonnage

bot = TelegramClient("vtc_bot", API_ID, API_HASH) if BOT_TOKEN else None  # bot dédié aux boutons
GROUP_NAMES: dict[int, str] = {}              # id groupe -> nom lisible (résolu au démarrage)
BOT_SELF_ID: int = 0                           # id du bot (pour ne pas doubler /note dans son DM)


class DriverRT:
    """État d'exécution d'UN chauffeur. Rien de ce qui touche une alerte, une position, un
    déduplicateur ou une saisie en attente n'est global : tout vit ici, par chauffeur.
    Les ids de messages du bot se répètent d'un chat à l'autre, donc les tables d'alertes
    ne peuvent pas être partagées."""

    def __init__(self, profile: "D.Profile"):
        self.p = profile
        self.client: "TelegramClient | None" = None
        self.self_id = 0                              # id du compte lu par la session (garde-fou)
        self.seen_courses: set[tuple[int, int]] = set()   # (groupe, message) déjà traités
        self.pending_alerts: dict[int, int] = {}      # id message alerte DM -> id course (actives)
        self.alert_ctx: dict[int, dict] = {}          # id message alerte DM -> contexte « Accepter »
        self.dup_seen: dict[str, float] = {}          # empreinte -> ts : dédup inter-groupes
        self.pending_note: dict[int, str] = {}        # id message sélecteur -> note en attente
        self.awaiting: "tuple | None" = None          # (id course, champ) : prochain message = saisie
        self.last_pos_warn = 0.0
        self.pos_expiry_task = None
        self.runner = None                            # tâche run_until_disconnected

    @property
    def id(self) -> int:
        return self.p.id

    @property
    def tag(self) -> str:                             # préfixe de log lisible
        return f"[{self.p.name or self.p.id}]"

    def cfg(self, key: str):
        """Réglage effectif de ce chauffeur (sa valeur, sinon le défaut)."""
        return self.p.get(key)


DRIVERS: dict[int, DriverRT] = {}                 # id Telegram -> chauffeur connecté


def group_name(gid: int) -> str:
    return GROUP_NAMES.get(gid, str(gid))


# ─────────────────────── Position live ────────────────────────
def pos_path(driver_id: int) -> str:
    return os.path.join(STATE_DIR, f"position_{driver_id}.json")


def save_position(drv: DriverRT, lat: float, lon: float) -> None:
    with open(pos_path(drv.id), "w") as f:
        json.dump({"lat": lat, "lon": lon, "ts": time.time()}, f)
    log.info("%s Position mise à jour : %.5f, %.5f", drv.tag, lat, lon)


def load_position(drv: DriverRT) -> dict | None:
    try:
        with open(pos_path(drv.id)) as f:
            pos = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if time.time() - pos["ts"] > POSITION_STALE_SECONDS:
        log.warning("%s Position périmée (%.0fs) — partage-t-il bien sa position en direct ?",
                    drv.tag, time.time() - pos["ts"])
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
    r"ce\s+(?:soir|matin|midi)|cet\s+apr[èe]s[\s-]?midi|"
    r"aujourd['’]?hui|demain|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|"
    r"janvier|février|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|décembre|"
    r"van|break|mono|[ée]co|c(?=[0-9a-z]*\d)[0-9a-z]{5,})\b"  # dernier = codes course type C2JR1085403 (exige un chiffre : n'efface pas Courbevoie, Clichy…)
    # tokens numériques / horaires / symboles (pas de frontière de mot)
    r"|\d{1,2}\s*[h:]\s*\d{0,2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{1,2}(?!\d)"
    r"|[\s❌✅🟢🔴🔥.,:;\-–—]"
    r")+", re.I)

# métadonnées (pax, bagages, distance) — jamais un lieu de départ
META_RE = re.compile(
    r"\b(personnes?|passagers?|pax|km|bagages?|valises?|r[ée]hausseurs?|si[èe]ges?|b[ée]b[ée]s?|enfants?)\b",
    re.I)
# Séparateur départ/arrivée dans une même ligne : « X pour Y », « X vers Y ».
_TO_WORD = r"\b(?:pour|vers)\b"


def geo_variants(c: str) -> list[str]:
    """Formes à soumettre au géocodeur, de la plus précise à la plus simple.
    Nominatim ne trouve pas « 75016(Trocadéro », « métro Oberkampf » ni « 75004 (côté quai) » :
    on retire « métro », on aplatit les parenthèses et on garde le code postal seul en secours."""
    out: list[str] = []

    def add(x: str) -> None:
        x = " ".join(x.split()).strip(_SYMBOLS + ".,-–—")
        if x and x.lower() not in (o.lower() for o in out):
            out.append(x)

    s = re.sub(r"\b(?:m[ée]tro|station)\b\s*", "", c, flags=re.I)
    if "(" in s:
        add(re.sub(r"[()]", " ", s))
        add(s.split("(")[0])
    else:
        add(s)
    add(re.sub(r"^h[ôo]tel\s+", "", s, flags=re.I))   # « hôtel Aéroport d'Orly » -> « Aéroport d'Orly »
    add(c)
    return out


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

    # 2) motif "X pour Y" / "X vers Y" -> départ = X (dernier bout avant le mot)
    m = re.search(r"(.+?)" + _TO_WORD, text, re.I)
    if m:
        candidates.append(re.split(r"/|\n", m.group(1))[-1])

    # 3) positionnel : chaque segment (départ = avant un éventuel "pour")
    candidates += segs

    # nettoyage (statut de tête) + coupe "pour Y" + expansion raccourcis + filtre + dédup
    seen, out = set(), []
    for c in candidates:
        c = re.split(_TO_WORD, c, maxsplit=1, flags=re.I)[0]   # garde le départ, coupe l'arrivée
        c = expand_shortcuts(_clean(c))
        if c and _is_location(c):
            for g in geo_variants(c):                    # formes que Nominatim comprend
                if g.lower() not in seen:
                    seen.add(g.lower())
                    out.append(g)
    return out[:6]


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


def commission_for(text: str, price: "float | None",
                   default_rate: float = 0.0) -> "tuple[float, float | None]":
    """(commission €, taux) pour une course. Priorité : commission explicite du message,
    puis 'net' (= 0), puis `default_rate` (le taux réglé par le chauffeur, en fraction)."""
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
    if default_rate:
        return round(price * default_rate, 2), default_rate
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
    m = re.search(_TO_WORD + r"(.+)", text, re.I)     # "X pour/vers Y" -> arrivée = Y
    if m:
        right = re.split(r"/|\n", m.group(1))[0]
        # « 75004(côté quai) 30€ net » : on coupe au premier prix / km (sinon _is_location refuse)
        right = re.split(r"\d{1,3}(?:[.,]\d+)?\s*(?:€|euros?|eur\b|net\b|pab\b|km\b)", right,
                         maxsplit=1, flags=re.I)[0]
        right = expand_shortcuts(_clean(right))
        if _is_location(right):
            return right
    locs = []                                         # positionnel : 2e lieu distinct
    for s in segs:
        for part in re.split(_TO_WORD, s, flags=re.I):
            c = expand_shortcuts(_clean(part))
            if c and _is_location(c) and c.lower() not in [x.lower() for x in locs]:
                locs.append(c)
    return locs[1] if len(locs) > 1 else None


# ─────────────────────── Géocodage / ETA ──────────────────────
# Géocodage partagé par tous les chauffeurs : la même course tombe chez N chauffeurs au
# même instant. Un cache évite N appels identiques, et un verrou unique garde la cadence
# de Nominatim (~1 requête/s pour l'ensemble du service, pas par chauffeur).
_GEO_CACHE: dict[str, tuple[float, "tuple[float, float, str] | None"]] = {}
_GEO_TTL_HIT = 6 * 3600           # une adresse trouvée ne bouge pas
_GEO_TTL_MISS = 600               # un « introuvable » se retente au bout de 10 min
_GEO_MAX = 4000
_GEO_LOCK = asyncio.Lock()
_geo_last = 0.0


def _geo_cached(key: str):
    hit = _GEO_CACHE.get(key)
    if hit is None:
        return False, None
    ts, val = hit
    if time.time() - ts > (_GEO_TTL_HIT if val else _GEO_TTL_MISS):
        _GEO_CACHE.pop(key, None)
        return False, None
    return True, val


async def geocode(session: aiohttp.ClientSession, query: str) -> tuple[float, float, str] | None:
    global _geo_last
    key = " ".join(query.lower().split())
    found, val = _geo_cached(key)
    if found:
        return val
    async with _GEO_LOCK:
        found, val = _geo_cached(key)              # un autre chauffeur l'a peut-être résolu entre-temps
        if found:
            return val
        wait = 1.1 - (time.monotonic() - _geo_last)
        if wait > 0:
            await asyncio.sleep(wait)
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
            return None                              # erreur réseau : on ne met rien en cache
        finally:
            _geo_last = time.monotonic()
        result = None
        if data:
            hit = data[0]
            result = float(hit["lat"]), float(hit["lon"]), hit.get("display_name", query)
        if len(_GEO_CACHE) >= _GEO_MAX:
            _GEO_CACHE.pop(next(iter(_GEO_CACHE)), None)
        _GEO_CACHE[key] = (time.time(), result)
        return result


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


def _good_mid(p: "D.Profile") -> "tuple[float, float]":
    """Seuils de couleur du chauffeur : vert dès « rentable », orange dès son €/km minimum."""
    mid = float(p.get("eurkm_min"))
    return max(float(p.get("eurkm_good")), mid), mid


def rentability_dot(eurkm, p: "D.Profile") -> str:
    """🟢 rentable · 🟠 moyen · 🔴 peu rentable · ⚪ inconnu (seuils propres au chauffeur)."""
    if eurkm is None:
        return "⚪"
    good, mid = _good_mid(p)
    if eurkm >= good:
        return "🟢"
    if eurkm >= mid:
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


def metrics_header(eurkm, price, dur_min, dep_time: str, p: "D.Profile") -> str:
    """Ligne compacte en tête : rentabilité (couleur) · prix · durée · heure."""
    parts = [rentability_dot(eurkm, p) + (f" {eurkm:.2f} €/km" if eurkm is not None else "")]
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


def rentability_line(eurkm, p: "D.Profile") -> str:
    """Ligne de décision mise en avant : pastille + €/km en gras + niveau."""
    dot = rentability_dot(eurkm, p)
    if eurkm is None:
        return f"{dot}  <i>rentabilité à confirmer</i>"
    good, mid = _good_mid(p)
    tier = "rentable" if eurkm >= good else ("correct" if eurkm >= mid else "peu rentable")
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
    if not g:
        return None
    rt = await route_eta(session, {"lat": from_coords[0], "lon": from_coords[1]}, (g[0], g[1]))
    if not rt:
        return None
    return rt[1] / 1000, rt[0] / 60


async def _expire_alert(drv: DriverRT, msg_id: int) -> None:
    """Passé le délai réglé par le chauffeur sans réponse, neutralise l'alerte."""
    minutes = drv.cfg("expire_min")
    await asyncio.sleep(minutes * 60)
    drv.alert_ctx.pop(msg_id, None)
    cid = drv.pending_alerts.pop(msg_id, None)
    if cid is None:                            # déjà répondue / passée
        return
    store.set_status(cid, "expirée", drv.id)
    try:
        await bot.edit_message(
            drv.id, msg_id,
            f"⏱️ <b>Course expirée</b>{MID}<i>sans réponse en {minutes} min</i>",
            buttons=None, parse_mode="html")
    except Exception:                          # noqa: BLE001
        pass


def time_options(eta_s: float, sp_under_min: "float | None" = None,
                 choices: "int | None" = None) -> "tuple[str, list[str]]":
    """Temps à annoncer pour rejoindre le départ : (meilleur, choix proposés).
    Le meilleur = le plus petit délai réellement tenable (ETA arrondie à la minute
    supérieure, « SP » sous `sp_under_min`) : annoncer moins = arriver en retard,
    annoncer plus = se faire doubler. Les autres choix = paliers de 5 min au-dessus.
    Les deux seuils sont des réglages du chauffeur ; sans argument : les défauts."""
    if sp_under_min is None:
        sp_under_min = D.SETTINGS_BY_KEY["sp_under_min"].default
    if choices is None:
        choices = D.SETTINGS_BY_KEY["time_choices"].default
    if eta_s < sp_under_min * 60:
        best = "SP"
        base = 0                               # SP, puis 5, 10…
    else:
        best = str(math.ceil(eta_s / 60))
        base = int(best)
    opts = [best]
    step = (base // 5 + 1) * 5                 # palier de 5 strictement au-dessus
    while len(opts) < max(1, choices):
        opts.append(str(step))
        step += 5
    return best, opts


def driver_time_options(drv: DriverRT, eta_s: float) -> "tuple[str, list[str]]":
    return time_options(eta_s, drv.cfg("sp_under_min"), drv.cfg("time_choices"))


def _decision_rows(gid: int, mid: int) -> list:
    """Boutons d'une alerte fraîche : Accepter / Refuser (+ lien vers la course)."""
    rows = [[Button.inline("✅ Accepter", f"a|{gid}|{mid}".encode()),
             Button.inline("❌ Refuser", b"x")]]
    link = course_link(gid, mid)
    if link:
        rows.append([Button.url("🔗 Voir la course", link)])
    return rows


async def deliver_alert(drv: DriverRT, dm_body: str, feed_body: str, reply: str, gid: int,
                        mid: int, course: "dict | None" = None, pickup_coords=None,
                        eta_s: "float | None" = None) -> None:
    """Bot configuré -> DM propre (HTML) vers CE chauffeur + boutons Accepter / Refuser.
    Sinon -> texte technique dans son relais (Hermès). Si `course` est fourni, la course est
    journalisée (statut « proposée »). `pickup_coords` / `eta_s` servent à recalculer le temps
    depuis sa position au moment d'accepter."""
    if bot:
        sent = await bot.send_message(drv.id, dm_body, buttons=_decision_rows(gid, mid),
                                      parse_mode="html", link_preview=False)
        if course is not None:
            cid = store.record_course(driver_id=drv.id, alert_id=sent.id, reply=reply, **course)
            drv.pending_alerts[sent.id] = cid
        drv.alert_ctx[sent.id] = dict(body=dm_body, reply=reply, pickup=pickup_coords, eta_s=eta_s)
        asyncio.create_task(_expire_alert(drv, sent.id))
    elif drv.p.feed_group and drv.client:
        body = feed_body + (f"\n{BOT_MENTION}" if BOT_MENTION else "")
        await drv.client.send_message(drv.p.feed_group, body)
    else:
        log.warning("%s Alerte perdue : ni bot ni relais configuré.", drv.tag)


async def _fresh_eta(drv: DriverRT, ctx: dict) -> "tuple[float, float | None, bool] | None":
    """ETA vers le départ depuis sa position ACTUELLE : (secondes, mètres, à_jour).
    Repli sur l'ETA calculée à l'arrivée de l'alerte si la position ou le routage manque."""
    src = load_position(drv)
    if ctx.get("pickup") and src is not None:
        async with aiohttp.ClientSession() as session:
            rt = await route_eta(session, src, ctx["pickup"])
        if rt:
            return rt[0], rt[1], True
    if ctx.get("eta_s") is not None:
        return ctx["eta_s"], None, False
    return None


async def escalate(drv: DriverRT, session, text_original: str, dep_text: str, pickup_coords,
                   eta_s: float, dist_m: float, gid: int, mid: int) -> None:
    prox = round(eta_s / 60)                        # temps pour rejoindre le départ
    # Règle de réponse VTC : < 5 min -> "SP" (sur place), sinon le délai en minutes
    reply = driver_time_options(drv, eta_s)[0]
    link = course_link(gid, mid)
    price = extract_price(text_original)
    arr_text = extract_dropoff(text_original)
    trip = await _geo_route(session, pickup_coords, arr_text)   # (km course, durée course)
    trip_km = trip[0] if trip else None
    dur_min = trip[1] if trip else None
    eurkm = (price / trip_km) if (price and trip_km) else None
    commission, comm_rate = commission_for(text_original, price, drv.p.commission_rate)
    net = (price - commission) if price is not None else None
    header = metrics_header(eurkm, price, dur_min, depart_time(text_original), drv.p)
    dm_body = (
        f"🚕 <b>Course proche</b>\n"
        f"{HR}\n"
        f"📍 <b>{html.escape(_short_loc(dep_text))}</b>\n"
        f"🏁 <b>{html.escape(_short_loc(arr_text))}</b>\n\n"
        f"🚗 <b>{prox} min</b> de toi{MID}{dist_m/1000:.1f} km\n"
        f"{rentability_line(eurkm, drv.p)}\n"
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
    await deliver_alert(drv, dm_body, feed_body, reply, gid, mid, course,
                        pickup_coords=pickup_coords, eta_s=eta_s)
    log.info("%s Course proche escaladée (à %s min | PEC=%r | brut=%r).",
             drv.tag, prox, dep_text, text_original[:70].replace("\n", " "))


# Telegram code un partage « sans fin » par une durée de 0x7FFFFFFF s (plus de 68 ans).
# Dès qu'une durée atteint ce seuil, il n'y a rien à relancer : pas de rappel d'expiration.
LIVE_INDEFINITE = 0x7FFFFFFF
LIVE_HOWTO = ("<i>📎 → Position → Partager ma position en direct → la durée la plus longue "
              "proposée (« jusqu'à ce que je l'arrête » si ton Telegram l'affiche, sinon 8 h).</i>")


def schedule_live_expiry(drv: DriverRT, period: int) -> None:
    """Programme un rappel LIVE_REMIND_BEFORE avant la fin d'un partage de position en direct.
    Un partage sans fin annule le rappel d'un partage précédent et n'en programme aucun."""
    if drv.pos_expiry_task and not drv.pos_expiry_task.done():
        drv.pos_expiry_task.cancel()
    drv.pos_expiry_task = None
    if period >= LIVE_INDEFINITE:
        return
    drv.pos_expiry_task = asyncio.create_task(
        _live_expiry_reminder(drv, max(0, period - LIVE_REMIND_BEFORE)))


async def _live_expiry_reminder(drv: DriverRT, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    if not bot:
        return
    try:
        await bot.send_message(
            drv.id,
            "⏰ <b>Ta position expire bientôt</b>\n"
            f"{HR}\n"
            "Relance le partage en direct pour rester couvert sans interruption :\n"
            + LIVE_HOWTO,
            parse_mode="html")
    except Exception:                       # noqa: BLE001
        pass


async def _reservation_reminder_loop() -> None:
    """Toutes les 10 min : pour chaque réservation dont l'heure est passée (+ RESA_PROMPT_AFTER),
    demande une seule fois « effectuée ? » afin de compter (ou non) le paiement."""
    await asyncio.sleep(30)
    while True:
        try:
            if bot:
                for r in store.due_reservations(time.time() - RESA_PROMPT_AFTER):
                    drv = DRIVERS.get(r["driver_id"])
                    if drv is None:                  # chauffeur déconnecté : on redemandera à son retour
                        continue
                    store.set_prompted(r["id"], drv.id)
                    rows = [[Button.inline("✅ Effectuée", f"eff|{r['id']}|1".encode()),
                             Button.inline("❌ Annulée", f"eff|{r['id']}|0".encode())]]
                    await bot.send_message(
                        drv.id,
                        f"📅 <b>Réservation — effectuée ?</b>\n{HR}\n{_recap_line(r)}\n\n"
                        f"<i>L'heure est passée. Valide pour compter le paiement.</i>",
                        buttons=rows, parse_mode="html")
        except Exception as e:                       # noqa: BLE001
            log.warning("Rappel réservation KO : %s", e)
        await asyncio.sleep(600)


async def _daily_summary_loop() -> None:
    """Envoie à chaque chauffeur, une fois par jour, vers DAILY_SUMMARY_HOUR, le récap de
    ses dernières 24 h (seulement s'il y a eu des courses réalisées ou des frais)."""
    if DAILY_SUMMARY_HOUR is None:
        return
    await asyncio.sleep(60)
    while True:
        now = _dt.datetime.now()
        today = now.date().isoformat()
        if bot and now.hour == DAILY_SUMMARY_HOUR:
            for drv in list(DRIVERS.values()):
                try:
                    if store.get_setting(drv.id, "last_daily") == today:
                        continue
                    store.set_setting(drv.id, "last_daily", today)    # une seule fois par jour
                    s = store.summary(drv.id, "24h")
                    if s["done"] or s.get("frais"):                  # pas de spam les jours sans activité
                        await bot.send_message(
                            drv.id,
                            "🌙 <b>Bonne fin de service — voici ta journée.</b>\n\n"
                            + build_report(drv, "24h"),
                            parse_mode="html", link_preview=False)
                except Exception as e:                       # noqa: BLE001
                    log.warning("%s Bilan auto KO : %s", drv.tag, e)
        await asyncio.sleep(600)


async def warn_position(drv: DriverRT) -> None:
    """Rappel throttlé (max 1 / 10 min par chauffeur) quand sa position n'est plus partagée."""
    now = time.time()
    if now - drv.last_pos_warn < 600 or not bot:
        return
    drv.last_pos_warn = now
    try:
        await bot.send_message(
            drv.id,
            "📍 <b>Position introuvable</b>\n"
            f"{HR}\n"
            "Je ne reçois plus ta position — impossible de repérer les courses proches.\n\n"
            "👉 Partage ta <b>position en direct</b> ici, dans ce chat :\n"
            + LIVE_HOWTO,
            parse_mode="html")
    except Exception:                       # noqa: BLE001
        pass


async def escalate_raw(drv: DriverRT, text_original: str, src: dict | None, reason: str,
                       gid: int, mid: int = 0) -> None:
    """Course non exploitable (non géocodée…) : on la LOGGE sans notifier (zéro bruit).
    Les alertes ne partent que pour les courses réellement jugées proches / rentables."""
    log.info("%s Course à confirmer (%s) | gid %s | brut=%r",
             drv.tag, reason, gid, text_original[:80].replace("\n", " "))


async def escalate_resa(drv: DriverRT, text_original: str, price: float, km: float,
                        eurkm: float, dur_min, gid: int, mid: int) -> None:
    """Réservation qui passe les réglages du chauffeur (prix, €/km, rayon)."""
    link = course_link(gid, mid)
    resa_reply = drv.cfg("resa_reply")
    deps = extract_pickup(text_original)
    depart = deps[0] if deps else "?"
    arrivee = extract_dropoff(text_original)
    commission, comm_rate = commission_for(text_original, price, drv.p.commission_rate)
    net = (price - commission) if price is not None else None
    header = metrics_header(eurkm, price, dur_min, depart_time(text_original), drv.p)
    dm_body = (
        f"📅 <b>Réservation</b>\n"
        f"{HR}\n"
        f"📍 <b>{html.escape(_short_loc(depart))}</b>\n"
        f"🏁 <b>{html.escape(_short_loc(arrivee))}</b>\n\n"
        f"{rentability_line(eurkm, drv.p)}\n"
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
        f"💬 Réponse suggérée : {resa_reply}\n"
        + (f"🔗 {link}\n" if link else "")
        + f"[GID {gid}]\n──────────\n{text_original}\n──────────\n"
        f"→ Hermès : réservation rentable, propose « {resa_reply} »."
    )
    await deliver_alert(drv, dm_body, feed_body, resa_reply, gid, mid, course)
    log.info("%s Réservation escaladée (%.2f €/km, gid %s).", drv.tag, eurkm, gid)


# Trajet d'une réservation (départ -> arrivée) : identique pour tous les chauffeurs, donc
# calculé une fois. Évite N appels de routage (quota HERE) pour la même réservation.
_RESA_ROUTE_CACHE: dict[tuple, tuple[float, "tuple[float, float] | None"]] = {}
_RESA_ROUTE_TTL = 3600


async def _resa_route(session, g1, g2, when_hour):
    key = (round(g1[0], 4), round(g1[1], 4), round(g2[0], 4), round(g2[1], 4), when_hour)
    hit = _RESA_ROUTE_CACHE.get(key)
    if hit and time.time() - hit[0] < _RESA_ROUTE_TTL:
        return hit[1]
    rt = await route_eta(session, {"lat": g1[0], "lon": g1[1]}, (g2[0], g2[1]), when_hour=when_hour)
    if rt:                                           # un échec réseau ne se met pas en cache
        if len(_RESA_ROUTE_CACHE) > 2000:
            _RESA_ROUTE_CACHE.clear()
        _RESA_ROUTE_CACHE[key] = (time.time(), rt)
    return rt


async def _geocode_any(session, cands: "list[str]") -> "tuple[float, float, str] | None":
    """Premier candidat que le géocodeur sait placer (même logique que les courses immédiates)."""
    for c in cands:
        g = await geocode(session, c)
        if g:
            return g
    return None


async def _resa_pickup_km(drv: DriverRT, session, deps: "list[str]") -> "float | None":
    """Distance à vol d'oiseau entre la dernière position du chauffeur et le départ de la
    réservation. None si on ne sait pas (position périmée, départ introuvable)."""
    src = load_position(drv)
    if src is None or not deps:
        return None
    g = await _geocode_any(session, deps)
    if not g:
        return None
    return D.haversine_km(src["lat"], src["lon"], g[0], g[1])


async def handle_reservation(drv: DriverRT, text: str, gid: int, mid: int) -> None:
    """Réservation : le tarif au km décide, borné par les réglages du chauffeur (prix
    minimum, €/km minimum, rayon autour de sa position)."""
    price = extract_price(text)
    if price is None:
        return                                       # pas de prix lisible -> on ne peut pas juger
    ok, why = D.accept_resa_price(drv.p, price=price)
    if not ok:
        log.info("%s Réservation ignorée : %s.", drv.tag, why)
        return
    deps = extract_pickup(text)
    dep = deps[0] if deps else None
    async with aiohttp.ClientSession() as s:
        # Rayon (si le chauffeur en a réglé un) : filtre bon marché, avant tout routage.
        if drv.cfg("resa_radius_km"):
            ok, why = D.accept_resa_radius(drv.p, pickup_km=await _resa_pickup_km(drv, s, deps))
            if not ok:
                log.info("%s Réservation ignorée : %s.", drv.tag, why)
                return
        km = extract_km(text)                        # km explicite dans le message ?
        dur_min = None
        if km is None:                               # sinon on géocode dep->arr et on route
            arr = extract_dropoff(text)
            if not dep or not arr:
                await escalate_raw(drv, text, load_position(drv), "réservation : trajet incomplet", gid, mid)
                return
            g1 = await _geocode_any(s, deps)         # tous les candidats, pas seulement le 1er
            g2 = await _geocode_any(s, geo_variants(arr))
            if not g1 or not g2:
                await escalate_raw(drv, text, load_position(drv), "réservation : géocodage trajet KO", gid, mid)
                return
            # trafic estimé à l'HEURE PRÉVUE de la réservation (pas maintenant)
            rt = await _resa_route(s, g1, g2, depart_hour(text))
            if not rt:
                return
            km = rt[1] / 1000
            dur_min = rt[0] / 60
    if km < 0.5:
        return
    eurkm = price / km
    ok, why = D.accept_resa_rate(drv.p, eurkm=eurkm)
    if ok:
        await escalate_resa(drv, text, price, km, eurkm, dur_min, gid, mid)
    else:
        log.info("%s Réservation ignorée : %s.", drv.tag, why)


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


def is_duplicate_course(drv: DriverRT, fp: "str | None", window: "float | None" = None) -> bool:
    """True si CE chauffeur a déjà vu cette empreinte dans la fenêtre (repost inter-groupes).
    La mémoire est par chauffeur : un repost qu'un chauffeur a filtré ne masque rien aux autres."""
    if not fp:
        return False
    if window is None:
        window = DEDUP_WINDOW_SECONDS
    now = time.time()
    horizon = max(DEDUP_WINDOW_SECONDS, DEDUP_TEXT_WINDOW_SECONDS)
    for k, ts in list(drv.dup_seen.items()):     # purge -> borne la mémoire
        if now - ts > horizon:
            del drv.dup_seen[k]
    already = fp in drv.dup_seen and now - drv.dup_seen[fp] <= window
    drv.dup_seen[fp] = now                         # (ré)arme la fenêtre glissante
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


def _upcoming_block(drv: DriverRT, s: dict) -> list:
    """Bloc « À venir » : réservations validées, pas encore effectuées (hors CA réalisé)."""
    if not s.get("upcoming_n"):
        return []
    out = ["", f"📅 <b>À venir</b>{MID}{s['upcoming_n']} résa · {s['upcoming_ca']:.0f} € "
               f"<i>(pas encore compté)</i>"]
    for r in store.pending_reservations(drv.id, 6):
        dtm = reservation_datetime(r["raw"] or "")
        when = dtm.strftime("%d/%m %Hh%M") if dtm else "date ?"
        trip = f"{_short_loc(r['pickup'] or '?')} → {_short_loc(r['dropoff'] or '?')}"
        price = f"{r['price']:.0f} €" if r["price"] else "?"
        out.append(f"▸ {when} · {html.escape(trip)} · {price}")
    return out


def build_report(drv: DriverRT, period: str = "jour") -> str:
    s = store.summary(drv.id, period)
    c = s["counts"]
    lbl = _PERIOD_LABEL.get(period, period)
    obj_set = store.get_setting(drv.id, "objectif_jour")
    out = [f"📊 <b>Bilan · {lbl}</b>", HR]

    if s["done"] or s.get("frais") or (obj_set and period in ("jour", "24h")):
        if s.get("frais"):
            head = (f"🏁 <b>{s['benefice']:.0f} €</b> de bénéfice{MID}{s['net']:.0f} € net"
                    f"{MID}{s['ca']:.0f} € brut")
        else:
            head = f"💚 <b>{s['net']:.0f} €</b> net{MID}{s['ca']:.0f} € brut"
        out += [head, "", _receipt(s), "",
                f"✅ {s['done']} réalisées{MID}💬 {c['répondue']}{MID}📤 {c['proposée']}"]
        obj = store.get_setting(drv.id, "objectif_jour")
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

    out += _upcoming_block(drv, s)     # réservations à venir (toujours affichées)

    done = store.list_done(drv.id, period)
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


def build_table_html(drv: DriverRT, period: str) -> str:
    """Page HTML autonome : tableau de toutes les courses de la période + totaux."""
    s = store.summary(drv.id, period)
    rows = store.courses_detail(drv.id, period)
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


def build_table_pdf(drv: DriverRT, period: str) -> bytes:
    """PDF A4 designé : en-tête, cartes de totaux, tableau paginé de toutes les courses."""
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    s = store.summary(drv.id, period)
    rows = store.courses_detail(drv.id, period)
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


async def send_courses_table(drv: DriverRT, period: str) -> None:
    """Envoie le tableau détaillé (PDF designé) dans le DM du bot du chauffeur."""
    if not bot:
        return
    lbl = _PERIOD_LABEL.get(period, period)
    try:
        data = build_table_pdf(drv, period)
        name = f"releve_{period}.pdf"
    except Exception as e:                       # noqa: BLE001
        log.warning("%s PDF KO, repli HTML : %s", drv.tag, e)
        data = build_table_html(drv, period).encode("utf-8")
        name = f"courses_{period}.html"
    bio = io.BytesIO(data)
    bio.name = name
    await bot.send_file(
        drv.id, bio, force_document=True,
        attributes=[DocumentAttributeFilename(name)],
        caption=(f"📄 <b>Relevé · {lbl}</b>\n"
                 f"<i>Toutes tes courses avec les totaux — prêt à imprimer / envoyer au comptable.</i>"),
        parse_mode="html")


# ─────────────────────────── Handlers ─────────────────────────
# Handlers de la SESSION d'un chauffeur (son compte Telegram). Branchés par attach_driver().
async def on_feed(drv: DriverRT, event) -> None:
    """Relais Hermès (mode historique, optionnel) : position live + canal retour [SEND ...]."""
    # 1) position en direct : les mises à jour arrivent en éditions de message
    if event.message.geo is not None:
        save_position(drv, event.message.geo.lat, event.message.geo.long)
        return

    # 2) canal retour : Hermès approuve -> "[SEND -1001234567890] texte de la réponse"
    m = re.match(r"^\[SEND (-?\d+)\]\s*(.+)", event.raw_text or "", re.DOTALL)
    if m:
        gid, reply = int(m.group(1)), m.group(2).strip()
        if gid not in drv.p.groups:
            log.warning("%s SEND refusé : %s hors de ses groupes.", drv.tag, gid)
            return
        await drv.client.send_message(gid, reply)
        log.info("%s Réponse envoyée dans %s.", drv.tag, gid)


async def on_self_note(drv: DriverRT, event) -> None:
    """/note ou /courses depuis SES Messages sauvegardés (privé à lui seul).
    Dans la conversation de quelqu'un d'autre : on NE traite PAS (l'autre le verrait) — on
    supprime et on le prévient, car un compte ne peut pas envoyer un message invisible."""
    if not event.is_private or not bot:
        return
    if BOT_SELF_ID and event.chat_id == BOT_SELF_ID:   # DM du bot -> géré par on_command
        return
    txt = (event.raw_text or "").strip()
    low = txt.lower()
    if not low.startswith(("/note", "/courses", "/course")):
        return

    # Messages sauvegardés (conversation avec soi-même) = sûr, personne d'autre ne voit
    if drv.self_id and event.chat_id == drv.self_id:
        try:
            await event.delete()
        except Exception:                   # noqa: BLE001
            pass
        note = txt[5:].lstrip(" :").strip() if low.startswith("/note") else ""
        if low.startswith("/note") and note:
            await send_note_picker(drv, note)
        else:
            await send_courses_dashboard(drv)
        log.info("%s Commande via Messages sauvegardés -> UI envoyée dans le DM du bot.", drv.tag)
        return

    # Conversation avec quelqu'un d'autre : il a PU la voir (notification). Supprimer + prévenir.
    try:
        await event.delete()
    except Exception:                       # noqa: BLE001
        pass
    try:
        await bot.send_message(
            drv.id,
            "⚠️ <b>Attention</b> — tu as tapé une commande dans une conversation avec quelqu'un.\n"
            "Il a <b>pu la voir</b> (notification), même si je l'ai supprimée.\n"
            "👉 Utilise le <b>DM du bot</b> ou tes <b>Messages sauvegardés</b> pour rester privé.",
            parse_mode="html")
    except Exception:                       # noqa: BLE001
        pass
    log.warning("%s Commande tapée dans une conversation tierce -> supprimée + alerte.", drv.tag)


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


async def send_note_picker(drv: DriverRT, note_text: str) -> None:
    """Envoie dans le DM du bot la liste des courses récentes : tape celle à annoter."""
    if not bot:
        return
    rs = store.recent(drv.id, 8)
    if not rs:
        await bot.send_message(drv.id, "Aucune course à annoter pour l'instant.")
        return
    rows = [[Button.inline(_course_btn_label(r), f"n|{r['id']}".encode())] for r in rs]
    rows.append([Button.inline("✖️ Annuler", b"nc")])
    sent = await bot.send_message(
        drv.id,
        f"📝 <b>Nouvelle note</b>\n« {html.escape(note_text)} »\n\n<i>Sur quelle course ?</i>",
        buttons=rows, parse_mode="html")
    drv.pending_note[sent.id] = note_text


async def send_courses_dashboard(drv: DriverRT) -> None:
    """Tableau des courses récentes (par statut) — touche une course pour agir dessus."""
    if not bot:
        return
    rs = store.recent(drv.id, 8)
    if not rs:
        await bot.send_message(drv.id, "Aucune course enregistrée pour l'instant.")
        return
    rows = [[Button.inline(_course_btn_label(r), f"c|{r['id']}".encode())] for r in rs]
    await bot.send_message(
        drv.id,
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


async def apply_edit(drv: DriverRT, event, cid: int, field: str, txt: str) -> None:
    """Applique une édition (note/prix/comm) à la course #cid du chauffeur et confirme."""
    r = store.get(cid, drv.id)
    if not r:
        await event.respond("Course introuvable.")
        return
    trip = f"{_short_loc(r['pickup'] or '?')} → {_short_loc(r['dropoff'] or '?')}"
    if field == "note":
        nr = store.append_note(cid, txt, drv.id)
        await event.respond(f"📝 <b>Note</b> · {html.escape(trip)}\n« {html.escape(nr['note'])} »",
                            parse_mode="html")
        return
    m = re.search(r"(\d+(?:[.,]\d+)?)", txt)
    if not m:
        await event.respond("Écris un nombre (ex. 45).")
        return
    val = float(m.group(1).replace(",", "."))
    if field == "prix":
        nr = store.adjust_amount(cid, price=val, rate=(drv.p.commission_rate or None),
                                 driver_id=drv.id)
        extra = f" · net {nr['net']:.0f} €" if nr["commission"] else ""
        await event.respond(f"✏️ <b>Prix corrigé</b> · {html.escape(trip)}\n💶 {val:.0f} €{extra}",
                            parse_mode="html")
    else:  # comm
        nr = store.adjust_amount(cid, commission=val, driver_id=drv.id)
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


async def add_reservation_to_calendar(drv: DriverRT, cid: int) -> bool:
    """Ajoute une réservation (par id) à l'agenda, avec date/heure lues du texte.
    L'agenda est celui de la machine qui héberge le pont : réservé au chauffeur propriétaire.
    Les autres reçoivent le fichier .ics (bouton « Fichier agenda »)."""
    if not drv.p.is_owner:
        return False
    r = store.get(cid, drv.id)
    if not r:
        return False
    dt = reservation_datetime(r["raw"] or "")
    if dt is None:
        return False
    trip = f"{_short_loc(r['pickup'] or '?')} → {_short_loc(r['dropoff'] or '?')}"
    price = f"{r['price']:.0f} €" if r["price"] else ""
    desc = trip + (f" · {price}" if price else "")
    return await add_event_to_calendar(f"🚕 VTC · {trip}", dt, desc, r["pickup"] or "")


async def send_reservation_ics(drv: DriverRT, cid: int) -> None:
    """Envoie un fichier .ics (agenda) pour une réservation, avec rappels 24 h et 1 h avant."""
    if not bot:
        return
    r = store.get(cid, drv.id)
    if not r:
        return
    trip = f"{_short_loc(r['pickup'] or '?')} → {_short_loc(r['dropoff'] or '?')}"
    dt = reservation_datetime(r["raw"] or "")
    if dt is None:
        await bot.send_message(
            drv.id,
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
        drv.id, bio, force_document=True,
        attributes=[DocumentAttributeFilename("reservation.ics")],
        caption=(f"📅 <b>{dt.strftime('%d/%m à %Hh%M')}</b> · {html.escape(trip)}\n"
                 f"<i>Ouvre le fichier pour l'ajouter à ton agenda — rappels 24 h et 1 h avant.</i>"),
        parse_mode="html")


# ───────────────────── Réglages du chauffeur (/reglages) ─────────────────────
# Chaque chauffeur règle ses propres seuils depuis le DM du bot : prix minimum, distance
# jusqu'au départ, temps, €/km, rayon des réservations… Définitions dans drivers.SETTINGS.
SETTING_ALIASES = {
    "/prixmin": "min_price", "/prix_min": "min_price",
    "/distancemax": "max_pickup_km", "/distance": "max_pickup_km", "/kmmax": "max_pickup_km",
    "/tempsmax": "threshold_min", "/temps": "threshold_min",
    "/eurkm": "eurkm_min", "/rayon": "resa_radius_km",
}


def rules_summary(drv: DriverRT) -> str:
    """Ce que le chauffeur reçoit, en une phrase par type de course (lecture rapide)."""
    g, n = drv.cfg, D.fmt_num
    imm = f"≤ {n(g('threshold_min'))} min"
    if g("max_pickup_km"):
        imm += f", ≤ {n(g('max_pickup_km'))} km"
    resa = f"≥ {n(g('eurkm_min'))} €/km"
    if g("resa_radius_km"):
        resa += f", ≤ {n(g('resa_radius_km'))} km de toi"
    price = f", ≥ {n(g('min_price'))} €" if g("min_price") else ""
    return (f"🚕 <b>Immédiates</b> : {imm}{price}\n"
            f"📅 <b>Réservations</b> : {resa}{price}")


def settings_home(drv: DriverRT, advanced: bool = False) -> "tuple[str, list]":
    specs = [s for s in D.SETTINGS if s.advanced == advanced]
    vals = [(s, D.fmt_value(s, drv.cfg(s.key))) for s in specs]
    lines = ["⚙️ <b>Réglages" + (" avancés" if advanced else "") + "</b>", HR]
    if not advanced:
        lines += [rules_summary(drv), ""]
    lines += [f"{html.escape(s.label)}{MID}<b>{html.escape(v)}</b>" for s, v in vals]
    lines += ["", "<i>Touche un réglage pour le modifier — effet immédiat, pour toi seul.</i>"]
    btns = [[Button.inline(f"{s.label} · {v}"[:60], f"st|{s.key}".encode())] for s, v in vals]
    btns.append([Button.inline("⬅️ Réglages", b"st|home")] if advanced
                else [Button.inline("🔧 Avancé", b"st|adv")])
    return "\n".join(lines), btns


def settings_detail(drv: DriverRT, key: str, note: str = "") -> "tuple[str, list]":
    s = D.SETTINGS_BY_KEY[key]
    cur = D.fmt_value(s, drv.cfg(key))
    default = D.fmt_value(s, s.default)
    lines = [f"⚙️ <b>{html.escape(s.label)}</b>", HR,
             f"Actuel{MID}<b>{html.escape(cur)}</b>{MID}<i>défaut : {html.escape(default)}</i>"]
    if s.help:
        lines += ["", f"<i>{html.escape(s.help)}</i>"]
    if note:
        lines += ["", note]
    unit = f" {s.unit}" if s.unit else ""
    btns = []
    if s.kind in ("int", "float"):
        st = D.fmt_num(s.step)
        btns.append([Button.inline(f"➖ {st}{unit}", f"st|{key}|-".encode()),
                     Button.inline(f"➕ {st}{unit}", f"st|{key}|+".encode())])
    if s.kind == "bool":
        btns.append([Button.inline("🔄 Basculer", f"st|{key}|+".encode())])
    else:
        btns.append([Button.inline("✍️ Saisir", f"st|{key}|in".encode())])
    btns.append([Button.inline("↩︎ Valeur par défaut", f"st|{key}|rst".encode())])
    btns.append([Button.inline("⬅️ Retour", b"st|adv" if s.advanced else b"st|home")])
    return "\n".join(lines), btns


def _persist_settings(drv: DriverRT) -> None:
    store.save_driver_settings(drv.id, drv.p.settings)


async def send_settings_home(drv: DriverRT, event) -> None:
    text, btns = settings_home(drv)
    await event.respond(text, buttons=btns, parse_mode="html")


async def apply_setting_input(drv: DriverRT, event, key: str, txt: str) -> None:
    """Valeur saisie à la main pour un réglage : validée, sinon on redemande (borne affichée)."""
    s = D.SETTINGS_BY_KEY.get(key)
    if s is None:
        return
    try:
        val = drv.p.set(key, txt)
    except ValueError as e:
        drv.awaiting = (0, f"set:{key}")                 # il peut réessayer sans rouvrir l'écran
        await event.respond(f"❌ Valeur refusée : {html.escape(str(e))}. Réessaie, ou /reglages pour annuler.",
                            parse_mode="html")
        return
    _persist_settings(drv)
    log.info("%s Réglage %s = %r", drv.tag, key, val)
    text, btns = settings_detail(drv, key, note="✅ <b>Enregistré</b>")
    await event.respond(text, buttons=btns, parse_mode="html")


async def handle_settings_callback(drv: DriverRT, event, data: str) -> None:
    """Callbacks st|home, st|adv, st|<clé>, st|<clé>|+|-|in|rst."""
    parts = data.split("|")
    try:
        if parts[1] in ("home", "adv"):
            text, btns = settings_home(drv, advanced=(parts[1] == "adv"))
        else:
            key = parts[1]
            if key not in D.SETTINGS_BY_KEY:
                await event.answer("Réglage inconnu", alert=True)
                return
            s = D.SETTINGS_BY_KEY[key]
            op = parts[2] if len(parts) > 2 else ""
            note = ""
            if op in ("+", "-"):
                before = drv.cfg(key)
                drv.p.set(key, D.step_value(s, before, +1 if op == "+" else -1))
                _persist_settings(drv)
                if drv.cfg(key) == before:
                    await event.answer("Limite atteinte")
            elif op == "rst":
                drv.p.reset(key)
                _persist_settings(drv)
                note = "↩︎ <i>Valeur par défaut rétablie.</i>"
            elif op == "in":
                drv.awaiting = (0, f"set:{key}")
                hint = "oui ou non" if s.kind == "bool" else ("le texte" if s.kind == "text"
                                                              else f"le nombre{' en ' + s.unit if s.unit else ''}")
                await event.answer("✍️ Écris la valeur en message")
                await bot.send_message(drv.id, f"✍️ Écris {hint} pour <b>{html.escape(s.label)}</b>.",
                                       parse_mode="html")
                return
            text, btns = settings_detail(drv, key, note)
        await event.edit(text, buttons=btns, parse_mode="html")
    except Exception as e:                               # noqa: BLE001  (message identique, valeur invalide…)
        log.debug("%s Réglages : %s", drv.tag, e)
        try:
            await event.answer()
        except Exception:                                # noqa: BLE001
            pass


if bot is not None:
    @bot.on(events.CallbackQuery)
    async def on_button(event):
        """Tap sur un bouton d'alerte -> poste la réponse, puis suit le résultat (obtenue/ratée).
        Le chauffeur est identifié par SON id Telegram : tout (course, alerte, groupe, session)
        est résolu à partir de lui, jamais d'un état global."""
        drv = DRIVERS.get(event.sender_id)
        if drv is None:
            await event.answer("Non autorisé", alert=True)
            return
        data = event.data.decode("utf-8", "ignore")

        # ── ⚙️ Réglages (prix minimum, distance, …) ──
        if data.startswith("st|"):
            await handle_settings_callback(drv, event, data)
            return

        # ── ❌ Refuser : la course n'est pas prise ──
        if data == "x":
            drv.alert_ctx.pop(event.message_id, None)
            cid = drv.pending_alerts.pop(event.message_id, None)
            if cid:
                store.set_status(cid, "passée", drv.id)
            await event.edit("❌ <b>Course refusée</b>", parse_mode="html")
            return

        # ── ✅ Accepter : propose les temps calculés depuis ta position, le meilleur en tête ──
        # ── ⬅️ Retour : revient au choix Accepter / Refuser ──
        if data.startswith(("a|", "b|")):
            try:
                _, sgid, smid = data.split("|", 2)
                gid, mid = int(sgid), int(smid)
            except ValueError:
                await event.answer("Donnée invalide", alert=True)
                return
            ctx = drv.alert_ctx.get(event.message_id)
            if ctx is None or event.message_id not in drv.pending_alerts:
                await event.answer("⏱️ Alerte expirée — rien envoyé.", alert=True)
                return
            if data.startswith("b|"):
                await event.edit(ctx["body"], buttons=_decision_rows(gid, mid),
                                 parse_mode="html", link_preview=False)
                return
            back = Button.inline("⬅️ Retour", f"b|{gid}|{mid}".encode())
            refuse = Button.inline("❌ Refuser", b"x")
            if ctx.get("eta_s") is None:
                # Réservation : pas de délai à annoncer, la réponse est la confirmation.
                rows = [[Button.inline(f"⭐ {ctx['reply']}", f"s|{gid}|{mid}|{ctx['reply']}".encode())],
                        [back, refuse]]
                await event.edit(
                    f"{ctx['body']}\n\n{HR}\n✅ <b>Réponse à envoyer</b>{MID}⭐ « {html.escape(ctx['reply'])} »",
                    buttons=rows, parse_mode="html", link_preview=False)
                return
            await event.answer("🧭 Calcul depuis ta position…")
            fresh = await _fresh_eta(drv, ctx)
            eta_now, dist_now, live = fresh
            best, opts = driver_time_options(drv, eta_now)

            def label(o: str) -> str:
                return "SP (sur place)" if o == "SP" else f"{o} min"

            btns = [Button.inline(("⭐ " if o == best else "") + label(o), f"s|{gid}|{mid}|{o}".encode())
                    for o in opts]
            rows = [btns[:1], btns[1:]] if len(btns) > 1 else [btns]
            rows.append([back, refuse])
            where = (f"🧭 Depuis ta position : <b>{round(eta_now / 60)} min</b>"
                     + (f"{MID}{dist_now / 1000:.1f} km" if dist_now else "")
                     if live else
                     f"🧭 <i>Position indisponible — temps calculé à l'arrivée de l'alerte</i> : "
                     f"<b>{round(eta_now / 60)} min</b>")
            await event.edit(
                f"{ctx['body']}\n\n{HR}\n{where}\n"
                f"⭐ <b>Meilleur temps : {label(best)}</b>\n"
                f"<i>Arrondi à la minute au-dessus : annoncer moins = retard, plus = tu te fais doubler.</i>",
                buttons=rows, parse_mode="html", link_preview=False)
            return

        # ── 📄 Tableau détaillé (fichier HTML) ──
        if data.startswith("tbl|"):
            await event.answer("📄 Génération du tableau…")
            await send_courses_table(drv, data.split("|", 1)[1] or "mois")
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
                r = store.get(cid, drv.id)
                if r is None:
                    await event.answer("Course introuvable", alert=True)
                    return
                if r["kind"] == "resa":
                    # Réservation : validée mais À VENIR -> ne compte pas encore dans le CA.
                    store.set_status(cid, "réservée", drv.id)
                    dtm = reservation_datetime(r["raw"] or "")
                    if dtm is not None:
                        store.set_sched(cid, dtm.timestamp(), drv.id)
                    cal_ok = await add_reservation_to_calendar(drv, cid)
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
                    store.set_status(cid, "obtenue", drv.id)
                    pay_row = [Button.inline(lbl, f"p|{cid}|{code}".encode())
                               for code, lbl in PAYMENTS.items()]
                    await event.edit(
                        f"✅ <b>Course obtenue</b>\n{HR}\n{_recap_line(r)}\n\n💳 <i>Mode de paiement ?</i>",
                        buttons=[pay_row], parse_mode="html")
            else:
                store.set_status(cid, "ratée", drv.id)
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
            r = store.get(cid, drv.id)
            if not r:
                await event.answer("Course introuvable", alert=True)
                return
            if ok == "1":
                store.set_status(cid, "effectuée", drv.id)
                pay_row = [Button.inline(lbl, f"p|{cid}|{code}".encode())
                           for code, lbl in PAYMENTS.items()]
                await event.edit(
                    f"✅ <b>Réservation effectuée</b>\n{HR}\n{_recap_line(r)}\n\n💳 <i>Mode de paiement ?</i>",
                    buttons=[pay_row], parse_mode="html")
            else:
                store.set_status(cid, "annulée", drv.id)
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
            await send_reservation_ics(drv, cid)
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
            store.set_payment(cid, pay, drv.id)
            r = store.get(cid, drv.id)
            if r is None:
                await event.answer("Course introuvable", alert=True)
                return
            title = "Réservation effectuée" if r["status"] == "effectuée" else "Course obtenue"
            await event.edit(
                f"✅ <b>{title}</b>\n{HR}\n{_recap_line(r)}\n\n"
                f"<i>📝 /note pour ajouter un détail</i>",
                parse_mode="html")
            return

        # ── 📝 Sélecteur « sur quelle course ? » : tap sur une course pour y lier la note ──
        if data == "nc":
            drv.pending_note.pop(event.message_id, None)
            await event.edit("✖️ Annulé")
            return
        if data.startswith("n|"):
            try:
                cid = int(data.split("|", 1)[1])
            except ValueError:
                await event.answer("Donnée invalide", alert=True)
                return
            note = drv.pending_note.pop(event.message_id, None)
            if not note:
                await event.answer("Note expirée — retape /note.", alert=True)
                return
            nr = store.append_note(cid, note, drv.id)
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
            r = store.get(cid, drv.id)
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
            drv.awaiting = (cid, field)
            r = store.get(cid, drv.id)
            trip = f"{_short_loc(r['pickup'] or '?')} → {_short_loc(r['dropoff'] or '?')}" if r else "?"
            ask = {"prix": "le nouveau <b>prix</b> (ex. 45)",
                   "comm": "la <b>commission</b> en € (ex. 8)",
                   "note": "la <b>note</b> (paiement, détail…)"}.get(field, "la valeur")
            await event.answer("✍️ Écris la valeur en message")
            await bot.send_message(drv.id, f"✍️ Écris {ask} pour <b>{html.escape(trip)}</b> <code>#{cid}</code>.",
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
        if gid not in drv.p.groups:
            await event.answer("Groupe non autorisé", alert=True)
            return
        drv.alert_ctx.pop(event.message_id, None)
        cid = drv.pending_alerts.pop(event.message_id, None)
        if cid is None:                                            # expirée ou déjà répondue
            await event.answer("⏱️ Alerte expirée — rien envoyé.", alert=True)
            return
        try:
            await drv.client.send_message(gid, reply, reply_to=mid)   # réponse citée à la course
        except Exception:                                          # course supprimée/expirée
            await drv.client.send_message(gid, reply)
        store.set_status(cid, "répondue", drv.id)
        store.set_reply(cid, reply, drv.id)
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

    _stranger_seen: dict[int, float] = {}          # id inconnu -> dernier refus (anti-spam)

    @bot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def on_bot_location_new(event):
        """Partage de position en direct DANS le DM du bot -> position de CE chauffeur."""
        drv = DRIVERS.get(event.sender_id)
        if drv is None or event.message.geo is None:
            return
        save_position(drv, event.message.geo.lat, event.message.geo.long)
        period = getattr(event.message.media, "period", None)   # durée du partage live (ex. 28800 = 8 h)
        if period:
            schedule_live_expiry(drv, period)
        # Un NOUVEAU message de position = un partage qui démarre (les mises à jour arrivent
        # en éditions). On confirme et on dit combien de temps il tient.
        if not period:
            body = ("📍 <b>Position reçue, mais ponctuelle</b>\n"
                    f"{HR}\n"
                    "Elle ne se met pas à jour. Pour suivre les courses proches, partage-la "
                    "<b>en direct</b> :\n" + LIVE_HOWTO)
        elif period >= LIVE_INDEFINITE:
            body = ("✅ <b>Position activée, sans limite de durée</b>\n"
                    f"{HR}\n"
                    "Je surveille les courses proches. 🚕 Rien à relancer : le partage tient "
                    "jusqu'à ce que tu l'arrêtes toi-même.")
        else:
            hours = period / 3600
            span = f"{hours:g} h" if hours >= 1 else f"{round(period / 60)} min"
            body = (f"✅ <b>Position activée pour {span}</b>\n"
                    f"{HR}\n"
                    f"Je surveille les courses proches. 🚕 Je te préviens "
                    f"{LIVE_REMIND_BEFORE // 60} min avant la fin.\n"
                    "Pour ne plus avoir à relancer, choisis la durée la plus longue "
                    "proposée par Telegram (« jusqu'à ce que je l'arrête » si elle apparaît).")
        await event.respond(body, parse_mode="html")

    @bot.on(events.MessageEdited(func=lambda e: e.is_private))
    async def on_bot_location_edit(event):
        """Mises à jour de la position en direct (arrivent en éditions de message)."""
        drv = DRIVERS.get(event.sender_id)
        if drv is not None and event.message.geo is not None:
            save_position(drv, event.message.geo.lat, event.message.geo.long)

    @bot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def on_command(event):
        """Commandes DM : /resume, /courses, /reglages, /note, /prix, /comm, /aide
        (+ saisie en attente : note, prix, commission ou valeur d'un réglage)."""
        txt = (event.raw_text or "").strip()
        low = txt.lower()
        if not txt:                          # message sans texte (position, média…) -> ignore
            return
        drv = DRIVERS.get(event.sender_id)
        if drv is None:
            # Inconnu : le bot est privé. On donne l'id à communiquer à l'admin (1 réponse / 10 min).
            now = time.time()
            if now - _stranger_seen.get(event.sender_id, 0) > 600:
                _stranger_seen[event.sender_id] = now
                await event.respond(
                    "🔒 <b>Bot privé.</b>\nTu n'es pas encore inscrit. Donne cet identifiant à "
                    f"l'administrateur : <code>{event.sender_id}</code>", parse_mode="html")
                log.info("Message d'un inconnu (id %s) ignoré.", event.sender_id)
            return
        # saisie en attente (bouton ✏️ Prix / 🧾 Commission / 📝 Note d'une fiche, ou un réglage)
        aw = drv.awaiting
        if aw is not None:
            if txt.startswith("/"):
                drv.awaiting = None                              # une commande annule la saisie
            else:
                cid, field = aw
                drv.awaiting = None
                if field.startswith("set:"):
                    await apply_setting_input(drv, event, field[4:], txt)
                else:
                    await apply_edit(drv, event, cid, field, txt)
                return
        # ── ⚙️ Réglages : /reglages, ou un raccourci (/prixmin 30, /distancemax 12…) ──
        cmd = low.split(None, 1)[0] if low.startswith("/") else ""
        cmd = cmd.split("@", 1)[0]
        if cmd in ("/reglages", "/réglages", "/reglage", "/settings", "/parametres", "/paramètres"):
            await send_settings_home(drv, event)
            return
        if cmd in SETTING_ALIASES:
            key = SETTING_ALIASES[cmd]
            arg = txt.split(None, 1)[1] if len(txt.split(None, 1)) > 1 else ""
            if arg:
                await apply_setting_input(drv, event, key, arg)
            else:
                text, btns = settings_detail(drv, key)
                await event.respond(text, buttons=btns, parse_mode="html")
            return
        if low.startswith(("/courses", "/course")):
            await send_courses_dashboard(drv)
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
            await event.respond(build_report(drv, period), buttons=btn,
                                parse_mode="html", link_preview=False)
        elif low.startswith(("/tableau", "/table", "/export")):
            period = "mois"
            if "semaine" in low:
                period = "semaine"
            elif "jour" in low or "auj" in low:
                period = "jour"
            elif "tout" in low or "total" in low:
                period = "tout"
            await send_courses_table(drv, period)
        elif low.startswith("/prix"):
            m = re.search(r"(\d+(?:[.,]\d+)?)", txt)
            r = store.last_realized_course(drv.id)
            if m and r:
                await apply_edit(drv, event, r["id"], "prix", txt)
                await event.respond("<i>Pas la bonne course ? → /courses pour choisir précisément.</i>",
                                    parse_mode="html")
            else:
                await event.respond("Usage : <b>/prix 55</b> (dernière course faite) — ou /courses pour choisir.",
                                    parse_mode="html")
        elif low.startswith("/comm"):
            m = re.search(r"(\d+(?:[.,]\d+)?)", txt)
            r = store.last_realized_course(drv.id)
            if m and r:
                await apply_edit(drv, event, r["id"], "comm", txt)
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
                store.add_expense(drv.id, amt, label)
                fr = store.expenses_total(drv.id, "jour")
                await event.respond(
                    f"⛽ <b>Frais ajouté</b> · {amt:.0f} € · {html.escape(label)}\n"
                    f"<i>Total frais du jour : {fr:.0f} € — /resume pour le bénéfice.</i>",
                    parse_mode="html")
        elif low.startswith(("/objectif", "/but")):
            m = re.search(r"(\d+(?:[.,]\d+)?)", txt)
            if not m:
                cur = store.get_setting(drv.id, "objectif_jour")
                await event.respond(
                    f"🎯 Objectif du jour actuel : <b>{cur or 'aucun'} €</b>.\n"
                    f"Usage : <b>/objectif 200</b>", parse_mode="html")
            else:
                obj = float(m.group(1).replace(",", "."))
                store.set_setting(drv.id, "objectif_jour", obj)
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
            target = store.find_by_alert(rid, drv.id) if rid else None
            if target is not None:
                nr = store.append_note(target["id"], note, drv.id)
                trip = f"{_short_loc(nr['pickup'] or '?')} → {_short_loc(nr['dropoff'] or '?')}"
                await event.respond(f"📝 Note ajoutée · {html.escape(trip)}\n« {html.escape(nr['note'])} »",
                                    parse_mode="html")
            else:
                await send_note_picker(drv, note)
        elif low.startswith(("/aide", "/help", "/start")):
            await event.respond(
                "🚕 <b>Assistant courses</b>\n"
                f"{HR}\n"
                f"⚙️ <b>/reglages</b>{MID}prix minimum, distance, temps, €/km, rayon\n"
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


# ─────────────────────── Courses : une décision par chauffeur ───────────────────────
async def on_course(drv: DriverRT, event) -> None:
    """Message d'un groupe de CE chauffeur. Chaque chauffeur a sa propre session : le même
    message arrive N fois (une par abonné du groupe) et chacun le juge avec SES réglages."""
    key = (event.chat_id, event.id)
    if key in drv.seen_courses:
        return
    drv.seen_courses.add(key)
    if len(drv.seen_courses) > 5000:                 # borne la mémoire d'un service permanent
        drv.seen_courses.clear()

    text = event.raw_text or ""
    if not is_course(text):
        return

    # un message peut contenir PLUSIEURS réservations -> une alerte par course
    segments = split_courses(text)
    if len(segments) > 1:
        log.info("%s Message multi-courses -> %d segments (gid %s).", drv.tag, len(segments), event.chat_id)
    for seg in segments:
        try:
            await _handle_course(drv, seg, event.chat_id, event.id)
        except Exception:                            # noqa: BLE001  (un chauffeur ne bloque jamais les autres)
            log.exception("%s Traitement de course KO (gid %s).", drv.tag, event.chat_id)


async def _handle_course(drv: DriverRT, text: str, gid: int, mid: int) -> None:
    """Traite UNE course pour UN chauffeur (immédiate ou réservation) : filtres, dédup,
    décision selon ses réglages, alerte."""
    if requires_van(text) and not drv.cfg("accept_van"):
        log.info("%s Course van ignorée.", drv.tag)
        return

    # même course repostée dans un autre groupe -> une seule alerte / une seule réponse
    fp = course_fingerprint(text)
    if fp is not None:
        if is_duplicate_course(drv, fp):
            log.info("%s Doublon inter-groupes ignoré (gid %s).", drv.tag, gid)
            return
    elif is_duplicate_course(drv, course_text_fingerprint(text), DEDUP_TEXT_WINDOW_SECONDS):
        # Pas de code postal exploitable : on retombe sur le texte, fenêtre courte.
        log.info("%s Repost identique ignoré (gid %s, fenêtre %d s).", drv.tag, gid, DEDUP_TEXT_WINDOW_SECONDS)
        return

    # Réservation -> logique rentabilité (€/km). Immédiat -> logique proximité (ETA).
    if is_reservation(text):
        await handle_reservation(drv, text, gid, mid)
        return

    # Prix plancher du chauffeur : refus avant tout appel réseau (géocodage, routage).
    price = extract_price(text)
    if price is not None:
        ok, why = D.accept_resa_price(drv.p, price=price)
        if not ok:
            log.info("%s Course ignorée : %s.", drv.tag, why)
            return

    src = load_position(drv)
    if src is None:
        await warn_position(drv)       # rappel unique (throttlé), pas un message par course
        return

    async with aiohttp.ClientSession() as session:
        pickup = None
        pickup_cand = None
        for cand in extract_pickup(text):
            geo = await geocode(session, cand)
            if geo:
                pickup = geo
                pickup_cand = cand            # texte propre du départ (pour l'affichage)
                break

        if pickup is None:
            await escalate_raw(drv, text, src, "adresse non géocodée", gid, mid)
            return

        lat, lon, name = pickup
        eta = await route_eta(session, src, (lat, lon))
        if eta is None:
            await escalate_raw(drv, text, src, "OSRM indisponible", gid, mid)
            return

        eta_s, dist_m = eta
        ok, why = D.accept_immediate(drv.p, price=price, eta_s=eta_s, dist_m=dist_m)
        if ok:
            await escalate(drv, session, text, pickup_cand, (lat, lon), eta_s, dist_m, gid, mid)
        else:
            log.info("%s Course ignorée : %s | PEC=%r (%s) | brut=%r",
                     drv.tag, why, pickup_cand, name, text[:80].replace("\n", " "))


async def _set_bot_menu() -> None:
    """Enregistre les commandes -> menu « / » dans la conversation du bot."""
    cmds = [
        ("reglages", "Tes réglages — prix minimum, distance, €/km, rayon"),
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


# ───────────────────── Cycle de vie des chauffeurs ─────────────────────
def attach_driver(drv: DriverRT) -> None:
    """Branche sur la session du chauffeur ses handlers. Les groupes sont lus à chaque
    message dans drv.p.groups : ajouter un groupe ne demande aucun redémarrage."""
    c = drv.client

    async def _course(event):
        if event.chat_id in drv.p.groups:
            await on_course(drv, event)

    async def _note(event):
        await on_self_note(drv, event)

    c.add_event_handler(_course, events.NewMessage(incoming=True))
    c.add_event_handler(_note, events.NewMessage(outgoing=True))
    if drv.p.feed_group:                              # relais Hermès (mode historique)
        feed = drv.p.feed_group

        async def _feed(event):
            if event.chat_id == feed:
                await on_feed(drv, event)

        c.add_event_handler(_feed, events.NewMessage())
        c.add_event_handler(_feed, events.MessageEdited())


async def _resolve_group_names(drv: DriverRT) -> None:
    """Nom lisible de chaque groupe du chauffeur (pour le résumé « par groupe »)."""
    for gid in drv.p.groups:
        if gid in GROUP_NAMES:
            continue
        try:
            ent = await drv.client.get_entity(gid)
            GROUP_NAMES[gid] = getattr(ent, "title", None) or str(gid)
        except Exception:                     # noqa: BLE001
            GROUP_NAMES[gid] = str(gid)


async def _run_client(drv: DriverRT) -> None:
    try:
        await drv.client.run_until_disconnected()
    except Exception as e:                    # noqa: BLE001
        log.warning("%s Session interrompue : %s", drv.tag, e)
    if DRIVERS.get(drv.id) is drv:            # coupure inattendue : la boucle de synchro relancera
        DRIVERS.pop(drv.id, None)
        log.warning("%s Déconnecté — nouvelle tentative au prochain cycle.", drv.tag)


async def _start_driver(profile: "D.Profile") -> "DriverRT | None":
    """Ouvre la session Telegram du chauffeur. Refuse une session qui n'est pas la sienne :
    sinon ses alertes partiraient vers le DM d'un autre."""
    drv = DriverRT(profile)
    interactive_owner = sys.stdin.isatty() and profile.is_owner     # première connexion d'origine
    if not os.path.exists(profile.session + ".session") and not interactive_owner:
        # Telethon créerait un fichier de session vide : on ne laisse pas de déchet, on le dit.
        log.error("[%s] Fichier de session absent (%s.session) — relance `python add_driver.py add`.",
                  profile.name or profile.id, profile.session)
        return None
    client = TelegramClient(profile.session, API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            if interactive_owner:
                await client.start()
            else:
                log.error("[%s] Session non autorisée (%s) — relance `python add_driver.py add`.",
                          profile.name or profile.id, profile.session)
                await client.disconnect()
                return None
        me = await client.get_me()
    except Exception as e:                    # noqa: BLE001
        log.error("[%s] Connexion impossible : %s", profile.name or profile.id, e)
        try:
            await client.disconnect()
        except Exception:                     # noqa: BLE001
            pass
        return None
    if me.id != profile.id:
        log.error("[%s] La session %s appartient au compte %s, pas à %s : chauffeur ignoré.",
                  profile.name or profile.id, profile.session, me.id, profile.id)
        await client.disconnect()
        return None
    drv.client, drv.self_id = client, me.id
    attach_driver(drv)
    await _resolve_group_names(drv)
    drv.runner = asyncio.create_task(_run_client(drv))
    DRIVERS[profile.id] = drv
    log.info("%s Connecté — %d groupe(s), seuil %s min.", drv.tag, len(profile.groups),
             profile.get("threshold_min"))
    if profile.is_owner and sys.platform == "darwin":
        await _calendar_selftest()
    return drv


async def _stop_driver(drv: DriverRT) -> None:
    DRIVERS.pop(drv.id, None)
    if drv.pos_expiry_task and not drv.pos_expiry_task.done():
        drv.pos_expiry_task.cancel()
    if drv.runner:
        drv.runner.cancel()
    try:
        await drv.client.disconnect()
    except Exception:                         # noqa: BLE001
        pass
    log.info("%s Arrêté.", drv.tag)


async def _sync_drivers_once() -> None:
    """Aligne les sessions ouvertes sur la table `drivers` : démarre les nouveaux chauffeurs,
    arrête les désactivés, rafraîchit groupes et réglages modifiés hors du bot (add_driver.py)."""
    rows = {r["id"]: r for r in store.list_driver_rows()}
    for did, row in rows.items():
        prof = D.profile_from_row(row)
        drv = DRIVERS.get(did)
        if drv is None and prof.active:
            await _start_driver(prof)
        elif drv is not None and not prof.active:
            await _stop_driver(drv)
        elif drv is not None:
            if prof.groups != drv.p.groups:
                log.info("%s Groupes mis à jour : %s", drv.tag, prof.groups)
                drv.p.groups = prof.groups
                await _resolve_group_names(drv)
            drv.p.settings = prof.settings
            drv.p.name = prof.name
    for did in [d for d in DRIVERS if d not in rows]:
        await _stop_driver(DRIVERS[did])


async def _driver_sync_loop() -> None:
    while True:
        try:
            await _sync_drivers_once()
        except Exception:                     # noqa: BLE001
            log.exception("Synchro des chauffeurs KO.")
        await asyncio.sleep(60)


def _bootstrap_legacy() -> None:
    """Premier lancement après le passage au multi-chauffeurs : le compte configuré dans
    .env (TG_SESSION, NOTIFY_USER_ID, VTC_GROUPS) devient le chauffeur propriétaire, et tout
    l'historique (courses, frais, objectif) lui est rattaché. Idempotent."""
    if not store.list_driver_rows():
        if not NOTIFY_USER_ID:
            log.warning("Aucun chauffeur en base et NOTIFY_USER_ID vide : ajoute-en un avec "
                        "`python add_driver.py add`.")
            return
        store.upsert_driver(NOTIFY_USER_ID, name="Propriétaire", session=SESSION,
                            groups=LEGACY_GROUPS, feed_group=LEGACY_FEED_GROUP, is_owner=True)
        log.info("Chauffeur d'origine créé depuis .env (id %s, %d groupes).",
                 NOTIFY_USER_ID, len(LEGACY_GROUPS))
    owner = next((r for r in store.list_driver_rows() if r["is_owner"]), None)
    if owner is None:
        return
    n = store.adopt_legacy_rows(owner["id"])
    if n:
        log.info("%d ligne(s) d'historique rattachées au chauffeur %s.", n, owner["id"])
    old, new = os.path.join(STATE_DIR, "position.json"), pos_path(owner["id"])
    if os.path.exists(old) and not os.path.exists(new):
        import shutil
        shutil.copy2(old, new)


async def _amain() -> None:
    store.init(COURSES_DB)            # journal des courses (persistant)
    _bootstrap_legacy()
    tasks = [_driver_sync_loop()]
    if bot is not None:
        await bot.start(bot_token=BOT_TOKEN)          # le bot d'abord : les alertes en ont besoin
        global BOT_SELF_ID
        BOT_SELF_ID = (await bot.get_me()).id
        await _set_bot_menu()
        tasks.append(bot.run_until_disconnected())
        tasks.append(_reservation_reminder_loop())
        tasks.append(_daily_summary_loop())
    await asyncio.gather(*tasks)


def main() -> None:
    log.info("Pont VTC multi-chauffeurs démarré | boutons : %s",
             "oui" if bot else "non (fallback Hermès)")
    asyncio.run(_amain())


if __name__ == "__main__":
    main()

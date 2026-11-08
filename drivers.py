#!/usr/bin/env python3
"""
Profils chauffeur : un compte Telegram = un chauffeur = ses groupes, sa position,
ses réglages et son journal de courses.

Ce module ne dépend ni de Telegram ni de la base : il porte la définition des réglages
modifiables, leur lecture/validation, et les règles qui décident si une course vaut une
alerte pour CE chauffeur. Tout est testable à sec (test_drivers.py).
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field


# ───────────────────────── Réglages modifiables ─────────────────────────
# Valeurs par défaut : celles du .env d'origine, pour qu'un chauffeur qui n'a rien
# réglé garde exactement l'ancien comportement.
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


_DEF_THRESHOLD_MIN = max(1, round(_env_float("THRESHOLD_SECONDS", 8 * 60) / 60))
_DEF_SP_UNDER_MIN = max(1, round(_env_float("SP_UNDER_SECONDS", 5 * 60) / 60))
_DEF_EXPIRE_MIN = max(1, round(_env_float("EXPIRE_SECONDS", 15 * 60) / 60))
_DEF_COMMISSION_PCT = round(_env_float("COMMISSION_RATE", 0.0) * 100, 2)


@dataclass(frozen=True)
class Setting:
    key: str
    label: str            # libellé court affiché sur le bouton / l'écran
    kind: str             # "int" | "float" | "text" | "bool"
    default: object
    lo: float = 0
    hi: float = 0
    step: float = 1
    unit: str = ""
    off_label: str = ""   # texte affiché quand la valeur vaut 0 (« sans limite », « aucun »…)
    help: str = ""
    advanced: bool = False


SETTINGS: tuple[Setting, ...] = (
    Setting("threshold_min", "Temps max jusqu'au départ", "int", _DEF_THRESHOLD_MIN,
            lo=1, hi=60, step=1, unit="min",
            help="Course immédiate : au-delà de ce temps de route depuis ta position, "
                 "tu ne reçois rien."),
    Setting("max_pickup_km", "Distance max jusqu'au départ", "float", 0,
            lo=0, hi=100, step=1, unit="km", off_label="sans limite",
            help="Course immédiate : distance de route entre toi et le départ. "
                 "0 = pas de limite (seul le temps compte)."),
    Setting("min_price", "Prix minimum", "float", 0,
            lo=0, hi=1000, step=5, unit="€", off_label="aucun",
            help="Courses immédiates et réservations sous ce prix : ignorées."),
    Setting("eurkm_min", "€/km minimum (réservation)", "float", _env_float("EURKM_MIN", 1.5),
            lo=0, hi=10, step=0.1, unit="€/km",
            help="Réservation : en dessous de ce tarif au kilomètre, rien n'est notifié."),
    Setting("resa_radius_km", "Rayon des réservations", "float", 0,
            lo=0, hi=200, step=5, unit="km", off_label="sans limite",
            help="Réservation : ignorée si son départ est à plus de X km à vol d'oiseau "
                 "de ta dernière position. 0 = toute la zone."),
    Setting("eurkm_good", "€/km « rentable » 🟢", "float", _env_float("EURKM_GOOD", 2.0),
            lo=0, hi=10, step=0.1, unit="€/km",
            help="Seuil de la pastille verte. En dessous du €/km minimum : rouge."),
    Setting("commission_pct", "Commission de l'apporteur", "float", _DEF_COMMISSION_PCT,
            lo=0, hi=60, step=1, unit="%", off_label="aucune",
            help="Part retenue quand le message ne précise rien. Sert au calcul du net."),
    Setting("resa_reply", "Réponse type (réservation)", "text",
            os.environ.get("RESA_REPLY", "ok").strip() or "ok",
            help="Texte envoyé dans le groupe quand tu acceptes une réservation."),
    Setting("accept_van", "Courses van", "bool", False,
            help="Activé : les courses van (7 places…) ne sont plus écartées."),
    Setting("sp_under_min", "« SP » sous", "int", _DEF_SP_UNDER_MIN,
            lo=1, hi=15, step=1, unit="min", advanced=True,
            help="Sous ce temps de route, la réponse proposée est « SP » (sur place)."),
    Setting("time_choices", "Nombre de temps proposés", "int",
            int(_env_float("TIME_CHOICES", 3)),
            lo=1, hi=5, step=1, advanced=True,
            help="Boutons proposés après « Accepter »."),
    Setting("expire_min", "Expiration d'une alerte", "int", _DEF_EXPIRE_MIN,
            lo=1, hi=120, step=1, unit="min", advanced=True,
            help="Sans réponse après ce délai, l'alerte se neutralise."),
)
SETTINGS_BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS}


def _num(x: float, kind: str):
    """Entier propre pour kind='int', flottant arrondi à 2 décimales sinon."""
    return int(round(x)) if kind == "int" else round(float(x), 2)


def fmt_num(x) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.2f}".rstrip("0").rstrip(".")


def coerce(setting: Setting, raw) -> object:
    """Valeur brute (JSON, saisie) -> valeur typée et bornée. Lève ValueError si illisible."""
    if setting.kind == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("1", "true", "oui", "on", "o", "yes", "y", "activé", "actif"):
            return True
        if s in ("0", "false", "non", "off", "n", "no", "désactivé", "inactif"):
            return False
        raise ValueError("réponds oui ou non")
    if setting.kind == "text":
        s = str(raw).strip()
        if not s:
            raise ValueError("texte vide")
        if len(s) > 40:
            raise ValueError("40 caractères maximum")
        return s
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        x = float(raw)
    else:
        m = re.search(r"-?\d+(?:\.\d+)?", str(raw).strip().replace(",", "."))
        if m is None:
            raise ValueError("écris un nombre")
        x = float(m.group(0))
    if math.isnan(x) or math.isinf(x):
        raise ValueError("nombre invalide")
    if x < setting.lo or x > setting.hi:
        raise ValueError(
            f"entre {fmt_num(setting.lo)} et {fmt_num(setting.hi)} {setting.unit}".strip())
    return _num(x, setting.kind)


def fmt_value(setting: Setting, value) -> str:
    """Valeur affichée au chauffeur (« 15 min », « sans limite », « activé »…)."""
    if setting.kind == "bool":
        return "activé" if value else "désactivé"
    if setting.kind == "text":
        return f"« {value} »"
    if setting.off_label and not value:
        return setting.off_label
    unit = f" {setting.unit}" if setting.unit else ""
    return f"{fmt_num(value)}{unit}"


def step_value(setting: Setting, current, direction: int):
    """Valeur après un tap − / +. Reste dans les bornes ; bool = bascule."""
    if setting.kind == "bool":
        return not bool(current)
    x = float(current) + direction * setting.step
    x = max(setting.lo, min(setting.hi, x))
    return _num(x, setting.kind)


# ───────────────────────────── Profil ─────────────────────────────
@dataclass
class Profile:
    id: int                                   # id Telegram du chauffeur (= chat du bot)
    name: str = ""
    session: str = ""                         # chemin de la session Telethon (sans .session)
    groups: list[int] = field(default_factory=list)
    feed_group: int = 0                       # relais Hermès (optionnel, mode historique)
    settings: dict = field(default_factory=dict)
    active: bool = True
    is_owner: bool = False                    # a le droit d'écrire dans l'agenda macOS du serveur

    def get(self, key: str):
        """Réglage effectif : valeur du chauffeur si valide, sinon défaut."""
        spec = SETTINGS_BY_KEY[key]
        if key in self.settings:
            try:
                return coerce(spec, self.settings[key])
            except ValueError:
                pass
        return spec.default

    def set(self, key: str, raw) -> object:
        """Valide et mémorise (en mémoire seulement : l'appelant persiste)."""
        val = coerce(SETTINGS_BY_KEY[key], raw)
        self.settings[key] = val
        return val

    def reset(self, key: str) -> None:
        self.settings.pop(key, None)

    @property
    def commission_rate(self) -> float:
        return float(self.get("commission_pct")) / 100


def profile_from_row(row) -> Profile:
    """Ligne de la table drivers -> Profil."""
    try:
        groups = [int(g) for g in json.loads(row["groups"] or "[]")]
    except (ValueError, TypeError):
        groups = []
    try:
        settings = json.loads(row["settings"] or "{}")
        if not isinstance(settings, dict):
            settings = {}
    except ValueError:
        settings = {}
    return Profile(id=row["id"], name=row["name"] or "", session=row["session"] or "",
                   groups=groups, feed_group=int(row["feed_group"] or 0),
                   settings=settings, active=bool(row["active"]),
                   is_owner=bool(row["is_owner"]))


# ───────────────────── Règles de décision (pures) ─────────────────────
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance à vol d'oiseau en km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def accept_immediate(p: Profile, *, price, eta_s: float, dist_m: float) -> tuple[bool, str]:
    """Une course immédiate vaut-elle une alerte pour ce chauffeur ? (ok, raison du refus).
    Prix illisible : on laisse passer — impossible de juger, et le chauffeur voit le texte."""
    limit_s = float(p.get("threshold_min")) * 60
    if eta_s > limit_s:
        return False, f"trop loin en temps ({eta_s / 60:.0f} min > {limit_s / 60:.0f} min)"
    max_km = float(p.get("max_pickup_km"))
    if max_km and dist_m / 1000 > max_km:
        return False, f"trop loin en distance ({dist_m / 1000:.1f} km > {max_km:g} km)"
    min_price = float(p.get("min_price"))
    if min_price and price is not None and price < min_price:
        return False, f"prix trop bas ({price:.0f} € < {min_price:g} €)"
    return True, ""


def accept_resa_price(p: Profile, *, price: float) -> tuple[bool, str]:
    """Premier filtre d'une réservation, avant tout appel réseau : le prix plancher."""
    min_price = float(p.get("min_price"))
    if min_price and price < min_price:
        return False, f"prix trop bas ({price:.0f} € < {min_price:g} €)"
    return True, ""


def accept_resa_radius(p: Profile, *, pickup_km: "float | None") -> tuple[bool, str]:
    """Rayon autour du chauffeur. pickup_km=None (position ou géocodage inconnus) : on
    laisse passer — une réservation reste valable des heures, mieux vaut un doute qu'un trou."""
    radius = float(p.get("resa_radius_km"))
    if radius and pickup_km is not None and pickup_km > radius:
        return False, f"hors rayon ({pickup_km:.0f} km > {radius:g} km)"
    return True, ""


def accept_resa_rate(p: Profile, *, eurkm: float) -> tuple[bool, str]:
    """Tarif au kilomètre d'une réservation."""
    floor = float(p.get("eurkm_min"))
    if eurkm < floor:
        return False, f"{eurkm:.2f} €/km < {floor:g}"
    return True, ""

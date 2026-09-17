#!/usr/bin/env python3
"""
Estime l'ETA voiture depuis ta dernière position live jusqu'à une adresse.

Usage :  python3 /Users/rrr/vtc-bridge/eta.py "75010 Paris"
         (accepte les raccourcis : "P10", "CDG", "Orly", "Gare de Lyon"…)

Sort un JSON sur stdout, ex. :
  {"address":"P10","resolved":"...","eta_min":4,"dist_km":1.8,"reply":"SP","decision":"alert"}
  {"error":"position périmée","age_s":420}

Ne dépend que de la bibliothèque standard (utilisable par Hermès sans venv).
Applique la même logique que le pont : < 5 min -> "SP", 5–15 min -> minutes, >= 15 min -> ignore.
"""
import os
import re
import sys
import json
import time
import urllib.parse
import urllib.request

POS_FILE  = os.environ.get("POS_FILE", "/Users/rrr/vtc-bridge/state/position.json")
OSRM      = os.environ.get("OSRM_URL", "https://router.project-osrm.org")
NOMINATIM = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org")
VIEWBOX   = os.environ.get("GEO_VIEWBOX", "1.44,49.24,3.56,48.12")
UA        = os.environ.get("USER_AGENT", "vtc-bridge/1.0 (contact: admin@tuumagency.com)")
THRESHOLD = int(os.environ.get("THRESHOLD_SECONDS", "900"))   # 15 min
SP_UNDER  = int(os.environ.get("SP_UNDER_SECONDS", "300"))    # 5 min
STALE     = int(os.environ.get("POSITION_STALE_SECONDS", "180"))
# HERE Routing API (trafic réel). Clé gratuite sur platform.here.com (250 000 req/mois).
# Sans clé : multiplicateur heure de pointe Paris appliqué sur la durée OSRM.
HERE_KEY = os.environ.get("HERE_KEY", "").strip()

import datetime as _dt

def _traffic_multiplier() -> float:
    """Facteur de ralentissement estimé selon l'heure Paris (fallback sans TomTom)."""
    now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=2)))
    h   = now.hour + now.minute / 60
    wd  = now.weekday()  # 0=lun … 6=dim
    if wd >= 5:           return 1.15   # week-end
    if 7.5  <= h < 9.5:  return 1.8    # pointe matin
    if 9.5  <= h < 11.0: return 1.4    # matin secondaire
    if 12.0 <= h < 14.0: return 1.3    # déjeuner
    if 17.0 <= h < 20.0: return 1.9    # pointe soir (pire)
    if 20.0 <= h < 22.0: return 1.3    # soirée
    return 1.1                          # nuit / hors pointe

LANDMARKS = [
    (r"\bcdg\b|\broissy\b", "Aéroport Charles de Gaulle, 95700 Roissy"),
    (r"\borly\b",           "Aéroport d'Orly, 94390 Orly"),
    (r"\bgdl\b|\bgare de lyon\b", "Gare de Lyon, 75012 Paris"),
    (r"\bmontparnasse\b",   "Gare Montparnasse, 75015 Paris"),
    (r"\bgare du nord\b",   "Gare du Nord, 75010 Paris"),
    (r"\bgare de l.?est\b", "Gare de l'Est, 75010 Paris"),
    (r"\baust?erlitz\b",    "Gare d'Austerlitz, 75013 Paris"),
    (r"\bstade de france\b", "Stade de France, 93200 Saint-Denis"),
    (r"\bdisney\b",         "Disneyland Paris, 77700 Marne-la-Vallée"),
    (r"\bvillepinte\b",     "Parc des Expositions, 93420 Villepinte"),
]


def _arr(m):
    n = int(m.group(1))
    return f"750{n:02d} Paris" if 1 <= n <= 20 else m.group(0)


def expand(s):
    s = re.sub(r"\bP\s?0?(\d{1,2})\b(?!\d)", _arr, s)
    s = re.sub(r"\bParis\s?0?(\d{1,2})\b(?!\d)", _arr, s, flags=re.I)
    for pat, rep in LANDMARKS:
        s = re.sub(pat, rep, s, flags=re.I)
    # Abréviations fréquentes dans les adresses françaises
    s = re.sub(r"\bDr\.?\b", "Docteur", s)
    s = re.sub(r"\bSt\.?\b", "Saint", s, flags=re.I)
    s = re.sub(r"\bAv\.?\b", "Avenue", s, flags=re.I)
    s = re.sub(r"\bBd\.?\b", "Boulevard", s, flags=re.I)
    s = re.sub(r"\bPl\.?\b", "Place", s, flags=re.I)
    return s.strip()


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def geocode(q):
    params = {"q": q, "format": "jsonv2", "limit": "1", "viewbox": VIEWBOX, "bounded": "1"}
    data = _get(f"{NOMINATIM}/search?" + urllib.parse.urlencode(params))
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"]), data[0].get("display_name", "")


def route(src, dst):
    """Retourne (durée_secondes_avec_trafic, distance_mètres).
    - Si HERE_KEY défini : HERE Routing API avec trafic temps réel.
    - Sinon : OSRM × multiplicateur heure de pointe Paris.
    """
    if HERE_KEY:
        return _route_here(src, dst)
    return _route_osrm_with_factor(src, dst)


def _route_here(src, dst):
    """HERE Routing API v8 — trafic temps réel (250 000 req/mois gratuites)."""
    url = (
        f"https://router.hereapi.com/v8/routes"
        f"?transportMode=car"
        f"&origin={src[0]},{src[1]}"
        f"&destination={dst[0]},{dst[1]}"
        f"&return=summary"
        f"&traffic=enabled"
        f"&apikey={HERE_KEY}"
    )
    try:
        data = _get(url)
        section = data["routes"][0]["sections"][0]["summary"]
        dur  = section["duration"]
        dist = section["length"]
        return dur, dist
    except Exception as e:
        import sys
        print(f"[HERE KO, fallback OSRM] {e}", file=sys.stderr)
        return _route_osrm_with_factor(src, dst)


def _route_osrm_with_factor(src, dst):
    """OSRM (sans trafic) × multiplicateur heure de pointe Paris."""
    url = f"{OSRM}/route/v1/driving/{src[1]},{src[0]};{dst[1]},{dst[0]}?overview=false"
    d = _get(url)
    if d.get("code") != "Ok" or not d.get("routes"):
        return None
    r    = d["routes"][0]
    dur  = r["duration"] * _traffic_multiplier()
    dist = r["distance"]
    return dur, dist


def out(obj):
    print(json.dumps(obj, ensure_ascii=False))


def main():
    if len(sys.argv) < 2:
        out({"error": "usage: eta.py <adresse>"})
        return
    addr = " ".join(sys.argv[1:])
    try:
        pos = json.load(open(POS_FILE))
    except Exception:
        out({"error": "position live indisponible"})
        return
    if time.time() - pos["ts"] > STALE:
        out({"error": "position périmée", "age_s": round(time.time() - pos["ts"])})
        return
    q = expand(addr)
    try:
        g = geocode(q)
    except Exception as e:
        out({"error": f"nominatim: {e}"})
        return
    if not g:
        out({"error": "adresse non géocodée", "query": q})
        return
    lat, lon, name = g
    try:
        rt = route((pos["lat"], pos["lon"]), (lat, lon))
    except Exception as e:
        out({"error": f"osrm: {e}"})
        return
    if not rt:
        out({"error": "OSRM indisponible"})
        return
    dur, dist = rt
    mins = round(dur / 60)
    if dur >= THRESHOLD:
        reply, decision = None, "ignore"
    elif dur < SP_UNDER:
        reply, decision = "SP", "alert"
    else:
        reply, decision = str(mins), "alert"
    out({"address": addr, "resolved": name, "eta_min": mins,
         "dist_km": round(dist / 1000, 1), "reply": reply, "decision": decision})


if __name__ == "__main__":
    main()

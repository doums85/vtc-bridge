#!/usr/bin/env python3
"""Teste le parseur sur des vrais messages capturés dans les groupes."""
import vtc_bridge as v

SAMPLES = [
    "Immédiat / Hôtel Paris 14 / Paris 19 / 35€ PAB",
    "🟢🟢 IMMÉDIAT / 📍 DÉPART : Rue Maurice Ravel, 92300 Levallois-Perret, France / 📍 ARRIVÉE : 12 Place Léon Blum, 75011 Paris / 💵 45.00€",
    "🔴🔴 Réservation pour aujourd'hui / 🕰 Heure: 11:00 / 📍 DÉPART: 75019 Paris / 📍 ARRIVÉE: Pl. Louis Armand, 75012 Paris / 💵 30€",
    "Immédiat 5h45 / St Germain en Laye / Campus paris St Germain / 5 km / 30 euro",
    "6h10 / Noisy le sec / Disney / 50€",
    "Stade de France / Hotel CDG / 40€",
    "23h30 / Saint Denis -> P17 / 35€ PAB",
    "Immédiat  ❌  Relance / P1 pour P8 / 29/9 net PAB     2,8km",
    "🆔 : C2JR1085403 / COURSE IMMÉDIATE 🚨  all / Nbr de Pax :1 / Paiement : Par Carte bancaire 💳 / 📍 De : 75010 Paris / 📍 À : 75003 Paris / Net 22 €",
    "🆔 : C2JR112724 / COURSE IMMÉDIATE 🚨  all / 📍 De : Rue du Bel air 92190 Meudon / 📍 À : Rue des Vignes 92140 Clamart / Net 35€",
    "Aujourd’hui 15:30. VAN. / 5 personnes / ✅️ Gare du nord Paris / 🚩 Hotel Hilton paris opera / Trajet 2 km / 40€",
    "Immédiat / Orly / P5 / 40€",
    "Immédiat / Disney P9 / 90€ / Paiement sous 7 jours",
    "Imediat 75019(Quai de la Marne) pour 75010 3km / 25 net pab",
    "10:00 / Le chesnay Rocquencourt / P16 / Gdl / 2 pax / 70€ pab",
    "Immédiatemnt / Nanterre / Secteur Montparnasse / 50€ PAB 🔥",
    "Imediat 75016 pour Orly / 50 net pab",
    "Resa aujourd'hui / 12h00 / Paris 19 / Paris 20 / 10 min / 20€",
    # bruit -> doivent être ignorés
    "Ok sp",
    "Privé stp",
    "10",
    "Pv",
]

print(f"{'is_course':>9}  départ extrait                          | message")
print("-" * 100)
for s in SAMPLES:
    ok = v.is_course(s)
    picks = v.extract_pickup(s) if ok else []
    dep = picks[0] if picks else "—"
    flag = "COURSE" if ok else "bruit"
    print(f"{flag:>9}  {dep[:38]:38} | {s[:52]}")

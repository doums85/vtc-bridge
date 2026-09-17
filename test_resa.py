#!/usr/bin/env python3
"""Teste la détection réservation + extraction prix/km/arrivée sur de vrais messages."""
import vtc_bridge as v

SAMPLES = [
    "Réservation 8h30 / Roissy / Paris 17 / 50 net",
    "🔴🔴 Réservation pour aujourd'hui / Heure: 11:00 / 📍 DÉPART: 75019 Paris / 📍 ARRIVÉE: 75012 Paris / 30€",
    "6h10 / Noisy le sec / Disney / 50€",
    "Lundi 8h / Saint Denis / Cdg / 40€",
    "12/07 à 6h30 / 91300 Massy / Orly / 30€",
    "Aujourd’hui 15:30 VAN / 5 personnes / Gare du nord / Hotel Hilton / Trajet 2 km / 40€",
    "13 juillet / Train arrive 8h53 / Austerlitz / Gare du nord / 25€ par trajet",
    # immédiats -> NE DOIVENT PAS être des réservations
    "Immédiat / Hôtel Paris 14 / Paris 19 / 35€ PAB",
    "Immédiat 5h45 / St Germain en Laye / Campus paris St Germain / 5 km / 30 euro",
]

print(f"{'résa':>5}  {'prix':>5}  {'km':>5}  {'€/km':>5}  arrivée              | message")
print("-" * 100)
for s in SAMPLES:
    resa = v.is_reservation(s)
    price = v.extract_price(s)
    km = v.extract_km(s)
    arr = v.extract_dropoff(s) or "—"
    eurkm = f"{price/km:.2f}" if (resa and price and km) else "—"
    print(f"{('OUI' if resa else 'non'):>5}  {str(price or '—'):>5}  {str(km or '—'):>5}  {eurkm:>5}  {arr[:20]:20} | {s[:46]}")

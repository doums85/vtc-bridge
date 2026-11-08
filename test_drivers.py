#!/usr/bin/env python3
"""Multi-chauffeurs : réglages, règles de décision, isolation des données.

Ne dépend ni de Telegram ni du réseau : drivers.py + store.py sur une base temporaire.
"""
import os
import sys
import tempfile

import drivers as D
import store


def check(label, got, want):
    ok = got == want
    print(f"{'OK ' if ok else 'ÉCHEC'} {label}" + ("" if ok else f" (attendu {want!r}, obtenu {got!r})"))
    return ok


def raises(label, fn):
    try:
        fn()
    except ValueError:
        print(f"OK  {label}")
        return True
    print(f"ÉCHEC {label} (aucune erreur levée)")
    return False


def test_settings() -> bool:
    ok = True
    p = D.Profile(id=1)
    # défauts = ancien comportement (.env d'origine)
    ok &= check("défaut seuil temps", p.get("threshold_min"), 8)
    ok &= check("défaut prix min", p.get("min_price"), 0)
    ok &= check("défaut distance", p.get("max_pickup_km"), 0)
    # saisie libre : virgule, texte autour, unité
    ok &= check("saisie '12,5 km'", D.coerce(D.SETTINGS_BY_KEY["max_pickup_km"], "12,5 km"), 12.5)
    ok &= check("saisie entier", D.coerce(D.SETTINGS_BY_KEY["threshold_min"], "10 min"), 10)
    ok &= raises("hors bornes refusé", lambda: D.coerce(D.SETTINGS_BY_KEY["threshold_min"], "500"))
    ok &= raises("texte non numérique refusé", lambda: D.coerce(D.SETTINGS_BY_KEY["min_price"], "beaucoup"))
    ok &= raises("négatif refusé", lambda: D.coerce(D.SETTINGS_BY_KEY["min_price"], "-5"))
    ok &= raises("texte vide refusé", lambda: D.coerce(D.SETTINGS_BY_KEY["resa_reply"], "  "))
    ok &= check("booléen oui", D.coerce(D.SETTINGS_BY_KEY["accept_van"], "oui"), True)
    # un réglage corrompu en base retombe sur le défaut au lieu de planter
    bad = D.Profile(id=2, settings={"min_price": "n'importe quoi", "threshold_min": 9999})
    ok &= check("réglage corrompu -> défaut", (bad.get("min_price"), bad.get("threshold_min")), (0, 8))
    # affichage
    ok &= check("0 km = sans limite", D.fmt_value(D.SETTINGS_BY_KEY["max_pickup_km"], 0), "sans limite")
    ok &= check("prix", D.fmt_value(D.SETTINGS_BY_KEY["min_price"], 30), "30 €")
    ok &= check("€/km décimal", D.fmt_value(D.SETTINGS_BY_KEY["eurkm_min"], 1.5), "1.5 €/km")
    # boutons − / +
    s = D.SETTINGS_BY_KEY["min_price"]
    ok &= check("+ pas de 5", D.step_value(s, 30, +1), 35)
    ok &= check("− borné à 0", D.step_value(s, 0, -1), 0)
    ok &= check("+ borné au max", D.step_value(s, 1000, +1), 1000)
    ok &= check("bascule booléen", D.step_value(D.SETTINGS_BY_KEY["accept_van"], False, +1), True)
    e = D.SETTINGS_BY_KEY["eurkm_min"]
    ok &= check("pas décimal sans dérive", D.step_value(e, 1.5, +1), 1.6)
    return ok


def test_rules() -> bool:
    ok = True
    p = D.Profile(id=1)
    # immédiat : temps
    ok &= check("immédiat à 6 min ok", D.accept_immediate(p, price=30, eta_s=360, dist_m=2000)[0], True)
    ok &= check("immédiat à 9 min refusé", D.accept_immediate(p, price=30, eta_s=540, dist_m=2000)[0], False)
    # les réglages du chauffeur changent la décision
    p.set("threshold_min", 15)
    ok &= check("seuil relevé -> 9 min ok", D.accept_immediate(p, price=30, eta_s=540, dist_m=2000)[0], True)
    p.set("max_pickup_km", 5)
    ok &= check("distance 8 km > 5 refusée", D.accept_immediate(p, price=30, eta_s=300, dist_m=8000)[0], False)
    ok &= check("distance 4 km <= 5 ok", D.accept_immediate(p, price=30, eta_s=300, dist_m=4000)[0], True)
    p.set("min_price", 25)
    ok &= check("prix 20 < 25 refusé", D.accept_immediate(p, price=20, eta_s=300, dist_m=1000)[0], False)
    ok &= check("prix 25 ok", D.accept_immediate(p, price=25, eta_s=300, dist_m=1000)[0], True)
    ok &= check("prix illisible passe", D.accept_immediate(p, price=None, eta_s=300, dist_m=1000)[0], True)
    # réservations
    ok &= check("résa prix 20 < 25 refusée", D.accept_resa_price(p, price=20)[0], False)
    ok &= check("résa prix 60 ok", D.accept_resa_price(p, price=60)[0], True)
    ok &= check("résa 1.4 €/km < 1.5 refusée", D.accept_resa_rate(p, eurkm=1.4)[0], False)
    ok &= check("résa 1.5 €/km ok", D.accept_resa_rate(p, eurkm=1.5)[0], True)
    ok &= check("rayon off : tout passe", D.accept_resa_radius(p, pickup_km=150)[0], True)
    p.set("resa_radius_km", 30)
    ok &= check("rayon 30 : 45 km refusée", D.accept_resa_radius(p, pickup_km=45)[0], False)
    ok &= check("rayon 30 : 12 km ok", D.accept_resa_radius(p, pickup_km=12)[0], True)
    ok &= check("rayon 30 : distance inconnue passe", D.accept_resa_radius(p, pickup_km=None)[0], True)
    # distance à vol d'oiseau : Gare de Lyon -> CDG ≈ 23 km
    d = D.haversine_km(48.8443, 2.3744, 49.0097, 2.5479)
    ok &= check("haversine Gare de Lyon -> CDG ≈ 23 km", 21 < d < 25, True)
    return ok


def test_isolation() -> bool:
    ok = True
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    store.init(path)
    A, B = 111, 222
    store.upsert_driver(A, name="Alice", groups=[-1001])
    store.upsert_driver(B, name="Bruno", groups=[-1001, -1002])
    a1 = store.record_course(driver_id=A, kind="immediat", price=40, alert_id=7, status="obtenue")
    b1 = store.record_course(driver_id=B, kind="immediat", price=15, alert_id=7, status="obtenue")
    store.add_expense(A, 10, "essence")
    store.set_setting(A, "objectif_jour", 200)

    ok &= check("CA Alice", store.summary(A, "tout")["ca"], 40)
    ok &= check("CA Bruno", store.summary(B, "tout")["ca"], 15)
    ok &= check("frais Alice", store.summary(A, "tout")["frais"], 10)
    ok &= check("frais Bruno vides", store.summary(B, "tout")["frais"], 0)
    ok &= check("objectif propre à Alice", (store.get_setting(A, "objectif_jour"),
                                            store.get_setting(B, "objectif_jour")), ("200", None))
    # même id de message d'alerte chez deux chauffeurs : pas de collision
    ok &= check("find_by_alert Alice", store.find_by_alert(7, A)["id"], a1)
    ok &= check("find_by_alert Bruno", store.find_by_alert(7, B)["id"], b1)
    # un id de course deviné ne traverse pas
    ok &= check("get course d'autrui = None", store.get(a1, B), None)
    store.set_status(a1, "ratée", driver_id=B)
    ok &= check("set_status d'autrui sans effet", store.get(a1)["status"], "obtenue")
    ok &= check("append_note d'autrui = None", store.append_note(a1, "x", driver_id=B), None)
    ok &= check("adjust_amount d'autrui = None", store.adjust_amount(a1, price=1, driver_id=B), None)
    ok &= check("prix intact", store.get(a1)["price"], 40)
    ok &= check("listes séparées", ([r["id"] for r in store.recent(A)], [r["id"] for r in store.recent(B)]),
                ([a1], [b1]))
    # profils
    pa = D.profile_from_row(store.get_driver_row(A))
    ok &= check("groupes Bruno", D.profile_from_row(store.get_driver_row(B)).groups, [-1001, -1002])
    pa.set("min_price", 30)
    store.save_driver_settings(A, pa.settings)
    ok &= check("réglage persisté", D.profile_from_row(store.get_driver_row(A)).get("min_price"), 30)
    ok &= check("réglage de Bruno intact", D.profile_from_row(store.get_driver_row(B)).get("min_price"), 0)
    # ré-ajout d'un chauffeur : garde ses réglages et son état
    store.set_driver_active(A, False)
    store.upsert_driver(A, name="Alice B.", groups=[-1001, -1003])
    row = store.get_driver_row(A)
    ok &= check("ré-ajout garde réglages/état", (D.profile_from_row(row).get("min_price"), row["active"]), (30, 0))
    ok &= check("ré-ajout met à jour groupes", D.profile_from_row(row).groups, [-1001, -1003])
    ok &= check("actifs seulement", [r["id"] for r in store.list_driver_rows(active_only=True)], [B])
    return ok


def test_migration() -> bool:
    """Une base d'avant le multi-chauffeurs : tout est rattaché au propriétaire."""
    import sqlite3
    ok = True
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    raw = sqlite3.connect(path)
    raw.execute("""CREATE TABLE courses(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        created TEXT NOT NULL, kind TEXT, group_id INTEGER, group_name TEXT, msg_id INTEGER,
        alert_id INTEGER, pickup TEXT, dropoff TEXT, price REAL, km REAL, eur_km REAL,
        duration_min REAL, eta_min REAL, commission REAL, commission_rate REAL, net REAL,
        reply TEXT, status TEXT, raw TEXT, note TEXT, payment TEXT)""")
    raw.execute("INSERT INTO courses(ts,created,price,status) VALUES(1,'2026-01-01T10:00:00',50,'obtenue')")
    raw.execute("CREATE TABLE expenses(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, created TEXT, amount REAL, label TEXT)")
    raw.execute("INSERT INTO expenses(ts,created,amount,label) VALUES(1,'2026-01-01T10:00:00',12,'péage')")
    raw.execute("CREATE TABLE settings(k TEXT PRIMARY KEY, v TEXT)")
    raw.execute("INSERT INTO settings VALUES('objectif_jour','180')")
    raw.commit()
    raw.close()
    store.init(path)
    OWNER = 999
    ok &= check("lignes orphelines avant", store.summary(OWNER, "tout")["ca"], 0)
    n = store.adopt_legacy_rows(OWNER)
    ok &= check("lignes rattachées", n, 2)
    ok &= check("CA conservé", store.summary(OWNER, "tout")["ca"], 50)
    ok &= check("frais conservés", store.summary(OWNER, "tout")["frais"], 12)
    ok &= check("objectif conservé", store.get_setting(OWNER, "objectif_jour"), "180")
    ok &= check("idempotent", store.adopt_legacy_rows(OWNER), 0)
    ok &= check("un autre chauffeur ne voit rien", store.summary(5, "tout")["ca"], 0)
    return ok


def main() -> int:
    ok = True
    for fn in (test_settings, test_rules, test_isolation, test_migration):
        print(f"\n— {fn.__name__}")
        ok &= fn()
    print("\nTout passe." if ok else "\nDes tests échouent.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Extraction d'adresses : messages réels qui finissaient en « Course à confirmer » (géocodage KO).

Chaque cas vient des logs du 10/11. On vérifie que le texte envoyé à Nominatim est une adresse,
pas une heure, un siège enfant ou une parenthèse mal fermée."""
import vtc_bridge as v


def test_heure_texte_pas_une_adresse():
    t = "Ce soir à 17h00\nSiège bébé -10ans\nMontrouge, France\n75014 Paris, France\n25€"
    assert v.extract_pickup(t)[0] == "Montrouge, France"
    assert v.extract_dropoff(t) == "75014 Paris, France"


def test_rehausseur_pas_une_adresse():
    t = "Ce soir à 17h00\nun réhausseur\nMontrouge, France\n75014 Paris, France\n30€"
    assert v.extract_pickup(t)[0] == "Montrouge, France"
    assert v.extract_dropoff(t) == "75014 Paris, France"


def test_parentheses_aplaties_et_code_postal_en_secours():
    t = "Imediat 75016(Trocadéro) pour 75004(côté quai) 30€ net pab"
    assert v.extract_pickup(t)[:2] == ["75016 Trocadéro", "75016"]
    arr = v.extract_dropoff(t)
    assert arr is not None and arr.startswith("75004")
    assert v.geo_variants(arr)[:2] == ["75004 côté quai", "75004"]


def test_metro_retire_du_premier_candidat():
    t = "Immédiat\nP11 métro Oberkampf\nP16 métro la muettes\n30€"
    assert v.extract_pickup(t)[0] == "75011 Paris Oberkampf"


def test_vers_separe_depart_et_arrivee():
    t = "Immédiat\nhôtel Orly vers Orly\n20 pab"
    assert "vers" not in " ".join(v.extract_pickup(t)).lower()
    assert v.extract_dropoff(t) == "Aéroport d'Orly, 94390 Orly"


def test_hotel_variante_sans_prefixe():
    assert "Aéroport d'Orly, 94390 Orly" in v.geo_variants("hôtel Aéroport d'Orly, 94390 Orly")


def test_cas_existants_inchanges():
    t = "🟢🟢 IMMÉDIAT / 📍 DÉPART : Rue Maurice Ravel, 92300 Levallois-Perret, France / 📍 ARRIVÉE : 12 Place Léon Blum, 75011 Paris / 💵 45.00€"
    assert v.extract_pickup(t)[0].startswith("Rue Maurice Ravel")
    assert v.extract_dropoff(t) == "Place Léon Blum, 75011 Paris"   # même résultat qu'avant la correction


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

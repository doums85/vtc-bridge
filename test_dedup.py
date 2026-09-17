#!/usr/bin/env python3
"""Déduplication inter-groupes : empreinte géographique, et repli textuel quand elle manque."""
import time

import vtc_bridge as v

# Le cas réel du 16/09 : même course postée dans quatre groupes, aucun code postal.
LA_DEFENSE = "Immédiat la défense pour Courbevoie 25 net pab"
REPOSTS = [
    "Immédiat la défense pour Courbevoie 25 net pab",
    "immediat LA DEFENSE pour courbevoie 25 net pab",       # casse + accents
    "🚨 Immédiat  la  défense  pour  Courbevoie  25 net pab 🚨",  # emoji + espaces
]


def fresh():
    v._dup_seen.clear()


def check(label, got, want):
    print(f"{'OK ' if got == want else 'ÉCHEC'} {label} (attendu {want}, obtenu {got})")
    return got == want


def main() -> int:
    ok = True
    fresh()

    # 1. Empreinte géographique : comportement inchangé.
    geo = "Immédiat / 75010 Paris / 75003 Paris / 22€"
    fp = v.course_fingerprint(geo)
    ok &= check("empreinte géo calculée", fp is not None, True)
    ok &= check("géo, 1re occurrence", v.is_duplicate_course(fp), False)
    ok &= check("géo, repost", v.is_duplicate_course(v.course_fingerprint(geo)), True)

    # 2. Sans code postal : l'empreinte géo reste muette, c'est le repli qui doit couvrir.
    fresh()
    ok &= check("pas d'empreinte géo sans code postal", v.course_fingerprint(LA_DEFENSE) is None, True)
    first = v.is_duplicate_course(v.course_text_fingerprint(REPOSTS[0]), v.DEDUP_TEXT_WINDOW_SECONDS)
    ok &= check("texte, 1re occurrence", first, False)
    for i, repost in enumerate(REPOSTS[1:], start=2):
        got = v.is_duplicate_course(v.course_text_fingerprint(repost), v.DEDUP_TEXT_WINDOW_SECONDS)
        ok &= check(f"texte, repost {i} (casse/accents/emoji)", got, True)

    # 3. Deux courses différentes ne doivent PAS fusionner (prix distinct).
    fresh()
    v.is_duplicate_course(v.course_text_fingerprint("Immédiat la défense pour Courbevoie 25 net pab"),
                          v.DEDUP_TEXT_WINDOW_SECONDS)
    autre = v.is_duplicate_course(v.course_text_fingerprint("Immédiat la défense pour Courbevoie 45 net pab"),
                                  v.DEDUP_TEXT_WINDOW_SECONDS)
    ok &= check("prix différent -> course distincte", autre, False)

    # 4. Hors fenêtre : la même formulation redevient une course à part entière.
    fresh()
    fp_txt = v.course_text_fingerprint(LA_DEFENSE)
    v._dup_seen[fp_txt] = time.time() - v.DEDUP_TEXT_WINDOW_SECONDS - 1
    ok &= check("hors fenêtre -> pas un doublon",
                v.is_duplicate_course(fp_txt, v.DEDUP_TEXT_WINDOW_SECONDS), False)

    # 5. Message trop court -> pas d'empreinte, jamais de dédup au hasard.
    ok &= check("message trop court", v.course_text_fingerprint("ok sp") is None, True)

    print("\n" + ("Tout passe." if ok else "Au moins un test échoue."))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

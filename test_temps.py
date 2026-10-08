#!/usr/bin/env python3
"""Temps proposés après « Accepter » : le meilleur (le plus court tenable) puis les paliers de 5 min."""
import vtc_bridge as v


def check(label, got, want):
    print(f"{'OK ' if got == want else 'ÉCHEC'} {label} (attendu {want}, obtenu {got})")
    return got == want


def main() -> int:
    ok = True
    # Sous le seuil SP (5 min par défaut) : « SP » est le meilleur choix.
    ok &= check("2 min -> SP", v.time_options(2 * 60), ("SP", ["SP", "5", "10"]))
    ok &= check("4 min 59 -> SP", v.time_options(299)[0], "SP")
    # Au-delà : minute supérieure (annoncer moins = arriver en retard).
    ok &= check("5 min pile", v.time_options(300), ("5", ["5", "10", "15"]))
    ok &= check("7 min 10 -> 8", v.time_options(7 * 60 + 10), ("8", ["8", "10", "15"]))
    ok &= check("10 min pile", v.time_options(600), ("10", ["10", "15", "20"]))
    ok &= check("12 min 30 -> 13", v.time_options(12 * 60 + 30), ("13", ["13", "15", "20"]))
    # Le meilleur choix est toujours proposé en premier, sans doublon.
    for s in range(0, 30 * 60, 17):
        best, opts = v.time_options(s)
        if opts[0] != best or len(set(opts)) != len(opts):
            ok &= check(f"ordre/doublon à {s}s", opts, "meilleur en tête, sans doublon")
    print("\nTout passe." if ok else "\nDes tests échouent.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

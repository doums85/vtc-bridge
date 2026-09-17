#!/usr/bin/env python3
"""
Journal des courses (SQLite) : persistance + résumés (prix, commission, net).

Chaque alerte envoyée est enregistrée (statut « proposée »). Les taps du chauffeur
font évoluer le statut : proposée -> répondue -> obtenue / ratée (ou passée / expirée).
Le résumé (/resume) agrège les courses réellement OBTENUES : CA, commission, net, €/km.
"""
from __future__ import annotations

import os
import time
import sqlite3
import datetime as dt

_DB: "sqlite3.Connection | None" = None

# Cycle de vie. Immédiat : proposée -> répondue -> obtenue (compte dans le CA réalisé).
# Réservation : proposée -> répondue -> réservée (à venir) -> effectuée (compte) / annulée.
STATUSES = ("proposée", "répondue", "obtenue", "réservée", "effectuée",
            "ratée", "annulée", "passée", "expirée")
# Statuts qui comptent dans le CA RÉALISÉ (argent réellement gagné).
REALIZED = ("obtenue", "effectuée")


def init(path: str) -> None:
    global _DB
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    _DB = sqlite3.connect(path, check_same_thread=False)
    _DB.row_factory = sqlite3.Row
    _DB.execute("PRAGMA journal_mode=WAL")
    _DB.execute(
        """CREATE TABLE IF NOT EXISTS courses(
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             ts REAL NOT NULL,           -- epoch de l'enregistrement
             created TEXT NOT NULL,      -- datetime local ISO
             kind TEXT,                  -- 'immediat' | 'resa'
             group_id INTEGER,
             group_name TEXT,
             msg_id INTEGER,             -- id du message source (groupe)
             alert_id INTEGER,           -- id du message d'alerte DM (lookup callback)
             pickup TEXT,
             dropoff TEXT,
             price REAL,
             km REAL,
             eur_km REAL,
             duration_min REAL,
             eta_min REAL,
             commission REAL,
             commission_rate REAL,
             net REAL,
             reply TEXT,
             status TEXT,
             raw TEXT,
             note TEXT,
             payment TEXT
           )""")
    # migrations
    cols = [r[1] for r in _DB.execute("PRAGMA table_info(courses)")]
    if "payment" not in cols:
        _DB.execute("ALTER TABLE courses ADD COLUMN payment TEXT")
    if "prompted" not in cols:          # rappel post-course déjà envoyé ? (0/1)
        _DB.execute("ALTER TABLE courses ADD COLUMN prompted INTEGER DEFAULT 0")
    if "sched_ts" not in cols:          # epoch prévu de la réservation (pour le rappel)
        _DB.execute("ALTER TABLE courses ADD COLUMN sched_ts REAL")
    _DB.execute("UPDATE courses SET status='réservée' WHERE status='resa_future'")  # ancien nom
    # frais (essence, péages, charges…) et réglages (objectif du jour…)
    _DB.execute("""CREATE TABLE IF NOT EXISTS expenses(
             id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, created TEXT, amount REAL, label TEXT)""")
    _DB.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)")
    _DB.commit()


def record_course(*, kind=None, group_id=None, group_name=None, msg_id=None,
                  alert_id=None, pickup=None, dropoff=None, price=None, km=None,
                  eur_km=None, duration_min=None, eta_min=None, commission=None,
                  commission_rate=None, net=None, reply=None, raw=None,
                  status="proposée") -> int:
    cur = _DB.execute(
        """INSERT INTO courses(ts,created,kind,group_id,group_name,msg_id,alert_id,
             pickup,dropoff,price,km,eur_km,duration_min,eta_min,commission,
             commission_rate,net,reply,status,raw)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (time.time(), dt.datetime.now().isoformat(timespec="seconds"), kind, group_id,
         group_name, msg_id, alert_id, pickup, dropoff, price, km, eur_km,
         duration_min, eta_min, commission, commission_rate, net, reply, status, raw))
    _DB.commit()
    return cur.lastrowid


def get(cid: int):
    return _DB.execute("SELECT * FROM courses WHERE id=?", (cid,)).fetchone()


def last_course():
    return _DB.execute("SELECT * FROM courses ORDER BY id DESC LIMIT 1").fetchone()


def last_realized_course():
    """Dernière course RÉELLE (obtenue/effectuée/réservée) — pas une alerte proposée/passée.
    C'est celle que /prix et /comm doivent viser par défaut."""
    return _DB.execute(
        "SELECT * FROM courses WHERE status IN ('obtenue','effectuée','réservée') "
        "ORDER BY id DESC LIMIT 1").fetchone()


def set_status(cid: int, status: str) -> None:
    _DB.execute("UPDATE courses SET status=? WHERE id=?", (status, cid))
    _DB.commit()


def set_reply(cid: int, reply: str) -> None:
    _DB.execute("UPDATE courses SET reply=? WHERE id=?", (reply, cid))
    _DB.commit()


def find_by_alert(alert_id: int):
    """Course associée à un message d'alerte DM (pour annoter en répondant à la carte)."""
    return _DB.execute(
        "SELECT * FROM courses WHERE alert_id=? ORDER BY id DESC LIMIT 1", (alert_id,)).fetchone()


def set_payment(cid: int, payment: str) -> None:
    _DB.execute("UPDATE courses SET payment=? WHERE id=?", (payment, cid))
    _DB.commit()


def append_note(cid: int, note: str):
    """Ajoute une note (paiement, détail client…) — cumulée avec les précédentes."""
    row = get(cid)
    if not row:
        return None
    combined = (row["note"] + " · " + note) if row["note"] else note
    _DB.execute("UPDATE courses SET note=? WHERE id=?", (combined, cid))
    _DB.commit()
    return get(cid)


def adjust_amount(cid: int, price=None, commission=None, rate=None):
    """Corrige prix et/ou commission d'une course et recalcule le net."""
    row = get(cid)
    if not row:
        return None
    p = price if price is not None else row["price"]
    if commission is not None:
        c = commission
    elif rate is not None and p is not None:
        c = round(p * rate, 2)
    else:
        c = row["commission"]
    net = (p - c) if (p is not None and c is not None) else p
    _DB.execute("UPDATE courses SET price=?,commission=?,net=? WHERE id=?", (p, c, net, cid))
    _DB.commit()
    return get(cid)


def _bound(period: str) -> float:
    now = dt.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period in ("jour", "today", "aujourd'hui", "auj"):
        start = midnight
    elif period == "24h":                                       # dernières 24 h (capture une nuit)
        return (now - dt.timedelta(hours=24)).timestamp()
    elif period == "semaine":
        start = midnight - dt.timedelta(days=now.weekday())      # depuis lundi
    elif period == "mois":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:                                                        # tout / total
        return 0.0
    return start.timestamp()


_REAL_SQL = "(" + ",".join(f"'{s}'" for s in REALIZED) + ")"   # ('obtenue','effectuée')


def summary(period: str = "jour") -> dict:
    b = _bound(period)
    counts = {s: 0 for s in STATUSES}
    for r in _DB.execute("SELECT status, COUNT(*) n FROM courses WHERE ts>=? GROUP BY status", (b,)):
        if r["status"] in counts:
            counts[r["status"]] = r["n"]
    done = _DB.execute(
        f"""SELECT COUNT(*) n, COALESCE(SUM(price),0) ca, COALESCE(SUM(commission),0) comm,
                   COALESCE(SUM(net),0) net, COALESCE(SUM(km),0) km,
                   COALESCE(SUM(duration_min),0) dur
            FROM courses WHERE ts>=? AND status IN {_REAL_SQL}""", (b,)).fetchone()
    ek = _DB.execute(
        f"SELECT AVG(eur_km) e FROM courses WHERE ts>=? AND status IN {_REAL_SQL} AND eur_km IS NOT NULL",
        (b,)).fetchone()
    by_group = _DB.execute(
        f"""SELECT COALESCE(group_name,'?') g, COUNT(*) n, COALESCE(SUM(price),0) ca
            FROM courses WHERE ts>=? AND status IN {_REAL_SQL}
            GROUP BY group_name ORDER BY ca DESC""", (b,)).fetchall()
    pays = _DB.execute(
        f"""SELECT payment p, COUNT(*) n, COALESCE(SUM(price),0) ca
            FROM courses WHERE ts>=? AND status IN {_REAL_SQL} AND payment IS NOT NULL
            GROUP BY payment ORDER BY ca DESC""", (b,)).fetchall()
    # réservations À VENIR (validées, pas encore effectuées) — indépendant de la période
    up = _DB.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(price),0) ca FROM courses WHERE status='réservée'").fetchone()
    frais = expenses_total(period)
    return {
        "period": period,
        "counts": counts,
        "done": done["n"], "ca": done["ca"], "commission": done["comm"],
        "net": done["net"], "km": done["km"], "duration_min": done["dur"],
        "eur_km_avg": ek["e"],
        "by_group": [(r["g"], r["n"], r["ca"]) for r in by_group],
        "payments": [(r["p"], r["n"], r["ca"]) for r in pays],
        "upcoming_n": up["n"], "upcoming_ca": up["ca"],
        "frais": frais, "benefice": done["net"] - frais,
    }


def list_done(period: str = "jour", limit: int = 25):
    b = _bound(period)
    return _DB.execute(
        f"SELECT * FROM courses WHERE ts>=? AND status IN {_REAL_SQL} ORDER BY ts LIMIT ?",
        (b, limit)).fetchall()


def courses_detail(period: str, statuses=("obtenue", "effectuée", "réservée")):
    """Toutes les courses (réalisées + à venir) d'une période, pour le tableau détaillé."""
    b = _bound(period)
    ph = ",".join("?" * len(statuses))
    return _DB.execute(
        f"SELECT * FROM courses WHERE ts>=? AND status IN ({ph}) ORDER BY ts", (b, *statuses)).fetchall()


def pending_reservations(limit: int = 20):
    """Réservations validées (à venir), les plus proches d'abord (par heure prévue)."""
    return _DB.execute(
        "SELECT * FROM courses WHERE status='réservée' ORDER BY COALESCE(sched_ts, ts) LIMIT ?",
        (limit,)).fetchall()


def set_sched(cid: int, sched_ts: "float | None") -> None:
    _DB.execute("UPDATE courses SET sched_ts=? WHERE id=?", (sched_ts, cid))
    _DB.commit()


def set_prompted(cid: int) -> None:
    _DB.execute("UPDATE courses SET prompted=1 WHERE id=?", (cid,))
    _DB.commit()


def add_expense(amount: float, label: str) -> None:
    _DB.execute("INSERT INTO expenses(ts,created,amount,label) VALUES(?,?,?,?)",
                (time.time(), dt.datetime.now().isoformat(timespec="seconds"), amount, label))
    _DB.commit()


def expenses_total(period: str) -> float:
    b = _bound(period)
    r = _DB.execute("SELECT COALESCE(SUM(amount),0) s FROM expenses WHERE ts>=?", (b,)).fetchone()
    return r["s"]


def list_expenses(period: str, limit: int = 12):
    b = _bound(period)
    return _DB.execute("SELECT * FROM expenses WHERE ts>=? ORDER BY ts LIMIT ?", (b, limit)).fetchall()


def last_expense():
    return _DB.execute("SELECT * FROM expenses ORDER BY id DESC LIMIT 1").fetchone()


def del_expense(eid: int) -> None:
    _DB.execute("DELETE FROM expenses WHERE id=?", (eid,))
    _DB.commit()


def set_setting(k: str, v) -> None:
    _DB.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, str(v)))
    _DB.commit()


def get_setting(k: str, default=None):
    r = _DB.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def due_reservations(now_ts: float):
    """Réservations validées dont l'heure est passée et pour lesquelles on n'a pas encore
    demandé « effectuée ? » (rappel post-course)."""
    return _DB.execute(
        "SELECT * FROM courses WHERE status='réservée' AND COALESCE(prompted,0)=0 "
        "AND sched_ts IS NOT NULL AND sched_ts<=? ORDER BY sched_ts", (now_ts,)).fetchall()


def recent(limit: int = 8, statuses: "tuple | None" = None):
    """Dernières courses (toutes ou filtrées par statut), la plus récente d'abord."""
    if statuses:
        ph = ",".join("?" * len(statuses))
        return _DB.execute(
            f"SELECT * FROM courses WHERE status IN ({ph}) ORDER BY id DESC LIMIT ?",
            (*statuses, limit)).fetchall()
    return _DB.execute("SELECT * FROM courses ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

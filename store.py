#!/usr/bin/env python3
"""
Journal des courses (SQLite) : persistance + résumés (prix, commission, net).

Plusieurs chauffeurs partagent la même base : chaque ligne (course, frais, réglage) porte
un `driver_id` et chaque lecture est filtrée dessus. Un chauffeur ne voit jamais les
courses d'un autre.

Chaque alerte envoyée est enregistrée (statut « proposée »). Les taps du chauffeur
font évoluer le statut : proposée -> répondue -> obtenue / ratée (ou passée / expirée).
Le résumé (/resume) agrège les courses réellement OBTENUES : CA, commission, net, €/km.
"""
from __future__ import annotations

import os
import json
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
    _DB.execute("PRAGMA busy_timeout=5000")      # le script admin écrit en parallèle du pont
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
    if "driver_id" not in cols:         # multi-chauffeurs : propriétaire de la ligne
        _DB.execute("ALTER TABLE courses ADD COLUMN driver_id INTEGER")
    _DB.execute("UPDATE courses SET status='réservée' WHERE status='resa_future'")  # ancien nom
    # frais (essence, péages, charges…) et réglages (objectif du jour…)
    _DB.execute("""CREATE TABLE IF NOT EXISTS expenses(
             id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, created TEXT, amount REAL, label TEXT)""")
    ecols = [r[1] for r in _DB.execute("PRAGMA table_info(expenses)")]
    if "driver_id" not in ecols:
        _DB.execute("ALTER TABLE expenses ADD COLUMN driver_id INTEGER")
    _DB.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)")  # ancien, global
    _DB.execute("""CREATE TABLE IF NOT EXISTS kv(
             driver_id INTEGER NOT NULL, k TEXT NOT NULL, v TEXT,
             PRIMARY KEY(driver_id, k))""")
    _DB.execute("""CREATE TABLE IF NOT EXISTS drivers(
             id INTEGER PRIMARY KEY,           -- id Telegram = chat du bot
             name TEXT,
             session TEXT,                     -- chemin de la session Telethon (sans .session)
             groups TEXT NOT NULL DEFAULT '[]',-- JSON : ids des groupes lus par ce chauffeur
             feed_group INTEGER DEFAULT 0,     -- relais Hermès optionnel
             settings TEXT NOT NULL DEFAULT '{}',  -- JSON : réglages modifiés par le chauffeur
             active INTEGER NOT NULL DEFAULT 1,
             is_owner INTEGER NOT NULL DEFAULT 0,
             created TEXT)""")
    _DB.execute("CREATE INDEX IF NOT EXISTS idx_courses_driver ON courses(driver_id, ts)")
    _DB.execute("CREATE INDEX IF NOT EXISTS idx_courses_alert ON courses(driver_id, alert_id)")
    _DB.execute("CREATE INDEX IF NOT EXISTS idx_expenses_driver ON expenses(driver_id, ts)")
    _DB.commit()


def adopt_legacy_rows(driver_id: int) -> int:
    """Rattache au chauffeur d'origine tout ce qui a été écrit avant le multi-chauffeurs
    (lignes sans driver_id, réglages globaux). Idempotent. Retourne le nombre de lignes."""
    n = 0
    for table in ("courses", "expenses"):
        n += _DB.execute(f"UPDATE {table} SET driver_id=? WHERE driver_id IS NULL",
                         (driver_id,)).rowcount
    for r in _DB.execute("SELECT k, v FROM settings").fetchall():
        _DB.execute("INSERT OR IGNORE INTO kv(driver_id,k,v) VALUES(?,?,?)",
                    (driver_id, r["k"], r["v"]))
    _DB.commit()
    return n


# ───────────────────────────── Chauffeurs ─────────────────────────────
def upsert_driver(driver_id: int, *, name: str = "", session: str = "", groups=None,
                  feed_group: int = 0, is_owner: "bool | None" = None) -> None:
    """Crée le chauffeur ou met à jour son identité. Ne touche ni à ses réglages ni à son
    état actif : un second passage du script d'ajout ne remet rien à zéro."""
    cur = _DB.execute("SELECT 1 FROM drivers WHERE id=?", (driver_id,)).fetchone()
    gjson = json.dumps([int(g) for g in (groups or [])])
    if cur is None:
        _DB.execute(
            "INSERT INTO drivers(id,name,session,groups,feed_group,is_owner,created) "
            "VALUES(?,?,?,?,?,?,?)",
            (driver_id, name, session, gjson, int(feed_group or 0), int(bool(is_owner)),
             dt.datetime.now().isoformat(timespec="seconds")))
    else:
        _DB.execute("UPDATE drivers SET name=COALESCE(NULLIF(?,''),name), "
                    "session=COALESCE(NULLIF(?,''),session), groups=?, feed_group=? WHERE id=?",
                    (name, session, gjson, int(feed_group or 0), driver_id))
        if is_owner is not None:
            _DB.execute("UPDATE drivers SET is_owner=? WHERE id=?", (int(is_owner), driver_id))
    _DB.commit()


def get_driver_row(driver_id: int):
    return _DB.execute("SELECT * FROM drivers WHERE id=?", (driver_id,)).fetchone()


def list_driver_rows(active_only: bool = False):
    sql = "SELECT * FROM drivers" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
    return _DB.execute(sql).fetchall()


def set_driver_active(driver_id: int, active: bool) -> None:
    _DB.execute("UPDATE drivers SET active=? WHERE id=?", (int(active), driver_id))
    _DB.commit()


def set_driver_groups(driver_id: int, groups) -> None:
    _DB.execute("UPDATE drivers SET groups=? WHERE id=?",
                (json.dumps([int(g) for g in groups]), driver_id))
    _DB.commit()


def save_driver_settings(driver_id: int, settings: dict) -> None:
    _DB.execute("UPDATE drivers SET settings=? WHERE id=?",
                (json.dumps(settings, ensure_ascii=False), driver_id))
    _DB.commit()


# ───────────────────────────── Courses ─────────────────────────────
def record_course(*, driver_id: int, kind=None, group_id=None, group_name=None, msg_id=None,
                  alert_id=None, pickup=None, dropoff=None, price=None, km=None,
                  eur_km=None, duration_min=None, eta_min=None, commission=None,
                  commission_rate=None, net=None, reply=None, raw=None,
                  status="proposée") -> int:
    cur = _DB.execute(
        """INSERT INTO courses(driver_id,ts,created,kind,group_id,group_name,msg_id,alert_id,
             pickup,dropoff,price,km,eur_km,duration_min,eta_min,commission,
             commission_rate,net,reply,status,raw)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (driver_id, time.time(), dt.datetime.now().isoformat(timespec="seconds"), kind,
         group_id, group_name, msg_id, alert_id, pickup, dropoff, price, km, eur_km,
         duration_min, eta_min, commission, commission_rate, net, reply, status, raw))
    _DB.commit()
    return cur.lastrowid


# Les accesseurs par id acceptent un driver_id : fourni, la ligne doit lui appartenir.
# Les callbacks du bot le fournissent toujours, pour qu'un id deviné ne traverse pas.
_OWN = "(? IS NULL OR driver_id=?)"


def get(cid: int, driver_id: "int | None" = None):
    return _DB.execute(f"SELECT * FROM courses WHERE id=? AND {_OWN}",
                       (cid, driver_id, driver_id)).fetchone()


def last_course(driver_id: int):
    return _DB.execute("SELECT * FROM courses WHERE driver_id=? ORDER BY id DESC LIMIT 1",
                       (driver_id,)).fetchone()


def last_realized_course(driver_id: int):
    """Dernière course RÉELLE (obtenue/effectuée/réservée) — pas une alerte proposée/passée.
    C'est celle que /prix et /comm doivent viser par défaut."""
    return _DB.execute(
        "SELECT * FROM courses WHERE driver_id=? AND status IN ('obtenue','effectuée','réservée') "
        "ORDER BY id DESC LIMIT 1", (driver_id,)).fetchone()


def set_status(cid: int, status: str, driver_id: "int | None" = None) -> None:
    _DB.execute(f"UPDATE courses SET status=? WHERE id=? AND {_OWN}",
                (status, cid, driver_id, driver_id))
    _DB.commit()


def set_reply(cid: int, reply: str, driver_id: "int | None" = None) -> None:
    _DB.execute(f"UPDATE courses SET reply=? WHERE id=? AND {_OWN}",
                (reply, cid, driver_id, driver_id))
    _DB.commit()


def find_by_alert(alert_id: int, driver_id: int):
    """Course associée à un message d'alerte DM (pour annoter en répondant à la carte).
    Les ids de messages du bot se répètent d'un chauffeur à l'autre : driver_id obligatoire."""
    return _DB.execute(
        "SELECT * FROM courses WHERE driver_id=? AND alert_id=? ORDER BY id DESC LIMIT 1",
        (driver_id, alert_id)).fetchone()


def set_payment(cid: int, payment: str, driver_id: "int | None" = None) -> None:
    _DB.execute(f"UPDATE courses SET payment=? WHERE id=? AND {_OWN}",
                (payment, cid, driver_id, driver_id))
    _DB.commit()


def append_note(cid: int, note: str, driver_id: "int | None" = None):
    """Ajoute une note (paiement, détail client…) — cumulée avec les précédentes."""
    row = get(cid, driver_id)
    if not row:
        return None
    combined = (row["note"] + " · " + note) if row["note"] else note
    _DB.execute("UPDATE courses SET note=? WHERE id=?", (combined, cid))
    _DB.commit()
    return get(cid, driver_id)


def adjust_amount(cid: int, price=None, commission=None, rate=None,
                  driver_id: "int | None" = None):
    """Corrige prix et/ou commission d'une course et recalcule le net."""
    row = get(cid, driver_id)
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
    return get(cid, driver_id)


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


def summary(driver_id: int, period: str = "jour") -> dict:
    b = _bound(period)
    counts = {s: 0 for s in STATUSES}
    for r in _DB.execute(
            "SELECT status, COUNT(*) n FROM courses WHERE driver_id=? AND ts>=? GROUP BY status",
            (driver_id, b)):
        if r["status"] in counts:
            counts[r["status"]] = r["n"]
    done = _DB.execute(
        f"""SELECT COUNT(*) n, COALESCE(SUM(price),0) ca, COALESCE(SUM(commission),0) comm,
                   COALESCE(SUM(net),0) net, COALESCE(SUM(km),0) km,
                   COALESCE(SUM(duration_min),0) dur
            FROM courses WHERE driver_id=? AND ts>=? AND status IN {_REAL_SQL}""",
        (driver_id, b)).fetchone()
    ek = _DB.execute(
        f"SELECT AVG(eur_km) e FROM courses WHERE driver_id=? AND ts>=? "
        f"AND status IN {_REAL_SQL} AND eur_km IS NOT NULL", (driver_id, b)).fetchone()
    by_group = _DB.execute(
        f"""SELECT COALESCE(group_name,'?') g, COUNT(*) n, COALESCE(SUM(price),0) ca
            FROM courses WHERE driver_id=? AND ts>=? AND status IN {_REAL_SQL}
            GROUP BY group_name ORDER BY ca DESC""", (driver_id, b)).fetchall()
    pays = _DB.execute(
        f"""SELECT payment p, COUNT(*) n, COALESCE(SUM(price),0) ca
            FROM courses WHERE driver_id=? AND ts>=? AND status IN {_REAL_SQL}
            AND payment IS NOT NULL GROUP BY payment ORDER BY ca DESC""",
        (driver_id, b)).fetchall()
    # réservations À VENIR (validées, pas encore effectuées) — indépendant de la période
    up = _DB.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(price),0) ca FROM courses "
        "WHERE driver_id=? AND status='réservée'", (driver_id,)).fetchone()
    frais = expenses_total(driver_id, period)
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


def list_done(driver_id: int, period: str = "jour", limit: int = 25):
    b = _bound(period)
    return _DB.execute(
        f"SELECT * FROM courses WHERE driver_id=? AND ts>=? AND status IN {_REAL_SQL} "
        f"ORDER BY ts LIMIT ?", (driver_id, b, limit)).fetchall()


def courses_detail(driver_id: int, period: str, statuses=("obtenue", "effectuée", "réservée")):
    """Toutes les courses (réalisées + à venir) d'une période, pour le tableau détaillé."""
    b = _bound(period)
    ph = ",".join("?" * len(statuses))
    return _DB.execute(
        f"SELECT * FROM courses WHERE driver_id=? AND ts>=? AND status IN ({ph}) ORDER BY ts",
        (driver_id, b, *statuses)).fetchall()


def pending_reservations(driver_id: int, limit: int = 20):
    """Réservations validées (à venir), les plus proches d'abord (par heure prévue)."""
    return _DB.execute(
        "SELECT * FROM courses WHERE driver_id=? AND status='réservée' "
        "ORDER BY COALESCE(sched_ts, ts) LIMIT ?", (driver_id, limit)).fetchall()


def set_sched(cid: int, sched_ts: "float | None", driver_id: "int | None" = None) -> None:
    _DB.execute(f"UPDATE courses SET sched_ts=? WHERE id=? AND {_OWN}",
                (sched_ts, cid, driver_id, driver_id))
    _DB.commit()


def set_prompted(cid: int, driver_id: "int | None" = None) -> None:
    _DB.execute(f"UPDATE courses SET prompted=1 WHERE id=? AND {_OWN}",
                (cid, driver_id, driver_id))
    _DB.commit()


def add_expense(driver_id: int, amount: float, label: str) -> None:
    _DB.execute("INSERT INTO expenses(driver_id,ts,created,amount,label) VALUES(?,?,?,?,?)",
                (driver_id, time.time(), dt.datetime.now().isoformat(timespec="seconds"),
                 amount, label))
    _DB.commit()


def expenses_total(driver_id: int, period: str) -> float:
    b = _bound(period)
    r = _DB.execute("SELECT COALESCE(SUM(amount),0) s FROM expenses WHERE driver_id=? AND ts>=?",
                    (driver_id, b)).fetchone()
    return r["s"]


def list_expenses(driver_id: int, period: str, limit: int = 12):
    b = _bound(period)
    return _DB.execute("SELECT * FROM expenses WHERE driver_id=? AND ts>=? ORDER BY ts LIMIT ?",
                       (driver_id, b, limit)).fetchall()


def last_expense(driver_id: int):
    return _DB.execute("SELECT * FROM expenses WHERE driver_id=? ORDER BY id DESC LIMIT 1",
                       (driver_id,)).fetchone()


def del_expense(eid: int, driver_id: int) -> None:
    _DB.execute("DELETE FROM expenses WHERE id=? AND driver_id=?", (eid, driver_id))
    _DB.commit()


def set_setting(driver_id: int, k: str, v) -> None:
    """Petit état par chauffeur (objectif du jour, dernier bilan envoyé…). Les réglages
    d'alerte, eux, vivent dans drivers.settings (voir drivers.py)."""
    _DB.execute("INSERT INTO kv(driver_id,k,v) VALUES(?,?,?) "
                "ON CONFLICT(driver_id,k) DO UPDATE SET v=excluded.v", (driver_id, k, str(v)))
    _DB.commit()


def get_setting(driver_id: int, k: str, default=None):
    r = _DB.execute("SELECT v FROM kv WHERE driver_id=? AND k=?", (driver_id, k)).fetchone()
    return r["v"] if r else default


def due_reservations(now_ts: float):
    """Réservations validées dont l'heure est passée et pour lesquelles on n'a pas encore
    demandé « effectuée ? » (rappel post-course). Tous chauffeurs : chaque ligne porte
    son driver_id."""
    return _DB.execute(
        "SELECT * FROM courses WHERE status='réservée' AND COALESCE(prompted,0)=0 "
        "AND sched_ts IS NOT NULL AND sched_ts<=? AND driver_id IS NOT NULL "
        "ORDER BY sched_ts", (now_ts,)).fetchall()


def recent(driver_id: int, limit: int = 8, statuses: "tuple | None" = None):
    """Dernières courses (toutes ou filtrées par statut), la plus récente d'abord."""
    if statuses:
        ph = ",".join("?" * len(statuses))
        return _DB.execute(
            f"SELECT * FROM courses WHERE driver_id=? AND status IN ({ph}) "
            f"ORDER BY id DESC LIMIT ?", (driver_id, *statuses, limit)).fetchall()
    return _DB.execute("SELECT * FROM courses WHERE driver_id=? ORDER BY id DESC LIMIT ?",
                       (driver_id, limit)).fetchall()

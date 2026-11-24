"""Rider reports ("I got a seat" / "standing" / "packed") checked against the model.

Every report keeps what the model predicted for that route, stretch, hour and day type,
so the agreement table needs no recomputation. Phone numbers are stored only as a salted
hash, never in clear.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from .model import LEVELS

ROOT = Path(__file__).resolve().parents[1]
_lock = threading.Lock()
REPORTS = {"seats": "💺 Got a seat", "standing": "🧍 Stood, OK", "crowded": "👥 Crowded", "packed": "🚫 Packed"}


def _path() -> Path:
    p = Path(os.environ.get("FEEDBACK_DB", ROOT / "data" / "feedback.db"))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch(exist_ok=True)
    except OSError:  # read-only deploy: keep it in /tmp
        p = Path("/tmp/bheedbus-feedback.db")
    return p


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(_path())
    con.execute("""create table if not exists reports (
        ts real, channel text, user text, route text, hour int, daytype text, from_stop text, to_stop text,
        predicted text, predicted_seat int, reported text)""")
    con.execute("create table if not exists last_trip (user text primary key, ts real, trip text)")
    return con


def _hash(user: str) -> str:
    salt = os.environ.get("WHATSAPP_VERIFY_TOKEN", "bheedbus")
    return hashlib.sha256((salt + user).encode()).hexdigest()[:16] if user else ""


def record(channel: str, user: str, trip: dict, reported: str) -> dict:
    """trip: {"route", "hour", "daytype", "from", "to", "predicted", "seat_pct"} (as stored when the plan was shown)."""
    if reported not in REPORTS:
        raise ValueError("reported must be one of " + ", ".join(REPORTS))
    with _lock, _con() as con:
        con.execute("insert into reports values (?,?,?,?,?,?,?,?,?,?,?)",
                    (time.time(), channel, _hash(user), trip.get("route"), int(trip.get("hour", 0)),
                     trip.get("daytype", "weekday"), trip.get("from"), trip.get("to"),
                     trip.get("predicted"), int(trip.get("seat_pct") or 0), reported))
    return summary()


def summary() -> dict:
    with _lock, _con() as con:
        rows = con.execute("select predicted, reported, predicted_seat, channel from reports").fetchall()
    n = len(rows)
    conf = {p: {r: 0 for r in LEVELS} for p in LEVELS}
    exact = within = 0
    seat_said, seat_got = [], []
    for p, r, sp, _ in rows:
        if p in conf and r in conf[p]:
            conf[p][r] += 1
            d = abs(LEVELS.index(p) - LEVELS.index(r))
            exact += d == 0
            within += d <= 1
        seat_said.append(sp or 0)
        seat_got.append(100 if r == "seats" else 0)
    return {"n": n, "exact_pct": round(100 * exact / n) if n else None,
            "within_one_pct": round(100 * within / n) if n else None,
            "predicted_seat_pct": round(sum(seat_said) / n) if n else None,
            "reported_seat_pct": round(sum(seat_got) / n) if n else None,
            "confusion": conf, "labels": REPORTS,
            "channels": {c: sum(1 for *_, ch in rows if ch == c) for c in {r[3] for r in rows}}}


def trip_of(option: dict, daytype: str) -> dict:
    """What to remember about a shown plan so a later report can be scored: its first leg."""
    l = option["legs"][0]
    return {"route": l["route"], "hour": int(l["board_min"] // 60) % 24, "daytype": daytype,
            "from": l["from"], "to": l["to"], "predicted": l["worst_level"], "seat_pct": l["seat_board_pct"],
            "ride_min": round(option["duration_min"])}


# The last plan shown to each user, waiting for a report. Kept on disk so a server restart,
# a timeout or going back to the menu doesn't lose it.
def remember(user: str, trip: dict) -> None:
    with _lock, _con() as con:
        con.execute("insert or replace into last_trip values (?,?,?)", (_hash(user), time.time(), json.dumps(trip)))


def last_trip(user: str, max_age_h: float = 24) -> dict | None:
    with _lock, _con() as con:
        row = con.execute("select ts, trip from last_trip where user = ?", (_hash(user),)).fetchone()
    return json.loads(row[1]) if row and time.time() - row[0] < max_age_h * 3600 else None


def forget(user: str) -> None:
    with _lock, _con() as con:
        con.execute("delete from last_trip where user = ?", (_hash(user),))

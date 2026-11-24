"""Optional static GTFS support.

When a GTFS zip is present the pipeline uses it for
  * scheduled departures per hour per route (replaces the Chao1 fleet estimate)
  * stop coordinates (map view), matched to ticket stop names
"""
from __future__ import annotations

import difflib
import re
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd


def _key(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(bus stop|stop|terminal|depot)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load(path: Path) -> dict[str, pd.DataFrame] | None:
    if not path.exists():
        return None
    out = {}
    with zipfile.ZipFile(path) as z:
        names = {Path(n).name: n for n in z.namelist()}
        for t in ("stops", "routes", "trips", "stop_times"):
            if f"{t}.txt" in names:
                with z.open(names[f"{t}.txt"]) as f:
                    out[t] = pd.read_csv(f, dtype=str, low_memory=False)
    return out if {"stops", "routes", "trips", "stop_times"} <= out.keys() else None


def departures_per_hour(g: dict) -> pd.DataFrame:
    """Weekday departures per hour per (route_short_name, direction_id), averaged over 5-23h."""
    st = g["stop_times"]
    first = st.assign(seq=pd.to_numeric(st["stop_sequence"])).sort_values("seq").drop_duplicates("trip_id")
    first["hour"] = first["departure_time"].str.split(":").str[0].astype(int) % 24
    trips = g["trips"].merge(g["routes"][["route_id", "route_short_name"]], on="route_id")
    if "direction_id" not in trips:
        trips["direction_id"] = "0"
    d = first.merge(trips[["trip_id", "route_short_name", "direction_id"]], on="trip_id")
    d = d[d.hour.between(5, 22)]
    f = d.groupby(["route_short_name", "direction_id"]).size() / 18.0
    return f.rename("gtfs_dep_per_hour").reset_index()


def apply_frequencies(meta: pd.DataFrame, freq: pd.DataFrame) -> pd.DataFrame:
    # ticket routes look like "8A" + UP/DOWN; GTFS direction 0 -> UP, 1 -> DOWN by convention
    f = freq.assign(dir=np.where(freq.direction_id.astype(str) == "1", "DOWN", "UP"),
                    base=freq.route_short_name.str.upper().str.replace(r"\s+", "", regex=True))
    m = meta.merge(f[["base", "dir", "gtfs_dep_per_hour"]], on=["base", "dir"], how="left")
    has = m["gtfs_dep_per_hour"].notna()
    m.loc[has, "dep_per_hour"] = m.loc[has, "gtfs_dep_per_hour"]
    m.loc[has, "deph_source"] = "gtfs"
    return m.drop(columns="gtfs_dep_per_hour")


def match_stop_coords(g: dict, names) -> pd.DataFrame:
    s = g["stops"].dropna(subset=["stop_name"]).copy()
    s["k"] = s["stop_name"].map(_key)
    s["lat"] = pd.to_numeric(s["stop_lat"])
    s["lon"] = pd.to_numeric(s["stop_lon"])
    byk = s.groupby("k")[["lat", "lon"]].mean()
    keys = list(byk.index)
    rows = []
    for n in names:
        k = _key(n)
        hit, score = (k, 1.0) if k in byk.index else (None, 0.0)
        if hit is None:
            c = difflib.get_close_matches(k, keys, n=1, cutoff=0.82)
            if c:
                hit, score = c[0], difflib.SequenceMatcher(None, k, c[0]).ratio()
        if hit is not None:
            rows.append({"stop": n, "lat": byk.at[hit, "lat"], "lon": byk.at[hit, "lon"], "match": round(score, 3)})
    return pd.DataFrame(rows)

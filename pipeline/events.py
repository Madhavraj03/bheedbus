"""Two detectors that read straight off the tickets, with no outside information.

1. Unusual days and places. Each day is compared with the median of the same weekday
   on the other, normal days of the month. Days whose ridership moves far beyond the
   usual spread are flagged; then each flagged or normal day is scanned for stops whose
   boardings jump far above their own same-weekday baseline. The holiday list in
   config.json is NOT used here: known events are only looked up afterwards, to check
   what the detector found.

2. Bus bunching. When two different buses of the same route issue tickets at the same
   stop a few minutes apart, they were travelling together. We count such pairs by the
   gap between them and compare short gaps (0-3 min) with long ones (10-40 min), where
   pairs are spread evenly. That ratio is the pair-correlation of bus arrivals:
     0   = perfectly even spacing (a regular timetable never puts two buses together),
     1   = buses arrive as if at random,
     >1  = buses travel in packs.
   Digital tickets only reveal a bus at a stop if someone bought a ticket there. That
   thinning removes pairs at random and does not change the ratio, which is why it
   works on a ~10-tickets-per-bus sample.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

NEAR, FAR = (0, 3), (10, 40)


def _baseline(piv: pd.DataFrame, dows: pd.Series, normal: set) -> pd.DataFrame:
    """Expected value for every column: median of the same weekday on other normal days."""
    out = {}
    for d in piv.columns:
        same = [c for c in piv.columns if dows[c] == dows[d] and c != d and c in normal]
        out[d] = piv[same].median(axis=1) if same else piv.median(axis=1)
    return pd.DataFrame(out)


def unusual_days(df: pd.DataFrame, cfg: dict) -> dict[str, pd.DataFrame]:
    ec = cfg.get("events", {})
    day = df.groupby("date")["pax"].sum()
    dows = pd.Series([d.dayofweek for d in day.index], index=day.index)
    normal = set(day.index)
    for _ in range(3):  # re-fit the baseline without the days already flagged
        exp = _baseline(day.to_frame().T, dows, normal).T.iloc[:, 0]
        lr = np.log(day / exp)
        mad = 1.4826 * np.median(np.abs(lr[list(normal)] - np.median(lr[list(normal)])))
        z = lr / max(mad, 0.02)
        flag = (z.abs() >= ec.get("min_z", 3.0)) & ((day / exp - 1).abs() >= ec.get("min_change", 0.12))
        new = set(day.index[~flag])
        if new == normal:
            break
        normal = new
    known = {pd.Timestamp(k): v for k, v in ec.get("known_events", {}).items()}
    days = pd.DataFrame({"date": day.index.strftime("%Y-%m-%d"), "dow": day.index.day_name().str[:3],
                         "observed": day.to_numpy(), "expected": exp.round().to_numpy(),
                         "change_pct": (100 * (day / exp - 1)).round(1).to_numpy(), "z": z.round(1).to_numpy(),
                         # 2 = major (used to re-fit the baseline), 1 = notable, 0 = normal
                         "flagged": np.where(flag, 2, np.where((z.abs() >= ec.get("notable_z", 2.5))
                                             & ((day / exp - 1).abs() >= ec.get("notable_change", 0.15)), 1, 0)),
                         "known_event": [known.get(d, "") for d in day.index]})

    # hour-by-hour shape of every flagged day vs its baseline
    dh = df.groupby(["date", "hour"])["pax"].sum().unstack(fill_value=0).T
    base = _baseline(dh, dows, normal)
    hours = []
    for d in day.index[days["flagged"].to_numpy() > 0]:
        for h in range(5, 24):
            e = float(base.at[h, d]) if h in base.index else 0.0
            o = float(dh.at[h, d]) if h in dh.index else 0.0
            hours.append({"date": d.strftime("%Y-%m-%d"), "hour": h, "observed": o, "expected": round(e, 1)})

    # stops that surge far above their own same-weekday baseline
    sd = df.groupby(["start", "date"])["pax"].sum().unstack(fill_value=0)
    sbase = _baseline(sd, dows, normal)
    min_exp, min_ratio, min_zs = ec.get("stop_min_expected", 3), ec.get("stop_min_ratio", 2.5), ec.get("stop_min_z", 6.0)
    rows = []
    for d in sd.columns:
        o, e = sd[d], sbase[d]
        ratio = (o + 5) / (e + 5)
        zz = (o - e) / np.sqrt(e + 1)
        m = (e >= min_exp) & (ratio >= min_ratio) & (zz >= min_zs)
        for s in sd.index[m]:
            rows.append({"date": d.strftime("%Y-%m-%d"), "stop": s, "observed": int(o[s]),
                         "expected": float(e[s]), "ratio": round(float(ratio[s]), 2), "z": round(float(zz[s]), 1)})
    stops = pd.DataFrame(rows, columns=["date", "stop", "observed", "expected", "ratio", "z"])
    # a stop that 'surges' on many days is a renamed or new stop, not an event
    rep = stops.groupby("stop")["date"].transform("size")
    stops = stops[rep <= ec.get("stop_max_days", 2)].sort_values(["date", "z"], ascending=[True, False])
    return {"event_days": days, "event_hours": pd.DataFrame(hours), "event_stops": stops}


def bunching(df: pd.DataFrame, meta: pd.DataFrame, cfg: dict) -> dict[str, pd.DataFrame]:
    bc = cfg.get("bunching", {})
    v = df.dropna(subset=["vehicle"])
    # skip each route's first and last stop, where buses wait together at the terminal
    last = v["route"].map(meta.set_index("route")["n_stops"]) - 1
    v = v[(v["pos_s"] > 0) & (v["pos_s"] < last)]
    v = v.assign(m=(v["t"] - v["date"]).dt.total_seconds() / 60)
    v = v.assign(blk=(v["m"] // 15).astype(int))
    # one visit per bus, stop and 15 minutes: the first ticket it issued there
    vis = (v.groupby(["route", "start", "date", "vehicle", "blk"], observed=True)
            .agg(m=("m", "min"), hour=("hour", "first"), daytype=("daytype", "first")).reset_index()
            .sort_values(["route", "start", "date", "m"]))
    k = pd.factorize(vis["route"] + "|" + vis["start"] + "|" + vis["date"].astype(str))[0]
    mm, veh = vis["m"].to_numpy(), pd.factorize(vis["vehicle"])[0]
    route, hour, dt = vis["route"].to_numpy(), vis["hour"].to_numpy(), vis["daytype"].to_numpy()
    gaps, rr, hh, dd = [], [], [], []
    n = len(vis)
    for lag in range(1, bc.get("max_lag", 30)):
        a = np.arange(n - lag)
        b = a + lag
        gap = mm[b] - mm[a]
        ok = (k[a] == k[b]) & (veh[a] != veh[b]) & (gap < FAR[1])
        gaps.append(gap[ok]); rr.append(route[a][ok]); hh.append(hour[a][ok]); dd.append(dt[a][ok])
    p = pd.DataFrame({"gap": np.concatenate(gaps), "route": np.concatenate(rr),
                      "hour": np.concatenate(hh), "daytype": np.concatenate(dd)})
    p["near"] = p["gap"] < NEAR[1]
    p["far"] = (p["gap"] >= FAR[0]) & (p["gap"] < FAR[1])
    span = (NEAR[1] - NEAR[0]) / (FAR[1] - FAR[0])

    curve = np.histogram(p["gap"], bins=np.arange(0, FAR[1] + 1))[0]
    curve = pd.DataFrame({"gap_min": np.arange(FAR[1]), "pairs": curve,
                          "g": (curve / curve[FAR[0]:FAR[1]].mean()).round(3)})

    def index(g: pd.DataFrame, by: list[str]) -> pd.DataFrame:
        s = g.groupby(by, observed=True).agg(near=("near", "sum"), far=("far", "sum")).reset_index()
        s["g_raw"] = s["near"] / (s["far"] * span).replace(0, np.nan)
        return s

    city = index(p, ["daytype", "hour"])
    city_g = float(p["near"].sum() / (p["far"].sum() * span))
    # shrink each route-hour toward the city value for that hour (k pseudo-pairs)
    kk = bc.get("shrink_pairs", 20)
    rh = index(p, ["route", "daytype", "hour"]).merge(
        city[["daytype", "hour", "g_raw"]].rename(columns={"g_raw": "g_city"}), on=["daytype", "hour"], how="left")
    rh["g_city"] = rh["g_city"].fillna(city_g)
    rh["g"] = ((rh["near"] + kk * span * rh["g_city"]) / ((rh["far"] + kk) * span)).round(3)
    rt = index(p, ["route"])
    rt["g"] = ((rt["near"] + kk * span * city_g) / ((rt["far"] + kk) * span)).round(3)
    return {"bunching": rh[["route", "daytype", "hour", "near", "far", "g"]],
            "bunching_route": rt[["route", "near", "far", "g"]],
            "val_bunching_city": city.assign(g=city["g_raw"].round(3))[["daytype", "hour", "near", "far", "g"]],
            "val_bunching_curve": curve, "_city_g": city_g}

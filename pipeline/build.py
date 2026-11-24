"""BheedBus crowding pipeline.

Raw ticket CSV  ->  SQLite database of crowding estimates by
route, direction, stretch between stops, day type and hour.

    python pipeline/build.py [--config config.json] [--csv other.csv]

Steps
  1. load + clean tickets
  2. infer the stop order of every route-direction from ticket pairs
  3. estimate running time (min per stop) from vehicle-tagged tickets
  4. estimate buses per hour (Chao1 fleet estimate, or GTFS if supplied)
  5. turn tickets into on-board passenger flow per stretch per hour
  6. expand the sample, divide by buses -> load per bus -> crowding level
  7. confidence (sample size + week-to-week stability) and validation
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline import gtfs as gtfs_mod  # noqa: E402

DIR_RE = re.compile(r"^(?P<base>.*?)(?P<dir>UP|DOWN|\(\+\)|\(-\)|PLUS|MINUS)(?P<var>\d*)$")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def norm_stop(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.replace(r"\s+", " ", regex=True)


# --------------------------------------------------------------------------- 1
def load_tickets(path: Path, holidays: list[str]) -> pd.DataFrame:
    log(f"reading {path}")
    df = pd.read_csv(path, low_memory=False,
                     usecols=["passenger_count", "amount", "vehicle_number", "end_stop_name",
                              "route", "start_stop_name", "created_at", "is_ac"])
    n0 = len(df)
    df["t"] = pd.to_datetime(df["created_at"].str[:19], errors="coerce")
    df = df.dropna(subset=["t", "route", "start_stop_name", "end_stop_name"])
    df["start"] = norm_stop(df["start_stop_name"])
    df["end"] = norm_stop(df["end_stop_name"])
    df = df[df["start"] != df["end"]]
    df["route"] = df["route"].astype(str).str.strip()
    df["pax"] = df["passenger_count"].clip(1, 6).astype(np.int16)
    df["vehicle"] = df["vehicle_number"].where(df["vehicle_number"].notna() & (df["vehicle_number"] != "NULL"))

    m = df["route"].str.extract(DIR_RE)
    df["base"] = m["base"].fillna(df["route"]) + m["var"].fillna("")
    df["dir"] = m["dir"].fillna("ONE").replace({"(+)": "UP", "PLUS": "UP", "(-)": "DOWN", "MINUS": "DOWN"})
    df["date"] = df["t"].dt.normalize()
    hol = pd.to_datetime(holidays)
    dow = df["t"].dt.dayofweek
    df["daytype"] = np.where((dow >= 5) | df["date"].isin(hol), "weekend", "weekday")
    df["week"] = ((df["date"] - df["date"].min()).dt.days // 7).astype(np.int8)
    df["hour"] = df["t"].dt.hour.astype(np.int8)
    df = df.drop(columns=["created_at", "start_stop_name", "end_stop_name", "vehicle_number", "passenger_count"])
    log(f"  {len(df):,} usable tickets of {n0:,}")
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- 2
def order_stops(W: np.ndarray, start_share: np.ndarray, passes: int = 4) -> np.ndarray:
    """Find a linear order of stops that agrees with as many tickets as possible.

    W[a, b] = tickets boarding at a and alighting at b (so a should come before b).
    Initial order: stops sorted by the share of their tickets that board there
    (origins sit early, destinations late). Then a Kemeny-style insertion local
    search moves each stop to the slot that maximises agreeing ticket weight.
    """
    n = len(W)
    order = list(np.argsort(-start_share, kind="stable"))
    D = W - W.T  # D[a,b] > 0 => evidence a before b
    for _ in range(passes):
        moved = False
        for i in list(order):
            rest = [s for s in order if s != i]
            # gain of placing i at slot p = sum_{j in rest[:p]} D[j,i] + sum_{j in rest[p:]} D[i,j]
            before = np.concatenate([[0], np.cumsum(D[rest, i])])
            after = np.concatenate([np.cumsum(D[i, rest][::-1])[::-1], [0]])
            p = int(np.argmax(before + after))
            new = rest[:p] + [i] + rest[p:]
            if new != order:
                order, moved = new, True
        if not moved:
            break
    return np.array(order)


def infer_stop_orders(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    log("inferring stop order per route-direction")
    pairs = df.groupby(["route", "base", "dir", "start", "end"], observed=True)["pax"].size().rename("n").reset_index()
    by_route = {r: g for r, g in pairs.groupby("route")}
    # reverse direction of the same base route adds evidence (UP order = reversed DOWN order)
    partner = {}
    for (base, d), g in pairs.groupby(["base", "dir"]):
        partner[(base, d)] = g["route"].iloc[0]

    rows, meta = [], []
    for route, g in by_route.items():
        base, d = g["base"].iloc[0], g["dir"].iloc[0]
        opp = {"UP": "DOWN", "DOWN": "UP"}.get(d)
        stops = pd.Index(sorted(set(g["start"]) | set(g["end"])))
        n = len(stops)
        if n < 2:
            continue
        W = np.zeros((n, n))
        W[stops.get_indexer(g["start"]), stops.get_indexer(g["end"])] += g["n"].to_numpy()
        own = W.copy()
        if opp and (base, opp) in partner:
            o = by_route[partner[(base, opp)]]
            si, ei = stops.get_indexer(o["end"]), stops.get_indexer(o["start"])
            ok = (si >= 0) & (ei >= 0)
            np.add.at(W, (si[ok], ei[ok]), 0.5 * o["n"].to_numpy()[ok])
        out_, in_ = W.sum(1), W.sum(0)
        start_share = (out_ + 0.5) / (out_ + in_ + 1.0)
        order = order_stops(W, start_share)
        pos = np.empty(n, dtype=int)
        pos[order] = np.arange(n)
        agree = own[pos[:, None] < pos[None, :]].sum()
        meta.append({"route": route, "base": base, "dir": d, "n_stops": n,
                     "tickets": int(own.sum()), "order_agreement": float(agree / max(own.sum(), 1))})
        for k, si in enumerate(order):
            rows.append({"route": route, "pos": k, "stop": stops[si],
                         "boardings": int(own[si].sum()), "alightings": int(own[:, si].sum())})
    stops_df = pd.DataFrame(rows)
    meta_df = pd.DataFrame(meta)
    log(f"  {len(meta_df):,} route-directions, ticket-weighted order agreement "
        f"{np.average(meta_df.order_agreement, weights=meta_df.tickets):.1%}")
    return stops_df, meta_df


# --------------------------------------------------------------------------- 3
def travel_times(df: pd.DataFrame, cfg: dict) -> tuple[pd.Series, pd.DataFrame, float]:
    """Minutes per stop from consecutive tickets on the same vehicle and trip."""
    log("estimating running time from vehicle-tagged tickets")
    tc = cfg["travel_time"]
    v = df.dropna(subset=["vehicle"]).sort_values(["vehicle", "t"])
    same = (v["vehicle"].eq(v["vehicle"].shift())) & (v["route"].eq(v["route"].shift()))
    dt = (v["t"] - v["t"].shift()).dt.total_seconds() / 60
    dpos = v["pos_s"] - v["pos_s"].shift()
    ok = same & (dt > 0.5) & (dt <= tc["max_gap_min"]) & (dpos >= 2)
    p = pd.DataFrame({"route": v["route"][ok], "hour": v["hour"][ok],
                      "mps": (dt[ok] / dpos[ok])}).query("0.6 <= mps <= 6")
    city_hour = p.groupby("hour")["mps"].median()
    overall = p["mps"].median()
    cong = (city_hour / overall).reindex(range(24)).fillna(1.0).clip(0.7, 1.5)
    g = p.groupby("route")["mps"].agg(["median", "size"])
    log(f"  {len(p):,} usable pairs, city median {overall:.2f} min/stop")
    return cong, pd.DataFrame({"mps_raw": g["median"], "mps_pairs": g["size"]}), overall


def shrink_trip_time(meta: pd.DataFrame, city_mps: float, k: int) -> pd.Series:
    """Minutes per observed stop, shrunk toward a typical whole-trip time.

    Stops where nobody bought a digital ticket are missing from a route, so on a
    sparse route one 'position' can hide several real stops. Shrinking the trip
    time (not the per-stop time) toward the median of well-observed routes keeps
    sparse routes from looking implausibly fast.
    """
    raw = meta["mps_raw"].fillna(city_mps)
    well = meta["mps_pairs"] >= 200
    prior_trip = float((raw[well] * meta.loc[well, "n_stops"]).median())
    trip = (raw * meta["n_stops"] * meta["mps_pairs"] + prior_trip * k) / (meta["mps_pairs"] + k)
    log(f"  prior trip time {prior_trip:.0f} min (median of {well.sum()} well-observed routes)")
    return (trip / meta["n_stops"].clip(lower=1)).clip(0.8, 20)


# --------------------------------------------------------------------------- 4
def chao1(counts: np.ndarray) -> float:
    s = len(counts)
    f1, f2 = (counts == 1).sum(), (counts == 2).sum()
    if f2 > 0:
        return s + f1 * f1 / (2 * f2)
    return s + f1 * (f1 - 1) / 2


def fleet_estimate(df: pd.DataFrame) -> pd.DataFrame:
    """Buses working each base route on a typical weekday (both directions share the fleet)."""
    log("estimating fleet per route (Chao1)")
    v = df.dropna(subset=["vehicle"])
    v = v[v["daytype"] == "weekday"]
    # a bus counts toward a route only if that route is >= 25% of its tickets that day;
    # this drops buses that log a stray ticket on a route they do not work
    vb = v.groupby(["date", "vehicle", "base"]).size().rename("n").reset_index()
    vb["share"] = vb["n"] / vb.groupby(["date", "vehicle"])["n"].transform("sum")
    vb = vb[vb["share"] >= 0.25]
    rows = []
    for (base, date), g in vb.groupby(["base", "date"]):
        c = g["n"].to_numpy()
        # a bus split across two routes gives each route only its share of the day
        rows.append({"base": base, "date": date, "seen": len(c),
                     "chao1": min(chao1(c), 4 * len(c) + 2) * g["share"].mean()})
    f = pd.DataFrame(rows).groupby("base").agg(seen=("seen", "median"), fleet=("chao1", "median"),
                                               days=("date", "nunique"))
    return f


# --------------------------------------------------------------------------- 5
def segment_flows(df: pd.DataFrame, route_mps: pd.Series, cong: pd.Series) -> pd.DataFrame:
    """Explode each ticket over the stretches it rides; shift time by running time."""
    log("building on-board flows per stretch")
    ok = df["pos_e"] > df["pos_s"]
    d = df[ok]
    span = (d["pos_e"] - d["pos_s"]).to_numpy()
    idx = np.repeat(np.arange(len(d)), span)
    offs = np.arange(span.sum()) - np.repeat(np.cumsum(span) - span, span)
    seg = d["pos_s"].to_numpy()[idx] + offs
    mps = d["route"].map(route_mps).fillna(route_mps.median()).to_numpy()[idx]
    minute = (d["t"].dt.hour * 60 + d["t"].dt.minute).to_numpy()[idx]
    h0 = d["hour"].to_numpy()[idx]
    minute = minute + offs * mps * cong.to_numpy()[h0]
    hour = ((minute // 60) % 24).astype(np.int8)
    e = pd.DataFrame({"route": d["route"].to_numpy()[idx], "seg": seg.astype(np.int16), "hour": hour,
                      "daytype": d["daytype"].to_numpy()[idx], "week": d["week"].to_numpy()[idx],
                      "day": (d["date"] - d["date"].min()).dt.days.to_numpy().astype(np.int16)[idx],
                      "pax": d["pax"].to_numpy()[idx]})
    log(f"  {len(e):,} ticket-stretch rows")
    return e


def day_variability(e: pd.DataFrame, cell: list[str], days: dict, cfg: dict) -> pd.DataFrame:
    """Day-to-day coefficient of variation of the true load in each cell.

    Observed daily ticket counts vary for two reasons: real changes in demand and
    sampling noise (only a few digital tickets per cell per day). Sampling noise is
    roughly Poisson, so we subtract it (method of moments) and keep the excess.
    Cells with few tickets are shrunk toward the city median CV.
    """
    daily = e.groupby(cell + ["day"], observed=True)["pax"].sum().rename("p").reset_index()
    daily["p2"] = daily["p"].astype(float) ** 2
    g = daily.groupby(cell, observed=True).agg(s1=("p", "sum"), s2=("p2", "sum")).reset_index()
    n = g["daytype"].map(days)
    mean = g["s1"] / n
    var = g["s2"] / n - mean ** 2
    poisson = 1.1 * mean  # tickets carry 1-3 passengers, so slightly over-Poisson
    cv = np.sqrt((var - poisson).clip(lower=0)) / mean
    k = cfg["seat_probability"]["shrink_tickets"]
    city = float(cv[g["s1"] >= 200].median())
    g["day_cv"] = ((cv * g["s1"] + city * k) / (g["s1"] + k)).clip(0.05, 1.0)
    log(f"  day-to-day CV: city median {city:.2f} (well-sampled cells)")
    return g[cell + ["day_cv"]]


# --------------------------------------------------------------------------- 6
def level_of(load: np.ndarray, bus: dict) -> np.ndarray:
    seats, stand, crush = bus["seats"], bus["seats"] + bus["standing_comfort"], bus["crush"]
    return np.select([load <= 0.85 * seats, load <= stand, load <= crush], ["seats", "standing", "crowded"], "packed")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--csv", default=None, help="override ticket CSV path")
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    csv = Path(args.csv) if args.csv else ROOT / cfg["ticket_csv"]
    out = ROOT / cfg["out_db"]
    out.parent.mkdir(parents=True, exist_ok=True)

    df = load_tickets(csv, cfg["holidays"])
    stops_df, meta = infer_stop_orders(df)
    posmap = stops_df.set_index(["route", "stop"])["pos"]
    df["pos_s"] = posmap.reindex(pd.MultiIndex.from_arrays([df["route"], df["start"]])).to_numpy()
    df["pos_e"] = posmap.reindex(pd.MultiIndex.from_arrays([df["route"], df["end"]])).to_numpy()
    df = df.dropna(subset=["pos_s", "pos_e"])
    df[["pos_s", "pos_e"]] = df[["pos_s", "pos_e"]].astype(np.int16)

    cong, rt, city_mps = travel_times(df, cfg)
    meta = meta.merge(rt, left_on="route", right_index=True, how="left")
    meta["mps_pairs"] = meta["mps_pairs"].fillna(0).astype(int)
    meta["mps"] = shrink_trip_time(meta, city_mps, cfg["travel_time"]["min_pairs_for_route"])

    # ---- day counts and expansion factor
    days = df.groupby("daytype")["date"].nunique().to_dict()
    wk_pax = df[df.daytype == "weekday"].groupby("date")["pax"].sum().mean()
    E = cfg["expansion"]["assumed_weekday_boardings"] / wk_pax
    log(f"  weekday digital boardings/day {wk_pax:,.0f} -> expansion factor E = {E:.1f}")

    # ---- buses per hour
    fc = cfg["fleet"]
    fleet = fleet_estimate(df)
    meta = meta.merge(fleet, left_on="base", right_index=True, how="left")
    meta["trip_min"] = meta["n_stops"] * meta["mps"]
    cycle = 2 * meta["trip_min"] + 2 * fc["layover_min"]
    meta["deph_source"] = np.where(meta["fleet"].notna(), "chao1", "fallback")
    meta["fleet"] = meta["fleet"].fillna(meta["fleet"].median())
    meta["dep_per_hour"] = (meta["fleet"] * 60 / cycle).clip(fc["min_departures_per_hour"], fc["max_departures_per_hour"])

    # route-level expansion: partial pooling between one city-wide factor (alpha=0) and
    # equal boardings per bus on every route (alpha=1). Corrects for routes where far more
    # (or fewer) riders use the app than average, without erasing real crowding differences.
    alpha = cfg["expansion"]["adoption_alpha"]
    wkt = df[df.daytype == "weekday"].groupby("base")["pax"].sum() / days["weekday"]
    tpb = (wkt / meta.drop_duplicates("base").set_index("base")["fleet"]).rename("tickets_per_bus")
    meta = meta.merge(tpb, left_on="base", right_index=True, how="left")
    city_tpb = np.average(tpb.dropna(), weights=wkt.reindex(tpb.dropna().index))
    ratio = (city_tpb / meta["tickets_per_bus"]).fillna(1.0).clip(0.33, 3.0)
    meta["expansion"] = E * ratio ** alpha

    g = gtfs_mod.load(ROOT / cfg["gtfs_zip"])
    coords = None
    if g is not None:
        log("GTFS found: using scheduled frequencies and stop coordinates")
        freq = gtfs_mod.departures_per_hour(g)
        meta = gtfs_mod.apply_frequencies(meta, freq)
        coords = gtfs_mod.match_stop_coords(g, stops_df["stop"].unique())
    else:
        log("no GTFS feed: frequencies from Chao1 fleet estimate")

    # ---- bus size per route
    bt = cfg["bus_types"]
    pats = [re.compile(p) for p in bt.get("nine_m_route_patterns", [])]
    meta["bus_type"] = ["9m" if any(p.search(b) for p in pats) else "12m" for b in meta["base"]]

    # ---- flows -> loads
    e = segment_flows(df, meta.set_index("route")["mps"], cong)
    cell = ["route", "seg", "daytype", "hour"]
    agg = e.groupby(cell, observed=True)["pax"].agg(sample_pax="sum", n_tickets="size").reset_index()
    wk = e.groupby(cell + ["week"], observed=True)["pax"].sum().rename("p").reset_index()
    wdays = df.groupby(["week", "daytype"])["date"].nunique().rename("nd").reset_index()
    wk = wk.merge(wdays, on=["week", "daytype"])
    wk["p"] = wk["p"] / wk["nd"]
    full_weeks = wdays[wdays.nd >= 2].groupby("daytype")["week"].apply(set).to_dict()
    wk = wk[[w in full_weeks.get(dt, set()) for w, dt in zip(wk.week, wk.daytype)]]
    # pad missing weeks with zero so CV reflects weeks with no riders
    piv = wk.pivot_table(index=cell, columns="week", values="p", fill_value=0)
    cv = (piv.std(axis=1) / piv.mean(axis=1).replace(0, np.nan)).rename("week_cv")
    agg = agg.merge(cv.reset_index(), on=cell, how="left")
    agg = agg.merge(day_variability(e, cell, days, cfg), on=cell, how="left")

    agg["daily_sample"] = agg["sample_pax"] / agg["daytype"].map(days)
    ml_eval, ml_info = None, {"used": False}
    if cfg.get("ml", {}).get("enabled", True):
        from pipeline import ml as ml_mod
        agg["daily_sample_raw"] = agg["daily_sample"]
        agg["daily_sample"], ml_eval, ml_info = ml_mod.run(e, df, meta, agg)
    m = meta.set_index("route")
    agg["dep_per_hour"] = agg["route"].map(m["dep_per_hour"])
    in_service = agg["hour"].between(fc["service_start_hour"], fc["service_end_hour"] - 1)
    agg.loc[~in_service, "dep_per_hour"] = agg.loc[~in_service, "dep_per_hour"] * 0.5
    agg["dep_per_hour"] = agg["dep_per_hour"].clip(lower=fc["min_departures_per_hour"])
    agg["flow_per_hour"] = agg["daily_sample"] * agg["route"].map(m["expansion"])
    agg["load"] = agg["flow_per_hour"] / agg["dep_per_hour"]
    agg["bus_type"] = agg["route"].map(m["bus_type"])

    # capacity anchor: a bus cannot hold more than its crush load, so the busiest
    # well-sampled bus-hours should sit at (not far above) crush. If the ridership
    # anchor implies more, scale the expansion down. The smaller of the two wins.
    q = cfg["expansion"]["capacity_anchor_quantile"]
    crush = agg["bus_type"].map({k: v["crush"] for k, v in bt.items() if isinstance(v, dict)})
    # only routes whose fleet estimate rests on >= 10 buses seen per day, in service hours
    reliable = agg["route"].map(m["seen"]).fillna(0) >= 10
    well = ((agg.n_tickets >= cfg["confidence"]["high_min_tickets"]) & (agg.daytype == "weekday")
            & reliable & in_service)
    top = (agg.loc[well, "load"] / crush[well]).quantile(q)
    cap_scale = min(1.0, 1.0 / top) if top > 0 else 1.0
    agg["flow_per_hour"] *= cap_scale
    agg["load"] *= cap_scale
    meta["expansion"] *= cap_scale
    log(f"  capacity anchor: p{q*100:g} load/crush = {top:.2f} -> scale {cap_scale:.2f}, "
        f"implied weekday boardings {cfg['expansion']['assumed_weekday_boardings'] * cap_scale:,.0f}")
    agg["level"] = "seats"
    for btype, bus in ((k, v) for k, v in bt.items() if isinstance(v, dict)):
        sel = agg["bus_type"] == btype
        agg.loc[sel, "level"] = level_of(agg.loc[sel, "load"].to_numpy(), bus)
        agg.loc[sel, "occupancy"] = agg.loc[sel, "load"] / (bus["seats"] + bus["standing_comfort"])

    c = cfg["confidence"]
    agg["confidence"] = np.select(
        [(agg.n_tickets >= c["high_min_tickets"]) & (agg.week_cv.fillna(9) <= c["high_max_cv"]),
         agg.n_tickets >= c["medium_min_tickets"]], ["high", "medium"], "low")
    agg.loc[~in_service, "confidence"] = "low"  # night service levels are not observable

    # ---- validation tables
    val = build_validation(df, e, agg, meta, E * cap_scale, days, cfg)

    # ---- unusual days/places and bus bunching (pipeline/events.py)
    from pipeline import events as ev_mod
    log("detecting unusual days and places (holiday list not used)")
    ev = ev_mod.unusual_days(df, cfg)
    fl = ev["event_days"].query("flagged > 0")
    log("  flagged: " + ", ".join(f"{r.date} {r.change_pct:+.0f}%" for r in fl.itertuples()))
    log(f"  {len(ev['event_stops'])} stop surges")
    log("measuring bus bunching from same-stop ticket pairs")
    bn = ev_mod.bunching(df, meta, cfg)
    city_g = bn.pop("_city_g")
    log(f"  city pair-correlation g(0-3 min) = {city_g:.2f} (0 = even spacing, 1 = random)")

    # ---- write
    log(f"writing {out}")
    if out.exists():
        out.unlink()
    con = sqlite3.connect(out)
    stops_df.to_sql("route_stops", con, index=False)
    meta.to_sql("routes", con, index=False)
    cols = ["route", "seg", "daytype", "hour", "n_tickets", "daily_sample"] + \
           (["daily_sample_raw"] if "daily_sample_raw" in agg else []) + ["week_cv", "day_cv", "flow_per_hour",
            "dep_per_hour", "load", "occupancy", "level", "confidence"]
    agg[cols].round(3).to_sql("segment_load", con, index=False)
    pd.DataFrame({"hour": range(24), "factor": cong.to_numpy()}).to_sql("congestion", con, index=False)
    if coords is not None:
        coords.to_sql("stop_coords", con, index=False)
    for k, v in val.items():
        v.to_sql(f"val_{k}", con, index=False)
    if ml_eval is not None:
        ml_eval.to_sql("val_ml", con, index=False)
    for k, v in {**ev, **bn}.items():
        v.to_sql(k, con, index=False)
    info = {"expansion_factor": E * cap_scale, "ridership_anchor_E": E, "capacity_scale": cap_scale,
            "implied_weekday_boardings": cfg["expansion"]["assumed_weekday_boardings"] * cap_scale, "weekday_digital_boardings": wk_pax, "days": days,
            "city_min_per_stop": city_mps, "built_at": time.strftime("%Y-%m-%d %H:%M"),
            "tickets": int(len(df)), "source_csv": csv.name, "gtfs": g is not None, "ml": ml_info,
            "bunching_city_g": city_g, "flagged_days": fl["date"].tolist()}
    pd.DataFrame([{"key": k, "value": json.dumps(v, default=float)} for k, v in info.items()]).to_sql("info", con, index=False)
    con.execute("CREATE INDEX ix_seg ON segment_load(route, daytype, hour)")
    con.execute("CREATE INDEX ix_stops ON route_stops(stop)")
    con.commit()
    con.close()
    log("done")


def build_validation(df, e, agg, meta, E, days, cfg) -> dict[str, pd.DataFrame]:
    log("running validation checks")
    v = {}
    # 1. hourly profile, weekday vs weekend (expect AM + PM peaks on weekdays)
    hp = df.groupby(["daytype", "hour"])["pax"].sum().rename("boardings").reset_index()
    hp["per_day"] = hp["boardings"] / hp["daytype"].map(days)
    v["hourly_profile"] = hp
    # 2. share of bus-hours that are crowded, by hour
    w = agg[(agg.daytype == "weekday") & (agg.confidence != "low")]
    v["crowded_share_by_hour"] = (w.assign(crowded=w.level.isin(["crowded", "packed"]))
                                   .groupby("hour")["crowded"].mean().rename("share_crowded").reset_index())
    # 3. holdout: weeks 0-2 predict week 3 (weekday). correlation of route-hour peak load
    cell = ["route", "seg", "hour"]
    wd = e[e.daytype == "weekday"]
    nd = df[df.daytype == "weekday"].groupby("week")["date"].nunique()
    train = wd[wd.week <= 2].groupby(cell)["pax"].sum() / nd[nd.index <= 2].sum()
    test = wd[wd.week == 3].groupby(cell)["pax"].sum() / nd.get(3, 1)
    j = pd.concat([train.rename("train"), test.rename("test")], axis=1).fillna(0)
    j = j[(j.train + j.test) > 0]
    rp = j.groupby(["route", "hour"]).max()
    dph = meta.set_index("route")["dep_per_hour"]
    bus = cfg["bus_types"]["12m"]
    lv = {}
    for col in ("train", "test"):
        load = rp[col] * E / rp.index.get_level_values("route").map(dph).to_numpy()
        lv[col] = level_of(load.to_numpy(), bus)
    big = (rp.train + rp.test) >= 4
    v["holdout"] = pd.DataFrame([{
        "metric": "pearson_r_route_hour_peak_flow", "value": float(np.corrcoef(rp.train, rp.test)[0, 1])},
        {"metric": "spearman_r_route_hour_peak_flow", "value": float(rp.train.rank().corr(rp.test.rank()))},
        {"metric": "level_agreement_all", "value": float((lv["train"] == lv["test"]).mean())},
        {"metric": "level_agreement_well_sampled", "value": float((lv["train"][big.to_numpy()] == lv["test"][big.to_numpy()]).mean())},
        {"metric": "cells", "value": float(len(rp))}])
    # 4. busiest corridors (stretches with highest weekday peak flow)
    rs = (agg[agg.daytype == "weekday"].sort_values("flow_per_hour", ascending=False)
          .drop_duplicates(["route", "seg"]).head(200))
    v["top_stretches"] = rs[["route", "seg", "hour", "flow_per_hour", "load", "level", "confidence"]]
    # 5. weekday vs weekend totals
    v["daytype_totals"] = (df.groupby("daytype")["pax"].sum() / pd.Series(days)).rename("boardings_per_day").reset_index()
    v["daytype_totals"].columns = ["daytype", "boardings_per_day"]
    return v


if __name__ == "__main__":
    main()

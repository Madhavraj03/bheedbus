"""ML smoothing for sparse stretch-hours, tested on a held-out week.

The statistical estimate for a cell (route, stretch, day type, hour) is the mean
number of digital tickets crossing it per day. On quiet routes that mean rests on
a handful of tickets and is noisy. A gradient-boosted model learns the typical
shape of demand from well-measured cells (how a route's hourly total spreads over
its stretches, time of day, position along the route) and gives a smoother
estimate. The two are blended with weight n / (n + k): cells with many tickets keep
their own data, quiet cells lean on the model.

Honest evaluation (no peeking):
  * training target = week 3 (index 2); features from weeks 1-2 (indices 0-1);
  * test: features from weeks 1-3 predict week 4 (index 3), never seen in training;
  * k for the blend is tuned on the training target, not on the test week;
  * we report error by confidence bucket against the current (raw mean) method.
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

CELL = ["route", "seg", "daytype", "hour"]
FEATURES = ["hour", "weekend", "segfrac", "nseg", "l_route", "l_rh", "l_rs", "l_mult", "l_base", "l_n",
            "l_dep", "l_nb_mean"]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _per_day(e: pd.DataFrame, weeks: list[int], ndays: dict) -> pd.DataFrame:
    """Per-cell passengers per day and ticket count over the chosen weeks."""
    sub = e[e["week"].isin(weeks)]
    g = sub.groupby(CELL, observed=True)["pax"].agg(p="sum", n="size").reset_index()
    g["y"] = g["p"] / g["daytype"].map(ndays)
    return g[CELL + ["y", "n"]]


def _features(cells: pd.DataFrame, obs: pd.DataFrame, meta: pd.DataFrame) -> pd.DataFrame:
    """Features for each cell, computed only from the observation window `obs`."""
    X = cells[CELL].merge(obs, on=CELL, how="left").fillna({"y": 0.0, "n": 0})
    rh = obs.groupby(["route", "daytype", "hour"], observed=True)["y"].sum().rename("rh")
    rs = obs.groupby(["route", "daytype", "seg"], observed=True)["y"].sum().rename("rs")
    r = obs.groupby(["route", "daytype"], observed=True)["y"].sum().rename("rt")
    X = X.join(rh, on=["route", "daytype", "hour"]).join(rs, on=["route", "daytype", "seg"]).join(r, on=["route", "daytype"])
    X[["rh", "rs", "rt"]] = X[["rh", "rs", "rt"]].fillna(0.0)
    m = meta.set_index("route")
    X["nseg"] = X["route"].map(m["n_stops"]).fillna(2) - 1
    X["segfrac"] = X["seg"] / X["nseg"].clip(lower=1)
    X["weekend"] = (X["daytype"] == "weekend").astype(int)
    X["l_route"] = np.log1p(X["rt"])
    X["l_rh"] = np.log1p(X["rh"])
    X["l_rs"] = np.log1p(X["rs"])
    # "independence" guess: this hour's share of the route x this stretch's share of the route
    X["l_mult"] = np.log1p(X["rh"] * X["rs"] / X["rt"].replace(0, np.nan)).fillna(0)
    X["l_base"] = np.log1p(X["y"])
    X["l_n"] = np.log1p(X["n"])
    X["l_dep"] = np.log1p(X["route"].map(m["dep_per_hour"]).fillna(1))
    # neighbouring stretches in the same hour (load changes smoothly along a route)
    X = X.sort_values(["route", "daytype", "hour", "seg"])
    grp = X.groupby(["route", "daytype", "hour"], observed=True)["y"]
    X["nb"] = (grp.shift(1).fillna(X["y"]) + grp.shift(-1).fillna(X["y"])) / 2
    X["l_nb_mean"] = np.log1p(X["nb"])
    return X.sort_index()


def _fit(X: pd.DataFrame, y: np.ndarray):
    from sklearn.ensemble import HistGradientBoostingRegressor

    model = HistGradientBoostingRegressor(loss="poisson", learning_rate=0.08, max_iter=250, max_leaf_nodes=48,
                                          min_samples_leaf=40, l2_regularization=1.0, random_state=0)
    model.fit(X[FEATURES], y)
    return model


def _blend(base: np.ndarray, ml: np.ndarray, n: np.ndarray, k: float, low_only: bool = True) -> np.ndarray:
    """Quiet cells (fewer than 15 tickets) lean on the model; medium and busy cells keep their own data."""
    w = n / (n + k)
    out = w * base + (1 - w) * ml
    return np.where(n < 15, out, base) if low_only else out


def _deviance(pred: np.ndarray, y: np.ndarray) -> float:
    """Mean Poisson deviance: the right yardstick for counts (MAE rewards predicting 0 on quiet stretches)."""
    p = np.maximum(pred, 1e-6)
    return float(np.mean(2 * (np.where(y > 0, y * np.log(np.maximum(y, 1e-12) / p), 0) - (y - p))))


def _buckets(n: np.ndarray) -> np.ndarray:
    return np.select([n >= 60, n >= 15], ["high", "medium"], "low")


def run(e: pd.DataFrame, df: pd.DataFrame, meta: pd.DataFrame, agg: pd.DataFrame) -> tuple[pd.Series, pd.DataFrame, dict]:
    """Returns (smoothed daily sample per agg row, evaluation table, info)."""
    log("ML: training gradient boosting on weeks 1-3, testing on week 4")
    nd = df.groupby(["week", "daytype"])["date"].nunique()
    days = lambda ws: {dt: int(sum(nd.get((w, dt), 0) for w in ws)) or 1 for dt in ("weekday", "weekend")}  # noqa: E731
    cells = agg[CELL].copy()

    # ---- fit: features weeks 0-1 -> target week 2
    f01 = _per_day(e, [0, 1], days([0, 1]))
    t2 = _per_day(e, [2], days([2]))
    Xtr = _features(cells, f01, meta)
    ytr = Xtr[CELL].merge(t2, on=CELL, how="left")["y"].fillna(0).to_numpy()
    model = _fit(Xtr, ytr)

    # blend strength k, tuned on the training target (week 2), never on the test week
    p_tr = model.predict(Xtr[FEATURES])
    ks = [1, 3, 5, 10, 20, 40, 80, 160]
    k = min(ks, key=lambda kk: _deviance(_blend(Xtr["y"].to_numpy(), p_tr, Xtr["n"].to_numpy(), kk), ytr))

    # ---- test: features weeks 0-2 -> predict week 3 (unseen)
    f012 = _per_day(e, [0, 1, 2], days([0, 1, 2]))
    t3 = _per_day(e, [3], days([3]))
    Xte = _features(cells, f012, meta)
    yte = Xte[CELL].merge(t3, on=CELL, how="left")["y"].fillna(0).to_numpy()
    base = Xte["y"].to_numpy()
    ml = model.predict(Xte[FEATURES])
    bl = _blend(base, ml, Xte["n"].to_numpy(), k)
    ml_global = _blend(base, ml, Xte["n"].to_numpy(), k, low_only=False)
    b = _buckets(Xte["n"].to_numpy())
    rows = []
    for name in ("low", "medium", "high", "all"):
        msk = (b == name) if name != "all" else np.ones(len(b), bool)
        if not msk.any():
            continue
        preds = (("current", base), ("ml", ml_global), ("blend", bl))
        mae = {m: float(np.abs(p[msk] - yte[msk]).mean()) for m, p in preds}
        dev = {m: _deviance(p[msk], yte[msk]) for m, p in preds}
        rows.append({"bucket": name, "cells": int(msk.sum()),
                     "dev_current": dev["current"], "dev_ml": dev["ml"], "dev_blend": dev["blend"],
                     "mae_current": mae["current"], "mae_ml": mae["ml"], "mae_blend": mae["blend"],
                     "improvement_pct": 100 * (1 - dev["blend"] / dev["current"]),
                     "mae_improvement_pct": 100 * (1 - mae["blend"] / mae["current"])})
    ev = pd.DataFrame(rows)
    for r in rows:
        log(f"  {r['bucket']:>6} ({r['cells']:,} cells): deviance current {r['dev_current']:.3f} · ML {r['dev_ml']:.3f} · "
            f"blend {r['dev_blend']:.3f} ({r['improvement_pct']:+.1f}%) | MAE {r['mae_improvement_pct']:+.1f}%")

    # ---- production model: features weeks 0-2 -> target week 3
    model_p = _fit(Xte, yte)

    # ---- final check on data no step has looked at: week index 4 (29-31 Oct), features weeks 0-3
    if 4 in set(e["week"].unique()):
        f0123 = _per_day(e, [0, 1, 2, 3], days([0, 1, 2, 3]))
        t4 = _per_day(e, [4], days([4]))
        Xf = _features(cells, f0123, meta)
        yf = Xf[CELL].merge(t4, on=CELL, how="left")["y"].fillna(0).to_numpy()
        nf = Xf["n"].to_numpy()
        has = (Xf["daytype"].map(days([4])) > 0).to_numpy() & (Xf["daytype"] == "weekday").to_numpy()
        basef, blf = Xf["y"].to_numpy(), _blend(Xf["y"].to_numpy(), model_p.predict(Xf[FEATURES]), nf, k)
        bf = _buckets(nf)
        for name in ("low", "all"):
            msk = has & ((bf == name) if name != "all" else True)
            dc, db = _deviance(basef[msk], yf[msk]), _deviance(blf[msk], yf[msk])
            mc, mb = float(np.abs(basef[msk] - yf[msk]).mean()), float(np.abs(blf[msk] - yf[msk]).mean())
            rows.append({"bucket": f"final_{name}", "cells": int(msk.sum()), "dev_current": dc, "dev_ml": np.nan,
                         "dev_blend": db, "mae_current": mc, "mae_ml": np.nan, "mae_blend": mb,
                         "improvement_pct": 100 * (1 - db / dc), "mae_improvement_pct": 100 * (1 - mb / mc)})
            log(f"  final check 29-31 Oct, {name:>4} ({int(msk.sum()):,} cells): deviance {dc:.3f} -> {db:.3f} "
                f"({100 * (1 - db / dc):+.1f}%) | MAE {100 * (1 - mb / mc):+.1f}%")
        ev = pd.DataFrame(rows)
    allw = sorted(e["week"].unique().tolist())
    fall = _per_day(e, allw, days(allw))
    Xall = _features(cells, fall, meta)
    ml_all = model_p.predict(Xall[FEATURES])
    smooth = _blend(Xall["y"].to_numpy(), ml_all, Xall["n"].to_numpy(), k)
    info = {"k": k, "model": "HistGradientBoostingRegressor(poisson)", "features": FEATURES}
    # keep the ML only if the blend beats the current method overall on the held-out week
    overall = ev[ev.bucket == "all"].iloc[0]
    info["used"] = bool(overall["dev_blend"] < overall["dev_current"] and overall["mae_blend"] <= overall["mae_current"] * 1.01)
    info["policy"] = "ML blend on low-confidence cells (<15 tickets) only"
    log(f"  blend k={k}; {'using' if info['used'] else 'NOT using'} ML smoothing")
    out = pd.Series(smooth if info["used"] else Xall["y"].to_numpy(), index=agg.index)
    return out, ev, info

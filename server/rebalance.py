"""Fleet rebalancer: move buses between routes for a time window and see crowding change.

Assumptions (kept deliberately simple, and stated in the UI):
  * passenger flow on each stretch stays the same (no induced or lost demand);
  * a route's departures per hour scale with its fleet: dep' = dep * (F + delta) / F;
  * moved buses work the receiving route for the whole window (both directions).

The objective is crowded passenger-minutes per day in the window: for every
stretch and hour, passengers crossing it x minutes on it, counted only where the
load per bus is above comfortable capacity (seats + comfortable standing). Only
medium- and high-confidence stretches are counted, so the optimiser does not chase
noise on sparse routes.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .model import Model


@dataclass
class BaseRoute:
    base: str
    fleet: float
    flows: list      # per direction: (flow[h, seg] pax/hour, minutes[seg], dep_per_hour, comfort_cap, mask[h, seg])
    peak_occ: float  # worst load / comfortable capacity in window


class Rebalancer:
    def __init__(self, model: Model, daytype: str, start: int, end: int):
        self.m, self.daytype, self.hours = model, daytype, list(range(start, end))
        fleet = model._routes_df.drop_duplicates("base").set_index("base")["fleet"].to_dict()
        by_base: dict[str, list] = {}
        for R in model.routes.values():
            L, C = R.load.get(daytype), R.conf.get(daytype)
            if L is None:
                continue
            flow = L[self.hours] * model.scale * R.dep_per_hour
            mins = np.array([R.dt_h[h] for h in self.hours])[:, None] * np.ones(L.shape[1])
            cap = R.bus["seats"] + R.bus["standing_comfort"]
            by_base.setdefault(R.base, []).append((flow, mins, R.dep_per_hour, cap, C[self.hours] >= 1, R.name))
        self.bases: dict[str, BaseRoute] = {}
        for b, dirs in by_base.items():
            F = float(fleet.get(b) or 0)
            if F < 2:  # routes with almost no observed buses cannot give or take reliably
                continue
            occ = max(float((f / d / cap * k).max()) for f, _, d, cap, k, _ in dirs)
            self.bases[b] = BaseRoute(b, F, dirs, occ)

    def cpm(self, b: str, delta: float = 0.0) -> float:
        """Crowded passenger-minutes per day in the window for base route b with `delta` buses added."""
        B = self.bases[b]
        factor = max(B.fleet + delta, 0.5) / B.fleet
        tot = 0.0
        for flow, mins, dep, cap, mask, _ in B.flows:
            load = flow / (dep * factor)
            tot += float((flow * mins * ((load > cap) & mask)).sum())
        return tot  # each window hour: passengers crossing x minutes on the stretch

    def peak(self, b: str, delta: float = 0.0) -> float:
        B = self.bases[b]
        factor = max(B.fleet + delta, 0.5) / B.fleet
        return max(float(((flow / (dep * factor)) * mask).max()) for flow, _, dep, cap, mask, _ in B.flows)

    def candidates(self, n: int = 12) -> dict:
        rows = []
        for b, B in self.bases.items():
            gain = self.cpm(b) - self.cpm(b, +1)
            cost = self.cpm(b, -1) - self.cpm(b)
            rows.append({"base": b, "fleet": round(B.fleet, 1), "peak_occupancy_pct": round(100 * B.peak_occ),
                         "gain_per_bus": round(gain), "cost_per_bus": round(cost),
                         "routes": [d[5] for d in B.flows]})
        receivers = sorted([r for r in rows if r["gain_per_bus"] > 0], key=lambda r: -r["gain_per_bus"])[:n]
        donors = sorted([r for r in rows if r["fleet"] >= 6 and r["peak_occupancy_pct"] < 75],
                        key=lambda r: (r["cost_per_bus"], r["peak_occupancy_pct"]))[:n]
        return {"receivers": receivers, "donors": donors, **self.totals({})}

    def totals(self, delta: dict[str, float]) -> dict:
        before = sum(self.cpm(b) for b in self.bases)
        changed = sum(self.cpm(b, d) - self.cpm(b) for b, d in delta.items() if b in self.bases)
        return {"crowded_pax_min_before": round(before), "crowded_pax_min_after": round(before + changed),
                "change_pct": round(100 * changed / before, 1) if before else 0.0}

    def evaluate(self, moves: list[dict]) -> dict:
        delta: dict[str, float] = {}
        for mv in moves:
            n = float(mv.get("n", 1))
            delta[mv["from"]] = delta.get(mv["from"], 0) - n
            delta[mv["to"]] = delta.get(mv["to"], 0) + n
        bad = [b for b in delta if b not in self.bases]
        if bad:
            raise KeyError(f"unknown or too-sparse route(s): {', '.join(bad)}")
        per = []
        for b, d in sorted(delta.items(), key=lambda kv: kv[1]):
            B = self.bases[b]
            per.append({"base": b, "delta_buses": d, "fleet_before": round(B.fleet, 1),
                        "fleet_after": round(B.fleet + d, 1),
                        "peak_load_before": round(self.peak(b)), "peak_load_after": round(self.peak(b, d)),
                        "crowded_pax_min_before": round(self.cpm(b)), "crowded_pax_min_after": round(self.cpm(b, d))})
        return {**self.totals(delta), "routes": per, "buses_moved": int(sum(-d for d in delta.values() if d < 0))}

    def optimise(self, max_moves: int = 40, keep_share: float = 0.7) -> dict:
        """Greedy: repeatedly move one bus from the route that loses least to the route that gains most."""
        delta = {b: 0.0 for b in self.bases}
        floor = {b: -max(0.0, B.fleet * (1 - keep_share)) for b, B in self.bases.items()}
        gain = {b: self.cpm(b) - self.cpm(b, 1) for b in self.bases}
        cost = {b: self.cpm(b, -1) - self.cpm(b) for b in self.bases}
        moves = []
        for _ in range(max_moves):
            r = max(gain, key=gain.get)
            donors = [b for b in cost if b != r and delta[b] - 1 >= floor[b] and delta[b] <= 0]
            if not donors:
                break
            # among equal-cost donors, take from the route that stays emptiest after giving a bus
            d = min(donors, key=lambda b: (cost[b], self.peak(b, delta[b] - 1)))
            if gain[r] - cost[d] <= 1.0:
                break
            delta[r] += 1; delta[d] -= 1
            if moves and moves[-1]["from"] == d and moves[-1]["to"] == r:
                moves[-1]["n"] += 1
            else:
                moves.append({"from": d, "to": r, "n": 1})
            for b in (r, d):
                gain[b] = self.cpm(b, delta[b]) - self.cpm(b, delta[b] + 1)
                cost[b] = self.cpm(b, delta[b] - 1) - self.cpm(b, delta[b])
        # merge repeated pairs
        merged: dict[tuple, int] = {}
        for mv in moves:
            merged[(mv["from"], mv["to"])] = merged.get((mv["from"], mv["to"]), 0) + mv["n"]
        moves = [{"from": f, "to": t, "n": n} for (f, t), n in merged.items()]
        return {"moves": moves, **self.evaluate(moves)}

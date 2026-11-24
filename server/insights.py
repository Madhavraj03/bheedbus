"""City-level insights on top of the crowding model.

* events(): unusual days and stop surges found by pipeline/events.py (no calendar used)
* bunch(): how often buses of a route travel in pairs, and the chance another one is close behind
* nudge(): what happens if some riders leave a little later (or earlier) in the peaks
* city(): every mapped stretch in Delhi with its crowding hour by hour, for the time-lapse
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from pathlib import Path

import numpy as np
import pandas as pd

from .model import LEVELS, Model

CITY_CACHE = Path(__file__).resolve().parents[1] / "data" / "out" / "city_lines.json"


class Insights:
    def __init__(self, model: Model):
        self.m = model
        self._city: dict = {}
        self._city_lock = threading.Lock()
        self._near: np.ndarray | None = None  # place id, lat, lon for every stop we can map
        self.reload()

    def reload(self) -> None:
        con = sqlite3.connect(self.m.db_path)
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        q = lambda t: pd.read_sql(f"select * from {t}", con) if t in tables else None  # noqa: E731
        self.days, self.hours, self.stops = q("event_days"), q("event_hours"), q("event_stops")
        b, bc = q("bunching"), q("val_bunching_city")
        self.curve = q("val_bunching_curve")
        con.close()
        self.city_g = float(self.m.info.get("bunching_city_g", 0.8))
        self.g_city = {dt: np.full(24, self.city_g) for dt in ("weekday", "weekend")}
        if bc is not None:
            for r in bc.itertuples():
                if r.far >= 50:
                    self.g_city[r.daytype][int(r.hour)] = float(r.g)
        self.g = {}
        if b is not None:
            for (rn, dt), g in b.groupby(["route", "daytype"]):
                arr = self.g_city[dt].copy()
                arr[g["hour"].to_numpy()] = g["g"].to_numpy()
                self.g[(rn, dt)] = arr
        self._city = {}

    # ------------------------------------------------------------ bunching
    def bunch(self, route: str, hour: int, daytype: str = "weekday", window: float = 5.0) -> dict:
        """Pair-correlation g for this route-hour and what it means for someone at the stop.

        With buses arriving at rate 1/H and pair-correlation g at short gaps, the chance that
        another bus of the same route arrives within w minutes is about 1 - exp(-g w / H).
        A perfectly even timetable would give 0 whenever w < H.
        """
        R = self.m.routes[route]
        g = float(self.g.get((route, daytype), self.g_city[daytype])[hour % 24])
        H = 60 / max(R.dep_per_hour, 0.1)
        p_next = 1 - math.exp(-g * window / H)
        label = "even" if g < 0.5 else "irregular" if g < 1.0 else "in packs"
        return {"g": round(g, 2), "headway_min": round(H, 1), "p_next_within": round(100 * p_next),
                "window_min": window, "label": label,
                "tip": (f"Buses on {route} often come close together at this hour. If the first is packed, "
                        f"there's about a {round(100 * p_next)}% chance another {route} comes within {window:g} min.")
                if g >= 0.85 and H >= 6 and p_next >= 0.25 else None}

    def bunching_summary(self) -> dict:
        out = {"city_g": round(self.city_g, 2),
               "by_hour": {dt: [round(float(x), 3) for x in arr] for dt, arr in self.g_city.items()},
               "curve": self.curve.to_dict("records") if self.curve is not None else []}
        rows = []
        for (rn, dt), arr in self.g.items():
            if dt != "weekday" or rn not in self.m.routes:
                continue
            R = self.m.routes[rn]
            pk = [8, 9, 18, 19]
            rows.append({"route": rn, "g_peak": round(float(arr[pk].mean()), 2), "dep_per_hour": round(R.dep_per_hour, 1),
                         "from": R.stops[0], "to": R.stops[-1]})
        # rank only routes with enough buses for the measure to matter
        rows = [r for r in rows if r["dep_per_hour"] >= 4]
        out["worst_routes"] = sorted(rows, key=lambda r: -r["g_peak"])[:12]
        return out

    # ------------------------------------------------------------ events
    def events(self) -> dict:
        if self.days is None:
            return {"available": False}
        from .geo import points
        days = self.days.copy()
        flagged = days[days.flagged > 0]
        known = days[days.known_event != ""]
        found = known[known.flagged > 0]
        loc: dict[str, dict] = {}
        for s in self.stops["stop"].unique():
            pid = self.m.stop_place.get(s)
            for rn, pos in self.m.place_routes.get(pid, [])[:6]:
                if self.m.routes[rn].stops[pos] == s:
                    p = points(rn, self.m.routes[rn].stops)[pos]
                    if p["lat"] is not None:
                        loc[s] = {"lat": p["lat"], "lon": p["lon"]}
                        break
        stops = self.stops.assign(lat=self.stops["stop"].map(lambda s: loc.get(s, {}).get("lat")),
                                  lon=self.stops["stop"].map(lambda s: loc.get(s, {}).get("lon")))
        clusters = (stops.groupby("date").agg(n=("stop", "size"), top=("stop", lambda x: list(x)[:6]))
                    .reset_index().query("n >= 3").sort_values("n", ascending=False))
        return {"available": True,
                "days": days.to_dict("records"),
                "hours": {d: g.sort_values("hour")[["hour", "observed", "expected"]].to_dict("records")
                          for d, g in self.hours.groupby("date")} if self.hours is not None and len(self.hours) else {},
                "stops": stops.astype(object).where(stops.notna(), None).to_dict("records"),
                "clusters": clusters.to_dict("records"),
                "check": {"known": len(known), "found": len(found), "flagged": len(flagged),
                          "unexplained": flagged[flagged.known_event == ""]["date"].tolist()}}

    # ------------------------------------------------------------ nudge
    def nudge(self, share: float = 0.1, minutes: int = 20, start: int = 8, end: int = 10,
              daytype: str = "weekday", also_evening: bool = True) -> dict:
        """Move `share` of riders in the peak window `minutes` later (negative = earlier).

        Riders are spread evenly through an hour, so moving them m minutes pushes
        share * m / 60 of the hour's flow into the next hour (on every stretch).
        Stretch flows otherwise stay the same. Medium/high-confidence stretches only.
        """
        share = float(np.clip(share, 0, 0.6))
        minutes = int(np.clip(minutes, -60, 60))
        hours = set(range(start, end))
        if also_evening:
            hours |= set(range(17, 20))
        frac = share * abs(minutes) / 60
        step = 1 if minutes >= 0 else -1
        tot = {k: 0.0 for k in ("crowded_b", "crowded_a", "seated_b", "seated_a", "packed_b", "packed_a")}
        by_hour_b, by_hour_a = np.zeros(24), np.zeros(24)
        per_route = []
        sc = self.m.scale
        for R in self.m.routes.values():
            L, C, V = R.load.get(daytype), R.conf.get(daytype), R.cv.get(daytype)
            if L is None:
                continue
            F = L * R.dep_per_hour  # passengers per hour across each stretch (unscaled)
            F2 = F.copy()
            for h in hours:
                moved = F[h] * frac
                F2[h] -= moved
                F2[(h + step) % 24] += moved
            mask = C >= 1
            mins = np.array(R.dt_h)[:, None]
            cap = R.bus["seats"] + R.bus["standing_comfort"]

            def metrics(Fx):
                Lx = Fx / R.dep_per_hour
                lv = self.m.level_of(Lx, R.bus)
                seat = self.m.seat_prob(Lx, V, R.bus["seats"]) if mask.any() else np.zeros_like(Lx)
                pm = Fx * sc * mins * mask
                return ((pm * (Lx * sc > cap)).sum(axis=1), (pm * seat).sum(axis=1), ((lv == 3) & mask).any(axis=1))

            if not mask.any():
                continue
            cb, sb, pb = metrics(F)
            ca, sa, pa = metrics(F2)
            by_hour_b += cb; by_hour_a += ca
            tot["crowded_b"] += cb.sum(); tot["crowded_a"] += ca.sum()
            tot["seated_b"] += sb.sum(); tot["seated_a"] += sa.sum()
            tot["packed_b"] += pb.sum(); tot["packed_a"] += pa.sum()
            if cb.sum() - ca.sum() > 0:
                per_route.append({"route": R.name, "crowded_pax_min_saved": round(float(cb.sum() - ca.sum())),
                                  "packed_hours_before": int(pb.sum()), "packed_hours_after": int(pa.sum())})
        cb, ca = tot["crowded_b"], tot["crowded_a"]
        # riders shifted = digital boardings in those hours x expansion x share
        hp = self.m.validation["hourly_profile"]
        hp = hp[hp.daytype == daytype].set_index("hour")["per_day"]
        E = float(self.m.info.get("expansion_factor", 1.0)) * sc
        riders_moved = float(hp.reindex(sorted(hours)).fillna(0).sum()) * E * share
        return {"share": share, "minutes": minutes, "hours": sorted(hours), "daytype": daytype,
                "riders_shifted_per_day": round(riders_moved, -2),
                "crowded_pax_hours_before": round(float(cb) / 60), "crowded_pax_hours_after": round(float(ca) / 60),
                "crowded_change_pct": round(float(100 * (ca - cb) / cb), 1) if cb else 0.0,
                "seated_pax_hours_gained": round(float(tot["seated_a"] - tot["seated_b"]) / 60),
                "packed_route_hours_before": int(tot["packed_b"]), "packed_route_hours_after": int(tot["packed_a"]),
                "by_hour_before": np.round(by_hour_b / 60).astype(int).tolist(),
                "by_hour_after": np.round(by_hour_a / 60).astype(int).tolist(),
                "top_routes": sorted(per_route, key=lambda r: -r["crowded_pax_min_saved"])[:8]}

    # ------------------------------------------------------------ city time-lapse
    def _geometry(self) -> list[tuple]:
        """[(route, seg, lat1, lon1, lat2, lon2)] for every mapped stretch; cached on disk (~30 s to build)."""
        built = self.m.info.get("built_at")
        if CITY_CACHE.exists():
            d = json.loads(CITY_CACHE.read_text())
            if d.get("built_at") == built:
                return [tuple(x) for x in d["segs"]]
        from .geo import km, points
        segs = []
        for R in self.m.routes.values():
            pts = points(R.name, R.stops)
            for i in range(len(pts) - 1):
                a, b = pts[i], pts[i + 1]
                if a["lat"] is None or b["lat"] is None:
                    continue
                if km((a["lat"], a["lon"]), (b["lat"], b["lon"])) > 1.6:  # implausible hop (likely a wrong match): skip, don't draw
                    continue
                segs.append((R.name, i, round(a["lat"], 5), round(a["lon"], 5), round(b["lat"], 5), round(b["lon"], 5)))
        try:
            CITY_CACHE.parent.mkdir(parents=True, exist_ok=True)
            CITY_CACHE.write_text(json.dumps({"built_at": built, "segs": segs}))
        except OSError:  # read-only deploy: keep it in memory only
            pass
        return segs

    def near(self, lat: float, lon: float, k: int = 5, max_km: float = 3.0) -> list[dict]:
        """Closest places to a shared location (one entry per place, nearest stop first)."""
        if self._near is None:
            from .geo import km, points
            seen: dict[str, list] = {}
            for R in self.m.routes.values():
                pts = points(R.name, R.stops)
                for j, (s, p) in enumerate(zip(R.stops, pts)):
                    if p["lat"] is None or p.get("approx") or s not in self.m.stop_place:
                        continue  # a guessed spot between two known stops can be km off: never offer it as "nearby"
                    # trust a position only if a neighbouring stop on the route is close (wrong matches jump km away)
                    nb = [q for q in pts[max(0, j - 1):j + 2] if q is not p and q["lat"] is not None]
                    if any(km((p["lat"], p["lon"]), (q["lat"], q["lon"])) <= 1.6 for q in nb):
                        seen.setdefault(s, []).append((p["lat"], p["lon"]))
            rows = []
            for s, ps in seen.items():  # where routes disagree, keep the spot most of them agree on
                best = max(ps, key=lambda a: sum(km(a, b) < 0.5 for b in ps))
                rows.append((self.m.stop_place[s], *best))
            self._near = np.array(rows, dtype=float)
        if not len(self._near):
            return []
        pid, la, lo = self._near.T
        d = 6371 * np.hypot(np.radians(la - lat), np.radians(lo - lon) * math.cos(math.radians(lat)))
        out, seen = [], set()
        for i in np.argsort(d):
            if d[i] > max_km or len(out) >= k:
                break
            if int(pid[i]) not in seen:
                seen.add(int(pid[i]))
                out.append({"id": int(pid[i]), "name": self.m.place_label[int(pid[i])], "km": round(float(d[i]), 2)})
        return out

    def city(self, daytype: str = "weekday") -> dict:
        """Shared road pieces with, per hour, one character: '-' no data, else hex digit level*4 + width (0-3).

        Several routes share a road; each piece gets the passenger-weighted mean occupancy of the
        medium/high-confidence routes on it, and a width from total passengers per hour.
        """
        with self._city_lock:
            if daytype in self._city:
                return self._city[daytype]
            segs = self._geometry()
            pieces: dict[tuple, int] = {}
            coords = []
            flow = []   # per piece: [24] passengers/hour
            occw = []   # per piece: [24] sum(flow * occupancy)
            for rn, i, a1, o1, a2, o2 in segs:
                R = self.m.routes.get(rn)
                if R is None or daytype not in R.load:
                    continue
                k = (a1, o1, a2, o2) if (a1, o1) <= (a2, o2) else (a2, o2, a1, o1)
                j = pieces.get(k)
                if j is None:
                    j = pieces[k] = len(coords)
                    coords.append(list(k)); flow.append(np.zeros(24)); occw.append(np.zeros(24))
                L, C = R.load[daytype][:, i], R.conf[daytype][:, i]
                f = L * self.m.scale * R.dep_per_hour * (C >= 1)
                cap = R.bus["seats"] + R.bus["standing_comfort"]
                flow[j] += f
                occw[j] += f * (L * self.m.scale / cap)
            F, O = np.array(flow), np.array(occw)
            occ = np.divide(O, F, out=np.zeros_like(O), where=F > 0)
            # occupancy of comfortable capacity -> levels: seats <= 0.85*40/70, standing <= 1, crowded <= 100/70
            lvl = np.select([occ <= 0.486, occ <= 1.0, occ <= 1.43], [0, 1, 2], 3)
            wid = np.digitize(F, [150, 500, 1500])
            code = np.where(F > 0, lvl * 4 + wid, -1)
            keep = (F[:, 5:23] > 0).any(axis=1)
            hexd = "0123456789abcdef"
            rows = [coords[j] + ["".join("-" if c < 0 else hexd[c] for c in code[j])] for j in np.flatnonzero(keep)]
            # city totals per hour for the caption
            crowded = [int(((lvl[:, h] >= 2) & (F[:, h] > 0)).sum()) for h in range(24)]
            out = {"daytype": daytype, "pieces": rows, "crowded_pieces": crowded,
                   "levels": LEVELS, "n": len(rows)}
            self._city[daytype] = out
            return out

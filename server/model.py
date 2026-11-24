"""In-memory crowding model + crowding-aware journey planner.

Loads data/out/crowding.db (built by pipeline/build.py) and answers
  * place search
  * per-route crowding profiles
  * journey plans that trade travel time against crowding
  * operator hotspots
"""
from __future__ import annotations

import difflib
import json
import math
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

LEVELS = ["seats", "standing", "crowded", "packed"]
DAYTYPES = ["weekday", "weekend"]
ALIASES = {"cp": "connaught place", "isbt": "isbt", "nizamuddin": "nizamuddin",
           "aiims": "aiims", "ito": "ito", "jnu": "jnu", "du": "university"}


def place_key(name: str) -> str:
    s = name.lower().replace("i.s.b.t", "isbt").replace(".", " ")
    s = s.split("/")[0]
    s = re.sub(r"\(.*?\)|\(.*$", " ", s)
    s = re.sub(r"\bgate no ?\d+\b|\bterminal\b|\bbus (stand|terminal|stop)\b|\(t\)", " ", s)
    s = re.sub(r"\bsation\b", "station", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip() or name.lower()


@dataclass
class Route:
    name: str
    base: str
    dir: str
    stops: list[str]
    places: list[int]
    mps: float
    dep_per_hour: float
    bus: dict
    deph_source: str
    expansion: float
    # load[daytype][hour] -> np.array(n_seg); n[...] ticket counts; conf[...] 0/1/2
    load: dict = field(default_factory=dict)
    n: dict = field(default_factory=dict)
    conf: dict = field(default_factory=dict)
    cv: dict = field(default_factory=dict)


class Model:
    def __init__(self, db_path: Path, config: dict):
        self.db_path = db_path
        self.cfg = config
        self.scale = 1.0
        self.insights = None  # server.insights.Insights, attached by the app (bunching tips on legs)
        self.reload()

    # ------------------------------------------------------------------ load
    def reload(self) -> None:
        con = sqlite3.connect(self.db_path)
        q = lambda s: pd.read_sql(s, con)  # noqa: E731
        self.info = {r.key: json.loads(r.value) for r in q("select * from info").itertuples()}
        routes = q("select * from routes")
        self._routes_df = routes
        stops = q("select * from route_stops order by route, pos")
        seg = q("select * from segment_load")
        self.cong = q("select * from congestion").sort_values("hour")["factor"].to_numpy()
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        self.coords = (q("select * from stop_coords").set_index("stop")[["lat", "lon"]].to_dict("index")
                       if "stop_coords" in tables else {})
        self.validation = {t[4:]: q(f"select * from {t}") for t in tables if t.startswith("val_")}
        con.close()

        bt = self.cfg["bus_types"]
        # places = clusters of stop names that act as one interchange
        all_stops = stops.groupby("stop")[["boardings", "alightings"]].sum()
        all_stops["vol"] = all_stops.sum(axis=1)
        all_stops["key"] = [place_key(s) for s in all_stops.index]
        keys = sorted(all_stops["key"].unique())
        self.key_id = {k: i for i, k in enumerate(keys)}
        lab = all_stops.sort_values("vol", ascending=False).groupby("key").head(1)
        self.place_label = [""] * len(keys)
        self.place_vol = np.zeros(len(keys))
        self.place_names: list[list[str]] = [[] for _ in keys]
        for s, r in all_stops.iterrows():
            i = self.key_id[r.key]
            self.place_names[i].append(s)
            self.place_vol[i] += r.vol
        for s, r in lab.iterrows():
            self.place_label[self.key_id[r.key]] = s
        self.stop_place = {s: self.key_id[k] for s, k in all_stops["key"].items()}

        self.routes: dict[str, Route] = {}
        st = {r: g["stop"].tolist() for r, g in stops.groupby("route")}
        for r in routes.itertuples():
            if r.route not in st:
                continue
            names = st[r.route]
            self.routes[r.route] = Route(
                r.route, r.base, r.dir, names, [self.stop_place[s] for s in names], float(r.mps),
                float(r.dep_per_hour), bt[r.bus_type], r.deph_source, float(r.expansion))
        lvl_idx = {k: i for i, k in enumerate(LEVELS)}
        conf_idx = {"low": 0, "medium": 1, "high": 2}
        for (route, dt), g in seg.groupby(["route", "daytype"]):
            R = self.routes.get(route)
            if R is None:
                continue
            nseg = max(len(R.stops) - 1, 1)
            L = np.zeros((24, nseg)); N = np.zeros((24, nseg), dtype=int); C = np.zeros((24, nseg), dtype=int)
            V = np.full((24, nseg), 0.25)
            ok = g["seg"] < nseg
            g = g[ok]
            L[g.hour, g.seg] = g["load"]; N[g.hour, g.seg] = g["n_tickets"]
            if "day_cv" in g:
                V[g.hour, g.seg] = g["day_cv"].fillna(0.25)
            C[g.hour, g.seg] = g["confidence"].map(conf_idx)
            R.load[dt], R.n[dt], R.conf[dt], R.cv[dt] = L, N, C, V
        self.occ_vol = {(r.route, r.pos): r.boardings + r.alightings for r in stops.itertuples()}
        self._split_ambiguous_places()
        self.place_routes: dict[int, list[tuple[str, int]]] = defaultdict(list)
        for R in self.routes.values():
            for pos, p in enumerate(R.places):
                self.place_routes[p].append((R.name, pos))
        self.levels_cfg = {l["key"]: l for l in self.cfg["levels"]}
        self._prep()

    def _split_ambiguous_places(self, radius: int = 2) -> None:
        """Some stop names exist in two parts of the city ("Super Bazar").

        Two route occurrences of a place are the same physical stop if their
        neighbouring stops can reach each other within `radius` hops of the stop
        graph *without passing through the place itself*. Occurrences that cannot
        are split into separate places, so the planner never "teleports".
        """
        # only well-sampled routes are trusted as evidence of adjacency; on a sparse
        # route two consecutive positions can be far apart and would bridge areas
        adj: dict[int, set[int]] = defaultdict(set)
        occ: dict[int, list[tuple[str, int]]] = defaultdict(list)
        reliable = {r.route for r in self._routes_df.itertuples() if r.tickets >= 300 and r.n_stops >= 12}
        for R in self.routes.values():
            for i, p in enumerate(R.places):
                occ[p].append((R.name, i))
                if i and R.name in reliable:
                    adj[p].add(R.places[i - 1]); adj[R.places[i - 1]].add(p)
        n_split = 0
        for X, lst in list(occ.items()):
            if len({r for r, _ in lst}) < 2:
                continue
            groups: dict[frozenset, list] = defaultdict(list)
            for rn, i in lst:
                pl = self.routes[rn].places
                nb = frozenset(q for q in (pl[i - 1] if i else None, pl[i + 1] if i + 1 < len(pl) else None)
                               if q is not None and q != X)
                groups[nb].append((rn, i))
            keys = [k for k in groups if k and any(rn in reliable for rn, _ in groups[k])]
            loose = [k for k in groups if k and k not in keys]
            if len(keys) < 2:
                continue
            balls = []
            for nb in keys:
                seen, frontier = set(nb), set(nb)
                for _ in range(radius):
                    frontier = {y for x in frontier for y in adj[x] if y != X} - seen
                    seen |= frontier
                balls.append(seen)
            parent = list(range(len(keys)))
            def find(a):
                while parent[a] != a:
                    parent[a] = parent[parent[a]]; a = parent[a]
                return a
            for a in range(len(keys)):
                for b in range(a + 1, len(keys)):
                    if find(a) != find(b) and balls[a] & balls[b]:
                        parent[find(a)] = find(b)
            comps: dict[int, list] = defaultdict(list)
            for a, k in enumerate(keys):
                comps[find(a)].append(k)
            # sparse-route occurrences join the component they overlap most (never bridge)
            for k in loose:
                ov = [(len(k & balls[a]), find(a)) for a in range(len(keys))]
                best_ov, root = max(ov)
                if best_ov:
                    comps[root].append(k)
            if len(comps) < 2:
                continue
            ranked = sorted(comps.values(), key=lambda ks: -sum(self.occ_vol.get(o, 0) for k in ks for o in groups[k]))
            for ks in ranked[1:]:
                nid = len(self.place_label)
                members = [o for k in ks for o in groups[k]]
                nbr = max((q for k in ks for q in k), key=lambda q: self.place_vol[q])
                self.place_label.append(f"{self.place_label[X]} (near {self.place_label[nbr]})")
                self.place_names.append(sorted({self.routes[rn].stops[i] for rn, i in members}))
                vol = sum(self.occ_vol.get(o, 0) for o in members)
                self.place_vol = np.append(self.place_vol, vol)
                self.place_vol[X] = max(self.place_vol[X] - vol, 0)
                for rn, i in members:
                    self.routes[rn].places[i] = nid
                n_split += 1
        self.n_split = n_split
        self._adj = adj
        self._ball_cache: dict[tuple[str, int], set] = {}

    def _nb(self, rn: str, i: int) -> frozenset:
        pl = self.routes[rn].places
        return frozenset(q for q in (pl[i - 1] if i else None, pl[i + 1] if i + 1 < len(pl) else None)
                         if q is not None and q != pl[i])

    def can_transfer(self, ra: str, ia: int, rb: str, ib: int) -> bool:
        """Is (ra, ia) the same physical stop as (rb, ib)?

        Yes if the two routes run together through it (share a neighbouring stop),
        or a third route through the same place touches a neighbour of each
        (a turning route at a junction). Checked pairwise, never chained, so a
        common name in two parts of the city does not become a transfer.
        """
        A, B = self.routes[ra], self.routes[rb]
        if A.base == B.base:
            return True
        key = (ra, ia, rb, ib)
        hit = self._ball_cache.get(key)
        if hit is None:
            na, nb = self._nb(ra, ia), self._nb(rb, ib)
            hit = bool(na & nb)
            if not hit:
                for rc, ic in self.place_routes[A.places[ia]]:
                    nc = self._nb(rc, ic)
                    if nc & na and nc & nb:
                        hit = True
                        break
            self._ball_cache[key] = hit
        return hit

    def _prep(self) -> None:
        """Precompute per-route level matrices and per-hour stretch minutes (fast inner loop)."""
        for R in self.routes.values():
            # stops with very few tickets have an unreliable inferred position on the route
            R.anchor = [self.occ_vol.get((R.name, i), 0) >= 5 for i in range(len(R.stops))]
            R.lv = {dt: self.level_of(L, R.bus).tolist() for dt, L in R.load.items()}
            R.dt_h = [R.mps * float(c) for c in self.cong]

    def set_scale(self, scale: float) -> None:
        if abs(scale - self.scale) > 1e-9:
            self.scale = scale
            self._prep()

    # --------------------------------------------------------------- helpers
    def level_of(self, load: np.ndarray, bus: dict) -> np.ndarray:
        load = np.asarray(load) * self.scale
        return np.select([load <= 0.85 * bus["seats"], load <= bus["seats"] + bus["standing_comfort"],
                          load <= bus["crush"]], [0, 1, 2], 3)

    def seat_prob(self, load, day_cv, seats: int):
        """P(a bus arrives with fewer than `seats` people on board).

        Bus load ~ lognormal with the estimated mean, and spread from day-to-day
        variation (measured per cell) plus bus-to-bus variation within the hour
        (bunching; an assumption in config).
        """
        mu = np.maximum(np.asarray(load, dtype=float) * self.scale, 0.05)
        cv2 = np.asarray(day_cv, dtype=float) ** 2 + self.cfg["seat_probability"]["bus_to_bus_cv"] ** 2
        sig2 = np.log1p(cv2)
        z = (np.log(seats) - (np.log(mu) - sig2 / 2)) / np.sqrt(sig2)
        return 0.5 * (1 + np.vectorize(math.erf)(z / math.sqrt(2)))

    def route_loads(self, R: Route, daytype: str, hour: int) -> np.ndarray:
        L = R.load.get(daytype)
        if L is None:
            return np.zeros(max(len(R.stops) - 1, 1))
        return L[hour % 24]

    def search_places(self, q: str, limit: int = 8) -> list[dict]:
        ql = q.lower().strip()
        ql = " ".join(ALIASES.get(t, t) for t in re.findall(r"[a-z0-9]+", ql))
        toks = ql.split()
        if not toks:
            return []
        hits = []
        for i, names in enumerate(self.place_names):
            blob = " ".join(n.lower() for n in names)
            if all(t in blob for t in toks):
                exact = self.place_label[i].lower().startswith(ql)
                hits.append((exact, self.place_vol[i], i))
        if not hits:
            keys = list(self.key_id)
            for k in difflib.get_close_matches(ql, keys, n=limit, cutoff=0.6):
                i = self.key_id[k]
                hits.append((False, self.place_vol[i], i))
        hits.sort(key=lambda h: (not h[0], -h[1]))
        return [{"id": i, "name": self.place_label[i], "stops": len(self.place_names[i]),
                 "routes": len({r for r, _ in self.place_routes[i]}), "volume": int(v)}
                for _, v, i in hits[:limit]]

    def resolve(self, q: str | int, group: bool = True) -> list[int]:
        """A query may mean several places (e.g. "Anand Vihar" -> ISBT, In Gate, ...)."""
        if isinstance(q, int) or (isinstance(q, str) and q.isdigit()):
            return [int(q)]
        hits = self.search_places(q, limit=6 if group else 1)
        if not hits:
            return []
        top = hits[0]["volume"]
        return [h["id"] for h in hits if h["volume"] >= 0.08 * top]

    # --------------------------------------------------------------- planner
    def plan(self, origin, dest, minute: int, daytype: str = "weekday", comfort: float = 0.5,
             max_transfers: int = 2, n_options: int = 4) -> dict:
        O, D = self.resolve(origin), self.resolve(dest)
        if not O or not D:
            return {"error": f"Could not find {'origin' if not O else 'destination'} place", "options": []}
        if set(O) & set(D):
            return {"error": "Origin and destination are the same place", "options": []}
        variants = [("fastest", 0.0, 0), ("balanced", 0.8, 0), ("comfortable", 2.5, 0),
                    ("chosen", comfort * 3.0, 0), ("later20", max(comfort, 0.8) * 3.0, 20),
                    ("later40", max(comfort, 0.8) * 3.0, 40)]
        found: dict[tuple, dict] = {}
        for tag, w, offset in variants:
            j = self._raptor(O, D, minute + offset, daytype, w, max_transfers)
            if j is None:
                continue
            sig = tuple((l["route"], l["from_place"], l["to_place"]) for l in j["legs"]) + (offset,)
            if sig in found:
                found[sig]["tags"].append(tag)
            else:
                j["tags"] = [tag]
                j["depart_offset_min"] = offset
                found[sig] = j
        # alternatives: ban the longest leg's route of each distinct journey and re-plan
        for j in list(found.values())[:2]:
            if j["depart_offset_min"]:
                continue
            main = max(j["legs"], key=lambda l: l["ride_min"])["base"]
            ban = frozenset(r for r in self.routes if self.routes[r].base == main)
            alt = self._raptor(O, D, minute, daytype, comfort * 3.0, max_transfers, ban)
            if alt is None:
                continue
            sig = tuple((l["route"], l["from_place"], l["to_place"]) for l in alt["legs"]) + (0,)
            if sig not in found and alt["duration_min"] <= 1.6 * j["duration_min"] + 10:
                alt["tags"], alt["depart_offset_min"] = ["alternative"], 0
                found[sig] = alt
        opts = list(found.values())
        now = [o for o in opts if not o["depart_offset_min"]]
        if now:
            calm_now = min((o["worst_level"], o["crowd_penalty_min"]) for o in now)
            opts = now + [o for o in opts if o["depart_offset_min"]
                          and (o["worst_level"], o["crowd_penalty_min"]) < calm_now]
        for o in opts:
            # crowding is felt mostly by standees: a likely seat discounts the crowding penalty
            felt = o["crowd_penalty_min"] * (1 - 0.6 * o["seat_pct"] / 100)
            o["score"] = o["duration_min"] + comfort * 3.0 * felt
        opts.sort(key=lambda o: o["score"])
        opts = opts[:n_options]
        if opts:
            fastest = min(opts, key=lambda o: o["arrive_min"])
            calm = min(opts, key=lambda o: (-(o["seat_pct"] // 10), o["worst_level"], o["crowd_penalty_min"],
                                            o["arrive_min"]))
            fastest.setdefault("badges", []).append("Fastest")
            if calm is not fastest:
                calm.setdefault("badges", []).append("Most comfortable")
            for o in opts:
                if o["depart_offset_min"]:
                    o.setdefault("badges", []).append(f"Leave {o['depart_offset_min']} min later")
                o["summary"] = self._summary(o)
        return {"origin": [self.place_label[i] for i in O], "destination": [self.place_label[i] for i in D],
                "depart": _hhmm(minute), "daytype": daytype, "comfort": comfort, "options": opts}

    def _raptor(self, O, D, t0, daytype, w, max_transfers, banned=frozenset()):
        """Round-based search (RAPTOR) over frequency-based routes.

        Round k = journeys with k+1 buses. Cost = waiting + transfer penalty +
        in-vehicle minutes x (1 + w * (crowding multiplier - 1)), so w=0 is the
        fastest trip and larger w prefers emptier buses.
        """
        INF = math.inf
        mult = [self.levels_cfg[k]["perceived_multiplier"] for k in LEVELS]
        # labels[k][place] = (cost, arrival_minute, parent); parent = (route, bpos, apos, from_place)
        labels = [{p: (0.0, float(t0), None) for p in O}]
        marked = set(O)
        for k in range(max_transfers + 1):
            prev = labels[-1]
            cur = dict(prev)
            new = set()
            for rn in {r for p in marked for r, _ in self.place_routes[p]} - banned:
                R = self.routes[rn]
                wait = min(30.0, 30.0 / max(R.dep_per_hour, 0.1))  # expected wait = headway / 2
                lvm = R.lv.get(daytype)
                board = None  # (cost, minute, pos, place)
                cum_t = cum_c = 0.0
                for pos, p in enumerate(R.places):
                    if board is not None:
                        h = int((board[1] + cum_t) // 60) % 24
                        lv = lvm[h][pos - 1] if lvm is not None else 0
                        dt = R.dt_h[h]
                        cum_t += dt
                        cum_c += dt * (1 + w * (mult[lv] - 1))
                        cost = board[0] + cum_c
                        if R.anchor[pos] and cost < cur.get(p, (INF,))[0]:
                            cur[p] = (cost, board[1] + cum_t, (rn, board[2], pos, board[3]))
                            new.add(p)
                    if p in marked and R.anchor[pos]:
                        c0, a0, par = prev[p]
                        if par is not None and not self.can_transfer(par[0], par[2], rn, pos):
                            continue
                        xfer = 0.0 if par is None else 4.0
                        cand = (c0 + wait + xfer, a0 + wait + xfer, pos, p)
                        if board is None or cand[0] < board[0] + cum_c:
                            board, cum_t, cum_c = cand, 0.0, 0.0
            labels.append(cur)
            marked = new
            if not new:
                break
        final = labels[-1]
        tgt = [(final[d][0], d) for d in D if d in final and final[d][2] is not None]
        if not tgt:
            return None
        _, d = min(tgt)
        return self._unwind(labels, d, t0, daytype)

    def _unwind(self, labels, d, t0, daytype):
        legs = []
        p, k = d, len(labels) - 1
        while k > 0:
            lab = labels[k][p]
            if lab[2] is None:
                break
            # find the earliest round that holds this same label (its boarding came from round-1)
            while k > 1 and labels[k - 1].get(p) == lab:
                k -= 1
            rn, bpos, apos, from_p = lab[2]
            btime = labels[k - 1][from_p][1] + min(30.0, 30.0 / max(self.routes[rn].dep_per_hour, 0.1))
            if labels[k - 1][from_p][2] is not None:
                btime += 4.0
            legs.append(self._leg(rn, bpos, apos, btime, daytype))
            p, k = from_p, k - 1
        legs.reverse()
        if not legs:
            return None
        arrive = legs[-1]["arrive_min"]
        ride = sum(l["ride_min"] for l in legs)
        pen = sum(l["crowd_penalty_min"] for l in legs)
        worst = max(l["worst_level_idx"] for l in legs)
        conf = min(l["confidence_idx"] for l in legs)
        # chance of sitting on every leg from the start (legs treated as independent)
        seat_all = float(np.prod([l["seat_board_pct"] / 100 for l in legs]))
        return {"seat_pct": round(100 * seat_all), "legs": legs, "depart_min": t0, "arrive_min": arrive, "duration_min": round(arrive - t0, 1),
                "ride_min": round(ride, 1), "transfers": len(legs) - 1, "crowd_penalty_min": round(pen, 1),
                "worst_level": worst, "worst_level_label": self.levels_cfg[LEVELS[worst]]["label"],
                "confidence": ["low", "medium", "high"][conf]}

    def _leg(self, rn, bpos, apos, btime, daytype):
        R = self.routes[rn]
        mult = {k: self.levels_cfg[k]["perceived_multiplier"] for k in LEVELS}
        t = btime
        stretches = []
        for s in range(bpos, apos):
            h = int(t // 60) % 24
            load = float(self.route_loads(R, daytype, h)[s]) * self.scale
            lv = int(self.level_of([load / self.scale], R.bus)[0])
            dt = R.mps * self.cong[h]
            C = R.conf.get(daytype)
            conf = int(C[h, s]) if C is not None else 0
            V = R.cv.get(daytype)
            seat = float(self.seat_prob(load / self.scale, V[h, s] if V is not None else 0.25, R.bus["seats"]))
            stretches.append({"from": R.stops[s], "to": R.stops[s + 1], "load": round(load, 1),
                              "level": LEVELS[lv], "minutes": round(dt, 1), "confidence": conf,
                              "seat_pct": int(np.clip(round(100 * seat), 3, 97))})
            t += dt
        ride = t - btime
        pen = sum(x["minutes"] * (mult[x["level"]] - 1) for x in stretches)
        worst = max(LEVELS.index(x["level"]) for x in stretches)
        # trip-level confidence: ticket-weighted share of well-sampled stretches
        confs = [x["confidence"] for x in stretches]
        cidx = int(round(np.average(confs, weights=[x["minutes"] for x in stretches])))
        wait = min(30.0, 30.0 / max(R.dep_per_hour, 0.1))
        # seat when you board, and the first stop where a seat is more likely than not
        seat_board = stretches[0]["seat_pct"]
        mins, seat_after = 0.0, None
        for x in stretches:
            if x["seat_pct"] >= 50:
                seat_after = round(mins)
                break
            mins += x["minutes"]
        return {"route": rn, "seat_board_pct": seat_board, "seat_after_min": seat_after,
                "seat_tip": self.better_boarding(R, bpos, btime, daytype, seat_board),
                "bpos": bpos, "apos": apos, "base": R.base, "dir": R.dir, "from": R.stops[bpos], "to": R.stops[apos],
                "from_place": R.places[bpos], "to_place": R.places[apos], "board_min": btime,
                "board": _hhmm(btime), "arrive_min": t, "arrive": _hhmm(t), "stops": apos - bpos,
                "ride_min": round(ride, 1), "wait_min": round(wait, 1),
                "headway_min": round(60 / max(R.dep_per_hour, 0.1), 1), "crowd_penalty_min": round(pen, 1),
                "worst_level_idx": worst, "worst_level": LEVELS[worst], "confidence_idx": cidx,
                "stretches": stretches,
                "bunch": self.insights.bunch(rn, int(btime // 60) % 24, daytype) if self.insights else None}

    def better_boarding(self, R: Route, bpos: int, btime: float, daytype: str, seat_here: int,
                        max_back: int = 6) -> dict | None:
        """Would boarding a few stops earlier on the same route give a much better chance of a seat?

        Buses fill up as they go, so an earlier stop often has free seats. Looks back up to
        `max_back` stops (only stops with enough tickets to trust their position) and suggests
        the best one if it gains at least 20 points of seat chance.
        """
        if seat_here >= 75 or bpos == 0:
            return None
        V = R.cv.get(daytype)
        best = None
        back_min = 0.0
        for j in range(bpos - 1, max(-1, bpos - 1 - max_back), -1):
            h = int((btime - back_min) // 60) % 24
            back_min += R.dt_h[h]
            if not R.anchor[j]:
                continue
            load = float(self.route_loads(R, daytype, h)[j])
            seat = int(np.clip(round(100 * float(self.seat_prob(load, V[h, j] if V is not None else 0.25, R.bus["seats"]))), 3, 97))
            if best is None or seat > best["seat_pct"] + 2:
                best = {"pos": j, "stop": R.stops[j], "seat_pct": seat, "stops_back": bpos - j,
                        "minutes_back": round(back_min)}
        if best and best["seat_pct"] - seat_here >= 20:
            return best
        return None

    def _summary(self, o) -> str:
        legs = " → ".join(l["route"] for l in o["legs"])
        worst = o["worst_level_label"].lower()
        crowded = [l for l in o["legs"] if l["worst_level_idx"] >= 2]
        msg = f"{round(o['duration_min'])} min via {legs}"
        if crowded:
            mins = sum(x["minutes"] for l in crowded for x in l["stretches"] if LEVELS.index(x["level"]) >= 2)
            msg += f"; usually crowded for about {round(mins)} min"
        else:
            msg += f"; at worst {worst}"
        return msg + f"; {o['seat_pct']}% chance of a seat"

    # ----------------------------------------------------------- profiles
    def route_profile(self, rn: str, daytype: str = "weekday") -> dict:
        R = self.routes[rn]
        L = R.load.get(daytype, np.zeros((24, max(len(R.stops) - 1, 1)))) * self.scale
        lv = self.level_of(L / self.scale, R.bus)
        C = R.conf.get(daytype, np.zeros_like(lv))
        opp = {"UP": "DOWN", "DOWN": "UP"}.get(R.dir)
        twin = next((r.name for r in self.routes.values() if r.base == R.base and r.dir == opp), None)
        return {"route": rn, "base": R.base, "dir": R.dir, "twin": twin, "stops": R.stops,
                "coords": [self.coords.get(s) for s in R.stops], "min_per_stop": round(R.mps, 2),
                "dep_per_hour": round(R.dep_per_hour, 1), "headway_min": round(60 / R.dep_per_hour, 1),
                "deph_source": R.deph_source, "bus": R.bus, "load": np.round(L, 1).tolist(),
                "level": lv.tolist(), "confidence": C.tolist(),
                "seat_pct": np.round(100 * self.seat_prob(L / self.scale, R.cv.get(daytype, np.full_like(L, 0.25)),
                                                          R.bus["seats"])).clip(3, 97).astype(int).tolist()}

    def search_routes(self, q: str, limit: int = 20) -> list[dict]:
        ql = q.upper().strip()
        out = []
        for R in self.routes.values():
            if not ql or R.name.startswith(ql) or R.base == ql:
                L = R.load.get("weekday")
                peak = float(L.max() * self.scale) if L is not None else 0
                out.append({"route": R.name, "base": R.base, "dir": R.dir, "from": R.stops[0],
                            "to": R.stops[-1], "stops": len(R.stops), "peak_load": round(peak, 1),
                            "dep_per_hour": round(R.dep_per_hour, 1)})
        out.sort(key=lambda r: (r["route"] != ql, len(r["route"]), r["route"]))
        return out[:limit]

    def hotspots(self, daytype="weekday", hour: int | None = None, limit=25, min_conf=1) -> list[dict]:
        rows = []
        for R in self.routes.values():
            L = R.load.get(daytype)
            if L is None:
                continue
            C = R.conf[daytype]
            hours = [hour] if hour is not None else range(5, 23)
            for h in hours:
                ok = C[h] >= min_conf
                if not ok.any():
                    continue
                Lh = np.where(ok, L[h], 0) * self.scale
                s = int(Lh.argmax())
                peak = float(Lh[s])
                comfort_cap = R.bus["seats"] + R.bus["standing_comfort"]
                if peak <= comfort_cap:
                    continue
                flow = peak * R.dep_per_hour
                extra = math.ceil(flow / comfort_cap - R.dep_per_hour)
                over = Lh > comfort_cap
                rows.append({"route": R.name, "hour": h, "from": R.stops[s], "to": R.stops[s + 1],
                             "peak_load": round(peak, 1), "occupancy_pct": round(100 * peak / comfort_cap),
                             "level": LEVELS[int(self.level_of([peak / self.scale], R.bus)[0])],
                             "crowded_stretches": int(over.sum()), "dep_per_hour": round(R.dep_per_hour, 1),
                             "extra_buses_per_hour": extra, "pax_per_hour": round(flow),
                             "confidence": ["low", "medium", "high"][int(C[h, s])]})
        rows.sort(key=lambda r: (-r["peak_load"] * r["dep_per_hour"]))
        # one row per route (its worst hour) unless an hour was requested
        if hour is None:
            seen, uniq = set(), []
            for r in rows:
                if r["route"] not in seen:
                    seen.add(r["route"]); uniq.append(r)
            rows = uniq
        return rows[:limit]

    def summary(self) -> dict:
        v = self.validation
        hp = v["hourly_profile"]
        cs = v["crowded_share_by_hour"]
        hold = dict(zip(v["holdout"]["metric"], v["holdout"]["value"]))
        dtt = dict(zip(v["daytype_totals"]["daytype"], v["daytype_totals"]["boardings_per_day"]))
        return {"info": self.info, "scale": self.scale, "routes": len(self.routes), "places": len(self.place_label),
                "hourly_profile": {dt: g.sort_values("hour")["per_day"].round(1).tolist()
                                   for dt, g in hp.groupby("daytype")},
                "crowded_share_by_hour": cs.sort_values("hour")["share_crowded"].round(4).tolist(),
                "holdout": hold, "daytype_totals": dtt,
                "ml": (v["ml"].round(4).astype(object).where(v["ml"].notna(), None).to_dict("records")
                       if "ml" in v else []),  # NaN is not valid JSON
                "top_stretches": v["top_stretches"].head(15).to_dict("records")}


def _hhmm(m: float) -> str:
    m = int(round(m)) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"

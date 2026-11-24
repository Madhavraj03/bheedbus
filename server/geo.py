"""Stop locations from OpenStreetMap, matched to ticket stop names.

No GTFS needed. `data/geo/osm_bus_stops.json` (named bus stops) and
`data/geo/osm_places.json` (metro stations, neighbourhoods, landmarks) come from
the free Overpass API (`python -m server.geo --fetch`). For each route we:

  1. find candidate points for every stop name (exact normalised name, then
     fuzzy; each part of "A / B" names is tried);
  2. pick one candidate per stop with a Viterbi pass that prefers good name
     matches and short hops between consecutive stops, so a common name in two
     parts of the city resolves to the one that fits the route;
  3. drop picks that jump implausibly far, and place unmatched stops between
     their matched neighbours (marked "approx").
"""
from __future__ import annotations

import difflib
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEO = ROOT / "data" / "geo"
BBOX = "28.35,76.80,28.95,77.55"  # Delhi NCR
MIRRORS = ["https://overpass.kumi.systems/api/interpreter", "https://overpass.private.coffee/api/interpreter",
           "https://overpass-api.de/api/interpreter"]
QUERIES = {
    "osm_bus_stops.json": f'node["highway"="bus_stop"]["name"]({BBOX});'
                          f'node["public_transport"="platform"]["bus"="yes"]["name"]({BBOX});',
    "osm_places.json": f'node["railway"="station"]["name"]({BBOX});'
                       f'node["place"~"^(suburb|neighbourhood|village|quarter|locality|hamlet)$"]["name"]({BBOX});'
                       f'node["amenity"~"^(bus_station|hospital|college|university)$"]["name"]({BBOX});',
}

ABBR = [(r"\bi\s*s\s*b\s*t\b", "isbt"), (r"\bx[\s-]?ing\b|\bxing\b|\bcrossing\b|\bchowk\b", "crossing"),
        (r"\bsec\b|\bsector\b", "sector"), (r"\bph\b|\bphase\b", "phase"), (r"\brd\b", "road"),
        (r"\bmkt\b", "market"), (r"\bstn\b|\bsation\b", "station"), (r"\bhosp\b", "hospital"),
        (r"\bngr\b", "nagar"), (r"\benclave\b|\benc\b", "enclave"), (r"\bcollage\b", "college"),
        (r"\bgaon\b", "village"), (r"\bmor\b|\bmore\b", "more"), (r"\bdda\b", "dda"), (r"\bno\b\.?", "no")]
NOISE = r"\b(bus stop|bus stand|bus terminal|terminal|stop|the|opp|opposite|near|gate no ?\d*|t)\b"


def norm(s: str) -> str:
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[().,'\-_/]", " ", s)
    for pat, rep in ABBR:
        s = re.sub(pat, rep, s)
    s = re.sub(NOISE, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parts(name: str) -> list[str]:
    """'Rail Bhawan / Krishi Bhawan (Central Sectt)' -> whole name, each part, and text in brackets."""
    out = [name] + [p for p in re.split(r"/|\||\(|\)", name) if p.strip()]
    return list(dict.fromkeys(k for k in (norm(p) for p in out) if len(k) >= 3))


def km(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def fetch(force: bool = False) -> None:
    GEO.mkdir(parents=True, exist_ok=True)
    for fname, body in QUERIES.items():
        dst = GEO / fname
        if dst.exists() and not force:
            continue
        q = f"[out:json][timeout:150];({body});out body;"
        for i, url in enumerate(MIRRORS * 2):
            try:
                req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": q}).encode(),
                                             headers={"User-Agent": "BheedBus/1.0 (hackathon prototype)"})
                with urllib.request.urlopen(req, timeout=200) as r:
                    data = json.loads(r.read())
                dst.write_text(json.dumps(data))
                print(f"{fname}: {len(data['elements'])} points from {url}")
                break
            except Exception as e:  # busy mirrors return 429/504: back off and try the next
                print(f"{url}: {e}")
                time.sleep(5 * (i + 1))


PHOTON = "https://photon.komoot.io/api/"
POINTLIKE = {"highway", "amenity", "railway", "place", "public_transport", "building", "shop", "leisure",
             "tourism", "office", "landuse", "historic"}


class Geo:
    def __init__(self):
        self.points: list[tuple[float, float, str, str]] = []  # lat, lon, name, kind
        for fname, kind in (("osm_bus_stops.json", "bus_stop"), ("osm_places.json", "place")):
            f = GEO / fname
            if not f.exists():
                continue
            for e in json.loads(f.read_text())["elements"]:
                t = e.get("tags", {})
                k = "metro" if t.get("railway") == "station" or t.get("station") == "subway" else kind
                for nm in {t.get("name"), t.get("name:en"), t.get("alt_name")} - {None}:
                    self.points.append((e["lat"], e["lon"], nm, k))
        self.by_key: dict[str, list[int]] = defaultdict(list)
        for i, (_, _, nm, _) in enumerate(self.points):
            for k in parts(nm):
                self.by_key[k].append(i)
        self.keys = list(self.by_key)
        self._cand: dict[str, list] = {}
        self._route: dict[str, list] = {}
        self.complete: dict[str, bool] = {}  # False while some stops still wait for the geocoder
        self._photon_file = GEO / "photon_cache.json"
        self._photon = json.loads(self._photon_file.read_text()) if self._photon_file.exists() else {}
        self._photon_idx: dict[str, list[int]] = {}

    def geocode(self, name: str) -> list[int]:
        """Fallback for stops OSM's bus-stop list lacks: Photon (OSM-based, free, no key). Cached on disk."""
        if name not in self._photon:
            q = urllib.parse.urlencode({"q": f"{name}, Delhi", "limit": 4, "bbox": "76.80,28.35,77.55,28.95"})
            try:
                req = urllib.request.Request(f"{PHOTON}?{q}", headers={"User-Agent": "BheedBus/1.0 (hackathon prototype)"})
                with urllib.request.urlopen(req, timeout=6) as r:
                    feats = json.loads(r.read()).get("features", [])
            except Exception:
                return []  # offline or busy: try again next time
            keep = []
            for f in feats:
                pr = f.get("properties", {})
                nm = pr.get("name") or ""
                if pr.get("osm_key") not in POINTLIKE or not nm:
                    continue
                # the returned name must share the stop name's first word
                if not set(norm(nm).split()[:1]) & set(norm(name).split()[:2]):
                    continue
                lon, lat = f["geometry"]["coordinates"]
                keep.append([lat, lon, nm])
            self._photon[name] = keep
            self._photon_file.write_text(json.dumps(self._photon))
            time.sleep(0.15)  # be gentle with the free service
        if name not in self._photon_idx:
            self._photon_idx[name] = []
            for lat, lon, nm in self._photon[name]:
                self.points.append((lat, lon, nm, "geocoded"))
                self._photon_idx[name].append(len(self.points) - 1)
        return self._photon_idx[name]

    @property
    def ready(self) -> bool:
        return bool(self.points)

    def candidates(self, name: str, limit: int = 6) -> list[tuple[int, float]]:
        """[(point index, name score 0-1)] for a ticket stop name."""
        if name in self._cand:
            return self._cand[name]
        found: dict[int, float] = {}
        for k in parts(name):
            boost = 1.0 if k == norm(name) else 0.92  # whole-name matches beat partial ones
            for i in self.by_key.get(k, []):
                found[i] = max(found.get(i, 0), boost)
            if len(found) < 2:
                for kk in difflib.get_close_matches(k, self.keys, n=4, cutoff=0.86):
                    if kk.split()[0][:4] != k.split()[0][:4]:  # "jaitpur crossing" is not "gazipur crossing"
                        continue
                    sc = difflib.SequenceMatcher(None, k, kk).ratio() * boost * 0.95
                    for i in self.by_key[kk]:
                        found[i] = max(found.get(i, 0), sc)
        ranked = sorted(found.items(), key=lambda kv: -kv[1])[:limit * 3]
        # keep the best per distinct location, preferring bus stops over metro/places
        kind_w = {"bus_stop": 1.0, "metro": 0.97, "place": 0.9, "geocoded": 0.88}
        out = sorted(((i, s * kind_w[self.points[i][3]]) for i, s in ranked), key=lambda x: -x[1])[:limit]
        self._cand[name] = out
        return out

    def route(self, key: str, stops: list[str], geocode_budget: int = 30) -> list[dict]:
        if key in self._route and self.complete.get(key, True):
            return self._route[key]
        cands = [self.candidates(s) for s in stops]
        complete = True
        for i, s in enumerate(stops):
            if not cands[i] and (s in self._photon or geocode_budget > 0):
                geocode_budget -= s not in self._photon  # only new lookups use the budget
                cands[i] = [(j, 0.8) for j in self.geocode(s)]
            elif not cands[i]:
                complete = False
        idx = [i for i, c in enumerate(cands) if c]
        chosen: dict[int, int] = {}
        if idx:
            # Viterbi over stops that have candidates: emission = poor name match,
            # transition = hop longer than plausible for the number of stops in between
            INF = float("inf")
            prev_cost = {ci: 4 * (1 - sc) for ci, (_, sc) in enumerate(cands[idx[0]])}
            back: list[dict] = [{}]
            for a, b in zip(idx, idx[1:]):
                gap = b - a
                cur, bp = {}, {}
                for cj, (pj, sj) in enumerate(cands[b]):
                    best, arg = INF, None
                    for ci, (pi, _) in enumerate(cands[a]):
                        d = km(self.points[pi], self.points[pj])
                        cost = prev_cost[ci] + max(0.0, d - 1.4 * gap) * 1.5 + 0.05 * d
                        if cost < best:
                            best, arg = cost, ci
                    cur[cj], bp[cj] = best + 4 * (1 - sj), arg
                prev_cost = cur
                back.append(bp)
            c = min(prev_cost, key=prev_cost.get)
            for n in range(len(idx) - 1, -1, -1):
                chosen[idx[n]] = cands[idx[n]][c][0]
                if n:
                    c = back[n][c]
            # drop outliers that sit far from both matched neighbours
            # drop implausible jumps one at a time, worst first, so one bad pick
            # cannot also knock out its correct neighbour
            while len(chosen) > 2:
                keys = sorted(chosen)
                worst, excess = None, 0.0
                for n, i in enumerate(keys):
                    nb = [keys[m] for m in (n - 1, n + 1) if 0 <= m < len(keys)]
                    e = min(km(self.points[chosen[i]], self.points[chosen[j]]) - (3 + 1.4 * abs(j - i)) for j in nb)
                    if e > excess:
                        worst, excess = i, e
                if worst is None:
                    break
                del chosen[worst]
        out = []
        keys = sorted(chosen)
        for i, name in enumerate(stops):
            if i in chosen:
                p = self.points[chosen[i]]
                out.append({"stop": name, "lat": round(p[0], 6), "lon": round(p[1], 6), "match": p[2],
                            "kind": p[3], "approx": p[3] in ("place", "geocoded")})
                continue
            lo = max((k for k in keys if k < i), default=None)
            hi = min((k for k in keys if k > i), default=None)
            if lo is not None and hi is not None:
                f = (i - lo) / (hi - lo)
                a, b = self.points[chosen[lo]], self.points[chosen[hi]]
                out.append({"stop": name, "lat": round(a[0] + f * (b[0] - a[0]), 6),
                            "lon": round(a[1] + f * (b[1] - a[1]), 6), "match": None, "kind": "interpolated",
                            "approx": True})
            else:
                out.append({"stop": name, "lat": None, "lon": None, "match": None, "kind": "unknown", "approx": True})
        self._route[key] = out
        self.complete[key] = complete
        return out


if __name__ == "__main__":
    if "--fetch" in sys.argv:
        fetch(force="--force" in sys.argv)


_shared: "Geo | None" = None


def shared() -> "Geo":
    """One Geo for the whole server (the web map and WhatsApp map images share its caches)."""
    global _shared
    if _shared is None:
        _shared = Geo()
    return _shared


def points(rn: str, stops: list[str]) -> list[dict]:
    """Stop positions for a route without touching the network (OSM matches + cached geocodes)."""
    g = shared()
    return g._route.get(rn) or g._route.get(rn + "#quick") or g.route(rn + "#quick", stops, geocode_budget=0)

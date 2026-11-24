# BheedBus: technical note

BheedBus estimates how crowded Delhi buses are, by route, direction, stretch between stops, day type and hour. It shows this next to every option in a journey planner. Input: 2,559,796 digital tickets from 1–31 October 2026. Static GTFS is optional; the model runs without it. The whole pipeline reruns in under a minute with `python pipeline/build.py`.

## 1. The key fact about the data

Each bus logs only about 10 digital tickets a day. These tickets are a **sample** of riders, not a count. Cash/ETM tickets, passes and free pink tickets are missing. A trip-by-trip running count would therefore show 1–3 people on almost every bus, which is wrong. So the model works with **expected load**: it adds up every ticket that crosses a stretch in an hour, scales the sample to all riders, and divides by the number of buses that pass.

## 2. Method

| Step | What we do | Why |
|---|---|---|
| Clean | Parse the IST time, split the route into base + direction (`717AUP` → `717A`, UP), normalise stop names, mark 2 Oct and 20 Oct (Dussehra) as holidays | Holidays act like weekends |
| Stop order | Every ticket says "A comes before B" on its route. We find the stop order that agrees with the most tickets: a Kemeny-style insertion search, seeded with each stop's boarding share, plus the reversed opposite direction at half weight | GTFS is not needed. 99.9% of tickets agree with the inferred order |
| Running time | Consecutive tickets on the same vehicle and route give minutes per stop. City hourly congestion factors range from 0.7 to 1.5. Sparse routes shrink toward a 93-min typical trip | Stops nobody books from are missing, so one "position" on a sparse route can hide several real stops |
| Buses per hour | Chao1 richness estimator on vehicles seen per route per weekday. A bus counts toward a route only if that route is ≥25% of its tickets that day, weighted by its share of the day. Departures per hour = fleet × 60 / (2 × trip + layovers) | Total fleet comes out at 6,806, close to the ~5,900 distinct buses actually seen per day plus the unseen ones |
| Flow | Each ticket rides every stretch from its start to its planned end. Its clock time shifts forward by running time, so long rides cross into later hours | 32.6M ticket-stretch rows |
| Expansion | Two anchors. **Ridership:** 4.0M weekday boardings ÷ 102k digital gives ×39. **Capacity:** the busiest well-sampled bus-hours (p99) must not exceed crush load, which scales that by 0.50. The final factor is ×19.5, about 2.0M implied weekday boardings. Per route, app adoption is partly pooled (α = 0.75) using tickets per bus | A single city factor made high-adoption routes look physically impossible (150 people on a 100-capacity bus) |
| Level | Bus: 40 seats, comfortable to 70, crush 100 (12 m; a 9 m profile is in `config.json`). **Seats available** ≤ 85% of seats · **Standing room** ≤ 70 · **Crowded** ≤ 100 · **Packed** above that | What a commuter reads in one second |
| Confidence | **High:** ≥60 tickets behind the cell and week-to-week CV ≤ 0.35. **Medium:** ≥15 tickets. **Low:** anything else, and every night hour | Shown on every trip, stretch and heat-map cell |

### Journey planner

The planner runs a round-based (RAPTOR-style) search over routes with up to 2 changes. Waiting time is half the headway, and a change adds 4 minutes. Each stretch costs `minutes × (1 + w × (m − 1))`, where `m` is a perceived-time multiplier from the crowding literature (Wardman & Whelan 2011): 1.0 for seats, 1.35 for standing, 1.75 for crowded, 2.2 for packed. The comfort slider sets `w`. The planner runs several `w` values, then re-plans with the main route banned to find alternatives. It also tries leaving 20 and 40 minutes later, and keeps those only if they are calmer.

**Stop names are not unique.** "Super Bazar" is the name of stops both near Anand Vihar and at Connaught Place. A transfer between two routes is allowed only if they run together through the stop (they share a neighbouring stop), or a third route through it touches both. The check is pairwise and never chained. Stops with fewer than 5 tickets on a route have no reliable position, so the planner never boards or alights there.

### Chance of a seat

Each stretch and hour also gets a **probability of a free seat when you board**. The bus load is modelled as lognormal around the estimated mean. Its spread combines two things:

- **Day-to-day variation**, measured per cell from the 20 weekdays (11 weekend days). Sampling noise is removed first: daily ticket counts are roughly Poisson, so we keep only the variance above 1.1 × the mean. Cells with few tickets are shrunk toward the city median CV of 0.25.
- **Bus-to-bus variation** within the hour (bunching), assumed at a CV of 0.35 (`config.json`).

The displayed figure is capped at 3–97%, because the model cannot justify certainty. A trip's seat chance multiplies its legs. The planner's comfort score discounts crowding by 60% × seat chance, since a rider who is seated barely feels the standees.

### Fleet rebalancer (operators)

The rebalancer moves buses between routes for a chosen window, such as the 08–10 morning peak, with the total fleet unchanged. Two assumptions keep it simple:

- departures per hour scale with fleet: `dep' = dep × (F+Δ)/F`;
- passenger flow stays the same (no induced or lost demand).

The objective is **crowded passenger-minutes**: passengers crossing a stretch × minutes on it, counted wherever load per bus exceeds comfortable capacity, on medium/high-confidence stretches only. A greedy optimiser repeatedly moves one bus from the route that loses least to the route that gains most. Ties go to the donor that stays emptiest. Every donor keeps at least 70% of its fleet, and routes with fewer than 2 estimated buses are left out. On weekday 08–10 it moves 40 of ~6,800 buses, cuts crowded passenger-minutes by 7.8%, and saves about 3,800 crowded passenger-hours a day.

### Hinglish voice

The Ask box takes speech through the browser's Web Speech API (`en-IN` writes Hinglish in Latin script; `hi-IN` writes Devanagari) and can read answers aloud. With Claude, any language works: stop names are transliterated to English for the tools, and the reply comes back in the user's style. Offline, a Hinglish normaliser handles patterns like "X se Y shaam 6 baje, seat chahiye". It understands se/tak, baje with subah/shaam/raat/dopahar, "seat chahiye" and "jaldi", and replies in Hinglish.

### Route maps without GTFS

Stop coordinates come from OpenStreetMap. Overpass supplies 2,234 named bus stops and 2,803 metro stations, neighbourhoods and landmarks in Delhi NCR; the Photon geocoder fills in stops that list lacks. Both are free and need no key, and results are cached in `data/geo/`.

Matching works in three steps:
1. Names are normalised (ISBT, Xing → crossing, Sec → sector, …), and each part of a "A / B (C)" name is tried.
2. A Viterbi pass along the route picks, for each stop, the candidate that best balances name match against short hops between consecutive stops. This resolves common names that exist in several places.
3. Implausible jumps are removed one at a time, and unmatched stops are placed between their matched neighbours. These are shown hollow on the map.

On the 300 busiest routes, 59% of passenger volume falls on an exact OSM bus stop and 7% on a metro station or landmark. The rest is geocoded or placed between neighbours. When a GTFS feed is loaded, its coordinates take priority.

### Machine learning for quiet routes

About 1.2 million of 1.5 million stretch-hour cells rest on fewer than 15 tickets. Their raw mean is noisy: often "0 riders" just because nobody happened to buy a digital ticket. A gradient-boosting model (scikit-learn `HistGradientBoostingRegressor`, Poisson loss) predicts each cell from:

- the route's hourly total;
- its per-stretch totals, and their "independence" product;
- neighbouring stretches;
- hour, day type and position on the route;
- route size and buses per hour.

The result is blended with the raw mean as `n/(n+k)` (k = 160, tuned on training weeks), **only for cells with fewer than 15 tickets**. Medium and busy cells keep their own data.

Evaluation never peeks:
1. The model is trained with features from weeks 1–2 and a target of week 3, then tested with features from weeks 1–3 against week 4.
2. Scoring uses Poisson deviance, the right yardstick for counts; MAE rewards always guessing zero.

| Cells | Deviance before → with ML | MAE |
|---|---|---|
| Quiet, week 4 | 1.539 → 0.602 (−61%) | −3.5% |
| Medium / busy, week 4 | unchanged (keep own data) | unchanged |
| Quiet, untouched 29–31 Oct | 1.048 → 0.605 (−42%) | +4.7% |
| All, untouched 29–31 Oct | 1.066 → 0.790 (−26%) | +1.3% |

**Honesty notes:**
- The "quiet cells only" rule was chosen after seeing week 4: a single global blend hurt busy cells by 11%. That is why the 29–31 Oct slice, which no step had used, serves as the final check.
- There, MAE is slightly worse while deviance is much better. The model gives small non-zero estimates where the raw mean says zero, which is better calibrated but loses to "always zero" on absolute error.
- In practice the ML removes false alarms: on quiet low-confidence stretches, "crowded" labels fall from 33 to 9 and "standing" from 5,036 to 3,856. Busy-route results are identical.
- It can be switched off with `"ml": {"enabled": false}` in `config.json`.

### What the tickets reveal on their own (City tab)

**Unusual days, found blind** (`pipeline/events.py`). Each day's digital boardings are compared with the median of the same weekday on the other normal days of the month. The spread of normal days sets the bar (robust z from the median absolute deviation); a day is *major* at |z| ≥ 3 and ≥ 12% change, *notable* at |z| ≥ 2.5 and ≥ 15%. Flagged days are left out and the baseline is fitted again. **The holiday list is not an input.** `config.json → events.known_events` is only read afterwards, to label and score what was found.

| Day | Change vs usual | Calendar |
|---|---|---|
| Fri 2 Oct | −56% (z −9.5) | Gandhi Jayanti |
| Sun 18 Oct | −23% | Navratri (Durga Ashtami) |
| Mon 19 Oct | −20% | Navratri (Maha Navami) |
| Tue 20 Oct | −48% (z −7.7) | Dussehra |
| Tue 6 Oct | −20% | **not in our calendar**: unexplained |

All 4 calendar events were found; one flagged day has no explanation we know of (weather, a strike or a data gap are candidates). On Dussehra the morning peak almost disappears while the evening holds up (City tab, hour chart). The same test per stop finds sudden local crowds: at least 2.5× the stop's usual same-weekday boardings, z ≥ 6, on at most 4 days of the month (more often means a renamed stop, not an event). The biggest clusters are Sun 25 Oct (New Delhi Railway Station Gate 2, Regal, Bangla Sahib: 9 stops) and Tue 27 Oct (Rail Bhawan / Krishi Bhawan, Rafi Marg: 5 stops). We report *where and when*, not *why*. The festival labels should be checked against an official calendar before they're quoted.

**Bus bunching, measured from tickets.** When two different buses of the same route issue tickets at the same stop a few minutes apart, they were travelling together. We count such pairs by their gap and compare short gaps (0–3 min) with long ones (10–40 min). Under random arrivals, pairs are spread evenly, so the ratio is the pair-correlation of arrivals, *g*:
- 0 = even spacing;
- 1 = arrivals as if at random;
- above 1 = buses in packs.

Thinning by the digital sample removes pairs at random and leaves *g* unchanged, so it works on ~10 tickets per bus. Each route's first and last stops are excluded (buses wait together at terminals), and route-hours are shrunk toward the city value for that hour (20 pseudo-pairs).

- City-wide *g* = 0.80: service is barely more regular than chance.
- Weekdays: *g* = 0.43 at 05:00, when buses leave depots evenly. It rises to 0.93 by 09:00 and to 1.03–1.14 after 19:00. Gaps pile up through the day, the textbook bunching pattern.
- The worst peak-hour routes reach 1.5–1.6 (879, 774, 144, 440).
- In the planner, a leg with *g* ≥ 0.85 and a headway of at least 6 min gets a tip: "buses often come in pairs; ~64% chance another comes within 5 min" (P ≈ 1 − exp(−g·w/H)). That's an approximation; an evenly spaced timetable would give 0%.

**Nudge simulator.** It shifts a share of riders in the peak hours by N minutes. With arrivals spread evenly within an hour, share × N/60 of each stretch's hourly flow moves into the next (or previous) hour. Loads, levels and seat chances are then recomputed on medium/high-confidence stretches. Example: 10% of riders at 08–10 and 17–20 leaving 20 min later (~104k riders a weekday) gives:
- −3.7% crowded passenger-hours;
- +1,864 passenger-hours seated;
- route-hours with a packed stretch from 660 to 630.

This assumes fixed demand and the same buses; it's a what-if, not a forecast. It's the free counterpart to the fleet rebalancer.

**Delhi time-lapse.** Every stretch we can place on the map, about 28k road pieces after merging routes that share a road and dropping hops over 1.6 km as likely mismatches, is coloured hour by hour. The colour is the passenger-weighted occupancy of the medium/high-confidence routes on it; the width is total riders per hour. Geometry is cached in `data/out/city_lines.json`.

**Riders check the model.** After a trip, riders can tell us how full the bus was: *⭐ Rate my last ride* in the WhatsApp menu, or the buttons on web trip cards. The bot never messages first; it only answers.

Each answer is stored with the level and seat chance the model predicted for that route, stop, hour and day type, in `data/feedback.db`. Phone numbers are kept only as salted hashes. The City tab shows agreement (exact, within one level) and predicted vs reported seat chance. With enough reports this becomes the ground-truth check that ticket data alone cannot give.

## 3. Checks against known patterns

- **Peaks:** weekday boardings peak 08:00–10:00 (14.3k/h) and 17:00–20:00 (9.9k/h). Midday sits around 2.6k/h. Crowded bus-hours: 14.6% at 09:00, 6.2% at 19:00, under 0.5% at midday.
- **Weekday vs weekend:** 102k vs 64k digital boardings a day (1.6×). Weekend peaks are lower and flatter.
- **Busy corridors:** the top stretches are 433 (Badarpur–Okhla towards ITO, 09:00), 258 (North Delhi via Timarpur), 978 (Sultanpuri–Nangloi) and 717A/B (Badarpur–Saket–Vasant Kunj). These are corridors known for crowding.
- **Holidays and festivals:** found from the tickets alone, with no calendar input: Gandhi Jayanti −56%, Dussehra −48%, Navratri Ashtami/Navami about −20%. See the table above.
- **Bunching grows through the day** (*g* 0.43 at 05:00 → above 1 after 19:00), as expected when delays accumulate on a route.
- **Week-to-week holdout:** fit on weeks 1–3, test on week 4. Route-hour peak flow correlates at r = 0.97 (Spearman 0.88). The crowding level is the same in 80% of well-sampled route-hours and 90% of all of them.

## 4. Where the estimates are least reliable

1. **Absolute level depends on the expansion factor.** The two anchors disagree (4.0M published ridership vs 2.0M capacity-consistent). That gap says digital riders are over-represented at peak and on busy corridors. Off-peak crowding is therefore probably *under*-estimated. The Operators tab has a sensitivity slider (×0.5–×2).
2. **Uneven app adoption between routes** is only partly removed (α = 0.75). A route where far more riders use the app than average still looks busier than it is.
3. **Sparse routes:** 466k of 790k weekday cells are low confidence. Their stop order, running time and fleet all rest on few tickets. Their fleet falls back to the city median for 251 route-directions.
4. **Flat service through the day:** without GTFS, every service hour gets the same number of buses. Night service (23:00–05:00) is assumed at half rate and always marked low confidence.
5. **Planned vs actual alighting:** we use the stop printed on the ticket. People who ride past it, or get off early, shift load by a stretch or two at the ends of trips.
6. **One month:** no seasons, monsoon or exam periods. The festivals in it are detected, but there are too few to model festival crowding.
7. **Rebalancer realism:** it ignores dead mileage, depot location, crew shifts and bus type compatibility (AC vs non-AC, 9 m vs 12 m). Treat its output as a ranked shortlist for schedulers, not a timetable.
8. **Seat chance:** the bus-to-bus spread (CV 0.35) is an assumption, not measured. Bunching is now measured from tickets (*g* above), but the seat-chance spread is not yet calibrated from it. GTFS-RT positions would measure it directly.

## 5. What would make it better

A GTFS feed can be dropped into the Data lab. It replaces estimated frequencies with timetabled ones and adds stop coordinates for maps (GeoJSON at `/api/geojson`). ETM cash-ticket totals per route would let us calibrate app adoption per route instead of pooling it. GTFS-RT vehicle positions would replace ticket-gap running times with measured ones and give actual trips per hour.

## 6. Reproduce

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python pipeline/build.py          # ~50 s → data/out/crowding.db
.venv/bin/python -m uvicorn server.app:app --port 8000
```

All assumptions live in `config.json`. The output database tables are `route_stops`, `routes`, `segment_load` (route, seg, daytype, hour, load, level, confidence, …), `congestion` and `val_*`.

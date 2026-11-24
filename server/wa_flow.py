"""Menu-driven BheedBus chat for WhatsApp: tap-to-choose buttons and lists.

handle(model, user, incoming, ctx) -> list of outgoing messages, in a transport-neutral form
that whatsapp.py turns into Cloud API payloads and the web tester renders as bubbles:

    {"type": "text",    "body": str, "ask_location": bool (optional: offer a share-location button)}
    {"type": "buttons", "body": str, "buttons": [(id, title), ...]}            # max 3, title <= 20 chars
    {"type": "list",    "body": str, "button": str, "rows": [(id, title, description), ...]}  # max 10
    {"type": "cta",     "body": str, "label": str, "url": str}                  # "Open map" link button
    {"type": "voice",   "text": str, "hindi": bool}                             # spoken reply
    {"type": "later",   "delay_s": float, "msg": {...}}                         # send msg after a delay (ride feedback)

incoming is {"type": "text", "text": ...}, {"type": "choice", "id": ..., "title": ...} or {"type": "location", "lat", "lon"}
(a tapped button / list row), with optional "voice": True, "lang": "hi".

Free questions that don't fit the menu go to the AI assistant, as before.
"""
from __future__ import annotations

import os
import re
import time
import urllib.parse

from . import ai
from .model import LEVELS, Model

MENU_WORDS = re.compile(r"^\s*(hi+|hello|hey|hii+|namaste|menu|start|help|options|main menu|home)\s*[!.]*\s*$", re.I)
_state: dict[str, dict] = {}
TIMEOUT = 30 * 60  # forget a half-finished menu after 30 minutes
ICON = {"seats": "🟢", "standing": "🟡", "crowded": "🟠", "packed": "🔴"}


def _t(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _now(ctx: dict) -> tuple[int, str]:
    h, m = (int(x) for x in ctx.get("now", "09:00").split(":")[:2])
    return h * 60 + m, ctx.get("day", "weekday")


def _public(path: str) -> str | None:
    from .public import url
    base = url()
    return f"{base}{path}" if base else None


def menu(greeting: str = "Namaste! 🚌 I'm *BheedBus*. I tell you how full Delhi buses usually are.") -> list[dict]:
    return [{"type": "list", "body": greeting + "\n\nWhat would you like to do? You can also just type or send a voice note.",
             "button": "Choose an option",
             "rows": [("m:trip", "🚌 Plan a trip", "Find buses with crowding and seat chance"),
                      ("m:route", "📊 How crowded is a bus", "Pick a route and a time of day"),
                      ("m:hot", "🔥 Busiest routes now", "Where buses are most overloaded"),
                      ("m:rate", "⭐ Rate my last ride", "Tell us how full the bus really was"),
                      ("m:help", "❓ How it works", "What the colours and numbers mean")]}]


def _back() -> tuple[str, str]:
    return ("m:menu", "🏠 Menu")


def handle(model: Model, user: str, inc: dict, ctx: dict) -> list[dict]:
    st = _state.get(user)
    if st and time.time() - st.get("t", 0) > TIMEOUT:
        st = None
    _state[user] = st = st or {"step": None}
    st["user"] = user
    st["t"] = time.time()
    cid = inc.get("id", "") if inc["type"] == "choice" else ""
    text = (inc.get("text") or "").strip()

    # ---------------- menu and top-level choices
    if cid in ("m:menu", "") and (cid == "m:menu" or (text and MENU_WORDS.match(text))):
        st.clear(); st.update(step=None, t=time.time())
        return menu()
    if cid == "m:help":
        return [{"type": "buttons", "body": "ℹ️ *How BheedBus works*\n\nCrowding is estimated from 2.56 million October 2026 bus tickets: how full a bus "
                 "*usually* is on each stretch at each hour (not live).\n\n🟢 Seats available\n🟡 Standing room\n🟠 Crowded\n🔴 Packed\n\n"
                 "💺 *Seat chance* = how likely a free seat is when you board.\n\nTip: just type a question like _Badarpur se Saket subah 9 baje_ or send a voice note.",
                 "buttons": [("m:trip", "🚌 Plan a trip"), _back()]}]
    if cid == "m:hot":
        return _hotspots(model, ctx)
    if cid == "m:rate":
        from .feedback import last_trip
        trip = last_trip(user)
        if not trip:
            return [{"type": "buttons", "body": "Plan a trip first, then tell me how full the bus was. 🙂",
                     "buttons": [("m:trip", "🚌 Plan a trip"), _back()]}]
        return [rate_question(trip)]
    if cid.startswith("f:"):
        return _rated(st, cid[2:])
    if cid == "m:trip":
        st.clear(); st.update(step="from", t=time.time())
        return [{"type": "text", "ask_location": True,
                 "body": "🚌 *Plan a trip*\n\nWhere are you *starting*? Type a stop or area, e.g. _Anand Vihar_, _Nehru Place_."}]
    if cid == "m:route":
        st.clear(); st.update(step="route", t=time.time())
        return [{"type": "text", "body": "📊 Which *bus route*? Type its number, e.g. _433_ or _717A_."}]

    # ---------------- trip planning steps
    if inc["type"] == "location":  # shared location (Telegram, WhatsApp): start or end at the nearest stops
        if st.get("step") not in ("from", "to"):
            st.clear(); st.update(step="from", t=time.time())
        which = st["step"]
        hits = model.insights.near(inc["lat"], inc["lon"]) if model.insights else []
        if not hits:
            return [{"type": "text", "body": "📍 I don't know any bus stops within 3 km of that spot. Type a stop name instead."}]
        label = "starting point" if which == "from" else "destination"
        return [{"type": "list", "body": f"📍 Bus stops near you. Which is your *{label}*?", "button": "Pick a stop",
                 "rows": [(f"p:{which}:{h['id']}", _t(h["name"], 24), f"{round(h['km'] * 1000, -1):.0f} m away") for h in hits]}]
    if cid.startswith("p:"):
        _, which, pid = cid.split(":", 2)
        st[which] = (int(pid), model.place_label[int(pid)] if int(pid) < len(model.place_label) else inc.get("title", ""))
        return _trip_next(model, st, ctx)
    if cid.startswith("w:"):
        st["minute"] = _now(ctx)[0] if cid == "w:now" else int(cid[2:4]) * 60 + int(cid[4:6])
        return _trip_next(model, st, ctx)
    if cid.startswith("c:"):
        st["comfort"] = float(cid[2:])
        return _trip_next(model, st, ctx)
    if cid == "a:later" and st.get("last"):
        st["minute"] = st["last"]["minute"] + 20
        return _trip_result(model, st, ctx)
    if cid == "a:swap" and st.get("last"):
        st["from"], st["to"] = st["to"], st["from"]
        st["minute"] = max(st["last"]["minute"], _now(ctx)[0])
        return _trip_result(model, st, ctx)

    # ---------------- route crowding steps
    if cid.startswith("r:"):
        st["route"] = cid[2:]
        st["step"] = "hour"
        return [{"type": "buttons", "body": f"⏰ *{st['route']}*: which time of day?",
                 "buttons": [("h:9", "🌅 Morning 9:00"), ("h:13", "☀️ Midday 1:00"), ("h:18", "🌆 Evening 6:00")]}]
    if cid.startswith("h:") and st.get("route"):
        return _route_result(model, st["route"], int(cid[2:]), ctx)

    # ---------------- typed text while a step is waiting for it
    if text and st.get("step") in ("from", "to"):
        return _pick_place(model, st, st["step"], text)
    if text and st.get("step") == "route":
        return _pick_route(model, text)
    if text and st.get("step") in ("when", "pref"):
        st["step"] = None  # they moved on: treat as a free question

    # ---------------- anything else: the AI assistant answers
    if not text:
        return menu()
    from .whatsapp import answer  # local import: whatsapp imports this module
    out = answer(model, text, user, inc.get("lang", ""), ctx)
    msgs = [{"type": "buttons", "body": out["reply"][:1020], "buttons": [_back()]}]
    r = _replan(model, out.get("plans", []), ctx)
    if r:
        msgs += _rate_later(st, r["options"][0], out["plans"][-1].get("day", "weekday"))
    if inc.get("voice"):
        hindi = inc.get("lang") == "hi" or bool(ai.HINGLISH.search(text))
        said = spoken(model, out.get("plans", []), ctx, hindi)
        if said:  # no clean sentence to say -> no voice note (better than gibberish)
            msgs.append({"type": "voice", "text": said, "hindi": hindi})
    return msgs


# ---------------------------------------------------------------- spoken replies
# Voice notes are read from a short sentence built from the trip data, not from the chat text:
# the Hindi voice can't pronounce Hinglish written in Latin letters, and symbols like * or %
# come out as noise. Hindi listeners get proper Devanagari Hindi; others get plain English.
_HI_LETTER = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                      "ए बी सी डी ई एफ जी एच आई जे के एल एम एन ओ पी क्यू आर एस टी यू वी डब्ल्यू एक्स वाई ज़ेड".split()))
_ROUTE = re.compile(r"^([A-Z]*-?)(\d+)([A-Z]*?)(UP|DOWN)?\d*$")


def _route_words(rn: str, hindi: bool) -> str:
    """'717AUP' -> '717 A up' / '717 ए अप', so the voice says the bus number naturally."""
    m = _ROUTE.match(rn.replace("_", "").replace("(", "").replace(")", "").replace("+", "").replace(" ", ""))
    if not m:
        return rn
    pre, num, let, d = m.groups()
    letters = lambda t: " ".join((_HI_LETTER.get(c, c) if hindi else c) for c in t if c.isalpha())  # noqa: E731
    way = {"UP": "अप" if hindi else "up", "DOWN": "डाउन" if hindi else "down"}.get(d or "", "")
    return " ".join(x for x in (letters(pre), num, letters(let), way) if x)


def _replan(model: Model, plans: list[dict], ctx: dict) -> dict | None:
    """Re-run the assistant's last trip plan, to get the structured result behind its text."""
    if not plans:
        return None
    p = plans[-1]
    try:
        hh_, mm_ = (int(x) for x in ai.normalize_time(p.get("time"), ctx.get("now", "09:00")).split(":"))
        r = model.plan(p["origin"], p["destination"], hh_ * 60 + mm_, p.get("day", "weekday"), float(p.get("comfort", 0.5)))
    except Exception:
        return None
    return r if not r.get("error") and r.get("options") else None


def spoken(model: Model, plans: list[dict], ctx: dict, hindi: bool) -> str | None:
    r = _replan(model, plans, ctx)
    if r is None:
        return None
    p = plans[-1]
    o = r["options"][0]
    # say the places the way the person said them ("Badarpur"), not the official stop label ("Badarpur Border (T)")
    clean = lambda x: re.sub(r"\s*\(.*?\)|\s*/.*$", "", str(x)).strip().title()  # noqa: E731
    frm, to = clean(p["origin"]), clean(p["destination"])
    h, mi = (int(x) for x in o["legs"][0]["board"].split(":"))
    if hindi:
        buses = ", फिर ".join(f"बस {_route_words(l['route'], True)}" for l in o["legs"])
        out = (f"{frm} से {to}। सबसे अच्छा विकल्प: {buses}, {h} बजकर {mi} मिनट पर। लगभग {round(o['duration_min'])} मिनट लगेंगे। "
               f"सीट मिलने का चांस {o['seat_pct']} प्रतिशत।")
        tip = next((l for l in o["legs"] if l.get("seat_tip")), None)
        if tip:
            t = tip["seat_tip"]
            out += f" सीट के लिए {t['stop']} से चढ़ें, {t['stops_back']} स्टॉप पहले। वहाँ चांस {t['seat_pct']} प्रतिशत है।"
        return out
    buses = ", then ".join(f"bus {_route_words(l['route'], False)}" for l in o["legs"])
    out = (f"From {frm} to {to}. Best option: {buses}, at {h}:{mi:02d}. About {round(o['duration_min'])} minutes, "
           f"with a {o['seat_pct']} percent chance of a seat.")
    tip = next((l for l in o["legs"] if l.get("seat_tip")), None)
    if tip:
        t = tip["seat_tip"]
        out += f" For a seat, board at {t['stop']}, {t['stops_back']} stops earlier, for a {t['seat_pct']} percent chance."
    return out


def _pick_place(model: Model, st: dict, which: str, text: str) -> list[dict]:
    hits = model.search_places(text, limit=8)
    if not hits:
        return [{"type": "text", "body": f"🤔 I couldn't find *{_t(text, 40)}*. Try a nearby stop or a shorter name (e.g. _Saket_, _ISBT_)."}]
    rows = [(f"p:{which}:{h['id']}", _t(h["name"], 24), _t(f"{h['routes']} bus routes stop here · {h['name']}", 72)) for h in hits]
    label = "starting point" if which == "from" else "destination"
    return [{"type": "list", "body": f"📍 Which *{label}* did you mean?", "button": "Pick a stop", "rows": rows}]


def _trip_next(model: Model, st: dict, ctx: dict) -> list[dict]:
    if "from" not in st:
        st["step"] = "from"
        return [{"type": "text", "body": "Where are you *starting*?", "ask_location": True}]
    if "to" not in st:
        st["step"] = "to"
        return [{"type": "text", "body": f"From *{st['from'][1]}* ✅\n\nWhere are you *going*? Type a stop or area.", "ask_location": True}]
    if "minute" not in st:
        st["step"] = "when"
        return [{"type": "buttons", "body": f"*{st['from'][1]}* → *{st['to'][1]}*\n\n⏰ When are you leaving?",
                 "buttons": [("w:now", "Now"), ("w:0900", "Morning 9:00"), ("w:1800", "Evening 6:00")]}]
    if "comfort" not in st:
        st["step"] = "pref"
        return [{"type": "buttons", "body": "What matters more to you?",
                 "buttons": [("c:0", "⚡ Fastest"), ("c:0.5", "⚖️ Balanced"), ("c:1", "💺 Get a seat")]}]
    return _trip_result(model, st, ctx)


def _trip_result(model: Model, st: dict, ctx: dict) -> list[dict]:
    minute, day = st["minute"], _now(ctx)[1]
    note = ""
    if not 5 * 60 <= minute % 1440 < 23 * 60:
        minute, note = 8 * 60, "\n_(Few buses run at night; showing 08:00.)_"
    r = model.plan(str(st["from"][0]), str(st["to"][0]), minute, day, st.get("comfort", 0.5))
    st["last"] = {"minute": minute}
    st["step"] = None
    if r.get("error") or not r.get("options"):
        return [{"type": "buttons", "body": f"😕 No bus trip found from *{st['from'][1]}* to *{st['to'][1]}*. Try a nearby stop.",
                 "buttons": [("m:trip", "🚌 New trip"), _back()]}]
    hhmm = f"{minute // 60 % 24:02d}:{minute % 60:02d}"
    lines = [f"🚌 *{st['from'][1]}* → *{st['to'][1]}*\nLeaving {hhmm}, {day}{note}\n"]
    for o in r["options"][:3]:
        tag = ", ".join(o.get("badges", [])) or "Option"
        worst = LEVELS[o["worst_level"]]
        buses = " → ".join(f"*{l['route']}* ({l['board']})" for l in o["legs"])
        lines.append(f"{ICON[worst]} *{tag}*: {round(o['duration_min'])} min\n   {buses}\n   💺 {o['seat_pct']}% seat chance · {o['worst_level_label'].lower()} at worst")
    best = r["options"][0]
    tips = []
    for l in best["legs"]:
        if l.get("seat_tip"):
            t = l["seat_tip"]
            tips.append(f"💡 *Want a seat on {l['route']}?* Board at *{t['stop']}*, {t['stops_back']} stop{'s' if t['stops_back'] > 1 else ''} "
                        f"earlier (~{t['minutes_back']} min back): {t['seat_pct']}% seat chance vs {l['seat_board_pct']}% at {l['from']}.")
        elif l["seat_board_pct"] < 50 and l.get("seat_after_min"):
            tips.append(f"🪑 On *{l['route']}* a seat usually frees up after ~{l['seat_after_min']} min.")
        b = l.get("bunch") or {}
        if b.get("tip"):
            tips.append(f"🚌🚌 *{l['route']} buses often come in pairs* at this hour. If the first is packed, "
                        f"there's ~{b['p_next_within']}% chance another comes within {b['window_min']:g} min.")
    if tips:
        lines.append("\n".join(tips))
    first = best["legs"][0]
    msgs = []
    img = trip_map(model, r, best)
    if img:
        msgs.append(img)
    msgs += [{"type": "buttons", "body": "\n\n".join(lines)[:1020] + "\n\n_Typical crowding from Oct 2026 tickets_",
             "buttons": [("a:later", "⏰ 20 min later"), ("a:swap", "🔁 Return trip"), _back()]}]
    url = _public(f"/?route={urllib.parse.quote(first['route'])}&h={int(first['board_min'] // 60) % 24}#routes")
    if url:
        msgs.append({"type": "cta", "body": f"See *{first['route']}* and its crowding on the map:", "label": "🗺️ Open map", "url": url})
    return msgs + _rate_later(st, best, day)


def _rate_later(st: dict, option: dict, day: str) -> list[dict]:
    """Remember the plan so "⭐ Rate my last ride" can ask how full that bus really was."""
    from .feedback import remember, trip_of
    trip = trip_of(option, day)
    if st.get("user"):
        remember(st["user"], trip)
    return []  # no unprompted messages: riders rate from the menu (⭐ Rate my last ride)


# ---------------------------------------------------------------- rider feedback
def rate_question(trip: dict) -> dict:
    from .feedback import REPORTS
    return {"type": "list", "button": "How was it?",
            "body": f"⭐ *Quick check:* how full was *{trip['route']}* from {_t(trip['from'], 30)} (around {trip['hour']:02d}:00)?\n\n"
                    "Your answer checks our crowding estimate against real rides.",
            "rows": [(f"f:{k}", v, d) for (k, v), d in zip(REPORTS.items(), (
                "There was a free seat", "Standing, but comfortable", "Tight, hard to move", "Could barely get on"))]}


def _rated(st: dict, reported: str) -> list[dict]:
    from . import feedback
    trip = feedback.last_trip(st.get("user", ""))
    if not trip:
        return menu("Thanks! Plan a trip first so I know which bus you mean.")
    channel = "telegram" if st.get("user", "").startswith("tg-") else "web" if st.get("user", "").startswith("sim-") else "whatsapp"
    s = feedback.record(channel, st.get("user", ""), trip, reported)
    feedback.forget(st.get("user", ""))  # one report per trip
    said = {"seats": "seats available", "standing": "standing room", "crowded": "crowded", "packed": "packed"}
    match = "✅ That matches our estimate!" if reported == trip["predicted"] else \
        f"📝 We estimated *{said.get(trip['predicted'], trip['predicted'])}*; thanks, this helps us improve."
    body = (f"🙏 Thanks! {match}\n\n📊 So far {s['n']} rider report{'s' if s['n'] != 1 else ''}: "
            f"{s['within_one_pct']}% within one level of the model.")
    return [{"type": "buttons", "body": body, "buttons": [_back()]}]


def _pick_route(model: Model, text: str) -> list[dict]:
    q = re.sub(r"\s+", "", text).upper()
    rows = model.search_routes(q, limit=10)
    if not rows:
        return [{"type": "text", "body": f"🤔 No route matches *{_t(text, 30)}*. Try a number like _433_ or _717A_."}]
    return [{"type": "list", "body": f"🚍 Which *{q}* bus?", "button": "Pick a route",
             "rows": [(f"r:{r['route']}", r["route"], _t(f"{r['from']} → {r['to']}", 72)) for r in rows]}]


def _route_result(model: Model, rn: str, hour: int, ctx: dict) -> list[dict]:
    if rn not in model.routes:
        return [{"type": "text", "body": f"Unknown route {rn}."}]
    day = _now(ctx)[1]
    p = model.route_profile(rn, day)
    loads, levels = p["load"][hour], p["level"][hour]
    n = len(p["stops"]) - 1
    i = max(range(n), key=lambda k: loads[k])
    crowded = sum(1 for k in range(n) if levels[k] >= 2)
    seat0 = p["seat_pct"][hour][0] if p.get("seat_pct") else None
    body = (f"📊 *{rn}* at {hour:02d}:00 ({day})\n{p['stops'][0]} → {p['stops'][-1]}\n\n"
            f"{ICON[LEVELS[levels[i]]]} Busiest: *{p['stops'][i]} → {p['stops'][i + 1]}*, ~{round(loads[i])} on board ({LEVELS[levels[i]]})\n"
            f"🚦 {crowded} of {n} stretches crowded\n🕒 A bus every ~{round(p['headway_min'])} min"
            + (f"\n💺 {seat0}% seat chance at the first stop" if seat0 is not None else "")
            + "\n\n_Typical crowding from Oct 2026 tickets_")
    msgs = []
    img = route_map(model, rn, hour, day)
    if img:
        msgs.append(img)
    msgs.append({"type": "buttons", "body": body, "buttons": [("m:route", "📊 Another route"), _back()]})
    url = _public(f"/?route={urllib.parse.quote(rn)}&h={hour}#routes")
    if url:
        msgs.append({"type": "cta", "body": f"See *{rn}* stop by stop on the map:", "label": "🗺️ Open map", "url": url})
    return msgs


def _hotspots(model: Model, ctx: dict) -> list[dict]:
    minute, day = _now(ctx)
    h = minute // 60
    rows = model.hotspots(day, h if 6 <= h <= 21 else None, limit=5)
    when = f"{h:02d}:00" if 6 <= h <= 21 else "their worst hour"
    if not rows:
        body = f"✅ No route is overloaded at {when} on a typical {day}."
    else:
        body = f"🔥 *Busiest routes* ({day}, {when})\n\n" + "\n".join(
            f"{ICON[r['level']]} *{r['route']}* {r['hour']:02d}:00: {_t(r['from'], 22)} → {_t(r['to'], 22)}, ~{round(r['peak_load'])} on board"
            for r in rows)
    return [{"type": "buttons", "body": body[:1020], "buttons": [("m:route", "📊 Check a route"), _back()]}]


# ---------------------------------------------------------------- map images
def _pts(model: Model, rn: str) -> list[dict]:
    from .geo import points
    return points(rn, model.routes[rn].stops)


def trip_map(model: Model, r: dict, opt: dict) -> dict | None:
    """Picture of the chosen trip: ridden stretches coloured by crowding, where to board, change and get off."""
    try:
        from .mapimage import render, segments
        lines, markers = [], []
        for k, l in enumerate(opt["legs"]):
            pts = _pts(model, l["route"])
            if sum(p["lat"] is not None for p in pts[l["bpos"]:l["apos"] + 1]) < 2:
                return None
            levels = [None] * len(pts)
            for i, s in enumerate(l["stretches"]):
                levels[l["bpos"] + i] = s["level"]
            lines += segments(pts, levels, l["bpos"], l["apos"])
            a, b = pts[l["bpos"]], pts[l["apos"]]
            if a["lat"] is not None:
                markers.append({"lat": a["lat"], "lon": a["lon"], "big": True, "color": "#0ca30c" if k == 0 else "#f5f5f5",
                                "label": (f"Board {l['route']}: {_t(l['from'], 28)}" if k == 0 else f"Change to {l['route']}")})
            if k == len(opt["legs"]) - 1 and b["lat"] is not None:
                markers.append({"lat": b["lat"], "lon": b["lon"], "big": True, "color": "#d03b3b", "label": f"Get off: {_t(l['to'], 28)}"})
            t = l.get("seat_tip")
            if t and pts[t["pos"]]["lat"] is not None:
                p = pts[t["pos"]]
                lines.append({"points": [(pp["lat"], pp["lon"]) for pp in pts[t["pos"]:l["bpos"] + 1] if pp["lat"] is not None], "level": "seats", "faint": True})
                markers.append({"lat": p["lat"], "lon": p["lon"], "color": "#2563eb", "label": f"Seat: board here ({t['seat_pct']}%)"})
        buses = " → ".join(l["route"] for l in opt["legs"])
        path = render(lines, markers, f"{_t(opt['legs'][0]['from'], 26)} → {_t(opt['legs'][-1]['to'], 26)}",
                      f"{round(opt['duration_min'])} min · {buses} · leave {opt['legs'][0]['board']} · {opt['seat_pct']}% seat chance")
        return {"type": "image", "path": str(path), "caption": f"🗺️ Your trip: {buses}"}
    except Exception as e:  # maps are a bonus; the text answer still goes out
        print("map image failed:", e)
        return None


def route_map(model: Model, rn: str, hour: int, day: str) -> dict | None:
    try:
        from .mapimage import render, segments
        pts = _pts(model, rn)
        if sum(p["lat"] is not None for p in pts) < 2:
            return None
        p = model.route_profile(rn, day)
        levels = [LEVELS[x] for x in p["level"][hour]] + [None]
        markers = [{"lat": pts[0]["lat"], "lon": pts[0]["lon"], "big": True, "color": "#111111", "label": f"Start: {_t(p['stops'][0], 28)}"},
                   {"lat": pts[-1]["lat"], "lon": pts[-1]["lon"], "big": True, "color": "#111111", "label": f"End: {_t(p['stops'][-1], 28)}"}]
        markers = [m for m in markers if m["lat"] is not None]
        path = render(segments(pts, levels, 0, len(pts) - 1), markers, f"{rn} at {hour:02d}:00 ({day})",
                      f"{_t(p['stops'][0], 34)} → {_t(p['stops'][-1], 34)} · a bus every ~{round(p['headway_min'])} min")
        return {"type": "image", "path": str(path), "caption": f"🗺️ {rn}: crowding stop by stop at {hour:02d}:00"}
    except Exception as e:
        print("map image failed:", e)
        return None

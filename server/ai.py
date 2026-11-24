"""'Ask BheedBus' assistant.

With ANTHROPIC_API_KEY (or another Anthropic credential) set, questions go to
Claude, which answers by calling the planner and crowding tools below. Without
credentials a small rule-based parser handles the common question shapes, so the
demo always works offline.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request

from .model import LEVELS, Model

MODEL_ID = "claude-opus-5-5"

SYSTEM = """You are BheedBus, a Delhi bus assistant that helps commuters pick trips they can bear \
and helps operators see where buses are overloaded. Crowding estimates come from one month of \
digital ticket data (October 2026) and are typical values for that hour, not live data.

Use the tools to answer; never invent routes, times or crowding. When you plan a trip, compare \
the options on time and crowding in one or two short sentences each, say which you would pick \
for the user's stated priority, and mention low confidence when a tool reports it. Times are \
24-hour IST. Default to a weekday and the current hour if the user gives none. Keep answers short.

Reply in the user's language and style. If they write Hinglish (Hindi in Latin script, e.g. "Anand \
Vihar se CP 6 baje, seat chahiye"), answer in natural Hinglish; if they write Devanagari, answer in \
Hindi. Tool inputs must always use English stop names (transliterate: "कनॉट प्लेस" -> "Connaught \
Place", "आनंद विहार" -> "Anand Vihar"). "6 baje shaam" is 18:00, "subah" is morning. Each option \
includes seat_pct, the chance of getting a seat; mention it when comfort matters."""

TOOLS = [
    {"name": "search_places", "description": "Find bus stop places matching a name. Use before planning if a name is ambiguous.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
                      "additionalProperties": False}},
    {"name": "plan_journey",
     "description": "Plan bus journeys between two places. Returns up to 4 options with travel time, transfers, "
                    "crowding level per stretch (seats/standing/crowded/packed) and confidence.",
     "input_schema": {"type": "object", "properties": {
         "origin": {"type": "string"}, "destination": {"type": "string"},
         "time": {"type": "string", "description": "Departure time HH:MM, 24h"},
         "day": {"type": "string", "enum": ["weekday", "weekend"]},
         "comfort": {"type": "number", "description": "0 = fastest, 1 = most comfortable"}},
         "required": ["origin", "destination", "time", "day", "comfort"], "additionalProperties": False}},
    {"name": "route_crowding",
     "description": "Crowding along one route-direction (e.g. 717AUP) at a given hour: load per stretch and level.",
     "input_schema": {"type": "object", "properties": {
         "route": {"type": "string"}, "hour": {"type": "integer"},
         "day": {"type": "string", "enum": ["weekday", "weekend"]}},
         "required": ["route", "hour", "day"], "additionalProperties": False}},
    {"name": "operator_hotspots",
     "description": "Most overloaded routes and stretches, with how many extra buses per hour would bring them to standing room.",
     "input_schema": {"type": "object", "properties": {
         "day": {"type": "string", "enum": ["weekday", "weekend"]},
         "hour": {"type": "integer", "description": "Hour 0-23, or -1 for each route's worst hour"}},
         "required": ["day", "hour"], "additionalProperties": False}},
]


def _compact_plan(r: dict) -> dict:
    out = {"origin": r.get("origin"), "destination": r.get("destination"), "depart": r.get("depart"),
           "error": r.get("error"), "options": []}
    for o in r.get("options", []):
        out["options"].append({
            "summary": o["summary"], "badges": o.get("badges", []), "duration_min": o["duration_min"],
            "transfers": o["transfers"], "worst_crowding": o["worst_level_label"], "confidence": o["confidence"],
            "seat_pct": o["seat_pct"],
            "legs": [{"route": l["route"], "from": l["from"], "to": l["to"], "board": l["board"],
                      "arrive": l["arrive"], "stops": l["stops"], "worst": l["worst_level"],
                      "headway_min": l["headway_min"]} for l in o["legs"]]})
    return out


def run_tool(model: Model, name: str, args: dict):
    if name == "search_places":
        return model.search_places(args["query"])
    if name == "plan_journey":
        h, m = _parse_hhmm(args.get("time", "09:00"))
        return _compact_plan(model.plan(args["origin"], args["destination"], h * 60 + m,
                                        args.get("day", "weekday"), float(args.get("comfort", 0.5))))
    if name == "route_crowding":
        rn = args["route"].upper().replace(" ", "")
        if rn not in model.routes:
            cands = [r["route"] for r in model.search_routes(rn, 6)]
            return {"error": f"unknown route {rn}", "did_you_mean": cands}
        p = model.route_profile(rn, args.get("day", "weekday"))
        h = int(args["hour"]) % 24
        return {"route": rn, "hour": h, "headway_min": p["headway_min"],
                "stretches": [{"from": p["stops"][i], "to": p["stops"][i + 1], "load": p["load"][h][i],
                               "level": LEVELS[p["level"][h][i]],
                               "confidence": ["low", "medium", "high"][p["confidence"][h][i]]}
                              for i in range(len(p["stops"]) - 1)]}
    if name == "operator_hotspots":
        h = args.get("hour", -1)
        return model.hotspots(args.get("day", "weekday"), None if h is None or h < 0 else int(h), limit=12)
    return {"error": f"unknown tool {name}"}


def normalize_time(t, default: str = "09:00") -> str:
    """'18:00', '6 pm', 'shaam 6 baje', 'subah 9', 'morning' -> 'HH:MM'. Small models pass times as words."""
    t = str(t or "").strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", t):
        h, m = t.split(":")
        return f"{int(h) % 24:02d}:{int(m):02d}"
    if not t:
        return default
    s = _hinglish_normalise(t) if HINGLISH.search(t) else t
    if re.fullmatch(r"\d{1,2}", s.strip()):
        s = "at " + s.strip()
    return _time_from(s, default)


def _parse_hhmm(t: str) -> tuple[int, int]:
    h, m = normalize_time(t).split(":")
    return int(h), int(m)


def has_credentials() -> bool:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key and not key.startswith("sk-ant-"):  # placeholders like "your-key-here" are not credentials
        key = ""
    return bool(key or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_PROFILE"))


# ---------------------------------------------------------------- local AI (Ollama)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
_ollama_seen = {"t": 0.0, "ok": False}


def ollama_ready() -> bool:
    """Is a local Ollama server up with our model pulled? (checked at most every 20 s)"""
    # a busy model answers slowly, so remember a good check for a minute and a failed one for 10 s
    if time.time() - _ollama_seen["t"] < (60 if _ollama_seen["ok"] else 10):
        return _ollama_seen["ok"]
    ok = False
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5) as r:
            names = {m["name"] for m in json.loads(r.read()).get("models", [])}
        ok = OLLAMA_MODEL in names or f"{OLLAMA_MODEL}:latest" in names
    except Exception:
        ok = False
    _ollama_seen.update(t=time.time(), ok=ok)
    return ok


def engine_name() -> str:
    if has_credentials():
        return "claude"
    return "local" if ollama_ready() else "rules"


def _context_line(ctx: dict, question: str = "") -> str:
    now = ctx.get("now", "09:00")
    line = f"Now: {now}, {ctx.get('day', 'weekday')}."
    if not 5 <= int(now[:2]) <= 22:
        line += " It is night and there is no reliable night data: if the user gives no time, use 08:00 and say so."
    # tell the model the reply language outright; small models otherwise drift between languages.
    # An explicit Hinglish preference (e.g. a WhatsApp voice note) wins over stray Devanagari words.
    if ctx.get("lang") == "hinglish":
        line += " Reply in simple Hinglish (Hindi in Latin script, e.g. 'Sabse jaldi: 30 min, bus 717AUP, seat milne ka chance 97%')."
    elif ctx.get("lang") == "hi-IN" or re.search(r"[\u0900-\u097F]", question):
        line += " Reply in Hindi (Devanagari)."
    elif HINGLISH.search(question):
        line += " Reply in simple Hinglish (Hindi in Latin script)."
    else:
        line += " Reply in English."
    return line


def ask(model: Model, question: str, context: dict | None = None, history: list | None = None) -> dict:
    """Non-streaming wrapper: run the stream and return the final answer with its trace."""
    out = {"answer": "", "engine": "rules", "trace": [], "plans": []}
    for ev in ask_stream(model, question, context or {}, history or []):
        if ev["type"] == "tool":
            out["trace"].append({"tool": ev["name"], "input": ev["input"]})
        elif ev["type"] == "plan":
            out["plans"].append(ev["params"])
        elif ev["type"] == "answer":
            out.update({k: v for k, v in ev.items() if k != "type"})
    return out


def ask_stream(model: Model, question: str, ctx: dict, history: list):
    """Yield events: {type: engine|step|tool|plan|answer}. Tries Claude, then local AI, then the parser."""
    engines = (["claude"] if has_credentials() else []) + (["local"] if ollama_ready() else []) + ["rules"]
    note = None
    for eng in engines:
        yield {"type": "engine", "engine": eng}
        try:
            if eng == "claude":
                yield from _claude_stream(model, question, ctx, history)
            elif eng == "local":
                yield from _ollama_stream(model, question, ctx, history)
            else:
                res = _ask_rules(model, question, ctx)
                for t in res.get("trace", []):
                    yield {"type": "tool", "name": t["tool"], "input": t["input"]}
                for pl in res.get("plans", []):
                    yield {"type": "plan", "params": pl}
                yield {"type": "answer", "text": res["answer"], "answer": res["answer"], "engine": "rules",
                       **({"note": note or res.get("note")} if (note or res.get("note")) else {})}
            return
        except Exception as e:  # an engine failing must never break the chat: fall through to the next
            note = f"{'Claude' if eng == 'claude' else 'Local AI'} unavailable ({type(e).__name__}); used the next engine."
            yield {"type": "step", "text": note}


def _plan_event(name: str, args: dict, ctx: dict):
    if name == "plan_journey":
        return {"type": "plan", "params": {"origin": args.get("origin"), "destination": args.get("destination"),
                                           "time": normalize_time(args.get("time"), ctx.get("now", "09:00")),
                                           "day": args.get("day", "weekday"), "comfort": args.get("comfort", 0.5)}}
    return None


def _claude_stream(model: Model, question: str, ctx: dict, history: list):
    import anthropic

    client = anthropic.Anthropic()
    messages = [{"role": m["role"], "content": m["content"]} for m in history[-6:] if m.get("content")]
    messages.append({"role": "user", "content": f"{_context_line(ctx, question)}\n\n{question}"})
    tools = [{**t, "strict": True} for t in TOOLS]

    def create(**kw):
        try:
            return client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
        except anthropic.BadRequestError:
            return client.messages.create(**kw)

    for _ in range(6):
        resp = create(model=MODEL_ID, max_tokens=4000, system=SYSTEM, tools=tools, messages=messages,
                      output_config={"effort": "low"})
        if resp.stop_reason == "refusal":
            yield {"type": "answer", "text": "Sorry, I can't help with that request.", "answer": "Sorry, I can't help with that request.", "engine": "claude"}
            return
        uses = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not uses:
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            yield {"type": "answer", "text": text, "answer": text, "engine": "claude"}
            return
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for u in uses:
            yield {"type": "tool", "name": u.name, "input": u.input}
            try:
                out = run_tool(model, u.name, u.input)
                if (pe := _plan_event(u.name, u.input, ctx)):
                    yield pe
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": json.dumps(out, default=str)[:20000]})
            except Exception as e:
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": f"Error: {e}", "is_error": True})
        messages.append({"role": "user", "content": results})
    raise RuntimeError("Claude did not finish")


SMALL_SYSTEM = """You are BheedBus, a friendly Delhi bus assistant. You know typical crowding from \
October 2026 ticket data (not live).

Rules:
- For any trip question ("X to Y", "X se Y"), call plan_journey. Copy place names as the user wrote \
them (English spelling), e.g. "CP" -> "Connaught Place". time is HH:MM 24h ("6 baje shaam" = 18:00, \
"subah 9" = 09:00). comfort is 1 if they want a seat or comfort, 0 if they are in a hurry, else 0.5.
- For "how crowded is route N", call route_crowding. For "where to add buses" / overloaded routes, call operator_hotspots.
- If the start or the destination is missing or unclear, ask for it in one short line; do not call plan_journey.
- Voice notes may include the exact Hindi words in brackets: use them to recognise stops ("कनॉट/कनोट प्लेस" = Connaught Place, "कश्मीरी गेट" = Kashmere Gate) and pass English names to tools.
- Never invent routes, times or numbers. Use only what tools return.
- Answer in 2-4 short lines: the best option first (minutes, bus numbers, crowding, seat chance), then \
one alternative if useful. Reply in the user's language: Hinglish if they wrote Hinglish."""


def _small_tool_output(name: str, out):
    """Small models do better with short tool results."""
    if name == "route_crowding" and isinstance(out, dict) and "stretches" in out:
        st = sorted(out["stretches"], key=lambda x: -x["load"])
        return {"route": out["route"], "hour": out["hour"], "headway_min": out["headway_min"],
                "busiest_stretches": st[:4], "crowded_stretches": sum(x["level"] in ("crowded", "packed") for x in st),
                "total_stretches": len(st)}
    if name == "plan_journey" and isinstance(out, dict):
        return {"from": (out.get("origin") or [None])[0], "to": (out.get("destination") or [None])[0],
                "error": out.get("error"),
                "options": [{"summary": o["summary"], "badges": o["badges"], "duration_min": o["duration_min"],
                             "seat_pct": o["seat_pct"], "worst_crowding": o["worst_crowding"],
                             "buses": [f"{l['route']} {l['from']}→{l['to']} board {l['board']}" for l in o["legs"]]}
                            for o in out.get("options", [])[:3]]}
    if name == "operator_hotspots" and isinstance(out, list):
        return [{k: r[k] for k in ("route", "hour", "from", "to", "peak_load", "occupancy_pct", "extra_buses_per_hour")}
                for r in out[:6]]
    return out


def _ollama_chat(messages: list, tools: list | None) -> dict:
    body = {"model": OLLAMA_MODEL, "messages": messages, "stream": False,
            "options": {"temperature": 0.2, "num_ctx": 8192}}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())["message"]


def _calls_from(msg: dict) -> list[tuple[str, dict]]:
    calls = []
    for c in msg.get("tool_calls") or []:
        f = c.get("function", {})
        args = f.get("arguments") or {}
        if isinstance(args, str):
            args = json.loads(args or "{}")
        calls.append((f.get("name"), args))
    if not calls:  # some small models print the call as JSON text instead
        for m in re.finditer(r'\{\s*"name"\s*:\s*"(\w+)"\s*,\s*"arguments"\s*:\s*(\{.*?\})\s*\}', msg.get("content") or "", re.S):
            try:
                calls.append((m.group(1), json.loads(m.group(2))))
            except json.JSONDecodeError:
                pass
    return calls


def _mentioned(place: str, said: str) -> bool:
    """Did the user actually name this place? Stops the model inventing a start or destination."""
    if re.search(r"[\u0900-\u097F]", said):
        return True  # can't compare scripts; trust the model
    said = said.lower()
    words = [w for w in re.findall(r"[a-z0-9]+", place.lower()) if len(w) >= 3 and w not in {"isbt", "metro", "station", "terminal", "road", "the"}]
    alias = {"connaught": ["cp", "connaught", "konaot", "rajiv chowk"], "kashmere": ["kashmere", "kashmiri", "isbt"]}
    for w in words or [place.lower()]:
        if w in said or w[:5] in said or any(a in said for a in alias.get(w, [])):
            return True
    return False


def _ollama_stream(model: Model, question: str, ctx: dict, history: list):
    tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                               "parameters": t["input_schema"]}} for t in TOOLS]
    messages = [{"role": "system", "content": SMALL_SYSTEM}]
    messages += [{"role": m["role"], "content": m["content"]} for m in history[-6:] if m.get("content")]
    messages.append({"role": "user", "content": f"{_context_line(ctx, question)}\n{question}"})
    used = False
    for step in range(5):
        yield {"type": "step", "text": "Thinking…" if step == 0 else "Reading the results…"}
        msg = _ollama_chat(messages, tools)
        calls = _calls_from(msg)
        if not calls:
            text = (msg.get("content") or "").strip()
            looks_like_trip = re.search(r"\b(to|se|→)\b", question, re.I) and not used
            if looks_like_trip and step == 0:
                # small models sometimes answer from memory; insist once on using the planner
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": "Use the plan_journey tool for this, then answer."})
                continue
            if not text or looks_like_trip:
                raise RuntimeError("local model gave no grounded answer")
            yield {"type": "answer", "text": text, "answer": text, "engine": "local", "model": OLLAMA_MODEL}
            return
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in calls]})
        for name, args in calls:
            if name == "plan_journey":
                args.setdefault("time", ctx.get("now", "09:00"))
                args.setdefault("day", ctx.get("day", "weekday"))
                args.setdefault("comfort", 0.5)
            said = " ".join([question] + [m["content"] for m in history[-6:] if m.get("role") == "user"])
            if name == "plan_journey" and not (_mentioned(str(args.get("origin", "")), said)
                                               and _mentioned(str(args.get("destination", "")), said)):
                missing = "start" if not _mentioned(str(args.get("origin", "")), said) else "destination"
                messages.append({"role": "tool", "tool_name": name, "content": json.dumps(
                    {"error": f"The user has not said their {missing}. Do not guess: ask them for it in one short line."})})
                used = True
                continue
            yield {"type": "tool", "name": name, "input": args}
            try:
                out = _small_tool_output(name, run_tool(model, name, args))
                if (pe := _plan_event(name, args, ctx)):
                    yield pe
            except Exception as e:
                out = {"error": str(e)}
            used = True
            messages.append({"role": "tool", "content": json.dumps(out, default=str)[:6000], "tool_name": name})
    raise RuntimeError("local model did not finish")


# ---------------------------------------------------------------- offline parser
TIME_RE = re.compile(r"\b(?:at|by|around|leaving|leave)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)


def _time_from(q: str, default: str) -> str:
    ql = q.lower()
    for word, t in (("morning", "09:00"), ("evening", "18:00"), ("night", "21:00"), ("afternoon", "14:00"),
                    ("noon", "12:00")):
        if word in ql:
            default = t
    for m in TIME_RE.finditer(q):
        h, mm, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        if not ap and not m.group(2) and not re.search(r"\b(at|by|around|leav\w*)\s*$", q[:m.start(1)], re.I):
            continue
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if not ap and 1 <= h <= 7 and "morning" not in ql:
            h += 12  # "around 6" on a commute means evening
        if 0 <= h < 24:
            return f"{h:02d}:{mm:02d}"
    return default


HINGLISH = re.compile(r"\b(se|tak|baje|chahiye|jaana|jana|kitni|kitna|bheed|subah|shaam|sham|raat|dopahar|"
                      r"kaise|jaldi|milegi|kahan|kab)\b", re.I)


def _hinglish_normalise(q: str) -> str:
    """Rewrite common Hinglish trip phrasing into the English shape the parser understands."""
    s = q
    hm = re.search(r"(\d{1,2})(?:[:.](\d{2}))?\s*baje", s, re.I)
    if hm:
        h, mm = int(hm.group(1)), hm.group(2) or "00"
        sl = s.lower()
        if re.search(r"shaam|sham|raat", sl) and h < 12:
            h += 12
        elif re.search(r"dopahar", sl) and h < 6:
            h += 12
        elif not re.search(r"subah", sl) and 1 <= h <= 7:
            h += 12  # "6 baje" with no part of day: commuters mean evening
        s = s[:hm.start()] + f" at {h:02d}:{mm} " + s[hm.end():]
    s = re.sub(r"\b(subah|shaam|sham|raat|dopahar)\b", " ", s, flags=re.I)
    s = re.sub(r"\bseat\s+(chahiye|milegi|mil jaaye)\b|\bbaithna\b|\bkhaali\b", " seat ", s, flags=re.I)
    s = re.sub(r"\bjaldi\b", " fast ", s, flags=re.I)
    s = re.sub(r"\b(tak|jaana|jana|hai|kaise|jaun|jaayein)\b", " ", s, flags=re.I)
    s = re.sub(r"\s+se\s+", " to ", s, count=1, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def _to_hinglish(res: dict) -> dict:
    a = res["answer"]
    for en, hi in (("Fastest", "Sabse tez"), ("Most comfortable", "Sabse aaraam se"), ("Option", "Doosra option"),
                   ("leaving", "nikalna"), ("min via", "min, bus"), ("usually crowded for about", "lagbhag"),
                   ("at worst seats available", "seat mil jaati hai"), ("at worst standing room", "khade hone ki jagah"),
                   ("chance of a seat", "chance seat milne ka"), ("(confidence ", "(bharosa "),
                   ("For the fastest trip I'd take:", "Jaldi pahunchna hai to:"),
                   ("For the most comfortable trip I'd take:", "Aaraam se jaana hai to:"),
                   ("weekday", "kaam ka din"), ("weekend", "chhutti")):
        a = a.replace(en, hi)
    a = re.sub(r"lagbhag (\d+) min", r"lagbhag \1 min bheed", a)
    return {**res, "answer": a, "lang": "hinglish"}


# words people wrap around place names ("mujhe Anand Vihar se lekar Sarojini Nagar tak jaana hai")
FILLER = {"mujhe", "mujhko", "main", "mai", "mein", "hum", "humein", "hame", "hamein", "lekar", "le", "kar", "tak",
          "pata", "karna", "karo", "batao", "bataiye", "bata", "do", "dijiye", "kaise", "kahan", "kitna", "kitni",
          "ka", "ki", "ke", "ko", "hai", "hain", "wala", "wali", "jaana", "jana", "jaun", "jaane", "chalna",
          "please", "plz", "ek", "koi", "raasta", "rasta", "bus", "from", "i", "want", "need", "to", "go", "get",
          "the", "a", "me", "my", "show", "find", "best", "way", "route", "and", "chahta", "chahti", "chahiye"}


def _trim_filler(s: str) -> str:
    words = s.split()
    while words and words[0].lower().strip(",.?!") in FILLER:
        words.pop(0)
    while words and words[-1].lower().strip(",.?!") in FILLER:
        words.pop()
    return " ".join(words)


def _ask_rules(model: Model, q: str, ctx: dict) -> dict:
    if re.search(r"[\u0900-\u097F]", q):
        return {"answer": "देवनागरी के लिए Claude चाहिए (ANTHROPIC_API_KEY)। बिना key के Hinglish में लिखें या बोलें, जैसे: "
                          "“Anand Vihar se Connaught Place shaam 6 baje, seat chahiye”.", "engine": "rules"}
    if ctx.get("lang") == "hinglish" or HINGLISH.search(q):
        res = _ask_rules(model, _hinglish_normalise(q), {**ctx, "lang": "en-IN"})
        return _to_hinglish(res)
    ql = q.lower()
    day = "weekend" if re.search(r"weekend|saturday|sunday|\bsat\b|\bsun\b", ql) else ctx.get("day", "weekday")
    now = ctx.get("now", "09:00")
    night = not 5 <= int(now[:2]) <= 22
    t = _time_from(q, "08:00" if night else now)
    night_note = night and t == "08:00" and "08" not in q
    comfort = 0.5
    if re.search(r"seat|comfort|not crowded|less crowded|empty|sit", ql):
        comfort = 1.0
    elif re.search(r"fast|quick|hurry|asap|late", ql):
        comfort = 0.0

    if re.search(r"hotspot|overload|add buses|extra bus|where.*(buses|crowd)|operator|busiest", ql):
        h = int(t[:2]) if re.search(r"\d", ql) else -1
        rows = run_tool(model, "operator_hotspots", {"day": day, "hour": h})
        lines = [f"{r['route']} {r['hour']:02d}:00, {r['from']} → {r['to']}: ~{r['peak_load']:.0f} on board "
                 f"({r['occupancy_pct']}% of comfortable capacity), add ~{r['extra_buses_per_hour']} buses/h"
                 for r in rows[:5]]
        return {"answer": "Most overloaded stretches (" + day + "):\n" + "\n".join(lines), "engine": "rules",
                "trace": [{"tool": "operator_hotspots", "input": {"day": day, "hour": h}}], "data": rows}

    rm = re.search(r"\b(?:route|bus)\s*([0-9a-z\-]+(?:up|down)?)\b", ql)
    if rm and re.search(r"crowd|full|busy|load|how", ql) and " to " not in ql:
        rn = rm.group(1).upper()
        if rn not in model.routes:
            rn = next((r["route"] for r in model.search_routes(rn, 1)), rn)
        out = run_tool(model, "route_crowding", {"route": rn, "hour": int(t[:2]), "day": day})
        if "error" in out:
            return {"answer": f"I don't know route {rn}. Try one of: {', '.join(out['did_you_mean'])}", "engine": "rules"}
        worst = max(out["stretches"], key=lambda s: s["load"])
        cnt = {k: sum(s["level"] == k for s in out["stretches"]) for k in LEVELS}
        return {"answer": f"{rn} around {t} on a {day}: busiest between {worst['from']} and {worst['to']} "
                          f"(~{worst['load']:.0f} on board, {worst['level']}). "
                          f"{cnt['crowded'] + cnt['packed']} of {len(out['stretches'])} stretches are crowded; "
                          f"a bus every ~{out['headway_min']:.0f} min.",
                "engine": "rules", "trace": [{"tool": "route_crowding", "input": {"route": rn}}], "data": out}

    # "I want to go from X to Y": when there is a "from", the trip starts after it
    fm = re.search(r"\bfrom\s+(.+)", q, re.I)
    m = re.search(r"(?:from\s+)?(.+?)\s+(?:to|→|->)\s+(.+)", fm.group(1) if fm else q, re.I)
    # "I'm at X after office, ... way to Y"
    at = re.search(r"\b(?:i'?m|i am|main|hum)\s+(?:at|in|near)\s+(.+?)(?:,|\s+(?:after|around|and|right now|now)\b)", q, re.I)
    if at and not fm:
        tail = re.search(r"\bto\s+([^,?]+?)\s*\??$", q, re.I)
        if tail:
            m = re.match(r"(.+)\|(.+)", f"{at.group(1)}|{tail.group(1)}")
    if not m:
        return {"answer": "Ask me things like “Anand Vihar to Connaught Place at 6 pm, I want a seat”, "
                          "“how crowded is route 717AUP at 9am” or “where should we add buses?”", "engine": "rules"}
    clean = lambda s: re.sub(r"\b(at|by|around|leaving|on|this|tomorrow|today|weekday|weekend|saturday|sunday|"  # noqa: E731
                             r"morning|evening|night|afternoon|i want|want|a seat|seat|fast|quick|please|"
                             r"comfortable|\d{1,2}(:\d{2})?\s*(am|pm)?)\b.*$", "", s, flags=re.I).strip(" ,.?")
    o = re.sub(r"^(how (do|can) i (get|go)|take me|plan|route|i need to go|go)\s+(from\s+)?", "", m.group(1), flags=re.I)
    o, d = _trim_filler(clean(o)), _trim_filler(clean(m.group(2)))
    res = run_tool(model, "plan_journey", {"origin": o, "destination": d, "time": t, "day": day, "comfort": comfort})
    if res.get("error") or not res["options"]:
        return {"answer": res.get("error") or f"No bus journey found from {o} to {d}.", "engine": "rules"}
    if night_note:
        res["note"] = "no time given and it's night, so showing 08:00"
    lines = [f"• {', '.join(x['badges']) or 'Option'}: {x['summary']} (confidence {x['confidence']})"
             for x in res["options"]]
    pick = min(res["options"], key=lambda x: x["duration_min"] if comfort < 0.5 else
               (-(x["seat_pct"] // 10), max(LEVELS.index(l["worst"]) for l in x["legs"]), x["duration_min"]))
    pref = "fastest" if comfort < 0.5 else "most comfortable"
    return {"answer": f"{res['origin'][0]} → {res['destination'][0]}, leaving {t} ({day}):\n" + "\n".join(lines)
                      + f"\nFor the {pref} trip I'd take: {pick['summary']}.",
            "engine": "rules", "trace": [{"tool": "plan_journey", "input": {"origin": o, "destination": d, "time": t}}],
            "plans": [{"origin": o, "destination": d, "time": t, "day": day, "comfort": comfort}],
            **({"note": res["note"]} if res.get("note") else {})}

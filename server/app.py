"""BheedBus API + web app.

    .venv/bin/python -m uvicorn server.app:app --port 8000
    open http://localhost:8000        (interactive API docs at /docs)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai
from .model import LEVELS, Model

ROOT = Path(__file__).resolve().parents[1]
try:  # secrets such as WHATSAPP_TOKEN can live in a .env file next to config.json (never commit it)
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass
CFG = json.loads((ROOT / "config.json").read_text())
DB = ROOT / CFG["out_db"]
MAX_UPLOAD = 700 * 1024 * 1024

app = FastAPI(title="BheedBus API", version="1.0",
              description="Crowding-aware bus journey planner for Delhi, built on October 2026 ticket data.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=2000)  # the city time-lapse layer is ~2 MB of JSON


@app.middleware("http")
async def no_cache_app_files(request, call_next):
    # the web app is a few small files: always serve the current version
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp

model: Model | None = None
jobs: dict[str, dict] = {}
_geo = None
_geo_lock = threading.Lock()
_model_lock = threading.Lock()


def get_geo():
    from .geo import shared
    return shared()


def _warm_maps(n: int = 60) -> None:
    """Pre-compute stop positions for the busiest routes in the background (geocoder is slow the first time)."""
    try:
        m, g = get_model(), get_geo()
        if not g.ready:
            return
        busiest = [r for r in m._routes_df.sort_values("tickets", ascending=False)["route"].head(n) if r in m.routes]
        # short bursts (a few geocoder calls each) so a user's map request never waits long for the lock
        for _ in range(12):
            todo = [rn for rn in busiest if not g.complete.get(rn, False)]
            if not todo:
                break
            for rn in todo:
                with _geo_lock:
                    g.route(rn, m.routes[rn].stops, geocode_budget=4)
                time.sleep(0.05)
    except Exception as e:  # warming is best-effort
        print("map warm-up stopped:", e)


@app.on_event("startup")
def _startup() -> None:
    threading.Thread(target=_warm_maps, daemon=True).start()
    # build the city time-lapse layer ahead of the first visitor (~30 s the first time, then cached on disk)
    threading.Thread(target=lambda: (get_model().insights.city("weekday"), get_model().insights.near(28.63, 77.22)),
                     daemon=True).start()  # also index stop positions for "share my location"
    from . import telegram  # replies to Telegram chats by polling: no public URL needed
    telegram.start(get_model, _place_names, now_ctx)


def get_model() -> Model:
    global model
    with _model_lock:  # startup threads and the first request may arrive together
        if model is None:
            if not DB.exists():
                raise HTTPException(503, "No crowding database yet. Run: python pipeline/build.py")
            from .insights import Insights
            m = Model(DB, CFG)
            m.insights = Insights(m)
            model = m
    return model


def now_ctx() -> dict:
    n = datetime.now()
    return {"now": n.strftime("%H:%M"), "day": "weekend" if n.weekday() >= 5 else "weekday"}


def _minute(t: str) -> int:
    h, m = (t.split(":") + ["0"])[:2]
    return int(h) % 24 * 60 + int(m)


# ------------------------------------------------------------------ commuter
@app.get("/api/places", tags=["commuter"])
def places(q: str, limit: int = 8):
    return get_model().search_places(q, limit)


@app.get("/api/plan", tags=["commuter"])
def plan(origin: str = Query(..., alias="from"), destination: str = Query(..., alias="to"),
         time: str = "18:00", day: str = "weekday", comfort: float = Query(0.5, ge=0, le=1),
         max_transfers: int = Query(2, ge=0, le=3)):
    """Journey options with travel time and crowding per stretch. comfort: 0 = fastest, 1 = most comfortable."""
    return get_model().plan(origin, destination, _minute(time), day, comfort, max_transfers)


class Ask(BaseModel):
    question: str
    now: str | None = None
    day: str | None = None
    lang: str | None = None
    history: list[dict] = []


@app.post("/api/ask", tags=["commuter"])
def ask(body: Ask):
    """Natural-language questions. Uses Claude tool calls when credentials are set, else a built-in parser."""
    ctx = now_ctx()
    if body.now:
        ctx["now"] = body.now
    if body.day:
        ctx["day"] = body.day
    if body.lang:
        ctx["lang"] = body.lang
    return ai.ask(get_model(), body.question, ctx, body.history)


@app.post("/api/ask/stream", tags=["commuter"])
def ask_stream(body: Ask):
    """Live assistant: newline-delimited JSON events (engine, step, tool, plan, answer) as they happen."""
    ctx = now_ctx()
    for k in ("now", "day", "lang"):
        if getattr(body, k):
            ctx[k] = getattr(body, k)
    m = get_model()

    def gen():
        for ev in ai.ask_stream(m, body.question, ctx, body.history):
            yield json.dumps(ev, default=str) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ------------------------------------------------------------------- routes
@app.get("/api/routes", tags=["crowding"])
def routes(q: str = "", limit: int = 30):
    return get_model().search_routes(q, limit)


@app.get("/api/route/{route}", tags=["crowding"])
def route(route: str, day: str = "weekday"):
    m = get_model()
    if route not in m.routes:
        raise HTTPException(404, f"unknown route {route}")
    return m.route_profile(route, day)


@app.get("/api/route/{route}/map", tags=["crowding"])
def route_map(route: str, refine: bool = False):
    """Stop positions for a route from OpenStreetMap (bus stops, metro, landmarks), matched by name and
    route order. refine=true also asks the Photon geocoder about stops OSM's stop list lacks (slower, cached)."""
    m, g = get_model(), get_geo()
    if route not in m.routes:
        raise HTTPException(404, f"unknown route {route}")
    if not g.ready:
        raise HTTPException(503, "No map data yet. Run: python -m server.geo --fetch")
    R = m.routes[route]
    if g.complete.get(route):  # fully placed already: no lock, no waiting
        pts = g._route[route]
    elif refine:
        with _geo_lock:
            pts = g.route(route, R.stops, geocode_budget=60)
    else:  # fast first draw: whatever is known now (OSM stops + cached geocodes), never touches the network
        pts = g._route.get(route) or g._route.get(route + "#quick") or g.route(route + "#quick", R.stops, geocode_budget=0)
    kinds: dict[str, int] = {}
    for p in pts:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    exact = sum(v for k, v in kinds.items() if k in ("bus_stop", "metro"))
    return {"route": route, "stops": pts, "kinds": kinds, "exact": exact, "total": len(pts),
            "refined": bool(g.complete.get(route)), "source": "OpenStreetMap (Overpass API, Photon geocoder)"}


@app.get("/api/crowding", tags=["crowding"])
def crowding(route: str, hour: int = Query(..., ge=0, le=23), day: str = "weekday"):
    """Crowding per stretch for one route-direction at one hour (for maps and integrations)."""
    m = get_model()
    if route not in m.routes:
        raise HTTPException(404, f"unknown route {route}")
    p = m.route_profile(route, day)
    return {"route": route, "hour": hour, "day": day, "stretches": [
        {"seq": i, "from": p["stops"][i], "to": p["stops"][i + 1], "load": p["load"][hour][i],
         "level": LEVELS[p["level"][hour][i]], "confidence": ["low", "medium", "high"][p["confidence"][hour][i]],
         "from_coord": p["coords"][i], "to_coord": p["coords"][i + 1]}
        for i in range(len(p["stops"]) - 1)]}


@app.get("/api/geojson", tags=["crowding"])
def geojson(route: str, hour: int = Query(..., ge=0, le=23), day: str = "weekday"):
    """GeoJSON line segments coloured by crowding. Needs a GTFS feed for stop coordinates."""
    c = crowding(route, hour, day)
    colors = {l["key"]: l["color"] for l in CFG["levels"]}
    feats = [{"type": "Feature", "properties": {**{k: v for k, v in s.items() if "coord" not in k},
                                                "color": colors[s["level"]]},
              "geometry": {"type": "LineString", "coordinates": [[s["from_coord"]["lon"], s["from_coord"]["lat"]],
                                                                [s["to_coord"]["lon"], s["to_coord"]["lat"]]]}}
             for s in c["stretches"] if s["from_coord"] and s["to_coord"]]
    return {"type": "FeatureCollection", "features": feats}


# ----------------------------------------------------------------- insights
@app.get("/api/insights/events", tags=["insights"])
def insights_events():
    """Unusual days and stop surges, found from the tickets alone (the holiday calendar is only used to check)."""
    return get_model().insights.events()


@app.get("/api/insights/bunching", tags=["insights"])
def insights_bunching():
    """Bus bunching: pair-correlation of same-route buses at the same stop, by hour, and the worst routes."""
    return get_model().insights.bunching_summary()


@app.get("/api/insights/bunching/{route}", tags=["insights"])
def insights_bunching_route(route: str, hour: int = Query(18, ge=0, le=23), day: str = "weekday"):
    m = get_model()
    if route not in m.routes:
        raise HTTPException(404, f"unknown route {route}")
    return {"route": route, "hour": hour, "by_hour": [m.insights.bunch(route, h, day)["g"] for h in range(24)],
            **m.insights.bunch(route, hour, day)}


@app.get("/api/insights/nudge", tags=["insights"])
def insights_nudge(share: float = Query(0.1, ge=0, le=0.6), minutes: int = Query(20, ge=-60, le=60),
                   start: int = Query(8, ge=0, le=23), end: int = Query(10, ge=1, le=24), day: str = "weekday",
                   evening: bool = True):
    """What if `share` of peak riders left `minutes` later? Crowded passenger-hours and seats, before vs after."""
    return get_model().insights.nudge(share, minutes, start, end, day, evening)


@app.get("/api/city", tags=["insights"])
def city(day: str = "weekday"):
    """Every mapped stretch in Delhi with a crowding code per hour, for the time-lapse map."""
    return get_model().insights.city(day)


class Feedback(BaseModel):
    trip: dict
    reported: str
    channel: str = "web"


@app.post("/api/feedback", tags=["insights"])
def feedback_add(body: Feedback):
    """A rider says how the bus really was. Stored with the model's prediction for that trip."""
    from . import feedback
    try:
        return feedback.record(body.channel, "", body.trip, body.reported)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/feedback", tags=["insights"])
def feedback_summary():
    """How often riders' reports agree with the model."""
    from . import feedback
    return feedback.summary()


# ----------------------------------------------------------------- operator
@app.get("/api/operator/hotspots", tags=["operator"])
def hotspots(day: str = "weekday", hour: int | None = Query(None, ge=0, le=23), limit: int = 25,
             min_confidence: str = "medium"):
    return get_model().hotspots(day, hour, limit, {"low": 0, "medium": 1, "high": 2}[min_confidence])


@app.get("/api/operator/summary", tags=["operator"])
def summary():
    return get_model().summary()


@app.post("/api/operator/scale", tags=["operator"])
def set_scale(scale: float = Query(..., ge=0.3, le=3.0)):
    """Sensitivity: multiply every load by `scale` (e.g. if true ridership is higher than assumed)."""
    if os.environ.get("BHEEDBUS_READONLY") == "1" and abs(scale - 1.0) > 1e-9:
        raise HTTPException(403, "The ridership slider is switched off on the shared hosted demo, so one visitor can't change numbers for everyone.")
    get_model().set_scale(scale)
    return {"scale": scale}


_rb_cache: dict = {}


def _rebalancer(day: str, start: int, end: int):
    from .rebalance import Rebalancer
    m = get_model()
    key = (day, start, end, m.scale, m.info.get("built_at"))
    if key not in _rb_cache:
        _rb_cache.clear()
        _rb_cache[key] = Rebalancer(m, day, start, end)
    return _rb_cache[key]


class Moves(BaseModel):
    day: str = "weekday"
    start: int = 8
    end: int = 10
    moves: list[dict] = []
    max_moves: int = 40


@app.get("/api/operator/rebalance/candidates", tags=["operator"])
def rebalance_candidates(day: str = "weekday", start: int = Query(8, ge=0, le=23), end: int = Query(10, ge=1, le=24),
                         n: int = 12):
    """Routes that would gain most from one more bus, and routes that can spare one, in a time window."""
    return _rebalancer(day, start, end).candidates(n)


@app.post("/api/operator/rebalance", tags=["operator"])
def rebalance(body: Moves):
    """Evaluate moving buses between base routes ({from, to, n}) for the window."""
    try:
        return _rebalancer(body.day, body.start, body.end).evaluate(body.moves)
    except KeyError as e:
        raise HTTPException(400, str(e))


@app.post("/api/operator/optimise", tags=["operator"])
def optimise(body: Moves):
    """Greedy fleet reallocation that minimises crowded passenger-minutes with the fleet size fixed."""
    return _rebalancer(body.day, body.start, body.end).optimise(min(body.max_moves, 200))


@app.get("/api/alerts", tags=["integrations"])
def alerts(day: str | None = None, hour: int | None = Query(None, ge=0, le=23), limit: int = 8):
    """Ready-to-send crowding alerts for the given (or current) hour. Built for n8n / chat bots."""
    ctx = now_ctx()
    day = day or ctx["day"]
    hour = int(ctx["now"][:2]) if hour is None else hour
    rows = get_model().hotspots(day, hour, limit, 1)
    text = "\n".join(f"🚌 {r['route']}: {r['from']} → {r['to']} ~{r['peak_load']:.0f} on board "
                     f"({r['occupancy_pct']}%), +{r['extra_buses_per_hour']} buses/h suggested" for r in rows)
    return {"day": day, "hour": hour, "count": len(rows), "alerts": rows,
            "text": f"BheedBus crowding alerts, {day} {hour:02d}:00\n{text}" if rows else "No overloaded routes this hour."}


class Hook(BaseModel):
    message: str
    user: str | None = None


@app.post("/api/webhook/chat", tags=["integrations"])
def chat_hook(body: Hook):
    """Generic chat webhook (Telegram/WhatsApp via n8n): send {message}, get {reply}."""
    r = ai.ask(get_model(), body.message, now_ctx())
    return {"reply": r["answer"], "engine": r.get("engine")}


# ------------------------------------------------------------------- WhatsApp
from . import whatsapp as wa  # noqa: E402


def _place_names() -> list[str]:
    m = get_model()
    order = sorted(range(len(m.place_label)), key=lambda i: -m.place_vol[i])
    return [m.place_label[i] for i in order[:80]]


@app.get("/webhook/whatsapp", tags=["integrations"], response_class=PlainTextResponse)
def whatsapp_verify(mode: str = Query("", alias="hub.mode"), token: str = Query("", alias="hub.verify_token"),
                    challenge: str = Query("", alias="hub.challenge")):
    """Meta calls this once when you register the webhook."""
    if mode == "subscribe" and token and token == wa.cfg("WHATSAPP_VERIFY_TOKEN"):
        return challenge
    raise HTTPException(403, "verify token mismatch")


@app.post("/webhook/whatsapp", tags=["integrations"])
async def whatsapp_incoming(request: Request):
    """Incoming WhatsApp messages (text or voice notes). Answered in the background."""
    raw = await request.body()
    if not wa.signature_ok(raw, request.headers.get("x-hub-signature-256")):
        raise HTTPException(401, "bad signature")
    if not wa.configured():
        return {"queued": 0, "note": "WHATSAPP_TOKEN / WHATSAPP_PHONE_ID not set"}
    n = wa.handle_webhook(json.loads(raw or b"{}"), get_model(), _place_names(), now_ctx())
    return {"queued": n}


@app.post("/api/whatsapp/simulate", tags=["integrations"])
async def whatsapp_simulate(text: str = Form(""), choice_id: str = Form(""), choice_title: str = Form(""),
                            audio: UploadFile | None = File(None), user: str = Form("web")):
    """Try the WhatsApp bot without a phone: send text, a tapped option, or a voice recording.
    Returns the same messages (text, buttons, lists, map links, voice) the real bot would send."""
    from . import wa_flow
    heard = None
    if audio is not None:
        suffix = Path(audio.filename or "note.webm").suffix or ".webm"
        tmp = ROOT / "data" / "voice" / f"in-{uuid.uuid4().hex[:8]}{suffix}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(await audio.read())
        try:
            t = wa.transcribe(str(tmp), _place_names())
        finally:
            tmp.unlink(missing_ok=True)
        heard = t["text"]
        inc = {"type": "text", "text": t["english"], "lang": t["lang"], "voice": True}
    elif choice_id:
        inc = {"type": "choice", "id": choice_id, "title": choice_title}
    elif text.strip():
        inc = {"type": "text", "text": text}
    else:
        raise HTTPException(400, "Send some text, a choice or a voice note")
    out = []
    for m in wa_flow.handle(get_model(), f"sim-{user}", inc, now_ctx()):
        if m["type"] == "voice":
            v = wa.speak_to_file(m["text"], m["hindi"], fmt="m4a")
            if v:
                out.append({"type": "voice", "url": f"/api/whatsapp/voice/{v.name}"})
        elif m["type"] == "later":  # the tester shows delayed messages right away, marked as such
            out.append({**m["msg"], "later_min": round(m["delay_s"] / 60)})
        elif m["type"] == "image":
            out.append({"type": "image", "url": f"/api/whatsapp/map/{Path(m['path']).name}", "caption": m.get("caption", "")})
        else:
            out.append(m)
    return {"heard": heard, "messages": out, "engine": ai.engine_name()}


@app.get("/api/whatsapp/voice/{name}", tags=["integrations"])
def whatsapp_voice(name: str):
    f = wa.VOICE_DIR / Path(name).name  # basename only: no path traversal
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f, media_type="audio/mp4" if f.suffix == ".m4a" else "audio/ogg")


@app.get("/api/whatsapp/map/{name}", tags=["integrations"])
def whatsapp_map(name: str):
    from .mapimage import OUT
    f = OUT / Path(name).name  # basename only: no path traversal
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f, media_type="image/png")


@app.get("/api/whatsapp/status", tags=["integrations"])
def whatsapp_status(request: Request):
    return {"configured": wa.configured(), "number": wa.cfg("WHATSAPP_NUMBER") or None,
            "verify_token_set": bool(wa.cfg("WHATSAPP_VERIFY_TOKEN")), "signature_check": bool(wa.cfg("WHATSAPP_APP_SECRET")),
            "webhook_path": "/webhook/whatsapp", "voice_reply": wa.cfg("WHATSAPP_VOICE_REPLY", "1") == "1"}


@app.get("/api/telegram/status", tags=["integrations"])
def telegram_status():
    from . import telegram
    return {"configured": telegram.configured(), "username": telegram.me().get("username"), "link": telegram.link(),
            "mode": "polling", "voice_reply": telegram.cfg("TELEGRAM_VOICE_REPLY", "1") == "1"}


# ------------------------------------------------------------------- QR codes and poster
def _lan_ip() -> str | None:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packet is sent; this just picks the outgoing interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


@app.get("/api/share", tags=["integrations"])
def share(request: Request):
    """Links worth putting in a QR code: the website (public URL, else this Wi-Fi) and the WhatsApp chat."""
    base = str(request.base_url).rstrip("/")
    from .public import url as public_url
    public = public_url() or ""
    port = request.url.port or 8000
    ip = _lan_ip()
    lan = f"http://{ip}:{port}" if ip else None
    site = public or (lan if "localhost" in base or "127.0.0.1" in base else base)
    from . import telegram
    # WHATSAPP_PAUSED=1 hides the WhatsApp QR (e.g. while the number awaits Meta business verification)
    num = "" if wa.cfg("WHATSAPP_PAUSED") == "1" else re.sub(r"\D", "", wa.cfg("WHATSAPP_NUMBER"))
    return {"site_url": site, "public_url": public or None, "lan_url": lan, "local": "localhost" in site or "127.0.0.1" in site,
            "whatsapp_url": f"https://wa.me/{num}?text=" + urllib.parse.quote("hi") if num else None,
            "telegram_url": telegram.link()}


@app.get("/api/qr.svg", tags=["integrations"])
def qr_svg(data: str, scale: int = Query(8, ge=2, le=20), dark: str = "#111111"):
    import io
    import segno
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="svg", scale=scale, border=2, dark=dark, light="#ffffff")
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})


@app.get("/poster", include_in_schema=False, response_class=HTMLResponse)
def poster(request: Request):
    """A printable A4 poster for bus stops: scan to open BheedBus, scan to chat on WhatsApp."""
    sh = share(request)
    q = lambda u: "/api/qr.svg?scale=10&data=" + urllib.parse.quote(u, safe="")  # noqa: E731
    wa_block = (f'<div class="qr"><img src="{q(sh["whatsapp_url"])}" alt="QR code for WhatsApp"><b>Ask on WhatsApp</b>'
                f'<span>Text or send a voice note<br>मैसेज या वॉइस नोट भेजें</span></div>') if sh["whatsapp_url"] else ""
    if sh["telegram_url"]:
        wa_block += (f'<div class="qr"><img src="{q(sh["telegram_url"])}" alt="QR code for Telegram"><b>Ask on Telegram</b>'
                     f'<span>Text, voice note or your location<br>मैसेज, वॉइस नोट या लोकेशन भेजें</span></div>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>BheedBus poster</title><style>
@page {{ size: A4; margin: 14mm; }}
body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0; background: #fff; color: #111; text-align: center; }}
.p {{ max-width: 720px; margin: 24px auto; padding: 28px; border: 3px solid #111; border-radius: 28px; }}
h1 {{ font-size: 54px; margin: 0; letter-spacing: -0.03em; }} h2 {{ font-size: 26px; margin: 6px 0 2px; font-weight: 600; }}
.hi {{ font-size: 24px; color: #444; margin: 0 0 22px; }}
.qrs {{ display: flex; gap: 28px; justify-content: center; flex-wrap: wrap; margin: 18px 0; }}
.qr {{ display: flex; flex-direction: column; align-items: center; gap: 6px; }} .qr img {{ width: 240px; height: 240px; }}
.qr b {{ font-size: 22px; }} .qr span {{ color: #444; font-size: 15px; }}
.lv {{ display: flex; gap: 14px; justify-content: center; flex-wrap: wrap; margin: 14px 0 6px; font-size: 16px; }}
.lv i {{ display: inline-block; width: 14px; height: 14px; border-radius: 4px; vertical-align: -2px; margin-right: 5px; }}
.small {{ color: #666; font-size: 13px; margin-top: 18px; }} .noprint {{ margin: 16px; }}
@media print {{ .noprint {{ display: none; }} .p {{ margin: 0 auto; }} }}
</style></head><body><div class="noprint"><button onclick="print()">🖨️ Print poster</button></div><div class="p">
<div style="font-size:64px">🚌</div><h1>BheedBus</h1><h2>How full is your bus?</h2><p class="hi">आपकी बस में कितनी भीड़ है?</p>
<div class="qrs"><div class="qr"><img src="{q(sh['site_url'])}" alt="QR code for the BheedBus website"><b>Open BheedBus</b><span>Plan a trip with a seat<br>सीट वाली यात्रा चुनें</span></div>{wa_block}</div>
<div class="lv"><span><i style="background:#0ca30c"></i>Seats</span><span><i style="background:#fab219"></i>Standing room</span><span><i style="background:#ec835a"></i>Crowded</span><span><i style="background:#d03b3b"></i>Packed</span></div>
<p class="small">Typical crowding by route and hour, from 2.56 million October 2026 tickets. {sh['site_url']}</p></div></body></html>"""


@app.get("/privacy", include_in_schema=False, response_class=HTMLResponse)
def privacy():
    """Privacy policy (Meta asks for one before an app that uses WhatsApp can be published)."""
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>BheedBus privacy policy</title><style>body{font:16px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;max-width:720px;margin:40px auto;padding:0 20px;color:#111}h1{font-size:28px}h2{font-size:18px;margin-top:28px}</style></head><body>
<h1>BheedBus privacy policy</h1><p>BheedBus is a student hackathon prototype that tells Delhi bus commuters how crowded buses usually are. Last updated 18 November 2026.</p>
<h2>What we receive</h2><p>When you message BheedBus on WhatsApp we receive your WhatsApp phone number, the text you send, and any voice notes you send. On the website we receive the trip searches you type.</p>
<h2>How we use it</h2><p>Only to answer your question. Voice notes are converted to text on our own computer and deleted straight after. The last few messages of a conversation are kept in memory so follow-up questions make sense, and are lost when the server restarts. We do not sell, share or advertise with your data, and we do not use it to train AI models.</p>
<h2>Who else sees it</h2><p>WhatsApp messages travel through Meta's WhatsApp Business Platform under Meta's own terms. No other third party receives your messages.</p>
<h2>Bus data</h2><p>Crowding estimates come from anonymous October 2026 ticket records published for the Transport Stack Hackathon. They contain no passenger identities.</p>
<h2>Deleting your data</h2><p>Stop messaging or block the number; nothing is stored long-term. To ask about your data, contact the developer listed on the app.</p>
</body></html>"""


# ------------------------------------------------------------------- data lab
def _run_pipeline(job_id: str, csv: Path | None) -> None:
    job = jobs[job_id]
    cmd = [sys.executable, str(ROOT / "pipeline" / "build.py")]
    if csv is not None:
        cmd += ["--csv", str(csv)]
    job["status"] = "running"
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        job["log"].append(line.rstrip())
    proc.wait()
    if proc.returncode == 0:
        get_model().reload()
        get_model().insights.reload()
        job["status"] = "done"
    else:
        job["status"] = "failed"
    job["finished"] = time.time()


@app.post("/api/upload", tags=["data lab"])
async def upload(file: UploadFile = File(...)):
    """Drop a ticket CSV (or .zip of one) to rebuild the crowding model, or a GTFS .zip to add
    scheduled frequencies and stop coordinates."""
    if os.environ.get("BHEEDBUS_READONLY") == "1":
        raise HTTPException(403, "Rebuilding is switched off on the hosted demo. Run BheedBus locally to load new data.")
    if any(j["status"] == "running" for j in jobs.values()):
        raise HTTPException(409, "A rebuild is already running")
    job_id = uuid.uuid4().hex[:8]
    updir = ROOT / "data" / "uploads" / job_id
    updir.mkdir(parents=True, exist_ok=True)
    name = Path(file.filename or "upload").name
    dst = updir / name
    size = 0
    with dst.open("wb") as f:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD:
                raise HTTPException(413, "File too large")
            f.write(chunk)
    kind, csv = None, None
    if name.lower().endswith(".csv"):
        kind, csv = "tickets", dst
    elif name.lower().endswith(".zip"):
        with zipfile.ZipFile(dst) as z:
            names = [Path(n).name for n in z.namelist() if not n.startswith("__MACOSX")]
            if "stops.txt" in names and "stop_times.txt" in names:
                kind = "gtfs"
                (ROOT / CFG["gtfs_zip"]).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(dst, ROOT / CFG["gtfs_zip"])
            else:
                member = next((n for n in z.namelist() if n.lower().endswith(".csv") and "__MACOSX" not in n), None)
                if member is None:
                    raise HTTPException(400, "Zip has neither a ticket CSV nor a GTFS feed")
                kind, csv = "tickets", updir / Path(member).name
                with z.open(member) as src, csv.open("wb") as out:  # basename only: no path traversal
                    shutil.copyfileobj(src, out)
    else:
        raise HTTPException(400, "Upload a .csv ticket file or a .zip (tickets or GTFS)")
    jobs[job_id] = {"id": job_id, "kind": kind, "file": name, "bytes": size, "status": "queued",
                    "log": [f"received {name} ({size / 1e6:.1f} MB) as {kind}"], "started": time.time()}
    if kind == "gtfs":
        csv = None  # rebuild from the current ticket CSV with the new feed
    threading.Thread(target=_run_pipeline, args=(job_id, csv), daemon=True).start()
    return jobs[job_id]


@app.get("/api/jobs/{job_id}", tags=["data lab"])
def job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404)
    return jobs[job_id]


@app.get("/api/info", tags=["data lab"])
def info():
    m = get_model()
    return {"info": m.info, "routes": len(m.routes), "places": len(m.place_label), "ai": ai.engine_name(), "local_model": ai.OLLAMA_MODEL,
            "readonly": os.environ.get("BHEEDBUS_READONLY") == "1",
            "gtfs": bool(m.coords), "levels": CFG["levels"], "scale": m.scale}


@app.get("/api/note", tags=["data lab"], response_class=PlainTextResponse)
def note():
    return (ROOT / "docs" / "TECHNICAL_NOTE.md").read_text()


# --------------------------------------------------------------------- static
WEB = ROOT / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB / "index.html")

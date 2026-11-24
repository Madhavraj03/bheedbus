# 🚌 BheedBus: crowding-aware journey planner for Delhi buses

Transport Stack Hackathon 2026 · Problem 2 / Phase 1.

BheedBus turns one month of e-ticket data into crowding estimates by route, stretch and hour. It shows those estimates next to every journey option, so commuters can choose between **faster** and **more comfortable** trips. Operators can see **where extra buses would help most**.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# the dataset is not in this repo: put the hackathon's October-Delhi.csv in data/raw/ (or drop it into the Data lab later)
.venv/bin/python pipeline/build.py
.venv/bin/python -m uvicorn server.app:app --port 8000
```

Open http://localhost:8000. The API docs are at http://localhost:8000/docs.

Chat bots and links are optional: copy `.env.example` to `.env` and fill in what you use ([Telegram](docs/TELEGRAM_SETUP.md), [WhatsApp](docs/WHATSAPP_SETUP.md)).

### Live AI assistant (free, runs on your Mac)

The **✨ Ask AI** chat uses the first engine available and shows each step live (thinking → planning your trip → reading the results):

1. **Claude**, if `ANTHROPIC_API_KEY` is set (paid, best quality, any language);
2. **Local AI via Ollama** (free, private, offline): `qwen2.5:7b` calls the same BheedBus tools;
3. **Built-in parser**: the fallback for English and Hinglish when no AI is running.

To set up the local AI once:

```bash
brew install ollama && brew services start ollama
ollama pull qwen2.5:7b
```

Use `OLLAMA_MODEL=llama3.2` for a smaller or faster model.

## What's inside

| | |
|---|---|
| **Plan a trip** | From/to/time/day, plus a ⚡ Faster ↔ 💺 Comfier slider. Each option shows time, changes, and a strip coloured by crowding for every stretch, with waits hatched. Badges mark *Fastest*, *Most comfortable* and *Leave 20 min later*. Drag cards into the **Compare** tray. |
| **💺 Chance of a seat** | Every trip shows the probability of a free seat when you board, e.g. "💺 94%". It comes from day-to-day variation in the data plus bus bunching. Each leg also says when you'll *likely* get a seat. |
| **Ask BheedBus, with voice** | Type or 🎙️ speak, in English, Hinglish ("Anand Vihar se CP shaam 6 baje, seat chahiye") or हिन्दी. Answers can be read aloud. Claude calls the planner and crowding tools; offline, a built-in parser handles English and Hinglish. |
| **Route explorer + 🗺️ map** | Type-ahead route search (matches highlighted, ↑/↓ + Enter). A real map of every station on the route, coloured by crowding for the chosen hour, with a line diagram and a stretch × hour heat map. Click any route badge on a trip card to see that leg on the map. |
| **Operators** | KPIs, hourly peaks (weekday vs weekend), share of crowded bus-hours, a hotspot table with **buses/hour to add** (CSV export), a ridership sensitivity slider, and holdout validation. |
| **🔀 Fleet rebalancer** | Drag a 🚌 from a route with spare seats onto a crowded one, and crowding recalculates live with the fleet size fixed. **✨ Auto-optimise** finds the best 40 moves: −7.8% crowded passenger-minutes, ~3,800 crowded passenger-hours saved per weekday morning peak. |
| **🏙️ City tab** | ▶ **Delhi time-lapse**: every mapped stretch coloured by crowding hour by hour. **Festivals found blind**: the tickets alone flag Gandhi Jayanti, Navratri and Dussehra (4 of 4) plus sudden crowds at places. **Bus bunching**: buses of a route arrive in packs (pair-correlation 0.43 at 5 am → above 1 by evening). **Nudge simulator**: what if 10% left 20 min later? **Riders check the model**: riders rate how full their bus was (⭐ in the Telegram/WhatsApp menu, or on web trip cards). |
| **Data lab** | Drag and drop a new month's ticket CSV, or a GTFS zip, to rebuild the model in place. Also: n8n workflows and this note. |
| **n8n AI agent** | `web/n8n/bheedbus-ai-agent.json`: Chat Trigger → AI Agent (Ollama `qwen2.5:7b`, conversation memory) with 4 HTTP tools (plan_journey, route_crowding, operator_hotspots, search_places). Import it, add an Ollama credential, and click **Open chat**. |
| **n8n** | `web/n8n/bheedbus-crowding-alerts.json`: hourly alerts to Telegram and Slack. `web/n8n/bheedbus-chatbot.json`: a Telegram trip-planner bot. Set `BHEEDBUS_URL` in n8n. |

## Chat bots (Telegram, WhatsApp) + QR codes

- **WhatsApp:** commuters send a text or a **voice note** (English, Hinglish or Hindi). Voice notes are transcribed on this Mac with Whisper; Hindi speech comes out as Hinglish, e.g. *"Badarpur se Saket jaana hai, subah 9 baje"*. BheedBus answers on WhatsApp in the same language, and replies to voice notes with a voice note. It refuses to guess a trip when the start or destination is missing.
  - Try it without a phone: **Data → WhatsApp bot**.
- **Telegram:** the same chat on Telegram, plus **📍 Share my location** to start from the nearest stops and /menu /trip /route /busy /rate /help commands. No business verification and no public URL needed (the server polls Telegram). Setup: [docs/TELEGRAM_SETUP.md](docs/TELEGRAM_SETUP.md) (one token from @BotFather).
  - Go live: follow [docs/WHATSAPP_SETUP.md](docs/WHATSAPP_SETUP.md) (Meta's free test number plus a Cloudflare tunnel).
- **QR codes:** **📱 Share** in the header shows a QR for the website and ones that open a chat with the bot on Telegram and WhatsApp. `/poster` is a printable A4 bus-stop poster with them.
  - For phones on your Wi-Fi to open the site, start the server with `--host 0.0.0.0`. For a public link, set `PUBLIC_URL`.

## Maps (no API key)

Stop positions come from OpenStreetMap: named bus stops, metro stations and landmarks via the free **Overpass API**, plus the free **Photon** geocoder for stops OSM's stop list lacks. Ticket stop names are matched to points by name, and each route's stop order picks the candidate that keeps the line continuous. The downloads are cached in `data/geo/`. To refresh them:

```bash
.venv/bin/python -m server.geo --fetch --force
```

Map tiles: © OpenStreetMap contributors.

## API (selected)

| Endpoint | Purpose |
|---|---|
| `GET /api/plan?from=&to=&time=18:00&day=weekday&comfort=0.5` | Journey options with crowding per stretch |
| `POST /api/ask {question}` | Natural-language assistant |
| `GET /api/route/{route}?day=` | Load, level and confidence matrix (hour × stretch) |
| `GET /api/route/{route}/map?refine=` | Station coordinates for the route (OpenStreetMap match, geocoder, or between neighbours) |
| `GET /api/crowding?route=&hour=` · `GET /api/geojson?route=&hour=` | Per-stretch crowding (GeoJSON when GTFS is loaded) |
| `GET /api/operator/hotspots?day=&hour=` | Overloaded stretches and extra buses needed |
| `GET /api/operator/rebalance/candidates` · `POST /api/operator/rebalance` · `POST /api/operator/optimise` | Fleet rebalancer: candidates, evaluate moves, auto-optimise |
| `GET /api/alerts` · `POST /api/webhook/chat` | For n8n and chat bots |
| `POST /api/upload` | Rebuild from a dropped CSV or GTFS |
| `GET /api/insights/events` · `GET /api/insights/bunching[/{route}]` · `GET /api/insights/nudge?share=&minutes=` | Unusual days and places, bus bunching, peak-spreading what-if |
| `GET /api/city?day=` | All mapped stretches with a crowding code per hour (time-lapse) |
| `POST /api/feedback` · `GET /api/feedback` | Rider reports vs the model |

## Machine learning (where it helps)

The crowding numbers come from a transparent statistical model. **Machine learning is used only where the data is thin.** A gradient-boosting model fills gaps on quiet stretch-hours (fewer than 15 tickets), and busy ones keep their own data. On weeks it never saw, it cuts Poisson deviance on quiet cells by 61% (week 4) and 42% (untouched 29–31 Oct check), and removes false "crowded" alarms. Full details and caveats are in the technical note.

## Method in one paragraph

Digital tickets are a ~2–5% sample of riders, so a per-trip running count is meaningless. Instead, every ticket is spread over the stretches it rides. The total is scaled to all riders by two anchors (published ridership, and crush capacity) and divided by buses per hour. Bus counts come from a Chao1 estimate of the vehicles seen. Stop order is inferred from the tickets (99.9% agreement). Fit on weeks 1–3, test on week 4: r = 0.97. See [docs/TECHNICAL_NOTE.md](docs/TECHNICAL_NOTE.md) for assumptions and where the estimates are weakest.

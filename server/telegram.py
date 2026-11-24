"""BheedBus on Telegram: the same menu-driven chat as WhatsApp (wa_flow), plus location sharing.

Telegram needs no business verification and no public URL: the server asks Telegram for new
messages (long polling), so it works from a laptop behind Wi-Fi. Settings (environment or `.env`):

    TELEGRAM_BOT_TOKEN      token from @BotFather (keep secret)
    TELEGRAM_VOICE_REPLY    "1" to answer voice notes with a voice note too (default 1)

Extras over WhatsApp: tap "📍 Share my location" to start from the nearest stops, and the
/menu /trip /route /busy /rate /help commands in Telegram's menu button.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .whatsapp import speak_to_file, transcribe

API = "https://api.telegram.org"
COMMANDS = {"start": "m:menu", "menu": "m:menu", "trip": "m:trip", "route": "m:route", "busy": "m:hot",
            "rate": "m:rate", "help": "m:help"}
_me: dict = {}
_running = threading.Event()


def cfg(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def configured() -> bool:
    return bool(cfg("TELEGRAM_BOT_TOKEN"))


def _call(method: str, body: dict | None = None, files: dict | None = None, timeout: float = 40):
    url = f"{API}/bot{cfg('TELEGRAM_BOT_TOKEN')}/{method}"
    if files:  # multipart: photos and voice notes
        boundary = uuid.uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                 for k, v in (body or {}).items()]
        for field, (path, mime) in files.items():
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{Path(path).name}"\r\n'
                          f"Content-Type: {mime}\r\n\r\n").encode() + Path(path).read_bytes() + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(url, data=b"".join(parts), method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=json.dumps(body or {}).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())["result"]
        except urllib.error.HTTPError as e:  # surface Telegram's reason, never the token in the URL
            try:
                desc = json.loads(e.read()).get("description", "")
            except ValueError:
                desc = ""
            raise RuntimeError(f"Telegram {method} {e.code}: {desc}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:  # Wi-Fi blip: try again
            if attempt == 2:
                raise RuntimeError(f"Telegram {method}: network error {getattr(e, 'reason', e)}") from None
            time.sleep(1 + 2 * attempt)


def me() -> dict:
    """The bot's own account (username for the t.me link); cached."""
    if not _me and configured():
        try:
            _me.update(_call("getMe", timeout=10))
        except Exception as e:
            print("telegram: getMe failed:", e)
    return _me


def link() -> str | None:
    u = me().get("username")
    return f"https://t.me/{u}" if u else None


# ---------------------------------------------------------------- outgoing: wa_flow messages -> Telegram
def _keyboard(m: dict) -> dict | None:
    if m["type"] == "buttons":
        return {"inline_keyboard": [[{"text": t, "callback_data": i[:64]}] for i, t in m["buttons"]]}
    if m["type"] == "list":
        def label(t: str, d: str) -> str:  # Telegram cuts long buttons in the middle: keep them short
            full = d.split(" · ", 1)[1] if " · " in d else ""  # place rows carry the full stop name
            if full and len(full) <= 40:
                return full
            return f"{t} · {d}" if d and len(t) + len(d) <= 38 else t
        return {"inline_keyboard": [[{"text": label(t, d), "callback_data": i[:64]}] for i, t, d in m["rows"]]}
    if m["type"] == "cta" and m["url"].startswith("https://"):
        return {"inline_keyboard": [[{"text": m["label"], "url": m["url"]}]]}
    if m.get("ask_location"):
        return {"keyboard": [[{"text": "📍 Share my location", "request_location": True}]],
                "resize_keyboard": True, "one_time_keyboard": True}
    return None


def _send_text(chat: int, text: str, markup: dict | None) -> None:
    body = {"chat_id": chat, "text": text[:4096], "parse_mode": "Markdown", "disable_web_page_preview": True}
    if markup:
        body["reply_markup"] = markup
    try:
        _call("sendMessage", body)
    except RuntimeError as e:
        if "parse" not in str(e).lower():
            raise
        body.pop("parse_mode")  # a stray * or _ in a stop name: send it plain
        _call("sendMessage", body)


def send(chat: int, m: dict) -> None:
    if m["type"] == "later":
        return  # the bot only ever replies
    if m["type"] == "image":
        _call("sendPhoto", {"chat_id": chat, "caption": m.get("caption", "")[:1024]}, {"photo": (m["path"], "image/png")})
        return
    if m["type"] == "voice":
        if cfg("TELEGRAM_VOICE_REPLY", "1") == "1" and (v := speak_to_file(m["text"], m["hindi"])):
            try:
                _call("sendVoice", {"chat_id": chat}, {"voice": (str(v), "audio/ogg")})
            finally:
                v.unlink(missing_ok=True)
        return
    _send_text(chat, m["body"], _keyboard(m))


# ---------------------------------------------------------------- incoming: Telegram update -> wa_flow input
def _download(file_id: str) -> Path:
    f = _call("getFile", {"file_id": file_id})
    with urllib.request.urlopen(f"{API}/file/bot{cfg('TELEGRAM_BOT_TOKEN')}/{f['file_path']}", timeout=60) as r:
        data = r.read()
    p = Path(tempfile.mkstemp(suffix=".ogg")[1])
    p.write_bytes(data)
    return p


def incoming_of(u: dict, place_names: list[str]) -> tuple[int, dict] | None:
    """-> (chat id, wa_flow input), or None for updates we ignore."""
    if q := u.get("callback_query"):
        try:
            _call("answerCallbackQuery", {"callback_query_id": q["id"]}, timeout=10)  # stop the button's spinner
        except Exception:
            pass
        btn = next((b["text"] for row in q["message"].get("reply_markup", {}).get("inline_keyboard", [])
                    for b in row if b.get("callback_data") == q["data"]), "")
        return q["message"]["chat"]["id"], {"type": "choice", "id": q["data"], "title": btn}
    msg = u.get("message")
    if not msg:
        return None
    chat = msg["chat"]["id"]
    if loc := msg.get("location"):
        return chat, {"type": "location", "lat": loc["latitude"], "lon": loc["longitude"]}
    if v := msg.get("voice") or msg.get("audio"):
        f = _download(v["file_id"])
        try:
            t = transcribe(str(f), place_names)
        finally:
            f.unlink(missing_ok=True)
        return chat, {"type": "text", "text": t["english"], "heard": t["text"], "lang": t["lang"], "voice": True}
    text = (msg.get("text") or "").strip()
    if text.startswith("/"):
        cmd = text[1:].split()[0].split("@")[0].lower()
        if cmd in COMMANDS:
            return chat, {"type": "choice", "id": COMMANDS[cmd], "title": cmd}
    if text:
        return chat, {"type": "text", "text": text}
    return None


def _process(u: dict, get_model, place_names, get_ctx) -> None:
    from . import wa_flow
    chat = None
    try:
        got = incoming_of(u, place_names())
        if not got:
            return
        chat, inc = got
        _call("sendChatAction", {"chat_id": chat, "action": "typing"}, timeout=10)
        if inc.get("voice") and not inc.get("text"):
            _send_text(chat, "Sorry, I couldn't hear that clearly. Please try again.", None)
            return
        if inc.get("heard"):
            _send_text(chat, f"🎙️ _You said:_ {inc['heard']}", None)
        out = wa_flow.handle(get_model(), f"tg-{chat}", inc, get_ctx())
        print(f"telegram: …{str(chat)[-4:]} sent {inc.get('id') or inc.get('text', '')[:40] or inc['type']!r} → "
              + ", ".join(m["type"] for m in out))
        for m in out:
            try:
                send(chat, m)
            except Exception as e:
                if m["type"] in ("image", "voice"):  # a map or voice note is a bonus: never block the text answer
                    print(f"telegram: {m['type']} not sent:", e)
                    continue
                raise
    except Exception as e:  # never let one bad message stop the bot
        print("telegram: failed to answer:", type(e).__name__, e)
        if chat is not None:
            try:
                _send_text(chat, "Sorry, something went wrong on our side. Please try again in a minute.", None)
            except Exception:
                pass


def _loop(get_model, place_names, get_ctx) -> None:
    try:
        _call("deleteWebhook", {"drop_pending_updates": False}, timeout=15)  # polling and a webhook can't both be on
        _call("setMyCommands", {"commands": [
            {"command": "menu", "description": "Main menu"}, {"command": "trip", "description": "Plan a trip"},
            {"command": "route", "description": "How crowded is a bus"}, {"command": "busy", "description": "Busiest routes now"},
            {"command": "rate", "description": "Rate my last ride"}, {"command": "help", "description": "How it works"}]}, timeout=15)
        print(f"telegram: polling as @{me().get('username', '?')}")
    except Exception as e:
        print("telegram: setup failed:", e)
    offset, wait = 0, 1
    while _running.is_set():
        try:
            ups = _call("getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]}, timeout=45)
            wait = 1
        except Exception as e:  # network blip or Mac waking from sleep: back off and retry
            print("telegram: polling error:", e)
            time.sleep(wait)
            wait = min(wait * 2, 60)
            continue
        for u in ups:
            offset = u["update_id"] + 1
            threading.Thread(target=_process, args=(u, get_model, place_names, get_ctx), daemon=True).start()


def start(get_model, place_names, get_ctx) -> bool:
    """Start polling in the background (once per process). Returns False if no token is set."""
    if not configured() or _running.is_set():
        return False
    _running.set()
    threading.Thread(target=_loop, args=(get_model, place_names, get_ctx), daemon=True, name="telegram").start()
    return True


def stop() -> None:
    _running.clear()

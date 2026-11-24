"""BheedBus on WhatsApp: text and voice notes in, answers (and optional voice replies) out.

Uses Meta's WhatsApp Cloud API. Settings come from environment variables (or a
`.env` file next to config.json):

    WHATSAPP_TOKEN          access token from the Meta app (keep secret)
    WHATSAPP_PHONE_ID       "Phone number ID" of the WhatsApp sender
    WHATSAPP_VERIFY_TOKEN   any string you choose; paste the same into Meta's webhook form
    WHATSAPP_APP_SECRET     app secret, used to check that webhooks really come from Meta
    WHATSAPP_NUMBER         the bot's number in international form (for the QR code), e.g. 15551234567
    WHATSAPP_VOICE_REPLY    "1" to also answer voice notes with a voice note (default 1)
    WHISPER_MODEL           speech model size: tiny, base, small (default), medium

Voice notes are transcribed locally with Whisper (faster-whisper), so audio never
leaves this machine except to and from WhatsApp itself.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import defaultdict, deque
from pathlib import Path

from . import ai

GRAPH = "https://graph.facebook.com/v21.0"
ROOT = Path(__file__).resolve().parents[1]
VOICE_DIR = ROOT / "data" / "voice"


def cfg(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def configured() -> bool:
    return bool(cfg("WHATSAPP_TOKEN") and cfg("WHATSAPP_PHONE_ID"))


# ---------------------------------------------------------------- speech to text (local Whisper)
_whisper = None
_whisper_lock = threading.Lock()


def _model():
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel
            _whisper = WhisperModel(cfg("WHISPER_MODEL", "small"), device="cpu", compute_type="int8")
    return _whisper


def _prompt(place_names: list[str]) -> str:
    # a SHORT hint fixes spellings ("Konaot" -> "Connaught"); a long list of names makes Whisper drop words
    return "Delhi bus stops: Connaught Place, Kashmere Gate, Anand Vihar ISBT, Nehru Place, Saket, Badarpur."


# Indian languages Whisper may detect for Hindi/Hinglish speech
INDIC = {"hi", "ur", "pa", "mr", "bn", "gu", "ne"}
# A romanised-Hinglish prompt makes Whisper write Hindi speech in Latin letters
# ("Anand Vihar se Connaught Place shaam 6 baje"), which matches our stop names.
# Kept generic (no full trip) so it is not parroted back on unclear audio.
HINGLISH_PROMPT = "Haan ji, bus ka sawaal hai. Kashmere Gate, Nehru Place, Saket, Badarpur, Connaught Place, Anand Vihar."


# Whisper's usual romanisations of Hindi words, mapped to the spellings the assistant expects
HINGLISH_FIXES = [(r"\bbaja[iy]\b", "baje"), (r"\bshyam\b", "shaam"), (r"\bsi\b", "se"), (r"\bchahi(ye|e|y)\b", "chahiye"),
                  (r"\bjana\b", "jaana"), (r"\bsubha\b", "subah")]


def transcribe(path: str, place_names: list[str]) -> dict:
    """-> {text: what the user said (Latin script), english: same text for the assistant, lang, original}"""
    m = _model()
    segs, info = m.transcribe(path, beam_size=1, vad_filter=True)
    raw = " ".join(s.text.strip() for s in segs).strip()
    lang = info.language
    if lang in INDIC and raw:
        segs, _ = m.transcribe(path, beam_size=1, vad_filter=True, language="hi", initial_prompt=HINGLISH_PROMPT)
        text = " ".join(s.text.strip() for s in segs).strip() or raw
        lang = "hi"
    else:
        segs, _ = m.transcribe(path, beam_size=1, vad_filter=True, language=lang, initial_prompt=_prompt(place_names))
        text = " ".join(s.text.strip() for s in segs).strip() or raw
    # a spoken "to" often comes out as the digit 2: "Anand Vihar 2 Connaught Place"
    text = re.sub(r"(?<=[A-Za-z])\s+2\s+(?=[A-Z])", " to ", text)
    if lang == "hi":
        text = re.sub(r"[\u0900-\u097F]+[,.]?\s*", "", text).strip()  # stray Devanagari words ("मुजी")
        for wrong, right in HINGLISH_FIXES:
            text = re.sub(wrong, right, text, flags=re.I)
    return {"text": text, "english": text, "lang": lang, "original": raw}


# ---------------------------------------------------------------- text to speech (macOS voices)
def speak_to_file(text: str, hindi: bool, fmt: str = "ogg") -> Path | None:
    """Make a voice note: macOS `say` (natural Indian voices) or, on Linux servers, espeak-ng.
    Encoded with ffmpeg: ogg/opus for WhatsApp, m4a for browsers."""
    tts = "say" if shutil.which("say") else ("espeak-ng" if shutil.which("espeak-ng") else None)
    if not tts or not shutil.which("ffmpeg"):
        return None
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    clean = re.sub(r"[*_~•→]", " ", text)
    clean = re.sub(r"\b(\d+[A-Z]*)(UP|DOWN)\b", r"\1 \2", clean)  # "433DOWN" -> "433 down"
    with tempfile.TemporaryDirectory() as td:
        aiff = Path(td) / ("v.aiff" if tts == "say" else "v.wav")
        if tts == "say":
            voice = "Lekha" if hindi else "Rishi"
            r = subprocess.run(["say", "-v", voice, "-o", str(aiff), clean[:900]], capture_output=True)
            if r.returncode != 0:
                subprocess.run(["say", "-o", str(aiff), clean[:900]], capture_output=True)
        else:
            # espeak-ng's Hindi voice needs Devanagari; Latin-script Hinglish reads better with Indian English
            voice = "hi" if re.search(r"[\u0900-\u097F]", clean) else "en-in"
            subprocess.run(["espeak-ng", "-v", voice, "-s", "155", "-w", str(aiff), clean[:900]], capture_output=True)
        out = VOICE_DIR / f"{uuid.uuid4().hex[:12]}.{fmt}"
        codec = ["-c:a", "libopus", "-b:a", "32k", "-ac", "1", "-ar", "48000"] if fmt == "ogg" else ["-c:a", "aac", "-b:a", "64k"]
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff), *codec, str(out)], capture_output=True)
        return out if r.returncode == 0 and out.exists() else None


# ---------------------------------------------------------------- answering
_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=6))


def answer(model, text: str, user: str, lang_hint: str = "", ctx: dict | None = None) -> dict:
    """Run the assistant for one WhatsApp message and format the reply for WhatsApp."""
    ctx = dict(ctx or {})
    if lang_hint == "hi" or ai.HINGLISH.search(text or ""):
        ctx["lang"] = "hinglish"
    res = ai.ask(model, text, ctx, list(_history[user]))
    reply = res.get("answer") or "Sorry, I couldn't work that out. Try: Anand Vihar to Connaught Place at 6 pm."
    _history[user].extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
    return {"reply": _whatsappify(reply), "engine": res.get("engine"), "plans": res.get("plans", [])}


def _whatsappify(s: str) -> str:
    s = s.replace("**", "*")
    s = re.sub(r"\b(\d+[A-Z]*(?:UP|DOWN))\b", r"*\1*", s)  # bold bus numbers
    return s + "\n\n_Typical crowding from Oct 2026 tickets · BheedBus_"


# ---------------------------------------------------------------- WhatsApp Cloud API
def _graph(method: str, path: str, body: dict | None = None, raw: bytes | None = None, ctype: str | None = None):
    req = urllib.request.Request(f"{GRAPH}/{path}" if not path.startswith("http") else path, method=method,
                                 data=raw if raw is not None else (json.dumps(body).encode() if body else None))
    req.add_header("Authorization", f"Bearer {cfg('WHATSAPP_TOKEN')}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
    except urllib.error.HTTPError as e:  # surface Meta's reason (e.g. recipient not in the test allow-list)
        err: dict = {}
        try:
            err = json.loads(e.read()).get("error", {})
            msg = f"Meta error {err.get('code')}: {err.get('message')} {err.get('error_data', {}).get('details', '')}".strip()
        except ValueError:
            msg = str(e)
        if err.get("code") == 131030:
            msg += " -> add this number under Step 1 > Recipient in the Meta app (test numbers only reach verified recipients)"
        raise RuntimeError(msg) from None
    try:
        return json.loads(data)
    except ValueError:
        return data


def send_text(to: str, body: str) -> None:
    _graph("POST", f"{cfg('WHATSAPP_PHONE_ID')}/messages",
           {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body[:4000], "preview_url": False}})


def _upload(path: Path, mime: str) -> str:
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="messaging_product"\r\n\r\nwhatsapp\r\n'.encode(),
             f'--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\n{mime}\r\n'.encode(),
             (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
              f"Content-Type: {mime}\r\n\r\n").encode() + path.read_bytes() + b"\r\n",
             f"--{boundary}--\r\n".encode()]
    up = _graph("POST", f"{cfg('WHATSAPP_PHONE_ID')}/media", raw=b"".join(parts),
                ctype=f"multipart/form-data; boundary={boundary}")
    return up["id"]


def send_image(to: str, path: Path, caption: str = "") -> None:
    mid = _upload(path, "image/png")
    _graph("POST", f"{cfg('WHATSAPP_PHONE_ID')}/messages",
           {"messaging_product": "whatsapp", "to": to, "type": "image", "image": {"id": mid, "caption": caption[:1024]}})


def send_voice(to: str, path: Path) -> None:
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="messaging_product"\r\n\r\nwhatsapp\r\n'.encode(),
             f'--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\naudio/ogg\r\n'.encode(),
             (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
              f"Content-Type: audio/ogg\r\n\r\n").encode() + path.read_bytes() + b"\r\n",
             f"--{boundary}--\r\n".encode()]
    up = _graph("POST", f"{cfg('WHATSAPP_PHONE_ID')}/media", raw=b"".join(parts),
                ctype=f"multipart/form-data; boundary={boundary}")
    _graph("POST", f"{cfg('WHATSAPP_PHONE_ID')}/messages",
           {"messaging_product": "whatsapp", "to": to, "type": "audio", "audio": {"id": up["id"]}})


def mark_read(message_id: str) -> None:
    try:
        _graph("POST", f"{cfg('WHATSAPP_PHONE_ID')}/messages",
               {"messaging_product": "whatsapp", "status": "read", "message_id": message_id})
    except Exception:
        pass  # read receipts are nice-to-have


def download_media(media_id: str) -> Path:
    meta = _graph("GET", media_id)
    data = _graph("GET", meta["url"])
    f = Path(tempfile.mkstemp(suffix=".ogg")[1])
    f.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
    return f


def signature_ok(raw: bytes, header: str | None) -> bool:
    secret = cfg("WHATSAPP_APP_SECRET")
    if not secret:
        return True  # not configured: accept (development only)
    if not header or not header.startswith("sha256="):
        return False
    want = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header[7:])


_seen: deque = deque(maxlen=500)  # Meta retries webhooks; answer each message once


def handle_webhook(payload: dict, model, place_names: list[str], ctx: dict) -> int:
    """Queue every incoming message for background processing. Returns how many were queued."""
    n = 0
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for stt in change.get("value", {}).get("statuses", []) or []:
                if stt.get("status") == "failed":  # Meta accepted the reply but couldn't deliver it
                    for e in stt.get("errors", []):
                        print(f"whatsapp: reply to …{stt.get('recipient_id', '')[-4:]} failed: "
                              f"{e.get('code')} {e.get('title')} {e.get('error_data', {}).get('details', '')}")
            for msg in change.get("value", {}).get("messages", []) or []:
                if msg.get("id") in _seen:
                    continue
                _seen.append(msg.get("id"))
                threading.Thread(target=_process, args=(msg, model, place_names, ctx), daemon=True).start()
                n += 1
    return n


def to_payload(to: str, m: dict) -> dict | None:
    """Turn a wa_flow message into a WhatsApp Cloud API payload (voice notes are sent separately)."""
    base = {"messaging_product": "whatsapp", "to": to}
    if m["type"] == "text":
        return {**base, "type": "text", "text": {"body": m["body"][:4000], "preview_url": False}}
    if m["type"] == "buttons":
        return {**base, "type": "interactive", "interactive": {
            "type": "button", "body": {"text": m["body"][:1024]},
            "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t[:20]}} for i, t in m["buttons"][:3]]}}}
    if m["type"] == "list":
        return {**base, "type": "interactive", "interactive": {
            "type": "list", "body": {"text": m["body"][:1024]},
            "action": {"button": m["button"][:20], "sections": [{"title": "Options", "rows": [
                {"id": i, "title": t[:24], "description": d[:72]} for i, t, d in m["rows"][:10]]}]}}}
    if m["type"] == "cta":
        return {**base, "type": "interactive", "interactive": {
            "type": "cta_url", "body": {"text": m["body"][:1024]},
            "action": {"name": "cta_url", "parameters": {"display_text": m["label"][:20], "url": m["url"]}}}}
    return None


def incoming_of(msg: dict, place_names: list[str]) -> dict | None:
    """Webhook message -> wa_flow input. Returns None for types we don't handle."""
    kind = msg.get("type")
    if kind == "text":
        return {"type": "text", "text": msg["text"]["body"]}
    if kind == "interactive":
        it = msg["interactive"]
        r = it.get("button_reply") or it.get("list_reply") or {}
        return {"type": "choice", "id": r.get("id", ""), "title": r.get("title", "")}
    if kind == "location":
        return {"type": "location", "lat": msg["location"]["latitude"], "lon": msg["location"]["longitude"]}
    if kind == "button":  # quick-reply button on a template message
        return {"type": "text", "text": msg["button"].get("text", "")}
    if kind == "audio":
        f = download_media(msg["audio"]["id"])
        try:
            t = transcribe(str(f), place_names)
        finally:
            f.unlink(missing_ok=True)
        return {"type": "text", "text": t["english"], "heard": t["text"], "lang": t["lang"], "voice": True}
    return None


def _send_quietly(user: str, m: dict) -> None:
    try:
        _graph("POST", f"{cfg('WHATSAPP_PHONE_ID')}/messages", to_payload(user, m))
        print(f"whatsapp: …{user[-4:]} delayed {m['type']} sent")
    except Exception as e:
        print("whatsapp: delayed message not sent:", e)


def _process(msg: dict, model, place_names: list[str], ctx: dict) -> None:
    from . import wa_flow
    user, mid = msg.get("from"), msg.get("id")
    try:
        mark_read(mid)
        inc = incoming_of(msg, place_names)
        if inc is None:
            for m in wa_flow.menu("Namaste! 🚌 I can read text and voice notes."):
                _graph("POST", f"{cfg('WHATSAPP_PHONE_ID')}/messages", to_payload(user, m))
            return
        if inc.get("voice") and not inc.get("text"):
            send_text(user, "Sorry, I couldn't hear that clearly. Please try again.")
            return
        if inc.get("heard"):
            send_text(user, f"🎙️ _You said:_ {inc['heard']}")
        out = wa_flow.handle(model, user, inc, ctx)
        print(f"whatsapp: …{(user or '')[-4:]} sent {inc.get('id') or inc.get('text', '')[:40]!r} → "
              + ", ".join(m["type"] + (f"({m['delay_s'] / 60:g} min)" if m["type"] == "later" else "") for m in out))
        for m in out:
            if m["type"] == "image":
                try:
                    send_image(user, Path(m["path"]), m.get("caption", ""))
                except Exception as e:  # a map is a bonus: never block the text answer
                    print("whatsapp: map image not sent:", e)
                continue
            if m["type"] == "later":  # e.g. "how full was the bus?" after the ride (inside WhatsApp's 24 h window)
                t = threading.Timer(m["delay_s"], _send_quietly, args=(user, m["msg"]))
                t.daemon = True
                t.start()
                continue
            if m["type"] == "voice":
                if cfg("WHATSAPP_VOICE_REPLY", "1") == "1" and (v := speak_to_file(m["text"], m["hindi"])):
                    send_voice(user, v)
                    v.unlink(missing_ok=True)
                continue
            _graph("POST", f"{cfg('WHATSAPP_PHONE_ID')}/messages", to_payload(user, m))
    except Exception as e:  # never let one bad message kill the webhook
        print("whatsapp: failed to answer", mid, type(e).__name__, e)
        try:
            send_text(user, "Sorry, something went wrong on our side. Please try again in a minute.")
        except Exception:
            pass

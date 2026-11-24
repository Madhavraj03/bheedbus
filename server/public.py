"""The public address of this server, for links in chat replies and QR codes.

A free Cloudflare quick tunnel gets a new random address every time it starts. Instead of
copying it into PUBLIC_URL by hand, ask the running cloudflared for its address (its local
metrics port reports it) and check that the address really reaches us. PUBLIC_URL still wins
when it works, e.g. a fixed Hugging Face or named-tunnel address.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

_cache: dict = {"t": 0.0, "url": None, "good": None}
_lock = threading.Lock()
TTL = 60


def _reaches(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/info", timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def _tunnels() -> list[str]:
    out = []
    for port in range(20241, 20246):  # cloudflared's default metrics ports
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/quicktunnel", timeout=1) as r:
                host = json.loads(r.read()).get("hostname")
            if host:
                out.append(f"https://{host}")
        except Exception:
            pass
    return out


def url() -> str | None:
    """Working public https address, or None (then links are left out). Cached for a minute."""
    with _lock:
        if time.time() - _cache["t"] < TTL:
            return _cache["url"]
        env = os.environ.get("PUBLIC_URL", "").rstrip("/")
        found = None
        for cand in ([env] if env.startswith("https://") else []) + _tunnels():
            if _reaches(cand):
                found = cand
                break
        if found is None and env.startswith("https://") and not _tunnels():
            found = env  # no tunnel on this machine (e.g. hosted): trust the setting
        if found is None and _cache["good"] in _tunnels():
            found = _cache["good"]  # a slow check isn't a dead tunnel: keep the last address that worked
        _cache.update(t=time.time(), url=found, good=found or _cache["good"])
        if found and found != env:
            print("public address:", found)
        return found

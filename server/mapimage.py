"""Static map images (PNG) for WhatsApp: a trip or a route on OpenStreetMap, coloured by crowding.

Tiles come from tile.openstreetmap.org and are cached on disk under data/tiles, as the
OSM tile usage policy asks (identify the app, cache, no bulk downloading).
"""
from __future__ import annotations

import io
import math
import urllib.request
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
TILES = ROOT / "data" / "tiles"
OUT = ROOT / "data" / "maps"
UA = "BheedBus/1.0 (Delhi bus crowding hackathon prototype)"
COLORS = {"seats": "#0ca30c", "standing": "#fab219", "crowded": "#ec835a", "packed": "#d03b3b"}
W, H, PAD, TOP, BOTTOM = 1000, 760, 70, 92, 56


def _font(size: int, bold: bool = False):
    for f in (["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold else
              ["/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _xy(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = 256 * 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def _tile(z: int, x: int, y: int) -> Image.Image:
    f = TILES / str(z) / str(x) / f"{y}.png"
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(f"https://tile.openstreetmap.org/{z}/{x}/{y}.png", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            f.write_bytes(r.read())
    return Image.open(io.BytesIO(f.read_bytes())).convert("RGB")


def render(lines: list[dict], markers: list[dict], title: str, subtitle: str = "") -> Path:
    """lines: [{"points": [(lat, lon), ...], "level": "seats"|..., "width": int, "faint": bool}]
    markers: [{"lat", "lon", "color", "label", "big": bool}]. Returns the PNG path."""
    pts = [p for l in lines for p in l["points"]] + [(m["lat"], m["lon"]) for m in markers]
    lats, lons = [p[0] for p in pts], [p[1] for p in pts]
    view_w, view_h = W - 2 * PAD, H - TOP - BOTTOM - 2 * PAD
    z = 16
    while z > 9:
        x0, y1 = _xy(min(lats), min(lons), z)
        x1, y0 = _xy(max(lats), max(lons), z)
        if x1 - x0 <= view_w and y1 - y0 <= view_h:
            break
        z -= 1
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    left, top = cx - W / 2, cy - (H - TOP - BOTTOM) / 2 - TOP
    img = Image.new("RGB", (W, H), "#f2efe9")
    for tx in range(int(left // 256), int((left + W) // 256) + 1):
        for ty in range(int(top // 256), int((top + H) // 256) + 1):
            try:
                img.paste(_tile(z, tx % 2 ** z, ty), (int(tx * 256 - left), int(ty * 256 - top)))
            except Exception:
                pass  # a missing tile leaves plain background; the route still draws
    # soften the base map so the route stands out
    img = Image.blend(img, Image.new("RGB", (W, H), "#ffffff"), 0.35)
    d = ImageDraw.Draw(img)
    px = lambda lat, lon: (_xy(lat, lon, z)[0] - left, _xy(lat, lon, z)[1] - top)  # noqa: E731
    for l in lines:  # dark casing first, so colours read on any background
        if len(l["points"]) > 1 and not l.get("faint"):
            d.line([px(*p) for p in l["points"]], fill="#1b1b1b", width=l.get("width", 8) + 5, joint="curve")
    for l in lines:
        if len(l["points"]) > 1:
            d.line([px(*p) for p in l["points"]], fill="#9a9a9a" if l.get("faint") else COLORS[l["level"]],
                   width=4 if l.get("faint") else l.get("width", 8), joint="curve")
    for l in lines:  # direction arrows: a small white triangle every ~110 px, pointing the way the bus goes
        if len(l["points"]) > 1 and not l.get("faint"):
            _arrows(d, [px(*p) for p in l["points"]])
    f_lab = _font(19, bold=True)
    for m in markers:
        x, y = px(m["lat"], m["lon"])
        r = 13 if m.get("big") else 9
        d.ellipse([x - r - 3, y - r - 3, x + r + 3, y + r + 3], fill="#ffffff")
        d.ellipse([x - r, y - r, x + r, y + r], fill=m["color"])
        if m.get("label"):
            tw = d.textlength(m["label"], font=f_lab)
            lx = min(max(x + r + 8, 8), W - tw - 8)
            d.text((lx, y - 12), m["label"], font=f_lab, fill="#111111", stroke_width=4, stroke_fill="#ffffff")
    # title bar
    d.rectangle([0, 0, W, TOP], fill="#ffffff")
    d.text((24, 14), title, font=_font(32, bold=True), fill="#111111")
    if subtitle:
        d.text((24, 56), subtitle, font=_font(21), fill="#555555")
    # legend + attribution (required by OpenStreetMap)
    d.rectangle([0, H - BOTTOM, W, H], fill="#ffffff")
    x = 24
    f_leg = _font(19)
    for key, label in (("seats", "Seats"), ("standing", "Standing room"), ("crowded", "Crowded"), ("packed", "Packed")):
        d.rounded_rectangle([x, H - 38, x + 22, H - 18], radius=4, fill=COLORS[key])
        d.text((x + 30, H - 40), label, font=f_leg, fill="#333333")
        x += 40 + d.textlength(label, font=f_leg) + 18
    att = "© OpenStreetMap contributors"
    d.text((W - 24 - d.textlength(att, font=_font(15)), H - 36), att, font=_font(15), fill="#777777")
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{uuid.uuid4().hex[:12]}.png"
    img.save(out, optimize=True)
    return out


def _arrows(d: "ImageDraw.ImageDraw", xy: list[tuple[float, float]], gap: float = 120, size: float = 13) -> None:
    import math
    carry = gap / 2
    for (x0, y0), (x1, y1) in zip(xy, xy[1:]):
        dx, dy = x1 - x0, y1 - y0
        n = math.hypot(dx, dy)
        if n < 1:
            continue
        ux, uy = dx / n, dy / n
        t = carry
        while t < n:
            cx, cy = x0 + ux * t, y0 + uy * t
            tip = (cx + ux * size, cy + uy * size)
            back = (cx - ux * size * 0.6, cy - uy * size * 0.6)
            left = (back[0] - uy * size * 0.8, back[1] + ux * size * 0.8)
            right = (back[0] + uy * size * 0.8, back[1] - ux * size * 0.8)
            d.polygon([tip, left, (cx - ux * size * 0.1, cy - uy * size * 0.1), right], fill="#ffffff", outline="#111111", width=2)
            t += gap
        carry = t - n


def segments(points: list[dict], levels: list[str], lo: int, hi: int, width: int = 9) -> list[dict]:
    """Coloured line pieces for stretches lo..hi-1, merging neighbours with the same level."""
    out: list[dict] = []
    for i in range(lo, hi):
        a, b = points[i], points[i + 1]
        if a["lat"] is None or b["lat"] is None:
            continue
        if out and out[-1]["level"] == levels[i] and out[-1]["points"][-1] == (a["lat"], a["lon"]):
            out[-1]["points"].append((b["lat"], b["lon"]))
        else:
            out.append({"points": [(a["lat"], a["lon"]), (b["lat"], b["lon"])], "level": levels[i], "width": width})
    return out

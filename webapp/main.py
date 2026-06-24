#!/usr/bin/env python3
"""Wohnungssuche MA/LU — Web App

Reads the apartment data from a read-only mounted file (provided by Hermes) and
keeps its own favorites/notes/hidden state in overlay.json. It has no Docker
access and never writes to the Hermes side. Paths are configurable via env so
the app runs identically as a host process or inside its own container.
"""
import asyncio
import json
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

TZ = ZoneInfo("Europe/Berlin")
SECRET = os.environ.get("WOHNUNGEN_SECRET", "")
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/apartment_state.json"))
OVERLAY_FILE = Path(os.environ.get("OVERLAY_FILE", str(Path(__file__).parent / "overlay.json")))

app = FastAPI(docs_url=None, redoc_url=None)


def check_token(token: str):
    if not SECRET or not secrets.compare_digest(token.encode(), SECRET.encode()):
        raise HTTPException(status_code=404)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def load_overlay() -> dict:
    if OVERLAY_FILE.exists():
        try:
            return json.loads(OVERLAY_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_overlay(data: dict):
    tmp = OVERLAY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(OVERLAY_FILE)


def overlay_entry(overlay: dict, key: str) -> dict:
    return overlay.setdefault(key, {})


# Listing fields snapshotted into the overlay when a user favorites, so a
# favorite survives even if Hermes later prunes it from the state.
SNAPSHOT_FIELDS = (
    "id", "title", "location", "info", "zimmer", "flaeche",
    "kaltmiete", "nebenkosten", "warmmiete", "qm_preis", "heizungsart",
    "badewanne", "balkon", "terrasse", "ebk", "baeder", "gaeste_wc",
    "first_seen", "last_seen",
)


def snapshot_of(listing: dict) -> dict:
    return {k: listing[k] for k in SNAPSHOT_FIELDS if k in listing}


def resolve_url(apt_id: str) -> str | None:
    """Map a display ID (MA-0042) to its stable URL key in the Hermes state.

    The overlay is keyed by URL — the apartment's true identity — so that
    Hermes is free to reuse/renumber IDs without ever mis-attaching our
    favorites/notes/hidden flags. If an ID is itself a URL, pass it through.
    """
    if apt_id.startswith("http"):
        return apt_id.rstrip("/")
    state = load_state()
    for url, entry in state.items():
        if isinstance(entry, dict) and entry.get("id") == apt_id:
            return url
    return None


# ── Data helpers ────────────────────────────────────────────────────────────

def rank_score(apt: dict) -> int:
    bw = bool(apt.get("badewanne"))
    tr = bool(apt.get("terrasse"))
    bk = bool(apt.get("balkon"))
    ebk = bool(apt.get("ebk"))
    table = [
        (True,  True,  False, True,  15),
        (True,  False, True,  True,  14),
        (True,  True,  True,  True,  13),
        (True,  True,  False, False, 12),
        (True,  False, True,  False, 11),
        (True,  True,  True,  False, 10),
        (False, True,  False, True,  9),
        (False, False, True,  True,  8),
        (False, True,  True,  True,  7),
        (False, True,  False, False, 6),
        (False, False, True,  False, 5),
        (False, True,  True,  False, 4),
        (True,  False, False, True,  3),
        (True,  False, False, False, 2),
        (False, False, False, True,  1),
    ]
    for rb, rt, rk, re_, score in table:
        if bw == rb and tr == rt and bk == rk and ebk == re_:
            return score
    return 0


def price_num(apt: dict, info: str) -> int:
    """Best-effort numeric warm/cold rent for sorting. Returns 0 if unknown."""
    for src in (apt.get("warmmiete"), apt.get("kaltmiete"), info):
        if not src:
            continue
        # German number: 1.481,40 € -> 1481
        m = re.search(r"(\d{1,3}(?:\.\d{3})*|\d+)(?:,\d+)?\s*(?:€|EUR|KM|WM)", src)
        if m:
            num = m.group(1).replace(".", "")
            try:
                return int(num)
            except ValueError:
                pass
    return 0


def city_of(apt: dict) -> str:
    loc = apt.get("location", "").lower()
    if "ludwigshafen" in loc or (apt.get("id", "").startswith("LU")):
        return "LU"
    return "MA"


def fmt_ts(ts) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts), TZ).strftime("%d.%m.%Y")


def now_str() -> str:
    """Current wall-clock time in German timezone — used as the page's
    'last refreshed' stamp."""
    return datetime.now(TZ).strftime("%d.%m.%Y, %H:%M")


def rank_badge(score: int) -> str:
    if score >= 10:  icon, cls = "🔝", "top"
    elif score >= 4: icon, cls = "🌳", "mid"
    elif score == 3: icon, cls = "🍳", "ebk"
    elif score == 2: icon, cls = "✅", "bw"
    else:            icon, cls = "📋", "low"
    return f'<span class="rank-badge rank-{cls}">{icon} Rang {16 - score}</span>'


def amenity_tags(apt: dict) -> str:
    tags = []
    if apt.get("badewanne"):   tags.append(("bw",  "🛁", "Badewanne"))
    if apt.get("terrasse"):    tags.append(("tr",  "🪴", "Terrasse"))
    if apt.get("balkon"):      tags.append(("bk",  "🌳", "Balkon"))
    if apt.get("ebk"):         tags.append(("ebk", "🍳", "EBK"))
    if apt.get("baeder"):      tags.append(("bd",  "🚿", apt["baeder"]))
    if apt.get("gaeste_wc"):   tags.append(("gw",  "🚽", "Gäste-WC"))
    if apt.get("heizungsart"): tags.append(("hz",  "🔥", apt["heizungsart"]))
    return "".join(f'<span class="tag {c}">{i} {l}</span>' for c, i, l in tags)


def _has_digit(s: str) -> bool:
    return any(c.isdigit() for c in (s or ""))

_ZERO_SENTINEL = re.compile(r'^\s*0\s*(KM|NK|WM|QM)\s*$', re.IGNORECASE)


def cost_info(apt: dict, info: str) -> tuple[str, list[str]]:
    """Collect all cost components from BOTH the structured fields and the info
    string (whichever is populated), then return (headline, breakdown).

    headline = warm rent, else cold rent, else a bare € price.
    breakdown = the remaining components in the order KM · NK · WM · €/m².
    """
    comp: dict[str, str] = {}

    # 1) structured fields take precedence; skip "0 KM" / "0 WM" etc. sentinels
    for field, label in (("warmmiete", "WM"), ("kaltmiete", "KM"),
                          ("nebenkosten", "NK"), ("qm_preis", "€/m²")):
        v = (apt.get(field) or "").strip()
        if _has_digit(v) and not _ZERO_SENTINEL.match(v):
            comp[label] = v.replace("QM", "€/m²").replace("qm", "€/m²").strip()

    # 2) fill gaps from the info string tokens
    for part in info.split("|"):
        p = part.strip()
        if not _has_digit(p) or _ZERO_SENTINEL.match(p):
            continue
        up = p.upper()
        if "KM" in up:
            comp.setdefault("KM", p)
        elif "NK" in up:
            comp.setdefault("NK", p)
        elif "WM" in up:
            comp.setdefault("WM", p)
        elif "QM" in up or "€/M" in up:
            comp.setdefault("€/m²", p.replace("QM", "€/m²").replace("qm", "€/m²"))
        elif "€" in p:
            comp.setdefault("€", p)

    headline, hl_key = "", ""
    for key in ("WM", "KM", "€"):
        if comp.get(key):
            headline, hl_key = comp[key], key
            break

    breakdown = [comp[k] for k in ("KM", "NK", "WM", "€/m²") if k in comp and k != hl_key]
    return headline, breakdown


def size_line(info: str) -> str:
    """Rooms + area from the info string (the first two non-cost tokens)."""
    parts = []
    for p in info.split("|"):
        p = p.strip()
        if not p or p == "—":
            continue
        if "€" in p or "KM" in p or "WM" in p or "NK" in p or "QM" in p:
            break
        parts.append(p)
        if len(parts) == 2:
            break
    return " · ".join(parts)


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def safe_href(url: str) -> str:
    """Only allow http/https links to be rendered as clickable; everything else
    (e.g. javascript: schemes) is neutralised. Output is HTML-escaped."""
    u = (url or "").strip()
    if u.startswith("http://") or u.startswith("https://"):
        return esc(u)
    return "#"


def state_signature(state: dict, overlay: dict) -> str:
    listings = [(u, d) for u, d in state.items() if not u.startswith("__")]
    last = max((d.get("last_seen", 0) for _, d in listings), default=0)
    ov = OVERLAY_FILE.stat().st_mtime if OVERLAY_FILE.exists() else 0
    return f"{len(listings)}-{last}-{int(ov)}"


# ── Rendering ───────────────────────────────────────────────────────────────

def render_card(apt_id: str, apt: dict, url: str, ov: dict, is_new: bool = False, gone: bool = False) -> str:
    is_fav = bool(ov.get("favorite"))
    is_hidden = bool(ov.get("hidden"))
    note = ov.get("note") or ""
    info = apt.get("info", "")
    headline, breakdown = cost_info(apt, info)
    pnum = price_num(apt, info)
    score = rank_score(apt)
    city = city_of(apt)
    size = size_line(info)

    classes = "card"
    if is_fav: classes += " fav"
    if is_hidden: classes += " hidden-card"
    if gone: classes += " gone-card"
    star_class = " active" if is_fav else ""
    new_badge = '<span class="new-badge">NEU</span> ' if is_new else ""
    gone_badge = '<span class="gone-badge">nicht mehr inseriert</span> ' if gone else ""
    amenities = amenity_tags(apt)
    badge = rank_badge(score)

    fav_sub = (f'<div class="fav-date">⭐ Favorit seit {fmt_ts(ov.get("date_favorited"))}</div>'
               if is_fav and ov.get("date_favorited") else "")
    note_html = (f'<div class="note" id="note-{apt_id}">📝 {esc(note)}</div>'
                 if note else f'<div class="note empty" id="note-{apt_id}"></div>')

    flags = f'data-url="{esc(url)}" data-city="{city}" data-price="{pnum}" data-score="{score}" ' \
            f'data-seen="{int(apt.get("first_seen", 0))}" ' \
            f'data-new="{int(is_new)}" data-fav="{int(is_fav)}" data-hidden="{int(is_hidden)}" ' \
            f'data-bw="{int(bool(apt.get("badewanne")))}" data-tr="{int(bool(apt.get("terrasse")))}" ' \
            f'data-bk="{int(bool(apt.get("balkon")))}" data-ebk="{int(bool(apt.get("ebk")))}"'

    return f'''\
<div class="{classes}" id="card-{apt_id}" {flags}>
  <div class="card-top">
    <div class="card-meta">
      <span class="apt-id">{apt_id}</span>
      {badge}
      {new_badge}{gone_badge}<span class="location">{esc(apt.get("location", ""))}</span>
    </div>
    <div class="card-actions">
      <button class="icon-btn star-btn{star_class}" onclick="toggleFav(event,'{apt_id}')" title="Favorit">⭐</button>
      <button class="icon-btn note-btn" onclick="editNote(event,'{apt_id}')" title="Notiz">📝</button>
      <button class="icon-btn hide-btn" onclick="toggleHide(event,'{apt_id}')" title="{'Wieder einblenden' if is_hidden else 'Ausblenden'}">{'👁️' if is_hidden else '🙈'}</button>
    </div>
  </div>
  <div class="card-title">{esc(apt.get("title", ""))}</div>
  {f'<div class="size">{esc(size)}</div>' if size else ""}
  {f'<div class="price">💶 {esc(headline)}</div>' if headline else ""}
  {f'<div class="cost-detail">{esc(" · ".join(breakdown))}</div>' if breakdown else ""}
  {f'<div class="amenities">{amenities}</div>' if amenities else ""}
  {fav_sub}{note_html}
  <div class="card-footer">
    <span class="seen-date">Gefunden {fmt_ts(apt.get("first_seen"))} · Gesehen {apt.get("report_count", 1)}× · Zuletzt {fmt_ts(apt.get("last_seen"))}</span>
    <a href="{safe_href(url)}" target="_blank" rel="noopener noreferrer" class="link-btn">🔗 Anzeige</a>
  </div>
</div>'''


def section(sid, title, items, overlay, is_new=False, collapsed=False, gone=False):
    caret = "▸" if collapsed else "▾"
    display = ' style="display:none"' if collapsed else ""
    cards = "".join(render_card(d.get("id", "?"), d, u, overlay.get(u, {}), is_new, gone)
                    for u, d in items)
    return f'''\
<section class="section" data-section="{sid}">
  <h2 class="section-title" onclick="toggleSection('{sid}')">
    {title} <span class="count-badge" id="count-{sid}">{len(items)}</span>
    <span class="caret" id="caret-{sid}">{caret}</span>
  </h2>
  <div class="card-grid" id="grid-{sid}"{display}>{cards}</div>
</section>'''


def render_page(state: dict, overlay: dict, token: str) -> str:
    now = time.time()
    week = 7 * 24 * 3600

    listings = [(url, d) for url, d in state.items() if not url.startswith("__")]

    def is_hidden(u):
        return bool(overlay.get(u, {}).get("hidden"))

    def is_fav(u):
        return bool(overlay.get(u, {}).get("favorite"))

    state_urls = {u for u, _ in listings}

    favs    = [(u, d) for u, d in listings if is_fav(u) and not is_hidden(u)]
    new_lst = [(u, d) for u, d in listings if not is_fav(u) and not is_hidden(u)
               and (now - d.get("first_seen", 0)) < week]
    old_lst = [(u, d) for u, d in listings if not is_fav(u) and not is_hidden(u)
               and (now - d.get("first_seen", 0)) >= week]
    hidden  = [(u, d) for u, d in listings if is_hidden(u)]

    # Favorites whose listing was pruned from the Hermes state — rebuilt from
    # the snapshot captured at favorite time.
    gone_favs = [(u, ov["snapshot"]) for u, ov in overlay.items()
                 if ov.get("favorite") and not ov.get("hidden")
                 and u not in state_urls and ov.get("snapshot")]

    def sort_apt(items):
        # Newest discovered first; rank as tiebreaker for same timestamp.
        return sorted(items, key=lambda x: (-x[1].get("first_seen", 0), -rank_score(x[1])))

    favs.sort(key=lambda x: -overlay.get(x[0], {}).get("date_favorited", 0))
    new_lst = sort_apt(new_lst)
    old_lst = sort_apt(old_lst)
    hidden  = sort_apt(hidden)
    gone_favs.sort(key=lambda x: -overlay.get(x[0], {}).get("date_favorited", 0))

    fav_sec    = section("fav", "⭐ Favoriten", favs, overlay) if favs else ""
    gone_sec   = section("gone", "💔 Verschwundene Favoriten", gone_favs, overlay, gone=True) if gone_favs else ""
    new_sec    = section("new", "🆕 Neu (letzte 7 Tage)", new_lst, overlay, is_new=True) if new_lst else ""
    old_sec    = section("all", "📋 Alle Wohnungen", old_lst, overlay,
                         collapsed=bool(favs or new_lst)) if old_lst else ""
    hidden_sec = section("hidden", "🙈 Ausgeblendet", hidden, overlay, collapsed=True) if hidden else ""

    updated = fmt_ts(max((d.get("last_seen", 0) for _, d in listings), default=0))
    refreshed = now_str()
    counters = state.get("__counters__", {})
    ma_count = counters.get("MA", 0)
    lu_count = counters.get("LU", 0)
    sig = state_signature(state, overlay)

    return f'''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<meta name="theme-color" content="#0d1117">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Wohnungen">
<title>🏠 Wohnungssuche</title>
<link rel="manifest" href="/{token}/manifest.json">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#0d1117;--surface:#161b22;--border:#30363d;
  --text:#c9d1d9;--muted:#8b949e;--accent:#58a6ff;
  --gold:#f0c040;--green:#3fb950;--red:#f85149;
  --fav-bg:#1c1a10;--new:#1a3a1a
}}
html{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;overflow-x:hidden}}
body{{padding:0 0 env(safe-area-inset-bottom) 0;min-height:100vh;max-width:100%;overflow-x:hidden}}
.header{{background:var(--surface);border-bottom:1px solid var(--border);padding:14px 14px 10px;position:sticky;top:0;z-index:100}}
.header-row{{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}}
.header h1{{font-size:19px;color:var(--accent);margin-bottom:3px}}
.header-sub{{font-size:12px;color:var(--muted)}}
.refresh-btn{{background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);
  font-size:16px;padding:6px 10px;cursor:pointer;flex-shrink:0}}
.refresh-btn:active{{transform:scale(.94)}}
.stats-row{{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}}
.stat{{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:3px 9px;font-size:12px;color:var(--muted)}}
.stat b{{color:var(--text)}}
.controls{{display:flex;gap:6px;margin-top:10px;flex-wrap:wrap;align-items:center}}
.seg{{display:inline-flex;border:1px solid var(--border);border-radius:8px;overflow:hidden}}
.seg button{{background:var(--bg);border:none;color:var(--muted);font-size:12px;padding:5px 10px;cursor:pointer}}
.seg button.on{{background:var(--accent);color:#fff}}
.ctrl-select{{background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);
  font-size:12px;padding:5px 8px}}
.chip{{background:var(--bg);border:1px solid var(--border);border-radius:14px;color:var(--muted);
  font-size:12px;padding:4px 9px;cursor:pointer}}
.chip.on{{border-color:var(--gold);background:var(--fav-bg);color:var(--gold)}}
.content{{padding:12px 12px 24px}}
.section{{margin-bottom:20px}}
.section-title{{
  font-size:16px;font-weight:600;padding:10px 0 8px;
  border-bottom:2px solid var(--border);margin-bottom:10px;
  display:flex;align-items:center;gap:8px;cursor:pointer;user-select:none
}}
.section-title:hover{{color:var(--accent)}}
.caret{{margin-left:auto;font-size:14px;color:var(--muted)}}
.count-badge{{background:var(--green);color:#fff;border-radius:10px;padding:1px 8px;font-size:12px;font-weight:500}}
.card-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(320px,100%),1fr));gap:10px}}
.card{{
  background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:14px;transition:border-color .2s,opacity .2s;
  min-width:0;max-width:100%;overflow:hidden
}}
.card:hover{{border-color:var(--accent)}}
.card.fav{{border-color:var(--gold);background:var(--fav-bg)}}
.card.hidden-card{{opacity:.6}}
.card.gone-card{{opacity:.7;border-style:dashed}}
.gone-badge{{background:#3a1a1a;color:#e87e7e;font-size:10px;padding:1px 5px;border-radius:4px;white-space:nowrap}}
.card.filtered{{display:none}}
.card-top{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;gap:6px;min-width:0}}
.card-meta{{display:flex;align-items:center;gap:6px;flex-wrap:wrap;flex:1;min-width:0}}
.card-actions{{display:flex;gap:4px;flex-shrink:0}}
.apt-id{{color:var(--gold);font-size:13px;font-weight:600;white-space:nowrap}}
.location{{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.new-badge{{background:var(--new);color:#7ec87e;font-size:10px;padding:1px 5px;border-radius:4px;white-space:nowrap}}
.icon-btn{{
  background:none;border:1px solid var(--border);border-radius:6px;
  font-size:15px;cursor:pointer;padding:3px 6px;transition:all .15s;
  color:var(--muted);line-height:1
}}
.icon-btn:active{{transform:scale(.9)}}
.star-btn.active{{border-color:var(--gold);background:var(--fav-bg)}}
.icon-btn.loading{{opacity:.4;pointer-events:none}}
.card-footer{{display:flex;justify-content:space-between;align-items:center;margin-top:8px}}
.card-title{{font-size:15px;font-weight:600;color:#f0f6fc;margin-bottom:5px;line-height:1.3;overflow-wrap:anywhere}}
.size{{font-size:13px;color:var(--muted);margin-bottom:4px}}
.price{{font-size:15px;font-weight:600;color:var(--green);margin-bottom:3px}}
.cost-detail{{font-size:12px;color:var(--muted);margin-bottom:6px;overflow-wrap:anywhere}}
.amenities{{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px;min-width:0}}
.tag{{font-size:11px;padding:2px 6px;border-radius:4px;white-space:nowrap}}
.tag.bw{{background:#1a3a4a;color:#7ec8e3}}
.tag.tr{{background:#3a2a1a;color:#e3c87e}}
.tag.bk{{background:#1a3a1a;color:#7ec87e}}
.tag.ebk{{background:#3a1a2a;color:#e37ec8}}
.tag.bd{{background:#1a2a3a;color:#7ec8c8}}
.tag.gw{{background:#2a1a3a;color:#c87ee3}}
.tag.hz{{background:#2a1a1a;color:#e87e7e}}
.fav-date{{font-size:12px;color:var(--gold);margin-bottom:4px}}
.note{{font-size:12px;color:var(--gold);margin-bottom:4px;overflow-wrap:anywhere;cursor:pointer}}
.note.empty{{display:none}}
.seen-date{{font-size:11px;color:var(--muted)}}
.rank-badge{{font-size:11px;padding:1px 6px;border-radius:4px;white-space:nowrap}}
.rank-top{{background:#1a2a1a;color:#3fb950}}
.rank-mid{{background:#1a3a1a;color:#7ec87e}}
.rank-ebk{{background:#3a1a2a;color:#e37ec8}}
.rank-bw{{background:#1a3a4a;color:#58a6ff}}
.rank-low{{background:#222;color:#8b949e}}
.link-btn{{
  font-size:12px;color:var(--accent);text-decoration:none;
  border:1px solid var(--border);border-radius:6px;padding:4px 10px
}}
.toast{{
  position:fixed;bottom:calc(20px + env(safe-area-inset-bottom));left:50%;transform:translateX(-50%);
  background:#1f2937;border:1px solid var(--border);border-radius:8px;
  padding:8px 16px;font-size:13px;z-index:200;opacity:0;transition:opacity .3s;
  pointer-events:none;white-space:nowrap
}}
.toast.show{{opacity:1}}
.update-banner{{
  position:fixed;top:0;left:0;right:0;background:var(--accent);color:#fff;
  text-align:center;padding:10px;font-size:14px;z-index:300;cursor:pointer;
  transform:translateY(-100%);transition:transform .3s
}}
.update-banner.show{{transform:translateY(0)}}
.empty-msg{{color:var(--muted);font-size:13px;padding:20px;text-align:center;display:none}}
@media(max-width:480px){{
  .card-grid{{grid-template-columns:1fr}}
  .content{{padding:10px 10px 24px}}
}}
</style>
</head>
<body>
<div class="update-banner" id="updateBanner" onclick="location.reload()">🔄 Neue Daten verfügbar – tippen zum Aktualisieren</div>
<div class="header">
  <div class="header-row">
    <div>
      <h1>🏠 Wohnungssuche MA/LU</h1>
      <div class="header-sub">🔄 Aktualisiert: {refreshed} Uhr</div>
      <div class="header-sub">Daten-Stand: {updated}</div>
    </div>
    <button class="refresh-btn" onclick="location.reload()" title="Aktualisieren">🔄</button>
  </div>
  <div class="stats-row">
    <div class="stat">Gesamt <b>{len(listings)}</b></div>
    <div class="stat">⭐ <b>{len(favs)}</b></div>
    <div class="stat">🆕 <b>{len(new_lst)}</b></div>
    <div class="stat">MA <b>{ma_count}</b></div>
    <div class="stat">LU <b>{lu_count}</b></div>
  </div>
  <div class="controls">
    <div class="seg" id="citySeg">
      <button class="on" data-city="ALL" onclick="setCity(this)">Alle</button>
      <button data-city="MA" onclick="setCity(this)">MA</button>
      <button data-city="LU" onclick="setCity(this)">LU</button>
    </div>
    <select class="ctrl-select" id="sortSel" onchange="applyFilters()">
      <option value="new">Neueste zuerst</option>
      <option value="rank">Beste Ausstattung</option>
      <option value="price_asc">Günstigste</option>
      <option value="price_desc">Teuerste</option>
    </select>
    <span class="chip" data-am="bw" onclick="toggleChip(this)">🛁</span>
    <span class="chip" data-am="tr" onclick="toggleChip(this)">🪴</span>
    <span class="chip" data-am="bk" onclick="toggleChip(this)">🌳</span>
    <span class="chip" data-am="ebk" onclick="toggleChip(this)">🍳</span>
  </div>
</div>
<div class="content">
{fav_sec}
{gone_sec}
{new_sec}
{old_sec}
{hidden_sec}
</div>
<div class="toast" id="toast"></div>
<script>
const TOKEN = {json.dumps(token)};
const SIG = {json.dumps(sig)};
let CITY = 'ALL';
const AMEN = new Set();

function showToast(msg, ok=true) {{
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.borderColor = ok ? '#3fb950' : '#f85149';
  t.classList.add('show');
  clearTimeout(t._t);
  t._t = setTimeout(() => t.classList.remove('show'), 2000);
}}

async function post(action, aptId, extra) {{
  const card = document.getElementById('card-' + aptId);
  const body = Object.assign({{url: card ? card.dataset.url : ''}}, extra || {{}});
  const res = await fetch(`/${{TOKEN}}/api/${{action}}/${{encodeURIComponent(aptId)}}`, {{
    method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(body)
  }});
  return res.json();
}}

async function toggleFav(evt, aptId) {{
  evt.stopPropagation();
  const btn = evt.currentTarget;
  const card = document.getElementById('card-' + aptId);
  const isFav = btn.classList.contains('active');
  btn.classList.add('loading');
  try {{
    const data = await post(isFav ? 'unfavorite' : 'favorite', aptId);
    if (data.ok) {{
      btn.classList.toggle('active');
      card.classList.toggle('fav');
      card.dataset.fav = isFav ? '0' : '1';
      showToast(isFav ? '💔 Favorit entfernt' : '⭐ Als Favorit gespeichert');
      setTimeout(() => location.reload(), 600);
    }} else showToast('❌ ' + (data.error||'Fehler'), false);
  }} catch(e) {{ showToast('❌ Verbindungsfehler', false); }}
  finally {{ btn.classList.remove('loading'); }}
}}

async function toggleHide(evt, aptId) {{
  evt.stopPropagation();
  const btn = evt.currentTarget;
  const card = document.getElementById('card-' + aptId);
  const isHidden = card.dataset.hidden === '1';
  btn.classList.add('loading');
  try {{
    const data = await post(isHidden ? 'unhide' : 'hide', aptId);
    if (data.ok) {{
      showToast(isHidden ? '👁️ Wieder eingeblendet' : '🙈 Ausgeblendet');
      setTimeout(() => location.reload(), 600);
    }} else showToast('❌ ' + (data.error||'Fehler'), false);
  }} catch(e) {{ showToast('❌ Verbindungsfehler', false); }}
  finally {{ btn.classList.remove('loading'); }}
}}

async function editNote(evt, aptId) {{
  evt.stopPropagation();
  const noteEl = document.getElementById('note-' + aptId);
  const current = noteEl.classList.contains('empty') ? '' : noteEl.textContent.replace(/^📝\\s*/, '');
  const val = window.prompt('Notiz für ' + aptId + ':', current);
  if (val === null) return;
  try {{
    const data = await post('note', aptId, {{note: val}});
    if (data.ok) {{
      if (val.trim()) {{
        noteEl.textContent = '📝 ' + val;
        noteEl.classList.remove('empty');
      }} else {{
        noteEl.textContent = '';
        noteEl.classList.add('empty');
      }}
      showToast('📝 Notiz gespeichert');
    }} else showToast('❌ ' + (data.error||'Fehler'), false);
  }} catch(e) {{ showToast('❌ Verbindungsfehler', false); }}
}}

function setCity(btn) {{
  CITY = btn.dataset.city;
  document.querySelectorAll('#citySeg button').forEach(b => b.classList.toggle('on', b===btn));
  applyFilters();
}}
function toggleChip(el) {{
  const a = el.dataset.am;
  if (AMEN.has(a)) {{ AMEN.delete(a); el.classList.remove('on'); }}
  else {{ AMEN.add(a); el.classList.add('on'); }}
  applyFilters();
}}

function applyFilters() {{
  const sort = document.getElementById('sortSel').value;
  document.querySelectorAll('.section').forEach(sec => {{
    const grid = sec.querySelector('.card-grid');
    const cards = Array.from(grid.querySelectorAll('.card'));
    let visible = 0;
    cards.forEach(c => {{
      let ok = true;
      if (CITY !== 'ALL' && c.dataset.city !== CITY) ok = false;
      for (const a of AMEN) if (c.dataset[a] !== '1') ok = false;
      c.classList.toggle('filtered', !ok);
      if (ok) visible++;
    }});
    // sort visible cards
    const sorted = cards.slice().sort((a,b) => {{
      if (sort === 'price_asc') return (+a.dataset.price||1e9) - (+b.dataset.price||1e9);
      if (sort === 'price_desc') return (+b.dataset.price||0) - (+a.dataset.price||0);
      if (sort === 'rank') return (+b.dataset.score) - (+a.dataset.score);
      return (+b.dataset.seen||0) - (+a.dataset.seen||0); // new (default): newest discovered first
    }});
    sorted.forEach(c => grid.appendChild(c));
    const badge = document.getElementById('count-' + sec.dataset.section);
    if (badge) badge.textContent = visible;
  }});
}}

function toggleSection(sid) {{
  const grid = document.getElementById('grid-' + sid);
  const caret = document.getElementById('caret-' + sid);
  const hidden = grid.style.display === 'none';
  grid.style.display = hidden ? '' : 'none';
  if (caret) caret.textContent = hidden ? '▾' : '▸';
}}

// Auto-refresh poll: check signature every 60s, show banner if changed
setInterval(async () => {{
  try {{
    const res = await fetch(`/${{TOKEN}}/api/version`);
    const data = await res.json();
    if (data.sig && data.sig !== SIG) {{
      document.getElementById('updateBanner').classList.add('show');
    }}
  }} catch(e) {{}}
}}, 60000);
</script>
</body>
</html>'''


# ── Routes ─────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    raise HTTPException(status_code=404)


@app.get("/{token}/manifest.json")
async def manifest(token: str):
    check_token(token)
    data = {
        "name": "Wohnungssuche MA/LU",
        "short_name": "Wohnungen",
        "start_url": f"/{token}/",
        "display": "standalone",
        "background_color": "#0d1117",
        "theme_color": "#0d1117",
        "icons": [{"src": "https://emojicdn.elk.sh/🏠?style=apple", "sizes": "192x192", "type": "image/png"}],
    }
    return Response(json.dumps(data), media_type="application/manifest+json")


@app.get("/{token}/")
async def index(token: str):
    check_token(token)
    state = load_state()
    overlay = load_overlay()
    return HTMLResponse(render_page(state, overlay, token))


@app.get("/{token}/api/version")
async def version(token: str):
    check_token(token)
    state = await asyncio.to_thread(load_state)
    overlay = load_overlay()
    return JSONResponse({"sig": state_signature(state, overlay)})


async def _read_body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


async def _resolve_key(apt_id: str, body: dict) -> str:
    """Resolve the overlay key. Prefer an explicit URL from the request body
    (works even for pruned/gone listings); else map the ID via the state.

    A body URL is only accepted if it is already known — present in the Hermes
    state or already tracked in our overlay. This blocks an attacker (who has
    the link) from planting arbitrary URLs/hrefs or bloating the overlay file.
    """
    url = (body.get("url") or "").strip()
    if url.startswith("http://") or url.startswith("https://"):
        url = url.rstrip("/")
        if url in load_overlay():
            return url
        state = await asyncio.to_thread(load_state)
        if url in state:
            return url
        raise HTTPException(status_code=403, detail="unknown url")
    key = await asyncio.to_thread(resolve_url, apt_id)
    if not key:
        raise HTTPException(status_code=404, detail="unknown listing")
    return key


@app.post("/{token}/api/favorite/{apt_id}")
async def do_favorite(token: str, apt_id: str, request: Request):
    check_token(token)
    body = await _read_body(request)
    key = await _resolve_key(apt_id, body)
    overlay = load_overlay()
    e = overlay_entry(overlay, key)
    e["favorite"] = True
    e["date_favorited"] = int(time.time())
    # Snapshot the listing so the favorite survives a future Hermes prune.
    state = await asyncio.to_thread(load_state)
    listing = state.get(key)
    if isinstance(listing, dict):
        e["snapshot"] = snapshot_of(listing)
    save_overlay(overlay)
    return JSONResponse({"ok": True})


@app.post("/{token}/api/unfavorite/{apt_id}")
async def do_unfavorite(token: str, apt_id: str, request: Request):
    check_token(token)
    body = await _read_body(request)
    key = await _resolve_key(apt_id, body)
    overlay = load_overlay()
    e = overlay_entry(overlay, key)
    e.pop("favorite", None)
    e.pop("date_favorited", None)
    e.pop("snapshot", None)
    if not e:
        overlay.pop(key, None)
    save_overlay(overlay)
    return JSONResponse({"ok": True})


@app.post("/{token}/api/hide/{apt_id}")
async def do_hide(token: str, apt_id: str, request: Request):
    check_token(token)
    body = await _read_body(request)
    key = await _resolve_key(apt_id, body)
    overlay = load_overlay()
    e = overlay_entry(overlay, key)
    e["hidden"] = True
    e["date_hidden"] = int(time.time())
    save_overlay(overlay)
    return JSONResponse({"ok": True})


@app.post("/{token}/api/unhide/{apt_id}")
async def do_unhide(token: str, apt_id: str, request: Request):
    check_token(token)
    body = await _read_body(request)
    key = await _resolve_key(apt_id, body)
    overlay = load_overlay()
    e = overlay_entry(overlay, key)
    e.pop("hidden", None)
    e.pop("date_hidden", None)
    if not e:
        overlay.pop(key, None)
    save_overlay(overlay)
    return JSONResponse({"ok": True})


@app.post("/{token}/api/note/{apt_id}")
async def do_note(token: str, apt_id: str, request: Request):
    check_token(token)
    body = await _read_body(request)
    note = (body.get("note") or "").strip()
    key = await _resolve_key(apt_id, body)
    overlay = load_overlay()
    e = overlay_entry(overlay, key)
    if note:
        e["note"] = note
    else:
        e.pop("note", None)
        if not e:
            overlay.pop(key, None)
    save_overlay(overlay)
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)

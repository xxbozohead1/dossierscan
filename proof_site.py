#!/usr/bin/env python3
"""
proof_site: Dossier, the public site, built as static files from the index (proof_index.export_data).

    python proof_site.py                          # build into data/site
    python proof_site.py --out DIR
    python proof_site.py --single FILE            # also the whole site as one page (a preview host)

The indexer rebuilds it every few minutes while it runs (proof_index.maybe_build_site). Every link is
relative, so the folder can be served from any host or path. Stdlib only, like the rest of the project.
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import hashlib
import html
import json
import math
import os
import re
import shutil
import time
import urllib.parse
from pathlib import Path

import orbio_watch as w

ROOT = Path(__file__).resolve().parent
OUT = w.DATA / "site"
SPEC = ROOT / "docs" / "scoring-spec.md"
NAME = "Dossier"
DAY = 86400
CHECKING_FOR = 3 * DAY  # the index re-checks an unclaimed Orbio agent for 72 hours (proof_index.VAULT_RETRIES)

DIMS = (("product", "Product", 30), ("build", "Build", 20), ("team", "Team", 20), ("work", "Work", 20),
        ("integrity", "Integrity", 10))
STATE = {"verified": "Verified", "scam": "Impersonator", "linked": "Impersonator's wallet", "checking": "Checking",
         "unverified": "Unverified"}
RED = ("scam", "linked")  # both red; "linked" is red for its money (proof_index.link_pass), not for a project's word
STATE_TEXT = {
    "verified": "The project's own X account or website lists this exact contract.",
    "scam": "An official channel lists a different contract, or says this token isn't theirs.",
    "linked": "Launched from a wallet, or a chain of wallets, that also launched a confirmed copy of a real project. "
              "Its own project hasn't claimed it.",
    "checking": "Nothing official lists this contract yet. Dossier keeps checking for 72 hours after launch; "
                "the official post often lands a few minutes after the token.",
    "unverified": "Nothing official lists this contract. That isn't proof of a scam, but no one has claimed it.",
}
LEVEL_TEXT = {
    "bound": "claims this token: it posted or lists this contract",
    "bound·transitive": "vouched for by an identity that claims the token",
    "named": "names the ticker or project, not this contract",
    "unbound": "doesn't mention this token",
    "unvouched": "lists this contract, but the project's X account never linked this site",
    "contradicted": "claims a different contract",
    "disavowed": "says this token isn't theirs",
}
LINES = {
    "site_up": "Site responds", "substantive": "Site has real content", "surface": "A working page beyond the homepage",
    "changed_14d": "Site changed in the last 14 days", "repo": "Public repo tied to the project",
    "commits": "Recent commits", "predates_launch": "Repo predates the launch", "contributors": "2+ active contributors",
    "x_account": "X account claims the token", "x_age": "X account age at launch", "x_cadence": "Posts most weeks",
    "creator_history": "Creator wallet has history", "output": "Verified product output (7 days)",
    "contract_users": "Outside users of its contracts", "agent_active": "Agent wallet active",
    "credit_activated": "CREDIT activated", "top10": "Holders not concentrated", "creator_stayed": "Creator didn't dump",
    "fees_kept": "Creator fees not redirected",
}
FLAGS = {"CREATOR_EXIT": "Creator sold most of their launch buy", "FEE_REDIRECT": "Creator fees sent to a fresh wallet",
         "LAUNCH_BUNDLE": "Several buyers in the launch block", "SERIAL": "Creator launched 3+ tokens in a day",
         "CONFLICT": "Official channels disagree", "BORROWED": "Points at another organisation's brand"}
FONTS = ("https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wdth,wght@12..96,75..100,500..800"
         "&family=IBM+Plex+Mono:wght@400;500;600&family=Instrument+Sans:wght@400..700&display=swap")
EXPLORER = "https://robin.etherscan.io"


# ----------------------------------------------------------------- small helpers

def e(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def short(a: str) -> str:
    return f"{a[:6]}…{a[-4:]}" if a and len(a) > 12 else (a or "")


def when(ts: int | None, fallback: str = "—") -> str:
    """A <time> the page's script turns into "3 h ago" / "in 9 days"; the absolute UTC time stays in the tooltip."""
    if not ts:
        return fallback
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return f'<time datetime="{d:%Y-%m-%dT%H:%M:%SZ}" data-ts="{ts}" title="{d:%d %b %Y %H:%M UTC}">{d:%d %b %H:%M} UTC</time>'


def link(url: str, text: str | None = None, cls: str = "") -> str:
    if not re.match(r"https?://", url or ""):
        return e(text or url)
    c = f' class="{cls}"' if cls else ""
    return f'<a{c} href="{e(url)}" rel="nofollow noopener" target="_blank">{e(text or url.split("://", 1)[1].rstrip("/"))}</a>'


def receipt_text(url: str) -> str:
    """A receipt's link text: "@handle's post on X" for a post, else the address, shortened."""
    m = re.match(r"https?://(?:www\.)?(?:x|twitter)\.com/(\w+)/status/\d+", url or "")
    if m:
        return f"@{m.group(1)}'s post on X"
    m = re.fullmatch(r"https?://(?:www\.)?(?:x|twitter)\.com/(\w+)/?", url or "")
    if m:  # an account that shows the contract in its bio: the profile is the receipt
        return f"@{m.group(1)}'s bio on X"
    bare = re.sub(r"^https?://(www\.)?", "", url or "").rstrip("/")
    return bare if len(bare) <= 40 else bare[:38] + "…"


def sentence(s: str) -> str:
    """The verdict reasons are lowercase clauses (Telegram alerts embed them); a page shows them as sentences."""
    s = (s or "").strip()
    return s[:1].upper() + s[1:] + ("" if s.endswith((".", "…", "”")) else ".")


def plural(n, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def usd(x) -> str:
    if x is None:
        return "—"
    if x >= 1e6:
        return f"${x / 1e6:.2f}M"
    if x >= 1e3:
        return f"${x / 1e3:.1f}k"
    return f"${x:,.0f}" if x >= 10 else f"${x:,.2f}"


SUBSCRIPT = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")


def price(x) -> str:
    """Tiny prices the way trading screens show them: $0.0₅755 is 0.00000755 (five zeros after the point)."""
    if not x:
        return "—"
    if x >= 1:
        return f"${x:,.2f}"
    if x >= 0.001:
        return f"${x:.4f}"
    digits = f"{x:.14f}".split(".")[1]
    zeros = len(digits) - len(digits.lstrip("0"))
    return f"$0.0{str(zeros).translate(SUBSCRIPT)}{digits[zeros:zeros + 3]}"


def num(x, unit: str = "") -> str:
    if x is None:
        return "—"
    s = f"{x / 1e6:.2f}M" if x >= 1e6 else f"{x / 1e3:.1f}k" if x >= 1e4 else f"{x:,.0f}" if x >= 100 else f"{x:,.2f}".rstrip("0").rstrip(".")
    return s + (f" {unit}" if unit else "")


def state_of(t: dict, now: int) -> str:
    v = t["verdict"]["verdict"]
    if v == "scam" and t["verdict"].get("kind") == "linked":
        return "linked"
    if v == "unverified" and t.get("orbio_agent") and now - (t.get("launched_at") or 0) < CHECKING_FOR:
        return "checking"
    return v


def group_of(s: str) -> str:
    """The filter a state falls under: an impersonator's wallet with the impersonators, unverified with checking."""
    return "scam" if s == "linked" else "checking" if s == "unverified" else s


def chip(s: str) -> str:
    return f'<span class="chip v-{s}"><i></i>{STATE[s]}</span>'


def status_chip(s: str) -> str:
    return f'<span class="status s-{e((s or "unproven").lower())}">{e((s or "UNPROVEN").title())}</span>'


def holders_now(t: dict) -> tuple:
    h = t.get("holders") or {}
    for k in ("7d", "24h", "6h", "30m"):
        if h.get(k) is not None:
            return h[k], k
    return None, None


def curve(t: dict) -> str:
    m = t.get("market") or {}
    if m.get("graduated") or t.get("graduated"):
        return '<span class="grad">Graduated</span>'
    if m.get("curve_pct") is None:
        return "—"
    pct = m["curve_pct"]
    return f'<span class="meter" title="{pct:g}% of the way to graduation"><span style="width:{max(2, min(100, pct)):.1f}%"></span></span>{pct:.1f}%'


def fileno(t: dict) -> str:
    return f'File {t["orbio_agent"]}' if t.get("orbio_agent") else "Pons launch"


ON_FILE: set[str] = set()  # tokens with a page in this build; set by build() and build_single()


def tref(t: dict, up: str = "", cls: str = "tok") -> str:
    """A link to the token's file, or to the explorer for a token the index has no file on (a contract a project
    posted that the index never read)."""
    agent = f' <span class="no">#{t["orbio_agent"]}</span>' if t.get("orbio_agent") else ""
    inner = f'<b>${e(t["symbol"] or short(t["token"]))}</b>{agent}' + (f' <span class="nm">{e(t["name"])}</span>' if t.get("name") else "")
    if ON_FILE and t["token"] not in ON_FILE:
        return f'<a class="{cls}" href="{EXPLORER}/token/{e(t["token"])}" rel="nofollow noopener" target="_blank">{inner}</a>'
    return f'<a class="{cls}" href="{up}t/{t["token"]}.html">{inner}</a>'


def rank(t: dict) -> tuple:
    return ({"PROVEN": 0, "LIVE": 1, "BUILDING": 2}.get(t["status"], 3), -((t.get("scores") or {}).get("composite") or 0))


def search_key(t: dict) -> str:
    return f'{t["symbol"] or ""} {t["name"] or ""} {t["token"]}'.lower()


def bars(scores: dict | None, labels: bool = False) -> str:
    if not scores:
        return '<span class="muted">not scored</span>'
    out = []
    for key, label, mx in DIMS:
        v = scores.get(key) or 0
        pct = max(0, min(100, 100 * v / mx))
        out.append(f'<div class="dim" title="{label} {v:g} of {mx}">'
                   + (f'<span class="dl">{label}</span>' if labels else "")
                   + f'<span class="bar"><span style="width:{pct:.0f}%"></span></span>'
                   + (f'<span class="dv">{v:g}<small>/{mx}</small></span>' if labels else "") + "</div>")
    return f'<div class="dims{" full" if labels else ""}">' + "".join(out) + "</div>"


# ----------------------------------------------------------------- page chrome

# the $DOSSIER seal (static/logo.html) drawn for small sizes: the lettered ring becomes a dashed one
MARK = ('<svg class="mark" viewBox="0 0 24 24" aria-hidden="true"><g transform="rotate(-14 12 12)" fill="none" stroke="currentColor">'
        '<circle cx="12" cy="12" r="10.3" stroke-width="1.8"/><circle cx="12" cy="12" r="8.1" stroke-width="1.5" '
        'stroke-dasharray="1.5 1.3" opacity=".7"/><circle cx="12" cy="12" r="5.9" stroke-width="1"/>'
        '<path d="M9.5 12.1l1.8 1.8 3.4-3.7" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></g></svg>')
FAVICON = "data:image/svg+xml," + urllib.parse.quote(
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><circle cx='16' cy='16' r='16' fill='#0d1522'/>"
    "<g transform='rotate(-14 16 16)' fill='none' stroke='#39d08c'><circle cx='16' cy='16' r='12.4' stroke-width='2.6'/>"
    "<circle cx='16' cy='16' r='7.4' stroke-width='1.4'/><path d='M12.7 16.2l2.4 2.4 4.5-4.9' stroke-width='2.7' "
    "stroke-linecap='round' stroke-linejoin='round'/></g></svg>", safe=":/=' ")
LOGO = ROOT / "static" / "logo.png"  # the full seal, 512x512: the $DOSSIER page and the home-screen icon
NAV = (("index.html#live", "New launches"), ("index.html#board", "Board"), ("scams.html", "Impersonators"),
       ("token.html", "$DOSSIER"), ("method.html", "Method"), ("api.html", "API"))


# dark is the default; the switch keeps a viewer's choice of light in this browser. THEME_BOOT sits in <head> and applies
# it before the page paints, so a light-mode reader never sees a flash of dark
THEME_BOOT = ('<script>try{if(localStorage.getItem("dossier.theme")==="light")'
              'document.documentElement.setAttribute("data-theme","light")}catch(e){}</script>')
THEME_SWITCH = ('<button type="button" class="theme" role="switch" aria-checked="false" aria-label="Light mode" title="Light mode">'
                '<svg class="moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" '
                'fill="currentColor"/></svg><svg class="sun" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4.5" '
                'fill="currentColor"/><path d="M12 1.5v3M12 19.5v3M1.5 12h3M19.5 12h3M4.6 4.6l2.1 2.1M17.3 17.3l2.1 2.1M4.6 19.4l2.1-2.1'
                'M17.3 6.7l2.1-2.1" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg><span class="knob"></span></button>')


def header(up: str = "") -> str:
    items = list(NAV)
    if CASES:  # the case files, next to the impersonators, once there is one to show
        items.insert(3, ("cases.html", "Cases"))
    nav = "".join(f'<a href="{up}{href}">{label}</a>' for href, label in items)
    return (f'<header class="top"><div class="wrap"><a class="brand" href="{up}index.html">{MARK}{NAME}</a>'
            f'<nav aria-label="Sections">{nav}</nav>{THEME_SWITCH}</div></header>')


SOURCE_URL = "https://github.com/xxbozohead1/dossierscan"  # the public half: this site, the whitepaper, the server setup


def footer(feed: dict, up: str = "") -> str:
    bot = bot_username()
    report = f' Wrong verdict? <a href="https://t.me/{bot}" rel="noopener" target="_blank">Tell us</a>.' if bot else ""
    d = feed.get("dossier") or {}
    official = (f'<p class="official-ca">Official <a href="{up}token.html">$DOSSIER</a> contract: <code>{e(d["token"])}</code> '
                f'<button type="button" class="mini copy" data-copy="{e(d["token"])}">Copy CA</button></p>') if d.get("launched") else ""
    return f"""<footer class="foot"><div class="wrap">{official}
<p>Method <a href="{up}method.html">{e(feed["method"])}</a> · numbers updated <span data-l="net_at">—</span> ·
<a href="{up}api.html">API</a> · <a href="{SOURCE_URL}" rel="noopener" target="_blank">Source</a></p>
<p class="muted">{NAME} checks who claims a token and what the project behind it is doing. Verdicts are automated from public
posts and pages: one can change, or be wrong, so check its receipt before you trade.{report} Market numbers come from Orbio's
public API and are shown for context: they never change a verdict or a score. Nothing here is financial advice.</p></div></footer>"""


def inert(s: str) -> str:
    """JSON that stays inert inside a <script>: the characters HTML could act on are \\u-escaped."""
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


DESC = "Which Orbio launch is the real one? A file on every agent, with receipts."
OG_IMAGE = ROOT / "static" / "og.png"  # the link-preview card, 1200x630


def site_url() -> str:
    """Where the site is hosted (DOSSIER_SITE_URL), for the absolute links that link previews and sitemaps need."""
    u = os.environ.get("DOSSIER_SITE_URL", "").strip().rstrip("/")
    return u if re.fullmatch(r"https?://[^\s\"'<>]+", u) else ""


def share_tags(title: str, desc: str, path: str | None) -> str:
    """Open Graph and X card tags, so a link pasted on X or Telegram shows a card rather than a bare URL. `path` is the
    page's path from the site root (None: a page with no address of its own, the 404)."""
    site = site_url()
    tags = ['<meta property="og:type" content="website">', f'<meta property="og:site_name" content="{NAME}">',
            f'<meta property="og:title" content="{e(title)}">', f'<meta property="og:description" content="{e(desc)}">']
    if site and path is not None:
        url = f"{site}/{'' if path == 'index.html' else path}"
        tags += [f'<link rel="canonical" href="{e(url)}">', f'<meta property="og:url" content="{e(url)}">']
    if site and OG_IMAGE.exists():
        tags += [f'<meta property="og:image" content="{e(site)}/assets/og.png?v={file_version(OG_IMAGE)}">', '<meta property="og:image:width" content="1200">',
                 '<meta property="og:image:height" content="630">', '<meta name="twitter:card" content="summary_large_image">']
    else:
        tags.append('<meta name="twitter:card" content="summary">')
    if path is None:
        tags.append('<meta name="robots" content="noindex">')
    return "".join(tags)


@functools.cache
def file_version(path: Path) -> str:
    """For images under assets/, which are cached for a year: a new file gets a new URL."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10] if path.exists() else "0"


@functools.cache
def asset_version() -> str:
    """Changes with the stylesheet or the script, so a browser or CDN never pairs new pages with a stale copy."""
    return hashlib.sha256((CSS + JS).encode()).hexdigest()[:10]


def page(title: str, body: str, feed: dict, depth: int = 0, desc: str = "", path: str | None = "",
         root: str | None = None) -> str:
    """One page. Links are relative (`depth` folders down), except with `root`: the 404 page is served at whatever
    address was asked for, so its links start from the site root instead."""
    up = root if root is not None else "../" * depth
    v = asset_version()
    cfg = inert(json.dumps({"href": f"{up}t/{{t}}.html", "live": os.environ.get("DOSSIER_LIVE_URL") or f"{up}api/v1/live.json"}))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{e(title)}</title><meta name="description" content="{e(desc or DESC)}">{share_tags(title, desc or DESC, path)}
<link rel="icon" href="{FAVICON}"><link rel="apple-touch-icon" href="{up}assets/logo.png?v={file_version(LOGO)}"><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}"><link rel="stylesheet" href="{up}assets/style.css?v={v}">{THEME_BOOT}</head>
<body>{header(up)}
<main class="wrap">{body}</main>
{footer(feed, up)}
<div class="toast" role="status" aria-live="polite" hidden></div>
<script>window.DOSSIER={cfg};</script><script src="{up}api/v1/live.js"></script><script src="{up}assets/app.js?v={v}"></script></body></html>
"""


# ----------------------------------------------------------------- live numbers
#
# The pages carry no market numbers of their own: api/v1/live.js (loaded before the page script) fills them in, and
# the page polls api/v1/live.json every minute. So a page's file changes only when what it says changes, and a deploy
# for fresh numbers uploads one small file.

LIVE_KEYS = ("market", "activity", "treasury", "holders")


def thin(hist: list, n: int) -> list:
    if len(hist) <= n:
        return hist
    pts = hist[::max(1, len(hist) // n)]
    return pts if pts[-1] == hist[-1] else pts + [hist[-1]]


def live_data(feed: dict) -> dict:
    """What changes between builds, per token, with short keys (it's fetched every minute)."""
    now = feed["generated_at"]
    out = {}
    for t in feed["tokens"]:
        m, a, tr = t.get("market") or {}, t.get("activity") or {}, t.get("treasury") or {}
        h, hk = holders_now(t)
        real = [x for x in ((t.get("related") or {}).get("claimed") or []) if x["token"] != t["token"]]
        row = {"s": state_of(t, now), "sym": t["symbol"], "n": t["name"], "a": t.get("orbio_agent"), "lt": t["launched_at"],
               "why": sentence(t["verdict"]["why"]), "rc": (t["verdict"]["receipts"] or [None])[0],
               "rl": [real[0]["token"], real[0]["symbol"]] if real else None,
               "m": m.get("mcap_usd"), "p": m.get("price_usd"), "c": m.get("curve_pct"),
               "g": 1 if (m.get("graduated") or t.get("graduated")) else None,
               "v": a.get("vol_24h_usd"), "b": a.get("buys_24h"), "x": a.get("sells_24h"),
               "ch": a.get("change_pct"), "chh": a.get("change_hours"), "h": h, "hk": hk,
               "sp": [[x, round(y)] for x, y in thin(a.get("history") or [], 24)],
               "tb": tr.get("balance_usdg"), "tk": tr.get("staked_orbio"), "tw": tr.get("withdrawn_orbio") or None,
               "te": ((tr.get("credit_owed") or 0) + (tr.get("credit_claimed") or 0) + (tr.get("credit_minted") or 0)) if tr else None,
               "ta": tr.get("credit_activated"), "tu": tr.get("unlocks_at"), "tl": 1 if tr.get("locked") else None}
        out[t["token"]] = {k: v for k, v in row.items() if v is not None and v != []}
    return {"at": now, "net": feed.get("network") or {}, "t": out}


def write_live(feed: dict, out: Path = OUT) -> None:
    """api/v1/live.json (the API, and what pages poll) and api/v1/live.js (loaded before the page script, so numbers
    are there on first paint). Replaced atomically: a reader never sees half a file."""
    data = json.dumps(live_data(feed), separators=(",", ":"), ensure_ascii=False)
    api = out / "api" / "v1"
    api.mkdir(parents=True, exist_ok=True)
    for name, text in (("live.json", data), ("live.js", "window.DOSSIER_LIVE=" + inert(data) + ";\n")):
        tmp = api / (name + ".tmp")
        tmp.write_text(text, "utf-8")
        os.replace(tmp, api / name)


def bot_username() -> str:
    """The lookup bot's @name (DOSSIER_BOT_USERNAME, or what the running bot recorded), for report links."""
    u = os.environ.get("DOSSIER_BOT_USERNAME", "").lstrip("@")
    if not u:
        try:
            u = json.loads((w.DATA / "bot.json").read_text("utf-8")).get("username") or ""
        except (OSError, ValueError):
            u = ""
    return u if re.fullmatch(r"\w{4,32}", u) else ""


def signature(feed: dict) -> str:
    """Changes when anything a page shows changes, apart from the live numbers: deploy on this, not on the clock."""
    now = feed["generated_at"]
    static = [{**{k: v for k, v in t.items() if k not in LIVE_KEYS}, "_state": state_of(t, now)} for t in feed["tokens"]]
    blob = json.dumps({"speed": feed.get("speed"), "method": feed.get("method"), "bot": bot_username(), "site": site_url(),
                       "cases": load_cases(),
                       "assets": [asset_version(), file_version(OG_IMAGE), file_version(LOGO)], "tokens": static}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def ph(key: str) -> str:
    """A number the page script fills in from the live data."""
    return f'<span data-l="{key}">—</span>'


def tools(token: str) -> str:
    return (f'<span class="tools"><button type="button" class="mini copy" data-copy="{token}">Copy CA</button>'
            f'<button type="button" class="mini star" data-star="{token}" aria-pressed="false" aria-label="Add to watchlist">☆</button></span>')


# ----------------------------------------------------------------- the launch map (home) and ticker lineup (files)

def pack(radii: list[float], gap: float = 3.0, angles: int = 18) -> list[tuple[float, float]]:
    """Greedy circle packing, in the order given (biggest first reads best): each circle goes to the free spot nearest
    the centre among the spots touching one already placed. Returns centres, in input order."""
    placed: list[tuple[float, float, float]] = []
    trig = [(math.cos(2 * math.pi * k / angles), math.sin(2 * math.pi * k / angles)) for k in range(angles)]
    for r in radii:
        if not placed:
            placed.append((0.0, 0.0, r))
            continue
        best = None
        for x0, y0, r0 in placed:
            d0 = r0 + r + gap
            for c, s in trig:
                x, y = x0 + d0 * c, y0 + d0 * s
                dist = x * x + y * y
                if best is not None and dist >= best[0]:
                    continue
                if all((x - x1) ** 2 + (y - y1) ** 2 >= (r + r1 + gap) ** 2 - 1e-6 for x1, y1, r1 in placed):
                    best = (dist, x, y)
        placed.append((best[1], best[2], r))
    return [(x, y) for x, y, _ in placed]


def start_mcap(ts: list[dict]) -> float:
    """What every launch is worth before anyone buys (the curve's starting reserve, at today's ORBIO price): the
    market cap of the curves still at 0%. About $6.8k on 2026-10-01; 0 when no such curve is known."""
    return min((m["mcap_usd"] for t in ts if (m := t.get("market") or {}).get("mcap_usd")
                and m.get("curve_pct") == 0 and not m.get("graduated")), default=0.0)


def bubble_r(t: dict, base: float = 0.0) -> float:
    """Radius by market cap above where every launch starts (area follows it), from 8 for an untraded token to 44.
    Raw market cap would draw every untraded launch at the same middling size."""
    m = max(0.0, ((t.get("market") or {}).get("mcap_usd") or 0) - base)
    return max(8.0, min(44.0, 7 + 4.2 * math.sqrt(m / 1000)))


TOP_MAP = 30  # the map's "Biggest" view: at most this many Orbio agents


def biggest(ts: list[dict], base: float = 0.0, n: int = TOP_MAP) -> list[dict]:
    """The Orbio agents worth the most right now, of any age: the map's second view, so a $1M agent from last week
    isn't missing from a page whose map shows only the last 48 hours. Only agents worth a fifth more than every launch
    starts at: below that the list fills up with launches nobody has traded."""
    big = [t for t in ts if t.get("orbio_agent") and ((t.get("market") or {}).get("mcap_usd") or 0) > max(0.0, base) * 1.2]
    return sorted(big, key=lambda t: -t["market"]["mcap_usd"])[:n]


def top_r(t: dict, high: float) -> float:
    """A radius on the Biggest map: 50 for the biggest, the rest by market cap on a squeezed scale, so one at a
    hundredth of the top is still 10 rather than a dot."""
    m = (t.get("market") or {}).get("mcap_usd") or 0
    return max(10.0, min(50.0, 50 * (m / high) ** .35)) if high > 0 else 10.0


def map_notes(t: dict, now: int) -> list[list[str]]:
    """The map card's short list for one bubble: original or copy first, then the worst of the rest of the trader's
    card (the same thresholds as trader_rows), four at most. Plain text: the page script escapes it."""
    tc, ca, s, sym = t.get("trader") or {}, t.get("creator_activity") or {}, state_of(t, now), t["symbol"] or "?"
    head, rest = [], []
    tk = tc.get("ticker")
    if tk and tk["rank"] == 1:
        head.append(["ok", f"First token named ${sym}" + (f"; {tk['later']} more since" if tk["later"] else "")])
    elif tk:
        f = tk["first"] or {}
        head.append(["ok" if s == "verified" else "warn", f"{ordinal(tk['rank'])} ${sym}: the first"
                     + (f" (#{f['vault_id']})" if f.get("vault_id") else "")
                     + f" launched {fmt_span((t['launched_at'] or 0) - (f.get('ts') or 0))} earlier" + where_launched(f)])
    ln, dev = tc.get("launch") or {}, tc.get("dev") or {}
    if (ln.get("launch_block_buyers") or 0) >= 3:
        rest.append(["bad", f"Bundled: {ln['launch_block_buyers']} buyers in the launch block"])
    if "snipers" in ln:
        n, pct, win = ln["snipers"], ln.get("sniped_pct") or 0, ln.get("window_s", 10)
        rest.append(["ok" if not n or pct < 10 else "warn" if pct < 25 else "bad",
                     f"No snipers in the first {win} s" if not n else f"Snipers took {pct:g}% in the first {win} s"])
    if ca.get("bought"):
        sp = ca.get("sold_pct") or 0
        rest.append(["bad" if sp >= 50 else "warn" if sp else "ok",
                      f"Creator sold {sp}% of its launch buy" if sp else "Creator hasn't sold its launch buy"])
    elif ca:
        rest.append(["ok", "Creator didn't buy at launch"])
    if dev.get("walked_away"):
        n = dev["walked_away"]
        rest.append(["bad", f"Creator sold out of {n} earlier launch{'' if n == 1 else 'es'}"])
    if dev.get("launches_week"):
        n = dev["launches_week"]
        rest.append(["warn" if n < 3 else "bad", f"Creator launched {plural(n, 'other token')} this week"])
    top10 = next((f for f in t.get("facts") or [] if f["line"] == "top10"), None)
    if top10 and isinstance(top10.get("top10_share"), (int, float)):
        sh = top10["top10_share"]
        rest.append(["ok" if sh < .3 else "warn" if sh < .5 else "bad",
                     f"Top 10 holders own {sh:.0%}" if sh >= .01 else "Top 10 holders own under 1%"])
    rest += [["bad", FLAGS[f]] for f in t.get("flags") or [] if f in FLAGS and f not in COVERED_FLAGS]
    rest.sort(key=lambda n: ("bad", "warn", "ok").index(n[0]))  # stable: equal ones keep the order above
    return (head + rest)[:4]


def orb_defs() -> str:
    """The launch map's paint: per verdict, a glass body (lit top left, bright at the rim), light pooling at the bottom
    and a glow; one highlight and one backdrop for all. Stops take the verdict's colour from their gradient's v-* class.
    The rules that pick a gradient by verdict sit in the page (not the stylesheet), so url(#…) means this document,
    and a verdict that changes live repaints on its own."""
    grads, rules = [], []
    for k in STATE:
        grads.append(f'<radialGradient id="orb-{k}" class="v-{k}" fx=".34" fy=".28"><stop offset="0" stop-opacity=".62"/>'
                     '<stop offset=".4" stop-opacity=".2"/><stop offset=".74" stop-opacity=".1"/><stop offset=".92" '
                     'stop-opacity=".5"/><stop offset="1" stop-opacity=".95"/></radialGradient>'
                     f'<radialGradient id="cau-{k}" class="v-{k}" cx=".56" cy=".86" r=".5"><stop offset="0" stop-opacity=".6"/>'
                     '<stop offset="1" stop-opacity="0"/></radialGradient>'
                     f'<radialGradient id="glow-{k}" class="v-{k}"><stop offset=".72" stop-opacity="0"/><stop offset=".77" '
                     'stop-opacity=".5"/><stop offset="1" stop-opacity="0"/></radialGradient>'
                     # the scanner's beam, for the animation that opens a file
                     f'<linearGradient id="scan-{k}" class="v-{k}" x2="0" y2="1"><stop offset="0" stop-opacity="0"/>'
                     '<stop offset=".8" stop-opacity=".6"/><stop offset="1" stop-opacity="0"/></linearGradient>')
        rules.append(f".lmap .v-{k}>.skin{{fill:url(#orb-{k})}}.lmap .v-{k}>.cau{{fill:url(#cau-{k})}}"
                     f".lmap .v-{k}>.glow{{fill:url(#glow-{k})}}.lmap .v-{k} .bar{{fill:url(#scan-{k})}}")
    grads.append('<linearGradient id="orb-spec" x2="0" y2="1"><stop class="w" offset="0" stop-opacity=".85"/>'
                 '<stop class="w" offset="1" stop-opacity="0"/></linearGradient>'
                 '<radialGradient id="orb-aura"><stop class="a" offset="0" stop-opacity=".13"/><stop class="a" offset=".6" '
                 'stop-opacity=".05"/><stop class="a" offset="1" stop-opacity="0"/></radialGradient>'
                 '<radialGradient id="orb-field"><stop class="i" offset=".55" stop-opacity="0"/><stop class="i" offset="1" '
                 'stop-opacity=".07"/></radialGradient>')
    rules.append(".lmap .spec{fill:url(#orb-spec)}.lmap .aura{fill:url(#orb-aura)}.lmap .fam circle{fill:url(#orb-field)}")
    return f'<defs>{"".join(grads)}</defs><style>{"".join(rules)}</style>'


def orb(x: float, y: float, r: float) -> str:
    """One bubble's sphere: glow, body, glass, pooled light, a globe grid on the bigger ones (it turns while pointed
    at), and the highlight. Only the body takes the pointer. The page script draws the same sphere (orbSVG) for a
    launch that lands while the page is open and for the animation that opens a file: keep the two in step."""
    c = f'cx="{x:.1f}" cy="{y:.1f}"'
    grid = ""
    if r >= 17:
        grid = (f'<g class="grid" transform="rotate(-18 {x:.1f} {y:.1f})"><ellipse {c} rx="{r * .97:.1f}" ry="{r * .3:.1f}"/>'
                f'<ellipse class="mer" {c} rx="{r * .97:.1f}" ry="{r * .97:.1f}"/>'
                f'<ellipse class="mer b" {c} rx="{r * .97:.1f}" ry="{r * .97:.1f}"/></g>')
    sx, sy = x - r * .24, y - r * .5
    return (f'<circle class="glow" {c} r="{r * 1.3:.1f}"/><circle class="ret" {c} r="{r * 1.17 + 1.5:.1f}"/>'
            f'<circle class="core" {c} r="{r:.1f}"/><circle class="skin" {c} r="{r:.1f}"/><circle class="cau" {c} r="{r:.1f}"/>{grid}'
            f'<ellipse class="spec" cx="{sx:.1f}" cy="{sy:.1f}" rx="{r * .44:.1f}" ry="{r * .22:.1f}" transform="rotate(-28 {sx:.1f} {sy:.1f})"/>'
            f'<circle class="glint" cx="{x - r * .5:.1f}" cy="{y - r * .36:.1f}" r="{max(.8, r * .06):.1f}"/>')


def launch_map(ts: list[dict], now: int, base: float = 0.0, top: list[dict] | None = None) -> str:
    """The home page's map, in two views. New: every launch of the last 48 hours, sized by market cap above where
    every launch starts, older ones paler. Biggest (given `top`, from biggest()): the Orbio agents worth the most now,
    of any age. Both colour by verdict and pack tokens sharing a ticker inside one ring, so the real one (or the lack
    of one) shows among its copies at a glance. A switch flips between them: each view is its own svg with its own
    #mapx-<view> block, and the page script moves the one on show. One legend filters both."""
    views = []
    if len(ts) >= 2:
        views.append(("new", map_svg(ts, now, lambda t: bubble_r(t, base), "new", base)))
    if top and len(top) >= 2:
        high = max((t.get("market") or {}).get("mcap_usd") or 0 for t in top)
        views.append(("top", map_svg(top, now, lambda t: top_r(t, high), "top")))
    if not views:
        return ""
    keys = [("verified", "Verified"), ("scam", "Impersonator"), ("checking", "Checking")]
    if any(v == "top" for v, _ in views):  # agents past their 72 hours unclaimed: only the Biggest view has them
        keys.append(("unverified", "Unverified"))
    legend = "".join(f'<button type="button" class="v-{k}{" for-top" if k == "unverified" else ""}" data-filter="{k}" '
                     f'aria-pressed="false"><i></i>{label}</button>' for k, label in keys)
    switch = ""
    if len(views) == 2:
        switch = ('<div class="mapview" role="group" aria-label="Map view">'
                  '<button type="button" data-view="new" aria-pressed="true">New · 48 h</button>'
                  '<button type="button" data-view="top" aria-pressed="false">Biggest on Orbio</button></div>')
    n_top = len(top or [])
    notes = ('<span class="for-new">Size: market cap above where every launch starts · paler: older, gone after 48 h · '
             'a ring holds the tokens sharing a ticker · a pulse: launched in the last hour</span>'
             f'<span class="for-top">Size: market cap · the {n_top} Orbio agents worth the most now, any age · '
             'a ring holds the tokens sharing a ticker</span>')
    return (f'<figure class="lmapbox" data-view="{views[0][0]}">{switch}'
            f'<svg class="lmapdefs" aria-hidden="true" focusable="false">{orb_defs()}</svg>{"".join(v for _, v in views)}'
            f'<figcaption class="maplegend">{legend}<span class="muted">{notes} · <span class="on-hover">point '
            'at a bubble for its numbers, click for its file, drag to fling it</span><span class="on-touch">tap a bubble '
            'for its numbers, tap again for its file</span></span></figcaption></figure>')


def map_svg(ts: list[dict], now: int, size, mode: str = "new", base: float = 0.0) -> str:
    """One view of the launch map, sized by `size`: an svg of bubbles and rings, then its #mapx-<mode> block."""
    fams: dict[str, list[dict]] = {}
    for t in ts:
        fams.setdefault((t["symbol"] or "?").upper(), []).append(t)
    items = []  # (radius, [(token, dx, dy, r)], ticker or None)
    for sym, members in fams.items():
        members.sort(key=lambda t: -size(t))
        rs = [size(t) for t in members]
        if len(members) == 1:
            items.append((rs[0], [(members[0], 0.0, 0.0, rs[0])], None))
            continue
        cs = pack(rs, gap=2.5)
        cx = sum(x for x, _ in cs) / len(cs)
        cy = sum(y for _, y in cs) / len(cs)
        inner = [(m, x - cx, y - cy, r) for m, (x, y), r in zip(members, cs, rs)]
        items.append((max(math.hypot(dx, dy) + r for _, dx, dy, r in inner) + 7, inner, sym))
    items.sort(key=lambda it: -it[0])
    centres = pack([it[0] for it in items], gap=9)
    xs = [x - it[0] for (x, _), it in zip(centres, items)] + [x + it[0] for (x, _), it in zip(centres, items)]
    ys = [y - it[0] - (14 if it[2] else 0) for (_, y), it in zip(centres, items)] + [y + it[0] for (_, y), it in zip(centres, items)]
    x0, y0 = min(xs) - 6, min(ys) - 6
    w, h = max(xs) - x0 + 6, max(ys) - y0 + 6
    out, labels = [], []
    # per token: notes, ticker rank; per ring: members; b: the sizes' base (for a launch that lands while the page is
    # open); k: which view
    extra: dict = {"t": {}, "f": [], "b": round(base), "k": mode}
    # each lone bubble, or each ring with its bubbles, is one body: drawn around (0, 0) and put in place by a translate,
    # so the page script can float, push, drag and fling it by changing that alone (data-x/y: its place in the layout)
    for (fx, fy), (R, inner, sym) in zip(centres, items):
        X, Y = fx - x0, fy - y0
        fam, parts = "", []
        if sym:  # the ring now, its label last: a pill on top of whatever it touches, so it always reads
            fam = f' data-f="{len(extra["f"])}"'
            ranks = [((m.get("trader") or {}).get("ticker") or {}) for m, *_ in inner]
            extra["f"].append({"s": sym, "n": max([len(inner)] + [k.get("total") or 0 for k in ranks]),
                               "m": [m["token"] for m, *_ in sorted(inner, key=lambda it: it[0]["launched_at"] or 0)]})
            parts.append(f'<g class="fam"{fam}><circle cx="0" cy="0" r="{R:.1f}"/></g>')
            text = f"${sym} ×{len(inner)}"
            pw = 6.4 * len(text) + 14
            labels.append(f'<g class="famlabel{" pair" if len(inner) < 3 else ""}"{fam} transform="translate({X:.1f} {Y:.1f})" '
                          f'tabindex="0" role="button" aria-label="{e(f"{len(inner)} tokens named ${sym}: compare them")}">'
                          f'<g class="pill"><rect x="{-pw / 2:.1f}" y="{-R - 9:.1f}" width="{pw:.1f}" height="18" rx="9"/>'
                          f'<text x="0" y="{-R:.1f}">{e(text)}</text></g></g>')
        for t, x, y, r in inner:
            s = state_of(t, now)
            mc = (t.get("market") or {}).get("mcap_usd")
            tip = f'${t["symbol"] or "?"}' + (f' #{t["orbio_agent"]}' if t.get("orbio_agent") else "") + f" · {STATE[s]}"                 + (f" · {usd(mc)} market cap" if mc else "")
            label = (f'<text class="d" x="{x:.1f}" y="{y:.1f}" style="font-size:{min(13, r * .42):.1f}px">'
                     f'{e((t["symbol"] or "?")[:8 if r >= 34 else 7])}</text>') if r >= 17 else ""
            if r >= 30:  # a phone shows the map at about half size: only big bubbles get a (bigger) label there
                label += f'<text class="m" x="{x:.1f}" y="{y:.1f}">{e((t["symbol"] or "?")[:int(2 * r * .85 / 10.8)])}</text>'
            tk = (t.get("trader") or {}).get("ticker") or {}
            extra["t"][t["token"]] = {k: v for k, v in (("n", map_notes(t, now)),
                                                        ("o", tk.get("rank") if (tk.get("total") or 0) > 1 else None)) if v}
            # every launch in the last 48 hours is still being checked if nobody has claimed it (72 hours), so on the
            # New view an unclaimed one filters as checking; the Biggest view also has agents past that
            st = "scam" if s == "linked" else "checking" if s == "unverified" and mode == "new" else s
            parts.append(f'<a class="bub v-{s}" href="t/{t["token"]}.html" data-t="{t["token"]}"{fam} '
                         f'data-state="{st}"><title>{e(tip)}</title>{orb(x, y, r)}{label}</a>')
        out.append(f'<g class="body" data-x="{X:.1f}" data-y="{Y:.1f}" data-r="{R:.1f}" transform="translate({X:.1f} {Y:.1f})">'
                   f'{"".join(parts)}</g>')
    aura = f'<circle class="aura" cx="{w / 2:.1f}" cy="{h / 2:.1f}" r="{min(w, h) / 2:.1f}"/>'  # faded out before the edges
    what = "the last 48 hours of Orbio launches" if mode == "new" else "the biggest Orbio agents by market cap"
    return (f'<svg class="lmap" data-mode="{mode}" viewBox="0 0 {w:.0f} {h:.0f}" role="group" '
            f'aria-label="Map of {what}, by verdict">{aura}'
            f'<g class="bodies">{"".join(out)}</g><g class="labels">{"".join(labels)}</g></svg>'
            f'<script type="application/json" id="mapx-{mode}">'
            f'{inert(json.dumps(extra, separators=(",", ":"), ensure_ascii=False))}</script>')


def lineup(t: dict) -> str:
    """On a file: every token with this ticker, in launch order, coloured by verdict, this one ringed."""
    fam = ((t.get("trader") or {}).get("ticker") or {}).get("family") or []
    if len(fam) < 2:
        return ""
    dots = []
    for i, f in enumerate(fam, 1):
        tip = f'{ordinal(i)} ${t["symbol"] or "?"}' + (f' · #{f["vault_id"]}' if f.get("vault_id") else "") + f' · {STATE[f["state"]]}'
        href = f'../t/{f["token"]}.html' if not ON_FILE or f["token"] in ON_FILE else f'{EXPLORER}/token/{f["token"]}'
        dots.append(f'<a class="v-{f["state"]}{" me" if f["token"] == t["token"] else ""}" href="{e(href)}" title="{e(tip)}">{i}</a>')
    return (f'<div class="lineup" aria-label="Every token named ${e(t["symbol"] or "?")}, in launch order">{"".join(dots)}</div>'
            '<p class="muted small lineup-note">In launch order, coloured by verdict. This one is ringed.</p>')


# ----------------------------------------------------------------- home and lists

def home(feed: dict) -> str:
    toks, now = feed["tokens"], feed["generated_at"]
    orbio = [t for t in toks if t.get("orbio_agent")]
    verified = sum(1 for t in orbio if t["verdict"]["verdict"] == "verified")
    scams = sorted((t for t in toks if t["verdict"]["verdict"] == "scam"), key=lambda t: -(t["launched_at"] or 0))
    board = sorted((t for t in toks if t["status"] in ("PROVEN", "LIVE", "BUILDING")), key=rank)
    recent = sorted((t for t in orbio if (t["launched_at"] or 0) >= now - 2 * DAY), key=lambda t: -(t["launched_at"] or 0))
    base = start_mcap(toks)
    index = inert(json.dumps([{"t": t["token"], "s": t["symbol"], "n": t["name"], "a": t.get("orbio_agent"),
                               "k": state_of(t, now)} for t in toks], separators=(",", ":")))
    counts = {s: sum(1 for t in recent if state_of(t, now) == s) for s in STATE}
    filters = "".join(f'<button type="button" data-filter="{k}" aria-pressed="{"true" if k == "all" else "false"}">{label}'
                      f'<span>{n}</span></button>' for k, label, n in (
                          ("all", "All", len(recent)), ("verified", "Verified", counts["verified"]),
                          ("checking", "Checking", counts["checking"] + counts["unverified"]), ("scam", "Impersonators", counts["scam"] + counts["linked"])))
    # scams also holds copies launched straight on Pons, off the launchpad: next to the agent count, count agents only
    orbio_scams = sum(1 for t in scams if t.get("orbio_agent"))
    tally = [(e(len(orbio)), "Orbio agents on file"), (e(verified), "verified by their project"),
             (e(orbio_scams), "Orbio impersonators caught")]
    sp = feed.get("speed") or {}
    for kind, label in (("verified", "median time to verify a real project"), ("flagged", "median time to expose an impersonator")):
        if (sp.get(kind) or {}).get("n", 0) >= 3:  # fewer launches than that isn't a figure worth printing
            tally.append((f'{sp[kind]["median_min"]:.0f} min', label))
    tally += [(ph("net_orbio"), "ORBIO price"), (ph("net_mcap"), "all Orbio agents, market cap")]
    return f"""<div class="stack">
<section class="intro">
<p class="eyebrow">Orbio launchpad · Robinhood Chain</p>
<h1>Know which launch is the real one.</h1>
<p class="lede">{NAME} opens a file on every agent launched on Orbio. It checks the project's own X account and website for
this exact contract, flags the impersonators, and tracks whether anything is actually being built. Every verdict links its receipt.</p>
<div class="search"><label for="q" class="sr">Search tokens</label><input id="q" type="search"
placeholder="Paste a contract address or type a ticker" autocomplete="off" spellcheck="false">
<div id="results" class="results" hidden></div></div>
<script id="idx" type="application/json">{index}</script>
<dl class="tally">{"".join(f"<div><dd>{v}</dd><dt>{e(k)}</dt></div>" for v, k in tally)}</dl>
</section>

<section id="watching" hidden><div class="sec-head"><div><h2>Your watchlist</h2>
<p class="sub">Tokens you starred, saved in this browser.</p></div></div><ol class="feed" id="watchlist"></ol></section>

<section id="live"><div class="sec-head"><div><h2>New on Orbio</h2>
<p class="sub">Every agent launched in the last 48 hours, newest first. New launches and verdicts appear here as they land.</p></div>
<div class="filters" role="group" aria-label="Show">{filters}</div></div>
{launch_map(recent, now, base, biggest(toks, base))}
{feed_cards(recent, now)}
<p class="more"><a href="agents.html">Every Orbio agent on file →</a></p></section>

<section id="board"><div class="sec-head"><div><h2>The board</h2>
<p class="sub">Tokens their own project claims, ranked by how far they've proven something real: status first, then the
evidence that it exists and works. Hover a cell for what it measures. <a href="method.html">How scores work</a>.</p></div></div>
{board_table(board, now)}</section>

<section id="scams"><div class="sec-head"><div><h2>Impersonators caught</h2>
<p class="sub">Tokens that copy a real project. Each one says why it's fake and links the proof, with what else its
wallet launched and the real token when there is one.</p></div></div>
{scam_list(scams[:10], now)}
<p class="more"><a href="scams.html">All {len(scams)} impersonators →</a> <span class="muted">{scam_split(scams)}</span></p></section>

<section id="how"><h2>How {NAME} decides</h2><div class="how">
<div>{chip("verified")}<p>{STATE_TEXT["verified"]} Social links in the token's metadata count for nothing: anyone can copy them.</p></div>
<div>{chip("scam")}<p>{STATE_TEXT["scam"]} The page quotes the post or names the contract the project claims instead.
Anything launched from the same wallet, or the same chain of wallets, is flagged too: {chip("linked")}</p></div>
<div>{chip("checking")}<p>No official channel lists this contract yet. For the first six hours after launch, {NAME} looks for the
project's post every minute.</p></div>
</div><p class="more"><a href="method.html">The full method →</a></p></section>
</div>"""


def where_launched(f: dict) -> str:
    """", straight on Pons" for a first token that isn't an Orbio agent: it's not in the Orbio feed, so say where it is."""
    return "" if f.get("vault_id") else ", straight on Pons"


def first_link(f: dict, up: str = "") -> str:
    """"the first", linked to its file when it has one here, else to the explorer."""
    label = "the first" + (f" (#{e(f['vault_id'])})" if f.get("vault_id") else "")
    if f.get("token") and (not ON_FILE or f["token"] in ON_FILE):
        return f'<a href="{up}t/{e(f["token"])}.html">{label}</a>'
    return link(f"{EXPLORER}/token/{f['token']}", f"{label} ↗") if f.get("token") else label


def card(t: dict, now: int, up: str = "") -> str:
    s = state_of(t, now)
    rc = t["verdict"]["receipts"][:1]
    real = [x for x in ((t.get("related") or {}).get("claimed") or []) if x["token"] != t["token"]]
    extra = f'<p class="real">Real token: {tref(real[0], up, "tok inline")}</p>' if s == "scam" and real else ""
    tk = (t.get("trader") or {}).get("ticker")
    if tk and tk["rank"] > 1 and s != "verified":  # not the first token with this ticker: say so where hunters scan,
        f = tk["first"] or {}                        # and link it (it may be a Pons launch, which isn't in this feed)
        extra += (f'<p class="copynote">{ordinal(tk["rank"])} ${e(t["symbol"] or "?")}: {first_link(f, up)} launched '
                  f'{e(fmt_span((t["launched_at"] or 0) - (f.get("ts") or 0)))} earlier{where_launched(f)}</p>')
    return f"""<li class="card" data-t="{t["token"]}" data-lt="{t["launched_at"] or 0}" data-state="{group_of(s)}">
<div class="card-top"><span class="fileno">{fileno(t)}</span><span class="age">{when(t["launched_at"])}</span>{tools(t["token"])}</div>
<div class="card-title">{tref(t, up, "tok stretch")}<span data-l="chip">{chip(s)}</span></div>
<div class="trend" data-l="trend"></div>
<dl class="card-nums"><div><dt>Market cap</dt><dd>{ph("mcap")}</dd></div><div><dt>Curve</dt><dd>{ph("curve")}</dd></div>
<div><dt>Holders</dt><dd>{ph("holders")}</dd></div></dl>
<p class="why">{e(sentence(t["verdict"]["why"]))}{"".join(f" {link(u, 'Receipt', 'rcpt')}" for u in rc)}</p>{extra}</li>"""


def feed_cards(ts: list[dict], now: int, up: str = "") -> str:
    return f'<ol class="feed" id="feed">{"".join(card(t, now, up) for t in ts)}</ol>' + \
        ("" if ts else '<p class="muted empty">No launches in the last 48 hours.</p>')


# the board's read of a row, so a hunter can scan it: the status (scoring-spec §5) with an icon, a caution for a red
# flag on the token, and the five dimensions as heat cells, greener with more evidence
STATUS_TEXT = {
    "PROVEN": "Proven: a real product, steady output and an accountable team, held for 7 days in a row.",
    "LIVE": "Live: something verifiably works: a working app, verified product output, or outside users of its contracts.",
    "BUILDING": "Building: its project claims it and is showing work, but nothing verifiably works yet.",
}
DIM_ASK = {"product": "Does something exist, and does it work?", "build": "Is code being written?",
           "team": "Is someone accountable?", "work": "Is it doing anything?", "integrity": "Is the token itself sound?"}
CAUTION = {"CREATOR_EXIT": "Creator sold", "FEE_REDIRECT": "Fees redirected", "LAUNCH_BUNDLE": "Bundled launch",
           "SERIAL": "Serial launcher", "CONFLICT": "Channels disagree", "BORROWED": "Borrowed brand"}
ICON = {
    "proven": '<circle cx="8" cy="8" r="7" fill="currentColor"/><path d="M4.9 8.2l2.1 2.1 4.2-4.5" fill="none" '
              'stroke="var(--sheet)" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>',
    "live": '<circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M5.2 8.2l1.9 1.9 '
            '3.8-4.1" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    "building": '<circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor" stroke-width="1.6"/>'
                '<path d="M8 1.8a6.2 6.2 0 0 1 0 12.4z" fill="currentColor"/>',
    "caution": '<path d="M8 2l6.4 11.4H1.6z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>'
               '<path d="M8 6.3v3.2M8 11.5v.1" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
}
SEAL = MARK.replace('class="mark"', 'class="mark seal"')


def icon(name: str) -> str:
    return f'<svg class="ic" viewBox="0 0 16 16" aria-hidden="true">{ICON[name]}</svg>'


def cautions(t: dict) -> list[str]:
    return [f for f in t.get("flags") or [] if f in CAUTION]


def tone(t: dict) -> str:
    """A row's colour: amber for a red flag, whatever the status, else the status's own."""
    return "caution" if cautions(t) else {"PROVEN": "proven", "LIVE": "live"}.get(t["status"], "building")


def status_badge(status: str) -> str:
    k = {"PROVEN": "proven", "LIVE": "live"}.get(status, "building")
    return f'<span class="sbadge sb-{k}" title="{e(STATUS_TEXT.get(status, ""))}">{icon(k)}{e(status.title())}</span>'


def caution_pill(flag: str) -> str:
    return f'<span class="caution" title="{e(FLAGS[flag])}">{icon("caution")}{e(CAUTION[flag])}</span>'


def seal_or_chip(s: str) -> str:
    """Next to a board ticker: the seal when the project lists this contract, else the verdict chip (data-l "seal")."""
    return f'<span class="sealwrap" title="{e(STATE_TEXT["verified"])}">{SEAL}</span>' if s == "verified" else chip(s)


def heat(v: float, mx: int) -> float:
    """How green a dimension's cell is: its share of the maximum, with a floor so any evidence at all shows."""
    return 0.0 if v <= 0 else max(0.14, min(1.0, v / mx))


def lookalikes(t: dict) -> str:
    n = ((((t.get("trader") or {}).get("ticker") or {}).get("total") or 1) - 1)
    if n < 1:
        return ""
    return (f'<span class="look" title="{plural(n, "other token")} {"uses" if n == 1 else "use"} the ticker ${e(t["symbol"] or "?")}. This is the one '
            f'its project claims: check the contract before you buy.">+{plural(n, "lookalike")}</span>')


def board_table(board: list[dict], now: int, up: str = "") -> str:
    """The board in its own order (rank()), with # as that place: a click on a column re-sorts it (market cap, a
    dimension), a click on # puts it back. Trust reads left to right: the seal, the status and any red flag, the score,
    then the evidence behind it; market numbers sit apart, as context."""
    rows = []
    for i, t in enumerate(board, 1):
        sc, s = t.get("scores") or {}, state_of(t, now)
        comp = sc.get("composite") or 0
        dims = {k: sc.get(k) or 0 for k, _, _ in DIMS}
        sub = (lookalikes(t) if s == "verified" else "") + "".join(
            f'<span class="tag">{e(b.title())}</span>' for b in t.get("badges") or [] if b in ("WINNER", "BUILD WEEK"))
        shown = {k: f"{v:.0f}" if v >= 0.5 else "&lt;1" if v > 0 else "–" for k, v in dims.items()}
        cells = "".join(f'<td class="heat hide-sm" title="{label}: {dims[k]:g} of {mx}. {DIM_ASK[k]}">'
                        f'<span style="--f:{heat(dims[k], mx):.2f}">{shown[k]}</span></td>' for k, label, mx in DIMS)
        strip = "".join(f'<i style="--f:{heat(dims[k], mx):.2f}">{label[0]}</i>' for k, label, mx in DIMS)
        capped = f', capped by {e(t["cap"])}' if t.get("cap") else ""
        data = " ".join(f'data-{k}="{v:.1f}"' for k, v in dims.items())
        rows.append(f"""<tr class="t-{tone(t)}{" pg-off" if i > 10 else ""}" data-t="{t["token"]}" data-rank="{i}" data-score="{comp:.1f}" {data}>
<td class="num">{i}</td><td class="who"><div class="who-top"><span data-l="seal">{seal_or_chip(s)}</span>{tref(t, up)}</div>
{f'<div class="tsub">{sub}</div>' if sub else ""}<span class="mheat show-sm" title="Product, Build, Team, Work, Integrity">{strip}</span></td>
<td class="trust">{status_badge(t["status"])}{"".join(caution_pill(f) for f in cautions(t))}</td>
<td class="sc"><span class="ring" style="--p:{max(0, min(100, comp)):.0f}" title="Score {comp:.0f} of 100: the five evidence columns added up{capped}. It sets the order; the columns say why.">{comp:.0f}</span></td>
{cells}<td class="numcol mkt first hide-sm">{ph("mcap")}</td><td class="numcol mkt hide-sm">{ph("vol")}</td><td class="mkt hide-sm">{ph("curve")}</td></tr>""")
    group = ('<tr class="grp hide-sm"><th colspan="4"></th><th colspan="5" class="g-ev">Evidence · greener is stronger</th>'
             '<th colspan="3" class="g-mkt">Market · context only</th></tr>')
    head = (sort_th("rank", "#", "num", "ascending") + "<th>Token</th><th>Status</th>" + sort_th("score", "Score", "sc")
            + "".join(sort_th(k, f"{label}<small>of {mx}</small>", "heat hide-sm", tip=DIM_ASK[k]) for k, label, mx in DIMS)
            + sort_th("mcap", "Mkt cap", "numcol mkt first hide-sm") + sort_th("vol", "24h vol", "numcol mkt hide-sm")
            + sort_th("curve", "Curve", "mkt hide-sm"))
    return f"""{board_key()}<div class="scroll"><table class="list sortable board" data-per="10" data-label="Pages of the board"><thead>{group}<tr>{head}</tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>"""


def board_key() -> str:
    """The legend above the board: what each status, the seal, a red flag and a green cell mean."""
    items = [(f'<span class="sealwrap">{SEAL}</span>', "listed by its project"),
             (status_badge("PROVEN"), "held 7 days"), (status_badge("LIVE"), "verifiably works"),
             (status_badge("BUILDING"), "not working yet"),
             (f'<span class="caution">{icon("caution")}Red flag</span>', "on the token"),
             ('<span class="ramp" aria-hidden="true"><i style="--f:.14"></i><i style="--f:.45"></i><i style="--f:1"></i></span>',
              "more evidence")]
    strip = "".join(f'<i style="--f:.45">{label[0]}</i>' for _, label, _ in DIMS)
    return ('<ul class="boardkey">' + "".join(f"<li>{a}<span>{e(b)}</span></li>" for a, b in items)
            + f'<li class="k-sm"><span class="mheat" style="margin:0">{strip}</span><span>Product, Build, Team, Work, Integrity</span></li></ul>')


# an impersonator's row leads with why it's fake: a red reason, the evidence (the project's own words, the contract it
# claims instead, or the confirmed copy the wallet is tied to), then the numbers behind it, worst first
REASON = {"disowned": "Disowned by its project", "contract": "Not the project's contract", "wallet": "Copycat's wallet",
          "chain": "Copycat's funding", "other": "Impersonator"}
ICON["stop"] = ('<path d="M5.3 1.5h5.4l3.8 3.8v5.4l-3.8 3.8H5.3l-3.8-3.8V5.3z" fill="none" stroke="currentColor" stroke-width="1.5" '
                'stroke-linejoin="round"/><path d="M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4" stroke="currentColor" stroke-width="1.6" '
                'stroke-linecap="round"/>')


def handle_link(t: dict, who: str) -> str:
    """The project channel the verdict rests on, linked when the file has it: @handle for an X account, the address for
    a site (the reason names a site by its short key, "agraris")."""
    url = next((i["url"] for i in t.get("identities") or [] if i["binding"] in ("contradicted", "disavowed")), "")
    return (link(url, who) if who.startswith("@") else link(url)) if url else e(who)


def scam_reason(t: dict, up: str = "") -> tuple[str, str]:
    """(kind, the evidence as HTML). The verdict's reason (proof_index.explain) is one sentence the Telegram alerts embed;
    the list takes it apart so the project's quote, or the copy behind the wallet, is what the eye lands on."""
    v = t["verdict"]
    why = v.get("why") or ""
    rc = "".join(f" {link(u, 'Receipt', 'rcpt')}" for u in (v.get("receipts") or [])[:1])
    if v.get("kind") == "linked":
        via = v.get("via") or {}
        ref = (tref({"token": via["token"], "symbol": via.get("symbol"), "orbio_agent": via.get("vault_id"), "name": None},
                    up, "tok inline") if via.get("token") else "a confirmed copy")
        if "the same wallet launched" in why:
            return "wallet", f"The wallet that launched it also launched {ref}, a confirmed copy."
        m = re.search(r"and (\d+) other confirmed cop", why)
        more = f" and {m.group(1)} other confirmed {'copy' if m.group(1) == '1' else 'copies'}" if m else ", a confirmed copy"
        return "chain", f"Funded through the same chain of wallets that launched {ref}{more}."
    m = re.match(r"(@\w+) disowns it: “(.*)”\s*(.*)$", why, re.S)
    if m:
        return "disowned", (f'<blockquote>“{e(m.group(2))}”</blockquote><p class="cite">{handle_link(t, m.group(1))}'
                            f'{f", {e(m.group(3))}" if m.group(3) else ""}{rc}</p>')
    m = re.match(r"not the token (\S+) claims(?: \(it claims (.*?)\))?(.*)$", why, re.S)
    if m:  # (.*) after it: a cluster's "; it shares that identity with the claimed token"
        return "contract", (f"{handle_link(t, m.group(1))} lists a different contract"
                            + (f": <code>{e(m.group(2))}</code>" if m.group(2) else "") + f"{e(m.group(3))}.{rc}")
    return "other", e(sentence(why)) + rc


def scam_facts(t: dict, kind: str, up: str = "") -> list[tuple[str, str]]:
    """(bad|warn|ok|"", html): what its wallet has launched, how new the wallet was, where it sits among tokens with its
    ticker, and the real token when the project has one."""
    out = []
    dev = (t.get("trader") or {}).get("dev") or {}
    others, fakes = dev.get("other_launches") or 0, dev.get("impersonators") or 0
    if others:
        out.append(("bad" if fakes else "warn" if others >= 3 else "",
                    f"Its wallet launched <b>{others}</b> other token{'' if others == 1 else 's'}"
                    + (f", <b>{fakes}</b> of them confirmed {'copy' if fakes == 1 else 'copies'}" if fakes else "")))
    elif dev:
        out.append(("", "First launch from its wallet"))
    if dev.get("first_seen") and t.get("launched_at") and t["launched_at"] >= dev["first_seen"]:
        age = t["launched_at"] - dev["first_seen"]
        out.append(("bad" if age < DAY else "warn" if age < 30 * DAY else "", f"Wallet <b>{e(fmt_span(age))}</b> old at launch"))
    tk = (t.get("trader") or {}).get("ticker") or {}
    if (tk.get("total") or 0) > 1:
        out.append(("", f"{ordinal(tk['rank'])} of <b>{tk['total']}</b> tokens named ${e(t['symbol'] or '?')}"))
    if kind in ("wallet", "chain"):  # flagged for its money: name the identity it borrows
        handle = next((m.group(1) for i in t.get("identities") or []
                       if (m := re.fullmatch(r"https?://(?:www\.)?(?:x|twitter)\.com/(\w+)/?", i.get("url") or ""))), None)
        if handle:
            out.append(("", f"Claims to be {link(f'https://x.com/{handle}', '@' + handle)}"))
    real = [x for x in ((t.get("related") or {}).get("claimed") or []) if x["token"] != t["token"]]
    if real:
        out.append(("ok", f"Real token: {tref(real[0], up, 'tok inline')}"))
    elif kind == "disowned":
        out.append(("", "No real token launched yet"))
    order = {"bad": 0, "warn": 1, "": 2, "ok": 3}
    return sorted(out, key=lambda f: order[f[0]])


def scam_list(ts: list[dict], now: int, up: str = "", searchable: bool = False, per: int = 5) -> str:
    """Newest first, `per` a page (the page's script adds the pager and pages what a filter leaves)."""
    if not ts:
        return '<p class="muted">None caught yet.</p>'
    out = []
    for i, t in enumerate(ts, 1):
        kind, ev = scam_reason(t, up)
        facts = "".join(f'<li class="{c}"><i></i><span>{h}</span></li>' for c, h in scam_facts(t, kind, up))
        attr = f' data-search="{e(search_key(t))}"' if searchable else ""
        out.append(f"""<li class="imp{" pg-off" if i > per else ""}"{attr}><div class="imp-id">{tref(t, up)}<div class="imp-meta">
<span class="rbadge" title="{e(STATE_TEXT[state_of(t, now)])}">{icon("stop")}{REASON[kind]}</span><span class="age">{when(t["launched_at"])}</span></div></div>
<div class="imp-body"><div class="imp-ev">{ev}</div>{f'<ul class="imp-facts">{facts}</ul>' if facts else ""}</div></li>""")
    return (f'<ul class="imps" data-per="{per}" data-label="Pages of impersonators" data-prev="← Newer" data-next="Older →">'
            f'{"".join(out)}</ul>')


def sort_th(key: str, label: str, cls: str = "", order: str = "", tip: str = "") -> str:
    """A column a click sorts by; `order` marks the one the rows already come sorted by."""
    c = f' class="{cls}"' if cls else ""
    o = f' aria-sort="{order}"' if order else ""
    ti = f' title="{e(tip)}"' if tip else ""
    return f'<th{c} data-sort="{key}"{o}{ti}><button type="button">{label}</button></th>'


def agents_page(feed: dict) -> str:
    now = feed["generated_at"]
    orbio = sorted((t for t in feed["tokens"] if t.get("orbio_agent")), key=lambda t: -(t["orbio_agent"] or 0))
    rows = "".join(f"""<tr data-t="{t["token"]}" data-lt="{t["launched_at"] or 0}" data-sym="{e((t["symbol"] or "").lower())}"
data-state="{state_of(t, now)}" data-search="{e(search_key(t))}"><td>{tref(t)}</td><td><span data-l="chip">{chip(state_of(t, now))}</span></td>
<td class="hide-sm">{status_chip(t["status"])}</td><td class="numcol">{ph("mcap")}</td><td class="numcol hide-sm">{ph("vol")}</td>
<td class="hide-sm">{ph("curve")}</td><td class="hide-sm muted">{when(t["launched_at"])}</td>
<td class="star-cell hide-sm"><button type="button" class="mini star" data-star="{t["token"]}" aria-pressed="false" aria-label="Add to watchlist">☆</button></td></tr>"""
                   for t in orbio)
    head = (sort_th("name", "Token") + sort_th("verdict", "Official") + '<th class="hide-sm">Status</th>' + sort_th("mcap", "Mkt cap", "numcol")
            + sort_th("vol", "24h vol", "numcol hide-sm") + sort_th("curve", "Curve", "hide-sm")
            + sort_th("age", "Launched", "hide-sm") + '<th class="hide-sm"><span class="sr">Watch</span></th>')
    return f"""<section class="page-head"><h1>Every Orbio agent on file</h1><p class="sub">{len(orbio)} agents launched through the
Orbio AgentVault. Tap a column to sort.</p></section>
<input class="filter" type="search" placeholder="Filter by ticker, name or address" aria-label="Filter agents">
<div class="scroll"><table class="list sortable"><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>"""


def not_found_page() -> str:
    """The 404. A token address with no file gets its own message: brand-new Orbio agents are on file within a minute,
    other tokens only once a project claims them."""
    return f"""<section class="page-head notfound"><h1>No file here</h1>
<p class="sub" id="nf">This page doesn't exist. The link may be mistyped, or out of date.</p>
<p class="more"><a href="/index.html#live">New launches</a> · <a href="/agents.html">Every Orbio agent</a> ·
<a href="/scams.html">Impersonators</a></p></section>
<script>(function(){{var m=location.pathname.match(/\\/t\\/(0x[0-9a-fA-F]{{40}})(\\.html)?$/);if(!m)return;var a=m[1].toLowerCase();
document.getElementById('nf').innerHTML='{NAME} has no file on <code>'+a+'</code> yet. New Orbio agents get one within a minute '+
'of launch; other tokens only once a project claims them. <a href="{EXPLORER}/token/'+a+'" rel="nofollow noopener" '+
'target="_blank">See it on the explorer</a>.'}})();</script>"""


def scam_split(scams: list[dict]) -> str:
    n = sum(1 for t in scams if t.get("orbio_agent"))
    return f"{n} launched on Orbio, {len(scams) - n} elsewhere on Robinhood Chain"


def scams_page(feed: dict) -> str:
    now = feed["generated_at"]
    scams = sorted((t for t in feed["tokens"] if t["verdict"]["verdict"] == "scam"), key=lambda t: -(t["launched_at"] or 0))
    return f"""<section class="page-head"><h1>Impersonators caught</h1><p class="sub">{len(scams)} tokens that copy a real
project's identity: {scam_split(scams)}. Orbio agents show their number (#). A token is marked an impersonator when an
official channel lists a different contract or says the token isn't theirs, or when it was launched from the wallet, or the
chain of wallets, behind a confirmed copy.</p></section>
<input class="filter" type="search" placeholder="Filter by ticker, name or address" aria-label="Filter impersonators">
{scam_list(scams, now, searchable=True)}"""


# ----------------------------------------------------------------- token file

def token_page(t: dict, feed: dict) -> str:
    now = feed["generated_at"]
    s = state_of(t, now)
    addr = t["token"]
    tr, tl = t.get("treasury"), t.get("timeline") or {}
    agent = t.get("orbio_agent")
    xp = t.get("x_profile") or {}
    site = next((i["url"] for i in t.get("identities") or [] if i["role"] == "website"), None)
    actions = []
    if agent and s not in RED:
        actions.append(link(f"https://www.orbio.so/launchpad/{addr}", "Trade on Orbio" if s == "verified" else "Orbio page",
                            "btn primary" if s == "verified" else "btn"))
    actions.append(link(f"https://dexscreener.com/robinhood/{addr}", "Chart", "btn"))
    if xp.get("handle"):
        actions.append(link(f"https://x.com/{xp['handle']}", f"@{xp['handle']}", "btn"))
    if site:
        actions.append(link(site, "Website", "btn"))
    actions.append(link(f"{EXPLORER}/token/{addr}", "Explorer", "btn"))
    lag = (tl.get("verified_at") or 0) - (t["launched_at"] or 0) if tl.get("verified_at") else None
    note = {"verified": f"Verified {fmt_span(lag)} after launch" if lag is not None and lag >= 0 else "Verified",
            "scam": "Flagged " + (fmt_span(tl["flagged_at"] - t["launched_at"]) + " after launch"
                                  if tl.get("flagged_at") and t["launched_at"] and tl["flagged_at"] >= t["launched_at"] else "by the index"),
            "linked": "Flagged for its money: no project has claimed it",
            "checking": f"Checked {plural(tl.get('checks') or 0, 'time')} so far",
            "unverified": "No claim after 72 hours"}[s]
    badges = "".join(f'<span class="badge">{e(b.title())}</span>' for b in t.get("badges") or [] if b != "ORBIO AGENT")
    return f"""<div class="filepage" data-t="{addr}"><p class="crumb"><a href="../index.html#live">← New launches</a></p>
<article class="file v-{s}">
<div class="file-tab">{"File No. " + str(agent) + " · Orbio agent" if agent else "Pons launch"}</div>
<header class="file-head"><div class="who"><h1><span class="tkr">${e(t["symbol"] or "?")}</span> <span class="nm">{e(t["name"] or "")}</span></h1>
<p class="meta">Launched {when(t["launched_at"])}{f' by {link(EXPLORER + "/address/" + t["creator"], short(t["creator"]))}' if t.get("creator") else ""} {badges}</p>
<p class="ca"><code>{addr}</code><button type="button" class="copy" data-copy="{addr}">Copy</button>
<button type="button" class="copy star watch" data-star="{addr}" aria-pressed="false">☆ Watch</button></p></div>
<div class="stampbox"><span class="stamp big v-{s}">{STATE[s]}</span><span class="stampnote">{e(note)}</span></div></header>
<div class="actions">{"".join(actions)}</div>
{stat_board(bool(agent))}
<div data-l="chart"></div>{"" if agent else '<p class="muted small">Market numbers come from Orbio and cover Orbio agents only.</p>'}
</article>
{verdict_block(t, s)}
<div class="cols">{project_block(t, s)}{score_block(t)}</div>
{trader_block(t, now)}
{treasury_block(t) if tr else ""}
{identities(t)}
{evidence(t) if t.get("facts") else ""}
<p class="muted small">Raw data for this file: <a href="../api/v1/tokens/{addr}.json">{addr[:10]}….json</a></p></div>"""


def fmt_span(secs: int | None) -> str:
    if secs is None:
        return "—"
    if secs < 90:
        return "under 2 min"
    if secs < 3600:
        return f"{round(secs / 60)} min"
    if secs < 2 * DAY:
        return f"{secs / 3600:.1f} h"
    return f"{round(secs / DAY)} days"


def verdict_block(t: dict, s: str) -> str:
    v = t["verdict"]
    rel = t.get("related") or {}
    receipts = "".join(f"<li>{link(u, receipt_text(u))}</li>" for u in v["receipts"])
    extra = ""
    real = [r for r in rel.get("claimed") or [] if r["token"] != t["token"]]
    if s == "linked":
        via = v.get("via") or {}
        if via.get("token"):
            extra = ("<div class=\"related bad\"><h3>The confirmed copy it's linked to</h3><ul><li>"
                     + tref({"token": via["token"], "symbol": via.get("symbol"), "orbio_agent": via.get("vault_id"), "name": None}, "../")
                     + "</li></ul><p class=\"muted\">The receipt is the transfer that ties this launch's wallet to that one. "
                     "An official post or page from the project it names would clear it.</p></div>")
    elif s == "scam" and real:
        extra = ('<div class="related"><h3>The real token</h3><ul>' + "".join(
            f'<li>{tref(r, "../")} <span class="muted">claimed by {e(r["via"].split(":", 1)[1])}</span></li>'
            for r in real) + "</ul></div>")
    elif s == "scam":
        extra = '<p class="muted">The project behind the identity it copies hasn\'t launched a token the index knows of.</p>'
    elif rel.get("impersonators"):
        fakes = rel["impersonators"]
        extra = (f'<div class="related bad"><h3>{plural(len(fakes), "impersonator")} of this project</h3><ul>' + "".join(
            f'<li>{tref(r, "../")} <span class="muted">{when(r["launched_at"])}</span></li>' for r in fakes[:12]) + "</ul>"
            + (f'<p class="muted">…and {len(fakes) - 12} more.</p>' if len(fakes) > 12 else "") + "</div>")
    if s == "verified" and real:
        extra += ('<div class="related"><h3>The same team also claims</h3><ul>' + "".join(
            f"<li>{tref(r, '../')}</li>" for r in real) + "</ul></div>")
    bot = bot_username()
    report = (f'<p class="report">Wrong verdict? <a href="https://t.me/{bot}?start=r_{t["token"]}" rel="noopener" target="_blank">'
              "Report it</a>. A person reads every report, and it triggers a fresh check.</p>") if bot else ""
    return f"""<section class="verdict v-{s}"><h2>Is this the project's token?</h2>
<p class="why">{e(sentence(v["why"]))}</p>{f'<ul class="receipts">{receipts}</ul>' if receipts else ""}
<p class="muted">{STATE_TEXT[s]}</p>{extra}{report}</section>"""


def tile(label: str, key: str, cls: str) -> str:
    """One stat tile: a label, then what the page script draws for `key` (a big value, a gauge or a bar, a caption)."""
    return f'<div class="tile {cls}"><span class="lbl">{e(label)}</span><div class="tv" data-l="{key}">—</div></div>'


def stat_board(agent: bool) -> str:
    """A file's market numbers: the market cap large with its move beside it, then the tiles a trader scans."""
    return (f'<div class="stats"><div class="hero"><div class="hero-mc"><span class="lbl">Market cap</span>'
            f'<span class="mc" data-l="mcap">—</span><span data-l="chg_pill"></span></div>'
            f'<div><span class="lbl">24h volume</span><span class="hv" data-l="vol">—</span><small class="hc" data-l="vol_where"></small></div>'
            f'<div><span class="lbl">Price</span><span class="hv" data-l="price">—</span><small class="hc">per token, in USD</small></div></div>'
            f'<div class="tiles">{tile("Bonding curve", "curve_t", "t-curve")}{tile("24h buys / sells", "trades_t", "t-trades")}'
            f'{tile("Real holders", "holders_t", "t-holders")}{tile("Agent balance", "bal_t", "t-bal") if agent else ""}</div></div>')


def project_block(t: dict, s: str = "") -> str:
    xp = t.get("x_profile") or {}
    parts = []
    ab = t.get("about") or {}
    if ab.get("line") and s not in RED:  # an impersonator's page must not describe the project it copies as its own
        kind = {"utility": "Utility", "meme": "Meme"}.get(ab.get("kind"), "")
        parts.append(f'<div class="read"><p class="read-k"><span class="lbl">{NAME}’s read</span>'
                     + (f'<span class="kind k-{e(ab["kind"])}">{kind}</span>' if kind else "")
                     + f'</p><p class="read-line">{e(ab["line"])}</p></div><p class="muted small">An AI summary of what the '
                     "launcher’s description, the X account and the site say. It describes; the verdict above is what vouches.</p>")
    if t.get("claim"):
        parts.append(f'<p class="claim">{e(t["claim"])}</p><p class="muted small">Summarised from the site and posts it points at; '
                     "the quote behind it was checked against the page.</p>")
    if t.get("description"):
        parts.append(f'<blockquote class="desc">{e(w.clip(t["description"], 500))}</blockquote>'
                     '<p class="muted small">The token description, written by whoever launched it.</p>')
    if xp.get("followers") is not None:
        joined = dt.datetime.fromtimestamp(xp["created_at"], dt.timezone.utc).strftime("%b %Y") if xp.get("created_at") else "—"
        parts.append(f"""<div class="xcard"><p><b>@{e(xp["handle"])}</b> <span class="muted">· {num(xp["followers"])} followers ·
joined {joined}{f' · {plural(xp["posts_28d"], "post")} in 4 weeks' if xp.get("posts_28d") is not None else ""}</span></p>
{f'<p class="bio">{e(xp["bio"])}</p>' if xp.get("bio") else ""}</div>""")
    if not parts:
        parts.append('<p class="muted">Nothing it points at describes a product yet.</p>')
    return f'<section class="project"><h2>The project</h2>{"".join(parts)}</section>'


def score_block(t: dict) -> str:
    sc = t.get("scores")
    if t["verdict"]["verdict"] == "scam" and not sc:
        return """<section class="score"><h2>Score</h2><p class="big muted">Not scored</p>
<p>Impersonators aren't scored: nothing they point at belongs to them.</p></section>"""
    return f"""<section class="score"><h2>Score</h2><div class="scorehead">{status_chip(t["status"])}
<p class="big">{(sc or {}).get("composite") or 0:.0f}<small>/100</small></p></div><p>{e(sentence(t["explain"]))}</p>
{bars(sc, labels=True) if sc else ""}{f'<p class="muted small">Capped by {e(t["cap"])}.</p>' if t.get("cap") else ""}</section>"""


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


COVERED_FLAGS = {"LAUNCH_BUNDLE", "CREATOR_EXIT", "SERIAL"}  # the trader's card says these in its own words


def trader_rows(t: dict, now: int) -> list[tuple[str, str, str]]:
    """(group, ok|warn|bad, html) for the trader's card. Launcher-written text is escaped here."""
    tc, ca, s = t.get("trader") or {}, t.get("creator_activity") or {}, state_of(t, now)
    sym, rows = e(t["symbol"] or "?"), []
    tk = tc.get("ticker")
    if tk and tk["rank"] == 1:
        rows.append(("Original or copy", "ok", f"First token named ${sym}"
                     + (f". {plural(tk['later'], 'later token')} reused the ticker" if tk["later"] else "")))
    elif tk:
        f = tk["first"]
        gap = fmt_span((t["launched_at"] or 0) - (f["ts"] or 0))
        rows.append(("Original or copy", "ok" if s == "verified" else "warn",
                     f"{ordinal(tk['rank'])} token named ${sym}. The first, "
                     f"{tref({'token': f['token'], 'symbol': f['symbol'], 'orbio_agent': f.get('vault_id')}, '../', 'tok inline')}, "
                     f"launched {e(gap)} earlier{where_launched(f)}. "
                     + ("The project's own channels confirm this one." if s == "verified" else "Check which one the project claims.")))
    dev = tc.get("dev") or {}
    if dev:
        n = dev["launches_week"]
        rows.append(("Creator", "ok" if n == 0 else "warn" if n < 3 else "bad",
                     "This wallet launched nothing else within a week of this one" if n == 0
                     else f"This wallet launched {plural(n, 'other token')} within a week of this one"))
        if dev["other_launches"]:
            n_o, walked, fakes = dev["other_launches"], dev["walked_away"], dev["impersonators"]
            launches = f"{n_o} other launch{'' if n_o == 1 else 'es'}"
            extra = ([f"{dev['graduated']} graduated"] if dev["graduated"] else []) +                 ([f"{fakes} {'was an impersonator' if fakes == 1 else 'were impersonators'}"] if fakes else [])
            rows.append(("Creator", "bad" if walked or fakes else "ok",
                         (f"It sold out of {walked} of its {launches}" if walked else f"It hasn't sold out of any of its {launches}")
                         + ("; " + ", ".join(extra) if extra else "")))
        if dev.get("first_seen"):
            age = (t["launched_at"] or now) - dev["first_seen"]
            when_ = dt.datetime.fromtimestamp(dev["first_seen"], dt.timezone.utc).strftime("%d %b %Y")
            rows.append(("Creator", "ok" if age >= 90 * DAY else "warn",
                         f"Wallet active since {when_}" if age >= 90 * DAY else f"New wallet: first active {when_}"))
    if ca.get("bought"):
        sp = ca.get("sold_pct") or 0
        rows.append(("Creator", "bad" if sp >= 50 else "warn" if sp else "ok",
                     f"Bought {num(ca['bought'])} at launch and has sold {sp}% of it"))
    elif ca:
        rows.append(("Creator", "ok", "Didn't buy at launch"))
    ln = tc.get("launch") or {}
    lb = ln.get("launch_block_buyers")
    if lb is not None:
        rows.append(("Launch", "bad" if lb >= 3 else "warn" if lb else "ok",
                     "Nobody bought in the launch block itself" if not lb
                     else f"{plural(lb, 'buyer')} in the launch block itself" + (": a bundled launch" if lb >= 3 else "")))
    if "snipers" in ln:
        n, pct, sold, bots_ = ln["snipers"], ln.get("sniped_pct") or 0, ln.get("snipers_sold") or 0, ln.get("sniper_bots") or 0
        rows.append(("Launch", "ok" if not n or pct < 10 else "warn" if pct < 25 else "bad",
                     f"Nobody bought in the first {ln.get('window_s', 10)} seconds" if not n else
                     f"{plural(n, 'wallet')} took {pct:g}% of the supply in the first {ln.get('window_s', 10)} seconds"
                     + (f" ({bots_} of them bots that buy most launches)" if bots_ else "")
                     + (f". {sold} had sold or moved it within 30 minutes" if sold else ". None had sold within 30 minutes")))
    top10 = next((f for f in t.get("facts") or [] if f["line"] == "top10"), None)
    if top10 and isinstance(top10.get("top10_share"), (int, float)):
        sh = top10["top10_share"]
        rows.append(("Holders", "ok" if sh < .3 else "warn" if sh < .5 else "bad",
                     f"Top 10 holders own {sh:.0%}" if sh >= .01 else "Top 10 holders own under 1%"))
    h = [(k, v) for k, v in (t.get("holders") or {}).items() if v is not None]
    if h:
        grew = len(h) < 2 or h[-1][1] >= h[0][1]
        label = {"30m": "30 min", "6h": "6 h", "24h": "a day", "7d": "a week"}
        rows.append(("Holders", "ok" if grew else "warn",
                     "Real holders: " + ", ".join(f"{v} after {label[k]}" for k, v in h)))
    rows += [("Warnings", "bad", e(FLAGS[f])) for f in t.get("flags") or [] if f in FLAGS and f not in COVERED_FLAGS]
    return rows


def trader_block(t: dict, now: int) -> str:
    rows = trader_rows(t, now)
    if not rows:
        return ""
    groups: dict[str, list] = {}
    for g, c, txt in rows:
        groups.setdefault(g, []).append((c, txt))
    body = "".join(f'<h3>{e(g)}</h3><ul class="checks">' + "".join(f'<li class="{c}">{txt}</li>' for c, txt in items) + "</ul>"
                   + (lineup(t) if g == "Original or copy" else "") for g, items in groups.items())
    return (f'<section class="safety trader"><h2>Trader’s card</h2>{body}<p class="muted small">From the chain alone: '
            "holder counts are real end buyers, not routers or bots, and bots are wallets that buy 10 or more launches a day."
            "</p></section>")


# How Orbio splits every agent's creator fees: its live terms (network.fee_terms, read each build), else this. Orbio
# changed the split on 2026-10-01 at 18:05 UTC for every agent, older ones included, although their records kept the
# old fee (feeBps 500). Fees collected before then split 50% staked, 45% AI balance, 5% Orbio.
FEE_SPLIT_NOW = {"staked": 50, "credit": 30, "balance": 10, "orbio": 10}
FEE_SPLIT_BEFORE = {"staked": 50, "credit": 0, "balance": 45, "orbio": 5}
FEE_CHANGED_AT = 1_790_877_930  # 2026-10-01 18:05:30 UTC
TERMS: dict = {}  # this build's live terms, set by build() and build_single()


def fee_terms() -> dict:
    return dict(TERMS) if TERMS and sum(TERMS.values()) == 100 else dict(FEE_SPLIT_NOW)


def fee_terms_text(t: dict) -> str:
    """The treasury's opening line: how this agent's fees split now, how they split before Orbio's change when it
    launched before it, and what it has collected."""
    tr, k = t.get("treasury") or {}, fee_terms()
    staked = "half" if k["staked"] == 50 else f'{k["staked"]:g}%'
    s = (f'Trading fees fund this agent: {staked} is staked as ORBIO and earns CREDIT, '
         + (f'{k["credit"]:g}% is minted to it as CREDIT, ' if k["credit"] else "")
         + f'{k["balance"]:g}% becomes a balance it can spend on models and tools, and {k["orbio"]:g}% goes to Orbio.')
    if 0 < (t.get("launched_at") or 0) < FEE_CHANGED_AT:
        b = FEE_SPLIT_BEFORE
        s += f' Until 1 October, {b["balance"]}% went to that balance and {b["orbio"]}% to Orbio, and none was minted as CREDIT.'
    if tr.get("fees_orbio"):
        s += f' It has collected {tr["fees_orbio"]:,.0f} ORBIO in fees so far.'
    return s


def treasury_block(t: dict) -> str:
    cells = [("Spendable balance", "trt_bal", "t-bal"), ("Staked", "trt_staked", "t-stake"), ("CREDIT earned", "trt_earned", "t-credit"),
             ("CREDIT activated", "trt_act", "t-credit"), ("Principal withdrawn", "trt_wd", "t-wd")]
    return f"""<section class="treasury"><h2>Agent treasury</h2><p class="sub">{e(fee_terms_text(t))}</p>
<div class="tiles">{"".join(tile(a, k, c) for a, k, c in cells)}</div></section>"""


def identities(t: dict) -> str:
    role = {"twitter": "X account", "website": "Website", "repo": "Code", "github": "Code"}
    rows = "".join(f"""<tr><td>{role.get(i["role"], e(i["role"]))}</td><td class="wrapcell">{link(i["url"])}</td>
<td><span class="lvl l-{e((i["binding"] or "unbound").replace("·", "-"))}">{e(i["binding"] or "not checked")}</span>
<span class="muted hide-sm"> {e(LEVEL_TEXT.get(i["binding"] or "", ""))}</span></td></tr>""" for i in t.get("identities") or [])
    if not rows:
        return ""
    return f"""<section><h2>Who it points at</h2><p class="sub">The accounts and sites in the token's own metadata, and whether each
one claims this token. Metadata is written by the launcher, so only the claim counts.</p>
<div class="scroll"><table class="list"><thead><tr><th>Kind</th><th>Link</th><th>Claims it?</th></tr></thead>
<tbody>{rows}</tbody></table></div></section>"""


def line_label(f: dict) -> str:
    label = LINES.get(f["line"], f["line"])
    if f["line"] == "output":  # one line, read from the site and from the posts separately
        label += " in its posts" if (f.get("source") or "").startswith("x:") else " on its site"
    return label


def fact_detail(f: dict) -> str:
    line = f["line"]
    if line == "site_up":
        return f'HTTP {e(f.get("status"))}' + (" (read rendered)" if f.get("via") == "web.scrape" else "")
    if line == "substantive":
        return f'{e(f.get("words"))} words' + (", looks like a template" if f.get("template") else "")
    if line == "surface":
        return ", ".join(link(u) for u in (f.get("surfaces") or [])[:3])
    if line == "repo":
        return link(f.get("repo") or "")
    if line == "commits":
        return f'{e(plural(f.get("commits_30d"), "commit"))} in 30 days'
    if line == "predates_launch":
        return f'created {e((f.get("created") or "")[:10])}, {e(f.get("commits_before_launch"))} commits before launch'
    if line == "contributors":
        return f'{e(plural(f.get("contributors_30d"), "contributor"))} in 30 days'
    if line == "x_account":
        return "not readable" if f.get("readable") is False else \
            f'@{e(f.get("handle"))}' + (f', {f["followers"]:,} followers' if isinstance(f.get("followers"), int) else "")
    if line == "x_age":
        return f'{e(f.get("age_days"))} days old'
    if line == "x_cadence":
        return f'active {e(f.get("weeks_active"))} of the last 4 weeks, {e(f.get("posts_28d"))} posts'
    if line == "creator_history":
        return f'first seen {e(f.get("first_seen") or "—")}'
    if line == "agent_active":
        return f'{e(plural(f.get("transfers_7d"), "transfer"))} in 7 days'
    if line == "credit_activated":
        return f'{e(plural(f.get("activations_7d"), "activation"))} in 7 days'
    if line == "top10":
        share = f.get("top10_share")
        return (f"top 10 hold {share:.0%} of supply" if isinstance(share, (int, float)) else "") + \
            (f', {f["holders"]} holders' if f.get("holders") is not None else "")
    if line == "output":
        h = (f.get("source") or "")[2:] if (f.get("source") or "").startswith("x:") else None
        items = []
        for it in (f.get("items") or [])[:5]:
            src = link(f"https://x.com/{h}/status/{it['id']}", "post") if h and it.get("id") else ""
            items.append(f'<li><b>{e(it.get("date"))}</b> {e(it.get("what"))} <q>{e(w.clip(it.get("quote") or "", 160))}</q> {src}</li>')
        return f'<ul class="outs">{"".join(items)}</ul>' if items else "none in the last 7 days"
    return ""


def evidence(t: dict) -> str:
    groups = []
    for key, label, mx in DIMS:
        fs = sorted((f for f in t["facts"] if f["dim"] == key), key=lambda f: -(f["points"] or 0))
        if not fs:
            continue
        rows = "".join(f"""<tr class="{'' if f['points'] * f['weight'] > 0 else 'zero'}"><td>{e(line_label(f))}</td>
<td class="num">{f["points"] * f["weight"]:g}<small>{'' if f['weight'] == 1 else f' ({f["points"]:g} × {f["weight"]:g})'}</small></td>
<td class="wrapcell">{fact_detail(f)}</td><td class="hide-sm muted">{when(f.get("at"))}</td></tr>""" for f in fs)
        groups.append(f"""<h3>{label} <span class="muted">{(t["scores"] or {}).get(key) or 0:g} of {mx}</span></h3>
<div class="scroll"><table class="list ev"><tbody>{rows}</tbody></table></div>""")
    return f"""<details class="evidence"><summary><h2>Evidence log</h2><span class="muted">{plural(len(t["facts"]), "check")},
each with what it found and when</span></summary><p class="sub">A line counts only if its source claims the token (the weight),
and it fades as it ages.</p>{"".join(groups)}</details>"""


# ----------------------------------------------------------------- method and API pages

def md(text: str) -> str:
    """Just enough Markdown for the spec: headings, paragraphs, lists, tables, code blocks, inline code,
    bold, italics and links."""
    def inline(s: str) -> str:
        codes: list[str] = []
        s = re.sub(r"`([^`]+)`", lambda m: codes.append(m.group(1)) or f"\x00{len(codes) - 1}\x00", s)
        s = e(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
        return re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{e(codes[int(m.group(1))])}</code>", s)

    out: list[str] = []
    lines = text.splitlines()
    i = 0
    para: list[str] = []

    def flush() -> None:
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            flush()
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            out.append(f"<pre><code>{e(chr(10).join(lines[i + 1:j]))}</code></pre>")
            i = j + 1
            continue
        m = re.match(r"(#{1,4})\s+(.*)", ln)
        if m:
            flush()
            level, title = len(m.group(1)), m.group(2)
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            out.append(f'<h{level} id="{slug}">{inline(title)}</h{level}>')
        elif ln.strip() == "---":
            flush()
            out.append("<hr>")
        elif ln.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            head, body = rows[0], [r for r in rows[1:] if not all(re.fullmatch(r":?-+:?", c) for c in r)]
            out.append('<div class="scroll"><table class="list md"><thead><tr>' + "".join(f"<th>{inline(c)}</th>" for c in head)
                       + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>"
                                                          for r in body) + "</tbody></table></div>")
            continue
        elif re.match(r"(- |\d+\. )", ln):
            flush()
            ordered = bool(re.match(r"\d+\. ", ln))
            items: list[str] = []
            while i < len(lines) and (re.match(r"(- |\d+\. )", lines[i]) or (items and lines[i].startswith("  "))):
                if lines[i].startswith("  "):
                    items[-1] += " " + lines[i].strip()
                else:
                    items.append(re.sub(r"^(- |\d+\. )", "", lines[i]))
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{inline(x)}</li>" for x in items) + f"</{tag}>")
            continue
        elif not ln.strip():
            flush()
        else:
            para.append(ln.strip())
        i += 1
    flush()
    return "\n".join(out)


# ----------------------------------------------------------------- case files
#
# A case file is a deep dive: one investigation, written from chain data that was re-read from a public node, every
# claim with its receipt. Each is one JSON file in cases/ (kept out of the public repo: only its page is published).
# A draft is built only on the dev server, so nothing reaches the live site until its status says "published".

CASES_DIR = ROOT / "cases"
CASES: list[dict] = []  # the case files this build shows, newest first (set by build())


def load_cases(drafts: bool | None = None) -> list[dict]:
    """cases/*.json, newest first: the published ones, and drafts too when DOSSIER_DRAFTS=1 (tools/dev_site.sh)."""
    if drafts is None:
        drafts = os.environ.get("DOSSIER_DRAFTS") == "1"
    out = []
    for f in sorted(CASES_DIR.glob("*.json")):
        try:
            c = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(c, dict) or not re.fullmatch(r"[a-z0-9-]{3,80}", str(c.get("slug", ""))):
            continue  # the slug names a file: nothing else may get through
        if c.get("status") == "published" or (drafts and c.get("status") == "draft"):
            out.append(c)
    return sorted(out, key=lambda c: -int(c.get("no") or 0))


def utc(ts: int | None, secs: bool = False) -> str:
    if not ts:
        return "—"
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return f"{d.day} {d:%b %H:%M}" + (f":{d:%S}" if secs else "") + " UTC"


def case_date(s: str) -> str:
    try:
        d = dt.date.fromisoformat(s)
    except (TypeError, ValueError):
        return e(s)
    return f"{d.day} {d:%b %Y}"


def case_link(label: str, url: str, up: str) -> str:
    """A receipt (any explorer or X link) or a page of this site (a relative path)."""
    if re.match(r"https?://", url or ""):
        return link(url, f"{label} ↗", "rcpt")
    if re.fullmatch(r"(t|case)/[\w.-]+\.html", url or ""):
        return f'<a class="rcpt" href="{up}{e(url)}">{e(label)} →</a>'
    return e(label)


def case_tok(r: dict, by: dict, now: int, up: str) -> tuple[str, str]:
    """A launch named on a case page: its ticker and agent number, linked to its file (or the explorer), and its verdict
    now, which the page keeps current."""
    t = by.get(r.get("token"))
    s = state_of(t, now) if t else (r.get("state") if r.get("state") in STATE else "checking")  # off the site: as recorded
    inner = f'<b>${e(r.get("symbol") or "?")}</b>' + (f' <span class="no">#{e(r["vault_id"])}</span>' if r.get("vault_id") else "")
    if t:
        return f'<a class="tok" href="{up}t/{e(r["token"])}.html">{inner}</a>', s
    return f'<a class="tok" href="{EXPLORER}/token/{e(r.get("token"))}" rel="nofollow noopener" target="_blank">{inner}</a>', s


def beads(rows: list[dict], by: dict, now: int, up: str) -> str:
    """Launches in order as beads on a string, coloured by verdict: one look says how many and how many are fake."""
    out = []
    for r in rows:
        t = by.get(r.get("token"))
        s = state_of(t, now) if t else (r.get("state") if r.get("state") in STATE else "checking")
        href = f'{up}t/{e(r["token"])}.html' if t else f'{EXPLORER}/token/{e(r.get("token"))}'
        where = f'#{r["vault_id"]}' if r.get("vault_id") else "Pons"  # a launch straight on Pons has no agent number
        tip = f'${r.get("symbol") or "?"} {where} · {STATE[s]} · launched {utc(r.get("ts"))}'
        out.append(f'<li class="v-{s}"><a href="{href}" title="{e(tip)}"><i></i><b>${e(r.get("symbol") or "?")}</b>'
                   f'<span>{e(where)}</span></a></li>')
    counts = {s: sum(1 for r in rows if (state_of(by[r["token"]], now) if r.get("token") in by else
                                        (r.get("state") if r.get("state") in STATE else "checking")) == s) for s in STATE}
    legend = "".join(f'<span class="v-{s}"><i></i>{STATE[s]} · {n}</span>' for s, n in counts.items() if n)
    return f'<ol class="beads">{"".join(out)}</ol><p class="beadkey">{legend}</p>'


def case_tokens(rows: list[dict], by: dict, now: int, up: str) -> str:
    trs = []
    for i, r in enumerate(rows, 1):
        tok, s = case_tok(r, by, now, up)
        h = r.get("names") or ""
        names = (f'<a href="https://x.com/{e(h[1:])}" rel="nofollow noopener" target="_blank">{e(h)}</a>'
                 if re.fullmatch(r"@\w{1,15}", h) else "—")
        tx = " " + link(f"{EXPLORER}/tx/{r['tx']}", "Launch ↗", "rcpt") if r.get("tx") else ""
        trs.append(f'<tr data-t="{e(r.get("token"))}"><td class="num">{i}</td><td>{tok}</td>'
                   f'<td class="hide-sm muted">{utc(r.get("ts"))}{tx}</td><td>{names}</td>'
                   f'<td><span data-l="chip">{chip(s)}</span></td></tr>')
    return ('<div class="scroll"><table class="list"><thead><tr><th class="num">#</th><th>Token</th><th class="hide-sm">Launched</th>'
            f'<th>Links to</th><th>Verdict now</th></tr></thead><tbody>{"".join(trs)}</tbody></table></div>')


def trail(steps: list[dict], by: dict, now: int, up: str, compact: bool = False) -> str:
    """The money trail: wallets, and the transfers between them, top to bottom, every step with its receipt."""
    out = []
    for st in steps:
        if st.get("type") == "edge":
            moves = "".join(f'<li><b>{e(m.get("amount"))}</b><span class="muted">{utc(m.get("ts"), secs=True)}</span>'
                            + (link(f'{EXPLORER}/tx/{m["tx"]}', "Receipt ↗", "rcpt") if m.get("tx") else "") + "</li>"
                            for m in st.get("moves") or [])
            note = f'<p class="tr-note">{e(st["note"])}</p>' if st.get("note") else ""
            out.append(f'<li class="tr-edge"><ul class="tr-moves">{moves}</ul>{note}</li>')
            continue
        a, launches, state = st.get("addr") or "", [], ""
        for lch in st.get("launches") or []:
            if lch.get("vault_id"):
                tok, s = case_tok(lch, by, now, up)
                state = state or s
                what = f"Launched {tok} {chip(s)}"
            else:
                what = "Launched " + link(f'{EXPLORER}/token/{lch.get("token")}', "a token straight on Pons")
            rc = " " + link(f'{EXPLORER}/tx/{lch["tx"]}', "Receipt ↗", "rcpt") if lch.get("tx") else ""
            launches.append(f'<p class="tr-launch">{what} <span class="muted">{utc(lch.get("ts"))}</span>{rc}</p>')
        note = f'<p class="tr-note">{e(st["note"])}</p>' if st.get("note") else ""
        cls = "tr-node" + (f" has-launch v-{state}" if state else "")
        out.append(f'<li class="{cls}"><div class="tr-who"><span class="tr-role">{e(st.get("role"))}</span>'
                   f'<a class="tr-addr" href="{EXPLORER}/address/{e(a)}" rel="nofollow noopener" target="_blank"><code>{e(short(a))}</code></a>'
                   f'<button type="button" class="mini copy" data-copy="{e(a)}">Copy</button></div>{note}{"".join(launches)}</li>')
    return f'<ol class="trail{" compact" if compact else ""}">{"".join(out)}</ol>'


def case_no(c: dict) -> str:
    return f'Case file {int(c.get("no") or 0):03d}'


# A finding's figure: the gist at a glance above its full text (case JSON "fig"). Three kinds, all HTML and CSS, so they
# read at phone width: "lanes" (a timeline, one row per actor), "split" (a proportional bar) and "flow" (boxes and
# arrows, stacked on a phone). Colours come by role, from the theme's tokens: x and y for the two sides of a story, z for
# a third, bad, ok, hi (the focal part), mid and lo.
FIG_ROLES = ("x", "y", "z", "bad", "ok", "hi", "mid", "lo")


def fig_role(r) -> str:
    return f"r-{r}" if r in FIG_ROLES else "r-lo"


def fig_lanes(f: dict) -> str:
    t0, t1 = int(f["from"]), int(f["to"])
    pct = lambda t: max(0.0, min(100.0, (int(t) - t0) / max(1, t1 - t0) * 100))  # noqa: E731
    edge = lambda x: " at-start" if x < 6 else " at-end" if x > 94 else ""  # a label at either end stays inside  # noqa: E731
    rules = "".join(f'<span class="ln-rule" style="left:{pct(r["ts"]):.2f}%"></span>' for r in f.get("rules") or [])
    rows = []
    for ln in f.get("lanes") or []:
        track = [rules]
        for s in ln.get("spans") or []:
            a, b = pct(s["from"]), pct(s["to"])
            track.append(f'<span class="ln-span" style="left:{a:.2f}%;width:{max(.8, b - a):.2f}%" title="{e(s.get("title", ""))}">'
                         f'{e(s.get("label", ""))}</span>')
        for p in ln.get("points") or []:
            role = f' {fig_role(p["role"])}' if p.get("role") else ""
            track.append(f'<span class="ln-pt{role}" style="left:{pct(p["ts"]):.2f}%;--s:{float(p.get("s", .5)):.2f}" '
                         f'title="{e(p.get("title", ""))}"></span>')
        for m in ln.get("marks") or []:
            track.append(f'<span class="ln-mark{edge(pct(m["ts"]))}" style="left:{pct(m["ts"]):.2f}%" '
                         f'title="{e(m.get("title", ""))}"><em>{e(m.get("label", ""))}</em></span>')
        marked = " has-marks" if ln.get("marks") else ""
        rows.append(f'<div class="lane {fig_role(ln.get("role"))}{marked}"><div class="ln-label"><b>{e(ln.get("label"))}</b>'
                    f'<span>{e(ln.get("sub", ""))}</span></div><div class="ln-track">{"".join(track)}</div></div>')
    tk = f.get("ticks") or []
    minor = lambda i: " minor" if len(tk) > 4 and i % 2 else ""  # a narrow figure shows every other one  # noqa: E731
    ticks = "".join(f'<span class="{(edge(pct(t["ts"])) + minor(i)).strip()}" style="left:{pct(t["ts"]):.2f}%">'
                    f'{e(t["label"])}</span>' for i, t in enumerate(tk))
    ticks += "".join(f'<span class="ln-rule-label{edge(pct(r["ts"]))}" style="left:{pct(r["ts"]):.2f}%">'
                     f'{e(r.get("label", ""))}</span>' for r in f.get("rules") or [])
    return f'<div class="lanes">{"".join(rows)}<div class="lane ln-axis"><div></div><div class="ln-track">{ticks}</div></div></div>'


def fig_split(f: dict) -> str:
    out = []
    for row in f.get("rows") or []:
        parts = row.get("parts") or []
        total = sum(float(p["value"]) for p in parts) or 1.0
        unit = row.get("unit", "")
        fmt = lambda v: (f"{v:g}" if float(v) == int(float(v)) else f"{float(v):,.2f}") + unit  # noqa: E731
        segs = "".join(f'<span class="sp-seg {fig_role(p.get("role"))}{" narrow" if float(p["value"]) / total < .12 else ""}" '
                       f'style="width:{float(p["value"]) / total * 100:.2f}%" title="{e(p.get("label"))}: {e(fmt(p["value"]))}">'
                       f'<b>{e(fmt(p["value"]))}</b></span>' for p in parts)
        key = "".join(f'<li class="{fig_role(p.get("role"))}"><i></i>{e(p.get("label"))} <b>{e(fmt(p["value"]))}</b>'
                      + (f' <span class="muted">{e(p["note"])}</span>' if p.get("note") else "") + "</li>" for p in parts)
        out.append(f'<div class="sp-row"><p class="sp-label">{e(row.get("label"))}</p><div class="sp-bar">{segs}</div>'
                   f'<ul class="sp-key">{key}</ul></div>')
    for n in f.get("notes") or []:  # a before/after: "Stake lock: 10 days after launch → none"
        out.append(f'<p class="fig-change"><b>{e(n.get("k"))}</b> <s>{e(n.get("was"))}</s> <span aria-hidden="true">→</span> '
                   f'<strong>{e(n.get("now"))}</strong></p>')
    return "".join(out)


def fig_flow(f: dict) -> str:
    out = []
    for st in f.get("steps") or []:
        if "arrow" in st:
            out.append(f'<div class="fl-arrow"><span>{e(st["arrow"])}</span></div>')
            continue
        nodes = "".join(f'<div class="fl-node {fig_role(n.get("role"))}{" guess" if n.get("inferred") else ""}">'
                        f'<b>{e(n.get("label"))}</b>' + (f'<span>{e(n["sub"])}</span>' if n.get("sub") else "")
                        + (f'<em>{e(n["tag"])}</em>' if n.get("tag") else "") + "</div>" for n in st.get("nodes") or [])
        out.append(f'<div class="fl-col">{nodes}</div>')
    return f'<div class="flow">{"".join(out)}</div>'


FIGS = {"lanes": fig_lanes, "split": fig_split, "flow": fig_flow}


def case_fig(f) -> str:
    """A finding's figure, or nothing when it's missing, malformed or of a kind this build doesn't know: a figure never
    stops the page (or the site) from building."""
    if not isinstance(f, dict) or f.get("type") not in FIGS:
        return ""
    try:
        body = FIGS[f["type"]](f)
    except (KeyError, TypeError, ValueError, AttributeError):
        return ""
    cap = f'<figcaption>{e(f["caption"])}</figcaption>' if f.get("caption") else ""
    return f'<figure class="fig fig-{f["type"]}">{body}{cap}</figure>'


def case_page(c: dict, feed: dict) -> str:
    now, up = feed["generated_at"], "../"
    by = {t["token"]: t for t in feed["tokens"]}
    draft = ('<p class="draft">Draft: not published. Only the dev server builds this page.</p>'
             if c.get("status") != "published" else "")
    tiles = "".join(f"<div><dd>{e(v)}</dd><dt>{e(k)}</dt></div>" for v, k in c.get("tiles") or [])
    findings = "".join(
        f'<li><h3>{e(f.get("h"))}</h3>{case_fig(f.get("fig"))}<p>{e(f.get("p"))}</p>'
        + (f'<p class="rc">{"".join(case_link(lb, u, up) for lb, u in f["receipts"])}</p>' if f.get("receipts") else "")
        + "</li>" for f in c.get("findings") or [])
    parts = [f"""<section class="casehead"><p class="eyebrow">{case_no(c)} · {case_date(c.get("date"))}
<span class="pv">Money trail · preview</span></p><h1>{e(c.get("title"))}</h1><p class="lede">{e(c.get("dek"))}</p>{draft}
<dl class="tally">{tiles}</dl></section>""",
             f'<section><h2>What we found</h2><ol class="findings">{findings}</ol></section>']
    if (b := c.get("beads")) and b.get("rows"):
        parts.append(f'<section><h2>{e(b.get("title"))}</h2>{beads(b["rows"], by, now, up)}</section>')
    if (tr := c.get("trail")) and tr.get("steps"):
        body = trail(tr["steps"], by, now, up, compact=bool(tr.get("collapsed")))
        if tr.get("collapsed"):
            body = f'<details class="case-more"><summary>Show every wallet and transfer</summary>{body}</details>'
        parts.append(f'<section><h2>{e(tr.get("title"))}</h2>{body}</section>')
    if (tk := c.get("tokens")) and tk.get("rows"):
        parts.append(f'<section><h2>{e(tk.get("title"))}</h2>{case_tokens(tk["rows"], by, now, up)}</section>')
    if c.get("shows") or c.get("doesnt"):
        li = lambda xs: "".join(f"<li>{e(x)}</li>" for x in xs or [])  # noqa: E731
        parts.append(f'<section><h2>What this shows, and what it doesn\'t</h2><div class="how two">'
                     f'<div><h3>It shows</h3><ul>{li(c.get("shows"))}</ul></div>'
                     f'<div><h3>It doesn\'t show</h3><ul>{li(c.get("doesnt"))}</ul></div></div></section>')
    if c.get("related"):
        parts.append('<section><h2>Related</h2>' + "".join(f'<p>{e(t)} {case_link("Read it", u, up)}</p>'
                                                          for t, u in c["related"]) + "</section>")
    if c.get("method"):
        parts.append('<section class="method"><h2>How we checked</h2>' + "".join(f"<p>{e(p)}</p>" for p in c["method"])
                     + f'<p class="muted small">Money-trail analysis is in preview here. It is coming to every file on {NAME}. '
                       f'<a href="{up}cases.html">All case files →</a></p></section>')
    return f'<div class="stack casefile">{"".join(parts)}</div>'


def cases_page(cases: list[dict], feed: dict) -> str:
    """The case files as an investigator's folder: a manila cover that opens on an index card, and each case a typed
    document whose page turns over to its findings. The faces come in reading order (cover, index, then each case's
    document and findings, then the end of the file); the page script binds them into turning leaves, two pages a
    spread on a wide screen and one at a time on a phone. Without it, they simply stack."""
    now, by, n = feed["generated_at"], {t["token"]: t for t in feed["tokens"]}, len(cases)
    opened = min((c.get("date") or "" for c in cases), default="")
    faces = [f"""<section class="cf-face cf-cover" aria-label="The case files folder"><span class="cf-tab">{NAME}</span>
<p class="cf-stencil">Case<br>files</p>
<dl class="cf-label"><div><dt>Subject</dt><dd>Orbio launchpad · Robinhood Chain</dd></div>
<div><dt>Contents</dt><dd>{plural(n, "file")}</dd></div><div><dt>Opened</dt><dd>{case_date(opened)}</dd></div></dl>
<span class="cf-stamp">Follow the money</span><button type="button" class="cf-open">Open the file</button></section>"""]
    rows = "".join(f'<li><a href="#cf-doc-{i}" data-goto="{i}"><span class="no">{int(c.get("no") or 0):03d}</span>'
                   f'<span class="t">{e(c.get("title"))}</span><span class="d">{case_date(c.get("date"))}</span></a></li>'
                   for i, c in enumerate(cases, 1))
    faces.append(f'<section class="cf-face cf-inside" aria-label="Index of files"><div class="cf-card"><p class="cf-k">Index of files</p>'
                 f'<ol class="cf-index">{rows}</ol><p class="cf-hint">Turn the pages, or pick a file.</p></div></section>')
    for i, c in enumerate(cases, 1):
        no, href = case_no(c), f'case/{e(c["slug"])}.html'
        stamp = '<span class="cf-mark draft">Draft</span>' if c.get("status") != "published" else '<span class="cf-mark">Filed</span>'
        tiles = "".join(f"<div><dd>{e(v)}</dd><dt>{e(k)}</dt></div>" for v, k in (c.get("tiles") or [])[:3])
        exhibit = ""
        if (b := c.get("beads")) and b.get("rows"):
            dots = "".join(f'<li class="v-{state_of(by[r["token"]], now) if r.get("token") in by else (r.get("state") if r.get("state") in STATE else "checking")}" '
                           f'title="${e(r.get("symbol") or "?")} #{e(r.get("vault_id"))}"></li>' for r in b["rows"])
            exhibit = f'<div class="cf-exhibit"><p class="cf-k">Exhibit A · the launches, in order</p><ol class="cf-dots">{dots}</ol></div>'
        faces.append(f"""<section class="cf-face cf-doc" id="cf-doc-{i}" aria-label="{e(no)}"><div class="cf-paper">
<p class="cf-head"><span>{e(no)}</span><span>{case_date(c.get("date"))}</span></p>{stamp}
<h2>{e(c.get("title"))}</h2><p class="cf-dek">{e(c.get("dek"))}</p><dl class="cf-tiles">{tiles}</dl>{exhibit}
<a class="cf-go" href="{href}">Open the case file →</a><span class="cf-pg">{2 * i - 1}</span></div></section>""")
        finds = "".join(f"<li>{e(f.get('h'))}</li>" for f in c.get("findings") or [])
        faces.append(f"""<section class="cf-face cf-doc cf-findings" aria-label="{e(no)}: findings"><div class="cf-paper">
<p class="cf-head"><span>{e(no)} · Findings</span><span>{case_date(c.get("date"))}</span></p>
<ol class="cf-find">{finds}</ol><a class="cf-go" href="{href}">Every finding, with its receipts →</a>
<span class="cf-pg">{2 * i}</span></div></section>""")
    faces.append(f'<section class="cf-face cf-end" aria-label="End of file"><p class="cf-stencil small">End of file</p>'
                 f'<p>New cases are added as {NAME} follows the money.</p><a href="method.html">How {NAME} checks a token →</a></section>')
    return f"""<section class="page-head"><p class="eyebrow">Money trail · preview</p><h1>The investigator's files</h1>
<p class="sub">The deep dives behind the verdicts. Open the folder: each document follows the money on-chain, and every claim
in it links its receipt.</p></section>
<div class="casefolder" data-n="{n}">{"".join(faces)}</div>
<div class="cf-controls" hidden><button type="button" class="cf-prev" aria-label="Previous page">← Back</button>
<span class="cf-count" aria-live="polite"></span><button type="button" class="cf-next" aria-label="Next page">Turn →</button></div>"""


def method_page() -> str:
    spec = SPEC.read_text("utf-8") if SPEC.exists() else "# Method\n\nThe methodology is being written."
    spec = re.split(r"^## 11\. ", spec, flags=re.M)[0]  # §11 maps the method onto this codebase: internal
    return f'<article class="prose">{md(spec)}</article>'


def api_page(feed: dict) -> str:
    sample = feed["tokens"][0]["token"] if feed["tokens"] else "0x…"
    return f"""<article class="prose"><h1>API</h1><p>Everything on {NAME} is also plain JSON, free and without a key. It's rebuilt
from the index every few minutes, and every score line carries its source and time.</p>
<h2>Endpoints</h2><div class="scroll"><table class="list md"><thead><tr><th>Path</th><th>What</th></tr></thead><tbody>
<tr><td><a href="api/v1/feed.json"><code>api/v1/feed.json</code></a></td><td>Every token on file: verdict with receipts, status,
scores, market and treasury, related tokens, identities and the evidence behind each score line.</td></tr>
<tr><td><a href="api/v1/tokens/{sample}.json"><code>api/v1/tokens/&lt;address&gt;.json</code></a></td><td>One token, the same shape.
Addresses are lowercase.</td></tr>
<tr><td><a href="api/v1/summary.json"><code>api/v1/summary.json</code></a></td><td>A small list for lookups: address, ticker, name,
Orbio agent id, verdict and status.</td></tr>
<tr><td><a href="api/v1/live.json"><code>api/v1/live.json</code></a></td><td>The numbers that move, for every token, refreshed every minute:
market cap, price, curve, 24h volume and trades, holders, change and a sparkline, the agent's treasury, and its current verdict.
Short keys, to keep it small.</td></tr></tbody></table></div>
<h2>Fields worth knowing</h2><ul>
<li><code>verdict.verdict</code> is <code>verified</code>, <code>scam</code> or <code>unverified</code>, and
<code>verdict.receipts</code> links the post or page it rests on.</li>
<li><code>related.claimed</code> lists the tokens the same X account or site claims: for an impersonator, that's the real token.
<code>related.impersonators</code> lists the fakes around a real one.</li>
<li><code>market</code> and <code>treasury</code> come from Orbio's public API (Orbio agents only): price, market cap,
stake, CREDIT and the agent's spendable balance. <code>market.curve_pct</code> is read from the chain: the ORBIO the bonding
curve holds, as a share of the amount it graduates at.</li>
<li><code>timeline.verified_at</code> is when {NAME} first saw an official channel claim the token.</li>
<li><code>status</code> is <code>PROVEN</code>, <code>LIVE</code>, <code>BUILDING</code>, <code>UNPROVEN</code>, <code>ABANDONED</code> or
<code>SCAM</code> (<a href="method.html#5-status">rules</a>). <code>facts[]</code> are the score lines.</li></ul>
</article>"""


# ----------------------------------------------------------------- $DOSSIER: the token page

TOKEN_DOC = ROOT / "docs" / "token.md"
def fee_split_parts() -> tuple:
    """The token page's split bar, from the terms in force."""
    k = fee_terms()
    return tuple(p for p in ((k["staked"], "Staked as ORBIO", "earns CREDIT rewards; never withdrawn", "c-stake"),
                             (k["credit"], "Minted as CREDIT", "compute first, then buybacks", "c-credit"),
                             (k["balance"], "Dossier's compute", "AI balance: pays for every check", "c-compute"),
                             (k["orbio"], "Orbio's treasury", "the launchpad's share", "c-orbio")) if p[0])
PASS_SPLIT = ((70, "Burned", "sent to the dead address in batches", "c-burn"),
              (30, "Treasury", "runs and grows Dossier", "c-treasury"))
FLYWHEEL = (("Right, fast verdicts", "Each verdict links its receipt, often within minutes of launch."),
            ("Hunters rely on it", "They check a launch before they buy. The keenest buy a pass."),
            ("Fees and passes", "Trading pays creator fees. Passes are paid in $DOSSIER."),
            ("More checks, less supply", "Fees buy faster, wider checks. Passes and CREDIT buy back and burn $DOSSIER."))
BURN_SHARE = 0.7


def split_bar(parts, label: str) -> str:
    bar = "".join(f'<span class="{c}" style="width:{p:g}%" title="{p:g}%: {e(name)}"></span>' for p, name, _, c in parts)
    legend = "".join(f'<li><i class="{c}"></i><b>{p:g}%</b> {e(name)}<span>{e(note)}</span></li>' for p, name, note, c in parts)
    return f'<div class="split" role="img" aria-label="{e(label)}">{bar}</div><ul class="split-legend">{legend}</ul>'


def fees_collected(d: dict, feed: dict) -> str:
    """Under the fee split: what $DOSSIER's trading has actually paid so far, and where each part went."""
    tr = d.get("treasury") or {}
    if not d.get("launched") or not tr.get("fees_orbio"):
        return ""
    px = (feed.get("network") or {}).get("orbio_usd")
    worth = f" (about {usd(tr['fees_orbio'] * px)})" if px else ""
    orbio = lambda k: f"{tr.get(k) or 0:,.0f} ORBIO"  # noqa: E731 -- whole numbers, one style through the sentence
    minted = f' and {tr["credit_minted"]:,.2f} CREDIT' if tr.get("credit_minted") else ""
    return (f'<p class="collected">So far $DOSSIER\'s trading has paid <b>{orbio("fees_orbio")}</b> in creator fees{e(worth)}: '
            f'{orbio("staked_orbio")} staked, {orbio("sold_orbio")} sold for {e(usd(tr.get("balance_usdg") or 0))} of compute'
            f'{minted}, and {orbio("orbio_cut_orbio")} to Orbio.</p>')


def official_contract(d: dict) -> str:
    if not d.get("launched"):
        return ('<div class="official"><p class="eyebrow">Official contract</p><p class="big">Not launched yet.</p>'
                "<p>When $DOSSIER launches, its contract appears here. Until then, every token called $DOSSIER is an "
                "impersonator, and afterwards so is any address that doesn't match this one.</p></div>")
    t = d["token"]
    return (f'<div class="official live"><p class="eyebrow">Official contract · Orbio agent #{e(d.get("agent"))}</p>'
            f'<p class="ca"><code>{e(t)}</code><button type="button" class="copy" data-copy="{e(t)}">Copy</button></p>'
            f'<p><a href="t/{e(t)}.html">Its Dossier file</a> · {link(f"https://www.orbio.so/launchpad/{t}", "Trade on Orbio")} · '
            f'{link(f"{EXPLORER}/token/{t}", "Explorer")}</p><p class="muted small">Any other address using the name is an '
            "impersonator.</p></div>")


def flywheel() -> str:
    steps = "".join(f"<li><b>{e(title)}</b><p>{e(text)}</p></li>" for title, text in FLYWHEEL)
    return f'<ol class="flywheel">{steps}</ol><p class="loop">↻ and back to 1: more checks make better verdicts</p>'


def ledger(d: dict, feed: dict) -> str:
    days = d.get("compute_by_day") or []
    week = days[-7:]
    avg = (d.get("compute_7d") or 0) / len(week) if week else 0
    top = max((c for _, c in days), default=0) or 1
    bars = "".join(f'<span style="height:{max(3, 100 * c / top):.0f}%" title="{e(day)}: {c:.2f} CREDIT"></span>' for day, c in days)
    axis = f'<div class="bars-axis"><span>{e(days[0][0][5:])}</span><span>{e(days[-1][0][5:])}</span></div>' if days else ""
    rows = [("Checks, last 7 days", f'{d.get("compute_7d") or 0:.2f} CREDIT'), ("Per day, on average", f"{avg:.2f} CREDIT")]
    if d.get("launched"):
        tr = d.get("treasury") or {}
        tok = next((t for t in feed["tokens"] if t["token"] == d["token"]), {})
        px = (tok.get("market") or {}).get("price_usd")
        left = d.get("key_available")  # what Dossier's key (the token's account) can still spend
        runway = "—" if left is None or avg <= 0 else f"{left / avg:.1f} days" if left / avg < 10 else f"{left / avg:.0f} days"
        rev, burned = d.get("pass_revenue"), d.get("burned")
        rev_s = "—" if rev is None else num(rev, "$DOSSIER") + (f" <small>≈ {usd(rev * px)}</small>" if px and rev else "")
        rows += [("Creator fees collected", num(tr.get("fees_orbio") or 0, "ORBIO")),
                 ("AI balance credited by fees", usd(tr.get("balance_usdg"))), ("AI balance left for checks", usd(left)),
                 ("Compute runway, estimated", runway), ("ORBIO staked", num(tr.get("staked_orbio"), "ORBIO")),
                 ("CREDIT minted from fees", f'{tr.get("credit_minted") or 0:.2f}'),
                 ("CREDIT earned by the stake", f'{(tr.get("credit_owed") or 0) + (tr.get("credit_claimed") or 0):.2f}'),
                 ("Stake withdrawn (policy: never)", num(tr.get("withdrawn_orbio") or 0, "ORBIO")),
                 ("Pass revenue", rev_s), ("Burned (dead address)", "—" if burned is None else num(burned, "$DOSSIER"))]
        due = (rev or 0) * BURN_SHARE - (burned or 0)
        check = (f'<p class="behind">Burns are behind: {e(num(due, "$DOSSIER"))} of pass revenue still to burn.</p>' if rev and due > 0
                 else '<p class="muted small">Burns cover at least 70% of pass revenue. Buyback burns count on top.</p>' if rev else "")
    else:
        rows += [(k, "—") for k in ("AI balance credited by fees", "Compute runway", "ORBIO staked", "CREDIT earned by the stake",
                                    "Pass revenue", "Burned (dead address)")]
        check = '<p class="muted small">The token\'s numbers start at launch. What the checks cost is live now.</p>'
    cells = "".join(f"<div><dt>{e(k)}</dt><dd>{v}</dd></div>" for k, v in rows)
    return (f'<div class="bars" role="img" aria-label="CREDIT spent on checks per day, last {len(days)} days">{bars}</div>{axis}'
            f'<dl class="keynums small">{cells}</dl>{check}<p class="muted small">Updated every few minutes from Orbio\'s API, '
            "the chain and the index's own records. A CREDIT is about a dollar of compute.</p>")


def strategy_page(feed: dict) -> str:
    d = feed.get("dossier") or {}
    text = TOKEN_DOC.read_text("utf-8") if TOKEN_DOC.exists() else "# $DOSSIER\n\nComing soon."
    k = fee_terms()
    blocks = {"contract": official_contract(d),
              "fees": split_bar(fee_split_parts(), f'Creator fees: {k["staked"]:g}% stake, {k["credit"]:g}% CREDIT, '
                                f'{k["balance"]:g}% compute, {k["orbio"]:g}% Orbio') + fees_collected(d, feed),
              "passes": split_bar(PASS_SPLIT, "Pass revenue: 70% burned, 30% treasury"), "flywheel": flywheel(),
              "ledger": ledger(d, feed)}
    body = re.sub(r"<p>@@(\w+)@@</p>", lambda m: blocks.get(m.group(1), ""), md(text))
    body = re.sub(r"^(<h1 [^>]*>.*?</h1>)", rf'<div class="token-hero"><img src="assets/logo.png?v={file_version(LOGO)}" alt="The $DOSSIER seal" '
                  r'width="104" height="104">\1</div>', body, count=1)
    return f'<article class="prose">{body}</article>'


# ----------------------------------------------------------------- assets

# dark for everyone by default (the OS preference doesn't change it); light when the viewer picks it with the switch in
# the header, which sets data-theme="light" on <html> before the page paints (THEME_BOOT)
CSS = """
:root{--display:"Bricolage Grotesque","Instrument Sans",system-ui,sans-serif;--sans:"Instrument Sans",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
--mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;@@DARK@@}
:root[data-theme="light"]{color-scheme:light;--bg:#e8ebf0;--sheet:#fff;--sheet-2:#f4f6f9;--ink:#0d1522;--ink-2:#39455a;--muted:#657084;--rule:#d5dae2;--rule-2:#b8c0cc;
--accent:#2d49d8;--accent-bg:#e5e9fc;--scrim:rgba(232,235,240,.9);
--glass:rgba(255,255,255,.52);--glass-hi:rgba(255,255,255,.9);--glass-spec:rgba(255,255,255,.95);--rim-hi:#fff;--rim-lo:rgba(13,21,34,.1);
--ok-glow:rgba(11,122,75,.18);--bad-glow:rgba(204,37,57,.16);--accent-glow:rgba(45,73,216,.18);
--amb-a:rgba(45,73,216,.16);--amb-b:rgba(11,122,75,.12);--amb-c:rgba(204,37,57,.09);--ok:#0b7a4b;--ok-bg:#e0f2e8;--bad:#cc2539;--bad-bg:#fbe6e9;--warn:#9a6300;--warn-bg:#faefd4}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 var(--sans)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
code{font:12.5px var(--mono);background:var(--sheet-2);padding:2px 6px;border-radius:4px;overflow-wrap:anywhere}
h1,h2,h3{font-family:var(--display);text-wrap:balance;letter-spacing:-.01em;margin:0}
h1{font-size:clamp(28px,5.2vw,46px);line-height:1.04;font-weight:800;font-stretch:88%}
h2{font-size:22px;line-height:1.2;font-weight:700;font-stretch:92%}h3{font-size:16px;font-weight:700;margin:18px 0 8px}
.wrap{max-width:1120px;margin:0 auto;padding-inline:16px}.sr{position:absolute;left:-9999px}
.muted{color:var(--muted)}.small{font-size:13px}.sub{color:var(--muted);margin:4px 0 0;max-width:68ch}
.top{background:var(--sheet);border-bottom:1px solid var(--rule)}
.top .wrap{display:flex;align-items:center;gap:8px 28px;min-height:58px;flex-wrap:wrap;padding-block:8px}
.brand{display:flex;align-items:center;gap:8px;color:var(--ink);font:800 21px/1 var(--display);font-stretch:85%;letter-spacing:-.01em}
.brand:hover{text-decoration:none}.mark{width:26px;height:26px;color:var(--ok)}
.top nav{display:flex;gap:4px 18px;flex-wrap:wrap;font-weight:500}.top nav a{color:var(--ink-2)}
.theme{position:relative;flex:none;display:inline-flex;align-items:center;justify-content:space-between;width:54px;height:28px;
  margin-left:auto;padding:0 7px;border:1px solid var(--rule-2);border-radius:999px;background:var(--sheet-2);color:var(--muted);cursor:pointer}
.theme svg{position:relative;z-index:1;width:13px;height:13px;transition:color .2s}
.theme .knob{position:absolute;top:2px;left:2px;width:22px;height:22px;border-radius:50%;background:var(--ink);
  box-shadow:0 1px 4px rgba(0,0,0,.35);transition:transform .22s cubic-bezier(.3,.7,.3,1)}
.theme[aria-checked=true] .knob{transform:translateX(26px)}
.theme[aria-checked=false] .moon,.theme[aria-checked=true] .sun{color:var(--sheet)}
.theme:hover{border-color:var(--ink-2)}.theme:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (prefers-reduced-motion:reduce){.theme .knob{transition:none}}
main{padding-block:28px 56px}[hidden]{display:none!important}
.stack{display:flex;flex-direction:column;gap:52px}.filepage{display:flex;flex-direction:column;gap:16px}.filepage>.crumb{margin:0}
.eyebrow{font:600 12px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin:0 0 12px}
.intro h1{max-width:19ch}.lede{font-size:17px;line-height:1.55;color:var(--ink-2);max-width:62ch;margin:16px 0 22px}
.search{position:relative;max-width:620px}
input[type=search]{width:100%;font:15px var(--sans);padding:13px 16px;border:1px solid var(--rule-2);border-radius:10px;background:var(--sheet);color:var(--ink)}
.filter{max-width:420px;margin:0 0 14px}
.results{position:absolute;z-index:5;left:0;right:0;top:calc(100% + 6px);background:var(--sheet);border:1px solid var(--rule-2);border-radius:10px;box-shadow:0 12px 32px rgba(8,14,26,.16);overflow:hidden}
.results a{display:flex;gap:10px;align-items:center;padding:10px 14px;color:var(--ink);border-top:1px solid var(--rule)}
.results a:first-child{border-top:0}.results a:hover,.results a:focus{background:var(--sheet-2);text-decoration:none}
.results .none{padding:11px 14px;color:var(--muted)}.results .chip{margin-left:auto}
.tally{display:flex;flex-wrap:wrap;gap:10px 36px;margin:28px 0 0}.tally div{display:flex;flex-direction:column-reverse}
.tally dd{margin:0;font:600 26px/1.1 var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.tally dt{font-size:13px;color:var(--muted)}
.sec-head{display:flex;justify-content:space-between;align-items:flex-end;gap:12px 24px;flex-wrap:wrap;margin-bottom:16px}
.filters{display:flex;gap:6px;flex-wrap:wrap}
.filters button{font:600 13px var(--sans);display:inline-flex;gap:7px;align-items:center;padding:7px 12px;border-radius:999px;border:1px solid var(--rule-2);background:var(--sheet);color:var(--ink-2);cursor:pointer}
.filters button span{font:500 12px var(--mono);color:var(--muted)}
.filters button[aria-pressed=true]{background:var(--ink);border-color:var(--ink);color:var(--sheet)}.filters button[aria-pressed=true] span{color:inherit;opacity:.7}
.feed{list-style:none;margin:0;padding:0;display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(min(100%,330px),1fr))}
.feed>.card.pg-off,tr.pg-off,.imps>.pg-off{display:none}
.pager{display:flex;flex-wrap:wrap;justify-content:center;align-items:center;gap:6px;margin:18px 0 0}
.pager button{font:600 13px var(--mono);min-width:38px;padding:8px 12px;border-radius:999px;border:1px solid var(--rule-2);background:var(--sheet);
  color:var(--ink-2);cursor:pointer}.pager button:hover:not(:disabled){color:var(--ink);border-color:var(--ink-2)}
.pager button[aria-current=page]{background:var(--ink);border-color:var(--ink);color:var(--sheet)}.pager button:disabled{opacity:.35;cursor:default}
.pager .gap{color:var(--muted);padding:0 2px}.pager .pg-count{flex-basis:100%;text-align:center;font:12px var(--mono);color:var(--muted)}
/* the feed's cards are glass, like the map's spheres: a see-through fill with a sheen, a rim lit from the top left and
   tinted by the verdict at the far corner, and the verdict's glow */
.card{position:relative;border:0;border-radius:14px;padding:14px 16px 13px;display:flex;flex-direction:column;gap:9px;
  background:radial-gradient(55% 42% at 14% 0%,var(--glass-spec),transparent 72%),
    radial-gradient(110% 75% at 100% 0%,var(--st-glow,transparent),transparent 60%),
    radial-gradient(80% 45% at 50% 115%,var(--st-glow,transparent),transparent 72%),
    linear-gradient(165deg,var(--glass-hi),transparent 34%),var(--glass);
  box-shadow:0 14px 30px -20px rgba(0,0,0,.55),inset 0 1px 0 var(--glass-hi);transition:transform .18s,box-shadow .18s}
.card::before{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;border:1px solid var(--rule)}
@supports ((mask-composite:exclude) or (-webkit-mask-composite:xor)){
.card::before{border:0;padding:1px;background:linear-gradient(150deg,var(--rim-hi),var(--rim-lo) 32%,var(--rim-lo) 62%,var(--st-rim,var(--rim-lo)));
  -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;
  mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);mask-composite:exclude}}
.card:hover{transform:translateY(-2px);box-shadow:0 18px 38px -16px var(--st-glow,rgba(0,0,0,.5)),inset 0 1px 0 var(--glass-hi)}
@media (prefers-reduced-motion:reduce){.card,.card:hover{transition:none;transform:none}}
#live,#watching{position:relative;z-index:0}
/* soft colour behind the feed, repeating all the way down, for the glass to show */
#live::before{content:"";position:absolute;inset:-24px 0 0;z-index:-1;pointer-events:none;background-repeat:repeat-y;background-size:100% 1300px;
  background-image:radial-gradient(34% 24% at 12% 22%,var(--amb-a),transparent 70%),radial-gradient(30% 22% at 88% 50%,var(--amb-b),transparent 70%),
  radial-gradient(36% 24% at 45% 86%,var(--amb-c),transparent 70%)}
.card-top{display:flex;justify-content:space-between;gap:10px;font:500 11.5px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.card-top .age{text-transform:none;letter-spacing:0}
.card-title{display:flex;align-items:center;gap:10px;justify-content:space-between}
.stretch::after{content:"";position:absolute;inset:0;border-radius:14px}.card .rcpt,.card .real a,.card .copynote a{position:relative;z-index:1}
.copynote a{color:inherit;text-decoration:underline;text-underline-offset:2px}
.card-nums{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:0;padding:9px 0;border-block:1px dashed var(--rule)}
.card-nums dt{font-size:11.5px;color:var(--muted)}.card-nums dd{margin:0;font:500 14px var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
.card .why{margin:0;font-size:13.5px;color:var(--ink-2);display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.rcpt{font-weight:600;white-space:nowrap}
.trend{display:flex;align-items:center;gap:12px}.trend .spark{flex:1;min-width:0}.trend .tv{font:500 12.5px var(--mono);text-align:right;white-space:nowrap;color:var(--muted)}
.trend .tv b{display:block;font-weight:600;font-size:13.5px;color:var(--ink)}.trend .none{flex:1;font-size:12px;color:var(--muted)}
.spark{display:block;width:100%;height:32px;overflow:visible}.spark .line{fill:none;stroke:var(--sk);stroke-width:1.6;vector-effect:non-scaling-stroke;stroke-linejoin:round}
.spark .area{fill:var(--sk);opacity:.12}.spark.up{--sk:var(--ok)}.spark.down{--sk:var(--bad)}.up{color:var(--ok)}.down{color:var(--bad)}
.chart{margin:16px 0 0;padding:14px 14px 10px;border:1px solid var(--rule);border-radius:10px;background:var(--sheet-2)}
.chart .spark{height:96px}.chart figcaption{display:flex;justify-content:space-between;gap:8px 16px;flex-wrap:wrap;font:500 12.5px var(--mono);color:var(--muted);margin-top:8px}
.keynums dd small{font-size:11px;color:var(--muted);font-weight:500}.real{margin:0;font-size:13.5px}
.tok{display:inline-flex;align-items:baseline;gap:4px 6px;color:var(--ink);flex-wrap:wrap;min-width:0}.tok:hover{text-decoration:none}
.tok b{font:700 17px var(--display);font-stretch:90%;letter-spacing:-.005em}.tok .no{font:500 12px var(--mono);color:var(--muted)}
.tok .nm{color:var(--muted);font-size:13.5px}.tok.inline b{font-size:15px}.tok:hover b{text-decoration:underline}
.chip{display:inline-flex;align-items:center;gap:6px;flex:none;font:600 11.5px var(--mono);letter-spacing:.05em;text-transform:uppercase;padding:3px 9px;border-radius:999px;white-space:nowrap;color:var(--st);background:var(--st-bg)}
.chip i{width:6px;height:6px;border-radius:50%;background:currentColor}
.v-verified{--st:var(--ok);--st-bg:var(--ok-bg)}.v-scam,.v-linked{--st:var(--bad);--st-bg:var(--bad-bg)}
.v-checking{--st:var(--accent);--st-bg:var(--accent-bg)}.v-unverified{--st:var(--warn);--st-bg:var(--warn-bg)}
.status{display:inline-block;font:600 11.5px var(--mono);letter-spacing:.05em;text-transform:uppercase;padding:3px 9px;border-radius:5px;border:1px solid var(--rule-2);color:var(--ink-2);white-space:nowrap}
.s-proven{background:var(--ok);border-color:var(--ok);color:var(--sheet)}.s-live{border-color:var(--ok);color:var(--ok)}
.s-building{border-color:var(--accent);color:var(--accent)}.s-scam{border-color:var(--bad);color:var(--bad)}
.meter{display:inline-block;width:52px;height:6px;border-radius:3px;background:var(--rule);overflow:hidden;vertical-align:middle;margin-right:7px}
.meter>span{display:block;height:100%;background:var(--ink)}.grad{color:var(--ok);font-weight:600}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid var(--rule);border-radius:12px;background:var(--sheet)}
table.list{width:100%;border-collapse:collapse;font-size:14px}
.list th{text-align:left;font:600 11px var(--mono);letter-spacing:.07em;text-transform:uppercase;color:var(--muted);padding:11px 12px;border-bottom:1px solid var(--rule);white-space:nowrap}
.list td{padding:11px 12px;border-top:1px solid var(--rule);vertical-align:middle}.list tr:first-child td{border-top:0}
.list .num{width:1%;color:var(--muted);font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
.list .score{text-align:right;font:600 16px var(--mono);font-variant-numeric:tabular-nums;width:1%}
.list .numcol{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
.list.ev td:first-child{width:34%}.list tr.zero td{color:var(--muted)}.wrapcell{overflow-wrap:anywhere}
.list.md td,.list.md th{vertical-align:top;text-transform:none;letter-spacing:0;font:14px/1.5 var(--sans)}.list.md th{font-weight:600}
.boardkey{display:flex;flex-wrap:wrap;gap:8px 20px;list-style:none;margin:0 0 12px;padding:0;font-size:13px;color:var(--muted)}
.boardkey li{display:flex;align-items:center;gap:7px}.boardkey .k-sm{display:none}.boardkey .mheat{display:inline-flex}
.board{--heat:.52}.board tbody tr{--tone:var(--accent);--ring-bg:var(--sheet)}
.board tbody tr.t-proven,.board tbody tr.t-live{--tone:var(--ok)}.board tbody tr.t-caution{--tone:var(--warn)}
.board tbody td:first-child{box-shadow:inset 3px 0 0 var(--tone)}.board tbody tr:hover{--ring-bg:var(--sheet-2)}.board tbody tr:hover td{background:var(--sheet-2)}
.board td{padding-block:9px}.board .grp th{padding:11px 6px 0;border-bottom:0;font-size:10px;letter-spacing:.09em;text-align:center}
.board .grp .g-ev,.board .grp .g-mkt{background:linear-gradient(var(--rule-2),var(--rule-2)) bottom/calc(100% - 14px) 1px no-repeat;padding-bottom:6px}
.board .who{min-width:180px}.who-top{display:flex;align-items:center;gap:9px}.who-top>.chip{font-size:10px;padding:2px 7px}
.sealwrap{display:inline-flex;flex:none}.sealwrap .seal{width:21px;height:21px}
.tsub{display:flex;flex-wrap:wrap;gap:4px 8px;margin:5px 0 0 30px}
.look{font:500 11.5px var(--mono);color:var(--muted);cursor:help}
.tag{font:600 10px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--accent);border:1px solid currentColor;border-radius:4px;padding:1px 5px}
.ic{width:14px;height:14px;flex:none}
.sbadge{display:inline-flex;align-items:center;gap:6px;font:700 11.5px var(--mono);letter-spacing:.07em;text-transform:uppercase;padding:4px 10px 4px 7px;border-radius:999px;white-space:nowrap;color:var(--sb);background:var(--sb-bg);box-shadow:inset 0 0 0 1px var(--sb-rim,transparent);cursor:help}
.sb-proven{--sb:var(--ok);--sb-bg:var(--ok-bg);--sb-rim:var(--ok)}.sb-live{--sb:var(--ok);--sb-bg:var(--ok-bg)}.sb-building{--sb:var(--accent);--sb-bg:var(--accent-bg)}
.caution{display:inline-flex;align-items:center;gap:5px;font:600 12px var(--sans);color:var(--warn);background:var(--warn-bg);padding:3px 8px 3px 6px;border-radius:6px;white-space:nowrap;cursor:help}
.board .trust{white-space:nowrap}.board .trust .caution{display:flex;width:max-content;margin-top:5px}
.board .sc{width:1%;text-align:center;padding-inline:10px}
.ring{display:inline-grid;place-items:center;width:44px;height:44px;border-radius:50%;font:700 15px var(--mono);font-variant-numeric:tabular-nums;color:var(--ink);cursor:help;
  background:radial-gradient(closest-side,var(--ring-bg) 80%,transparent 82%),conic-gradient(var(--tone) calc(var(--p)*1%),var(--rule) 0)}
.board th.heat{text-align:center;padding-inline:3px;letter-spacing:.04em;line-height:1.25}.board th.heat small{display:block;font-size:9.5px;letter-spacing:0;opacity:.7}
.board td.heat{padding-inline:3px;width:58px}
.heat span,.mheat i,.ramp i{position:relative;z-index:0}
.heat span::before,.mheat i::before,.ramp i::before{content:"";position:absolute;inset:0;z-index:-1;border-radius:inherit;background:var(--ok);opacity:calc(var(--f)*var(--heat,.52))}
.heat span{display:block;min-width:40px;padding:8px 0;border-radius:8px;text-align:center;font:600 13.5px var(--mono);font-variant-numeric:tabular-nums;color:var(--ink)}
.heat span[style="--f:0.00"],.mheat i[style="--f:0.00"]{color:var(--muted);box-shadow:inset 0 0 0 1px var(--rule)}
.ramp{display:inline-flex;gap:3px}.ramp i{width:16px;height:14px;border-radius:4px}
.board .mkt{color:var(--ink-2);font-size:13px;white-space:nowrap}.board .mkt .meter{width:34px;margin-right:6px}.board .mkt.first{border-left:1px solid var(--rule)}.board td.mkt .grad{font-weight:600}
.show-sm{display:none}.mheat{gap:3px;margin:7px 0 0 30px}
.mheat i{width:19px;height:17px;border-radius:4px;font:600 9.5px/17px var(--mono);font-style:normal;text-align:center;color:var(--ink-2)}
@media (max-width:560px){.mheat.show-sm{display:flex}.boardkey .k-sm{display:flex}.boardkey{gap:7px 14px;font-size:12.5px}
.board .who{min-width:0}.ring{width:38px;height:38px;font-size:13.5px}.list.board th,.list.board td{padding-inline:5px}
.board .who-top{gap:7px}.sealwrap .seal{width:18px;height:18px}.tsub,.mheat{margin-left:25px}
.sbadge{font-size:10px;padding:3px 8px 3px 6px;gap:4px}.sbadge .ic{width:12px;height:12px}.caution{font-size:11px}}
.dims{display:flex;gap:4px;min-width:150px}.dims .dim{flex:1}
.bar{display:block;height:6px;border-radius:3px;background:var(--rule);overflow:hidden}.bar>span{display:block;height:100%;background:var(--ink)}
.dims.full{flex-direction:column;gap:9px;margin-top:14px}.dims.full .dim{display:grid;grid-template-columns:78px 1fr 58px;align-items:center;gap:10px}
.dims.full .bar{height:8px}.dl{font-size:13.5px;color:var(--ink-2)}.dv{text-align:right;font:600 14px var(--mono);font-variant-numeric:tabular-nums}.dv small{color:var(--muted);font-weight:400}
.imps{list-style:none;margin:0;padding:0;background:var(--sheet);border:1px solid var(--rule);border-radius:12px;overflow:hidden}
.imp{display:grid;grid-template-columns:minmax(210px,28%) minmax(0,1fr);gap:10px 24px;padding:16px 18px 16px 20px;border-top:1px solid var(--rule);box-shadow:inset 3px 0 0 var(--bad)}
.imp:first-child{border-top:0}.imp-id{min-width:0}.imp-meta{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;margin-top:8px}
.imp-meta .age{color:var(--muted);font:12px var(--mono)}.imp-body{min-width:0}
.rbadge{display:inline-flex;align-items:center;gap:6px;font:700 11.5px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--bad);background:var(--bad-bg);padding:4px 10px 4px 7px;border-radius:999px;white-space:nowrap;cursor:help}
.imp-ev{font-size:15.5px;line-height:1.45;color:var(--ink);overflow-wrap:anywhere}.imp-ev code{font:500 13.5px var(--mono);color:var(--ink-2)}
.imp-ev blockquote{margin:0;padding:1px 0 1px 13px;border-left:3px solid var(--bad);font-size:16px;font-weight:500}
.imp-ev .cite{margin:5px 0 0 16px;font-size:13px;color:var(--muted)}
.imp-facts{display:flex;flex-wrap:wrap;gap:6px;list-style:none;margin:11px 0 0;padding:0}
.imp-facts li{--st:var(--rule-2);display:inline-flex;align-items:center;gap:7px;font-size:13px;color:var(--ink-2);background:var(--sheet-2);border:1px solid var(--rule);border-radius:7px;padding:4px 10px 4px 8px}
.imp-facts li>i{flex:none;width:7px;height:7px;border-radius:50%;background:var(--st)}.imp-facts b{font-weight:700;font-variant-numeric:tabular-nums;color:var(--ink)}
.imp-facts .bad{--st:var(--bad);background:var(--bad-bg);border-color:transparent;color:var(--ink)}.imp-facts .bad b{color:var(--bad)}
.imp-facts .warn{--st:var(--warn)}.imp-facts .warn b{color:var(--warn)}.imp-facts .ok{--st:var(--ok)}
@media (max-width:700px){.imp{grid-template-columns:1fr;gap:9px;padding:14px 14px 14px 16px}.imp-ev,.imp-ev blockquote{font-size:15px}}
.more{margin:14px 0 0;font-weight:600}
.how{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:14px}
.how>div{background:var(--sheet);border:1px solid var(--rule);border-radius:12px;padding:16px}.how p{margin:10px 0 0;font-size:14px;color:var(--ink-2)}
.how.two{grid-template-columns:repeat(2,minmax(0,1fr))}.how.two ul{margin:10px 0 0;padding-left:18px;color:var(--ink-2);font-size:14.5px}
.how.two li+li{margin-top:7px}.collected{margin:14px 0 0;color:var(--ink-2)}.collected b{color:var(--ink)}.how.two h3{font-size:17px}
@media (max-width:700px){.how.two{grid-template-columns:1fr}}
.casefile{gap:40px}.casehead .lede{max-width:72ch}.casehead h1{margin-top:4px}
.pv{display:inline-block;margin-left:8px;padding:2px 9px;border-radius:999px;background:var(--accent-bg);color:var(--accent);letter-spacing:.06em}
.pv.draft-tag{background:var(--warn-bg);color:var(--warn)}
.draft{margin:14px 0 0;padding:10px 14px;border:1px dashed var(--warn);border-radius:10px;color:var(--warn);background:var(--warn-bg);font:600 13px var(--mono)}
.findings{list-style:none;counter-reset:f;margin:14px 0 0;padding:0;display:grid;gap:12px}
.findings>li{counter-increment:f;position:relative;padding:16px 18px 16px 58px;background:var(--sheet);border:1px solid var(--rule);border-radius:12px}
.findings>li::before{content:counter(f,decimal-leading-zero);position:absolute;left:18px;top:17px;font:700 15px var(--mono);color:var(--accent)}
.findings h3{font-size:18px}.findings p{margin:8px 0 0;color:var(--ink-2);max-width:78ch}
.findings .rc{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:13.5px}
.fig{margin:12px 0 4px;padding:14px 16px 12px;background:var(--sheet-2);border:1px solid var(--rule);border-radius:10px;max-width:78ch;min-width:0;container-type:inline-size}
.fig figcaption{margin-top:10px;font-size:12.5px;color:var(--muted)}
.r-x{--c:var(--accent)}.r-y{--c:var(--warn)}.r-z{--c:var(--ink-2)}.r-bad{--c:var(--bad)}.r-ok{--c:var(--ok)}.r-hi{--c:var(--accent)}
.r-mid{--c:var(--ink-2)}.r-lo{--c:var(--muted)}
.lanes{display:grid;gap:9px}
.lane{display:grid;grid-template-columns:minmax(0,11.5rem) minmax(0,1fr);gap:3px 14px;align-items:center}
.lane.has-marks{padding-top:14px}
.ln-label{display:flex;flex-direction:column;line-height:1.25;min-width:0}
.ln-label b{font:600 13.5px var(--sans);color:var(--c)}.ln-label span{font-size:12px;color:var(--muted)}
.ln-track{position:relative;height:26px;border-radius:6px;background:var(--sheet);border:1px solid var(--rule)}
.ln-span{position:absolute;top:4px;bottom:4px;border-radius:4px;background:var(--c);color:var(--sheet);font:600 11px/16px var(--mono);overflow:hidden;white-space:nowrap;padding:0 6px}
.ln-pt{position:absolute;top:50%;width:calc(5px + var(--s)*15px);height:calc(5px + var(--s)*15px);border-radius:50%;background:var(--c);opacity:.72;transform:translate(-50%,-50%);box-shadow:0 0 0 1px var(--sheet)}
.ln-mark{position:absolute;top:-3px;bottom:-3px;width:0;border-left:2px solid var(--bad)}
.ln-mark em{position:absolute;bottom:100%;left:0;transform:translateX(-50%);font:600 10.5px var(--mono);font-style:normal;color:var(--bad);white-space:nowrap}
.ln-rule{position:absolute;top:-6px;bottom:-6px;border-left:2px dashed var(--ink-2);opacity:.55}
.ln-axis .ln-track{height:34px;background:none;border:0}
.ln-axis .ln-track span{position:absolute;top:0;transform:translateX(-50%);font:11px var(--mono);color:var(--muted);white-space:nowrap}
.ln-axis .ln-track span.ln-rule-label{top:16px;transform:translateX(-100%);padding-right:6px;color:var(--ink-2);font-weight:600}
.ln-axis .ln-track span.at-start,.ln-mark.at-start em{transform:none}.ln-axis .ln-track span.at-end,.ln-mark.at-end em{transform:translateX(-100%)}
.sp-row+.sp-row{margin-top:14px}
.sp-label{margin:0 0 6px;font:600 13.5px var(--sans);color:var(--ink)}
.sp-bar{display:flex;gap:2px;height:30px;border-radius:7px;overflow:hidden}
.sp-seg{background:var(--c);display:flex;align-items:center;justify-content:center;min-width:3px}
.sp-seg b{font:600 12px var(--mono);color:var(--sheet);white-space:nowrap}.sp-seg.narrow b{display:none}
.sp-key{list-style:none;margin:8px 0 0;padding:0;display:flex;flex-wrap:wrap;gap:4px 16px;font-size:13px;color:var(--ink-2)}
.sp-key li{display:inline-flex;align-items:center;gap:6px}.sp-key b{color:var(--ink)}
.sp-key i{width:10px;height:10px;border-radius:3px;background:var(--c);flex:none}
.fig-change{margin:12px 0 0;font-size:13.5px;color:var(--ink-2)}.fig-change s{color:var(--muted)}.fig-change strong{color:var(--bad)}
.flow{display:flex;align-items:stretch}
.fl-col{display:flex;flex-direction:column;gap:8px;justify-content:center;flex:1 1 0;min-width:0}
.fl-node{background:var(--sheet);border:1px solid var(--rule);border-left:4px solid var(--c);border-radius:8px;padding:8px 10px;display:flex;flex-direction:column;gap:2px;min-width:0}
.fl-node b{font:600 13.5px/1.3 var(--sans);color:var(--ink);overflow-wrap:anywhere}
.fl-node span{font-size:12px;line-height:1.35;color:var(--ink-2)}
.fl-node em{font:600 10.5px var(--mono);font-style:normal;text-transform:uppercase;letter-spacing:.06em;color:var(--c)}
.fl-node.guess{border-style:dashed;border-left-style:solid}
.fl-arrow{flex:0 0 92px;position:relative;display:flex;align-items:center;justify-content:center}
.fl-arrow::after{content:"";position:absolute;left:6px;right:10px;top:50%;border-top:2px solid var(--rule-2)}
.fl-arrow::before{content:"";position:absolute;right:4px;top:calc(50% - 5px);border:6px solid transparent;border-left:8px solid var(--rule-2);border-right:0}
.fl-arrow span{position:absolute;left:2px;right:8px;bottom:calc(50% + 5px);font:600 11px/1.25 var(--mono);color:var(--ink-2);text-align:center}
@container (max-width:560px){.lane{grid-template-columns:minmax(0,1fr)}.ln-axis>div:first-child{display:none}
.ln-axis .ln-track span.minor{display:none}
.lane.has-marks{padding-top:0;padding-bottom:14px}.ln-mark em{bottom:auto;top:100%}
.ln-label{flex-direction:row;flex-wrap:wrap;gap:0 8px;align-items:baseline}}
@container (max-width:720px){.flow{flex-direction:column}.fl-col{flex-direction:row;flex-wrap:wrap}.fl-col>.fl-node{flex:1 1 12rem}
.fl-arrow{flex:0 0 44px}
.fl-arrow::after{left:22px;right:auto;top:4px;bottom:10px;border-top:0;border-left:2px solid var(--rule-2)}
.fl-arrow::before{right:auto;left:17px;top:auto;bottom:2px;border:6px solid transparent;border-top:8px solid var(--rule-2);border-bottom:0}
.fl-arrow span{left:36px;right:0;bottom:auto;top:50%;transform:translateY(-50%);text-align:left}}
.beads{list-style:none;margin:16px 0 0;padding:0;display:flex;flex-wrap:wrap;row-gap:16px}
.beads li{display:flex;align-items:flex-start}
.beads li+li::before{content:"";flex:none;width:10px;height:2px;margin-top:12px;background:var(--rule-2)}
.beads a{display:flex;flex-direction:column;align-items:center;gap:4px;width:62px;color:var(--ink);text-decoration:none}
.beads i,.beadkey i{display:block;width:24px;height:24px;border-radius:50%;flex:none;transition:transform .15s;
  background:radial-gradient(circle at 34% 30%,rgba(255,255,255,.6),var(--st) 44%,color-mix(in srgb,var(--st) 50%,#000) 100%);
  box-shadow:0 0 12px color-mix(in srgb,var(--st) 45%,transparent)}
.beads a:hover i,.beads a:focus-visible i{transform:scale(1.15)}
.beads b{font:600 10.5px var(--mono);max-width:62px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.beads span{font:500 10px var(--mono);color:var(--muted)}
.beadkey{display:flex;flex-wrap:wrap;gap:6px 16px;margin:14px 0 0;font-size:13px;color:var(--ink-2)}
.beadkey span{display:inline-flex;align-items:center;gap:7px}.beadkey i{width:12px;height:12px}
.trail{list-style:none;margin:14px 0 0;padding:0;max-width:760px}
.tr-node{background:var(--sheet);border:1px solid var(--rule);border-radius:12px;padding:12px 14px}
.tr-node.has-launch{border-color:color-mix(in srgb,var(--st) 45%,var(--rule));box-shadow:inset 3px 0 0 var(--st)}
.tr-who{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px}
.tr-role{font:600 11px var(--mono);letter-spacing:.07em;text-transform:uppercase;color:var(--muted)}
.tr-addr{text-decoration:none}.tr-note{margin:6px 0 0;font-size:14px;color:var(--ink-2)}
.tr-launch{margin:8px 0 0;font-size:14px;display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px}
.tr-edge{position:relative;margin-left:24px;padding:10px 0 14px 20px;border-left:2px dashed var(--rule-2)}
.tr-edge::after{content:"";position:absolute;left:-7px;bottom:-1px;border:6px solid transparent;border-top:8px solid var(--rule-2);border-bottom:0}
.tr-moves{list-style:none;margin:0;padding:0;display:grid;gap:4px;font-size:14px}
.tr-moves li{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 10px}.tr-moves b{font-family:var(--mono)}
.trail.compact .tr-node{padding:8px 12px}.trail.compact .tr-edge{padding:5px 0 9px 20px}.trail.compact .tr-launch{margin-top:4px}
.case-more{margin-top:14px}.case-more>summary{cursor:pointer;font-weight:600;color:var(--accent)}
.method p{color:var(--ink-2);max-width:76ch}
.cases{list-style:none;margin:0;padding:0;display:grid;gap:14px}
.casecard>a{display:block;padding:20px 22px;background:var(--sheet);border:1px solid var(--rule);border-radius:14px;color:inherit;text-decoration:none;transition:border-color .15s}
.casecard>a:hover{border-color:var(--rule-2)}.casecard h2{font-size:clamp(21px,3vw,26px)}.casecard p{color:var(--ink-2);max-width:76ch}
.casecard .tally{margin-top:16px}
/* the case files: a manila folder of typed documents (cases_page); paper and card stay paper-coloured in either theme */
.casefolder{--manila:#d8b676;--manila-2:#c9a35f;--manila-edge:#9c7a41;--paper:#f5f1e7;--paper-2:#ebe5d6;--ink:#272219;
  --ink-2:#5b5448;--rule-p:#d6cfbf;--stamp:#b8352c;--pw:min(430px,calc((100vw - 72px)/2));--ph:calc(var(--pw)*1.34);
  --turn:.85s cubic-bezier(.45,.05,.2,1);filter:brightness(.93) saturate(.95);font-family:var(--sans)}
:root[data-theme="light"] .casefolder{filter:none}
.casefolder:not(.ready){display:grid;gap:16px}
.cf-face{color:var(--ink);box-sizing:border-box}
.casefolder:not(.ready) .cf-face{border-radius:12px;overflow:hidden}.casefolder:not(.ready) .cf-open{display:none}
.cf-cover,.cf-inside,.cf-end{background:linear-gradient(135deg,var(--manila),var(--manila-2));padding:28px 26px;position:relative;
  box-shadow:inset 0 0 0 1px rgba(0,0,0,.08),inset 0 -40px 60px rgba(120,85,30,.18)}
.cf-cover{border-radius:4px 12px 12px 4px}
.cf-tab{position:absolute;top:-24px;left:22px;height:26px;padding:0 18px;border-radius:8px 8px 0 0;background:var(--manila);
  font:700 11px/26px var(--mono);letter-spacing:.18em;text-transform:uppercase;color:#5a4521;box-shadow:inset 0 1px 0 rgba(255,255,255,.35)}
.casefolder:not(.ready) .cf-tab{position:static;display:inline-block;margin-bottom:8px;border-radius:8px}
.cf-stencil{margin:26px 0 0;font:900 clamp(44px,9vw,68px)/.92 var(--display);letter-spacing:.04em;text-transform:uppercase;
  color:rgba(70,48,14,.82);font-stretch:85%}
.cf-stencil.small{font-size:34px;margin:0 0 12px}
.cf-label{margin:26px 0 0;padding:12px 14px;background:var(--paper);border:1px solid var(--rule-p);border-radius:3px;max-width:290px;
  box-shadow:0 1px 2px rgba(0,0,0,.18);transform:rotate(-1deg);display:grid;gap:6px}
.cf-label div{display:grid;grid-template-columns:76px 1fr;gap:8px}.cf-label dt{font:700 10.5px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--ink-2)}
.cf-label dd{margin:0;font:600 13px var(--mono);color:var(--ink)}
.cf-stamp,.cf-mark{display:inline-block;font:800 13px var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--stamp);
  border:2.5px solid var(--stamp);border-radius:5px;padding:5px 10px;opacity:.82;mix-blend-mode:multiply}
.cf-stamp{position:absolute;right:26px;bottom:92px;transform:rotate(-9deg)}
.cf-open{position:absolute;left:26px;bottom:26px;font:700 13px var(--mono);letter-spacing:.08em;text-transform:uppercase;cursor:pointer;
  color:#3b2c12;background:rgba(255,255,255,.35);border:1px solid rgba(60,40,10,.35);border-radius:999px;padding:10px 16px}
.cf-open:hover{background:rgba(255,255,255,.55)}
.cf-card{background:var(--paper);border:1px solid var(--rule-p);border-radius:4px;padding:20px 18px;box-shadow:0 2px 6px rgba(0,0,0,.18);
  transform:rotate(.6deg)}
.cf-k{margin:0 0 10px;font:700 11px var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--ink-2)}
.cf-index{list-style:none;margin:0;padding:0}.cf-index li+li{border-top:1px dashed var(--rule-p)}
.cf-index a{display:grid;grid-template-columns:40px 1fr;gap:2px 10px;padding:10px 2px;color:var(--ink);text-decoration:none}
.cf-index a:hover .t{text-decoration:underline}.cf-index .no{font:700 13px var(--mono);color:var(--stamp);grid-row:span 2}
.cf-index .t{font-weight:700;font-size:15px}.cf-index .d{font:12px var(--mono);color:var(--ink-2)}
.cf-hint{margin:14px 0 0;font:12px var(--mono);color:var(--ink-2)}
.cf-doc{background:var(--paper);background-image:repeating-linear-gradient(transparent 0 27px,rgba(90,80,60,.07) 27px 28px);
  border-radius:2px 6px 6px 2px;box-shadow:inset 18px 0 22px -18px rgba(0,0,0,.28)}
.cf-paper{position:relative;height:100%;padding:24px 26px 22px;display:flex;flex-direction:column;gap:10px;box-sizing:border-box;overflow:hidden}
.cf-head{display:flex;justify-content:space-between;margin:0;padding-bottom:8px;border-bottom:2px solid var(--ink);
  font:700 11.5px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--ink)}
.cf-mark{align-self:flex-end;margin:-2px 0 -6px;transform:rotate(-6deg);font-size:11px;padding:3px 8px;border-width:2px}
.cf-mark.draft{color:#9a5b00;border-color:#9a5b00}
.cf-doc h2{margin:2px 0 0;font:800 clamp(22px,2.6vw,28px)/1.08 var(--display);color:var(--ink)}
.cf-paper>*{flex-shrink:0}
.cf-dek{margin:0;font-size:14px;line-height:1.5;color:var(--ink-2);display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:5;overflow:hidden}
.cf-tiles{display:flex;gap:18px;margin:4px 0 0}.cf-tiles div{display:flex;flex-direction:column-reverse;min-width:0}
.cf-tiles dd{margin:0;font:700 24px/1.1 var(--mono);color:var(--ink)}.cf-tiles dt{font-size:11.5px;color:var(--ink-2)}
/* a page's height is fixed: the exhibit gives way, so the link to the file always shows */
.cf-exhibit{margin-top:4px;padding:10px 12px;border:1px dashed var(--rule-p);border-radius:4px;background:rgba(255,255,255,.4);flex-shrink:1;min-height:0;overflow:hidden;
  -webkit-mask-image:linear-gradient(#000 calc(100% - 16px),transparent);mask-image:linear-gradient(#000 calc(100% - 16px),transparent)}
.cf-exhibit .cf-k{margin-bottom:8px;font-size:10px}
.cf-dots{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:4px}
.cf-dots li{width:10px;height:10px;border-radius:50%;background:var(--st);box-shadow:inset 0 0 0 1px rgba(0,0,0,.15)}
.cf-go{margin-top:auto;align-self:flex-start;font:700 13px var(--mono);color:#1f4fb3;text-decoration:none;border-bottom:1.5px solid currentColor}
.cf-pg{position:absolute;right:26px;bottom:20px;font:12px var(--mono);color:var(--ink-2)}
.cf-find{margin:6px 0 0;padding-left:20px;display:grid;gap:12px;font-size:15px;line-height:1.4;font-weight:600;color:var(--ink)}
.cf-find li::marker{font:700 13px var(--mono);color:var(--stamp)}
.cf-end{display:flex;flex-direction:column;justify-content:center;gap:6px;color:#3b2c12}.cf-end p{margin:0}.cf-end a{color:#3b2c12;font-weight:700}
/* bound into a book by the page script: leaves turn about the spine (book) or about the left edge (single) */
.casefolder.ready{position:relative;width:calc(var(--pw)*2);height:var(--ph);margin:44px auto 0;perspective:2600px;transition:transform var(--turn)}
.casefolder.ready.closed{transform:translateX(calc(var(--pw)*-.5))}
.cf-back{position:absolute;left:50%;top:0;width:var(--pw);height:var(--ph);border-radius:4px 12px 12px 4px;
  background:linear-gradient(135deg,var(--manila-2),var(--manila));box-shadow:0 18px 40px rgba(0,0,0,.45),inset 0 0 0 1px rgba(0,0,0,.1)}
.cf-back>.cf-end{position:absolute;inset:0;background:none;box-shadow:none;padding:36px}
.cf-leaf{position:absolute;left:50%;top:0;width:var(--pw);height:var(--ph);transform-origin:left center;transform-style:preserve-3d;
  transition:transform var(--turn)}
.cf-leaf.paper{top:10px;height:calc(var(--ph) - 20px);width:calc(var(--pw) - 14px)}
.cf-leaf.turned{transform:rotateY(-180deg)}
.cf-leaf>.cf-face{position:absolute;inset:0;margin:0;overflow:hidden;backface-visibility:hidden;-webkit-backface-visibility:hidden}
.cf-leaf>.cf-face.back{transform:rotateY(180deg)}
.cf-leaf>.cf-face.cf-inside{border-radius:12px 4px 4px 12px}
.cf-leaf>.cf-doc.back{border-radius:6px 2px 2px 6px;box-shadow:inset -18px 0 22px -18px rgba(0,0,0,.28)}
.cf-leaf.moving>.cf-face{box-shadow:0 10px 40px rgba(0,0,0,.35)}
.cf-leaf>.cf-cover{cursor:pointer;overflow:visible}  /* the tab stands above the cover's edge */
.casefolder.ready .cf-tab{z-index:1}
.casefolder.single{width:var(--pw)}.casefolder.single .cf-back,.casefolder.single .cf-leaf{left:0}
.cf-controls{display:flex;align-items:center;justify-content:center;gap:14px;margin:22px 0 0}
.cf-controls button{font:700 13px var(--mono);letter-spacing:.06em;padding:9px 16px;border-radius:999px;border:1px solid var(--rule-2);
  background:var(--sheet);color:var(--ink);cursor:pointer}.cf-controls button:disabled{opacity:.35;cursor:default}
.cf-count{font:600 12.5px var(--mono);color:var(--muted);min-width:150px;text-align:center}
@media (max-width:760px){.casefolder{--pw:min(430px,calc(100vw - 32px))}.cf-tiles{gap:12px}.cf-tiles dd{font-size:19px}.cf-tiles dt{font-size:11px}
.cf-dek{-webkit-line-clamp:3}.cf-dots{gap:3px}.cf-dots li{width:8px;height:8px}}
@media (prefers-reduced-motion:reduce){.casefolder{--turn:0s}}
.crumb{margin:0 0 6px;font-weight:600}
.file{position:relative;background:var(--sheet);border:1px solid var(--rule);border-radius:0 14px 14px 14px;padding:22px 22px 20px;margin-top:18px}
.file-tab{position:absolute;left:-1px;top:-31px;height:32px;padding:8px 16px 0;background:var(--sheet);border:1px solid var(--rule);border-bottom:0;border-radius:10px 10px 0 0;font:600 11.5px var(--mono);letter-spacing:.09em;text-transform:uppercase;color:var(--muted);white-space:nowrap;max-width:calc(100% + 2px);overflow:hidden;text-overflow:ellipsis}
.file-head{display:flex;justify-content:space-between;gap:18px;flex-wrap:wrap}.who{min-width:0;flex:1 1 320px}
.file h1 .tkr{font-stretch:82%}.file h1 .nm{color:var(--muted);font-weight:600;font-size:.55em;font-stretch:95%;letter-spacing:0}
.meta{margin:8px 0 0;color:var(--muted)}.ca{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0 0}
.badge{display:inline-block;font:600 11px var(--mono);letter-spacing:.06em;text-transform:uppercase;padding:2px 7px;border:1px solid var(--rule-2);border-radius:5px;margin-left:6px;color:var(--ink-2)}
.copy{font:600 12.5px var(--sans);padding:4px 11px;border:1px solid var(--rule-2);border-radius:6px;background:var(--sheet);color:var(--ink);cursor:pointer}
.stampbox{display:flex;flex-direction:column;align-items:center;gap:10px;padding:6px 8px 0}
.stamp{display:inline-block;color:var(--st);font:800 15px/1 var(--display);font-stretch:78%;letter-spacing:.14em;text-transform:uppercase;padding:9px 13px 8px;border:3px double currentColor;border-radius:7px;transform:rotate(-5deg);opacity:.93}
.stamp.big{font-size:25px;padding:13px 20px 11px;animation:thunk .38s cubic-bezier(.2,.9,.3,1.2) both}
@keyframes thunk{from{transform:rotate(-9deg) scale(1.25);opacity:.55}to{transform:rotate(-5deg) scale(1);opacity:.93}}
@media (prefers-reduced-motion:reduce){.stamp.big{animation:none}}
.stampnote{font:500 12px var(--mono);color:var(--muted);text-align:center}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0 0}
.btn{display:inline-flex;align-items:center;padding:8px 13px;border-radius:8px;border:1px solid var(--rule-2);background:var(--sheet);color:var(--ink);font:600 13.5px var(--sans);white-space:nowrap}
.btn:hover{text-decoration:none;border-color:var(--ink-2)}.btn.primary{background:var(--ink);border-color:var(--ink);color:var(--sheet)}
.keynums{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;background:var(--rule);border:1px solid var(--rule);border-radius:10px;overflow:hidden;margin:18px 0 0}
.keynums>div{background:var(--sheet-2);padding:11px 13px;min-width:0}
.keynums dt{font-size:11.5px;color:var(--muted)}.keynums dd{margin:2px 0 0;font:600 16px var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.keynums.small{grid-template-columns:repeat(auto-fit,minmax(170px,1fr))}.keynums span{display:block;font-size:12px;color:var(--muted);margin-top:2px}
.verdict{background:var(--sheet);border:1px solid var(--rule);border-radius:14px;padding:20px 22px}
.verdict .why{font:600 19px/1.4 var(--sans);margin:10px 0 8px;max-width:62ch}.receipts{margin:0 0 10px;padding-left:18px;overflow-wrap:anywhere}
.related{margin-top:16px;padding-top:14px;border-top:1px dashed var(--rule)}.related h3{margin-top:0}
.related ul{list-style:none;margin:0;padding:0;display:grid;gap:6px}.related.bad h3{color:var(--bad)}
.cols{display:grid;grid-template-columns:1.25fr 1fr;gap:16px}
.project,.score,.safety,.treasury{background:var(--sheet);border:1px solid var(--rule);border-radius:14px;padding:20px 22px}
.claim{font-size:16.5px;line-height:1.5;margin:12px 0 4px}
.desc{margin:14px 0 4px;padding:0 0 0 14px;border-left:2px solid var(--rule-2);color:var(--ink-2);font-style:italic;overflow-wrap:anywhere}
.xcard{margin-top:14px;padding:12px 14px;border:1px solid var(--rule);border-radius:10px;background:var(--sheet-2)}
.xcard p{margin:0}.xcard .bio{margin-top:6px;font-size:14px;color:var(--ink-2);white-space:pre-line;overflow-wrap:anywhere}
.scorehead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:12px}
.big{font:600 44px/1 var(--mono);margin:0;letter-spacing:-.03em;font-variant-numeric:tabular-nums}.big small{font-size:16px;color:var(--muted);letter-spacing:0}
.score .big.muted{font:600 26px var(--display);margin-top:12px}
.checks{list-style:none;margin:12px 0 0;padding:0;display:grid;gap:8px}
.checks li{display:flex;gap:10px;align-items:baseline}.checks li::before{content:"";flex:none;width:9px;height:9px;border-radius:50%;background:var(--st);transform:translateY(-1px)}
.checks .ok{--st:var(--ok)}.checks .warn{--st:var(--warn)}.checks .bad{--st:var(--bad)}
.lvl{font:600 11px var(--mono);letter-spacing:.04em;padding:2px 8px;border-radius:999px;background:var(--sheet-2);color:var(--ink-2);white-space:nowrap}
.l-bound,.l-bound-transitive{background:var(--ok-bg);color:var(--ok)}.l-contradicted,.l-disavowed{background:var(--bad-bg);color:var(--bad)}.l-unvouched,.l-named{background:var(--warn-bg);color:var(--warn)}
.evidence{background:var(--sheet);border:1px solid var(--rule);border-radius:14px;padding:16px 22px}
.evidence summary{cursor:pointer;display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 12px;list-style:none}
.evidence summary::-webkit-details-marker{display:none}.evidence summary h2::before{content:"▸ ";color:var(--muted)}.evidence[open] summary h2::before{content:"▾ "}
.outs{margin:0;padding-left:18px}.outs q{color:var(--muted)}
.page-head{margin-bottom:18px}.page-head h1{font-size:clamp(26px,4.4vw,38px)}
.prose{max-width:780px}.prose h1{font-size:clamp(26px,4.4vw,38px)}.prose h2{margin-top:36px}.prose p,.prose li{max-width:70ch}
.prose pre{background:var(--sheet);border:1px solid var(--rule);border-radius:10px;padding:14px;overflow-x:auto}.prose pre code{background:none;padding:0}
.prose .scroll{margin:14px 0}hr{border:0;border-top:1px solid var(--rule);margin:30px 0}
.foot{border-top:1px solid var(--rule);background:var(--sheet)}.foot .wrap{padding-block:20px 40px;font-size:14px}.foot p{margin:4px 0}
.card-top{justify-content:flex-start;align-items:center}.card-top .age{margin-left:auto}
.tools{display:inline-flex;gap:4px}.card .tools{position:relative;z-index:1}
.mini{font:600 11px var(--mono);letter-spacing:.02em;text-transform:none;padding:3px 7px;border:1px solid var(--rule-2);border-radius:6px;background:var(--sheet);color:var(--ink-2);cursor:pointer}
.mini:hover{border-color:var(--ink-2);color:var(--ink)}.star[aria-pressed=true]{color:var(--warn);border-color:var(--warn)}
.newbadge{font:700 10px var(--mono);letter-spacing:.08em;color:var(--sheet);background:var(--accent);padding:2px 6px;border-radius:4px}
.card.is-new{--st-rim:var(--accent);box-shadow:0 0 0 1px var(--accent),0 14px 30px -18px var(--accent-glow)}.trend:empty{display:none}.star-cell{width:1%}
.toast{position:fixed;left:50%;bottom:calc(20px + env(safe-area-inset-bottom,0px));transform:translateX(-50%);z-index:30;background:var(--ink);color:var(--sheet);font:600 13.5px var(--sans);padding:10px 16px;border-radius:10px;box-shadow:0 8px 24px rgba(8,14,26,.25)}
.report{margin:16px 0 0;padding-top:12px;border-top:1px dashed var(--rule);font-size:14px}
th[data-sort] button{all:unset;cursor:pointer;font:inherit;letter-spacing:inherit;text-transform:inherit;color:inherit}
th[data-sort] button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
th[aria-sort=descending] button::after{content:" ↓"}th[aria-sort=ascending] button::after{content:" ↑"}
.keynums dd small,.card-nums dd small{font-size:11px;color:var(--muted);font-weight:500}
.official-ca{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px}.official-ca code{word-break:break-all}
.lmapbox{margin:4px 0 18px}.lmap{display:block;width:100%;max-width:700px;height:auto;margin:0 auto}
.lmapdefs{position:absolute;width:0;height:0;overflow:hidden}
.lmapbox[data-view=new] .lmap[data-mode=top],.lmapbox[data-view=top] .lmap[data-mode=new],
.lmapbox[data-view=new] .for-top,.lmapbox[data-view=top] .for-new{display:none}
.mapview{display:flex;gap:2px;width:max-content;max-width:100%;margin:0 auto 10px;padding:3px;border:1px solid var(--rule-2);border-radius:999px;background:var(--sheet)}
.mapview button{font:600 12.5px var(--sans);color:var(--ink-2);background:none;border:0;border-radius:999px;padding:6px 14px;cursor:pointer;transition:background .15s,color .15s}
.mapview button:hover{color:var(--ink)}.mapview button[aria-pressed=true]{background:var(--ink);color:var(--sheet)}
.mapview button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
/* --age (1 fresh, paler with the hours) and --shrink (the last 12 of the 48 hours) come from the page script */
.lmap{-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
.lmap .bub{opacity:var(--age,1);transform:scale(var(--shrink,1));transition:opacity .15s,transform .2s;transform-box:fill-box;transform-origin:center}
.lmap .bub:focus{outline:none}.lmap .bub.gone{visibility:hidden}
.lmap .bub:hover,.lmap .bub:focus-visible,.lmap .bub.cur{opacity:1;transform:scale(calc(var(--shrink,1)*1.07))}
.lmap .bub.dim{opacity:calc(var(--age,1)*.16);pointer-events:none}.lmap.famfocus .bub:not(.hl):not(.dim){opacity:calc(var(--age,1)*.28)}
.lmap .fam circle{cursor:grab}.lmap.dragging,.lmap.dragging *{cursor:grabbing!important}
.lmap .bub.lifted{visibility:hidden}
.lmap .bub.born{animation:born .9s cubic-bezier(.2,.9,.3,1.25) both}@keyframes born{from{opacity:0;transform:scale(0)}}
.lmap .bub>*{pointer-events:none}.lmap .bub>.core{pointer-events:auto}
.lmap .core{fill:var(--st-bg);stroke:var(--st);stroke-width:1;stroke-opacity:.9;transition:stroke-width .15s}
.lmap .bub:hover .core,.lmap .bub:focus-visible .core,.lmap .bub.cur .core{stroke-width:2.2}
.lmap .glow{opacity:.8;transition:opacity .2s}.lmap .bub:hover .glow,.lmap .bub.cur .glow,.lmap .bub.hl .glow{opacity:1}
.lmap .aura{pointer-events:none}
.lmap stop{stop-color:var(--st)}.lmap stop.w{stop-color:#fff}.lmap stop.a{stop-color:var(--accent)}.lmap stop.i{stop-color:var(--ink)}
.lmap .grid ellipse{fill:none;stroke:var(--st);stroke-width:.7;opacity:.3;transition:opacity .2s}
.lmap .grid .mer{transform-box:fill-box;transform-origin:center;transform:scaleX(.45)}.lmap .grid .mer.b{opacity:0}
.lmap .bub:hover .grid ellipse,.lmap .bub.cur .grid ellipse{opacity:.6}
.lmap .bub:hover .mer,.lmap .bub.cur .mer{animation:spin 2.6s linear infinite}.lmap .bub:hover .mer.b,.lmap .bub.cur .mer.b{animation-delay:-.65s}
@keyframes spin{0%{transform:scaleX(1)}12.5%{transform:scaleX(.71)}25%{transform:scaleX(0)}37.5%{transform:scaleX(-.71)}50%{transform:scaleX(-1)}
62.5%{transform:scaleX(-.71)}75%{transform:scaleX(0)}87.5%{transform:scaleX(.71)}100%{transform:scaleX(1)}}
@media (prefers-reduced-motion:reduce){.lmap .bub:hover .mer,.lmap .bub.cur .mer{animation:none}.lmap .bub{transition:opacity .15s}}
.lmap .glint{fill:#fff;opacity:.9}
.lmap .ret{fill:none;stroke:var(--st);stroke-width:1.2;stroke-dasharray:10 5 2 5;opacity:0;transform-box:fill-box;transform-origin:center;transition:opacity .2s}
.lmap .bub:hover .ret,.lmap .bub:focus-visible .ret,.lmap .bub.cur .ret{opacity:.85;animation:ret 6s linear infinite}
@keyframes ret{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.lmap .bub:hover .ret,.lmap .bub.cur .ret{animation:none}}
.lmap .bub,.lmap .bub:hover{text-decoration:none}
.lmap .bub text{fill:var(--ink);font-family:var(--mono);font-weight:600;text-anchor:middle;dominant-baseline:central;
  paint-order:stroke;stroke:var(--st-bg);stroke-width:2.5px;stroke-linejoin:round;stroke-opacity:.7}
.lmap .bub .halo{fill:none;stroke:var(--st);stroke-width:2;opacity:0;pointer-events:none;transform-box:fill-box;transform-origin:center}
.lmap .bub.fresh .halo{animation:halo 2.4s ease-out infinite}
@keyframes halo{0%{opacity:.75;transform:scale(1)}80%,100%{opacity:0;transform:scale(1.7)}}
@media (prefers-reduced-motion:reduce){.lmap .bub.fresh .halo{animation:none;opacity:.45;transform:scale(1.3)}}
.lmap .fam circle{fill:none;stroke:var(--rule-2);stroke-width:1.5;stroke-dasharray:4 4;pointer-events:visible;transition:stroke .15s}
.lmap .fam.on circle{stroke:var(--ink-2);stroke-dasharray:none}
.lmap .bub text.m{display:none;font-size:18px}
.lmap .famlabel{cursor:pointer}.lmap .famlabel:focus{outline:none}
.lmap .famlabel rect{fill:var(--sheet);stroke:var(--rule-2);stroke-width:1;transition:stroke .15s}
.lmap .famlabel.on rect,.lmap .famlabel:focus-visible rect{stroke:var(--ink-2);stroke-width:1.5}
.lmap .famlabel text{fill:var(--ink-2);font:600 10.5px var(--mono);text-anchor:middle;dominant-baseline:central}
.maplegend{display:flex;flex-wrap:wrap;justify-content:center;align-items:center;gap:6px 10px;margin-top:8px;font-size:12.5px}
.maplegend button{display:inline-flex;align-items:center;gap:6px;font:500 12.5px var(--sans);color:var(--ink);background:none;border:1px solid transparent;border-radius:999px;padding:3px 9px;cursor:pointer;transition:opacity .15s,border-color .15s}
.maplegend button:hover{border-color:var(--rule-2)}.maplegend button[aria-pressed=true]{border-color:var(--st);background:var(--st-bg)}
.maplegend.filtering button:not([aria-pressed=true]){opacity:.5}
.maplegend i{width:12px;height:12px;border-radius:50%;box-shadow:0 0 6px var(--st);
  background:radial-gradient(circle at 34% 28%,rgba(255,255,255,.85) 0 13%,transparent 16%),
  radial-gradient(circle at 40% 35%,transparent 15%,rgba(0,0,0,.5) 100%) var(--st)}
.maplegend>.muted{flex-basis:100%;text-align:center}
.on-touch{display:none}@media (hover:none){.on-hover{display:none}.on-touch{display:inline}}
.mapcard{position:absolute;z-index:40;width:292px;max-width:calc(100vw - 16px);background:var(--sheet);border:1px solid var(--rule-2);border-radius:12px;box-shadow:0 14px 36px rgba(8,14,26,.3);padding:12px 14px;font-size:13.5px;pointer-events:none;opacity:0;transform:translateY(3px);transition:opacity .12s,transform .12s}
.mapcard.on{opacity:1;transform:none}.mapcard.pinned{pointer-events:auto}
.mc-top{display:flex;align-items:center;justify-content:space-between;gap:8px;font:500 11.5px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted);white-space:nowrap}
.mc-top .x{all:unset;cursor:pointer;font:600 18px/1 var(--sans);padding:4px 8px;margin:-8px -10px -8px 0;color:var(--muted)}.mc-top .x:hover{color:var(--ink)}
.mc-title{margin:6px 0 0;display:flex;align-items:center;gap:7px;min-width:0}.mc-title b{flex:none;font:700 18px var(--display);font-stretch:90%}
.mc-title .nm{flex:1;min-width:0;color:var(--muted);font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.mc-title .chip{margin-left:auto}
.mapcard .spark{height:34px;margin:8px 0 0}
.mc-nums{display:grid;grid-template-columns:repeat(4,auto);justify-content:space-between;gap:6px 10px;margin:8px 0 0;padding:8px 0;border-block:1px dashed var(--rule)}
.mc-nums dt{font-size:11px;color:var(--muted)}.mc-nums dd{margin:1px 0 0;font:600 13px var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
.mc-nums dd small{display:none}
.mapcard .checks{margin:9px 0 0;gap:5px;font-size:13px;line-height:1.35}
.mc-why{margin:9px 0 0;color:var(--ink-2);font-size:13px;line-height:1.4;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.mc-real{margin:6px 0 0;font-size:13px}.mc-hint{margin:9px 0 0;font:500 11.5px var(--mono);color:var(--muted)}
.mc-act{display:flex;gap:6px;align-items:center;margin-top:11px}.mc-act .btn{padding:6px 11px;font-size:13px}
.mc-sum{margin:6px 0 0;font-size:13px;color:var(--ink-2)}
.mc-fam{list-style:none;margin:9px 0 0;padding:0;display:grid;gap:2px}
.mc-fam a,.mc-fam span.row{display:grid;grid-template-columns:12px 34px 40px 1fr auto;align-items:center;gap:8px;padding:4px 6px;margin:0 -6px;border-radius:6px;color:var(--ink);font:500 12.5px var(--mono);font-variant-numeric:tabular-nums}
.mc-fam a:hover{background:var(--sheet-2);text-decoration:none}.mc-fam i{width:10px;height:10px;border-radius:50%;background:var(--st-bg);border:2px solid var(--st)}
.mc-fam .o{color:var(--muted)}.mc-fam .m{text-align:right}.mc-fam .vs{color:var(--st);font:600 12px var(--sans)}
@media (max-width:600px){.mapcard{width:min(360px,calc(100vw - 24px))}}
.mc-more{margin:6px 0 0;font-size:12.5px;color:var(--muted)}
/* a file's numbers: a hero tinted by the verdict, then tiles, each with its accent, a big value and a gauge or bar */
.stats{display:grid;gap:10px;margin:18px 0 0}
.lbl{display:block;font:600 10.5px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.hero{display:grid;grid-template-columns:minmax(0,1.6fr) repeat(2,minmax(0,1fr));border:1px solid var(--rule);border-radius:14px;overflow:hidden;
  background:radial-gradient(120% 160% at 0% 0%,var(--st-bg,var(--sheet-2)),transparent 60%),var(--sheet-2)}
.hero>div{padding:16px 18px;min-width:0}.hero>div+div{border-left:1px solid var(--rule)}
.hero .mc{display:block;margin:5px 0 8px;font:800 clamp(30px,4.4vw,42px)/1 var(--display);font-stretch:88%;letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.hero .hv{display:block;margin-top:7px;font:700 20px var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hero .hc{display:block;margin-top:8px;font-size:12.5px;color:var(--muted)}
.pill{display:inline-flex;align-items:center;gap:5px;font:700 13px var(--mono);padding:4px 10px;border-radius:999px;font-variant-numeric:tabular-nums}
.pill.up{background:var(--ok-bg);color:var(--ok)}.pill.down{background:var(--bad-bg);color:var(--bad)}.pill small{font-weight:500;opacity:.75;margin-left:3px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:10px}
.tile{--tint:var(--rule-2);position:relative;min-width:0;background:var(--sheet-2);border:1px solid var(--rule);border-radius:12px;padding:14px 15px;overflow:hidden}
.tile::before{content:"";position:absolute;left:0;right:0;top:0;height:3px;background:var(--tint)}
.tile::after{content:"";position:absolute;inset:0;background:radial-gradient(90% 80% at 100% 0%,var(--tint),transparent 70%);opacity:.09;pointer-events:none}
.t-curve,.t-stake{--tint:var(--accent)}.t-trades,.t-wd{--tint:var(--ok)}.t-holders{--tint:var(--ink-2)}.t-bal,.t-credit{--tint:var(--warn)}
.tile:has(.tv-v.bad){--tint:var(--bad)}.treasury .tiles{margin-top:14px}
.tv{margin-top:8px}.tv-v{display:block;font:700 24px/1.15 var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tv-v em{font-style:normal;font-size:.55em;font-weight:600;color:var(--muted)}.tv-v.bad{color:var(--bad)}
.tv small{display:block;margin-top:8px;font-size:12.5px;color:var(--muted)}
.gauge{display:block;height:7px;margin-top:10px;border-radius:4px;background:var(--rule);overflow:hidden}
.gauge i{display:block;height:100%;border-radius:4px;background:linear-gradient(90deg,var(--accent),var(--ok))}.gauge.done i{background:var(--ok)}
.split{display:flex;height:7px;margin-top:10px;border-radius:4px;overflow:hidden;background:var(--bad)}.split i.b{background:var(--ok)}.split i.s{flex:1;background:var(--bad)}
.split i.b+i.s{border-left:2px solid var(--sheet-2)}
.lock{font:600 10px var(--mono);letter-spacing:.07em;text-transform:uppercase;padding:1px 6px;border-radius:4px;background:var(--accent-bg);color:var(--accent);margin-right:6px}
.read{margin:14px 0 6px;padding:14px 16px;border:1px solid var(--rule);border-radius:12px;background:linear-gradient(135deg,var(--accent-bg),transparent 75%),var(--sheet-2)}
.read-k{display:flex;align-items:center;gap:8px;margin:0}.read-k .lbl{display:inline}
.kind{font:700 10px var(--mono);letter-spacing:.08em;text-transform:uppercase;padding:2px 8px;border-radius:999px;background:var(--accent);color:var(--sheet)}
.k-meme{background:var(--warn)}.read-line{margin:8px 0 0;font:600 19px/1.4 var(--display);font-stretch:96%;color:var(--ink)}
@media (max-width:700px){.hero{grid-template-columns:1fr 1fr}.hero-mc{grid-column:1/-1}.hero>div+div{border-left:0;border-top:1px solid var(--rule)}
.hero>div:last-child{border-left:1px solid var(--rule)}.tiles{grid-template-columns:1fr 1fr}.tv-v{font-size:20px}.read-line{font-size:17px}}
/* opening a file from the map: the sphere lifts off, grows in the middle of the screen and is read out */
.scan{position:fixed;inset:0;z-index:60;display:grid;place-items:center;cursor:pointer;color:var(--ink)}
.scan-bg{position:absolute;inset:0;background:var(--scrim);-webkit-backdrop-filter:blur(4px);backdrop-filter:blur(4px);animation:fadein .25s ease-out both}
@keyframes fadein{from{opacity:0}}
.scan-stage{position:relative;display:flex;align-items:center;gap:34px;padding:16px;max-width:calc(100vw - 32px)}
.scan .scan-orb{width:min(300px,62vw);max-width:none;height:auto;margin:0;overflow:visible;flex:none;transform-origin:50% 50%}
.scan-orb .hud circle{fill:none;stroke:var(--st);transform-box:fill-box;transform-origin:center}
.scan-orb .h1{stroke-width:.8;stroke-dasharray:1.5 5;opacity:.7;animation:ret 9s linear infinite reverse}
.scan-orb .h2{stroke-width:1.4;stroke-dasharray:60 420;stroke-linecap:round;animation:ret 1.6s cubic-bezier(.5,0,.5,1) infinite}
.scan-orb .bar{animation:sweep .8s .3s ease-in-out infinite alternate both}@keyframes sweep{from{transform:translateY(0)}to{transform:translateY(124px)}}
.scan-read{min-width:0;width:min(340px,84vw);animation:fadein .3s .25s both}
.scan-k{margin:0;font:600 12px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.scan-t{margin:4px 0 12px;display:flex;align-items:baseline;gap:8px;min-width:0}.scan-t b{font:800 28px var(--display);font-stretch:88%}
.scan-t span{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.scan-read ul{list-style:none;margin:0;padding:0;display:grid;gap:7px;font-size:14px}
.scan-read li{display:flex;gap:12px;justify-content:space-between;padding-bottom:6px;border-bottom:1px dashed var(--rule-2);animation:readin .28s ease-out both}
.scan-read li span{color:var(--muted)}.scan-read li b{font:600 14px var(--mono);text-align:right}
.scan-read li.v b{color:var(--st);letter-spacing:.08em;text-transform:uppercase;animation:stampin .4s both;animation-delay:inherit}
.scan-read li.note{justify-content:flex-start;border-bottom:0;padding-bottom:0}.scan-read li.note b{font:500 13.5px var(--sans);text-align:left}
.scan-read li.note::before{content:"";flex:none;width:8px;height:8px;margin-top:6px;border-radius:50%;background:var(--st)}
.scan-read li.ok{--st:var(--ok)}.scan-read li.warn{--st:var(--warn)}.scan-read li.bad{--st:var(--bad)}
@keyframes readin{from{opacity:0;transform:translateX(-8px)}}@keyframes stampin{from{opacity:0;transform:scale(1.6)}}
.scan-go{margin:14px 0 0;font:600 12.5px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--accent);animation:readin .3s both}
@media (max-width:600px){.scan-stage{flex-direction:column;gap:14px}.scan .scan-orb{width:min(210px,56vw)}.scan-t b{font-size:24px}}
.lineup{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 4px}
.lineup a{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;background:var(--st-bg);border:2px solid var(--st);color:var(--st);font:600 11px var(--mono);text-decoration:none}
.lineup a.me{outline:2px solid var(--ink);outline-offset:2px}.lineup-note{margin:4px 0 0}
.card[data-state=verified]{--st-glow:var(--ok-glow);--st-rim:var(--ok)}.card[data-state=scam]{--st-glow:var(--bad-glow);--st-rim:var(--bad)}
.card[data-state=checking]{--st-glow:var(--accent-glow);--st-rim:var(--accent)}
.trader h3{margin:16px 0 6px;font:600 11.5px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}.trader h3:first-of-type{margin-top:8px}
.copynote{margin:0;font-size:13px;color:var(--warn);font-weight:600}
.token-hero{display:flex;align-items:center;gap:18px;margin-bottom:6px}.token-hero h1{margin:0}
.token-hero img{width:104px;height:104px;border-radius:50%;flex:none;box-shadow:0 6px 24px rgba(8,14,26,.25)}
.official{background:var(--sheet);border:1px solid var(--rule);border-left:4px solid var(--warn);border-radius:12px;padding:16px 18px;margin:20px 0}
.official.live{border-left-color:var(--ok)}.official .eyebrow{margin-bottom:6px}.official p{margin:6px 0 0}
.official .big{font:700 20px var(--display);font-stretch:92%;margin:0}
.split{display:flex;height:14px;border-radius:7px;overflow:hidden;margin:16px 0 12px;background:var(--rule)}
.split span{display:block;height:100%}.split span+span{border-left:2px solid var(--bg)}
.c-compute{background:var(--ok)}.c-stake{background:var(--accent)}.c-credit{background:var(--warn)}.c-orbio{background:var(--rule-2)}.c-burn{background:var(--bad)}
.c-treasury{background:var(--accent)}
.split-legend{list-style:none;padding:0;margin:0 0 8px;display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px 20px;font-size:14px}
.split-legend li{max-width:none}.split-legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:7px}
.split-legend span{display:block;color:var(--muted);font-size:13px;margin-top:2px}
.flywheel{list-style:none;padding:0;margin:18px 0 6px;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:26px;counter-reset:fw}
.flywheel li{position:relative;max-width:none;background:var(--sheet);border:1px solid var(--rule);border-radius:12px;padding:12px 14px;counter-increment:fw}
.flywheel li::before{content:counter(fw);font:600 12px var(--mono);color:var(--muted)}
.flywheel b{display:block;margin:3px 0 4px}.flywheel p{margin:0;font-size:13.5px;color:var(--ink-2)}
.flywheel li:not(:last-child)::after{content:"→";position:absolute;right:-19px;top:50%;transform:translateY(-50%);color:var(--muted);font-weight:700}
.loop{font:600 12.5px var(--mono);color:var(--muted);margin:0}
.bars{display:flex;align-items:flex-end;gap:3px;height:60px;margin:14px 0 4px}
.bars span{flex:1;background:var(--accent);border-radius:2px 2px 0 0;min-height:2px}
.bars-axis{display:flex;justify-content:space-between;font:500 11.5px var(--mono);color:var(--muted)}
.behind{color:var(--bad);font-weight:600}
@media (max-width:900px){.flywheel{grid-template-columns:1fr;gap:24px}
.flywheel li:not(:last-child)::after{content:"↓";right:auto;left:50%;top:auto;bottom:-21px;transform:translateX(-50%)}}
@media (max-width:900px){.cols{grid-template-columns:1fr}.how{grid-template-columns:1fr}}
@media (max-width:560px){
.top .wrap{position:relative;flex-direction:column;align-items:stretch;flex-wrap:nowrap;gap:4px;padding-block:12px 2px}
.theme{position:absolute;top:12px;right:16px}
.top nav{flex-wrap:nowrap;overflow-x:auto;gap:2px;margin:0 -16px;padding:0 10px;scrollbar-width:none;
  -webkit-mask-image:linear-gradient(90deg,#000 85%,transparent);mask-image:linear-gradient(90deg,#000 85%,transparent)}
.top nav::-webkit-scrollbar{display:none}.top nav a{flex:none;padding:9px 8px;border-radius:8px}
.tally{display:grid;grid-template-columns:1fr 1fr;gap:16px 18px}
.filters{flex-wrap:nowrap;overflow-x:auto;margin:0 -16px;padding:2px 16px;scrollbar-width:none}
.filters::-webkit-scrollbar{display:none}.filters button{flex:none;padding:8px 13px}
.lmap .bub text.d{display:none}.lmap .bub text.m{display:block}
.lmap .famlabel .pill{transform-box:fill-box;transform-origin:center;transform:scale(1.45)}.lmap .famlabel.pair{display:none}
.mini{padding:7px 10px;font-size:12px}.copy{padding:7px 12px}
th[data-sort] button{padding:8px 0}
.lineup a{width:34px;height:34px}
.list th,.list td{padding:10px 8px}.list .chip{font-size:10px;padding:3px 7px;letter-spacing:.03em}.list .status{font-size:10px;padding:2px 6px}.notfound .more a{display:inline-block;padding:8px 2px}.list th.num,.list td.num{padding-right:2px}
}
@media (max-width:560px){.keynums{grid-template-columns:repeat(2,minmax(0,1fr))}.hide-sm{display:none}.stampbox{flex-direction:row;padding:0}
.stamp.big{font-size:20px;padding:11px 16px 9px}.verdict .why{font-size:17px}.file{padding:18px 16px}.verdict,.project,.score,.safety,.treasury,.evidence{padding:16px}
.tally{gap:10px 24px}.tally dd{font-size:22px}}
"""
DARK = ("--bg:#090d12;--sheet:#10161f;--sheet-2:#151d28;--ink:#e7ecf3;--ink-2:#b4bfcd;--muted:#8792a4;--rule:#212a36;--rule-2:#324050;"
        "--accent:#8fa3ff;--accent-bg:#18214a;--scrim:rgba(6,9,14,.88);"
        "--glass:rgba(18,26,38,.5);--glass-hi:rgba(255,255,255,.08);--glass-spec:rgba(255,255,255,.11);"
        "--rim-hi:rgba(255,255,255,.5);--rim-lo:rgba(255,255,255,.06);"
        "--ok-glow:rgba(57,208,140,.24);--bad-glow:rgba(255,96,114,.22);--accent-glow:rgba(143,163,255,.24);"
        "--amb-a:rgba(143,163,255,.16);--amb-b:rgba(57,208,140,.11);--amb-c:rgba(255,96,114,.09);--ok:#39d08c;--ok-bg:#0e2a1d;--bad:#ff6072;--bad-bg:#301318;--warn:#f3b64b;"
        "--warn-bg:#2c2210;color-scheme:dark")
CSS = CSS.replace("@@DARK@@", DARK)

JS = r"""
(function(){
  var D=window.DOSSIER||{href:'t/{t}.html',live:'api/v1/live.json'},LIVE=window.DOSSIER_LIVE||null;
  // the light/dark switch: dark unless this browser chose light (THEME_BOOT applied that before the page painted)
  var TS=document.querySelector('.theme'),HTML=document.documentElement;
  function themeSync(){if(TS)TS.setAttribute('aria-checked',HTML.getAttribute('data-theme')==='light'?'true':'false')}
  if(TS)TS.addEventListener('click',function(){var light=HTML.getAttribute('data-theme')!=='light';
    if(light)HTML.setAttribute('data-theme','light');else HTML.removeAttribute('data-theme');
    try{localStorage.setItem('dossier.theme',light?'light':'dark')}catch(e){}themeSync()});
  themeSync();
  var LABEL={verified:'Verified',scam:'Impersonator',linked:'Impersonator’s wallet',checking:'Checking',unverified:'Unverified'},SUB='₀₁₂₃₄₅₆₇₈₉';
  var ORDER={verified:3,checking:2,unverified:1,scam:0,linked:0},filterKey='all',VTEXT=@@VTEXT@@;
  // the filter a state falls under (group_of in proof_site.py): an impersonator's wallet with the impersonators
  function grp(s){return s==='linked'?'scam':s==='unverified'?'checking':s}
  function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
  function now(){return Date.now()/1000}
  function href(t){return D.href.replace('{t}',t)}
  function rel(s){var f=s<0,t;s=Math.abs(s);if(s<90)t=f?'in a minute':'just now';else{var m=s/60,h=m/60;
    t=m<60?Math.round(m)+' min':h<36?Math.round(h)+' h':Math.round(h/24)+' days';t=f?'in '+t:t+' ago'}return t}
  function usd(x){if(x==null)return '—';if(x>=1e6)return '$'+(x/1e6).toFixed(2)+'M';if(x>=1e3)return '$'+(x/1e3).toFixed(1)+'k';
    return x>=10?'$'+Math.round(x).toLocaleString('en-US'):'$'+x.toFixed(2)}
  function price(x){if(!x)return '—';if(x>=1)return '$'+x.toFixed(2);if(x>=0.001)return '$'+x.toFixed(4);
    var d=x.toFixed(14).split('.')[1],z=d.length-d.replace(/^0+/,'').length;
    return '$0.0'+String(z).split('').map(function(c){return SUB[+c]}).join('')+d.substr(z,3)}
  function num(x){if(x==null)return '—';if(x>=1e6)return (x/1e6).toFixed(2)+'M';if(x>=1e4)return (x/1e3).toFixed(1)+'k';
    return x>=100?Math.round(x).toLocaleString('en-US'):String(+x.toFixed(2))}
  function chip(s){return '<span class="chip v-'+s+'"><i></i>'+LABEL[s]+'</span>'}
  function meter(r){if(r.g)return '<span class="grad">Graduated</span>';if(r.c==null)return '—';
    return '<span class="meter" title="'+r.c+'% of the way to graduation"><span style="width:'+Math.max(2,Math.min(100,r.c)).toFixed(1)+'%"></span></span>'+r.c.toFixed(1)+'%'}
  function change(r){if(r.ch==null)return '—';var up=r.ch>=0;
    return '<span class="'+(up?'up':'down')+'">'+(up?'+':'')+Math.round(r.ch)+'%</span>'+(r.chh>=23?'':' <small>in '+r.chh+' h</small>')}
  function spark(p,w,h){if(!p||p.length<2)return '';var x0=p[0][0],x1=p[p.length-1][0],ys=p.map(function(q){return q[1]}),
    lo=Math.min.apply(null,ys),hi=Math.max.apply(null,ys),sp=(hi-lo)||hi||1;
    var d='M'+p.map(function(q){return ((q[0]-x0)/((x1-x0)||1)*w).toFixed(1)+','+(h-2-(q[1]-lo)/sp*(h-4)).toFixed(1)}).join(' L');
    return '<svg class="spark '+(ys[ys.length-1]>=ys[0]?'up':'down')+'" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none" aria-hidden="true">'+
      '<path class="area" d="'+d+' L'+w+','+h+' L0,'+h+' Z"/><path class="line" d="'+d+'"/></svg>'}
  var F={
    mcap:function(r){return usd(r.m)},price:function(r){return price(r.p)},vol:function(r){return usd(r.v)},
    curve:meter,change:change,chip:function(r){return chip(r.s)},
    seal:function(r){return r.s==='verified'?'<span class="sealwrap" title="'+esc(VTEXT)+'">@@SEAL@@</span>':chip(r.s)},
    holders:function(r){return r.h==null?'—':r.h+' <small>('+r.hk+')</small>'},
    trades:function(r){return r.b==null?'—':'<span class="up">'+r.b+'</span> / <span class="down">'+r.x+'</span>'},
    trend:function(r){var s=spark(r.sp,120,32);if(!s&&r.v==null)return '';
      return (s||'<span class="none">The chart fills in as snapshots arrive</span>')+'<span class="tv"><b>'+change(r)+'</b>'+usd(r.v)+' 24h vol</span>'},
    chart:function(r){if(!r.sp||r.sp.length<2)return '';var ys=r.sp.map(function(q){return q[1]}),hrs=(r.sp[r.sp.length-1][0]-r.sp[0][0])/3600;
      return '<figure class="chart">'+spark(r.sp,600,96)+'<figcaption><span>Market cap, last '+(hrs<48?Math.max(1,Math.round(hrs))+' h':Math.round(hrs/24)+' days')+
        '</span><span>low '+usd(Math.min.apply(null,ys))+' · high '+usd(Math.max.apply(null,ys))+' · now '+usd(r.m!=null?r.m:ys[ys.length-1])+'</span></figcaption></figure>'},
    tr_bal:function(r){return usd(r.tb)},tr_staked:function(r){return num(r.tk||0)+' ORBIO'},tr_earned:function(r){return num(r.te||0)},
    tr_act:function(r){return num(r.ta||0)},tr_withdrawn:function(r){return num(r.tw||0)+' ORBIO'},
    tr_unlock:function(r){return r.tu?(r.tl?'unlocks '+rel(now()-r.tu):'unlocked'):'—'},
    // the file's stat board: a move pill, and tiles with a big value, a gauge or a bar, and a caption
    chg_pill:function(r){if(r.ch==null)return '';var up=r.ch>=0;
      return '<span class="pill '+(up?'up':'down')+'">'+(up?'▲':'▼')+' '+Math.abs(Math.round(r.ch)).toLocaleString('en-US')+'%<small>'+
        (r.chh>=23?'24 h':'in '+r.chh+' h')+'</small></span>'},
    curve_t:function(r){if(r.g)return '<b class="tv-v">Graduated</b><span class="gauge done"><i style="width:100%"></i></span><small>Trading on its pool now</small>';
      if(r.c==null)return '<b class="tv-v">—</b>';var c=Math.max(0,Math.min(100,r.c));
      return '<b class="tv-v">'+r.c.toFixed(1)+'<em>%</em></b><span class="gauge"><i style="width:'+Math.max(1.5,c).toFixed(1)+'%"></i></span><small>'+
        (c===0?'Nobody has bought yet':'of the way to graduation')+'</small>'},
    trades_t:function(r){if(r.b==null)return '<b class="tv-v">—</b>';var n=r.b+(r.x||0),pb=n?r.b/n*100:50;
      return '<b class="tv-v"><span class="up">'+r.b+'</span><em> / </em><span class="down">'+(r.x||0)+'</span></b><span class="split">'+
        '<i class="b" style="width:'+pb.toFixed(1)+'%"></i><i class="s"></i></span><small>'+(n?Math.round(pb)+'% of trades were buys':'No trades in 24 h')+'</small>'},
    holders_t:function(r){if(r.h==null)return '<b class="tv-v">—</b>';
      return '<b class="tv-v">'+num(r.h)+'</b><small>real buyers after '+({'30m':'30 min','6h':'6 h','24h':'a day','7d':'a week'}[r.hk]||r.hk)+'</small>'},
    bal_t:function(r){return '<b class="tv-v">'+usd(r.tb)+'</b><small>USDG it can spend on AI</small>'},
    vol_where:function(r){return r.v==null?'':r.g?'traded on its pool':'traded on its bonding curve'},
    trt_bal:function(r){return '<b class="tv-v">'+usd(r.tb)+'</b><small>USDG for inference and tools</small>'},
    trt_staked:function(r){return '<b class="tv-v">'+num(r.tk||0)+'<em> ORBIO</em></b><small>'+
      (r.tu?(r.tl?'<span class="lock">Locked</span>unlocks '+rel(now()-r.tu):'Unlocked'):'Staked for CREDIT')+'</small>'},
    trt_earned:function(r){return '<b class="tv-v">'+num(r.te||0)+'<em> CREDIT</em></b><small>minted from fees and earned by the stake</small>'},
    trt_act:function(r){return '<b class="tv-v">'+num(r.ta||0)+'<em> CREDIT</em></b><small>spent into its gateway balance</small>'},
    trt_wd:function(r){var wd=r.tw||0;return '<b class="tv-v'+(wd?' bad':'')+'">'+num(wd)+'<em> ORBIO</em></b><small>'+
      (wd?'taken out of the stake':'Never touched: the stake is intact')+'</small>'}
  };
  function fill(root,r){root.querySelectorAll('[data-l]').forEach(function(el){var f=F[el.dataset.l];if(f)el.innerHTML=f(r)})}
  function fileHref(k){return LIVE&&LIVE.t[k]?href(k):'https://robin.etherscan.io/token/'+k}
  function cardHtml(k,r){var s=r.s,rc=/^https?:\/\//.test(r.rc||'')?r.rc:'';
    return '<li class="card" data-t="'+k+'" data-lt="'+(r.lt||0)+'" data-state="'+grp(s)+'">'+
      '<div class="card-top"><span class="fileno">'+(r.a?'File '+r.a:'Pons launch')+'</span><span class="age">'+rel(now()-r.lt)+'</span>'+
      '<span class="tools"><button type="button" class="mini copy" data-copy="'+k+'">Copy CA</button>'+
      '<button type="button" class="mini star" data-star="'+k+'" aria-pressed="false" aria-label="Add to watchlist">☆</button></span></div>'+
      '<div class="card-title"><a class="tok stretch" href="'+href(k)+'"><b>$'+esc(r.sym||'?')+'</b>'+(r.a?' <span class="no">#'+r.a+'</span>':'')+
      ' <span class="nm">'+esc(r.n)+'</span></a><span data-l="chip"></span></div><div class="trend" data-l="trend"></div>'+
      '<dl class="card-nums"><div><dt>Market cap</dt><dd data-l="mcap"></dd></div><div><dt>Curve</dt><dd data-l="curve"></dd></div>'+
      '<div><dt>Holders</dt><dd data-l="holders"></dd></div></dl><p class="why">'+esc(r.why)+
      (rc?' <a class="rcpt" href="'+esc(rc)+'" rel="nofollow noopener" target="_blank">Receipt</a>':'')+'</p>'+
      (s==='scam'&&r.rl?'<p class="real">Real token: <a class="tok inline" href="'+fileHref(r.rl[0])+'"><b>$'+esc(r.rl[1]||'?')+'</b></a></p>':'')+'</li>'}
  function watchList(){try{return JSON.parse(localStorage.getItem('dossier.watch')||'[]')}catch(e){return []}}
  function saveWatch(a){try{localStorage.setItem('dossier.watch',JSON.stringify(a))}catch(e){}}
  var seen=0;try{seen=+(localStorage.getItem('dossier.seen')||0)}catch(e){}
  function syncStars(){var wl=watchList();document.querySelectorAll('.star').forEach(function(b){var on=wl.indexOf(b.dataset.star)>=0;
    b.setAttribute('aria-pressed',on?'true':'false');b.textContent=b.classList.contains('watch')?(on?'★ Watching':'☆ Watch'):(on?'★':'☆')})}
  function applyFilter(){var list=document.getElementById('feed');if(!list)return;
    list.querySelectorAll('.card').forEach(function(c){c.hidden=filterKey!=='all'&&c.dataset.state!==filterKey});
    MAPS.forEach(function(m){m.querySelectorAll('.bub').forEach(function(b){b.classList.toggle('dim',filterKey!=='all'&&b.dataset.state!==filterKey)})});
    if(mcFor&&mcFor.classList.contains('dim'))hideCard();paginate()}
  // the feed in pages, so the board below stays a short scroll away: 9 cards (3 rows) a page, 6 on a phone. Pages
  // count the cards the filter shows; a new filter starts again at page 1, and new launches land on page 1
  var feedPage=1,PAGE=matchMedia('(max-width: 600px)').matches?6:9;
  function paginate(){var list=document.getElementById('feed');if(!list)return;
    var cards=[].slice.call(list.children).filter(function(c){return c.classList.contains('card')&&!c.hidden}),
      pages=Math.max(1,Math.ceil(cards.length/PAGE));feedPage=Math.min(Math.max(1,feedPage),pages);
    cards.forEach(function(c,i){c.classList.toggle('pg-off',Math.floor(i/PAGE)+1!==feedPage)});
    var nav=document.getElementById('pager');
    if(!nav){nav=document.createElement('nav');nav.id='pager';nav.className='pager';nav.setAttribute('aria-label','Pages of new launches');
      list.parentNode.insertBefore(nav,list.nextSibling)}
    nav.hidden=pages<2;nav.innerHTML=pages<2?'':pagerHtml(feedPage,pages,cards.length,PAGE,'data-page','← Newer','Older →')}
  function pagerHtml(cur,pages,n,per,attr,prev,next){
    var nums=[];for(var p=1;p<=pages;p++){if(p===1||p===pages||Math.abs(p-cur)<=1)nums.push(p);else if(nums[nums.length-1]!=='…')nums.push('…')}
    return '<button type="button" '+attr+'="'+(cur-1)+'"'+(cur===1?' disabled':'')+'>'+prev+'</button>'+
      nums.map(function(p){return p==='…'?'<span class="gap">…</span>':'<button type="button" '+attr+'="'+p+'"'+(p===cur?' aria-current="page"':'')+'>'+p+'</button>'}).join('')+
      '<button type="button" '+attr+'="'+(cur+1)+'"'+(cur===pages?' disabled':'')+'>'+next+'</button>'+
      '<span class="pg-count">'+((cur-1)*per+1)+'–'+Math.min(n,cur*per)+' of '+n+'</span>'}
  // a long table or list in pages (the board: 10 rows a page; impersonators: 5), the rest already hidden by the build.
  // Pages count what a filter leaves; a sort or a new filter starts again at page 1
  function pageList(el){var per=+el.dataset.per,items=[].slice.call(el.tBodies?el.tBodies[0].rows:el.children).filter(function(x){return !x.hidden}),
      pages=Math.max(1,Math.ceil(items.length/per)),cur=Math.min(Math.max(1,+el.dataset.pg||1),pages);el.dataset.pg=cur;
    items.forEach(function(r,i){r.classList.toggle('pg-off',Math.floor(i/per)+1!==cur)});
    var box=el.closest('.scroll')||el,nav=box.nextElementSibling;
    if(!nav||!nav.classList.contains('tpager')){nav=document.createElement('nav');nav.className='pager tpager';
      nav.setAttribute('aria-label',el.dataset.label||'Pages');box.parentNode.insertBefore(nav,box.nextSibling)}
    nav.hidden=pages<2;nav.innerHTML=pages<2?'':pagerHtml(cur,pages,items.length,per,'data-tpage',el.dataset.prev||'← Prev',el.dataset.next||'Next →')}
  document.querySelectorAll('[data-per]').forEach(pageList);
  function setFilter(k){filterKey=k;feedPage=1;document.querySelectorAll('[data-filter]').forEach(function(x){x.setAttribute('aria-pressed',x.dataset.filter===k?'true':'false')});
    var lg=document.querySelector('.maplegend');if(lg)lg.classList.toggle('filtering',k!=='all');applyFilter()}
  function markNew(){if(!seen)return;document.querySelectorAll('.card[data-t]').forEach(function(c){
    if(+c.dataset.lt>seen&&!c.classList.contains('is-new')){c.classList.add('is-new');var f=c.querySelector('.fileno');
      if(f)f.insertAdjacentHTML('afterend','<span class="newbadge">New</span>')}})}
  function addNew(){var list=document.getElementById('feed');if(!list||!LIVE)return;var have={},t=now();
    list.querySelectorAll('.card[data-t]').forEach(function(c){have[c.dataset.t]=1});
    Object.keys(LIVE.t).filter(function(k){var r=LIVE.t[k];return r.a&&r.lt>=t-172800&&!have[k]})
      .sort(function(a,b){return LIVE.t[a].lt-LIVE.t[b].lt}).forEach(function(k){list.insertAdjacentHTML('afterbegin',cardHtml(k,LIVE.t[k]))});
    var empty=document.querySelector('#live .empty');if(empty)empty.hidden=list.children.length>0}
  function renderWatch(){var sec=document.getElementById('watching'),list=document.getElementById('watchlist');if(!sec||!list||!LIVE)return;
    var ks=watchList().filter(function(k){return LIVE.t[k]});sec.hidden=!ks.length;
    list.innerHTML=ks.map(function(k){return cardHtml(k,LIVE.t[k])}).join('')}
  function times(){var t=now();document.querySelectorAll('time[data-ts]').forEach(function(el){el.textContent=rel(t-(+el.dataset.ts))})}
  function apply(){times();if(!LIVE)return;addNew();renderWatch();
    document.querySelectorAll('[data-t]').forEach(function(root){var r=LIVE.t[root.dataset.t];if(!r)return;fill(root,r);
      if(root.classList.contains('card'))root.dataset.state=grp(r.s);
      if(root.classList.contains('bub'))bubble(root,r)});
    var n=LIVE.net||{};document.querySelectorAll('[data-l^="net_"]').forEach(function(el){var k=el.dataset.l;
      el.innerHTML=k==='net_orbio'?price(n.orbio_usd):k==='net_mcap'?usd(n.mcap_usd):k==='net_at'?rel(now()-LIVE.at):'—'});
    if(MAP)spawnNew();if(mcFor)renderCard();markNew();syncStars();applyFilter()}
  // ---- the launch map: a card for the bubble under the pointer (on a phone a tap pins it, a second tap opens the
  // file), a ticker's family lit up together, the legend as a filter, and a pulse on launches from the last hour
  // two views (New, Biggest), each its own svg with its own data and bodies: MAP and the rest below are the one on show
  var MAPS=[].slice.call(document.querySelectorAll('svg.lmap[data-mode]')),BOX=document.querySelector('.lmapbox'),
    MAP=MAPS.filter(function(m){return BOX&&m.dataset.mode===BOX.dataset.view})[0]||MAPS[0]||null,
    MX={t:{},f:[]},mc=null,mcFor=null,mcPinned=false,mcT=0,ptr='mouse';
  MAPS.forEach(function(m){var x={t:{},f:[]};try{x=JSON.parse(document.getElementById('mapx-'+m.dataset.mode).textContent)}catch(e){}
    var vb=m.viewBox.baseVal;m._st={MX:x,BODY:[],ONMAP:{},W:vb.width,H:vb.height};
    // the card replaces the browser's own tooltip; the text stays as the bubble's name for screen readers
    m.querySelectorAll('.bub').forEach(function(b){var t=b.querySelector('title');if(t){b.setAttribute('aria-label',t.textContent);t.remove()}})});
  function useMap(m){MAP=m;var s=m._st;MX=s.MX;BODY=s.BODY;ONMAP=s.ONMAP;W=s.W;H=s.H}
  if(MAP)MX=MAP._st.MX;
  function bubble(b,r){if(!r.s)return;for(var v in LABEL)b.classList.toggle('v-'+v,v===r.s);
    var top=!!b.closest('.lmap[data-mode="top"]');b.dataset.state=r.s==='unverified'&&top?'unverified':grp(r.s);
    var age=r.lt?now()-r.lt:0,fresh=age<3600;b.classList.toggle('fresh',fresh);
    if(fresh&&!b.querySelector('.halo')){var c=b.querySelector('.core'),h=c.cloneNode(false);h.setAttribute('class','halo');b.insertBefore(h,c)}
    if(top)return;  // the Biggest view holds agents of any age: none of them fades
    // older is paler: full strength for the first hour, 45% at 36 hours, then it shrinks and fades out by 48 (the
    // server drops it from the map then; its card stays in the feed and its file on the agents page)
    var HR=3600,fade=age<HR?1:age<36*HR?1-.55*(age-HR)/(35*HR):Math.max(0,.45*(1-(age-36*HR)/(12*HR))),
      shrink=age<36*HR?1:Math.max(.35,1-.65*(age-36*HR)/(12*HR));
    b.style.setProperty('--age',fade.toFixed(3));b.style.setProperty('--shrink',shrink.toFixed(3));b.classList.toggle('gone',fade<.03);
    var body=b.parentNode&&b.parentNode._body;if(body&&!body.lab)body.k=fade<.03?0:shrink}
  function ord(n){var v=n%100;return n+(v>=10&&v<=20?'th':{1:'st',2:'nd',3:'rd'}[n%10]||'th')}
  function mapTarget(n){var el=n&&n.closest?n.closest('.bub,.famlabel,.fam'):null;return el&&MAP.contains(el)&&!el.classList.contains('dim')&&!el.classList.contains('gone')?el:null}
  function closeBtn(){return mcPinned?'<button type="button" class="x" aria-label="Close">×</button>':''}
  function tokCard(k,kb){var r=(LIVE&&LIVE.t[k])||{},x=MX.t[k]||{},s=r.s||'checking',sym='$'+esc(r.sym||'?');
    var h='<div class="mc-top"><span>'+(r.a?'File '+r.a:'Pons launch')+(r.lt?' · '+rel(now()-r.lt):'')+'</span>'+closeBtn()+'</div>'+
      '<p class="mc-title"><b>'+sym+'</b><span class="nm">'+esc(r.n||'')+'</span>'+chip(s)+'</p>'+
      spark(r.sp,280,34)+'<dl class="mc-nums"><div><dt>Market cap</dt><dd>'+usd(r.m)+'</dd></div><div><dt>24h</dt><dd>'+change(r)+'</dd></div>'+
      '<div><dt>Curve</dt><dd>'+(r.g?'<span class="grad">Graduated</span>':r.c==null?'—':r.c.toFixed(1)+'%')+'</dd></div>'+
      '<div><dt>Holders</dt><dd>'+(r.h==null?'—':r.h)+'</dd></div></dl>';
    if(x.n)h+='<ul class="checks">'+x.n.map(function(q){return '<li class="'+esc(q[0])+'">'+esc(q[1])+'</li>'}).join('')+'</ul>';
    if(r.why)h+='<p class="mc-why">'+esc(r.why)+'</p>';
    if(s==='scam'&&r.rl){var real='<b>$'+esc(r.rl[1]||'?')+'</b>';
      h+='<p class="mc-real">Real token: '+(mcPinned?'<a class="tok inline" href="'+esc(fileHref(r.rl[0]))+'">'+real+'</a>':real)+'</p>'}
    return h+(mcPinned?'<div class="mc-act"><a class="btn primary" href="'+esc(href(k))+'" data-open="'+esc(k)+'">Open file →</a>'+
      '<button type="button" class="mini copy" data-copy="'+esc(k)+'">Copy CA</button><button type="button" class="mini star" data-star="'+esc(k)+
      '" aria-pressed="false" aria-label="Add to watchlist">☆</button></div>':'<p class="mc-hint">'+(kb?'Enter opens':'Click for')+' the full file</p>')}
  function famCard(i){var f=MX.f[i];if(!f)return '';var c={verified:0,scam:0,checking:0,unverified:0};
    // on New an unclaimed launch is still being checked; the Biggest view also has ones past their 72 hours
    f.m.forEach(function(k){var s=(LIVE&&LIVE.t[k]&&LIVE.t[k].s)||'checking';c[s==='unverified'&&MX.k==='top'?'unverified':grp(s)]++});
    var sum=[c.verified?c.verified+' verified':'',c.scam?c.scam+' impersonator'+(c.scam>1?'s':''):'',c.checking?c.checking+' checking':'',
      c.unverified?c.unverified+' unverified':''].filter(Boolean).join(' · ');
    var rows=f.m.slice(0,8).map(function(k){var r=(LIVE&&LIVE.t[k])||{},x=MX.t[k]||{},s=r.s||'checking';
      var cells='<i></i><span class="o">'+(x.o?ord(x.o):'')+'</span><span class="ag">'+(r.a?'#'+r.a:'Pons')+'</span>'+
        '<span class="vs">'+LABEL[s]+'</span><span class="m">'+usd(r.m)+'</span>';
      return '<li class="v-'+s+'">'+(mcPinned?'<a href="'+esc(href(k))+'" data-open="'+esc(k)+'">'+cells+'</a>':'<span class="row">'+cells+'</span>')+'</li>'}).join('');
    return '<div class="mc-top"><span>Same ticker</span>'+closeBtn()+'</div><p class="mc-title"><b>$'+esc(f.s)+'</b><span class="nm">'+
      f.m.length+(MX.k==='top'?' among the biggest':' in the last 48 h')+(f.n>f.m.length?', '+f.n+' in all':'')+'</span></p><p class="mc-sum">'+sum+'</p><ol class="mc-fam">'+rows+'</ol>'+
      (f.m.length>8?'<p class="mc-more">and '+(f.m.length-8)+' more in the ring</p>':'')+
      (mcPinned?'':'<p class="mc-hint">In launch order, ranked among every token with the ticker. Click to keep this open.</p>')}
  function light(f){MAP.classList.toggle('famfocus',f!=null);
    MAP.querySelectorAll('[data-f]').forEach(function(x){x.classList.toggle(x.classList.contains('bub')?'hl':'on',f!=null&&x.dataset.f===f)})}
  function anchor(el){var ring=el.classList.contains('famlabel')?MAP.querySelector('.fam[data-f="'+el.dataset.f+'"]'):el;
    return (ring||el).querySelector('.core,.fam>circle')||el}
  function placeCard(){if(!mc||!mcFor)return;var a=anchor(mcFor).getBoundingClientRect(),vw=document.documentElement.clientWidth,
    vh=window.innerHeight,w=mc.offsetWidth,h=mc.offsetHeight,g=10,x,y;
    if(vw<600){x=(vw-w)/2;y=a.bottom+g+h<=vh-8||a.top-g-h<8?a.bottom+g:a.top-g-h}  // a phone: under the bubble, or over it
    else{x=a.right+g+w<=vw-8?a.right+g:a.left-g-w>=8?a.left-g-w:(vw-w)/2;y=a.top+a.height/2-h/2}  // beside it, where there's room
    x=Math.max(8,Math.min(vw-w-8,x));y=Math.max(8,Math.min(vh-h-8,y));
    mc.style.left=Math.round(x+window.pageXOffset)+'px';mc.style.top=Math.round(y+window.pageYOffset)+'px'}
  function renderCard(kb){if(!mc||!mcFor)return;
    mc.innerHTML=mcFor.classList.contains('bub')?tokCard(mcFor.dataset.t,kb):famCard(mcFor.dataset.f);syncStars();placeCard()}
  function showCard(el,pin,kb){clearTimeout(mcT);
    if(!mc){mc=document.createElement('div');mc.className='mapcard';mc.id='mapcard';document.body.appendChild(mc)}
    if(mcFor&&mcFor!==el)mcFor.classList.remove('cur');
    mcFor=el;mcPinned=!!pin;el.classList.add('cur');
    mc.classList.toggle('pinned',mcPinned);mc.setAttribute('role',mcPinned?'dialog':'tooltip');
    if(mcPinned)mc.setAttribute('aria-label','Details');else mc.removeAttribute('aria-label');
    light(el.dataset.f==null?null:el.dataset.f);renderCard(kb);mc.classList.add('on')}
  function hideCard(){clearTimeout(mcT);if(!mc)return;mc.classList.remove('on','pinned');mcPinned=false;
    if(mcFor)mcFor.classList.remove('cur');mcFor=null;light(null)}
  function nearest(cx,cy,px){var best=null,bd=px;  // a phone shows the map at half size: a tap near a small bubble picks it
    MAP.querySelectorAll('.bub:not(.dim):not(.gone)').forEach(function(b){var r=b.querySelector('.core').getBoundingClientRect(),
      d=Math.hypot(cx-r.left-r.width/2,cy-r.top-r.height/2)-r.width/2;if(d<bd){bd=d;best=b}});return best}
  if(MAP)document.addEventListener('pointerdown',function(ev){ptr=ev.pointerType||'mouse'},true);
  MAPS.forEach(function(M){  // only the view on show gets events; the handlers act on MAP, which is that view
    M.addEventListener('pointerover',function(ev){if(ev.pointerType!=='mouse'||mcPinned||held)return;var el=mapTarget(ev.target);if(!el)return;
      if(el.classList.contains('fam')){light(el.dataset.f);return}  // inside a ring: light the family; the card waits for a bubble or the label
      showCard(el,false)});
    M.addEventListener('pointerout',function(ev){if(ev.pointerType!=='mouse'||mcPinned||held)return;var to=mapTarget(ev.relatedTarget);
      if(!to||to.classList.contains('fam')){clearTimeout(mcT);mcT=setTimeout(function(){hideCard();if(to)light(to.dataset.f)},80)}});
    M.addEventListener('click',function(ev){if(dragged){dragged=false;ev.preventDefault();return}
      var el=mapTarget(ev.target),touch=ev.detail!==0&&ptr!=='mouse';
      if(touch&&(!el||el.classList.contains('fam')))el=nearest(ev.clientX,ev.clientY,22)||el;
      if(!el){if(mcPinned)hideCard();return}
      if(el.classList.contains('bub')){if(!touch||(mcPinned&&mcFor===el)){if(!modKey(ev)){ev.preventDefault();openFile(el)}return}
        ev.preventDefault();showCard(el,true);return}
      ev.preventDefault();showCard(el.classList.contains('fam')?MAP.querySelector('.famlabel[data-f="'+el.dataset.f+'"]')||el:el,true)});
    M.addEventListener('focusin',function(ev){var el=mapTarget(ev.target),kb=false;try{kb=!!el&&el.matches(':focus-visible')}catch(e){}
      if(kb&&!mcPinned)showCard(el,false,true)});
    M.addEventListener('focusout',function(ev){if(!mcPinned&&!(mc&&mc.contains(ev.relatedTarget)))hideCard()});
    M.addEventListener('keydown',function(ev){var el=mapTarget(ev.target);
      if(el&&el.classList.contains('famlabel')&&(ev.key==='Enter'||ev.key===' ')){ev.preventDefault();showCard(el,true);
        var a=mc.querySelector('.mc-fam a');if(a)a.focus()}})});
  if(MAP){
    document.addEventListener('keydown',function(ev){if(ev.key!=='Escape'||!mcFor)return;var back=mcFor,inside=mc.contains(document.activeElement);
      hideCard();if(inside)back.focus()});
    window.addEventListener('resize',placeCard);window.addEventListener('hashchange',hideCard)}
  // ---- the map is alive: each body (a lone bubble, or a ring with its bubbles) floats slowly around its place in the
  // layout, bodies push each other apart, and a mouse can drag one and flick it. A soft spring brings every body home,
  // so the layout keeps its meaning. A phone gets the float but not the drag: a finger on the map scrolls the page
  var BODY=[],W=0,H=0,raf=0,lastT=0,onScreen=true,held=null,drag=null,dragged=false,ONMAP={},NS='http://www.w3.org/2000/svg',
    CALM=!!(window.matchMedia&&matchMedia('(prefers-reduced-motion: reduce)').matches);
  function rnd(a,b){return a+Math.random()*(b-a)}
  function addBody(g,x,y,r){var f=g.querySelector('.fam'),b={g:g,lab:f?MAP.querySelector('.famlabel[data-f="'+f.dataset.f+'"]'):null,
    hx:x,hy:y,x:x,y:y,vx:0,vy:0,r:r,k:1,m:r*r,a:Math.min(5,2+r*.05),w1:rnd(4e-4,8e-4),w2:rnd(3e-4,7e-4),p1:rnd(0,6.3),p2:rnd(0,6.3),dx:NaN,dy:NaN};
    g._body=b;BODY.push(b);g.querySelectorAll('.bub').forEach(function(a){ONMAP[a.dataset.t]=1});return b}
  function drawBody(b){if(Math.abs(b.x-b.dx)<.02&&Math.abs(b.y-b.dy)<.02)return;b.dx=b.x;b.dy=b.y;
    var tr='translate('+b.x.toFixed(2)+' '+b.y.toFixed(2)+')';b.g.setAttribute('transform',tr);if(b.lab)b.lab.setAttribute('transform',tr)}
  function moving(b){return b.vx*b.vx+b.vy*b.vy>.004}
  function kick(){if(!raf&&onScreen&&!document.hidden&&BODY.length)raf=requestAnimationFrame(step)}
  function step(t){raf=0;if(!onScreen||document.hidden){lastT=0;return}
    var busy=!!held||BODY.some(moving);
    if(!busy&&lastT&&t-lastT<32){raf=requestAnimationFrame(step);return}  // only floating: 30 frames a second is plenty
    var f=lastT?Math.min(3,(t-lastT)/16.7):1;lastT=t;
    BODY.forEach(function(b){if(b===held)return;var still=CALM||(mcFor&&(b.g.contains(mcFor)||b.lab===mcFor));  // the one you look at holds still
      var tx=b.hx+(still?0:b.a*Math.sin(t*b.w1+b.p1)),ty=b.hy+(still?0:b.a*Math.cos(t*b.w2+b.p2));
      b.vx+=(tx-b.x)*.004*f;b.vy+=(ty-b.y)*.004*f});
    collide();var damp=Math.pow(.9,f);
    BODY.forEach(function(b){if(b===held)return;b.vx*=damp;b.vy*=damp;b.x+=b.vx*f;b.y+=b.vy*f});
    walls();BODY.forEach(drawBody);
    if(mcFor&&mc&&mc.classList.contains('on'))placeCard();
    if(CALM&&!busy){lastT=0;return}  // reduced motion: nothing floats, so sleep until something is dragged or lands
    raf=requestAnimationFrame(step)}
  function collide(){for(var i=0;i<BODY.length;i++){var a=BODY[i],ra=a.r*a.k;if(!ra)continue;
    for(var j=i+1;j<BODY.length;j++){var b=BODY[j],rb=b.r*b.k;if(!rb)continue;var dx=b.x-a.x,dy=b.y-a.y,min=ra+rb+3,d2=dx*dx+dy*dy;
      if(d2>=min*min)continue;var dd=Math.sqrt(d2)||.01,nx=dx/dd,ny=dy/dd,o=min-dd,
        sa=a===held?0:b===held?1:b.m/(a.m+b.m),sb=1-sa;  // the lighter one gives way; a held one doesn't
      a.x-=nx*o*sa;a.y-=ny*o*sa;b.x+=nx*o*sb;b.y+=ny*o*sb;
      var rv=(b.vx-a.vx)*nx+(b.vy-a.vy)*ny;if(rv<0){var im=-1.2*rv;a.vx-=im*sa*nx;a.vy-=im*sa*ny;b.vx+=im*sb*nx;b.vy+=im*sb*ny}}}}
  function walls(){BODY.forEach(function(b){var r=b.r*b.k,top=r+(b.lab?18:0);
    if(b.x<r){b.x=r;b.vx=Math.abs(b.vx)*.4}else if(b.x>W-r){b.x=W-r;b.vx=-Math.abs(b.vx)*.4}
    if(b.y<top){b.y=top;b.vy=Math.abs(b.vy)*.4}else if(b.y>H-r){b.y=H-r;b.vy=-Math.abs(b.vy)*.4}})}
  function svgPt(ev){var m=MAP.getScreenCTM();return m?{x:(ev.clientX-m.e)/m.a,y:(ev.clientY-m.f)/m.d}:{x:0,y:0}}
  function bodyOf(el){if(el&&el.classList.contains('famlabel'))el=MAP.querySelector('.fam[data-f="'+el.dataset.f+'"]');
    var g=el&&el.closest('.body');return (g&&g._body)||null}
  // the page script's copy of orb() in proof_site.py: keep the two in step
  function orbSVG(x,y,r){var f=function(v){return v.toFixed(1)},c='cx="'+f(x)+'" cy="'+f(y)+'"',sx=x-r*.24,sy=y-r*.5,grid='';
    if(r>=17)grid='<g class="grid" transform="rotate(-18 '+f(x)+' '+f(y)+')"><ellipse '+c+' rx="'+f(r*.97)+'" ry="'+f(r*.3)+'"/>'+
      '<ellipse class="mer" '+c+' rx="'+f(r*.97)+'" ry="'+f(r*.97)+'"/><ellipse class="mer b" '+c+' rx="'+f(r*.97)+'" ry="'+f(r*.97)+'"/></g>';
    return '<circle class="glow" '+c+' r="'+f(r*1.3)+'"/><circle class="ret" '+c+' r="'+f(r*1.17+1.5)+'"/><circle class="core" '+c+' r="'+f(r)+'"/>'+
      '<circle class="skin" '+c+' r="'+f(r)+'"/><circle class="cau" '+c+' r="'+f(r)+'"/>'+grid+
      '<ellipse class="spec" cx="'+f(sx)+'" cy="'+f(sy)+'" rx="'+f(r*.44)+'" ry="'+f(r*.22)+'" transform="rotate(-28 '+f(sx)+' '+f(sy)+')"/>'+
      '<circle class="glint" cx="'+f(x-r*.5)+'" cy="'+f(y-r*.36)+'" r="'+f(Math.max(.8,r*.06))+'"/>'}
  // a launch that lands while the page is open drops onto the map, in the free spot nearest the middle (it joins its
  // ticker's ring when the page is next rebuilt)
  function freeSpot(r){var best=null,bd=Infinity;
    for(var y=r+4;y<=H-r-4;y+=7)for(var x=r+4;x<=W-r-4;x+=7){var ok=true;
      for(var i=0;i<BODY.length&&ok;i++){var b=BODY[i],m=b.r*b.k+r+6+(b.lab?10:0),dx=x-b.x,dy=y-b.y;if(dx*dx+dy*dy<m*m)ok=false}
      if(ok){var d=(x-W/2)*(x-W/2)+(y-H/2)*(y-H/2);if(d<bd){bd=d;best={x:x,y:y}}}}
    return best}
  function spawnNew(){if(!LIVE||!MAP||MAP.dataset.mode!=='new'||!BODY.length)return;var t=now(),n=0;  // New only, when on show
    Object.keys(LIVE.t).forEach(function(k){var r=LIVE.t[k];if(n>=6||ONMAP[k]||!r.a||!r.lt||t-r.lt>6*3600)return;
      var rr=Math.max(8,Math.min(44,7+4.2*Math.sqrt(Math.max(0,(r.m||0)-(MX.b||0))/1000))),p=freeSpot(rr);ONMAP[k]=1;if(!p)return;n++;
      var s=r.s||'checking',g=document.createElementNS(NS,'g');g.setAttribute('class','body');
      g.innerHTML='<a class="bub born v-'+s+'" href="'+esc(href(k))+'" data-t="'+esc(k)+'" data-state="'+grp(s)+
        '" aria-label="$'+esc(r.sym||'?')+' #'+esc(r.a)+' · '+LABEL[s]+'">'+orbSVG(0,0,rr)+(rr>=17?'<text class="d" x="0" y="0" style="font-size:'+
        Math.min(13,rr*.42).toFixed(1)+'px">'+esc((r.sym||'?').slice(0,7))+'</text>':'')+'</a>';
      MAP.querySelector('.bodies').appendChild(g);drawBody(addBody(g,p.x,p.y,rr));bubble(g.firstChild,r)});
    if(n){applyFilter();kick()}}
  // the switch between the views: the one going away keeps its bodies where they are, frozen until it's back
  function setView(v){var m=MAPS.filter(function(x){return x.dataset.mode===v})[0];if(!m||m===MAP)return;
    hideCard();if(drag){drag=null;held=null;MAP.classList.remove('dragging')}
    BOX.dataset.view=v;BOX.querySelectorAll('.mapview [data-view]').forEach(function(b){b.setAttribute('aria-pressed',b.dataset.view===v?'true':'false')});
    if(filterKey==='unverified'&&v!=='top')setFilter('all');  // only the Biggest view has unclaimed agents past 72 h
    useMap(m);lastT=0;spawnNew();kick()}
  if(MAP){
    var shown=MAP;  // each view's bodies go into its own list (useMap points BODY at it), then back to the one on show
    MAPS.forEach(function(m){useMap(m);m.querySelectorAll('.body').forEach(function(g){addBody(g,+g.dataset.x,+g.dataset.y,+g.dataset.r)})});
    useMap(shown);
    MAPS.forEach(function(M){
    M.addEventListener('pointerdown',function(ev){dragged=false;if(ev.pointerType==='touch'||ev.button!==0||scanning)return;
      var b=bodyOf(mapTarget(ev.target));if(!b)return;var p=svgPt(ev);
      drag={b:b,id:ev.pointerId,sx:ev.clientX,sy:ev.clientY,ox:p.x-b.x,oy:p.y-b.y,on:false,px:b.x,py:b.y,pt:performance.now(),vx:0,vy:0};
      ev.preventDefault()});
    M.addEventListener('dragstart',function(ev){ev.preventDefault()})});
    window.addEventListener('pointermove',function(ev){if(!drag||ev.pointerId!==drag.id)return;
      if(!drag.on){if(Math.abs(ev.clientX-drag.sx)+Math.abs(ev.clientY-drag.sy)<6)return;
        drag.on=true;held=drag.b;hideCard();MAP.classList.add('dragging');try{MAP.setPointerCapture(ev.pointerId)}catch(e){}}
      var p=svgPt(ev),b=drag.b,t=performance.now(),dt=Math.max(4,t-drag.pt);b.x=p.x-drag.ox;b.y=p.y-drag.oy;
      drag.vx=drag.vx*.4+.6*(b.x-drag.px)/dt*16.7;drag.vy=drag.vy*.4+.6*(b.y-drag.py)/dt*16.7;drag.px=b.x;drag.py=b.y;drag.pt=t;kick()});
    var letGo=function(ev){if(!drag||ev.pointerId!==drag.id)return;var d=drag;drag=null;if(!d.on)return;
      held=null;dragged=true;MAP.classList.remove('dragging');
      var flick=performance.now()-d.pt<90,v=Math.hypot(d.vx,d.vy),cap=v>14?14/v:1;  // held still before letting go: no flick
      d.b.vx=flick?d.vx*cap:0;d.b.vy=flick?d.vy*cap:0;kick()};
    window.addEventListener('pointerup',letGo);window.addEventListener('pointercancel',letGo);
    if('IntersectionObserver' in window){var io=new IntersectionObserver(function(es){
      es.forEach(function(x){if(x.target===MAP)onScreen=x.isIntersecting});kick()});MAPS.forEach(function(m){io.observe(m)})}
    document.addEventListener('visibilitychange',function(){if(!document.hidden){lastT=0;kick()}});
    kick()}
  // ---- opening a file from the map: the sphere lifts off, grows in the middle of the screen, and a scanner reads out
  // what its file says while the file loads. A click or a key skips it; with reduced motion the file simply opens
  var scanning=null;
  function modKey(ev){return ev.ctrlKey||ev.metaKey||ev.shiftKey||ev.altKey||(ev.button||0)>0}
  function openFile(b){var k=b.dataset.t,url=href(k);
    if(CALM||scanning||!Element.prototype.animate){location.href=url;return}
    hideCard();var r=(LIVE&&LIVE.t[k])||{},x=MX.t[k]||{},s=r.s||b.dataset.state||'checking';
    var pf=document.createElement('link');pf.rel='prefetch';pf.href=url;document.head.appendChild(pf);
    var rows=[['Contract',k.slice(0,6)+'…'+k.slice(-4)],['Market cap',usd(r.m)],['Holders',r.h==null?'—':num(r.h)],
      ['Curve',r.g?'Graduated':r.c==null?'—':r.c.toFixed(1)+'%'],['Verdict',LABEL[s],'v v-'+s]]
      .concat((x.n||[]).slice(0,2).map(function(q){return ['',q[1],'note '+q[0]]}));
    var T0=450,STEP=90,el=document.createElement('div');el.className='scan';el.setAttribute('role','status');
    el.innerHTML='<div class="scan-bg"></div><div class="scan-stage"><svg class="lmap scan-orb" viewBox="-80 -80 160 160" aria-hidden="true">'+
      '<defs><clipPath id="scan-clip"><circle r="56"/></clipPath></defs><g class="bub cur v-'+s+'">'+orbSVG(0,0,56)+'</g>'+
      '<g class="hud v-'+s+'"><circle class="h1" r="70"/><circle class="h2" r="76"/>'+
      '<g clip-path="url(#scan-clip)"><rect class="bar" x="-56" y="-74" width="112" height="18"/></g></g></svg>'+
      '<div class="scan-read"><p class="scan-k">'+(r.a?'File '+esc(r.a):'Pons launch')+' · reading the file</p><p class="scan-t"><b>$'+
      esc(r.sym||'?')+'</b><span>'+esc(r.n||'')+'</span></p><ul>'+rows.map(function(q,i){return '<li class="'+esc(q[2]||'')+
      '" style="animation-delay:'+(T0+i*STEP)+'ms">'+(q[0]?'<span>'+esc(q[0])+'</span>':'')+'<b>'+esc(q[1])+'</b></li>'}).join('')+
      '</ul><p class="scan-go" style="animation-delay:'+(T0+rows.length*STEP+60)+'ms">Opening the file →</p></div></div>';
    document.body.appendChild(el);b.classList.add('lifted');scanning={el:el,b:b};
    // FLIP: start the big sphere exactly over the bubble, then let it grow into place
    var orb=el.querySelector('.scan-orb'),a=b.querySelector('.core').getBoundingClientRect(),o=orb.getBoundingClientRect();
    orb.animate([{transform:'translate('+(a.left+a.width/2-o.left-o.width/2)+'px,'+(a.top+a.height/2-o.top-o.height/2)+'px) scale('+
      Math.max(.05,a.width/(o.width*.7))+')'},{transform:'none'}],{duration:560,easing:'cubic-bezier(.2,.85,.25,1)'});
    var go=function(){if(scanning&&scanning.el===el){clearTimeout(scanning.t);location.href=url}};
    scanning.go=go;scanning.t=setTimeout(go,T0+rows.length*STEP+450);el.addEventListener('click',go)}
  function unscan(){if(!scanning)return;clearTimeout(scanning.t);scanning.el.remove();scanning.b.classList.remove('lifted');scanning=null}
  document.addEventListener('keydown',function(ev){if(scanning&&!/^(Shift|Control|Alt|Meta)$/.test(ev.key)){ev.preventDefault();scanning.go()}},true);
  // ---- the case files: a folder that opens on its index, each case a document that turns over to its findings. The
  // faces arrive in reading order; on a wide screen two make a leaf (front on the right, back on the left once turned),
  // on a phone each face is a sheet of its own. The end of the file sits under every leaf, on the folder's back
  (function(){var CF=document.querySelector('.casefolder'),ctl=document.querySelector('.cf-controls');if(!CF||!ctl)return;
    var faces=[].slice.call(CF.children).filter(function(f){return f.classList.contains('cf-face')}),end=faces.pop(),
      prev=ctl.querySelector('.cf-prev'),next=ctl.querySelector('.cf-next'),count=ctl.querySelector('.cf-count'),
      n=+CF.dataset.n||0,mode='',leaves=[],pos=0,busy=0,TURN=matchMedia('(prefers-reduced-motion: reduce)').matches?0:850;
    var back=document.createElement('div');back.className='cf-back';back.appendChild(end);CF.appendChild(back);
    function z(i){return leaves[i].classList.contains('turned')?i+1:2*leaves.length-i}
    function build(){var single=matchMedia('(max-width: 760px)').matches,m=single?'single':'book';if(m===mode)return;
      // keep the reader on the same page across a rotation: a book leaf holds two faces, a sheet one
      if(mode)pos=single?Math.min(pos*2,faces.length):Math.ceil(pos/2);mode=m;
      leaves.forEach(function(l){l.remove()});leaves=[];
      var groups=[];for(var i=0;i<faces.length;i+=single?1:2)groups.push(faces.slice(i,i+(single?1:2)));
      groups.forEach(function(g,i){var l=document.createElement('div');l.className='cf-leaf'+(g[0].classList.contains('cf-doc')?' paper':'');
        g.forEach(function(f,k){f.classList.toggle('back',k===1);l.appendChild(f)});CF.appendChild(l);leaves.push(l)});
      CF.classList.add('ready');CF.classList.toggle('single',single);
      leaves.forEach(function(l,i){l.classList.toggle('turned',i<pos);l.style.zIndex=z(i)});show()}
    function show(){CF.classList.toggle('closed',mode==='book'&&pos===0);
      // only what's in view takes focus and clicks
      var seen=mode==='book'?[pos>0&&leaves[pos-1].lastChild,leaves[pos]&&leaves[pos].firstChild]:[leaves[pos]&&leaves[pos].firstChild];
      faces.forEach(function(f){f.inert=seen.indexOf(f)<0});end.inert=pos<leaves.length;
      prev.disabled=pos===0;next.disabled=pos>=leaves.length;ctl.hidden=false;
      var doc=mode==='book'?pos:Math.floor(pos/2);
      count.textContent=pos===0?'Closed':pos>=leaves.length?'End of file':(mode==='single'&&pos===1)?'Index':
        'File '+Math.min(doc,n)+' of '+n+(mode==='single'&&pos%2?' · findings':'')}
    function turn(i,fwd){var l=leaves[i];if(!l)return;l.style.zIndex=3*leaves.length;l.classList.add('moving');
      l.classList.toggle('turned',fwd);setTimeout(function(){l.classList.remove('moving');l.style.zIndex=z(i)},TURN+30)}
    function go(to){to=Math.max(0,Math.min(leaves.length,to));if(to===pos||busy)return;busy=1;
      var fwd=to>pos,steps=Math.abs(to-pos),k=0;
      (function stepOnce(){turn(fwd?pos:pos-1,fwd);pos+=fwd?1:-1;show();
        if(++k<steps)setTimeout(stepOnce,TURN?150:0);else setTimeout(function(){busy=0},TURN?TURN*.6:0)})()}
    next.addEventListener('click',function(){go(pos+1)});prev.addEventListener('click',function(){go(pos-1)});
    CF.addEventListener('click',function(ev){var g=ev.target.closest('[data-goto]');
      if(g){ev.preventDefault();var k=+g.dataset.goto;go(mode==='book'?k:2*k);return}
      if(pos===0&&ev.target.closest('.cf-cover'))go(1)});
    document.addEventListener('keydown',function(ev){if(ev.target.closest&&ev.target.closest('input,textarea'))return;
      var r=CF.getBoundingClientRect();if(r.bottom<0||r.top>innerHeight)return;
      if(ev.key==='ArrowRight'){ev.preventDefault();go(pos+1)}else if(ev.key==='ArrowLeft'){ev.preventDefault();go(pos-1)}});
    var sx=null;CF.addEventListener('pointerdown',function(ev){sx=ev.target.closest('a,button')?null:ev.clientX});
    CF.addEventListener('pointerup',function(ev){if(sx==null)return;var dx=ev.clientX-sx;sx=null;
      if(Math.abs(dx)>40)go(pos+(dx<0?1:-1))});
    build();addEventListener('resize',build)})();
  function load(){if(!D.live||!window.fetch)return spare();fetch(D.live+(D.live.indexOf('?')<0?'?':'&')+'_='+Date.now(),{cache:'no-store'})
    .then(function(r){return r.ok?r.json():null}).then(function(j){if(j&&j.t){LIVE=j;SPARE=null;apply()}else spare()}).catch(spare)}
  function spare(){if(!LIVE&&SPARE){LIVE=SPARE;SPARE=null;apply()}}  // no fresh numbers to be had: old ones beat none
  var toastEl=document.querySelector('.toast'),toastT;
  function toast(m){if(!toastEl)return;toastEl.textContent=m;toastEl.hidden=false;clearTimeout(toastT);toastT=setTimeout(function(){toastEl.hidden=true},2200)}
  function copyText(b){var v=b.dataset.copy,label=b.textContent;
    function done(){b.textContent='Copied';setTimeout(function(){b.textContent=label},1500)}
    function fallback(){var t=document.createElement('textarea');t.value=v;t.setAttribute('readonly','');t.style.position='fixed';t.style.opacity='0';
      document.body.appendChild(t);t.select();try{document.execCommand('copy')}catch(e){}document.body.removeChild(t);done()}
    if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(v).then(done,fallback);else fallback()}
  function sortVal(row,key){var r=(LIVE&&LIVE.t[row.dataset.t])||{};switch(key){
    case 'mcap':return r.m||0;case 'vol':return r.v||0;case 'curve':return r.g?101:(r.c||0);case 'age':return +row.dataset.lt||0;
    case 'verdict':return ORDER[r.s||row.dataset.state]||0;case 'rank':return +row.dataset.rank||0;
    case 'score':return +row.dataset.score||0;default:return key in row.dataset?+row.dataset[key]||0:row.dataset.sym||''}}
  function sortBy(th){var tb=th.closest('table').tBodies[0],key=th.dataset.sort,cur=th.getAttribute('aria-sort');
    var desc=cur?cur==='ascending':key!=='name'&&key!=='rank';  // biggest first, but a name or a place from the top
    th.closest('tr').querySelectorAll('th').forEach(function(x){x.removeAttribute('aria-sort')});
    th.setAttribute('aria-sort',desc?'descending':'ascending');
    var rows=[].slice.call(tb.rows);rows.sort(function(a,b){var x=sortVal(a,key),y=sortVal(b,key),c=typeof x==='string'?x.localeCompare(y):x-y;return desc?-c:c});
    rows.forEach(function(r){tb.appendChild(r)});var tbl=th.closest('table');if(tbl.dataset.per){tbl.dataset.pg=1;pageList(tbl)}}
  var q=document.getElementById('q'),res=document.getElementById('results'),idx=document.getElementById('idx');
  var all=idx?JSON.parse(idx.textContent):[];
  document.addEventListener('click',function(ev){
    if(mcPinned&&(ev.target.closest('.mapcard .x')||!(mc.contains(ev.target)||MAP.contains(ev.target)))){hideCard();if(ev.target.closest('.mapcard'))return}
    var op=ev.target.closest('[data-open]');if(op&&MAP&&!modKey(ev)){var ob=MAP.querySelector('.bub[data-t="'+op.dataset.open+'"]');
      if(ob){ev.preventDefault();openFile(ob);return}}
    var cp=ev.target.closest('[data-copy]');if(cp){ev.preventDefault();copyText(cp);return}
    var st=ev.target.closest('.star');if(st){ev.preventDefault();var wl=watchList(),k=st.dataset.star,i=wl.indexOf(k);
      if(i>=0)wl.splice(i,1);else wl.push(k);saveWatch(wl);syncStars();renderWatch();toast(i>=0?'Removed from your watchlist':'Added to your watchlist');return}
    // the filter buttons, and the map's legend, which toggles: a second click on the same key shows everything again
    var fb=ev.target.closest('[data-filter]');if(fb){var k=fb.dataset.filter;setFilter(fb.closest('.maplegend')&&filterKey===k?'all':k);return}
    var sb=ev.target.closest('th[data-sort] button');if(sb){sortBy(sb.parentNode);return}
    var pg=ev.target.closest('#pager [data-page]');if(pg){feedPage=+pg.dataset.page;paginate();
      var fl=document.getElementById('feed');if(fl)scrollTo({top:fl.getBoundingClientRect().top+scrollY-90,behavior:CALM?'auto':'smooth'});return}
    var tp=ev.target.closest('.tpager [data-tpage]');if(tp){var box=tp.parentNode.previousElementSibling,el=box.dataset.per?box:box.querySelector('[data-per]');
      el.dataset.pg=tp.dataset.tpage;pageList(el);var top=box.getBoundingClientRect().top;
      if(top<0)scrollTo({top:top+scrollY-90,behavior:CALM?'auto':'smooth'});return}
    var vw=ev.target.closest('.mapview [data-view]');if(vw){setView(vw.dataset.view);return}
    if(res&&!res.contains(ev.target)&&ev.target!==q)res.hidden=true});
  document.querySelectorAll('.filter').forEach(function(f){f.addEventListener('input',function(){
    var v=f.value.trim().toLowerCase(),scope=f.closest('.view')||document;
    scope.querySelectorAll('[data-search]').forEach(function(r){r.hidden=!!v&&r.dataset.search.indexOf(v)<0});
    scope.querySelectorAll('[data-per]').forEach(function(x){x.dataset.pg=1;pageList(x)})})});
  document.addEventListener('paste',function(ev){var t=ev.target;
    if(t&&((t.tagName==='INPUT'&&t!==q)||t.tagName==='TEXTAREA'||t.isContentEditable))return;
    var txt=(ev.clipboardData||window.clipboardData||{getData:function(){return ''}}).getData('text')||'',m=txt.match(/0x[0-9a-fA-F]{40}/);
    if(!m)return;var a=m[0].toLowerCase(),known=(LIVE&&LIVE.t[a])||all.some(function(x){return x.t===a});
    if(known){ev.preventDefault();location.href=href(a)}else if(t!==q)toast('That contract isn’t on file yet.')});
  if(q&&res){
    q.addEventListener('input',function(){var v=q.value.trim().toLowerCase().replace(/^\$/,'');
      if(!v){res.hidden=true;res.innerHTML='';return}
      var hits=all.filter(function(t){return t.t.indexOf(v)===0||(t.s||'').toLowerCase().indexOf(v)===0||(t.n||'').toLowerCase().indexOf(v)>=0}).slice(0,8);
      res.innerHTML=hits.length?hits.map(function(t){return '<a href="'+href(t.t)+'"><b>$'+esc(t.s)+'</b>'+(t.a?'<span class="muted">#'+t.a+'</span>':'')+
        '<span class="muted">'+esc(t.n)+'</span>'+chip(t.k)+'</a>'}).join('')
        :'<div class="none">'+(/^0x[0-9a-f]{40}$/.test(v)?'Not on file yet. Dossier covers Orbio agents and the tokens projects claim.':'No match.')+'</div>';
      res.hidden=false});
    q.addEventListener('keydown',function(ev){if(ev.key==='Enter'){var a=res.querySelector('a');if(a){ev.preventDefault();location.href=a.getAttribute('href')}}})}
  // api/v1/live.js is rewritten on every pass of the index, a minute or two apart, so a copy much older than that came
  // out of a cache (a browser keeps one for hours when a CDN rewrites its headers). Don't paint it: it would show old
  // market caps, and could turn verdicts the page already has back to older ones. Fetch the numbers now instead, and
  // fall back to the old copy only if that fails.
  var SPARE=null;if(LIVE&&now()-LIVE.at>180){SPARE=LIVE;LIVE=null}
  apply();if(!LIVE)load();setInterval(load,60000);setInterval(times,60000);
  document.addEventListener('visibilitychange',function(){if(!document.hidden)load()});
  window.addEventListener('pageshow',function(ev){if(ev.persisted){unscan();load()}});  // back to a page the browser kept
  setTimeout(function(){try{localStorage.setItem('dossier.seen',String(Math.floor(now())))}catch(e){}},4000);
})();
"""
JS = JS.replace("@@SEAL@@", SEAL).replace("@@VTEXT@@", json.dumps(STATE_TEXT["verified"]))


# ----------------------------------------------------------------- build

def build(feed: dict, out: Path = OUT) -> int:
    """Write the whole site into a fresh folder, then swap it in, so a host serving `out` never sees half a site."""
    tmp = out.with_name(out.name + ".new")
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "assets").mkdir(parents=True)
    (tmp / "t").mkdir()
    (tmp / "api" / "v1" / "tokens").mkdir(parents=True)
    (tmp / "assets" / "style.css").write_text(CSS.strip() + "\n", "utf-8")
    (tmp / "assets" / "app.js").write_text(JS.strip() + "\n", "utf-8")

    def put(rel: str, title: str, body: str, depth: int = 0, desc: str = "") -> None:
        (tmp / rel).write_text(page(title, body, feed, depth, desc, path=rel), "utf-8")

    now = feed["generated_at"]
    ON_FILE.clear()
    ON_FILE.update(t["token"] for t in feed["tokens"])
    TERMS.clear()
    TERMS.update((feed.get("network") or {}).get("fee_terms") or {})
    CASES[:] = load_cases()  # before any page: the header links them
    put("index.html", f"{NAME}: which Orbio launch is the real one?", home(feed))
    if CASES:
        (tmp / "case").mkdir()
        put("cases.html", f"Case files · {NAME}", cases_page(CASES, feed),
            desc="The deep dives behind Dossier's verdicts: following the money on-chain, every claim with its receipt.")
        for c in CASES:
            put(f'case/{c["slug"]}.html', f'{c.get("title")} · {NAME}', case_page(c, feed), depth=1, desc=c.get("dek") or "")
    put("agents.html", f"Every Orbio agent · {NAME}", agents_page(feed))
    put("scams.html", f"Impersonators caught · {NAME}", scams_page(feed))
    put("method.html", f"Method · {NAME}", method_page(), desc="How Dossier decides which tokens are real.")
    put("api.html", f"API · {NAME}", api_page(feed))
    put("token.html", f"$DOSSIER · {NAME}", strategy_page(feed),
        desc="How $DOSSIER's trading fees pay for Dossier's checks, and where passes, buybacks and burns go.")
    # served by the host for any address with no file (Cloudflare Pages would otherwise serve the home page)
    (tmp / "404.html").write_text(page(f"No file here · {NAME}", not_found_page(), feed, path=None, root="/"), "utf-8")
    for src in (OG_IMAGE, LOGO):
        if src.exists():
            shutil.copyfile(src, tmp / "assets" / src.name)
    site = site_url()
    (tmp / "robots.txt").write_text("User-agent: *\nAllow: /\n" + (f"Sitemap: {site}/sitemap.xml\n" if site else ""), "utf-8")
    if site:
        live_cases = [c for c in CASES if c.get("status") == "published"]  # never a draft, even on the dev server
        pages = ["", "agents.html", "scams.html", "token.html", "method.html", "api.html"] + [f't/{t["token"]}.html' for t in feed["tokens"]] \
            + (["cases.html"] + [f'case/{c["slug"]}.html' for c in live_cases] if live_cases else [])
        (tmp / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                                         + "".join(f"<url><loc>{e(site)}/{p}</loc></url>\n" for p in pages) + "</urlset>\n", "utf-8")
    for t in feed["tokens"]:
        s = STATE[state_of(t, now)]
        put(f't/{t["token"]}.html', f'${t["symbol"]}: {s} · {NAME}', token_page(t, feed), depth=1,
            desc=f'{s}: {sentence(t["verdict"]["why"])}')
        (tmp / "api" / "v1" / "tokens" / f'{t["token"]}.json').write_text(
            json.dumps({"method": feed["method"], "generated_at": feed["generated_at"], **t}, ensure_ascii=False), "utf-8")
    (tmp / "api" / "v1" / "feed.json").write_text(json.dumps(feed, ensure_ascii=False), "utf-8")
    write_live(feed, tmp)
    (tmp / "api" / "v1" / "summary.json").write_text(json.dumps(
        {"method": feed["method"], "generated_at": feed["generated_at"],
         "tokens": [{"token": t["token"], "symbol": t["symbol"], "name": t["name"], "orbio_agent": t.get("orbio_agent"),
                     "verdict": t["verdict"]["verdict"], "status": t["status"]} for t in feed["tokens"]]},
        ensure_ascii=False), "utf-8")
    old = out.with_name(out.name + ".old")
    if old.exists():
        shutil.rmtree(old)
    if out.exists():
        out.rename(old)
    tmp.rename(out)
    shutil.rmtree(old, ignore_errors=True)
    return len(feed["tokens"])


ROUTER = """
(function(){
  var views=[].slice.call(document.querySelectorAll('.view'));
  function show(){
    var el=document.getElementById(location.hash.slice(1)||'home');
    if(!el||!el.closest('.view'))el=document.getElementById('home');
    var view=el.closest('.view');
    views.forEach(function(v){v.hidden=v!==view});
    document.title=view.dataset.title;
    var r=document.getElementById('results');if(r)r.hidden=true;
    if(el===view)window.scrollTo(0,0);else el.scrollIntoView();
  }
  window.addEventListener('hashchange',show);show();
})();
"""


def to_hash(s: str) -> str:
    """Links between pages -> links between views of the one-page build."""
    for pat, rep in ((r'href="(?:\.\./)?t/(0x[0-9a-f]{40})\.html"', r'href="#t-\1"'),
                     (r'href="(?:\.\./)?index\.html#([\w-]+)"', r'href="#\1"'),
                     (r'href="(?:\.\./)?index\.html"', 'href="#home"'),
                     (r'href="(?:\.\./)?agents\.html"', 'href="#agents"'),
                     (r'href="(?:\.\./)?scams\.html"', 'href="#all-scams"'),
                     (r'href="(?:\.\./)?token\.html"', 'href="#token"'),
                     (r'href="(?:\.\./)?method\.html#([\w-]+)"', r'href="#\1"'),
                     (r'href="(?:\.\./)?method\.html"', 'href="#method"'),
                     (r'href="(?:\.\./)?api\.html"', 'href="#api"'),
                     (r'href="(?:\.\./)?api/v1/tokens/0x[0-9a-f]{40}\.json"', 'href="api/v1/feed.json"'),
                     (r'href="(?:\.\./)?api/v1/', 'href="api/v1/')):
        s = re.sub(pat, rep, s)
    return s


def build_single(feed: dict, name: str = NAME) -> str:
    """The whole site as one page (for a host that serves a single file, like a claude.ai Artifact): every
    page becomes a view, and the hash picks which one shows. No <html>/<head>/<body>: the host adds them."""
    global NAME
    saved, NAME = NAME, name
    try:
        now = feed["generated_at"]
        ON_FILE.clear()
        ON_FILE.update(t["token"] for t in feed["tokens"])
        TERMS.clear()
        TERMS.update((feed.get("network") or {}).get("fee_terms") or {})
        views = [("home", f"{name}: which Orbio launch is the real one?", home(feed)),
                 ("agents", f"Every Orbio agent · {name}", agents_page(feed)),
                 ("all-scams", f"Impersonators caught · {name}", scams_page(feed)),
                 ("token", f"$DOSSIER · {name}", strategy_page(feed)),
                 ("method", f"Method · {name}", method_page()),
                 ("api", f"API · {name}", api_page(feed))]
        views += [(f't-{t["token"]}', f'${t["symbol"]}: {STATE[state_of(t, now)]} · {name}', token_page(t, feed))
                  for t in feed["tokens"]]
        body = "\n".join(f'<div class="view" id="{vid}" data-title="{e(title)}"{"" if vid == "home" else " hidden"}>{html_}</div>'
                         for vid, title, html_ in views)
        body = to_hash(body)
        chrome_top, chrome_bottom = to_hash(header()), to_hash(footer(feed))
        cfg = inert(json.dumps({"href": "#t-{t}", "live": "api/v1/live.json"}))
        live = inert(json.dumps(live_data(feed), separators=(",", ":"), ensure_ascii=False))
        return f"""<title>{e(name)} Orbio Files</title>{THEME_BOOT}
<link rel="stylesheet" href="{FONTS}">
<style>{CSS.strip()}</style>
{chrome_top}
<main class="wrap">{body}</main>
{chrome_bottom}
<div class="toast" role="status" aria-live="polite" hidden></div>
<script>window.DOSSIER={cfg};window.DOSSIER_LIVE={live};</script>
<script>{JS.strip()}</script>
<script>{ROUTER.strip()}</script>
"""
    finally:
        NAME = saved


def lan_ip() -> str | None:
    """This machine's address on the local network (the interface that would reach the internet; nothing is sent)."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def serve(port: int, folder: Path = OUT, host: str = "127.0.0.1") -> None:
    """Preview the built site the way the host serves it: an address with no file gets 404.html, with a 404. With
    host 0.0.0.0, other devices on the same network (a phone) can open it too."""
    import http.server

    class Handler(http.server.SimpleHTTPRequestHandler):
        def end_headers(self):  # a dev server: every reload checks for a new build, never a cached old page
            self.send_header("Cache-Control", "no-cache")
            super().end_headers()

        def send_error(self, code, message=None, explain=None):
            page = folder / "404.html"
            if code != 404 or not page.exists():
                return super().send_error(code, message, explain)
            body = page.read_bytes()
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

    print(f"serving {folder} on http://127.0.0.1:{port}", flush=True)
    if host in ("0.0.0.0", "") and (ip := lan_ip()):
        print(f"on this network: http://{ip}:{port}", flush=True)
    http.server.ThreadingHTTPServer((host, port), functools.partial(Handler, directory=str(folder))).serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description="Build Dossier, the public site, from the index")
    ap.add_argument("--out", type=Path, default=OUT, help=f"output folder (default {OUT})")
    ap.add_argument("--single", type=Path, help="also write the whole site as one page to this file (a preview)")
    ap.add_argument("--name", default=NAME, help="the site's name in the one-page build")
    ap.add_argument("--serve", type=int, metavar="PORT", help="don't build: preview the built site on localhost:PORT")
    ap.add_argument("--host", default="127.0.0.1", help="the address --serve listens on (0.0.0.0: the whole local network)")
    args = ap.parse_args()
    if args.serve:
        return serve(args.serve, args.out, args.host)
    import proof_index as p  # here, not at the top: the indexer imports this module to rebuild the site
    p.CURVES_PER_PASS = 1000  # one build, no next pass to fill in the rest: read every curve now
    w.load_env()
    w.load_xlinks()
    t0 = time.time()
    db = p.open_db()
    feed = p.export_data(db, *p.site_inputs(db))
    n = build(feed, args.out)
    print(f"built {n} token pages into {args.out} in {time.time() - t0:.1f}s")
    if args.single:
        args.single.parent.mkdir(parents=True, exist_ok=True)
        args.single.write_text(build_single(feed, args.name), "utf-8")
        print(f"wrote the one-page build to {args.single} ({args.single.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()

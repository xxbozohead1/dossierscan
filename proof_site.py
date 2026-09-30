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
STATE = {"verified": "Verified", "scam": "Impersonator", "checking": "Checking", "unverified": "Unverified"}
STATE_TEXT = {
    "verified": "The project's own X account or website lists this exact contract.",
    "scam": "An official channel lists a different contract, or says this token isn't theirs.",
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
    if v == "unverified" and t.get("orbio_agent") and now - (t.get("launched_at") or 0) < CHECKING_FOR:
        return "checking"
    return v


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
    inner = f'<b>${e(t["symbol"] or short(t["token"]))}</b>{agent} <span class="nm">{e(t.get("name") or "")}</span>'
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


def header(up: str = "") -> str:
    nav = "".join(f'<a href="{up}{href}">{label}</a>' for href, label in NAV)
    return (f'<header class="top"><div class="wrap"><a class="brand" href="{up}index.html">{MARK}{NAME}</a>'
            f'<nav aria-label="Sections">{nav}</nav></div></header>')


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
<link rel="stylesheet" href="{FONTS}"><link rel="stylesheet" href="{up}assets/style.css?v={v}"></head>
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
               "te": ((tr.get("credit_owed") or 0) + (tr.get("credit_claimed") or 0)) if tr else None,
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
                       "assets": [asset_version(), file_version(OG_IMAGE), file_version(LOGO)], "tokens": static}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def ph(key: str) -> str:
    """A number the page script fills in from the live data."""
    return f'<span data-l="{key}">—</span>'


def tools(token: str) -> str:
    return (f'<span class="tools"><button type="button" class="mini copy" data-copy="{token}">Copy CA</button>'
            f'<button type="button" class="mini star" data-star="{token}" aria-pressed="false" aria-label="Add to watchlist">☆</button></span>')


# ----------------------------------------------------------------- home and lists

def home(feed: dict) -> str:
    toks, now = feed["tokens"], feed["generated_at"]
    orbio = [t for t in toks if t.get("orbio_agent")]
    verified = sum(1 for t in orbio if t["verdict"]["verdict"] == "verified")
    scams = sorted((t for t in toks if t["verdict"]["verdict"] == "scam"), key=lambda t: -(t["launched_at"] or 0))
    board = sorted((t for t in toks if t["status"] in ("PROVEN", "LIVE", "BUILDING")), key=rank)
    recent = sorted((t for t in orbio if (t["launched_at"] or 0) >= now - 2 * DAY), key=lambda t: -(t["launched_at"] or 0))
    index = inert(json.dumps([{"t": t["token"], "s": t["symbol"], "n": t["name"], "a": t.get("orbio_agent"),
                               "k": state_of(t, now)} for t in toks], separators=(",", ":")))
    counts = {s: sum(1 for t in recent if state_of(t, now) == s) for s in STATE}
    filters = "".join(f'<button type="button" data-filter="{k}" aria-pressed="{"true" if k == "all" else "false"}">{label}'
                      f'<span>{n}</span></button>' for k, label, n in (
                          ("all", "All", len(recent)), ("verified", "Verified", counts["verified"]),
                          ("checking", "Checking", counts["checking"] + counts["unverified"]), ("scam", "Impersonators", counts["scam"])))
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
{feed_cards(recent, now)}
<p class="more"><a href="agents.html">Every Orbio agent on file →</a></p></section>

<section id="board"><div class="sec-head"><div><h2>The board</h2>
<p class="sub">Tokens their project claims, ranked by evidence that something real exists and works.
<a href="method.html">How scores work</a>.</p></div></div>
{board_table(board, now)}</section>

<section id="scams"><div class="sec-head"><div><h2>Impersonators caught</h2>
<p class="sub">Tokens that copy a real project's name and socials. Each one links the post or page that exposes it, and
the real token when the project has launched one.</p></div></div>
{scam_list(scams[:10], now)}
<p class="more"><a href="scams.html">All {len(scams)} impersonators →</a> <span class="muted">{scam_split(scams)}</span></p></section>

<section id="how"><h2>How {NAME} decides</h2><div class="how">
<div>{chip("verified")}<p>{STATE_TEXT["verified"]} Social links in the token's metadata count for nothing: anyone can copy them.</p></div>
<div>{chip("scam")}<p>{STATE_TEXT["scam"]} The page quotes the post or names the contract the project claims instead.</p></div>
<div>{chip("checking")}<p>No official channel lists this contract yet. For the first six hours after launch, {NAME} looks for the
project's post every minute.</p></div>
</div><p class="more"><a href="method.html">The full method →</a></p></section>
</div>"""


def card(t: dict, now: int, up: str = "") -> str:
    s = state_of(t, now)
    rc = t["verdict"]["receipts"][:1]
    real = [x for x in ((t.get("related") or {}).get("claimed") or []) if x["token"] != t["token"]]
    extra = f'<p class="real">Real token: {tref(real[0], up, "tok inline")}</p>' if s == "scam" and real else ""
    tk = (t.get("trader") or {}).get("ticker")
    if tk and tk["rank"] > 1 and s != "verified":  # not the first token with this ticker: say so where hunters scan
        extra += (f'<p class="copynote">{ordinal(tk["rank"])} ${e(t["symbol"] or "?")}: the first launched '
                  f'{e(fmt_span((t["launched_at"] or 0) - (tk["first"]["ts"] or 0)))} earlier</p>')
    return f"""<li class="card" data-t="{t["token"]}" data-lt="{t["launched_at"] or 0}" data-state="{"checking" if s == "unverified" else s}">
<div class="card-top"><span class="fileno">{fileno(t)}</span><span class="age">{when(t["launched_at"])}</span>{tools(t["token"])}</div>
<div class="card-title">{tref(t, up, "tok stretch")}<span data-l="chip">{chip(s)}</span></div>
<div class="trend" data-l="trend"></div>
<dl class="card-nums"><div><dt>Market cap</dt><dd>{ph("mcap")}</dd></div><div><dt>Curve</dt><dd>{ph("curve")}</dd></div>
<div><dt>Holders</dt><dd>{ph("holders")}</dd></div></dl>
<p class="why">{e(sentence(t["verdict"]["why"]))}{"".join(f" {link(u, 'Receipt', 'rcpt')}" for u in rc)}</p>{extra}</li>"""


def feed_cards(ts: list[dict], now: int, up: str = "") -> str:
    return f'<ol class="feed" id="feed">{"".join(card(t, now, up) for t in ts)}</ol>' + \
        ("" if ts else '<p class="muted empty">No launches in the last 48 hours.</p>')


def board_table(board: list[dict], now: int, up: str = "") -> str:
    rows = "".join(f"""<tr data-t="{t["token"]}"><td class="num">{i}</td><td>{tref(t, up)}</td><td>{status_chip(t["status"])}</td>
<td class="score">{(t["scores"] or {}).get("composite") or 0:.0f}</td><td class="hide-sm">{bars(t["scores"])}</td>
<td class="numcol">{ph("mcap")}</td><td class="numcol hide-sm">{ph("vol")}</td><td class="hide-sm">{ph("curve")}</td>
<td><span data-l="chip">{chip(state_of(t, now))}</span></td></tr>""" for i, t in enumerate(board, 1))
    return f"""<div class="scroll"><table class="list"><thead><tr><th class="num">#</th><th>Token</th><th>Status</th>
<th class="score">Score</th><th class="hide-sm">Product · Build · Team · Work · Integrity</th><th class="numcol">Mkt cap</th>
<th class="numcol hide-sm">24h vol</th><th class="hide-sm">Curve</th><th>Official</th></tr></thead><tbody>{rows}</tbody></table></div>"""


def scam_list(ts: list[dict], now: int, up: str = "", searchable: bool = False) -> str:
    if not ts:
        return '<p class="muted">None caught yet.</p>'
    out = []
    for t in ts:
        real = [x for x in ((t.get("related") or {}).get("claimed") or []) if x["token"] != t["token"]]
        target = next((i["url"] for i in t.get("identities") or [] if i["binding"] in ("contradicted", "disavowed")), "")
        rc = t["verdict"]["receipts"][:1]
        attr = f' data-search="{e(search_key(t))}"' if searchable else ""
        out.append(f"""<li{attr}><div class="row">{tref(t, up)}{chip("scam")}<span class="age">{when(t["launched_at"])}</span></div>
<p>{e(sentence(t["verdict"]["why"]))}{"".join(f" {link(u, 'Receipt', 'rcpt')}" for u in rc)}</p>
<p class="real">{f"Real token: {tref(real[0], up, 'tok inline')}" if real else f"Copies {link(target) if target else 'a real project'}: no real token launched yet."}</p></li>""")
    return f'<ul class="scams">{"".join(out)}</ul>'


def sort_th(key: str, label: str, cls: str = "") -> str:
    c = f' class="{cls}"' if cls else ""
    return f'<th{c} data-sort="{key}"><button type="button">{label}</button></th>'


def agents_page(feed: dict) -> str:
    now = feed["generated_at"]
    orbio = sorted((t for t in feed["tokens"] if t.get("orbio_agent")), key=lambda t: -(t["orbio_agent"] or 0))
    rows = "".join(f"""<tr data-t="{t["token"]}" data-lt="{t["launched_at"] or 0}" data-sym="{e((t["symbol"] or "").lower())}"
data-state="{state_of(t, now)}" data-search="{e(search_key(t))}"><td>{tref(t)}</td><td><span data-l="chip">{chip(state_of(t, now))}</span></td>
<td>{status_chip(t["status"])}</td><td class="numcol">{ph("mcap")}</td><td class="numcol hide-sm">{ph("vol")}</td>
<td class="hide-sm">{ph("curve")}</td><td class="hide-sm muted">{when(t["launched_at"])}</td>
<td class="star-cell"><button type="button" class="mini star" data-star="{t["token"]}" aria-pressed="false" aria-label="Add to watchlist">☆</button></td></tr>"""
                   for t in orbio)
    head = (sort_th("name", "Token") + sort_th("verdict", "Official") + "<th>Status</th>" + sort_th("mcap", "Mkt cap", "numcol")
            + sort_th("vol", "24h vol", "numcol hide-sm") + sort_th("curve", "Curve", "hide-sm")
            + sort_th("age", "Launched", "hide-sm") + '<th><span class="sr">Watch</span></th>')
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
project's identity: {scam_split(scams)}. Orbio agents show their number (#). A token is marked an impersonator only when an
official channel lists a different contract, or says the token isn't theirs.</p></section>
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
    if agent and s != "scam":
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
            "checking": f"Checked {plural(tl.get('checks') or 0, 'time')} so far",
            "unverified": "No claim after 72 hours"}[s]
    keynums = [("Market cap", "mcap"), ("Change", "change"), ("24h volume", "vol"), ("24h buys / sells", "trades"),
               ("Price", "price"), ("Curve", "curve"), ("Real holders", "holders"), ("Agent balance", "tr_bal")]
    badges = "".join(f'<span class="badge">{e(b.title())}</span>' for b in t.get("badges") or [] if b != "ORBIO AGENT")
    return f"""<div class="filepage" data-t="{addr}"><p class="crumb"><a href="../index.html#live">← New launches</a></p>
<article class="file v-{s}">
<div class="file-tab">{"File No. " + str(agent) + " · Orbio agent" if agent else "Pons launch"}</div>
<header class="file-head"><div class="who"><h1><span class="tkr">${e(t["symbol"] or "?")}</span> <span class="nm">{e(t["name"] or "")}</span></h1>
<p class="meta">Launched {when(t["launched_at"])}{f' by {link(EXPLORER + "/address/" + t["creator"], short(t["creator"]))}' if t.get("creator") else ""} {badges}</p>
<p class="ca"><code>{addr}</code><button type="button" class="copy" data-copy="{addr}">Copy</button>
<button type="button" class="copy star big" data-star="{addr}" aria-pressed="false">☆ Watch</button></p></div>
<div class="stampbox"><span class="stamp big v-{s}">{STATE[s]}</span><span class="stampnote">{e(note)}</span></div></header>
<div class="actions">{"".join(actions)}</div>
<dl class="keynums">{"".join(f"<div><dt>{e(k)}</dt><dd>{ph(key)}</dd></div>" for k, key in keynums)}</dl>
<div data-l="chart"></div>{"" if agent else '<p class="muted small">Market numbers come from Orbio and cover Orbio agents only.</p>'}
</article>
{verdict_block(t, s)}
<div class="cols">{project_block(t)}{score_block(t)}</div>
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
    receipts = "".join(f"<li>{link(u)}</li>" for u in v["receipts"])
    extra = ""
    real = [r for r in rel.get("claimed") or [] if r["token"] != t["token"]]
    if s == "scam" and real:
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


def project_block(t: dict) -> str:
    xp = t.get("x_profile") or {}
    parts = []
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
                     f"{ordinal(tk['rank'])} token named ${sym}. The first, {tref(f, '../', 'tok inline')}, launched {e(gap)} earlier. "
                     + ("The project's own channels confirm this one." if s == "verified" else "Check which one the project claims.")))
    dev = tc.get("dev") or {}
    if dev:
        n = dev["launches_week"]
        rows.append(("Creator", "ok" if n == 0 else "warn" if n < 3 else "bad",
                     "This wallet launched nothing else within a week of this one" if n == 0
                     else f"This wallet launched {plural(n, 'other token')} within a week of this one"))
        if dev["other_launches"]:
            walked = dev["walked_away"]
            rows.append(("Creator", "bad" if walked else "ok",
                         f"Of its {plural(dev['other_launches'], 'other launch')}, it sold out of {walked}"
                         + (f", {dev['graduated']} graduated" if dev["graduated"] else "")
                         + (f", {dev['impersonators']} were impersonators" if dev["impersonators"] else "")))
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
        rows.append(("Holders", "ok" if sh < .3 else "warn" if sh < .5 else "bad", f"Top 10 holders own {sh:.0%}"))
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
                   for g, items in groups.items())
    return (f'<section class="safety trader"><h2>Trader’s card</h2>{body}<p class="muted small">From the chain alone: '
            "holder counts are real end buyers, not routers or bots, and bots are wallets that buy 10 or more launches a day."
            "</p></section>")


def treasury_block(t: dict) -> str:
    cells = [("Spendable balance", "tr_bal", "USDG for inference and tools"), ("Staked", "tr_staked", ph("tr_unlock")),
             ("CREDIT earned", "tr_earned", "owed plus claimed"), ("CREDIT activated", "tr_act", "spent into the gateway balance"),
             ("Principal withdrawn", "tr_withdrawn", "taken out of the stake")]
    return f"""<section class="treasury"><h2>Agent treasury</h2><p class="sub">Trading fees fund this agent: half is staked as
ORBIO and earns CREDIT, and 45% becomes a balance it can spend on models and tools.</p>
<dl class="keynums small">{"".join(f'<div><dt>{e(a)}</dt><dd>{ph(k)}</dd><span>{c}</span></div>' for a, k, c in cells)}</dl></section>"""


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
<li><code>market</code> and <code>treasury</code> come from Orbio's public API (Orbio agents only): price, market cap, curve progress,
stake, CREDIT and the agent's spendable balance.</li>
<li><code>timeline.verified_at</code> is when {NAME} first saw an official channel claim the token.</li>
<li><code>status</code> is <code>PROVEN</code>, <code>LIVE</code>, <code>BUILDING</code>, <code>UNPROVEN</code>, <code>ABANDONED</code> or
<code>SCAM</code> (<a href="method.html#5-status">rules</a>). <code>facts[]</code> are the score lines.</li></ul>
</article>"""


# ----------------------------------------------------------------- $DOSSIER: the token page

TOKEN_DOC = ROOT / "docs" / "token.md"
FEE_SPLIT = ((45, "Dossier's compute", "AI balance: pays for every check", "c-compute"),
             (50, "Staked as ORBIO", "earns CREDIT: compute first, then buybacks", "c-stake"),
             (5, "Orbio's treasury", "the launchpad's share", "c-orbio"))
PASS_SPLIT = ((70, "Burned", "sent to the dead address in batches", "c-burn"),
              (30, "Treasury", "runs and grows Dossier", "c-treasury"))
FLYWHEEL = (("Right, fast verdicts", "Each verdict links its receipt, often within minutes of launch."),
            ("Hunters rely on it", "They check a launch before they buy. The keenest buy a pass."),
            ("Fees and passes", "Trading pays creator fees. Passes are paid in $DOSSIER."),
            ("More checks, less supply", "Fees buy faster, wider checks. Passes and stake rewards buy back and burn $DOSSIER."))
BURN_SHARE = 0.7


def split_bar(parts, label: str) -> str:
    bar = "".join(f'<span class="{c}" style="width:{p}%" title="{p}%: {e(name)}"></span>' for p, name, _, c in parts)
    legend = "".join(f'<li><i class="{c}"></i><b>{p}%</b> {e(name)}<span>{e(note)}</span></li>' for p, name, note, c in parts)
    return f'<div class="split" role="img" aria-label="{e(label)}">{bar}</div><ul class="split-legend">{legend}</ul>'


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
        rows += [("AI balance credited by fees", usd(tr.get("balance_usdg"))), ("AI balance left for checks", usd(left)),
                 ("Compute runway, estimated", runway), ("ORBIO staked", num(tr.get("staked_orbio"), "ORBIO")),
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
    blocks = {"contract": official_contract(d), "fees": split_bar(FEE_SPLIT, "Creator fees: 45% compute, 50% stake, 5% Orbio"),
              "passes": split_bar(PASS_SPLIT, "Pass revenue: 70% burned, 30% treasury"), "flywheel": flywheel(),
              "ledger": ledger(d, feed)}
    body = re.sub(r"<p>@@(\w+)@@</p>", lambda m: blocks.get(m.group(1), ""), md(text))
    body = re.sub(r"^(<h1 [^>]*>.*?</h1>)", rf'<div class="token-hero"><img src="assets/logo.png?v={file_version(LOGO)}" alt="The $DOSSIER seal" '
                  r'width="104" height="104">\1</div>', body, count=1)
    return f'<article class="prose">{body}</article>'


# ----------------------------------------------------------------- assets

# light palette on :root; the dark one below is applied by the OS preference (unless the viewer chose light) and by
# an explicit data-theme="dark"
CSS = """
:root{--bg:#e8ebf0;--sheet:#fff;--sheet-2:#f4f6f9;--ink:#0d1522;--ink-2:#39455a;--muted:#657084;--rule:#d5dae2;--rule-2:#b8c0cc;
--accent:#2d49d8;--accent-bg:#e5e9fc;--ok:#0b7a4b;--ok-bg:#e0f2e8;--bad:#cc2539;--bad-bg:#fbe6e9;--warn:#9a6300;--warn-bg:#faefd4;
--display:"Bricolage Grotesque","Instrument Sans",system-ui,sans-serif;--sans:"Instrument Sans",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
--mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){@@DARK@@}}
:root[data-theme="dark"]{@@DARK@@}
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
.card{position:relative;background:var(--sheet);border:1px solid var(--rule);border-radius:12px;padding:14px 16px 13px;display:flex;flex-direction:column;gap:9px}
.card:hover{border-color:var(--rule-2)}
.card-top{display:flex;justify-content:space-between;gap:10px;font:500 11.5px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.card-top .age{text-transform:none;letter-spacing:0}
.card-title{display:flex;align-items:center;gap:10px;justify-content:space-between}
.stretch::after{content:"";position:absolute;inset:0;border-radius:12px}.card .rcpt,.card .real a{position:relative;z-index:1}
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
.v-verified{--st:var(--ok);--st-bg:var(--ok-bg)}.v-scam{--st:var(--bad);--st-bg:var(--bad-bg)}
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
.dims{display:flex;gap:4px;min-width:150px}.dims .dim{flex:1}
.bar{display:block;height:6px;border-radius:3px;background:var(--rule);overflow:hidden}.bar>span{display:block;height:100%;background:var(--ink)}
.dims.full{flex-direction:column;gap:9px;margin-top:14px}.dims.full .dim{display:grid;grid-template-columns:78px 1fr 58px;align-items:center;gap:10px}
.dims.full .bar{height:8px}.dl{font-size:13.5px;color:var(--ink-2)}.dv{text-align:right;font:600 14px var(--mono);font-variant-numeric:tabular-nums}.dv small{color:var(--muted);font-weight:400}
.scams{list-style:none;margin:0;padding:0;background:var(--sheet);border:1px solid var(--rule);border-radius:12px}
.scams li{padding:13px 16px;border-top:1px solid var(--rule)}.scams li:first-child{border-top:0}
.scams .row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.scams .age{margin-left:auto;color:var(--muted);font:12px var(--mono)}
.scams p{margin:5px 0 0;font-size:14px;color:var(--ink-2);overflow-wrap:anywhere}.scams .real{color:var(--muted)}
.more{margin:14px 0 0;font-weight:600}
.how{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:14px}
.how>div{background:var(--sheet);border:1px solid var(--rule);border-radius:12px;padding:16px}.how p{margin:10px 0 0;font-size:14px;color:var(--ink-2)}
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
.card.is-new{border-color:var(--accent)}.trend:empty{display:none}.star-cell{width:1%}
.toast{position:fixed;left:50%;bottom:calc(20px + env(safe-area-inset-bottom,0px));transform:translateX(-50%);z-index:30;background:var(--ink);color:var(--sheet);font:600 13.5px var(--sans);padding:10px 16px;border-radius:10px;box-shadow:0 8px 24px rgba(8,14,26,.25)}
.report{margin:16px 0 0;padding-top:12px;border-top:1px dashed var(--rule);font-size:14px}
th[data-sort] button{all:unset;cursor:pointer;font:inherit;letter-spacing:inherit;text-transform:inherit;color:inherit}
th[data-sort] button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
th[aria-sort=descending] button::after{content:" ↓"}th[aria-sort=ascending] button::after{content:" ↑"}
.keynums dd small,.card-nums dd small{font-size:11px;color:var(--muted);font-weight:500}
.official-ca{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px}.official-ca code{word-break:break-all}
.trader h3{margin:16px 0 6px;font:600 11.5px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}.trader h3:first-of-type{margin-top:8px}
.copynote{margin:0;font-size:13px;color:var(--warn);font-weight:600}
.token-hero{display:flex;align-items:center;gap:18px;margin-bottom:6px}.token-hero h1{margin:0}
.token-hero img{width:104px;height:104px;border-radius:50%;flex:none;box-shadow:0 6px 24px rgba(8,14,26,.25)}
.official{background:var(--sheet);border:1px solid var(--rule);border-left:4px solid var(--warn);border-radius:12px;padding:16px 18px;margin:20px 0}
.official.live{border-left-color:var(--ok)}.official .eyebrow{margin-bottom:6px}.official p{margin:6px 0 0}
.official .big{font:700 20px var(--display);font-stretch:92%;margin:0}
.split{display:flex;height:14px;border-radius:7px;overflow:hidden;margin:16px 0 12px;background:var(--rule)}
.split span{display:block;height:100%}.split span+span{border-left:2px solid var(--bg)}
.c-compute{background:var(--ok)}.c-stake{background:var(--accent)}.c-orbio{background:var(--rule-2)}.c-burn{background:var(--bad)}
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
@media (max-width:560px){.keynums{grid-template-columns:repeat(2,minmax(0,1fr))}.hide-sm{display:none}.stampbox{flex-direction:row;padding:0}
.stamp.big{font-size:20px;padding:11px 16px 9px}.verdict .why{font-size:17px}.file{padding:18px 16px}.verdict,.project,.score,.safety,.treasury,.evidence{padding:16px}
.tally{gap:10px 24px}.tally dd{font-size:22px}}
"""
DARK = ("--bg:#090d12;--sheet:#10161f;--sheet-2:#151d28;--ink:#e7ecf3;--ink-2:#b4bfcd;--muted:#8792a4;--rule:#212a36;--rule-2:#324050;"
        "--accent:#8fa3ff;--accent-bg:#18214a;--ok:#39d08c;--ok-bg:#0e2a1d;--bad:#ff6072;--bad-bg:#301318;--warn:#f3b64b;"
        "--warn-bg:#2c2210;color-scheme:dark")
CSS = CSS.replace("@@DARK@@", DARK)

JS = r"""
(function(){
  var D=window.DOSSIER||{href:'t/{t}.html',live:'api/v1/live.json'},LIVE=window.DOSSIER_LIVE||null;
  var LABEL={verified:'Verified',scam:'Impersonator',checking:'Checking',unverified:'Unverified'},SUB='₀₁₂₃₄₅₆₇₈₉';
  var ORDER={verified:3,checking:2,unverified:1,scam:0},filterKey='all';
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
    holders:function(r){return r.h==null?'—':r.h+' <small>('+r.hk+')</small>'},
    trades:function(r){return r.b==null?'—':'<span class="up">'+r.b+'</span> / <span class="down">'+r.x+'</span>'},
    trend:function(r){var s=spark(r.sp,120,32);if(!s&&r.v==null)return '';
      return (s||'<span class="none">The chart fills in as snapshots arrive</span>')+'<span class="tv"><b>'+change(r)+'</b>'+usd(r.v)+' 24h vol</span>'},
    chart:function(r){if(!r.sp||r.sp.length<2)return '';var ys=r.sp.map(function(q){return q[1]}),hrs=(r.sp[r.sp.length-1][0]-r.sp[0][0])/3600;
      return '<figure class="chart">'+spark(r.sp,600,96)+'<figcaption><span>Market cap, last '+(hrs<48?Math.max(1,Math.round(hrs))+' h':Math.round(hrs/24)+' days')+
        '</span><span>low '+usd(Math.min.apply(null,ys))+' · high '+usd(Math.max.apply(null,ys))+' · now '+usd(ys[ys.length-1])+'</span></figcaption></figure>'},
    tr_bal:function(r){return usd(r.tb)},tr_staked:function(r){return num(r.tk||0)+' ORBIO'},tr_earned:function(r){return num(r.te||0)},
    tr_act:function(r){return num(r.ta||0)},tr_withdrawn:function(r){return num(r.tw||0)+' ORBIO'},
    tr_unlock:function(r){return r.tu?(r.tl?'unlocks '+rel(now()-r.tu):'unlocked'):'—'}
  };
  function fill(root,r){root.querySelectorAll('[data-l]').forEach(function(el){var f=F[el.dataset.l];if(f)el.innerHTML=f(r)})}
  function fileHref(k){return LIVE&&LIVE.t[k]?href(k):'https://robin.etherscan.io/token/'+k}
  function cardHtml(k,r){var s=r.s,rc=/^https?:\/\//.test(r.rc||'')?r.rc:'';
    return '<li class="card" data-t="'+k+'" data-lt="'+(r.lt||0)+'" data-state="'+(s==='unverified'?'checking':s)+'">'+
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
    b.setAttribute('aria-pressed',on?'true':'false');b.textContent=b.classList.contains('big')?(on?'★ Watching':'☆ Watch'):(on?'★':'☆')})}
  function applyFilter(){var list=document.getElementById('feed');if(!list)return;
    list.querySelectorAll('.card').forEach(function(c){c.hidden=filterKey!=='all'&&c.dataset.state!==filterKey})}
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
      if(root.classList.contains('card'))root.dataset.state=r.s==='unverified'?'checking':r.s});
    var n=LIVE.net||{};document.querySelectorAll('[data-l^="net_"]').forEach(function(el){var k=el.dataset.l;
      el.innerHTML=k==='net_orbio'?price(n.orbio_usd):k==='net_mcap'?usd(n.mcap_usd):k==='net_at'?rel(now()-LIVE.at):'—'});
    markNew();syncStars();applyFilter()}
  function load(){if(!D.live||!window.fetch)return;fetch(D.live+(D.live.indexOf('?')<0?'?':'&')+'_='+Date.now(),{cache:'no-store'})
    .then(function(r){return r.ok?r.json():null}).then(function(j){if(j&&j.t){LIVE=j;apply()}}).catch(function(){})}
  var toastEl=document.querySelector('.toast'),toastT;
  function toast(m){if(!toastEl)return;toastEl.textContent=m;toastEl.hidden=false;clearTimeout(toastT);toastT=setTimeout(function(){toastEl.hidden=true},2200)}
  function copyText(b){var v=b.dataset.copy,label=b.textContent;
    function done(){b.textContent='Copied';setTimeout(function(){b.textContent=label},1500)}
    function fallback(){var t=document.createElement('textarea');t.value=v;t.setAttribute('readonly','');t.style.position='fixed';t.style.opacity='0';
      document.body.appendChild(t);t.select();try{document.execCommand('copy')}catch(e){}document.body.removeChild(t);done()}
    if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(v).then(done,fallback);else fallback()}
  function sortVal(row,key){var r=(LIVE&&LIVE.t[row.dataset.t])||{};switch(key){
    case 'mcap':return r.m||0;case 'vol':return r.v||0;case 'curve':return r.g?101:(r.c||0);case 'age':return +row.dataset.lt||0;
    case 'verdict':return ORDER[r.s||row.dataset.state]||0;default:return row.dataset.sym||''}}
  function sortBy(th){var tb=th.closest('table').tBodies[0],key=th.dataset.sort,cur=th.getAttribute('aria-sort');
    var desc=cur?cur==='ascending':key!=='name';
    th.closest('tr').querySelectorAll('th').forEach(function(x){x.removeAttribute('aria-sort')});
    th.setAttribute('aria-sort',desc?'descending':'ascending');
    var rows=[].slice.call(tb.rows);rows.sort(function(a,b){var x=sortVal(a,key),y=sortVal(b,key),c=typeof x==='string'?x.localeCompare(y):x-y;return desc?-c:c});
    rows.forEach(function(r){tb.appendChild(r)})}
  var q=document.getElementById('q'),res=document.getElementById('results'),idx=document.getElementById('idx');
  var all=idx?JSON.parse(idx.textContent):[];
  document.addEventListener('click',function(ev){
    var cp=ev.target.closest('[data-copy]');if(cp){ev.preventDefault();copyText(cp);return}
    var st=ev.target.closest('.star');if(st){ev.preventDefault();var wl=watchList(),k=st.dataset.star,i=wl.indexOf(k);
      if(i>=0)wl.splice(i,1);else wl.push(k);saveWatch(wl);syncStars();renderWatch();toast(i>=0?'Removed from your watchlist':'Added to your watchlist');return}
    var fb=ev.target.closest('.filters button');if(fb){filterKey=fb.dataset.filter;
      fb.parentNode.querySelectorAll('button').forEach(function(x){x.setAttribute('aria-pressed',x===fb?'true':'false')});applyFilter();return}
    var sb=ev.target.closest('th[data-sort] button');if(sb){sortBy(sb.parentNode);return}
    if(res&&!res.contains(ev.target)&&ev.target!==q)res.hidden=true});
  document.querySelectorAll('.filter').forEach(function(f){f.addEventListener('input',function(){
    var v=f.value.trim().toLowerCase(),scope=f.closest('.view')||document;
    scope.querySelectorAll('[data-search]').forEach(function(r){r.hidden=!!v&&r.dataset.search.indexOf(v)<0})})});
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
  apply();setInterval(load,60000);setInterval(times,60000);
  document.addEventListener('visibilitychange',function(){if(!document.hidden)load()});
  setTimeout(function(){try{localStorage.setItem('dossier.seen',String(Math.floor(now())))}catch(e){}},4000);
})();
"""


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
    put("index.html", f"{NAME}: which Orbio launch is the real one?", home(feed))
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
        pages = ["", "agents.html", "scams.html", "token.html", "method.html", "api.html"] + [f't/{t["token"]}.html' for t in feed["tokens"]]
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
        return f"""<title>{e(name)} Orbio Files</title>
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


def serve(port: int, folder: Path = OUT) -> None:
    """Preview the built site the way the host serves it: an address with no file gets 404.html, with a 404."""
    import http.server

    class Handler(http.server.SimpleHTTPRequestHandler):
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
    http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Handler, directory=str(folder))).serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description="Build Dossier, the public site, from the index")
    ap.add_argument("--out", type=Path, default=OUT, help=f"output folder (default {OUT})")
    ap.add_argument("--single", type=Path, help="also write the whole site as one page to this file (a preview)")
    ap.add_argument("--name", default=NAME, help="the site's name in the one-page build")
    ap.add_argument("--serve", type=int, metavar="PORT", help="don't build: preview the built site on localhost:PORT")
    args = ap.parse_args()
    if args.serve:
        return serve(args.serve, args.out)
    import proof_index as p  # here, not at the top: the indexer imports this module to rebuild the site
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

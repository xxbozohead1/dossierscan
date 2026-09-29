#!/usr/bin/env python3
"""
proof_site: the public Proof site, built as static files from the index (proof_index.export_data).

    python proof_site.py                # build into data/site
    python proof_site.py --out DIR

The indexer rebuilds it every few minutes while it runs (proof_index.maybe_build_site). Every link is
relative, so the folder can be served from any host or path. Stdlib only, like the rest of the project.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import shutil
import time
from pathlib import Path

import orbio_watch as w

ROOT = Path(__file__).resolve().parent
OUT = w.DATA / "site"
SPEC = ROOT / "docs" / "scoring-spec.md"
NAME = "Proof"  # working name until the project has one
DAY = 86400

DIMS = (("product", "Product", 30), ("build", "Build", 20), ("team", "Team", 20), ("work", "Work", 20),
        ("integrity", "Integrity", 10))
VERDICT = {"verified": "Verified", "scam": "Scam", "unverified": "Unverified"}
VERDICT_TEXT = {"verified": "The project's own X account or site lists this exact contract.",
                "scam": "An official channel lists a different contract, or says this token isn't theirs.",
                "unverified": "Nothing official lists this contract yet. That isn't proof of a scam."}
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
FLAGS = {"CREATOR_EXIT": "creator sold most of their launch buy", "FEE_REDIRECT": "creator fees sent to a fresh wallet",
         "LAUNCH_BUNDLE": "several buyers in the launch block", "SERIAL": "creator launched 3+ tokens in a day",
         "CONFLICT": "official channels disagree", "BORROWED": "points at another organisation's brand"}


# ----------------------------------------------------------------- small helpers

def e(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def short(a: str) -> str:
    return f"{a[:6]}…{a[-4:]}" if a and len(a) > 12 else (a or "")


def stamp(ts: int | None) -> str:
    """A <time> the page's script turns into "3 h ago"; the absolute UTC time stays in the tooltip."""
    if not ts:
        return "—"
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return f'<time datetime="{d:%Y-%m-%dT%H:%M:%SZ}" data-ts="{ts}" title="{d:%d %b %Y %H:%M UTC}">{d:%d %b %H:%M} UTC</time>'


def link(url: str, text: str | None = None) -> str:
    if not re.match(r"https?://", url or ""):
        return e(text or url)
    return f'<a href="{e(url)}" rel="nofollow noopener" target="_blank">{e(text or url.split("://", 1)[1].rstrip("/"))}</a>'


def verdict_chip(v: str) -> str:
    return f'<span class="chip v-{v}"><i></i>{VERDICT[v]}</span>'


def status_chip(s: str) -> str:
    return f'<span class="chip s-{e((s or "unproven").lower())}">{e((s or "UNPROVEN").title())}</span>'


def tref(t: dict, up: str = "") -> str:
    agent = f' <span class="agent">#{t["orbio_agent"]}</span>' if t.get("orbio_agent") else ""
    return (f'<a class="tok" href="{up}t/{t["token"]}.html"><b>${e(t["symbol"] or "?")}</b>{agent}'
            f'<span class="nm">{e(t["name"] or "")}</span></a>')


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


def sentence(s: str) -> str:
    """The verdict reasons are lowercase clauses (Telegram alerts embed them); a page shows them as sentences."""
    s = (s or "").strip()
    return s[:1].upper() + s[1:] + ("" if s.endswith((".", "…", "”")) else ".")


def rank(t: dict) -> tuple:
    return ({"PROVEN": 0, "LIVE": 1, "BUILDING": 2}.get(t["status"], 3), -((t.get("scores") or {}).get("composite") or 0))


def search_key(t: dict) -> str:
    return f'{t["symbol"] or ""} {t["name"] or ""} {t["token"]}'.lower()


# ----------------------------------------------------------------- page chrome

FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' "
           "height='32' rx='8' fill='%2316181d'/%3E%3Cpath d='M9 16.5l4.5 4.5L23 11.5' stroke='%23fff' stroke-width='3.2' "
           "fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E")


def page(title: str, body: str, feed: dict, depth: int = 0, desc: str = "") -> str:
    up = "../" * depth
    nav = "".join(f'<a href="{up}{href}">{label}</a>' for href, label in (
        ("index.html#board", "Board"), ("agents.html", "All agents"), ("scams.html", "Scams"),
        ("method.html", "Method"), ("api.html", "API")))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title><meta name="description" content="{e(desc or 'Which Orbio agents are real? Evidence, with receipts.')}">
<link rel="icon" href="{FAVICON}"><link rel="stylesheet" href="{up}assets/style.css"></head>
<body><header class="top"><div class="wrap"><a class="brand" href="{up}index.html"><img src="{FAVICON}" alt="">{NAME}</a>
<nav>{nav}</nav></div></header>
<main class="wrap">{body}</main>
<footer class="wrap foot"><p>Method <a href="{up}method.html">{e(feed["method"])}</a> · updated {stamp(feed["generated_at"])} ·
<a href="{up}api/v1/feed.json">feed.json</a></p>
<p class="muted">{NAME} checks who claims a token. It says nothing about price, and nothing here is financial advice.
Scores come only from public evidence, and every line links its source.</p></footer>
<script src="{up}assets/app.js"></script></body></html>
"""


# ----------------------------------------------------------------- home, lists

def home(feed: dict) -> str:
    toks, now = feed["tokens"], feed["generated_at"]
    orbio = [t for t in toks if t["orbio_agent"]]
    n = {v: sum(1 for t in orbio if t["verdict"]["verdict"] == v) for v in VERDICT}
    scams = sorted((t for t in toks if t["verdict"]["verdict"] == "scam"), key=lambda t: -(t["launched_at"] or 0))
    board = sorted((t for t in toks if t["status"] in ("PROVEN", "LIVE", "BUILDING")), key=rank)
    recent = sorted((t for t in orbio if (t["launched_at"] or 0) >= now - 2 * DAY), key=lambda t: -(t["launched_at"] or 0))
    index = json.dumps([{"t": t["token"], "s": t["symbol"], "n": t["name"], "a": t["orbio_agent"],
                         "v": t["verdict"]["verdict"], "st": t["status"]} for t in toks],
                       separators=(",", ":"))
    # launcher-written names inside <script>: encode the characters HTML could act on, so the block stays inert
    index = index.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    rows = "".join(f"""<tr><td class="num">{i}</td><td>{tref(t)}</td><td>{status_chip(t["status"])}</td>
<td class="score">{(t["scores"] or {}).get("composite") or 0:.0f}</td><td class="hide-sm">{bars(t["scores"])}</td>
<td>{verdict_chip(t["verdict"]["verdict"])}</td></tr>""" for i, t in enumerate(board, 1))
    return f"""
<section class="hero"><h1>Which Orbio agents are real?</h1>
<p class="lede">{NAME} checks every agent launched on the Orbio launchpad against its project's own X account, website
and code. A token counts as the project's only when the project itself lists that exact contract. Every verdict
links its receipt.</p>
<div class="search"><input id="q" type="search" placeholder="Paste a contract address or type a ticker" autocomplete="off"
aria-label="Search tokens"><div id="results" class="results" hidden></div></div>
<script id="idx" type="application/json">{index}</script>
<div class="stats"><div><b>{len(orbio)}</b><span>Orbio agents indexed</span></div>
<div><b class="ok">{n["verified"]}</b><span>verified by their project</span></div>
<div><b class="bad">{len(scams)}</b><span>impersonators caught</span></div>
<div><b>{n["unverified"]}</b><span>not verified yet</span></div></div></section>

<section id="board"><h2>The board</h2>
<p class="sub">Tokens their project claims, ranked by evidence that something real exists and is working.
<a href="method.html">How scores work</a>.</p>
<div class="scroll"><table class="list"><thead><tr><th class="num">#</th><th>Token</th><th>Status</th><th class="score">Score</th>
<th class="hide-sm">Product · Build · Team · Work · Integrity</th><th>Official</th></tr></thead><tbody>{rows}</tbody></table></div>
</section>

<section id="launches"><h2>Orbio launches, last 48 hours</h2>
<p class="sub">Checked minutes after launch, then again at 15 min, 1 h, 3 h, 6 h, 24 h and 72 h. The official launch post
often lands a few minutes after the token.</p>
{launch_list(recent)}
<p><a href="agents.html">All {len(orbio)} Orbio agents →</a></p></section>

<section id="scams"><h2>Impersonators caught</h2>
<p class="sub">Tokens that copy a real project's name and socials. Each one links the post or page that exposes it.</p>
{scam_list(scams[:12])}
<p><a href="scams.html">All {len(scams)} →</a></p></section>
"""


def launch_list(ts: list[dict], up: str = "") -> str:
    if not ts:
        return '<p class="muted">None.</p>'
    return '<ul class="feed">' + "".join(
        f"""<li><div class="row">{tref(t, up)}{verdict_chip(t["verdict"]["verdict"])}<span class="when">{stamp(t["launched_at"])}</span></div>
<p>{e(sentence(t["verdict"]["why"]))}{receipt_links(t["verdict"]["receipts"][:1])}</p></li>""" for t in ts) + "</ul>"


def scam_list(ts: list[dict], up: str = "") -> str:
    if not ts:
        return '<p class="muted">None caught yet.</p>'
    return '<ul class="feed">' + "".join(
        f"""<li><div class="row">{tref(t, up)}{verdict_chip("scam")}<span class="when">{stamp(t["launched_at"])}</span></div>
<p>{e(sentence(t["verdict"]["why"]))}{receipt_links(t["verdict"]["receipts"][:1])}</p></li>""" for t in ts) + "</ul>"


def receipt_links(urls: list[str]) -> str:
    return "".join(f' <span class="rcpt">{link(u, "receipt")}</span>' for u in urls)


def agents_page(feed: dict) -> str:
    orbio = sorted((t for t in feed["tokens"] if t["orbio_agent"]), key=lambda t: -(t["orbio_agent"] or 0))
    rows = "".join(f"""<tr data-search="{e(search_key(t))}"><td>{tref(t)}</td><td>{verdict_chip(t["verdict"]["verdict"])}</td>
<td>{status_chip(t["status"])}</td><td class="score">{(t["scores"] or {}).get("composite") or 0:.0f}</td>
<td class="hide-sm muted">{stamp(t["launched_at"])}</td></tr>""" for t in orbio)
    return f"""<h1>All Orbio agents</h1><p class="sub">Every agent launched through the Orbio AgentVault that the index has
read, newest first.</p><input id="filter" type="search" placeholder="Filter by ticker, name or address" aria-label="Filter">
<div class="scroll"><table class="list"><thead><tr><th>Token</th><th>Official</th><th>Status</th><th class="score">Score</th>
<th class="hide-sm">Launched</th></tr></thead><tbody>{rows}</tbody></table></div>"""


def scams_page(feed: dict) -> str:
    scams = sorted((t for t in feed["tokens"] if t["verdict"]["verdict"] == "scam"), key=lambda t: -(t["launched_at"] or 0))
    return f"""<h1>Impersonators caught</h1><p class="sub">{len(scams)} tokens that copy a real project's identity. A token is
marked a scam only when an official channel lists a different contract, or says the token isn't theirs.</p>
<input id="filter" type="search" placeholder="Filter by ticker, name or address" aria-label="Filter">
{scam_list(scams).replace("<li>", "<li data-search>")}"""


# ----------------------------------------------------------------- token page

def token_page(t: dict, feed: dict) -> str:
    v = t["verdict"]
    sc = t["scores"]
    addr = t["token"]
    head_links = " · ".join([link(f"https://www.orbio.so/launchpad/{addr}", "Launchpad"),
                             link(f"https://dexscreener.com/robinhood/{addr}", "Chart"),
                             link(f"https://robinhoodchain.blockscout.com/address/{addr}", "Explorer")])
    badges = "".join(f'<span class="badge">{e(b.title())}</span>' for b in t["badges"])
    receipts = "".join(f"<li>{link(u)}</li>" for u in v["receipts"])
    score = f"""<div class="panel"><div class="ph"><h2>Status</h2>{status_chip(t["status"])}</div>
<p class="big">{(sc or {}).get("composite") or 0:.0f}<small>/100</small></p><p>{e(sentence(t["explain"]))}</p>
{bars(sc, labels=True) if sc else ""}{f'<p class="muted">Capped by {e(t["cap"])}.</p>' if t.get("cap") else ""}</div>"""
    claim = f"""<section><h2>What it says it does</h2>
{f'<p class="claim">{e(t["claim"])}</p><p class="muted">Summarised from its bound site and posts; the quote behind it was checked against the page.</p>' if t.get("claim") else ""}
{f'<p class="desc">“{e(w.clip(t["description"], 600))}”</p><p class="muted">The token description: written by whoever launched it.</p>' if t.get("description") else ""}
</section>""" if t.get("claim") or t.get("description") else ""
    return f"""<p class="crumb"><a href="../index.html#board">← Board</a></p>
<div class="thead"><h1>${e(t["symbol"] or "?")} <span class="nm">{e(t["name"] or "")}</span></h1>
<p>{f'Orbio agent #{t["orbio_agent"]} · ' if t["orbio_agent"] else "Pons launch · "}launched {stamp(t["launched_at"])} {badges}</p>
<p class="addr"><code>{addr}</code><button class="copy" data-copy="{addr}">Copy</button></p><p>{head_links}</p></div>

<div class="grid2"><div class="panel verdict v-{v["verdict"]}"><div class="ph"><h2>Is this the project's token?</h2>
{verdict_chip(v["verdict"])}</div><p class="why">{e(sentence(v["why"]))}</p>
{f'<ul class="receipts">{receipts}</ul>' if receipts else ""}<p class="muted">{VERDICT_TEXT[v["verdict"]]}</p></div>
{score}</div>
{claim}
{identities(t)}
{evidence(t) if t["facts"] else ""}
{market(t)}
<p class="muted">Raw data: <a href="../api/v1/tokens/{addr}.json">{addr[:10]}….json</a></p>"""


def identities(t: dict) -> str:
    role = {"twitter": "X account", "website": "Website", "repo": "Code", "github": "Code"}
    rows = "".join(f"""<tr><td>{role.get(i["role"], e(i["role"]))}</td><td class="wrapcell">{link(i["url"])}</td>
<td><span class="lvl l-{e((i["binding"] or "unbound").replace("·", "-"))}">{e(i["binding"] or "not checked")}</span>
<span class="muted hide-sm"> {e(LEVEL_TEXT.get(i["binding"] or "", ""))}</span></td></tr>""" for i in t["identities"])
    if not rows:
        return ""
    return f"""<section><h2>Who it points at</h2><p class="sub">The accounts and sites in the token's own metadata, and
whether each one claims this token. Metadata is self-asserted, so only the claim counts.</p>
<div class="scroll"><table class="list"><thead><tr><th>Kind</th><th>Link</th><th>Claims it?</th></tr></thead>
<tbody>{rows}</tbody></table></div></section>"""


def line_label(f: dict) -> str:
    label = LINES.get(f["line"], f["line"])
    if f["line"] == "output":  # one line, read from the site and from the posts separately
        label += " in its posts" if (f.get("source") or "").startswith("x:") else " on its site"
    return label


def plural(n, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


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
<td class="wrapcell">{fact_detail(f)}</td><td class="hide-sm muted">{stamp(f.get("at"))}</td></tr>""" for f in fs)
        groups.append(f"""<h3>{label} <span class="muted">{(t["scores"] or {}).get(key) or 0:g} of {mx}</span></h3>
<div class="scroll"><table class="list ev"><tbody>{rows}</tbody></table></div>""")
    return f"""<section><h2>Evidence</h2><p class="sub">Each line is a check the index ran, with what it found and when.
A line only counts if its source claims the token (the weight), and it fades as it ages.</p>{"".join(groups)}</section>"""


def market(t: dict) -> str:
    h = t["holders"]
    cells = "".join(f'<div><b>{"—" if h.get(k) is None else h[k]}</b><span>holders at {k}</span></div>'
                    for k in ("30m", "6h", "24h", "7d"))
    flags = [FLAGS[f] for f in t["flags"] if f in FLAGS]
    return f"""<section><h2>Token</h2><div class="stats small">{cells}<div><b>{"yes" if t["graduated"] else "no"}</b>
<span>graduated</span></div></div>
<p class="muted">Holders are real end buyers, not routers or bots. Creator: <code>{short(t["creator"] or "")}</code>.</p>
{f'<p>Flags: {e("; ".join(flags))}.</p>' if flags else ""}</section>"""


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
    return f"""<article class="prose"><h1>API</h1><p>Everything on this site is also plain JSON, free and without a key.
It's rebuilt from the index every few minutes, and every score line carries its source and time.</p>
<h2>Endpoints</h2><div class="scroll"><table class="list md"><thead><tr><th>Path</th><th>What</th></tr></thead><tbody>
<tr><td><a href="api/v1/feed.json"><code>api/v1/feed.json</code></a></td><td>Every token in the index: verdict with receipts,
status, scores, identities and the evidence behind each score line.</td></tr>
<tr><td><a href="api/v1/tokens/{sample}.json"><code>api/v1/tokens/&lt;address&gt;.json</code></a></td><td>One token, the same shape.
Addresses are lowercase.</td></tr>
<tr><td><a href="api/v1/summary.json"><code>api/v1/summary.json</code></a></td><td>A small list for lookups: address, ticker, name,
Orbio agent id, verdict and status.</td></tr></tbody></table></div>
<h2>Fields worth knowing</h2><ul>
<li><code>verdict.verdict</code> is <code>verified</code>, <code>scam</code> or <code>unverified</code>. It's the same check the
index uses to decide what counts as a project's token. <code>verdict.receipts</code> links the post or page it rests on.</li>
<li><code>status</code> is <code>PROVEN</code>, <code>LIVE</code>, <code>BUILDING</code>, <code>UNPROVEN</code>, <code>ABANDONED</code> or
<code>SCAM</code> (<a href="method.html#5-status">rules</a>).</li>
<li><code>facts[]</code> are the score lines: <code>points × weight</code> is what counted, <code>at</code> is when it was observed.</li>
<li><code>method</code> names the scoring method. When it changes, everything is re-scored and the change is logged.</li></ul>
</article>"""


# ----------------------------------------------------------------- assets

CSS = """
:root{--bg:#f7f7f4;--fg:#15171c;--muted:#5d6370;--line:#e3e1db;--card:#fff;--link:#2849b8;
--ok:#17753f;--ok-bg:#e3f3e9;--bad:#b42323;--bad-bg:#fbe7e7;--warn:#7d5a00;--warn-bg:#fbf1d6;
--info:#2849b8;--info-bg:#e6ecfb;--dim:#5d6370;--dim-bg:#ecedf0;--bar:#15171c;--bar-bg:#e6e4de;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#0e1014;--fg:#e7e8eb;--muted:#9aa0ab;--line:#252932;--card:#15181e;--link:#8fb0ff;
--ok:#56c98f;--ok-bg:#10281b;--bad:#ff7676;--bad-bg:#2e1515;--warn:#f1b84f;--warn-bg:#2a2210;--info:#8fb0ff;--info-bg:#152042;
--dim:#9aa0ab;--dim-bg:#1e222a;--bar:#e7e8eb;--bar-bg:#262a33}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 var(--sans)}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
code{font:13px var(--mono);background:var(--dim-bg);padding:1px 5px;border-radius:4px;overflow-wrap:anywhere}
.wrap{max-width:1080px;margin:0 auto;padding:0 16px}
.top{border-bottom:1px solid var(--line);background:var(--card)}
.top .wrap{display:flex;align-items:center;gap:24px;min-height:56px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:8px;font-weight:700;font-size:17px;color:var(--fg)}
.brand img{width:22px;height:22px}.top nav{display:flex;gap:18px;flex-wrap:wrap}.top nav a{color:var(--muted)}
main{padding:28px 16px 48px}section{margin:40px 0}
h1{font-size:30px;line-height:1.2;margin:0 0 10px;letter-spacing:-.01em}h2{font-size:20px;margin:0 0 6px}h3{font-size:16px;margin:22px 0 8px}
.sub,.muted{color:var(--muted)}.sub{margin:0 0 14px}.muted{font-size:13.5px}
.hero{margin:8px 0 36px}.lede{font-size:17px;max-width:720px;color:var(--muted);margin:0 0 20px}
.search{position:relative;max-width:560px}
input[type=search]{width:100%;font:15px var(--sans);padding:11px 14px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--fg)}
input[type=search]:focus{outline:2px solid var(--info);outline-offset:1px}#filter{max-width:420px;margin:0 0 14px}
.results{position:absolute;z-index:5;left:0;right:0;top:calc(100% + 4px);background:var(--card);border:1px solid var(--line);border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.12);overflow:hidden}
.results a{display:flex;gap:10px;align-items:center;padding:9px 14px;color:var(--fg);border-top:1px solid var(--line)}
.results a:first-child{border-top:0}.results a:hover,.results a:focus{background:var(--dim-bg);text-decoration:none}
.results .none{padding:10px 14px;color:var(--muted)}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:22px}
.stats>div{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.stats b{display:block;font-size:26px;line-height:1.1;font-variant-numeric:tabular-nums}.stats span{color:var(--muted);font-size:13.5px}
.stats.small{grid-template-columns:repeat(5,1fr)}.stats.small b{font-size:20px}
b.ok{color:var(--ok)}b.bad{color:var(--bad)}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid var(--line);border-radius:12px;background:var(--card)}
table.list{width:100%;border-collapse:collapse;font-size:14.5px}
.list th{text-align:left;font-weight:600;color:var(--muted);font-size:12.5px;text-transform:uppercase;letter-spacing:.04em;padding:10px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
.list td{padding:10px 12px;border-top:1px solid var(--line);vertical-align:middle}.list tr:first-child td{border-top:0}
.list .num{width:1%;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}.list .score{text-align:right;font-weight:700;font-variant-numeric:tabular-nums;width:1%}
.list.ev td:first-child{width:34%}.list tr.zero td{color:var(--muted)}.wrapcell{overflow-wrap:anywhere}
.list.md td,.list.md th{vertical-align:top;text-transform:none;letter-spacing:0;font-size:14px}
.tok{display:inline-flex;align-items:baseline;gap:6px;color:var(--fg);flex-wrap:wrap}.tok b{font-weight:700}
.tok .agent{color:var(--muted);font-size:13px;font-variant-numeric:tabular-nums}.tok .nm{color:var(--muted);font-size:13.5px}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;font-weight:600;padding:2px 9px;border-radius:999px;white-space:nowrap}
.chip i{width:7px;height:7px;border-radius:50%;background:currentColor}
.v-verified.chip{color:var(--ok);background:var(--ok-bg)}.v-scam.chip{color:var(--bad);background:var(--bad-bg)}.v-unverified.chip{color:var(--warn);background:var(--warn-bg)}
.s-proven{color:#fff;background:var(--ok)}.s-live{color:var(--ok);background:var(--ok-bg)}.s-building{color:var(--info);background:var(--info-bg)}
.s-unproven,.s-abandoned,.s-excluded{color:var(--dim);background:var(--dim-bg)}.s-scam{color:var(--bad);background:var(--bad-bg)}
.dims{display:flex;gap:4px;min-width:150px}.dims .dim{flex:1}
.bar{display:block;height:6px;border-radius:3px;background:var(--bar-bg);overflow:hidden}.bar>span{display:block;height:100%;background:var(--bar)}
.dims.full{flex-direction:column;gap:8px;margin-top:12px}.dims.full .dim{display:grid;grid-template-columns:80px 1fr 56px;align-items:center;gap:10px}
.dims.full .bar{height:8px}.dl{font-size:13.5px;color:var(--muted)}.dv{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}.dv small{color:var(--muted);font-weight:400}
.feed{list-style:none;margin:0;padding:0;border:1px solid var(--line);border-radius:12px;background:var(--card)}
.feed li{padding:12px 16px;border-top:1px solid var(--line)}.feed li:first-child{border-top:0}
.feed .row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.feed .when{margin-left:auto;color:var(--muted);font-size:13px}
.feed p{margin:4px 0 0;color:var(--muted);font-size:14px;overflow-wrap:anywhere}.rcpt a{font-weight:600}
.crumb{margin:0 0 14px}.thead h1{margin-bottom:4px}.thead h1 .nm{color:var(--muted);font-weight:500}.thead p{margin:4px 0}
.addr{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.copy{font:12.5px var(--sans);padding:3px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);cursor:pointer}
.badge{display:inline-block;font-size:11.5px;font-weight:700;letter-spacing:.04em;padding:1px 7px;border:1px solid var(--line);border-radius:5px;margin-left:4px;color:var(--muted)}
.grid2{display:grid;grid-template-columns:1.3fr 1fr;gap:16px;margin:24px 0}
.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px}
.panel .ph{display:flex;justify-content:space-between;align-items:center;gap:10px}.panel h2{font-size:15px;margin:0;color:var(--muted);font-weight:600}
.verdict.v-verified{border-color:var(--ok);box-shadow:inset 4px 0 0 var(--ok)}.verdict.v-scam{border-color:var(--bad);box-shadow:inset 4px 0 0 var(--bad)}
.verdict.v-unverified{box-shadow:inset 4px 0 0 var(--warn)}
.why{font-size:18px;line-height:1.45;margin:12px 0 8px;font-weight:500}.receipts{margin:0 0 10px;padding-left:18px;overflow-wrap:anywhere}
.big{font-size:40px;font-weight:700;margin:8px 0 0;line-height:1;font-variant-numeric:tabular-nums}.big small{font-size:16px;color:var(--muted);font-weight:500}
.claim{font-size:16px}.desc{color:var(--muted);font-style:italic;overflow-wrap:anywhere}
.lvl{font-size:12.5px;font-weight:600;padding:1px 8px;border-radius:999px;background:var(--dim-bg);color:var(--dim);white-space:nowrap}
.l-bound,.l-bound-transitive{background:var(--ok-bg);color:var(--ok)}.l-contradicted,.l-disavowed{background:var(--bad-bg);color:var(--bad)}.l-unvouched,.l-named{background:var(--warn-bg);color:var(--warn)}
.outs{margin:0;padding-left:18px}.outs q{color:var(--muted)}
.prose{max-width:780px}.prose h1{margin-top:0}.prose h2{margin-top:34px}.prose p,.prose li{max-width:720px}
.prose pre{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;overflow-x:auto}.prose pre code{background:none;padding:0}
.prose .scroll{margin:14px 0}hr{border:0;border-top:1px solid var(--line);margin:28px 0}
.foot{border-top:1px solid var(--line);padding-top:18px;padding-bottom:40px;font-size:14px}.foot p{margin:4px 0}
@media (max-width:720px){h1{font-size:25px}.stats,.stats.small{grid-template-columns:repeat(2,1fr)}.grid2{grid-template-columns:1fr}
.hide-sm{display:none}.top .wrap{gap:10px;padding-top:8px;padding-bottom:8px}.top nav{gap:14px;font-size:14px}.why{font-size:16.5px}}
"""

JS = """
(function(){
  function ago(s){if(s<90)return'just now';var m=s/60;if(m<60)return Math.round(m)+' min ago';var h=m/60;
    if(h<36)return Math.round(h)+' h ago';return Math.round(h/24)+' days ago'}
  var now=Date.now()/1000;
  document.querySelectorAll('time[data-ts]').forEach(function(t){t.textContent=ago(now-(+t.dataset.ts))});
  document.querySelectorAll('[data-copy]').forEach(function(b){b.addEventListener('click',function(){
    if(!navigator.clipboard)return;navigator.clipboard.writeText(b.dataset.copy).then(function(){
      b.textContent='Copied';setTimeout(function(){b.textContent='Copy'},1500)},function(){})})});
  var f=document.getElementById('filter');
  if(f)f.addEventListener('input',function(){var v=f.value.trim().toLowerCase();
    document.querySelectorAll('[data-search]').forEach(function(r){var k=r.dataset.search||r.textContent.toLowerCase();
      r.hidden=!!v&&k.indexOf(v)<0})});
  var q=document.getElementById('q'),res=document.getElementById('results'),idx=document.getElementById('idx');
  if(!q||!res||!idx)return;
  var all=JSON.parse(idx.textContent),label={verified:'Verified',scam:'Scam',unverified:'Unverified'};
  function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
  q.addEventListener('input',function(){
    var v=q.value.trim().toLowerCase().replace(/^\\$/,'');
    if(!v){res.hidden=true;res.innerHTML='';return}
    var hits=all.filter(function(t){return t.t.indexOf(v)===0||(t.s||'').toLowerCase().indexOf(v)===0||(t.n||'').toLowerCase().indexOf(v)>=0}).slice(0,8);
    res.innerHTML=hits.length?hits.map(function(t){return'<a href="t/'+t.t+'.html"><b>$'+esc(t.s)+'</b>'+(t.a?'<span class="muted">#'+t.a+'</span>':'')+
      '<span class="muted">'+esc(t.n)+'</span><span class="chip v-'+t.v+'" style="margin-left:auto"><i></i>'+label[t.v]+'</span></a>'}).join('')
      :'<div class="none">'+(/^0x[0-9a-f]{40}$/.test(v)?'Not in the index yet. The index covers Orbio agents and the tokens projects claim.':'No match.')+'</div>';
    res.hidden=false});
  document.addEventListener('click',function(ev){if(!res.contains(ev.target)&&ev.target!==q)res.hidden=true});
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
        (tmp / rel).write_text(page(title, body, feed, depth, desc), "utf-8")

    put("index.html", f"{NAME}: which Orbio agents are real?", home(feed))
    put("agents.html", f"All Orbio agents · {NAME}", agents_page(feed))
    put("scams.html", f"Impersonators caught · {NAME}", scams_page(feed))
    put("method.html", f"Method · {NAME}", method_page(), desc="How the index decides which tokens are real.")
    put("api.html", f"API · {NAME}", api_page(feed))
    for t in feed["tokens"]:
        v = VERDICT[t["verdict"]["verdict"]]
        put(f't/{t["token"]}.html', f'${t["symbol"]}: {v} · {NAME}', token_page(t, feed), depth=1,
            desc=f'{v}: {t["verdict"]["why"]}.')
        (tmp / "api" / "v1" / "tokens" / f'{t["token"]}.json').write_text(
            json.dumps({"method": feed["method"], "generated_at": feed["generated_at"], **t}, ensure_ascii=False), "utf-8")
    (tmp / "api" / "v1" / "feed.json").write_text(json.dumps(feed, ensure_ascii=False), "utf-8")
    (tmp / "api" / "v1" / "summary.json").write_text(json.dumps(
        {"method": feed["method"], "generated_at": feed["generated_at"],
         "tokens": [{"token": t["token"], "symbol": t["symbol"], "name": t["name"], "orbio_agent": t["orbio_agent"],
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


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the public Proof site from the index")
    ap.add_argument("--out", type=Path, default=OUT, help=f"output folder (default {OUT})")
    args = ap.parse_args()
    import proof_index as p  # here, not at the top: the indexer imports this module to rebuild the site
    w.load_env()
    w.load_xlinks()
    t0 = time.time()
    n = build(p.export_data(p.open_db()), args.out)
    print(f"built {n} token pages into {args.out} in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()

# Proof: scoring methodology and funnel predicates

Status: draft v0.1 · method id `proof/0.1` · 2026-09-29

This spec defines how the index decides which tokens to look at (the funnel, stages 0–2), and how
it scores the ones it looks at (the methodology). Every threshold in §6 is calibrated against a
measured sample of Pons launches on Robinhood Chain and validated against Orbio vault tokens with
known outcomes (§8).

---

## 1. What the score answers

**"Is there a real project behind this token, and is it doing anything?"**

It does not answer "will the price go up". Safety appears only as integrity flags (§4.5).
Orbio affiliation, Build Week participation and winning are **badges**, not points: they say what a
project is, not whether it is real.

## 2. Principles

| # | Principle | Why |
|---|---|---|
| P1 | **Claims are leads; evidence is points.** Token metadata (description, socials) is self-asserted and scores nothing on its own. | It costs nothing to write, and clones copy it verbatim. 37% of launches share their X account or site with another creator's token (§8.3). |
| P2 | **Binding.** An external asset is attributed to a token only if the project behind the asset claims *that* token (§3.1). | Clones copy socials, not claims. In every clone cluster we resolved, the project claimed at most one of the cluster's tokens, and where one graduated it was the claimed one (§8.3). |
| P3 | **Every point has a receipt**: source, method, `observed_at`, content hash, cost. | A public index is only as credible as its audit trail. |
| P4 | **Evidence decays.** Each item has a half-life. A failed re-check doesn't erase the last good observation; it lets it fade. | Flaky sites shouldn't flip a status overnight, and abandoned projects should fade on their own. |
| P5 | **Dimensions, not one number.** Five dimensions plus a status; the composite exists only for sorting. | "72" says nothing. "Product live, no code, anonymous team" does. |
| P6 | **Deterministic checks award points; models only extract.** A model may read a page and return a fixed schema with quoted spans. Code checks each quote exists in the fetched page before anything counts. Models never output a score. | Fetched pages are attacker-controlled input (prompt injection). |
| P7 | **Versioned and public.** Every score carries its `method` id. A method change re-scores everything and is changelogged. | Stops quiet goalpost moves. |
| P8 | **Every exclusion has a public reason code, and appeals re-run the pipeline.** Humans can add evidence, never points. | An index that quietly drops things isn't trusted twice. |

## 3. Evidence records

Everything the index knows is an append-only evidence record. Scores are a pure function of the
records, the method id and the clock, so any score can be recomputed and audited.

```json
{
  "id": "ev_…", "token": "0x…", "kind": "x.binding",
  "subject": "x:collaraxyz",
  "claimed_by": "metadata.socials.twitter",
  "binding": "bound",
  "value": {"posts_read": 16, "cas_posted": ["0xba082576…", "0x19773082…", "0x3db7ca45…"], "sort": "Top"},
  "observed_at": 1790612345, "method": "x.binding.v1",
  "content_hash": "sha256:…", "cost_credit": "0.00374",
  "receipts": ["https://x.com/collaraxyz/status/…"]
}
```

### 3.1 Binding levels

| Level | Meaning | Weight |
|---|---|---|
| `bound` | The project claims this token: its **X account posted the CA** (primary channel), or its site shows the full or truncated CA (`0xd5D2…D8e2`), or a Pons / Orbio / DexScreener URL containing it. For vault launches, the site showing the vault's **owner or agent wallet** also binds. | 100% |
| `bound·transitive` | Vouched for by a bound asset: the bound X account's bio or posts link the site, or the bound site links the repo. When a token lists no site, the bound account's bio site is adopted. | 100% |
| `named` | Names the ticker and project name, but not the address, and isn't linked from a bound asset. | 40% |
| `unbound` | Only the token claims the asset. | 0% |
| `unvouched` | A site shows this CA, but the token also names an X account that neither claims the token nor ever linked the site (bio, posts read, builder posts orbio_watch recorded, or a `from:<handle> <site name>` search). | 0% |
| `contradicted` | The asset claims a **different** CA for the same project, and never this one: a labelled CA, a claimed token that points at the same asset, or a contract the X account posted itself (not in a warning) whose token has **this token's ticker** (@ordesk_bot posting $ORDESK #109 contradicts every other $ORDESK naming @ordesk_bot). An unlabelled post of another ticker (a partner's coin, a reply) contradicts nothing. | 0%, and triggers `SCAM` (§5) |
| `disavowed` | The project's account says a token isn't theirs, in a post ("It is not us. Ludi has no token.") or its bio ("NO CRYPTO TOKEN"). A post covers the tokens already out when it was made (and launches up to 5 minutes later). A post saying the project has **no token at all**, and the bio, also cover later launches for 30 days, judged from a read of the account made after the launch, until the account posts a contract of its own; "no token yet" never does. If the statement names tickers, it applies only to those. | 0%, and triggers `SCAM` |

A `contradicted` from an unlabelled post, and a `disavowed` from the bio or from a post made before the token
launched, never apply to a token that another official channel claims: verified wins.

Why X is primary and transitive binding exists: in the validation set, real project sites almost
never show their token's CA (0 of 7 after JS rendering; §8.4), while project X accounts routinely
post it. A site becomes `bound` through its bound X account.

Why `unvouched` exists: token metadata is self-asserted, so the site a token lists proves nothing about
who runs it. Anyone can put up a free site (a `*.up.railway.app` costs nothing) showing their own CA and list
it next to a real project's handle. When the X account claims the token, the project set the metadata, so its
site counts; when it doesn't, the site needs the account's link. X search doesn't match links (`url:` and the
bare host both miss a post that links the site), so the search uses the site's name as words and keeps a post
only if it links the site. HUNCH (#210) is bound through its site this way: @0xileri never posted the CA,
but linked hunch-agent.up.railway.app in their Build Week post, and the site's status API names #210 as its
own launch.

Why `contradicted` exists: it is the strongest clone signal available. The Talis and Occlusion sites
each name a CA that isn't the Orbio vault token pointing at them (§8.4).

### 3.2 Evidence kinds

| Kind | What | Tool and cost |
|---|---|---|
| `identity` | name, symbol, description, logo, socials at launch | 5 `eth_call`s, free RPC |
| `market` | end-holder counts at checkpoints (§6.2), phase, curve progress, graduation, creator flows | `chain.read` `eth_getLogs`, ~$0.00004 per token-checkpoint |
| `x.binding` | CAs the account itself posted | `social.x.posts` `from:<h>`, **`sort: "Top"`**, limit 20, ≤$0.0044 |
| `x.account` | account age, followers, post cadence over 4 weeks, profile URL | `social.x.profile`, $0.00022 |
| `site.fetch` | status, word count, CA / truncated CA / owner wallet present, ticker present, template, links, content hash | plain HTTP first (free); `web.scrape` if the page is JS-rendered, $0.0011 |
| `site.extract` | model: what the product claims to do, dated output items, each with quoted spans | `jev-1.13` or a small chat model, <$0.001 |
| `repo` | binding, created_at, commits in 7 and 30 days, contributors | GitHub API, free |
| `deployer` | Pons launch history and outcomes; first-seen age on 6 EVM mainnets | `chain.read`, ~$0.0001 |
| `orbio.agent` | vault id, stake, converted balance, CREDIT owed / claimed / activated, principal withdrawn, agent-wallet activity | Orbio public API, free |
| `integrity` | creator-fee recipient changes, principal withdrawals, creator exits | logs |

**`sort: "Top"` is required for X binding.** `sort: "Latest"` returns nothing for accounts a few
days old (tested: @collaraxyz and @errandboard return 0 posts on Latest and 16–18 on Top), and new
accounts are the whole population this index cares about.

**An account's Top 20 misses older posts**, so every token pointing at an account that wasn't seen
claiming gets a targeted search, `from:<handle> <CA>` (free when empty; it found @aitraderss claiming
AIMEMES, which its Top 20 didn't show). X search doesn't match an address inside a launchpad URL, so
posts orbio_watch recorded (a builder posting a CA or launchpad link) count as claims too.

**One team, several products.** An account claiming one token disowns only tokens *of the same
project*: a sibling from the same creator with the same name or ticker is `SUPERSEDED` (the team's
own earlier or test launch); one with a different name is the team's other product and is left alone
(@aitraderss runs AIMEMES and CURVETRUTH from one wallet).

## 4. Dimensions (composite 0–100)

Each item contributes `points × binding weight × decay(age)`, where
`decay = 0.5 ^ (max(0, age − 2 days) / half_life)`. Items are re-observed on a schedule (§7), and a successful
re-observation resets the age.

*Why evidence holds in full for 2 days (2 Oct):* `LIVE`'s thresholds sit on single items (Team 8 is a
`bound` X account; Product 18 is a site that's up and substantive). With decay from the moment of
observation, a re-score between checks put them at 7.9 and 17.x, and 85 tokens that met `LIVE` at their
own check showed `BUILDING`, among them $TANK, a Build Week winner. Two days is twice the daily
re-check of `LIVE` tokens (§7), so a token falls only when its checks stop finding the evidence.

### 4.1 Product: 0 to 30 · half-life 7 days
*Does something exist, and does it work?*

| Evidence | Points |
|---|---|
| Site fetched with status 200, `bound` or `bound·transitive` | 12 |
| Site is substantive: ≥150 words of non-boilerplate text, and not a template, "coming soon" or link-in-bio page | 6 |
| A functional surface reachable from the site (`/app`, `/docs`, an API host, a dashboard, a hash route like `#/board/6`) returns 200 with ≥60 prose words that aren't the homepage again (word-set overlap < 0.8) | 6 |
| Site content changed in the last 14 days (hash differs) | 6 |

A page with fewer than 150 prose words is re-read rendered (`web.scrape`) and the richer read is kept.
Most crypto sites are JS apps whose shell passes any character-count test while showing nothing:
errandboard.xyz is 46 prose words plain and 1,142 rendered. Surfaces are judged the same way, and
must differ from the homepage because single-page apps serve it on every route (`sw1pe.fun/api/health`
did).

### 4.2 Build: 0 to 20 · half-life 30 days
*Is code being written?*

| Evidence | Points |
|---|---|
| Public repo bound (mentions the CA or the bound site, or is linked from a bound asset) | 8 |
| Commits in the last 7 days | 4 (2 if only within the last 30) |
| Repo created before the launch, with ≥10 commits before it | 4 |
| ≥2 contributors with commits in the last 30 days | 4 |

### 4.3 Team: 0 to 20 · half-life 30 days
*Is someone accountable?*

| Evidence | Points |
|---|---|
| X account `bound` (it posted the CA) | 8 |
| Account age at launch: ≥90 days → 3, ≥1 year → 5 | ≤5 |
| Posted in ≥3 of the last 4 weeks | 3 |
| Creator (§6.1) first seen ≥90 days before launch on any of robinhood, ethereum, base, arbitrum, optimism, bnb | 4 |

Age is weighed carefully: @PackwoodAPP is a 2025 account with **zero posts**, and seven different
creators attached it to their tokens. Age without activity or binding is worth little.

### 4.4 Work: 0 to 20 · half-life 14 days
*Is it doing anything?* This is the hardest dimension and the one that matters most.

| Evidence | Points |
|---|---|
| **Observable output**: the bound site or account shows dated product output from the last 7 days (errands completed, reports published, alerts sent, features shipped): 1 item → 4, 2 → 7, 3+ → 10, site and posts sharing the 10. **Never the token itself**: launches, burns, buybacks, airdrops, holder counts and prices are not product work. | up to 10 |
| Product contracts (discovered from the bound site or repo) called by ≥10 distinct non-team addresses in the last 7 days | up to 6 |
| Orbio agents: agent wallet active on-chain in the last 7 days, beyond token trading | 2 |
| Orbio agents: CREDIT activated in the last 7 days | 2 |
| Orbio agents: Orbio-attested gateway spend, once Orbio publishes it (§9) | replaces the two lines above, up to 8 |

**How outputs are verified (P6).** One call to `deepseek/deepseek-v4-pro` with reasoning off (about
0.0006 CREDIT) reads the site and the account's last 28 days of posts. It returns a product claim and
outputs, each with a quote. Code then keeps:

- a site item only if its quote is on the page (compared formatting-blind, since models quote
  rendered markdown with or without the markup) and a date within a day of the claimed one appears
  within 160 characters of it ("6h ago" counts, resolved against the fetch time);
- a post item only by its index, dated by the post's own timestamp;
- the product claim only if its quote is on the page.

The model's raw answer is stored, and verification is a pure function re-run on every refresh, so the
model is only asked again when the content changes.

**Which model reads (calibrated 2026-10-02, `extract.v3`).** A post item rests on the model's judgment:
code only checks that the post exists. So the reader was chosen against blind labels. Graders who
never saw a model's answer marked 72 of 497 posts from 40 projects as reports of delivered work.
- `deepseek/deepseek-v4.1-flash`, the reader until then, credited none of them.
- `deepseek/deepseek-v4-pro` credited 52, and 73% of what it credited was real, once the prompt asked
  for one delivered instance each and ruled out plans, previews, general descriptions of the product
  and marketing (`extract.v3`). On the old prompt it found more but half its picks were wrong.
- Claude Sonnet 5.5 was as thorough and more precise (42 found and 3 wrong, on the first 25
  projects), at over 20 times the cost.

Most rejected site items are paraphrases rather than near-misses (142 of 146 across models), so the
quote check stays strict. A model with no provider hands over to the next one
(`PROOF_MODEL_FALLBACKS`), and a fallback's answer is asked again once the first model answers. The
one-line read stays on `deepseek/deepseek-v4.1-flash`, which wrote those as well as the others.

### 4.5 Integrity: 0 to 10, plus caps
*Is the token itself structurally sound?*

| Evidence | Points |
|---|---|
| Top-10 holders (excluding curve, pool, locker, vault) own <30% → 4, <50% → 2 | ≤4 |
| Creator did not exit: sold <50% of their own launch buys within 24h (flag `CREATOR_EXIT`, §6.2) | 3 |
| No creator-fee recipient change in the first 7 days, and no principal withdrawal at the Orbio cliff | 3 |

The last two lines state flags, and a flag can be raised between dossiers: `CREATOR_EXIT` at a later
holder check, `FEE_REDIRECT` at ingest. So both lines come from the token's flags through one rule. When
one of these flags is raised on a token that has a dossier, the token is re-scored then, and `rescore`
records the line again wherever the latest observation disagrees with the flags. *Why (2026-10-02):*
$GBLC's 30-minute check saw its creator still holding and its dossier wrote "creator did not exit". The
6-hour check then found the sell, but the stale line kept Integrity at 10/10 next to a `CREATOR_EXIT`
flag until the next dossier. 13 tokens were in that state.

**Caps** apply after summing and override everything:

| Condition | Composite capped at |
|---|---|
| `CLONE` (§5) | 10 |
| Borrowed identity: an unbound social points at another organisation's brand (S0.4) | 20 |
| Serial creator (S0.1) | 30 |
| `CREATOR_EXIT` **and** creator fees redirected to a fresh wallet | 20 |

## 5. Status

What users see first. Derived, mutually exclusive, and evaluated top to bottom:

| Status | Rule |
|---|---|
| `SCAM` | It impersonates a project: an asset it claims is `contradicted` or `disavowed`, or it's in a resolved cluster (§6.4) where another token is `bound` and it isn't. The explanation always quotes the evidence (the official post or the address the project claims instead). Formerly `CLONE`. |
| `EXCLUDED` | Failed a funnel predicate; the record is kept, the reason code is public, and it can be appealed |
| `ABANDONED` | Was `BUILDING` or better, and has had no fresh Product, Build or Team evidence for 30 days |
| `PROVEN` | Product ≥18, Work ≥10 and Team ≥8, all sustained for 7 consecutive days |
| `LIVE` | Product ≥18, and Team ≥8 or Build ≥8, **and something verifiably works**: a functional surface, a verified product output, or outside users of its contracts |
| `BUILDING` | At least one `bound` asset, and composite ≥20 |
| `UNPROVEN` | In the index, with nothing bound yet |

**Badges** (independent of status): `ORBIO AGENT` (vault launch), `BUILD WEEK` (bound to an approved
builder), `WINNER`, `GRADUATED`.

*Why LIVE needs "works" (calibrated 2026-09-29):* without it, a site that changes plus an account
posting the CA was enough. An NFT mint page reached LIVE that way, while 10 of the 12 strongest claimed
projects clear the stricter bar on their own.

**Method versions in practice (P7).** Each fact line carries the version of the method that made it.
When a collection method is corrected, facts from the old version are **discarded, not faded**: they
weren't stale, they were wrong. Examples: `extract.v1` counted token burns as output, `extract.v2`
credited almost no work reported in posts (§4.4), and `surface.v1` accepted SPA catch-alls. `rescore` recomputes every score from stored evidence without fetching
anything.

---

## 6. The funnel

The rule: **never spend on a token that hasn't shown it's worth a look.** Stage 0 costs nothing,
stage 1 fractions of a cent, and binding (stage 2) is where the first real spend happens.
Measured volume: **~5,960 launches/day** on Robinhood Chain (12,664 in 51h), of which ~1% graduate.

**Orbio vault launches skip stages 0 and 1** (S0.0) and get full coverage: ~44/day, ~$2/day.

**Scope.** The index runs on Orbio launches first (`PROOF_SCOPE=orbio`, the default). Every Pons
launch is still ingested, but a non-Orbio launch is only read when a project claims it, which is how a
vault impersonator is caught when the real token was launched directly on Pons. `robinhood` runs the
whole funnel below on every launch.

**Official verification (the alert gate).** A token is *verified* only when the project's X launch
post or website lists this exact address and no official channel contradicts or disowns it. The website
is the project's only if its X account claims the token or links the site (`unvouched`, §3.1). It's
*scam* when one lists a different address or disowns it, and *unverified* otherwise, including when
official sources disagree. A README counts toward Build, but not toward verification. Orbio agents
that nothing claims yet are re-checked at 15 min, 1 h, 3 h, 6 h, 24 h and 72 h, reading the account
fresh (Top and Latest). The official launch post, or a "that's not us" PSA, typically lands minutes
after the launch.

### 6.1 Identities

- **Creator** = the `deployer` in `TokenLaunched`, except for the Orbio AgentVault, where it is the
  agent's `owner`. Contract deployers are users' own smart-account / 7702 wallets, not launchpads:
  243 contract deployers in the sample mapped 1:1 onto 242 signers.
- **End holder** = in any transaction containing a transfer *out of* the curve or the Uniswap v4
  PoolManager (`0x8366…0951`), an address with positive net token inflow in that transaction. It
  excludes the curve, PoolManager, locker (`0x2674…4952`), fee escrow (`0xd3af…ac9e`), buyback vault
  (`0x42df…219c`), factory, AgentVault, the creator, and any address that received curve buys in
  ≥10 distinct tokens in the trailing 24h (routers and sniper bots).
  *Why:* 189 router/bot addresses receive **63% of all curve buys**. Counting transfer recipients
  understates real holders up to 14× (STABLE: 92 recipients, 1,338 end holders at T+30m).
- **Identity key** of a social: X handle (lowercased); site domain root, except hosting platforms
  (vercel, netlify, railway, fly, kicker, …) where it's host + path.

### 6.2 Stage 0 — at launch (T+0)

Inputs: the `TokenLaunched` log (token, curve, deployer, pairToken, graduationThreshold) plus five
`eth_call`s. Cost: free RPC.

| Code | Predicate | Action | Calibration (810-launch sample) |
|---|---|---|---|
| S0.0 `VAULT` | deployer is the Orbio AgentVault | Orbio tier: skip S0/S1 drops, full coverage | 1.5% of launches |
| S0.1 `SERIAL` | creator launched **≥3** tokens in the trailing 24h (this one included) | `EXCLUDED` | 17.8% of launches; **0 of 8 graduates**; 0 of the validation projects. Every graduate's creator launched exactly once that day. (≥2 would also lose no graduates, at 24.9%; ≥3 allows one relaunch after a mistake.) |
| S0.2 `NO_LEAD` | no website (non-social host), no X handle and no GitHub link | `EXCLUDED` from spend, still answerable on lookup as `UNPROVEN` ("nothing to verify") | 11% of launches after S0.1; 1 graduate (LAUNCHNEAR, a pure meme); 1 validation token (TRENDAR, which has no socials at all) |
| S0.3 `CLUSTER` | its identity key is shared with a token from a **different creator** launched in the trailing 48h | Mark the cluster; resolved once in stage 2 | 37% of launches sit in a cluster; 110 clusters per 3 hours; 6 of 8 graduates are in one |
| S0.4 `BORROWED` | a social points at a known brand host (wikipedia, orbio.so, robinhood.com, uniswap.org, dexscreener, etherscan), or all socials are bare roots (`x.com/`, `t.me/`) | Flag; cap 20 unless later bound | 0.7% of launches |

**Stage 0 keeps 71% of launches (~4,250/day), 7 of 8 graduates, and 9 of the 10 genuine validation
projects.**

### 6.3 Stage 1 — attention (T+30m, T+6h; late re-checks T+24h and T+7d)

Inputs: one `eth_getLogs` of the token's `Transfer` events per checkpoint.

| Code | Predicate | Action | Calibration |
|---|---|---|---|
| S1.1 `ATTENTION` | **≥10 end holders at T+30m, or ≥20 at T+6h** | Pass to stage 2 | Graduates had ≥158 end holders at T+30m; non-graduates median 1, p90 25, p95 37. The T+6h arm rescues slow starters: BREAD 1 → 28, BASIS 1 → 37, ARTISAN 0 → 23, TALIS 0 → 36. |
| S1.2 `QUIET` | fails S1.1 | `EXCLUDED` (re-checked at T+24h and T+7d against the T+6h bar) | |
| S1.3 `GRADUATED` | `PoolGraduated` emitted | Pass immediately | |
| S1.4 `BYPASS` | the free plain-HTTP site fetch finds the token's full or truncated CA (or the vault owner wallet); or a watched account posted the CA | Pass regardless of attention | Catches quiet launches whose sites bind; 2 of 11 validation sites bind in raw HTML |
| S1.5 flags | `CREATOR_EXIT`: creator sold ≥50% of their own launch buys before the checkpoint. `FEE_REDIRECT`: `CreatorFeeRecipientUpdated` within 7 days, outside the launch transaction, to an address **with no code** that isn't the creator. `LAUNCH_BUNDLE`: ≥3 end holders in the launch block. | Record for §4.5; **no gating** | `CREATOR_EXIT`: 87 of 128 creators who bought at launch; none of them graduated. `FEE_REDIRECT`: recipient changes hit ~8% of launches, but every one of 60 sampled pointed at a per-token contract with identical 291-byte code (routine fee-contract setup), so only a switch to a plain wallet counts. `LAUNCH_BUNDLE`: 0.4%, rare because Pons' snipe tax starts at 99% and decays over 3 seconds, so the real bundle detector is funding-source clustering (later). |

**Stages 0 and 1 together keep ~17% of launches (~1,000/day), 7 of 8 graduates, and 7 of the 10
genuine validation projects** (§8.2). Promoting `CREATOR_EXIT` from a flag to a gate would cut that
to ~12% without losing a graduate; it's a cost lever, held back because a team selling its launch
buy doesn't make the project unreal.

**Known blind spot: quiet launches.** CLUSTLY (#100, 8 holders at T+6h) and PRIOR (#119, 1 holder)
point at real products and launched to near-silence, so they're excluded until something re-admits
them. (Neither project's site shows a CA, so whether they claim these tokens is unverified.) The
re-entry triggers: a lookup by any user, the T+24h and T+7d re-checks, a bound site (S1.4), a watched
account posting the CA, and graduation. On lookup, an excluded token shows as "not yet reviewed",
never as fake.

### 6.4 Stage 2 — binding resolution (the first real spend)

Resolve **per identity key, not per token**, cached 24h:

1. X: `from:<handle>`, `sort: "Top"`, limit 20 → which CAs did the account itself post?
2. Site: plain fetch (free); `web.scrape` only if the page is JS-rendered → full CA, truncated CA,
   vault owner wallet, or a *different* CA (`contradicted`).
3. Assign `bound` / `bound·transitive` / `named` / `unbound` / `contradicted` to every token
   sharing the key. In a cluster, the claimed token is `bound` and the rest become `CLONE`.

Only tokens with at least one `bound` asset proceed to the full dossier (stage 3), which fills the
dimensions in §4.

## 7. Refresh schedule and cost

| Status | Site | X | Repo | Market | Work |
|---|---|---|---|---|---|
| `LIVE`, `PROVEN`, Orbio agents at `BUILDING` or better | daily | daily | daily | daily | daily |
| `BUILDING` | 3 days | 3 days | 3 days | 3 days | 3 days |
| `UNPROVEN` (incl. other Orbio agents) | T+24h, T+72h, T+7d, T+14d, then archived; Orbio agents then weekly, never archived | same | same | same | same |
| `ABANDONED` | weekly | weekly | weekly | weekly | weekly |
| `EXCLUDED`, `CLONE` | re-entry triggers only | | | | |

Each refresh is one dossier covering every dimension. Refreshing every Orbio agent daily, as first
drafted, would spend the whole default budget on vault memes. Measured: **~$0.003–0.005 and ~15–20 s
per dossier**, most of it under the self-imposed 60 requests/min.

Estimated cost at full Robinhood Chain coverage:

| Step | Volume/day | Unit cost | $/day |
|---|---|---|---|
| Ingest `TokenLaunched` logs | ~5,960 | 1 `getLogs` per 200k blocks | <0.01 |
| Stage 0 metadata | ~5,960 | free RPC | 0 |
| Stage 1 holder counts (2 checkpoints + re-checks) | ~9,000 | ~$0.00004 | ~0.40 |
| Stage 1 bypass site fetch | ~1,300 | free | 0 |
| Stage 2 X binding, per identity (≈1 per survivor before caching) | ~1,000 | $0.0044 | ~4.40 |
| Stage 2 rendered site fetch, when needed | ≤600 | $0.0011 | ≤0.66 |
| Stage 3 dossier, bound tokens only (measured) | ~200–300 | ~$0.004 | ~1 |
| Refreshing tracked tokens (grows with the index) | ~500 dossiers | ~$0.004 | ~2 |
| **Total** | | | **~$8–10/day** |

The Orbio tier alone is ~$2/day. An average Orbio token's fee stream produces ~$33/day of compute.

## 8. Calibration evidence

Measured 2026-09-29. Total cost of calibration: ~0.08 CREDIT.

### 8.1 Data

- **Context:** every `TokenLaunched` event from the Pons factory in the 51h to 2026-09-29 00:15 UTC:
  12,664 launches, 145 graduations.
- **Sample:** all 810 launches from 27h to 24h before that, so each has a full 24h of outcomes.
  Pair tokens: ETH 86%, tokenized NVDA 4%, USDG 3.5%, ORBIO 1.5%. 8 graduated within 24h (0.99%).
- **End-holder subset:** all 8 graduates plus 150 random non-graduates with at least one buy.
- **Validation:** all 175 Orbio vault tokens, run through the funnel as if launched directly on Pons,
  labelled with the orbio-watch tiers and a hand-checked set of 13 projects.

### 8.2 Funnel results

| Group | Pass stage 0 | Pass stages 0+1 |
|---|---|---|
| Sample (all launches) | 71% | ~17% |
| 24h graduates (8) | 7 | 7 |
| Validation: genuine projects (10) | 9 | 7 — misses: TRENDAR (no socials), CLUSTLY and PRIOR (quiet) |
| Validation: tokens pointing at a real project that the project doesn't claim (3: OCCLUS #111, TALIS #112, COLLARA #168) | 3 | 2, both then caught in stage 2 as `contradicted` |
| Validation: orbio-watch "meme/low" tier (97) | 33% | 8% |

The hand-checked set started as 13 "known real" projects; binding showed three of them are not the
tokens those projects claim (§8.4), which is the method working on its own validation set.

### 8.3 Clusters: shared identities resolve to one claimed token

| X account | Tokens pointing at it | Distinct creators | Token the account posted | Outcome |
|---|---|---|---|---|
| @collaraxyz | 6 | 6 | `COLL` 0xba08… | the claimed token graduated; 5 clones |
| @stablecashcc | 7 | 7 | `STABLE` 0x4515… | the claimed token graduated; 6 clones |
| @0xhashlings | 5 | 4 | `HASHLINGS` 0x0e8b… | the claimed token graduated; 4 clones |
| @PackwoodAPP | 7 | 7 | none (account has 0 posts) | all 7 borrowing a dormant account |

The largest clusters also include narrative memes pointing at someone else's post (9 tokens citing
an @RobinhoodApp post, 12 citing @VitalikButerin) and one false cluster from a hosting platform
(`kicker`), which is why platform hosts key on host + path.

### 8.4 Sites rarely bind, and sometimes contradict

- Raw HTML: 2 of 11 real project sites contain their token's full CA (ASK, AIMEMES). All 11 name the
  ticker.
- JS-rendered via `web.scrape`: 0 of 7 show the vault token's CA. ERRAND's site shows its **vault
  owner wallet** `0xa7de…` (hence owner-wallet binding); TRENCH and PRIOR show their protocol wallets
  and contracts.
- **Contradictions:** the Occlusion site claims `$OCLU` = `0x449ca622…9b95`, a direct Pons launch,
  not vault #111 "OCCLUSION NETWORK" (`0x5f67…`). The Talis site claims `0xd5D2…D8e2`, which is
  neither vault #68 nor #112 "TALIS". @collaraxyz never posted vault #168 "COLLARA" (`0x776c…`); it
  claims `COLL` on Pons. Three of the 13 "known real" validation entries were therefore tokens
  borrowing a real project's identity. Keyword or social-link scoring can't see that; binding can.

## 9. Orbio dependency: "Proof of Inference" is not publicly measurable yet

What the public API exposes per agent (`/api/protocol/agents/{id}`): stake, withdrawn principal,
converted balance credited, CREDIT owed / claimed / activated, and hourly reward epochs.

What it does **not** expose: **gateway spend**. The 45% converted share is credited straight to
the agent's gateway balance and spent off-chain, so it is invisible. Example: ERRAND (#106) has
$6,414 credited and `activatedAtoms: 0`. It may be spending heavily; nobody outside can tell.

Consequences for the method:

- Activated CREDIT measures "claimed staking rewards", not consumption. Most agents never need to
  claim when they hold a converted balance. It gets 2 points, not a headline.
- **Do not publish ecosystem "spend" figures** derived from activated CREDIT. They would be wrong
  by orders of magnitude, and an index caught overstating a number on day one doesn't recover.
- **Ask Orbio** to publish `usage.lifetimeMicroUsd` and `usage.last7dMicroUsd` per agent on
  `/api/protocol/agents/{id}`. They already account it for the owner dashboard. When it exists,
  "attested gateway spend" replaces the two weak Work lines (§4.4), and Proof of Inference becomes
  the headline metric it should be. The index is also the reason for Orbio to publish it.

## 10. Anti-gaming

| Attack | Mitigation |
|---|---|
| Clone copies the real project's socials | Binding (P2) and cluster resolution (§6.4): the project's account claims one token; the copies become `CLONE`. |
| Launch before the real project does, using its socials | Same. The sample's COLLARA, STABLE and HASHLINGS clones all launched around the real token; only the claimed one is `bound`. |
| Put the CA on a site you control | Allowed: binding means "the site's owner claims this token". It earns Product points only if the site is substantive and functional. |
| A project posts several CAs | All posted CAs are `bound` (Collara posted three); none of the rest is. |
| Bought or aged X account | Age is capped at 5 points and needs activity; binding needs the account to post the CA itself. |
| Commit stuffing | Build is capped at 20; commits count once per author-day; "predates launch" and contributor count weigh more than volume. |
| Wash buyers to pass stage 1 | Stage 1 gates attention only; it awards no points. Router and bot addresses are excluded from holder counts. Funding-source clustering is a later stage-2 feature. |
| Prompt injection in fetched pages | P6: the model returns a fixed schema with quotes; code verifies every quote exists in the page; the model has no tools and never scores. |
| Fake "dated output" | Output items must resolve (an on-chain tx, a post, a page that exists); identical items count once. |
| Appeal spam | Appeals re-run the pipeline, are rate-limited per token, and queue behind scheduled work. |

## 11. Mapping onto `orbio_watch.py`

**Reuse as is:** `rpc`, `eth_call`, `abi_strings`, `read_agent`, `pons_state`, `dexscreener`,
`orbio_tool`, the GitHub helpers, `norm` / `domain_root` / `handle`, and the builder and xlinks
registries (they become the input to the `BUILD WEEK` badge and S1.4's watched accounts).

**Change:**

- `classify()` splits in two. Its keyword signals (`UTILITY_KW`, `MEME_KW`, `ORBIO_KW`, description
  length) become optional stage-2 triage features with zero public points (P1). The Build Week (+8),
  winner (+6) and "tags @orbiodotso" (+3) points become badges. The rest is replaced by
  evidence-based dimension scoring.
- `tier` (ORBIO / utility / maybe / meme) becomes `status` (§5). The copycat block becomes cluster
  resolution with binding (§6.4). `same_owner` becomes S0.1.
- `website_check()` becomes `site_fetch()`, returning the evidence value (status, word count, CA /
  truncated CA / owner wallet present, ticker present, template, links, content hash), with
  `web.scrape` as the fallback for JS-rendered pages and blocked networks.
- `x_fetch()` uses `sort: "Latest"`, which misses brand-new accounts. Binding lookups must use `Top`.

**Add:**

- `ingest_launches()`: `TokenLaunched` logs for **all** Pons launches via `chain.read` `eth_getLogs`
  (Alchemy accepts 200k-block ranges; drpc caps at 100). Topic
  `0x8d4aad49…a89607`; also `PoolGraduated` `0x0a44ef75…c259`, `LaunchSwept` `0xcdb72f15…b6b4`,
  `CreatorFeeRecipientUpdated` `0x308c390e…a980`.
- `holders(token, checkpoint)`: end-holder counting per §6.1 from the token's `Transfer` logs. No
  curve ABI is needed (the curve contract is unverified).
- `resolve_identity(key)`: stage 2, cached per identity.
- An evidence store. SQLite rather than JSON files at ~6,000 launches/day.
- `score(token, now, method)`: a pure function over evidence records.
- Throttling: the public RPC returns 429s above ~40 calls/s (batches of 10 work); the Orbio gateway
  allows 120 requests/min per key, shared with anything else using that key.

**Your own alerts** become a consumer of the index: "tell me when a token with `ORBIO AGENT` or
`BUILD WEEK` reaches `BUILDING` or better" replaces the current combined ORBIO-tier score.

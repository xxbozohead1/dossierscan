# Dossier

**[dossierscan.com](https://dossierscan.com)** · independent verification for tokens launched on
[Orbio](https://www.orbio.so/launchpad/).

Dossier opens a file on every Orbio launch. It checks the project's official X account and website for that exact
contract, flags the impersonators that copy a real project's name and socials, and scores whether anything is actually
being built. Every verdict links its receipt: the post or page it rests on.

## What's in this repository

This is the public half of Dossier:

| Path | What |
|---|---|
| `proof_site.py` | Builds the site from the index: the feed, a file per token, the impersonator list, the agents table, the $DOSSIER page and the free JSON API under `api/v1/` |
| `docs/token.md` | The $DOSSIER whitepaper, rendered at [dossierscan.com/token.html](https://dossierscan.com/token.html) |
| `deploy/` | How it runs: a Hetzner server behind Cloudflare, with systemd services and Caddy |
| `static/` | The token logo and the link-preview card, with the HTML they're rendered from |

The indexer that reads the chain, X and project websites and reaches the verdicts isn't published here. The rules it
follows are: [dossierscan.com/method.html](https://dossierscan.com/method.html). Keeping the code private makes the
checks harder to game. The rules themselves are the same for every token, $DOSSIER included.

## $DOSSIER

Dossier's token, launched on the Orbio launchpad (agent #282). Its trading fees pay for the checks.

**Official contract: `0xe4d41dfec020cf616c3b4c7b805cba9ad9a82878`**

It's also published at [dossierscan.com/token.html](https://dossierscan.com/token.html). Any other address using the
name is an impersonator.

## Data

Everything on the site is also free JSON:
[feed](https://dossierscan.com/api/v1/feed.json) · [summary](https://dossierscan.com/api/v1/summary.json) ·
[live numbers](https://dossierscan.com/api/v1/live.json) · one token: `api/v1/tokens/<address>.json`.

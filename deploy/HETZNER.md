# Running Dossier on a Hetzner server behind Cloudflare

One server runs everything: the watcher, the Proof indexer (which rebuilds the site every few minutes), the bot, and
Caddy serving `data/site`. Cloudflare sits in front of it: your domain's DNS, HTTPS for visitors, caching and DDoS
protection. Nothing is deployed anywhere else. A new verdict is live on the site as soon as the indexer writes it.

```
visitor ──https──▶ Cloudflare (proxy) ──https, origin cert──▶ Caddy on the server ──▶ /opt/orbio/data/site
                                                               orbio-proof ─ rebuilds data/site, posts verdicts
                                                               orbio-watch ─ launch + builder alerts (Telegram)
                                                               orbio-bot   ─ the Telegram bot (optional)
```

## What you need

- **A server:** Hetzner Cloud, Ubuntu 24.04. The smallest shared plan (2 vCPU, 4 GB) is plenty. Turn on Hetzner's
  backups when you create it: `data/proof.db` is the only copy of the index.
- **A domain on Cloudflare** (the free plan). Either buy it through Cloudflare Registrar, or add it to Cloudflare and
  change the nameservers where you bought it.
- **The code on GitHub:** the server clones it. Push the `proof-indexer` branch (or merge it into `main`) first.

## Steps

**1. Cloudflare DNS and HTTPS.** In the Cloudflare dashboard for your domain:
- **DNS → Records:** add an `A` record. Use `@` for the bare domain or a name such as `dossier` for a subdomain,
  point it at the server's IPv4 address, and leave **Proxy status: Proxied** (orange cloud). For a bare domain, also
  add a `CNAME` record from `www` to the domain, also Proxied. Caddy redirects `www.` to the bare domain.
- **SSL/TLS → Overview:** set the mode to **Full (strict)**.
- **SSL/TLS → Edge Certificates:** turn on **Always Use HTTPS**.
- **SSL/TLS → Origin Server → Create Certificate:** keep the defaults (your domain and `*.` your domain, 15 years). Leave
  the page open: step 4 needs the certificate and the private key, and the key is shown only once.

**2. Clone the code on the server** (as root):
```bash
git clone -b proof-indexer https://github.com/<owner>/<repo>.git /opt/orbio
```
If the repository is private, give the server a read-only deploy key first: run `ssh-keygen -t ed25519`, add
`~/.ssh/id_ed25519.pub` under GitHub → the repo → Settings → Deploy keys, then clone
`git@github.com:<owner>/<repo>.git` instead.

**3. Run the setup script** with your domain and timezone. It installs Python and Caddy, creates the `orbio` user,
installs the services, the Caddyfile and log rotation, and opens ports 22, 80 and 443. It starts nothing yet.
```bash
bash /opt/orbio/deploy/setup.sh dossier.example.com Asia/Singapore
```

**4. Install the origin certificate.** Paste the certificate from step 1 into `/etc/caddy/cf-origin.pem` and the private
key into `/etc/caddy/cf-origin-key.pem`, then:
```bash
chown caddy:caddy /etc/caddy/cf-origin*.pem && chmod 600 /etc/caddy/cf-origin*.pem
```

**5. Move the state from Windows.** On the Windows machine, in the repo folder:
```powershell
powershell -ExecutionPolicy Bypass -File deploy\push_state.ps1 -Server root@<server-ip>
```
It stops and disables the `orbio-watch` and `orbio-proof` tasks first (two copies would poll X twice and post to
Telegram twice), then copies `data\` and `.env` across. The site folder, logs and heartbeats aren't copied: the site is
rebuilt on the server.

**6. Add the site's address, then start.** On the server, add these lines to `/opt/orbio/.env`:
```
DOSSIER_SITE_URL=https://dossier.example.com
PROOF_DAILY_BUDGET=3
```
- `DOSSIER_SITE_URL` gives every page its link preview and canonical address, adds the sitemap, and links public
  channel posts and bot replies to the token's file.
- `PROOF_DAILY_BUDGET` is the indexer's CREDIT per day. At 1, stage 2's share ran out at 20:34 UTC on 28 Sept and
  15:33 UTC on 29 Sept, and re-checks of unclaimed agents then waited for midnight UTC. First checks of new agents no
  longer wait, but re-checks still do. 3 leaves room for them.

Then start the services:
```bash
systemctl enable --now orbio-watch orbio-proof
systemctl restart caddy
```
Once the bot has its token (`DOSSIER_BOT_TOKEN` in `.env`), start it too: `systemctl enable --now orbio-bot`.

**7. Check it.**
```bash
systemctl status orbio-watch orbio-proof caddy --no-pager
tail -f /opt/orbio/data/proof.log      # a "pass …" line about once a minute; the site is rebuilt on the first pass
curl -sI https://dossier.example.com/ | head -3                                        # HTTP/2 200
curl -s -o /dev/null -w '%{http_code}\n' https://dossier.example.com/t/0x0000000000000000000000000000000000000000.html   # 404
```
Open the site on your phone. Paste its address into a Telegram chat (Saved Messages is fine) and a preview card should
appear. In the first minutes you may get one ⚠️ health alert while the second process starts; a ✅ follows.

## Later

- **Updating the code:**
  ```bash
  cd /opt/orbio && git pull && chown -R orbio:orbio . && systemctl restart orbio-watch orbio-proof
  ```
  Also restart `orbio-bot` if it runs.
- **Logs:** `data/proof.log`, `data/watch.log` and `data/bot.log`, with crash output in `*.console.log`. They're rotated
  weekly and kept for 8 weeks.
- **Stopping everything:** `systemctl stop orbio-watch orbio-proof orbio-bot`.
- **Only Cloudflare may reach the web ports (optional).** Visitors can't reach the server directly anyway. To make
  sure, replace the `80/tcp` and `443/tcp` ufw rules with rules allowing only
  [Cloudflare's IP ranges](https://www.cloudflare.com/ips/).
- **Cloudflare Pages instead of Caddy:** the indexer can push the site to Pages (`DOSSIER_CF_PROJECT`, README). It
  needs Node.js and deploys on every page change plus every 10 minutes for prices, about 5,000 deploys a month.
  Cloudflare's docs don't say whether that counts toward the free plan's 500 builds a month, so serving from the
  server is the safer choice.

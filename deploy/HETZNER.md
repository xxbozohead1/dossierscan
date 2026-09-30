# Running Dossier on a Hetzner server behind Cloudflare

One server runs everything: the watcher, the Proof indexer (which rebuilds the site every few minutes), the bot, and
Caddy serving `data/site`. Cloudflare sits in front of it: your domain's DNS, HTTPS for visitors, caching and DDoS
protection. Nothing is deployed anywhere else. A new verdict is live on the site as soon as the indexer writes it.

```
visitor ──https──▶ Cloudflare (proxy) ──https──▶ Caddy on the server ──▶ /opt/orbio/data/site
                                                  orbio-proof ─ rebuilds data/site, posts verdicts
                                                  orbio-watch ─ launch + builder alerts (Telegram)
                                                  orbio-bot   ─ the Telegram bot (optional)
```

## What you need

- **A server:** Hetzner Cloud, Ubuntu 24.04 or later. The smallest shared plan (2 vCPU, 4 GB) is plenty. Turn on
  Hetzner's backups: `data/proof.db` is the only copy of the index.
- **A domain on Cloudflare** (the free plan). Either buy it through Cloudflare Registrar, or add it to Cloudflare and
  change the nameservers where you bought it.
- **The code on GitHub:** the server clones it. Push the `proof-indexer` branch (or merge it into `main`) first.

## Steps

**1. Cloudflare DNS and HTTPS.** In the Cloudflare dashboard for your domain:
- **DNS → Records:** add an `A` record. Use `@` for the bare domain or a name such as `dossier` for a subdomain,
  point it at the server's IPv4 address, and leave **Proxy status: Proxied** (orange cloud). For a bare domain, also
  add a `CNAME` record from `www` to the domain, also Proxied. Caddy redirects `www.` to the bare domain.
- **SSL/TLS → Overview:** set the mode to **Full (strict)**.
- **SSL/TLS → Edge Certificates:** leave **Always Use HTTPS** off. Caddy gets its certificate from Let's Encrypt, whose
  check (every renewal, about every two months) has to reach the server over plain HTTP. Caddy sends visitors on to
  HTTPS itself.

**2. Clone the code on the server** (as root):
```bash
git clone -b proof-indexer https://github.com/<owner>/<repo>.git /opt/orbio
```
If the repository is private, give the server a read-only deploy key first. Run
`ssh-keygen -t ed25519 -f ~/.ssh/orbio_deploy -N ""` and add `~/.ssh/orbio_deploy.pub` under GitHub → the repo →
Settings → Deploy keys. Then tell SSH to use it (GitHub accepts a deploy key for one repository only, so a server that
already clones another repo needs its own name for this one):
```bash
printf 'Host github-orbio\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/orbio_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
git clone -b proof-indexer github-orbio:<owner>/<repo>.git /opt/orbio
```

**3. Run the setup script** with your domain and timezone. It installs Python and Caddy, creates the `orbio` user,
installs the services (running on your timezone, for the daily digest), the Caddyfile and log rotation, and allows
ports 22, 80 and 443. It starts nothing, and it leaves the server's clock and anything else on it alone. If another
web server already holds port 443, it skips Caddy: serve `/opt/orbio/data/site` from that one instead.
```bash
bash /opt/orbio/deploy/setup.sh dossier.example.com Asia/Singapore
```

**4. Optional: a Cloudflare Origin certificate instead of Let's Encrypt.** It lasts 15 years and needs no renewals, so
Always Use HTTPS may be on. In Cloudflare: SSL/TLS → Origin Server → Create Certificate (the defaults). Paste the
certificate into `/etc/caddy/cf-origin.pem` and the private key into `/etc/caddy/cf-origin-key.pem`, then:
```bash
chown caddy:caddy /etc/caddy/cf-origin*.pem && chmod 600 /etc/caddy/cf-origin*.pem
bash /opt/orbio/deploy/setup.sh dossier.example.com Asia/Singapore   # again: now it uses the origin certificate
```

**5. Move the state from Windows.** On the Windows machine, in the repo folder:
```powershell
powershell -ExecutionPolicy Bypass -File deploy\push_state.ps1 -Server root@<server-ip>
```
Add `-Key <file>` when the server's SSH key isn't your default one. The script stops and disables the `orbio-watch`
and `orbio-proof` tasks first (two copies would poll X twice and post to Telegram twice), then copies `data\` and
`.env` across. The site folder, logs and heartbeats aren't copied: the site is rebuilt on the server.

**6. Check `.env`, then start.** `/opt/orbio/.env` on the server should have these lines. Add them if the Windows
copy didn't:
```
DOSSIER_SITE_URL=https://dossier.example.com
PROOF_DAILY_BUDGET=3
```
- `DOSSIER_SITE_URL` gives every page its link preview and canonical address, adds the sitemap, and links public
  channel posts and bot replies to the token's file.
- `PROOF_DAILY_BUDGET` is the indexer's CREDIT per day. At 1, it ran out at 14:32 Singapore time on 30 Sept, and new
  agents then waited until midnight UTC for their first check.

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
journalctl -u caddy --no-pager | grep -i "certificate obtained"   # Let's Encrypt: within a minute or two
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
  [Cloudflare's IP ranges](https://www.cloudflare.com/ips/). Let's Encrypt's check comes through Cloudflare, so it
  still works.
- **Cloudflare Pages instead of Caddy:** the indexer can push the site to Pages (`DOSSIER_CF_PROJECT`, README). It
  needs Node.js and deploys on every page change plus every 10 minutes for prices, about 5,000 deploys a month.
  Cloudflare's docs don't say whether that counts toward the free plan's 500 builds a month, so serving from the
  server is the safer choice.

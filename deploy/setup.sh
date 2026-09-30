#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 server for orbio-watch, the Proof indexer, the bot and the Dossier site.
# Run as root from the checkout (deploy/HETZNER.md has the whole sequence):
#   bash deploy/setup.sh dossier.example.com Asia/Singapore
# Safe to run again: it only (re)installs packages, units and the Caddyfile. It starts nothing.
set -euo pipefail
DOMAIN=${1:?usage: setup.sh <domain> <timezone, e.g. Asia/Singapore>}
TZONE=${2:?usage: setup.sh <domain> <timezone, e.g. Asia/Singapore>}
DIR=$(cd "$(dirname "$0")/.." && pwd)

# the daily digest goes out at PROOF_DIGEST_HOUR local time, and Hetzner images start on UTC
timedatectl set-timezone "$TZONE"

apt-get update
apt-get install -y python3 git ufw curl gnupg debian-keyring debian-archive-keyring apt-transport-https
python3 -c 'import sys; assert sys.version_info >= (3, 11), "needs Python 3.11+"'
if ! command -v caddy >/dev/null; then  # Caddy's own repository (Ubuntu's package lags behind)
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update
    apt-get install -y caddy
fi

# everything runs as an unprivileged user that owns the checkout
id orbio >/dev/null 2>&1 || useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin orbio
mkdir -p "$DIR/data"
chown -R orbio:orbio "$DIR"
chmod +x "$DIR"/run_*.sh
# Caddy (user caddy) may pass through to data/site but can't list or open the rest of data/
chmod 711 "$DIR" "$DIR/data"
if [ -f "$DIR/.env" ]; then chmod 600 "$DIR/.env"; fi
# root pulls updates into a checkout the orbio user owns
git config --system --get-all safe.directory 2>/dev/null | grep -qxF "$DIR" || git config --system --add safe.directory "$DIR"

# the runners append to data/*.log forever: keep eight weeks (copytruncate, since they hold the files open)
cat > /etc/logrotate.d/orbio <<EOF
$DIR/data/*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
    su orbio orbio
}
EOF

for s in orbio-watch orbio-proof orbio-bot; do
    sed "s#/opt/orbio#$DIR#g" "$DIR/deploy/$s.service" > "/etc/systemd/system/$s.service"
done
systemctl daemon-reload

sed -e "s#dossier.example.com#$DOMAIN#g" -e "s#/opt/orbio#$DIR#g" "$DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile

# SSH and the web; the bot and the indexer only make outgoing connections
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo
echo "Installed. Next (deploy/HETZNER.md):"
echo "  - Cloudflare origin certificate: /etc/caddy/cf-origin.pem and /etc/caddy/cf-origin-key.pem"
echo "  - state from the old machine: deploy/push_state.ps1 (Windows)"
echo "  - then: systemctl enable --now orbio-watch orbio-proof && systemctl restart caddy"

#!/usr/bin/env bash
# Run on a fresh Ubuntu 24.04 EC2 instance:  sudo DOMAIN=<ip-with-dashes>.sslip.io bash setup_ec2.sh
# Installs the backend as a systemd service behind Caddy (automatic HTTPS via Let's Encrypt).
set -euo pipefail
DOMAIN="${DOMAIN:?set DOMAIN, e.g. 3-111-160-146.sslip.io}"
REPO="${REPO:-https://github.com/vaibhavgeek/orangeinvest.app.git}"

apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -yq python3 git caddy
id errand >/dev/null 2>&1 || useradd --system --home /var/lib/errand-agent --shell /usr/sbin/nologin errand
mkdir -p /var/lib/errand-agent && chown errand: /var/lib/errand-agent
if [[ -d /opt/errand-agent/.git ]]; then git -C /opt/errand-agent pull -q; else git clone -q "$REPO" /opt/errand-agent; fi

cp /opt/errand-agent/deploy/errand-agent.service /etc/systemd/system/
sed "s/{\$DOMAIN}/$DOMAIN/" /opt/errand-agent/deploy/Caddyfile > /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now errand-agent
systemctl restart errand-agent caddy
sleep 2
curl -fsS http://127.0.0.1:8000/api/health && echo "  backend OK -> https://$DOMAIN/api/health"

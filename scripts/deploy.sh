#!/usr/bin/env bash
# Deploy the cloud side: bridge service + nginx location. Idempotent.
# Requires: HERMES_BRIDGE_TOKEN in ~/.hermes/.env (600), sudo.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${HERMES_PYTHON:-$(command -v python3)}"

install -m 600 -d "$HOME/.hermes/bridge"
systemctl --version >/dev/null

sudo tee /etc/systemd/system/hermes-bridge.service >/dev/null <<EOF
[Unit]
Description=Hermes phone agent bridge
After=network.target

[Service]
EnvironmentFile=$HOME/.hermes/.env
Environment=HERMES_BRIDGE_HOME=$HOME/.hermes/bridge
ExecStart=$PY $ROOT/bridge/server.py
Restart=always
RestartSec=3
User=$USER

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now hermes-bridge
sudo systemctl --no-pager --lines=5 status hermes-bridge || true
curl -fsS http://127.0.0.1:8787/healthz && echo
echo "Add the nginx location from scripts/nginx-agent-bridge.conf, then: nginx -t && systemctl reload nginx"

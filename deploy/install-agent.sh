#!/bin/sh
# install-agent.sh - run ON a camera host (as a user with sudo).
# Installs the skysong agent + systemd unit; idempotent.
set -eu
SRC=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p ~/skysong/agent
cp "$SRC/agent/skysong-agent.py" ~/skysong/agent/
[ -f ~/skysong/agent/cameras.json ] || cp "$SRC/agent/cameras.json" ~/skysong/agent/
sudo usermod -aG video "$USER" || true
sudo cp "$SRC/deploy/skysong-agent.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now skysong-agent
systemctl --no-pager status skysong-agent | head -5

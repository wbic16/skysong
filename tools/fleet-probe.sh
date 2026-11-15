#!/bin/sh
# fleet-probe.sh - find which tailnet nodes have a camera attached.
#
#   fleet-probe.sh name=ip [name=ip ...]
#
# SSHes to each node (via the tailnet proxy recipe used on the Muse VM;
# override with SSH_PROXY_CMD="") and lists its V4L2 devices.
# First run against the fleet, 2026-11-15: exactly one hit - ni-knight
# with a Logitech BRIO.
set -eu
KEY=${SSH_IDENTITY:-$HOME/.ssh/id_ed25519}
PROXY=${SSH_PROXY_CMD:-nc -X connect -x hatch-egress-proxy:3130 %h %p}
for pair in "$@"; do
  name=${pair%%=*}; ip=${pair##*=}
  out=$(timeout 25 ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes \
        -o ConnectTimeout=12 -o StrictHostKeyChecking=accept-new \
        -o "ProxyCommand=$PROXY" "wbic16@$ip" \
        'ls /dev/video* 2>/dev/null; for d in /sys/class/video4linux/video*; do cat $d/name 2>/dev/null; done' \
        2>/dev/null | sort -u | tr '\n' ' ')
  echo "$name ($ip): ${out:-no camera / unreachable}"
done

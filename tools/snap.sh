#!/bin/sh
# snap.sh - capture one still from a V4L2 camera, with exposure warm-up.
#
#   snap.sh [device] [output.jpg] [resolution] [skip-frames]
#
# Defaults: /dev/video0, ./snap-<timestamp>.jpg, 3840x2160, 200 skip frames.
# The skip matters: a cold auto-exposure/AGC needs seconds to settle.
# On the Logitech BRIO prototype, 30 skipped frames produced an unreadable
# near-black frame; 200 skipped frames (~10 s) produced a readable one.
# Requires: fswebcam. Reading /dev/video* usually needs the 'video' group
# or sudo.
set -eu
DEV=${1:-/dev/video0}
OUT=${2:-"snap-$(date +%Y%m%d-%H%M%S).jpg"}
RES=${3:-3840x2160}
SKIP=${4:-200}
fswebcam -d "$DEV" -r "$RES" --skip "$SKIP" --jpeg 92 --no-banner "$OUT"
echo "wrote $OUT"

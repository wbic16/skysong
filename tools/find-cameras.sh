#!/bin/sh
# find-cameras.sh - list V4L2 cameras attached to THIS machine.
# Prints each /dev/videoN with its driver name and stable by-id path.
set -eu
found=0
for dev in /dev/video*; do
  [ -e "$dev" ] || continue
  found=1
  base=$(basename "$dev")
  name=$(cat "/sys/class/video4linux/$base/name" 2>/dev/null || echo "?")
  echo "$dev  $name"
done
if [ -d /dev/v4l/by-id ]; then
  echo "-- by-id --"
  ls -l /dev/v4l/by-id | grep -v '^total' || true
fi
[ "$found" = 1 ] || echo "no /dev/video* devices found"

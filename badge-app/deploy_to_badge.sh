#!/usr/bin/env bash
# Deploy the "runlog" app to a Badger that is in DISK MODE.
#
# Usage:
#   1. Plug the badge into the Mac.
#   2. Double-tap the RESET button so the "BADGER" USB volume appears.
#   3. Run:  bash badge-app/deploy_to_badge.sh
#
# This copies ONLY the app files (no new badge secrets are required for the
# multi-week / pagination update). Your WiFi + DASHBOARD_URL in secrets.py are
# left untouched.
set -euo pipefail

VOL="/Volumes/BADGER"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/runlog" && pwd)"
DST="$VOL/apps/runlog"

if [ ! -d "$VOL" ]; then
  echo "ERROR: $VOL is not mounted."
  echo "Double-tap the RESET button on the badge, wait for the BADGER drive, then re-run."
  exit 1
fi

echo "Deploying runlog from: $SRC"
mkdir -p "$DST"

# Copy only the files the app needs (skip backups, caches, sidecars).
for f in __init__.py nightmode.py icon.png; do
  if [ -f "$SRC/$f" ]; then
    cp "$SRC/$f" "$DST/$f"
    echo "  copied $f"
  fi
done

# Strip macOS sidecar files that can confuse the badge's FAT filesystem.
find "$DST" -name '._*' -delete 2>/dev/null || true
find "$DST" -name '.DS_Store' -delete 2>/dev/null || true
find "$VOL" -maxdepth 1 -name '._*' -delete 2>/dev/null || true
command -v dot_clean >/dev/null 2>&1 && dot_clean -m "$VOL" || true

sync
echo "Ejecting BADGER..."
diskutil eject BADGER >/dev/null 2>&1 || diskutil eject "$VOL" >/dev/null 2>&1 || true
echo "Done. Unplug/replug or let it reboot; open the 'runlog' app."

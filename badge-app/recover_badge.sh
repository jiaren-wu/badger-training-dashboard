#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Badge boot-failure recovery  (run this if the badge won't boot / blank screen)
# ---------------------------------------------------------------------------
# Root cause (two bugs, both fixed here):
#   BOOT CRASH  - a dead-weight large font (absolute.ppf, 9 KB) was loaded at
#                 boot but never rendered; on the fragmented boot heap that lone
#                 allocation MemoryError'd -> blank REPL. Removed (aliased small).
#   FETCH ENOMEM- the ~93 KB runlog module was COMPILED from source at every
#                 import. That compile fragments the heap so badly that, even
#                 with 213 KB free, no contiguous ~16-32 KB block remains for the
#                 TLS handshake -> every fetch fails ("Offline - retry"). Fix:
#                 ship runlog as PRECOMPILED .mpy bytecode (~29 KB, no runtime
#                 compile) so the heap stays contiguous and TLS fits. Verified
#                 working on hardware.
#
# This script installs three hardened pieces:
#   1. main.py        - dual-build boot script. Tries the live runlog build,
#                       falls back to a known-good stable build, then to the
#                       launcher menu. It gc.collect()s before the import and
#                       prints "boot: free heap ..." to the serial console.
#   2. apps/runlog        - per-person charts as PRECOMPILED __init__.mpy (the
#                           fetch fix). Falls back cleanly if the .mpy ABI ever
#                           mismatches the firmware (ImportError -> stable).
#   3. apps/runlog_stable - git HEAD build (source), dead font removed, as an
#                           always-boots safety net.
#
# Usage:
#   1. Plug the badge into the Mac.
#   2. Double-tap RESET so the "BADGER" USB drive appears (disk mode).
#   3. Run:  bash badge-app/recover_badge.sh
#      (You can also start it first; it waits up to 90s for the drive.)
# ---------------------------------------------------------------------------
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
VOL="/Volumes/BADGER"
PY="$(command -v python3 || true)"

echo "==> Preparing builds..."
# Live (per-person) build + boot script come straight from the repo.
LIVE_INIT="$HERE/runlog/__init__.py"
MAIN_SRC="$HERE/system-main.py"
NIGHT="$HERE/runlog/nightmode.py"
ICON="$HERE/runlog/icon.png"
for f in "$LIVE_INIT" "$MAIN_SRC" "$NIGHT" "$ICON"; do
  [ -f "$f" ] || { echo "ERROR: missing $f"; exit 1; }
done

# Stable fallback build = git HEAD runlog, with the dead-weight large font
# dropped (aliased to small_font). HEAD loaded a ~9 KB font that this fix has
# shown is what starves the boot heap and the TLS fetch; not loading it makes
# the fallback lean enough to boot AND fetch. HEAD's one large_font render site
# just draws slightly smaller.
STABLE_FIXED="$(mktemp -t runlog_stable_fixed).py"
git -C "$REPO" show HEAD:badge-app/runlog/__init__.py > "$STABLE_FIXED.head"
"$PY" - "$STABLE_FIXED.head" "$STABLE_FIXED" <<'PY'
import sys
src = open(sys.argv[1]).read()
old = ('small_font = PixelFont.load("/system/assets/fonts/ark.ppf")\n'
       'large_font = PixelFont.load("/system/assets/fonts/absolute.ppf")\n')
new = ('small_font = PixelFont.load("/system/assets/fonts/ark.ppf")\n'
       '# Do NOT load the ~9 KB large font: it fragments the boot heap and\n'
       '# starves the TLS fetch (ENOMEM / "Offline - retry"). Alias to small.\n'
       'large_font = small_font\n')
if old not in src:
    sys.stderr.write("ERROR: could not find font lines in HEAD build\n"); sys.exit(1)
open(sys.argv[2], "w").write(src.replace(old, new, 1))
PY
"$PY" -m py_compile "$STABLE_FIXED" && echo "    stable fallback compiled OK ($(wc -c < "$STABLE_FIXED" | tr -d ' ') B)"
"$PY" -m py_compile "$LIVE_INIT"    && echo "    live per-person compiled OK ($(wc -c < "$LIVE_INIT" | tr -d ' ') B)"
"$PY" -m py_compile "$MAIN_SRC"     && echo "    boot script compiled OK"

# --- Precompile the live runlog module to .mpy (the fetch-ENOMEM fix) ---------
# Find a python that can run mpy_cross; if none, install it into ~/.badger-venv
# so future runs are fast. If it truly can't be had, fall back to source deploy.
find_mpycross_py() {
  for cand in /tmp/simvenv/bin/python "$HOME/.badger-venv/bin/python" "$($PY -c 'import sys;print(sys.executable)')"; do
    [ -x "$cand" ] || continue
    "$cand" -m mpy_cross --version >/dev/null 2>&1 && { echo "$cand"; return 0; }
  done
  local vpy="$HOME/.badger-venv/bin/python"
  [ -x "$vpy" ] || python3 -m venv "$HOME/.badger-venv" >/dev/null 2>&1 || true
  if [ -x "$vpy" ]; then
    "$vpy" -m pip install --quiet --disable-pip-version-check mpy-cross >/dev/null 2>&1 || true
    "$vpy" -m mpy_cross --version >/dev/null 2>&1 && { echo "$vpy"; return 0; }
  fi
  return 1
}
MPY_OUT="$(mktemp -t runlog).mpy"
USE_MPY=0
MPYCROSS_PY="$(find_mpycross_py || true)"
if [ -n "${MPYCROSS_PY:-}" ] && "$MPYCROSS_PY" -m mpy_cross -o "$MPY_OUT" "$LIVE_INIT" 2>/dev/null && [ -s "$MPY_OUT" ]; then
  USE_MPY=1
  echo "    precompiled runlog -> .mpy ($(wc -c < "$MPY_OUT" | tr -d ' ') B; $("$MPYCROSS_PY" -m mpy_cross --version 2>&1 | tail -1))"
else
  echo "    WARNING: mpy-cross unavailable -> deploying SOURCE runlog."
  echo "             It will boot but may hit fetch ENOMEM ('Offline - retry')."
  echo "             Install mpy-cross (pip install mpy-cross) and re-run to fix."
fi

echo "==> Waiting for BADGER disk mode (double-tap RESET)..."
for _ in $(seq 1 "${WAIT_ITERS:-180}"); do
  [ -d "$VOL" ] && break
  if [ -d /Volumes/RPI-RP2 ]; then
    echo "    NOTE: 'RPI-RP2' mounted -- that's the firmware BOOTSEL drive (wrong mode)."
    echo "    Don't hold any button. Just tap RESET TWICE quickly; look for 'BADGER'."
  fi
  sleep 0.5
done
if [ ! -d "$VOL" ]; then
  echo "ERROR: $VOL never appeared. Double-tap RESET and re-run."; exit 1
fi
echo "    mounted: $VOL"

RL="$VOL/apps/runlog"
ST="$VOL/apps/runlog_stable"
mkdir -p "$RL" "$ST"

copy_verify() { # src dst
  cp "$1" "$2"
  if cmp -s "$1" "$2"; then echo "    OK  $(basename "$2")  ($(wc -c < "$2" | tr -d ' ') B)"; else
    echo "    !! MISMATCH copying $2"; return 1; fi
}

echo "==> Deploying (with byte verification)..."
FAIL=0
copy_verify "$MAIN_SRC"     "$VOL/main.py"        || FAIL=1
if [ "$USE_MPY" -eq 1 ]; then
  # .mpy is primary: remove any stale source so the firmware loads the bytecode.
  rm -f "$RL/__init__.py"
  copy_verify "$MPY_OUT"    "$RL/__init__.mpy"    || FAIL=1
  [ -f "$RL/__init__.py" ] && { echo "    !! stale runlog/__init__.py still present"; FAIL=1; }
else
  rm -f "$RL/__init__.mpy"
  copy_verify "$LIVE_INIT"  "$RL/__init__.py"     || FAIL=1
fi
copy_verify "$NIGHT"        "$RL/nightmode.py"    || FAIL=1
copy_verify "$ICON"         "$RL/icon.png"        || FAIL=1
copy_verify "$STABLE_FIXED" "$ST/__init__.py"     || FAIL=1
copy_verify "$NIGHT"        "$ST/nightmode.py"    || FAIL=1
copy_verify "$ICON"         "$ST/icon.png"        || FAIL=1

echo "==> Stripping macOS sidecar files..."
find "$VOL" \( -name '._*' -o -name '.DS_Store' \) -type f -delete 2>/dev/null || true
find "$VOL" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
command -v dot_clean >/dev/null 2>&1 && dot_clean -m "$VOL" || true

if [ "$FAIL" -ne 0 ]; then
  echo "!! One or more files failed byte-verification. NOT ejecting so you can retry."
  echo "   Re-run this script; do not unplug yet."
  exit 1
fi

echo "==> All files verified. Syncing + ejecting to reboot..."
sync
diskutil eject BADGER >/dev/null 2>&1 || diskutil eject "$VOL" >/dev/null 2>&1 || true

# Best-effort: capture the boot serial so we can confirm it boots (needs the
# pyserial helper in /tmp/simvenv; optional).
LOG="/tmp/badge_recover_boot.log"
if [ -x /tmp/simvenv/bin/python ] && [ -f /tmp/badge_boot_logger.py ]; then
  echo "==> Capturing boot serial for ~25s (watch for 'boot: free heap' and no MemoryError)..."
  sed -i '' 's/^run_secs = .*/run_secs = 25/' /tmp/badge_boot_logger.py 2>/dev/null || true
  /tmp/simvenv/bin/python /tmp/badge_boot_logger.py >/dev/null 2>&1 || true
  cp -f /tmp/badge_boot.log "$LOG" 2>/dev/null || true
  echo "---- boot log ($LOG) ----"; cat -v "$LOG" 2>/dev/null || true; echo "-------------------------"
else
  echo "    (serial capture helper not present; just watch the badge screen)"
fi

echo "Done. The badge should show the training dashboard (per-person charts) and,"
echo "within a few seconds, live mileage/weather data (status 'Live', not 'Offline')."
echo "If it shows the app menu instead, the .mpy ABI didn't match the firmware and"
echo "it fell back to source — send me /tmp/badge_recover_boot.log ('mpy = N' line)."

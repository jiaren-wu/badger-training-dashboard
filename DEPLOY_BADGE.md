# Deploy the "runlog" training app to your Badger

The app is at `home/badge/apps/runlog/` and has been verified in the simulator.
The badge's launcher **auto-discovers** any folder in `/system/apps/` that has an
`__init__.py`, so installing is basically "copy the folder + set your secrets".

You'll do this over **disk mode** (a writable USB drive), because the normal REPL
filesystem is restored from templates on every boot.

---

## What you need
- The Badger, a USB-C data cable, your Mac.
- The `home/badge/apps/runlog/` folder from this workspace.
- Your WiFi name + password.
- (Optional) your `DASHBOARD_URL` from the backend (`backend/README.md`). Without
  it the app still shows live weather / air quality / next-hour outlook and
  *demo* mileage.

---

## Step 1 — Enter disk mode
1. Plug the badge into your Mac.
2. **Double-tap the RESET button** (quick double-press). A USB volume named
   **`BADGER`** appears in Finder (like a small thumb drive).
   - If it doesn't appear, try double-tapping a little faster, or use a different
     cable/port (must be a *data* cable, not charge-only).

## Step 2 — Copy the app

**Quick way (recommended):** once the `BADGER` volume is mounted, just run the
bundled script — it copies only the app files, strips macOS sidecar files, syncs,
and ejects:

```bash
bash badge-app/deploy_to_badge.sh
```

**Manual way:** copy the whole `runlog` folder into the badge's apps directory:

```bash
cp -R "badge-app/runlog" "/Volumes/BADGER/apps/runlog"
# strip macOS sidecar files that can confuse the badge's FAT filesystem
find "/Volumes/BADGER/apps/runlog" -name '._*' -delete
find "/Volumes/BADGER/apps/runlog" -name '.DS_Store' -delete
```

> Updating an already-installed badge? No new `secrets.py` values are needed for
> the multi-week / week-pagination update — the script leaves your WiFi and
> `DASHBOARD_URL` untouched. You can skip Step 3.

(That includes `__init__.py` and `icon.png` — the icon is what shows in the menu.
The badge's apps live at the **volume root** `/Volumes/BADGER/apps/`, which maps to
`/system/apps/` on the running device.)

## Step 3 — Set your secrets
Open the badge's secrets file and add your settings. In Terminal:

```bash
open -e "/Volumes/BADGER/secrets.py"
```

Make sure these lines exist (edit the WiFi values to your own):

```python
# --- WiFi (required for live data) ---
WIFI_SSID = "your-wifi-name"
WIFI_PASSWORD = "your-wifi-password"

# --- Training dashboard (optional) ---
# Public URL of dashboard.json from the backend (GitHub Pages / gist raw).
# Leave commented out to run in demo mode.
# DASHBOARD_URL = "https://jiaren-wu.github.io/badger-training-dashboard/dashboard.json"

# Distance units: "mi" or "km"
DIST_UNITS = "mi"

# --- Weather location (optional) ---
# If omitted, the badge auto-detects your city from your internet connection.
# To force a location, use ONE of these forms:
# WEATHER_LOCATION = {"lat": 47.7557, "lon": -122.3415, "name": "Shoreline"}
# WEATHER_LOCATION = (47.7557, -122.3415, "Shoreline")
```

Save and close the file.

> Tip: keep your existing `WIFI_SSID` / `WIFI_PASSWORD` if they're already set —
> just make sure they're your real network so the badge can reach the internet.

## Step 4 — Eject and reboot
1. Eject the **`BADGER`** volume in Finder (drag to trash / click the ⏏ icon).
2. Press **RESET** once. The badge boots normally.

## Step 5 — Open it
1. From the badge menu, scroll to the **runlog** icon (a small progress ring /
   road) and select it.
2. It connects to WiFi (~1–2s), then shows the **current week**:
   - the **current date** header (e.g. `Wed Aug 6`) + current temp / conditions,
   - an **air-quality** strip (city + US AQI + category),
   - a **1h** line — next-hour **rain %** and whether **AQI** is trending
     up / dn / flat,
   - **Ruby** (on top) and **Jiaren** each with `actual / planned mi` and a **%** bar,
   - a footer: **Live** (green) if it loaded your `DASHBOARD_URL`, else **Demo**,
     plus a small **C plan** hint.

### Buttons (A = left, B = middle, C = right)

| Button | Action |
| --- | --- |
| **C** (right) | Show the **workout detail** — today's workout (or the next one this week). Press **C** again to step forward day by day through Sunday. |
| **A** (left) | Step **back** to the previous workout. From the main screen it opens the last workout before today (nothing if today is Monday). |
| **B** (middle) | Jump back to the **current-day** screen. Press again there to **refresh** now. |
| **DOWN** | Page through **upcoming weeks** (4 per screen, planned mileage). |
| **UP** | Page through **past weeks** — actual **/** planned progress, newest first. |
| back / home | Exit the app. |

The workout screen shows both runners for the chosen day: planned distance +
title, and (for today or past days) the **actual** mileage done.

Data refreshes automatically every **15 min** while awake, and **hourly**
overnight while the screen is dark.

---

## Answers to "will it rain / will air quality get worse in the next hour?"
That's the **1h** line near the top:
- **Rain 0–100%** = Open-Meteo precipitation probability for the *next* hour
  (it also flips to a "Rain likely" style if probability ≥ 50% or ≥ 0.2 mm).
- **AQI ▲/▼/flat** = whether the US AQI *category* for the next hour is worse,
  better, or unchanged vs. now.

No API key or backend is needed for any of that — it comes straight from
Open-Meteo over your WiFi.

---

## Troubleshooting
- **App shows "Demo" footer** → `DASHBOARD_URL` isn't set or wasn't reachable.
  Check the URL in a browser; make sure the backend Action has published it.
- **"WiFi failed" / no weather** → double-check `WIFI_SSID` / `WIFI_PASSWORD`;
  2.4 GHz networks are the safest bet for these boards.
- **App not in the menu** → confirm the folder is exactly
  `/Volumes/BADGER/apps/runlog/` and contains `__init__.py`; re-enter disk mode to
  verify the copy landed.
- **Wrong city in weather** → set `WEATHER_LOCATION` explicitly (Step 3).
- **Changes didn't stick** → you must edit files on the **`BADGER`** disk-mode
  volume; edits over the plain REPL/`/` filesystem are wiped on reboot.

---

## (Optional) quick test without disk mode
You can also preview the app on your Mac in the simulator:

```bash
cd home
python3 -m venv .simvenv && source .simvenv/bin/activate
pip install pygame-ce pillow
python -m simulator.badge_simulator badge/apps/runlog
```

(`pygame-ce` ships prebuilt wheels; the classic `pygame` may fail to build on
newer Python. Press **F12** in the window to save a screenshot.)

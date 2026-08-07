import sys
import os

# On the badge these make imports + relative asset paths resolve. On the
# desktop simulator the path doesn't exist, so failing is fine.
try:
    sys.path.insert(0, "/system/apps/runlog")
    os.chdir("/system/apps/runlog")
except Exception:
    pass

from badgeware import io, brushes, shapes, screen, PixelFont, run

# Battery status is exposed as module-level badgeware functions
# (get_battery_level -> 0..100, is_charging -> bool). Import defensively so the
# app still runs on any firmware/simulator that doesn't provide them.
try:
    from badgeware import get_battery_level as _get_battery_level
except Exception:
    _get_battery_level = None
try:
    from badgeware import is_charging as _is_charging
except Exception:
    _is_charging = None

# `network` / MicroPython urlopen only exist on the badge. Guard them so the
# app also imports (and shows demo data) under the desktop simulator.
try:
    import network
except Exception:
    network = None
try:
    from urllib.urequest import urlopen
except Exception:
    try:
        from urllib.request import urlopen  # CPython / simulator
    except Exception:
        urlopen = None

# machine.reset() lets us reboot the badge for a clean heap overnight. Only
# present on real hardware (absent under the desktop simulator / CPython).
try:
    import machine
except Exception:
    machine = None

import json
import gc

from nightmode import NightMode

# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
small_font = PixelFont.load("/system/assets/fonts/ark.ppf")
large_font = PixelFont.load("/system/assets/fonts/absolute.ppf")

# ---------------------------------------------------------------------------
# Palette (GitHub dark)
# ---------------------------------------------------------------------------
white = brushes.color(235, 245, 255)
phosphor = brushes.color(211, 250, 55)
background = brushes.color(13, 17, 23)
black = brushes.color(0, 0, 0)
gray = brushes.color(110, 120, 130)
dim = brushes.color(70, 78, 88)
track = brushes.color(38, 44, 52)
blue = brushes.color(48, 148, 255)
green = brushes.color(63, 210, 110)
orange = brushes.color(255, 165, 0)
red = brushes.color(248, 81, 73)
purple = brushes.color(188, 140, 255)
cyan = brushes.color(56, 232, 225)

# ---------------------------------------------------------------------------
# Config (populated from /secrets.py)
# ---------------------------------------------------------------------------
WIFI_SSID = None
WIFI_PASSWORD = None
DASHBOARD_URL = None          # optional: URL returning dashboard.json
WEATHER_LOCATION = None       # optional: same formats as the weather app
DIST_UNITS = "mi"             # "mi" or "km"
WIFI_TIMEOUT = 60             # seconds to wait for association before giving up
WIFI_RECONNECT_MS = 12 * 1000 # re-issue connect() this often within the window
# Power saving: when True, the WiFi radio is powered down (disconnect +
# active(False)) between refreshes and only brought up for each fetch. This
# saves battery, BUT on this badge's WiFi chip a cold radio start can drop the
# first connect(), leaving the badge stuck retrying and never re-associating.
# So it's OFF by default -- the radio is brought up once at boot and stays
# associated (the known-good behaviour). Only opt in (WIFI_POWER_SAVE = True in
# /secrets.py) if you've confirmed reconnection is reliable on your network.
WIFI_POWER_SAVE = False

# Night mode: display goes dark between these hours (local time); any button
# wakes it for WAKE_SECONDS. Overridable from /secrets.py.
NIGHT_START_H = 23            # 11 PM
NIGHT_END_H = 6               # 6 AM
WAKE_SECONDS = 20
# Long-run memory hygiene: MicroPython's heap fragments over days of uptime,
# which can eventually fail a TLS handshake ("Offline - retry"). Once per night,
# while the screen is already dark, reboot after a long uptime to start from a
# clean heap. system-main.py auto-launches runlog on boot, so this returns
# straight to the dashboard with no interaction. Off via NIGHT_REBOOT = False.
NIGHT_REBOOT = True
NIGHT_REBOOT_MIN_UPTIME_MS = 12 * 60 * 60 * 1000   # only after >12h uptime

# Location
LATITUDE = None
LONGITUDE = None
LOCATION_NAME = "Detecting..."
COUNTRY_CODE = None
use_fahrenheit = True
location_detected = False

# Runtime state
wlan = None
connected = False
ticks_start = None
_wifi_connect_ts = None       # io.ticks when we last issued wlan.connect()
config_loaded = False

weather = None                # {'temp','code','condition'}
aqi = None                    # {'us_aqi','pm2_5','label','brush'}
dashboard = None              # parsed running data
running_live = False          # true when running numbers came from DASHBOARD_URL
weather_live = False          # true when weather came from the network
status = "Starting..."
last_update = None
auto_refresh = True
AUTO_REFRESH_MS = 15 * 60 * 1000   # 15 minutes (awake)
NIGHT_REFRESH_MS = 60 * 60 * 1000  # 1 hour (slower cadence while asleep)
RETRY_REFRESH_MS = 60 * 1000       # 60s: retry quickly while not live (self-heal)

# Night mode controller (local clock from network time + io.ticks).
night = NightMode(NIGHT_START_H, NIGHT_END_H, WAKE_SECONDS * 1000)
_display_on = True
_forced_hhmm = None           # simulator/testing override, e.g. "23:30"
_forced_page = None           # simulator/testing override, e.g. 1 or 2

# Refresh state machine. Each update() frame performs at most ONE blocking
# network call so the main loop keeps ticking (and never stalls long enough to
# trip the hardware watchdog / bounce back to the launcher).
refresh_queue = None          # list of remaining step names, or None when idle
loading = False

# Multi-week pagination / navigation state defined below.
# Navigation. Two views:
#   * "week"    : page 0 = current week (detailed). page > 0 = upcoming weeks
#                 (planned, DOWN). page < 0 = past weeks (progress, UP).
#   * "workout" : one day's detailed plan; LEFT/RIGHT (A/C) step between the
#                 week's workouts, seeded at today.
# Buttons: A=LEFT (prev workout), B=MIDDLE (home/current; refresh if already
# home), C=RIGHT (next workout), UP=past weeks, DOWN=upcoming weeks.
LOOKAHEAD_PER_PAGE = 4
view = "week"
page = 0
wk_idx = 0
today_iso = None              # "YYYY-MM-DD" from the network clock, when known
_forced_date = None           # simulator/testing override, e.g. "2026-08-05"
_forced_view = None           # simulator/testing override: "week" | "workout"
_forced_wk = None             # simulator/testing override: workout day index

# ---------------------------------------------------------------------------
# Demo data so the dashboard renders even with no WiFi / no backend yet.
# Multi-week shape mirrors the backend: names[i] lines up with each week's
# planned[i]/actual[i]; weeks[0] is the current week, the rest are upcoming.
# ---------------------------------------------------------------------------
# Ruby and Jiaren follow the SAME Final Surge Level 1 plan, so every day's
# planned distance + title is identical across both runners and only the Garmin
# "done" actuals differ. The demo mirrors that shape so the offline/no-WiFi view
# exercises the same shared-plan layout as live data (one plan column + each
# runner's progress), not the per-person fallback.
DEMO_DASHBOARD = {
    "week_start": "2025-08-04",
    "units": "mi",
    "today": "2025-08-06",
    "names": ["Ruby", "Jiaren"],
    "weeks": [
        {"start": "2025-08-04", "planned": [19.0, 19.0], "actual": [9.0, 6.8]},
        {"start": "2025-08-11", "planned": [19.0, 19.0], "actual": [0.0, 0.0]},
        {"start": "2025-08-18", "planned": [20.0, 20.0], "actual": [0.0, 0.0]},
        {"start": "2025-08-25", "planned": [21.0, 21.0], "actual": [0.0, 0.0]},
        {"start": "2025-09-01", "planned": [19.0, 19.0], "actual": [0.0, 0.0]},
        {"start": "2025-09-08", "planned": [22.0, 22.0], "actual": [0.0, 0.0]},
        {"start": "2025-09-15", "planned": [20.0, 20.0], "actual": [0.0, 0.0]},
        {"start": "2025-09-22", "planned": [23.0, 23.0], "actual": [0.0, 0.0]},
        {"start": "2025-09-29", "planned": [19.0, 19.0], "actual": [0.0, 0.0]},
    ],
    "past": [
        {"start": "2025-07-07", "planned": [18.0, 18.0], "actual": [17.5, 18.2]},
        {"start": "2025-07-14", "planned": [19.0, 19.0], "actual": [19.0, 16.8]},
        {"start": "2025-07-21", "planned": [20.0, 20.0], "actual": [18.4, 20.5]},
        {"start": "2025-07-28", "planned": [19.0, 19.0], "actual": [19.0, 19.1]},
    ],
    # One shared plan per day (Mon Solo #1, Wed Group Workout, Thu Solo #2,
    # Sat Group Long Run; Tue/Fri/Sun rest) totalling 19 mi/week. Prev week is
    # fully logged, the current week is logged through Wed (today), next week is
    # still to come (done=None).
    "days": [
        {"date": "2025-07-28", "dow": "Mon", "workouts": [
            {"dist": 4.0, "title": "Solo Run #1", "done": 4.0},
            {"dist": 4.0, "title": "Solo Run #1", "done": 4.1}]},
        {"date": "2025-07-29", "dow": "Tue", "workouts": [
            {"dist": 0.0, "title": "Rest", "done": None},
            {"dist": 0.0, "title": "Rest", "done": None}]},
        {"date": "2025-07-30", "dow": "Wed", "workouts": [
            {"dist": 5.0, "title": "Group Workout", "done": 5.0,
             "wtype": "Intervals", "l1": "Minutes 4-3-2-1, 4 x 100m strides",
             "l2": "Minutes 5-4-3-2-1, 4 x 100m strides"},
            {"dist": 5.0, "title": "Group Workout", "done": 4.7,
             "wtype": "Intervals", "l1": "Minutes 4-3-2-1, 4 x 100m strides",
             "l2": "Minutes 5-4-3-2-1, 4 x 100m strides"}]},
        {"date": "2025-07-31", "dow": "Thu", "workouts": [
            {"dist": 4.0, "title": "Solo Run #2", "done": 4.0},
            {"dist": 4.0, "title": "Solo Run #2", "done": 4.0}]},
        {"date": "2025-08-01", "dow": "Fri", "workouts": [
            {"dist": 0.0, "title": "Rest", "done": None},
            {"dist": 0.0, "title": "Rest", "done": None}]},
        {"date": "2025-08-02", "dow": "Sat", "workouts": [
            {"dist": 6.0, "title": "Group Long Run", "done": 6.0},
            {"dist": 6.0, "title": "Group Long Run", "done": 6.3}]},
        {"date": "2025-08-03", "dow": "Sun", "workouts": [
            {"dist": 0.0, "title": "Rest", "done": None},
            {"dist": 0.0, "title": "Rest", "done": None}]},
        {"date": "2025-08-04", "dow": "Mon", "workouts": [
            {"dist": 4.0, "title": "Solo Run #1", "done": 4.0},
            {"dist": 4.0, "title": "Solo Run #1", "done": 3.8}]},
        {"date": "2025-08-05", "dow": "Tue", "workouts": [
            {"dist": 0.0, "title": "Rest", "done": None},
            {"dist": 0.0, "title": "Rest", "done": None}]},
        {"date": "2025-08-06", "dow": "Wed", "workouts": [
            {"dist": 5.0, "title": "Group Workout", "done": 5.0,
             "wtype": "Hills", "l1": "4 x 1K Loops", "l2": "5 x 1K Loops"},
            {"dist": 5.0, "title": "Group Workout", "done": 3.0,
             "wtype": "Hills", "l1": "4 x 1K Loops", "l2": "5 x 1K Loops"}]},
        {"date": "2025-08-07", "dow": "Thu", "workouts": [
            {"dist": 4.0, "title": "Solo Run #2", "done": None},
            {"dist": 4.0, "title": "Solo Run #2", "done": None}]},
        {"date": "2025-08-08", "dow": "Fri", "workouts": [
            {"dist": 0.0, "title": "Rest", "done": None},
            {"dist": 0.0, "title": "Rest", "done": None}]},
        {"date": "2025-08-09", "dow": "Sat", "workouts": [
            {"dist": 6.0, "title": "Group Long Run", "done": None},
            {"dist": 6.0, "title": "Group Long Run", "done": None}]},
        {"date": "2025-08-10", "dow": "Sun", "workouts": [
            {"dist": 0.0, "title": "Rest", "done": None},
            {"dist": 0.0, "title": "Rest", "done": None}]},
        {"date": "2025-08-11", "dow": "Mon", "workouts": [
            {"dist": 4.0, "title": "Solo Run #1", "done": None},
            {"dist": 4.0, "title": "Solo Run #1", "done": None}]},
        {"date": "2025-08-12", "dow": "Tue", "workouts": [
            {"dist": 0.0, "title": "Rest", "done": None},
            {"dist": 0.0, "title": "Rest", "done": None}]},
        {"date": "2025-08-13", "dow": "Wed", "workouts": [
            {"dist": 5.0, "title": "Group Workout", "done": None,
             "wtype": "Tempo", "l1": "4 x 1 mile @ threshold, 4 x 100m strides",
             "l2": "5 x 1 mile @ threshold, 4 x 100m strides"},
            {"dist": 5.0, "title": "Group Workout", "done": None,
             "wtype": "Tempo", "l1": "4 x 1 mile @ threshold, 4 x 100m strides",
             "l2": "5 x 1 mile @ threshold, 4 x 100m strides"}]},
        {"date": "2025-08-14", "dow": "Thu", "workouts": [
            {"dist": 4.0, "title": "Solo Run #2", "done": None},
            {"dist": 4.0, "title": "Solo Run #2", "done": None}]},
        {"date": "2025-08-15", "dow": "Fri", "workouts": [
            {"dist": 0.0, "title": "Rest", "done": None},
            {"dist": 0.0, "title": "Rest", "done": None}]},
        {"date": "2025-08-16", "dow": "Sat", "workouts": [
            {"dist": 6.0, "title": "Group Long Run", "done": None},
            {"dist": 6.0, "title": "Group Long Run", "done": None}]},
        {"date": "2025-08-17", "dow": "Sun", "workouts": [
            {"dist": 0.0, "title": "Rest", "done": None},
            {"dist": 0.0, "title": "Rest", "done": None}]},
    ],
    "people": [
        {"name": "Ruby", "planned": 19.0, "actual": 9.0},
        {"name": "Jiaren", "planned": 19.0, "actual": 6.8},
    ],
}
DEMO_WEATHER = {"temp": 72, "code": 1, "condition": "Mainly Clear",
                "rain_prob": 0, "rain_mm": 0.0}
DEMO_AQI = {"us_aqi": 42, "pm2_5": 9.4, "next": 48}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def load_config():
    global WIFI_SSID, WIFI_PASSWORD, DASHBOARD_URL, WEATHER_LOCATION
    global DIST_UNITS, config_loaded, _forced_hhmm, _forced_page
    global NIGHT_START_H, NIGHT_END_H, WAKE_SECONDS, night
    global WIFI_POWER_SAVE, NIGHT_REBOOT
    global _forced_date, _forced_view, _forced_wk, today_iso, view, wk_idx
    if config_loaded:
        return
    config_loaded = True
    try:
        sys.path.insert(0, "/")
        try:
            from secrets import WIFI_SSID as S, WIFI_PASSWORD as P
            WIFI_SSID, WIFI_PASSWORD = S, P
        except ImportError:
            pass
        try:
            from secrets import DASHBOARD_URL as D
            DASHBOARD_URL = D or None
        except ImportError:
            pass
        try:
            from secrets import WEATHER_LOCATION as WL
            WEATHER_LOCATION = WL
        except ImportError:
            pass
        try:
            from secrets import DIST_UNITS as U
            if U in ("mi", "km"):
                DIST_UNITS = U
        except ImportError:
            pass
        try:
            from secrets import NIGHT_START_H as NS
            NIGHT_START_H = int(NS)
        except Exception:
            pass
        try:
            from secrets import NIGHT_END_H as NE
            NIGHT_END_H = int(NE)
        except Exception:
            pass
        try:
            from secrets import WAKE_SECONDS as WS
            WAKE_SECONDS = int(WS)
        except Exception:
            pass
        try:
            from secrets import WIFI_POWER_SAVE as WPS
            WIFI_POWER_SAVE = bool(WPS)
        except Exception:
            pass
        try:
            from secrets import NIGHT_REBOOT as NR
            NIGHT_REBOOT = bool(NR)
        except Exception:
            pass
        night = NightMode(NIGHT_START_H, NIGHT_END_H, WAKE_SECONDS * 1000)
        sys.path.pop(0)
    except Exception as e:
        print("config load error:", e)

    # Optional testing override (used by the desktop simulator).
    try:
        _forced_hhmm = os.getenv("RUNLOG_FORCE_HHMM")
    except Exception:
        _forced_hhmm = None
    if _forced_hhmm:
        try:
            h, m = _forced_hhmm.split(":")
            night.sync_from_hm(int(h), int(m), io.ticks)
        except Exception:
            pass

    # Optional testing override to jump straight to a page (simulator only).
    try:
        fp = os.getenv("RUNLOG_FORCE_PAGE")
        if fp:
            _forced_page = int(fp)
    except Exception:
        _forced_page = None

    # Optional testing overrides for the date + workout navigation (simulator).
    try:
        _forced_date = os.getenv("RUNLOG_FORCE_DATE") or None
        if _forced_date:
            today_iso = _forced_date
    except Exception:
        _forced_date = None
    try:
        _forced_view = os.getenv("RUNLOG_FORCE_VIEW") or None
        if _forced_view:
            view = _forced_view
    except Exception:
        _forced_view = None
    try:
        fw = os.getenv("RUNLOG_FORCE_WK")
        if fw is not None and fw != "":
            _forced_wk = int(fw)
            wk_idx = _forced_wk
    except Exception:
        _forced_wk = None


# ---------------------------------------------------------------------------
# WiFi (non-blocking: polled across frames)
# ---------------------------------------------------------------------------
def wlan_start():
    global wlan, ticks_start, connected, _wifi_connect_ts
    if network is None or not WIFI_SSID:
        return False
    if wlan is None:
        wlan = network.WLAN(network.STA_IF)
    try:
        if not wlan.active():
            wlan.active(True)
            # Disable the CYW43's aggressive radio power-management unless the
            # user explicitly opted into WiFi power-save. The default PM mode
            # sleeps the radio between beacons and is a well-known cause of slow
            # or failed association ("keeps retrying / WiFi failed") on this
            # chip; PM_NONE trades a little idle current for a reliable connect.
            if not WIFI_POWER_SAVE:
                try:
                    pm_none = getattr(wlan, "PM_NONE", 0xa11140)
                    wlan.config(pm=pm_none)
                except Exception:
                    pass
    except Exception:
        pass
    # Already associated (and has an IP)?  Fast path.
    if wlan.isconnected():
        connected = True
        ticks_start = None
        return True
    connected = False
    now = io.ticks
    # Begin (or re-arm) an association attempt with a fresh timeout window.
    # ticks_start is reset to None at the start of every refresh cycle and after
    # each failure, so a dropped/again-in-range network is retried cleanly
    # instead of getting stuck on "WiFi failed" until a manual reboot.
    if ticks_start is None:
        ticks_start = now
        _wifi_connect_ts = None       # force an immediate connect() below
        try:
            wlan.disconnect()
        except Exception:
            pass
    # Issue connect() when the window opens, and RE-ISSUE it periodically if we
    # still haven't associated. A single connect() can be silently dropped (a
    # weak first beacon, a busy AP, or a not-quite-ready radio), which used to
    # leave the badge idle until the full 60s timeout and then show "WiFi
    # failed"; re-issuing every WIFI_RECONNECT_MS self-heals that within the
    # same window instead of waiting the whole minute.
    if _wifi_connect_ts is None or now - _wifi_connect_ts > WIFI_RECONNECT_MS:
        _wifi_connect_ts = now
        try:
            wlan.connect(WIFI_SSID, WIFI_PASSWORD)
            print("Connecting to WiFi...")
        except Exception as e:
            print("wifi connect error:", e)
    if wlan.isconnected():
        connected = True
        ticks_start = None
        return True
    if now - ticks_start > WIFI_TIMEOUT * 1000:
        ticks_start = None      # re-arm for a clean retry on the next cycle
        return False
    return None  # still trying


def wlan_stop():
    """Power the WiFi radio down between refreshes (energy saving).

    Called at the end of a refresh when WIFI_POWER_SAVE is on. wlan_start()
    brings the radio back up (active(True) + connect) at the next refresh, so
    the association is torn down and rebuilt each cycle instead of being held
    open 24/7. No-op / harmless under the simulator (network is None).
    """
    global connected, ticks_start, wlan, _wifi_connect_ts
    connected = False
    ticks_start = None
    _wifi_connect_ts = None
    if wlan is None:
        return
    try:
        wlan.disconnect()
    except Exception:
        pass
    try:
        wlan.active(False)
    except Exception:
        pass
    # Drop the object so the next wlan_start() creates a fresh WLAN() and does a
    # clean cold init -- more reliable than re-activating a powered-down radio.
    wlan = None


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def http_json(url):
    if urlopen is None:
        raise OSError("no network")
    response = urlopen(url, headers={"User-Agent": "GitHubBadge"})
    data = b""
    chunk = bytearray(512)
    while True:
        length = response.readinto(chunk)
        if length == 0:
            break
        data += chunk[:length]
    result = json.loads(data.decode("utf-8"))
    del response, data, chunk
    gc.collect()
    return result


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------
def _is_num(v):
    return isinstance(v, (int, float))


def resolve_location():
    global LATITUDE, LONGITUDE, LOCATION_NAME, COUNTRY_CODE
    global location_detected, use_fahrenheit
    if location_detected:
        return
    # explicit override
    if WEATHER_LOCATION is not None:
        try:
            wl = WEATHER_LOCATION
            if isinstance(wl, dict) and "lat" in wl and "lon" in wl:
                LATITUDE = wl["lat"]
                LONGITUDE = wl["lon"]
                LOCATION_NAME = wl.get("name", "Home")
                COUNTRY_CODE = wl.get("country", "US")
            elif isinstance(wl, (tuple, list)) and len(wl) >= 2 and _is_num(wl[0]):
                LATITUDE, LONGITUDE = wl[0], wl[1]
                LOCATION_NAME = wl[2] if len(wl) > 2 else "Home"
                COUNTRY_CODE = wl[3] if len(wl) > 3 else "US"
            if LATITUDE is not None:
                use_fahrenheit = (COUNTRY_CODE == "US")
                location_detected = True
                return
        except Exception as e:
            print("WEATHER_LOCATION parse error:", e)
    # IP detect
    try:
        r = http_json("https://ipapi.co/json/")
        LATITUDE = r["latitude"]
        LONGITUDE = r["longitude"]
        LOCATION_NAME = r["city"]
        COUNTRY_CODE = r.get("country_code", "US")
        use_fahrenheit = (COUNTRY_CODE == "US")
        location_detected = True
    except Exception as e:
        print("IP geolocation error:", e)
        # Default to Seattle 98115 (owner's home) rather than failing.
        LATITUDE, LONGITUDE = 47.6849, -122.2968
        LOCATION_NAME = "Seattle"
        COUNTRY_CODE = "US"
        use_fahrenheit = True
        location_detected = True


# ---------------------------------------------------------------------------
# Weather + Air quality (Open-Meteo, no key)
# ---------------------------------------------------------------------------
WMO = {
    0: "Clear", 1: "Mainly Clear", 2: "Partly Cloudy", 3: "Overcast",
    45: "Foggy", 48: "Foggy", 51: "Drizzle", 53: "Drizzle", 55: "Drizzle",
    61: "Light Rain", 63: "Rain", 65: "Heavy Rain",
    71: "Light Snow", 73: "Snow", 75: "Heavy Snow", 77: "Snow",
    80: "Showers", 81: "Showers", 82: "Heavy Showers",
    85: "Snow", 86: "Snow", 95: "Storm", 96: "Storm", 99: "Storm",
}


def fetch_weather():
    global weather, today_iso
    unit = "fahrenheit" if use_fahrenheit else "celsius"
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&current=temperature_2m,weather_code"
           "&hourly=precipitation_probability,precipitation&forecast_hours=2"
           "&temperature_unit=%s&timezone=auto"
           % (LATITUDE, LONGITUDE, unit))
    r = http_json(url)
    c = r["current"]
    code = c["weather_code"]
    # Sync the local clock from the network (timezone=auto -> local time).
    # Skip when a testing override is active so the simulator stays deterministic.
    try:
        if c.get("time") and not _forced_hhmm:
            night.sync_from_iso(c["time"], io.ticks)
    except Exception as e:
        print("time sync:", e)
    # Capture today's calendar date (local) so the header + workout screens know
    # which day it is. Skip when a testing override is active.
    try:
        if c.get("time") and not _forced_date:
            today_iso = c["time"][:10]
    except Exception as e:
        print("date sync:", e)
    prob = None
    mm = None
    try:
        h = r.get("hourly", {}) or {}
        pp = h.get("precipitation_probability") or []
        pr = h.get("precipitation") or []
        if pp:
            prob = pp[1] if len(pp) > 1 else pp[0]
        if pr:
            mm = pr[1] if len(pr) > 1 else pr[0]
    except Exception as e:
        print("precip parse:", e)
    weather = {
        "temp": int(round(c["temperature_2m"])),
        "code": code,
        "condition": WMO.get(code, "Unknown"),
        "rain_prob": prob,
        "rain_mm": mm,
    }


def fetch_aqi():
    global aqi
    url = ("https://air-quality-api.open-meteo.com/v1/air-quality?latitude=%s"
           "&longitude=%s&current=us_aqi,pm2_5&hourly=us_aqi&forecast_hours=2"
           "&timezone=auto" % (LATITUDE, LONGITUDE))
    r = http_json(url)
    c = r["current"]
    nxt = None
    try:
        hh = (r.get("hourly", {}) or {}).get("us_aqi") or []
        if hh:
            nxt = hh[1] if len(hh) > 1 else hh[0]
    except Exception as e:
        print("aqi hourly parse:", e)
    aqi = {"us_aqi": c.get("us_aqi"), "pm2_5": c.get("pm2_5"), "next": nxt}


AQI_LABELS = ("Good", "Moderate", "Unhealthy", "Unhealthy", "Hazard")
AQI_BRUSHES = (green, orange, red, red, purple)


def aqi_cat(value):
    """US AQI category index: 0 Good .. 4 Hazardous. -1 if unknown."""
    if value is None:
        return -1
    if value <= 50:
        return 0
    if value <= 100:
        return 1
    if value <= 150:
        return 2
    if value <= 200:
        return 3
    return 4


def aqi_style(value):
    """Return (label, brush) for a US AQI value."""
    i = aqi_cat(value)
    if i < 0:
        return "--", gray
    return AQI_LABELS[i], AQI_BRUSHES[i]


def rain_soon(w):
    """True if meaningful rain is expected in the next hour."""
    prob = w.get("rain_prob")
    mm = w.get("rain_mm")
    return (prob is not None and prob >= 50) or (mm is not None and mm >= 0.2)


# ---------------------------------------------------------------------------
# Running dashboard
# ---------------------------------------------------------------------------
def fetch_dashboard():
    global dashboard
    if not DASHBOARD_URL:
        return False
    dashboard = http_json(DASHBOARD_URL)
    return True


# ---------------------------------------------------------------------------
# Refresh state machine (one network step per frame)
# ---------------------------------------------------------------------------
def start_refresh():
    global refresh_queue, loading, running_live, weather_live, ticks_start
    # Fetch the dashboard first (right after WiFi) so its TLS handshake runs
    # while the ESP32 heap is freshest. Doing location/weather/aqi first can
    # fragment memory enough that the 4th HTTPS handshake (dashboard) fails,
    # which showed up as live weather but an "Offline - retry" dashboard.
    refresh_queue = ["wifi", "dashboard", "location", "weather", "aqi", "finish"]
    ticks_start = None    # re-arm a fresh WiFi association attempt each cycle
    loading = True
    running_live = False
    weather_live = False


def step_refresh():
    """Run one refresh step. At most one blocking network call per frame."""
    global refresh_queue, loading, running_live, weather_live
    global status, last_update, weather, aqi, dashboard
    if not refresh_queue:
        return
    step = refresh_queue[0]

    if step == "wifi":
        state = wlan_start()
        if state is None:
            status = "Connecting WiFi..."
            return  # keep polling next frame, don't advance
        # advance whether connected (True) or gave up (False)
    elif step == "location":
        if connected:
            try:
                resolve_location()
            except Exception as e:
                print("location error:", e)
    elif step == "weather":
        if connected:
            try:
                fetch_weather()
                weather_live = True
            except Exception as e:
                print("weather error:", e)
    elif step == "aqi":
        if connected:
            try:
                fetch_aqi()
            except Exception as e:
                print("aqi error:", e)
    elif step == "dashboard":
        if connected:
            try:
                gc.collect()  # free heap before the TLS handshake (ESP32)
                if fetch_dashboard():
                    running_live = True
            except Exception as e:
                print("dashboard error:", e)
    elif step == "finish":
        if weather is None:
            weather = dict(DEMO_WEATHER)
        if aqi is None:
            aqi = dict(DEMO_AQI)
        if dashboard is None:
            dashboard = dict(DEMO_DASHBOARD)
        if running_live:
            status = "Live"
        elif not WIFI_SSID or network is None:
            status = "Demo - no WiFi"
        elif not connected:
            status = "WiFi failed"
        elif not DASHBOARD_URL:
            status = "Demo - set URL"
        else:
            status = "Offline - retry"
        last_update = io.ticks
        loading = False
        if WIFI_POWER_SAVE:
            wlan_stop()          # radio off until the next refresh (energy save)
        gc.collect()

    refresh_queue.pop(0)
    if not refresh_queue:
        refresh_queue = None


# ---------------------------------------------------------------------------
# Display power (night mode).  The screen-backlight API isn't documented, so we
# probe a few likely calls and always fall back to a black frame + LEDs off.
# ---------------------------------------------------------------------------
def _try_backlight(level01):
    try:
        from badgeware import display as _disp
    except Exception:
        _disp = None
    targets = []
    if _disp is not None:
        targets.append(_disp)
    targets.append(screen)
    ok = False
    for obj in targets:
        for attr, is_float in (("set_backlight", True), ("backlight", True),
                               ("set_brightness", False), ("brightness", False)):
            fn = getattr(obj, attr, None)
            if callable(fn):
                try:
                    fn(level01 if is_float else int(level01 * 255))
                    ok = True
                except Exception:
                    continue
    return ok


def _leds_off():
    try:
        for k in (io.LED_TOP_LEFT, io.LED_TOP_RIGHT,
                  io.LED_BOTTOM_LEFT, io.LED_BOTTOM_RIGHT):
            io.led[k] = 0
    except Exception:
        pass


def display_power(on):
    global _display_on
    if on:
        if not _display_on:
            _try_backlight(1.0)
            _display_on = True
    else:
        if _display_on:
            _try_backlight(0.0)
            _leds_off()
            _display_on = False


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
def fmt_dist(v):
    try:
        return "%.1f" % float(v)
    except Exception:
        return "0.0"


def fmt_mi(v):
    """Compact whole-mile string for the multi-week tables.

    Keeps each cell narrow (e.g. "6/23" instead of "6.2/23.0") so the two
    person columns never collide on the 160px-wide screen.  The current-week
    page keeps full decimal precision via fmt_dist.
    """
    try:
        return "%d" % int(round(float(v)))
    except Exception:
        return "0"


def pct_brush(p):
    if p >= 100:
        return green
    if p >= 90:
        return green
    if p >= 60:
        return orange
    return red


# ---------------------------------------------------------------------------
# Shared-plan detection.  Ruby and Jiaren normally follow the *same* Final Surge
# plan at the same level, so their planned mileage is identical every day/week.
# When that's true the multi-person screens collapse the duplicated planned
# column into a single shared one ("both").  If a per-weekday level override is
# ever re-enabled (so the two plans diverge) these return None/False and the
# screens fall back to the original per-person columns automatically.
def _shared_value(vals):
    """Common value if every present entry is equal, else None."""
    seen = None
    have = False
    for v in vals:
        if v is None:
            continue
        try:
            f = round(float(v), 3)
        except Exception:
            return None
        if not have:
            seen = f
            have = True
        elif f != seen:
            return None
    return seen if have else None


def _weeks_share_plan(rows, ncol):
    """True when every row carries an identical planned value across all people."""
    if ncol < 2 or not rows:
        return False
    for wk in rows:
        pl = wk.get("planned", [])
        if len(pl) < ncol or _shared_value(pl[:ncol]) is None:
            return False
    return True


def _shared_workout(wos, ncol):
    """(dist, title) shared across all people for a day, else None."""
    key = None
    have = False
    for i in range(ncol):
        wo = wos[i] if i < len(wos) else {}
        try:
            d = round(float(wo.get("dist", 0) or 0), 3)
        except Exception:
            d = 0.0
        t = str(wo.get("title", "") or "")
        if not have:
            key = (d, t)
            have = True
        elif key != (d, t):
            return None
    return key if have else None


def _detail_of(wos, ncol):
    """(wtype, l1, l2) when a day is a quality group workout, else None.

    Group (Wednesday) workouts carry per-level prescriptions (l1/l2) from the
    backend; both runners share them, so the first one that has them wins.
    """
    for i in range(min(ncol, len(wos))):
        wo = wos[i] or {}
        l1 = wo.get("l1")
        l2 = wo.get("l2")
        if l1 or l2:
            return (str(wo.get("wtype", "") or ""),
                    str(l1 or ""), str(l2 or ""))
    return None


def battery_level():
    """Battery charge as 0-100 int, or None when the firmware doesn't expose it."""
    if _get_battery_level is None:
        return None
    try:
        v = _get_battery_level()
        if v is None:
            return None
        v = float(v)
        if 0.0 <= v <= 1.0:          # some builds report a 0-1 fraction
            v *= 100.0
        return max(0, min(100, int(round(v))))
    except Exception:
        return None


def battery_charging():
    """True when the badge is charging over USB, else False (best effort)."""
    if _is_charging is None:
        return False
    try:
        return bool(_is_charging())
    except Exception:
        return False


# Battery is shown as a single colored dot in the footer instead of a percentage:
#   charging -> blue, low -> red, mid -> orange, high -> green, full -> phosphor.
def batt_state():
    """Return 'charging'|'low'|'mid'|'high'|'full', or None when unknown."""
    if battery_charging():
        return "charging"
    lvl = battery_level()
    if lvl is None:
        return None
    if lvl < 20:
        return "low"
    if lvl < 55:
        return "mid"
    if lvl < 90:
        return "high"
    return "full"


BATT_DOT = {
    "charging": None,   # filled in after brushes exist (see BATT_DOT_INIT)
    "low": None,
    "mid": None,
    "high": None,
    "full": None,
}
BATT_DOT["charging"] = blue
BATT_DOT["low"] = red
BATT_DOT["mid"] = orange
BATT_DOT["high"] = green
BATT_DOT["full"] = cyan


def draw_footer_right(y, fallback=None):
    """Draw the battery dot + local clock, right-aligned at x=152 on row `y`.

    Returns the left-most x the block occupies so callers can keep other footer
    text from colliding with it. Either element is omitted when unavailable.
    """
    right = 152
    lx = right
    clock = night.hhmm(io.ticks) if night.has_time() else fallback
    if clock:
        screen.font = small_font
        screen.brush = dim
        cw, _ = screen.measure_text(clock)
        screen.text(clock, right - cw, y)
        lx = right - cw
    st = batt_state()
    if st:
        r = 2                       # 4px-wide dot reads cleanly on the 6px font row
        gap = 5 if clock else 0
        cx = lx - gap - r
        cy = y + 3
        screen.brush = BATT_DOT.get(st, gray)
        screen.draw(shapes.circle(cx, cy, r))
        lx = cx - r
    return lx


def draw_progress(x, y, w, h, pct):
    screen.brush = track
    screen.draw(shapes.rounded_rectangle(x, y, w, h, h // 2))
    fill = int(w * min(max(pct, 0), 100) / 100.0)
    if fill < h:
        fill = h if pct > 0 else 0
    if fill > 0:
        screen.brush = pct_brush(pct)
        screen.draw(shapes.rounded_rectangle(x, y, fill, h, h // 2))


def draw_person(person, y, units):
    name = str(person.get("name", "?"))
    planned = float(person.get("planned", 0) or 0)
    actual = float(person.get("actual", 0) or 0)
    pct = (actual / planned * 100.0) if planned > 0 else 0.0

    # name (left) + percent (right)
    screen.font = small_font
    screen.brush = phosphor
    screen.text(name, 8, y)

    pct_txt = "%d%%" % int(round(pct))
    screen.font = small_font
    screen.brush = pct_brush(pct)
    pw, _ = screen.measure_text(pct_txt)
    screen.text(pct_txt, 152 - pw, y)

    # mileage line
    miles = "%s / %s %s" % (fmt_dist(actual), fmt_dist(planned), units)
    screen.brush = white
    screen.text(miles, 8, y + 10)

    # progress bar
    draw_progress(8, y + 20, 144, 7, pct)


def draw_sleep():
    """Night mode: fully dark screen."""
    screen.brush = black
    screen.clear()


# ---------------------------------------------------------------------------
# Multi-week data access (works with the new names+weeks schema or the legacy
# single-week {people:[...]} shape).
# ---------------------------------------------------------------------------
_MON = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _fmt_md(iso):
    try:
        p = iso.split("-")
        return "%s %d" % (_MON[int(p[1])], int(p[2]))
    except Exception:
        return iso or "?"


def dashboard_weeks():
    """Return (names, weeks) from either the new or the legacy schema."""
    if not dashboard:
        return [], []
    names = dashboard.get("names")
    weeks = dashboard.get("weeks")
    if names and weeks:
        return names, weeks
    people = dashboard.get("people") or []
    names = [p.get("name", "?") for p in people]
    wk = {
        "start": dashboard.get("week_start", ""),
        "planned": [float(p.get("planned", 0) or 0) for p in people],
        "actual": [float(p.get("actual", 0) or 0) for p in people],
    }
    return names, [wk]


def max_page():
    """Highest page index available (0 = only the current week)."""
    _, weeks = dashboard_weeks()
    future = len(weeks) - 1
    if future <= 0:
        return 0
    return 1 + (future - 1) // LOOKAHEAD_PER_PAGE


def current_people(names, weeks):
    """Build current-week person dicts from the (names, weeks) pair."""
    people = []
    if weeks:
        w0 = weeks[0]
        pl = w0.get("planned", [])
        ac = w0.get("actual", [])
        for i, nm in enumerate(names):
            people.append({
                "name": nm,
                "planned": pl[i] if i < len(pl) else 0,
                "actual": ac[i] if i < len(ac) else 0,
            })
    return people


# ---------------------------------------------------------------------------
# Date helpers. MicroPython has no datetime module, so weekday/day-offset math
# is done with pure-integer civil-day counting (Howard Hinnant's algorithm).
# ---------------------------------------------------------------------------
_DOW_APP = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _parse_ymd(iso):
    p = iso.split("-")
    return int(p[0]), int(p[1]), int(p[2])


def _civil_days(y, m, d):
    y2 = y - (1 if m <= 2 else 0)
    era = (y2 if y2 >= 0 else y2 - 399) // 400
    yoe = y2 - era * 400
    mp = (m + 9) % 12
    doy = (153 * mp + 2) // 5 + (d - 1)
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _iso_days(iso):
    try:
        y, m, d = _parse_ymd(iso)
        return _civil_days(y, m, d)
    except Exception:
        return None


def _weekday_mon0(iso):
    n = _iso_days(iso)
    if n is None:
        return None
    return (n + 3) % 7            # 1970-01-01 was a Thursday -> index 3


def _today():
    """Best available 'today' as YYYY-MM-DD, or None."""
    if _forced_date:
        return _forced_date
    if today_iso:
        return today_iso
    if dashboard:
        return dashboard.get("today")
    return None


def _date_header():
    iso = _today()
    if not iso:
        return "TRAINING"
    try:
        _, m, d = _parse_ymd(iso)
        wd = _weekday_mon0(iso)
        dow = _DOW_APP[wd] if wd is not None else ""
        return ("%s %s %d" % (dow, _MON[m], d)).strip()
    except Exception:
        return "TRAINING"


def _fit(txt, maxw):
    """Truncate txt (with trailing '..') so it fits within maxw pixels."""
    try:
        if screen.measure_text(txt)[0] <= maxw:
            return txt
        while txt and screen.measure_text(txt + "..")[0] > maxw:
            txt = txt[:-1]
        return txt + ".."
    except Exception:
        return txt


def _wrap(txt, maxw, maxlines):
    """Word-wrap txt into <=maxlines lines, each fitting maxw px.

    The final line is ellipsized when text is dropped so nothing looks complete
    when it isn't. Uses measure_text with a coarse fallback for safety.
    """
    words = str(txt or "").split()
    if not words:
        return []

    def _fits(t):
        try:
            return screen.measure_text(t)[0] <= maxw
        except Exception:
            return len(t) * 6 <= maxw

    lines = []
    cur = ""
    i = 0
    while i < len(words):
        w = words[i]
        cand = w if not cur else (cur + " " + w)
        if _fits(cand):
            cur = cand
            i += 1
        elif cur:
            lines.append(cur)
            cur = ""
            if len(lines) >= maxlines:
                break
        else:                              # single word wider than the line
            lines.append(_fit(w, maxw))
            i += 1
            if len(lines) >= maxlines:
                break
    if cur and len(lines) < maxlines:
        lines.append(cur)
        i = len(words)
    if i < len(words) and lines:           # text left over -> mark truncation
        lines[-1] = _fit(lines[-1] + " ..", maxw)
    return lines


def dashboard_days():
    if dashboard:
        d = dashboard.get("days")
        if d:
            return d
    return []


def dashboard_past():
    if dashboard:
        p = dashboard.get("past")
        if p:
            return p
    return []


def today_index():
    """Index of today within the (multi-week) days list, or None if not present.

    The days list now spans past + current + upcoming weeks, so we locate today
    by matching its date rather than assuming it sits in the first seven entries.
    """
    days = dashboard_days()
    ti = _today()
    if not days or not ti:
        return None
    for i in range(len(days)):
        if days[i].get("date") == ti:
            return i
    return None


def past_pages():
    n = len(dashboard_past())
    if n <= 0:
        return 0
    return (n + LOOKAHEAD_PER_PAGE - 1) // LOOKAHEAD_PER_PAGE


def min_page():
    return -past_pages()


def _has_wk(idx):
    """True if day idx holds a real planned session.

    A workout counts when someone's planned distance is > 0, or (for a genuine
    planned session with no stated distance, e.g. a titled group workout) the
    backend marked it ``plan`` AND it carries a title to show. Falls back to
    distance-only on older data that lacks the flag.
    """
    days = dashboard_days()
    if idx < 0 or idx >= len(days):
        return False
    for w in days[idx].get("workouts", []):
        try:
            if float(w.get("dist", 0) or 0) > 0:
                return True
        except Exception:
            pass
        if w.get("plan") and (w.get("title") or "").strip():
            return True
    return False


def has_any_workout():
    """True if the current week has at least one real (non-rest) workout."""
    for i in range(len(dashboard_days())):
        if _has_wk(i):
            return True
    return False


def default_wk_idx():
    """Today's workout if today has one, else the next workout this week."""
    days = dashboard_days()
    if not days:
        return 0
    ti = today_index()
    if ti is None:
        ti = 0
    if _has_wk(ti):
        return ti
    for i in range(ti + 1, len(days)):
        if _has_wk(i):
            return i
    return ti


def next_wk_idx(cur):
    days = dashboard_days()
    for i in range(cur + 1, len(days)):
        if _has_wk(i):
            return i
    return None


def prev_wk_idx(cur):
    for i in range(cur - 1, -1, -1):
        if _has_wk(i):
            return i
    return None


def prev_from_today():
    """Last workout strictly before today (None if today is Monday / none)."""
    ti = today_index()
    if ti is None:
        ti = 0
    return prev_wk_idx(ti)


def jump_week(cur, direction):
    """Move ~one week (7 day-entries) forward/back in the flat days list.

    Lands on a day that has a workout when possible so UP/DOWN in the workout
    view flips between weeks quickly instead of stepping day by day.
    """
    days = dashboard_days()
    n = len(days)
    if n == 0:
        return cur
    target = cur + 7 * direction
    if target < 0:
        target = 0
    elif target > n - 1:
        target = n - 1
    if _has_wk(target):
        return target
    for off in range(1, 8):        # snap outward to the nearest real workout
        for t in (target + off, target - off):
            if 0 <= t < n and _has_wk(t):
                return t
    return target


def btn(name):
    """Safe button test that tolerates missing constants across hw/simulator."""
    try:
        b = getattr(io, name, None)
        return (b is not None) and (b in io.pressed)
    except Exception:
        return False


def draw():
    screen.brush = background
    screen.clear()

    units = "mi"
    week = ""
    names, weeks = dashboard_weeks()
    if dashboard:
        units = dashboard.get("units", DIST_UNITS)
        week = dashboard.get("week_start", "")
    people = current_people(names, weeks)

    # ---- header: current date (falls back to "TRAINING" until clock syncs) ----
    screen.font = small_font
    screen.brush = phosphor
    screen.text(_date_header(), 8, 3)

    # weather at top-right: "72F Clear"
    if weather:
        wt = "%d%s %s" % (weather["temp"], "F" if use_fahrenheit else "C",
                          weather["condition"])
        screen.brush = white
        ww, _ = screen.measure_text(wt)
        if ww > 96:
            wt = "%d%s" % (weather["temp"], "F" if use_fahrenheit else "C")
            ww, _ = screen.measure_text(wt)
        screen.text(wt, 152 - ww, 3)

    screen.brush = dim
    screen.draw(shapes.rectangle(8, 13, 144, 1))

    # ---- current air quality strip ----
    y = 17
    if aqi:
        label, brush = aqi_style(aqi.get("us_aqi"))
        val = aqi.get("us_aqi")
        left = LOCATION_NAME if location_detected else "Air"
        screen.font = small_font
        screen.brush = gray
        if len(left) > 12:
            left = left[:12]
        screen.text(left, 8, y)
        aq = "AQI %s %s" % ("--" if val is None else int(val), label)
        screen.brush = brush
        aw, _ = screen.measure_text(aq)
        screen.text(aq, 152 - aw, y)

    # ---- next-hour outlook: rain + AQI trend ----
    y2 = 27
    screen.font = small_font
    screen.brush = white
    screen.text("1h", 8, y2)
    if weather is not None:
        prob = weather.get("rain_prob")
        soon = rain_soon(weather)
        if prob is None and weather.get("rain_mm") is None:
            rain_txt = "Rain --"
        else:
            rain_txt = "Rain %d%%" % (0 if prob is None else int(prob))
        screen.brush = blue if soon else gray
        screen.text(rain_txt, 26, y2)
    if aqi is not None:
        nxt = aqi.get("next")
        if nxt is None:
            nx_txt = "AQI --"
            nx_brush = gray
        else:
            now_i = aqi_cat(aqi.get("us_aqi"))
            nx_i = aqi_cat(nxt)
            if nx_i > now_i and now_i >= 0:
                trend = "up"
            elif nx_i < now_i:
                trend = "dn"
            else:
                trend = "flat"
            nx_txt = "AQI %d %s" % (int(nxt), trend)
            _, nx_brush = aqi_style(nxt)
        screen.brush = nx_brush
        nw, _ = screen.measure_text(nx_txt)
        screen.text(nx_txt, 152 - nw, y2)

    # ---- people ----
    py = 40
    if people:
        for person in people[:2]:
            draw_person(person, py, units)
            py += 31
    else:
        screen.font = small_font
        screen.brush = gray
        screen.text("No data", 8, py)

    # ---- footer ----
    screen.font = small_font
    # footer-right first: battery dot + local clock (or the refresh hint until
    # the clock syncs).  Drawn first so the status text can dodge its left edge.
    rx = draw_footer_right(110, fallback="B refresh")
    if loading:
        screen.brush = blue
        screen.text("Updating...", 8, 110)
    else:
        if running_live:
            screen.brush = green
        elif status.startswith("Offline"):
            screen.brush = red
        else:
            screen.brush = orange
        screen.text(status, 8, 110)
        # C drills into the plan (today's/this-or-next week's workouts).  We no
        # longer hint UP/DOWN here so the status can't collide with it; only show
        # "C plan" and only when it fits left of the clock block.
        hint = "C plan" if has_any_workout() else ""
        if hint:
            sw, _ = screen.measure_text(status)
            hx = 8 + sw + 6
            hwid, _ = screen.measure_text(hint)
            if hx + hwid <= rx - 6:
                screen.brush = dim
                screen.text(hint, hx, 110)


# ---------------------------------------------------------------------------
# Upcoming-weeks page (planned mileage lookahead), shown when page > 0.
# ---------------------------------------------------------------------------
COL_R = (100, 152)   # right edges of the two person columns
# Shared-plan Past layout: plan shown once (gray) + each runner's actual.
PAST_ACT_R = (112, 152)   # right edges of the two actual columns
PAST_PLAN_R = 74          # right edge of the single shared plan column


def draw_lookahead(p):
    screen.brush = background
    screen.clear()

    names, weeks = dashboard_weeks()
    units = dashboard.get("units", DIST_UNITS) if dashboard else "mi"
    ncol = min(len(names), len(COL_R))

    # ---- header ----
    screen.font = small_font
    screen.brush = phosphor
    screen.text("UPCOMING", 8, 3)
    pos = "%d/%d" % (p + 1, max_page() + 1)
    screen.brush = dim
    pw, _ = screen.measure_text(pos)
    screen.text(pos, 152 - pw, 3)
    screen.draw(shapes.rectangle(8, 13, 144, 1))

    # ---- one row per upcoming week ----
    start_idx = 1 + (p - 1) * LOOKAHEAD_PER_PAGE
    rows = weeks[start_idx:start_idx + LOOKAHEAD_PER_PAGE]
    shared = _weeks_share_plan(rows, ncol)

    # ---- column headers ----
    screen.font = small_font
    screen.brush = gray
    screen.text("Week", 8, 17)
    if shared:
        # One shared plan column: Ruby and Jiaren are on the same plan.
        lbl = "Plan both"
        screen.brush = dim
        w, _ = screen.measure_text(lbl)
        screen.text(lbl, 152 - w, 17)
    else:
        for i in range(ncol):
            nm = str(names[i])
            if len(nm) > 7:
                nm = nm[:7]
            screen.brush = phosphor
            w, _ = screen.measure_text(nm)
            screen.text(nm, COL_R[i] - w, 17)

    ry = 30
    if not rows:
        screen.brush = gray
        screen.text("No upcoming weeks", 8, ry)
    for wk in rows:
        screen.font = small_font
        screen.brush = gray
        screen.text(_fmt_md(wk.get("start", "")), 8, ry)
        planned = wk.get("planned", [])
        if shared:
            txt = "%s %s" % (fmt_mi(_shared_value(planned[:ncol])), units)
            screen.brush = white
            w, _ = screen.measure_text(txt)
            screen.text(txt, 152 - w, ry)
        else:
            for i in range(ncol):
                v = planned[i] if i < len(planned) else 0
                txt = fmt_mi(v)
                screen.brush = white
                w, _ = screen.measure_text(txt)
                screen.text(txt, COL_R[i] - w, ry)
        ry += 15

    # ---- footer: nav hint + battery/clock ----
    screen.font = small_font
    rx = draw_footer_right(110)
    screen.brush = dim
    hint = "UP/DN wks"
    if 8 + screen.measure_text(hint)[0] <= rx - 6:
        screen.text(hint, 8, 110)



# ---------------------------------------------------------------------------
# Past-weeks page (actual vs planned progress), shown when page < 0 (UP).
# Weeks are listed newest-first so last week is at the top.
# ---------------------------------------------------------------------------
def draw_pastweeks(p):
    screen.brush = background
    screen.clear()

    names, _ = dashboard_weeks()
    past = dashboard_past()
    units = dashboard.get("units", DIST_UNITS) if dashboard else "mi"
    ncol = min(len(names), len(COL_R))

    # ---- header ----
    screen.font = small_font
    screen.brush = phosphor
    screen.text("PAST", 8, 3)
    pp = past_pages()
    pos = "%d/%d" % (-p, pp) if pp else "0/0"
    screen.brush = dim
    pw, _ = screen.measure_text(pos)
    screen.text(pos, 152 - pw, 3)
    screen.draw(shapes.rectangle(8, 13, 144, 1))

    # ---- one row per past week, newest-first ----
    rev = list(reversed(past))
    start = (-p - 1) * LOOKAHEAD_PER_PAGE
    rows = rev[start:start + LOOKAHEAD_PER_PAGE]
    shared = _weeks_share_plan(rows, ncol)

    # ---- column headers ----
    screen.font = small_font
    screen.brush = gray
    if shared:
        screen.text("Week", 8, 17)
        screen.brush = dim
        w, _ = screen.measure_text("Pl")
        screen.text("Pl", PAST_PLAN_R - w, 17)
        for i in range(ncol):
            nm = str(names[i])
            if len(nm) > 6:
                nm = nm[:6]
            screen.brush = phosphor
            w, _ = screen.measure_text(nm)
            screen.text(nm, PAST_ACT_R[i] - w, 17)
    else:
        screen.text("Act/Pl", 8, 17)
        for i in range(ncol):
            nm = str(names[i])
            if len(nm) > 7:
                nm = nm[:7]
            screen.brush = phosphor
            w, _ = screen.measure_text(nm)
            screen.text(nm, COL_R[i] - w, 17)

    ry = 30
    if not rows:
        screen.brush = gray
        screen.text("No past weeks", 8, ry)
    for wk in rows:
        screen.font = small_font
        screen.brush = gray
        screen.text(_fmt_md(wk.get("start", "")), 8, ry)
        planned = wk.get("planned", [])
        actual = wk.get("actual", [])
        if shared:
            pv = _shared_value(planned[:ncol]) or 0
            # shared plan (gray), shown once
            screen.brush = gray
            pt = fmt_mi(pv)
            w, _ = screen.measure_text(pt)
            screen.text(pt, PAST_PLAN_R - w, ry)
            # each runner's actual, colored by their % of the shared plan
            for i in range(ncol):
                av = actual[i] if i < len(actual) else 0
                try:
                    pct = (float(av) / float(pv) * 100.0) if float(pv) > 0 else 0.0
                    screen.brush = pct_brush(pct) if float(pv) > 0 else white
                except Exception:
                    screen.brush = white
                at = fmt_mi(av)
                w, _ = screen.measure_text(at)
                screen.text(at, PAST_ACT_R[i] - w, ry)
        else:
            for i in range(ncol):
                pv = planned[i] if i < len(planned) else 0
                av = actual[i] if i < len(actual) else 0
                txt = "%s/%s" % (fmt_mi(av), fmt_mi(pv))
                try:
                    pct = (float(av) / float(pv) * 100.0) if float(pv) > 0 else 0.0
                    screen.brush = pct_brush(pct) if float(pv) > 0 else white
                except Exception:
                    screen.brush = white
                w, _ = screen.measure_text(txt)
                screen.text(txt, COL_R[i] - w, ry)
        ry += 15

    # ---- footer ----
    screen.font = small_font
    rx = draw_footer_right(110)
    screen.brush = dim
    hint = "UP/DN wks"
    if 8 + screen.measure_text(hint)[0] <= rx - 6:
        screen.text(hint, 8, 110)


# ---------------------------------------------------------------------------
# Single-workout detail page. Stepped through with A (prev) / C (next),
# seeded at today. When both runners share the workout it's shown once with
# each runner's completion; if their plans differ it falls back to per-person.
# Wednesday "group workouts" are quality efforts described by reps rather than
# miles, so they get a dedicated Level 1 / Level 2 layout.
# ---------------------------------------------------------------------------
def _draw_level(label, spec, color, y):
    """Render an 'L1'/'L2' label plus its wrapped spec; return the next y."""
    screen.font = small_font
    screen.brush = color
    screen.text(label, 8, y)
    lines = _wrap(spec, 124, 2)          # spec column runs x=28..152
    if not lines:
        return y + 11
    screen.brush = white
    for ln in lines:
        screen.text(ln, 28, y)
        y += 10
    return y + 3


def _draw_group_detail(detail, shared, wos, names, nn, units):
    """Group workout: show the type + Level 1 / Level 2 plan (shared by both
    runners), then each runner's completion. No planned distance is shown."""
    wtype, l1, l2 = detail
    ttl = wtype or (shared[1] if shared else "") or "Group workout"
    screen.font = small_font
    screen.brush = phosphor
    screen.text(_fit(ttl, 144), 8, 18)
    y = 31
    if l1:
        y = _draw_level("L1", l1, cyan, y)
    if l2:
        y = _draw_level("L2", l2, orange, y)
    screen.brush = dim
    screen.draw(shapes.rectangle(8, y, 144, 1))
    y += 6
    for i in range(nn):
        wo = wos[i] if i < len(wos) else {}
        done = wo.get("done", None)
        screen.font = small_font
        screen.brush = phosphor
        screen.text(str(names[i]), 8, y)
        if done is not None:
            try:
                dtx = "done %s %s" % (fmt_dist(float(done)), units)
            except Exception:
                dtx = "done"
            screen.brush = green
        else:
            dtx = "--"
            screen.brush = gray
        w, _ = screen.measure_text(dtx)
        screen.text(dtx, 152 - w, y)
        y += 12


def draw_workout(idx):
    screen.brush = background
    screen.clear()

    days = dashboard_days()
    names, _ = dashboard_weeks()
    units = dashboard.get("units", DIST_UNITS) if dashboard else "mi"

    d = days[idx] if (0 <= idx < len(days)) else None

    # ---- header: day + date, plus a TODAY tag when applicable ----
    screen.font = small_font
    screen.brush = phosphor
    if d:
        date_iso = d.get("date", "")
        dow = d.get("dow")
        if not dow:
            wd = _weekday_mon0(date_iso)
            dow = _DOW_APP[wd] if wd is not None else ""
        hdr = ("%s %s" % (dow, _fmt_md(date_iso))).strip()
    else:
        hdr = "WORKOUT"
    screen.text(hdr, 8, 3)
    ti = today_index()
    if ti is not None and ti == idx:
        screen.brush = green
        tw, _ = screen.measure_text("TODAY")
        screen.text("TODAY", 152 - tw, 3)
    screen.brush = dim
    screen.draw(shapes.rectangle(8, 13, 144, 1))

    if not d:
        screen.font = small_font
        screen.brush = gray
        screen.text("No plan detail", 8, 44)
    else:
        wos = d.get("workouts", [])
        nn = min(len(names), 2)
        detail = _detail_of(wos, nn) if wos else None
        shared = _shared_workout(wos, nn) if wos else None
        if detail is not None:
            _draw_group_detail(detail, shared, wos, names, nn, units)
        elif shared is not None:
            # Ruby and Jiaren share this workout: show it ONCE, then each
            # runner's completion underneath (no duplicated plan line).
            dist, title = shared
            screen.font = small_font
            if dist > 0:
                screen.brush = phosphor
                screen.text("%s %s" % (fmt_dist(dist), units), 8, 22)
                if title:
                    screen.brush = white
                    screen.text(_fit(title, 144), 8, 34)
            else:
                # Rest / cross-train / info day: the title is the whole story.
                screen.brush = phosphor
                screen.text(_fit(title or "Rest", 144), 8, 22)
            screen.brush = dim
            screen.draw(shapes.rectangle(8, 48, 144, 1))
            # completion, one line per runner
            yy = 54
            for i in range(nn):
                wo = wos[i] if i < len(wos) else {}
                done = wo.get("done", None)
                screen.font = small_font
                screen.brush = phosphor
                screen.text(str(names[i]), 8, yy)
                if done is not None:
                    try:
                        dt = "done %s %s" % (fmt_dist(float(done)), units)
                    except Exception:
                        dt = "done"
                    screen.brush = green
                else:
                    dt = "--"
                    screen.brush = gray
                w, _ = screen.measure_text(dt)
                screen.text(dt, 152 - w, yy)
                yy += 14
        else:
            # Plans differ (per-weekday level override): keep the original
            # per-person blocks so each runner's distinct plan is visible.
            py = 22
            for i in range(nn):
                nm = str(names[i])
                wo = wos[i] if i < len(wos) else {}
                dist = float(wo.get("dist", 0) or 0)
                title = str(wo.get("title", "") or "")
                done = wo.get("done", None)
                screen.font = small_font
                screen.brush = phosphor
                screen.text(nm, 8, py)
                if dist > 0:
                    line = "%s %s  %s" % (fmt_dist(dist), units, title)
                else:
                    line = title or "Rest"
                screen.brush = white
                screen.text(_fit(line, 144), 8, py + 11)
                if done is not None:
                    try:
                        dtxt = "done %s %s" % (fmt_dist(float(done)), units)
                    except Exception:
                        dtxt = "done"
                    screen.brush = green
                    screen.text(dtxt, 8, py + 22)
                py += 44

    # ---- footer ----
    screen.font = small_font
    rx = draw_footer_right(110)
    screen.brush = dim
    # A/C step days (across week boundaries, incl. into other weeks); B home.
    legend = "A/C day"
    if 8 + screen.measure_text(legend)[0] <= rx - 6:
        screen.text(legend, 8, 110)
# ---------------------------------------------------------------------------
started = False


def _maybe_night_reboot():
    # Once per night, after a long uptime and only while the screen is already
    # dark and idle, reboot to defragment MicroPython's heap. system-main.py
    # auto-launches runlog on boot, so the badge wakes straight back into the
    # dashboard (a brief lit screen while it re-syncs the clock, then dark).
    # Only reached from inside the should_sleep branch, so the clock is known
    # and WiFi has been working -- never fires during the unsynced post-boot
    # window. After a reset io.ticks restarts near 0, so at most one per night.
    if not NIGHT_REBOOT or machine is None:
        return
    if refresh_queue is not None or loading:
        return
    if io.ticks < NIGHT_REBOOT_MIN_UPTIME_MS:
        return
    try:
        machine.reset()
    except Exception:
        pass


def _update_impl():
    global started, page, view, wk_idx
    if not started:
        started = True
        load_config()
        start_refresh()

    # advance the (non-blocking) refresh, one network step per frame
    if refresh_queue is not None:
        step_refresh()

    # any button press wakes the screen (and counts as activity)
    try:
        if io.pressed:
            night.wake(io.ticks)
    except Exception:
        pass

    # ---- navigation ----
    #   UP    = past weeks (progress)      DOWN = upcoming weeks (planned)
    #   A     = previous workout           C    = next workout (seeded at today)
    #   B     = home (current week); refresh when already home
    try:
        if btn("BUTTON_DOWN"):
            if view == "workout":
                wk_idx = jump_week(wk_idx, +1)   # next week's workout
            else:
                view = "week"
                page = min(page + 1, max_page())
        elif btn("BUTTON_UP"):
            if view == "workout":
                wk_idx = jump_week(wk_idx, -1)   # previous week's workout
            else:
                view = "week"
                page = max(page - 1, min_page())
        elif btn("BUTTON_C") or btn("BUTTON_RIGHT"):
            if view == "workout":
                nxt = next_wk_idx(wk_idx)
                if nxt is not None:
                    wk_idx = nxt
            elif has_any_workout():
                view = "workout"
                page = 0
                wk_idx = default_wk_idx()
        elif btn("BUTTON_A") or btn("BUTTON_LEFT"):
            if view == "workout":
                prv = prev_wk_idx(wk_idx)
                if prv is not None:
                    wk_idx = prv
            else:
                prv = prev_from_today()
                if prv is not None and dashboard_days():
                    view = "workout"
                    page = 0
                    wk_idx = prv
        elif btn("BUTTON_B"):
            if view == "week" and page == 0:
                if refresh_queue is None:
                    start_refresh()          # already home -> manual refresh
            else:
                view = "week"                # otherwise jump back to today
                page = 0
    except Exception:
        pass

    # periodic auto refresh. While asleep at night we still refresh, but only
    # once an hour (NIGHT_REFRESH_MS) instead of every AUTO_REFRESH_MS, to keep
    # radio/network use low overnight. The refresh steps run at the top of each
    # frame, so it still completes while the screen stays dark.
    _interval = NIGHT_REFRESH_MS if night.should_sleep(io.ticks) else AUTO_REFRESH_MS
    if not running_live and not night.should_sleep(io.ticks):
        _interval = RETRY_REFRESH_MS   # not live yet -> retry quickly to self-heal
    if (auto_refresh and refresh_queue is None and last_update is not None
            and io.ticks - last_update > _interval):
        start_refresh()

    # night mode: dark screen between NIGHT_START_H and NIGHT_END_H
    if night.should_sleep(io.ticks):
        display_power(False)
        draw_sleep()
        _maybe_night_reboot()
        return

    # clamp the page in case the data shrank since the last frame; a forced page
    # (simulator testing) re-applies once the dashboard data has loaded.
    mp = max_page()
    mn = min_page()
    if _forced_page is not None:
        page = _forced_page
    if page > mp:
        page = mp
    if page < mn:
        page = mn

    display_power(True)
    if view == "workout":
        draw_workout(wk_idx)
    elif page < 0:
        draw_pastweeks(page)
    elif page > 0:
        draw_lookahead(page)
    else:
        draw()


def update():
    # Never let an exception escape to run(): that would end the loop and the
    # firmware would reset back to the launcher. This keeps runlog up 24/7.
    try:
        _update_impl()
    except Exception as e:
        try:
            print("update error:", e)
        except Exception:
            pass
    return None


if __name__ == "__main__":
    run(update)

import sys
import os

sys.path.insert(0, "/system/apps/runlog")
os.chdir("/system/apps/runlog")

from badgeware import io, brushes, shapes, screen, PixelFont, run
import network
from urllib.urequest import urlopen
import json
import gc

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
gray = brushes.color(110, 120, 130)
dim = brushes.color(70, 78, 88)
track = brushes.color(38, 44, 52)
blue = brushes.color(48, 148, 255)
green = brushes.color(63, 210, 110)
orange = brushes.color(255, 165, 0)
red = brushes.color(248, 81, 73)
purple = brushes.color(188, 140, 255)

# ---------------------------------------------------------------------------
# Config (populated from /secrets.py)
# ---------------------------------------------------------------------------
WIFI_SSID = None
WIFI_PASSWORD = None
DASHBOARD_URL = None          # optional: URL returning dashboard.json
WEATHER_LOCATION = None       # optional: same formats as the weather app
DIST_UNITS = "mi"             # "mi" or "km"
WIFI_TIMEOUT = 45

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
config_loaded = False

weather = None                # {'temp','code','condition'}
aqi = None                    # {'us_aqi','pm2_5','label','brush'}
dashboard = None              # parsed running data
running_live = False          # true when running numbers came from DASHBOARD_URL
weather_live = False          # true when weather came from the network
status = "Starting..."
loading = False
last_update = None
auto_refresh = True
AUTO_REFRESH_MS = 15 * 60 * 1000   # 15 minutes

# ---------------------------------------------------------------------------
# Demo data so the dashboard renders even with no WiFi / no backend yet
# ---------------------------------------------------------------------------
DEMO_DASHBOARD = {
    "week_start": "Mon",
    "units": "mi",
    "people": [
        {"name": "Ruby", "planned": 35.0, "actual": 23.6},
        {"name": "Jiaren", "planned": 42.0, "actual": 18.3},
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
    global DIST_UNITS, config_loaded
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
        sys.path.pop(0)
    except Exception as e:
        print("config load error:", e)


# ---------------------------------------------------------------------------
# WiFi
# ---------------------------------------------------------------------------
def wlan_start():
    global wlan, ticks_start, connected
    if ticks_start is None:
        ticks_start = io.ticks
    if connected:
        return True
    if not WIFI_SSID:
        return False
    if wlan is None:
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        if wlan.isconnected():
            connected = True
            return True
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        print("Connecting to WiFi...")
    connected = wlan.isconnected()
    if connected:
        return True
    if io.ticks - ticks_start > WIFI_TIMEOUT * 1000:
        return False
    return None  # still trying


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def http_json(url):
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
        LATITUDE, LONGITUDE = 37.3382, -121.8863
        LOCATION_NAME = "San Jose"
        COUNTRY_CODE = "US"
        use_fahrenheit = True
        location_detected = True


def _is_num(v):
    return isinstance(v, (int, float))


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
    global weather
    unit = "fahrenheit" if use_fahrenheit else "celsius"
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&current=temperature_2m,weather_code"
           "&hourly=precipitation_probability,precipitation&forecast_hours=2"
           "&temperature_unit=%s&timezone=auto"
           % (LATITUDE, LONGITUDE, unit))
    r = http_json(url)
    c = r["current"]
    code = c["weather_code"]
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


def refresh_all():
    """Pull everything; fall back to demo data on any failure."""
    global weather, aqi, dashboard, running_live, weather_live
    global status, loading, last_update
    loading = True
    running_live = False
    weather_live = False

    state = wlan_start()
    if state is True:
        try:
            resolve_location()
        except Exception as e:
            print("location error:", e)
        try:
            fetch_weather()
            weather_live = True
        except Exception as e:
            print("weather error:", e)
        try:
            fetch_aqi()
        except Exception as e:
            print("aqi error:", e)
        try:
            if fetch_dashboard():
                running_live = True
        except Exception as e:
            print("dashboard error:", e)
    elif state is None:
        status = "Connecting WiFi..."
        loading = False
        return  # keep trying next frame

    # Fallbacks so the screen is always useful
    if weather is None:
        weather = dict(DEMO_WEATHER)
    if aqi is None:
        aqi = dict(DEMO_AQI)
    if dashboard is None:
        dashboard = dict(DEMO_DASHBOARD)

    # Footer status reflects the RUNNING data (the app's purpose)
    if running_live:
        status = "Live"
    elif not WIFI_SSID:
        status = "Demo - no WiFi"
    elif not DASHBOARD_URL:
        status = "Demo - set URL"
    else:
        status = "Offline - retry"

    last_update = io.ticks
    loading = False
    gc.collect()


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
def fmt_dist(v):
    try:
        return "%.1f" % float(v)
    except Exception:
        return "0.0"


def pct_brush(p):
    if p >= 100:
        return green
    if p >= 90:
        return green
    if p >= 60:
        return orange
    return red


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


def draw():
    screen.brush = background
    screen.clear()

    units = "mi"
    week = ""
    people = []
    if dashboard:
        units = dashboard.get("units", DIST_UNITS)
        week = dashboard.get("week_start", "")
        people = dashboard.get("people", []) or []

    # ---- header ----
    screen.font = small_font
    screen.brush = phosphor
    screen.text("TRAINING", 8, 3)

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
    screen.brush = dim
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
    screen.brush = dim
    hint = "B refresh"
    hw, _ = screen.measure_text(hint)
    screen.text(hint, 152 - hw, 110)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
started = False


def update():
    global started, auto_refresh
    if not started:
        started = True
        load_config()
        refresh_all()

    # still connecting? keep trying
    if status == "Connecting WiFi...":
        refresh_all()

    if io.BUTTON_B in io.pressed:
        refresh_all()

    if (auto_refresh and last_update is not None
            and io.ticks - last_update > AUTO_REFRESH_MS):
        refresh_all()

    draw()


if __name__ == "__main__":
    run(update)

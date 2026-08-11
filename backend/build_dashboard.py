#!/usr/bin/env python3
"""Build dashboard.json for the Badger "runlog" training app.

For every configured person we compute PLANNED and ACTUAL running mileage:

  * ACTUAL  comes from that person's own Garmin Connect account (this week only;
            future weeks have no actual yet).
  * PLANNED comes from that person's own Final Surge training plan, per week.
            Some plans (e.g. the Fleet Feet marathon plans) encode weekly
            mileage as free text at two levels ("Level 1: ~19 miles /
            Level 2: ~23 miles"); we parse that text using each person's
            configured "finalsurge_level" (default 2). An optional
            "finalsurge_level_overrides" map (e.g. {"Wed": 2}) picks a
            different level for specific days of the week. If a person has no
            Final Surge account, or a given week has no parseable planned
            distance, that week's planned mileage is 0.

We emit the current week plus a configurable number of future weeks so the
badge can page through upcoming planned mileage:

    {
      "week_start": "2025-08-04",
      "units": "mi",
      "updated": "2025-08-06T22:00:00Z",
      "names": ["Ruby", "Jiaren"],
      "weeks": [
        {"start": "2025-08-04", "planned": [35.0, 42.0], "actual": [23.6, 18.3]},
        {"start": "2025-08-11", "planned": [38.0, 26.2], "actual": [0.0, 0.0]},
        ...
      ],
      "people": [                      # legacy: current week only, for old apps
        {"name": "Ruby",   "planned": 35.0, "actual": 23.6},
        {"name": "Jiaren", "planned": 42.0, "actual": 18.3}
      ]
    }

Secrets are read from environment variables (never stored in config.json), so
this runs cleanly in GitHub Actions with repository secrets.

Usage:
    python build_dashboard.py                 # normal build
    python build_dashboard.py --demo          # write sample data, no network
    python build_dashboard.py --debug-finalsurge "Ruby"   # dump one raw workout
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import sys

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
METERS_PER_MILE = 1609.344
METERS_PER_KM = 1000.0
_DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Weeks of lookahead to emit beyond the current week (overridable in config.json).
DEFAULT_LOOKAHEAD_WEEKS = 8
# Past weeks of progress to emit before the current week (overridable in config).
DEFAULT_PAST_WEEKS = 4
# Which mileage tier to read from multi-level plan text (per-person overridable).
DEFAULT_FS_LEVEL = 2


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_dotenv(path):
    """Load KEY=VALUE lines from a .env file into os.environ (no dependency).

    Existing environment variables win (so CI secrets are never overridden).
    Silently does nothing if the file is absent.
    """
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception as e:
        print("WARN: could not read %s: %s" % (path, e), file=sys.stderr)


def env(name, required=True):
    val = os.environ.get(name)
    if required and not val:
        raise RuntimeError("Missing environment variable: %s" % name)
    return val


def env_opt(name):
    """Read an optional env var by (possibly missing) name."""
    if not name:
        return None
    return os.environ.get(name)


def to_units(meters, units):
    return meters / (METERS_PER_MILE if units == "mi" else METERS_PER_KM)


def week_bounds(tzname):
    tz = ZoneInfo(tzname) if (ZoneInfo and tzname) else dt.timezone.utc
    now = dt.datetime.now(tz)
    monday = (now - dt.timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    sunday = monday + dt.timedelta(days=6)
    return now, monday, sunday


def week_mondays(base_monday, num_weeks):
    return [base_monday + dt.timedelta(days=7 * i) for i in range(num_weeks)]


# ---------------------------------------------------------------------------
# Garmin Connect  (unofficial, via the `garminconnect` library — no partner
# approval needed; logs in with your normal Garmin account).
#
# Per-person auth, checked in order:
#   1. <garmin_tokens_env>        - base64 token blob from garmin_setup.py.
#   2. GARMIN_TOKENS_BASE64       - shared token blob (single-account fallback).
#   3. <garmin_email_env> + <garmin_password_env>
#   4. GARMIN_EMAIL + GARMIN_PASSWORD  - shared login (single-account fallback).
# Token blobs are best for CI: no password or MFA prompt on each run.
# ---------------------------------------------------------------------------
def garmin_login_person(p):
    from garminconnect import Garmin  # imported lazily so demo runs need nothing

    tokens = env_opt(p.get("garmin_tokens_env")) or os.environ.get("GARMIN_TOKENS_BASE64")
    if tokens:
        g = Garmin()
        g.login(tokenstore=tokens)  # >512 chars -> loaded as a base64 token blob
        return g

    email = env_opt(p.get("garmin_email_env")) or os.environ.get("GARMIN_EMAIL")
    password = env_opt(p.get("garmin_password_env")) or os.environ.get("GARMIN_PASSWORD")
    if not email or not password:
        raise RuntimeError("No Garmin credentials for %s" % p.get("name", "?"))
    g = Garmin(email, password)
    g.login()
    return g


def _is_run(activity):
    at = activity.get("activityType") or {}
    key = (at.get("typeKey") or "").lower()
    return "run" in key  # running, trail_running, treadmill_running, virtual_run...


def garmin_week_meters(g, monday, sunday, run_only=True):
    """Sum run distances (meters) for one week using an existing session `g`."""
    start_date = monday.date().isoformat()
    end_date = sunday.date().isoformat()
    activities = g.get_activities_by_date(start_date, end_date) or []
    total = 0.0
    for a in activities:
        if run_only and not _is_run(a):
            continue
        total += float(a.get("distance", 0) or 0)  # meters
    return total


def _activity_date(a):
    """Local calendar date (YYYY-MM-DD) of a Garmin activity, or None."""
    for k in ("startTimeLocal", "startTimeGMT", "beginTimestamp"):
        v = a.get(k)
        if isinstance(v, str) and len(v) >= 10:
            return v[:10]
    return None


def garmin_day_meters(g, monday, sunday, run_only=True):
    """Per-day run distances (meters) keyed by YYYY-MM-DD for one week."""
    start_date = monday.date().isoformat()
    end_date = sunday.date().isoformat()
    activities = g.get_activities_by_date(start_date, end_date) or []
    out = {}
    for a in activities:
        if run_only and not _is_run(a):
            continue
        d = _activity_date(a)
        if not d:
            continue
        out[d] = out.get(d, 0.0) + float(a.get("distance", 0) or 0)
    return out


# ---------------------------------------------------------------------------
# Google Sheet training plan  (PLANNED weekly mileage, per current week)
#
# The Fleet Feet Fall 2026 plan is a public Google Sheet with one row per week
# (columns: Week, Dates "M/D-M/D", ..., Weekly Mileage "~ low-high"). We read
# the tab as CSV (no auth) and pick the row whose week contains "today".
#
# The weekly cell is a range; `target` chooses "low", "high", or "mid".
# ---------------------------------------------------------------------------
DEFAULT_PLAN_SHEET = {
    "id": "1kLi7D6fM4LzT4_95W6ZKr3AYPD423vDkb_ntvUUFUsQ",
    "gids": {"half": 1172380107, "full": 766571768},
    "target": "mid",   # low | high | mid
    "year": 2026,      # year the plan's M/D dates fall in
    "weekly_col": 10,  # 0-based index of the "Weekly Mileage" column
}

_plan_cache = {}  # gid -> list of {"start": date, "end": date, "low", "high"}


def _plan_sheet_cfg(config):
    cfg = dict(DEFAULT_PLAN_SHEET)
    override = (config or {}).get("plan_sheet") or {}
    cfg.update({k: v for k, v in override.items() if k != "gids"})
    gids = dict(DEFAULT_PLAN_SHEET["gids"])
    gids.update(override.get("gids") or {})
    cfg["gids"] = gids
    return cfg


def _parse_weekly_range(cell):
    """'~ 19-23' -> (19.0, 23.0); '~ 34' -> (34.0, 34.0); '' -> None."""
    if not cell:
        return None
    nums = re.findall(r"\d+(?:\.\d+)?", cell)
    if not nums:
        return None
    lo = float(nums[0])
    hi = float(nums[1]) if len(nums) > 1 else lo
    return lo, hi


def _fetch_plan_weeks(sheet_id, gid, year, weekly_col):
    if gid in _plan_cache:
        return _plan_cache[gid]
    url = ("https://docs.google.com/spreadsheets/d/%s/export?format=csv&gid=%s"
           % (sheet_id, gid))
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    weeks = []
    for row in csv.reader(r.text.splitlines()):
        if len(row) <= weekly_col or not row[0].strip().isdigit():
            continue
        dates = row[1].strip()          # "8/10-8/16"
        rng = _parse_weekly_range(row[weekly_col])
        m = re.match(r"\s*(\d{1,2})/(\d{1,2})", dates)
        if not m or rng is None:
            continue
        start = dt.date(year, int(m.group(1)), int(m.group(2)))
        weeks.append({"start": start, "end": start + dt.timedelta(days=6),
                      "low": rng[0], "high": rng[1]})
    _plan_cache[gid] = weeks
    return weeks


def plan_week_miles(config, plan, monday, target=None):
    """Planned miles for the week containing `monday`, or None if out of plan."""
    cfg = _plan_sheet_cfg(config)
    gid = cfg["gids"].get(plan)
    if gid is None:
        raise RuntimeError("Unknown plan '%s' (expected one of %s)"
                           % (plan, ", ".join(cfg["gids"])))
    weeks = _fetch_plan_weeks(cfg["id"], gid, int(cfg["year"]),
                              int(cfg["weekly_col"]))
    today = monday.date()
    match = next((w for w in weeks if w["start"] <= today <= w["end"]), None)
    if match is None:
        return None
    pick = (target or cfg.get("target") or "mid").lower()
    if pick == "low":
        return match["low"]
    if pick == "high":
        return match["high"]
    return (match["low"] + match["high"]) / 2.0


def miles_to_units(miles, units):
    return miles if units == "mi" else miles * (METERS_PER_MILE / METERS_PER_KM)


# ---------------------------------------------------------------------------
# Final Surge  (unofficial API confirmed at beta.finalsurge.com)
#   POST /api/login {email,password} -> data.token, data.user_key
#   GET  /api/WorkoutList?scope=USER&scopekey=<user_key>
#         &startdate=YYYY-MM-DD&enddate=YYYY-MM-DD   (Authorization: Bearer token)
# ---------------------------------------------------------------------------
FS_BASE = "https://beta.finalsurge.com/api"


def finalsurge_login(email, password):
    r = requests.post(FS_BASE + "/login",
                      json={"email": email, "password": password}, timeout=30)
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        raise RuntimeError("Final Surge login failed: %s"
                           % body.get("error_description"))
    data = body["data"]
    return data["token"], data.get("user_key") or data.get("userKey")


def finalsurge_workouts(token, user_key, start_date, end_date):
    r = requests.get(FS_BASE + "/WorkoutList", headers={
        "Authorization": "Bearer %s" % token,
    }, params={
        "scope": "USER",
        "scopekey": user_key,
        "startdate": start_date,
        "enddate": end_date,
    }, timeout=30)
    r.raise_for_status()
    body = r.json()
    data = body.get("data", body)
    if isinstance(data, dict) and "workouts" in data:
        data = data["workouts"]
    return data or []


def _first_number(obj, keys):
    """Search a dict (case-insensitively) for the first present numeric key."""
    if not isinstance(obj, dict):
        return None
    lower = {k.lower(): v for k, v in obj.items()}
    for k in keys:
        v = lower.get(k.lower())
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _distance_meters(dist_value, unit_hint):
    """Convert a Final Surge distance value to meters using a unit hint."""
    if dist_value is None:
        return 0.0
    u = (unit_hint or "").lower()
    if u in ("mi", "mile", "miles"):
        return dist_value * METERS_PER_MILE
    if u in ("km", "kilometer", "kilometers"):
        return dist_value * METERS_PER_KM
    if u in ("m", "meter", "meters"):
        return dist_value
    # No hint: Final Surge distances are commonly stored in meters.
    return dist_value


def _workout_date(w):
    """Best-effort extract a date from a Final Surge workout record."""
    if not isinstance(w, dict):
        return None
    for k in ("workout_date", "WorkoutDate", "workoutDate", "date", "Date",
              "start_date", "StartDate", "planned_date", "PlannedDate"):
        v = w.get(k)
        if isinstance(v, str) and len(v) >= 10:
            try:
                return dt.date.fromisoformat(v[:10])
            except Exception:
                pass
    return None


# Matches "Level 1: ~19 miles" / "Level 2: 6 mi" etc. -> (level, miles).
_FS_LEVEL_RE = re.compile(
    r"level\s*([12])\b[^0-9]*?(\d+(?:\.\d+)?)\s*(?:mi|mile|miles)\b", re.I)


def _parse_level_miles(text, level=DEFAULT_FS_LEVEL):
    """Miles for the given plan ``level`` parsed from Final Surge text.

    Returns a float (miles) or None when the text has no "Level N: X miles"
    marker. If the requested level is absent but the other level is present,
    the other level is used as a fallback.
    """
    if not text:
        return None
    found = {}
    for m in _FS_LEVEL_RE.finditer(text):
        try:
            found[int(m.group(1))] = float(m.group(2))
        except ValueError:
            pass
    if not found:
        return None
    if level in found:
        return found[level]
    return found.get(1) if found.get(1) is not None else found.get(2)


# "Level 1:" / "Level 2:" section markers used to split a quality-workout
# description into its two per-level prescriptions.
_FS_LEVEL_MARK = re.compile(r"level\s*([12])\s*:\s*", re.I)


def _condense_spec(text):
    """Collapse a multi-line spec block into one comma-joined line."""
    out = []
    for ln in (text or "").split("\n"):
        ln = " ".join(ln.split())        # squeeze internal whitespace
        if ln:
            out.append(ln)
    return ", ".join(out)


def _level_detail(w):
    """(headline, level1, level2) for a "quality" group workout, else Nones.

    Group workouts (Intervals/Hills/Tempo) describe the session as reps for
    each level (e.g. "Level 1: 4 x 1K Loops / Level 2: 5 x 1K Loops") rather
    than a single "Level N: X miles" distance. Detail is only returned when the
    description carries both level markers but NO plain per-level mileage (plain
    runs are shown as a number instead). The headline is the workout type taken
    from the first descriptive line (text before " - ").
    """
    desc = w.get("description") or w.get("Description") or ""
    if not desc or _parse_level_miles(desc) is not None:
        return (None, None, None)
    marks = list(_FS_LEVEL_MARK.finditer(desc))
    if len(marks) < 2:
        return (None, None, None)
    headline = ""
    name = (w.get("name") or w.get("Name") or "").strip().lower()
    for ln in desc.split("\n"):
        s = ln.strip()
        if not s or _FS_LEVEL_MARK.match(s):
            continue
        if s.lower() == name:            # skip a line that repeats the name
            continue
        headline = s.split(" - ")[0].strip()
        break
    specs = {}
    for i, m in enumerate(marks):
        lvl = int(m.group(1))
        if lvl in specs:
            continue
        block = desc[m.end():(marks[i + 1].start() if i + 1 < len(marks)
                              else len(desc))]
        cut = block.find("\n\n")           # stop before trailing prose
        if cut != -1:
            block = block[:cut]
        wk = re.search(r"\bworkout\s*:", block, re.I)
        if wk:
            block = block[:wk.start()]
        specs[lvl] = _condense_spec(block)
    l1, l2 = specs.get(1), specs.get(2)
    if not (l1 or l2):
        return (None, None, None)
    return (headline or None, l1, l2)


def _planned_meters(w, level=DEFAULT_FS_LEVEL):
    """Planned distance (meters) for one Final Surge workout record.

    Prefers a structured planned distance when present; otherwise parses the
    per-run mileage out of the workout description text for the chosen plan
    ``level`` (e.g. "Level 2: 6 miles at Easy Run Pace").
    """
    planned = w.get("Planned") or w.get("planned") or {}
    p_dist = _first_number(planned, ["Distance", "distance", "PlannedDistance"])
    if p_dist:
        p_unit = None
        if isinstance(planned, dict):
            p_unit = (planned.get("DistanceUnit") or planned.get("distanceUnit")
                      or planned.get("unit"))
        return _distance_meters(p_dist, p_unit)
    # Text plans: per-run miles live in the description ("Level N: X miles").
    desc = w.get("description") or w.get("Description") or ""
    mi = _parse_level_miles(desc, level)
    if mi is not None:
        return mi * METERS_PER_MILE
    return 0.0


# Day-of-week names (any casing / 3-letter abbrev) -> Python weekday index.
_WEEKDAY_IDX = {}
for _i, _names in enumerate((
        ("mon", "monday"), ("tue", "tues", "tuesday"), ("wed", "weds", "wednesday"),
        ("thu", "thur", "thurs", "thursday"), ("fri", "friday"),
        ("sat", "saturday"), ("sun", "sunday"))):
    for _n in _names:
        _WEEKDAY_IDX[_n] = _i


def _normalize_level_overrides(raw):
    """Config {"Wed": 2, ...} -> {weekday_index: level}. Ignores bad entries."""
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            idx = _WEEKDAY_IDX.get(str(k).strip().lower())
            try:
                lvl = int(v)
            except (TypeError, ValueError):
                continue
            if idx is not None and lvl in (1, 2):
                out[idx] = lvl
    return out


def _resolve_level(w, level, overrides):
    """Plan level for one workout: a per-weekday override wins over ``level``."""
    if overrides:
        d = _workout_date(w)
        if d is not None:
            ov = overrides.get(d.weekday())
            if ov is not None:
                return ov
    return level


def _effective_planned_meters(workouts, level=DEFAULT_FS_LEVEL, overrides=None):
    """Per-date planned meters for counted runs, with weekly back-fill.

    Runs whose text or structured data give an explicit distance keep it.
    Some sessions (e.g. a "Group Workout" of drills/intervals) count as a
    planned run but state no mileage, so they parse to 0. When a week's coach
    summary note ("Level N: ~X miles") totals more than the sum of that week's
    explicit runs, the shortfall is spread evenly over the week's mileage-less
    runs. This surfaces every planned session on the badge and makes the weekly
    total match the plan's stated volume. Returns {iso_date: meters}.
    """
    per_date = {}        # iso -> meters (explicit; 0 for mileage-less runs)
    zero_runs = {}       # week_monday_iso -> [iso, ...] counted runs with 0 miles
    explicit_wk = {}     # week_monday_iso -> summed explicit run meters
    summary_wk = {}      # week_monday_iso -> coach summary target meters
    for w in workouts:
        d = _workout_date(w)
        if not d:
            continue
        iso = d.isoformat()
        wk = (d - dt.timedelta(days=d.weekday())).isoformat()
        lvl = _resolve_level(w, level, overrides)
        if _counts_as_planned_run(w):
            m = _planned_meters(w, lvl)
            per_date[iso] = per_date.get(iso, 0.0) + m
            if m > 0:
                explicit_wk[wk] = explicit_wk.get(wk, 0.0) + m
            else:
                zero_runs.setdefault(wk, []).append(iso)
        else:
            # A non-run note may be the plan's weekly-summary total entry.
            txt = w.get("description") or w.get("Description") or ""
            mi = _parse_level_miles(txt, lvl)
            if mi is None:
                mi = _parse_level_miles(_workout_title(w), lvl)
            if mi is not None:
                summary_wk[wk] = max(summary_wk.get(wk, 0.0),
                                     mi * METERS_PER_MILE)
    for wk, isos in zero_runs.items():
        target = summary_wk.get(wk)
        if not target:
            continue
        remainder = target - explicit_wk.get(wk, 0.0)
        if remainder <= 0:
            continue
        share = remainder / len(isos)
        for iso in isos:
            per_date[iso] = per_date.get(iso, 0.0) + share
    return per_date


def finalsurge_planned_by_week(email, password, mondays, units,
                               level=DEFAULT_FS_LEVEL):
    """Return {monday_iso: planned_units} across the full lookahead range."""
    token, user_key = finalsurge_login(email, password)
    start = mondays[0].date().isoformat()
    end = (mondays[-1] + dt.timedelta(days=6)).date().isoformat()
    workouts = finalsurge_workouts(token, user_key, start, end)
    return _planned_by_week(workouts, units, level)


def _planned_by_week(workouts, units, level=DEFAULT_FS_LEVEL, overrides=None):
    """Weekly planned mileage {monday_iso: units}.

    The weekly total is the sum of the week's run-type workouts (each run's
    per-level mileage parsed from its description). Coaching tips and cross-
    training (given in minutes) are skipped. A run that counts as planned but
    states no mileage (e.g. a group workout of drills) is back-filled from the
    week's coach summary total so the weekly volume matches the plan's stated
    intent. Weeks with no parseable running mileage are omitted (planned = 0).
    A per-weekday level override (e.g. Wednesday on level 2) is honored.
    """
    per_date = _effective_planned_meters(workouts, level, overrides)
    buckets = {}
    for iso, m in per_date.items():
        if m <= 0:
            continue
        d = dt.date.fromisoformat(iso)
        key = (d - dt.timedelta(days=d.weekday())).isoformat()
        buckets[key] = buckets.get(key, 0.0) + m
    return {k: to_units(v, units) for k, v in buckets.items()}


def _workout_title(w):
    """Best-effort short description/name of a Final Surge workout."""
    planned = w.get("Planned") or w.get("planned") or {}
    for src in (w, planned):
        if not isinstance(src, dict):
            continue
        for k in ("name", "Name", "title", "Title", "description",
                  "Description", "WorkoutName", "workout_name", "label",
                  "Label", "notes", "Notes"):
            v = src.get(k)
            if isinstance(v, str) and v.strip():
                return " ".join(v.split())
    # Fall back to the discipline/type (e.g. "Run", "Bike").
    for k in ("Discipline", "discipline", "ActivityType", "activityType",
              "type", "Type", "activity"):
        v = w.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _is_run_workout(w):
    """True if a Final Surge workout's planned activity type is a run."""
    acts = w.get("Activities")
    if isinstance(acts, list):
        for a in acts:
            if isinstance(a, dict) and "run" in (a.get("activity_type_name") or "").lower():
                return True
    return False


def _counts_as_planned_run(w):
    """Whether a workout contributes to planned running mileage.

    True for run-type workouts and for any workout carrying a structured
    planned distance. This excludes coaching tips, cross-training (expressed
    in minutes) and the plan's weekly-summary total entry, whose miles already
    equal the sum of the week's runs and would otherwise double-count.
    """
    if _is_run_workout(w):
        return True
    planned = w.get("Planned") or w.get("planned") or {}
    return bool(_first_number(planned, ["Distance", "distance", "PlannedDistance"]))


def finalsurge_days(workouts, monday, units, level=DEFAULT_FS_LEVEL,
                    overrides=None):
    """Per-day planned detail keyed by ISO date: {iso: {'dist','title','plan'}}.

    When `monday` is a week's Monday, only that Mon..Sun window is returned; pass
    `monday=None` to bucket every workout in `workouts` (the whole fetched range),
    which is what the multi-week plan calendar uses. A per-weekday level override
    (e.g. Wednesday on level 2) is applied per workout. ``plan`` marks days that
    hold a genuine planned session (so the badge can navigate to a group workout
    even when its distance is unspecified); ``dist`` is back-filled from the
    week's coach summary for counted runs that state no mileage.
    """
    lo = hi = None
    if monday is not None:
        lo = monday.date()
        hi = lo + dt.timedelta(days=6)
    per_date = _effective_planned_meters(workouts, level, overrides)
    out = {}
    for w in workouts:
        d = _workout_date(w)
        if not d:
            continue
        if lo is not None and (d < lo or d > hi):
            continue
        iso = d.isoformat()
        e = out.get(iso) or {"dist": 0.0, "title": "", "plan": False}
        if _counts_as_planned_run(w):
            e["plan"] = True
            hl, l1, l2 = _level_detail(w)
            if l1 or l2:
                e["wtype"] = hl or ""
                e["l1"] = l1 or ""
                e["l2"] = l2 or ""
        title = _workout_title(w)
        is_run = _is_run_workout(w)
        # Prefer a real run's title over coaching tips / cross-training notes.
        if title and (not e["title"] or (is_run and not e.get("_run"))):
            e["title"] = title
            e["_run"] = is_run
        out[iso] = e
    for iso, e in out.items():
        e["dist"] = to_units(per_date.get(iso, 0.0), units)
        e.pop("_run", None)
    return out


def finalsurge_plan(email, password, mondays, units, current_monday,
                    level=DEFAULT_FS_LEVEL, overrides=None):
    """One login + one fetch -> (planned_by_week_map, full_range_day_map).

    The day map spans the entire fetched range (every week, not just the current
    one) so the badge can show planned workouts for past and upcoming weeks too.
    ``overrides`` maps a weekday index to a plan level (e.g. Wednesday -> 2).
    """
    token, user_key = finalsurge_login(email, password)
    start = mondays[0].date().isoformat()
    end = (mondays[-1] + dt.timedelta(days=6)).date().isoformat()
    workouts = finalsurge_workouts(token, user_key, start, end)
    return (_planned_by_week(workouts, units, level, overrides),
            finalsurge_days(workouts, None, units, level, overrides))


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build_payload(cfg, units, tzname):
    now, base_monday, _ = week_bounds(tzname)
    lookahead = int(cfg.get("lookahead_weeks", DEFAULT_LOOKAHEAD_WEEKS))
    past_n = int(cfg.get("past_weeks", DEFAULT_PAST_WEEKS))

    # Mondays: [oldest past ... last week, CURRENT, future ...]
    past_mondays = [base_monday - dt.timedelta(days=7 * (past_n - i))
                    for i in range(max(0, past_n))]
    fut_mondays = week_mondays(base_monday, 1 + max(0, lookahead))
    all_mondays = past_mondays + fut_mondays

    people = cfg.get("people", [])
    names = [p.get("name", "?") for p in people]

    planned_maps = []        # per person: {monday_iso: planned_units}
    fs_day_maps = []         # per person: {iso: {'dist','title'}} FULL range
    day_actual_maps = []     # per person: {iso: actual_units} current + past days
    cur_actual_total = []    # per person: this week's actual total (units)
    past_actual_maps = []    # per person: {monday_iso: actual_units}
    for p in people:
        name = p.get("name", "?")

        planned, fs_days = {}, {}
        fs_email = env_opt(p.get("finalsurge_email_env"))
        fs_password = env_opt(p.get("finalsurge_password_env"))
        fs_level = int(p.get("finalsurge_level") or DEFAULT_FS_LEVEL)
        fs_overrides = _normalize_level_overrides(
            p.get("finalsurge_level_overrides"))
        if fs_email and fs_password:
            try:
                planned, fs_days = finalsurge_plan(
                    fs_email, fs_password, all_mondays, units, base_monday,
                    fs_level, fs_overrides)
            except Exception as e:
                print("WARN: Final Surge for %s failed: %s" % (name, e),
                      file=sys.stderr)

        # Fill any week with no Final Surge planned distance from the public
        # Google Sheet training plan (e.g. "full"/"half"), when the person has a
        # `plan` configured. Final Surge (per-day, personalised) still wins where
        # present; the sheet supplies the official weekly target elsewhere --
        # notably future weeks Final Surge hasn't populated yet.
        plan_name = p.get("plan")
        if plan_name:
            for mon in all_mondays:
                iso = mon.date().isoformat()
                if planned.get(iso):
                    continue
                try:
                    miles = plan_week_miles(cfg, plan_name, mon)
                except Exception as e:
                    print("WARN: plan sheet for %s failed: %s" % (name, e),
                          file=sys.stderr)
                    break
                if miles is not None:
                    planned[iso] = miles_to_units(miles, units)
        planned_maps.append(planned)
        fs_day_maps.append(fs_days)

        day_actual, past_actual = {}, {}
        total = 0.0
        try:
            g = garmin_login_person(p)
            day_actual_m = garmin_day_meters(
                g, base_monday, base_monday + dt.timedelta(days=6))
            day_actual = {k: to_units(v, units) for k, v in day_actual_m.items()}
            total = to_units(sum(day_actual_m.values()), units)
            for pmon in past_mondays:
                try:
                    # per-day meters -> weekly total (sum) AND per-day actuals so
                    # past-week workouts can show what was actually run.
                    dm = garmin_day_meters(
                        g, pmon, pmon + dt.timedelta(days=6))
                    past_actual[pmon.date().isoformat()] = to_units(
                        sum(dm.values()), units)
                    for k, v in dm.items():
                        day_actual[k] = to_units(v, units)
                except Exception as e:
                    print("WARN: Garmin past week for %s failed: %s" % (name, e),
                          file=sys.stderr)
        except Exception as e:
            print("WARN: Garmin for %s failed: %s" % (name, e), file=sys.stderr)
        day_actual_maps.append(day_actual)
        cur_actual_total.append(total)
        past_actual_maps.append(past_actual)

    def planned_row(iso):
        row = []
        for pi in range(len(people)):
            pv = planned_maps[pi].get(iso) or 0.0
            row.append(round(pv, 1))
        return row

    # ---- current + future weeks (actual only for the current week) ----
    weeks = []
    for i, mon in enumerate(fut_mondays):
        iso = mon.date().isoformat()
        actual_row = [round(cur_actual_total[pi], 1) if i == 0 else 0.0
                      for pi in range(len(people))]
        weeks.append({"start": iso, "planned": planned_row(iso),
                      "actual": actual_row})

    # ---- past weeks (progress: planned + actual), oldest -> newest ----
    past = []
    for pmon in past_mondays:
        iso = pmon.date().isoformat()
        actual_row = [round(past_actual_maps[pi].get(iso, 0.0), 1)
                      for pi in range(len(people))]
        past.append({"start": iso, "planned": planned_row(iso),
                     "actual": actual_row})

    # ---- per-day plan calendar for EVERY week (oldest past -> future) ----
    # This is a flat, date-ordered list so the badge can step through workouts
    # across week boundaries (past and upcoming), not just the current week.
    days = []
    for mon in all_mondays:
        for k in range(7):
            d = mon + dt.timedelta(days=k)
            iso = d.date().isoformat()
            workouts = []
            for pi in range(len(people)):
                fd = fs_day_maps[pi].get(iso) or {}
                dist = round(float(fd.get("dist", 0.0) or 0.0), 1)
                done = day_actual_maps[pi].get(iso)
                title = fd.get("title", "") or ""
                # Emit only non-default fields. The badge reads every workout
                # key with a .get() default, so omitting zeros/blanks/nulls is
                # invisible to it but roughly halves the (91-day) payload -- and
                # the whole file must fit the badge's small TLS/heap budget or
                # the fetch fails ("Offline - retry"). The badge is the only
                # consumer of dashboard.json, so this is safe.
                wo = {}
                if dist:
                    wo["dist"] = dist
                if title:
                    wo["title"] = title
                if done is not None:
                    wo["done"] = round(float(done), 1)
                if fd.get("plan"):
                    wo["plan"] = True
                # Quality group workout: carry the per-level prescription so the
                # badge can show Level 1 / Level 2 detail instead of a distance.
                if fd.get("l1") or fd.get("l2"):
                    wo["l1"] = fd.get("l1", "") or ""
                    wo["l2"] = fd.get("l2", "") or ""
                    if fd.get("wtype"):
                        wo["wtype"] = fd["wtype"]
                workouts.append(wo)
            days.append({"date": iso, "dow": _DOW[d.weekday()],
                         "workouts": workouts})

    # Legacy current-week people list so an un-updated badge app still renders.
    legacy_people = []
    if weeks:
        w0 = weeks[0]
        for pi, name in enumerate(names):
            legacy_people.append({
                "name": name,
                "planned": w0["planned"][pi],
                "actual": w0["actual"][pi],
            })

    return {
        "week_start": base_monday.date().isoformat(),
        "units": units,
        "updated": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "today": now.date().isoformat(),
        "names": names,
        "weeks": weeks,
        "past": past,
        "days": days,
        "people": legacy_people,
    }


# ---------------------------------------------------------------------------
# Demo (no network) — mirrors the real multi-week schema.
# ---------------------------------------------------------------------------
def demo_payload(units, tzname):
    now, base_monday, _ = week_bounds(tzname)
    names = ["Ruby", "Jiaren"]
    ruby_plan = [35, 38, 40, 42, 30, 45, 48, 26.2, 50]
    jiaren_plan = [42, 44, 26.2, 46, 48, 50, 30, 52, 26.2]
    n = min(len(ruby_plan), len(jiaren_plan))
    mondays = week_mondays(base_monday, n)
    weeks = []
    for i, mon in enumerate(mondays):
        actual = [23.6, 18.3] if i == 0 else [0.0, 0.0]
        weeks.append({
            "start": mon.date().isoformat(),
            "planned": [float(ruby_plan[i]), float(jiaren_plan[i])],
            "actual": actual,
        })

    # A few past weeks of "progress" (planned + actual), oldest -> newest.
    past = []
    past_demo = [([32, 40], [30.1, 41.2]), ([34, 38], [34.0, 22.5]),
                 ([36, 44], [35.2, 44.6]), ([35, 42], [31.8, 39.0])]
    for i, (pl, ac) in enumerate(past_demo):
        pmon = base_monday - dt.timedelta(days=7 * (len(past_demo) - i))
        past.append({"start": pmon.date().isoformat(),
                     "planned": [float(pl[0]), float(pl[1])],
                     "actual": [float(ac[0]), float(ac[1])]})

    # Per-day detail for the current week (Mon..Sun). "today" is Wednesday here.
    ruby_days = [(8, "Easy", 8.1), (10, "Intervals 6x800", 10.2),
                 (6, "Recovery", 5.8), (0, "Rest", None),
                 (12, "Tempo", None), (5, "Easy", None), (14, "Long run", None)]
    jiaren_days = [(6, "Easy", 6.0), (12, "Track 5x1k", 11.7),
                   (8, "Easy", 4.0), (8, "Easy", None),
                   (0, "Rest", None), (6, "Easy", None), (16, "Long run", None)]
    days = []
    for k in range(7):
        d = base_monday + dt.timedelta(days=k)
        rd, jd = ruby_days[k], jiaren_days[k]
        days.append({
            "date": d.date().isoformat(), "dow": _DOW[d.weekday()],
            "workouts": [
                {"dist": float(rd[0]), "title": rd[1], "done": rd[2]},
                {"dist": float(jd[0]), "title": jd[1], "done": jd[2]},
            ],
        })

    legacy_people = [
        {"name": "Ruby", "planned": weeks[0]["planned"][0], "actual": weeks[0]["actual"][0]},
        {"name": "Jiaren", "planned": weeks[0]["planned"][1], "actual": weeks[0]["actual"][1]},
    ]
    # Demo "today" = the current week's Wednesday, so LEFT/RIGHT have room to move.
    today = (base_monday + dt.timedelta(days=2)).date().isoformat()
    return {
        "week_start": base_monday.date().isoformat(),
        "units": units,
        "updated": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "today": today,
        "names": names,
        "weeks": weeks,
        "past": past,
        "days": days,
        "people": legacy_people,
    }


# ---------------------------------------------------------------------------
# Debug
# ---------------------------------------------------------------------------
def debug_finalsurge(cfg, name):
    for p in cfg.get("people", []):
        if p.get("name") != name:
            continue
        email = env(p["finalsurge_email_env"])
        password = env(p["finalsurge_password_env"])
        _, base_monday, _ = week_bounds(cfg.get("timezone", "America/Los_Angeles"))
        mondays = week_mondays(base_monday, 2)
        token, user_key = finalsurge_login(email, password)
        workouts = finalsurge_workouts(
            token, user_key, mondays[0].date().isoformat(),
            (mondays[-1] + dt.timedelta(days=6)).date().isoformat())
        print(json.dumps(workouts[0] if workouts else {}, indent=2)[:4000])
        return
    print("No such person: %s" % name, file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
DEFAULT_CFG = {
    "units": "mi",
    "timezone": "America/Los_Angeles",
    "output": os.path.join(HERE, "public", "dashboard.json"),
    "lookahead_weeks": DEFAULT_LOOKAHEAD_WEEKS,
    "past_weeks": DEFAULT_PAST_WEEKS,
    "people": [{"name": "Ruby"}, {"name": "Jiaren"}],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--demo", action="store_true",
                    help="Write sample data without any network calls.")
    ap.add_argument("--debug-finalsurge", metavar="NAME", default=None,
                    help="Print one raw Final Surge workout for NAME and exit.")
    args = ap.parse_args()

    # Load backend/.env for local runs (CI passes real secrets as env vars,
    # which take precedence and are never overridden by the file).
    load_dotenv(os.path.join(HERE, ".env"))

    if os.path.exists(args.config):
        cfg = load_config(args.config)
    else:
        cfg = json.loads(json.dumps(DEFAULT_CFG))

    units = cfg.get("units", "mi")
    tzname = cfg.get("timezone", "America/Los_Angeles")
    out_path = cfg.get("output", os.path.join(HERE, "public", "dashboard.json"))
    if not os.path.isabs(out_path):
        out_path = os.path.join(HERE, out_path)

    if args.debug_finalsurge:
        debug_finalsurge(cfg, args.debug_finalsurge)
        return

    if args.demo:
        write_output(out_path, demo_payload(units, tzname))
        return

    write_output(out_path, build_payload(cfg, units, tzname))


def write_output(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    print("Wrote %s" % path)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()

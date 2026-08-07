#!/usr/bin/env python3
"""Build dashboard.json for the Badger "runlog" training app.

For every configured person we compute PLANNED and ACTUAL running mileage:

  * ACTUAL  comes from that person's own Garmin Connect account (this week only;
            future weeks have no actual yet).
  * PLANNED comes from that person's own Final Surge training plan, per week.
            Some plans (e.g. the Fleet Feet marathon plans) encode weekly
            mileage as free text at two levels ("Level 1: ~19 miles /
            Level 2: ~23 miles"); we parse that text using each person's
            configured "finalsurge_level" (default 2). If a person has no
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


def finalsurge_planned_by_week(email, password, mondays, units,
                               level=DEFAULT_FS_LEVEL):
    """Return {monday_iso: planned_units} across the full lookahead range."""
    token, user_key = finalsurge_login(email, password)
    start = mondays[0].date().isoformat()
    end = (mondays[-1] + dt.timedelta(days=6)).date().isoformat()
    workouts = finalsurge_workouts(token, user_key, start, end)
    return _planned_by_week(workouts, units, level)


def _planned_by_week(workouts, units, level=DEFAULT_FS_LEVEL):
    """Weekly planned mileage {monday_iso: units}.

    The weekly total is the sum of the week's run-type workouts (each run's
    per-level mileage parsed from its description). Coaching tips, cross-
    training (given in minutes) and the plan's own weekly-summary total entry
    are skipped so they never inflate or double-count the total. Weeks with no
    parseable running mileage are omitted (planned = 0).
    """
    buckets = {}
    for w in workouts:
        if not _counts_as_planned_run(w):
            continue
        d = _workout_date(w)
        if not d:
            continue
        m = _planned_meters(w, level)
        if m <= 0:
            continue
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


def finalsurge_days(workouts, monday, units, level=DEFAULT_FS_LEVEL):
    """Per-day planned detail for ONE week: {iso: {'dist':units,'title':str}}."""
    sunday = monday.date() + dt.timedelta(days=6)
    out = {}
    for w in workouts:
        d = _workout_date(w)
        if not d or d < monday.date() or d > sunday:
            continue
        iso = d.isoformat()
        e = out.get(iso) or {"dist": 0.0, "title": ""}
        if _counts_as_planned_run(w):
            e["dist"] += to_units(_planned_meters(w, level), units)
        title = _workout_title(w)
        is_run = _is_run_workout(w)
        # Prefer a real run's title over coaching tips / cross-training notes.
        if title and (not e["title"] or (is_run and not e.get("_run"))):
            e["title"] = title
            e["_run"] = is_run
        out[iso] = e
    for e in out.values():
        e.pop("_run", None)
    return out


def finalsurge_plan(email, password, mondays, units, current_monday,
                    level=DEFAULT_FS_LEVEL):
    """One login + one fetch -> (planned_by_week_map, current_week_day_map)."""
    token, user_key = finalsurge_login(email, password)
    start = mondays[0].date().isoformat()
    end = (mondays[-1] + dt.timedelta(days=6)).date().isoformat()
    workouts = finalsurge_workouts(token, user_key, start, end)
    return _planned_by_week(workouts, units, level), finalsurge_days(
        workouts, current_monday, units, level)


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
    fs_day_maps = []         # per person: {iso: {'dist','title'}} current week
    cur_day_actual = []      # per person: {iso: actual_units} current week
    cur_actual_total = []    # per person: this week's actual total (units)
    past_actual_maps = []    # per person: {monday_iso: actual_units}
    for p in people:
        name = p.get("name", "?")

        planned, fs_days = {}, {}
        fs_email = env_opt(p.get("finalsurge_email_env"))
        fs_password = env_opt(p.get("finalsurge_password_env"))
        fs_level = int(p.get("finalsurge_level") or DEFAULT_FS_LEVEL)
        if fs_email and fs_password:
            try:
                planned, fs_days = finalsurge_plan(
                    fs_email, fs_password, all_mondays, units, base_monday,
                    fs_level)
            except Exception as e:
                print("WARN: Final Surge for %s failed: %s" % (name, e),
                      file=sys.stderr)
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
                    m = garmin_week_meters(g, pmon, pmon + dt.timedelta(days=6))
                    past_actual[pmon.date().isoformat()] = to_units(m, units)
                except Exception as e:
                    print("WARN: Garmin past week for %s failed: %s" % (name, e),
                          file=sys.stderr)
        except Exception as e:
            print("WARN: Garmin for %s failed: %s" % (name, e), file=sys.stderr)
        cur_day_actual.append(day_actual)
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

    # ---- per-day detail for the CURRENT week (Mon..Sun) ----
    days = []
    for k in range(7):
        d = base_monday + dt.timedelta(days=k)
        iso = d.date().isoformat()
        workouts = []
        for pi in range(len(people)):
            fd = fs_day_maps[pi].get(iso) or {}
            dist = round(float(fd.get("dist", 0.0) or 0.0), 1)
            done = cur_day_actual[pi].get(iso)
            workouts.append({
                "dist": dist,
                "title": fd.get("title", "") or "",
                "done": round(float(done), 1) if done is not None else None,
            })
        days.append({"date": iso, "dow": _DOW[d.weekday()], "workouts": workouts})

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

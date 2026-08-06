#!/usr/bin/env python3
"""Build dashboard.json for the Badger "runlog" training app.

For each configured person it computes this week's PLANNED and ACTUAL running
mileage (week = Monday 00:00 through now, in the configured timezone) and writes
a tiny JSON file the badge fetches over HTTP:

    {
      "week_start": "2025-08-04",
      "units": "mi",
      "updated": "2025-08-06T22:00:00Z",
      "people": [
        {"name": "Jiaren", "planned": 42.0, "actual": 18.3},
        {"name": "Ruby",   "planned": 35.0, "actual": 23.6}
      ]
    }

Data sources per person (set in config.json):
  - "strava":     ACTUAL from Strava activities; PLANNED from "weekly_goal".
  - "finalsurge": PLANNED + ACTUAL from the Final Surge training plan.
  - "manual":     PLANNED from "weekly_goal"; ACTUAL from "manual_actual" (0 default).

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
import sys

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
METERS_PER_MILE = 1609.344
METERS_PER_KM = 1000.0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def env(name, required=True):
    val = os.environ.get(name)
    if required and not val:
        raise RuntimeError("Missing environment variable: %s" % name)
    return val


def to_units(meters, units):
    return meters / (METERS_PER_MILE if units == "mi" else METERS_PER_KM)


def week_bounds(tzname):
    tz = ZoneInfo(tzname) if (ZoneInfo and tzname) else dt.timezone.utc
    now = dt.datetime.now(tz)
    monday = (now - dt.timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    sunday = monday + dt.timedelta(days=6)
    return now, monday, sunday


# ---------------------------------------------------------------------------
# Strava  (https://developers.strava.com)
# ---------------------------------------------------------------------------
def strava_access_token(refresh_token):
    r = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": env("STRAVA_CLIENT_ID"),
        "client_secret": env("STRAVA_CLIENT_SECRET"),
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def strava_week_meters(refresh_token, monday, run_only=True):
    token = strava_access_token(refresh_token)
    after = int(monday.timestamp())
    total = 0.0
    page = 1
    while True:
        r = requests.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers={"Authorization": "Bearer %s" % token},
            params={"after": after, "per_page": 100, "page": page},
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for a in batch:
            if run_only and a.get("type") not in ("Run", "TrailRun", "VirtualRun"):
                continue
            total += float(a.get("distance", 0) or 0)  # meters
        if len(batch) < 100:
            break
        page += 1
    return total


# ---------------------------------------------------------------------------
# Final Surge  (unofficial API confirmed at beta.finalsurge.com)
#   POST /api/login {email,password} -> data.token, data.user_key
#   GET  /api/Data?request=WorkoutList&scope=USER&scopekey=<user_key>
#         &startdate=YYYY-MM-DD&enddate=YYYY-MM-DD   (Bearer token)
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
    r = requests.get(FS_BASE + "/Data", headers={
        "Authorization": "Bearer %s" % token,
    }, params={
        "request": "WorkoutList",
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


def finalsurge_week_meters(email, password, start_date, end_date, debug=False):
    token, user_key = finalsurge_login(email, password)
    workouts = finalsurge_workouts(token, user_key, start_date, end_date)
    if debug:
        sample = workouts[0] if workouts else {}
        print(json.dumps(sample, indent=2)[:4000])
        return 0.0, 0.0
    planned_m = 0.0
    actual_m = 0.0
    for w in workouts:
        planned = w.get("Planned") or w.get("planned") or {}
        # Actual details can be a dict or a list of completed segments.
        details = w.get("Details") or w.get("details") or w.get("Actual") or {}

        p_dist = _first_number(planned, ["Distance", "distance", "PlannedDistance"])
        p_unit = None
        if isinstance(planned, dict):
            p_unit = planned.get("DistanceUnit") or planned.get("distanceUnit") \
                or planned.get("unit")
        planned_m += _distance_meters(p_dist, p_unit)

        if isinstance(details, list):
            for d in details:
                a_dist = _first_number(d, ["distance", "Distance"])
                a_unit = (d.get("DistanceUnit") or d.get("distanceUnit")
                          or d.get("unit")) if isinstance(d, dict) else None
                actual_m += _distance_meters(a_dist, a_unit)
        else:
            a_dist = _first_number(details, ["Distance", "distance", "ActualDistance"])
            a_unit = None
            if isinstance(details, dict):
                a_unit = details.get("DistanceUnit") or details.get("distanceUnit") \
                    or details.get("unit")
            actual_m += _distance_meters(a_dist, a_unit)
    return planned_m, actual_m


# ---------------------------------------------------------------------------
# Per-person resolution
# ---------------------------------------------------------------------------
def resolve_person(p, units, monday, sunday, debug_name=None):
    name = p.get("name", "?")
    source = p.get("source", "manual")
    goal = float(p.get("weekly_goal", 0) or 0)
    start_date = monday.date().isoformat()
    end_date = sunday.date().isoformat()

    planned = goal
    actual = 0.0

    if source == "strava":
        rt = env(p["strava_refresh_token_env"])
        actual = to_units(strava_week_meters(rt, monday), units)
        planned = goal  # Strava has no training plan

    elif source == "finalsurge":
        email = env(p["finalsurge_email_env"])
        password = env(p["finalsurge_password_env"])
        debug = (debug_name is not None and debug_name == name)
        p_m, a_m = finalsurge_week_meters(email, password, start_date, end_date,
                                          debug=debug)
        if debug:
            return None
        planned = to_units(p_m, units) or goal
        actual = to_units(a_m, units)

    elif source == "manual":
        planned = goal
        actual = float(p.get("manual_actual", 0) or 0)

    else:
        raise RuntimeError("Unknown source for %s: %s" % (name, source))

    return {"name": name, "planned": round(planned, 1), "actual": round(actual, 1)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
DEMO = {
    "units": "mi",
    "people": [
        {"name": "Ruby", "planned": 35.0, "actual": 23.6},
        {"name": "Jiaren", "planned": 42.0, "actual": 18.3},
    ],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--demo", action="store_true",
                    help="Write sample data without any network calls.")
    ap.add_argument("--debug-finalsurge", metavar="NAME", default=None,
                    help="Print one raw Final Surge workout for NAME and exit.")
    args = ap.parse_args()

    if os.path.exists(args.config):
        cfg = load_config(args.config)
    else:
        cfg = json.loads(json.dumps(DEMO))  # fall back to demo shape
        cfg["timezone"] = "America/Los_Angeles"
        cfg["output"] = os.path.join(HERE, "public", "dashboard.json")

    units = cfg.get("units", "mi")
    tzname = cfg.get("timezone", "America/Los_Angeles")
    out_path = cfg.get("output", os.path.join(HERE, "public", "dashboard.json"))
    if not os.path.isabs(out_path):
        out_path = os.path.join(HERE, out_path)

    now, monday, sunday = week_bounds(tzname)

    if args.demo:
        payload = {
            "week_start": monday.date().isoformat(),
            "units": units,
            "updated": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "people": DEMO["people"],
        }
        write_output(out_path, payload)
        return

    people = []
    for p in cfg.get("people", []):
        try:
            row = resolve_person(p, units, monday, sunday,
                                 debug_name=args.debug_finalsurge)
            if row is None:  # debug mode
                return
            people.append(row)
        except Exception as e:
            print("WARN: %s failed: %s" % (p.get("name"), e), file=sys.stderr)
            people.append({
                "name": p.get("name", "?"),
                "planned": float(p.get("weekly_goal", 0) or 0),
                "actual": 0.0,
                "error": True,
            })

    payload = {
        "week_start": monday.date().isoformat(),
        "units": units,
        "updated": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "people": people,
    }
    write_output(out_path, payload)


def write_output(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    print("Wrote %s" % path)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()

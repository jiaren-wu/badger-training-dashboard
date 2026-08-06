# Badger Training Dashboard — backend

This builds a tiny `dashboard.json` that your **Badger** "runlog" app fetches
over WiFi to show, for you and Ruby, this week's **planned vs. actual running
mileage** (week = Monday → now). Weather, air quality, and the next-hour
rain / air-quality outlook come straight from the badge itself (Open-Meteo, no
key), so the backend is only responsible for the running numbers.

```
Strava / Final Surge ──> build_dashboard.py ──> dashboard.json ──> GitHub Pages
                              (GitHub Action, every 30 min)              │
                                                                         ▼
                                                        Badger "runlog" app (WiFi)
```

The published file looks like:

```json
{
  "week_start": "2025-08-04",
  "units": "mi",
  "updated": "2025-08-06T22:00:00Z",
  "people": [
    {"name": "Jiaren", "planned": 42.0, "actual": 18.3},
    {"name": "Ruby",   "planned": 35.0, "actual": 23.6}
  ]
}
```

---

## 1. Configure who to track

Copy the example and edit names / goals:

```bash
cp config.example.json config.json
```

`config.json` (safe to commit — it holds **no secrets**, only the *names* of the
environment variables that do):

| field | meaning |
|-------|---------|
| `units` | `"mi"` or `"km"` |
| `timezone` | IANA name, e.g. `America/Los_Angeles`. Defines when the week rolls over. |
| `output` | where to write the JSON (leave as `public/dashboard.json`). |
| `people[].source` | `strava`, `finalsurge`, or `manual`. |
| `people[].weekly_goal` | planned weekly miles (used as PLANNED for `strava`/`manual`, and as a fallback for `finalsurge`). |
| `people[].*_env` | the **name** of the env var / secret holding a credential. |

Sources:

- **`strava`** — ACTUAL = sum of this week's Strava runs. PLANNED = `weekly_goal`
  (Strava has no training plan).
- **`finalsurge`** — PLANNED **and** ACTUAL come from the Final Surge plan.
- **`manual`** — PLANNED = `weekly_goal`, ACTUAL = `manual_actual` (edit by hand).

---

## 2. Provide credentials (local dev)

```bash
cp .env.example .env      # then fill it in; .env is git-ignored
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Strava (once per Strava person)

1. Create an API app at <https://www.strava.com/settings/api> (any name; set
   "Authorization Callback Domain" to `localhost`). Note the **Client ID** and
   **Client Secret**.
2. Put them in `.env` as `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET`.
3. Get a long-lived refresh token:

   ```bash
   export STRAVA_CLIENT_ID=xxxx STRAVA_CLIENT_SECRET=xxxx
   python strava_setup.py
   ```

   Approve in the browser, paste the `code` from the redirect URL, and copy the
   printed `refresh_token` into `.env` (e.g. `STRAVA_REFRESH_TOKEN_JIAREN`).

### Final Surge (once per Final Surge person)

Just the account email + password in `.env`
(`FINALSURGE_EMAIL_RUBY` / `FINALSURGE_PASSWORD_RUBY`). The script logs in to
`beta.finalsurge.com` and reads the workout list for the current week.

---

## 3. Run it

```bash
python build_dashboard.py            # real build -> public/dashboard.json
python build_dashboard.py --demo     # sample data, no network (nice for testing)
```

### Verify the Final Surge field mapping (recommended, one time)

Final Surge's unofficial API isn't formally documented, so distance field
names can vary between accounts. Dump one real workout to confirm the mapping:

```bash
python build_dashboard.py --debug-finalsurge "Ruby"
```

Look at the printed JSON for where planned/actual distance and its unit live. The
parser already tries the common keys (`Planned.Distance` + `DistanceUnit`,
`Details[].distance`, etc.) and assumes **meters** when no unit is given. If your
account uses different keys, adjust `finalsurge_week_meters()` in
`build_dashboard.py` — the spot to edit is clearly marked by the `_first_number`
and `_distance_meters` helpers.

---

## 4. Host it (GitHub Actions + Pages)

The included workflow `.github/workflows/dashboard.yml` rebuilds every 30 minutes
and publishes `backend/public/` to GitHub Pages.

1. Push this repo to GitHub.
2. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
3. **Settings → Secrets and variables → Actions → New repository secret**, add
   the same names as in `.env`:
   - `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN_JIAREN`
   - `FINALSURGE_EMAIL_RUBY`, `FINALSURGE_PASSWORD_RUBY`
4. Run the workflow once from the **Actions** tab (or push a commit).

Your public URL will be:

```
https://<your-username>.github.io/<repo-name>/dashboard.json
```

That is the `DASHBOARD_URL` the badge uses. (No auth — the file only contains
names + mileage totals.)

> Prefer not to use Pages? Any public static host works: a GitHub Gist "raw" URL,
> Netlify, S3, etc. Just publish `dashboard.json` somewhere the badge can GET.

---

## 5. Point the badge at it

On the badge's `secrets.py` (see the app deploy steps in `../DEPLOY_BADGE.md`):

```python
DASHBOARD_URL = "https://<your-username>.github.io/<repo-name>/dashboard.json"
DIST_UNITS = "mi"
```

If `DASHBOARD_URL` is unset the app still runs — it shows demo mileage plus live
weather/air-quality and a "Demo" footer.

---

## Files

| file | purpose |
|------|---------|
| `build_dashboard.py` | the aggregator (Strava + Final Surge + manual). |
| `strava_setup.py` | one-time helper to mint a Strava refresh token. |
| `config.example.json` | template config (copy to `config.json`). |
| `.env.example` | template secrets (copy to `.env`). |
| `requirements.txt` | just `requests`. |
| `public/` | published directory; `dashboard.json` is written here. |

## Notes & limitations

- **Garmin** is intentionally not used: its Activity API is partner-gated and not
  practical for a personal project. Use Strava (Garmin can auto-sync to Strava).
- Distances are converted to your `units`. Strava is always meters; Final Surge
  is assumed meters unless a unit field is present.
- "This week" starts **Monday 00:00** in your configured timezone.

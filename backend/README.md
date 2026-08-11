# Badger Training Dashboard — backend

This builds a tiny `dashboard.json` that your **Badger** "runlog" app fetches
over WiFi to show, for **Ruby** and **Jiaren**, weekly **planned vs. actual
running mileage** — the current week plus several upcoming weeks you can page
through on the badge. Weather, air quality, and the next-hour rain / air-quality
outlook come straight from the badge itself (Open-Meteo, no key), so the backend
is only responsible for the running numbers.

```
Garmin (actual) + Final Surge (planned) ──> build_dashboard.py ──> dashboard.json ──> GitHub Pages
                                              (GitHub Action, every 30 min)              │
                                                                                        ▼
                                                                       Badger "runlog" app (WiFi)
```

**How the numbers are sourced**

- **ACTUAL** mileage comes from each person's own **Garmin Connect** account
  (this week only; future weeks have no actual yet).
- **PLANNED** mileage comes from each person's own **Final Surge** training plan,
  per week. Any week Final Surge doesn't cover — no account, or a week not yet in
  the plan — is filled from the public **Google Sheet** training plan when the
  person has a `plan` (`"full"`/`"half"`) configured. Weeks neither source covers
  are `0`.

The published file looks like:

```json
{
  "week_start": "2025-08-04",
  "units": "mi",
  "updated": "2025-08-06T22:00:00Z",
  "names": ["Ruby", "Jiaren"],
  "weeks": [
    {"start": "2025-08-04", "planned": [35.0, 42.0], "actual": [23.6, 18.3]},
    {"start": "2025-08-11", "planned": [38.0, 26.2], "actual": [0.0, 0.0]}
  ],
  "people": [
    {"name": "Ruby",   "planned": 35.0, "actual": 23.6},
    {"name": "Jiaren", "planned": 42.0, "actual": 18.3}
  ]
}
```

`names[i]` lines up with `weeks[].planned[i]` and `weeks[].actual[i]`. The
`weeks[0]` entry is the current week; the rest are upcoming (planned-only). The
legacy top-level `people` block is the current week again, kept so an older badge
app still renders.

---

## 1. Configure who to track

Copy the example and edit names / env-var names:

```bash
cp config.example.json config.json
```

`config.json` (safe to commit — it holds **no secrets**, only the *names* of the
environment variables that do):

| field | meaning |
|-------|---------|
| `units` | `"mi"` or `"km"` |
| `timezone` | IANA name, e.g. `America/Los_Angeles`. Defines when the week rolls over (Monday 00:00). |
| `output` | where to write the JSON (leave as `public/dashboard.json`). |
| `lookahead_weeks` | how many upcoming weeks to emit beyond the current one (default `8`). |
| `people[].name` | display name (order matters — first shows on top of the badge). |
| `people[].garmin_tokens_env` | env var holding that person's Garmin token blob (preferred). |
| `people[].garmin_email_env` / `garmin_password_env` | optional Garmin login fallback. |
| `people[].finalsurge_email_env` / `finalsurge_password_env` | optional Final Surge login for PLANNED mileage. |
| `people[].plan` | optional `"full"`/`"half"` — fills weeks Final Surge doesn't cover from the Google Sheet plan. |

Every person uses **Garmin for actual** and **Final Surge for planned** (with the
**Google Sheet** plan filling any uncovered weeks) — there
is no per-person `source` field anymore.

---

## 2. Provide credentials (local dev)

```bash
cp .env.example .env      # then fill it in; .env is git-ignored
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Garmin — ACTUAL mileage (once per person)

Garmin's official Activity API needs partner approval, so this uses the
unofficial `garminconnect` library (same login you use on connect.garmin.com).
Logging in fresh on every CI run can trip MFA / bot checks, so mint a **token
blob once per person** and store that instead:

```bash
python garmin_setup.py          # log in as Ruby;   enter RUBY when prompted
python garmin_setup.py          # log in as Jiaren; enter JIAREN when prompted
```

Copy each printed value into `.env` as `GARMIN_TOKENS_BASE64_RUBY` and
`GARMIN_TOKENS_BASE64_JIAREN` (and later into GitHub secrets). The build resumes
those sessions with no password and no MFA; Garmin tokens refresh themselves and
last ~a year. Re-run `garmin_setup.py` if one ever expires.

*Local-only fallback:* skip the token and set `GARMIN_EMAIL_RUBY` /
`GARMIN_PASSWORD_RUBY` (and the `_JIAREN` pair) instead, but that may prompt MFA
and isn't reliable in CI.

### Final Surge — PLANNED mileage (optional, once per person)

Just the account email + password in `.env`
(`FINALSURGE_EMAIL_RUBY` / `FINALSURGE_PASSWORD_RUBY`, and the `_JIAREN` pair).
The script logs in to `beta.finalsurge.com` and reads each week's planned
distance. **If you skip this for a person, weeks are filled from the Google Sheet
plan below (or `0` if they have no `plan`).**

### Google Sheet — PLANNED fallback (no login needed)

Give a person a `plan` of `"full"` or `"half"` in `config.json` and any week
Final Surge doesn't cover is read from the public Fleet Feet Fall 2026 Google
Sheet (exported as CSV, no auth). The weekly cell is a range (e.g. `~ 19-23`);
`plan_sheet.target` picks `"low"`, `"high"`, or `"mid"` (default). Override the
sheet id, tab gids, `target`, or `year` via a top-level `plan_sheet` block —
see `config.example.json`. Sensible defaults are baked in, so `plan` alone works.

---

## 3. Run it

```bash
python build_dashboard.py            # real build -> public/dashboard.json
python build_dashboard.py --demo     # sample multi-week data, no network
```

> The build auto-loads `backend/.env` (if present) for local runs. In GitHub
> Actions the same variables come from repository secrets and take precedence.

### Verify the Final Surge field mapping (recommended, one time)

Final Surge's unofficial API isn't formally documented, so distance field
names can vary between accounts. Dump one real workout to confirm the mapping:

```bash
python build_dashboard.py --debug-finalsurge "Ruby"
```

Look at the printed JSON for where planned distance and its unit live. The parser
tries the common keys (`Planned.Distance` + `DistanceUnit`, etc.) and assumes
**meters** when no unit is given. If your account uses different keys, adjust the
`_planned_meters()` / `_first_number()` / `_distance_meters()` helpers in
`build_dashboard.py`.

---

## 4. Host it (GitHub Actions + Pages)

The included workflow `.github/workflows/dashboard.yml` rebuilds every 30 minutes
and publishes `backend/public/` to GitHub Pages.

1. Push this repo to GitHub.
2. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
3. **Settings → Secrets and variables → Actions → New repository secret**, add
   the names your `config.json` references:
   - Garmin (preferred): `GARMIN_TOKENS_BASE64_RUBY`, `GARMIN_TOKENS_BASE64_JIAREN`
   - Garmin (fallback): `GARMIN_EMAIL_RUBY` + `GARMIN_PASSWORD_RUBY`, and the `_JIAREN` pair
   - Final Surge (optional): `FINALSURGE_EMAIL_RUBY` + `FINALSURGE_PASSWORD_RUBY`, and the `_JIAREN` pair
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
| `build_dashboard.py` | the aggregator (Garmin actual + Final Surge planned, multi-week). |
| `garmin_setup.py` | one-time helper to mint a per-person Garmin token blob. |
| `config.example.json` | template config (copy to `config.json`). |
| `.env.example` | template secrets (copy to `.env`). |
| `requirements.txt` | `requests` + `garminconnect`. |
| `public/` | published directory; `dashboard.json` is written here. |

## Notes & limitations

- **Garmin** uses the unofficial `garminconnect` login (the official Activity API
  is partner-gated). Mint a token per person with `garmin_setup.py` so CI never
  needs a password or an MFA code.
- **Final Surge** is optional per person; any week it doesn't cover is filled
  from the **Google Sheet** plan (if the person has a `plan`), else `0`.
- Distances are converted to your `units`. Garmin is always meters; Final Surge
  is assumed meters unless a unit field is present.
- Only the **current** week has actual mileage; upcoming weeks are planned-only.
- "This week" starts **Monday 00:00** in your configured timezone.

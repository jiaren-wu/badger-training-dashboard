# Badger Training Dashboard

A custom app for the **GitHub Universe "Badger" badge** that shows, for **Ruby**
and **Jiaren**:

- This week's **planned vs. actual running mileage** + **%** (week starts Monday)
- Live **weather** and **air quality** (Open-Meteo, no API key)
- A **next-hour outlook**: chance of **rain** and whether **air quality** is about
  to get worse — answering "will it rain / will the air get bad in the next hour?"

Weather / air-quality / next-hour data run **directly on the badge**. The running
numbers come from a tiny `dashboard.json` built by the backend from **Garmin**
(actual) and **Final Surge** + a public **Google Sheet** training plan (planned),
published to a public URL the badge fetches over WiFi.

```
Garmin / Final Surge / Google Sheet ─▶ backend/build_dashboard.py ─▶ dashboard.json ─▶ GitHub Pages
   (GitHub Action, every 30 min)                                            │
                                                                            ▼
Open-Meteo ───────────────────────────────────────▶  Badger "runlog" app (WiFi)
   (weather · AQI · next-hour rain/AQI, on-device)
```

## Repo map

| path | what it is |
|------|-----------|
| `badge-app/runlog/` | **The badge app** (canonical copy). `__init__.py` + `icon.png`. |
| `backend/` | The `dashboard.json` builder (Garmin + Final Surge + Google Sheet plan) and its docs. |
| `.github/workflows/dashboard.yml` | Scheduled GitHub Action that builds + publishes the JSON. |
| `DEPLOY_BADGE.md` | Step-by-step to install the app on the badge (disk mode). |
| `home/` | Vendored upstream `badger/home` firmware — reference + simulator only. Not part of this project's build; git-ignored. |

## Status / what's next

- ✅ **Badge app** — built, simulator-verified, and **installed on the badge**
  (weather + AQI + next-hour rain/AQI are live now; mileage is demo until the
  backend is hosted).
- ⏭️ **Host the backend** so the mileage bars go live:
  1. `backend/README.md` → mint each runner's Garmin token, optionally add Final
     Surge logins, push this repo, enable **Pages** (Actions source), add the
     repository **secrets**.
  2. Uncomment `DASHBOARD_URL` in the badge's `secrets.py`
     (`https://jiaren-wu.github.io/badger-training-dashboard/dashboard.json`).

## Publish

This folder is a self-contained git repo (the vendored `home/` clone is ignored).
To publish:

```bash
git remote add origin git@github.com:jiaren-wu/badger-training-dashboard.git
git push -u origin main
```

Then follow `backend/README.md` for Pages + secrets.

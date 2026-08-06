#!/usr/bin/env python3
"""One-time helper to obtain a Strava refresh token for a person.

Strava requires a browser consent the first time. Run this locally:

    pip install requests
    export STRAVA_CLIENT_ID=xxxxx
    export STRAVA_CLIENT_SECRET=xxxxx
    python strava_setup.py

Steps it walks you through:
  1. Opens (prints) an authorize URL. Approve it in your browser.
  2. Strava redirects to http://localhost/exchange_token?code=XXXX (the page
     will fail to load - that's fine). Copy the `code` value from the URL.
  3. Paste it here. The script exchanges it for a refresh_token and prints it.

Put the printed refresh_token into your .env (e.g. STRAVA_REFRESH_TOKEN_JIAREN)
or a GitHub Actions secret. Refresh tokens are long-lived; the build script
uses them to mint short-lived access tokens automatically.
"""
import os
import sys
import webbrowser

import requests

CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID")
CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET")


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET first.", file=sys.stderr)
        sys.exit(1)

    # activity:read_all covers private activities too.
    authorize = (
        "https://www.strava.com/oauth/authorize"
        "?client_id=%s"
        "&response_type=code"
        "&redirect_uri=http://localhost/exchange_token"
        "&approval_prompt=force"
        "&scope=activity:read_all" % CLIENT_ID
    )
    print("\n1) Open this URL and click Authorize:\n")
    print(authorize + "\n")
    try:
        webbrowser.open(authorize)
    except Exception:
        pass

    print("2) After approving, your browser goes to a localhost URL that fails "
          "to load.\n   Copy the value of `code` from that URL's query string.\n")
    code = input("Paste the code here: ").strip()

    r = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
    }, timeout=30)
    r.raise_for_status()
    tok = r.json()
    print("\nSUCCESS. Save this refresh token as a secret:\n")
    print("  refresh_token =", tok["refresh_token"])
    print("\n(athlete: %s %s)" % (
        tok.get("athlete", {}).get("firstname", ""),
        tok.get("athlete", {}).get("lastname", "")))


if __name__ == "__main__":
    main()

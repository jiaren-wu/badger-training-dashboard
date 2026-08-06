#!/usr/bin/env python3
"""One-time helper to create a Garmin Connect token blob for CI.

Logging in with an email/password on every CI run can trigger MFA and Garmin's
bot protection. Instead, log in ONCE locally with this script; it prints a
base64 token blob you store as the GARMIN_TOKENS_BASE64 secret. The build script
then resumes that session with no password and no MFA. Garmin tokens refresh
themselves and last roughly a year.

Usage:
    pip install garminconnect
    python garmin_setup.py
    # enter email, password, and MFA code if prompted
    # copy the printed GARMIN_TOKENS_BASE64 value into .env / GitHub secrets

Re-run this if the token ever expires or is revoked.
"""
import getpass
import sys

from garminconnect import Garmin


def main():
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")

    # prompt_mfa is called only if Garmin requires a code this session.
    def prompt_mfa():
        return input("MFA code (from your authenticator/email): ").strip()

    g = Garmin(email, password, prompt_mfa=prompt_mfa)
    try:
        g.login()
    except Exception as e:
        print("Login failed:", e, file=sys.stderr)
        sys.exit(1)

    token_b64 = g.client.dumps()  # base64 blob understood by Garmin.login(tokenstore=...)
    print("\nSUCCESS. Store this as the secret GARMIN_TOKENS_BASE64:\n")
    print(token_b64)
    print("\n(length: %d chars)" % len(token_b64))


if __name__ == "__main__":
    main()

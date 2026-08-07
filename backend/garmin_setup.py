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

try:
    from garminconnect import Garmin
except ModuleNotFoundError:
    sys.exit(
        "\ngarminconnect isn't installed for THIS python.\n"
        "Use the project virtualenv instead:\n\n"
        "  ~/.badger-venv/bin/python garmin_setup.py\n\n"
        "(one-time setup, if that venv doesn't exist yet:)\n"
        "  python3 -m venv ~/.badger-venv\n"
        "  ~/.badger-venv/bin/python -m pip install -r requirements.txt\n"
    )


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
    who = input("\nWhose token is this? (e.g. RUBY or JIAREN, blank to skip): ").strip().upper()
    secret_name = "GARMIN_TOKENS_BASE64_%s" % who if who else "GARMIN_TOKENS_BASE64"
    print("\nSUCCESS. Store this as the secret %s:\n" % secret_name)
    print(token_b64)
    print("\n(length: %d chars)" % len(token_b64))
    print("Run this script again logged in as the OTHER person for their token.")


if __name__ == "__main__":
    main()

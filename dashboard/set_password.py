"""Set the Mission Control dashboard password.

Usage:
    python3 set_password.py
Prompts for a password (twice), prints a bcrypt hash and the exact
`export` line to put in the dashboard's environment. The dashboard reads
the hash from the DASHBOARD_PASSWORD environment variable and compares
with bcrypt — the plain password is never stored anywhere.
"""
import getpass
import bcrypt

pw1 = getpass.getpass("New dashboard password: ")
pw2 = getpass.getpass("Confirm password: ")
if pw1 != pw2:
    raise SystemExit("Passwords do not match — aborting.")
if len(pw1) < 8:
    raise SystemExit("Use at least 8 characters.")
h = bcrypt.hashpw(pw1.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
print("\nAdd this to the dashboard's environment (never commit it):\n")
print("export DASHBOARD_PASSWORD='%s'" % h)
print("\nThen restart the dashboard. Log in as user: tahir")

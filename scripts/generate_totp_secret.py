"""One-time TOTP enrollment for the ops dashboard (audit F-08).

Run once, anywhere with this repo checked out:

    python scripts/generate_totp_secret.py

Then:
  1. Add the printed secret to the deployment env as ``OPS_TOTP_SECRET``
     (and to the owner's password manager / continuity pack).
  2. Add the account to an authenticator app — either scan a QR encoding
     the printed otpauth:// URI (any "text to QR" tool, offline preferred)
     or type the secret in manually.
  3. Redeploy ops.  The login form now requires the 6-digit code.

Losing the phone: the secret in the password manager re-enrolls a new
device; rotating = run this script again and replace the env var.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.totp import generate_secret, provisioning_uri, totp_code  # noqa: E402


def main() -> None:
    secret = generate_secret()
    print("OPS_TOTP_SECRET (base32) — store in env + password manager:\n")
    print(f"  {secret}\n")
    print("Authenticator enrollment URI (encode as QR or enter secret manually):\n")
    print(f"  {provisioning_uri(secret)}\n")
    print(f"Sanity check — a code valid right now: {totp_code(secret)}")


if __name__ == "__main__":
    main()

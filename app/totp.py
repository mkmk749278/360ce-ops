"""TOTP second factor for the owner login (audit F-08, 2026-07-10).

The ops dashboard guards the kill switch, live/paper mode flips, and (via
the diag runner's docker.sock) the VPS host itself — a single static
password was the whole gate.  This module adds an RFC 6238 TOTP check to
both login paths (web form + ``/api/v1/auth/login``), compatible with any
authenticator app (Google Authenticator, Aegis, 1Password, ...).

Design:

* **Stdlib only** — HMAC-SHA1 per RFC 4226/6238, no new dependency (same
  reasoning as the engine's stdlib JWT).
* **Opt-in via ``OPS_TOTP_SECRET``** (base32).  Unset → the gate is
  password-only, exactly as before, and ``TotpGate.enabled`` is False so
  the login form hides the code field.  This keeps existing deployments
  working until the owner enrolls; enrollment is one env var + one QR scan
  (``python scripts/generate_totp_secret.py``).
* **±1 step clock tolerance** (90s total) — standard allowance for phone
  clock drift.
* **Replay protection** — a code is accepted once; the matched time-step
  is remembered and codes at or before it are rejected.  In-memory,
  single-process (uvicorn), which matches how the dashboard deploys.
* **Constant-time comparison** throughout.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import threading
import time
from typing import Optional

TOTP_STEP_SEC = 30
TOTP_DIGITS = 6
TOTP_DRIFT_STEPS = 1  # accept current step ±1


def generate_secret(num_bytes: int = 20) -> str:
    """Fresh base32 TOTP secret (RFC 4226 recommends 160 bits)."""
    return base64.b32encode(secrets.token_bytes(num_bytes)).decode("ascii")


def provisioning_uri(secret_b32: str, *, account: str = "owner", issuer: str = "360 CE Ops") -> str:
    """otpauth:// URI for authenticator-app enrollment (QR-encodable)."""
    from urllib.parse import quote

    label = f"{quote(issuer)}:{quote(account)}"
    return (
        f"otpauth://totp/{label}?secret={secret_b32}"
        f"&issuer={quote(issuer)}&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_SEC}"
    )


def _hotp(key: bytes, counter: int) -> str:
    """RFC 4226 HOTP value (6 digits, HMAC-SHA1)."""
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10**TOTP_DIGITS)).zfill(TOTP_DIGITS)


def _decode_secret(secret_b32: str) -> bytes:
    # Tolerate lowercase / missing padding — authenticator exports vary.
    normalized = secret_b32.strip().replace(" ", "").upper()
    normalized += "=" * (-len(normalized) % 8)
    return base64.b32decode(normalized)


def totp_code(secret_b32: str, at_time: Optional[float] = None) -> str:
    """Current TOTP code — used by tests and the enrollment script."""
    ts = time.time() if at_time is None else at_time
    return _hotp(_decode_secret(secret_b32), int(ts) // TOTP_STEP_SEC)


def match_step(
    secret_b32: str,
    code: str,
    at_time: Optional[float] = None,
    drift_steps: int = TOTP_DRIFT_STEPS,
) -> Optional[int]:
    """Return the matched time-step for ``code`` within ±drift, else None."""
    candidate = (code or "").strip().replace(" ", "")
    if len(candidate) != TOTP_DIGITS or not candidate.isdigit():
        return None
    ts = time.time() if at_time is None else at_time
    step_now = int(ts) // TOTP_STEP_SEC
    key = _decode_secret(secret_b32)
    for step in range(step_now - drift_steps, step_now + drift_steps + 1):
        if hmac.compare_digest(_hotp(key, step), candidate):
            return step
    return None


class TotpGate:
    """Login-time TOTP verifier with replay protection.

    ``verify()`` is what the login routes call: True when the second factor
    passes (or when the gate is disabled — password-only mode).
    """

    def __init__(self, secret_b32: str) -> None:
        self._secret = (secret_b32 or "").strip()
        self._last_step_used = -1
        self._lock = threading.Lock()
        if self._secret:
            # Fail fast on a malformed secret at boot, not at first login.
            _decode_secret(self._secret)

    @property
    def enabled(self) -> bool:
        return bool(self._secret)

    def verify(self, code: str, at_time: Optional[float] = None) -> bool:
        if not self._secret:
            return True
        step = match_step(self._secret, code, at_time=at_time)
        if step is None:
            return False
        with self._lock:
            if step <= self._last_step_used:
                return False  # replay of an already-consumed code
            self._last_step_used = step
        return True

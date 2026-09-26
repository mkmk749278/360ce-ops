"""Both owner logins answered 500 to a non-ASCII password (2026-09-26 audit).

``hmac.compare_digest`` raises ``TypeError`` when either ``str`` holds a
non-ASCII character, so ``POST /login`` and ``POST /api/v1/auth/login`` crashed
on "pässwörd" instead of refusing it — and the TOTP check did the same for
full-width digits ("１２３４５６"), which ``str.isdigit()`` accepts. The gate
still held (a crash grants nothing), but the unauthenticated surface of the
control plane answered with an unhandled exception, and a phone keyboard that
autocorrects to an accented character got "Internal Server Error" instead of
"Invalid credentials".
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.totp import TotpGate, match_step, totp_code

_SECRET = "JBSWY3DPEHPK3PXP"


@pytest.mark.parametrize("password", ["pässwörd", "test-tokén", "密码", "test-token​"])
def test_web_login_refuses_a_non_ascii_password(password) -> None:
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post("/login", data={"password": password}, follow_redirects=False)
    assert r.status_code == 401
    assert "Invalid credentials" in r.text


@pytest.mark.parametrize("password", ["pässwörd", "密码"])
def test_app_login_refuses_a_non_ascii_password(password) -> None:
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post("/api/v1/auth/login", json={"password": password})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid credentials"


def test_the_right_password_still_logs_in() -> None:
    """Control: the fix must not break the ASCII path it sits on."""
    with TestClient(app) as client:
        r = client.post("/login", data={"password": "test-token"}, follow_redirects=False)
        assert r.status_code == 302
        r = client.post("/api/v1/auth/login", json={"password": "test-token"})
        assert r.status_code == 200 and r.json()["token"]


@pytest.mark.parametrize("code", ["１２３４５６", "١٢٣٤٥٦", "12345é", "۱۲۳۴۵۶"])
def test_totp_refuses_non_ascii_digits_instead_of_raising(code) -> None:
    assert match_step(_SECRET, code, at_time=1_000_000.0) is None
    assert TotpGate(_SECRET).verify(code) is False


def test_totp_still_accepts_the_real_code() -> None:
    now = 1_000_000.0
    assert match_step(_SECRET, totp_code(_SECRET, now), at_time=now) is not None

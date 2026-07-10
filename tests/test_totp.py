"""TOTP second factor (app/totp.py + both login paths) — audit F-08."""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app.totp import (
    TotpGate,
    generate_secret,
    match_step,
    provisioning_uri,
    totp_code,
)

# RFC 6238 Appendix B test vectors (SHA1, 8 digits truncated to our 6 by
# taking the last 6 — instead we verify against the full algorithm with a
# known secret/time pair computed from the reference implementation).
RFC_SECRET_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890"


class TestAlgorithm:
    def test_rfc6238_vector_t59(self):
        # RFC 6238: T=59s, SHA1 → 8-digit 94287082 → 6-digit suffix 287082.
        assert totp_code(RFC_SECRET_B32, at_time=59) == "287082"

    def test_rfc6238_vector_t1111111109(self):
        # RFC 6238: 07081804 → 081804.
        assert totp_code(RFC_SECRET_B32, at_time=1111111109) == "081804"

    def test_code_changes_across_steps(self):
        assert totp_code(RFC_SECRET_B32, at_time=0) != totp_code(RFC_SECRET_B32, at_time=31)

    def test_secret_normalisation(self):
        # lowercase + spaces + stripped padding all decode identically.
        messy = RFC_SECRET_B32.lower()[:16] + " " + RFC_SECRET_B32[16:]
        assert totp_code(messy, at_time=59) == "287082"


class TestMatchStep:
    def test_current_code_matches(self):
        assert match_step(RFC_SECRET_B32, "287082", at_time=59) is not None

    def test_previous_step_within_drift(self):
        code_at_59 = totp_code(RFC_SECRET_B32, at_time=59)
        assert match_step(RFC_SECRET_B32, code_at_59, at_time=61) is not None

    def test_two_steps_out_rejected(self):
        code_at_0 = totp_code(RFC_SECRET_B32, at_time=0)
        # at_time=90 → step 3; code from step 1 is outside ±1.
        assert match_step(RFC_SECRET_B32, code_at_0, at_time=95) is None

    @pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", "12 34"])
    def test_malformed_codes_rejected(self, bad):
        assert match_step(RFC_SECRET_B32, bad, at_time=59) is None


class TestTotpGate:
    def test_disabled_gate_accepts_anything(self):
        gate = TotpGate("")
        assert not gate.enabled
        assert gate.verify("")
        assert gate.verify("000000")

    def test_enabled_gate_verifies(self):
        gate = TotpGate(RFC_SECRET_B32)
        assert gate.enabled
        assert gate.verify(totp_code(RFC_SECRET_B32, at_time=59), at_time=59)
        assert not gate.verify("000000", at_time=59)

    def test_replay_rejected(self):
        gate = TotpGate(RFC_SECRET_B32)
        code = totp_code(RFC_SECRET_B32, at_time=59)
        assert gate.verify(code, at_time=59)
        assert not gate.verify(code, at_time=60)  # same code, same step → replay

    def test_next_step_accepted_after_replay_block(self):
        gate = TotpGate(RFC_SECRET_B32)
        assert gate.verify(totp_code(RFC_SECRET_B32, at_time=59), at_time=59)
        assert gate.verify(totp_code(RFC_SECRET_B32, at_time=95), at_time=95)

    def test_malformed_secret_fails_at_boot(self):
        with pytest.raises(Exception):
            TotpGate("not-base32-!!!")


class TestEnrollmentHelpers:
    def test_generate_secret_is_valid_base32(self):
        secret = generate_secret()
        assert totp_code(secret)  # decodes + produces a code

    def test_provisioning_uri_shape(self):
        uri = provisioning_uri(RFC_SECRET_B32)
        assert uri.startswith("otpauth://totp/")
        assert f"secret={RFC_SECRET_B32}" in uri
        assert "period=30" in uri


# ---------------------------------------------------------------------------
# Login-path integration (web form + /api/v1)
# ---------------------------------------------------------------------------


@pytest.fixture()
def totp_client(monkeypatch):
    """App with TOTP enabled (fresh import so lifespan picks up the env)."""
    monkeypatch.setenv("OPS_TOTP_SECRET", RFC_SECRET_B32)
    import app.config as config_mod
    import app.main as main_mod

    importlib.reload(config_mod)
    importlib.reload(main_mod)
    with TestClient(main_mod.app) as client:
        yield client
    monkeypatch.delenv("OPS_TOTP_SECRET", raising=False)
    importlib.reload(config_mod)
    importlib.reload(main_mod)


class TestLoginPaths:
    def test_web_login_requires_totp_when_enabled(self, totp_client):
        r = totp_client.post(
            "/login", data={"password": "test-token"}, follow_redirects=False
        )
        assert r.status_code == 401

    def test_web_login_rejects_wrong_totp(self, totp_client):
        r = totp_client.post(
            "/login",
            data={"password": "test-token", "totp": "000000"},
            follow_redirects=False,
        )
        assert r.status_code == 401

    def test_web_login_accepts_password_plus_totp(self, totp_client):
        r = totp_client.post(
            "/login",
            data={"password": "test-token", "totp": totp_code(RFC_SECRET_B32)},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["location"] == "/"

    def test_web_login_form_shows_totp_field(self, totp_client):
        r = totp_client.get("/login")
        assert 'name="totp"' in r.text

    def test_api_v1_login_requires_totp(self, totp_client):
        r = totp_client.post(
            "/api/v1/auth/login", json={"password": "test-token"}
        )
        assert r.status_code == 401

    def test_api_v1_login_accepts_totp(self, totp_client):
        r = totp_client.post(
            "/api/v1/auth/login",
            json={"password": "test-token", "totp": totp_code(RFC_SECRET_B32)},
        )
        assert r.status_code == 200
        assert r.json()["token"]

    def test_api_v1_wrong_password_right_totp_rejected(self, totp_client):
        r = totp_client.post(
            "/api/v1/auth/login",
            json={"password": "nope", "totp": totp_code(RFC_SECRET_B32)},
        )
        assert r.status_code == 401

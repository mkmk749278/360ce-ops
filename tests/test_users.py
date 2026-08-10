"""Tests for the manual tier-grant route (ops' second write surface).

Covers auth gate, the lookup page (found / not-found / error), and the
grant POST (success, invalid tier, audit-log round trip) with the engine
client monkeypatched — mirrors test_control.py's pattern.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import users as users_route  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


def test_users_page_requires_auth():
    with TestClient(app) as client:
        r = client.get("/control/users", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


def test_users_page_blank_shows_lookup_form_only():
    with TestClient(app) as client:
        _login(client)
        r = client.get("/control/users")
        assert r.status_code == 200
        assert "Look up user" in r.text


def test_users_lookup_found_renders_tier_and_grant_form(monkeypatch):
    async def fake_lookup(self, phone):
        return {
            "user_id": 7,
            "phone": phone,
            "tier": "free",
            "paid_until": None,
            "display_name": "Test User",
            "onboarded": True,
        }

    monkeypatch.setattr(EngineApiClient, "user_lookup", fake_lookup)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/control/users", params={"phone": "+15551230000"})
        assert r.status_code == 200
        assert "Test User" in r.text
        assert "FREE" in r.text
        assert "/control/users/grant" in r.text


def test_users_lookup_not_found(monkeypatch):
    async def fake_lookup(self, phone):
        return {"error": "no user with phone +19998887777", "status_code": 404}

    monkeypatch.setattr(EngineApiClient, "user_lookup", fake_lookup)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/control/users", params={"phone": "+19998887777"})
        assert r.status_code == 200
        assert "No user found" in r.text


def test_users_lookup_form_redirects_with_phone():
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/users/lookup",
            data={"phone": "+15551230000"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        # "+" is percent-encoded — Starlette's query parser decodes a literal
        # "+" back to a space (form-urlencoded convention), so an unencoded
        # "+" in the Location header would corrupt the phone on the very
        # next GET.
        assert r.headers["location"] == "/control/users?phone=%2B15551230000"


def test_users_lookup_raw_digits_normalize_with_selected_country_code():
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/users/lookup",
            data={"phone": "9618579123", "country_code": "91"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/control/users?phone=%2B919618579123"


def test_users_lookup_raw_digits_default_to_india_when_country_omitted():
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/users/lookup",
            data={"phone": "9618579123"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/control/users?phone=%2B919618579123"


def test_users_lookup_pasted_plus_number_ignores_country_select():
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/users/lookup",
            data={"phone": "+15551230000", "country_code": "91"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/control/users?phone=%2B15551230000"


def test_users_lookup_round_trip_calls_engine_with_normalized_e164(monkeypatch):
    """Reproduces the live bug: typing raw digits (no +) into the lookup
    form must reach the engine as a proper E.164 number, end to end through
    the POST -> redirect -> GET round trip — not just at the redirect-URL
    level."""
    seen: dict = {}

    async def fake_lookup(self, phone):
        seen["phone"] = phone
        return {
            "user_id": 1,
            "phone": phone,
            "tier": "owner",
            "paid_until": None,
            "display_name": "Kishore",
            "onboarded": True,
        }

    monkeypatch.setattr(EngineApiClient, "user_lookup", fake_lookup)
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/users/lookup",
            data={"phone": "9618579123", "country_code": "91"},
        )
        assert r.status_code == 200
        assert seen["phone"] == "+919618579123"
        assert "Kishore" in r.text


def test_users_page_blank_shows_india_default_in_country_select():
    with TestClient(app) as client:
        _login(client)
        r = client.get("/control/users")
        assert r.status_code == 200
        assert '<option value="91" selected' in r.text
        assert "India" in r.text


def test_grant_calls_engine_and_audits(monkeypatch):
    calls: dict = {}

    async def fake_grant(self, phone, tier, duration_days=None, reason=None):
        calls["phone"] = phone
        calls["tier"] = tier
        calls["duration_days"] = duration_days
        calls["reason"] = reason
        return {"ok": True, "user_id": 7, "phone": phone, "tier": tier,
                "paid_until": "2026-07-27T00:00:00+00:00"}

    recorded: list = []
    monkeypatch.setattr(EngineApiClient, "grant_tier", fake_grant)
    monkeypatch.setattr(
        users_route.audit, "record",
        lambda *a, **k: recorded.append(k),
    )

    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/users/grant",
            data={
                "phone": "+15551230000",
                "tier": "auto",
                "duration_days": "30",
                "reason": "influencer comp",
            },
        )
        assert r.status_code == 200  # followed the 303 back to /control/users
        assert calls == {
            "phone": "+15551230000",
            "tier": "auto",
            "duration_days": 30,
            "reason": "influencer comp",
        }
        assert recorded[0]["action"] == "grant_tier"
        assert recorded[0]["ok"] is True
        assert "granted AUTO" in r.text


def test_grant_invalid_tier_rejected_without_engine_call(monkeypatch):
    called = {"grant": False}

    async def fake_grant(self, phone, tier, duration_days=None, reason=None):
        called["grant"] = True
        return {"ok": True}

    monkeypatch.setattr(EngineApiClient, "grant_tier", fake_grant)
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/users/grant",
            data={"phone": "+15551230000", "tier": "owner"},
        )
        assert r.status_code == 200
        assert called["grant"] is False
        assert "invalid tier" in r.text.lower()


def test_grant_free_revokes(monkeypatch):
    async def fake_grant(self, phone, tier, duration_days=None, reason=None):
        return {"ok": True, "user_id": 7, "phone": phone, "tier": "free",
                "paid_until": None}

    monkeypatch.setattr(EngineApiClient, "grant_tier", fake_grant)
    monkeypatch.setattr(users_route.audit, "record", lambda *a, **k: None)

    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/users/grant",
            data={"phone": "+15551230000", "tier": "free"},
        )
        assert r.status_code == 200
        assert "revoked" in r.text.lower()


def test_grant_engine_error_surfaces_failure_flash(monkeypatch):
    async def fake_grant(self, phone, tier, duration_days=None, reason=None):
        return {"error": "no user with phone +15551230000", "status_code": 404}

    recorded: list = []
    monkeypatch.setattr(EngineApiClient, "grant_tier", fake_grant)
    monkeypatch.setattr(
        users_route.audit, "record",
        lambda *a, **k: recorded.append(k),
    )

    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/users/grant",
            data={"phone": "+15551230000", "tier": "auto"},
        )
        assert r.status_code == 200
        assert recorded[0]["ok"] is False
        assert "Grant failed" in r.text


# ---------------------------------------------------------------------------
# Exit mechanism — the one control on this page that changes how a REAL
# position closes, rather than what an account is entitled to.
# ---------------------------------------------------------------------------


def _lookup(monkeypatch, **over):
    row = {
        "user_id": 7, "phone": "+15551230000", "tier": "auto",
        "paid_until": None, "onboarded": True, "name": "Test User",
    }
    row.update(over)

    async def fake_lookup(self, phone):
        return dict(row, phone=phone)

    monkeypatch.setattr(EngineApiClient, "user_lookup", fake_lookup)


def _capture_set(monkeypatch, response):
    seen = {}

    async def fake_set(self, phone, exit_mechanism, reason=None):
        seen.update(phone=phone, exit_mechanism=exit_mechanism, reason=reason)
        return response

    monkeypatch.setattr(EngineApiClient, "set_exit_mechanism", fake_set)
    return seen


def test_exit_mechanism_form_renders_on_a_found_user(monkeypatch):
    _lookup(monkeypatch)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/control/users", params={"phone": "+15551230000"})
        assert "/control/users/exit-mechanism" in r.text
        assert "chandelier" in r.text
        # The page must say the per-user value is not sufficient on its own.
        assert "Two switches are required" in r.text


def test_setting_a_mechanism_with_the_master_switch_off_is_not_reported_as_running(
    monkeypatch,
):
    """A control that reports success for a state that does nothing is the
    same class as one that 403s — the flash has to name the missing half."""
    _lookup(monkeypatch)
    _capture_set(monkeypatch, {
        "ok": True, "user_id": 7, "phone": "+15551230000",
        "exit_mechanism": "sar", "governor_enabled": False,
    })
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/users/exit-mechanism",
            data={"phone": "+15551230000", "exit_mechanism": "sar"},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert "trail governor is OFF" in r.text
        assert "nothing changes until" in r.text


def test_setting_a_mechanism_with_the_master_switch_on_says_it_is_live(monkeypatch):
    _lookup(monkeypatch)
    _capture_set(monkeypatch, {
        "ok": True, "user_id": 7, "phone": "+15551230000",
        "exit_mechanism": "sar", "governor_enabled": True,
    })
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/users/exit-mechanism",
            data={"phone": "+15551230000", "exit_mechanism": "sar"},
            follow_redirects=True,
        )
        assert "master switch is ON" in r.text


def test_returning_to_default_is_phrased_as_the_unchanged_exit(monkeypatch):
    _lookup(monkeypatch)
    _capture_set(monkeypatch, {
        "ok": True, "user_id": 7, "phone": "+15551230000",
        "exit_mechanism": "default", "governor_enabled": True,
    })
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/users/exit-mechanism",
            data={"phone": "+15551230000", "exit_mechanism": "default"},
            follow_redirects=True,
        )
        assert "SL/TP FSM" in r.text


def test_an_unknown_mechanism_never_reaches_the_engine(monkeypatch):
    _lookup(monkeypatch)
    seen = _capture_set(monkeypatch, {"ok": True})
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/users/exit-mechanism",
            data={"phone": "+15551230000", "exit_mechanism": "parabolic"},
            follow_redirects=True,
        )
        assert "unknown exit mechanism" in r.text.lower()
    assert seen == {}, "ops forwarded a mechanism the engine never defined"


def test_the_engine_is_the_source_of_truth_for_what_was_stored(monkeypatch):
    """The flash reports what the ENGINE read back, not what was submitted —
    an echo would report success for a write the coercion layer dropped."""
    _lookup(monkeypatch)
    _capture_set(monkeypatch, {
        "ok": False, "user_id": 7, "phone": "+15551230000",
        "exit_mechanism": "default", "governor_enabled": True,
    })
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/users/exit-mechanism",
            data={"phone": "+15551230000", "exit_mechanism": "chandelier"},
            follow_redirects=True,
        )
        assert "CHANDELIER" not in r.text.replace("chandelier", "")


def test_the_change_is_audited(monkeypatch):
    _lookup(monkeypatch)
    _capture_set(monkeypatch, {
        "ok": True, "user_id": 7, "phone": "+15551230000",
        "exit_mechanism": "sar", "governor_enabled": True,
    })
    recorded: list = []
    monkeypatch.setattr(
        users_route.audit, "record", lambda *a, **k: recorded.append(k)
    )
    with TestClient(app) as client:
        _login(client)
        client.post(
            "/control/users/exit-mechanism",
            data={"phone": "+15551230000", "exit_mechanism": "sar",
                  "reason": "owner canary"},
            follow_redirects=True,
        )
    assert recorded, "money-path write was not audited"
    entry = recorded[0]
    assert entry["action"] == "set_exit_mechanism"
    assert entry["params"]["exit_mechanism"] == "sar"
    assert entry["params"]["reason"] == "owner canary"
    assert entry["ok"] is True

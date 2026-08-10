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


# ---------------------------------------------------------------------------
# The exit-mechanism card is a control over a money-path setting, so it has to
# show the state it is about to change.
#
# Owner screenshot 2026-08-10: he had handed his account to SAR, the flash
# confirmed it, and this card still rendered "default (SL/TP FSM — unchanged)"
# on every reload — a static three-option select with nothing selected, over a
# lookup payload that carried no such field. A write with no read-back, on the
# one control that decides how a real position closes.
# ---------------------------------------------------------------------------


def _lookup_returning(**extra):
    async def fake_lookup(self, phone):
        body = {
            "user_id": 1,
            "phone": phone,
            "tier": "auto",
            "paid_until": None,
            "display_name": "Kishore",
            "onboarded": True,
        }
        body.update(extra)
        return body

    return fake_lookup


def _card(monkeypatch, **extra) -> str:
    monkeypatch.setattr(EngineApiClient, "user_lookup", _lookup_returning(**extra))
    with TestClient(app) as client:
        _login(client)
        r = client.get("/control/users", params={"phone": "+919618579123"})
        assert r.status_code == 200
        return r.text


def _option_tag(html: str, value: str) -> str:
    """The whole `<option …>` tag for one value, attributes included.

    Asserting on the tag rather than on the word: the pre-fix page contained
    "sar" too — in an option nobody had chosen.
    """
    import re

    m = re.search(r"<option[^>]*value=\"%s\"[^>]*>" % re.escape(value), html)
    assert m, f"no option for {value!r}"
    return m.group(0)


def test_the_stored_mechanism_is_preselected_not_the_first_option(monkeypatch):
    """GUARD — the select must open on what is stored."""
    html = _card(monkeypatch, exit_mechanism="sar", governor_enabled=True)
    assert "selected" in _option_tag(html, "sar")
    assert "selected" not in _option_tag(html, "default")
    assert "selected" not in _option_tag(html, "chandelier")


def test_default_is_preselected_for_an_account_that_never_opted_in(monkeypatch):
    html = _card(monkeypatch, exit_mechanism="default", governor_enabled=True)
    assert "selected" in _option_tag(html, "default")
    assert "selected" not in _option_tag(html, "sar")


def test_a_mechanism_with_the_master_switch_on_reads_live(monkeypatch):
    html = _card(monkeypatch, exit_mechanism="sar", governor_enabled=True)
    assert "Exit mechanism now:" in html
    assert "SAR" in html and "LIVE" in html


def test_a_mechanism_with_the_master_switch_off_is_not_called_live(monkeypatch):
    """Set-but-inert and running are different states. Both switches are
    required, so a card showing only the per-user half would report a live
    mechanism over an account still exiting on the FSM."""
    html = _card(monkeypatch, exit_mechanism="sar", governor_enabled=False)
    assert "SET, NOT RUNNING" in html
    assert ">LIVE<" not in html


def test_default_says_the_fsm_owns_the_exit(monkeypatch):
    html = _card(monkeypatch, exit_mechanism="default", governor_enabled=True)
    assert "SET, NOT RUNNING" not in html
    assert ">LIVE<" not in html


def test_an_engine_that_does_not_report_the_field_says_so(monkeypatch):
    """An older engine must render "not reported", never "default".

    Jinja's attribute access yields Undefined for a missing key, which is
    neither none nor a mechanism — the pre-`.get` version of this template fell
    past both branches and printed the "set" wording with a blank name.
    """
    html = _card(monkeypatch)  # no exit_mechanism key at all
    assert "not reported" in html
    assert "SET, NOT RUNNING" not in html

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
        assert r.headers["location"] == "/control/users?phone=+15551230000"


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

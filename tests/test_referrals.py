"""Tests for the referral commission payout surface (Phase 2, 2026-07-21).

Engine client monkeypatched, per the control-plane test convention: we
assert the page renders the ledger + per-currency totals, the mark-paid
write calls the right engine method with the selected ids, records an
audit entry, and an empty selection never reaches the engine.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import referrals as referrals_route  # noqa: E402
from app.routes.referrals import _totals_by_currency  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


_ITEMS = [
    {
        "commission_id": 1,
        "referrer_id": 7,
        "referee_id": 9,
        "referrer_phone": "+919999999999",
        "product_id": "lumin_auto_monthly",
        "period_expiry": "2026-08-21T00:00:00+00:00",
        "amount": 1000.0,
        "currency": "INR",
        "rate": 0.5,
        "status": "accrued",
        "created_at": "2026-07-21T05:00:00+00:00",
        "paid_at": None,
    },
    {
        "commission_id": 2,
        "referrer_id": 7,
        "referee_id": 11,
        "referrer_phone": "+919999999999",
        "product_id": "web_auto",
        "period_expiry": "2026-08-20T00:00:00+00:00",
        "amount": 12.5,
        "currency": "USD",
        "rate": 0.5,
        "status": "paid",
        "created_at": "2026-07-20T05:00:00+00:00",
        "paid_at": "2026-07-21T00:00:00+00:00",
    },
]


def test_totals_group_by_currency_never_summing_across():
    totals = _totals_by_currency(_ITEMS)
    assert totals == [
        {"currency": "INR", "accrued": 1000.0, "paid": 0.0},
        {"currency": "USD", "accrued": 0.0, "paid": 12.5},
    ]


def test_referrals_page_requires_auth():
    with TestClient(app) as client:
        r = client.get("/control/referrals", follow_redirects=False)
        assert r.status_code in (302, 303, 307)


def test_referrals_page_renders_ledger(monkeypatch):
    async def fake_list(self, status=None):
        return {"items": _ITEMS}

    monkeypatch.setattr(EngineApiClient, "referral_commissions", fake_list)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/control/referrals")
        assert r.status_code == 200
        assert "+919999999999" in r.text
        assert "1000.00" in r.text
        assert "ACCRUED" in r.text and "PAID" in r.text


def test_mark_paid_calls_engine_and_audits(monkeypatch):
    calls: dict = {}

    async def fake_list(self, status=None):
        return {"items": []}

    async def fake_mark(self, commission_ids):
        calls["ids"] = commission_ids
        return {"ok": True, "updated": len(commission_ids)}

    recorded: list = []
    monkeypatch.setattr(EngineApiClient, "referral_commissions", fake_list)
    monkeypatch.setattr(
        EngineApiClient, "mark_referral_commissions_paid", fake_mark
    )
    monkeypatch.setattr(
        referrals_route.audit, "record", lambda *a, **k: recorded.append(k)
    )
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/referrals/mark-paid",
            data={"commission_ids": ["1", "3"]},
        )
        assert r.status_code == 200  # followed the 303 back to the page
        assert calls["ids"] == [1, 3]
        assert recorded and recorded[0]["action"] == "referral_commissions_mark_paid"
        assert recorded[0]["ok"] is True
        assert "Marked 2 commission row(s) paid." in r.text


def test_mark_paid_empty_selection_never_reaches_engine(monkeypatch):
    called = {"mark": False}

    async def fake_list(self, status=None):
        return {"items": []}

    async def fake_mark(self, commission_ids):
        called["mark"] = True
        return {"ok": True, "updated": 0}

    monkeypatch.setattr(EngineApiClient, "referral_commissions", fake_list)
    monkeypatch.setattr(
        EngineApiClient, "mark_referral_commissions_paid", fake_mark
    )
    with TestClient(app) as client:
        _login(client)
        r = client.post("/control/referrals/mark-paid", data={})
        assert r.status_code == 200
        assert called["mark"] is False
        assert "Nothing selected" in r.text

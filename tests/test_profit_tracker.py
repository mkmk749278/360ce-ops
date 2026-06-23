"""Profit tracker reshapes engine /api/signals into running + peak profit rows.

Covers the core contract the page promises the operator:
* active vs closed status mapping (a signal runs until it terminates),
* max-profit (MFE) surfaced alongside current P/L,
* the peak price reconstructed from entry + MFE in a direction-aware way,
* active rows sorting ahead of closed.
"""
from __future__ import annotations

import asyncio
import os
import types

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.routes.profit import _build_rows, _normalize, _peak_price, _summary  # noqa: E402


def _req(items):
    class _API:
        async def signals(self, status=None, setup_class=None):
            return {"items": items, "total": len(items)}

    return types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(engine_api=_API()))
    )


def _sig(**kw):
    base = {
        "signal_id": "S1",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "entry": 100.0,
        "stop_loss": 95.0,
        "current_price": 103.0,
        "pnl_pct": 3.0,
        "max_favorable_excursion_pct": 5.0,
        "max_adverse_excursion_pct": -1.0,
        "status": "ACTIVE",
        "minutes_ago": 10,
    }
    base.update(kw)
    return base


def test_active_status_and_max_profit_surfaced():
    row = _normalize(_sig())
    assert row["is_active"] is True
    assert row["status"] == "Active"
    assert row["pnl_pct"] == 3.0
    assert row["mfe_pct"] == 5.0
    # LONG peak = entry * (1 + mfe/100) = 100 * 1.05
    assert abs(row["max_price"] - 105.0) < 1e-9


def test_terminal_status_is_closed_with_reason():
    row = _normalize(_sig(status="SL_HIT"))
    assert row["is_active"] is False
    assert row["status"] == "Closed"
    assert row["close_reason"] == "SL_HIT"


def test_short_peak_is_below_entry():
    assert abs(_peak_price(100.0, "SHORT", 5.0) - 95.0) < 1e-9
    assert _peak_price(None, "LONG", 5.0) is None
    assert _peak_price(100.0, "LONG", None) is None


def test_missing_mfe_defaults_render_as_none_not_crash():
    row = _normalize({"signal_id": "X", "symbol": "ETHUSDT", "direction": "LONG",
                      "entry": 10.0, "status": "ACTIVE"})
    assert row["mfe_pct"] is None
    assert row["max_price"] is None


def test_active_rows_sort_before_closed():
    items = [
        _sig(signal_id="CLOSED", status="SL_HIT", minutes_ago=1),
        _sig(signal_id="ACTIVE", status="ACTIVE", minutes_ago=50),
    ]
    rows, error = asyncio.run(_build_rows(_req(items), "all"))
    assert error is None
    assert [r["id"] for r in rows] == ["ACTIVE", "CLOSED"]


def test_view_filters():
    items = [_sig(signal_id="A", status="ACTIVE"), _sig(signal_id="C", status="INVALIDATED")]
    active, _ = asyncio.run(_build_rows(_req(items), "active"))
    closed, _ = asyncio.run(_build_rows(_req(items), "closed"))
    assert {r["id"] for r in active} == {"A"}
    assert {r["id"] for r in closed} == {"C"}


def test_summary_counts_and_best_mfe():
    items = [
        _sig(signal_id="A", status="ACTIVE", max_favorable_excursion_pct=2.0),
        _sig(signal_id="B", status="SL_HIT", max_favorable_excursion_pct=7.5),
    ]
    rows, _ = asyncio.run(_build_rows(_req(items), "all"))
    s = _summary(rows)
    assert s == {"active": 1, "closed": 1, "best_mfe": 7.5}


def test_engine_error_surfaced():
    req = types.SimpleNamespace(
        app=types.SimpleNamespace(
            state=types.SimpleNamespace(
                engine_api=types.SimpleNamespace(
                    signals=lambda status=None, setup_class=None: _err()
                )
            )
        )
    )
    rows, error = asyncio.run(_build_rows(req, "all"))
    assert rows == []
    assert "boom" in error


async def _err():
    return {"error": "boom", "endpoint": "/api/signals"}


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


def test_profit_route_requires_auth():
    with TestClient(app) as client:
        r = client.get("/profit", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


def test_profit_route_renders_rows(monkeypatch):
    with TestClient(app) as client:
        _login(client)

        async def _fake_signals(self, status=None, setup_class=None):
            return {"items": [_sig(signal_id="REND", symbol="ETHUSDT")], "total": 1}

        from app.data_sources.engine_api import EngineApiClient
        monkeypatch.setattr(EngineApiClient, "signals", _fake_signals, raising=True)
        r = client.get("/profit")
        assert r.status_code == 200
        assert "Signal Profit Tracker" in r.text
        assert "ETHUSDT" in r.text
        # max-profit (MFE 5%) rendered as a percentage in the Max profit column
        assert "+5.00%" in r.text

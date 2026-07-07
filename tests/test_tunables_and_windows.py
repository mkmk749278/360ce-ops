"""Tests for the 2026-07-07 additions: the Engine Tunables control section
and the Profit page 24h / 3d / custom From→To windows."""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import control as control_route  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


_TUNABLES_PAYLOAD = {
    "initialised": True,
    "tunables": [
        {
            "key": "noise_floor_stops_enabled",
            "label": "Noise-floor stops",
            "description": "Widen every stop to clear the pair's 1h noise band.",
            "type": "bool",
            "default": True,
            "value": True,
            "min": None,
            "max": None,
            "unit": "",
            "category": "Stops & exits",
        },
        {
            "key": "noise_floor_atr_mult",
            "label": "Noise floor × ATR(1h)",
            "description": "Minimum stop distance as a multiple of the 1h ATR.",
            "type": "float",
            "default": 1.0,
            "value": 1.0,
            "min": 0.0,
            "max": 3.0,
            "unit": "× ATR",
            "category": "Stops & exits",
        },
        {
            "key": "cohort_edge_gate_enabled",
            "label": "Cohort-edge gate",
            "description": "Suppress measurably-losing cohorts.",
            "type": "bool",
            "default": True,
            "value": True,
            "min": None,
            "max": None,
            "unit": "",
            "category": "Signal gating",
        },
    ],
}


def _patch_control_reads(monkeypatch):
    async def fake_auto_mode(self):
        return {"mode": "paper"}

    async def fake_ks(self):
        return {"engaged": False, "initialised": True, "reason": None}

    async def fake_glob(self):
        return {"enabled": True, "initialised": True}

    async def fake_expiry(self):
        return {"enabled": False, "initialised": True}

    async def fake_tunables(self):
        return _TUNABLES_PAYLOAD

    monkeypatch.setattr(EngineApiClient, "auto_mode", fake_auto_mode)
    monkeypatch.setattr(EngineApiClient, "kill_switch_state", fake_ks)
    monkeypatch.setattr(EngineApiClient, "auto_trade_global_state", fake_glob)
    monkeypatch.setattr(EngineApiClient, "signal_expiry_state", fake_expiry)
    monkeypatch.setattr(EngineApiClient, "tunables_state", fake_tunables)
    monkeypatch.setattr(control_route.audit, "tail", lambda *a, **k: [])


def test_control_page_renders_tunables_section(monkeypatch):
    _patch_control_reads(monkeypatch)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/control")
        assert r.status_code == 200
        assert "Engine tunables" in r.text
        assert "Noise-floor stops" in r.text
        assert "Cohort-edge gate" in r.text
        # grouped by category
        assert "Stops &amp; exits" in r.text or "Stops & exits" in r.text
        assert "Signal gating" in r.text


def test_tunables_post_calls_engine_and_audits(monkeypatch, tmp_path):
    _patch_control_reads(monkeypatch)
    sent: dict = {}

    async def fake_set(self, values):
        sent.update(values)
        return {"initialised": True, "tunables": []}

    recorded: list = []
    monkeypatch.setattr(EngineApiClient, "set_tunables", fake_set)
    monkeypatch.setattr(
        control_route.audit, "record", lambda *a, **k: recorded.append(k)
    )

    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/tunables",
            data={
                "_bool_keys": "noise_floor_stops_enabled,cohort_edge_gate_enabled",
                "noise_floor_stops_enabled": "on",  # checked
                # cohort_edge_gate_enabled absent → unchecked → False
                "noise_floor_atr_mult": "1.5",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
    assert sent["noise_floor_stops_enabled"] is True
    assert sent["cohort_edge_gate_enabled"] is False
    assert sent["noise_floor_atr_mult"] == "1.5"  # engine coerces types
    assert recorded and recorded[0]["action"] == "tunables_update"
    assert recorded[0]["ok"] is True


def test_tunables_post_requires_auth():
    with TestClient(app) as client:
        r = client.post(
            "/control/tunables",
            data={"noise_floor_atr_mult": "1.5"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


# ---- Profit page windows -------------------------------------------------


def _patch_profit_sources(monkeypatch, records):
    """Route the profit page at the data-volume source with fixed records."""
    from app.data_sources.data_volume import DataVolumeReader

    monkeypatch.setattr(DataVolumeReader, "signal_performance", lambda self: records)


def _perf_record(ts: float, sid: str) -> dict:
    return {
        "signal_id": sid,
        "symbol": "TESTUSDT",
        "direction": "SHORT",
        "entry": 100.0,
        "stop_loss": 101.0,
        "pnl_pct": 0.5,
        "hit_tp": 0,
        "hit_sl": False,
        "outcome_label": "TP1_HIT",
        "setup_class": "SR_FLIP_RETEST",
        "market_phase": "TRENDING_DOWN",
        "confidence": 70.0,
        "max_favorable_excursion_pct": 1.0,
        "max_adverse_excursion_pct": -0.2,
        "terminal_outcome_timestamp": ts,
        "timestamp": ts,
    }


def test_profit_accepts_new_window_values(monkeypatch):
    _patch_profit_sources(monkeypatch, [])
    with TestClient(app) as client:
        _login(client)
        for window in ("24h", "3d", "range"):
            r = client.get(f"/profit?window={window}")
            assert r.status_code == 200, window
            assert "last 24 hours" in r.text
            assert "last 3 days" in r.text
            assert "custom From" in r.text


def test_profit_range_window_filters_by_dates(monkeypatch):
    from datetime import datetime, timezone

    inside = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc).timestamp()
    outside = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc).timestamp()
    _patch_profit_sources(
        monkeypatch,
        [_perf_record(inside, "IN-1"), _perf_record(outside, "OUT-1")],
    )
    with TestClient(app) as client:
        _login(client)
        r = client.get("/profit?window=range&date_from=2026-07-01&date_to=2026-07-05")
        assert r.status_code == 200
        assert "IN-1" in r.text
        assert "OUT-1" not in r.text


def test_profit_range_open_ended(monkeypatch):
    from datetime import datetime, timezone

    ts = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc).timestamp()
    _patch_profit_sources(monkeypatch, [_perf_record(ts, "IN-2")])
    with TestClient(app) as client:
        _login(client)
        # Only a From bound — To stays open.
        r = client.get("/profit?window=range&date_from=2026-07-01")
        assert r.status_code == 200
        assert "IN-2" in r.text

"""Tests for the engine control plane (ops' first write surface, 2026-06-20).

Covers the audit-log round trip and the control routes (auth gate,
auto-mode flip, kill-switch engage/disengage) with the engine client
monkeypatched — we assert ops calls the right engine method, records an
audit entry, and surfaces the result via the PRG flash.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app import audit  # noqa: E402
from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import control as control_route  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


async def _fake_glob(self):
    return {"enabled": True, "initialised": True}


# ---- audit log ----------------------------------------------------------


def test_audit_record_and_tail_round_trip(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    audit.record(path, action="auto_mode", params={"mode": "paper"},
                 result={}, ok=True)
    audit.record(path, action="kill_switch", params={"engaged": True},
                 result={"error": "boom"}, ok=False)
    rows = audit.tail(path, limit=10)
    # Newest first.
    assert rows[0]["action"] == "kill_switch"
    assert rows[0]["ok"] is False
    assert rows[0]["result"] == "boom"
    assert rows[1]["action"] == "auto_mode"
    assert rows[1]["ok"] is True


def test_audit_tail_missing_file_is_empty():
    assert audit.tail("/nonexistent/path/audit.jsonl") == []


def test_audit_record_bad_path_does_not_raise():
    # A control action must never blow up because the audit volume is
    # unwritable — record swallows the OSError.
    audit.record("/proc/cannot/write/here.jsonl", action="x",
                 params={}, result={}, ok=True)


# ---- control routes -----------------------------------------------------


def test_control_page_requires_auth():
    with TestClient(app) as client:
        r = client.get("/control", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


def _patch_reads(monkeypatch, *, mode="paper"):
    """Monkeypatch the three read calls _render makes, so control tests
    don't hit the network."""
    async def fake_auto_mode(self):
        return {"mode": mode}

    async def fake_ks(self):
        return {"engaged": False, "initialised": True, "reason": None}

    async def fake_glob(self):
        return {"enabled": True, "initialised": True}

    monkeypatch.setattr(EngineApiClient, "auto_mode", fake_auto_mode)
    monkeypatch.setattr(EngineApiClient, "kill_switch_state", fake_ks)
    monkeypatch.setattr(EngineApiClient, "auto_trade_global_state", fake_glob)
    monkeypatch.setattr(control_route.audit, "tail", lambda *a, **k: [])


def test_control_page_renders_state(monkeypatch):
    _patch_reads(monkeypatch, mode="paper")
    with TestClient(app) as client:
        _login(client)
        r = client.get("/control")
        assert r.status_code == 200
        assert "Engine Control" in r.text
        assert "PAPER" in r.text  # current mode surfaced


def test_auto_mode_flip_calls_engine_and_audits(monkeypatch):
    calls: dict = {}

    async def fake_set(self, mode):
        calls["mode"] = mode
        return {"success": True, "mode": mode}

    async def fake_auto_mode(self):
        return {"mode": calls.get("mode", "off")}

    async def fake_ks(self):
        return {"engaged": False, "initialised": True}

    async def fake_glob(self):
        return {"enabled": True, "initialised": True}

    recorded: list = []
    monkeypatch.setattr(EngineApiClient, "auto_trade_global_state", fake_glob)
    monkeypatch.setattr(EngineApiClient, "set_auto_mode", fake_set)
    monkeypatch.setattr(EngineApiClient, "auto_mode", fake_auto_mode)
    monkeypatch.setattr(EngineApiClient, "kill_switch_state", fake_ks)
    monkeypatch.setattr(control_route.audit, "tail", lambda *a, **k: [])
    monkeypatch.setattr(
        control_route.audit, "record",
        lambda *a, **k: recorded.append(k),
    )

    with TestClient(app) as client:
        _login(client)
        r = client.post("/control/auto-mode", data={"mode": "live"})
        assert r.status_code == 200  # followed the 303 to /control
        assert calls["mode"] == "live"
        assert recorded and recorded[0]["action"] == "auto_mode"
        assert recorded[0]["ok"] is True
        assert "Auto-mode set to LIVE" in r.text


def test_auto_mode_invalid_is_rejected_without_engine_call(monkeypatch):
    called = {"set": False}

    async def fake_set(self, mode):
        called["set"] = True
        return {"success": True}

    async def fake_auto_mode(self):
        return {"mode": "off"}

    async def fake_ks(self):
        return {"engaged": False, "initialised": True}

    monkeypatch.setattr(EngineApiClient, "set_auto_mode", fake_set)
    monkeypatch.setattr(EngineApiClient, "auto_mode", fake_auto_mode)
    monkeypatch.setattr(EngineApiClient, "kill_switch_state", fake_ks)
    monkeypatch.setattr(EngineApiClient, "auto_trade_global_state", _fake_glob)
    monkeypatch.setattr(control_route.audit, "tail", lambda *a, **k: [])

    with TestClient(app) as client:
        _login(client)
        r = client.post("/control/auto-mode", data={"mode": "yolo"})
        assert r.status_code == 200
        assert called["set"] is False
        assert "invalid mode" in r.text.lower()


def test_kill_switch_engage_calls_engine_and_audits(monkeypatch):
    calls: dict = {}

    async def fake_set_ks(self, engaged, reason=None):
        calls["engaged"] = engaged
        calls["reason"] = reason
        return {"engaged": engaged, "initialised": True, "reason": reason}

    async def fake_auto_mode(self):
        return {"mode": "off"}

    async def fake_ks(self):
        return {"engaged": calls.get("engaged", False), "initialised": True}

    recorded: list = []
    monkeypatch.setattr(EngineApiClient, "set_kill_switch", fake_set_ks)
    monkeypatch.setattr(EngineApiClient, "auto_mode", fake_auto_mode)
    monkeypatch.setattr(EngineApiClient, "kill_switch_state", fake_ks)
    monkeypatch.setattr(EngineApiClient, "auto_trade_global_state", _fake_glob)
    monkeypatch.setattr(control_route.audit, "tail", lambda *a, **k: [])
    monkeypatch.setattr(
        control_route.audit, "record",
        lambda *a, **k: recorded.append(k),
    )

    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/kill-switch",
            data={"engaged": "true", "reason": "manual halt"},
        )
        assert r.status_code == 200
        assert calls["engaged"] is True
        assert calls["reason"] == "manual halt"
        assert recorded[0]["action"] == "kill_switch"
        assert "ENGAGED" in r.text


def test_control_positions_partial_renders_open_positions(monkeypatch):
    async def fake_diag(self):
        return {
            "monitor_running": True,
            "items": [
                {"status": "ACTIVE", "symbol": "BTCUSDT", "direction": "long",
                 "entry": 65000.0, "current_price": 65500.0, "stop_loss": 64000.0,
                 "pnl_pct": 0.77, "minutes_open": 12, "signal_id": "abc"},
                # Phantom placeholder (no symbol / zero entry) — must be filtered.
                {"status": "ACTIVE", "symbol": "", "entry": 0.0, "signal_id": "x"},
            ],
        }

    monkeypatch.setattr(EngineApiClient, "positions_diag", fake_diag)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/control/positions")
        assert r.status_code == 200
        assert "BTCUSDT" in r.text
        assert "1 open" in r.text  # phantom row filtered out


def test_control_positions_partial_empty(monkeypatch):
    async def fake_diag(self):
        return {"monitor_running": True, "items": []}

    monkeypatch.setattr(EngineApiClient, "positions_diag", fake_diag)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/control/positions")
        assert r.status_code == 200
        assert "No open positions" in r.text


def test_auto_trade_global_flip_calls_engine_and_audits(monkeypatch):
    calls: dict = {}

    async def fake_set_glob(self, enabled):
        calls["enabled"] = enabled
        return {"enabled": enabled, "initialised": True}

    async def fake_glob(self):
        return {"enabled": calls.get("enabled", False), "initialised": True}

    async def fake_auto_mode(self):
        return {"mode": "off"}

    async def fake_ks(self):
        return {"engaged": False, "initialised": True}

    recorded: list = []
    monkeypatch.setattr(EngineApiClient, "set_auto_trade_global", fake_set_glob)
    monkeypatch.setattr(EngineApiClient, "auto_trade_global_state", fake_glob)
    monkeypatch.setattr(EngineApiClient, "auto_mode", fake_auto_mode)
    monkeypatch.setattr(EngineApiClient, "kill_switch_state", fake_ks)
    monkeypatch.setattr(control_route.audit, "tail", lambda *a, **k: [])
    monkeypatch.setattr(
        control_route.audit, "record",
        lambda *a, **k: recorded.append(k),
    )

    with TestClient(app) as client:
        _login(client)
        r = client.post("/control/auto-trade-global", data={"enabled": "false"})
        assert r.status_code == 200
        assert calls["enabled"] is False
        assert recorded[0]["action"] == "auto_trade_global"
        assert "DISABLED" in r.text

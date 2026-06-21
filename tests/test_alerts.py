"""Tests for the safety-alerts panel — the agent-alert surface that
replaces Telegram paging (2026-06-20).

Covers the Redis reader's parsing / sorting / WARN-threshold suppression
(with a fake async Redis) and the /alerts route (auth gate + render).
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.data_sources.agent_alerts import AgentAlertsReader  # noqa: E402
from app.main import app  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


class _FakeRedis:
    """Minimal async stand-in: ``keys`` + ``get`` over a dict."""

    def __init__(self, data: dict[str, str], *, fail: bool = False) -> None:
        self._data = data
        self._fail = fail

    async def keys(self, pattern: str):
        if self._fail:
            raise RuntimeError("redis down")
        return list(self._data.keys())

    async def get(self, key: str):
        return self._data.get(key)


def _reader_with(data: dict[str, str], *, fail: bool = False) -> AgentAlertsReader:
    r = AgentAlertsReader(load_settings())
    r._client = _FakeRedis(data, fail=fail)
    return r


# ---- reader ------------------------------------------------------------


async def test_active_alerts_high_first_and_describes():
    data = {
        "alert:state:redis_stale": json.dumps({
            "severity": "WARN", "count": 3, "description": "Redis idle 70s",
            "first_seen": "2026-06-20T10:00:00+00:00",
            "last_seen": "2026-06-20T10:05:00+00:00", "paged": True,
        }),
        "alert:state:naked_position:BTCUSDT:abc": json.dumps({
            "severity": "HIGH", "count": 1, "description": "OPEN BTCUSDT no SL",
            "first_seen": "2026-06-20T10:02:00+00:00",
            "last_seen": "2026-06-20T10:06:00+00:00", "paged": True,
        }),
    }
    out = await _reader_with(data).active_alerts()
    assert out["error"] is None
    assert len(out["alerts"]) == 2
    # HIGH (naked position) sorts ahead of WARN.
    assert out["alerts"][0]["severity"] == "HIGH"
    assert out["alerts"][0]["kind"] == "naked_position"
    assert out["alerts"][0]["description"] == "OPEN BTCUSDT no SL"


async def test_active_alerts_suppresses_subthreshold_warn():
    # A single-cycle WARN hasn't reached the agent's page gate — hide it.
    data = {
        "alert:state:signal_silence": json.dumps({
            "severity": "WARN", "count": 1, "description": "no signals 30m",
            "first_seen": "x", "last_seen": "x", "paged": False,
        }),
    }
    out = await _reader_with(data).active_alerts()
    assert out["alerts"] == []


async def test_active_alerts_redis_failure_is_degraded_not_raised():
    out = await _reader_with({}, fail=True).active_alerts()
    assert out["alerts"] == []
    assert out["error"]


# ---- route -------------------------------------------------------------


def test_alerts_page_requires_auth():
    with TestClient(app) as client:
        r = client.get("/alerts", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


def test_alerts_page_renders_active(monkeypatch):
    async def fake_active(self):
        return {
            "alerts": [{
                "severity": "HIGH", "fingerprint": "naked_position:BTCUSDT:a",
                "kind": "naked_position", "description": "OPEN BTCUSDT no SL",
                "count": 1, "first_seen": "2026-06-20T10:02:00+00:00",
                "last_seen": "2026-06-20T10:06:00+00:00", "paged": True,
            }],
            "error": None,
        }

    monkeypatch.setattr(AgentAlertsReader, "active_alerts", fake_active)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/alerts")
        assert r.status_code == 200
        assert "Safety Alerts" in r.text
        assert "naked_position" in r.text
        assert "1 HIGH-severity alert" in r.text


def test_alerts_page_all_clear(monkeypatch):
    async def fake_active(self):
        return {"alerts": [], "error": None}

    monkeypatch.setattr(AgentAlertsReader, "active_alerts", fake_active)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/alerts")
        assert r.status_code == 200
        assert "No active alerts" in r.text

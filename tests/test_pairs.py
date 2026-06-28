"""Tests for the Pairs page (regular universe + live promoting movers)."""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.main import app  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


_PAYLOAD = {
    "regular": [
        {"symbol": "BTCUSDT", "tier": "TIER1", "volume_24h_usd": 5e9, "change_24h_pct": 1.2},
    ],
    "promoting": [
        {"symbol": "ARXUSDT", "cycles_left": 5, "volume_24h_usd": 8e6, "change_24h_pct": 22.0},
    ],
    "regular_count": 1,
    "promoting_count": 1,
    "updated_at": "2026-06-27T09:00:00+00:00",
    "ignition": {
        "enabled": True, "tracked_symbols": 180, "frames_ingested": 4200,
        "ignitions_total": 3, "last_ignition_at": "2026-06-27T08:55:00+00:00",
        "ws_connected": True, "ws_streams": 1,
    },
}


def test_pairs_requires_auth():
    with TestClient(app) as client:
        r = client.get("/pairs", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


def test_pairs_promoting_tab_default(monkeypatch):
    async def fake_pairs(self):
        return _PAYLOAD

    monkeypatch.setattr(EngineApiClient, "pairs", fake_pairs)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/pairs")
        assert r.status_code == 200
        assert "Promoting" in r.text
        assert "ARXUSDT" in r.text                 # promoting row rendered
        assert "2026-06-27T09:00:00+00:00" in r.text  # updated_at visible
        assert "Ignition feed" in r.text           # health line rendered
        assert "alive" in r.text                    # frames flowing + WS up


def test_pairs_stalled_feed_flagged(monkeypatch):
    async def fake_pairs(self):
        return {"regular": [], "promoting": [], "regular_count": 0,
                "promoting_count": 0, "updated_at": "t",
                "ignition": {"enabled": True, "tracked_symbols": 0,
                             "frames_ingested": 0, "ignitions_total": 0,
                             "last_ignition_at": None, "ws_connected": False,
                             "ws_streams": 0}}

    monkeypatch.setattr(EngineApiClient, "pairs", fake_pairs)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/pairs")
        assert r.status_code == 200
        assert "STALLED" in r.text


def test_pairs_regular_tab(monkeypatch):
    async def fake_pairs(self):
        return _PAYLOAD

    monkeypatch.setattr(EngineApiClient, "pairs", fake_pairs)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/pairs", params={"tab": "regular"})
        assert r.status_code == 200
        assert "BTCUSDT" in r.text
        assert "TIER1" in r.text


def test_pairs_empty_promoting_shows_hint(monkeypatch):
    async def fake_pairs(self):
        return {"regular": [], "promoting": [], "regular_count": 0,
                "promoting_count": 0, "updated_at": "t"}

    monkeypatch.setattr(EngineApiClient, "pairs", fake_pairs)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/pairs")
        assert r.status_code == 200
        assert "No pairs currently promoted" in r.text


def test_pairs_engine_error_surfaced(monkeypatch):
    async def fake_pairs(self):
        return {"error": "engine unreachable"}

    monkeypatch.setattr(EngineApiClient, "pairs", fake_pairs)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/pairs")
        assert r.status_code == 200
        assert "engine unreachable" in r.text

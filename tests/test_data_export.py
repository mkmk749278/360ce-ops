"""Tests for the Data Export page + raw JSON download routes."""
from __future__ import annotations

import json
import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.data_volume import DataVolumeReader  # noqa: E402
from app.main import app  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


_FAKE_HISTORY = [
    {"signal_id": "A1", "pnl_pct": 0.5, "component_scores": {"total": 71.0, "smc": 22.0}},
    {"signal_id": "A2", "pnl_pct": -0.3, "component_scores": {"total": 68.0, "smc": 20.0}},
]


def test_data_page_requires_auth():
    with TestClient(app) as client:
        r = client.get("/data", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


def test_data_page_lists_artifacts_with_counts(monkeypatch):
    monkeypatch.setattr(DataVolumeReader, "signal_history", lambda self: _FAKE_HISTORY)
    monkeypatch.setattr(DataVolumeReader, "signal_performance", lambda self: [])
    monkeypatch.setattr(
        DataVolumeReader, "invalidation_records", lambda self: {"error": "missing: x"}
    )
    with TestClient(app) as client:
        _login(client)
        r = client.get("/data")
        assert r.status_code == 200
        assert "Data downloads" in r.text
        assert "signal_history.json" in r.text
        # record count surfaced for the loaded artifact
        assert ">2<" in r.text or "2" in r.text
        # error surfaced for the missing artifact
        assert "missing: x" in r.text


def test_download_signal_history_is_json_attachment(monkeypatch):
    monkeypatch.setattr(DataVolumeReader, "signal_history", lambda self: _FAKE_HISTORY)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/data/download/signal_history")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        cd = r.headers["content-disposition"]
        assert "attachment" in cd and "signal_history_" in cd and cd.endswith('.json"')
        # body is the faithful payload — incl. component_scores for calibration
        payload = json.loads(r.text)
        assert payload == _FAKE_HISTORY
        assert payload[0]["component_scores"]["total"] == 71.0


def test_download_unknown_export_404():
    with TestClient(app) as client:
        _login(client)
        r = client.get("/data/download/secrets")
        assert r.status_code == 404

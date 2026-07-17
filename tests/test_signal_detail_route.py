"""Per-signal drill-down route (`/signals/{signal_id}`).

The route joins the live engine view with the three durable JSON stores
and renders whichever sections returned data; it 404s only when *no*
source knows the signal.  Untested until now — a regression in the
"any source counts" logic would either 404 real historical signals or
render empty drill-downs for live ones.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.data_volume import DataVolumeReader  # noqa: E402
from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.main import app  # noqa: E402
from app.routes.signal_detail import _find_record  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


def _stub_sources(
    monkeypatch,
    *,
    live=None,
    performance=None,
    invalidation=None,
    history=None,
) -> None:
    async def fake_signal(self, signal_id):
        return live if live is not None else {"error": "engine unreachable"}

    monkeypatch.setattr(EngineApiClient, "signal", fake_signal)
    monkeypatch.setattr(
        DataVolumeReader, "signal_performance", lambda self: performance
    )
    monkeypatch.setattr(
        DataVolumeReader, "invalidation_records", lambda self: invalidation
    )
    monkeypatch.setattr(DataVolumeReader, "signal_history", lambda self: history)


class TestFindRecord:
    def test_list_matches_signal_id_or_id(self):
        records = [{"id": "a"}, {"signal_id": "b", "symbol": "ETHUSDT"}]
        assert _find_record(records, "b")["symbol"] == "ETHUSDT"
        assert _find_record(records, "a") == {"id": "a"}

    def test_non_dict_entries_are_skipped(self):
        assert _find_record(["junk", 42, {"signal_id": "x"}], "x") == {
            "signal_id": "x"
        }

    def test_dict_keyed_store(self):
        assert _find_record({"sig-1": {"pnl": 1.0}}, "sig-1") == {"pnl": 1.0}
        assert _find_record({"sig-1": "not-a-dict"}, "sig-1") is None

    def test_missing_returns_none(self):
        assert _find_record([], "nope") is None
        assert _find_record(None, "nope") is None


class TestRoute:
    def test_requires_auth(self, monkeypatch):
        _stub_sources(monkeypatch)
        with TestClient(app) as client:
            r = client.get("/signals/sig-1", follow_redirects=False)
            assert r.status_code == 302
            assert r.headers["location"] == "/login"

    def test_404_when_no_source_knows_the_signal(self, monkeypatch):
        _stub_sources(monkeypatch)
        with TestClient(app) as client:
            _login(client)
            r = client.get("/signals/ghost-1")
            assert r.status_code == 404

    def test_renders_live_signal(self, monkeypatch):
        _stub_sources(
            monkeypatch,
            live={"signal_id": "sig-1", "symbol": "BTCUSDT", "status": "ACTIVE"},
        )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/signals/sig-1")
            assert r.status_code == 200
            assert "BTCUSDT" in r.text

    def test_renders_from_durable_history_when_engine_down(self, monkeypatch):
        # The engine being unreachable must not 404 a signal the durable
        # stores still know — that history is the whole point of the join.
        _stub_sources(
            monkeypatch,
            history=[{"signal_id": "old-1", "symbol": "SOLUSDT"}],
        )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/signals/old-1")
            assert r.status_code == 200
            assert "SOLUSDT" in r.text

    def test_live_error_dict_does_not_count_as_found(self, monkeypatch):
        # An {"error": ...} payload from the engine client is "no data",
        # not a hit — only durable rows can rescue the request then.
        _stub_sources(monkeypatch, live={"error": "timeout"})
        with TestClient(app) as client:
            _login(client)
            assert client.get("/signals/sig-9").status_code == 404

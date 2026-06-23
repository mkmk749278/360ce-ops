"""Profit tracker = held-to-stop candle replay (parallel observation only).

Three layers, exercised independently so they collect without a live engine or
Binance:

* ``simulate_to_stop`` — the pure replay: max profit (pessimistic on the stop
  candle), held-to-stop result, active vs stopped.
* ``FreeRunTracker`` — caching of terminal results, graceful degradation to
  engine numbers when candles can't be fetched, and the disabled (skipped) path.
* ``_build_rows`` / route — reshaping, view filters, sorting, summary, and the
  give-back comparison vs the engine's real exit.
"""
from __future__ import annotations

import asyncio
import os
import types
from dataclasses import replace

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")
# Keep the live Profit route off Binance in TestClient tests by default.
os.environ.setdefault("FREE_RUN_ENABLED", "0")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.data_sources.binance_klines import Kline  # noqa: E402
from app.data_sources.free_run import FreeRunTracker, simulate_to_stop  # noqa: E402
from app.main import app  # noqa: E402
from app.routes.profit import _aggregates, _build_rows, _summary  # noqa: E402

_MIN = 60_000


def _k(i, high, low, close):
    return Kline(open_time_ms=i * _MIN, high=high, low=low, close=close)


# --------------------------------------------------------------------------- #
# simulate_to_stop — pure replay
# --------------------------------------------------------------------------- #

def test_long_stop_excludes_stop_candle_high_from_max_profit():
    # Rises to 105 over c0/c1, then c2 wicks down to 94 (≤ SL 95) AND up to 106.
    # Pessimistic: the stop candle's 106 high must NOT count as max profit.
    candles = [_k(0, 102, 99, 101), _k(1, 105, 101, 104), _k(2, 106, 94, 96)]
    out = simulate_to_stop(candles, entry=100.0, sl=95.0, direction="LONG", dispatch_ms=0)
    assert out["sl_hit"] is True
    assert abs(out["mfe_pct"] - 5.0) < 1e-9       # from 105, not 106
    assert abs(out["mfe_price"] - 105.0) < 1e-9
    assert abs(out["result_pct"] - (-5.0)) < 1e-9  # held → SL loss
    assert out["current_price"] == 95.0
    assert out["hold_mins"] == 2


def test_short_stop_is_direction_aware():
    # SHORT entry 100, SL 105. Best favourable = low 95 (before stop candle).
    candles = [_k(0, 101, 98, 100), _k(1, 103, 95, 99), _k(2, 106, 100, 104)]
    out = simulate_to_stop(candles, entry=100.0, sl=105.0, direction="SHORT", dispatch_ms=0)
    assert out["sl_hit"] is True
    assert abs(out["mfe_pct"] - 5.0) < 1e-9
    assert abs(out["result_pct"] - (-5.0)) < 1e-9


def test_active_when_stop_never_touched_marks_to_last_close():
    candles = [_k(0, 103, 99, 102), _k(1, 104, 100, 101.5)]
    out = simulate_to_stop(candles, entry=100.0, sl=95.0, direction="LONG", dispatch_ms=0)
    assert out["sl_hit"] is False
    assert abs(out["mfe_pct"] - 4.0) < 1e-9         # peak 104
    assert out["current_price"] == 101.5            # last close
    assert abs(out["result_pct"] - 1.5) < 1e-9


def test_stop_on_first_candle_yields_zero_max_profit():
    out = simulate_to_stop([_k(0, 108, 94, 96)], entry=100.0, sl=95.0, direction="LONG", dispatch_ms=0)
    assert out["sl_hit"] is True
    assert out["mfe_pct"] == 0.0
    assert out["mfe_price"] is None


# --------------------------------------------------------------------------- #
# FreeRunTracker — caching, degradation, disabled
# --------------------------------------------------------------------------- #

class _FakeKlines:
    def __init__(self, candles, fail=False):
        self._candles = candles
        self.fail = fail
        self.calls = 0

    async def fetch_1m(self, symbol, start_ms, end_ms):
        self.calls += 1
        if self.fail:
            raise httpx.HTTPError("binance unreachable")
        return list(self._candles)


def _tracker(*, enabled=True, candles=None, fail=False):
    settings = replace(
        load_settings(),
        free_run_enabled=enabled,
        free_run_cache_ttl_sec=30,
        free_run_concurrency=3,
        free_run_max_lookback_min=10080,
    )
    fake = _FakeKlines(candles or [], fail=fail)
    return FreeRunTracker(settings, fake), fake


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
        "status": "ACTIVE",
        "minutes_ago": 10,
        "timestamp": "2026-06-23T00:00:00+00:00",
    }
    base.update(kw)
    return base


def test_tracker_replays_and_caches_terminal_result():
    tracker, fake = _tracker(candles=[_k(0, 105, 99, 104), _k(1, 106, 94, 96)])
    r1 = asyncio.run(tracker.compute(_sig()))
    assert r1.source == "replay"
    assert r1.sl_hit is True
    assert abs(r1.mfe_pct - 5.0) < 1e-9
    # Terminal results are immutable → a second compute serves from cache.
    r2 = asyncio.run(tracker.compute(_sig()))
    assert r2 is r1
    assert fake.calls == 1


def test_tracker_degrades_to_engine_values_on_fetch_failure():
    tracker, _ = _tracker(fail=True)
    r = asyncio.run(tracker.compute(_sig()))
    assert r.degraded is True
    assert r.source == "engine-fallback"
    # Falls back to the engine's own numbers rather than crashing.
    assert r.mfe_pct == 5.0
    assert r.result_pct == 3.0


def test_tracker_disabled_uses_engine_values_without_calling_binance():
    tracker, fake = _tracker(enabled=False)
    r = asyncio.run(tracker.compute(_sig()))
    assert r.source == "skipped"
    assert r.degraded is False
    assert fake.calls == 0


def test_tracker_falls_back_when_signal_missing_replay_inputs():
    tracker, fake = _tracker(candles=[_k(0, 105, 94, 96)])
    # No timestamp → can't anchor the replay window → engine fallback.
    r = asyncio.run(tracker.compute(_sig(timestamp=None)))
    assert r.source == "engine-fallback"
    assert fake.calls == 0


# --------------------------------------------------------------------------- #
# Route — _build_rows / _summary (driven with a disabled tracker = engine values)
# --------------------------------------------------------------------------- #

def _req(items, tracker=None):
    if tracker is None:
        tracker, _ = _tracker(enabled=False)

    class _API:
        async def signals(self, status=None, setup_class=None):
            return {"items": items, "total": len(items)}

    return types.SimpleNamespace(
        app=types.SimpleNamespace(
            state=types.SimpleNamespace(engine_api=_API(), free_run=tracker)
        )
    )


def test_active_rows_sort_before_stopped():
    items = [
        _sig(signal_id="CLOSED", status="SL_HIT", minutes_ago=1),
        _sig(signal_id="ACTIVE", status="ACTIVE", minutes_ago=50),
    ]
    rows, error = asyncio.run(_build_rows(_req(items), "all"))
    assert error is None
    assert [r["id"] for r in rows] == ["ACTIVE", "CLOSED"]


def test_view_filters_on_held_to_stop_status():
    items = [_sig(signal_id="A", status="ACTIVE"), _sig(signal_id="C", status="INVALIDATED")]
    active, _ = asyncio.run(_build_rows(_req(items), "active"))
    closed, _ = asyncio.run(_build_rows(_req(items), "closed"))
    assert {r["id"] for r in active} == {"A"}
    assert {r["id"] for r in closed} == {"C"}


def test_summary_counts_best_mfe_and_giveback():
    items = [
        _sig(signal_id="A", status="ACTIVE", max_favorable_excursion_pct=2.0),
        _sig(signal_id="B", status="SL_HIT", max_favorable_excursion_pct=7.5, pnl_pct=3.0),
    ]
    rows, _ = asyncio.run(_build_rows(_req(items), "all"))
    s = _summary(rows)
    assert s["active"] == 1
    assert s["closed"] == 1
    assert s["best_mfe"] == 7.5
    # Give-back only counts the stopped row: 7.5 max profit − 3.0 realised.
    assert abs(s["avg_giveback"] - 4.5) < 1e-9
    assert s["degraded"] == 0


def test_aggregates_group_giveback_by_exit_and_setup():
    items = [
        _sig(signal_id="A", status="INVALIDATED", setup_class="SR_FLIP_RETEST",
             max_favorable_excursion_pct=10.0, pnl_pct=-0.3),
        _sig(signal_id="B", status="PROFIT_LOCKED", setup_class="SR_FLIP_RETEST",
             max_favorable_excursion_pct=0.0, pnl_pct=0.4),
        _sig(signal_id="C", status="INVALIDATED", setup_class="VWAP_RECLAIM",
             max_favorable_excursion_pct=2.0, pnl_pct=0.0),
        # Engine-live row contributes no give-back → excluded from the roll-up.
        _sig(signal_id="D", status="ACTIVE", setup_class="VWAP_RECLAIM"),
    ]
    rows, _ = asyncio.run(_build_rows(_req(items), "all"))
    agg = _aggregates(rows)
    assert agg["n"] == 3
    assert abs(agg["total_giveback"] - 11.9) < 1e-9   # 10.3 - 0.4 + 2.0

    by_exit = {g["key"]: g for g in agg["by_exit"]}
    assert by_exit["INVALIDATED"]["n"] == 2
    assert abs(by_exit["INVALIDATED"]["total_giveback"] - 12.3) < 1e-9  # 10.3 + 2.0
    assert abs(by_exit["PROFIT_LOCKED"]["total_giveback"] - (-0.4)) < 1e-9
    # Biggest total give-back sorts first.
    assert agg["by_exit"][0]["key"] == "INVALIDATED"

    by_setup = {g["key"]: g for g in agg["by_setup"]}
    assert by_setup["SR_FLIP_RETEST"]["n"] == 2
    assert abs(by_setup["SR_FLIP_RETEST"]["total_giveback"] - 9.9) < 1e-9  # 10.3 - 0.4
    assert abs(by_setup["VWAP_RECLAIM"]["total_giveback"] - 2.0) < 1e-9


def test_engine_error_surfaced():
    async def _err():
        return {"error": "boom", "endpoint": "/api/signals"}

    tracker, _ = _tracker(enabled=False)
    req = types.SimpleNamespace(
        app=types.SimpleNamespace(
            state=types.SimpleNamespace(
                engine_api=types.SimpleNamespace(
                    signals=lambda status=None, setup_class=None: _err()
                ),
                free_run=tracker,
            )
        )
    )
    rows, error = asyncio.run(_build_rows(req, "all"))
    assert rows == []
    assert "boom" in error


# --------------------------------------------------------------------------- #
# Route — TestClient (auth + render)
# --------------------------------------------------------------------------- #

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
        # Force the engine-values path so the render never reaches Binance.
        client.app.state.free_run._enabled = False

        async def _fake_signals(self, status=None, setup_class=None):
            return {"items": [_sig(signal_id="REND", symbol="ETHUSDT")], "total": 1}

        from app.data_sources.engine_api import EngineApiClient
        monkeypatch.setattr(EngineApiClient, "signals", _fake_signals, raising=True)
        r = client.get("/profit")
        assert r.status_code == 200
        assert "Signal Profit Tracker" in r.text
        assert "ETHUSDT" in r.text
        # max profit (engine MFE 5%) rendered in the Max profit column
        assert "+5.00%" in r.text

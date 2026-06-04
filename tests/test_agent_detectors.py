"""Unit tests for Tier 0 detectors.

All tests use plain dicts — no live network, no Docker, no Redis.
"""
from __future__ import annotations

import pytest

from app.agent.detectors import (
    ApiHealthDetector,
    BackgroundTaskDetector,
    EngineStatusDetector,
    HeartbeatAgeDetector,
    NakedPositionDetector,
    RedisStalenessDetector,
    SignalSilenceDetector,
    SigningHealthDetector,
)


# ---------------------------------------------------------------------------
# D1 — NakedPositionDetector
# ---------------------------------------------------------------------------

class TestNakedPositionDetector:
    def test_triggers_on_zero_sl(self):
        d = NakedPositionDetector(grace_sec=90)
        items = [{"status": "ACTIVE", "symbol": "BTCUSDT", "signal_id": "s1",
                  "minutes_open": 5, "stop_loss": 0.0}]
        results = d.check(items)
        assert len(results) == 1
        assert results[0].severity == "HIGH"
        assert results[0].fingerprint == "naked_position:BTCUSDT:s1"

    def test_no_trigger_within_grace(self):
        d = NakedPositionDetector(grace_sec=90)
        items = [{"status": "ACTIVE", "symbol": "BTCUSDT", "signal_id": "s1",
                  "minutes_open": 1, "stop_loss": 0.0}]
        assert d.check(items) == []

    def test_no_trigger_with_valid_sl(self):
        d = NakedPositionDetector(grace_sec=90)
        items = [{"status": "ACTIVE", "symbol": "BTCUSDT", "signal_id": "s1",
                  "minutes_open": 5, "stop_loss": 65000.0}]
        assert d.check(items) == []

    def test_no_trigger_for_non_active_status(self):
        d = NakedPositionDetector(grace_sec=90)
        items = [{"status": "SL_HIT", "symbol": "BTCUSDT", "signal_id": "s1",
                  "minutes_open": 5, "stop_loss": 0.0}]
        assert d.check(items) == []

    def test_multiple_naked_positions(self):
        d = NakedPositionDetector(grace_sec=90)
        items = [
            {"status": "ACTIVE", "symbol": "BTCUSDT", "signal_id": "s1",
             "minutes_open": 5, "stop_loss": 0.0},
            {"status": "ACTIVE", "symbol": "ETHUSDT", "signal_id": "s2",
             "minutes_open": 10, "stop_loss": 0.0},
        ]
        results = d.check(items)
        assert len(results) == 2
        fps = {r.fingerprint for r in results}
        assert "naked_position:BTCUSDT:s1" in fps
        assert "naked_position:ETHUSDT:s2" in fps

    def test_empty_list(self):
        d = NakedPositionDetector()
        assert d.check([]) == []

    def test_grace_boundary_exact(self):
        d = NakedPositionDetector(grace_sec=90)
        # minutes_open=2 → 120s > 90s → should trigger
        items = [{"status": "ACTIVE", "symbol": "BTCUSDT", "signal_id": "s1",
                  "minutes_open": 2, "stop_loss": 0.0}]
        assert len(d.check(items)) == 1


# ---------------------------------------------------------------------------
# D2 — BackgroundTaskDetector
# ---------------------------------------------------------------------------

class TestBackgroundTaskDetector:
    _ALL = ["trade_monitor", "reconciler", "mark_price_feed",
            "funding_exit_watcher", "pretp_dispatcher"]

    def test_no_trigger_all_present(self):
        d = BackgroundTaskDetector()
        assert d.check(self._ALL) == []

    def test_triggers_on_missing_task(self):
        d = BackgroundTaskDetector()
        tasks = [t for t in self._ALL if t != "trade_monitor"]
        results = d.check(tasks)
        assert len(results) == 1
        assert results[0].severity == "HIGH"
        assert results[0].fingerprint == "task_dead:trade_monitor"

    def test_accepts_suffixed_task_name(self):
        d = BackgroundTaskDetector()
        tasks = [f"{t}-1" for t in self._ALL]
        assert d.check(tasks) == []

    def test_multiple_missing(self):
        d = BackgroundTaskDetector()
        results = d.check([])
        assert len(results) == 5

    def test_extra_tasks_ignored(self):
        d = BackgroundTaskDetector()
        tasks = self._ALL + ["scanner", "some_other_task"]
        assert d.check(tasks) == []


# ---------------------------------------------------------------------------
# D3 — SigningHealthDetector
# ---------------------------------------------------------------------------

class TestSigningHealthDetector:
    def test_healthy(self):
        d = SigningHealthDetector()
        assert d.check({"360scalp-v2-signing": "Up 2 hours (healthy)"}) == []

    def test_absent(self):
        d = SigningHealthDetector()
        results = d.check({})
        assert len(results) == 1
        assert results[0].severity == "HIGH"
        assert results[0].fingerprint == "signing_service:absent"

    def test_unhealthy(self):
        d = SigningHealthDetector()
        results = d.check({"360scalp-v2-signing": "Up 10 minutes (unhealthy)"})
        assert len(results) == 1
        assert results[0].severity == "HIGH"
        assert "unhealthy" in results[0].fingerprint

    def test_exited(self):
        d = SigningHealthDetector()
        results = d.check({"360scalp-v2-signing": "Exited (1) 5 minutes ago"})
        assert len(results) == 1
        assert results[0].severity == "HIGH"


# ---------------------------------------------------------------------------
# D4 — EngineStatusDetector
# ---------------------------------------------------------------------------

class TestEngineStatusDetector:
    def test_healthy(self):
        d = EngineStatusDetector()
        assert d.check({"status": "Healthy", "mode": "live"}) == []

    def test_degraded(self):
        d = EngineStatusDetector()
        results = d.check({"status": "Degraded", "mode": "live"})
        assert len(results) == 1
        assert results[0].severity == "HIGH"
        assert results[0].fingerprint == "engine_status:degraded"

    def test_unreachable(self):
        d = EngineStatusDetector()
        results = d.check({"error": "Connection refused"})
        assert len(results) == 1
        assert results[0].severity == "HIGH"
        assert results[0].fingerprint == "engine_unreachable"


# ---------------------------------------------------------------------------
# D5 — HeartbeatAgeDetector
# ---------------------------------------------------------------------------

class TestHeartbeatAgeDetector:
    def test_fresh(self):
        d = HeartbeatAgeDetector(warn_sec=120, high_sec=300)
        assert d.check({"signal_history.json": 10.0}) == []

    def test_warn(self):
        d = HeartbeatAgeDetector(warn_sec=120, high_sec=300)
        results = d.check({"signal_history.json": 180.0})
        assert len(results) == 1
        assert results[0].severity == "WARN"
        assert results[0].fingerprint == "heartbeat_stale"

    def test_high(self):
        d = HeartbeatAgeDetector(warn_sec=120, high_sec=300)
        results = d.check({"signal_history.json": 400.0})
        assert len(results) == 1
        assert results[0].severity == "HIGH"

    def test_empty(self):
        d = HeartbeatAgeDetector()
        assert d.check({}) == []

    def test_uses_worst_file(self):
        d = HeartbeatAgeDetector(warn_sec=120, high_sec=300)
        ages = {"a.json": 10.0, "b.json": 250.0}
        results = d.check(ages)
        assert len(results) == 1
        assert results[0].severity == "WARN"


# ---------------------------------------------------------------------------
# D6 — ApiHealthDetector
# ---------------------------------------------------------------------------

class TestApiHealthDetector:
    def test_ok(self):
        d = ApiHealthDetector()
        assert d.check({"uptime_seconds": 3600, "version": "1.0"}) == []

    def test_error(self):
        d = ApiHealthDetector()
        results = d.check({"error": "timeout"})
        assert len(results) == 1
        assert results[0].severity == "WARN"


# ---------------------------------------------------------------------------
# D7 — SignalSilenceDetector
# ---------------------------------------------------------------------------

class TestSignalSilenceDetector:
    def test_silence_in_live_mode(self):
        d = SignalSilenceDetector()
        pulse = {"signals_today": 0, "uptime_seconds": 7200, "mode": "live"}
        results = d.check(pulse, mode="live")
        assert len(results) == 1
        assert results[0].severity == "WARN"
        assert results[0].fingerprint == "signal_silence"

    def test_no_trigger_in_off_mode(self):
        d = SignalSilenceDetector()
        pulse = {"signals_today": 0, "uptime_seconds": 7200}
        assert d.check(pulse, mode="off") == []

    def test_no_trigger_with_signals(self):
        d = SignalSilenceDetector()
        pulse = {"signals_today": 3, "uptime_seconds": 7200}
        assert d.check(pulse, mode="live") == []

    def test_no_trigger_fresh_engine(self):
        d = SignalSilenceDetector()
        # Uptime < 1h — silence expected after restart
        pulse = {"signals_today": 0, "uptime_seconds": 1800}
        assert d.check(pulse, mode="live") == []

    def test_no_trigger_on_pulse_error(self):
        d = SignalSilenceDetector()
        assert d.check({"error": "timeout"}, mode="live") == []


# ---------------------------------------------------------------------------
# D8 — RedisStalenessDetector
# ---------------------------------------------------------------------------

class TestRedisStalenessDetector:
    def test_fresh(self):
        d = RedisStalenessDetector(stale_sec=45)
        assert d.check("10") == []

    def test_stale(self):
        d = RedisStalenessDetector(stale_sec=45)
        results = d.check("120")
        assert len(results) == 1
        assert results[0].severity == "WARN"
        assert results[0].fingerprint == "redis_stale"

    def test_empty_output(self):
        d = RedisStalenessDetector()
        assert d.check("") == []

    def test_non_numeric(self):
        d = RedisStalenessDetector()
        assert d.check("(error) ERR") == []

    def test_boundary_exact(self):
        d = RedisStalenessDetector(stale_sec=45)
        # Exactly at threshold — should NOT trigger (≤ stale_sec)
        assert d.check("45") == []
        # One above — should trigger
        assert len(d.check("46")) == 1

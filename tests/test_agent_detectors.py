"""Unit tests for Tier 0 detectors.

All tests use plain dicts — no live network, no Docker, no Redis.
"""
from __future__ import annotations

import pytest

from app.agent.detectors import (
    ApiHealthDetector,
    BackgroundTaskDetector,
    CoreContainerDetector,
    EngineStatusDetector,
    NakedPositionDetector,
    RedisProbe,
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
                  "minutes_open": 5, "entry": 65000.0, "stop_loss": 0.0}]
        results = d.check(items)
        assert len(results) == 1
        assert results[0].severity == "HIGH"
        assert results[0].fingerprint == "naked_position:BTCUSDT:s1"

    def test_no_trigger_within_grace(self):
        d = NakedPositionDetector(grace_sec=90)
        items = [{"status": "ACTIVE", "symbol": "BTCUSDT", "signal_id": "s1",
                  "minutes_open": 1, "entry": 65000.0, "stop_loss": 0.0}]
        assert d.check(items) == []

    def test_no_trigger_with_valid_sl(self):
        d = NakedPositionDetector(grace_sec=90)
        items = [{"status": "ACTIVE", "symbol": "BTCUSDT", "signal_id": "s1",
                  "minutes_open": 5, "entry": 65000.0, "stop_loss": 64000.0}]
        assert d.check(items) == []

    def test_no_trigger_for_non_active_status(self):
        d = NakedPositionDetector(grace_sec=90)
        items = [{"status": "SL_HIT", "symbol": "BTCUSDT", "signal_id": "s1",
                  "minutes_open": 5, "entry": 65000.0, "stop_loss": 0.0}]
        assert d.check(items) == []

    def test_no_trigger_on_empty_symbol(self):
        # Phantom signal-tracking entry from the Redis facade in isolated
        # mode — empty symbol, zero entry, zero SL. Must NOT page.
        d = NakedPositionDetector(grace_sec=90)
        items = [{"status": "ACTIVE", "symbol": "", "signal_id": "SRFLIP-50A18331",
                  "minutes_open": 3, "entry": 0.0, "stop_loss": 0.0}]
        assert d.check(items) == []

    def test_no_trigger_on_zero_entry(self):
        d = NakedPositionDetector(grace_sec=90)
        items = [{"status": "ACTIVE", "symbol": "BTCUSDT", "signal_id": "s1",
                  "minutes_open": 5, "entry": 0.0, "stop_loss": 0.0}]
        assert d.check(items) == []

    def test_multiple_naked_positions(self):
        d = NakedPositionDetector(grace_sec=90)
        items = [
            {"status": "ACTIVE", "symbol": "BTCUSDT", "signal_id": "s1",
             "minutes_open": 5, "entry": 65000.0, "stop_loss": 0.0},
            {"status": "ACTIVE", "symbol": "ETHUSDT", "signal_id": "s2",
             "minutes_open": 10, "entry": 3500.0, "stop_loss": 0.0},
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
                  "minutes_open": 2, "entry": 65000.0, "stop_loss": 0.0}]
        assert len(d.check(items)) == 1

    def test_no_trigger_for_user_owned_stopless(self):
        # Manual trade builder: a user_owned position may be intentionally
        # stop-less (the user owns the exit). Must NOT page.
        d = NakedPositionDetector(grace_sec=90)
        items = [{"status": "ACTIVE", "symbol": "BTCUSDT", "signal_id": "s1",
                  "minutes_open": 5, "entry": 65000.0, "stop_loss": 0.0,
                  "protection_mode": "user_owned"}]
        assert d.check(items) == []

    def test_managed_stopless_still_triggers(self):
        # A managed (auto-dispatched) position without a stop is still the
        # naked-position invariant violation — must page.
        d = NakedPositionDetector(grace_sec=90)
        items = [{"status": "ACTIVE", "symbol": "BTCUSDT", "signal_id": "s1",
                  "minutes_open": 5, "entry": 65000.0, "stop_loss": 0.0,
                  "protection_mode": "managed"}]
        assert len(d.check(items)) == 1


# ---------------------------------------------------------------------------
# D2 — BackgroundTaskDetector
# ---------------------------------------------------------------------------

class TestBackgroundTaskDetector:
    # The 4 persistent engine loops. pretp_dispatcher is NOT included — it's
    # an event-driven singleton, never a long-lived task.
    _ALL = ["trade_monitor", "reconciler", "mark_price_feed",
            "funding_exit_watcher"]

    def test_no_trigger_all_present(self):
        d = BackgroundTaskDetector()
        assert d.check(self._ALL) == []

    def test_pretp_dispatcher_not_required(self):
        # A census without pretp_dispatcher must NOT fire — it's not expected.
        d = BackgroundTaskDetector()
        assert d.check(self._ALL) == []
        assert all(r.fingerprint != "task_dead:pretp_dispatcher"
                   for r in d.check(self._ALL))

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

    def test_empty_census_does_not_fire(self):
        # Empty list = census unavailable, not mass death. Covered elsewhere.
        d = BackgroundTaskDetector()
        assert d.check([]) == []

    def test_one_missing_among_real_tasks(self):
        d = BackgroundTaskDetector()
        tasks = ["trade_monitor", "reconciler", "mark_price_feed", "scanner"]
        results = d.check(tasks)
        assert len(results) == 1
        assert results[0].fingerprint == "task_dead:funding_exit_watcher"

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

    def test_engine_connected_true_is_silent(self):
        d = ApiHealthDetector()
        assert d.check({
            "uptime_seconds": 3600, "engine_connected": True,
            "engine_state_age_seconds": 12.0,
        }) == []

    def test_engine_disconnected_pages_high(self):
        """The 2026-07-27 shape: a clean 200 from an api container that has
        not heard from the engine in hours."""
        d = ApiHealthDetector()
        results = d.check({
            "uptime_seconds": 10800, "version": "0.0.2",
            "engine_connected": False, "engine_state_age_seconds": 9000.0,
        })
        assert len(results) == 1
        assert results[0].severity == "HIGH"
        assert results[0].fingerprint == "engine_disconnected"
        assert "150.0 min" in results[0].description

    def test_engine_never_reachable_reports_that_not_an_age(self):
        d = ApiHealthDetector()
        results = d.check({
            "uptime_seconds": 60, "engine_connected": False,
            "engine_state_age_seconds": None,
        })
        assert len(results) == 1
        assert "has not been reachable" in results[0].description

    def test_field_absent_stays_quiet(self):
        """Pre-upgrade engine build — don't page on deploy skew."""
        d = ApiHealthDetector()
        assert d.check({"uptime_seconds": 3600, "version": "0.0.1"}) == []


# ---------------------------------------------------------------------------
# D3b — CoreContainerDetector
# ---------------------------------------------------------------------------

class TestCoreContainerDetector:
    def test_all_up_is_silent(self):
        d = CoreContainerDetector()
        assert d.check({
            "360scalp-v2-engine": "Up 2 minutes (healthy)",
            "360scalp-v2-redis": "Up 2 minutes (healthy)",
        }) == []

    def test_the_outage_state_pages(self):
        """Verbatim docker ps output from the 2026-07-27 incident."""
        d = CoreContainerDetector()
        results = d.check({
            "360scalp-v2-engine": "Exited (137) 28 minutes ago",
            "360scalp-v2-redis": "Created",
            "360scalp-v2-api": "Up 3 hours (healthy)",
        })
        assert len(results) == 2
        assert {r.severity for r in results} == {"HIGH"}
        assert {r.fingerprint for r in results} == {
            "container_down:360scalp-v2-engine",
            "container_down:360scalp-v2-redis",
        }

    def test_absent_container_pages(self):
        d = CoreContainerDetector()
        results = d.check({"360scalp-v2-redis": "Up 5 minutes"})
        assert len(results) == 1
        assert results[0].fingerprint == "container_absent:360scalp-v2-engine"

    def test_booting_engine_is_not_a_page(self):
        """The engine reads unhealthy for minutes on every boot while it
        re-seeds history. Paging on that trains the owner to ignore this."""
        d = CoreContainerDetector()
        assert d.check({
            "360scalp-v2-engine": "Up 30 seconds (health: starting)",
            "360scalp-v2-redis": "Up 30 seconds (healthy)",
        }) == []

    def test_empty_ps_reports_blindness_not_mass_death(self):
        d = CoreContainerDetector()
        results = d.check({})
        assert len(results) == 1
        assert results[0].fingerprint == "docker_ps_unavailable"


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

def _ok(output: str) -> RedisProbe:
    return RedisProbe(ok=True, output=output, returncode=0)


class TestRedisStalenessDetector:
    def test_fresh(self):
        d = RedisStalenessDetector(stale_sec=45)
        assert d.check(_ok("10")) == []

    def test_stale(self):
        d = RedisStalenessDetector(stale_sec=45)
        results = d.check(_ok("120"))
        assert len(results) == 1
        assert results[0].severity == "WARN"
        assert results[0].fingerprint == "redis_stale"

    def test_probe_failure_pages_it_does_not_pass(self):
        """This assertion used to read ``== []`` and that was the 2026-07-27 bug.

        ``docker exec`` against a stopped redis container produces no stdout,
        so the probe returned "" and the detector called it healthy. The one
        condition it exists to catch was the one guaranteed to stay silent.

        **The property survives; the fingerprint moved (2026-08-18.)** The
        premise in the paragraph above turned out to be wrong about *this*
        case: ``docker exec`` against a stopped container exits **non-zero**,
        so it lands in ``exec_error`` and is covered by the parametrised test
        below. rc=0 with empty stdout is the opposite situation — redis
        answering nil for a missing key — and that is now
        ``snapshot_key_missing``, which points at the engine. Both still page
        HIGH; neither reads as all-clear, which is the property this test was
        written to protect. See ``tests/test_system_health.py`` for the split.
        """
        d = RedisStalenessDetector()
        for probe, expected in (
            (RedisProbe(ok=False, cause="no_output", returncode=0), "snapshot_key_missing"),
            (RedisProbe(ok=False, cause="exec_error", returncode=1,
                        detail="container is not running"), "redis_unreachable"),
        ):
            results = d.check(probe)
            assert len(results) == 1
            assert results[0].severity == "HIGH"
            assert results[0].fingerprint == expected

    @pytest.mark.parametrize(
        "probe, must_contain",
        [
            (
                RedisProbe(ok=False, cause="timeout",
                           detail="no response within 10s", attempts=2),
                ["timeout", "no response within 10s", "2 attempts"],
            ),
            (
                RedisProbe(ok=False, cause="exec_error", returncode=1,
                           detail="Error response from daemon: container is not running"),
                ["exec_error", "rc=1", "not running", "1 attempt"],
            ),
            (
                RedisProbe(ok=False, cause="exception",
                           detail="FileNotFoundError: docker"),
                ["exception", "FileNotFoundError"],
            ),
            (
                RedisProbe(ok=False, cause="not_run", detail="the cycle did not reach the probe"),
                ["not_run"],
            ),
        ],
    )
    def test_the_alert_carries_the_probes_own_words(self, probe, must_contain):
        """A counter is not a cause on a path that talks to another process.

        The pre-2026-08-16 description was one fixed sentence naming two
        causes and distinguishing neither, while ``rc`` and stderr went to a
        container log the owner cannot read from a phone. Every one of these
        cases is that same HIGH page with a different next move.
        """
        results = RedisStalenessDetector().check(probe)
        assert len(results) == 1
        assert results[0].fingerprint == "redis_unreachable"
        for token in must_contain:
            assert token in results[0].description, token
        assert results[0].raw["cause"] == probe.cause

    def test_non_numeric_is_its_own_state_not_all_clear(self):
        """Ran, answered, unreadable — the branch that used to ``return []``.

        Directly below a docstring saying not-measured and measured-fine must
        not share a return value, a ``(error) …`` reply read as all-clear. It
        is neither ``redis_unreachable`` (redis replied) nor ``redis_stale``
        (there is no idle time to compare), so it gets its own fingerprint.
        """
        results = RedisStalenessDetector().check(_ok("(error) ERR wrong number of arguments"))
        assert len(results) == 1
        assert results[0].fingerprint == "redis_probe_unreadable"
        assert results[0].severity == "WARN"
        assert "wrong number of arguments" in results[0].description

    def test_implausible_idletime_is_not_silently_healthy(self):
        for value in ("-1", "999999"):
            results = RedisStalenessDetector().check(_ok(value))
            assert len(results) == 1, value
            assert results[0].fingerprint == "redis_probe_unreadable", value

    def test_boundary_exact(self):
        d = RedisStalenessDetector(stale_sec=45)
        # Exactly at threshold — should NOT trigger (≤ stale_sec)
        assert d.check(_ok("45")) == []
        # One above — should trigger
        assert len(d.check(_ok("46"))) == 1

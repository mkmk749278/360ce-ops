"""The monitoring agent's cycle, driven for real across several cycles.

Until 2026-09-26 only the agent's probe helpers had tests; ``run()`` — fetch,
detect, notify, resolve, heartbeat — had none, and it carried a defect in the
resolve pass: an alert absent from this cycle's results was RESOLVED, and a
recovery paged, even when its detector had produced nothing only because its
input failed to fetch. Driven for two cycles, a HIGH naked-position alert
followed by one ``positions_diag`` timeout sent a RECOVERY for a position that
was still naked. "We could not look" read as "all clear" on the one alert that
must never be wrong in that direction.

Every test here drives the REAL ``run_cycle`` with the REAL detectors and the
REAL in-memory ``AlertStateStore``; only the transports (engine API, docker,
redis probe, notifier) are fakes.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.agent import heartbeat, runner
from app.agent.alert_state import AlertStateStore
from app.agent.detectors import RedisProbe

_HEALTHY_TASKS = ["trade_monitor", "reconciler", "mark_price_feed", "funding_exit_watcher"]
_HEALTHY_CONTAINERS = {
    "360scalp-v2-engine": "Up 2 hours",
    "360scalp-v2-redis": "Up 3 days",
    "360scalp-v2-signing": "Up 2 hours (healthy)",
}
_NAKED = {
    "status": "ACTIVE", "symbol": "BTCUSDT", "entry": 60000.0, "minutes_open": 10,
    "stop_loss": 0.0, "signal_id": "sig-naked-1",
}


class _Api:
    """Engine API fake. Each field is either a payload or an exception."""

    def __init__(self) -> None:
        self.pulse_payload: object = {"status": "OK", "mode": "live", "uptime_sec": 10}
        self.health_payload: object = {"status": "ok", "engine_connected": True}
        self.positions: object = {"items": []}
        self.tasks_payload: object = {"tasks": list(_HEALTHY_TASKS)}

    async def _answer(self, value):
        if isinstance(value, BaseException):
            raise value
        return value

    async def pulse(self):
        return await self._answer(self.pulse_payload)

    async def health(self):
        return await self._answer(self.health_payload)

    async def positions_diag(self):
        return await self._answer(self.positions)

    async def _get(self, path):
        assert path == "/internal/diag/tasks"
        return await self._answer(self.tasks_payload)

    async def auto_mode(self):
        return {"mode": "live"}


class _Notifier:
    def __init__(self) -> None:
        self.alerts: list[str] = []
        self.recoveries: list[str] = []
        self.heartbeats = 0

    async def send_alert(self, action) -> None:
        self.alerts.append(action.result.fingerprint)

    async def send_recovery(self, resolved) -> None:
        self.recoveries.append(resolved.fingerprint)

    async def ping_heartbeat(self) -> None:
        self.heartbeats += 1

    def armed_sinks(self) -> dict:
        return {}


class _Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.api = _Api()
        self.notifier = _Notifier()
        self.store = AlertStateStore(None, expiry_sec=3600, dedup_sec=1800)
        self.containers: dict = dict(_HEALTHY_CONTAINERS)
        self.probe = RedisProbe(ok=True, cause="ok", detail="", output="3")
        self.detectors = runner.Detectors(
            naked=runner.NakedPositionDetector(grace_sec=90),
            tasks=runner.BackgroundTaskDetector(),
            signing=runner.SigningHealthDetector(),
            core=runner.CoreContainerDetector(),
            engine_status=runner.EngineStatusDetector(),
            api_health=runner.ApiHealthDetector(),
            silence=runner.SignalSilenceDetector(),
            redis=runner.RedisStalenessDetector(stale_sec=45),
            restart=runner.EngineRestartDetector(window_sec=3600, loop_threshold=2),
        )

        async def _ps(*_a, **_k):
            if isinstance(self.containers, BaseException):
                raise self.containers
            return self.containers

        async def _probe(*_a, **_k):
            return self.probe

        async def _no_heartbeat(*_a, **_k):
            return None

        monkeypatch.setattr(runner, "_docker_ps_statuses", _ps)
        monkeypatch.setattr(runner, "_redis_idletime", _probe)
        monkeypatch.setattr(heartbeat, "publish", _no_heartbeat)

    async def cycle(self) -> runner.CycleReport:
        return await runner.run_cycle(
            api=self.api, alert_state=self.store, notifier=self.notifier,
            detectors=self.detectors,
        )


@pytest.fixture
def h(monkeypatch) -> _Harness:
    return _Harness(monkeypatch)


# ---------------------------------------------------------------------------
# The defect: a failed fetch must not page a recovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure", [
    {"error": "ReadTimeout('')", "endpoint": "/internal/diag/positions"},  # how EngineApiClient reports it
    TimeoutError("ReadTimeout"),                                          # a transport that raises
])
async def test_a_failed_positions_fetch_does_not_clear_a_naked_position(h, failure) -> None:
    h.api.positions = {"items": [_NAKED]}
    first = await h.cycle()
    assert "naked_position:BTCUSDT:sig-naked-1" in first.triggered
    assert h.notifier.alerts.count("naked_position:BTCUSDT:sig-naked-1") == 1

    h.api.positions = failure
    second = await h.cycle()
    assert "positions_diag" in second.blind
    assert "naked_position:BTCUSDT:sig-naked-1" in second.carried
    assert h.notifier.recoveries == [], "paged a RECOVERY for a position nobody could see"
    assert "naked_position:BTCUSDT:sig-naked-1" in await h.store.active_fingerprints()

    # Once positions are readable again AND the position is protected, it clears.
    h.api.positions = {"items": [dict(_NAKED, stop_loss=59000.0)]}
    third = await h.cycle()
    assert "naked_position:BTCUSDT:sig-naked-1" in third.resolved
    assert h.notifier.recoveries == ["naked_position:BTCUSDT:sig-naked-1"]


async def test_a_readable_empty_book_does_clear_the_alert(h) -> None:
    """Control: the carry-forward must not freeze alerts forever — when the
    source answers and the position is gone, the alert resolves."""
    h.api.positions = {"items": [_NAKED]}
    await h.cycle()
    h.api.positions = {"items": []}
    report = await h.cycle()
    assert report.resolved == {"naked_position:BTCUSDT:sig-naked-1"}
    assert h.notifier.recoveries == ["naked_position:BTCUSDT:sig-naked-1"]


async def test_a_blind_cycle_does_not_page_the_carried_alert_again(h) -> None:
    h.api.positions = {"items": [_NAKED]}
    await h.cycle()
    h.api.positions = {"error": "ReadTimeout('')"}
    await h.cycle()
    await h.cycle()
    assert h.notifier.alerts.count("naked_position:BTCUSDT:sig-naked-1") == 1


async def test_a_dead_task_survives_an_unavailable_census(h) -> None:
    h.api.tasks_payload = {"tasks": ["reconciler", "mark_price_feed", "funding_exit_watcher"]}
    await h.cycle()
    assert "task_dead:trade_monitor" in await h.store.active_fingerprints()
    h.api.tasks_payload = {"error": "ReadTimeout('')"}
    report = await h.cycle()
    assert "task_dead:trade_monitor" in report.carried
    assert h.notifier.recoveries == []


async def test_a_down_container_survives_docker_ps_failing(h) -> None:
    h.containers = dict(_HEALTHY_CONTAINERS, **{"360scalp-v2-engine": "Exited (137) 2 minutes ago"})
    await h.cycle()
    assert "container_down:360scalp-v2-engine" in await h.store.active_fingerprints()
    h.containers = {}
    report = await h.cycle()
    assert "container_down:360scalp-v2-engine" in report.carried
    assert "docker_ps_unavailable" in report.triggered
    assert "container_down:360scalp-v2-engine" not in h.notifier.recoveries


async def test_the_daily_kill_switch_alert_survives_an_unreachable_engine(h) -> None:
    h.api.pulse_payload = {"status": "Degraded", "mode": "live", "uptime_sec": 10}
    await h.cycle()
    assert "engine_status:degraded" in await h.store.active_fingerprints()
    h.api.pulse_payload = {"error": "ConnectError"}
    report = await h.cycle()
    assert "engine_status:degraded" in report.carried
    assert "engine_unreachable" in report.triggered
    assert h.notifier.recoveries == []


async def test_a_stale_snapshot_survives_an_unreadable_probe(h) -> None:
    h.probe = RedisProbe(ok=True, cause="ok", detail="", output="400")
    await h.cycle()
    assert "redis_stale" in await h.store.active_fingerprints()
    h.probe = RedisProbe(ok=False, cause="timeout", detail="docker exec timed out")
    report = await h.cycle()
    assert "redis_stale" in report.carried
    assert "redis_unreachable" in report.triggered
    assert h.notifier.recoveries == []


async def test_a_source_failure_alert_resolves_when_the_source_answers(h) -> None:
    """The alerts that ARE the source failing must not be carried — they
    are exactly what the recovery is about."""
    h.api.pulse_payload = {"error": "ConnectError"}
    await h.cycle()
    assert "engine_unreachable" in await h.store.active_fingerprints()
    h.api.pulse_payload = {"status": "OK", "mode": "live", "uptime_sec": 10}
    report = await h.cycle()
    assert "engine_unreachable" in report.resolved
    assert h.notifier.recoveries == ["engine_unreachable"]


async def test_a_clean_cycle_pings_the_heartbeat_and_a_raising_fetch_does_not(h) -> None:
    await h.cycle()
    assert h.notifier.heartbeats == 1
    h.api.positions = TimeoutError("boom")
    report = await h.cycle()
    assert report.cycle_ok is False
    assert h.notifier.heartbeats == 1


# ---------------------------------------------------------------------------
# Every fingerprint is classified — derived from detectors.py
# ---------------------------------------------------------------------------


def _emitted_fingerprint_prefixes() -> set[str]:
    src = (Path(runner.__file__).parent / "detectors.py").read_text()
    out = set()
    for m in re.finditer(r'fingerprint\s*=\s*f?"([^"{]+)', src):
        out.add(m.group(1))
    return out


def test_every_fingerprint_is_either_source_bound_or_a_source_failure() -> None:
    emitted = _emitted_fingerprint_prefixes()
    assert len(emitted) >= 15, "fingerprint parse found too little to mean anything"
    unclassified = sorted(
        fp for fp in emitted
        if runner.source_of(fp) is None and fp not in runner.SOURCE_FAILURE_ALERTS
    )
    assert not unclassified, (
        "a detector emits a fingerprint the resolve pass does not know how to "
        "gate — add it to ALERT_SOURCES (cleared only when its source answered) "
        f"or SOURCE_FAILURE_ALERTS: {unclassified}"
    )
    both = sorted(
        fp for fp in emitted
        if runner.source_of(fp) is not None and fp in runner.SOURCE_FAILURE_ALERTS
    )
    assert not both, f"classified twice: {both}"

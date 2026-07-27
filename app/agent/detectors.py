"""Tier 0 monitoring detectors.

Each detector is a pure function wrapped in a class. All inputs arrive via
method parameters — no hidden I/O — so unit tests pass plain dicts without
live network or Docker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


#: Sentinel a probe returns when it could not run at all, as distinct from
#: running and finding nothing wrong.  A detector that treats "no data" as
#: "no problem" is silent in exactly the outage it exists to catch — which is
#: what happened on 2026-07-27, when a stopped redis container made the
#: idletime probe return "" and RedisStalenessDetector read that as healthy.
PROBE_FAILED = "PROBE_FAILED"


@dataclass
class DetectorResult:
    severity: Literal["HIGH", "WARN"]
    fingerprint: str
    description: str
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# D1 — NakedPositionDetector  (HIGH, pages immediately)
# ---------------------------------------------------------------------------

class NakedPositionDetector:
    """OPEN position with no SL *geometry* stored past the grace window.

    Reads the /internal/diag/positions response (PositionDiagDetail list).

    LIMITATION: this checks ``sig.stop_loss`` (the signal's intended SL
    price), not whether a live protective STOP ORDER exists on Binance.
    The real JTOUSDT failure mode — signal has a valid SL price but the
    Binance stop order never confirmed — would show a non-zero stop_loss
    here and slip past this detector. Catching that requires the engine to
    publish the FSM's live ``sl_order_id`` per position to Redis; tracked
    as a follow-up engine telemetry PR. Until then this only flags the
    grosser failure of missing SL geometry on a genuine position.
    """

    name = "NakedPositionDetector"

    def __init__(self, grace_sec: int = 90) -> None:
        self._grace = grace_sec

    def check(self, diag_items: list[dict]) -> list[DetectorResult]:
        results: list[DetectorResult] = []
        for item in diag_items:
            if item.get("status") != "ACTIVE":
                continue
            # A real open position always carries a symbol and a positive
            # entry price. Entries in router.active_signals with an empty
            # symbol / zero entry are signal-tracking placeholders surfaced by
            # the Redis facade in isolated mode — NOT live Binance positions.
            # Skip them so we don't page on phantom entries.
            symbol = (item.get("symbol") or "").strip()
            entry = float(item.get("entry") or 0.0)
            if not symbol or entry <= 0.0:
                continue
            minutes_open: int = item.get("minutes_open") or 0
            if minutes_open * 60 <= self._grace:
                continue
            # Manual trade builder (2026-07-18): a ``user_owned`` position is a
            # user-directed manual take and may be intentionally stop-less —
            # the user owns the exit. The naked-position invariant applies only
            # to engine-``managed`` (auto-dispatched) positions, so never page
            # a user_owned one. ``protection_mode`` is published by the engine
            # diag; absent (older engine) → treat as ``managed`` (current
            # behaviour), so this is safe forward-compat.
            if (item.get("protection_mode") or "managed") == "user_owned":
                continue
            sl: float = item.get("stop_loss") or 0.0
            if sl > 0.0:
                continue
            signal_id = item.get("signal_id", "unknown")
            results.append(DetectorResult(
                severity="HIGH",
                fingerprint=f"naked_position:{symbol}:{signal_id}",
                description=(
                    f"OPEN {symbol} (sig {signal_id[:8]}) open {minutes_open}m "
                    f"with no stored stop-loss."
                ),
                raw=item,
            ))
        return results


# ---------------------------------------------------------------------------
# D2 — BackgroundTaskDetector  (HIGH, pages immediately)
# ---------------------------------------------------------------------------

# The four persistent engine loops. pretp_dispatcher is deliberately NOT
# here — it's an event-driven singleton that only spawns per-symbol
# pd_track_* tasks at position open, so it never appears as a long-lived
# task and would false-positive.
_REQUIRED_TASKS = (
    "trade_monitor",
    "reconciler",
    "mark_price_feed",
    "funding_exit_watcher",
)


class BackgroundTaskDetector:
    """All critical engine asyncio tasks must be alive.

    Reads the /internal/diag/tasks response, which the engine now answers
    correctly in both single-process and isolated mode (the engine container
    publishes its task census to Redis; the API facade serves it). Task names
    may carry a numeric suffix (e.g. 'trade_monitor-1') so we match by prefix.

    Guard: an empty task list means the census itself is unavailable (engine
    snapshot stale / endpoint error), not that every loop died — that failure
    mode is covered by the engine-status and snapshot-freshness detectors, so
    we skip rather than fire five HIGH pages off one missing census.
    """

    name = "BackgroundTaskDetector"

    def __init__(self, required: tuple[str, ...] = _REQUIRED_TASKS) -> None:
        self._required = required

    def check(self, tasks: list[str]) -> list[DetectorResult]:
        # Empty census = source unavailable, not a mass task death. Skip.
        if not tasks:
            return []
        results: list[DetectorResult] = []
        for name in self._required:
            found = any(t == name or t.startswith(f"{name}-") for t in tasks)
            if not found:
                results.append(DetectorResult(
                    severity="HIGH",
                    fingerprint=f"task_dead:{name}",
                    description=f"Engine task '{name}' absent from asyncio task census.",
                    raw={"tasks": tasks, "missing": name},
                ))
        return results


# ---------------------------------------------------------------------------
# D3b — CoreContainerDetector  (HIGH, pages immediately)
# ---------------------------------------------------------------------------

class CoreContainerDetector:
    """The engine and redis containers must be present and running.

    Added 2026-07-27. An interrupted ``docker compose`` recreate left the
    engine ``Exited (137)`` and redis/watchdog ``Created`` for two and a half
    hours. Nothing paged, for a reason worth keeping in mind: the *only*
    container anyone was watching was the signing service, and every other
    detector inferred engine health from data the api container was still
    happily serving out of a frozen cache. Watch the containers directly —
    it is the one check that cannot be fooled by stale data, because it
    doesn't read any.

    Deliberately does NOT require ``(healthy)`` the way the signing detector
    does: the engine reports unhealthy for several minutes on every boot
    while it re-seeds history (480s healthcheck grace), and paging on that
    would train the owner to ignore this alert. Presence and a running state
    are the property that matters here; a container that is up but wedged is
    covered by TaskCensusDetector and the engine-disconnected path.
    """

    name = "CoreContainerDetector"
    _CONTAINERS = ("360scalp-v2-engine", "360scalp-v2-redis")

    def check(self, container_statuses: dict[str, str]) -> list[DetectorResult]:
        if not container_statuses:
            # docker ps itself failed — not "no containers exist". Reporting
            # every container missing would be a lie about which thing broke.
            return [DetectorResult(
                severity="HIGH",
                fingerprint="docker_ps_unavailable",
                description=(
                    "docker ps returned nothing — the agent cannot see the "
                    "engine stack at all. Container-level monitoring is blind "
                    "until this clears."
                ),
                raw={"containers": container_statuses},
            )]

        results: list[DetectorResult] = []
        for name in self._CONTAINERS:
            status = container_statuses.get(name)
            if status is None:
                results.append(DetectorResult(
                    severity="HIGH",
                    fingerprint=f"container_absent:{name}",
                    description=f"Container '{name}' not found in docker ps.",
                    raw={"containers": container_statuses},
                ))
                continue
            if not status.lower().startswith("up"):
                # "Exited (137) 28 minutes ago", "Created", "Restarting" —
                # every one of these is the engine not running.
                results.append(DetectorResult(
                    severity="HIGH",
                    fingerprint=f"container_down:{name}",
                    description=(
                        f"Container '{name}' is not running: {status!r}. "
                        "A 'Created' or 'Exited' core container usually means "
                        "a deploy was interrupted part-way."
                    ),
                    raw={"container": name, "status": status},
                ))
        return results


# ---------------------------------------------------------------------------
# D3 — SigningHealthDetector  (HIGH, pages immediately)
# ---------------------------------------------------------------------------

class SigningHealthDetector:
    """Signing container must be present and healthy."""

    name = "SigningHealthDetector"
    _CONTAINER = "360scalp-v2-signing"

    def check(self, container_statuses: dict[str, str]) -> list[DetectorResult]:
        status = container_statuses.get(self._CONTAINER)
        if status is None:
            return [DetectorResult(
                severity="HIGH",
                fingerprint="signing_service:absent",
                description=f"Signing container '{self._CONTAINER}' not found in docker ps.",
                raw={"containers": container_statuses},
            )]
        # Docker reports health as "(healthy)" or "(unhealthy)" — check for
        # the exact parenthesised token so "unhealthy" doesn't match "(healthy)".
        if "(healthy)" not in status.lower():
            return [DetectorResult(
                severity="HIGH",
                fingerprint=f"signing_service:{status[:40]}",
                description=f"Signing container not healthy: {status!r}.",
                raw={"container": self._CONTAINER, "status": status},
            )]
        return []


# ---------------------------------------------------------------------------
# D4 — EngineStatusDetector  (HIGH, pages immediately)
# ---------------------------------------------------------------------------

class EngineStatusDetector:
    """Engine pulse 'Degraded' means the daily kill-switch tripped."""

    name = "EngineStatusDetector"

    def check(self, pulse: dict) -> list[DetectorResult]:
        if "error" in pulse:
            return [DetectorResult(
                severity="HIGH",
                fingerprint="engine_unreachable",
                description=f"Engine /api/pulse unreachable: {pulse['error']}",
                raw=pulse,
            )]
        status: str = pulse.get("status", "")
        if status == "Degraded":
            return [DetectorResult(
                severity="HIGH",
                fingerprint="engine_status:degraded",
                description="Engine status Degraded — daily kill-switch has tripped.",
                raw=pulse,
            )]
        return []


# NOTE: A file-mtime "HeartbeatAgeDetector" was removed here. It watched
# signal_performance.json / signal_history.json, but those files only update
# on signal lifecycle events — not every scan cycle — so a healthy-but-quiet
# engine looked "stale" and it false-paged. Engine/scanner liveness is
# covered correctly by RedisStalenessDetector (snapshot:tickers freshness)
# and EngineStatusDetector (pulse status), which read engine-published state.


# ---------------------------------------------------------------------------
# D6 — ApiHealthDetector  (WARN)
# ---------------------------------------------------------------------------

class ApiHealthDetector:
    """Engine /api/health must respond, AND must still hear the engine.

    The second half is the 2026-07-27 lesson. In isolated mode the thing
    answering /api/health is the *api* container, which caches the engine's
    last-good state in memory and keeps serving it forever. On that day the
    engine and redis were both dead for two and a half hours while this
    detector saw a clean 200 and every other detector read plausible numbers
    out of the frozen snapshot. A subscriber's screenshot was the alert.

    ``engine_connected`` is the one field a freeze cannot fake: the api
    container derives it from *when redis last answered it*, not from the
    payload's contents. Absent (older engine build) → skip, don't guess.
    """

    name = "ApiHealthDetector"

    def check(self, health: dict) -> list[DetectorResult]:
        if "error" in health:
            return [DetectorResult(
                severity="WARN",
                fingerprint="api_health:error",
                description=f"Engine /api/health error: {health['error']}",
                raw=health,
            )]

        connected = health.get("engine_connected")
        if connected is None or connected is True:
            # None = pre-upgrade engine that doesn't publish the field. Stay
            # quiet rather than page on a deploy skew; the container-status
            # detector covers a genuinely dead engine independently.
            return []

        age = health.get("engine_state_age_seconds")
        if isinstance(age, (int, float)):
            detail = f"last engine state {float(age)/60.0:.1f} min old"
        else:
            detail = "engine has not been reachable since the API started"
        return [DetectorResult(
            severity="HIGH",
            fingerprint="engine_disconnected",
            description=(
                f"API container is serving WITHOUT a live engine — {detail}. "
                "Everything the app shows is last-known-good, not live: "
                "signals, pulse and positions are all frozen. Check the "
                "engine and redis containers."
            ),
            raw=health,
        )]


# ---------------------------------------------------------------------------
# D7 — SignalSilenceDetector  (WARN)
# ---------------------------------------------------------------------------

class SignalSilenceDetector:
    """No signals emitted today while engine is in live auto-mode."""

    name = "SignalSilenceDetector"

    def check(self, pulse: dict, mode: str) -> list[DetectorResult]:
        if "error" in pulse:
            return []
        if mode != "live":
            return []
        signals_today: int = pulse.get("signals_today", -1)
        if signals_today != 0:
            return []
        uptime: float = pulse.get("uptime_seconds", 0.0) or 0.0
        if uptime < 3600:
            # Engine just restarted — silence is expected for first hour
            return []
        return [DetectorResult(
            severity="WARN",
            fingerprint="signal_silence",
            description=(
                f"Engine in live mode, uptime {uptime/3600:.1f}h, but signals_today=0. "
                "Scanner may be fully suppressed."
            ),
            raw={"pulse": pulse, "mode": mode},
        )]


# ---------------------------------------------------------------------------
# D8 — RedisStalenessDetector  (WARN)
# ---------------------------------------------------------------------------

class RedisStalenessDetector:
    """Engine Redis snapshot:tickers key must be recently written.

    Uses OBJECT IDLETIME which returns seconds since the key was last
    accessed.  snapshot:tickers is written every ~15s by SnapshotWriter.
    """

    name = "RedisStalenessDetector"

    def __init__(self, stale_sec: int = 45) -> None:
        self._stale = stale_sec

    def check(self, idletime_output: str) -> list[DetectorResult]:
        line = idletime_output.strip()
        if line == PROBE_FAILED or not line:
            # The probe could not run — redis container stopped, docker error,
            # timeout. Pre-2026-07-27 this returned [] and a dead redis was
            # indistinguishable from a healthy one. An unmeasurable dependency
            # is a HIGH, not a shrug.
            return [DetectorResult(
                severity="HIGH",
                fingerprint="redis_unreachable",
                description=(
                    "Could not read snapshot:tickers idletime from the engine "
                    "redis container — it is stopped, or docker exec failed. "
                    "With redis down the engine cannot publish and the API "
                    "serves frozen state."
                ),
                raw={"idletime_output": idletime_output},
            )]
        try:
            idle_sec = int(line)
        except ValueError:
            return []
        if idle_sec < 0 or idle_sec > 86400:
            return []
        if idle_sec <= self._stale:
            return []
        return [DetectorResult(
            severity="WARN",
            fingerprint="redis_stale",
            description=(
                f"Engine Redis snapshot:tickers idle {idle_sec}s "
                f"(threshold {self._stale}s). SnapshotWriter may be stuck."
            ),
            raw={"idle_sec": idle_sec},
        )]

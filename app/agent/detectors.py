"""Tier 0 monitoring detectors.

Each detector is a pure function wrapped in a class. All inputs arrive via
method parameters — no hidden I/O — so unit tests pass plain dicts without
live network or Docker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


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

_REQUIRED_TASKS = (
    "trade_monitor",
    "reconciler",
    "mark_price_feed",
    "funding_exit_watcher",
    "pretp_dispatcher",
)


class BackgroundTaskDetector:
    """All critical engine asyncio tasks must be alive.

    Reads the /internal/diag/tasks response.  Task names may carry a
    numeric suffix (e.g. 'trade_monitor-1') so we match by prefix.
    """

    name = "BackgroundTaskDetector"

    def __init__(self, required: tuple[str, ...] = _REQUIRED_TASKS) -> None:
        self._required = required

    def check(self, tasks: list[str]) -> list[DetectorResult]:
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
    """Engine /api/health must respond without error."""

    name = "ApiHealthDetector"

    def check(self, health: dict) -> list[DetectorResult]:
        if "error" in health:
            return [DetectorResult(
                severity="WARN",
                fingerprint="api_health:error",
                description=f"Engine /api/health error: {health['error']}",
                raw=health,
            )]
        return []


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
        if not line:
            return []
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

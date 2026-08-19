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


@dataclass(frozen=True)
class RedisProbe:
    """What the ``docker exec … redis-cli OBJECT IDLETIME`` probe actually did.

    This replaced a bare ``PROBE_FAILED`` sentinel string on 2026-08-16.  The
    sentinel could say *that* the probe failed and never *why*, so the HIGH
    page it raised read "it is stopped, or docker exec failed" — two causes
    named, neither distinguished, and four different next moves behind them:
    a stopped container, a docker daemon error, a probe that blew its deadline
    because the host was busy, and redis answering something we cannot parse.
    The rejection was already in hand — ``_redis_idletime`` logged ``rc=`` and
    the stderr tail — and then threw it away into a container log the owner
    cannot read from a phone.

    Same defect the trail governor's ``place_failed`` counter carried
    (2026-08-10, CLAUDE.md): **a counter is not a cause on a path that talks
    to another process.**  Keep the probe's own words and put them where the
    page is.

    ``ok=True`` means the probe ran and produced output; it says nothing about
    whether that output is *good*.  Grading it is the detector's job.
    """

    ok: bool
    output: str = ""
    #: "" when ok. Otherwise "timeout" | "exec_error" | "no_output" |
    #: "exception" | "not_run" — each has a different next move.
    cause: str = ""
    returncode: int | None = None
    #: The probe's own words: stderr tail, or the exception text.
    detail: str = ""
    #: How many times the probe was tried in this cycle before giving up.
    #: One timeout is not evidence redis is gone; two in a row is worth more.
    attempts: int = 1

    def summary(self) -> str:
        """One line naming the cause — safe to put in an alert description."""
        if self.ok:
            return f"ok: {self.output!r}"
        bits = [self.cause or "unknown"]
        if self.returncode is not None:
            bits.append(f"rc={self.returncode}")
        if self.detail:
            bits.append(self.detail)
        return " · ".join(bits)


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

#: One sentence per probe failure cause, naming the next move rather than
#: restating the failure.  Keyed on ``RedisProbe.cause``; a cause this map has
#: never heard of falls back to a neutral phrase and the raw ``summary()``
#: still carries the probe's own words — the same rule the data-intake card
#: follows, where copy looks a reason up instead of enumerating its own keys.
_UNREACHABLE_CAUSE_COPY: dict[str, str] = {
    "timeout": (
        "docker exec did not return before the probe's deadline. That is as "
        "often the host being too busy to start a process as it is redis "
        "being gone"
    ),
    "exec_error": "docker exec exited non-zero, which is what a stopped container looks like",
    # Deliberately absent from this map: ``no_output`` with rc=0. It never
    # reaches this alert any more — see ``_snapshot_key_missing`` below.
    "no_output": "docker exec exited cleanly and printed nothing",
    "exception": "the probe itself raised",
    "not_run": "the probe never ran this cycle",
}


def _unreadable(output: str, why: str) -> DetectorResult:
    """Redis answered and we cannot grade the answer.

    Deliberately WARN and a fingerprint of its own: this is **not**
    ``redis_unreachable`` (redis replied) and **not** ``redis_stale`` (we have
    no idle time to compare), and folding it into either would report a fault
    that is not happening.
    """
    return DetectorResult(
        severity="WARN",
        fingerprint="redis_probe_unreadable",
        description=(
            "redis-cli answered the snapshot:tickers idletime probe with "
            f"something that is not a usable idle time ({why}): {output[:120]!r}. "
            "Redis is reachable — this is the probe or the key, not the "
            "container. Snapshot freshness is UNGRADED until it clears."
        ),
        raw={"output": output[:200], "why": why},
    )


def _snapshot_key_missing(probe: RedisProbe, key: str) -> DetectorResult:
    """redis answered, and what it said was *nothing* — the key is not there.

    This is the 2026-08-18 defect. The owner was paged HIGH, repeatedly and
    with a recovery between each, reading::

        redis_unreachable — Could not read snapshot:tickers idletime from the
        engine redis container … Probe said: no_output · rc=0 · exited 0 and
        printed nothing.

    Every word after the fingerprint was true and the fingerprint was wrong.
    ``docker exec`` exited **0**, which it cannot do against a stopped
    container (that is ``exec_error``), and ``redis-cli`` exits non-zero when
    it cannot reach the server. So rc=0 is *positive evidence that redis
    answered* — and ``redis-cli`` prints a nil bulk reply as nothing at all
    when stdout is not a TTY. The empty output means the KEY was absent, not
    the container.

    Which makes it an **engine** fault, and a serious one. Every ``snapshot:*``
    key carries a TTL of twice its write interval (360-v2
    ``src/api/snapshot_store.py``: ``snapshot:tickers`` is written every ~15s
    with a 60s TTL), so an absent key means the SnapshotWriter missed several
    cycles in a row. In isolated mode the api container reads those keys and
    nothing else — so the same stall that expires the key is exactly what makes
    the dashboard stop answering, which is the second symptom the owner
    reported and had no way to connect to the first.

    Still HIGH: the engine has stopped publishing and every engine-backed page
    is going empty. What changes is that the alert now names the container that
    can actually be fixed, and says what to look at.
    """
    return DetectorResult(
        severity="HIGH",
        fingerprint="snapshot_key_missing",
        description=(
            f"Redis is UP and answering — and the key '{key}' is not there. "
            "redis-cli exited 0 with an empty reply, which is a nil result for "
            "a missing key; a stopped container or an unreachable server both "
            "exit non-zero, so this is not a redis fault. Every snapshot key "
            "carries a TTL of twice its write interval, so an absent key means "
            "the ENGINE's SnapshotWriter has missed several cycles. While this "
            "stands the api container has nothing to serve and every "
            "engine-backed ops page reads empty. Look at the engine container "
            "(restart count, healthcheck) on Ops → System, not at redis."
        ),
        raw={
            "cause": probe.cause,
            "returncode": probe.returncode,
            "detail": probe.detail,
            "attempts": probe.attempts,
            "key": key,
            "points_at": "360scalp-v2-engine",
        },
    )


class RedisStalenessDetector:
    """Engine Redis snapshot:tickers key must be recently written.

    Uses OBJECT IDLETIME which returns seconds since the key was last
    accessed.  snapshot:tickers is written every ~15s by SnapshotWriter.
    """

    name = "RedisStalenessDetector"

    def __init__(self, stale_sec: int = 45) -> None:
        self._stale = stale_sec

    #: An idletime this large is not a stale snapshot writer, it is a reading
    #: we do not believe — snapshot:tickers is rewritten every 15s, so a full
    #: day of idleness means we are looking at the wrong key or the wrong box.
    _IMPLAUSIBLE_SEC = 86400

    #: The key the probe reads. Named here so the alert can say which one.
    _KEY = "snapshot:tickers"

    def check(self, probe: RedisProbe) -> list[DetectorResult]:
        if not probe.ok and probe.cause == "no_output" and probe.returncode == 0:
            # Redis answered and the key was absent. Two facts, one of which
            # used to be reported as the other — see _snapshot_key_missing.
            return [_snapshot_key_missing(probe, self._KEY)]
        if not probe.ok:
            # The probe could not run. Pre-2026-07-27 this returned [] and a
            # dead redis was indistinguishable from a healthy one. An
            # unmeasurable dependency is a HIGH, not a shrug — and since
            # 2026-08-16 it names WHICH way it failed, because "stopped" and
            # "docker exec timed out on a busy host" are the same alert with
            # different next moves.
            return [DetectorResult(
                severity="HIGH",
                fingerprint="redis_unreachable",
                description=(
                    "Could not read snapshot:tickers idletime from the engine "
                    f"redis container — {_UNREACHABLE_CAUSE_COPY.get(probe.cause, 'the probe failed')}. "
                    f"Probe said: {probe.summary()}. "
                    f"Failed on {probe.attempts} attempt"
                    f"{'' if probe.attempts == 1 else 's'} this cycle. "
                    "Check for a container alert beside this one: if redis is "
                    "genuinely down the engine cannot publish and the API "
                    "serves frozen state; if it is not, this is the host, not "
                    "redis."
                ),
                raw={
                    "cause": probe.cause,
                    "returncode": probe.returncode,
                    "detail": probe.detail,
                    "attempts": probe.attempts,
                },
            )]

        line = probe.output.strip()
        try:
            idle_sec = int(line)
        except ValueError:
            # Ran, answered, and the answer is not a number. This branch used
            # to `return []` — all clear — one branch below a docstring saying
            # not-measured and measured-fine must not share a return value.
            # It is its own state: redis is answering (so this is not
            # `redis_unreachable`) and we cannot grade it (so it is not
            # health either).
            return [_unreadable(line, "not an integer")]
        if idle_sec < 0 or idle_sec > self._IMPLAUSIBLE_SEC:
            return [_unreadable(line, f"outside 0..{self._IMPLAUSIBLE_SEC}s")]
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


# ---------------------------------------------------------------------------
# D9 — EngineRestartDetector  (HIGH on a loop, WARN on one restart)
# ---------------------------------------------------------------------------

class EngineRestartDetector:
    """The engine restarting on its own, which nothing on the box could see.

    Measured live 2026-08-19: scan cycles of 9.2s to 402.5s against a 15s
    target. ``healthcheck.py`` fails when the scanner's heartbeat file is older
    than 120s and compose runs it every 30s with ``retries: 3``, so a stale beat
    flips the container unhealthy and the autoheal sidecar restarts it. Each
    restart takes every ``snapshot:*`` key past its TTL, so the dashboard stops
    answering and the Lumin app reads "No signals yet".

    **The beat used to be written only at the END of a cycle**, which made cycle
    wall-time and heartbeat age one number and every slow cycle a restart — the
    loop that ran all of 2026-08-19. Engine #971 moved it onto PROGRESS (a
    symbol finished), so a stale beat now means the loop stopped advancing.
    This detector is unaffected by that: it keys on uptime going backwards, not
    on any theory of why. Only its *description* changed, and that mattered —
    the sentence it sends to a phone is what decides where the reader looks
    next, and it had been naming a cause that is no longer the cause.

    **Three things independently hid it, and each one is why this detector is
    shaped the way it is:**

    * ``RestartCount`` cannot see it. Docker increments that only for restarts
      made by the container's *restart policy*; autoheal issues a manual
      restart, which does not, and a ``compose`` recreate starts the count
      over. It read ``0`` throughout.
    * ``CoreContainerDetector`` cannot see it either. It asks whether the
      container is *running*, and a restart that completes between two 60s
      poll cycles never presents as anything but ``Up``.
    * The engine's own counters reset, so ``signals_today`` reads 0 again and
      the quietness looks like the market.

    So this keys on the one thing a restart always does: **uptime goes
    backwards.** The engine publishes ``uptime_seconds`` in its pulse, and a
    reading lower than the previous one means the process it describes is not
    the process we saw last cycle. That is a fact about the engine, read on the
    engine's own clock — this detector never times anything itself, by the same
    rule that keeps ops from grading engine freshness on ops' clock.

    **One restart is a WARN, a loop is a HIGH**, and they are never pooled. A
    single restart is usually autoheal working: a wedged scan loop cleared by a
    bounce, which is the design. A *loop* is the failure — each cycle re-seeds
    75 pairs over REST and rebuilds every indicator cache cold, so the cure
    pushes the next cycle further past the deadline that triggered it.
    """

    name = "EngineRestartDetector"

    def __init__(self, window_sec: int = 3600, loop_threshold: int = 2) -> None:
        #: How far back a restart still counts toward the loop verdict.
        self._window = window_sec
        #: Restarts inside the window that make it a loop rather than an event.
        self._loop_threshold = loop_threshold
        #: Monotonic-ish history of observed restart wall-times.
        self._restarts: list[float] = []
        self._last_uptime: float | None = None

    #: Below this, two consecutive pulses can legitimately disagree — the
    #: engine's uptime is a float and the API may serve a snapshot written a
    #: moment earlier. A restart moves it by minutes, never by a second.
    _NOISE_SEC = 5.0

    def check(self, pulse: dict, now: float) -> list[DetectorResult]:
        raw = pulse.get("uptime_seconds") if isinstance(pulse, dict) else None
        try:
            uptime = float(raw)
        except (TypeError, ValueError):
            # Unknown is not "no restart". But it is also not evidence OF one,
            # and the engine being unreachable already has its own detector —
            # raising a second alert for one event is how a page stops being
            # read. Forget the previous reading so the next comparison is not
            # made across a gap we cannot account for.
            self._last_uptime = None
            return []

        previous = self._last_uptime
        self._last_uptime = uptime

        if previous is None:
            # First reading of this agent's life. A restart may well have
            # happened before we started watching; claiming one on no evidence
            # would page on every agent deploy.
            return []
        if uptime >= previous - self._NOISE_SEC:
            return []

        # Uptime went backwards: this is a different process than last cycle.
        self._restarts.append(now)
        self._restarts = [t for t in self._restarts if now - t <= self._window]
        count = len(self._restarts)
        mins = int(self._window // 60)

        if count >= self._loop_threshold:
            return [DetectorResult(
                severity="HIGH",
                fingerprint="engine_restart_loop",
                description=(
                    f"The engine has restarted {count} times in the last {mins} "
                    f"minutes (uptime fell {previous:.0f}s -> {uptime:.0f}s). "
                    "This is an autoheal loop, not a single bounce: each "
                    "restart re-seeds every pair over REST and rebuilds the "
                    "indicator caches cold, which pushes the next cycle further "
                    "past the deadline, and every restart expires the "
                    "snapshot:* keys so the dashboard and the app feed go empty "
                    "while it runs. Since 2026-08-19 the healthcheck grades "
                    "whether the scan loop is ADVANCING (the scanner beats when "
                    "a symbol finishes), not how long a cycle takes — so this "
                    "firing means the loop stopped advancing, or the beat is "
                    "not reaching the file. Read /system/liveness and check "
                    "'Beats on progress' FIRST: 'not reported' means the engine "
                    "predates that fix, and a beat count that is not climbing "
                    "means it is not working. docker RestartCount will read 0 "
                    "either way, because autoheal restarts are manual restarts."
                ),
                raw={
                    "restarts_in_window": count,
                    "window_sec": self._window,
                    "uptime_before": previous,
                    "uptime_now": uptime,
                },
            )]

        return [DetectorResult(
            severity="WARN",
            fingerprint="engine_restarted",
            description=(
                f"The engine restarted (uptime fell {previous:.0f}s -> "
                f"{uptime:.0f}s). One restart is usually autoheal working — a "
                "wedged scan loop cleared by a bounce. It is worth knowing "
                "about because docker's RestartCount cannot show it and the "
                f"engine's own counters reset. A second one inside {mins} "
                "minutes pages as a loop."
            ),
            raw={
                "restarts_in_window": count,
                "window_sec": self._window,
                "uptime_before": previous,
                "uptime_now": uptime,
            },
        )]

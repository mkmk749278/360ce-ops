"""Main monitoring agent poll loop.

Entry point: python -m app.agent.runner

Each 60s cycle:
  1. Fetch data from engine (API + data volume + docker)
  2. Run all 8 Tier 0 detectors
  3. Resolve any fingerprints that were triggered last cycle but not this one
  4. Notify via Telegram for new/escalated alerts and recoveries
  5. Ping healthchecks.io (Tier 2) — only if the full cycle completed cleanly
"""
from __future__ import annotations

import asyncio
import logging
import os

from app.config import load_settings
from app.data_sources.engine_api import EngineApiClient
from app.agent.detectors import (
    ApiHealthDetector,
    BackgroundTaskDetector,
    CoreContainerDetector,
    DetectorResult,
    EngineStatusDetector,
    NakedPositionDetector,
    RedisProbe,
    RedisStalenessDetector,
    SignalSilenceDetector,
    SigningHealthDetector,
)
from app.agent import heartbeat
from app.agent.alert_state import AlertStateStore
from app.agent.notifier import Notifier
from app.device_registry import DeviceRegistry
from app.fcm import FcmSender

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("agent.runner")

# ---------------------------------------------------------------------------
# Docker helpers (direct subprocess — no engine exec, just host daemon)
# ---------------------------------------------------------------------------

#: How long a single docker probe may take before we give up on it.
DOCKER_PROBE_TIMEOUT_S = float(os.getenv("AGENT_DOCKER_PROBE_TIMEOUT_S", "10"))

#: How many times the redis idletime probe is tried within ONE cycle before
#: it is declared failed.  See ``_redis_idletime`` for why this is not a
#: relaxation of the alert.
REDIS_PROBE_ATTEMPTS = max(1, int(os.getenv("AGENT_REDIS_PROBE_ATTEMPTS", "2")))


async def _kill_probe(proc: asyncio.subprocess.Process | None) -> None:
    """Kill a probe subprocess that outlived its deadline.

    ``asyncio.wait_for`` cancels the *coroutine*, not the process: a timed-out
    ``docker exec`` keeps running, and the ``redis-cli`` it started inside the
    container with it.  So the probe that timed out *because the host was
    busy* left another process behind to make the host busier — once a minute,
    forever, on a box the engine's own API tunables describe as 1-vCPU
    (``src/api/server.py``: "Tuned for a 1-vCPU VPS").  A monitoring agent
    that degrades the thing it monitors is worse than no agent.

    Output on these probes is a few bytes, so killing and reaping cannot
    deadlock on a full pipe.
    """
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except Exception:
        # Reaping failed too. Say so — a probe we cannot even kill is the
        # shape of a wedged docker daemon, and that is worth a log line
        # rather than a silent leak.
        log.warning("could not reap timed-out docker probe pid=%s", proc.pid)


async def _exec_capture(
    argv: tuple[str, ...],
    timeout: float = DOCKER_PROBE_TIMEOUT_S,
) -> tuple[int | None, str, str, str]:
    """Run ``argv``, returning ``(returncode, stdout, stderr, cause)``.

    ``cause`` is ``""`` when the process ran to completion (whatever its exit
    code), else ``"timeout"`` or ``"exception"``.  A completed run with a
    non-zero code is a *result*, not a failure of the probe — the caller
    decides what it means.
    """
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
            "",
        )
    # asyncio.TimeoutError is TimeoutError on 3.11+, an OSError subclass —
    # it must be caught ahead of the generic Exception below or a timeout
    # reports as an exception and the child is still never killed.
    except (asyncio.TimeoutError, TimeoutError):
        await _kill_probe(proc)
        return None, "", f"no response within {timeout:.0f}s", "timeout"
    except Exception as exc:
        await _kill_probe(proc)
        return None, "", f"{type(exc).__name__}: {exc}", "exception"


async def _docker_ps_statuses(container_prefix: str = "360scalp-v2") -> dict[str, str]:
    """Return {container_name: status_string} for containers matching prefix.

    Prefix widened from ``360scalp-v2-signing`` to the whole stack on
    2026-07-27.  Only the signing container was ever inspected, so on that
    day the engine sat ``Exited (137)`` and redis/signing/watchdog sat
    ``Created`` for two and a half hours and no detector so much as looked
    at them.
    """
    rc, stdout, stderr, cause = await _exec_capture((
        "docker", "ps", "-a",
        "--filter", f"name={container_prefix}",
        "--format", "{{.Names}}\t{{.Status}}",
    ))
    if cause or rc != 0:
        log.warning("docker ps failed: cause=%s rc=%s %s", cause or "-", rc, stderr.strip()[:200])
        # Empty is how CoreContainerDetector recognises "we are blind", which
        # it reports as docker_ps_unavailable rather than as every container
        # being missing. Preserved deliberately.
        return {}
    result: dict[str, str] = {}
    for line in stdout.splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) == 2:
            result[parts[0]] = parts[1]
    return result


async def _redis_idletime(
    container: str = "360scalp-v2-redis",
    key: str = "snapshot:tickers",
    attempts: int = REDIS_PROBE_ATTEMPTS,
) -> RedisProbe:
    """Read OBJECT IDLETIME for ``key``, reporting HOW it failed if it did.

    Before 2026-07-27 every failure returned ``""`` and the detector read an
    empty string as "nothing to report".  So the single condition this probe
    exists to catch — redis not answering — was the one condition guaranteed
    to read as all-clear.  Not-measured and measured-fine are different
    states and must not share a return value.

    Since 2026-08-16 they do not share a return *shape* either: the cause,
    the exit code and the vendor's own stderr ride out on ``RedisProbe`` and
    into the alert, instead of being logged into a container the owner cannot
    read from a phone.

    **The retry is not a softened alert.**  It happens inside one cycle, so a
    container that is genuinely down still pages on the cycle it went down —
    ``docker exec`` against a stopped container fails immediately and fails
    the same way twice.  What the retry removes is the single 10s deadline
    miss on a busy host, which pages HIGH and clears on the next cycle: two
    messages, no fault, on the channel that also has to carry a naked
    position.  An alert that cries wolf is not a safe default in either
    direction.
    """
    last: RedisProbe | None = None
    for attempt in range(1, max(1, attempts) + 1):
        rc, stdout, stderr, cause = await _exec_capture((
            "docker", "exec", container,
            "redis-cli", "OBJECT", "IDLETIME", key,
        ))
        out = stdout.strip()
        detail = stderr.strip()[:200]
        if cause:
            last = RedisProbe(ok=False, cause=cause, detail=detail, attempts=attempt)
        elif rc != 0:
            last = RedisProbe(
                ok=False, cause="exec_error", returncode=rc,
                detail=detail or "no stderr", attempts=attempt,
            )
        elif not out:
            last = RedisProbe(
                ok=False, cause="no_output", returncode=rc,
                detail="exited 0 and printed nothing", attempts=attempt,
            )
        else:
            return RedisProbe(ok=True, output=out, returncode=rc, attempts=attempt)
        log.warning(
            "redis idletime probe failed (attempt %s/%s): %s",
            attempt, attempts, last.summary(),
        )
    assert last is not None  # the loop runs at least once
    return last


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run() -> None:
    settings = load_settings()
    poll_interval = int(os.getenv("AGENT_POLL_INTERVAL_S", "60"))

    # Redis for alert state (optional — falls back to in-memory)
    redis_client = None
    redis_url = os.getenv("AGENT_REDIS_URL", "redis://360ce-ops-redis:6379/0")
    if redis_url:
        try:
            import redis.asyncio as aioredis
            redis_client = aioredis.from_url(redis_url, decode_responses=True)
            await redis_client.ping()
            log.info("Alert state: connected to Redis at %s", redis_url)
        except Exception as exc:
            log.warning("Redis unavailable (%s) — using in-memory alert state", exc)
            redis_client = None

    alert_state = AlertStateStore(
        redis_client,
        expiry_sec=int(os.getenv("AGENT_ALERT_EXPIRY_SEC", "3600")),
        dedup_sec=int(os.getenv("AGENT_DEDUP_SEC", "1800")),
    )

    notifier = Notifier(
        bot_token=os.getenv("AGENT_TELEGRAM_BOT_TOKEN", ""),
        chat_id=os.getenv("AGENT_TELEGRAM_CHAT_ID", ""),
        healthchecks_url=os.getenv("AGENT_HEALTHCHECKS_URL", ""),
        # FCM push sink — the in-region-reliable alert path. Reads the same
        # device registry the web app writes on /api/v1/devices; disabled-safe
        # when FIREBASE_SERVICE_ACCOUNT is unset.
        fcm=FcmSender(settings.fcm_service_account),
        device_registry=DeviceRegistry(settings.device_tokens_path),
    )

    api = EngineApiClient(settings)

    d1 = NakedPositionDetector(grace_sec=int(os.getenv("AGENT_NAKED_POSITION_GRACE_SEC", "90")))
    d2 = BackgroundTaskDetector()
    d3 = SigningHealthDetector()
    d4 = EngineStatusDetector()
    d3b = CoreContainerDetector()
    d6 = ApiHealthDetector()
    d7 = SignalSilenceDetector()
    d8 = RedisStalenessDetector(stale_sec=int(os.getenv("AGENT_REDIS_STALE_SEC", "45")))

    log.info("Monitoring agent started — poll interval %ss", poll_interval)

    while True:
        cycle_ok = True
        triggered_fingerprints: set[str] = set()

        # ---- Fetch all data sources -----------------------------------
        pulse: dict = {}
        health: dict = {}
        diag_positions: list[dict] = []
        tasks: list[str] = []
        container_statuses: dict[str, str] = {}
        redis_probe = RedisProbe(
            ok=False, cause="not_run", detail="the cycle did not reach the probe",
        )
        auto_mode: dict = {}

        try:
            pulse = await api.pulse() or {}
        except Exception as exc:
            log.warning("pulse fetch failed: %s", exc)
            pulse = {"error": str(exc)}
            cycle_ok = False

        try:
            health = await api.health() or {}
        except Exception as exc:
            health = {"error": str(exc)}
            cycle_ok = False

        try:
            diag_raw = await api.positions_diag() or {}
            diag_positions = diag_raw.get("items") or []
        except Exception as exc:
            log.warning("positions_diag fetch failed: %s", exc)
            cycle_ok = False

        try:
            tasks_raw = await api._get("/internal/diag/tasks") or {}
            tasks = tasks_raw.get("tasks") or []
        except Exception as exc:
            log.warning("diag/tasks fetch failed: %s", exc)
            cycle_ok = False

        try:
            auto_mode = await api.auto_mode() or {}
        except Exception as exc:
            auto_mode = {}

        try:
            container_statuses = await _docker_ps_statuses()
        except Exception as exc:
            log.warning("docker ps failed: %s", exc)
            cycle_ok = False

        try:
            redis_probe = await _redis_idletime()
        except Exception as exc:
            # _redis_idletime does not raise — it reports. This stays as the
            # last line of defence so an unexpected raise cannot silently
            # leave the previous cycle's probe in scope.
            log.warning("redis idletime check raised: %s", exc)
            redis_probe = RedisProbe(
                ok=False, cause="exception", detail=f"{type(exc).__name__}: {exc}",
            )
            cycle_ok = False

        # ---- Run detectors -------------------------------------------
        all_results: list[DetectorResult] = []

        for detector, args in [
            (d1, {"diag_items": diag_positions}),
            (d2, {"tasks": tasks}),
            (d3, {"container_statuses": container_statuses}),
            (d3b, {"container_statuses": container_statuses}),
            (d4, {"pulse": pulse}),
            (d6, {"health": health}),
            (d7, {"pulse": pulse, "mode": auto_mode.get("mode") or pulse.get("mode", "off")}),
            (d8, {"probe": redis_probe}),
        ]:
            try:
                results = detector.check(**args)
                all_results.extend(results)
            except Exception:
                log.exception("%s raised unexpectedly", detector.name)
                cycle_ok = False

        # ---- Process results -----------------------------------------
        for result in all_results:
            triggered_fingerprints.add(result.fingerprint)
            try:
                action = await alert_state.process(result)
                if action.should_notify:
                    await notifier.send_alert(action)
            except Exception:
                log.exception("alert_state.process failed for %s", result.fingerprint)

        # ---- Resolve cleared alerts ----------------------------------
        try:
            active = await alert_state.active_fingerprints()
            for fp in active - triggered_fingerprints:
                resolved = await alert_state.resolve(fp)
                if resolved is not None:
                    await notifier.send_recovery(resolved)
        except Exception:
            log.exception("resolution pass failed")

        # ---- Tier 2 heartbeat ----------------------------------------
        if cycle_ok:
            await notifier.ping_heartbeat()
        else:
            log.debug("Cycle had failures — skipping healthchecks.io ping")

        # ---- Tier 3: the agent's own liveness, where ops can see it ---
        # The dead-man's switch above is deliberately NOT pinged on a failed
        # cycle, so on that channel a degraded agent and a dead one look the
        # same. This one is published either way and carries `cycle_ok`, so
        # `/system/liveness` can tell them apart. Best-effort by construction:
        # a heartbeat that could break a detection cycle would be a monitoring
        # surface that reduces monitoring.
        await heartbeat.publish(
            redis_client,
            cycle_ok=cycle_ok,
            alerts_firing=len(triggered_fingerprints),
            detector_count=len(all_results),
            redis_probe_summary=redis_probe.summary(),
            redis_probe_ok=redis_probe.ok,
            poll_interval_s=poll_interval,
        )

        await asyncio.sleep(poll_interval)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

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
    PROBE_FAILED,
    ApiHealthDetector,
    BackgroundTaskDetector,
    CoreContainerDetector,
    DetectorResult,
    EngineStatusDetector,
    NakedPositionDetector,
    RedisStalenessDetector,
    SignalSilenceDetector,
    SigningHealthDetector,
)
from app.agent.alert_state import AlertStateStore
from app.agent.notifier import Notifier
from app.device_registry import DeviceRegistry
from app.fcm import FcmSender

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("agent.runner")

# ---------------------------------------------------------------------------
# Docker helpers (direct subprocess — no engine exec, just host daemon)
# ---------------------------------------------------------------------------

async def _docker_ps_statuses(container_prefix: str = "360scalp-v2") -> dict[str, str]:
    """Return {container_name: status_string} for containers matching prefix.

    Prefix widened from ``360scalp-v2-signing`` to the whole stack on
    2026-07-27.  Only the signing container was ever inspected, so on that
    day the engine sat ``Exited (137)`` and redis/signing/watchdog sat
    ``Created`` for two and a half hours and no detector so much as looked
    at them.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a",
            "--filter", f"name={container_prefix}",
            "--format", "{{.Names}}\t{{.Status}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        result: dict[str, str] = {}
        for line in stdout.decode().splitlines():
            parts = line.strip().split("\t", 1)
            if len(parts) == 2:
                result[parts[0]] = parts[1]
        return result
    except Exception as exc:
        log.warning("docker ps failed: %s", exc)
        return {}


async def _redis_idletime(container: str = "360scalp-v2-redis", key: str = "snapshot:tickers") -> str:
    """Return OBJECT IDLETIME for key as a raw string (e.g. '12').

    Returns the sentinel ``PROBE_FAILED`` when the probe itself could not
    run — a stopped redis container, a docker error, a timeout.

    Before 2026-07-27 every one of those returned ``""``, and the detector
    read an empty string as "nothing to report" and passed.  So the single
    condition this probe exists to catch — redis not answering — was the one
    condition guaranteed to read as all-clear.  Not-measured and
    measured-fine are different states and must not share a return value.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container,
            "redis-cli", "OBJECT", "IDLETIME", key,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        out = stdout.decode().strip()
        if proc.returncode != 0 or not out:
            log.warning(
                "redis idletime probe failed rc=%s stderr=%s",
                proc.returncode, stderr.decode().strip()[:200],
            )
            return PROBE_FAILED
        return out
    except Exception as exc:
        log.warning("redis idletime check failed: %s", exc)
        return PROBE_FAILED


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
        redis_idletime: str = PROBE_FAILED
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
            redis_idletime = await _redis_idletime()
        except Exception as exc:
            log.warning("redis idletime check failed: %s", exc)
            redis_idletime = PROBE_FAILED
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
            (d8, {"idletime_output": redis_idletime}),
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

        await asyncio.sleep(poll_interval)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

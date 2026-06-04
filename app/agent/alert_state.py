"""Redis-backed alert deduplication and escalation FSM.

Key schema:  alert:state:{fingerprint}
Value (JSON):
  first_seen   iso8601
  last_seen    iso8601
  count        int   — consecutive triggered cycles
  severity     WARN | HIGH
  paged        bool  — true once Telegram message has been sent
  last_paged   iso8601 | null

TTL: alert_expiry_sec (default 3600) — auto-expires when condition stays
resolved so Redis doesn't accumulate stale keys.

Falls back to an in-memory dict when Redis is unavailable.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.agent.detectors import DetectorResult

log = logging.getLogger("agent.alert_state")

_PREFIX = "alert:state:"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _dump(state: dict) -> str:
    return json.dumps(state)


class AlertStateStore:
    """Manages alert state with Redis backend and in-memory fallback."""

    def __init__(
        self,
        redis_client: Any = None,
        *,
        expiry_sec: int = 3600,
        dedup_sec: int = 1800,
    ) -> None:
        self._redis = redis_client
        self._expiry = expiry_sec
        self._dedup = dedup_sec
        self._mem: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Low-level get/set using Redis or memory fallback
    # ------------------------------------------------------------------

    async def _get(self, fingerprint: str) -> dict | None:
        key = _PREFIX + fingerprint
        if self._redis is not None:
            try:
                raw = await self._redis.get(key)
                return _load(raw)
            except Exception:
                log.warning("Redis get failed for %s, using memory fallback", key)
        return self._mem.get(fingerprint)

    async def _set(self, fingerprint: str, state: dict) -> None:
        key = _PREFIX + fingerprint
        if self._redis is not None:
            try:
                await self._redis.set(key, _dump(state), ex=self._expiry)
                return
            except Exception:
                log.warning("Redis set failed for %s, using memory fallback", key)
        self._mem[fingerprint] = state

    async def _delete(self, fingerprint: str) -> None:
        key = _PREFIX + fingerprint
        if self._redis is not None:
            try:
                await self._redis.delete(key)
                return
            except Exception:
                pass
        self._mem.pop(fingerprint, None)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def process(self, result: DetectorResult) -> "AlertAction":
        """Update state for a triggered detector result.

        Returns an AlertAction describing what (if anything) to notify.
        """
        state = await self._get(result.fingerprint)
        now = _now_iso()

        if state is None:
            state = {
                "first_seen": now,
                "last_seen": now,
                "count": 1,
                "severity": result.severity,
                "paged": False,
                "last_paged": None,
            }
        else:
            state["last_seen"] = now
            state["count"] = state.get("count", 0) + 1
            # Severity can only escalate
            if result.severity == "HIGH" and state["severity"] == "WARN":
                state["severity"] = "HIGH"
                state["paged"] = False  # re-page on escalation

        await self._set(result.fingerprint, state)

        # Decide whether to page
        should_page = self._should_page(state, result)
        if should_page:
            state["paged"] = True
            state["last_paged"] = now
            await self._set(result.fingerprint, state)

        return AlertAction(
            should_notify=should_page,
            is_first=state["count"] == 1,
            count=state["count"],
            first_seen=state["first_seen"],
            severity=state["severity"],
            result=result,
        )

    def _should_page(self, state: dict, result: DetectorResult) -> bool:
        if state["paged"] and self._within_dedup(state):
            return False
        # HIGH always pages immediately
        if state["severity"] == "HIGH":
            return True
        # WARN pages after 2+ consecutive cycles
        return state["count"] >= 2

    def _within_dedup(self, state: dict) -> bool:
        last_paged = state.get("last_paged")
        if not last_paged:
            return False
        try:
            paged_at = datetime.fromisoformat(last_paged)
            age = (datetime.now(timezone.utc) - paged_at).total_seconds()
            return age < self._dedup
        except Exception:
            return False

    async def resolve(self, fingerprint: str) -> "ResolvedAction | None":
        """Mark a fingerprint resolved. Returns action if it was previously paged."""
        state = await self._get(fingerprint)
        if state is None:
            return None
        was_paged = state.get("paged", False)
        first_seen = state.get("first_seen", "")
        await self._delete(fingerprint)
        if was_paged:
            return ResolvedAction(fingerprint=fingerprint, first_seen=first_seen)
        return None

    async def active_fingerprints(self) -> set[str]:
        """Return all fingerprints currently tracked (for resolution detection)."""
        if self._redis is not None:
            try:
                keys = await self._redis.keys(_PREFIX + "*")
                return {k.removeprefix(_PREFIX) for k in keys}
            except Exception:
                pass
        return set(self._mem.keys())


class AlertAction:
    def __init__(
        self,
        *,
        should_notify: bool,
        is_first: bool,
        count: int,
        first_seen: str,
        severity: str,
        result: DetectorResult,
    ) -> None:
        self.should_notify = should_notify
        self.is_first = is_first
        self.count = count
        self.first_seen = first_seen
        self.severity = severity
        self.result = result


class ResolvedAction:
    def __init__(self, *, fingerprint: str, first_seen: str) -> None:
        self.fingerprint = fingerprint
        self.first_seen = first_seen

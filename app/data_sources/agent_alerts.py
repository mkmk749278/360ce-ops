"""Reader for the monitoring agent's active-alert state.

The agent (``app/agent/runner.py``, a separate container) runs the Tier-0
safety detectors — naked position, signing-service down, engine/Redis
stale, … — and tracks each firing condition as a Redis key
``alert:state:{fingerprint}`` (see ``app/agent/alert_state.py``).

Telegram was the agent's only page channel; with Telegram gone (2026-06-20)
the ops dashboard's alerts panel is the surface.  This module reads the same
Redis the agent writes, so the panel reflects live agent state without
re-running detectors or duplicating their logic.

Read-only.  Falls back to an ``error`` payload (never raises) so the panel
renders a degraded state instead of 500-ing when Redis is unreachable.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.config import Settings

log = logging.getLogger("ops.agent_alerts")

_PREFIX = "alert:state:"
# WARN alerts page only after 2+ consecutive cycles (agent dedup FSM); mirror
# that here so the panel doesn't surface a single transient WARN blip.
_WARN_MIN_COUNT = 2


class AgentAlertsReader:
    """Lazy async Redis reader for the agent's ``alert:state:*`` keys."""

    def __init__(self, settings: Settings) -> None:
        self._url = settings.agent_redis_url
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self._url, decode_responses=True)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def active_alerts(self) -> dict[str, Any]:
        """Return ``{"alerts": [...], "error": None}``.

        Each alert: ``severity, fingerprint, kind, description, count,
        first_seen, last_seen, paged``.  HIGH first, then most-recent first.
        A WARN that hasn't reached the page threshold is included but flagged
        ``paged=False`` so the panel can de-emphasise it.
        """
        try:
            client = self._get_client()
            keys = await client.keys(_PREFIX + "*")
        except Exception as exc:
            log.warning("agent alert read failed: %s", exc)
            return {"alerts": [], "error": str(exc)}

        alerts: list[dict[str, Any]] = []
        for key in keys:
            try:
                raw = await client.get(key)
            except Exception:
                continue
            state = _load(raw)
            if state is None:
                continue
            fingerprint = key.removeprefix(_PREFIX)
            severity = state.get("severity", "WARN")
            count = int(state.get("count", 0) or 0)
            # Suppress sub-threshold WARN blips (single-cycle), matching the
            # agent's own page gate; HIGH always shows.
            if severity != "HIGH" and count < _WARN_MIN_COUNT:
                continue
            alerts.append({
                "severity": severity,
                "fingerprint": fingerprint,
                "kind": fingerprint.split(":", 1)[0],
                "description": state.get("description") or fingerprint,
                "count": count,
                "first_seen": state.get("first_seen", ""),
                "last_seen": state.get("last_seen", ""),
                "paged": bool(state.get("paged", False)),
            })

        # Stable two-pass: most-recent first, then HIGH ahead of WARN.
        # ISO-8601 sorts lexically, so reverse=True gives newest-first.
        alerts.sort(key=lambda a: a["last_seen"] or "", reverse=True)
        alerts.sort(key=lambda a: 0 if a["severity"] == "HIGH" else 1)
        return {"alerts": alerts, "error": None}


def _load(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None

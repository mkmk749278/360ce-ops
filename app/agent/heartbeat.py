"""The agent's own liveness, published where a reader can see it.

A monitoring agent is the one process whose failure is silent by construction:
a dead pager sends no message, so "no alerts" and "no agent" look identical on
every surface that reads alerts. Before this, the only evidence the agent was
running was the healthchecks.io ping — an external service the owner has to go
and look at, and one that is deliberately *not* pinged on a cycle that had
failures, so a degraded agent and a dead one shared a symptom there too.

So the agent stamps what it just did, every cycle, on the same Redis it already
uses for alert state. ``/system/liveness`` renders it as the last link in the
chain.

Two things it deliberately records beyond a timestamp:

* ``cycle_ok`` — a cycle that ran and reported failures is not a cycle that did
  not run. They are different states with different next moves, and the
  heartbeat has to be able to say which, or the page inherits the same
  conflation the dead-man's switch has.
* ``probe`` — how the redis probe went, in the probe's own words. The alert
  carries this only while it is firing; the heartbeat carries it always, which
  is what makes a *flapping* probe visible as a pattern rather than as a series
  of unrelated pages.
* ``sinks`` — which delivery paths are actually armed (2026-08-19). ``/alerts``
  used to grade only FCM and print *"Nothing pages you — this page is the only
  way you learn about a naked position"* over a box whose Telegram sink was
  working and delivering. That page has now asserted a delivery path it could
  not observe three times, in both directions. The fix is not a third
  assertion: the **agent** is the only process that knows what it can send
  through, so it publishes that here and the page renders it. One writer, one
  reader. Only whether a sink is configured is published — never a token.

Best-effort throughout: a heartbeat that could break a detection cycle would be
a monitoring surface that reduces monitoring. Every failure here is logged and
swallowed.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("agent.heartbeat")

KEY = "agent:cycle"

#: Long enough that a reader can tell a stopped agent from a missing key —
#: the value outlives many poll intervals, so an *absent* key means the agent
#: has been gone for the best part of an hour, not that it missed a beat.
TTL_SEC = 3600


async def publish(
    client: Any,
    *,
    cycle_ok: bool,
    alerts_firing: int,
    detector_count: int,
    redis_probe_summary: str,
    redis_probe_ok: bool,
    poll_interval_s: int,
    sinks: dict[str, bool] | None = None,
) -> None:
    """Stamp this cycle. Never raises.

    ``sinks`` maps a delivery path to whether it is armed — booleans only, and
    a token never travels through here.
    """
    if client is None:
        # In-memory alert state — the agent still works, but nothing outside
        # this process can see it. Logged once per cycle at debug rather than
        # silently, so "the page says no heartbeat" has a findable cause.
        log.debug("no redis client — heartbeat not published")
        return
    payload = {
        "at": datetime.now(timezone.utc).isoformat(),
        "cycle_ok": cycle_ok,
        "alerts_firing": alerts_firing,
        "detectors": detector_count,
        "redis_probe": redis_probe_summary,
        "redis_probe_ok": redis_probe_ok,
        "poll_interval_s": poll_interval_s,
        # `{}` and "no sink armed" are different claims and must stay
        # distinguishable: an older agent that predates this field publishes
        # nothing, and a reader has to render NOT REPORTED rather than "nothing
        # pages you" — which is the exact sentence this field exists to stop
        # being guessed at.
        "sinks": dict(sinks) if sinks else {},
    }
    try:
        await client.set(KEY, json.dumps(payload), ex=TTL_SEC)
    except Exception as exc:  # noqa: BLE001 — reported, never fatal
        log.warning("heartbeat publish failed: %s", exc)

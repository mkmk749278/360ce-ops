"""Container healthcheck for the monitoring-agent — the one it never had.

Both ops services run the SAME image, and that image carries one
``HEALTHCHECK``: fetch ``http://localhost:8000/healthz``. That is right for the
``ops`` web container and structurally impossible for this one, which overrides
the command to ``python -m app.agent.runner`` and serves no HTTP at all. So the
monitoring agent has been reporting **unhealthy** by construction — on the box
2026-08-18, ``failing streak 14`` eight minutes after a deploy, with a urllib
connection traceback as its last output.

Nothing restarted because of it (this container is not autoheal-labelled), and
that is the whole problem: a health signal that can never be true is not a
false alarm, it is a **dead instrument**. Red on it means nothing, so red on it
gets ignored — on the one container whose failure is otherwise silent, because
a dead pager sends no message.

It was invisible until `/system` rendered the healthcheck's own output. The
status had been sitting in `docker ps` all along.

What this checks instead: the heartbeat the agent already stamps after every
cycle (``app/agent/heartbeat.py``). That is the agent's real liveness — it
means detectors ran — where an HTTP probe on a process with no HTTP server
means nothing whatever.

Deliberately NOT checked here: whether the last cycle was clean. A cycle that
ran and reported failures is the agent working, and failing the container on it
would restart the one process able to tell you what failed. ``cycle_ok`` is
published and rendered on `/system/liveness`; it is not a restart condition.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from app.agent.heartbeat import KEY

#: Three poll intervals at the 60s default. Long enough that one slow cycle on
#: a busy host is not a failure, short enough that a stopped agent is caught
#: within a few minutes. Docker's own ``retries`` multiplies this again.
STALE_SEC = int(os.getenv("AGENT_HEARTBEAT_STALE_SEC", "180"))


def _fail(reason: str) -> int:
    # stdout, because Docker captures it into `State.Health.Log` — which is
    # where `/system` reads it from. A reason in a log nobody can reach from a
    # phone is the defect this whole surface exists to repair.
    print(f"agent heartbeat: {reason}")
    return 1


def check() -> int:
    url = os.getenv("AGENT_REDIS_URL", "redis://360ce-ops-redis:6379/0")
    try:
        import redis
    except ImportError:  # pragma: no cover - the image always has it
        return _fail("redis client not installed")

    try:
        client = redis.from_url(url, decode_responses=True, socket_timeout=3)
        raw = client.get(KEY)
    except Exception as exc:
        # The store being unreachable is NOT the agent being dead — it keeps
        # detecting with in-memory alert state. Failing here would restart a
        # working agent because its optional dependency blinked, so this is a
        # refusal to grade rather than a failure.
        print(f"agent heartbeat: cannot read {url} ({type(exc).__name__}) — not graded")
        return 0
    if not raw:
        return _fail("no heartbeat stamped yet")

    try:
        stamped = json.loads(raw).get("at")
        when = datetime.fromisoformat(str(stamped))
    except (TypeError, ValueError, json.JSONDecodeError):
        return _fail(f"unreadable heartbeat: {str(raw)[:80]!r}")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    age = (datetime.now(timezone.utc) - when).total_seconds()
    if age > STALE_SEC:
        return _fail(f"last cycle {age:.0f}s ago, over the {STALE_SEC}s bound")
    print(f"agent heartbeat: last cycle {age:.0f}s ago")
    return 0


if __name__ == "__main__":
    sys.exit(check())

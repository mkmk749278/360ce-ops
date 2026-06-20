"""Append-only audit log for control-plane actions.

Every engine write the dashboard performs (auto-mode flip, kill-switch
engage/disengage, …) is recorded here as one JSON object per line.  This
is the accountability backstop for promoting ops from a read-only
dashboard to the engine control plane (2026-06-20): when Telegram is gone
and ops can halt or arm live trading, there must be a durable record of
who flipped what, when, and whether the engine accepted it.

Design notes:
* **Best-effort, never blocking.** A failed audit write must NOT prevent a
  control action — being able to hit the kill switch matters more than
  logging it.  Write failures are logged via the stdlib logger and
  swallowed; the action still proceeds.
* **JSONL, newest-last.** Append is O(1); the reader tails the last N
  lines for the control page's "recent actions" panel.
* Lives on a writable volume (``OPS_AUDIT_LOG``), distinct from the
  read-only ``/engine-data`` mount.
"""
from __future__ import annotations

import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("ops.audit")


def record(
    path: str,
    *,
    action: str,
    params: dict[str, Any],
    result: dict[str, Any],
    ok: bool,
    actor: str = "owner",
) -> None:
    """Append one audit entry.  Best-effort — swallows write errors."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "action": action,
        "params": params,
        "ok": ok,
        # Keep the result compact — store the engine's error/detail when
        # the action failed, and a short success marker otherwise.
        "result": result.get("error") if not ok else "ok",
    }
    line = json.dumps(entry, separators=(",", ":"), default=str)
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        # Never block a control action on an audit-write failure; the
        # stdlib log is the fallback record.
        logger.error("audit write failed (%s) — entry=%s", exc, line)


def tail(path: str, limit: int = 25) -> list[dict[str, Any]]:
    """Return the last ``limit`` audit entries, newest first.  Returns an
    empty list when the log doesn't exist yet or can't be read."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            last = deque(fh, maxlen=limit)
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for raw in last:
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    out.reverse()
    return out

"""CoinDCX venue — grading the engine's status file and self-test report.

Engine side: ``src/venues/coindcx/`` in 360-v2 (PR #1075). CoinDCX execution
ships DARK: ``COINDCX_EXECUTION_ENABLED`` is off and the owner runs a
real-account self-test on his own account, at a tiny size, before any user.
This module is how that is read.

Ops computes nothing about the venue. Every number is the engine's; this
module only decides which STATE the page is in, and each state names its own
next move:

* ``missing``  — no status file. The reconciler is not running in the engine
  (the server-side execution stack is off, or an engine predating the venue).
  A deploy question, never "all quiet".
* ``unreadable`` — the file exists and could not be parsed. Ours or the
  writer's; the loader's own words are shown.
* ``stale``    — the file stopped being rewritten. Graded on the engine's
  published ``reconcile_interval_sec``; a status older than
  ``STALE_CYCLES`` cycles means the loop stopped, and positions it guards are
  unwatched. ``FALLBACK_INTERVAL_SEC`` is used only for a file predating that
  stamp, and the page says so.
* ``running``  — current.

Counter dicts (``executor``, ``dispatch``, reconciler ``stats``) are rendered by
iterating THE ENGINE'S keys. A key list kept here would be silent on the next
counter the engine adds — ``MEASUREMENT_SUFFIXES`` wearing another hat.
"""
from __future__ import annotations

from typing import Any

#: A status file older than this many reconcile cycles is stale.
STALE_CYCLES = 4

#: Only for a status file written before the engine published its cadence.
FALLBACK_INTERVAL_SEC = 30.0

#: A requested self-test with no newer report after this long is overdue.
#: A run is ~1 minute (a 10s fill wait plus a handful of calls).
SELF_TEST_OVERDUE_SEC = 600.0

#: Verdicts the engine's ``self_test.run`` finishes with. COPY, not a mirror:
#: an unknown verdict renders under its raw name badged ``unclassified``.
VERDICT_COPY: dict[str, tuple[str, str]] = {
    "pass": ("ok", "Every gate step passed on the real account, and it ended flat."),
    "fail": ("err", "A gate step failed. The account was left flat — read the failing step."),
    "fail_position_left_open": (
        "err",
        "A POSITION MAY STILL BE OPEN on the CoinDCX account. Check it on CoinDCX now "
        "and close it by hand if it is.",
    ),
    "refused": ("warn", "Refused before any call: the uid is not on the engine's allow-list."),
    "error": ("err", "The run crashed. The engine's own error is in the last step."),
}

#: Steps that are recorded but do not decide the verdict (engine
#: ``self_test``: a finding about CoinDCX's behaviour, not a gate).
INFORMATIONAL_STEPS = {"exchange_auto_cancels_on_exit"}


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def grade_status(status: Any, *, now: float) -> dict[str, Any]:
    """``{state, age_sec, interval_sec, fallback_interval, detail}``."""
    if not isinstance(status, dict):
        return {"state": "unreadable", "detail": f"unexpected shape: {type(status).__name__}"}
    err = status.get("error")
    if "written_at" not in status and err is not None:
        text = str(err)
        state = "missing" if text.startswith("missing") else "unreadable"
        return {"state": state, "detail": text}
    written = _num(status.get("written_at"))
    if written is None:
        return {"state": "unreadable", "detail": "no written_at stamp"}
    interval = _num(status.get("reconcile_interval_sec"))
    fallback = interval is None or interval <= 0
    interval = FALLBACK_INTERVAL_SEC if fallback else interval
    age = max(0.0, now - written)
    state = "stale" if age > STALE_CYCLES * interval else "running"
    return {
        "state": state,
        "age_sec": age,
        "interval_sec": interval,
        "stale_after_sec": STALE_CYCLES * interval,
        "fallback_interval": fallback,
    }


def execution_view(status: dict[str, Any]) -> dict[str, Any]:
    """Whether the engine will place CoinDCX orders, and for whom."""
    allow = status.get("allow_list") if isinstance(status.get("allow_list"), dict) else {}
    enabled = status.get("execution_enabled")
    active = allow.get("active")
    size = allow.get("size")
    if enabled is not True:
        who = "nobody — execution is off"
    elif active is True:
        who = f"only the {size} allow-listed uid(s)"
    elif active is False:
        who = "EVERY connected CoinDCX user"
    else:
        who = "not reported"
    return {"enabled": enabled, "allow_active": active, "allow_size": size, "who": who}


def stream_view(status: dict[str, Any]) -> dict[str, Any]:
    """Tri-state: off by switch · not reported · the manager's own snapshot."""
    if status.get("stream_enabled") is False:
        return {"state": "off"}
    snap = status.get("stream")
    if not isinstance(snap, dict):
        # Enabled but no snapshot: the manager is not attached in the
        # process that wrote the file. Not "no streams" — we cannot tell.
        return {"state": "not_reported"}
    return {"state": "reported", **snap}


def safety_view(status: dict[str, Any]) -> dict[str, Any]:
    """The two things that can hurt a user's real account, plus the breaker.

    ``faults`` (engine 2026-10-01): live positions the exchange shows WITHOUT
    a stop, and records the exchange returns no row for (skipped every cycle,
    age cap included).  ``breaker``: CoinDCX's own venue breaker, whose trip
    writes the master switch OFF instead of halting Binance users.

    Each block is tri-state.  ``None`` means an engine predating it, never
    zero: an absent count is not a clean account.
    """
    rec = status.get("reconciler") if isinstance(status.get("reconciler"), dict) else {}
    faults = rec.get("faults") if isinstance(rec.get("faults"), dict) else None
    breaker = status.get("breaker") if isinstance(status.get("breaker"), dict) else None
    paused_by_breaker = bool(
        breaker and breaker.get("trips") and status.get("execution_enabled") is False
    )
    return {"faults": faults, "breaker": breaker, "paused_by_breaker": paused_by_breaker}


def counter_rows(block: Any) -> list[tuple[str, Any]]:
    """The engine's counters in the engine's keys, sorted for reading."""
    if not isinstance(block, dict):
        return []
    return sorted(block.items())


def grade_self_test(report: Any, *, last_request_at: float | None, now: float) -> dict[str, Any]:
    """The last report, and whether a newer request is still owed one."""
    have = isinstance(report, dict) and "verdict" in report
    started = _num(report.get("started_at")) if have else None
    pending = None
    if last_request_at is not None and (started is None or last_request_at > started + 1):
        waited = max(0.0, now - last_request_at)
        pending = {
            "requested_at": last_request_at,
            "waited_sec": waited,
            "overdue": waited > SELF_TEST_OVERDUE_SEC,
        }
    if not have:
        missing = isinstance(report, dict) and str(report.get("error", "")).startswith("missing")
        return {
            "state": "never_run" if missing else "unreadable",
            "detail": report.get("error") if isinstance(report, dict) else None,
            "pending": pending,
        }
    verdict = str(report.get("verdict"))
    tone, sentence = VERDICT_COPY.get(verdict, ("warn", None))
    steps = []
    for s in report.get("steps") or []:
        if not isinstance(s, dict):
            continue
        name = str(s.get("step", "?"))
        observed = {k: v for k, v in s.items() if k not in ("step", "ok")}
        steps.append({
            "step": name,
            "ok": s.get("ok") is True,
            "gate": name not in INFORMATIONAL_STEPS,
            "observed": observed,
        })
    return {
        "state": "reported",
        "verdict": verdict,
        "tone": tone,
        "sentence": sentence,
        "unclassified": verdict not in VERDICT_COPY,
        "symbol": report.get("symbol"),
        "margin_currency": report.get("margin_currency"),
        "uid": report.get("uid"),
        "started_at": started,
        "finished_at": _num(report.get("finished_at")),
        "steps": steps,
        "pending": pending,
    }


def grade_access(payload: Any) -> dict[str, Any]:
    """Who may trade on CoinDCX — five states, never pooled.

    * ``ok`` — the engine answered with its switches and list;
    * ``unreadable`` — it answered, and its settings store could not be read,
      so NO switch position is shown (a switch drawn either way would be a
      verdict nobody observed);
    * ``not_reported`` — an engine predating the endpoint (404);
    * ``unreachable`` — ops' own call failed;
    * ``unknown`` — any other shape, shown raw rather than guessed at.

    Graded by key presence, never by whether a shared key is truthy
    (``str(httpx.ReadTimeout())`` is ``""``).
    """
    if not isinstance(payload, dict):
        return {"state": "unknown", "raw": payload}
    if "readable" in payload:
        if payload.get("readable") is True:
            return {
                "state": "ok",
                "execution_enabled": payload.get("execution_enabled"),
                "open_to_all": payload.get("open_to_all"),
                "allowed": [r for r in payload.get("allowed") or [] if isinstance(r, dict)],
            }
        return {"state": "unreadable",
                "store_initialised": payload.get("store_initialised")}
    if "error" in payload:
        if payload.get("status_code") == 404:
            return {"state": "not_reported"}
        return {"state": "unreachable", "detail": payload.get("error") or "no reason given"}
    return {"state": "unknown", "raw": payload}

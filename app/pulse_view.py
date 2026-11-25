"""Pulse view model — what the landing page shows, decided in one place.

The page used to render ``/api/pulse`` and ``/api/auto-mode`` as two raw JSON
blocks (outside audit, 2026-11-25: "the landing page is a JSON dump"). The
engine already types both payloads (360-v2 ``src/api/schemas.py``:
``PulseSnapshot``, ``AutoModeStatus``), so they become tiles here.

Rules this module carries, each one already in this repo's CLAUDE.md:

- **Unknown is not a value.** A field the engine did not send renders ``None``
  (the template's em-dash), never ``0``. A transport failure (ops' own wrapper:
  ``error`` with no ``ok``) makes every tile unreadable and names the cause.
- **The raw payload stays reachable** behind a toggle, because a field this
  module has never heard of must still be readable by the owner.
- **Liveness leads with what needs a look.** A 60-row alphabetical table put
  the four sustained failures wherever their names happened to sort.
"""
from __future__ import annotations

from typing import Any


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _unreadable(payload: Any) -> str | None:
    """The cause, if this payload is not a reading; ``None`` if it is."""
    if not isinstance(payload, dict):
        return "the engine returned no object"
    # Tell producers apart by a key only one has (2026-09-03): ops' transport
    # wrapper carries `error` and never `ok`.
    if "error" in payload and "ok" not in payload:
        return str(payload.get("error") or "the request failed with no message")
    # /api/auto-mode answers a synthetic mode="off" with zeros when it cannot
    # build the status, and since engine 2026-11-25 says so with
    # readable=False. Absent means an older engine: read as before.
    if payload.get("readable") is False:
        return "the engine could not build this status and sent a placeholder"
    return None


def pulse_view(pulse: Any, auto: Any) -> dict[str, Any]:
    p_err = _unreadable(pulse)
    a_err = _unreadable(auto)
    p = pulse if p_err is None else {}
    a = auto if a_err is None else {}

    budget = _num(p.get("daily_loss_budget_usd"))
    used = _num(p.get("daily_loss_used_usd"))
    used_frac = None
    if budget is not None and used is not None and budget > 0:
        used_frac = max(0.0, min(1.0, used / budget))

    status = p.get("status")
    status_tone = {"Healthy": "ok", "Degraded": "warn", "Down": "bad"}.get(status, "muted")

    mode = a.get("mode") if a else None
    if mode is None:
        mode = p.get("mode")
    flags = []
    if a.get("daily_kill_tripped") is True:
        flags.append("daily loss limit tripped")
    if a.get("manual_paused") is True:
        flags.append("manually paused")

    pnl = _num(p.get("today_pnl_usd"))
    return {
        "pulse_error": p_err,
        "auto_error": a_err,
        "status": status,
        "status_tone": status_tone,
        "uptime": _num(p.get("uptime_seconds")),
        "mode": mode,
        "mode_flags": flags,
        "regime": p.get("regime"),
        "regime_pct_trending": _num(p.get("regime_pct_trending")),
        "pnl_usd": pnl,
        "pnl_pct": _num(p.get("today_pnl_pct")),
        "pnl_tone": None if pnl is None else ("pos" if pnl > 0 else "neg" if pnl < 0 else ""),
        "equity": _num(a.get("current_equity_usd")),
        "budget": budget,
        "budget_used": used,
        "budget_frac": used_frac,
        "budget_tone": (
            None if used_frac is None
            else "bad" if used_frac >= 0.8 else "warn" if used_frac >= 0.5 else "ok"
        ),
        "signals_today": p.get("signals_today"),
        "open_positions": p.get("open_positions"),
        "scanning_pairs": p.get("scanning_pairs"),
    }


_STATUS_ORDER = {"violating": 0, "unknown": 1}


def liveness_view(liveness: Any) -> dict[str, Any]:
    """Split the manifest into what needs a look and the rest.

    The engine's statuses are ``ok`` / ``violating`` / ``unknown``
    (360-v2 ``src/feature_liveness.py``). ``unknown`` means the probe could not
    read its counter, so it is listed beside ``violating`` — a probe that
    cannot see is not a healthy one — but sorted after it.
    """
    if not isinstance(liveness, dict) or not isinstance(liveness.get("features"), dict):
        return {"state": "unreadable", "error": (liveness or {}).get("error") if isinstance(liveness, dict) else None}
    rows = [
        {"name": name, "status": str((f or {}).get("status") or "unknown"),
         "detail": (f or {}).get("detail"), "streak": (f or {}).get("streak")}
        for name, f in liveness["features"].items()
    ]
    needs = sorted(
        (r for r in rows if r["status"] != "ok"),
        key=lambda r: (_STATUS_ORDER.get(r["status"], 2), -(_num(r["streak"]) or 0), r["name"]),
    )
    return {
        "state": "ok",
        "alerts": liveness.get("alerts") or [],
        "needs_look": needs,
        "all": sorted(rows, key=lambda r: r["name"]),
        "n_ok": sum(1 for r in rows if r["status"] == "ok"),
        "boot_grace": bool(liveness.get("boot_grace_active")),
        "generated_at": liveness.get("generated_at_iso") or liveness.get("generated_at"),
        "fail_open": liveness.get("fail_open") or {},
    }

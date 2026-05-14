"""Signal table — merges live ``/api/signals`` with monitor ``signals_last100``."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, Request

router = APIRouter()


def _parse_iso_timestamp(value: Any) -> datetime | None:
    """Best-effort ISO-8601 / numeric parse.

    The live engine API returns Pydantic-serialized ``datetime`` (ISO 8601 with
    ``+00:00``).  The monitor JSON dumps may write the same, but historical
    artefacts can be numeric epoch.  Return ``None`` on anything we can't read
    so the renderer falls back to the em-dash rather than crashing.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError):
            return None
    if isinstance(value, str):
        try:
            # ``Z`` suffix → ``+00:00`` for ``fromisoformat`` compatibility on
            # Python < 3.11.  Newer versions accept ``Z`` natively but this
            # form is also valid.
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _format_relative(ts: datetime | None, now: datetime | None = None) -> str | None:
    """Format a ``datetime`` as a compact "Xm ago" / "Xh ago" / "Xd ago" string.

    Returns ``None`` (template renders em-dash) when the input is unparseable.
    Matches the relative-age format used on the Lumin app's Signals list so
    the dashboard and the app present timestamps consistently for the operator.
    """
    if ts is None:
        return None
    now = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = now - ts
    secs = int(delta.total_seconds())
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _normalize_signal(entry: dict) -> dict:
    """Common shape across the live API and the monitor JSON dumps.

    The engine surfaces creation time as ``timestamp`` (an ISO datetime on
    ``SignalDetail``), and also stamps ``dispatch_timestamp`` for the moment
    of Telegram dispatch.  Historical monitor dumps use the same ``timestamp``
    key.  Older payloads sometimes carried camelCase / ``created_at`` variants;
    keep those in the fallback chain so this normaliser stays a one-stop
    shape adapter as recommended by CLAUDE.md.

    ``created_at_raw`` retains the original value for hover / tooltip use;
    ``created_at_relative`` is the operator-facing short form.
    """
    raw_ts = (
        entry.get("timestamp")
        or entry.get("dispatch_timestamp")
        or entry.get("created_at")
        or entry.get("dispatched_at")
        or entry.get("createdAt")
    )
    parsed = _parse_iso_timestamp(raw_ts)
    relative = _format_relative(parsed)
    absolute = (
        parsed.strftime("%Y-%m-%d %H:%M UTC")
        if parsed is not None
        else (str(raw_ts) if raw_ts else None)
    )
    return {
        "id": entry.get("id") or entry.get("signal_id") or "",
        "symbol": entry.get("symbol", ""),
        "side": entry.get("side") or entry.get("direction", ""),
        "setup_class": entry.get("setup_class") or entry.get("setupClass", ""),
        "confidence": entry.get("confidence"),
        "status": entry.get("status") or entry.get("terminal_status") or entry.get("outcome_label") or "",
        "regime": entry.get("regime", ""),
        "pnl_pct": entry.get("pnl_pct") if entry.get("pnl_pct") is not None else entry.get("pnlPct"),
        "entry": entry.get("entry") or entry.get("entry_price"),
        "sl": entry.get("sl") or entry.get("stop_loss"),
        "tp1": entry.get("tp1"),
        # Kept for backwards-compat — the template still references it.
        "created_at": absolute,
        "created_at_raw": raw_ts,
        "created_at_relative": relative,
        "created_at_absolute": absolute,
        "channel": entry.get("channel", ""),
    }


def _extract_signals(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("signals"), list):
            return [e for e in payload["signals"] if isinstance(e, dict)]
        if isinstance(payload.get("items"), list):
            return [e for e in payload["items"] if isinstance(e, dict)]
    return []


@router.get("/signals")
async def signals(
    request: Request,
    status: str | None = Query(None),
    setup_class: str | None = Query(None),
):
    api = request.app.state.engine_api
    logs = request.app.state.monitor_logs

    live = await api.signals(status=status, setup_class=setup_class)
    historic = await logs.signals_last100()

    live_list = _extract_signals(live)
    hist_list = _extract_signals(historic)

    seen_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    for entry in [*live_list, *hist_list]:
        norm = _normalize_signal(entry)
        if not norm["id"] or norm["id"] in seen_ids:
            continue
        if status and (norm["status"] or "").lower() != status.lower():
            continue
        if setup_class and norm["setup_class"] != setup_class:
            continue
        seen_ids.add(norm["id"])
        rows.append(norm)

    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)

    setup_classes = sorted({r["setup_class"] for r in rows if r["setup_class"]})
    statuses = sorted({r["status"] for r in rows if r["status"]})

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "signals.html",
        {
            "request": request,
            "rows": rows,
            "setup_classes": setup_classes,
            "statuses": statuses,
            "filter_status": status,
            "filter_setup": setup_class,
            "active": "signals",
        },
    )

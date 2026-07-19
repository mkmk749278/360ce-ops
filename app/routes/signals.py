"""Signal table — merges live ``/api/signals`` with monitor ``signals_last100``."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, Request

from app.reports import csv_response

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


# Status names that mean "position still working" across engine builds —
# used only when a payload predates the engine ``is_open`` stamp.
_OPEN_STATUSES = {
    "ACTIVE",
    "TP1_HIT",
    "TP2_HIT",
    "TP3_HIT",
    "PRE_TP",
    "BE_ARMED",
    "RUNNER",
}


def _status_class(status: str, is_open: bool) -> str:
    """CSS badge class for a signal status — colour over reading."""
    s = (status or "").upper()
    if s.startswith("SL") or "STOP" in s:
        return "st-sl"
    if "EXPIRED" in s:
        return "st-expired"
    if s.startswith("TP") or "TP" in s:
        return "st-tp"
    if is_open or s == "ACTIVE":
        return "st-active"
    return "st-closed"


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
    status = (
        entry.get("status")
        or entry.get("terminal_status")
        or entry.get("outcome_label")
        or ""
    )
    # Open/closed truth: prefer the engine's own ``is_open`` stamp (Session
    # 46 made it the display truth in the app); fall back to a status-name
    # heuristic for older monitor dumps that predate the stamp.
    if "is_open" in entry:
        is_open = bool(entry.get("is_open"))
    else:
        is_open = status.upper() in _OPEN_STATUSES

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
        "status": status,
        "is_open": is_open,
        "status_class": _status_class(status, is_open),
        "regime": entry.get("entry_regime") or entry.get("regime", ""),
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


async def _build_rows(
    request: Request,
    status: str | None,
    setup_class: str | None,
) -> list[dict[str, Any]]:
    """Merge live ``/api/signals`` with the monitor ``signals_last100`` dump,
    de-dupe by id, apply the status / setup filters, and return newest-first.

    Shared by the page route and the CSV export so the download is always a
    faithful copy of what the table shows."""
    api = request.app.state.engine_api
    logs = request.app.state.monitor_logs

    live = await api.signals(status=status, setup_class=setup_class)
    live_list = _extract_signals(live)

    # The monitor ``signals_last100`` dump is a TTL-cached artefact on the
    # monitor-logs branch that ``/reset_full`` (engine-side) cannot clear, so
    # merging it unconditionally left phantom pre-reset rows on the page and
    # made a genuine clear look like it had failed.  Use it only as a FALLBACK
    # when the live engine API returns nothing (API cold / engine restarting),
    # so the tab otherwise reflects true live engine state and a reset shows.
    if live_list:
        hist_list: list[dict[str, Any]] = []
    else:
        hist_list = _extract_signals(await logs.signals_last100())

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

    # Open positions first (they're what the operator acts on), newest first
    # inside each group — same ordering truth-fix the app got in Session 46.
    rows.sort(
        key=lambda r: (0 if r.get("is_open") else 1, -(_sort_ts(r))),
    )
    return rows


def _sort_ts(row: dict[str, Any]) -> float:
    parsed = _parse_iso_timestamp(row.get("created_at_raw"))
    return parsed.timestamp() if parsed is not None else 0.0


@router.get("/signals")
async def signals(
    request: Request,
    status: str | None = Query(None),
    setup_class: str | None = Query(None),
):
    rows = await _build_rows(request, status, setup_class)

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
            "flash": request.session.pop("_control_flash", None),
            "active": "signals",
        },
    )


# Column order for the signals CSV — flat, spreadsheet-friendly scalars.
_SIGNAL_EXPORT_COLS = [
    "id", "symbol", "side", "setup_class", "confidence", "status",
    "regime", "pnl_pct", "entry", "sl", "tp1", "channel",
    "created_at_absolute", "created_at_raw",
]


@router.get("/signals/export.csv")
async def signals_export(
    request: Request,
    status: str | None = Query(None),
    setup_class: str | None = Query(None),
):
    """Download the (filtered) signal table as CSV — same merge + filters as
    the page, one row per signal."""
    rows = await _build_rows(request, status, setup_class)
    data = [[r.get(col) for col in _SIGNAL_EXPORT_COLS] for r in rows]
    suffix = status or setup_class or "all"
    return csv_response(f"signals_{suffix}", _SIGNAL_EXPORT_COLS, data)

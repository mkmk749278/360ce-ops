"""Signal table — merges live ``/api/signals`` with monitor ``signals_last100``."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

router = APIRouter()


def _normalize_signal(entry: dict) -> dict:
    """Common shape across the live API and the monitor JSON dumps."""
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
        "created_at": entry.get("created_at") or entry.get("dispatched_at") or entry.get("createdAt"),
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

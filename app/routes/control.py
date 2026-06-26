"""Engine control plane — the dashboard's first *write* surface.

Promoted from read-only on 2026-06-20: Telegram is unavailable in-region,
so ops becomes the manual control surface for the engine.  Two controls
ship here:

* **Auto-execution mode** — off / paper / live (engine-wide, the
  ``/api/auto-mode`` flip the operator used to do over Telegram).
* **Global kill switch** — B18 emergency halt (engage / disengage).

Every action is owner-gated on the engine (the dashboard's static token is
owner-tier) and recorded in the append-only audit log.  We use POST→redirect
→GET with a one-shot session flash so a browser refresh never re-fires a
control action.
"""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import audit
from app.routes.positions import _enrich_row

router = APIRouter()

_VALID_MODES = {"off", "paper", "live"}


def _is_error(result: object) -> bool:
    return isinstance(result, dict) and bool(result.get("error"))


async def _render(request: Request):
    api = request.app.state.engine_api
    settings = request.app.state.settings
    templates = request.app.state.templates

    auto = await api.auto_mode()
    ks = await api.kill_switch_state()
    glob = await api.auto_trade_global_state()
    expiry = await api.signal_expiry_state()
    flash = request.session.pop("_control_flash", None)

    return templates.TemplateResponse(
        "control.html",
        {
            "request": request,
            "active": "control",
            "auto": auto if isinstance(auto, dict) else {},
            "ks": ks if isinstance(ks, dict) else {},
            "glob": glob if isinstance(glob, dict) else {},
            "expiry": expiry if isinstance(expiry, dict) else {},
            "audit": audit.tail(settings.audit_log_path, limit=25),
            "flash": flash,
        },
    )


@router.get("/control")
async def control_page(request: Request):
    return await _render(request)


@router.get("/control/positions")
async def control_positions_partial(request: Request):
    """HTMX partial — the live open-positions table the control panel polls.

    Read-only for now (the foundation of the control panel's position view);
    the per-position close action lands as a separate owner-sign-off PR since
    it fires real Binance order changes through the FSM.
    """
    api = request.app.state.engine_api
    templates = request.app.state.templates
    payload = await api.positions_diag()
    items: list = []
    error = None
    monitor_running = False
    if isinstance(payload, dict):
        if payload.get("error"):
            error = str(payload.get("error"))
        else:
            raw = payload.get("items") or []
            if isinstance(raw, list):
                items = [_enrich_row(it) for it in raw if isinstance(it, dict)]
            monitor_running = bool(payload.get("monitor_running", False))
    # Only genuine open positions (skip phantom placeholder rows).
    items = [
        r for r in items
        if (r.get("symbol") or "").strip()
        and float(r.get("entry") or 0.0) > 0.0
    ]
    items.sort(key=lambda r: -(r.get("minutes_open") or 0))
    return templates.TemplateResponse(
        "_control_positions.html",
        {
            "request": request,
            "rows": items,
            "error": error,
            "monitor_running": monitor_running,
        },
    )


@router.post("/control/auto-mode")
async def control_auto_mode(request: Request, mode: str = Form(...)):
    api = request.app.state.engine_api
    settings = request.app.state.settings
    mode = (mode or "").strip().lower()

    if mode not in _VALID_MODES:
        request.session["_control_flash"] = {
            "ok": False,
            "text": f"Rejected — invalid mode {mode!r}.",
        }
        return RedirectResponse("/control", status_code=303)

    result = await api.set_auto_mode(mode)
    ok = not _is_error(result)
    audit.record(
        settings.audit_log_path,
        action="auto_mode",
        params={"mode": mode},
        result=result if isinstance(result, dict) else {},
        ok=ok,
    )
    code = result.get("status_code") if isinstance(result, dict) else None
    if ok:
        text = f"Auto-mode set to {mode.upper()}."
        flash_ok = True
    elif code == 409:
        # Engine was already in that mode — a no-op, not a real failure.
        text = f"Already in {mode.upper()} — no change."
        flash_ok = True
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        text = f"Auto-mode change failed: {detail}"
        flash_ok = False
    request.session["_control_flash"] = {"ok": flash_ok, "text": text}
    return RedirectResponse("/control", status_code=303)


@router.post("/control/kill-switch")
async def control_kill_switch(
    request: Request,
    engaged: str = Form(...),
    reason: str = Form(""),
):
    api = request.app.state.engine_api
    settings = request.app.state.settings
    engage = engaged.strip().lower() in ("1", "true", "on", "yes", "engage")

    result = await api.set_kill_switch(engage, reason=reason.strip() or None)
    ok = not _is_error(result)
    audit.record(
        settings.audit_log_path,
        action="kill_switch",
        params={"engaged": engage, "reason": reason.strip()},
        result=result if isinstance(result, dict) else {},
        ok=ok,
    )
    if ok:
        text = (
            "KILL SWITCH ENGAGED — all auto-trade halted."
            if engage
            else "Kill switch disengaged — auto-trade resumed."
        )
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        text = f"Kill-switch flip failed: {detail}"
    request.session["_control_flash"] = {"ok": ok, "text": text}
    return RedirectResponse("/control", status_code=303)


@router.post("/control/auto-trade-global")
async def control_auto_trade_global(request: Request, enabled: str = Form(...)):
    api = request.app.state.engine_api
    settings = request.app.state.settings
    enable = enabled.strip().lower() in ("1", "true", "on", "yes", "enable")

    result = await api.set_auto_trade_global(enable)
    ok = not _is_error(result)
    audit.record(
        settings.audit_log_path,
        action="auto_trade_global",
        params={"enabled": enable},
        result=result if isinstance(result, dict) else {},
        ok=ok,
    )
    if ok:
        text = (
            "Global auto-trade ENABLED — new orders allowed engine-wide."
            if enable
            else "Global auto-trade DISABLED — new orders halted (open "
            "positions untouched)."
        )
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        text = f"Global auto-trade flip failed: {detail}"
    request.session["_control_flash"] = {"ok": ok, "text": text}
    return RedirectResponse("/control", status_code=303)


@router.post("/control/signal-expiry")
async def control_signal_expiry(request: Request, enabled: str = Form(...)):
    api = request.app.state.engine_api
    settings = request.app.state.settings
    enable = enabled.strip().lower() in ("1", "true", "on", "yes", "enable")

    result = await api.set_signal_expiry(enable)
    ok = not _is_error(result)
    audit.record(
        settings.audit_log_path,
        action="signal_expiry",
        params={"enabled": enable},
        result=result if isinstance(result, dict) else {},
        ok=ok,
    )
    if ok:
        text = (
            "Signal expiry ENABLED — signals force-close at max hold time."
            if enable
            else "Signal expiry DISABLED — signals now run to TP or SL only "
            "(2h auto-trade reconciler safety net unaffected)."
        )
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        text = f"Signal-expiry flip failed: {detail}"
    request.session["_control_flash"] = {"ok": ok, "text": text}
    return RedirectResponse("/control", status_code=303)


@router.post("/control/reset-signals")
async def control_reset_signals(request: Request):
    """Full signal reset — clears active signals, history, stats, invalidation,
    and paper broker state for all users.  Requires explicit double-confirm in
    the UI (first form sets confirm=pending, second fires the actual reset)."""
    api = request.app.state.engine_api
    settings = request.app.state.settings

    result = await api.reset_signals()
    ok = not _is_error(result)
    audit.record(
        settings.audit_log_path,
        action="signal_reset_full",
        params={},
        result=result if isinstance(result, dict) else {},
        ok=ok,
    )
    if ok:
        active = result.get("cleared_active_signals", 0) if isinstance(result, dict) else 0
        history = result.get("cleared_history", 0) if isinstance(result, dict) else 0
        paper = result.get("paper_positions_closed", 0) if isinstance(result, dict) else 0
        queued = result.get("engine_reset_queued", False) if isinstance(result, dict) else False
        queued_note = " (engine reset queued, propagates in ≤15s)" if queued else ""
        text = (
            f"Full reset complete{queued_note}: "
            f"{active} active signals, {history} history, {paper} paper positions cleared."
        )
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        text = f"Full reset failed: {detail}"
    request.session["_control_flash"] = {"ok": ok, "text": text}
    return RedirectResponse("/control", status_code=303)

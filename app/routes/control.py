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
    flash = request.session.pop("_control_flash", None)

    return templates.TemplateResponse(
        "control.html",
        {
            "request": request,
            "active": "control",
            "auto": auto if isinstance(auto, dict) else {},
            "ks": ks if isinstance(ks, dict) else {},
            "audit": audit.tail(settings.audit_log_path, limit=25),
            "flash": flash,
        },
    )


@router.get("/control")
async def control_page(request: Request):
    return await _render(request)


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

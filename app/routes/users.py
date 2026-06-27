"""Manual tier grant — owner-only comp for testers/influencers.

Second write surface on the control plane (after kill-switch / auto-mode /
auto-trade-global / signal-expiry / reset-signals): lets the owner look up
a user by phone and manually set their subscription tier
(``free`` / ``assist`` / ``auto``) without a Play Billing purchase — e.g.
comping a tester or an influencer. Calls the engine's owner-gated
``GET /api/admin/users/lookup`` and ``POST /api/admin/grant-tier``
(360-v2 PR #655); every grant carries an expiry (default 30 days,
1-365 range) except ``tier=free`` which revokes immediately.

Same POST→redirect→GET + audit-log pattern as the rest of ``control.py``.
The lookup itself is a read, so it is not audited — only the grant write is.
"""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import audit

router = APIRouter()

_VALID_TIERS = {"free", "assist", "auto"}


def _is_error(result: object) -> bool:
    return isinstance(result, dict) and bool(result.get("error"))


@router.get("/control/users")
async def control_users_page(request: Request, phone: str = ""):
    api = request.app.state.engine_api
    templates = request.app.state.templates
    flash = request.session.pop("_control_flash", None)

    phone = phone.strip()
    lookup = None
    not_found = False
    if phone:
        result = await api.user_lookup(phone)
        if isinstance(result, dict) and result.get("status_code") == 404:
            not_found = True
        elif _is_error(result):
            flash = flash or {"ok": False, "text": f"Lookup failed: {result.get('error')}"}
        else:
            lookup = result

    return templates.TemplateResponse(
        "users.html",
        {
            "request": request,
            "active": "control",
            "phone": phone,
            "lookup": lookup,
            "not_found": not_found,
            "flash": flash,
        },
    )


@router.post("/control/users/lookup")
async def control_users_lookup(request: Request, phone: str = Form(...)):
    phone = phone.strip()
    return RedirectResponse(f"/control/users?phone={phone}", status_code=303)


@router.post("/control/users/grant")
async def control_users_grant(
    request: Request,
    phone: str = Form(...),
    tier: str = Form(...),
    duration_days: int = Form(30),
    reason: str = Form(""),
):
    api = request.app.state.engine_api
    settings = request.app.state.settings
    phone = phone.strip()
    tier = tier.strip().lower()

    if tier not in _VALID_TIERS:
        request.session["_control_flash"] = {
            "ok": False,
            "text": f"Rejected — invalid tier {tier!r}.",
        }
        return RedirectResponse(f"/control/users?phone={phone}", status_code=303)

    result = await api.grant_tier(
        phone,
        tier,
        duration_days=duration_days,
        reason=reason.strip() or None,
    )
    ok = not _is_error(result)
    audit.record(
        settings.audit_log_path,
        action="grant_tier",
        params={
            "phone": phone,
            "tier": tier,
            "duration_days": duration_days,
            "reason": reason.strip(),
        },
        result=result if isinstance(result, dict) else {},
        ok=ok,
    )
    if ok:
        if tier == "free":
            text = f"{phone}: tier revoked — now FREE."
        else:
            paid_until = result.get("paid_until") if isinstance(result, dict) else None
            text = f"{phone}: granted {tier.upper()} until {paid_until}."
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        text = f"Grant failed: {detail}"
    request.session["_control_flash"] = {"ok": ok, "text": text}
    return RedirectResponse(f"/control/users?phone={phone}", status_code=303)

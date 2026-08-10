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

The engine stores phone numbers as exact-match E.164 (``UserStore.get_by_phone``
does a plain ``WHERE phone_e164 = ?``, no normalisation) — so the lookup form
must send a ``+``-prefixed number or every lookup silently 404s, even for a
real account. The country-code select below (default India, mirrors the
lumin-app picker) plus ``_to_e164`` close that gap client-side-free, entirely
in this route.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import audit

router = APIRouter()

_VALID_TIERS = {"free", "assist", "auto"}

#: Exit mechanisms a user may be opted into. Mirrors the engine's
#: ``user_overrides.EXIT_MECHANISMS``; the engine validates independently and
#: reads the stored value back, so a drift here is caught by the response
#: rather than silently written.
_VALID_EXIT_MECHANISMS = {"default", "sar", "chandelier"}

# (dial code without "+", display name) — subset of lumin-app's
# lib/data/country_codes.dart kept in sync by hand; India first since it's
# the default and the bulk of the current tester base.
COUNTRY_CODES: list[tuple[str, str]] = [
    ("91", "India"),
    ("1", "United States/Canada"),
    ("44", "United Kingdom"),
    ("971", "United Arab Emirates"),
    ("65", "Singapore"),
    ("61", "Australia"),
    ("60", "Malaysia"),
    ("63", "Philippines"),
    ("62", "Indonesia"),
    ("92", "Pakistan"),
    ("880", "Bangladesh"),
    ("966", "Saudi Arabia"),
    ("234", "Nigeria"),
    ("27", "South Africa"),
    ("49", "Germany"),
    ("33", "France"),
    ("81", "Japan"),
    ("86", "China"),
    ("52", "Mexico"),
    ("55", "Brazil"),
]
_DEFAULT_COUNTRY_CODE = "91"


def _to_e164(country_code: str, raw_phone: str) -> str:
    """Normalise a lookup-form phone to ``+<dial><digits>``.

    A number already starting with ``+`` (pasted full international
    format) passes through with only whitespace/punctuation stripped —
    the country-code select is ignored in that case.
    """
    raw_phone = raw_phone.strip()
    if raw_phone.startswith("+"):
        return "+" + re.sub(r"\D", "", raw_phone)
    digits = re.sub(r"\D", "", raw_phone)
    dial = re.sub(r"\D", "", country_code) or _DEFAULT_COUNTRY_CODE
    return f"+{dial}{digits}"


def _is_error(result: object) -> bool:
    return isinstance(result, dict) and bool(result.get("error"))


def _users_redirect(phone: str) -> RedirectResponse:
    """Build the post-action redirect, percent-encoding ``phone``.

    Starlette's query-string parser decodes a literal ``+`` back to a
    space (form-urlencoded convention), so an unencoded E.164 phone in
    the Location header comes back corrupted on the very next GET —
    every redirect here must percent-encode it.
    """
    return RedirectResponse(f"/control/users?phone={quote(phone, safe='')}", status_code=303)


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
            "active": "users",
            "phone": phone,
            "lookup": lookup,
            "not_found": not_found,
            "flash": flash,
            "country_codes": COUNTRY_CODES,
            "default_country_code": _DEFAULT_COUNTRY_CODE,
        },
    )


@router.post("/control/users/lookup")
async def control_users_lookup(
    request: Request,
    phone: str = Form(...),
    country_code: str = Form(_DEFAULT_COUNTRY_CODE),
):
    phone = _to_e164(country_code, phone)
    return _users_redirect(phone)


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
        return _users_redirect(phone)

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
    return _users_redirect(phone)


@router.post("/control/users/exit-mechanism")
async def control_users_exit_mechanism(
    request: Request,
    phone: str = Form(...),
    exit_mechanism: str = Form(...),
    reason: str = Form(""),
):
    """Opt one account into the live trail governor.

    The one control on this page that changes how a real position EXITS.
    Everything else here is entitlement; this hands the stop to a mechanism.

    The flash names **both** switches, because the per-user value alone
    changes nothing and an operator who sets one without the other would
    otherwise read a bare "ok" as "it is running" — a control that reports
    success for a state that does nothing is the same class as one that 403s.
    """
    api = request.app.state.engine_api
    settings = request.app.state.settings
    phone = phone.strip()
    mech = exit_mechanism.strip().lower()

    if mech not in _VALID_EXIT_MECHANISMS:
        request.session["_control_flash"] = {
            "ok": False,
            "text": f"Rejected — unknown exit mechanism {exit_mechanism!r}.",
        }
        return _users_redirect(phone)

    result = await api.set_exit_mechanism(
        phone, mech, reason=reason.strip() or None
    )
    ok = not _is_error(result)
    audit.record(
        settings.audit_log_path,
        action="set_exit_mechanism",
        params={"phone": phone, "exit_mechanism": mech, "reason": reason.strip()},
        result=result if isinstance(result, dict) else {},
        ok=ok,
    )
    if ok and isinstance(result, dict):
        stored = result.get("exit_mechanism")
        governor_on = result.get("governor_enabled")
        if stored == "default":
            text = f"{phone}: exit returned to the SL/TP FSM."
        elif governor_on:
            text = (
                f"{phone}: exit handed to {str(stored).upper()} — "
                "the master switch is ON, so this is live on the next signal."
            )
        else:
            # Not a failure, and not running either. Naming it is the point.
            text = (
                f"{phone}: exit set to {str(stored).upper()}, but the engine-wide "
                "trail governor is OFF — nothing changes until you enable "
                "'Live trail governor' on Control → Engine."
            )
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        text = f"Exit-mechanism change failed: {detail}"
    request.session["_control_flash"] = {"ok": ok, "text": text}
    return _users_redirect(phone)

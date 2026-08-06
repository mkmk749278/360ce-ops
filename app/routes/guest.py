"""``/guest`` — the read-only door.

The holder of a temporary code exchanges it here for a guest session. Two entry
styles, because the two callers are different:

* **A browser** posts the code to ``/guest`` and gets a session cookie, then
  navigates normally.
* **A machine** (script, agent) can skip the cookie entirely and send
  ``Authorization: Bearer <code>`` — but that is handled by this same POST via
  ``?json=1``, which returns the session cookie in the response like any HTTP
  client expects. There is deliberately **no** per-request code header: a code
  replayed on every request is a code written into every log line and every
  shell history entry it passes through, whereas a cookie minted once carries
  only the grant id.

The failure message is identical for "wrong code", "revoked", "expired" and
"locked out" — the same rule the owner login already follows (F-08): a prober
must not learn which of those it hit. The one exception is the lockout, which is
reported *as a delay* rather than a distinct cause, so an owner who has genuinely
locked himself out is not left staring at "invalid" while waiting.
"""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import audit

router = APIRouter()

_GENERIC = "That code is not valid. It may have expired or been revoked."


def _page(request: Request, error: str | None, status: int = 200, note: str | None = None):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "guest_login.html",
        {"request": request, "error": error, "note": note},
        status_code=status,
    )


@router.get("/guest", response_class=HTMLResponse)
async def guest_get(request: Request, expired: int = 0):
    note = None
    if expired:
        note = "That read-only session has ended — the code was revoked or expired."
    return _page(request, None, note=note)


@router.post("/guest")
async def guest_post(request: Request, code: str = Form(...), json: int = 0):
    store = request.app.state.guest_access
    settings = request.app.state.settings

    locked = store.locked_out()
    grant = store.redeem(code) if not locked else None

    if grant is None:
        audit.record(
            settings.audit_log_path,
            action="guest_login_failed",
            params={"locked_out_sec": int(locked)},
            result={},
            ok=False,
            actor="guest:unknown",
        )
        if json:
            return JSONResponse({"error": _GENERIC}, status_code=401)
        error = _GENERIC
        if locked:
            error = (
                f"Too many failed codes — read-only login is paused for "
                f"{int(locked // 60) + 1} more minute(s)."
            )
        return _page(request, error, status=401)

    request.session.clear()
    request.session["authenticated"] = True
    request.session["role"] = "guest"
    request.session["guest_id"] = grant.grant_id
    audit.record(
        settings.audit_log_path,
        action="guest_login",
        params={"label": grant.label, "expires_in_sec": grant.seconds_remaining},
        result={},
        ok=True,
        actor=f"guest:{grant.label}",
    )
    if json:
        return JSONResponse(
            {
                "ok": True,
                "label": grant.label,
                "expires_in_sec": grant.seconds_remaining,
                "scope": "read-only",
            }
        )
    return RedirectResponse("/", status_code=303)


@router.get("/guest/logout")
async def guest_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/guest", status_code=302)

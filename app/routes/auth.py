"""Login / logout routes — password gate + optional TOTP second factor.

The dashboard is a control plane (kill switch, mode flips, diag runner) so
the login is hardened per audit F-08: when ``OPS_TOTP_SECRET`` is set, the
form requires a 6-digit authenticator code alongside the password.  Unset →
password-only, exactly the pre-2FA behaviour.

Both factors are checked on every attempt and failures return the same
generic message — a wrong password and a wrong code are indistinguishable
to an attacker probing either factor.
"""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": None,
            "totp_enabled": request.app.state.totp_gate.enabled,
        },
    )


@router.post("/login")
async def login_post(
    request: Request,
    password: str = Form(...),
    totp: str = Form(""),
):
    settings = request.app.state.settings
    gate = request.app.state.totp_gate
    password_ok = hmac.compare_digest(password, settings.auth_token)
    totp_ok = gate.verify(totp)
    if not (password_ok and totp_ok):
        templates = request.app.state.templates
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Invalid credentials",
                "totp_enabled": gate.enabled,
            },
            status_code=401,
        )
    request.session["authenticated"] = True
    return RedirectResponse("/", status_code=302)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)

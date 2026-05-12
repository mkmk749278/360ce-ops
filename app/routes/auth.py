"""Login / logout routes — single-password gate."""
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
        {"request": request, "error": None},
    )


@router.post("/login")
async def login_post(request: Request, password: str = Form(...)):
    settings = request.app.state.settings
    if not hmac.compare_digest(password, settings.auth_token):
        templates = request.app.state.templates
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid password"},
            status_code=401,
        )
    request.session["authenticated"] = True
    return RedirectResponse("/", status_code=302)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)

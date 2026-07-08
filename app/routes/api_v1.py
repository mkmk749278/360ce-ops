"""``/api/v1`` — JSON API for the native ops app.

Phase 1 of the native app (``docs/OPS_MOBILE_APP_PLAN.md``): the read surface
plus token auth. These are JSON siblings of the existing HTML pages, reusing
the same ``EngineApiClient`` the web dashboard uses — the app renders them as
native screens.

Auth model (distinct from the web session gate):

* ``POST /api/v1/auth/login`` exchanges the ops password for an app-token.
* Every other ``/api/v1`` route requires ``Authorization: Bearer <token>`` and
  returns **401 JSON** on failure — never the web's 302→/login redirect (which
  is why ``/api/v1`` is exempted from ``AuthRedirectMiddleware``).

Control *writes* are deliberately NOT here yet — they land in Phase 3 on the
owner-gated, audited control surface. This module is read-only.
"""
from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
async def require_app_token(
    request: Request,
    authorization: str = Header(default=""),
) -> None:
    """Dependency: enforce a valid Bearer app-token. 401 JSON on failure."""
    store = request.app.state.app_tokens
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token or not store.verify(token.strip()):
        raise HTTPException(status_code=401, detail="invalid or missing app token")


class LoginBody(BaseModel):
    password: str
    label: str = "ops-app"


@router.post("/auth/login")
async def login(request: Request, body: LoginBody) -> dict[str, Any]:
    """Exchange the ops password for an app-token. Public (no token required)."""
    settings = request.app.state.settings
    if not hmac.compare_digest(body.password, settings.auth_token):
        raise HTTPException(status_code=401, detail="invalid password")
    token = request.app.state.app_tokens.issue(label=body.label)
    return {"token": token, "token_type": "bearer"}


@router.get("/auth/whoami", dependencies=[Depends(require_app_token)])
async def whoami() -> dict[str, Any]:
    """Cheap token-validity probe the app calls on cold start."""
    return {"ok": True, "actor": "owner"}


@router.post("/auth/revoke-all", dependencies=[Depends(require_app_token)])
async def revoke_all(request: Request) -> dict[str, Any]:
    """Lost-phone switch: revoke every issued app-token."""
    revoked = request.app.state.app_tokens.revoke_all()
    return {"revoked": revoked}


# ---------------------------------------------------------------------------
# read surface (reuses EngineApiClient — same data the web pages render)
# ---------------------------------------------------------------------------
@router.get("/pulse", dependencies=[Depends(require_app_token)])
async def pulse(request: Request) -> dict[str, Any]:
    api = request.app.state.engine_api
    logs = request.app.state.monitor_logs
    return {
        "pulse": await api.pulse(),
        "auto_mode": await api.auto_mode(),
        "heartbeat": await logs.heartbeat(),
    }


@router.get("/signals", dependencies=[Depends(require_app_token)])
async def signals(request: Request) -> Any:
    return await request.app.state.engine_api.signals()


@router.get("/signals/{signal_id}", dependencies=[Depends(require_app_token)])
async def signal_detail(request: Request, signal_id: str) -> Any:
    return await request.app.state.engine_api.signal(signal_id)


@router.get("/positions", dependencies=[Depends(require_app_token)])
async def positions(request: Request) -> Any:
    return await request.app.state.engine_api.positions_diag()


@router.get("/pairs", dependencies=[Depends(require_app_token)])
async def pairs(request: Request) -> Any:
    return await request.app.state.engine_api.pairs()


@router.get("/activity", dependencies=[Depends(require_app_token)])
async def activity(request: Request, setup_class: str | None = None) -> Any:
    return await request.app.state.engine_api.activity(setup_class)


@router.get("/control/state", dependencies=[Depends(require_app_token)])
async def control_state(request: Request) -> dict[str, Any]:
    """Read-only control snapshot: the same state the /control page renders,
    so the app's control screen (Phase 3) can display current state before it
    can write it."""
    api = request.app.state.engine_api
    return {
        "auto_mode": await api.auto_mode(),
        "kill_switch": await api.kill_switch_state(),
        "auto_trade_global": await api.auto_trade_global_state(),
        "signal_expiry": await api.signal_expiry_state(),
        "tunables": await api.tunables_state(),
    }

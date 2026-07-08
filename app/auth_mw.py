"""Auth-redirect middleware. Must be registered INSIDE SessionMiddleware so
``request.session`` is initialised by the time we check it."""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

PUBLIC_PATHS = {"/login", "/logout", "/healthz"}


class AuthRedirectMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static"):
            return await call_next(request)
        # /api/v1 is the native app's surface — it authenticates with a Bearer
        # app-token and returns 401 JSON, so it must bypass the session-cookie
        # redirect (a 302→/login would break a non-browser client).
        if path.startswith("/api/v1"):
            return await call_next(request)
        if not request.session.get("authenticated"):
            return RedirectResponse("/login", status_code=302)
        return await call_next(request)

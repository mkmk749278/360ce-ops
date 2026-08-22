"""Auth-redirect middleware. Must be registered INSIDE SessionMiddleware so
``request.session`` is initialised by the time we check it.

Two roles reach this gate:

* **owner** — the ``OPS_AUTH_TOKEN`` (+ TOTP) session. Full control plane,
  unchanged behaviour.
* **guest** — a temporary read-only grant (``app/guest_access.py``), scoped by
  ``app/guest_scope.py``. Added 2026-08-06 so a collaborator or an agent can
  read the measurement pages without holding the key that arms live trading.

The guest half enforces two things that a login-time check could not:

* **Revocation is immediate.** The session stores the grant *id*, never the
  code, and the grant is re-read from the store on every request. A cookie
  minted before the owner hit Revoke is dead on the next click — otherwise
  "I can disable that access too" would be true only once the cookie expired.
* **The scope is checked per request, not per page.** A guest that navigates
  (or is redirected) into a control route is refused at the door, so no route
  has to remember it is privileged.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from app import audit, guest_scope

PUBLIC_PATHS = guest_scope.PUBLIC_PATHS


class AuthRedirectMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static"):
            return await call_next(request)
        # A guest goes through one path for every request it makes, including
        # /api/v1. Letting the /api/v1 exemption below short-circuit it would
        # have produced the one refusal that never reached the audit log —
        # and a probe of the token surface is precisely what an owner reading
        # that log wants to see.
        if request.session.get("authenticated") and request.session.get("role") == "guest":
            return await self._dispatch_guest(request, call_next)

        # /api/v1 is the native app's surface — it authenticates with a Bearer
        # app-token and returns 401 JSON, so it must bypass the session-cookie
        # redirect (a 302→/login would break a non-browser client).
        if path.startswith("/api/v1"):
            return await call_next(request)
        if not request.session.get("authenticated"):
            return RedirectResponse("/login", status_code=302)

        return await call_next(request)

    # ------------------------------------------------------------------
    async def _dispatch_guest(self, request: Request, call_next) -> Response:
        store = getattr(request.app.state, "guest_access", None)
        grant_id = str(request.session.get("guest_id") or "")
        grant = store.lookup(grant_id) if store is not None else None
        if grant is None:
            # Revoked, expired, or minted by a previous store file. Clear the
            # session so the browser stops presenting a dead credential.
            request.session.clear()
            return RedirectResponse("/guest?expired=1", status_code=302)

        allowed, reason = guest_scope.guest_may(request.app, request.scope)
        if not allowed:
            if store is not None:
                store.record_denial(grant_id)
            settings = request.app.state.settings
            # The RESOLVED route template, beside the concrete path. Without it
            # a denial cannot say which of two faults it is: the route did not
            # resolve at all (`null`), or it resolved and is simply not in the
            # table. Those have different fixes, and "read-only access cannot
            # issue POST" reads identically for both — "blank needs a cause
            # before it gets a caption", at the auth layer.
            #
            # Chasing a live 403 on `POST /diagnostics/console/run` (2026-08-22)
            # cost an hour that this single field would have ended, because the
            # gate allows that route on `main` and refused it in production and
            # nothing recorded which branch ran.
            #
            # Computed only on the denial path, so it costs nothing on a served
            # request, and through the SAME function the decision used — a
            # second resolver here could disagree with the one that refused.
            # Audit only: the 403 page's wording is unchanged, because a prober
            # must not learn the route table from a refusal.
            try:
                _route = guest_scope.matched_route_path(request.app, request.scope)
            except Exception:  # noqa: BLE001 - diagnostics must not break a refusal
                _route = None
            audit.record(
                settings.audit_log_path,
                action="guest_denied",
                params={
                    "path": request.url.path,
                    "method": request.method,
                    "route": _route,
                    "why": reason,
                },
                result={},
                ok=False,
                actor=f"guest:{grant.label}",
            )
            return self._deny(request, reason)

        # Templates read these to hide the control nav and show the countdown.
        # `request.scope` is the same dict the endpoint's Request wraps, so what
        # is set here is what the template sees.
        request.scope["ops_role"] = "guest"
        request.scope["ops_guest_label"] = grant.label
        request.scope["ops_guest_expires_in"] = grant.seconds_remaining
        try:
            return await call_next(request)
        finally:
            # Counters are bumped in memory on the hot path; persist once per
            # request rather than fsyncing inside `lookup`.
            store.flush()

    def _deny(self, request: Request, reason: str) -> Response:
        # A JSON caller gets JSON. An HTML 403 body handed to a script is the
        # same unreadable answer as a 302→/login handed to one.
        if request.url.path.startswith("/api/") or "application/json" in request.headers.get(
            "accept", ""
        ):
            return JSONResponse({"error": reason, "scope": "read-only"}, status_code=403)
        templates = getattr(request.app.state, "templates", None)
        if templates is None:  # pragma: no cover - templates always wired in app
            return Response(f"403 — {reason}", status_code=403)
        return templates.TemplateResponse(
            "guest_denied.html",
            {"request": request, "reason": reason, "path": request.url.path},
            status_code=403,
        )

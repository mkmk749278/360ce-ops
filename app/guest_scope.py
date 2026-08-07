"""What a guest session may reach — the classification, and why it is a table.

A guest holds a temporary read-only code (``app/guest_access.py``). This module
decides, per request, whether that code is allowed to see the thing being asked
for. Three rules, applied in order, and the order is the design:

1. **Method.** A guest may issue ``GET`` and ``HEAD``. Nothing else, ever. This
   is structural: it covers every write route in the app today *and* every one
   added tomorrow, without anybody remembering to update anything.
2. **Route classification.** The matched route's *path template* must be
   classified ``guest``. Unclassified is denied.
3. **Nothing else.** There is no third rule and no override.

**Why rule 2 exists at all, given rule 1.** "GET is safe" is false in this app,
and it is false in the one place you would least expect: ``/exit-backtest/run-
now`` is a ``GET`` link that starts a ``docker exec`` backtest against the
production engine, deliberately, because a proxy was eating the form POST. A
method check alone would hand a read-only guest a job trigger. So the safe set
is enumerated rather than inferred.

**Why a table and not a deny-list.** This repo has paid for the deny-list shape
repeatedly — ``is_tradfi_perp``'s name list, ``MEASUREMENT_SUFFIXES``, the
hand-written key carry in ``_build_scan_context`` — and the lesson each time was
that *a list of what to exclude is silent by construction on the next member*.
Here the next member is a new ops page, and a deny-list would quietly hand it to
every live guest code the day it ships. So the table is **total**: every
registered route is classified exactly once, ``tests/test_guest_access.py``
derives the requirement from ``app.routes`` and fails when a route appears that
nobody has classified, and the runtime default for an unclassified route is
**deny**. A new page is invisible to guests until someone says otherwise, and CI
says so out loud rather than the page silently appearing or silently vanishing.

The classification is keyed on the **route template** (``/signals/{signal_id}``),
not the concrete path, so it cannot be confused by a path segment that happens to
look like a literal page.
"""
from __future__ import annotations

from starlette.routing import Match

#: Methods a guest may issue. Rule 1 — structural, covers every future write.
GUEST_METHODS = frozenset({"GET", "HEAD"})

#: Route templates a guest session may reach. Everything registered and not in
#: here is owner-only; see OWNER_ONLY below for the deliberate exclusions.
GUEST_READ_ROUTES: frozenset[str] = frozenset(
    {
        # -- overview / feed ------------------------------------------------
        "/",
        "/_partial/pulse",
        "/signals",
        "/signals/export.csv",
        "/signals/{signal_id}",
        "/positions",
        "/pairs",
        # -- measurement lanes ---------------------------------------------
        "/signals/sar",
        "/signals/sar/export.csv",
        "/signals/sar-live",
        "/signals/sar-live/export.csv",
        "/signals/dark-live",
        "/signals/dark-live/export.csv",
        "/signals/entry-features",
        "/signals/entry-features/export.csv",
        "/signals/structural-snap",
        "/signals/structural-snap/export.csv",
        "/signals/structural-snap/live.csv",
        "/signals/structural-veto",
        "/signals/price-action",
        "/signals/price-action/export.csv",
        # Read-only census of what the router dropped. No control, no write —
        # it renders counters the engine already publishes.
        "/signals/router-drops",
        # -- performance ----------------------------------------------------
        "/profit",
        "/profit/export.csv",
        "/profit/export.json",
        "/dark-signals",
        "/dark-signals/export.csv",
        "/track-record",
        "/track-record/export.csv",
        "/track-record/trades.csv",
        "/performance",
        "/performance/export.csv",
        "/performance/export.json",
        "/raw-edge",
        "/raw-edge/export.csv",
        "/raw-edge/export.json",
        "/invalidations",
        "/invalidations/export.csv",
        "/invalidations/export.json",
        # The exit backtest's *results* are data; its trigger is not. The run
        # routes are owner-only below — including the GET one, which is exactly
        # why this file classifies routes instead of trusting the method.
        "/exit-backtest",
        "/exit-backtest/download.csv",
        "/exit-backtest/download.md",
        "/_partial/exit-backtest/status",
        # -- autonomy -------------------------------------------------------
        "/strategy-lab",
        "/strategy-lab/export.csv",
        "/strategy-lab/export.json",
        "/_partial/strategy_lab",
        "/emission-controller",
        "/sar-exit",
        "/sar-exit/export.csv",
        "/sar-exit/export.json",
        # -- diagnostics ----------------------------------------------------
        "/truth",
        "/truth/raw.json",
        "/truth/raw.md",
        "/alerts",
        "/diagnostics/data-intake",
        "/audit",
        # The export index and its whitelisted artifacts. `/data/raw/{...}`
        # serves ANY file on the engine volume and stays owner-only: a guest
        # tier should not be the thing that discovers what else is mounted
        # there.
        "/data",
        "/data/download/{name}",
        # Ending one's own session is not a privileged act.
        "/logout",
        "/guest",
        "/guest/logout",
    }
)

#: Registered routes deliberately withheld, with the reason. Kept explicit (and
#: asserted by the test) so "why can't the agent see this" has a written answer
#: rather than an absence.
OWNER_ONLY: dict[str, str] = {
    "/control": "the control panel itself — the owner's exclusion",
    "/control/positions": "control-panel partial",
    "/control/auto-mode": "write",
    "/control/kill-switch": "write",
    "/control/auto-trade-global": "write",
    "/control/signal-expiry": "write",
    "/control/billing": "write",
    "/control/tunables": "write",
    "/control/reset-signals": "write — destructive",
    "/control/close-signal": "write — destructive",
    "/control/users": "subscriber PII",
    "/control/users/lookup": "subscriber PII",
    "/control/users/grant": "write — entitlements",
    "/control/referrals": "subscriber PII",
    "/control/referrals/mark-paid": "write — payouts",
    "/control/access": "the read-only access panel — a guest must not see or mint grants",
    "/control/access/issue": "write — mints access",
    "/control/access/revoke": "write — revokes access",
    "/trials": "subscriber trial grants — PII",
    "/diag/geometry": "runs `docker exec` against the engine container",
    "/diag/paper": "runs `docker exec` against the engine container",
    "/exit-backtest/run": "starts a job on the production engine",
    "/exit-backtest/run-now": "starts a job on the production engine — a GET that writes",
    "/signals/sar/clear": "write — clears the SAR ledger",
    "/data/raw/{rel_path:path}": "serves any file on the engine data volume",
    "/api/v1/auth/login": "the native app's own auth surface",
    "/api/v1/auth/whoami": "app-token surface",
    "/api/v1/auth/revoke-all": "app-token surface",
    "/api/v1/pulse": "app-token surface (owner-tier, includes control state)",
    "/api/v1/signals": "app-token surface",
    "/api/v1/signals/{signal_id}": "app-token surface",
    "/api/v1/positions": "app-token surface",
    "/api/v1/pairs": "app-token surface",
    "/api/v1/activity": "app-token surface",
    "/api/v1/alerts": "app-token surface",
    "/api/v1/truth": "app-token surface",
    "/api/v1/profit": "app-token surface",
    "/api/v1/invalidations": "app-token surface",
    "/api/v1/performance": "app-token surface",
    "/api/v1/analysis-bundle": "app-token surface",
    "/api/v1/control/state": "control state",
    "/api/v1/control/auto-mode": "write",
    "/api/v1/control/kill-switch": "write",
    "/api/v1/control/auto-trade-global": "write",
    "/api/v1/control/signal-expiry": "write",
    "/api/v1/control/tunables": "write",
    "/api/v1/devices": "write — push registry",
    "/openapi.json": "route inventory; nothing a reader needs and a map for a prober",
    "/login": "owner login",
}

#: Paths served before any role check — the login doors and the health probe.
PUBLIC_PATHS = frozenset({"/login", "/logout", "/healthz", "/guest", "/guest/logout"})


def matched_route_path(app, scope) -> str | None:
    """The template of the route this request would be dispatched to.

    Routing has not happened yet when the middleware runs, so we resolve it
    ourselves against the app's own route table. Matching the *template* (rather
    than comparing raw URL strings) is what stops a crafted path from being
    classified as something it is not — the router and the gate then agree by
    construction, because they are the same lookup.
    """
    for route in app.routes:
        try:
            match, _child = route.matches(scope)
        except Exception:  # pragma: no cover - defensive; a bad route must not 500 the gate
            continue
        if match is Match.FULL:
            return getattr(route, "path", None)
    return None


def guest_may(app, scope) -> tuple[bool, str]:
    """``(allowed, reason)`` for a guest issuing this request.

    ``reason`` is always populated on a denial and is what the 403 page and the
    audit row say — "denied" with no cause is the same defect as a blank panel
    with no caption.
    """
    method = scope.get("method", "GET").upper()
    if method not in GUEST_METHODS:
        return False, f"read-only access cannot issue {method}"
    path = matched_route_path(app, scope)
    if path is None:
        # No route matched — let it through to the app's own 404 rather than
        # answering 403, which would tell a prober that the path exists.
        return True, ""
    if path in GUEST_READ_ROUTES:
        return True, ""
    reason = OWNER_ONLY.get(path)
    if reason:
        return False, f"owner-only: {reason}"
    return False, "not classified for read-only access"


def may_use(request, path: str, method: str = "GET") -> bool:
    """May the CURRENT session use this control? — the template-side companion.

    The nav has been filtered from ``GUEST_READ_ROUTES`` since the tier shipped,
    but **in-page controls were not**, and the filtering stopped exactly one
    layer short of the thing that matters most. ``/exit-backtest`` is
    guest-readable and rendered, to a read-only session, both a
    ``POST /exit-backtest/run`` form and a ``GET /exit-backtest/run-now`` link
    that starts a ``docker exec`` job on the production engine — with the copy
    *"Button not responding? Use the plain link"* sitting between them, coaching
    the reader straight into the 403 (2026-08-07).

    The gate held, so this was never a security defect; it is the nav's own rule
    unapplied one level down, and a control that 403s is indistinguishable from
    a broken page. Same two rules as ``guest_may``, read off the same table —
    **not** a second list of "controls a guest may see", because a nav that
    mirrored the gate would drift and the drift is invisible until somebody
    clicks.

    Owner sessions get True unconditionally: this decides what to *render*, and
    the gate above still decides what is *served*.
    """
    if getattr(request, "scope", {}).get("ops_role") != "guest":
        return True
    if method.upper() not in GUEST_METHODS:
        return False
    return str(path) in GUEST_READ_ROUTES

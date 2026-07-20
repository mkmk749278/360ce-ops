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

Control *writes* (Phase 3) live under ``/api/v1/control/*``: each requires the
app-token, calls the engine's owner-gated setter, and appends to the same audit
log as the web control page. The engine remains the source of truth — the app
re-reads ``/control/state`` after every write.
"""
from __future__ import annotations

import hmac
import time
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app import audit

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
    # TOTP second factor (audit F-08) — required when OPS_TOTP_SECRET is
    # set; ignored (may stay empty) when the gate is disabled.
    totp: str = ""


@router.post("/auth/login")
async def login(request: Request, body: LoginBody) -> dict[str, Any]:
    """Exchange the ops password (+ TOTP when enabled) for an app-token.

    Public (no token required). Both factors are always evaluated and the
    failure detail is identical either way, so a probing client can't tell
    which factor failed.
    """
    settings = request.app.state.settings
    gate = request.app.state.totp_gate
    password_ok = hmac.compare_digest(body.password, settings.auth_token)
    totp_ok = gate.verify(body.totp)
    if not (password_ok and totp_ok):
        raise HTTPException(status_code=401, detail="invalid credentials")
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


# ---------------------------------------------------------------------------
# control writes — owner-gated (app-token) + audited; engine is source of truth
# ---------------------------------------------------------------------------
_VALID_MODES = {"off", "paper", "live", "both"}


def _is_error(result: object) -> bool:
    return isinstance(result, dict) and bool(result.get("error"))


async def _audited(
    request: Request,
    *,
    action: str,
    params: dict[str, Any],
    call: Callable[[], Any],
) -> dict[str, Any]:
    """Run an engine control setter, audit it (best-effort, matching the web
    control surface), and return a uniform result the app can act on."""
    settings = request.app.state.settings
    result = await call()
    ok = not _is_error(result)
    audit.record(
        settings.audit_log_path,
        action=action,
        params=params,
        result=result if isinstance(result, dict) else {},
        ok=ok,
        actor="ops-app",
    )
    detail = None
    if not ok:
        detail = result.get("error") if isinstance(result, dict) else str(result)
    return {
        "ok": ok,
        "action": action,
        "detail": detail,
        "result": result if isinstance(result, dict) else None,
    }


class AutoModeBody(BaseModel):
    mode: str


class ToggleBody(BaseModel):
    enabled: bool


class KillSwitchBody(BaseModel):
    engaged: bool
    reason: str = ""


class TunablesBody(BaseModel):
    values: dict[str, Any]


@router.post("/control/auto-mode", dependencies=[Depends(require_app_token)])
async def control_auto_mode(request: Request, body: AutoModeBody) -> dict[str, Any]:
    mode = body.mode.strip().lower()
    if mode not in _VALID_MODES:
        raise HTTPException(status_code=422, detail=f"invalid mode {body.mode!r}")
    return await _audited(
        request,
        action="auto_mode",
        params={"mode": mode},
        call=lambda: request.app.state.engine_api.set_auto_mode(mode),
    )


@router.post("/control/kill-switch", dependencies=[Depends(require_app_token)])
async def control_kill_switch(request: Request, body: KillSwitchBody) -> dict[str, Any]:
    reason = body.reason.strip() or None
    return await _audited(
        request,
        action="kill_switch",
        params={"engaged": body.engaged, "reason": body.reason.strip()},
        call=lambda: request.app.state.engine_api.set_kill_switch(body.engaged, reason=reason),
    )


@router.post("/control/auto-trade-global", dependencies=[Depends(require_app_token)])
async def control_auto_trade_global(request: Request, body: ToggleBody) -> dict[str, Any]:
    return await _audited(
        request,
        action="auto_trade_global",
        params={"enabled": body.enabled},
        call=lambda: request.app.state.engine_api.set_auto_trade_global(body.enabled),
    )


@router.post("/control/signal-expiry", dependencies=[Depends(require_app_token)])
async def control_signal_expiry(request: Request, body: ToggleBody) -> dict[str, Any]:
    return await _audited(
        request,
        action="signal_expiry",
        params={"enabled": body.enabled},
        call=lambda: request.app.state.engine_api.set_signal_expiry(body.enabled),
    )


@router.post("/control/tunables", dependencies=[Depends(require_app_token)])
async def control_tunables(request: Request, body: TunablesBody) -> dict[str, Any]:
    if not body.values:
        raise HTTPException(status_code=422, detail="no tunable values provided")
    return await _audited(
        request,
        action="tunables",
        params={"values": body.values},
        call=lambda: request.app.state.engine_api.set_tunables(body.values),
    )


# ---------------------------------------------------------------------------
# device registry — the app registers its FCM token so the monitoring agent
# can push alerts to it (Phase 4). The agent (separate process) reads the same
# registry; sending lives there, not here.
# ---------------------------------------------------------------------------
class DeviceBody(BaseModel):
    fcm_token: str
    platform: str = "android"


@router.post("/devices", dependencies=[Depends(require_app_token)])
async def register_device(request: Request, body: DeviceBody) -> dict[str, Any]:
    token = body.fcm_token.strip()
    if not token:
        raise HTTPException(status_code=422, detail="fcm_token required")
    request.app.state.device_registry.register(token, platform=body.platform)
    return {"ok": True, "devices": request.app.state.device_registry.count()}


@router.delete("/devices", dependencies=[Depends(require_app_token)])
async def unregister_device(request: Request, body: DeviceBody) -> dict[str, Any]:
    removed = request.app.state.device_registry.unregister(body.fcm_token.strip())
    return {"ok": True, "removed": removed}


# ---------------------------------------------------------------------------
# analysis surfaces — JSON siblings of the web Profit / Alerts / Truth /
# Invalidations pages, reusing the same computations and data sources.
# ---------------------------------------------------------------------------
@router.get("/profit", dependencies=[Depends(require_app_token)])
async def profit(
    request: Request,
    window: str = "live",
    view: str = "all",
    strategy: str = "tp1",
    target_pct: float = 1.0,
    fee_pct: float = 0.07,
    direction: str = "all",
    limit: int = 100,
) -> dict[str, Any]:
    """Signal-analysis surface: the exit-strategy replay the owner analyses
    signals from. Reuses the web profit route's row builder + summaries."""
    from app.routes import profit as p

    target = p._resolve_target_pct(target_pct)
    fee = p._resolve_fee_pct(fee_pct)
    rows, error = await p._build_rows(
        request, view, window, strategy, target, fee, direction, "", ""
    )
    return {
        "summary": p._summary(rows),
        "aggregates": p._aggregates(rows),
        "strategy_summary": p._strategy_summary(rows, fee),
        "breakdown": {
            "scoreband": p._breakdown_scoreband(rows, fee),
            "path": p._breakdown_path(rows, fee),
            "regime": p._breakdown_regime(rows, fee),
        },
        "rows": rows[: max(1, min(limit, 500))],
        "count": len(rows),
        "window": window,
        "view": view,
        "strategy": strategy,
        "target_pct": target,
        "fee_pct": fee,
        "error": error,
    }


@router.get("/alerts", dependencies=[Depends(require_app_token)])
async def alerts(request: Request) -> Any:
    return await request.app.state.agent_alerts.active_alerts()


@router.get("/truth", dependencies=[Depends(require_app_token)])
async def truth(request: Request) -> dict[str, Any]:
    logs = request.app.state.monitor_logs
    return {
        "snapshot": await logs.truth_snapshot(),
        "window": await logs.window_comparison(),
    }


@router.get("/invalidations", dependencies=[Depends(require_app_token)])
async def invalidations(request: Request) -> Any:
    return request.app.state.data_volume.invalidation_records()


@router.get("/performance", dependencies=[Depends(require_app_token)])
async def performance(request: Request) -> Any:
    return request.app.state.data_volume.signal_performance()


# ---------------------------------------------------------------------------
# analysis-bundle — the "mediator" surface: one call returns everything a
# signal-analysis session needs (owner ask, 2026-07-20). It is the live/on-demand
# sibling of the engine's secretless monitor-logs dead-drop: the git drop covers
# the strategy matrix / performance / suppression verdicts without a token, and
# this endpoint adds what only ops can compute — the profit held-to-stop
# exit-replay — plus live tunables, in a single token-gated pull.
#
# Every section is isolated: a failing data source yields {"error": …} for that
# key instead of 500-ing the whole bundle, matching the dashboard's
# adapt-to-shape-drift convention. Bounded by construction (row/record caps) so
# the payload can't blow up on a large data volume.
# ---------------------------------------------------------------------------
async def _section(factory: Callable[[], Any]) -> Any:
    """Call ``factory`` (sync or async), awaiting if needed, and convert any
    failure into an ``{"error": …}`` marker so one broken source can't sink the
    whole bundle. Takes a callable (not a value) so a synchronous source that
    raises is caught here rather than at the call site."""
    import inspect

    try:
        result = factory()
        if inspect.isawaitable(result):
            result = await result
        return result
    except Exception as exc:  # noqa: BLE001 — deliberately fail-soft per section
        return {"error": f"{type(exc).__name__}: {exc}"}


@router.get("/analysis-bundle", dependencies=[Depends(require_app_token)])
async def analysis_bundle(
    request: Request,
    window: str = "all",
    profit_strategy: str = "tp1",
    target_pct: float = 1.0,
    fee_pct: float = 0.07,
    direction: str = "all",
    profit_view: str = "all",
    limit: int = 200,
    invalidation_limit: int = 200,
) -> dict[str, Any]:
    from app.routes import performance as perf_mod
    from app.routes import profit as profit_mod
    from app.routes import strategy_lab as lab_mod

    state = request.app.state
    vol = state.data_volume
    api = state.engine_api
    logs = state.monitor_logs
    row_cap = max(1, min(limit, 1000))

    async def _profit() -> Any:
        target = profit_mod._resolve_target_pct(target_pct)
        fee = profit_mod._resolve_fee_pct(fee_pct)
        rows, error = await profit_mod._build_rows(
            request, profit_view, window, profit_strategy, target, fee, direction, "", ""
        )
        return {
            "summary": profit_mod._summary(rows),
            "aggregates": profit_mod._aggregates(rows),
            "strategy_summary": profit_mod._strategy_summary(rows, fee),
            "breakdown": {
                "scoreband": profit_mod._breakdown_scoreband(rows, fee),
                "path": profit_mod._breakdown_path(rows, fee),
                "regime": profit_mod._breakdown_regime(rows, fee),
            },
            "rows": rows[:row_cap],
            "count": len(rows),
            "target_pct": target,
            "fee_pct": fee,
            "error": error,
        }

    def _performance() -> Any:
        _norm, days = perf_mod._resolve_window(window)
        return perf_mod._aggregate(vol.signal_performance(), days)

    def _invalidations() -> Any:
        records = vol.invalidation_records()
        if isinstance(records, list):
            return {"count": len(records), "records": records[: max(1, invalidation_limit)]}
        return records

    bundle = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "params": {
            "window": window,
            "profit_strategy": profit_strategy,
            "target_pct": target_pct,
            "fee_pct": fee_pct,
            "direction": direction,
            "profit_view": profit_view,
            "row_cap": row_cap,
        },
        "note": (
            "Live/on-demand analysis bundle. The strategy_lab / performance / "
            "suppression data is also published secretless on the engine's "
            "monitor-logs branch (analysis/); profit exit-replay + live tunables "
            "are ops-only and live here."
        ),
        "strategy_lab": await _section(lambda: lab_mod._build_view(vol)),
        "profit": await _section(_profit),
        "performance": await _section(_performance),
        "invalidations": await _section(_invalidations),
        "tunables": await _section(api.tunables_state),
        "truth": {
            "snapshot": await _section(logs.truth_snapshot),
            "window": await _section(logs.window_comparison),
        },
        "alerts": await _section(state.agent_alerts.active_alerts),
    }
    return bundle

"""Async httpx client for engine ``/api/*`` endpoints."""
from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings


class EngineApiClient:
    """Thin async client. Returns JSON or ``{"error": ...}`` on failure so
    templates can render either shape without crashing on transient outages."""

    def __init__(self, settings: Settings) -> None:
        self._base = settings.engine_api_base.rstrip("/")
        self._token = settings.auth_token
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=httpx.Timeout(10.0),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, **params: Any) -> Any:
        try:
            r = await self.client.get(path, params=params or None)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as exc:
            detail: Any = None
            try:
                detail = exc.response.json().get("detail")
            except Exception:
                detail = exc.response.text
            return {
                "error": detail or str(exc),
                "status_code": exc.response.status_code,
                "endpoint": path,
            }
        except httpx.HTTPError as exc:
            return {"error": str(exc), "endpoint": path}

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        """POST a JSON body to an engine control endpoint.

        Returns parsed JSON on success, or ``{"error": ..., "status_code":
        ...}`` on failure so the control route can surface a precise banner
        (409 same-mode, 503 not-initialised, 403 non-owner) instead of a
        generic crash.  The dashboard's static token is owner-tier on the
        engine, so owner-gated writes authorise.
        """
        try:
            r = await self.client.post(path, json=payload)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as exc:
            detail: Any = None
            try:
                detail = exc.response.json().get("detail")
            except Exception:
                detail = exc.response.text
            return {
                "error": detail or str(exc),
                "status_code": exc.response.status_code,
                "endpoint": path,
            }
        except httpx.HTTPError as exc:
            return {"error": str(exc), "endpoint": path}

    async def health(self) -> Any:
        return await self._get("/api/health")

    async def pulse(self) -> Any:
        return await self._get("/api/pulse")

    async def auto_mode(self) -> Any:
        return await self._get("/api/auto-mode")

    async def signals(
        self,
        status: str | None = None,
        setup_class: str | None = None,
        limit: int | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if setup_class:
            params["setup_class"] = setup_class
        if limit is not None:
            params["limit"] = limit
        return await self._get("/api/signals", **params)

    async def signal(self, signal_id: str) -> Any:
        return await self._get(f"/api/signals/{signal_id}")

    async def positions(self) -> Any:
        return await self._get("/api/positions")

    async def positions_diag(self) -> Any:
        """Operator-facing position-state X-ray.

        Owner-tier endpoint shipped in 360-v2 PR #385 that surfaces, per
        active signal, exactly the inputs ``TradeMonitor._evaluate_signal``
        reads — stored SL/TP, 1m candle wick the monitor is comparing
        against, candle-feed age, and ``sl_breach_distance_pct``.
        Distinguishes stale-feed vs monitor-evaluation-bug vs state-sync-gap
        failure modes when a position closes on Binance but stays ACTIVE in
        the engine.

        Auth: same Bearer token the dashboard already uses for ``/api/*``.
        Static-token bypass is treated as owner-tier by the engine, so the
        dashboard's ``OPS_AUTH_TOKEN``-equivalent token is sufficient.
        """
        return await self._get("/internal/diag/positions")

    async def activity(self, setup_class: str | None = None) -> Any:
        params: dict[str, str] = {}
        if setup_class:
            params["setup_class"] = setup_class
        return await self._get("/api/activity", **params)

    async def agents(self) -> Any:
        return await self._get("/api/agents")

    # ---- Control plane (writes — owner-tier) -----------------------------
    # The dashboard is the engine control plane now that Telegram is
    # unavailable in-region (2026-06-20).  These call the engine's
    # owner-gated write endpoints; the static Bearer token is owner-tier.

    async def set_auto_mode(self, mode: str) -> Any:
        """Flip the engine-wide auto-execution mode (off/paper/live/both).

        Engine returns 409 when the requested mode is already active —
        surfaced as an ``error`` so the route renders it as a no-op notice
        rather than a success."""
        return await self._post("/api/auto-mode", {"mode": mode})

    async def kill_switch_state(self) -> Any:
        """Current global kill-switch state ``{engaged, reason,
        initialised}``."""
        return await self._get("/api/kill-switch")

    async def set_kill_switch(self, engaged: bool, reason: str | None = None) -> Any:
        """Engage (halt all auto-trade) or disengage the global kill
        switch.  Owner-gated on the engine."""
        payload: dict[str, Any] = {"engaged": engaged}
        if reason:
            payload["reason"] = reason
        return await self._post("/api/kill-switch", payload)

    async def auto_trade_global_state(self) -> Any:
        """Global ``auto_trade_globally_enabled`` flag ``{enabled,
        initialised}`` — distinct from the kill switch; disabling halts new
        order placement engine-wide without touching existing positions."""
        return await self._get("/api/auto-trade-global")

    async def set_auto_trade_global(self, enabled: bool) -> Any:
        """Enable/disable global auto-trade. Owner-gated on the engine."""
        return await self._post("/api/auto-trade-global", {"enabled": enabled})

    async def signal_expiry_state(self) -> Any:
        """Time-based signal-expiry backstop ``{enabled, initialised}``. When
        disabled (default), signals run to TP/SL only — no max-hold force-close.
        The 2h auto-trade reconciler stale-close net is independent."""
        return await self._get("/api/signal-expiry")

    async def set_signal_expiry(self, enabled: bool) -> Any:
        """Enable/disable the signal-expiry backstop. Owner-gated on the engine."""
        return await self._post("/api/signal-expiry", {"enabled": enabled})

    async def reset_signals(self) -> Any:
        """Full-signal reset: clears active signals, history, stats, invalidation,
        and paper broker state for all users.  Owner-gated on the engine."""
        return await self._post("/api/admin/reset-signals", {})

    async def user_lookup(self, phone: str) -> Any:
        """Look up a user's current tier/paid_until/display_name by phone —
        what the ops UI shows before the owner decides whether/what to
        grant.  Owner-gated on the engine; a 404 (unknown phone) surfaces
        as ``{"error": ..., "status_code": 404}`` via ``_get``'s plain
        ``httpx.HTTPError`` branch."""
        return await self._get("/api/admin/users/lookup", phone=phone)

    async def grant_tier(
        self,
        phone: str,
        tier: str,
        duration_days: int | None = None,
        reason: str | None = None,
    ) -> Any:
        """Manually grant (or revoke, ``tier="free"``) a subscription tier —
        tester/influencer comp, not a Play Billing purchase.  Owner-gated
        on the engine; every grant carries an expiry (engine defaults
        ``duration_days`` to 30 when omitted; ignored for ``tier="free"``)."""
        payload: dict[str, Any] = {"phone": phone, "tier": tier}
        if duration_days is not None:
            payload["duration_days"] = duration_days
        if reason:
            payload["reason"] = reason
        return await self._post("/api/admin/grant-tier", payload)

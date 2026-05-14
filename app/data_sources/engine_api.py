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
        except httpx.HTTPError as exc:
            return {"error": str(exc), "endpoint": path}

    async def health(self) -> Any:
        return await self._get("/api/health")

    async def pulse(self) -> Any:
        return await self._get("/api/pulse")

    async def auto_mode(self) -> Any:
        return await self._get("/api/auto-mode")

    async def signals(self, status: str | None = None, setup_class: str | None = None) -> Any:
        params: dict[str, str] = {}
        if status:
            params["status"] = status
        if setup_class:
            params["setup_class"] = setup_class
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

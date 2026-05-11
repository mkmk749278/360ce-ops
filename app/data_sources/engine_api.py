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

    async def activity(self, setup_class: str | None = None) -> Any:
        params: dict[str, str] = {}
        if setup_class:
            params["setup_class"] = setup_class
        return await self._get("/api/activity", **params)

    async def agents(self) -> Any:
        return await self._get("/api/agents")

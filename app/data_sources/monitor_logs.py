"""Cached fetcher for the engine's ``monitor-logs`` branch artifacts.

We pull from raw.githubusercontent.com rather than git-cloning — keeps the
container image small and avoids needing git inside it. Per-path TTL cache
bounds the fetch rate; on transient HTTP failure we return
``{"error": ...}`` so templates can render the degraded state."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings


@dataclass
class _Cached:
    value: Any
    ts: float


class MonitorLogsReader:
    def __init__(self, settings: Settings) -> None:
        self._base = settings.monitor_logs_base_url.rstrip("/")
        self._ttl = settings.monitor_logs_ttl_sec
        self._cache: dict[str, _Cached] = {}
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _fetch(self, path: str, *, as_json: bool) -> Any:
        async with self._lock:
            cached = self._cache.get(path)
            if cached and (time.time() - cached.ts) < self._ttl:
                return cached.value
            url = f"{self._base}/{path.lstrip('/')}"
            try:
                r = await self.client.get(url)
                r.raise_for_status()
                if as_json:
                    value: Any = r.json()
                else:
                    value = r.text
            except httpx.HTTPError as exc:
                return {"error": str(exc), "url": url}
            except json.JSONDecodeError as exc:
                return {"error": f"json: {exc}", "url": url}
            self._cache[path] = _Cached(value=value, ts=time.time())
            return value

    async def truth_markdown(self) -> str:
        result = await self._fetch("monitor/report/truth_report.md", as_json=False)
        if isinstance(result, dict):
            return f"<!-- error: {result.get('error')} -->\n"
        return result

    async def truth_snapshot(self) -> Any:
        return await self._fetch("monitor/report/truth_snapshot.json", as_json=True)

    async def window_comparison(self) -> Any:
        return await self._fetch("monitor/report/window_comparison.json", as_json=True)

    async def signals_last100(self) -> Any:
        return await self._fetch("monitor/report/signals_last100.json", as_json=True)

    async def dispatch_log(self) -> Any:
        return await self._fetch("monitor/report/dispatch_log.json", as_json=True)

    async def heartbeat(self) -> str:
        result = await self._fetch("monitor/raw/heartbeat.txt", as_json=False)
        if isinstance(result, dict):
            return f"<error: {result.get('error')}>"
        return result

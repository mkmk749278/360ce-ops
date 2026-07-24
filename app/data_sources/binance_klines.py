"""Binance Futures 1m kline fetcher — backing data for the Profit tab's
"held to SL" replay.

We trade USDT-M perpetuals, so candles come from Binance Futures
(``fapi.binance.com/fapi/v1/klines``) — the same source the engine itself
reads (see ``360-v2/src/binance.py``). This is **public market data**: no API
key, no signing, read-only. Nothing here touches engine state.

Pagination: Binance caps a single klines response at 1500 candles. A signal
held for more than ~25h of 1m candles therefore needs several pages; we walk
forward from ``start_ms`` until we reach ``end_ms`` or the server returns a
short page (no more data).

Network policy note: the ops container must be allowed outbound to
``fapi.binance.com`` for this to work. When it can't reach Binance the caller
(``free_run``) degrades gracefully to the engine's own MFE rather than
crashing the page — see ``FreeRunTracker``.
"""
from __future__ import annotations

from typing import NamedTuple

import httpx

from app.config import Settings

# Binance hard cap on klines per request.
_MAX_LIMIT = 1500
_MINUTE_MS = 60_000


class Kline(NamedTuple):
    """One 1m candle, reduced to the fields the replay needs.

    ``open`` was added 2026-07-24 for the Dark-Signals trailing-exit sim, which
    needs the bar open to model gap-through fills (a trailing stop that price
    opens beyond fills at the open, not the stop level). The held-to-stop replay
    (``free_run``) constructs ``Kline`` by keyword and ignores ``open``, so the
    added field is backward-compatible.
    """

    open_time_ms: int
    high: float
    low: float
    close: float
    open: float = 0.0


class BinanceKlinesClient:
    """Async, connection-pooled client for futures 1m klines."""

    def __init__(self, settings: Settings) -> None:
        self._base = settings.binance_futures_rest_base.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base,
                timeout=httpx.Timeout(10.0),
                headers={"Accept": "application/json"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch_1m(self, symbol: str, start_ms: int, end_ms: int) -> list[Kline]:
        """All 1m candles for ``symbol`` in ``[start_ms, end_ms]`` (inclusive).

        Raises ``httpx.HTTPError`` on transport/HTTP failure so the caller can
        decide whether to degrade. Returns candles in ascending time order.
        """
        out: list[Kline] = []
        cursor = start_ms
        # Bound the loop defensively: even a 7-day window is ~7 pages. Cap at
        # 40 pages (~40 days of 1m) so a pathological end_ms can't spin.
        for _ in range(40):
            if cursor > end_ms:
                break
            params = {
                "symbol": symbol,
                "interval": "1m",
                "startTime": cursor,
                "endTime": end_ms,
                "limit": _MAX_LIMIT,
            }
            r = await self.client.get("/fapi/v1/klines", params=params)
            r.raise_for_status()
            rows = r.json()
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                # Binance kline array: [openTime, open, high, low, close, ...]
                try:
                    out.append(
                        Kline(
                            open_time_ms=int(row[0]),
                            open=float(row[1]),
                            high=float(row[2]),
                            low=float(row[3]),
                            close=float(row[4]),
                        )
                    )
                except (IndexError, TypeError, ValueError):
                    continue
            last_open = int(rows[-1][0])
            # Advance past the last candle we received. A short page means we've
            # caught up to the present / end of available data.
            cursor = last_open + _MINUTE_MS
            if len(rows) < _MAX_LIMIT:
                break
        return out

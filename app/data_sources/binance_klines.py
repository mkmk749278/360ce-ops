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

Circuit breaker (2026-07-24): when Binance IP-bans the box (HTTP 418 / 403 with
``-1003 'Way too many requests; IP(...) banned until <ms>'``), every subsequent
kline fetch would otherwise hang until timeout — so the Held-to-stop and
Dark-Signals pages (up to ~500 signals each) crawl on *every* load for the whole
ban window. The breaker trips on the first ban response, parses the ``banned
until`` deadline when present, and short-circuits all fetches until it clears —
so those pages return **instantly in degraded mode** instead of timing out row
by row. This is the ops-side symptom fix; the root cause (a PositionWorker
hammering a dead key into the ban) is fixed engine-side.
"""
from __future__ import annotations

import re
import time
from typing import NamedTuple

import httpx

from app.config import Settings

# Binance hard cap on klines per request.
_MAX_LIMIT = 1500
_MINUTE_MS = 60_000

# Status codes that mean "Binance is refusing this IP for now": 418 (IP banned,
# -1003), 403 (WAF/ban), 429 (rate-limited). We back off on all three.
_BAN_STATUSES = frozenset({403, 418, 429})
_BAN_UNTIL_RE = re.compile(r"banned until (\d+)")


class _BanCircuit:
    """A one-shot backoff gate for Binance IP bans. Pure/monotonic; no I/O.

    ``note_ban`` opens the gate for a cooldown — parsed from the ``banned until``
    epoch-ms in the response when Binance provides it (so the breaker clears the
    moment the real ban lifts), else a fixed fallback. The gate only ever
    *extends* (never shrinks) an open window, so a nearer re-ban can't reopen the
    door early. After the window passes the next fetch probes again (half-open),
    re-tripping if still banned.
    """

    def __init__(self, cooldown_sec: float, max_sec: float) -> None:
        self._cooldown = max(1.0, cooldown_sec)
        self._max = max(self._cooldown, max_sec)
        self._open_until = 0.0  # monotonic deadline

    @property
    def open(self) -> bool:
        return time.monotonic() < self._open_until

    def seconds_remaining(self) -> float:
        return max(0.0, self._open_until - time.monotonic())

    def note_ban(self, body: str, *, now_wall: float | None = None) -> None:
        """Open (or extend) the breaker in response to a ban body."""
        cooldown = self._cooldown
        match = _BAN_UNTIL_RE.search(body or "")
        if match:
            try:
                deadline_wall = int(match.group(1)) / 1000.0
                remaining = deadline_wall - (
                    now_wall if now_wall is not None else time.time()
                )
                if remaining > 0:
                    # +5s guard so we don't probe a hair before the ban lifts.
                    cooldown = remaining + 5.0
            except ValueError:
                pass
        cooldown = max(self._cooldown, min(self._max, cooldown))
        self._open_until = max(self._open_until, time.monotonic() + cooldown)


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
        self._circuit = _BanCircuit(
            settings.binance_ban_cooldown_sec,
            settings.binance_ban_max_sec,
        )
        # Whole-book price cache for fetch_all_prices (TTL enforced there).
        self._prices: dict[str, float] = {}
        self._prices_at: float = 0.0

    @property
    def circuit_open(self) -> bool:
        """True while the box is in a Binance-ban cooldown (fetches short-circuit)."""
        return self._circuit.open

    @property
    def ban_seconds_remaining(self) -> float:
        return self._circuit.seconds_remaining()

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

    async def fetch_all_prices(self, *, ttl_sec: float = 15.0) -> dict[str, float]:
        """Last price for **every** futures symbol, in one request.

        ``/fapi/v1/ticker/price`` with no symbol returns the whole book for
        weight 2 — so marking N open rows costs one call, not N. Doing it
        per-symbol would put the page's cost on the number of running trades,
        which is exactly the hot-path shape the cost rules forbid.

        TTL-cached (a mark that is 15s stale is fine for an operator view) and
        ban-circuit aware. Returns ``{}`` rather than raising when prices are
        unavailable: a missing mark must blank a column, never break the page.
        """
        now = time.monotonic()
        if self._prices and (now - self._prices_at) < ttl_sec:
            return self._prices
        if self._circuit.open:
            return {}
        try:
            r = await self.client.get("/fapi/v1/ticker/price")
            if r.status_code in _BAN_STATUSES:
                self._circuit.note_ban(r.text)
                return {}
            r.raise_for_status()
            rows = r.json()
        except (httpx.HTTPError, ValueError):
            return {}
        if not isinstance(rows, list):
            return {}
        out: dict[str, float] = {}
        for row in rows:
            try:
                price = float(row["price"])
                if price > 0:
                    out[str(row["symbol"])] = price
            except (KeyError, TypeError, ValueError):
                continue
        if out:
            self._prices = out
            self._prices_at = now
        return out

    async def fetch_1m(self, symbol: str, start_ms: int, end_ms: int) -> list[Kline]:
        """All 1m candles for ``symbol`` in ``[start_ms, end_ms]`` (inclusive).

        Raises ``httpx.HTTPError`` on transport/HTTP failure so the caller can
        decide whether to degrade. Returns candles in ascending time order.

        While the ban circuit is open (recent 418/403/429 from Binance), this
        short-circuits immediately with ``httpx.HTTPError`` — no network call —
        so callers degrade instantly instead of timing out row by row.
        """
        if self._circuit.open:
            raise httpx.HTTPError(
                f"binance klines circuit open — IP-ban cooldown "
                f"{self._circuit.seconds_remaining():.0f}s remaining"
            )
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
            # Trip the breaker on a ban response before raising, so the *next*
            # fetch short-circuits instead of re-hitting a banned endpoint.
            if r.status_code in _BAN_STATUSES:
                self._circuit.note_ban(r.text)
                r.raise_for_status()
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

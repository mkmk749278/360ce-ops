"""Free-run ("held to stop") replay for the Profit tab.

The Profit tab answers one question the live Signals tab cannot: *if every
signal were held until its original stop — ignoring the real exits (pre-TP,
invalidation, expiry, TP) the engine actually took — how much profit was on
the table, and what would the result have been?*

This is a **parallel observation only**. It changes nothing about real
trading: the engine's exits still fire for real, the app and the live Signals
tab are untouched. Here we simply replay Binance 1m candles from each signal's
dispatch forward and simulate the single rule "exit only at the stop":

* **Max profit** — the best favourable excursion the trade printed before the
  stop was touched (peak-to-entry).
* **Result** — Closed at the stop (a real SL loss) once price first touches it,
  or still Active if it hasn't yet.

Stop-candle convention is deliberately *pessimistic*: on the candle that first
touches the stop we assume the stop was hit before any favourable wick on that
same candle, so that candle's high/low does **not** inflate max profit. Intrabar
order is unknowable from 1m candles; understating the peak is the honest choice.

Degradation: if Binance candles can't be fetched (network policy, outage) the
tracker falls back to the engine's own ``pnl_pct`` / ``max_favorable_excursion_pct``
for that row and flags it ``degraded`` so the page can say so — never a crash,
never a silent wrong number.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings
from app.data_sources.binance_klines import BinanceKlinesClient, Kline

logger = logging.getLogger("ops.free_run")

_MINUTE_MS = 60_000
_ACTIVE_STATUSES = frozenset({"ACTIVE", "OPEN", "RUNNING"})


@dataclass(frozen=True)
class FreeRunResult:
    """Outcome of replaying one signal under the held-to-stop rule."""

    sl_hit: bool
    # Best favourable excursion (peak-to-entry), always ≥ 0.
    mfe_pct: float | None
    mfe_price: float | None
    # Realised-if-held result: the SL loss when stopped, else live P/L.
    result_pct: float | None
    # Price the row is "at": the stop if hit, else the latest candle close.
    current_price: float | None
    hold_mins: int | None
    capped: bool          # lookback window ended before the stop was touched
    degraded: bool        # candles unavailable → engine values used as fallback
    candles: int
    source: str           # "replay" | "engine-fallback" | "skipped"

    @property
    def is_active(self) -> bool:
        return not self.sl_hit


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dispatch_ms(timestamp: Any) -> int | None:
    if timestamp is None or timestamp == "":
        return None
    try:
        ts = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


def simulate_to_stop(
    klines: list[Kline],
    entry: float,
    sl: float,
    direction: str,
    dispatch_ms: int,
) -> dict[str, Any]:
    """Walk candles under the held-to-stop rule. Pure; no I/O.

    Returns the raw fields ``FreeRunResult`` is built from (minus degraded /
    source, which the caller owns). ``entry`` must be > 0.
    """
    is_long = direction.upper() == "LONG"
    best_fav = entry           # most favourable price seen pre-stop
    sl_hit = False
    sl_open_ms: int | None = None
    last_close: float | None = None

    for k in klines:
        # Check the stop FIRST so the stop candle's favourable wick is excluded
        # from max profit (pessimistic intrabar assumption — see module docs).
        if is_long:
            if k.low <= sl:
                sl_hit = True
                sl_open_ms = k.open_time_ms
                break
        else:
            if k.high >= sl:
                sl_hit = True
                sl_open_ms = k.open_time_ms
                break
        if is_long:
            if k.high > best_fav:
                best_fav = k.high
        else:
            if k.low < best_fav:
                best_fav = k.low
        last_close = k.close

    if is_long:
        mfe_pct = max(0.0, (best_fav - entry) / entry * 100.0)
    else:
        mfe_pct = max(0.0, (entry - best_fav) / entry * 100.0)
    mfe_price = best_fav if mfe_pct > 0 else None

    if sl_hit:
        current_price = sl
        result_pct = ((sl - entry) if is_long else (entry - sl)) / entry * 100.0
        end_ms = sl_open_ms if sl_open_ms is not None else dispatch_ms
    else:
        # Still running: mark to the latest candle close.
        current_price = last_close if last_close is not None else (
            klines[-1].close if klines else None
        )
        if current_price is not None:
            result_pct = ((current_price - entry) if is_long else (entry - current_price)) / entry * 100.0
        else:
            result_pct = None
        end_ms = klines[-1].open_time_ms if klines else dispatch_ms

    hold_mins = max(0, int((end_ms - dispatch_ms) // _MINUTE_MS)) if klines else None

    return {
        "sl_hit": sl_hit,
        "mfe_pct": mfe_pct,
        "mfe_price": mfe_price,
        "result_pct": result_pct,
        "current_price": current_price,
        "hold_mins": hold_mins,
        "candles": len(klines),
    }


class FreeRunTracker:
    """Caches per-signal replay results and fans out the Binance fetches.

    Terminal (stop-hit) results are immutable, so they're cached for the
    process lifetime. Active results carry a short TTL (a still-running signal
    can move) before being re-replayed. Caching is what keeps repeated page
    loads from re-pulling the same candle history.
    """

    def __init__(self, settings: Settings, klines: BinanceKlinesClient) -> None:
        self._klines = klines
        self._enabled = settings.free_run_enabled
        self._max_lookback_ms = settings.free_run_max_lookback_min * _MINUTE_MS
        self._ttl = settings.free_run_cache_ttl_sec
        self._sem = asyncio.Semaphore(max(1, settings.free_run_concurrency))
        # signal_id -> (result, cached_at_monotonic)
        self._cache: dict[str, tuple[FreeRunResult, float]] = {}

    def _fallback(self, signal: dict, source: str) -> FreeRunResult:
        """Engine-value result when replay is disabled or candles fail."""
        raw_status = str(signal.get("status") or "").upper()
        is_active = raw_status in _ACTIVE_STATUSES or raw_status == ""
        mfe = _f(signal.get("max_favorable_excursion_pct"))
        return FreeRunResult(
            sl_hit=not is_active,
            mfe_pct=max(0.0, mfe) if mfe is not None else None,
            mfe_price=None,
            result_pct=_f(signal.get("pnl_pct")),
            current_price=_f(signal.get("current_price")),
            hold_mins=signal.get("hold_mins"),
            capped=False,
            degraded=source != "skipped",
            candles=0,
            source=source,
        )

    async def _replay_one(self, signal: dict) -> FreeRunResult:
        symbol = str(signal.get("symbol") or "")
        entry = _f(signal.get("entry"))
        # Held-to-stop means the *original* protective stop, not the live one.
        # The engine shifts stop_loss to break-even/TP1 as a trade progresses,
        # so a pre-TP signal carries a stop sitting at entry — replaying against
        # that records an instant flat stop-out. Prefer original_stop_loss
        # (exposed by the engine for exactly this), fall back to stop_loss when
        # it's absent (0/None) e.g. before the engine ships the field.
        sl = _f(signal.get("original_stop_loss"))
        if sl is None or sl <= 0:
            sl = _f(signal.get("stop_loss"))
        direction = str(signal.get("direction") or signal.get("side") or "").upper()
        dispatch_ms = _dispatch_ms(signal.get("timestamp"))

        if not symbol or entry is None or entry <= 0 or sl is None or direction not in ("LONG", "SHORT") or dispatch_ms is None:
            # Not enough to replay — fall back to engine values for this row.
            return self._fallback(signal, "engine-fallback")

        now_ms = int(time.time() * 1000)
        end_ms = min(now_ms, dispatch_ms + self._max_lookback_ms)
        capped_window = (dispatch_ms + self._max_lookback_ms) < now_ms

        try:
            async with self._sem:
                klines = await self._klines.fetch_1m(symbol, dispatch_ms, end_ms)
        except httpx.HTTPError as exc:
            logger.warning("free-run candle fetch failed for %s: %s", symbol, exc)
            return self._fallback(signal, "engine-fallback")

        sim = simulate_to_stop(klines, entry, sl, direction, dispatch_ms)
        # "capped" only matters when the stop was never touched within the
        # window — then the Active result is provisional, not the final story.
        capped = capped_window and not sim["sl_hit"]
        # Floor max profit at the engine's own recorded MFE. The engine tracks
        # max-favourable-excursion tick-by-tick over the real trade; that's
        # ground truth and a lower bound on "best it ever showed" — never let a
        # coarser 1m-candle replay report less than what actually happened.
        mfe_pct = sim["mfe_pct"]
        mfe_price = sim["mfe_price"]
        engine_mfe = _f(signal.get("max_favorable_excursion_pct"))
        if engine_mfe is not None and engine_mfe > (mfe_pct or 0.0):
            mfe_pct = engine_mfe
            mfe_price = (
                entry * (1 + mfe_pct / 100.0) if direction == "LONG"
                else entry * (1 - mfe_pct / 100.0)
            )
        return FreeRunResult(
            sl_hit=sim["sl_hit"],
            mfe_pct=mfe_pct,
            mfe_price=mfe_price,
            result_pct=sim["result_pct"],
            current_price=sim["current_price"],
            hold_mins=sim["hold_mins"],
            capped=capped,
            degraded=False,
            candles=sim["candles"],
            source="replay",
        )

    async def compute(self, signal: dict) -> FreeRunResult:
        if not self._enabled:
            return self._fallback(signal, "skipped")

        sid = str(signal.get("signal_id") or signal.get("id") or "")
        cached = self._cache.get(sid) if sid else None
        if cached is not None:
            result, cached_at = cached
            # Terminal results never change; active ones expire on the TTL.
            if result.sl_hit or (time.monotonic() - cached_at) < self._ttl:
                return result

        result = await self._replay_one(signal)
        # Don't cache degraded rows long — we want to retry once Binance is
        # reachable again. A short TTL is enough to avoid hammering on outage.
        if sid and not result.degraded:
            self._cache[sid] = (result, time.monotonic())
        elif sid:
            self._cache[sid] = (result, time.monotonic() - self._ttl + 5)
        return result

    async def compute_many(self, signals: list[dict]) -> dict[str, FreeRunResult]:
        results = await asyncio.gather(*(self.compute(s) for s in signals))
        out: dict[str, FreeRunResult] = {}
        for sig, res in zip(signals, results):
            sid = str(sig.get("signal_id") or sig.get("id") or "")
            if sid:
                out[sid] = res
        return out

"""Dark-Signals trailing-exit bake-off for the Performance tab.

This is a **parallel observation only** — it changes nothing about live trading.
The engine's real exits (pre-TP banking, invalidation, TP/SL) still fire for
real; the app and the live Signals tab are untouched. Here we take every real
signal, keep only its **entry price + direction**, throw away its TPs entirely,
and replay Binance candles forward under a single question:

    *if the ONLY exit were a trailing-stop indicator — no take-profit — how would
    realised P/L compare to what the engine actually did?*

The owner's thesis (Session 2026-07-24): our signals are directionally right but
we leak on the exit — stops too tight get wicked out before the favourable move,
or fixed TPs cap winners that would have run. A volatility-scaled *trailing* exit
rides the trend instead of capping it. This page measures whether that's true,
on real signals, per market regime, before anything touches the money path.

Three trailing methods are measured side by side, each with **no TP** — the trail
IS the stop, present from bar one (so the never-a-naked-position invariant holds
for what we'd advise):

* **ATR-trail (Chandelier)** — stop = running-extreme − mult × ATR, ratcheted in
  our favour only. Intrabar wick stop.
* **SuperTrend(period, mult)** — the ATR-band trend flip the owner saw on the
  chart. Exit when the SuperTrend direction flips against the position
  (close-based, true to the indicator).
* **Parabolic SAR(step, max)** — the accelerating trailing stop. Intrabar wick
  stop.

Honesty rules (mirrors ``free_run`` / ``exit_sim``):

* **Pessimistic intrabar order** — on the exit bar the stop is assumed hit before
  any favourable wick, so that bar never inflates max-favourable-excursion.
* **Gap-through fills at the open**, not the stop level — a bar that opens beyond
  the trail fills worse, never better.
* **Fees + funding are modelled**, because a no-TP trail holds positions far
  longer than a quick scalp, so funding is a real cost here. Funding is a flat
  modelled estimate (``funding_bps_per_8h``), applied as a holding cost to both
  sides — deliberately conservative, and labelled as an estimate in the UI.
* **Warm-up matters** — ATR/SuperTrend need bars before entry to seed. We fetch a
  warm-up window before dispatch; if a method can't seed for a row it returns
  ``None`` for that row rather than inventing a number.

The candles come from Binance Futures public 1m klines (the same source the
engine reads); when they can't be fetched the row degrades and the page says so.
Read-only — this module never mutates engine state, and nothing here runs on the
live money path.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings
from app.data_sources.binance_klines import BinanceKlinesClient, Kline

logger = logging.getLogger("ops.dark_signals")

_MINUTE_MS = 60_000
_ACTIVE_STATUSES = frozenset({"ACTIVE", "OPEN", "RUNNING"})

# The trailing methods this page measures. Keep keys stable — they're used as
# dict keys across the route, template, and CSV export.
METHODS: tuple[str, ...] = ("atr", "supertrend", "sar")
METHOD_LABELS: dict[str, str] = {
    "atr": "ATR-trail (Chandelier)",
    "supertrend": "SuperTrend flip",
    "sar": "Parabolic SAR",
}


# --------------------------------------------------------------------------- #
# Resampled bar
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Bar:
    """One resampled OHLC candle (tf_min-minute) built from 1m klines."""

    open_time_ms: int
    open: float
    high: float
    low: float
    close: float


def resample(klines: list[Kline], tf_min: int) -> list[Bar]:
    """Aggregate ascending 1m klines into ``tf_min``-minute OHLC bars.

    Bars are keyed by the floored bucket start (``open_time_ms // bucket_ms``),
    so a 5-minute bar spans [:00, :05). A ``tf_min`` of 1 is a straight 1:1 map.
    Pure; no I/O. Assumes ``klines`` are time-ascending (as ``fetch_1m`` returns).
    """
    if tf_min <= 1:
        return [
            Bar(k.open_time_ms, k.open or k.close, k.high, k.low, k.close)
            for k in klines
        ]
    bucket_ms = tf_min * _MINUTE_MS
    out: list[Bar] = []
    cur_key: int | None = None
    o = h = lo = c = 0.0
    o_ms = 0
    for k in klines:
        key = (k.open_time_ms // bucket_ms) * bucket_ms
        if cur_key is None or key != cur_key:
            if cur_key is not None:
                out.append(Bar(o_ms, o, h, lo, c))
            cur_key = key
            o_ms = key
            o = k.open or k.close
            h = k.high
            lo = k.low
            c = k.close
        else:
            h = max(h, k.high)
            lo = min(lo, k.low)
            c = k.close
    if cur_key is not None:
        out.append(Bar(o_ms, o, h, lo, c))
    return out


# --------------------------------------------------------------------------- #
# Indicators — self-contained (ops never imports engine code). Wilder-smoothed
# to match the engine's own indicators.py conventions.
# --------------------------------------------------------------------------- #
def wilder_atr(
    highs: list[float], lows: list[float], closes: list[float], period: int
) -> list[float | None]:
    """Average True Range (Wilder smoothing). ``None`` until enough bars."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < period + 1:
        return out
    trs: list[float] = [0.0] * n
    for i in range(1, n):
        trs[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    # Seed with the simple mean of the first ``period`` true ranges (index 1..period).
    atr = sum(trs[1 : period + 1]) / period
    out[period] = atr
    for i in range(period + 1, n):
        atr = (atr * (period - 1) + trs[i]) / period
        out[i] = atr
    return out


def supertrend(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int,
    mult: float,
) -> tuple[list[float | None], list[int | None]]:
    """SuperTrend line + direction (1 up / -1 down). ``None`` until ATR seeds.

    Mirrors the engine's ``indicators.supertrend`` band-flip logic so the shadow
    result matches what the engine would compute for the same series.
    """
    n = len(closes)
    line: list[float | None] = [None] * n
    direction: list[int | None] = [None] * n
    atr = wilder_atr(highs, lows, closes, period)

    upper: list[float | None] = [None] * n
    lower: list[float | None] = [None] * n
    for i in range(n):
        a = atr[i]
        if a is None:
            continue
        hl2 = (highs[i] + lows[i]) / 2.0
        upper[i] = hl2 + mult * a
        lower[i] = hl2 - mult * a

    first = next((i for i in range(n) if upper[i] is not None), None)
    if first is None:
        return line, direction
    direction[first] = 1
    line[first] = lower[first]
    for i in range(first + 1, n):
        if upper[i] is None:
            continue
        # Carry the more protective band forward (standard SuperTrend).
        if not (lower[i] > lower[i - 1] or closes[i - 1] < (lower[i - 1] or -math.inf)):
            lower[i] = lower[i - 1]
        if not (upper[i] < upper[i - 1] or closes[i - 1] > (upper[i - 1] or math.inf)):
            upper[i] = upper[i - 1]
        prev_dir = direction[i - 1] if direction[i - 1] is not None else 1
        if prev_dir == 1:
            if closes[i] < (lower[i] or -math.inf):
                direction[i] = -1
                line[i] = upper[i]
            else:
                direction[i] = 1
                line[i] = lower[i]
        else:
            if closes[i] > (upper[i] or math.inf):
                direction[i] = 1
                line[i] = lower[i]
            else:
                direction[i] = -1
                line[i] = upper[i]
    return line, direction


def parabolic_sar(
    highs: list[float],
    lows: list[float],
    step: float,
    max_step: float,
) -> list[float | None]:
    """Parabolic SAR (Wilder). Returns the stop-and-reverse level per bar.

    Standard implementation: AF starts at ``step``, increments by ``step`` on
    each new extreme up to ``max_step``, and the SAR is bounded by the prior two
    bars' extremes so it can't penetrate the recent range.
    """
    n = len(highs)
    out: list[float | None] = [None] * n
    if n < 2:
        return out
    # Seed direction from the first two bars.
    up = highs[1] >= highs[0]
    af = step
    ep = highs[1] if up else lows[1]
    sar = lows[0] if up else highs[0]
    out[1] = sar
    for i in range(2, n):
        sar = sar + af * (ep - sar)
        if up:
            # SAR can't exceed the prior two lows.
            sar = min(sar, lows[i - 1], lows[i - 2])
            if lows[i] < sar:
                # Flip to down.
                up = False
                sar = ep
                ep = lows[i]
                af = step
            else:
                if highs[i] > ep:
                    ep = highs[i]
                    af = min(af + step, max_step)
        else:
            sar = max(sar, highs[i - 1], highs[i - 2])
            if highs[i] > sar:
                up = True
                sar = ep
                ep = highs[i]
                af = step
            else:
                if lows[i] < ep:
                    ep = lows[i]
                    af = min(af + step, max_step)
        out[i] = sar
    return out


# --------------------------------------------------------------------------- #
# Trailing-exit simulation (pure)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TrailResult:
    """Outcome of replaying one signal under one trailing-exit method."""

    method: str
    exited: bool               # trail fired within the window
    result_pct: float | None   # net realised return (fees + funding deducted)
    gross_pct: float | None    # before fees/funding
    mfe_pct: float | None       # best favourable excursion pre-exit (>= 0)
    exit_price: float | None
    hold_mins: int | None
    fee_pct: float
    funding_pct: float
    bars: int
    reason: str                # "trail" | "open" (still running) | "no-data"


def _stop_series(
    method: str,
    bars: list[Bar],
    period: int,
    mult: float,
    sar_step: float,
    sar_max: float,
) -> tuple[list[float | None], list[int | None] | None]:
    """Per-bar stop level for ``method`` (+ direction series for supertrend)."""
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    if method == "supertrend":
        line, direction = supertrend(highs, lows, closes, period, mult)
        return line, direction
    if method == "sar":
        return parabolic_sar(highs, lows, sar_step, sar_max), None
    # ATR-trail (chandelier): the raw ATR series; the ratcheted stop is built
    # inside the walk because it depends on the running extreme since entry.
    return wilder_atr(highs, lows, closes, period), None


def simulate_trailing_exit(
    bars: list[Bar],
    entry_idx: int,
    entry: float,
    direction: str,
    method: str,
    *,
    period: int,
    mult: float,
    sar_step: float,
    sar_max: float,
    tf_min: int,
    fee_pct: float,
    funding_bps_per_8h: float,
) -> TrailResult:
    """Replay one signal from ``entry_idx`` under a trailing-only exit. Pure.

    ``entry`` is the signal's stated entry price (we enter there, not at the
    bar). Walks forward; on the first bar whose range breaches the trail we exit
    (gap-through fills at the bar open). If the trail never fires within the
    window the position is marked to the last close and flagged still-running.
    """
    is_long = direction.upper() == "LONG"
    n = len(bars)
    if entry_idx >= n or entry <= 0:
        return TrailResult(method, False, None, None, None, None, None,
                           0.0, 0.0, 0, "no-data")

    series, st_dir = _stop_series(method, bars, period, mult, sar_step, sar_max)

    # Need a valid indicator value at entry to have a stop from bar one.
    if series[entry_idx] is None:
        return TrailResult(method, False, None, None, None, None, None,
                           0.0, 0.0, 0, "no-data")

    best_fav = entry
    run_extreme = bars[entry_idx].high if is_long else bars[entry_idx].low
    trail: float | None = None
    exit_price: float | None = None
    exit_idx: int | None = None
    reason = "open"

    for i in range(entry_idx, n):
        b = bars[i]
        lvl = series[i]

        if method == "atr":
            if lvl is None:
                continue
            # Ratchet the running extreme, then the chandelier stop, in our
            # favour only — a trailing stop never loosens.
            if is_long:
                run_extreme = max(run_extreme, b.high)
                cand = run_extreme - mult * lvl
                trail = cand if trail is None else max(trail, cand)
            else:
                run_extreme = min(run_extreme, b.low)
                cand = run_extreme + mult * lvl
                trail = cand if trail is None else min(trail, cand)
            stop_level = trail
        elif method == "supertrend":
            # SuperTrend is a close-based flip: exit when direction turns against
            # the position. The line itself is the stop reference for fill price.
            stop_level = lvl
            assert st_dir is not None
            flipped = (is_long and st_dir[i] == -1) or (not is_long and st_dir[i] == 1)
            if flipped and i > entry_idx:
                # Fill at this bar's close (the flip is confirmed on close).
                exit_price = b.close
                exit_idx = i
                reason = "trail"
                break
            # No flip → update MFE and continue (no intrabar stop for supertrend).
            if is_long:
                best_fav = max(best_fav, b.high)
            else:
                best_fav = min(best_fav, b.low)
            continue
        else:  # sar
            stop_level = lvl
            if stop_level is None:
                continue

        # Intrabar wick stop (atr + sar). Check the stop FIRST so the exit bar's
        # favourable wick never inflates MFE (pessimistic intrabar order). Never
        # evaluated on the entry bar itself — you don't get stopped out by your
        # own entry candle's wick; the trail seeds here and arms next bar.
        if stop_level is not None and i > entry_idx:
            if is_long and b.low <= stop_level:
                # Gap-through: if the bar opened below the stop, we fill at the
                # open (worse), else at the stop level.
                exit_price = min(b.open, stop_level) if b.open < stop_level else stop_level
                exit_idx = i
                reason = "trail"
                break
            if (not is_long) and b.high >= stop_level:
                exit_price = max(b.open, stop_level) if b.open > stop_level else stop_level
                exit_idx = i
                reason = "trail"
                break

        # Not stopped this bar → extend the favourable excursion.
        if is_long:
            best_fav = max(best_fav, b.high)
        else:
            best_fav = min(best_fav, b.low)

    exited = exit_price is not None
    if not exited:
        # Still running: mark to the last close in the window.
        exit_price = bars[-1].close
        exit_idx = n - 1
        reason = "open"

    if is_long:
        gross = (exit_price - entry) / entry * 100.0
        mfe = max(0.0, (best_fav - entry) / entry * 100.0)
    else:
        gross = (entry - exit_price) / entry * 100.0
        mfe = max(0.0, (entry - best_fav) / entry * 100.0)

    hold_mins = max(0, (exit_idx - entry_idx) * tf_min) if exit_idx is not None else 0
    # Funding: modelled holding cost proportional to time held. 1 bp = 0.01%.
    funding = (funding_bps_per_8h / 100.0) * (hold_mins / 480.0)
    # Round-trip fee only counts once the position has actually closed.
    fee = fee_pct if exited else 0.0
    net = gross - fee - funding

    return TrailResult(
        method=method,
        exited=exited,
        result_pct=net,
        gross_pct=gross,
        mfe_pct=mfe,
        exit_price=exit_price,
        hold_mins=hold_mins,
        fee_pct=fee,
        funding_pct=funding,
        bars=(exit_idx - entry_idx + 1) if exit_idx is not None else 0,
        reason=reason,
    )


# --------------------------------------------------------------------------- #
# Tracker — fans out candle fetches, caches, degrades. Mirrors FreeRunTracker.
#
# The expensive part is the Binance fetch, so the tracker caches the raw 1m
# klines per signal and runs the (cheap, pure) resample+sim per request. That
# lets the owner sweep timeframe / period / multiplier from the UI without
# re-pulling candles — the whole point of a bake-off.
# --------------------------------------------------------------------------- #
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


def _regime(signal: dict) -> str:
    """Leading regime token from market_phase / entry_regime / regime."""
    raw = (signal.get("market_phase") or signal.get("entry_regime")
           or signal.get("regime"))
    if not raw:
        return "UNKNOWN"
    return str(raw).split("|")[0].strip() or "UNKNOWN"


@dataclass(frozen=True)
class SimParams:
    """The sweepable knobs for one bake-off run (not the candle fetch window)."""

    tf_min: int
    period: int
    mult: float
    sar_step: float
    sar_max: float
    funding_bps_per_8h: float
    fee_pct: float

    @classmethod
    def from_settings(cls, s: Settings) -> "SimParams":
        return cls(
            tf_min=max(1, s.dark_tf_min),
            period=max(2, s.dark_atr_period),
            mult=max(0.1, s.dark_atr_mult),
            sar_step=max(0.001, s.dark_sar_step),
            sar_max=max(max(0.001, s.dark_sar_step), s.dark_sar_max),
            funding_bps_per_8h=max(0.0, s.dark_funding_bps_per_8h),
            fee_pct=0.07,
        )

    def clamped(self) -> "SimParams":
        """Bound every knob to a sane band (used for query-param overrides)."""
        return SimParams(
            tf_min=min(60, max(1, self.tf_min)),
            period=min(100, max(2, self.period)),
            mult=min(20.0, max(0.1, self.mult)),
            sar_step=min(0.2, max(0.001, self.sar_step)),
            sar_max=min(1.0, max(min(0.2, max(0.001, self.sar_step)), self.sar_max)),
            funding_bps_per_8h=min(100.0, max(0.0, self.funding_bps_per_8h)),
            fee_pct=min(2.0, max(0.0, self.fee_pct)),
        )


@dataclass(frozen=True)
class DarkSignalResult:
    """All trailing-method outcomes for one signal, plus context for grouping."""

    signal_id: str
    symbol: str
    side: str
    entry: float | None
    setup_class: str
    regime: str
    confidence: float | None
    real_pnl_pct: float | None   # engine's actual exit (baseline), if closed
    real_is_active: bool
    timestamp: Any
    minutes_ago: Any
    results: dict[str, TrailResult]   # method -> TrailResult
    degraded: bool
    source: str                       # "replay" | "degraded" | "skipped"


class DarkSignalTracker:
    """Per-signal trailing-exit replay: cached 1m candles, per-request sim.

    Raw klines are cached per signal (closed signals for the process lifetime;
    active signals on a short TTL). The warm-up window before dispatch seeds
    ATR/SuperTrend; the forward window is bounded by ``dark_max_lookback_min``.
    """

    def __init__(self, settings: Settings, klines: BinanceKlinesClient) -> None:
        self._klines = klines
        self._enabled = settings.dark_signals_enabled
        self._max_lookback_ms = settings.dark_max_lookback_min * _MINUTE_MS
        self._ttl = settings.dark_cache_ttl_sec
        self._sem = asyncio.Semaphore(max(1, settings.dark_concurrency))
        self.defaults = SimParams.from_settings(settings)
        # Generous fixed warm-up so any swept (period, tf) still seeds: covers
        # e.g. period 60 on 15m (900m) with room to spare, in one extra page.
        self._warmup_ms = settings.dark_warmup_min * _MINUTE_MS
        # signal_id -> (klines, dispatch_ms, is_active, cached_at_monotonic)
        self._cache: dict[str, tuple[list[Kline], int, bool, float]] = {}

    async def _klines_for(
        self, signal: dict
    ) -> tuple[list[Kline], int] | None:
        """Fetched-or-cached 1m klines + dispatch_ms for a signal, or None."""
        symbol = str(signal.get("symbol") or "")
        direction = str(signal.get("direction") or signal.get("side") or "").upper()
        entry = _f(signal.get("entry") if signal.get("entry") is not None
                   else signal.get("entry_price"))
        dispatch_ms = _dispatch_ms(signal.get("timestamp"))
        if (not symbol or entry is None or entry <= 0
                or direction not in ("LONG", "SHORT") or dispatch_ms is None):
            return None

        sid = str(signal.get("signal_id") or signal.get("id") or "")
        raw_status = str(signal.get("status") or "").upper()
        is_active = raw_status in _ACTIVE_STATUSES or raw_status == ""
        cached = self._cache.get(sid) if sid else None
        if cached is not None:
            klines, d_ms, was_active, cached_at = cached
            # A closed signal's window is stable; an active one may extend.
            if not was_active or (time.monotonic() - cached_at) < self._ttl:
                return klines, d_ms

        now_ms = int(time.time() * 1000)
        start_ms = dispatch_ms - self._warmup_ms
        end_ms = min(now_ms, dispatch_ms + self._max_lookback_ms)
        try:
            async with self._sem:
                klines = await self._klines.fetch_1m(symbol, start_ms, end_ms)
        except httpx.HTTPError as exc:
            logger.warning("dark-signals candle fetch failed for %s: %s", symbol, exc)
            return None
        if not klines:
            return None
        if sid:
            self._cache[sid] = (klines, dispatch_ms, is_active, time.monotonic())
        return klines, dispatch_ms

    def _skeleton(self, signal: dict, source: str) -> DarkSignalResult:
        """A no-replay result (disabled / bad row / candle failure)."""
        raw_status = str(signal.get("status") or "").upper()
        is_active = raw_status in _ACTIVE_STATUSES or raw_status == ""
        empty = {
            m: TrailResult(m, False, None, None, None, None, None, 0.0, 0.0, 0,
                          "no-data")
            for m in METHODS
        }
        return DarkSignalResult(
            signal_id=str(signal.get("signal_id") or signal.get("id") or ""),
            symbol=str(signal.get("symbol") or ""),
            side=str(signal.get("direction") or signal.get("side") or "").upper(),
            entry=_f(signal.get("entry") if signal.get("entry") is not None
                     else signal.get("entry_price")),
            setup_class=str(signal.get("setup_class") or "UNKNOWN"),
            regime=_regime(signal),
            confidence=_f(signal.get("confidence")),
            real_pnl_pct=_f(signal.get("pnl_pct")),
            real_is_active=is_active,
            timestamp=signal.get("timestamp"),
            minutes_ago=signal.get("minutes_ago"),
            results=empty,
            degraded=source != "skipped",
            source=source,
        )

    def _simulate(
        self, signal: dict, klines: list[Kline], dispatch_ms: int, params: SimParams
    ) -> DarkSignalResult:
        """Pure resample+sim for a signal whose candles are already in hand."""
        entry = _f(signal.get("entry") if signal.get("entry") is not None
                   else signal.get("entry_price"))
        direction = str(signal.get("direction") or signal.get("side") or "").upper()
        bars = resample(klines, params.tf_min)
        # Entry bar = the first resampled bar whose bucket covers dispatch.
        entry_idx = None
        bucket_ms = params.tf_min * _MINUTE_MS
        for i, b in enumerate(bars):
            if b.open_time_ms + bucket_ms > dispatch_ms:
                entry_idx = i
                break
        if entry_idx is None or entry is None:
            return self._skeleton(signal, "degraded")

        results = {
            m: simulate_trailing_exit(
                bars, entry_idx, entry, direction, m,
                period=params.period, mult=params.mult,
                sar_step=params.sar_step, sar_max=params.sar_max,
                tf_min=params.tf_min, fee_pct=params.fee_pct,
                funding_bps_per_8h=params.funding_bps_per_8h,
            )
            for m in METHODS
        }
        raw_status = str(signal.get("status") or "").upper()
        is_active = raw_status in _ACTIVE_STATUSES or raw_status == ""
        return DarkSignalResult(
            signal_id=str(signal.get("signal_id") or signal.get("id") or ""),
            symbol=str(signal.get("symbol") or ""),
            side=direction,
            entry=entry,
            setup_class=str(signal.get("setup_class") or "UNKNOWN"),
            regime=_regime(signal),
            confidence=_f(signal.get("confidence")),
            real_pnl_pct=_f(signal.get("pnl_pct")),
            real_is_active=is_active,
            timestamp=signal.get("timestamp"),
            minutes_ago=signal.get("minutes_ago"),
            results=results,
            degraded=False,
            source="replay",
        )

    async def compute(self, signal: dict, params: SimParams) -> DarkSignalResult:
        if not self._enabled:
            return self._skeleton(signal, "skipped")
        fetched = await self._klines_for(signal)
        if fetched is None:
            return self._skeleton(signal, "degraded")
        klines, dispatch_ms = fetched
        return self._simulate(signal, klines, dispatch_ms, params)

    async def compute_many(
        self, signals: list[dict], params: SimParams
    ) -> dict[str, DarkSignalResult]:
        results = await asyncio.gather(
            *(self.compute(s, params) for s in signals)
        )
        out: dict[str, DarkSignalResult] = {}
        for sig, res in zip(signals, results):
            sid = str(sig.get("signal_id") or sig.get("id") or "")
            if sid:
                out[sid] = res
        return out

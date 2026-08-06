"""`/signals/price-action` — what price action produces when it drives.

Phase 5 of the price-action program (engine ``src/price_action_lane.py``), and
the answer to the owner's original question: *if we really follow price action,
what is our signal volume and what is its performance?*

What a row on this page is
--------------------------
A signal **this engine generated from structure alone** — a LevelBook level
swept and reclaimed, with delta confirmation from the footprint, the stop beyond
the sweep wick and the target at the next opposing level. No moving average, no
fixed R-multiple.

It was diverted before the queue. It reached **no channel, no push, no app feed
and no order.** Owner-only, and nothing on this page has cost or made a rupee.

Why it is not on `/signals/dark-live`
--------------------------------------
Both populations live in one ledger — the lane rides it for its **resolver**,
which is correct and was paid for over six sessions of defects. But they are not
the same kind of thing, and the split is enforced at one place
(``dark_signals_live.reduce_rows`` / ``reduce_lane_rows``) rather than in two
that could drift:

* a **dark-live** row cleared the full scoring engine and every gate but the one
  loosened for it;
* a **lane** row has been through none of that — no scoring, no MTF policy, no
  confidence floor, no context gate.

Pooling them would make the dark-live page's own first sentence false, and it is
exactly how 15 structural rows disappeared into 2,418 MA rows in the audit that
started this program.

What the page must keep saying
------------------------------
* **`confidence` is 0.0 on every row, and that is honest, not missing.** The lane
  does not score. A number there would be fabricated performance data on a
  surface an adoption decision reads.
* **PnL % leads and nothing here divides by a stop.** Dispatch sizes at a fixed
  notional, so R equalises nothing.
* **Gross and net, both.** Our book loses ~10x its edge to fees, so a gross-only
  price-action figure answers the wrong question — and §2's standing warning is a
  controlled test of 54 SMC variants where the best win rate was 56.3% and
  **zero were profitable after costs**.
* **"No edge detected" is a supported headline.** If structure adds nothing to
  this book, that is a successful outcome of the program and it saves the
  engineering that would otherwise follow.
* **Volume per day is the first number**, because the owner's question was about
  volume before it was about performance — and a lane that produces two signals a
  week cannot be judged on its win rate whatever that rate says.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Query, Request

from app.routes.dark_signals_live import (
    TABLE_ROW_CAP,
    mark_freshness,
    mark_live_pnl,
    reduce_lane_rows,
)

router = APIRouter()

#: Binance USD-M maker in + taker out. Same default as /track-record so a reader
#: moving between them is comparing like with like.
DEFAULT_FEE_PCT = 0.07


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def summarize(rows: list[dict], *, fee_pct: float) -> dict:
    """Volume, win rate and money — gross and net — over CLOSED rows only.

    Open rows carry a live mark and are shown, but they are **not** in any
    realized figure: an unrealized number pooled into a win rate is a claim about
    trades that have not happened yet.
    """
    closed = [r for r in rows if _f(r.get("pnl_pct")) is not None]
    open_rows = [r for r in rows if _f(r.get("pnl_pct")) is None]

    wins = sum(1 for r in closed if (_f(r.get("pnl_pct")) or 0.0) > 0)
    gross = sum(_f(r.get("pnl_pct")) or 0.0 for r in closed)
    # One round trip per trade, charged to every row — including the winners.
    fees = fee_pct * len(closed)
    net = gross - fees

    # Volume per day, from the row timestamps rather than an assumed window: the
    # lane is deliberately rare, and a rate over a window nobody measured is the
    # kind of number that reads plausible and means nothing.
    stamps = [_f(r.get("emitted_at")) for r in rows]
    stamps = [s for s in stamps if s]
    span_days = None
    if len(stamps) >= 2:
        span_days = max(1e-6, (max(stamps) - min(stamps)) / 86_400.0)

    return {
        "n_total": len(rows),
        "n_closed": len(closed),
        "n_open": len(open_rows),
        "wins": wins,
        "losses": len(closed) - wins,
        "win_rate": (wins / len(closed) * 100.0) if closed else None,
        "gross_pct": gross if closed else None,
        "fees_pct": fees if closed else None,
        "net_pct": net if closed else None,
        "avg_gross_pct": (gross / len(closed)) if closed else None,
        "avg_net_pct": (net / len(closed)) if closed else None,
        "span_days": span_days,
        "per_day": (len(rows) / span_days) if span_days else None,
        "fee_pct": fee_pct,
        # The honest headline when the book is negative net. Stated as a state,
        # not inferred by the reader from a minus sign.
        "verdict": (
            None if not closed
            else ("no_edge" if net <= 0 else "positive_net")
        ),
    }


def concentration(rows: list[dict]) -> dict:
    """Distinct moves behind the row count, and the largest one's share.

    A sweep persists for several bars, so one move can contribute several rows
    — and every rate on this page is computed per ROW. `#816` cost a session
    over exactly this: counted per row a population read 32% win / −0.364R and
    per move 55% / +0.003R, so **the sign of the verdict was an artefact of
    re-detection**.

    The engine throttles (`price_action_lane.EMIT_COOLDOWN_S`), and on
    2026-08-06 that throttle was in memory while this ledger was on disk, so
    every deploy re-armed it and duplicates landed anyway. Fixed engine-side;
    this panel stays, because a throttle can regress and a row count alone
    cannot show it. Entry is rounded into the key — two stamps of the same
    sweep differ in the last tick and are not two moves.
    """
    if not rows:
        return {"n_rows": 0, "n_moves": 0, "top_share": 0.0, "top": "",
                "rows_per_move": 0.0, "n_symbols": 0}
    counts: dict[str, int] = {}
    symbols: set[str] = set()
    for r in rows:
        sym = str(r.get("symbol") or "?")
        symbols.add(sym)
        entry = _f(r.get("entry"))
        # 4 significant figures: the same level re-detected drifts by ticks.
        stamp = f"{entry:.4g}" if entry else "?"
        counts[f"{sym} {r.get('side') or '?'} @{stamp}"] = (
            counts.get(f"{sym} {r.get('side') or '?'} @{stamp}", 0) + 1
        )
    top, n = max(counts.items(), key=lambda kv: kv[1])
    return {
        "n_rows": len(rows),
        "n_moves": len(counts),
        "top_share": n / len(rows),
        "top": top,
        "rows_per_move": len(rows) / len(counts),
        "n_symbols": len(symbols),
    }


#: Layer-1 regime buckets are NOT enumerated here. The engine's regime detector
#: owns the label set, and a list ops keeps would be silent by construction on
#: the next label it adds — `MEASUREMENT_SUFFIXES` wearing a fourth hat. The
#: split iterates whatever the rows carry.


def by_regime(rows: list[dict], *, fee_pct: float, key: str) -> list[dict]:
    """Split the book by layer 1 — "is the prevailing trend with us or not".

    §1 of the price-action program defines this lane's own trigger relative to
    the prevailing trend: a break WITH it is a BOS (continuation), AGAINST it a
    CHoCH (reversal). The lane takes both identically, so this is the first
    split that can say whether that is what is costing it.

    Every row before 2026-08-06 carries no regime at all — the field was
    declared and never assigned — so those land in `unstamped`, which is
    **counted apart from every real bucket**. Folding them into one would let a
    bucket take credit for rows nobody classified, and there is no honest
    backfill: the regime at entry is knowable only at entry (#817).
    """
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(str(r.get(key) or "unstamped"), []).append(r)
    out = []
    for name, bucket in sorted(buckets.items()):
        closed = [x for x in bucket if _f(x.get("pnl_pct")) is not None]
        wins = sum(1 for x in closed if (_f(x.get("pnl_pct")) or 0.0) > 0)
        gross = sum(_f(x.get("pnl_pct")) or 0.0 for x in closed)
        out.append({
            "regime": name,
            "n": len(bucket),
            "n_closed": len(closed),
            "win_rate": (wins / len(closed) * 100.0) if closed else None,
            "avg_net_pct": (
                (gross - fee_pct * len(closed)) / len(closed) if closed else None
            ),
            "unstamped": name == "unstamped",
        })
    return out


def regime_timeframes(rows: list[dict]) -> list[str]:
    """Which timeframes the 5m-column labels actually came from.

    The scanner classifies on 5m and this lane triggers on 15m. Never pool
    timeframes silently — if this returns more than one, the column is mixed and
    the page says so.
    """
    return sorted({str(r.get("regime_tf") or "") for r in rows if r.get("regime_tf")})


def by_level_source(rows: list[dict]) -> list[dict]:
    """Split by which timeframe produced the swept level.

    A 1d level and a 1h level are different obstacles and the program says so —
    pooling them averages a real effect into noise. Reported per source rather
    than as one number for the same reason timeframes are never pooled elsewhere.
    """
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        key = str(r.get("level_source_tf") or "unstamped")
        buckets.setdefault(key, []).append(r)
    out = []
    for key, bucket in sorted(buckets.items()):
        closed = [x for x in bucket if _f(x.get("pnl_pct")) is not None]
        wins = sum(1 for x in closed if (_f(x.get("pnl_pct")) or 0.0) > 0)
        out.append({
            "source": key,
            "n": len(bucket),
            "n_closed": len(closed),
            "win_rate": (wins / len(closed) * 100.0) if closed else None,
            "avg_pct": (
                sum(_f(x.get("pnl_pct")) or 0.0 for x in closed) / len(closed)
                if closed else None
            ),
        })
    return out


@router.get("/signals/price-action")
async def price_action_page(
    request: Request,
    fee_pct: float = Query(DEFAULT_FEE_PCT),
):
    vol = request.app.state.data_volume
    error = ""
    rows: list[dict] = []
    try:
        rows = reduce_lane_rows(vol.dark_signals())
    except Exception as exc:  # noqa: BLE001
        error = f"dark ledger unreadable: {type(exc).__name__}: {exc}"

    # One request for the whole futures book, TTL-cached — marking N open rows
    # costs one call, not N, so the page's cost does not scale with open trades.
    prices: dict = {}
    if rows:
        try:
            prices = await request.app.state.binance_klines.fetch_all_prices()
        except Exception:  # noqa: BLE001
            prices = {}

    now = time.time()
    # Freshness FIRST — a mark is only meaningful once the reader knows whether
    # the row beside it is still being advanced by the engine (#108).
    mark_freshness(rows, now=now)
    mark_live_pnl(rows, prices)

    fee = max(0.0, min(1.0, float(fee_pct)))
    summary = summarize(rows, fee_pct=fee)
    sources = by_level_source(rows)
    conc = concentration(rows)
    regimes_entry = by_regime(rows, fee_pct=fee, key="regime")
    regimes_trigger = by_regime(rows, fee_pct=fee, key="regime_15m")
    regime_tfs = regime_timeframes(rows)
    shown = rows[:TABLE_ROW_CAP]

    return request.app.state.templates.TemplateResponse(
        "price_action.html",
        {
            "request": request,
            "active": "price_action",
            "rows": shown,
            "summary": summary,
            "sources": sources,
            "concentration": conc,
            "regimes_entry": regimes_entry,
            "regimes_trigger": regimes_trigger,
            "regime_tfs": regime_tfs,
            "error": error,
            "fee_pct": fee,
            "capped": len(rows) > len(shown),
            "total_rows": len(rows),
        },
    )

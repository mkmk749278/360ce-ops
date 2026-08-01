"""Track record — every signal we delivered, bucketed by day / week / month.

The owner's problem (2026-07-28): paper trading is **per-user and starts empty**
(`execution/paper_book_registry.py` in the engine), so a new user has to wait a
week to a month before their own book says anything. Meanwhile the engine has
been recording every closed signal since it was built. This page reads that
record and answers "how have the signals actually done" without anyone waiting.

**This is a recorded track record, not a backtest.** Every row is a signal the
router confirmed, tracked forward in real time by ``trade_monitor``, and written
to ``data/signal_performance.json`` at its terminal transition. Nothing here
replays candles or reconstructs an outcome after the fact — that distinction was
the owner's explicit call, and it is what separates this page from
``free_run`` / ``dark_signals`` / ``exit_backtest``, which are counterfactuals
and are labelled as such. Do not add a reconstructed number to this page.

Four rules it inherits from the pages that got them wrong first:

* **R is the headline, not portfolio %.** A "+34% last month" needs an invented
  position size, and the router's ``MAX_SAME_DIRECTION_GLOBAL=3`` cap means two
  users on identical settings get different fills — so a portfolio return is not
  a fact about anyone. ``R = pnl_pct / sl_distance_pct_at_entry`` is, and it is
  the same denominator the SAR arm and the edge matrix use.
* **Divide by the risk taken, not the stop on the record.** The engine moves a
  signal's stop in place — BE shift, TP1 park, trail — so ``stop_loss`` is the
  stop as of the *exit*. This page divided by it until 2026-08-01, which scored
  a BE-shifted −0.1% stop-out as a full −1.00R loss; 9 of 28 SL_HITs in that
  window were that row and it flipped the sign of the headline. Engine
  ``sl_distance_pct_at_entry`` is the risk the trade was sized for.
* **Refuse, don't clamp.** A record without a usable entry risk has no R. It is
  counted in the trade count and excluded from the R average, and the page says
  how many were excluded **and why** — never scored 0R, which would drag every
  average toward zero and read as mediocrity rather than as missing data.
* **Bucket by CLOSE time, not entry.** A day's PnL is the PnL realised that day.
  Bucketing by entry would credit Monday with a trade that closed on Thursday.
* **Disclose concentration.** Overlapping entries into one move are not
  independent evidence — the rule ``/signals/sar`` needed twice. Same
  ``SAME_MOVE_PCT`` definition, mirrored from the engine.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Query, Request

from app.reports import csv_response

router = APIRouter()

# Mirrors the engine's ``SAR_EXIT_SHADOW_SAME_MOVE_PCT`` and the ops
# ``sar_exit.SAME_MOVE_PCT``. Ops ports the engine's math; it does not invent it.
SAME_MOVE_PCT = 0.5

# Granularities the page offers. "week" is ISO week (Mon-start) so a week never
# straddles a month boundary differently from how the owner reads a calendar.
GRANULARITIES = ("day", "week", "month")

# Window presets. ``custom`` means the from/to dates govern.
WINDOWS = ("1d", "7d", "30d", "90d", "all", "custom")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> Optional[datetime]:
    """Engine timestamps are epoch floats; tolerate ISO strings defensively."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _close_time(rec: dict) -> Optional[datetime]:
    """When the PnL was realised.

    ``terminal_outcome_timestamp`` is the close; ``timestamp`` is when the record
    was written, which is the same moment in practice and the only field older
    rows are guaranteed to carry. ``create_timestamp`` is the ENTRY and is
    deliberately last — bucketing a day's PnL by entry credits Monday with a
    trade that closed on Thursday.
    """
    return (
        _parse_ts(rec.get("terminal_outcome_timestamp"))
        or _parse_ts(rec.get("timestamp"))
        or _parse_ts(rec.get("create_timestamp"))
    )


def record_r(rec: dict) -> Optional[float]:
    """R for one closed signal, or None when the record cannot support one.

    ``R = pnl_pct / sl_distance_pct_at_entry``, the same denominator the SAR arm
    and the edge matrix divide by, so a number here is comparable with one there.

    **The denominator is the risk the trade was sized for, and ``stop_loss`` is
    not it.** The engine mutates the signal's stop in place as the trade
    progresses — BE shift, TP1 park, trail — so the ``stop_loss`` on the record
    is the stop as of the *exit*. Dividing by it scored a BE-shifted −0.1%
    stop-out as exactly −1.00R, indistinguishable from a trade that gave back
    its whole designed risk. On the 2026-07-29→08-01 window that was 9 of 28
    SL_HITs and it flipped the sign of the book: −0.088R against a true +0.160R
    (2026-08-01). Engine ``sl_distance_pct_at_entry`` carries the real one.

    Returns None rather than 0.0 when the geometry is missing. A missing
    denominator is "we cannot score this", not "this scored flat", and the two
    must stay distinguishable — scoring it 0R would pull every average toward
    zero and make a data gap read as mediocre performance. Records closed before
    the engine stamp exists carry no usable risk and are refused here; the page
    surfaces them as their own bucket rather than silently shrinking the
    population it averages.
    """
    try:
        sl_distance_pct = float(rec.get("sl_distance_pct_at_entry") or 0.0)
        pnl = float(rec.get("pnl_pct") or 0.0)
    except (TypeError, ValueError):
        return None
    if sl_distance_pct <= 0.0:
        return None
    return pnl / sl_distance_pct


def unscored_reason(rec: dict) -> Optional[str]:
    """Why this record has no R — or None when it has one.

    Two states that look identical on a page and have different fixes, so they
    are never pooled into one sentence:

    * ``awaiting_engine_stamp`` — closed before the engine carried
      ``sl_distance_pct_at_entry`` (2026-08-01). The risk it was sized for is
      genuinely unrecoverable: the stop on the record has already been moved.
      This population only shrinks, and it shrinks on its own.
    * ``no_geometry`` — stamped by the current engine and still unusable, i.e.
      a signal whose evaluator never recorded an entry→SL distance. That is a
      producer-side fault and does *not* age out; if this count is non-zero and
      not falling, something upstream is failing to stamp.
    """
    try:
        if float(rec.get("sl_distance_pct_at_entry") or 0.0) > 0.0:
            return None
    except (TypeError, ValueError):
        pass
    return "awaiting_engine_stamp" if "sl_distance_pct_at_entry" not in rec else "no_geometry"


def _regime(rec: dict) -> str:
    """The 5m regime at entry.

    Engine PR #817 added ``entry_regime`` to the record. Everything closed
    before that deploy carries nothing, and there is no honest backfill — the
    regime at entry is knowable only at entry — so those rows read UNPLACED and
    are visible as their own bucket rather than being folded into a real regime.
    """
    return str(rec.get("entry_regime") or "").strip() or "UNPLACED"


def _bucket_key(when: datetime, granularity: str) -> str:
    if granularity == "month":
        return when.strftime("%Y-%m")
    if granularity == "week":
        iso = when.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    return when.strftime("%Y-%m-%d")


def distinct_moves(rows: list[dict]) -> int:
    """How many distinct moves these rows describe.

    Greedy single-pass clustering per (symbol, direction), oldest first: a row
    opens a new move unless it sits within ``SAME_MOVE_PCT`` of the move
    currently open on that key. Anchored on the open move, not the previous row,
    so a slow walk cannot drift arbitrarily far and still count as one move.

    Disclosure, not de-duplication — no row is dropped and the averages still
    cover every row.
    """
    open_move: dict[tuple[str, str], float] = {}
    moves = 0
    for row in sorted(rows, key=lambda r: r.get("closed_at_ts") or 0.0):
        key = (str(row.get("symbol", "")), str(row.get("direction", "")))
        try:
            entry = float(row.get("entry") or 0.0)
        except (TypeError, ValueError):
            continue
        if entry <= 0:
            continue
        anchor = open_move.get(key)
        if anchor is not None and abs(entry - anchor) / anchor * 100.0 < SAME_MOVE_PCT:
            continue
        open_move[key] = entry
        moves += 1
    return moves


def reduce_records(records: Any) -> list[dict]:
    """Flatten ``signal_performance.json`` into display rows (pure).

    Rows carry their own close timestamp and R so every downstream consumer
    reads one definition of both rather than re-deriving them.
    """
    out: list[dict] = []
    rows = records if isinstance(records, list) else []
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        closed = _close_time(rec)
        if closed is None:
            # No usable time means it cannot land in any bucket. Counted in the
            # page's "undateable" figure rather than silently dropped.
            out.append({
                "signal_id": str(rec.get("signal_id", "")),
                "symbol": str(rec.get("symbol", "")),
                "direction": str(rec.get("direction", "")).upper(),
                "setup": str(rec.get("setup_class") or "UNKNOWN"),
                "regime": _regime(rec),
                "entry": rec.get("entry"),
                "stop_loss": rec.get("stop_loss"),
                "pnl_pct": rec.get("pnl_pct"),
                "r_multiple": record_r(rec),
                "unscored_reason": unscored_reason(rec),
                "outcome": str(rec.get("outcome_label") or ""),
                "hit_sl": bool(rec.get("hit_sl")),
                "hit_tp": int(rec.get("hit_tp") or 0),
                "closed_at": None,
                "closed_at_ts": None,
                "closed_iso": "",
            })
            continue
        out.append({
            "signal_id": str(rec.get("signal_id", "")),
            "symbol": str(rec.get("symbol", "")),
            "direction": str(rec.get("direction", "")).upper(),
            "setup": str(rec.get("setup_class") or "UNKNOWN"),
            "regime": _regime(rec),
            "entry": rec.get("entry"),
            "stop_loss": rec.get("stop_loss"),
            "pnl_pct": rec.get("pnl_pct"),
            "r_multiple": record_r(rec),
            "unscored_reason": unscored_reason(rec),
            "outcome": str(rec.get("outcome_label") or ""),
            "hit_sl": bool(rec.get("hit_sl")),
            "hit_tp": int(rec.get("hit_tp") or 0),
            "closed_at": closed,
            "closed_at_ts": closed.timestamp(),
            "closed_iso": closed.isoformat(),
        })
    out.sort(key=lambda r: r.get("closed_at_ts") or 0.0, reverse=True)
    return out


def resolve_range(
    window: str, date_from: str, date_to: str, *, now: Optional[datetime] = None
) -> tuple[Optional[datetime], Optional[datetime], str]:
    """Turn the window/date inputs into a concrete (start, end, label).

    An unparseable custom date collapses to open-ended rather than 500ing or —
    worse — silently returning zero rows, which would read as "no trades" when
    it means "bad input".
    """
    now = now or datetime.now(tz=timezone.utc)
    if window == "custom":
        start = _parse_iso_date(date_from)
        end = _parse_iso_date(date_to)
        if end is not None:
            # Inclusive end date: the user means all of that day.
            end = end + timedelta(days=1)
        label = f"{date_from or '…'} → {date_to or '…'}"
        return start, end, label
    days = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}.get(window)
    if days is None:
        return None, None, "all time"
    return now - timedelta(days=days), None, f"last {days}d"


def _parse_iso_date(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def filter_rows(
    rows: list[dict],
    *,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    regime: str = "",
    setup: str = "",
    symbol: str = "",
    direction: str = "",
) -> list[dict]:
    """Apply the selectors (pure). Undateable rows drop out of any dated range."""
    out = list(rows)
    if start is not None or end is not None:
        kept = []
        for r in out:
            when = r.get("closed_at")
            if when is None:
                continue
            if start is not None and when < start:
                continue
            if end is not None and when >= end:
                continue
            kept.append(r)
        out = kept
    if regime:
        out = [r for r in out if r["regime"] == regime]
    if setup:
        out = [r for r in out if r["setup"] == setup]
    if symbol:
        out = [r for r in out if r["symbol"] == symbol]
    if direction:
        out = [r for r in out if r["direction"] == direction]
    return out


def summarize(rows: list[dict]) -> dict:
    """Headline over whatever the filters selected.

    Two denominators, kept apart on purpose:

    * ``n`` — every selected trade.
    * ``scored`` — those with a usable R. The R figures divide by THIS, and the
      gap between the two is shown rather than absorbed.
    """
    scored = [r for r in rows if r["r_multiple"] is not None]
    awaiting = sum(1 for r in rows if r.get("unscored_reason") == "awaiting_engine_stamp")
    no_geometry = sum(1 for r in rows if r.get("unscored_reason") == "no_geometry")
    r_values = [float(r["r_multiple"]) for r in scored]
    wins = sum(1 for v in r_values if v > 0)
    pnls = []
    for r in rows:
        try:
            pnls.append(float(r["pnl_pct"] or 0.0))
        except (TypeError, ValueError):
            pass
    moves = distinct_moves(rows)
    return {
        "n": len(rows),
        "moves": moves,
        "rows_per_move": (len(rows) / moves) if moves else None,
        "scored": len(scored),
        "unscored": len(rows) - len(scored),
        # Named apart because the fixes differ: one ages out on its own, the
        # other is a producer-side fault that does not.
        "unscored_awaiting_stamp": awaiting,
        "unscored_no_geometry": no_geometry,
        "wins": wins,
        "losses": len(r_values) - wins,
        "win_rate": (wins / len(r_values)) if r_values else None,
        "total_r": sum(r_values) if r_values else None,
        "avg_r": (sum(r_values) / len(r_values)) if r_values else None,
        "best_r": max(r_values) if r_values else None,
        "worst_r": min(r_values) if r_values else None,
        "avg_pnl_pct": (sum(pnls) / len(pnls)) if pnls else None,
    }


def bucket_rows(rows: list[dict], granularity: str) -> list[dict]:
    """Group into day / week / month buckets, newest first.

    Each bucket carries the same summary shape as the headline, so a bucket and
    the total are never computed two different ways.
    """
    if granularity not in GRANULARITIES:
        granularity = "day"
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        when = row.get("closed_at")
        if when is None:
            continue
        grouped[_bucket_key(when, granularity)].append(row)
    out = []
    for key in sorted(grouped, reverse=True):
        bucket = summarize(grouped[key])
        bucket["bucket"] = key
        out.append(bucket)
    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

_EXPORT_COLS = [
    "bucket", "n", "moves", "scored", "unscored", "wins", "losses",
    "win_rate", "total_r", "avg_r", "best_r", "worst_r", "avg_pnl_pct",
]


def _page_context(request: Request, **q) -> dict:
    vol = request.app.state.data_volume
    raw = vol.signal_performance()
    error = raw.get("error") if isinstance(raw, dict) else None
    all_rows = reduce_records(raw)

    start, end, range_label = resolve_range(
        q["window"], q["date_from"], q["date_to"]
    )
    # Every selector is measured with every OTHER filter applied, never its own
    # — a selector applied to its own counts makes each option describe only
    # itself (the rule /signals/sar needed).
    scoped = filter_rows(
        all_rows, start=start, end=end,
        setup=q["setup"], symbol=q["symbol"], direction=q["direction"],
    )
    rows = filter_rows(scoped, regime=q["regime"])
    return {
        "rows": rows,
        "all_rows": all_rows,
        "scoped": scoped,
        "start": start,
        "end": end,
        "range_label": range_label,
        "error": error,
        "undateable": sum(1 for r in all_rows if r.get("closed_at") is None),
    }


@router.get("/track-record")
async def track_record(
    request: Request,
    window: str = Query("30d"),
    date_from: str = Query(""),
    date_to: str = Query(""),
    granularity: str = Query("day"),
    regime: str = Query(""),
    setup: str = Query(""),
    symbol: str = Query(""),
    direction: str = Query(""),
):
    """Delivered signals, bucketed — the record a new subscriber would read.

    Deliberately NOT the Profit tab: nothing here is replayed or reconstructed.
    Every number is what the engine recorded as the trade happened.
    """
    if window not in WINDOWS:
        window = "30d"
    if granularity not in GRANULARITIES:
        granularity = "day"
    q = dict(
        window=window, date_from=date_from, date_to=date_to,
        regime=regime, setup=setup, symbol=symbol, direction=direction,
    )
    ctx = _page_context(request, **q)
    rows, all_rows, scoped = ctx["rows"], ctx["all_rows"], ctx["scoped"]

    return request.app.state.templates.TemplateResponse("track_record.html", {
        "request": request,
        "active": "track_record",
        "buckets": bucket_rows(rows, granularity),
        "summary": summarize(rows),
        "granularity": granularity,
        "granularities": GRANULARITIES,
        "windows": WINDOWS,
        "range_label": ctx["range_label"],
        "same_move_pct": SAME_MOVE_PCT,
        "undateable": ctx["undateable"],
        "total_records": len(all_rows),
        # Option lists come from the whole ledger so a selector never hides the
        # value you would need to select next.
        "regimes": sorted({r["regime"] for r in all_rows}),
        "setups": sorted({r["setup"] for r in all_rows}),
        "symbols": sorted({r["symbol"] for r in all_rows if r["symbol"]}),
        # Regime counts measured with every other filter applied but not their own.
        "regime_counts": {
            name: sum(1 for r in scoped if r["regime"] == name)
            for name in sorted({r["regime"] for r in all_rows})
        },
        "filter_window": window,
        "filter_from": date_from,
        "filter_to": date_to,
        "filter_regime": regime,
        "filter_setup": setup,
        "filter_symbol": symbol,
        "filter_direction": direction,
        "error": ctx["error"],
    })


@router.get("/track-record/export.csv")
async def track_record_export(
    request: Request,
    window: str = Query("30d"),
    date_from: str = Query(""),
    date_to: str = Query(""),
    granularity: str = Query("day"),
    regime: str = Query(""),
    setup: str = Query(""),
    symbol: str = Query(""),
    direction: str = Query(""),
):
    """The buckets as CSV, honouring the current filter."""
    if granularity not in GRANULARITIES:
        granularity = "day"
    q = dict(
        window=window if window in WINDOWS else "30d",
        date_from=date_from, date_to=date_to,
        regime=regime, setup=setup, symbol=symbol, direction=direction,
    )
    ctx = _page_context(request, **q)
    buckets = bucket_rows(ctx["rows"], granularity)
    data = [[b.get(col) for col in _EXPORT_COLS] for b in buckets]
    return csv_response(f"track_record_{granularity}", _EXPORT_COLS, data)

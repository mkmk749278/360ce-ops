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

Rules it carries:

* **PnL % leads and nothing here divides by a stop** (owner, 2026-08-03). This
  page led with R until that date, and R described 6% of it: **421 of 448**
  trades in the owner's 30d window carry no ``sl_distance_pct_at_entry``, so
  ``-5.17R total / -0.192R avg / 26% win`` were computed on 27 rows while the
  ``avg_pnl_pct`` beside them covered all 448, and the page never said the two
  described different populations. R also equalises nothing here:
  ``signal_dispatch`` sizes at a **fixed notional** (``raw_qty = notional /
  entry_price``), so the stop distance is absent from the sizing and a 0.80%
  loss and a 6.14% loss both read −1.00R at very different cost. A percentage
  needs no denominator, so it cannot silently shrink its own population. The
  Strategy Lab remains the R-keyed surface; this one is money.
* **The position size is an INPUT, not an assumption.** The old copy refused a
  portfolio figure because it "needs an assumed position size". That objection
  is about an *assumed* size — here the owner types it, so the page states the
  assumption on screen beside the number instead of hiding one. Every dollar
  figure is ``amount × pnl_pct / 100``: fixed size, no compounding, every
  delivered signal taken.
* **Charge the fee, and say what it is.** The window above is −$3.21 gross at
  $100/signal and −$34.57 once a 0.07% round trip is charged — the cost of
  trading is ~10× the edge, so a gross-only figure answers the wrong question.
  Gross stays visible beside net so the split is readable.
* **Bucket by CLOSE time, not entry.** A day's PnL is the PnL realised that day.
  Bucketing by entry would credit Monday with a trade that closed on Thursday.
* **Disclose concentration.** Overlapping entries into one move are not
  independent evidence — the rule ``/signals/sar`` needed twice. Same
  ``SAME_MOVE_PCT`` definition, mirrored from the engine.
* **Refuse, don't clamp.** A row with no parseable ``pnl_pct`` is counted in the
  trade count and excluded from every money figure, and a row with no entry
  timestamp is named in the concurrency panel rather than assumed never to have
  overlapped. Neither is scored zero.
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

# What the owner types when he asks "what if I traded every signal with this".
# A notional, not a margin: it is what the engine's own sizing means by
# ``notional`` in ``raw_qty = notional / entry_price``, so $PnL is exactly
# ``amount * pnl_pct / 100`` with no leverage assumption invented.
DEFAULT_AMOUNT_USDT = 100.0

# Round-trip cost, both legs, as a percentage of notional. 0.07 = Binance USD-M
# maker 0.02% on the entry + taker 0.05% on the exit — the owner's default
# (2026-08-03). Editable, and 0 renders the gross book.
DEFAULT_FEE_PCT = 0.07

# A render bound, applied in the route AFTER every filter — never inside a
# reducer. ``reduce_sar_signals`` truncated first and every filter then ran on
# the newest 300 rows of a 2,000-row ledger (#97); the page says when it bit.
PER_TRADE_LIMIT = 500

# Sortable columns on the per-trade table, as an allow-list: an arbitrary key
# from the query string is a way to sort on something the page never showed.
SORT_KEYS = ("closed_at", "symbol", "setup", "pnl_pct", "net_usd")


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


def _open_time(rec: dict) -> Optional[datetime]:
    """When the position was taken — the mirror of ``_close_time``.

    Needed only by the concurrency sweep: two trades overlap when one opens
    before the other closes, and without an entry stamp there is no honest way
    to say whether they did. ``dispatch_timestamp`` is the fill;
    ``create_timestamp`` is the signal being written, which is the same moment
    in practice and the only one older rows are guaranteed to carry.
    """
    return (
        _parse_ts(rec.get("dispatch_timestamp"))
        or _parse_ts(rec.get("create_timestamp"))
    )


def _f(value: Any) -> Optional[float]:
    """Float or None — never 0.0 as a stand-in for "could not read this"."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def net_pnl_pct(row: dict, fee_pct: float) -> Optional[float]:
    """The recorded move minus the round-trip cost of taking it.

    ``fee_pct`` is both legs together as a percentage of notional. It is charged
    against the **entry** notional for both: charging the exit leg on
    ``notional * (1 + pnl)`` is a second-order correction worth well under
    0.0001% against a 0.07% fee, and pretending to that precision would imply a
    fill model this page does not have.

    None when the row carries no readable ``pnl_pct`` — a missing move is "we
    cannot say", not a flat trade, and it stays out of every money figure.
    """
    gross = _f(row.get("pnl_pct"))
    return None if gross is None else gross - fee_pct


def money(pct: Optional[float], amount: float) -> Optional[float]:
    """A percentage of the entered notional, in dollars.

    The engine sizes at a fixed notional (``raw_qty = notional / entry_price``),
    so a percentage move on a fixed size is exactly linear in the amount — which
    is what makes this multiplication honest rather than a model.
    """
    return None if pct is None else amount * pct / 100.0


def geometry_stamp_reason(rec: dict) -> Optional[str]:
    """Whether the engine stamped this record's entry→SL distance, and why not.

    **Nothing on this page divides by it.** R was removed on 2026-08-03 and PnL
    needs no denominator, so this affects no figure the owner reads here. It is
    kept because ``no_geometry`` is this repo's only detector for a live
    producer-side fault, and dropping it silently would lose that signal:

    * ``awaiting_engine_stamp`` — closed before the engine carried
      ``sl_distance_pct_at_entry`` (2026-08-01). Unrecoverable, and it shrinks
      on its own.
    * ``no_geometry`` — stamped by the current engine and still unusable, i.e.
      an evaluator that recorded no entry→SL distance. That does *not* age out;
      if this count is non-zero and not falling, something upstream is failing.

    Pooling them would report a live fault that is not happening.
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


def bucket_span(key: str, granularity: str) -> Optional[tuple[datetime, datetime]]:
    """The calendar extent a bucket key **claims**, `[start, end)` in UTC.

    The inverse of ``_bucket_key``, and the thing that makes "is this bucket
    complete" answerable: a bucket labelled ``2026-08-02`` claims a whole day, and
    whether it *measured* one depends on where the window and the current moment
    fall inside this span.

    Returns None on a key this function cannot parse, so a caller degrades to
    "cannot say" rather than asserting completeness it did not check.
    """
    try:
        if granularity == "month":
            first = datetime.strptime(key, "%Y-%m").replace(tzinfo=timezone.utc)
            # Month lengths vary; step into the next month via day 28 + 4 days.
            nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
            return first, nxt
        if granularity == "week":
            year, week = key.split("-W")
            first = datetime.fromisocalendar(int(year), int(week), 1).replace(
                tzinfo=timezone.utc
            )
            return first, first + timedelta(days=7)
        first = datetime.strptime(key, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return first, first + timedelta(days=1)
    except (TypeError, ValueError):
        return None


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

    Rows carry their own open and close timestamps so every downstream consumer
    reads one definition of both rather than re-deriving them. A row with no
    usable close time keeps its place here — it is counted in the page's
    "undateable" figure rather than silently dropped — and lands in no bucket.
    """
    out: list[dict] = []
    rows = records if isinstance(records, list) else []
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        closed = _close_time(rec)
        opened = _open_time(rec)
        out.append({
            "signal_id": str(rec.get("signal_id", "")),
            "symbol": str(rec.get("symbol", "")),
            "direction": str(rec.get("direction", "")).upper(),
            "setup": str(rec.get("setup_class") or "UNKNOWN"),
            "regime": _regime(rec),
            # How the pair entered the scan set, and WHERE IN ITS HOLD this
            # signal fired. Promoted movers are the majority of the delivered
            # book and nothing on this export said so; `promotion_age_sec` is
            # the half that separates "we caught the ignition" from "we arrived
            # in hour five of a finished move".
            #
            # Three states, never two. `None` = the engine that closed this row
            # predates the stamp (2026-08-13) and there is no backfill — the
            # promotion expires long before the signal closes. `-1.0` = the pair
            # was not a held mover at all. `0.0` and up = a real reading. A
            # spreadsheet is exactly where the first two would get averaged into
            # the third.
            "pair_admission": str(rec.get("pair_admission") or ""),
            "promotion_age_sec": rec.get("promotion_age_sec"),
            "entry": rec.get("entry"),
            "stop_loss": rec.get("stop_loss"),
            "pnl_pct": rec.get("pnl_pct"),
            "geometry_stamp_reason": geometry_stamp_reason(rec),
            "outcome": str(rec.get("outcome_label") or ""),
            "hit_sl": bool(rec.get("hit_sl")),
            "hit_tp": int(rec.get("hit_tp") or 0),
            "opened_at": opened,
            "opened_at_ts": opened.timestamp() if opened else None,
            "closed_at": closed,
            "closed_at_ts": closed.timestamp() if closed else None,
            "closed_iso": closed.isoformat() if closed else "",
        })
    out.sort(key=lambda r: r.get("closed_at_ts") or 0.0, reverse=True)
    return out


def resolve_range(
    window: str, date_from: str, date_to: str, *, now: Optional[datetime] = None
) -> tuple[Optional[datetime], Optional[datetime], str]:
    """Turn the window/date inputs into a concrete (start, end, label).

    **Presets snap to whole UTC days** (owner, 2026-08-03). They used to start at
    ``now - N days`` — a *moment* — while ``bucket_rows`` groups by calendar day,
    so the oldest bucket of every preset held only the part of that day after the
    cut and rendered identically to a complete one. On the owner's own export the
    ``1d`` view showed ``2026-08-02`` as **+12.29% over 3 trades, 3W/0L**; the
    full day was **−0.40% over 11, 4W/7L**. The window had removed 8 trades, 7 of
    them losers, and *the sign flipped*. Flooring the start to midnight makes a
    day on this page mean the whole day, whatever window contains it.

    The bucket count does not change — ``30d`` still renders 31 rows — only the
    oldest one goes from a fragment to a whole day.

    ``custom`` already floors to midnight (``_parse_iso_date``) with an inclusive
    end date, so it needs nothing.

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
    start = _floor_day(now - timedelta(days=days))
    # The label says what was actually selected. "last 30d" is the exact phrase
    # that hid the fragment, so it does not survive the fix.
    whole = "day" if days == 1 else "days"
    return start, None, f"last {days} whole {whole} + today — from {start:%Y-%m-%d} UTC"


def _floor_day(when: datetime) -> datetime:
    """Midnight UTC of the day ``when`` falls in."""
    return when.replace(hour=0, minute=0, second=0, microsecond=0)


def local_day_boundary_note(tz_name: str, *, now: Optional[datetime] = None) -> str:
    """Where a UTC day boundary lands in the owner's own time, e.g. "05:30 IST".

    **This re-buckets nothing.** Every figure on this page is UTC, because the
    engine is UTC end to end and ops ports the engine's clock rather than
    inventing one — a local-day boundary here would make an ops day stop matching
    an engine day. The note exists because a date with no zone is the same class
    of omission as a percentage with no denominator: on 2026-08-03 a trade that
    closed 19:48 UTC (01:18 IST the next morning) read as "yesterday" on screen,
    and the page never said which day it meant.

    Computed at ``now`` rather than hardcoded so a DST zone stays correct. An
    unknown or empty zone yields "" and the template simply says UTC — a wrong
    offset is worse than no offset.

    The zone arrives as a parameter rather than being read from config in here,
    so this stays a pure function the tests can drive without an app.
    """
    if not tz_name:
        return ""
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(tz_name)
    except Exception:
        return ""
    now = now or datetime.now(tz=timezone.utc)
    midnight = _floor_day(now) + timedelta(days=1)
    local = midnight.astimezone(zone)
    return f"{local:%H:%M} {local:%Z}".strip()


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


def peak_concurrency(rows: list[dict]) -> dict:
    """How many of these trades were open at once, at the worst moment.

    A sweep line over (open, +1) and (close, −1) events. This is what answers
    the owner's "can he maintain it in the account": at a fixed notional the
    balance a book demands is ``peak * amount``, because that is the most it
    ever had committed at one time.

    Closes are processed before opens at an identical timestamp — a position
    that closed on the same second another opened did not need funding twice.

    A row that cannot supply a window is **counted and named** rather than
    assumed simultaneous (which would inflate the requirement) or assumed
    sequential (which would understate it) — and the two causes are counted
    apart, because their fixes differ:

    * ``undated`` — no entry or no close stamp. Older records simply predate the
      field; nothing is wrong upstream.
    * ``bad_window`` — both stamps present and the close is *before* the open.
      That is a producer fault, and pooling it into ``undated`` would report
      missing data where the data is present and wrong.
    """
    events: list[tuple[float, int]] = []
    undated = 0
    bad_window = 0
    for row in rows:
        start = row.get("opened_at_ts")
        end = row.get("closed_at_ts")
        if start is None or end is None:
            undated += 1
            continue
        if end < start:
            bad_window += 1
            continue
        events.append((float(start), 1))
        events.append((float(end), -1))
    # -1 sorts before +1 at an equal timestamp.
    events.sort(key=lambda e: (e[0], e[1]))
    live = 0
    peak = 0
    peak_ts: Optional[float] = None
    for when, delta in events:
        live += delta
        if live > peak:
            peak = live
            peak_ts = when
    return {
        "peak": peak,
        "peak_at": datetime.fromtimestamp(peak_ts, tz=timezone.utc) if peak_ts else None,
        "measured": len(rows) - undated - bad_window,
        "undated": undated,
        "bad_window": bad_window,
    }


def summarize(
    rows: list[dict],
    *,
    amount: float = DEFAULT_AMOUNT_USDT,
    fee_pct: float = DEFAULT_FEE_PCT,
) -> dict:
    """Headline over whatever the filters selected, in money.

    Two denominators, kept apart on purpose:

    * ``n`` — every selected trade.
    * ``n_pnl`` — those carrying a readable ``pnl_pct``. Every figure below
      divides by THIS, and the page says so when the two differ.

    They are normally identical, which is the whole point of the 2026-08-03
    change: R divided by a stamp 421 of 448 rows did not carry, so it described
    6% of the book while looking like the book. A percentage needs no
    denominator, so it cannot shrink its own population that way.

    The geometry-stamp counts ride along for the engine-health line. They gate
    nothing here.
    """
    awaiting = sum(1 for r in rows if r.get("geometry_stamp_reason") == "awaiting_engine_stamp")
    no_geometry = sum(1 for r in rows if r.get("geometry_stamp_reason") == "no_geometry")

    gross = [p for p in (_f(r.get("pnl_pct")) for r in rows) if p is not None]
    net = [p - fee_pct for p in gross]
    # The win rate counts on the MONEY — net of the cost of taking the trade,
    # because a trade that made less than its fee did not make money.
    wins = sum(1 for p in net if p > 0)
    moves = distinct_moves(rows)
    return {
        "n": len(rows),
        "moves": moves,
        "rows_per_move": (len(rows) / moves) if moves else None,
        "n_pnl": len(gross),
        "no_pnl": len(rows) - len(gross),
        "wins": wins,
        "losses": len(net) - wins,
        "win_rate": (wins / len(net)) if net else None,
        # The money, at the entered size. Primary — it is what the owner reads.
        "gross_usd": money(sum(gross), amount) if gross else None,
        "fee_usd": (amount * fee_pct / 100.0 * len(gross)) if gross else None,
        "net_usd": money(sum(net), amount) if net else None,
        # The same book as percentages, size-independent.
        "total_pnl_pct": sum(gross) if gross else None,
        "avg_pnl_pct": (sum(gross) / len(gross)) if gross else None,
        "total_net_pct": sum(net) if net else None,
        "avg_net_pct": (sum(net) / len(net)) if net else None,
        "best_pnl_pct": max(gross) if gross else None,
        "worst_pnl_pct": min(gross) if gross else None,
        # Engine-data health only. Named apart because the fixes differ: one
        # ages out on its own, the other is a producer-side fault that does not.
        "geometry_awaiting_stamp": awaiting,
        "geometry_no_geometry": no_geometry,
    }


def bucket_completeness(
    key: str,
    granularity: str,
    *,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Did this bucket measure the whole period its label claims?

    A bucket labelled ``2026-08-02`` claims a day. Two different things can leave
    it holding less than one, and **they are never pooled**, because the reader's
    next move differs:

    * ``window_cut`` — the selected range starts after (or ends before) the
      bucket's own span. The data exists; **widening the window reveals it.**
    * ``in_progress`` — the period has not finished yet. The data does not exist;
      **only waiting reveals it.**

    Precedence when both apply: ``window_cut``, because it is the one the reader
    can act on now.

    This is the guard for the fault that prompted it (2026-08-03): a rolling
    window cut 8 of 11 trades off a day and the table rendered the remaining 3 —
    all winners — as that day's result, flipping its sign. ``resolve_range`` now
    snaps presets to midnight so no *day* bucket can be cut, but snapping days is
    not snapping weeks or months: a 7d window at week granularity still starts
    mid-ISO-week. It also catches a future regression in ``resolve_range``.

    An unparseable key yields ``partial_reason=None`` with no span — "cannot say"
    rather than an unchecked claim of completeness.
    """
    span = bucket_span(key, granularity)
    if span is None:
        return {
            "partial_reason": None, "covers_from": None, "covers_to": None,
            "covers_from_iso": "", "span_fraction": None,
        }
    span_start, span_end = span
    cut = (start is not None and start > span_start) or (
        end is not None and end < span_end
    )
    running = now is not None and span_start <= now < span_end

    covers_from = max(span_start, start) if start is not None else span_start
    covers_to = min(span_end, end) if end is not None else span_end
    if now is not None and now < covers_to:
        covers_to = now
    measured = (covers_to - covers_from).total_seconds()
    claimed = (span_end - span_start).total_seconds()
    return {
        "partial_reason": "window_cut" if cut else ("in_progress" if running else None),
        "covers_from": covers_from,
        "covers_to": covers_to,
        "covers_from_iso": covers_from.isoformat(),
        "span_fraction": max(0.0, measured / claimed) if claimed else None,
    }


def bucket_rows(
    rows: list[dict],
    granularity: str,
    *,
    amount: float = DEFAULT_AMOUNT_USDT,
    fee_pct: float = DEFAULT_FEE_PCT,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Group into day / week / month buckets, newest first.

    Each bucket carries the same summary shape as the headline, so a bucket and
    the total are never computed two different ways.

    ``cum_net_usd`` is accumulated oldest-first and then the list is reversed,
    so the newest row carries the whole book's total rather than one day's.

    ``start`` / ``end`` / ``now`` are only used to mark **completeness** — they
    filter nothing, because filtering already happened. Pass them and a bucket
    that measured less than the period its label claims says so; omit them and
    every bucket reads complete, which is exactly the fault this guards.
    """
    if granularity not in GRANULARITIES:
        granularity = "day"
    now = now or datetime.now(tz=timezone.utc)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        when = row.get("closed_at")
        if when is None:
            continue
        grouped[_bucket_key(when, granularity)].append(row)
    out = []
    running = 0.0
    for key in sorted(grouped):
        bucket = summarize(grouped[key], amount=amount, fee_pct=fee_pct)
        bucket["bucket"] = key
        bucket.update(
            bucket_completeness(key, granularity, start=start, end=end, now=now)
        )
        if bucket["net_usd"] is not None:
            running += bucket["net_usd"]
            bucket["cum_net_usd"] = running
        else:
            # No readable move in the bucket: the curve did not move, and it is
            # not a gap in the curve either. Carry the level forward.
            bucket["cum_net_usd"] = running if out else None
        out.append(bucket)
    out.reverse()
    return out


def decorate_money(rows: list[dict], *, amount: float, fee_pct: float) -> list[dict]:
    """Stamp each row with its own money, so the table and the headline agree.

    Mutates in place and returns the same list — these rows are built fresh by
    ``reduce_records`` on every request, so there is no shared state to corrupt.
    """
    for row in rows:
        net = net_pnl_pct(row, fee_pct)
        row["net_pct"] = net
        row["gross_usd"] = money(_f(row.get("pnl_pct")), amount)
        row["fee_usd"] = None if net is None else amount * fee_pct / 100.0
        row["net_usd"] = money(net, amount)
    return rows


def sort_rows(rows: list[dict], sort: str, order: str) -> list[dict]:
    """Order the per-trade table. Unknown keys fall back to newest-closed-first.

    Rows whose sort value is missing sort last in either direction — a blank is
    not a low number, and floating it to the top of an ascending sort would make
    "worst trades" mean "trades we could not read".
    """
    if sort not in SORT_KEYS:
        sort, order = "closed_at", "desc"
    reverse = order != "asc"
    numeric = sort in ("pnl_pct", "net_usd", "closed_at")

    def key(row: dict) -> tuple[int, Any]:
        if sort == "closed_at":
            value = row.get("closed_at_ts")
        elif numeric:
            value = _f(row.get(sort))
        else:
            value = str(row.get(sort) or "")
        if value is None or value == "":
            # 1 sorts after 0 ascending; negated under reverse so it stays last.
            return (-1 if reverse else 1, 0 if numeric else "")
        return (0, value)

    return sorted(rows, key=key, reverse=reverse)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

_EXPORT_COLS = [
    # ``partial_reason`` / ``covers_from`` ride with the numbers rather than
    # staying on screen: an export that drops the caveat is the same fault one
    # file over, and a spreadsheet is exactly where a part-day gets averaged in.
    "bucket", "partial_reason", "covers_from_iso",
    "n", "moves", "n_pnl", "wins", "losses", "win_rate",
    "total_pnl_pct", "avg_pnl_pct", "best_pnl_pct", "worst_pnl_pct",
    "total_net_pct", "gross_usd", "fee_usd", "net_usd", "cum_net_usd",
]

_TRADE_COLS = [
    # ``signal_id`` leads because it is the JOIN KEY, and its absence turned a
    # one-line check into a session. Every measurement lane in the engine stamps
    # its rows by `signal_id` — the SAR arms, the dark feed, the entry-feature
    # lane — and this export, the only place the *delivered outcome* leaves ops,
    # dropped it. So the 2026-08-08 audit asking "what fraction of the delivered
    # book did the SAR lane actually arm?" had to join on
    # ``(symbol, direction, entry)`` rounded to six significant figures: 366 rows
    # collapsed to 365 distinct keys, so at least one collision, and the coverage
    # answer moved between 81.6% and 90.8% purely with the matching tolerance.
    # The finding survived that (the unarmed slice ran −1.643%/trade at every
    # tolerance) but it should never have rested on a price match. The row has
    # carried the field all along; only the column list did not.
    "signal_id",
    "closed_iso", "symbol", "direction", "setup", "regime", "outcome",
    "pair_admission", "promotion_age_sec",
    "entry", "pnl_pct", "net_pct", "gross_usd", "fee_usd", "net_usd",
]


def _positive_float(raw: str, default: float) -> float:
    """A user-entered number, or the default. Never raises, never negative.

    A bad amount must not 500 and must not silently become zero either — zero is
    a legitimate fee (the gross book) but a meaningless position size, so an
    unreadable value falls back to the default and the field echoes what was
    typed.
    """
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return max(0.0, value)


def _optional_float(raw: str) -> Optional[float]:
    """A number the owner may not have entered at all.

    Blank means "not asked", and so does a typo — falling back to 0.0 would
    render the underfunded warning against a balance the owner never claimed to
    have, which is a fault report about the reader rather than the book.
    """
    if not str(raw).strip():
        return None
    sentinel = -1.0
    value = _positive_float(raw, sentinel)
    return None if value == sentinel else value


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
    amount: str = Query(""),
    fee_pct: str = Query(""),
    balance: str = Query(""),
    sort: str = Query(""),
    order: str = Query(""),
):
    """Delivered signals, bucketed and priced — the record a subscriber reads.

    Deliberately NOT the Profit tab: nothing here is replayed or reconstructed.
    Every number is what the engine recorded as the trade happened, multiplied
    by a position size the owner typed.
    """
    if window not in WINDOWS:
        window = "30d"
    if granularity not in GRANULARITIES:
        granularity = "day"
    amount_usdt = _positive_float(amount, DEFAULT_AMOUNT_USDT)
    fee = _positive_float(fee_pct, DEFAULT_FEE_PCT)
    balance_usdt = _optional_float(balance)
    q = dict(
        window=window, date_from=date_from, date_to=date_to,
        regime=regime, setup=setup, symbol=symbol, direction=direction,
    )
    ctx = _page_context(request, **q)
    rows, all_rows, scoped = ctx["rows"], ctx["all_rows"], ctx["scoped"]
    decorate_money(rows, amount=amount_usdt, fee_pct=fee)
    summary = summarize(rows, amount=amount_usdt, fee_pct=fee)
    concurrency = peak_concurrency(rows)
    required_usdt = concurrency["peak"] * amount_usdt
    # The row cap is a RENDER bound: applied here, after every filter, never
    # inside a reducer (#97). The page says when it bit.
    ordered = sort_rows(rows, sort, order)

    return request.app.state.templates.TemplateResponse("track_record.html", {
        "request": request,
        "active": "track_record",
        "buckets": bucket_rows(
            rows, granularity, amount=amount_usdt, fee_pct=fee,
            start=ctx["start"], end=ctx["end"],
        ),
        "day_boundary_note": local_day_boundary_note(
            getattr(request.app.state.settings, "local_tz_hint", "")
        ),
        "summary": summary,
        "trades": ordered[:PER_TRADE_LIMIT],
        "trades_shown": min(len(ordered), PER_TRADE_LIMIT),
        "trades_capped": len(ordered) > PER_TRADE_LIMIT,
        "per_trade_limit": PER_TRADE_LIMIT,
        "sort": sort if sort in SORT_KEYS else "closed_at",
        "order": "asc" if order == "asc" else "desc",
        "amount": amount_usdt,
        "fee_pct": fee,
        "balance": balance_usdt,
        "concurrency": concurrency,
        "required_usdt": required_usdt,
        "return_on_balance": (
            (summary["net_usd"] / balance_usdt * 100.0)
            if balance_usdt and summary["net_usd"] is not None
            else None
        ),
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
    amount: str = Query(""),
    fee_pct: str = Query(""),
):
    """The buckets as CSV, honouring the current filter, amount and fee."""
    if granularity not in GRANULARITIES:
        granularity = "day"
    amount_usdt = _positive_float(amount, DEFAULT_AMOUNT_USDT)
    fee = _positive_float(fee_pct, DEFAULT_FEE_PCT)
    q = dict(
        window=window if window in WINDOWS else "30d",
        date_from=date_from, date_to=date_to,
        regime=regime, setup=setup, symbol=symbol, direction=direction,
    )
    ctx = _page_context(request, **q)
    buckets = bucket_rows(
        ctx["rows"], granularity, amount=amount_usdt, fee_pct=fee,
        start=ctx["start"], end=ctx["end"],
    )
    data = [[b.get(col) for col in _EXPORT_COLS] for b in buckets]
    return csv_response(f"track_record_{granularity}", _EXPORT_COLS, data)


@router.get("/track-record/trades.csv")
async def track_record_trades_export(
    request: Request,
    window: str = Query("30d"),
    date_from: str = Query(""),
    date_to: str = Query(""),
    regime: str = Query(""),
    setup: str = Query(""),
    symbol: str = Query(""),
    direction: str = Query(""),
    amount: str = Query(""),
    fee_pct: str = Query(""),
    sort: str = Query(""),
    order: str = Query(""),
):
    """Every selected trade, one row each — the book behind the buckets.

    Deliberately **not** capped at ``PER_TRADE_LIMIT``: that bound exists so a
    browser does not render 2,000 rows, and a file the owner opens in a
    spreadsheet has no such problem. A truncated export would be the #97 fault
    wearing a download button.
    """
    amount_usdt = _positive_float(amount, DEFAULT_AMOUNT_USDT)
    fee = _positive_float(fee_pct, DEFAULT_FEE_PCT)
    q = dict(
        window=window if window in WINDOWS else "30d",
        date_from=date_from, date_to=date_to,
        regime=regime, setup=setup, symbol=symbol, direction=direction,
    )
    ctx = _page_context(request, **q)
    rows = decorate_money(ctx["rows"], amount=amount_usdt, fee_pct=fee)
    data = [
        [r.get(col) for col in _TRADE_COLS]
        for r in sort_rows(rows, sort, order)
    ]
    return csv_response("track_record_trades", _TRADE_COLS, data)

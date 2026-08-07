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

    # Three buckets, never two. An EXPIRED row is scored 0.0% by the engine —
    # a walked window in which neither level was touched — so it is *flat*, not
    # a loss. Folding it into the loss count is how 80 trades that lost nothing
    # got reported as losing trades, and it moves the win rate by 5pp
    # (115W/462 = 25%, 115W/382 decided = 30%). The rate is published on both
    # denominators because both are defensible and they describe genuinely
    # different populations; neither is called "the" win rate.
    wins = sum(1 for r in closed if (_f(r.get("pnl_pct")) or 0.0) > 0)
    losses = sum(1 for r in closed if (_f(r.get("pnl_pct")) or 0.0) < 0)
    flats = len(closed) - wins - losses
    decided = wins + losses
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
        "losses": losses,
        "flats": flats,
        "n_decided": decided,
        "win_rate": (wins / len(closed) * 100.0) if closed else None,
        "win_rate_decided": (wins / decided * 100.0) if decided else None,
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


#: Three times the engine's 30-minute per-symbol emit throttle. Rows on one
#: symbol and one side closer together than this are re-entries into the same
#: directional run, not independent reads of the market.
EPISODE_GAP_S = 5_400.0


def episodes(rows: list[dict], *, fee_pct: float) -> dict:
    """Concentration the move-dedup key above **cannot** see, by construction.

    `concentration()` keys on ``symbol · side · entry`` to catch the same sweep
    re-stamped at the same price. That is the right key for its own question and
    it is the wrong one for this page's verdict, because **a trending symbol
    hands out a different entry every time**. On the 2026-08-07 book it read
    1.12 rows/move and "largest single move = 1.0% of all rows" — which a reader
    takes as *concentration is not a problem here*.

    It was. BEATUSDT whipsawed across a 24% range (1.975–2.450) and the lane
    bought reclaimed support ten times through it, every 30 minutes, exactly as
    throttled — ten different entries, so ten distinct "moves", **none of which
    won**. The worst nine of them form one run contributing **−85.71%** against
    a whole-book net of −78.25%. One symbol, one side, 4.5 hours: remove that
    run and the book reads **+7.46%**. The verdict's *sign* was one episode.

    Worth reading beside the regime split further down the page: **eight of
    those ten longs were stamped `TRENDING_UP`** (mean −10.19%). The program
    doc's diagnosis is that this lane has no context layer — true, but a context
    layer keyed on the regime label as stamped would have *confirmed* these
    entries, not filtered them. That is a fact about the labels on this run, not
    a verdict on the detector; it is here so the reader checks it before
    treating "add a context layer" as the fix this page recommends. It
    recommends nothing.

    This is #816 ("a throttle on rate is not a throttle on evidence") arriving
    at the display side, and this repo's own rule — *disclose concentration,
    don't silently average it*. Nothing here de-duplicates: episodes are counted
    and named beside the row count, because de-duplicating is a judgement call
    and the reader makes it, not us.
    """
    empty = {
        "n_rows": len(rows), "n_episodes": 0, "rows_per_episode": 0.0,
        "worst": None, "book_net": None, "multi_row_share": 0.0,
    }
    if not rows:
        return empty

    runs: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        runs.setdefault((str(r.get("symbol") or "?"), str(r.get("side") or "?")), []).append(r)

    grouped: list[tuple[str, str, list[dict]]] = []
    for (sym, side), v in runs.items():
        v = sorted(v, key=lambda r: _f(r.get("emitted_at")) or 0.0)
        run = [v[0]]
        for prev, cur in zip(v, v[1:]):
            a, b = _f(prev.get("emitted_at")), _f(cur.get("emitted_at"))
            if a is not None and b is not None and (b - a) < EPISODE_GAP_S:
                run.append(cur)
            else:
                grouped.append((sym, side, run))
                run = [cur]
        grouped.append((sym, side, run))

    if not grouped:
        return empty

    def _net(rs: list[dict]) -> float | None:
        c = [x for x in rs if _f(x.get("pnl_pct")) is not None]
        if not c:
            return None
        return sum(_f(x.get("pnl_pct")) or 0.0 for x in c) - fee_pct * len(c)

    book = _net(rows)
    scored = [(s, d, g, _net(g)) for s, d, g in grouped]
    losing = [x for x in scored if x[3] is not None]
    worst = min(losing, key=lambda x: x[3]) if losing else None

    worst_out = None
    if worst is not None:
        sym, side, g, n = worst
        stamps = [_f(r.get("emitted_at")) for r in g]
        stamps = [s for s in stamps if s is not None]
        hours = (max(stamps) - min(stamps)) / 3600.0 if len(stamps) >= 2 else 0.0
        worst_out = {
            "label": f"{sym} {side}", "n_rows": len(g), "hours": hours, "net_pct": n,
            # Only meaningful while the book is negative: "this one run accounts
            # for N% of everything the lane lost". A share over 100% means the
            # rest of the book is net positive without it.
            "share_of_book": (n / book * 100.0) if (book is not None and book < 0) else None,
            # Exactly this episode's rows, not the whole symbol: the reader is
            # being told what one directional run cost, so removing unrelated
            # rows that happen to share a ticker would overstate it.
            "book_without": _net([r for r in rows if id(r) not in {id(x) for x in g}]),
        }

    multi = sum(len(g) for _, _, g in grouped if len(g) > 1)
    return {
        "n_rows": len(rows),
        "n_episodes": len(grouped),
        "rows_per_episode": len(rows) / len(grouped),
        "worst": worst_out,
        "book_net": book,
        "multi_row_share": multi / len(rows),
        "gap_minutes": EPISODE_GAP_S / 60.0,
    }


def by_context(rows: list[dict], *, fee_pct: float, key: str) -> list[dict]:
    """Split by layer 1 — Context, the layer the lane was built without.

    §1 of the program doc defines price action as a **four-layer** read; the
    lane shipped with layers 2 (LevelBook), 3 (sweep + reclaim) and 4 (footprint
    delta) and no layer 1, while `volume_profile.py` had computed POC and the
    value area all along and the lane never imported it.

    Why this split and not another: a sweep + reclaim is a **failed break**, so
    it is a mean-reversion trade. It pays in **balance** (price rotating inside
    a value area, rejected at the edge, returning toward POC) and traps in
    **imbalance** (value being accepted away from the area, so each failed break
    is a pause before continuation). Those two states have an identical
    layer-2/3/4 signature, which is exactly why nothing already stamped could
    separate them and why every column on this page looked like noise.

    `unstamped` is its own bucket and is never folded into a real one — the
    engine refuses rather than guessing when a profile cannot be built, and a
    missing stamp is not a pass.
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
            "zone": name,
            "n": len(bucket),
            "n_closed": len(closed),
            "win_rate": (wins / len(closed) * 100.0) if closed else None,
            "avg_net_pct": (
                (gross - fee_pct * len(closed)) / len(closed) if closed else None
            ),
            "unstamped": name == "unstamped",
        })
    return out


def balance_shadow(rows: list[dict], *, fee_pct: float) -> dict:
    """The what-if: keep only sweeps taken **in balance, with room to POC**.

    Applied to **nothing**. This is a display-side counterfactual over rows the
    engine already stamped and already emitted, and it changes no emission.

    **Its cutoff is honest about where it comes from, and half of it is fitted.**
    The *balance* half is not: "inside the value area" is the value area's own
    70%-of-volume definition, which existed before this lane and was not chosen
    to fit any window. The *rotation* half — requiring POC to sit ahead of the
    trade — is a reasoned condition from auction theory, but **which** of the
    several defensible layer-1 conditions to combine was picked while looking at
    this book. So the number below is a hypothesis this window generated, not
    one it tested, and it has to be re-earned on rows stamped after it shipped.
    The owner was told this and asked for it anyway; the page says it too,
    because the reader after him was not in that conversation.

    Three buckets, never two: folding rows whose layer 1 never computed into
    `keep` is how a rule takes credit for rows it never filtered.
    """
    keep, drop, unknown = [], [], []
    for r in rows:
        zone = str(r.get("vp_entry_zone") or "")
        room = _f(r.get("vp_poc_room_pct"))
        if not zone or room is None:
            unknown.append(r)
        elif zone == "in" and room > 0:
            keep.append(r)
        else:
            drop.append(r)

    def _m(rs: list[dict]) -> dict:
        closed = [x for x in rs if _f(x.get("pnl_pct")) is not None]
        if not closed:
            return {"n": len(rs), "n_closed": 0, "win_rate": None, "avg_net_pct": None}
        wins = sum(1 for x in closed if (_f(x.get("pnl_pct")) or 0.0) > 0)
        gross = sum(_f(x.get("pnl_pct")) or 0.0 for x in closed)
        return {
            "n": len(rs),
            "n_closed": len(closed),
            "win_rate": wins / len(closed) * 100.0,
            "avg_net_pct": (gross - fee_pct * len(closed)) / len(closed),
        }

    base = _m(rows)
    kept = _m(keep)
    # The baseline is measured on the WHOLE book the page is showing, and the
    # delta against it is only meaningful where the rule actually had an opinion
    # — so the tested population is named beside the kept fraction, and a rule
    # that abstained on most of the book has not been tested whatever its delta.
    decided = _m(keep + drop)
    delta = (
        None if kept["avg_net_pct"] is None or base["avg_net_pct"] is None
        else kept["avg_net_pct"] - base["avg_net_pct"]
    )
    return {
        "base": base, "keep": kept, "drop": _m(drop), "unknown": _m(unknown),
        "decided": decided,
        "delta_vs_base": delta,
        "kept_frac": (len(keep) / len(rows)) if rows else None,
        "unknown_frac": (len(unknown) / len(rows)) if rows else None,
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


#: Which stamps a row carries. FOUR states, not two — the level provenance
#: (#891) and layer 1 (#892) shipped about an hour apart, so rows written
#: between them carry one and not the other. Folding that middle population into
#: either end would misdescribe it, and it is exactly the rows a reader is most
#: likely to wonder about.
STAMP_STATES: list[tuple[str, str]] = [
    ("all", "Everything in the ledger"),
    ("full", "Carries BOTH the level source and the regime — the clean population"),
    ("partial", "Carries one stamp and not the other (written between the two fixes)"),
    ("none", "Carries neither — written before either fix"),
]


def _has_level_stamp(row: dict) -> bool:
    return bool(str(row.get("level_source_tf") or ""))


def _has_regime_stamp(row: dict) -> bool:
    return bool(str(row.get("regime_15m") or "") or str(row.get("regime") or ""))


def stamp_state(row: dict) -> str:
    """`full` / `partial` / `none` for one row."""
    n = _has_level_stamp(row) + _has_regime_stamp(row)
    return ("none", "partial", "full")[n]


def filter_lane_rows(
    rows: list[dict],
    *,
    stamped: str = "",
    regime: str = "",
    level: str = "",
    status: str = "",
) -> list[dict]:
    """Each selector applied independently, so a caller can omit one when
    counting that selector's own options (#90/#91). A selector applied to its
    own counts makes every option describe only itself.
    """
    out = rows
    if stamped and stamped != "all":
        out = [r for r in out if stamp_state(r) == stamped]
    if regime:
        out = [r for r in out if str(r.get("regime_15m") or "unstamped") == regime]
    if level:
        out = [r for r in out if str(r.get("level_source_tf") or "unstamped") == level]
    if status:
        out = [r for r in out if str(r.get("status") or "") == status]
    return out


def selector_options(
    rows: list[dict], *, stamped: str, regime: str, level: str, status: str,
) -> dict:
    """Every option's count, measured with every OTHER filter applied.

    #90/#91: a count computed with its own selector applied makes each option
    describe only itself — every row would read "n = whatever I picked".
    """
    def _count(key, getter, **skip):
        base = filter_lane_rows(
            rows,
            stamped="" if key == "stamped" else stamped,
            regime="" if key == "regime" else regime,
            level="" if key == "level" else level,
            status="" if key == "status" else status,
        )
        counts: dict[str, int] = {}
        for r in base:
            counts[getter(r)] = counts.get(getter(r), 0) + 1
        return counts

    return {
        "stamped": _count("stamped", stamp_state),
        "regime": _count(
            "regime", lambda r: str(r.get("regime_15m") or "unstamped")),
        "level": _count(
            "level", lambda r: str(r.get("level_source_tf") or "unstamped")),
        "status": _count("status", lambda r: str(r.get("status") or "")),
    }


#: Export columns. Everything a row carries that a reader could want offline,
#: in a stable order — the CSV is a surface and inherits the page's rules, so
#: gross and net both appear and nothing here divides by a stop.
EXPORT_COLS: list[str] = [
    "signal_id", "symbol", "side", "setup_class", "emitted_at", "closed_at",
    "status", "entry", "stop_loss", "tp1", "exit_price",
    "pnl_pct", "mfe_pct", "mae_pct",
    "stamp_state",
    "regime_15m", "regime", "regime_tf",
    "level_source_tf", "level_type", "level_price", "level_score",
    "sweep_extreme", "sweep_depth_pct", "delta_quote", "rr",
    # Layer 1 — Context. The column a spreadsheet needs to separate the two
    # populations the lane could not previously tell apart.
    "vp_entry_zone", "vp_level_zone", "vp_poc_room_pct", "vp_value_width_pct",
    "vp_poc", "vp_vah", "vp_val",
    "bars_seen", "last_resolved_at", "bars_behind", "stalled",
]


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


async def _load_and_mark(request: Request) -> tuple[list[dict], dict, str]:
    """Every lane row, freshness-graded and marked. Shared by page and export so
    the download can never describe a different book than the screen."""
    vol = request.app.state.data_volume
    error = ""
    rows: list[dict] = []
    try:
        rows = reduce_lane_rows(vol.dark_signals())
    except Exception as exc:  # noqa: BLE001
        return [], {}, f"dark ledger unreadable: {type(exc).__name__}: {exc}"

    prices: dict = {}
    if rows:
        try:
            prices = await request.app.state.binance_klines.fetch_all_prices()
        except Exception:  # noqa: BLE001
            prices = {}
    now = time.time()
    mark_freshness(rows, now=now)
    mark_live_pnl(rows, prices)
    for r in rows:
        r["stamp_state"] = stamp_state(r)
    return rows, prices, error


@router.get("/signals/price-action/export.csv")
async def price_action_export(
    request: Request,
    stamped: str = Query(""),
    regime: str = Query(""),
    level: str = Query(""),
    status: str = Query(""),
):
    """The current selection as CSV — **uncapped**, unlike the rendered table.

    A truncated export is #97 wearing a download button: the row cap is a render
    bound and must not follow the data off the page.
    """
    from app.reports import csv_response

    rows, _prices, _err = await _load_and_mark(request)
    rows = filter_lane_rows(
        rows, stamped=stamped, regime=regime, level=level, status=status,
    )
    data = [[r.get(c) for c in EXPORT_COLS] for r in rows]
    return csv_response("price_action_lane", EXPORT_COLS, data)


@router.get("/signals/price-action")
async def price_action_page(
    request: Request,
    fee_pct: float = Query(DEFAULT_FEE_PCT),
    stamped: str = Query(""),
    regime: str = Query(""),
    level: str = Query(""),
    status: str = Query(""),
):
    all_rows, _prices, error = await _load_and_mark(request)
    rows = all_rows

    # Selector option counts BEFORE the filter narrows anything — each option
    # measured with every filter applied except its own (#90/#91).
    options = selector_options(
        all_rows, stamped=stamped, regime=regime, level=level, status=status,
    )
    rows = filter_lane_rows(
        all_rows, stamped=stamped, regime=regime, level=level, status=status,
    )

    fee = max(0.0, min(1.0, float(fee_pct)))
    summary = summarize(rows, fee_pct=fee)
    sources = by_level_source(rows)
    conc = concentration(rows)
    eps = episodes(rows, fee_pct=fee)
    ctx_entry = by_context(rows, fee_pct=fee, key="vp_entry_zone")
    ctx_level = by_context(rows, fee_pct=fee, key="vp_level_zone")
    shadow = balance_shadow(rows, fee_pct=fee)
    regimes_entry = by_regime(rows, fee_pct=fee, key="regime")
    regimes_trigger = by_regime(rows, fee_pct=fee, key="regime_15m")
    regime_tfs = regime_timeframes(rows)
    shown = rows[:TABLE_ROW_CAP]
    capped = len(rows) > TABLE_ROW_CAP

    return request.app.state.templates.TemplateResponse(
        "price_action.html",
        {
            "request": request,
            "active": "price_action",
            "rows": shown,
            "summary": summary,
            "sources": sources,
            "concentration": conc,
            "episodes": eps,
            "ctx_entry": ctx_entry,
            "ctx_level": ctx_level,
            "shadow": shadow,
            "regimes_entry": regimes_entry,
            "regimes_trigger": regimes_trigger,
            "regime_tfs": regime_tfs,
            "error": error,
            "fee_pct": fee,
            "options": options,
            "stamp_states": STAMP_STATES,
            "sel": {"stamped": stamped, "regime": regime,
                    "level": level, "status": status},
            "n_all": len(all_rows),
            "n_shown": len(rows),
            "capped": capped,
            "row_cap": TABLE_ROW_CAP,
            "total_rows": len(rows),
        },
    )

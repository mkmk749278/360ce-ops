"""Live SAR exit mechanism — the arms as they run, not as they are replayed.

This is the owner-facing surface of the engine's ``src/sar_live_shadow.py``,
added 2026-07-30 because the two SAR surfaces that preceded it are both
hindsight and neither can justify enabling a SAR exit for users:

* ``/signals/sar`` renders the ``@SARBASE``/``@SAREXIT`` replay ledger. On the
  owner's 2026-07-30 export 8 of 19 rows were unresolved — including **all
  four** of the window's winners — so its verdict was an artefact of which
  symbols the resolver's refresh budget reached.
* The Performance tab's dark-signals bake-off replays klines *after* the engine
  has already closed the trade. Splitting its rows by whether the trail actually
  exited collapses the apparent edge from +0.375pp to +0.070pp per trade.

Neither had ever computed a stop while a position was open. These arms do:
each one is stepped forward bar by bar inside the monitor loop, so an open row
carries the stop the mechanism *would have parked right now*.

Two tabs, because they answer different questions
-------------------------------------------------
**Live** (default) — arms currently running. Which leg is governing, where the
stop sits, how far price is from it. This is the operability read: it is the
only view in the system that shows the mechanism working rather than the
mechanism scored.

**Resolved** — arms that closed. This is where the verdict lives. You cannot
judge a mechanism from open trades alone, which is why the resolved tab exists
even though the owner asked for a live feed.

Rules this page inherits, all paid for elsewhere in this repo
-------------------------------------------------------------
* **Every count is measured with every filter applied except its own.** A
  selector applied to its own counts makes each option describe only itself
  (#90/#91).
* **Truncate after filtering, never before.** The row cap is a render bound
  applied in the route, and the page says when it bit (#97).
* **Unrealized never sits in a realized column.** Live rows carry marks; the
  realized columns stay blank until the arm actually closes.
* **Ops ports the engine's math, it does not invent it.** Every field here is
  computed engine-side; this module groups and counts, it does not re-derive a
  fill, an R, or a stop.
* **Copy is part of the measurement.** The page states that both fills are
  shown and what the difference means, because a single "SAR result" would be a
  choice this data does not support making yet.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Query, Request

router = APIRouter()

#: Render bound. Applied in the route AFTER filtering — never inside a reducer.
TABLE_ROW_CAP = 400

GOV_SAR = "SAR"
GOV_GEOMETRY = "GEOMETRY"

STATUS_RUNNING = "RUNNING"
STATUS_CLOSED_SAR_FLIP = "CLOSED_SAR_FLIP"
STATUS_CLOSED_SL = "CLOSED_SL"
STATUS_CLOSED_TP1 = "CLOSED_TP1"
STATUS_INSUFFICIENT = "INSUFFICIENT"

RESOLVED_STATUSES = (
    STATUS_CLOSED_SAR_FLIP,
    STATUS_CLOSED_SL,
    STATUS_CLOSED_TP1,
    STATUS_INSUFFICIENT,
)

#: How stale the arm file may be before the live tab stops claiming to be live.
#: Arms advance on 5m/15m bar closes and the engine flushes at most every 15s,
#: so anything past two minutes means the monitor loop is not stepping them —
#: a quiet market cannot cause this.
LIVE_STALE_SEC = 120.0


def _f(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def reduce_arms(payload: Any) -> tuple[list[dict], list[dict]]:
    """Split the engine's ledger into (open, resolved). Pure; no truncation.

    A payload that is missing, unparseable, or of an unexpected shape yields two
    empty lists rather than raising — the page then says the file is unavailable,
    which is a different statement from "no arms", and the template distinguishes
    them.
    """
    if not isinstance(payload, dict):
        return [], []
    open_rows = payload.get("open")
    resolved_rows = payload.get("resolved")
    live = [r for r in open_rows if isinstance(r, dict)] if isinstance(open_rows, list) else []
    done = (
        [r for r in resolved_rows if isinstance(r, dict)]
        if isinstance(resolved_rows, list)
        else []
    )
    live.sort(key=lambda r: _f(r.get("opened_at")) or 0.0, reverse=True)
    done.sort(key=lambda r: _f(r.get("closed_at")) or 0.0, reverse=True)
    return live, done


def filter_arms(
    rows: list[dict],
    *,
    timeframe: str = "",
    governor: str = "",
    alignment: str = "",
    status: str = "",
) -> list[dict]:
    """Apply the page's selectors. Each is independent so a caller can omit one
    when counting that selector's own options."""
    out = rows
    if timeframe:
        out = [r for r in out if str(r.get("timeframe") or "") == timeframe]
    if governor:
        out = [r for r in out if str(r.get("governor") or "") == governor]
    if alignment == "agreed":
        out = [r for r in out if r.get("aligned_at_entry") is True]
    elif alignment == "opposed":
        out = [r for r in out if r.get("aligned_at_entry") is False]
    elif alignment == "unknown":
        out = [r for r in out if r.get("aligned_at_entry") is None]
    if status:
        out = [r for r in out if str(r.get("status") or "") == status]
    return out


def mark_distance_to_stop(rows: list[dict], prices: dict[str, float]) -> list[dict]:
    """Add the live mark and how far price sits from the parked stop.

    Distance-to-stop is the number the live tab exists for: it is the only thing
    on any SAR surface that says "this mechanism is about to act". Written into
    its own keys — never into a realized column, and never into ``sar_stop``,
    which is the engine's value and must stay the engine's value.

    A missing price leaves the row alone: the columns blank, the page renders.
    """
    for row in rows:
        if row.get("status") != STATUS_RUNNING:
            continue
        # Present-but-None, not absent: the column has a defined meaning
        # ("we could not compute it") and the template must not have to
        # distinguish a missing key from an unknown value.
        row.setdefault("stop_distance_pct", None)
        price = _f(prices.get(str(row.get("symbol") or "")))
        entry = _f(row.get("entry"))
        if price is None or price <= 0 or not entry:
            continue
        is_long = str(row.get("side") or "").upper() == "LONG"
        row["current_price"] = price
        move = (price - entry) if is_long else (entry - price)
        row["unrealized_pct"] = move / entry * 100.0
        stop = _f(row.get("sar_stop"))
        if stop is None or stop <= 0 or row.get("governor") != GOV_SAR:
            # The geometry leg's stop is the original SL, which the row already
            # carries; distance-to-SAR is meaningless until the handover.
            continue
        gap = (price - stop) if is_long else (stop - price)
        row["stop_distance_pct"] = gap / price * 100.0
    return rows


def summarize_resolved(rows: list[dict]) -> dict:
    """The verdict, on both fills, refusing rather than averaging over blanks.

    Both fills are reported side by side and neither is called *the* result.
    ``fill_level`` is the stop being touched intrabar; ``fill_confirm`` is
    waiting for the bar to close and exiting at market. The gap between them is
    the cost of confirmation, and choosing one before that cost is known would
    be picking the answer.

    Rows with no usable R (no SL distance at entry) are counted in ``n`` and
    excluded from every R figure, with the shortfall stated — scoring them 0R
    would drag the averages toward zero and make missing data read as mediocre
    performance.
    """
    closed = [r for r in rows if r.get("status") in RESOLVED_STATUSES]
    measurable = [r for r in closed if r.get("status") != STATUS_INSUFFICIENT]
    r_level = [_f(r.get("r_level")) for r in measurable]
    r_confirm = [_f(r.get("r_confirm")) for r in measurable]
    r_level = [x for x in r_level if x is not None]
    r_confirm = [x for x in r_confirm if x is not None]
    slip = [_f(r.get("confirm_slippage_pct")) for r in measurable]
    slip = [x for x in slip if x is not None]
    wins_level = sum(1 for x in r_level if x > 0)
    wins_confirm = sum(1 for x in r_confirm if x > 0)

    def _avg(xs: list[float]) -> Optional[float]:
        return (sum(xs) / len(xs)) if xs else None

    return {
        "n": len(closed),
        "measurable": len(measurable),
        "insufficient": sum(
            1 for r in closed if r.get("status") == STATUS_INSUFFICIENT
        ),
        "no_r": len(measurable) - len(r_level),
        "n_r": len(r_level),
        "avg_r_level": _avg(r_level),
        "avg_r_confirm": _avg(r_confirm),
        "win_rate_level": (wins_level / len(r_level)) if r_level else None,
        "win_rate_confirm": (wins_confirm / len(r_confirm)) if r_confirm else None,
        "avg_confirm_slippage_pct": _avg(slip),
        "by_exit": {
            reason: sum(1 for r in measurable if r.get("exit_reason") == reason)
            for reason in ("sar_flip", "static_sl", "static_tp1")
        },
        "handovers": sum(1 for r in measurable if r.get("handover_at")),
        "ambiguous": sum(1 for r in measurable if r.get("ambiguous_bar")),
    }


def summarize_by_timeframe(rows: list[dict]) -> list[dict]:
    """The 5m-vs-15m question, answered on the same population.

    Reported per timeframe rather than pooled because they are different
    experiments on the same signals — pooling them would let the mix of
    timeframes move the headline instead of the mechanism.
    """
    out = []
    for tf in sorted({str(r.get("timeframe") or "") for r in rows if r.get("timeframe")}):
        summary = summarize_resolved([r for r in rows if r.get("timeframe") == tf])
        summary["timeframe"] = tf
        out.append(summary)
    return out


def reduce_live_state(
    provenance: dict, live_rows: list[dict], now_ts: Optional[float] = None
) -> dict:
    """Is this tab actually live? — read before any number on it.

    A frozen arm file renders as a page full of open trades with plausible
    stops beside accurate live prices, which is precisely how the replay
    ledger looked healthy on 2026-07-29 while its newest resolution was 11.6h
    old. So the file's own age is stated first, separately from the prices.
    """
    now = time.time() if now_ts is None else now_ts
    age = provenance.get("age_sec")
    if not provenance.get("exists"):
        return {
            "state": "unavailable",
            "detail": (
                f"{provenance.get('file')} is not on the data volume. The engine "
                "writes it from the monitor loop — check SAR_LIVE_SHADOW_ENABLED "
                "and the engine container, not this page."
            ),
        }
    if provenance.get("newer_version"):
        return {
            "state": "orphan",
            "detail": (
                f"The engine has moved to {provenance.get('newer_file')}. Every "
                "number here describes a population it has discarded — bump "
                "SAR_LIVE_VERSION in data_volume.py."
            ),
        }
    if age is not None and age > LIVE_STALE_SEC:
        return {
            "state": "frozen",
            "detail": (
                f"The arm file has not been written for {age / 60.0:.1f} minutes. "
                "Arms advance on bar closes and flush within 15s, so the monitor "
                "loop is not stepping them. Live prices below are still real — "
                "a working price feed is not evidence the measurement is running."
            ),
        }
    if not live_rows:
        return {
            "state": "idle",
            "detail": (
                "The file is current and no signals are open, so there is "
                "nothing to run. This is the quiet case, not a fault."
            ),
        }
    return {
        "state": "live",
        "detail": f"{len(live_rows)} arms running, stepped inside the monitor loop.",
    }


def _view(request: Request, tab: str, **selectors: str) -> dict:
    vol = request.app.state.data_volume
    payload = vol.sar_live_arms()
    provenance = vol.sar_live_provenance()
    live_rows, resolved_rows = reduce_arms(payload)
    return {
        "payload": payload,
        "provenance": provenance,
        "live_rows": live_rows,
        "resolved_rows": resolved_rows,
        "tab": tab,
        "selectors": selectors,
    }


@router.get("/signals/sar-live")
async def sar_live(
    request: Request,
    tab: str = Query("live"),
    timeframe: str = Query(""),
    governor: str = Query(""),
    alignment: str = Query(""),
    status: str = Query(""),
):
    templates = request.app.state.templates
    ctx = _view(request, tab)
    live_rows, resolved_rows = ctx["live_rows"], ctx["resolved_rows"]
    base = live_rows if tab != "resolved" else resolved_rows

    # Every count is measured with every filter applied EXCEPT its own, so a
    # selector never makes each of its options describe only itself (#90/#91).
    scoped_tf = filter_arms(base, governor=governor, alignment=alignment, status=status)
    scoped_gov = filter_arms(base, timeframe=timeframe, alignment=alignment, status=status)
    scoped_align = filter_arms(base, timeframe=timeframe, governor=governor, status=status)
    selected = filter_arms(
        base, timeframe=timeframe, governor=governor, alignment=alignment, status=status
    )

    try:
        prices = await request.app.state.binance_klines.fetch_all_prices()
    except Exception:
        prices = {}

    # Truncate here, after every filter — never in a reducer. The cap is a
    # render bound and the template says when it bit.
    rows = selected[:TABLE_ROW_CAP]
    mark_distance_to_stop(rows, prices)

    return templates.TemplateResponse("sar_live.html", {
        "request": request,
        "active": "sar_live",
        "tab": tab,
        "rows": rows,
        "matched": len(selected),
        "shown": len(rows),
        "row_cap": TABLE_ROW_CAP,
        "n_live": len(live_rows),
        "n_resolved": len(resolved_rows),
        # Summaries measured on the full selection, never the truncated table.
        "summary": summarize_resolved(selected),
        "by_timeframe": summarize_by_timeframe(selected),
        "timeframes": sorted({r["timeframe"] for r in base if r.get("timeframe")}),
        "governors": sorted({r["governor"] for r in base if r.get("governor")}),
        "statuses": sorted({r["status"] for r in base if r.get("status")}),
        "n_tf": {
            tf: sum(1 for r in scoped_tf if r.get("timeframe") == tf)
            for tf in sorted({r["timeframe"] for r in base if r.get("timeframe")})
        },
        "n_gov": {
            g: sum(1 for r in scoped_gov if r.get("governor") == g)
            for g in sorted({r["governor"] for r in base if r.get("governor")})
        },
        "n_agreed": sum(1 for r in scoped_align if r.get("aligned_at_entry") is True),
        "n_opposed": sum(1 for r in scoped_align if r.get("aligned_at_entry") is False),
        "filter_timeframe": timeframe,
        "filter_governor": governor,
        "filter_alignment": alignment,
        "filter_status": status,
        "provenance": ctx["provenance"],
        "live_state": reduce_live_state(ctx["provenance"], live_rows),
    })


_ARM_COLS = [
    "arm_id", "signal_id", "symbol", "side", "setup_class", "timeframe",
    "entry", "stop_loss", "tp1", "sl_distance_pct",
    "aligned_at_entry", "governor", "handover_at", "sar_stop", "sar_up",
    "status", "exit_reason", "bars_seen",
    "fill_level", "fill_confirm", "pnl_level_pct", "pnl_confirm_pct",
    "r_level", "r_confirm", "confirm_slippage_pct",
    "mfe_pct", "current_price", "unrealized_pct", "stop_distance_pct",
    "ambiguous_bar",
]


@router.get("/signals/sar-live/export.csv")
async def sar_live_export_csv(
    request: Request,
    tab: str = Query("live"),
    timeframe: str = Query(""),
    governor: str = Query(""),
    alignment: str = Query(""),
    status: str = Query(""),
):
    """The current selection as CSV — uncapped, unlike the rendered table."""
    from app.reports import csv_response

    ctx = _view(request, tab)
    base = ctx["live_rows"] if tab != "resolved" else ctx["resolved_rows"]
    rows = filter_arms(
        base, timeframe=timeframe, governor=governor, alignment=alignment, status=status
    )
    try:
        prices = await request.app.state.binance_klines.fetch_all_prices()
    except Exception:
        prices = {}
    mark_distance_to_stop(rows, prices)
    data = [[r.get(c) for c in _ARM_COLS] for r in rows]
    return csv_response("sar_live_arms", _ARM_COLS, data)

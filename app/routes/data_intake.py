"""`/diagnostics/data-intake` — what we are actually reading from Binance.

Built for the price-action program (360-v2 `docs/PRICE_ACTION_PROGRAM.md`,
Phase 1) so that every later decision in it is read off visible numbers rather
than off a source-tree audit.

Why the page exists
-------------------
The 2026-08-05 audit that produced the program found four things, and **none of
them fails**:

* ``data_store.ticks`` is a one-shot ``/fapi/v1/trades`` snapshot taken at seed
  time, and five call sites read it as live — including a `$500k cumulative tick
  volume` gate.
* ``orderblocks`` has never had a writer, so every
  ``bool(fvgs) or bool(orderblocks)`` gate is ``bool(fvgs)`` alone.
* ``detect_fvg`` sees twelve bars, which is what makes a deliberately loose gate
  behave like a strict one.
* The order book is one bid and one ask.

Every one of those is a *provenance* problem, not an error — the numbers are the
right shape and mean something other than what the consumer assumes. Logs cannot
show that and a review will not catch it twice. A page can.

Rules this page holds to
------------------------
* **The census renders whether or not anything is wrong.** A check that appears
  only when it trips teaches the reader that its absence means "fine" when it
  equally means the check stopped running.
* **Provenance leads freshness.** Age answers "is this current"; source answers
  "is this the thing I think it is", and the audit turned entirely on the second.
* **Named absence.** A pool that was never started and a pool whose connections
  all died both show zero streams. They are different states with different
  fixes, so the engine names them and this page renders the name.
* **The engine's clock, not ours.** Every age on this page is computed engine-side
  from engine state. Ops adds no timestamp of its own — a surface may not grade
  its own liveness on a clock it supplies (#108).
* **Unreadable is not empty.** If the endpoint fails, the page says so with the
  cause. "Nothing is subscribed" and "we could not ask" must never render alike.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()

#: Stream kinds we expect to have an opinion about, in render order, with the
#: sentence that says what each one's absence *means*. Enumerated here rather
#: than derived from what is subscribed: an absence cannot be seen in a list of
#: what is present, which is exactly how a missing trade stream sat behind a
#: complete trade handler without anyone noticing.
STREAM_KIND_COPY: list[tuple[str, str, str]] = [
    ("klines", "Candles",
     "OHLCV per timeframe. The scanner's whole world today."),
    ("liquidations", "Liquidations",
     "@forceOrder. Feeds the liquidation-cluster and WHALE paths."),
    ("all_market_ticker", "Whole-board ticker",
     "!ticker@arr, ~1/sec. Drives mover ignition across ~600 pairs."),
    ("aggregate_trades", "Aggregate trades",
     "@aggTrade — per-trade aggression at price. WITHOUT it there is no "
     "footprint, no absorption and no real delta: CVD is one number per bar "
     "off the kline taker-buy field."),
    ("raw_trades", "Raw trades",
     "@trade. A complete handler for this exists in main.py and nothing "
     "subscribes it, so the tick store serves a seed-time REST snapshot."),
    ("depth", "Order book depth",
     "@depth. Without it the book is one bid and one ask from bookTicker — "
     "no walls, no refills, no absorption. Subscribed is not consumed: the "
     "depth card below says which book the four consumers are actually "
     "reading."),
]


#: What each price-action-lane refusal *means*, and what a reader does about it.
#: Keyed by the engine's own reason strings (`price_action_lane.REFUSE_*`).
#:
#: This is copy, **not a mirror of the reason list**. The page renders whatever
#: refusals the engine sends and looks its sentence up here; a reason with no
#: entry renders under its raw name with an `unclassified` badge rather than
#: being dropped or quietly bucketed. That matters because the alternative —
#: iterating a list ops keeps — is silent by construction on the next reason
#: somebody adds, which is the `MEASUREMENT_SUFFIXES` drift and the
#: `is_tradfi_perp` deny-list wearing a third hat. One writer, one reader.
#:
#: The four classes exist because they have four different next moves, and
#: pooling any two of them into "no signals" is how a page reports a fault that
#: is not happening.
LANE_REFUSAL_COPY: dict[str, tuple[str, str]] = {
    # fault — the lane is blind and cannot answer at all
    "no_levels": ("fault", "the LevelBook holds nothing for this symbol, so "
                           "there is no level to sweep. The lane is blind here."),
    "short_series": ("fault", "not enough bars to look back over. Blind for a "
                              "different reason, and it fixes differently."),
    "bad_geometry": ("fault", "entry, stop and target did not form a usable "
                              "trade. Rare by construction — a run of these is "
                              "a defect, not a market."),
    # coverage — a layer this lane depends on does not reach the symbol
    "no_footprint": ("coverage", "the delta-confirmation layer (Phase 2b) does "
                                 "not cover this symbol. Nothing is wrong with "
                                 "the market; we cannot confirm."),
    # market — the setup was genuinely not on offer, or was and failed its test
    "no_sweep": ("market", "no level was swept and reclaimed on the newest "
                           "closed bar. The setup is not on offer — this is the "
                           "quiet case and it needs no action."),
    "delta_opposed": ("market", "a sweep was found and the flow disagreed with "
                                "it. This is the confirmation layer WORKING, "
                                "not a refusal to look."),
    "no_opposing_target": ("market", "nothing structural ahead to target, so "
                                     "there is no honest TP1."),
    "rr_below_floor": ("market", "the trade exists and does not pay. Counted "
                                 "apart from the ones we could not see."),
    # throttle — our own decision, and the one reading that proves the lane fires
    "cooldown": ("throttle", "a setup was found and deliberately not stamped: "
                             "this symbol emitted inside the window. A non-zero "
                             "count here is POSITIVE evidence the lane fires — "
                             "the unit of evidence is the move, not the scan."),
}

#: Render order for the class rollup. Fault first because it is the only class
#: that is ours to fix today.
LANE_REFUSAL_CLASSES: list[tuple[str, str]] = [
    ("fault", "the lane is blind — ours to fix"),
    ("coverage", "a dependency does not reach the symbol"),
    ("market", "the setup was not on offer, or failed its own test"),
    ("throttle", "found and deliberately not stamped"),
    ("unclassified", "the engine sent a reason this page has no sentence for"),
]


def lane_refusal_rows(report: Any) -> list[dict[str, Any]]:
    """Refusals as rows, largest share first, every engine reason represented.

    Deliberately driven by the engine's payload rather than by
    `LANE_REFUSAL_COPY`'s keys: a reason ops has never heard of must appear —
    named, badged `unclassified` — instead of vanishing into a total that still
    adds up.
    """
    # Nested under `derived`, where the engine assembles it. Walked rather
    # than assumed: the first cut of this page read it off the top level —
    # a location ops chose — and its fixture agreed, so the test went green
    # over a card that would have rendered NOT REPORTED against the real
    # engine. Pinned on the producing side too (360-v2
    # `test_price_action_lane.test_the_payload_key_is_the_one_ops_reads`).
    lane = ((report or {}).get("derived") or {}).get("price_action_lane") or {}
    refusals = lane.get("refusals") or {}
    shares = lane.get("refusal_share") or {}
    rows: list[dict[str, Any]] = []
    for reason, count in refusals.items():
        cls, why = LANE_REFUSAL_COPY.get(reason, ("unclassified", ""))
        rows.append({
            "reason": reason,
            "count": int(count or 0),
            "share": shares.get(reason),
            "cls": cls,
            "why": why,
        })
    rows.sort(key=lambda r: (-(r["share"] or 0.0), -r["count"], r["reason"]))
    return rows


def lane_class_totals(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-class rollup over the same rows the table shows (#90)."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        agg = out.setdefault(r["cls"], {"count": 0, "share": 0.0})
        agg["count"] += r["count"]
        agg["share"] += r["share"] or 0.0
    return out


def _pool_badge(state: str) -> tuple[str, str]:
    """(css class, label) for a pool state. Named states, never a boolean."""
    return {
        "healthy": ("badge-ok", "HEALTHY"),
        "partially_degraded": ("badge-warn", "PARTIALLY DEGRADED"),
        "all_degraded": ("badge-err", "ALL DEGRADED"),
        "no_connections": ("badge-err", "NO CONNECTIONS"),
        "not_started": ("badge", "NOT STARTED"),
    }.get(state, ("badge", state.upper() or "UNKNOWN"))


@router.get("/diagnostics/data-intake")
async def data_intake_page(request: Request):
    report: Any = None
    error = ""
    try:
        report = await request.app.state.engine_api.data_intake()
    except Exception as exc:  # noqa: BLE001
        # Named cause. An empty page here would read exactly like an engine
        # with nothing subscribed, which is the one conclusion this page must
        # never let a reader reach by accident.
        error = f"engine data-intake endpoint unavailable: {type(exc).__name__}: {exc}"

    if report is not None and not isinstance(report, dict):
        error = error or f"unexpected payload shape: {type(report).__name__}"
        report = None
    if isinstance(report, dict) and report.get("error"):
        # A payload carrying only an error is NOT a report. Rendering the
        # sections from it would show empty pools, an empty census and a zero
        # weight gauge — every one of which reads as a fact about the engine
        # rather than as the absence of an answer.
        error = error or f"engine reported: {report['error']}"
        report = None

    lane_rows = lane_refusal_rows(report)

    return request.app.state.templates.TemplateResponse(
        "data_intake.html",
        {
            "request": request,
            "active": "data_intake",
            "report": report,
            "error": error,
            "stream_kind_copy": STREAM_KIND_COPY,
            "pool_badge": _pool_badge,
            "lane_rows": lane_rows,
            "lane_class_totals": lane_class_totals(lane_rows),
            "lane_classes": LANE_REFUSAL_CLASSES,
        },
    )

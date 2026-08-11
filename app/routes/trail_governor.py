"""`/signals/trail-governor` — the one exit surface that is not a measurement.

Every other trailing page in this repo (`/signals/sar-live`,
`/signals/atr-live`, `/signals/sar`, the Profit tab's bake-off) renders a
ledger of where a stop *would* have been parked. This one renders the stop the
engine is **actually amending on a live Binance account**, bar by bar, for the
users who opted in — added 2026-08-10 for the owner's own-capital test of the
SAR exit ("only for me not for us not for users").

The distinction that decides how the page is written
-----------------------------------------------------
A measurement page's worst failure is a wrong number. This page's worst failure
is a **reassuring blank**: a governor that is switched off, that nobody has
opted into, that is refusing every series as stale, and one that is working
perfectly on a quiet book all render as "no rows" unless the page insists on
saying which. So the refusal mix leads, before the table, and the four states
are never pooled — this is the repo's own "blank needs a cause before it gets a
caption" rule arriving somewhere it can cost money.

Rules it inherits
-----------------
* **Freshness is graded on the ENGINE's stamp, never on ops' clock** (#108).
  Each row carries `bar_age_sec` computed engine-side against the bar the
  governor actually consumed. A page that fetched a price itself and printed it
  beside a two-hour-old stop under the words "right now" is exactly the defect
  `/signals/sar-live` paid for.
* **A cold position index is not an empty book.** The engine returns
  `index_cold` rather than falling back to a Firestore query, and this page
  renders that as its own state.
* **The mode is read off the payload**, never mirrored from a copy of the
  engine's flag registry — the fix for a drifting mirror is not a second
  mirror.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Query, Request

from app.reports import csv_response

router = APIRouter()

#: Copy attached to each refusal the engine may report. Rendered by looking up
#: the ENGINE's key, never by iterating this dict — a reason ops has never
#: heard of must render under its raw name rather than vanish.
REFUSAL_COPY: dict[str, tuple[str, str]] = {
    "disabled": (
        "switch",
        "The master switch is off. Every position is on the ordinary SL/TP "
        "exit — this is the default and is not a fault.",
    ),
    "kill_switch": (
        "switch",
        "The global kill switch is engaged, so the governor is not amending "
        "anything. Parked stops stay exactly where they are.",
    ),
    "index_cold": (
        "fault",
        "The engine's in-memory position index is not serving, so the sweep "
        "cannot see the book at all. Not the same as nothing being open.",
    ),
    "ladder_touched": (
        "expected",
        "The position already fired pre-TP, shifted to BE or filled a TP leg, "
        "so the governor declined to adopt it mid-flight. Expected and benign.",
    ),
    "stale_series": (
        "fault",
        "The newest closed bar is too old to park a level against — typically "
        "a promoted mover on REST re-seed. The existing stop is left alone.",
    ),
    "no_series": (
        "fault",
        "No usable candle window for THIS SYMBOL. The existing stop is left "
        "alone; the position is protected, not naked.",
    ),
    "bad_timeframe": (
        "switch",
        "The configured governing timeframe is not one the candle store "
        "carries — check Control → Engine → Stops & exits. Until it is 5m or "
        "15m the governor can never hand over, and every position keeps its "
        "original SL/TP.",
    ),
    "not_onside": (
        "expected",
        "The mechanism has not come onside yet, so the evaluator's own SL and "
        "TP1 are still governing. This is the pre-handover state.",
    ),
    "no_quantity": (
        "fault",
        "The position's remaining size read as zero, so there was nothing to "
        "park a stop over and none was sent. The exchange was never asked — "
        "this is our own book, not a rejection.",
    ),
    "no_level": (
        "fault",
        "The mechanism could not produce a level for the bar now forming.",
    ),
}


def classify_refusals(health: dict[str, Any]) -> list[dict[str, Any]]:
    """Refusals, in the engine's own vocabulary, each with its class.

    Iterates the ENGINE's payload. A reason this repo has never heard of is
    rendered under its raw name and badged, never dropped and never bucketed
    into a neighbour.
    """
    raw = (health or {}).get("refusals") or {}
    out: list[dict[str, Any]] = []
    for reason, count in sorted(raw.items(), key=lambda kv: -int(kv[1] or 0)):
        cls, note = REFUSAL_COPY.get(str(reason), ("unclassified", ""))
        out.append({
            "reason": reason,
            "count": int(count or 0),
            "cls": cls,
            "note": note,
        })
    return out


#: Render bound on the history table.  Applied in the route AFTER filtering, and
#: the page says when it bit — a cap inside a reducer filters the newest N and
#: then filters that, which starves exactly the rarest rows (#97).  The CSV is
#: uncapped, because a truncated export is #97 wearing a download button.
HISTORY_ROW_CAP = 500

#: What each fill means, in one sentence.  Rendered by looking up the ENGINE's
#: `exit_kind`; a kind ops has never heard of renders under its raw name badged
#: `unclassified` rather than being dropped or bucketed into a neighbour.
FILL_COPY: dict[str, tuple[str, str]] = {
    "trail_stop": (
        "@level",
        "The parked stop was touched. This is the fill the measurement lane "
        "calls <code>@level</code>.",
    ),
    "flip_close": (
        "@confirm",
        "The mechanism came offside — SAR flipped, or the chandelier's level "
        "was already past the close — and the position was closed at market. "
        "The measurement lane's <code>@confirm</code>.",
    ),
}


def reduce_history(payload: Any) -> tuple[list[dict[str, Any]], Optional[str]]:
    """The stored record, newest FIRST, plus a reason when it cannot be read.

    ``(rows, error)`` rather than an empty list on failure: *could not read* and
    *nothing has closed yet* are different states with different next moves, and
    a page that renders them alike reports a fault that is not happening — or,
    worse, hides one that is.
    """
    if payload is None:
        return [], "the engine did not report a history ledger"
    if isinstance(payload, dict) and payload.get("error"):
        err = str(payload["error"])
        # The engine's own reader says "missing: <path>" for a file it has never
        # written. That is an engine that predates the ledger or a governor that
        # has never closed a position — NOT a fault on our side, and the two
        # send a reader to completely different places.
        if err.startswith("missing:"):
            return [], None
        return [], err
    if not isinstance(payload, dict):
        return [], f"unexpected ledger shape: {type(payload).__name__}"
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return [], "unexpected ledger shape: no row list"
    clean = [r for r in rows if isinstance(r, dict)]
    clean.sort(key=lambda r: float(r.get("ts") or 0.0), reverse=True)
    # Rendered UTC, stamped here rather than in the template: every figure in
    # this dashboard is UTC because the engine is UTC end to end, and a date
    # with no zone is the same class of omission as a percentage with no
    # denominator.  A row with no readable stamp renders an em-dash rather than
    # the epoch — 1970 beside a live trade is a number nobody computed.
    for r in clean:
        try:
            r["closed_utc"] = datetime.fromtimestamp(
                float(r.get("ts") or 0.0), tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")
        except (TypeError, ValueError, OSError, OverflowError):
            r["closed_utc"] = None
    return clean, None


def filter_history(
    rows: list[dict[str, Any]],
    *,
    symbol: str = "",
    fill: str = "",
    mechanism: str = "",
) -> list[dict[str, Any]]:
    """Narrow the record. Each selector's own counts are computed by the caller
    with every filter applied EXCEPT its own (#90/#91) — a selector applied to
    its own counts makes each option read "n = whatever I picked"."""
    out = rows
    if symbol:
        out = [r for r in out if str(r.get("symbol") or "") == symbol]
    if fill:
        out = [r for r in out if str(r.get("exit_kind") or "") == fill]
    if mechanism:
        out = [r for r in out if str(r.get("mechanism") or "") == mechanism]
    return out


def summarize_history(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-fill totals — **never** a blended figure across the two.

    Their difference is the cost of confirmation, which is the number this
    mechanism's design turns on, so there is deliberately no pooled average here
    any more than there is on `/signals/sar-live`. A row whose fill price never
    arrived is counted in ``unpriced`` rather than averaged as a flat trade:
    `None` and `0.0` are different facts and a zero there is a claim.
    """
    out: dict[str, Any] = {"n": len(rows), "unpriced": 0, "by_fill": {}}
    buckets: dict[str, list[float]] = {}
    for r in rows:
        kind = str(r.get("exit_kind") or "unknown")
        buckets.setdefault(kind, [])
        pnl = r.get("pnl_pct")
        if pnl is None:
            out["unpriced"] += 1
            continue
        try:
            buckets[kind].append(float(pnl))
        except (TypeError, ValueError):
            out["unpriced"] += 1
    for kind, vals in sorted(buckets.items()):
        n = len(vals)
        label, note = FILL_COPY.get(kind, (kind, ""))
        out["by_fill"][kind] = {
            "label": label,
            "note": note,
            "classified": kind in FILL_COPY,
            "n": n,
            "wins": sum(1 for v in vals if v > 0),
            "win_rate": (sum(1 for v in vals if v > 0) / n * 100.0) if n else None,
            "avg_pnl_pct": (sum(vals) / n) if n else None,
            "total_pnl_pct": sum(vals) if n else None,
            "best_pnl_pct": max(vals) if n else None,
            "worst_pnl_pct": min(vals) if n else None,
        }
    return out


def lane_state(payload: dict[str, Any]) -> str:
    """One of: `error` · `index_cold` · `off` · `armed` · `governing`.

    Five states rather than "on/off", because the middle three all render as
    an empty table and have entirely different next moves.
    """
    if not isinstance(payload, dict) or payload.get("error"):
        return "error"
    if payload.get("index_cold"):
        return "index_cold"
    if not payload.get("enabled"):
        return "off"
    if int(payload.get("governed") or 0) > 0:
        return "governing"
    return "armed"


HISTORY_COLS = (
    "ts", "signal_id", "uid", "symbol", "side", "mechanism", "exit_kind",
    "entry", "exit", "pnl_pct", "designed_sl", "parked_stop", "seq",
)


@router.get("/signals/trail-governor/history.csv")
async def trail_governor_history_export(
    request: Request,
    symbol: str = Query(""),
    fill: str = Query(""),
    mechanism: str = Query(""),
):
    """The record as CSV, honouring the current filter and **uncapped**.

    A truncated export is #97 wearing a download button, and it matters more
    here than on any measurement page: these rows are real fills that cannot be
    re-derived, so a spreadsheet quietly missing the oldest of them is a trade
    record with a hole in it. `exit_kind` rides on every row, because a
    spreadsheet is precisely where the two fills get averaged into one.
    """
    vol = request.app.state.data_volume
    rows, _err = reduce_history(vol.trail_history())
    rows = filter_history(rows, symbol=symbol, fill=fill, mechanism=mechanism)
    data = [[r.get(c) for c in HISTORY_COLS] for r in rows]
    return csv_response("trail_governor_history", list(HISTORY_COLS), data)


@router.get("/signals/trail-governor")
async def trail_governor_page(
    request: Request,
    symbol: str = Query(""),
    fill: str = Query(""),
    mechanism: str = Query(""),
):
    api = request.app.state.engine_api
    payload = await api.trail_governor()
    if not isinstance(payload, dict):
        payload = {"error": "engine returned a non-object payload"}

    health = payload.get("health") or {}
    rows = payload.get("rows") or []
    # Handed-over rows first: those are the ones carrying a stop nobody else
    # placed, and they are what a reader is here to check.
    rows = sorted(
        [r for r in rows if isinstance(r, dict)],
        key=lambda r: (not r.get("governing"), r.get("symbol") or ""),
    )

    # The traded record, read off the data volume rather than the diag payload.
    # The payload's `history` is a bounded tail (it rides a snapshot written
    # every ~15s); this is the whole ledger, which is what "keep the history"
    # means.
    vol = request.app.state.data_volume
    hist_all, hist_error = reduce_history(vol.trail_history())

    # Every selector's counts are measured with every filter applied EXCEPT its
    # own (#90/#91) — otherwise each option reads "n = whatever I picked".
    scoped_symbol = filter_history(hist_all, fill=fill, mechanism=mechanism)
    scoped_fill = filter_history(hist_all, symbol=symbol, mechanism=mechanism)
    scoped_mech = filter_history(hist_all, symbol=symbol, fill=fill)
    hist_sel = filter_history(
        hist_all, symbol=symbol, fill=fill, mechanism=mechanism
    )

    def _counts(rows: list[dict[str, Any]], key: str) -> list[tuple[str, int]]:
        seen: dict[str, int] = {}
        for r in rows:
            v = str(r.get(key) or "")
            if v:
                seen[v] = seen.get(v, 0) + 1
        return sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "trail_governor.html",
        {
            "request": request,
            "state": lane_state(payload),
            # The record. Summaries are measured on the SELECTION, never on the
            # truncated table (#90) — and the cap is applied here, after every
            # filter, with the template saying when it bit (#97).
            "history": hist_sel[:HISTORY_ROW_CAP],
            "history_matched": len(hist_sel),
            "history_shown": min(len(hist_sel), HISTORY_ROW_CAP),
            "history_cap": HISTORY_ROW_CAP,
            "history_total": len(hist_all),
            "history_summary": summarize_history(hist_sel),
            "history_error": hist_error,
            "history_stats": payload.get("history_stats"),
            "history_provenance": vol.trail_history_provenance(),
            "sel": {"symbol": symbol, "fill": fill, "mechanism": mechanism},
            "symbol_counts": _counts(scoped_symbol, "symbol"),
            "fill_counts": _counts(scoped_fill, "exit_kind"),
            "mechanism_counts": _counts(scoped_mech, "mechanism"),
            "fill_copy": FILL_COPY,
            "error": payload.get("error"),
            "enabled": bool(payload.get("enabled")),
            "timeframe": payload.get("timeframe"),
            "rows": rows,
            "governed": int(payload.get("governed") or 0),
            # The sweep's OWN count, which is not the same population: it also
            # requires `protection_mode == "managed"`, so a `user_owned` take
            # carrying a mechanism appears in `governed` and not here.  Both
            # render, and the page says why they differ — they were shown under
            # the same word until 2026-08-11, one in the badge and one in the
            # counters table, where a disagreement read as a contradiction.
            "swept_governed": (health or {}).get("governed"),
            "open_total": payload.get("open_total"),
            "health": health,
            # Realized governed exits, newest FIRST for reading.  The engine
            # appends newest-last (a ring), so the reversal happens here rather
            # than engine-side: the ledger's order is its own business and a
            # display preference must not reach back into it.
            "outcomes": list(reversed(
                [o for o in ((health or {}).get("outcomes") or [])
                 if isinstance(o, dict)]
            )),
            "refusals": classify_refusals(health),
            "active": "trail_governor",
        },
    )

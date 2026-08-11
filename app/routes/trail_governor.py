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

from typing import Any

from fastapi import APIRouter, Request

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


@router.get("/signals/trail-governor")
async def trail_governor_page(request: Request):
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

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "trail_governor.html",
        {
            "request": request,
            "state": lane_state(payload),
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

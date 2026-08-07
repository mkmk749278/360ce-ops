"""`/signals/router-drops` — the last hop before a subscriber.

Every other funnel surface in this repo stops at **enqueue**. The scanner's own
path funnel increments `emitted` immediately after `_enqueue_signal` succeeds,
so that column counts enqueues and has been called *emitted* the whole time —
and `SignalRouter._process` then rejects on **twelve** further conditions before
anything reaches a channel, a push or the app feed.

What this page exists to answer
-------------------------------
Owner, 2026-08-07: *"that max concurrent and same direction stopping signals
from other paths? because MVRTP might generate more signals so this might be the
reason also?"*

The delivered book is consistent with that and cannot prove it: `MOVER_TREND_
PULLBACK` is 65.7% of delivered against ~59% of enqueued, and it delivers 158
LONG / 68 SHORT while every other path combined delivers 52 LONG / 66 SHORT.
`MAX_SAME_DIRECTION_GLOBAL` is a **global** cap of 3, so a long-skewed
high-volume path would eat the long slots — but a long-skewed *market* produces
the same table, and the delivered rows cannot separate the two.

The rows that **were dropped** can, and the engine has been counting them keyed
`reason:setup_class` all along. `delivery_stats()` computed it into
`drops_by_reason_setup` and its only caller logged the *un*-keyed half, so the
decisive number was written on every cycle and read by nobody.

Rules this page carries
-----------------------
* **The denominator is `processed`, never enqueued.** `processed` is what the
  router dequeued; anything else divides by a population this surface never saw.
* **A drop is not a fault.** Every gate here is doing its job — the caps are
  blast-radius protection and this page is not an argument to widen them. It
  says *where* volume goes, and separately whether one path is absorbing a
  shared cap.
* **`reason` and `reason:setup` are never pooled.** The un-keyed total is the
  gate's cost; the keyed rows are its distribution across paths. Summing the
  keyed rows can fall short of the total when a signal carries no setup class,
  and that shortfall is shown rather than hidden.
* **Counters are cumulative since engine start and reset on restart.** They are
  in-process ints, not a ledger. The page says so, because a number that silently
  restarts reads as a quiet market.
* **A shared cap is the only one that can starve another path.** `per_channel_cap`
  and `same_direction_throttle` are shared; `symbol_channel_cooldown` and
  `correlation_lock` are per symbol and cannot. They are grouped apart, because
  reading a per-symbol cooldown as crowding-out is the whole error this page
  exists to prevent.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()

#: Gates whose budget is SHARED across every path, so one path consuming them
#: genuinely starves another. Mirrors the engine's own `_drop` reasons; a reason
#: this list has never heard of renders under its raw name rather than being
#: dropped or silently bucketed (`MEASUREMENT_SUFFIXES` wearing another hat).
SHARED_CAPS = {
    "same_direction_throttle": (
        "MAX_SAME_DIRECTION_GLOBAL — a GLOBAL cap of 3 concurrent positions in "
        "one direction, across every path and every symbol. The only gate here "
        "whose budget is shared book-wide."
    ),
    "per_channel_cap": (
        "MAX_CONCURRENT_SIGNALS_PER_CHANNEL — 5 for 360_SCALP, 3 elsewhere. "
        "Shared by every setup that emits on the same channel."
    ),
    "correlation_group_limit": (
        "MAX_SAME_DIRECTION_PER_GROUP — 3 per correlated group (~25 named "
        "pairs). Shared, but only within a group."
    ),
}

#: Gates keyed on the candidate's own symbol or its own geometry. A path cannot
#: consume another path's budget here, so a big number is volume, not crowding.
PER_CANDIDATE = {
    "correlation_lock": "one open position per symbol, either direction",
    "symbol_channel_cooldown": "60s after a signal on the same symbol+channel",
    "stale_age": "120s for scalp channels, 3600s otherwise",
    "stale_past_tp1": "detection-time price was already past TP1",
    "stale_past_sl": "detection-time price was already past SL",
    "tp_sanity": "TP1 on the wrong side of entry",
    "sl_sanity": "SL on the wrong side of entry",
    "channel_min_confidence": "below the channel floor, re-checked after AI enrichment",
    "watchlist_tier": "defensive — the tier was removed in the app-era reset",
}


def _i(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def classify(reason: str) -> str:
    if reason in SHARED_CAPS:
        return "shared_cap"
    if reason in PER_CANDIDATE:
        return "per_candidate"
    return "unclassified"


def reduce_drops(payload: dict) -> dict:
    """Split the census into the two questions it can answer.

    Iterates **the engine's payload**, never this module's own keys: a reason
    ops has never heard of renders under its raw name badged `unclassified`,
    because a reader iterating its own copy is silent by construction on the
    next gate the engine adds.
    """
    if not isinstance(payload, dict):
        return {"available": False, "reason": "no payload"}
    if payload.get("error"):
        return {"available": False, "reason": str(payload["error"])}
    if _i(payload.get("schema")) < 1:
        return {"available": False, "reason": "engine reported no census (schema 0)"}

    by_reason = payload.get("drops_by_reason") or {}
    by_setup = payload.get("drops_by_reason_setup") or {}
    processed = _i(payload.get("processed"))
    delivered = _i(payload.get("delivered"))
    dropped = _i(payload.get("dropped"))

    rows = []
    for reason, count in sorted(by_reason.items(), key=lambda kv: -_i(kv[1])):
        n = _i(count)
        # Every setup row belonging to this reason. Split on the FIRST colon
        # only — a setup class may not contain one today, and assuming it never
        # will is how a hand-read key silently drops rows.
        setups = []
        for key, v in by_setup.items():
            head, sep, tail = str(key).partition(":")
            if sep and head == reason:
                setups.append({"setup": tail or "UNKNOWN", "n": _i(v)})
        setups.sort(key=lambda r: -r["n"])
        attributed = sum(s["n"] for s in setups)
        rows.append({
            "reason": reason,
            "n": n,
            "share_of_processed": (n / processed) if processed else None,
            "kind": classify(reason),
            "note": SHARED_CAPS.get(reason) or PER_CANDIDATE.get(reason) or "",
            "setups": setups,
            # Named, never hidden: a signal with no setup class is attributed to
            # no row, so the keyed rows can fall short of the gate's own total.
            "unattributed": max(0, n - attributed),
        })

    shared = [r for r in rows if r["kind"] == "shared_cap"]
    return {
        "available": True,
        "processed": processed,
        "delivered": delivered,
        "dropped": dropped,
        "delivery_rate": payload.get("delivery_rate"),
        "rows": rows,
        "shared_total": sum(r["n"] for r in shared),
        "shared_share": (
            sum(r["n"] for r in shared) / processed if processed else None
        ),
        "unclassified": [r["reason"] for r in rows if r["kind"] == "unclassified"],
    }


def concentration(reduced: dict) -> list[dict]:
    """Per setup, across the SHARED caps only — the crowding-out question.

    Deliberately excludes per-candidate gates. A path with a huge
    `symbol_channel_cooldown` count is re-detecting its own symbol and is taking
    nothing from anybody; pooling that in would manufacture crowding out of
    ordinary volume, which is the exact misread this page exists to prevent.
    """
    if not reduced.get("available"):
        return []
    totals: dict[str, int] = {}
    for row in reduced["rows"]:
        if row["kind"] != "shared_cap":
            continue
        for s in row["setups"]:
            totals[s["setup"]] = totals.get(s["setup"], 0) + s["n"]
    grand = sum(totals.values())
    return [
        {"setup": k, "n": v, "share": (v / grand) if grand else None}
        for k, v in sorted(totals.items(), key=lambda kv: -kv[1])
    ]


@router.get("/signals/router-drops")
async def router_drops(request: Request):
    engine = request.app.state.engine_api
    error = None
    payload: dict = {}
    try:
        raw = await engine.router_delivery()
        payload = raw if isinstance(raw, dict) else {}
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    reduced = reduce_drops(payload)
    return request.app.state.templates.TemplateResponse(
        "router_drops.html",
        {
            "request": request,
            "active": "router_drops",
            "reduced": reduced,
            "by_setup": concentration(reduced),
            "error": error,
            "raw": payload,
        },
    )

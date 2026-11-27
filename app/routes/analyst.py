"""``/analyst`` — the hourly AI analyst's plans and their dark outcomes (2026-11-20).

Engine side: ``src/analyst_plan.py`` (validator + ledger) and
``src/analyst_walk.py`` (the dark forward walk), read through the diagnostic
catalog: ``read.analyst_outcomes`` and ``read.analyst_plans``. Ops computes
nothing about either — both are assembled in the ENGINE container, which holds
the ledger and the candle store.

The page is the spec's §1 refutation condition made visible: the ``analyst``
arm sits beside ``fixed_rule`` (the same symbol and side, levels picked
mechanically from the same menu). If the rule matches the analyst net of fees,
the model added nothing.

Rules it carries, each one already in this repo's CLAUDE.md:

* **Per arm, never pooled**, and every arm renders whether or not it has a row.
* **n leads every row**, and the top-1% trim sits beside the average: our gross
  is tail-driven, so an average without its tail is the honest second reading.
* **Unfilled limits are scored at market**, and their count is on screen.
* **An arm the engine has not built is named** (``arms_not_built``), never
  shown as an empty row that reads like zero.
* **Four states, never pooled:** the engine predates the entry, the engine
  answered with an error (its own words shown), no ledger yet (the analyst has
  not submitted a plan — expected before step 5), or data.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()

STATE_FAILED = "failed"
STATE_NOT_REPORTED = "not_reported"
STATE_NO_LEDGER = "no_ledger"
STATE_OK = "ok"

#: The engine's arm keys, in display order. Labels only: an arm the engine
#: publishes and this map lacks still renders, under its raw key.
ARM_LABELS = {
    "analyst": "Analyst — limit at its first entry level",
    "tranche": "Analyst — its own tranches",
    "ladder": "Analyst — half at TP1, half to TP2",
    "fixed_rule": "Fixed rule — same menu, mechanical levels",
    "market": "Market at submit",
    "none": "No trade",
}


def classify(payload: Any) -> tuple[str, str]:
    """(state, detail) for one catalog call. Key presence, never truthiness."""
    if not isinstance(payload, dict):
        return STATE_FAILED, "no answer"
    if "ok" in payload:
        if payload.get("ok") is False:
            err = str(payload.get("error") or "")
            if "unknown catalog entry" in err:
                return STATE_NOT_REPORTED, err
            return STATE_FAILED, err or "the engine refused without a reason"
        res = payload.get("result")
        if isinstance(res, dict) and res.get("readable") is False:
            return STATE_NO_LEDGER, str(res.get("reason") or "")
        return STATE_OK, ""
    if "error" in payload:  # ops' own transport wrapper
        return STATE_FAILED, str(payload.get("error") or "") or "transport failed with no message"
    return STATE_FAILED, "unrecognised reply"


def arm_rows(by_arm: dict) -> list[dict]:
    """Display rows iterating the ENGINE's arms, labelled where known."""
    keys = [k for k in ARM_LABELS if k in by_arm] + [k for k in by_arm if k not in ARM_LABELS]
    rows = []
    for k in keys:
        a = by_arm.get(k) or {}
        trim = a.get("without_top_1pct") or {}
        filled = a.get("filled") or {}
        cost = a.get("cost_model") or {}
        old = a.get("with_unfilled_at_market") or {}
        missed = a.get("missed_market") or {}
        day = a.get("by_day") or {}
        rows.append({
            # The engine says what its headline is (walk v2, 2026-11-27: realized
            # per setup posted, an unfilled limit = 0.00%). An engine predating it
            # sends no basis, and its headline pooled unfilled rows at market.
            "basis": a.get("basis"),
            "filled_n": filled.get("n"), "filled_avg": filled.get("avg_net_pct"),
            "cost_n": cost.get("n"), "cost_avg": cost.get("avg_net_pct"),
            "cost_unknown": cost.get("unknown"),
            "cost_known_filled": cost.get("known_filled"),
            "cost_flat_same": cost.get("flat_fee_same_rows_avg_pct"),
            "old_avg": old.get("avg_net_pct"), "missed_n": missed.get("n"),
            "missed_avg": missed.get("avg_net_pct"),
            "n_days": day.get("n_days"), "days_positive": day.get("days_positive"),
            "avg_day_total": day.get("avg_day_total_pct"),
            "excluded_blend_defect": a.get("excluded_blend_defect"),
            "key": k, "label": ARM_LABELS.get(k, k), "known": k in ARM_LABELS,
            "n": a.get("n", 0), "win_rate": a.get("win_rate"),
            "avg_net_pct": a.get("avg_net_pct"), "total_net_pct": a.get("total_net_pct"),
            "trim_avg": trim.get("avg_net_pct"), "trim_dropped": trim.get("dropped"),
            "open": a.get("open", 0), "insufficient": a.get("insufficient", 0),
            "unfilled": a.get("unfilled_scored_at_market", 0),
            # None = an engine predating the 2026-11-25 geometry fix, not zero.
            "no_valid_trade": a.get("no_valid_trade"),
        })
    return rows


def day_rows(by_arm: dict, arms: tuple = ("analyst", "market", "fixed_rule")) -> list[dict]:
    """Per UTC submit day, each arm's realized total side by side. Days come
    from the engine's rows; a day one arm lacks reads as missing, not zero."""
    per = {k: {d["day"]: d for d in ((by_arm.get(k) or {}).get("by_day") or {}).get("days") or []}
           for k in arms}
    days = sorted({d for m in per.values() for d in m}, reverse=True)
    return [{"day": d, **{k: per[k].get(d) for k in arms}} for d in days]


@router.get("/analyst")
async def analyst_page(request: Request):
    api = request.app.state.engine_api
    try:
        out_raw = await api.diag_run("read.analyst_outcomes", {})
    except Exception as exc:  # pragma: no cover - defensive
        out_raw = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        plans_raw = await api.diag_run("read.analyst_plans", {"limit": 10})
    except Exception as exc:  # pragma: no cover - defensive
        plans_raw = {"error": f"{type(exc).__name__}: {exc}"}

    out_state, out_detail = classify(out_raw)
    plans_state, plans_detail = classify(plans_raw)
    outcomes = (out_raw.get("result") or {}) if out_state == STATE_OK else {}
    plans = (plans_raw.get("result") or {}) if plans_state == STATE_OK else {}

    return request.app.state.templates.TemplateResponse("analyst.html", {
        "request": request, "active": "analyst",
        "out_state": out_state, "out_detail": out_detail,
        "rows": arm_rows(outcomes.get("by_arm") or {}),
        "arms_not_built": outcomes.get("arms_not_built") or [],
        "note": outcomes.get("note"),
        # Each None = an engine predating it (2026-11-25), rendered as such.
        "split": outcomes.get("analyst_split"),
        "whatif": outcomes.get("analyst_whatif"),
        "calibration": outcomes.get("calibration"),
        "walk_version": outcomes.get("walk_version"),
        "days": day_rows(outcomes.get("by_arm") or {}),
        "book": outcomes.get("active_book"),
        "recent_closed": outcomes.get("recent_closed"),
        "plans_state": plans_state, "plans_detail": plans_detail,
        "plans": plans.get("plans") or [],
        "totals": plans.get("totals") or {},
        "rejections": plans.get("rejections_by_reason") or {},
    })


# ── /signals/analyst — the analyst's setups read as a signal feed (2026-11-22) ──
#
# Owner: "make analyst like real signals but to ops only, not for users".
# Engine side: ``analyst_walk.feed`` via ``read.analyst_feed``. Every row is a
# setup the validator ACCEPTED, walked dark; none of it reached a subscriber,
# the signal queue or an order. Ops computes nothing about the outcome — state
# and PnL are the engine's ``analyst`` arm. The one thing added here is a live
# mark for rows still running, from the whole-book ticker (one request,
# TTL-cached), and it is kept out of every realised figure.

#: The engine's states, in display order, with what each means. Labels only:
#: a state the engine publishes and this map lacks renders under its raw name,
#: badged ``unclassified``.
FEED_STATE_COPY = {
    "pending": ("Pending", "badge-accent", "Limit not reached yet; still inside its expiry."),
    "active": ("Active", "badge-warn", "Limit filled; walking to TP1 or the stop."),
    "tp_hit": ("TP1 hit", "badge-ok", "Reached the first target before the stop."),
    "sl_hit": ("SL hit", "badge-bad", "Stopped out (stop counts first on an ambiguous bar)."),
    "time_exit": ("Time exit", "badge-muted", "Neither level in 12h; closed at the bar close."),
    "not_filled": ("Not filled", "badge-muted",
                   "The limit was never reached. PnL shown is the MARKET outcome, not a fill."),
    "waiting_data": ("Waiting for bars", "badge-muted", "No 5m bars after the submit yet."),
    "not_walked": ("Not walked yet", "badge-muted", "The 5-minute walk has not reached it."),
    "insufficient": ("Insufficient", "badge-err", "Bars do not cover the window; no verdict."),
}
#: States whose position is still running, so a live mark means something.
LIVE_STATES = ("pending", "active")
#: The engine walks every 5 minutes; a running row not advanced in 3 steps is stale.
FEED_STALE_STEPS = 3


def _signed_pct(side: str, frm: float, to: float) -> float:
    move = (to - frm) / frm * 100.0
    return move if side == "long" else -move


def feed_rows(rows: list, prices: dict, now: float, step_s: float = 300.0) -> list[dict]:
    """Display rows: the engine's row plus label, live mark and freshness."""
    out = []
    for r in rows:
        r = dict(r)
        st = r.get("state") or ""
        label, badge, meaning = FEED_STATE_COPY.get(st, (st, "badge-warn", ""))
        r.update(state_label=label, state_badge=badge, state_meaning=meaning,
                 state_known=st in FEED_STATE_COPY)
        a = r.get("analyst") or {}
        side = r.get("side") or ""
        mark = prices.get(r.get("symbol") or "") if st in LIVE_STATES else None
        r["mark"] = mark
        r["unrealized_pct"] = r["to_entry_pct"] = r["to_stop_pct"] = r["to_tp1_pct"] = None
        entries = r.get("entries") or []
        targets = r.get("targets") or []
        if mark:
            if st == "active" and a.get("entry"):
                r["unrealized_pct"] = round(_signed_pct(side, float(a["entry"]), mark), 3)
            elif st == "pending" and entries:
                # Distance price still has to travel to the limit (positive = away).
                e1 = float(entries[0]["price"])
                r["to_entry_pct"] = round(abs(mark - e1) / mark * 100.0, 3)
            if r.get("stop"):
                r["to_stop_pct"] = round(-_signed_pct(side, mark, float(r["stop"])), 3)
            if targets:
                r["to_tp1_pct"] = round(_signed_pct(side, mark, float(targets[0])), 3)
        upd = a.get("updated_at")
        r["walk_age_s"] = (now - float(upd)) if upd else None
        r["walk_stale"] = bool(st in LIVE_STATES and upd
                               and now - float(upd) > FEED_STALE_STEPS * step_s)
        out.append(r)
    return out


def feed_summary(rows: list[dict]) -> dict:
    """Over the rows on screen. Realised = TP / SL / time exit on a FILL;
    a not-filled row is counted apart, because its PnL is the market's."""
    def net(r):
        return (r.get("analyst") or {}).get("net_pct")
    filled = [r for r in rows if r.get("state") in ("tp_hit", "sl_hit", "time_exit")
              and net(r) is not None]
    nf = [r for r in rows if r.get("state") == "not_filled" and net(r) is not None]
    nets = [net(r) for r in filled]
    return {
        "n_filled_closed": len(filled),
        "wins": sum(x > 0 for x in nets),
        "avg_net_pct": round(sum(nets) / len(nets), 4) if nets else None,
        "total_net_pct": round(sum(nets), 4) if nets else None,
        "n_not_filled": len(nf),
        "not_filled_avg_market_pct": (round(sum(net(r) for r in nf) / len(nf), 4)
                                      if nf else None),
        "n_running": sum(r.get("state") in LIVE_STATES for r in rows),
    }


def _counts(rows: list[dict], key: str) -> dict:
    c: dict = {}
    for r in rows:
        c[r.get(key) or ""] = c.get(r.get(key) or "", 0) + 1
    return c


@router.get("/signals/analyst")
async def analyst_feed_page(request: Request, state: str = "", side: str = ""):
    import time

    api = request.app.state.engine_api
    try:
        raw = await api.diag_run("read.analyst_feed", {"limit": 300})
    except Exception as exc:  # pragma: no cover - defensive
        raw = {"error": f"{type(exc).__name__}: {exc}"}
    feed_state, detail = classify(raw)
    res = (raw.get("result") or {}) if feed_state == STATE_OK else {}
    engine_rows = res.get("rows") or []
    prices: dict = {}
    if any(r.get("state") in LIVE_STATES for r in engine_rows):
        try:
            prices = await request.app.state.binance_klines.fetch_all_prices()
        except Exception:  # pragma: no cover - a missing mark blanks a column
            prices = {}
    now = float(res.get("generated_at") or time.time())
    rows = feed_rows(engine_rows, prices, now, float(res.get("walk_step_s") or 300.0))
    # Every selector's counts with every filter applied EXCEPT its own.
    by_side = [r for r in rows if not side or r.get("side") == side]
    by_state = [r for r in rows if not state or r.get("state") == state]
    shown = [r for r in by_side if not state or r.get("state") == state]
    state_counts = _counts(by_side, "state")
    state_keys = [k for k in FEED_STATE_COPY if k in state_counts] + \
        [k for k in state_counts if k not in FEED_STATE_COPY]
    return request.app.state.templates.TemplateResponse("analyst_feed.html", {
        "request": request, "active": "analyst_feed",
        "feed_state": feed_state, "detail": detail,
        "rows": shown, "summary": feed_summary(shown),
        "state_counts": state_counts, "state_keys": state_keys,
        "state_copy": FEED_STATE_COPY, "side_counts": _counts(by_state, "side"),
        "f_state": state, "f_side": side,
        "n_total": res.get("n_accepted_total"), "n_engine": len(engine_rows),
        "limit": res.get("limit"), "note": res.get("note"),
        "marks_missing": bool(any(r.get("state") in LIVE_STATES for r in engine_rows)
                              and not prices),
        "generated_at": res.get("generated_at"),
    })

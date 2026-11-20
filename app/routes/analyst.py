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
        rows.append({
            "key": k, "label": ARM_LABELS.get(k, k), "known": k in ARM_LABELS,
            "n": a.get("n", 0), "win_rate": a.get("win_rate"),
            "avg_net_pct": a.get("avg_net_pct"), "total_net_pct": a.get("total_net_pct"),
            "trim_avg": trim.get("avg_net_pct"), "trim_dropped": trim.get("dropped"),
            "open": a.get("open", 0), "insufficient": a.get("insufficient", 0),
            "unfilled": a.get("unfilled_scored_at_market", 0),
        })
    return rows


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
        "plans_state": plans_state, "plans_detail": plans_detail,
        "plans": plans.get("plans") or [],
        "totals": plans.get("totals") or {},
        "rejections": plans.get("rejections_by_reason") or {},
    })

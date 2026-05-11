"""PROTECTIVE / PREMATURE / NEUTRAL audit histograms.

Mirrors the invalidation-quality classification the engine produces in
``src/invalidation_audit.py`` — protective = kill saved >0.3R; premature =
kill destroyed a TP1 we would have hit; neutral = price stayed within ±0.3R.
We aggregate by setup × class and by kill-reason × class so the operator can
see which kill-reasons net-help vs net-hurt by path."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()


def _classify(records: Any) -> dict:
    by_setup: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_reason: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    totals: dict[str, int] = defaultdict(int)
    rows: list[dict[str, Any]] = []

    if isinstance(records, dict) and "error" in records:
        return {"by_setup": {}, "by_reason": {}, "totals": {}, "rows": [], "error": records.get("error")}

    if not isinstance(records, list):
        return {"by_setup": {}, "by_reason": {}, "totals": {}, "rows": [], "error": "non-list payload"}

    for r in records:
        if not isinstance(r, dict):
            continue
        cls = r.get("classification") or r.get("classification_label") or "UNCLASSIFIED"
        setup = r.get("setup_class") or "UNKNOWN"
        reason = r.get("kill_reason") or r.get("reason") or "unknown"
        by_setup[setup][cls] += 1
        by_reason[reason][cls] += 1
        totals[cls] += 1
        rows.append(r)

    rows.sort(key=lambda r: r.get("killed_at") or r.get("created_at") or "", reverse=True)

    return {
        "by_setup": {k: dict(v) for k, v in by_setup.items()},
        "by_reason": {k: dict(v) for k, v in by_reason.items()},
        "totals": dict(totals),
        "rows": rows[:200],
        "error": None,
    }


@router.get("/invalidations")
async def invalidations(request: Request):
    vol = request.app.state.data_volume
    records = vol.invalidation_records()
    agg = _classify(records)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "invalidations.html",
        {"request": request, "agg": agg, "active": "invalidations"},
    )

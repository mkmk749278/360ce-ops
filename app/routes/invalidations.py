"""PROTECTIVE / PREMATURE / NEUTRAL audit histograms.

Mirrors the invalidation-quality classification the engine produces in
``src/invalidation_audit.py`` — protective = kill saved >0.3R; premature =
kill destroyed a TP1 we would have hit; neutral = price stayed within ±0.3R.
We aggregate by setup × class and by kill-reason × class so the operator can
see which kill-reasons net-help vs net-hurt by path.

Kill-reason strings embed per-record numbers (``adverse excursion (+0.40%
against, 0.50×SL_dist)``), so a raw group-by produces one row per record and
is useless for spotting a pattern.  ``_reason_family`` collapses each string to
its stable family prefix so the operator sees the handful of mechanisms
(adverse excursion / trailing invalidation / momentum against thesis / regime
shift) with a real premature-rate per family — that family view is the lever
the SR_FLIP premature-kill research (session 22) actually needs."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Request

from app.reports import csv_response

router = APIRouter()


# Classification column order — fixed so every table + export reads the same
# regardless of which classes happened to appear in the window.
_CLASS_ORDER = ["PROTECTIVE", "PREMATURE", "NEUTRAL"]

# Known kill-reason family prefixes.  A reason string is assigned to the first
# family whose prefix it starts with; anything unmatched falls back to the text
# before its first delimiter.  Keep this list in sync with the kill-reason
# strings emitted by ``src/invalidation_audit.py`` in the engine.
_REASON_FAMILIES = [
    "adverse excursion",
    "trailing invalidation",
    "momentum against thesis",
    "regime shift",
    "structure break",
    "stop loss",
    "take profit",
    "manual",
]


def _reason_family(reason: str) -> str:
    """Collapse a per-record kill-reason string to its stable family.

    ``adverse excursion (+0.40% against, 0.50×SL_dist) — early invalidation``
    → ``adverse excursion``.  Unknown shapes fall back to the text before the
    first ``(`` / em-dash / hyphen so the family view never silently drops a
    record into an opaque bucket."""
    r = (reason or "").strip().lower()
    if not r:
        return "unknown"
    for fam in _REASON_FAMILIES:
        if r.startswith(fam):
            return fam
    for sep in ("(", "—", " - "):
        idx = r.find(sep)
        if idx > 0:
            return r[:idx].strip() or "unknown"
    return r


def _premature_rate(counts: dict[str, int]) -> float:
    """PREMATURE / (PROTECTIVE + PREMATURE + NEUTRAL).  Zero when the bucket is
    empty.  This is the headline health number per family/setup — the fraction
    of kills that destroyed a position that would have reached TP1."""
    total = sum(counts.get(c, 0) for c in _CLASS_ORDER)
    return counts.get("PREMATURE", 0) / total if total else 0.0


def _classify(records: Any) -> dict:
    by_setup: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_reason: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_family: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    totals: dict[str, int] = defaultdict(int)
    rows: list[dict[str, Any]] = []

    if isinstance(records, dict) and "error" in records:
        return {
            "by_setup": {}, "by_reason": {}, "by_family": {}, "totals": {},
            "rows": [], "error": records.get("error"),
        }

    if not isinstance(records, list):
        return {
            "by_setup": {}, "by_reason": {}, "by_family": {}, "totals": {},
            "rows": [], "error": "non-list payload",
        }

    for r in records:
        if not isinstance(r, dict):
            continue
        cls = r.get("classification") or r.get("classification_label") or "UNCLASSIFIED"
        setup = r.get("setup_class") or "UNKNOWN"
        reason = r.get("kill_reason") or r.get("reason") or "unknown"
        by_setup[setup][cls] += 1
        by_reason[reason][cls] += 1
        by_family[_reason_family(reason)][cls] += 1
        totals[cls] += 1
        rows.append(r)

    rows.sort(key=lambda r: r.get("killed_at") or r.get("created_at") or "", reverse=True)

    # Classes actually present, in fixed order, plus any stragglers appended.
    classes = [c for c in _CLASS_ORDER if c in totals]
    classes += [c for c in totals if c not in classes]

    def _finalize(d: dict[str, dict[str, int]]) -> list[dict]:
        out: list[dict] = []
        for key, counts in d.items():
            total = sum(counts.values())
            out.append({
                "key": key,
                "counts": dict(counts),
                "total": total,
                "premature_rate": _premature_rate(counts),
            })
        # Highest premature-rate first (then volume) so the worst offenders
        # surface at the top — that ordering is the whole point of the view.
        out.sort(key=lambda r: (r["premature_rate"], r["total"]), reverse=True)
        return out

    return {
        "classes": classes,
        "by_setup": _finalize(by_setup),
        "by_family": _finalize(by_family),
        "by_reason": _finalize(by_reason),
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


@router.get("/invalidations/export.csv")
async def invalidations_export(request: Request, view: str = "family"):
    """Download the audit as CSV.  ``view`` selects which reduction:
    ``family`` (default), ``setup``, or ``reason`` (raw per-string)."""
    vol = request.app.state.data_volume
    agg = _classify(vol.invalidation_records())
    classes = agg.get("classes") or _CLASS_ORDER

    table_key = {"setup": "by_setup", "reason": "by_reason"}.get(view, "by_family")
    label = {"by_setup": "setup", "by_reason": "kill_reason"}.get(table_key, "reason_family")
    table = agg.get(table_key, [])

    header = [label, *classes, "total", "premature_rate"]
    rows = [
        [
            r["key"],
            *[r["counts"].get(c, 0) for c in classes],
            r["total"],
            "%.4f" % r["premature_rate"],
        ]
        for r in table
    ]
    return csv_response(f"invalidations_{label}", header, rows)

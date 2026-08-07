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


#: How long an empty audit may sit unwritten before "quiet" stops being a
#: defensible reading. The engine rewrites this artifact on its audit cycle, so
#: a day of silence is not a slow market — it is a writer that has stopped.
STALE_WRITER_SEC = 24 * 3600


def _blank_cause(records: Any, provenance: Any = None) -> dict:
    """Why this page is empty — because "blank needs a cause before it gets a
    caption", and the caption here was a bare "No invalidation records loaded".

    Four states with four different next moves. The artifact is **missing** (the
    engine has not written it — wait, or check the engine is up), it is
    **unreadable** (a real fault, ours), it is **present, empty and fresh** (no
    signal has been invalidated recently — the quiet case, and not a fault), or
    it is **present, empty and stale**.

    That fourth state is the one this function shipped without, and the cost was
    the exact defect the docstring above was written to prevent, arriving from
    the caption's side. On 2026-08-07 ``invalidation_records.json`` was **2
    bytes, last written 22 days earlier**, over a book that had closed 1,043
    signals and whose ``/raw-edge`` exit mix independently read **0%
    invalidation**. This page said: *"no signal has been invalidated in the
    window. This is the quiet case, not a fault."* Both sentences were false,
    and the second sent the reader away from the writer.

    An empty file cannot describe itself — *nothing happened* and *the writer
    stopped* are byte-identical — so the mtime is not decoration here, it is the
    only thing that separates the two states. When it is unavailable the state is
    ``empty_unknown``: an unknown is not a pass.
    """
    if isinstance(records, dict) and records.get("error"):
        detail = str(records.get("error"))
        low = detail.lower()
        # `DataVolumeReader._load` reports an absent file as ``"missing: <path>"``
        # and this check matched neither of its own producer's words, so a file
        # the engine had simply never written rendered under UNREADABLE — "a
        # fault on our side, not a quiet market" — which is the opposite of the
        # truth and the opposite next move. The "not found" / "no such file"
        # variants stay for an OSError message that phrases it differently.
        missing = low.startswith("missing:") or "not found" in low or "no such file" in low
        return {
            "state": "missing" if missing else "unreadable",
            "detail": detail,
        }
    # Note there is no "records present but unclassified" state here, and there
    # must not be: `_classify` buckets a verdict-less row under UNCLASSIFIED and
    # renders it, so `agg["totals"]` is truthy and the caller never reaches this
    # function. A branch for it would be a caption nothing can display — which
    # is the defect class this whole function exists to close, so it is worth
    # saying out loud rather than adding one on the assumption it is needed.
    prov = provenance if isinstance(provenance, dict) else {}
    age = prov.get("age_sec")
    if age is None:
        return {"state": "empty_unknown", "detail": "", "age_sec": None,
                "modified_at": prov.get("modified_at")}
    if float(age) > STALE_WRITER_SEC:
        return {
            "state": "empty_stale",
            "detail": "",
            "age_sec": float(age),
            "age_days": float(age) / 86400.0,
            "modified_at": prov.get("modified_at"),
            "bound_hours": STALE_WRITER_SEC // 3600,
        }
    return {"state": "empty", "detail": "", "age_sec": float(age),
            "modified_at": prov.get("modified_at")}


@router.get("/invalidations")
async def invalidations(request: Request):
    vol = request.app.state.data_volume
    records = vol.invalidation_records()
    agg = _classify(records)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "invalidations.html",
        {
            "request": request,
            "agg": agg,
            "active": "invalidations",
            "blank": (
                _blank_cause(records, vol.artifact_age("invalidation_records.json"))
                if not agg.get("totals") else None
            ),
        },
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


@router.get("/invalidations/export.json")
async def invalidations_export_json(request: Request):
    """Full PROTECTIVE/PREMATURE/NEUTRAL classification aggregation as JSON."""
    from app.reports import json_response

    agg = _classify(request.app.state.data_volume.invalidation_records())
    return json_response("invalidations", agg)

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
from typing import Any, Optional

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


#: Terminal outcome the engine stamps on a signal the invalidation kill-switch
#: closed. It is the ONLY thing that puts a row in this ledger via the
#: trade-monitor path, so it is the population that would be harmed by a broken
#: writer — and therefore the thing to key the fault check on (#815).
INVALIDATED_OUTCOME = "INVALIDATED"


def _invalidated_closes(performance: Any) -> Optional[int]:
    """How many closed signals the engine actually killed on thesis invalidation.

    ``None`` when the closed-signal record could not be read — an unknown, which
    must not be graded as either state.
    """
    if not isinstance(performance, list):
        return None
    total = 0
    for rec in performance:
        if not isinstance(rec, dict):
            continue
        label = str(rec.get("outcome_label") or rec.get("status") or "").upper()
        if label == INVALIDATED_OUTCOME:
            total += 1
    return total


def _blank_cause(
    records: Any,
    provenance: Any = None,
    invalidated_closes: Optional[int] = None,
) -> dict:
    """Why this page is empty — because "blank needs a cause before it gets a
    caption", and the caption here was a bare "No invalidation records loaded".

    Five states with five different next moves. The artifact is **missing** (the
    engine has not written it), **unreadable** (a real fault, ours), or present
    and empty — and that last case splits three ways on whether a row was ever
    OWED, never on how old the file is.

    **The mtime is not the discriminator, and grading on it shipped a page that
    was wrong in the opposite direction.** The first cut of this fix saw a 2-byte
    file last written 22 days earlier over a book of 1,043 closed signals and
    badged it WRITER STALE — *"``invalidation_audit`` has stopped recording, and
    every kill classification since that write is lost"*. The owner corrected it
    the same day: **invalidation and pre-TP are per-user settings, not
    engine-wide** (OWNER_BRIEF B17; ``user_invalidation_settings``). If no user
    has invalidation enabled, no kill ever fires, no row is ever written, and an
    empty 22-day-old file is exactly correct. That is the ``/alerts`` trap from
    2026-08-06 arriving with the sign flipped — replacing a wrong benign caption
    with a wrong alarming one is the same defect, and an alarming one sends the
    owner to debug a subsystem that is working.

    Worse, the fix cited ``/raw-edge``'s 0% invalidation share as independent
    corroboration. **It is not independent**: that bucket is derived from the
    same terminal ``outcome_label``, so it was one fact read twice and presented
    as two.

    What ops *can* observe is whether a row was **owed** — the closed-signal
    record stamps ``INVALIDATED`` on exactly the signals the trade-monitor path
    writes a record for. So the fault check keys on the population that would be
    harmed, not on a clock:

    * ``invalidated_closes == 0`` → nothing was owed. Empty is correct at any
      age, and the page says it cannot see per-user settings rather than
      claiming the feature is off.
    * ``invalidated_closes > 0`` with an empty ledger → rows were owed and are
      not there. **That** is a writer fault, and it is one regardless of mtime.
    * ``None`` → the closed-signal record was unreadable; an unknown, graded as
      neither.

    Note the expiry path (``main.py`` ``cleanup_expired``) also writes here, but
    only as the fallback when the trade monitor did not win the close race — so
    291 EXPIRED closes do **not** imply rows are owed, and this check does not
    count them.
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
    common = {
        "detail": "",
        "age_sec": float(age) if age is not None else None,
        "age_days": (float(age) / 86400.0) if age is not None else None,
        "modified_at": prov.get("modified_at"),
        "invalidated_closes": invalidated_closes,
    }
    if invalidated_closes is None:
        # Cannot read the closed-signal record, so cannot tell whether a row was
        # owed. Neither state is claimed.
        return {"state": "owed_unknown", **common}
    if invalidated_closes > 0:
        # Rows were owed and the ledger is empty. A real fault, at any age.
        return {"state": "writer_fault", **common}
    return {"state": "none_owed", **common}


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
                _blank_cause(
                    records,
                    vol.artifact_age("invalidation_records.json"),
                    _invalidated_closes(vol.signal_performance()),
                )
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

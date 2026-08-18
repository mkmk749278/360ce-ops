"""Runtime truth-report viewer.

Fetches the structured ``truth_snapshot.json`` + window comparison from the
monitor-logs branch and shapes the operator-critical sections (executive
summary, feature liveness, confidence-gate decisions, headline health) into
readable cards/tables.  Every *other* section is kept verbatim but collapsed
into an expandable raw-JSON block instead of a wall of always-open text — so
the page is scannable at a glance and nothing is lost.  Raw markdown / JSON
downloads stay for full-fidelity offline analysis.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

router = APIRouter()

# Sections rendered by dedicated readable views — excluded from the raw list.
_RENDERED = {
    "executive_summary",
    "feature_liveness",
    "confidence_gate_decisions",
    "runtime_health",
    "recommended_operator_focus",
}


def _as_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


#: Used ONLY when the report predates the publication stamp (2026-08-18). The
#: producer now declares its own cadence and this page reads that; see
#: ``report_provenance``.
#:
#: What this constant used to be is the whole lesson. It was 3600 — an hour —
#: on an artifact published **once a day** by a scheduled GitHub Action, so this
#: page read STALE for ~23 hours out of every 24 while the workflow's last six
#: runs had all succeeded. And the caption went further than the badge, naming a
#: cause the page cannot observe: *"the engine has not published a newer
#: report"*. The engine does not publish it at all.
#:
#: An alarming caption over a healthy subsystem is worse than a blank, because
#: it sends the owner to debug something that works. This repo paid for exactly
#: that on ``/invalidations`` (2026-08-07, WRITER STALE) and it recurred one
#: surface over.
TRUTH_FALLBACK_STALE_SEC = 24 * 3600 + 4 * 3600

#: Retained under its old name for any caller still importing it; the value is
#: the fallback, not an hour.
TRUTH_STALE_SEC = TRUTH_FALLBACK_STALE_SEC


def report_provenance(snapshot: Any, now_ts: float | None = None) -> dict:
    """When this report was published, on whose schedule, and how far back it looks.

    This page rendered **no timestamp at all** until 2026-08-07, while serving a
    TTL-cached snapshot of the ``monitor-logs`` branch. On that day its
    ``cohort_edge_gate`` row read ``streak 85`` beside a live ``/`` pulse reading
    ``streak 156`` for the same probe, and nothing on either page told the reader
    they were on different clocks — so the two surfaces silently disagreed about
    the state of a live gate.

    The clock is the report's own ``generated_at``, never ops' own: a surface may
    not grade its own freshness on a clock it supplies (the rule
    ``/signals/sar-live`` already carries, and the one this page was missing
    entirely). ``lookback_hours`` rides along because the counters here are
    **cumulative over that window** — a just-shipped change is invisible in them
    however fresh the report is, which is a second, independent reason a number
    here can disagree with a live panel.

    **And the BOUND is the producer's too, from 2026-08-18.** Freshness needs a
    cadence, and a reader holding its own copy of the cadence drifts from it the
    first time the schedule changes — so the snapshot carries a ``publication``
    block (360-v2 ``runtime_truth_report._publication_contract``) naming the
    interval, the grace and the publisher, and this function reads it. Ops
    inventing the number is what produced a one-hour bound on a daily artifact.

    A report written before that stamp existed falls back to a named default and
    says so, exactly as ``MECHANISM_FALLBACK`` and ``FALLBACK_SPEC`` do: a silent
    fallback is a mirror nobody knows is a mirror.
    """
    out: dict[str, Any] = {
        "generated_at": None,
        "generated_at_iso": None,
        "age_sec": None,
        "stale": None,
        "lookback_hours": None,
        "bound_sec": TRUTH_FALLBACK_STALE_SEC,
        # Named so the page can say WHOSE schedule it is being graded against,
        # and can send an overdue reader to the workflow rather than the engine.
        "publisher": None,
        "schedule": None,
        "interval_sec": None,
        "bound_from_producer": False,
        "next_due_sec": None,
    }
    if not isinstance(snapshot, dict) or snapshot.get("error"):
        return out
    try:
        lookback = snapshot.get("lookback_hours")
        out["lookback_hours"] = float(lookback) if lookback is not None else None
    except (TypeError, ValueError):
        pass
    try:
        generated = float(snapshot.get("generated_at") or 0.0)
    except (TypeError, ValueError):
        return out
    if generated <= 0:
        return out
    # The producer's own contract, when the report carries one.
    publication = snapshot.get("publication")
    if isinstance(publication, dict):
        try:
            bound = float(publication.get("stale_after_sec") or 0.0)
        except (TypeError, ValueError):
            bound = 0.0
        if bound > 0:
            out["bound_sec"] = bound
            out["bound_from_producer"] = True
        try:
            interval = float(publication.get("interval_sec") or 0.0)
            out["interval_sec"] = interval or None
        except (TypeError, ValueError):
            pass
        out["publisher"] = publication.get("publisher") or None
        out["schedule"] = publication.get("schedule") or None

    now = now_ts if now_ts is not None else time.time()
    out["generated_at"] = generated
    out["generated_at_iso"] = datetime.fromtimestamp(
        generated, tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")
    out["age_sec"] = max(0.0, now - generated)
    out["stale"] = out["age_sec"] > out["bound_sec"]
    # When the next one is due, so a fresh report reads as "on schedule" rather
    # than as a number the reader has to compare against a cron in their head.
    if out["interval_sec"]:
        out["next_due_sec"] = max(0.0, out["interval_sec"] - out["age_sec"])
    return out


def _shape(snapshot: Any) -> dict:
    """Split the snapshot into readable view-models + collapsible raw sections."""
    out: dict[str, Any] = {
        "top_error": None,
        "exec": None,
        "health": None,
        "focus": None,
        "liveness": None,
        "liveness_bad": 0,
        "gate_rows": None,
        "raw_sections": [],
    }
    if not isinstance(snapshot, dict):
        out["raw_sections"] = [{"key": "snapshot", "title": "Snapshot", "value": snapshot}]
        return out
    if snapshot.get("error"):
        out["top_error"] = str(snapshot["error"])
        return out

    ex = snapshot.get("executive_summary")
    if isinstance(ex, dict):
        out["exec"] = ex

    rh = snapshot.get("runtime_health")
    if isinstance(rh, dict):
        out["health"] = rh

    rf = snapshot.get("recommended_operator_focus")
    if rf:
        out["focus"] = rf

    fl = snapshot.get("feature_liveness")
    feats = fl.get("features") if isinstance(fl, dict) else None
    if isinstance(feats, dict):
        rows = []
        bad = 0
        for name, v in sorted(feats.items()):
            if not isinstance(v, dict):
                continue
            status = str(v.get("status", "?"))
            if status.lower() not in ("ok", "healthy", "warmup", "disabled"):
                bad += 1
            rows.append(
                {
                    "name": name,
                    "status": status,
                    "detail": str(v.get("detail", "")),
                    "streak": v.get("streak"),
                }
            )
        # Unhealthy first, then by name.
        rows.sort(
            key=lambda r: (r["status"].lower() in ("ok", "healthy", "disabled"), r["name"])
        )
        out["liveness"] = rows
        out["liveness_bad"] = bad

    gd = snapshot.get("confidence_gate_decisions")
    if isinstance(gd, dict):
        grows = []
        for setup, d in gd.items():
            kept_map = d.get("kept") if isinstance(d, dict) else None
            filt_map = d.get("filtered") if isinstance(d, dict) else None
            kept = sum(_as_int(x) for x in kept_map.values()) if isinstance(kept_map, dict) else 0
            filt = filt_map if isinstance(filt_map, dict) else {}
            filt_total = sum(_as_int(x) for x in filt.values())
            total = kept + filt_total
            grows.append(
                {
                    "setup": setup,
                    "kept": kept,
                    "filtered_total": filt_total,
                    "total": total,
                    "pass_pct": (100.0 * kept / total) if total else 0.0,
                    "filtered": dict(sorted(filt.items(), key=lambda kv: -_as_int(kv[1]))),
                }
            )
        grows.sort(key=lambda r: r["total"], reverse=True)
        out["gate_rows"] = grows

    for k, v in snapshot.items():
        if k in _RENDERED:
            continue
        out["raw_sections"].append(
            {"key": k, "title": str(k).replace("_", " ").title(), "value": v}
        )
    return out


@router.get("/truth")
async def truth(request: Request):
    logs = request.app.state.monitor_logs
    snapshot = await logs.truth_snapshot()
    comparison = await logs.window_comparison()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "truth.html",
        {
            "request": request,
            "snapshot": snapshot,
            "shaped": _shape(snapshot),
            "provenance": report_provenance(snapshot),
            "comparison": comparison,
            "active": "truth",
        },
    )


@router.get("/truth/raw.md", response_class=PlainTextResponse)
async def truth_raw_md(request: Request):
    logs = request.app.state.monitor_logs
    return await logs.truth_markdown()


@router.get("/truth/raw.json")
async def truth_raw_json(request: Request):
    logs = request.app.state.monitor_logs
    return JSONResponse(await logs.truth_snapshot())

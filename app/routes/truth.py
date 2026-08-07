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


#: Beyond this, the snapshot is describing a window the reader is no longer in.
#: The engine regenerates the report on its monitor cycle, so an hour is slack,
#: not a schedule.
TRUTH_STALE_SEC = 3600


def report_provenance(snapshot: Any, now_ts: float | None = None) -> dict:
    """When the ENGINE generated this report, and how far back it looks.

    This page rendered **no timestamp at all** until 2026-08-07, while serving a
    TTL-cached snapshot of the ``monitor-logs`` branch. On that day its
    ``cohort_edge_gate`` row read ``streak 85`` beside a live ``/`` pulse reading
    ``streak 156`` for the same probe, and nothing on either page told the reader
    they were on different clocks — so the two surfaces silently disagreed about
    the state of a live gate.

    The clock is the engine's ``generated_at``, never ops' own: a surface may not
    grade its own freshness on a clock it supplies (the rule ``/signals/sar-live``
    already carries, and the one this page was missing entirely). ``lookback_hours``
    rides along because the counters here are **cumulative over that window** —
    a just-shipped change is invisible in them however fresh the report is, which
    is a second, independent reason a number here can disagree with a live panel.
    """
    out: dict[str, Any] = {
        "generated_at": None,
        "generated_at_iso": None,
        "age_sec": None,
        "stale": None,
        "lookback_hours": None,
        "bound_sec": TRUTH_STALE_SEC,
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
    now = now_ts if now_ts is not None else time.time()
    out["generated_at"] = generated
    out["generated_at_iso"] = datetime.fromtimestamp(
        generated, tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")
    out["age_sec"] = max(0.0, now - generated)
    out["stale"] = out["age_sec"] > TRUTH_STALE_SEC
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

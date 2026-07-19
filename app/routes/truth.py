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

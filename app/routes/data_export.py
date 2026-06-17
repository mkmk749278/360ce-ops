"""Data Export — download the engine's raw ``data/*.json`` artifacts.

The analytics pages reduce the engine JSON into operator tables, but some
offline analysis needs the *raw* per-record data. In particular
``signal_history.json`` is a ``vars(sig)`` dump that carries each signal's
``component_scores`` (the 7 scoring dimensions) alongside its outcome — the
exact pairing needed to calibrate the confidence score, which no table exposes.

This page lists the mounted ``/engine-data`` artifacts and serves each as a
timestamped JSON download. Read-only by construction (it only re-serializes
what ``DataVolumeReader`` already loaded); owner-gated by the global auth
middleware like every other route.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import PlainTextResponse

from app.reports import json_response

router = APIRouter()

# Whitelist: export key -> (DataVolumeReader method, label, why it's useful).
# The key doubles as the download filename stem; whitelisting it also prevents
# any path-traversal into the data volume.
_EXPORTS: dict[str, tuple[str, str, str]] = {
    "signal_history": (
        "signal_history",
        "Signal history",
        "raw vars(sig) dump — per-signal component_scores (7 scoring dimensions) "
        "+ outcomes. Source for offline score-calibration.",
    ),
    "signal_performance": (
        "signal_performance",
        "Signal performance",
        "aggregated per-signal performance records.",
    ),
    "invalidation_records": (
        "invalidation_records",
        "Invalidation records",
        "trade-monitor kill classifications (PROTECTIVE / PREMATURE / NEUTRAL).",
    ),
}


def _summarize(payload: object) -> tuple[int | None, str | None]:
    """Return (record_count, error) for the index table."""
    if isinstance(payload, dict) and "error" in payload:
        return None, str(payload["error"])
    if hasattr(payload, "__len__"):
        return len(payload), None  # type: ignore[arg-type]
    return None, None


@router.get("/data")
async def data_index(request: Request):
    dv = request.app.state.data_volume
    rows = []
    for key, (method, label, why) in _EXPORTS.items():
        count, error = _summarize(getattr(dv, method)())
        rows.append(
            {"key": key, "label": label, "why": why, "count": count, "error": error}
        )
    return request.app.state.templates.TemplateResponse(
        "data_export.html",
        {"request": request, "active": "data", "rows": rows},
    )


@router.get("/data/download/{name}")
async def data_download(name: str, request: Request):
    spec = _EXPORTS.get(name)
    if spec is None:
        return PlainTextResponse(f"unknown export: {name}", status_code=404)
    payload = getattr(request.app.state.data_volume, spec[0])()
    return json_response(name, payload)

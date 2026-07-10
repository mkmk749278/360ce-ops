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

import time

from fastapi import APIRouter, Request
from starlette.responses import FileResponse, PlainTextResponse

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


def _fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.0f} B"


def _fmt_age(mtime: float, now: float | None = None) -> str:
    secs = max(0, int((now or time.time()) - mtime))
    if secs < 90:
        return f"{secs}s ago"
    if secs < 5400:
        return f"{secs // 60}m ago"
    if secs < 48 * 3600:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


@router.get("/data")
async def data_index(request: Request):
    dv = request.app.state.data_volume
    rows = []
    for key, (method, label, why) in _EXPORTS.items():
        count, error = _summarize(getattr(dv, method)())
        rows.append(
            {"key": key, "label": label, "why": why, "count": count, "error": error}
        )
    # Raw-file browser: everything on the volume, newest write first — the
    # write recency doubles as a liveness readout (a status file that should
    # tick every 30s but shows "3d ago" is itself a finding).
    files = dv.list_files()
    now = time.time()
    for f in files:
        f["size_h"] = _fmt_size(f["size_bytes"])
        f["age_h"] = _fmt_age(f["mtime"], now)
        f["age_class"] = (
            "pos" if now - f["mtime"] < 3600
            else ("warn" if now - f["mtime"] < 86400 else "muted")
        )
    return request.app.state.templates.TemplateResponse(
        "data_export.html",
        {"request": request, "active": "data", "rows": rows, "files": files},
    )


@router.get("/data/download/{name}")
async def data_download(name: str, request: Request):
    spec = _EXPORTS.get(name)
    if spec is None:
        return PlainTextResponse(f"unknown export: {name}", status_code=404)
    payload = getattr(request.app.state.data_volume, spec[0])()
    return json_response(name, payload)


@router.get("/data/raw/{rel_path:path}")
async def data_raw_download(rel_path: str, request: Request):
    """Serve any file on the read-only volume as a download.

    ``resolve_safe`` is the security boundary: symlinks and ``..`` are
    resolved BEFORE the containment check, so only files physically inside
    the volume mount are reachable — the route cannot be walked out of.
    Owner-gated by the global auth middleware like every other route.
    """
    path = request.app.state.data_volume.resolve_safe(rel_path)
    if path is None:
        return PlainTextResponse(f"not found: {rel_path}", status_code=404)
    return FileResponse(
        path, media_type="application/octet-stream", filename=path.name
    )

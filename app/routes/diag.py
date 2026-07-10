"""On-demand diag-script runner."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request

router = APIRouter()


@router.get("/diag/geometry")
async def diag_geometry_get(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "diag.html",
        {"request": request, "result": None, "params": {}, "active": "diag"},
    )


@router.post("/diag/geometry")
async def diag_geometry_post(
    request: Request,
    limit: int = Form(50),
    setup_class: str = Form(""),
):
    limit = max(1, min(limit, 500))
    args: list[str] = ["--limit", str(limit)]
    if setup_class:
        args.extend(["--path", setup_class])
    runner = request.app.state.diag_runner
    result = await runner.run("diag_geometry_vs_reality", args)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "diag.html",
        {
            "request": request,
            "result": result,
            "params": {"limit": limit, "setup_class": setup_class},
            "active": "diag",
        },
    )


@router.post("/diag/paper")
async def diag_paper_post(
    request: Request,
    hours: int = Form(30),
):
    """Paper-health evidence dump (engine scripts/diag_paper_health.py) —
    joins the paper cohort, per-user books, and recent signals to show which
    gate is eating paper opens.  Added for the 2026-07-10 frozen-paper-book
    investigation; read-only on the engine side."""
    hours = max(1, min(hours, 720))
    runner = request.app.state.diag_runner
    result = await runner.run("diag_paper_health", ["--hours", str(hours)])
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "diag.html",
        {
            "request": request,
            "result": result,
            "params": {"hours": hours},
            "active": "diag",
        },
    )

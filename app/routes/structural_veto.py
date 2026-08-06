"""`/signals/structural-veto` — does a level ahead of the trade predict anything?

Added 2026-08-05 alongside engine ``src/structural_veto.py`` (price-action
program, Phase 4). See ``app/data_sources/structural_veto.py`` for the rules the
page holds to.

**Route ordering matters here and has been paid for once already.**
``signal_detail`` registers ``/signals/{signal_id}``, which matches any
``/signals/<literal>``. A page included *after* it 404s while its own route
object sits in ``app.routes`` looking perfectly registered — the route list is
not the authority, the request is. ``tests/test_structural_veto_page.py``
asserts the ordering in ``app/main.py`` as well as the live request.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.data_sources.structural_veto import build_report

router = APIRouter()


@router.get("/signals/structural-veto")
async def structural_veto_page(request: Request, setup: str = ""):
    dv = request.app.state.data_volume
    error = ""
    ledger: object = None
    performance: object = None
    try:
        ledger = dv.structural_veto()
        performance = dv.signal_performance()
    except Exception as exc:  # noqa: BLE001
        # Named cause. A blank page here reads exactly like a lane that has
        # stamped nothing, which is the one conclusion this page must never let
        # a reader reach by accident.
        error = f"veto ledger unreadable: {type(exc).__name__}: {exc}"

    report = build_report(ledger, performance, setup_class=setup)
    if error:
        report.error = error

    return request.app.state.templates.TemplateResponse(
        "structural_veto.html",
        {
            "request": request,
            "active": "structural_veto",
            "report": report,
            "setup": setup,
        },
    )

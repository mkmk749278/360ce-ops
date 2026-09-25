"""`/signals/unlock-shorts` — the unlock-short dark lane.

Engine ``src/unlock_shorts.py``; reducer ``app/data_sources/unlock_shorts.py``.
Measurement only: nothing the page shows reached a subscriber or an order.

**Route ordering, paid for once already:** ``signal_detail`` owns
``/signals/{signal_id}``, which matches any ``/signals/<literal>``. This router
is included BEFORE it in ``app/main.py``; the route list is not the authority,
the request is, and ``tests/test_unlock_shorts_page.py`` drives the request.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.data_sources.unlock_shorts import BUCKETS, VARIANTS, build_report, export_csv

router = APIRouter()


@router.get("/signals/unlock-shorts")
async def unlock_shorts_page(request: Request):
    dv = request.app.state.data_volume
    try:
        ledger = dv.unlock_shorts()
    except Exception as exc:  # noqa: BLE001 — named, never blank
        ledger = {"error": f"read: {type(exc).__name__}: {exc}"}
    prices: dict = {}
    has_open = isinstance(ledger, dict) and any(
        isinstance(r, dict) and r.get("status") == "OPEN" for r in ledger.get("rows") or []
    )
    if has_open:
        # One request marks the whole book (weight 2, TTL-cached) — never one
        # per row, so the page's cost does not scale with open trades.
        try:
            prices = await request.app.state.binance_klines.fetch_all_prices()
        except Exception:  # noqa: BLE001 — a missing mark blanks a column only
            prices = {}
    report = build_report(ledger, now=time.time(), prices=prices)
    return request.app.state.templates.TemplateResponse(
        "unlock_shorts.html",
        {
            "request": request,
            "active": "unlock_shorts",
            "report": report,
            "variants": VARIANTS,
            "buckets": BUCKETS,
            "has_open": has_open,
        },
    )


@router.get("/signals/unlock-shorts/export.csv")
async def unlock_shorts_export(request: Request):
    """Every row, uncapped — the page's own loader, so the download can never
    describe a different book than the screen."""
    try:
        ledger = request.app.state.data_volume.unlock_shorts()
    except Exception:  # noqa: BLE001
        ledger = {}
    return Response(
        content=export_csv(ledger),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="unlock_shorts.csv"'},
    )

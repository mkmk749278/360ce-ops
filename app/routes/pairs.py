"""Pairs: the scanned universe (Regular) + live mover-promoted pairs (Promoting).

Diagnostic for "are the mover-ignition promotions actually updating?" — the
Promoting sub-tab lists the engine's in-memory ``_mover_promoted_pairs`` with
cycles-remaining and an ``updated_at`` stamp, so a stale/empty list is obvious.
Read-only consumer of the engine's owner-gated ``GET /api/pairs``.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()

_TABS = ("promoting", "regular")


@router.get("/pairs")
async def pairs_page(request: Request, tab: str = "promoting"):
    api = request.app.state.engine_api
    templates = request.app.state.templates
    tab = tab if tab in _TABS else "promoting"

    data = await api.pairs()
    if not isinstance(data, dict):
        data = {}
    error = data.get("error") if isinstance(data, dict) else None

    return templates.TemplateResponse(
        "pairs.html",
        {
            "request": request,
            "active": "pairs",
            "tab": tab,
            "regular": data.get("regular") or [],
            "promoting": data.get("promoting") or [],
            "regular_count": data.get("regular_count", len(data.get("regular") or [])),
            "promoting_count": data.get("promoting_count", len(data.get("promoting") or [])),
            "updated_at": data.get("updated_at"),
            "ignition": data.get("ignition") or {},
            # Dynamic retention (engine `src/mover_retention.py`). Read off the
            # engine's own report, never recomputed here — a second scorer in
            # the display layer would be a mirror, and the fix for a drifting
            # mirror is not a second mirror.
            #
            # `None` (absent) and `{}` (present but empty) are DIFFERENT and the
            # template must not pool them: absent means the engine predates the
            # lane, empty means it is running and holds nothing. Different
            # causes, different next moves — "blank needs a cause before it gets
            # a caption".
            "retention": data.get("retention"),
            # Dual universe (engine `Scanner.mover_universe_role`). A pair can
            # be in BOTH lists — core by volume AND currently promoted — and
            # until 2026-08-22 the Regular tab hid every promoted symbol, so
            # the owner was reading a regular universe roughly half its real
            # size behind a tab that said "Regular (167)".
            #
            # `None` (absent) is an engine predating the census and is NOT
            # `{}` — the template must be able to say "this engine does not
            # report it" rather than "nothing is dual", which is a claim.
            "dual_universe": data.get("dual_universe"),
            "dual_count": data.get("dual_count"),
            "error": error,
        },
    )

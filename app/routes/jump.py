"""``/_partial/jump-index`` — the Ctrl+K palette's route list.

It is the nav itself, read from the compiled ``_nav.html`` (one list, shared
with base.html), filtered for the session with the same set the gate enforces
(``GUEST_READ_ROUTES``). So the palette cannot offer a guest a page that would
403, and cannot drift from the nav.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import guest_scope

router = APIRouter()


@router.get("/_partial/jump-index")
async def jump_index(request: Request):
    nav = request.app.state.templates.env.get_template("_nav.html").module.NAV
    guest = request.scope.get("ops_role") == "guest"
    out = []
    for gkey, glabel, subs in nav:
        vis = [list(s) for s in subs if not guest or s[0] in guest_scope.GUEST_READ_ROUTES]
        if vis:
            out.append([gkey, glabel, vis])
    return JSONResponse(out, headers={"Cache-Control": "private, no-store"})

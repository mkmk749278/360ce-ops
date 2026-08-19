"""The diagnostic console — named engine diagnostics, runnable from here.

Owner, 2026-08-19: *"what amount send commands to vps from ops, then you can
directly interact with engine send commands accordingly fix problems"*, scoped
to a catalog plus a few safe actions, usable inside a read-only guest session.

**This page sends a catalog KEY, never a command.** It has no field for a
command, a path or a shell fragment, and adding one would defeat the whole
design. What each key may do is decided by `360-v2/src/diag_catalog.py` — which
refuses unknown keys and is asserted there, per entry and by AST, to reach no
order, no secret and no kill switch. Ops cannot widen that from here, and this
module deliberately holds no list of its own: the catalog arrives as data, so an
entry ops has never heard of still renders under the engine's own label.

**Why a guest may POST here at all.** `guest_scope.GUEST_ACTION_ROUTES` names
this one route with a written reason. That list was a narrowing of an invariant
that used to read "GET and HEAD, nothing else, ever" — narrowed rather than
deleted, because an invariant that blocks correct work gets deleted outright by
whoever needs the work, while one that states what it means survives.

**The action half is switchable engine-side** (`DIAG_ACTIONS_ENABLED`), enforced
where entries run rather than only where they render, so this grant is revocable
without touching this repo.
"""
from __future__ import annotations

import secrets
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import audit

router = APIRouter()

#: Where the result of a run is parked between the POST and the redirect.
#: PRG, like every other action surface here: a refresh must not re-fire a run.
_FLASH_KEY = "diag_console_result_id"

#: Results live HERE, not in the session cookie.
#:
#: The first cut stored the payload in `request.session`, which Starlette signs
#: and base64-encodes into a cookie — capped around 4 KB. `read.edge_store`
#: (~2 KB) round-tripped fine and `read.loop` did not: the card rendered
#: **FAILED with a blank entry, blank kind and no reason**, which is precisely
#: the "blank needs a cause before it gets a caption" defect, on the page whose
#: whole job is explaining faults. A diagnostic payload has no business being
#: bounded by a cookie, and the bound is invisible until one exceeds it.
#:
#: Bounded so a long session cannot grow it without limit; keyed by a random id
#: so one browser cannot read another's result.
_RESULTS: "OrderedDict[str, dict]" = OrderedDict()
_RESULTS_MAX = 32


def _stash(result: dict) -> str:
    rid = secrets.token_urlsafe(12)
    _RESULTS[rid] = result
    while len(_RESULTS) > _RESULTS_MAX:
        _RESULTS.popitem(last=False)
    return rid


def _role(request: Request) -> str:
    return "guest" if request.scope.get("ops_role") == "guest" else "owner"


@router.get("/diagnostics/console")
async def diag_console(request: Request):
    api = request.app.state.engine_api
    try:
        raw = await api.diag_catalog() or {}
        entries = raw.get("entries") if isinstance(raw, dict) else None
        error = raw.get("error") if isinstance(raw, dict) else None
    except Exception as exc:  # noqa: BLE001 — reported to the page, never raised
        entries, error = None, f"{type(exc).__name__}: {exc}"

    # Three states, never two. An engine that does not offer the catalog at all
    # is a different finding from one offering an empty one, and both differ
    # from "we could not ask" — the reader's next move is a deploy, a shrug and
    # a network check respectively.
    if entries is None:
        state = "unreachable" if error else "not_reported"
    elif not entries:
        state = "empty"
    else:
        state = "ok"

    rid = request.session.pop(_FLASH_KEY, None)
    result = _RESULTS.pop(rid, None) if rid else None
    return request.app.state.templates.TemplateResponse(
        "diag_console.html",
        {
            "request": request,
            "active": "diag_console",
            "entries": entries or [],
            "state": state,
            "error": error or "",
            "result": result,
        },
    )


@router.post("/diagnostics/console/run")
async def diag_console_run(
    request: Request,
    key: str = Form(...),
    symbol: str = Form(""),
):
    """Run one named entry and park the result for the redirect.

    `key` is passed through untouched and unvalidated **here on purpose**: this
    repo validating it would be a second implementation of the engine's own
    allow-list, and the two would drift. The engine refuses an unknown key and
    says what it does know. `symbol` is the only argument any entry takes.
    """
    api = request.app.state.engine_api
    args: dict[str, Any] = {}
    if symbol.strip():
        args["symbol"] = symbol.strip()

    try:
        out = await api.diag_run(key, args)
    except Exception as exc:  # noqa: BLE001
        out = {"ok": False, "key": key, "error": f"{type(exc).__name__}: {exc}"}

    # Audited like every other action surface, and the actor records WHICH tier
    # ran it — a read-only session driving an engine action is exactly the row
    # an owner reading this log wants to be able to find.
    audit.record(
        request.app.state.settings.audit_log_path,
        action="diag_console_run",
        params={"key": key, "args": args},
        result={"ok": bool(isinstance(out, dict) and out.get("ok")),
                "error": (out or {}).get("error", "")},
        ok=bool(isinstance(out, dict) and out.get("ok")),
        actor=_role(request),
    )

    request.session[_FLASH_KEY] = _stash(
        out if isinstance(out, dict) else {"ok": False, "key": key,
                                           "error": f"unexpected reply: {out!r}"}
    )
    return RedirectResponse("/diagnostics/console", status_code=303)

"""``/control/routing`` — every (path, side), live → dark and dark → live.

Owner, 2026-09-24: *"there is no clear diversion screen, Dark to live and live
to dark, make it clear and I do it later."* The composition and the reasons
are in :mod:`app.data_sources.routing`; this module is the page and the three
writes.

The writes follow the control doctrine exactly:

* **Owner-gated end to end** — they go through the engine's owner-gated
  ``/api/tunables`` (the same endpoint Control → Tunables uses) and are
  classified owner-only in ``guest_scope``.
* **Audited**, best-effort, never blocking the action.
* **PRG**, and **the engine is the source of truth**: each write re-reads the
  engine's retirement snapshot *at the moment of the write* (never the page the
  operator loaded, which may be minutes old), edits that list, writes the whole
  string, then reads the snapshot back and reports what the engine now says —
  not what was submitted.
* **The confirm sits on the risky direction only.** Restoring a path to the
  live feed changes what paid subscribers receive and places orders for
  auto-trade users, so it needs the explicit confirm; diverting to dark stops
  delivery and places nothing, and a confirm there only teaches the operator
  to click through both. The Promotions page carries the same asymmetry for
  the same reason.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import audit
from app.data_sources import routing as rt
from app.routes.dark_signals_live import reduce_rows
from app.routes.path_scorecard import _unwrap as _scorecard_unwrap
from app.routes.path_scorecard import classify as _scorecard_state

router = APIRouter()

_FLASH = "_routing_flash"


def _back(setup_class: str = "") -> str:
    token = quote(str(setup_class or "").strip().upper())
    return f"/control/routing#path-{token}" if token else "/control/routing"


async def _fresh_retirement(api: Any) -> tuple[Any, Any]:
    """The engine's snapshot and its retirement block, read now."""
    snapshot = await api.dark_promotions()
    retirement = snapshot.get("path_retirement") if isinstance(snapshot, dict) else None
    return snapshot, retirement


@router.get("/control/routing")
async def routing_page(request: Request):
    api = request.app.state.engine_api
    templates = request.app.state.templates
    snapshot, retirement = await _fresh_retirement(api)
    try:
        raw = await api.diag_run("read.path_scorecard", {})
    except Exception as exc:  # pragma: no cover - defensive
        raw = {"error": f"{type(exc).__name__}: {exc}"}
    scorecard = _scorecard_unwrap(raw)
    dark_rows = reduce_rows(request.app.state.data_volume.dark_signals())
    rows = rt.build_rows(retirement, snapshot, scorecard.get("cells"), dark_rows)
    return templates.TemplateResponse(
        "control_routing.html",
        {
            "request": request,
            "active": "routing",
            "rows": rows,
            "head": rt.headline(rows, retirement, snapshot),
            "retirement": retirement,
            "readable": rt.retirement_readable(retirement),
            "default_diff": rt.default_diff(retirement),
            "scorecard_state": _scorecard_state(raw),
            "scorecard_window": scorecard.get("window_days"),
            "floor": scorecard.get("floor") or {},
            "snapshot_error": (
                snapshot.get("error") if isinstance(snapshot, dict) else "no snapshot"
            ),
            "live_copy": rt.LIVE_COPY,
            "flash": request.session.pop(_FLASH, None),
        },
    )


async def _write_list(
    request: Request,
    *,
    action: str,
    setup_class: str,
    side: str,
    new_pairs: list[tuple[str, str]],
    before: list[tuple[str, str]],
) -> tuple[bool, Any, Any]:
    """Write the whole list, audit it, read the engine back."""
    api = request.app.state.engine_api
    settings = request.app.state.settings
    spec = rt.spec_for(new_pairs)
    result = await api.set_tunables({"retired_paths": spec})
    ok = isinstance(result, dict) and not result.get("error")
    audit.record(
        settings.audit_log_path,
        action=action,
        params={
            "setup_class": setup_class,
            "side": side,
            "before": rt.spec_for(before),
            "after": spec,
        },
        result=result if isinstance(result, dict) else {"result": str(result)},
        ok=ok,
    )
    _snapshot, after = await _fresh_retirement(api)
    return ok, result, after


def _refuse(request: Request, setup_class: str, text: str) -> RedirectResponse:
    request.session[_FLASH] = {"ok": False, "text": text}
    return RedirectResponse(_back(setup_class), status_code=303)


@router.post("/control/routing/divert")
async def routing_divert(
    request: Request, setup_class: str = Form(...), side: str = Form(...)
):
    """Live → dark for one (path, side). No confirm: see the module docstring."""
    setup, want = str(setup_class).strip().upper(), str(side).strip().upper()
    if want not in rt.SIDES:
        return _refuse(request, setup, f"{setup} {side}: side must be LONG or SHORT.")
    _snap, retirement = await _fresh_retirement(request.app.state.engine_api)
    if not rt.retirement_readable(retirement):
        return _refuse(
            request, setup,
            f"{setup} {want}: nothing changed — the engine did not report its "
            f"retirement list, so an edit would overwrite a list this page "
            f"cannot see.",
        )
    before = rt.pairs_of(retirement.get("retired"))
    if rt.is_listed(before, setup, want):
        return _refuse(request, setup, f"{setup} {want} is already diverted to dark.")
    ok, result, after = await _write_list(
        request, action="path_divert", setup_class=setup, side=want,
        new_pairs=rt.with_diverted(before, setup, want), before=before,
    )
    now_listed = rt.retirement_readable(after) and rt.is_listed(
        rt.pairs_of(after.get("retired")), setup, want
    )
    if not ok:
        detail = result.get("error") if isinstance(result, dict) else result
        text, good = f"{setup} {want}: engine refused — {detail}", False
    elif not now_listed:
        text = (
            f"{setup} {want}: the write was accepted but the engine does not "
            f"report it as diverted. Nothing is confirmed — re-read this page."
        )
        good = False
    elif not after.get("enabled"):
        text = (
            f"{setup} {want}: on the retired list, but the retirement master "
            f"switch is OFF — it still reaches subscribers."
        )
        good = False
    else:
        text = (
            f"{setup} {want}: diverted to dark. Subscribers stop receiving it on "
            f"the next scan; it keeps being measured in the dark feed."
        )
        good = True
    request.session[_FLASH] = {"ok": good, "text": text}
    return RedirectResponse(_back(setup), status_code=303)


@router.post("/control/routing/restore")
async def routing_restore(
    request: Request,
    setup_class: str = Form(...),
    side: str = Form(...),
    confirm: str = Form(""),
):
    """Dark → live for one retired (path, side). Needs the confirm."""
    setup, want = str(setup_class).strip().upper(), str(side).strip().upper()
    if want not in rt.SIDES:
        return _refuse(request, setup, f"{setup} {side}: side must be LONG or SHORT.")
    if not confirm:
        return _refuse(
            request, setup,
            f"{setup} {want}: not restored — putting a path back on the live "
            f"feed changes what paid subscribers receive, so it needs the "
            f"confirm box.",
        )
    _snap, retirement = await _fresh_retirement(request.app.state.engine_api)
    if not rt.retirement_readable(retirement):
        return _refuse(
            request, setup,
            f"{setup} {want}: nothing changed — the engine did not report its "
            f"retirement list.",
        )
    before = rt.pairs_of(retirement.get("retired"))
    if not rt.is_listed(before, setup, want):
        return _refuse(request, setup, f"{setup} {want} is not diverted — nothing to restore.")
    ok, result, after = await _write_list(
        request, action="path_restore", setup_class=setup, side=want,
        new_pairs=rt.with_restored(before, setup, want), before=before,
    )
    still_listed = (not rt.retirement_readable(after)) or rt.is_listed(
        rt.pairs_of(after.get("retired")), setup, want
    )
    if not ok:
        detail = result.get("error") if isinstance(result, dict) else result
        text, good = f"{setup} {want}: engine refused — {detail}", False
    elif still_listed:
        text = (
            f"{setup} {want}: the write was accepted but the engine still "
            f"reports it as diverted (or could not be read back). Nothing is "
            f"confirmed — re-read this page."
        )
        good = False
    else:
        text = (
            f"{setup} {want}: restored — it reaches subscribers again from the "
            f"next scan."
        )
        good = True
    request.session[_FLASH] = {"ok": good, "text": text}
    return RedirectResponse(_back(setup), status_code=303)


@router.post("/control/routing/adopt-default")
async def routing_adopt_default(request: Request, confirm: str = Form("")):
    """Replace the list with the owner's signed-off default.

    The confirm is required only when adopting it would put something back on
    the live feed — the same asymmetry as the per-row controls.
    """
    _snap, retirement = await _fresh_retirement(request.app.state.engine_api)
    diff = rt.default_diff(retirement)
    if not diff["readable"]:
        return _refuse(
            request, "",
            "Nothing changed — the engine did not report its retirement list or "
            "its default.",
        )
    if diff["same"]:
        return _refuse(request, "", "The live list already matches the signed-off default.")
    if diff["to_restore"] and not confirm:
        names = ", ".join(f"{p} {s}" for p, s in diff["to_restore"])
        return _refuse(
            request, "",
            f"Not adopted — it would put {names} back on the live feed, which "
            f"needs the confirm box.",
        )
    before = rt.pairs_of(retirement.get("retired"))
    default = rt.pairs_of(retirement.get("default"))
    ok, result, after = await _write_list(
        request, action="path_adopt_default", setup_class="*", side="*",
        new_pairs=default, before=before,
    )
    now = rt.default_diff(after)
    if not ok:
        detail = result.get("error") if isinstance(result, dict) else result
        text, good = f"Engine refused — {detail}", False
    elif not now.get("same"):
        text = (
            "The write was accepted but the engine's list still differs from "
            "the default. Nothing is confirmed — re-read this page."
        )
        good = False
    else:
        text, good = ("Retired list set to the signed-off default.", True)
    request.session[_FLASH] = {"ok": good, "text": text}
    return RedirectResponse(_back(), status_code=303)

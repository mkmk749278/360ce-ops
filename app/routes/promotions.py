"""``/control/promotions`` — moving a measured path from dark to the live feed.

The dark lane has been answering *"was this setup worth sending"* for weeks.
Until now the only way to act on the answer was to edit
``REGIME_SETUP_COMPATIBILITY`` in the engine and ship a deploy, which means
every promotion is a code change, an owner-sign-off item and a ~45s production
rollout — and is all-or-nothing per path, when the measured evidence is
per-gate and per-direction.

This page is the act. Per path: one master switch, and the conditions under
which that path's diverted rows stop being diverted. Everything else about the
candidate is unchanged — it has already cleared every gate except the loosened
one, and the router's full second layer still runs below it.

**The evidence is on the page with the control.** Each dimension's cells render
beside the checkboxes that select them: n, campaigns, then the average, in that
order and never sorted by edge. A control panel that made a promotion one click
away from a number the owner had to go to another page to read would be a
machine for promoting the top row of a table — and the top row of a long table
beats a coin flip by construction (`FAILED_AUCTION_RECLAIM`, +0.846R on three
rows, promotion requested within the day).

Its own sub-tab under Control, for the reason `/control/access` has one: the
engine page is long, and a control that changes what paid subscribers receive
should not be a card below the tunables list. It carries its own flash key so
an action cannot render its result on a page the operator did not come from.

Owner-only, and classified as such in ``guest_scope`` — a read-only guest can
see every measurement page this decision is made from and none of the switches.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import RedirectResponse

from app import audit
from app.data_sources import dark_promotion as dp
from app.routes.dark_signals_live import reduce_rows

router = APIRouter()

#: The dimensions a rule can filter on, in the order a reader should build
#: one: the gate is what the rule is *about*, the rest narrow it.
DIMENSIONS = ("gate", "regime", "session")

#: Rendered beside each dimension so the form says what it is selecting on,
#: rather than leaving the reader to infer it from the values.
DIMENSION_COPY = {
    "gate": (
        "Which rejection this path may be carried past. The rule is about "
        "this — the evidence is per gate, and promoting a path wholesale "
        "promotes gates whose rows never argued for it."
    ),
    "regime": (
        "The entry regime stamped on the row, at the trigger timeframe."
    ),
    "session": (
        "The session component of the engine's context key "
        "(session/phase/volatility/rotation)."
    ),
}

#: Direction conditions, with the sentence each one means. Mirrors the engine's
#: vocabulary but is never the authority for it: the form offers what the
#: engine's snapshot lists, and anything the engine names that is missing here
#: renders under its raw token rather than disappearing from the select.
DIRECTION_COPY = {
    "any": "Any direction — no alignment condition.",
    "long": "Longs only.",
    "short": "Shorts only.",
    "with_trend": (
        "Only when the trade agrees with the entry regime's trend. A range "
        "label names no trend, so those rows abstain rather than passing."
    ),
    "counter_trend": (
        "Only when the trade opposes the entry regime's trend. Unknown trends "
        "abstain here too."
    ),
}

_FLASH = "_promotions_flash"


def _back(setup_class: str) -> str:
    """Redirect target after a write — the card that was acted on.

    PRG so a refresh cannot re-fire the action, and scoped to the path so the
    operator lands on the rule they just changed instead of the top of a long
    page.
    """
    token = quote(str(setup_class or "").strip())
    return f"/control/promotions?setup={token}" if token else "/control/promotions"


def _checked(values: list[str]) -> list[str]:
    """Normalise one dimension's checked boxes.

    Empty stays empty. The engine reads an empty allow-list as "matches
    nothing", which is the whole fail-closed property — so this must never
    helpfully substitute a wildcard for a field the owner left blank.

    The checkboxes post as repeated fields and are read as repeated fields.
    The first cut joined them into a hidden input with JavaScript on submit,
    which meant a browser with JS off (or a submit that fired before the
    handler) would post *no* allow-lists at all — and because empty is
    fail-closed, the owner would get a rule saved, armed and silently inert.
    A control that appears to work and does nothing is the failure this repo
    already names; here it would look like a promotion that never promotes.
    """
    return [tok.strip() for tok in (values or []) if tok and tok.strip()]


def _load_rows(request: Request) -> list[dict]:
    """The dark ledger, through the same reducer the dark-feed page uses.

    One loader and one reducer, so a condition cannot be worth one thing on the
    page it is chosen from and another on the page it is judged from.
    """
    return reduce_rows(request.app.state.data_volume.dark_signals())


def _build_context(
    request: Request, snapshot: Any, rows: list[dict], open_setup: str = ""
) -> dict:
    setups = sorted({str(r.get("setup_class") or "") for r in rows if r.get("setup_class")})
    cards = []
    for setup in setups:
        setup_rows = [r for r in rows if str(r.get("setup_class") or "") == setup]
        rule = dp.rule_for(snapshot, setup)
        state, state_copy = dp.rule_state(snapshot, rule)
        cards.append({
            "setup_class": setup,
            "rule": rule,
            "state": state,
            "state_copy": state_copy,
            "summary": dp.summarize(setup_rows),
            "lanes": dp.promoted_vs_dark(setup_rows),
            "evidence": {
                dim: dp.condition_evidence(setup_rows, dimension=dim)
                for dim in DIMENSIONS
            },
            # Each table above is MARGINAL and the rule built from them is an
            # INTERSECTION. `selection` is the joint count — what this rule
            # actually keeps — and the per-dimension refusals that explain the
            # rest. Without it a reader can tick the best-looking cell of each
            # table and save a rule that matches nothing, with every number on
            # the page still reading well-evidenced (2026-08-17).
            "selection": dp.rule_selection(setup_rows, rule),
            # The engine's own census, from the process that ran `decide`.
            # Authority for what actually happened; `selection` above is ops
            # reconstructing the predicate over the whole ledger, and the panel
            # says which is which.
            "refusals": dp.engine_refusals(snapshot, setup),
            "n_rows": len(setup_rows),
            # Collapsed by default. A path with a rule opens, because the thing
            # you changed is the thing you want to see — and so does the one
            # named in `?setup=`, which is what the flash links back to after a
            # save. Thirteen expanded cards is what made this page 32 screens.
            "open": bool(rule) or setup.upper() == open_setup.upper(),
        })
    # Most evidence first. Deliberately not "best average first": a page that
    # ranks paths by edge puts the luckiest thin cell at the top and invites
    # exactly the promotion this repo has already paid for once.
    cards.sort(key=lambda c: -c["summary"]["n_scored"])

    # The filter. `?setup=` narrows the page to one path — the index and the
    # selector above it still list every path, measured on the WHOLE ledger,
    # because a selector applied to its own counts makes every option read
    # "n = whatever I picked" (#90/#91).
    index = [
        {"setup_class": c["setup_class"], "state": c["state"],
         "summary": c["summary"], "lanes": c["lanes"]}
        for c in cards
    ]
    if open_setup:
        want = open_setup.strip().upper()
        matched = [c for c in cards if c["setup_class"] == want]
        # An unknown path filters to nothing rather than silently showing
        # everything: "you picked a path that has no rows" and "here is the
        # whole book" are different answers and only one of them is true.
        cards = matched
        filter_missing = not matched
    else:
        filter_missing = False

    directions = []
    if isinstance(snapshot, dict):
        directions = list(snapshot.get("directions") or [])
    if not directions:
        directions = list(DIRECTION_COPY)

    return {
        "request": request,
        "active": "promotions",
        "snapshot": snapshot if isinstance(snapshot, dict) else {},
        "error": (snapshot or {}).get("error") if isinstance(snapshot, dict) else None,
        "cards": cards,
        "index": index,
        "filter_setup": open_setup.strip().upper() if open_setup else "",
        "filter_missing": filter_missing,
        "dimensions": DIMENSIONS,
        "dimension_copy": DIMENSION_COPY,
        "refusal_copy": dp.DIMENSION_REFUSAL_COPY,
        "directions": directions,
        "direction_copy": DIRECTION_COPY,
        "any_token": (snapshot or {}).get("any_token", "*") if isinstance(snapshot, dict) else "*",
        "flash": request.session.pop(_FLASH, None),
        "n_rows": len(rows),
        "open_setup": open_setup,
        # Path retirement — the same decision pointing the other way. Read off
        # the engine's own snapshot, never recomputed here; a second copy of
        # the retired list in ops would drift from the one the scanner
        # enforces, and the drift is invisible until a path nobody retired
        # stops delivering. `None` (absent) means the engine predates the
        # mechanism, which is NOT the same as "nothing is retired".
        "retirement": (snapshot or {}).get("path_retirement")
                      if isinstance(snapshot, dict) else None,
    }


@router.get("/control/promotions")
async def promotions_page(request: Request, setup: str = Query("")):
    """The index, then the cards.

    ``?setup=`` opens one path's card. Every write redirects back with it set,
    so a save lands on the card it changed rather than at the top of a page the
    operator then has to search — the whole reason this page was hard to use.
    """
    api = request.app.state.engine_api
    templates = request.app.state.templates
    snapshot = await api.dark_promotions()
    rows = _load_rows(request)
    return templates.TemplateResponse(
        "control_promotions.html",
        _build_context(request, snapshot, rows, open_setup=setup),
    )


@router.post("/control/promotions/save")
async def promotions_save(
    request: Request,
    setup_class: str = Form(...),
    enabled: str = Form(""),
    gate_pick: list[str] = Form(default=[]),
    regime_pick: list[str] = Form(default=[]),
    session_pick: list[str] = Form(default=[]),
    direction: str = Form("any"),
    min_confidence: str = Form(""),
    max_per_day: str = Form("25"),
    note: str = Form(""),
    confirm: str = Form(""),
):
    """Create or replace one path's rule.

    Arming a rule requires an explicit confirm; disabling one does not. The
    asymmetry is deliberate and matches the control doctrine's treatment of
    every destructive action: switching a rule ON changes what paid subscribers
    receive, switching it OFF returns the path to exactly where it was, and a
    confirm on the safe direction only teaches the operator to click through
    both.

    The engine's response is what gets rendered, not the submitted form. It
    normalises tokens, refuses an unknown direction and clamps the cap, so an
    echo would report a setting the engine will not enforce.
    """
    api = request.app.state.engine_api
    settings = request.app.state.settings
    arming = bool(enabled)

    if arming and not confirm:
        request.session[_FLASH] = {
            "ok": False,
            "text": (
                f"{setup_class}: not armed — arming a rule changes what "
                f"subscribers receive, so it needs the confirm box."
            ),
        }
        return RedirectResponse(_back(setup_class), status_code=303)

    try:
        cap = max(0, int(float(max_per_day or 0)))
    except (TypeError, ValueError):
        request.session[_FLASH] = {
            "ok": False,
            "text": f"{setup_class}: daily cap must be a number.",
        }
        return RedirectResponse(_back(setup_class), status_code=303)

    conf: float | None
    try:
        conf = float(min_confidence) if str(min_confidence).strip() else None
    except (TypeError, ValueError):
        conf = None

    payload = {
        "setup_class": setup_class,
        "enabled": arming,
        "gates": _checked(gate_pick),
        "regimes": _checked(regime_pick),
        "sessions": _checked(session_pick),
        "direction": (direction or "any").strip().lower(),
        "min_confidence": conf,
        "max_per_day": cap,
        "note": note or "",
        "updated_by": "ops",
    }
    result = await api.set_dark_promotion(payload)
    ok = isinstance(result, dict) and not result.get("error")

    audit.record(
        settings.audit_log_path,
        action="dark_promotion_set",
        params=payload,
        result=result if isinstance(result, dict) else {"result": str(result)},
        ok=ok,
    )

    if not ok:
        detail = (result or {}).get("error") if isinstance(result, dict) else result
        request.session[_FLASH] = {
            "ok": False,
            "text": f"{setup_class}: engine refused — {detail}",
        }
        return RedirectResponse(_back(setup_class), status_code=303)

    stored = result.get("rule") or {}
    if stored.get("inert"):
        # Saved successfully and incapable of doing anything. Reported as its
        # own outcome rather than as a success, because a switch in the on
        # position that promotes nothing is the state an operator is most
        # likely to misread as working.
        text = (
            f"{setup_class}: saved and ARMED, but INERT — an allow-list is "
            f"empty so no candidate can match. Nothing is being promoted."
        )
        state_ok = False
    elif stored.get("enabled") and not result.get("master_enabled"):
        text = (
            f"{setup_class}: rule armed, but the engine-wide master switch "
            f"(dark_promotion_enabled) is OFF — nothing is promoted yet."
        )
        state_ok = False
    elif stored.get("enabled"):
        text = (
            f"{setup_class}: LIVE — gates {', '.join(stored.get('gates') or []) or '—'}, "
            f"direction {stored.get('direction')}, cap {stored.get('max_per_day')}/day."
        )
        state_ok = True
    else:
        text = f"{setup_class}: rule saved and switched OFF. Conditions kept."
        state_ok = True

    request.session[_FLASH] = {"ok": state_ok, "text": text}
    return RedirectResponse(_back(setup_class), status_code=303)


@router.post("/control/promotions/delete")
async def promotions_delete(request: Request, setup_class: str = Form(...)):
    """Remove a rule entirely — distinct from switching it off.

    Off keeps the conditions, so re-arming is one switch. Delete throws them
    away, which is why it is a separate control rather than a state of the same
    one.
    """
    api = request.app.state.engine_api
    settings = request.app.state.settings
    result = await api.delete_dark_promotion(setup_class)
    ok = isinstance(result, dict) and not result.get("error")
    audit.record(
        settings.audit_log_path,
        action="dark_promotion_deleted",
        params={"setup_class": setup_class},
        result=result if isinstance(result, dict) else {"result": str(result)},
        ok=ok,
    )
    request.session[_FLASH] = {
        "ok": ok,
        "text": (
            f"{setup_class}: rule removed — every dark row for this path is "
            f"diverted again."
            if ok
            else f"{setup_class}: engine refused — {(result or {}).get('error')}"
        ),
    }
    return RedirectResponse(_back(setup_class), status_code=303)

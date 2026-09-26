"""Engine control plane — the dashboard's first *write* surface.

Promoted from read-only on 2026-06-20: Telegram is unavailable in-region,
so ops becomes the manual control surface for the engine.  Two controls
ship here:

* **Auto-execution mode** — off / paper / live (engine-wide, the
  ``/api/auto-mode`` flip the operator used to do over Telegram).
* **Global kill switch** — B18 emergency halt (engage / disengage).

Every action is owner-gated on the engine (the dashboard's static token is
owner-tier) and recorded in the append-only audit log.  We use POST→redirect
→GET with a one-shot session flash so a browser refresh never re-fires a
control action.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import audit
from app.routes.positions import _enrich_row
from app.routes.trail_governor import lane_state

router = APIRouter()

_VALID_MODES = {"off", "paper", "live"}


def _is_error(result: object) -> bool:
    return isinstance(result, dict) and bool(result.get("error"))


#: Category render order, most-consequential first. Anything the engine adds
#: that is not named here still renders — it sorts to the end rather than
#: disappearing, because a hand-kept order list that silently drops a category
#: is the deny-list defect this repo has paid for under several names.
_CATEGORY_ORDER = ("Stops & exits", "Signal gating", "Execution", "Measurement")


def anchor_for(category: str) -> str:
    """A stable DOM id for a category, for the jump nav."""
    slug = "".join(
        ch.lower() if ch.isalnum() else "-" for ch in str(category)
    ).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return f"tun-{slug or 'other'}"


#: The tunable the Routing page owns. It renders here read-only, as the list
#: it is, with a link to the page that edits it. Editing it from a category
#: form wrote back the whole string as the page had loaded it, which could
#: undo a divert made on Routing a minute earlier: that page re-reads the list
#: at write time, and a stale form does not.
ROUTING_OWNED_KEYS = frozenset({"retired_paths"})


def split_tunables(entries: list) -> tuple[list, list]:
    """Split a category into its on/off switches and its values.

    A switch saves on the tap that flips it; a value is typed, so it is still
    applied from a form. Keeping them apart is what lets a switch be one tap
    without dragging every number in its category into the same save.
    """
    switches = [e for e in entries if e.get("type") == "bool"]
    values = [e for e in entries if e.get("type") != "bool"]
    return switches, values


def is_changed(entry: dict) -> bool:
    """Has this tunable been moved off its boot default?

    The single most useful operational question on this page — *what did I
    change?* — and it was not answerable before: 77 knobs rendered identically
    whether or not anyone had ever touched them.

    Compared as strings deliberately. The engine round-trips these through
    JSON, so an int knob can arrive as ``3`` or ``3.0`` depending on the store,
    and ``!=`` on the raw values would mark untouched rows as changed — a
    "you edited this" badge that cries wolf is worse than no badge, because the
    reader stops reading it.
    """
    if not isinstance(entry, dict):
        return False
    value, default = entry.get("value"), entry.get("default")
    if isinstance(value, bool) or isinstance(default, bool):
        return bool(value) is not bool(default)
    if value is None and default is None:
        return False
    try:
        if value is not None and default is not None:
            return float(value) != float(default)
    except (TypeError, ValueError):
        pass
    return str(value if value is not None else "") != str(
        default if default is not None else ""
    )


def group_tunables(tunables: object) -> tuple[dict[str, list], bool]:
    """Group the engine's tunables by category, in render order.

    Each entry gains ``changed``; the caller does not recompute it, so the
    badge, the per-category count and the page total can never disagree.
    """
    groups: dict[str, list] = {}
    initialised = False
    if isinstance(tunables, dict) and not tunables.get("error"):
        initialised = bool(tunables.get("initialised"))
        for entry in tunables.get("tunables") or []:
            if isinstance(entry, dict):
                entry = dict(entry)
                entry["changed"] = is_changed(entry)
                groups.setdefault(
                    str(entry.get("category") or "Other"), []
                ).append(entry)

    def _rank(cat: str) -> tuple[int, str]:
        try:
            return (_CATEGORY_ORDER.index(cat), "")
        except ValueError:
            return (len(_CATEGORY_ORDER), cat)

    return {c: groups[c] for c in sorted(groups, key=_rank)}, initialised


def category_meta(tunable_groups: dict[str, list]) -> list[dict]:
    """One dict per category, in render order, for the template.

    The typed knobs sit under ``value_knobs`` and not ``values``: Jinja
    resolves ``meta.values`` to the dict's own ``values`` method before the
    item of that name, and the page 500'd on its first render. ``/system/redis``
    paid for the same collision on ``keys``, and the throttle table on
    ``copy`` — a test now asserts no key here shadows a dict method.
    """
    out = []
    for cat, entries in tunable_groups.items():
        switches, values = split_tunables(entries)
        out.append({
            "category": cat,
            "anchor": anchor_for(cat),
            "count": len(entries),
            "changed": sum(1 for e in entries if e.get("changed")),
            "switches": switches,
            "value_knobs": values,
            "value_knobs_changed": sum(1 for e in values if e.get("changed")),
        })
    return out


def governor_summary(payload: object) -> dict:
    """The trail governor, reduced for its row on the switchboard.

    (Written for the at-a-glance strip that preceded the switchboard; the strip
    was folded into it on 2026-09-24 and the reasoning below carries over.)
    The strip named five switches and omitted the only one that moves a real
    stop order on a live account — the governor cancels the evaluator's SL and
    parks its own, and its state was readable nowhere on this page. A reader
    checking "is anything touching my stops right now?" had to know that the
    answer lived 77 knobs down inside a collapsed category.

    The state is CLASSIFIED BY THE GOVERNOR PAGE'S OWN ``lane_state``, not
    re-derived here — its five states exist because ``off``, ``armed``,
    ``index_cold`` and a working-but-quiet book all render as "no rows" and
    have four different next moves. A second classifier would drift from the
    page it is meant to summarise, and the fix for a drifting mirror is not a
    second mirror.

    Note this deliberately does NOT read ``trail_governor_enabled`` from the
    tunables already on the page. That is the *switch*; this is what the
    governor is *doing*, and the two came apart on 2026-08-10 when a
    free-text timeframe left the switch reading ON while every position was
    refused. A tile sourced from the switch would have shown green.
    """
    state = lane_state(payload if isinstance(payload, dict) else {"error": "no payload"})
    data = payload if isinstance(payload, dict) else {}
    return {
        "state": state,
        "governed": int(data.get("governed") or 0),
        "timeframe": data.get("timeframe"),
    }


#: Readable names for the control actions this repo writes. A name missing
#: here is not an error: it renders humanised from the raw action, so a new
#: writer reads sensibly the day it ships instead of rendering blank.
_ACTION_TITLES = {
    "auto_mode": "Auto-execution mode",
    "kill_switch": "Kill switch",
    "auto_trade_global": "Global auto-trade",
    "signal_expiry": "Signal expiry",
    "play_billing": "Play billing",
    "tunables_update": "Engine tunables",
    "tunables": "Engine tunables",
    "signal_reset_full": "Full signal reset",
    "close_signal": "Close signal",
}

#: How many tunable writes one audit row lists before summarising the rest.
_AUDIT_VALUES_SHOWN = 3


def _audit_value(value: object) -> str:
    """One value as a person reads it. Bools arrive as ``True`` or ``"True"``
    (the tunables writer stores ``str(v)``), and both read as on/off."""
    if isinstance(value, bool) or value in ("True", "False"):
        return "on" if value in (True, "True") else "off"
    if value is None or value == "":
        return "empty"
    return str(value)


def describe_audit(entry: object, labels: dict | None = None) -> dict:
    """An audit row as a sentence, with the raw params kept for the hover.

    The table used to print ``{"be_arm_trigger_pct": 1.2}``: correct, and
    unreadable at a glance. Tunable keys resolve to the label the engine
    publishes; a key it no longer publishes keeps its raw name rather than
    vanishing.
    """
    labels = labels or {}
    e = entry if isinstance(entry, dict) else {}
    action = str(e.get("action") or "")
    params = e.get("params") if isinstance(e.get("params"), dict) else {}
    title = _ACTION_TITLES.get(action) or (
        action.replace("_", " ").strip().capitalize() or "Unknown action"
    )
    lines: list[str] = []
    values = params.get("values")
    if action == "kill_switch":
        lines.append("Engaged" if params.get("engaged") else "Disengaged")
        if params.get("reason"):
            lines.append(f"Reason: {params['reason']}")
    elif action == "auto_mode" and params.get("mode"):
        lines.append(f"Set to {str(params['mode']).upper()}")
    elif "enabled" in params and len(params) == 1:
        lines.append("Turned on" if params.get("enabled") else "Turned off")
    elif isinstance(values, dict):
        items = list(values.items())
        for key, val in items[:_AUDIT_VALUES_SHOWN]:
            lines.append(f"{labels.get(key, key)} → {_audit_value(val)}")
        if len(items) > _AUDIT_VALUES_SHOWN:
            lines.append(f"…and {len(items) - _AUDIT_VALUES_SHOWN} more")
    else:
        for key, val in params.items():
            lines.append(f"{str(key).replace('_', ' ')}: {_audit_value(val)}")
    return {
        "ts": e.get("ts"),
        "title": title,
        "lines": lines,
        "ok": bool(e.get("ok")),
        "result": e.get("result"),
        "raw": json.dumps(params, default=str),
    }


#: Session key for the mode change THIS browser asked for, so the page can say
#: "switching" through the window before the engine applies it. It is a note
#: about a request, never a reading: the mode shown always comes from the
#: engine.
_MODE_REQUEST_KEY = "_control_mode_request"

#: How long a request is tracked. The engine applies a queued change within one
#: writer cycle (~15s) and the queue itself expires after 60s; past this a
#: request that never showed has not merely been slow.
MODE_REQUEST_WINDOW_SEC = 120

#: How long an engine answer (a refusal) stays on screen after it was given.
MODE_RESULT_SHOWN_SEC = 900


def _parse_ts(value: object) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def mode_view(auto: object, cmd: object, req: object, now: datetime) -> dict:
    """The auto-execution mode row, graded from the engine's own answers.

    Owner, 2026-09-26: *"There is some problem with auto execution mode
    toggle, not showing correctly."* In production a click only QUEUES the
    change; the engine applies it at the end of its next writer cycle, or
    REFUSES it (open positions, no exchange keys for LIVE) — and the page said
    "Auto-mode set to PAPER" beside a toggle still reading LIVE either way.

    States, each with a different next move:

    * ``ok`` — the reading is the whole story;
    * ``pending`` — the engine reports the change queued, not yet applied;
    * ``applying`` — this browser asked, nothing contradicts it yet, and the
      engine cannot say more (an engine predating the command endpoint);
    * ``not_applied`` — asked long enough ago that it should have shown, and
      it has not; the engine gave no reason we can read;
    * ``refused`` — the engine answered, and the answer was no.

    ``cmd`` comes from two producers, told apart by KEY PRESENCE (the
    2026-09-03 rule): the engine's payload carries ``mode_queue``; ops' own
    transport wrapper carries ``error``. A 404 is an engine that predates the
    endpoint — not reported, which is not "nothing pending".
    """
    auto = auto if isinstance(auto, dict) else {}
    cmd = cmd if isinstance(cmd, dict) else {}
    req = req if isinstance(req, dict) else {}

    if "mode_queue" in cmd:
        queue = cmd.get("mode_queue")
    elif cmd.get("status_code") == 404:
        queue = "not_reported"
    else:
        queue = "unreadable"

    mode = None
    if queue in ("queued", "direct") and cmd.get("mode"):
        mode = str(cmd["mode"]).lower()  # read from Redis now, not 10s ago
    elif auto.get("mode"):
        mode = str(auto["mode"]).lower()

    view: dict = {
        "mode": mode or "unknown",
        "error": None if mode else (auto.get("error") or "the engine reported no mode"),
        "queue": queue,
        "state": "ok",
        "target": None,
        "message": None,
        "age_sec": None,
        "boot_mode": (str(cmd["boot_mode"]).lower() if cmd.get("boot_mode") else None),
        "refresh": False,
    }
    if view["boot_mode"] and mode and view["boot_mode"] != mode:
        view["resets_to"] = view["boot_mode"]

    req_mode = str(req.get("mode") or "").lower() or None
    req_at = _parse_ts(req.get("at"))
    req_age = (now - req_at).total_seconds() if req_at else None
    tracking = bool(req_mode and req_age is not None and req_age <= MODE_REQUEST_WINDOW_SEC)

    pending = str(cmd.get("pending_mode") or "").lower() or None
    last = cmd.get("last_mode_command") if isinstance(cmd.get("last_mode_command"), dict) else None
    last_at = _parse_ts(last.get("at")) if last else None
    last_age = (now - last_at).total_seconds() if last_at else None

    if queue == "queued" and pending:
        view.update(state="pending", target=pending, refresh=True)
        return view
    if (
        last
        and last.get("outcome") in ("refused", "error", "invalid")
        and last_age is not None
        and last_age <= MODE_RESULT_SHOWN_SEC
        and (not req_at or not last_at or last_at >= req_at)
    ):
        view.update(
            state="refused",
            target=str(last.get("requested") or "").lower() or None,
            message=last.get("message"),
            age_sec=last_age,
        )
        return view
    if tracking and mode and req_mode != mode:
        if queue == "queued":
            # The engine's queue is empty and it reports no answer to this
            # request: the command expired unapplied, or the answer is older.
            view.update(state="not_applied", target=req_mode, age_sec=req_age)
        elif req_age <= 60:
            view.update(state="applying", target=req_mode, age_sec=req_age, refresh=True)
        else:
            view.update(state="not_applied", target=req_mode, age_sec=req_age)
    return view


async def _render(request: Request):
    api = request.app.state.engine_api
    settings = request.app.state.settings
    templates = request.app.state.templates

    auto = await api.auto_mode()
    mode_cmd = await api.auto_mode_command()
    ks = await api.kill_switch_state()
    glob = await api.auto_trade_global_state()
    expiry = await api.signal_expiry_state()
    billing = await api.billing_enabled_state()
    tunables = await api.tunables_state()
    # Tolerated separately from the five switches above: this page carries the
    # kill switch, and the newest read on it must never be what stops the
    # owner reaching that button. `_get` already converts an HTTP failure into
    # an error dict; this catches the residue (a non-JSON body). Nothing is
    # swallowed — an error renders as the tile's own `error` state.
    try:
        governor_raw = await api.trail_governor()
    except Exception as exc:  # pragma: no cover - defensive
        governor_raw = {"error": f"{type(exc).__name__}: {exc}"}
    flash = request.session.pop("_control_flash", None)
    mode = mode_view(
        auto, mode_cmd, request.session.get(_MODE_REQUEST_KEY),
        datetime.now(timezone.utc),
    )
    if mode["state"] in ("ok", "refused", "not_applied"):
        # Resolved one way or the other; stop tracking this browser's request.
        request.session.pop(_MODE_REQUEST_KEY, None)

    tunable_groups, tunables_initialised = group_tunables(tunables)
    groups_meta = category_meta(tunable_groups)
    changed_total = sum(g["changed"] for g in groups_meta)
    labels = {
        str(e.get("key")): str(e.get("label") or e.get("key"))
        for entries in tunable_groups.values() for e in entries
    }

    return templates.TemplateResponse(
        "control.html",
        {
            "request": request,
            "active": "control",
            "auto": auto if isinstance(auto, dict) else {},
            "mode_row": mode,
            "ks": ks if isinstance(ks, dict) else {},
            "glob": glob if isinstance(glob, dict) else {},
            "expiry": expiry if isinstance(expiry, dict) else {},
            "billing": billing if isinstance(billing, dict) else {},
            "governor": governor_summary(governor_raw),
            "tunable_groups": tunable_groups,
            "groups_meta": groups_meta,
            "changed_total": changed_total,
            "tunables_initialised": tunables_initialised,
            "audit": [
                describe_audit(e, labels)
                for e in audit.tail(settings.audit_log_path, limit=25)
            ],
            "routing_owned": ROUTING_OWNED_KEYS,
            "flash": flash,
        },
    )


@router.get("/control")
async def control_page(request: Request):
    return await _render(request)


@router.get("/control/positions")
async def control_positions_partial(request: Request):
    """HTMX partial — the live open-positions table the control panel polls.

    Read-only for now (the foundation of the control panel's position view);
    the per-position close action lands as a separate owner-sign-off PR since
    it fires real Binance order changes through the FSM.
    """
    api = request.app.state.engine_api
    templates = request.app.state.templates
    payload = await api.positions_diag()
    items: list = []
    error = None
    monitor_running = False
    if isinstance(payload, dict):
        if payload.get("error"):
            error = str(payload.get("error"))
        else:
            raw = payload.get("items") or []
            if isinstance(raw, list):
                items = [_enrich_row(it) for it in raw if isinstance(it, dict)]
            monitor_running = bool(payload.get("monitor_running", False))
    # Only genuine open positions (skip phantom placeholder rows).
    items = [
        r for r in items
        if (r.get("symbol") or "").strip()
        and float(r.get("entry") or 0.0) > 0.0
    ]
    items.sort(key=lambda r: -(r.get("minutes_open") or 0))
    return templates.TemplateResponse(
        "_control_positions.html",
        {
            "request": request,
            "rows": items,
            "error": error,
            "monitor_running": monitor_running,
        },
    )


@router.post("/control/auto-mode")
async def control_auto_mode(request: Request, mode: str = Form(...)):
    api = request.app.state.engine_api
    settings = request.app.state.settings
    mode = (mode or "").strip().lower()

    if mode not in _VALID_MODES:
        request.session["_control_flash"] = {
            "ok": False,
            "text": f"Rejected — invalid mode {mode!r}.",
        }
        return RedirectResponse("/control", status_code=303)

    result = await api.set_auto_mode(mode)
    ok = not _is_error(result)
    audit.record(
        settings.audit_log_path,
        action="auto_mode",
        params={"mode": mode},
        result=result if isinstance(result, dict) else {},
        ok=ok,
    )
    code = result.get("status_code") if isinstance(result, dict) else None
    detail = result.get("error") if isinstance(result, dict) else result
    if ok and isinstance(result, dict) and result.get("queued"):
        # Accepted, not applied: the engine picks it up on its next cycle and
        # may still refuse it. The page reads back what it actually did.
        text = (
            f"{mode.upper()} requested — queued for the engine, which applies "
            f"it on its next cycle (about 15 seconds)."
        )
        flash_ok = True
        request.session[_MODE_REQUEST_KEY] = {
            "mode": mode, "at": datetime.now(timezone.utc).isoformat(),
        }
    elif ok:
        text = f"Auto-mode set to {mode.upper()}."
        flash_ok = True
    elif code == 409 and "already" in str(detail).lower():
        # A no-op — already there, or already queued. The engine's own words,
        # because "already queued" and "already in" are different facts.
        text = f"No change — {detail}"
        flash_ok = True
    elif code == 409:
        # Anything else the engine answers 409 with is a refusal (open
        # positions, no exchange keys for LIVE). This branch used to print
        # "Already in LIVE — no change" over every 409, refusals included.
        text = f"The engine refused {mode.upper()}: {detail}"
        flash_ok = False
    else:
        text = f"Auto-mode change failed: {detail}"
        flash_ok = False
    request.session["_control_flash"] = {"ok": flash_ok, "text": text}
    return RedirectResponse("/control#sec-mode", status_code=303)


@router.post("/control/kill-switch")
async def control_kill_switch(
    request: Request,
    engaged: str = Form(...),
    reason: str = Form(""),
):
    api = request.app.state.engine_api
    settings = request.app.state.settings
    engage = engaged.strip().lower() in ("1", "true", "on", "yes", "engage")

    result = await api.set_kill_switch(engage, reason=reason.strip() or None)
    ok = not _is_error(result)
    audit.record(
        settings.audit_log_path,
        action="kill_switch",
        params={"engaged": engage, "reason": reason.strip()},
        result=result if isinstance(result, dict) else {},
        ok=ok,
    )
    if ok:
        text = (
            "KILL SWITCH ENGAGED — all auto-trade halted."
            if engage
            else "Kill switch disengaged — auto-trade resumed."
        )
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        text = f"Kill-switch flip failed: {detail}"
    request.session["_control_flash"] = {"ok": ok, "text": text}
    return RedirectResponse("/control", status_code=303)


@router.post("/control/auto-trade-global")
async def control_auto_trade_global(request: Request, enabled: str = Form(...)):
    api = request.app.state.engine_api
    settings = request.app.state.settings
    enable = enabled.strip().lower() in ("1", "true", "on", "yes", "enable")

    result = await api.set_auto_trade_global(enable)
    ok = not _is_error(result)
    audit.record(
        settings.audit_log_path,
        action="auto_trade_global",
        params={"enabled": enable},
        result=result if isinstance(result, dict) else {},
        ok=ok,
    )
    if ok:
        text = (
            "Global auto-trade ENABLED — new orders allowed engine-wide."
            if enable
            else "Global auto-trade DISABLED — new orders halted (open "
            "positions untouched)."
        )
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        text = f"Global auto-trade flip failed: {detail}"
    request.session["_control_flash"] = {"ok": ok, "text": text}
    return RedirectResponse("/control", status_code=303)


@router.post("/control/signal-expiry")
async def control_signal_expiry(request: Request, enabled: str = Form(...)):
    api = request.app.state.engine_api
    settings = request.app.state.settings
    enable = enabled.strip().lower() in ("1", "true", "on", "yes", "enable")

    result = await api.set_signal_expiry(enable)
    ok = not _is_error(result)
    audit.record(
        settings.audit_log_path,
        action="signal_expiry",
        params={"enabled": enable},
        result=result if isinstance(result, dict) else {},
        ok=ok,
    )
    if ok:
        text = (
            "Signal expiry ENABLED — signals force-close at max hold time."
            if enable
            else "Signal expiry DISABLED — signals now run to TP or SL only "
            "(2h auto-trade reconciler safety net unaffected)."
        )
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        text = f"Signal-expiry flip failed: {detail}"
    request.session["_control_flash"] = {"ok": ok, "text": text}
    return RedirectResponse("/control", status_code=303)


@router.post("/control/billing")
async def control_billing(request: Request, enabled: str = Form(...)):
    """Turn the Google Play subscription paywall on/off engine-wide. Owner-gated
    on the engine; disabling stops NEW purchases + RTDN processing (existing
    subscribers keep their tier until it expires naturally)."""
    api = request.app.state.engine_api
    settings = request.app.state.settings
    enable = enabled.strip().lower() in ("1", "true", "on", "yes", "enable")

    result = await api.set_billing_enabled(enable)
    ok = not _is_error(result)
    audit.record(
        settings.audit_log_path,
        action="play_billing",
        params={"enabled": enable},
        result=result if isinstance(result, dict) else {},
        ok=ok,
    )
    if ok:
        text = (
            "Play billing ENABLED — subscription purchases are live."
            if enable
            else "Play billing DISABLED — new purchases blocked (existing "
            "subscribers keep their tier until it expires)."
        )
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        text = f"Play-billing flip failed: {detail}"
    request.session["_control_flash"] = {"ok": ok, "text": text}
    return RedirectResponse("/control", status_code=303)


#: Form fields that steer the handler and are never tunable values.
_FORM_META_KEYS = frozenset({"_bool_keys", "_str_keys", "_label", "_return"})

#: A return target is an element id on /control and nothing else, so a posted
#: value can never turn the redirect into a trip off the page.
_RETURN_ANCHOR = re.compile(r"^[A-Za-z0-9_-]{1,120}$")


def _return_to(form) -> str:
    """Where a tunables save lands: back at the switch that was tapped.

    Without it every save reloaded /control at the top with every category
    collapsed, so flipping three switches meant finding the category three
    times.
    """
    anchor = str(form.get("_return", "")).strip()
    return f"/control#{anchor}" if _RETURN_ANCHOR.match(anchor) else "/control"


@router.post("/control/tunables")
async def control_tunables(request: Request):
    """Update one or more engine runtime tunables (noise-floor stops, BE
    ratchet, cohort-edge gate). Reads the whole form so a single card can
    submit several knobs at once; checkboxes arrive as on/absent and are
    normalised against the ``_bool_keys`` companion field the template
    renders for every boolean tunable."""
    api = request.app.state.engine_api
    settings = request.app.state.settings

    form = await request.form()
    bool_keys = {k for k in str(form.get("_bool_keys", "")).split(",") if k}
    # Text knobs must submit even when blank. The skip-empty rule below exists
    # so an untouched numeric field does not post garbage, but for a string
    # tunable "" is a real value — the structural-snap per-path allow-list is
    # cleared by emptying it, and without this companion the list could be
    # added to from ops and never cleared.
    str_keys = {k for k in str(form.get("_str_keys", "")).split(",") if k}
    values: dict[str, object] = {}
    for key, raw in form.multi_items():
        if key in _FORM_META_KEYS:
            continue
        if key in bool_keys:
            continue  # handled below so unchecked boxes become False
        if key in str_keys:
            values[key] = str(raw).strip()
            continue
        if str(raw).strip() != "":
            values[key] = str(raw).strip()
    for key in bool_keys:
        values[key] = form.get(key) is not None

    result = await api.set_tunables(values)
    ok = not _is_error(result)
    audit.record(
        settings.audit_log_path,
        action="tunables_update",
        params={"values": {k: str(v) for k, v in values.items()}},
        result={"initialised": result.get("initialised")} if isinstance(result, dict) else {},
        ok=ok,
    )
    # A single switch names itself in the flash: "Mean revert live → OFF"
    # says what just happened, "1 value(s) updated" makes you look for it.
    label = str(form.get("_label", "")).strip()[:160]
    single = next(iter(values.items())) if len(values) == 1 else None
    if ok and single and label and single[0] in bool_keys:
        text = f"{label} → {'ON' if single[1] else 'OFF'}. Live within 5 seconds."
    elif ok:
        text = f"Engine tunables updated ({len(values)} value(s)) — live within 5 seconds."
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        what = f"{label}: " if single and label else ""
        text = f"{what}Tunables update failed: {detail}"
    request.session["_control_flash"] = {"ok": ok, "text": text}
    return RedirectResponse(_return_to(form), status_code=303)


@router.post("/control/reset-signals")
async def control_reset_signals(request: Request):
    """Full signal reset — clears active signals, history, stats, invalidation,
    and paper broker state for all users.  Requires explicit double-confirm in
    the UI (first form sets confirm=pending, second fires the actual reset)."""
    api = request.app.state.engine_api
    settings = request.app.state.settings

    result = await api.reset_signals()
    ok = not _is_error(result)
    audit.record(
        settings.audit_log_path,
        action="signal_reset_full",
        params={},
        result=result if isinstance(result, dict) else {},
        ok=ok,
    )
    if ok:
        active = result.get("cleared_active_signals", 0) if isinstance(result, dict) else 0
        history = result.get("cleared_history", 0) if isinstance(result, dict) else 0
        paper = result.get("paper_positions_closed", 0) if isinstance(result, dict) else 0
        queued = result.get("engine_reset_queued", False) if isinstance(result, dict) else False
        queued_note = " (engine reset queued, propagates in ≤15s)" if queued else ""
        text = (
            f"Full reset complete{queued_note}: "
            f"{active} active signals, {history} history, {paper} paper positions cleared."
        )
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        text = f"Full reset failed: {detail}"
    request.session["_control_flash"] = {"ok": ok, "text": text}
    return RedirectResponse("/control", status_code=303)



@router.post("/control/close-signal")
async def control_close_signal(
    request: Request,
    signal_id: str = Form(...),
    redirect_to: str = Form("/signals"),
):
    """Force-close ONE stuck OPEN signal (the "Close" button on the Signals
    feed). Owner-gated + audited; PRG back to the referring page."""
    api = request.app.state.engine_api
    settings = request.app.state.settings
    signal_id = (signal_id or "").strip()
    # Only ever redirect to an in-app path (no open-redirect).  A single
    # leading slash is not sufficient: "//evil.example.com" passes a bare
    # startswith("/") check and browsers read it as protocol-relative, so the
    # redirect leaves the app entirely.  Require exactly one leading slash.
    dest = (
        redirect_to
        if redirect_to.startswith("/") and not redirect_to.startswith("//")
        else "/signals"
    )

    if not signal_id:
        request.session["_control_flash"] = {"ok": False, "text": "No signal id supplied."}
        return RedirectResponse(dest, status_code=303)

    result = await api.close_signal(signal_id)
    ok = not _is_error(result)
    audit.record(
        settings.audit_log_path,
        action="close_signal",
        params={"signal_id": signal_id},
        result=result if isinstance(result, dict) else {},
        ok=ok,
    )
    if ok and isinstance(result, dict):
        if result.get("closed") is True:
            pnl = result.get("pnl_pct")
            pnl_s = f" at {pnl:+.2f}%" if isinstance(pnl, (int, float)) else ""
            text = f"Closed {signal_id}{pnl_s}."
        elif result.get("closed") is None:
            text = f"Close queued for {signal_id} — refresh shortly to confirm."
        else:
            text = f"{signal_id} was already closed / not in the active book."
    elif ok:
        text = f"Close requested for {signal_id}."
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        text = f"Close failed for {signal_id}: {detail}"
    request.session["_control_flash"] = {"ok": ok, "text": text}
    return RedirectResponse(dest, status_code=303)

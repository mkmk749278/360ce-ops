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

import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import audit
from app.routes.positions import _enrich_row

router = APIRouter()

_VALID_MODES = {"off", "paper", "live"}

#: (form value, label, seconds). Deliberately short by default — a read-only
#: grant that outlives the reason it was minted is the one nobody remembers to
#: revoke, and the whole tier exists to be temporary.
_GUEST_TTL_CHOICES: list[tuple[str, str, int]] = [
    ("1h", "1 hour", 3600),
    ("6h", "6 hours", 6 * 3600),
    ("24h", "24 hours", 24 * 3600),
    ("7d", "7 days", 7 * 86400),
]
_GUEST_TTLS = {key: sec for key, _label, sec in _GUEST_TTL_CHOICES}


def _is_error(result: object) -> bool:
    return isinstance(result, dict) and bool(result.get("error"))


async def _render(request: Request):
    api = request.app.state.engine_api
    settings = request.app.state.settings
    templates = request.app.state.templates

    auto = await api.auto_mode()
    ks = await api.kill_switch_state()
    glob = await api.auto_trade_global_state()
    expiry = await api.signal_expiry_state()
    billing = await api.billing_enabled_state()
    tunables = await api.tunables_state()
    flash = request.session.pop("_control_flash", None)

    # Group tunables by category so the template renders one card per
    # concern ("Stops & exits", "Signal gating") with per-knob explanations.
    tunable_groups: dict[str, list] = {}
    tunables_initialised = False
    if isinstance(tunables, dict) and not tunables.get("error"):
        tunables_initialised = bool(tunables.get("initialised"))
        for entry in tunables.get("tunables") or []:
            if isinstance(entry, dict):
                tunable_groups.setdefault(
                    str(entry.get("category") or "Other"), []
                ).append(entry)

    # A freshly-minted guest code is shown exactly once, on the redirect that
    # follows the mint. It is held in process memory keyed by a one-shot nonce
    # in the flash rather than in the flash itself, so the plaintext code never
    # rides in the session cookie and a refresh cannot re-display it.
    new_code = None
    if isinstance(flash, dict) and flash.get("code_nonce"):
        pending = getattr(request.app.state, "guest_pending_codes", None) or {}
        new_code = pending.pop(flash["code_nonce"], None)

    return templates.TemplateResponse(
        "control.html",
        {
            "request": request,
            "active": "control",
            "auto": auto if isinstance(auto, dict) else {},
            "ks": ks if isinstance(ks, dict) else {},
            "glob": glob if isinstance(glob, dict) else {},
            "expiry": expiry if isinstance(expiry, dict) else {},
            "billing": billing if isinstance(billing, dict) else {},
            "tunable_groups": tunable_groups,
            "tunables_initialised": tunables_initialised,
            "audit": audit.tail(settings.audit_log_path, limit=25),
            "guest_grants": request.app.state.guest_access.list_grants(),
            "guest_ttl_choices": _GUEST_TTL_CHOICES,
            "guest_new_code": new_code,
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
    if ok:
        text = f"Auto-mode set to {mode.upper()}."
        flash_ok = True
    elif code == 409:
        # Engine was already in that mode — a no-op, not a real failure.
        text = f"Already in {mode.upper()} — no change."
        flash_ok = True
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        text = f"Auto-mode change failed: {detail}"
        flash_ok = False
    request.session["_control_flash"] = {"ok": flash_ok, "text": text}
    return RedirectResponse("/control", status_code=303)


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
        if key in ("_bool_keys", "_str_keys"):
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
    if ok:
        text = f"Engine tunables updated ({len(values)} value(s)) — live within 5 seconds."
    else:
        detail = result.get("error") if isinstance(result, dict) else result
        text = f"Tunables update failed: {detail}"
    request.session["_control_flash"] = {"ok": ok, "text": text}
    return RedirectResponse("/control", status_code=303)


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


@router.post("/control/guest-access/issue")
async def control_guest_issue(
    request: Request,
    label: str = Form(""),
    ttl: str = Form("6h"),
):
    """Mint a temporary read-only access code.

    The code grants ``GET`` on the classified read pages only — never the
    control panel, the diag runner, the subscriber tables or the ``/api/v1``
    token surface (``app/guest_scope.py`` holds the table, and a route nobody
    has classified is denied). It is displayed once here and cannot be
    recovered afterwards; the store keeps only its SHA-256 hash."""
    settings = request.app.state.settings
    store = request.app.state.guest_access
    ttl_key = (ttl or "").strip()
    ttl_sec = _GUEST_TTLS.get(ttl_key)
    if ttl_sec is None:
        request.session["_control_flash"] = {
            "ok": False,
            "text": f"Rejected — unknown duration {ttl_key!r}.",
        }
        return RedirectResponse("/control", status_code=303)

    code, grant_id = store.issue(label=label, ttl_sec=ttl_sec)
    # Hand the plaintext to the next render through process memory, not the
    # session cookie. Popped on display; a restart just loses it, which costs
    # one re-mint.
    pending = getattr(request.app.state, "guest_pending_codes", None)
    if pending is None:
        pending = {}
        request.app.state.guest_pending_codes = pending
    nonce = secrets.token_hex(8)
    pending[nonce] = code

    audit.record(
        settings.audit_log_path,
        action="guest_access_issued",
        params={"label": label or "guest", "ttl": ttl_key, "grant_id": grant_id},
        result={},
        ok=True,
    )
    request.session["_control_flash"] = {
        "ok": True,
        "text": f"Read-only code minted ({ttl_key}). Copy it now — it is not shown again.",
        "code_nonce": nonce,
    }
    return RedirectResponse("/control", status_code=303)


@router.post("/control/guest-access/revoke")
async def control_guest_revoke(
    request: Request,
    grant_id: str = Form(""),
    scope: str = Form("one"),
):
    """Kill one read-only grant, or all of them.

    Revocation takes effect on the guest's **next request** — the session holds
    only the grant id and the middleware re-reads the grant every time — so
    there is no window in which a revoked code keeps working."""
    settings = request.app.state.settings
    store = request.app.state.guest_access

    if scope == "all":
        n = store.revoke_all()
        audit.record(
            settings.audit_log_path,
            action="guest_access_revoked_all",
            params={"count": n},
            result={},
            ok=True,
        )
        text = f"Revoked {n} read-only code(s). Any open guest session ends on its next request."
        request.session["_control_flash"] = {"ok": True, "text": text}
        return RedirectResponse("/control", status_code=303)

    grant_id = (grant_id or "").strip()
    ok = store.revoke(grant_id) if grant_id else False
    audit.record(
        settings.audit_log_path,
        action="guest_access_revoked",
        params={"grant_id": grant_id},
        result={} if ok else {"error": "not found or already dead"},
        ok=ok,
    )
    text = (
        "Read-only code revoked — the session ends on its next request."
        if ok
        else "Nothing to revoke: that code is already revoked or expired."
    )
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

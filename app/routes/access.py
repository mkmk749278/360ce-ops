"""``/control/access`` — the read-only access sub-tab.

Minting and revoking temporary read-only codes (``app/guest_access.py``), on
their own page under Control rather than as a card on the engine page. Two
reasons it is separated:

* **It is not an engine control.** Everything else on ``/control`` writes to the
  engine — auto-mode, kill switch, tunables, expiry — and this writes to ops'
  own access store. Sitting them together invited reading a revoke as an engine
  action, and the engine page is long enough that a card at the bottom is where
  a code goes to be forgotten.
* **The audit is different.** Engine actions are answered by the engine and
  reported back; these are answered locally, so the page carries its own flash
  (``_access_flash``) rather than sharing the control page's. Two writers on one
  flash key means an action can render its result on a page the operator did not
  come from.

Everything else is unchanged: the code is displayed exactly once, passed to that
render through process memory rather than the session cookie so a refresh cannot
re-display it, and only its SHA-256 hash is ever persisted.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import audit

router = APIRouter()

#: (form value, label, seconds). Deliberately short by default — a read-only
#: grant that outlives the reason it was minted is the one nobody remembers to
#: revoke, and the whole tier exists to be temporary.
GUEST_TTL_CHOICES: list[tuple[str, str, int]] = [
    ("1h", "1 hour", 3600),
    ("6h", "6 hours", 6 * 3600),
    ("24h", "24 hours", 24 * 3600),
    ("7d", "7 days", 7 * 86400),
]
_GUEST_TTLS = {key: sec for key, _label, sec in GUEST_TTL_CHOICES}


def _has_route(request: Request, name: str) -> bool:
    """Is ``name`` a registered route on this app?

    ``url_for`` raises when it is not, and this page must not 500 because a
    route was renamed — the whole card exists so a one-shot code can be handed
    over reliably.
    """
    try:
        request.url_for(name)
        return True
    except Exception:
        return False


@router.get("/control/access")
async def access_page(request: Request):
    templates = request.app.state.templates
    settings = request.app.state.settings
    store = request.app.state.guest_access
    flash = request.session.pop("_access_flash", None)

    # A freshly-minted code is shown exactly once, on the redirect that follows
    # the mint. It is held in process memory keyed by a one-shot nonce in the
    # flash rather than in the flash itself, so the plaintext never rides in the
    # session cookie and a refresh cannot re-display it.
    new_code = None
    if isinstance(flash, dict) and flash.get("code_nonce"):
        pending = getattr(request.app.state, "guest_pending_codes", None) or {}
        new_code = pending.pop(flash["code_nonce"], None)

    # Only the access-related rows, so the page reports on itself rather than
    # showing the engine's control history a second time.
    rows = [
        e
        for e in audit.tail(settings.audit_log_path, limit=400)
        if str(e.get("action", "")).startswith("guest_")
    ][:25]

    return templates.TemplateResponse(
        "control_access.html",
        {
            "request": request,
            "active": "access",
            "grants": store.list_grants(),
            "ttl_choices": GUEST_TTL_CHOICES,
            "new_code": new_code,
            # The URL the holder actually opens, derived from the request rather
            # than hardcoded: the page is reachable on the deployed host and on
            # a local dev server, and a copy button that hands over the wrong
            # host is worse than no copy button — the code is shown once and
            # cannot be re-displayed if the hand-off fails.
            "guest_url": str(request.url_for("guest_get"))
            if _has_route(request, "guest_get")
            else str(request.base_url).rstrip("/") + "/guest",
            "audit": rows,
            "flash": flash,
        },
    )


@router.post("/control/access/issue")
async def access_issue(
    request: Request,
    label: str = Form(""),
    ttl: str = Form("6h"),
):
    """Mint a temporary read-only access code.

    The code grants ``GET`` on the classified read pages only — never the
    control panel, the diag runner, the subscriber tables or the ``/api/v1``
    token surface (``app/guest_scope.py`` holds the table, and a route nobody
    has classified is denied). It is displayed once and cannot be recovered
    afterwards; the store keeps only its SHA-256 hash."""
    settings = request.app.state.settings
    store = request.app.state.guest_access
    ttl_key = (ttl or "").strip()
    ttl_sec = _GUEST_TTLS.get(ttl_key)
    if ttl_sec is None:
        request.session["_access_flash"] = {
            "ok": False,
            "text": f"Rejected — unknown duration {ttl_key!r}.",
        }
        return RedirectResponse("/control/access", status_code=303)

    code, grant_id = store.issue(label=label, ttl_sec=ttl_sec)
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
    request.session["_access_flash"] = {
        "ok": True,
        "text": f"Read-only code minted ({ttl_key}). Copy it now — it is not shown again.",
        "code_nonce": nonce,
    }
    return RedirectResponse("/control/access", status_code=303)


@router.post("/control/access/revoke")
async def access_revoke(
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
        request.session["_access_flash"] = {"ok": True, "text": text}
        return RedirectResponse("/control/access", status_code=303)

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
    request.session["_access_flash"] = {"ok": ok, "text": text}
    return RedirectResponse("/control/access", status_code=303)

"""Referral commission payouts — the owner's manual-settlement surface.

Referral Phase 2 (360-v2, 2026-07-21) accrues 50% of each verified paid
billing period of a referred user (first 3 periods) to the engine's
``referral_commissions`` ledger. Payouts are deliberately manual — the
owner pays the referrer directly (UPI etc., the listing carries the
referrer's phone as payout identity) and then marks the rows paid here.

Control doctrine as everywhere: engine's owner-gated endpoints only
(``GET /api/referral/admin/commissions`` + ``POST .../mark-paid``),
POST→redirect→GET with an explicit confirm, every write audited, and the
page always re-reads engine state after a write — ops holds nothing.
"""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import audit

router = APIRouter()

_VALID_FILTERS = {"", "accrued", "paid"}


def _is_error(result: object) -> bool:
    return isinstance(result, dict) and bool(result.get("error"))


def _totals_by_currency(items: list[dict]) -> list[dict]:
    """Per-currency accrued/paid sums — currencies never sum across each
    other (Play accrues INR, the web rail USD)."""
    buckets: dict[str, dict[str, float]] = {}
    for row in items:
        currency = str(row.get("currency") or "?")
        bucket = buckets.setdefault(currency, {"accrued": 0.0, "paid": 0.0})
        key = "paid" if row.get("status") == "paid" else "accrued"
        try:
            bucket[key] += float(row.get("amount") or 0.0)
        except (TypeError, ValueError):
            continue
    return [
        {"currency": currency, **sums} for currency, sums in sorted(buckets.items())
    ]


@router.get("/control/referrals")
async def referrals_page(request: Request, status: str = ""):
    api = request.app.state.engine_api
    templates = request.app.state.templates
    flash = request.session.pop("_control_flash", None)

    status = status.strip().lower()
    if status not in _VALID_FILTERS:
        status = ""

    result = await api.referral_commissions(status or None)
    items: list[dict] = []
    error = None
    if _is_error(result):
        error = result.get("error")
    elif isinstance(result, dict):
        items = [row for row in result.get("items", []) if isinstance(row, dict)]

    return templates.TemplateResponse(
        "referrals.html",
        {
            "request": request,
            "active": "referrals",
            "status": status,
            "items": items,
            "totals": _totals_by_currency(items),
            "error": error,
            "flash": flash,
        },
    )


@router.post("/control/referrals/mark-paid")
async def referrals_mark_paid(request: Request):
    api = request.app.state.engine_api
    settings = request.app.state.settings

    form = await request.form()
    ids: list[int] = []
    for raw in form.getlist("commission_ids"):
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    if not ids:
        request.session["_control_flash"] = {
            "ok": False,
            "text": "Nothing selected — tick the rows you have paid out first.",
        }
        return RedirectResponse("/control/referrals", status_code=303)

    result = await api.mark_referral_commissions_paid(ids)
    ok = not _is_error(result)
    updated = result.get("updated") if isinstance(result, dict) else None
    audit.record(
        settings.audit_log_path,
        action="referral_commissions_mark_paid",
        params={"commission_ids": ids},
        ok=ok,
        result=result if isinstance(result, dict) else {"raw": str(result)},
    )
    request.session["_control_flash"] = {
        "ok": ok,
        "text": (
            f"Marked {updated} commission row(s) paid."
            if ok
            else f"Mark-paid failed: {result.get('error')}"
        ),
    }
    return RedirectResponse("/control/referrals", status_code=303)

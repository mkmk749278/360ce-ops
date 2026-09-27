"""``/control/coindcx`` — the CoinDCX venue, and the owner's real-account test.

Engine side: ``src/venues/coindcx/`` in 360-v2. CoinDCX auto-trade ships
DARK (``COINDCX_EXECUTION_ENABLED`` off). Before any user trades on it, the
owner runs the self-test on HIS OWN account at a tiny size (owner decision,
2026-09-27): it opens a minimum position, rests a stop and a target
together, reads them back, exits, and confirms nothing is left open. This page
is where that test is started and read, and where the venue's health is read
every day after.

Owner-only, deliberately. It renders the owner's uid and wallet balance, and
it hosts a control that places a REAL order. A read-only guest has no reason
to see either, and "a control that 403s is indistinguishable from a broken
page".

Control doctrine, as everywhere under ``/control``:

* **PRG + confirm.** The test places a real order, so it needs an explicit
  confirm, and POST redirects so a refresh cannot place a second one.
* **Audited on every outcome** — the refusal is exactly what the log needs.
* **The engine is the source of truth.** The page never says "passed" because
  the click was accepted: the engine only QUEUES the run, and the verdict is
  read back from the report file the engine writes when the run ends.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import audit
from app.data_sources import coindcx as dcx

router = APIRouter()

#: The engine's uid field is 4–128 chars; a Firebase uid is alphanumeric.
_UID_RE = re.compile(r"^[A-Za-z0-9_-]{4,128}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")
MARGIN_CHOICES = ("USDT", "INR")
DEFAULT_SYMBOL = "DOGEUSDT"
AUDIT_ACTION = "coindcx_self_test"


def _last_request_at(rows: list[dict]) -> float | None:
    for row in rows:
        if row.get("action") == AUDIT_ACTION and row.get("ok"):
            try:
                return datetime.fromisoformat(str(row.get("ts"))).timestamp()
            except ValueError:
                return None
    return None


@router.get("/control/coindcx")
async def coindcx_page(request: Request):
    settings = request.app.state.settings
    dv = request.app.state.data_volume
    now = time.time()
    try:
        status = dv.coindcx_status()
    except Exception as exc:  # noqa: BLE001 — named, never blank
        status = {"error": f"read: {type(exc).__name__}: {exc}"}
    try:
        report = dv.coindcx_self_test()
    except Exception as exc:  # noqa: BLE001
        report = {"error": f"read: {type(exc).__name__}: {exc}"}

    rows = [
        e for e in audit.tail(settings.audit_log_path, limit=400)
        if str(e.get("action", "")).startswith("coindcx_")
    ][:15]
    grade = dcx.grade_status(status, now=now)
    ok_status = status if grade["state"] in ("running", "stale") else {}
    return request.app.state.templates.TemplateResponse(
        "control_coindcx.html",
        {
            "request": request,
            "active": "coindcx",
            "grade": grade,
            "status": ok_status,
            "execution": dcx.execution_view(ok_status) if ok_status else None,
            "stream": dcx.stream_view(ok_status) if ok_status else None,
            "counters": {
                "Reconciler": dcx.counter_rows((ok_status.get("reconciler") or {}).get("stats")),
                "Executor": dcx.counter_rows(ok_status.get("executor")),
                "Dispatch": dcx.counter_rows(ok_status.get("dispatch")),
                "Instrument feed": dcx.counter_rows((ok_status.get("instruments") or {}).get("stats")),
            },
            "self_test": dcx.grade_self_test(
                report, last_request_at=_last_request_at(rows), now=now),
            "stale_cycles": dcx.STALE_CYCLES,
            "margin_choices": MARGIN_CHOICES,
            "default_symbol": DEFAULT_SYMBOL,
            "audit": rows,
            "flash": request.session.pop("_coindcx_flash", None),
        },
    )


@router.post("/control/coindcx/self-test")
async def coindcx_self_test(
    request: Request,
    uid: str = Form(""),
    symbol: str = Form(DEFAULT_SYMBOL),
    margin_currency: str = Form("USDT"),
    confirm: str = Form(""),
):
    """Queue the real-account self-test. Places a REAL minimum-size order."""
    settings = request.app.state.settings
    uid = (uid or "").strip()
    symbol = (symbol or "").strip().upper()
    margin = (margin_currency or "").strip().upper()
    params = {"uid": uid, "symbol": symbol, "margin_currency": margin}

    refusal = None
    if confirm != "yes":
        refusal = "Tick the confirmation: this places a real order on CoinDCX."
    elif not _UID_RE.match(uid):
        refusal = "That is not a Firebase uid (letters, digits, - and _ only)."
    elif not _SYMBOL_RE.match(symbol):
        refusal = f"{symbol!r} is not a USDT futures symbol (e.g. DOGEUSDT)."
    elif margin not in MARGIN_CHOICES:
        refusal = f"Margin must be one of {', '.join(MARGIN_CHOICES)}."
    if refusal:
        audit.record(settings.audit_log_path, action=AUDIT_ACTION, params=params,
                     result={"error": refusal}, ok=False)
        request.session["_coindcx_flash"] = {"ok": False, "text": refusal}
        return RedirectResponse("/control/coindcx", status_code=303)

    result = await request.app.state.engine_api.coindcx_self_test(uid, symbol, margin)
    ok = isinstance(result, dict) and result.get("queued") is True
    audit.record(settings.audit_log_path, action=AUDIT_ACTION, params=params,
                 result=result if isinstance(result, dict) else {"error": str(result)}, ok=ok)
    if ok:
        text = (f"Self-test queued ({symbol}, {margin} margin). The engine writes the "
                "report when the run ends — about a minute. This page shows "
                "“requested, no report yet” until then.")
    else:
        err = result.get("error") if isinstance(result, dict) else result
        code = result.get("status_code") if isinstance(result, dict) else None
        text = f"The engine refused the self-test{f' (HTTP {code})' if code else ''}: {err}"
    request.session["_coindcx_flash"] = {"ok": ok, "text": text}
    return RedirectResponse("/control/coindcx", status_code=303)

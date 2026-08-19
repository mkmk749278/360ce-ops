"""Alerts panel — surfaces the monitoring agent's live safety alerts.

Reads the agent's ``alert:state:*`` keys from the shared Redis and renders
active alerts, HIGH first. Same state the agent writes, so what is on this page
is what paged.

**Two sentences in this module were false and shipped for weeks** (corrected
2026-08-06). It said Telegram was "banned in-region" — `CLAUDE.md` carries the
2026-07-25 correction that it is not, and never was — and it said "there is no
push", which stopped being true when Phase 4 wired FCM (``app/fcm.py``, tokens
in ``app/device_registry.py``). Together they told the owner that this page was
the *only* way he would learn about a naked position. That is the one direction
an alert surface must never be wrong in, and nothing in the suite could catch it
because a docstring and a paragraph are not asserted.

So the page no longer *asserts* a delivery path: it reads whether push is
actually armed (a service account is configured **and** a device is registered)
and says which of those is missing when it is not. Push is disabled-safe by
design — no ``FIREBASE_SERVICE_ACCOUNT`` means every send is a silent no-op —
and a page claiming "you will be paged" over that configuration is the same
defect wearing the opposite sign.

**And it happened a third time** (2026-08-19). The fix above graded FCM and
*only* FCM, then kept the old conclusion — "Nothing pages you — this page is
the only way you learn about a naked position" — on a box where the agent's
**Telegram** sink was configured and delivering; those were the
``redis_unreachable`` pages the owner had been receiving all along. Reading one
sink and asserting about all of them is the same class as reading none.

The repair is not a third assertion and not a second copy of the config: the
web container does not even receive ``AGENT_TELEGRAM_*``, so any check here
would be a guess. The **agent** is the one process that knows what it can send
through, so it publishes its armed sinks in its heartbeat
(``Notifier.armed_sinks`` -> ``app/agent/heartbeat.py``) and this page renders
them. One writer, one reader. An agent that predates the field publishes
nothing, and that renders as *not reported* — never as "nothing pages you",
which is the sentence this whole history is about.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/alerts")
async def alerts_page(request: Request):
    reader = request.app.state.agent_alerts
    templates = request.app.state.templates
    settings = request.app.state.settings
    payload = await reader.active_alerts()
    alerts = payload.get("alerts", [])
    # Read the delivery path off the configuration rather than describing it in
    # prose. Both halves are required: a service account with no registered
    # device pages nobody, and a registered device with no service account is a
    # no-op send.
    devices = request.app.state.device_registry.count()
    configured = bool(settings.fcm_service_account)
    push = {
        "armed": configured and devices > 0,
        "configured": configured,
        "devices": devices,
    }
    # The other sinks come from the agent, because the agent is what holds
    # them. `reported` is tri-state on purpose: an agent that predates this
    # field, or one we could not read, is NOT the same as one with no sink
    # armed, and only the second justifies telling the owner nothing pages him.
    hb = await reader.heartbeat()
    raw_sinks = hb.get("sinks") if isinstance(hb, dict) else None
    delivery = {
        "reported": isinstance(raw_sinks, dict) and bool(raw_sinks),
        "telegram": bool((raw_sinks or {}).get("telegram")),
        "healthchecks": bool((raw_sinks or {}).get("healthchecks")),
        "agent_seen": bool(hb.get("present")) if isinstance(hb, dict) else False,
    }
    # "Nothing pages you" may only be said when every path we can observe is
    # dark AND the agent actually told us so. Computed here, once, rather than
    # re-derived in the template — this sentence has been wrong three times.
    delivery["silent"] = (
        delivery["reported"]
        and not push["armed"]
        and not delivery["telegram"]
    )
    return templates.TemplateResponse(
        "alerts.html",
        {
            "request": request,
            "active": "alerts",
            "alerts": alerts,
            "error": payload.get("error"),
            "high_count": sum(1 for a in alerts if a.get("severity") == "HIGH"),
            "push": push,
            "delivery": delivery,
        },
    )

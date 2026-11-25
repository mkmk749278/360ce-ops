"""Pulse: engine health + auto-mode + heartbeat."""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.pulse_view import liveness_view, pulse_view

router = APIRouter()


@router.get("/")
async def pulse(request: Request):
    api = request.app.state.engine_api
    logs = request.app.state.monitor_logs
    pulse_data = await api.pulse()
    auto = await api.auto_mode()
    heartbeat = await logs.heartbeat()
    liveness = request.app.state.data_volume.feature_liveness()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "pulse.html",
        {
            "request": request,
            "pulse": pulse_data,
            "auto_mode": auto,
            "view": pulse_view(pulse_data, auto),
            "heartbeat": heartbeat,
            "liveness": liveness,
            "lv": liveness_view(liveness),
            "active": "pulse",
        },
    )


@router.get("/_partial/pulse")
async def pulse_partial(request: Request):
    api = request.app.state.engine_api
    pulse_data = await api.pulse()
    auto = await api.auto_mode()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "_pulse_card.html",
        {"request": request, "pulse": pulse_data, "auto_mode": auto,
         "view": pulse_view(pulse_data, auto)},
    )

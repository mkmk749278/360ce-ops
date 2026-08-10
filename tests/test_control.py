"""Tests for the engine control plane (ops' first write surface, 2026-06-20).

Covers the audit-log round trip and the control routes (auth gate,
auto-mode flip, kill-switch engage/disengage) with the engine client
monkeypatched — we assert ops calls the right engine method, records an
audit entry, and surfaces the result via the PRG flash.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app import audit  # noqa: E402
from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import control as control_route  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


async def _fake_glob(self):
    return {"enabled": True, "initialised": True}


# ---- audit log ----------------------------------------------------------


def test_audit_record_and_tail_round_trip(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    audit.record(path, action="auto_mode", params={"mode": "paper"},
                 result={}, ok=True)
    audit.record(path, action="kill_switch", params={"engaged": True},
                 result={"error": "boom"}, ok=False)
    rows = audit.tail(path, limit=10)
    # Newest first.
    assert rows[0]["action"] == "kill_switch"
    assert rows[0]["ok"] is False
    assert rows[0]["result"] == "boom"
    assert rows[1]["action"] == "auto_mode"
    assert rows[1]["ok"] is True


def test_audit_tail_missing_file_is_empty():
    assert audit.tail("/nonexistent/path/audit.jsonl") == []


def test_audit_record_bad_path_does_not_raise():
    # A control action must never blow up because the audit volume is
    # unwritable — record swallows the OSError.
    audit.record("/proc/cannot/write/here.jsonl", action="x",
                 params={}, result={}, ok=True)


# ---- control routes -----------------------------------------------------


def test_control_page_requires_auth():
    with TestClient(app) as client:
        r = client.get("/control", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


def _patch_reads(monkeypatch, *, mode="paper"):
    """Monkeypatch the three read calls _render makes, so control tests
    don't hit the network."""
    async def fake_auto_mode(self):
        return {"mode": mode}

    async def fake_ks(self):
        return {"engaged": False, "initialised": True, "reason": None}

    async def fake_glob(self):
        return {"enabled": True, "initialised": True}

    async def fake_billing(self):
        return {"enabled": True, "configured": True, "initialised": True}

    monkeypatch.setattr(EngineApiClient, "auto_mode", fake_auto_mode)
    monkeypatch.setattr(EngineApiClient, "kill_switch_state", fake_ks)
    monkeypatch.setattr(EngineApiClient, "auto_trade_global_state", fake_glob)
    monkeypatch.setattr(EngineApiClient, "billing_enabled_state", fake_billing)
    monkeypatch.setattr(control_route.audit, "tail", lambda *a, **k: [])


def test_control_page_renders_state(monkeypatch):
    _patch_reads(monkeypatch, mode="paper")
    with TestClient(app) as client:
        _login(client)
        r = client.get("/control")
        assert r.status_code == 200
        assert "Engine Control" in r.text
        assert "PAPER" in r.text  # current mode surfaced


def test_auto_mode_flip_calls_engine_and_audits(monkeypatch):
    calls: dict = {}

    async def fake_set(self, mode):
        calls["mode"] = mode
        return {"success": True, "mode": mode}

    async def fake_auto_mode(self):
        return {"mode": calls.get("mode", "off")}

    async def fake_ks(self):
        return {"engaged": False, "initialised": True}

    async def fake_glob(self):
        return {"enabled": True, "initialised": True}

    recorded: list = []
    monkeypatch.setattr(EngineApiClient, "auto_trade_global_state", fake_glob)
    monkeypatch.setattr(EngineApiClient, "set_auto_mode", fake_set)
    monkeypatch.setattr(EngineApiClient, "auto_mode", fake_auto_mode)
    monkeypatch.setattr(EngineApiClient, "kill_switch_state", fake_ks)
    monkeypatch.setattr(control_route.audit, "tail", lambda *a, **k: [])
    monkeypatch.setattr(
        control_route.audit, "record",
        lambda *a, **k: recorded.append(k),
    )

    with TestClient(app) as client:
        _login(client)
        r = client.post("/control/auto-mode", data={"mode": "live"})
        assert r.status_code == 200  # followed the 303 to /control
        assert calls["mode"] == "live"
        assert recorded and recorded[0]["action"] == "auto_mode"
        assert recorded[0]["ok"] is True
        assert "Auto-mode set to LIVE" in r.text


def test_auto_mode_invalid_is_rejected_without_engine_call(monkeypatch):
    called = {"set": False}

    async def fake_set(self, mode):
        called["set"] = True
        return {"success": True}

    async def fake_auto_mode(self):
        return {"mode": "off"}

    async def fake_ks(self):
        return {"engaged": False, "initialised": True}

    monkeypatch.setattr(EngineApiClient, "set_auto_mode", fake_set)
    monkeypatch.setattr(EngineApiClient, "auto_mode", fake_auto_mode)
    monkeypatch.setattr(EngineApiClient, "kill_switch_state", fake_ks)
    monkeypatch.setattr(EngineApiClient, "auto_trade_global_state", _fake_glob)
    monkeypatch.setattr(control_route.audit, "tail", lambda *a, **k: [])

    with TestClient(app) as client:
        _login(client)
        r = client.post("/control/auto-mode", data={"mode": "yolo"})
        assert r.status_code == 200
        assert called["set"] is False
        assert "invalid mode" in r.text.lower()


def test_kill_switch_engage_calls_engine_and_audits(monkeypatch):
    calls: dict = {}

    async def fake_set_ks(self, engaged, reason=None):
        calls["engaged"] = engaged
        calls["reason"] = reason
        return {"engaged": engaged, "initialised": True, "reason": reason}

    async def fake_auto_mode(self):
        return {"mode": "off"}

    async def fake_ks(self):
        return {"engaged": calls.get("engaged", False), "initialised": True}

    recorded: list = []
    monkeypatch.setattr(EngineApiClient, "set_kill_switch", fake_set_ks)
    monkeypatch.setattr(EngineApiClient, "auto_mode", fake_auto_mode)
    monkeypatch.setattr(EngineApiClient, "kill_switch_state", fake_ks)
    monkeypatch.setattr(EngineApiClient, "auto_trade_global_state", _fake_glob)
    monkeypatch.setattr(control_route.audit, "tail", lambda *a, **k: [])
    monkeypatch.setattr(
        control_route.audit, "record",
        lambda *a, **k: recorded.append(k),
    )

    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/kill-switch",
            data={"engaged": "true", "reason": "manual halt"},
        )
        assert r.status_code == 200
        assert calls["engaged"] is True
        assert calls["reason"] == "manual halt"
        assert recorded[0]["action"] == "kill_switch"
        assert "ENGAGED" in r.text


def test_control_positions_partial_renders_open_positions(monkeypatch):
    async def fake_diag(self):
        return {
            "monitor_running": True,
            "items": [
                {"status": "ACTIVE", "symbol": "BTCUSDT", "direction": "long",
                 "entry": 65000.0, "current_price": 65500.0, "stop_loss": 64000.0,
                 "pnl_pct": 0.77, "minutes_open": 12, "signal_id": "abc"},
                # Phantom placeholder (no symbol / zero entry) — must be filtered.
                {"status": "ACTIVE", "symbol": "", "entry": 0.0, "signal_id": "x"},
            ],
        }

    monkeypatch.setattr(EngineApiClient, "positions_diag", fake_diag)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/control/positions")
        assert r.status_code == 200
        assert "BTCUSDT" in r.text
        assert "1 open" in r.text  # phantom row filtered out


def test_control_positions_partial_empty(monkeypatch):
    async def fake_diag(self):
        return {"monitor_running": True, "items": []}

    monkeypatch.setattr(EngineApiClient, "positions_diag", fake_diag)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/control/positions")
        assert r.status_code == 200
        assert "No open positions" in r.text


def test_auto_trade_global_flip_calls_engine_and_audits(monkeypatch):
    calls: dict = {}

    async def fake_set_glob(self, enabled):
        calls["enabled"] = enabled
        return {"enabled": enabled, "initialised": True}

    async def fake_glob(self):
        return {"enabled": calls.get("enabled", False), "initialised": True}

    async def fake_auto_mode(self):
        return {"mode": "off"}

    async def fake_ks(self):
        return {"engaged": False, "initialised": True}

    recorded: list = []
    monkeypatch.setattr(EngineApiClient, "set_auto_trade_global", fake_set_glob)
    monkeypatch.setattr(EngineApiClient, "auto_trade_global_state", fake_glob)
    monkeypatch.setattr(EngineApiClient, "auto_mode", fake_auto_mode)
    monkeypatch.setattr(EngineApiClient, "kill_switch_state", fake_ks)
    monkeypatch.setattr(control_route.audit, "tail", lambda *a, **k: [])
    monkeypatch.setattr(
        control_route.audit, "record",
        lambda *a, **k: recorded.append(k),
    )

    with TestClient(app) as client:
        _login(client)
        r = client.post("/control/auto-trade-global", data={"enabled": "false"})
        assert r.status_code == 200
        assert calls["enabled"] is False
        assert recorded[0]["action"] == "auto_trade_global"
        assert "DISABLED" in r.text


def test_billing_flip_calls_engine_and_audits(monkeypatch):
    calls: dict = {}

    async def fake_set_billing(self, enabled):
        calls["enabled"] = enabled
        return {"enabled": enabled, "configured": True, "initialised": True}

    async def fake_billing(self):
        return {
            "enabled": calls.get("enabled", True),
            "configured": True,
            "initialised": True,
        }

    async def fake_auto_mode(self):
        return {"mode": "off"}

    async def fake_ks(self):
        return {"engaged": False, "initialised": True}

    recorded: list = []
    monkeypatch.setattr(EngineApiClient, "set_billing_enabled", fake_set_billing)
    monkeypatch.setattr(EngineApiClient, "billing_enabled_state", fake_billing)
    monkeypatch.setattr(EngineApiClient, "auto_mode", fake_auto_mode)
    monkeypatch.setattr(EngineApiClient, "kill_switch_state", fake_ks)
    monkeypatch.setattr(EngineApiClient, "auto_trade_global_state", _fake_glob)
    monkeypatch.setattr(control_route.audit, "tail", lambda *a, **k: [])
    monkeypatch.setattr(
        control_route.audit, "record",
        lambda *a, **k: recorded.append(k),
    )

    with TestClient(app) as client:
        _login(client)
        r = client.post("/control/billing", data={"enabled": "false"})
        assert r.status_code == 200
        assert calls["enabled"] is False
        assert recorded[0]["action"] == "play_billing"
        assert "DISABLED" in r.text


# ---------------------------------------------------------------------------
# Layout — the owner asked for the control panel to be arranged "for easy
# access". These pin the arrangement, because a page's readability is not
# something any other test in this repo can see (the 2026-08-06 surf's lesson).
# ---------------------------------------------------------------------------


def _flat(html: str) -> str:
    """Whitespace-collapsed text, for asserting on copy that wraps in the
    template. A reflow is not a behaviour change and must not fail a test."""
    import re

    return re.sub(r"\s+", " ", html)


def _tunables(*entries):
    async def fake(self):
        return {"initialised": True, "tunables": list(entries)}
    return fake


def _knob(key, category, value, default, **over):
    row = {
        "key": key, "label": key.replace("_", " ").title(), "description": "d",
        "type": "bool" if isinstance(default, bool) else "float",
        "value": value, "default": default, "category": category,
        "min": None, "max": None, "unit": "", "choices": None,
    }
    row.update(over)
    return row


def test_operational_controls_render_above_the_wall_of_tunables(monkeypatch):
    """GUARD — this is the whole complaint.

    77 knobs across 4 categories used to sit between the safety switches and
    the auto-execution mode, so on a phone the mode toggle and the live
    positions table were several screens below them. Asserted on ORDER in the
    rendered HTML: a test that merely checked both exist passed before and
    after, which is why the arrangement went unnoticed for as long as it did.
    """
    _patch_reads(monkeypatch)
    monkeypatch.setattr(
        EngineApiClient, "tunables_state",
        _tunables(_knob("a", "Signal gating", True, True)),
    )
    with TestClient(app) as client:
        _login(client)
        html = client.get("/control").text
    assert html.index('id="sec-safety"') < html.index('id="sec-mode"')
    assert html.index('id="sec-mode"') < html.index('id="sec-tunables"')
    assert html.index('id="sec-positions"') < html.index('id="sec-tunables"')
    # Destructive and historical stay at the bottom, out of thumb's way.
    assert html.index('id="sec-tunables"') < html.index('id="sec-danger"')
    assert html.index('id="sec-danger"') < html.index('id="sec-audit"')


def test_every_jump_target_exists(monkeypatch):
    """A nav link to an anchor nobody rendered scrolls nowhere and reads as a
    broken page — the nav rule one level down."""
    import re

    _patch_reads(monkeypatch)
    monkeypatch.setattr(
        EngineApiClient, "tunables_state",
        _tunables(_knob("a", "Signal gating", True, True)),
    )
    with TestClient(app) as client:
        _login(client)
        html = client.get("/control").text
    nav = html[html.index('class="ctl-jump"'):html.index("</nav>")]
    for target in re.findall(r'href="#([^"]+)"', nav):
        assert f'id="{target}"' in html, f"jump target #{target} renders nowhere"


def test_changed_knobs_are_badged_and_counted(monkeypatch):
    """"What did I change?" was unanswerable — 77 knobs rendered identically
    whether or not anyone had touched them."""
    _patch_reads(monkeypatch)
    monkeypatch.setattr(
        EngineApiClient, "tunables_state",
        _tunables(
            _knob("untouched", "Signal gating", True, True),
            _knob("moved", "Signal gating", 0.9, 0.4),
        ),
    )
    with TestClient(app) as client:
        _login(client)
        html = client.get("/control").text
    assert "1 changed" in html
    assert "1</strong> of 2 are off their boot default" in _flat(html)


def test_no_changed_knobs_says_so_rather_than_rendering_nothing(monkeypatch):
    """A blank needs a cause before it gets a caption."""
    _patch_reads(monkeypatch)
    monkeypatch.setattr(
        EngineApiClient, "tunables_state",
        _tunables(_knob("a", "Signal gating", True, True)),
    )
    with TestClient(app) as client:
        _login(client)
        html = client.get("/control").text
    assert "Every tunable is currently at its boot default" in _flat(html)


def test_categories_render_in_consequence_order(monkeypatch):
    """Stops & exits before Measurement — the money path leads."""
    _patch_reads(monkeypatch)
    monkeypatch.setattr(
        EngineApiClient, "tunables_state",
        _tunables(
            _knob("m", "Measurement", True, True),
            _knob("s", "Stops & exits", True, True),
            _knob("g", "Signal gating", True, True),
        ),
    )
    with TestClient(app) as client:
        _login(client)
        html = client.get("/control").text
    assert html.index("Stops &amp; exits") < html.index(">Signal gating<")
    assert html.index(">Signal gating<") < html.index(">Measurement<")


def test_an_unknown_category_still_renders(monkeypatch):
    """The order list is a preference, not a filter. A category the engine
    adds tomorrow sorts to the end rather than disappearing — a hand-kept list
    that silently drops a member is the defect this repo keeps paying for."""
    _patch_reads(monkeypatch)
    monkeypatch.setattr(
        EngineApiClient, "tunables_state",
        _tunables(
            _knob("x", "Brand New Concern", True, True),
            _knob("s", "Stops & exits", True, True),
        ),
    )
    with TestClient(app) as client:
        _login(client)
        html = client.get("/control").text
    assert "Brand New Concern" in html
    assert html.index("Stops &amp; exits") < html.index("Brand New Concern")


def test_filtering_never_removes_a_knob_from_its_form(monkeypatch):
    """The filter hides rows with CSS and must not disable or drop inputs.

    If it did, what you APPLY would depend on what you typed — the one
    behaviour a money-path form must not have. Every knob's input is present
    in the posted form regardless of the filter.
    """
    _patch_reads(monkeypatch)
    monkeypatch.setattr(
        EngineApiClient, "tunables_state",
        _tunables(
            _knob("alpha", "Signal gating", True, True),
            _knob("beta", "Signal gating", True, True),
        ),
    )
    with TestClient(app) as client:
        _login(client)
        html = client.get("/control").text
    assert 'name="alpha"' in html and 'name="beta"' in html
    assert "_bool_keys" in html
    assert "disabled" not in html[html.index('name="alpha"') - 200:
                                  html.index('name="alpha"') + 200]


def test_is_changed_tolerates_json_numeric_drift():
    """An int knob can arrive as 3 or 3.0 depending on the store; a badge that
    cries wolf is worse than no badge, because the reader stops reading it."""
    assert control_route.is_changed({"value": 3, "default": 3.0}) is False
    assert control_route.is_changed({"value": "900", "default": 900}) is False
    assert control_route.is_changed({"value": True, "default": True}) is False
    assert control_route.is_changed({"value": False, "default": True}) is True
    assert control_route.is_changed({"value": 0.9, "default": 0.4}) is True
    assert control_route.is_changed({"value": "", "default": ""}) is False
    assert control_route.is_changed({"value": "SR_FLIP", "default": ""}) is True


def test_anchor_for_is_stable_and_dom_safe():
    assert control_route.anchor_for("Stops & exits") == "tun-stops-exits"
    assert control_route.anchor_for("Signal gating") == "tun-signal-gating"
    assert control_route.anchor_for("") == "tun-other"


def test_jump_targets_clear_the_sticky_chrome(monkeypatch):
    """GUARD — owner-reported: every pill "looked the same".

    The anchors were all present and correct. This app stacks TWO sticky bars
    (`header` at top:0 and `.subnav` at top:51px) and the stylesheet had no
    `scroll-margin` anywhere, so a fragment link scrolled its target to
    viewport-top — behind the chrome. The heading was hidden and the view
    barely changed, which is indistinguishable from a jump that never fired.

    Asserted on the stylesheet because that is where the defect lived: the
    HTML was already right, and every previous test passed over it.
    """
    import pathlib

    css = (pathlib.Path(__file__).resolve().parents[1]
           / "app" / "static" / "style.css").read_text()
    assert "scroll-margin-top" in css, (
        "jump targets scroll behind the sticky header/subnav"
    )
    assert '[id^="sec-"]' in css
    # Measured at runtime, not hardcoded — `header nav` wraps on mobile, which
    # is the device this was reported from.
    assert "--sticky-h" in css


def test_the_jump_handler_never_swallows_a_click_it_cannot_serve(monkeypatch):
    """A preventDefault() with no scroll is a dead link — strictly worse than
    the native behaviour it replaced."""
    _patch_reads(monkeypatch)
    monkeypatch.setattr(
        EngineApiClient, "tunables_state",
        _tunables(_knob("a", "Signal gating", True, True)),
    )
    with TestClient(app) as client:
        _login(client)
        html = client.get("/control").text
    handler = html[html.index("Jump-nav scroll offset"):]
    handler = handler[: handler.index("</script>")]
    # The guard must come BEFORE preventDefault, or a missing target kills
    # the link instead of falling back to the browser.
    assert handler.index("if (!el) return;") < handler.index("preventDefault")


def test_a_tunable_with_choices_renders_a_select_not_a_text_box(monkeypatch):
    """GUARD — owner-reported 2026-08-10.

    `trail_governor_timeframe` has exactly two valid values and shipped as a
    free text box. It was stored as "5"; the candle store is keyed "5m"/"15m",
    so the live trail governor refused every position forever while the ops
    panel blamed the candle feed. A closed set of values must not be typeable.
    """
    _patch_reads(monkeypatch)
    monkeypatch.setattr(
        EngineApiClient, "tunables_state",
        _tunables(_knob("trail_governor_timeframe", "Stops & exits", "15m",
                        "15m", type="str", choices=["5m", "15m"])),
    )
    with TestClient(app) as client:
        _login(client)
        html = client.get("/control").text
    assert '<select id="tun-trail_governor_timeframe"' in html
    assert 'value="5m"' in html and 'value="15m"' in html
    assert 'type="text" id="tun-trail_governor_timeframe"' not in html


def test_a_stored_value_outside_its_choices_is_badged(monkeypatch):
    """The exact production state. It must be visible on the page, not a
    silently-unselected dropdown that looks fine."""
    _patch_reads(monkeypatch)
    monkeypatch.setattr(
        EngineApiClient, "tunables_state",
        _tunables(_knob("trail_governor_timeframe", "Stops & exits", "5",
                        "15m", type="str", choices=["5m", "15m"])),
    )
    with TestClient(app) as client:
        _login(client)
        html = client.get("/control").text
    assert "not a valid option" in html
    assert "stored 5" in html


def test_a_str_tunable_without_choices_is_still_free_text(monkeypatch):
    """The structural-snap allow-list is genuinely free text and must not
    become a dropdown of values nobody enumerated."""
    _patch_reads(monkeypatch)
    monkeypatch.setattr(
        EngineApiClient, "tunables_state",
        _tunables(_knob("structural_snap_apply_paths", "Stops & exits",
                        "SR_FLIP", "", type="str")),
    )
    with TestClient(app) as client:
        _login(client)
        html = client.get("/control").text
    assert 'type="text" id="tun-structural_snap_apply_paths"' in html
    assert "<select id=\"tun-structural_snap_apply_paths\"" not in html

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


#: What the governor diag looks like with the switch off — the default state,
#: and the one every test that is not about the governor should see.
GOVERNOR_OFF = {"enabled": False, "governed": 0, "rows": [], "health": {}}


def _patch_reads(monkeypatch, *, mode="paper", governor=None):
    """Monkeypatch the read calls _render makes, so control tests
    don't hit the network."""
    async def fake_auto_mode(self):
        return {"mode": mode}

    async def fake_governor(self):
        return GOVERNOR_OFF if governor is None else governor

    monkeypatch.setattr(EngineApiClient, "trail_governor", fake_governor)

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
        lambda *a, **k: None if k.get("action") == "login" else recorded.append(k),
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
        lambda *a, **k: None if k.get("action") == "login" else recorded.append(k),
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
        lambda *a, **k: None if k.get("action") == "login" else recorded.append(k),
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
        lambda *a, **k: None if k.get("action") == "login" else recorded.append(k),
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
    """A link to an anchor nobody rendered scrolls nowhere and reads as a
    broken page — the nav rule one level down.

    Scans the WHOLE document rather than the nav element. The first cut read
    only ``.ctl-jump``, which was correct on the day and silent by
    construction the moment a second set of fragment links appeared — which
    is exactly what the status strip then became. Derive the requirement from
    the rendered page, not from the one place links happened to live.
    """
    import re

    _patch_reads(monkeypatch)
    monkeypatch.setattr(
        EngineApiClient, "tunables_state",
        _tunables(_knob("a", "Signal gating", True, True)),
    )
    with TestClient(app) as client:
        _login(client)
        html = client.get("/control").text
    targets = set(re.findall(r'href="#([^"]+)"', html))
    assert "sec-safety" in targets, "the strip/nav rendered no fragment links"
    for target in targets:
        assert f'id="{target}"' in html, f"jump target #{target} renders nowhere"


def test_governor_summary_classifies_with_the_governor_pages_own_states():
    """The five states exist because four of them render as an empty table.

    Re-deriving them here would put a second classifier beside the page this
    tile summarises, and the two would drift.
    """
    assert control_route.governor_summary(
        {"enabled": False, "governed": 0})["state"] == "off"
    assert control_route.governor_summary(
        {"enabled": True, "governed": 0})["state"] == "armed"
    gov = control_route.governor_summary({"enabled": True, "governed": 2})
    assert gov["state"] == "governing" and gov["governed"] == 2
    assert control_route.governor_summary(
        {"enabled": True, "index_cold": True})["state"] == "index_cold"
    assert control_route.governor_summary({"error": "boom"})["state"] == "error"
    # Not a dict at all — an engine that answered with a list must not read as
    # a healthy quiet book.
    assert control_route.governor_summary(["nope"])["state"] == "error"


def test_the_strip_shows_what_the_governor_is_doing_not_what_the_switch_says(
    monkeypatch,
):
    """The tile reads the governor's own diag, never `trail_governor_enabled`.

    Those two came apart on 2026-08-10: a free-text timeframe left the switch
    reading ON while every position was refused as `bad_timeframe`. A tile
    sourced from the tunable would have shown green over a governor that had
    not moved a stop all day.
    """
    _patch_reads(
        monkeypatch,
        governor={"enabled": True, "governed": 2, "timeframe": "5m"},
    )
    # The switch below says the opposite of the diag above; the tile must
    # follow the diag.
    monkeypatch.setattr(
        EngineApiClient, "tunables_state",
        _tunables(_knob("trail_governor_enabled", "Stops & exits", False, False)),
    )
    with TestClient(app) as client:
        _login(client)
        html = client.get("/control").text
    flat = _flat(html)
    assert "Trail governor" in flat
    assert "Governing 2" in flat
    assert "/signals/trail-governor" in html
    # The timeframe is the field that broke; a value carried into the template
    # and rendered by nothing is the seam this repo keeps paying for.
    assert "5m" in flat


def test_the_timeframe_renders_only_when_the_governor_is_running(monkeypatch):
    """A stored timeframe beside an OFF governor describes a governor that is
    not consuming it — the caption naming a state the page is not in."""
    _patch_reads(
        monkeypatch,
        governor={"enabled": False, "governed": 0, "timeframe": "15m"},
    )
    with TestClient(app) as client:
        _login(client)
        html = client.get("/control").text
    tile = _flat(html).split("Trail governor")[1][:300]
    assert "Off" in tile
    assert "15m" not in tile


def test_an_unreadable_governor_is_named_and_does_not_break_the_page(monkeypatch):
    """This page carries the kill switch. The newest read on it must never be
    what stops the owner reaching that button, and a failure must be visible
    rather than rendering as a quiet 'off'."""
    _patch_reads(monkeypatch, governor={"error": "connection refused"})
    with TestClient(app) as client:
        _login(client)
        r = client.get("/control")
    assert r.status_code == 200
    flat = _flat(r.text)
    assert "Unreadable" in flat
    assert "Off</span>" not in flat.split("Trail governor")[1][:400]
    # The kill switch is still operable.
    assert 'action="/control/kill-switch"' in r.text


def test_a_governor_read_that_raises_still_renders_the_page(monkeypatch):
    """`_get` converts an HTTP failure into an error dict; this covers the
    residue (a non-JSON body), because the tolerated path is the whole point
    of catching it."""
    async def boom(self):
        raise RuntimeError("non-JSON body")

    _patch_reads(monkeypatch)
    monkeypatch.setattr(EngineApiClient, "trail_governor", boom)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/control")
    assert r.status_code == 200
    assert "Unreadable" in _flat(r.text)


def test_the_danger_zone_is_framed_apart_from_the_reversible_controls(monkeypatch):
    """The one irreversible action rendered in a card identical to five
    reversible toggles."""
    _patch_reads(monkeypatch)
    with TestClient(app) as client:
        _login(client)
        html = client.get("/control").text
    assert 'class="sec-head sec-head-danger" id="sec-danger"' in html
    assert "card card-danger" in html


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


# ---- the switch that could not be thrown (2026-09-02) -------------------
#
# Owner screenshot: /control read "Kill switch — Clear" in green and "Global
# auto-trade — Disabled", with the explanation in the grey this page uses for
# footnotes.  What that state actually meant is that POST /api/kill-switch
# returned 503 — the owner could not halt auto-trade from the control plane at
# all, against B18's five-second requirement.
#
# The engine now publishes `availability` (ok / not_configured / read_failed),
# `detail`, `throwable` and `source`, so this page can stop inventing a cause
# and can grade the BUTTON rather than the reading.


def _patch_switches(monkeypatch, ks_state, glob_state):
    async def fake_auto_mode(self):
        return {"mode": "paper"}

    async def fake_governor(self):
        return GOVERNOR_OFF

    async def fake_ks(self):
        return ks_state

    async def fake_glob_(self):
        return glob_state

    async def fake_billing(self):
        return {"enabled": True, "configured": True, "initialised": True}

    monkeypatch.setattr(EngineApiClient, "auto_mode", fake_auto_mode)
    monkeypatch.setattr(EngineApiClient, "trail_governor", fake_governor)
    monkeypatch.setattr(EngineApiClient, "kill_switch_state", fake_ks)
    monkeypatch.setattr(EngineApiClient, "auto_trade_global_state", fake_glob_)
    monkeypatch.setattr(EngineApiClient, "billing_enabled_state", fake_billing)
    monkeypatch.setattr(control_route.audit, "tail", lambda *a, **k: [])


def _get_control(monkeypatch, ks_state, glob_state) -> str:
    _patch_switches(monkeypatch, ks_state, glob_state)
    with TestClient(app) as client:
        _login(client)
        return client.get("/control").text


def test_an_unthrowable_kill_switch_renders_as_an_outage(monkeypatch):
    """Not a footnote, and not green.  This is the state the owner was in."""
    body = _get_control(
        monkeypatch,
        {"engaged": False, "initialised": False, "availability": "not_configured",
         "throwable": False, "source": "local", "detail": None},
        {"enabled": False, "initialised": True, "availability": "ok",
         "throwable": True, "source": "local"},
    )
    assert "INOPERABLE" in body
    assert "cannot be thrown" in body
    assert "badge-ok\">Clear" not in body, (
        "an unreadable switch must never render as the green all-clear — "
        "`engaged: false` because we could not ask is a different fact"
    )


def test_an_unreadable_but_throwable_switch_says_which_half_works(monkeypatch):
    """The api container is blind and the engine bridge is up: the value is not
    a reading, and the buttons still work.  A page that graded the button on
    readability would hide a working emergency stop."""
    body = _get_control(
        monkeypatch,
        {"engaged": False, "initialised": False, "availability": "not_configured",
         "throwable": True, "source": "engine", "detail": None},
        {"enabled": False, "initialised": True, "availability": "ok",
         "throwable": True, "source": "local"},
    )
    assert "STATE UNREADABLE" in body
    assert "INOPERABLE" not in body
    assert "Engage kill switch" in body, "the control must still be offered"
    assert "routed to the engine container" in body


def test_a_failed_read_quotes_the_engine_rather_than_naming_a_cause(monkeypatch):
    """/invalidations' WRITER STALE and /dark-signals' hardcoded ban cause, for
    the fourth time: say what the engine said, never why you think it said it."""
    body = _get_control(
        monkeypatch,
        {"engaged": False, "initialised": False, "availability": "read_failed",
         "throwable": True, "source": "local",
         "detail": "ResourceExhausted: 429 Quota exceeded."},
        {"enabled": False, "initialised": True, "availability": "ok",
         "throwable": True, "source": "local"},
    )
    assert "Firestore refused the read" in body
    assert "Quota exceeded" in body
    assert "no Firestore / GCP creds in this deployment" not in body, (
        "the old copy asserted a cause this page cannot observe"
    )


def test_a_healthy_switch_still_renders_exactly_as_before(monkeypatch):
    body = _get_control(
        monkeypatch,
        {"engaged": False, "initialised": True, "availability": "ok",
         "throwable": True, "source": "local", "reason": None},
        {"enabled": True, "initialised": True, "availability": "ok",
         "throwable": True, "source": "local"},
    )
    assert "Disengaged — auto-trade allowed" in body
    assert "ENABLED — new orders allowed" in body
    assert "INOPERABLE" not in body
    assert "STATE UNREADABLE" not in body


def test_an_old_engine_without_the_new_fields_still_renders(monkeypatch):
    """An engine that predates this change sends neither `throwable` nor
    `availability`.  Jinja yields Undefined for both, which is neither None nor
    a value — the exact shape the exit-mechanism control fell past on its first
    cut."""
    body = _get_control(
        monkeypatch,
        {"engaged": False, "initialised": True, "reason": None},
        {"enabled": True, "initialised": True},
    )
    assert "Disengaged — auto-trade allowed" in body
    assert "INOPERABLE" not in body


# ---- the verdict must not outlive the reading (2026-09-02, owner screenshot) -
#
# The first cut of the availability work guarded the TILE and left the card
# BODY unconditional, so /control shipped reading:
#
#     ⚠ STATE UNREADABLE — Firestore refused the read.
#     Not a safety pause and not "off": we could not ask.
#     ✓ Disengaged — auto-trade allowed.          <- in green, one line below
#
# The page said we could not ask and then answered anyway, in the flattering
# direction, on the safety card. "Fixed one writer, not the field" — and the
# reason no test caught it is that every assertion was about the tile.
#
# These assert the CARD BODY, on both cards, in both directions.


def test_an_unreadable_kill_switch_prints_no_verdict_at_all(monkeypatch):
    body = _get_control(
        monkeypatch,
        {"engaged": False, "initialised": False, "availability": "read_failed",
         "throwable": True, "source": "local",
         "detail": "ResourceExhausted: 429 Quota exceeded."},
        {"enabled": False, "initialised": True, "availability": "ok",
         "throwable": True, "source": "local"},
    )
    assert "STATE UNREADABLE" in body
    assert "Disengaged — auto-trade allowed" not in body, (
        "a value we could not read must not be printed as a reading — and "
        "green is the flattering direction of that error"
    )
    assert "ENGAGED — all auto-trade is halted" not in body
    assert "No state is shown because none was read" in body


def test_an_unreadable_kill_switch_still_offers_BOTH_controls(monkeypatch):
    """We do not know which way it is set, so both actions must be reachable.

    Offering only one would be the verdict smuggled back in as a control: a
    lone "Engage" button asserts it is currently disengaged.
    """
    body = _get_control(
        monkeypatch,
        {"engaged": False, "initialised": False, "availability": "read_failed",
         "throwable": True, "source": "local", "detail": "429"},
        {"enabled": False, "initialised": True, "availability": "ok",
         "throwable": True, "source": "local"},
    )
    assert "Engage kill switch — halt all" in body
    assert "Disengage — resume trading" in body


def test_an_unreadable_auto_trade_flag_does_not_claim_DISABLED(monkeypatch):
    """"DISABLED — no new orders are placed" is a claim about the engine's
    behaviour that a failed read in THIS process cannot support. The engine
    reads the same flag with its own client and may be trading normally."""
    body = _get_control(
        monkeypatch,
        {"engaged": False, "initialised": True, "availability": "ok",
         "throwable": True, "source": "local", "reason": None},
        {"enabled": False, "initialised": False, "availability": "read_failed",
         "throwable": True, "source": "local", "detail": "429 Quota exceeded."},
    )
    assert "DISABLED — no new orders are placed" not in body
    assert "ENABLED — new orders allowed" not in body
    assert "Enable global auto-trade" in body
    assert "Disable global auto-trade" in body


def test_a_readable_switch_still_prints_its_verdict(monkeypatch):
    """The repair must not have made every card silent."""
    body = _get_control(
        monkeypatch,
        {"engaged": True, "initialised": True, "availability": "ok",
         "throwable": True, "source": "local", "reason": "manual halt"},
        {"enabled": True, "initialised": True, "availability": "ok",
         "throwable": True, "source": "local"},
    )
    assert "ENGAGED — all auto-trade is halted" in body
    assert "manual halt" in body
    assert "ENABLED — new orders allowed" in body
    assert "No state is shown because none was read" not in body


def test_an_old_engine_without_availability_still_prints_its_verdict(monkeypatch):
    """`availability` absent is an older engine, not an unreadable state.

    Jinja yields Undefined for the missing key; `.get(..., 'ok')` is what keeps
    that from silencing every card against an engine that predates the field.
    """
    body = _get_control(
        monkeypatch,
        {"engaged": False, "initialised": True, "reason": None},
        {"enabled": True, "initialised": True},
    )
    assert "Disengaged — auto-trade allowed" in body
    assert "No state is shown because none was read" not in body


# The audit captures above ignore ``action="login"``: since 2026-09-26 a
# successful owner sign-in is itself audited (app/routes/auth.py), and these
# tests log in before exercising the control action they are about.


# ---- one-tap switches (2026-09-26) ---------------------------------------
#
# Owner: "make control panel simple easy to toggle, not like raw data". An
# on/off knob now saves on the tap that flips it, as its own form, and the
# switchboard's text buttons became switches. What these guard is what makes
# a one-tap control safe to have: it writes one knob, it never shows a state
# nobody read, and it cannot be steered off the page.


def _switch_forms(html: str) -> list[str]:
    import re

    return re.findall(r'<form[^>]*class="tg-tile[^"]*"[^>]*>.*?</form>', html, re.S)


def test_each_on_off_knob_is_its_own_form(monkeypatch):
    """A tap must write exactly the knob that was tapped. A switch sharing a
    form with its category would post every sibling as it stood on load —
    the whole category re-written by one tap."""
    import re

    _patch_reads(monkeypatch)
    monkeypatch.setattr(
        EngineApiClient, "tunables_state",
        _tunables(
            _knob("alpha", "Signal gating", True, True),
            _knob("beta", "Signal gating", False, False),
            _knob("gamma", "Signal gating", 0.5, 0.5),
        ),
    )
    with TestClient(app) as client:
        _login(client)
        html = client.get("/control").text
    forms = _switch_forms(html)
    assert len(forms) == 2
    for key, form in zip(("alpha", "beta"), forms):
        assert f'name="_bool_keys" value="{key}"' in form
        names = set(re.findall(r'name="([a-z]\w*)"', form))
        assert names == {key}, f"switch form for {key} posts {names}"
        assert 'action="/control/tunables"' in form
    # The typed value lives in a separate form, with no bool keys in it.
    assert 'name="gamma"' not in "".join(forms)
    assert 'name="_bool_keys" value=""' in html


def test_a_switch_save_writes_one_knob_and_returns_to_it(monkeypatch):
    sent: dict = {}

    async def fake_set(self, values):
        sent.update(values)
        return {"initialised": True, "tunables": []}

    _patch_reads(monkeypatch)
    monkeypatch.setattr(EngineApiClient, "set_tunables", fake_set)
    monkeypatch.setattr(control_route.audit, "record", lambda *a, **k: None)
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/tunables",
            data={"_bool_keys": "mean_revert_live", "_label": "MEAN_REVERT live",
                  "_return": "tun-row-mean_revert_live"},
            follow_redirects=False,
        )
        assert r.status_code == 303  # PRG: a refresh cannot re-fire it
        assert r.headers["location"] == "/control#tun-row-mean_revert_live"
        page = client.get("/control").text
    # The steering fields never reach the engine as tunables.
    assert sent == {"mean_revert_live": False}
    assert "MEAN_REVERT live → OFF" in page


def test_the_return_target_cannot_leave_the_page(monkeypatch):
    """`_return` is an element id and nothing else — never a URL."""
    async def fake_set(self, values):
        return {"initialised": True, "tunables": []}

    _patch_reads(monkeypatch)
    monkeypatch.setattr(EngineApiClient, "set_tunables", fake_set)
    monkeypatch.setattr(control_route.audit, "record", lambda *a, **k: None)
    with TestClient(app) as client:
        _login(client)
        for bad in ("//evil.example.com", "https://evil.example.com",
                    "x#y", "../login", "a b", "javascript:alert(1)", ""):
            r = client.post(
                "/control/tunables",
                data={"_bool_keys": "k", "_return": bad},
                follow_redirects=False,
            )
            assert r.headers["location"] == "/control", bad


def test_a_failed_switch_save_says_which_knob_failed(monkeypatch):
    async def fake_set(self, values):
        return {"error": "HTTP 503"}

    _patch_reads(monkeypatch)
    monkeypatch.setattr(EngineApiClient, "set_tunables", fake_set)
    monkeypatch.setattr(control_route.audit, "record", lambda *a, **k: None)
    with TestClient(app) as client:
        _login(client)
        client.post("/control/tunables",
                    data={"_bool_keys": "k", "k": "on", "_label": "Knob K"},
                    follow_redirects=False)
        page = client.get("/control").text
    assert "Knob K: Tunables update failed: HTTP 503" in _flat(page)
    assert 'data-autohide="0"' in page, "a failure must not fade on its own"


def test_the_switchboard_draws_a_switch_only_over_a_reading(monkeypatch):
    """A switch drawn in either position is a verdict. Over a flag we could
    not read, the page keeps both text buttons and draws no switch — the
    2026-09-02 rule that an unreadable state renders no verdict."""
    readable = _get_control(
        monkeypatch,
        {"engaged": False, "initialised": True, "availability": "ok",
         "throwable": True, "source": "local"},
        {"enabled": True, "initialised": True, "availability": "ok",
         "throwable": True, "source": "local"},
    )
    assert ('role="switch"\n        aria-checked="true" aria-label="Disable global '
            'auto-trade"') in readable

    unreadable = _get_control(
        monkeypatch,
        {"engaged": False, "initialised": True, "availability": "ok",
         "throwable": True, "source": "local"},
        {"enabled": False, "initialised": False, "availability": "read_failed",
         "throwable": True, "source": "engine", "detail": "quota"},
    )
    row = unreadable[unreadable.index('id="sw-auto-trade"'):
                     unreadable.index('id="sec-mode"')]
    assert 'role="switch"' not in row
    assert "Enable global auto-trade" in row and "Disable global auto-trade" in row


def test_the_routing_list_renders_read_only_and_is_never_posted_from_here(
    monkeypatch,
):
    """`retired_paths` is edited on /control/routing, which re-reads the list
    at write time. A category form here re-posted it as the page loaded it,
    and could undo a divert made on Routing a minute earlier."""
    _patch_reads(monkeypatch)
    monkeypatch.setattr(
        EngineApiClient, "tunables_state",
        _tunables(
            _knob("path_retirement_enabled", "Signal gating", True, True),
            _knob("retired_paths", "Signal gating",
                  "MOVER_TREND_PULLBACK:SHORT, VOLUME_SURGE_BREAKOUT:*",
                  "", type="str"),
            _knob("structural_snap_apply_paths", "Signal gating", "", "",
                  type="str"),
        ),
    )
    with TestClient(app) as client:
        _login(client)
        html = client.get("/control").text
    assert 'name="retired_paths"' not in html
    str_keys = html[html.index('name="_str_keys"'):]
    str_keys = str_keys[: str_keys.index(">")]
    assert "retired_paths" not in str_keys
    assert "structural_snap_apply_paths" in str_keys
    assert 'href="/control/routing"' in html
    assert '<span class="chip">MOVER_TREND_PULLBACK:SHORT</span>' in html
    assert '<span class="chip">VOLUME_SURGE_BREAKOUT:*</span>' in html


def test_default_and_range_show_only_when_they_mean_something(monkeypatch):
    """"default 10 · range 5–100" under every knob was most of what read as
    raw. The default shows beside a knob that is OFF it; the range lives in
    the ⓘ and in the input's own min/max."""
    _patch_reads(monkeypatch)
    monkeypatch.setattr(
        EngineApiClient, "tunables_state",
        _tunables(
            _knob("still", "Signal gating", 10, 10, type="int", min=5, max=100),
            _knob("moved", "Signal gating", 0.9, 0.4, min=0.0, max=1.0),
        ),
    )
    with TestClient(app) as client:
        _login(client)
        html = client.get("/control").text
    assert _flat(html).count('<div class="tun-meta">default') == 1
    assert '<div class="tun-meta">default 0.4</div>' in html
    assert "· range" not in html
    assert 'min="5" max="100"' in _flat(html)


def test_no_category_key_shadows_a_dict_method():
    """Jinja resolves `meta.values` to `dict.values` before the item named
    `values`; the first cut of this page 500'd on exactly that. Derived from
    the helper's real output, not from a list of names to avoid."""
    groups, _ = control_route.group_tunables({
        "initialised": True,
        "tunables": [_knob("a", "Signal gating", True, True),
                     _knob("b", "Signal gating", 1.0, 1.0)],
    })
    for meta in control_route.category_meta(groups):
        assert not set(meta) & set(dir({})), set(meta) & set(dir({}))


def test_audit_rows_read_as_sentences():
    labels = {"be_arm_trigger_pct": "BE arm: flat trigger", "mean_revert_live": "MEAN_REVERT live"}
    row = control_route.describe_audit(
        {"action": "tunables_update", "ok": True,
         "params": {"values": {"be_arm_trigger_pct": "1.2", "mean_revert_live": "False"}}},
        labels,
    )
    assert row["title"] == "Engine tunables"
    assert row["lines"] == ["BE arm: flat trigger → 1.2", "MEAN_REVERT live → off"]
    # The raw params survive for the hover — nothing is thrown away.
    assert '"be_arm_trigger_pct": "1.2"' in row["raw"]

    many = control_route.describe_audit(
        {"action": "tunables_update",
         "params": {"values": {f"k{i}": str(i) for i in range(7)}}}, {})
    assert many["lines"][-1] == "…and 4 more" and len(many["lines"]) == 4

    ks = control_route.describe_audit(
        {"action": "kill_switch", "params": {"engaged": True, "reason": "drill"}})
    assert ks["lines"] == ["Engaged", "Reason: drill"]
    assert control_route.describe_audit(
        {"action": "auto_trade_global", "params": {"enabled": False}}
    )["lines"] == ["Turned off"]

    # A writer this page has never heard of reads sensibly, not blank.
    unknown = control_route.describe_audit(
        {"action": "path_divert",
         "params": {"setup_class": "MOVER_AVWAP_SCALP", "side": "SHORT"}})
    assert unknown["title"] == "Path divert"
    assert unknown["lines"] == ["setup class: MOVER_AVWAP_SCALP", "side: SHORT"]
    # A key the engine no longer publishes keeps its raw name.
    gone = control_route.describe_audit(
        {"action": "tunables_update", "params": {"values": {"old_knob": "3"}}}, {})
    assert gone["lines"] == ["old_knob → 3"]


# ---- the auto-execution mode is queued, so say what the engine did (2026-09-26)
#
# Owner: "There is some problem with auto execution mode toggle, not showing
# correctly." In production a click only QUEUES the change; the engine applies
# it at the end of its next writer cycle — or refuses it (open positions, no
# exchange keys for LIVE), an answer that lived only in the engine log. The
# page said "Auto-mode set to PAPER" beside a toggle still reading LIVE either
# way, and printed "Already in LIVE — no change" over every 409, refusals
# included.

from datetime import datetime, timedelta, timezone  # noqa: E402

_NOW = datetime(2026, 9, 26, 17, 0, tzinfo=timezone.utc)


def _ago(seconds: float) -> str:
    return (_NOW - timedelta(seconds=seconds)).isoformat()


def _mv(auto=None, cmd=None, req=None):
    return control_route.mode_view(auto or {}, cmd or {}, req, _NOW)


def test_a_queued_change_reads_as_pending_not_applied():
    v = _mv({"mode": "live"}, {"mode_queue": "queued", "mode": "live",
                               "pending_mode": "paper"})
    assert (v["state"], v["mode"], v["target"], v["refresh"]) == (
        "pending", "live", "paper", True)


def test_the_engines_fresh_reading_beats_the_ten_second_copy():
    v = _mv({"mode": "live"}, {"mode_queue": "queued", "mode": "paper"},
            {"mode": "paper", "at": _ago(5)})
    assert (v["state"], v["mode"]) == ("ok", "paper")


def test_a_refusal_is_shown_in_the_engines_words():
    v = _mv({"mode": "live"},
            {"mode_queue": "queued", "mode": "live", "last_mode_command": {
                "requested": "paper", "outcome": "refused", "at": _ago(20),
                "message": "refused: 2 open position(s) — close them first"}},
            {"mode": "paper", "at": _ago(30)})
    assert (v["state"], v["target"]) == ("refused", "paper")
    assert "2 open position" in v["message"]


def test_an_old_refusal_is_not_the_answer_to_a_new_request():
    """A refusal from before this request describes a different click."""
    v = _mv({"mode": "live"},
            {"mode_queue": "queued", "mode": "live", "last_mode_command": {
                "requested": "off", "outcome": "refused", "at": _ago(100),
                "message": "refused: 1 open position(s)"}},
            {"mode": "paper", "at": _ago(10)})
    assert v["state"] == "not_applied"  # queue empty, no answer for THIS request


def test_a_refusal_ages_off_the_page():
    v = _mv({"mode": "live"}, {"mode_queue": "queued", "mode": "live",
            "last_mode_command": {"requested": "paper", "outcome": "refused",
                                  "at": _ago(3600), "message": "x"}})
    assert v["state"] == "ok"


def test_an_engine_without_the_endpoint_still_gets_an_honest_window():
    """404 = an engine predating the command endpoint: not reported, which is
    not 'nothing pending'. The browser's own request carries the window."""
    old = {"error": "Not Found", "status_code": 404, "endpoint": "/api/auto-mode/command"}
    assert _mv({"mode": "live"}, old, {"mode": "paper", "at": _ago(20)})["state"] == "applying"
    late = _mv({"mode": "live"}, old, {"mode": "paper", "at": _ago(80)})
    assert late["state"] == "not_applied" and late["queue"] == "not_reported"
    assert _mv({"mode": "live"}, old)["state"] == "ok"


def test_a_blank_transport_error_is_unreadable_not_a_quiet_queue():
    """`str(httpx.ReadTimeout())` is ''. Graded by key presence, never by
    whether `error` is truthy (the 2026-09-03 rule)."""
    v = _mv({"mode": "live"}, {"error": "", "endpoint": "/api/auto-mode/command"})
    assert (v["queue"], v["mode"], v["state"]) == ("unreadable", "live", "ok")


def test_a_mode_that_differs_from_boot_says_it_will_not_survive_a_restart():
    v = _mv({}, {"mode_queue": "queued", "mode": "live", "boot_mode": "paper"})
    assert v["resets_to"] == "paper"
    assert "resets_to" not in _mv({}, {"mode_queue": "queued", "mode": "paper",
                                       "boot_mode": "paper"})


def _post_mode(monkeypatch, answer, cmd=None, mode="paper"):
    async def fake_set(self, m):
        return answer

    async def fake_cmd(self):
        return cmd or {"mode_queue": "queued", "mode": "live"}

    _patch_reads(monkeypatch, mode="live")
    monkeypatch.setattr(EngineApiClient, "set_auto_mode", fake_set)
    monkeypatch.setattr(EngineApiClient, "auto_mode_command", fake_cmd)
    monkeypatch.setattr(control_route.audit, "record", lambda *a, **k: None)
    with TestClient(app) as client:
        _login(client)
        r = client.post("/control/auto-mode", data={"mode": mode}, follow_redirects=False)
        assert r.headers["location"] == "/control#sec-mode"
        return client.get("/control").text


def test_a_queued_post_says_requested_not_set_and_shows_the_wait(monkeypatch):
    page = _post_mode(
        monkeypatch, {"success": True, "mode": "paper", "queued": True},
        {"mode_queue": "queued", "mode": "live", "pending_mode": "paper"},
    )
    flat = _flat(page)
    assert "PAPER requested — queued" in flat
    assert "Auto-mode set to PAPER" not in flat
    assert "Switching to <strong>PAPER</strong>" in flat
    assert 'class="seg-wait"' in page and "Paper …" in flat
    assert "location.reload()" in page


def test_a_409_refusal_is_not_reported_as_already_there(monkeypatch):
    page = _flat(_post_mode(
        monkeypatch,
        {"error": "refused: 2 open position(s) — close them first", "status_code": 409},
    ))
    assert "The engine refused PAPER: refused: 2 open position(s)" in page
    assert "Already in" not in page


def test_a_409_no_op_keeps_the_engines_words(monkeypatch):
    page = _flat(_post_mode(
        monkeypatch,
        {"error": "a change to PAPER is already queued — the engine applies it "
                  "on its next cycle", "status_code": 409},
    ))
    assert "No change — a change to PAPER is already queued" in page


def test_the_page_never_reloads_itself_when_nothing_is_on_its_way(monkeypatch):
    _patch_reads(monkeypatch, mode="live")

    async def fake_cmd(self):
        return {"mode_queue": "queued", "mode": "live", "boot_mode": "live"}

    monkeypatch.setattr(EngineApiClient, "auto_mode_command", fake_cmd)
    with TestClient(app) as client:
        _login(client)
        page = client.get("/control").text
    assert "location.reload()" not in page
    assert "seg-wait" not in page.split('id="sec-mode"')[1].split("sw-expiry")[0]

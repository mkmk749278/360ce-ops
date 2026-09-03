"""`/signals/ai-governor` — reachable, honest, and pinned to the ENGINE's shape.

The engine ships this lane dark (measurement on, effect off), and a dark change
without its panel is unfinished. Two pages shipped with panels, tests and PR
bodies and **neither was in the navigation** — reachable only by typing the URL,
which is what the owner was reduced to. So the first assertion here is that a
reader can get to it.

The second is the one that has cost this repo more: the cross-repo contract is
driven against the **real engine payload**, not a fixture. An ops fixture puts
the block where the reader assumed it and then agrees with you about it — every
test green over a card that would render NOT REPORTED against the real engine.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import ai_governor as page  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


def _get(monkeypatch, path="/signals/ai-governor", diag=None) -> str:
    """Render the page against the ENGINE'S OWN payload by default.

    Not a hand-written dict: `build_diag` is imported from the engine and
    called, so a key this page reads that the engine stops publishing fails
    here rather than rendering a blank card in production.
    """
    payload = _engine_diag() if diag is None else diag

    async def fake_run(self, key, args=None):
        return {"ok": True, "key": key, "result": payload}

    monkeypatch.setattr(EngineApiClient, "diag_run", fake_run)
    with TestClient(app) as client:
        _login(client)
        return client.get(path).text


def test_the_page_is_in_the_navigation():
    """A panel that renders perfectly on a page nobody can reach is exactly as
    useful as no panel — two lanes shipped that way and the owner was reduced
    to typing URLs.

    The NAV literal is parsed rather than a rendered page scraped, because
    `base.html` only expands the ACTIVE group's sub-links: asserting against
    `/` would pass for a page that is in no group at all. `tests/test_nav.py`
    derives the wider requirement (every literal page under `/signals/` is
    linked, no two labels or active keys collide, every destination is driven
    as a real request) and covers this page automatically — this assertion is
    the cheap direct one beside it.
    """
    nav = open("app/templates/base.html", encoding="utf-8").read()
    assert "('/signals/ai-governor', 'AI governor', 'ai_governor')" in nav


def test_the_page_renders(monkeypatch):
    assert "AI Trade Governor" in _get(monkeypatch)


def test_a_literal_route_is_registered_before_the_catch_all():
    """`signal_detail` owns `/signals/{signal_id}`, which matches any literal.
    A page included after it 404s while its route object sits in `app.routes`
    looking perfectly registered — the route list is not the authority."""
    import app.main as main

    src = open(main.__file__, encoding="utf-8").read()
    assert src.index("ai_governor.router") < src.index("signal_detail.router")


# ── The cross-repo contract, driven against the REAL engine ─────────────────

def _engine_diag() -> dict:
    """Call the engine's own `build_diag`, not a shape this repo invented."""
    import sys
    import pathlib

    engine = pathlib.Path(__file__).resolve().parents[2] / "360-v2"
    if not engine.exists():
        pytest.skip("engine repo not checked out beside ops")
    sys.path.insert(0, str(engine))
    try:
        from src.execution import ai_governor as gov  # type: ignore
        return gov.build_diag()
    finally:
        sys.path.remove(str(engine))


def test_the_keys_this_page_reads_are_the_keys_the_engine_writes():
    """Pinned against the engine's real assembler.

    `zone_distance_atr` read a zone's edges by guessing five key names, none of
    which the only producer in the engine actually carries, and its two tests
    passed on a shape nothing has ever produced. The price-action lane card
    then repeated it one level up with the shape right and the PATH wrong.
    """
    diag = _engine_diag()
    for key in ("measure_enabled", "apply_enabled", "armed_arms", "provider",
                "provider_configured", "model_requested", "bounds", "health",
                "arms", "ledger_rows", "queue_depth", "rate_table_version"):
        assert key in diag, f"engine no longer publishes {key!r}"
    for key in ("cycles", "arms", "triggers", "calls", "verdicts", "applied",
                "spend_usd", "by_action", "refusals", "throttles"):
        assert key in diag["health"], f"engine no longer publishes health.{key!r}"
    for key in ("calls_per_signal", "calls_per_hour", "usd_per_day",
                "panic_max_positions", "panic_armed"):
        assert key in diag["bounds"], f"engine no longer publishes bounds.{key!r}"


def test_classify_grades_the_real_engine_payload_as_ok():
    assert page.classify({"ok": True, "result": _engine_diag()}) == page.STATE_OK


def test_an_engine_predating_the_entry_reads_as_not_reported_not_unreachable():
    """The engine ANSWERED and refused the key. Reading that as unreachable
    sends the operator to check a network that is fine."""
    assert page.classify({"ok": False, "error": "unknown catalog entry"}) == page.STATE_NOT_REPORTED
    assert page.classify({"error": "connect timeout"}) == page.STATE_UNREACHABLE
    assert page.classify(None) == page.STATE_UNREACHABLE


# ── The honesty rules ───────────────────────────────────────────────────────

def test_a_refusal_the_page_has_never_heard_of_is_badged_not_dropped():
    """Iterating this page's own copy table would be silent by construction on
    the next reason the engine adds."""
    rows = page.annotate({"a_brand_new_reason": 3}, page.REFUSAL_COPY)
    assert len(rows) == 1
    assert rows[0]["unclassified"] is True
    assert rows[0]["count"] == 3


def test_throttles_and_refusals_are_never_pooled():
    """`cooldown` means the lane found an arm it was willing to evaluate and
    deliberately did not — positive evidence it is working."""
    assert "cooldown" in page.THROTTLE_COPY
    assert "cooldown" not in page.REFUSAL_COPY


def test_the_page_publishes_no_blended_cross_arm_figure(monkeypatch):
    """One number over all four arms would move with the SL arm's refusal rate
    rather than with the mechanism."""
    body = _get(monkeypatch).lower()
    for banned in ("overall edge", "combined delta", "blended r", "avg_r"):
        assert banned not in body


def test_an_unset_panic_ceiling_renders_as_a_state_not_an_absent_row(monkeypatch):
    """The panic arm refuses while the ceiling is 0. A missing row would read as
    an arm that is simply quiet."""
    body = _get(monkeypatch)
    assert "Panic close" in body
    assert "UNSET" in body or "panic_max_positions" in body


def test_lane_state_separates_not_configured_from_off_and_from_working():
    """"Measuring but no key set" is the state this lane ships in, and it is
    neither working nor broken. Collapsing it into either sends the owner to
    fix the wrong thing."""
    assert page.lane_state({"measure_enabled": False}) == "off"
    assert page.lane_state({"measure_enabled": True, "provider_configured": False}) == "not_configured"
    assert page.lane_state({"measure_enabled": True, "provider_configured": True}) == "measuring"
    assert page.lane_state(
        {"measure_enabled": True, "provider_configured": True, "apply_enabled": True}
    ) == "enforcing"
    assert page.lane_state({}) == "unknown"


def test_a_partial_payload_renders_rather_than_500ing(monkeypatch):
    """An engine that publishes SOME of the block must not take the page down.

    `classify` short-circuits a wholly-absent payload to NOT REPORTED, so the
    dangerous case is the partial one: a build that reports `measure_enabled`
    and not yet `armed_arms`. This repo's convention is that a template adapts
    to the engine's shape rather than crashing on drift — the engine REST
    surface is the source of truth and ops follows it.
    """
    body = _get(monkeypatch, diag={"measure_enabled": True})
    assert "AI Trade Governor" in body
    assert "NOT CONFIGURED" in body or "MEASUREMENT OFF" in body

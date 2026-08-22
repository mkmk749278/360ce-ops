"""`/signals/router-drops` — the same-direction cap panel.

Added 2026-08-22 with engine `DIRECTION_CAP_MODE`, for the owner: *"set cap
per path 3 same direction and no cumulative max cap anyways"*.

That gate took **499 of 500 drops — 91.6% of everything the router dequeued**
over one 10.5h boot, and the row above it could only say that it did. This
panel says which budget produced the count and what the other mode would have
passed.

**The cross-repo tests drive the REAL engine router**, not a fixture. A fixture
chooses a shape and then agrees with you about it — `zone_distance_atr` and the
price-action lane card each cost a session to exactly that, and the second one
had the shape right and the *path* wrong.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.routes.router_drops import reduce_direction_cap
from app.template_filters import share_of

# The engine repo sits beside this one in every session that has both. CI for
# this repo checks out ops alone, so the cross-repo test below skips there —
# and the skip states WHICH of the two reasons applies, because "the engine
# repo is not here" and "it is here and its dependencies are not" have
# different fixes and a single 'skipped' hides that.
_ENGINE = Path(__file__).resolve().parents[2] / "360-v2"


def _engine_reachable() -> str:
    """Empty string when the engine's router can be imported; else the reason."""
    if not _ENGINE.is_dir():
        return "engine repo not checked out beside ops"
    sys.path.insert(0, str(_ENGINE))
    try:
        import src.signal_router  # noqa: F401
        return ""
    except ImportError as exc:
        return f"engine deps unavailable in this environment: {exc}"
    finally:
        sys.path.remove(str(_ENGINE))
        for mod in [m for m in sys.modules if m.startswith(("src.", "config"))]:
            sys.modules.pop(mod, None)


_SKIP_REASON = _engine_reachable()


# ---------------------------------------------------------------------------
# The reducer's states — three, never two
# ---------------------------------------------------------------------------

def test_an_engine_predating_the_block_reads_not_reported_not_zero():
    """"could not answer" and "answered none" have different next moves."""
    out = reduce_direction_cap({"processed": 10})
    assert out["available"] is False
    assert out["reason"] == "not_reported"


def test_a_missing_payload_is_named_rather_than_assumed_empty():
    assert reduce_direction_cap(None)["available"] is False
    assert reduce_direction_cap({})["available"] is False


def test_the_mode_is_read_off_the_payload_never_mirrored():
    """`MEASUREMENT_SUFFIXES` drifted for a week. One writer, one reader."""
    out = reduce_direction_cap(
        {"direction_cap": {"mode": "per_path", "per_path_limit": 3, "evaluated": 1}}
    )
    assert out["mode"] == "per_path"
    assert out["mode_known"] is True
    assert out["other_mode"] == "global"


def test_a_mode_this_page_has_never_heard_of_still_renders_its_counts():
    out = reduce_direction_cap(
        {"direction_cap": {"mode": "some_future_mode", "evaluated": 7}}
    )
    assert out["available"] is True
    assert out["mode_known"] is False
    assert out["evaluated"] == 7
    assert out["other_mode"] == "", "we must not guess which mode it is not"


def test_a_cumulative_limit_of_zero_is_a_decision_not_an_unset():
    off = reduce_direction_cap({"direction_cap": {"mode": "per_path", "cumulative_limit": 0}})
    on = reduce_direction_cap({"direction_cap": {"mode": "per_path", "cumulative_limit": 10}})
    assert off["cumulative_off"] is True
    assert on["cumulative_off"] is False and on["cumulative_limit"] == 10


def test_budgets_held_splits_the_engines_composite_key():
    out = reduce_direction_cap({
        "direction_cap": {
            "mode": "per_path",
            "budgets_held": {"MOVER_TREND_PULLBACK|LONG": 3, "RANGE_FADE|SHORT": 1},
            "budgets_held_total": 4,
        }
    })
    assert out["budgets_held"][0] == {
        "path": "MOVER_TREND_PULLBACK", "direction": "LONG", "n": 3,
    }
    assert out["budgets_held_total"] == 4


# ---------------------------------------------------------------------------
# `share_of` — a denominator of zero is not zero percent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("num,den,expected", [
    (5, 20, "25.0%"),
    (0, 20, "0.0%"),
    (5, 0, "—"),
    (5, None, "—"),
    (None, 20, "—"),
])
def test_share_of_names_the_zero_denominator(num, den, expected):
    """"0 of 0" is not 0% — nothing was measured, and printing 0% claims it was."""
    assert share_of(num, den) == expected


# ---------------------------------------------------------------------------
# The cross-repo contract — driven against the REAL engine router
# ---------------------------------------------------------------------------

@pytest.mark.skipif(bool(_SKIP_REASON), reason=_SKIP_REASON or "")
def test_the_reducer_reads_what_the_real_engine_router_publishes():
    """Drive `SignalRouter.delivery_stats()` and reduce its actual output.

    This is the assertion a fixture cannot make: that the keys this page reads
    are the keys the engine writes, at the level it writes them. The engine
    nests the block under `direction_cap` inside `delivery_stats`, and the
    first thing a hand-built fixture gets wrong is the nesting.
    """
    sys.path.insert(0, str(_ENGINE))
    try:
        import asyncio

        from src.channels.base import Signal
        from src.signal_router import SignalRouter
        from src.smc import Direction

        async def _send(chat_id: str, text: str) -> bool:
            return True

        router = SignalRouter(
            queue=asyncio.Queue(),
            send_telegram=_send,
            format_signal=lambda sig: "x",
        )

        def _sig(sym: str, origin: str) -> Signal:
            return Signal(
                channel="360_SCALP", symbol=sym, direction=Direction.LONG,
                entry=100.0, stop_loss=98.0, tp1=103.0, tp2=106.0,
                setup_class=origin, origin_setup_class=origin,
                signal_id=f"{sym}-{origin}",
            )

        for c in "ABC":
            s = _sig(f"{c}USDT", "MOVER_TREND_PULLBACK")
            router._active_signals[s.signal_id] = s

        # A starved path: its own budget is empty, the book's is full.
        cap = router._direction_cap_decision(_sig("DUSDT", "MEAN_REVERT"))
        router._record_direction_cap_counterfactual(cap)

        payload = router.delivery_stats()
    finally:
        sys.path.remove(str(_ENGINE))
        for mod in [m for m in sys.modules if m.startswith(("src.", "config"))]:
            sys.modules.pop(mod, None)

    assert "direction_cap" in payload, (
        "the engine stopped publishing the block this page renders"
    )
    # …and assert it is NOT at the top level, which is where a fixture would
    # have put it.
    assert "mode" not in payload

    out = reduce_direction_cap(payload)
    assert out["available"] is True
    assert out["mode"] in ("global", "per_path")
    assert out["evaluated"] == 1
    assert out["global_only"] == 1, (
        "the global cap kills a candidate a per-path budget would pass — "
        "this is the population the owner is asking about"
    )
    assert out["budgets_held"][0]["path"] == "MOVER_TREND_PULLBACK"
    assert out["budgets_held"][0]["n"] == 3


# ---------------------------------------------------------------------------
# The page renders — and its copy does not assert a mode
# ---------------------------------------------------------------------------

@contextmanager
def _client(payload, monkeypatch):
    """The repo's own helper shape — patch the real client class, run the
    lifespan, log in. `app.state.engine_api` is built at startup, so assigning
    to it beforehand assigns to something about to be replaced."""
    from fastapi.testclient import TestClient

    from app.data_sources.engine_api import EngineApiClient
    from app.main import app

    async def _fake(self):  # noqa: ANN001
        return payload

    monkeypatch.setattr(EngineApiClient, "router_delivery", _fake)
    with TestClient(app) as c:
        c.post("/login", data={"password": "test-token"})
        yield c


_LIVE_SHAPED = {
    "schema": 1, "processed": 545, "delivered": 17, "dropped": 500,
    "delivery_rate": 0.031,
    "drops_by_reason": {"same_direction_throttle": 499},
    "drops_by_reason_setup": {"same_direction_throttle:MOVER_TREND_PULLBACK": 475},
    "direction_cap": {
        "mode": "global", "per_path_limit": 3, "global_limit": 3,
        "cumulative_limit": 0, "evaluated": 545,
        "counterfactual": {
            "evaluated": 545, "both_block": 24, "global_only": 475,
            "per_path_only": 0, "neither_blocks": 46,
        },
        "counterfactual_by_path": {"global_only:MOVER_TREND_PULLBACK": 475},
        "would_gain": 475, "would_gain_share": 0.8716,
        "budgets_held": {"MOVER_TREND_PULLBACK|LONG": 3},
        "budgets_held_total": 3,
    },
}


def test_the_page_renders_the_panel(monkeypatch):
    with _client(_LIVE_SHAPED, monkeypatch) as c:
        body = c.get("/signals/router-drops").text
    assert "GLOBAL" in body
    assert "475" in body, "the counterfactual must be on screen"
    assert "MOVER_TREND_PULLBACK" in body


def test_the_copy_no_longer_asserts_a_mode_the_engine_decides(monkeypatch):
    """Copy is part of the measurement.

    The shared-cap description read "a GLOBAL cap of 3 … shared book-wide",
    which stops being unconditionally true the moment the engine has two
    modes — a sentence asserting one mode over a counter produced by the other
    is wrong on screen even when every number above it is right.
    """
    with _client(_LIVE_SHAPED, monkeypatch) as c:
        body = c.get("/signals/router-drops").text
    assert "a GLOBAL cap of 3 concurrent positions" not in body


def test_the_panel_states_that_past_this_hop_is_not_delivered(monkeypatch):
    """An upper bound on volume must never read as an expected gain."""
    with _client(_LIVE_SHAPED, monkeypatch) as c:
        body = c.get("/signals/router-drops").text
    assert "never as an expected gain" in body
    assert "reset on" in body, "cumulative-since-boot must stay on screen"


def test_an_older_engine_reads_not_reported_on_this_panel(monkeypatch):
    payload = {k: v for k, v in _LIVE_SHAPED.items() if k != "direction_cap"}
    with _client(payload, monkeypatch) as c:
        body = c.get("/signals/router-drops").text
    assert "NOT REPORTED" in body
    assert "predating it" in body


def test_per_path_mode_describes_the_per_path_budget(monkeypatch):
    payload = dict(_LIVE_SHAPED)
    payload["direction_cap"] = dict(_LIVE_SHAPED["direction_cap"], mode="per_path")
    with _client(payload, monkeypatch) as c:
        body = c.get("/signals/router-drops").text
    assert "PER PATH" in body
    assert "long and" in body, "the 3-long-3-short shape must be stated"

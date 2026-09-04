"""`/signals/router-drops` — the per-channel cap panel.

Added 2026-09-04 with engine `CHANNEL_CAP_MODE`, for the owner: *"actually we
have only one channel we don't have any other channel, with in that scalp
channel we have about 17 paths, and each path has its own cap so don't keep any
cap on max signals of channel"*.

He is right about the shape, which is why this panel exists rather than the cap
simply being deleted: `360_SCALP` is the only fully-live channel, so a cap named
per-channel was a cap of 5 on the WHOLE BOOK across 17 paths. It took 45 of 56
router drops over one measured 4.9h boot and 32 of 101 promoted
`LIQUIDITY_SWEEP_REVERSAL` rows — the largest single stamped reason a promoted
dark signal reached nobody.

The cap is switched, not removed, and the counterfactual runs either way. This
file pins the panel that makes the switch readable.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.routes.router_drops import (  # noqa: E402
    SHARED_CAPS, classify, reduce_channel_cap,
)


def _cap(**kw) -> dict:
    base = {
        "mode": "off",
        "channel_limits": {"360_SCALP": 5, "360_SCALP_DIVERGENCE": 3},
        "book_limit": 0,
        "evaluated": 20,
        "counterfactual": {
            "both_block": 0,
            "channel_only": 8,
            "book_only": 0,
            "neither_blocks": 12,
        },
        "counterfactual_by_setup": {
            "channel_only:MOVER_TREND_PULLBACK": 6,
            "channel_only:LIQUIDITY_SWEEP_REVERSAL": 2,
        },
        "would_have_blocked": 8,
        "would_have_blocked_share": 0.4,
        "held_by_channel": {"360_SCALP": 5},
        "held_total": 5,
    }
    base.update(kw)
    return base


@contextmanager
def _client(payload, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    async def _fake(self):
        return payload

    monkeypatch.setattr(EngineApiClient, "router_delivery", _fake)
    with TestClient(app) as c:
        c.post("/login", data={"password": "test-token"})
        yield c


class TestTheThreeStatesAreNeverPooled:
    """`enforce` / `off with a ceiling` / `off with nothing` have three
    different next moves, so they must not share a rendering."""

    def test_an_engine_predating_the_block_reads_not_reported(self):
        """NOT a cap that dropped nothing — that reports zeroes with a mode
        beside them. The next move is a deploy, not a reading."""
        out = reduce_channel_cap({"schema": 1, "processed": 10})
        assert out["available"] is False
        assert out["reason"] == "not_reported"

    def test_off_with_no_ceiling_is_flagged_unbounded(self):
        out = reduce_channel_cap({"channel_cap": _cap(mode="off", book_limit=0)})
        assert out["unbounded"] is True
        assert out["armed"] is False
        assert out["book_off"] is True

    def test_off_with_a_ceiling_is_not_unbounded(self):
        out = reduce_channel_cap({"channel_cap": _cap(mode="off", book_limit=40)})
        assert out["unbounded"] is False
        assert out["book_off"] is False

    def test_enforcing_is_not_unbounded_even_with_the_ceiling_off(self):
        out = reduce_channel_cap({"channel_cap": _cap(mode="enforce", book_limit=0)})
        assert out["armed"] is True
        assert out["unbounded"] is False

    def test_a_mode_this_page_has_never_heard_of_is_badged_not_guessed(self):
        """A reason ops has never heard of renders under its raw name rather
        than borrowing the wording of a state it may not be in."""
        out = reduce_channel_cap({"channel_cap": _cap(mode="whatever")})
        assert out["mode_known"] is False
        assert out["mode"] == "whatever"


class TestZeroIsADecision:

    def test_book_limit_zero_reads_off_not_a_ceiling_of_zero(self):
        out = reduce_channel_cap({"channel_cap": _cap(book_limit=0)})
        assert out["book_off"] is True

    def test_no_candidate_seen_yields_no_share_rather_than_zero_percent(self):
        """`None` is "no candidate reached this gate"; 0.0 would be a claim."""
        out = reduce_channel_cap(
            {"channel_cap": _cap(evaluated=0, would_have_blocked_share=None)}
        )
        assert out["would_have_blocked_share"] is None
        assert out["evaluated"] == 0


class TestTheDropReasonsAreClassified:

    def test_book_cap_is_a_shared_cap_not_an_unknown_gate(self):
        assert "book_cap" in SHARED_CAPS
        assert classify("book_cap") == "shared_cap"

    def test_book_cap_and_per_channel_cap_are_separate_reasons(self):
        """A full book and a full channel are different findings with
        different fixes; one word for two events is how a page stops being
        able to say what happened."""
        assert SHARED_CAPS["book_cap"] != SHARED_CAPS["per_channel_cap"]


class TestThePanelRenders:

    def test_the_page_carries_the_panel(self, monkeypatch):
        payload = {"schema": 1, "processed": 20, "delivered": 2, "dropped": 18,
                   "drops_by_reason": {}, "drops_by_reason_setup": {},
                   "channel_cap": _cap()}
        with _client(payload, monkeypatch) as c:
            body = c.get("/signals/router-drops").text
        assert "per-channel cap" in body.lower()
        assert "OFF — BOOK UNBOUNDED" in body

    def test_it_states_the_blast_radius_when_nothing_bounds_the_book(self, monkeypatch):
        """The owner asked for the cap off and that is what ships. The page
        owes him the sentence saying what is left bounding the book — a
        control surface that hides the consequence of its own setting is the
        reassuring direction of a wrong caption."""
        payload = {"schema": 1, "processed": 20, "delivered": 2, "dropped": 18,
                   "drops_by_reason": {}, "drops_by_reason_setup": {},
                   "channel_cap": _cap(mode="off", book_limit=0)}
        with _client(payload, monkeypatch) as c:
            body = c.get("/signals/router-drops").text
        assert "nothing bounds the SIZE of the book" in body
        assert "position count" in body.lower()

    def test_it_says_past_this_hop_is_not_delivered(self, monkeypatch):
        """The counterfactual is an upper bound on volume and is structurally
        incapable of being an expected gain."""
        payload = {"schema": 1, "processed": 20, "delivered": 2, "dropped": 18,
                   "drops_by_reason": {}, "drops_by_reason_setup": {},
                   "channel_cap": _cap()}
        with _client(payload, monkeypatch) as c:
            body = c.get("/signals/router-drops").text
        assert "not delivered" in body
        assert "never traded" in body

    def test_an_old_engine_renders_not_reported_for_this_panel(self, monkeypatch):
        payload = {"schema": 1, "processed": 20, "delivered": 2, "dropped": 18,
                   "drops_by_reason": {}, "drops_by_reason_setup": {}}
        with _client(payload, monkeypatch) as c:
            body = c.get("/signals/router-drops").text
        assert "channel_cap" in body  # names the absent block, not a blank


def _engine_channel_cap_report() -> dict:
    """Drive the engine's REAL report, not a shape this repo invented.

    A fixture chooses a location and then agrees with you about it — that cost
    a session on `zone_distance_atr` and another on the price-action lane card.
    """
    import asyncio
    import sys

    engine = Path(__file__).resolve().parents[2] / "360-v2"
    if not engine.exists():
        pytest.skip("engine repo not checked out beside ops")
    sys.path.insert(0, str(engine))
    try:
        from src.signal_router import SignalRouter  # type: ignore

        async def _send(_a, _b):
            return True

        router = SignalRouter(
            queue=asyncio.Queue(), send_telegram=_send, format_signal=lambda s: ""
        )
        return router.delivery_stats()
    finally:
        sys.path.remove(str(engine))


def test_the_keys_this_page_reads_are_the_keys_the_engine_writes():
    """Pinned against the engine's real assembler, including its LOCATION.

    `channel_cap` is nested inside `delivery_stats()`, not at the payload's top
    level, and a fixture that put it in the wrong place would have passed every
    other test in this file while rendering NOT REPORTED against production.
    """
    stats = _engine_channel_cap_report()
    assert "channel_cap" in stats, "engine must publish the block where ops reads it"
    out = reduce_channel_cap(stats)
    assert out["available"] is True, "the real engine payload must reduce cleanly"
    for key in (
        "mode", "armed", "unbounded", "book_limit", "book_off", "evaluated",
        "both_block", "channel_only", "book_only", "neither_blocks",
        "would_have_blocked", "would_have_blocked_share", "by_setup",
        "held_by_channel", "held_total", "channel_limits",
    ):
        assert key in out, f"{key} missing from the reduced payload"


def test_the_engine_publishes_the_block_before_any_candidate_is_seen():
    """A panel that appears only once it trips teaches the reader that its
    absence means "fine" when it equally means the check stopped running."""
    stats = _engine_channel_cap_report()
    cap = stats["channel_cap"]
    assert cap["evaluated"] == 0
    assert cap["would_have_blocked"] == 0
    assert cap["would_have_blocked_share"] is None

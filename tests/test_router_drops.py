"""`/signals/router-drops` — the last hop before a subscriber.

Built for the owner's question (2026-08-07): *"that max concurrent and same
direction stopping signals from other paths? because MVRTP might generate more
signals so this might be the reason also?"*

The delivered book is consistent with that and cannot prove it. The dropped rows
can, and the engine had been counting them keyed `reason:setup_class` all along
— `delivery_stats()` computed `drops_by_reason_setup` and its only caller logged
the un-keyed half, so the decisive number was written every cycle and read by
nobody.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app.routes.router_drops import (  # noqa: E402
    classify, concentration, reduce_drops,
)


def _payload(**kw):
    base = {
        "schema": 1,
        "processed": 100,
        "delivered": 20,
        "dropped": 80,
        "delivery_rate": 0.2,
        "drops_by_reason": {
            "same_direction_throttle": 50,
            "symbol_channel_cooldown": 30,
        },
        "drops_by_reason_setup": {
            "same_direction_throttle:MOVER_TREND_PULLBACK": 45,
            "same_direction_throttle:TREND_PULLBACK_EMA": 5,
            "symbol_channel_cooldown:MOVER_TREND_PULLBACK": 30,
        },
    }
    base.update(kw)
    return base


class TestTheTwoQuestionsAreNotPooled:

    def test_a_shared_cap_is_distinguished_from_a_per_symbol_gate(self):
        """The whole point. A path cannot consume another path's per-symbol
        cooldown budget, so a big number there is volume, not crowding out."""
        assert classify("same_direction_throttle") == "shared_cap"
        assert classify("per_channel_cap") == "shared_cap"
        assert classify("correlation_group_limit") == "shared_cap"
        assert classify("symbol_channel_cooldown") == "per_candidate"
        assert classify("correlation_lock") == "per_candidate"

    def test_the_crowding_panel_counts_only_shared_caps(self):
        """Pooling the per-symbol cooldown in would manufacture crowding-out out
        of ordinary re-detection volume — the exact misread this page prevents."""
        out = concentration(reduce_drops(_payload()))
        by = {r["setup"]: r["n"] for r in out}
        # 45 from the shared throttle only — NOT 75 with the cooldown pooled in.
        assert by["MOVER_TREND_PULLBACK"] == 45
        assert by["TREND_PULLBACK_EMA"] == 5

    def test_shared_total_excludes_per_candidate_gates(self):
        r = reduce_drops(_payload())
        assert r["shared_total"] == 50
        assert r["shared_share"] == pytest.approx(0.5)

    def test_a_gate_total_and_its_setup_rows_are_never_pooled(self):
        r = reduce_drops(_payload())
        row = next(x for x in r["rows"] if x["reason"] == "same_direction_throttle")
        assert row["n"] == 50
        assert sum(s["n"] for s in row["setups"]) == 50
        assert "same_direction_throttle" not in {s["setup"] for s in row["setups"]}


class TestRefusalsAndUnknowns:

    def test_an_unrecognised_reason_renders_under_its_own_name(self):
        """Iterating ops' own key list would be silent by construction on the
        next gate the engine adds."""
        p = _payload(
            drops_by_reason={"brand_new_gate": 4},
            drops_by_reason_setup={"brand_new_gate:X": 4},
        )
        r = reduce_drops(p)
        assert r["unclassified"] == ["brand_new_gate"]
        row = next(x for x in r["rows"] if x["reason"] == "brand_new_gate")
        assert row["kind"] == "unclassified" and row["n"] == 4

    def test_a_drop_with_no_setup_class_is_named_not_absorbed(self):
        p = _payload(
            drops_by_reason={"same_direction_throttle": 50},
            drops_by_reason_setup={"same_direction_throttle:MOVER_TREND_PULLBACK": 45},
        )
        row = next(
            x for x in reduce_drops(p)["rows"]
            if x["reason"] == "same_direction_throttle"
        )
        assert row["unattributed"] == 5

    def test_schema_zero_is_not_reported_rather_than_zero_drops(self):
        """An old engine and a router that dropped nothing must not render
        alike — the first needs a deploy, the second needs nothing."""
        r = reduce_drops({"schema": 0, "error": "engine has no router.delivery_stats"})
        assert r["available"] is False
        assert "delivery_stats" in r["reason"]

    def test_an_empty_payload_refuses_rather_than_inventing_zeroes(self):
        assert reduce_drops({})["available"] is False
        assert reduce_drops(None)["available"] is False

    def test_a_setup_containing_a_colon_is_split_on_the_first_one_only(self):
        p = _payload(
            drops_by_reason={"per_channel_cap": 3},
            drops_by_reason_setup={"per_channel_cap:WEIRD:NAME": 3},
        )
        row = next(
            x for x in reduce_drops(p)["rows"] if x["reason"] == "per_channel_cap"
        )
        assert row["setups"][0]["setup"] == "WEIRD:NAME"

    def test_zero_processed_does_not_divide_by_zero(self):
        p = _payload(processed=0, delivered=0, dropped=0,
                     drops_by_reason={}, drops_by_reason_setup={})
        r = reduce_drops(p)
        assert r["available"] is True
        assert r["shared_share"] is None


from app.data_sources.engine_api import EngineApiClient  # noqa: E402


@contextmanager
def _client(payload, monkeypatch):
    """Monkeypatch the real client class and let the app lifespan run.

    `app.state.engine_api` is built at startup, so assigning to it before the
    lifespan runs is assigning to something that is about to be replaced.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    async def _fake(self):
        return payload

    monkeypatch.setattr(EngineApiClient, "router_delivery", _fake)
    with TestClient(app) as c:
        c.post("/login", data={"password": "test-token"})
        yield c


class TestThePageRendersAndStatesItsLimits:

    def test_the_page_renders(self, monkeypatch):
        with _client(_payload(), monkeypatch) as c:
            body = c.get("/signals/router-drops").text
        assert "Router drops" in body
        assert "same_direction_throttle" in body

    def test_it_says_the_counters_reset_on_restart(self, monkeypatch):
        """A number that silently restarts reads as a quiet market."""
        with _client(_payload(), monkeypatch) as c:
            body = c.get("/signals/router-drops").text
        assert "reset on restart" in body

    def test_it_says_a_drop_is_not_a_fault(self, monkeypatch):
        """The caps are blast-radius protection; this page must not read as an
        argument to widen them."""
        with _client(_payload(), monkeypatch) as c:
            body = c.get("/signals/router-drops").text
        assert "not a fault" in body.lower()

    def test_it_warns_that_the_biggest_path_loses_most_by_arithmetic(self, monkeypatch):
        with _client(_payload(), monkeypatch) as c:
            body = c.get("/signals/router-drops").text
        assert "arithmetic, not crowding out" in body

    def test_an_old_engine_reads_not_reported_rather_than_empty(self, monkeypatch):
        with _client({"schema": 0, "error": "nope"}, monkeypatch) as c:
            body = c.get("/signals/router-drops").text
        assert "NOT REPORTED" in body

    def test_the_route_is_registered_before_signal_detail(self):
        """`signal_detail` owns /signals/{signal_id} and matches any literal
        that follows it — this cost a debugging session on entry-features."""
        import app.main as m
        src = open(m.__file__).read()
        assert src.index("router_drops.router") < src.index("signal_detail.router")

    def test_the_live_request_does_not_404(self, monkeypatch):
        with _client(_payload(), monkeypatch) as c:
            assert c.get("/signals/router-drops").status_code == 200

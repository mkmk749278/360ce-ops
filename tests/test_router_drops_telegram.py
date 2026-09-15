"""Was Telegram in front of the money path? — and is it still (2026-09-15).

Engine #1034. Until that change `SignalRouter._process` sent to a Telegram
broadcast channel BEFORE `_write_dispatch_log`, before
`dispatch_signal_to_active_users` — which places the orders — before the
`_active_signals` book and before the app's own push. Both failure modes
returned, so a failed chat send or an evaluator channel with no mapping took
the candidate off all four. Every document in these repos calls Telegram a
"mirror", and nobody audits the delivery path of a mirror.

This card is where the owner watches that change. It reads the engine's own
`telegram_channels_enabled` / `telegram_bypassed` — ops computes nothing
about it — and the contract test below drives the REAL engine assembler
rather than a payload this repo invented, because a fixture chooses a
location and then agrees with you about it.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.routes.router_drops import reduce_telegram  # noqa: E402


def _payload(**kw):
    base = {
        "schema": 1,
        "processed": 100,
        "delivered": 20,
        "dropped": 80,
        "delivery_rate": 0.2,
        "drops_by_reason": {},
        "drops_by_reason_setup": {},
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


class TestThreeStatesNeverTwo:
    def test_an_engine_predating_the_keys_is_not_a_report_that_channels_are_off(self):
        """The two are opposite claims and one of them is flattering.

        Defaulting an absent flag to `off` would render every older build as
        having an independent money path — the mechanism-manifest rule
        (`governs` must be tri-state) arriving at a different flag.
        """
        assert reduce_telegram(_payload())["state"] == "not_reported"

    def test_channels_on_is_reported_as_the_old_ordering(self):
        r = reduce_telegram(_payload(
            telegram_channels_enabled=True, telegram_bypassed=0
        ))
        assert r["state"] == "on"

    def test_channels_off_is_reported_with_its_counter(self):
        r = reduce_telegram(_payload(
            telegram_channels_enabled=False, telegram_bypassed=20
        ))
        assert r["state"] == "off"
        assert r["bypassed"] == 20

    def test_false_is_told_apart_from_absent_by_key_presence(self):
        """`if not flag` cannot tell them apart, and this repo has already
        paid for that once on `ok`/`error`."""
        assert reduce_telegram(_payload(
            telegram_channels_enabled=False, telegram_bypassed=20
        ))["state"] == "off"
        assert reduce_telegram(_payload())["state"] == "not_reported"


class TestTheCounterIsGradedNotPrinted:
    def test_bypassed_must_track_delivered_while_channels_are_off(self):
        ok = reduce_telegram(_payload(
            telegram_channels_enabled=False, telegram_bypassed=20
        ))
        assert ok["tracks_delivered"] is True
        assert ok["gap"] == 0
        assert ok["no_traffic_yet"] is False

    def test_an_empty_population_is_not_a_match(self):
        """Found by reading this card on production, ten minutes after it
        shipped (2026-09-15).

        `bypassed == delivered` is trivially true at 0 == 0, so the first
        cut badged a freshly restarted engine "tracks delivered" — a
        confirmation drawn from a population that cannot support one, in
        exactly the window where the page is most likely to be read (the
        counters reset on every deploy). Three states, never two.
        """
        fresh = reduce_telegram(_payload(
            delivered=0, telegram_channels_enabled=False, telegram_bypassed=0
        ))
        assert fresh["no_traffic_yet"] is True
        assert fresh["tracks_delivered"] is not True, (
            "0 == 0 must not read as a verified branch"
        )

    def test_no_traffic_is_only_a_channels_off_state(self):
        """With channels ON the branch is never taken, so a zero there is
        not 'waiting for evidence' — it is the correct final answer."""
        on = reduce_telegram(_payload(
            delivered=0, telegram_channels_enabled=True, telegram_bypassed=0
        ))
        assert on["no_traffic_yet"] is False
        assert on["tracks_delivered"] is None

    def test_a_branch_that_is_not_being_taken_is_the_thing_this_can_see(self):
        """The refutation condition stated in #1034's own body: if
        `telegram_bypassed` stays flat while `delivered` climbs, the bypass
        branch is not being taken — and nothing else on this page can say so,
        because every other number looks identical either way."""
        bad = reduce_telegram(_payload(
            telegram_channels_enabled=False, telegram_bypassed=0
        ))
        assert bad["tracks_delivered"] is False
        assert bad["gap"] == 20

    def test_with_channels_on_a_zero_is_correct_and_is_not_graded(self):
        """The branch is never taken, so grading it would render a healthy
        engine as a fault — an alarming caption over a working subsystem."""
        r = reduce_telegram(_payload(
            telegram_channels_enabled=True, telegram_bypassed=0
        ))
        assert r["tracks_delivered"] is None
        assert r["gap"] is None


class TestThePageSaysWhichWorldItIsIn:
    def test_the_off_state_renders_the_counter(self, monkeypatch):
        with _client(_payload(
            telegram_channels_enabled=False, telegram_bypassed=20
        ), monkeypatch) as c:
            body = c.get("/signals/router-drops").text
        assert "CHANNELS OFF" in body
        assert "Bypassed Telegram" in body

    def test_the_on_state_warns_that_the_money_path_depends_on_a_chat_service(
        self, monkeypatch
    ):
        with _client(_payload(
            telegram_channels_enabled=True, telegram_bypassed=0
        ), monkeypatch) as c:
            body = c.get("/signals/router-drops").text
        assert "CHANNELS ON" in body
        # The two drop reasons mean something different in this world, and the
        # gate table below cannot say so on its own.
        assert "delivery_failed" in body
        assert "no_channel_configured" in body

    def test_a_fresh_engine_says_nothing_is_confirmed_yet(self, monkeypatch):
        """The badge a reader takes as 'verified' must not appear over zero."""
        with _client(_payload(
            delivered=0, telegram_channels_enabled=False, telegram_bypassed=0
        ), monkeypatch) as c:
            body = c.get("/signals/router-drops").text
        assert "CHANNELS OFF" in body
        assert "nothing routed yet" in body
        assert "Nothing is confirmed yet" in body
        assert "tracks delivered" not in body
        assert "does not track delivered" not in body

    def test_a_divergence_is_badged_not_printed_as_one_more_number(
        self, monkeypatch
    ):
        with _client(_payload(
            telegram_channels_enabled=False, telegram_bypassed=0
        ), monkeypatch) as c:
            body = c.get("/signals/router-drops").text
        assert "does not track delivered" in body

    def test_an_older_engine_reads_as_a_deploy_question(self, monkeypatch):
        with _client(_payload(), monkeypatch) as c:
            body = c.get("/signals/router-drops").text
        assert "NOT REPORTED" in body
        assert "CHANNELS OFF" not in body


class TestTheContractWithTheEngine:
    """Drives the engine's real assembler, at the path it really lands on."""

    @staticmethod
    def _engine_router_delivery():
        import sys
        from pathlib import Path

        engine = Path(__file__).resolve().parents[2] / "360-v2"
        if not engine.exists():
            pytest.skip("engine repo not checked out beside ops")
        sys.path.insert(0, str(engine))
        try:
            import asyncio

            try:
                from src.api.snapshot_writer import SnapshotWriter
                from src.signal_router import SignalRouter
            except ImportError as exc:
                # Skip ONLY when the engine's own third-party dependencies
                # are absent from this interpreter — a missing `loguru` is an
                # environment fact. A missing `src.*` or `config` name is a
                # renamed symbol, which is exactly the contract break this
                # class exists to catch, so it is re-raised.
                missing = (exc.name or "").split(".")[0]
                if missing in ("src", "config"):
                    raise
                pytest.skip(f"engine dependency not installed here: {exc.name}")

            async def _send(_c, _t):
                return True

            router = SignalRouter(
                queue=asyncio.Queue(),
                send_telegram=_send,
                format_signal=lambda s: "",
            )
            writer = SnapshotWriter.__new__(SnapshotWriter)
            writer._engine = type("_E", (), {"router": router})()
            return writer._build_router_delivery()
        finally:
            sys.path.remove(str(engine))

    def test_the_keys_this_card_reads_are_keys_the_engine_publishes(self):
        payload = self._engine_router_delivery()
        assert "telegram_channels_enabled" in payload
        assert "telegram_bypassed" in payload

    def test_they_are_at_the_top_level_and_not_nested(self):
        """The price-action lane card rendered NOT REPORTED against
        production with every ops test green, because a fixture put the block
        where the reader assumed it and the engine nested it under `derived`.
        Assert the path, including that it is not the one you might guess."""
        payload = self._engine_router_delivery()
        assert "telegram_bypassed" not in (payload.get("derived") or {})
        assert isinstance(payload["telegram_bypassed"], int)

    def test_the_card_renders_against_the_real_assembler(self):
        """A reducer that agrees with a fixture and disagrees with the engine
        is the whole defect this test class exists to prevent."""
        r = reduce_telegram(self._engine_router_delivery())
        assert r["available"] is True
        assert r["state"] in ("on", "off")

    def test_the_new_keys_carry_no_colon(self):
        """`delivery_stats` partitions the drop counters on ":" — a key
        carrying the delimiter lands in the wrong table, which `/system/redis`
        and the throttle table have both already paid for."""
        payload = self._engine_router_delivery()
        for key in ("telegram_channels_enabled", "telegram_bypassed"):
            assert ":" not in key
            assert key not in (payload.get("drops_by_reason") or {})

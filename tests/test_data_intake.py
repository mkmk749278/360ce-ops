"""The data-intake page must make an absence and a provenance visible.

Every finding this page exists for is a value of the right shape arriving from
somewhere other than where its consumers assume. None of them raises, so none
can be tested by asserting "no exception" — these assert the page *says so*.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")


def _report(**over):
    """The engine's real payload shape.

    Mirrors ``src/data_intake.build_data_intake``'s output rather than a shape
    invented here — a fixture whose keys you chose asserts your assumption back
    at you and goes green over a page that cannot render.
    """
    base = {
        "schema": 1,
        "generated_at": 1_700_000_000.0,
        "pools": [
            {
                "label": "futures_klines", "present": True, "state": "healthy",
                "streams_total": 2, "degraded_count": 0,
                "connections": [{
                    "index": 0, "streams": 2, "silent_streams": 0,
                    "silent_sample": [], "degraded": False,
                    "reconnect_attempts": 0, "ping_latency_ms": 11.0,
                    "last_reconnect_ms": 0.0, "connected_age_s": 120.0,
                    "seconds_to_forced_cycle": 86_280.0,
                    "msgs_since_health_check": 500,
                }],
            },
            {
                "label": "spot", "present": False, "state": "not_started",
                "streams_total": 0, "degraded_count": 0, "connections": [],
            },
        ],
        "stream_kinds": {
            "distinct_stream_suffixes": ["!ticker@arr", "@forceOrder", "@kline_1m"],
            "kinds": {
                "klines": True, "liquidations": True, "all_market_ticker": True,
                "aggregate_trades": False, "raw_trades": False, "depth": False,
            },
        },
        "series": {
            "present": True, "symbols_sampled": 1, "symbols_total": 1,
            "symbols_in_scan_universe": 1,
            "symbols_retained_outside_universe": 0,
            "by_timeframe": {
                "1m": {
                    "series": 2, "stale": 1, "undated": 1,
                    "series_scanned": 1, "stale_scanned": 1, "undated_scanned": 0,
                    "bars_min": 10, "bars_max": 500,
                    "oldest_newest_bar_age_s": 3600.0, "stale_budget_s": 180.0,
                    "stalest_symbols": [{"symbol": "BTCUSDT", "age_s": 3600.0}],
                },
            },
        },
        "derived": {
            "cvd": {"source": "kline_taker_buy",
                    "detail": "closed 1m/15m kline Q/q fields — not tick data",
                    "symbols_tracked": 75, "symbols_tracked_15m": 75},
            "ticks": {"source": "rest_seed_snapshot",
                      "detail": "/fapi/v1/trades limit=1000 at seed time; no "
                                "@trade or @aggTrade subscription feeds it",
                      "symbols": 75, "rows": 75_000,
                      "newest_trade_age_s": 14_400.0,
                      "consumers": ["scanner._build_scan_context (recent_ticks)"]},
            "order_book": {"source": "book_ticker", "quality": "top_of_book_only",
                           "detail": "one bid and one ask", "symbols_cached": 75},
            "open_interest": {"source": "rest_poll", "symbols": 75},
            "funding": {"source": "rest_poll", "symbols": 75},
            "liquidations": {"source": "ws_force_order", "symbols": 75},
        },
        "primitives": {"rows": [
            {"primitive": "orderblocks", "status": "not_implemented",
             "healthy": False,
             "detail": "declared on SMCResult and never assigned — every "
                       "`bool(fvgs) or bool(orderblocks)` gate is `bool(fvgs)` alone"},
            {"primitive": "fvg", "status": "lookback=10", "healthy": True,
             "detail": "sees ~12 bars — on 15m that is ~3.0h of structure"},
        ]},
        "levels": {
            "level_book": {"present": True, "symbols": 75, "levels_total": 900,
                           "oldest_refresh_age_s": 42.0},
            "volume_profile": {
                "micro": {"symbols": 75, "state": "ok"},
                "macro": {"symbols": 0, "state": "unreadable: no _results attribute"},
            },
        },
        "live_ticks": {
            "present": True, "stream_enabled": True, "max_symbols": 40,
            "subscribed": 40, "fed": 38, "quiet": 1, "subscribed_silent": 1,
            "subscribed_silent_sample": ["NOPEUSDT"], "quiet_sample": [],
            "rows": 38_000, "total_accepted": 120_000, "total_rejected": 0,
            "uptime_s": 900.0, "maxlen": 1000, "quiet_after_s": 90.0,
            "serving_consumers": False,
            "drift_vs_seeded": {
                "compared": 30, "median_gap_s": 3_600.0,
                "worst": [{"symbol": "BTCUSDT", "seeded_age_s": 14400.0,
                           "live_age_s": 0.4, "gap_s": 14399.6}],
            },
        },
        "weight": {
            "futures": {"used": 300, "budget": 2200, "pct": 13.6},
            "spot": {"used": 10, "budget": 5500, "pct": 0.2},
            "declared": {
                "/fapi/v1/trades": {"weight": 5, "source": "verified:2026-08-05",
                                    "note": "Recent Trades List"},
                "/fapi/v1/klines": {"weight": -1, "source": "carried",
                                    "note": "weight varies by limit"},
            },
        },
    }
    base.update(over)
    return base


@contextmanager
def _client(payload=None, raises=None):
    from fastapi.testclient import TestClient

    from app.main import app

    class _API:
        async def data_intake(self):
            if raises is not None:
                raise raises
            return payload

        async def aclose(self):
            pass

    with TestClient(app) as client:
        client.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]},
                    follow_redirects=False)
        app.state.engine_api = _API()
        yield client


class TestTheAbsencePanel:
    def test_an_unsubscribed_stream_kind_is_named_not_omitted(self):
        """An absence cannot be seen in a list of what is present. That is
        exactly how a missing trade stream sat behind a complete, unreachable
        handler in main.py."""
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "Aggregate trades" in body
        assert "Raw trades" in body
        assert "Order book depth" in body
        assert "NOT SUBSCRIBED" in body

    def test_the_page_says_what_each_absence_costs(self):
        """A row reading 'not subscribed' is a fact. The consequence is what
        makes it actionable."""
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "no footprint" in body
        assert "one bid and one ask" in body

    def test_a_subscribed_kind_renders_as_subscribed(self):
        r = _report()
        r["stream_kinds"]["kinds"]["aggregate_trades"] = True
        with _client(r) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "SUBSCRIBED" in body


class TestProvenance:
    def test_the_tick_store_is_badged_not_live(self):
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "rest_seed_snapshot" in body
        assert "not live" in body

    def test_a_stale_tick_store_shows_its_age_prominently(self):
        """4h old, on a store five call sites read as current."""
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "240m old" in body

    def test_the_consumers_of_the_stale_store_are_named(self):
        """Named because the risk is theirs."""
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "_build_scan_context" in body

    def test_cvd_source_is_shown_rather_than_implied(self):
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "kline_taker_buy" in body
        assert "not tick data" in body

    def test_the_order_book_quality_is_badged(self):
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "top_of_book_only" in body


class TestTheCensusRendersUnconditionally:
    def test_a_hollow_primitive_is_shown_with_its_cause(self):
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "not_implemented" in body
        assert "no writer" in body

    def test_the_census_renders_when_everything_is_healthy(self):
        """A check that appears only when it trips teaches the reader that its
        absence means 'fine' when it equally means the check stopped running."""
        r = _report()
        r["primitives"]["rows"] = [
            {"primitive": "orderblocks", "status": "live", "healthy": True,
             "detail": ""},
        ]
        with _client(r) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "Structural primitives" in body
        assert "orderblocks" in body


class TestPools:
    def test_a_never_started_pool_is_named_not_shown_as_zero(self):
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "NOT STARTED" in body

    def test_the_24h_forced_cycle_countdown_renders(self):
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "To 24h cycle" in body
        assert "24.0h" in body


class TestSeries:
    def test_undated_is_a_separate_column_from_stale(self):
        """Different faults, different fixes — pooling them reports the wrong
        one."""
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "Undated" in body and "Stale" in body

    def test_each_timeframe_shows_its_own_budget(self):
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "180s" in body  # the 1m budget, beside the age


class TestWeight:
    def test_the_live_gauge_renders(self):
        """The header value had been synced into the limiter and never
        rendered anywhere."""
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "300 / 2200" in body

    def test_the_declared_table_shows_provenance_per_entry(self):
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "verified:2026-08-05" in body
        assert "carried" in body
        assert "not thereby confirmed" in body


class TestFailureIsNotEmptiness:
    def test_an_unreachable_engine_renders_a_cause(self):
        """An empty page here reads exactly like an engine with nothing
        subscribed — the one conclusion this page must never allow by
        accident."""
        with _client(raises=RuntimeError("connection refused")) as c:
            resp = c.get("/diagnostics/data-intake")
        assert resp.status_code == 200
        assert "Cannot read the intake report" in resp.text
        assert "connection refused" in resp.text

    def test_an_engine_side_error_payload_is_surfaced(self):
        with _client({"schema": 0, "error": "RuntimeError: boom"}) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "engine reported" in body and "boom" in body

    def test_an_unexpected_shape_does_not_crash_the_page(self):
        """The engine REST surface is the source of truth and this dashboard
        adapts to it rather than crashing on drift."""
        with _client(["not", "a", "dict"]) as c:
            resp = c.get("/diagnostics/data-intake")
        assert resp.status_code == 200
        assert "unexpected payload shape" in resp.text


class TestWiring:
    def test_the_page_is_in_the_nav(self):
        """A page reachable only by typing its URL is 'measured but nowhere to
        look' one step removed."""
        from pathlib import Path
        base = Path(__file__).resolve().parents[1] / "app/templates/base.html"
        assert "/diagnostics/data-intake" in base.read_text()

    def test_the_route_is_registered(self):
        from app.main import app
        assert any(
            getattr(r, "path", "") == "/diagnostics/data-intake" for r in app.routes
        )


class TestLiveTickPanel:
    """Phase 2a shipped its engine payload and NO ops surface — the panel below
    did not exist, so the drift measurement its handover flag waits on had
    nowhere to be read. "A dark change ships together with the ops surface that
    shows what it is doing" is the rule, and this is it."""

    def test_the_panel_renders(self):
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "Live aggressive trades" in body

    def test_the_handover_flag_is_shown_as_a_flag_not_as_health(self):
        """A working feed is not a reason to change what a live gate sees."""
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "SEED SNAPSHOT" in body
        assert "TICKS_LIVE_FOR_CONSUMERS" in body

    def test_the_handover_reads_differently_once_flipped(self):
        r = _report()
        r["live_ticks"]["serving_consumers"] = True
        with _client(r) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "LIVE SERIES" in body

    def test_the_drift_measurement_is_on_screen(self):
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "Drift against the seeded store" in body
        assert "60 min" in body   # median gap, badged

    def test_a_subscription_that_never_delivered_is_named(self):
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "never delivered" in body and "NOPEUSDT" in body

    def test_the_uptime_caveat_is_stated(self):
        """The seed snapshot is freshest right after a restart, so a page read
        minutes after a deploy understates the very error it is sizing."""
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "function of" in body and "uptime" in body


class TestReportedFaultsThatAreNotHappening:
    def test_a_pool_whose_silence_is_expected_says_so(self):
        r = _report()
        r["pools"][0]["silence_is_expected"] = True
        r["pools"][0]["silence_budget_s"] = 12_000.0
        with _client(r) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "silence expected on this pool" in body

    def test_stale_leads_with_the_scanned_count(self):
        """A rotated-out mover's bucket freezes by design. Pooling it with a
        core pair reports a real fault at several times its true size."""
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "scanned / all" in body
        assert "in the live scan universe" in body

    def test_an_unreadable_volume_profile_is_not_rendered_as_zero(self):
        """An all-zero column is a claim about the reader before it is a claim
        about the system — a guessed attribute name returns an empty dict just
        as convincingly for "empty" as for "wrong name"."""
        with _client(_report()) as c:
            body = c.get("/diagnostics/data-intake").text
        assert "unreadable: no _results attribute" in body

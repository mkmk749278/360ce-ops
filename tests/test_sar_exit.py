"""SAR exit A/B panel — reducer units + route integration.

The reducers mirror the engine's ``src/sar_exit_shadow.summarize_sar_exit`` and
``src/suppression_audit.candidate_outcome`` math (360-v2); the fixtures here pin
that parity on plain data, since ops cannot import engine code.

The panel's whole job is to keep two counterfactual arms readable *as a pair*
and out of the strategy rollup, so the tests that matter most are the ones
asserting a half-resolved pair never renders and a thin arm never reads as a
winner.
"""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.data_volume import DataVolumeReader  # noqa: E402
from app.routes.sar_exit import (  # noqa: E402
    SAR_CLOSED_SL,
    SAR_CLOSED_TP1,
    SAR_CLOSED_TRAIL,
    SAR_CLOSED_WINDOW,
    SAR_RUNNING,
    filter_sar_signals,
    mark_running_rows,
    reduce_ledger_status,
    summarize_rows,
    reduce_sar_exit,
    reduce_sar_pairs,
    reduce_sar_signals,
    reduce_totals,
)
from app.routes.strategy_lab import (  # noqa: E402
    is_measurement_variant,
    reduce_per_strategy,
)
from app.main import app  # noqa: E402

CTX = "OVERLAP/MARKUP/NORMAL/BTC_NEUTRAL"


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


def _edge_records(n: int, won: bool = True, r: float = 1.0, src: str = "shadow"):
    return [
        {"won": won, "pnl_pct": r, "r": r if won else -1.0, "mfe": 1.0,
         "ts": "2026-07-25T00:00:00+00:00", "src": src}
        for _ in range(n)
    ]


def _arm(suffix: str, *, ts: float, cls: str | None, **kw) -> dict:
    rec = {
        "gate_name": "sar_exit_shadow:base" if suffix == "@SARBASE" else "sar_exit_shadow:trail",
        "setup_class": f"SR_FLIP_RETEST{suffix}",
        "symbol": "BTCUSDT", "side": "LONG", "channel": "scalp",
        "entry": 100.0, "stop_loss": 99.0, "tp1": 102.0, "sl_distance": 1.0,
        "context_key": CTX, "suppress_timestamp": ts,
        "classification": cls,
        "exit_model": "trailing" if suffix == "@SAREXIT" else "static",
    }
    rec.update(kw)
    return rec


def _pair(ts: float, *, base_cls="WOULD_WIN", sar_cls="WOULD_WIN", sar_exit=103.5,
          provenance=""):
    # Both arms share the provenance — they are one candidate, and a pair split
    # across the source filter would corrupt both halves of the comparison.
    return [
        _arm("@SARBASE", ts=ts, cls=base_cls, provenance=provenance),
        _arm("@SAREXIT", ts=ts + 0.001, cls=sar_cls, trail_exit_price=sar_exit,
             trail_exit_reason="trail", trail_hold_min=90.0, provenance=provenance),
    ]


class TestRollup:
    def test_a_measured_pair_names_its_leader(self):
        rows = [
            {"strategy": "SR_FLIP_RETEST@SAREXIT", "n": 100, "win_rate": 0.40, "avg_r": 0.30},
            {"strategy": "SR_FLIP_RETEST@SARBASE", "n": 100, "win_rate": 0.55, "avg_r": -0.10},
        ]
        out = reduce_sar_exit(rows, min_sample=15)
        assert len(out) == 1
        assert out[0]["strategy"] == "SR_FLIP_RETEST"
        assert out[0]["leader"] == "SAR"
        assert out[0]["delta_r"] == 0.4
        # The lower win rate must not decide it — small-often-lose /
        # big-rarely-win is the signature under test, not a disqualifier.
        assert out[0]["sar"]["win_rate"] < out[0]["base"]["win_rate"]

    def test_a_thin_arm_reads_measuring_never_a_winner(self):
        rows = [
            {"strategy": "SR_FLIP_RETEST@SAREXIT", "n": 80, "win_rate": 0.4, "avg_r": 0.9},
            {"strategy": "SR_FLIP_RETEST@SARBASE", "n": 3, "win_rate": 0.5, "avg_r": -0.1},
        ]
        out = reduce_sar_exit(rows, min_sample=15)
        assert out[0]["leader"] == "MEASURING"
        assert out[0]["delta_r"] is None

    def test_other_measurement_arms_are_ignored(self):
        rows = [
            {"strategy": "SR_FLIP_RETEST@ATR", "n": 50, "win_rate": 0.5, "avg_r": 0.2},
            {"strategy": "SR_FLIP_RETEST@TUNED", "n": 50, "win_rate": 0.5, "avg_r": 0.2},
            {"strategy": "SR_FLIP_RETEST", "n": 50, "win_rate": 0.5, "avg_r": 0.2},
        ]
        assert reduce_sar_exit(rows) == []


class TestMeasurementArmsStayOutOfTheStrategyRollup:
    """Regression for a real drift: ops knew only @FIXED/@ATR while the engine
    had been writing @TUNED/@DSV2/@GOV for over a week, so those arms were
    counted as strategies — double-counting the candidates they were stamped
    from and moving the numbers strategy decisions read."""

    def test_every_engine_measurement_suffix_is_recognised(self):
        for sfx in ("@FIXED", "@ATR", "@TUNED", "@DSV2", "@GOV", "@SARBASE", "@SAREXIT"):
            assert is_measurement_variant(f"SR_FLIP_RETEST{sfx}"), sfx
        assert not is_measurement_variant("SR_FLIP_RETEST")

    def test_arms_do_not_appear_as_strategies(self):
        rows = [
            {"strategy": "SR_FLIP_RETEST", "context_key": CTX, "n": 20, "n_emitted": 0,
             "n_suppressed": 20, "n_shadow": 0, "win_rate": 0.5, "avg_r": 0.1,
             "edge_r": 0.1, "avg_pnl_pct": 0.1},
            {"strategy": "SR_FLIP_RETEST@SAREXIT", "context_key": CTX, "n": 20, "n_emitted": 0,
             "n_suppressed": 0, "n_shadow": 20, "win_rate": 0.4, "avg_r": 0.9,
             "edge_r": 0.9, "avg_pnl_pct": 0.9},
            {"strategy": "SR_FLIP_RETEST@TUNED", "context_key": CTX, "n": 20, "n_emitted": 0,
             "n_suppressed": 0, "n_shadow": 20, "win_rate": 0.4, "avg_r": 0.9,
             "edge_r": 0.9, "avg_pnl_pct": 0.9},
        ]
        out = reduce_per_strategy(rows)
        assert [r["strategy"] for r in out] == ["SR_FLIP_RETEST"]
        assert out[0]["n"] == 20, "measurement arms inflated the strategy's sample"


class TestPairing:
    def test_arms_pair_in_stamp_order(self):
        recs = _pair(1000.0) + _pair(2000.0, sar_exit=101.0)
        pairs = reduce_sar_pairs(recs)
        assert len(pairs) == 2
        # Newest first.
        assert pairs[0]["stamped_at"] == 2000.0
        assert pairs[0]["sar_exit_price"] == 101.0
        assert pairs[1]["sar_exit_price"] == 103.5

    def test_a_half_resolved_pair_is_not_shown(self):
        """Half a pair is not a comparison — showing it invites reading the
        resolved arm on its own."""
        recs = [
            _arm("@SARBASE", ts=1000.0, cls="WOULD_WIN"),
            _arm("@SAREXIT", ts=1000.001, cls=None),
        ]
        assert reduce_sar_pairs(recs) == []

    def test_r_math_matches_the_engine_for_both_arms(self):
        pairs = reduce_sar_pairs(_pair(1000.0))
        p = pairs[0]
        # Static WIN → R_to_TP1 = |102-100| / 1.0
        assert p["base_r"] == 2.0
        # Trailing → continuous: (103.5-100) / 1.0, past TP1 and the R says so
        assert p["sar_r"] == 3.5
        assert p["delta_r"] == 1.5

    def test_static_loss_and_short_side(self):
        recs = [
            _arm("@SARBASE", ts=1.0, cls="WOULD_LOSE", side="SHORT"),
            _arm("@SAREXIT", ts=1.001, cls="WOULD_LOSE", side="SHORT",
                 trail_exit_price=100.4, trail_exit_reason="trail"),
        ]
        p = reduce_sar_pairs(recs)[0]
        assert p["base_r"] == -1.0
        # SHORT: (100 - 100.4) / 1.0
        assert abs(p["sar_r"] - (-0.4)) < 1e-9

    def test_expired_static_arm_marks_to_the_final_close(self):
        recs = [
            _arm("@SARBASE", ts=1.0, cls="WOULD_EXPIRE", post_price_final=100.7),
            _arm("@SAREXIT", ts=1.001, cls="WOULD_EXPIRE", trail_exit_price=100.2,
                 trail_exit_reason="window"),
        ]
        p = reduce_sar_pairs(recs)[0]
        assert abs(p["base_r"] - 0.7) < 1e-9
        assert abs(p["sar_r"] - 0.2) < 1e-9

    def test_bad_shapes_do_not_crash(self):
        assert reduce_sar_pairs({"error": "missing"}) == []
        assert reduce_sar_pairs(None) == []
        assert reduce_sar_pairs([None, 5, "x"]) == []


class TestTotals:
    def test_aggregates_both_arms(self):
        recs = (
            _pair(1.0, sar_exit=103.0)      # base +2.0, sar +3.0
            + _pair(2.0, base_cls="WOULD_LOSE", sar_cls="WOULD_LOSE", sar_exit=99.5)
        )                                    # base -1.0, sar -0.5
        t = reduce_totals(reduce_sar_pairs(recs))
        assert t["n"] == 2
        assert t["base_total_r"] == 1.0
        assert t["sar_total_r"] == 2.5
        assert t["delta_total_r"] == 1.5
        assert t["base_pf"] == 2.0          # 2.0 gain / 1.0 loss
        assert t["sar_pf"] == 6.0           # 3.0 gain / 0.5 loss
        assert t["sar_win_rate"] == 0.5

    def test_profit_factor_is_none_with_no_losers_rather_than_infinite(self):
        t = reduce_totals(reduce_sar_pairs(_pair(1.0)))
        assert t["sar_pf"] is None, "a PF with no losing trades would be a lie"

    def test_empty(self):
        assert reduce_totals([])["n"] == 0


class TestLedgerStatus:
    def test_empty_ledger_reads_dark_not_broken(self):
        st = reduce_ledger_status([], [])
        assert st["state"] == "dark"
        assert "dark by design" in st["detail"]

    def test_stamped_but_unresolved_reads_measuring(self):
        recs = [_arm("@SARBASE", ts=1.0, cls=None), _arm("@SAREXIT", ts=1.001, cls=None)]
        st = reduce_ledger_status(recs, [])
        assert st["state"] == "measuring"
        assert st["pending"] == 2 and st["classified"] == 0

    def test_resolved_reads_live(self):
        recs = _pair(1.0)
        st = reduce_ledger_status(recs, reduce_sar_pairs(recs))
        assert st["state"] == "live"
        assert st["classified"] == 2 and st["pairs"] == 1

    def test_missing_file_reads_unavailable(self):
        st = reduce_ledger_status({"error": "missing: sar_exit_candidates.json"}, [])
        assert st["state"] == "unavailable"
        assert "missing" in st["detail"]


class TestSarSignalFeed:
    """The signal-shaped view. A SAR trade has no TP and no SL — the trail is
    its only exit — so those columns must not exist, and a still-running trade
    must read as 'not yet', never as a flat zero."""

    def test_running_trades_are_listed_with_no_exit_yet(self):
        recs = [
            _arm("@SARBASE", ts=1000.0, cls=None),
            _arm("@SAREXIT", ts=1000.001, cls=None),
        ]
        rows = reduce_sar_signals(recs)
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == SAR_RUNNING
        assert row["status_class"] == "st-active"
        # Blank, not zero — "no exit yet" must not read as a flat trade.
        assert row["exit_price"] is None
        assert row["r_multiple"] is None
        assert row["pnl_pct"] is None
        assert row["delta_r"] is None
        assert row["entry"] == 100.0

    def test_a_trail_close_carries_exit_price_r_and_pnl(self):
        rows = reduce_sar_signals(_pair(1000.0, sar_exit=103.5))
        row = rows[0]
        assert row["status"] == SAR_CLOSED_TRAIL
        assert row["status_class"] == "st-tp"
        assert row["exit_price"] == 103.5
        assert row["r_multiple"] == 3.5
        assert row["pnl_pct"] == pytest.approx(3.5)     # (103.5-100)/100
        assert row["delta_r"] == 1.5                    # 3.5 SAR − 2.0 live
        assert row["hold_min"] == 90.0

    def test_a_window_expiry_is_its_own_status(self):
        recs = [
            _arm("@SARBASE", ts=1.0, cls="WOULD_EXPIRE", post_price_final=100.5),
            _arm("@SAREXIT", ts=1.001, cls="WOULD_EXPIRE", trail_exit_price=100.2,
                 trail_exit_reason="window", trail_hold_min=2880.0),
        ]
        row = reduce_sar_signals(recs)[0]
        assert row["status"] == SAR_CLOSED_WINDOW
        assert row["status_class"] == "st-expired"
        assert row["hold_min"] == 2880.0

    def test_a_live_stop_close_is_its_own_status_not_no_data(self):
        """Conditional handover (engine 2026-07-27): a SAR trade can now end on
        the live geometry it started behind. Before this mapping existed those
        rows fell through to NO_DATA — the page reporting "couldn't resolve it"
        about trades that resolved perfectly well."""
        recs = [
            _arm("@SARBASE", ts=1.0, cls="WOULD_LOSE"),
            _arm("@SAREXIT", ts=1.001, cls="WOULD_LOSE", trail_exit_price=99.0,
                 trail_exit_reason="static_sl", trail_hold_min=45.0),
        ]
        row = reduce_sar_signals(recs)[0]
        assert row["status"] == SAR_CLOSED_SL
        assert row["status_class"] == "st-sl"
        assert row["exit_price"] == 99.0

    def test_a_live_target_close_is_its_own_status(self):
        recs = [
            _arm("@SARBASE", ts=1.0, cls="WOULD_WIN", tp1=102.0),
            _arm("@SAREXIT", ts=1.001, cls="WOULD_WIN", trail_exit_price=102.0,
                 trail_exit_reason="static_tp1", trail_hold_min=60.0),
        ]
        row = reduce_sar_signals(recs)[0]
        assert row["status"] == SAR_CLOSED_TP1
        assert row["status_class"] == "st-tp"

    def test_handover_bars_reach_the_row(self):
        """None = never handed over = this row IS the control arm. 0 is a real
        handover (onside at entry) and must survive being the falsiest value."""
        recs = [
            _arm("@SARBASE", ts=1.0, cls="WOULD_WIN", tp1=102.0),
            _arm("@SAREXIT", ts=1.001, cls="WOULD_WIN", trail_exit_price=103.0,
                 trail_exit_reason="trail", sar_handover_bars=0),
        ]
        assert reduce_sar_signals(recs)[0]["handover_bars"] == 0
        recs2 = [
            _arm("@SARBASE", ts=2.0, cls="WOULD_LOSE"),
            _arm("@SAREXIT", ts=2.001, cls="WOULD_LOSE", trail_exit_price=99.0,
                 trail_exit_reason="static_sl"),
        ]
        assert reduce_sar_signals(recs2)[0]["handover_bars"] is None

    def test_short_side_pnl_is_signed_from_the_short_direction(self):
        recs = [
            _arm("@SARBASE", ts=1.0, cls="WOULD_WIN", side="SHORT", tp1=98.0),
            _arm("@SAREXIT", ts=1.001, cls="WOULD_WIN", side="SHORT",
                 trail_exit_price=97.0, trail_exit_reason="trail"),
        ]
        row = reduce_sar_signals(recs)[0]
        assert row["r_multiple"] == 3.0                 # (100-97)/1.0
        assert row["pnl_pct"] == pytest.approx(3.0)     # short profits as price falls

    def test_newest_first_and_bad_shapes_survive(self):
        recs = _pair(1000.0) + _pair(2000.0, sar_exit=101.0)
        rows = reduce_sar_signals(recs)
        assert [r["stamped_at"] for r in rows] == [2000.001, 1000.001]
        assert reduce_sar_signals({"error": "missing"}) == []
        assert reduce_sar_signals(None) == []
        assert reduce_sar_signals([None, 7, "x"]) == []

    def test_filters(self):
        recs = (
            _pair(1000.0)
            + [_arm("@SARBASE", ts=2000.0, cls=None), _arm("@SAREXIT", ts=2000.001, cls=None)]
        )
        rows = reduce_sar_signals(recs)
        assert len(rows) == 2
        assert len(filter_sar_signals(rows, status=SAR_RUNNING)) == 1
        assert len(filter_sar_signals(rows, status=SAR_CLOSED_TRAIL)) == 1
        assert len(filter_sar_signals(rows, strategy="SR_FLIP_RETEST")) == 2
        assert len(filter_sar_signals(rows, strategy="NOPE")) == 0


class TestSourceFilter:
    """Delivered vs queued-then-dropped vs gate-suppressed. Only the delivered
    subset can justify changing what subscribers receive, so it must be
    separable, must never be padded with unknown-provenance rows, and must
    never absorb the queued stage — that conflation reported 98 "emitted" in a
    window where 3 signals reached the feed (engine fix 2026-07-25)."""

    def _mixed(self):
        return (
            _pair(1000.0, sar_exit=103.0, provenance="emitted")       # +3.0R
            + _pair(2000.0, sar_exit=99.0, provenance="suppressed")   # -1.0R
            + _pair(3000.0, sar_exit=104.0)                           # unknown
            + _pair(5000.0, sar_exit=102.0, provenance="enqueued")    # +2.0R
        )

    def test_all_shows_every_candidate(self):
        rows = reduce_sar_signals(self._mixed())
        assert len(filter_sar_signals(rows)) == 4

    def test_emitted_never_absorbs_the_queued_stage(self):
        """A queued candidate the router dropped was seen by nobody. Counting
        it as delivered is the bug this filter exists to prevent."""
        rows = reduce_sar_signals(self._mixed())
        emitted = filter_sar_signals(rows, source="emitted")
        assert len(emitted) == 1
        assert all(r["provenance"] == "emitted" for r in emitted)

    def test_queued_is_its_own_separable_stage(self):
        rows = reduce_sar_signals(self._mixed())
        queued = filter_sar_signals(rows, source="enqueued")
        assert len(queued) == 1
        assert queued[0]["r_multiple"] == 2.0

    def test_emitted_narrows_to_signals_that_went_out(self):
        rows = reduce_sar_signals(self._mixed())
        emitted = filter_sar_signals(rows, source="emitted")
        assert len(emitted) == 1
        assert emitted[0]["r_multiple"] == 3.0

    def test_suppressed_narrows_to_gate_kills(self):
        rows = reduce_sar_signals(self._mixed())
        supp = filter_sar_signals(rows, source="suppressed")
        assert len(supp) == 1
        assert supp[0]["r_multiple"] == -1.0

    def test_unknown_provenance_matches_neither_filter(self):
        """Guessing an old record into 'emitted' would inflate exactly the
        number an adoption decision reads."""
        rows = reduce_sar_signals(self._mixed())
        assert len(filter_sar_signals(rows, source="emitted")) == 1
        assert len(filter_sar_signals(rows, source="suppressed")) == 1
        assert len(filter_sar_signals(rows, source="enqueued")) == 1
        # 4 total, 1+1+1 named → the unknown row is reachable only under "All".
        assert sum(1 for r in rows if r["provenance"] == "") == 1

    def test_source_composes_with_the_other_filters(self):
        recs = self._mixed() + [
            _arm("@SARBASE", ts=4000.0, cls=None, provenance="emitted"),
            _arm("@SAREXIT", ts=4000.001, cls=None, provenance="emitted"),
        ]
        rows = reduce_sar_signals(recs)
        both = filter_sar_signals(rows, source="emitted", status=SAR_RUNNING)
        assert len(both) == 1
        assert both[0]["stamped_at"] == 4000.001


class TestFilteredSummary:
    def test_averages_cover_resolved_trades_only(self):
        """A running trade has no R. Scoring it 0R would drag the average
        toward zero and make the arm look flat while it is still measuring."""
        recs = _pair(1000.0, sar_exit=103.0) + [
            _arm("@SARBASE", ts=2000.0, cls=None),
            _arm("@SAREXIT", ts=2000.001, cls=None),
        ]
        s = summarize_rows(reduce_sar_signals(recs))
        assert s["n"] == 2
        assert s["running"] == 1
        assert s["closed"] == 1
        assert s["avg_r"] == 3.0          # not 1.5 — the running trade is excluded
        assert s["win_rate"] == 1.0

    def test_delta_covers_only_fully_paired_trades(self):
        recs = _pair(1000.0, sar_exit=103.0)          # base +2.0, sar +3.0
        s = summarize_rows(reduce_sar_signals(recs))
        assert s["compared"] == 1
        assert s["avg_delta_r"] == 1.0

    def test_nothing_resolved_yet(self):
        recs = [_arm("@SARBASE", ts=1.0, cls=None), _arm("@SAREXIT", ts=1.001, cls=None)]
        s = summarize_rows(reduce_sar_signals(recs))
        assert s["n"] == 1 and s["closed"] == 0
        assert s["avg_r"] is None and s["win_rate"] is None

    def test_empty(self):
        s = summarize_rows([])
        assert s["n"] == 0 and s["avg_r"] is None


class TestMarkToMarket:
    """Without a live mark the tab is a dead list for 48h — every row RUNNING,
    every number blank. But the mark must never be mistaken for the arm's
    result: both arms share the entry, so the unrealized move is identical for
    both and says nothing about which exit wins."""

    def _running(self, side="LONG"):
        return reduce_sar_signals([
            _arm("@SARBASE", ts=1.0, cls=None, side=side),
            _arm("@SAREXIT", ts=1.001, cls=None, side=side),
        ])

    def test_long_marks_up_and_short_marks_down(self):
        rows = mark_running_rows(self._running("LONG"), {"BTCUSDT": 103.0})
        assert rows[0]["current_price"] == 103.0
        assert rows[0]["unrealized_pct"] == pytest.approx(3.0)

        rows = mark_running_rows(self._running("SHORT"), {"BTCUSDT": 97.0})
        # A short profits as price falls.
        assert rows[0]["unrealized_pct"] == pytest.approx(3.0)

    def test_a_short_going_against_you_marks_negative(self):
        rows = mark_running_rows(self._running("SHORT"), {"BTCUSDT": 102.0})
        assert rows[0]["unrealized_pct"] == pytest.approx(-2.0)

    def test_realized_columns_are_never_written(self):
        """An unrealized number in a realized column is how a still-open trade
        gets read as a finished one."""
        rows = mark_running_rows(self._running(), {"BTCUSDT": 150.0})
        assert rows[0]["r_multiple"] is None
        assert rows[0]["pnl_pct"] is None
        assert rows[0]["delta_r"] is None
        assert rows[0]["exit_price"] is None

    def test_closed_rows_are_not_marked(self):
        rows = reduce_sar_signals(_pair(1.0, sar_exit=103.5))
        mark_running_rows(rows, {"BTCUSDT": 999.0})
        assert "current_price" not in rows[0]
        # Its realized numbers are untouched.
        assert rows[0]["r_multiple"] == 3.5

    def test_a_missing_price_leaves_the_row_blank_not_zero(self):
        rows = mark_running_rows(self._running(), {})
        assert "current_price" not in rows[0]
        assert "unrealized_pct" not in rows[0]

    def test_a_junk_price_is_ignored(self):
        rows = mark_running_rows(self._running(), {"BTCUSDT": 0.0})
        assert "current_price" not in rows[0]


class TestSarExitRoutes:
    def _stub_volume(self, monkeypatch, *, ledger=None, edge=None):
        now = time.time()
        monkeypatch.setattr(DataVolumeReader, "market_context", lambda self: {
            "context_key": CTX, "mc_session": "OVERLAP", "mc_phase": "MARKUP",
            "mc_volatility": "NORMAL", "mc_funding": "NEUTRAL",
            "mc_rotation": "BTC_NEUTRAL", "generated_at": now,
            "generated_at_iso": "2026-07-25T10:00:00Z",
        })
        monkeypatch.setattr(DataVolumeReader, "strategy_edge", lambda self: edge if edge is not None else {
            f"SR_FLIP_RETEST@SARBASE|{CTX}": _edge_records(20, won=False),
            f"SR_FLIP_RETEST@SAREXIT|{CTX}": _edge_records(20, won=True, r=1.5),
        })
        monkeypatch.setattr(
            DataVolumeReader, "sar_exit_candidates",
            lambda self: ledger if ledger is not None else _pair(now),
        )

    def test_requires_auth(self):
        with TestClient(app) as client:
            r = client.get("/sar-exit", follow_redirects=False)
            assert r.status_code == 302

    def test_page_renders_all_panels(self, monkeypatch):
        self._stub_volume(monkeypatch)
        with TestClient(app) as client:
            _login(client)
            r = client.get("/sar-exit")
            assert r.status_code == 200
            assert "SAR exit A/B" in r.text
            assert "SR_FLIP_RETEST" in r.text
            assert "Paired totals" in r.text
            assert "Paired trades" in r.text
            assert "BTCUSDT" in r.text
            assert ">SAR</span>" in r.text        # leader badge from the fixture
            assert "LIVE" in r.text               # ledger status badge

    def test_dark_state_says_off_not_broken(self, monkeypatch):
        self._stub_volume(monkeypatch, ledger=[], edge={})
        with TestClient(app) as client:
            _login(client)
            r = client.get("/sar-exit")
            assert r.status_code == 200
            assert "DARK" in r.text
            assert "dark by design" in r.text
            assert "No completed pairs yet" in r.text

    def test_missing_ledger_renders_rather_than_crashing(self, monkeypatch):
        self._stub_volume(
            monkeypatch,
            ledger={"error": "missing: sar_exit_candidates.json"},
            edge={"error": "missing: strategy_edge_store.json"},
        )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/sar-exit")
            assert r.status_code == 200
            assert "UNAVAILABLE" in r.text

    def test_csv_export_carries_both_arms(self, monkeypatch):
        self._stub_volume(monkeypatch)
        with TestClient(app) as client:
            _login(client)
            r = client.get("/sar-exit/export.csv")
            assert r.status_code == 200
            assert "base_r" in r.text and "sar_r" in r.text
            assert "BTCUSDT" in r.text

    def test_json_export_carries_the_full_view(self, monkeypatch):
        self._stub_volume(monkeypatch)
        with TestClient(app) as client:
            _login(client)
            r = client.get("/sar-exit/export.json")
            assert r.status_code == 200
            body = r.json()
            for key in ("rollup", "pairs", "totals", "status"):
                assert key in body

    def test_nav_links_the_panel(self, monkeypatch):
        self._stub_volume(monkeypatch)
        with TestClient(app) as client:
            _login(client)
            r = client.get("/sar-exit")
            assert '/sar-exit' in r.text
            assert 'Strategy Lab' in r.text     # sibling under Autonomy


class TestSarSignalRoutes:
    def _stub_ledger(self, monkeypatch, ledger):
        monkeypatch.setattr(
            DataVolumeReader, "sar_exit_candidates", lambda self: ledger
        )

    def test_requires_auth(self):
        with TestClient(app) as client:
            r = client.get("/signals/sar", follow_redirects=False)
            assert r.status_code == 302

    def test_the_route_is_not_swallowed_by_the_signal_detail_catch_all(self, monkeypatch):
        """`/signals/{signal_id}` is a catch-all registered on another router.
        If ordering regresses, "sar" gets treated as a signal id and this page
        silently becomes a signal-detail 404/blank instead of the feed."""
        self._stub_ledger(monkeypatch, _pair(1000.0))
        with TestClient(app) as client:
            _login(client)
            r = client.get("/signals/sar")
            assert r.status_code == 200
            assert "SAR signals" in r.text
            assert "Signal not found" not in r.text

    def test_feed_renders_trades_without_tp_or_sl_columns(self, monkeypatch):
        self._stub_ledger(monkeypatch, _pair(1000.0))
        with TestClient(app) as client:
            _login(client)
            r = client.get("/signals/sar")
            assert r.status_code == 200
            assert "BTCUSDT" in r.text
            assert "CLOSED_TRAIL" in r.text
            assert "<th>Entry</th>" in r.text
            assert "<th>Closed at</th>" in r.text
            # A trailing exit has neither — rendering them would show a level
            # the trail never consults.
            assert "<th>TP1</th>" not in r.text
            assert "<th>SL</th>" not in r.text
            assert "<th>Stop</th>" not in r.text

    def test_filter_applies(self, monkeypatch):
        recs = (
            _pair(1000.0)
            + [_arm("@SARBASE", ts=2000.0, cls=None), _arm("@SAREXIT", ts=2000.001, cls=None)]
        )
        self._stub_ledger(monkeypatch, recs)
        with TestClient(app) as client:
            _login(client)
            r = client.get("/signals/sar?status=RUNNING")
            assert r.status_code == 200
            assert "1 of 2 rows" in r.text

    def test_empty_ledger_renders_the_dark_hint(self, monkeypatch):
        self._stub_ledger(monkeypatch, [])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/signals/sar")
            assert r.status_code == 200
            assert "No SAR trades stamped yet" in r.text
            assert "DARK" in r.text

    def test_csv_export(self, monkeypatch):
        self._stub_ledger(monkeypatch, _pair(1000.0))
        with TestClient(app) as client:
            _login(client)
            r = client.get("/signals/sar/export.csv")
            assert r.status_code == 200
            assert "exit_price" in r.text and "delta_r" in r.text
            assert "BTCUSDT" in r.text

    def test_source_filter_renders_and_narrows(self, monkeypatch):
        self._stub_ledger(
            monkeypatch,
            _pair(1000.0, sar_exit=103.0, provenance="emitted")
            + _pair(2000.0, sar_exit=99.0, provenance="suppressed"),
        )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/signals/sar")
            assert "Delivered to users (1)" in r.text
            assert "Gate-suppressed (1)" in r.text
            assert "DELIVERED" in r.text and "SUPPRESSED" in r.text

            r = client.get("/signals/sar?source=emitted")
            assert r.status_code == 200
            assert "1 of 2 rows" in r.text
            assert "Signals we actually delivered" in r.text
            # The emitted trade made +3.0R; the suppressed one (−1.0R) must be
            # excluded, so the total is +3.00 and not +2.00.
            assert "+3.00R" in r.text
            assert "+2.00R" not in r.text

    def test_unknown_provenance_is_disclosed_not_hidden(self, monkeypatch):
        self._stub_ledger(monkeypatch, _pair(1000.0))
        with TestClient(app) as client:
            _login(client)
            r = client.get("/signals/sar")
            assert "1 UNKNOWN" in r.text
            # Reachable under All, but not counted into either named source.
            assert "Delivered to users (0)" in r.text

    def test_csv_honours_the_source_filter(self, monkeypatch):
        self._stub_ledger(
            monkeypatch,
            _pair(1000.0, provenance="emitted")
            + _pair(2000.0, provenance="suppressed"),
        )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/signals/sar/export.csv?source=emitted")
            assert r.status_code == 200
            assert "provenance" in r.text
            assert r.text.count("emitted") >= 1
            assert "suppressed" not in r.text

    def test_running_rows_render_a_live_mark(self, monkeypatch):
        self._stub_ledger(monkeypatch, [
            _arm("@SARBASE", ts=1.0, cls=None, provenance="emitted"),
            _arm("@SAREXIT", ts=1.001, cls=None, provenance="emitted"),
        ])
        with TestClient(app) as client:
            _login(client)
            client.app.state.binance_klines._prices = {"BTCUSDT": 104.0}
            client.app.state.binance_klines._prices_at = time.monotonic()
            r = client.get("/signals/sar")
            assert r.status_code == 200
            assert "<th>Current</th>" in r.text
            assert "104" in r.text
            assert "+4.00%" in r.text

    def test_the_page_survives_binance_being_unavailable(self, monkeypatch):
        """A Binance hiccup must blank a column, never break the page."""
        self._stub_ledger(monkeypatch, [
            _arm("@SARBASE", ts=1.0, cls=None),
            _arm("@SAREXIT", ts=1.001, cls=None),
        ])

        async def _boom(*a, **k):
            raise RuntimeError("binance down")

        with TestClient(app) as client:
            _login(client)
            monkeypatch.setattr(
                client.app.state.binance_klines, "fetch_all_prices", _boom
            )
            r = client.get("/signals/sar")
            assert r.status_code == 200
            assert "RUNNING" in r.text

    def test_the_live_feed_still_works(self, monkeypatch):
        """The new route must not disturb the real signal book."""
        self._stub_ledger(monkeypatch, [])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/signals")
            assert r.status_code == 200
            assert "<h1>Signals</h1>" in r.text

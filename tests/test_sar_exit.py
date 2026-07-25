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

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.data_volume import DataVolumeReader  # noqa: E402
from app.routes.sar_exit import (  # noqa: E402
    reduce_ledger_status,
    reduce_sar_exit,
    reduce_sar_pairs,
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


def _pair(ts: float, *, base_cls="WOULD_WIN", sar_cls="WOULD_WIN", sar_exit=103.5):
    return [
        _arm("@SARBASE", ts=ts, cls=base_cls),
        _arm("@SAREXIT", ts=ts + 0.001, cls=sar_cls, trail_exit_price=sar_exit,
             trail_exit_reason="trail", trail_hold_min=90.0),
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

"""Strategy Lab page — reducer units + route integration.

The reducers mirror the engine's Wilson/verdict/gate-EV math
(src/strategy_edge.py, src/suppression_audit.py in 360-v2); the fixtures here
pin that parity on plain data.
"""
from __future__ import annotations

import pytest

import os
import time

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.data_volume import DataVolumeReader  # noqa: E402
from app.main import app  # noqa: E402
from app.routes.strategy_lab import (  # noqa: E402
    reduce_allocations,
    reduce_context_card,
    reduce_edge_matrix,
    reduce_gate_metrics,
    reduce_path_silence,
    reduce_geometry_ab,
    reduce_per_strategy,
)

CTX = "OVERLAP/MARKUP/NORMAL/BTC_NEUTRAL"


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


def _edge_records(n: int, won: bool = True, r: float = 1.0, src: str = "suppressed"):
    return [
        {"won": won, "pnl_pct": r, "r": r if won else -1.0, "mfe": 1.0,
         "ts": "2026-07-12T00:00:00+00:00", "src": src}
        for _ in range(n)
    ]


def _suppressed(cls: str, gate: str = "quiet_scalp_block") -> dict:
    return {
        "gate_name": gate,
        "setup_class": "BREAKOUT_RETEST",
        "side": "LONG",
        "entry": 100.0,
        "stop_loss": 99.0,
        "tp1": 101.5,
        "sl_distance": 1.0,
        "classification": cls,
    }


class TestContextCard:
    def test_reduces_fields_and_freshness(self):
        now = time.time()
        mc = {
            "context_key": CTX, "mc_session": "OVERLAP", "mc_phase": "MARKUP",
            "mc_volatility": "NORMAL", "mc_funding": "NEUTRAL",
            "mc_rotation": "BTC_NEUTRAL", "mc_is_weekend": False,
            "generated_at": now - 60, "generated_at_iso": "2026-07-12T10:00:00Z",
        }
        card = reduce_context_card(mc, now_ts=now)
        assert card["context_key"] == CTX
        assert card["session"] == "OVERLAP" and card["phase"] == "MARKUP"
        assert card["age_sec"] == 60
        assert card["stale"] is False

    def test_stale_and_error_states(self):
        now = time.time()
        assert reduce_context_card({"generated_at": now - 3600}, now_ts=now)["stale"] is True
        assert "error" in reduce_context_card({"error": "missing: x"})
        assert "error" in reduce_context_card(None)


class TestEdgeMatrix:
    def test_engine_parity_edge_and_verdicts(self):
        raw = {
            f"WINNER|{CTX}": _edge_records(20, won=True, r=1.5),
            f"LOSER|{CTX}": _edge_records(20, won=False),
            f"THIN|{CTX}": _edge_records(3, won=True),
        }
        rows = {r["strategy"]: r for r in reduce_edge_matrix(raw)}
        assert rows["WINNER"]["verdict"] == "STRONG"
        assert rows["WINNER"]["edge_r"] > 0.25
        assert rows["LOSER"]["verdict"] == "NEGATIVE"
        assert rows["THIN"]["verdict"] == "INSUFFICIENT_DATA"
        assert rows["THIN"]["edge_r"] is None
        assert rows["WINNER"]["n_suppressed"] == 20

    def test_alignment_from_persisted_affinity(self):
        raw = {f"BREAKOUT_RETEST|{CTX}": _edge_records(20)}
        affinity = {
            "BREAKOUT_RETEST": {"phases": ["MARKUP", "MARKDOWN"],
                                "sessions": ["OVERLAP", "NY", "LONDON"]}
        }
        rows = reduce_edge_matrix(raw, affinity)
        assert rows[0]["aligned"] is True
        raw2 = {"BREAKOUT_RETEST|ASIA/RANGE/NORMAL/BTC_NEUTRAL": _edge_records(20)}
        assert reduce_edge_matrix(raw2, affinity)[0]["aligned"] is False
        # No affinity map → unknown, not misaligned.
        assert reduce_edge_matrix(raw)[0]["aligned"] is None

    def test_error_dict_and_garbage_are_empty(self):
        assert reduce_edge_matrix({"error": "missing"}) == []
        assert reduce_edge_matrix(None) == []
        assert reduce_edge_matrix({"no-pipe-key": []}) == []


class TestPerStrategy:
    def test_aggregates_cells(self):
        raw = {
            f"A|{CTX}": _edge_records(20, won=True, r=1.0),
            "A|ASIA/RANGE/NORMAL/BTC_NEUTRAL": _edge_records(20, won=False),
        }
        agg = reduce_per_strategy(reduce_edge_matrix(raw))
        assert len(agg) == 1
        a = agg[0]
        assert a["n"] == 40 and a["cells"] == 2
        assert abs(a["win_rate"] - 0.5) < 1e-9
        assert a["best_cell"] is not None

    def test_geometry_variants_excluded_from_rollup(self):
        raw = {
            f"A|{CTX}": _edge_records(20, won=True, r=1.0),
            f"A@FIXED|{CTX}": _edge_records(20, won=False),
            f"A@ATR|{CTX}": _edge_records(20, won=True, r=1.0),
        }
        agg = reduce_per_strategy(reduce_edge_matrix(raw))
        assert [a["strategy"] for a in agg] == ["A"]
        assert agg[0]["n"] == 20  # arms not double-counted


class TestGeometryAb:
    def test_pools_arms_and_names_leader(self):
        raw = {
            f"A@FIXED|{CTX}": _edge_records(10, won=False),
            "A@FIXED|ASIA/RANGE/NORMAL/BTC_NEUTRAL": _edge_records(10, won=False),
            f"A@ATR|{CTX}": _edge_records(10, won=True, r=1.0),
            "A@ATR|ASIA/RANGE/NORMAL/BTC_NEUTRAL": _edge_records(10, won=True, r=1.0),
            # Thin ATR arm → MEASURING.
            f"B@FIXED|{CTX}": _edge_records(20, won=True, r=0.5),
            f"B@ATR|{CTX}": _edge_records(3, won=True, r=2.0),
            # Non-variant rows ignored here.
            f"A|{CTX}": _edge_records(20, won=True, r=1.0),
        }
        rows = reduce_geometry_ab(reduce_edge_matrix(raw))
        by_name = {r["strategy"]: r for r in rows}
        a = by_name["A"]
        assert a["fixed"]["n"] == 20 and a["atr"]["n"] == 20
        assert a["delta_r"] is not None and a["delta_r"] > 0
        assert a["leader"] == "ATR"
        b = by_name["B"]
        assert b["leader"] == "MEASURING" and b["delta_r"] is None
        assert rows[0]["strategy"] == "A"  # measured A/B ranks first

    def test_empty(self):
        assert reduce_geometry_ab([]) == []


class TestGateMetrics:
    def test_keep_and_drop_verdicts(self):
        records = (
            [_suppressed("WOULD_LOSE") for _ in range(25)]           # gate saves 1R each
            + [_suppressed("WOULD_WIN", gate="bad_gate") for _ in range(25)]  # gate costs 1.5R each
            + [_suppressed(None)]  # pending
        )
        # None classification means pending — build it explicitly.
        records[-1]["classification"] = None
        out = reduce_gate_metrics(records)
        assert out["by_gate"]["quiet_scalp_block"]["verdict"] == "KEEP"
        assert out["by_gate"]["bad_gate"]["verdict"] == "DROP"
        assert out["by_gate"]["bad_gate"]["would_win_pct"] == 100.0
        assert out["pending"] == 1
        assert out["classified"] == 50

    def test_small_sample_is_insufficient(self):
        out = reduce_gate_metrics([_suppressed("WOULD_LOSE") for _ in range(5)])
        assert out["by_gate"]["quiet_scalp_block"]["verdict"] == "INSUFFICIENT_SAMPLE"

    def test_error_dict_is_empty(self):
        out = reduce_gate_metrics({"error": "missing"})
        assert out["by_gate"] == {} and out["classified"] == 0


class TestAllocationsPanel:
    def test_reduces_payload(self):
        now = time.time()
        alloc = {
            "mode": "RECOMMENDATION_ONLY", "context_key": CTX,
            "activate": [{"strategy": "A", "weight": 0.35, "edge_r": 0.3,
                          "n": 20, "reason": "STRONG edge"}],
            "demote": [], "unallocated_weight": 0.65,
            "limits": {"max_concurrent_strategies": 6, "max_strategy_weight": 0.35},
            "cells_in_context": 4, "generated_at": now - 30,
            "generated_at_iso": "2026-07-12T10:00:00Z",
        }
        panel = reduce_allocations(alloc, now_ts=now)
        assert panel["mode"] == "RECOMMENDATION_ONLY"
        assert panel["activate"][0]["strategy"] == "A"
        assert panel["stale"] is False

    def test_error_state(self):
        assert "error" in reduce_allocations({"error": "missing"})


class TestStrategyLabRoutes:
    def _stub_volume(self, monkeypatch):
        now = time.time()
        monkeypatch.setattr(DataVolumeReader, "market_context", lambda self: {
            "context_key": CTX, "mc_session": "OVERLAP", "mc_phase": "MARKUP",
            "mc_volatility": "NORMAL", "mc_funding": "NEUTRAL",
            "mc_rotation": "BTC_NEUTRAL", "generated_at": now,
            "generated_at_iso": "2026-07-12T10:00:00Z",
            "strategy_affinity": {"BREAKOUT_RETEST": {
                "phases": ["MARKUP"], "sessions": ["OVERLAP"]}},
        })
        monkeypatch.setattr(DataVolumeReader, "strategy_edge", lambda self: {
            f"BREAKOUT_RETEST|{CTX}": _edge_records(20, won=True, r=1.5),
            f"BREAKOUT_RETEST@FIXED|{CTX}": _edge_records(20, won=False),
            f"BREAKOUT_RETEST@ATR|{CTX}": _edge_records(20, won=True, r=1.0),
        })
        monkeypatch.setattr(DataVolumeReader, "suppressed_candidates", lambda self: [
            _suppressed("WOULD_LOSE") for _ in range(25)
        ])
        monkeypatch.setattr(DataVolumeReader, "strategy_allocations", lambda self: {
            "mode": "RECOMMENDATION_ONLY", "context_key": CTX,
            "activate": [], "demote": [], "unallocated_weight": 1.0,
            "limits": {"max_concurrent_strategies": 6, "max_strategy_weight": 0.35},
            "cells_in_context": 1, "generated_at": now,
            "generated_at_iso": "2026-07-12T10:00:00Z",
        })

    def test_requires_auth(self):
        with TestClient(app) as client:
            r = client.get("/strategy-lab", follow_redirects=False)
            assert r.status_code == 302

    def test_page_renders_all_panels(self, monkeypatch):
        self._stub_volume(monkeypatch)
        with TestClient(app) as client:
            _login(client)
            r = client.get("/strategy-lab")
            assert r.status_code == 200
            assert "Strategy Lab" in r.text
            assert "BREAKOUT_RETEST" in r.text
            assert "STRONG" in r.text
            assert "quiet_scalp_block" in r.text
            assert "KEEP" in r.text
            assert "RECOMMENDATION_ONLY" in r.text
            assert "in design ctx" in r.text
            assert "Stop-geometry A/B" in r.text
            assert ">ATR</span>" in r.text  # leader badge from the A/B fixture

    def test_partial_returns_fragment(self, monkeypatch):
        self._stub_volume(monkeypatch)
        with TestClient(app) as client:
            _login(client)
            r = client.get("/_partial/strategy_lab")
            assert r.status_code == 200
            assert "<html" not in r.text  # fragment, not a full page
            assert "Strategy × Context edge matrix" in r.text

    def test_cold_state_renders_hints(self, monkeypatch):
        for name in ("market_context", "strategy_edge",
                     "suppressed_candidates", "strategy_allocations"):
            monkeypatch.setattr(
                DataVolumeReader, name,
                lambda self, _n=name: {"error": f"missing: {_n}.json"},
            )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/strategy-lab")
            assert r.status_code == 200
            assert "Matrix is cold" in r.text
            assert "No geometry pairs measured yet" in r.text


# --------------------------------------------------------------------------- #
# Pre-scoring gates (engine #839) — audited here, and kept out of the matrix
# --------------------------------------------------------------------------- #


def _supp(gate, setup, cls=None, pre=False, delta_r=None):
    rec = {
        "gate_name": gate, "setup_class": setup, "classification": cls,
        "entry": 100.0, "stop_loss": 97.0, "tp1": 106.0, "sl_distance": 3.0,
        "side": "LONG", "symbol": "AAAUSDT",
    }
    if pre:
        rec["pre_scoring"] = True
    return rec


def test_a_pre_scoring_gate_is_badged_rather_than_pooled_silently():
    """The rows are right; pooling them unlabelled with post-scoring gates
    would invite comparing two differently selected populations."""
    out = reduce_gate_metrics([
        _supp("setup_compat:regime_STRONG_TREND", "MEAN_REVERT", "WOULD_LOSE", pre=True),
        _supp("min_confidence", "SR_FLIP_RETEST", "WOULD_LOSE"),
    ])
    assert out["by_gate"]["setup_compat:regime_STRONG_TREND"]["pre_scoring"] is True
    assert out["by_gate"]["min_confidence"]["pre_scoring"] is False


def test_the_flag_comes_from_the_record_not_from_the_gate_name():
    """A renamed gate must not silently lose its badge."""
    out = reduce_gate_metrics([_supp("renamed_gate", "X", "WOULD_LOSE", pre=True)])
    assert out["by_gate"]["renamed_gate"]["pre_scoring"] is True


def test_path_silence_names_the_gate_that_stops_each_path():
    rows = reduce_path_silence([
        _supp("setup_compat:regime_STRONG_TREND", "MEAN_REVERT", "WOULD_LOSE", pre=True),
        _supp("setup_compat:regime_WEAK_TREND", "MEAN_REVERT", "WOULD_WIN", pre=True),
        _supp("setup_compat:regime_STRONG_TREND", "MEAN_REVERT", "WOULD_LOSE", pre=True),
        _supp("min_confidence", "SR_FLIP_RETEST", "WOULD_LOSE"),
    ])
    by = {r["setup_class"]: r for r in rows}
    assert by["MEAN_REVERT"]["n"] == 3
    assert by["MEAN_REVERT"]["top_gate"] == "setup_compat:regime_STRONG_TREND"
    assert by["MEAN_REVERT"]["top_gate_share"] == pytest.approx(66.67, abs=0.1)
    assert by["MEAN_REVERT"]["would_win_pct"] == pytest.approx(33.33, abs=0.1)
    # Sorted by suppressions descending — the paths losing most candidates first.
    assert rows[0]["setup_class"] == "MEAN_REVERT"


def test_a_path_killed_only_before_scoring_says_so():
    """It is not being rejected on confidence or context — it never got there."""
    rows = reduce_path_silence([
        _supp("setup_compat:regime_STRONG_TREND", "MEAN_REVERT", "WOULD_LOSE", pre=True),
    ])
    assert rows[0]["pre_scoring_only"] is True


def test_a_path_with_nothing_classified_refuses_rather_than_scoring_zero():
    """Blank means 'we do not know yet', not 'none would have won'."""
    rows = reduce_path_silence([_supp("setup_compat:channel", "RANGE_FADE", None, pre=True)])
    assert rows[0]["classified"] == 0
    assert rows[0]["would_win_pct"] is None


def test_path_silence_survives_a_bad_payload():
    assert reduce_path_silence(None) == []
    assert reduce_path_silence(["not a dict"]) == []


# ---------------------------------------------------------------------------
# The ring's denominator (2026-08-07).
#
# The engine has counted per-cell evictions since 2026-08-04 and persists them
# under the reserved ``__evicted__`` key, specifically so the matrix's verdicts
# could carry their denominator — its docstring quotes this repo's own rule.
# ``reduce_edge_matrix`` dropped the key on its ``"|" not in str(key)`` guard,
# so the Strategy Lab published 1,731 cells reading ``n = 50`` (the ring cap)
# with nothing on screen distinguishing fifty outcomes from five thousand.
# A field one repo writes and no repo reads — #817 with the arrow reversed.
# ---------------------------------------------------------------------------


def test_edge_matrix_carries_the_eviction_count_the_engine_persists():
    """Pinned against the engine's real key name and payload shape."""
    store = {
        f"MOVER_TREND_PULLBACK|{CTX}": _edge_records(50),
        f"MEAN_REVERT|{CTX}": _edge_records(20),
        # Engine shape: {"STRATEGY|context_key": count}, top-level, no "|" in
        # the reserved key itself so it cannot collide with a cell.
        "__evicted__": {f"MOVER_TREND_PULLBACK|{CTX}": 4_950},
    }
    rows = {r["strategy"]: r for r in reduce_edge_matrix(store)}

    capped = rows["MOVER_TREND_PULLBACK"]
    assert capped["n"] == 50
    assert capped["evicted"] == 4_950
    assert capped["seen"] == 5_000       # n=50 stood for five thousand outcomes
    assert capped["sampled"] is True
    assert capped["at_cap"] is True

    # A cell the engine reported and that evicted nothing is a POPULATION, and
    # must be distinguishable from one the engine said nothing about.
    sparse = rows["MEAN_REVERT"]
    assert sparse["evicted"] == 0
    assert sparse["sampled"] is False
    assert sparse["at_cap"] is False

    # The reserved key must never itself become a row.
    assert "__evicted__" not in rows


def test_absent_eviction_map_is_unknown_and_never_reads_as_zero():
    """A missing stamp is not a pass.

    The engine writes ``__evicted__`` only ``if _ev``, so an absent key means
    either an old engine or a store that never evicted — and nothing here may
    guess which. Rendering 0 would make an unmeasured cell read as a clean
    population, which is the flattering direction.
    """
    store = {f"MOVER_TREND_PULLBACK|{CTX}": _edge_records(50)}
    row = reduce_edge_matrix(store)[0]

    assert row["evicted"] is None
    assert row["seen"] is None
    assert row["sampled"] is None
    # `at_cap` is derivable from n alone, so it is still answerable.
    assert row["at_cap"] is True

    from app.routes.strategy_lab import matrix_sampling

    summary = matrix_sampling(reduce_edge_matrix(store))
    assert summary["reported"] is False
    assert summary["sampled"] is None
    assert summary["evicted_total"] is None
    assert summary["visible_frac"] is None
    assert summary["at_cap"] == 1        # still countable without the engine


def test_matrix_sampling_is_measured_over_the_whole_matrix_not_the_capped_render():
    """A sampling summary over the capped rows would describe the worst cells.

    ``split_matrix`` sorts most-evidence-first, so the capped render is exactly
    the population most likely to be at the ring cap. Measuring the summary
    there would report the matrix as far more sampled than it is.
    """
    from app.routes.strategy_lab import matrix_sampling, split_matrix

    store = {f"S{i}|{CTX}": _edge_records(50) for i in range(3)}
    store.update({f"T{i}|{CTX}": _edge_records(20) for i in range(3)})
    store["__evicted__"] = {f"S{i}|{CTX}": 100 for i in range(3)}

    rows = reduce_edge_matrix(store)
    summary = matrix_sampling(rows)

    assert summary["at_cap"] == 3
    assert summary["sampled"] == 3
    assert summary["evicted_total"] == 300
    assert summary["held_total"] == 3 * 50 + 3 * 20
    # 210 held of 510 seen — the page can see 41% of what was recorded.
    assert summary["visible_frac"] == pytest.approx(210 / 510)

    # Same reducer over the capped render would have said 3 of 3 at cap (100%).
    capped = split_matrix(rows)["rows"]
    assert matrix_sampling(capped)["at_cap"] == 3
    assert summary["decidable"] == 6


def test_matrix_csv_export_carries_the_denominator():
    """A spreadsheet is exactly where a rolling window gets averaged with an
    all-time one, so the stamps ride into the export (the structural lanes'
    rule, applied to the biggest table in the repo)."""
    from app.routes.strategy_lab import _MATRIX_COLS

    for col in ("evicted", "seen", "sampled"):
        assert col in _MATRIX_COLS

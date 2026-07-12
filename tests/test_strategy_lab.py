"""Strategy Lab page — reducer units + route integration.

The reducers mirror the engine's Wilson/verdict/gate-EV math
(src/strategy_edge.py, src/suppression_audit.py in 360-v2); the fixtures here
pin that parity on plain data.
"""
from __future__ import annotations

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

"""Why an armed rule promoted nothing — and the joint count the page lacked.

Added 2026-08-17, for the owner's *"still no LSR or any dark feed signal that
we enable is actually delivered to users"*. Two walls stood between an armed
rule and a subscriber:

1. `dark_promotion.decide` refused every candidate, and the engine's only
   counter was one integer per path. The engine now publishes a per-dimension
   refusal census; this page renders it.
2. The rows that DID promote were taken by the router's second layer — already
   measured, already on this page (`promoted_dropped` + reason).

The design defect that made the first wall invisible is the one these tests
mostly pin: **the evidence tables are marginal and the rule is a
conjunction.** Each dimension gets its own table, sorted by evidence, and the
rule built from them is the intersection of every tick — so the best-looking
cell of each can combine into a rule that matches nothing, while every number
on the page still reads well-evidenced.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app.data_sources import dark_promotion as dp  # noqa: E402


def _row(
    *,
    gate="setup_compat:regime_STRONG_TREND",
    regime="TRENDING_DOWN",
    regime_15m="",
    session="NY",
    side="SHORT",
    confidence=70.0,
    status="CLOSED_TP1",
    pnl=1.0,
    symbol="AAAUSDT",
    setup="LIQUIDITY_SWEEP_REVERSAL",
):
    return {
        "symbol": symbol, "side": side, "setup_class": setup,
        "dark_gate": gate, "regime": regime, "regime_15m": regime_15m,
        "context_key": f"{session}/MARKDOWN/NORMAL/BTC_NEUTRAL",
        "confidence": confidence, "status": status, "pnl_pct": pnl,
        "emitted_at": 1_786_000_000.0, "delivery": dp.DELIVERY_DARK,
    }


def _rule(**over):
    rule = {
        "setup_class": "LIQUIDITY_SWEEP_REVERSAL", "enabled": True,
        "gates": ["SETUP_COMPAT:REGIME_STRONG_TREND"],
        "regimes": ["*"], "sessions": ["*"], "direction": "any",
        "min_confidence": None, "max_per_day": 25,
    }
    rule.update(over)
    return rule


# --------------------------------------------------------------------------- #
# The conjunction — the number the page never had
# --------------------------------------------------------------------------- #


def test_a_rule_built_from_marginal_tables_can_select_nothing():
    """The defect, stated as a test.

    Both cells below are well-evidenced on their own table — one row each, and
    on a real book hundreds — and their intersection is empty. Nothing on the
    pre-2026-08-17 page could say so.
    """
    rows = [
        _row(gate="execution:overextended", regime="TRENDING_DOWN"),
        _row(gate="setup_compat:regime_STRONG_TREND", regime="RANGING"),
    ]
    ev_gate = dp.condition_evidence(rows, dimension="gate")
    ev_regime = dp.condition_evidence(rows, dimension="regime")
    assert {c["value"] for c in ev_gate["cells"]} == {
        "execution:overextended", "setup_compat:regime_STRONG_TREND"
    }
    assert {c["value"] for c in ev_regime["cells"]} == {"TRENDING_DOWN", "RANGING"}

    sel = dp.rule_selection(rows, _rule(
        gates=["EXECUTION:OVEREXTENDED"], regimes=["RANGING"],
    ))
    assert sel["n_selected"] == 0
    assert sel["n_refused"] == 2


def test_the_selection_is_summarised_on_the_rows_it_keeps():
    """A summary computed over the whole ledger above a rule that keeps a
    tenth of it is not a summary of anything the reader is looking at."""
    rows = [
        _row(regime="TRENDING_DOWN", pnl=3.0),
        _row(regime="RANGING", pnl=-9.0),
        _row(regime="RANGING", pnl=-9.0),
    ]
    sel = dp.rule_selection(rows, _rule(regimes=["TRENDING_DOWN"]))
    assert sel["n_selected"] == 1
    assert sel["selected"]["avg_pct"] == pytest.approx(3.0)
    assert sel["baseline"]["avg_pct"] == pytest.approx(-5.0)


def test_sole_blocker_says_what_one_edit_would_add():
    rows = [
        _row(regime="RANGING", session="NY"),          # regime only
        _row(regime="RANGING", session="LONDON"),      # regime + session
    ]
    sel = dp.rule_selection(
        rows, _rule(regimes=["TRENDING_DOWN"], sessions=["NY"])
    )
    assert sel["by_dimension"]["regime"] == 2
    assert sel["by_dimension"]["session"] == 1
    assert sel["sole_blocker"] == {"regime": 1}


def test_marginal_counts_sum_past_the_refusal_count_by_design():
    rows = [_row(gate="other:gate", regime="RANGING", session="ASIA")]
    sel = dp.rule_selection(rows, _rule(
        gates=["SETUP_COMPAT:REGIME_STRONG_TREND"],
        regimes=["TRENDING_DOWN"], sessions=["NY"],
    ))
    assert sel["n_refused"] == 1
    assert sum(sel["by_dimension"].values()) == 3
    assert sel["sole_blocker"] == {}


def test_a_trend_condition_abstains_on_a_range_label():
    """`with_trend` and `counter_trend` BOTH refuse a regime naming no trend —
    an unknown trend is not an aligned one, and it is not a misaligned one."""
    rows = [_row(regime="RANGING", side="SHORT")]
    assert dp.rule_selection(rows, _rule(direction="with_trend"))["n_selected"] == 0
    assert dp.rule_selection(rows, _rule(direction="counter_trend"))["n_selected"] == 0


def test_the_15m_regime_is_a_fallback_and_not_an_override():
    """A 5m label naming a trend wins; the 15m read only fills a range label."""
    fallback = [_row(regime="RANGING", regime_15m="TRENDING_DOWN", side="SHORT")]
    assert dp.rule_selection(fallback, _rule(direction="with_trend"))["n_selected"] == 1

    primary = [_row(regime="TRENDING_UP", regime_15m="TRENDING_DOWN", side="SHORT")]
    assert dp.rule_selection(primary, _rule(direction="with_trend"))["n_selected"] == 0


def test_an_empty_allow_list_matches_nothing_rather_than_everything():
    """Fail-closed, the same direction as the engine. An empty list reads as
    'unrestricted' in most config, and that reading is what turns a
    half-finished rule into a live promotion of everything a path emits."""
    rows = [_row()]
    assert dp.rule_selection(rows, _rule(gates=[]))["n_selected"] == 0


def test_only_closed_rows_are_scored():
    rows = [_row(status="OPEN", pnl=None), _row(status="CLOSED_TP1", pnl=1.0)]
    sel = dp.rule_selection(rows, _rule())
    assert sel["n_closed"] == 1
    assert sel["n_selected"] == 1


def test_no_rule_yields_no_selection_rather_than_the_whole_book():
    sel = dp.rule_selection([_row()], None)
    assert sel["has_rule"] is False
    assert sel["n_selected"] == 0


# --------------------------------------------------------------------------- #
# The engine's census — three states, and the middle one is the point
# --------------------------------------------------------------------------- #


def _snap(runtime=None):
    snap = {"master_enabled": True, "dark_lane_enabled": True, "rules": []}
    if runtime is not None:
        snap["runtime"] = runtime
    return snap


def test_a_silent_engine_is_not_reported_rather_than_zero():
    """An engine predating the census and one refusing every candidate read
    identically if this collapses to zero — which is the whole reason the
    census exists. Also the isolated-mode case: an API container that never
    received the engine's published block."""
    assert dp.engine_refusals(_snap(), "LIQUIDITY_SWEEP_REVERSAL")["state"] == "not_reported"
    assert dp.engine_refusals(_snap({}), "LIQUIDITY_SWEEP_REVERSAL")["state"] == "not_reported"
    assert dp.engine_refusals({}, "LIQUIDITY_SWEEP_REVERSAL")["state"] == "not_reported"


def test_reporting_and_idle_is_its_own_state():
    """The benign case, and it names where to look next: no candidate reached
    the decision, so the rule is not what is stopping the path."""
    got = dp.engine_refusals(
        _snap({"source": "engine", "refusals": {}}), "LIQUIDITY_SWEEP_REVERSAL"
    )
    assert got["state"] == "idle"
    assert got["cell"]["total"] == 0


def test_the_census_is_read_off_the_engines_payload():
    runtime = {
        "source": "engine",
        "refusals": {"LIQUIDITY_SWEEP_REVERSAL": {
            "total": 610,
            "by_dimension": {"gate": 513, "direction": 97},
            "sole_blocker": {"direction": 97},
        }},
        "near_misses": [
            {"setup_class": "LIQUIDITY_SWEEP_REVERSAL", "symbol": "AAAUSDT",
             "unmet": ["direction"], "detail": "trend=UP via entry_regime"},
            {"setup_class": "MEAN_REVERT", "symbol": "BBBUSDT", "unmet": ["gate"]},
        ],
        "near_miss_ring": 60, "near_miss_seen": 610,
    }
    got = dp.engine_refusals(_snap(runtime), "LIQUIDITY_SWEEP_REVERSAL")
    assert got["state"] == "refusing"
    assert got["cell"]["sole_blocker"] == {"direction": 97}
    assert [m["symbol"] for m in got["near_misses"]] == ["AAAUSDT"], (
        "another path's near-misses must not appear under this card"
    )
    assert got["ring"] == 60 and got["seen"] == 610


def test_every_dimension_the_engine_can_report_has_ops_copy():
    """The panel iterates the engine's payload and looks each dimension up, so
    an unknown one renders badged rather than dropped. This asserts we are not
    *starting* with a gap — and it derives the list from the engine when the
    repo is present rather than from a copy kept here."""
    engine = pathlib.Path("/home/user/360-v2/src/dark_promotion.py")
    if not engine.exists():
        pytest.skip("engine repo not checked out beside ops")
    import re
    src = engine.read_text(encoding="utf-8")
    block = re.search(
        r"REFUSAL_DIMENSIONS: Tuple\[str, \.\.\.\] = \((.*?)\)", src, re.S
    )
    assert block, "the engine no longer declares REFUSAL_DIMENSIONS"
    names = re.findall(r"DIM_[A-Z_]+", block.group(1))
    values = {
        re.search(rf"^{n} = \"([a-z_]+)\"", src, re.M).group(1) for n in names
    }
    missing = values - set(dp.DIMENSION_REFUSAL_COPY)
    assert not missing, f"no ops copy for engine refusal dimension(s): {missing}"


# --------------------------------------------------------------------------- #
# Cross-repo contract: ops' replay must be the engine's predicate
#
# A fixture chooses a shape and then agrees with you about it. `rule_unmet` is
# a second implementation of `decide`'s conjunction, so it is driven against
# the REAL engine module — the only thing that keeps a mirror from drifting.
# --------------------------------------------------------------------------- #


def _engine_module():
    repo = pathlib.Path("/home/user/360-v2")
    if not (repo / "src" / "dark_promotion.py").exists():
        pytest.skip("engine repo not checked out beside ops")
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    try:
        from src import dark_promotion as engine_dp
    except Exception as exc:  # pragma: no cover - engine deps unavailable
        pytest.skip(f"engine module not importable: {exc}")
    return engine_dp


class _EngineSig:
    """A candidate built from an ops LEDGER ROW, so the two sides are fed the
    same facts — which is the point of the contract."""

    def __init__(self, row):
        self.setup_class = row["setup_class"]
        self.direction = type("D", (), {"value": row["side"]})()
        self.entry_regime = row["regime"]
        self.entry_regime_15m = row.get("regime_15m") or ""
        self.mc_session = dp.session_of(row)
        self.confidence = row["confidence"]
        self.symbol = row["symbol"]


@pytest.mark.parametrize("rule_over,row_over", [
    ({}, {}),
    ({"gates": ["EXECUTION:OVEREXTENDED"]}, {}),
    ({"regimes": ["TRENDING_UP"]}, {}),
    ({"sessions": ["LONDON"]}, {}),
    ({"direction": "with_trend"}, {"regime": "TRENDING_DOWN", "side": "SHORT"}),
    ({"direction": "with_trend"}, {"regime": "TRENDING_UP", "side": "SHORT"}),
    ({"direction": "with_trend"}, {"regime": "RANGING", "side": "SHORT"}),
    ({"direction": "counter_trend"}, {"regime": "TRENDING_UP", "side": "SHORT"}),
    ({"direction": "with_trend"},
     {"regime": "RANGING", "regime_15m": "TRENDING_DOWN", "side": "SHORT"}),
    ({"direction": "long"}, {"side": "SHORT"}),
    ({"direction": "short"}, {"side": "SHORT"}),
    ({"min_confidence": 80.0}, {"confidence": 70.0}),
    ({"min_confidence": 60.0}, {"confidence": 70.0}),
    ({"gates": []}, {}),
    ({"regimes": ["TRENDING_UP"], "sessions": ["LONDON"]}, {}),
])
def test_ops_replay_agrees_with_the_engines_own_decide(
    rule_over, row_over, monkeypatch
):
    engine_dp = _engine_module()
    row = _row(**row_over)
    rule = _rule(**rule_over)

    # The engine's master switch reads a runtime tunable backed by Firestore,
    # which no test process has. Forced on so the comparison is about the
    # rule's conditions — the only thing ops replays. `decide` refusing on
    # `master_switch` is a fact about a running engine, not about a row, and is
    # deliberately outside what `rule_unmet` reproduces (as is the daily cap).
    monkeypatch.setattr(engine_dp, "master_enabled", lambda: True)

    engine_dp.reset_for_test("/tmp/ops-contract-promotions.json")
    try:
        engine_dp.set_rule(engine_dp.PromotionRule(
            setup_class=rule["setup_class"], enabled=True,
            gates=rule["gates"], regimes=rule["regimes"],
            sessions=rule["sessions"], direction=rule["direction"],
            min_confidence=rule["min_confidence"], max_per_day=10_000,
        ))
        decision = engine_dp.decide(_EngineSig(row), row["dark_gate"])
        engine_unmet = set(decision.unmet)
    finally:
        engine_dp.reset_for_test("data/dark_promotions_v1.json")

    ops_unmet = set(dp.rule_unmet(row, rule) or [])
    assert ops_unmet == engine_unmet, (
        f"ops replay disagrees with the engine: ops={sorted(ops_unmet)} "
        f"engine={sorted(engine_unmet)}"
    )


def test_a_named_cause_for_a_missing_census_is_carried_through():
    """"Not reported" and "not reported because the engine is down" send the
    reader to different places.

    In isolated mode the engine API replaces its own zeros with an explicit
    `unavailable` rather than serving them under `source: "engine"` — otherwise
    ops reads them as *reporting, nothing refused*, a benign caption for a
    state nobody observed.
    """
    got = dp.engine_refusals(
        _snap({"source": None, "unavailable": "the engine has not published"}),
        "LIQUIDITY_SWEEP_REVERSAL",
    )
    assert got["state"] == "not_reported"
    assert got["unavailable"] == "the engine has not published"

    # An old engine sends no runtime block at all: still not_reported, and the
    # page says no cause was reported rather than inventing one.
    assert dp.engine_refusals(_snap(), "LSR")["unavailable"] is None

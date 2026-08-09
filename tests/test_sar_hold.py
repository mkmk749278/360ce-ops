"""The held-to-stop arm and stop-management panels on ``/signals/sar-live``.

**The rows in here are built by driving the real engine module**, not by hand.
``zone_distance_atr`` shipped dead for its whole life because its two tests
passed on a dict shape nothing has ever produced, and the price-action lane card
rendered NOT REPORTED because an ops fixture put the block where the reader
assumed it rather than where the engine writes it. A fixture chooses a location
and then agrees with you about it.

So :func:`_engine_rows` imports ``sar_live_shadow`` from the engine checkout when
it is present and steps real arms through it, and the contract tests are skipped
— loudly — when it is not, rather than silently falling back to a shape ops
invented.
"""

from __future__ import annotations

import os
import sys

import pytest

from app.data_sources import sar_hold

ENGINE = os.getenv("ENGINE_REPO", "/home/user/360-v2")


def _engine():
    """The real engine modules, or None when the checkout is absent."""
    if not os.path.isdir(ENGINE):
        return None
    if ENGINE not in sys.path:
        sys.path.insert(0, ENGINE)
    try:
        from src import sar_exit_strategies, sar_live_shadow, trail_mechanisms  # noqa: F401
    except Exception:
        return None
    from src import sar_exit_strategies, sar_live_shadow, trail_mechanisms
    return sar_live_shadow, sar_exit_strategies, trail_mechanisms


BAR_MS = 900_000.0


def _engine_rows():
    """Two real arms stepped by the engine: one held to its stop, one still open.

    Everything the panels read is produced by the code that will write it in
    production — the only way a cross-repo contract test is worth anything.
    """
    mods = _engine()
    if mods is None:
        return None
    live, _strat, mech = mods

    def rising(n, start=100.0, s=1.0):
        return [(start + i * s, start + i * s + 0.5, start + i * s - 0.5, start + i * s)
                for i in range(n)]

    def series(bars):
        return {
            "open": [b[0] for b in bars], "high": [b[1] for b in bars],
            "low": [b[2] for b in bars], "close": [b[3] for b in bars],
            "open_time": [1_700_000_000_000.0 + i * BAR_MS for i in range(len(bars))],
        }

    def now_at(i):
        return (1_700_000_000_000.0 + i * BAR_MS) / 1000.0 + BAR_MS / 1000.0

    base = rising(60)
    s0 = series(base)
    flip = [(159.0, 159.5, 157.0, 157.5), (157.5, 158.0, 155.0, 155.5)]
    rally = [(155.5, 160.0, 155.0, 159.5)] + [
        (159.5 + i * 3, 163.0 + i * 3, 158.5 + i * 3, 162.5 + i * 3) for i in range(8)
    ]
    crash = [(186.0, 186.5, 150.0, 152.0), (152.0, 153.0, 138.0, 139.0)]

    def build(sl, tail, sid):
        arm = live.new_arm(
            signal_id=sid, symbol="TESTUSDT", side="LONG",
            setup_class="MOVER_TREND_PULLBACK", timeframe="15m",
            entry=160.0, stop_loss=sl, tp1=999.0,
            point=mech.point(
                mech.MECH_SAR, None, s0["high"], s0["low"], s0["close"],
                len(s0["high"]) - 1, side="LONG", state={},
                params={"step": 0.02, "max_step": 0.2},
            ),
            opened_ms=s0["open_time"][-1], now_ts=1_700_000_000.0,
        )
        allb = base + tail
        live.step_arm(arm, series(allb), now_ts=now_at(len(allb)))
        return arm

    return [
        build(140.0, flip + rally + crash, "SIG-DONE"),   # held arm reached its stop
        build(100.0, flip + rally, "SIG-OPEN"),           # held arm still walking
    ]


requires_engine = pytest.mark.skipif(
    _engine() is None, reason=f"engine checkout not at {ENGINE}"
)


# --------------------------------------------------------------------------- #
# Contract: the fields ops reads are the fields the engine writes
# --------------------------------------------------------------------------- #


@requires_engine
def test_the_engine_writes_every_field_these_panels_read():
    """A field one repo reads and no repo writes fails silently and looks full.

    Pinned against the real producer rather than a fixture, because a fixture
    would agree with whatever ops assumed.
    """
    rows = _engine_rows()
    done = next(r for r in rows if r["hold_status"] == sar_hold.HOLD_SL)
    for key in (
        "hold_status", "hold_bars", "hold_pnl_pct", "hold_mfe_pct",
        "hold_mfe_incl_pct", "hold_mae_pct", "hold_mae_pre_peak_pct",
        "hold_peak_bar", "hold_ambiguous_bar", "strategies",
    ):
        assert key in done, f"engine does not write {key}"


@requires_engine
def test_the_strategy_block_is_keyed_by_rule_with_the_fields_the_table_renders():
    rows = _engine_rows()
    strategies = rows[0]["strategies"]
    assert strategies, "engine wrote no strategy block"
    for key, st in strategies.items():
        for field in ("status", "armed", "pnl_pct"):
            assert field in st, f"{key} missing {field}"


@requires_engine
def test_ops_status_constants_match_the_engine_s():
    """Two modules must agree on the strings, or every bucket silently empties."""
    live, strat, _ = _engine()
    assert sar_hold.HOLD_SL == live.HOLD_SL
    assert sar_hold.HOLD_HORIZON == live.HOLD_HORIZON
    assert sar_hold.HOLD_INSUFFICIENT == live.HOLD_INSUFFICIENT
    assert sar_hold.ST_RULE_STOP == strat.ST_RULE_STOP
    assert sar_hold.ST_ORIGINAL_SL == strat.ST_ORIGINAL_SL
    assert sar_hold.ST_OPEN == strat.ST_OPEN


@requires_engine
def test_the_panels_render_real_engine_rows():
    rows = _engine_rows()
    peak = sar_hold.reduce_peak(rows)
    assert peak["resolved"]["n"] == 1
    assert peak["resolved"]["avg_peak"] > 15.0, "the held arm saw the whole rally"
    assert peak["n_open"] == 1

    out = sar_hold.reduce_strategies(rows)
    assert out["rules"], "no rules priced from real engine rows"
    assert all(r["label"] for r in out["rules"]), "labels come from the engine"


# --------------------------------------------------------------------------- #
# Reducer behaviour — these need no engine
# --------------------------------------------------------------------------- #


def test_pre_arm_rows_are_their_own_bucket_never_unresolved():
    """A row with no hold status predates the arm and is owed nothing.

    Counting it as unresolved reports a fault that is not happening, on a
    population that ages out on its own.
    """
    buckets = sar_hold.split_rows([{"status": "CLOSED_SL"}, {"hold_status": "OPEN"}])
    assert len(buckets["pre_arm"]) == 1
    assert len(buckets["open"]) == 1
    assert buckets["insufficient"] == []


def test_horizon_rows_are_never_pooled_into_the_headline_peak():
    """A peak that has not finished is a FLOOR, not an answer.

    Pooled, a growing horizon bucket moves the headline without a single trade
    behaving differently.
    """
    rows = [
        {"hold_status": sar_hold.HOLD_SL, "hold_mfe_pct": 2.0},
        {"hold_status": sar_hold.HOLD_HORIZON, "hold_mfe_pct": 40.0},
    ]
    out = sar_hold.reduce_peak(rows)
    assert out["resolved"]["avg_peak"] == pytest.approx(2.0)
    assert out["horizon"]["avg_peak"] == pytest.approx(40.0)


def test_insufficient_rows_are_unscored_not_zero():
    rows = [
        {"hold_status": sar_hold.HOLD_SL, "hold_mfe_pct": 5.0},
        {"hold_status": sar_hold.HOLD_INSUFFICIENT, "hold_mfe_pct": 0.0},
    ]
    out = sar_hold.reduce_peak(rows)
    assert out["resolved"]["n"] == 1
    assert out["resolved"]["avg_peak"] == pytest.approx(5.0)
    assert out["n_insufficient"] == 1


def test_the_fee_is_charged_to_the_baseline_too():
    """Two identical exits must show zero edge.

    Charging the round trip to the rules and not to the baseline manufactures an
    edge out of the cost of trading.
    """
    rows = [{
        "pnl_level_pct": -2.0,
        "strategies": {"be_3": {"status": sar_hold.ST_ORIGINAL_SL, "pnl_pct": -2.0,
                                "armed": False, "label": "BE stop after +3%"}},
    }]
    out = sar_hold.reduce_strategies(rows, fee_pct=0.07)
    assert out["rules"][0]["edge"] == pytest.approx(0.0)
    assert out["baseline"]["avg_pnl"] == pytest.approx(-2.07)
    assert out["rules"][0]["avg_pnl"] == pytest.approx(-2.07)


def test_a_rule_still_walking_is_counted_apart_never_scored_as_flat():
    """Scoring an open rule as zero drags every average toward flat."""
    rows = [{
        "pnl_level_pct": -1.0,
        "strategies": {"be_3": {"status": sar_hold.ST_OPEN, "pnl_pct": None,
                                "armed": True, "label": "BE stop after +3%"}},
    }]
    out = sar_hold.reduce_strategies(rows, fee_pct=0.0)
    rule = out["rules"][0]
    assert rule["n"] == 0
    assert rule["avg_pnl"] is None
    assert rule["still_open"] == 1


def test_the_edge_is_paired_never_two_populations_differenced():
    """A rule priced on rows the baseline could not price is not a comparison."""
    rows = [
        {"pnl_level_pct": -2.0, "strategies": {
            "be_3": {"status": sar_hold.ST_RULE_STOP, "pnl_pct": 0.0, "armed": True, "label": "x"}}},
        {"pnl_level_pct": None, "strategies": {
            "be_3": {"status": sar_hold.ST_RULE_STOP, "pnl_pct": 5.0, "armed": True, "label": "x"}}},
    ]
    out = sar_hold.reduce_strategies(rows, fee_pct=0.0)
    rule = out["rules"][0]
    assert rule["n"] == 2, "both rows priced for the rule"
    assert rule["n_paired"] == 1, "only one has a baseline to pair against"
    assert rule["edge"] == pytest.approx(2.0)


def test_the_rule_order_is_the_engine_s_not_ops_s():
    """Ops keeps no catalog. A rule ops has never heard of still renders."""
    rows = [{"pnl_level_pct": -1.0, "strategies": {
        "zzz_new_rule": {"status": sar_hold.ST_ORIGINAL_SL, "pnl_pct": -1.0,
                         "armed": False, "label": "Something ops never heard of"},
    }}]
    out = sar_hold.reduce_strategies(rows, fee_pct=0.0)
    assert [r["key"] for r in out["rules"]] == ["zzz_new_rule"]
    assert out["rules"][0]["label"] == "Something ops never heard of"


def test_coverage_grades_the_excluded_rows_rather_than_only_counting_them():
    """A coverage count cannot say which way the missing rows lean."""
    rows = [
        {"hold_status": sar_hold.HOLD_SL, "hold_mfe_pct": 1.0, "pnl_level_pct": 1.0},
        {"hold_status": None, "pnl_level_pct": -5.0},
    ]
    out = sar_hold.reduce_peak(rows)
    cov = out["coverage"]
    assert cov["n_scored"] == 1 and cov["n_excluded"] == 1
    assert cov["scored_sar_pnl"] == pytest.approx(1.0)
    assert cov["excluded_sar_pnl"] == pytest.approx(-5.0)


# --------------------------------------------------------------------------- #
# The label seam — found by rendering the page, not by a test
# --------------------------------------------------------------------------- #


def test_labels_come_from_the_ledger_manifest_not_from_ops():
    """Rules rendered as `be_3` until the manifest was wired through.

    The engine's per-row state carries the rule KEY and no label — labels live in
    the ledger's ``strategy_catalog``, written once per file. The first cut of
    this page read ``st["label"]``, which no row has ever carried, so the table
    rendered raw keys at the reader. Nothing crashed and every test passed,
    because none of them asserted the label: the defect was only visible on the
    rendered page.
    """
    rows = [{"pnl_level_pct": -1.0, "strategies": {
        "be_3": {"status": sar_hold.ST_ORIGINAL_SL, "pnl_pct": -1.0, "armed": False},
    }}]
    catalog = [{"key": "be_3", "label": "BE stop after +3%", "trigger_pct": 3.0}]
    out = sar_hold.reduce_strategies(rows, fee_pct=0.0, catalog=catalog)
    assert out["rules"][0]["label"] == "BE stop after +3%"
    assert out["rules"][0]["trigger_pct"] == 3.0
    assert out["rules"][0]["described"] is True


def test_a_rule_the_manifest_does_not_describe_is_badged_never_renamed():
    """Ops must not invent a label, and must not drop the row."""
    rows = [{"pnl_level_pct": -1.0, "strategies": {
        "future_rule": {"status": sar_hold.ST_ORIGINAL_SL, "pnl_pct": -1.0, "armed": False},
    }}]
    out = sar_hold.reduce_strategies(rows, fee_pct=0.0, catalog=[])
    rule = out["rules"][0]
    assert rule["label"] == "future_rule", "renders under its raw key"
    assert rule["described"] is False, "and says the manifest did not describe it"


@requires_engine
def test_the_engine_ships_a_manifest_describing_every_rule_it_stamps():
    """The cross-repo contract, driven from the real producer on both ends.

    A manifest that describes only some of the rules an arm carries would leave
    the rest rendering as raw keys — the defect above, half-fixed.
    """
    live, strat, _ = _engine()
    manifest = strat.catalog_manifest()
    described = {m["key"] for m in manifest}
    stamped = set(_engine_rows()[0]["strategies"])
    assert stamped <= described, f"stamped but undescribed: {stamped - described}"
    assert all(m.get("label") for m in manifest)


@requires_engine
def test_the_flushed_ledger_actually_carries_the_manifest(tmp_path):
    """Defining a manifest is not writing one — pin the serializer.

    A field one repo writes and no repo reads is #817; a field the producer
    defines and never serializes is the same defect one layer down, and it is
    invisible while the process lives.
    """
    import json

    live, strat, _ = _engine()
    path = tmp_path / "arms.json"
    ledger = live.SarLiveLedger(path=str(path))
    for arm in _engine_rows():
        ledger.add(arm)
    assert ledger.flush(force=True) is True
    payload = json.loads(path.read_text())
    assert payload.get("strategy_catalog"), "the ledger did not write the manifest"
    assert {m["key"] for m in payload["strategy_catalog"]} == set(strat.CATALOG_ORDER)
    assert payload.get("schema") == live.LEDGER_SCHEMA

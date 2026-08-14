"""The live SAR mechanism panel — reducers, and the cross-repo contract.

**The fixture is real engine output.** ``tests/fixtures_sar_live_arms.json`` was
produced by running the engine's ``src/sar_live_shadow.py`` (``new_arm`` /
``step_arm`` / ``SarLiveLedger.flush``) against synthetic candles and saving what
it wrote. Nothing here hand-writes a row shape.

That is the #798 rule: a mock whose keys the author chose cannot verify a
contract the author got wrong — it asserts the assumption back and goes green
over dead code. It is also the #817 rule, from the other side: ops read
``entry_regime`` off closed-signal records for months while the engine never
wrote it, and the page looked full the whole time. The engine pins these keys in
``tests/test_sar_live_shadow.py::test_arm_rows_carry_every_field_ops_reads``;
this file pins that ops actually reads them.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test")

from app.routes.sar_live import (  # noqa: E402
    ANCHOR_CLEAN,
    ANCHOR_REPLAYED,
    ANCHOR_SUSPECT,
    ANCHOR_UNVERIFIED,
    GOV_GEOMETRY,
    GOV_SAR,
    LIVE_STALE_SEC,
    STATUS_INSUFFICIENT,
    STATUS_RUNNING,
    count_anchor_verdicts,
    filter_arms,
    mark_anchor_integrity,
    mark_distance_to_stop,
    mark_risk_adjusted_r,
    reduce_arms,
    reduce_live_state,
    summarize_by_risk,
    summarize_by_timeframe,
    summarize_resolved,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures_sar_live_arms.json").read_text()
)


def _rows():
    return reduce_arms(json.loads(json.dumps(FIXTURE)))


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #


def test_reduce_splits_open_from_resolved():
    live, resolved = _rows()
    assert len(live) == 3
    assert len(resolved) == 4
    assert all(r["status"] == STATUS_RUNNING for r in live)
    assert all(r["status"] != STATUS_RUNNING for r in resolved)


def test_reduce_survives_a_missing_or_broken_file():
    assert reduce_arms({"error": "missing: /engine-data/sar_live_arms_v1.json"}) == ([], [])
    assert reduce_arms(None) == ([], [])
    assert reduce_arms({"open": "not-a-list", "resolved": None}) == ([], [])


def test_reduce_does_not_truncate():
    """#97: truncate after filtering, never inside a reducer."""
    payload = {"open": [dict(FIXTURE["open"][0]) for _ in range(900)], "resolved": []}
    live, _ = reduce_arms(payload)
    assert len(live) == 900


# --------------------------------------------------------------------------- #
# The mechanism, as the engine actually wrote it
# --------------------------------------------------------------------------- #


def test_aligned_entries_are_governed_by_sar_opposed_ones_are_not():
    live, _ = _rows()
    by_symbol = {r["symbol"]: r for r in live}
    assert by_symbol["RUNUSDT"]["aligned_at_entry"] is True
    assert by_symbol["RUNUSDT"]["governor"] == GOV_SAR
    assert by_symbol["GEOUSDT"]["aligned_at_entry"] is False
    assert by_symbol["GEOUSDT"]["governor"] == GOV_GEOMETRY


def test_both_timeframes_run_as_independent_arms_on_one_signal():
    live, _ = _rows()
    run_arms = [r for r in live if r["signal_id"] == "SIG-RUN"]
    assert {r["timeframe"] for r in run_arms} == {"5m", "15m"}
    assert len({r["arm_id"] for r in run_arms}) == 2


def test_resolved_rows_carry_both_fills():
    _live, resolved = _rows()
    flip = next(r for r in resolved if r["exit_reason"] == "sar_flip")
    assert flip["fill_level"] is not None
    assert flip["fill_confirm"] is not None
    assert flip["fill_confirm"] != flip["fill_level"]
    # Waiting for the close cost something on this row, and the engine says so
    # rather than the panel inferring it.
    assert flip["confirm_slippage_pct"] == pytest.approx(
        flip["pnl_confirm_pct"] - flip["pnl_level_pct"]
    )


def test_every_exit_route_the_mechanism_allows_is_present():
    _live, resolved = _rows()
    assert {r["exit_reason"] for r in resolved if r["exit_reason"]} >= {
        "sar_flip", "static_sl", "static_tp1"
    }


# --------------------------------------------------------------------------- #
# Filters — each count measured with every filter except its own
# --------------------------------------------------------------------------- #


def test_filters_are_independent_so_a_selector_can_omit_itself():
    live, _ = _rows()
    assert len(filter_arms(live, timeframe="15m")) == 2
    assert len(filter_arms(live, timeframe="5m")) == 1
    assert len(filter_arms(live, governor=GOV_SAR)) == 2
    assert len(filter_arms(live, alignment="agreed")) == 2
    assert len(filter_arms(live, alignment="opposed")) == 1
    # Combined, and order-independent.
    assert filter_arms(live, timeframe="15m", governor=GOV_SAR) == filter_arms(
        filter_arms(live, timeframe="15m"), governor=GOV_SAR
    )


def test_alignment_unknown_selects_the_arms_that_could_not_read_sar():
    _live, resolved = _rows()
    unknown = filter_arms(resolved, alignment="unknown")
    assert [r["symbol"] for r in unknown] == ["NONEUSDT"]
    assert unknown[0]["status"] == STATUS_INSUFFICIENT


# --------------------------------------------------------------------------- #
# Summary — refuses rather than averaging over blanks
# --------------------------------------------------------------------------- #


def test_insufficient_arms_are_excluded_from_rates_not_scored_as_losses():
    _live, resolved = _rows()
    s = summarize_resolved(resolved)
    assert s["n"] == 4
    assert s["insufficient"] == 1
    assert s["measurable"] == 3
    # The INSUFFICIENT row must not drag the averages toward zero.
    r_values = [r["r_level"] for r in resolved if r.get("r_level") is not None]
    assert s["avg_r_level"] == pytest.approx(sum(r_values) / len(r_values))


def test_summary_reports_both_fills_and_never_picks_one():
    _live, resolved = _rows()
    s = summarize_resolved(resolved)
    assert s["avg_r_level"] is not None
    assert s["avg_r_confirm"] is not None
    assert s["avg_confirm_slippage_pct"] is not None
    assert "avg_r" not in s, "a single blended R would be choosing the answer"


def test_rows_without_an_entry_stop_are_counted_but_excluded_from_R():
    """Refuse, don't clamp — scoring them 0R makes missing data read as mediocre.

    Since 2026-08-02 the win rate is counted on the **money** rather than on R,
    so a row with no entry-risk stamp is excluded from the R columns and still
    contributes to the win rate and the PnL average. That is the point of
    demoting R: a percentage needs no denominator, so it cannot silently shrink
    its own population the way R does.
    """
    _live, resolved = _rows()
    rows = [dict(r) for r in resolved]
    for r in rows:
        if r["status"] != STATUS_INSUFFICIENT:
            r["r_level"] = None
            r["r_confirm"] = None
    s = summarize_resolved(rows)
    assert s["measurable"] == 3
    assert s["n_r"] == 0
    assert s["no_r"] == 3
    assert s["avg_r_level"] is None
    # ...but the money still reports, on the full measurable population.
    assert s["n_pnl"] == 3
    assert s["avg_pnl_level_pct"] is not None
    assert s["win_rate_level"] is not None


def test_pnl_is_the_primary_measure_and_can_disagree_with_R():
    """Owner, 2026-08-02: *"that R is purely confusing — 3% SL still only 1R"*.

    Position sizing is a fixed $500 notional (`raw_qty = notional / entry_price`),
    so the stop distance is absent from the sizing formula. R divides each
    outcome by its own stop, which only equalises trades when size scales
    inversely to that stop — it does not. Two trades can therefore share a
    −1.00R and cost wildly different money, and the aggregate sign can differ
    between the two units.

    This pins that the panel reports the money, using a book where R is positive
    and PnL is negative: a small win on a tight stop and a bigger loss on a wide
    one.
    """
    rows = [
        # +1.0% on a 1% stop  -> +1.00R
        {"status": "CLOSED_SAR_FLIP", "anchor_verdict": "clean",
         "sl_distance_pct": 1.0, "r_level": 1.0, "r_confirm": 1.0,
         "pnl_level_pct": 1.0, "pnl_confirm_pct": 1.0},
        # -3.0% on a 6% stop  -> -0.50R
        {"status": "CLOSED_SAR_FLIP", "anchor_verdict": "clean",
         "sl_distance_pct": 6.0, "r_level": -0.5, "r_confirm": -0.5,
         "pnl_level_pct": -3.0, "pnl_confirm_pct": -3.0},
    ]
    s = summarize_resolved(rows)

    assert s["avg_r_level"] == pytest.approx(0.25), "R says this book made money"
    assert s["avg_pnl_level_pct"] == pytest.approx(-1.0), "the money says it lost"
    # The two units disagree on the SIGN, which is why the page leads with PnL.
    assert s["avg_r_level"] > 0 > s["avg_pnl_level_pct"]
    assert s["total_pnl_level_pct"] == pytest.approx(-2.0)


def test_handovers_are_counted_because_only_they_test_the_exit():
    _live, resolved = _rows()
    s = summarize_resolved(resolved)
    # The two arms that never handed over closed on the original geometry and
    # say nothing about SAR as an exit.
    assert s["handovers"] == 1
    assert s["by_exit"]["sar_flip"] == 1


def test_timeframes_are_reported_separately_never_pooled():
    _live, resolved = _rows()
    per_tf = summarize_by_timeframe(resolved)
    assert {t["timeframe"] for t in per_tf} == {"5m", "15m"}
    assert sum(t["measurable"] for t in per_tf) == summarize_resolved(resolved)["measurable"]


# --------------------------------------------------------------------------- #
# Marks
# --------------------------------------------------------------------------- #


def test_distance_to_stop_is_written_only_while_sar_governs():
    live, _ = _rows()
    mark_distance_to_stop(live, {"RUNUSDT": 175.0, "GEOUSDT": 158.0})
    by_symbol = {r["symbol"]: r for r in live}
    assert by_symbol["RUNUSDT"]["stop_distance_pct"] is not None
    # The geometry leg's stop is the original SL; distance-to-SAR is meaningless
    # before the handover, so the column stays blank rather than showing a
    # number that means something else. Present-but-None, not absent: "we could
    # not compute it" is a defined state and the template should not have to
    # tell a missing key from an unknown value.
    assert "stop_distance_pct" in by_symbol["GEOUSDT"]
    assert by_symbol["GEOUSDT"]["stop_distance_pct"] is None
    assert by_symbol["GEOUSDT"]["unrealized_pct"] is not None


def test_marks_never_write_a_realized_column():
    live, _ = _rows()
    mark_distance_to_stop(live, {"RUNUSDT": 175.0, "GEOUSDT": 158.0})
    for r in live:
        assert r["pnl_level_pct"] is None
        assert r["r_level"] is None
        assert r["fill_level"] is None


def test_a_missing_price_blanks_the_column_and_keeps_the_row():
    live, _ = _rows()
    before = len(live)
    mark_distance_to_stop(live, {})
    assert len(live) == before
    assert all(r.get("stop_distance_pct") is None for r in live)


def test_mark_never_overwrites_the_engines_stop():
    live, _ = _rows()
    stops = {r["arm_id"]: r["sar_stop"] for r in live}
    mark_distance_to_stop(live, {"RUNUSDT": 175.0, "GEOUSDT": 158.0})
    assert {r["arm_id"]: r["sar_stop"] for r in live} == stops


# --------------------------------------------------------------------------- #
# Freshness — a live price feed is not evidence the measurement is running
# --------------------------------------------------------------------------- #


def test_a_frozen_arm_file_is_reported_as_frozen_not_live():
    live, _ = _rows()
    state = reduce_live_state(
        {"exists": True, "file": "sar_live_arms_v1.json", "age_sec": LIVE_STALE_SEC + 60},
        live,
    )
    assert state["state"] == "frozen"
    # The copy must name the heartbeat, because that is what makes a stale file
    # mean "the loop stopped" rather than "no bar closed recently".
    assert "on a heartbeat" in state["detail"]
    assert "not that the market is quiet" in state["detail"]


def test_a_current_file_with_open_arms_is_live():
    live, _ = _rows()
    state = reduce_live_state(
        {"exists": True, "file": "sar_live_arms_v1.json", "age_sec": 8.0}, live
    )
    assert state["state"] == "live"


def test_no_open_signals_is_idle_not_a_fault():
    state = reduce_live_state(
        {"exists": True, "file": "sar_live_arms_v1.json", "age_sec": 8.0}, []
    )
    assert state["state"] == "idle"
    assert "not a fault" in state["detail"]


def test_a_missing_file_is_distinguished_from_an_empty_one():
    state = reduce_live_state({"exists": False, "file": "sar_live_arms_v1.json"}, [])
    assert state["state"] == "unavailable"


def test_an_orphaned_version_is_loud():
    """#822: ops read an abandoned ledger for nine hours and every number
    described a population the engine had already discarded."""
    state = reduce_live_state(
        {"exists": True, "file": "sar_live_arms_v1.json", "age_sec": 5.0,
         "newer_version": 2, "newer_file": "sar_live_arms_v2.json"},
        [],
    )
    assert state["state"] == "orphan"
    assert "sar_live_arms_v2.json" in state["detail"]


# --------------------------------------------------------------------------- #
# Cross-repo contract
# --------------------------------------------------------------------------- #

#: Mirrors the engine's OPS_CONTRACT_KEYS. If the engine renames one of these,
#: the fixture regenerates without it and this fails — loudly, instead of
#: quietly emptying a column.
READ_BY_THIS_PAGE = frozenset({
    "arm_id", "signal_id", "symbol", "side", "setup_class", "timeframe",
    "entry", "stop_loss", "tp1", "sl_distance_pct",
    "opened_at", "bars_seen", "aligned_at_entry", "governor", "handover_at",
    "sar_stop", "status", "exit_reason", "closed_at",
    "fill_level", "fill_confirm", "pnl_level_pct", "pnl_confirm_pct",
    "r_level", "r_confirm", "confirm_slippage_pct", "mfe_pct", "ambiguous_bar",
    "sar_risk_pct", "max_sar_risk_pct", "handover_risk_pct",
    "handover_wider_than_sl",
})


def test_every_field_this_page_reads_is_present_in_real_engine_output():
    live, resolved = _rows()
    for row in live + resolved:
        missing = READ_BY_THIS_PAGE - set(row)
        assert not missing, f"engine stopped writing {missing} — this page reads them"


def test_ops_reads_the_filename_the_engine_writes():
    from app.data_sources.data_volume import SAR_LIVE_FILE

    assert SAR_LIVE_FILE == "sar_live_arms_v1.json"


def test_fixture_really_came_from_the_engine_not_from_this_file():
    """The engine stamps its own schema into the payload it writes."""
    assert FIXTURE["schema"] == 1
    assert FIXTURE["written_at"] > 0
    assert all(r.get("schema") == 1 for r in FIXTURE["open"])


# --------------------------------------------------------------------------- #
# The page must actually render — a Jinja error here is a 500 in production
# --------------------------------------------------------------------------- #


@contextmanager
def _client(payload=None, provenance=None, prices=None, raise_prices=False):
    """The real app, with only the two I/O methods this page depends on swapped.

    The real ``DataVolumeReader`` and the real klines client stay in place —
    the lifespan wires and closes them, and substituting whole objects means
    re-implementing whatever startup and shutdown happen to touch. Only the
    calls that would hit disk or the network are replaced, so the seam under
    test is the one the page actually uses.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    _prov = provenance if provenance is not None else {
        "file": "sar_live_arms_v1.json", "version": 1, "exists": True,
        "modified_at": "2026-07-30 05:00 UTC", "age_sec": 7.0,
        "newer_version": None, "newer_file": None,
    }
    _prices = prices if prices is not None else {"RUNUSDT": 175.0, "GEOUSDT": 158.0}

    async def _fetch_all_prices():
        if raise_prices:
            raise RuntimeError("binance down")
        return _prices

    with TestClient(app) as client:
        vol, klines = app.state.data_volume, app.state.binance_klines
        # Swap the LANE accessor, not the SAR-named convenience wrapper.
        # ``/signals/sar-live`` and ``/signals/atr-live`` are one handler over
        # four (mechanism, lane) files, so a fixture bound to the old spelling
        # would render an empty page while every assertion below still ran —
        # exactly the seam this repo keeps paying for.
        vol_arms = vol.trail_arms
        vol_prov = vol.trail_arms_provenance
        klines_fetch = klines.fetch_all_prices
        vol.trail_arms = lambda mechanism, dark=False: (
            payload if payload is not None else FIXTURE
        )
        vol.trail_arms_provenance = lambda mechanism, dark=False: _prov
        klines.fetch_all_prices = _fetch_all_prices
        try:
            client.post("/login", data={"password": "test-token"})
            yield client
        finally:
            vol.trail_arms = vol_arms
            vol.trail_arms_provenance = vol_prov
            klines.fetch_all_prices = klines_fetch


def test_live_tab_renders():
    with _client() as client:
        r = client.get("/signals/sar-live")
    assert r.status_code == 200
    assert "RUNUSDT" in r.text
    # The live tab must not print realized columns for open arms.
    assert "Dist. to stop" in r.text
    assert "Fill @level" not in r.text


def test_resolved_tab_renders_the_verdict():
    with _client() as client:
        r = client.get("/signals/sar-live?tab=resolved")
    assert r.status_code == 200
    assert "Fill @level" in r.text
    assert "Fill @confirm" in r.text
    # Copy is part of the measurement: the page must say the figures are gross.
    assert "gross" in r.text


def test_page_states_it_changes_no_exit():
    with _client() as client:
        r = client.get("/signals/sar-live")
    assert "place no orders and change no exit" in r.text


def test_frozen_file_says_so_on_the_page():
    with _client(provenance={
        "file": "sar_live_arms_v1.json", "version": 1, "exists": True,
        "modified_at": "2026-07-30 01:00 UTC", "age_sec": 4000.0,
        "newer_version": None, "newer_file": None,
    }) as client:
        r = client.get("/signals/sar-live")
    assert r.status_code == 200
    assert "FROZEN" in r.text


def test_missing_file_renders_instead_of_500():
    with _client(
        payload={"error": "missing: /engine-data/sar_live_arms_v1.json"},
        provenance={"file": "sar_live_arms_v1.json", "version": 1, "exists": False,
                    "modified_at": None, "age_sec": None,
                    "newer_version": None, "newer_file": None},
    ) as client:
        r = client.get("/signals/sar-live")
    assert r.status_code == 200
    assert "UNAVAILABLE" in r.text


def test_a_binance_outage_blanks_the_column_not_the_page():
    with _client(raise_prices=True) as client:
        r = client.get("/signals/sar-live")
    assert r.status_code == 200
    assert "RUNUSDT" in r.text


def test_csv_export_is_uncapped_and_honours_the_filter():
    with _client() as client:
        r = client.get("/signals/sar-live/export.csv?tab=resolved&timeframe=15m")
    assert r.status_code == 200
    body = r.text
    assert "fill_level" in body and "confirm_slippage_pct" in body
    assert "FLIPUSDT" in body
    assert "SLUSDT" not in body        # 5m row, filtered out


def test_table_cap_is_applied_after_filtering_and_declared():
    """#97: a row cap is a render bound, and the page says when it bit."""
    from app.routes.sar_live import TABLE_ROW_CAP

    many = {"open": [dict(FIXTURE["open"][0]) for _ in range(TABLE_ROW_CAP + 50)],
            "resolved": []}
    with _client(payload=many) as client:
        r = client.get("/signals/sar-live")
    assert r.status_code == 200
    assert f"capped at {TABLE_ROW_CAP} rows" in r.text
    assert f"of {TABLE_ROW_CAP + 50}" in r.text


def test_unavailable_copy_says_how_long_to_wait_before_it_is_a_fault():
    """The engine heartbeats every 60s even with no arms, so 'missing' is only a
    fault past that. Copy that names a cause must be true of the case it names."""
    state = reduce_live_state({"exists": False, "file": "sar_live_arms_v1.json"}, [])
    assert state["state"] == "unavailable"
    assert "heartbeat" in state["detail"]
    assert "no signals open" in state["detail"]


# --------------------------------------------------------------------------- #
# Risk split — "timed badly" and "risked more" are different findings
# --------------------------------------------------------------------------- #


def test_arms_carry_the_risk_their_sar_stop_actually_took():
    live, _ = _rows()
    by_key = {(r["symbol"], r["timeframe"]): r for r in live}
    wide = by_key[("RUNUSDT", "15m")]
    tight = by_key[("RUNUSDT", "5m")]
    # Same signal, same entry, opposite verdicts — which is the whole point of
    # running 5m and 15m as independent arms.
    assert wide["handover_wider_than_sl"] is True
    assert tight["handover_wider_than_sl"] is False
    assert wide["handover_risk_pct"] > wide["sl_distance_pct"]
    assert tight["handover_risk_pct"] < tight["sl_distance_pct"]


def test_the_verdict_splits_on_whether_the_stop_exceeded_the_designed_sl():
    _live, resolved = _rows()
    split = summarize_by_risk(resolved)
    assert set(split) == {"wider", "inside", "unknown"}
    # Every resolved row lands in exactly one bucket — no double count, no drop.
    assert sum(split[k]["n"] for k in split) == len(resolved)


def test_never_handed_over_is_its_own_bucket_not_folded_into_either():
    """An arm with no SAR risk to compare is not the same as one with a narrow
    stop — folding it in would make 'inside' describe two different things."""
    _live, resolved = _rows()
    split = summarize_by_risk(resolved)
    never = [r for r in resolved if r.get("handover_wider_than_sl") is None]
    assert split["unknown"]["n"] == len(never)
    assert split["unknown"]["avg_risk_pct"] is None


def test_each_bucket_reports_the_risk_it_actually_took():
    _live, resolved = _rows()
    split = summarize_by_risk(resolved)
    for key in ("wider", "inside"):
        rows = [
            r for r in resolved
            if r.get("handover_wider_than_sl") is (key == "wider")
            and r.get("handover_risk_pct") is not None
        ]
        if not rows:
            continue
        expected = sum(r["handover_risk_pct"] for r in rows) / len(rows)
        assert split[key]["avg_risk_pct"] == pytest.approx(expected)


def test_live_tab_renders_the_sar_risk_column_and_flags_the_wide_arm():
    with _client() as client:
        r = client.get("/signals/sar-live")
    assert r.status_code == 200
    assert "SAR risk" in r.text
    assert ">wider<" in r.text          # the 15m RUNUSDT arm is badged


def test_resolved_tab_explains_why_the_risk_split_exists():
    with _client() as client:
        r = client.get("/signals/sar-live?tab=resolved")
    assert r.status_code == 200
    assert "sized for" in r.text
    # Copy is part of the measurement: the page must say this changes nothing.
    assert "the mechanism is unchanged" in r.text


def test_csv_export_carries_the_risk_stamps():
    with _client() as client:
        r = client.get("/signals/sar-live/export.csv")
    assert "handover_wider_than_sl" in r.text
    assert "max_sar_risk_pct" in r.text


def test_an_arm_written_before_the_risk_stamps_still_renders():
    """Cross-repo ordering: arms persisted by the previous engine build have no
    risk keys and stay in the ledger until they resolve. The tab must render
    them with the column blank, not 500 on a missing key."""
    legacy = json.loads(json.dumps(FIXTURE))
    for row in legacy["open"] + legacy["resolved"]:
        for key in ("sar_risk_pct", "max_sar_risk_pct",
                    "handover_risk_pct", "handover_wider_than_sl"):
            row.pop(key, None)
    with _client(payload=legacy) as client:
        r = client.get("/signals/sar-live")
        assert r.status_code == 200
        assert "RUNUSDT" in r.text
        assert ">wider<" not in r.text        # nothing to flag, nothing invented
        rr = client.get("/signals/sar-live?tab=resolved")
    assert rr.status_code == 200


def test_the_risk_split_treats_a_legacy_arm_as_unknown_not_inside():
    """No stamp is 'we do not know', which is not the same as 'it was narrow'."""
    _live, resolved = _rows()
    legacy = [{k: v for k, v in r.items() if k != "handover_wider_than_sl"}
              for r in resolved]
    split = summarize_by_risk(legacy)
    assert split["unknown"]["n"] == len(legacy)
    assert split["inside"]["n"] == 0
    assert split["wider"]["n"] == 0




# --------------------------------------------------------------------------- #
# Freshness of the measurement, not of the price beside it (#108)
# --------------------------------------------------------------------------- #
#
# The owner's 2026-07-30 page read "LIVE — 3 arms running, stepped inside the
# monitor loop" over two KORUUSDT SHORT arms that had consumed zero bars in
# 2h19m, with a parked 5m stop the price had already crossed by 5.45%. Every
# number on the row was the engine's, the price was real, and the page was wrong:
# it graded liveness on the FILE's age and then supplied the live price itself.
#
# This module's own docstring already carried the rule — *a working price feed is
# not evidence the measurement is running*. Here the price feed was ours.
#
# FRESHNESS_FIXTURE is real engine output, generated by the engine repo's
# scripts/gen_ops_sar_live_fixture.py: two stalled KORUUSDT arms (0 bars, 3.3 and
# 10 bar-widths behind) beside one advancing SLXUSDT arm (3 bars, current). None
# of the freshness values here were typed by hand — the engine computed them.

FRESHNESS_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures_sar_live_freshness.json").read_text()
)
FRESHNESS_PRICES = {"KORUUSDT": 12.47, "SLXUSDT": 0.0855}


def _fresh_rows():
    return reduce_arms(json.loads(json.dumps(FRESHNESS_FIXTURE)))


def _fresh_state(rows):
    return reduce_live_state(
        {"exists": True, "age_sec": 18.0, "newer_version": None}, rows
    )


def test_the_engine_stamps_the_freshness_this_page_reads():
    """The cross-repo contract, from the consuming side (#817 in reverse)."""
    live, _ = _fresh_rows()
    for row in live:
        for key in ("last_advance_at", "last_swept_at", "bars_behind",
                    "stalled", "stall_reason", "series_bar_ms"):
            assert key in row, f"engine stopped writing {key}"


def test_a_stalled_arm_is_not_reported_as_advancing():
    from app.routes.sar_live import count_live_freshness

    live, _ = _fresh_rows()
    counts = count_live_freshness(live)
    assert counts == {"running": 3, "stalled": 2, "stepping": 1, "no_bars_yet": 2}
    state = _fresh_state(live)
    assert state["state"] == "partial"
    assert "1 of 3 arms are advancing" in state["detail"]
    assert "2 are stalled" in state["detail"]


def test_an_all_stalled_book_says_so_and_drops_the_old_claim():
    live, _ = _fresh_rows()
    stalled_only = [r for r in live if r["stalled"] is True]
    state = _fresh_state(stalled_only)
    assert state["state"] == "stalled"
    assert "all 2 running arms" in state["detail"]
    # The sentence the owner was shown over frozen arms must be gone.
    assert "stepped inside the monitor loop" not in state["detail"]


def test_a_healthy_book_still_reads_live():
    live, _ = _fresh_rows()
    advancing = [r for r in live if r["stalled"] is False]
    state = _fresh_state(advancing)
    assert state["state"] == "live"
    assert "advancing" in state["detail"]


def test_freshness_is_the_arms_own_clock_not_the_files():
    """Three clocks — file write, price fetch, last bar consumed — and only the
    third says the stop beside it is current."""
    from app.routes.sar_live import mark_freshness

    live, _ = _fresh_rows()
    koru = next(r for r in live if r["symbol"] == "KORUUSDT" and r["timeframe"] == "15m")
    mark_freshness(live, now=koru["last_swept_at"])
    # The engine swept it 50 minutes after it last advanced: the file was being
    # written the whole time and the arm had not moved.
    assert koru["advance_age_sec"] == pytest.approx(3000.0)
    assert koru["is_stalled"] is True
    slx = next(r for r in live if r["symbol"] == "SLXUSDT")
    assert slx["is_stalled"] is False


def test_an_arm_with_no_freshness_stamp_reads_unknown_not_fresh():
    """Arms persisted before the engine stamped freshness must not render as
    'just advanced' — that is exactly the population that had the bug."""
    from app.routes.sar_live import mark_freshness

    payload = json.loads(json.dumps(FRESHNESS_FIXTURE))
    for row in payload["open"]:
        for key in ("last_advance_at", "bars_behind", "stalled", "stall_reason"):
            row.pop(key, None)
    live, _ = reduce_arms(payload)
    mark_freshness(live, now=1_800_000_000.0)
    assert all(r["advance_age_sec"] is None for r in live)


def test_a_crossed_stop_on_an_open_arm_is_flagged_not_printed_as_a_number():
    """KORUUSDT: price 12.47 against parked SHORT stops of 11.86 and 11.66. The
    mechanism would have exited bars ago and the arm is still open. A bare
    '-5.1%' in a row of percentages is not a report of that."""
    live, _ = _fresh_rows()
    mark_distance_to_stop(live, FRESHNESS_PRICES)
    koru = [r for r in live if r["symbol"] == "KORUUSDT"]
    assert len(koru) == 2
    assert all(r["stop_distance_pct"] < 0 for r in koru)
    assert all(r["stop_crossed"] is True for r in koru)


def test_a_stop_price_has_not_reached_is_not_flagged_as_crossed():
    live, _ = _fresh_rows()
    mark_distance_to_stop(live, FRESHNESS_PRICES)
    slx = next(r for r in live if r["symbol"] == "SLXUSDT")
    assert slx["stop_distance_pct"] > 0
    assert slx["stop_crossed"] is False


def test_the_live_table_badges_the_stalled_rows_and_the_crossed_level():
    with _client(payload=FRESHNESS_FIXTURE, prices=FRESHNESS_PRICES) as client:
        r = client.get("/signals/sar-live")
    assert r.status_code == 200
    assert "PARTLY STALLED" in r.text
    assert ">stalled" in r.text
    assert ">crossed<" in r.text
    # And the page names the column to read before any stop-derived number.
    assert "Last advance" in r.text


def test_the_page_says_how_many_arms_have_never_advanced():
    with _client(payload=FRESHNESS_FIXTURE, prices=FRESHNESS_PRICES) as client:
        r = client.get("/signals/sar-live")
    assert "consumed <strong>zero</strong> bars" in r.text


def test_the_csv_export_carries_the_freshness_columns():
    """An export is a surface too — the owner's export had no column that could
    have shown a 2h19m-old stop, so it read as healthy as the page did."""
    with _client(payload=FRESHNESS_FIXTURE, prices=FRESHNESS_PRICES) as client:
        r = client.get("/signals/sar-live/export.csv")
    assert r.status_code == 200
    header = r.text.splitlines()[0]
    for col in ("last_advance_at", "advance_age_sec", "bars_behind",
                "stalled", "is_stalled", "stop_crossed"):
        assert col in header


# --------------------------------------------------------------------------- #
# Anchor integrity — an arm can be born a replay (#836)
# --------------------------------------------------------------------------- #
#
# Rows here are **real engine output with the one field under test changed**.
# Nothing invents a row shape: the keys, types and neighbours all come from the
# fixture the engine wrote, and only the value the check reads is moved.


def _resolved_row():
    _live, resolved = _rows()
    return next(
        dict(r) for r in resolved
        if r["status"] != STATUS_INSUFFICIENT and r.get("r_level") is not None
    )


def test_an_engine_stamped_replay_is_named_and_excluded_from_every_R():
    """ACHUSDT 15m: 158 bars consumed in ten bars of life, published as a live
    fill. Its R describes a replay and must not reach the verdict."""
    clean, replayed = _resolved_row(), _resolved_row()
    replayed["arm_id"] = "REPLAYED:15m"
    replayed["first_step_bars"] = 158
    replayed["anchor_bars_behind"] = 0.0
    clean["first_step_bars"] = 1
    clean["anchor_bars_behind"] = 0.0

    rows = mark_anchor_integrity([clean, replayed], now=1_700_200_000.0)
    assert rows[0]["anchor_verdict"] == ANCHOR_CLEAN
    assert rows[1]["anchor_verdict"] == ANCHOR_REPLAYED
    assert rows[1]["anchor_engine_stamped"] is True

    s = summarize_resolved(rows)
    assert s["n"] == 2 and s["replayed"] == 1
    assert s["measurable"] == 1
    assert s["avg_r_level"] == pytest.approx(clean["r_level"])


def test_a_stale_anchor_is_a_replay_even_before_the_arm_advances():
    """The harm starts at the anchor, not at the first step: SAR-at-entry was
    already read off the stale bar."""
    row = _resolved_row()
    row["first_step_bars"] = None
    row["anchor_bars_behind"] = 158.0
    mark_anchor_integrity([row], now=1_700_200_000.0)
    assert row["anchor_verdict"] == ANCHOR_REPLAYED
    assert row["anchor_replay_bars"] == pytest.approx(158.0)


def test_a_legacy_row_that_out_ran_its_own_lifetime_is_suspect():
    """Rows written before the engine stamped anything are exactly the rows
    that have the bug, so a missing stamp must not read as a pass."""
    row = _resolved_row()
    row.pop("first_step_bars", None)
    row.pop("anchor_bars_behind", None)
    row["timeframe"] = "15m"
    row["opened_at"] = 1_700_000_000.0
    row["closed_at"] = 1_700_000_000.0 + 10 * 900.0   # ten 15m bars of life
    row["bars_seen"] = 158
    mark_anchor_integrity([row], now=1_700_200_000.0)
    assert row["anchor_verdict"] == ANCHOR_SUSPECT
    assert row["anchor_engine_stamped"] is False
    assert summarize_resolved([row])["replayed"] == 1


def test_a_legacy_row_within_its_lifetime_passes_but_says_who_checked_it():
    row = _resolved_row()
    row.pop("first_step_bars", None)
    row.pop("anchor_bars_behind", None)
    row["timeframe"] = "15m"
    row["opened_at"] = 1_700_000_000.0
    row["closed_at"] = 1_700_000_000.0 + 10 * 900.0
    row["bars_seen"] = 9
    mark_anchor_integrity([row], now=1_700_200_000.0)
    assert row["anchor_verdict"] == ANCHOR_CLEAN
    # Verified by the reader, not by the producer — the panel reports both.
    assert row["anchor_engine_stamped"] is False


def test_a_row_neither_check_can_evaluate_is_unverified_not_clean():
    """An unknown reported as a pass is how the bug this check exists for
    survived a whole export."""
    row = _resolved_row()
    row.pop("first_step_bars", None)
    row.pop("anchor_bars_behind", None)
    row["timeframe"] = "2h"          # no width mirrored for it — refuse
    mark_anchor_integrity([row], now=1_700_200_000.0)
    assert row["anchor_verdict"] == ANCHOR_UNVERIFIED
    # Unverified stays IN the R's: excluding rows we have no evidence against
    # would empty the population. The count is stated instead.
    s = summarize_resolved([row])
    assert s["unverified"] == 1 and s["replayed"] == 0 and s["measurable"] == 1


def test_the_anchor_panel_counts_every_row_exactly_once():
    _live, resolved = _rows()
    rows = mark_anchor_integrity([dict(r) for r in resolved], now=1_700_200_000.0)
    counts = count_anchor_verdicts(rows)
    assert counts["total"] == len(rows)
    assert (
        counts[ANCHOR_CLEAN] + counts[ANCHOR_REPLAYED]
        + counts[ANCHOR_SUSPECT] + counts[ANCHOR_UNVERIFIED]
    ) == len(rows)


# --------------------------------------------------------------------------- #
# Both denominators — R against the SL, and R against the risk actually parked
# --------------------------------------------------------------------------- #


def _handed_over_row():
    _live, resolved = _rows()
    return next(dict(r) for r in resolved if r.get("handover_at") is not None)


def test_risk_adjusted_r_divides_by_the_stop_the_arm_actually_parked():
    row = _handed_over_row()
    row["handover_risk_pct"] = row["sl_distance_pct"] * 2.0
    mark_risk_adjusted_r([row])
    assert row["risk_denominator_pct"] == pytest.approx(row["handover_risk_pct"])
    assert row["r_level_risk"] == pytest.approx(
        row["pnl_level_pct"] / row["handover_risk_pct"]
    )
    # A wider stop cannot make the same loss look worse than the designed R.
    assert abs(row["r_level_risk"]) < abs(row["r_level"])


def test_an_arm_that_never_handed_over_took_exactly_its_designed_risk():
    """Not a fallback — the original stop *is* the risk that arm ran."""
    row = _handed_over_row()
    row["handover_at"] = None
    row["handover_risk_pct"] = None
    mark_risk_adjusted_r([row])
    assert row["risk_denominator_pct"] == pytest.approx(row["sl_distance_pct"])
    assert row["r_level_risk"] == pytest.approx(row["r_level"])


def test_the_two_denominators_are_published_side_by_side_never_blended():
    _live, resolved = _rows()
    rows = mark_risk_adjusted_r(
        mark_anchor_integrity([dict(r) for r in resolved], now=1_700_200_000.0)
    )
    s = summarize_resolved(rows)
    assert s["avg_r_level"] is not None
    assert s["avg_r_level_risk"] is not None
    assert "avg_r" not in s
    assert "avg_r_blended" not in s


def test_risk_r_refuses_rather_than_scoring_a_row_with_no_usable_risk():
    row = _resolved_row()
    row["handover_at"] = None
    row["handover_risk_pct"] = None
    row["sl_distance_pct"] = 0.0
    mark_risk_adjusted_r([row])
    assert row["risk_denominator_pct"] is None
    assert row["r_level_risk"] is None


# --------------------------------------------------------------------------- #
# Cross-repo contract for the anchor stamps, on current engine output
# --------------------------------------------------------------------------- #


def test_the_engine_writes_the_anchor_stamps_this_page_grades_on():
    """#817 from the consuming side. ``fixtures_sar_live_freshness.json`` is
    regenerated by 360-v2's ``scripts/gen_ops_sar_live_fixture.py``, so this
    fails the moment the engine stops writing a field the anchor panel reads —
    loudly, instead of quietly grading every arm as unverified."""
    fresh = json.loads(
        (Path(__file__).parent / "fixtures_sar_live_freshness.json").read_text()
    )
    rows = fresh["open"] + fresh["resolved"]
    assert rows, "the freshness fixture is empty"
    for row in rows:
        assert "anchor_bars_behind" in row
        assert "first_step_bars" in row
    # And the values are the engine's, not placeholders: the advancing arm
    # consumed exactly one bar on its first step.
    advancing = [r for r in rows if r.get("first_step_bars") is not None]
    assert advancing and all(r["first_step_bars"] == 1 for r in advancing)


def test_the_page_renders_the_anchor_panel_whether_or_not_anything_failed():
    with _client() as client:
        r = client.get("/signals/sar-live?tab=resolved")
    assert r.status_code == 200
    assert "walk history" in r.text
    assert "stepped forward" in r.text
    assert "R @risk" in r.text


def test_a_replayed_arm_is_badged_in_the_resolved_table():
    payload = json.loads(json.dumps(FIXTURE))
    for row in payload["resolved"]:
        row["first_step_bars"] = 158
        row["anchor_bars_behind"] = 0.0
    with _client(payload=payload) as client:
        r = client.get("/signals/sar-live?tab=resolved")
    assert r.status_code == 200
    assert ">replayed" in r.text
    assert "walked\n    history at open" in r.text or "walked" in r.text


# --------------------------------------------------------------------------- #
# The engine's `open` set stopped meaning "running SAR arms" (engine schema 2)
# --------------------------------------------------------------------------- #


class TestOpenSetIsNotTheRunningSet:
    """A row can sit in the engine's ``open`` list with its SAR arm long closed.

    The held-to-stop arm exits at the ORIGINAL stop, normally later than the SAR
    flip, so the engine keeps the row open while *either* arm is owed a verdict.
    Reading ``open`` as "running SAR arms" would put a resolved fill in the
    Running table under a live mark and a "Dist. to stop" column — a finished
    trade rendered as an open one, on the page whose entire identity is knowing
    the difference.
    """

    def _payload(self):
        return {
            "open": [
                {"arm_id": "a:15m", "status": "RUNNING", "opened_at": 200.0},
                # SAR is done; only the held arm is still walking.
                {"arm_id": "b:15m", "status": "CLOSED_SAR_FLIP", "opened_at": 100.0,
                 "closed_at": 150.0, "hold_status": "OPEN"},
            ],
            "resolved": [
                {"arm_id": "c:15m", "status": "CLOSED_SL", "closed_at": 50.0},
            ],
        }

    def test_a_hold_only_row_is_not_rendered_as_a_running_arm(self):
        live, done = reduce_arms(self._payload())
        assert [r["arm_id"] for r in live] == ["a:15m"]
        assert "b:15m" in {r["arm_id"] for r in done}

    def test_its_sar_verdict_still_counts_in_the_resolved_population(self):
        """The SAR figures must not lose a row just because its sibling walks on."""
        _, done = reduce_arms(self._payload())
        assert len(done) == 2, "the SAR verdict belongs with the resolved rows"

    def test_a_running_row_with_no_hold_status_is_unaffected(self):
        """Schema-1 rows partition exactly as they always did."""
        live, done = reduce_arms(
            {"open": [{"arm_id": "x", "status": "RUNNING"}], "resolved": []}
        )
        assert [r["arm_id"] for r in live] == ["x"]
        assert done == []



# --------------------------------------------------------------------------- #
# Which arm is actually being traded (2026-08-11)
# --------------------------------------------------------------------------- #
#
# Owner: *"make SAR live and Binance autotrade exactly same"*.  They are not one
# population and cannot be: the trail governor runs ONE mechanism on ONE
# timeframe for the users who opted in, while this page renders 5m and 15m as
# independent arms across two mechanisms and two lanes.  At most a quarter of
# what a reader sees describes the account, and the page said nothing about
# which quarter — the governor's INXUSDT stop sat at the 5m arm's level while
# the 15m arm beside it was parked 3.7% away.


def _gov_payload(**over):
    base = {
        "schema": 1, "enabled": True, "timeframe": "5m",
        "index_cold": False, "open_total": 1, "governed": 1,
        "health": {}, "rows": [{"symbol": "BTCUSDT", "mechanism": "sar"}],
    }
    base.update(over)
    return base


def _with_governor(client, payload):
    """Swap only `engine_api.trail_governor`, leaving every other call real.

    Applied INSIDE the client context: `_client()` enters the lifespan, which
    installs the real engine client, so a swap made before it would be
    overwritten and every assertion below would run against the real one.
    """
    from app.main import app

    class _API:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        async def trail_governor(self):
            if isinstance(payload, Exception):
                raise payload
            return payload

    app.state.engine_api = _API(app.state.engine_api)
    return client


def _gov_html(payload, lane="delivered"):
    with _client() as client:
        _with_governor(client, payload)
        return client.get(f"/signals/sar-live?lane={lane}").text


def test_the_executing_timeframe_is_named_on_the_page():
    """GUARD (fails pre-fix — the page had no such panel).

    Both timeframes render as equals and only one is resting on a real account.
    """
    html = _gov_html(_gov_payload(timeframe="5m"))
    assert "EXECUTING" in html
    assert "5m" in html


def test_the_dark_lane_is_never_badged_as_executing():
    """A dark row reached nobody, so no position exists to govern. The lane
    check lives in the route rather than the template, so the dark page cannot
    badge a row live by omission."""
    html = _gov_html(_gov_payload(), lane="dark")
    assert "EXECUTING —" not in html
    assert "MEASUREMENT ONLY" in html


def test_a_governor_running_the_other_mechanism_does_not_claim_this_page():
    """The mechanism is per user, so "the governor is on" says nothing about
    which mechanism any account chose. Read it off the governed rows."""
    html = _gov_html(
        _gov_payload(rows=[{"symbol": "BTCUSDT", "mechanism": "chandelier"}])
    )
    assert "EXECUTING —" not in html
    assert "other mechanism" in html.lower()


def test_governor_off_reads_as_measurement_only():
    html = _gov_html(_gov_payload(enabled=False, governed=0, rows=[]))
    assert "MEASUREMENT ONLY" in html
    assert "EXECUTING —" not in html


def test_an_engine_that_cannot_answer_says_so_rather_than_the_safe_negative():
    """Absence of knowledge is not permission to assert the negative. A page
    reading "nothing here is live" while a stop moves on a real account is the
    reassuring blank the governor page exists to refuse."""
    html = _gov_html(RuntimeError("engine down"))
    assert "CANNOT TELL" in html
    assert "MEASUREMENT ONLY" not in html


def test_a_governor_with_no_reported_timeframe_is_a_fault_not_a_quiet_state():
    """It has been stored wrong before — `5` where the candle store is keyed
    `5m` — and the governor cannot hand over without one."""
    html = _gov_html(_gov_payload(timeframe=""))
    assert "did not report a timeframe" in html


def test_the_page_no_longer_claims_it_cannot_touch_capital():
    """GUARD. "Nothing on this page has touched anyone's capital" was true when
    written and became false when the trail governor shipped: one arm here is
    the mechanism running on a real account."""
    html = _gov_html(_gov_payload())
    assert "has touched anyone's capital" not in html
    assert "/signals/trail-governor" in html


# --------------------------------------------------------------------------- #
# One page, two mechanisms — the wording must follow the mechanism
# --------------------------------------------------------------------------- #

def test_the_confirm_row_does_not_call_a_chandelier_stop_a_flip():
    """A SAR *reverses*; a chandelier stop is simply *touched*.

    The engine names them apart (CLOSED_SAR_FLIP vs CLOSED_TRAIL_STOP) precisely
    so a page can say which event happened. This row said "Confirmed flip" on
    both pages, which is one word covering two events on the surface whose whole
    job is telling mechanisms apart.
    """
    import re
    from pathlib import Path

    tpl = Path("app/templates/sar_live.html").read_text()
    row = tpl[tpl.find("(@confirm)") - 600: tpl.find("(@confirm)") + 60]
    assert "mech.has_direction" in row, (
        "the confirm row must branch on the mechanism's own has_direction, "
        "not assume a flip"
    )
    # And the directionless wording must exist for the chandelier to use.
    assert "Confirmed close beyond the stop" in tpl


def test_the_governor_mechanism_renders_a_label_not_a_raw_key():
    """`executing.mechanism` is the engine's key (`sar` / `chandelier`).

    Rendering it straight puts "chandelier" in front of a reader while this
    module has held "ATR-trail (Chandelier)" all along — the strategy_catalog
    label seam, one field over.
    """
    from pathlib import Path

    tpl = Path("app/templates/sar_live.html").read_text()
    assert "executing.mechanism_label" in tpl
    # The raw key must not be what gets printed.
    assert "{{ executing.mechanism or" not in tpl


def test_an_unknown_mechanism_keeps_the_engines_word_and_is_badged():
    """Never rename a mechanism we have no label for — badge it instead."""
    from app.routes.sar_live import MECHANISM_FALLBACK

    assert "chandelier" in MECHANISM_FALLBACK
    assert MECHANISM_FALLBACK["chandelier"]["label"] == "ATR-trail (Chandelier)"

    from pathlib import Path
    tpl = Path("app/templates/sar_live.html").read_text()
    assert "mechanism_known" in tpl, (
        "an unlabelled mechanism must be visibly badged rather than silently "
        "borrowing another mechanism's name"
    )


def test_the_risk_paragraph_carries_no_frozen_anecdote():
    """A specific signal's numbers, hardcoded, told a SAR story on the ATR page.

    The paragraph's argument is general and stays; the anecdote was frozen at
    the first two arms ever opened and the table below it shows the live
    distribution anyway.
    """
    from pathlib import Path

    tpl = Path("app/templates/sar_live.html").read_text()
    assert "MUUUSDT" not in tpl

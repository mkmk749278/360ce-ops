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
    GOV_GEOMETRY,
    GOV_SAR,
    LIVE_STALE_SEC,
    STATUS_INSUFFICIENT,
    STATUS_RUNNING,
    filter_arms,
    mark_distance_to_stop,
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
    """Refuse, don't clamp — scoring them 0R makes missing data read as mediocre."""
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
    assert s["win_rate_level"] is None


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
    assert "every 60s" in state["detail"]
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
        vol_arms = vol.sar_live_arms
        vol_prov = vol.sar_live_provenance
        klines_fetch = klines.fetch_all_prices
        vol.sar_live_arms = lambda: (payload if payload is not None else FIXTURE)
        vol.sar_live_provenance = lambda: _prov
        klines.fetch_all_prices = _fetch_all_prices
        try:
            client.post("/login", data={"password": "test-token"})
            yield client
        finally:
            vol.sar_live_arms = vol_arms
            vol.sar_live_provenance = vol_prov
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

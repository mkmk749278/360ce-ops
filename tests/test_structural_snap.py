"""The structural-snap page must not let a bounded measurement look complete.

The engine's snap has two arms with **different knowability**, and every way
this page could quietly lie is a way of forgetting that:

* blending a fully-decidable arm with a partly-decidable one into a single
  headline that then moves with the refusal rate rather than the mechanism,
* folding the two undecidable SL cases into one "unknown" count, when they
  remove opposite ends of the distribution,
* scoring a refused row as zero, which is the fabrication class arriving as a
  rate rather than as a number,
* measuring the "now" baseline over a different population than the arm scored,
* letting the catch-all ``/signals/{signal_id}`` swallow the page.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app.data_sources.structural_snap import (  # noqa: E402
    SCORED,
    V_DECIDED,
    V_NO_EXCURSION,
    V_NO_OUTCOME,
    V_UNCHANGED,
    V_UNDECIDABLE_ORDER,
    V_UNDECIDABLE_TRUNC,
    build_report,
    setups_present,
)


def _row(**kw):
    """A stamp in the engine's real shape.

    Field names mirror ``src/structural_snap.compute``'s output exactly — a
    hand-invented shape asserts your assumption back at you and goes green over
    dead code, which is how ``smc_zone_dist_atr`` stayed uncomputable for its
    whole life.
    """
    base = {
        "signal_id": "S1",
        "symbol": "TESTUSDT",
        "setup_class": "MOVER_TREND_PULLBACK",
        "direction": "LONG",
        "tf": "15m",
        "entry": 100.0,
        "refused": "",
        "apply_mode": False,
        "applied_sl": False,
        "applied_tp1": False,
        "apply_refused": "",
        "sl_arith": 97.0,
        "sl_snapped": 97.0,
        "sl_source": "none",
        "sl_shift_pct": 0.0,
        "tp1_arith": 103.0,
        "tp1_snapped": 103.0,
        "tp1_source": "none",
        "tp1_shift_pct": 0.0,
        "n_swing_highs": 2,
        "n_swing_lows": 2,
        "round_step_pct": 1.0,
        "bars": 60,
    }
    base.update(kw)
    return base


def _rec(**kw):
    """A closed-signal record in ``signal_performance.json``'s real shape.

    MFE is a POSITIVE pnl percent and MAE a NEGATIVE one — both are written by
    ``trade_monitor`` as ``max(..., sig.pnl_pct)`` / ``min(..., sig.pnl_pct)``,
    i.e. already direction-adjusted. Getting that sign convention wrong would
    invert every verdict on this page.
    """
    base = {
        "signal_id": "S1",
        "pnl_pct": -3.0,
        "hit_sl": True,
        "hit_tp": 0,
        "max_favorable_excursion_pct": 0.5,
        "max_adverse_excursion_pct": -3.0,
    }
    base.update(kw)
    return base


def _ledger(rows, **kw):
    payload = {"schema": 1, "written_at": 1.0, "max_rows": 4000,
               "evicted": 0, "rows": rows,
               "spec": {"sl_band": [0.7, 1.3], "tp1_band": [0.8, 1.2],
                        "tp1_direction": "nearer_only",
                        "swing_lookback": 20, "swing_window": 3}}
    payload.update(kw)
    return payload


# ---------------------------------------------------------------------------
# TP1 arm — fully decidable from MFE
# ---------------------------------------------------------------------------

class TestTp1Arm:
    def test_a_nearer_target_the_trade_reached_is_booked_as_reached(self):
        """The trade stopped out, but ran far enough to have hit the snapped
        target first. Every recorded excursion precedes the close, so this is a
        rescue and there is no ordering ambiguity."""
        rows = [_row(tp1_snapped=101.0, tp1_source="swing", tp1_shift_pct=-2.0)]
        rec = _rec(pnl_pct=-3.0, max_favorable_excursion_pct=1.4)
        rep = build_report(_ledger(rows), [rec])
        r = rep.tp1.rows[0]
        assert r.verdict == V_DECIDED
        assert r.arm_pnl_pct == pytest.approx(1.0)
        assert r.delta_pct == pytest.approx(4.0)

    def test_a_nearer_target_the_trade_never_reached_leaves_the_outcome_alone(self):
        rows = [_row(tp1_snapped=101.0, tp1_shift_pct=-2.0)]
        rec = _rec(pnl_pct=-3.0, max_favorable_excursion_pct=0.2)
        rep = build_report(_ledger(rows), [rec])
        r = rep.tp1.rows[0]
        assert r.verdict == V_DECIDED
        assert r.arm_pnl_pct == pytest.approx(-3.0)
        assert r.delta_pct == pytest.approx(0.0)

    def test_a_winner_takes_less_at_the_nearer_target(self):
        rows = [_row(tp1_snapped=101.5, tp1_shift_pct=-1.5)]
        rec = _rec(pnl_pct=3.0, hit_sl=False, hit_tp=1,
                   max_favorable_excursion_pct=3.0, max_adverse_excursion_pct=-0.2)
        rep = build_report(_ledger(rows), [rec])
        r = rep.tp1.rows[0]
        assert r.verdict == V_DECIDED
        assert r.arm_pnl_pct == pytest.approx(1.5)
        assert r.delta_pct == pytest.approx(-1.5)   # the snap COSTS here

    def test_no_level_found_is_unchanged_not_undecidable(self):
        """Counted, and counted apart from a refusal: a rule that changes
        nothing has not been tested however good its delta looks."""
        rep = build_report(_ledger([_row()]), [_rec()])
        r = rep.tp1.rows[0]
        assert r.verdict == V_UNCHANGED
        assert r.delta_pct == pytest.approx(0.0)

    def test_the_tp1_arm_has_no_undecidable_bucket(self):
        """Its whole design claim. If a refusal ever appears here, the claim on
        the page is false and this must fail rather than the reader finding
        out from a shifting number."""
        rows = [
            _row(signal_id="A", tp1_snapped=101.0),
            _row(signal_id="B", tp1_snapped=102.0),
        ]
        recs = [
            _rec(signal_id="A", pnl_pct=-3.0, max_favorable_excursion_pct=2.5),
            _rec(signal_id="B", pnl_pct=3.0, hit_sl=False, hit_tp=1,
                 max_favorable_excursion_pct=3.0, max_adverse_excursion_pct=-2.9),
        ]
        rep = build_report(_ledger(rows), recs)
        assert V_UNDECIDABLE_ORDER not in rep.tp1.counts
        assert V_UNDECIDABLE_TRUNC not in rep.tp1.counts


# ---------------------------------------------------------------------------
# SL arm — the two refusals must stay apart
# ---------------------------------------------------------------------------

class TestSlArm:
    def test_wider_stop_on_a_loser_is_refused_as_truncated(self):
        """The walk ended at the stop it hit. Whether price would have come
        back is not in the record, and guessing removes losers — which
        flatters widening."""
        rows = [_row(sl_snapped=96.0, sl_source="swing", sl_shift_pct=1.0)]
        rec = _rec(pnl_pct=-3.0, hit_sl=True, max_adverse_excursion_pct=-3.0)
        rep = build_report(_ledger(rows), [rec])
        r = rep.sl.rows[0]
        assert r.verdict == V_UNDECIDABLE_TRUNC
        assert r.delta_pct is None
        assert r.verdict not in SCORED

    def test_tighter_stop_on_a_winner_that_drew_down_is_refused_as_ordering(self):
        """MFE and MAE carry no ordering between them. Guessing removes winners
        — which flatters tightening. Opposite bias to the case above, which is
        exactly why the two are named separately."""
        rows = [_row(sl_snapped=98.0, sl_source="swing", sl_shift_pct=-1.0)]
        rec = _rec(pnl_pct=3.0, hit_sl=False, hit_tp=1,
                   max_favorable_excursion_pct=3.0, max_adverse_excursion_pct=-2.5)
        rep = build_report(_ledger(rows), [rec])
        r = rep.sl.rows[0]
        assert r.verdict == V_UNDECIDABLE_ORDER
        assert r.delta_pct is None

    def test_the_two_refusals_are_never_pooled(self):
        rows = [
            _row(signal_id="A", sl_snapped=96.0, sl_shift_pct=1.0),
            _row(signal_id="B", sl_snapped=98.0, sl_shift_pct=-1.0),
        ]
        recs = [
            _rec(signal_id="A", pnl_pct=-3.0, hit_sl=True, max_adverse_excursion_pct=-3.0),
            _rec(signal_id="B", pnl_pct=3.0, hit_sl=False, hit_tp=1,
                 max_favorable_excursion_pct=3.0, max_adverse_excursion_pct=-2.5),
        ]
        rep = build_report(_ledger(rows), recs)
        assert rep.sl.counts[V_UNDECIDABLE_TRUNC] == 1
        assert rep.sl.counts[V_UNDECIDABLE_ORDER] == 1

    def test_tighter_stop_on_a_loser_just_loses_less(self):
        rows = [_row(sl_snapped=98.0, sl_shift_pct=-1.0)]
        rec = _rec(pnl_pct=-3.0, hit_sl=True, max_adverse_excursion_pct=-3.0)
        rep = build_report(_ledger(rows), [rec])
        r = rep.sl.rows[0]
        assert r.verdict == V_DECIDED
        assert r.arm_pnl_pct == pytest.approx(-2.0)
        assert r.delta_pct == pytest.approx(1.0)

    def test_a_stop_never_touched_leaves_the_outcome_alone(self):
        rows = [_row(sl_snapped=98.0, sl_shift_pct=-1.0)]
        rec = _rec(pnl_pct=3.0, hit_sl=False, hit_tp=1,
                   max_favorable_excursion_pct=3.0, max_adverse_excursion_pct=-0.5)
        rep = build_report(_ledger(rows), [rec])
        r = rep.sl.rows[0]
        assert r.verdict == V_DECIDED
        assert r.arm_pnl_pct == pytest.approx(3.0)

    def test_wider_stop_on_a_winner_is_decidable(self):
        """The nearer stop was never hit, so the further one cannot have been."""
        rows = [_row(sl_snapped=96.0, sl_shift_pct=1.0)]
        rec = _rec(pnl_pct=3.0, hit_sl=False, hit_tp=1,
                   max_favorable_excursion_pct=3.0, max_adverse_excursion_pct=-0.5)
        rep = build_report(_ledger(rows), [rec])
        assert rep.sl.rows[0].verdict == V_DECIDED


# ---------------------------------------------------------------------------
# The properties that stop a bounded measurement looking complete
# ---------------------------------------------------------------------------

class TestHonesty:
    def test_a_refused_row_is_excluded_not_scored_zero(self):
        """Scoring an unknown as zero is the fabrication class arriving as a
        rate rather than as a number."""
        rows = [_row(sl_snapped=96.0, sl_shift_pct=1.0)]
        rec = _rec(pnl_pct=-3.0, hit_sl=True, max_adverse_excursion_pct=-3.0)
        rep = build_report(_ledger(rows), [rec])
        assert rep.sl.n_scored == 0
        assert rep.sl.avg_delta_pct is None
        assert rep.sl.decidable_frac == pytest.approx(0.0)

    def test_the_baseline_is_measured_on_the_rows_the_arm_scored(self):
        """A summary over the whole ledger above a table showing a subset is
        not a summary of anything the reader is looking at (#90)."""
        rows = [
            _row(signal_id="A", tp1_snapped=101.0),       # decidable
            _row(signal_id="B"),                          # unchanged, decidable
            _row(signal_id="C", tp1_snapped=101.0),       # never joined
        ]
        recs = [
            _rec(signal_id="A", pnl_pct=-3.0, max_favorable_excursion_pct=1.5),
            _rec(signal_id="B", pnl_pct=-9.0, max_favorable_excursion_pct=0.0),
        ]
        rep = build_report(_ledger(rows), recs)
        # C contributes to neither the baseline nor the arm.
        assert rep.tp1.n_scored == 2
        assert rep.tp1.now_avg_pnl_pct == pytest.approx((-3.0 + -9.0) / 2)

    def test_a_row_that_never_joined_is_no_outcome_not_a_loss(self):
        rep = build_report(_ledger([_row(signal_id="Z")]), [])
        assert rep.tp1.rows[0].verdict == V_NO_OUTCOME
        assert rep.tp1.n_scored == 0

    def test_a_record_with_no_excursion_is_its_own_state(self):
        """Pre-excursion-tracking records sit here permanently. Different
        cause, different caption, and never pooled with 'not yet joined'."""
        rows = [_row(tp1_snapped=101.0)]
        rec = {"signal_id": "S1", "pnl_pct": -3.0, "hit_sl": True}
        rep = build_report(_ledger(rows), [rec])
        assert rep.tp1.rows[0].verdict == V_NO_EXCURSION

    def test_refusals_are_named_and_never_reach_an_arm(self):
        rows = [
            _row(signal_id="A", refused="tf_unknown"),
            _row(signal_id="B", refused="short_series"),
            _row(signal_id="C", tp1_snapped=101.0),
        ]
        rep = build_report(_ledger(rows), [_rec(signal_id="C")])
        assert rep.n_rows == 3
        assert rep.n_measured == 1
        assert rep.refusals == {"tf_unknown": 1, "short_series": 1}
        assert rep.tp1.n_total == 1

    def test_the_ring_denominator_travels_with_the_data(self):
        """A capped buffer whose cap is invisible makes every rate here a
        sample nobody knows is a sample."""
        rep = build_report(_ledger([_row()], evicted=1200, max_rows=4000), [_rec()])
        assert rep.evicted == 1200
        assert rep.max_rows == 4000

    def test_apply_mode_is_read_off_the_rows_not_mirrored(self):
        rep = build_report(_ledger([_row(apply_mode=True)]), [_rec()])
        assert rep.any_applied is True
        rep2 = build_report(_ledger([_row()]), [_rec()])
        assert rep2.any_applied is False

    def test_there_is_no_blended_figure(self):
        """The two arms are not equally knowable, so one number over both would
        move with the SL arm's refusal rate rather than with the mechanism —
        and those refusals are loss-selected on one side and win-selected on
        the other."""
        rep = build_report(_ledger([_row()]), [_rec()])
        for banned in ("avg_delta_pct_combined", "combined", "overall_delta_pct",
                       "blended", "avg_delta"):
            assert not hasattr(rep, banned), (
                f"SnapReport grew {banned!r} — the arms must stay separate"
            )

    def test_the_filter_applies_before_every_count(self):
        rows = [
            _row(signal_id="A", setup_class="MOVER_TREND_PULLBACK"),
            _row(signal_id="B", setup_class="SR_FLIP_RETEST"),
        ]
        rep = build_report(_ledger(rows), [], setup_class="SR_FLIP_RETEST")
        assert rep.n_rows == 1
        assert rep.tp1.rows[0].signal_id == "B"

    def test_setup_options_count_the_whole_ledger(self):
        """The selector describes the whole ledger, so applying it to its own
        counts would make each option describe only itself."""
        rows = [
            _row(signal_id="A", setup_class="MOVER_TREND_PULLBACK"),
            _row(signal_id="B", setup_class="MOVER_TREND_PULLBACK"),
            _row(signal_id="C", setup_class="SR_FLIP_RETEST"),
        ]
        assert setups_present(_ledger(rows)) == [
            ("MOVER_TREND_PULLBACK", 2), ("SR_FLIP_RETEST", 1),
        ]

    def test_a_missing_ledger_is_an_error_not_an_empty_page(self):
        """An empty page is indistinguishable from a quiet market. 'Blank needs
        a cause before it gets a caption'."""
        rep = build_report({"error": "missing: /engine-data/structural_snap_v1.json"}, [])
        assert "missing" in rep.error
        assert rep.n_rows == 0


# ---------------------------------------------------------------------------
# Route ordering + reachability
# ---------------------------------------------------------------------------

class TestRouteOrdering:
    """``signal_detail`` registers ``/signals/{signal_id}``, which matches any
    ``/signals/<literal>``. Every literal page under ``/signals/`` must be
    included BEFORE it, or the catch-all swallows the request into a 404 while
    the route object sits in ``app.routes`` looking perfectly registered."""

    def test_the_page_is_reachable(self):
        from fastapi.testclient import TestClient

        from app.main import app

        class _DV:
            def structural_snap(self):
                return {"schema": 1, "written_at": 1.0, "rows": []}

            def signal_performance(self):
                return []

        app.state.data_volume = _DV()
        with TestClient(app) as client:
            client.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})
            assert client.get("/signals/structural-snap").status_code == 200

    def test_it_is_included_before_the_catch_all(self):
        import app.main as main

        text = open(main.__file__ or "", encoding="utf-8").read()
        i_page = text.index("app.include_router(structural_snap.router)")
        i_catch = text.index("app.include_router(signal_detail.router)")
        assert i_page < i_catch


class TestRender:
    """An empty page exercises none of the template's row branches.

    The row table walks the TP1 arm and indexes the SL arm by position, which
    is only sound because both arms are appended inside the same loop over the
    same rows. That is an invariant of the reducer, so it is asserted here as
    well as rendered — a template that silently mispairs two arms would put one
    signal's stop verdict beside another's target verdict.
    """

    @contextmanager
    def _client(self, rows, recs):
        """Driven through the real app with lifespan running.

        ``TestClient`` must be entered as a context manager or the startup
        hooks never run and the event loop closes underneath the request.
        """
        from fastapi.testclient import TestClient

        from app.main import app

        class _DV:
            def structural_snap(self):
                return _ledger(rows, evicted=17)

            def signal_performance(self):
                return recs

        with TestClient(app) as client:
            # AFTER entering the context: the lifespan startup builds the real
            # DataVolumeReader and overwrites app.state, so a stub installed
            # beforehand is silently replaced and the page renders the engine
            # volume's "missing file" error instead of the fixture.
            #
            # follow_redirects=False because /login redirects to the pulse
            # page, which reads several other DataVolumeReader methods — the
            # stub only overrides the two this page uses.
            client.post(
                "/login",
                data={"password": os.environ["OPS_AUTH_TOKEN"]},
                follow_redirects=False,
            )
            app.state.data_volume = _DV()
            yield client

    def test_the_arms_are_positionally_aligned(self):
        rows = [_row(signal_id=f"S{i}") for i in range(5)]
        rep = build_report(_ledger(rows), [])
        assert len(rep.tp1.rows) == len(rep.sl.rows)
        for a, b in zip(rep.tp1.rows, rep.sl.rows):
            assert a.signal_id == b.signal_id

    def test_a_populated_page_renders(self):
        rows = [
            _row(signal_id="A", tp1_snapped=101.0, tp1_source="swing",
                 tp1_shift_pct=-2.0),
            _row(signal_id="B", sl_snapped=96.0, sl_source="round",
                 sl_shift_pct=1.0),
            _row(signal_id="C", refused="tf_unknown"),
        ]
        recs = [
            _rec(signal_id="A", pnl_pct=-3.0, max_favorable_excursion_pct=1.5),
            _rec(signal_id="B", pnl_pct=-3.0, hit_sl=True,
                 max_adverse_excursion_pct=-3.0),
        ]
        with self._client(rows, recs) as client:
            resp = client.get("/signals/structural-snap")
        assert resp.status_code == 200
        body = resp.text
        assert "TP1 arm" in body and "SL arm" in body
        # The refusal is named on screen rather than pooled into "no data".
        assert "tf_unknown" in body
        # The ring cap is disclosed.
        assert "17" in body and "evicted" in body
        # The dark badge, read off the rows.
        assert "DARK" in body

    def test_the_csv_export_is_uncapped_and_carries_both_arms(self):
        rows = [_row(signal_id=f"S{i}", tp1_snapped=101.0) for i in range(400)]
        recs = [_rec(signal_id=f"S{i}", max_favorable_excursion_pct=1.5)
                for i in range(400)]
        with self._client(rows, recs) as client:
            resp = client.get("/signals/structural-snap/export.csv")
        assert resp.status_code == 200
        lines = [ln for ln in resp.text.splitlines() if ln.strip()]
        # 400 rows x 2 arms + header. A truncated export is a row cap wearing a
        # download button.
        assert len(lines) == 801


class TestTunablePlumbing:
    """The per-path allow-list is only useful if the owner can set AND clear it.

    Both halves were broken on the first cut and neither would have surfaced as
    an error: a ``str`` tunable rendered through the numeric branch is
    untypeable, and the POST handler's skip-empty rule (right for an untouched
    number field) made "" unsendable — an allow-list that can be added to and
    never cleared, which is the state a money-path switch must never be in.
    """

    def test_a_text_tunable_renders_as_text_not_a_number_input(self):
        from pathlib import Path

        import jinja2

        tpl_dir = Path(__file__).resolve().parents[1] / "app" / "templates"
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(tpl_dir)))
        src = (tpl_dir / "control.html").read_text()
        # Asserted on the template source: the branch must exist at all.
        assert "t.type == 'str'" in src
        assert '_str_keys' in src
        env.parse(src)  # and it must still be valid Jinja

    def test_the_handler_sends_an_empty_string_tunable(self):
        """Driven through the real route body rather than asserting the source,
        so a future refactor that drops the branch fails here."""
        import asyncio

        from app.routes import control

        sent: dict = {}

        class _Api:
            async def set_tunables(self, values):
                sent.update(values)
                return {"initialised": True}

        class _Req:
            def __init__(self):
                self.session = {}
                self.app = type("A", (), {"state": type("S", (), {
                    "engine_api": _Api(),
                    "settings": type("C", (), {"audit_log_path": "/dev/null"})(),
                })()})()

            async def form(self):
                class _F(dict):
                    def multi_items(self):
                        return list(self.items())
                return _F({
                    "_bool_keys": "structural_snap_apply",
                    "_str_keys": "structural_snap_apply_paths",
                    "structural_snap_apply_paths": "",
                })

        asyncio.run(control.control_tunables(_Req()))
        assert "structural_snap_apply_paths" in sent
        assert sent["structural_snap_apply_paths"] == ""
        # ...and the unchecked bool still becomes an explicit False.
        assert sent["structural_snap_apply"] is False

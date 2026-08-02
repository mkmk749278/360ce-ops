"""The entry-feature page must not let a candidate rule flatter itself.

Owner, 2026-08-01: *"we need to know the difference as of now vs later"*. This
page answers that by putting the book as it shipped beside the book one rule
would have produced — on the same rows, immediately, rather than waiting for two
time periods to accumulate.

Everything pinned here is a way that comparison could quietly lie:

* rows the rule could not judge being folded into the side that flatters it,
* the "now" baseline drifting between rows so the comparison isn't like-for-like,
* R computed off the exit stop rather than the entry risk (#848, one page over),
* a rule that keeps everything reading as a free win.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
# conftest imports first and sets this to "test-token"; setdefault here keeps a
# standalone run of this module working without diverging from it.
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app.routes.entry_features import (  # noqa: E402
    FALLBACK_SPEC,
    FEATURE_COPY,
    entry_quality_panel,
    features_for,
    join_outcomes,
    setups_present,
    split_by_feature as _split,
)

#: The engine ships this with the ledger. Tests below drive the real reducer
#: with a real-shaped spec rather than letting each call invent one.
_SPEC = {
    "core": ["tp1_r_multiple", "pullback_vol_ratio", "extension_pct"],
    "paths": {
        "TREND_PULLBACK_EMA": ["h1_trend_sep_atr", "rsi_at_entry"],
        "MOVER_AVWAP_SCALP": ["anchor_age_bars", "leg_move_pct"],
    },
    "keep_above": ["tp1_r_multiple", "pullback_vol_ratio", "h1_trend_sep_atr"],
}


def split_by_feature(joined, feature, threshold, spec=None):
    return _split(joined, feature, threshold, spec if spec is not None else _SPEC)


def _stamp(sid, **kw):
    row = {"signal_id": sid, "setup_class": "MOVER_TREND_PULLBACK", "symbol": "BTCUSDT"}
    row.update(kw)
    return row


def _rec(sid, pnl, sl_pct=3.0, **kw):
    rec = {"signal_id": sid, "pnl_pct": pnl, "symbol": "BTCUSDT"}
    if sl_pct is not None:
        rec["sl_distance_pct_at_entry"] = sl_pct
    rec.update(kw)
    return rec


class TestJoin:
    def test_it_divides_by_the_entry_risk_not_the_exit_stop(self):
        """#848, one page over. A BE-shifted trade stopped out for -0.1%
        against a designed 3% risk is a -0.03R scratch, not a -1.00R loss."""
        joined, cov = join_outcomes([_stamp("s1")], [_rec("s1", -0.1, 3.0)])
        assert joined[0]["r"] == pytest.approx(-0.1 / 3.0)
        assert cov["scored"] == 1

    def test_a_record_with_no_entry_risk_gets_no_r_but_keeps_its_pnl(self):
        joined, cov = join_outcomes([_stamp("s1")], [_rec("s1", -3.0, None)])
        assert joined[0]["r"] is None
        assert joined[0]["pnl_pct"] == pytest.approx(-3.0)
        assert cov["scored"] == 0 and cov["joined"] == 1

    def test_a_stamp_with_no_record_is_counted_not_dropped(self):
        """A signal the router dropped, or one still open. Silently discarding
        it would report a book that is not the book."""
        joined, cov = join_outcomes(
            [_stamp("s1"), _stamp("never-delivered")], [_rec("s1", 1.0)]
        )
        assert len(joined) == 1
        assert cov["stamped_not_closed"] == 1 and cov["stamps"] == 2

    def test_junk_payloads_do_not_raise(self):
        assert join_outcomes(None, None) == ([], {
            "stamps": 0, "joined": 0, "stamped_not_closed": 0, "scored": 0})
        assert join_outcomes("nonsense", {"not": "a list"})[0] == []


class TestNowVsLater:
    def _book(self):
        stamps = [
            _stamp("a", pullback_vol_ratio=2.0),
            _stamp("b", pullback_vol_ratio=0.3),
            _stamp("c", pullback_vol_ratio=1.5),
        ]
        recs = [_rec("a", 3.0), _rec("b", -3.0), _rec("c", 6.0)]
        return join_outcomes(stamps, recs)[0]

    def test_now_is_the_whole_book_keep_is_the_subset(self):
        out = split_by_feature(self._book(), "pullback_vol_ratio", 0.8)
        assert out["now"]["n"] == 3
        assert out["keep"]["n"] == 2 and out["drop"]["n"] == 1
        assert out["now"]["avg_r"] == pytest.approx(2.0 / 3.0)
        assert out["keep"]["avg_r"] == pytest.approx(1.5)
        assert out["delta_avg_r"] == pytest.approx(1.5 - 2.0 / 3.0)

    def test_now_does_not_move_when_the_threshold_does(self):
        """The baseline is the book as it shipped. If it drifted with the
        threshold, the comparison would not be like-for-like and every Δ on the
        page would be measured against a different thing."""
        book = self._book()
        a = split_by_feature(book, "pullback_vol_ratio", 0.1)
        b = split_by_feature(book, "pullback_vol_ratio", 1.9)
        assert a["now"] == b["now"]

    def test_unjudgeable_rows_are_their_own_bucket(self):
        """The failure this prevents: binning unknowns with `keep` lets a rule
        take credit for rows it never filtered."""
        stamps = [_stamp("a", pullback_vol_ratio=2.0), _stamp("b"), _stamp("c")]
        recs = [_rec("a", 3.0), _rec("b", -3.0), _rec("c", 9.0)]
        out = split_by_feature(join_outcomes(stamps, recs)[0], "pullback_vol_ratio", 0.8)
        assert out["unknown"]["n"] == 2
        assert out["keep"]["n"] == 1
        assert out["keep"]["n"] + out["drop"]["n"] + out["unknown"]["n"] == out["now"]["n"]

    def test_direction_is_inverted_for_features_where_high_is_the_problem(self):
        """`extension_pct` keeps rows BELOW the threshold — a rule that kept the
        most extended entries would be backwards, and the arrow on screen is how
        the reader knows which way it runs.

        The direction comes from the engine's spec, not from an ops-side flag:
        ops does not decide which way a feature it does not stamp should filter.
        """
        assert "extension_pct" not in _SPEC["keep_above"]
        stamps = [_stamp("a", extension_pct=2.0), _stamp("b", extension_pct=40.0)]
        recs = [_rec("a", 3.0), _rec("b", -3.0)]
        out = split_by_feature(join_outcomes(stamps, recs)[0], "extension_pct", 15.0)
        assert out["direction"] == "≤"
        assert out["keep"]["n"] == 1 and out["drop"]["n"] == 1

    def test_a_rule_that_keeps_everything_is_visible_as_such(self):
        out = split_by_feature(self._book(), "pullback_vol_ratio", 0.01)
        assert out["kept_fraction"] == pytest.approx(1.0)
        assert out["delta_avg_r"] == pytest.approx(0.0)

    def test_pnl_is_published_beside_r_on_every_split(self):
        """The R-scored population is not a random sample of the book, so a
        page that showed only R would describe a subset and not say so."""
        stamps = [_stamp("a", pullback_vol_ratio=2.0), _stamp("b", pullback_vol_ratio=2.0)]
        recs = [_rec("a", 3.0), _rec("b", -5.0, None)]   # b has no denominator
        out = split_by_feature(join_outcomes(stamps, recs)[0], "pullback_vol_ratio", 0.8)
        assert out["keep"]["n"] == 2 and out["keep"]["scored"] == 1
        assert out["keep"]["avg_r"] == pytest.approx(1.0)
        assert out["keep"]["avg_pnl_pct"] == pytest.approx(-1.0)

    def test_an_empty_book_yields_none_never_zero(self):
        out = split_by_feature([], "pullback_vol_ratio", 0.8)
        assert out["now"]["avg_r"] is None
        assert out["delta_avg_r"] is None
        assert out["kept_fraction"] is None


class TestTheRegistryIsNotMirroredHere:
    """Which features a path declares and which way a rule filters are decided
    by the engine that stamps them, and arrive in the ledger's ``spec``.

    Ops kept its own copy of ``MEASUREMENT_SUFFIXES`` once; it drifted and
    inflated the Strategy Lab rollup for a week. The fix for a drifting mirror
    is not a second mirror.
    """

    def test_the_direction_comes_from_the_spec_not_from_ops(self):
        rows = [
            {"f": 1.0, "r": 1.0, "pnl_pct": 1.0},
            {"f": 9.0, "r": -1.0, "pnl_pct": -1.0},
        ]
        above = split_by_feature(rows, "f", 5.0, {"keep_above": ["f"]})
        below = split_by_feature(rows, "f", 5.0, {"keep_above": []})
        assert above["direction"] == "≥" and above["keep"]["n"] == 1
        assert below["direction"] == "≤" and below["keep"]["n"] == 1
        assert above["keep"]["avg_r"] != below["keep"]["avg_r"]

    def test_columns_are_core_plus_the_selected_path(self):
        tpe = features_for(_SPEC, "TREND_PULLBACK_EMA")
        mvavw = features_for(_SPEC, "MOVER_AVWAP_SCALP")
        assert tpe[: len(_SPEC["core"])] == _SPEC["core"]
        assert "h1_trend_sep_atr" in tpe
        # The point of splitting: neither path's own features leak onto the other.
        assert "anchor_age_bars" not in tpe
        assert "h1_trend_sep_atr" not in mvavw

    def test_with_no_path_selected_the_union_is_shown_core_first(self):
        every = features_for(_SPEC, "")
        assert every[: len(_SPEC["core"])] == _SPEC["core"]
        assert {"h1_trend_sep_atr", "anchor_age_bars"} <= set(every)
        assert len(every) == len(set(every)), "a feature must not appear twice"

    def test_an_engine_feature_ops_has_no_copy_for_still_renders(self):
        """Silently dropping an unknown feature would make a newly added engine
        input invisible on the page that exists to read it."""
        out = split_by_feature([], "a_brand_new_engine_feature", 1.0)
        assert out["label"] == "a_brand_new_engine_feature"

    def test_the_fallback_is_only_a_fallback(self):
        """It exists so a pre-`spec` ledger still renders. It is deliberately
        minimal — a full second registry here is the mirror we are avoiding."""
        assert FALLBACK_SPEC["paths"] == {}
        assert FALLBACK_SPEC["core"]

    def test_every_feature_ops_has_copy_for_states_its_question(self):
        """A split with no stated question invites the reader to invent one."""
        for name, copy in FEATURE_COPY.items():
            assert copy.get("asks"), name
            assert copy.get("label"), name
            assert copy.get("default") is not None, name


class TestTimeframesAreNotPooledSilently:
    """TPE triggers on 5m and the mover paths on 15m. A volume ratio over 5m
    bars and one over 15m bars are different measurements, so a split that spans
    both has to say so rather than presenting one number."""

    def test_a_single_timeframe_split_is_not_flagged(self):
        rows = [{"tf_name": "5m", "f": 1.0, "r": 1.0, "pnl_pct": 1.0}]
        out = split_by_feature(rows, "f", 0.5, {"keep_above": ["f"]})
        assert out["timeframes"] == ["5m"]
        assert out["mixed_timeframes"] is False

    def test_a_split_spanning_two_series_is_flagged(self):
        rows = [
            {"tf_name": "5m", "f": 1.0, "r": 1.0, "pnl_pct": 1.0},
            {"tf_name": "15m", "f": 2.0, "r": -1.0, "pnl_pct": -1.0},
        ]
        out = split_by_feature(rows, "f", 0.5, {"keep_above": ["f"]})
        assert out["timeframes"] == ["15m", "5m"]
        assert out["mixed_timeframes"] is True


class TestTheSelectorDescribesTheWholeBook:
    def test_counts_come_from_the_data_not_a_hardcoded_list(self):
        """A path that starts or stops stamping must be visible rather than
        silently absent — a fixed list shows exactly the paths someone typed."""
        rows = [
            {"setup_class": "MOVER_AVWAP_SCALP"},
            {"setup_class": "MOVER_AVWAP_SCALP"},
            {"setup_class": "TREND_PULLBACK_EMA"},
            {"setup_class": "A_PATH_NOBODY_HAS_HEARD_OF"},
        ]
        assert setups_present(rows) == [
            ("MOVER_AVWAP_SCALP", 2),
            ("A_PATH_NOBODY_HAS_HEARD_OF", 1),
            ("TREND_PULLBACK_EMA", 1),
        ]

    def test_a_row_with_no_setup_is_named_not_dropped(self):
        assert setups_present([{}]) == [("UNKNOWN", 1)]


class TestRoute:
    def _client(self, monkeypatch=None, stamps=(), records=()):
        from fastapi.testclient import TestClient

        from app.main import app

        class _DV:
            def entry_features(self):
                return {"schema": 1, "written_at": 1.0, "rows": stamps}

            def signal_performance(self):
                return records

        app.state.data_volume = _DV()
        return TestClient(app)

    def _login(self, client):
        client.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})

    def test_the_page_separates_what_is_applied_from_what_is_a_question(self):
        """Copy is part of the measurement, and this page's copy changed.

        It said "Nothing on this page is applied" for its first day, and that
        sentence became false the moment the entry-quality gate shipped. A page
        that carries a live gate and a what-if table has to say which is which
        — a reader who conflates them will read a shadow Δ as a live effect.
        """
        client = self._client(
            monkeypatch=None,
            stamps=[_stamp("a", pullback_vol_ratio=2.0)],
            records=[_rec("a", 3.0)],
        )
        with client:
            self._login(client)
            r = client.get("/signals/entry-features")
            assert r.status_code == 200
            assert "Nothing on this page is applied" not in r.text
            assert "Live entry-quality rules" in r.text
            assert "applied nowhere" in r.text

    def test_an_empty_lane_says_quiet_not_broken(self, monkeypatch):
        """The dark page reported a fault that was not happening on exactly this
        shape. Empty must read as quiet, with the check to distinguish them."""
        client = self._client(monkeypatch, [], [])
        with client:
            self._login(client)
            r = client.get("/signals/entry-features")
            assert r.status_code == 200
            assert "quiet" in r.text.lower()

    def test_a_broken_data_volume_degrades_rather_than_500s(self, monkeypatch):
        from fastapi.testclient import TestClient

        from app.main import app

        class _Boom:
            def entry_features(self):
                raise RuntimeError("volume unmounted")

            def signal_performance(self):
                return []

        app.state.data_volume = _Boom()
        client = TestClient(app)
        with client:
            self._login(client)
            assert client.get("/signals/entry-features").status_code == 200


class TestRouteOrdering:
    """``signal_detail`` registers ``/signals/{signal_id}``, which matches any
    ``/signals/<literal>`` path. Every literal page under ``/signals/`` must be
    included BEFORE it or the catch-all swallows the request into a 404 — this
    page was registered after it on the first cut and 404'd while its own route
    object sat in ``app.routes`` looking perfectly registered.
    """

    def test_the_page_is_reachable_and_not_shadowed_by_the_catch_all(self):
        from fastapi.testclient import TestClient

        from app.main import app

        class _DV:
            def entry_features(self):
                return {"schema": 1, "written_at": 1.0, "rows": []}

            def signal_performance(self):
                return []

        app.state.data_volume = _DV()
        with TestClient(app) as client:
            client.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})
            assert client.get("/signals/entry-features").status_code == 200

    def test_it_is_included_before_the_catch_all(self):
        """Asserted on the router order itself, so the reason survives even if
        the request test above is later changed."""
        import app.main as main

        src = (main.__file__ or "")
        text = open(src, encoding="utf-8").read()
        i_page = text.index("app.include_router(entry_features.router)")
        i_catch = text.index("app.include_router(signal_detail.router)")
        assert i_page < i_catch, (
            "entry_features must be registered before signal_detail, whose "
            "/signals/{signal_id} route matches any /signals/<literal>"
        )


class TestRegimeProvenance:
    """The regime on a joined row comes from the closed-signal record.

    Engine-side, ``sig.entry_regime`` is written by
    ``scanner._populate_signal_context`` — which runs AFTER the evaluator that
    produces the stamp. The first cut of the stamp read the attribute there and
    recorded "" on every row; nothing crashed, and the per-regime split would
    have silently described one undifferentiated bucket.

    Fixed on the producing side. This page prefers the record regardless,
    because the scanner's value is the finalised one.
    """

    def test_the_record_wins_over_the_stamp(self):
        joined, _ = join_outcomes(
            [_stamp("s1", entry_regime="RANGING")],
            [_rec("s1", 1.0, entry_regime="TRENDING_UP")],
        )
        assert joined[0]["entry_regime"] == "TRENDING_UP"

    def test_the_stamp_is_the_fallback_when_the_record_predates_the_field(self):
        joined, _ = join_outcomes(
            [_stamp("s1", entry_regime="VOLATILE")], [_rec("s1", 1.0)]
        )
        assert joined[0]["entry_regime"] == "VOLATILE"

    def test_neither_source_yields_unplaced_not_an_empty_string(self):
        """An empty label renders as a nameless bucket that reads like a real
        one. UNPLACED says which rows could not be placed — the same word
        /track-record uses for pre-#817 records."""
        joined, _ = join_outcomes([_stamp("s1")], [_rec("s1", 1.0)])
        assert joined[0]["entry_regime"] == "UNPLACED"


class TestPerPathRendering:
    """The page's primary control is the path selector, not a filter.

    A split drawn across paths that share no trigger, timeframe or stop geometry
    moves with the setup mix as much as with the feature.
    """

    def _client(self, stamps, records, spec=None):
        from fastapi.testclient import TestClient

        from app.main import app

        payload = {"schema": 2, "written_at": 1.0, "rows": stamps}
        if spec is not None:
            payload["spec"] = spec

        class _DV:
            def entry_features(self):
                return payload

            def signal_performance(self):
                return records

        # Installed by the caller INSIDE the context manager: app startup
        # assigns a real DataVolumeReader over app.state.data_volume, so a stub
        # set before entering is silently replaced and every assertion below
        # would run against an empty ledger.
        self._stub = _DV()
        return TestClient(app)

    def _login(self, client):
        from app.main import app

        client.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})
        app.state.data_volume = self._stub

    @staticmethod
    def _book():
        stamps = [
            {"signal_id": "t1", "setup_class": "TREND_PULLBACK_EMA",
             "symbol": "BTCUSDT", "tf_name": "5m", "h1_trend_sep_atr": 1.2,
             "tp1_r_multiple": 0.8},
            {"signal_id": "m1", "setup_class": "MOVER_AVWAP_SCALP",
             "symbol": "ETHUSDT", "tf_name": "15m", "anchor_age_bars": 30.0,
             "tp1_r_multiple": 1.0},
        ]
        recs = [
            {"signal_id": "t1", "pnl_pct": -2.0, "sl_distance_pct_at_entry": 2.0},
            {"signal_id": "m1", "pnl_pct": 3.0, "sl_distance_pct_at_entry": 3.0},
        ]
        return stamps, recs

    def test_selecting_a_path_shows_only_that_path_s_features(self):
        stamps, recs = self._book()
        client = self._client(stamps, recs, spec=_SPEC)
        with client:
            self._login(client)
            tpe = client.get("/signals/entry-features?setup=TREND_PULLBACK_EMA").text
            assert "h1_trend_sep_atr" in tpe or "1H EMA21/50 separation" in tpe
            # The mover path's own question must not appear on the TPE view.
            assert "Anchor age" not in tpe

    def test_the_selector_counts_the_whole_book_not_the_filtered_view(self):
        """A selector applied to its own counts makes each option describe only
        itself — #90's rule, one page over."""
        stamps, recs = self._book()
        client = self._client(stamps, recs, spec=_SPEC)
        with client:
            self._login(client)
            page = client.get("/signals/entry-features?setup=TREND_PULLBACK_EMA").text
            # Both paths still offered, each with its full count.
            assert "MOVER_AVWAP_SCALP (1)" in page
            assert "TREND_PULLBACK_EMA (1)" in page

    def test_the_unfiltered_view_warns_that_it_pools_paths(self):
        stamps, recs = self._book()
        client = self._client(stamps, recs, spec=_SPEC)
        with client:
            self._login(client)
            page = client.get("/signals/entry-features").text
            assert "Showing every path at once" in page

    def test_a_ledger_without_a_spec_says_it_fell_back(self):
        """A silent fallback is a mirror nobody knows is a mirror."""
        stamps, recs = self._book()
        client = self._client(stamps, recs, spec=None)
        with client:
            self._login(client)
            page = client.get("/signals/entry-features").text
            assert "ops fallback" in page

    def test_a_ledger_with_a_spec_does_not_show_the_fallback_warning(self):
        stamps, recs = self._book()
        client = self._client(stamps, recs, spec=_SPEC)
        with client:
            self._login(client)
            assert "ops fallback" not in client.get("/signals/entry-features").text

    def test_the_number_of_cells_drawn_is_on_screen(self):
        """"Best of N" is not a fact about the winner until N is on screen."""
        stamps, recs = self._book()
        client = self._client(stamps, recs, spec=_SPEC)
        with client:
            self._login(client)
            assert "cells drawn on this view" in client.get(
                "/signals/entry-features"
            ).text

    def test_the_export_respects_the_selected_path(self):
        stamps, recs = self._book()
        client = self._client(stamps, recs, spec=_SPEC)
        with client:
            self._login(client)
            csv = client.get(
                "/signals/entry-features/export.csv?setup=TREND_PULLBACK_EMA"
            ).text
            assert "BTCUSDT" in csv and "ETHUSDT" not in csv
            # ...but every path's columns are present, so a per-path column does
            # not vanish because of the current filter.
            assert "anchor_age_bars" in csv


class TestTheLiveGatePanel:
    """The consuming half of this lane, and the ways it could flatter itself.

    The gate's own populations are the trap here: a candidate it suppressed
    never delivered, so it has no closed-signal record and cannot appear in the
    join every other panel on this page is measured on. A panel built on
    ``joined`` would silently exclude exactly the rows the gate acted on and
    render a live gate that had done nothing.
    """

    @staticmethod
    def _annotated(sid, rule, verdict, live, enforced=""):
        return {
            "signal_id": sid,
            "setup_class": "MOVER_TREND_PULLBACK",
            "eq_enforced_by": enforced,
            "eq_budget_suspended": False,
            "eq_rules": [{
                "rule": rule, "verdict": verdict, "live": live,
                "value": 1.0, "threshold": None, "feature": "profile_would_reject",
            }],
        }

    def test_a_suppressed_candidate_is_counted_even_though_it_never_delivered(self):
        """Read from the stamps, not the join — the whole reason this panel
        takes both."""
        stamps = [self._annotated("s1", "profile_reject", "reject", True,
                                  enforced="profile_reject")]
        panel = entry_quality_panel(stamps, joined=[])
        assert panel["rules"][0]["enforced"] == 1
        assert panel["rules"][0]["live"] is True

    def test_a_shadow_rejection_carries_the_outcome_a_promotion_would_read(self):
        stamps = [self._annotated("s1", "tpe_smc_zone", "reject", False)]
        joined = [{"signal_id": "s1", "pnl_pct": -2.0, "r": -0.5}]
        panel = entry_quality_panel(stamps, joined)
        rule = panel["rules"][0]
        assert rule["shadow_reject"] == 1
        assert rule["enforced"] == 0
        assert rule["would_remove"]["n"] == 1
        assert rule["would_remove"]["avg_pnl_pct"] == pytest.approx(-2.0)

    def test_a_suppression_contributes_no_outcome_because_none_exists(self):
        """It cannot, and the page must not imply otherwise: the gate killed it
        before it could deliver. Those verdicts live in the suppression audit."""
        stamps = [self._annotated("s1", "profile_reject", "reject", True,
                                  enforced="profile_reject")]
        joined = [{"signal_id": "s1", "pnl_pct": -2.0}]   # cannot happen live
        panel = entry_quality_panel(stamps, joined)
        assert panel["rules"][0]["would_remove"]["n"] == 0

    def test_unknown_is_its_own_bucket_and_never_a_pass(self):
        stamps = [{
            "signal_id": "s1", "setup_class": "MOVER_TREND_PULLBACK",
            "eq_enforced_by": "", "eq_rules": [{
                "rule": "profile_reject", "verdict": "unknown", "live": True,
                "unknown_reason": "feature_none", "feature": "profile_would_reject",
            }],
        }]
        rule = entry_quality_panel(stamps, [])["rules"][0]
        assert rule["unknown"] == 1
        assert rule["pass"] == 0
        assert rule["unknown_reasons"] == {"feature_none": 1}

    def test_an_enforcing_rule_that_abstains_on_nearly_everything_is_badged_blind(self):
        """Inert and working look identical on every other count."""
        stamps = [{
            "signal_id": f"s{i}", "setup_class": "MOVER_TREND_PULLBACK",
            "eq_enforced_by": "", "eq_rules": [{
                "rule": "profile_reject", "verdict": "unknown", "live": True,
                "unknown_reason": "feature_none", "feature": "profile_would_reject",
            }],
        } for i in range(25)]
        assert entry_quality_panel(stamps, [])["rules"][0]["blind"] is True

    def test_a_shadow_rule_is_never_badged_blind(self):
        """Abstaining costs nothing while nothing is being enforced, and
        flagging it would fill the signal that is meant to make a real fault
        stand out."""
        stamps = [{
            "signal_id": f"s{i}", "setup_class": "MOVER_TREND_PULLBACK",
            "eq_enforced_by": "", "eq_rules": [{
                "rule": "tpe_smc_zone", "verdict": "unknown", "live": False,
                "unknown_reason": "feature_absent", "feature": "smc_zone_dist_atr",
            }],
        } for i in range(25)]
        assert entry_quality_panel(stamps, [])["rules"][0]["blind"] is False

    def test_rows_stamped_before_the_gate_are_counted_apart_not_as_passes(self):
        """A missing stamp is not a pass — the ledger deliberately did not bump
        its schema for the verdict, so these rows stay in the population."""
        stamps = [{"signal_id": "old", "setup_class": "MOVER_TREND_PULLBACK"}]
        panel = entry_quality_panel(stamps, [])
        assert panel["not_evaluated"] == 1
        assert panel["evaluated"] == 0
        assert panel["rules"] == []

    def test_a_held_back_suppression_is_its_own_state(self):
        """Over the blast-radius cap the gate degrades to shadow. A panel that
        could not tell that from 'the rule passed' would read a suspended gate
        as a healthy one."""
        stamps = [dict(self._annotated("s1", "profile_reject", "reject", True),
                       eq_budget_suspended=True)]
        assert entry_quality_panel(stamps, [])["budget_suspended"] == 1

    def test_the_delivered_book_is_published_as_the_comparison_denominator(self):
        """'This rule would have removed rows averaging −2%' means nothing
        without what the book averaged."""
        joined = [{"signal_id": "s1", "pnl_pct": 1.0}, {"signal_id": "s2", "pnl_pct": -3.0}]
        panel = entry_quality_panel([], joined)
        assert panel["delivered"]["n"] == 2
        assert panel["delivered"]["avg_pnl_pct"] == pytest.approx(-1.0)

    def test_the_mode_is_read_off_the_rows_the_gate_decided(self):
        """Not mirrored from a copy of the engine's registry — ops kept its own
        copy of MEASUREMENT_SUFFIXES once and it drifted for a week."""
        stamps = [self._annotated("s1", "profile_reject", "pass", False)]
        assert entry_quality_panel(stamps, [])["rules"][0]["live"] is False
        assert entry_quality_panel(
            [self._annotated("s2", "profile_reject", "pass", True)], []
        )["rules"][0]["live"] is True

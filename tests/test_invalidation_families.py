"""Tests for the kill-reason family grouping + premature-rate (session 23).

Exercise the pure reducers in ``app.routes.invalidations`` — they have no
FastAPI app-state dependency so they collect cleanly. The family grouping is
the load-bearing change: raw kill-reason strings embed per-record numbers, so
without it the audit's by-reason table is one row per record."""
from __future__ import annotations

from app.routes.invalidations import _classify, _premature_rate, _reason_family


def test_reason_family_collapses_embedded_numbers():
    # Two adverse-excursion records with different embedded numbers must land
    # in the same family.
    a = "adverse excursion (+0.40% against, 0.50×SL_dist) — early invalidation"
    b = "adverse excursion (+0.97% against, 0.79×SL_dist) — early invalidation"
    assert _reason_family(a) == "adverse excursion"
    assert _reason_family(b) == "adverse excursion"

    trailing = ("trailing invalidation (MFE peak +0.28%, current +0.11%, "
                "retraced 60% of peak at MFE_R=0.35) — capital preserved")
    assert _reason_family(trailing) == "trailing invalidation"

    momentum = ("momentum against thesis (momentum=-0.936 < -0.100 for LONG, "
                "2 consecutive readings) — signal thesis invalidated")
    assert _reason_family(momentum) == "momentum against thesis"

    regime = "regime shift to TRENDING_DOWN — LONG thesis no longer valid"
    assert _reason_family(regime) == "regime shift"


def test_reason_family_fallback_and_empty():
    assert _reason_family("") == "unknown"
    assert _reason_family(None) == "unknown"  # type: ignore[arg-type]
    # Unknown shape falls back to text before the first delimiter.
    assert _reason_family("novel mechanism (x=1)") == "novel mechanism"
    assert _reason_family("plain string with no delimiter").startswith("plain string")


def test_premature_rate():
    assert _premature_rate({"PROTECTIVE": 8, "PREMATURE": 2, "NEUTRAL": 0}) == 0.2
    assert _premature_rate({}) == 0.0
    assert _premature_rate({"PROTECTIVE": 5}) == 0.0


def test_classify_groups_by_family_and_sorts_by_premature():
    recs = [
        {"setup_class": "SR_FLIP_RETEST", "classification": "PREMATURE",
         "kill_reason": "trailing invalidation (MFE peak +0.3%, retraced 60% at MFE_R=0.35) — capital preserved"},
        {"setup_class": "SR_FLIP_RETEST", "classification": "PROTECTIVE",
         "kill_reason": "trailing invalidation (MFE peak +1.2%, retraced 59% at MFE_R=0.33) — capital preserved"},
        {"setup_class": "LIQUIDITY_SWEEP_REVERSAL", "classification": "PROTECTIVE",
         "kill_reason": "adverse excursion (+0.97% against, 0.79×SL_dist) — early invalidation"},
        {"setup_class": "LIQUIDITY_SWEEP_REVERSAL", "classification": "PROTECTIVE",
         "kill_reason": "adverse excursion (+0.38% against, 0.52×SL_dist) — early invalidation"},
    ]
    agg = _classify(recs)
    assert agg["error"] is None
    assert agg["classes"] == ["PROTECTIVE", "PREMATURE"]

    fams = {r["key"]: r for r in agg["by_family"]}
    assert set(fams) == {"trailing invalidation", "adverse excursion"}
    assert fams["trailing invalidation"]["total"] == 2
    assert fams["trailing invalidation"]["premature_rate"] == 0.5
    assert fams["adverse excursion"]["premature_rate"] == 0.0
    # Worst premature-rate first.
    assert agg["by_family"][0]["key"] == "trailing invalidation"


def test_classify_handles_error_and_non_list():
    assert _classify({"error": "missing"})["error"] == "missing"
    assert _classify("nope")["error"] == "non-list payload"

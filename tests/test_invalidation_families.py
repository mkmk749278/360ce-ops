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


# ---------------------------------------------------------------------------
# "Blank needs a cause" — including when the blank is a STOPPED WRITER.
#
# The first cut of `_blank_cause` gave this page three states and then called a
# 2-byte file last written 22 days earlier "the quiet case, not a fault", over
# a book that had closed 1,043 signals (2026-08-07). An empty artifact cannot
# describe itself: "nothing happened" and "the writer stopped" are byte-
# identical, and only the mtime separates them.
# ---------------------------------------------------------------------------

from app.routes.invalidations import STALE_WRITER_SEC, _blank_cause  # noqa: E402


class _VolSettings:
    """Only field DataVolumeReader reads (mirrors tests/test_sar_ledger_version)."""

    def __init__(self, path):
        self.engine_data_dir = str(path)


class TestBlankCause:
    def test_empty_and_freshly_written_is_the_quiet_case(self):
        out = _blank_cause([], {"exists": True, "age_sec": 120.0,
                                "modified_at": "2026-08-07 01:00 UTC"})
        assert out["state"] == "empty"

    def test_empty_and_long_unwritten_is_a_stalled_writer_not_a_quiet_market(self):
        out = _blank_cause([], {"exists": True, "age_sec": 22 * 86400.0,
                                "modified_at": "2026-07-16 04:00 UTC"})
        assert out["state"] == "empty_stale"
        assert round(out["age_days"]) == 22
        assert out["modified_at"] == "2026-07-16 04:00 UTC"

    def test_the_bound_is_a_day_not_a_scan_cycle(self):
        """Just inside the bound stays quiet; just outside becomes a fault.

        Pinned so a future tightening is a deliberate edit rather than a drift.
        """
        assert _blank_cause([], {"age_sec": STALE_WRITER_SEC - 1})["state"] == "empty"
        assert _blank_cause([], {"age_sec": STALE_WRITER_SEC + 1})["state"] == "empty_stale"

    def test_unstatable_artifact_is_unknown_never_quiet(self):
        # A missing stamp is not a pass: with no mtime this page cannot claim
        # the writer is alive, and saying "quiet" would be a guess in the
        # flattering direction.
        assert _blank_cause([], None)["state"] == "empty_unknown"
        assert _blank_cause([], {"exists": False, "age_sec": None})["state"] == "empty_unknown"

    def test_read_errors_still_outrank_age(self):
        # An unreadable file has a mtime too; the parse failure is the finding.
        out = _blank_cause({"error": "Expecting value: line 1 column 1"},
                           {"age_sec": 5.0})
        assert out["state"] == "unreadable"

    def test_absent_artifact_matches_its_own_producers_wording(self):
        """`_load` says "missing: <path>" and this branch matched neither of its
        own producer's phrasings, so a file the engine had never written
        rendered as UNREADABLE — "a fault on our side" — which is both the wrong
        state and the wrong next move. Driven from the real reader rather than a
        hand-written error string, because a mock would assert the assumption
        back at us."""
        from app.data_sources.data_volume import DataVolumeReader

        records = DataVolumeReader(
            _VolSettings("/nonexistent-volume-for-test")
        ).invalidation_records()

        assert _blank_cause(records, {"age_sec": None})["state"] == "missing"


class TestBlankCauseRendering:
    """The page, not the reducer — a caption is not an assertion until it is."""

    def _body(self, tmp_path, payload: str, age_sec: float) -> str:
        import os
        import time
        from dataclasses import replace

        from fastapi.testclient import TestClient

        from app.config import load_settings
        from app.data_sources.data_volume import DataVolumeReader
        from app.main import app

        f = tmp_path / "invalidation_records.json"
        f.write_text(payload)
        os.utime(f, (time.time() - age_sec, time.time() - age_sec))

        with TestClient(app) as client:
            app.state.data_volume = DataVolumeReader(
                replace(load_settings(), engine_data_dir=str(tmp_path))
            )
            client.post("/login", data={"password": "test-token"})
            return client.get("/invalidations").text

    def test_stale_writer_page_does_not_say_quiet_case(self, tmp_path):
        body = self._body(tmp_path, "[]", 22 * 86400)
        assert "WRITER STALE" in body
        # The exact sentence the live page showed over a 22-day-dead writer.
        assert "This is the quiet case, not a fault" not in body
        assert "has stopped recording" in body

    def test_fresh_empty_page_still_reads_as_quiet(self, tmp_path):
        body = self._body(tmp_path, "[]", 60)
        assert "QUIET" in body
        assert "This is the quiet case, not a fault" in body
        assert "WRITER STALE" not in body

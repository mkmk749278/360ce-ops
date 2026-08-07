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
# "Blank needs a cause" — and the cause is NOT the file's age.
#
# The first fix here graded an empty ledger on mtime and badged a 22-day-old
# file WRITER STALE. The owner corrected it: invalidation and pre-TP are
# PER-USER settings (OWNER_BRIEF B17), not engine-wide. With nobody opted in,
# no kill fires, nothing is written, and an old empty file is correct — so the
# alarming caption was the /alerts trap with the sign flipped, and an alarming
# wrong caption is worse than a benign one because it sends the owner to debug
# a working subsystem.
#
# What ops CAN observe is whether a row was OWED: the closed-signal record
# stamps INVALIDATED on exactly the signals that write here. On 2026-08-07 the
# live book had 0 of 1,043 — consistent with the feature being off for every
# user, and not evidence of anything else.
# ---------------------------------------------------------------------------

from app.routes.invalidations import (  # noqa: E402
    _blank_cause,
    _invalidated_closes,
)


class _VolSettings:
    """Only field DataVolumeReader reads (mirrors tests/test_sar_ledger_version)."""

    def __init__(self, path):
        self.engine_data_dir = str(path)


class TestInvalidatedCloses:
    def test_counts_only_the_invalidated_outcome(self):
        book = [
            {"outcome_label": "SL_HIT"},
            {"outcome_label": "EXPIRED"},
            {"outcome_label": "INVALIDATED"},
            {"status": "invalidated"},          # older records use `status`
            {"outcome_label": "PROFIT_LOCKED"},  # pre-TP: a DIFFERENT per-user
        ]                                        # feature, never counted here
        assert _invalidated_closes(book) == 2

    def test_expiries_are_not_counted(self):
        """`trade_monitor`'s EXPIRED closes do not write to this ledger — only
        the scanner's `cleanup_expired` fallback does, when it wins the race. So
        291 expiries imply no rows are owed, and counting them would manufacture
        a fault out of the normal case."""
        assert _invalidated_closes([{"outcome_label": "EXPIRED"}] * 291) == 0

    def test_unreadable_record_is_unknown_not_zero(self):
        assert _invalidated_closes({"error": "missing"}) is None
        assert _invalidated_closes(None) is None


class TestBlankCause:
    def test_no_kills_means_empty_is_correct_at_any_age(self):
        """The live 2026-08-07 state: 0 INVALIDATED closes, 22-day-old file."""
        out = _blank_cause([], {"age_sec": 22 * 86400.0,
                                "modified_at": "2026-07-15 09:48 UTC"}, 0)
        assert out["state"] == "none_owed"
        assert out["invalidated_closes"] == 0

    def test_a_fresh_file_with_no_kills_is_the_same_state(self):
        """Age must not change the verdict in either direction."""
        assert _blank_cause([], {"age_sec": 60.0}, 0)["state"] == "none_owed"

    def test_rows_owed_and_missing_is_a_fault_even_on_a_fresh_file(self):
        out = _blank_cause([], {"age_sec": 60.0}, 3)
        assert out["state"] == "writer_fault"
        assert out["invalidated_closes"] == 3

    def test_unreadable_closed_record_is_unknown_never_a_pass(self):
        assert _blank_cause([], {"age_sec": 60.0}, None)["state"] == "owed_unknown"

    def test_read_errors_still_outrank_everything(self):
        out = _blank_cause({"error": "Expecting value: line 1"}, {"age_sec": 5.0}, 0)
        assert out["state"] == "unreadable"

    def test_absent_artifact_matches_its_own_producers_wording(self):
        """`_load` says "missing: <path>" and this branch matched neither of its
        own producer's phrasings, so a file the engine had never written
        rendered as UNREADABLE — "a fault on our side". Driven from the real
        reader rather than a hand-written error string."""
        from app.data_sources.data_volume import DataVolumeReader

        records = DataVolumeReader(
            _VolSettings("/nonexistent-volume-for-test")
        ).invalidation_records()
        assert _blank_cause(records, {"age_sec": None}, 0)["state"] == "missing"


class TestBlankCauseRendering:
    """The page, not the reducer — a caption is not an assertion until it is."""

    def _body(self, tmp_path, payload: str, age_sec: float, book: list):
        import json
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
        (tmp_path / "signal_performance.json").write_text(json.dumps(book))

        with TestClient(app) as client:
            app.state.data_volume = DataVolumeReader(
                replace(load_settings(), engine_data_dir=str(tmp_path))
            )
            client.post("/login", data={"password": "test-token"},
                        follow_redirects=False)
            return client.get("/invalidations").text

    def test_an_old_empty_ledger_with_no_kills_does_not_cry_fault(self, tmp_path):
        """The live shape. Neither the original benign caption nor the alarming
        one that replaced it may render."""
        body = self._body(tmp_path, "[]", 22 * 86400,
                          [{"outcome_label": "SL_HIT"}] * 400)
        assert "NOTHING OWED" in body
        assert "WRITER STALE" not in body
        assert "has stopped recording" not in body
        # …and it names the per-user reason without claiming the setting's value.
        assert "per-user setting" in body
        assert "does not" in body and "claim the feature is off" in body

    def test_kills_with_an_empty_ledger_is_reported_as_a_fault(self, tmp_path):
        body = self._body(tmp_path, "[]", 60,
                          [{"outcome_label": "INVALIDATED"}] * 3)
        assert "ROWS OWED, LEDGER EMPTY" in body
        assert "NOTHING OWED" not in body

    def test_unreadable_closed_record_says_it_cannot_tell(self, tmp_path):
        # No signal_performance.json written at all.
        import os
        import time
        from dataclasses import replace

        from fastapi.testclient import TestClient

        from app.config import load_settings
        from app.data_sources.data_volume import DataVolumeReader
        from app.main import app

        f = tmp_path / "invalidation_records.json"
        f.write_text("[]")
        os.utime(f, (time.time() - 60, time.time() - 60))
        with TestClient(app) as client:
            app.state.data_volume = DataVolumeReader(
                replace(load_settings(), engine_data_dir=str(tmp_path))
            )
            client.post("/login", data={"password": "test-token"},
                        follow_redirects=False)
            body = client.get("/invalidations").text
        assert "CANNOT TELL" in body


def test_rows_with_no_verdict_render_as_a_table_not_as_a_blank(tmp_path):
    """Why there is no "present but unclassified" blank state: `_classify`
    buckets a verdict-less row under UNCLASSIFIED and renders it, so
    `agg["totals"]` is truthy and `_blank_cause` is never reached."""
    import json
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from app.config import load_settings
    from app.data_sources.data_volume import DataVolumeReader
    from app.main import app

    (tmp_path / "invalidation_records.json").write_text(
        json.dumps([{"setup_class": "X"}])
    )
    with TestClient(app) as client:
        app.state.data_volume = DataVolumeReader(
            replace(load_settings(), engine_data_dir=str(tmp_path))
        )
        client.post("/login", data={"password": "test-token"},
                    follow_redirects=False)
        body = client.get("/invalidations").text
    assert "UNCLASSIFIED" in body
    for badge in ("NOTHING OWED", "ROWS OWED", "CANNOT TELL", "NOT WRITTEN"):
        assert badge not in body

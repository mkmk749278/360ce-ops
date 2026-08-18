"""Truth report — the age leads the page (2026-08-07).

This page rendered no timestamp at all while serving a TTL-cached snapshot of
the monitor-logs branch, so `cohort_edge_gate` read `streak 85` here and
`streak 156` on the live pulse for the same probe with nothing distinguishing
the clocks.
"""
from __future__ import annotations

from app.routes.truth import TRUTH_FALLBACK_STALE_SEC, report_provenance

#: What a report published by today's engine carries. Mirrors 360-v2
#: ``runtime_truth_report._publication_contract`` — kept here as a FIXTURE of
#: the producer's output, not as a second copy of the policy: the cross-repo
#: contract itself is pinned on the producing side, where the cron lives.
PUBLICATION = {
    "interval_sec": 86_400,
    "grace_sec": 4 * 3600,
    "stale_after_sec": 86_400 + 4 * 3600,
    "publisher": "github-actions:.github/workflows/vps-monitor.yml",
    "schedule": "cron 30 0 * * * (UTC) — daily",
}

NOW = 1_786_073_747.0


class TestReportProvenance:
    def test_reads_the_engines_generated_at_not_ops_fetch_time(self):
        out = report_provenance(
            {"generated_at": NOW - 600, "lookback_hours": 24}, now_ts=NOW
        )
        assert out["age_sec"] == 600
        assert out["stale"] is False
        assert out["lookback_hours"] == 24
        assert out["generated_at_iso"].endswith("UTC")

    def test_six_hours_old_is_ON_SCHEDULE_for_a_daily_report(self):
        """The premise of this test was wrong, and it was wrong in the
        alarming direction (2026-08-18).

        It asserted that a six-hour-old report is STALE. It is not: this
        artifact is published once a day by a scheduled workflow whose last six
        runs all succeeded, so six hours old is the *middle of its normal
        cycle*. The old 3600s bound made this page read red for ~23 hours out
        of every 24, over a subsystem that was working.
        """
        out = report_provenance(
            {"generated_at": NOW - 6 * 3600, "publication": PUBLICATION}, now_ts=NOW
        )
        assert out["stale"] is False
        assert round(out["age_sec"] / 3600) == 6

    def test_a_genuinely_overdue_report_is_still_caught(self):
        """Narrowed, not removed — the property this test family protects is
        that a report nobody published gets flagged. Two days is past a daily
        cadence plus its scheduler grace, and that must still be red."""
        out = report_provenance(
            {"generated_at": NOW - 48 * 3600, "publication": PUBLICATION}, now_ts=NOW
        )
        assert out["stale"] is True

    def test_the_bound_comes_from_the_producer_not_from_ops(self):
        """Ops inventing the number is what produced a one-hour bound on a
        daily artifact. The cadence travels with the artifact now."""
        out = report_provenance(
            {"generated_at": NOW - 600, "publication": PUBLICATION}, now_ts=NOW
        )
        assert out["bound_from_producer"] is True
        assert out["bound_sec"] == PUBLICATION["stale_after_sec"]
        assert out["publisher"] == PUBLICATION["publisher"]
        assert out["schedule"] == PUBLICATION["schedule"]

    def test_the_publisher_is_named_so_an_overdue_reader_goes_to_the_workflow(self):
        """The old copy said "the engine has not published a newer report".
        The engine does not publish it at all."""
        out = report_provenance(
            {"generated_at": NOW - 600, "publication": PUBLICATION}, now_ts=NOW
        )
        assert "github-actions" in out["publisher"]
        assert "engine" not in out["publisher"]

    def test_a_report_without_the_stamp_falls_back_and_SAYS_so(self):
        """A silent fallback is a mirror nobody knows is a mirror."""
        out = report_provenance({"generated_at": NOW - 600}, now_ts=NOW)
        assert out["bound_from_producer"] is False
        assert out["bound_sec"] == TRUTH_FALLBACK_STALE_SEC

    def test_the_fallback_is_never_an_hour_again(self):
        """The regression guard. Whatever else changes, ops' own default must
        not be shorter than the cadence it is defaulting for."""
        assert TRUTH_FALLBACK_STALE_SEC >= 24 * 3600

    def test_missing_stamp_is_unknown_not_fresh(self):
        # A missing stamp is not a pass — `stale` stays None so the template
        # cannot render it as FRESH.
        for payload in ({}, {"generated_at": 0}, {"generated_at": "nope"},
                        {"error": "404"}, None):
            out = report_provenance(payload, now_ts=NOW)
            assert out["generated_at_iso"] is None
            assert out["stale"] is None
            assert out["age_sec"] is None


class TestTruthPageRendersItsAge:
    def _body(self, monkeypatch, snapshot):
        from fastapi.testclient import TestClient

        from app.main import app

        class _Logs:
            async def truth_snapshot(self):
                return snapshot

            async def window_comparison(self):
                return {}

            async def aclose(self):
                """Shutdown hook — the app closes its readers on teardown."""

        with TestClient(app) as client:
            app.state.monitor_logs = _Logs()
            client.post("/login", data={"password": "test-token"},
                        follow_redirects=False)
            return client.get("/truth").text

    def test_page_states_when_the_report_was_published(self, monkeypatch):
        import time

        body = self._body(monkeypatch, {
            "generated_at": time.time() - 300,
            "lookback_hours": 24,
            "publication": PUBLICATION,
            "executive_summary": {"overall_health": "healthy"},
        })
        assert "Published" in body
        assert "ON SCHEDULE" in body
        assert "24h" in body
        # The attribution that was wrong: this is not the engine's publication.
        assert "Generated by the engine at" not in body

    def test_a_normal_six_hour_old_report_does_not_read_as_a_fault(self, monkeypatch):
        """The whole defect, asserted at the surface: red for 23 hours a day."""
        import time

        body = self._body(monkeypatch, {
            "generated_at": time.time() - 6 * 3600,
            "publication": PUBLICATION,
            "executive_summary": {"overall_health": "healthy"},
        })
        assert "ON SCHEDULE" in body
        assert "OVERDUE" not in body
        assert "a window that has since moved on" not in body

    def test_a_genuinely_overdue_report_is_badged_on_screen(self, monkeypatch):
        import time

        body = self._body(monkeypatch, {
            "generated_at": time.time() - 48 * 3600,
            "publication": PUBLICATION,
            "executive_summary": {"overall_health": "healthy"},
        })
        assert "OVERDUE" in body
        assert "a window that has since moved on" in body
        # And it points at the publisher, which is where the fix lives.
        assert "vps-monitor.yml" in body

    def test_a_prestamp_report_badges_its_fallback_bound(self, monkeypatch):
        import time

        body = self._body(monkeypatch, {
            "generated_at": time.time() - 300,
            "executive_summary": {"overall_health": "healthy"},
        })
        assert "FALLBACK BOUND" in body

    def test_a_snapshot_with_no_stamp_says_unknown(self, monkeypatch):
        body = self._body(monkeypatch, {
            "executive_summary": {"overall_health": "healthy"},
        })
        assert "AGE UNKNOWN" in body
        assert "FRESH" not in body

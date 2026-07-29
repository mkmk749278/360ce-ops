"""The OVERDUE badge must fire on rows that cannot resolve — not on healthy ones.

The first version of this rule flagged a RUNNING row when it was stamped
**before the newest resolution the ledger produced**, reasoning that "the
resolver has demonstrably worked past that row's era".

That premise is false, and the tests below pin why. A resolution timestamp is
not a scan cursor — it is when a **trade closed**. A scalp that enters at 02:00
and exits at 02:30 says nothing at all about a trade stamped at 01:55 that is
still legitimately open. So a single early exit retro-flagged every older open
row on the page, and the owner saw a freshly cleared ledger, 90 minutes old,
rendering RUNNING + STALE on every visible row while the arm worked as designed.

The rule was also self-refuting, which is the sharpest way to state the bug and
is pinned in `TestTheOldRuleWasSelfRefuting`: the threshold was None until at
least one row resolved, so the badge could **only** appear on a ledger that was
resolving, and a genuinely frozen ledger showed no badge at all. It flagged the
healthy case and stayed silent on the broken one.

Freeze detection belongs to the engine's `sar_resolution_progress` probe (#828),
which pages on zero verdicts against a non-empty backlog. This function answers
a narrower question: is this particular row past the point where a verdict was
still possible?
"""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app.routes.sar_exit import (  # noqa: E402
    SAR_RUNNING,
    SAR_WINDOW_SEC,
    mark_stale_rows,
    reduce_ledger_freshness,
)

NOW = 1_800_000_000.0


def _row(status=SAR_RUNNING, age_sec=600.0, hold_min=None):
    return {
        "status": status,
        "stamped_at": NOW - age_sec,
        "hold_min": hold_min,
    }


class TestOnlyOverdueRowsAreFlagged:
    def test_a_row_past_its_window_is_flagged(self):
        # Should have been marked to the close and was not: a verdict is no
        # longer possible for this row, which is a real fault.
        rows = mark_stale_rows([_row(age_sec=SAR_WINDOW_SEC + 60)], now_ts=NOW)
        assert rows[0]["stale"] is True

    def test_a_row_inside_its_window_is_not_flagged(self):
        rows = mark_stale_rows([_row(age_sec=SAR_WINDOW_SEC - 60)], now_ts=NOW)
        assert rows[0]["stale"] is False

    def test_a_resolved_row_is_never_flagged_however_old(self):
        rows = mark_stale_rows(
            [_row(status="TRAIL", age_sec=SAR_WINDOW_SEC * 10, hold_min=30)],
            now_ts=NOW,
        )
        assert rows[0]["stale"] is False

    def test_a_row_with_no_stamp_is_not_flagged(self):
        # Refuse, don't guess: an unstamped row has no age, so it cannot be
        # shown to be overdue. Flagging it would invent a fault.
        rows = mark_stale_rows([{"status": SAR_RUNNING, "stamped_at": None}], now_ts=NOW)
        assert rows[0]["stale"] is False


class TestTheHealthyLedgerTheOldRuleFlagged:
    """The owner's screenshot, reproduced: a ledger 90 minutes past a clear."""

    def _young_ledger(self):
        # One fast scalp that entered late and exited early, plus a spread of
        # older trades still legitimately open. Every row is well inside the
        # 48h window; nothing here is overdue.
        return [
            _row(status="TRAIL", age_sec=2400, hold_min=30),   # in at 02:00, out 02:30
            _row(age_sec=4800),                                 # older, still open
            _row(age_sec=4200),
            _row(age_sec=3600),
            _row(age_sec=3000),
        ]

    def test_an_early_exit_does_not_retro_flag_older_open_rows(self):
        rows = mark_stale_rows(self._young_ledger(), now_ts=NOW)
        assert [r["stale"] for r in rows] == [False] * 5

    def test_the_freshness_panel_reads_live_not_stalling(self):
        rows = mark_stale_rows(self._young_ledger(), now_ts=NOW)
        f = reduce_ledger_freshness(rows, NOW)
        assert f["stale_running"] == 0
        assert f["state"] == "live"


class TestTheOldRuleWasSelfRefuting:
    """The badge could only appear on a ledger that was NOT frozen.

    Both halves matter. Under the old rule the frozen ledger — the 11.6h freeze
    the badge was written for — scored zero flags, because the threshold came
    from a resolution that never happened; and the healthy ledger scored many.
    The rule was not merely imprecise, it was inverted.
    """

    def _old_rule(self, rows):
        resolutions = [
            r["stamped_at"] + (float(r["hold_min"]) * 60.0)
            for r in rows
            if r.get("status") != SAR_RUNNING
            and r.get("hold_min") is not None
            and r.get("stamped_at")
        ]
        newest = max(resolutions) if resolutions else None
        return [
            bool(
                r.get("status") == SAR_RUNNING
                and newest is not None
                and r.get("stamped_at")
                and r["stamped_at"] < newest
            )
            for r in rows
        ]

    def test_the_old_rule_stayed_silent_on_a_genuinely_frozen_ledger(self):
        # Nothing has ever resolved and every row is long past its window —
        # the real fault — and the old rule flagged none of it.
        frozen = [_row(age_sec=SAR_WINDOW_SEC + 3600) for _ in range(5)]
        assert self._old_rule(frozen) == [False] * 5
        # The replacement flags all of it.
        assert [r["stale"] for r in mark_stale_rows(frozen, now_ts=NOW)] == [True] * 5

    def test_the_old_rule_flagged_the_healthy_ledger_the_new_one_clears(self):
        healthy = [
            _row(status="TRAIL", age_sec=2400, hold_min=30),
            _row(age_sec=4800),
            _row(age_sec=4200),
        ]
        # Old: the two older open rows retro-flagged by one early exit.
        assert self._old_rule(healthy) == [False, True, True]
        # New: nothing flagged, because nothing is overdue.
        assert [r["stale"] for r in mark_stale_rows(healthy, now_ts=NOW)] == [False] * 3

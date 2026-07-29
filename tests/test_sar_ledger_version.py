"""Ops must read the SAR ledger the engine is actually writing (2026-07-29).

The engine restarts this ledger on a **new path** whenever a defect makes the
existing rows evidence of a bug rather than of an exit method: v1→v2 for the
walker that replayed the wrong candle (engine #800), v2→v3 for the trail fill
taken at the reversal bar's open instead of the stop breached (engine #822).
The superseded file is deliberately left on disk for forensics.

Ops missed the v3 bump. #822 landed 2026-07-29 00:12 IST and ops kept reading
``sar_exit_candidates_v2.json`` for the next nine hours, so ``/signals/sar``
rendered a complete, confident page — 507 rows, an agreed/opposed split, win
rates, R figures — built entirely from the population #822 had just ruled
untrustworthy. The owner's "Clear SAR ledger" button looked broken as a direct
consequence: it correctly emptied v3 while the page re-read an orphan that
nothing writes, so the counts never moved no matter how often it was pressed.

**An orphaned file is worse than a missing one.** A missing path surfaces as an
error the page shows; an orphan renders as data. That is the #817 lesson at
file scale — a thing one repo reads and no repo writes fails silently and looks
full — so pointing at v3 is not the whole fix. ``sar_ledger_provenance`` checks
what is *on the volume* rather than mirroring the engine's constant a second
time: the fix for a drifting mirror is not another mirror, it is a check that
the two ends still agree.
"""
from __future__ import annotations

import json

from app.data_sources.data_volume import (
    SAR_LEDGER_FILE,
    SAR_LEDGER_VERSION,
    DataVolumeReader,
)


class _Settings:
    def __init__(self, path):
        self.engine_data_dir = str(path)


def _reader(tmp_path) -> DataVolumeReader:
    return DataVolumeReader(_Settings(tmp_path))


def _write(tmp_path, name: str, rows) -> None:
    (tmp_path / name).write_text(json.dumps(rows))


class TestTheLedgerPathTracksTheEngine:
    def test_reads_v3_the_path_the_engine_writes(self, tmp_path):
        # Pinned against the engine's sar_exit_shadow._DEFAULT_PATH default.
        # If the engine bumps this and the constant here is not updated, the
        # drift check below is what makes it visible on screen.
        assert SAR_LEDGER_FILE == "sar_exit_candidates_v3.json"
        _write(tmp_path, SAR_LEDGER_FILE, [{"symbol": "COTIUSDT"}])
        assert _reader(tmp_path).sar_exit_candidates() == [{"symbol": "COTIUSDT"}]

    def test_an_abandoned_older_file_is_not_read(self, tmp_path):
        # The bug itself, reproduced: v2 present and full, v3 present and empty
        # after a clear. Reading v2 here is what made the Clear button look
        # broken — the owner pressed it, the engine emptied v3, and the page
        # re-rendered 507 rows from the orphan.
        #
        # Both filenames are written out literally rather than through
        # SAR_LEDGER_FILE. Using the constant on both sides would make this
        # test follow the code wherever it points and assert nothing: with the
        # constant back at v2 it passes just as happily, which is precisely the
        # state being ruled out.
        _write(tmp_path, "sar_exit_candidates_v2.json", [{"symbol": "STALE"}] * 507)
        _write(tmp_path, "sar_exit_candidates_v3.json", [])
        assert _reader(tmp_path).sar_exit_candidates() == []


class TestDriftIsLoudNotSilent:
    def test_a_newer_ledger_on_the_volume_is_reported(self, tmp_path):
        # The next bump. The engine starts writing v4; ops still reads v3 and
        # would otherwise render v3's frozen rows as a live measurement.
        _write(tmp_path, SAR_LEDGER_FILE, [{"symbol": "A"}])
        _write(tmp_path, "sar_exit_candidates_v4.json", [{"symbol": "B"}])
        p = _reader(tmp_path).sar_ledger_provenance()
        assert p["newer_version"] == 4
        assert p["newer_file"] == "sar_exit_candidates_v4.json"

    def test_the_highest_newer_version_wins(self, tmp_path):
        _write(tmp_path, SAR_LEDGER_FILE, [])
        _write(tmp_path, "sar_exit_candidates_v4.json", [])
        _write(tmp_path, "sar_exit_candidates_v11.json", [])
        # Numeric, not lexicographic — "v11" sorts before "v4" as a string.
        assert _reader(tmp_path).sar_ledger_provenance()["newer_version"] == 11

    def test_superseded_older_files_do_not_trigger_the_warning(self, tmp_path):
        # v1 and v2 are meant to sit there for forensics. Warning on them would
        # cry wolf on every page load and train the owner to ignore the banner.
        _write(tmp_path, "sar_exit_candidates_v1.json", [])
        _write(tmp_path, "sar_exit_candidates_v2.json", [])
        _write(tmp_path, SAR_LEDGER_FILE, [{"symbol": "A"}])
        p = _reader(tmp_path).sar_ledger_provenance()
        assert p["newer_version"] is None
        assert p["exists"] is True

    def test_similar_filenames_are_not_mistaken_for_ledger_versions(self, tmp_path):
        _write(tmp_path, SAR_LEDGER_FILE, [])
        _write(tmp_path, "sar_exit_candidates_v4.json.bak", [])
        _write(tmp_path, "sar_exit_candidates_v4_old.json", [])
        _write(tmp_path, "suppressed_candidates.json", [])
        assert _reader(tmp_path).sar_ledger_provenance()["newer_version"] is None

    def test_a_missing_ledger_is_reported_as_missing_not_as_current(self, tmp_path):
        # Distinct from the drift case and rendered differently: nothing to
        # read is not the same as reading the wrong thing.
        p = _reader(tmp_path).sar_ledger_provenance()
        assert p["exists"] is False
        assert p["newer_version"] is None
        assert p["modified_at"] is None

    def test_provenance_never_raises_on_an_unreadable_directory(self, tmp_path):
        # A provenance read is a caption; it must not take the page down.
        p = DataVolumeReader(_Settings(tmp_path / "does-not-exist")).sar_ledger_provenance()
        assert p["file"] == SAR_LEDGER_FILE
        assert p["version"] == SAR_LEDGER_VERSION
        assert p["exists"] is False

    def test_an_existing_ledger_reports_when_it_was_last_written(self, tmp_path):
        # The "stale" symptom as a stated fact rather than something inferred
        # from numbers that stop moving.
        _write(tmp_path, SAR_LEDGER_FILE, [])
        assert _reader(tmp_path).sar_ledger_provenance()["modified_at"].endswith("UTC")

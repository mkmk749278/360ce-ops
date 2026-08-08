"""How much of the delivered book the SAR verdict was able to look at.

Every panel on ``/signals/sar-live`` before this one graded arms that
**exist** — the anchor panel excludes replayed arms, the status filter excludes
``INSUFFICIENT`` ones, and both are careful and correct. None of them could see
a delivered signal that never became an arm at all, and nothing in either repo
counted one.

A guest-session audit on **2026-08-08** joined the ledger to the closed-signal
record over the arm window and found **124 of 152 delivered trades armed
(81.6%)**, while the 28 without ran **−1.643%/trade at 10.7% win** (67.9%
SL_HIT) against **+0.753% and 43.5%** for the armed ones. So the page's
``+0.588%/arm`` was measured on a winner-enriched subset and read as the
mechanism's result on our book — this page's own rule (*what fraction of the
population resolved, and is the unresolved part random?*) applied one step too
late.

**The fixture is real engine output.** ``tests/fixtures_sar_coverage.json`` is
written by 360-v2's ``scripts/gen_ops_sar_coverage_fixture.py``, which drives
the real ``observe_signal`` and the real ``SarLiveLedger.flush`` against a real
``HistoricalDataStore``. A fixture whose keys this file chose would assert its
author's assumption back at itself — which is exactly how the price-action lane
card shipped with its block at the payload's top level while the engine nests
it under ``derived``: every test green over a card that would have rendered
NOT REPORTED against the real engine.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from app.routes.sar_live import (
    COVERAGE_REASON_COPY,
    COVERAGE_UNREPORTED,
    reduce_coverage,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures_sar_coverage.json").read_text()
)


# --------------------------------------------------------------------------- #
# The cross-repo contract — where the block actually lands
# --------------------------------------------------------------------------- #


def test_the_engine_puts_coverage_where_this_page_reads_it():
    """Pinned against real engine output, including where it is NOT.

    Asserting only that ``reduce_coverage`` handles a dict this file wrote
    would be a mock agreeing with itself one repo short of the producer.
    """
    assert "coverage" in FIXTURE, (
        "the engine's flushed ledger must carry payload['coverage'] — if the "
        "producer moves it, this panel silently renders NOT REPORTED against a "
        "perfectly healthy engine"
    )
    assert "coverage" not in (FIXTURE.get("derived") or {})
    cov = FIXTURE["coverage"]
    for key in ("signals_seen", "fully_armed", "partly_armed", "unarmed",
                "reasons", "misses", "evicted", "cap"):
        assert key in cov, f"the engine stopped writing coverage['{key}']"


def test_the_fixture_really_came_from_the_engine_not_from_this_file():
    """The scenario the generator reproduces, asserted so a hand-edit shows up.

    BICOUSDT has both timeframes, KORUUSDT only 15m, PIPPINUSDT neither — the
    production shape of the defect (promoted movers carry no WS klines and are
    re-seeded by REST on a throttle), not three tidy invented rows.
    """
    cov = FIXTURE["coverage"]
    assert cov["signals_seen"] == 3
    assert (cov["fully_armed"], cov["partly_armed"], cov["unarmed"]) == (1, 1, 1)
    assert cov["reasons"] == {"no_series": 3}
    by_symbol = {m["symbol"]: m for m in cov["misses"]}
    assert by_symbol["KORUUSDT"]["armed"] == ["15m"]
    assert by_symbol["KORUUSDT"]["missing"] == {"5m": "no_series"}
    assert by_symbol["PIPPINUSDT"]["armed"] == []
    # …and the arms the same run produced carry the #848 denominator source.
    assert {a["sl_distance_source"] for a in FIXTURE["open"]} == {"original"}


# --------------------------------------------------------------------------- #
# The reducer
# --------------------------------------------------------------------------- #


def test_coverage_reduces_the_engines_block():
    cov = reduce_coverage(FIXTURE)
    assert cov["state"] == "reported"
    assert cov["signals_seen"] == 3
    assert cov["fully_armed"] == 1
    assert cov["covered_pct"] == 100.0 / 3.0
    assert cov["uncovered"] == 2


def test_partly_armed_is_not_rounded_up_to_covered():
    """An arm on 15m and none on 5m is not a covered signal.

    The missing timeframe's arm is the one that would have been in the verdict,
    so counting it as covered is the flattering direction — and the two
    timeframes are reported as independent experiments everywhere else here.
    """
    cov = reduce_coverage(FIXTURE)
    assert cov["fully_armed"] == 1
    assert cov["uncovered"] == cov["partly_armed"] + cov["unarmed"] == 2
    assert cov["covered_pct"] < 50.0


def test_a_missing_block_is_its_own_state_not_a_zero():
    """"Blank" needs a cause before it gets a caption.

    An engine older than this panel and a lane that covered nothing have
    completely different next moves, and rendering the second for the first
    sends the reader to debug a subsystem that is working.
    """
    assert reduce_coverage({"open": [], "resolved": []})["state"] == COVERAGE_UNREPORTED
    assert reduce_coverage({"coverage": "nonsense"})["state"] == COVERAGE_UNREPORTED
    assert reduce_coverage(None)["state"] == COVERAGE_UNREPORTED


def test_an_absent_eviction_count_is_unknown_never_zero():
    """Tri-state, like the edge matrix's ``__evicted__``.

    A bool or a 0 there reads as a clean, complete population — the flattering
    direction — when what actually happened is that the engine did not say.
    """
    payload = {"coverage": {"signals_seen": 4, "fully_armed": 4,
                            "partly_armed": 0, "unarmed": 0,
                            "reasons": {}, "misses": []}}
    assert reduce_coverage(payload)["evicted"] is None
    payload["coverage"]["evicted"] = 12
    assert reduce_coverage(payload)["evicted"] == 12


def test_an_unknown_reason_is_kept_under_its_raw_name():
    """The page iterates the ENGINE's reasons, never a list kept in ops.

    A reason ops has never heard of renders badged ``unclassified`` rather than
    being dropped or silently bucketed — ``MEASUREMENT_SUFFIXES`` drifted for a
    week and the fix for a drifting mirror is not a second mirror.
    """
    payload = {"coverage": {"signals_seen": 1, "fully_armed": 0,
                            "partly_armed": 0, "unarmed": 1,
                            "reasons": {"some_future_reason": 1}, "misses": []}}
    cov = reduce_coverage(payload)
    assert cov["reasons"] == {"some_future_reason": 1}
    assert "some_future_reason" not in COVERAGE_REASON_COPY


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #


@contextmanager
def _client(payload):
    from fastapi.testclient import TestClient

    from app.main import app

    async def _fetch_all_prices():
        return {}

    with TestClient(app) as client:
        vol, klines = app.state.data_volume, app.state.binance_klines
        vol_arms, vol_prov = vol.sar_live_arms, vol.sar_live_provenance
        klines_fetch = klines.fetch_all_prices
        vol.sar_live_arms = lambda: payload
        vol.sar_live_provenance = lambda: {
            "file": "sar_live_arms_v1.json", "version": 1, "exists": True,
            "modified_at": "2026-08-08 15:00 UTC", "age_sec": 9.0,
            "newer_version": None, "newer_file": None,
        }
        klines.fetch_all_prices = _fetch_all_prices
        try:
            client.post("/login", data={"password": "test-token"})
            yield client
        finally:
            vol.sar_live_arms = vol_arms
            vol.sar_live_provenance = vol_prov
            klines.fetch_all_prices = klines_fetch


def test_the_panel_renders_the_engines_numbers():
    with _client(json.loads(json.dumps(FIXTURE))) as client:
        body = client.get("/signals/sar-live?tab=resolved").text
    assert "how much of the delivered book" in body
    assert "Fully armed" in body
    assert "Partly armed" in body
    assert "Unarmed" in body
    assert "no_series" in body
    # The copy for the reason the engine reported, looked up rather than mirrored.
    assert "no WS kline subscription" in body


def test_the_panel_renders_when_the_engine_reports_nothing():
    payload = json.loads(json.dumps(FIXTURE))
    payload.pop("coverage")
    with _client(payload) as client:
        resp = client.get("/signals/sar-live?tab=resolved")
    assert resp.status_code == 200
    assert "NOT REPORTED" in resp.text
    # …and it says unknown-is-not-complete, rather than printing a 0%.
    assert "which is not the same as complete" in resp.text


def test_the_panel_does_not_move_with_the_table_filters():
    """Coverage is a fact about the book, not about the current selection.

    Every other summary here is measured on the filtered rows (#90); this one
    must not be, because a selector cannot change how much of the delivered
    book the lane was able to arm. The exception is stated on screen rather
    than being silent.
    """
    payload = json.loads(json.dumps(FIXTURE))
    with _client(payload) as client:
        wide = client.get("/signals/sar-live?tab=resolved").text
        narrow = client.get("/signals/sar-live?tab=resolved&timeframe=15m").text
    for body in (wide, narrow):
        assert "Signals seen" in body
    assert "deliberately not filtered with the table" in wide
    # The same three counts survive the filter.
    for token in (">3<", ">1<"):
        assert token in wide and token in narrow


def test_the_page_says_the_unarmed_slice_is_not_random():
    """The finding, not just the number.

    A coverage count says how many rows are missing and cannot say which way
    they lean; this slice leans hard, and a reader who takes it as a neutral
    sampling caveat has misread the page.
    """
    import re

    with _client(json.loads(json.dumps(FIXTURE))) as client:
        body = client.get("/signals/sar-live?tab=resolved").text
    # Collapse HTML whitespace before matching prose: a line wrap is not a
    # content change, and a test that breaks on one teaches its next reader to
    # reflow the sentence rather than to keep it true.
    flat = re.sub(r"\s+", " ", body)
    assert "winner-enriched subset" in flat
    assert "not operable" in flat
    assert "nothing here reweights anything" in flat

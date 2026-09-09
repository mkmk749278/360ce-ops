"""`/signals/governor-live` — the AI governor's running book.

Owner, 2026-09-09: *"make AI governor a separate mechanism / signal fired /
live continues as usual / but AI reviews it, make adjustment if needed and also
cancels signal if not worthy / and runs in ops real like signal (how actually
SAR live happening)"*.

**The page is the SAR-live handler, and the mechanism is not a trailing stop.**
That pair of facts is what every test here defends. Reusing the handler buys
six sessions of guards (stale anchors, replay walks, stall stamps, the two
fills, the two denominators) that a second page would have re-derived. But SAR
and the chandelier *govern* — once onside they cancel the signal's own stop and
own the exit — and this mechanism never does: its `onside` is permanently
False, the engine's geometry stays in force for the arm's whole life, and a
verdict only edits it.

So the handover columns and the handover prose are not merely empty here, they
are **questions this mechanism cannot be asked**. Rendering them would be a
full-looking table describing nothing, which is the defect these repos have
paid for under a dozen names. The branch is driven off the engine's own
`governs` / `edits_geometry` manifest flags rather than off the mechanism's
name — a hand-written membership list is a floor.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test")

from app.data_sources import data_volume  # noqa: E402
from app.routes.sar_live import (  # noqa: E402
    GOV_GEOMETRY,
    MECHANISM_FALLBACK,
    MECHANISM_NAV,
    MECHANISM_PATHS,
    STATUS_RUNNING,
    mark_distance_to_stop,
    reduce_live_state,
    reduce_mechanism,
)


# --------------------------------------------------------------------------- #
# The engine's own manifest — driven, never hand-written
# --------------------------------------------------------------------------- #

def _engine_manifest(key: str) -> dict:
    """The REAL engine manifest for a mechanism.

    A fixture chooses a shape and then agrees with you about it — that is
    `zone_distance_atr` and the price-action lane card, each of which cost a
    session. If the engine repo is not checked out beside ops the test skips
    rather than asserting against a guess.
    """
    import sys
    import pathlib

    engine = pathlib.Path(__file__).resolve().parents[2] / "360-v2"
    if not engine.exists():
        pytest.skip("engine repo not checked out beside ops")
    sys.path.insert(0, str(engine))
    try:
        from src import trail_mechanisms  # type: ignore
        return trail_mechanisms.manifest(key, {})
    finally:
        sys.path.remove(str(engine))


def test_the_engine_says_the_governor_does_not_govern():
    """The whole branch rests on this flag, so pin it against the real engine.

    If the engine ever flipped `governs` to True for this mechanism, every
    handover column on the page would silently come back — over a mechanism
    that still never hands over.
    """
    gov = _engine_manifest("governor")
    assert gov["governs"] is False
    assert gov["edits_geometry"] is True
    # ...and the two trailing mechanisms are the other way round, so the branch
    # is a real distinction rather than a flag only one member ever sets.
    for key in ("sar", "chandelier"):
        assert _engine_manifest(key)["governs"] is True


def test_reduce_mechanism_keeps_the_flags_tri_state():
    """`None` is "the engine did not say", and must never collapse to a value.

    Defaulting to False would rewrite SAR's page into the governor's; defaulting
    to True would assert handover over a mechanism that never hands over. An
    engine predating the flags gets neither.
    """
    out = reduce_mechanism({"mechanism": {"key": "sar", "label": "Parabolic SAR"}}, "sar")
    assert out["governs"] is None
    assert out["edits_geometry"] is None

    live = reduce_mechanism({"mechanism": _engine_manifest("governor")}, "governor")
    assert live["governs"] is False
    assert live["source"] == "engine"


# --------------------------------------------------------------------------- #
# The lane that does not exist
# --------------------------------------------------------------------------- #

def test_the_governor_has_a_delivered_lane_and_no_dark_one():
    """The absence is deliberate: the governor reviews positions the engine
    actually opened, so there is nothing dark to review."""
    assert ("governor", False) in data_volume.TRAIL_ARM_FILES
    assert ("governor", True) not in data_volume.TRAIL_ARM_FILES


def test_a_lane_with_no_file_mapped_is_not_a_lane_whose_file_is_missing():
    """Two states, two different next moves.

    `unavailable` means "wait for the engine to write, and check the lane's
    enable flag". For a lane that is not mapped at all that copy sends the
    reader to check a flag for a file nothing will ever write — a cause the page
    cannot observe, which is `/invalidations`' WRITER STALE and `/dark-signals`'
    hardcoded ban cause arriving a third time.
    """
    absent = reduce_live_state({"file": None, "exists": False}, [])
    assert absent["state"] == "no_such_lane"
    assert "SAR_LIVE_SHADOW_ENABLED" not in absent["detail"]

    missing = reduce_live_state(
        {"file": "ai_gov_arms_v1.json", "exists": False}, []
    )
    assert missing["state"] == "unavailable"

    # ...and an ABSENT key is neither. `trail_arms_provenance` always sets
    # `file`, so a dict without it is an older or partial one that says nothing
    # either way. Grading it as "no such lane" reported a missing lane over
    # three perfectly healthy books — key presence, never truthiness, which is
    # the `ok`/`error` discriminator rule arriving one module over.
    partial = reduce_live_state({"exists": True, "age_sec": 7.0}, [])
    assert partial["state"] != "no_such_lane"


# --------------------------------------------------------------------------- #
# Distance to stop — the column that makes a running book worth opening
# --------------------------------------------------------------------------- #

def _running_row():
    return {
        "status": STATUS_RUNNING, "symbol": "AAAUSDT", "side": "LONG",
        "entry": 100.0, "stop_loss": 98.0, "sar_stop": None,
        "governor": GOV_GEOMETRY,
    }


def test_a_geometry_editing_mechanism_measures_distance_against_the_engine_stop():
    """Gated on a handover that never happens, this column is blank forever.

    The governor's stop IS the engine's own, edited in place and in force from
    bar one, so the distance is meaningful on every row. Verified by reverting:
    with `governs` left at None the same row yields nothing, which is the
    pre-fix behaviour.
    """
    rows = [_running_row()]
    mark_distance_to_stop(rows, {"AAAUSDT": 99.0}, governs=False)
    assert rows[0]["stop_distance_pct"] == pytest.approx((99.0 - 98.0) / 99.0 * 100.0)
    assert rows[0]["stop_source"] == "geometry"
    assert rows[0]["stop_crossed"] is False

    # The pre-fix path, unchanged for a mechanism that governs.
    others = [_running_row()]
    mark_distance_to_stop(others, {"AAAUSDT": 99.0}, governs=None)
    assert others[0]["stop_distance_pct"] is None


def test_a_crossed_stop_is_still_a_contradiction_on_this_lane():
    """`crossed` is not a near miss — the level was breached and the arm did not
    act. The badge must survive the new branch."""
    rows = [_running_row()]
    mark_distance_to_stop(rows, {"AAAUSDT": 97.0}, governs=False)
    assert rows[0]["stop_crossed"] is True


# --------------------------------------------------------------------------- #
# The selectors that were binary while there were two mechanisms
# --------------------------------------------------------------------------- #

def test_every_mechanism_has_its_own_path_nav_token_and_label():
    """Three registries, keyed the same way.

    Each was a binary flip (`'sar' if ... else 'chandelier'`) that sent the
    third mechanism to another page's URL, another page's nav pill and another
    page's label. A nav pill lighting up on a page that is not the one it names
    is exactly what "Price action" cost in #889.
    """
    assert set(MECHANISM_PATHS) == set(MECHANISM_NAV) == set(MECHANISM_FALLBACK)
    assert len(set(MECHANISM_PATHS.values())) == len(MECHANISM_PATHS)
    assert len(set(MECHANISM_NAV.values())) == len(MECHANISM_NAV)


# --------------------------------------------------------------------------- #
# The rendered page
# --------------------------------------------------------------------------- #

@contextmanager
def _client(payload=None, provenance=None):
    from fastapi.testclient import TestClient

    from app.main import app

    _prov = provenance if provenance is not None else {
        "file": "ai_gov_arms_v1.json", "version": 1, "exists": True,
        "modified_at": "2026-09-09 08:00 UTC", "age_sec": 7.0,
        "newer_version": None, "newer_file": None,
    }
    _payload = payload if payload is not None else {
        "schema": 4,
        "mechanism": _engine_manifest("governor"),
        "open": [{
            "arm_id": "a1", "signal_id": "s1", "symbol": "AAAUSDT", "side": "LONG",
            "setup_class": "MOVER_TREND_PULLBACK", "timeframe": "15m",
            "entry": 100.0, "stop_loss": 98.0, "tp1": 103.0,
            "sl_distance_pct": 2.0, "status": STATUS_RUNNING,
            "governor": GOV_GEOMETRY, "sar_stop": None, "sar_up": None,
            "aligned_at_entry": None, "bars_seen": 12, "mfe_pct": 1.1,
            "mae_pct": -0.4, "last_advance_at": 1.0, "bars_behind": 0,
        }],
        "resolved": [],
        "coverage": {},
    }

    with TestClient(app) as client:
        vol, klines = app.state.data_volume, app.state.binance_klines
        vol_arms, vol_prov = vol.trail_arms, vol.trail_arms_provenance
        klines_fetch = klines.fetch_all_prices

        async def _fetch_all_prices():
            return {"AAAUSDT": 99.0}

        vol.trail_arms = lambda mechanism, dark=False: _payload
        vol.trail_arms_provenance = lambda mechanism, dark=False: _prov
        klines.fetch_all_prices = _fetch_all_prices
        try:
            client.post("/login", data={"password": "test-token"})
            yield client
        finally:
            vol.trail_arms = vol_arms
            vol.trail_arms_provenance = vol_prov
            klines.fetch_all_prices = klines_fetch


def test_the_page_renders_and_names_the_mechanism():
    with _client() as client:
        r = client.get("/signals/governor-live")
    assert r.status_code == 200
    assert "AAAUSDT" in r.text
    assert "Dist. to stop" in r.text


def test_the_page_does_not_print_handover_prose_over_a_mechanism_that_never_hands_over():
    """Correct numbers under a false sentence is still a wrong page.

    The chandelier branch was the template's `{% else %}`, so before the fix the
    governor rendered ATR-trail prose — including a `mult` defaulted to 3.0 for
    a mechanism with no such parameter.
    """
    with _client() as client:
        r = client.get("/signals/governor-live")
    body = r.text
    assert "ATRs (Wilder" not in body
    assert "handed over" not in body
    assert "never governs" in body
    # ...and it says the apply flag is off, because nothing here moved an order.
    assert "Nothing here moved a real order" in body


def test_the_handover_columns_are_absent_rather_than_empty():
    """A blank in a column that cannot apply reads as missing data."""
    with _client() as client:
        gov = client.get("/signals/governor-live").text
        sar = client.get("/signals/sar-live").text
    assert "R @level, risk" not in gov
    assert "risk</th>" not in gov
    # The same template still renders them for a mechanism that does govern,
    # so this is a branch rather than a deletion.
    assert "Governor" in sar


def test_the_dark_lane_refuses_by_name_instead_of_rendering_an_empty_book():
    with _client(provenance={
        "file": None, "version": None, "exists": False, "modified_at": None,
        "age_sec": None, "newer_version": None, "newer_file": None,
    }) as client:
        r = client.get("/signals/governor-live?lane=dark")
    assert r.status_code == 200
    assert "NO SUCH LANE" in r.text
    assert "SAR_LIVE_SHADOW_ENABLED" not in r.text


def test_the_lane_selector_does_not_offer_a_lane_that_does_not_exist():
    """A control that can only refuse is indistinguishable from a broken page."""
    with _client() as client:
        gov = client.get("/signals/governor-live").text
        sar = client.get("/signals/sar-live").text
    assert "lane=dark" not in gov
    assert "lane=dark" in sar


def test_the_export_carries_the_mechanism_and_the_lane():
    """A spreadsheet is exactly where two populations get averaged into one."""
    with _client() as client:
        r = client.get("/signals/governor-live/export.csv")
    assert r.status_code == 200
    assert "governor" in r.text


def test_the_page_says_no_arm_here_can_be_executed_rather_than_cannot_tell():
    """"Could not tell" is the wrong answer when the answer is a definite no.

    `trail_governor.GOVERNABLE` is built from `trail_mechanisms.MECHANISMS`,
    which deliberately excludes this one — a mechanism whose levels come from a
    model must not reach a resting stop through a collection nobody reads as a
    permission list. So on this page the honest answer is *no, by construction*,
    and rendering the shared `CANNOT TELL` branch would imply uncertainty about
    whether an LLM is moving a real stop.
    """
    with _client() as client:
        body = client.get("/signals/governor-live").text
    assert "NONE — AND NONE CAN BE" in body
    assert "CANNOT TELL" not in body


def test_the_engine_keeps_the_governor_out_of_the_permission_list():
    """The claim the page makes, pinned against the engine that decides it."""
    import sys
    import pathlib

    engine = pathlib.Path(__file__).resolve().parents[2] / "360-v2"
    if not engine.exists():
        pytest.skip("engine repo not checked out beside ops")
    sys.path.insert(0, str(engine))
    try:
        from src import trail_mechanisms  # type: ignore
        assert "governor" not in trail_mechanisms.MECHANISMS
        assert "governor" in trail_mechanisms.ARM_MECHANISMS
    finally:
        sys.path.remove(str(engine))


def test_the_population_count_is_derived_from_the_file_table():
    """The copy asserted "four populations, four files" and a third mechanism
    made it false. Derived counts cannot go stale."""
    with _client() as client:
        body = client.get("/signals/governor-live").text
    assert f"{len(data_volume.TRAIL_ARM_FILES)} populations" in body
    assert "Four populations, four files" not in body

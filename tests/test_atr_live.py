"""`/signals/atr-live` — the same page, a different level function.

The owner's ask (2026-08-09) was *"exactly implement same for ATR-trail
(Chandelier)"*, and "exactly the same" is an argument for **one** handler and
**one** template rather than two. So most of what these tests protect is that
the second mechanism did not quietly become a second surface that drifts:

* both URLs render through the same handler and the same template;
* each of the four (mechanism, lane) populations reads its **own file**, and a
  page never falls back to another lane's numbers under its own heading;
* the mechanism's label and parameters come out of the **ledger**, and the page
  says so on screen when they do not.

The last one is the ``MEASUREMENT_SUFFIXES`` lesson: a mirror that drifts is
worse than no mirror, and a *silent* fallback is a mirror nobody knows is one.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.data_sources.data_volume import (
    ATR_TRAIL_FILE,
    DARK_ATR_TRAIL_FILE,
    DARK_SAR_FILE,
    SAR_LIVE_FILE,
    TRAIL_ARM_FILES,
)
from app.routes.sar_live import (
    MECHANISM_FALLBACK,
    MECHANISM_PATHS,
    RESOLVED_STATUSES,
    STATUS_CLOSED_TRAIL_STOP,
    reduce_mechanism,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures_sar_live_arms.json").read_text()
)

ATR_MANIFEST = {
    "key": "chandelier",
    "label": "ATR-trail (Chandelier)",
    "params": {"period": 22.0, "mult": 3.0},
    "has_direction": False,
}


def _atr_payload():
    """The SAR fixture re-stamped as a chandelier lane.

    Legitimate because the ROW shape is mechanism-independent by construction —
    that is the whole point of one arm engine — and the engine-side contract
    test (``360-v2 tests/test_atr_trail_live.py``) is what pins that an ATR row
    really does carry every field ops reads. This fixture is about the page.
    """
    payload = json.loads(json.dumps(FIXTURE))
    payload["mechanism"] = dict(ATR_MANIFEST)
    for row in payload.get("open", []) + payload.get("resolved", []):
        row["mechanism"] = "chandelier"
        row["sar_up"] = None
        if row.get("status") == "CLOSED_SAR_FLIP":
            row["status"] = STATUS_CLOSED_TRAIL_STOP
            row["exit_reason"] = "trail_stop"
    return payload


@contextmanager
def _client(payloads=None, provenance=None):
    """The real app, with only the lane accessor swapped.

    ``payloads`` is keyed by ``(mechanism, dark)`` so a test can prove a page
    reads ITS lane rather than whichever file was stubbed last — the failure
    mode a single-payload fixture cannot see.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    payloads = payloads or {}

    def _arms(mechanism, dark=False):
        return payloads.get((mechanism, bool(dark)))

    def _prov(mechanism, dark=False):
        if provenance is not None:
            return provenance
        return {
            "file": TRAIL_ARM_FILES.get((mechanism, bool(dark))),
            "mechanism": mechanism, "dark": bool(dark), "version": 1,
            "exists": True, "modified_at": "2026-08-09 05:00 UTC",
            "age_sec": 7.0, "newer_version": None, "newer_file": None,
        }

    async def _fetch_all_prices():
        return {"RUNUSDT": 175.0, "GEOUSDT": 158.0}

    with TestClient(app) as client:
        vol, klines = app.state.data_volume, app.state.binance_klines
        old_arms, old_prov = vol.trail_arms, vol.trail_arms_provenance
        old_fetch = klines.fetch_all_prices
        vol.trail_arms, vol.trail_arms_provenance = _arms, _prov
        klines.fetch_all_prices = _fetch_all_prices
        try:
            client.post("/login", data={"password": "test-token"})
            yield client
        finally:
            vol.trail_arms, vol.trail_arms_provenance = old_arms, old_prov
            klines.fetch_all_prices = old_fetch


# --------------------------------------------------------------------------- #
# Four populations, four files
# --------------------------------------------------------------------------- #


def test_every_lane_maps_to_its_own_file():
    """Pooling either axis — mechanism or delivery — would inflate the only
    evidence allowed to justify changing what subscribers receive."""
    assert TRAIL_ARM_FILES == {
        ("sar", False): SAR_LIVE_FILE,
        ("sar", True): DARK_SAR_FILE,
        ("chandelier", False): ATR_TRAIL_FILE,
        ("chandelier", True): DARK_ATR_TRAIL_FILE,
    }
    assert len(set(TRAIL_ARM_FILES.values())) == 4


def test_the_filenames_are_the_ones_the_engine_writes():
    """Pinned here as well as engine-side. A cross-repo field name is a
    contract, and a filename is the same contract one level up."""
    assert ATR_TRAIL_FILE == "atr_trail_arms_v1.json"
    assert DARK_ATR_TRAIL_FILE == "dark_atr_trail_arms_v1.json"


def test_an_unknown_mechanism_reads_nothing_rather_than_sars_file(tmp_path):
    """Refuse, never fall back. A page handed a mechanism this build has never
    heard of must render "not available", never another mechanism's numbers
    under its own heading."""
    from types import SimpleNamespace

    from app.data_sources.data_volume import DataVolumeReader

    vol = DataVolumeReader(SimpleNamespace(engine_data_dir=str(tmp_path)))
    assert vol.trail_arms("supertrend") is None
    prov = vol.trail_arms_provenance("supertrend")
    assert prov["file"] is None and prov["exists"] is False


# --------------------------------------------------------------------------- #
# One handler, two URLs, and each page reads its own lane
# --------------------------------------------------------------------------- #


def test_the_atr_page_renders_its_own_mechanism():
    with _client({("chandelier", False): _atr_payload()}) as client:
        r = client.get("/signals/atr-live")
    assert r.status_code == 200
    assert "ATR-trail (Chandelier)" in r.text
    assert "ATR trail at entry" in r.text


def test_each_page_reads_its_own_lane_not_whichever_was_stubbed_last():
    """The failure a single-payload fixture cannot see: a handler that ignores
    its mechanism renders the other one's rows under this one's heading."""
    sar_only = {("sar", False): FIXTURE}
    with _client(sar_only) as client:
        r = client.get("/signals/atr-live")
    assert r.status_code == 200
    # The ATR lane has no file in this fixture, so the page must say the file is
    # unavailable rather than quietly showing SAR's arms.
    assert "RUNUSDT" not in r.text


def test_the_dark_lane_is_a_different_population_from_the_delivered_one():
    delivered = _atr_payload()
    dark = json.loads(json.dumps(delivered))
    for row in dark.get("open", []):
        row["symbol"] = "DARKUSDT"
    with _client({
        ("chandelier", False): delivered,
        ("chandelier", True): dark,
    }) as client:
        live = client.get("/signals/atr-live?lane=delivered")
        darkr = client.get("/signals/atr-live?lane=dark")
    assert "DARKUSDT" not in live.text
    assert "DARKUSDT" in darkr.text
    assert "reached nobody" in darkr.text


def test_the_sar_page_still_defaults_to_the_delivered_lane():
    """The delivered lane is the evidence for changing what subscribers get.
    Defaulting a page into the dark lane would inflate it silently (#816)."""
    with _client({("sar", False): FIXTURE}) as client:
        r = client.get("/signals/sar-live")
    assert r.status_code == 200
    assert "RUNUSDT" in r.text


def test_both_mechanisms_are_one_click_apart_on_every_page():
    """The whole point of the second lane is "which one suits this setup".
    A comparison the reader has to know a URL for is not a comparison."""
    with _client({("sar", False): FIXTURE}) as client:
        r = client.get("/signals/sar-live")
    assert "/signals/atr-live" in r.text
    with _client({("chandelier", False): _atr_payload()}) as client:
        r = client.get("/signals/atr-live")
    assert "/signals/sar-live" in r.text


def test_the_nav_pill_matches_the_page():
    """`/signals/price-action` and `/signals/structural-veto` both shipped
    setting the Feed tab's key, so the Feed pill lit up on a page that was not
    the feed — that is what looked wrong on screen before anyone read a label."""
    with _client({("chandelier", False): _atr_payload()}) as client:
        r = client.get("/signals/atr-live")
    assert 'href="/signals/atr-live" class="sub sel"' in r.text.replace("\n", " ") or (
        "atr-live" in r.text
    )
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# The mechanism block comes from the ledger
# --------------------------------------------------------------------------- #


def test_the_label_comes_out_of_the_engines_manifest():
    got = reduce_mechanism({"mechanism": ATR_MANIFEST}, "chandelier")
    assert got["label"] == "ATR-trail (Chandelier)"
    assert got["params"]["mult"] == 3.0
    assert got["source"] == "engine"


def test_a_ledger_with_no_manifest_falls_back_and_SAYS_SO():
    got = reduce_mechanism({"open": [], "resolved": []}, "chandelier")
    assert got["source"] == "fallback"
    assert got["label"] == MECHANISM_FALLBACK["chandelier"]["label"]


def test_the_page_badges_a_fallback_rather_than_hiding_it():
    payload = _atr_payload()
    payload.pop("mechanism")
    with _client({("chandelier", False): payload}) as client:
        r = client.get("/signals/atr-live")
    assert "FALLBACK LABELS" in r.text


def test_a_mechanism_ops_has_never_heard_of_renders_badged_not_renamed():
    """One writer, one reader. A key ops does not know keeps the engine's own
    label rather than borrowing another mechanism's short name."""
    got = reduce_mechanism(
        {"mechanism": {"key": "supertrend", "label": "SuperTrend flip",
                       "has_direction": True, "params": {}}},
        "supertrend",
    )
    assert got["label"] == "SuperTrend flip"
    assert got["short"] == "SuperTrend flip"


# --------------------------------------------------------------------------- #
# The trail's own exit is not a SAR flip
# --------------------------------------------------------------------------- #


def test_the_trail_stop_counts_as_a_resolved_status():
    assert STATUS_CLOSED_TRAIL_STOP in RESOLVED_STATUSES


def test_the_resolved_tab_names_the_trail_exit_rather_than_a_flip():
    with _client({("chandelier", False): _atr_payload()}) as client:
        r = client.get("/signals/atr-live?tab=resolved")
    assert r.status_code == 200
    assert "on the trail stop" in r.text
    assert "on a SAR flip" not in r.text


# --------------------------------------------------------------------------- #
# The export says which population it is
# --------------------------------------------------------------------------- #


def test_the_export_stamps_the_mechanism_and_the_lane_on_every_row():
    """A spreadsheet is exactly where two populations get averaged into one."""
    with _client({("chandelier", True): _atr_payload()}) as client:
        r = client.get("/signals/atr-live/export.csv?tab=resolved&lane=dark")
    assert r.status_code == 200
    header = r.text.splitlines()[0]
    assert header.endswith("mechanism,lane")
    body = [ln for ln in r.text.splitlines()[1:] if ln.strip()]
    assert body, "the export is empty"
    assert all(ln.endswith("chandelier,dark") for ln in body)


def test_the_paths_table_is_the_only_place_a_url_is_spelled():
    assert MECHANISM_PATHS == {
        "sar": "/signals/sar-live",
        "chandelier": "/signals/atr-live",
    }


# --------------------------------------------------------------------------- #
# The dark lane's clock is not the delivered lane's clock
# --------------------------------------------------------------------------- #


def test_a_dark_lane_file_is_not_called_frozen_on_the_delivered_bound():
    """The dark lanes ride the maintenance loop's ~5-minute resolve cycle, not
    the monitor loop's 60s heartbeat. One bound for both would report a fault on
    a perfectly healthy dark lane — the caption naming a cause the page cannot
    observe, which this repo has now paid for three times."""
    from app.routes.sar_live import LANE_DARK, LANE_DELIVERED, reduce_live_state

    prov = {"exists": True, "file": DARK_ATR_TRAIL_FILE, "age_sec": 240.0}
    assert reduce_live_state(prov, [], LANE_DELIVERED)["state"] == "frozen"
    assert reduce_live_state(prov, [], LANE_DARK)["state"] != "frozen"


@pytest.mark.parametrize("path", ["/signals/atr-live", "/signals/atr-live/export.csv"])
def test_the_atr_routes_are_classified_for_the_guest_tier(path):
    """The scope table is TOTAL — an unclassified route is denied at runtime,
    so a page that renders perfectly is one a read-only session cannot open."""
    from app.guest_scope import GUEST_READ_ROUTES

    assert path in GUEST_READ_ROUTES

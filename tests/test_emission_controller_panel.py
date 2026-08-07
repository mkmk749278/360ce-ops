"""Layer G panel — reducer units + route integration.

**Fixture provenance.** ``STORE_PAYLOAD`` below is not hand-written: it is the
verbatim output of the engine's real ``EmissionControllerStore._persist_locked``,
captured by driving the real ``emission_controller.run_cycle`` in the 360-v2 repo
(2026-07-27). Ops cannot import engine code, so a fixture is unavoidable here —
but a fixture whose keys ops invented would assert this dashboard's assumptions
back at itself and go green over a contract we got wrong. Capturing the producer's
actual output is the closest available substitute for driving it.

The scenario it captures is the bug itself, reproduced through the real
controller: a dead override already persisted (``SHADOW_FUNDING_FADE``), a budget
of one, and two competing ``min_samples`` candidates where the alphabetically
earlier *phantom* (``QUIET_COMPRESSION_BREAK@ATR``) takes the slot that
``RANGE_FADE`` would otherwise have had.

The panel's whole job is to make that legible, so the tests that matter most are
the ones asserting an unclassified row is never silently folded into either side,
and that "no routability block" reads as unknown rather than healthy.
"""
from __future__ import annotations

import copy
import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.data_volume import DataVolumeReader  # noqa: E402
from app.main import app  # noqa: E402
from app.routes.emission_controller import (  # noqa: E402
    classify_ledger,
    split_overrides,
    summarise,
)

# --- verbatim engine output; see module docstring for provenance --------------
STORE_PAYLOAD = {
    "state": {
        "overrides": {
            "SHADOW_FUNDING_FADE": {"suppress_negative": None, "min_samples": 15},
            "QUIET_COMPRESSION_BREAK@ATR": {"suppress_negative": None, "min_samples": 25},
        },
        "history": {
            "QUIET_COMPRESSION_BREAK@ATR|min_samples": [],
            "RANGE_FADE|min_samples": ["LOWER", "LOWER"],
        },
        "last_change_cycle": {"QUIET_COMPRESSION_BREAK@ATR|min_samples": 2},
        "cycle": 2,
    },
    "ledger": [
        {
            "strategy": "QUIET_COMPRESSION_BREAK@ATR",
            "param": "min_samples",
            "old": 30,
            "new": 25,
            "applied": True,
            "status": "PROMOTED",
            "reason": "stable+bar+grace → promote (strong_cell_n=25)",
            "verdict": "LOWER",
            "ev_per_suppression_r": None,
            "n": 25,
            "routable": False,
        }
    ],
    "pending": [],
    "active_overrides": {
        "SHADOW_FUNDING_FADE": {"min_samples": 15},
        "QUIET_COMPRESSION_BREAK@ATR": {"min_samples": 25},
    },
    "routability": {
        "enforced": False,
        "routable_candidates": 1,
        "unroutable_candidates": 1,
        "unroutable_strategies": ["QUIET_COMPRESSION_BREAK@ATR", "SHADOW_FUNDING_FADE"],
        "dead_overrides": {"SHADOW_FUNDING_FADE": {"min_samples": 15}},
        "promoted_unroutable": ["QUIET_COMPRESSION_BREAK@ATR|min_samples"],
        "starved_routable": ["RANGE_FADE|min_samples"],
        "pruned": [],
        "wasted_promotions": 1,
    },
}


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


def _payload(**overrides) -> dict:
    p = copy.deepcopy(STORE_PAYLOAD)
    p.update(overrides)
    return p


class TestClassifyLedger:
    def test_splits_on_the_engine_stamped_flag(self):
        out = classify_ledger([
            {"strategy": "RANGE_FADE", "status": "PROMOTED", "routable": True},
            {"strategy": "QCB@ATR", "status": "PROMOTED", "routable": False},
            {"strategy": "QCB@FIXED", "status": "PROMOTED", "routable": False},
        ])
        assert out["counts"] == {
            "routable": 1, "unroutable": 2, "unclassified": 0, "pruned": 0,
        }
        assert out["promotions"] == 3
        assert round(out["wasted_pct"]) == 67
        assert out["unroutable_by_key"] == {"QCB@ATR": 1, "QCB@FIXED": 1}

    def test_unstamped_rows_are_unclassified_never_guessed(self):
        # A row written before the engine stamped routability carries None. Ops
        # must not infer it from the name — re-deriving the suffix list here is
        # the drift class that inflated the Strategy Lab rollup for a week.
        out = classify_ledger([
            {"strategy": "QUIET_COMPRESSION_BREAK@ATR", "status": "PROMOTED"},
            {"strategy": "RANGE_FADE", "status": "PROMOTED", "routable": True},
        ])
        assert out["counts"]["unclassified"] == 1
        assert out["counts"]["unroutable"] == 0      # NOT inferred from "@ATR"
        assert out["unroutable_by_key"] == {}
        # and the honest denominator is reported alongside
        assert out["classified"] == 1
        assert out["promotions"] == 2

    def test_prunes_are_not_counted_as_promotions(self):
        # A prune removes dead state rather than changing policy; folding it into
        # promotions would overstate both budget spent and cleanup done.
        out = classify_ledger([
            {"strategy": "QCB@ATR", "status": "PRUNED", "routable": False},
            {"strategy": "RANGE_FADE", "status": "PROMOTED", "routable": True},
        ])
        assert out["counts"]["pruned"] == 1
        assert out["counts"]["unroutable"] == 0
        assert out["promotions"] == 1
        assert out["wasted_pct"] == 0.0

    def test_empty_ledger_does_not_divide_by_zero(self):
        # None, not 0.0: nothing classified is an UNKNOWN share, and a rendered
        # "0%" would read as the controller behaving perfectly.
        out = classify_ledger([])
        assert out["promotions"] == 0 and out["wasted_pct"] is None

    def test_wasted_share_is_measured_on_classified_rows_not_all_promotions(self):
        """The denominator is the population the numerator can come from.

        ``unroutable`` is knowable only for a row the engine stamped, so
        dividing it by every promotion mixes populations — and the unstamped
        rows can only push the figure DOWN, on the panel whose whole job is to
        show wasted budget. The live page read **19%** (10 of 52) on 2026-08-07
        where the measured share was **83%** (10 of 12), understating #806/#807
        by 4.3x. The right value was already being computed one line below.
        """
        rows = (
            [{"strategy": "RANGE_FADE", "status": "PROMOTED", "routable": True}] * 2
            + [{"strategy": "QCB@ATR", "status": "PROMOTED", "routable": False}] * 10
            # 40 rows written before the engine stamped routability.
            + [{"strategy": "RANGE_FADE", "status": "PROMOTED"}] * 40
        )
        out = classify_ledger(rows)

        assert out["promotions"] == 52
        assert out["classified"] == 12
        assert out["counts"]["unclassified"] == 40
        # 10/12, not 10/52.
        assert round(out["wasted_pct"]) == 83

    def test_all_rows_unclassified_reports_unknown_not_zero(self):
        """A window entirely predating the stamp says nothing about waste."""
        out = classify_ledger(
            [{"strategy": "RANGE_FADE", "status": "PROMOTED"}] * 5
        )
        assert out["promotions"] == 5
        assert out["classified"] == 0
        assert out["wasted_pct"] is None


class TestSplitOverrides:
    def test_membership_comes_from_the_engines_dead_list(self):
        out = split_overrides(
            {"RANGE_FADE": {"min_samples": 20}, "QCB@ATR": {"min_samples": 15}},
            {"QCB@ATR": {"min_samples": 15}},
        )
        assert list(out["live"]) == ["RANGE_FADE"]
        assert list(out["unroutable"]) == ["QCB@ATR"]
        assert (out["live_n"], out["unroutable_n"], out["total_n"]) == (1, 1, 2)

    def test_absent_from_dead_list_is_treated_as_live(self):
        # Failing toward "this override is real" keeps a classification bug here
        # from quietly hiding a genuine override from the owner.
        out = split_overrides({"WEIRD@THING": {"min_samples": 20}}, {})
        assert list(out["live"]) == ["WEIRD@THING"]
        assert out["unroutable_n"] == 0


class TestSummarise:
    def test_reduces_the_real_engine_payload(self):
        s = summarise(STORE_PAYLOAD)
        assert s["error"] is None
        assert s["measuring"] is True and s["enforced"] is False
        assert s["cycle"] == 2
        assert s["overrides"]["unroutable_n"] == 1     # SHADOW_FUNDING_FADE
        assert s["overrides"]["live_n"] == 1           # QCB@ATR is not in dead_overrides
        assert s["latest"]["promoted_unroutable"] == ["QUIET_COMPRESSION_BREAK@ATR|min_samples"]
        assert s["latest"]["starved_routable"] == ["RANGE_FADE|min_samples"]
        assert s["lifetime"]["counts"]["unroutable"] == 1

    def test_missing_routability_block_reads_as_unknown_not_healthy(self):
        # The distinction the SAR arm paid for: an empty measurement must never
        # render as zeros that look like health.
        s = summarise(_payload(routability={}))
        assert s["measuring"] is False
        assert s["enforced"] is False

    def test_volume_error_is_surfaced_not_swallowed(self):
        s = summarise({"error": "missing: /engine-data/emission_controller_store.json"})
        assert s["error"] and "missing" in s["error"]

    def test_shape_drift_yields_empty_sections_not_a_crash(self):
        # The engine surface is the source of truth and this dashboard adapts to
        # it (ops convention), so wrong types must degrade rather than 500.
        s = summarise({"ledger": "not-a-list", "active_overrides": 7, "routability": None})
        assert s["ledger"] == [] and s["overrides"]["total_n"] == 0
        assert s["measuring"] is False

    def test_ledger_is_newest_first(self):
        p = _payload(ledger=[
            {"strategy": "A", "status": "PROMOTED", "routable": True},
            {"strategy": "B", "status": "PROMOTED", "routable": True},
        ])
        assert [r["strategy"] for r in summarise(p)["ledger"]] == ["B", "A"]

    def test_non_dict_payload_is_handled(self):
        s = summarise(None)
        assert s["measuring"] is False and s["ledger"] == []


class TestEmissionControllerRoute:
    def test_requires_auth(self):
        with TestClient(app) as client:
            r = client.get("/emission-controller", follow_redirects=False)
            assert r.status_code == 302

    def test_page_renders_the_finding(self, monkeypatch):
        monkeypatch.setattr(
            DataVolumeReader, "emission_controller", lambda self: STORE_PAYLOAD,
        )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/emission-controller")
            assert r.status_code == 200
            assert "Layer G" in r.text
            assert "MEASURING" in r.text
            # the counterfactual — both halves on screen
            assert "QUIET_COMPRESSION_BREAK@ATR|min_samples" in r.text
            assert "RANGE_FADE|min_samples" in r.text
            assert "would have promoted instead" in r.text
            # the standing dead-override footprint
            assert "SHADOW_FUNDING_FADE" in r.text

    def test_not_measuring_says_unknown_on_screen(self, monkeypatch):
        monkeypatch.setattr(
            DataVolumeReader, "emission_controller",
            lambda self: _payload(routability={}),
        )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/emission-controller")
            assert r.status_code == 200
            assert "Not measuring" in r.text
            # the wording must say unknown, not healthy — an empty measurement
            # rendering as zeros is the failure mode this page exists to avoid
            assert "<em>unknown</em>" in r.text

    def test_missing_store_renders_a_hint(self, monkeypatch):
        monkeypatch.setattr(
            DataVolumeReader, "emission_controller",
            lambda self: {"error": "missing: /engine-data/emission_controller_store.json"},
        )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/emission-controller")
            assert r.status_code == 200
            assert "Store unavailable" in r.text

    def test_enforcing_with_dead_overrides_flags_the_prune_failure(self, monkeypatch):
        rout = copy.deepcopy(STORE_PAYLOAD["routability"])
        rout["enforced"] = True
        monkeypatch.setattr(
            DataVolumeReader, "emission_controller", lambda self: _payload(routability=rout),
        )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/emission-controller")
            assert r.status_code == 200
            assert "ENFORCING" in r.text
            assert "yet dead overrides persist" in r.text


# --- regressions from the first deploy (owner-caught 2026-07-27) -------------
#
# Both of these shipped and were visible on the live page. Neither was caught by
# the reducer tests above, which is the lesson: `classify_ledger` counted an
# unstamped row as `unclassified` perfectly correctly, while the template
# rendered that same row as "no". A reducer test cannot see a template bug.


class TestUnstampedRowRendering:
    """A row the engine never stamped must read as unknown, never as unroutable.

    In Jinja, ``row.routable`` on a dict *lacking* the key yields ``Undefined``,
    and ``Undefined is none`` is **False** — so a none/truthy/else chain lands on
    the else branch and renders "no". The live page therefore asserted that
    ``MOVER_AVWAP_SCALP`` and ``DIVERGENCE_CONTINUATION`` — real, routable
    strategies — were unroutable. That is ops inventing a classification the
    engine did not make, which is the one thing this module must never do.
    """

    UNSTAMPED = {
        "strategy": "MOVER_AVWAP_SCALP", "param": "min_samples",
        "old": 25, "new": 30, "applied": True, "status": "PROMOTED",
        "verdict": "RAISE", "ev_per_suppression_r": None, "n": 22,
        "reason": "stable+bar+grace → promote (strong_cell_n=22)",
        # NOTE: no "routable" key — exactly what the pre-fix store holds.
    }

    def test_reducer_normalises_the_missing_key_to_none(self):
        from app.routes.emission_controller import _rows

        rows = _rows({"ledger": [dict(self.UNSTAMPED)]}, "ledger")
        assert "routable" in rows[0] and rows[0]["routable"] is None

    def test_page_renders_unknown_not_unroutable(self, monkeypatch):
        # bind outside the lambda: inside it, `self` is the DataVolumeReader
        payload = _payload(ledger=[dict(self.UNSTAMPED)])
        monkeypatch.setattr(
            DataVolumeReader, "emission_controller", lambda _self: payload,
        )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/emission-controller")
            assert r.status_code == 200
            assert "MOVER_AVWAP_SCALP" in r.text
            # the unknown marker is present, and the unroutable badge is NOT
            assert '<span class="muted">?</span>' in r.text
            assert '<span class="neg">no</span>' not in r.text

    def test_a_genuinely_unroutable_row_still_reads_no(self, monkeypatch):
        # The fix must not swing the other way and hide real unroutable rows.
        payload = _payload(ledger=[dict(self.UNSTAMPED, strategy="QCB@ATR", routable=False)])
        monkeypatch.setattr(
            DataVolumeReader, "emission_controller", lambda _self: payload,
        )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/emission-controller")
            assert '<span class="neg">no</span>' in r.text


class TestNotMeasuringMakesNoClaims:
    """With no routability block, the page must not say the policy reads anything.

    The first cut split overrides into two states and defaulted to "live", so with
    no routability block all 18 overrides — including the 9 known dead keys —
    rendered under the heading "Active overrides the policy actually reads". Every
    number was defensible; the sentence above them was false.
    """

    ACTIVE = {
        "RANGE_FADE": {"min_samples": 20},
        "QUIET_COMPRESSION_BREAK@ATR": {"min_samples": 15},
        "SHADOW_FUNDING_FADE": {"min_samples": 15},
    }

    def test_split_refuses_to_classify_when_not_measuring(self):
        out = split_overrides(self.ACTIVE, {}, measuring=False)
        assert out["unknown_n"] == 3
        assert out["live_n"] == 0 and out["unroutable_n"] == 0
        assert out["total_n"] == 3

    def test_split_still_classifies_when_measuring(self):
        out = split_overrides(
            self.ACTIVE, {"QUIET_COMPRESSION_BREAK@ATR": {"min_samples": 15}},
            measuring=True,
        )
        assert out["live_n"] == 2 and out["unroutable_n"] == 1
        assert out["unknown_n"] == 0

    def test_page_does_not_claim_the_policy_reads_them(self, monkeypatch):
        payload = _payload(routability={}, active_overrides=self.ACTIVE)
        monkeypatch.setattr(
            DataVolumeReader, "emission_controller", lambda _self: payload,
        )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/emission-controller")
            assert r.status_code == 200
            assert "Not measuring" in r.text
            # the false claim must be gone, replaced by an honest heading
            assert "the policy actually reads" not in r.text
            assert "routability unknown" in r.text
            # the overrides are still listed — hiding them would be worse
            assert "QUIET_COMPRESSION_BREAK@ATR" in r.text
            assert "RANGE_FADE" in r.text

    def test_measuring_page_does_make_the_claim(self, monkeypatch):
        # The honest heading is conditional, not a blanket removal.
        monkeypatch.setattr(
            DataVolumeReader, "emission_controller", lambda self: STORE_PAYLOAD,
        )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/emission-controller")
            assert "the policy actually reads" in r.text

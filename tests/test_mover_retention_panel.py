"""The dynamic-retention panel on /pairs.

The engine scores every promoted mover on opportunity and liveness and stamps
a verdict; this page is where the owner reads it. Three states the panel must
keep apart, because they have three different next moves and pooling any two
is how a page reports a fault that is not happening:

* **not reported** — the engine build predates the lane. Nothing is scoring.
* **unreadable** — the lane reported an error. A fault, ours or the engine's.
* **reporting** — the lane is running. It may be holding zero pairs, which is
  the quiet case and not a fault.

And one state inside a row: a pair with **no activity reading**. That renders
an em-dash, never `0.00×`, because a missing reading is precisely why the pair
is held rather than released, and a zero there reads as a dead move.
"""
from __future__ import annotations

import os
import re

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


def _visible(html: str) -> str:
    """Tag-stripped, whitespace-collapsed text.

    Assertions run against what a reader SEES: an apostrophe arrives as
    `&#39;` and a sentence wrapped across two source lines carries a newline,
    and a test that fails on either is testing the template's formatting
    rather than its meaning.
    """
    txt = re.sub(r"<[^>]+>", " ", html)
    txt = txt.replace("&#39;", "'").replace("&amp;", "&").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", txt)


_BOUNDS = {
    "min_hold_sec": 1800.0, "max_hold_sec": 43200.0, "flat_ttl_sec": 21600.0,
    "min_scans_to_judge": 20, "spent_burst_ratio": 1.0, "spent_for_sec": 900.0,
}

_IGNITION = {
    "enabled": True, "tracked_symbols": 400, "frames_ingested": 9000,
    "ignitions_total": 5, "last_ignition_at": "2026-08-13T09:00:00+00:00",
    "ws_connected": True, "ws_streams": 1,
}


def _row(symbol: str, **over) -> dict:
    row = {
        "symbol": symbol, "minutes_left": 100.0, "volume_24h_usd": 5e6,
        "change_24h_pct": 12.0, "reject_reason": None, "reject_path": None,
        "reject_age_sec": None,
        "retention_verdict": "hold", "retention_reason": "producing",
        "retention_age_sec": 4000.0, "retention_scans": 100,
        "retention_candidates": 4, "retention_reached_enqueue": 1,
        "retention_enqueued": 1, "retention_dark": 0,
        "retention_burst": 2.5, "promotion_source": "MOVER_IGNITION",
    }
    row.update(over)
    return row


def _payload(promoting=None, retention="present") -> dict:
    p = promoting if promoting is not None else [_row("AAAUSDT")]
    out = {
        "regular": [], "regular_count": 0,
        "promoting": p, "promoting_count": len(p),
        "updated_at": "2026-08-13T10:00:00+00:00",
        "ignition": _IGNITION,
    }
    if retention == "present":
        out["retention"] = {
            "enforcing": False, "held": len(p), "counters": {}, "bounds": _BOUNDS,
        }
    elif retention == "enforcing":
        out["retention"] = {
            "enforcing": True, "held": len(p), "counters": {}, "bounds": _BOUNDS,
        }
    elif retention == "error":
        out["retention"] = {"error": "boom"}
    # retention == "absent" ⇒ no key at all
    return out


class _API:
    def __init__(self, payload):
        self._payload = payload

    def __getattr__(self, name):
        async def _f(*a, **kw):
            return {}
        return _f

    async def pairs(self):
        return self._payload


def _page(payload, path="/pairs?tab=promoting") -> str:
    with TestClient(app) as client:
        app.state.engine_api = _API(payload)
        _login(client)
        resp = client.get(path)
        assert resp.status_code == 200, resp.status_code
        return resp.text


# --------------------------------------------------------------------------- #
# Three states, never two
# --------------------------------------------------------------------------- #

def test_an_engine_that_does_not_report_says_so_rather_than_reading_empty():
    """An older engine holds pairs on the flat TTL with nothing scoring them.

    That is NOT "the lane is running and holds no pairs", and a page that
    rendered the same thing for both would tell the owner a scorer is working
    when no scorer exists.
    """
    text = _visible(_page(_payload(retention="absent")))
    assert "NOT REPORTED" in text
    assert "predates" in text
    assert "MEASURING ONLY" not in text
    assert "ENFORCING" not in text


def test_an_error_from_the_lane_is_a_fault_not_a_quiet_market():
    text = _visible(_page(_payload(retention="error")))
    assert "UNREADABLE" in text
    assert "boom" in text
    assert "NOT REPORTED" not in text


def test_a_reporting_lane_holding_nothing_is_the_quiet_case():
    """Zero held pairs in a quiet market is normal and must not read as a fault."""
    text = _visible(_page(_payload(promoting=[], retention="present")))
    assert "MEASURING ONLY" in text
    assert "UNREADABLE" not in text
    assert "NOT REPORTED" not in text


# --------------------------------------------------------------------------- #
# The mode is what decides whether a verdict does anything
# --------------------------------------------------------------------------- #

def test_measuring_only_says_the_verdicts_change_nothing_yet():
    """The two-flag rule on screen.

    A verdict column beside a flat-TTL hold reads as a decision unless the page
    says otherwise — and "released 14 pairs" that released nothing is the
    reading that would follow.
    """
    text = _visible(_page(_payload(retention="present")))
    assert "MEASURING ONLY" in text
    assert "change nothing yet" in text


def test_enforcing_says_the_verdicts_are_acted_on():
    text = _visible(_page(_payload(retention="enforcing")))
    assert "ENFORCING" in text
    assert "acted on" in text
    assert "change nothing yet" not in text


def test_neither_mode_is_rendered_as_an_error():
    """Enforcing is a setting the owner chose, not a fault.

    Colouring it red sends the reader to debug a subsystem doing exactly what
    it was told — the /invalidations lesson with the sign flipped, and the
    alarming version is the worse one.
    """
    html = _page(_payload(retention="enforcing"))
    panel = html[html.find("Retention:") - 400: html.find("Retention:") + 200]
    assert "flash-err" not in panel


# --------------------------------------------------------------------------- #
# The row
# --------------------------------------------------------------------------- #

def test_a_release_verdict_names_its_reason():
    """A release with no reason beside it is a counter, and a counter is not a
    cause: no_candidates and move_spent are different findings with different
    fixes."""
    text = _visible(_page(_payload([
        _row("BBBUSDT", retention_verdict="release",
             retention_reason="no_candidates", retention_candidates=0),
    ])))
    assert "release" in text
    assert "no_candidates" in text


def test_a_pair_with_no_activity_reading_renders_a_dash_never_a_zero():
    """An em-dash is "the engine could not measure this", which is exactly why
    the pair is HELD. `0.00×` there reads as a dead move — the opposite."""
    html = _page(_payload([_row("CCCUSDT", retention_burst=None)]))
    text = _visible(html)
    assert "0.00×" not in text
    assert "—" in text
    assert "held, not dropped" in html  # the title= explaining the dash


def test_a_pair_with_no_retention_window_reads_not_scored_not_hold():
    """Absent is not a pass.

    A promoted pair the scorer does not hold means the two sets have desynced;
    rendering it as `hold` would report a verdict nothing reached.
    """
    row = _row("DDDUSDT")
    for key in list(row):
        if key.startswith("retention_"):
            row.pop(key)
    text = _visible(_page(_payload([row])))
    assert "not scored" in text


def test_the_row_shows_the_funnel_not_just_a_verdict():
    """Scans → candidates → enqueue is the evidence the verdict was reached on.

    Without it the reader cannot tell a pair that offered nothing from one we
    barely scanned — which is the whole distinction MIN_SCANS_TO_JUDGE exists
    to draw.
    """
    text = _visible(_page(_payload([
        _row("EEEUSDT", retention_scans=214, retention_candidates=9,
             retention_reached_enqueue=3),
    ])))
    assert "214 → 9 → 3" in text


# --------------------------------------------------------------------------- #
# The copy carries the one rule the lane rests on
# --------------------------------------------------------------------------- #

def test_the_page_states_that_retention_never_scores_on_outcomes():
    """Copy is part of the measurement.

    A reader who assumes this drops losing pairs would read every verdict
    wrongly — and would eventually ask for it to be "improved" with a win
    rate, which is the absorbing state cohort_edge cost 23 days of feed.
    """
    text = _visible(_page(_payload(retention="present")))
    assert "never on outcomes" in text
    assert "earn its way back" in text


def test_the_thresholds_render_beside_the_verdicts_they_were_reached_against():
    """A verdict with no threshold on screen cannot be judged, and a threshold
    that lives only in the engine source is one the owner cannot check."""
    text = _visible(_page(_payload(retention="present")))
    assert "30 min" in text        # min_hold_sec
    assert "≥20 times" in text     # min_scans_to_judge
    assert "15 min" in text        # spent_for_sec
    assert "12h ceiling" in text   # max_hold_sec
    assert "6.0h TTL" in text      # flat_ttl_sec


def test_the_bounds_come_from_the_engine_and_are_not_hardcoded_here():
    """Ops ports the engine's numbers, it does not invent them.

    A threshold typed into this template would disagree with the engine the
    first time the owner changed one, and the page would keep looking right.
    """
    payload = _payload(retention="present")
    payload["retention"]["bounds"] = dict(_BOUNDS, min_scans_to_judge=99,
                                          max_hold_sec=7200.0)
    text = _visible(_page(payload))
    assert "≥99 times" in text
    assert "2h ceiling" in text


# --------------------------------------------------------------------------- #
# The distribution that is meant to replace the guessed floor
# --------------------------------------------------------------------------- #

_TTFC = {"n_held": 4, "n_produced": 3, "median_sec": 2700.0,
         "max_sec": 5400.0, "first_after_min_hold": 2}


def _with_ttfc(ttfc) -> dict:
    payload = _payload(retention="present")
    payload["retention"]["time_to_first_candidate"] = ttfc
    return payload


def test_the_page_says_the_release_floor_was_chosen_without_evidence():
    """An unlabelled inference reads exactly like a measurement.

    The 30-minute floor came from neither existing code nor a measured window,
    and a reader who assumed otherwise would treat the release counts below it
    as a finding rather than as an artifact of my number.
    """
    text = _visible(_page(_with_ttfc(_TTFC)))
    assert "chosen without evidence" in text


def test_a_pair_that_produced_only_after_the_floor_is_flagged_loudly():
    """This is the destructive direction and the one worth colour.

    A floor below the distribution does not merely fail to save budget — it
    drops pairs that were about to produce, which costs signals.
    """
    text = _visible(_page(_with_ttfc(_TTFC)))
    assert "2 produced only AFTER the floor" in text
    assert "too low" in text


def test_no_such_pair_means_no_alarm():
    text = _visible(_page(_with_ttfc(dict(_TTFC, first_after_min_hold=0))))
    assert "produced only AFTER the floor" not in text
    assert "chosen without evidence" in text, "the caveat is not conditional"


def test_the_distribution_names_the_population_it_is_measured_on():
    """It answers "when do producers produce", never "how many produce" — and
    a reader who took it for the second would read a survivor's timing as the
    lane's hit rate."""
    text = _visible(_page(_with_ttfc(_TTFC)))
    assert "never how many produce" in text
    assert "Of 4 held pair(s), 3 have produced" in text


def test_nothing_produced_yet_is_stated_rather_than_left_blank():
    text = _visible(_page(_with_ttfc(
        {"n_held": 2, "n_produced": 0, "median_sec": None,
         "max_sec": None, "first_after_min_hold": 0}
    )))
    assert "None of the 2 held pair(s) has produced" in text


def test_an_engine_that_does_not_report_the_distribution_renders_nothing():
    """A block with no data must not render as zeros — `0 min median` over an
    absent measurement is a blank wearing a finding's clothes."""
    text = _visible(_page(_payload(retention="present")))
    assert "Time to first candidate" not in text


# --------------------------------------------------------------------------- #
# The stamp's readers — a field one repo writes and no repo reads is #817 with
# the arrow reversed, and the producing side's test passes either way.
# --------------------------------------------------------------------------- #

def test_the_track_record_export_carries_the_promotion_age():
    """Where a mover signal fired in its hold leaves ops here or nowhere.

    100% of today's rows read blank, which is correct and expected — the stamp
    ships now and there is no backfill. The column has to exist first or the
    window it describes accumulates with no reader.
    """
    from app.routes.track_record import _TRADE_COLS

    assert "promotion_age_sec" in _TRADE_COLS
    assert "pair_admission" in _TRADE_COLS


def test_the_track_record_row_reads_the_engines_field_names():
    """Driven through the real reducer, against the engine's own key names.

    A cross-repo field name is a contract; a fixture that renamed it would pass
    here and read blank against production forever.
    """
    from app.routes.track_record import reduce_records

    rows = reduce_records([{
        "signal_id": "s1", "symbol": "AAAUSDT", "direction": "LONG",
        "setup_class": "MOVER_TREND_PULLBACK", "pnl_pct": 1.0,
        "outcome_label": "TP1_HIT", "pair_admission": "MOVER_IGNITION",
        "promotion_age_sec": 900.0,
    }])
    assert rows[0]["promotion_age_sec"] == 900.0
    assert rows[0]["pair_admission"] == "MOVER_IGNITION"


def test_a_row_without_the_stamp_is_none_not_zero():
    """`0.0` is a real reading — a signal fired at the top of a hold — so a row
    the engine never stamped must not borrow its meaning."""
    from app.routes.track_record import reduce_records

    rows = reduce_records([{
        "signal_id": "s2", "symbol": "BBBUSDT", "direction": "SHORT",
        "setup_class": "MEAN_REVERT", "pnl_pct": -1.0, "outcome_label": "SL_HIT",
    }])
    assert rows[0]["promotion_age_sec"] is None
    assert rows[0]["pair_admission"] == ""


def test_the_dark_feed_export_carries_it_too():
    """The dark lane is the population that reaches the rare paths, so it is
    where a per-path promotion-age split will have any n at all."""
    from app.routes.dark_signals_live import _DARK_COLS

    assert "promotion_age_sec" in _DARK_COLS


# --------------------------------------------------------------------------- #
# Top gainer vs top loser — the distinction the promotion path discards
# --------------------------------------------------------------------------- #

def test_a_top_gainer_and_a_top_loser_are_labelled_apart():
    """The engine promotes on |24h %| and stores abs(change_pct), so this is
    the only surface carrying the sign. On the delivered book buying a gainer
    and shorting a loser differed by +1.944%/trade — they are not the same
    kind of pair and must not render as one."""
    text = _visible(_page(_payload([
        _row("UPUSDT", promotion_gainer=True, promotion_change_pct=31.4),
        _row("DOWNUSDT", promotion_gainer=False, promotion_change_pct=-27.9),
    ])))
    assert "top gainer" in text and "+31.4%" in text
    assert "top loser" in text and "-27.9%" in text


def test_no_reading_reads_unknown_and_never_loser():
    """`None` is not `False`. A detector that could not report the move must
    not make every unmeasurable pair render as a top loser — that is a bool
    standing in for a tri-state, in the flattering-to-nobody direction."""
    text = _visible(_page(_payload([
        _row("QQQUSDT", promotion_gainer=None, promotion_change_pct=None),
    ])))
    assert "kind unknown" in text
    assert "top loser" not in text
    assert "top gainer" not in text


def test_an_engine_without_the_stamp_does_not_invent_a_kind():
    """Older engines send neither key. Absent must behave like unknown, not
    like a gainer — Jinja yields Undefined for a missing key, which is neither
    None nor a value, and the first cut of the exit-mechanism control fell past
    both branches on exactly that."""
    row = _row("OLDUSDT")
    row.pop("promotion_gainer", None); row.pop("promotion_change_pct", None)
    text = _visible(_page(_payload([row])))
    assert "top gainer" not in text and "top loser" not in text
    assert "kind unknown" in text

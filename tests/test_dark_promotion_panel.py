"""Dark → live promotion: the control panel, and the panels that judge it.

The defects this file is shaped against are the ones a passing unit test does
not catch, because they are about **which population a number describes**:

* pooling promoted rows with dark ones, so an average describes a feed that
  never existed;
* reading ``promoted`` as ``delivered``, which is the enqueue-is-not-dispatch
  error arriving at the mechanism that deliberately puts more rows on the
  queue;
* folding rows written before the mechanism into "not promoted", which buries
  the evidence every rule was built from inside the rows the rules declined;
* a control page whose numbers disagree with the page the evidence came from.

Plus the two structural ones: the panel must be reachable from the nav, and it
must be owner-only.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app.data_sources import dark_promotion as dp  # noqa: E402


def _row(
    *,
    setup="LIQUIDITY_SWEEP_REVERSAL",
    gate="setup_compat:regime_STRONG_TREND",
    regime="TRENDING_DOWN",
    session="NY",
    side="SHORT",
    status="CLOSED_TP1",
    pnl=1.0,
    symbol="AAAUSDT",
    delivery=dp.DELIVERY_DARK,
    drop_reason=None,
    unstamped=False,
):
    row = {
        "symbol": symbol, "side": side, "setup_class": setup,
        "dark_gate": gate, "regime": regime,
        "context_key": f"{session}/MARKDOWN/NORMAL/BTC_NEUTRAL",
        "status": status, "pnl_pct": pnl, "emitted_at": 1_786_000_000.0,
    }
    if not unstamped:
        row["delivery"] = delivery
        row["promoted"] = delivery != dp.DELIVERY_DARK
        row["router_drop_reason"] = drop_reason
    return row


# --------------------------------------------------------------------------- #
# The populations, and the lines between them
# --------------------------------------------------------------------------- #


def test_a_row_without_the_block_is_unstamped_not_dark():
    """Absent is not `False`.

    Every row that argued for the first promotion was written before the
    mechanism existed. Calling those "not promoted" folds the evidence into the
    population the rules declined to act on, and the two only diverge as the
    lane keeps running — so it would look right on the day it shipped.
    """
    assert dp.delivery_of(_row(unstamped=True)) == dp.DELIVERY_UNSTAMPED
    assert dp.delivery_of(_row(delivery=dp.DELIVERY_DARK)) == dp.DELIVERY_DARK
    assert dp.is_promoted(_row(unstamped=True)) is False


def test_promoted_and_dark_are_never_pooled():
    rows = [
        _row(pnl=5.0, delivery=dp.DELIVERY_DELIVERED, symbol="AAA"),
        _row(pnl=-3.0, delivery=dp.DELIVERY_DARK, symbol="BBB"),
    ]
    lanes = dp.promoted_vs_dark(rows)
    assert lanes["delivered"]["avg_pct"] == 5.0
    assert lanes["dark"]["avg_pct"] == -3.0
    # …and there is deliberately no blended figure to read by mistake.
    assert "combined" not in lanes
    assert "overall" not in lanes


def test_promoted_is_not_delivered():
    """The whole enqueue-is-not-dispatch rule, at the panel that could hide it.

    A rule that promotes 20 rows into a correlation lock has delivered nothing,
    and "20 promoted" reads exactly like "20 subscribers saw it" unless the
    split is published.
    """
    rows = [
        _row(delivery=dp.DELIVERY_DELIVERED, symbol="A"),
        _row(delivery=dp.DELIVERY_DROPPED, drop_reason="correlation_lock", symbol="B"),
        _row(delivery=dp.DELIVERY_DROPPED, drop_reason="correlation_lock", symbol="C"),
        _row(delivery=dp.DELIVERY_ENQUEUED, symbol="D"),
    ]
    split = dp.delivery_split(rows)
    assert split["n_promoted"] == 4
    assert split["n_delivered"] == 1
    assert split["n_dropped"] == 2
    assert split["n_awaiting_router"] == 1
    assert split["delivery_rate"] == 25.0
    assert split["drop_reasons"] == {"correlation_lock": 2}


def test_an_unknown_delivery_state_is_counted_not_dropped():
    """The engine owns this vocabulary; ops must not iterate its own copy."""
    split = dp.delivery_split([_row(delivery="promoted_something_new")])
    assert split["unclassified"] == {"promoted_something_new": 1}
    assert split["counts"]["promoted_something_new"] == 1


def test_a_drop_with_no_reason_is_named_rather_than_silently_absent():
    split = dp.delivery_split([_row(delivery=dp.DELIVERY_DROPPED)])
    assert split["drop_reasons"] == {"unnamed": 1}


# --------------------------------------------------------------------------- #
# Three buckets, never two
# --------------------------------------------------------------------------- #


def test_a_flat_expiry_is_not_a_loss():
    rows = [
        _row(pnl=2.0, symbol="A"),
        _row(pnl=-1.0, symbol="B"),
        _row(pnl=0.0, status="EXPIRED", symbol="C"),
    ]
    s = dp.summarize(rows)
    assert (s["wins"], s["losses"], s["flats"]) == (1, 1, 1)
    # Both denominators, neither called "the" win rate.
    assert s["win_rate_closed"] == 100 / 3
    assert s["win_rate_levelled"] == 50.0


def test_insufficient_rows_are_excluded_from_every_rate():
    """Terminal but unscored — the absence of a measurement, not a zero."""
    rows = [_row(pnl=2.0), _row(status="INSUFFICIENT", pnl=None, symbol="B")]
    s = dp.summarize(rows)
    assert s["n_rows"] == 2
    assert s["n_scored"] == 1
    assert s["avg_pct"] == 2.0


def test_open_rows_carry_no_outcome_and_do_not_dilute_an_average():
    rows = [_row(pnl=2.0), _row(status="OPEN", pnl=None, symbol="B")]
    assert dp.summarize(rows)["n_scored"] == 1


# --------------------------------------------------------------------------- #
# Evidence: n and concentration before the average
# --------------------------------------------------------------------------- #


def test_condition_evidence_splits_by_the_dimension_asked_for():
    rows = [
        _row(gate="execution:overextended", pnl=1.0, symbol="A"),
        _row(gate="execution:overextended", pnl=3.0, symbol="B"),
        _row(gate="setup_compat:regime_STRONG_TREND", pnl=-1.0, symbol="C"),
    ]
    ev = dp.condition_evidence(rows, dimension="gate")
    cells = {c["value"]: c for c in ev["cells"]}
    assert cells["execution:overextended"]["n_scored"] == 2
    assert cells["execution:overextended"]["avg_pct"] == 2.0
    assert cells["setup_compat:regime_STRONG_TREND"]["avg_pct"] == -1.0


def test_the_session_dimension_reads_the_engines_context_key():
    rows = [_row(session="ASIA", symbol="A"), _row(session="NY", symbol="B")]
    ev = dp.condition_evidence(rows, dimension="session")
    assert {c["value"] for c in ev["cells"]} == {"ASIA", "NY"}


def test_cells_are_sorted_by_evidence_and_never_by_edge():
    """A table sorted by edge puts the luckiest thin cell on the top line.

    "Best of N" is not a fact about the winner until N is on screen, and this
    panel sits directly beside the checkbox that promotes it.
    """
    rows = (
        [_row(gate="thin", pnl=99.0, symbol="Z")]
        + [_row(gate="thick", pnl=0.1, symbol=f"S{i}") for i in range(5)]
    )
    ev = dp.condition_evidence(rows, dimension="gate")
    assert ev["cells"][0]["value"] == "thick"


def test_cells_drawn_is_published_so_the_winner_can_be_priced():
    rows = [_row(gate=f"g{i}", symbol=f"S{i}") for i in range(6)]
    ev = dp.condition_evidence(rows, dimension="gate")
    assert ev["cells_drawn"] == 6


def test_a_cell_reports_its_campaigns_not_just_its_rows():
    """Concentration keyed on symbol·side over the window.

    A dark candidate is diverted before the router, so no per-symbol cooldown
    applies and its repeats spread across hours instead of bunching — a
    time-clustered "run" key reads ~1.1 rows/group here and calls concentration
    a non-problem over a book whose sign is a handful of campaigns.
    """
    rows = [_row(symbol="AAA") for _ in range(4)] + [_row(symbol="BBB")]
    ev = dp.condition_evidence(rows, dimension="gate")
    cell = ev["cells"][0]
    assert cell["n_scored"] == 5
    assert cell["campaigns"] == 2


def test_a_cell_says_how_much_of_itself_is_already_the_rules_own_output():
    rows = [
        _row(delivery=dp.DELIVERY_DELIVERED, symbol="A"),
        _row(delivery=dp.DELIVERY_DARK, symbol="B"),
    ]
    ev = dp.condition_evidence(rows, dimension="gate")
    assert ev["cells"][0]["n_promoted"] == 1


def test_the_baseline_is_the_whole_selection_not_the_winning_cell():
    rows = [
        _row(gate="a", pnl=4.0, symbol="A"),
        _row(gate="b", pnl=-2.0, symbol="B"),
    ]
    ev = dp.condition_evidence(rows, dimension="gate")
    assert ev["baseline"]["n_scored"] == 2
    assert ev["baseline"]["avg_pct"] == 1.0


def test_a_confidence_interval_is_stable_across_reloads():
    """A figure that moves when the owner refreshes is one he stops trusting."""
    values = [1.0, -2.0, 3.0, 0.5, -1.5, 2.0]
    assert dp.bootstrap_ci(values) == dp.bootstrap_ci(values)


def test_a_single_row_gets_no_interval_rather_than_a_fake_one():
    assert dp.bootstrap_ci([1.0]) == (None, None)


# --------------------------------------------------------------------------- #
# Rule state: five, because there are five different next moves
# --------------------------------------------------------------------------- #


def _snap(**over):
    snap = {"master_enabled": True, "dark_lane_enabled": True, "rules": []}
    snap.update(over)
    return snap


def test_no_rule_is_distinguished_from_a_rule_switched_off():
    assert dp.rule_state(_snap(), None)[0] == "none"
    assert dp.rule_state(_snap(), {"enabled": False})[0] == "off"


def test_armed_but_inert_is_its_own_state():
    """A switch in the on position that promotes nothing.

    The state an operator is most likely to misread as working, so it is never
    reported as either ON or OFF.
    """
    state, copy = dp.rule_state(_snap(), {"enabled": True, "inert": True})
    assert state == "inert"
    assert "unfinished" in copy


def test_the_master_switch_and_the_lane_switch_are_distinguished():
    rule = {"enabled": True, "inert": False}
    assert dp.rule_state(_snap(master_enabled=False), rule)[0] == "master_off"
    assert dp.rule_state(_snap(dark_lane_enabled=False), rule)[0] == "lane_off"
    assert dp.rule_state(_snap(), rule)[0] == "live"


def test_rule_lookup_is_case_insensitive_and_returns_the_engines_own_dict():
    snap = _snap(rules=[{"setup_class": "LIQUIDITY_SWEEP_REVERSAL", "enabled": True}])
    assert dp.rule_for(snap, "liquidity_sweep_reversal")["enabled"] is True
    assert dp.rule_for(snap, "MEAN_REVERT") is None


def test_a_broken_snapshot_does_not_crash_the_page():
    assert dp.rule_for(None, "X") is None
    assert dp.rule_for({"error": "engine down"}, "X") is None


def test_an_unknown_promoted_state_still_counts_as_promoted():
    """Found by rendering the page, not by a test — which is the point.

    ``is_promoted`` matched against ops' own list of three known states, so a
    fourth state in the ledger vanished from ``n_promoted`` entirely: the
    figure that says how many rows a rule put on the queue silently
    under-reported itself. Ops iterating its own key list, for the fourth time
    under a fourth name.

    It is counted as promoted (it carries the engine's prefix) and kept out of
    both ``delivered`` and ``dropped``, so an unknown state can neither improve
    nor worsen the delivery rate — it shows as rows the page cannot place.
    """
    rows = [
        _row(delivery=dp.DELIVERY_DELIVERED, symbol="A"),
        _row(delivery="promoted_something_new", symbol="B"),
    ]
    split = dp.delivery_split(rows)
    assert split["n_promoted"] == 2
    assert split["n_delivered"] == 1
    assert split["n_dropped"] == 0
    assert split["n_unclassified_promoted"] == 1
    # …and it never lands in "awaiting the router", which would read as a
    # normal in-flight row rather than as a state nobody has classified.
    assert split["n_awaiting_router"] == 0
    assert split["delivery_rate"] == 50.0


# --------------------------------------------------------------------------- #
# The surfaces themselves — rendered, not merely reduced
# --------------------------------------------------------------------------- #
#
# Every defect this repo's two panel surfs found was invisible to a unit test,
# because a paragraph, a caption and a page nobody can reach are none of them
# assertions. So these drive real requests.

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    from app.main import app

    rows = [
        _row(symbol="A1", delivery=dp.DELIVERY_DELIVERED, pnl=2.6),
        _row(symbol="A2", delivery=dp.DELIVERY_DROPPED, drop_reason="correlation_lock"),
        _row(symbol="A3", delivery=dp.DELIVERY_ENQUEUED),
        _row(symbol="A4", delivery=dp.DELIVERY_DARK, pnl=-1.2),
        _row(symbol="A5", unstamped=True),
        # A second path — without one, "collapsed except the card with a rule"
        # cannot fail, because there is nothing left to stay collapsed.
        _row(symbol="B1", setup="MEAN_REVERT", pnl=-1.0),
        _row(symbol="B2", setup="MEAN_REVERT", pnl=0.4),
    ]
    snapshot = {
        "schema": 1, "master_enabled": True, "dark_lane_enabled": True,
        "utc_day": "2026-08-12", "any_token": "*",
        "directions": ["any", "long", "short", "with_trend", "counter_trend"],
        "rules": [{
            "setup_class": "LIQUIDITY_SWEEP_REVERSAL", "enabled": True,
            "gates": ["SETUP_COMPAT:REGIME_STRONG_TREND"], "regimes": ["*"],
            "sessions": ["*"], "direction": "with_trend", "min_confidence": None,
            "max_per_day": 25, "note": "", "inert": False, "promoted_today": 3,
        }],
    }
    with TestClient(app) as c:
        c.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})
        app.state.data_volume.dark_signals = lambda: {"schema": 2, "rows": rows}
        app.state.data_volume.dark_signals_provenance = lambda: {
            "exists": True, "mtime": 1_786_500_000.0, "age_sec": 30.0
        }
        app.state.data_volume.dark_sar_arms = lambda: {"rows": []}

        async def _promos():
            return snapshot

        async def _prices():
            return {}

        app.state.engine_api.dark_promotions = _promos
        app.state.binance_klines.fetch_all_prices = _prices
        yield c


def test_the_control_page_renders_and_states_both_master_switches(client):
    """A rule is inert if either switch is off, for different reasons.

    An operator seeing one without the other cannot tell which half is missing —
    the exit-mechanism control's "three states, never two", with the second
    switch that actually exists in this chain.
    """
    r = client.get("/control/promotions")
    assert r.status_code == 200
    assert "dark_promotion_enabled" in r.text
    assert "dark_emission_enabled" in r.text


def test_the_control_page_says_measurement_continues_after_a_promotion(client):
    """The design's load-bearing sentence. Copy is part of the measurement."""
    r = client.get("/control/promotions")
    assert "Measuring does not stop" in r.text


def test_the_control_page_says_how_many_cells_each_table_drew_from(client):
    """"Best of N" is not a fact about the winner until N is on screen — and
    this table sits directly beside the checkbox that promotes the top row."""
    r = client.get("/control/promotions")
    assert "Drawn from" in r.text


def test_arming_a_rule_without_the_confirm_box_is_refused(client):
    """Arming changes what paid subscribers receive; disabling does not.

    The asymmetry is deliberate — a confirm on the safe direction only teaches
    the operator to click through both.
    """
    r = client.post(
        "/control/promotions/save",
        data={
            "setup_class": "LIQUIDITY_SWEEP_REVERSAL", "enabled": "1",
            "gates": "EXECUTION:OVEREXTENDED", "regimes": "*", "sessions": "*",
            "direction": "with_trend", "max_per_day": "25",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    # …and nothing was sent to the engine.
    assert not hasattr(client.app.state.engine_api, "_promotion_called")


def test_the_dark_feed_renders_the_promotion_panels(client):
    r = client.get("/signals/dark-live")
    assert r.status_code == 200
    assert "Promotion" in r.text
    assert "Promotion evidence" in r.text


def test_the_dark_feed_never_calls_a_promoted_row_delivered(client):
    """Enqueue is not dispatch, at the mechanism that adds rows to the queue."""
    r = client.get("/signals/dark-live")
    assert "Promoted is not delivered" in r.text


def test_the_delivery_filter_narrows_the_page(client):
    r = client.get("/signals/dark-live?delivery=promoted_delivered")
    assert r.status_code == 200


def test_the_export_takes_the_delivery_filter_and_stamps_the_column(client):
    """A download that cannot say which population it describes is the artifact
    the whole split exists to prevent."""
    r = client.get("/signals/dark-live/export.csv?delivery=promoted_delivered")
    assert r.status_code == 200
    header = r.text.splitlines()[0]
    for col in ("promoted", "delivery", "router_drop_reason"):
        assert col in header
    # One data row — the single delivered row in the fixture.
    assert len(r.text.strip().splitlines()) == 2


def test_the_form_works_without_javascript(client, monkeypatch):
    """The checkboxes post as repeated fields and are read as repeated fields.

    The first cut joined them into a hidden input with JS on submit. With JS
    off that posts no allow-lists at all — and because empty is fail-closed,
    the owner would get a rule saved, armed, and silently promoting nothing.
    A control that appears to work and does nothing is exactly the failure this
    repo already names, and here it wears the shape of a working promotion.
    """
    sent = {}

    async def _set(rule):
        sent.update(rule)
        stored = dict(rule)
        stored["inert"] = not (
            stored["gates"] and stored["regimes"] and stored["sessions"]
        )
        return {"ok": True, "rule": stored, "master_enabled": True}

    client.app.state.engine_api.set_dark_promotion = _set

    r = client.post(
        "/control/promotions/save",
        data={
            "setup_class": "LIQUIDITY_SWEEP_REVERSAL",
            "enabled": "1",
            "confirm": "1",
            # Repeated field — exactly what a plain HTML checkbox group posts.
            "gate_pick": [
                "SETUP_COMPAT:REGIME_STRONG_TREND", "EXECUTION:OVEREXTENDED",
            ],
            "regime_pick": ["*"],
            "session_pick": ["*"],
            "direction": "with_trend",
            "max_per_day": "25",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert sent["gates"] == [
        "SETUP_COMPAT:REGIME_STRONG_TREND", "EXECUTION:OVEREXTENDED",
    ]
    assert sent["regimes"] == ["*"]
    assert sent["direction"] == "with_trend"
    assert sent["enabled"] is True


def test_an_unticked_dimension_is_sent_empty_rather_than_widened(client):
    """Fail-closed, end to end.

    The route must never substitute a wildcard for a box the owner left blank —
    the engine reads empty as "matches nothing", and that is what makes a
    half-filled form inert instead of a promotion of everything the path emits.
    """
    sent = {}

    async def _set(rule):
        sent.update(rule)
        return {"ok": True, "rule": dict(rule, inert=True), "master_enabled": True}

    client.app.state.engine_api.set_dark_promotion = _set
    client.post(
        "/control/promotions/save",
        data={"setup_class": "MEAN_REVERT", "enabled": "1", "confirm": "1",
              "direction": "any", "max_per_day": "25"},
        follow_redirects=False,
    )
    assert sent["gates"] == []
    assert sent["regimes"] == []
    assert sent["sessions"] == []


def test_a_saved_but_inert_rule_is_not_reported_as_a_success(client):
    """The state an operator is most likely to misread as working."""
    async def _set(rule):
        return {"ok": True, "rule": dict(rule, inert=True), "master_enabled": True}

    client.app.state.engine_api.set_dark_promotion = _set
    client.post(
        "/control/promotions/save",
        data={"setup_class": "MEAN_REVERT", "enabled": "1", "confirm": "1",
              "direction": "any", "max_per_day": "25"},
        follow_redirects=False,
    )
    page = client.get("/control/promotions")
    assert "INERT" in page.text


# --------------------------------------------------------------------------- #
# Findability — the page has to be usable, not only correct
# --------------------------------------------------------------------------- #
#
# The first cut rendered every path's card expanded, each repeating the same
# explainers: 13 cards × (3 dimension paragraphs + 5 direction descriptions +
# the cap prose) came to 32 printed pages, and finding one path meant scrolling
# past twelve. Owner-caught 2026-08-12, from a PDF of the page.
#
# The guardrail copy is not cut — it is why the page can be trusted. It is said
# ONCE. These tests pin that split, because "the page got long again" is not
# something any other assertion would notice.


def _visible(body: str) -> str:
    """Rendered text, normalised.

    Asserting on raw HTML is brittle for reasons that say nothing about the
    page: Jinja escapes an apostrophe to ``&#39;``, and a sentence wrapped
    across two source lines carries a newline the reader never sees. Both bit
    the first cut of these tests, which is its own small version of the lesson
    — check what the reader sees, not what the template happens to emit.
    """
    import html as _html
    import re as _re

    out = _re.sub(r"<script.*?</script>", " ", body, flags=_re.S)
    out = _re.sub(r"<style.*?</style>", " ", out, flags=_re.S)
    out = _html.unescape(_re.sub(r"<[^>]+>", " ", out))
    return _re.sub(r"\s+", " ", out)


def test_the_explainers_are_said_once_not_once_per_path(client):
    """Prose that repeats per card is what made this page unreadable."""
    import re

    from app.routes import promotions as P

    text = _visible(client.get("/control/promotions").text)
    for phrase in (P.DIMENSION_COPY["gate"], P.DIRECTION_COPY["with_trend"]):
        norm = re.sub(r"\s+", " ", phrase)
        assert text.count(norm) == 1, (
            f"{norm[:45]!r} appears {text.count(norm)}x — repeated per path "
            f"again, which is the 32-page regression"
        )


def test_every_path_is_listed_in_one_index(client):
    """Find your path in one screen, without expanding anything.

    The index links through the FILTER (``?setup=``) rather than to an anchor,
    so clicking a path narrows the page to it instead of scrolling to a card
    with twelve others still below.
    """
    import re

    r = client.get("/control/promotions")
    listed = set(re.findall(r'href="\?setup=([A-Z_]+)"', r.text))
    assert {"LIQUIDITY_SWEEP_REVERSAL", "MEAN_REVERT"} <= listed
    # …and the index lists every path, not just the filtered one.
    filtered = client.get("/control/promotions?setup=MEAN_REVERT")
    assert set(re.findall(r'href="\?setup=([A-Z_]+)"', filtered.text)) == listed


def test_cards_are_collapsed_except_the_ones_that_carry_a_rule(client):
    """The thing you changed is the thing you want open."""
    import re

    r = client.get("/control/promotions")
    opened = set(re.findall(r'<details class="panel" id="p-([A-Z_]+)"[^>]*open', r.text))
    allc = set(re.findall(r'<details class="panel" id="p-([A-Z_]+)"', r.text))
    # The fixture arms exactly one rule.
    assert opened == {"LIQUIDITY_SWEEP_REVERSAL"}
    assert len(allc) > len(opened), "every card is expanded again"


def test_setup_query_opens_one_path(client):
    import re

    r = client.get("/control/promotions?setup=MEAN_REVERT")
    opened = set(re.findall(r'<details class="panel" id="p-([A-Z_]+)"[^>]*open', r.text))
    assert "MEAN_REVERT" in opened


def test_a_save_returns_to_the_card_it_changed(client):
    """PRG, scoped to the path — not the top of a long page the operator then
    has to search for the rule they just wrote."""
    async def _set(rule):
        return {"ok": True, "rule": dict(rule, inert=False), "master_enabled": True}

    client.app.state.engine_api.set_dark_promotion = _set
    r = client.post(
        "/control/promotions/save",
        data={"setup_class": "MEAN_REVERT", "direction": "any", "max_per_day": "25"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/control/promotions?setup=MEAN_REVERT"


def test_the_guardrail_copy_survived_the_simplification(client):
    """Shortening the page must not have removed why it can be trusted."""
    text = _visible(client.get("/control/promotions").text)
    for phrase in (
        "Measuring does not stop",
        "n, then campaigns, then the average",
        "FAILED_AUCTION_RECLAIM",
        "Drawn from",
        "changes what paid subscribers receive",
    ):
        assert phrase in text, f"lost the guardrail copy: {phrase!r}"


def test_the_filter_narrows_the_page_to_one_path(client):
    """`?setup=` shows that path only — the index above it still lists all."""
    import re

    r = client.get("/control/promotions?setup=MEAN_REVERT")
    cards = set(re.findall(r'<details class="panel" id="p-([A-Z_]+)"', r.text))
    assert cards == {"MEAN_REVERT"}, f"filter did not narrow the cards: {cards}"
    assert 'selected' in r.text


def test_filtering_to_a_path_with_no_rows_says_so(client):
    """"You picked a path with no rows" and "the book is empty" are different
    answers, and only one of them is true — so the filter must not silently
    fall back to showing everything."""
    r = client.get("/control/promotions?setup=NOT_A_REAL_PATH")
    assert r.status_code == 200
    text = _visible(r.text)
    assert "NO SUCH PATH" in text
    assert "NOT_A_REAL_PATH" in text
    # …and the index is still there so the reader can pick a real one.
    assert "LIQUIDITY_SWEEP_REVERSAL" in text


def test_the_index_counts_are_measured_on_the_whole_book_not_the_filter(client):
    """A selector applied to its own counts makes every option read
    "n = whatever I picked" (#90/#91)."""
    import re

    def counts(body):
        return dict(re.findall(r'([A-Z_]+) \(n=(\d+)\)', body))

    assert counts(client.get("/control/promotions").text) == \
           counts(client.get("/control/promotions?setup=MEAN_REVERT").text)


# --------------------------------------------------------------------------- #
# Path retirement — the same decision pointing the other way (live -> dark)
# --------------------------------------------------------------------------- #

_RETIRED = {
    "enabled": True, "count": 2, "is_default": True,
    "retired": [
        {"setup_class": "MOVER_TREND_PULLBACK", "side": "SHORT"},
        {"setup_class": "VOLUME_SURGE_BREAKOUT", "side": "*"},
    ],
    "default": [
        {"setup_class": "MOVER_TREND_PULLBACK", "side": "SHORT"},
        {"setup_class": "VOLUME_SURGE_BREAKOUT", "side": "*"},
    ],
}


def _promo_page(retirement="present", rows=None):
    """Render /control/promotions with a chosen path_retirement block."""
    snap = {
        "rules": [], "master_enabled": True, "dark_lane_enabled": True,
        "directions": ["ANY", "LONG", "SHORT", "WITH_TREND"], "any_token": "*",
    }
    if retirement == "present":
        snap["path_retirement"] = _RETIRED
    elif retirement == "off":
        snap["path_retirement"] = dict(_RETIRED, enabled=False)
    elif retirement == "empty":
        snap["path_retirement"] = {"enabled": True, "count": 0, "retired": [],
                                   "is_default": False, "default": []}
    elif retirement == "error":
        snap["path_retirement"] = {"error": "boom"}
    # "absent" -> no key

    from app.main import app

    with TestClient(app) as client:
        client.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})
        app.state.data_volume.dark_signals = lambda: {"schema": 2, "rows": rows or []}
        app.state.data_volume.dark_signals_provenance = lambda: {
            "exists": True, "mtime": 1_786_500_000.0, "age_sec": 30.0
        }

        async def _promos():
            return snap

        app.state.engine_api.dark_promotions = _promos
        r = client.get("/control/promotions")
        assert r.status_code == 200, r.status_code
        return r.text


def test_an_engine_without_the_mechanism_says_so():
    """"No gate exists" and "a gate exists and retires nothing" are different
    facts, and only one of them means a path could be being held back."""
    t = _visible(_promo_page("absent"))
    assert "NOT REPORTED" in t
    assert "predates" in t
    assert "ACTIVE" not in t


def test_an_unreadable_retirement_block_is_a_fault():
    t = _visible(_promo_page("error"))
    assert "UNREADABLE" in t and "boom" in t
    assert "NOT REPORTED" not in t


def test_the_retired_pairs_render_with_their_side():
    t = _visible(_promo_page("present"))
    assert "MOVER_TREND_PULLBACK" in t
    assert "VOLUME_SURGE_BREAKOUT" in t
    assert "diverted to dark" in t


def test_a_wildcard_side_reads_both_not_a_star():
    """`*` is our token, not a word. A reader should not have to know it."""
    t = _visible(_promo_page("present"))
    assert "both" in t


def test_the_switch_being_off_makes_every_entry_inert_on_screen():
    """A configured-but-disarmed retirement is the state most likely to be
    misread as working — it looks identical to an armed one in the list."""
    t = _visible(_promo_page("off"))
    assert "OFF" in t
    assert "inert" in t
    assert "diverted to dark" not in t


def test_nothing_retired_is_stated_rather_than_left_blank():
    t = _visible(_promo_page("empty"))
    assert "Nothing is retired" in t


def test_the_page_explains_why_it_diverts_rather_than_disables():
    """Copy is part of the measurement. Without this a reader assumes a retired
    path is switched off, and the whole reversibility argument is invisible."""
    t = _visible(_promo_page("present"))
    assert "earn its way back" in t
    assert "re-read on fresh evidence" in t


def test_a_changed_list_is_flagged_against_the_signed_off_default():
    t = _visible(_promo_page("present"))
    assert "changed from the signed-off default" not in t
    t2 = _visible(_promo_page("empty"))
    assert "changed from the signed-off default" in t2

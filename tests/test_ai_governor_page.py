"""`/signals/ai-governor` — reachable, honest, and pinned to the ENGINE's shape.

The engine ships this lane dark (measurement on, effect off), and a dark change
without its panel is unfinished. Two pages shipped with panels, tests and PR
bodies and **neither was in the navigation** — reachable only by typing the URL,
which is what the owner was reduced to. So the first assertion here is that a
reader can get to it.

The second is the one that has cost this repo more: the cross-repo contract is
driven against the **real engine payload**, not a fixture. An ops fixture puts
the block where the reader assumed it and then agrees with you about it — every
test green over a card that would render NOT REPORTED against the real engine.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import ai_governor as page  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


def _get(monkeypatch, path="/signals/ai-governor", diag=None, score=None) -> str:
    """Render the page against the ENGINE'S OWN payload by default.

    Not a hand-written dict: `build_diag` is imported from the engine and
    called, so a key this page reads that the engine stops publishing fails
    here rather than rendering a blank card in production.
    """
    payload = _engine_diag() if diag is None else diag
    score_payload = _engine_scorecard() if score is None else score

    async def fake_run(self, key, args=None):
        # Routed BY KEY: the page now makes two calls, and a fake that returns
        # one payload for both would hand the lane diag to the scorecard
        # classifier and grade a healthy page NOT REPORTED.
        if key == "read.ai_governor_scorecard":
            return {"ok": True, "key": key, "result": score_payload}
        return {"ok": True, "key": key, "result": payload}

    monkeypatch.setattr(EngineApiClient, "diag_run", fake_run)
    with TestClient(app) as client:
        _login(client)
        return client.get(path).text


def test_the_page_is_in_the_navigation():
    """A panel that renders perfectly on a page nobody can reach is exactly as
    useful as no panel — two lanes shipped that way and the owner was reduced
    to typing URLs.

    The NAV literal is parsed rather than a rendered page scraped, because
    `base.html` only expands the ACTIVE group's sub-links: asserting against
    `/` would pass for a page that is in no group at all. `tests/test_nav.py`
    derives the wider requirement (every literal page under `/signals/` is
    linked, no two labels or active keys collide, every destination is driven
    as a real request) and covers this page automatically — this assertion is
    the cheap direct one beside it.
    """
    nav = open("app/templates/base.html", encoding="utf-8").read()
    assert "('/signals/ai-governor', 'AI governor', 'ai_governor')" in nav


def test_the_page_renders(monkeypatch):
    assert "AI Trade Governor" in _get(monkeypatch)


def test_a_literal_route_is_registered_before_the_catch_all():
    """`signal_detail` owns `/signals/{signal_id}`, which matches any literal.
    A page included after it 404s while its route object sits in `app.routes`
    looking perfectly registered — the route list is not the authority."""
    import app.main as main

    src = open(main.__file__, encoding="utf-8").read()
    assert src.index("ai_governor.router") < src.index("signal_detail.router")


# ── The cross-repo contract, driven against the REAL engine ─────────────────

def _engine_diag() -> dict:
    """Call the engine's own `build_diag`, not a shape this repo invented."""
    import sys
    import pathlib

    engine = pathlib.Path(__file__).resolve().parents[2] / "360-v2"
    if not engine.exists():
        pytest.skip("engine repo not checked out beside ops")
    sys.path.insert(0, str(engine))
    try:
        from src.execution import ai_governor as gov  # type: ignore
        return gov.build_diag()
    finally:
        sys.path.remove(str(engine))


def _engine_scorecard():
    """The engine's REAL scorecard assembler, for the same reason as above."""
    import sys
    from pathlib import Path

    engine = Path(__file__).resolve().parents[2] / "360-v2"
    sys.path.insert(0, str(engine))
    try:
        from src.execution import ai_governor as gov

        return gov.build_scorecard()
    finally:
        sys.path.remove(str(engine))


def test_the_keys_this_page_reads_are_the_keys_the_engine_writes():
    """Pinned against the engine's real assembler.

    `zone_distance_atr` read a zone's edges by guessing five key names, none of
    which the only producer in the engine actually carries, and its two tests
    passed on a shape nothing has ever produced. The price-action lane card
    then repeated it one level up with the shape right and the PATH wrong.
    """
    diag = _engine_diag()
    for key in ("measure_enabled", "apply_enabled", "armed_arms", "provider",
                "provider_configured", "model_requested", "bounds", "health",
                "arms", "ledger_rows", "queue_depth", "rate_table_version"):
        assert key in diag, f"engine no longer publishes {key!r}"
    for key in ("cycles", "arms", "triggers", "calls", "verdicts", "applied",
                "spend_usd", "by_action", "refusals", "throttles"):
        assert key in diag["health"], f"engine no longer publishes health.{key!r}"
    for key in ("calls_per_signal", "calls_per_hour", "usd_per_day",
                "panic_max_positions", "panic_armed"):
        assert key in diag["bounds"], f"engine no longer publishes bounds.{key!r}"


def test_classify_grades_the_real_engine_payload_as_ok():
    assert page.classify({"ok": True, "result": _engine_diag()}) == page.STATE_OK


def test_an_engine_predating_the_entry_reads_as_not_reported_not_unreachable():
    """The engine ANSWERED and refused the key. Reading that as unreachable
    sends the operator to check a network that is fine."""
    assert page.classify({"ok": False, "error": "unknown catalog entry"}) == page.STATE_NOT_REPORTED
    assert page.classify({"error": "connect timeout"}) == page.STATE_UNREACHABLE
    assert page.classify(None) == page.STATE_UNREACHABLE


# ── The honesty rules ───────────────────────────────────────────────────────

def test_a_refusal_the_page_has_never_heard_of_is_badged_not_dropped():
    """Iterating this page's own copy table would be silent by construction on
    the next reason the engine adds."""
    rows = page.annotate({"a_brand_new_reason": 3}, page.REFUSAL_COPY)
    assert len(rows) == 1
    assert rows[0]["unclassified"] is True
    assert rows[0]["count"] == 3


def test_throttles_and_refusals_are_never_pooled():
    """`cooldown` means the lane found an arm it was willing to evaluate and
    deliberately did not — positive evidence it is working."""
    assert "cooldown" in page.THROTTLE_COPY
    assert "cooldown" not in page.REFUSAL_COPY


def test_the_page_publishes_no_blended_cross_arm_figure(monkeypatch):
    """One number over all four arms would move with the SL arm's refusal rate
    rather than with the mechanism."""
    body = _get(monkeypatch).lower()
    for banned in ("overall edge", "combined delta", "blended r", "avg_r"):
        assert banned not in body


def test_an_unset_panic_ceiling_renders_as_a_state_not_an_absent_row(monkeypatch):
    """The panic arm refuses while the ceiling is 0. A missing row would read as
    an arm that is simply quiet."""
    body = _get(monkeypatch)
    assert "Panic close" in body
    assert "UNSET" in body or "panic_max_positions" in body


def test_lane_state_separates_not_configured_from_off_and_from_working():
    """"Measuring but no key set" is the state this lane ships in, and it is
    neither working nor broken. Collapsing it into either sends the owner to
    fix the wrong thing."""
    assert page.lane_state({"measure_enabled": False}) == "off"
    assert page.lane_state({"measure_enabled": True, "provider_configured": False}) == "not_configured"
    assert page.lane_state({"measure_enabled": True, "provider_configured": True}) == "measuring"
    assert page.lane_state(
        {"measure_enabled": True, "provider_configured": True, "apply_enabled": True}
    ) == "enforcing"
    assert page.lane_state({}) == "unknown"


def test_a_partial_payload_renders_rather_than_500ing(monkeypatch):
    """An engine that publishes SOME of the block must not take the page down.

    `classify` short-circuits a wholly-absent payload to NOT REPORTED, so the
    dangerous case is the partial one: a build that reports `measure_enabled`
    and not yet `armed_arms`. This repo's convention is that a template adapts
    to the engine's shape rather than crashing on drift — the engine REST
    surface is the source of truth and ops follows it.
    """
    body = _get(monkeypatch, diag={"measure_enabled": True})
    assert "AI Trade Governor" in body
    assert "NOT CONFIGURED" in body or "MEASUREMENT OFF" in body


# ── The verdict must not outlive the reading (2026-09-03) ───────────────────
#
# The first load of this page in production rendered *"NOT REPORTED — the
# engine has no `read.ai_governor` catalog entry. That is an engine predating
# this page, so it is a deploy question."* over an engine that HAD the entry,
# was answering it, and listed it in the diag console one tab away. The diag
# bridge had timed out mid-cycle; the engine's own error string said so, and
# `classify` threw it away to print a cause this page cannot observe.
#
# `/invalidations`' WRITER STALE and `/dark-signals`' hardcoded ban cause, at
# the newest lane — and intermittent, so a reload shows data and the reader
# concludes nothing was ever wrong.

_BRIDGE_TIMEOUT = {
    "ok": False,
    "key": "read.ai_governor",
    "error": ("the engine did not answer within 20.0s — it may be mid-cycle "
              "or the snapshot loop may be stalled"),
    "request_id": "abc123",
}


def test_an_engine_that_answered_and_FAILED_is_not_called_a_missing_entry():
    assert page.classify(_BRIDGE_TIMEOUT) == page.STATE_ENGINE_ERROR


def test_only_an_unknown_key_reads_as_an_engine_predating_the_page():
    """The one error text that genuinely means a deploy question. Anything else
    with `ok: false` is the engine failing to answer a key it has."""
    assert page.classify({"ok": False, "error": "unknown catalog entry"}) == page.STATE_NOT_REPORTED
    assert page.classify({"ok": False, "error": "LookupError: unavailable"}) == page.STATE_ENGINE_ERROR
    assert page.classify({"ok": False, "error": ""}) == page.STATE_ENGINE_ERROR


def test_a_read_failure_quotes_the_engine_and_names_no_cause_of_its_own(monkeypatch):
    async def fake_run(self, key, args=None):
        return _BRIDGE_TIMEOUT

    monkeypatch.setattr(EngineApiClient, "diag_run", fake_run)
    with TestClient(app) as client:
        _login(client)
        html = client.get("/signals/ai-governor").text

    assert "READ FAILED" in html
    # The engine's own words, verbatim — a paraphrase is where the invented
    # cause got in.
    assert "the engine did not answer within 20.0s" in html
    # And NOT the verdict that sent a reader to check a deploy that was fine.
    assert "NOT REPORTED" not in html


def test_a_failure_with_no_reason_says_so_rather_than_inventing_one(monkeypatch):
    async def fake_run(self, key, args=None):
        return {"ok": False, "key": "read.ai_governor", "error": ""}

    monkeypatch.setattr(EngineApiClient, "diag_run", fake_run)
    with TestClient(app) as client:
        _login(client)
        html = client.get("/signals/ai-governor").text
    assert "The engine gave no reason" in html


# ── A payload key must not collide with a dict method ───────────────────────

def test_no_annotated_row_uses_a_key_that_shadows_a_dict_method():
    """Jinja resolves an attribute BEFORE an item, so a row key named `copy`,
    `keys`, `items` or `get` renders the builtin at the reader.

    This shipped: the throttle table's "What it means" column read
    `<built-in method copy of dict object at 0x7…>` in production, and the
    refusal table was one refusal away from doing the same. `/system/redis`
    paid for the identical collision on `keys`. Derived rather than a list of
    forbidden names, so the next key added is covered without anybody
    remembering.
    """
    rows = page.annotate({"cooldown": 749}, page.THROTTLE_COPY)
    assert rows, "annotate produced nothing to check"
    for row in rows:
        clash = set(row) & set(dir({}))
        assert not clash, f"row key(s) {clash} shadow a dict method in Jinja"


def test_the_throttle_table_renders_its_sentence_not_a_builtin(monkeypatch):
    diag = _engine_diag()
    diag["health"]["throttles"] = {"cooldown": 749}
    html = _get(monkeypatch, diag=diag)
    assert "built-in method" not in html
    assert "An arm was eligible and deliberately not evaluated" in html


# ── The provider's own words reach the page ─────────────────────────────────

def test_the_engine_publishes_the_failure_ring_this_page_reads():
    """A field one repo writes and no repo reads is the defect this lane keeps
    paying for; this is the same contract from the reading side."""
    assert "provider_failures" in _engine_diag()["health"]


def test_the_failure_ring_renders_the_vendors_words_and_the_token_columns(monkeypatch):
    diag = _engine_diag()
    diag["health"]["provider_status"] = {"bad_json": 9}
    diag["health"]["provider_failures"] = [{
        "at": 1756800000.0, "status": "bad_json",
        "detail": "content not JSON: Unterminated string starting at: line 1",
        "finish_reason": "MAX_TOKENS", "served_model": "gemini-3.7-flash-002",
        "output_tokens": 1174, "thinking_tokens": 1160,
        "max_output_tokens": 1174, "latency_ms": 1343,
    }]
    html = _get(monkeypatch, diag=diag)
    assert "MAX_TOKENS" in html
    assert "Unterminated string" in html
    # The token columns ARE the diagnosis: output at the ceiling with the
    # reasoning counted apart is a budget fault, not a prompt fault.
    assert "1174" in html and "1160" in html


def test_a_provider_that_did_not_say_why_renders_as_such_never_as_a_clean_stop(monkeypatch):
    diag = _engine_diag()
    diag["health"]["provider_status"] = {"timeout": 5}
    diag["health"]["provider_failures"] = [{
        "at": 1756800000.0, "status": "timeout", "detail": "TimeoutError: ",
        "finish_reason": "", "served_model": "", "output_tokens": 0,
        "thinking_tokens": 0, "max_output_tokens": 1174, "latency_ms": 20000,
    }]
    html = _get(monkeypatch, diag=diag)
    assert "did not say" in html


def test_an_engine_predating_the_ring_says_so_rather_than_reading_clean(monkeypatch):
    """No detail beside a non-zero failure count is a deploy question, not a
    healthy run — the two must not render identically."""
    diag = _engine_diag()
    diag["health"]["provider_status"] = {"bad_json": 9}
    diag["health"].pop("provider_failures", None)
    html = _get(monkeypatch, diag=diag)
    assert "No failure detail recorded" in html


# ── The shape production actually produced (2026-09-03) ─────────────────────
#
# Read off the live box through the diagnostic console: the run came back as
# `{"endpoint": "/internal/diag/catalog/run", "error": ""}` — ops' own
# transport wrapper, whose `str(httpx.ReadTimeout())` is the empty string. It
# is falsy, so `if payload.get("error")` treated a timeout as no error at all,
# and the payload was then graded on its SHAPE and called an engine predating
# the page. Two producers, one key, and only `ok` tells them apart.

_OPS_TIMEOUT = {"endpoint": "/internal/diag/catalog/run", "error": ""}


def test_an_ops_side_timeout_with_no_message_is_unreachable_not_a_missing_entry():
    assert page.classify(_OPS_TIMEOUT) == page.STATE_UNREACHABLE


def test_the_transport_envelope_is_told_from_the_engines_by_the_ok_key():
    """The engine's envelope carries `error` on SUCCESS too — empty — so
    truthiness cannot separate them and key presence alone would misread every
    successful read as a failure."""
    engine_ok = {"ok": True, "key": "read.ai_governor", "error": "",
                 "result": _engine_diag()}
    assert page.classify(engine_ok) == page.STATE_OK
    assert page.classify({"error": "connect timeout", "endpoint": "/x"}) == page.STATE_UNREACHABLE


def test_an_unreachable_page_quotes_the_client_and_names_a_blank_as_a_finding(monkeypatch):
    async def fake_run(self, key, args=None):
        return _OPS_TIMEOUT

    monkeypatch.setattr(EngineApiClient, "diag_run", fake_run)
    with TestClient(app) as client:
        _login(client)
        html = client.get("/signals/ai-governor").text
    assert "UNREACHABLE" in html
    assert "NOT REPORTED" not in html
    assert "No cause was reported" in html


def test_the_client_never_reports_a_failure_with_no_cause():
    """Fixed at the WRITER as well as at every reader: a timeout that carries
    no message still names itself, so the next page to read this envelope
    cannot inherit the same blank."""
    import httpx

    from app.data_sources.engine_api import _named_failure

    assert _named_failure(httpx.ReadTimeout("")) == "ReadTimeout (the client gave no message)"
    assert _named_failure(httpx.ReadTimeout("timed out")) == "timed out"


# ---------------------------------------------------------------------------
# D0 — blindness and the scorecard, pinned to the ENGINE'S real assembler
# ---------------------------------------------------------------------------


def test_the_engine_publishes_the_blindness_and_scorecard_blocks_this_page_reads():
    """The cross-repo contract, driven rather than fixtured.

    A fixture chooses a location and then agrees with you about it — every test
    green over a card that renders NOT REPORTED against the real engine. So
    these keys are asserted against `build_diag()` itself.
    """
    diag = _engine_diag()
    assert "blindness" in diag, "engine no longer publishes the blindness block"
    for key in ("rows", "measured"):
        assert key in diag["blindness"], f"blindness.{key!r} is gone"

    # The scorecard is its OWN entry. It parses the closed-signal record off
    # disk, and folded into the light entry it made `read.ai_governor` blow its
    # 25s budget in production while every other entry answered in 0.0s.
    assert "scorecard" not in diag, "the record parse must not ride the light entry"
    score = _engine_scorecard()
    for key in ("coverage", "mix", "selection", "arms", "shadow_note"):
        assert key in score, f"scorecard.{key!r} is gone"
    for arm in ("ADJUST_TP", "ADJUST_SL", "PANIC_CLOSE"):
        assert arm in score["arms"], f"arm {arm} must render even at n=0"


def test_an_empty_lane_renders_not_measured_rather_than_zero_percent_blind():
    """The flattering direction of this error is the dangerous one: 0% would
    report a fully-informed governor on a lane nobody has asked anything."""
    assert page.blindness_state({"rows": 0, "measured": False}) == "unmeasured"
    assert page.blindness_state({}) == page.STATE_NOT_REPORTED
    assert page.blindness_state(None) == page.STATE_NOT_REPORTED
    assert page.blindness_state({"rows": 5, "measured": True}) == "measured"


def test_the_blindness_card_says_not_measured_and_renders_no_figure(monkeypatch):
    """An unmeasured lane must publish no blindness number at all.

    Asserted on the card's STRUCTURE rather than on a substring: the copy
    explaining *why* there is no 0% naturally contains "0%", and a substring
    check would either fail on correct copy or force the sentence to be
    worse. Substring assertions rot; this one pins the property that actually
    holds — the unmeasured branch renders prose and no data table.
    """
    diag = _engine_diag()
    diag["blindness"] = {"rows": 0, "measured": False}
    html = _get(monkeypatch, diag=diag)
    card = html.split("Blindness")[-1].split("Scorecard")[0]
    assert "Not measured" in card
    assert "<table" not in card, "an unmeasured lane must render no figures at all"
    assert "Order-book blind" not in card and "Mean unknown fraction" not in card


def test_book_and_flow_blindness_are_rendered_apart_because_the_fixes_differ(monkeypatch):
    diag = _engine_diag()
    diag["blindness"] = {
        "rows": 10, "measured": True, "rows_with_split": 10,
        "avg_unknown_frac": 0.5, "fully_blind": 1,
        "book_blind": 9, "flow_blind": 1,
        "book_reasons": {"not_subscribed": 9}, "flow_reasons": {"stale": 1},
    }
    html = _get(monkeypatch, diag=diag)
    assert "Order-book blind" in html and "Flow (CVD) blind" in html
    assert "not_subscribed" in html and "stale" in html


def test_rows_predating_the_split_are_shown_as_their_own_count(monkeypatch):
    diag = _engine_diag()
    diag["blindness"] = {"rows": 10, "measured": True, "rows_with_split": 3,
                         "avg_unknown_frac": 0.5, "fully_blind": 0,
                         "book_blind": 1, "flow_blind": 0,
                         "book_reasons": {}, "flow_reasons": {}}
    html = _get(monkeypatch, diag=diag)
    assert "Carrying the book/flow split" in html
    assert "a missing stamp is not a pass" in html


def test_the_scorecard_leads_with_coverage_not_with_a_delta(monkeypatch):
    """A scorecard over the rows that happened to close is not a scorecard over
    the book, and a reader who sees the delta first will not go looking."""
    html = _get(monkeypatch)
    body = html.split("Scorecard")[-1]
    assert body.index("Read coverage first") < body.index("Selection")


def test_selection_is_never_labelled_as_an_effect(monkeypatch):
    html = _get(monkeypatch)
    assert "not an effect estimate" in html
    assert "counterfactual" in html.lower()


def test_the_page_publishes_no_blended_scorecard_figure(monkeypatch):
    """One number over four arms moves with the undecidable fraction rather
    than with the mechanism. It must not appear, at any level."""
    score = _engine_scorecard()
    assert "governor_edge" not in score
    assert "combined" not in score
    assert "avg_delta_pct" not in score, "no cross-arm delta"
    html = _get(monkeypatch)
    assert "no blended across-arm number" in html


def test_an_undecidable_reason_the_page_has_never_heard_of_is_badged_not_dropped(monkeypatch):
    """The table iterates the ENGINE'S payload and looks the sentence up.
    Iterating this page's own keys would be silent on the next reason."""
    html = _get(monkeypatch, score={
        "coverage": {}, "mix": {}, "selection": {},
        "arms": {"ADJUST_TP": {"n": 1, "decidable": 0,
                               "undecidable": {"a_reason_from_the_future": 1}}},
        "shadow_note": "x",
    })
    assert "a_reason_from_the_future" in html
    assert "unclassified" in html


def test_an_arm_with_no_rows_still_renders(monkeypatch):
    """A missing arm reads as one that never fired; those are opposite facts."""
    html = _get(monkeypatch)
    for arm in ("ADJUST_TP", "ADJUST_SL", "PANIC_CLOSE"):
        assert arm in html


def test_no_scorecard_row_uses_a_key_that_shadows_a_dict_method():
    """`row.copy` resolved to `dict.copy` and rendered a builtin at the reader
    once already. Derived, not a list of forbidden names."""
    rows = page.annotate({"no_pnl": 1}, page.UNDECIDABLE_COPY)
    for row in rows:
        assert not (set(row) & set(dir({}))), f"key shadows a dict method: {row}"


def test_every_undecidable_reason_the_engine_can_emit_has_copy():
    """A reason with no sentence renders unclassified, which is honest but
    useless. The engine's own vocabulary is the source of the requirement."""
    import sys
    from pathlib import Path

    engine = Path(__file__).resolve().parents[2] / "360-v2"
    if not engine.exists():
        import pytest as _pytest
        _pytest.skip("engine repo not checked out beside ops")
    sys.path.insert(0, str(engine))
    try:
        from src import ai_governor_score as sc
        reasons = {
            getattr(sc, name) for name in dir(sc)
            if name.startswith("WHY_") and isinstance(getattr(sc, name), str)
        }
    finally:
        sys.path.remove(str(engine))
    missing = reasons - set(page.UNDECIDABLE_COPY)
    assert not missing, f"no copy for engine reasons: {sorted(missing)}"


def test_a_failing_scorecard_does_not_take_the_rest_of_the_page_with_it(monkeypatch):
    """The whole point of the two-entry split.

    The scorecard parses the closed-signal record off disk; the arms, bounds and
    refusals do not. Fetched together, a slow or broken record took the lane's
    own state down with it — which is exactly what happened in production, where
    `read.ai_governor` blew its 25s budget while every other catalog entry
    answered in 0.0s.
    """
    async def fake_run(self, key, args=None):
        if key == "read.ai_governor_scorecard":
            return {"ok": False, "key": key, "error": "engine bridge timed out"}
        return {"ok": True, "key": key, "result": _engine_diag()}

    monkeypatch.setattr(EngineApiClient, "diag_run", fake_run)
    with TestClient(app) as client:
        _login(client)
        html = client.get("/signals/ai-governor").text

    # The scorecard says what went wrong, in the engine's own words.
    assert "could not compute the scorecard" in html
    assert "engine bridge timed out" in html
    # ...and everything that did not depend on it still rendered.
    assert "Blindness" in html
    assert "Open arms" in html
    assert "Bounds" in html


def test_the_scorecard_is_graded_on_its_own_shape_not_the_lanes(monkeypatch):
    """`classify` keys on `measure_enabled`, which only the lane entry carries.

    Running a scorecard payload through it would grade every healthy scorecard
    as an engine predating the page — the shape-vs-path defect this file already
    records twice, one line away from shipping again.
    """
    healthy = {"ok": True, "result": {"coverage": {}, "arms": {}}}
    assert page.classify_scorecard(healthy) == page.STATE_OK
    assert page.classify(healthy) == page.STATE_NOT_REPORTED, (
        "the lane classifier must NOT be what grades a scorecard"
    )
    assert page.classify_scorecard({"ok": False, "error": "unknown catalog entry: x"}) \
        == page.STATE_NOT_REPORTED
    assert page.classify_scorecard({"ok": False, "error": "boom"}) == page.STATE_ENGINE_ERROR
    assert page.classify_scorecard({"error": "", "endpoint": "/x"}) == page.STATE_UNREACHABLE
    assert page.classify_scorecard({"ok": True, "result": {"error": "no record"}}) \
        == page.STATE_ENGINE_ERROR

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


def _get(monkeypatch, path="/signals/ai-governor", diag=None, score=None,
         paired=None) -> str:
    """Render the page against the ENGINE'S OWN payload by default.

    Not a hand-written dict: `build_diag` is imported from the engine and
    called, so a key this page reads that the engine stops publishing fails
    here rather than rendering a blank card in production.
    """
    payload = _engine_diag() if diag is None else diag
    score_payload = STUB_SCORECARD if score is None else score
    paired_payload = _engine_paired() if paired is None else paired

    async def fake_run(self, key, args=None):
        # Routed BY KEY: the page makes THREE calls, and a fake that returned
        # one payload for all of them would hand the lane diag to the other two
        # classifiers and grade a healthy page NOT REPORTED. That is not
        # hypothetical — the paired card silently rendered its not-reported
        # branch under every test on this file until this branch was added,
        # because the default fell through to the lane payload.
        if key == "read.ai_governor_scorecard":
            return {"ok": True, "key": key, "result": score_payload}
        if key == "read.ai_governor_paired":
            return {"ok": True, "key": key, "result": paired_payload}
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

#: A scorecard shaped like the engine's, for tests about RENDERING rather than
#: about the contract. CI checks out this repo alone, so calling the real
#: assembler here would skip every render test — including ones that predate
#: this lane, which is how a stub requirement silently deletes coverage.
#:
#: It is kept honest by `test_the_stub_scorecard_matches_the_engines_shape`,
#: which drives the real assembler when the engine IS beside us and asserts the
#: keys agree. A fixture that nothing checks is one that agrees with whatever
#: you assumed — the defect this file already records twice.
STUB_SCORECARD: dict = {
    "coverage": {
        "theses": 0, "records": 0, "joined": 0,
        "still_open_or_undelivered": 0, "records_without_thesis": 0,
        "verdict_rows": 0, "record_error": None,
    },
    "mix": {},
    "blindness": {"theses_with_stamp": 0, "avg_unknown_frac": None, "fully_blind": 0},
    "selection": {
        "fee_pct": 0.07,
        "intervened": {"n": 0, "n_pnl": 0, "no_pnl": 0, "wins": 0, "losses": 0,
                       "avg_pnl_pct": None, "net_avg_pnl_pct": None},
        "maintain_only": {"n": 0, "n_pnl": 0, "no_pnl": 0, "wins": 0, "losses": 0,
                          "avg_pnl_pct": None, "net_avg_pnl_pct": None},
        "flip_flopped": 0,
    },
    "arms": {
        "ADJUST_TP": {"n": 0, "decidable": 0, "undecidable": {}, "reached": 0,
                      "unreached": 0, "avg_delta_pct": None},
        "ADJUST_SL": {"n": 0, "decidable": 0, "undecidable": {}, "why": "dark"},
        "PANIC_CLOSE": {"n": 0, "decidable": 0, "undecidable": {}, "why": "dark"},
    },
    "shadow_note": "Apply is OFF, so every recorded outcome is the MAINTAIN counterfactual.",
}


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
    if not engine.exists():
        pytest.skip("engine repo not checked out beside ops")
    sys.path.insert(0, str(engine))
    try:
        from src.execution import ai_governor as gov

        return gov.build_scorecard()
    finally:
        sys.path.remove(str(engine))


def _engine_paired():
    """The engine's REAL paired assembler, for the same reason as the two above.

    A fixture chooses a location and then agrees with you about it. The
    price-action lane card rendered NOT REPORTED against production with every
    ops test green, because the ops fixture put the block at the payload's top
    level and the engine nests it — the shape right and the PATH wrong.
    """
    import sys
    from pathlib import Path

    engine = Path(__file__).resolve().parents[2] / "360-v2"
    if not engine.exists():
        pytest.skip("engine repo not checked out beside ops")
    sys.path.insert(0, str(engine))
    try:
        from src import ai_governor_live as cf  # type: ignore

        return cf.build_diag()
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
    # The floor block, and it is at the TOP level rather than under `bounds` —
    # the price-action lane card rendered NOT REPORTED against a real engine
    # because an ops fixture put a block where the reader assumed it and then
    # agreed with itself about the location. Asserted where it actually lands.
    assert "verdict_age_floor" in diag, "engine no longer publishes the floor"
    assert "verdict_age_floor" not in diag["bounds"], (
        "the floor is top-level; if it moves under bounds this page reads a dash"
    )
    assert "sweep_period" in diag["health"], (
        "engine no longer publishes the achieved sweep interval"
    )


def test_the_floor_renders_and_names_a_bound_it_sits_under(monkeypatch):
    """The defect this block exists to make visible.

    Measured in production 2026-09-06, minutes after the engine deploy:
    `floor_sec` 10.83 against `bound_sec` 10.0 — the bound is unreachable, and
    the dominant term is the achieved sweep interval (7.98s) rather than the
    model round trip (2.85s). Those have opposite fixes. Before this block the
    page could only show that verdicts were late, never that they could not
    possibly be early.
    """
    payload = _engine_diag()
    payload["verdict_age_floor"] = {
        "measurable": True, "bound_sec": 10.0, "floor_sec": 10.83,
        "model_mean_sec": 2.851, "sweep_p50_sec": 7.979,
        "bound_below_floor": True, "headroom_sec": -0.83,
        "stale_frac": 0.0, "n": 4,
    }
    payload.setdefault("health", {})["verdict_age"] = {
        "n": 4, "stale_n": 0, "max_sec": 9.4, "samples": [],
    }
    payload["health"]["sweep_period"] = {"n": 120, "p50_sec": 7.979, "max_sec": 11.4}
    body = _get(monkeypatch, diag=payload)
    assert "the bound is BELOW the floor" in body
    # Scoped to the measured cadence, not asserted as a permanent property.
    # The floor tracks the monitor loop and the loop MOVES: measured 10.83s on
    # one window and 9.57s an hour later, flipping `bound_below_floor` from
    # true to false. An absolute caption over a moving measurement is the
    # constant-asserting-a-property-of-a-moving-system defect, and this page's
    # first cut carried one.
    assert "At the cadence measured right now" in body
    assert "Read the floor as a reading, not a constant" in body
    # Both terms on screen, because one is the provider's and one is ours.
    assert "Achieved sweep interval" in body
    assert "Model round trip" in body
    # The WORST interval beside the p50, because the bound sits inside the
    # spread and a median alone cannot show that.
    assert "worst" in body


def test_an_unmeasured_floor_is_not_a_floor_of_zero(monkeypatch):
    """Three states, not two. An unmeasured floor rendered as a clean one is
    the flattering direction of the same error, and it would make a bound look
    like it had headroom nobody has ever shown it to have."""
    payload = _engine_diag()
    payload["verdict_age_floor"] = {"measurable": False, "reason": "no_split_samples",
                                    "bound_sec": 10.0}
    payload.setdefault("health", {})["verdict_age"] = {
        "n": 1, "stale_n": 0, "max_sec": 1.0, "samples": [],
    }
    body = _get(monkeypatch, diag=payload)
    assert "Floor not yet measurable" in body
    assert "no_split_samples" in body
    assert "the bound is BELOW the floor" not in body


def test_an_engine_predating_the_floor_renders_the_page_anyway(monkeypatch):
    """A missing block is an older engine, never a floor of zero — and it must
    not take the rest of the card down with it."""
    payload = _engine_diag()
    payload.pop("verdict_age_floor", None)
    payload.setdefault("health", {})["verdict_age"] = {
        "n": 1, "stale_n": 0, "max_sec": 1.0, "samples": [],
    }
    body = _get(monkeypatch, diag=payload)
    assert "Verdict age" in body
    assert "the bound is BELOW the floor" not in body


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
    the book, and a reader who sees the delta first will not go looking.

    Anchored on the card's own HEADING, not on the bare word. It split on
    ``"Scorecard"`` until 2026-09-09, which silently assumed the word appears
    exactly once on the page and that everything after it belongs to this card
    — and the paired panel below broke both halves the moment its copy referred
    to this one by name. It went red rather than green, which is luck: a
    substring assertion of this shape can just as easily start passing over the
    wrong region. Narrowed rather than deleted, because the property it pins is
    real.
    """
    html = _get(monkeypatch)
    body = html.split("<h2>Scorecard")[-1]
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
    refusals do not. Fetched together, a slow or broken record would take the
    lane's own state down with it.

    Stated as the precaution it is: the production timeout that prompted the
    split was later measured to be engine warm-up, not the parse. The property
    below is still worth holding — the story first attached to it was not.
    """
    # The lane payload is built OUTSIDE the request. Calling a helper that can
    # `pytest.skip` from inside an ASGI handler raises into the test client's
    # portal ("This portal is not running") — a failure that reads like a
    # transport bug and is really a fixture in the wrong place.
    lane = {"measure_enabled": True, "apply_enabled": False, "armed_arms": ["tp"],
            "provider": "google", "provider_configured": True,
            "bounds": {"calls_per_signal": 8, "calls_per_hour": 30,
                       "usd_per_day": 0.0, "panic_max_positions": 0,
                       "panic_armed": False},
            "health": {"refusals": {}, "throttles": {}, "by_action": {}},
            "arms": [], "blindness": {"rows": 0, "measured": False}}

    async def fake_run(self, key, args=None):
        if key == "read.ai_governor_scorecard":
            return {"ok": False, "key": key, "error": "engine bridge timed out"}
        return {"ok": True, "key": key, "result": lane}

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


def test_the_stub_scorecard_matches_the_engines_shape():
    """Keeps `STUB_SCORECARD` honest.

    CI checks out this repo alone, so render tests run against a stub. A stub
    nothing checks is one that agrees with whatever you assumed — which is the
    `zone_distance_atr` failure and the price-action lane card, both of which
    went green over a shape no producer had ever emitted. When the engine IS
    beside us, this drives the real assembler and asserts the top-level keys
    and the arm names agree.
    """
    real = _engine_scorecard()  # skips when the engine is not checked out
    assert set(STUB_SCORECARD) == set(real), (
        "STUB_SCORECARD has drifted from the engine's scorecard shape"
    )
    assert set(STUB_SCORECARD["arms"]) == set(real["arms"])
    assert set(STUB_SCORECARD["coverage"]) >= set(real["coverage"]) - {"record_error"}


# ── Verdict age — the counter that could not name its own cause ─────────────


def test_the_verdict_age_block_is_where_the_engine_ACTUALLY_puts_it():
    """Driven against the engine's own assembler, not a shape this page hoped
    for. The price-action lane card shipped reading a block off the top level
    while the engine nested it under `derived` — every ops test green over a
    card that would render NOT REPORTED in production.
    """
    diag = _engine_diag()
    assert "verdict_age" in diag["health"], "the age block is under health"
    assert "verdict_max_age_sec" in diag["bounds"], (
        "the bound must travel with the payload — a duration with no threshold "
        "beside it cannot be read, and a second copy of the config here is the "
        "drifting mirror this repo has paid for under several names"
    )


def test_the_age_panel_renders_the_bound_beside_the_age(monkeypatch):
    diag = _engine_diag()
    diag["health"]["verdict_age"] = {
        "n": 18, "stale_n": 6, "max_sec": 41.2,
        "samples": [
            {"action": "MAINTAIN", "age_sec": 3.1, "stale": False},
            {"action": "ADJUST_SL", "age_sec": 41.2, "stale": True},
        ],
    }
    diag["bounds"]["verdict_max_age_sec"] = 10.0

    html = _get(monkeypatch, diag=diag)
    assert "Verdict age" in html
    assert "41.2" in html, "the oldest age is on screen"
    assert "aged out" in html, "and a late verdict is badged, not left to arithmetic"
    assert "ADJUST_SL" in html


def test_an_unmeasured_age_says_NOTHING_MEASURED_never_zero(monkeypatch):
    """Blank needs a cause before it gets a caption. `0.0s` over a lane that
    has never reached the apply path reads as a healthy clock."""
    diag = _engine_diag()
    diag["health"]["verdict_age"] = {"n": 0, "stale_n": 0, "max_sec": 0.0, "samples": []}

    html = _get(monkeypatch, diag=diag)
    assert "nothing measured" in html.lower()


def test_the_enforced_bound_leads_and_the_configured_floor_sits_beside_it(monkeypatch):
    """The floor is not what refuses a verdict. Rendering only the configured
    number would tell a reader a 15s verdict was late against a 30s bound.
    """
    diag = _engine_diag()
    diag["health"]["verdict_age"] = {
        "n": 8, "stale_n": 7, "max_sec": 20.1,
        "samples": [{"action": "ADJUST_SL", "age_sec": 20.1, "stale": True}],
    }
    diag["bounds"]["verdict_max_age_sec"] = 10.0
    diag["bounds"]["verdict_max_age_effective_sec"] = 30.0
    diag["bounds"]["observed_tick_sec"] = 20.0

    html = _get(monkeypatch, diag=diag)
    assert "Observed monitor tick" in html
    assert "Configured floor" in html
    assert "enforced" in html


def test_an_unmeasured_tick_says_so_rather_than_rendering_zero(monkeypatch):
    """`observed_tick_sec: None` is an engine that has not swept twice — not a
    loop running at 0s. Blank needs a cause before it gets a caption."""
    diag = _engine_diag()
    diag["health"]["verdict_age"] = {"n": 1, "stale_n": 0, "max_sec": 1.0, "samples": []}
    diag["bounds"]["observed_tick_sec"] = None

    html = _get(monkeypatch, diag=diag)
    assert "not measured yet" in html


# ── Would arming the effect flag do anything? ───────────────────────────────
#
# `AI_GOV_ARMS_ENABLED` defaults to `tp` alone, and the reasoning is sound on
# its own terms: the TP arm is the only one fully decidable from the
# closed-signal record. What nothing checked is whether the model ever chooses
# it. Live 2026-09-08: 90 verdicts, MAINTAIN 56, ADJUST_SL 34, ADJUST_TP zero
# — so arming apply would move 18 verdicts from `apply_off` to `arm_off` and
# change nothing else. The fault is an ABSENCE in one table read against a
# config echo in another, which is the one thing a table of counts cannot show.


def _reach_payload(**over):
    diag = _engine_diag()
    block = {
        "arms": [
            {"arm": "tp", "armed": True, "verdicts_seen": 0,
             "armed_and_never_chosen": True},
            {"arm": "sl", "armed": False, "verdicts_seen": 34,
             "armed_and_never_chosen": False},
            {"arm": "panic", "armed": False, "verdicts_seen": 0,
             "armed_and_never_chosen": False},
        ],
        "actionable_verdicts": 34,
        "reachable_verdicts": 0,
        "all_armed_arms_unchosen": True,
    }
    block.update(over)
    diag["arm_reachability"] = block
    return diag


def test_the_engine_publishes_the_reachability_join():
    """Driven against the engine's real assembler — a key this page reads that
    the engine does not write is #817 with the arrow reversed, and the
    producing side's own test passes either way."""
    diag = _engine_diag()
    assert "arm_reachability" in diag
    block = diag["arm_reachability"]
    for key in ("arms", "actionable_verdicts", "reachable_verdicts",
                "all_armed_arms_unchosen"):
        assert key in block, f"engine no longer publishes arm_reachability.{key!r}"
    assert {r["arm"] for r in block["arms"]} == {"tp", "sl", "panic"}


def test_the_live_shape_renders_as_a_fault(monkeypatch):
    body = _get(monkeypatch, diag=_reach_payload())
    assert "every armed arm has never been chosen" in body
    assert "armed and never chosen" in body


def test_every_arm_gets_a_row_even_with_no_verdicts(monkeypatch):
    """The engine's action counter creates a key when first incremented, so an
    arm nobody chose has no row rather than a zero one. A missing row reads as
    an arm that is simply quiet, which is the opposite fact."""
    body = _get(monkeypatch, diag=_reach_payload(
        arms=[{"arm": a, "armed": False, "verdicts_seen": 0,
               "armed_and_never_chosen": False} for a in ("tp", "sl", "panic")],
        actionable_verdicts=0, reachable_verdicts=0,
        all_armed_arms_unchosen=False))
    for arm in ("tp", "sl", "panic"):
        assert f"<code>{arm}</code>" in body


def test_no_actionable_verdict_is_quiet_not_blocked(monkeypatch):
    body = _get(monkeypatch, diag=_reach_payload(
        actionable_verdicts=0, reachable_verdicts=0,
        all_armed_arms_unchosen=False,
        arms=[{"arm": a, "armed": a == "tp", "verdicts_seen": 0,
               "armed_and_never_chosen": False} for a in ("tp", "sl", "panic")]))
    assert "quiet,\n    not blocked" in body or "quiet" in body
    assert "every armed arm has never been chosen" not in body


def test_an_engine_predating_the_join_renders_no_panel(monkeypatch):
    """Not an empty panel claiming everything is reachable — absent."""
    diag = _engine_diag()
    diag.pop("arm_reachability", None)
    body = _get(monkeypatch, diag=diag)
    assert "Would arming the effect flag do anything?" not in body


# ── The bound the page has always described ─────────────────────────────────


def test_the_engine_sends_the_two_bound_keys_this_page_renders(monkeypatch):
    """This page has read `verdict_max_age_effective_sec` and
    `observed_tick_sec` since it shipped and the engine sent neither, so it
    fell to its "not measured yet" branch under a paragraph promising a
    derivation — while the enforced bound stayed a flat constant sitting 4.1s
    below its own measured floor."""
    bounds = _engine_diag()["bounds"]
    for key in ("verdict_max_age_sec", "verdict_max_age_effective_sec",
                "verdict_max_age_source"):
        assert key in bounds, f"engine no longer publishes bounds.{key!r}"
    assert "observed_tick_sec" in bounds


def test_which_bound_actually_bound_is_on_screen(monkeypatch):
    diag = _engine_diag()
    # The card is gated on there being ages to read, like its sibling above —
    # a fresh `build_diag` has recorded no verdicts.
    diag["health"]["verdict_age"] = {
        "n": 8, "stale_n": 7, "max_sec": 20.1,
        "samples": [{"action": "ADJUST_SL", "age_sec": 20.1, "stale": True}],
    }
    diag["bounds"]["verdict_max_age_effective_sec"] = 30.0
    diag["bounds"]["observed_tick_sec"] = 20.0
    diag["bounds"]["verdict_max_age_source"] = "derived"
    body = _get(monkeypatch, diag=diag)
    assert "Which one bound" in body
    assert "widened to the slowest recent tick" in body


# --------------------------------------------------------------------------- #
# The paired card — the one comparison the scorecard cannot make
# --------------------------------------------------------------------------- #


def test_the_paired_keys_this_card_reads_are_the_keys_the_engine_writes():
    """Driven against the engine's real assembler, and asserting the PATH.

    `paired` is nested inside the payload, not at its top level. The
    price-action lane card put an engine block where the reader assumed it and
    every ops test agreed, over a card that rendered NOT REPORTED in
    production — the shape right and the path wrong. So this asserts where the
    keys actually land, including that they are NOT at the level a first guess
    would put them.
    """
    payload = _engine_paired()
    for key in ("enabled", "lane", "mechanism", "open", "resolved",
                "resolution", "edits", "paired", "dark_lane"):
        assert key in payload, f"engine no longer publishes {key!r}"
    block = payload["paired"]
    for key in ("per_arm", "untouched", "agreement_violations",
                "agreement_violation_rows", "unpairable", "rows_seen",
                "no_pooled_figure"):
        assert key in block, f"engine no longer publishes paired.{key!r}"
    # Nested, not top-level. The guess that would have shipped.
    assert "per_arm" not in payload
    assert "agreement_violations" not in payload
    # The pairable denominator lives on the RESOLUTION block, beside the other
    # arm-health counts, not inside `paired` — a reader of either has to be
    # able to find it where the engine actually puts it.
    assert "pairable" in payload["resolution"]


def test_the_paired_card_is_reachable_and_names_its_difference_from_selection(
    monkeypatch,
):
    """The whole point of the card is that it is not the panel above it.

    A reader who pools them gets the sign wrong: the selection split favours
    the touched population, and the only arm the model chooses can do nothing
    to a winner but clip it.

    Takes the FIXTURE, not a hand-built `pytest.MonkeyPatch()`. The first cut
    constructed one and never called `.undo()`, so `EngineApiClient.diag_run`
    stayed patched to a fake for the rest of the session and
    `tests/test_diag_run_timeout.py` — which reads that method's SOURCE — went
    red two files later. Every file passed in isolation; the failure existed
    only in the ordering the full suite produces, and it landed in somebody
    else's file, which is precisely the shape that reads as "not mine,
    pre-existing". Same class as `asyncio.run` in a test, one fixture over.
    """
    html = _get(monkeypatch)
    assert "Against the engine&#39;s own exit" in html or \
           "Against the engine's own exit" in html
    assert "paired on one row, not two populations" in html
    assert "selection" in html.lower()
    assert "Tightening a stop on a winner can only clip it" in html


def test_every_arm_renders_even_with_no_paired_rows(monkeypatch):
    """A missing arm reads as one that never fired, and those are opposite
    facts — the same reason `arm_reachability` exists two cards above."""
    html = _get(monkeypatch)
    body = html.split("Against the engine")[-1]
    for arm in ("ADJUST_SL", "ADJUST_TP", "PANIC_CLOSE"):
        assert arm in body, f"{arm} has no row in the paired card"


def test_the_paired_card_publishes_no_pooled_cross_arm_figure(monkeypatch):
    """One number across three mechanisms would move with whichever fired most
    rather than with any of them, and two of the three have never fired."""
    html = _get(monkeypatch)
    body = html.split("Against the engine")[-1]
    for banned in ("pooled delta", "combined delta", "overall delta",
                   "blended delta"):
        assert banned not in body.lower()


def test_an_agreement_violation_is_shouted_not_footnoted(monkeypatch):
    """A MAINTAIN-only signal edits nothing, so its two walks are required to
    agree exactly. A violation invalidates every delta on the card, so it has
    to lead and it has to be inspectable."""
    payload = _engine_paired()
    payload["paired"] = dict(payload["paired"])
    payload["paired"]["agreement_violations"] = 2
    payload["paired"]["agreement_violation_rows"] = [
        {"signal_id": "SIG-BAD", "symbol": "TESTUSDT", "delta_pct": 1.25},
    ]
    html = _get(monkeypatch, paired=payload)
    body = html.split("Against the engine")[-1]
    assert "every\n                delta below is suspect" in body or \
           "delta below is suspect" in body
    assert "SIG-BAD" in body


def test_a_clean_agreement_check_does_not_shout(monkeypatch):
    """The check renders whether or not it trips. A check that appears only
    when it fires teaches the reader that its absence means "fine" when it
    equally means the check stopped running."""
    html = _get(monkeypatch)
    body = html.split("Against the engine")[-1]
    assert "Walks that disagreed when they must not" in body
    assert "delta below is suspect" not in body


def test_an_unpairable_reason_the_page_has_never_heard_of_is_badged(monkeypatch):
    """The table iterates the ENGINE's payload and looks the sentence up.
    Iterating this page's own keys would be silent by construction on the next
    reason the engine adds — the drifting mirror, wearing yet another hat."""
    payload = _engine_paired()
    payload["paired"] = dict(payload["paired"])
    payload["paired"]["unpairable"] = {"some_future_reason": 3}
    html = _get(monkeypatch, paired=payload)
    body = html.split("Against the engine")[-1]
    assert "some_future_reason" in body
    assert "unclassified" in body


def test_every_unpairable_reason_the_engine_can_emit_has_copy():
    """A reason with no sentence renders badged rather than dropped, which is
    correct — but a reason the ENGINE already declares and this page has never
    been told about is an omission, not a future-proofing case."""
    import sys
    from pathlib import Path

    engine = Path(__file__).resolve().parents[2] / "360-v2"
    if not engine.exists():
        pytest.skip("engine repo not checked out beside ops")
    sys.path.insert(0, str(engine))
    try:
        from src import ai_governor_live as cf  # type: ignore

        declared = {
            getattr(cf, name) for name in dir(cf) if name.startswith("UNPAIRED_")
        }
    finally:
        sys.path.remove(str(engine))
    missing = declared - set(page.UNPAIRABLE_COPY)
    assert not missing, f"unpairable reasons with no copy on this page: {missing}"


def test_a_switched_off_lane_says_so_rather_than_rendering_empty_tables(monkeypatch):
    """Zero rows because the lane is off, and zero rows because nothing has
    closed yet, are different facts with different next moves."""
    payload = _engine_paired()
    payload["enabled"] = False
    html = _get(monkeypatch, paired=payload)
    body = html.split("Against the engine")[-1]
    assert "The lane is switched off" in body
    assert "AI_GOV_LIVE_ARMS_ENABLED" in body


def test_an_engine_predating_the_paired_entry_renders_a_deploy_question(monkeypatch):
    """`not_reported` is an engine that has never heard of the key, and only
    the engine's own marker means that. Every other `ok: false` is the engine
    failing to answer a key it HAS — a different next move."""
    async def fake_run(self, key, args=None):
        if key == "read.ai_governor_paired":
            return {"ok": False, "key": key, "error": "unknown catalog entry"}
        return {"ok": True, "key": key, "result": _engine_diag()}

    monkeypatch.setattr(EngineApiClient, "diag_run", fake_run)
    with TestClient(app) as client:
        _login(client)
        html = client.get("/signals/ai-governor").text
    body = html.split("Against the engine")[-1]
    assert "read.ai_governor_paired" in body
    assert "deploy question" in body


def test_a_paired_read_that_times_out_is_not_read_as_a_missing_entry(monkeypatch):
    """The 2026-09-03 defect, at the third entry. `str(ReadTimeout())` is `""`,
    so a transport failure carries a falsy `error` and no `ok` — and grading it
    on shape would tell the owner to check a deploy that is fine."""
    async def fake_run(self, key, args=None):
        if key == "read.ai_governor_paired":
            return {"endpoint": "/internal/diag/catalog/run", "error": ""}
        return {"ok": True, "key": key, "result": _engine_diag()}

    monkeypatch.setattr(EngineApiClient, "diag_run", fake_run)
    with TestClient(app) as client:
        _login(client)
        html = client.get("/signals/ai-governor").text
    body = html.split("Against the engine")[-1]
    assert "Could not reach the engine" in body
    assert "deploy question" not in body


def test_the_three_classifiers_key_on_three_different_shapes():
    """`classify` keys on `measure_enabled`, `classify_scorecard` on
    `coverage`, `classify_paired` on `paired`. Running one payload through
    another's classifier grades a healthy lane as an engine predating the page
    — the shape-vs-path defect, and it was one line away twice."""
    lane = {"ok": True, "result": {"measure_enabled": True}}
    score = {"ok": True, "result": {"coverage": {}}}
    pair = {"ok": True, "result": {"paired": {}}}

    assert page.classify(lane) == page.STATE_OK
    assert page.classify_scorecard(score) == page.STATE_OK
    assert page.classify_paired(pair) == page.STATE_OK

    assert page.classify_paired(lane) == page.STATE_NOT_REPORTED
    assert page.classify_paired(score) == page.STATE_NOT_REPORTED
    assert page.classify(pair) == page.STATE_NOT_REPORTED

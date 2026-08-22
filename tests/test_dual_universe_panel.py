"""`/pairs` — the dual-universe card, and the tab that hid half the universe.

Owner, 2026-08-22: *"if something is moved to promoted pairs, but by volume it
should still keep in regular pairs too, so one pair can be there in two
universes"*.

Ops had the display half of the engine's defect, with the same tell — a comment
about *synthetically-admitted* movers sitting over a branch that skipped
**every** promoted symbol from the Regular list. On the live box that was 163 of
a 330-pair universe hidden behind a tab reading "Regular (167)", so the owner
was reading a regular universe roughly half its real size while asking why
nothing fired on the regular pairs.

The engine-side assembler test lives in `360-v2/tests/test_dual_universe.py`;
this file covers what ops renders and the states it must keep apart.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app.data_sources.engine_api import EngineApiClient  # noqa: E402

_ENGINE = Path(__file__).resolve().parents[2] / "360-v2"


@contextmanager
def _client(payload, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    async def _fake(self):  # noqa: ANN001
        return payload

    monkeypatch.setattr(EngineApiClient, "pairs", _fake)
    with TestClient(app) as c:
        c.post("/login", data={"password": "test-token"})
        yield c


def _card(body: str) -> str:
    """Just the dual-universe card.

    Substring assertions over a whole page are the rot this repo keeps paying
    for: "MEASURING ONLY" also belongs to the retention card and "in both"
    appears in this card's own heading, so a page-wide `in` check answers a
    question about a different element. Slice to the card and assert inside it.
    """
    start = body.index("Dual universe")
    end = body.index("</section>", start)
    return body[start:end]


def _payload(**kw):
    base = {
        "regular": [
            {"symbol": "BTCUSDT", "tier": "TIER1", "volume_24h_usd": 9e8,
             "change_24h_pct": 1.0, "change_24h_signed_pct": 1.0,
             "also_promoted": False, "universe_role": ""},
            {"symbol": "LTCUSDT", "tier": "TIER1", "volume_24h_usd": 4e8,
             "change_24h_pct": 18.0, "change_24h_signed_pct": 18.0,
             "also_promoted": True, "universe_role": "dual_core"},
        ],
        "promoting": [
            {"symbol": "LTCUSDT", "universe_role": "dual_core", "minutes_left": 210.0,
             "volume_24h_usd": 4e8, "change_24h_pct": 18.0},
            {"symbol": "NEWUSDT", "universe_role": "mover_only", "minutes_left": 300.0,
             "volume_24h_usd": 4e6, "change_24h_pct": 41.0},
        ],
        "regular_count": 2,
        "promoting_count": 2,
        "dual_count": 1,
        "updated_at": "2026-08-22T07:09:40Z",
        "ignition": {},
        "retention": {},
        "dual_universe": {
            "enabled": False,
            "min_volume_usd": 50000000.0,
            "universe_size": 330,
            "promoted": 163,
            "by_role": {"dual_core": 133, "mover_only": 30},
            "symbols": {"dual_core": ["LTCUSDT", "DOTUSDT"], "mover_only": ["NEWUSDT"]},
            "symbols_truncated": {"dual_core": 131, "mover_only": 0},
            "dual_candidates": 133,
            "dual_share_of_universe": 0.403,
            "withheld_evaluators": [f"_evaluate_x{i}" for i in range(15)],
        },
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# The card's states — three, never two
# ---------------------------------------------------------------------------

def test_the_card_renders_the_census(monkeypatch):
    with _client(_payload(), monkeypatch) as c:
        card = _card(c.get("/pairs").text)
    assert "MEASURING ONLY" in card
    assert "133" in card, "the count of narrowed core pairs must be on screen"
    assert "dual_core" in card


def test_an_engine_predating_the_census_reads_not_reported(monkeypatch):
    """Absent is not "nothing is dual" — that second reading is a claim."""
    payload = _payload()
    payload.pop("dual_universe")
    with _client(payload, monkeypatch) as c:
        body = c.get("/pairs").text
    assert "NOT REPORTED" in body
    assert "predating it" in body


def test_an_unreadable_census_says_so_rather_than_rendering_zeroes(monkeypatch):
    with _client(_payload(dual_universe={"error": "boom"}), monkeypatch) as c:
        body = c.get("/pairs").text
    assert "UNREADABLE" in body
    assert "boom" in body


def test_the_effect_flag_state_is_read_off_the_payload(monkeypatch):
    """Never mirrored from a copy of the engine's config."""
    payload = _payload()
    payload["dual_universe"] = dict(payload["dual_universe"], enabled=True)
    with _client(payload, monkeypatch) as c:
        card = _card(c.get("/pairs").text)
    assert "ENFORCING" in card
    assert "MEASURING ONLY" not in card, (
        "the effect flag must follow the payload, not another card's badge"
    )


# ---------------------------------------------------------------------------
# What the card must NOT claim
# ---------------------------------------------------------------------------

def test_the_card_states_it_cannot_say_how_many_more_signals(monkeypatch):
    """The evaluators it names are the ones that did not run.

    Anything measured from these rows is survivorship-biased by construction —
    the same limit the setup_tf census carries, and it belongs on screen rather
    than in a footnote.
    """
    with _client(_payload(), monkeypatch) as c:
        body = c.get("/pairs").text
    assert "cannot say how" in body


def test_the_volume_floor_is_named_as_an_existing_boundary(monkeypatch):
    with _client(_payload(), monkeypatch) as c:
        body = c.get("/pairs").text
    assert "MIDCAP boundary" in body
    assert "50M" in body


def test_a_bounded_symbol_list_says_the_count_is_not_bounded(monkeypatch):
    """The list is capped for the payload; the number above it is not."""
    with _client(_payload(), monkeypatch) as c:
        body = c.get("/pairs").text
    assert "+131 more" in body
    assert "the count above is not" in body


def test_a_role_ops_has_never_heard_of_renders_under_its_own_name(monkeypatch):
    """`MEASUREMENT_SUFFIXES` wearing another hat — never drop the unknown."""
    payload = _payload()
    payload["dual_universe"] = dict(
        payload["dual_universe"],
        by_role={"dual_core": 2, "some_future_role": 7},
    )
    with _client(payload, monkeypatch) as c:
        body = c.get("/pairs").text
    assert "some_future_role" in body
    assert "unclassified" in body


# ---------------------------------------------------------------------------
# The overlap the tab used to hide
# ---------------------------------------------------------------------------

def test_a_dual_pair_appears_under_regular_and_the_overlap_is_declared(monkeypatch):
    with _client(_payload(), monkeypatch) as c:
        regular = c.get("/pairs?tab=regular").text
    assert "LTCUSDT" in regular, "a core pair must not vanish while promoted"
    assert "1 in both" in regular, (
        "the two tab counts overlap by construction — a reader adding them "
        "must be told, not left to overshoot the universe silently"
    )


def test_a_mover_only_pair_stays_out_of_the_regular_tab(monkeypatch):
    """The opposite error: a pair the mover path invented is not a regular pair."""
    with _client(_payload(), monkeypatch) as c:
        regular = c.get("/pairs?tab=regular").text
        promoting = c.get("/pairs?tab=promoting").text
    # NEWUSDT is only in the promoting list of the payload, so it must appear
    # there and not in the regular table.
    assert "NEWUSDT" in promoting
    assert "mover_only" in promoting


def test_no_overlap_badge_when_nothing_is_dual(monkeypatch):
    """A badge that renders unconditionally teaches nothing."""
    payload = _payload(dual_count=0)
    payload["regular"] = [payload["regular"][0]]
    with _client(payload, monkeypatch) as c:
        body = c.get("/pairs?tab=regular").text
    # The badge itself, not the card heading that also contains the phrase.
    assert "0 in both" not in body
    assert 'class="muted small" title="A pair in both lists' not in body


# ---------------------------------------------------------------------------
# The cross-repo contract — the key is where the engine actually puts it
# ---------------------------------------------------------------------------

def _engine_reachable() -> str:
    if not _ENGINE.is_dir():
        return "engine repo not checked out beside ops"
    sys.path.insert(0, str(_ENGINE))
    try:
        import src.api.snapshot  # noqa: F401
        return ""
    except ImportError as exc:
        return f"engine deps unavailable in this environment: {exc}"
    finally:
        sys.path.remove(str(_ENGINE))
        for mod in [m for m in sys.modules if m.startswith(("src.", "config"))]:
            sys.modules.pop(mod, None)


_SKIP = _engine_reachable()


@pytest.mark.skipif(bool(_SKIP), reason=_SKIP or "")
def test_the_engine_puts_the_census_where_this_page_reads_it():
    """Drive the REAL `collect_pairs_live` and assert the key's location.

    A fixture chooses a location and then agrees with you about it — that is
    exactly how the price-action lane card shipped reading `derived` off the
    top level and passing every ops test against a card that would have
    rendered NOT REPORTED in production.
    """
    sys.path.insert(0, str(_ENGINE))
    try:
        from src.api.snapshot import collect_pairs_live

        class _Info:
            volume_24h_usd = 4e8
            tier = "TIER1"
            volatility_24h = 18.0
            change_24h_signed_pct = 18.0

        class _PairMgr:
            pairs = {"LTCUSDT": _Info(), "BTCUSDT": _Info()}

        import src.scanner as scanner_mod

        sc = scanner_mod.Scanner.__new__(scanner_mod.Scanner)
        sc._mover_promoted_pairs = {"LTCUSDT": 1.0}
        sc._synthetic_mover_pairs = set()
        sc.pair_mgr = _PairMgr()

        class _Engine:
            _scanner = sc
            pair_mgr = _PairMgr()
            _channels: list = []
            _mover_ignition = None

        payload = collect_pairs_live(_Engine())
    finally:
        sys.path.remove(str(_ENGINE))
        for mod in [m for m in sys.modules if m.startswith(("src.", "config"))]:
            sys.modules.pop(mod, None)

    assert "dual_universe" in payload, "the key this page reads must exist"
    assert "dual_count" in payload
    # …and the fields the template reads must be the ones the engine writes.
    census = payload["dual_universe"]
    for key in (
        "enabled", "by_role", "dual_candidates", "withheld_evaluators",
        "symbols", "symbols_truncated", "min_volume_usd", "universe_size",
    ):
        assert key in census, f"template reads {key!r} and the engine does not write it"

    regular = {r["symbol"] for r in payload["regular"]}
    assert "LTCUSDT" in regular, "the dual pair must be in the Regular list"
    assert any(r.get("also_promoted") for r in payload["regular"])

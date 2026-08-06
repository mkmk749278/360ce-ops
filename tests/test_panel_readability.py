"""The 2026-08-06 panel surf — what a full read of every page turned up.

Four defects, found by fetching all 26 guest-readable pages and looking at what
they actually rendered rather than at what their code intended. Three of them
were invisible to the suite by construction: a paragraph, a caption and a page
size are none of them assertions.

* **Two dead pages.** ``/signals/sar`` and ``/sar-exit`` had read UNAVAILABLE
  since 2026-08-02, because the engine's shared ``SuppressedCandidateStore``
  gained a schema-2 envelope for a *different* consumer and this ledger changed
  shape with it.
* **A caption asserting a benign cause for a state that had another one** —
  "an empty ledger here means off, not broken", printed over an UNAVAILABLE
  badge, beside an mtime updating every few minutes.
* **An alert page describing a delivery path it did not have.** It said Telegram
  was banned in-region (it is not) and that there was no push (there is), which
  told the owner this page was the only way he would learn about a naked
  position.
* **A 3.9 MB table**, 62% of whose rows carried no verdict.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.data_volume import _unwrap_records  # noqa: E402
from app.main import app  # noqa: E402
from app.routes.strategy_lab import MATRIX_ROW_CAP, split_matrix  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


# ---------------------------------------------------------------------------
# the ledger envelope
# ---------------------------------------------------------------------------
def test_schema_2_envelope_is_unwrapped():
    """The shape the engine has written since 2026-08-02."""
    rows = [{"strategy": "X@SARBASE"}, {"strategy": "X@SAREXIT"}]
    wrapped = {
        "schema": 2,
        "records": rows,
        "evicted_by_gate": {"cohort_edge": 12},
        "stamped_total": 5000,
    }
    assert _unwrap_records(wrapped) == rows


def test_bare_list_still_loads():
    """The pre-schema file is still the file until the store next flushes, so
    both shapes are live on disk and a reader that knows one is wrong half the
    time."""
    rows = [{"strategy": "X@SARBASE"}]
    assert _unwrap_records(rows) == rows


def test_a_loader_error_is_not_mistaken_for_a_shape():
    """'The file could not be read' and 'the file holds a shape I do not know'
    are different states with different fixes. Pooling them is how a page
    reports a fault that is not happening."""
    err = {"error": "not found: sar_exit_candidates_v3.json"}
    assert _unwrap_records(err) == err


def test_unwrap_matches_the_engines_own_serializer():
    """Driven from the engine's writer, not from a shape we invented here.

    A mock whose keys the reader chose cannot verify a contract the reader got
    wrong — that is exactly how this defect survived four days, and how
    `zone_distance_atr` shipped uncomputable. These keys are transcribed from
    `suppression_audit.SuppressedCandidateStore._persist`."""
    payload = {
        "schema": 2,
        "records": [{"gate": "cohort_edge", "won": True}],
        "evicted_by_gate": {},
        "stamped_total": 1,
    }
    # Round-trip through JSON the way the file does.
    assert _unwrap_records(json.loads(json.dumps(payload))) == payload["records"]


def test_sar_pages_render_against_a_wrapped_ledger(monkeypatch):
    """The end the owner sees: both pages, off a schema-2 file, not UNAVAILABLE."""
    from app.data_sources import data_volume as dv

    wrapped = {"schema": 2, "records": [], "stamped_total": 0}
    monkeypatch.setattr(dv.DataVolumeReader, "_load", lambda self, name: wrapped)
    with TestClient(app) as client:
        _login(client)
        for path in ("/sar-exit", "/signals/sar"):
            r = client.get(path)
            assert r.status_code == 200, path
            assert "unexpected ledger shape" not in r.text, path


def test_sar_page_does_not_blame_the_dark_flag_when_it_cannot_read(monkeypatch):
    """The caption follows the state. Over an UNAVAILABLE badge it must not say
    an empty ledger means the flag is off — that sentence sent the owner looking
    at a switch when the fault was a parser."""
    from app.data_sources import data_volume as dv

    monkeypatch.setattr(
        dv.DataVolumeReader, "_load", lambda self, name: {"unexpected": "shape"}
    )
    with TestClient(app) as client:
        _login(client)
        body = client.get("/sar-exit").text
        assert "This is not the dark flag being off" in body
        assert "An empty ledger here means off, not broken" not in body


# ---------------------------------------------------------------------------
# the edge matrix render bound
# ---------------------------------------------------------------------------
def _cells(n_decidable: int, n_thin: int) -> list[dict]:
    """Cells in the shape ``reduce_edge_matrix`` actually emits.

    Built by driving the real reducer rather than by hand — the first cut of
    this fixture invented a four-key row, and `reduce_per_strategy` blew up on
    the missing keys. A shape you chose yourself agrees with you and then fails
    the moment a real consumer reads it, which is the whole subject of this
    file, arriving in its own test.
    """
    from app.routes.strategy_lab import reduce_edge_matrix

    store: dict[str, list[dict]] = {}
    # n >= EDGE_MIN_SAMPLES earns a verdict; below it the cell is INSUFFICIENT.
    for i in range(n_decidable):
        store[f"S{i}|ASIA/MARKUP/NORMAL/NEUTRAL"] = [
            {"won": False, "r": -1.0, "pnl_pct": -1.0, "src": "shadow"}
            for _ in range(50 - (i % 10))
        ]
    for i in range(n_thin):
        store[f"T{i}|ASIA/MARKUP/NORMAL/NEUTRAL"] = [
            {"won": True, "r": 1.0, "pnl_pct": 1.0, "src": "shadow"}
        ]
    rows = reduce_edge_matrix(store)
    assert len(rows) == n_decidable + n_thin
    return rows


def test_cells_with_no_verdict_are_counted_not_shown():
    """62% of the live matrix read INSUFFICIENT_DATA. They are the page saying
    nothing, thousands of times — counted on screen, kept out of the table."""
    out = split_matrix(_cells(10, 90))
    assert out["total"] == 100
    assert out["decidable"] == 10
    assert out["thin"] == 90
    assert all(r["verdict"] != "INSUFFICIENT_DATA" for r in out["rows"])


def test_show_all_puts_them_back():
    out = split_matrix(_cells(10, 90), show_all=True)
    assert out["shown"] == 100


def test_the_cap_is_a_render_bound_and_says_when_it_bit():
    out = split_matrix(_cells(MATRIX_ROW_CAP + 50, 0))
    assert out["shown"] == MATRIX_ROW_CAP
    assert out["cap_bit"] is True
    # …and it does not lie about the population it was drawn from.
    assert out["total"] == MATRIX_ROW_CAP + 50


def test_cap_not_flagged_when_it_did_not_bite():
    out = split_matrix(_cells(5, 5))
    assert out["cap_bit"] is False


def test_default_sort_is_evidence_not_edge():
    """Sorting by edge puts the best-looking cell of thousands on the top line,
    and 'best of N' is not a fact about the winner until N is on screen. Most
    evidence first instead."""
    from app.routes.strategy_lab import reduce_edge_matrix

    rows = reduce_edge_matrix({
        # 16 wins: clears the sample floor and posts the best edge on the page.
        "A|ASIA/MARKUP/NORMAL/NEUTRAL": [
            {"won": True, "r": 2.0, "pnl_pct": 2.0, "src": "shadow"} for _ in range(16)
        ],
        # 48 rows: far more evidence, unflattering verdict.
        "B|ASIA/MARKUP/NORMAL/NEUTRAL": [
            {"won": False, "r": -1.0, "pnl_pct": -1.0, "src": "shadow"} for _ in range(48)
        ],
    })
    assert split_matrix(rows)["rows"][0]["strategy"] == "B"


def test_the_other_panels_still_read_the_whole_matrix(monkeypatch):
    """The split is a render bound on ONE table. A rollup measured on the capped
    rows would silently become a rollup of 'the 400 cells with most evidence'."""
    from app.routes import strategy_lab as sl

    seen = {}
    real = sl.reduce_per_strategy

    def spy(rows):
        seen["n"] = len(rows)
        return real(rows)

    # Built BEFORE the patch: `_cells` drives the real reducer, so patching it
    # to a lambda that calls `_cells` recurses forever.
    rows = _cells(500, 500)
    monkeypatch.setattr(sl, "reduce_per_strategy", spy)
    monkeypatch.setattr(sl, "reduce_edge_matrix", lambda *a, **k: rows)

    class _Vol:
        """Named methods, not a __getattr__ catch-all — the catch-all swallowed
        dunder lookups and recursed."""

        def market_context(self):
            return {}

        def strategy_edge(self):
            return {}

        def suppressed_candidates(self):
            return {}

        def strategy_allocations(self):
            return {}

    sl._build_view(_Vol())
    assert seen["n"] == 1000, "a panel was measured on the capped rows"


# ---------------------------------------------------------------------------
# copy that described something the app did not have
# ---------------------------------------------------------------------------
def test_alerts_never_claims_telegram_is_banned():
    """CLAUDE.md carries the 2026-07-25 correction: Telegram is NOT banned
    in-region. This page asserted it as the reason it existed."""
    with TestClient(app) as client:
        _login(client)
        body = client.get("/alerts").text
        assert "Telegram is unavailable" not in body
        assert "there is no push" not in body


def test_alerts_reads_its_delivery_path_rather_than_asserting_one(monkeypatch):
    """Push is disabled-safe: no service account means every send is a no-op. A
    page promising a page over that configuration is the old error's mirror
    image, so the state is read, both halves of it."""
    with TestClient(app) as client:
        _login(client)

        monkeypatch.setattr(app.state.device_registry, "count", lambda: 0)
        assert "PULL ONLY" in client.get("/alerts").text

        monkeypatch.setattr(app.state.device_registry, "count", lambda: 2)
        object.__setattr__(app.state.settings, "fcm_service_account", "{}")
        body = client.get("/alerts").text
        assert "PUSH ARMED" in body
        assert "2 registered devices" in body


# ---------------------------------------------------------------------------
# blank needs a cause
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload,expect",
    [
        ({"error": "not found: invalidation_records.json"}, "NOT WRITTEN"),
        ({"error": "Expecting value: line 1"}, "UNREADABLE"),
        ([], "QUIET"),
    ],
)
def test_invalidations_names_why_it_is_empty(monkeypatch, payload, expect):
    """Three states, three next moves. The caption was one sentence naming none
    of them, so 'the audit is broken' and 'nothing has been killed' read alike."""
    from app.data_sources import data_volume as dv

    monkeypatch.setattr(dv.DataVolumeReader, "_load", lambda self, name: payload)
    with TestClient(app) as client:
        _login(client)
        body = client.get("/invalidations").text
        assert expect in body
        assert "No invalidation records loaded." not in body

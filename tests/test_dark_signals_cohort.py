"""Which signals reach the Dark Signals panel — the cohort layer.

``_fetch_signals`` decides the population every aggregate on the page is
computed over.  It was entirely uncovered, which means the window filter, the
payload-shape tolerance and the error surface were all unverified.

Two things here matter more than line coverage:

* **A window filter that silently drops rows is indistinguishable from a
  quiet market.**  If the timestamp parse fails, the row must be *kept*, not
  discarded — an unparseable date is missing metadata, not evidence the
  signal falls outside the window.  Dropping it shrinks the denominator with
  no cause shown, which is the "blank needs a cause before it gets a caption"
  failure.
* **An engine error must surface as an error**, not as an empty cohort.  Zero
  rows and "could not fetch" render identically on a panel unless the error
  is carried through, and the owner reads the first as "nothing traded".
"""
from __future__ import annotations

import asyncio
import time
import types
from datetime import datetime, timedelta

from app.routes.dark_signals import _fetch_signals, _perf_to_signal


def _req(*, api_payload=None, perf_payload=None):
    class _API:
        async def signals(self, status=None, limit=None):
            return api_payload

    class _Vol:
        def signal_performance(self):
            return perf_payload

    return types.SimpleNamespace(
        app=types.SimpleNamespace(
            state=types.SimpleNamespace(engine_api=_API(), data_volume=_Vol())
        )
    )


def _fetch(req, window):
    return asyncio.run(_fetch_signals(req, window))


def _perf_row(sid: str, *, age_days: float = 0.0, **kw) -> dict:
    ts = time.time() - age_days * 86400
    row = {
        "signal_id": sid,
        "symbol": "BTCUSDT",
        "direction": "long",
        "entry": 100.0,
        "outcome_label": "tp_hit",
        "pnl_pct": 1.5,
        "setup_class": "BREAKOUT_RETEST",
        "entry_regime": "TREND",
        "confidence": 70.0,
        # Real records carry both: the window filter prefers the terminal
        # stamp, the row mapper reads the dispatch/create chain.
        "terminal_outcome_timestamp": ts,
        "dispatch_timestamp": ts,
    }
    row.update(kw)
    return row


# ---------------------------------------------------------------------------
# Live window — payload shape tolerance
# ---------------------------------------------------------------------------


def test_live_accepts_a_bare_list_payload() -> None:
    rows, err = _fetch(_req(api_payload=[{"signal_id": "A"}]), "live")
    assert err is None
    assert [r["signal_id"] for r in rows] == ["A"]


def test_live_accepts_items_and_signals_envelopes() -> None:
    """The engine REST surface is the source of truth; ops adapts to it."""
    for key in ("items", "signals"):
        rows, err = _fetch(_req(api_payload={key: [{"signal_id": "A"}]}), "live")
        assert err is None
        assert len(rows) == 1


def test_live_drops_entries_with_no_usable_id() -> None:
    """A row with no id cannot be joined to a replay result."""
    payload = [{"signal_id": "A"}, {"symbol": "BTCUSDT"}, {"id": "B"}]
    rows, _ = _fetch(_req(api_payload=payload), "live")
    assert len(rows) == 2


def test_live_ignores_non_dict_entries() -> None:
    rows, _ = _fetch(_req(api_payload=[{"signal_id": "A"}, "junk", None]), "live")
    assert len(rows) == 1


def test_engine_error_is_surfaced_not_flattened_to_empty() -> None:
    """Empty-because-broken must not render as empty-because-quiet."""
    rows, err = _fetch(_req(api_payload={"error": "engine unreachable"}), "live")
    assert rows == []
    assert err == "engine unreachable"


def test_data_volume_error_is_surfaced() -> None:
    rows, err = _fetch(_req(perf_payload={"error": "no such file"}), "7d")
    assert rows == []
    assert err == "no such file"


def test_unexpected_perf_shape_is_empty_without_a_false_error() -> None:
    """Not-a-list is a shape we don't understand, not a reportable failure."""
    rows, err = _fetch(_req(perf_payload={"unexpected": True}), "7d")
    assert rows == []
    assert err is None


# ---------------------------------------------------------------------------
# The window filter
# ---------------------------------------------------------------------------


def test_window_excludes_rows_older_than_its_span() -> None:
    payload = [_perf_row("recent", age_days=1.0), _perf_row("old", age_days=10.0)]
    rows, _ = _fetch(_req(perf_payload=payload), "7d")
    assert [r["signal_id"] for r in rows] == ["recent"]


def test_all_window_applies_no_time_filter() -> None:
    payload = [_perf_row("recent", age_days=1.0), _perf_row("ancient", age_days=400.0)]
    rows, _ = _fetch(_req(perf_payload=payload), "all")
    assert len(rows) == 2


def test_unparseable_timestamp_keeps_the_row() -> None:
    """Bad metadata must not silently shrink the denominator.

    Dropping here would remove the row from every aggregate on the page with
    no cause shown — a data fault reported as a quiet market.
    """
    payload = [_perf_row("bad", terminal_outcome_timestamp="not-a-date")]
    rows, _ = _fetch(_req(perf_payload=payload), "24h")
    assert [r["signal_id"] for r in rows] == ["bad"]


def test_missing_timestamp_keeps_the_row() -> None:
    payload = [{"signal_id": "nots", "symbol": "BTCUSDT", "direction": "long"}]
    rows, _ = _fetch(_req(perf_payload=payload), "24h")
    assert [r["signal_id"] for r in rows] == ["nots"]


def test_timestamp_falls_back_through_the_field_chain() -> None:
    """Older records carry create_timestamp rather than a terminal one."""
    payload = [
        {"signal_id": "old-schema", "symbol": "BTCUSDT",
         "create_timestamp": time.time() - 10 * 86400},
    ]
    rows, _ = _fetch(_req(perf_payload=payload), "7d")
    assert rows == []  # correctly aged out via the fallback field


def test_non_dict_perf_records_are_skipped() -> None:
    rows, _ = _fetch(_req(perf_payload=[_perf_row("a"), "junk", 7]), "all")
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Record mapping
# ---------------------------------------------------------------------------


def test_perf_record_maps_onto_the_tracker_shape() -> None:
    out = _perf_to_signal(_perf_row("S1"))
    assert out["signal_id"] == "S1" and out["id"] == "S1"
    assert out["direction"] == "LONG"        # normalised upward
    assert out["status"] == "TP_HIT"
    assert out["entry"] == out["entry_price"] == 100.0
    # ISO-8601 with an explicit UTC offset — the template parses this.
    parsed = datetime.fromisoformat(out["timestamp"])
    assert parsed.utcoffset() == timedelta(0)


def test_regime_falls_back_when_entry_regime_absent() -> None:
    out = _perf_to_signal({"signal_id": "S", "regime": "RANGE"})
    assert out["entry_regime"] == "RANGE"


def test_missing_setup_class_becomes_unknown_not_none() -> None:
    """``None`` would bucket separately from the UNKNOWN group downstream."""
    out = _perf_to_signal({"signal_id": "S"})
    assert out["setup_class"] == "UNKNOWN"


def test_terminal_only_record_passes_the_window_but_maps_no_timestamp() -> None:
    """Pins a known asymmetry between the two timestamp chains.

    The window filter reads ``terminal_outcome_timestamp`` first; the row
    mapper reads only ``dispatch_timestamp``/``create_timestamp``/
    ``timestamp``.  A record carrying *just* the terminal stamp therefore
    survives the window and then renders with no created-time.

    Not currently reachable — real performance records carry a dispatch or
    create stamp too — so this pins the behaviour rather than asserting a
    bug.  If the record schema ever drops those fields, this test turning
    into a visible blank on the panel is the thing to notice.
    """
    ts = time.time()
    rows, _ = _fetch(
        _req(perf_payload=[{"signal_id": "T", "terminal_outcome_timestamp": ts}]),
        "24h",
    )
    assert len(rows) == 1
    assert rows[0]["timestamp"] is None

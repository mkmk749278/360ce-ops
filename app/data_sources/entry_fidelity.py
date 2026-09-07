"""Entry fidelity — the recorded book beside the same exits priced from the tape.

A **labelled port** of ``360-v2/src/entry_fidelity.py``. Ops carries the
arithmetic locally because it must run over the whole ledger — including rows
the engine process has long since forgotten — and it is pinned against the real
engine module by ``tests/test_entry_fidelity_contract.py``, which drives
``entry_fidelity.rebase`` itself rather than a fixture of what it might return.
That is the standing rule here: ops ports the engine's math, it does not invent
it, and where a port exists a test drives the original.

What it is for. ``entry`` on a closed-signal record is the close of the candle
the evaluator triggered on, and every number the engine publishes about a trade
divides by it — ``pnl_pct``, MFE, MAE, R, the app's signal card, this page. The
order goes out seconds later, and on a continuation setup price has usually kept
moving in the signal's direction over those seconds, so the stamped entry is
systematically better than the price that was available and the difference is
booked as profit.

Measured 2026-09-07 against Binance's own USD-M 1m tape, 605 of the 652 rows
closed in the 30 days to 09-06: mean drift **+0.226%**, already moved with the
trade on **67%** of rows, and this page's **+0.342% / trade (+207.1%)** is
**+0.118% / trade (+71.1%)** when the same exits are priced from the tape —
about **+0.048%** net of a 0.07% round trip. ``0.342 − 0.226 = 0.116`` against a
measured ``0.118``: the drift IS the gap, and there is nothing else in it.

Three rules this surface keeps:

* **Beside, never instead** (owner, 2026-09-07). Every recorded figure on this
  page stays exactly what it was. The rebased book is a second column and a
  second panel, and there is deliberately no blended third number — one figure
  over both would move with the coverage rate rather than with the market.
* **Coverage leads.** The observation is knowable exactly once, so rows closed
  before the engine stamp shipped can never be rebased. They are counted under
  a named refusal, never folded in as zero drift, and while the window behind
  the stamp fills, a small ``priced`` count is the honest state and not a fault.
* **A frozen mover close is refused, not used.** A symbol that has left the scan
  universe serves a stale candle; rebasing onto it manufactures a drift that is
  a fact about our feed rather than about the market.

Nothing here is a gate, and one obvious gate was priced and withdrawn the same
day: refusing signals whose mark had already moved >0.5% past the stamped entry
drops 11% of rows carrying **−91.4% of book PnL** and **+20.0% of real PnL**.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

REFUSAL_NO_ENTRY = "no_entry"
REFUSAL_NO_OBSERVATION = "no_observation"
REFUSAL_STALE_OBSERVATION = "stale_observation"

#: What each refusal means, in the operator's words. The table iterates the
#: DATA and looks a sentence up here — a reason this page has never heard of
#: renders under its raw name rather than vanishing, because iterating ops' own
#: keys is silent by construction on the next reason the engine adds.
REFUSAL_COPY = {
    REFUSAL_NO_ENTRY: (
        "No usable entry price on the record — a producer fault, and the row "
        "cannot be priced either way."
    ),
    REFUSAL_NO_OBSERVATION: (
        "Closed before the engine stamped what it first observed. There is no "
        "backfill: the price that existed at dispatch is knowable once."
    ),
    REFUSAL_STALE_OBSERVATION: (
        "The only price available was a frozen candle from a symbol that had "
        "left the scan universe. Rebasing onto it would measure our feed."
    ),
}


def _is_long(direction: Any) -> bool:
    return str(getattr(direction, "value", direction)).upper() == "LONG"


def signed_drift_pct(
    entry: Optional[float], observed: Optional[float], direction: Any
) -> Optional[float]:
    """Percent the market had already moved, signed toward the trade.

    Positive means price was already past the stamped entry in the direction
    the trade wanted — the move the book collects and the trade never had.
    """
    if not entry or entry <= 0 or not observed or observed <= 0:
        return None
    raw = (observed - entry) / entry * 100.0
    return raw if _is_long(direction) else -raw


def implied_exit_price(
    entry: Optional[float], pnl_pct: Optional[float], direction: Any
) -> Optional[float]:
    """The exit LEVEL behind a recorded row.

    A stop, a target or a parked breakeven is a real price, so it survives
    rebasing untouched — only the entry is in question.
    """
    if not entry or entry <= 0 or pnl_pct is None:
        return None
    factor = (
        1.0 + (pnl_pct / 100.0) if _is_long(direction) else 1.0 - (pnl_pct / 100.0)
    )
    price = entry * factor
    return price if price > 0 else None


def rebased_pnl_pct(
    observed: Optional[float], exit_price: Optional[float], direction: Any
) -> Optional[float]:
    if not observed or observed <= 0 or not exit_price or exit_price <= 0:
        return None
    raw = (exit_price - observed) / observed * 100.0
    return raw if _is_long(direction) else -raw


def rebase_record(rec: dict) -> dict:
    """``{drift_pct, rebased_pnl_pct, refusal}`` for one closed-signal record.

    Exactly one of the numbers or the refusal is populated, so a caller that
    reads a value without checking gets ``None`` and never a plausible zero.
    """
    entry = rec.get("entry")
    pnl = rec.get("pnl_pct")
    observed = rec.get("first_observed_price")
    direction = rec.get("direction") or ""
    if not entry or float(entry) <= 0:
        return {"drift_pct": None, "rebased_pnl_pct": None, "refusal": REFUSAL_NO_ENTRY}
    if not observed or float(observed) <= 0:
        return {
            "drift_pct": None,
            "rebased_pnl_pct": None,
            "refusal": REFUSAL_NO_OBSERVATION,
        }
    if bool(rec.get("first_observed_stale")):
        return {
            "drift_pct": None,
            "rebased_pnl_pct": None,
            "refusal": REFUSAL_STALE_OBSERVATION,
        }
    exit_price = implied_exit_price(float(entry), None if pnl is None else float(pnl), direction)
    return {
        "drift_pct": signed_drift_pct(float(entry), float(observed), direction),
        "rebased_pnl_pct": rebased_pnl_pct(float(observed), exit_price, direction),
        "refusal": None,
    }


def _median(values: list) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def summarise(rows: Iterable[dict]) -> dict:
    """Census over the rows the page is SHOWING, filters already applied (#90).

    A panel computed over the whole ledger above a filtered table is not a
    summary of anything the reader is looking at, so this takes the same list
    the table renders.
    """
    priced: list[dict] = []
    refusals: dict[str, int] = {}
    total = 0
    for row in rows:
        total += 1
        reason = row.get("rebase_refusal")
        if reason:
            refusals[reason] = refusals.get(reason, 0) + 1
            continue
        priced.append(row)
    n = len(priced)
    out: dict[str, Any] = {
        "rows": total,
        "priced": n,
        "coverage_pct": round(100.0 * n / total, 1) if total else 0.0,
        "refusals": [
            {
                "reason": reason,
                "n": count,
                # NOT ``copy``: Jinja resolves ``row.copy`` to ``dict.copy``
                # and renders the builtin at the reader. This repo has paid for
                # that collision twice — ``/system/redis`` on ``keys`` and the
                # AI-governor throttle table on ``copy`` itself — and I hit it a
                # third time here, caught by RENDERING the page rather than by a
                # test, because dict access in Python works perfectly.
                "meaning": REFUSAL_COPY.get(reason, ""),
                "known": reason in REFUSAL_COPY,
            }
            for reason, count in sorted(refusals.items(), key=lambda kv: -kv[1])
        ],
    }
    if not n:
        return out
    drifts = [r["entry_drift_pct"] for r in priced if r.get("entry_drift_pct") is not None]
    book = [r["pnl_pct"] for r in priced if r.get("pnl_pct") is not None]
    real = [
        r["rebased_pnl_pct"] for r in priced if r.get("rebased_pnl_pct") is not None
    ]
    if drifts:
        out["drift_mean_pct"] = sum(drifts) / len(drifts)
        out["drift_median_pct"] = _median(drifts)
        out["drift_positive_pct"] = 100.0 * sum(1 for d in drifts if d > 0) / len(drifts)
    if book:
        out["book_avg_pct"] = sum(book) / len(book)
        out["book_total_pct"] = sum(book)
    if real:
        out["rebased_avg_pct"] = sum(real) / len(real)
        out["rebased_total_pct"] = sum(real)
    # Win rates are stated on both books because the drift can carry a row
    # across zero — the count that changes is the thing worth seeing, and a
    # single blended rate would hide exactly that.
    if book:
        out["book_win_pct"] = 100.0 * sum(1 for p in book if p > 0) / len(book)
    if real:
        out["rebased_win_pct"] = 100.0 * sum(1 for p in real if p > 0) / len(real)
    return out


def mfe_floor(rows: Iterable[dict]) -> dict:
    """How many rows read ``+0.00%`` MFE, and how many of those are the FLOOR.

    ``max_favorable_excursion_pct`` starts at 0.0 and only ratchets, so it
    cannot be negative: a trade whose best moment was −0.4% is recorded
    identically to one that printed exactly its entry and to one nobody priced.
    The engine now stamps an unclamped ``peak_pnl_pct`` beside it, and the two
    read together are the detector — MFE 0.00 with a negative peak is a floor,
    in one row. Rows without the unclamped peak predate the stamp and cannot
    say, which is its own count rather than a zero.
    """
    total = zero = stamped = floor = 0
    for row in rows:
        total += 1
        mfe = row.get("mfe_pct")
        is_zero = mfe is not None and float(mfe) == 0.0
        if is_zero:
            zero += 1
        peak = row.get("peak_pnl_pct")
        if peak is None:
            continue
        stamped += 1
        if is_zero and float(peak) < 0.0:
            floor += 1
    return {
        "rows": total,
        "zero_mfe": zero,
        "zero_mfe_pct": round(100.0 * zero / total, 1) if total else 0.0,
        "with_peak": stamped,
        "floor_rows": floor,
        "floor_pct": round(100.0 * floor / stamped, 1) if stamped else None,
    }

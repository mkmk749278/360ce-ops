"""Structural snap — what the geometry would have been if it read levels.

Added 2026-08-04 alongside engine ``src/structural_snap.py``, for the owner's
question *"what actually price action is, are we using it?"*.

The short answer the audit produced: the engine reads structure into its
**score** and almost never into its **triggers**, and not at all into its
**targets**.  `MOVER_TREND_PULLBACK` is 59% of the enqueued book and its
geometry is a moving-average stop with TPs at fixed 1.0/1.6/2.5 R-multiples,
so TP1 lands wherever the multiplication puts it.

A repair for exactly that has existed in ``structural_levels.py`` since it was
written, and ``build_channel_signal`` called it — behind a guard on
``candle_highs is not None`` that no caller in the engine ever satisfied, under
a comment claiming every evaluator passed the arrays.  It never ran.  It now
runs at ``Scanner._enqueue_signal``, stamping by default and **applying
nothing** until a path is named in the ops allow-list.

What this page is careful about
-------------------------------
* **Two arms, published separately, with no combined figure.**  The TP1 snap
  moves nearer only, which ``max_favorable_excursion_pct`` settles outright.
  The SL snap moves either way, and two of its cases are not in the record at
  all — a wider stop on a loser (the walk ended at the stop) and a tighter stop
  on a winner (MFE and MAE carry no ordering between them).  Those are refused
  under **separate** names because they remove opposite ends of the
  distribution, and a reader who cannot see that cannot judge the residue.
  There is deliberately no blended "would the snap have helped" number; a test
  asserts the key does not exist.
* **Decidable fraction sits beside every delta.**  Not in a footnote.  A
  loss-selected sample is worse than no sample because it looks like an answer.
* **The baseline is measured on the rows the arm scored**, never over the whole
  ledger — a summary computed on a different population than the table beside
  it is not a summary of anything the reader is looking at (#90).
* **The mode is read off the rows**, never mirrored from a copy of the engine's
  flag registry.  ``MEASUREMENT_SUFFIXES`` drifted for a week; the fix for a
  drifting mirror is one writer and one reader.
* **PnL % leads and nothing here divides by a stop.**  ``signal_dispatch``
  sizes at a fixed notional, so R equalises nothing (owner, 2026-08-02).
* **Refusals are named, never pooled into "no data".**  ``tf_unknown`` means a
  new evaluator needs a declared timeframe; ``short_series`` means the buffer,
  not the market; ``no_candles`` means the store.  Different fixes.
* **The ring is capped and says so.**  ``evicted`` rides with the data from the
  engine, because a bounded buffer whose cap is invisible turns every rate on
  this page into a sample nobody knows is a sample.

Route ordering: this must be registered **before** ``signal_detail.router``,
whose ``/signals/{signal_id}`` matches any literal under ``/signals/``.  A page
that 404s while its route object sits in ``app.routes`` looking registered is a
debugging cost this repo has already paid; ``tests/test_structural_snap.py``
pins the ordering.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query, Request

from app.data_sources.structural_snap import (
    V_NO_EXCURSION,
    V_NO_OUTCOME,
    V_UNCHANGED,
    V_UNDECIDABLE_ORDER,
    V_UNDECIDABLE_TRUNC,
    build_report,
    setups_present,
)
from app.reports import csv_response

router = APIRouter()

#: Plain-language captions for each verdict. A status with no cause gets a
#: caption invented for it, and this page has to say which of "not yet",
#: "cannot be", and "unchanged" a row is in.
VERDICT_COPY: dict[str, str] = {
    V_UNCHANGED: (
        "No structural level fell inside the search band, so this arm would "
        "have shipped the identical level. Counted, because a rule that "
        "changes nothing has not been tested however good its delta looks."
    ),
    V_NO_OUTCOME: (
        "Stamped at enqueue but never joined to a closed signal — the router "
        "dropped it, or it is still open. Not a fault."
    ),
    V_NO_EXCURSION: (
        "Joined, but the closed record carries no MFE/MAE, so no "
        "counterfactual is computable. Records written before excursion "
        "tracking existed sit here permanently."
    ),
    V_UNDECIDABLE_ORDER: (
        "SL only: a winner that drew down past the tighter snapped stop. "
        "Whether the drawdown came before or after the target is not in the "
        "record — MFE and MAE carry no ordering between them. Refused rather "
        "than guessed; guessing here removes winners and flatters tightening."
    ),
    V_UNDECIDABLE_TRUNC: (
        "SL only: a loser under a wider snapped stop. Whether price would "
        "have come back is unknowable — the walk ended at the stop it hit. "
        "Refused; guessing here removes losers and flatters widening."
    ),
}


def _load(request: Request, setup: str) -> tuple[Any, Optional[str], list]:
    dv = request.app.state.data_volume
    try:
        ledger = dv.structural_snap()
        perf = dv.signal_performance()
    except Exception as exc:  # noqa: BLE001
        return None, f"engine data volume unavailable: {exc}", []
    options = setups_present(ledger)
    return build_report(ledger, perf, setup_class=setup), None, options


@router.get("/signals/structural-snap")
async def structural_snap_page(request: Request, setup: str = Query("")):
    report, error, setup_options = _load(request, setup)
    return request.app.state.templates.TemplateResponse(
        "structural_snap.html",
        {
            "request": request,
            "error": error or (report.error if report else ""),
            "report": report,
            "filter_setup": setup,
            # Counted with every filter applied except its own, so the selector
            # describes the whole ledger rather than only the chosen option.
            "setup_options": setup_options,
            "verdict_copy": VERDICT_COPY,
            # Row cap is a RENDER bound and is applied after filtering, never
            # inside the reducer — truncating first starves the rarest rows
            # hardest, which is #97.
            "row_cap": 300,
        },
    )


@router.get("/signals/structural-snap/export.csv")
async def structural_snap_export(request: Request, setup: str = Query("")):
    report, _error, _options = _load(request, setup)
    cols = [
        "signal_id", "symbol", "setup_class", "direction", "tf",
        "arm", "verdict", "level_source", "shift_pct",
        "actual_pnl_pct", "arm_pnl_pct", "delta_pct",
    ]
    rows: list[list[Any]] = []
    if report is not None:
        for arm in (report.tp1, report.sl):
            for r in arm.rows:
                rows.append([
                    r.signal_id, r.symbol, r.setup_class, r.direction, r.tf,
                    arm.name, r.verdict, r.level_source, r.shift_pct,
                    r.actual_pnl_pct, r.arm_pnl_pct, r.delta_pct,
                ])
    # Uncapped: a truncated export is #97 wearing a download button.
    return csv_response("structural_snap", cols, rows)

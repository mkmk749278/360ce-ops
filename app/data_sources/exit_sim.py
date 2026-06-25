"""Scaled-exit "what-if" simulator for the Profit tab.

Answers the owner's question directly: *if a signal were closed only at a
take-profit target (or its stop) — stripping out the engine's pre-TP banking
and invalidation exits — what would the realised P/L have been?* This isolates
**entry quality** from **exit policy**. If good entries only turn negative
because of how the engine exits, the strategy results here beat the engine's
real P/L, and the fix is the exit method, not the entries.

Every number is derived from the engine's own recorded ground truth — ``hit_tp``,
``first_tp_touch_timestamp`` vs ``first_sl_touch_timestamp``, MFE / MAE, and the
engine's TP-based PnL — so it needs **no Binance candles** and covers *every*
signal, including the rows the held-to-stop candle replay can't reach when the
ops box can't egress to Binance. That is what "fills" the degraded rows.

A **strategy** is an ordered list of **legs**; each leg takes a fraction of the
position off at a **target** — a fixed ``+x%`` from entry, or a TP level. Any
fraction not filled by a target is closed at the original stop if the stop is
hit first, else marked to the live price (still-running). That is exactly the
"only TP or SL closes the trade" rule the owner asked for.

Fill order (target vs stop) is resolved from the engine's first-touch record.
When the record genuinely can't disambiguate, the leg falls back to a
**pessimistic** (stop-first) assumption — it never invents a favourable number.
Rows that lean on the engine's TP-based PnL approximation (older signals that
don't store exact TP prices) are flagged ``approx`` so the page can mark them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Targets a leg can aim at.
_TP_LEVEL = "tp"
_FIXED = "fixed"


@dataclass(frozen=True)
class Target:
    """A take-profit objective: either a TP level (1/2/3) or a fixed +pct."""

    kind: str            # "tp" | "fixed"
    value: float         # tp level (1/2/3) or the fixed pct (e.g. 1.0)

    @property
    def label(self) -> str:
        if self.kind == _TP_LEVEL:
            return f"TP{int(self.value)}"
        return f"+{self.value:g}%"


@dataclass(frozen=True)
class Leg:
    fraction: float      # 0..1 of the position taken off at this target
    target: Target


@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    legs: tuple[Leg, ...]


@dataclass
class ExitResult:
    """Outcome of replaying one signal under a scaled-exit strategy."""

    result_pct: float | None       # realised weighted return, None if uncomputable
    filled_labels: list[str] = field(default_factory=list)  # target legs that filled
    sl_frac: float = 0.0           # fraction closed at the stop
    open_frac: float = 0.0         # fraction still running (marked to live)
    approx: bool = False           # used engine TP-based PnL (no exact TP price)
    is_active: bool = False        # any fraction still open


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class SignalInputs:
    """The engine-recorded fields the simulator reads, from live or perf records."""

    direction: str
    entry: float
    sl: float
    # Exact TP prices — present on live API rows, absent on perf records.
    tp1: float | None
    tp2: float | None
    tp3: float | None
    mfe_pct: float | None          # max favourable excursion (magnitude, >= 0)
    mae_pct: float | None          # max adverse excursion (magnitude, >= 0)
    hit_tp: int                    # highest TP level price touched (0..3)
    hit_sl: bool | None            # explicit stop-hit flag (perf records)
    sq_pnl_pct: float | None       # engine TP-based PnL (signal_quality_pnl_pct)
    pnl_pct: float | None          # engine real/live P/L (mark for the open leg)
    first_tp_touch: float | None   # epoch secs of first TP1 touch
    first_sl_touch: float | None   # epoch secs of first SL touch
    active: bool                   # signal still live on the engine

    @property
    def is_long(self) -> bool:
        return self.direction == "LONG"

    @classmethod
    def from_dict(cls, d: dict) -> "SignalInputs | None":
        direction = str(d.get("direction") or d.get("side") or "").upper()
        entry = _f(d.get("entry"))
        if entry is None:
            entry = _f(d.get("entry_price"))
        # Original protective stop — never the break-even/trailed live stop.
        sl = _f(d.get("original_stop_loss"))
        if sl is None or sl <= 0:
            sl = _f(d.get("stop_loss"))
        if sl is None or sl <= 0:
            sl = _f(d.get("sl"))
        if direction not in ("LONG", "SHORT") or entry is None or entry <= 0 or sl is None or sl <= 0:
            return None

        mfe = _f(d.get("max_favorable_excursion_pct"))
        mae = _f(d.get("max_adverse_excursion_pct"))
        raw_status = str(d.get("status") or "").upper()
        active = raw_status in ("ACTIVE", "OPEN", "RUNNING") or raw_status == ""
        hit_sl_raw = d.get("hit_sl")
        hit_sl = bool(hit_sl_raw) if hit_sl_raw is not None else None
        try:
            hit_tp = int(d.get("hit_tp") or d.get("signal_quality_hit_tp") or 0)
        except (TypeError, ValueError):
            hit_tp = 0

        return cls(
            direction=direction,
            entry=entry,
            sl=sl,
            tp1=_f(d.get("tp1")),
            tp2=_f(d.get("tp2")),
            tp3=_f(d.get("tp3")),
            mfe_pct=abs(mfe) if mfe is not None else None,
            mae_pct=abs(mae) if mae is not None else None,
            hit_tp=hit_tp,
            hit_sl=hit_sl,
            sq_pnl_pct=_f(d.get("signal_quality_pnl_pct")),
            pnl_pct=_f(d.get("pnl_pct")),
            first_tp_touch=_f(d.get("first_tp_touch_timestamp")),
            first_sl_touch=_f(d.get("first_sl_touch_timestamp")),
            active=active,
        )

    # --- derived geometry -------------------------------------------------

    def sl_return_pct(self) -> float:
        """Signed return if closed at the original stop (negative for a real loss)."""
        if self.is_long:
            return (self.sl - self.entry) / self.entry * 100.0
        return (self.entry - self.sl) / self.entry * 100.0

    def _tp_price(self, level: int) -> float | None:
        return {1: self.tp1, 2: self.tp2, 3: self.tp3}.get(level)

    def tp_return_pct(self, level: int) -> tuple[float | None, bool]:
        """(return %, approx?) for taking profit at TP ``level``.

        Exact from the TP price when the row carries it (live API). Otherwise
        falls back to the engine's recorded TP-based PnL — approximate, flagged.
        """
        price = self._tp_price(level)
        if price is not None and price > 0:
            if self.is_long:
                return (price - self.entry) / self.entry * 100.0, False
            return (self.entry - price) / self.entry * 100.0, False
        # No exact TP price (older perf record) → engine TP-based PnL.
        if self.sq_pnl_pct is not None:
            return self.sq_pnl_pct, True
        return None, True

    def stop_hit(self) -> bool:
        """Did price ever reach the original stop?"""
        if self.hit_sl is not None:
            return self.hit_sl
        if self.mae_pct is not None:
            return self.mae_pct + 1e-9 >= abs(self.sl_return_pct())
        return False

    def _target_before_stop(self, reached: bool, near_tp1: bool) -> bool:
        """Did the favourable target fill before the stop was touched?

        ``near_tp1`` marks a target at/inside the TP1 distance — for those, a
        TP1-before-SL first-touch record proves the nearer target filled first
        too. When nothing in the record can disambiguate a both-touched case we
        return False (stop-first): pessimistic, never an invented win.
        """
        if not reached:
            return False
        if not self.stop_hit():
            return True
        # Both barriers were touched — order matters. Trust the engine's
        # first-touch timestamps when present.
        if near_tp1 and self.first_tp_touch is not None and self.first_sl_touch is not None:
            return self.first_tp_touch <= self.first_sl_touch
        return False

    def fixed_target(self, pct: float) -> tuple[bool, float]:
        """(filled_before_stop, return %) for a fixed +``pct`` target."""
        reached = self.mfe_pct is not None and self.mfe_pct + 1e-9 >= pct
        # A fixed target reached and at/inside TP1 (which the engine timestamps
        # describe) can use the TP1-before-SL record; hit_tp >= 1 confirms TP1
        # was touched so the nearer fixed target was too.
        near_tp1 = self.hit_tp >= 1
        return self._target_before_stop(reached, near_tp1), pct

    def tp_target(self, level: int) -> tuple[bool, float | None, bool]:
        """(filled_before_stop, return %, approx) for a TP-level target."""
        ret, approx = self.tp_return_pct(level)
        # hit_tp is the highest level price touched over the whole life; >= level
        # means TPn was reached. To touch TPn price must have moved past TP1, so
        # the TP1 first-touch timestamp bounds the order vs the stop.
        reached = self.hit_tp >= level or (
            level == 1 and self.mfe_pct is not None and self.tp1 is not None
            and self.tp1 > 0
            and self.mfe_pct + 1e-9 >= abs(self.tp_return_pct(1)[0] or 0.0)
        )
        return self._target_before_stop(reached, near_tp1=True), ret, approx


def evaluate(inp: SignalInputs, strategy: Strategy) -> ExitResult:
    """Replay one signal under ``strategy`` → realised weighted return."""
    total = 0.0
    filled = 0.0
    labels: list[str] = []
    approx = False
    computable = False

    for leg in strategy.legs:
        if leg.target.kind == _FIXED:
            ok, ret = inp.fixed_target(leg.target.value)
            leg_approx = False
        else:
            ok, ret, leg_approx = inp.tp_target(int(leg.target.value))
        if ret is None:
            # Can't price this leg at all — leave its fraction for the SL/open
            # tail rather than fabricate. Flag approx so the row is marked.
            approx = True
            continue
        if ok:
            total += leg.fraction * ret
            filled += leg.fraction
            labels.append(leg.target.label)
            approx = approx or leg_approx
            computable = True

    remaining = max(0.0, 1.0 - filled)
    is_active = False
    if remaining > 1e-9:
        if inp.stop_hit():
            total += remaining * inp.sl_return_pct()
            computable = True
            sl_frac = remaining
            open_frac = 0.0
        elif inp.active:
            # Still running: mark the untaken tail to the engine's live P/L.
            mark = inp.pnl_pct if inp.pnl_pct is not None else 0.0
            total += remaining * mark
            open_frac = remaining
            sl_frac = 0.0
            is_active = True
            computable = True
        else:
            # Closed without hitting target or stop (e.g. expiry) — mark the
            # tail to the engine's recorded terminal P/L.
            mark = inp.pnl_pct if inp.pnl_pct is not None else 0.0
            total += remaining * mark
            sl_frac = 0.0
            open_frac = 0.0
            computable = computable or inp.pnl_pct is not None
    else:
        sl_frac = 0.0
        open_frac = 0.0

    return ExitResult(
        result_pct=total if computable else None,
        filled_labels=labels,
        sl_frac=sl_frac,
        open_frac=open_frac,
        approx=approx,
        is_active=is_active,
    )


# --------------------------------------------------------------------------- #
# Strategy catalog
# --------------------------------------------------------------------------- #

def build_catalog(target_pct: float = 1.0) -> dict[str, Strategy]:
    """The selectable what-if exit strategies.

    ``target_pct`` parametrises the fixed-percent target (default +1%), so the
    owner can sweep it from the UI without code changes.
    """
    g = target_pct
    fixed = Target(_FIXED, g)
    tp1, tp2, tp3 = Target(_TP_LEVEL, 1), Target(_TP_LEVEL, 2), Target(_TP_LEVEL, 3)
    strategies = [
        Strategy("tp1", "TP1 full (100% @ TP1)", (Leg(1.0, tp1),)),
        Strategy("be_tp1", f"BE stop at +{g:g}% → hold to TP1", (Leg(1.0, tp1),)),
        Strategy("flat", f"Pre-TP only +{g:g}% · no invalidation", (Leg(1.0, fixed),)),
        Strategy("tp1_tp2", "50% TP1 · 50% TP2", (Leg(0.5, tp1), Leg(0.5, tp2))),
        Strategy("flat_tp1", f"Pre-TP +{g:g}% · TP1 · no invalidation",
                 (Leg(0.5, fixed), Leg(0.5, tp1))),
        Strategy("tp1_tp2_tp3", "TP1/TP2/TP3 thirds",
                 (Leg(1 / 3, tp1), Leg(1 / 3, tp2), Leg(1 / 3, tp3))),
    ]
    return {s.key: s for s in strategies}


def get_strategy(key: str, target_pct: float = 1.0) -> Strategy:
    catalog = build_catalog(target_pct)
    return catalog.get(key) or catalog["tp1"]


def evaluate_be(inp: SignalInputs, strategy: Strategy, be_pct: float) -> ExitResult:
    """Like evaluate() but with a break-even stop once MFE reaches be_pct%.

    If price moved be_pct% in our favour and then hit the original stop (implying
    it crossed back through entry on the way to the stop), simulate a 0% gross
    exit — the stop was at break-even, so we scratch instead of losing.

    If TP1 fills before the stop fires (verified by engine first-touch timestamps
    or hit_tp flag), the BE stop never activates — TP1 takes us out profitably and
    we fall through to the base strategy result unchanged.

    Signals where BE was not triggered (MFE < be_pct) are evaluated via the base
    strategy with no modification.
    """
    be_triggered = inp.mfe_pct is not None and inp.mfe_pct + 1e-9 >= be_pct
    if be_triggered and inp.stop_hit():
        # TP1 hit at or before the stop → TP1 exits the position first; BE irrelevant.
        tp1_before_sl = (
            inp.hit_tp >= 1
            or (
                inp.first_tp_touch is not None
                and inp.first_sl_touch is not None
                and inp.first_tp_touch <= inp.first_sl_touch
            )
        )
        if not tp1_before_sl:
            # BE fires: price went be_pct% our way then reversed through entry to SL.
            # Gross return is 0% (entry == exit); fee is deducted by the caller.
            return ExitResult(
                result_pct=0.0,
                filled_labels=["BE"],
                sl_frac=0.0,
                open_frac=0.0,
                approx=False,
                is_active=False,
            )
    return evaluate(inp, strategy)


def summarize(results: list[ExitResult]) -> dict[str, Any]:
    """Net read-out across a set of rows: avg %, win rate, total, n."""
    vals = [r.result_pct for r in results if r.result_pct is not None]
    if not vals:
        return {"n": 0, "avg_pct": None, "win_rate": None, "total_pct": None,
                "wins": 0, "losses": 0}
    wins = sum(1 for v in vals if v > 1e-9)
    losses = sum(1 for v in vals if v < -1e-9)
    return {
        "n": len(vals),
        "avg_pct": sum(vals) / len(vals),
        "win_rate": wins / len(vals) * 100.0,
        "total_pct": sum(vals),
        "wins": wins,
        "losses": losses,
    }

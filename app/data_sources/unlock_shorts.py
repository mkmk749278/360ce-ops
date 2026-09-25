"""Unlock-short dark lane — reducer for ``/signals/unlock-shorts``.

Engine: ``360-v2/src/unlock_shorts.py`` (research:
``docs/SHORTS_MARKET_RESEARCH_2026_09_25.md``). The engine reads DefiLlama's
vesting calendar, stamps every insider cliff unlock ≥ 0.5% of max supply at the
close of T−14 on real Binance prices, walks it hourly and closes it at T+2.
Nothing in that lane reaches a subscriber or an order; this page is where the
owner reads it.

**Ops computes no measurement here.** Entry, walk, stops, funding and every
variant's result are the engine's, written into the ledger. This module grades
freshness, groups closed rows and prices the open ones against a live mark.

Rules the page holds to, each one this repo's own:

* **The lane's state is graded on the ENGINE'S clock** (``written_at``), never
  on this process's. Five states, never pooled: ``missing`` (no file — an engine
  predating the lane, or the loop never ran) · ``unreadable`` · ``off`` (the
  switch, a decision) · ``stale`` (the loop stopped writing) · ``live``.
* **Four buckets, never two.** ``all`` is the rule as registered; ``selected``
  (both squeeze filters pass), ``rejected`` and ``unknown`` (a filter input was
  missing at entry) split it. Folding ``unknown`` into either side is how a
  filter takes credit for rows it never judged.
* **Variants are never blended.** No-stop, 20% stop, 40% stop, BTC-hedged and
  alt-hedged answer different questions; one pooled number would move with the
  mix, not with the mechanism.
* **Read n first.** An interval is only computed from five rows, and the
  backtest reference is labelled as one and never pooled with a forward row.
* **Refusals iterate the ENGINE'S payload** and look their sentence up here; a
  status or reason ops has never heard of renders under its raw name, badged
  ``unclassified`` — never dropped.
* **A live mark is only honest beside a row that can say whether it is still
  true**: every open row leads with the engine's ``bars_behind`` / ``stalled``.
"""
from __future__ import annotations

import csv
import io
import random
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

#: The engine flushes every 5 minutes (after a 60s boot delay). Four missed
#: flushes is a stopped loop, not a slow one.
LANE_STALE_SEC = 20 * 60.0
#: The calendar is re-read every 20h; beyond this it has stopped arriving.
CALENDAR_STALE_SEC = 30 * 3600.0
MIN_N_FOR_CI = 5
CLOSED_TABLE_CAP = 300
BOOT_SEED = 20260925

STATUS_COPY: dict[str, str] = {
    "SCHEDULED": "Waiting for its entry (the close of T−14).",
    "OPEN": "Stamped at T−14; being walked hourly until the close of T+2.",
    "CLOSED": "Walked to the close of T+2; every variant's result recorded.",
    "MISSED": "The entry came due while the lane could not stamp it. Never backfilled.",
    "LATE": "First seen after its entry had passed — see the cause.",
    "REFUSED": "The entry could not be priced honestly — see the reason.",
    "CANCELLED": "Left the calendar (moved or withdrawn) before its entry.",
    "INSUFFICIENT": "Its bars stopped (delisting); no verdict was invented.",
}

REASON_COPY: dict[str, str] = {
    "no_price": "Binance had no price for the symbol at entry.",
    "price_mismatch": (
        "DefiLlama's price and Binance's differ by more than 3x — almost always "
        "two tokens sharing a ticker. Refused rather than measured on the wrong coin."
    ),
    "calendar_removed": "The unlock left the calendar before its entry (moved or withdrawn).",
    "walk_incomplete": "The hourly walk could not reach the exit within 48h of it.",
    "no_bars": "Binance returned no hourly bars for the symbol.",
    "lane_start": "The lane started after this entry had passed — not a calendar fault.",
    "calendar_late": "The calendar first showed this unlock after its entry had passed.",
}

VARIANTS: tuple[tuple[str, str, str], ...] = (
    ("net", "No stop", "the rule as registered: short at T−14, cover at T+2"),
    ("stop20", "20% stop", "covered at the stop if price rose 20% above entry"),
    ("stop40", "40% stop", "covered at the stop if price rose 40% above entry"),
    ("btc", "BTC-hedged", "no stop, with an equal-notional BTC long; both legs charged"),
    ("alt", "Alt-basket-hedged", "no stop, hedged with an equal-weight basket of liquid alts (basket funding not charged)"),
)

BUCKETS: tuple[tuple[str, str], ...] = (
    ("all", "All unlocks — the rule as registered"),
    ("selected", "Both filters pass"),
    ("rejected", "A filter rejects"),
    ("unknown", "Filter input missing at entry"),
)


def _f(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def variant_value(row: dict, key: str) -> Optional[float]:
    res = row.get("results") or {}
    if key == "net":
        return _f(res.get("net_pct"))
    if key in ("stop20", "stop40"):
        return _f((res.get(key) or {}).get("net_pct"))
    if key == "btc":
        return _f(res.get("btc_hedged_pct"))
    if key == "alt":
        return _f(res.get("alt_hedged_pct"))
    return None


def bucket_of(row: dict) -> str:
    sel = row.get("selected")
    if sel is True:
        return "selected"
    if sel is False:
        return "rejected"
    return "unknown"


def cluster_ci(values: list[float], clusters: list[str], n_boot: int = 2000) -> Optional[tuple[float, float]]:
    """95% interval of the mean, resampling whole symbols (fixed seed).

    Symbol-clustered because a token with monthly vesting contributes a row a
    month, and those rows are not independent evidence.
    """
    if len(values) < MIN_N_FOR_CI:
        return None
    groups: dict[str, list[float]] = {}
    for v, c in zip(values, clusters):
        groups.setdefault(c, []).append(v)
    keys = list(groups)
    rng = random.Random(BOOT_SEED)
    means = []
    for _ in range(n_boot):
        tot = cnt = 0.0
        for _k in range(len(keys)):
            g = groups[keys[rng.randrange(len(keys))]]
            tot += sum(g)
            cnt += len(g)
        means.append(tot / cnt)
    means.sort()
    return means[int(0.025 * n_boot)], means[int(0.975 * n_boot) - 1]


@dataclass
class Cell:
    n: int = 0
    mean: Optional[float] = None
    median: Optional[float] = None
    win: Optional[float] = None
    total: Optional[float] = None
    ci: Optional[tuple[float, float]] = None
    missing: int = 0   # closed rows where this variant could not be priced
    stops_hit: Optional[int] = None


def _cell(rows: list[dict], key: str) -> Cell:
    vals, syms, missing = [], [], 0
    for r in rows:
        v = variant_value(r, key)
        if v is None:
            missing += 1
            continue
        vals.append(v)
        syms.append(str(r.get("symbol") or ""))
    c = Cell(n=len(vals), missing=missing)
    if vals:
        c.mean = sum(vals) / len(vals)
        c.median = statistics.median(vals)
        c.win = sum(1 for v in vals if v > 0) / len(vals) * 100.0
        c.total = sum(vals)
        c.ci = cluster_ci(vals, syms)
    if key in ("stop20", "stop40"):
        c.stops_hit = sum(1 for r in rows if ((r.get("results") or {}).get(key) or {}).get("hit"))
    return c


@dataclass
class Report:
    state: str = "missing"
    state_detail: str = ""
    written_at: Optional[float] = None
    written_age_sec: Optional[float] = None
    enabled: Optional[bool] = None
    rule: dict = field(default_factory=dict)
    calendar: dict = field(default_factory=dict)
    calendar_state: str = "never"
    calendar_age_sec: Optional[float] = None
    counters: dict = field(default_factory=dict)
    last_cycle: dict = field(default_factory=dict)
    last_cycle_at: Optional[float] = None
    status_rows: list = field(default_factory=list)       # [(status, count, copy, classified)]
    reason_rows: list = field(default_factory=list)       # [(status, reason, count, copy, classified)]
    upcoming: list = field(default_factory=list)
    open_rows: list = field(default_factory=list)
    closed_rows: list = field(default_factory=list)
    closed_total: int = 0
    closed_capped: bool = False
    matrix: dict = field(default_factory=dict)             # bucket -> variant -> Cell
    bucket_n: dict = field(default_factory=dict)
    first_verdict_due: Optional[float] = None
    marks_available: bool = False
    stalled_open: int = 0
    evicted: int = 0
    load_refused: Optional[str] = None
    n_rows: int = 0


def build_report(ledger: Any, *, now: Optional[float] = None, prices: Optional[dict] = None) -> Report:
    now = time.time() if now is None else float(now)
    prices = prices or {}
    rep = Report(marks_available=bool(prices))
    if not isinstance(ledger, dict):
        rep.state, rep.state_detail = "unreadable", f"unexpected ledger shape: {type(ledger).__name__}"
        return rep
    err = ledger.get("error")
    if err and "rows" not in ledger:
        if str(err).startswith("missing"):
            rep.state = "missing"
            rep.state_detail = (
                "The engine has not written unlock_shorts_v1.json: an engine that "
                "predates the lane, or its loop has not completed a first cycle "
                "(it waits 60s after boot)."
            )
        else:
            rep.state, rep.state_detail = "unreadable", str(err)
        return rep

    rep.written_at = _f(ledger.get("written_at"))
    rep.written_age_sec = (now - rep.written_at) if rep.written_at is not None else None
    rep.enabled = ledger.get("enabled") if isinstance(ledger.get("enabled"), bool) else None
    rep.rule = dict(ledger.get("rule") or {})
    rep.calendar = dict(ledger.get("calendar") or {})
    rep.counters = dict(ledger.get("counters") or {})
    rep.last_cycle = dict(ledger.get("last_cycle") or {})
    rep.last_cycle_at = _f(ledger.get("last_cycle_at"))
    rep.evicted = int(ledger.get("evicted") or 0)
    rep.load_refused = ledger.get("load_refused")

    if rep.written_age_sec is None:
        rep.state, rep.state_detail = "unreadable", "the file carries no written_at stamp"
    elif rep.enabled is False:
        rep.state = "off"
        rep.state_detail = (
            "The lane is switched off (Control → Tunables → Measurement → "
            "unlock_short_lane_enabled). Entries that come due while off are "
            "recorded MISSED, never backfilled."
        )
    elif rep.written_age_sec > LANE_STALE_SEC:
        rep.state = "stale"
        rep.state_detail = (
            f"The engine last wrote this file {int(rep.written_age_sec // 60)} min ago; "
            f"it writes every 5 min. The loop has stopped — every figure below is frozen."
        )
    else:
        rep.state = "live"

    last_ok = _f(rep.calendar.get("last_ok_at"))
    last_err_at = _f(rep.calendar.get("last_error_at"))
    if last_ok is None:
        rep.calendar_state = "failing" if rep.calendar.get("last_error") else "never"
    else:
        rep.calendar_age_sec = now - last_ok
        if rep.calendar_age_sec > CALENDAR_STALE_SEC:
            rep.calendar_state = "stale"
        elif last_err_at is not None and last_err_at > last_ok and rep.calendar.get("last_error"):
            rep.calendar_state = "retrying"
        else:
            rep.calendar_state = "ok"

    rows = [r for r in (ledger.get("rows") or []) if isinstance(r, dict)]
    rep.n_rows = len(rows)

    # Status and reason tables iterate the ENGINE'S payload.
    counts: dict[str, int] = {}
    reasons: dict[tuple[str, str], int] = {}
    for r in rows:
        st = str(r.get("status") or "?")
        counts[st] = counts.get(st, 0) + 1
        why = r.get("late_cause") if st == "LATE" else r.get("reason")
        if st in ("LATE", "REFUSED", "CANCELLED", "INSUFFICIENT") and why:
            key = (st, str(why))
            reasons[key] = reasons.get(key, 0) + 1
    order = list(STATUS_COPY)
    for st in sorted(counts, key=lambda s: (order.index(s) if s in order else 99, s)):
        rep.status_rows.append((st, counts[st], STATUS_COPY.get(st, ""), st in STATUS_COPY))
    for (st, why), n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        copy = REASON_COPY.get(why)
        if copy is None and st == "MISSED":
            copy = STATUS_COPY["MISSED"]
        rep.reason_rows.append((st, why, n, copy or "", why in REASON_COPY))

    scheduled = sorted((r for r in rows if r.get("status") == "SCHEDULED"),
                       key=lambda r: _f(r.get("entry_due_ts")) or 0)
    for r in scheduled:
        entry_due = _f(r.get("entry_due_ts"))
        rep.upcoming.append({
            **r,
            "days_to_entry": ((entry_due - now) / 86400.0) if entry_due else None,
            "recipients": ", ".join(sorted({a.get("recipient", "") for a in r.get("allocations") or []}))[:120],
        })

    open_rows = sorted((r for r in rows if r.get("status") == "OPEN"),
                       key=lambda r: _f(r.get("exit_due_ts")) or 0)
    for r in open_rows:
        rep.open_rows.append(_price_open(r, now, prices))
    rep.stalled_open = sum(1 for r in rep.open_rows if r.get("stalled"))

    due = [_f(r.get("exit_due_ts")) for r in open_rows + scheduled]
    due = [d for d in due if d is not None]
    rep.first_verdict_due = min(due) if due else None

    closed = [r for r in rows if r.get("status") == "CLOSED" and r.get("results")]
    closed.sort(key=lambda r: _f(r.get("exit_due_ts")) or 0, reverse=True)
    rep.closed_total = len(closed)
    rep.closed_capped = len(closed) > CLOSED_TABLE_CAP
    rep.closed_rows = closed[:CLOSED_TABLE_CAP]

    split = {"all": closed, "selected": [], "rejected": [], "unknown": []}
    for r in closed:
        split[bucket_of(r)].append(r)
    for b, _label in BUCKETS:
        rep.bucket_n[b] = len(split[b])
        rep.matrix[b] = {v: _cell(split[b], v) for v, _l, _d in VARIANTS}
    return rep


def _price_open(row: dict, now: float, prices: dict) -> dict:
    out = dict(row)
    out.pop("basket_entry", None)
    entry = _f(row.get("entry_price"))
    mark = _f(prices.get(row.get("symbol")))
    out["mark"] = mark
    out["funding_so_far_pct"] = sum(_f(ev[1]) or 0.0 for ev in row.get("funding_events") or []) * 100.0
    out["unrealized_gross_pct"] = ((entry - mark) / entry * 100.0) if (entry and mark) else None
    maxh, minl = _f(row.get("max_high")), _f(row.get("min_low"))
    out["mae_pct"] = ((maxh - entry) / entry * 100.0) if (entry and maxh) else None
    out["mfe_pct"] = ((entry - minl) / entry * 100.0) if (entry and minl) else None
    stops = row.get("stops") or {}
    rooms = {}
    for pct in (20, 40):
        if str(pct) in stops:
            rooms[pct] = "hit"
        elif entry and mark:
            level = entry * (1 + pct / 100.0)
            rooms[pct] = (level - mark) / mark * 100.0
        else:
            rooms[pct] = None
    out["room"] = rooms
    exit_due = _f(row.get("exit_due_ts"))
    out["days_to_exit"] = ((exit_due - now) / 86400.0) if exit_due else None
    out["bucket"] = bucket_of(row)
    return out


# --------------------------------------------------------------------------- #
# Export — uncapped, one loader with the page
# --------------------------------------------------------------------------- #

CSV_COLUMNS = (
    "row_id", "status", "reason", "late_cause", "symbol", "token_name", "token_symbol",
    "unlock_date", "fraction", "entry_due_ts", "exit_due_ts",
    "entry_stamped_at", "entry_price", "entry_ref_close", "entry_drift_pct",
    "funding_last", "ret_14d", "crowded", "running", "selected", "bucket",
    "bars_seen", "gap_bars", "bars_behind", "stalled", "max_high", "min_low",
    "exit_price", "exit_basis", "exit_ref_close", "basket_ret_pct",
    "gross_pct", "funding_pct", "net_pct",
    "stop20_hit", "stop20_net_pct", "stop40_hit", "stop40_net_pct",
    "btc_ret_pct", "btc_hedged_pct", "alt_hedged_pct",
)


def export_csv(ledger: Any) -> str:
    rows = [r for r in ((ledger or {}).get("rows") or []) if isinstance(r, dict)] if isinstance(ledger, dict) else []
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in sorted(rows, key=lambda r: _f(r.get("entry_due_ts")) or 0):
        res = r.get("results") or {}
        flat = {k: r.get(k) for k in CSV_COLUMNS}
        flat["bucket"] = bucket_of(r)
        for k in ("gross_pct", "funding_pct", "net_pct", "btc_ret_pct", "btc_hedged_pct", "alt_hedged_pct"):
            flat[k] = res.get(k)
        for s in ("stop20", "stop40"):
            flat[f"{s}_hit"] = (res.get(s) or {}).get("hit")
            flat[f"{s}_net_pct"] = (res.get(s) or {}).get("net_pct")
        w.writerow(flat)
    return buf.getvalue()


def iter_variants() -> Iterable[tuple[str, str, str]]:
    return VARIANTS

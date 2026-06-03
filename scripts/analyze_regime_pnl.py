#!/usr/bin/env python3
"""Regime-aware position P&L analyser.

Ingests a Binance Futures position-history CSV export, joins each closed
position back to the engine signal that triggered it (matched by symbol +
direction + open-time proximity), tags with ``entry_regime``, then produces
per-regime / per-alignment breakdowns.

Usage
-----
  # Point at local signal_history.json from engine data volume:
  python3 scripts/analyze_regime_pnl.py positions.csv

  # Explicit paths:
  python3 scripts/analyze_regime_pnl.py positions.csv \\
      --signals /engine-data/signal_history.json \\
      --match-window 900 \\
      --output report.json

Binance CSV columns accepted (flexible — Binance renames them occasionally):
  Symbol           → symbol
  Side/Type/Direction  → direction  (LONG/SHORT)
  Entry Price / Avg. Entry Price / Open Price → entry_price
  Close Price / Exit Price / Avg. Close Price → exit_price
  Realized PNL / Closed PNL / Realized Profit → pnl_usdt
  Commission / Trading Fee / Fee               → commission_usdt
  Open Time / Opened Time / Opened At         → open_time  (UTC)
  Close Time / Closed Time / Closed At        → close_time (UTC)
  Duration                                    → duration_raw (optional)
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Column-name normalisation map (lowercase alias → canonical key)
# ---------------------------------------------------------------------------
_COL_MAP: dict[str, str] = {
    # symbol
    "symbol": "symbol",
    # direction
    "side": "direction",
    "type": "direction",
    "direction": "direction",
    "position side": "direction",
    # entry price
    "entry price": "entry_price",
    "avg. entry price": "entry_price",
    "average entry price": "entry_price",
    "open price": "entry_price",
    "avg open price": "entry_price",
    # exit price
    "exit price": "exit_price",
    "close price": "exit_price",
    "avg. close price": "exit_price",
    "average close price": "exit_price",
    "avg close price": "exit_price",
    # pnl
    "realized pnl": "pnl_usdt",
    "realized profit": "pnl_usdt",
    "closed pnl": "pnl_usdt",
    "pnl": "pnl_usdt",
    "profit": "pnl_usdt",
    # commission
    "commission": "commission_usdt",
    "trading fee": "commission_usdt",
    "fee": "commission_usdt",
    "fees": "commission_usdt",
    # open time
    "open time": "open_time",
    "opened time": "open_time",
    "opened at": "open_time",
    "time of open": "open_time",
    "start time": "open_time",
    # close time
    "close time": "close_time",
    "closed time": "close_time",
    "closed at": "close_time",
    "time of close": "close_time",
    "end time": "close_time",
    # duration (optional, derived if absent)
    "duration": "duration_raw",
}

_REQUIRED_CANONICAL = {"symbol", "direction", "pnl_usdt", "open_time"}


def _normalise_headers(raw_headers: list[str]) -> dict[str, str]:
    """Map raw CSV header names → canonical keys using the alias table."""
    mapping: dict[str, str] = {}
    for raw in raw_headers:
        canonical = _COL_MAP.get(raw.strip().lower())
        if canonical:
            mapping[raw] = canonical
    return mapping


def _parse_dt(value: str | None) -> datetime | None:
    if not value or value.strip() == "":
        return None
    value = value.strip().replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y/%m/%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_float(value: str | None) -> float:
    if not value or value.strip() in ("", "-", "N/A"):
        return 0.0
    try:
        return float(value.strip().replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _parse_duration_raw(raw: str | None, open_time: datetime | None, close_time: datetime | None) -> int | None:
    """Return duration in seconds. Prefer computed from open/close; fall back to raw string."""
    if open_time and close_time:
        delta = close_time - open_time
        secs = int(delta.total_seconds())
        return max(secs, 0)
    if not raw:
        return None
    raw = raw.strip()
    total = 0
    parts = raw.replace("d", "d ").replace("h", "h ").replace("m", "m ").replace("s", "s ").split()
    for part in parts:
        if part.endswith("d"):
            total += int(part[:-1]) * 86400
        elif part.endswith("h"):
            total += int(part[:-1]) * 3600
        elif part.endswith("m"):
            total += int(part[:-1]) * 60
        elif part.endswith("s"):
            total += int(part[:-1])
        elif ":" in part:
            segments = part.split(":")
            if len(segments) == 3:
                h, m, s = segments
                total += int(h) * 3600 + int(m) * 60 + int(s)
    return total if total > 0 else None


# ---------------------------------------------------------------------------
# Binance CSV parser
# ---------------------------------------------------------------------------

def load_binance_csv(path: Path) -> tuple[list[dict], list[str]]:
    """Parse Binance position history CSV.

    Returns (rows, warnings). Each row has canonical keys; unknown columns
    are kept as-is under their raw name. 'direction' is normalised to
    LONG/SHORT. Rows missing required fields are skipped with a warning.
    """
    rows: list[dict] = []
    warnings: list[str] = []

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            warnings.append("CSV has no header row — cannot parse.")
            return rows, warnings

        header_map = _normalise_headers(list(reader.fieldnames))
        missing = _REQUIRED_CANONICAL - set(header_map.values())
        if missing:
            warnings.append(
                f"CSV missing required columns: {missing}. "
                f"Detected headers: {list(reader.fieldnames)}"
            )
            if "pnl_usdt" in missing or "symbol" in missing or "direction" in missing:
                warnings.append("Analysis cannot proceed without symbol/direction/pnl — aborting CSV load.")
                return rows, warnings

        for i, raw_row in enumerate(reader, start=2):
            row: dict[str, Any] = {}
            for raw_key, value in raw_row.items():
                canonical = header_map.get(raw_key)
                if canonical:
                    row[canonical] = value
                else:
                    row[raw_key] = value

            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue

            direction_raw = (row.get("direction") or "").strip().upper()
            if direction_raw in ("BUY", "LONG", "L"):
                direction = "LONG"
            elif direction_raw in ("SELL", "SHORT", "S"):
                direction = "SHORT"
            else:
                warnings.append(f"Row {i} ({symbol}): unrecognised direction '{direction_raw}' — skipped.")
                continue

            open_time = _parse_dt(row.get("open_time"))
            close_time = _parse_dt(row.get("close_time"))
            if open_time is None:
                warnings.append(f"Row {i} ({symbol}): cannot parse open_time '{row.get('open_time')}' — skipped.")
                continue

            duration_sec = _parse_duration_raw(row.get("duration_raw"), open_time, close_time)
            pnl = _parse_float(row.get("pnl_usdt"))
            commission = _parse_float(row.get("commission_usdt"))
            entry_price = _parse_float(row.get("entry_price"))
            exit_price = _parse_float(row.get("exit_price"))

            rows.append({
                "symbol": symbol,
                "direction": direction,
                "open_time": open_time,
                "close_time": close_time,
                "duration_sec": duration_sec,
                "pnl_usdt": pnl,
                "commission_usdt": commission,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "net_pnl_usdt": pnl - abs(commission),
            })

    return rows, warnings


# ---------------------------------------------------------------------------
# Signal loader + index
# ---------------------------------------------------------------------------

def load_signals(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _signal_timestamp(sig: dict) -> datetime | None:
    """Best timestamp for the signal — prefer detected_at, fall back to timestamp."""
    for key in ("detected_at", "timestamp", "dispatch_timestamp"):
        raw = sig.get(key)
        if raw:
            dt = _parse_dt(str(raw))
            if dt:
                return dt
    return None


def build_signal_index(signals: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Index signals by (symbol, direction) for fast lookup."""
    index: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for sig in signals:
        symbol = (sig.get("symbol") or "").upper()
        direction = (sig.get("direction") or "").upper()
        if symbol and direction and direction in ("LONG", "SHORT"):
            ts = _signal_timestamp(sig)
            if ts:
                sig["_ts"] = ts
                index[(symbol, direction)].append(sig)
    return index


# ---------------------------------------------------------------------------
# Position → signal matching
# ---------------------------------------------------------------------------

def match_position_to_signal(
    pos: dict,
    index: dict[tuple[str, str], list[dict]],
    window_sec: int = 900,
) -> dict | None:
    """Find the engine signal that most likely produced this position.

    Strategy: within the (symbol, direction) bucket, pick the signal whose
    timestamp is closest to the position's open_time and falls within
    [signal_ts - 60s, signal_ts + window_sec]. The -60s slack handles
    clock-skew and the case where Binance fills before the engine's
    signal-history write completes.
    """
    key = (pos["symbol"], pos["direction"])
    candidates = index.get(key, [])
    if not candidates:
        return None

    open_time: datetime = pos["open_time"]
    best: dict | None = None
    best_delta = timedelta(seconds=window_sec + 1)

    for sig in candidates:
        sig_ts: datetime = sig["_ts"]
        # The position must open AFTER the signal was detected (or within 60s before, for clock skew)
        lower = sig_ts - timedelta(seconds=60)
        upper = sig_ts + timedelta(seconds=window_sec)
        if lower <= open_time <= upper:
            delta = abs(open_time - sig_ts)
            if delta < best_delta:
                best_delta = delta
                best = sig

    return best


# ---------------------------------------------------------------------------
# Regime alignment classification
# ---------------------------------------------------------------------------

_TREND_REGIMES = {"TRENDING_UP", "TRENDING_DOWN"}
_ALL_REGIMES = {"TRENDING_UP", "TRENDING_DOWN", "RANGING", "VOLATILE", "QUIET"}


def classify_alignment(regime: str, direction: str) -> str:
    """Map (regime, direction) → alignment bucket.

    trend-aligned  — direction agrees with the prevailing trend
    counter-trend  — direction opposes the prevailing trend
    ranging        — RANGING regime (no trend to align with)
    volatile       — VOLATILE regime
    quiet          — QUIET regime (fee-bleed risk)
    untagged       — no regime recorded on the signal
    """
    r = (regime or "").upper()
    d = direction.upper()
    if r == "TRENDING_UP":
        return "trend-aligned" if d == "LONG" else "counter-trend"
    if r == "TRENDING_DOWN":
        return "trend-aligned" if d == "SHORT" else "counter-trend"
    if r == "RANGING":
        return "ranging"
    if r == "VOLATILE":
        return "volatile"
    if r == "QUIET":
        return "quiet"
    return "untagged"


# ---------------------------------------------------------------------------
# Stats aggregation
# ---------------------------------------------------------------------------

def _empty_stats() -> dict:
    return {
        "count": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0.0,
        "total_commission": 0.0,
        "total_net_pnl": 0.0,
        "hold_times": [],
        "pnls": [],
    }


def _add_to_stats(bucket: dict, pos: dict) -> None:
    bucket["count"] += 1
    pnl = pos["pnl_usdt"]
    bucket["pnls"].append(pnl)
    bucket["total_pnl"] += pnl
    bucket["total_commission"] += abs(pos["commission_usdt"])
    bucket["total_net_pnl"] += pos["net_pnl_usdt"]
    if pnl > 0.001:
        bucket["wins"] += 1
    elif pnl < -0.001:
        bucket["losses"] += 1
    if pos["duration_sec"] is not None:
        bucket["hold_times"].append(pos["duration_sec"])


def _finalise_stats(bucket: dict) -> dict:
    n = bucket["count"]
    if n == 0:
        return {**bucket, "win_rate": 0.0, "avg_pnl": 0.0, "avg_net_pnl": 0.0,
                "avg_commission": 0.0, "avg_hold_sec": None, "best_pnl": None, "worst_pnl": None}
    pnls = bucket["pnls"]
    holds = bucket["hold_times"]
    return {
        "count": n,
        "wins": bucket["wins"],
        "losses": bucket["losses"],
        "neutral": n - bucket["wins"] - bucket["losses"],
        "win_rate": bucket["wins"] / n,
        "total_pnl": round(bucket["total_pnl"], 4),
        "avg_pnl": round(bucket["total_pnl"] / n, 4),
        "total_net_pnl": round(bucket["total_net_pnl"], 4),
        "avg_net_pnl": round(bucket["total_net_pnl"] / n, 4),
        "avg_commission": round(bucket["total_commission"] / n, 4),
        "avg_hold_sec": round(sum(holds) / len(holds)) if holds else None,
        "best_pnl": round(max(pnls), 4),
        "worst_pnl": round(min(pnls), 4),
    }


def aggregate(
    positions: list[dict],
    index: dict[tuple[str, str], list[dict]],
    window_sec: int,
) -> dict:
    """Join positions to signals, then aggregate by regime and alignment."""
    by_regime: dict[str, dict] = defaultdict(_empty_stats)
    by_alignment: dict[str, dict] = defaultdict(_empty_stats)
    by_setup: dict[str, dict] = defaultdict(_empty_stats)
    matched: list[dict] = []
    unmatched: list[dict] = []

    for pos in positions:
        sig = match_position_to_signal(pos, index, window_sec)
        if sig:
            regime = (sig.get("entry_regime") or "").upper() or "UNTAGGED"
            setup = sig.get("setup_class") or "UNKNOWN"
            alignment = classify_alignment(regime, pos["direction"])
            enriched = {**pos, "entry_regime": regime, "setup_class": setup, "alignment": alignment}
            matched.append(enriched)
            _add_to_stats(by_regime[regime], pos)
            _add_to_stats(by_alignment[alignment], pos)
            _add_to_stats(by_setup[setup], pos)
        else:
            unmatched.append({**pos, "entry_regime": "UNMATCHED"})
            _add_to_stats(by_alignment["unmatched"], pos)

    return {
        "by_regime": {k: _finalise_stats(v) for k, v in sorted(by_regime.items(), key=lambda x: -x[1]["count"])},
        "by_alignment": {k: _finalise_stats(v) for k, v in sorted(by_alignment.items(), key=lambda x: -x[1]["count"])},
        "by_setup": {k: _finalise_stats(v) for k, v in sorted(by_setup.items(), key=lambda x: -x[1]["count"])},
        "matched": matched,
        "unmatched": unmatched,
        "total_positions": len(positions),
        "matched_count": len(matched),
        "unmatched_count": len(unmatched),
    }


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

_ALIGNMENT_ORDER = ["trend-aligned", "counter-trend", "ranging", "volatile", "quiet", "untagged", "unmatched"]

_REGIME_ORDER = ["TRENDING_UP", "TRENDING_DOWN", "RANGING", "VOLATILE", "QUIET", "UNTAGGED"]


def _fmt_hold(sec: int | None) -> str:
    if sec is None:
        return "  —   "
    if sec < 60:
        return f"{sec:3d}s"
    m = sec // 60
    if m < 60:
        return f"{m:3d}m"
    h = m // 60
    rm = m % 60
    return f"{h}h{rm:02d}m"


def _fmt_pnl(val: float | None) -> str:
    if val is None:
        return "      —  "
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:+.4f}"


def _pct(val: float) -> str:
    return f"{val * 100:5.1f}%"


def _bar(val: float, width: int = 12) -> str:
    n = round(val * width)
    n = max(0, min(width, n))
    return "█" * n + "░" * (width - n)


def _table_row(label: str, stats: dict) -> str:
    wr = stats.get("win_rate", 0)
    return (
        f"  {label:<20s} "
        f"{stats['count']:>4d}  "
        f"{_pct(wr)}  {_bar(wr, 10)}  "
        f"{_fmt_pnl(stats.get('avg_pnl'))}  "
        f"{_fmt_pnl(stats.get('avg_net_pnl'))}  "
        f"{_fmt_hold(stats.get('avg_hold_sec'))}"
    )


def render_report(result: dict, show_unmatched: bool = False) -> str:
    lines: list[str] = []
    sep = "─" * 90

    total = result["total_positions"]
    matched = result["matched_count"]
    unmatched_count = result["unmatched_count"]
    match_rate = matched / total if total else 0

    lines += [
        "",
        "╔══════════════════════════════════════════════════════════════════════════════════╗",
        "║          REGIME-AWARE POSITION P&L ANALYSIS  —  360 Scalp Engine              ║",
        "╚══════════════════════════════════════════════════════════════════════════════════╝",
        "",
        f"  Positions loaded : {total}",
        f"  Matched to engine signals : {matched}  ({match_rate * 100:.1f}%)",
        f"  Unmatched (no signal found) : {unmatched_count}",
        "",
    ]

    # --- By alignment ---
    lines += [
        sep,
        "  BY ALIGNMENT (regime direction × trade direction)",
        sep,
        f"  {'Alignment':<20s} {'N':>4s}  {'Win%':>5s}  {'Win bar':>12s}  {'AvgPNL':>8s}  {'NetPNL':>8s}  {'AvgHold':>7s}",
        sep,
    ]
    by_aln = result["by_alignment"]
    for aln in _ALIGNMENT_ORDER:
        if aln in by_aln:
            lines.append(_table_row(aln, by_aln[aln]))
    lines.append("")

    # --- By regime ---
    lines += [
        sep,
        "  BY ENTRY REGIME",
        sep,
        f"  {'Regime':<20s} {'N':>4s}  {'Win%':>5s}  {'Win bar':>12s}  {'AvgPNL':>8s}  {'NetPNL':>8s}  {'AvgHold':>7s}",
        sep,
    ]
    by_reg = result["by_regime"]
    for reg in _REGIME_ORDER:
        if reg in by_reg:
            lines.append(_table_row(reg, by_reg[reg]))
    for reg, stats in by_reg.items():
        if reg not in _REGIME_ORDER:
            lines.append(_table_row(reg, stats))
    lines.append("")

    # --- By setup (matched only) ---
    lines += [
        sep,
        "  BY SETUP CLASS  (matched positions only)",
        sep,
        f"  {'Setup':<22s} {'N':>4s}  {'Win%':>5s}  {'Win bar':>12s}  {'AvgPNL':>8s}  {'NetPNL':>8s}  {'AvgHold':>7s}",
        sep,
    ]
    for setup, stats in result["by_setup"].items():
        label = (setup or "UNKNOWN")[:22]
        lines.append(_table_row(label, stats))
    lines.append("")

    # --- Narrative ---
    lines += [sep, "  KEY FINDINGS", sep]

    aln_stats = result["by_alignment"]
    if "trend-aligned" in aln_stats and "counter-trend" in aln_stats:
        ta = aln_stats["trend-aligned"]
        ct = aln_stats["counter-trend"]
        wr_diff = (ta["win_rate"] - ct["win_rate"]) * 100
        pnl_diff = ta["avg_pnl"] - ct["avg_pnl"]
        lines.append(
            f"  Trend-aligned vs counter-trend: +{wr_diff:.1f}pp win rate, "
            f"{'+' if pnl_diff >= 0 else ''}{pnl_diff:.4f} USDT avg PNL difference."
        )

    if "quiet" in aln_stats:
        q = aln_stats["quiet"]
        lines.append(
            f"  QUIET regime: {q['count']} trades, {q['win_rate']*100:.1f}% win rate, "
            f"avg net PNL {q['avg_net_pnl']:+.4f} USDT — "
            f"{'FEE BLEED CONFIRMED' if q['avg_net_pnl'] < 0 else 'net positive'}."
        )

    if "ranging" in aln_stats:
        r = aln_stats["ranging"]
        lines.append(
            f"  RANGING regime: {r['count']} trades, {r['win_rate']*100:.1f}% win rate, "
            f"avg net PNL {r['avg_net_pnl']:+.4f} USDT."
        )

    total_net = sum(pos["net_pnl_usdt"] for pos in result["matched"] + result["unmatched"])
    total_comm = sum(abs(pos["commission_usdt"]) for pos in result["matched"] + result["unmatched"])
    lines += [
        f"  Total net PNL (all {total} positions): {total_net:+.4f} USDT",
        f"  Total commissions paid: {total_comm:.4f} USDT",
        "",
    ]

    if show_unmatched and result["unmatched"]:
        lines += [sep, "  UNMATCHED POSITIONS (no engine signal found within window)", sep]
        for pos in result["unmatched"][:20]:
            ts = pos["open_time"].strftime("%Y-%m-%d %H:%M") if pos["open_time"] else "?"
            lines.append(
                f"  {pos['symbol']:<12s} {pos['direction']:<6s} "
                f"{ts}  PNL {pos['pnl_usdt']:+.4f}"
            )
        if len(result["unmatched"]) > 20:
            lines.append(f"  ... and {len(result['unmatched']) - 20} more")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _default_signals_path() -> Path:
    for candidate in (
        Path("/engine-data/signal_history.json"),
        Path("data/signal_history.json"),
        Path("../360-v2/data/signal_history.json"),
    ):
        if candidate.exists():
            return candidate
    return Path("signal_history.json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Regime-aware Binance position P&L analyser for the 360 Scalp Engine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("csv", type=Path, help="Binance Futures position history CSV file.")
    parser.add_argument(
        "--signals",
        type=Path,
        default=None,
        help="Path to engine signal_history.json. Auto-detected if omitted.",
    )
    parser.add_argument(
        "--match-window",
        type=int,
        default=900,
        metavar="SECONDS",
        help="Max seconds after signal detection to accept a position-open match. Default: 900.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write full JSON result to this path (in addition to console output).",
    )
    parser.add_argument(
        "--show-unmatched",
        action="store_true",
        help="List individual unmatched positions in the report.",
    )
    args = parser.parse_args()

    csv_path: Path = args.csv
    if not csv_path.exists():
        print(f"ERROR: CSV file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    signals_path: Path = args.signals or _default_signals_path()
    if not signals_path.exists():
        print(
            f"WARNING: Signal history not found at {signals_path}. "
            "Proceeding without regime tagging (all positions will be UNMATCHED).",
            file=sys.stderr,
        )
        signals: list[dict] = []
    else:
        signals = load_signals(signals_path)
        print(f"  Loaded {len(signals)} engine signals from {signals_path}", file=sys.stderr)

    positions, csv_warnings = load_binance_csv(csv_path)
    for w in csv_warnings:
        print(f"  CSV WARNING: {w}", file=sys.stderr)

    if not positions:
        print("ERROR: No positions parsed from CSV. Check column names and file format.", file=sys.stderr)
        print("  Expected headers (any subset):", file=sys.stderr)
        for alias, canonical in sorted(_COL_MAP.items()):
            print(f"    '{alias}' → {canonical}", file=sys.stderr)
        sys.exit(1)

    print(f"  Parsed {len(positions)} positions from {csv_path}", file=sys.stderr)

    signal_index = build_signal_index(signals)
    result = aggregate(positions, signal_index, args.match_window)

    report = render_report(result, show_unmatched=args.show_unmatched)
    print(report)

    if args.output:
        serialisable = {
            **result,
            "matched": [
                {**p, "open_time": p["open_time"].isoformat() if p.get("open_time") else None,
                 "close_time": p["close_time"].isoformat() if p.get("close_time") else None}
                for p in result["matched"]
            ],
            "unmatched": [
                {**p, "open_time": p["open_time"].isoformat() if p.get("open_time") else None,
                 "close_time": p["close_time"].isoformat() if p.get("close_time") else None}
                for p in result["unmatched"]
            ],
        }
        args.output.write_text(json.dumps(serialisable, indent=2))
        print(f"\n  Full JSON result written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()

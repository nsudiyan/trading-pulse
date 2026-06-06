#!/usr/bin/env python3
"""
calibration/pnl_report.py — P&L and R-multiple expectancy report.

Reads outcomes/resolved.csv and outputs:
  1. Per-setup expectancy table (WR, Avg R, expectancy, Sharpe)
  2. Equity curve plot  → calibration/equity_curve.png
  3. Daily P&L histogram → calibration/daily_pnl_hist.png

Usage:
  python3 calibration/pnl_report.py [--horizon 4h|24h] [--no-plots]

R-multiple is read from r_multiple_4h / r_multiple_24h if present.
Falls back to computing it from price_entry, stop, tp1, and outcome columns
so that historical rows without the new columns still work.
"""

import argparse
import csv
import math
import sys
from collections import defaultdict
from pathlib import Path

RESOLVED_CSV = Path(__file__).parent.parent / "outcomes" / "resolved.csv"
OUT_DIR = Path(__file__).parent


def _try_float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _derive_r(row: dict, horizon: str) -> float | None:
    """
    Return R-multiple for a row at given horizon.
    Prefers the pre-computed column; falls back to manual derivation.
    """
    pre = _try_float(row.get(f"r_multiple_{horizon}"))
    if pre is not None:
        return pre

    entry = _try_float(row.get("price_entry"))
    sl    = _try_float(row.get("stop"))
    tp1   = _try_float(row.get("tp1"))
    close = _try_float(row.get(f"price_{horizon}"))
    outcome = row.get(f"outcome_{horizon}", "")

    if entry is None or sl is None or close is None:
        return None
    risk = entry - sl
    if abs(risk) < 1e-12:
        return None

    if outcome == "TP1" and tp1:
        exit_px = tp1
    elif outcome == "STOP":
        exit_px = sl
    else:
        exit_px = close

    return round((exit_px - entry) / risk, 4)


def load_rows(horizon: str) -> list[dict]:
    """Load resolved.csv rows that have a resolved outcome for the given horizon."""
    if not RESOLVED_CSV.exists():
        print(f"[pnl_report] CSV not found: {RESOLVED_CSV}")
        return []
    rows = []
    with open(RESOLVED_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            outcome = row.get(f"outcome_{horizon}", "")
            if not outcome:
                continue
            r = _derive_r(row, horizon)
            row[f"_r_{horizon}"] = r
            rows.append(row)
    return rows


def expectancy_table(rows: list[dict], horizon: str) -> list[dict]:
    """Compute per-setup expectancy stats."""
    by_setup = defaultdict(list)
    for row in rows:
        r = row.get(f"_r_{horizon}")
        if r is None:
            continue
        by_setup[row.get("setup", "?")].append(r)

    stats = []
    for setup, rs in sorted(by_setup.items(), key=lambda x: -len(x[1])):
        n = len(rs)
        if n < 3:
            continue
        wins = [r for r in rs if r > 0]
        losses = [r for r in rs if r <= 0]
        wr = len(wins) / n
        avg_win  = sum(wins)  / len(wins)  if wins   else 0.0
        avg_loss = sum(losses)/ len(losses) if losses else 0.0
        expectancy = wr * avg_win + (1 - wr) * avg_loss
        # Sharpe-like: mean R / std R
        mean_r = sum(rs) / n
        var_r  = sum((r - mean_r) ** 2 for r in rs) / n
        sharpe = mean_r / math.sqrt(var_r) if var_r > 0 else 0.0
        stats.append({
            "setup":      setup,
            "n":          n,
            "wr_pct":     round(wr * 100, 1),
            "avg_r":      round(mean_r, 3),
            "expectancy": round(expectancy, 3),
            "sharpe":     round(sharpe, 3),
        })
    return sorted(stats, key=lambda x: -x["expectancy"])


def print_table(stats: list[dict], horizon: str):
    col_w = [14, 6, 7, 8, 12, 8]
    headers = ["Setup", "N", "WR%", "Avg R", "Expectancy", "Sharpe"]
    sep = "+" + "+".join("-" * (w + 2) for w in col_w) + "+"
    def row_str(vals):
        return "|" + "|".join(f" {str(v):<{w}} " for v, w in zip(vals, col_w)) + "|"

    print(f"\n  P&L / Expectancy Report — {horizon.upper()} horizon")
    print(sep)
    print(row_str(headers))
    print(sep)
    for s in stats:
        exp_str = f"{s['expectancy']:+.3f}"
        sharpe_str = f"{s['sharpe']:+.3f}"
        print(row_str([s["setup"], s["n"], f"{s['wr_pct']}%",
                       f"{s['avg_r']:+.3f}", exp_str, sharpe_str]))
    print(sep)

    if stats:
        total_n = sum(s["n"] for s in stats)
        all_exp = sum(s["expectancy"] * s["n"] for s in stats) / total_n
        print(f"\n  Total trades: {total_n}  |  Weighted expectancy: {all_exp:+.3f}R\n")


def plot_equity_curve(rows: list[dict], horizon: str, out_path: Path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from datetime import datetime
    except ImportError:
        print("[pnl_report] matplotlib not installed — skipping equity curve")
        return

    dated = []
    for row in rows:
        r = row.get(f"_r_{horizon}")
        ts_str = row.get("run_ts", "")
        if r is None or not ts_str:
            continue
        try:
            dt = datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
        dated.append((dt, r))

    if not dated:
        return

    dated.sort(key=lambda x: x[0])
    dts, rs = zip(*dated)
    cumulative = []
    total = 0.0
    for r in rs:
        total += r
        cumulative.append(total)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), height_ratios=[2, 1])
    fig.suptitle(f"Equity Curve — {horizon.upper()} Horizon  (N={len(rs)})", fontsize=13)

    # Equity curve
    ax1.plot(dts, cumulative, color="#2196F3", linewidth=1.5)
    ax1.axhline(0, color="gray", linestyle="--", linewidth=0.8)
    ax1.fill_between(dts, cumulative, 0,
                     where=[c >= 0 for c in cumulative], alpha=0.15, color="#4CAF50")
    ax1.fill_between(dts, cumulative, 0,
                     where=[c < 0 for c in cumulative], alpha=0.15, color="#f44336")
    ax1.set_ylabel("Cumulative R")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax1.xaxis.set_major_locator(mdates.DayLocator(interval=3))
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=30)
    ax1.grid(True, alpha=0.3)

    # Per-trade R bars
    colors = ["#4CAF50" if r > 0 else "#f44336" for r in rs]
    ax2.bar(dts, rs, color=colors, width=0.02, alpha=0.7)
    ax2.axhline(0, color="gray", linestyle="--", linewidth=0.8)
    ax2.set_ylabel("R per trade")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax2.xaxis.set_major_locator(mdates.DayLocator(interval=3))
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=30)
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Equity curve saved → {out_path}")


def plot_daily_pnl(rows: list[dict], horizon: str, out_path: Path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from datetime import datetime
        from collections import defaultdict
    except ImportError:
        return

    daily = defaultdict(list)
    for row in rows:
        r = row.get(f"_r_{horizon}")
        ts_str = row.get("run_ts", "")
        if r is None or not ts_str:
            continue
        try:
            day = datetime.strptime(ts_str[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        daily[day].append(r)

    if not daily:
        return

    days = sorted(daily.keys())
    daily_r = [sum(daily[d]) for d in days]

    fig, ax = plt.subplots(figsize=(12, 4))
    colors = ["#4CAF50" if r >= 0 else "#f44336" for r in daily_r]
    ax.bar(range(len(days)), daily_r, color=colors, alpha=0.8)
    ax.axhline(0, color="gray", linestyle="--", linewidth=0.8)
    ax.set_xticks(range(len(days)))
    ax.set_xticklabels([str(d)[5:] for d in days], rotation=45, fontsize=8)
    ax.set_ylabel("Daily R")
    ax.set_title(f"Daily P&L Histogram — {horizon.upper()} Horizon")
    ax.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Daily P&L histogram saved → {out_path}")


def main():
    parser = argparse.ArgumentParser(description="P&L and R-multiple report")
    parser.add_argument("--horizon", default="4h", choices=["4h", "24h"])
    parser.add_argument("--no-plots", action="store_true")
    args = parser.parse_args()

    rows = load_rows(args.horizon)
    if not rows:
        print("[pnl_report] No resolved rows found.")
        sys.exit(1)

    stats = expectancy_table(rows, args.horizon)
    print_table(stats, args.horizon)

    if not args.no_plots:
        plot_equity_curve(rows, args.horizon, OUT_DIR / "equity_curve.png")
        plot_daily_pnl(rows, args.horizon, OUT_DIR / "daily_pnl_hist.png")

    # Verification: print 5 sample trades with manual R calculation
    print("  Sample R-multiple verification (first 5 resolved trades):")
    print(f"  {'Symbol':<14} {'Setup':<12} {'Entry':>10} {'SL':>10} {'Exit':>10} "
          f"{'Outcome':<10} {'R':>7}")
    print("  " + "-" * 75)
    shown = 0
    for row in rows:
        if shown >= 5:
            break
        r = row.get(f"_r_{args.horizon}")
        if r is None:
            continue
        entry = _try_float(row.get("price_entry"), 0)
        sl    = _try_float(row.get("stop"), 0)
        outcome = row.get(f"outcome_{args.horizon}", "")
        tp1   = _try_float(row.get("tp1"), 0)
        close = _try_float(row.get(f"price_{args.horizon}"), 0)
        exit_px = tp1 if outcome == "TP1" else (sl if outcome == "STOP" else close)
        print(f"  {row.get('symbol','?'):<14} {row.get('setup','?'):<12} "
              f"{entry:>10.5g} {sl:>10.5g} {exit_px:>10.5g} {outcome:<10} {r:>+7.3f}R")
        shown += 1
    print()


if __name__ == "__main__":
    main()

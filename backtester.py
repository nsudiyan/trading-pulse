"""
Walk-forward backtester for resolved trade outcomes.
Usage: python3 backtester.py [--csv PATH] [--out PATH] [--horizon 4h|24h]
"""

import csv
import math
import sys
import argparse
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

BASE_DIR     = Path(__file__).parent
CSV_PATH     = BASE_DIR / "outcomes" / "resolved.csv"
REPORT_PATH  = BASE_DIR / "outcomes" / "backtest_report.md"

WIN_OUTCOMES  = {"TP1", "WIN"}
LOSS_OUTCOMES = {"STOP", "LOSS"}

# ─── helpers ────────────────────────────────────────────────────────────────

def _parse_ts(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def load_csv(path: Path) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["_ts"] = _parse_ts(row.get("run_ts", ""))
            row["score"] = float(row.get("score") or 0)
            row["rsi_1h"] = _safe_float(row.get("rsi_1h"))
            row["hour"] = row["_ts"].hour
            rows.append(row)
    rows.sort(key=lambda r: r["_ts"])
    return rows


def _safe_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def _outcome(row: dict, horizon: str) -> str:
    return row.get(f"outcome_{horizon}", "")


def _is_win(row: dict, horizon: str) -> bool:
    return _outcome(row, horizon) in WIN_OUTCOMES


def _is_decided(row: dict, horizon: str) -> bool:
    return _outcome(row, horizon) in WIN_OUTCOMES | LOSS_OUTCOMES


def wr_stats(rows: list[dict], horizon: str) -> dict:
    decided = [r for r in rows if _is_decided(r, horizon)]
    if not decided:
        return {"n": 0, "wins": 0, "wr": float("nan"), "pf": float("nan")}
    wins   = sum(1 for r in decided if _is_win(r, horizon))
    losses = len(decided) - wins
    pf     = wins / losses if losses else float("inf")
    return {"n": len(decided), "wins": wins, "wr": wins / len(decided), "pf": pf}


def drawdown(equity_curve: list[float]) -> float:
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        if peak == 0:
            continue
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd
    return max_dd


# ─── equity simulation (flat risk per trade) ────────────────────────────────

def simulate_equity(rows: list[dict], horizon: str, risk_r: float = 1.0,
                    reward_r: float = 1.5) -> list[float]:
    """Simple R-multiple sim: win = +reward_r, loss = -risk_r."""
    eq = [0.0]
    for r in rows:
        if not _is_decided(r, horizon):
            continue
        if _is_win(r, horizon):
            eq.append(eq[-1] + reward_r)
        else:
            eq.append(eq[-1] - risk_r)
    return eq


# ─── walk-forward ────────────────────────────────────────────────────────────

def walk_forward(rows: list[dict], horizon: str, train_frac: float = 0.6) -> dict:
    decided = [r for r in rows if _is_decided(r, horizon)]
    if len(decided) < 10:
        return {}
    split   = int(len(decided) * train_frac)
    train   = decided[:split]
    test    = decided[split:]
    return {
        "train": wr_stats(train, horizon),
        "test":  wr_stats(test,  horizon),
        "train_period": (train[0]["_ts"].date(), train[-1]["_ts"].date()),
        "test_period":  (test[0]["_ts"].date(),  test[-1]["_ts"].date()),
    }


# ─── per-setup breakdown ─────────────────────────────────────────────────────

def by_setup(rows: list[dict], horizon: str) -> dict[str, dict]:
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[r.get("setup", "unknown")].append(r)
    return {s: wr_stats(g, horizon) for s, g in sorted(groups.items())}


# ─── per-direction breakdown ─────────────────────────────────────────────────

def by_direction(rows: list[dict], horizon: str) -> dict[str, dict]:
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[r.get("direction", "?")].append(r)
    return {d: wr_stats(g, horizon) for d, g in sorted(groups.items())}


# ─── score threshold sensitivity ─────────────────────────────────────────────

def score_sensitivity(rows: list[dict], horizon: str,
                      thresholds=(40, 50, 60, 70, 80, 90)) -> list[dict]:
    results = []
    for thr in thresholds:
        subset = [r for r in rows if r["score"] >= thr]
        s = wr_stats(subset, horizon)
        s["min_score"] = thr
        results.append(s)
    return results


# ─── time-of-day analysis ────────────────────────────────────────────────────

def hourly_wr(rows: list[dict], horizon: str) -> dict[int, dict]:
    groups: dict[int, list] = defaultdict(list)
    for r in rows:
        groups[r["hour"]].append(r)
    return {h: wr_stats(g, horizon) for h, g in sorted(groups.items())}


# ─── RSI at entry analysis ───────────────────────────────────────────────────

def rsi_buckets(rows: list[dict], horizon: str) -> dict[str, dict]:
    buckets = {"<30": [], "30-50": [], "50-70": [], ">70": []}
    for r in rows:
        v = r["rsi_1h"]
        if math.isnan(v):
            continue
        if v < 30:
            buckets["<30"].append(r)
        elif v < 50:
            buckets["30-50"].append(r)
        elif v < 70:
            buckets["50-70"].append(r)
        else:
            buckets[">70"].append(r)
    return {b: wr_stats(g, horizon) for b, g in buckets.items()}


# ─── combo: setup × direction ────────────────────────────────────────────────

def setup_dir_combos(rows: list[dict], horizon: str, min_n: int = 5) -> list[dict]:
    groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        key = (r.get("setup", "?"), r.get("direction", "?"))
        groups[key].append(r)
    results = []
    for (setup, direction), g in sorted(groups.items()):
        s = wr_stats(g, horizon)
        if s["n"] >= min_n:
            s["setup"] = setup
            s["direction"] = direction
            results.append(s)
    results.sort(key=lambda x: x.get("wr", 0), reverse=True)
    return results


# ─── report formatting ───────────────────────────────────────────────────────

def _pct(v) -> str:
    if math.isnan(v):
        return "n/a"
    return f"{v*100:.1f}%"


def _pf(v) -> str:
    if math.isnan(v) or v == float("inf"):
        return "∞"
    return f"{v:.2f}"


def build_report(rows: list[dict], horizon: str) -> str:
    lines = []
    total = len(rows)
    decided_all = [r for r in rows if _is_decided(r, horizon)]

    lines.append(f"# Walk-Forward Backtest Report")
    lines.append(f"**Horizon**: {horizon}  |  **Total signals**: {total}  |  **Decided**: {len(decided_all)}")
    lines.append(f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # ── Overall stats ──
    ov = wr_stats(rows, horizon)
    eq = simulate_equity(decided_all, horizon)
    dd = drawdown(eq) if len(eq) > 1 else 0.0
    lines.append("## Overall")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Signals decided | {ov['n']} |")
    lines.append(f"| Win rate | {_pct(ov['wr'])} |")
    lines.append(f"| Profit factor | {_pf(ov['pf'])} |")
    lines.append(f"| Max drawdown (R) | {dd*100:.1f}% of peak |")
    lines.append(f"| Final equity (R) | {eq[-1]:.1f}R |")
    lines.append("")

    # ── Walk-forward ──
    wf = walk_forward(rows, horizon)
    if wf:
        tr, te = wf["train"], wf["test"]
        tp, pp  = wf["train_period"], wf["test_period"]
        lines.append("## Walk-Forward Split (60% train / 40% test)")
        lines.append(f"| Period | Dates | N | WR | PF |")
        lines.append(f"|--------|-------|---|----|----|")
        lines.append(f"| Train  | {tp[0]} → {tp[1]} | {tr['n']} | {_pct(tr['wr'])} | {_pf(tr['pf'])} |")
        lines.append(f"| Test   | {pp[0]} → {pp[1]} | {te['n']} | {_pct(te['wr'])} | {_pf(te['pf'])} |")
        delta_wr = te["wr"] - tr["wr"] if not (math.isnan(te["wr"]) or math.isnan(tr["wr"])) else float("nan")
        verdict = "⚠️ possible overfit" if not math.isnan(delta_wr) and abs(delta_wr) > 0.15 else "✅ generalizes"
        lines.append(f"\n**WR delta (test − train)**: {_pct(delta_wr)}  →  {verdict}")
        lines.append("")

    # ── Per-setup ──
    lines.append("## By Setup")
    lines.append(f"| Setup | N | WR | PF |")
    lines.append(f"|-------|---|----|----|")
    for setup, s in by_setup(rows, horizon).items():
        lines.append(f"| {setup} | {s['n']} | {_pct(s['wr'])} | {_pf(s['pf'])} |")
    lines.append("")

    # ── Per-direction ──
    lines.append("## By Direction")
    lines.append(f"| Direction | N | WR | PF |")
    lines.append(f"|-----------|---|----|----|")
    for d, s in by_direction(rows, horizon).items():
        lines.append(f"| {d} | {s['n']} | {_pct(s['wr'])} | {_pf(s['pf'])} |")
    lines.append("")

    # ── Setup × Direction combos ──
    combos = setup_dir_combos(rows, horizon)
    if combos:
        lines.append("## Best Setup × Direction Combos (n≥5, sorted by WR)")
        lines.append(f"| Setup | Dir | N | WR | PF |")
        lines.append(f"|-------|-----|---|----|----|")
        for c in combos[:10]:
            lines.append(f"| {c['setup']} | {c['direction']} | {c['n']} | {_pct(c['wr'])} | {_pf(c['pf'])} |")
        lines.append("")

    # ── Score sensitivity ──
    lines.append("## Score Threshold Sensitivity")
    lines.append(f"| Min Score | N | WR | PF |")
    lines.append(f"|-----------|---|----|----|")
    for s in score_sensitivity(rows, horizon):
        lines.append(f"| ≥{s['min_score']} | {s['n']} | {_pct(s['wr'])} | {_pf(s['pf'])} |")
    lines.append("")

    # ── Hourly ──
    hw = hourly_wr(rows, horizon)
    # show top 8 worst hours
    worst = sorted(
        [(h, s) for h, s in hw.items() if s["n"] >= 5 and not math.isnan(s["wr"])],
        key=lambda x: x[1]["wr"]
    )[:8]
    best = sorted(
        [(h, s) for h, s in hw.items() if s["n"] >= 5 and not math.isnan(s["wr"])],
        key=lambda x: -x[1]["wr"]
    )[:8]
    lines.append("## Hourly Performance (UTC, n≥5)")
    lines.append("### Best hours")
    lines.append(f"| Hour (UTC) | N | WR | PF |")
    lines.append(f"|------------|---|----|----|")
    for h, s in best:
        lines.append(f"| {h:02d}:00 | {s['n']} | {_pct(s['wr'])} | {_pf(s['pf'])} |")
    lines.append("\n### Worst hours")
    lines.append(f"| Hour (UTC) | N | WR | PF |")
    lines.append(f"|------------|---|----|----|")
    for h, s in worst:
        lines.append(f"| {h:02d}:00 | {s['n']} | {_pct(s['wr'])} | {_pf(s['pf'])} |")
    lines.append("")

    # ── RSI buckets ──
    lines.append("## RSI at Entry")
    lines.append(f"| RSI range | N | WR | PF |")
    lines.append(f"|-----------|---|----|----|")
    for bucket, s in rsi_buckets(rows, horizon).items():
        lines.append(f"| {bucket} | {s['n']} | {_pct(s['wr'])} | {_pf(s['pf'])} |")
    lines.append("")

    # ── Key takeaways ──
    lines.append("## Key Takeaways")
    takeaways = []

    # best setup
    setups_sorted = sorted(
        [(s, v) for s, v in by_setup(rows, horizon).items() if v["n"] >= 10 and not math.isnan(v["wr"])],
        key=lambda x: -x[1]["wr"]
    )
    if setups_sorted:
        best_s, best_v = setups_sorted[0]
        worst_s, worst_v = setups_sorted[-1]
        takeaways.append(f"- Best setup: **{best_s}** ({_pct(best_v['wr'])} WR, n={best_v['n']})")
        takeaways.append(f"- Worst setup: **{worst_s}** ({_pct(worst_v['wr'])} WR, n={worst_v['n']})")

    # score threshold sweet spot
    sens = score_sensitivity(rows, horizon)
    best_thr = max((s for s in sens if s["n"] >= 20), key=lambda x: x["wr"], default=None)
    if best_thr:
        takeaways.append(f"- Optimal score filter: **≥{best_thr['min_score']}** ({_pct(best_thr['wr'])} WR, n={best_thr['n']})")

    # bad hours
    bad_hours = [h for h, s in hw.items() if s["n"] >= 5 and not math.isnan(s["wr"]) and s["wr"] < 0.40]
    if bad_hours:
        takeaways.append(f"- Avoid UTC hours: {sorted(bad_hours)} (WR < 40%)")

    # overfit warning
    if wf and not math.isnan(delta_wr) and abs(delta_wr) > 0.15:
        takeaways.append(f"- ⚠️ Train/test WR gap {_pct(abs(delta_wr))} — signals may be overfit to recent market regime")

    if not takeaways:
        takeaways.append("- Not enough data for strong conclusions yet")

    lines.extend(takeaways)
    lines.append("")

    return "\n".join(lines)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Walk-forward backtester")
    parser.add_argument("--csv",     default=str(CSV_PATH),    help="Path to resolved.csv")
    parser.add_argument("--out",     default=str(REPORT_PATH), help="Output report path")
    parser.add_argument("--horizon", default="4h", choices=["4h", "24h"], help="Outcome horizon")
    parser.add_argument("--print-only", action="store_true",   help="Print report, don't save")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"[backtest] CSV not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    rows = load_csv(csv_path)
    print(f"[backtest] Loaded {len(rows)} rows from {csv_path.name}", file=sys.stderr)

    report = build_report(rows, args.horizon)
    print(report)

    if not args.print_only:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"\n[backtest] Report saved → {out}", file=sys.stderr)


if __name__ == "__main__":
    main()

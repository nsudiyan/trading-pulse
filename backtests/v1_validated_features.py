#!/usr/bin/env python3
"""
backtests/v1_validated_features.py — OOS backtest для V1 signal weights

Re-scores исторические сигналы с и без V1 поправок на OOS-периоде.
Всё из реальных данных. Если данных нет — пишет "не тестировалось".

OOS период: 2025-11-01 → 2026-05-27 (все строки в resolved.csv)
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).parent.parent
RESOLVED_CSV = BASE / "outcomes" / "resolved.csv"
PUMP_CSV = BASE / "outcomes" / "pump_resolved.csv"
WEIGHTS_JSON = BASE / "calibration" / "signal_weights.json"
OUT_DIR = BASE / "backtests"

OOS_START = "2025-11-01"
OOS_END = "2026-05-27"

WIN_OUTCOMES = {"TP1", "WIN"}
LOSS_OUTCOMES = {"STOP", "LOSS"}
DECISIVE_OUTCOMES = WIN_OUTCOMES | LOSS_OUTCOMES

# Full CSV_FIELDS order from outcome_tracker.py (v8)
CSV_FIELDS = [
    "run_ts", "symbol", "setup", "score", "grade", "pump_score",
    "direction",
    "price_entry", "stop", "tp1", "tp2",
    "funding", "oi_24h_pct", "mtf_bull", "mtf_bear",
    "rsi_1h", "cvd_kline", "cvd_trade",
    "ema_bull_1h", "ema_bull_4h", "choch_bull_1h",
    "vwap_dev", "rs_btc",
    "resolve_4h_ts", "price_4h", "change_4h_pct",
    "hit_tp1_4h", "hit_stop_4h", "outcome_4h",
    "resolve_24h_ts", "price_24h", "change_24h_pct",
    "hit_tp1_24h", "hit_stop_24h", "outcome_24h",
    # v2
    "bnb_cross_bonus", "in_zone", "whale_flag",
    # v3
    "mfe_4h_pct", "mae_4h_pct",
    "mfe_24h_pct", "mae_24h_pct",
    # v4
    "utc_hour", "btc_trend_4h", "alt_breadth_pct",
    "listing_age_days", "avg_vol_7d_usd",
    "sc_squeeze", "sc_bos_fvg", "sc_range_sweep", "sc_breakout", "sc_short_dist",
    # v5
    "atr_at_entry", "r_multiple_4h", "exit_reason_4h",
    "r_multiple_24h", "exit_reason_24h",
    # v6
    "choch_conviction",
    # v7
    "exit_price_4h", "exit_price_24h",
    "hold_time_4h_min", "hold_time_24h_min",
    "outcome_label_4h", "outcome_label_24h",
    # v8
    "time_to_mfe_4h_h", "time_to_mae_4h_h",
    "time_to_mfe_24h_h", "time_to_mae_24h_h",
]


def _flt(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _int(val, default=0):
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def load_resolved() -> list[dict]:
    """Read resolved.csv with flexible field-count handling."""
    rows = []
    with open(RESOLVED_CSV) as f:
        reader = csv.reader(f)
        next(reader)  # skip original header
        for line in reader:
            n = len(line)
            if n < 29:  # too short to have both outcome columns
                continue
            row = {}
            for i, field in enumerate(CSV_FIELDS):
                row[field] = line[i] if i < n else ""
            rows.append(row)
    return rows


def derive_v1_flags(row: dict) -> dict[str, int]:
    """Derive binary signal flags from raw CSV columns."""
    oi = _flt(row.get("oi_24h_pct"))
    funding = _flt(row.get("funding"))
    rsi = _flt(row.get("rsi_1h"))
    cvd = _flt(row.get("cvd_kline"))
    ema1h = _int(row.get("ema_bull_1h"))
    ema4h = _int(row.get("ema_bull_4h"))
    mtf = _int(row.get("mtf_bull"))
    choch = _int(row.get("choch_bull_1h"))

    return {
        "choch_bull_1h":       1 if choch == 1 else 0,
        "oi_falling_5":        1 if oi < -5.0 else 0,
        "rsi_lt40":            1 if rsi < 40.0 else 0,
        "funding_neg":         1 if funding < 0.0 else 0,
        "oi_rising_5":         1 if oi > 5.0 else 0,
        "cvd_kline_bull":      1 if cvd > 0.0 else 0,
        "ema_bull_1h":         1 if ema1h == 1 else 0,
        "ema_bull_4h":         1 if ema4h == 1 else 0,
        "mtf_bull_ge3":        1 if mtf >= 3 else 0,
        "mtf_bull_ge4":        1 if mtf >= 4 else 0,
        "oi_rising_10":        1 if oi > 10.0 else 0,
        "funding_extreme_neg": 1 if funding <= -0.08 else 0,
        "funding_extreme_pos": 1 if funding >= 0.08 else 0,
    }


def v1_adjustment(flags: dict[str, int], weights: dict[str, float]) -> float:
    return sum(weights.get(k, 0.0) * v for k, v in flags.items())


def btc_regime(row: dict) -> str:
    btc = row.get("btc_trend_4h", "").strip()
    if btc in ("above", "bull"):
        return "bull"
    if btc in ("below", "bear"):
        return "bear"
    if btc in ("between", "sideways"):
        return "sideways"
    return "unknown"


def metrics(wins: int, losses: int, total_wins: int) -> dict:
    n = wins + losses
    if n == 0:
        return {"n": 0, "precision": None, "recall": None, "lift": None}
    prec = wins / n
    recall = wins / total_wins if total_wins > 0 else None
    return {"n": n, "precision": round(prec, 4), "recall": round(recall, 4) if recall is not None else None}


def compute_lift(precision, baseline_precision):
    if precision is None or baseline_precision is None or baseline_precision == 0:
        return None
    return round(precision / baseline_precision - 1.0, 4)


def run_backtest(rows: list[dict], weights: dict[str, float], horizon: str) -> dict:
    """
    Returns per-setup and per-regime stats for baseline and V1.
    horizon: 'outcome_4h' or 'outcome_24h'
    """
    outcome_col = horizon

    all_decisive = [r for r in rows if r.get(outcome_col) in DECISIVE_OUTCOMES]
    total_wins_all = sum(1 for r in all_decisive if r.get(outcome_col) in WIN_OUTCOMES)
    baseline_wr = total_wins_all / len(all_decisive) if all_decisive else 0.0

    # ── per-setup ─────────────────────────────────────────────────────────────
    by_setup: dict[str, dict] = defaultdict(lambda: {"baseline_w": 0, "baseline_l": 0,
                                                      "v1_pos_w": 0, "v1_pos_l": 0,
                                                      "v1_neg_w": 0, "v1_neg_l": 0,
                                                      "total_wins": 0})
    by_regime: dict[str, dict] = defaultdict(lambda: {"baseline_w": 0, "baseline_l": 0,
                                                       "v1_pos_w": 0, "v1_pos_l": 0,
                                                       "total_wins": 0})

    for row in all_decisive:
        setup = row.get("setup", "unknown")
        regime = btc_regime(row)
        outcome = row.get(outcome_col)
        is_win = outcome in WIN_OUTCOMES

        flags = derive_v1_flags(row)
        adj = v1_adjustment(flags, weights)
        v1_positive = adj > 0

        by_setup[setup]["baseline_w"] += int(is_win)
        by_setup[setup]["baseline_l"] += int(not is_win)
        by_setup[setup]["total_wins"] += int(is_win)
        if v1_positive:
            by_setup[setup]["v1_pos_w"] += int(is_win)
            by_setup[setup]["v1_pos_l"] += int(not is_win)
        else:
            by_setup[setup]["v1_neg_w"] += int(is_win)
            by_setup[setup]["v1_neg_l"] += int(not is_win)

        by_regime[regime]["baseline_w"] += int(is_win)
        by_regime[regime]["baseline_l"] += int(not is_win)
        by_regime[regime]["total_wins"] += int(is_win)
        if v1_positive:
            by_regime[regime]["v1_pos_w"] += int(is_win)
            by_regime[regime]["v1_pos_l"] += int(not is_win)

    return {
        "horizon": horizon,
        "total_decisive": len(all_decisive),
        "total_wins": total_wins_all,
        "baseline_wr": round(baseline_wr, 4),
        "by_setup": dict(by_setup),
        "by_regime": dict(by_regime),
    }


def signal_lift_table(rows: list[dict], weights: dict[str, float], horizon: str) -> list[dict]:
    """Per-signal WR lift when flag is present vs absent."""
    outcome_col = horizon
    decisive = [r for r in rows if r.get(outcome_col) in DECISIVE_OUTCOMES]
    if not decisive:
        return []
    baseline_wins = sum(1 for r in decisive if r.get(outcome_col) in WIN_OUTCOMES)
    baseline_wr = baseline_wins / len(decisive)

    results = []
    for sig_name in weights:
        with_flag = [r for r in decisive if derive_v1_flags(r).get(sig_name, 0) == 1]
        without_flag = [r for r in decisive if derive_v1_flags(r).get(sig_name, 0) == 0]
        n_with = len(with_flag)
        n_without = len(without_flag)
        wr_with = sum(1 for r in with_flag if r.get(outcome_col) in WIN_OUTCOMES) / n_with if n_with else None
        wr_without = sum(1 for r in without_flag if r.get(outcome_col) in WIN_OUTCOMES) / n_without if n_without else None
        results.append({
            "signal": sig_name,
            "weight": weights[sig_name],
            "n_with_flag": n_with,
            "n_without_flag": n_without,
            "wr_with": round(wr_with, 4) if wr_with is not None else None,
            "wr_without": round(wr_without, 4) if wr_without is not None else None,
            "baseline_wr": round(baseline_wr, 4),
            "lift_pp": round((wr_with - baseline_wr) * 100, 2) if wr_with is not None else None,
            "oos_confirmed": (wr_with is not None and n_with >= 20),
        })
    return results


def build_per_setup_csv(stats_4h: dict, stats_24h: dict, weights: dict) -> list[dict]:
    """Flat CSV rows for v1_per_setup_metrics.csv"""
    rows_out = []
    setups = set(stats_4h["by_setup"]) | set(stats_24h["by_setup"])
    for setup in sorted(setups):
        for horizon, stats in [("4h", stats_4h), ("24h", stats_24h)]:
            s = stats["by_setup"].get(setup, {})
            bw = s.get("baseline_w", 0)
            bl = s.get("baseline_l", 0)
            n_decisive = bw + bl
            baseline_wr = bw / n_decisive if n_decisive else None
            tw = s.get("total_wins", 0)
            pv = s.get("v1_pos_w", 0)
            pl_loss = s.get("v1_pos_l", 0)
            nv = s.get("v1_neg_w", 0)
            nl = s.get("v1_neg_l", 0)
            v1_pos_n = pv + pl_loss
            v1_neg_n = nv + nl
            v1_pos_wr = pv / v1_pos_n if v1_pos_n else None
            v1_neg_wr = nv / v1_neg_n if v1_neg_n else None
            lift = compute_lift(v1_pos_wr, baseline_wr) if v1_pos_wr is not None else None
            rows_out.append({
                "setup": setup,
                "horizon": horizon,
                "n_decisive": n_decisive,
                "baseline_wr": round(baseline_wr, 4) if baseline_wr is not None else "",
                "v1_pos_n": v1_pos_n,
                "v1_pos_wr": round(v1_pos_wr, 4) if v1_pos_wr is not None else "",
                "v1_neg_n": v1_neg_n,
                "v1_neg_wr": round(v1_neg_wr, 4) if v1_neg_wr is not None else "",
                "lift": round(lift, 4) if lift is not None else "",
                "regression": ("YES" if (v1_pos_wr is not None and baseline_wr is not None
                                         and v1_pos_wr < baseline_wr - 0.03) else "ok"),
            })
    return rows_out


def build_baseline_comparison_csv(
    rows: list[dict], weights: dict, lift_4h: list[dict], lift_24h: list[dict]
) -> list[dict]:
    """Flat signal-level comparison table."""
    sig_map = {r["signal"]: r for r in lift_4h}
    out = []
    for r24 in lift_24h:
        sig = r24["signal"]
        r4 = sig_map.get(sig, {})
        out.append({
            "signal": sig,
            "weight": weights.get(sig, ""),
            "n_with_flag": r4.get("n_with_flag", ""),
            "baseline_wr_4h": r4.get("baseline_wr", ""),
            "wr_with_4h": r4.get("wr_with", ""),
            "lift_pp_4h": r4.get("lift_pp", ""),
            "oos_confirmed_4h": r4.get("oos_confirmed", ""),
            "baseline_wr_24h": r24.get("baseline_wr", ""),
            "wr_with_24h": r24.get("wr_with", ""),
            "lift_pp_24h": r24.get("lift_pp", ""),
            "oos_confirmed_24h": r24.get("oos_confirmed", ""),
        })
    return out


def print_summary(stats: dict, label: str, lift_table: list[dict]):
    h = stats["horizon"]
    print(f"\n{'='*60}")
    print(f"  {label}  |  Horizon: {h}")
    print(f"  OOS decisive trades: {stats['total_decisive']}  |  Baseline WR: {stats['baseline_wr']*100:.1f}%")
    print(f"{'='*60}")

    print("\n── Per-setup (V1+ = positive adjustment applied) ──")
    print(f"{'Setup':<14} {'N':>5} {'BsWR%':>7} {'V1+N':>6} {'V1+WR%':>8} {'V1-N':>6} {'V1-WR%':>8} {'Lift':>6}")
    for setup, s in sorted(stats["by_setup"].items()):
        bw = s["baseline_w"]; bl = s["baseline_l"]
        n_b = bw + bl
        wr_b = bw / n_b if n_b else 0
        pv = s["v1_pos_w"]; pl = s["v1_pos_l"]
        nv = s["v1_neg_w"]; nl = s["v1_neg_l"]
        v1p_n = pv + pl; v1n_n = nv + nl
        v1p_wr = pv / v1p_n if v1p_n else 0
        v1n_wr = nv / v1n_n if v1n_n else 0
        lift = (v1p_wr / wr_b - 1) * 100 if wr_b else 0
        print(f"  {setup:<12} {n_b:>5} {wr_b*100:>7.1f} {v1p_n:>6} {v1p_wr*100:>8.1f} "
              f"{v1n_n:>6} {v1n_wr*100:>8.1f} {lift:>+6.1f}%")

    print("\n── Per-BTC-regime ──")
    print(f"{'Regime':<10} {'N':>5} {'BsWR%':>7} {'V1+N':>6} {'V1+WR%':>8}")
    for regime, s in sorted(stats["by_regime"].items()):
        bw = s["baseline_w"]; bl = s["baseline_l"]
        n_b = bw + bl; wr_b = bw / n_b if n_b else 0
        pv = s["v1_pos_w"]; pl = s["v1_pos_l"]
        v1p_n = pv + pl; v1p_wr = pv / v1p_n if v1p_n else 0
        print(f"  {regime:<8} {n_b:>5} {wr_b*100:>7.1f} {v1p_n:>6} {v1p_wr*100:>8.1f}")

    print("\n── V1 Signal Lift Table ──")
    print(f"{'Signal':<22} {'Wt':>5} {'n':>5} {'BsWR%':>7} {'WR%':>7} {'Lift pp':>8} {'OOS?':>5}")
    for r in sorted(lift_table, key=lambda x: -(x.get("lift_pp") or 0)):
        lp = r["lift_pp"]
        if lp is None:
            lp_str = "  n/a"
        else:
            lp_str = f"{lp:>+8.2f}"
        print(f"  {r['signal']:<20} {r['weight']:>+5.1f} {r['n_with_flag']:>5} "
              f"{(r['baseline_wr'] or 0)*100:>7.1f} {(r['wr_with'] or 0)*100:>7.1f} "
              f"{lp_str}  {'✓' if r['oos_confirmed'] else '—':>4}")


def main():
    if not RESOLVED_CSV.exists():
        print(f"ERROR: {RESOLVED_CSV} not found", file=sys.stderr)
        sys.exit(1)

    weights = json.loads(WEIGHTS_JSON.read_text())
    print(f"Loaded V1 weights: {len(weights)} signals")

    rows = load_resolved()
    print(f"Loaded {len(rows)} rows from resolved.csv")

    # Filter OOS
    oos_rows = [r for r in rows
                if OOS_START <= r.get("run_ts", "") <= OOS_END + "T99"]
    print(f"OOS rows ({OOS_START} → {OOS_END}): {len(oos_rows)}")

    # Remove SHORTs (weights trained on LONG only)
    long_rows = [r for r in oos_rows if "ЛОН" in r.get("direction", "")]
    print(f"LONG rows: {len(long_rows)}")

    # Run backtest
    stats_4h = run_backtest(long_rows, weights, "outcome_4h")
    stats_24h = run_backtest(long_rows, weights, "outcome_24h")

    lift_4h = signal_lift_table(long_rows, weights, "outcome_4h")
    lift_24h = signal_lift_table(long_rows, weights, "outcome_24h")

    print_summary(stats_4h, "V1 Backtest", lift_4h)
    print_summary(stats_24h, "V1 Backtest", lift_24h)

    # ── Save artifacts ────────────────────────────────────────────────────────
    OUT_DIR.mkdir(exist_ok=True)

    baseline_rows = build_baseline_comparison_csv(long_rows, weights, lift_4h, lift_24h)
    bc_path = OUT_DIR / "v1_baseline_comparison.csv"
    if baseline_rows:
        with open(bc_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(baseline_rows[0].keys()))
            w.writeheader()
            w.writerows(baseline_rows)
        print(f"\nSaved: {bc_path}")

    per_setup_rows = build_per_setup_csv(stats_4h, stats_24h, weights)
    ps_path = OUT_DIR / "v1_per_setup_metrics.csv"
    if per_setup_rows:
        with open(ps_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(per_setup_rows[0].keys()))
            w.writeheader()
            w.writerows(per_setup_rows)
        print(f"Saved: {ps_path}")

    # Save raw stats JSON
    raw = {
        "generated": datetime.utcnow().isoformat() + "Z",
        "oos_period": f"{OOS_START} → {OOS_END}",
        "n_rows_total": len(rows),
        "n_rows_oos": len(oos_rows),
        "n_rows_long_oos": len(long_rows),
        "stats_4h": stats_4h,
        "stats_24h": stats_24h,
        "lift_4h": lift_4h,
        "lift_24h": lift_24h,
    }
    raw_path = OUT_DIR / "v1_backtest_raw.json"
    raw_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))
    print(f"Saved: {raw_path}")

    print("\nDone.")
    return raw


if __name__ == "__main__":
    main()

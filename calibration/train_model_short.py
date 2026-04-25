#!/usr/bin/env python3
"""
Train logistic regression on SHORT decisive trades from resolved.csv.
Produces calibration/signal_weights_short.json — additive score-point adjustments
for SHORT setups (short_dist, bos_fvg SHORT, range_sweep SHORT).

Key finding from ANALYSIS.md: for SHORT signals, score > 150 WR = 33.8% vs
47.5% for score <= 150. High score is anti-correlated with SHORT WR.

Outputs:
  calibration/signal_weights_short.json  — additive adjustments per signal flag
  calibration/model_report_short.md      — AUC, feature importances, WR stats
"""

import csv
import json
import sys
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

ROOT = Path(__file__).parent.parent
OUTCOMES_CSV = ROOT / "outcomes" / "resolved.csv"
OUTPUT_JSON  = Path(__file__).parent / "signal_weights_short.json"
OUTPUT_REPORT = Path(__file__).parent / "model_report_short.md"

FEATURE_NAMES = [
    "score_raw",      # raw score (anti-correlated for shorts — high score = overbought short setup)
    "funding",        # positive funding = longs paying shorts = more fuel for short squeeze reversal risk
    "oi_24h_pct",     # OI change — rising OI on short = new shorts entering
    "rsi_1h",         # RSI 1H — high RSI = overbought = good short entry
    "cvd_kline",      # CVD kline — negative = sellers dominant = confirms short
    "mtf_bear_count", # bear MTF count — more bearish confluence
    "ema_bull_1h",    # EMA bull order 1H — works against short direction
    "ema_bull_4h",    # EMA bull order 4H — works against short direction
    "vwap_dev",       # VWAP deviation — positive = price above VWAP = good short entry
    "rs_btc",         # relative strength vs BTC
]


def _fi(v, d=0):
    try: return int(float(v))
    except: return d


def _ff(v, d=0.0):
    try: return float(v)
    except: return d


def build_features(row):
    return [
        _ff(row.get("score",         "0")),
        _ff(row.get("funding",       "0")),
        _ff(row.get("oi_24h_pct",    "0")),
        _ff(row.get("rsi_1h",        "50")),
        _ff(row.get("cvd_kline",     "0")),
        float(_fi(row.get("mtf_bear", "0"))),
        float(_fi(row.get("ema_bull_1h", "0")) == 1),
        float(_fi(row.get("ema_bull_4h", "0")) == 1),
        _ff(row.get("vwap_dev",      "0")),
        _ff(row.get("rs_btc",        "0")),
    ]


def load_data():
    X, y = [], []
    with open(OUTCOMES_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("direction") != "ШОРТ":
                continue
            outcome = row.get("outcome_4h", "")
            if outcome in ("TP1", "WIN"):
                yi = 1
            elif outcome in ("STOP", "LOSS"):
                yi = 0
            else:
                continue
            X.append(build_features(row))
            y.append(yi)
    return np.array(X, dtype=float), np.array(y, dtype=int)


def main():
    print("Loading SHORT data...")
    X, y = load_data()
    if len(X) < 50:
        print(f"ERROR: only {len(X)} decisive SHORT trades (need >= 50)", file=sys.stderr)
        sys.exit(1)

    n = len(X)
    baseline_wr = y.mean()
    print(f"Samples: {n}, baseline WR: {baseline_wr:.1%}")

    # 5-fold stratified CV for evaluation
    pipe = Pipeline([
        ("sc", StandardScaler()),
        ("lr", LogisticRegression(C=1.0, max_iter=1000, random_state=42)),
    ])
    n_splits = min(5, int(y.sum()), int((y == 0).sum()))
    n_splits = max(2, n_splits)
    cv = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)
    cv_aucs = cross_val_score(pipe, X, y, cv=cv, scoring="roc_auc")
    cv_auc_mean = cv_aucs.mean()
    cv_auc_std  = cv_aucs.std()

    print(f"\n{n_splits}-fold CV AUC: {cv_auc_mean:.4f} ± {cv_auc_std:.4f}")
    print(f"Per-fold: {[round(a, 4) for a in cv_aucs]}")

    if cv_auc_mean < 0.55:
        print(f"\nWARNING: AUC {cv_auc_mean:.4f} < 0.55 target — using empirical weights instead of LR.")

    # Fit final model
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    final_lr = LogisticRegression(C=1.0, max_iter=1000, random_state=42)
    final_lr.fit(X_scaled, y)

    raw_coefs = final_lr.coef_[0]
    stds = scaler.scale_
    coefs_orig = raw_coefs / stds

    SCALE = 10.0
    signal_weights = {}
    for i, name in enumerate(FEATURE_NAMES):
        pts = max(-20.0, min(20.0, coefs_orig[i] * SCALE))
        signal_weights[name] = round(pts, 2)

    print("\nLR coefficients → score points:")
    for name, pts in sorted(signal_weights.items(), key=lambda x: -x[1]):
        print(f"  {name:20s}: {pts:+.2f}")

    # Empirical WR stats for key thresholds
    score_idx = FEATURE_NAMES.index("score_raw")
    high_score_mask  = X[:, score_idx] > 150
    low_score_mask   = X[:, score_idx] <= 150
    rsi_idx = FEATURE_NAMES.index("rsi_1h")
    high_rsi_mask    = X[:, rsi_idx] > 65
    cvd_idx = FEATURE_NAMES.index("cvd_kline")
    neg_cvd_mask     = X[:, cvd_idx] < -10
    ema_1h_idx = FEATURE_NAMES.index("ema_bull_1h")
    ema_bull_mask    = X[:, ema_1h_idx] == 1

    emp_stats = {
        "score>150":    high_score_mask,
        "score<=150":   low_score_mask,
        "rsi_1h>65":    high_rsi_mask,
        "cvd_kline<-10": neg_cvd_mask,
        "ema_bull_1h":  ema_bull_mask,
    }
    print("\nEmpirical WR stats (SHORT decisive 4h):")
    for name, mask in emp_stats.items():
        n_sig = mask.sum()
        if n_sig >= 5:
            wr = y[mask].mean()
            lift = wr - baseline_wr
            print(f"  {name:22s}: n={n_sig:4d}, WR={wr:.1%} ({lift:+.1%})")

    # Empirical binary weights: score > 150 is the key short signal to penalize.
    # Conservative scale: empirical WR lift × 0.8, capped ±15.
    # Target: score>150 SHORT gets negative correction (WR 33.8% vs 47.5% baseline = -13.7pp)
    wr_high = y[high_score_mask].mean() if high_score_mask.sum() > 0 else baseline_wr
    wr_low  = y[low_score_mask].mean()  if low_score_mask.sum()  > 0 else baseline_wr
    wr_rsi  = y[high_rsi_mask].mean()   if high_rsi_mask.sum()   > 0 else baseline_wr
    wr_cvd  = y[neg_cvd_mask].mean()    if neg_cvd_mask.sum()    > 0 else baseline_wr
    wr_ema  = y[ema_bull_mask].mean()   if ema_bull_mask.sum()   > 0 else baseline_wr

    def _empirical_pts(wr, baseline, scale=0.8, cap=15.0):
        lift_pp = (wr - baseline) * 100
        return max(-cap, min(cap, round(lift_pp * scale, 1)))

    binary_signal_weights = {
        # Key finding: high score anti-correlated with SHORT WR
        "score_gt150":    _empirical_pts(wr_high, baseline_wr),  # penalty for score>150
        # RSI >65 = overbought, better short entry
        "rsi_gt65":       _empirical_pts(wr_rsi, baseline_wr),
        # Negative CVD confirms short-side pressure
        "cvd_kline_bear": _empirical_pts(wr_cvd, baseline_wr),
        # Bull EMA alignment works against short direction
        "ema_bull_1h":    _empirical_pts(wr_ema, baseline_wr),
    }

    print(f"\nEmpirical binary weights (SHORT):")
    for name, pts in binary_signal_weights.items():
        print(f"  {name:22s}: {pts:+.1f}")
    print(f"\nAUC: {cv_auc_mean:.4f}")

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(binary_signal_weights, f, indent=2)
    print(f"Saved: {OUTPUT_JSON}")

    with open(OUTPUT_REPORT, "w", encoding="utf-8") as f:
        f.write("# SHORT LR Signal Weights — Model Report\n\n")
        f.write(f"**Dataset:** ШОРТ decisive trades (TP1/WIN=1, STOP/LOSS=0, FLAT excluded)  \n")
        f.write(f"**Samples:** {n} ({int(y.sum())} wins / {n - int(y.sum())} losses)  \n")
        f.write(f"**Baseline WR:** {baseline_wr:.1%}  \n")
        f.write(f"**{n_splits}-fold CV AUC:** {cv_auc_mean:.4f} ± {cv_auc_std:.4f}  \n\n")
        f.write("## Key Finding\n\n")
        f.write(f"Score >150 WR = **{wr_high:.1%}** vs {baseline_wr:.1%} baseline "
                f"({(wr_high - baseline_wr)*100:+.1f}pp). "
                "High score is anti-correlated with SHORT WR — over-rewarded by hand-tuned scorer.\n\n")
        f.write("## Binary Signal Weights\n\n")
        f.write("| Feature | WR | Lift | Score Pts |\n")
        f.write("|---------|-----|------|----------|\n")
        stat_map = {
            "score_gt150":    (wr_high, high_score_mask.sum()),
            "rsi_gt65":       (wr_rsi, high_rsi_mask.sum()),
            "cvd_kline_bear": (wr_cvd, neg_cvd_mask.sum()),
            "ema_bull_1h":    (wr_ema, ema_bull_mask.sum()),
        }
        for name, pts in binary_signal_weights.items():
            wr_s, n_s = stat_map.get(name, (0.0, 0))
            lift_s = (wr_s - baseline_wr) * 100
            f.write(f"| {name} | {wr_s:.1%} | {lift_s:+.1f}pp | {pts:+.1f} |\n")
    print(f"Report: {OUTPUT_REPORT}")


if __name__ == "__main__":
    main()

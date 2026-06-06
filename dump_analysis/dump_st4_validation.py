#!/usr/bin/env python3
"""
dump_st4_validation.py — AVEVA-67 / AVEC-63 ST4

Walk-forward OOS + Sensitivity Sweep + Regime Breakdown for dump prediction model.

Inputs:
  dump_analysis/feature_matrix.csv
  dump_analysis/dump_events.csv
  pump_analysis/klines_1h/BTCUSDT.csv.gz

Outputs:
  dump_analysis/walkforward_oos.csv
  dump_analysis/sensitivity_sweep_dump.csv
  dump_analysis/regime_analysis.csv
  dump_analysis/plots/walkforward_oos.png
  dump_analysis/plots/regime_breakdown.png
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
import pandas as pd
from scipy import stats as scipy_stats
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import precision_score, recall_score

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dump_st4")

BASE       = Path(__file__).parent
PUMP_DIR   = BASE.parent / "pump_analysis"
PLOTS_DIR  = BASE / "plots"
PLOTS_DIR.mkdir(exist_ok=True)

FEATURES = [
    "vol_ratio_1h",
    "vol_spike_count",
    "price_momentum_4h",
    "cvd_divergence",
    "upper_wick_ratio",
    "support_break",
    "vol_trend_slope",
    "hour_of_day",
    "oi_chg_4h",
    "funding_rate",
    "long_liq_ratio_4h",
]

# ── 1. LOAD DATA ──────────────────────────────────────────────────────────────

def load_feature_matrix() -> pd.DataFrame:
    df = pd.read_csv(BASE / "feature_matrix.csv")
    df["ts"] = pd.to_datetime(df["start_ms"], unit="ms", utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    # fill NA features with column median
    for col in FEATURES:
        if col in df.columns:
            df[col] = df[col].fillna(df[col].median())
    return df


def load_dump_events() -> pd.DataFrame:
    df = pd.read_csv(BASE / "dump_events.csv")
    df["start_ts"] = pd.to_datetime(df["start_ts"], utc=True)
    return df


def load_btc_klines() -> pd.DataFrame:
    path = PUMP_DIR / "klines_1h" / "BTCUSDT.csv.gz"
    with gzip.open(path, "rt") as f:
        df = pd.read_csv(f)
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    return df


# ── 2. BTC REGIME CALCULATION ─────────────────────────────────────────────────

def compute_btc_regime_monthly(btc: pd.DataFrame) -> pd.DataFrame:
    """30-day rolling returns to assign bull/sideways/bear regime per month-end."""
    btc = btc.set_index("ts").resample("1D")["close"].last().dropna().reset_index()
    btc["ret_30d"] = btc["close"].pct_change(30)
    btc["regime"] = btc["ret_30d"].apply(
        lambda r: 1 if r >= 0.10 else (-1 if r <= -0.10 else 0)
    )
    btc["month"] = btc["ts"].dt.to_period("M")
    # dominant regime per calendar month
    monthly = (
        btc.groupby("month")["regime"]
        .agg(lambda x: x.value_counts().idxmax())
        .reset_index()
    )
    monthly.columns = ["month", "btc_regime_calc"]
    return monthly


# ── 3. WALK-FORWARD OOS ───────────────────────────────────────────────────────

def run_walkforward(df: pd.DataFrame, min_train_samples: int = 100) -> pd.DataFrame:
    """
    Expanding-window walk-forward: train on all months before T, test on month T.
    Requires ≥10 OOS test periods with ≥1 positive class sample.
    """
    df = df.copy()
    df["month"] = df["ts"].dt.to_period("M")
    months = sorted(df["month"].unique())

    records = []
    for i, test_month in enumerate(months):
        train_df = df[df["month"] < test_month]
        test_df  = df[df["month"] == test_month]

        if len(train_df) < min_train_samples:
            continue
        if len(test_df) < 5:
            continue
        if test_df["label"].sum() == 0:
            continue  # no positives in test period — skip for precision/recall

        X_train = train_df[FEATURES].values
        y_train = train_df["label"].values
        X_test  = test_df[FEATURES].values
        y_test  = test_df["label"].values

        scaler = StandardScaler()
        X_train_s = scaler.fit_transform(X_train)
        X_test_s  = scaler.transform(X_test)

        model = LogisticRegression(max_iter=300, class_weight="balanced", C=0.5,
                                   random_state=42)
        try:
            model.fit(X_train_s, y_train)
        except Exception as e:
            log.warning("Fold %s fit failed: %s", test_month, e)
            continue

        y_prob = model.predict_proba(X_test_s)[:, 1]
        threshold = 0.5
        y_pred = (y_prob >= threshold).astype(int)

        n_pos = y_test.sum()
        base_rate = y_test.mean()

        tp = int(((y_pred == 1) & (y_test == 1)).sum())
        fp = int(((y_pred == 1) & (y_test == 0)).sum())
        fn = int(((y_pred == 0) & (y_test == 1)).sum())

        prec  = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        rec   = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        lift  = prec / base_rate if base_rate > 0 else 0.0

        # top-quartile lift
        q75 = np.percentile(y_prob, 75)
        top25_idx = y_prob >= q75
        top25_rate = y_test[top25_idx].mean() if top25_idx.sum() > 0 else 0.0
        top25_lift = top25_rate / base_rate if base_rate > 0 else 0.0

        records.append({
            "period":     str(test_month),
            "n_train":    len(train_df),
            "n_test":     len(test_df),
            "n_dumps":    int(n_pos),
            "base_rate":  round(base_rate, 4),
            "precision":  round(prec, 4),
            "recall":     round(rec, 4),
            "lift":       round(lift, 4),
            "top25_lift": round(top25_lift, 4),
            "tp":         tp,
            "fp":         fp,
            "fn":         fn,
        })

    result = pd.DataFrame(records)
    log.info("Walk-forward: %d OOS folds produced", len(result))
    return result


# ── 4. SENSITIVITY SWEEP ──────────────────────────────────────────────────────

def run_sensitivity_sweep(df: pd.DataFrame, dump_events: pd.DataFrame) -> pd.DataFrame:
    """
    Vary dump detection threshold (pct_chg) and time window.

    For each (threshold, window) combination:
      - threshold: how large a drop qualifies as a dump (10%, 12%, 15%, 20%)
      - window:    max hours for the drop to complete (1h, 4h, 8h, 24h)

    Uses actual dump duration from dump_events (end_ts - start_ts).
    Control windows are always included as negatives.

    Metrics: N_events, base_rate, vol_spike lift, logistic regression model lift (top-25%)
    """
    THRESHOLDS  = [10.0, 12.0, 15.0, 20.0]
    WINDOWS_H   = [1, 4, 8, 24]

    # Compute actual dump duration
    de = dump_events.copy()
    de["start_ts"] = pd.to_datetime(de["start_ts"], utc=True)
    de["end_ts"]   = pd.to_datetime(de["end_ts"], utc=True)
    de["duration_h"] = (de["end_ts"] - de["start_ts"]).dt.total_seconds() / 3600
    # dump_id → (pct_chg, duration_h, split)
    de_lookup = de.set_index("dump_id")[["pct_chg", "duration_h", "split"]]

    # feature_matrix dump_ids: window_id like 'd_0', 'd_1' → numeric id
    df = df.copy()
    df["dump_id_num"] = df["window_id"].str.extract(r"d_(\d+)").astype(float)

    # Build merged info for dump rows only
    dump_rows = df[df["label"] == 1].copy()
    de_merge  = de_lookup.rename(
        columns={"pct_chg": "pct_chg_orig", "duration_h": "dur_h", "split": "split_de"}
    )
    dump_rows = dump_rows.join(de_merge, on="dump_id_num")
    # For control rows, pct_chg_orig and dur_h are NaN

    ctrl_rows = df[df["label"] == 0].copy()

    records = []
    for thresh in THRESHOLDS:
        for win_h in WINDOWS_H:
            window_str = f"{win_h}h"

            # qualifying dump events: pct_chg >= thresh AND duration <= win_h
            qualifying_dump_ids = set(
                de.loc[
                    (de["pct_chg"] >= thresh) & (de["duration_h"] <= win_h), "dump_id"
                ].tolist()
            )

            n_events_total = len(qualifying_dump_ids)

            # subset: qualifying dumps + ALL control windows
            qdump = dump_rows[dump_rows["dump_id_num"].isin(qualifying_dump_ids)].copy()
            qdump["label_sw"] = 1
            ctrl  = ctrl_rows.copy()
            ctrl["label_sw"] = 0
            subset = pd.concat([qdump, ctrl], ignore_index=True)

            train_sub = subset[subset["split"] == "train"]
            test_sub  = subset[subset["split"] == "test"]

            n_train = int((train_sub["label_sw"] == 1).sum())
            n_test  = int((test_sub["label_sw"] == 1).sum())

            base_rate_test = test_sub["label_sw"].mean() if len(test_sub) > 0 else 0.0

            # vol_spike_count univariate lift (top-quartile in test)
            pos_vs  = test_sub.loc[test_sub["label_sw"] == 1, "vol_spike_count"]
            all_vs  = test_sub["vol_spike_count"]
            q75_vs  = all_vs.quantile(0.75) if len(all_vs) >= 4 else 1.0
            top_pos = (pos_vs >= q75_vs).mean() if len(pos_vs) > 0 else 0.0
            top_all = (all_vs >= q75_vs).mean() if len(all_vs) > 0 else 0.25
            vs_lift = round(top_pos / top_all, 3) if top_all > 0 else 0.0

            # logistic regression model lift (top-25% predicted probability)
            model_lift = None
            if (
                (train_sub["label_sw"] == 1).sum() >= 10 and
                (test_sub["label_sw"] == 1).sum() >= 3 and
                len(train_sub) >= 50
            ):
                try:
                    X_tr = train_sub[FEATURES].fillna(0).values
                    y_tr = train_sub["label_sw"].values
                    X_te = test_sub[FEATURES].fillna(0).values
                    y_te = test_sub["label_sw"].values
                    sc   = StandardScaler()
                    X_tr = sc.fit_transform(X_tr)
                    X_te = sc.transform(X_te)
                    m    = LogisticRegression(max_iter=300, class_weight="balanced",
                                              C=0.5, random_state=42)
                    m.fit(X_tr, y_tr)
                    prob   = m.predict_proba(X_te)[:, 1]
                    q75_p  = np.percentile(prob, 75)
                    top_rt = y_te[prob >= q75_p].mean()
                    base   = y_te.mean()
                    model_lift = round(top_rt / base, 3) if base > 0 else None
                except Exception as e:
                    log.debug("Sweep model error (%s, %s): %s", thresh, window_str, e)

            records.append({
                "threshold_pct":    thresh,
                "window_h":         win_h,
                "n_events_total":   n_events_total,
                "n_events_train":   n_train,
                "n_events_test":    n_test,
                "base_rate_test":   round(base_rate_test, 4),
                "vol_spike_lift":   vs_lift,
                "model_lift_top25": model_lift,
            })

    return pd.DataFrame(records)


# ── 5. REGIME BREAKDOWN ───────────────────────────────────────────────────────

def run_regime_analysis(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """
    Break down dump prediction quality by BTC market regime.

    Uses btc_regime column from feature_matrix:
      -1 = bear  (BTC -10%+ in 30d)
       0 = sideways
      +1 = bull   (BTC +10%+ in 30d)

    Key hypothesis: dumps are more predictable in bear regime.
    Credibility rule: HYPOTHESIS if N(bear dumps in test) < 20.
    """
    regime_names = {-1: "bear", 0: "sideways", 1: "bull"}
    records = []
    model_records = []

    train = df[df["split"] == "train"].copy()
    test  = df[df["split"] == "test"].copy()

    # global model trained on all train data
    X_tr_all = train[FEATURES].fillna(0).values
    y_tr_all = train["label"].values
    sc_all   = StandardScaler()
    X_tr_all = sc_all.fit_transform(X_tr_all)
    model_all = LogisticRegression(max_iter=300, class_weight="balanced",
                                   C=0.5, random_state=42)
    model_all.fit(X_tr_all, y_tr_all)

    X_te_all = sc_all.transform(test[FEATURES].fillna(0).values)
    prob_all  = model_all.predict_proba(X_te_all)[:, 1]

    test = test.copy()
    test["dump_prob"] = prob_all

    n_bear_dumps_test = int(
        ((test["btc_regime"] == -1) & (test["label"] == 1)).sum()
    )
    hypothesis_valid = n_bear_dumps_test >= 20

    for regime_code, regime_name in regime_names.items():
        # feature lifts vs global baseline
        feature_lifts = {}
        for feat in FEATURES:
            pos_vals  = df.loc[df["label"] == 1, feat].dropna()
            ctrl_vals = df.loc[df["label"] == 0, feat].dropna()
            reg_pos   = df.loc[(df["label"] == 1) & (df["btc_regime"] == regime_code), feat].dropna()
            reg_ctrl  = df.loc[(df["label"] == 0) & (df["btc_regime"] == regime_code), feat].dropna()

            global_median_pos  = pos_vals.median()
            global_median_ctrl = ctrl_vals.median()
            regime_median_pos  = reg_pos.median() if len(reg_pos) else np.nan

            lift_vs_ctrl = (
                (regime_median_pos / global_median_ctrl)
                if (global_median_ctrl != 0 and not np.isnan(regime_median_pos))
                else np.nan
            )
            feature_lifts[feat] = round(lift_vs_ctrl, 3) if not np.isnan(lift_vs_ctrl) else None

        # dump frequency
        reg_all   = df[df["btc_regime"] == regime_code]
        reg_train = train[train["btc_regime"] == regime_code]
        reg_test  = test[test["btc_regime"] == regime_code]

        n_total      = len(reg_all)
        n_dumps_all  = int(reg_all["label"].sum())
        n_dumps_test = int(reg_test["label"].sum())
        base_rate    = reg_test["label"].mean() if len(reg_test) > 0 else 0.0

        # model precision in this regime (test set)
        regime_test_probs = reg_test["dump_prob"].values
        regime_test_y     = reg_test["label"].values
        if len(regime_test_y) > 0 and regime_test_y.sum() > 0:
            q75_p = np.percentile(regime_test_probs, 75) if len(regime_test_probs) >= 4 else 0.5
            top25_mask = regime_test_probs >= q75_p
            prec_top25 = regime_test_y[top25_mask].mean() if top25_mask.sum() > 0 else 0.0
            lift_top25 = prec_top25 / base_rate if base_rate > 0 else 0.0
            y_pred = (regime_test_probs >= 0.5).astype(int)
            prec_thresh = precision_score(regime_test_y, y_pred, zero_division=0)
            rec_thresh  = recall_score(regime_test_y, y_pred, zero_division=0)
        else:
            prec_top25  = 0.0
            lift_top25  = 0.0
            prec_thresh = 0.0
            rec_thresh  = 0.0

        # credibility flag
        if regime_code == -1:
            status = "FACT" if hypothesis_valid else "HYPOTHESIS (N<20)"
        else:
            status = "FACT" if n_dumps_test >= 10 else "HYPOTHESIS (N<10)"

        row = {
            "regime":          regime_name,
            "regime_code":     regime_code,
            "n_windows_total": n_total,
            "n_dumps_total":   n_dumps_all,
            "n_dumps_test":    n_dumps_test,
            "base_rate_test":  round(base_rate, 4),
            "precision_05":    round(prec_thresh, 4),
            "recall_05":       round(rec_thresh, 4),
            "lift_top25":      round(lift_top25, 4),
            "credibility":     status,
        }
        row.update({f"lift_{k}": v for k, v in feature_lifts.items()})
        records.append(row)

    summary = {
        "n_bear_dumps_test":  n_bear_dumps_test,
        "hypothesis_valid":   hypothesis_valid,
        "hypothesis_status":  "FACT" if hypothesis_valid else "HYPOTHESIS — N(bear)=" + str(n_bear_dumps_test) + " < 20",
    }
    return pd.DataFrame(records), summary


# ── 6. PLOTS ──────────────────────────────────────────────────────────────────

def plot_walkforward(wf: pd.DataFrame) -> None:
    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
    fig.suptitle("Walk-Forward OOS — Dump Prediction (AVEC-63 ST4)", fontsize=13, fontweight="bold")

    x = range(len(wf))
    labels = wf["period"].tolist()

    # Panel 1: Precision vs lift
    ax = axes[0]
    ax.bar(x, wf["precision"], alpha=0.6, color="#2196F3", label="Precision @0.5")
    ax.bar(x, wf["lift"], alpha=0.0)  # just for scale
    ax2 = ax.twinx()
    ax2.plot(x, wf["lift"], "o-", color="#FF5722", linewidth=2, markersize=5, label="Lift @0.5")
    ax2.axhline(1.0, linestyle="--", color="gray", linewidth=0.8, alpha=0.6)
    ax.set_ylabel("Precision", color="#2196F3")
    ax2.set_ylabel("Lift", color="#FF5722")
    ax.set_ylim(0, 1.05)
    ax2.set_ylim(0, max(wf["lift"].max() * 1.2, 2.0))
    lines = ax.containers[0:1]
    ax.legend(loc="upper left", fontsize=8)
    ax2.legend(loc="upper right", fontsize=8)
    ax.set_title("Precision & Lift per OOS Period")

    # Panel 2: Recall + top25_lift
    ax = axes[1]
    ax.bar(x, wf["recall"], alpha=0.6, color="#4CAF50", label="Recall @0.5")
    ax2 = ax.twinx()
    ax2.plot(x, wf["top25_lift"], "s--", color="#9C27B0", linewidth=2,
             markersize=4, label="Top-25% Lift")
    ax2.axhline(1.0, linestyle="--", color="gray", linewidth=0.8, alpha=0.6)
    ax.set_ylabel("Recall", color="#4CAF50")
    ax2.set_ylabel("Top-25% Lift", color="#9C27B0")
    ax.set_ylim(0, 1.05)
    ax.legend(loc="upper left", fontsize=8)
    ax2.legend(loc="upper right", fontsize=8)
    ax.set_title("Recall & Top-25% Lift per OOS Period")

    # Panel 3: N dumps in test
    ax = axes[2]
    ax.bar(x, wf["n_dumps"], color="#FF9800", alpha=0.7, label="N dumps (test)")
    ax.set_ylabel("N Dumps")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=7)
    ax.set_title("Positive Sample Count per OOS Period")
    ax.legend(fontsize=8)

    plt.tight_layout()
    out = PLOTS_DIR / "walkforward_oos.png"
    plt.savefig(out, dpi=120, bbox_inches="tight")
    plt.close()
    log.info("Saved %s", out)


def plot_regime_breakdown(regime_df: pd.DataFrame, summary: dict) -> None:
    fig = plt.figure(figsize=(14, 9))
    fig.suptitle(
        f"Dump Prediction by BTC Regime — AVEC-63 ST4\n"
        f"Bear-regime hypothesis: {summary['hypothesis_status']}",
        fontsize=11, fontweight="bold"
    )

    gs = gridspec.GridSpec(2, 3, figure=fig, hspace=0.45, wspace=0.35)

    regime_order = ["bear", "sideways", "bull"]
    colors = {"bear": "#F44336", "sideways": "#FF9800", "bull": "#4CAF50"}
    rdf = regime_df.set_index("regime").loc[regime_order]

    # 1. Base rate by regime
    ax = fig.add_subplot(gs[0, 0])
    bars = ax.bar(regime_order, rdf["base_rate_test"],
                  color=[colors[r] for r in regime_order], alpha=0.8)
    ax.set_title("Base Rate (test set)", fontsize=9)
    ax.set_ylabel("Dump freq")
    for bar, val in zip(bars, rdf["base_rate_test"]):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.002,
                f"{val:.1%}", ha="center", va="bottom", fontsize=8)

    # 2. Model Precision @0.5 by regime
    ax = fig.add_subplot(gs[0, 1])
    bars = ax.bar(regime_order, rdf["precision_05"],
                  color=[colors[r] for r in regime_order], alpha=0.8)
    ax.set_title("Precision @0.5 (test set)", fontsize=9)
    ax.set_ylabel("Precision")
    ax.set_ylim(0, 1.0)
    for bar, val in zip(bars, rdf["precision_05"]):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.01,
                f"{val:.2f}", ha="center", va="bottom", fontsize=8)

    # 3. Top-25% Lift by regime
    ax = fig.add_subplot(gs[0, 2])
    bars = ax.bar(regime_order, rdf["lift_top25"],
                  color=[colors[r] for r in regime_order], alpha=0.8)
    ax.axhline(1.0, linestyle="--", color="gray", linewidth=0.8)
    ax.set_title("Top-25% Lift (test set)", fontsize=9)
    ax.set_ylabel("Lift")
    for bar, val in zip(bars, rdf["lift_top25"]):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.02,
                f"{val:.2f}×", ha="center", va="bottom", fontsize=8)

    # 4. N dumps by regime (test)
    ax = fig.add_subplot(gs[1, 0])
    bars = ax.bar(regime_order, rdf["n_dumps_test"],
                  color=[colors[r] for r in regime_order], alpha=0.8)
    ax.axhline(20, linestyle="--", color="red", linewidth=0.8, label="N=20 credibility")
    ax.set_title("N Dumps in Test Set", fontsize=9)
    ax.set_ylabel("N dumps")
    ax.legend(fontsize=7)
    for bar, val in zip(bars, rdf["n_dumps_test"]):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                str(int(val)), ha="center", va="bottom", fontsize=8)

    # 5. Feature lifts: vol_spike_count and vol_ratio_1h
    ax = fig.add_subplot(gs[1, 1])
    x_pos = np.arange(len(regime_order))
    w = 0.35
    vs_lifts = [rdf.loc[r, "lift_vol_spike_count"] or 0 for r in regime_order]
    vr_lifts = [rdf.loc[r, "lift_vol_ratio_1h"] or 0 for r in regime_order]
    ax.bar(x_pos - w/2, vs_lifts, w, color="#2196F3", alpha=0.8, label="vol_spike_count")
    ax.bar(x_pos + w/2, vr_lifts, w, color="#9C27B0", alpha=0.8, label="vol_ratio_1h")
    ax.axhline(1.0, linestyle="--", color="gray", linewidth=0.8)
    ax.set_xticks(x_pos)
    ax.set_xticklabels(regime_order)
    ax.set_title("Feature Lift vs Control (vol)", fontsize=9)
    ax.set_ylabel("Lift ratio")
    ax.legend(fontsize=7)

    # 6. Recall by regime
    ax = fig.add_subplot(gs[1, 2])
    bars = ax.bar(regime_order, rdf["recall_05"],
                  color=[colors[r] for r in regime_order], alpha=0.8)
    ax.set_title("Recall @0.5 (test set)", fontsize=9)
    ax.set_ylabel("Recall")
    ax.set_ylim(0, 1.0)
    for bar, val in zip(bars, rdf["recall_05"]):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.01,
                f"{val:.2f}", ha="center", va="bottom", fontsize=8)

    out = PLOTS_DIR / "regime_breakdown.png"
    plt.savefig(out, dpi=120, bbox_inches="tight")
    plt.close()
    log.info("Saved %s", out)


# ── 7. SENSITIVITY SWEEP SUMMARY PLOT ─────────────────────────────────────────

def plot_sensitivity(sweep: pd.DataFrame) -> None:
    """Heatmap of model_lift_top25 by threshold × window."""
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    fig.suptitle("Sensitivity Sweep — Dump Detection Parameters", fontsize=11,
                 fontweight="bold")

    windows    = [1, 4, 8, 24]
    thresholds = [10.0, 12.0, 15.0, 20.0]

    for ax_idx, metric in enumerate(["vol_spike_lift", "n_events_total"]):
        ax = axes[ax_idx]
        grid = np.zeros((len(thresholds), len(windows)))
        for i, t in enumerate(thresholds):
            for j, w in enumerate(windows):
                val = sweep.loc[
                    (sweep["threshold_pct"] == t) & (sweep["window_h"] == w), metric
                ].values
                grid[i, j] = float(val[0]) if len(val) > 0 and val[0] is not None else 0.0

        im = ax.imshow(grid, aspect="auto", cmap="YlOrRd")
        ax.set_xticks(range(len(windows)))
        ax.set_yticks(range(len(thresholds)))
        ax.set_xticklabels([f"{w}h" for w in windows])
        ax.set_yticklabels([f"{t:.0f}%" for t in thresholds])
        ax.set_xlabel("Time Window")
        ax.set_ylabel("Dump Threshold")
        ax.set_title(
            "Vol-Spike Lift" if metric == "vol_spike_lift" else "N Events"
        )
        plt.colorbar(im, ax=ax, shrink=0.8)
        for i in range(len(thresholds)):
            for j in range(len(windows)):
                txt = f"{grid[i,j]:.2f}" if metric == "vol_spike_lift" else f"{grid[i,j]:.0f}"
                ax.text(j, i, txt, ha="center", va="center", fontsize=9,
                        color="black" if grid[i,j] < grid.max() * 0.7 else "white")

    plt.tight_layout()
    out = PLOTS_DIR / "sensitivity_sweep_dump.png"
    plt.savefig(out, dpi=120, bbox_inches="tight")
    plt.close()
    log.info("Saved %s", out)


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("Loading data…")
    df           = load_feature_matrix()
    dump_events  = load_dump_events()

    # ── Part 1: Walk-Forward OOS ──────────────────────────────────────────────
    log.info("Running walk-forward OOS…")
    wf = run_walkforward(df)
    n_folds = len(wf)
    if n_folds < 10:
        log.warning("Only %d valid OOS folds; target is ≥10", n_folds)
    else:
        log.info("Walk-forward: %d OOS folds ✓", n_folds)

    wf.to_csv(BASE / "walkforward_oos.csv", index=False)
    log.info("Saved walkforward_oos.csv")
    plot_walkforward(wf)

    # ── Part 2: Sensitivity Sweep ─────────────────────────────────────────────
    log.info("Running sensitivity sweep…")
    sweep = run_sensitivity_sweep(df, dump_events)
    sweep.to_csv(BASE / "sensitivity_sweep_dump.csv", index=False)
    log.info("Saved sensitivity_sweep_dump.csv (%d rows)", len(sweep))
    plot_sensitivity(sweep)

    # ── Part 3: Regime Breakdown ──────────────────────────────────────────────
    log.info("Running regime analysis…")
    regime_df, summary = run_regime_analysis(df)
    regime_df.to_csv(BASE / "regime_analysis.csv", index=False)
    log.info("Saved regime_analysis.csv")
    log.info("Regime hypothesis status: %s", summary["hypothesis_status"])
    plot_regime_breakdown(regime_df, summary)

    # ── Final Report ──────────────────────────────────────────────────────────
    log.info("\n" + "="*60)
    log.info("AVEC-63 ST4 — RESULTS SUMMARY")
    log.info("="*60)
    log.info("Walk-Forward OOS (%d folds):", n_folds)
    if n_folds > 0:
        log.info("  Median precision:  %.3f", wf["precision"].median())
        log.info("  Median recall:     %.3f", wf["recall"].median())
        log.info("  Median lift:       %.3f", wf["lift"].median())
        log.info("  Median top25_lift: %.3f", wf["top25_lift"].median())
        folds_above_1 = (wf["lift"] > 1.0).sum()
        log.info("  Folds with lift>1: %d / %d", folds_above_1, n_folds)

    log.info("\nRegime Analysis:")
    for _, row in regime_df.iterrows():
        log.info("  %8s: prec=%.3f  lift=%.3f  N_test=%d  [%s]",
                 row["regime"], row["precision_05"], row["lift_top25"],
                 row["n_dumps_test"], row["credibility"])
    log.info("  Bear dumps in test: %d → %s",
             summary["n_bear_dumps_test"], summary["hypothesis_status"])

    log.info("\nSensitivity Sweep (vol_spike_lift at canonical 15%/24h):")
    canonical = sweep[
        (sweep["threshold_pct"] == 15.0) & (sweep["window_h"] == 24)
    ]
    if len(canonical) > 0:
        row = canonical.iloc[0]
        log.info("  N_events=%d  base_rate=%.3f  lift=%.3f",
                 row["n_events_total"], row["base_rate_test"], row["vol_spike_lift"])

    log.info("="*60)
    log.info("All outputs written to dump_analysis/")


if __name__ == "__main__":
    main()

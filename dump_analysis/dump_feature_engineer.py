#!/usr/bin/env python3
"""
dump_feature_engineer.py — AVEVA-65 ST2: Feature engineering for dump precursors.

Mirrors pump_analysis/feature_engineer.py for dump events.
All features use ONLY data strictly in [-4h, 0) before dump start — no lookahead.

Feature window: [-4h, 0) relative to start_ms.

Inputs (from dump_analysis/):
  dump_events.csv, control_windows.csv

Shared data (from pump_analysis/):
  klines_1h/{symbol}.csv.gz, funding.csv, open_interest.csv, liquidations_summary.csv

Outputs (dump_analysis/):
  feature_matrix.csv      — N×28 col: 12 features + metadata
  feature_stats.csv       — lift, p-value, medians: dump vs control
  combo_stats.csv         — precision/recall/F1 for 2-feature combos
  walkforward_results.csv — walk-forward OOS by period
  sensitivity_sweep.csv   — by dump type: standard/fast
  plots/feature_lift_bar.png
  plots/walkforward_precision.png
"""

import gzip
import logging
from itertools import combinations
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dump_feature_engineer")

OUT_DIR    = Path(__file__).parent
PUMP_DIR   = OUT_DIR.parent / "pump_analysis"
KLINES_DIR = PUMP_DIR / "klines_1h"
PLOTS_DIR  = OUT_DIR / "plots"
PLOTS_DIR.mkdir(exist_ok=True)

H1_MS  = 3_600_000
H4_MS  = 4 * H1_MS
H12_MS = 12 * H1_MS
H20    = 20       # bars for vol median
H30D   = 30 * 24  # bars for long-window median


def _parse_ts_ms(ts_series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(ts_series, utc=True)
    return (parsed.values.astype("int64") // 1_000).astype("int64")


# ── data loaders ──────────────────────────────────────────────────────────────

def load_windows() -> tuple[pd.DataFrame, pd.DataFrame]:
    dumps = pd.read_csv(OUT_DIR / "dump_events.csv")
    dumps["start_ms"] = _parse_ts_ms(dumps["start_ts"])
    dumps["label"] = 1
    ctrls = pd.read_csv(OUT_DIR / "control_windows.csv")
    ctrls["start_ms"] = _parse_ts_ms(ctrls["start_ts"])
    ctrls["label"] = 0
    return dumps, ctrls


def load_klines(symbol: str) -> pd.DataFrame | None:
    path = KLINES_DIR / f"{symbol}.csv.gz"
    if not path.exists():
        return None
    with gzip.open(path, "rt") as f:
        df = pd.read_csv(f)
    df = df.rename(columns={"open_time": "ts"})
    df["ts"] = df["ts"].astype("int64")
    for col in ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_quote"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values("ts").reset_index(drop=True)
    # CVD (taker buy - taker sell quote volume)
    df["cvd"] = (
        2 * df.get("taker_buy_quote", pd.Series(dtype=float))
        - df.get("quote_volume", pd.Series(dtype=float))
    )
    # 20h rolling median volume (shift 1 to avoid lookahead)
    df["vol_med_20h"] = df["volume"].rolling(H20, min_periods=8).median().shift(1)
    # 30d rolling median for context (shift 1)
    df["vol_med_30d"] = df["volume"].rolling(H30D, min_periods=7 * 24).median().shift(1)
    return df


def load_oi() -> dict[str, pd.DataFrame]:
    path = PUMP_DIR / "open_interest.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    df["timestamp"] = df["timestamp"].astype("int64")
    df["openInterest"] = pd.to_numeric(df["openInterest"], errors="coerce")
    result: dict[str, pd.DataFrame] = {}
    for sym, grp in df.groupby("symbol"):
        result[sym] = grp.sort_values("timestamp").reset_index(drop=True)
    return result


def load_funding() -> dict[str, pd.DataFrame]:
    path = PUMP_DIR / "funding.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    df["fundingTime"] = df["fundingTime"].astype("int64")
    df["fundingRate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    result: dict[str, pd.DataFrame] = {}
    for sym, grp in df.groupby("symbol"):
        result[sym] = grp.sort_values("fundingTime").reset_index(drop=True)
    return result


def load_liquidations() -> dict[str, pd.DataFrame]:
    path = PUMP_DIR / "liquidations_summary.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    df["ts"] = df["ts"].astype("int64")
    df["usd"] = pd.to_numeric(df["usd"], errors="coerce").fillna(0)
    result: dict[str, pd.DataFrame] = {}
    for sym, grp in df.groupby("symbol"):
        result[sym] = grp.sort_values("ts").reset_index(drop=True)
    return result


# ── feature lookup helpers ────────────────────────────────────────────────────

def _searchsorted_le(arr: np.ndarray, val: int) -> int:
    """Index of largest element <= val, or -1 if none."""
    return int(np.searchsorted(arr, val, side="right") - 1)


# ── feature extractors ────────────────────────────────────────────────────────

def klines_features(kl: pd.DataFrame, start_ms: int) -> dict:
    """
    Compute 9 klines-based features from window [-12h, 0) before start_ms.
    Returns {} if insufficient data.
    """
    ts_arr = kl["ts"].values

    # Last bar BEFORE start_ms: open_time < start_ms
    pos = _searchsorted_le(ts_arr, start_ms - 1)
    if pos < 1:
        return {}

    row_last = kl.iloc[pos]

    # ── vol_ratio_1h: last 1h volume / 20h median ──────────────────────────
    vol_med = float(row_last["vol_med_20h"]) if not np.isnan(row_last["vol_med_20h"]) else None
    vol_last = float(row_last["volume"])
    vol_ratio_1h = vol_last / vol_med if (vol_med and vol_med > 0) else np.nan

    # ── vol_spike_count: bars with vol >= 2x median in [-4h, 0) ───────────
    pos_4h = max(0, pos - 3)  # 4 bars inclusive [pos-3 .. pos]
    bars_4h = kl.iloc[pos_4h : pos + 1]
    vol_spike_count = int(
        (bars_4h["volume"] > 2 * vol_med).sum()
    ) if (vol_med and vol_med > 0) else 0

    # ── price_momentum_4h: % price change over [-4h, 0) ───────────────────
    open_4h = float(kl.iloc[pos_4h]["open"])
    close_now = float(row_last["close"])
    price_momentum_4h = (close_now - open_4h) / open_4h * 100 if open_4h > 0 else np.nan

    # ── cvd_divergence: bearish divergence = price up but CVD down (or vice versa signal) ──
    # We look at 4h: if price went UP but CVD went DOWN → distribution (bearish divergence = 1)
    cvd_start = float(kl.iloc[pos_4h]["cvd"]) if "cvd" in kl.columns else 0.0
    cvd_end   = float(row_last["cvd"]) if "cvd" in kl.columns else 0.0
    price_up  = close_now > open_4h
    cvd_up    = (cvd_end - cvd_start) > 0
    # Bearish divergence: price up, CVD down → classic distribution / smart money exit
    cvd_divergence = int(price_up and not cvd_up)

    # ── upper_wick_ratio: (high - max(open,close)) / (high - low) ─────────
    high  = float(row_last["high"]) if "high" in row_last else np.nan
    low   = float(row_last["low"])  if "low"  in row_last else np.nan
    open_ = float(row_last["open"])
    body_top = max(open_, close_now)
    candle_range = high - low if (not np.isnan(high) and not np.isnan(low) and high > low) else np.nan
    upper_wick_ratio = (high - body_top) / candle_range if candle_range and candle_range > 0 else np.nan

    # ── support_break: did price close below previous 3-bar low in [-1h, -0h]? ──
    pos_prev = max(0, pos - 3)
    prev_bars = kl.iloc[pos_prev:pos]  # excludes current bar
    prev_low = float(prev_bars["low"].min()) if (not prev_bars.empty and "low" in prev_bars.columns) else np.nan
    support_break = int(close_now < prev_low) if not np.isnan(prev_low) else 0

    # ── vol_trend_slope: linear slope of volume over last 12h ─────────────
    pos_12h = max(0, pos - 11)
    bars_12h = kl.iloc[pos_12h : pos + 1]
    vol_trend_slope = np.nan
    if len(bars_12h) >= 4:
        y = bars_12h["volume"].values.astype(float)
        valid_mask = ~np.isnan(y)
        if valid_mask.sum() >= 4:
            x = np.arange(len(y))[valid_mask]
            y_v = y[valid_mask]
            med_vol = np.median(y_v) if np.median(y_v) > 0 else 1.0
            slope, *_ = np.polyfit(x, y_v, 1)
            vol_trend_slope = float(slope / med_vol)  # normalised slope

    # ── hour_of_day ────────────────────────────────────────────────────────
    hour_of_day = int((start_ms // H1_MS) % 24)

    return {
        "vol_ratio_1h":      vol_ratio_1h,
        "vol_spike_count":   vol_spike_count,
        "price_momentum_4h": price_momentum_4h,
        "cvd_divergence":    cvd_divergence,
        "upper_wick_ratio":  upper_wick_ratio,
        "support_break":     support_break,
        "vol_trend_slope":   vol_trend_slope,
        "hour_of_day":       hour_of_day,
    }


def oi_features(oi_sym: pd.DataFrame, start_ms: int) -> dict:
    """oi_chg_4h: fractional change in OI over [-4h, 0)."""
    ts_arr = oi_sym["timestamp"].values
    oi_arr = oi_sym["openInterest"].values

    pos = _searchsorted_le(ts_arr, start_ms)
    if pos < 4:
        return {}

    oi_now = float(oi_arr[pos])
    oi_4h  = float(oi_arr[max(0, pos - 4)])
    oi_chg_4h = (oi_now - oi_4h) / oi_4h if oi_4h > 0 else np.nan
    return {"oi_chg_4h": oi_chg_4h}


def funding_features(fund_sym: pd.DataFrame, start_ms: int) -> dict:
    """funding_rate: latest funding rate before start_ms (8h interval)."""
    ts_arr   = fund_sym["fundingTime"].values
    rate_arr = fund_sym["fundingRate"].values

    pos = _searchsorted_le(ts_arr, start_ms)
    if pos < 0:
        return {}
    return {"funding_rate": float(rate_arr[pos])}


def liq_features(liq_sym: pd.DataFrame | None, start_ms: int) -> dict:
    """long_liq_ratio_4h: long liquidations / total over [-4h, 0)."""
    if liq_sym is None or liq_sym.empty:
        return {"long_liq_ratio_4h": 0.5}

    lo = start_ms - H4_MS
    mask = (liq_sym["ts"] >= lo) & (liq_sym["ts"] < start_ms)
    w = liq_sym[mask]
    if w.empty:
        return {"long_liq_ratio_4h": 0.5}

    long_usd  = float(w[w["side"] == "long_liq"]["usd"].sum())
    short_usd = float(w[w["side"] == "short_liq"]["usd"].sum())
    total     = long_usd + short_usd
    ratio     = long_usd / total if total > 0 else 0.5
    return {"long_liq_ratio_4h": ratio}


def btc_regime(btc_kl: pd.DataFrame | None, start_ms: int) -> dict:
    """
    btc_regime: BTC trend at dump time.
    Returns numeric encoding: bull=1, bear=-1, sideways=0.
    Uses 24h return threshold ±1%.
    """
    if btc_kl is None:
        return {"btc_regime": 0}

    ts_arr = btc_kl["ts"].values
    pos = _searchsorted_le(ts_arr, start_ms - 1)
    if pos < 24:
        return {"btc_regime": 0}

    close_now  = float(btc_kl["close"].iat[pos])
    close_24h  = float(btc_kl["close"].iat[pos - 24])
    chg_24h    = (close_now - close_24h) / close_24h * 100 if close_24h > 0 else 0.0

    if chg_24h > 1.0:
        regime = 1    # bull
    elif chg_24h < -1.0:
        regime = -1   # bear
    else:
        regime = 0    # sideways

    return {"btc_regime": regime}


# ── feature matrix builder ────────────────────────────────────────────────────

FEATURE_COLS = [
    "vol_ratio_1h",
    "vol_spike_count",
    "oi_chg_4h",
    "funding_rate",
    "long_liq_ratio_4h",
    "cvd_divergence",
    "price_momentum_4h",
    "btc_regime",
    "hour_of_day",
    "vol_trend_slope",
    "upper_wick_ratio",
    "support_break",
]


def build_feature_matrix(dumps: pd.DataFrame, controls: pd.DataFrame) -> pd.DataFrame:
    log.info("Loading OI, funding, liquidations ...")
    oi_all      = load_oi()
    funding_all = load_funding()
    liq_all     = load_liquidations()

    btc_kl = load_klines("BTCUSDT")

    dump_rows = dumps[["dump_id", "symbol", "start_ms", "split", "dump_type", "pct_chg"]].copy()
    dump_rows["label"]     = 1
    dump_rows["window_id"] = "d_" + dump_rows["dump_id"].astype(str)

    ctrl_rows = controls[["symbol", "start_ms", "split"]].copy()
    ctrl_rows["label"]     = 0
    ctrl_rows["dump_id"]   = np.nan
    ctrl_rows["dump_type"] = "control"
    ctrl_rows["pct_chg"]   = np.nan
    ctrl_rows["window_id"] = "c_" + ctrl_rows.index.astype(str)

    windows = pd.concat([dump_rows, ctrl_rows], ignore_index=True)
    symbols = windows["symbol"].unique()
    log.info("Computing features for %d windows across %d symbols ...", len(windows), len(symbols))

    all_rows = []
    for sym in sorted(symbols):
        kl       = load_klines(sym)
        oi_sym   = oi_all.get(sym)
        fund_sym = funding_all.get(sym)
        liq_sym  = liq_all.get(sym)

        for _, w in windows[windows["symbol"] == sym].iterrows():
            start_ms = int(w["start_ms"])
            row: dict = {
                "window_id": w["window_id"],
                "symbol":    sym,
                "start_ms":  start_ms,
                "label":     int(w["label"]),
                "split":     w["split"],
                "dump_type": w["dump_type"],
                "pct_chg":   w.get("pct_chg", np.nan),
            }

            if kl is not None:
                row.update(klines_features(kl, start_ms))
            if oi_sym is not None:
                row.update(oi_features(oi_sym, start_ms))
            if fund_sym is not None:
                row.update(funding_features(fund_sym, start_ms))
            row.update(liq_features(liq_sym, start_ms))
            row.update(btc_regime(btc_kl, start_ms))

            for col in FEATURE_COLS:
                row.setdefault(col, np.nan)
            all_rows.append(row)

    df = pd.DataFrame(all_rows)
    log.info("Feature matrix: %d rows × %d cols", len(df), len(df.columns))
    return df


# ── statistical analysis ──────────────────────────────────────────────────────

def _lift(feat_vals: pd.Series, labels: pd.Series, threshold: float, base_rate: float) -> float:
    above = feat_vals > threshold
    if above.sum() < 5:
        return np.nan
    return labels[above].mean() / base_rate if base_rate > 0 else np.nan


def _bootstrap_lift_ci(
    feat: np.ndarray, labels: np.ndarray, threshold: float,
    base_rate: float, n_boot: int = 1000
) -> tuple[float, float]:
    rng = np.random.default_rng(42)
    lifts = []
    n = len(feat)
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        above = feat[idx] > threshold
        if above.sum() < 5:
            continue
        r = labels[idx][above].mean()
        lifts.append(r / base_rate if base_rate > 0 else np.nan)
    if len(lifts) < 100:
        return np.nan, np.nan
    return float(np.percentile(lifts, 2.5)), float(np.percentile(lifts, 97.5))


def feature_stats(fmat: pd.DataFrame) -> pd.DataFrame:
    train = fmat[fmat["split"] == "train"].copy()
    base_rate = train["label"].mean()
    log.info("Train base_rate: %.4f (%d dumps / %d total)",
             base_rate, train["label"].sum(), len(train))

    rows = []
    for feat in FEATURE_COLS:
        col = train[feat].dropna()
        if len(col) < 20:
            rows.append({"feature": feat, "note": "insufficient_data (<20 non-null)"})
            continue

        aligned   = train.dropna(subset=[feat])
        dump_vals = aligned.loc[aligned["label"] == 1, feat].values
        ctrl_vals = aligned.loc[aligned["label"] == 0, feat].values

        if len(dump_vals) < 5 or len(ctrl_vals) < 5:
            rows.append({"feature": feat, "note": "insufficient_dump_or_ctrl_data"})
            continue

        median_dump = float(np.median(dump_vals))
        median_ctrl = float(np.median(ctrl_vals))

        _, p_val = scipy_stats.mannwhitneyu(dump_vals, ctrl_vals, alternative="two-sided")
        p_val = float(p_val)

        threshold = float(np.median(ctrl_vals))
        lift = _lift(aligned[feat], aligned["label"], threshold, base_rate)
        ci_lo, ci_hi = _bootstrap_lift_ci(
            aligned[feat].values, aligned["label"].values, threshold, base_rate
        )

        rows.append({
            "feature":     feat,
            "median_dump": round(median_dump, 6),
            "median_ctrl": round(median_ctrl, 6),
            "n_dump":      len(dump_vals),
            "n_ctrl":      len(ctrl_vals),
            "p_value":     round(p_val, 6),
            "lift":        round(lift, 4) if not np.isnan(lift) else None,
            "lift_ci_lo":  round(ci_lo, 4) if not np.isnan(ci_lo) else None,
            "lift_ci_hi":  round(ci_hi, 4) if not np.isnan(ci_hi) else None,
            "note": (
                "significant"      if p_val <= 0.05 and not np.isnan(lift) and lift and lift >= 1.2
                else "not_significant" if p_val > 0.05
                else "significant_low_lift"
            ),
        })

    return pd.DataFrame(rows).sort_values("lift", ascending=False, na_position="last")


def combo_stats(fmat: pd.DataFrame, top_features: list[str], n: int = 2) -> pd.DataFrame:
    train = fmat[fmat["split"] == "train"].dropna(subset=top_features)
    base_rate = train["label"].mean()
    rows = []
    for combo in combinations(top_features, n):
        subset = train.copy()
        masks = []
        for f in combo:
            thresh = subset.loc[subset["label"] == 0, f].median()
            masks.append(subset[f] > thresh)
        cond = masks[0]
        for m in masks[1:]:
            cond = cond & m
        flagged = subset[cond]
        if len(flagged) < 5:
            continue
        tp  = int(flagged["label"].sum())
        fp  = int((flagged["label"] == 0).sum())
        fn  = int(((~cond) & (subset["label"] == 1)).sum())
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0
        rec  = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1   = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0
        rows.append({
            "features":  "+".join(combo),
            "n_flagged": len(flagged),
            "tp": tp, "fp": fp, "fn": fn,
            "precision": round(prec, 4),
            "recall":    round(rec, 4),
            "f1":        round(f1, 4),
            "lift":      round(prec / base_rate if base_rate > 0 else np.nan, 4),
        })
    return pd.DataFrame(rows).sort_values("f1", ascending=False)


def walkforward(fmat: pd.DataFrame, top_feature: str, step_days: int = 30) -> pd.DataFrame:
    if top_feature not in fmat.columns:
        return pd.DataFrame()
    df = fmat.dropna(subset=[top_feature]).copy()
    df["date"] = pd.to_datetime(df["start_ms"], unit="ms", utc=True)

    start_date = df["date"].min()
    end_date   = df["date"].max()
    step       = pd.Timedelta(days=step_days)

    rows = []
    train_end = start_date + step * 6
    while train_end + step <= end_date:
        test_start = train_end
        test_end   = train_end + step

        train_df = df[df["date"] < train_end]
        test_df  = df[(df["date"] >= test_start) & (df["date"] < test_end)]

        if len(train_df) < 50 or len(test_df) < 5:
            train_end += step
            continue

        base_rate = train_df["label"].mean()
        pump_med  = train_df.loc[train_df["label"] == 1, top_feature].median()
        ctrl_med  = train_df.loc[train_df["label"] == 0, top_feature].median()
        threshold = (pump_med + ctrl_med) / 2

        flagged = test_df[test_df[top_feature] > threshold]
        if len(flagged) < 2:
            train_end += step
            continue

        tp   = int(flagged["label"].sum())
        fp   = int((flagged["label"] == 0).sum())
        fn   = int(((test_df[top_feature] <= threshold) & (test_df["label"] == 1)).sum())
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0
        rec  = tp / (tp + fn) if (tp + fn) > 0 else 0
        rows.append({
            "period_start": str(test_start.date()),
            "period_end":   str(test_end.date()),
            "threshold":    round(threshold, 4),
            "n_train":      len(train_df),
            "n_test":       len(test_df),
            "tp": tp, "fp": fp, "fn": fn,
            "precision": round(prec, 4),
            "recall":    round(rec, 4),
            "lift":      round(prec / base_rate if base_rate > 0 else np.nan, 4),
        })
        train_end += step

    return pd.DataFrame(rows)


def sensitivity_sweep(fmat: pd.DataFrame, top_features: list[str]) -> pd.DataFrame:
    """Lift of top features per dump_type (standard / fast)."""
    train = fmat[fmat["split"] == "train"].copy()
    base_rate = train["label"].mean()

    feat_thresholds: dict[str, float | None] = {}
    for feat in top_features[:5]:
        if feat in train.columns:
            v = train.loc[train["label"] == 0, feat].median()
            feat_thresholds[feat] = float(v) if not np.isnan(v) else None

    rows = []
    for dump_type in fmat["dump_type"].dropna().unique():
        if dump_type == "control":
            continue
        sub = fmat[((fmat["label"] == 1) & (fmat["dump_type"] == dump_type)) | (fmat["label"] == 0)]
        br  = sub["label"].mean()
        row: dict = {"dump_type": dump_type, "base_rate": round(float(br), 4)}
        for feat, thresh in feat_thresholds.items():
            if thresh is None:
                continue
            col   = sub.dropna(subset=[feat])
            above = col[col[feat] > thresh]
            lift  = above["label"].mean() / br if (br > 0 and len(above) > 0) else np.nan
            row[f"lift_{feat}"] = round(float(lift), 4) if not np.isnan(lift) else None
        rows.append(row)

    return pd.DataFrame(rows)


# ── plots ─────────────────────────────────────────────────────────────────────

def plot_feature_lifts(stats_df: pd.DataFrame) -> None:
    valid = stats_df.dropna(subset=["lift"]).head(10)
    if valid.empty:
        return
    fig, ax = plt.subplots(figsize=(12, 5))
    colors = [
        "#e74c3c" if (row.get("p_value", 1.0) <= 0.05) else "#95a5a6"
        for _, row in valid.iterrows()
    ]
    ax.barh(valid["feature"], valid["lift"].astype(float), color=colors)
    if "lift_ci_lo" in valid.columns and "lift_ci_hi" in valid.columns:
        xerr_lo = (valid["lift"] - valid["lift_ci_lo"]).clip(lower=0)
        xerr_hi = (valid["lift_ci_hi"] - valid["lift"]).clip(lower=0)
        ax.errorbar(
            valid["lift"].astype(float), range(len(valid)),
            xerr=[xerr_lo.values, xerr_hi.values],
            fmt="none", color="black", capsize=4,
        )
    ax.axvline(1.0, color="gray", linestyle="--", linewidth=1)
    ax.set_xlabel("Lift (vs base rate)")
    ax.set_title("Dump Precursors — Top-10 Features by Lift (train split)\nRed = p≤0.05")
    ax.invert_yaxis()
    plt.tight_layout()
    plt.savefig(PLOTS_DIR / "feature_lift_bar.png", dpi=150, bbox_inches="tight")
    plt.close()
    log.info("Plot saved → plots/feature_lift_bar.png")


def plot_walkforward(wf_df: pd.DataFrame) -> None:
    if wf_df.empty:
        return
    fig, ax = plt.subplots(figsize=(12, 4))
    x = range(len(wf_df))
    ax.plot(x, wf_df["precision"], marker="o", label="Precision", color="#e74c3c")
    ax.plot(x, wf_df["recall"],    marker="s", label="Recall",    color="#3498db")
    ax.set_xticks(list(x))
    ax.set_xticklabels(wf_df["period_start"].str[:7], rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Metric")
    ax.set_title("Walk-forward Validation — Dump Precursor Precision & Recall")
    ax.legend()
    ax.set_ylim(0, 1)
    plt.tight_layout()
    plt.savefig(PLOTS_DIR / "walkforward_precision.png", dpi=150, bbox_inches="tight")
    plt.close()
    log.info("Plot saved → plots/walkforward_precision.png")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=== AVEVA-65 ST2: Dump feature engineering ===")

    dumps, ctrls = load_windows()
    log.info("Loaded: %d dump events, %d control windows", len(dumps), len(ctrls))

    fmat = build_feature_matrix(dumps, ctrls)
    fmat.to_csv(OUT_DIR / "feature_matrix.csv", index=False)
    log.info("feature_matrix.csv → %d rows", len(fmat))

    log.info("Statistical analysis (train only) ...")
    stats_df = feature_stats(fmat)
    stats_df.to_csv(OUT_DIR / "feature_stats.csv", index=False)
    log.info("feature_stats.csv → %d rows", len(stats_df))

    valid_stats = stats_df.dropna(subset=["lift"])
    top_feats   = valid_stats.head(10)["feature"].tolist()
    log.info("Top features: %s", top_feats)

    if len(top_feats) >= 2:
        log.info("Computing combo stats ...")
        combo_df = combo_stats(fmat, top_feats[:6], n=2)
        combo_df.to_csv(OUT_DIR / "combo_stats.csv", index=False)
        log.info("combo_stats.csv → %d rows", len(combo_df))
    else:
        pd.DataFrame().to_csv(OUT_DIR / "combo_stats.csv", index=False)

    top1 = top_feats[0] if top_feats else None
    if top1:
        log.info("Walk-forward validation on feature: %s", top1)
        wf_df = walkforward(fmat, top1)
        wf_df.to_csv(OUT_DIR / "walkforward_results.csv", index=False)
        log.info("walkforward_results.csv → %d rows", len(wf_df))
    else:
        pd.DataFrame().to_csv(OUT_DIR / "walkforward_results.csv", index=False)
        wf_df = pd.DataFrame()

    log.info("Sensitivity sweep by dump_type ...")
    sweep_df = sensitivity_sweep(fmat, top_feats[:5])
    sweep_df.to_csv(OUT_DIR / "sensitivity_sweep.csv", index=False)
    log.info("sensitivity_sweep.csv → %d rows", len(sweep_df))

    plot_feature_lifts(stats_df)
    if top1:
        plot_walkforward(wf_df)

    # ── summary ────────────────────────────────────────────────────────────
    log.info("\n=== Dump Feature Analysis Summary ===")
    train_mask = fmat["split"] == "train"
    test_mask  = fmat["split"] == "test"
    log.info("  Train base_rate: %.4f", fmat[train_mask]["label"].mean())
    if test_mask.any():
        log.info("  Test  base_rate: %.4f", fmat[test_mask]["label"].mean())
    sig = stats_df[stats_df.get("note", pd.Series()).eq("significant")] if "note" in stats_df.columns else pd.DataFrame()
    log.info("  Significant features (p≤0.05, lift≥1.2): %d / %d", len(sig), len(stats_df))
    for _, row in stats_df.dropna(subset=["lift"]).head(5).iterrows():
        log.info("    %-22s lift=%.3f p=%.4f",
                 row["feature"], row.get("lift", float("nan")), row.get("p_value", float("nan")))


if __name__ == "__main__":
    main()

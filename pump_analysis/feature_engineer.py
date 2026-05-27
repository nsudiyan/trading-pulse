#!/usr/bin/env python3
"""
feature_engineer.py — AVEVA-60 ST3: Feature engineering + statistical analysis.

Computes 16 predictive features for every pump and control window from ST2.
All features use ONLY data strictly before the window start (no lookahead).

Feature window: [-4h, 0) relative to start_ms.

Outputs (pump_analysis/):
  feature_matrix.csv
  feature_stats.csv
  combo_stats.csv
  walkforward_results.csv
  sensitivity_sweep.csv
  plots/feature_lift_bar.png
  plots/walkforward_precision.png
"""

import gzip
import json
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
log = logging.getLogger("feature_engineer")

OUT_DIR    = Path(__file__).parent
KLINES_DIR = OUT_DIR / "klines_1h"
PLOTS_DIR  = OUT_DIR / "plots"
PLOTS_DIR.mkdir(exist_ok=True)

H1_MS  = 3_600_000
H4_MS  = 4 * H1_MS
H7D_MS = 7 * 24 * H1_MS
H30D   = 30 * 24  # bars


def _to_ms(s) -> int:
    return int(pd.Timestamp(str(s), tz="UTC" if "+" not in str(s) and "Z" not in str(s) else None).value // 1_000_000)


# ── data loaders ──────────────────────────────────────────────────────────────

def _parse_ts_ms(ts_series: pd.Series) -> pd.Series:
    """Parse datetime strings → Unix milliseconds (int64)."""
    parsed = pd.to_datetime(ts_series, utc=True)
    # pandas DatetimeArray.astype("int64") yields microseconds, not nanoseconds
    return (parsed.values.astype("int64") // 1_000).astype("int64")


def load_windows() -> tuple[pd.DataFrame, pd.DataFrame]:
    pumps = pd.read_csv(OUT_DIR / "pump_events.csv")
    pumps["start_ms"] = _parse_ts_ms(pumps["start_ts"])
    pumps["label"] = 1
    ctrls = pd.read_csv(OUT_DIR / "control_windows.csv")
    ctrls["start_ms"] = _parse_ts_ms(ctrls["start_ts"])
    ctrls["label"] = 0
    return pumps, ctrls


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
    # CVD
    df["cvd"] = 2 * df.get("taker_buy_quote", pd.Series(dtype=float)) - df.get("quote_volume", pd.Series(dtype=float))
    df["cvd_div"] = (df["cvd"] / df["quote_volume"].replace(0, np.nan)).fillna(0)
    # 30d rolling median volume for vol_ratio
    df["vol_med_30d"] = df["volume"].rolling(H30D, min_periods=7*24).median().shift(1)
    return df


def load_oi() -> dict[str, pd.DataFrame]:
    path = OUT_DIR / "open_interest.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    df["timestamp"] = df["timestamp"].astype("int64")
    df["openInterest"] = pd.to_numeric(df["openInterest"], errors="coerce")
    result = {}
    for sym, grp in df.groupby("symbol"):
        result[sym] = grp.sort_values("timestamp").reset_index(drop=True)
    return result


def load_funding() -> dict[str, pd.DataFrame]:
    path = OUT_DIR / "funding.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    df["fundingTime"] = df["fundingTime"].astype("int64")
    df["fundingRate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    result = {}
    for sym, grp in df.groupby("symbol"):
        result[sym] = grp.sort_values("fundingTime").reset_index(drop=True)
    return result


def load_liquidations() -> dict[str, pd.DataFrame]:
    path = OUT_DIR / "liquidations_summary.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    df["ts"] = df["ts"].astype("int64")
    df["usd"] = pd.to_numeric(df["usd"], errors="coerce").fillna(0)
    result = {}
    for sym, grp in df.groupby("symbol"):
        result[sym] = grp.sort_values("ts").reset_index(drop=True)
    return result


CATALYST_TYPES = ["liq_sweep", "macro_extreme", "stablecoin_inflow", "news_signal", "token_unlock"]


def load_causal() -> dict[int, dict]:
    """Return dict pump_id → {catalyst_hit_*: 0/1} from causal_attribution.csv."""
    path = OUT_DIR / "causal_attribution.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    result = {}
    for _, row in df.iterrows():
        cats_str = str(row.get("catalysts", "none") or "none")
        cats_set = set(cats_str.lower().split("|"))
        result[int(row["pump_id"])] = {
            f"catalyst_hit_{c}": int(c in cats_set) for c in CATALYST_TYPES
        }
    return result


# ── feature lookup helpers ────────────────────────────────────────────────────

def _searchsorted_le(arr: np.ndarray, val: int) -> int:
    """Index of largest element <= val, or -1 if none."""
    idx = np.searchsorted(arr, val, side="right") - 1
    return int(idx)


def klines_features(kl: pd.DataFrame, start_ms: int) -> dict:
    """Return vol/CVD/context features from klines at start_ms."""
    ts_arr = kl["ts"].values

    # Last bar BEFORE start_ms is the bar with open_time < start_ms
    pos = _searchsorted_le(ts_arr, start_ms - 1)
    if pos < 0:
        return {}

    row_1h = kl.iloc[pos]
    vol_med = float(row_1h["vol_med_30d"]) if not np.isnan(row_1h["vol_med_30d"]) else None

    # Volume
    vol_1h = float(row_1h["volume"])
    vol_ratio_1h = vol_1h / vol_med if vol_med and vol_med > 0 else np.nan

    pos_4h_start = max(0, pos - 3)
    bars_4h = kl.iloc[pos_4h_start : pos + 1]
    vol_ratio_4h = float(bars_4h["volume"].mean()) / vol_med if vol_med and vol_med > 0 else np.nan
    vol_spike_count = int((bars_4h["volume"] > 2 * vol_med).sum()) if vol_med and vol_med > 0 else 0

    # CVD
    cvd_div_1h = float(row_1h["cvd_div"])
    close_change_1h = float(row_1h["close"]) - float(row_1h["open"])
    price_vs_cvd = int((close_change_1h > 0) != (cvd_div_1h > 0))  # 1 if divergence

    # Price vs 7d high
    pos_7d = max(0, pos - 7 * 24)
    bars_7d = kl.iloc[pos_7d : pos + 1]
    high_7d = float(bars_7d["high"].max()) if "high" in bars_7d.columns else np.nan
    close_now = float(row_1h["close"])
    price_vs_high_7d = close_now / high_7d if high_7d > 0 else np.nan

    # Hour of day
    hour_of_day = int((start_ms // H1_MS) % 24)

    return {
        "vol_ratio_1h":    vol_ratio_1h,
        "vol_ratio_4h":    vol_ratio_4h,
        "vol_spike_count": vol_spike_count,
        "cvd_div_1h":      cvd_div_1h,
        "price_vs_cvd":    price_vs_cvd,
        "price_vs_high_7d": price_vs_high_7d,
        "hour_of_day":     hour_of_day,
    }


def oi_features(oi_sym: pd.DataFrame, oi_btc: pd.DataFrame, start_ms: int) -> dict:
    ts_arr = oi_sym["timestamp"].values
    oi_arr = oi_sym["openInterest"].values

    pos = _searchsorted_le(ts_arr, start_ms)
    if pos < 1:
        return {}

    oi_now   = float(oi_arr[pos])
    oi_1h    = float(oi_arr[pos - 1]) if pos >= 1 else np.nan
    pos_4h   = max(0, pos - 4)
    oi_4h    = float(oi_arr[pos_4h])

    oi_chg_1h = (oi_now - oi_1h) / oi_1h if oi_1h > 0 else np.nan
    oi_chg_4h = (oi_now - oi_4h) / oi_4h if oi_4h > 0 else np.nan

    # BTC correlation over 7d
    oi_oi_btc_corr = np.nan
    if oi_btc is not None and pos >= 168:
        btc_ts  = oi_btc["timestamp"].values
        btc_oi  = oi_btc["openInterest"].values
        btc_pos = _searchsorted_le(btc_ts, start_ms)
        if btc_pos >= 168:
            sym_slice = np.diff(oi_arr[max(0, pos - 168): pos + 1])
            btc_slice = np.diff(btc_oi[max(0, btc_pos - 168): btc_pos + 1])
            n = min(len(sym_slice), len(btc_slice))
            if n >= 20:
                r, _ = scipy_stats.pearsonr(sym_slice[-n:], btc_slice[-n:])
                oi_oi_btc_corr = float(r)

    return {
        "oi_chg_1h":       oi_chg_1h,
        "oi_chg_4h":       oi_chg_4h,
        "oi_oi_btc_corr":  oi_oi_btc_corr,
    }


def funding_features(fund_sym: pd.DataFrame, start_ms: int) -> dict:
    ts_arr = fund_sym["fundingTime"].values
    rate_arr = fund_sym["fundingRate"].values

    pos = _searchsorted_le(ts_arr, start_ms)
    if pos < 0:
        return {}

    funding_last = float(rate_arr[pos])
    funding_trend_3 = float(rate_arr[pos] - rate_arr[max(0, pos - 2)]) if pos >= 2 else np.nan

    return {
        "funding_last":    funding_last,
        "funding_trend_3": funding_trend_3,
    }


def liq_features(liq_sym: pd.DataFrame | None, start_ms: int) -> dict:
    if liq_sym is None or liq_sym.empty:
        return {"liq_long_1h": 0.0, "liq_short_1h": 0.0, "liq_ratio": 0.5}
    lo = start_ms - H1_MS
    mask = (liq_sym["ts"] >= lo) & (liq_sym["ts"] < start_ms)
    w = liq_sym[mask]
    if w.empty:
        return {"liq_long_1h": 0.0, "liq_short_1h": 0.0, "liq_ratio": 0.5}
    long_usd  = float(w[w["side"] == "long_liq"]["usd"].sum())
    short_usd = float(w[w["side"] == "short_liq"]["usd"].sum())
    total     = long_usd + short_usd
    ratio     = long_usd / total if total > 0 else 0.5
    return {"liq_long_1h": long_usd, "liq_short_1h": short_usd, "liq_ratio": ratio}


def btc_trend(btc_klines: pd.DataFrame | None, start_ms: int) -> float:
    if btc_klines is None:
        return np.nan
    ts_arr = btc_klines["ts"].values
    pos = _searchsorted_le(ts_arr, start_ms - 1)
    if pos < 1:
        return np.nan
    c1 = float(btc_klines["close"].iat[pos])
    c2 = float(btc_klines["close"].iat[pos - 1])
    return (c1 - c2) / c2 if c2 > 0 else np.nan


# ── feature matrix builder ────────────────────────────────────────────────────

FEATURE_COLS = [
    "vol_ratio_1h", "vol_ratio_4h", "vol_spike_count",
    "oi_chg_1h", "oi_chg_4h", "oi_oi_btc_corr",
    "funding_last", "funding_trend_3",
    "cvd_div_1h", "price_vs_cvd",
    "liq_long_1h", "liq_short_1h", "liq_ratio",
    "btc_trend_1h", "hour_of_day", "price_vs_high_7d",
    # ST3b catalyst features
    "catalyst_hit_liq_sweep", "catalyst_hit_macro_extreme",
    "catalyst_hit_stablecoin_inflow", "catalyst_hit_news_signal",
    "catalyst_hit_token_unlock",
]


def build_feature_matrix(pumps: pd.DataFrame, controls: pd.DataFrame) -> pd.DataFrame:
    log.info("Loading OI, funding, liquidations, causal ...")
    oi_all      = load_oi()
    funding_all = load_funding()
    liq_all     = load_liquidations()
    causal_map  = load_causal()

    btc_kl = load_klines("BTCUSDT")
    oi_btc = oi_all.get("BTCUSDT")

    null_catalyst = {f"catalyst_hit_{c}": 0 for c in CATALYST_TYPES}

    # Build combined windows table
    pump_rows = pumps[["pump_id", "symbol", "start_ms", "split", "pump_type", "pct_chg"]].copy()
    pump_rows["label"]    = 1
    pump_rows["window_id"] = "p_" + pump_rows["pump_id"].astype(str)

    ctrl_rows = controls[["symbol", "start_ms", "split"]].copy()
    ctrl_rows["label"]    = 0
    ctrl_rows["pump_id"]  = np.nan
    ctrl_rows["pump_type"] = "control"
    ctrl_rows["pct_chg"]  = np.nan
    ctrl_rows["window_id"] = "c_" + ctrl_rows.index.astype(str)

    windows = pd.concat([pump_rows, ctrl_rows], ignore_index=True)
    symbols = windows["symbol"].unique()
    log.info("Computing features for %d windows across %d symbols ...", len(windows), len(symbols))

    all_rows = []
    for sym in sorted(symbols):
        kl = load_klines(sym)
        oi_sym   = oi_all.get(sym)
        fund_sym = funding_all.get(sym)
        liq_sym  = liq_all.get(sym)

        sym_windows = windows[windows["symbol"] == sym]
        for _, w in sym_windows.iterrows():
            start_ms = int(w["start_ms"])
            row = {
                "window_id": w["window_id"],
                "symbol":    sym,
                "start_ms":  start_ms,
                "label":     int(w["label"]),
                "split":     w["split"],
                "pump_type": w["pump_type"],
                "pct_chg":   w["pct_chg"],
            }

            if kl is not None:
                row.update(klines_features(kl, start_ms))
                row["btc_trend_1h"] = btc_trend(btc_kl, start_ms)
            if oi_sym is not None:
                row.update(oi_features(oi_sym, oi_btc, start_ms))
            if fund_sym is not None:
                row.update(funding_features(fund_sym, start_ms))
            row.update(liq_features(liq_sym, start_ms))

            # Catalyst features: non-zero only for pump windows; controls get 0
            if int(w["label"]) == 1 and not np.isnan(w.get("pump_id", float("nan"))):
                row.update(causal_map.get(int(w["pump_id"]), null_catalyst))
            else:
                row.update(null_catalyst)

            for col in FEATURE_COLS:
                row.setdefault(col, np.nan)
            all_rows.append(row)

    df = pd.DataFrame(all_rows)
    log.info("Feature matrix: %d rows × %d cols", len(df), len(df.columns))
    return df


# ── statistical analysis ──────────────────────────────────────────────────────

def compute_lift(feat_vals: pd.Series, labels: pd.Series, threshold: float,
                 base_rate: float) -> float:
    above = feat_vals > threshold
    n_above = above.sum()
    if n_above < 5:
        return np.nan
    rate_above = labels[above].mean()
    return rate_above / base_rate if base_rate > 0 else np.nan


def bootstrap_lift_ci(feat: np.ndarray, labels: np.ndarray, threshold: float,
                      base_rate: float, n_boot: int = 1000) -> tuple[float, float]:
    rng = np.random.default_rng(42)
    lifts = []
    n = len(feat)
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        f_b, l_b = feat[idx], labels[idx]
        above = f_b > threshold
        if above.sum() < 5:
            continue
        r = l_b[above].mean()
        lifts.append(r / base_rate if base_rate > 0 else np.nan)
    if len(lifts) < 100:
        return np.nan, np.nan
    return float(np.percentile(lifts, 2.5)), float(np.percentile(lifts, 97.5))


def feature_stats(fmat: pd.DataFrame) -> pd.DataFrame:
    train = fmat[fmat["split"] == "train"].copy()
    base_rate = train["label"].mean()
    log.info("Train base_rate: %.4f (%d pumps / %d total)", base_rate,
             train["label"].sum(), len(train))

    rows = []
    for feat in FEATURE_COLS:
        col = train[feat].dropna()
        if len(col) < 20:
            rows.append({"feature": feat, "note": "insufficient_data (<20 non-null)"})
            continue

        aligned = train.dropna(subset=[feat])
        pump_vals = aligned.loc[aligned["label"] == 1, feat].values
        ctrl_vals = aligned.loc[aligned["label"] == 0, feat].values

        if len(pump_vals) < 5 or len(ctrl_vals) < 5:
            rows.append({"feature": feat, "note": "insufficient_pump_or_ctrl_data"})
            continue

        median_pump = float(np.median(pump_vals))
        median_ctrl = float(np.median(ctrl_vals))

        u_stat, p_val = scipy_stats.mannwhitneyu(pump_vals, ctrl_vals, alternative="two-sided")
        p_val = float(p_val)

        threshold = float(np.median(ctrl_vals))  # use control median as threshold
        lift = compute_lift(aligned[feat], aligned["label"], threshold, base_rate)
        ci_lo, ci_hi = bootstrap_lift_ci(
            aligned[feat].values, aligned["label"].values, threshold, base_rate
        )

        rows.append({
            "feature":      feat,
            "median_pump":  round(median_pump, 6),
            "median_ctrl":  round(median_ctrl, 6),
            "n_pump":       len(pump_vals),
            "n_ctrl":       len(ctrl_vals),
            "mw_u":         float(u_stat),
            "p_value":      round(p_val, 6),
            "lift":         round(lift, 4) if not np.isnan(lift) else None,
            "lift_ci_lo":   round(ci_lo, 4) if not np.isnan(ci_lo) else None,
            "lift_ci_hi":   round(ci_hi, 4) if not np.isnan(ci_hi) else None,
            "note": ("significant" if p_val <= 0.05 and not np.isnan(lift) and lift and lift >= 1.2
                     else ("not_significant" if p_val > 0.05 else "significant_low_lift")),
        })

    return pd.DataFrame(rows).sort_values("lift", ascending=False, na_position="last")


def combo_stats(fmat: pd.DataFrame, top_features: list[str], n: int = 2) -> pd.DataFrame:
    train = fmat[fmat["split"] == "train"].dropna(subset=top_features)
    base_rate = train["label"].mean()
    rows = []
    for combo in combinations(top_features, n):
        subset = train.copy()
        # Threshold = control median for each feature
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
        lift_combo = prec / base_rate if base_rate > 0 else np.nan
        rows.append({
            "features": "+".join(combo),
            "n_flagged": len(flagged),
            "tp": tp, "fp": fp, "fn": fn,
            "precision": round(prec, 4),
            "recall":    round(rec,  4),
            "f1":        round(f1,   4),
            "lift":      round(float(lift_combo), 4),
        })
    return pd.DataFrame(rows).sort_values("f1", ascending=False)


def walkforward(fmat: pd.DataFrame, top_feature: str,
                step_days: int = 30) -> pd.DataFrame:
    if top_feature not in fmat.columns:
        return pd.DataFrame()
    df = fmat.dropna(subset=[top_feature]).copy()
    df["date"] = pd.to_datetime(df["start_ms"], unit="ms", utc=True)

    start_date = df["date"].min()
    end_date   = df["date"].max()
    step       = pd.Timedelta(days=step_days)

    rows = []
    train_end = start_date + step * 6  # need at least 6 months of training
    while train_end + step <= end_date:
        test_start = train_end
        test_end   = train_end + step

        train_df = df[df["date"] < train_end]
        test_df  = df[(df["date"] >= test_start) & (df["date"] < test_end)]

        if len(train_df) < 50 or len(test_df) < 5:
            train_end += step
            continue

        base_rate = train_df["label"].mean()
        # Learn threshold: percentile that gives lift ≥ 1.5 on train
        pump_med = train_df.loc[train_df["label"] == 1, top_feature].median()
        ctrl_med = train_df.loc[train_df["label"] == 0, top_feature].median()
        threshold = (pump_med + ctrl_med) / 2

        flagged = test_df[test_df[top_feature] > threshold]
        if len(flagged) < 2:
            train_end += step
            continue

        tp    = int(flagged["label"].sum())
        fp    = int((flagged["label"] == 0).sum())
        fn    = int(((test_df[top_feature] <= threshold) & (test_df["label"] == 1)).sum())
        prec  = tp / (tp + fp) if (tp + fp) > 0 else 0
        rec   = tp / (tp + fn) if (tp + fn) > 0 else 0
        rows.append({
            "period_start": str(test_start.date()),
            "period_end":   str(test_end.date()),
            "threshold":    round(threshold, 4),
            "n_train":      len(train_df),
            "n_test":       len(test_df),
            "tp": tp, "fp": fp, "fn": fn,
            "precision":    round(prec, 4),
            "recall":       round(rec,  4),
            "lift":         round(prec / base_rate if base_rate > 0 else np.nan, 4),
        })
        train_end += step

    return pd.DataFrame(rows)


def sensitivity_sweep(fmat: pd.DataFrame, top_features: list[str]) -> pd.DataFrame:
    """Lift of top features per ST2 label_stats.json pump threshold sweep."""
    label_stats_path = OUT_DIR / "label_stats.json"
    sweep_params: list[dict] = []
    if label_stats_path.exists():
        with open(label_stats_path) as fh:
            sweep_params = json.load(fh).get("sweep", [])

    train = fmat[fmat["split"] == "train"].copy()
    base_rate = train["label"].mean()

    feat_thresholds: dict[str, float | None] = {}
    for feat in top_features[:5]:
        if feat in train.columns:
            v = train.loc[train["label"] == 0, feat].median()
            feat_thresholds[feat] = float(v) if not np.isnan(v) else None

    rows = []
    for sp in sweep_params:
        row: dict = {
            "window_h":        sp["window_h"],
            "threshold":       sp["threshold"],
            "vol_mult":        sp["vol_mult"],
            "n_pump_sweep":    sp["n_pumps"],
            "base_rate_sweep": sp["base_rate"],
            "base_rate_train": round(float(base_rate), 6),
        }
        for feat, thresh in feat_thresholds.items():
            if thresh is None:
                row[f"lift_{feat}"] = None
                continue
            col = train.dropna(subset=[feat])
            above = col[col[feat] > thresh]
            lift = above["label"].mean() / base_rate if (base_rate > 0 and len(above) > 0) else np.nan
            row[f"lift_{feat}"] = round(float(lift), 4) if not np.isnan(lift) else None
        rows.append(row)

    if not rows:
        for pump_type in fmat["pump_type"].dropna().unique():
            if pump_type == "control":
                continue
            sub = fmat[((fmat["label"] == 1) & (fmat["pump_type"] == pump_type)) | (fmat["label"] == 0)]
            br = sub["label"].mean()
            row = {"pump_type": pump_type, "base_rate": round(float(br), 4)}
            for feat, thresh in feat_thresholds.items():
                if thresh is None:
                    continue
                col = sub.dropna(subset=[feat])
                above = col[col[feat] > thresh]
                lift = above["label"].mean() / br if (br > 0 and len(above) > 0) else np.nan
                row[f"lift_{feat}"] = round(float(lift), 4) if not np.isnan(lift) else None
            rows.append(row)

    return pd.DataFrame(rows)


# ── plots ─────────────────────────────────────────────────────────────────────

def plot_feature_lifts(stats_df: pd.DataFrame) -> None:
    valid = stats_df.dropna(subset=["lift"]).head(10)
    if valid.empty:
        return
    fig, ax = plt.subplots(figsize=(12, 5))
    colors = ["#e74c3c" if (row["p_value"] <= 0.05 if "p_value" in row else False)
              else "#95a5a6" for _, row in valid.iterrows()]
    bars = ax.barh(valid["feature"], valid["lift"].astype(float), color=colors)
    if "lift_ci_lo" in valid.columns and "lift_ci_hi" in valid.columns:
        xerr_lo = (valid["lift"] - valid["lift_ci_lo"]).clip(lower=0)
        xerr_hi = (valid["lift_ci_hi"] - valid["lift"]).clip(lower=0)
        ax.errorbar(valid["lift"].astype(float), range(len(valid)),
                    xerr=[xerr_lo.values, xerr_hi.values],
                    fmt="none", color="black", capsize=4)
    ax.axvline(1.0, color="gray", linestyle="--", linewidth=1)
    ax.set_xlabel("Lift (vs base rate)")
    ax.set_title("Top-10 Features by Lift (train split)\nRed = p≤0.05")
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
    ax.axhline(wf_df["lift"].map(lambda v: 1/v if v else np.nan).mean(), color="gray",
               linestyle="--", linewidth=1, label="Base rate (approx)")
    ax.set_xticks(list(x))
    ax.set_xticklabels(wf_df["period_start"].str[:7], rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Metric")
    ax.set_title("Walk-forward Validation — Precision & Recall")
    ax.legend()
    ax.set_ylim(0, 1)
    plt.tight_layout()
    plt.savefig(PLOTS_DIR / "walkforward_precision.png", dpi=150, bbox_inches="tight")
    plt.close()
    log.info("Plot saved → plots/walkforward_precision.png")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=== AVEVA-60 ST3: Feature engineering ===")

    pumps, ctrls = load_windows()
    log.info("Loaded: %d pumps, %d controls", len(pumps), len(ctrls))

    fmat = build_feature_matrix(pumps, ctrls)
    fmat.to_csv(OUT_DIR / "feature_matrix.csv", index=False)
    log.info("feature_matrix.csv → %d rows", len(fmat))

    log.info("Statistical analysis (train only) ...")
    stats_df = feature_stats(fmat)
    stats_df.to_csv(OUT_DIR / "feature_stats.csv", index=False)
    log.info("feature_stats.csv → %d rows", len(stats_df))

    # Exclude catalyst_hit_* features — only computed for pumps (not controls),
    # so their lift is artificially inflated and has no predictive validity.
    valid_stats = stats_df.dropna(subset=["lift"])
    top_feats = (valid_stats[~valid_stats["feature"].str.startswith("catalyst_hit")]
                 .head(10)["feature"].tolist())
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

    log.info("Sensitivity sweep ...")
    sweep_df = sensitivity_sweep(fmat, top_feats[:5])
    sweep_df.to_csv(OUT_DIR / "sensitivity_sweep.csv", index=False)
    log.info("sensitivity_sweep.csv → %d rows", len(sweep_df))

    plot_feature_lifts(stats_df)
    if top1:
        plot_walkforward(wf_df)

    # Summary
    log.info("\n=== Feature Analysis Summary ===")
    log.info("  Train base_rate: %.4f", fmat[fmat["split"] == "train"]["label"].mean())
    log.info("  Test  base_rate: %.4f", fmat[fmat["split"] == "test"]["label"].mean())
    sig = stats_df[stats_df["note"] == "significant"] if "note" in stats_df.columns else pd.DataFrame()
    log.info("  Significant features (p≤0.05, lift≥1.2): %d / %d", len(sig), len(stats_df))
    for _, row in stats_df.dropna(subset=["lift"]).head(5).iterrows():
        log.info("    %-22s lift=%.3f p=%.4f",
                 row["feature"], row.get("lift", float("nan")), row.get("p_value", float("nan")))


if __name__ == "__main__":
    main()

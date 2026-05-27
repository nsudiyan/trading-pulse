#!/usr/bin/env python3
"""
ST2 — Pump event labeler (AVEVA-56 / AVEVA-59).

Detects pump events from historical 1h klines and builds a labelled dataset
with matched negative (control) windows for downstream ML.

Pump definitions:
  main  — price rise >= 15% in <= 24h, volume >= 3× 30-day hourly median
  fast  — price rise >= 10% in <= 1h,  volume >= 3× 30-day hourly median

Sensitivity sweep covers:
  thresholds : [10%, 12%, 15%, 20%]
  windows    : [1h, 4h, 12h, 24h]
  vol_mults  : [2×, 3×, 5×]

Train/test split (by time, no data leakage):
  train — 2024-05-01 → 2025-11-01  (first 18 months)
  test  — 2025-11-01 → 2026-05-27  (last  ~6 months, out-of-sample)

Outputs (in pump_analysis/):
  pump_events.csv       — symbol, pump_id, start_ts, end_ts, pct_chg, vol_mult, pump_type, split
  control_windows.csv   — symbol, start_ts, end_ts, matched_pump_id, split
  label_stats.json      — base rates per symbol/split, sensitivity sweep counts
"""

from __future__ import annotations

import json
import random
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

# ── paths ─────────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent
KLINES_1H = BASE / "klines_1h"

OUT_EVENTS   = BASE / "pump_events.csv"
OUT_CONTROLS = BASE / "control_windows.csv"
OUT_STATS    = BASE / "label_stats.json"

# ── sweep params ──────────────────────────────────────────────────────────────
PRICE_THRESHOLDS = [0.10, 0.12, 0.15, 0.20]
WINDOW_HOURS     = [1, 4, 12, 24]
VOL_MULTIPLIERS  = [2.0, 3.0, 5.0]

CANONICAL_CONFIGS = [
    {"threshold": 0.15, "window_h": 24, "vol_mult": 3.0, "pump_type": "main"},
    {"threshold": 0.10, "window_h": 1,  "vol_mult": 3.0, "pump_type": "fast"},
]

# ── control sampling ──────────────────────────────────────────────────────────
K_CONTROLS     = 5      # negatives per pump
BUFFER_H       = 48     # hours of exclusion zone around each pump
VOL_LOOKBACK_H = 30 * 24  # 30-day rolling window for median volume

# ── train/test cutoff (absolute — reproducible) ───────────────────────────────
TRAIN_END = pd.Timestamp("2025-11-01", tz="UTC")

RANDOM_SEED = 42
random.seed(RANDOM_SEED)
np.random.seed(RANDOM_SEED)


# ── data loading ──────────────────────────────────────────────────────────────

def load_klines(symbol: str) -> pd.DataFrame | None:
    path = KLINES_1H / f"{symbol}.csv.gz"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df.sort_values("ts", inplace=True)
    df.reset_index(drop=True, inplace=True)
    # 30-day rolling median volume: shift(1) prevents lookahead at current bar
    df["med_vol_30d"] = (
        df["volume"]
        .rolling(VOL_LOOKBACK_H, min_periods=24)
        .median()
        .shift(1)
    )
    return df


# ── forward rolling helpers ───────────────────────────────────────────────────

def _fwd_rolling(series: pd.Series, window: int, func: str) -> pd.Series:
    """
    Apply rolling aggregation to series[i+1 : i+1+window] for each index i.

    Achieved by reversing the series, applying a backward rolling, reversing
    back, then shifting by -1 so position i sees the *next* window.
    """
    rev = series.iloc[::-1]
    rolled = getattr(rev.rolling(window=window, min_periods=window), func)()
    result = rolled.iloc[::-1]
    return result.shift(-1)


def fwd_max(series: pd.Series, window: int) -> pd.Series:
    return _fwd_rolling(series, window, "max")


def fwd_sum(series: pd.Series, window: int) -> pd.Series:
    return _fwd_rolling(series, window, "sum")


# ── pump detection ────────────────────────────────────────────────────────────

def detect_pumps(
    df: pd.DataFrame,
    symbol: str,
    threshold: float,
    window_h: int,
    vol_mult: float,
    pump_type: str,
) -> pd.DataFrame:
    """Vectorised pump detection — no lookahead at start-of-window."""
    max_close = fwd_max(df["close"], window_h)
    sum_vol   = fwd_sum(df["volume"], window_h)

    pct_chg   = (max_close - df["close"]) / df["close"]
    vol_ratio = (sum_vol / window_h) / df["med_vol_30d"]

    valid = (
        df["med_vol_30d"].notna()
        & max_close.notna()
        & (df["close"] > 0)
    )
    hits = df[valid & (pct_chg >= threshold) & (vol_ratio >= vol_mult)].copy()

    if hits.empty:
        return pd.DataFrame()

    # Find timestamp of peak close within the forward window (only for output CSV)
    close_arr = df["close"].values

    peak_ts_list = []
    for iloc_i in hits.index:  # df is reset_index so this IS the positional iloc
        wnd_close = close_arr[iloc_i + 1 : iloc_i + 1 + window_h]
        if len(wnd_close) == 0:
            peak_ts_list.append(df["ts"].iloc[iloc_i])
        else:
            peak_ts_list.append(df["ts"].iloc[iloc_i + 1 + int(np.argmax(wnd_close))])

    return pd.DataFrame({
        "symbol":    symbol,
        "start_ts":  hits["ts"].tolist(),
        "end_ts":    peak_ts_list,
        "pct_chg":   (pct_chg[hits.index] * 100).round(4).values,
        "vol_mult":  vol_ratio[hits.index].round(4).values,
        "pump_type": pump_type,
        "split":     "tbd",
    })


def dedup_pumps_by_type(pumps: pd.DataFrame) -> pd.DataFrame:
    """
    Within each (symbol, pump_type) group remove overlapping events,
    keeping the one with the highest pct_chg per cluster.
    """
    if pumps.empty:
        return pumps

    out = []
    for (sym, ptype), grp in pumps.groupby(["symbol", "pump_type"], sort=False):
        grp = grp.sort_values("start_ts").reset_index(drop=True)
        keep: list[dict] = []
        current: list[dict] = []
        cluster_end: pd.Timestamp | None = None

        def flush(cluster: list[dict]) -> None:
            best = max(cluster, key=lambda r: r["pct_chg"])
            keep.append(best)

        for _, row in grp.iterrows():
            if cluster_end is None or row["start_ts"] > cluster_end:
                if current:
                    flush(current)
                current = [row.to_dict()]
                cluster_end = row["end_ts"]
            else:
                current.append(row.to_dict())
                if row["end_ts"] > cluster_end:
                    cluster_end = row["end_ts"]

        if current:
            flush(current)

        out.extend(keep)

    return pd.DataFrame(out).reset_index(drop=True) if out else pd.DataFrame()


def assign_split(pumps: pd.DataFrame) -> pd.DataFrame:
    pumps = pumps.copy()
    pumps["split"] = np.where(pumps["start_ts"] <= TRAIN_END, "train", "test")
    return pumps


# ── control window sampling ───────────────────────────────────────────────────

def make_controls(
    df: pd.DataFrame,
    pumps: pd.DataFrame,
    symbol: str,
    window_h: int = 24,
) -> pd.DataFrame:
    """
    Sample K=5 negative windows per pump for the same symbol.
    Constraints:
      - No overlap with any pump ± BUFFER_H hours
      - Same split (train / test) as the matched pump
      - Evenly spaced across the eligible period (uniformly distributed)
    """
    if pumps.empty or len(df) < window_h + 2:
        return pd.DataFrame()

    buf = pd.Timedelta(hours=BUFFER_H)
    wnd = pd.Timedelta(hours=window_h)
    ts  = df["ts"]
    n   = len(df)

    # Build per-row exclusion mask (boolean Series)
    excluded = pd.Series(False, index=df.index)
    for _, pump in pumps.iterrows():
        excluded |= (ts >= pump["start_ts"] - buf) & (ts <= pump["end_ts"] + buf)
    excluded |= df["med_vol_30d"].isna()
    # Drop last window_h rows (no full forward window)
    excluded.iloc[max(0, n - window_h) :] = True

    records = []
    for pump_id, pump in pumps.iterrows():
        split = pump["split"]
        if split == "train":
            period_mask = ts <= TRAIN_END - wnd
        else:
            period_mask = (ts > TRAIN_END) & (ts <= ts.max() - wnd)

        pool = df.index[~excluded & period_mask].tolist()
        if not pool:
            continue

        k = min(K_CONTROLS, len(pool))
        # Evenly spaced indices across the pool for uniform temporal distribution
        sel_positions = np.linspace(0, len(pool) - 1, k, dtype=int)
        # Add small random jitter so identical pumps don't always get the exact same windows
        jitter_range = max(1, len(pool) // (k * 4))
        jitter = np.random.randint(-jitter_range, jitter_range + 1, size=k)
        sel_positions = np.clip(sel_positions + jitter, 0, len(pool) - 1)
        sel_positions = np.unique(sel_positions)

        for pos in sel_positions:
            ci = pool[pos]
            records.append({
                "symbol":          symbol,
                "start_ts":        ts.iloc[ci],
                "end_ts":          ts.iloc[ci] + wnd,
                "matched_pump_id": pump_id,
                "split":           split,
            })

    return pd.DataFrame(records) if records else pd.DataFrame()


# ── sensitivity sweep stats ───────────────────────────────────────────────────

def sweep_stats(df: pd.DataFrame, symbol: str) -> list[dict]:
    rows = []
    for window_h, threshold, vol_mult in product(WINDOW_HOURS, PRICE_THRESHOLDS, VOL_MULTIPLIERS):
        max_c    = fwd_max(df["close"], window_h)
        sum_v    = fwd_sum(df["volume"], window_h)
        pct_chg  = (max_c - df["close"]) / df["close"]
        vol_ratio = (sum_v / window_h) / df["med_vol_30d"]
        valid    = df["med_vol_30d"].notna() & max_c.notna() & (df["close"] > 0)
        n_valid  = int(valid.sum())
        n_pumps  = int(((pct_chg >= threshold) & (vol_ratio >= vol_mult) & valid).sum())
        rows.append({
            "symbol":    symbol,
            "window_h":  window_h,
            "threshold": threshold,
            "vol_mult":  vol_mult,
            "n_valid":   n_valid,
            "n_pumps":   n_pumps,
            "base_rate": round(n_pumps / n_valid, 6) if n_valid > 0 else 0.0,
        })
    return rows


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    symbols = sorted(p.stem.replace(".csv", "") for p in KLINES_1H.glob("*.csv.gz"))
    print(f"Symbols: {len(symbols)}")
    print(f"Train ends: {TRAIN_END.date()}  |  Test through: end-of-data\n")

    all_pumps:    list[pd.DataFrame] = []
    all_controls: list[pd.DataFrame] = []
    all_sweep:    list[dict]         = []

    for symbol in symbols:
        df = load_klines(symbol)
        if df is None or len(df) < VOL_LOOKBACK_H + 24:
            print(f"  SKIP  {symbol}  (insufficient rows)")
            continue

        # Canonical detection
        sym_pumps_frames = []
        for cfg in CANONICAL_CONFIGS:
            raw = detect_pumps(df, symbol, cfg["threshold"], cfg["window_h"],
                               cfg["vol_mult"], cfg["pump_type"])
            if not raw.empty:
                sym_pumps_frames.append(raw)

        sym_pumps = (
            dedup_pumps_by_type(pd.concat(sym_pumps_frames, ignore_index=True))
            if sym_pumps_frames else pd.DataFrame()
        )

        if not sym_pumps.empty:
            sym_pumps = assign_split(sym_pumps)
            n_main = (sym_pumps["pump_type"] == "main").sum()
            n_fast = (sym_pumps["pump_type"] == "fast").sum()
            print(f"  {symbol:20s}  {len(sym_pumps):3d} pumps  "
                  f"(main={n_main} fast={n_fast}  "
                  f"train={(sym_pumps['split']=='train').sum()} "
                  f"test={(sym_pumps['split']=='test').sum()})")
            controls = make_controls(df, sym_pumps, symbol, window_h=24)
            all_pumps.append(sym_pumps)
            if not controls.empty:
                all_controls.append(controls)
        else:
            print(f"  {symbol:20s}    0 pumps")

        all_sweep.extend(sweep_stats(df, symbol))

    # ── combine ───────────────────────────────────────────────────────────────
    pumps_df = (
        pd.concat(all_pumps, ignore_index=True).reset_index(drop=True)
        if all_pumps else pd.DataFrame()
    )
    controls_df = (
        pd.concat(all_controls, ignore_index=True).reset_index(drop=True)
        if all_controls else pd.DataFrame()
    )

    if not pumps_df.empty:
        pumps_df.insert(0, "pump_id", range(len(pumps_df)))
        pumps_df.to_csv(OUT_EVENTS, index=False)
        print(f"\n-> pump_events.csv      {len(pumps_df):5d} rows")
    else:
        print("\nWARNING: no pumps detected")

    if not controls_df.empty:
        # Remap matched_pump_id to the final pump_id column
        if not pumps_df.empty:
            old_to_new = {old_idx: new_id for new_id, old_idx in
                          zip(pumps_df["pump_id"], pumps_df.index)}
            controls_df["matched_pump_id"] = controls_df["matched_pump_id"].map(
                lambda x: old_to_new.get(x, x)
            )
        controls_df.to_csv(OUT_CONTROLS, index=False)
        print(f"-> control_windows.csv  {len(controls_df):5d} rows")
    else:
        print("WARNING: no control windows generated")

    # ── label stats ───────────────────────────────────────────────────────────
    def split_stats(split: str) -> dict:
        n_p = int((pumps_df["split"] == split).sum()) if not pumps_df.empty else 0
        n_c = int((controls_df["split"] == split).sum()) if not controls_df.empty else 0
        total = n_p + n_c
        return {"n_pumps": n_p, "n_controls": n_c,
                "base_rate": round(n_p / total, 4) if total else 0.0}

    by_symbol = {}
    if not pumps_df.empty:
        for sym, grp in pumps_df.groupby("symbol"):
            n_p = len(grp)
            n_c = int((controls_df["symbol"] == sym).sum()) if not controls_df.empty else 0
            total = n_p + n_c
            by_symbol[sym] = {"n_pumps": n_p, "n_controls": n_c,
                               "base_rate": round(n_p / total, 4) if total else 0.0}

    # Aggregate sweep over all symbols
    sweep_df = pd.DataFrame(all_sweep)
    sweep_agg: list[dict] = []
    if not sweep_df.empty:
        for (wh, thr, vm), g in sweep_df.groupby(["window_h", "threshold", "vol_mult"]):
            nv = int(g["n_valid"].sum())
            np_ = int(g["n_pumps"].sum())
            sweep_agg.append({
                "window_h": int(wh), "threshold": float(thr), "vol_mult": float(vm),
                "n_valid": nv, "n_pumps": np_,
                "base_rate": round(np_ / nv, 6) if nv else 0.0,
            })

    stats = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "train_end": str(TRAIN_END.date()),
        "n_pumps_total":   int(len(pumps_df)) if not pumps_df.empty else 0,
        "n_controls_total": int(len(controls_df)) if not controls_df.empty else 0,
        "by_split":  {s: split_stats(s) for s in ["train", "test"]},
        "by_symbol": by_symbol,
        "sweep":     sweep_agg,
    }

    with open(OUT_STATS, "w") as fh:
        json.dump(stats, fh, indent=2, default=str)
    print(f"-> label_stats.json     written\n")

    # ── summary ───────────────────────────────────────────────────────────────
    if not pumps_df.empty:
        tr = stats["by_split"]["train"]
        te = stats["by_split"]["test"]
        print("Split summary:")
        print(f"  train  pumps={tr['n_pumps']:4d}  controls={tr['n_controls']:5d}  "
              f"base_rate={tr['base_rate']:.3f}")
        print(f"  test   pumps={te['n_pumps']:4d}  controls={te['n_controls']:5d}  "
              f"base_rate={te['base_rate']:.3f}")


if __name__ == "__main__":
    main()

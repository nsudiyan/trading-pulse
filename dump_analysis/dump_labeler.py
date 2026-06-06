#!/usr/bin/env python3
"""
ST1 — Dump event labeler (AVEC-63 / AVEVA-64).

Detects dump events from historical 1h klines and builds a labelled dataset
with matched negative (control) windows for downstream ML.

Dump definitions:
  standard — price fall >= 15% in <= 24h, volume >= 2× 30-day hourly median
  fast     — price fall >= 10% in <= 1h,  volume >= 2× 30-day hourly median

Train/test split (by time, no data leakage):
  train — 2024-05-01 → 2025-11-01  (first 18 months)
  test  — 2025-11-01 → 2026-05-27  (last  ~6 months, out-of-sample)

Inputs (uses existing pump_analysis klines — no re-download):
  pump_analysis/klines_1h/*.csv.gz   (28 symbols)
  pump_analysis/klines_5m/*.csv.gz   (available if needed)

Outputs (in dump_analysis/):
  dump_events.csv       — dump_id, symbol, start_ts, end_ts, pct_chg, vol_mult, dump_type, split
  control_windows.csv   — symbol, start_ts, end_ts, matched_dump_id, split
  label_stats.json      — N dumps, breakdown by type/split, sensitivity sweep
"""

from __future__ import annotations

import json
import random
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

# ── paths ─────────────────────────────────────────────────────────────────────
BASE      = Path(__file__).parent
KLINES_1H = BASE.parent / "pump_analysis" / "klines_1h"

OUT_EVENTS   = BASE / "dump_events.csv"
OUT_CONTROLS = BASE / "control_windows.csv"
OUT_STATS    = BASE / "label_stats.json"

# ── sweep params ──────────────────────────────────────────────────────────────
PRICE_THRESHOLDS = [0.07, 0.10, 0.15, 0.20]
WINDOW_HOURS     = [1, 4, 12, 24]
VOL_MULTIPLIERS  = [1.5, 2.0, 3.0]

CANONICAL_CONFIGS = [
    {"threshold": 0.15, "window_h": 24, "vol_mult": 2.0, "dump_type": "standard"},
    {"threshold": 0.10, "window_h": 1,  "vol_mult": 2.0, "dump_type": "fast"},
]

# ── control sampling ──────────────────────────────────────────────────────────
K_CONTROLS     = 5       # negatives per dump
BUFFER_H       = 48      # hours of exclusion zone around each dump
VOL_LOOKBACK_H = 30 * 24  # 30-day rolling window for median volume

# ── train/test cutoff ────────────────────────────────────────────────────────
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
    rev    = series.iloc[::-1]
    rolled = getattr(rev.rolling(window=window, min_periods=window), func)()
    result = rolled.iloc[::-1]
    return result.shift(-1)


def fwd_min(series: pd.Series, window: int) -> pd.Series:
    return _fwd_rolling(series, window, "min")


def fwd_sum(series: pd.Series, window: int) -> pd.Series:
    return _fwd_rolling(series, window, "sum")


# ── dump detection ────────────────────────────────────────────────────────────

def detect_dumps(
    df: pd.DataFrame,
    symbol: str,
    threshold: float,
    window_h: int,
    vol_mult: float,
    dump_type: str,
) -> pd.DataFrame:
    """Vectorised dump detection — no lookahead at start-of-window."""
    min_close = fwd_min(df["close"], window_h)
    sum_vol   = fwd_sum(df["volume"], window_h)

    # pct_chg: positive value = magnitude of the price drop from entry close
    pct_chg   = (df["close"] - min_close) / df["close"]
    vol_ratio = (sum_vol / window_h) / df["med_vol_30d"]

    valid = (
        df["med_vol_30d"].notna()
        & min_close.notna()
        & (df["close"] > 0)
    )
    hits = df[valid & (pct_chg >= threshold) & (vol_ratio >= vol_mult)].copy()

    if hits.empty:
        return pd.DataFrame()

    close_arr = df["close"].values

    trough_ts_list = []
    for iloc_i in hits.index:  # df is reset_index so this IS the positional iloc
        wnd_close = close_arr[iloc_i + 1 : iloc_i + 1 + window_h]
        if len(wnd_close) == 0:
            trough_ts_list.append(df["ts"].iloc[iloc_i])
        else:
            trough_ts_list.append(df["ts"].iloc[iloc_i + 1 + int(np.argmin(wnd_close))])

    return pd.DataFrame({
        "symbol":    symbol,
        "start_ts":  hits["ts"].tolist(),
        "end_ts":    trough_ts_list,
        "pct_chg":   (pct_chg[hits.index] * 100).round(4).values,
        "vol_mult":  vol_ratio[hits.index].round(4).values,
        "dump_type": dump_type,
        "split":     "tbd",
    })


def dedup_dumps_by_type(dumps: pd.DataFrame) -> pd.DataFrame:
    """
    Within each (symbol, dump_type) group remove overlapping events,
    keeping the one with the highest pct_chg per cluster.
    """
    if dumps.empty:
        return dumps

    out = []
    for (sym, dtype), grp in dumps.groupby(["symbol", "dump_type"], sort=False):
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


def assign_split(dumps: pd.DataFrame) -> pd.DataFrame:
    dumps = dumps.copy()
    dumps["split"] = np.where(dumps["start_ts"] <= TRAIN_END, "train", "test")
    return dumps


# ── control window sampling ───────────────────────────────────────────────────

def make_controls(
    df: pd.DataFrame,
    dumps: pd.DataFrame,
    symbol: str,
    window_h: int = 24,
) -> pd.DataFrame:
    """
    Sample K=5 negative windows per dump for the same symbol.
    Constraints:
      - No overlap with any dump ± BUFFER_H hours
      - Same split (train / test) as the matched dump
      - Evenly spaced across the eligible period (uniformly distributed)
    """
    if dumps.empty or len(df) < window_h + 2:
        return pd.DataFrame()

    buf = pd.Timedelta(hours=BUFFER_H)
    wnd = pd.Timedelta(hours=window_h)
    ts  = df["ts"]
    n   = len(df)

    # Build per-row exclusion mask (boolean Series)
    excluded = pd.Series(False, index=df.index)
    for _, dump in dumps.iterrows():
        excluded |= (ts >= dump["start_ts"] - buf) & (ts <= dump["end_ts"] + buf)
    excluded |= df["med_vol_30d"].isna()
    # Drop last window_h rows (no full forward window)
    excluded.iloc[max(0, n - window_h) :] = True

    records = []
    for dump_id, dump in dumps.iterrows():
        split = dump["split"]
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
        # Add small random jitter so identical dumps don't always get the exact same windows
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
                "matched_dump_id": dump_id,
                "split":           split,
            })

    return pd.DataFrame(records) if records else pd.DataFrame()


# ── sensitivity sweep stats ───────────────────────────────────────────────────

def sweep_stats(df: pd.DataFrame, symbol: str) -> list[dict]:
    rows = []
    for window_h, threshold, vol_mult in product(WINDOW_HOURS, PRICE_THRESHOLDS, VOL_MULTIPLIERS):
        min_c    = fwd_min(df["close"], window_h)
        sum_v    = fwd_sum(df["volume"], window_h)
        pct_chg  = (df["close"] - min_c) / df["close"]
        vol_ratio = (sum_v / window_h) / df["med_vol_30d"]
        valid    = df["med_vol_30d"].notna() & min_c.notna() & (df["close"] > 0)
        n_valid  = int(valid.sum())
        n_dumps  = int(((pct_chg >= threshold) & (vol_ratio >= vol_mult) & valid).sum())
        rows.append({
            "symbol":    symbol,
            "window_h":  window_h,
            "threshold": threshold,
            "vol_mult":  vol_mult,
            "n_valid":   n_valid,
            "n_dumps":   n_dumps,
            "base_rate": round(n_dumps / n_valid, 6) if n_valid > 0 else 0.0,
        })
    return rows


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    symbols = sorted(p.stem.replace(".csv", "") for p in KLINES_1H.glob("*.csv.gz"))
    print(f"Symbols: {len(symbols)}")
    print(f"Train ends: {TRAIN_END.date()}  |  Test through: end-of-data\n")

    all_dumps:    list[pd.DataFrame] = []
    all_controls: list[pd.DataFrame] = []
    all_sweep:    list[dict]         = []

    for symbol in symbols:
        df = load_klines(symbol)
        if df is None or len(df) < VOL_LOOKBACK_H + 24:
            print(f"  SKIP  {symbol}  (insufficient rows)")
            continue

        # Canonical detection
        sym_dumps_frames = []
        for cfg in CANONICAL_CONFIGS:
            raw = detect_dumps(df, symbol, cfg["threshold"], cfg["window_h"],
                               cfg["vol_mult"], cfg["dump_type"])
            if not raw.empty:
                sym_dumps_frames.append(raw)

        sym_dumps = (
            dedup_dumps_by_type(pd.concat(sym_dumps_frames, ignore_index=True))
            if sym_dumps_frames else pd.DataFrame()
        )

        if not sym_dumps.empty:
            sym_dumps = assign_split(sym_dumps)
            n_std  = (sym_dumps["dump_type"] == "standard").sum()
            n_fast = (sym_dumps["dump_type"] == "fast").sum()
            print(f"  {symbol:20s}  {len(sym_dumps):3d} dumps  "
                  f"(standard={n_std} fast={n_fast}  "
                  f"train={(sym_dumps['split']=='train').sum()} "
                  f"test={(sym_dumps['split']=='test').sum()})")
            controls = make_controls(df, sym_dumps, symbol, window_h=24)
            all_dumps.append(sym_dumps)
            if not controls.empty:
                all_controls.append(controls)
        else:
            print(f"  {symbol:20s}    0 dumps")

        all_sweep.extend(sweep_stats(df, symbol))

    # ── combine ───────────────────────────────────────────────────────────────
    dumps_df = (
        pd.concat(all_dumps, ignore_index=True).reset_index(drop=True)
        if all_dumps else pd.DataFrame()
    )
    controls_df = (
        pd.concat(all_controls, ignore_index=True).reset_index(drop=True)
        if all_controls else pd.DataFrame()
    )

    if not dumps_df.empty:
        dumps_df.insert(0, "dump_id", range(len(dumps_df)))
        dumps_df.to_csv(OUT_EVENTS, index=False)
        print(f"\n-> dump_events.csv      {len(dumps_df):5d} rows")
    else:
        print("\nWARNING: no dumps detected")

    if not controls_df.empty:
        # Remap matched_dump_id to the final dump_id column
        if not dumps_df.empty:
            old_to_new = {old_idx: new_id for new_id, old_idx in
                          zip(dumps_df["dump_id"], dumps_df.index)}
            controls_df["matched_dump_id"] = controls_df["matched_dump_id"].map(
                lambda x: old_to_new.get(x, x)
            )
        controls_df.to_csv(OUT_CONTROLS, index=False)
        print(f"-> control_windows.csv  {len(controls_df):5d} rows")
    else:
        print("WARNING: no control windows generated")

    # ── label stats ───────────────────────────────────────────────────────────
    def split_stats(split: str) -> dict:
        n_d = int((dumps_df["split"] == split).sum()) if not dumps_df.empty else 0
        n_c = int((controls_df["split"] == split).sum()) if not controls_df.empty else 0
        total = n_d + n_c
        return {"n_dumps": n_d, "n_controls": n_c,
                "base_rate": round(n_d / total, 4) if total else 0.0}

    by_symbol: dict[str, dict] = {}
    if not dumps_df.empty:
        for sym, grp in dumps_df.groupby("symbol"):
            n_d = len(grp)
            n_c = int((controls_df["symbol"] == sym).sum()) if not controls_df.empty else 0
            total = n_d + n_c
            by_symbol[sym] = {"n_dumps": n_d, "n_controls": n_c,
                               "base_rate": round(n_d / total, 4) if total else 0.0}

    # Aggregate sweep over all symbols
    sweep_df = pd.DataFrame(all_sweep)
    sweep_agg: list[dict] = []
    if not sweep_df.empty:
        for (wh, thr, vm), g in sweep_df.groupby(["window_h", "threshold", "vol_mult"]):
            nv  = int(g["n_valid"].sum())
            nd_ = int(g["n_dumps"].sum())
            sweep_agg.append({
                "window_h": int(wh), "threshold": float(thr), "vol_mult": float(vm),
                "n_valid": nv, "n_dumps": nd_,
                "base_rate": round(nd_ / nv, 6) if nv else 0.0,
            })

    n_by_type: dict[str, int] = {}
    if not dumps_df.empty:
        for dtype, grp in dumps_df.groupby("dump_type"):
            n_by_type[str(dtype)] = len(grp)

    stats = {
        "generated_at":    pd.Timestamp.now(tz="UTC").isoformat(),
        "train_end":       str(TRAIN_END.date()),
        "n_dumps_total":   int(len(dumps_df)) if not dumps_df.empty else 0,
        "n_controls_total": int(len(controls_df)) if not controls_df.empty else 0,
        "by_type":         n_by_type,
        "by_split":        {s: split_stats(s) for s in ["train", "test"]},
        "by_symbol":       by_symbol,
        "sweep":           sweep_agg,
    }

    with open(OUT_STATS, "w") as fh:
        json.dump(stats, fh, indent=2, default=str)
    print(f"-> label_stats.json     written\n")

    # ── summary ───────────────────────────────────────────────────────────────
    if not dumps_df.empty:
        tr = stats["by_split"]["train"]
        te = stats["by_split"]["test"]
        print("Split summary:")
        print(f"  train  dumps={tr['n_dumps']:4d}  controls={tr['n_controls']:5d}  "
              f"base_rate={tr['base_rate']:.3f}")
        print(f"  test   dumps={te['n_dumps']:4d}  controls={te['n_controls']:5d}  "
              f"base_rate={te['base_rate']:.3f}")
        print(f"\nDump types: {n_by_type}")


if __name__ == "__main__":
    main()

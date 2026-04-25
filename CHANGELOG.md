# Changelog

## [Phase 1] — 2026-04-25 (AVEA-31)

### T1.1 — k1w and k15m fetches added

**File:** `screener.py` — `_fetch_symbol_data_parallel()`

- Added `"k1w": lambda: fetch_klines(sym, "W", 52)` (weekly klines, 52 candles, TTL 24h)
- Added `"k15m": lambda: fetch_klines(sym, "15", 96)` (15m klines, 96 candles, TTL 3 min)
- Added `"k1w"` and `"k15m"` to the `defaults` dict
- Bumped `max_workers` from 8 → 10

These fetches are consumed by Phase 2 functions (`compute_weekly_context()`, `compute_15m_context()`).

---

### T1.2 — SHORT direction LR calibration

**Files:** `calibration/train_model_short.py` (new), `calibration/signal_weights_short.json` (new), `screener.py`

Key empirical finding: for SHORT setups, score > 150 shows WR = 33.8% vs 47.5% baseline (−11pp). High score is anti-correlated with SHORT WR.

- Created `calibration/train_model_short.py` — logistic regression trained on ШОРТ decisive 4h trades (n=371)
- AUC = 0.4844 (below 0.55 threshold) → only empirically robust weight deployed
- `calibration/signal_weights_short.json` contains `score_gt150: -8.8` (the only n≥50 reliable feature)
- `load_score_weights()` in `screener.py` now also loads `signal_weights_short.json` under `__signal_weights_short__`
- SHORT-specific adjustments applied in `score_symbol()` when `setup_dir == "short"`:
  - `score > 150` → −8.8 pts correction (tagged `sws` in score notes)

---

### T1.3 — Unit tests for detect_* functions

**File:** `tests/test_detect.py` (new), `tests/__init__.py` (new)

Created pytest test suite with 24 assertions covering:
- `detect_sweep()` — 5 tests: sweep_up, sweep_down, flat/no-sweep, insufficient data, 3-candle lookback
- `detect_fvg()` — 5 tests: bull FVG, bear FVG, flat (no FVG), return type, struct keys
- `detect_order_blocks()` — 4 tests: bull OB, struct keys, flat market, max-5 cap
- `detect_choch()` — 4 tests: bull_choch (downtrend break), bear_choch (uptrend break), None on short data, None on flat
- `detect_htf_trend()` — 6 tests: bull, bear, range, insufficient data, valid values, SMA20 fallback

All 24 tests pass: `pytest tests/test_detect.py` → 24 passed in 0.06s

---

### T1.4 — WAIT signal misclassification fix

**File:** `outcome_tracker.py`

ANALYSIS.md finding: 45 short_dist signals with `direction == "ЖДАТЬ"` all resolved as TP1 (100% WR), inflating short_dist 24h WR from 39.0% (true SHORT-only) to an artificially high level.

Root cause: ЖДАТЬ signals were resolved using LONG-style TP logic → price moved up → hit LONG TP1 → incorrect WIN.

Fix applied in both 4h and 24h resolution blocks:
```python
# After the directional outcome computation:
if direction == "ЖДАТЬ":
    outcome = "FLAT"
    hit_tp1 = False
    hit_stop = False
```

ЖДАТЬ outcomes are now always recorded as FLAT and excluded from directional WR aggregation. Real short_dist SHORT 24h WR = 39.0%.

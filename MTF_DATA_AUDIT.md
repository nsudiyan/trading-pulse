# MTF Data Audit — AVEA-26
**Date**: 2026-04-25  
**Scope**: All 5 timeframes (15m / 1H / 4H / 1D / 1W) in `free_data.py` and `screener.py`

---

## Summary Table

| Timeframe | Bybit interval | Currently Fetched | Currently Used | Bybit API supports |
|-----------|---------------|-------------------|----------------|--------------------|
| **15m**   | `"15"`        | ❌ No             | ❌ No          | ✅ Yes             |
| **1H**    | `"60"`        | ✅ Yes (212 bars) | ✅ Extensively | ✅ Yes             |
| **4H**    | `"240"`       | ✅ Yes (212 bars) | ✅ Extensively | ✅ Yes             |
| **1D**    | `"D"`         | ✅ Yes (52 bars)  | ✅ Partially   | ✅ Yes             |
| **1W**    | `"W"`         | ❌ No             | ❌ No          | ✅ Yes             |

**Result: 3 of 5 timeframes fetched. 15m and 1W are completely missing.**

---

## Where data is fetched: `screener.py`

`free_data.py` contains **zero kline/OHLCV fetches** — it only fetches ETF flows, macro calendar, options data (Deribit), CoinGecko trending, and Binance Futures order book/taker pressure. All timeframe data lives in `screener.py`.

### Parallel fetch block (`_fetch_symbol_data_parallel`, line 4776)

```python
tasks = {
    "oi":     lambda: fetch_oi_history(sym, limit=50),
    "k1h":    lambda: fetch_klines(sym, "60",  212),  # ✅ 1H
    "k4h":    lambda: fetch_klines(sym, "240", 212),  # ✅ 4H
    "kD":     lambda: fetch_klines(sym, "D",    52),  # ✅ 1D
    "fund":   lambda: fetch_funding_history(sym, limit=8),
    "ls":     lambda: fetch_ls_ratio(sym),
    "trades": lambda: fetch_recent_trades(sym, 1000),
    "book":   lambda: fetch_orderbook(sym, 50),
}
```

15m (`"15"`) and 1W (`"W"`) are absent.

### `fetch_klines` function (line 241)

Uses Bybit V5 `/v5/market/kline` with `interval` param. Docstring only documents `'60'`, `'240'`, `'D'` — 15m and 1W not mentioned. The function itself is generic and supports any valid Bybit interval string.

---

## What each fetched timeframe is used for

### 1H (op1h, hi1h, lo1h, cl1h, vol1h)
- FVG detection: `detect_fvg(..., lookback=40, min_size_pct=0.05)`
- Order Blocks: `detect_order_blocks(..., lookback=40)`
- MTF Confluence (1H+4H)
- CVD kline: `calc_kline_cvd(..., lookback=20)`
- Candle patterns: `detect_candle_patterns(...)`
- ATR: `calc_atr(..., period=14)`
- RSI + RSI divergence: `calc_rsi(cl1h)`, `detect_rsi_divergence(..., lookback=30)`
- VWAP: `calc_vwap(..., lookback=24)`
- EMA structure (20/50/200): `calc_ema_structure(...)`
- CHoCH 1H: `detect_choch(..., lookback=30)` — strongest signal (+14.3pp WR per ANALYSIS.md)
- Volume Profile + LVN
- Equal highs/lows

### 4H (op4h, hi4h, lo4h, cl4h, vol4h)
- FVG 4H: `detect_fvg(..., lookback=30, min_size_pct=0.10)`
- Order Blocks 4H: `detect_order_blocks(..., lookback=30)`
- HTF trend: `detect_htf_trend(hi4h, lo4h, cl4h)` → feeds `h4_trend` (bull/bear/neutral)
- EMA structure 4H: for score bonuses in Setup 4 (EMA_bull4H +10, EMA200↑4H +8)
- CHoCH 4H: `detect_choch(..., lookback=20)` — used in Setups 4 and 5
- MTF Confluence as senior TF

### 1D (opD, hiD, loD, clD, volD)
- HTF trend: `detect_htf_trend(hiD, loD, clD)` → feeds `daily_trend`
- FVG 1D: `detect_fvg(..., lookback=20, min_size_pct=0.30)` for MTF Extended
- Order Blocks 1D: `detect_order_blocks(..., lookback=20)` for MTF Extended
- MTF Extended: 1H+4H+1D zone overlap → bonus `MTF_1D` in scoring

---

## Missing timeframes

### 15m — Entry timing layer (MISSING)

**Use case**: 15m is the natural entry confirmation layer between 1H signal and trade execution.

**What we miss without it**:
1. **Sub-1H sweep detection**: Current `detect_sweep` uses 3-candle 1H lookback. A liquidity sweep on the 15m chart (4 candles = 1H bar) is invisible to the screener. Per AVEC-16 mandate, sweeps expire in 1–3 candles — on 1H data that means we only catch sweeps that have already aged 1–3 hours.
2. **15m FVG/OB for entry precision**: 1H FVG zones are 20–80 pip wide. 15m zones inside them give sub-level entry with tighter SL.
3. **15m CHoCH**: Allows entry confirmation *within* the 1H structure shift rather than waiting for next 1H close.
4. **Engulfing/hammer on 15m**: Candle patterns at key levels are more actionable on 15m than 1H.

**Bybit interval string**: `"15"` (confirmed supported by Bybit V5 API).  
**Suggested limit**: 200 bars = 50 hours of 15m data.

### 1W — Macro structural layer (MISSING)

**Use case**: Weekly timeframe gives major S/R levels, weekly OB zones, and macro trend direction that 1D can miss during consolidations.

**What we miss without it**:
1. **Weekly OB/FVG**: Large institutional order blocks that are only visible on the 1W chart. These act as hard walls for 4H+ trends.
2. **Weekly trend context**: Current HTF = Daily + 4H. A daily downtrend against a weekly uptrend is different from a daily downtrend aligned with weekly downtrend. The system treats them identically.
3. **Weekly equal highs/lows**: Liquidity pools above/below weekly levels are the highest-probability sweep targets.
4. **Weekly structure (BOS/CHoCH)**: Would add a 5th confluence layer above 1D.

**Bybit interval string**: `"W"` (confirmed supported by Bybit V5 API).  
**Suggested limit**: 52 bars = 1 year of weekly data.

---

## API capacity check

`fetch_klines(symbol, interval, limit)` uses Bybit V5 `/v5/market/kline`. Bybit supports:

| Interval string | Timeframe |
|----------------|-----------|
| `"1"`, `"3"`, `"5"`, `"15"`, `"30"` | Minutes |
| `"60"`, `"120"`, `"240"`, `"360"`, `"720"` | Hours (minute encoding) |
| `"D"` | Daily |
| `"W"` | Weekly |
| `"M"` | Monthly |

**All 5 target timeframes (15m, 1H, 4H, 1D, 1W) are supported by Bybit public API.** No API key required.

---

## Implementation plan

### Step 1 — Add fetches to `_fetch_symbol_data_parallel` (screener.py:4776)

```python
tasks = {
    "oi":    lambda: fetch_oi_history(sym, limit=50),
    "k15m":  lambda: fetch_klines(sym, "15",  200),   # ADD: 15m entry layer
    "k1h":   lambda: fetch_klines(sym, "60",  212),
    "k4h":   lambda: fetch_klines(sym, "240", 212),
    "kD":    lambda: fetch_klines(sym, "D",    52),
    "k1w":   lambda: fetch_klines(sym, "W",    52),   # ADD: 1W macro layer
    "fund":  lambda: fetch_funding_history(sym, limit=8),
    "ls":    lambda: fetch_ls_ratio(sym),
    "trades":lambda: fetch_recent_trades(sym, 1000),
    "book":  lambda: fetch_orderbook(sym, 50),
}
```

Increase `ThreadPoolExecutor(max_workers=8)` to `max_workers=10` at line 4793.

Add defaults:
```python
defaults = {
    ...,
    "k15m": _empty_klines,
    "k1w":  _empty_klines,
}
```

### Step 2 — Unpack in `_fetch_and_score` (screener.py:4818)

```python
op15m, hi15m, lo15m, cl15m, vol15m = _d["k15m"]
# ...existing unpacking...
op1w,  hi1w,  lo1w,  cl1w,  vol1w  = _d["k1w"]
```

### Step 3 — Pass to `score_symbol`

Add `op15m`...`vol15m` and `op1w`...`vol1w` to the `score_symbol` signature.

### Step 4 — Use in `score_symbol`

**15m signals** (in `score_symbol`, alongside existing 1H analysis):
- `choch_15m = detect_choch(hi15m, lo15m, cl15m, lookback=30)` — entry confirmation
- `sweep_15m = detect_sweep(hi15m, lo15m, cl15m)` — sub-1H sweep detection
- `fvgs_15m = detect_fvg(hi15m, lo15m, cl15m, lookback=30, min_size_pct=0.03)` — tight entry zones
- `candle_15m = detect_candle_patterns(op15m, hi15m, lo15m, cl15m)` — entry candle

**1W signals** (HTF extension):
- `weekly_trend = detect_htf_trend(hi1w, lo1w, cl1w)` — macro structure direction
- `fvgs_1w = detect_fvg(hi1w, lo1w, cl1w, lookback=20, min_size_pct=0.50)` — weekly OB/FVG
- `obs_1w = detect_order_blocks(op1w, hi1w, lo1w, cl1w, vol1w, lookback=20)` — weekly OB
- MTF Extended update: 1H+4H+1D+1W

### Step 5 — Update `fetch_klines` docstring (line 243)

```python
"""
OHLCV. interval: '15'=15m, '60'=1H, '240'=4H, 'D'=Daily, 'W'=Weekly.
"""
```

---

## Performance impact

| Metric | Before | After |
|--------|--------|-------|
| Parallel fetches per symbol | 8 | 10 |
| API calls per screener run (100 symbols) | 800 | 1000 |
| Wall-clock time (parallel, ~0.3s dominant) | ~0.3s/sym | ~0.3s/sym (unchanged — still parallel) |
| 15m data per symbol | 0 | 200 bars × 5 OHLCV = 1000 floats |
| 1W data per symbol | 0 | 52 bars × 5 OHLCV = 260 floats |

Memory overhead: negligible (~10KB per symbol).  
Latency overhead: none — ThreadPoolExecutor absorbs the two extra fetches.

---

## free_data.py verdict

`free_data.py` has **no timeframe gaps**. It is not a kline-fetching module. All its data sources are timeframe-agnostic (ETF flows, macro events, options, social sentiment). No changes needed to `free_data.py` for this audit.

---

## Next step

Implement the two missing timeframe fetches and wire 15m+1W into scoring signals.  
Suggested follow-up issue: **AVEA-27** — "Implement 15m entry signals and 1W macro trend in score_symbol".

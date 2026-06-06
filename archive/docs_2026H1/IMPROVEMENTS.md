# Proposed Improvements to Setup Scoring

**Date:** 2026-04-24  
**Author:** CEO agent (AVEC-13)  
**Basis:** WR audit (ANALYSIS.md, N=2,232 resolved trades, 2026-04-12 to 2026-04-23), screener.py code review  
**Status:** Proposals ONLY — do not modify screener.py until IMPLEMENTATION_PLAN.md is board-approved

---

## Setup 1 — Squeeze (WR 35.8% @ 4H, 46.4% @ 24H)

### Problem 1: Mid-score band (100–140) is the worst performer
**Evidence:** U-shaped WR curve — scores <100 achieve 53.6% WR, scores >150 achieve 51.0% WR, but scores 100–140 achieve only 42.0% WR (24H). The middle band is statistically worse than a coin flip.

**Root cause:** The scoring accumulates many small bonuses (5–8 pts each) that push a mediocre setup into the 100–140 range without any single strong confirming signal. These signals look OK on paper but lack a decisive catalyst.

**Proposed fix:**
```python
# In _passes_setup_tg_filter() or equivalent gate logic
# Add a "squeeze quality" minimum: at least ONE of these must be present
SQUEEZE_HARD_REQUIREMENTS = [
    lambda r: r.get("fund_extreme") in ("extreme_neg", "high_neg"),  # strong funding pressure
    lambda r: r.get("choch_1h") == "bull_choch",                     # structure change confirmed
    lambda r: r.get("liq_short_usd", 0) >= 300_000,                  # real liquidation data
    lambda r: r.get("bull_mtf", 0) >= 2,                              # MTF confluence ≥2
]
# If score is 100-140, require at least 1 hard requirement
# Score <100 or >150: no additional gate (already filtered by min/max thresholds)
```

**Weight adjustments:**
- CHoCH↑1H: +25 is correct (already implemented based on WR=55.3%, +14.3pp). Keep.
- `in_bull_fvg` (+18) and `in_bull_ob` (+18): These should require `price_pos < 0.40` to avoid scoring zones far from the bottom. Proposed: add `and price_pos < 0.40` conditional.
- `bull_div` OI divergence (+18): Remove the unconditional award. Require `price_pos < 0.35`.

### Problem 2: No volume spike requirement
**Evidence:** A squeeze should be confirmed by a volume event. The current scoring awards volume only if present (+0 if absent) but doesn't penalize its absence.

**Proposed fix:**
```python
# In s1 scoring block, after existing volume section
if vol_ratio < 0.8 and price_pos < 0.20:
    s1 -= 10  # low volume at bottom = no one is buying yet, trap
```

### Problem 3: Funding streak threshold too loose
Current: `neg_streak >= 3` → +6 pts. This fires on almost every setup since most squeezes have 3+ negative funding periods.

**Proposed fix:** Raise threshold: `neg_streak >= 4` → +6, `neg_streak >= 6` → +12.

---

## Setup 2 — BOS/FVG (WR 42.3% @ 4H, 51.2% @ 24H) — BEST SETUP

### Problem 1: No actual BOS verification
**Evidence:** The setup name says "Break of Structure" but the scoring only checks for FVG/OB zones. There is no code that verifies price actually broke a prior swing high/low.

**Root cause:** `detect_sweep()` detects fake breakouts; `detect_htf_trend()` checks overall trend direction. But neither confirms that in the 1H timeframe, price broke above the last swing high (for longs) or below the last swing low (for shorts).

**Proposed fix:** Add a BOS detection function:
```python
def detect_bos(highs, lows, closes, lookback=20):
    """
    Returns: 'bull_bos' if current close > highest high of prior N-1 candles
             'bear_bos' if current close < lowest low of prior N-1 candles
             None otherwise
    Uses last COMPLETED candle only ([-2] index).
    """
    if len(closes) < lookback + 2:
        return None
    ref_high = max(highs[-(lookback+2):-2])
    ref_low  = min(lows[-(lookback+2):-2])
    last_close = closes[-2]
    if last_close > ref_high:
        return "bull_bos"
    if last_close < ref_low:
        return "bear_bos"
    return None
```

Add to scoring:
```python
bos_1h = detect_bos(hi1h, lo1h, cl1h, lookback=20)
# In s2 scoring block, add:
if bos_1h == "bull_bos" and bull_mtf >= 1:
    s2 += 20; n2.append("BOS↑1H!")
elif bos_1h == "bear_bos" and bear_mtf >= 1:
    s2 += 20; n2.append("BOS↓1H!")
elif not bos_1h:
    s2 -= 10  # No BOS = this isn't really a BOS setup
```

### Problem 2: High-score signals (>150) are worse at 4H, better at 24H
**Evidence:** Score >150 → 33.6% WR (4H) vs 61.1% WR (24H). These are delayed winners.

**Proposed fix:** Add a metadata flag to high-score BOS/FVG signals:
```python
if s2 > 150:
    notes["bos_fvg"] += " | HOLD_24H"  # Signal Telegram to flag for longer hold
```
Do NOT suppress >150 signals — they're the best 24H entries. Just tag them differently.

### Problem 3: Price position bonus too wide
Current: `0.25 < price_pos < 0.75` → +15. This catches 50% of the range and gives points for being "in the middle," which has weak directional significance.

**Proposed fix:** Split into directional: 
```python
if bull_fvg_1h or bull_ob_1h:  # bullish BOS
    if 0.30 < price_pos < 0.60:  # valid pullback zone for longs
        s2 += 12
elif bear_fvg_1h or bear_ob_1h:  # bearish BOS
    if 0.40 < price_pos < 0.70:  # valid pullback zone for shorts
        s2 += 12
```

### Problem 4: Missing 4H FVG/OB in-zone bonus
Currently the +22 `in_zone` bonus only checks 1H FVG/OB. But 4H zones are structurally stronger.

**Proposed fix:**
```python
in_bull_fvg_4h = any(f["in_zone"] for f in fvgs_4h if f["type"] == "bull")
in_bear_fvg_4h = any(f["in_zone"] for f in fvgs_4h if f["type"] == "bear")
# In s2:
if in_bull_fvg_4h:
    s2 += 18; n2.append("В 4H_FVG↑!")
if in_bear_fvg_4h:
    s2 += 18; n2.append("В 4H_FVG↓!")
```

---

## Setup 3 — Range Sweep (WR 25.0% @ 4H) — DISABLED

### Problem 1: Batch polling misses sweep events (root cause)
**Evidence:** Average loss when wrong: −21.89% (4H). Screener runs on a cron schedule (~15min). Sweep events last 1–3 candles (1–3 hours). By the time the screener detects a sweep, price has already reversed significantly or continued, making the entry too late.

**Current code:** `sweep_watcher.py` exists as a separate process. The scoring logic in `score_symbol()` still computes sweep scores (+55 base), but the TG gate is set to 9999 (disabled).

**Root cause fix (structural, not a weight change):**
The `range_sweep` score should only be computed and emitted by `sweep_watcher.py` in real-time mode, NOT by the batch screener. The scoring block in `score_symbol()` is dead code for production use.

**Proposed fix:** Remove sweep scoring from batch screener entirely OR gate it with a recency flag:
```python
# Only score range_sweep if the sweep occurred within the last 2 completed candles
SWEEP_MAX_AGE_CANDLES = 2
sweep_up, sweep_down = detect_sweep(hi1h, lo1h, cl1h, lookback=SWEEP_MAX_AGE_CANDLES)
# If lookback=2, sweep must be in the most recent 2 closed hours
```

### Problem 2: Sweep base score (+55) is independent of sweep quality
Current code awards +55 for ANY sweep regardless of:
- How much the price swept beyond the range
- Whether the sweep bar closed back inside the range
- The size of the sweep relative to ATR

**Proposed fix:**
```python
def detect_sweep_quality(highs, lows, closes, ref_high, ref_low, atr):
    """Returns sweep quality score 0-100 based on: size vs ATR, close-back into range."""
    # ...
```

---

## Setup 4 — Breakout/Pre-Pump (WR 31.8% @ 4H) — DISABLED

### Problem 1: Score is INVERSELY correlated at 4H
**Evidence:** Score <100 → 39.2% WR (4H). Score >150 → 27.9% WR (4H). Higher score = worse 4H performance. This means the scoring model is adding weight to features that hurt 4H performance.

**Root cause analysis:** Reviewing the scoring block (lines 2300–2515):
- ATR compression (+38 for <0.50): Correct signal for ACCUMULATION, but compression alone doesn't tell you WHEN the breakout happens. Over-scoring this creates false urgency.
- Whale buy (+22): Whale buys are sometimes the top of the move (distribution disguised as accumulation).
- EMA bull 1H (+14): This signal fires AFTER the move has started, not before — creating curve-fitting.
- Multiple CHoCH bonuses (+16 for 1H, +14 for 4H): Both firing simultaneously (which they can) adds up to +30 for a single structural event.

**Proposed fix — recalibration:**
```python
# Remove or reduce signals that correlate with ALREADY-STARTED moves:
# EMA bull 1H: reduce from +14 to +7 (price already moved)
# EMA golden cross: reduce from +18 to +10
# price_pos > 0.88 (breakout in progress): reduce from +16 to +8
# vol_ratio > 2.5: reduce from +18 to +12 (volume spike may be the top)

# Keep or increase signals that catch BEFORE the move:
# atr_compression < 0.50: keep +38
# oi_coiling: keep +32
# cvd_div == "strong_bull_div": keep +30
# whale Buy: reduce from +22 to +14 (whales are sometimes distributing)
```

### Problem 2: Missing "setup not started yet" filter
The Pre-Pump setup should ideally fire BEFORE the move. But several scoring conditions (+8 for `price_pos > 0.72`, +16 for `price_pos > 0.88`) reward ALREADY-HAPPENING moves.

**Proposed fix:**
```python
# At the END of s4 scoring, apply a penalty if the move has clearly already started:
if price_pos > 0.80 and vol_ratio > 2.0 and rs_btc is not None and rs_btc > 2.0:
    s4 -= 25  # "FOMO entry" penalty — move already running, late entry
    n4.append("FOMO⚠")
```

---

## Setup 5 — Short Distribution (WR 33.3% @ 4H) — WEAK

### Problem 1: WAIT-classified signals inflate reported WR
**Evidence:** WAIT signals (n=45) show 100% WR at 24H but these are flat-price resolves, not genuine wins. Actual short-only WR is 39.0% (24H), not 43.4%.

**Proposed fix:** The WAIT classification should be removed or reclassified as a separate outcome type. Do not include WAIT in WR calculations.

### Problem 2: Score >150 is inversely correlated (already has a ceiling)
**Current code:** `SETUP_TG_MAX_SCORE["short_dist"] = 150` ✅ Already fixed.

### Problem 3: Direction logic has too much LONG contamination
**Evidence:** 103 LONG signals in a SHORT setup. The setup is designed to find distribution/shorts, but sometimes the same conditions score for a long.

**Root cause:** The 5th setup scores `in_bear_fvg`, `in_bear_ob` for shorts, but positive funding alone (+35 base) can generate a high score in a bullish trending market where shorts are risky.

**Proposed fix:**
```python
# Gate short_dist: require BOTH funding > 0 AND price near top
# Currently: funding > 0.01 → +35 regardless of price position
# Proposed: split the condition
if funding > 0.01:
    if price_pos > 0.60:
        s5 += 35  # full points only at top of range
    elif price_pos > 0.40:
        s5 += 18  # reduced at mid-range
    else:
        s5 += 5   # minimal at bottom — funding is positive but price is at support
```

---

## Cross-Setup Improvements

### 1. Listing age filter needs tightening
**Current code:** `listing_age_days` is computed but only used to print in output. Young coins (<30 days) are much more volatile and prone to manipulation.

**Proposed fix:**
```python
# In _passes_setup_tg_filter() or run_screener() filtering:
if listing_age_days is not None and listing_age_days < 30:
    return False  # Too new, skip
```

### 2. Cross-exchange funding confirmation should be stronger
**Current code:** Binance funding confirmation adds only +5 to score if signs match, +8 if both are extreme. These are small relative to the 100+ thresholds.

**Proposed fix:**
```python
# Increase cross-exchange confirmation weight:
if bnb_fund is not None:
    same_sign = (funding < 0 and bnb_fund < 0) or (funding > 0 and bnb_fund > 0)
    both_extreme = abs(funding) > 0.05 and abs(bnb_fund) > 0.05
    if same_sign:
        cross_conf += 12  # increased from +5
    if both_extreme:
        cross_conf += 18  # increased from +8
    # Add penalty for conflicting signals:
    if not same_sign and abs(funding) > 0.02 and abs(bnb_fund) > 0.02:
        cross_conf -= 15  # conflicting strong signals = reduce confidence
```

### 3. Social sentiment integration is incomplete
**Current code:** `_cp` (CryptoPanic) and `_lc` (LunarCrush) are fetched but not included in scoring (only in notes/display). They require API keys.

**Proposed fix:** Since both require API keys which may not always be configured, add CoinGecko trending as a free-always-available substitute:
```python
# In score_symbol(), use existing is_trending() which is already always available:
if is_trending(symbol):
    # Context-dependent: trending + low price = potential pump catalyst
    if price_pos < 0.40:
        s1 += 10; n1.append("trending!")  # squeeze candidate + hype
    if atr_compression < 0.65:
        s4 += 12; n4.append("trending!")  # pre-pump with hype
```

---

## Summary: Priority Changes

| Setup | Change | Impact | Complexity |
|-------|--------|--------|-----------|
| Squeeze | Mid-score (100–140) hard requirement gate | +3–5pp WR | Low |
| Squeeze | `in_bull_fvg/ob` → require price_pos < 0.40 | Remove false signals | Low |
| BOS/FVG | Add BOS verification function | Better signal quality | Medium |
| BOS/FVG | Add 4H FVG/OB in-zone bonus | +2–3pp WR | Low |
| BOS/FVG | Tag >150 signals as HOLD_24H | Better trade management | Low |
| Breakout | Recalibrate weights (reduce post-move signals) | Fix inverted correlation | High |
| Breakout | Add FOMO entry penalty | Reduce late entries | Low |
| Short Dist | Fix funding scoring vs price position | Remove long contamination | Medium |
| All | Listing age hard filter (<30 days) | Remove manipulation targets | Low |
| All | Increase cross-exchange confirmation weight | Better signal confidence | Low |
| All | Integrate CoinGecko trending into scoring | Free signal boost | Low |

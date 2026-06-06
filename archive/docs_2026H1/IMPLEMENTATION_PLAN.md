# Implementation Plan

**Date:** 2026-04-24  
**Author:** CEO agent (AVEC-13)  
**Status:** PENDING BOARD APPROVAL — do not modify screener.py until approved  
**Dependencies:** RESEARCH.md, IMPROVEMENTS.md, NEW_STRATEGIES.md

---

## Overview

Based on the WR audit (N=2,232 trades) and codebase review, we have three categories of work:
1. **Quick wins** — low-effort changes to existing scoring that directly address identified WR gaps
2. **Data enrichment** — new data sources that improve signal quality across setups
3. **New setups** — entirely new strategies using existing or new data

All changes below are sequenced by priority (impact/effort ratio), with high-impact/low-effort items first.

---

## Priority 1: Quick Wins (no new data sources, 1–4 hours each)

### P1.1 — Squeeze: Mid-score hard requirement gate
**Problem:** Scores 100–140 achieve only 42.0% WR vs 53.6% for <100 scores.  
**File:** `screener.py` — `_passes_setup_tg_filter()` or inline in `run_screener()`  
**Change:** Require at least ONE hard signal when squeeze score is 100–140: extreme funding, CHoCH↑, or real liquidation data (>$300K).  
**Estimated effort:** 1 hour  
**Expected impact:** Remove ~30% of weakest squeeze signals, improve average WR by 3–5pp  
**Dependencies:** None  
**Risk:** Low — filter-only change, doesn't affect signal generation  

### P1.2 — BOS/FVG: Tag >150 signals as HOLD_24H
**Problem:** Score >150 BOS signals achieve 33.6% WR at 4H but 61.1% at 24H.  
**File:** `screener.py` — Telegram alert output section; `telegram_alerts.py`  
**Change:** Add `| HOLD_24H` tag to Telegram message for BOS/FVG signals with score >150. Do NOT suppress — these are the best 24H entries.  
**Estimated effort:** 1 hour  
**Expected impact:** Better trade management for best signals; no signal suppression  
**Dependencies:** None  
**Risk:** Very low  

### P1.3 — Breakout: Add FOMO penalty
**Problem:** High breakout scores (>150) correlate with WORSE 4H performance (27.9% WR vs 39.2% for <100).  
**File:** `screener.py` — s4 scoring block (around line 2515)  
**Change:** Add penalty when move is already running: `if price_pos > 0.80 and vol_ratio > 2.0 and rs_btc > 2.0: s4 -= 25 (FOMO⚠)`  
**Estimated effort:** 30 minutes  
**Expected impact:** Reduce late-entry breakout signals; improve 4H WR for Breakout by 3–5pp  
**Dependencies:** None  
**Risk:** Low  

### P1.4 — Short Dist: Fix funding score vs price position
**Problem:** Positive funding (+35 base) fires even when price is at support, contaminating the signal with 103 unintended LONG signals.  
**File:** `screener.py` — s5 scoring block (around line 2525)  
**Change:** Split funding score by price position (full +35 only at price_pos > 0.60; reduced at mid-range; minimal at bottom).  
**Estimated effort:** 1 hour  
**Expected impact:** Reduce long-contamination in short_dist; cleaner SHORT-only signals  
**Dependencies:** None  
**Risk:** Low  

### P1.5 — All setups: CoinGecko trending in scoring
**Problem:** `is_trending()` is computed but not used in scores.  
**File:** `screener.py` — s1, s4 scoring blocks  
**Change:** If `is_trending(symbol)`: +10 to squeeze at bottom (price_pos < 0.40); +12 to breakout with ATR compression.  
**Estimated effort:** 30 minutes  
**Expected impact:** Free social confirmation layer, no API key required  
**Dependencies:** None  
**Risk:** Very low  

### P1.6 — All setups: Listing age hard filter
**Problem:** Coins <30 days old are manipulation targets; they appear frequently in screener output.  
**File:** `screener.py` — `run_screener()` filtering section  
**Change:** `if listing_age_days is not None and listing_age_days < 30: skip symbol`  
**Estimated effort:** 30 minutes  
**Expected impact:** Reduce manipulation signal noise; higher signal quality  
**Dependencies:** None (listing_age_days already computed)  
**Risk:** Very low  

---

## Priority 2: Scoring Improvements (existing data, 2–8 hours each)

### P2.1 — BOS/FVG: Add actual BOS verification
**Problem:** Setup name says "Break of Structure" but code doesn't verify a structural break.  
**File:** `screener.py` — new function `detect_bos()`, s2 scoring block  
**Change:** Implement `detect_bos(highs, lows, closes, lookback=20)` function that verifies last close broke prior N-period swing high/low. Award +20 for confirmed BOS; penalize -10 when no BOS detected.  
**Estimated effort:** 3 hours (code + testing)  
**Expected impact:** Improve signal precision for BOS/FVG; likely +3–6pp WR  
**Dependencies:** None  
**Risk:** Medium — structural change to primary setup  

### P2.2 — BOS/FVG: Add 4H FVG/OB in-zone bonus
**Problem:** In-zone bonus (+22) only checks 1H zones; 4H zones are structurally more important.  
**File:** `screener.py` — s2 scoring block  
**Change:** Add `in_bull_fvg_4h` and `in_bear_fvg_4h` variables (already feasible from `fvgs_4h`); award +18 when price is in a 4H FVG.  
**Estimated effort:** 1 hour  
**Expected impact:** Strengthen signals at higher-timeframe zones; reduce false entries  
**Dependencies:** P2.1 (can be done independently but works better with BOS verification)  
**Risk:** Low  

### P2.3 — Breakout: Weight recalibration
**Problem:** High-score breakout signals perform WORSE at 4H (score is inversely correlated). Several scoring components reward already-in-progress moves.  
**File:** `screener.py` — s4 scoring block (lines 2300–2515)  
**Change:** Reduce post-move signal weights (EMA structure, price_pos > 0.72/0.88, golden cross). Maintain or increase pre-move signal weights (ATR compression, OI coiling, CVD divergence). See IMPROVEMENTS.md for specific weight changes.  
**Estimated effort:** 4 hours (weight changes + backtesting on historical outcomes)  
**Expected impact:** Fix inverted correlation; improve Breakout WR from 31.8% toward 40%+  
**Dependencies:** P1.3 (FOMO penalty, should be done first as it addresses the same root cause)  
**Risk:** High — requires careful calibration; test with paper trading before enabling TG  

### P2.4 — Squeeze: Conditional in-zone bonus
**Problem:** `in_bull_fvg` (+18) and `in_bull_ob` (+18) fire regardless of price position.  
**File:** `screener.py` — s1 scoring block  
**Change:** Add `and price_pos < 0.40` condition to both bonuses. Add `price_pos < 0.35` condition to bull_div OI divergence (+18 → conditional).  
**Estimated effort:** 30 minutes  
**Expected impact:** Tighter squeeze signals; remove false zone signals at wrong price position  
**Dependencies:** None  
**Risk:** Very low  

### P2.5 — CVD Divergence at Structure: New setup
**Problem:** Existing CVD detection ignores whether divergence is at a structural level.  
**File:** `screener.py` — new function + new scoring block + new Telegram template  
**Change:** Implement `detect_cvd_structural_divergence()` combining existing CVD pct, price change, and zone detection. Add new setup `"cvd_structure"` with scoring and TG output. See NEW_STRATEGIES.md for full spec.  
**Estimated effort:** 6 hours  
**Expected impact:** New signal category with expected 58–65% WR; fills gap in ranging markets  
**Dependencies:** P2.1, P2.2 (zones must be accurate first)  
**Risk:** Medium — new setup, needs 2-week paper trading validation  

---

## Priority 3: Data Enrichment (new API integrations, 2–5 hours each)

### P3.1 — Funding Extreme Trigger Setup (no new data needed)
**Note:** Strategy 2 from NEW_STRATEGIES.md uses only existing data.  
**File:** `screener.py` — new function `check_funding_extreme_trigger()`, called in `run_screener()`  
**Change:** Standalone event-based check (not score-based). When funding ≤ -0.08% + neg_streak ≥ 4 + trigger candle → emit high-priority TG alert regardless of screener schedule.  
**Estimated effort:** 4 hours  
**Expected impact:** New highest-WR signal type (expected 62–72% WR per research); fires rarely (1–5 times per week max)  
**Dependencies:** None (all data already available)  
**Risk:** Low to Medium — requires a separate alert path from normal screening  

### P3.2 — Binance Taker L/S Ratio
**Source:** `https://fapi.binance.com/futures/data/takerlongshortRatio`  
**File:** `binance_bridge.py` — new function `fetch_taker_ls_ratio(symbols)`  
**Data available:** Taker buy/sell volume ratio (5m, 15m, 1H, 4H). Who is the AGGRESSOR.  
**Integration into scoring:**
- If Binance taker buy ratio > 0.60 (buyers are 60%+ of aggressive volume): +10 to squeeze, +8 to breakout
- If Binance taker sell ratio > 0.60: +10 to short_dist, -8 to squeeze
- If Bybit L/S ratio and Binance taker ratio align: +5 cross-confirmation bonus
**Estimated effort:** 3 hours (API + integration + scoring)  
**Expected impact:** Better CVD proxy from larger exchange; reduce false CVD signals  
**Dependencies:** None  
**Risk:** Low  

### P3.3 — Binance Cross-Volume Validation
**Source:** `https://fapi.binance.com/fapi/v1/ticker/24hr` (all symbols in one request)  
**File:** `binance_bridge.py` — new function `fetch_all_24h_stats()`  
**Data available:** 24H volume, 24H price change for all Binance futures symbols  
**Integration into scoring:**
- If vol_ratio > 2.0 on Bybit AND Binance 24H volume is also elevated → confirm volume spike is real
- If vol_ratio > 2.0 on Bybit but Binance volume is flat → possible wash trading → penalize signal
**Estimated effort:** 3 hours  
**Expected impact:** Reduce false volume signals (estimated 15–20% of current volume spikes may be Bybit-specific)  
**Dependencies:** P3.2 (can share the bridge fetch infrastructure)  
**Risk:** Low  

### P3.4 — CoinGecko Spot Volume for Alt Perp/Spot Ratio
**Source:** `https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids={coin_id}`  
**File:** `free_data.py` — new function `get_spot_volumes(symbols)`  
**Data available:** 24H spot volume in USD for each coin  
**Integration into scoring:**
- Compute `perp_spot_ratio = bybit_oi_usd / coingecko_spot_vol_24h`
- Currently only BTC gets this ratio from global market data
- Applying this to each symbol individually allows per-alt perp/spot detection
- High ratio (>5x) means leverage is far exceeding spot demand = bubble risk → penalize breakout, boost short_dist
**Estimated effort:** 4 hours (need Bybit symbol → CoinGecko ID mapping for common alts)  
**Dependencies:** None  
**Risk:** Medium — CoinGecko free tier has 30 req/min limit; need efficient batching  
**Note:** Rate limiting: fetch top 100 symbols by OI only; cache results 15min  

### P3.5 — CoinGlass Free API: Liquidation Aggregates
**Source:** Register free API key at coinglass.com  
**Endpoint:** `/public/v2/liquidation` (hourly liquidation volumes by pair)  
**File:** New `coinglass_bridge.py`  
**Data available:** Aggregated liquidations (long + short) across Bybit + Binance + OKX + Deribit per hour  
**Integration into scoring:**
- Replace/supplement current liquidation scoring (which only captures Bybit websocket data)
- If CoinGlass aggregate liq > $1M: +20 to squeeze; currently only fires if Bybit local DB shows $1M
- Cross-exchange liquidation data gives true market-wide picture
**Estimated effort:** 5 hours  
**Expected impact:** Large improvement to Squeeze setup accuracy; captures 2.5–4x more liquidation events than current local DB  
**Dependencies:** Free API key registration  
**Risk:** Medium — requires maintaining new API integration; needs fallback to local DB on API failures  

---

## Priority 4: New Setup Implementations (after P1–P3)

### P4.1 — VWAP Deviation + OI Divergence Setup
**See:** NEW_STRATEGIES.md — Strategy 1  
**Estimated effort:** 3 hours  
**Dependencies:** P2.1 (BOS verification), basic scoring infrastructure validated  
**Validation:** 2-week paper trading before TG enable  

### P4.2 — Liquidation Cluster Entry Setup (Proxy Mode)
**See:** NEW_STRATEGIES.md — Strategy 3 (proxy mode using equal highs/lows)  
**Estimated effort:** 4 hours  
**Dependencies:** P3.5 (CoinGlass), though proxy mode works without it  
**Validation:** 3-week paper trading (high-risk setup)  

---

## Execution Sequence

```
Week 1 (Quick Wins, no approval risk):
  Day 1:  P1.1 + P1.2 + P1.3 + P1.5 + P1.6
  Day 2:  P1.4 + P2.4 (simple weight adjustments)
  Day 3–5: Monitor WR on new signals; compare to baseline

Week 2 (Scoring Improvements):
  Day 1–2: P2.1 (BOS verification — most impactful)
  Day 2–3: P2.2 (4H zone bonus)
  Day 4–5: P3.1 (Funding Extreme Trigger — no new data needed)

Week 3 (Data Enrichment):
  Day 1–2: P3.2 + P3.3 (Binance enrichment — same module)
  Day 3–4: P3.4 (CoinGecko spot volume)
  Day 5:   P3.5 setup + API key registration

Week 4 (New Setups + Recalibration):
  Day 1–3: P2.3 (Breakout recalibration — needs P1.3 data from prior weeks)
  Day 3–4: P2.5 (CVD at Structure new setup)
  Day 4–5: P4.1 (VWAP Deviation setup)

Week 5+ (Validation):
  P4.2 (Liquidation Cluster — proxy mode)
  Continuous WR monitoring vs baseline
```

---

## Success Metrics

| Metric | Current Baseline | Target |
|--------|-----------------|--------|
| Overall 4H WR | 36.6% | >42% |
| Squeeze 4H WR | 35.8% | >42% |
| BOS/FVG 4H WR | 42.3% | >47% |
| Breakout 4H WR | 31.8% | >38% |
| Signals per day | ~15–25 | 10–20 (fewer, better) |
| False signal rate (score 100–140) | ~30% of total | <20% |

---

## Risks & Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Weight changes break live screener | Medium | Test on historical outcomes CSV before deploying |
| New setup (CVD/VWAP) performs poorly | Medium | 2-week paper trading gate before TG enable |
| CoinGecko rate limits affect screener speed | Medium | Cache aggressively; fetch top-50 symbols by OI only |
| CoinGlass API changes pricing/endpoints | Low | Maintain local DB as fallback |
| BOS verification too strict (misses signals) | Low | Tune lookback parameter; default 20 candles can be reduced to 10 if too strict |

---

## Board Approval Required For

Per the original task specification, **do not modify screener.py** until this plan is board-approved. The following items are lowest risk and can be approved as a batch:

**Batch A (Low risk — approve first):**
- P1.1, P1.2, P1.3, P1.4, P1.5, P1.6 (all Quick Wins)
- P2.4 (conditional in-zone bonus — simple tweak)

**Batch B (Medium risk — approve after Batch A validated):**
- P2.1 (BOS verification)
- P2.2 (4H zone bonus)
- P3.1 (Funding Extreme Trigger)
- P3.2, P3.3 (Binance data — read-only additions)

**Batch C (Higher risk — approve after 2 weeks of Batch B data):**
- P2.3 (Breakout recalibration)
- P2.5 (CVD at Structure new setup)
- P3.4, P3.5 (CoinGecko/CoinGlass integrations)
- P4.1, P4.2 (New full setups)

# Crypto Screener — Win Rate Audit by Setup
**Updated:** 2026-04-24  
**Data:** `outcomes/resolved.csv` — 2,232 resolved trades (2026-04-12 to 2026-04-23)  
**Pending:** 66 signals awaiting resolution  
**Analyst:** CEO

---

## Setup Win Rate Summary

| Setup | Signals | WR 4h | Avg Δ 4h | WR 24h | Avg Δ 24h |
|-------|---------|-------|----------|--------|-----------|
| **BOS/FVG** | 787 | **42.3%** | +0.19% | **51.2%** | −0.23% |
| **Squeeze** | 450 | 35.8% | +1.16% | 46.4% | +0.79% |
| Short Distribution | 435 | 33.3% | +0.15% | 43.4% | +1.91% |
| Breakout | 548 | 31.8% | −0.26% | 42.3% | +1.29% |
| **Range Sweep** | 12 | **25.0%** | −7.65% | 41.7% | −6.85% |
| **Overall** | **2,232** | **36.6%** | — | **46.5%** | — |

---

## Setup-by-Setup Findings

### 1. BOS/FVG — Top Performer

- **Best win rate at both horizons:** 42.3% (4h), 51.2% (24h)
- **Most signals (787):** statistically the most reliable dataset
- **Directions:** LONG=419, SHORT=278 — bidirectional setup works in both directions
- **Score is predictive at 24h:** high-score signals (>150) achieve **61.1% WR** (n=113) — the only setup where high score reliably improves outcomes
- **Score HURTS at 4h:** >150 score gives 33.6% vs 49.6% for low-score (<100) signals. The high-score filter is useful as a 24h entry, not a 4h scalp

**Verdict: Primary setup. Prioritize >150 score signals for 24h holds.**

---

### 2. Squeeze — Best When It Wins

- **Win rate:** 35.8% (4h), 46.4% (24h) — below BOS/FVG
- **All-LONG setup** (450 long, 0 short) — directional bias toward bull runs
- **Massive average win when right:** +5.55% (4h), **+8.77% (24h)**
- **Heavy losses when wrong:** −2.62% (4h), −6.94% (24h) — high volatility, needs tight stops
- **U-shaped score curve:** low (<100) gives 53.6% WR, mid (100–150) gives 42.0%, high (>150) gives 51.0% at 24h. **The middle-score squeeze signals are the weakest.** Skip scores 100–140.
- Only 58 TP1 hits vs 150 at 24h — most wins hit TP1 over time, not immediately

**Verdict: Second-best setup. Filter out mid-score (100–140) signals. Position-size conservatively due to volatility.**

---

### 3. Short Distribution — Directional Confusion

- **Win rate:** 33.3% (4h), 43.4% (24h) — mediocre
- **Mixed direction:** SHORT=287, LONG=103, WAIT=45
- **WAIT signals (n=45) inflate 24h WR to 100%** — these resolve as FLAT, not genuine wins. The real short-only WR is 39.0% (24h), below reported.
- **Score is INVERSELY correlated at 4h:** >150 score → 23.5% WR vs <100 → 40.0% WR. High-score short_dist signals are worse than coin flips.
- High score (>150): avg 24h change positive (+1.91% aggregate) — bad for shorts
- **avg_win shows −2.29% (24h), avg_loss shows +5.49% (24h):** Sign inversion confirms price-change metric doesn't account for short direction — a price drop IS a win for short signals, but the raw % shows negative.

**Verdict: Weak setup. Disable signals with score >150. Re-examine direction logic — WAIT classification may be masking poor signal quality.**

---

### 4. Breakout — Highest Potential, Worst Reliability

- **Lowest WR among active setups:** 31.8% (4h), 42.3% (24h)
- **All-LONG setup** (548 long, 0 short)
- **Explosive when right:** avg win +7.73% (4h), **+12.72% (24h)** — highest avg win of all setups
- **Score is INVERSELY correlated at 4h:** <100 score → 39.2% WR, >150 → **27.9% WR**. Higher score = worse performance. The scoring model is miscalibrated for this setup.
- At 24h, high score (>150) recovers to 46.5% — suggesting these are delayed winners, not failed signals
- 90 stop hits vs 84 TP1 hits (4h): stops firing slightly more than TP1

**Verdict: Underperforming. Score is broken for this setup (inverted). Filter to score <100 for 4h trades. Score >150 can be held for 24h. Needs weight recalibration most urgently.**

---

### 5. Range Sweep — Broken / Insufficient Data

- **Worst performance:** 25.0% WR (4h), −7.65% avg change at 4h
- **Only 12 signals in 12 days** — statistically meaningless (N too small for any conclusion)
- **Catastrophic average loss when not winning:** avg loss −21.89% (4h) suggests catching falling knives or bad entry timing
- Even "wins" show negative average change (−1.39% at 4h) — wins are marginal, losses are catastrophic
- Root cause: sweep events last 1–3 candles. Batch polling (cron schedule) misses most sweeps by the time the screener runs. The setup logic is sound but the detection window is wrong.

**Verdict: Do not act on Range Sweep signals until WebSocket real-time detection is implemented. Disable from Telegram output.**

---

## Rankings

### By Win Rate (4h)
1. **BOS/FVG** — 42.3% ✅
2. Squeeze — 35.8%
3. Short Dist — 33.3%
4. Breakout — 31.8%
5. **Range Sweep — 25.0%** ❌

### By Win Rate (24h)
1. **BOS/FVG** — 51.2% ✅
2. Squeeze — 46.4%
3. Short Dist — 43.4%
4. Breakout — 42.3%
5. Range Sweep — 41.7%

### By Expected Value (Avg Δ 24h)
1. Short Distribution — +1.91% *(inflated by WAIT/FLAT signals)*
2. Breakout — +1.29%
3. Squeeze — +0.79%
4. BOS/FVG — −0.23%
5. Range Sweep — −6.85% ❌

---

## Score Quality Assessment

The scoring model is **partially broken**:

| Setup | Score Effect (4h) | Score Effect (24h) |
|-------|------------------|-------------------|
| BOS/FVG | Inverse (high = worse) | **Positive (high = 61.1%)** ✅ |
| Squeeze | U-shaped (mid = worst) | U-shaped |
| Short Dist | **Strongly inverse** (high = 23.5%) ❌ | Mildly inverse |
| Breakout | **Strongly inverse** (high = 27.9%) ❌ | Slight positive recovery |
| Range Sweep | N/A (too few signals) | N/A |

**Key finding:** Score is only a reliable positive predictor for BOS/FVG at 24h. For Breakout and Short Distribution, higher score actively predicts worse 4h performance. The scoring model needs per-setup recalibration.

---

## Recommended Actions (Priority Order)

1. **Disable Range Sweep from Telegram** — worst WR, catastrophic avg loss, too few signals for any edge
2. **Filter Breakout to score <100 for 4h alerts** — only range where WR is above 35%
3. **Filter Short Distribution to score <150** — above 150 is below coin-flip at 4h
4. **For BOS/FVG, add score >150 filter for 24h hold recommendations** — 61.1% WR is real edge
5. **Skip Squeeze signals in 100–140 score range** — worst tier for this setup; low and high scores both outperform
6. **Recalibrate scoring weights using logistic regression on resolved.csv** — data exists (2,232 trades), the `load_score_weights()` hook exists, this is now tractable
7. **Implement WebSocket sweep detection** — Range Sweep hypothesis is structurally sound, but cron polling misses the event window

---

## Previous Architecture Analysis (2026-04-23)

The analysis below from the previous session identified architectural weaknesses that remain valid. Updated data (N=2,232) confirms the scoring inversion finding and increases confidence in the setup rankings.

---

### What the System Does Well

- Solid Bybit V5 integration with retry logic and parallel per-symbol fetching
- Comprehensive signal library: FVG, Order Block, BOS/CHoCH, ATR compression, OI velocity, CVD divergence, whale detection, DOM, absorption
- Working outcome tracker: 2,232 trades resolved with 4h and 24h WR tracking
- Multi-timeframe confluence (MTF) framework across 1H, 4H, Daily
- Quality filtering: $50M+ turnover gate, 50% move exclusion, 8h cooldown, HARD_BLOCK hours
- Macro context layer: ETF flows, ForexFactory, Deribit options, CoinGecko, CryptoPanic, LunarCrush

---

### Priority 1 — Stop the Bleeding (This Week)

**Task 1.1: Disable or quarantine Range Sweep and high-score Breakout signals**  
- Range Sweep: disable entirely (n=12, WR=25%, avg loss −21.89%)
- Breakout: raise minimum score to 150+ for 24h, filter to <100 for 4h
- Short Distribution: raise score ceiling to 150 (above that is worse than random)
- Owner: CTO | Estimate: 1 day

**Task 1.2: Boost CHoCH↑_1H as a primary scoring signal**  
- CHoCH is underweighted. Add `if choch_1h == "bull_choch"` scoring block to BOS/FVG and Squeeze
- Owner: CTO | Estimate: 2 hours

---

### Priority 2 — Score Recalibration (Next 2 Weeks)

**Task 2.1: Train logistic regression on resolved.csv signal flags**  
- Extract binary features from each signal flag
- Run logistic regression with 4h WR as target
- Replace hand-tuned additive scoring with dot-product of [signal_flags] × [learned_coefficients]
- The `load_score_weights()` hook is the right entry point
- Owner: CTO | Estimate: 3–5 days

**Task 2.2: Per-signal WR dashboard**  
- Extend weekly_report output to show per-signal WR vs baseline
- Expose via web_dashboard.py
- Owner: CTO | Estimate: 2 days

---

### Priority 3 — Real-Time Sweep (Next Month)

**Task 3.1: WebSocket-triggered screener for sweep detection**  
- Subscribe to Bybit public kline WebSocket for top-50 symbols
- Trigger full symbol analysis when sweep condition fires on candle close
- Reuse WebSocket infrastructure from liquidation_tracker.py
- Owner: CTO | Estimate: 1 week

---

### Priority 4 — Code Architecture (Next Quarter)

**Task 4.1:** Split screener.py monolith into `signals/`, `scoring/`, `data/`, `output/` modules  
**Task 4.2:** Unit tests for detect_sweep, detect_fvg, detect_order_block, detect_choch  
**Task 4.3:** Move GOOD_SIGNAL_HOURS, HARD_BLOCK_HOURS, MIN_TURNOVER_24H to config.py  

---

## Bottom Line

**BOS/FVG is the only setup with statistically reliable edge** (51.2% WR at 24h, 787 signals, score-predictive above 150). Every other setup has a critical flaw: Range Sweep is broken by architecture, Breakout has inverted score correlation, Short Distribution has directional measurement issues, and Squeeze is viable but volatile.

Fix scoring for Breakout and Short Distribution via logistic regression. Build WebSocket detection for Range Sweep. BOS/FVG is the foundation to build on.

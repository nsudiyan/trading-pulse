# TRADE_ANALYSIS.md
*Generated: 2026-04-25 | Dataset: outcomes/resolved.csv | 2,289 signals | 2026-04-12 → 2026-04-24*

---

## 1. Executive Summary

| Metric | Value |
|--------|-------|
| Total signals | 2,289 |
| Actionable (LONG + SHORT) | 2,150 |
| ЖДАТЬ (WAIT) | 139 |
| 24H Win Rate (actionable) | **47.3%** |
| 24H Win Rate (ЖДАТЬ) | **82.7%** |
| Best setup | `bos_fvg` — 55.0% WR |
| Worst setup | `breakout` — 45.9% WR |
| Best hour (UTC) | 10:00 — 64.5% WR |
| Worst hour (UTC) | 18:00 — 19.1% WR |
| Score correlation with WR | Weak / nonlinear — highest scores underperform |

**Critical finding:** Score is NOT a reliable predictor of outcome. The top decile (score 171–221) has the *lowest* WR at 45.1%, while mid-range scores (127–155) outperform. The grade field is unused (all `—`). The `bos_fvg` setup dominates on both WR and volume.

---

## 2. Dataset Overview

```
Timeframe: 2026-04-12 to 2026-04-24 (12 trading days)
Signal frequency: 100–406 signals/day (mean ~190)
Outcome resolution: 4H and 24H windows
Outcome labels: WIN, TP1 (good), LOSS, STOP (bad), FLAT (neutral)
```

### 2.1 Outcome Distribution (24H)

| Outcome | Count | % |
|---------|-------|---|
| TP1     | 806   | 35.2% |
| STOP    | 756   | 33.0% |
| LOSS    | 321   | 14.0% |
| FLAT    | 152   | 6.6% |
| WIN     | 254   | 11.1% |

**Note:** STOP (33%) dominates losses — wide ATR stops are getting triggered frequently, suggesting many setups are entering into unfavorable volatility. WIN (11.1%) vs TP1 (35.2%) means most wins are from TP1 hits rather than just directional moves.

---

## 3. Win Rate by Setup

| Setup | n | Wins | Losses | Flats | WR (excl. FLAT) | Avg Score |
|-------|---|------|--------|-------|-----------------|-----------|
| `bos_fvg` | 806 | 411 | 336 | 59 | **55.0%** | 124.4 |
| `squeeze` | 483 | 221 | 229 | 33 | 49.1% | 127.9 |
| `range_sweep` | 12 | 5 | 6 | 1 | 45.5% | 107.3 |
| `breakout` | 553 | 234 | 276 | 43 | 45.9% | 131.0 |
| `short_dist` | 435 | 189 | 230 | 16 | **45.1%** | 145.0 |

### 3.1 Setup × Score Breakpoint (score ≥120 vs <120)

| Setup | score<120 WR | score≥120 WR | Delta |
|-------|-------------|-------------|-------|
| `bos_fvg` | 52.3% | **57.1%** | +4.8pp |
| `squeeze` | 52.1% | 46.9% | -5.2pp |
| `breakout` | 44.1% | 46.9% | +2.8pp |
| `range_sweep` | 57.1% | 25.0% | **-32.1pp** |
| `short_dist` | 46.1% | 44.8% | -1.3pp |

**Key insight:** Higher score helps `bos_fvg` but *hurts* `squeeze`, `range_sweep`, and `short_dist`. A single universal score threshold is not valid — setup-specific thresholds are required.

### 3.2 4H vs 24H Divergence

| Setup | 4H WR | 24H WR | Divergence |
|-------|-------|--------|------------|
| `bos_fvg` | 54.4% | 55.0% | ~flat |
| `squeeze` | 53.7% | 49.1% | -4.6pp (reverses in 24H) |
| `short_dist` | 53.9% | 45.1% | **-8.8pp** (degrades badly) |
| `breakout` | 41.3% | 45.9% | +4.6pp (improves) |
| `range_sweep` | 42.9% | 45.5% | +2.6pp |

**Critical:** `short_dist` looks good at 4H (53.9%) but decays to 45.1% at 24H — the edge expires quickly. Trade only with tight 4H exits if using `short_dist`.

---

## 4. Score Analysis

### 4.1 Score Bracket Win Rates (24H, excl. FLAT)

| Score Range | n | WR |
|------------|---|-----|
| 45–80      | 55  | 43.4% |
| 80–100     | 263 | **53.6%** |
| 100–120    | 564 | 48.5% |
| 120–140    | 605 | 49.5% |
| 140–160    | 425 | **52.2%** |
| 160–180    | 221 | 46.2% |
| 180+       | 156 | 47.4% |

### 4.2 Score Decile Analysis (10th percentile buckets)

| Decile | Score Range | WR |
|--------|------------|-----|
| D1 | 45–94   | 48.8% |
| D2 | 94–105  | 49.8% |
| D3 | 105–113 | 50.5% |
| D4 | 113–121 | 48.3% |
| D5 | 121–127 | 51.4% |
| D6 | 127–134 | 47.6% |
| D7 | 134–143 | 51.6% |
| D8 | 143–155 | 51.6% |
| D9 | 155–171 | 50.5% |
| D10 | 171–221 | **45.1%** ← worst |

**Conclusion:** Score ≥120 is NOT anticorrelated in a simple linear sense, but the highest scores (160+, especially 171+) do *underperform* the mean. The score model is poorly calibrated at the top end — many high-score signals are inflated `breakout` or `short_dist` setups which have subpar underlying WR.

---

## 5. Direction Analysis

| Direction | n | 24H WR |
|-----------|---|--------|
| **ЖДАТЬ** | 139 | **82.7%** |
| ЛОНГ | 1,576 | 47.7% |
| ШОРТ | 574 | 46.2% |

ЖДАТЬ signals are massively underexploited. With 82.7% WR and 139 occurrences, these represent the highest-quality setups in the dataset by a wide margin. These should be entered aggressively as soon as the wait condition resolves.

---

## 6. Time-of-Day Analysis (UTC)

### 6.1 Win Rates by Hour

| Hour (UTC) | n | WR | Signal |
|------------|---|-----|--------|
| 05:00 | 256 | 57.2% | ✅ Strong |
| 09:00 | 327 | 59.7% | ✅ Strong |
| 10:00 | 71  | **64.5%** | ✅ Best |
| 20:00 | 168 | 58.1% | ✅ Strong |
| 12:00 | 100 | 42.2% | ⚠️ Weak |
| 14:00 | 80  | 38.7% | ⚠️ Weak |
| 22:00 | 158 | 32.8% | ❌ Avoid |
| 17:00 | 121 | 36.8% | ❌ Avoid |
| **18:00** | 50  | **19.1%** | ❌ Critical Avoid |
| **19:00** | 42  | **21.4%** | ❌ Critical Avoid |

**Action:** Block signal generation (or raise threshold to 200+) for UTC 17:00–19:00 and 22:00. These windows likely correspond to NY open volatility and low-liquidity Asian session transitions where sweep-based setups get violated.

---

## 7. Signal Feature Win Rates

| Feature | ON (n, WR) | OFF (n, WR) | Delta |
|---------|-----------|------------|-------|
| `choch_bull_1h=1` | 64, **53.2%** | 2086, 47.1% | **+6.1pp** |
| `ema_bull_1h=1` | 844, 48.6% | 1306, 46.5% | +2.1pp |
| `mtf_bull=5` (max) | 1097, 46.9% | 1053, 47.7% | -0.8pp (neutral) |
| `mtf_bear=1` | 219, **37.9%** | 788, 47.8% | **-9.9pp** ← strong negative |
| `ema_bull_4h=1` | 1096, 46.5% | 1054, 48.1% | -1.6pp |

### 7.1 CHoCH↑1H Deep Dive

| Setup + CHoCH | n | WR |
|---------------|---|-----|
| `bos_fvg` + CHoCH=1 | 10 | **77.8%** |
| `breakout` + CHoCH=1 | 34 | **60.6%** |
| `bos_fvg` (no CHoCH) | 702 | 51.9% |
| `squeeze` (no CHoCH) | 477 | 49.5% |
| `breakout` (no CHoCH) | 519 | 44.9% |
| `short_dist` + CHoCH=1 | 14 | 35.7% |
| `short_dist` (no CHoCH) | 376 | **38.6%** ← worst combo |

CHoCH↑1H is a strong positive signal for `bos_fvg` (+26pp vs baseline) and `breakout` (+15.7pp). It has *no benefit* or is negative for `short_dist` — likely because CHoCH in a short-distance distribution context creates conflicting structure.

### 7.2 Other Market Context Features

| Feature | Value | n | WR |
|---------|-------|---|-----|
| CVD kline | >0 | 1054 | 48.9% |
| CVD kline | ≤0 | 1096 | 45.8% |
| Funding LONG | tailwind (<0) | 639 | 48.2% |
| Funding LONG | headwind (≥0) | 937 | 47.4% |
| OI 24H | rising | 1100 | 47.7% |
| OI 24H | falling | 1050 | 46.9% |
| \|VWAP dev\| | >5 | 448 | 46.1% |
| \|VWAP dev\| | ≤5 | 1702 | 47.6% |
| `mtf_bear=1` | any | 219 | **37.9%** |

`mtf_bear=1` is the single strongest negative feature: -9.9pp WR. This likely means entering longs when the bearish 4H structure is confirmed is a significant drag. Consider blocking LONG signals when `mtf_bear` is active.

---

## 8. P&L and R:R Analysis

### 8.1 Overall P&L (24H, % price change)

| Cohort | Avg Change |
|--------|-----------|
| All signals | +0.68% |
| Winners | +4.82% |
| Losers | -3.31% |

Median change is -0.16% — the system has slight negative skew when all signals included.

### 8.2 Implied R:R by Setup

| Setup | Avg Win % | Avg Loss % | Implied R:R |
|-------|-----------|-----------|-------------|
| `breakout` | +12.66% | -8.27% | **1.53** |
| `squeeze` | +8.41% | -6.86% | **1.23** |
| `bos_fvg` | +1.76% | -6.87% | 0.26 |
| `short_dist` | -2.29% | -8.03% | **-0.29** ← broken |
| `range_sweep` | -0.68% | -18.05% | **-0.04** ← broken |

**Critical:** `short_dist` and `range_sweep` have negative implied R:R — winners lose money on average while losers lose more. This is likely because STOP outcomes inflate "loss" avg change. Still, these setups need immediate review — they're not delivering mean reversion.

`bos_fvg` has strong WR (55%) but low R:R (0.26): wins are small, losses are large. Breakout and squeeze have better payout profiles.

---

## 9. High-Conviction Combinations

### 9.1 Best Signal Combos (24H)

| Combo | n | WR |
|-------|---|-----|
| `bos_fvg` + CHoCH↑1H | 10 | **77.8%** |
| `breakout` + CHoCH↑1H | 34 | **60.6%** |
| score≥140 + CHoCH↑1H | 34 | **57.6%** |
| `bos_fvg` (any score) | 806 | 55.0% |
| `squeeze` + no CHoCH | 477 | 49.5% |

### 9.2 Worst Signal Combos (24H)

| Combo | n | WR |
|-------|---|-----|
| `short_dist` + no CHoCH | 376 | **38.6%** |
| `short_dist` + CHoCH↑1H | 14 | 35.7% |
| Any signal at UTC 18–19 | 92 | ~20% |
| Any signal with `mtf_bear=1` | 219 | **37.9%** |

---

## 10. Performance by Date

| Date | n | 24H WR |
|------|---|--------|
| 2026-04-12 | 120 | 68.6% |
| 2026-04-15 | 295 | 70.3% |
| 2026-04-16 | 147 | 73.5% |
| 2026-04-13 | 280 | 40.7% |
| 2026-04-14 | 347 | 40.7% |
| 2026-04-18 | 100 | **26.8%** |
| 2026-04-22 | 138 | **28.9%** |
| 2026-04-24 | 7   | 14.3% (small n) |

Day-to-day swings are large (27%–73%) suggesting market regime matters more than signal quality. The system has no regime filter — adding a market-wide trend/volatility filter would reduce exposure on bad days.

---

## 11. Notable Symbols

### Best Performers (min 5 trades, 24H)

| Symbol | n | WR |
|--------|---|-----|
| LDOUSDT | 14 | 100.0% |
| PROMUSDT | 6 | 100.0% |
| DRIFTUSDT | 9 | 88.9% |
| XAGUSDT | 23 | 87.0% |
| ENJUSDT | 35 | **85.7%** |

### Worst Performers (min 5 trades, 24H)

| Symbol | n | WR |
|--------|---|-----|
| WETUSDT | 15 | 0.0% |
| METUSDT | 10 | 0.0% |
| API3USDT | 9 | 0.0% |
| APRUSDT | 8 | 0.0% |
| INXUSDT | 8 | 0.0% |

Several symbols have 0% WR across 8–15 trades — statistically significant enough to blacklist. WETUSDT (n=15, 0%) and METUSDT (n=10, 0%) should be added to an exclusion list immediately.

---

## 12. Recommendations

### 12.1 Immediate Actions (Score Threshold Adjustments)

| Setup | Current Behavior | Recommended Action |
|-------|-----------------|-------------------|
| `breakout` | Any score alerts | Raise Telegram threshold to **145** |
| `range_sweep` | Any score alerts | Raise Telegram threshold to **160** |
| `short_dist` | Alerts at any score | Raise Telegram threshold to **160** |
| `bos_fvg` | Standard | Keep at **110** — already best setup |
| `squeeze` | Standard | Raise threshold to **120** |

### 12.2 Signal Boosting (Scoring Weight Increases)

1. **CHoCH↑1H on bos_fvg** (+6.1pp average, +26pp on bos_fvg): Add +20 pts to score when `choch_bull_1h=1` AND setup is `bos_fvg`
2. **CHoCH↑1H on breakout** (+15.7pp): Add +15 pts to score when `choch_bull_1h=1` AND setup is `breakout`
3. **ЖДАТЬ signals**: Flag as tier-1 alert with immediate entry instruction rather than "wait" label

### 12.3 Signal Suppression

1. **Block signals at UTC 17–19**: Zero generation or require score ≥180 to alert
2. **Block signals at UTC 22**: Require score ≥160 to alert
3. **Block LONG when `mtf_bear=1`**: -9.9pp WR, clearly conflicting structure
4. **Blacklist symbols**: Add WETUSDT, METUSDT, API3USDT, APRUSDT to `SYMBOL_BLACKLIST`

### 12.4 R:R Improvements

- `bos_fvg` has good WR but 0.26 R:R — TP1 targets are too tight. Extend TP2 as primary target for bos_fvg trades.
- `short_dist` and `range_sweep` have broken R:R — stops are too wide or targets too close. Re-evaluate ATR multipliers for these setups.

### 12.5 Regime Filter

Implement a daily market regime gate based on BTC 4H trend:
- Bear days (BTC -3% from 4H EMA): Suppress all LONG signals regardless of score
- High volatility days (BTC ATR > 2× 20-day avg): Reduce all scores by 20 pts

---

## 13. Score Model Recalibration Plan

Based on this dataset, logistic regression weights should reflect:

| Feature | Direction | Estimated Coeff |
|---------|-----------|----------------|
| `setup = bos_fvg` | + | high |
| `choch_bull_1h = 1` | + | high |
| `setup = short_dist` | - | moderate |
| `mtf_bear = 1` | - | high |
| `hour in [18,19]` | - | very high |
| `cvd_kline > 0` | + | low |
| `pump_score >= 100` | + | low |
| `score_raw > 160` (as-is) | - | moderate (currently over-weighted) |

The calibration module (`calibration/train_model.py`) should be trained on this dataset to replace the hand-tuned scoring. See `calibration/model_report.md` for prior model output.

---

## 14. Summary Scorecard

| Dimension | Status | Priority Fix |
|-----------|--------|-------------|
| `bos_fvg` setup | ✅ Good (55% WR) | Reduce threshold to maximize volume |
| `breakout` setup | ⚠️ Marginal (45.9%) | Raise to 145 |
| `short_dist` setup | ❌ Below baseline (45.1%) | Raise to 160, review ATR |
| `range_sweep` setup | ❌ Broken R:R | Raise to 160 or disable |
| CHoCH↑1H signal | ✅ Strong (+6.1pp) | Add to scoring weights |
| Score at 160+ | ❌ Anticorrelated | Recalibrate model |
| ЖДАТЬ signals | ✅ Excellent (82.7%) | Convert to immediate alerts |
| UTC 17–19 window | ❌ Critical avoid | Time filter |
| `mtf_bear=1` on LONG | ❌ -9.9pp WR | Block rule |
| Regime filter | ❌ Missing | BTC 4H trend gate |

---

*Analysis based on 2,289 resolved trades from 2026-04-12 to 2026-04-24.*
*Next: train logistic regression on this dataset to replace hand-tuned scoring (see ANALYSIS.md mandate item 1).*

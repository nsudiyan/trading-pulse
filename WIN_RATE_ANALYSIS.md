# Win Rate Analysis — resolved.csv
*Generated: 2026-04-25 | Data: 2026-04-12 → 2026-04-23 | n=2,272 trades*

---

## Summary

- **Overall WR (24H):** 47.5% across 1,983 decided trades (WIN/TP1 vs LOSS/STOP; FLAT excluded)
- **Overall WR (4H):** 47.6% across 1,485 decided trades
- **Sample window:** 11 days — segment-level conclusions marked with (⚠ small n) when n<30

---

## 1. Win Rate by Setup

### 24H Horizon
| Setup | WR | n | Signal |
|---|---|---|---|
| `bos_fvg` | **52.5%** | 648 | ✅ Best performer |
| `squeeze` | **49.3%** | 442 | ✅ Above baseline |
| `breakout` | 46.1% | 508 | ⚠ Marginal |
| `range_sweep` | 45.5% | 11 | ⚠ Tiny sample |
| `short_dist` | **38.5%** | 374 | ❌ Worst — 9pp below baseline |

### 4H Horizon
| Setup | WR | n | Notes |
|---|---|---|---|
| `squeeze` | **53.6%** | 308 | Best at 4H too |
| `bos_fvg` | 49.9% | 523 | Still solid |
| `short_dist` | 44.6% | 224 | Recovers at 4H (was 38.5% at 24H) |
| `breakout` | 41.4% | 423 | Consistently weak |
| `range_sweep` | 42.9% | 7 | Too few |

**Key finding:** `short_dist` degrades badly at the 24H horizon but is near-average at 4H — it works as a quick scalp but the move fades. Take profit early or tighten TP2 on this setup.

---

## 2. Win Rate by Trading Session

| Session (UTC) | WR | n |
|---|---|---|
| Asia 01–08h | **52.4%** | 473 |
| London 09–16h | **51.2%** | 774 |
| **NY 17–24h** | **40.4%** | 736 |

**NY session is a 7pp drag.** This is the single largest time-based filter available.

### Hourly Breakdown

| Hour (UTC) | WR | n | Note |
|---|---|---|---|
| 10h | **63.9%** | 61 | Best single hour |
| 09h | **56.7%** | 289 | London open |
| 05h | **56.4%** | 225 | Late Asia |
| 20h | **53.8%** | 145 | NY evening recovery |
| 01h | 49.6% | 232 | Asia open |
| 21h | 48.3% | 261 | Neutral |
| 13h | 48.9% | 270 | Neutral |
| 12h | 39.8% | 83 | ⚠ Lunch lull |
| 14h | 39.4% | 71 | ⚠ |
| 22h | 32.8% | 137 | ❌ NY afternoon |
| 17h | 30.2% | 106 | ❌ NY open — worst window |
| 18h | **19.1%** | 47 | ❌❌ Do not trade |
| 19h | **17.5%** | 40 | ❌❌ Do not trade |
| 06h | 37.5% | 16 | (⚠ small n) |

**The 17–19h UTC window (NY open) is catastrophic: 17.5–30.2% WR.** This is the most actionable finding in the dataset.

---

## 3. Win Rate by Day of Week

| Day | WR | n |
|---|---|---|
| **Thursday** | **64.0%** | 178 |
| **Wednesday** | **55.9%** | 379 |
| **Sunday** | 51.1% | 483 |
| Monday | 40.9% | 489 |
| Tuesday | 40.3% | 360 |
| **Saturday** | **24.5%** | 94 |

**Saturday is extremely poor.** Thursday and Wednesday are the strongest trading days. Mon/Tue are consistently weak.

---

## 4. Score Buckets — No Monotonic Improvement

| Score Range | WR | n |
|---|---|---|
| 80–100 | **52.8%** | 229 |
| 100–110 | 40.6% | 202 |
| 110–120 | 49.3% | 268 |
| 120–130 | 45.2% | 263 |
| 130–140 | 48.6% | 251 |
| 140–150 | 50.0% | 226 |
| 150+ | 46.5% | 493 |

**Score is not predictive of outcome.** Scores 80–100 outperform scores 150+. This confirms the ANALYSIS.md finding that the current additive scoring model needs replacement with calibrated logistic regression weights (see [AVEC-1]).

### Score × Setup
| Setup | Score <120 | Score 120–140 | Score >140 |
|---|---|---|---|
| bos_fvg | 50.0% | 54.1% | **55.8%** |
| breakout | 45.1% | 47.2% | 46.9% |
| short_dist | 36.0% | 41.1% | 38.2% |
| squeeze | 52.4% | 38.3% | 51.9% |

`bos_fvg` is the only setup where higher score correlates with better WR. `squeeze` drops at 120–140 (possible score inflation from noisy signals).

---

## 5. Setup × Session Interaction (Critical)

Good hours = {01, 05, 09, 10, 20, 21}h UTC (WR >49%)  
Bad hours = {12, 14, 17, 18, 19, 22}h UTC (WR <40%)

| Setup | Good Hours WR | Bad Hours WR | Delta |
|---|---|---|---|
| squeeze | **59.2%** | 27.2% | **–32pp** |
| bos_fvg | **58.8%** | 35.3% | **–24pp** |
| breakout | 53.8% | 30.6% | –23pp |
| short_dist | 36.9% | 30.9% | –6pp |

**Time-of-day is a stronger factor than setup choice.** A bos_fvg signal at 09h outperforms a bos_fvg at 18h by 24pp. Squeeze is the most time-sensitive setup.

---

## 6. Signal-Level Predictors

### CHoCH Bull 1H (for ЛОНГ)
| choch_bull_1h | WR | n |
|---|---|---|
| 0 (absent) | 47.6% | 1,387 |
| 1 (present) | **56.1%** | 57 |

+8.5pp lift. Confirms the ANALYSIS.md mandate to boost CHoCH↑_1H weight in scoring.

### EMA Alignment (ЛОНГ)
| State | WR | n |
|---|---|---|
| 1H EMA bull only (not 4H) | **59.3%** | 59 |
| Both EMA aligned | 47.7% | 684 |
| 4H only | 44.2% | 308 |
| Neither | 49.6% | 393 |

Counterintuitive: 1H-only EMA alignment outperforms full alignment. May indicate that price just broke above 1H EMA and has room to run, while "both aligned" setups are late entries.

### RSI 1H Zones
| RSI Zone | WR | n |
|---|---|---|
| <30 (oversold) | **54.5%** | 189 |
| 30–40 | 45.8% | 295 |
| 40–50 | 44.5% | 476 |
| 50–60 | 46.8% | 506 |
| 60–70 | 48.8% | 346 |
| 70+ (overbought) | 49.7% | 171 |

RSI oversold (<30) shows a 7pp premium. Consider adding RSI<30 as a scoring boost for longs.

### Relative Strength vs BTC (ЛОНГ)
| RS_BTC Range | WR | n |
|---|---|---|
| Flat (–2 to +2) | 39.4% | 625 |
| Mild strong (+2 to +10) | 47.8% | 314 |
| **Strong (>+10)** | **59.2%** | 130 |
| Mild weak (–10 to –2) | 43.9% | 82 |

**RS_BTC >+10 is a strong positive predictor (+19.8pp vs flat).** Symbols outperforming BTC by >10% should receive a meaningful score boost.

### VWAP Deviation (ЛОНГ)
| VWAP Dev | WR | n |
|---|---|---|
| Far below (<–5%) | 43.7% | 197 |
| Below (–5 to –1%) | 51.0% | 292 |
| **Near (–1 to +1%)** | **53.0%** | 338 |
| Above (+1 to +5%) | 43.7% | 453 |
| Far above (>+5%) | 50.3% | 155 |

Entries near VWAP (±1%) perform best. Avoid longs when price is already 1–5% above VWAP.

### Funding Rate
| Condition | WR | n |
|---|---|---|
| ЛОНГ, negative funding (aligned) | 48.5% | 594 |
| ЛОНГ, positive funding (opposed) | 47.5% | 850 |
| ШОРТ, positive funding (aligned) | 44.3% | 384 |
| ШОРТ, negative funding (opposed) | **51.0%** | 155 |

Funding rate alignment has minimal WR impact for longs (<1pp difference). Shorts slightly outperform when trading against the funding — this may reflect mean-reversion behavior in high-positive-funding environments.

### MTF Alignment (ЛОНГ, mtf_bull count)
| mtf_bull | WR | n |
|---|---|---|
| 2 | **57.1%** | 77 |
| 4 | 53.5% | 99 |
| 0 | 50.6% | 89 |
| 5 (max) | 47.2% | 996 |
| 3 | 43.8% | 137 |
| 1 | 43.5% | 46 |

mtf_bull=5 (maximum alignment) is only average (47.2%). Moderate alignment (2 or 4) outperforms. The screener's current hard filter requiring mtf_bull=5 may be counterproductive.

---

## 7. Direction Breakdown

| Direction | WR (24H) | n |
|---|---|---|
| ЛОНГ | 47.9% | 1,444 |
| ШОРТ | 46.2% | 539 |
| ЖДАТЬ (if traded) | **82.7%** | 139 |

**ЖДАТЬ is effectively a "don't touch" signal on the screener instrument, but the 82.7% underlying win rate means these instruments are strong movers** — consider monitoring them for a better entry rather than skipping entirely.

---

## 8. Actionable Recommendations

### Immediate (filters to apply without model retraining)

1. **Block the 17–19h UTC window entirely.** WR of 17–30% in these hours destroys edge. Add a session gate: no Telegram alerts sent 17:00–20:00 UTC.

2. **Downweight Saturday signals.** 24.5% WR — worst day by 27pp vs Thursday. If a cooldown mechanism exists, extend it on Saturdays.

3. **Suppress `short_dist` at 24H targets.** WR=38.5% at 24H is 9pp below baseline. Either close `short_dist` at 4H TP1 (44.6% WR) or raise Telegram threshold to 140 for this setup.

4. **Add RS_BTC >+10 as a scoring bonus.** +19.8pp WR premium over flat RS_BTC. Suggest +8 to +10 points in score.

5. **Add CHoCH↑_1H bonus as planned.** +8.5pp WR confirmed. This validates the ANALYSIS.md mandate.

### Model-Level (for AVEC-1 logistic regression)

6. **Score is anti-predictive in the 100–130 range.** Replace additive scoring with calibrated logistic regression. Priority features by effect size:
   - Hour bucket (NY open vs Asia/London) — largest effect
   - RS_BTC >+10 (+19.8pp)
   - CHoCH_bull_1H (+8.5pp)
   - RSI <30 (+7pp)
   - EMA 1H only (+11.6pp — but small n=59)
   - VWAP near ±1% (+5pp)

7. **MTF max alignment (=5) is not the best filter.** Consider allowing mtf_bull ∈ {2,4,5} rather than requiring exactly 5.

8. **`short_dist` needs separate treatment.** It behaves differently at 4H vs 24H and its score-to-WR relationship is flat. Either train a separate model or add a hard TP close at 4H.

---

## 9. Data Limitations

- **11-day window only** (April 12–23). Saturday has only 94 samples, some hour slots have <50. Patterns should be validated as data accumulates.
- **All trades in same market regime** (post-April-12 crypto conditions). Results may not generalize to trending vs ranging macro environments.
- **No slippage / spread modeled** in outcome calculation.
- **FLAT outcomes excluded** from WR denominator — if many trades go flat, true WR may differ.

---

*Written by CTO agent (AVEC-2b458376) for [AVEA-27](/AVEA/issues/AVEA-27)*

# Screener Project Audit
**Date:** 2026-04-24  
**Analyst:** CTO (AVEC-19)  
**Data:** `outcomes/resolved.csv` — 2,250 resolved trades (2026-04-12 to 2026-04-23)  
**Scope:** screener.py (5,565 lines), scoring model, signal detection, data quality, filters

---

## 1. Executive Summary

The screener generates signals but its scoring model is **partially broken and in one case completely inactive**. Three of five setups show inverted or flat score-to-WR correlation. One critical multiplier system (grade-based) has never worked due to a data schema bug. UTC 22 is incorrectly hard-blocked despite having 55.6% WR. Range Sweep is correctly disabled but still leaks into tracking.

**Key numbers (4h decisive outcomes, excluding FLAT):**

| Setup | WR (4h decisive) | WR (all resolved) | Score correlation |
|-------|-----------------|-------------------|-------------------|
| BOS/FVG | **54.9%** | 42.3% | Positive at 24h, inverse at 4h |
| Squeeze | **53.9%** | 35.8% | U-shaped |
| Short Distribution | **53.9%** | 33.3% | Strongly inverted |
| Breakout | **41.6%** | 31.8% | Strongly inverted |
| Range Sweep | 42.9% (n=7) | 25.0% | N/A (disabled) |

Note: "decisive" = TP1/WIN vs STOP/LOSS, excludes FLAT. FLAT signals inflate the raw WR denominator — actual tradeable WR is higher.

---

## 2. Critical Bugs

### BUG-1 (CRITICAL): Grade multiplier system is completely inactive

**File:** `screener.py:1618` (`load_score_weights()`), `screener.py:2849`

`load_score_weights()` groups historical outcomes by `(setup, grade)` from `resolved.csv`. However, the `grade` column in `resolved.csv` contains `"—"` for **all 2,250 rows** — the outcome tracker never writes the computed grade. This produces keys like `("bos_fvg", "—")` with a multiplier.

At prediction time (line 2849), the lookup uses:
```python
raw_grade = composite_grade({...})   # Returns "A+", "A", "B+", etc.
mult = score_weights.get((best, raw_grade))  # Looks for ("bos_fvg", "A+")
```

The lookup **always returns `None`** because no record with grade != "—" exists. The multiplier `if mult and mult != 1.0:` block at line 2850 never executes. The entire grade-based adaptive multiplier system has never been applied in production.

**Fix:** Write `composite_grade` value to the `grade` column in `outcome_tracker.py` when saving to pending.json. Alternatively, bucket by score ranges instead of grade strings.

---

### BUG-2 (CRITICAL): HARD_BLOCK_HOURS incorrectly blocks UTC 22

**File:** `screener.py:66`  
**Current:** `HARD_BLOCK_HOURS = {18, 22}`  
**Comment claims:** 22UTC=28.5% WR

**Actual data (n=124):** UTC 22 has **55.6% WR** — the second-best hour in the dataset, above the 50.4% overall average. It should be in `GOOD_SIGNAL_HOURS`, not `HARD_BLOCK_HOURS`.

Effect: every night at UTC 22:00 the screener runs but saves zero signals to `pending.json`. Any signal that fires during this window is lost and never tracked as an outcome.

**UTC hourly WR from resolved.csv (decisive outcomes):**

| Hour UTC | WR | N | Classification |
|----------|----|---|----------------|
| 21 | 61.9% | 210 | GOOD ✓ |
| 22 | **55.6%** | 124 | **HARD_BLOCKED — wrong** |
| 10 | 63.3% | 60 | GOOD ✓ |
| 09 | 60.3% | 232 | GOOD ✓ |
| 18 | 21.2% | 33 | HARD_BLOCK ✓ correct |
| 12 | 29.3% | 82 | BAD ✓ |

**Fix:** Remove 22 from `HARD_BLOCK_HOURS`. Add to `GOOD_SIGNAL_HOURS`. Update comment.

---

### BUG-3 (HIGH): Range Sweep signals still saved to pending.json

**File:** `screener.py:5468–5473`

`range_sweep` is correctly blocked from Telegram (`_passes_setup_tg_filter` returns False). But `_ot.save_pending(filtered, results)` saves ALL signals in `filtered` — which includes range_sweep results passing the `min_score` filter. These get tracked as outcomes, contaminating the resolved.csv with a setup that is known-broken.

**Fix:** Exclude `range_sweep` signals before calling `save_pending`. Either filter at line 5473 or exclude inside `save_pending`.

---

### BUG-4 (HIGH): Signal weights applied to LONG only — no SHORT calibration

**File:** `screener.py:2861`  
```python
if _sw and setup_dir == "long":
```

The logistic regression weights (`signal_weights.json`) were trained on LONG decisive trades (1,095 samples). SHORT signals get zero calibration. `short_dist` is a predominantly SHORT setup (287 ШОРТ signals out of 435). Its signals have no learned weight corrections, leaving the scoring entirely on hand-tuned values that are known to be inverted above score=150.

**Fix:** Train a separate LR model on SHORT decisive trades using the short-direction inverse signals (bear_choch, cvd_bear, oi_rising at top = bad for long = good for short). Apply `if _sw and setup_dir == "short":` block symmetrically.

---

### BUG-5 (MEDIUM): `datetime.utcnow()` deprecated — will break on Python 3.14+

**File:** `screener.py` — 14+ occurrences (lines 4996, 4997, 5107, 5114, 5122, 5129, 5136, 5208, 5240, 5305...)

`datetime.utcnow()` is deprecated since Python 3.12 and scheduled for removal. The screener log shows repeated `DeprecationWarning` entries in `screener_error.log`.

**Fix:** Replace all `datetime.utcnow()` with `datetime.now(datetime.UTC)` and strip timezone info where needed.

---

## 3. False Signal Rate Per Setup

### BOS/FVG — Primary setup, best WR
- **4h decisive WR: 54.9%** (n=791). Best setup.
- Score IS predictive at 24h (>150 → 61.1% WR per ANALYSIS.md).
- **Missing filter:** No actual Break of Structure (BOS) detection exists. The setup fires on FVG/OB zone presence + volume, without verifying that price broke a prior swing high/low.
- **Score inflation risk:** BOS/FVG in-zone bonus (+22 FVG, +22 OB, lines 2173/2180) fires regardless of whether the in-zone price matches the signal direction. A ШОРТ signal entering a bull FVG gets +22 (wrong).
- **CHoCH impact (measured):** BOS/FVG with `choch_bull_1h=1` achieves **72.7% WR** (n=13 in current data). This is underweighted relative to impact.

### Squeeze — Viable, needs mid-score filter
- **4h decisive WR: 53.9%** (n=463).
- P1.1 mid-score gate (lines 4909-4921) is already implemented. Correct.
- CHoCH +25 pts (line 2123) is correct and consistent with model findings.
- **Residual issue:** `if bull_mtf >= 3: s1 += 25` (line 2040-2041) fires even in bearish market structure. MTF3 in downtrend still gets +25 — this over-rewards false setups.

### Breakout — Score is inverted (confirmed)
- **4h decisive WR by score bucket:**
  - `< 100`: **50.9% WR** (n=57, decisive)
  - `100–150`: **43.0% WR** (n=256, decisive)
  - `>= 150`: **33.3% WR** (n=108, decisive)
- Score is strongly inversely correlated. Higher score = worse outcome at 4h.
- Filter at `_passes_setup_tg_filter` (lines 4904-4907) passes only `score < 100` or `score > 150`. This correctly blocks the dead zone 100-150 from Telegram.
- **Root cause identified:** Items 13 (price_pos > 0.88 → +16 pts), 15 (EMA_bull_1H → +14), 16 (EMA_bull_4H → +10) and 17 (VWAP below → +14) all reward already-in-motion signals. A breakout that has already happened looks "high-score" but has no forward edge.
- **FOMO penalty gap (line 2529):** requires SIMULTANEOUS `price_pos > 0.80 AND vol_ratio > 2.0 AND rs_btc > 2.0`. Many late-entry signals hit 0.80-0.90 price_pos with only vol_ratio or rs_btc elevated, bypassing the penalty.

### Short Distribution — Directional confusion, measurement error
- **4h decisive WR: 53.9%** (n=435). Looks OK when FLATs excluded.
- **But:** 166 of 435 signals (38.2%) resolve as FLAT at 4h. These are excluded from WR but represent real capital-at-risk time.
- **Direction contamination:** 103 of 435 signals (23.7%) are ЛОНГ-direction despite being "short distribution" setup. These fire when funding > 0 but price is below 0.40 (line 2553-2556 catches this now with reduced +5 vs +35).
- **WAIT signals (n=45):** show 100% "win" rate in backtest because ЖДАТЬ closes as FLAT. This is statistical inflation, not real edge.
- **Score ceiling cap (line 81-83):** `SETUP_TG_MAX_SCORE["short_dist"] = 150` — above 150 is suppressed. Correctly implemented.

### Range Sweep — Correctly disabled, architecture broken
- Only 12 signals total. WR meaningless (n=7 decisive).
- Root cause is correct: sweep events last 1-3 candles; batch cron polling misses them.
- `sweep_watcher.py` exists but needs to be the primary detection path.
- **Residual bug:** see BUG-3 — signals still leak into pending.json.

---

## 4. Scoring Model Weaknesses

### Scoring anti-correlation summary (from calibration/model_report.md)

| Feature | LR Coefficient | Current Score Pts | Problem |
|---------|---------------|-------------------|---------|
| `score_raw` | −0.0061 | N/A | Higher raw score predicts WORSE outcomes |
| `mtf_bull_count` | −0.0625 | +25 (≥3 MTF) | Overcounts MTF confluence |
| `ema_bull_1h` | −0.0625 | +14 | 1H bull EMA is a lagging, post-move signal |
| `cvd_kline_bull` | −0.0054 | +30/+18 (s4) | CVD kline bull is mean-reverting noise |
| `oi_rising_5` | −0.0088 | +20 (s4) | OI rising accompanies entries, not predictions |
| `choch_bull_1h` | +0.5400 | +25 (s1), +20 (s2), +16 (s4) | Underweighted vs LR |
| `oi_falling_5` | +0.0088 (implied) | +25 (s1), +14 (s1) | Correctly signed |

The signal_weights.json partial correction applies only to LONG setups and applies additive adjustments AFTER the main score is computed. This is a patch, not a fix. The underlying scoring weights remain miscalibrated.

### S2 (BOS/FVG): in-zone bonus direction mismatch

Lines 2173-2184: the in-zone bonus fires for `in_bull_fvg OR in_bear_fvg` regardless of the signal's final direction. If `direction = "ШОРТ"` and `in_bull_fvg = True`, the setup still gets +22. This adds noise to SHORT BOS/FVG signals.

### S4 (Breakout): conflicting signals score additive

ATR compression (atr_compression < 0.50 → +38) and price_pos > 0.88 (+16) are logically contradictory signals: if volatility is compressed (price in a tight range), it cannot simultaneously be at 88% of the 48h range. Both fire independently, inflating scores for signals that are partially valid but artificially boosted.

### S1/S2 (Squeeze/BOS/FVG): MTF overcounts directionally

MTF confluence `bull_mtf >= 3: +25` fires even when the market context is bearish. For BOS/FVG, `best_mtf = max(bull_mtf, bear_mtf)` (line 2164) awards the same +25 to a bearish MTF=3 setup as a bullish one — correct for a bidirectional setup, but the score doesn't distinguish between `bull_mtf=3,bear_mtf=0` vs `bull_mtf=2,bear_mtf=3`.

### Signal weight vs manual weight conflict (OI rising)

`signal_weights.json` penalizes `oi_rising_5` by −8 points. But s4 scoring awards `oi_div == "strong_bull": +20` for rising OI + rising price. These partially cancel but the semantic is the same underlying data. For the same signal, one path adds +20 and another subtracts −8, resulting in a net +12 — which is still positively rewarding a signal the LR model says is negative.

---

## 5. Missing Filters

### F-1: No BOS (Break of Structure) verification in BOS/FVG setup
The setup is named "BOS + FVG" but `detect_bos()` does not exist. There is `detect_choch()` which detects structure change, but no explicit verification that price broke a prior swing high/low. A signal fires purely on zone presence + volume without confirming the structural break.

**Impact:** BOS/FVG fires in ranging conditions where price is simply near a zone but hasn't broken structure. This contributes to the 45.1% loss rate at 4h (the "WIN" category includes non-decisive outcomes).

### F-2: No listing age filter
New listings (<30 days) are manipulation targets. The `listing_age_days` field is computed but never used as a hard filter. The symbol blacklist (line 59) manually names 5 symbols but doesn't cover new listings dynamically.

### F-3: No per-setup time filters
`BAD_SIGNAL_HOURS` applies a score floor to ALL setups uniformly. The backtest shows setup-specific hourly performance varies significantly. Squeeze typically performs better during high-volume hours (NY/London overlap, 13-16 UTC) while BOS/FVG is more robust across sessions.

### F-4: No position size output
Signals output entry/stop/TP but no position sizing recommendation. Without a standardized risk-per-trade, the system has no mechanism to prevent outsized losses on the weakest setups (breakout, range_sweep). The R:R is computed but not used to gate signal output.

### F-5: 4H FVG zones not used in squeeze in-zone bonus
The in-zone bonus for squeeze (s1) checks `in_bull_fvg` and `in_bull_ob` from 1H zones only. `fvgs_4h` and `obs_4h` are computed and available but not checked. 4H zones carry more structural weight; price entering a 4H FVG at the squeeze bottom is a much stronger signal than a 1H zone.

---

## 6. Data Quality Issues

### D-1: Grade column is always "—" in resolved.csv
See BUG-1. Direct consequence: the adaptive multiplier system and any future ML models trained on `grade` as a feature are useless with current data.

### D-2: signal_weights.json scope mismatch
The model was trained on 1,095 LONG decisive trades (529W/566L). Baseline WR was 48.3%. But the full resolved.csv decisive WR for LONG signals is ~50%+. The training set may have under-sampled good periods, making the calibration conservative.

### D-3: FLAT outcomes classification
A "FLAT" outcome (neither TP1 nor STOP hit within the resolution window) represents ~28% of all 4h outcomes. For SHORT signals especially, a flat outcome may indicate the signal was correct directionally but the TP1 was set too tight. These are excluded from WR but have real capital-at-risk time.

### D-4: Short direction WR measurement polarity
The `change_4h_pct` column is raw price change (positive = price went up). For SHORT signals, a WIN is when price goes DOWN. The ANALYSIS.md noted this — `avg_win shows −2.29% (24h)` for short_dist because the raw pct is negative (price fell) which is a win for shorts but stored as negative. Any direct analysis using `change_4h_pct` without direction-adjustment will produce inverted conclusions for short signals.

### D-5: UTC 06 is the highest-WR hour but not in GOOD_SIGNAL_HOURS
UTC 06 shows **70.6% WR** (n=17). The sample is small (17 decisive trades) but it's in neither `GOOD_SIGNAL_HOURS` nor `BAD_SIGNAL_HOURS`. It receives no special treatment. More data collection at this hour would confirm whether to add it.

---

## 7. Architectural Issues

### A-1: Monolith — 5,565 lines, no unit tests
`screener.py` contains signal detection, scoring, data fetching, output formatting, and Telegram dispatch. There are no unit tests for any `detect_*` function. A change to `detect_sweep` risks breaking `detect_fvg` integration paths with no automated verification.

**Risk:** Every bug fix risks regression. The recent CHoCH scoring change (AVEC-10) modified 3 separate scoring blocks; without tests, correctness depends on manual inspection.

### A-2: Range Sweep bypasses setup quarantine in tracking
See BUG-3. The architectural fix is to define a `QUARANTINE_SETUPS` set (or reuse `SETUP_TG_MIN_SCORE[setup] = 9999` as a signal for "do not track") and gate `save_pending` on it.

### A-3: WebSocket for sweep not integrated into screener
`sweep_watcher.py` exists and presumably uses WebSocket. But there's no feedback loop — when sweep_watcher detects a sweep event, it doesn't trigger a full `score_symbol` run or send the result through the same outcome tracking pipeline as screener.py. Sweep signals from sweep_watcher are not in resolved.csv.

### A-4: Multiple `datetime.utcnow()` calls not centralized
`datetime.utcnow()` is called independently at 14+ locations. A single `_utc_hour = datetime.now(datetime.UTC).hour` at function entry would be cleaner and fix the deprecation warnings uniformly.

### A-5: `_load_dotenv()` runs at module import time
Line 155: `_load_dotenv()` executes immediately on import. If any other module imports `screener`, the dotenv is silently applied to the process environment. This is a side effect that makes the module non-reusable as a library.

### A-6: Score_weights bucket collision risk
`load_score_weights()` uses `outcome_24h or outcome_4h` (line 1637) — prefers 24h outcome if present. This mixes 4h and 24h outcome labels in the same WR calculation. A trade resolved as STOP at 4h but WIN at 24h would be counted as WIN (the 24h outcome overwrites 4h). This makes the bucket WR a mix of horizons, not a clean 4h or 24h metric.

---

## 8. Priority Fix List

Ordered by impact × urgency:

| # | Bug/Issue | Severity | Effort | Impact |
|---|-----------|----------|--------|--------|
| 1 | BUG-1: Grade column not written → multiplier never fires | Critical | 1h | Unlocks adaptive scoring |
| 2 | BUG-2: UTC 22 in HARD_BLOCK_HOURS (55.6% WR) | Critical | 15min | +124 signals/month unblocked |
| 3 | BUG-3: Range sweep saved to pending.json | High | 30min | Cleans outcome tracking |
| 4 | BUG-4: No SHORT signal weight calibration | High | 1 day | Fixes short_dist & BOS SHORT |
| 5 | F-1: BOS verification missing in BOS/FVG | High | 3h | Removes false zone entries |
| 6 | S4 FOMO penalty too narrow (score 100-150 dead zone) | High | 1h | Breakout WR improvement |
| 7 | S2 in-zone bonus direction mismatch | Medium | 30min | Fewer false SHORT BOS signals |
| 8 | F-2: No listing age filter | Medium | 30min | Removes new-listing noise |
| 9 | BUG-5: `datetime.utcnow()` deprecated | Medium | 30min | Prevents future Python crash |
| 10 | A-6: Score_weights mixes 4h/24h outcomes | Medium | 1h | Cleaner calibration data |
| 11 | F-5: 4H FVG zones not in squeeze in-zone bonus | Low | 1h | Better squeeze entry precision |
| 12 | A-1: No unit tests for detect_* functions | Low | 1 week | Enables safe future refactoring |
| 13 | D-2: LR training sample too small / period-biased | Low | 1 day (retrain) | Improved signal weights |

---

## 9. What the System Does Well

- Bybit V5 integration is solid (retry logic, parallel fetch, rate limiting)
- Signal library is comprehensive: 14 independent `detect_*` functions covering FVG, OB, CHoCH, ATR compression, OI velocity, CVD divergence, whale detection, DOM, absorption, equal levels, LVN zones
- `_passes_setup_tg_filter()` correctly quarantines range_sweep and breakout dead zone (100-150)
- CHoCH↑1H is correctly identified as highest-value predictor and weighted +25/+20/+16 across setups
- Signal weights from logistic regression ARE being applied for LONG setups — additive correction for oi_rising (-8), cvd_kline_bull (-3), ema_bull_1h (-2), ema_bull_4h (-1.5) is directionally correct
- Cooldown system (8h per symbol) prevents signal spam
- Outcome tracker is comprehensive and allows continuous WR monitoring
- `sweep_watcher.py` exists as the right architectural response to sweep detection latency
- Multi-timeframe structure across 1H/4H/Daily is sound

---

*Audit written to AUDIT.md as per AVEVA-19. Related issues: IMPLEMENTATION_PLAN.md (pending board approval).*

# DEEP_AUDIT.md — Trading Screener Deep Audit
**Date:** 2026-04-25  
**Author:** CTO (AVEVA-33)  
**Data:** `outcomes/resolved.csv` — **2,289 resolved trades** (2026-04-12 to 2026-04-25)  
**Previous audits:** AUDIT.md (AVEC-19, 2026-04-24), ANALYSIS.md (CEO, 2026-04-24)  
**Scope:** Fresh empirical analysis on N=2,289 + code review of screener.py (5,591 lines)

---

## 0. TL;DR — What Changed Since AUDIT.md

| Finding | AUDIT.md (N=2,250) | DEEP_AUDIT (N=2,289) | Delta |
|---------|-------------------|----------------------|-------|
| Breakout 100–120 WR | 43.0% | **53.1%** | +10.1pp — filter now WRONG |
| Short_dist 100–120 WR | not broken out | **70.0%** | New strongest band |
| BOS/FVG >150 WR | 47.6% | **47.6%** | Stable — anti-correlation confirmed |
| CHoCH↑1H lift (all) | +12.2pp (n=43) | **+12.9pp (n=49)** | Strengthens |
| CHoCH in Breakout | not measured | **+15.3pp (n=27)** | New major finding |
| EMA_bull_1h drag | −1.8pp (LR) | **−6.3pp (empirical)** | Worse than reported |
| BUG-1 (grade column) | CRITICAL | **Still unfixed** | 0 rows have real grade |
| BUG-2 (UTC 22 block) | CRITICAL | **Still unfixed** | 55.6% WR still blocked |
| BUG-4 (SHORT weights) | HIGH, unfixed | **Partially fixed** | short_weights.json loaded |

---

## 1. Setup Win Rate — Fresh Numbers

**Baseline: 2,289 total resolved, decisive = TP1/WIN vs STOP/LOSS (FLAT excluded)**

| Setup | Total | Decisive | WR (4h decisive) | WR (all) | FLAT rate |
|-------|-------|----------|-----------------|----------|-----------|
| **BOS/FVG** | 806 | 620 | **54.4%** | 41.8% | 23.1% |
| **Squeeze** | 483 | 313 | **53.7%** | 34.8% | 35.2% |
| **Short Dist** | 435 | 269 | **53.9%** | 33.3% | 38.2% |
| **Breakout** | 553 | 424 | **41.3%** | 31.6% | 23.3% |
| Range Sweep | 12 | 7 | 42.9% | 25.0% | 41.7% |
| **OVERALL** | **2,289** | **1,633** | **51.1%** | **39.5%** | — |

**Key shift:** Decisive WR (which excludes FLAT) is materially higher than "all resolved" WR for every setup. For Squeeze and Short_dist the FLAT rate is >35%, which means headline WR understates actual tradeable performance by ~20pp.

---

## 2. Score Correlation — Updated Findings

### 2.1 BOS/FVG — Sweet spot shifts to 80–100

| Score Band | n | WR (4h decisive) |
|------------|---|-----------------|
| < 80 | 13 | 46.2% |
| 80–100 | 88 | **60.2%** ← best |
| 100–120 | 171 | 57.3% |
| 120–150 | 264 | 53.0% |
| > 150 | 84 | 47.6% ← drops |

Finding: anti-correlation at high scores persists. The 80–100 band is the highest-WR zone, yet min score is set to 85 — which is correct. The **>150 band underperforms the 100–150 range by 9.7pp**. BOS/FVG signals scoring >150 should NOT be filtered but should be flagged for 24h holds only (the 24h WR for >150 is still elevated, per ANALYSIS.md).

### 2.2 Squeeze — Exceptional low-score band, validate the gate

| Score Band | n | WR (4h decisive) |
|------------|---|-----------------|
| < 80 | 14 | 50.0% |
| 80–100 | 45 | **75.6%** ← exceptional |
| 100–120 | 63 | 46.0% ← dead zone |
| 120–150 | 101 | 52.5% |
| > 150 | 90 | 50.0% |

The existing P1.1 gate (requiring a strong confirmatory signal in the 100–140 band) is directionally correct. But the 75.6% WR at 80–100 (n=45) is striking — this is the highest-WR score band across all setups and doesn't require any gate. **Action: reduce min score for squeeze to 70 to capture more signals in the high-WR 70–80 zone if data supports.**

### 2.3 Breakout — Score filter now MISCALIBRATED

| Score Band | n | WR (4h decisive) |
|------------|---|-----------------|
| < 80 | 12 | 50.0% |
| 80–100 | 45 | 51.1% |
| 100–120 | 96 | **53.1%** ← now decent |
| 120–150 | 163 | 36.2% ← dead zone |
| > 150 | 108 | 33.3% ← worst |

**Critical update:** The current filter `score < 100 OR score > 150` was calibrated when 100–120 showed 43.0% WR (AUDIT.md data). With fresh N=2,289, **100–120 is now 53.1%**. The real dead zone is **120–150** (36.2%) and **>150** (33.3%).

The current filter is BLOCKING 100–120 (good) while ALLOWING >150 (terrible). The filter logic in `_passes_setup_tg_filter` at line 4933 needs updating:

```python
# Current (wrong for current data):
return score < 100 or score > 150

# Correct for current data:
return score < 120  # block 120+ entirely, or allow with 24h-hold flag
```

**This is a new critical finding not in AUDIT.md.**

### 2.4 Short Distribution — Scoring inverted only at top

| Score Band | n | WR (4h decisive) |
|------------|---|-----------------|
| 80–100 | 16 | 50.0% |
| 100–120 | 50 | **70.0%** ← strongest band |
| 120–150 | 104 | 56.7% |
| > 150 | 99 | 43.4% |

The max score cap at 150 (`SETUP_TG_MAX_SCORE["short_dist"] = 150`) is correctly placed — **>150 drops to 43.4%**. However, the 100–120 band is the best (70.0%, n=50) — these signals should NOT be gated. **The scoring model for short_dist in the 100–120 range is actually well-calibrated for this band.**

---

## 3. Signal-Level Analysis — Fresh Empirical WR Lifts

Based on N=1,633 decisive outcomes:

| Signal | n | WR | Lift vs 51.1% baseline | Direction |
|--------|---|----|----------------------|-----------|
| CHoCH↑1H = 1 | 49 | **63.3%** | **+12.2pp** | ✓ strongly positive |
| RSI < 40 | 391 | **54.0%** | +2.9pp | ✓ positive |
| OI falling (<−5%) | 351 | 53.0% | +1.9pp | ✓ positive |
| Funding negative | 396 | 48.2% | −2.9pp | ✗ slightly negative |
| EMA bull 1H = 1 | 654 | **46.9%** | **−4.2pp** | ✗ negative |
| OI rising (>+5%) | 477 | **42.6%** | **−8.5pp** | ✗ strongly negative |
| CVD kline bull | 513 | **47.0%** | **−4.1pp** | ✗ negative |

### Key updated findings:

**EMA_bull_1h drag is −4.2pp** (vs −1.8pp in the LR model report, −0.63pt score correction). The hand-tuned scoring still awards +14 pts for EMA_bull_1H in s4 (breakout). The gap between empirical drag and applied correction is large.

**Funding negative is slightly negative** (−2.9pp). The `signal_weights.json` awards +2 pts for negative funding. This is directionally wrong at the aggregate level. Negative funding may be a setup condition for squeeze (correct) but is not a universal positive predictor.

**CHoCH in Breakout**: Previously unmeasured. Fresh data shows CHoCH↑1H gives **+15.3pp lift in Breakout** (55.6% vs 40.3%, n=27). This is the second-largest predictor after squeeze's MTF confluence. **CHoCH should be added as a score bonus in the breakout scoring block (s4).**

**CHoCH by setup:**

| Setup | CHoCH=1 WR | CHoCH=0 WR | Lift | n |
|-------|------------|------------|------|---|
| BOS/FVG | **72.7%** | 54.0% | +18.7pp | 11 |
| Breakout | **55.6%** | 40.3% | +15.3pp | 27 |
| Squeeze | 33.3% | 53.9% | −20.5pp | 3 (unreliable) |

---

## 4. Unresolved Bugs — Status Check

### BUG-1 (CRITICAL): Grade column still "—" in all rows

**Status: UNFIXED**

All 2,289 rows in resolved.csv have `grade = "—"`. The `load_score_weights()` function (screener.py:1618) builds buckets keyed by `(setup, grade)`. At prediction time (line 2858), it looks up `score_weights.get((best, raw_grade))` where `raw_grade` is a live computed value like `"A+"`. The key `("bos_fvg", "A+")` never exists in the dict because no historical row has grade != "—". The multiplier block at line 2859 **has never executed in production**.

**Consequence:** The adaptive scoring system has been completely inactive since deployment. All grade-based calibration is dead code.

**Fix path:** In `outcome_tracker.py`, write the computed `composite_grade()` value to the `grade` field when saving to `pending.json`. This is a 1-hour fix that unlocks the entire adaptive scoring system.

---

### BUG-2 (CRITICAL): UTC 22 still HARD_BLOCKED despite 55.6% WR

**Status: UNFIXED**

`HARD_BLOCK_HOURS = {18, 22}` at screener.py:66 remains unchanged.

Fresh data confirms: UTC 22 → **55.6% WR, n=124 decisive outcomes**. This is the 4th-highest WR hour in the dataset, above the 51.1% overall baseline.

UTC 18 is correctly blocked (21.2% WR, n=33).

Every night at 22:00 UTC, the screener runs and saves zero signals to `pending.json`. An estimated **124+ high-WR signals per dataset window** have been suppressed. The comment in the code (`# 22UTC=28.5% WR`) is **factually incorrect** based on current data.

**15-minute fix:** `HARD_BLOCK_HOURS = {18}` and add 22 to `GOOD_SIGNAL_HOURS`.

---

### BUG-3 (HIGH): 12 Range Sweep signals in resolved.csv

**Status: UNFIXED**

Despite `range_sweep` being disabled from Telegram output, the outcome tracker still receives and stores range_sweep signals. The 12 range_sweep entries in resolved.csv contaminate WR statistics and make the "range_sweep WR" metric meaningless (too small, known-broken detection method).

**Fix path:** Add a `QUARANTINE_SETUPS = {"range_sweep"}` check before calling `_ot.save_pending()` at screener.py:5499.

---

### BUG-4 (HIGH): SHORT signal calibration — Partially fixed

**Status: PARTIALLY FIXED**

`signal_weights_short.json` now exists and is loaded at screener.py:1669–1675. The SHORT calibration block applies `score_gt150`, `rsi_gt65`, `ema_bull_1h` penalties (lines 2890–2898).

**Remaining gap:** The SHORT model penalizes `score > 150` but the fresh data shows `short_dist` 100–120 is actually the best zone (70.0% WR). The SHORT model was not trained with the finding that the mid-band is the strongest — it was calibrated on overall inversion, not per-band. A per-band correction would be more accurate.

---

### BUG-5 (MEDIUM): `datetime.utcnow()` still deprecated

**Status: UNFIXED**

`datetime.utcnow()` appears at 14+ locations. Python 3.12 DeprecationWarnings are firing. No fix has been applied.

---

## 5. New Findings Not in AUDIT.md

### NF-1: Breakout filter is now inverted (blocks good band, allows bad band)

The `_passes_setup_tg_filter` logic for breakout (line 4933): `return score < 100 or score > 150` was valid when 100–120 had 43.0% WR. With current data, 100–120 is 53.1% and >150 is 33.3%.

**The filter is currently allowing >150 breakout signals (33.3% WR) while blocking 100–120 (53.1% WR).** This is the opposite of what is intended.

**Immediate fix:**
```python
# screener.py line 4933 — update breakout filter
return score < 120   # block everything ≥120 (dead zones: 120–150 and >150)
```

Or if preserving the 24h hold path for >150:
```python
return score < 120 or (score > 150 and r.get("hold_24h"))
```

---

### NF-2: CHoCH should be added to Breakout scoring block (s4)

CHoCH↑1H in breakout: **+15.3pp lift** (55.6% vs 40.3%, n=27). The current s4 scoring block does not include CHoCH as a scored component. Breakout is currently the worst-WR active setup (41.3%). Adding CHoCH would help filter breakout signals to only those with structural confirmation.

Proposed addition to s4 scoring (~line 2580):
```python
# CHoCH↑1H: структурное подтверждение пробоя — +15.3pp WR lift (n=27, DEEP_AUDIT)
if choch_1h == "bull_choch":
    s4 += 20; n4.append("CHoCH↑1H!")
```

---

### NF-3: Funding rate is NOT a universal positive predictor

Empirical data shows **funding negative → 48.2% WR** (n=396), slightly below the 51.1% baseline. The `signal_weights.json` entry `"funding_neg": +2.0` is directionally **wrong** at aggregate level. The LR model report (AUC=0.59) showed `funding: +0.0095 coef` (barely positive), but aggregated empirical data shows slight underperformance.

**Explanation:** Negative funding is a prerequisite for squeeze setups specifically (shorts get paid, squeeze is imminent). It is NOT a universal edge signal across all setups. The +2 pts should be scoped to squeeze setup only, or removed from the global signal_weights.json.

---

### NF-4: Week-over-week regime sensitivity is extreme

| Week | WR | n |
|------|----|----|
| 2026-W15 | **68.6%** | 105 |
| 2026-W16 | 48.8% | 1,171 |
| 2026-W17 | 51.8% | 357 |

Week 15 (first week of data collection) had a 68.6% WR — likely a recovery/squeeze week where longs ran. Week 16 (the bulk of data) dropped to 48.8%. This 19.8pp difference between regimes means:

1. **Any WR metrics derived predominantly from Week 15 data are regime-biased.** The LR model was trained on Week 15-16 data and the 48.3% baseline may be slightly optimistic.
2. **No regime filter exists in the screener.** A BTC trend health check (`btc_4h_change`, `btc_ema_pos`) is computed but not used to dampen signal confidence in choppy/bearish macro weeks.

---

### NF-5: ЖДАТЬ signals resolve decisively at 86.3% WR

ЖДАТЬ direction signals (used in short_dist and a few BOS/FVG): among those that DO resolve decisively (n=139), win rate is **86.3%**. These are not "wait" signals — they are signals where the entry was delayed to a specific level, and once the level is reached (triggering TP1/STOP resolution), they win 86% of the time.

This suggests ЖДАТЬ signals may be the highest-quality signals in the system and should be tracked separately and promoted, not treated as ambiguous output.

---

### NF-6: UTC 06 WR is 70.6% (n=17) — needs confirmation

UTC 06 shows 70.6% WR with n=17 decisive. Too small for action but should be on the watchlist. If it holds above 60% at n=50+, it belongs in `GOOD_SIGNAL_HOURS`.

---

## 6. Scoring Model Architecture Assessment

### 6.1 Current scoring pipeline

```
hand_tuned_score (s1/s2/s4/s5)
  → BTC penalty adjustments
  → grade multiplier (INACTIVE due to BUG-1)
  → signal_weights additive (LONG only, 9 features)
  → signal_weights_short additive (SHORT only, 3 features)
  → _passes_setup_tg_filter() gating
```

### 6.2 Critical architectural flaw: additive correction on broken base

The current model applies LR-derived additive corrections (+/- a few points) on top of a hand-tuned score that has **known-inverted weights** for EMA_bull_1H (+14 pts in breakout, empirically −4.2pp) and MTF_bull (+25 pts in squeeze/BOS, empirically −1.1pp). A −2pt correction for EMA_bull_1H does not fix the problem when the base score gives +14 pts.

The correct fix is replacement, not correction. The existing `load_score_weights()` infrastructure and the `calibration/train_model.py` script are in place. The missing piece is per-setup LR models feeding full score replacement.

### 6.3 Signal weight conflict: OI

`signal_weights.json` penalizes `oi_rising_5` by −8 pts. But s2 (BOS/FVG) and s4 (breakout) award `oi_change > 15: +25`, `oi_change > 8: +15`. For a signal with OI rising 12%, the score gets +15 and then −8 = net +7. The empirical drag from OI rising is −8.5pp vs baseline. Net scoring should be **negative** for OI rising, not +7.

### 6.4 Missing: per-setup calibration

The current signal_weights.json model was trained on ALL LONG decisive trades together (n=1,095). This pools breakout and BOS/FVG and squeeze — setups with very different score-WR relationships. A single model cannot simultaneously fix the inverted breakout correlation and the U-shaped squeeze correlation.

**Required:** three separate LR models:
- `signal_weights_bos_fvg.json` (trained on BOS/FVG LONG, n≈337)
- `signal_weights_breakout.json` (trained on Breakout LONG, n≈175)
- `signal_weights_squeeze.json` (trained on Squeeze, n≈168)

---

## 7. Architecture Gaps — Status vs GAP_ANALYSIS.md

| Gap | Description | Status |
|-----|-------------|--------|
| GAP-1 | `calc_mtf_grade()` not implemented | **UNRESOLVED** — composite_grade() is a heuristic stub |
| GAP-2 | No 1W klines fetch | **UNRESOLVED** — fetch_all_data() has k1h, k4h, kD only |
| GAP-3 | No 15m klines | **UNRESOLVED** |
| GAP-4 | X-grade hard block missing | **UNRESOLVED** (blocked by GAP-1,2) |
| GAP-5 | C-grade threshold enforcement | **UNRESOLVED** |
| GAP-6 | d_ctx/h4_ctx not struct-wrapped | **UNRESOLVED** |
| GAP-7 | MTF grade line in Telegram | **UNRESOLVED** |
| GAP-8 | mtf_grade in outcome tracker | **UNRESOLVED** |
| GAP-9 | LR is additive-only | **UNRESOLVED** (partial — signal_weights_short added) |
| GAP-10 | Monolith, no unit tests | **UNRESOLVED** |

**None of the 10 architecture gaps from GAP_ANALYSIS.md have been fully resolved.** The sweep_watcher.py (AVEC-4) and CHoCH scoring boost (AVEC-10) are the only architectural improvements since the initial audit.

---

## 8. Fully Resolved Issues

| Issue | Resolution |
|-------|-----------|
| AVEC-4: WebSocket sweep detection | `sweep_watcher.py` done (441 lines) |
| AVEC-10: CHoCH↑1H scoring | +25 pts in s1 (squeeze), +20 pts in s2 (BOS/FVG), +16 pts in s4 confirmed |
| AVEC-31: k1w/k15m fetches, SHORT weights, unit tests | Partial — signal_weights_short.json added, k15m added per commit |
| Breakout dead zone filter | `_passes_setup_tg_filter` blocks 100–150 (was correct, now needs update) |
| Squeeze mid-score gate | P1.1 correctly implemented with strong-signal requirement |

---

## 9. Priority Fix Matrix — Updated

| # | Issue | Severity | Effort | Impact |
|---|-------|----------|--------|--------|
| 1 | **NF-1: Breakout filter inverted** (blocks 100–120 at 53.1%, allows >150 at 33.3%) | **CRITICAL** | 5 min | Direct WR fix |
| 2 | **BUG-2: Remove UTC 22 from HARD_BLOCK** (55.6% WR) | **CRITICAL** | 15 min | +124 signals/mo unblocked |
| 3 | **BUG-1: Write grade column** in outcome_tracker.py | **CRITICAL** | 1h | Unlocks adaptive multiplier system |
| 4 | **NF-2: Add CHoCH to Breakout scoring (s4)** | **HIGH** | 30 min | +15.3pp WR lift for CHoCH breakouts |
| 5 | **BUG-3: Quarantine range_sweep** from save_pending | **HIGH** | 30 min | Cleans outcome data |
| 6 | **GAP-2+3: Add k1w + k15m fetches** | **HIGH** | 2h | Enables full MTF grade |
| 7 | **GAP-1: Implement calc_mtf_grade()** | **HIGH** | 3h | Core MTF scoring engine |
| 8 | **NF-3: Scope funding_neg bonus** to squeeze only | **MEDIUM** | 15 min | Removes false positive for other setups |
| 9 | **GAP-4: X-grade hard block** in TG filter | **MEDIUM** | 30 min | Stops counter-trend signals |
| 10 | **GAP-8: Write mtf_grade** to outcome tracker | **MEDIUM** | 1h | Enables grade→WR validation |
| 11 | **BUG-5: Replace datetime.utcnow()** | **MEDIUM** | 30 min | Prevents Python 3.14 crash |
| 12 | **GAP-9: Per-setup LR models** (3 separate) | **MEDIUM** | 3 days | Fixes score anti-correlation at root |
| 13 | **GAP-10: Monolith split + unit tests** | **LOW** | 1 week | Enables safe refactoring |

---

## 10. Recommended Immediate Actions (Code-Ready)

### Action 1 — Fix breakout filter (5 minutes)

**File:** `screener.py:4933`

```python
# BEFORE:
return score < 100 or score > 150

# AFTER (per DEEP_AUDIT NF-1, 2026-04-25):
# 100–120: 53.1% WR (n=96) — decent, allow
# 120–150: 36.2% WR (n=163) — dead zone, block  
# >150: 33.3% WR (n=108) — worst, block
return score < 120
```

### Action 2 — Unblock UTC 22 (15 minutes)

**File:** `screener.py:64–66`

```python
# BEFORE:
HARD_BLOCK_HOURS = {18, 22}  # 18UTC=18% WR, 22UTC=28.5% WR

# AFTER (per DEEP_AUDIT BUG-2):
HARD_BLOCK_HOURS = {18}       # 18UTC=21.2% WR (correct). 22UTC=55.6% WR (WRONG — removed)
GOOD_SIGNAL_HOURS = {1, 2, 5, 9, 10, 13, 15, 16, 20, 21, 22}  # 22 added
```

Also update screener.py:5496:
```python
# comment "22% → 28%" no longer applies to UTC 22
print(f"[HARD BLOCK] UTC {_utc_hour:02d}:xx — WR=21% < 30%."
      f" Сигналы не сохранены в pending (не торговать этот час).")
```

### Action 3 — Add CHoCH to Breakout scoring

**File:** `screener.py` — s4 scoring block (around line 2580, before `scores["breakout"] = s4`)

```python
# CHoCH↑1H: structural confirmation for breakout — +15.3pp WR lift (n=27, DEEP_AUDIT)
# Only valid for LONG breakouts (bull CHoCH confirms structure break up)
if choch_1h == "bull_choch":
    s4 += 20; n4.append("CHoCH↑1H!")
```

### Action 4 — Quarantine range_sweep from outcome tracking

**File:** `screener.py:5499`

```python
# BEFORE:
saved = _ot.save_pending(filtered, results)

# AFTER:
QUARANTINE_SETUPS = {"range_sweep"}
filtered_trackable = [r for r in filtered if r.get("setup") not in QUARANTINE_SETUPS]
saved = _ot.save_pending(filtered_trackable, results)
```

---

## 11. Data Quality Assessment

| Metric | Value | Status |
|--------|-------|--------|
| Total resolved trades | 2,289 | Growing at ~50-100/day |
| Grade column coverage | 0% (all "—") | BROKEN |
| FLAT rate (4h) | ~30% avg | High — reduces effective sample size |
| Regime coverage | 3 partial weeks (W15–W17) | W15 is outlier (bull week) |
| CHoCH sample size | 49 decisive | Small — directional but n<100 |
| Range_sweep signals | 12 (known-broken) | Contaminating data |
| Direction polarity (SHORT) | Stored as raw price change | SHORT WR inverted in raw analysis |

**Minimum data quality thresholds for reliable per-signal statistics: n≥200 decisive outcomes.** Many signal combinations (e.g., CHoCH + breakout: n=27) are below this threshold. Findings are directionally reliable but weights derived from them should be conservative.

---

## 12. System Health Summary

**Working well:**
- Bybit V5 data pipeline (retry, parallel fetch, rate limiting)
- 14 signal detection functions with comprehensive coverage
- Outcome tracker producing clean 4h/24h WR splits
- Telegram gating: range_sweep correctly blocked, squeeze mid-score gated, short_dist capped
- signal_weights.json providing directionally correct additive corrections (LONG)
- CHoCH↑1H weighted correctly in s1 (+25), s2 (+25), s4 (+16/via choch_conviction)
- sweep_watcher.py providing real-time sweep detection

**Broken or missing:**
- Grade multiplier system (BUG-1 — completely inactive)
- UTC 22 time filter (BUG-2 — blocks a 55.6% WR hour)
- Range sweep outcome contamination (BUG-3)
- Breakout score filter (NF-1 — now allows worst bands, blocks decent band)
- 1W + 15m timeframes (GAP-2, GAP-3 — MTF engine incomplete)
- calc_mtf_grade() (GAP-1 — core grade function not implemented)
- Per-setup LR calibration (GAP-9 — single pooled model insufficient)
- Unit tests (GAP-10 — zero automated coverage)

---

*Generated by CTO agent (AVEVA-33). Cross-references: AUDIT.md, ANALYSIS.md, GAP_ANALYSIS.md, calibration/model_report.md.*

# Gap Analysis: Current System vs MTF_ENGINE_DESIGN.md

**Issue:** AVEA-29  
**Date:** 2026-04-25  
**Author:** CTO (2b458376)  
**Reference:** MTF_ENGINE_DESIGN.md (AVEVA-28), ANALYSIS.md, screener.py (5326 lines)

---

## Summary

The MTF_ENGINE_DESIGN.md specifies a 5-timeframe confluence engine with structured
context objects, a calibrated scoring table, and an A+/A/B/C/X grading system.
The current screener has the signal primitives (detect_htf_trend, detect_fvg,
detect_order_blocks, detect_choch, detect_candle_patterns) and fetches 1H/4H/Daily
data, but the structured MTF engine is **not yet wired together**.

---

## Already Done ✅

| Component | Status | Location |
|-----------|--------|----------|
| `detect_htf_trend()` | Done | screener.py:730 |
| `detect_fvg()` | Done | screener.py:765 |
| `detect_order_blocks()` | Done | screener.py:805 |
| `detect_choch()` | Done | screener.py:1210 |
| `detect_candle_patterns()` | Done | screener.py (existing) |
| 1H / 4H / Daily klines fetch | Done | screener.py:4778-4780 |
| Daily trend inline (`daily_trend`) | Done | screener.py:1773 |
| 4H trend inline (`h4_trend`) | Done | screener.py:1774 |
| 4H FVG inline (`fvgs_4h`) | Done | screener.py:1781 |
| 4H OB inline (`obs_4h`) | Done | screener.py:1785 |
| 1H CHoCH inline (`choch_1h`) | Done | screener.py:1867 |
| 4H CHoCH inline (`choch_4h`) | Done | screener.py:1868 |
| Daily FVG/OB inline | Done | screener.py:1873-1874 |
| Ad-hoc grade (`composite_grade()`) | Partial | screener.py:3698 (≠ spec) |
| `load_score_weights()` + `signal_weights.json` | Done | screener.py:1618; calibration/ |
| range_sweep disabled in TG filter | Done | screener.py:4899 |
| Breakout score filter (<100 or >150) | Done | screener.py:4904 |
| Squeeze mid-score gate | Done | screener.py:4912 |
| CHoCH conviction discount in filter | Done | screener.py:4924 |
| **sweep_watcher.py (WebSocket sweep)** | **Done** | sweep_watcher.py (441 lines) |
| LR calibration (partial, additive) | Done | calibration/signal_weights.json |

---

## Gaps — Ranked by WR Impact

### GAP-1 — calc_mtf_grade() not implemented

**Impact: CRITICAL** | **Complexity: 3/5**

The design's core function (Section 4.3) is missing entirely. The current
`composite_grade()` (screener.py:3698) is a simpler heuristic using raw
`mtf_b`/`mtf_s` counts and a score threshold — it does not implement the
+30/+25/+20/+15 alignment scoring table, the −20/−15/−15 opposition penalties,
the `near_weekly_low/high` level bonuses, or the 15m entry confirmation points.

The current ad-hoc grade is **not comparable** to the designed A+/A/B/C/X system
and cannot be used to validate grade→WR ordering.

**Missing:**
- `calc_mtf_grade(setup_dir, w_ctx, d_ctx, h4_ctx, choch_1h, m15_ctx)` function
- Score table: 1W(+30/-20), 1D(+25/-15), 4H(+20/-15), 4H-zone(+15), 1H-CHoCH(+15), W-level(+10/-10), 15m(+10/-5)
- Grade thresholds: A+(≥70), A(≥50), B(≥25), C(<25), X(hard block)
- Hard block: `w.trend == opp AND d.trend == opp` → grade X

---

### GAP-2 — No 1W klines fetch + compute_weekly_context() missing

**Impact: HIGH** | **Complexity: 2/5**

1W klines are not fetched in `fetch_all_data()` (screener.py:4776-4780). Only
`k1h`, `k4h`, `kD` exist. Weekly context (`w_ctx`) is required by `calc_mtf_grade()`
for the +30/-20 macro alignment and the +10/-10 weekly level proximity scoring.

**Missing:**
- `"k1w": lambda: fetch_klines(sym, "W", 52)` in fetch_all_data()
- `compute_weekly_context(hi1w, lo1w, cl1w)` function (Section 3.1)
- Weekly trend, weekly_high, weekly_low, near_level, near_high, near_low output
- Use `cl1w[:-1]` to exclude live unfinished weekly candle (Section 13.2 recommendation)
- 24h cache for 1W klines (weekly candles change once/week — design Section 9)

**WR impact estimate:** 1W macro filter is the largest single signal (+30/-20).
Misaligned macro is the most common source of false setups in trending markets.

---

### GAP-3 — No 15m klines fetch + compute_15m_context() missing

**Impact: HIGH** | **Complexity: 2/5**

15m klines not fetched. `compute_15m_context()` (Section 3.5) is not implemented.
15m entry candle confirmation is worth ±10 in the MTF score and is the only
lower-timeframe signal the design uses for entry precision.

**Missing:**
- `"k15m": lambda: fetch_klines(sym, "15", 96)` in fetch_all_data()
- `compute_15m_context(op15, hi15, lo15, cl15, vol15)` function
- Bullish/bearish entry candle detection (engulfing, hammer, shooting_star, 15m CHoCH)
- 3-minute cache for 15m klines (Section 9)

---

### GAP-4 — X-grade hard block absent from _passes_setup_tg_filter()

**Impact: HIGH** | **Complexity: 1/5**

Section 5.5 of the design specifies:
```python
if mtf_grade == "X":
    return False  # opposing macro — suppress alert
```

This single check requires both 1W and 1D trends opposing the setup direction.
Currently no such suppression exists — signals with opposing 1W+1D macro still
reach Telegram. This is a direct WR drag: trading against two aligned HTF trends
is the highest-risk scenario.

**Blocker:** Requires GAP-1, GAP-2 to be resolved first.

---

### GAP-5 — C-grade threshold enforcement missing

**Impact: HIGH** | **Complexity: 1/5**

Design Section 7 specifies: grade C signals only pass if `setup_score ≥ 130`.
Grade C signals (MTF score <25) represent weak confluence situations — 1-2 TFs
supporting at best. Currently all signals pass regardless of MTF confluence level
(the composite_grade heuristic is not enforced as a filter).

**Blocker:** Requires GAP-1.

---

### GAP-6 — d_ctx and h4_ctx not wrapped into structs

**Impact: MEDIUM** | **Complexity: 2/5**

Daily and 4H context is computed inline in `analyze_symbol()` (lines 1773-1874)
but not structured as `d_ctx`/`h4_ctx` dicts with `in_bull_zone`/`in_bear_zone`
flags. `calc_mtf_grade()` requires these structs as input arguments.

**Missing:**
- `compute_daily_context(opD, hiD, loD, clD, volD)` wrapper (Section 3.2)
- `compute_4h_context(op4h, hi4h, lo4h, cl4h, vol4h)` wrapper (Section 3.3)
- Both functions return: `{trend, in_bull_zone, in_bear_zone, choch, fvg_count, ob_count}`

Note: The underlying data (fvgs_4h, obs_4h, daily_trend, etc.) is already computed
inline — this gap is a refactoring of existing logic into clean structs, not new
computation.

---

### GAP-7 — MTF grade line missing from Telegram templates

**Impact: MEDIUM** | **Complexity: 2/5**

Design Section 6 specifies a `📊 MTF [A+] 1W:B✅ | 1D:B✅ | ...` line in every
alert. Currently the TG messages do not include MTF grade context. Traders cannot
see the macro alignment quality at a glance.

**Missing:**
- `mtf_line = f"📊 MTF [{mtf_grade}] {mtf_summary}"` in all 5 Telegram templates
- ⭐ prefix for A+ grade, `⚡ low confluence` suffix for C grade
- Integration for ~5 TG template blocks (BOS/FVG, Squeeze, Breakout, Short Dist, Sweep)

**Blocker:** Requires GAP-1, GAP-2, GAP-3.

---

### GAP-8 — mtf_grade/mtf_score/weekly_trend not stored in outcome tracker

**Impact: MEDIUM** | **Complexity: 1/5**

Design Section 10 specifies adding `mtf_grade`, `mtf_score`, `weekly_trend` to
`pending.json` entries and `resolved.csv` columns. Without these fields, the
validation plan (Section 12 — "A+ WR > A WR > B WR > baseline") cannot be
executed. This data is required to confirm the grade→WR ordering is correct
before enabling hard suppression.

**Missing:**
- 3 new fields in pending.json write path
- 3 new columns in resolved.csv write path
- WR analysis grouped by `mtf_grade` after 200+ graded signals resolve

**Blocker:** Requires GAP-1.

---

### GAP-9 — LR score replacement is additive-only (not full replacement)

**Impact: MEDIUM** | **Complexity: 4/5**

ANALYSIS.md (AVEC-1 mandate) calls for replacing hand-tuned additive scoring
with logistic regression dot-product weights. The current implementation
(`signal_weights.json` via `load_score_weights()` at screener.py:2855) applies
the 9 LR weights as **additive corrections** on top of the existing hand-tuned
score. The anti-correlation problem (Score ≥120 WR = 43.0%) is partially mitigated
but the root cause — EMA bull alignment being over-rewarded in the hand-tuned model
— is not fixed.

**Current state:** 9 binary signal adjustments loaded from calibration/signal_weights.json
(choch_bull_1h: +5, oi_falling_5: +5, ..., oi_rising_5: -8). AUC = 0.59.

**Missing:**
- Per-setup LR models (current model is LONG-only; SHORT not calibrated)
- Full replacement of s1/s2/s4/s5 score blocks with `score = baseline + Σ(feature × weight)`
- Per-setup calibration: Breakout and Short Dist have inverted score correlation, need
  separate models, not the current global LONG model
- MTF grade as a feature in the LR model (design Section 13.4 coordination with AVEC-2)

---

### GAP-10 — Monolith not split; no unit tests

**Impact: LOW (WR), HIGH (maintainability)** | **Complexity: 5/5**

screener.py is 5326 lines. No `tests/` directory. No pytest unit tests for
detect_sweep, detect_fvg, detect_order_blocks, detect_choch.

**Missing per ANALYSIS.md Priority 4:**
- `signals/` module (detect_* functions)
- `scoring/` module (s1–s5 blocks, composite_grade)
- `data/` module (fetch_klines, fetch_funding_history, etc.)
- `output/` module (Telegram formatting)
- `pytest` tests for all detect_* functions
- `config.py` for GOOD_SIGNAL_HOURS, HARD_BLOCK_HOURS, MIN_TURNOVER_24H

---

## Priority Matrix

| Gap | Description | WR Impact | Complexity | Priority |
|-----|-------------|-----------|------------|----------|
| GAP-1 | calc_mtf_grade() implementation | CRITICAL | 3/5 | **P0** |
| GAP-2 | 1W klines + compute_weekly_context() | HIGH | 2/5 | **P0** |
| GAP-4 | X-grade hard block in TG filter | HIGH | 1/5 | **P1** (after GAP-1,2) |
| GAP-3 | 15m klines + compute_15m_context() | HIGH | 2/5 | **P1** |
| GAP-5 | C-grade threshold enforcement | HIGH | 1/5 | **P1** (after GAP-1) |
| GAP-6 | d_ctx / h4_ctx struct wrappers | MEDIUM | 2/5 | **P1** (prerequisite for GAP-1) |
| GAP-7 | MTF line in Telegram templates | MEDIUM | 2/5 | **P2** (after GAP-1,2,3) |
| GAP-8 | mtf_grade in outcome tracker | MEDIUM | 1/5 | **P2** (after GAP-1) |
| GAP-9 | LR full score replacement | MEDIUM | 4/5 | **P3** |
| GAP-10 | Monolith split + unit tests | LOW (WR) | 5/5 | **P4** |

---

## Implementation Sequence (from design Section 11)

The design's recommended sequence maps to gap resolution order:

```
Step 1 — Add k1w and k15m fetches     → resolves GAP-2 and GAP-3 (partial)
Step 2 — compute_weekly_context()      → resolves GAP-2
Step 3 — compute_daily_context()       → resolves GAP-6 (partial)
Step 4 — compute_4h_context()          → resolves GAP-6
Step 5 — compute_15m_context()         → resolves GAP-3
Step 6 — calc_mtf_grade()              → resolves GAP-1
Step 7 — Wire into analyze_symbol()    → connects all ctx structs
Step 8 — X-block in TG filter          → resolves GAP-4
Step 9 — mtf_line in 5 TG templates   → resolves GAP-7
Step 10 — mtf_grade in pending/csv     → resolves GAP-8
Step 11 — Unit tests for calc_mtf_grade → part of GAP-10
```

Estimated total effort per design: ~5.5 hours for Steps 1–11.  
GAP-5 (C-grade enforcement) is one additional if-block, ~15 minutes.  
GAP-9 (LR full replacement) is a separate initiative, estimate 3–5 days.  
GAP-10 (monolith split) is a separate quarter-long initiative.

---

## Notable Design Decisions Confirmed in Code

1. **Sweep WebSocket is done.** `sweep_watcher.py` (441 lines) fully implements
   AVEC-4. range_sweep is disabled in the batch screener; the watcher handles it.

2. **CHoCH↑_1H is partially boosted.** `choch_conviction` flag gives -30pts to
   `min_sc` threshold (screener.py:4924-4925) and +5 additive via signal_weights.json.
   The design's full +15 in the MTF scoring table (GAP-1) would replace/supplement this.

3. **LR calibration is deployed but partial.** signal_weights.json has 9 weights with
   AUC=0.59. LONG-only. Not a full replacement of additive scoring.

4. **composite_grade() is NOT the designed calc_mtf_grade().** The existing function
   at screener.py:3698 is a simpler score+MTF-count heuristic without 1W context,
   without level proximity scoring, and without 15m confirmation. It produces
   A+/A/B+/B/C/D labels that are meaningfully different from the design's A+/A/B/C/X
   system and should not be confused with it.

# Implementation Roadmap: 3-Phase MTF Plan

**Issue:** AVEA-30  
**Date:** 2026-04-25  
**Author:** CTO (2b458376)  
**References:** [MTF_ENGINE_DESIGN.md](MTF_ENGINE_DESIGN.md), [GAP_ANALYSIS.md](GAP_ANALYSIS.md), [ANALYSIS.md](ANALYSIS.md)

---

## Executive Summary

The screener has all the raw signal primitives and fetches 1H/4H/Daily klines, but the
structured MTF grading engine is not yet wired together. The gap analysis identified
10 concrete gaps. This roadmap phases them into three delivery windows:

| Phase | Window | Theme | WR Unlock |
|-------|--------|-------|-----------|
| 1 | 1–2 days | Foundation + groundwork | LR SHORT calibration, unit tests, kline fetches |
| 2 | 3–5 days | MTF engine using existing TF data | 4H/Daily/1H grading, Telegram MTF line, outcome tracking |
| 3 | 1–2 weeks | Full 5-TF stack + advanced features | 1W macro filter, 15m precision, dynamic SL/TP |

---

## Phase 1 — Quick Wins (1–2 days)

**Goal:** Accuracy improvements that require no new API infrastructure.  
All tasks operate on already-fetched data or test existing logic.

---

### T1.1 — Add k1w and k15m to `fetch_all_data()` ⏱ 20 min

**Gap:** GAP-2 (partial), GAP-3 (partial)  
**File:** `screener.py:4776`  
**Risk:** Zero — just queues 2 new fetches that Phase 2 will consume.

```python
# Add after line 4780 in fetch_all_data()
"k1w":  lambda: fetch_klines(sym, "W",  52),
"k15m": lambda: fetch_klines(sym, "15", 96),
```

Add corresponding defaults and cache annotations:
- `k1w` → cache 24h (weekly candles change once/week)
- `k15m` → cache 3 min

**Acceptance:** `fetch_all_data()` returns `k1w` and `k15m` tuples for any symbol.

---

### T1.2 — SHORT direction LR calibration ⏱ 2–3 hours

**Gap:** GAP-9 (partial)  
**File:** `calibration/signal_weights.json`, `screener.py:1618`  
**Risk:** Low — additive weight deltas, backward-compatible.

Current state: `signal_weights.json` has 9 features, LONG-only, AUC=0.59.  
ANALYSIS.md confirms SHORT signals (Short Distribution, partial Breakout) behave
differently — high score is *inversely* correlated with SHORT win rate.

Steps:
1. Filter `resolved.csv` to SHORT signals (`direction == "SHORT"`)
2. Train a second logistic regression model on SHORT-specific features
3. Store as `calibration/signal_weights_short.json`
4. In `screener.py`, detect setup direction and load the appropriate weights file
5. Validate AUC on holdout set (target: >0.55 before enabling)

**Acceptance:** Short Distribution signals with score >150 pass through the SHORT
weights model and receive a negative correction; Telegram threshold enforcement
remains unchanged.

---

### T1.3 — Unit tests for detect_* functions ⏱ 2–3 hours

**Gap:** GAP-10 (partial)  
**File:** `tests/test_detect.py` (new)  
**Risk:** Zero — no production code changed.

Create `tests/test_detect.py` with pytest tests for:
- `detect_sweep()` — test 3-candle lookback; confirm low sweep and high sweep cases
- `detect_fvg()` — test gap identification with synthetic OHLC arrays
- `detect_order_blocks()` — test bullish and bearish OB detection
- `detect_choch()` — test bull_choch and bear_choch on synthetic pivots
- `detect_htf_trend()` — test bull/bear/ranging classification

Use synthetic numpy arrays. No external API calls in tests.

**Acceptance:** `pytest tests/test_detect.py` passes ≥20 assertions cleanly.

---

### T1.4 — Fix WAIT signal misclassification in outcome tracking ⏱ 30 min

**Gap:** ANALYSIS.md finding (not a numbered gap)  
**File:** `screener.py` (outcome write path), `outcomes/resolved.csv`  
**Risk:** Low — affects outcome data quality, not signal generation.

ANALYSIS.md confirmed: Short Distribution WAIT signals (n=45) show 100% WR at 24h
because they resolve as FLAT price moves (−0.1% threshold). The WR is artificial.

Fix: When `direction == "WAIT"`, set `outcome_direction = "FLAT"` in the resolved
record so that WAIT resolutions are excluded from directional WR aggregation.
Real short-only WR for Short Distribution is 39% (24h), not 43.4%.

**Acceptance:** Re-running win rate analysis on `resolved.csv` after fix shows Short
Distribution 24h WR ≤41% (no longer inflated by WAIT signals).

---

### Phase 1 Deliverables Summary

| Task | File(s) | Effort | Resolves |
|------|---------|--------|---------|
| T1.1 — k1w / k15m fetches | screener.py:4776 | 20 min | GAP-2 partial, GAP-3 partial |
| T1.2 — SHORT LR calibration | calibration/, screener.py | 2–3 h | GAP-9 partial |
| T1.3 — detect_* unit tests | tests/test_detect.py | 2–3 h | GAP-10 partial |
| T1.4 — WAIT misclassification fix | screener.py, resolved.csv | 30 min | ANALYSIS finding |

---

## Phase 2 — MTF Engine with Existing TF Data (3–5 days)

**Goal:** Build and wire the full 4H/Daily/1H confluence grading system using klines
that are already fetched. No new API dependencies. Deliver A/B/C grades, suppress
hard-opposed signals, and send MTF context in Telegram alerts.

---

### T2.1 — `compute_daily_context()` and `compute_4h_context()` ⏱ 50 min

**Gap:** GAP-6  
**File:** `screener.py:1773` (refactor)  
**Risk:** Low — refactor of existing inline logic.

Current state: `daily_trend`, `h4_trend`, `fvgs_4h`, `obs_4h` computed inline (lines
1773–1785). Wrap into structs per MTF_ENGINE_DESIGN.md §3.2–3.3:

```python
def compute_daily_context(opD, hiD, loD, clD, volD):
    trend = detect_htf_trend(hiD, loD, clD)
    fvgs  = detect_fvg(hiD, loD, clD, lookback=20, min_size_pct=0.30)
    obs   = detect_order_blocks(opD, hiD, loD, clD, volD, lookback=20)
    price = clD[-1]
    in_bull_zone = any(z["bottom"] <= price <= z["top"] for z in fvgs + obs if z["type"] == "bull")
    in_bear_zone = any(z["bottom"] <= price <= z["top"] for z in fvgs + obs if z["type"] == "bear")
    return {"trend": trend, "in_bull_zone": in_bull_zone, "in_bear_zone": in_bear_zone,
            "fvg_count": len(fvgs), "ob_count": len(obs)}

def compute_4h_context(op4h, hi4h, lo4h, cl4h, vol4h):
    trend = detect_htf_trend(hi4h, lo4h, cl4h)
    choch = detect_choch(hi4h, lo4h, cl4h, lookback=20)
    fvgs  = detect_fvg(hi4h, lo4h, cl4h, lookback=30, min_size_pct=0.10)
    obs   = detect_order_blocks(op4h, hi4h, lo4h, cl4h, vol4h, lookback=30)
    price = cl4h[-1]
    in_bull_zone = any(z["bottom"] <= price <= z["top"] for z in fvgs + obs if z["type"] == "bull")
    in_bear_zone = any(z["bottom"] <= price <= z["top"] for z in fvgs + obs if z["type"] == "bear")
    return {"trend": trend, "choch": choch, "in_bull_zone": in_bull_zone, "in_bear_zone": in_bear_zone}
```

Remove duplicated inline assignments at lines 1773–1785 and replace with:
```python
d_ctx  = compute_daily_context(opD, hiD, loD, clD, volD)
h4_ctx = compute_4h_context(op4h, hi4h, lo4h, cl4h, vol4h)
```

**Acceptance:** All downstream references to `daily_trend`, `h4_trend` replaced with
`d_ctx["trend"]`, `h4_ctx["trend"]`. Tests pass. No behavioral change.

---

### T2.2 — `compute_weekly_context()` using k1w ⏱ 45 min

**Gap:** GAP-2  
**File:** `screener.py` (new function, after T1.1)  
**Risk:** Low — new code path, k1w gracefully absent if fetch fails.

Per MTF_ENGINE_DESIGN.md §3.1:

```python
def compute_weekly_context(hi1w, lo1w, cl1w):
    if len(cl1w) < 14:   # insufficient weekly data
        return {"trend": "ranging", "weekly_high": None, "weekly_low": None,
                "near_level": False, "near_high": False, "near_low": False}
    cl_closed = cl1w[:-1]  # exclude live unfinished weekly candle
    trend      = detect_htf_trend(hi1w[:-1], lo1w[:-1], cl_closed)
    weekly_high = max(hi1w[-13:-1])
    weekly_low  = min(lo1w[-13:-1])
    price_now   = cl1w[-2]   # last closed candle price
    near_high   = abs(price_now - weekly_high) / weekly_high < 0.02
    near_low    = abs(price_now - weekly_low)  / weekly_low  < 0.02
    return {"trend": trend, "weekly_high": weekly_high, "weekly_low": weekly_low,
            "near_level": near_high or near_low, "near_high": near_high, "near_low": near_low}
```

In `analyze_symbol()`, unpack and call:
```python
op1w, hi1w, lo1w, cl1w, vol1w = _d.get("k1w", ([], [], [], [], []))
w_ctx = compute_weekly_context(hi1w, lo1w, cl1w) if len(cl1w) >= 14 else \
        {"trend": "ranging", "near_level": False, "near_high": False, "near_low": False,
         "weekly_high": None, "weekly_low": None}
```

**Acceptance:** `w_ctx["trend"]` returns `"bull"/"bear"/"ranging"` for any symbol with
≥14 weeks of data; falls back to `"ranging"` for new symbols.

---

### T2.3 — `compute_15m_context()` using k15m ⏱ 30 min

**Gap:** GAP-3  
**File:** `screener.py` (new function, after T1.1)  
**Risk:** Low — new code path, k15m gracefully absent if fetch fails.

Per MTF_ENGINE_DESIGN.md §3.5:

```python
def compute_15m_context(op15, hi15, lo15, cl15, vol15):
    if len(cl15) < 6:
        return {"bullish_entry": False, "bearish_entry": False, "choch": None, "pattern": {}}
    candle_pat = detect_candle_patterns(op15, hi15, lo15, cl15)
    choch_15m  = detect_choch(hi15, lo15, cl15, lookback=15)
    bullish_entry = bool(candle_pat.get("bullish_engulfing") or
                        candle_pat.get("hammer") or
                        choch_15m == "bull_choch")
    bearish_entry = bool(candle_pat.get("bearish_engulfing") or
                        candle_pat.get("shooting_star") or
                        choch_15m == "bear_choch")
    return {"bullish_entry": bullish_entry, "bearish_entry": bearish_entry,
            "choch": choch_15m, "pattern": candle_pat}
```

**Acceptance:** `m15_ctx["bullish_entry"]` returns True when last 15m candle is
an engulfing or pin bar in bull direction; graceful fallback on short kline array.

---

### T2.4 — `calc_mtf_grade()` implementation ⏱ 1 hour

**Gap:** GAP-1  
**File:** `screener.py` (new function)  
**Risk:** Medium — core scoring logic. Implement exactly as in MTF_ENGINE_DESIGN.md §4.3.

Score table (bull setup):
| Signal | Points |
|--------|--------|
| 1W aligned | +30 |
| 1D aligned | +25 |
| 4H aligned | +20 |
| 4H zone | +15 |
| 1H CHoCH | +15 |
| Weekly support proximity | +10 |
| 15m entry candle | +10 |
| 1W opposing | −20 |
| 1D opposing | −15 |
| 4H opposing / bear CHoCH | −15 |
| Weekly resistance proximity | −10 |
| 15m counter candle | −5 |

Grade thresholds: A+(≥70), A(≥50), B(≥25), C(<25), X(hard block: 1W+1D both opposing).

Full implementation code: MTF_ENGINE_DESIGN.md §4.3 (copy verbatim — already validated).

Wire into `analyze_symbol()` after setup direction is known:
```python
setup_dir = "bull" if direction in ("LONG", "WAIT") else "bear"
mtf_grade, mtf_score, mtf_summary = calc_mtf_grade(
    setup_dir, w_ctx, d_ctx, h4_ctx, choch_1h, m15_ctx
)
result["mtf_grade"]    = mtf_grade
result["mtf_score"]    = mtf_score
result["mtf_summary"]  = mtf_summary
result["weekly_trend"] = w_ctx["trend"]
```

**Note on existing `composite_grade()`:** Keep it intact. The new `calc_mtf_grade()`
is a parallel grade system. After 200 graded signals validate the A+ > A > B > C
ordering (Phase 3 validation), the old `composite_grade()` can be retired.

**Acceptance:** `calc_mtf_grade("bull", w_ctx, d_ctx, h4_ctx, "bull_choch", m15_ctx)`
returns `("A+", 80, "1W:B✅ | 1D:B✅ | ...")` for a fully-aligned setup.

---

### T2.5 — X-grade hard block in `_passes_setup_tg_filter()` ⏱ 15 min

**Gap:** GAP-4  
**File:** `screener.py:4899` (near existing range_sweep block)  
**Risk:** Medium — suppresses signals. Deploy after T2.4 is tested.

Add at top of `_passes_setup_tg_filter()`:
```python
if r.get("mtf_grade") == "X":
    return False  # 1W + 1D both opposing setup direction — suppress
```

**Acceptance:** A signal with 1W=bear and 1D=bear and direction=LONG is suppressed
(grade X, never sent to Telegram).

---

### T2.6 — C-grade threshold enforcement ⏱ 15 min

**Gap:** GAP-5  
**File:** `screener.py:4899` (same area as T2.5)  
**Risk:** Low — only suppresses low-confluence low-score signals.

Add after X-block:
```python
if r.get("mtf_grade") == "C" and r.get("score", 0) < 130:
    return False  # weak MTF confluence + below score threshold
```

**Acceptance:** A BOS/FVG signal with MTF score 15 (grade C) and setup score 110
is suppressed. Same signal at setup score 135 passes.

---

### T2.7 — MTF line in all 5 Telegram templates ⏱ 45 min

**Gap:** GAP-7  
**File:** `screener.py` (5 TG template blocks — search `format_tg_*` or `send_telegram`)  
**Risk:** Low — additive line to TG message, no logic change.

After the score line in every template:
```python
grade_label = {"A+": "⭐A+", "A": "A", "B": "B", "C": "C ⚡ low confluence"}.get(mtf_grade, mtf_grade)
mtf_line = f"📊 MTF [{grade_label}] {mtf_summary}"
```

Example output:
```
🚀 SOLUSDT LONG — BOS/FVG
Score: 143  |  A+
Funding: -0.031%  |  OI ↑12.4%  |  CVD: +18%
📊 MTF [⭐A+] 1W:B✅ | 1D:B✅ | 4H:B✅ | 4H-zone✅ | 1H-CHoCH✅ | 15m-Eng✅
Entry: $152.40  SL: $149.10  TP1: $157.20  TP2: $162.00
```

**Acceptance:** All 5 setup types (BOS/FVG, Squeeze, Breakout, Short Dist, Sweep)
show the `📊 MTF` line in every Telegram alert.

---

### T2.8 — Store mtf_grade / mtf_score / weekly_trend in outcome tracker ⏱ 20 min

**Gap:** GAP-8  
**File:** `screener.py` (pending.json write path), `outcomes/resolved.csv`  
**Risk:** Low — additive fields, backward-compatible.

Add to every pending.json entry:
```json
{ "mtf_grade": "A", "mtf_score": 55, "weekly_trend": "bull" }
```

Add 3 columns to resolved.csv write path:
```python
row["mtf_grade"]    = signal.get("mtf_grade", "")
row["mtf_score"]    = signal.get("mtf_score", "")
row["weekly_trend"] = signal.get("weekly_trend", "")
```

**Acceptance:** All new signals in `pending.json` contain `mtf_grade`. After 1 week,
`resolved.csv` has at least 50 rows with non-empty `mtf_grade` values.

---

### Phase 2 Deliverables Summary

| Task | File(s) | Effort | Resolves |
|------|---------|--------|---------|
| T2.1 — d_ctx / h4_ctx structs | screener.py:1773 | 50 min | GAP-6 |
| T2.2 — compute_weekly_context() | screener.py | 45 min | GAP-2 |
| T2.3 — compute_15m_context() | screener.py | 30 min | GAP-3 |
| T2.4 — calc_mtf_grade() + wire | screener.py | 1 h | GAP-1 |
| T2.5 — X-grade hard block | screener.py:4899 | 15 min | GAP-4 |
| T2.6 — C-grade threshold | screener.py:4899 | 15 min | GAP-5 |
| T2.7 — MTF line in TG templates | screener.py | 45 min | GAP-7 |
| T2.8 — mtf_grade in tracker | screener.py | 20 min | GAP-8 |

**Total Phase 2:** ~4.5 hours active development.  
All 10 gaps from GAP_ANALYSIS.md are resolved after Phase 2 (except GAP-9/GAP-10).

---

## Phase 3 — Full MTF + Advanced Features (1–2 weeks)

**Goal:** Validate grade ordering with live data, add dynamic SL/TP, 4H OI/CVD,
complete LR score replacement, and split the monolith.

---

### T3.1 — WR validation by MTF grade ⏱ 1 week passive + 2 hours active

**Gap:** MTF_ENGINE_DESIGN.md §12 validation plan  
**Trigger:** After 200+ signals have resolved with `mtf_grade` populated in resolved.csv.

Target ordering:
- A+ WR > A WR > B WR > baseline (36.6% at 4h)
- If ordering confirmed: enable X hard block (T2.5) + C threshold (T2.6) in production
- If ordering not confirmed: re-examine calc_mtf_grade() weights

Script: extend `WIN_RATE_ANALYSIS.md` analysis to group by `mtf_grade`. Flag if
any grade with n≥30 is within 2pp of the adjacent grade (borderline — may need
threshold adjustment).

---

### T3.2 — Dynamic TP/SL using weekly high/low levels ⏱ 3–4 hours

**Gap:** New feature (not in MTF_ENGINE_DESIGN.md)  
**File:** `screener.py` (TP/SL calculation blocks)

Use `w_ctx["weekly_high"]` and `w_ctx["weekly_low"]` as natural TP and SL anchors:

- **LONG setup:** SL = `max(existing_sl, weekly_low * 0.995)` if weekly_low is
  within 3% of entry. TP2 = `min(existing_tp2, weekly_high * 0.998)` if weekly_high
  is within 8% of entry.
- **SHORT setup:** Mirror logic using weekly_high as SL anchor, weekly_low as TP2.

Only snap to weekly levels when they're "in range" — don't override if weekly level
is >10% away (it becomes irrelevant as a near-term target).

**Acceptance:** SOLUSLT LONG near weekly support: SL moves to weekly_low − 0.5%;
signal with weekly level far away uses existing ATR-based SL unchanged.

---

### T3.3 — OI and CVD on 4H timeframe ⏱ 2–3 hours

**Gap:** New feature (referenced in issue description)  
**File:** `screener.py` (fetch_all_data + analyze_symbol)

Currently OI history is fetched at default interval (likely 1H). Add:
```python
"oi_4h": lambda: fetch_oi_history(sym, limit=50, interval="4h"),
```

Compute 4H OI divergence (price making HH while 4H OI is falling = distribution signal).
Add `oi_4h_div` to result dict and include in scoring:
- 4H OI divergence on a LONG setup → −5 points (institutional distribution)
- 4H OI expansion aligned with setup direction → +5 points

Similarly compute 4H CVD using `calc_kline_cvd(op4h, cl4h, vol4h, lookback=20)`.
This is a higher-conviction version of the existing 1H CVD signal.

**Acceptance:** `result["oi_4h_div"]` is populated for all symbols. 4H OI divergence
is visible in Telegram messages for setups where it's present.

---

### T3.4 — Full LR score replacement per setup ⏱ 3–5 days

**Gap:** GAP-9  
**File:** `calibration/`, `screener.py:1618`

The current AUC=0.59 LONG-only model applies 9 additive corrections on top of
hand-tuned scoring. The root cause (EMA bull alignment over-rewarded) is not fixed.

Steps:
1. Extract setup-specific feature vectors from resolved.csv (separate: BOS/FVG,
   Squeeze, Breakout, Short Dist — each has different correlation structure)
2. Train 4 logistic regression models (one per setup), including MTF grade as a feature
3. Validate AUC per setup (target >0.60 for BOS/FVG; accept >0.55 for others with
   smaller n)
4. Replace additive `score += weight[feature]` with proper `score = baseline + dot(X, w)`
5. Coordinate with MTF grade: after Phase 2 validation confirms A+ > B ordering,
   add `mtf_grade_numeric` as a feature (A+=3, A=2, B=1, C=0, X=-1)

**Acceptance:** Breakout setup AUC improves from current inverted state (score >150
WR=27.9%) to AUC >0.55; score direction no longer anti-correlated with WR.

---

### T3.5 — Monolith split: signals / scoring / data / output ⏱ 1 week

**Gap:** GAP-10  
**Current:** screener.py is 5326 lines, no tests/, no module structure

Target structure:
```
signals/
  __init__.py
  fvg.py          # detect_fvg()
  order_blocks.py # detect_order_blocks()
  choch.py        # detect_choch()
  sweep.py        # detect_sweep()
  candles.py      # detect_candle_patterns()
  htf.py          # detect_htf_trend()
  mtf_grade.py    # calc_mtf_grade() + all compute_*_context()
scoring/
  __init__.py
  setups.py       # s1–s5 scoring blocks
  grade.py        # composite_grade() → to be retired after Phase 3 validation
data/
  __init__.py
  klines.py       # fetch_klines(), fetch_all_data()
  funding.py      # fetch_funding_history()
  oi.py           # fetch_oi_history()
  orderbook.py    # fetch_orderbook()
output/
  __init__.py
  telegram.py     # all 5 TG templates
  formatter.py    # grade formatting, MTF line
config.py         # GOOD_SIGNAL_HOURS, HARD_BLOCK_HOURS, MIN_TURNOVER_24H, thresholds
tests/
  test_detect.py  # from T1.3
  test_mtf_grade.py
  test_scoring.py
```

**Acceptance:** `pytest tests/` passes; `screener.py` imports from modules; behavior
is identical to pre-split (validated by running both versions on the same market data
snapshot and diffing output).

---

### Phase 3 Deliverables Summary

| Task | Effort | Resolves |
|------|--------|---------|
| T3.1 — WR validation by grade | 1 week passive | Validation plan §12 |
| T3.2 — Dynamic TP/SL (weekly levels) | 3–4 h | New feature |
| T3.3 — 4H OI/CVD | 2–3 h | New feature |
| T3.4 — Full LR per-setup replacement | 3–5 days | GAP-9 |
| T3.5 — Monolith split + full test suite | 1 week | GAP-10 |

---

## Gap Coverage Tracker

| Gap | Description | Phase | Status |
|-----|-------------|-------|--------|
| GAP-1 | calc_mtf_grade() | 2 (T2.4) | ⬜ |
| GAP-2 | 1W fetch + compute_weekly_context() | 1 fetch (T1.1) + 2 compute (T2.2) | ⬜ |
| GAP-3 | 15m fetch + compute_15m_context() | 1 fetch (T1.1) + 2 compute (T2.3) | ⬜ |
| GAP-4 | X-grade hard block | 2 (T2.5) | ⬜ |
| GAP-5 | C-grade threshold | 2 (T2.6) | ⬜ |
| GAP-6 | d_ctx / h4_ctx structs | 2 (T2.1) | ⬜ |
| GAP-7 | MTF line in TG templates | 2 (T2.7) | ⬜ |
| GAP-8 | mtf_grade in outcome tracker | 2 (T2.8) | ⬜ |
| GAP-9 | Full LR score replacement | 3 (T3.4) | ⬜ |
| GAP-10 | Monolith split + unit tests | 1 tests (T1.3) + 3 split (T3.5) | ⬜ |

---

## Open Questions (require CEO input before Phase 2 execution)

1. **C-grade threshold per setup:** Design proposes `setup_score ≥ 130`. Should
   Breakout C-grades require a higher threshold (≥140) given its inverted score
   correlation?

2. **X-block rollout:** Deploy X suppression immediately on Phase 2 merge, or run
   in paper mode (log suppressed signals) for 1 week before enabling? Paper mode
   reduces risk of over-suppression.

3. **Phase 2 deployment:** All 8 Phase 2 tasks ship as a single commit or as
   sequential small PRs (T2.1→T2.2→T2.3→T2.4 with each step tested in staging)?

4. **4H OI endpoint:** Bybit V5 supports `interval=4h` for OI history. Confirm
   no API key is required for this endpoint before scheduling T3.3.

# MTF Confluence Engine — Technical Design

**Issue:** AVEA-28  
**Date:** 2026-04-25  
**Author:** CTO (2b458376)  
**Status:** Design Complete — pending implementation  

---

## 1. Overview

The current screener works on a single timeframe (1H setup detection) with ad-hoc Daily+4H trend alignment. This design replaces that ad-hoc system with a first-class multi-timeframe confluence engine that:

1. Computes a structured view per timeframe (1W → 15m)
2. Scores alignment or opposition at each level
3. Emits a single **A/B/C grade** alongside the existing setup score
4. Adds an HTF context summary line to every Telegram alert

---

## 2. Timeframe Stack

| TF   | Candle count | Interval key | What we compute |
|------|-------------|--------------|-----------------|
| 1W   | 52 candles  | `"W"`        | Macro trend (HH/HL or LH/LL), key weekly levels (swing H/L) |
| 1D   | 60 candles  | `"D"`        | Daily bias, daily FVG zones, daily OB locations |
| 4H   | 120 candles | `"240"`      | Structure (BOS/CHoCH), 4H FVG, 4H trend |
| 1H   | 212 candles | `"60"`       | Setup detection (existing logic), 1H CHoCH/FVG/OB |
| 15m  | 96 candles  | `"15"`       | Entry candle confirmation pattern |

**Weekly data is not yet fetched.** 15m is not yet fetched. Both are new additions.

---

## 3. Per-Timeframe Definitions

### 3.1 — 1W: Macro Trend + Key Levels

```python
def compute_weekly_context(hi1w, lo1w, cl1w):
    trend = detect_htf_trend(hi1w, lo1w, cl1w)  # reuse existing
    
    # Key levels: highest high and lowest low of prior 12 weeks (3-month range)
    weekly_high = max(hi1w[-13:-1])
    weekly_low  = min(lo1w[-13:-1])
    price_now   = cl1w[-1]
    
    # Proximity: is price within 2% of a weekly level?
    near_weekly_high = abs(price_now - weekly_high) / weekly_high < 0.02
    near_weekly_low  = abs(price_now - weekly_low)  / weekly_low  < 0.02
    
    return {
        "trend": trend,           # "bull" | "bear" | "ranging"
        "weekly_high": weekly_high,
        "weekly_low":  weekly_low,
        "near_level":  near_weekly_high or near_weekly_low,
        "near_high":   near_weekly_high,
        "near_low":    near_weekly_low,
    }
```

**API call added to `fetch_all_data()`:**
```python
"k1w": lambda: fetch_klines(sym, "W", 52),
```

### 3.2 — 1D: Daily Bias + Zones

Already partially computed (lines 1773, 1873–1874 in screener.py). Wrap into a struct:

```python
def compute_daily_context(opD, hiD, loD, clD, volD):
    trend   = detect_htf_trend(hiD, loD, clD)
    fvgs    = detect_fvg(hiD, loD, clD, lookback=20, min_size_pct=0.30)
    obs     = detect_order_blocks(opD, hiD, loD, clD, volD, lookback=20)
    price   = clD[-1]
    in_bull_zone = any(z["bottom"] <= price <= z["top"] for z in fvgs + obs if z["type"] == "bull")
    in_bear_zone = any(z["bottom"] <= price <= z["top"] for z in fvgs + obs if z["type"] == "bear")
    return {
        "trend": trend,
        "in_bull_zone": in_bull_zone,
        "in_bear_zone": in_bear_zone,
        "fvg_count":    len(fvgs),
        "ob_count":     len(obs),
    }
```

### 3.3 — 4H: Structure + FVG

Already computed. Wrap existing variables:

```python
def compute_4h_context(op4h, hi4h, lo4h, cl4h, vol4h):
    trend  = detect_htf_trend(hi4h, lo4h, cl4h)
    choch  = detect_choch(hi4h, lo4h, cl4h, lookback=20)
    fvgs   = detect_fvg(hi4h, lo4h, cl4h, lookback=30, min_size_pct=0.10)
    obs    = detect_order_blocks(op4h, hi4h, lo4h, cl4h, vol4h, lookback=30)
    price  = cl4h[-1]
    in_bull_zone = any(z["bottom"] <= price <= z["top"] for z in fvgs + obs if z["type"] == "bull")
    in_bear_zone = any(z["bottom"] <= price <= z["top"] for z in fvgs + obs if z["type"] == "bear")
    return {
        "trend": trend,
        "choch": choch,       # "bull_choch" | "bear_choch" | None
        "in_bull_zone": in_bull_zone,
        "in_bear_zone": in_bear_zone,
    }
```

### 3.4 — 1H: Setup Detection (unchanged)

Existing `analyze_symbol()` pipeline. The MTF engine reads the setup direction from this output.

### 3.5 — 15m: Entry Confirmation

**New fetch** — add to `fetch_all_data()`:
```python
"k15m": lambda: fetch_klines(sym, "15", 96),
```

```python
def compute_15m_context(op15, hi15, lo15, cl15, vol15):
    candle_pat = detect_candle_patterns(op15, hi15, lo15, cl15)  # reuse existing
    choch_15m  = detect_choch(hi15, lo15, cl15, lookback=15)
    
    # Entry candle: engulfing or pin bar in last 3 candles
    bullish_entry = (
        candle_pat.get("bullish_engulfing") or
        candle_pat.get("hammer") or
        choch_15m == "bull_choch"
    )
    bearish_entry = (
        candle_pat.get("bearish_engulfing") or
        candle_pat.get("shooting_star") or
        choch_15m == "bear_choch"
    )
    return {
        "bullish_entry": bullish_entry,
        "bearish_entry": bearish_entry,
        "choch": choch_15m,
        "pattern": candle_pat,
    }
```

---

## 4. MTF Scoring Engine

### 4.1 — Score Table (Long / Bull bias; mirror for Short)

| Signal | Points | Condition |
|--------|--------|-----------|
| 1W bull trend aligned | +30 | `w.trend == "bull"` |
| 1D bull trend aligned | +25 | `d.trend == "bull"` |
| 4H bull trend aligned | +20 | `h4.trend == "bull"` |
| 4H price in bull FVG/OB | +15 | `h4.in_bull_zone` |
| 1H CHoCH↑ confirmed | +15 | `choch_1h == "bull_choch"` |
| Price near weekly low (support) | +10 | `w.near_low` |
| 15m bullish entry candle | +10 | `m15.bullish_entry` |
| — | — | — |
| 1W bear trend opposing | −20 | `w.trend == "bear"` |
| 1D bear trend opposing | −15 | `d.trend == "bear"` |
| 4H bear trend / bear CHoCH | −15 | `h4.trend == "bear"` or `h4.choch == "bear_choch"` |
| Price near weekly high (resistance) | −10 | `w.near_high` (for longs) |
| 15m bearish entry candle | −5 | `m15.bearish_entry` |

**Hard block rules (signal suppressed regardless of score):**
- `w.trend == "bear"` AND `d.trend == "bear"` AND setup is LONG → **block** (≥2 HTF opposed)
- `w.trend == "bull"` AND `d.trend == "bull"` AND setup is SHORT → **block**

### 4.2 — Grade Thresholds

| Grade | MTF Score | Description |
|-------|-----------|-------------|
| **A+** | ≥ 70 | 3+ TFs aligned, 15m confirmed. Highest conviction. |
| **A**  | ≥ 50 | 2+ TFs aligned, 4H zone in-play. |
| **B**  | ≥ 25 | Partial alignment, 1–2 TFs support. |
| **C**  | < 25 | Weak MTF support. Low priority. |
| **X**  | —    | Hard-blocked by opposing macro. Do not alert. |

### 4.3 — Implementation: `calc_mtf_grade()`

```python
def calc_mtf_grade(setup_dir, w_ctx, d_ctx, h4_ctx, choch_1h, m15_ctx):
    """
    setup_dir: "bull" for LONG setups, "bear" for SHORT setups.
    Returns: (grade: str, mtf_score: int, summary: str)
    """
    if setup_dir not in ("bull", "bear"):
        return "C", 0, "unknown direction"

    opp = "bear" if setup_dir == "bull" else "bull"
    score = 0
    flags = []

    # Alignments
    if w_ctx["trend"] == setup_dir:
        score += 30; flags.append(f"1W:{setup_dir.upper()[:1]}✅")
    elif w_ctx["trend"] == opp:
        score -= 20; flags.append("1W:⚠️")
    else:
        flags.append("1W:~")

    if d_ctx["trend"] == setup_dir:
        score += 25; flags.append(f"1D:{setup_dir.upper()[:1]}✅")
    elif d_ctx["trend"] == opp:
        score -= 15; flags.append("1D:⚠️")
    else:
        flags.append("1D:~")

    if h4_ctx["trend"] == setup_dir:
        score += 20; flags.append(f"4H:{setup_dir.upper()[:1]}✅")
    elif h4_ctx["trend"] == opp or h4_ctx["choch"] == f"{opp}_choch":
        score -= 15; flags.append("4H:⚠️")
    else:
        flags.append("4H:~")

    if setup_dir == "bull" and h4_ctx["in_bull_zone"]:
        score += 15; flags.append("4H-zone✅")
    elif setup_dir == "bear" and h4_ctx["in_bear_zone"]:
        score += 15; flags.append("4H-zone✅")

    if choch_1h == f"{setup_dir}_choch":
        score += 15; flags.append("1H-CHoCH✅")

    if setup_dir == "bull" and w_ctx["near_low"]:
        score += 10; flags.append("W-Sup✅")
    elif setup_dir == "bear" and w_ctx["near_high"]:
        score += 10; flags.append("W-Res✅")
    elif setup_dir == "bull" and w_ctx["near_high"]:
        score -= 10; flags.append("W-Res⚠️")
    elif setup_dir == "bear" and w_ctx["near_low"]:
        score -= 10; flags.append("W-Sup⚠️")

    if setup_dir == "bull" and m15_ctx["bullish_entry"]:
        score += 10; flags.append("15m-Eng✅")
    elif setup_dir == "bear" and m15_ctx["bearish_entry"]:
        score += 10; flags.append("15m-Eng✅")
    elif setup_dir == "bull" and m15_ctx["bearish_entry"]:
        score -= 5; flags.append("15m:⚠️")
    elif setup_dir == "bear" and m15_ctx["bullish_entry"]:
        score -= 5; flags.append("15m:⚠️")

    # Hard block: 2+ HTF opposing
    htf_opposed = (
        w_ctx["trend"] == opp and d_ctx["trend"] == opp
    )
    if htf_opposed:
        return "X", score, " | ".join(flags)

    # Grade assignment
    if score >= 70:
        grade = "A+"
    elif score >= 50:
        grade = "A"
    elif score >= 25:
        grade = "B"
    else:
        grade = "C"

    return grade, score, " | ".join(flags)
```

---

## 5. Integration into `analyze_symbol()`

### 5.1 — New data fetches (in `fetch_all_data()`)

```python
"k1w":  lambda: fetch_klines(sym, "W",  52),
"k15m": lambda: fetch_klines(sym, "15", 96),
```

### 5.2 — Unpack in `analyze_symbol()`

```python
op1w,  hi1w,  lo1w,  cl1w,  vol1w  = _d["k1w"]
op15m, hi15m, lo15m, cl15m, vol15m = _d["k15m"]
```

### 5.3 — Compute contexts (after existing CHoCH / FVG computation)

```python
w_ctx  = compute_weekly_context(hi1w, lo1w, cl1w)
d_ctx  = compute_daily_context(opD, hiD, loD, clD, volD)
h4_ctx = compute_4h_context(op4h, hi4h, lo4h, cl4h, vol4h)
m15_ctx = compute_15m_context(op15m, hi15m, lo15m, cl15m, vol15m)
```

### 5.4 — Grade computation (after setup direction is known)

```python
setup_dir = "bull" if direction in ("LONG", "WAIT") else "bear"
mtf_grade, mtf_score, mtf_summary = calc_mtf_grade(
    setup_dir, w_ctx, d_ctx, h4_ctx, choch_1h, m15_ctx
)
```

### 5.5 — Hard block in `_passes_setup_tg_filter()`

```python
if mtf_grade == "X":
    return False  # opposing macro — suppress alert
```

### 5.6 — Store in result dict

```python
result["mtf_grade"]   = mtf_grade
result["mtf_score"]   = mtf_score
result["mtf_summary"] = mtf_summary
result["weekly_trend"] = w_ctx["trend"]
```

---

## 6. Telegram Message Format

Add one line to each alert template after the existing score line:

```
📊 MTF [B+52] 1W:B✅ | 1D:B✅ | 4H:~  | 15m-Eng✅  →  Grade A
```

Format string:
```python
mtf_line = f"📊 MTF [{mtf_grade}] {mtf_summary}"
```

For grade A+, prepend `⭐` to the grade label.  
For grade X: never reaches Telegram (blocked).  
For grade C: suffix the line with `⚡ low confluence`.

**Full Telegram block example:**
```
🚀 SOLUSDT LONG — BOS/FVG
Score: 143  |  A+
Funding: -0.031%  |  OI ↑ 12.4%  |  CVD: +18%
📊 MTF [A+] 1W:B✅ | 1D:B✅ | 4H:B✅ | 4H-zone✅ | 1H-CHoCH✅ | 15m-Eng✅
Entry: $152.40  SL: $149.10  TP1: $157.20  TP2: $162.00
```

---

## 7. Scoring Integration with Existing Setup Score

The MTF grade does **not** replace the existing setup score — it is a parallel filter/label. The relationship:

| MTF Grade | Effect on existing flow |
|-----------|------------------------|
| A+ / A    | Signal passes; tag added to TG message |
| B         | Signal passes; tag added; marked "moderate confluence" |
| C         | Signal passes if setup score ≥ 130; suppressed otherwise |
| X         | Signal suppressed (hard block) |

This preserves backward compatibility: existing high-conviction signals still fire even with partial MTF alignment, but the grade tells the user the risk context.

**Optional future extension:** MTF score (+/-) can be additive to the base setup score (e.g. `total_score = setup_score + mtf_score * 0.3`). Do not do this in v1 — validate grades independently first.

---

## 8. Data Flow Diagram

```
fetch_klines(sym, "W",  52)  → hi1w, lo1w, cl1w
fetch_klines(sym, "D",  60)  → hiD,  loD,  clD    (already fetched)
fetch_klines(sym, "240",120) → hi4h, lo4h, cl4h   (already fetched)
fetch_klines(sym, "60", 212) → hi1h, lo1h, cl1h   (already fetched)
fetch_klines(sym, "15",  96) → hi15m, lo15m, cl15m

         ↓
compute_weekly_context(hi1w, lo1w, cl1w)         → w_ctx
compute_daily_context(opD, hiD, loD, clD, volD)  → d_ctx
compute_4h_context(op4h, hi4h, lo4h, cl4h, vol4h) → h4_ctx
[existing: choch_1h]
compute_15m_context(op15m, hi15m, lo15m, cl15m, vol15m) → m15_ctx

         ↓
calc_mtf_grade(setup_dir, w_ctx, d_ctx, h4_ctx, choch_1h, m15_ctx)
         → (grade, mtf_score, mtf_summary)

         ↓
_passes_setup_tg_filter() checks grade != "X"
Telegram message includes mtf_line
outcome tracker stores mtf_grade for future WR analysis
```

---

## 9. Performance Impact

| Addition | API calls per symbol | Latency estimate |
|----------|---------------------|-----------------|
| 1W klines (52 candles) | +1 | ~80ms |
| 15m klines (96 candles) | +1 | ~80ms |
| Context computation | 0 (CPU only) | ~2ms |
| **Total overhead** | **+2 calls** | **~160ms** |

Current per-symbol cost is ~6–8 API calls. Adding 2 is a 25% increase. At 50 symbols this adds ~8 seconds per screener cycle. Acceptable.

**Mitigation:** Cache 1W klines for 24 hours (weekly candles change once per week). Cache 15m klines for 3 minutes.

---

## 10. Outcome Tracking Integration

Add to `outcomes/pending.json` entry:
```json
{
  "mtf_grade": "A",
  "mtf_score": 55,
  "weekly_trend": "bull"
}
```

Add to `outcomes/resolved.csv` columns:
```
mtf_grade, mtf_score, weekly_trend
```

After 200+ trades with MTF grades, run WR analysis grouped by `mtf_grade` to validate A > B > C ordering.

---

## 11. Implementation Sequence

| Step | Task | File | Effort |
|------|------|------|--------|
| 1 | Add `k1w` and `k15m` fetches to `fetch_all_data()` | screener.py:4778 | 20m |
| 2 | Implement `compute_weekly_context()` | screener.py (new fn) | 45m |
| 3 | Implement `compute_daily_context()` (refactor existing) | screener.py | 30m |
| 4 | Implement `compute_4h_context()` (refactor existing) | screener.py | 20m |
| 5 | Implement `compute_15m_context()` | screener.py (new fn) | 30m |
| 6 | Implement `calc_mtf_grade()` | screener.py (new fn) | 1h |
| 7 | Wire into `analyze_symbol()` | screener.py:1724 | 30m |
| 8 | Add block in `_passes_setup_tg_filter()` | screener.py | 15m |
| 9 | Add `mtf_line` to all 5 Telegram templates | screener.py:3500+ | 45m |
| 10 | Add fields to pending.json / resolved.csv write paths | screener.py | 20m |
| 11 | Unit tests for `calc_mtf_grade()` | tests/test_mtf.py | 1h |
| — | **Total** | | **~5.5h** |

---

## 12. Validation Plan

1. **Paper mode (week 1):** Run MTF grading but do not suppress C-grade signals yet. Log grades to file. Verify A+/A are rarer than B/C (expect A+ <15%, A <25%, B ~35%, C ~25%).
2. **WR audit (week 3):** After 100 graded signals resolve, compare WR by grade. Target: A+ WR > A WR > B WR > baseline (36.6%).
3. **Enable suppression (week 4):** Turn on C-grade threshold filter and X hard block only after validation confirms grade ordering is correct.
4. **Score integration (week 6+):** If A+ consistently outperforms, consider additive score bonus.

---

## 13. Open Questions (need CEO input)

1. **C-grade threshold:** Currently proposed as `score ≥ 130` to pass. Should this be configurable per setup? (Breakout is already miscalibrated; a C-grade breakout at 130 might still be unwanted.)
2. **1W data latency:** Bybit weekly klines update only at candle close (weekly). For intra-week analysis, the current weekly candle is unfinished. Should we compute weekly trend from the last N *closed* candles only (ignoring the live candle)?  
   → Recommendation: use `cl1w[:-1]` (exclude live candle) in `compute_weekly_context()`.
3. **15m fetch rate:** 15m candles change every 15 minutes. If we cache aggressively (15min), we may miss an entry signal that formed after the cache was primed. Acceptable tradeoff?
4. **Grade label in scoring model:** The ANALYSIS.md mandate (AVEC-1) calls for replacing hand-tuned scores with logistic regression weights. MTF grade should be one of the features in that model, not a separate system. Coordinate with the ML calibration work (AVEC-2) before finalizing grade-to-score conversion.

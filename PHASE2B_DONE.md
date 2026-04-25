# Phase 2B Complete — Medium/High-Risk Scoring Fixes

**Date:** 2026-04-25  
**Branch:** main  
**Issue:** AVEVA-42  
**Parent:** AVEVA-40  
**Phase 2A commit:** b5cc613

---

## Changes Made

### Finding 3 — CHoCH in SQUEEZE (HIGHEST RISK)

**File:** screener.py  
**Line:** 2174 (squeeze block, Setup 1)

| | Value |
|---|---|
| Old | `s1 += 25; n1.append("CHoCH↑1H!")` |
| New | `s1 -= 15; n1.append("CHoCH↑1H⚠")` |

**Rationale:** calibration model showed CHoCH in squeeze = WR -20.5pp (momentum already spent before CHoCH fires).

**Verified unchanged:**
- bos_fvg CHoCH: `s2 += 20` at line 2270 ✓
- breakout CHoCH: `s4 += 20` at line 2530 ✓

---

### Finding 2 — FUND_EXTREME point change (MEDIUM RISK)

**File:** screener.py  
**Lines:** 2011, 2021

| Change | Line | Old | New |
|---|---|---|---|
| extreme_neg scoring | 2011 | `s1 += 25` | `s1 -= 10` |
| optimal zone bonus | 2021 | *(new)* | `if -0.03 <= funding < 0: s1 += 15` |

**Rationale:** extreme_neg (< -0.08%) means squeeze already happened; optimal entry zone is moderately negative funding (-0.03% to 0%).

**Verified:**
- `FUND_EXTREME!` label preserved in n1.append ✓
- `detect_funding_extreme()` function unchanged ✓
- Other setups (s4 line 2464, s5 line 2642) untouched ✓

---

### Finding 7 — Day-of-week threshold multipliers (MEDIUM RISK)

**File:** screener.py  
**Lines:** 72–73 (constants), 5631–5652 (time gate)

| Constant | Line | Value |
|---|---|---|
| `FRIDAY_MIN_SCORE` | 72 | 169 = round(130 × 1.3) |
| `TUESDAY_MIN_SCORE` | 73 | 150 = round(130 × 1.15) |

**Time gate blocks added:**
- `elif _utc_weekday == 4:` → Friday filter at line 5631
- `elif _utc_weekday == 1:` → Tuesday filter at line 5642

**Verified:**
- `SATURDAY_MIN_SCORE = 195` unchanged ✓
- Friday is NOT a hard block (same soft pattern as Saturday) ✓
- Saturday pattern unchanged ✓

---

## Test Results

- 24/24 unit tests pass
- Syntax check: OK
- CHoCH verification: squeeze=-15, bos_fvg=+20, breakout=+20 ✓
- FUND_EXTREME flag preserved ✓

---

## Do NOT Implement (deferred to Phase 2C)

- Findings 1, 4, 5, 6 — already done in AVEVA-41 (b5cc613)
- Features A, B, C — Phase 2C (next subtask)

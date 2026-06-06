# Phase 1 — 4 Critical Bug Fixes

Date: 2026-04-25

## FIX 1 — HARD_BLOCK_HOURS corrected

**File:** `screener.py:66`

| Before | After |
|--------|-------|
| `{18, 22}` | `{18, 19}` |

UTC 22 was wrongly blocked (WR=55.6% — profitable). UTC 19 (WR=17.5% — worst hour) was wrongly allowed. Swapped.

## FIX 2 — Breakout score filter inverted correctly

**File:** `screener.py:4933`

| Before | After |
|--------|-------|
| `return score < 100 or score > 150` | `return score < 120` |

The old logic blocked score 100–120 (WR=53.1%) and allowed score >150 (WR=33.3%). Filter was inverted. New logic blocks only signals ≥120 where WR degrades to 33.3%.

## FIX 3 — range_sweep excluded from pending.json

**File:** `screener.py:5499`

range_sweep setups (WR=25%) are now excluded from `save_pending()`. They were already blocked from Telegram but still polluting `resolved.csv` with ~25% WR rows that corrupted ML training data.

## FIX 4 — Grade stamped at save time

**File:** `screener.py:5502`

`composite_grade(r)` is now called for each signal before `save_pending()`. Previously grade was only computed at render time and never stored — all 2,289 rows in `resolved.csv` had `grade="—"`, making the adaptive multiplier system completely inoperative.

## Verification

- `python3 -c "import ast; ast.parse(open('screener.py').read())"` → syntax OK
- Confirmed `range_sweep` entries will be skipped in `to_save` list
- Confirmed `grade` field will contain A+/A/B+/B/C/D values in pending.json going forward

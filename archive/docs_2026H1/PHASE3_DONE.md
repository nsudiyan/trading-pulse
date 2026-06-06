# Phase 3 — Architecture Improvements

Date: 2026-04-25

## TASK A — Weekly + 15m context functions

**New functions in `screener.py`:**
- `compute_weekly_context(op1w, hi1w, lo1w, cl1w, price)` — returns weekly trend ('bull'|'bear'|'range'|'unknown'), 4-week price position (0..1), weekly above/below 10-week EMA flag.
- `compute_15m_context(op15m, hi15m, lo15m, cl15m, price)` — returns 15m micro-trend ('bull'|'bear'|'neutral'), 20-bar EMA momentum %.

**Data flow:** k1w and k15m were already fetched in `_fetch_symbol_data_parallel()` but were unused downstream. Fixed by passing them through `sym_ctx` (global_ctx) in `_fetch_and_score()` so `score_symbol()` can extract and use them.

**New result dict fields added:**
- `weekly_trend`, `weekly_pos`, `weekly_above_ema`
- `m15_trend`, `m15_momentum`

## TASK B — calc_mtf_grade() function

**New function `calc_mtf_grade(r, setup_dir="long")`** implemented in `screener.py`.

Grade system: A+/A/B+/B/C/D/X

- **Grade X**: Weekly AND Daily both oppose signal direction → hard counter-trend, signal blocked.
- **Grade A+**: score ≥ 90 + MTF_ext ≥ 2 + trend aligned + CVD confirms + weekly aligned.
- **Grade A**: score ≥ 80 + MTF ≥ 2 OR score ≥ 90.
- **Grade B+**: score ≥ 70 + MTF ≥ 1.
- **Grade B**: score ≥ 55.
- **Grade C**: score ≥ 35.
- **Grade D**: < 35.

**Grade X hard block** also applied inline in `score_symbol()` (before building result dict) for signals where weekly + daily both oppose the setup direction — returns `None` to filter the signal completely.

**Usage updated:** `r["grade"] = composite_grade(r)` replaced with `r["grade"] = calc_mtf_grade(r, setup_dir=_sdir)` at save time.

## TASK C — Per-setup LR models

**Problem:** CHoCH coefficient differs per setup — a single pooled model averages incorrect values:
| Setup | CHoCH WR Lift | Correct Pts | Old Generic Pts |
|-------|--------------|-------------|-----------------|
| bos_fvg | +18.7pp | +14.96 | +5.0 |
| breakout | +15.3pp (empirical: +14.3pp) | +11.43 | +5.0 |
| squeeze | −20.5pp | −16.40 | +5.0 (wrong sign!) |

**Files produced by `calibration/train_model.py`:**
- `calibration/signal_weights_bos_fvg.json` — CHoCH = +14.96
- `calibration/signal_weights_breakout.json` — CHoCH = +11.43
- `calibration/signal_weights_squeeze.json` — CHoCH = −16.40

**`load_score_weights()` updated** to load per-setup files and store as `__signal_weights_bos_fvg__`, `__signal_weights_breakout__`, `__signal_weights_squeeze__`.

**Score application updated**: per-setup weights take priority over generic `__signal_weights__`. For squeeze with CHoCH, adjustment is now −16.40 pts instead of +5.0 pts — a 21.4 pt correction.

## TASK D — short_dist TP adjustment

**Problem:** short_dist signals have 4H WR = 44.6% but 24H WR = 38.5% — movement fades after 4H. Holding to TP2 (48h low) loses the edge.

**Fix in `build_trade_plan()`:** When `setup == "short_dist"`, after computing TP2, override `tp2 = tp1` and label it "TP1 (4H-edge)". Traders now exit at TP1 as primary target, not at the 48h low.

## Verification

```
python3 -c "import ast; ast.parse(open('screener.py').read())"   → syntax OK
python3 -c "import ast; ast.parse(open('calibration/train_model.py').read())"  → syntax OK
python3 calibration/train_model.py  → CV AUC 0.5853 ≥ 0.55, per-setup files written
```

## Expected Impact

| Task | Expected Effect |
|------|----------------|
| A: Weekly context | Prevents counter-trend entries when weekly + daily both opposing; adds 15m precision for entry timing |
| B: calc_mtf_grade | Grade X blocks ~5-10% of signals that appear high-score but are counter-trend on both senior TFs |
| C: Per-setup CHoCH | Squeeze signals with CHoCH drop by ~21 pts (filter false positives); bos_fvg/breakout CHoCH signals boosted (reward real breakouts) |
| D: short_dist TP | Captures the 44.6% 4H WR edge; avoids the −6.1pp WR degradation from holding to 24H |

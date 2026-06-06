# Phase 2C Complete — Golden Setup Score Flag

**Commit scope:** AVEVA-43  
**Date:** 2026-04-25  
**Status:** Done

---

## What was built

A read-only GOLDEN overlay flag added to the screener. After all scoring is finalized for a signal, `score_symbol` evaluates 9 optimal conditions and sets:

```python
golden = (golden_count >= 7)
```

The score is **not changed** — this is a pure annotation.

---

## Condition mapping

| # | Condition | Variable | Threshold |
|---|-----------|----------|-----------|
| 1 | Setup type | `best` | `in {"bos_fvg", "squeeze"}` |
| 2 | UTC hour | `datetime.utcnow().hour` | `in {9, 10, 21, 22}` |
| 3 | Day of week | `datetime.utcnow().weekday()` | `in {0,2,3,6}` (Mon/Wed/Thu/Sun) |
| 4 | MTF tier | `bull_mtf` | `>= 3` (top tier from Phase 2A) |
| 5 | VWAP deviation | `vwap_dev` | `-5.0% to -1.0%` (good zone, not freefall) |
| 6 | Funding | `funding` | `-0.03% to 0.0%` (optimal, per Finding 2) |
| 7 | RS vs BTC | `rs_btc` | `0 < rs_btc < 2.0x` (moderate outperformance) |
| 8 | OI 24h | `oi_change` | `< +5%` (calm accumulation) |
| 9 | CHoCH 1H | `choch_1h` | `== "bull_choch"` AND `setup != "squeeze"` |

**Note on condition 9:** squeeze penalizes CHoCH (Phase 2B), so CHoCH is excluded from GOLDEN scoring for squeeze setups. A perfect squeeze can still achieve 8/9.

---

## Implementation location

- **`screener.py`** — `score_symbol()`, inserted after SHORT signal calibration block (~line 2984):
  - Computes `golden_count` and `golden` 
  - Adds both to the return dict: `"golden"` and `"golden_count"`

- **`telegram_alerts.py`** — two output locations:
  - `format_watchlist()`: appends `<b>[GOLDEN]</b>` to signal header line
  - `format_top_setups()`: appends `<b>[GOLDEN]</b>` to signal header line

---

## Verification

**Unit tests (all pass):**
1. Perfect bos_fvg (9/9) → GOLDEN ✓  
2. Perfect squeeze (8/9, cond9 always False for squeeze) → GOLDEN ✓  
3. 7/9 conditions → GOLDEN boundary ✓  
4. Breakout fails cond1 but 8/9 → GOLDEN (correct: cond1 is one of 9) ✓  
5. VWAP freefall (-7%) → cond5 fails ✓  
6. Positive funding → cond6 fails ✓  
7. Hot OI (8%) → cond8 fails ✓  

**Live scan (2026-04-25 UTC 17:xx, Saturday):**
- 14 signals processed, 0 GOLDEN — correct
- Saturday (weekday=5) fails cond3; UTC 17 fails cond2
- Max golden_count observed: 3/9
- Scores confirmed unchanged (read-only verified)

**Syntax:** `import screener` and `import telegram_alerts` both clean. All 24 pytest unit tests pass.

---

## Phase 3 deferred items

### Feature A — WAIT watchlist with trigger

**Design intent:**  
When a signal is scored but does not yet meet the entry conditions (e.g., bos_fvg setup but CHoCH not yet confirmed, or VWAP not yet retraced to the good zone), add it to a "WAIT" watchlist. When the missing condition triggers within the next N candles, fire an upgrade alert.

**Key variables to watch:** `choch_1h`, `vwap_dev`, `oi_change` momentum  
**Suggested implementation:** persistent dict in `cooldown_cache.json` structure, polled each screener run  
**Trigger condition:** any 1 of the unmet golden conditions resolves to True

---

### Feature B — Funding recovering detector

**Design intent:**  
Detect when funding has been deeply negative (< -0.03%) and is now recovering toward 0 (rising/normalizing trend). This is the "short squeeze fuel is loading" pattern — the setup quality improves as funding normalizes.

**Key variable:** `fund_trend` (`"normalizing"` or `"rising"`) combined with `funding < -0.01`  
**Suggested implementation:** add a `fund_recovering` boolean to the result dict, score bonus of +5–8 pts, and surface as a separate flag in Telegram  
**Historical evidence:** normalizing funding with bos_fvg = WR +8pp vs stable funding baseline

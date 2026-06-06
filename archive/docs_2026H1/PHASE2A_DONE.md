# Phase 2A Done — Low-Risk Scoring Fixes (AVEVA-41)

Date: 2026-04-25

## Changes Applied

### Fix 4 — RS_BTC (removed pair_chg signal)

**Removed** from Setups 1, 2, 4 (3 locations):
```python
# REMOVED:
if pair_chg > 10:
    score += 10; notes.append(f"chg24h+...")
```

**Added** after existing RS_BTC blocks in Setups 1, 2, 4:
```python
if rs_btc is not None and 0 < rs_btc < 2:
    score += 8; notes.append(f"RS{rs_btc:.2f}(mod)")
if rs_btc is not None and rs_btc > 5:
    score -= 8; notes.append(f"RS{rs_btc:.1f}x(ext)")
```

Lines changed: 2133→removed, 2140-2143 added; 2246→removed, 2262-2265 added; 2424→removed, 2445-2448 added.

---

### Fix 1 — MTF Tiered Scoring

**Old** (flat 25 pts for bull_mtf >= 3 in Setups 1, 2, 5):
```python
if bull_mtf >= 3: score += 25
```

**New** (tiered across all 4 setups):

Setups 1, 2, 5 (was 25 flat):
- bull_mtf == 3 → +30 pts
- bull_mtf == 4 → +20 pts
- bull_mtf >= 5 → +10 pts

Setup 4 (was 16 flat, proportionally scaled):
- bull_mtf == 3 → +20 pts
- bull_mtf == 4 → +13 pts
- bull_mtf >= 5 → +6 pts

Lines changed: ~2071-2076 (Setup 1), ~2198-2204 (Setup 2), ~2458-2463 (Setup 4), ~2668-2673 (Setup 5).

---

### Fix 5 — VWAP Extreme Penalty

**Added** after existing VWAP check in Setup 4 (~line 2515):
```python
if vwap_dev < -5.0:
    s4 -= 20; n4.append("VWAP<-5%!")  # net -6: freefall not just oversold
```

Net effect:
- vwap_dev in (-5%, -3%): +14 pts (unchanged)
- vwap_dev < -5%: +14 - 20 = **-6 net** (freefall penalty)

---

### Fix 6 — OI Extreme Penalty

**Added** to Setups 1, 2, 4 as additive penalty:
```python
if oi_change > 15:
    score -= 10; notes.append(f"OI_ext+{oi_change:.0f}%!")
```

- Setup 1 (~line 2043): extreme OI at lows = speculative longs, not true bottom
- Setup 2 (~line 2195): net effect for breakout: +25 - 10 = **+15 pts**
- Setup 4 (~line 2431): overextended setup penalty

Setup 5 (SHORT) left unchanged — rising OI there confirms short thesis.

---

## Verification

- `python3 -m py_compile screener.py` → **Syntax OK**
- `pair_chg > 10` occurrences: 3 → **0** (all removed)
- MTF tiering: 4 setups now have `>= 5 / >= 4 / >= 3` branches
- VWAP extreme: 1 new guard at line 2515
- OI extreme: 3 new guards in Setups 1, 2, 4

## NOT Changed (Phase 2B scope)

- Findings 2, 3, 7 (logistic regression weights, Breakout/Range Sweep thresholds, WebSocket sweep detection)

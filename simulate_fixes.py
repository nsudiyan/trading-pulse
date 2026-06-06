#!/usr/bin/env python3
"""
Simulation of scoring fixes against pump_resolved.csv.
Applies CVD fix (with sweep exception), L/S fix (with sweep exception),
and estimates impact on WINs vs LOSSes.

Run: python3 simulate_fixes.py
"""
import csv, re

CSV_PATH  = "outcomes/pump_resolved.csv"

OI_ACCUM_4H       = 5.0
CVD_BULL_DIV      = 30.0
CONVICTION_NEW    = 95
CONVICTION_OLD    = 85


def _cvd_pts(pct: float) -> int:
    """Mirrors _cvd_pts() from pump_detector.py."""
    pct = abs(pct)
    if pct >= 80: return 55
    if pct >= 50: return 45
    if pct >= 30: return 35
    return 0


def parse_row(row: list[str]) -> dict:
    n = len(row)
    # Old format: 17 cols.  New format: 27 cols (added mfe/mae/tp_hit/sl_hit).
    base = {
        "id":          row[0],
        "symbol":      row[2],
        "signal_type": row[3],
        "stage":       row[4],
        "score":       int(row[5]),
        "oi_chg_4h":  float(row[8]),
        "cvd_pct":    float(row[9]),
    }
    if n <= 17:
        base["outcome_1h"] = row[13]
        base["outcome_4h"] = row[16]
    else:
        # Extended format: outcome_1h at index 17, outcome_4h at last col
        base["outcome_1h"] = row[17]
        base["outcome_4h"] = row[-1]
    return base


def simulate(row: dict) -> dict:
    score       = row["score"]
    sig_type    = row["signal_type"]
    stage       = row["stage"]
    oi          = row["oi_chg_4h"]
    cvd         = row["cvd_pct"]
    o1h         = row["outcome_1h"]
    o4h         = row["outcome_4h"]

    is_sweep    = "ПРУЖИНА" in stage
    absorb_fire = oi >= OI_ACCUM_4H   # heuristic (exact needs price_chg_4h <= 1.5%)
    cvd_high    = cvd >= CVD_BULL_DIV

    delta       = 0
    notes       = []

    # ── Fix 2: CVD double-count (only pump, only ABSORPTION fired, only non-sweep) ──
    if sig_type == "pump" and absorb_fire and cvd_high and not is_sweep:
        old_cvd = _cvd_pts(cvd)       # what old code gave (full scale)
        new_cvd = 20                   # new capped bonus
        d = new_cvd - old_cvd          # always negative
        delta += d
        notes.append(f"CVD fix: {old_cvd}→{new_cvd} ({d:+d})")

    new_score = score + delta

    # Gate comparison
    passed_old = score    >= CONVICTION_OLD    # (pre-DEX; DEX adj may have added +5..+15)
    passes_new = new_score >= CONVICTION_NEW

    # Simple outcome: is it a WIN at any timeframe?
    is_win  = "WIN"  in (o1h, o4h)
    is_loss = "LOSS" in (o1h, o4h)
    is_flat = not is_win and not is_loss

    # Classify change
    if passed_old and not passes_new:
        status = "🚫 BLOCKED"
    elif not passed_old and not passes_new:
        status = "⬇ already_low"
    else:
        status = "✅ PASS"

    return {
        "sym":      row["symbol"],
        "stage":    stage,
        "sig":      sig_type,
        "score":    score,
        "delta":    delta,
        "new":      new_score,
        "outcome":  f"1h={o1h} 4h={o4h}",
        "win":      is_win,
        "loss":     is_loss,
        "flat":     is_flat,
        "status":   status,
        "notes":    "; ".join(notes),
    }


rows = []
with open(CSV_PATH, newline="") as f:
    reader = csv.reader(f)
    header = next(reader)
    for r in reader:
        if len(r) >= 17:
            rows.append(parse_row(r))

results = [simulate(r) for r in rows]

# ── Summary ────────────────────────────────────────────────────────────────────
blocked   = [r for r in results if r["status"] == "🚫 BLOCKED"]
passing   = [r for r in results if r["status"] == "✅ PASS"]
low       = [r for r in results if r["status"] == "⬇ already_low"]

blk_win   = [r for r in blocked if r["win"]]
blk_loss  = [r for r in blocked if r["loss"]]
blk_flat  = [r for r in blocked if r["flat"]]
pass_win  = [r for r in passing if r["win"]]
pass_loss = [r for r in passing if r["loss"]]
pass_flat = [r for r in passing if r["flat"]]

print("=" * 72)
print("SIMULATION — SCORE FIX IMPACT ON pump_resolved.csv")
print("=" * 72)
print(f"Total signals analysed : {len(results)}")
print(f"Passing new gate (≥95) : {len(passing)}  "
      f"(WIN={len(pass_win)} LOSS={len(pass_loss)} FLAT={len(pass_flat)})")
print(f"Newly BLOCKED          : {len(blocked)}  "
      f"(WIN={len(blk_win)} LOSS={len(blk_loss)} FLAT={len(blk_flat)})")
print(f"Already below 85       : {len(low)}")
print()

print("─── BLOCKED signals (old score ≥85, new score <95) ───────────────────")
for r in sorted(blocked, key=lambda x: x["score"], reverse=True):
    tag = "❌ FALSE-NEG" if r["win"] else ("✓ GOOD BLOCK" if r["loss"] else "  FLAT block")
    print(f"  {tag}  {r['sym']:18s} {r['score']:3d}→{r['new']:3d}  "
          f"{r['stage']:22s} {r['outcome']}  [{r['notes']}]")

print()
print("─── PASSING signals breakdown ─────────────────────────────────────────")
print("  WINs preserved:")
for r in sorted(pass_win, key=lambda x: x["score"], reverse=True):
    print(f"    ✅ {r['sym']:18s} score={r['new']:3d}  {r['stage']:22s} {r['outcome']}")
print("  LOSSes still passing:")
for r in sorted(pass_loss, key=lambda x: x["score"], reverse=True):
    print(f"    ⚠  {r['sym']:18s} score={r['new']:3d}  {r['stage']:22s} {r['outcome']}")

print()
# Win-rate estimate
all_sent_old = [r for r in results if r["score"] >= CONVICTION_OLD]
all_sent_new = [r for r in results if r["new"]   >= CONVICTION_NEW]
wr_old = sum(1 for r in all_sent_old if r["win"]) / max(len(all_sent_old), 1) * 100
wr_new = sum(1 for r in all_sent_new if r["win"]) / max(len(all_sent_new), 1) * 100
print(f"Win-rate estimate (old gate ≥{CONVICTION_OLD}): "
      f"{sum(1 for r in all_sent_old if r['win'])}/{len(all_sent_old)} = {wr_old:.1f}%")
print(f"Win-rate estimate (new gate ≥{CONVICTION_NEW}): "
      f"{sum(1 for r in all_sent_new if r['win'])}/{len(all_sent_new)} = {wr_new:.1f}%")

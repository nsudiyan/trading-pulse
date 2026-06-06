"""
migrate_pump_resolved_win_fix.py — Пересчёт outcomes в pump_resolved.csv по новой логике.

Новые правила:
  PUMP 4h: WIN +12%, LOSS -3.0% (R:R ~ 1:4)
  RUG  4h: WIN -10%, LOSS +3.0%
  PUMP 1h: tp +3%, sl -2% (без изменений)
  RUG  1h: tp -3%, sl +2% (без изменений)
  Order-aware: if tp_hit AND sl_hit → WIN if t_mfe < t_mae else LOSS

Backup создаётся как pump_resolved.csv.backup_YYYYMMDD_HHMMSS_pre_win_fix.
"""

from __future__ import annotations

import csv
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
CSV_PATH = BASE_DIR / "outcomes" / "pump_resolved.csv"

PUMP_WIN_4H  = 12.0
PUMP_LOSS_4H = 3.0
RUG_WIN_4H   = 10.0
RUG_LOSS_4H  = 3.0

PUMP_TP_1H = 3.0
PUMP_SL_1H = 2.0
RUG_TP_1H  = 3.0
RUG_SL_1H  = 2.0


def _outcome(tp_hit: bool, sl_hit: bool, t_mfe, t_mae) -> str:
    if tp_hit and sl_hit:
        try:
            tm, ta = float(t_mfe or 0), float(t_mae or 0)
            if tm and ta and tm < ta:
                return "WIN"
            return "LOSS"
        except (TypeError, ValueError):
            return "LOSS"
    if tp_hit:
        return "WIN"
    if sl_hit:
        return "LOSS"
    return "FLAT"


def _to_float(s: str, default: float = 0.0) -> float:
    try:
        return float(s) if s not in ("", None) else default
    except (TypeError, ValueError):
        return default


def migrate():
    if not CSV_PATH.exists():
        print(f"[ERROR] нет {CSV_PATH}")
        return

    # Backup
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup = CSV_PATH.with_suffix(f".csv.backup_{ts}_pre_win_fix")
    shutil.copy2(CSV_PATH, backup)
    print(f"[Backup] {backup.name}")

    # Read all
    with open(CSV_PATH, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = reader.fieldnames or []

    print(f"[Loaded] {len(rows)} строк")

    old_1h = Counter(r.get("outcome_1h", "") for r in rows)
    old_4h = Counter(r.get("outcome_4h", "") for r in rows)

    changed_1h, changed_4h = 0, 0

    for r in rows:
        stype  = (r.get("signal_type") or "").lower()
        is_rug = stype in ("rug", "rug_prep")

        # 1h
        mfe_1h = _to_float(r.get("mfe_1h_pct", ""))
        mae_1h = _to_float(r.get("mae_1h_pct", ""))
        t_mfe_1h = _to_float(r.get("time_to_mfe_1h", ""))
        t_mae_1h = _to_float(r.get("time_to_mae_1h", ""))

        if r.get("outcome_1h"):
            if is_rug:
                tp_hit_1h = mfe_1h >= RUG_TP_1H
                sl_hit_1h = mae_1h >= RUG_SL_1H
            else:
                tp_hit_1h = mfe_1h >= PUMP_TP_1H
                sl_hit_1h = mae_1h >= PUMP_SL_1H
            new_1h = _outcome(tp_hit_1h, sl_hit_1h, t_mfe_1h, t_mae_1h)
            if new_1h != r.get("outcome_1h"):
                changed_1h += 1
            r["outcome_1h"] = new_1h

        # 4h
        mfe_4h = _to_float(r.get("mfe_4h_pct", ""))
        mae_4h = _to_float(r.get("mae_4h_pct", ""))
        t_mfe_4h = _to_float(r.get("time_to_mfe_4h", ""))
        t_mae_4h = _to_float(r.get("time_to_mae_4h", ""))

        if r.get("outcome_4h"):
            if is_rug:
                tp_hit_4h = mfe_4h >= RUG_WIN_4H
                sl_hit_4h = mae_4h >= RUG_LOSS_4H
            else:
                tp_hit_4h = mfe_4h >= PUMP_WIN_4H
                sl_hit_4h = mae_4h >= PUMP_LOSS_4H
            r["tp_hit"] = str(bool(tp_hit_4h))
            r["sl_hit"] = str(bool(sl_hit_4h))
            new_4h = _outcome(tp_hit_4h, sl_hit_4h, t_mfe_4h, t_mae_4h)
            if new_4h != r.get("outcome_4h"):
                changed_4h += 1
            r["outcome_4h"] = new_4h

    new_1h = Counter(r.get("outcome_1h", "") for r in rows)
    new_4h = Counter(r.get("outcome_4h", "") for r in rows)

    print()
    print("=== 1h outcomes ===")
    print(f"  OLD: WIN={old_1h.get('WIN', 0):3d}  LOSS={old_1h.get('LOSS', 0):3d}  FLAT={old_1h.get('FLAT', 0):3d}")
    print(f"  NEW: WIN={new_1h.get('WIN', 0):3d}  LOSS={new_1h.get('LOSS', 0):3d}  FLAT={new_1h.get('FLAT', 0):3d}")
    print(f"  Изменено: {changed_1h} строк")
    print()
    print("=== 4h outcomes ===")
    print(f"  OLD: WIN={old_4h.get('WIN', 0):3d}  LOSS={old_4h.get('LOSS', 0):3d}  FLAT={old_4h.get('FLAT', 0):3d}")
    print(f"  NEW: WIN={new_4h.get('WIN', 0):3d}  LOSS={new_4h.get('LOSS', 0):3d}  FLAT={new_4h.get('FLAT', 0):3d}")
    print(f"  Изменено: {changed_4h} строк")
    print()

    # Write back atomically
    tmp = CSV_PATH.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    tmp.replace(CSV_PATH)
    print(f"[Written] {CSV_PATH.name} (rows={len(rows)})")


if __name__ == "__main__":
    migrate()

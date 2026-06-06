#!/usr/bin/env python3
"""
migrate_resolved_utcfix.py — пересчёт outcomes/resolved.csv после фикса BUG1 (UTC-окно).

Старый резолвер (naive .timestamp() на MSK-хосте) считал исход по окну [run−3h, run+1h]
вместо [run, run+H] → все строки размечены по ДО-сигнальным свечам. Здесь пересчитываем
КАЖДУЮ строку с КОРРЕКТНЫМ окном, переиспользуя ИСПРАВЛЕННЫЕ функции outcome_tracker
(ноль дрейфа логики). Заодно унифицируем схему (header → полный CSV_FIELDS).

Только пересчёт окно-зависимых полей; entry-time поля (score/фичи/entry/stop/tp) сохраняются.
Атомарная запись (temp+replace). Бэкап делается вызывающим. Идемпотентно.
"""
from __future__ import annotations
import csv, os, sys
from datetime import timedelta
import outcome_tracker as ot

BASE = os.path.dirname(os.path.abspath(__file__))
RESOLVED = os.path.join(BASE, "outcomes", "resolved.csv")
CF = ot.CSV_FIELDS

def _f(x):
    try: return float(x)
    except (TypeError, ValueError): return None

def recompute(e: dict):
    """Пересчёт 4h+24h для одной записи. Возвращает (changed_4h_bool, ok_bool)."""
    run_dt = ot._parse_ts(e["run_ts"])
    entry_px = _f(e.get("price_entry"))
    if not entry_px:
        return False, False
    stop = _f(e.get("stop")) or 0
    tp1  = _f(e.get("tp1")) or 0
    direction = e.get("direction", "ЛОНГ")
    old_oc4 = e.get("outcome_4h")
    ok = False
    for hours, suf in [(4, "4h"), (24, "24h")]:
        end = run_dt + timedelta(hours=hours)
        mh, ml, ce, tmax, tmin = ot._fetch_klines_extremes(e["symbol"], run_dt, end)
        if ce is None:
            continue
        ok = True
        price_now = ce
        pct = (price_now - entry_px) / entry_px * 100 if entry_px else 0
        if direction in ("ЛОНГ", "ЖДАТЬ"):
            hit_tp1  = bool(mh and tp1 and mh >= tp1)
            hit_stop = bool(ml and stop and ml <= stop)
            outcome  = "TP1" if hit_tp1 else ("STOP" if hit_stop else
                       ("WIN" if pct > 0.5 else ("LOSS" if pct < -0.5 else "FLAT")))
            mfe = ((mh - entry_px) / entry_px * 100) if (mh and entry_px) else 0.0
            mae = ((ml - entry_px) / entry_px * 100) if (ml and entry_px) else 0.0
        else:
            hit_tp1  = bool(ml and tp1 and ml <= tp1)
            hit_stop = bool(mh and stop and mh >= stop)
            outcome  = "TP1" if hit_tp1 else ("STOP" if hit_stop else
                       ("WIN" if pct < -0.5 else ("LOSS" if pct > 0.5 else "FLAT")))
            mfe = ((entry_px - ml) / entry_px * 100) if (ml and entry_px) else 0.0
            mae = ((entry_px - mh) / entry_px * 100) if (mh and entry_px) else 0.0
        if direction == "ЖДАТЬ":
            outcome = "FLAT"; hit_tp1 = False; hit_stop = False
        exit_px, exit_rsn = ot._resolve_exit(hit_tp1, hit_stop, tp1, stop, price_now, suf)
        r_mult = ot._compute_r_multiple(exit_px, entry_px, stop)
        e[f"price_{suf}"]       = price_now
        e[f"change_{suf}_pct"]  = round(pct, 2)
        e[f"hit_tp1_{suf}"]     = int(hit_tp1)
        e[f"hit_stop_{suf}"]    = int(hit_stop)
        e[f"outcome_{suf}"]     = outcome
        e[f"mfe_{suf}_pct"]     = round(mfe, 2)
        e[f"mae_{suf}_pct"]     = round(mae, 2)
        e[f"r_multiple_{suf}"]  = r_mult
        e[f"exit_reason_{suf}"] = exit_rsn
        e[f"exit_price_{suf}"]  = round(exit_px, 8) if exit_px else ""
        e[f"outcome_label_{suf}"] = ot._outcome_label(outcome)
        if tmax is not None and tmin is not None:
            if direction in ("ЛОНГ", "ЖДАТЬ"):
                e[f"time_to_mfe_{suf}_h"] = tmax; e[f"time_to_mae_{suf}_h"] = tmin
            else:
                e[f"time_to_mfe_{suf}_h"] = tmin; e[f"time_to_mae_{suf}_h"] = tmax
    return (ok and e.get("outcome_4h") != old_oc4), ok

def main():
    rows = []
    with open(RESOLVED) as fh:
        r = csv.reader(fh); next(r)
        for line in r:
            if not line:
                continue
            rows.append({CF[i]: (line[i] if i < len(line) else "") for i in range(len(CF))})
    print(f"Загружено {len(rows)} строк; пересчитываю с КОРРЕКТНЫМ UTC-окном...", flush=True)
    changed = ok = fail = 0
    for i, e in enumerate(rows):
        ch, good = recompute(e)
        if good: ok += 1
        else: fail += 1
        if ch: changed += 1
        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(rows)}  ok={ok} fail={fail} changed_4h={changed}", flush=True)
    # атомарная запись с полным унифицированным header
    tmp = RESOLVED + ".tmp"
    with open(tmp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CF, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    os.replace(tmp, RESOLVED)
    # сводка WR (4h decisive: TP1/WIN vs STOP/LOSS)
    dec_w = sum(1 for e in rows if e.get("outcome_4h") in ("WIN", "TP1"))
    dec_l = sum(1 for e in rows if e.get("outcome_4h") in ("STOP", "LOSS"))
    print(f"\nГОТОВО. ok={ok} fail(нет klines)={fail} changed_4h_outcomes={changed}", flush=True)
    print(f"Новый 4h WR (decisive, TP1/WIN vs STOP/LOSS): {dec_w}/{dec_w+dec_l} = "
          f"{dec_w/(dec_w+dec_l)*100:.1f}%" if (dec_w+dec_l) else "n/a", flush=True)
    print(f"resolved.csv переписан с унифицированным header ({len(CF)} колонок).", flush=True)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""SMOKE 2023-H2 (PREREG §6): прогон ядра на 2023-07-01…2023-12-31 (внутри train).
Печатает ТОЛЬКО состав и счётчики — БЕЗ агрегатов PnL (дисциплина: интерпретаций нет).
Сохраняет smoke_trades.json для ручной сверки (manual_check.py)."""
import json, gzip, os, random
from datetime import datetime, timezone
from core_replay import run_engine, BAR

ROOT = os.path.expanduser("~/trading/backtest_6y")
def ms(s): return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()*1000)
T0, T1 = ms("2023-07-01"), ms("2024-01-01")
LO, HI = ms("2023-06-20"), ms("2024-01-05")

uni_all = json.load(gzip.open(f"{ROOT}/data/universe.json.gz", "rt"))
uni = {d: v for d, v in uni_all.items() if "2023-06-25" <= d <= "2024-01-02"}
need = sorted({s for lst in uni.values() for s in lst})
print(f"smoke-окно: символов в объединении топ-150 = {len(need)}")
bars = {}
for s in need:
    p = f"{ROOT}/data/klines/{s}.json.gz"
    if not os.path.exists(p): continue
    b = {int(k): v for k, v in json.load(gzip.open(p, "rt")).items() if LO <= int(k) <= HI}
    if b: bars[s] = b
print(f"загружено с данными в окне: {len(bars)}")

hit_log = []
trades, counters = run_engine(bars, {}, uni, T0, T1, log=print, hit_log=hit_log)
for t in trades:                       # дисциплина: доходности в лог НЕ выводим
    for k in ("gross_mkt_pct", "gross_opt_pct", "gross_cons_pct"): t.pop(k, None)
json.dump(trades, open(f"{ROOT}/smoke_trades.json", "w"))
json.dump([{**r, "hits": r["hits"]} for r in hit_log], open(f"{ROOT}/smoke_hitlog.json", "w"))

by_kind = {}
for t in trades: by_kind[t["kind"]] = by_kind.get(t["kind"], 0) + 1
by_month = {}
for t in trades: by_month[t["ts_utc"][:7]] = by_month.get(t["ts_utc"][:7], 0) + 1
print(f"\n═══ SMOKE 2023-H2: СОСТАВ (без PnL) ═══")
print(f"счётчики: { {k: v for k, v in counters.items() if v} }")
print(f"позиций по типам: {by_kind}")
print(f"по месяцам: {dict(sorted(by_month.items()))}")
opt = sum(t["opt_fill"] for t in trades); cons = sum(t["cons_fill"] for t in trades)
print(f"fill-rate лимиток: optimistic {opt}/{len(trades)}, conservative {cons}/{len(trades)}")
random.seed(20260710)
sample = random.sample(trades, min(12, len(trades)))
print(f"\n12 случайных сделок для ручной сверки (seed 20260710):")
for t in sample:
    print(f"  {t['ts_utc']} {t['symbol']:>14} {t['kind']:>9} vr={t['vol_ratio']:>5.1f} "
          f"entry_ts={t['entry_ts']} exit_ts={t['exit_ts']} cens={t['censored']}")
json.dump(sample, open(f"{ROOT}/smoke_sample.json", "w"))
print("сохранено: smoke_trades.json, smoke_hitlog.json, smoke_sample.json")

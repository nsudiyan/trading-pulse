#!/usr/bin/env python3
"""
backtests/shadow_conviction_retro.py — Shadow-A/B объёмного edge на ИСПРАВЛЕННОЙ линейке.

Вопрос: предсказывает ли валидированный объёмный conviction-тир (vol_core.conviction,
ровно live-логика) исход реальных сигналов бота — теперь, когда resolved.csv пересчитан
с КОРРЕКТНЫМ окном (BUG1 fix)? Ретроспективно = ответ сразу, без ожидания дней.

Для каждого ЛОНГ-сигнала: режу klines_1h до момента входа (как видел бы детектор —
текущая свеча forming), считаю vol_core.vol_factors+conviction → тир; джойню с
КОРРЕКТНЫМ outcome_4h/24h (WIN/TP1 vs LOSS/STOP). Группирую WR по тиру.

Офлайн (klines_1h на диске), без сети/API/правок live. Воспроизводимо.
"""
from __future__ import annotations
import csv, gzip, glob, os, sys
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
import numpy as np

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))
import vol_core as vc
KL1H = BASE / "pump_analysis" / "klines_1h"
RESOLVED = BASE / "outcomes" / "resolved.csv"

def to_ms(ts):
    for f in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try: return int(datetime.strptime(ts.strip()[:19], f).replace(tzinfo=timezone.utc).timestamp()*1000)
        except ValueError: continue
    return None

_cache = {}
def load_vols(sym):
    if sym in _cache: return _cache[sym]
    cands = [p for p in glob.glob(str(KL1H/f"{sym}*")) if os.path.basename(p).split('.')[0].upper()==sym.upper()]
    res = None
    if cands:
        try:
            import pandas as pd
            df = pd.read_csv(gzip.open(cands[0],'rt') if cands[0].endswith('.gz') else open(cands[0]))
            tcol = "open_time" if "open_time" in df.columns else df.columns[0]
            res = (df[tcol].astype("int64").values, pd.to_numeric(df["volume"],errors="coerce").values)
        except Exception: res = None
    _cache[sym] = res
    return res

def tier_at(sym, run_ms):
    v = load_vols(sym)
    if v is None: return None
    ts, vols = v
    idx = int(np.searchsorted(ts, run_ms, side="right") - 1)  # forming-свеча на момент входа
    if idx < 200: return None
    series = [float(x) for x in vols[:idx+1]]   # история до entry включительно
    vf = vc.vol_factors(series)
    return vc.conviction(vf)["tier"] if vf else None

def main():
    rows = [r for r in csv.DictReader(open(RESOLVED)) if "ЛОН" in (r.get("direction") or "")]
    print(f"ЛОНГ-сигналов в resolved.csv: {len(rows)}")
    by_tier_4h = defaultdict(lambda:[0,0])   # [win, loss]
    by_tier_24h = defaultdict(lambda:[0,0])
    by_tier_mfe = defaultdict(list)
    n_cov = 0
    for r in rows:
        ms = to_ms(r["run_ts"])
        if ms is None: continue
        t = tier_at(r["symbol"], ms)
        if t is None: continue
        n_cov += 1
        for col, acc in (("outcome_4h",by_tier_4h),("outcome_24h",by_tier_24h)):
            oc=(r.get(col) or "").upper()
            if oc in ("WIN","TP1"): acc[t][0]+=1
            elif oc in ("LOSS","STOP"): acc[t][1]+=1
        try:
            mfe=float(r.get("mfe_24h_pct") or "nan")
            if mfe==mfe: by_tier_mfe[t].append(mfe)
        except: pass
    print(f"покрыто klines (тир посчитан): {n_cov}\n")
    order=["high","standard","weak","below_gate","none"]
    def show(acc, label):
        print(f"=== {label} (WR = TP1/WIN vs STOP/LOSS, на ИСПРАВЛЕННОЙ линейке) ===")
        tot_w=tot_n=0
        for t in order:
            w,l=acc[t]; n=w+l
            if n>0:
                print(f"  {t:11s} n={n:4d}  WR={w/n*100:5.1f}%")
                tot_w+=w; tot_n+=n
        if tot_n: print(f"  ВСЕ        n={tot_n:4d}  WR={tot_w/tot_n*100:5.1f}%")
    show(by_tier_4h, "4h")
    print()
    show(by_tier_24h, "24h")
    print("\n=== средний MFE_24h по тиру (магнитуда) ===")
    for t in order:
        if by_tier_mfe[t]:
            import statistics as st
            print(f"  {t:11s} n={len(by_tier_mfe[t]):4d}  ср.MFE={st.mean(by_tier_mfe[t]):5.2f}%  медиана={st.median(by_tier_mfe[t]):.2f}%")
    print("\nВЕРДИКТ: если high-тир заметно > below_gate по WR/MFE СТАБИЛЬНО → объём улучшает сигналы бота → стоит активировать.")
    print("Если тиры неразличимы → объём поверх уже-отобранных сигналов бота не добавляет (на этой линейке).")

if __name__ == "__main__":
    main()

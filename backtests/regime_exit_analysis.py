#!/usr/bin/env python3
"""
backtests/regime_exit_analysis.py — мульти-режимная экзит-экономика conviction-тиров.

Закрывает последний caveat: экзиты мерили на resolved.csv (6 недель весны, ОДИН режим).
HIGH-тир валидирован на 7-мес OOS для ДЕТЕКЦИИ (precision 52%), но держится ли P&L по
режимам? Берём ВСЕ окна feature_matrix (25 мес), считаем conviction-тир, для high/standard
симулируем LONG-сделку на klines_1h, размечаем режим BTC (7д-тренд на входе), агрегируем.

Экзит: фикс SL 8% / TP 20%, 24ч, order-aware (intra-bar пессимистично: стоп раньше).
Соответствует выводу шага 6 (фикс ≈ лучшее, трейлинг не бьёт). Вход = close последнего
1h-бара до start_ms. Регим: BTC 7д-изменение (bull>+5%, bear<-5%, иначе sideways).

Только чтение локальных klines. Воспроизводимо.
"""
from __future__ import annotations
import gzip, glob, os, sys
from pathlib import Path
from collections import defaultdict
from statistics import mean
import numpy as np, pandas as pd

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))
import vol_core as vc
FM = BASE / "pump_analysis" / "feature_matrix.csv"
KL1H = BASE / "pump_analysis" / "klines_1h"
OUT = BASE / "backtests" / "REGIME_EXIT_REPORT.md"

SL_PCT, TP_PCT, HORIZON = 8.0, 20.0, 24  # 24×1h = 24ч

def load_1h(sym):
    cands = [p for p in glob.glob(str(KL1H / f"{sym}*")) if os.path.basename(p).split('.')[0].upper()==sym.upper()]
    if not cands: return None
    p = cands[0]
    try:
        df = pd.read_csv(gzip.open(p,'rt') if p.endswith('.gz') else open(p))
        df = df.rename(columns={"open_time":"ts"}); df["ts"]=df["ts"].astype("int64")
        for c in ["open","high","low","close"]: df[c]=pd.to_numeric(df[c],errors="coerce")
        return df.sort_values("ts").reset_index(drop=True)
    except Exception:
        return None

def sim_trade(hi, lo, cl, entry):
    sl = entry*(1-SL_PCT/100); tp = entry*(1+TP_PCT/100)
    for j in range(len(hi)):
        if lo[j] <= sl: return -SL_PCT          # пессимистично: стоп раньше
        if hi[j] >= tp: return TP_PCT
    return (cl[-1]/entry - 1)*100 if len(cl) else 0.0

def tier_of(r):
    return vc.conviction({"vol_ratio_1h":r.vol_ratio_1h,"vol_ratio_4h":r.vol_ratio_4h,
                          "vol_spike_count":r.vol_spike_count})["tier"]

def main():
    fm = pd.read_csv(FM)
    fm["tier"] = fm.apply(tier_of, axis=1)
    # BTC регим
    btc = load_1h("BTCUSDT")
    if btc is None:
        print("НЕТ BTCUSDT klines"); return
    bts, bclose = btc["ts"].values, btc["close"].values
    def regime(ms):
        pos = int(np.searchsorted(bts, ms-1, side="right")-1)
        if pos < 168: return "unknown"
        chg = (bclose[pos]/bclose[pos-168]-1)*100  # 7д
        return "bull" if chg>5 else ("bear" if chg<-5 else "sideways")

    # симулируем только high/standard (то, на чём бы торговали)
    targets = fm[fm.tier.isin(["high","standard"])].copy()
    print(f"окон всего {len(fm)}; high+standard для симуляции: {len(targets)} "
          f"(high={int((fm.tier=='high').sum())}, standard={int((fm.tier=='standard').sum())})")

    results = []  # (tier, regime, ret_pct)
    by_sym = defaultdict(list)
    for idx, r in targets.iterrows():
        by_sym[r.symbol].append(r)
    done = 0
    for sym, rows in by_sym.items():
        df = load_1h(sym)
        if df is None: continue
        ts = df["ts"].values; H=df["high"].values; L=df["low"].values; C=df["close"].values
        for r in rows:
            pos = int(np.searchsorted(ts, int(r.start_ms)-1, side="right")-1)
            if pos < 1 or pos+1 >= len(ts): continue
            entry = float(C[pos])
            if entry <= 0: continue
            end = min(pos+1+HORIZON, len(ts))
            if end-pos < 4: continue
            ret = sim_trade(H[pos+1:end], L[pos+1:end], C[pos+1:end], entry)
            results.append((r.tier, regime(int(r.start_ms)), ret))
            done += 1
    print(f"симулировано сделок: {done}\n")

    # агрегация tier × regime
    agg = defaultdict(list)
    for tier, reg, ret in results:
        agg[(tier,reg)].append(ret); agg[(tier,"ALL")].append(ret)
    def stat(lst):
        if not lst: return None
        return dict(n=len(lst), E_pct=round(mean(lst),3), E_R=round(mean(lst)/SL_PCT,3),
                    WR=round(sum(1 for x in lst if x>0)/len(lst),3))

    md = ["# Мульти-режимная экзит-экономика conviction-тиров (25 мес)\n",
          f"Экзит: фикс SL{SL_PCT:.0f}%/TP{TP_PCT:.0f}%, {HORIZON}ч, order-aware (gross). "
          f"Симулировано {done} сделок (high+standard).\n",
          "> Режим = BTC 7д-тренд на входе. P&L gross (без ~0.3% комиссий). 1h-бары (порядок внутри часа коарсе).\n",
          "| tier | режим | n | E %/сделка | E (R) | WR |","|---|---|---|---|---|---|"]
    for tier in ["high","standard"]:
        for reg in ["ALL","bull","sideways","bear","unknown"]:
            s = stat(agg.get((tier,reg)))
            if s:
                md.append(f"| {tier} | {reg} | {s['n']} | **{s['E_pct']:+.2f}%** | {s['E_R']:+.3f} | {s['WR']:.0%} |")
    OUT.write_text("\n".join(md))

    print(f"{'tier':9s} {'regime':9s} {'n':>5s} {'E%':>8s} {'E_R':>8s} {'WR':>5s}")
    for tier in ["high","standard"]:
        for reg in ["ALL","bull","sideways","bear","unknown"]:
            s = stat(agg.get((tier,reg)))
            if s:
                print(f"{tier:9s} {reg:9s} {s['n']:>5d} {s['E_pct']:>+7.2f}% {s['E_R']:>+7.3f} {s['WR']*100:>4.0f}%")
    print(f"\nSaved: {OUT.name}")

if __name__ == "__main__":
    main()

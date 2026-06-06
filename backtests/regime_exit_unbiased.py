#!/usr/bin/env python3
"""
backtests/regime_exit_unbiased.py — НЕСМЕЩЁННЫЙ мульти-режимный бэктест объёмного гейта.

regime_exit_analysis.py был СМЕЩЁН: окна feature_matrix размечены по forward-доходности
(памп = ≥15%/24ч), вход в начале окна → торговля на метке (+0.62R = артефакт).

Здесь БЕЗ метки: сканируем полную 1h-историю каждого символа, на КАЖДОМ баре считаем
conviction-тир (та же vol_core-логика), при tier=high стреляем LONG (с кулдауном 24ч),
и считаем forward-P&L из klines — что бы рынок ни дал, включая движения ВНИЗ. Режим =
BTC 7д-тренд на входе. Экзит: фикс SL8%/TP20%, 24ч, order-aware (бар low/high, пессимистично).

Это честная оценка «торгуй каждый сигнал гейта». Сравнить с resolved.csv (+0.12R). Воспроизводимо.
"""
from __future__ import annotations
import gzip, glob, os, sys
from pathlib import Path
from collections import defaultdict
from statistics import mean
import numpy as np, pandas as pd

BASE = Path(__file__).parent.parent
KL1H = BASE / "pump_analysis" / "klines_1h"
OUT = BASE / "backtests" / "REGIME_EXIT_UNBIASED_REPORT.md"
SL_PCT, TP_PCT, HORIZON, COOLDOWN_BARS = 8.0, 20.0, 24, 24

def load_1h(path):
    try:
        df = pd.read_csv(gzip.open(path,'rt') if path.endswith('.gz') else open(path))
        df = df.rename(columns={"open_time":"ts"}); df["ts"]=df["ts"].astype("int64")
        for c in ["open","high","low","close","volume"]: df[c]=pd.to_numeric(df[c],errors="coerce")
        return df.sort_values("ts").reset_index(drop=True)
    except Exception:
        return None

def main():
    # BTC регим
    btcp = [p for p in glob.glob(str(KL1H/"*")) if os.path.basename(p).split('.')[0].upper()=="BTCUSDT"]
    btc = load_1h(btcp[0]) if btcp else None
    if btc is None: print("НЕТ BTC"); return
    bts, bcl = btc["ts"].values, btc["close"].values
    def regime(ms):
        pos = int(np.searchsorted(bts, ms-1, side="right")-1)
        if pos < 168: return "unknown"
        chg = (bcl[pos]/bcl[pos-168]-1)*100
        return "bull" if chg>5 else ("bear" if chg<-5 else "sideways")

    files = [p for p in glob.glob(str(KL1H/"*")) if p.endswith(".gz") or p.endswith(".csv")]
    results = []   # (regime, ret_pct, pumped)
    n_sym = 0
    for path in files:
        sym = os.path.basename(path).split('.')[0].upper()
        if sym == "BTCUSDT": continue
        df = load_1h(path)
        if df is None or len(df) < 800: continue
        n_sym += 1
        vol = df["volume"]; med = vol.rolling(720, min_periods=168).median().shift(1)
        vr1 = vol/med
        vr4 = vol.rolling(4).mean()/med
        spike = (vol > 2*med).rolling(4).sum()
        accel = vr1/vr4
        vscore = 15*(spike>=1).astype(int) + 10*(vr1>1.0).astype(int) + 8*(vr4>1.0).astype(int)
        # HIGH tier: vscore>=23 AND accel>=0.8 AND (vr1>3 OR accel>1.2)
        is_high = (vscore>=23) & (accel>=0.8) & ((vr1>3.0) | (accel>1.2))
        idx = np.where(is_high.fillna(False).values)[0]
        ts=df["ts"].values; H=df["high"].values; L=df["low"].values; C=df["close"].values
        last_entry = -10**9
        for i in idx:
            if i - last_entry < COOLDOWN_BARS: continue      # кулдаун
            if i+1 >= len(df): continue
            last_entry = i
            entry = float(C[i])
            if entry<=0 or not np.isfinite(entry): continue
            end = min(i+1+HORIZON, len(df))
            if end-i < 4: continue
            sl, tp = entry*(1-SL_PCT/100), entry*(1+TP_PCT/100)
            ret = None; pumped = (H[i+1:end].max()/entry - 1) >= 0.15
            for j in range(i+1, end):
                if L[j] <= sl: ret=-SL_PCT; break          # пессимистично: стоп раньше
                if H[j] >= tp: ret=TP_PCT; break
            if ret is None: ret = (C[end-1]/entry-1)*100
            results.append((regime(int(ts[i])), ret, bool(pumped)))

    if not results: print("нет сделок"); return
    agg=defaultdict(list); pump_hits=defaultdict(int)
    for reg,ret,pumped in results:
        agg[reg].append(ret); agg["ALL"].append(ret)
        if pumped: pump_hits[reg]+=1; pump_hits["ALL"]+=1
    def stat(reg):
        l=agg[reg]
        if not l: return None
        return dict(n=len(l), E=round(mean(l),3), ER=round(mean(l)/SL_PCT,3),
                    WR=round(sum(1 for x in l if x>0)/len(l),3), pumprate=round(pump_hits[reg]/len(l),3))

    md=["# НЕсмещённый мульти-режимный бэктест объёмного гейта (HIGH-тир)\n",
        f"Символов: {n_sym}. Сделок (гейт-триггеры, кулдаун 24ч): {len(results)}. "
        f"Экзит фикс SL{SL_PCT:.0f}/TP{TP_PCT:.0f}, 24ч, gross. Без метки (P&L из klines как есть).\n",
        "| режим | n | E %/сделка | E (R) | WR | доля реальных пампов(≥15%) |","|---|---|---|---|---|---|"]
    print(f"символов {n_sym}, сделок {len(results)}\n")
    print(f"{'режим':9s} {'n':>5s} {'E%':>8s} {'E_R':>8s} {'WR':>5s} {'pump%':>6s}")
    for reg in ["ALL","bull","sideways","bear","unknown"]:
        s=stat(reg)
        if s:
            md.append(f"| {reg} | {s['n']} | **{s['E']:+.2f}%** | {s['ER']:+.3f} | {s['WR']:.0%} | {s['pumprate']:.0%} |")
            print(f"{reg:9s} {s['n']:>5d} {s['E']:>+7.2f}% {s['ER']:>+7.3f} {s['WR']*100:>4.0f}% {s['pumprate']*100:>5.0f}%")
    md.append(f"\n> Сравнение: resolved.csv (реальные сигналы бота, late-entry) дал ~+0.12R. "
              f"Здесь вход в момент срабатывания гейта (раньше). pump% = доля входов, где цена дошла +15% за 24ч.")
    OUT.write_text("\n".join(md))
    print(f"\nSaved: {OUT.name}")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
backtests/exit_sweep_highvol.py — тест гипотезы «широкий стоп + let-run на объёмном сабсете».

Читает backtests/joined_dataset.csv. На сабсете vol_score>=23 (высокий объёмный
конфлюенс, 24h) сравнивает связки SL×TP и let-run. Метрика: expectancy в % И в R
(нормируем на риск, т.к. широкий стоп = больше абсолютный лосс, но можно сайзить меньше).

order-aware через t_mfe/t_mae. Пиковая аппроксимация (трейлинг = потолок MFE сверху).
"""
from __future__ import annotations
import csv
from pathlib import Path
from statistics import mean

BASE = Path(__file__).parent.parent
JD = BASE / "backtests" / "joined_dataset.csv"


def f(v):
    try: return float(v)
    except (TypeError, ValueError): return None


def load(vol_min):
    rows = []
    with open(JD) as fh:
        for r in csv.DictReader(fh):
            if f(r["vol_score"]) is None or f(r["vol_score"]) < vol_min:
                continue
            rows.append(r)
    return rows


def sim(rows, H, sl_pct, tp_pct, let_run=False):
    """Возвращает (E_pct, E_R, WR, n). let_run=True: TP=∞, выход по пику MFE если SL не выбил."""
    pe, sl_R = [], []
    for r in rows:
        fav = f(r[f"mfe_{H}"]); adv = f(r[f"mae_{H}"]); chg = f(r[f"change_{H}"])
        tf = f(r[f"t_mfe_{H}"]); ta = f(r[f"t_mae_{H}"])
        if fav is None or adv is None: continue
        fav = max(fav, 0.0); adv = abs(adv)
        hit_sl = adv >= sl_pct
        if let_run:
            # выход по пику MFE, если стоп не выбил раньше
            if hit_sl and ta is not None and tf is not None and ta < tf:
                ret = -sl_pct
            elif hit_sl and (ta is None or tf is None):
                ret = -sl_pct  # порядок неизвестен → пессимизм
            else:
                ret = fav  # потолок (верхняя граница того, что взял бы идеальный трейлинг)
        else:
            hit_tp = fav >= tp_pct
            if hit_tp and hit_sl:
                ret = tp_pct if (tf is not None and ta is not None and tf <= ta) else -sl_pct
            elif hit_tp: ret = tp_pct
            elif hit_sl: ret = -sl_pct
            else: ret = chg if chg is not None else 0.0
        pe.append(ret); sl_R.append(ret / sl_pct)
    if not pe: return None
    return {"n": len(pe), "E_pct": round(mean(pe), 3), "E_R": round(mean(sl_R), 3),
            "WR": round(sum(1 for x in pe if x > 0) / len(pe), 4)}


def main():
    for vol_min, label in [(23, "vol_score>=23 (высокий объём)"), (0, "ВСЕ объёмы (контроль)")]:
        rows = load(vol_min)
        print(f"\n{'='*72}\n  {label}  ·  n={len(rows)}  ·  горизонт 24h\n{'='*72}")
        print(f"  {'стратегия':28s} {'n':>4s} {'E %/trade':>10s} {'E (в R)':>9s} {'WR':>7s}")
        configs = [
            ("SL 4% / TP 12% (текущее)", dict(sl_pct=4, tp_pct=12)),
            ("SL 6% / TP 15%",           dict(sl_pct=6, tp_pct=15)),
            ("SL 8% / TP 20%",           dict(sl_pct=8, tp_pct=20)),
            ("SL 8% / TP 30%",           dict(sl_pct=8, tp_pct=30)),
            ("SL 6% / let-run (потолок)", dict(sl_pct=6, tp_pct=0, let_run=True)),
            ("SL 8% / let-run (потолок)", dict(sl_pct=8, tp_pct=0, let_run=True)),
            ("SL 10% / let-run (потолок)",dict(sl_pct=10, tp_pct=0, let_run=True)),
        ]
        for name, kw in configs:
            s = sim(rows, "24h", **kw)
            if s:
                star = "  ←" if s["E_R"] > 0.1 else ""
                print(f"  {name:28s} {s['n']:>4d} {s['E_pct']:>+9.2f}% {s['E_R']:>+8.3f}R {s['WR']*100:>6.1f}%{star}")
    print("\nПримечание: let-run 'потолок' = ВЕРХНЯЯ граница (идеальный трейлинг ловит весь пик MFE).")
    print("Реальный трейлинг возьмёт меньше — нужен полный путь по klines. Фикс SL/TP — корректны.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
⚠️⚠️ УСТАРЕЛ — НЕ ИСПОЛЬЗОВАТЬ. Замена: shortdist_forward_pathresolved.py
Этот baseline ЗАВЫШЕН look-ahead bias (показывает мнимые +0.215R вместо честных −0.009).
Причина: берёт r_multiple_24h напрямую, а та при both-hit засчитывает TP (см. outcome_tracker._resolve_exit).
─────────────────────────────────────────────────────────────────────────────
shortdist_forward.py — ФОРВАРД (out-of-sample) трекер гипотезы:
  short_dist/SHORT на ликвидных парах (≥ VOL_MIN), вход MAKER → положительный net-R.

READ-ONLY по outcomes/resolved.csv. Накапливает сделки С ДАТЫ FORWARD_START (out-of-sample),
считает net-R после maker-костов + 95% CI, сравнивает с in-sample baseline и даёт go/no-go
по достижении TARGET_N. Это ПРОВЕРКА НА OVERFIT: держится ли in-sample эдж (+0.215R) вперёд.

Запуск:  python3 shortdist_forward.py
Это НЕ торговля и НЕ live-изменение — только чтение уже собранных исходов.
"""
from __future__ import annotations
import csv, math, os
from datetime import datetime

# ── КОНФИГ (правь при необходимости) ─────────────────────────────────────────
FORWARD_START = "2026-05-29"   # сделки с этой даты = out-of-sample. При рестарте теста — обнови.
VOL_MIN_USD   = 50e6           # порог ликвидности (там, где maker реален)
FEE_RT_PCT    = 0.04           # maker round-trip (оценка)
SLIP_RT_PCT   = 0.03           # слиппедж на ликвиде (оценка)
FUND_INTERVAL_H = 8.0
TARGET_N      = 35             # сколько сделок нужно для go/no-go
HORIZON       = "24h"

HERE     = os.path.dirname(os.path.abspath(__file__))
RESOLVED = os.path.join(HERE, "outcomes", "resolved.csv")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _is_target(r) -> bool:
    return (r.get("setup") or "").strip() == "short_dist" and \
           (r.get("direction") or "").strip().upper() in ("ШОРТ", "SHORT")


def _net_r(r):
    g    = _f(r.get(f"r_multiple_{HORIZON}"))
    e    = _f(r.get("price_entry"))
    s    = _f(r.get("stop"))
    hold = _f(r.get(f"hold_time_{HORIZON}_min"))
    fund = _f(r.get("funding"))
    if g is None or e is None or s is None or e <= 0:
        return None
    risk = abs(e - s) / e * 100.0
    if risk <= 0:
        return None
    n_fund = (hold / 60.0 / FUND_INTERVAL_H) if hold else 0.0
    funding_cost = (-1 * (fund or 0.0)) * n_fund      # SHORT платит при funding<0
    cost_r = (FEE_RT_PCT + SLIP_RT_PCT + funding_cost) / risk
    return g - cost_r


def _agg(xs):
    n = len(xs)
    if not n:
        return None
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n else 0.0
    lo, hi = m - 1.96 * se, m + 1.96 * se
    wr = 100.0 * sum(1 for x in xs if x > 0) / n
    return {"n": n, "mean": m, "total": sum(xs), "lo": lo, "hi": hi, "wr": wr,
            "sig": (lo > 0 or hi < 0)}


def _date(r):
    ts = (r.get("run_ts") or "")[:10]
    try:
        datetime.strptime(ts, "%Y-%m-%d")
        return ts
    except ValueError:
        return None


def main():
    if not os.path.exists(RESOLVED):
        print(f"нет файла: {RESOLVED}")
        return
    rows = list(csv.DictReader(open(RESOLVED, encoding="utf-8")))
    insample, forward = [], []
    for r in rows:
        if not _is_target(r):
            continue
        v = _f(r.get("avg_vol_7d_usd"))
        if v is None or v < VOL_MIN_USD:
            continue
        nr = _net_r(r)
        if nr is None:
            continue
        d = _date(r)
        if d is None:
            continue
        (forward if d >= FORWARD_START else insample).append(nr)

    print("=" * 70)
    print(f"  ФОРВАРД-ТРЕКЕР: short_dist/SHORT, ликвид ≥${VOL_MIN_USD/1e6:.0f}M, MAKER")
    print(f"  косты: fee {FEE_RT_PCT}% + slip {SLIP_RT_PCT}% + funding | горизонт {HORIZON}")
    print(f"  out-of-sample С {FORWARD_START} | цель {TARGET_N} сделок для go/no-go")
    print("=" * 70)

    a = _agg(insample)
    if a:
        tag = "✅" if (a["sig"] and a["mean"] > 0) else "❓"
        print(f"\nIN-SAMPLE (до {FORWARD_START}, ориентир): "
              f"n={a['n']} net_R={a['mean']:+.3f} WR={a['wr']:.0f}% "
              f"CI[{a['lo']:+.2f},{a['hi']:+.2f}] {tag}")

    fwd = _agg(forward)
    print(f"\nFORWARD (out-of-sample, с {FORWARD_START}):")
    if not fwd:
        print("  пока 0 сделок. Тест стартовал — возвращайся по мере накопления.")
    else:
        filled = min(fwd["n"], TARGET_N)
        bar = "#" * filled + "." * max(0, TARGET_N - fwd["n"])
        print(f"  [{bar}] {fwd['n']}/{TARGET_N}")
        print(f"  net_R={fwd['mean']:+.3f}  totR={fwd['total']:+.1f}  WR={fwd['wr']:.0f}%  "
              f"CI[{fwd['lo']:+.2f},{fwd['hi']:+.2f}]")
        if fwd["n"] < TARGET_N:
            print(f"  -> рано. Нужно ещё {TARGET_N - fwd['n']} сделок до вердикта.")
        elif fwd["sig"] and fwd["mean"] > 0:
            print("  -> ✅ GO: out-of-sample эдж ПОДТВЕРЖДЁН. Масштабировать малыми деньгами.")
        elif fwd["sig"] and fwd["mean"] < 0:
            print("  -> ⛔ STOP: значимо убыточно forward. Эджа нет.")
        else:
            print(f"  -> ❓ CI через ноль на {fwd['n']} сделках: эдж НЕ подтверждён. Не дотюнивать.")

    print("\nОграничение (честно): net-R считается ПРИ ДОПУЩЕНИИ, что maker-лимитка")
    print("исполнилась у входа. Реальный fill-rate (особенно пропуск моментальных дропов")
    print("= adverse selection) даст только бумажная/боевая лимитная торговля.")


if __name__ == "__main__":
    main()

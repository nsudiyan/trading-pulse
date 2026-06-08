#!/usr/bin/env python3
"""
C1 — стресс-разбор противоречия по short_dist/SHORT (РАЗБОР, не правка логики).

Цель: понять, почему «+0.288 (n=233) GO» из памяти штаба расходится с живым прогоном
shortdist_forward_pathresolved.py (сейчас −0.002, n=306, CI через ноль), и вынести вердикт
эдж РЕАЛЕН / РЕГИМ-ЗАВИСИМ / АРТЕФАКТ.

READ-ONLY по outcomes/resolved.csv. Формула net_R зеркалит форвард 1:1 (path-resolved,
maker fee+slip+funding). Дополнительно: by-week, ×2 slippage, воспроизведение исторических
срезов n=141/233/306 обрезкой по дате, кластеризация (эфф. n), gross/look-ahead vs path.

Запуск: python3 tools/c1_shortdist_stress.py
"""
from __future__ import annotations
import csv, math, os
from collections import defaultdict
from datetime import datetime, date

HERE     = os.path.dirname(os.path.abspath(__file__))
BASE     = os.path.dirname(HERE)
RESOLVED = os.path.join(BASE, "outcomes", "resolved.csv")

FORWARD_START   = "2026-05-29"
VOL_MIN_USD     = 50e6
FEE_RT_PCT      = 0.04
SLIP_RT_PCT     = 0.03
FUND_INTERVAL_H = 8.0
HORIZON         = "24h"


def _f(x):
    try: return float(x)
    except (TypeError, ValueError): return None

def _truthy(x):
    return str(x).strip() in ("1", "1.0", "True", "true")

def _is_target(r):
    return (r.get("setup") or "").strip() == "short_dist" and \
           (r.get("direction") or "").strip().upper() in ("ШОРТ", "SHORT")

def _gross_r(r, path_resolved=True, look_ahead=False):
    """look_ahead=True → сырой r_multiple (both-hit→TP, как outcome_tracker). path_resolved →
    both-hit разрешаем по таймингу MAE/MFE."""
    g = _f(r.get(f"r_multiple_{HORIZON}"))
    if g is None:
        return None
    if look_ahead:
        return g
    if path_resolved and _truthy(r.get(f"hit_tp1_{HORIZON}")) and _truthy(r.get(f"hit_stop_{HORIZON}")):
        tmfe = _f(r.get(f"time_to_mfe_{HORIZON}_h"))
        tmae = _f(r.get(f"time_to_mae_{HORIZON}_h"))
        if tmfe is not None and tmae is not None and tmae <= tmfe:
            return -1.0
    return g

def _net_r(r, slip=SLIP_RT_PCT, path_resolved=True, look_ahead=False):
    g    = _gross_r(r, path_resolved, look_ahead)
    e    = _f(r.get("price_entry")); s = _f(r.get("stop"))
    hold = _f(r.get(f"hold_time_{HORIZON}_min")); fund = _f(r.get("funding"))
    if g is None or e is None or s is None or e <= 0:
        return None
    risk = abs(e - s) / e * 100.0
    if risk <= 0:
        return None
    n_fund = (hold / 60.0 / FUND_INTERVAL_H) if hold else 0.0
    funding_cost = (-1 * (fund or 0.0)) * n_fund
    cost_r = (FEE_RT_PCT + slip + funding_cost) / risk
    return g - cost_r

def _date(r):
    ts = (r.get("run_ts") or "")[:10]
    try:
        datetime.strptime(ts, "%Y-%m-%d"); return ts
    except ValueError:
        return None

def _agg(xs):
    n = len(xs)
    if not n: return None
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n else 0.0
    lo, hi = m - 1.96 * se, m + 1.96 * se
    wr = 100.0 * sum(1 for x in xs if x > 0) / n
    return {"n": n, "mean": m, "total": sum(xs), "lo": lo, "hi": hi, "wr": wr, "sd": sd}

def _fmt(a):
    if not a: return "n=0"
    sig = "✅" if a["lo"] > 0 else ("⛔" if a["hi"] < 0 else "❓")
    return (f"n={a['n']:<3} net_R={a['mean']:+.3f} CI[{a['lo']:+.2f},{a['hi']:+.2f}] "
            f"WR={a['wr']:.0f}% totalR={a['total']:+.1f} {sig}")

def iso_week(d):
    y, w, _ = date(*map(int, d.split("-"))).isocalendar()
    return f"{y}-W{w:02d}"


def main():
    rows = list(csv.DictReader(open(RESOLVED, encoding="utf-8")))
    # форвард-выборка как в скрипте
    fwd = []   # (date, net_r, row)
    insample = []
    for r in rows:
        if not _is_target(r):
            continue
        v = _f(r.get("avg_vol_7d_usd"))
        if v is None or v < VOL_MIN_USD:
            continue
        nr = _net_r(r)
        d = _date(r)
        if nr is None or d is None:
            continue
        (fwd if d >= FORWARD_START else insample).append((d, nr, r))
    fwd.sort(key=lambda t: (t[0], t[2].get("run_ts", "")))

    print("=" * 78)
    print("  C1 СТРЕСС-РАЗБОР: short_dist/SHORT ликвид ≥$50M, path-resolved net_R (maker)")
    print("=" * 78)

    xs = [nr for _, nr, _ in fwd]
    print(f"\n[0] FORWARD весь (зеркало скрипта): {_fmt(_agg(xs))}")
    print(f"    IN-SAMPLE весь:                  {_fmt(_agg([nr for _,nr,_ in insample]))}")

    # ── [1] РЕПРОДУКЦИЯ исторических срезов обрезкой по порядку run_ts ──
    print("\n[1] РЕПРОДУКЦИЯ '+0.288': кумулятивный net_R по мере накопления форварда")
    print("    (срез = первые N сигналов в хронологии; показывает, был ли +0.288 пиком)")
    for N in (35, 70, 100, 141, 180, 200, 233, 260, 290, len(xs)):
        if N <= len(xs):
            a = _agg(xs[:N])
            print(f"    первые {N:>3}: net_R={a['mean']:+.3f}  CI[{a['lo']:+.2f},{a['hi']:+.2f}]  totalR={a['total']:+.1f}")

    # ── [2] ПО НЕДЕЛЯМ (регим-прокси по датам) ──
    print("\n[2] ПО НЕДЕЛЯМ (ISO) — где сидит плюс, держится ли вне отдельных недель:")
    byw = defaultdict(list)
    for d, nr, _ in fwd:
        byw[iso_week(d)].append(nr)
    for w in sorted(byw):
        a = _agg(byw[w])
        print(f"    {w}: {_fmt(a)}")
    # вклад лучшей недели в общий total
    best_w = max(byw, key=lambda w: sum(byw[w]))
    tot = sum(xs); tot_best = sum(byw[best_w])
    print(f"    → лучшая неделя {best_w}: totalR={tot_best:+.1f} из {tot:+.1f} "
          f"({100*tot_best/tot:.0f}% всего плюса/итога на одной неделе)" if tot else "")

    # ── [3] ПО ДНЯМ — пик и обвал ──
    print("\n[3] ПО ДНЯМ (кумулятивный итог) — пик и разворот:")
    byd = defaultdict(list)
    for d, nr, _ in fwd:
        byd[d].append(nr)
    cum = 0.0; cn = 0
    for d in sorted(byd):
        cum += sum(byd[d]); cn += len(byd[d])
        a = _agg(byd[d])
        print(f"    {d}: день n={len(byd[d]):>2} mean={a['mean']:+.2f}  |  кумул n={cn:>3} mean={cum/cn:+.3f} totalR={cum:+.1f}")

    # ── [4] КОСТЫ ×2 (slippage 0.03→0.06) ──
    print("\n[4] КОСТЫ ×2 (slippage удвоен 0.03→0.06%):")
    xs2 = [_net_r(r, slip=SLIP_RT_PCT * 2) for _, _, r in fwd]
    xs2 = [x for x in xs2 if x is not None]
    print(f"    forward ×2-slip: {_fmt(_agg(xs2))}")

    # ── [5] LOOK-AHEAD vs PATH (величина артефакта на форварде) ──
    print("\n[5] LOOK-AHEAD (сырой r_multiple, both-hit→TP) vs PATH-RESOLVED на ТОЙ ЖЕ выборке:")
    la = [_net_r(r, look_ahead=True) for _, _, r in fwd]; la = [x for x in la if x is not None]
    print(f"    look-ahead net_R: {_fmt(_agg(la))}")
    print(f"    path-resolved   : {_fmt(_agg(xs))}")
    bh = sum(1 for _, _, r in fwd if _truthy(r.get('hit_tp1_24h')) and _truthy(r.get('hit_stop_24h')))
    print(f"    both-hit (спорных) сделок: {bh} из {len(fwd)} ({100*bh/max(len(fwd),1):.0f}%)")

    # ── [6] КЛАСТЕРИЗАЦИЯ / НЕЗАВИСИМОСТЬ (эфф. n ≪ N) ──
    print("\n[6] КЛАСТЕРИЗАЦИЯ (CI наивно полагает независимость; реально эфф. n меньше):")
    sym = defaultdict(list)
    for _, nr, r in fwd:
        sym[r.get("symbol")].append(nr)
    ndays = len(byd); nsym = len(sym)
    print(f"    сигналов {len(fwd)} | уникальных дней {ndays} | уникальных монет {nsym}")
    print(f"    сигналов/день: медиана~{sorted(len(v) for v in byd.values())[len(byd)//2]}, "
          f"макс {max(len(v) for v in byd.values())}")
    top_sym = sorted(sym, key=lambda s: -len(sym[s]))[:5]
    print("    топ-монеты по числу сигналов: " +
          ", ".join(f"{s}×{len(sym[s])}(R{sum(sym[s]):+.1f})" for s in top_sym))
    # одна монета может доминировать total
    best_sym = max(sym, key=lambda s: sum(sym[s]))
    print(f"    монета с макс. вкладом: {best_sym} totalR={sum(sym[best_sym]):+.1f} "
          f"(n={len(sym[best_sym])})")

    print("\n" + "=" * 78)
    print("  Числа выше — детерминированы из resolved.csv (read-only). Вердикт — в C1_*.md")
    print("=" * 78)


if __name__ == "__main__":
    main()

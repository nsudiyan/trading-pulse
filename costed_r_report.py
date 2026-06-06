#!/usr/bin/env python3
"""
costed_r_report.py — ЧИСТЫЙ R после издержек (комиссия + проскальзывание + funding).

READ-ONLY: читает только outcomes/resolved.csv, НИЧЕГО не пишет, live-демоны не трогает.
Переиспользует валовый R из колонок r_multiple_4h / r_multiple_24h (определение —
outcome_tracker._compute_r_multiple = (exit-entry)/(entry-sl)). Новый R НЕ изобретается.

Косты — ЯВНЫЕ ОЦЕНКИ (не факты). Правь под свою реальность ниже.
Цель: ответить на вопрос совета — есть ли эдж ПОСЛЕ издержек, и значим ли он (CI ≠ 0).

Запуск:  python3 costed_r_report.py            (горизонт 24h, по умолчанию)
         python3 costed_r_report.py 4h          (горизонт 4h)
"""
from __future__ import annotations
import csv, math, os, sys
from collections import defaultdict

# ── КОСТ-ПРЕДПОЛОЖЕНИЯ (оценки, не факты — подгони под себя) ──────────────────
FEE_ROUNDTRIP_PCT      = 0.11   # Bybit перп тейкер ~0.055%/сторона × 2 (round-trip)
SLIPPAGE_ROUNDTRIP_PCT = 0.10   # оценка проскальзывания вход+выход (тонкие альты → больше)
FUNDING_INTERVAL_H     = 8.0    # Bybit списывает funding каждые 8ч
# funding в resolved.csv = ставка (%) НА ВХОДЕ. Приближение: держится постоянной за сделку.

HERE     = os.path.dirname(os.path.abspath(__file__))
RESOLVED = os.path.join(HERE, "outcomes", "resolved.csv")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _dir_sign(direction: str):
    d = (direction or "").strip().upper()
    if d in ("ЛОНГ", "LONG"):
        return +1
    if d in ("ШОРТ", "SHORT"):
        return -1
    return None


def costed_r(row: dict, horizon: str):
    """Вернёт dict(gross, net, direction, setup, funding_cost_pct) или None если данных мало."""
    gross   = _f(row.get(f"r_multiple_{horizon}"))
    entry   = _f(row.get("price_entry"))
    stop    = _f(row.get("stop"))
    sign    = _dir_sign(row.get("direction"))
    hold_m  = _f(row.get(f"hold_time_{horizon}_min"))
    funding = _f(row.get("funding"))          # % на входе
    if gross is None or entry is None or stop is None or sign is None or entry <= 0:
        return None
    risk_pct = abs(entry - stop) / entry * 100.0
    if risk_pct <= 0:
        return None
    # funding: ЛОНГ платит при funding>0; ШОРТ платит при funding<0 (ключевой улов совета)
    n_fund = (hold_m / 60.0 / FUNDING_INTERVAL_H) if hold_m else 0.0
    funding_cost_pct = (sign * (funding or 0.0)) * n_fund        # >0 = платим
    total_cost_pct   = FEE_ROUNDTRIP_PCT + SLIPPAGE_ROUNDTRIP_PCT + funding_cost_pct
    cost_r = total_cost_pct / risk_pct
    return {
        "gross": gross,
        "net":   gross - cost_r,
        "direction": "ЛОНГ" if sign > 0 else "ШОРТ",
        "setup": (row.get("setup") or "—").strip(),
        "funding_cost_pct": funding_cost_pct,
    }


def agg(nets: list[float]) -> dict | None:
    n = len(nets)
    if n == 0:
        return None
    mean = sum(nets) / n
    if n >= 2:
        var = sum((x - mean) ** 2 for x in nets) / (n - 1)
        sd = math.sqrt(var)
        se = sd / math.sqrt(n)
    else:
        se = 0.0
    lo, hi = mean - 1.96 * se, mean + 1.96 * se
    wr = 100.0 * sum(1 for x in nets if x > 0) / n
    return {"n": n, "mean": mean, "total": sum(nets), "wr": wr,
            "lo": lo, "hi": hi, "sig": (lo > 0 or hi < 0)}


def _line(label, gross_list, net_list):
    a = agg(net_list)
    if not a:
        return
    g_mean = sum(gross_list) / len(gross_list) if gross_list else 0.0
    flag = "✅ЗНАЧИМ" if a["sig"] and a["mean"] > 0 else ("⛔ОТРИЦ" if a["sig"] and a["mean"] < 0 else "❓шум(CI∋0)")
    print(f"  {label:<26} n={a['n']:>4}  gross_R={g_mean:+.3f}  net_R={a['mean']:+.3f}  "
          f"totNet={a['total']:>+8.1f}  WR={a['wr']:>4.1f}%  CI[{a['lo']:+.2f},{a['hi']:+.2f}]  {flag}")


def main():
    horizon = "24h"
    if len(sys.argv) > 1 and sys.argv[1] in ("4h", "24h"):
        horizon = sys.argv[1]
    if not os.path.exists(RESOLVED):
        print(f"НЕТ ФАЙЛА: {RESOLVED}")
        return

    rows = list(csv.DictReader(open(RESOLVED, encoding="utf-8")))
    results, skipped = [], 0
    for r in rows:
        cr = costed_r(r, horizon)
        if cr is None:
            skipped += 1
        else:
            results.append(cr)

    print("=" * 78)
    print(f"  COSTED-R ОТЧЁТ  |  горизонт {horizon}  |  resolved.csv: {len(rows)} строк")
    print(f"  Косты (ОЦЕНКА): fee {FEE_ROUNDTRIP_PCT}% + slippage {SLIPPAGE_ROUNDTRIP_PCT}% "
          f"+ funding(дин., {FUNDING_INTERVAL_H}ч) | использовано {len(results)}, пропущено {skipped} (нет R/entry/stop/dir)")
    print("=" * 78)

    gross_all = [x["gross"] for x in results]
    net_all   = [x["net"] for x in results]
    print("\n── ИТОГО ──")
    _line("ВСЕ сделки", gross_all, net_all)

    print("\n── ПО НАПРАВЛЕНИЮ ──")
    for d in ("ЛОНГ", "ШОРТ"):
        g = [x["gross"] for x in results if x["direction"] == d]
        nlist = [x["net"] for x in results if x["direction"] == d]
        _line(d, g, nlist)

    print("\n── ПО СЕТАПУ × НАПРАВЛЕНИЮ (n≥10, по totNet) ──")
    buckets = defaultdict(lambda: {"g": [], "n": []})
    for x in results:
        k = f"{x['setup']}/{x['direction']}"
        buckets[k]["g"].append(x["gross"])
        buckets[k]["n"].append(x["net"])
    ordered = sorted(buckets.items(),
                     key=lambda kv: sum(kv[1]["n"]) if len(kv[1]["n"]) >= 10 else -1e9,
                     reverse=True)
    for k, v in ordered:
        if len(v["n"]) >= 10:
            _line(k, v["g"], v["n"])

    # средняя funding-нагрузка по направлению — иллюстрация улова совета
    print("\n── СРЕДНЯЯ FUNDING-НАГРУЗКА (>0 = платим, съедает R) ──")
    for d in ("ЛОНГ", "ШОРТ"):
        fc = [x["funding_cost_pct"] for x in results if x["direction"] == d]
        if fc:
            print(f"  {d:<8} средний funding-cost = {sum(fc)/len(fc):+.4f}%  (n={len(fc)})")

    print("\nЛегенда: ✅ЗНАЧИМ = 95% CI чистого R не пересекает 0 и >0. "
          "❓шум = CI включает 0 (эдж не доказан). ⛔ОТРИЦ = значимо убыточно.")


if __name__ == "__main__":
    main()

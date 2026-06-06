#!/usr/bin/env python3
"""
backtests/shorts_path_sim.py — ЗЕРКАЛО шага 6 для ШОРТ-сигналов: path-level реплей.

Для каждого ШОРТ-сигнала проигрываем РЕАЛЬНЫЙ путь цены по 5m-свечам с трейлинг-стопом,
бар-за-баром, intra-bar КОНСЕРВАТИВНО. Инверсия относительно лонга:
  • ВХОД в шорт по price_entry.
  • ПРОФИТ когда цена ПАДАЕТ ниже входа.
  • HARD STOP ВЫШЕ входа: stop = entry*(1+sl0/100). Стоп выбивается, когда HIGH бара >= stop.
  • Трейлинг следует за минимумом (trough) вниз: подтягиваем стоп ВНИЗ по мере падения.
  • intra-bar консервативно: на каждом баре СНАЧАЛА проверяем, не выбило ли стоп (high>=stop),
    и только потом обновляем trough.

Метрики: expectancy %/сделка И в R (нормируем на sl0), WR, медиана.
Сравниваем дамп-объёмный сабсет (vol_score>=23) vs все (контроль).
Горизонт 24h (288 5m-баров). Офлайн (klines_5m на диске), без сети/API.

Комиссии: round-trip ≈ 0.3% (taker+слип, открытие+закрытие шорта). Применяется к каждой сделке.
Funding: за 24h-холд шорт на перпах с ПОЛОЖИТЕЛЬНЫМ funding ПОЛУЧАЕТ выплаты
  (медиана funding в выборке ≈ +0.005 за 8h ⇒ ~+0.015% за 24h в пользу шорта).
  Это мелкий ПОЛОЖИТЕЛЬНЫЙ вклад — отмечаем, но в базовый расчёт не зашиваем (см. флаг APPLY_FUNDING).

Воспроизводимо: python3 backtests/shorts_path_sim.py
"""
from __future__ import annotations
import csv, gzip, glob, os
from pathlib import Path
from statistics import mean, median
import numpy as np

BASE = Path(__file__).parent.parent
JD = BASE / "backtests" / "shorts_joined.csv"
KL5 = BASE / "pump_analysis" / "klines_5m"
HORIZON_BARS = 288  # 24h / 5m

FEE_ROUNDTRIP_PCT = 0.30   # round-trip комиссия+слип на перпах (открыть+закрыть шорт)
APPLY_FUNDING = False      # если True — прибавить funding-кредит за время в позиции
FUNDING_PER_8H_PCT = 0.005 # медиана funding-rate в выборке шортов (положит. => шорт получает)

_klcache = {}


def f(v):
    try: return float(v)
    except (TypeError, ValueError): return None


def load_5m(sym):
    if sym in _klcache:
        return _klcache[sym]
    cands = glob.glob(str(KL5 / f"{sym}*.csv.gz")) + glob.glob(str(KL5 / f"{sym}*"))
    df = None
    for p in cands:
        if os.path.basename(p).split('.')[0].upper() == sym.upper():
            try:
                op = gzip.open(p, "rt") if p.endswith(".gz") else open(p)
                import pandas as pd
                df = pd.read_csv(op)
                tcol = "open_time" if "open_time" in df.columns else ("ts" if "ts" in df.columns else df.columns[0])
                df = df.rename(columns={tcol: "ts"})
                df["ts"] = df["ts"].astype("int64")
                for c in ["open", "high", "low", "close"]:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
                df = df.sort_values("ts").reset_index(drop=True)
            except Exception:
                df = None
            break
    _klcache[sym] = df
    return df


def to_ms(run_ts):
    from datetime import datetime, timezone
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(run_ts.strip()[:19], fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    return None


def replay_short(sym, start_ms, entry, sl0_pct, mode, **p):
    """Реплей ШОРТ-трейлинга. Возврат: (gross_ret_pct, exit_reason, bars_held) или None.
    gross_ret_pct = прибыль шорта в % (положит. = цена упала). БЕЗ комиссий."""
    df = load_5m(sym)
    if df is None or entry is None or entry <= 0:
        return None
    ts = df["ts"].values
    i0 = int(np.searchsorted(ts, start_ms, side="left"))
    if i0 >= len(df) - 5:
        return None
    end = min(i0 + 1 + HORIZON_BARS, len(df))
    if end - i0 < 12:
        return None
    hi = df["high"].values; lo = df["low"].values; cl = df["close"].values

    hard_stop = entry * (1 + sl0_pct / 100)   # стоп ВЫШЕ входа для шорта
    stop = hard_stop
    trough = entry                            # минимум цены (благоприятно для шорта)

    def short_ret(price):  # прибыль шорта в % при выходе по price
        return (entry - price) / entry * 100

    for j in range(i0 + 1, end):
        # intra-bar консервативно: сначала проверяем, не выбило ли стоп (high пробил вверх)
        if hi[j] >= stop:
            return (short_ret(stop), "stop", j - i0)
        # обновляем trough (новый минимум)
        if lo[j] < trough:
            trough = lo[j]
        # трейлинг-правило (стоп подтягивается ВНИЗ)
        if mode == "giveback":
            # активируем трейл после падения на act%; стоп = trough*(1+gb/100)
            if trough <= entry * (1 - p["act"] / 100):
                ns = trough * (1 + p["gb"] / 100)
                if ns < stop:
                    stop = ns
        elif mode == "be_ladder":
            if trough <= entry * 0.95 and stop > entry:
                stop = entry              # безубыток после -5%
            if trough <= entry * 0.90 and stop > entry * 0.95:
                stop = entry * 0.95
            if trough <= entry * 0.80:
                ns = trough * (1 + p.get("gb", 7) / 100)
                if ns < stop:
                    stop = ns
        elif mode == "hard_only":
            pass
        elif mode == "fixed_tp":
            if lo[j] <= entry * (1 - p["tp"] / 100):
                return (p["tp"], "tp", j - i0)
    return (short_ret(cl[end - 1]), "horizon", end - 1 - i0)


def net_ret(gross_pct, bars_held):
    """gross минус комиссии (+ опц. funding-кредит шорта за время в позиции)."""
    net = gross_pct - FEE_ROUNDTRIP_PCT
    if APPLY_FUNDING:
        hours = bars_held * 5 / 60.0
        net += FUNDING_PER_8H_PCT * (hours / 8.0)  # положит. funding => шорт получает
    return net


def run(rows, label, configs):
    print(f"\n{'='*82}\n  {label}  ·  входов в выборке: {len(rows)}\n{'='*82}")
    print(f"  {'стратегия':34s} {'n':>4s} {'E %/tr':>8s} {'E (R)':>8s} {'WR':>6s} {'медиана%':>9s}")
    out = {}
    for name, sl0, mode, kw in configs:
        rets = []
        for r in rows:
            res = replay_short(r["symbol"], r["_ms"], r["_entry"], sl0, mode, **kw)
            if res is not None:
                rets.append(net_ret(res[0], res[2]))
        if not rets:
            continue
        e = mean(rets); er = e / sl0
        wr = sum(1 for x in rets if x > 0) / len(rets)
        med = float(median(rets))
        out[name] = {"n": len(rets), "E_pct": round(e, 3), "E_R": round(er, 3),
                     "WR": round(wr, 4), "median_pct": round(med, 3)}
        star = "  <-" if er > 0.15 else ""
        print(f"  {name:34s} {len(rets):>4d} {e:>+7.2f}% {er:>+7.3f}R {wr*100:>5.1f}% {med:>+8.2f}%{star}")
    return out


def main():
    rows_all, rows_hv, rows_casc = [], [], []
    with open(JD) as fh:
        for r in csv.DictReader(fh):
            ms = to_ms(r["run_ts"]); entry = f(r["price_entry"])
            if ms is None or entry is None:
                continue
            r["_ms"] = ms; r["_entry"] = entry
            rows_all.append(r)
            if f(r["vol_score"]) is not None and f(r["vol_score"]) >= 23:
                rows_hv.append(r)
            if r.get("btc_cascade") == "1":
                rows_casc.append(r)

    configs = [
        ("hard SL8, без трейла",            8, "hard_only", {}),
        ("фикс SL8 / TP20",                 8, "fixed_tp", {"tp": 20}),
        ("трейл SL8, act-5%, giveback4%",   8, "giveback", {"act": 5, "gb": 4}),
        ("трейл SL8, act-6%, giveback6%",   8, "giveback", {"act": 6, "gb": 6}),
        ("трейл SL10, act-8%, giveback8%",  10, "giveback", {"act": 8, "gb": 8}),
        ("BE-лесенка SL8 + трейл trough+7%", 8, "be_ladder", {"gb": 7}),
    ]
    print(f"FEE_ROUNDTRIP={FEE_ROUNDTRIP_PCT}%  APPLY_FUNDING={APPLY_FUNDING} "
          f"(funding медиана +{FUNDING_PER_8H_PCT}%/8h => шорт получает)")
    res_hv = run(rows_hv, "ДАМП-ОБЪЁМ vol_score>=23 (24h, реальный путь по 5m, NET комиссий)", configs)
    res_all = run(rows_all, "ВСЕ шорты (контроль, NET)", configs)
    if rows_casc:
        run(rows_casc, "btc_cascade=1 (валид. триггер)", configs)
    else:
        print(f"\n  btc_cascade=1: 0 входов в выборке — триггер не сработал ни разу в этом режиме, "
              f"его денежный edge на шортах ИЗМЕРИТЬ НЕЛЬЗЯ.")

    print("\nПримечание: NET = gross минус round-trip 0.3%. intra-bar консервативно (стоп раньше trough).")
    print("Вход = price_entry, выход по реальному 5m-пути. Funding (мелкий +) НЕ зашит (APPLY_FUNDING=False).")
    print("Чтобы признать денежным: E_R стабильно > ~0.2R после комиссий на дамп-объёмном сабсете.")


if __name__ == "__main__":
    main()

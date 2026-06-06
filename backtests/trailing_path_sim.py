#!/usr/bin/env python3
"""
backtests/trailing_path_sim.py — ШАГ 6 (определяющий): path-level реплей трейлинг-стопа.

Для каждого сигнала проигрываем РЕАЛЬНЫЙ путь цены по 5m-свечам с трейлинг-стопом
(не пик MFE, а честная симуляция бар-за-баром, intra-bar консервативно: стоп
проверяется ПЕРЕД обновлением пика). Даёт настоящую expectancy в R — сколько хвоста
реально ловится. Офлайн (klines_5m на диске), без сети/API.

Сравниваем high-vol сабсет (vol_score>=23) vs все. Горизонт 24h (288 5m-баров).
"""
from __future__ import annotations
import csv, gzip, glob, os
from pathlib import Path
from statistics import mean
import numpy as np

BASE = Path(__file__).parent.parent
JD = BASE / "backtests" / "joined_dataset.csv"
KL5 = BASE / "pump_analysis" / "klines_5m"
HORIZON_BARS = 288  # 24h / 5m

_klcache = {}


def f(v):
    try: return float(v)
    except (TypeError, ValueError): return None


def load_5m(sym):
    if sym in _klcache:
        return _klcache[sym]
    # файлы вида SYM.csv.gz
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


def replay(sym, start_ms, entry, sl0_pct, mode, **p):
    """Реплей трейлинг-стопа. Возврат: (ret_pct, exit_reason) или None если нет данных."""
    df = load_5m(sym)
    if df is None or entry is None or entry <= 0:
        return None
    ts = df["ts"].values
    i0 = int(np.searchsorted(ts, start_ms, side="left"))  # первый бар на/после входа
    if i0 >= len(df) - 5:
        return None
    end = min(i0 + 1 + HORIZON_BARS, len(df))
    if end - i0 < 12:  # слишком мало форвард-данных
        return None
    hi = df["high"].values; lo = df["low"].values; cl = df["close"].values

    hard_stop = entry * (1 - sl0_pct / 100)
    stop = hard_stop
    peak = entry
    for j in range(i0 + 1, end):
        # intra-bar консервативно: сначала проверяем выбило ли стоп
        if lo[j] <= stop:
            return ((stop / entry - 1) * 100, "stop")
        # обновляем пик
        if hi[j] > peak:
            peak = hi[j]
        # трейлинг-правило
        if mode == "giveback":
            if peak >= entry * (1 + p["act"] / 100):
                ns = peak * (1 - p["gb"] / 100)
                if ns > stop:
                    stop = ns
        elif mode == "be_ladder":
            if peak >= entry * 1.05 and stop < entry:
                stop = entry  # безубыток
            if peak >= entry * 1.10 and stop < entry * 1.05:
                stop = entry * 1.05
            if peak >= entry * 1.20:
                ns = peak * (1 - p.get("gb", 7) / 100)
                if ns > stop:
                    stop = ns
        elif mode == "hard_only":
            pass
        elif mode == "fixed_tp":
            if hi[j] >= entry * (1 + p["tp"] / 100):
                return (p["tp"], "tp")
    # вышли по закрытию горизонта
    return ((cl[end - 1] / entry - 1) * 100, "horizon")


def run(rows, label, configs):
    print(f"\n{'='*78}\n  {label}  ·  входов в выборке: {len(rows)}\n{'='*78}")
    print(f"  {'стратегия':34s} {'n':>4s} {'E %/tr':>8s} {'E (R)':>8s} {'WR':>6s} {'медиана%':>9s}")
    out = {}
    for name, sl0, mode, kw in configs:
        rets = []
        for r in rows:
            res = replay(r["symbol"], r["_ms"], r["_entry"], sl0, mode, **kw)
            if res is not None:
                rets.append(res[0])
        if not rets:
            continue
        e = mean(rets); er = e / sl0
        wr = sum(1 for x in rets if x > 0) / len(rets)
        med = float(np.median(rets))
        out[name] = {"n": len(rets), "E_pct": round(e, 3), "E_R": round(er, 3), "WR": round(wr, 4)}
        star = "  ←" if er > 0.15 else ""
        print(f"  {name:34s} {len(rets):>4d} {e:>+7.2f}% {er:>+7.3f}R {wr*100:>5.1f}% {med:>+8.2f}%{star}")
    return out


def main():
    rows_all, rows_hv = [], []
    with open(JD) as fh:
        for r in csv.DictReader(fh):
            ms = to_ms(r["run_ts"]); entry = f(r["price_entry"])
            if ms is None or entry is None:
                continue
            r["_ms"] = ms; r["_entry"] = entry
            rows_all.append(r)
            if f(r["vol_score"]) is not None and f(r["vol_score"]) >= 23:
                rows_hv.append(r)

    configs = [
        ("hard SL8, без трейла",          8, "hard_only", {}),
        ("фикс SL8 / TP20",               8, "fixed_tp", {"tp": 20}),
        ("трейл SL8, act+5%, giveback4%", 8, "giveback", {"act": 5, "gb": 4}),
        ("трейл SL8, act+6%, giveback6%", 8, "giveback", {"act": 6, "gb": 6}),
        ("трейл SL10, act+8%, giveback8%",10,"giveback", {"act": 8, "gb": 8}),
        ("BE-лесенка SL8 + трейл peak-7%",8, "be_ladder", {"gb": 7}),
    ]
    res_hv = run(rows_hv, "ВЫСОКИЙ ОБЪЁМ vol_score>=23 (24h, реальный путь по 5m)", configs)
    res_all = run(rows_all, "ВСЕ объёмы (контроль)", configs)
    print("\nПримечание: gross (без комиссий). Round-trip на Bybit-перпах ≈ 0.2-0.4% (taker+слип).")
    print("intra-bar консервативно (стоп раньше пика). Вход = price_entry, выход по 5m-пути.")
    print("Чтобы признать денежным: E_R стабильно > ~0.2R после комиссий на high-vol.")


if __name__ == "__main__":
    main()

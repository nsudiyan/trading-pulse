#!/usr/bin/env python3
"""
pump_fade_forward.py — честный форвард-тест ИНВЕРСИИ памп-сигналов.

Гипотеза (chart-commission 2026-06-01): сильный bullish памп-сигнал
(absorption_bull / ПРУЖИНА / PRE-BREAKOUT), особенно при extreme-neg funding +
высоком CVD — это НЕ накопление, а перегретый сквиз → откат. Значит ФЕЙД
(шорт выброса вверх) может быть +EV. Проверяем честно, в первую очередь на
ФОРВАРД-данных (ретро-май = 1 режим, НЕ доказательство).

Логика: для каждого pump-LONG сигнала тянем 15m klines [ts, ts+HORIZON_H].
Если цена ПЕРВОЙ достигает entry*(1+TRIGGER%) (сквиз состоялся) — открываем ШОРТ там.
TP = short_entry*(1-TP%) (откат ~к уровню сигнала), STOP = short_entry*(1+STOP%).
Path-resolve по барам; both-in-bar → консервативно STOP. net-of-cost.
Бакеты funding/CVD = прямой тест инверсии (фейд должен быть ЛУЧШЕ там, где
лонг был ХУЖЕ: ext-neg funding + CVD>=70).

Запускать периодически: pump_resolved.csv пополняется живым ботом → forward-срез копится.
"""
import csv
import json
import time as _time
import urllib.request
import statistics as st
from pathlib import Path
from datetime import datetime, timezone

HERE = Path(__file__).resolve().parent
PUMP_RESOLVED = HERE / "outcomes" / "pump_resolved.csv"
OUT_LOG = HERE / "outcomes" / "pump_fade_forward.json"
BYBIT = "https://api.bybit.com/v5/market/kline"

# ── Параметры фейда (геометрия в %) ──────────────────────────────────────────
TRIGGER_PCT = 3.0     # сквиз должен дойти до +TRIGGER% чтобы фейдить
TP_PCT      = 3.0     # цель шорта: откат на TP% от точки входа в шорт (≈ к entry сигнала)
STOP_PCT    = 3.0     # стоп: +STOP% выше точки входа в шорт
HORIZON_H   = 6       # окно (часы) для отыгрыша фейда
COST_PCT    = 0.1     # издержки за СТОРОНУ (round-trip = 2×)
FORWARD_CUTOFF_TS = datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp()


def fetch_klines(symbol, from_ts, horizon_h):
    end_ts = from_ts + horizon_h * 3600
    url = (f"{BYBIT}?category=linear&symbol={symbol}&interval=15"
           f"&start={int(from_ts * 1000)}&end={int(end_ts * 1000)}&limit=200")
    try:
        d = json.load(urllib.request.urlopen(url, timeout=15))
        bars = sorted(d["result"]["list"], key=lambda b: int(b[0]))
        return [(int(b[0]) // 1000, float(b[2]), float(b[3]), float(b[4])) for b in bars]
    except Exception:
        return []


def fade_trade(entry, bars):
    """Фейд-шорт. Возвращает (status, R_gross). status: win/loss/flat/no_spike/no_data."""
    if not bars:
        return ("no_data", 0.0)
    trig = entry * (1 + TRIGGER_PCT / 100)
    start_i = next((i for i, b in enumerate(bars) if b[1] >= trig), None)
    if start_i is None:
        return ("no_spike", 0.0)               # сквиз не состоялся → фейдить нечего
    se = trig
    tp = se * (1 - TP_PCT / 100)
    stop = se * (1 + STOP_PCT / 100)
    risk = stop - se
    for (_t, h, l, _c) in bars[start_i + 1:]:
        if l <= tp and h >= stop:
            return ("loss", -1.0)              # both-in-bar → консервативно стоп
        if h >= stop:
            return ("loss", -1.0)
        if l <= tp:
            return ("win", (se - tp) / risk)
    c_last = bars[-1][3]                        # ни тейк, ни стоп → выход по close
    return ("flat", (se - c_last) / risk)


def cost_R():
    # round-trip издержки в R: (2*COST_PCT)% цены / (STOP_PCT)% риска
    return (2 * COST_PCT) / STOP_PCT


def fbucket(f):
    try:
        f = float(f)
    except (TypeError, ValueError):
        return "?"
    if f <= -0.1:
        return "ext_neg(<=-0.1)"
    if f <= -0.01:
        return "mild_neg"
    return "flat/pos"


def cbucket(c):
    try:
        c = float(c)
    except (TypeError, ValueError):
        return "?"
    if c >= 70:
        return "CVD>=70"
    if c >= 50:
        return "CVD50-70"
    return "CVD<50"


def summarize(items, label):
    traded = [x for x in items if x["netR"] is not None]
    nospike = sum(1 for x in items if x["status"] == "no_spike")
    if not traded:
        print(f"  {label:22} сделок=0 (no_spike={nospike}, total={len(items)})")
        return
    wins = sum(1 for x in traded if x["netR"] > 0)
    meanR = st.mean(x["netR"] for x in traded)
    medR = st.median(x["netR"] for x in traded)
    print(f"  {label:22} сделок={len(traded):>3} (no_spike={nospike:>3}) "
          f"WR={100 * wins / len(traded):>3.0f}% meanNetR={meanR:+.3f} medNetR={medR:+.3f}")


def main():
    if not PUMP_RESOLVED.exists():
        print(f"нет файла {PUMP_RESOLVED}")
        return
    rows = [r for r in csv.DictReader(open(PUMP_RESOLVED, encoding="utf-8"))
            if r.get("signal_type") == "pump"]
    cR = cost_R()
    results = []
    for r in rows:
        try:
            ts = float(r["ts"])
            entry = float(r["price"])
        except (TypeError, ValueError, KeyError):
            continue
        if entry <= 0:
            continue
        bars = fetch_klines(r["symbol"], ts, HORIZON_H)
        status, rg = fade_trade(entry, bars)
        netR = (rg - cR) if status in ("win", "loss", "flat") else None
        results.append({
            "sym": r["symbol"], "ts": ts, "funding": r.get("funding"),
            "cvd": r.get("cvd_pct"), "score": r.get("score"),
            "status": status, "grossR": round(rg, 3),
            "netR": round(netR, 3) if netR is not None else None,
            "fwd": ts >= FORWARD_CUTOFF_TS,
        })
        _time.sleep(0.1)

    print(f"=== ФЕЙД ПАМП-СИГНАЛОВ ===  шорт сквиза +{TRIGGER_PCT}%, "
          f"TP {TP_PCT}%, stop {STOP_PCT}%, окно {HORIZON_H}ч, costR={cR:.3f}")
    print(f"всего pump-LONG сигналов: {len(rows)}")

    retro = [x for x in results if not x["fwd"]]
    fwd = [x for x in results if x["fwd"]]

    print("\n[РЕТРО (май 2026 = 1 РЕЖИМ, НЕ доказательство — только проверка инструмента)]")
    summarize(retro, "overall")
    print("  -- по funding (инверсия: ext_neg должен фейдиться ЛУЧШЕ) --")
    for b in ("ext_neg(<=-0.1)", "mild_neg", "flat/pos"):
        summarize([x for x in retro if fbucket(x["funding"]) == b], f"funding {b}")
    print("  -- по CVD (инверсия: CVD>=70 должен фейдиться ЛУЧШЕ) --")
    for b in ("CVD>=70", "CVD50-70", "CVD<50"):
        summarize([x for x in retro if cbucket(x["cvd"]) == b], b)

    print("\n[FORWARD (ts>=2026-06-01) — ЧИСТЫЙ OOS, копится с каждым новым сигналом]")
    summarize(fwd, "overall forward")

    json.dump({
        "generated": datetime.now(timezone.utc).isoformat(),
        "params": {"TRIGGER_PCT": TRIGGER_PCT, "TP_PCT": TP_PCT, "STOP_PCT": STOP_PCT,
                   "HORIZON_H": HORIZON_H, "COST_PCT": COST_PCT},
        "n_total": len(results),
        "n_forward": len(fwd),
        "results": results,
    }, open(OUT_LOG, "w"), indent=1)
    print(f"\nлог сохранён: {OUT_LOG}")
    print("ПОВТОРНЫЙ ЗАПУСК через ~неделю даст растущий forward-срез на НОВЫХ сигналах.")


if __name__ == "__main__":
    main()

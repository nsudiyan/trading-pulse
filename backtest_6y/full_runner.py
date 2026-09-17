#!/usr/bin/env python3
"""ПОЛНЫЙ ПРОГОН (единый запуск, PREREG 2026-07-10): эффективный торговый sample
2021-10-17…2026-07-10 (правило широты ≥30), кусками по ~6 мес с переносом состояния
(cd/budget/open_ex) — семантически ОДИН непрерывный прогон. Прогрев с 2021-10-15,
сделки/дни до 2021-10-17 отбрасываются. stdout БЕЗ PnL — только состав; вся доходность
уходит в out/*.csv и открывается только на этапе RESULTS."""
import json, gzip, os, csv
from datetime import datetime, timezone
from core_replay import run_engine, daily_series, BAR

ROOT = os.path.expanduser("~/trading/backtest_6y")
os.makedirs(f"{ROOT}/out", exist_ok=True)
def ms(s): return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()*1000)
WARM, ACCT, END = ms("2021-10-15"), ms("2021-10-17"), ms("2026-07-10")
CHUNKS = ["2021-10-15", "2022-04-01", "2022-10-01", "2023-04-01", "2023-10-01",
          "2024-04-01", "2024-10-01", "2025-04-01", "2025-10-01", "2026-04-01", "2026-07-10"]
COSTS = (0.14, 0.31, 0.71)

uni_all = json.load(gzip.open(f"{ROOT}/data/universe.json.gz", "rt"))
state = {}
all_trades, totals = [], {}
daily = {c: {} for c in COSTS}
for i in range(len(CHUNKS) - 1):
    a, b = ms(CHUNKS[i]), ms(CHUNKS[i+1])
    lo, hi = a - 3*86400_000, b + 3*86400_000
    days = [d for d in uni_all if CHUNKS[i] <= d < CHUNKS[i+1]]
    need = sorted({s for d in days for s in uni_all[d]} |
                  {s for s in (state.get("open_syms") or [])})
    bars, fund = {}, {}
    for s in need:
        p = f"{ROOT}/data/klines/{s}.json.gz"
        if not os.path.exists(p): continue
        bb = {int(k): v for k, v in json.load(gzip.open(p, "rt")).items() if lo <= int(k) <= hi}
        if bb: bars[s] = bb
        pf = f"{ROOT}/data/funding/{s}.json.gz"
        if os.path.exists(pf):
            fund[s] = sorted((int(k), v) for k, v in json.load(gzip.open(pf, "rt")).items()
                             if lo <= int(k) <= hi)
    uni = {d: uni_all[d] for d in days}
    trades, counters = run_engine(bars, fund, uni, a, b, state=state, censor_end=END)
    trades = [t for t in trades if t["signal_bar"] + BAR >= ACCT]
    for c in COSTS:
        for d, v in daily_series(trades, bars, cost_rt=c).items():
            if d >= "2021-10-17": daily[c][d] = daily[c].get(d, 0.0) + v
    all_trades += trades
    for k, v in counters.items(): totals[k] = totals.get(k, 0) + v
    state["open_syms"] = [t["symbol"] for t in trades if t["exit_ts"] > b - 3*86400_000]
    print(f"кусок {CHUNKS[i]}…{CHUNKS[i+1]}: символов {len(bars)}, сделок {len(trades)} "
          f"(всего {len(all_trades)})", flush=True)

with open(f"{ROOT}/out/trades.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(all_trades[0].keys()))
    w.writeheader(); w.writerows(all_trades)
dates = sorted(set().union(*[set(daily[c]) for c in COSTS]))
with open(f"{ROOT}/out/daily_pnl.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["date", "ret_c014", "ret_c031", "ret_c071"])
    for d in dates: w.writerow([d] + [round(daily[c].get(d, 0.0), 6) for c in COSTS])
json.dump({"counters": totals, "chunks": CHUNKS, "n_trades": len(all_trades),
           "acct_start": "2021-10-17", "end": "2026-07-10",
           "generated": datetime.now(timezone.utc).isoformat()},
          open(f"{ROOT}/out/meta.json", "w"), indent=1)
by_kind = {}
for t in all_trades: by_kind[t["kind"]] = by_kind.get(t["kind"], 0) + 1
print(f"\nПОЛНЫЙ ПРОГОН ЗАВЕРШЁН: сделок {len(all_trades)} {by_kind}, "
      f"торговых дат {len(dates)}, counters={ {k: v for k, v in totals.items() if v} }")
print("PnL записан в out/ — НЕ печатается; открытие только на этапе RESULTS")

#!/usr/bin/env python3
"""PIT-вселенная (PREREG §2): на каждый UTC-день — top-150 по turnover ПРЕДЫДУЩЕГО
дня из собственных скачанных баров. Допуск: first_bar ≤ D−48ч И last_bar ≥ D.
Исключения = боевые (is_stablecoin/is_commodity из vol_radar) + symbolType=='stock'
из снапшота инструментов. Манифест на каждый день: n_alive, исключения по причинам, топ с turnover.
Запуск: python3 universe_pit.py [--from 2026-04-01] [--to 2026-07-10]"""
import sys, os, json, gzip, argparse
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.expanduser("~/trading"))
from vol_radar import is_stablecoin, is_commodity

ROOT = os.path.expanduser("~/trading/backtest_6y")
DAY = 86400_000
TOP_N = 150

def build(d_from, d_to, kdir=None, out_suffix=""):
    ins = json.load(open(f"{ROOT}/data/instruments_2026-07-10.json"))
    stocks = {i["symbol"] for i in ins if i.get("symbolType") == "stock"}
    kdir = kdir or f"{ROOT}/data/klines"
    syms = [f[:-8] for f in os.listdir(kdir) if f.endswith(".json.gz")]
    # дневной turnover + границы данных по каждому символу
    turn, bounds = {}, {}
    for n, s in enumerate(sorted(syms)):
        try:
            bars = json.load(gzip.open(f"{kdir}/{s}.json.gz", "rt"))
        except Exception as e:                      # полузаписанный gz при параллельной качке
            print(f"  ⚠ {s}: нечитаем ({e}) — пропущен"); continue
        if not bars: continue
        ts = sorted(int(k) for k in bars)
        bounds[s] = (ts[0], ts[-1])
        dt_ = {}
        for k, b in bars.items():
            d = datetime.fromtimestamp(int(k)/1000, timezone.utc).strftime("%Y-%m-%d")
            dt_[d] = dt_.get(d, 0.0) + (b[6] if len(b) > 6 else 0.0)
        turn[s] = dt_
        if (n+1) % 100 == 0: print(f"  …turnover {n+1}/{len(syms)}", flush=True)

    uni, manifest = {}, {}
    cur = datetime.strptime(d_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(d_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    while cur <= end:
        D = cur.strftime("%Y-%m-%d")
        D0 = int(cur.timestamp()*1000)
        prev = (cur - timedelta(days=1)).strftime("%Y-%m-%d")
        excl = {"stable": 0, "commodity": 0, "stock": 0, "young": 0, "dead": 0, "no_turnover": 0}
        alive = []
        for s, (fb, lb) in bounds.items():
            if is_stablecoin(s):   excl["stable"] += 1;    continue
            if is_commodity(s):    excl["commodity"] += 1; continue
            if s in stocks:        excl["stock"] += 1;     continue
            if fb > D0 - 2*DAY:    excl["young"] += 1;     continue
            if lb < D0:            excl["dead"] += 1;      continue
            tv = turn.get(s, {}).get(prev, 0.0)
            if tv <= 0:            excl["no_turnover"] += 1; continue
            alive.append((s, tv))
        alive.sort(key=lambda x: -x[1])
        top = alive[:TOP_N]
        uni[D] = [s for s, _ in top]
        manifest[D] = {"n_alive": len(alive), "excluded": excl,
                       "top_min_turnover": round(top[-1][1]) if top else 0,
                       "top": [(s, round(tv)) for s, tv in top]}
        cur += timedelta(days=1)
    save = lambda p, o: json.dump(o, gzip.open(p, "wt"), separators=(",", ":"))
    save(f"{ROOT}/data/universe{out_suffix}.json.gz", uni)
    save(f"{ROOT}/data/universe_manifest{out_suffix}.json.gz", manifest)
    days = sorted(uni)
    widths = [len(v) for v in uni.values()]
    print(f"вселенная: {days[0]}…{days[-1]}, дней {len(days)}, ширина мин/мед/макс = "
          f"{min(widths)}/{sorted(widths)[len(widths)//2]}/{max(widths)}")
    thirty = next((d for d in days if len(uni[d]) >= 30), None)
    print(f"первый день с шириной ≥30 (старт портфельного учёта, PREREG §2): {thirty}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", default="2020-04-01")
    ap.add_argument("--to", dest="d_to", default="2026-07-09")
    ap.add_argument("--kdir", default=None)
    ap.add_argument("--suffix", default="")
    a = ap.parse_args()
    build(a.d_from, a.d_to, a.kdir, a.suffix)

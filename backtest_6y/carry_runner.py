#!/usr/bin/env python3
"""H-CARRY-01 — ЕДИНЫЙ ЗАПУСК по замороженному предрегу (2026-07-11).
Параметры зашиты из PREREG §8 (одобрены Codex), правки №1-2 в ядре/портфеле.
Печатает вердикт механически; сохраняет out/carry_results.json + episodes."""
import json, gzip, os, csv, random
from datetime import datetime, timezone, timedelta
from statistics import mean, pstdev
from carry_core import run_carry, resample_1h

ROOT = os.path.expanduser("~/trading/backtest_6y")
rnd = random.Random(20260711)
def ms(s): return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()*1000)
def ud(t): return datetime.fromtimestamp(t/1000, timezone.utc).strftime("%Y-%m-%d")

P = dict(entry_th=0.10, exit_th=0.03, fee_spot=0.0010, fee_perp=0.00055,
         m=0.25, maint=0.125)                       # PREREG §8, заморожено
SLIPS = (0.0002, 0.0005, 0.0010)                    # per-leg-execution; base = 0.0005
ASSETS = {"BTCUSDT": "2021-07-01", "ETHUSDT": "2021-07-01", "SOLUSDT": "2022-01-01"}
T1 = ms("2026-07-10")

def load(sym):
    spot = {int(k): v[:4] for k, v in json.load(gzip.open(f"{ROOT}/data/spot/{sym}.json.gz", "rt")).items()}
    p30 = {int(k): v for k, v in json.load(gzip.open(f"{ROOT}/data/klines/{sym}.json.gz", "rt")).items()}
    perp = resample_1h(p30)
    fund = sorted((int(k), v) for k, v in json.load(gzip.open(f"{ROOT}/data/funding/{sym}.json.gz", "rt")).items())
    return spot, perp, fund

def drange(a, b):
    d = datetime.strptime(a, "%Y-%m-%d")
    while d.strftime("%Y-%m-%d") <= b:
        yield d.strftime("%Y-%m-%d"); d += timedelta(days=1)
WIN = {"full": ("2021-07-01", "2026-07-09"), "train": ("2021-07-01", "2023-12-31"),
       "val": ("2024-01-01", "2024-12-31"), "test_verdict": ("2025-01-01", "2026-04-30"),
       "contaminated": ("2026-05-01", "2026-07-09"), "2022": ("2022-01-01", "2022-12-31"),
       "2023": ("2023-01-01", "2023-12-31"), "2025": ("2025-01-01", "2025-12-31")}
def stats(xs):
    n = len(xs); mu = mean(xs); sd = pstdev(xs)*(n/(n-1))**0.5 if n > 1 else 0
    t = mu/(sd/n**0.5) if sd else 0
    cum = mx = dd = 0.0
    for x in xs:
        cum += x; mx = max(mx, cum); dd = min(dd, cum - mx)
    return {"total": round(sum(xs), 2), "t": round(t, 2),
            "sharpe": round(mu/sd*365**0.5, 2) if sd else 0, "maxDD": round(dd, 2)}
def boot(xs, k=10000):
    nb = max(1, len(xs)//7); blocks = [xs[i*7:(i+1)*7] for i in range(nb)]
    tot = sorted(sum(v for bl in (rnd.choice(blocks) for _ in range(nb)) for v in bl) for _ in range(k))
    return round(tot[int(.025*k)], 2), round(tot[int(.975*k)], 2)

res, sleeves = {}, {}
for sym, start in ASSETS.items():
    spot, perp, fund = load(sym)
    for slip in SLIPS:
        r = run_carry(spot, perp, fund, slip=slip, t0=ms(start), t1=T1, **P)
        res[(sym, slip)] = r
        # sleeve-дневная серия: последний hourly-эквити дня, приращения ×100 (%)
        by_day = {}
        for t, e in sorted(r["hourly"].items()): by_day[ud(t)] = e
        dd_, prev = {}, 0.0
        for d in drange(*WIN["full"]):
            e = by_day.get(d, prev)
            dd_[d] = (e - prev)*100; prev = e
        sleeves[(sym, slip)] = dd_
port = {slip: {d: mean(sleeves[(s, slip)][d] for s in ASSETS) for d in sleeves[("BTCUSDT", slip)]}
        for slip in SLIPS}

print("═══════ H-CARRY-01: RESULTS (единственное вскрытие) ═══════")
print("cash-and-carry BTC/ETH/SOL · косты = КОНСЕРВАТИВНАЯ МОДЕЛЬ (базовая сетка Bybit)")
print("портфель = среднее трёх фиксированных sleeve по 1/3, без перетоков\n")
S = {}
print(f"{'окно':>13} {'slip':>7} | {'net%':>7} {'t':>6} {'Sharpe':>6} {'maxDD':>7}")
for slip in SLIPS:
    for w in WIN:
        xs = [port[slip][d] for d in drange(*WIN[w])]
        S[(w, slip)] = stats(xs)
        if slip == 0.0005 or w in ("full", "val", "test_verdict"):
            s = S[(w, slip)]
            print(f"{w:>13} {slip:>7} | {s['total']:>7} {s['t']:>6} {s['sharpe']:>6} {s['maxDD']:>7}")
for w in ("full", "val", "test_verdict"):
    lo, hi = boot([port[0.0005][d] for d in drange(*WIN[w])])
    print(f"bootstrap {w}@base: 95% CI [{lo}, {hi}]")

print("\n── по активам (base slip) ──")
tot_breach = 0
all_eps = []
for sym, start in ASSETS.items():
    r = res[(sym, 0.0005)]; c = r["counters"]; eps = r["episodes"]
    tot_breach += c["breach"]
    dur = sum(e["t_out"] - e["t_in"] for e in eps)/3600_000
    span = (T1 - ms(start))/3600_000
    open_end = c["entries"] > c["exits"]
    fund_sum = sum(e["fund"] for e in eps); fee_sum = sum(e["fees"] for e in eps)
    s = stats([sleeves[(sym, 0.0005)][d] for d in drange(*WIN["full"])])
    print(f"  {sym}: net {s['total']}% (t={s['t']}) · эпизодов {len(eps)} · в позиции "
          f"{100*dur/span:.0f}% времени · funding +{fund_sum*100:.1f}% · fees −{fee_sum*100:.1f}% "
          f"· breach {c['breach']} · открыта_в_конце={open_end}")
    for e in eps: all_eps.append({"sym": sym, **{k: (round(v, 6) if isinstance(v, float) else v) for k, v in e.items()}})
with open(f"{ROOT}/out/carry_episodes.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(all_eps[0].keys())); w.writeheader(); w.writerows(all_eps)

print("\n── ВЕРДИКТ ПО §5 (механически) ──")
ok1 = all(S[(w, 0.0005)]["total"] > 0 for w in ("full", "val", "test_verdict"))
f_b, f_w = S[("full", 0.0005)]["total"], S[("full", 0.0010)]["total"]
ok2 = ((S[("test_verdict", 0.0010)]["total"] > 0) == (S[("test_verdict", 0.0005)]["total"] > 0)) \
      and (f_w > -0.5*abs(f_b) if f_b < 0 else f_w > 0.5*f_b if f_b > 0 else True)
ok3 = S[("test_verdict", 0.0005)]["total"] > 0 and S[("test_verdict", 0.0005)]["t"] >= 2.0
ok4 = tot_breach <= 2
print(f"1) net>0 @base (full+val+test_verdict): {ok1}")
print(f"2) выживает worst-slip: {ok2}")
print(f"3) test_verdict: net>0 и t≥2.0: {ok3} (t={S[('test_verdict', 0.0005)]['t']})")
print(f"4) breach ≤2: {ok4} (всего {tot_breach})")
# §5 замороженного предрега: ВСЕ четыре ворот, иначе KILLED (правка Codex №4 —
# формула приведена к frozen-тексту; INCONCLUSIVE в §5 carry не существует)
verdict = "CONFIRMED FOR PAPER ONLY" if (ok1 and ok2 and ok3 and ok4) else "KILLED / NO-TRADE"
print(f"\n════ ВЕРДИКТ: {verdict} ════")
json.dump({"verdict": verdict, "gates": [ok1, ok2, ok3, ok4], "breach": tot_breach,
           "S": {f"{w}@{s_}": v for (w, s_), v in S.items()}},
          open(f"{ROOT}/out/carry_results.json", "w"), indent=1)
print("сохранено: out/carry_results.json, out/carry_episodes.csv")

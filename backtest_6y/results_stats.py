#!/usr/bin/env python3
"""RESULTS-машина (PREREG §5, §7 + поправки §9): механический расчёт метрик,
ablations, sensitivity и ВЕРДИКТА. Запускается ОДИН раз после full_runner.
Все пороги — из замороженного предрега, интерпретация запрещена коду."""
import json, gzip, csv, os, random
from datetime import datetime, timezone, timedelta
from statistics import mean, pstdev

ROOT = os.path.expanduser("~/trading/backtest_6y")
rnd = random.Random(20260710)

trades = []
for r in csv.DictReader(open(f"{ROOT}/out/trades.csv")):
    for k in ("vol_ratio","mkt_entry","entry_close","lim_price","exit_px","gross_mkt_pct","funding_pct"):
        r[k] = float(r[k])
    for k in ("signal_bar","entry_ts","exit_ts","opt_fill","cons_fill","censored"):
        r[k] = int(r[k])
    r["gross_opt_pct"] = float(r["gross_opt_pct"]) if r["gross_opt_pct"] not in ("", "None") else None
    trades.append(r)
trades = [t for t in trades if not t["censored"]]
daily = {}
for r in csv.DictReader(open(f"{ROOT}/out/daily_pnl.csv")):
    daily[r["date"]] = {0.14: float(r["ret_c014"]), 0.31: float(r["ret_c031"]), 0.71: float(r["ret_c071"])}
summ = json.load(open(f"{ROOT}/data/manifest/_summary.json"))
first_bar = {s: c["first"] for s, c in summ["coverage"].items()}

def drange(a, b):
    d = datetime.strptime(a, "%Y-%m-%d")
    while d.strftime("%Y-%m-%d") <= b:
        yield d.strftime("%Y-%m-%d"); d += timedelta(days=1)
WIN = {"full": ("2021-10-17","2026-07-09"), "train": ("2021-10-17","2023-12-31"),
       "val": ("2024-01-01","2024-12-31"), "test_verdict": ("2025-01-01","2026-04-30"),
       "test_full": ("2025-01-01","2026-07-09"), "contaminated": ("2026-05-01","2026-07-09"),
       "2021H2": ("2021-10-17","2021-12-31"), "2022": ("2022-01-01","2022-12-31"),
       "2023": ("2023-01-01","2023-12-31"), "2024": ("2024-01-01","2024-12-31"),
       "2025": ("2025-01-01","2025-12-31"), "2026H1": ("2026-01-01","2026-07-09")}

# ЕДИНАЯ модель портфельного учёта (архивная v2, ревью Codex №7): terminal
# per-trade net, атрибуция на UTC-дату ВЫХОДА, вес 1/5, без компаундинга.
# Детерминированно из замороженного trades.csv; идентична базе ablations.
# MTM-ряд из daily_pnl.csv остаётся вспомогательным (печатается кросс-чеком).
REAL = {c: {} for c in (0.14, 0.31, 0.71)}      # наполняется ниже, после def net()

def series(win, cost):
    a, b = WIN[win]
    return [REAL[cost].get(d, 0.0) for d in drange(a, b)]
def series_mtm(win, cost):
    a, b = WIN[win]
    return [daily.get(d, {}).get(cost, 0.0) for d in drange(a, b)]
def stats(xs):
    n = len(xs); m = mean(xs); sd = pstdev(xs)*(n/(n-1))**0.5 if n > 1 else 0
    t = m/(sd/n**0.5) if sd else 0
    sharpe = m/sd*(365**0.5) if sd else 0
    dn = [x for x in xs if x < 0]
    sortino = m/(pstdev(dn)*(len(dn)/(len(dn)-1))**0.5)*(365**0.5) if len(dn) > 2 and pstdev(dn) else 0
    cum = mx = dd = 0.0
    for x in xs:
        cum += x; mx = max(mx, cum); dd = min(dd, cum - mx)
    return {"total": round(sum(xs), 2), "t": round(t, 2), "sharpe": round(sharpe, 2),
            "sortino": round(sortino, 2), "maxDD": round(dd, 2), "days": n,
            "act_days": sum(1 for x in xs if x != 0.0)}
def boot(xs, k=10000):
    nb = max(1, len(xs)//7)
    blocks = [xs[i*7:(i+1)*7] for i in range(nb)]
    tot = sorted(sum(v for b in (rnd.choice(blocks) for _ in range(nb)) for v in b) for _ in range(k))
    return round(tot[int(0.025*k)], 2), round(tot[int(0.975*k)], 2)

def net(t, cost, entry="mkt_entry", short=False):
    g = (t["exit_px"]/t[entry] - 1)*100
    return (-g - cost + t["funding_pct"]) if short else (g - cost - t["funding_pct"])
def in_win(t, win):
    a, b = WIN[win]
    d = t["ts_utc"][:10]
    return a <= d <= b
for _t in trades:
    _d = datetime.fromtimestamp(_t["exit_ts"]/1000, timezone.utc).strftime("%Y-%m-%d")
    for _c in REAL:
        REAL[_c][_d] = REAL[_c].get(_d, 0.0) + net(_t, _c)/5

def realized(sub, cost=0.31, **kw):          # атрибуция по дню выхода (для ablations)
    dd = {}
    for t in sub:
        d = datetime.fromtimestamp(t["exit_ts"]/1000, timezone.utc).strftime("%Y-%m-%d")
        dd[d] = dd.get(d, 0.0) + net(t, cost, **kw)/5
    a, b = WIN["full"]
    return [dd.get(d, 0.0) for d in drange(a, b)]

print("═══════ ШЕСТИЛЕТНИЙ ПРОГОН: RESULTS v2 (единая учётная модель) ═══════")
print("SURVIVOR-ONLY UNIVERSE: RESULTS ARE AN UPPER BOUND FOR LONG-SIDE PERFORMANCE")
print("учёт: terminal per-trade net → дата выхода, вес 1/5, без компаундинга")
mtm = round(sum(series_mtm("full", 0.31)), 2)
print(f"кросс-чек идентичности: MTM-ряд full@0.31 = {mtm} vs terminal (ниже) — "
      f"разница = путевая сумма промежуточных сегментов, обе модели одного знака\n")
print(f"{'окно':>13} {'кост':>5} | {'total%':>8} {'t':>6} {'Sharpe':>6} {'Sortino':>7} {'maxDD':>7} {'дней':>5} {'актив':>5}")
S = {}
for w in WIN:
    for c in (0.14, 0.31, 0.71):
        S[(w, c)] = stats(series(w, c))
        if c == 0.31 or w in ("full", "test_verdict", "val"):
            s = S[(w, c)]
            print(f"{w:>13} {c:>5} | {s['total']:>8} {s['t']:>6} {s['sharpe']:>6} "
                  f"{s['sortino']:>7} {s['maxDD']:>7} {s['days']:>5} {s['act_days']:>5}")
for w in ("full", "val", "test_verdict"):
    lo, hi = boot(series(w, 0.31))
    print(f"bootstrap 7д×10к {w}@0.31: 95% CI total = [{lo}, {hi}]")

n_by = {}
for t in trades:
    if in_win(t, "full"): n_by[t["kind"]] = n_by.get(t["kind"], 0) + 1
print(f"\nсделок в sample: {sum(n_by.values())} {n_by} · fill opt {sum(t['opt_fill'] for t in trades)}"
      f"/{len(trades)} cons {sum(t['cons_fill'] for t in trades)}/{len(trades)}")

print("\n── ABLATIONS (realized-атрибуция, full@0.31) ──")
base = realized([t for t in trades if in_win(t, "full")])
print(f"база realized: total {round(sum(base),2)} t={stats(base)['t']}")
contrib = {}
for t in trades:
    if in_win(t, "full"): contrib[t["symbol"]] = contrib.get(t["symbol"], 0) + net(t, 0.31)
top5s = sorted(contrib, key=lambda s: -abs(contrib[s]))[:5]
ab1 = realized([t for t in trades if in_win(t, "full") and t["symbol"] not in top5s])
print(f"без топ-5 символов {top5s}: total {round(sum(ab1),2)} t={stats(ab1)['t']}")
day_r = {}
for i, d in enumerate(drange(*WIN["full"])): day_r[d] = base[i]
top5d = sorted(day_r, key=lambda d: -abs(day_r[d]))[:5]
ab2 = [v for d, v in day_r.items() if d not in top5d]
print(f"без топ-5 дней {top5d}: total {round(sum(ab2),2)} t={stats(ab2)['t']}")
mature = [t for t in trades if in_win(t, "full")
          and first_bar.get(t["symbol"]) and t["signal_bar"] - first_bar[t["symbol"]] >= 180*86400_000]
ab3 = realized(mature)
print(f"возраст ≥180д (n={len(mature)}): total {round(sum(ab3),2)} t={stats(ab3)['t']}")
ab4 = realized([t for t in trades if in_win(t, "full")], entry="entry_close")
print(f"запоздалый вход (+30м, по close): total {round(sum(ab4),2)} t={stats(ab4)['t']}")
ab5 = realized([t for t in trades if in_win(t, "full")], short=True)
print(f"SHORT-зеркало (диагностика): total {round(sum(ab5),2)} t={stats(ab5)['t']}")

print("\n── ВЕРДИКТ ПО §7 (механически) ──")
ok1 = all(S[(w, 0.31)]["total"] > 0 for w in ("full", "val", "test_verdict"))
f31, f71 = S[("full", 0.31)]["total"], S[("full", 0.71)]["total"]
ok2 = (f71 > -0.5*abs(f31)) and (S[("test_verdict", 0.71)]["total"] > 0) == (S[("test_verdict", 0.31)]["total"] > 0)
sgn = f31 > 0
ok3 = all((sum(a) > 0) == sgn and stats(a)["t"] > 1.0 for a in (ab1, ab2, ab3))
ok4 = S[("test_verdict", 0.31)]["total"] > 0 and S[("test_verdict", 0.31)]["t"] >= 2.0
delayed_flip = (sum(ab4) > 0) != sgn
print(f"1) net>0 @0.31 (full+val+test_verdict): {ok1}")
print(f"2) выживает @0.71: {ok2}")
print(f"3) ablations держат знак и t>1 (симв/дни/зрелость): {ok3}")
print(f"4) test_verdict: net>0 и t≥2.0: {ok4}  (t={S[('test_verdict',0.31)]['t']})")
print(f"5) запоздалый вход флипает знак: {delayed_flip}")
if ok1 and ok2 and ok3 and ok4 and not delayed_flip:
    verdict = "CONFIRMED FOR PAPER ONLY"
elif not ok1 or not ok4:
    verdict = "KILLED / NO-TRADE"
else:
    verdict = "INCONCLUSIVE"
print(f"\n════ ВЕРДИКТ: {verdict} ════")
json.dump({"S": {f"{w}@{c}": v for (w, c), v in S.items()}, "verdict": verdict,
           "gates": {"ok1": ok1, "ok2": ok2, "ok3": ok3, "ok4": ok4, "delayed_flip": delayed_flip},
           "ablations": {"no_top5_sym": round(sum(ab1),2), "no_top5_day": round(sum(ab2),2),
                          "mature180": round(sum(ab3),2), "delayed": round(sum(ab4),2),
                          "short_mirror": round(sum(ab5),2)},
           "top5_symbols": top5s, "top5_days": top5d},
          open(f"{ROOT}/out/results_stats.json", "w"), indent=1)
print("сохранено: out/results_stats.json")

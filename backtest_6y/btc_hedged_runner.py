#!/usr/bin/env python3
"""BTC-BETA-HEDGED RADAR — тест №2 (PREREG studies/2026-07-10_btc_hedged_PREREG.md,
ЗАМОРОЖЕН до открытия данных). Вход = замороженный out/trades.csv (6702 сделки,
ре-симуляции НЕТ). На сделку: β по формуле Codex (30д 30м-ретёрнов ДО сигнального
бара, ≥480 общих точек, клип [0.2;3.0], нет беты → исключение из primary),
alpha-net(c) = (ret_coin − β·ret_btc) − c·(1+β) − f_coin + β·f_btc.
Учётная модель v2 (terminal per-trade → дата выхода, вес 1/5). Один запуск,
вердикт печатает машина по §5."""
import json, gzip, csv, os, random
from datetime import datetime, timezone, timedelta
from statistics import mean, pstdev

ROOT = os.path.expanduser("~/trading/backtest_6y")
BAR, D30 = 1800_000, 30*86400_000
rnd = random.Random(20260711)

def load_bars(sym):
    p = f"{ROOT}/data/klines/{sym}.json.gz"
    if not os.path.exists(p): return None
    return {int(k): v for k, v in json.load(gzip.open(p, "rt")).items()}
def load_fund(sym):
    p = f"{ROOT}/data/funding/{sym}.json.gz"
    if not os.path.exists(p): return []
    return sorted((int(k), v) for k, v in json.load(gzip.open(p, "rt")).items())

BTC = load_bars("BTCUSDT")
BTC_F = load_fund("BTCUSDT")
btc_close = {t: b[4] for t, b in BTC.items()}

trades = []
for r in csv.DictReader(open(f"{ROOT}/out/trades.csv")):
    if int(r["censored"]): continue
    trades.append({"sym": r["symbol"], "kind": r["kind"], "T": int(r["signal_bar"]),
                   "entry_ts": int(r["entry_ts"]), "exit_ts": int(r["exit_ts"]),
                   "entry": float(r["mkt_entry"]), "exit": float(r["exit_px"]),
                   "f_coin": float(r["funding_pct"]), "date": r["ts_utc"][:10]})
print(f"замороженных сделок на входе: {len(trades)}")

def beta_for(sb, T):
    """β по предрегу §2: окно [T+BAR−30д; T−BAR], пары соседних баров обеих серий."""
    lo, hi = T + BAR - D30, T - BAR
    xs, ys = [], []
    t = lo - (lo % BAR)
    while t <= hi:
        a0, a1 = sb.get(t - BAR), sb.get(t)
        b0, b1 = btc_close.get(t - BAR), btc_close.get(t)
        if a0 and a1 and b0 and b1:
            xs.append(b1/b0 - 1)
            ys.append(a1[4]/a0[4] - 1)
        t += BAR
    if len(xs) < 480: return None, len(xs)
    mx, my = mean(xs), mean(ys)
    var = sum((x - mx)**2 for x in xs)
    if var <= 0: return None, len(xs)
    cov = sum((x - mx)*(y - my) for x, y in zip(xs, ys))
    return cov/var, len(xs)

out, counters = [], {"no_beta": 0, "no_btc_bar": 0, "clip_lo": 0, "clip_hi": 0, "beta_pts_med": []}
by_sym = {}
for t in trades: by_sym.setdefault(t["sym"], []).append(t)
for n, (sym, ts_) in enumerate(sorted(by_sym.items())):
    sb = load_bars(sym)
    ff = load_fund(sym)
    for tr in ts_:
        raw_b, npts = beta_for(sb, tr["T"]) if sb else (None, 0)
        eb, xb = BTC.get(tr["entry_ts"]), BTC.get(tr["exit_ts"] - BAR)
        if eb is None or xb is None:
            counters["no_btc_bar"] += 1; continue
        rec = dict(tr)
        rec["ret_coin"] = (tr["exit"]/tr["entry"] - 1)*100
        rec["ret_btc"] = (xb[4]/eb[1] - 1)*100
        rec["f_btc"] = sum(r for t_, r in BTC_F if tr["entry_ts"] < t_ <= tr["exit_ts"]) * 100
        if raw_b is None:
            counters["no_beta"] += 1
            rec["beta"] = None                    # вне primary; живёт в β=1-диагностике
        else:
            b = min(3.0, max(0.2, raw_b))
            counters["clip_lo"] += raw_b < 0.2; counters["clip_hi"] += raw_b > 3.0
            counters["beta_pts_med"].append(npts)
            rec["beta"] = round(b, 4); rec["beta_raw"] = round(raw_b, 4)
        out.append(rec)
    if (n+1) % 100 == 0: print(f"  …символов {n+1}/{len(by_sym)}", flush=True)

def alpha(rec, c, b=None):
    b = rec["beta"] if b is None else b
    return (rec["ret_coin"] - b*rec["ret_btc"]) - c*(1 + b) - rec["f_coin"] + b*rec["f_btc"]

prim = [r for r in out if r["beta"] is not None]
med = sorted(counters["beta_pts_med"])[len(counters["beta_pts_med"])//2] if counters["beta_pts_med"] else 0
print(f"\nprimary sample: {len(prim)} · исключено no_beta={counters['no_beta']} "
      f"no_btc_bar={counters['no_btc_bar']} · клипы lo/hi={counters['clip_lo']}/{counters['clip_hi']} · медиана точек β={med}")
with open(f"{ROOT}/out/hedged_trades.csv", "w", newline="") as f:
    cols = ["sym","kind","date","T","entry_ts","exit_ts","beta","beta_raw","ret_coin","ret_btc","f_coin","f_btc"]
    w = csv.writer(f); w.writerow(cols)
    for r in out: w.writerow([r.get(k, "") for k in cols])

# ── вердиктная печать (окна/ворота §4–5 = убитый тест; учёт v2) ──
def ud_exit(r): return datetime.fromtimestamp(r["exit_ts"]/1000, timezone.utc).strftime("%Y-%m-%d")
def drange(a, b):
    d = datetime.strptime(a, "%Y-%m-%d")
    while d.strftime("%Y-%m-%d") <= b:
        yield d.strftime("%Y-%m-%d"); d += timedelta(days=1)
WIN = {"full": ("2021-10-17","2026-07-09"), "train": ("2021-10-17","2023-12-31"),
       "val": ("2024-01-01","2024-12-31"), "test_verdict": ("2025-01-01","2026-04-30"),
       "contaminated": ("2026-05-01","2026-07-09"), "2022": ("2022-01-01","2022-12-31"),
       "2023": ("2023-01-01","2023-12-31"), "2025": ("2025-01-01","2025-12-31"),
       "2026H1": ("2026-01-01","2026-07-09")}
def stream(sub, c, b=None):
    dd = {}
    for r in sub: dd[ud_exit(r)] = dd.get(ud_exit(r), 0.0) + alpha(r, c, b)/5
    return dd
def series(dd, win):
    a, b = WIN[win]; return [dd.get(d, 0.0) for d in drange(a, b)]
def stats(xs):
    n = len(xs); m = mean(xs); sd = pstdev(xs)*(n/(n-1))**0.5 if n > 1 else 0
    t = m/(sd/n**0.5) if sd else 0
    cum = mx = dd_ = 0.0
    for x in xs:
        cum += x; mx = max(mx, cum); dd_ = min(dd_, cum - mx)
    return {"total": round(sum(xs), 2), "t": round(t, 2),
            "sharpe": round(m/sd*365**0.5, 2) if sd else 0, "maxDD": round(dd_, 2)}
def boot(xs, k=10000):
    nb = max(1, len(xs)//7)
    blocks = [xs[i*7:(i+1)*7] for i in range(nb)]
    tot = sorted(sum(v for bl in (rnd.choice(blocks) for _ in range(nb)) for v in bl) for _ in range(k))
    return round(tot[int(0.025*k)], 2), round(tot[int(0.975*k)], 2)

print("\n═══════ BTC-BETA-HEDGED RADAR: RESULTS (единственное вскрытие) ═══════")
print("SURVIVOR-ONLY UNIVERSE: UPPER BOUND (наследуется) · учёт v2 · косты c·(1+β)\n")
print(f"{'окно':>13} {'c/ногу':>6} | {'alphaΣ%':>8} {'t':>6} {'Sharpe':>6} {'maxDD':>8}")
S = {}
for c in (0.14, 0.31, 0.71):
    dd = stream(prim, c)
    for w in WIN:
        S[(w, c)] = stats(series(dd, w))
        if c == 0.31 or w in ("full", "val", "test_verdict"):
            s = S[(w, c)]
            print(f"{w:>13} {c:>6} | {s['total']:>8} {s['t']:>6} {s['sharpe']:>6} {s['maxDD']:>8}")
dd31 = stream(prim, 0.31)
for w in ("full", "val", "test_verdict"):
    lo, hi = boot(series(dd31, w))
    print(f"bootstrap {w}@0.31: 95% CI [{lo}, {hi}]")

print("\n── вторичные (НЕ вердикт) ──")
bmed = sorted(r["beta"] for r in prim)[len(prim)//2]
print(f"β: медиана {bmed}, gross-альфа (c=0, funding в нулях НЕ обнулён): "
      f"{round(sum(alpha(r, 0) for r in prim)/len(prim), 4)}%/сделку")
for k in ("single", "awakening"):
    sub = [r for r in prim if r["kind"] == k]
    s = stats(series(stream(sub, 0.31), "full"))
    print(f"  {k}: n={len(sub)} full@0.31 {s['total']} t={s['t']}")
for lab, lo_, hi_ in (("β<0.8", 0, 0.8), ("0.8–1.5", 0.8, 1.5), (">1.5", 1.5, 99)):
    sub = [r for r in prim if lo_ <= r["beta"] < hi_]
    if sub:
        s = stats(series(stream(sub, 0.31), "full"))
        print(f"  β {lab}: n={len(sub)} full@0.31 {s['total']} t={s['t']}")
sub_all = out
s1 = stats(series(stream(sub_all, 0.31, b=1.0), "full"))
print(f"  диагностика β=1 (ВСЕ {len(sub_all)} сделок, вкл. исключённых): full@0.31 {s1['total']} t={s1['t']}")

print("\n── ВЕРДИКТ ПО §5 (механически) ──")
ok1 = all(S[(w, 0.31)]["total"] > 0 for w in ("full", "val", "test_verdict"))
f31, f71 = S[("full", 0.31)]["total"], S[("full", 0.71)]["total"]
ok2 = (f71 > -0.5*abs(f31)) and ((S[("test_verdict", 0.71)]["total"] > 0) == (S[("test_verdict", 0.31)]["total"] > 0))
sgn = f31 > 0
contrib = {}
for r in prim: contrib[r["sym"]] = contrib.get(r["sym"], 0) + alpha(r, 0.31)
top5s = sorted(contrib, key=lambda s_: -abs(contrib[s_]))[:5]
ab1 = series(stream([r for r in prim if r["sym"] not in top5s], 0.31), "full")
day31 = series(dd31, "full")
dmap = dict(zip(drange(*WIN["full"]), day31))
top5d = sorted(dmap, key=lambda d: -abs(dmap[d]))[:5]
ab2 = [v for d, v in dmap.items() if d not in top5d]
summ = json.load(open(f"{ROOT}/data/manifest/_summary.json"))
fb = {s_: c_["first"] for s_, c_ in summ["coverage"].items()}
ab3 = series(stream([r for r in prim if fb.get(r["sym"]) and r["T"] - fb[r["sym"]] >= 180*86400_000], 0.31), "full")
ok3 = all((sum(a) > 0) == sgn and abs(stats(a)["t"]) > 1.0 for a in (ab1, ab2, ab3))
ok4 = S[("test_verdict", 0.31)]["total"] > 0 and S[("test_verdict", 0.31)]["t"] >= 2.0
print(f"1) alpha>0 @0.31 (full+val+test_verdict): {ok1}")
print(f"2) выживает @0.71: {ok2}")
print(f"3) ablations (топ-5 симв {round(sum(ab1),1)} / топ-5 дней {round(sum(ab2),1)} / ≥180д {round(sum(ab3),1)}) держат знак и |t|>1: {ok3}")
print(f"4) test_verdict: alpha>0 и t≥2.0: {ok4} (t={S[('test_verdict',0.31)]['t']})")
verdict = ("CONFIRMED FOR PAPER ONLY" if (ok1 and ok2 and ok3 and ok4)
           else "KILLED / NO-TRADE" if (not ok1 or not ok4) else "INCONCLUSIVE")
print(f"\n════ ВЕРДИКТ: {verdict} ════")
json.dump({"verdict": verdict, "gates": {"ok1": ok1, "ok2": ok2, "ok3": ok3, "ok4": ok4},
           "primary_n": len(prim), "excluded_no_beta": counters["no_beta"],
           "S": {f"{w}@{c}": v for (w, c), v in S.items()}},
          open(f"{ROOT}/out/hedged_results.json", "w"), indent=1)
print("сохранено: out/hedged_trades.csv, out/hedged_results.json")

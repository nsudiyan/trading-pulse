#!/usr/bin/env python3
"""H-OI-01: crowded leverage unwind (PREREG studies/2026-07-11_H-OI-01_PREREG.md,
ЗАМОРОЖЕН с вето Codex). Раз в сутки 00:00 UTC по PIT-вселенной:
t1/t0-правила OI буквально по timestamp; f_norm = rate×24ч/фактический интервал
двух последних settlement; score = f_norm × max(0, ΔOI_4h%); K_D = min(10,#pos,#neg),
торгуем при K_D≥5; шорт top-K положительных, лонг top-K отрицательных; вес 1/K_D
на сторону; hold 24ч; косты и funding обеих ног. Учёт v2. Дни независимы (стейта нет).
python3 h_oi_runner.py --selfcheck | python3 h_oi_runner.py"""
import json, gzip, csv, os, random, argparse
from datetime import datetime, timezone, timedelta
from statistics import mean, pstdev
from bisect import bisect_right, bisect_left

ROOT = os.path.expanduser("~/trading/backtest_6y")
BAR, H, DAY = 1800_000, 3600_000, 86400_000
COSTS = (0.14, 0.31, 0.71)
rnd = random.Random(20260711)
def ms(s): return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()*1000)
def ud(t): return datetime.fromtimestamp(t/1000, timezone.utc).strftime("%Y-%m-%d")

# ── чистое ядро (тестируемо): один день ──────────────────────────────────────
def day_positions(D, uni_day, oi_ts, oi_val, fund, counters):
    """Кандидаты дня по замороженным правилам. oi_ts: {sym: sorted [ts]},
    oi_val: {sym: {ts: oi}}, fund: {sym: sorted [(ts, rate)]}. → [(sym, score)]"""
    cands = []
    for sym in uni_day:                                   # порядок вселенной = тай-брейк
        ts = oi_ts.get(sym)
        if not ts: counters["excl_no_oi"] += 1; continue
        i1 = bisect_left(ts, D) - 1                       # as-of bugfix 11.07: строго < D
        if i1 < 0 or D - ts[i1] > 5*H: counters["excl_oi_age"] += 1; continue
        t1 = ts[i1]
        i0 = bisect_right(ts, t1 - 4*H) - 1
        if i0 < 0 or not (3*H <= t1 - ts[i0] <= 5*H): counters["excl_oi_gap"] += 1; continue
        t0 = ts[i0]
        v0, v1 = oi_val[sym][t0], oi_val[sym][t1]
        if v0 <= 0: counters["excl_oi_zero"] += 1; continue
        doi = v1/v0 - 1
        fs = fund.get(sym, [])
        j = bisect_left(fs, (D, float("-inf"))) - 1       # as-of bugfix: оба settlement < D
        if j < 1: counters["excl_no_fund"] += 1; continue
        (tp, _), (tl, rl) = fs[j-1], fs[j]
        if tl <= tp: counters["excl_no_fund"] += 1; continue
        f_norm = rl * DAY / (tl - tp)                     # суточный эквивалент, факт. интервал
        cands.append((sym, f_norm * max(0.0, doi)))
    pos = sorted([c for c in cands if c[1] > 0], key=lambda x: -x[1])
    neg = sorted([c for c in cands if c[1] < 0], key=lambda x: x[1])
    K = min(10, len(pos), len(neg))
    if K < 5: counters["skipped_K"] += 1; return None
    return {"K": K, "short": pos[:K], "long": neg[:K]}

def price_leg(sb, D, t_end):
    """(entry_open, exit_close, exit_ms). as-of bugfix 11.07: вход по open бара
    D+30м (00:30) — решение из данных <D не может исполняться по цене момента D."""
    eb = sb.get(D + BAR)
    if eb is None: return None
    t = D + BAR + DAY
    while t <= min(D + BAR + 3*DAY, t_end):
        b = sb.get(t)
        if b: return eb[1], b[4], t + BAR
        t += BAR
    return None

# ── статистика/вердикт (тот же §7-паттерн) ──────────────────────────────────
def drange(a, b):
    d = datetime.strptime(a, "%Y-%m-%d")
    while d.strftime("%Y-%m-%d") <= b:
        yield d.strftime("%Y-%m-%d"); d += timedelta(days=1)
def stats(xs):
    n = len(xs); m = mean(xs); sd = pstdev(xs)*(n/(n-1))**0.5 if n > 1 else 0
    t = m/(sd/n**0.5) if sd else 0
    cum = mx = dd = 0.0
    for x in xs:
        cum += x; mx = max(mx, cum); dd = min(dd, cum - mx)
    return {"total": round(sum(xs), 2), "t": round(t, 2),
            "sharpe": round(m/sd*365**0.5, 2) if sd else 0, "maxDD": round(dd, 2)}
def boot(xs, k=10000):
    nb = max(1, len(xs)//7); blocks = [xs[i*7:(i+1)*7] for i in range(nb)]
    tot = sorted(sum(v for bl in (rnd.choice(blocks) for _ in range(nb)) for v in bl) for _ in range(k))
    return round(tot[int(.025*k)], 2), round(tot[int(.975*k)], 2)

def main():
    uni_all = json.load(gzip.open(f"{ROOT}/data/universe.json.gz", "rt"))
    END = ms("2026-07-10")
    chunks = ["2021-01-01","2021-10-01","2022-04-01","2022-10-01","2023-04-01","2023-10-01",
              "2024-04-01","2024-10-01","2025-04-01","2025-10-01","2026-04-01","2026-07-09"]
    counters = {k: 0 for k in ("excl_no_oi","excl_oi_age","excl_oi_gap","excl_oi_zero",
                               "excl_no_fund","skipped_K","pos_no_bar")}
    daily = {c: {} for c in COSTS}; rows = []; Ks = []
    for i in range(len(chunks)-1):
        a, b = ms(chunks[i]), ms(chunks[i+1])
        days = [d for d in uni_all if chunks[i] <= d < chunks[i+1]]
        if not days: continue
        need = sorted({s for d in days for s in uni_all[d]})
        bars, oi_ts, oi_val, fund = {}, {}, {}, {}
        for s in need:
            po = f"{ROOT}/data/oi/{s}.json.gz"
            if os.path.exists(po):
                ov = {int(k): v for k, v in json.load(gzip.open(po, "rt")).items()
                      if a - DAY <= int(k) <= b}
                if ov: oi_val[s] = ov; oi_ts[s] = sorted(ov)
            pk = f"{ROOT}/data/klines/{s}.json.gz"
            if os.path.exists(pk):
                bb = {int(k): v for k, v in json.load(gzip.open(pk, "rt")).items()
                      if a - DAY <= int(k) <= b + 4*DAY}
                if bb: bars[s] = bb
            pf = f"{ROOT}/data/funding/{s}.json.gz"
            if os.path.exists(pf):
                fund[s] = sorted((int(k), v) for k, v in json.load(gzip.open(pf, "rt")).items())
        for dstr in sorted(days):
            D = ms(dstr)
            if D > END - 25*H: continue                    # хвостовая цензура
            sel = day_positions(D, uni_all[dstr], oi_ts, oi_val, fund, counters)
            if sel is None: continue
            K = sel["K"]; Ks.append(K)
            for side, sgn in (("long", 1), ("short", -1)):
                for sym, score in sel[side]:
                    sb = bars.get(sym)
                    leg = price_leg(sb, D, b + 3*DAY) if sb else None
                    if leg is None: counters["pos_no_bar"] += 1; continue
                    e, x, xms = leg
                    ret = (x/e - 1)*100
                    f = sum(r for t_, r in fund.get(sym, ()) if D + BAR < t_ <= xms) * 100
                    rows.append({"date": dstr, "sym": sym, "side": side, "K": K,
                                 "ret": round(ret, 4), "f": round(f, 4), "score": round(score, 6)})
                    for c in COSTS:
                        net = (ret - c - f) if sgn == 1 else (-ret - c + f)
                        dd_ = daily[c]; dd_[dstr] = dd_.get(dstr, 0.0) + net/K
        print(f"кусок {chunks[i]}…{chunks[i+1]}: дней с торгами накоплено "
              f"{len(daily[0.31])}, позиций {len(rows)}", flush=True)

    tradable = sorted(daily[0.31])
    if not tradable: print("НЕТ торгуемых дней"); return
    start = max("2021-10-17", tradable[0])
    WIN = {"full": (start, "2026-07-09"), "train": (start, "2023-12-31"),
           "val": ("2024-01-01","2024-12-31"), "test_verdict": ("2025-01-01","2026-04-30"),
           "contaminated": ("2026-05-01","2026-07-09"), "2022": ("2022-01-01","2022-12-31"),
           "2023": ("2023-01-01","2023-12-31"), "2025": ("2025-01-01","2025-12-31"),
           "2026H1": ("2026-01-01","2026-07-09")}
    def series(c, w): a_, b_ = WIN[w]; return [daily[c].get(d, 0.0) for d in drange(a_, b_)]

    with open(f"{ROOT}/out/hoi_positions.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(f"\n═══════ H-OI-01: RESULTS (единственное вскрытие) ═══════")
    print(f"SURVIVOR-ONLY POOL: направление смещения для L/S заявлено неоднозначным (PREREG §4)")
    print(f"старт торгуемого окна: {start} (правило §3) · дней с торгами {len(tradable)} · "
          f"медиана K_D {sorted(Ks)[len(Ks)//2]} · счётчики { {k: v for k, v in counters.items() if v} }\n")
    print(f"{'окно':>13} {'c/ногу':>6} | {'netΣ%':>8} {'t':>6} {'Sharpe':>6} {'maxDD':>8}")
    S = {}
    for c in COSTS:
        for w in WIN:
            S[(w, c)] = stats(series(c, w))
            if c == 0.31 or w in ("full", "val", "test_verdict"):
                s = S[(w, c)]
                print(f"{w:>13} {c:>6} | {s['total']:>8} {s['t']:>6} {s['sharpe']:>6} {s['maxDD']:>8}")
    for w in ("full", "val", "test_verdict"):
        lo, hi = boot(series(0.31, w))
        print(f"bootstrap {w}@0.31: 95% CI [{lo}, {hi}]")

    print("\n── вторичные (НЕ вердикт) ──")
    gross = [ (r["ret"] - r["f"]) if r["side"] == "long" else (-r["ret"] + r["f"]) for r in rows]
    print(f"gross/позицию (c=0, funding учтён): {round(mean(gross), 4)}% · позиций {len(rows)}")
    for side in ("long", "short"):
        sub = [ (r["ret"] - r["f"]) if side == "long" else (-r["ret"] + r["f"])
                for r in rows if r["side"] == side]
        print(f"  нога {side}: gross {round(mean(sub), 4)}%/позицию (n={len(sub)})")

    print("\n── ВЕРДИКТ ПО §3 (механически) ──")
    ok1 = all(S[(w, 0.31)]["total"] > 0 for w in ("full", "val", "test_verdict"))
    f31, f71 = S[("full", 0.31)]["total"], S[("full", 0.71)]["total"]
    ok2 = (f71 > -0.5*abs(f31)) and ((S[("test_verdict", 0.71)]["total"] > 0) == (S[("test_verdict", 0.31)]["total"] > 0))
    sgn = f31 > 0
    contrib = {}
    for r in rows:
        net = (r["ret"] - 0.31 - r["f"]) if r["side"] == "long" else (-r["ret"] - 0.31 + r["f"])
        contrib[r["sym"]] = contrib.get(r["sym"], 0) + net/r["K"]
    top5s = set(sorted(contrib, key=lambda s_: -abs(contrib[s_]))[:5])
    dd_ab = {}
    summ = json.load(open(f"{ROOT}/data/manifest/_summary.json"))
    fb = {s_: c_["first"] for s_, c_ in summ["coverage"].items()}
    for tag, keep in (("ab1", lambda r: r["sym"] not in top5s),
                      ("ab3", lambda r: fb.get(r["sym"]) and ms(r["date"]) - fb[r["sym"]] >= 180*DAY)):
        dd_ = {}
        for r in rows:
            if not keep(r): continue
            net = (r["ret"] - 0.31 - r["f"]) if r["side"] == "long" else (-r["ret"] - 0.31 + r["f"])
            dd_[r["date"]] = dd_.get(r["date"], 0.0) + net/r["K"]
        dd_ab[tag] = [dd_.get(d, 0.0) for d in drange(*WIN["full"])]
    dmap = dict(zip(drange(*WIN["full"]), series(0.31, "full")))
    top5d = set(sorted(dmap, key=lambda d: -abs(dmap[d]))[:5])
    dd_ab["ab2"] = [v for d, v in dmap.items() if d not in top5d]
    ok3 = all((sum(a) > 0) == sgn and abs(stats(a)["t"]) > 1.0 for a in dd_ab.values())
    ok4 = S[("test_verdict", 0.31)]["total"] > 0 and S[("test_verdict", 0.31)]["t"] >= 2.0
    print(f"1) net>0 @0.31 (full+val+test_verdict): {ok1}")
    print(f"2) выживает @0.71: {ok2}")
    print(f"3) ablations (симв {round(sum(dd_ab['ab1']),1)} / дни {round(sum(dd_ab['ab2']),1)} / ≥180д {round(sum(dd_ab['ab3']),1)}): {ok3}")
    print(f"4) test_verdict: net>0 и t≥2.0: {ok4} (t={S[('test_verdict',0.31)]['t']})")
    verdict = ("CONFIRMED FOR PAPER ONLY" if (ok1 and ok2 and ok3 and ok4)
               else "KILLED / NO-TRADE" if (not ok1 or not ok4) else "INCONCLUSIVE")
    print(f"\n════ ВЕРДИКТ: {verdict} ════")
    json.dump({"verdict": verdict, "gates": [ok1, ok2, ok3, ok4], "start": start,
               "days": len(tradable), "positions": len(rows), "counters": counters,
               "S": {f"{w}@{c}": v for (w, c), v in S.items()}},
              open(f"{ROOT}/out/hoi_results.json", "w"), indent=1)
    print("сохранено: out/hoi_positions.csv, out/hoi_results.json")

def selfcheck():
    D = ms("2024-06-01")
    mkoi = lambda pts: ({k: v for k, v in pts}, sorted(k for k, _ in pts))
    oiv, oit = {}, {}
    # A: валидный crowded-long (f+, OI +10%); B: crowded-short (f−, OI +10%);
    # C: OI-точка старая (>5ч) → excl; E: разгрузка (OI ↓, f−) → score 0
    for sym, (o0, o1, t1off) in {"A": (100, 110, -H), "B": (200, 220, -H),
                                 "C": (50, 55, -6*H), "E": (300, 270, -H)}.items():
        pts = [(D - 5*H + 0, o0), (D + t1off, o1)] if sym != "C" else [(D - 10*H, o0), (D - 6*H, o1)]
        oiv[sym], oit[sym] = mkoi(pts)
    fund = {s: [(D - 12*H, r), (D - 4*H, r)] for s, r in
            (("A", .0004), ("B", -.0004), ("C", .0004), ("E", -.0004))}
    c = {k: 0 for k in ("excl_no_oi","excl_oi_age","excl_oi_gap","excl_oi_zero","excl_no_fund","skipped_K")}
    sel = day_positions(D, ["A","B","C","E"], oit, oiv, fund, c)
    assert sel is None and c["skipped_K"] == 1 and c["excl_oi_age"] == 1, (sel, c)  # K=1 < 5 → день скипнут
    # 10 лонг-толп + 10 шорт-толп → K=10, состав верный
    oiv2, oit2, fund2, uni = {}, {}, {}, []
    for i in range(12):
        s = f"L{i}"; uni.append(s)
        oiv2[s], oit2[s] = mkoi([(D - 5*H, 100), (D - H, 100 + i + 1)])
        fund2[s] = [(D - 12*H, .0001*(i+1)), (D - 4*H, .0001*(i+1))]
    for i in range(12):
        s = f"S{i}"; uni.append(s)
        oiv2[s], oit2[s] = mkoi([(D - 5*H, 100), (D - H, 100 + i + 1)])
        fund2[s] = [(D - 12*H, -.0001*(i+1)), (D - 4*H, -.0001*(i+1))]
    c2 = {k: 0 for k in c}
    sel2 = day_positions(D, uni, oit2, oiv2, fund2, c2)
    assert sel2["K"] == 10 and sel2["short"][0][0] == "L11" and sel2["long"][0][0] == "S11", sel2
    # f_norm: интервал 8ч → rate×3 (суточный эквивалент)
    _, sc = sel2["short"][0]
    exp = (.0001*12)*(DAY/(8*H)) * ((100+12)/100 - 1)
    assert abs(sc - exp) < 1e-12, (sc, exp)
    # нога/арифметика: вход 00:30 (as-of), выход +24ч
    sb = {D + BAR: [D+BAR, 100, 101, 99, 100, 1],
          D + BAR + DAY: [D+BAR+DAY, 98, 99, 97, 98, 1]}
    e, x, xms = price_leg(sb, D, D + 5*DAY)
    assert (e, x) == (100, 98) and xms == D + BAR + DAY + BAR
    # ═ AS-OF РЕГРЕССИЯ (bugfix 11.07): данные с меткой ровно D НЕВИДИМЫ решению ═
    # всем 24 символам подкладываем «отравленные» точки в ts=D: OI-обвал до 1.0
    # (видимость → ΔOI<0 → relu 0 → день бы скипнулся) и funding ×100
    # (видимость → другой f_norm → другой score). Отбор обязан НЕ измениться.
    for s in list(oiv2):
        oiv2[s] = dict(oiv2[s]); oiv2[s][D] = 1.0
        oit2[s] = oit2[s] + [D]
        fund2[s] = fund2[s] + [(D, fund2[s][-1][1]*100)]
    c3 = {k: 0 for k in c2}
    sel3 = day_positions(D, uni, oit2, oiv2, fund2, c3)
    assert sel3 is not None and sel3["K"] == 10, (sel3, c3)
    assert sel3["short"][0][0] == "L11" and abs(sel3["short"][0][1] - exp) < 1e-12, sel3["short"][0]
    net_short = -(x/e - 1)*100 - 0.31 + 0.05
    assert abs(net_short - (2 - 0.31 + 0.05)) < 1e-9
    print("✓ H-OI selfcheck: исключения/K-гейт/состав корзин/f_norm-кадентность/нога — OK")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--selfcheck", action="store_true")
    if ap.parse_args().selfcheck: selfcheck()
    else: main()

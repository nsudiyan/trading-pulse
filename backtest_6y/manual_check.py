#!/usr/bin/env python3
"""НЕЗАВИСИМАЯ СВЕРКА smoke (усилена по ревью Codex №4).

ЧАСТЬ 1 — независимый конвейер: этот файл НЕ импортирует core_replay/vol_radar-логику.
Все eligible-кандидаты собираются заново из сырых баров, сортируются глобально по
времени, и доставка (awakening-кап → каскад/одиночки → кулдауны → дневные капы →
конкаренси 5) применяется ЗАНОВО по тексту PREREG. Дифф позиций с движком обязан
быть ПУСТЫМ — иначе баг в одном из двух. Стиль намеренно другой: пре-скан кандидатов
per-symbol + глобальный хронологический fold (у движка — per-scan цикл по вселенной).
Единственный импорт из боевого файла — константные МНОЖЕСТВА (MAJOR_SYMBOLS: это
версионированные ДАННЫЕ предрега, не логика; переписывание руками = риск опечатки).

ЧАСТЬ 2 — стратифицированная выборка (fixed seed + обязательные страты: single,
awakening, граница кулдауна, забитый дневной кап, vr у порога, гард у порога,
≥4 разных месяца) с построчным пересчётом каждой сделки из сырых баров."""
import json, gzip, os, random
from datetime import datetime, timezone

ROOT = os.path.expanduser("~/trading/backtest_6y")
BAR, DAY = 1800_000, 86400_000
import sys; sys.path.insert(0, os.path.expanduser("~/trading"))
from vol_radar import MAJOR_SYMBOLS          # только данные-константа (28 тикеров)

def ms(s): return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()*1000)
def ud(t): return datetime.fromtimestamp(t/1000, timezone.utc).strftime("%Y-%m-%d")
T0, T1 = ms("2023-07-01"), ms("2024-01-01")
LO, HI = ms("2023-06-20"), ms("2024-01-05")

man = json.load(gzip.open(f"{ROOT}/data/universe_manifest.json.gz", "rt"))
uni = {d: [s for s, _ in m["top"]] for d, m in man.items() if "2023-06-25" <= d <= "2024-01-02"}
need = sorted({s for lst in uni.values() for s in lst})
bars = {}
for s in need:
    p = f"{ROOT}/data/klines/{s}.json.gz"
    if not os.path.exists(p): continue
    b = {int(k): v for k, v in json.load(gzip.open(p, "rt")).items() if LO <= int(k) <= HI}
    if b: bars[s] = b
print(f"[независимый конвейер] символов: {len(bars)}")

# ── пре-скан: ВСЕ сырые кандидаты (без кулдаунов — они применяются в fold) ──
cands = {}                                   # T -> [(sym, vr, chg, live)]
for sym, b in bars.items():
    keys = sorted(b)
    for T in keys:
        if not (T0 - BAR <= T < T1): continue
        win = [b.get(T - i*BAR) for i in range(10, 0, -1)]
        prev, nxt = b.get(T - BAR), b.get(T + BAR)
        if any(w is None for w in win) or prev is None or nxt is None: continue
        avg = sum(w[5] for w in win) / 10
        if avg <= 0: continue
        vr = b[T][5] / avg
        if vr < 5.0: continue
        chg = (b[T][4] - prev[4]) / prev[4] * 100
        live = (nxt[1] - b[T][4]) / b[T][4] * 100
        if abs(chg) > 1.0 or abs(live) > 1.0: continue
        cands.setdefault(T, []).append((sym, round(vr, 2), chg, live))
print(f"[независимый конвейер] сырых кандидатов: {sum(map(len, cands.values()))} на {len(cands)} сканах")

# ── глобальный хронологический fold: доставка по тексту PREREG ──
cd, day, singles, awaken, cascade_ts = {}, None, 0, 0, 0
positions = set(); open_exits = []; annot = {}
skipped_full = ind_cascades = 0
for T in sorted(cands):
    scan_now = T + BAR
    if ud(scan_now) != day:
        day, singles, awaken = ud(scan_now), 0, 0
    uni_day = uni.get(ud(scan_now), [])
    cmap = {s: vr for s, vr, _, _ in cands[T]}
    # порядок вселенной (turnover-ранг) + stable sort по vr: тай-брейк как в бою
    hits = [(s, cmap[s]) for s in uni_day if s in cmap and cd.get(s, 0) <= scan_now]
    if not hits: continue
    hits.sort(key=lambda x: -x[1])
    # топ-1 одиночка выбирается из ВСЕХ хитов ДО пробуждений (боевой порядок:
    # select_hits раньше awakening-блока); если топ-1 ушёл пробуждением — замены НЕТ
    alts_all = [h for h in hits if h[0] not in MAJOR_SYMBOLS]
    top1 = alts_all[0] if alts_all else None
    sent, delivered = set(), []
    for s, vr in [h for h in hits if h[1] >= 15.0 and h[0] not in MAJOR_SYMBOLS]:
        if awaken >= 3: break
        awaken += 1; sent.add(s); delivered.append((s, "awakening", vr))
    rest = [h for h in hits if h[0] not in sent]
    if len(hits) >= 3:                        # каскад считается по ВСЕМ хитам скана
        if scan_now - cascade_ts >= 2*3600_000:
            cascade_ts = scan_now; sent |= {s for s, _ in rest}; ind_cascades += 1
    else:
        if top1 and top1[0] not in sent and singles < 5:
            s, vr = top1; singles += 1; sent.add(s); delivered.append((s, "single", vr))
    for s, _ in hits:
        cd[s] = scan_now + (4*3600_000 if s in sent else 30*60_000)
    for s, kind, vr in delivered:             # позиционный слой: конкаренси 5, цензор-гард
        if scan_now > T1 - 25*3600_000: continue
        open_exits = [e for e in open_exits if e > scan_now]
        if len(open_exits) >= 5: skipped_full += 1; continue
        b = bars[s]; tgt = (T + BAR) + DAY; t_ = tgt
        while t_ not in b and t_ < tgt + 2*DAY: t_ += BAR
        if t_ not in b: continue
        open_exits.append(t_ + BAR)
        positions.add((s, T, kind))
        annot[(s, T)] = {"vr": vr, "singles_at": singles, "awaken_at": awaken,
                         "rank": uni[ud(scan_now)].index(s) + 1}

eng = json.load(open(f"{ROOT}/smoke_trades.json"))
eng_set = {(t["symbol"], t["signal_bar"], t["kind"]) for t in eng if not t["censored"]}
ind_only, eng_only = positions - eng_set, eng_set - positions
print(f"\n═══ ЧАСТЬ 1: ДИФФ ПОЗИЦИЙ движок ↔ независимый конвейер ═══")
print(f"движок: {len(eng_set)} · независимый: {len(positions)} (skipped_full={skipped_full}, каскадов={ind_cascades})")
print(f"только у независимого: {len(ind_only)} · только у движка: {len(eng_only)}")
for tag, dif in (("НЕЗАВИСИМЫЙ", sorted(ind_only)), ("ДВИЖОК", sorted(eng_only))):
    for s, T, k in dif[:10]:
        print(f"  только {tag}: {s} {datetime.fromtimestamp((T+BAR)/1000, timezone.utc):%m-%d %H:%M} {k}")
print("ВЕРДИКТ ЧАСТИ 1:", "✅ ДИФФ ПУСТ — конвейер движка воспроизведён независимо"
      if not ind_only and not eng_only else "❌ РАЗОБРАТЬ ДО ПОЛНОГО ПРОГОНА")

# ── ЧАСТЬ 2: стратифицированная выборка + построчный пересчёт ──
def strata(trades):
    random.seed(20260710)
    pick = {id(t): t for t in random.sample(trades, min(12, len(trades)))}
    by_day = {}
    for t in trades: by_day.setdefault(ud(t["signal_bar"]+BAR), []).append(t)
    def add(f, label):
        c = [t for t in trades if f(t)]
        if c: t = c[0]; t.setdefault("_why", []).append(label); pick[id(t)] = t
    add(lambda t: t["kind"] == "awakening", "страта: awakening")
    add(lambda t: t["kind"] == "single", "страта: single")
    add(lambda t: 5.0 <= t["vol_ratio"] <= 5.5, "страта: vr у порога 5")
    add(lambda t: len([x for x in by_day[ud(t["signal_bar"]+BAR)] if x["kind"] == "single"]) >= 5
                  and t["kind"] == "single", "страта: день с забитым капом 5")
    reps = {}
    for t in sorted(trades, key=lambda x: x["signal_bar"]):
        k = t["symbol"]; p = reps.get(k)
        if p is not None and 4*3600_000 <= t["signal_bar"] - p <= 6*3600_000:
            t.setdefault("_why", []).append("страта: повтор символа сразу после 4ч-кулдауна"); pick[id(t)] = t; break
        reps[k] = t["signal_bar"]
    for t in trades:                          # гард у порога — по сырым барам
        b = bars.get(t["symbol"], {}); T = t["signal_bar"]
        prev, sig, nxt = b.get(T-BAR), b.get(T), b.get(T+BAR)
        if not (prev and sig and nxt): continue
        chg = abs((sig[4]-prev[4])/prev[4]*100); live = abs((nxt[1]-sig[4])/sig[4]*100)
        if chg >= 0.8 or live >= 0.8:
            t.setdefault("_why", []).append(f"страта: гард у порога (Δ={chg:.2f}%, live={live:.2f}%)")
            pick[id(t)] = t; break
    months = {ud(t["signal_bar"])[:7] for t in pick.values()}
    for m in sorted({ud(t["signal_bar"])[:7] for t in trades} - months):
        c = [t for t in trades if ud(t["signal_bar"])[:7] == m]
        if c: c[0].setdefault("_why", []).append(f"страта: месяц {m}"); pick[id(c[0])] = c[0]
    return sorted(pick.values(), key=lambda t: t["signal_bar"])

sample = strata([t for t in eng if not t["censored"]])
print(f"\n═══ ЧАСТЬ 2: ПОСТРОЧНАЯ СВЕРКА ({len(sample)} сделок, страты + seed) ═══")
fails = 0
for tr in sample:
    sym, T = tr["symbol"], tr["signal_bar"]
    b = bars.get(sym, {})
    probs = []
    win = [b.get(T - i*BAR) for i in range(10, 0, -1)]
    prev, sig, nxt = b.get(T-BAR), b.get(T), b.get(T+BAR)
    if any(w is None for w in win) or not (prev and sig and nxt):
        probs.append("дыра в сырых данных")
    else:
        vr = sig[5] / (sum(w[5] for w in win)/10)
        if abs(vr - tr["vol_ratio"]) > 0.05: probs.append(f"vr {vr:.2f}≠{tr['vol_ratio']}")
        if abs((sig[4]-prev[4])/prev[4]*100) > 1.0: probs.append("гард Δцены нарушен")
        if abs((nxt[1]-sig[4])/sig[4]*100) > 1.0: probs.append("live-гард нарушен")
        if vr < 5.0: probs.append("vr<5")
        if tr["kind"] == "awakening" and vr < 15.0: probs.append("awakening при vr<15")
        if tr["kind"] == "single" and sym in MAJOR_SYMBOLS: probs.append("мажор одиночкой")
        if tr["mkt_entry"] != nxt[1]: probs.append("вход ≠ open следующего")
        tgt = (T+BAR) + DAY; t_ = tgt
        while t_ not in b and t_ < tgt + 2*DAY: t_ += BAR
        if t_ in b and (tr["exit_ts"] != t_+BAR or abs(tr["exit_px"]-b[t_][4]) > 1e-9):
            probs.append("выход не совпал")
    if (sym, T, tr["kind"]) not in positions: probs.append("НЕТ у независимого конвейера")
    a = annot.get((sym, T), {})
    if a.get("singles_at", 0) > 5 or a.get("awaken_at", 0) > 3: probs.append("кап превышен")
    fails += bool(probs)
    why = "; ".join(tr.get("_why", [])) or "seed-случайная"
    print(f"{tr['ts_utc']} {sym:>14} {tr['kind']:>9} vr={tr['vol_ratio']:>5.1f} ранг={a.get('rank','-'):>3} "
          f"[{why}] → {'✅ OK' if not probs else '❌ ' + '; '.join(probs)}")
print(f"\nИТОГ: {len(sample)-fails}/{len(sample)} OK · дифф конвейеров: "
      f"{'ПУСТ ✅' if not ind_only and not eng_only else f'{len(ind_only)}+{len(eng_only)} ❌'}")

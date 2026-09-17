#!/usr/bin/env python3
"""PARITY (PREREG §6): ядро vs живые radar_hits.csv. ТОЛЬКО состав (символ-бар-тип-sent),
НИКАКИХ доходностей (окно внутри test-периода). Окно сравнения 2026-07-07…10 00:00 UTC —
период, где боевой конфиг совпадает с замороженным (порог 5 с 02.07, пробуждения 05.07,
live-guard и 30м-кулдаун 06.07). Прогрев движка с 04.07 (кулдауны/бюджет), прогрев не сравнивается."""
import json, gzip, csv, os
from datetime import datetime, timezone
from core_replay import run_engine, BAR, uday

ROOT = os.path.expanduser("~/trading/backtest_6y")
KDIR = f"{ROOT}/data/klines_snapA"
def ms(s): return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()*1000)
WARM, CMP0, CMP1 = ms("2026-07-04"), ms("2026-07-07"), ms("2026-07-10")

uni = json.load(gzip.open(f"{ROOT}/data/universe_parity.json.gz", "rt"))
need = sorted({s for d, lst in uni.items() if d >= "2026-07-03" for s in lst})
bars = {}
for s in need:
    p = f"{KDIR}/{s}.json.gz"
    if not os.path.exists(p): continue
    bars[s] = {int(k): v for k, v in json.load(gzip.open(p, "rt")).items()
               if ms("2026-07-01") <= int(k) <= CMP1 + 3*86400_000}
print(f"символов загружено: {len(bars)}")

hit_log = []
_, counters = run_engine(bars, {}, uni, WARM, CMP1, hit_log=hit_log)

mine = {}   # (sym, bar_start) -> {vr, sent}
for rec in hit_log:
    if rec["T"] < CMP0: continue
    for sym, vr in rec["hits"]:
        mine[(sym, rec["T"])] = {"vr": vr, "sent": sym in rec["sent"]}

live, live_ts = {}, []
for row in csv.DictReader(open(os.path.expanduser("~/trading/outcomes/radar_hits.csv"))):
    t = datetime.strptime(row["ts_utc"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    tms = int(t.timestamp()*1000)
    live_ts.append(tms)
    if not (CMP0 <= tms < CMP1): continue
    bar = (tms//BAR - 1)*BAR                      # последний ЗАКРЫТЫЙ бар на момент лога
    key = (row["symbol"], bar)
    prev = live.get(key)
    rec = {"vr": float(row["vol_ratio"]), "sent": bool(int(row["sent"])),
           "lag_min": round((tms - (bar + BAR))/60000, 1)}
    if not prev or rec["sent"] and not prev["sent"]: live[key] = rec

common = sorted(set(mine) & set(live))
only_live = sorted(set(live) - set(mine))
only_mine = sorted(set(mine) - set(live))
def alive_near(ts_bar):                           # жив ли был демон около этого скана
    lo, hi = ts_bar + BAR - 90*60000, ts_bar + BAR + 90*60000
    return any(lo <= t <= hi for t in live_ts)

print(f"\n═══ PARITY 2026-07-07…10 (состав, без PnL) ═══")
print(f"общих (символ,бар): {len(common)} · только live: {len(only_live)} · только ядро: {len(only_mine)}")
dv = [abs(mine[k]['vr'] - live[k]['vr']) for k in common]
if common:
    print(f"Δvr на общих: медиана {sorted(dv)[len(dv)//2]:.2f}, макс {max(dv):.2f}")
    agree = sum(1 for k in common if mine[k]["sent"] == live[k]["sent"])
    print(f"sent-совпадение: {agree}/{len(common)}")
    for k in common:
        m, l = mine[k], live[k]
        flag = "" if m["sent"] == l["sent"] else "  ← SENT-РАСХОЖДЕНИЕ"
        print(f"  {k[0]:>14} {datetime.fromtimestamp(k[1]/1000, timezone.utc):%d %H:%M} "
              f"vr {l['vr']:>5.1f}/{m['vr']:<5.1f} sent L{int(l['sent'])}/M{int(m['sent'])} lag {l['lag_min']:>5.1f}м{flag}")
print("\n── только LIVE (ядро не увидело) ──")
for k in only_live:
    l = live[k]
    why = []
    if l["lag_min"] > 6: why.append(f"рескан внутри бара (+{l['lag_min']:.0f}м — одиночный скан ядра это не ловит)")
    if k[0] not in uni.get(uday(k[1] + BAR), []): why.append("вне моей дневной топ-150 (live освежает вселенную каждый скан)")
    if k[0] not in bars: why.append("нет файла данных")
    elif k[1] not in bars[k[0]]: why.append("дыра данных на баре")
    print(f"  {k[0]:>14} {datetime.fromtimestamp(k[1]/1000, timezone.utc):%d %H:%M} vr {l['vr']:>5.1f} "
          f"sent {int(l['sent'])} lag {l['lag_min']:>5.1f}м — {'; '.join(why) or 'НЕОБЪЯСНЁН — разобрать вручную'}")
print("\n── только ЯДРО (live не логировал) ──")
for k in only_mine:
    m = mine[k]
    why = "вероятный даунтайм демона (live молчал ±90м)" if not alive_near(k[1]) else \
          "live жив рядом — вселенная скана/кулдаун-стейт/порядок; разобрать вручную"
    print(f"  {k[0]:>14} {datetime.fromtimestamp(k[1]/1000, timezone.utc):%d %H:%M} vr {m['vr']:>5.1f} "
          f"sent {int(m['sent'])} — {why}")
print(f"\ncounters ядра (окно с прогревом): { {k: v for k, v in counters.items() if v} }")

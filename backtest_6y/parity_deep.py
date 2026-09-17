#!/usr/bin/env python3
"""Глубокий разбор parity-расхождений (состав, без PnL): механика sent-мисматчей
и классификация «только-ядро при живом live» через live-кулдауны/ранг вселенной/каденс."""
import json, gzip, csv, os
from datetime import datetime, timezone
from core_replay import run_engine, BAR, uday

ROOT = os.path.expanduser("~/trading/backtest_6y")
def ms(s): return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()*1000)
WARM, CMP0, CMP1 = ms("2026-07-04"), ms("2026-07-07"), ms("2026-07-10")

uni = json.load(gzip.open(f"{ROOT}/data/universe_parity.json.gz", "rt"))
man = json.load(gzip.open(f"{ROOT}/data/universe_manifest_parity.json.gz", "rt"))
rank = {d: {s: i+1 for i, (s, _) in enumerate(m["top"])} for d, m in man.items()}
need = sorted({s for d, lst in uni.items() if d >= "2026-07-03" for s in lst})
bars = {}
for s in need:
    p = f"{ROOT}/data/klines_snapA/{s}.json.gz"
    if not os.path.exists(p): continue
    bars[s] = {int(k): v for k, v in json.load(gzip.open(p, "rt")).items()
               if ms("2026-07-01") <= int(k) <= CMP1 + 3*86400_000}
hit_log = []
run_engine(bars, {}, uni, WARM, CMP1, hit_log=hit_log)
myscan = {r["T"]: r for r in hit_log}
mine = {(s, r["T"]): {"vr": v, "sent": s in r["sent"], "mode": r["mode"], "n": len(r["hits"])}
        for r in hit_log if r["T"] >= CMP0 for s, v in r["hits"]}

rows = []
for row in csv.DictReader(open(os.path.expanduser("~/trading/outcomes/radar_hits.csv"))):
    t = int(datetime.strptime(row["ts_utc"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()*1000)
    rows.append({"sym": row["symbol"], "tms": t, "bar": (t//BAR - 1)*BAR,
                 "vr": float(row["vol_ratio"]), "sent": int(row["sent"])})
live = {}
for r in rows:
    if CMP0 <= r["tms"] < CMP1:
        k = (r["sym"], r["bar"])
        if k not in live or (r["sent"] and not live[k]["sent"]): live[k] = r

def live_cooldown_at(sym, scan_ms):
    """Был ли sym у БОЯ под кулдауном в момент скана (по его же логу)."""
    for r in rows:
        if r["sym"] != sym or r["tms"] >= scan_ms: continue
        exp = r["tms"] + (4*3600_000 if r["sent"] else 30*60_000)
        if exp > scan_ms: return f"live-кулдаун ({'sent 4ч' if r['sent'] else 'unsent 30м'} от {datetime.fromtimestamp(r['tms']/1000, timezone.utc):%d %H:%M})"
    return None
def live_alive_tight(scan_ms):
    return any(abs(r["tms"] - scan_ms) <= 10*60_000 for r in rows)

print("═══ 1. SENT-РАСХОЖДЕНИЯ: механика ═══")
for k in sorted(set(mine) & set(live)):
    m, l = mine[k], live[k]
    if m["sent"] == bool(l["sent"]): continue
    same_bar_live = [r["sym"] for r in rows if r["bar"] == k[1] and CMP0 <= r["tms"] < CMP1]
    print(f"{k[0]:>14} {datetime.fromtimestamp(k[1]/1000, timezone.utc):%d %H:%M} L{l['sent']}/M{int(m['sent'])} · "
          f"мой скан: mode={m['mode']} хитов_в_баре={m['n']} · live в том же баре логировал {len(same_bar_live)} монет: {same_bar_live[:6]}")

print("\n═══ 2. ТОЛЬКО-ЯДРО при живом live: классификация ═══")
cls = {}
unexplained = []
for k in sorted(set(mine) - set(live)):
    sym, T = k
    scan_ms = T + BAR
    tight = live_alive_tight(scan_ms)
    cd = live_cooldown_at(sym, scan_ms)
    rk = rank.get(uday(scan_ms), {}).get(sym)
    if not tight: reason = "микро-даунтайм (live молчал ±10м вокруг скана)"
    elif cd: reason = cd
    elif rk and rk > 110: reason = f"край вселенной (мой ранг {rk}/150 — live-лист скана иной)"
    else: reason = "НЕОБЪЯСНЁН"; unexplained.append((k, rk, mine[k]))
    cls[reason.split(" (")[0]] = cls.get(reason.split(" (")[0], 0) + 1
print("сводка:", dict(sorted(cls.items(), key=lambda x: -x[1])))
print(f"\nнеобъяснённых: {len(unexplained)} — построчно:")
for (sym, T), rk, m in unexplained:
    print(f"  {sym:>14} {datetime.fromtimestamp(T/1000, timezone.utc):%d %H:%M} vr={m['vr']} "
          f"mode={m['mode']} ранг={rk} sent={int(m['sent'])}")

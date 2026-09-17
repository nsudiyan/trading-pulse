#!/usr/bin/env python3
"""Качалка данных для шестилетнего honest-бэктеста (PREREG 2026-07-10).
Pass A: 30м свечи, последние 250 дней, ВСЕ линейные USDT-перпы (для smoke+parity).
Pass B: доистория каждого символа до max(2020-04-01, launchTime-7д).
Pass C: funding history с 2020-04-01 (для PnL удержания).
Манифест на символ: каждый чанк (url-параметры, retCode, n, границы, время фетча),
дыры в сетке 30м, первый/последний бар. Ошибка API != пустой ответ — пишется явно.
ponytail: один файл на символ, gz-json; raw-эквивалент = chunk-log (воспроизводимый путь)."""
import json, gzip, os, time, urllib.request, urllib.parse, threading
import concurrent.futures as cf
from datetime import datetime, timezone

ROOT = os.path.expanduser("~/trading/backtest_6y")
D_K, D_F, D_M = f"{ROOT}/data/klines", f"{ROOT}/data/funding", f"{ROOT}/data/manifest"
for d in (D_K, D_F, D_M): os.makedirs(d, exist_ok=True)

NOW_MS = int(datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc).timestamp()*1000)  # фикс. правая граница (закрытые сутки)
T0_MS  = int(datetime(2020, 4, 1, tzinfo=timezone.utc).timestamp()*1000)
BAR    = 1800_000
CH_K   = 1000*BAR              # 1000 баров 30м на запрос
CH_F   = 200*8*3600_000        # 200 фандинг-точек на запрос
RECENT = NOW_MS - 250*86400_000

_lock = threading.Lock()
def get(params, path="kline"):
    u = f"https://api.bybit.com/v5/market/{path}?{urllib.parse.urlencode(params)}"
    for i in range(5):
        try:
            r = json.load(urllib.request.urlopen(u, timeout=25))
            return r, None
        except Exception as e:
            if i == 4: return None, str(e)
            time.sleep(0.8*(i+1))

def fetch_range(sym, a, b, kind):
    """Скачать [a,b) чанками. Возвращает (bars|rows dict, chunk_log)."""
    out, log = {}, []
    cur, CH = a, (CH_K if kind == "kline" else CH_F)
    while cur < b:
        ce = min(cur+CH, b)
        if kind == "kline":
            p = {"category":"linear","symbol":sym,"interval":"30","start":cur,"end":ce,"limit":1000}
            r, err = get(p, "kline")
        else:
            p = {"category":"linear","symbol":sym,"startTime":cur,"endTime":ce,"limit":200}
            r, err = get(p, "funding/history")
        rec = {"start":cur,"end":ce,"fetched":int(time.time())}
        if err: rec["error"] = err
        else:
            rec["retCode"] = r.get("retCode")
            rows = r.get("result",{}).get("list",[]) or []
            rec["n"] = len(rows)
            if r.get("retCode") != 0: rec["retMsg"] = r.get("retMsg")
            for x in rows:
                if kind == "kline":
                    out[int(x[0])] = [int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5]),float(x[6])]
                else:
                    out[int(x["fundingRateTimestamp"])] = float(x["fundingRate"])
        log.append(rec); cur = ce; time.sleep(0.02)
    return out, log

def gaps_of(ts_sorted):
    g = []
    for i in range(1, len(ts_sorted)):
        d = ts_sorted[i]-ts_sorted[i-1]
        if d > BAR: g.append({"after":ts_sorted[i-1],"missing":d//BAR-1})
    return g

def load_gz(p): return json.load(gzip.open(p,"rt")) if os.path.exists(p) else None
def save_gz(p, obj):
    with gzip.open(p,"wt") as f: json.dump(obj,f,separators=(",",":"))

# инструменты (сырой снапшот сохраняем целиком)
ins_path = f"{ROOT}/data/instruments_2026-07-10.json"
if os.path.exists(ins_path):
    ins = json.load(open(ins_path))
else:
    ins, cursor = [], ""
    while True:
        p = {"category":"linear","limit":1000}
        if cursor: p["cursor"] = cursor
        r,_ = get(p, "instruments-info")
        res = r.get("result",{})
        ins += res.get("list",[])
        cursor = res.get("nextPageCursor","")
        if not cursor: break
    json.dump(ins, open(ins_path,"w"))
SYMS = sorted(i["symbol"] for i in ins
              if i["symbol"].endswith("USDT") and i.get("contractType")=="LinearPerpetual")
LAUNCH = {i["symbol"]: int(i.get("launchTime") or 0) for i in ins}
print(f"символов к качке: {len(SYMS)}", flush=True)

def mpath(s): return f"{D_M}/{s}.json"
def mread(s):
    return json.load(open(mpath(s))) if os.path.exists(mpath(s)) else {"symbol":s,"chunks":[],"passes":{}}
def mwrite(s, m): json.dump(m, open(mpath(s),"w"))

def do_symbol(sym, phase):
    m = mread(sym)
    if m["passes"].get(phase): return sym, "skip"
    kp, fp = f"{D_K}/{sym}.json.gz", f"{D_F}/{sym}.json.gz"
    try:
        if phase == "A":
            bars = {int(k):v for k,v in (load_gz(kp) or {}).items()}
            new, log = fetch_range(sym, RECENT, NOW_MS, "kline")
            bars.update(new)
        elif phase == "B":
            bars = {int(k):v for k,v in (load_gz(kp) or {}).items()}
            a = max(T0_MS, (LAUNCH.get(sym) or T0_MS) - 7*86400_000)
            new, log = fetch_range(sym, a, RECENT, "kline")
            bars.update(new)
        else:
            f0 = {int(k):v for k,v in (load_gz(fp) or {}).items()}
            a = max(T0_MS, (LAUNCH.get(sym) or T0_MS) - 7*86400_000)
            new, log = fetch_range(sym, a, NOW_MS, "funding")
            f0.update(new)
            save_gz(fp, f0)
            m["chunks"] += [{**c,"pass":phase} for c in log]
            m["passes"][phase] = True
            m["funding_n"] = len(f0)
            mwrite(sym, m)
            return sym, f"F n={len(f0)}"
        keys = sorted(bars)
        save_gz(kp, {str(k):bars[k] for k in keys})
        m["chunks"] += [{**c,"pass":phase} for c in log]
        m["passes"][phase] = True
        m["first_bar"] = keys[0] if keys else None
        m["last_bar"]  = keys[-1] if keys else None
        m["n_bars"]    = len(keys)
        if phase == "B": m["gaps"] = gaps_of(keys)
        m["errors"] = sum(1 for c in m["chunks"] if c.get("error") or (c.get("retCode") not in (0,None)))
        mwrite(sym, m)
        return sym, f"{phase} n={len(keys)}"
    except Exception as e:
        m.setdefault("fatal",[]).append({"pass":phase,"err":str(e)})
        mwrite(sym, m)
        return sym, f"FATAL {e}"

for phase, tag in (("A","ПАСС A (250д все символы)"),("B","ПАСС B (доистория до 2020-04)"),("C","ПАСС C (funding)")):
    print(f"═══ {tag} ═══", flush=True)
    done = 0
    with cf.ThreadPoolExecutor(6) as ex:
        for sym, st in ex.map(lambda s: do_symbol(s, phase), SYMS):
            done += 1
            if "FATAL" in st: print(f"  ⚠ {sym}: {st}", flush=True)
            if done % 25 == 0: print(f"  …{phase} {done}/{len(SYMS)}", flush=True)
    print(f"  {tag}: завершён", flush=True)

# сводный манифест
summ = {"generated": datetime.now(timezone.utc).isoformat(), "symbols": len(SYMS),
        "t0": T0_MS, "now": NOW_MS, "errors": {}, "coverage": {}}
tot_err = 0
for s in SYMS:
    m = mread(s)
    e = m.get("errors", 0)
    tot_err += e
    if e: summ["errors"][s] = e
    summ["coverage"][s] = {"first": m.get("first_bar"), "last": m.get("last_bar"),
                           "n": m.get("n_bars"), "gaps": len(m.get("gaps", [])),
                           "funding_n": m.get("funding_n")}
json.dump(summ, open(f"{D_M}/_summary.json","w"))
print(f"КАЧКА ЗАВЕРШЕНА: символов {len(SYMS)}, чанков с ошибками {tot_err}", flush=True)

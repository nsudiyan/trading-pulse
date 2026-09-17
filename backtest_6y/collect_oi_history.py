#!/usr/bin/env python3
"""OI-история для H-OI-01: интервал 4ч, вся глубина Bybit (probe: BTC с 2021-01),
все 617 симв. Манифест обязателен (Bybit предупреждает о задержках в экстрим-волу):
на чанк — границы/retCode/n/время фетча; на символ — first/last, дыры 4ч-сетки.
Ошибка ≠ пустой ответ. Хранение: data/oi/{SYM}.json.gz {ts_ms: openInterest}."""
import json, gzip, os, time, urllib.request, urllib.parse
import concurrent.futures as cf
from datetime import datetime, timezone

ROOT = os.path.expanduser("~/trading/backtest_6y")
D_O, D_M = f"{ROOT}/data/oi", f"{ROOT}/data/manifest_oi"
for d in (D_O, D_M): os.makedirs(d, exist_ok=True)
H4 = 4*3600_000
T0 = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp()*1000)
NOW = int(datetime(2026, 7, 10, tzinfo=timezone.utc).timestamp()*1000)
CH = 200*H4

ins = json.load(open(f"{ROOT}/data/instruments_2026-07-10.json"))
SYMS = sorted(i["symbol"] for i in ins
              if i["symbol"].endswith("USDT") and i.get("contractType") == "LinearPerpetual")
LAUNCH = {i["symbol"]: int(i.get("launchTime") or 0) for i in ins}

def get(u):
    for i in range(5):
        try: return json.load(urllib.request.urlopen(u, timeout=25)), None
        except Exception as e:
            if i == 4: return None, str(e)
            time.sleep(0.9*(i+1))

def do(sym):
    mp = f"{D_M}/{sym}.json"
    if os.path.exists(mp): return sym, "skip"
    a = max(T0, (LAUNCH.get(sym) or T0) - 86400_000)
    out, log, cur = {}, [], a
    while cur < NOW:
        ce = min(cur + CH, NOW)
        p = urllib.parse.urlencode({"category": "linear", "symbol": sym,
                                    "intervalTime": "4h", "startTime": cur, "endTime": ce, "limit": 200})
        r, err = get(f"https://api.bybit.com/v5/market/open-interest?{p}")
        rec = {"start": cur, "end": ce, "fetched": int(time.time())}
        if err: rec["error"] = err
        else:
            rec["retCode"] = r.get("retCode")
            rows = (r.get("result", {}) or {}).get("list", []) or []
            rec["n"] = len(rows)
            if r.get("retCode") != 0: rec["retMsg"] = r.get("retMsg")
            for x in rows: out[int(x["timestamp"])] = float(x["openInterest"])
        log.append(rec); cur = ce; time.sleep(0.05)
    keys = sorted(out)
    with gzip.open(f"{D_O}/{sym}.json.gz", "wt") as f:
        json.dump({str(k): out[k] for k in keys}, f, separators=(",", ":"))
    gaps = sum(1 for i in range(1, len(keys)) if keys[i] - keys[i-1] > H4)
    json.dump({"symbol": sym, "chunks": log, "first": keys[0] if keys else None,
               "last": keys[-1] if keys else None, "n": len(keys), "gaps": gaps,
               "errors": sum(1 for c in log if c.get("error") or c.get("retCode") not in (0, None))},
              open(mp, "w"))
    return sym, f"n={len(out)} gaps={gaps}"

done = 0
with cf.ThreadPoolExecutor(5) as ex:
    for sym, st in ex.map(do, SYMS):
        done += 1
        if done % 40 == 0: print(f"  …OI {done}/{len(SYMS)}", flush=True)
summ = {}
tot_err = 0
for s in SYMS:
    mp = f"{D_M}/{s}.json"
    if not os.path.exists(mp): continue
    m = json.load(open(mp))
    summ[s] = {"first": m["first"], "n": m["n"], "gaps": m["gaps"], "errors": m["errors"]}
    tot_err += m["errors"]
json.dump(summ, open(f"{D_M}/_summary.json", "w"))
print(f"OI-ИСТОРИЯ СОБРАНА: символов {len(summ)}, чанков с ошибками {tot_err}", flush=True)

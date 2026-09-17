#!/usr/bin/env python3
"""Починка чанков с ошибками (retCode!=0 / error) по манифестам: повторный фетч
одиночным потоком с мягким рейтом, мердж в gz, пересчёт дыр, обновление манифестов
и _summary. Прицельно — только ошибочные диапазоны (PREREG C: ошибки не глотаем)."""
import json, gzip, os, time, urllib.request, urllib.parse
from datetime import datetime, timezone

ROOT = os.path.expanduser("~/trading/backtest_6y")
BAR = 1800_000
def get(params, path):
    u = f"https://api.bybit.com/v5/market/{path}?{urllib.parse.urlencode(params)}"
    for i in range(6):
        try:
            r = json.load(urllib.request.urlopen(u, timeout=25))
            if r.get("retCode") == 0: return r, None
            err = f"retCode {r.get('retCode')}"
        except Exception as e: err = str(e)
        time.sleep(1.5*(i+1))
    return None, err
def load_gz(p): return json.load(gzip.open(p, "rt")) if os.path.exists(p) else {}
def save_gz(p, o):
    with gzip.open(p, "wt") as f: json.dump(o, f, separators=(",", ":"))

fixed = still = 0
summary = json.load(open(f"{ROOT}/data/manifest/_summary.json"))
for sym in sorted(summary.get("errors", {})):
    mp = f"{ROOT}/data/manifest/{sym}.json"
    m = json.load(open(mp))
    bad = [c for c in m["chunks"] if c.get("error") or (c.get("retCode") not in (0, None))]
    if not bad: continue
    kl = {int(k): v for k, v in load_gz(f"{ROOT}/data/klines/{sym}.json.gz").items()}
    fu = {int(k): v for k, v in load_gz(f"{ROOT}/data/funding/{sym}.json.gz").items()}
    for c in bad:
        width = c["end"] - c["start"]
        kind = "kline" if width <= 1000*BAR + 1 else "funding"
        if kind == "kline":
            r, err = get({"category":"linear","symbol":sym,"interval":"30",
                          "start":c["start"],"end":c["end"],"limit":1000}, "kline")
            rows = r["result"]["list"] if r else []
            for x in rows:
                kl[int(x[0])] = [int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5]),float(x[6])]
        else:
            r, err = get({"category":"linear","symbol":sym,"startTime":c["start"],
                          "endTime":c["end"],"limit":200}, "funding/history")
            rows = r["result"]["list"] if r else []
            for x in rows:
                fu[int(x["fundingRateTimestamp"])] = float(x["fundingRate"])
        c["repaired"] = {"ok": err is None, "n": len(rows), "err": err,
                         "at": datetime.now(timezone.utc).isoformat()}
        c.pop("error", None); c["retCode"] = 0 if err is None else c.get("retCode")
        fixed += err is None; still += err is not None
        time.sleep(0.25)
    keys = sorted(kl)
    save_gz(f"{ROOT}/data/klines/{sym}.json.gz", {str(k): kl[k] for k in keys})
    if fu: save_gz(f"{ROOT}/data/funding/{sym}.json.gz", {str(k): fu[k] for k in sorted(fu)})
    m["first_bar"], m["last_bar"], m["n_bars"] = (keys[0], keys[-1], len(keys)) if keys else (None, None, 0)
    m["gaps"] = [{"after": keys[i-1], "missing": (keys[i]-keys[i-1])//BAR - 1}
                 for i in range(1, len(keys)) if keys[i]-keys[i-1] > BAR]
    m["errors"] = sum(1 for c in m["chunks"] if c.get("error") or (c.get("retCode") not in (0, None)))
    m["funding_n"] = len(fu)
    json.dump(m, open(mp, "w"))
    print(f"  {sym}: починено чанков, ошибок осталось {m['errors']}, дыр {len(m['gaps'])}", flush=True)

for sym in list(summary["coverage"]):
    m = json.load(open(f"{ROOT}/data/manifest/{sym}.json"))
    summary["coverage"][sym] = {"first": m.get("first_bar"), "last": m.get("last_bar"),
                                "n": m.get("n_bars"), "gaps": len(m.get("gaps", [])),
                                "funding_n": m.get("funding_n")}
summary["errors"] = {s: json.load(open(f"{ROOT}/data/manifest/{s}.json")).get("errors", 0)
                     for s in summary["coverage"]}
summary["errors"] = {s: e for s, e in summary["errors"].items() if e}
summary["repaired_at"] = datetime.now(timezone.utc).isoformat()
json.dump(summary, open(f"{ROOT}/data/manifest/_summary.json", "w"))
print(f"ПОЧИНКА: успешно {fixed}, не удалось {still}; ошибок в манифестах осталось: {sum(summary['errors'].values())}")

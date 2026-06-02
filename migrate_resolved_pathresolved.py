#!/usr/bin/env python3
"""
migrate_resolved_pathresolved.py — ЧЕСТНО переразрешает both-hit строки outcomes/resolved.csv
по ПЕРВОМУ КАСАНИЮ УРОВНЯ на 5m klines (а не по времени ПИКА, как migrate_resolved_orderaware.py).

Контекст (council 2026-06-01): both-hit строки (цена за окно коснулась И TP, И стопа) помечены
TP1 (+1.07R) при недоказуемом порядке. migrate_resolved_orderaware.py пытался чинить через
time_to_mfe/mae, но это время ПИКА экстремума, а не первого касания УРОВНЯ → ранний клевок в
стоп (не абсолютный минимум) невидим → все 259 остались TP1. Live Claude-фильтр кормится иллюзией.

Этот скрипт: тянет 5m bars [run, run+гориз], идёт бар-за-баром, берёт ПЕРВЫЙ бар, коснувшийся
TP-уровня или стоп-уровня. Если один бар коснулся ОБОИХ — STOP (внутрибарный порядок недоказуем,
пессимистично). Меняет ТОЛЬКО порядко-зависимые поля: outcome_<h>, hit_tp1_<h>, hit_stop_<h>,
r_multiple_<h>, exit_price_<h>, exit_reason_<h>, outcome_label_<h>. mfe/mae/change не трогает.
Идемпотентно. Переиспользует уже скачанные klines из outcomes/latency_test/kl/.

Запуск:  python3 migrate_resolved_pathresolved.py            — fetch + DRY-RUN (только показать)
         python3 migrate_resolved_pathresolved.py --apply    — применить (бэкап + atomic write)
"""
import csv, sys, os, json, time, shutil, urllib.request
from datetime import datetime, timezone
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
F    = os.path.join(HERE, "outcomes", "resolved.csv")
KL_REUSE = os.path.join(HERE, "outcomes", "latency_test", "kl")   # уже скачанные (reuse)
KL_CACHE = os.path.join(HERE, "outcomes", "path_reresolve", "kl") # докачка сюда
APPLY = "--apply" in sys.argv
os.makedirs(KL_CACHE, exist_ok=True)


def _f(x):
    try:    return float(x)
    except (TypeError, ValueError): return None

def _truthy(x):
    return str(x).strip() in ("1", "1.0", "True", "true")

def _epoch(ts):
    return int(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())

def _outcome_label(outcome):
    if outcome in ("TP1", "WIN"):  return "profitable"
    if outcome in ("STOP", "LOSS"): return "unprofitable"
    return "breakeven"


def _fetch_5m(symbol, start_ms, end_ms):
    url = ("https://api.bybit.com/v5/market/kline?category=linear"
           f"&symbol={symbol}&interval=5&start={start_ms}&end={end_ms}&limit=1000")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    if d.get("retCode") != 0:
        raise RuntimeError(f"retCode {d.get('retCode')} {d.get('retMsg')}")
    out = []
    for b in d["result"]["list"]:   # desc, [ms,o,h,l,c,v]
        out.append([int(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])])
    out.sort(key=lambda x: x[0])    # ascending
    return out


def _get_bars(symbol, run_ts):
    """Возвращает список [ms,o,h,l,c,v] ascending. Reuse → cache → fetch."""
    ep = _epoch(run_ts)
    fn = f"{symbol}_{ep}.json"
    # reuse latency_test (формат: dict с ключом 'rows')
    p_reuse = os.path.join(KL_REUSE, fn)
    if os.path.exists(p_reuse):
        try:
            d = json.load(open(p_reuse))
            rows = d.get("rows") if isinstance(d, dict) else d
            if rows: return rows
        except Exception: pass
    # cache (формат: просто список rows)
    p_cache = os.path.join(KL_CACHE, fn)
    if os.path.exists(p_cache):
        try:
            rows = json.load(open(p_cache))
            if rows: return rows
        except Exception: pass
    # fetch [run, run+25h] (с запасом)
    run_ms = ep * 1000
    for attempt in range(3):
        try:
            rows = _fetch_5m(symbol, run_ms, run_ms + 25 * 3600 * 1000)
            json.dump(rows, open(p_cache, "w"), separators=(",", ":"))
            time.sleep(0.12)
            return rows
        except Exception as e:
            if attempt == 2:
                return None
            time.sleep(0.4)
    return None


def first_touch(bars, run_ts, horizon_h, direction, stop, tp1):
    """Возвращает 'TP1' / 'STOP' / 'NEITHER' / 'NODATA' по первому касанию УРОВНЯ."""
    if bars is None: return "NODATA"
    run_ms = _epoch(run_ts) * 1000
    end_ms = run_ms + horizon_h * 3600 * 1000
    win = [b for b in bars if run_ms <= b[0] < end_ms]
    if not win: return "NODATA"
    is_long = direction in ("ЛОНГ", "ЖДАТЬ")
    for _, o, hi, lo, c, v in win:
        if is_long:
            tp_hit = bool(tp1) and hi >= tp1
            sl_hit = bool(stop) and lo <= stop
        else:
            tp_hit = bool(tp1) and lo <= tp1
            sl_hit = bool(stop) and hi >= stop
        if tp_hit and sl_hit: return "STOP"   # один бар коснулся обоих → пессимистично
        if sl_hit:            return "STOP"
        if tp_hit:            return "TP1"
    return "NEITHER"


def main():
    rows = list(csv.DictReader(open(F, encoding="utf-8")))
    fields = list(rows[0].keys())
    print(f"строк в resolved.csv: {len(rows)}")

    # собрать both-hit задачи
    tasks = []  # (row_idx, horizon, row)
    for i, r in enumerate(rows):
        for h in ("4h", "24h"):
            if _truthy(r.get(f"hit_tp1_{h}")) and _truthy(r.get(f"hit_stop_{h}")):
                tasks.append((i, h, r))
    syms = {(r["symbol"], r["run_ts"]) for _, _, r in tasks}
    print(f"both-hit задач: {len(tasks)} (4h+24h)  | уникальных сигналов для klines: {len(syms)}")

    # резолв
    stats = defaultdict(lambda: defaultdict(int))     # [h][verdict] = n
    cell  = defaultdict(lambda: defaultdict(int))     # [(setup,dir)][verdict]=n
    changes = []   # (row_idx, h, new_outcome)  только для флипов TP1->STOP
    nodata = []
    r_old = {"4h": [], "24h": []}
    r_new = {"4h": [], "24h": []}
    bars_cache = {}
    done = 0
    for i, h, r in tasks:
        key = (r["symbol"], r["run_ts"])
        if key not in bars_cache:
            bars_cache[key] = _get_bars(r["symbol"], r["run_ts"])
            done += 1
            if done % 30 == 0:
                print(f"  ...klines {done}/{len(syms)}")
        bars = bars_cache[key]
        horizon = 4 if h == "4h" else 24
        verdict = first_touch(bars, r["run_ts"], horizon, r["direction"],
                              _f(r.get("stop")), _f(r.get("tp1")))
        stats[h][verdict] += 1
        cell[(r["setup"], r["direction"])][verdict] += 1
        ro = _f(r.get(f"r_multiple_{h}"))
        if ro is not None: r_old[h].append(ro)
        # новый R: TP1 оставляем старый R; STOP=-1.0; NEITHER/NODATA не трогаем
        if verdict == "STOP":
            r_new[h].append(-1.0)
            if (r.get(f"outcome_{h}") or "").strip() == "TP1":
                changes.append((i, h))
        elif verdict == "TP1":
            if ro is not None: r_new[h].append(ro)
        else:
            if ro is not None: r_new[h].append(ro)   # gap → оставляем как есть
            nodata.append((r["symbol"], r["run_ts"], h, verdict))

    print("\n=== ВЕРДИКТЫ ПО ПЕРВОМУ КАСАНИЮ (5m) ===")
    for h in ("4h", "24h"):
        s = stats[h]
        n = sum(s.values())
        if not n: continue
        print(f"  {h}: both-hit={n}  TP1-first(оставить)={s['TP1']}  "
              f"STOP-first(флип TP1→STOP)={s['STOP']}  NEITHER(gap)={s['NEITHER']}  NODATA={s['NODATA']}")
        if r_old[h] and r_new[h]:
            print(f"       both-hit R: было mean={sum(r_old[h])/len(r_old[h]):+.3f}  "
                  f"стало mean={sum(r_new[h])/len(r_new[h]):+.3f}")
    print("\n=== по setup×direction (24h, флипы) ===")
    for k in sorted(cell):
        c = cell[k]
        if c["STOP"] or c["TP1"]:
            print(f"  {k[0]:<12} {k[1]:<5}: TP1-keep={c['TP1']:>3}  →STOP={c['STOP']:>3}  gap={c['NEITHER']+c['NODATA']}")
    if nodata:
        print(f"\n⚠ {len(nodata)} строк без чистого касания на 5m (gap/делистинг) — оставлены без изменений:")
        for x in nodata[:12]: print("   ", x)

    flips = len(changes)
    print(f"\nИТОГО флипов TP1→STOP: {flips}  (из {len(tasks)} both-hit задач)")

    if not APPLY:
        print("\n[DRY-RUN] resolved.csv не тронут. Применить: python3 migrate_resolved_pathresolved.py --apply")
        return

    # ── APPLY: перечитать свежий CSV, защита от гонки со screener ──
    if os.path.getmtime(F) > _APPLY_GUARD_MTIME:
        print("⚠ resolved.csv изменился во время прогона (screener?) — ПРЕРВАНО. Перезапусти.")
        return
    fresh = list(csv.DictReader(open(F, encoding="utf-8")))
    if len(fresh) != len(rows):
        print(f"⚠ кол-во строк изменилось ({len(rows)}→{len(fresh)}) — ПРЕРВАНО. Перезапусти.")
        return
    for i, h in changes:
        r = fresh[i]
        # sanity: тот же сигнал
        if r["symbol"] != rows[i]["symbol"] or r["run_ts"] != rows[i]["run_ts"]:
            print("⚠ строки разъехались — ПРЕРВАНО."); return
        r[f"outcome_{h}"]       = "STOP"
        r[f"hit_tp1_{h}"]       = "0"
        r[f"hit_stop_{h}"]      = "1"
        r[f"r_multiple_{h}"]    = "-1.0"
        r[f"exit_reason_{h}"]   = "sl"
        if r.get("stop") not in (None, ""):
            r[f"exit_price_{h}"] = r["stop"]
        if f"outcome_label_{h}" in r:
            r[f"outcome_label_{h}"] = "unprofitable"

    bak = F + ".backup_" + time.strftime("%Y%m%d_%H%M%S") + "_pre_pathresolve"
    shutil.copy2(F, bak)
    tmp = F + ".tmp_pathresolve"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(fresh)
    os.replace(tmp, F)
    print(f"\n✅ применено {flips} флипов. Бэкап: {os.path.basename(bak)}")


_APPLY_GUARD_MTIME = os.path.getmtime(F) + 2  # +2с допуска; apply прерётся если CSV тронут сильно позже старта

if __name__ == "__main__":
    main()

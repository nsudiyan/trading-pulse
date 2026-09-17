#!/usr/bin/env python3
"""XEX-COLLECTOR — multi-exchange сбор для будущего cross-exchange lead-lag
(ROADMAP №2; директива Codex 11.07: «параллельно ТОЛЬКО данные, не тестируем»).

Bybit + Binance futures + OKX swap, топ-20 общих перпов по обороту Bybit:
- trades (сторона тейкера) и best bid/ask → 250мс-вёдра по биржевым меткам;
  решение будущего runner возможно только после локального `flush_ts`, а `d`
  остаётся диагностикой, не оценкой latency;
- L5-глубина: Binance depth5@500ms, OKX books5 (нотионалы b5/a5 в ведро);
  Bybit bbo из orderbook.1 (его L50 уже пишет flow-collector раз в минуту);
- clock.jsonl: раз в 60с REST serverTime всех трёх + локальное (дрифт-контроль);
- манифест: старты/реконнекты по биржам/ошибки/часовой heartbeat со счётчиками;
- диск-предохранитель (чужой прод!): <5ГБ свободно → прунит старейший день
  с записью в манифест; <2ГБ → останавливает запись (FATAL), сервер не душит.
Хранение: data_x/YYYY-MM-DD/{xex.jsonl, clock.jsonl, manifest.jsonl}; gzip по дням.
Ведро пишется ТОЛЬКО при событиях (альты молчат → диск экономится)."""
import json, gzip, os, time, threading, shutil, urllib.request, hashlib
from datetime import datetime, timezone
import websocket

ROOT = os.environ.get("XEX_ROOT", os.path.expanduser("~/trading/flow_collector/data_x"))
os.makedirs(ROOT, exist_ok=True)
TOP_N = 20
BUCKET_MS = 250
SCHEMA = 2
SCHEMA_VERSION = "xvenue-schema-v2-2026-07-11"

_lock = threading.RLock()
S = {"day": None, "files": {}, "buckets": {}, "sealed": {}, "stop_write": False,
     "counters": {"by": 0, "bn": 0, "ok": 0, "clock": 0, "late_xex_fragment": 0}}

def rest(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (xex-collector)"})
    for i in range(3):                     # OKX 403-ит дефолтный python-UA — заголовок обязателен
        try: return json.load(urllib.request.urlopen(req, timeout=20))
        except Exception:
            if i == 2: return None
            time.sleep(1.5)

def code_sha256():
    with open(__file__, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

def day_str(): return datetime.now(timezone.utc).strftime("%Y-%m-%d")
def _open_day(d):
    dd = f"{ROOT}/{d}"; os.makedirs(dd, exist_ok=True)
    for n in ("xex", "clock", "manifest"):
        S["files"][n] = open(f"{dd}/{n}.jsonl", "a", buffering=1)
def _gzip_old(d):
    dd = f"{ROOT}/{d}"
    for f in os.listdir(dd):
        if f.endswith(".jsonl"):
            with open(f"{dd}/{f}", "rb") as i, gzip.open(f"{dd}/{f}.gz", "wb") as o:
                o.write(i.read())
            os.remove(f"{dd}/{f}")

def emit(name, obj):
    with _lock:
        if S["stop_write"] and name != "manifest": return
        d = day_str()
        if d != S["day"]:
            old = S["day"]
            for f in S["files"].values(): f.close()
            _open_day(d); S["day"] = d
            if old:
                try: _gzip_old(old)
                except Exception as e: mani({"ev": "gzip_fail", "err": str(e)[:120]})
            mani({"ev": "rollover", "from": old})
        S["files"][name].write(json.dumps(obj, separators=(",", ":")) + "\n")
def mani(o): o["ts"] = int(time.time()*1000); emit("manifest", o)

# ── маппинг символов: пересечение трёх бирж по base, топ-20 по обороту Bybit ──
def build_mapping():
    by = rest("https://api.bybit.com/v5/market/tickers?category=linear")
    bn = rest("https://fapi.binance.com/fapi/v1/exchangeInfo")
    ok = rest("https://www.okx.com/api/v5/public/instruments?instType=SWAP")
    bn_set = {s["symbol"] for s in bn["symbols"]
              if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING"} if bn else set()
    ok_set = {i["instId"] for i in ok["data"] if i.get("state") == "live"} if ok else set()
    if not bn_set or not ok_set:
        mani({"ev": "map_source_empty", "bn": len(bn_set), "ok": len(ok_set)})
    rows = sorted((t for t in by["result"]["list"] if t["symbol"].endswith("USDT")),
                  key=lambda t: -float(t.get("turnover24h") or 0))
    out = []
    for t in rows:
        sym = t["symbol"]; base = sym[:-4]
        okx = f"{base}-USDT-SWAP"
        if sym in bn_set and okx in ok_set:
            out.append({"base": base, "by": sym, "bn": sym, "ok": okx})
        if len(out) >= TOP_N: break
    return out

MAP, BY2B, BN2B, OK2B = [], {}, {}, {}   # заполняется в main() ПОСЛЕ открытия манифеста

def bucket(exch, base, ts_srv, recv_ms):
    k = (exch, base, int(ts_srv)//BUCKET_MS)
    with _lock:
        if k in S["sealed"]:
            S["counters"]["late_xex_fragment"] += 1
            mani({"ev": "late_xex_fragment", "e": exch, "s": base, "t": k[2]*BUCKET_MS,
                  "recv_ts": int(recv_ms)})
            return None
        b = S["buckets"].setdefault(k, {"b": None, "a": None, "bn": 0.0, "sn": 0.0,
                                        "n": 0, "b5": None, "a5": None, "ds": [],
                                        "rmin": int(recv_ms), "rmax": int(recv_ms)})
        b["ds"].append(recv_ms - ts_srv)
        b["rmin"] = min(b["rmin"], int(recv_ms))
        b["rmax"] = max(b["rmax"], int(recv_ms))
        return b

def flush_once(now_ms=None):
    """Seal only buckets quiet in local receipt time for >=1s; testable without network."""
    flush_ts = int(time.time()*1000) if now_ms is None else int(now_ms)
    with _lock:
        done = [k for k, v in S["buckets"].items() if flush_ts - v["rmax"] >= 1000]
        rows = []
        for k in done:
            v = S["buckets"].pop(k)
            S["sealed"][k] = flush_ts
            r = {"e": k[0], "s": k[1], "t": k[2]*BUCKET_MS, "n": v["n"], "schema": SCHEMA,
                 "rmin": v["rmin"], "rmax": v["rmax"], "flush_ts": flush_ts,
                 "d": round(sum(v["ds"])/len(v["ds"])) if v["ds"] else None}
            for f in ("b", "a", "b5", "a5"):
                if v[f] is not None: r[f] = v[f]
            if v["n"]: r["bn"] = round(v["bn"], 2); r["sn"] = round(v["sn"], 2)
            rows.append(r)
        ttl = flush_ts - 24*3600_000
        S["sealed"] = {k: t for k, t in S["sealed"].items() if t >= ttl}
    for r in sorted(rows, key=lambda x: (x["flush_ts"], x["e"], x["s"], x["t"])):
        emit("xex", r)
    return rows

def flush_loop():
    while True:
        time.sleep(2)
        flush_once()

# ── Bybit: publicTrade + orderbook.1 (bbo с matching-ts cts) ──
def ws_bybit():
    def on_open(ws):
        mani({"ev": "by_open"})
        args = [f"publicTrade.{m['by']}" for m in MAP] + [f"orderbook.1.{m['by']}" for m in MAP]
        for i in range(0, len(args), 10):
            ws.send(json.dumps({"op": "subscribe", "args": args[i:i+10]}))
    def on_msg(ws, raw):
        recv = time.time()*1000
        m = json.loads(raw); t = m.get("topic", "")
        if t.startswith("publicTrade"):
            for d in m.get("data", []):
                base = BY2B.get(d["s"])
                if not base: continue
                b = bucket("by", base, int(d["T"]), recv)
                if b is None: continue
                nl = float(d["v"])*float(d["p"])
                b["bn" if d["S"] == "Buy" else "sn"] += nl; b["n"] += 1
                S["counters"]["by"] += 1
        elif t.startswith("orderbook.1"):
            d = m.get("data", {})
            base = BY2B.get(d.get("s"))
            if not base: return
            ts = int(m.get("cts") or m.get("ts"))
            b = bucket("by", base, ts, recv)
            if b is None: return
            if d.get("b"): b["b"] = float(d["b"][0][0])
            if d.get("a"): b["a"] = float(d["a"][0][0])
            S["counters"]["by"] += 1
    _run_ws("wss://stream.bybit.com/v5/public/linear", on_open, on_msg, "by", ping=20)

# ── Binance futures: aggTrade + bookTicker + depth5@500ms (combined stream) ──
def ws_binance():
    streams = "/".join(f"{m['bn'].lower()}@{ch}" for m in MAP
                       for ch in ("aggTrade", "bookTicker", "depth5@500ms"))
    url = f"wss://fstream.binance.com/stream?streams={streams}"
    def on_open(ws): mani({"ev": "bn_open"})
    def on_msg(ws, raw):
        recv = time.time()*1000
        m = json.loads(raw).get("data", {})
        et = m.get("e")
        if et == "aggTrade":
            base = BN2B.get(m["s"])
            if not base: return
            b = bucket("bn", base, int(m["T"]), recv)
            if b is None: return
            nl = float(m["q"])*float(m["p"])
            b["sn" if m["m"] else "bn"] += nl; b["n"] += 1    # m=true → тейкер продал
            S["counters"]["bn"] += 1
        elif et == "bookTicker":
            base = BN2B.get(m.get("s"))
            if not base: return
            b = bucket("bn", base, int(m.get("E") or m.get("T")), recv)
            if b is None: return
            b["b"] = float(m["b"]); b["a"] = float(m["a"])
            S["counters"]["bn"] += 1
        elif et == "depthUpdate":
            base = BN2B.get(m.get("s"))
            if not base: return
            b = bucket("bn", base, int(m.get("E")), recv)
            if b is None: return
            b["b5"] = round(sum(float(p)*float(q) for p, q in m.get("b", [])[:5]))
            b["a5"] = round(sum(float(p)*float(q) for p, q in m.get("a", [])[:5]))
            S["counters"]["bn"] += 1
    _run_ws(url, on_open, on_msg, "bn", ping=180)

# ── OKX: trades + bbo-tbt + books5 ──
def ws_okx():
    def on_open(ws):
        mani({"ev": "ok_open"})
        args = ([{"channel": "trades", "instId": m["ok"]} for m in MAP]
                + [{"channel": "bbo-tbt", "instId": m["ok"]} for m in MAP]
                + [{"channel": "books5", "instId": m["ok"]} for m in MAP])
        ws.send(json.dumps({"op": "subscribe", "args": args}))
    def on_msg(ws, raw):
        recv = time.time()*1000
        m = json.loads(raw)
        ch = (m.get("arg") or {}).get("channel"); inst = (m.get("arg") or {}).get("instId")
        base = OK2B.get(inst)
        if not base or "data" not in m: return
        if ch == "trades":
            for d in m["data"]:
                b = bucket("ok", base, int(d["ts"]), recv)
                if b is None: continue
                nl = float(d["sz"])*float(d["px"])*_ctval(inst)
                b["bn" if d["side"] == "buy" else "sn"] += nl; b["n"] += 1
                S["counters"]["ok"] += 1
        elif ch == "bbo-tbt":
            d = m["data"][0]
            b = bucket("ok", base, int(d["ts"]), recv)
            if b is None: return
            if d.get("bids"): b["b"] = float(d["bids"][0][0])
            if d.get("asks"): b["a"] = float(d["asks"][0][0])
            S["counters"]["ok"] += 1
        elif ch == "books5":
            d = m["data"][0]
            b = bucket("ok", base, int(d["ts"]), recv)
            if b is None: return
            cv = _ctval(inst)
            b["b5"] = round(sum(float(p)*float(q)*cv for p, q, *_ in d.get("bids", [])))
            b["a5"] = round(sum(float(p)*float(q)*cv for p, q, *_ in d.get("asks", [])))
            S["counters"]["ok"] += 1
    _run_ws("wss://ws.okx.com:8443/ws/v5/public", on_open, on_msg, "ok", ping=25)

_CTVAL = {}
def _ctval(inst):
    """OKX swap: размер в КОНТРАКТАХ → нотионал = sz × ctVal × px."""
    if inst not in _CTVAL:
        r = rest(f"https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId={inst}")
        try: _CTVAL[inst] = float(r["data"][0]["ctVal"])
        except Exception: _CTVAL[inst] = 1.0
    return _CTVAL[inst]

def _run_ws(url, on_open, on_msg, tag, ping):
    while True:
        try:
            ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_msg,
                on_close=lambda w, c, m: mani({"ev": f"{tag}_close", "code": c}),
                on_error=lambda w, e: mani({"ev": f"{tag}_error", "err": str(e)[:120]}))
            ws.run_forever(ping_interval=ping, ping_timeout=10)
        except Exception as e:
            mani({"ev": f"{tag}_crash", "err": str(e)[:120]})
        time.sleep(5)

def clock_probe(venue, url, extract):
    send_ts = int(time.time()*1000)
    response = rest(url)
    recv_ts = int(time.time()*1000)
    try:
        server_ts = extract(response) if response else None
    except Exception:
        server_ts = None
    return {"venue": venue, "send_ts": send_ts, "recv_ts": recv_ts, "server_ts": server_ts}

def clock_once():
    probes = [
        clock_probe("by", "https://api.bybit.com/v5/market/time", lambda x: int(x["result"]["timeNano"])//1_000_000),
        clock_probe("bn", "https://fapi.binance.com/fapi/v1/time", lambda x: int(x["serverTime"])),
        clock_probe("ok", "https://www.okx.com/api/v5/public/time", lambda x: int(x["data"][0]["ts"])),
    ]
    emit("clock", {"schema": SCHEMA, "probes": probes})
    S["counters"]["clock"] += 1
    return probes

def clock_loop():
    while True:
        clock_once()
        time.sleep(60)

def guard_loop():
    while True:
        free_gb = shutil.disk_usage("/").free/2**30
        if free_gb < 2:
            with _lock: S["stop_write"] = True
            mani({"ev": "FATAL_disk", "free_gb": round(free_gb, 1)})
        elif free_gb < 5:
            # жёсткая защита (Codex 11.07): прунить можно ТОЛЬКО дни с подтверждённой
            # внешней копией (маркер .pulled_ok ставит daily-pull после checksum-сверки).
            # Нет подтверждённых — лучше упереться в FATAL, чем стереть forward-историю.
            days = sorted(d for d in os.listdir(ROOT) if d[0].isdigit() and d != S["day"])
            pruned = False
            for d in days:
                if os.path.exists(f"{ROOT}/{d}/.pulled_ok"):
                    shutil.rmtree(f"{ROOT}/{d}")
                    mani({"ev": "prune_day", "day": d, "free_gb": round(free_gb, 1)})
                    pruned = True; break
            if not pruned:
                mani({"ev": "prune_blocked_no_pulled_copy", "free_gb": round(free_gb, 1)})
        mani({"ev": "hb", **S["counters"], "free_gb": round(free_gb, 1),
              "pend": len(S["buckets"])})
        time.sleep(3600)

def main():
    with _lock:
        S["day"] = day_str(); _open_day(S["day"])
    MAP.extend(build_mapping())
    BY2B.update({m["by"]: m["base"] for m in MAP})
    BN2B.update({m["bn"]: m["base"] for m in MAP})
    OK2B.update({m["ok"]: m["base"] for m in MAP})
    mani({"ev": "schema_v2_start", "schema": SCHEMA, "version": SCHEMA_VERSION,
          "code_sha256": code_sha256(), "pid": os.getpid(), "map": MAP})
    if not MAP:
        mani({"ev": "FATAL_empty_map"}); time.sleep(60); raise SystemExit(1)
    for m in MAP: _ctval(m["ok"])                       # прогреть ctVal до потока
    for fn in (flush_loop, clock_loop, guard_loop):
        threading.Thread(target=fn, daemon=True).start()
    for fn in (ws_bybit, ws_binance, ws_okx):
        threading.Thread(target=fn, daemon=True).start()
    while True: time.sleep(60)

if __name__ == "__main__":
    main()

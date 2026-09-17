#!/usr/bin/env python3
"""FLOW-COLLECTOR для H-FLOW-01 (спека Codex 11.07): append-only forward-сбор
причинного слоя — ликвидации (сырьё), taker buy/sell notional (1м-вёдра),
книга L10/L50 + спред (1м REST-снапшоты), OI+funding (5м из tickers),
динамический топ-150 (1ч, версионируется). Манифест: старты/реконнекты/дыры/счётчики.

Хранение: ~/trading/flow_collector/data/YYYY-MM-DD/{liq,trades_1m,book_1m,oi_5m,universe_1h,manifest}.jsonl
(вчерашний день гзипается на ролловере). Диск ≈ 30-60 МБ/день.
⚠ Семантика ликвидаций Bybit: side=Buy — ликвидирован ШОРТ (buy-ордер закрывает шорт)."""
import json, gzip, os, time, threading, shutil, urllib.request, traceback, hashlib
from datetime import datetime, timezone
import websocket

ROOT = os.path.expanduser("~/trading/flow_collector/data")
WS_URL = "wss://stream.bybit.com/v5/public/linear"
TOP_N = 150
SCHEMA = 2
SCHEMA_VERSION = "hflow-schema-v2-2026-07-11"
STABLE_BASES = {
    "USDC", "USDE", "FDUSD", "TUSD", "DAI", "USDD", "USTC", "PYUSD", "GUSD",
    "EUR", "EURC", "EURT", "EURI", "AEUR", "USD1", "USDR", "USDX", "USDY",
    "BUSD", "LUSD", "FRAX", "USDP", "SUSD", "CRVUSD", "GHO", "USDB", "USDF",
}
# Fallback only. Current instrument metadata is authoritative and also catches new names (e.g. CLUSDT).
COMMODITY_BASES = {"XAU", "XAG", "XAUT", "PAXG"}
os.makedirs(ROOT, exist_ok=True)

_lock = threading.RLock()   # реентерабельный: flush/rollover зовут emit ПОД локом
state = {"day": None, "files": {}, "top": [], "sub_syms": set(),
         "tr_buckets": {}, "sealed_mins": {}, "instrument_types": None, "instrument_recv_ts": 0,
         "stop_write": False,
         "counters": {"liq": 0, "trades": 0, "book": 0, "oi": 0, "late_trade_fragment": 0},
         "ws": None, "last_msg": time.time()}

def utcnow(): return datetime.now(timezone.utc)
def day_str(): return utcnow().strftime("%Y-%m-%d")

def _open_day(d):
    dd = f"{ROOT}/{d}"
    os.makedirs(dd, exist_ok=True)
    for name in ("liq", "trades_1m", "book_1m", "oi_5m", "universe_1h", "manifest"):
        state["files"][name] = open(f"{dd}/{name}.jsonl", "a", buffering=1)

def _gzip_old(d_old):
    dd = f"{ROOT}/{d_old}"
    for f in os.listdir(dd):
        if f.endswith(".jsonl"):
            p = f"{dd}/{f}"
            with open(p, "rb") as fin, gzip.open(p + ".gz", "wb") as fout:
                fout.write(fin.read())
            os.remove(p)

def emit(name, obj):
    with _lock:
        if state["stop_write"] and name != "manifest":
            return
        d = day_str()
        if d != state["day"]:
            old = state["day"]
            for f in state["files"].values(): f.close()
            _open_day(d)
            state["day"] = d
            if old:
                try: _gzip_old(old)
                except Exception as e: mani({"ev": "gzip_fail", "err": str(e)})
            mani({"ev": "rollover", "from": old})
        state["files"][name].write(json.dumps(obj, separators=(",", ":")) + "\n")

def mani(obj):
    obj["ts"] = int(time.time()*1000)
    emit("manifest", obj)

def rest(path, params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    for i in range(3):
        try:
            return json.load(urllib.request.urlopen(
                f"https://api.bybit.com/v5/market/{path}?{q}", timeout=15))
        except Exception:
            if i == 2: return None
            time.sleep(1)

def code_sha256():
    with open(__file__, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

def refresh_instrument_types(now):
    """Cache Bybit metadata; on startup failure fail closed, later retain last known good set."""
    if state["instrument_types"] is not None and now - state["instrument_recv_ts"] < 6*3600_000:
        return True
    r = rest("instruments-info", {"category": "linear", "limit": 1000})
    recv_ts = int(time.time()*1000)
    if not r or r.get("retCode") != 0:
        mani({"ev": "instrument_metadata_fail", "has_cached": state["instrument_types"] is not None})
        return state["instrument_types"] is not None
    types = {i.get("symbol"): i.get("symbolType", "") for i in r["result"].get("list", []) if i.get("symbol")}
    if not types:
        mani({"ev": "instrument_metadata_empty", "has_cached": state["instrument_types"] is not None})
        return state["instrument_types"] is not None
    state["instrument_types"] = types
    state["instrument_recv_ts"] = recv_ts
    return True

# ── динамический топ-150 + OI/funding из одного вызова tickers ──
def refresh_universe():
    r = rest("tickers", {"category": "linear"})
    if not r or r.get("retCode") != 0:
        mani({"ev": "tickers_fail"}); return
    now = int(time.time()*1000)  # response receipt time, not request-start time
    if not refresh_instrument_types(now):
        mani({"ev": "universe_fail_closed_no_metadata"}); return
    types = state["instrument_types"]
    excluded = {"stable": 0, "commodity": 0, "stock": 0, "unknown_meta": 0}
    rows = []
    for t in r["result"]["list"]:
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        typ = types.get(s)
        if typ is None:
            excluded["unknown_meta"] += 1; continue
        if base in STABLE_BASES:
            excluded["stable"] += 1; continue
        if typ == "commodity" or base in COMMODITY_BASES:
            excluded["commodity"] += 1; continue
        if typ == "stock":
            excluded["stock"] += 1; continue
        rows.append(t)
    rows.sort(key=lambda t: -float(t.get("turnover24h") or 0))
    top = [t["symbol"] for t in rows[:TOP_N]]
    with _lock:
        changed = top != state["top"]
        state["top"] = top
    emit("universe_1h", {"ts": now, "recv_ts": now, "top": top, "excluded": excluded,
                          "instrument_recv_ts": state["instrument_recv_ts"], "schema": SCHEMA})
    if changed: resubscribe()
    for t in rows[:TOP_N]:
        emit("oi_5m", {"ts": now, "recv_ts": now, "schema": SCHEMA, "s": t["symbol"], "oi": float(t.get("openInterest") or 0),
                       "oiv": float(t.get("openInterestValue") or 0),
                       "f": float(t.get("fundingRate") or 0),
                       "nf": int(t.get("nextFundingTime") or 0),
                       "last": float(t.get("lastPrice") or 0)})
        state["counters"]["oi"] += 1

def oi_loop():
    while True:
        try: refresh_universe()
        except Exception as e: mani({"ev": "oi_loop_err", "err": str(e)[:200]})
        time.sleep(300)

# ── книга: REST-снапшот L50 раз в минуту на символ (растянуто по минуте) ──
def book_loop():
    while True:
        top = list(state["top"])
        if not top: time.sleep(5); continue
        pause = max(0.2, 55.0/len(top))
        for s in top:
            r = rest("orderbook", {"category": "linear", "symbol": s, "limit": 50})
            recv_ts = int(time.time()*1000)
            if r and r.get("retCode") == 0:
                res = r["result"]
                b, a = res.get("b") or [], res.get("a") or []
                if b and a:
                    bid, ask = float(b[0][0]), float(a[0][0])
                    f10 = lambda side: sum(float(p)*float(v) for p, v in side[:10])
                    f50 = lambda side: sum(float(p)*float(v) for p, v in side[:50])
                    emit("book_1m", {"ts": int(res.get("ts") or recv_ts), "recv_ts": recv_ts, "schema": SCHEMA, "s": s,
                                     "spread_bp": round((ask-bid)/bid*1e4, 3),
                                     "b10": round(f10(b)), "a10": round(f10(a)),
                                     "b50": round(f50(b)), "a50": round(f50(a))})
                    state["counters"]["book"] += 1
            time.sleep(pause)

# ── WS: ликвидации сырьём + trades → минутные вёдра ──
def flush_buckets(force=False):
    flush_ts = int(time.time()*1000)
    now_min = flush_ts // 60_000
    with _lock:
        done = [k for k in state["tr_buckets"] if force or k[1] < now_min]
        for k in done:
            v = state["tr_buckets"].pop(k)
            state["sealed_mins"][k] = flush_ts
            emit("trades_1m", {"s": k[0], "min": k[1]*60_000, "first_recv_ts": v["first_recv_ts"],
                                "last_recv_ts": v["last_recv_ts"], "flush_ts": flush_ts, "schema": SCHEMA,
                                "buyN": round(v["buyN"], 2), "sellN": round(v["sellN"], 2), "n": v["n"]})
        expire = now_min - 24*60
        state["sealed_mins"] = {k: v for k, v in state["sealed_mins"].items() if k[1] >= expire}

def on_message(ws, raw):
    state["last_msg"] = time.time()
    recv_ts = int(time.time()*1000)
    try: m = json.loads(raw)
    except Exception: return
    topic = m.get("topic", "")
    if topic.startswith("allLiquidation"):
        for d in m.get("data", []):
            emit("liq", {"ts": int(d.get("T") or 0), "recv_ts": recv_ts, "schema": SCHEMA, "s": d.get("s"), "side": d.get("S"),
                         "v": float(d.get("v") or 0), "p": float(d.get("p") or 0)})
            state["counters"]["liq"] += 1
    elif topic.startswith("publicTrade"):
        for d in m.get("data", []):
            s, mn = d["s"], int(int(d["T"])//60000)
            key = (s, mn)
            with _lock:
                if key in state["sealed_mins"] or mn < recv_ts//60_000 - 1:
                    state["counters"]["late_trade_fragment"] += 1
                    mani({"ev": "late_trade_fragment", "s": s, "min": mn*60_000, "recv_ts": recv_ts})
                    continue
                b = state["tr_buckets"].setdefault(key, {"buyN": 0.0, "sellN": 0.0, "n": 0,
                                                           "first_recv_ts": recv_ts, "last_recv_ts": recv_ts})
                notional = float(d["v"])*float(d["p"])
                b["buyN" if d["S"] == "Buy" else "sellN"] += notional
                b["n"] += 1
                b["last_recv_ts"] = recv_ts
            state["counters"]["trades"] += 1

def resubscribe():
    ws = state.get("ws")
    top = list(state["top"])
    if not ws or not top: return
    want = set(top)
    old = state["sub_syms"]
    add, rem = sorted(want - old), sorted(old - want)
    try:
        for i in range(0, len(rem), 10):
            args = [f"allLiquidation.{s}" for s in rem[i:i+10]] + [f"publicTrade.{s}" for s in rem[i:i+10]]
            ws.send(json.dumps({"op": "unsubscribe", "args": args}))
        for i in range(0, len(add), 10):
            args = [f"allLiquidation.{s}" for s in add[i:i+10]] + [f"publicTrade.{s}" for s in add[i:i+10]]
            ws.send(json.dumps({"op": "subscribe", "args": args}))
        state["sub_syms"] = want
        if add or rem: mani({"ev": "resub", "add": len(add), "rem": len(rem)})
    except Exception as e:
        mani({"ev": "resub_err", "err": str(e)[:200]})

def on_open(ws):
    state["ws"] = ws
    state["sub_syms"] = set()
    mani({"ev": "ws_open"})
    resubscribe()

def on_close(ws, code, msg): mani({"ev": "ws_close", "code": code})
def on_error(ws, err): mani({"ev": "ws_error", "err": str(err)[:200]})

def flush_loop():
    while True:
        time.sleep(10)
        try: flush_buckets()
        except Exception as e: mani({"ev": "flush_err", "err": str(e)[:200]})

def guard_once(free_gb=None):
    """One conservative disk-safety pass; separated for no-network tests."""
    if free_gb is None:
        free_gb = shutil.disk_usage("/").free/2**30
    if free_gb < 2:
        with _lock: state["stop_write"] = True
        mani({"ev": "FATAL_disk", "free_gb": round(free_gb, 1)})
    elif free_gb < 5:
        days = sorted(d for d in os.listdir(ROOT) if d[:1].isdigit() and d != state["day"])
        pruned = False
        for d in days:
            if os.path.exists(f"{ROOT}/{d}/.pulled_ok"):
                shutil.rmtree(f"{ROOT}/{d}")
                mani({"ev": "prune_day", "day": d, "free_gb": round(free_gb, 1)})
                pruned = True; break
        if not pruned:
            mani({"ev": "prune_blocked_no_pulled_copy", "free_gb": round(free_gb, 1)})
    mani({"ev": "hb", **state["counters"], "top_n": len(state["top"]), "free_gb": round(free_gb, 1),
          "pending": len(state["tr_buckets"])})

def guard_loop():
    while True:
        guard_once()
        time.sleep(3600)

def main():
    with _lock:
        state["day"] = day_str(); _open_day(state["day"])
    mani({"ev": "schema_v2_start", "schema": SCHEMA, "version": SCHEMA_VERSION,
          "code_sha256": code_sha256(), "pid": os.getpid()})
    refresh_universe()
    for fn in (oi_loop, book_loop, flush_loop, guard_loop):
        threading.Thread(target=fn, daemon=True).start()
    while True:                                   # реконнект-петля; дыры видны по манифесту
        try:
            ws = websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message,
                                        on_close=on_close, on_error=on_error)
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            mani({"ev": "run_forever_err", "err": str(e)[:200]})
        mani({"ev": "reconnect_wait"})
        time.sleep(5)

if __name__ == "__main__":
    main()

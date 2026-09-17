#!/usr/bin/env python3
"""FLOW-COLLECTOR v3 — DOM-слой. Локальная книга из WS-дельт + признаки на 1с-сетке.

Не хранит сырую книгу (30+ ГБ/сут на 175 символов). Хранит производные признаки:
OFI, book imbalance по глубинам, крупные лимитки, absorption, footprint-дельту, скорость ленты.

Хранение: ~/trading/flow_collector/data_dom/YYYY-MM-DD/{dom_1s,dom_events,manifest}.jsonl(.gz)

⚠ ЧЕСТНОСТЬ ДАННЫХ: каждая 1с-строка несёт флаг dirty. dirty=1 означает, что книга в этой
секунде была рассинхронизирована (разрыв seq, реконнект, ожидание снапшота) и признаки
считать НЕЛЬЗЯ. Молчаливая подстановка нулей вместо dirty — классический тихий баг,
который делает бэктест недостоверным. Фильтруй dirty==0 в любом анализе.

⚠ ВРЕМЯ: ts_sec — секунда по ЛОКАЛЬНЫМ часам приёма. lat_p50 — (recv_local - cts_exchange),
включает и сеть, и рассинхрон локальных часов. Если часы не синхронены NTP, lat врёт.
"""
import json, gzip, os, time, threading, shutil, urllib.request, hashlib, collections
from datetime import datetime, timezone
import websocket

ROOT = os.path.expanduser("~/trading/flow_collector/data_dom")
WS_URL = "wss://stream.bybit.com/v5/public/linear"
DEPTH = int(os.environ.get("DOM_DEPTH", "50"))        # 50 | 200
TOP_N = int(os.environ.get("DOM_TOP_N", "30"))
SYMS_ENV = os.environ.get("DOM_SYMS", "").strip()
SCHEMA = 3
SCHEMA_VERSION = "dom-schema-v3-2026-07-20"
LEVELS = (1, 5, 10, 25, 50)          # глубины для book imbalance

os.makedirs(ROOT, exist_ok=True)
_lock = threading.RLock()

state = {"day": None, "files": {}, "syms": [], "sub": set(), "ws": None,
         "books": {}, "acc": {}, "stop_write": False,
         "counters": {"ob_msg": 0, "trade_msg": 0, "rows": 0, "events": 0,
                      "resync": 0, "gap": 0, "dirty_sec": 0}}


def utcnow(): return datetime.now(timezone.utc)
def day_str(): return utcnow().strftime("%Y-%m-%d")


def _open_day(d):
    dd = f"{ROOT}/{d}"
    os.makedirs(dd, exist_ok=True)
    for name in ("dom_1s", "dom_events", "manifest"):
        state["files"][name] = open(f"{dd}/{name}.jsonl", "a", buffering=1)


def _gzip_old(d_old):
    dd = f"{ROOT}/{d_old}"
    if not os.path.isdir(dd):
        return
    for f in os.listdir(dd):
        if f.endswith(".jsonl"):
            p = f"{dd}/{f}"
            with open(p, "rb") as fin, gzip.open(p + ".gz", "wb", compresslevel=6) as fout:
                shutil.copyfileobj(fin, fout)
            os.remove(p)


def emit(name, obj):
    with _lock:
        if state["stop_write"] and name != "manifest":
            return
        d = day_str()
        if d != state["day"]:
            old = state["day"]
            for f in state["files"].values():
                f.close()
            _open_day(d)
            state["day"] = d
            if old:
                try:
                    _gzip_old(old)
                except Exception as e:
                    mani({"ev": "gzip_fail", "err": str(e)[:200]})
            mani({"ev": "rollover", "from": old})
        state["files"][name].write(json.dumps(obj, separators=(",", ":")) + "\n")


def mani(obj):
    obj["ts"] = int(time.time() * 1000)
    emit("manifest", obj)


def rest(path, params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    for i in range(3):
        try:
            return json.load(urllib.request.urlopen(
                f"https://api.bybit.com/v5/market/{path}?{q}", timeout=15))
        except Exception:
            if i == 2:
                return None
            time.sleep(1)


def code_sha256():
    with open(__file__, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ──────────────────────────── локальная книга ────────────────────────────
class Book:
    """Локальная книга из WS-дельт Bybit v5.

    Инварианты: b/a — dict price->size. dirty=True пока не пришёл валидный снапшот
    или после разрыва последовательности. prev_* — состояние для OFI (Cont et al.)."""

    __slots__ = ("b", "a", "u", "seq", "dirty", "prev_bp", "prev_bq", "prev_ap", "prev_aq",
                 "size_hist", "thr", "last_thr_ts", "last_trade_px", "last_trade_ts")

    def __init__(self):
        self.b = {}
        self.a = {}
        self.u = -1
        self.seq = -1
        self.dirty = True
        self.prev_bp = self.prev_bq = self.prev_ap = self.prev_aq = None
        self.size_hist = collections.deque(maxlen=4000)   # выборка размеров уровней
        self.thr = None                                    # порог "крупной" заявки, в базовой валюте
        self.last_thr_ts = 0
        self.last_trade_px = None
        self.last_trade_ts = 0

    def pre_sizes(self, m):
        """Размеры затронутых дельтой уровней ДО применения. {(side,price): size}"""
        d = m["data"]
        out = {}
        for side, arr in (("b", d.get("b", [])), ("a", d.get("a", []))):
            book = self.b if side == "b" else self.a
            for p, _ in arr:
                fp = float(p)
                out[(side, fp)] = book.get(fp, 0.0)
        return out

    def sample_sizes(self):
        """Копим распределение размеров уровней для адаптивного порога 'крупной' заявки.
        Порог = p99 наблюдённых размеров, пересчёт раз в 60с."""
        now = time.time()
        if self.b:
            self.size_hist.append(max(self.b.values()))
        if self.a:
            self.size_hist.append(max(self.a.values()))
        if now - self.last_thr_ts > 60 and len(self.size_hist) >= 500:
            arr = sorted(self.size_hist)
            self.thr = arr[int(len(arr) * 0.99)]
            self.last_thr_ts = now

    def apply(self, m):
        """Возвращает 'ok' | 'resync' (был разрыв, книга сброшена и ждёт снапшот)."""
        d = m["data"]
        typ = m.get("type")
        u, seq = d.get("u", -1), d.get("seq", -1)

        if typ == "snapshot" or u == 1:
            # u==1 = снапшот после рестарта сервиса Bybit — книгу обязательно сбросить
            self.b, self.a = {}, {}
            for p, v in d.get("b", []):
                if float(v) > 0:
                    self.b[float(p)] = float(v)
            for p, v in d.get("a", []):
                if float(v) > 0:
                    self.a[float(p)] = float(v)
            self.u, self.seq, self.dirty = u, seq, False
            return "ok"

        # delta
        if self.dirty:
            return "wait"                    # ждём снапшот, дельты не применяем
        if self.u >= 0 and u != self.u + 1:
            self.dirty = True                # ДЫРА: не склеиваем молча
            self.b, self.a = {}, {}
            return "resync"
        for p, v in d.get("b", []):
            p, v = float(p), float(v)
            if v == 0:
                self.b.pop(p, None)
            else:
                self.b[p] = v
        for p, v in d.get("a", []):
            p, v = float(p), float(v)
            if v == 0:
                self.a.pop(p, None)
            else:
                self.a[p] = v
        self.u, self.seq = u, seq
        return "ok"

    def best(self):
        if not self.b or not self.a:
            return None, None, None, None
        bp = max(self.b)
        ap = min(self.a)
        return bp, self.b[bp], ap, self.a[ap]

    def depth(self, n):
        """Нотионал по n лучшим уровням с каждой стороны."""
        bs = sorted(self.b.items(), key=lambda x: -x[0])[:n]
        as_ = sorted(self.a.items(), key=lambda x: x[0])[:n]
        return (sum(p * v for p, v in bs), sum(p * v for p, v in as_))


def ofi_step(bk, bp, bq, ap, aq):
    """OFI по лучшим котировкам, Cont-Kukanov-Stoikov 2014, формула (4).
    e = I(Pb>=Pb')*qb - I(Pb<=Pb')*qb' - I(Pa<=Pa')*qa + I(Pa>=Pa')*qa'
    Знак: >0 = давление вверх. Отмена бида и маркет-селл дают одинаковый вклад."""
    if bk.prev_bp is None:
        return 0.0
    e = 0.0
    if bp >= bk.prev_bp:
        e += bq
    if bp <= bk.prev_bp:
        e -= bk.prev_bq
    if ap <= bk.prev_ap:
        e -= aq
    if ap >= bk.prev_ap:
        e += bk.prev_aq
    return e


# ──────────────────────────── аккумулятор 1с ────────────────────────────
def new_acc():
    return {"ofi": 0.0, "ofi_n": 0, "upd": 0, "dirty": 0,
            "buyN": 0.0, "sellN": 0.0, "ntr": 0, "buyq": 0.0, "sellq": 0.0,
            "px_first": None, "px_last": None, "px_hi": None, "px_lo": None,
            "add_big": 0, "cxl_big": 0, "add_bigN": 0.0, "cxl_bigN": 0.0,
            "fill_at_bid": 0.0, "fill_at_ask": 0.0,
            "lat": [], "snap": {}}


def acc_for(s, sec):
    a = state["acc"].get(s)
    if a is None or a["sec"] != sec:
        if a is not None:
            flush_row(s, a)
        a = new_acc()
        a["sec"] = sec
        state["acc"][s] = a
    return a


def flush_row(s, a):
    bk = state["books"].get(s)
    row = {"ts": a["sec"] * 1000, "s": s, "schema": SCHEMA,
           "dirty": 1 if a["dirty"] else 0,
           "upd": a["upd"], "ofi": round(a["ofi"], 4),
           "buyN": round(a["buyN"], 2), "sellN": round(a["sellN"], 2), "ntr": a["ntr"],
           "add_big": a["add_big"], "cxl_big": a["cxl_big"],
           "add_bigN": round(a["add_bigN"], 1), "cxl_bigN": round(a["cxl_bigN"], 1),
           "fill_bid": round(a["fill_at_bid"], 2), "fill_ask": round(a["fill_at_ask"], 2)}
    if a["px_last"] is not None:
        row.update({"o": a["px_first"], "h": a["px_hi"], "l": a["px_lo"], "c": a["px_last"]})
    snap = a["snap"]
    if snap:
        row.update(snap)
    if a["lat"]:
        a["lat"].sort()
        row["lat_p50"] = int(a["lat"][len(a["lat"]) // 2])
    emit("dom_1s", row)
    state["counters"]["rows"] += 1
    if a["dirty"]:
        state["counters"]["dirty_sec"] += 1


def snapshot_features(bk):
    """Срез книги на конец секунды: mid, spread, глубины, имбалансы."""
    bp, bq, ap, aq = bk.best()
    if bp is None:
        return {}
    mid = (bp + ap) / 2
    out = {"mid": round(mid, 8), "spread_bp": round((ap - bp) / mid * 1e4, 3)}
    for n in LEVELS:
        db, da = bk.depth(n)
        out[f"b{n}"] = round(db)
        out[f"a{n}"] = round(da)
        tot = db + da
        out[f"imb{n}"] = round((db - da) / tot, 4) if tot > 0 else 0.0
    return out


# ──────────────────────────── обработка WS ────────────────────────────
def on_message(ws, raw):
    recv = time.time()
    recv_ms = int(recv * 1000)
    sec = int(recv)
    try:
        m = json.loads(raw)
    except Exception:
        return
    topic = m.get("topic", "")
    if not topic:
        return

    if topic.startswith("orderbook"):
        s = topic.rsplit(".", 1)[-1]
        with _lock:
            bk = state["books"].setdefault(s, Book())
            a = acc_for(s, sec)
            # снимаем ПРЕДЫДУЩИЕ размеры затронутых уровней ДО применения дельты —
            # иначе снятие крупной заявки невосстановимо (её размер уже стёрт)
            pre = bk.pre_sizes(m) if not bk.dirty and m.get("type") != "snapshot" else None
            res = bk.apply(m)
            state["counters"]["ob_msg"] += 1

            if res == "resync":
                state["counters"]["gap"] += 1
                a["dirty"] = 1
                mani({"ev": "book_gap", "s": s, "expected_u": bk.u + 1, "got_u": m["data"].get("u")})
                emit("dom_events", {"ts": recv_ms, "s": s, "ev": "gap", "schema": SCHEMA})
                state["counters"]["events"] += 1
                return
            if res == "wait":
                a["dirty"] = 1
                return

            a["upd"] += 1
            cts = m.get("cts") or m.get("ts")
            if cts:
                a["lat"].append(recv_ms - cts)

            bp, bq, ap, aq = bk.best()
            if bp is None:
                a["dirty"] = 1
                return

            if m.get("type") != "snapshot":
                a["ofi"] += ofi_step(bk, bp, bq, ap, aq)
                a["ofi_n"] += 1
                _emit_large_events(s, bk, a, recv_ms, pre)
            bk.prev_bp, bk.prev_bq, bk.prev_ap, bk.prev_aq = bp, bq, ap, aq
            bk.sample_sizes()
            a["snap"] = snapshot_features(bk)

    elif topic.startswith("publicTrade"):
        with _lock:
            for d in m.get("data", []):
                s = d["s"]
                a = acc_for(s, sec)
                p, v = float(d["p"]), float(d["v"])
                n = p * v
                if d["S"] == "Buy":
                    a["buyN"] += n; a["buyq"] += v
                else:
                    a["sellN"] += n; a["sellq"] += v
                a["ntr"] += 1
                if a["px_first"] is None:
                    a["px_first"] = a["px_hi"] = a["px_lo"] = p
                a["px_last"] = p
                a["px_hi"] = max(a["px_hi"], p)
                a["px_lo"] = min(a["px_lo"], p)
                # absorption-сырьё: агрессия, ударившая в бид/аск
                bk = state["books"].get(s)
                if bk:
                    bk.last_trade_px, bk.last_trade_ts = p, recv_ms
                    if not bk.dirty:
                        bp, _, ap, _ = bk.best()
                        if bp is not None:
                            if p <= bp:
                                a["fill_at_bid"] += n
                            elif p >= ap:
                                a["fill_at_ask"] += n
                state["counters"]["trade_msg"] += 1


def _emit_large_events(s, bk, a, recv_ms, pre):
    """Постановка / снятие / исполнение КРУПНОЙ лимитки.

    Крупная = размер уровня выше p99 наблюдённого распределения размеров (адаптивно
    на символ, пересчёт раз в 60с). Абсолютный порог не годится: у BTC и ENJ разные масштабы.

    ⚠ ГРАНИЦА ДОСТОВЕРНОСТИ (L2, не L3): уровень — это СУММА заявок нескольких участников.
    Нельзя отличить (а) снятие одной крупной заявки от снятия нескольких мелких,
    (б) появление одной крупной от прихода десяти средних. Отличие снятия от исполнения
    делается эвристикой по недавнему принту на этой цене и НЕ является достоверным.
    Это статистика уровня, а не идентификация участника."""
    if pre is None or bk.thr is None:
        return
    thr = bk.thr
    bp, _, ap, _ = bk.best()
    if bp is None:
        return
    mid = (bp + ap) / 2
    for (side, p), prev_v in pre.items():
        book = bk.b if side == "b" else bk.a
        new_v = book.get(p, 0.0)
        d_v = new_v - prev_v
        if d_v >= thr:                       # ПОСТАНОВКА крупной
            a["add_big"] += 1
            a["add_bigN"] += p * d_v
            ev = "add"
        elif -d_v >= thr:                    # УБЫЛО крупно: снятие или исполнение
            traded_here = (bk.last_trade_px is not None
                           and abs(bk.last_trade_px - p) < 1e-12
                           and recv_ms - bk.last_trade_ts <= 200)
            ev = "fill" if traded_here else "cxl"
            a["cxl_big"] += 1
            a["cxl_bigN"] += p * (-d_v)
        else:
            continue
        emit("dom_events", {"ts": recv_ms, "s": s, "ev": ev, "side": side,
                            "p": p, "prev_v": round(prev_v, 6), "new_v": round(new_v, 6),
                            "notional": round(p * abs(d_v), 1), "thr": round(thr, 6),
                            "dist_bp": round((p - mid) / mid * 1e4, 2),
                            "schema": SCHEMA})
        state["counters"]["events"] += 1


# ──────────────────────────── вселенная / подписка ────────────────────────────
def pick_universe():
    if SYMS_ENV:
        return [x.strip().upper() for x in SYMS_ENV.split(",") if x.strip()]
    r = rest("tickers", {"category": "linear"})
    if not r or r.get("retCode") != 0:
        mani({"ev": "tickers_fail"})
        return state["syms"] or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    rows = [t for t in r["result"]["list"] if t.get("symbol", "").endswith("USDT")]
    rows.sort(key=lambda t: -float(t.get("turnover24h") or 0))
    return [t["symbol"] for t in rows[:TOP_N]]


def resubscribe():
    ws, syms = state.get("ws"), list(state["syms"])
    if not ws or not syms:
        return
    want = set(syms)
    add, rem = sorted(want - state["sub"]), sorted(state["sub"] - want)
    try:
        for i in range(0, len(rem), 10):
            ch = rem[i:i + 10]
            ws.send(json.dumps({"op": "unsubscribe",
                                "args": [f"orderbook.{DEPTH}.{s}" for s in ch] +
                                        [f"publicTrade.{s}" for s in ch]}))
        for i in range(0, len(add), 10):
            ch = add[i:i + 10]
            ws.send(json.dumps({"op": "subscribe",
                                "args": [f"orderbook.{DEPTH}.{s}" for s in ch] +
                                        [f"publicTrade.{s}" for s in ch]}))
            time.sleep(0.05)
        state["sub"] = want
        if add or rem:
            mani({"ev": "resub", "add": len(add), "rem": len(rem)})
    except Exception as e:
        mani({"ev": "resub_err", "err": str(e)[:200]})


def on_open(ws):
    state["ws"] = ws
    state["sub"] = set()
    with _lock:
        # реконнект = все книги невалидны, пока не придут свежие снапшоты
        for bk in state["books"].values():
            bk.dirty = True
            bk.b, bk.a, bk.u = {}, {}, -1
        state["counters"]["resync"] += 1
    mani({"ev": "ws_open", "depth": DEPTH, "n_syms": len(state["syms"])})
    resubscribe()


def on_close(ws, code, msg): mani({"ev": "ws_close", "code": code})
def on_error(ws, err): mani({"ev": "ws_error", "err": str(err)[:200]})


def flush_loop():
    """Досылает строки для символов, по которым перестали приходить сообщения."""
    while True:
        time.sleep(2)
        try:
            now_sec = int(time.time())
            with _lock:
                for s, a in list(state["acc"].items()):
                    if a["sec"] < now_sec - 1:
                        flush_row(s, a)
                        state["acc"].pop(s, None)
        except Exception as e:
            mani({"ev": "flush_err", "err": str(e)[:200]})


def universe_loop():
    while True:
        try:
            syms = pick_universe()
            if syms != state["syms"]:
                state["syms"] = syms
                resubscribe()
        except Exception as e:
            mani({"ev": "universe_err", "err": str(e)[:200]})
        time.sleep(3600)


def guard_loop():
    while True:
        try:
            free_gb = shutil.disk_usage(ROOT).free / 2**30
            if free_gb < 3:
                with _lock:
                    state["stop_write"] = True
                mani({"ev": "FATAL_disk", "free_gb": round(free_gb, 1)})
            mani({"ev": "hb", **state["counters"], "n_syms": len(state["syms"]),
                  "free_gb": round(free_gb, 1),
                  "dirty_books": sum(1 for b in state["books"].values() if b.dirty)})
        except Exception as e:
            mani({"ev": "guard_err", "err": str(e)[:200]})
        time.sleep(600)


def main():
    with _lock:
        state["day"] = day_str()
        _open_day(state["day"])
    state["syms"] = pick_universe()
    mani({"ev": "dom_v3_start", "schema": SCHEMA, "version": SCHEMA_VERSION,
          "code_sha256": code_sha256(), "pid": os.getpid(),
          "depth": DEPTH, "syms": state["syms"]})
    for fn in (flush_loop, universe_loop, guard_loop):
        threading.Thread(target=fn, daemon=True).start()
    while True:
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

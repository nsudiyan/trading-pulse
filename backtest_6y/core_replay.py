#!/usr/bin/env python3
"""Единое ядро шестилетнего honest-бэктеста (PREREG studies/2026-07-10_six_year_PREREG.md).

Чистые функции ИМПОРТИРУЮТСЯ из боевого vol_radar.py (sha256[:16]=9e112767a763fd2e):
detect_spike / select_hits / select_awakenings / is_stablecoin / is_commodity / MAJOR_SYMBOLS.
Здесь реплицируется ТОЛЬКО оркестрация run() (кулдауны, бюджет, каскад, капы) +
позиционный слой (входы/филлы/выходы/funding/дневной MTM), которого в бою нет.

Скан = одно закрытие 30м бара. Live-guard цены незакрытого бара аппроксимируется
open следующего бара (PREREG §9.3). Позиции: только доставленные ОДИНОЧНЫЕ и
ПРОБУЖДЕНИЯ (§9.2), equal-weight 1/5, max concurrent 5 (по market-книге; limit-книги
делят список допущенных сделок, отличаются только фактом филла и ценой входа).
"""
import sys, os, json, gzip, argparse
from datetime import datetime, timezone

sys.path.insert(0, os.path.expanduser("~/trading"))
from vol_radar import (detect_spike, select_hits, select_awakenings,      # боевые чистые функции
                       MAJOR_SYMBOLS, COOLDOWN_H, UNSENT_COOLDOWN_MIN,
                       CASCADE_COOLDOWN_H, SINGLES_PER_DAY, AWAKENING_PER_DAY)

BAR = 1800_000
DAY = 86400_000
MAX_POS = 5
CENSOR_GUARD = 25*3600_000       # входы стоп за 25ч до правой границы (PREREG §9.5)

def uday(ms): return datetime.fromtimestamp(ms/1000, timezone.utc).strftime("%Y-%m-%d")

# ── оркестрация одного скана (зеркало vol_radar.run, без I/O) ──────────────────
def sim_scan(T, uni, bars, cd, budget, counters, hit_log=None):
    """T = start ts закрытого бара; scan_now = T+BAR (момент закрытия).
    Возвращает список доставленных [(hit, kind)]; мутирует cd/budget/counters."""
    scan_now = T + BAR
    today = uday(scan_now)
    if budget.get("date") != today:                       # ролловер UTC-дня (как в бою)
        budget.update({"date": today, "singles": 0, "awakenings": 0,
                       "cascade_ts": budget.get("cascade_ts", 0)})
    hits = []
    for sym in uni:
        if cd.get(sym, 0) > scan_now: continue            # кулдаун-скип ДО детекта (боевой порядок)
        sb = bars.get(sym)
        if not sb: continue
        nxt = sb.get(T + BAR)
        if nxt is None:                                   # нет следующего бара — нет ни live-guard, ни входа
            counters["skip_no_next"] += 1; continue
        win = [sb.get(T - i*BAR) for i in range(10, -1, -1)]
        if any(w is None for w in win):
            counters["skip_gap"] += 1; continue
        kl = win + [[T + BAR, nxt[1], nxt[1], nxt[1], nxt[1], 0.0]]   # live-стаб: close=open следующего
        sp = detect_spike(kl)                             # ═ боевой детект ═
        if sp:
            hits.append({"symbol": sym, "vol_ratio": sp["vol_ratio"],
                         "price_chg_pct": sp["price_chg_pct"], "price": kl[-1][4]})
    counters["hits"] += len(hits)
    mode, chosen = select_hits(hits)                      # ═ боевой отбор ═
    delivered, sent = [], set()

    aw_room = max(0, AWAKENING_PER_DAY - budget["awakenings"])
    delivered_awk = set()
    for h in select_awakenings(hits)[:aw_room]:           # ═ боевой отбор пробуждений ═
        budget["awakenings"] += 1
        sent.add(h["symbol"]); delivered_awk.add(h["symbol"])
        delivered.append((h, "awakening"))
    chosen = [h for h in chosen if h["symbol"] not in delivered_awk]

    if mode == "cascade":
        if scan_now - budget["cascade_ts"] >= CASCADE_COOLDOWN_H*3600_000:
            budget["cascade_ts"] = scan_now
            sent |= {h["symbol"] for h in chosen}         # дайджест: кулдауны да, позиций нет (§9.2)
            counters["cascades"] += 1
        else:
            counters["cascades_muted"] += 1
    else:
        room = max(0, SINGLES_PER_DAY - budget["singles"])
        for h in chosen[:room]:
            budget["singles"] += 1
            sent.add(h["symbol"])
            delivered.append((h, "single"))
        counters["singles_capped"] += max(0, len(chosen) - room)

    for h in hits:                                        # кулдауны на ВСЕ хиты (боевая строка 334)
        cd[h["symbol"]] = scan_now + (COOLDOWN_H*3600_000 if h["symbol"] in sent
                                      else UNSENT_COOLDOWN_MIN*60_000)
    if hit_log is not None and hits:                      # инструментовка parity: состав, БЕЗ доходностей
        hit_log.append({"T": T, "mode": mode,
                        "hits": [(h["symbol"], h["vol_ratio"]) for h in hits],
                        "sent": sorted(sent)})
    return delivered

# ── позиционный слой ────────────────────────────────────────────────────────────
def find_exit(sb, entry_start, t_end):
    """close первого бара с start >= entry+24ч; при дырах ищем до +48ч, иначе цензура."""
    tgt = entry_start + DAY
    t = tgt
    while t <= min(tgt + 2*DAY, t_end):
        b = sb.get(t)
        if b: return t, b[4], False
        t += BAR
    last, lb = None, None
    t = tgt - BAR
    while t > entry_start:
        b = sb.get(t)
        if b: last, lb = t, b[4]; break
        t -= BAR
    return last, lb, True                                  # цензура (делистинг/край данных)

def funding_cost_pct(fr, entry_ms, exit_ms):
    """Лонг платит положительный funding: cost% = +rate*100 за каждую 8ч-метку в (entry, exit]."""
    return sum(r for ts, r in fr if entry_ms < ts <= exit_ms) * 100.0

def run_engine(bars, fund, uni_by_day, t0, t1, log=lambda *a: None, hit_log=None,
               state=None, censor_end=None):
    """bars: {sym:{start_ts:[ts,o,h,l,c,vol,...]}}, fund: {sym:[(ts,rate)...]},
    uni_by_day: {'YYYY-MM-DD': [syms по убыванию turnover]}. Возвращает (trades, counters).
    state: перенос cd/budget/open_ex между кусками сквозного прогона (мутируется на месте);
    censor_end: глобальная правая граница для хвостовой цензуры (по умолчанию t1)."""
    st = state if state is not None else {}
    cd = st.setdefault("cd", {})
    budget = st.setdefault("budget", {"date": None, "singles": 0, "awakenings": 0, "cascade_ts": 0})
    open_ex = st.setdefault("open_ex", [])                 # exit_close_ms открытых позиций (market-книга)
    censor_end = censor_end or t1
    counters = dict(scans=0, hits=0, cascades=0, cascades_muted=0, singles_capped=0,
                    skip_gap=0, skip_no_next=0, skipped_full=0, censored=0,
                    delivered_singles=0, delivered_awakenings=0)
    trades = []
    T = t0
    while T + BAR <= t1:
        uni = uni_by_day.get(uday(T + BAR), [])
        if uni:
            counters["scans"] += 1
            for h, kind in sim_scan(T, uni, bars, cd, budget, counters, hit_log):
                counters[f"delivered_{kind}s"] += 1
                scan_now = T + BAR
                if scan_now > censor_end - CENSOR_GUARD: continue  # хвостовая цензура входов
                open_ex[:] = [e for e in open_ex if e > scan_now]
                if len(open_ex) >= MAX_POS:
                    counters["skipped_full"] += 1; continue
                sym = h["symbol"]; sb = bars[sym]
                entry_start = T + BAR                      # вход market по open следующего бара
                eb = sb[entry_start]
                mkt_px, lim_px = eb[1], sb[T][4]           # limit = close сигнального бара
                exit_start, exit_px, cens = find_exit(sb, entry_start, t1)
                if exit_px is None: continue
                if cens: counters["censored"] += 1
                exit_close_ms = exit_start + BAR
                open_ex.append(exit_close_ms)
                fpct = funding_cost_pct(fund.get(sym, ()), entry_start, exit_close_ms)
                gross_mkt = (exit_px/mkt_px - 1)*100
                opt_fill  = eb[3] <= lim_px                # касание low в баре входа
                cons_fill = eb[3] <= lim_px*0.9995         # пробитие
                trades.append({
                    "ts_utc": datetime.fromtimestamp((T+BAR)/1000, timezone.utc).strftime("%Y-%m-%dT%H:%M"),
                    "signal_bar": T, "symbol": sym, "kind": kind, "vol_ratio": h["vol_ratio"],
                    "entry_ts": entry_start, "mkt_entry": mkt_px, "entry_close": eb[4],
                    "lim_price": lim_px,
                    "opt_fill": int(opt_fill), "cons_fill": int(cons_fill),
                    "exit_ts": exit_close_ms, "exit_px": exit_px, "censored": int(cens),
                    "gross_mkt_pct": round(gross_mkt, 4),
                    "gross_opt_pct": round((exit_px/lim_px - 1)*100, 4) if opt_fill else None,
                    "gross_cons_pct": round((exit_px/lim_px - 1)*100, 4) if cons_fill else None,
                    "funding_pct": round(fpct, 4),
                })
        T += BAR
        if T % (30*DAY) < BAR: log(f"  …{uday(T)} trades={len(trades)}")
    return trades, counters

def daily_series(trades, bars, cost_rt=0.31):
    """Календарный дневной портфельный PnL, %: MTM по закрытиям суток, вес 1/MAX_POS,
    издержки+funding списываются в день выхода. Некомпаундированная сумма весов."""
    def px_at(sb, ms):
        t = ms - BAR
        for _ in range(96):
            b = sb.get(t)
            if b: return b[4]
            t -= BAR
        return None
    daily = {}
    w = 1.0/MAX_POS
    for tr in trades:
        if tr["censored"]: continue
        sb = bars[tr["symbol"]]
        prev_px, prev_ms = tr["mkt_entry"], tr["entry_ts"]
        m0 = (tr["entry_ts"]//DAY + 1)*DAY
        marks = list(range(m0, tr["exit_ts"], DAY)) + [tr["exit_ts"]]
        for i, m in enumerate(marks):
            px = tr["exit_px"] if m == tr["exit_ts"] else px_at(sb, m)
            if px is None: continue
            d = uday(m - 1)
            daily[d] = daily.get(d, 0.0) + w*(px/prev_px - 1)*100
            prev_px, prev_ms = px, m
        d_exit = uday(tr["exit_ts"] - 1)
        daily[d_exit] = daily.get(d_exit, 0.0) - w*(cost_rt + tr["funding_pct"])
    return dict(sorted(daily.items()))

# ── I/O обвязка ────────────────────────────────────────────────────────────────
ROOT = os.path.expanduser("~/trading/backtest_6y")
def load_bars(sym):
    p = f"{ROOT}/data/klines/{sym}.json.gz"
    if not os.path.exists(p): return None
    return {int(k): v for k, v in json.load(gzip.open(p, "rt")).items()}
def load_fund(sym):
    p = f"{ROOT}/data/funding/{sym}.json.gz"
    if not os.path.exists(p): return []
    return sorted((int(k), v) for k, v in json.load(gzip.open(p, "rt")).items())

# ── selfcheck: фикстуры оркестрации и позиционного слоя ────────────────────────
def _mk(ts, o, h, l, c, v): return [ts, o, h, l, c, v]
def _flat(sym_bars, t0, n, px=100.0, vol=50.0):
    for i in range(n): sym_bars[t0+i*BAR] = _mk(t0+i*BAR, px, px+0.2, px-0.2, px, vol)

def selfcheck():
    t0 = 1600000000000 - (1600000000000 % DAY)             # ровная UTC-полночь
    # A: одиночный спайк на альте → доставка, позиция, кулдаун 4ч
    A = {}; _flat(A, t0, 11)
    sig = t0+11*BAR
    A[sig] = _mk(sig, 100, 100.5, 99.6, 100.3, 400)        # 8× объём, цена стоит
    for i in range(12, 120): A[t0+i*BAR] = _mk(t0+i*BAR, 100.3, 106, 100.2, 105, 60)
    bars = {"AAAUSDT": A}
    uni = {uday(t0+i*BAR): ["AAAUSDT"] for i in range(0, 130)}
    tr, c = run_engine(bars, {}, uni, t0, t0+120*BAR)
    assert c["delivered_singles"] == 1 and len(tr) == 1, (c, tr)
    assert tr[0]["mkt_entry"] == 100.3 and abs(tr[0]["gross_mkt_pct"] - (105/100.3-1)*100) < 1e-3
    exp_exit = sig+BAR+DAY                                  # первый бар ≥ вход+24ч
    assert tr[0]["exit_ts"] == exp_exit + BAR, tr[0]
    # B: мажор-одиночка НЕ доставляется; кулдаун unsent 30м
    B = dict(A); bars2 = {"BTCUSDT": B}
    tr2, c2 = run_engine(bars2, {}, {d: ["BTCUSDT"] for d in uni}, t0, t0+120*BAR)
    assert c2["delivered_singles"] == 0 and c2["hits"] >= 1, c2
    # C: каскад ≥3 → дайджест без позиций; повтор в 2ч подавлен
    bars3 = {}
    for s in ("AUSDT", "BUSDT", "CUSDT"):
        d = {}; _flat(d, t0, 11); d[sig] = _mk(sig, 100, 100.5, 99.6, 100.3, 400)
        d[sig+BAR] = _mk(sig+BAR, 100.3, 100.6, 100.1, 100.4, 300)   # 2й спайк-бар подряд
        for i in range(13, 120): d[t0+i*BAR] = _mk(t0+i*BAR, 100.4, 100.8, 100.1, 100.4, 60)
        bars3[s] = d
    tr3, c3 = run_engine(bars3, {}, {d_: ["AUSDT","BUSDT","CUSDT"] for d_ in uni}, t0, t0+120*BAR)
    assert c3["cascades"] == 1 and len(tr3) == 0, (c3, len(tr3))
    # D: пробуждение vr≥15 → позиция даже при каскаде; мини-кап 3/день
    bars4 = {}
    for s in ("AUSDT", "BUSDT", "CUSDT", "DUSDT"):
        d = {}; _flat(d, t0, 11)
        d[sig] = _mk(sig, 100, 100.5, 99.6, 100.3, 1000)   # 20× = пробуждение
        for i in range(12, 120): d[t0+i*BAR] = _mk(t0+i*BAR, 100.3, 101, 100, 100.5, 60)
        bars4[s] = d
    tr4, c4 = run_engine(bars4, {}, {d_: ["AUSDT","BUSDT","CUSDT","DUSDT"] for d_ in uni}, t0, t0+120*BAR)
    assert c4["delivered_awakenings"] == 3, c4              # 4-е — сверх мини-капа
    assert c4["cascades"] == 1, c4                          # остаток ушёл дайджестом
    # E: live-guard — цена улетела на open следующего бара → хита нет
    E = {}; _flat(E, t0, 11)
    E[sig] = _mk(sig, 100, 100.5, 99.6, 100.3, 400)
    E[sig+BAR] = _mk(sig+BAR, 103, 104, 102.5, 103.5, 60)   # open +2.7% — опоздали
    for i in range(13, 120): E[t0+i*BAR] = _mk(t0+i*BAR, 103, 104, 102, 103, 60)
    tr5, c5 = run_engine({"AAAUSDT": E}, {}, uni, t0, t0+120*BAR)
    assert c5["hits"] == 0, c5
    # F: funding уменьшает net; филлы: opt по касанию, cons по пробитию
    fund = {"AAAUSDT": [(sig+BAR+8*3600_000, 0.01)]}        # 1% за период — утрирован для теста
    tr6, _ = run_engine(bars, fund, uni, t0, t0+120*BAR)
    assert abs(tr6[0]["funding_pct"] - 1.0) < 1e-9, tr6[0]
    eb_low = bars["AAAUSDT"][sig+BAR][3]                    # low=100.2 ≤ lim=100.3 → opt филл
    assert tr6[0]["opt_fill"] == 1 and eb_low <= tr6[0]["lim_price"]
    assert tr6[0]["cons_fill"] == 1                         # 100.2 ≤ 100.3×0.9995=100.25
    # G: дневной MTM — сумма дневных = сделке минус кост
    ds = daily_series(tr, bars, cost_rt=0.31)
    assert abs(sum(ds.values()) - (tr[0]["gross_mkt_pct"] - 0.31 - 0)/MAX_POS) < 1e-3, ds
    print("✓ core selfcheck: 7 фикстур прошли (single/major/cascade/awakening-кап/"
          "live-guard/funding+филлы/дневной MTM)")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck: selfcheck()
    else: print("используется как модуль (smoke/parity/full — отдельные раннеры)")

#!/usr/bin/env python3
"""
BOS_FVG-SHORT FORWARD HARNESS (2026-05-31)
==========================================
Форвард-валидация второго слабо-живого кандидата: ШОРТ-сигналы сетапа bos_fvg
из СКРИНЕРА (outcomes/resolved.csv).

Контекст (см. memory project_3day_postmortem_2026-05-30):
- На 3-дневном пост-мортеме bos_fvg-шорты показали БОЛЬШЕ всего селекшн-альфы среди
  шорт-сетапов: beta-neutral net +0.32%/4ч, +1.72%/24ч (vs swing-шорт ОТРИЦАТЕЛЬНЫЙ).
- НО n=13, одно окно, рынок падал → ГИПОТЕЗА, не эдж. Этот скрипт копит честную выборку
  и считает по ДВУМ честным метрикам сразу:
    1) path-resolved R (order-aware: что тронули раньше — стоп или тейк; убирает both-hit→TP look-ahead);
    2) beta-neutral net (шорт альта + лонг равного $ BTC за то же окно; убирает «рынок просто падал»).

Правило: вход SHORT по price_entry, стоп = поле stop, тейк = tp1, горизонт 24ч.
Издержки: fee 0.055%/side + slippage. Beta-neutral = 2 ноги (двойные издержки).

READ-ONLY: читает outcomes/resolved.csv + BTC klines с Bybit. Ничего не пишет (кроме опц. --tg).
Запуск:  python3 bos_fvg_short_forward.py [--since YYYY-MM-DD] [--lookback-days 60] [--slip 0.10] [--tg]
Решение по эджу — ТОЛЬКО при forward n>=TARGET_N И положительном BETA-NEUTRAL net.
"""
import csv, sys, time, json, urllib.request, statistics as st
from datetime import datetime, timezone
import os

HERE       = os.path.dirname(os.path.abspath(__file__))
CSV_PATH   = os.path.join(HERE, "outcomes", "resolved.csv")
GATE_LIVE  = "2026-05-31"   # начало честного форварда (день постановки гипотезы)
TARGET_N   = 50             # порог статзначимости
TAKER_PCT  = 0.055          # Bybit taker за сторону, %
HOLD_H     = 24.0
SETUP      = "bos_fvg"
BASE       = "https://api.bybit.com"

def _f(x):
    try: return float(x) if x not in (None, "") else None
    except Exception: return None

def _iso(s):
    if not s: return 0.0
    for fmt, cut in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try: return datetime.strptime(s[:cut], fmt).replace(tzinfo=timezone.utc).timestamp()
        except Exception: continue
    return 0.0

def _is_short(d): return "ШОРТ" in (d or "") or "SHORT" in (d or "").upper()

# ── BTC klines (для beta-neutral) ──
def _fetch_btc_hourly(t0, t1):
    """dict hour_ts->close, частями по 1000 баров."""
    out = {}
    cur = int(t0)
    while cur < t1 + 3600:
        try:
            q = f"category=linear&symbol=BTCUSDT&interval=60&start={cur*1000}&limit=1000"
            with urllib.request.urlopen(f"{BASE}/v5/market/kline?{q}", timeout=20) as r:
                lst = json.load(r)["result"]["list"]
        except Exception as e:
            print(f"[warn] BTC fetch failed @ {cur}: {e}"); break
        if not lst: break
        for c in lst:
            out[int(c[0]) // 1000 // 3600 * 3600] = float(c[4])
        newest = max(int(c[0]) // 1000 for c in lst)
        if newest <= cur: break
        cur = newest + 3600
    return out

def _btc_at(btc, ts):
    h = int(ts) // 3600 * 3600
    for d in range(0, 8):
        if h + d * 3600 in btc: return btc[h + d * 3600]
        if h - d * 3600 in btc: return btc[h - d * 3600]
    return None

def _btc_ret(btc, ts, hours):
    a = _btc_at(btc, ts); b = _btc_at(btc, ts + hours * 3600)
    return (b - a) / a * 100 if (a and b) else None

# ── path-resolved R (order-aware) ──
def path_R(r, slip_side):
    e = _f(r.get("price_entry")); stop = _f(r.get("stop")); tp = _f(r.get("tp1"))
    mfe = _f(r.get("mfe_24h_pct")); mae = _f(r.get("mae_24h_pct"))
    tmfe = _f(r.get("time_to_mfe_24h_h")); tmae = _f(r.get("time_to_mae_24h_h"))
    ch = _f(r.get("change_24h_pct"))
    if None in (e, stop, tp) or e == 0: return None
    sd = abs(stop - e) / e * 100; td = abs(tp - e) / e * 100
    if sd == 0: return None
    cost_R = (2 * TAKER_PCT + 2 * slip_side) / sd
    th = mfe is not None and mfe >= td
    sh = mae is not None and mae >= sd
    if sh and (not th or (tmae is not None and tmfe is not None and tmae < tmfe)):
        return -1.0 - cost_R
    if th and (not sh or (tmfe is not None and tmae is not None and tmfe < tmae)):
        return td / sd - cost_R
    if ch is None: return -cost_R
    return (-ch) / sd - cost_R   # шорт: прибыль = -change

def main():
    a = sys.argv[1:]
    slip = float(a[a.index("--slip") + 1]) if "--slip" in a else 0.10
    lookback = int(a[a.index("--lookback-days") + 1]) if "--lookback-days" in a else 60
    since = a[a.index("--since") + 1] if "--since" in a else None
    want_tg = "--tg" in a

    if not os.path.exists(CSV_PATH):
        print(f"НЕТ ФАЙЛА: {CSV_PATH}"); sys.exit(1)
    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))

    now = time.time()
    cut_look = now - lookback * 86400
    gate_ts = _iso(GATE_LIVE if not since else since)

    # фильтр: bos_fvg SHORT, в окне lookback; дедуп по symbol (первое появление)
    seen = set(); rec = []
    for r in sorted(rows, key=lambda x: _iso(x.get("run_ts", ""))):
        if r.get("setup", "") != SETUP or not _is_short(r.get("direction", "")):
            continue
        t = _iso(r.get("run_ts", ""))
        if t < cut_look: continue
        k = r.get("symbol", "")
        if k in seen: continue
        seen.add(k); r["_ts"] = t; rec.append(r)

    if not rec:
        print(f"Нет bos_fvg-шортов за последние {lookback} дней."); return

    t0 = min(r["_ts"] for r in rec); t1 = max(r["_ts"] for r in rec)
    btc = _fetch_btc_hourly(t0 - 7200, t1 + HOLD_H * 3600 + 7200)

    def stats(subset):
        Rs, BNs, ALs = [], [], []
        cost_bn = 2 * (2 * TAKER_PCT + 2 * slip) / 100  # доля, грубо в %-единицах P&L
        for r in subset:
            R = path_R(r, slip)
            if R is not None: Rs.append(R)
            alt = _f(r.get("change_24h_pct"))
            b = _btc_ret(btc, r["_ts"], HOLD_H)
            if alt is not None and b is not None:
                al = b - alt                     # gross alpha (шорт альта vs шорт BTC)
                ALs.append(al)
                BNs.append(al - (4 * TAKER_PCT + 4 * slip))  # 2 ноги издержек, в %
        out = {"n": len(subset)}
        if Rs:
            out["meanR"] = st.mean(Rs); out["medR"] = st.median(Rs)
            out["winR"] = 100 * sum(1 for x in Rs if x > 0) / len(Rs)
        if ALs:
            out["alpha_gross"] = st.mean(ALs)
            out["bn_net"] = st.mean(BNs)
            out["bn_win"] = 100 * sum(1 for x in BNs if x > 0) / len(BNs)
        return out

    hist = [r for r in rec if r["_ts"] < gate_ts]
    fwd  = [r for r in rec if r["_ts"] >= gate_ts]

    L = []
    L.append("=" * 72)
    L.append(f"BOS_FVG-SHORT FORWARD | {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    L.append(f"окно lookback={lookback}д, стоп=поле stop, hold={HOLD_H:.0f}ч, taker={TAKER_PCT}%/side, slip={slip}%/side")
    L.append(f"bos_fvg-шортов в окне: {len(rec)} | до гейта {GATE_LIVE}: {len(hist)} | ФОРВАРД: {len(fwd)}")
    L.append("=" * 72)
    btc_span = None
    if btc:
        hs = sorted(btc); btc_span = (btc[hs[-1]] - btc[hs[0]]) / btc[hs[0]] * 100
        L.append(f"BTC за окно данных: {btc_span:+.1f}% (контекст беты)")

    for label, sub in (("HISTORICAL (lookback)", hist), (f"ФОРВАРД (после {GATE_LIVE})", fwd)):
        s = stats(sub)
        L.append(f"\n{label}  n={s['n']}")
        if "meanR" in s:
            L.append(f"  path-resolved R:   mean={s['meanR']:+.2f}R  median={s['medR']:+.2f}R  win={s['winR']:.0f}%")
        if "bn_net" in s:
            L.append(f"  alpha gross (vs BTC): {s['alpha_gross']:+.2f}%   (>0 = альт падает сильнее рынка)")
            L.append(f"  BETA-NEUTRAL net (2 ноги): {s['bn_net']:+.2f}%  win={s['bn_win']:.0f}%  ← ЧЕСТНЫЙ ЭДЖ")

    # вердикт по форварду
    fs = stats(fwd)
    L.append("\n" + "-" * 72)
    if fs["n"] < TARGET_N:
        L.append(f"ВЕРДИКТ: NEED MORE — форвард n={fs['n']}/{TARGET_N}. Копим. Капитал НЕ ставим.")
    elif fs.get("bn_net", -99) > 0 and fs.get("meanR", -99) > 0:
        L.append(f"ВЕРДИКТ: EDGE-кандидат ПОДТВЕРЖДАЕТСЯ (n={fs['n']}, R={fs.get('meanR'):+.2f}, "
                 f"beta-neutral={fs.get('bn_net'):+.2f}%). Малый live-сайзинг с тем же стопом.")
    else:
        L.append(f"ВЕРДИКТ: REJECT — n={fs['n']}, но честный эдж неположителен "
                 f"(R={fs.get('meanR')}, BN={fs.get('bn_net')}). Гипотеза не подтвердилась.")
    L.append("-" * 72)

    rep = "\n".join(L); print(rep)
    if want_tg:
        try:
            import telegram_alerts as _ta
            cfg = _ta.load_config(); tok, chat = cfg.get("bot_token"), str(cfg.get("chat_id") or "")
            if tok and chat:
                _ta._send(tok, chat, "<b>BOS_FVG-SHORT forward</b>\n<pre>" + rep[-3500:] + "</pre>")
                print("\n[TG] отправлено")
        except Exception as e:
            print(f"\n[TG] не отправлен: {e}")

if __name__ == "__main__":
    main()

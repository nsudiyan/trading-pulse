#!/usr/bin/env python3
"""
trend/engine.py — Donchian Trend Breakout, PAPER-форвард. READ-ONLY к бирже (Bybit V5 public,
без ключей, реальных ордеров НЕТ). Накапливает бумажный трек для kill-метрики ДО капитала.

СПЕК (из верифицированного аудита 2026-06-17; дневной Donchian +0.50R/сделку, кластер-CI>0):
- вселенная: ликвидные USDT-перпы (ниже UNIVERSE)
- вход: close пробил N_BREAK-дневный макс/мин И в направлении TREND_MA И объём>VOL_X×avg(20)
- стоп: ATR_STOP×ATR(ATR_N)
- выход: трейл до N_EXIT-дневного обратного канала Donchian ИЛИ стоп
- риск: R_PCT экв./сделку (vol-target через ATR-сайзинг), плечо ≤1x, шорты вкл.
БЕЗ LOOK-AHEAD: торгуем только ЗАКРЫТЫЕ дневные бары; вход по close сигнального бара,
выход проверяется со СЛЕДУЮЩЕГО бара.

=== KILL-МЕТРИКА (ПРЕДЗАПИСАНА, не двигать) ===
После ≥4 недель И ≥20 сделок проект СТОП, если ЛЮБОЕ:
  (1) реализованная экспектанси ≤ 0R, ИЛИ
  (2) Sharpe бумажной equity < 0, ИЛИ
  (3) maxDD бумажной equity пробил −50%.
Любое → деньги НЕ заводим. Иначе — кандидат на малый капитал (1-2%).

Запуск:
  python3 trend/engine.py            # обработать новые закрытые бары, обновить paper, отчёт
  python3 trend/engine.py status     # текущие позиции + сигналы без обработки
  python3 trend/engine.py killcheck  # проверка kill-метрики
  python3 trend/engine.py selftest   # self-check логики (без сети)
"""
from __future__ import annotations
import urllib.request, json, csv, os, sys, math
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
TRADES = os.path.join(HERE, "trades.csv")
EQUITY = os.path.join(HERE, "equity.csv")
LOG = os.path.join(HERE, "engine.log")

UNIVERSE = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","DOGEUSDT","ADAUSDT",
            "LINKUSDT","AVAXUSDT","LTCUSDT","DOTUSDT","NEARUSDT","UNIUSDT","XLMUSDT","SUIUSDT"]
N_BREAK   = 20      # пробойный канал
N_EXIT    = 10      # обратный канал выхода
TREND_MA  = 100     # фильтр тренда
ATR_N     = 20
ATR_STOP  = 2.0
VOL_X     = 1.3     # объёмное подтверждение пробоя
R_PCT     = 0.005   # риск экв. на сделку
FEE       = 0.00055 # тейкер/сторона
SLIP      = 0.0005  # проскальзывание/сторона
COST_RT_FRAC = (FEE + SLIP) * 2   # round-trip в долях цены

def log(msg):
    line = f"[{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%S}] {msg}"
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as f: f.write(line + "\n")
    except Exception: pass

# ── data ──
def fetch_daily(sym, limit=1000):
    u = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}&interval=D&limit={limit}"
    req = urllib.request.Request(u, headers={"User-Agent": "trend/1.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        d = json.load(r).get("result", {}).get("list", [])
    bars = sorted(([int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])] for x in d),
                  key=lambda b: b[0])  # ts,o,h,l,c,v (ascending)
    # только ЗАКРЫТЫЕ дни (отбрасываем сегодняшний формирующийся бар)
    today = datetime.now(timezone.utc).date()
    return [b for b in bars if datetime.fromtimestamp(b[0]/1000, timezone.utc).date() < today]

# ── indicators (всё по барам ≤ i, без look-ahead) ──
def atr(bars, i, n=ATR_N):
    if i < n: return None
    s = 0.0
    for j in range(i-n+1, i+1):
        s += max(bars[j][2]-bars[j][3], abs(bars[j][2]-bars[j-1][4]), abs(bars[j][3]-bars[j-1][4]))
    return s / n

def signal(bars, i):
    """Вернёт ('L'/'S'/None, stop) по сигнальному бару i (close известен)."""
    if i < max(N_BREAK, TREND_MA, ATR_N) + 1: return None, None
    o, h, l, c, v = bars[i][1], bars[i][2], bars[i][3], bars[i][4], bars[i][5]
    hh = max(b[2] for b in bars[i-N_BREAK:i]); ll = min(b[3] for b in bars[i-N_BREAK:i])
    ma = sum(b[4] for b in bars[i-TREND_MA:i]) / TREND_MA
    a = atr(bars, i)
    if a is None or a <= 0: return None, None
    volok = v > VOL_X * (sum(b[5] for b in bars[i-20:i]) / 20)
    if c > hh and c > ma and volok: return "L", c - ATR_STOP*a
    if c < ll and c < ma and volok: return "S", c + ATR_STOP*a
    return None, None

def check_exit(bars, i, pos):
    """Выход на баре i для уже открытой позиции pos. Вернёт (exit_price, reason) или (None,None)."""
    d, entry, stop = pos["dir"], pos["entry"], pos["stop"]
    o, h, l, c = bars[i][1], bars[i][2], bars[i][3], bars[i][4]
    exit_lo = min(b[3] for b in bars[i-N_EXIT:i]); exit_hi = max(b[2] for b in bars[i-N_EXIT:i])
    if d == "L":
        if l <= stop: return (min(stop, o), "stop")    # гэп вниз через стоп → честный филл по open
        if c < exit_lo: return c, "exit_channel"
    else:
        if h >= stop: return (max(stop, o), "stop")    # гэп вверх через стоп → честный филл по open
        if c > exit_hi: return c, "exit_channel"
    return None, None

# ── state ──
def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f: return json.load(f)
    return {"positions": {}, "last_date": {}, "started": None}

def save_state(s):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f: json.dump(s, f, indent=1, ensure_ascii=False)
    os.replace(tmp, STATE)

def append_trade(row):
    key = (str(row[0]), str(row[1]), str(row[3]))   # close_ts|symbol|entry — дедуп от краша/гонки
    if os.path.exists(TRADES):
        with open(TRADES) as f:
            for r in csv.reader(f):
                if len(r) >= 4 and (r[0], r[1], r[3]) == key:
                    return  # уже записана
    new = not os.path.exists(TRADES)
    with open(TRADES, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new: w.writerow(["close_ts","symbol","dir","entry","exit","stop","gross_R","net_R","reason","bars_held"])
        w.writerow(row)

def bar_date(b): return datetime.fromtimestamp(b[0]/1000, timezone.utc).strftime("%Y-%m-%d")

# ── core: обработать новые закрытые бары (paper forward) ──
def process():
    # файл-лок (non-blocking): не даём параллельным прогонам дублировать сделки
    _lock = open(os.path.join(HERE, ".lock"), "w")
    try:
        import fcntl
        fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        log("другой прогон уже идёт — выход"); return
    s = load_state()
    if s["started"] is None:
        s["started"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new_entries, new_exits = [], []
    for sym in UNIVERSE:
        try:
            bars = fetch_daily(sym)
        except Exception as e:
            log(f"{sym}: fetch error {e}"); continue
        if len(bars) < max(N_BREAK, TREND_MA, ATR_N) + 5: continue
        last = s["last_date"].get(sym)
        # на ПЕРВОМ прогоне стартуем с последнего закрытого бара (paper начинается сейчас, flat)
        start_idx = len(bars) - 1
        if last is not None:
            idxs = [k for k, b in enumerate(bars) if bar_date(b) > last]
            start_idx = idxs[0] if idxs else len(bars)
        for i in range(start_idx, len(bars)):
            pos = s["positions"].get(sym)
            # 1) выход (только для позиции, открытой на баре < i)
            if pos and pos["entry_idx_date"] < bar_date(bars[i]):
                ex, reason = check_exit(bars, i, pos)
                if ex is not None:
                    risk = abs(pos["entry"] - pos["stop"])
                    grossR = ((ex - pos["entry"]) if pos["dir"] == "L" else (pos["entry"] - ex)) / risk
                    netR = grossR - COST_RT_FRAC / (risk / pos["entry"])
                    append_trade([bar_date(bars[i]), sym, pos["dir"], round(pos["entry"],6), round(ex,6),
                                  round(pos["stop"],6), round(grossR,3), round(netR,3), reason,
                                  i - pos["entry_idx"]])
                    new_exits.append((sym, pos["dir"], reason, round(netR,3)))
                    s["positions"].pop(sym, None); pos = None
            # 2) вход (если flat)
            if not s["positions"].get(sym):
                d, stop = signal(bars, i)
                if d:
                    s["positions"][sym] = {"dir": d, "entry": bars[i][4], "stop": stop,
                                           "entry_idx": i, "entry_idx_date": bar_date(bars[i])}
                    new_entries.append((sym, d, round(bars[i][4],6), round(stop,6)))
            s["last_date"][sym] = bar_date(bars[i])
    save_state(s)
    _notify(new_entries, new_exits)
    _report(s, new_entries, new_exits)

def _notify(entries, exits):
    """Telegram-алерты входов (с кнопками [Вошёл/Пропустил]) и выходов. Не критично — не валит движок."""
    if not entries and not exits:
        return
    try:
        root = os.path.dirname(HERE)
        if root not in sys.path:
            sys.path.insert(0, root)
        from telegram_alerts import send_trend_alert, send_trend_exit, load_config
        cfg = load_config()
        for sym, d, entry, stop in entries:
            send_trend_alert({"symbol": sym, "dir": d, "entry": entry, "stop": stop}, cfg)
        for sym, d, reason, netR in exits:
            send_trend_exit({"symbol": sym, "dir": d, "reason": reason, "netR": netR}, cfg)
    except Exception as e:
        log(f"TG notify error (не критично): {e}")

def _trades_rows():
    if not os.path.exists(TRADES): return []
    with open(TRADES) as f:
        return sorted((r for r in csv.DictReader(f)), key=lambda r: r["close_ts"])

def _equity_curve():
    rows = _trades_rows()
    if not rows: return [], []
    eq = 1.0; curve = [1.0]; rets = []
    for r in rows:
        rt = float(r["net_R"]) * R_PCT
        eq *= (1 + rt); curve.append(eq); rets.append(rt)
    return curve, rets

def killcheck():
    curve, rets = _equity_curve()
    n = len(rets)
    dates = sorted(r["close_ts"] for r in _trades_rows())
    s = load_state()
    # недели от ПЕРВОЙ сделки (не от старта демона) — синхронно с накопленными сделками
    if dates:
        first = datetime.strptime(dates[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    elif s.get("started"):
        first = datetime.strptime(s["started"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        first = datetime.now(timezone.utc)
    weeks = (datetime.now(timezone.utc) - first).days / 7
    print(f"=== KILL-CHECK ===  сделок={n}  недель≈{weeks:.1f}")
    if n < 20 or weeks < 4:
        print(f"  Рано судить (нужно ≥20 сделок И ≥4 недель). Накапливаем."); return
    exp = sum(float(x) for x in rets) / n / R_PCT  # средний net-R
    mean = sum(rets)/n; sd = (sum((x-mean)**2 for x in rets)/n) ** 0.5
    # annualize по реальной частоте сделок (трендовые сделки редки, не ежедневные)
    span_yr = max((datetime.strptime(dates[-1],"%Y-%m-%d") - datetime.strptime(dates[0],"%Y-%m-%d")).days/365, 0.1)
    tpy = n / span_yr
    sharpe = (mean/sd*math.sqrt(tpy)) if sd > 0 else 0.0
    peak = 1.0; mdd = 0.0
    for e in curve: peak = max(peak, e); mdd = min(mdd, e/peak - 1)
    fails = []
    if exp <= 0: fails.append(f"экспектанси {exp:+.3f}R ≤ 0")
    if sharpe < 0: fails.append(f"Sharpe {sharpe:+.2f} < 0")
    if mdd < -0.50: fails.append(f"maxDD {mdd*100:.0f}% < −50%")
    print(f"  экспектанси={exp:+.3f}R  Sharpe≈{sharpe:+.2f}  maxDD={mdd*100:.0f}%  equity={curve[-1]:.3f}x")
    print("  ВЕРДИКТ:", "🔴 KILL — " + "; ".join(fails) if fails else "🟢 ЖИВ — kill-условия не сработали")

def _report(s, entries, exits):
    log(f"paper-обновление: +{len(entries)} входов, {len(exits)} выходов")
    if entries:
        print("  НОВЫЕ ВХОДЫ:")
        for sym, d, e, st in entries: print(f"    {sym} {d} @ {e}  stop {st}")
    if exits:
        print("  ВЫХОДЫ:")
        for sym, d, r, nr in exits: print(f"    {sym} {d} {r} netR={nr:+.2f}")
    pos = s["positions"]
    print(f"  ОТКРЫТО позиций: {len(pos)}")
    for sym, p in pos.items(): print(f"    {sym} {p['dir']} entry {round(p['entry'],6)} stop {round(p['stop'],6)} (с {p['entry_idx_date']})")
    curve, rets = _equity_curve()
    if rets:
        exp = sum(rets)/len(rets)/R_PCT
        print(f"  PAPER: сделок={len(rets)} экспектанси={exp:+.3f}R equity={curve[-1]:.3f}x")

def status():
    s = load_state()
    print(f"started: {s.get('started')}  открыто: {len(s.get('positions',{}))}")
    for sym, p in s.get("positions", {}).items():
        print(f"  {sym} {p['dir']} entry {round(p['entry'],6)} stop {round(p['stop'],6)} (с {p['entry_idx_date']})")
    # текущие сигналы на последнем закрытом баре (инфо)
    print("Сигналы на последнем закрытом баре:")
    for sym in UNIVERSE:
        try:
            bars = fetch_daily(sym)
            d, stop = signal(bars, len(bars)-1)
            if d: print(f"  {sym}: {d} @ {bars[-1][4]}  stop {round(stop,6)}")
        except Exception: pass

# ── self-check (без сети): флэт → бар-пробой даёт лонг; разворот даёт выход ──
def selftest():
    # 130 флэт-баров вокруг 100 (high 101 / low 99), затем бар-пробой close=110 с объёмом
    bars = [[k*86400000, 100.0, 101.0, 99.0, 100.0, 1000.0] for k in range(130)]
    bars.append([130*86400000, 100.5, 110.5, 100.5, 110.0, 5000.0])  # пробой вверх + объём×5
    i = len(bars) - 1
    d, stop = signal(bars, i)
    assert d == "L", f"selftest: ожидался лонг-сигнал, получили {d}"
    assert stop < bars[i][4], "selftest: стоп ниже входа для лонга"
    assert atr(bars, i) > 0, "selftest: ATR должен быть >0"
    # без объёма (×1.0) пробой НЕ должен сработать
    noatr = [r[:] for r in bars]; noatr[-1][5] = 1000.0
    dn, _ = signal(noatr, i)
    assert dn is None, "selftest: пробой без объёма должен отсекаться VOL_X-фильтром"
    # против тренда: лонг-пробой консолидации, но MA выше close → нет лонга (и не шорт)
    dt2 = [[k*86400000, 250-k, 251-k, 249-k, 250-k, 1000.0] for k in range(110)]  # спад 250→141
    base = dt2[-1][4]
    dt2 += [[(110+k)*86400000, base, base+1, base-1, base, 1000.0] for k in range(25)]  # флэт ~141
    dt2.append([135*86400000, base+0.5, base+2.5, base, base+2.0, 5000.0])  # лонг-пробой флэта
    dd, _ = signal(dt2, len(dt2)-1)
    assert dd is None, f"selftest: лонг-пробой под нисходящей MA должен отсекаться, получили {dd}"
    # разворот вниз → выход
    pos = {"dir":"L","entry":bars[i][4],"stop":stop}
    down = bars + [[(len(bars)+j)*86400000, 109.0-j*2, 109.5-j*2, 100.0-j*2, 101.0-j*2, 1000.0] for j in range(12)]
    ex, reason = check_exit(down, len(down)-1, pos)
    assert ex is not None, "selftest: падение должно триггерить выход (стоп/канал)"
    assert COST_RT_FRAC > 0
    print("selftest: OK — пробой+объём+тренд-фильтр+стоп+выход работают; "
          "контр-проверки (без объёма / против тренда = нет входа) прошли")

def fetch_last_price(sym):
    u = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}"
    req = urllib.request.Request(u, headers={"User-Agent": "trend/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return float(json.load(r)["result"]["list"][0]["lastPrice"])

def _fp(p):
    if p is None: return "—"
    return f"{p:,.1f}" if p >= 100 else (f"{p:.4f}" if p >= 1 else f"{p:.6f}")

def summary():
    """Сводка статуса трендовых позиций → Telegram (открытые с P&L + закрытые за 48ч + итог)."""
    from datetime import timedelta
    s = load_state(); pos = s.get("positions", {})
    lines = [f"📊 <b>СВОДКА ТРЕНД-ПОЗИЦИЙ</b>  |  {datetime.now().strftime('%d.%m %H:%M')}", ""]
    if pos:
        lines.append(f"🟢 <b>В РАБОТЕ ({len(pos)})</b>:")
        for sym, p in pos.items():
            try: cur = fetch_last_price(sym)
            except Exception: cur = None
            risk = abs(p["entry"] - p["stop"])
            d = "ЛОНГ" if p["dir"] == "L" else "ШОРТ"
            if cur and risk > 0:
                uR = ((cur - p["entry"]) if p["dir"] == "L" else (p["entry"] - cur)) / risk
                dist = ((cur - p["stop"]) / cur * 100) if p["dir"] == "L" else ((p["stop"] - cur) / cur * 100)
                em = "🟩" if uR >= 0 else "🟥"
                lines.append(f"  {em} {sym} {d}  вход {_fp(p['entry'])} → тек {_fp(cur)}  |  P&L <b>{uR:+.2f}R</b>  |  до стопа {dist:.1f}%")
            else:
                lines.append(f"  • {sym} {d} вход {_fp(p['entry'])} (цена недоступна)")
    else:
        lines.append("🟢 В работе: <b>нет открытых позиций</b> (рынок без пробоев)")
    rows = _trades_rows()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    recent = [r for r in rows if r["close_ts"] >= cutoff]
    if recent:
        lines += ["", "🏁 <b>ЗАКРЫТЫ за 48ч</b>:"]
        for r in recent:
            nr = float(r["net_R"]); d = "ЛОНГ" if r["dir"] == "L" else "ШОРТ"
            em = "🔴 СТОП" if r["reason"] == "stop" else (("🟢" if nr > 0 else "🔴") + " выход")
            lines.append(f"  {em}  {r['symbol']} {d}  netR=<b>{nr:+.2f}</b>")
    curve, rets = _equity_curve()
    if rets:
        n = len(rets); w = sum(1 for x in rets if x > 0); exp = sum(rets) / n / R_PCT
        lines += ["", f"📈 Итого paper: {n} сделок · {w}W/{n-w}L · экспектанси <b>{exp:+.2f}R</b> · equity {curve[-1]:.3f}x"]
    else:
        lines += ["", "📈 Paper: сделок ещё нет — копим."]
    text = "\n".join(lines)
    try:
        root = os.path.dirname(HERE)
        if root not in sys.path: sys.path.insert(0, root)
        from telegram_alerts import send_trend_summary, load_config
        send_trend_summary(text, load_config())
    except Exception as e:
        log(f"summary send error: {e}")
    print(text)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    {"run": process, "status": status, "killcheck": killcheck,
     "selftest": selftest, "summary": summary}.get(cmd, process)()

#!/usr/bin/env python3
"""
RUG-SHORT FORWARD HARNESS (2026-05-30)
======================================
Форвард-валидация ЕДИНСТВЕННОГО +EV-кандидата, найденного в honest backtest:
шорт сигналов rug_prep памп-детектора.

Контекст (см. memory project_pump_latency_diagnosis_2026-05-30):
- pump-LONG убыточен даже gross → выведен в SHADOW (PumpGate в pump_detector.py).
- pump-SHORT (fade) дохнет на издержках.
- rug-SHORT: net +0.66%..+1.06%/сделку (4ч, WR ~56%, медиана +), выжил при slip0.2 и
  стопах 8-10%; НЕ артефакт «шорт в падающий рынок» (работал при BTC↑). НО n=39, одно
  окно 27 дней, НЕТ out-of-sample → ГИПОТЕЗА. Этот скрипт копит честную OOS-выборку.

Правило тестируемой стратегии:
  вход SHORT по цене сигнала, hold 4ч, стоп STOP% (order-aware через mfe/mae+timing),
  выход по -change_4h либо по стопу; издержки fee+slippage round-trip; funding шорт ПОЛУЧАЕТ.

READ-ONLY: читает outcomes/pump_resolved.csv, ничего не пишет (кроме опц. TG-отчёта).
Запуск:   python3 rug_short_forward.py [--since YYYY-MM-DD] [--stop 10] [--tg]
Идея: гонять периодически; решение по эджу — ТОЛЬКО при forward n>=50.
"""
import csv, sys, time, statistics as st
from datetime import datetime, timezone
import os

CSV_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outcomes", "pump_resolved.csv")
GATE_LIVE  = "2026-05-30"          # дата запуска PumpGate → начало честного форварда
TARGET_N   = 50                    # порог статзначимости для вердикта
TAKER_PCT  = 0.055                 # Bybit taker за сторону, %
HOLD_H     = 4.0

def _f(x):
    try:
        return float(x) if x not in (None, "") else None
    except Exception:
        return None

def _load():
    if not os.path.exists(CSV_PATH):
        print(f"НЕТ ФАЙЛА: {CSV_PATH}"); sys.exit(1)
    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))
    return [r for r in rows if r.get("signal_type") == "rug_prep"]

def _short_return(r, stop_pct, slip_side):
    """Чистый % для ШОРТА одного rug_prep сигнала. order-aware стоп если есть mfe/mae.
       Возвращает (net_pct, used_stop_logic: bool)."""
    ch   = _f(r.get("change_4h"))
    fund = _f(r.get("funding")) or 0.0
    if ch is None:
        return None, False
    cost = 2 * TAKER_PCT + 2 * slip_side           # round-trip издержки, %
    fund_eff = +fund * (HOLD_H / 8.0)              # шорт ПОЛУЧАЕТ при положительном funding

    mfe  = _f(r.get("mfe_4h_pct"))                 # favorable-for-short (ход вниз), %
    mae  = _f(r.get("mae_4h_pct"))                 # adverse-for-short (ход ВВЕРХ против шорта), %
    tmfe = _f(r.get("time_to_mfe_4h"))
    tmae = _f(r.get("time_to_mae_4h"))

    if mae is not None and stop_pct is not None:
        # order-aware: стоп срабатывает, если adverse достиг STOP И случился НЕ позже favorable
        stop_hit = mae >= stop_pct
        if stop_hit and not (tmae is not None and tmfe is not None and tmfe < tmae):
            return (-stop_pct - cost + fund_eff), True
        return (-ch - cost + fund_eff), True       # выход по времени (шорт: прибыль = -change)
    # нет mfe/mae → стоп применить нельзя, fixed-time (оптимистично)
    return (-ch - cost + fund_eff), False

def _stats(rows, stop_pct, slip_side):
    nets, with_stop = [], 0
    for r in rows:
        v, used = _short_return(r, stop_pct, slip_side)
        if v is None:
            continue
        nets.append(v); with_stop += int(used)
    if not nets:
        return None
    return {
        "n": len(nets), "with_stop": with_stop,
        "mean": st.mean(nets), "median": st.median(nets),
        "wr": 100 * sum(1 for x in nets if x > 0) / len(nets),
        "sum": sum(nets),
    }

def _line(label, s):
    if not s:
        return f"  {label:30} (нет данных)"
    return (f"  {label:30} n={s['n']:>3} mean={s['mean']:+.3f}% "
            f"median={s['median']:+.3f}% WR={s['wr']:.0f}% Σ={s['sum']:+.1f}%")

def main():
    args = sys.argv[1:]
    stop_pct = 10.0
    since = None
    want_tg = "--tg" in args
    if "--stop" in args:
        stop_pct = float(args[args.index("--stop") + 1])
    if "--since" in args:
        since = args[args.index("--since") + 1]

    rug = _load()

    def _ts_after(r, dstr):
        try:
            cut = datetime.strptime(dstr, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
            return int(r.get("ts", 0)) >= cut
        except Exception:
            return False

    fwd = [r for r in rug if _ts_after(r, GATE_LIVE)]
    hist = [r for r in rug if not _ts_after(r, GATE_LIVE)]

    out = []
    out.append("=" * 70)
    out.append(f"RUG-SHORT FORWARD HARNESS | {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    out.append(f"стоп={stop_pct:.0f}% (order-aware), hold={HOLD_H:.0f}ч, taker={TAKER_PCT}%/side")
    out.append(f"rug_prep всего: {len(rug)} | до гейта ({GATE_LIVE}): {len(hist)} | ФОРВАРД после: {len(fwd)}")
    out.append("=" * 70)

    for label, subset in (("ALL-TIME", rug), (f"ФОРВАРД (после {GATE_LIVE})", fwd)):
        out.append(f"\n{label} (n={len(subset)}):")
        out.append(_line("fee+slip0.1% (rt 0.31%)", _stats(subset, stop_pct, 0.10)))
        out.append(_line("fee+slip0.2% (rt 0.51%)", _stats(subset, stop_pct, 0.20)))
        # BTC-СПЛИТ (диагностика идиосинкр. vs бета). NB 2026-05-31: реврейм детектора ОТКАЧЕН —
        # big-sample sweep (1420 событий) показал, что idiosyncratic-преференция была overfit (BTC↓ ≥ BTC↑).
        sub_idio = [r for r in subset if (_f(r.get("btc_4h")) or 0) >= 0]
        sub_beta = [r for r in subset if (_f(r.get("btc_4h")) or 0) < 0]
        out.append(_line("  идиосинкр. BTC>=0 (slip0.1)", _stats(sub_idio, stop_pct, 0.10)))
        out.append(_line("  бета BTC<0       (slip0.1)", _stats(sub_beta, stop_pct, 0.10)))

    # ── ВЕРДИКТ по форварду ──
    fs = _stats(fwd, stop_pct, 0.20)            # консервативно: slip0.2
    out.append("\n" + "-" * 70)
    if not fs or fs["n"] < TARGET_N:
        have = fs["n"] if fs else 0
        out.append(f"ВЕРДИКТ: NEED MORE — форвард n={have}/{TARGET_N}. Копим выборку, капитал НЕ ставим.")
    elif fs["mean"] > 0 and fs["median"] > 0:
        out.append(f"ВЕРДИКТ: EDGE-кандидат ПОДТВЕРЖДАЕТСЯ на форварде (n={fs['n']}, "
                   f"mean={fs['mean']:+.3f}%, median={fs['median']:+.3f}% net@slip0.2). "
                   f"Переходить к малому live-сайзингу с тем же стопом/фильтром.")
    else:
        out.append(f"ВЕРДИКТ: REJECT — форвард n={fs['n']}, но net неположителен "
                   f"(mean={fs['mean']:+.3f}%). Гипотеза rug-SHORT не подтвердилась. Закрыть.")
    out.append("-" * 70)

    report = "\n".join(out)
    print(report)

    if want_tg:
        try:
            import telegram_alerts as _ta
            cfg = _ta.load_config()
            tok, chat = cfg.get("bot_token"), str(cfg.get("chat_id") or "")
            if tok and chat:
                _ta._send(tok, chat, "<b>RUG-SHORT forward</b>\n<pre>" + report[-3500:] + "</pre>")
                print("\n[TG] отчёт отправлен в канал")
        except Exception as e:
            print(f"\n[TG] не отправлен: {e}")

if __name__ == "__main__":
    main()

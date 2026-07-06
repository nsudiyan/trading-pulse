#!/usr/bin/env python3
"""calibration/virtual_account.py — виртуальный депозит «зайди в каждый сигнал».

Честная симуляция стратегии: виртуальный депозит, вход в КАЖДЫЙ сигнал,
выход через 24ч ИЛИ по стопу/ликвидации. Со ВСЕМИ факторами, которые при
плече решают исход:
  - плечо (P&L на маржу = движение% × плечо);
  - комиссии taker round-trip;
  - реальный фандинг по символу (колонка funding) за время удержания;
  - ЛИКВИДАЦИЯ: при |MAE_24h| ≥ порога теряем всю маржу (даже если цена отскочила);
  - компаундинг депозита.

Источник истины — outcomes/resolved.csv (path-resolved, both-hit), честное
окно ≥2026-06-02 (до этой даты в r_multiple был look-ahead).

НЕ моделируем (честно, поэтому результат — ВЕРХНЯЯ граница): проскальзывание/
глубину стакана, латентность реакции на алерт, одновременную загрузку маржи
(каждая сделка сайзится от текущего депозита независимо = кривая ожидания).

Usage: python3 calibration/virtual_account.py [--since 2026-06-02]
       [--deposit 1000] [--risk-frac 0.10] [--tp-mode hold|tp1]
"""
from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

RESOLVED_CSV = Path(__file__).parent.parent / "outcomes" / "resolved.csv"

TAKER_FEE = 0.0005          # 0.05% за сторону (вход+выход = 0.10% нотионала)
MAINT_MARGIN = 0.005        # поддерживающая маржа (isolated)
FUNDING_INTERVAL_H = 8.0    # фандинг каждые 8ч

# --- Живой счётчик депозита в TG-алертах (правится тут) ---
# 2026-07-03, решение брата: бот сторону НЕ берёт и в сделки НЕ входит (даже виртуально) —
# отработка штормов = storm_report.py (%-хода, без стопов/WIN-LOSS). Все TG-сообщения
# вирт. депозита выключены; CLI-анализ (main) продолжает работать руками.
LIVE_ENABLED = False
LIVE_SINCE = "2026-06-02"   # с какой даты считаем (честное окно без look-ahead)
LIVE_DEPOSIT = 1000.0       # виртуальный депозит, $
LIVE_RISK_FRAC = 0.10       # маржа на сделку = доля депозита
LIVE_TP_MODE = "hold"       # hold = держим 24ч (стоп режет); tp1 = выход по тейку
_CACHE = {"ts": 0.0, "line": ""}
_CACHE_TTL = 120.0          # пересчёт не чаще раза в 2 мин (алертов может быть много)


def _f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def load(since: str) -> list[dict]:
    rows = []
    for r in csv.DictReader(open(RESOLVED_CSV)):
        if r.get("run_ts", "") < since:
            continue
        if (r.get("direction") or "") not in ("ЛОНГ", "ШОРТ"):
            continue  # ЖДАТЬ = не сделка
        if not (r.get("exit_reason_24h") or "").strip():
            continue  # ещё не резолвнуто
        if _f(r.get("price_entry")) and _f(r.get("exit_price_24h")) is not None:
            rows.append(r)
    rows.sort(key=lambda r: r.get("run_ts", ""))
    return rows


def trade_return_on_margin(r: dict, lev: float, tp_mode: str) -> tuple[float, str]:
    """Возвращает (доходность_на_маржу, причина_выхода). Доходность −1.0 = вся маржа потеряна."""
    entry = _f(r["price_entry"])
    sign = 1.0 if r["direction"] == "ЛОНГ" else -1.0
    mae = abs(_f(r.get("mae_24h_pct"), 0.0)) / 100.0      # макс просадка против позиции, доля
    funding = _f(r.get("funding"), 0.0) / 100.0           # ставка фандинга, доля
    hold_h = (_f(r.get("hold_time_24h_min"), 1440.0) or 1440.0) / 60.0

    liq_move = (1.0 / lev) - MAINT_MARGIN                 # адверс-движение до ликвидации
    fees = TAKER_FEE * 2 * lev                            # round-trip на маржу
    fund_intervals = max(0.0, hold_h / FUNDING_INTERVAL_H)
    # фандинг: при положительной ставке лонг платит, шорт получает
    fund_cost = funding * sign * fund_intervals * lev

    # --- определяем цену выхода и причину ---
    stop = _f(r.get("stop"))
    stop_dist = abs(stop - entry) / entry if (stop and entry) else 1e9
    hit_stop = (r.get("hit_stop_24h") or "").strip().lower() in ("1", "true", "yes")

    # 1) ликвидация раньше стопа? (стоп шире, чем дистанция ликвидации, и просадка достигла её)
    if mae >= liq_move and stop_dist >= liq_move:
        return -1.0, "liq"

    # 2) выход по стопу
    if hit_stop and stop_dist < liq_move:
        move = -stop_dist                                 # стоп против нас
        return max(-1.0, move * lev - fees + fund_cost), "stop"

    # 3) ликвидация при близком MAE даже без флага стопа (страховка)
    if mae >= liq_move:
        return -1.0, "liq"

    # 4) штатный выход
    if tp_mode == "tp1":
        exit_px = _f(r.get("exit_price_24h"), entry)      # учитывает tp1/24h по существующей резолюции
    else:  # hold: держим 24ч, тейк не фиксируем
        exit_px = _f(r.get("price_24h")) or _f(r.get("exit_price_24h"), entry)
    move = sign * (exit_px - entry) / entry
    return max(-1.0, move * lev - fees + fund_cost), ("24h" if tp_mode == "hold" else (r.get("exit_reason_24h") or "24h"))


def simulate(rows, deposit: float, risk_frac: float, lev: float, tp_mode: str) -> dict:
    eq = deposit
    peak = deposit
    max_dd = 0.0
    wins = losses = liqs = 0
    curve = [deposit]
    for r in rows:
        ret, reason = trade_return_on_margin(r, lev, tp_mode)
        margin = eq * risk_frac
        pnl = margin * ret
        eq += pnl
        if eq <= 0:
            eq = 0.0
            curve.append(eq)
            break
        if reason == "liq":
            liqs += 1
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak if peak > 0 else 0.0)
        curve.append(eq)
    n = wins + losses
    return {
        "final": eq, "ret_pct": (eq / deposit - 1) * 100, "max_dd_pct": max_dd * 100,
        "trades": len(rows), "wins": wins, "losses": losses, "liqs": liqs,
        "wr": (wins / n * 100) if n else 0.0, "curve": curve,
    }


def deposit_line() -> str:
    """Строка для TG-алерта: вирт. депозит «в каждый сигнал», 1x и 5x рядом.
    НИКОГДА не бросает (return '' при любой ошибке) — не должна ронять алерт.
    Кэш 2 мин, т.к. алертов может быть много."""
    try:
        if not LIVE_ENABLED:
            return ""
        now = time.time()
        if _CACHE["line"] and (now - _CACHE["ts"] < _CACHE_TTL):
            return _CACHE["line"]
        rows = load(LIVE_SINCE)
        if not rows:
            return ""
        s1 = simulate(rows, LIVE_DEPOSIT, LIVE_RISK_FRAC, 1, LIVE_TP_MODE)
        s5 = simulate(rows, LIVE_DEPOSIT, LIVE_RISK_FRAC, 5, LIVE_TP_MODE)
        line = (
            f"💰 <b>Вирт. депозит</b> ${LIVE_DEPOSIT:.0f} «в каждый сигнал» "
            f"(с {LIVE_SINCE}, {s1['trades']} сделок, винрейт {s1['wr']:.0f}%):\n"
            f"   <b>1x</b> ${s1['final']:.0f} ({s1['ret_pct']:+.0f}%)  ·  "
            f"<b>5x</b> ${s5['final']:.0f} ({s5['ret_pct']:+.0f}%, ликв. {s5['liqs']})"
        )
        _CACHE.update(ts=now, line=line)
        return line
    except Exception:
        return ""


_EXIT_RU = {"sl": "стоп", "tp1": "тейк", "stop": "стоп", "24h": "24ч",
            "24h_close": "24ч", "liq": "ликвидация"}
_LAST_ROW_FILE = Path(__file__).parent / ".va_last_rowcount"


def resolution_note(new_rows) -> str:
    """Сообщение о закрытых сигналах: как отреагировала цена (стоп/тейк/24ч/ликв.),
    P&L каждой сделки на маржу (1x и 5x) + НОВЫЙ вирт. баланс. Никогда не бросает."""
    try:
        valid = [r for r in new_rows
                 if r.get("direction") in ("ЛОНГ", "ШОРТ")
                 and _f(r.get("price_entry")) and _f(r.get("exit_price_24h")) is not None]
        if not valid:
            return ""
        lines = [f"📒 <b>Закрыто {len(valid)} сигнал(ов)</b> (24ч/стоп) — учёт в вирт. депозит:"]
        for r in valid[:10]:
            sym = r.get("symbol", "?")
            d = "▲ЛОНГ" if r.get("direction") == "ЛОНГ" else "▼ШОРТ"
            r1, rsn = trade_return_on_margin(r, 1, LIVE_TP_MODE)
            r5, _ = trade_return_on_margin(r, 5, LIVE_TP_MODE)
            lines.append(f"• <b>{sym}</b> {d} — {_EXIT_RU.get(rsn, rsn)}: "
                         f"1x {r1*100:+.1f}% · 5x {r5*100:+.0f}%")
        a = load(LIVE_SINCE)
        s1 = simulate(a, LIVE_DEPOSIT, LIVE_RISK_FRAC, 1, LIVE_TP_MODE)
        s5 = simulate(a, LIVE_DEPOSIT, LIVE_RISK_FRAC, 5, LIVE_TP_MODE)
        _CACHE["ts"] = 0.0  # следующий алерт пересчитает строку с новым балансом
        lines += ["", f"💰 Вирт. баланс: <b>1x</b> ${s1['final']:.0f} ({s1['ret_pct']:+.0f}%) · "
                      f"<b>5x</b> ${s5['final']:.0f} ({s5['ret_pct']:+.0f}%)"]
        return "\n".join(lines)
    except Exception:
        return ""


def _send_tg(text: str):
    try:
        import json
        cfg = json.loads((Path(__file__).parent.parent / "telegram_config.json").read_text())
        token, chat = cfg.get("bot_token"), str(cfg.get("chat_id", ""))
        if not token or not chat:
            return
        from telegram_alerts import _send
        _send(token, chat, text)
    except Exception:
        pass


def notify_resolutions():
    """Зовётся после outcome_tracker.check_and_resolve. Если в resolved.csv появились
    новые закрытые сигналы — шлёт их P&L (1x/5x) + новый вирт. баланс в TG. Стейтфул
    (помнит число строк), идемпотентно, НИКОГДА не бросает. Первый запуск только
    запоминает rowcount, не спамя всей историей."""
    try:
        all_rows = list(csv.DictReader(open(RESOLVED_CSV)))
        cur = len(all_rows)
        if not LIVE_ENABLED:  # выключено: двигаем счётчик строк, чтобы ре-включение не спамило историей
            _LAST_ROW_FILE.write_text(str(cur))
            return
        prev = 0
        if _LAST_ROW_FILE.exists():
            try:
                prev = int((_LAST_ROW_FILE.read_text().strip() or "0"))
            except (ValueError, OSError):
                prev = 0
        if prev <= 0 or prev > cur:      # первый запуск / файл усечён → только запомнить
            _LAST_ROW_FILE.write_text(str(cur))
            return
        new = all_rows[prev:]
        _LAST_ROW_FILE.write_text(str(cur))
        if new:
            note = resolution_note(new)
            if note:
                _send_tg(note)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-02")
    ap.add_argument("--deposit", type=float, default=1000.0)
    ap.add_argument("--risk-frac", type=float, default=0.10)
    ap.add_argument("--tp-mode", choices=["hold", "tp1"], default="hold")
    a = ap.parse_args()

    rows = load(a.since)
    print(f"=== Виртуальный депозit «в каждый сигнал» ===")
    print(f"Окно: ≥{a.since} | сделок: {len(rows)} | депозит ${a.deposit:.0f} | "
          f"маржа/сделку: {a.risk_frac:.0%} | комиссия {TAKER_FEE*100:.3f}%/сторону\n")

    print(f"{'плечо':>6} {'tp-режим':>9} {'итог $':>10} {'доход%':>9} {'просадка%':>10} "
          f"{'винрейт':>8} {'ликвид.':>8}")
    for tp in ("hold", "tp1"):
        for lev in (1, 2, 3, 5):
            s = simulate(rows, a.deposit, a.risk_frac, lev, tp)
            print(f"{lev:>5}x {tp:>9} {s['final']:>10.0f} {s['ret_pct']:>+8.1f}% "
                  f"{s['max_dd_pct']:>9.1f}% {s['wr']:>7.1f}% {s['liqs']:>4}/{s['trades']}")

    print(f"\n--- Детально: плечо 5x, режим {a.tp_mode}, маржа {a.risk_frac:.0%} (твоя конфигурация) ---")
    s = simulate(rows, a.deposit, a.risk_frac, 5, a.tp_mode)
    print(f"  Старт ${a.deposit:.0f} → Итог ${s['final']:.0f} ({s['ret_pct']:+.1f}%)")
    print(f"  Сделок {s['trades']} | W/L {s['wins']}/{s['losses']} (винрейт {s['wr']:.1f}%) | "
          f"ликвидаций {s['liqs']} | макс. просадка {s['max_dd_pct']:.1f}%")
    # чувствительность к размеру позиции при 5x
    print(f"\n--- Чувствительность к размеру маржи (плечо 5x, режим {a.tp_mode}) ---")
    for rf in (0.05, 0.10, 0.20):
        s = simulate(rows, a.deposit, rf, 5, a.tp_mode)
        print(f"  маржа {rf:>4.0%}/сделку → итог ${s['final']:>8.0f} ({s['ret_pct']:>+7.1f}%), "
              f"просадка {s['max_dd_pct']:.0f}%, ликвид {s['liqs']}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
tv_pump_plan.py — ПОДТВЕРЖДАЮЩАЯ визуальная разметка pump/rug на 15m.

Грунтуется на РЕАЛЬНЫХ данных бота (не выдуманное TA):
  • стенки стакана  → orderbook_imbalance.get_book_metrics (реальная стоящая ликвидность)
  • кластеры ликвидаций → liquidations.db (где реально выбивало стопы) — магниты для TP, барьеры для SL
  • OB/FVG → считает рендерер из баров графика, рисует ВТОРИЧНО подписью «det» (SMC разоблачена, не прогноз)

⚠️ Это КАРТИНКА ДЛЯ ГЛАЗА (подтверждение), а НЕ генератор сигналов и НЕ повод поднять плечо.
Логика TP/SL: TP — к ближайшему магниту ликвидности по ходу; SL — ЗА кластером против хода
(чтобы стоп-хант/свип не выбил; правило брата «SL переживает свип»). Без LLM/API.

Использование:
    python3 tv_pump_plan.py SOL SHORT
    from tv_pump_plan import make_pump_plan
    png = make_pump_plan("SOL", "SHORT")
"""
import asyncio
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

from tv_trade_plan import make_plan_image, _log

HERE = Path(__file__).resolve().parent
LIQ_DB = HERE / "liquidations.db"


def _base(symbol: str) -> str:
    """ETH / BYBIT:ETHUSDT.P → ETHUSDT (формат БД ликвидаций и стакана)."""
    s = symbol.upper().replace("BYBIT:", "").replace(".P", "")
    if not s.endswith("USDT"):
        s += "USDT"
    return s


def liq_clusters(symbol: str, price: float, n: int = 4, band: float = 0.12):
    """Кластеры ликвидаций возле цены: бакетим по цене, суммируем $, берём топ-n.
    Возвращает [{price, usd, side: 'below'|'above'}]."""
    try:
        sym = _base(symbol)
        con = sqlite3.connect(str(LIQ_DB))
        lo, hi = price * (1 - band), price * (1 + band)
        rows = con.execute(
            "SELECT price, usd FROM liquidations WHERE symbol=? AND price BETWEEN ? AND ?",
            (sym, lo, hi)).fetchall()
        con.close()
        if not rows:
            return []
        bucket = max(price * 0.003, 1e-9)  # ~0.3% бакет
        agg = defaultdict(float)
        for p, u in rows:
            agg[round(p / bucket) * bucket] += (u or 0)
        top = sorted(agg.items(), key=lambda x: -x[1])[:n]
        return [{"price": round(p, 8), "usd": round(u),
                 "side": "below" if p < price else "above"} for p, u in top]
    except Exception as e:
        _log(f"liq_clusters: {e}")
        return []


def get_walls(symbol: str):
    """Стенки стакана (реальные лимитки) + цена. {price, bid_wall, ask_wall} или None.
    Запускаем в отдельном потоке с собственным loop — безопасно и из sync, и изнутри
    демона с уже работающим event loop (иначе asyncio.run падает)."""
    try:
        from orderbook_imbalance import get_book_metrics
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            m = ex.submit(lambda: asyncio.run(get_book_metrics(_base(symbol)))).result(timeout=20)
        if not m:
            return None
        price = ((m.get("best_bid") or 0) + (m.get("best_ask") or 0)) / 2 or m.get("best_bid")
        return {"price": price, "bid_wall": m.get("bid_wall"), "ask_wall": m.get("ask_wall")}
    except Exception as e:
        _log(f"get_walls: {e}")
        return None


def _fmt_usd(u):
    return f"${u/1e6:.1f}M" if u >= 1e6 else f"${round(u/1e3)}k"


def make_pump_plan(symbol: str, direction: str = "SHORT",
                   entry: Optional[float] = None) -> Optional[str]:
    """Разметить pump/rug-картинку на реальной ликвидности. None при проблеме (graceful)."""
    direction = (direction or "SHORT").upper()
    w = get_walls(symbol)
    if not w or not w.get("price"):
        _log("нет данных стакана/цены — не размечаю")
        return None
    price = w["price"]
    bidw, askw = w.get("bid_wall"), w.get("ask_wall")
    clusters = liq_clusters(symbol, price)
    entry = entry if entry else price

    # уровни ликвидности для рисунка
    levels = []
    if bidw:
        levels.append({"price": bidw["price"], "color": "#26a69a", "width": 3, "style": 0,
                       "label": f"BID wall {_fmt_usd(bidw['size_usd'])}"})
    if askw:
        levels.append({"price": askw["price"], "color": "#ef5350", "width": 3, "style": 0,
                       "label": f"ASK wall {_fmt_usd(askw['size_usd'])}"})
    for cl in clusters:
        levels.append({"price": cl["price"], "color": "#ff9800", "width": 2, "style": 1,
                       "label": f"liq {_fmt_usd(cl['usd'])}"})

    below = sorted([c["price"] for c in clusters if c["price"] < price * 0.999], reverse=True)
    above = sorted([c["price"] for c in clusters if c["price"] > price * 1.001])
    if bidw and bidw["price"] < price:
        below.append(bidw["price"])
    if askw and askw["price"] > price:
        above.insert(0, askw["price"])
    below.sort(reverse=True)
    above.sort()

    # TP — к магниту по ходу; SL — ЗА барьером против хода (+буфер, переживаем свип).
    # min_sl: пол стопа ≥1% — иначе стенка впритык к цене даёт фейк-R:R (тест поймал 11:1).
    buf = price * 0.0015
    min_sl = price * 0.01     # пол стопа 1%
    min_tp = price * 0.015    # пол цели 1.5% (магнит впритык не даёт мизерный R:R)
    if direction == "SHORT":
        tp_base = (below[0] if below else price * 0.97)         # ближайший магнит снизу
        tp = min(tp_base, entry - min_tp)                       # не ближе 1.5%
        sl_base = (above[0] + buf) if above else price * 1.02   # за барьером сверху
        sl = max(sl_base, entry + min_sl)                       # не ближе 1%
    else:
        tp_base = (above[0] if above else price * 1.03)
        tp = max(tp_base, entry + min_tp)
        sl_base = (below[0] - buf) if below else price * 0.98
        sl = min(sl_base, entry - min_sl)

    # зона входа (примерный диапазон) — полосой вокруг входа
    ez = price * 0.0025
    zones = [{"top": entry + ez, "bottom": entry - ez, "color": "#2962ff", "label": "entry zone"}]

    note = "цель — у крупной ликвидности (магнит) · стоп — за барьером · визуал, не сигнал"
    _log(f"{direction} {symbol} price={round(price,6)} bidW={bidw and round(bidw['price'],6)} "
         f"askW={askw and round(askw['price'],6)} clusters={[(c['price'],_fmt_usd(c['usd'])) for c in clusters]} "
         f"=> entry≈{round(entry,6)} tp={round(tp,6)} sl={round(sl,6)}")

    # ПРОСТОЙ режим: на графике только вход/стоп/цель + заголовок (стенки/кластеры/OB
    # ведут расчёт TP/SL, но НЕ рисуются — глазу нужно где зайти/выйти, без шума)
    return make_plan_image(symbol, direction=direction, entry=entry, sl=sl, tp=tp,
                           note=note, simple=True)


def attach_tv_plan_to_tg(symbol: str, direction: str, token: str, chat_id: str,
                         caption: str = "", entry: Optional[float] = None) -> bool:
    """Размечает 15m по реальной ликвидности и шлёт PNG в Telegram. ОПЦИОНАЛЬНО:
    работает ТОЛЬКО при env TV_PLAN_ENABLED=1 (по умолчанию выкл — мердж кода ничего
    не меняет). Полностью graceful: любая проблема (TV закрыт, нет данных) → False,
    алерт уже ушёл текстом. БЕЗ обращений к Claude API."""
    if os.environ.get("TV_PLAN_ENABLED", "") != "1":
        return False
    if not token or not chat_id:
        return False
    try:
        png = make_pump_plan(symbol, direction, entry)
        if not png:
            return False
        import chart_analyzer as _ca
        _ca.tg_send_photo(token, chat_id, Path(png), caption=(caption or "")[:1024])
        _log(f"attach_tv_plan_to_tg: PNG отправлен {symbol} {direction}")
        return True
    except Exception as e:
        _log(f"attach_tv_plan_to_tg: {e}")
        return False


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "SOL"
    d = sys.argv[2] if len(sys.argv) > 2 else "SHORT"
    png = make_pump_plan(sym, d)
    print(png or "FAILED", file=sys.stderr if not png else sys.stdout)
    raise SystemExit(0 if png else 1)

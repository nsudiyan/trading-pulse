"""
top_trader_positions.py — Топ-трейдеры (long/short) с Binance Futures.

Binance публикует ТРИ метрики:
  - globalLongShortAccountRatio — все аккаунты
  - topLongShortAccountRatio    — топ-20% по балансу
  - topLongShortPositionRatio   — топ-20% по позиции (объёму)

Это РАЗЛИЧАЮЩИЕ метрики. Если global=0.8 long, но top_position=0.4 long
— это значит ритейл закупился (бычка), а киты сидят в шорте. Классическая
distribution-фаза.

Использование:
    from top_trader_positions import get_position_skew

    skew = await get_position_skew("BTCUSDT", "1h")
    # {"global_long_pct": 60, "top_pos_long_pct": 38, "delta": -22, "signal": "🔴 retail_long_top_short — дист."}
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from async_http import http_get_json

log = logging.getLogger("top_traders")
BINANCE_BASE = "https://fapi.binance.com"


async def _fetch(endpoint: str, symbol: str, period: str = "1h") -> Optional[dict]:
    """Берём только последнюю свечу (limit=1)."""
    data = await http_get_json(
        f"{BINANCE_BASE}/futures/data/{endpoint}",
        params={"symbol": symbol, "period": period, "limit": "1"},
        max_retries=2,
    )
    if not data or not isinstance(data, list) or not data:
        return None
    return data[0]


async def get_position_skew(symbol: str, period: str = "1h") -> Optional[dict]:
    """
    Возвращает спред между ритейлом и китами.
    period ∈ {"5m","15m","30m","1h","2h","4h","6h","12h","1d"}
    """
    glob_task = asyncio.create_task(_fetch("globalLongShortAccountRatio", symbol, period))
    top_acc_task = asyncio.create_task(_fetch("topLongShortAccountRatio", symbol, period))
    top_pos_task = asyncio.create_task(_fetch("topLongShortPositionRatio", symbol, period))
    glob, top_acc, top_pos = await asyncio.gather(glob_task, top_acc_task, top_pos_task)

    if not glob or not top_pos:
        return None

    try:
        global_long  = float(glob["longAccount"])  * 100
        top_pos_long = float(top_pos["longAccount"]) * 100
        top_acc_long = float(top_acc["longAccount"]) * 100 if top_acc else None
    except (KeyError, TypeError, ValueError) as e:
        log.warning(f"parse error {symbol}: {e}")
        return None

    delta = top_pos_long - global_long
    return {
        "symbol":           symbol,
        "period":           period,
        "global_long_pct":  round(global_long, 1),
        "top_acc_long_pct": round(top_acc_long, 1) if top_acc_long else None,
        "top_pos_long_pct": round(top_pos_long, 1),
        "delta":            round(delta, 1),
        "signal":           _classify(global_long, top_pos_long, delta),
    }


def _classify(global_long: float, top_pos_long: float, delta: float) -> str:
    """
    Логика:
      retail bullish + top short = распределение → bearish
      retail bearish + top long  = аккумуляция  → bullish
    """
    if global_long > 65 and top_pos_long < 45:
        return "🔴 retail_LONG_top_SHORT — distribution, киты сливают"
    if global_long < 40 and top_pos_long > 60:
        return "🟢 retail_SHORT_top_LONG — accumulation, киты собирают"
    if abs(delta) > 15:
        return f"⚠ divergence_{delta:+.0f}pp"
    return "⚪ согласовано"


async def get_position_skew_batch(symbols: list[str], period: str = "1h") -> dict:
    """Параллельно для списка символов, с защитой от частичных отказов."""
    tasks = {s: asyncio.create_task(get_position_skew(s, period)) for s in symbols}
    out = {}
    for sym, task in tasks.items():
        try:
            r = await task
            if r:
                out[sym] = r
        except Exception as e:
            log.warning(f"{sym}: {e}")
    return out


if __name__ == "__main__":
    import sys
    import json
    logging.basicConfig(level=logging.INFO)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    async def _demo():
        r = await get_position_skew_batch(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        print(json.dumps(r, indent=2, ensure_ascii=False))

    asyncio.run(_demo())

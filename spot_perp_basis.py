"""
spot_perp_basis.py — Spot vs Perpetual basis (Bybit).

Метрика: basis_pct = (perp - spot) / spot × 100
  - basis < -0.3%   → перп дисконт (short squeeze setup)
  - basis > +0.5%   → перп премиум (фруза, риск дампа)
  - basis ~  0      → норма

Использование:
    from spot_perp_basis import get_basis_batch

    basis = await get_basis_batch(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    # {"BTCUSDT": {"spot": 67200, "perp": 67180, "basis_pct": -0.030, "signal": "норма"}, ...}
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from async_http import http_get_json

log = logging.getLogger("basis")
BYBIT_BASE = "https://api.bybit.com"


async def _fetch_tickers(category: str) -> dict:
    """category = 'spot' | 'linear'. Возвращает {symbol: lastPrice}."""
    data = await http_get_json(
        f"{BYBIT_BASE}/v5/market/tickers",
        params={"category": category},
        max_retries=3,
    )
    if not data or data.get("retCode") != 0:
        return {}
    out = {}
    for item in data.get("result", {}).get("list", []):
        sym = item.get("symbol")
        try:
            out[sym] = float(item.get("lastPrice", 0) or 0)
        except (TypeError, ValueError):
            continue
    return out


async def get_basis_batch(symbols: list[str]) -> dict:
    """Один запрос на категорию, сравнение по списку символов."""
    spot_task = asyncio.create_task(_fetch_tickers("spot"))
    perp_task = asyncio.create_task(_fetch_tickers("linear"))
    spot, perp = await asyncio.gather(spot_task, perp_task)

    out = {}
    for sym in symbols:
        s = spot.get(sym)
        p = perp.get(sym)
        if not s or not p or s <= 0:
            continue
        basis_pct = (p - s) / s * 100
        out[sym] = {
            "spot":      s,
            "perp":      p,
            "basis_pct": round(basis_pct, 4),
            "signal":    _classify(basis_pct),
        }
    return out


def _classify(basis_pct: float) -> str:
    if basis_pct < -0.5:
        return "🟢 СИЛЬНЫЙ_ДИСКОНТ_перпа — потенциал short squeeze"
    if basis_pct < -0.3:
        return "🟢 дисконт_перпа"
    if basis_pct > 0.7:
        return "🔴 СИЛЬНАЯ_ПРЕМИЯ_перпа — froth, риск коррекции"
    if basis_pct > 0.5:
        return "🔴 премия_перпа"
    return "⚪ норма"


if __name__ == "__main__":
    import sys
    import json
    logging.basicConfig(level=logging.INFO)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    async def _demo():
        r = await get_basis_batch(["BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT"])
        print(json.dumps(r, indent=2, ensure_ascii=False))

    asyncio.run(_demo())

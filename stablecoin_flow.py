"""
stablecoin_flow.py — Мониторинг эмиссии USDT/USDC/DAI/FDUSD/etc через DefiLlama.

Метрики (за 24h):
  - delta_24h_usd: чистая эмиссия (mint - burn)
  - delta_7d_usd: тренд за неделю
  - chain_breakdown: где именно печатают (Ethereum / Tron / Arbitrum / Solana)
  - alert_threshold: > $200M / 24h = «buying power coming»

Использование (sync, через asyncio.run):
    from stablecoin_flow import get_stable_flow

    flow = await get_stable_flow()
    if flow["total_delta_24h_usd"] > 200_000_000:
        # это leading indicator — крупный buy power входит в рынок

Кэш на диск 30 мин (lookback окно DefiLlama обновляется не чаще).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from async_http import http_get_json

log = logging.getLogger("stablecoin")

BASE_DIR = Path(__file__).parent
CACHE_PATH = BASE_DIR / "outcomes" / "stablecoin_flow.json"
CACHE_TTL = 1800  # 30 мин

# DefiLlama endpoints
URL_STABLES = "https://stablecoins.llama.fi/stablecoins?includePrices=true"
URL_HISTORY = "https://stablecoins.llama.fi/stablecoincharts/all"

# Топ-стейблы по market cap (>$100M)
TRACKED = {"USDT", "USDC", "DAI", "FDUSD", "PYUSD", "TUSD", "USDe"}


async def get_stable_flow() -> Optional[dict]:
    """
    Возвращает агрегированный flow по стейблам.
    Кэшируется на диск 30 мин.
    """
    cached = _read_cache()
    if cached:
        return cached

    data = await http_get_json(URL_STABLES, max_retries=3)
    if not data or "peggedAssets" not in data:
        log.warning("DefiLlama stables: пустой ответ")
        return None

    total_delta_24h = 0.0
    total_delta_7d = 0.0
    per_asset = {}

    for asset in data["peggedAssets"]:
        symbol = asset.get("symbol", "")
        if symbol not in TRACKED:
            continue
        try:
            circ = asset.get("circulating", {}).get("peggedUSD", 0)
            circ_prev_24h = asset.get("circulatingPrevDay", {}).get("peggedUSD", 0)
            circ_prev_7d  = asset.get("circulatingPrevWeek", {}).get("peggedUSD", 0)
            d24 = circ - circ_prev_24h
            d7  = circ - circ_prev_7d

            chains = {}
            for chain, supply in (asset.get("chainCirculating") or {}).items():
                cur = supply.get("current", {}).get("peggedUSD", 0)
                prev = supply.get("circulatingPrevDay", {}).get("peggedUSD", 0)
                if abs(cur - prev) > 1_000_000:
                    chains[chain] = round(cur - prev)

            per_asset[symbol] = {
                "circulating_usd": round(circ),
                "delta_24h_usd":   round(d24),
                "delta_7d_usd":    round(d7),
                "by_chain_24h":    chains,
            }
            total_delta_24h += d24
            total_delta_7d  += d7
        except (TypeError, KeyError, ValueError) as e:
            log.debug(f"skip {symbol}: {e}")
            continue

    result = {
        "ts":                    int(time.time()),
        "total_delta_24h_usd":   round(total_delta_24h),
        "total_delta_7d_usd":    round(total_delta_7d),
        "per_asset":             per_asset,
        "signal":                _classify_signal(total_delta_24h, total_delta_7d),
    }
    _write_cache(result)
    return result


def _classify_signal(d24: float, d7: float) -> str:
    """Классификация для Claude-контекста."""
    if d24 > 500_000_000:
        return "🟢 СИЛЬНЫЙ_BUYPOWER: эмиссия >$500M/24h"
    if d24 > 200_000_000:
        return "🟢 buy_power_coming: эмиссия >$200M/24h"
    if d24 < -200_000_000:
        return "🔴 risk_off: burn >$200M/24h"
    if d7 > 1_000_000_000:
        return "🟢 недельный_тренд_бычий"
    if d7 < -1_000_000_000:
        return "🔴 недельный_тренд_медвежий"
    return "⚪ нейтральный"


def _read_cache() -> Optional[dict]:
    if not CACHE_PATH.exists():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if time.time() - data.get("ts", 0) < CACHE_TTL:
            return data
    except Exception:
        pass
    return None


def _write_cache(data: dict):
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning(f"cache write failed: {e}")


if __name__ == "__main__":
    import asyncio
    import sys
    logging.basicConfig(level=logging.INFO)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    async def _demo():
        result = await get_stable_flow()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    asyncio.run(_demo())

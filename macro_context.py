"""
macro_context.py — Агрегатор макро-контекста для Claude-промпта.

Собирает в один dict:
  - stablecoin_flow (DefiLlama)
  - options_skew (Deribit BTC/ETH)
  - btc_dominance + eth_btc (CoinGecko)
  - fear_and_greed (alternative.me — уже используется в screener)

Кэш 5 минут — макро не меняется чаще.

Использование в claude_realtime_filter._build_context():
    from macro_context import get_macro

    macro = await get_macro()   # либо синхронно asyncio.run(get_macro())
    context_lines += [f"=== MACRO ===", *macro["lines"]]
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from async_http import http_get_json

log = logging.getLogger("macro")
BASE_DIR = Path(__file__).parent
CACHE_PATH = BASE_DIR / "outcomes" / "macro_snapshot.json"
CACHE_TTL = 300  # 5 мин


async def _btc_dominance() -> Optional[dict]:
    """CoinGecko /global. Без ключа, 30 req/min."""
    data = await http_get_json("https://api.coingecko.com/api/v3/global", max_retries=2)
    if not data or "data" not in data:
        return None
    d = data["data"]
    return {
        "btc_dominance":   round(d.get("market_cap_percentage", {}).get("btc", 0), 2),
        "eth_dominance":   round(d.get("market_cap_percentage", {}).get("eth", 0), 2),
        "total_mcap_usd":  d.get("total_market_cap", {}).get("usd"),
        "total_vol_24h":   d.get("total_volume", {}).get("usd"),
        "mcap_change_24h": round(d.get("market_cap_change_percentage_24h_usd", 0), 2),
    }


async def _fear_greed() -> Optional[dict]:
    data = await http_get_json("https://api.alternative.me/fng/?limit=2", max_retries=2)
    if not data or "data" not in data or not data["data"]:
        return None
    today = data["data"][0]
    yest = data["data"][1] if len(data["data"]) > 1 else None
    return {
        "value":     int(today["value"]),
        "label":     today["value_classification"],
        "yesterday": int(yest["value"]) if yest else None,
        "delta":     int(today["value"]) - int(yest["value"]) if yest else 0,
    }


async def get_macro() -> dict:
    """Агрегатор. Кэшируется 5 минут, fail-soft по каждому источнику."""
    cached = _read_cache()
    if cached:
        return cached

    # Импортируем модули лениво — чтобы они не требовали быть установленными если макро отключено
    try:
        from stablecoin_flow import get_stable_flow
    except Exception:
        get_stable_flow = None
    try:
        from options_skew import get_btc_eth_skew
    except Exception:
        get_btc_eth_skew = None

    tasks = {
        "btc_dom": asyncio.create_task(_btc_dominance()),
        "fng":     asyncio.create_task(_fear_greed()),
    }
    if get_stable_flow:
        tasks["stable"] = asyncio.create_task(get_stable_flow())
    if get_btc_eth_skew:
        tasks["options"] = asyncio.create_task(get_btc_eth_skew())

    results = {}
    for name, task in tasks.items():
        try:
            results[name] = await task
        except Exception as e:
            log.warning(f"{name} failed: {e}")
            results[name] = None

    # Сборка текстовых строк для Claude
    lines = []
    if results.get("btc_dom"):
        b = results["btc_dom"]
        lines.append(
            f"BTC.D: {b['btc_dominance']}%  ETH.D: {b['eth_dominance']}%  "
            f"Mcap 24h: {b['mcap_change_24h']:+.2f}%"
        )
    if results.get("fng"):
        f = results["fng"]
        delta_str = f" (Δ{f['delta']:+d})" if f.get("yesterday") else ""
        lines.append(f"F&G: {f['value']} ({f['label']}){delta_str}")
    if results.get("stable"):
        s = results["stable"]
        lines.append(
            f"Стейблы 24h: {s['total_delta_24h_usd']/1e6:+.0f}M  "
            f"7d: {s['total_delta_7d_usd']/1e9:+.2f}B  →  {s['signal']}"
        )
    if results.get("options"):
        o = results["options"]
        for ccy, d in o.items():
            if d:
                lines.append(
                    f"{ccy} опционы: skew={d['skew_25d']:+.1f}  ATM_IV={d['atm_iv']:.0f}%  →  {d['signal']}"
                )

    snapshot = {
        "ts":      int(time.time()),
        "raw":     results,
        "lines":   lines,
    }
    _write_cache(snapshot)
    return snapshot


def _read_cache() -> Optional[dict]:
    if not CACHE_PATH.exists():
        return None
    try:
        d = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if time.time() - d.get("ts", 0) < CACHE_TTL:
            return d
    except Exception:
        pass
    return None


def _write_cache(data: dict):
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning(f"cache: {e}")


def get_macro_sync() -> dict:
    """Synchronous wrapper для кода который ещё не на asyncio."""
    try:
        # Если уже есть running loop — используем nest_asyncio или возвращаем кэш
        loop = asyncio.get_running_loop()
        # В running loop нельзя asyncio.run — fallback на кэш
        return _read_cache() or {"lines": [], "raw": {}}
    except RuntimeError:
        pass
    return asyncio.run(get_macro())


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    async def _demo():
        m = await get_macro()
        print("=== MACRO LINES ===")
        for l in m["lines"]:
            print(" ", l)
        print("\n=== RAW ===")
        print(json.dumps(m["raw"], ensure_ascii=False, indent=2, default=str)[:2000])

    asyncio.run(_demo())

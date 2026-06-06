"""
options_skew.py — Опционный skew с Deribit (BTC/ETH).

Метрика skew = IV(put_25delta) - IV(call_25delta).
  > +5%  → рынок боится падения (хедж в путах) — может быть bullish contrarian
  > +10% → паника
  < -3%  → froth (все в коллах) — bearish contrarian

Это leading indicator БТЦ/ЭТХ за 6-24ч до движения. Альты следуют.

Использование:
    from options_skew import get_btc_eth_skew

    s = await get_btc_eth_skew()
    # {"BTC": {"skew_25d": 4.2, "atm_iv": 52.1, "signal": "норма"},
    #  "ETH": {...}}
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from async_http import http_get_json

log = logging.getLogger("options")
DERIBIT_BASE = "https://www.deribit.com/api/v2/public"


async def _book_summary(currency: str) -> Optional[list]:
    """Все опционы по валюте, с IV и delta."""
    data = await http_get_json(
        f"{DERIBIT_BASE}/get_book_summary_by_currency",
        params={"currency": currency, "kind": "option"},
        max_retries=2,
    )
    if not data or "result" not in data:
        return None
    return data["result"]


def _nearest_expiry(items: list) -> Optional[str]:
    """Берём ближайшую серию (наиболее жидкая для краткосрочного skew)."""
    from collections import Counter
    exps = Counter()
    for it in items:
        try:
            exp = it["instrument_name"].split("-")[1]
            if it.get("open_interest", 0) > 0:
                exps[exp] += 1
        except Exception:
            continue
    if not exps:
        return None
    return exps.most_common(1)[0][0]


def _calc_skew(items: list, expiry: str) -> Optional[dict]:
    """
    25-delta skew = avg(put_iv where delta≈-0.25) - avg(call_iv where delta≈+0.25).
    Deribit не отдаёт delta напрямую в book_summary — используем proxy через strike.
    Для production: подтянуть данные через /get_ticker для топ-5 пут/колл.
    """
    # Простая прокси: берём ATM и сравниваем mid_iv put vs call
    series = [i for i in items if expiry in i["instrument_name"]]
    if not series:
        return None

    # underlying_price из любого инструмента
    underlying = None
    for s in series:
        if s.get("underlying_price"):
            underlying = float(s["underlying_price"])
            break
    if not underlying:
        return None

    puts, calls = [], []
    for s in series:
        try:
            parts = s["instrument_name"].split("-")
            strike = float(parts[2])
            otype = parts[3]   # "P" or "C"
            iv = s.get("mark_iv") or s.get("bid_iv") or 0
            if iv <= 0:
                continue
            moneyness = strike / underlying
            # Берём 25-delta proxy: puts на 0.85-0.95 underlying, calls на 1.05-1.15
            if otype == "P" and 0.85 <= moneyness <= 0.95:
                puts.append(iv)
            elif otype == "C" and 1.05 <= moneyness <= 1.15:
                calls.append(iv)
        except (ValueError, IndexError, KeyError):
            continue

    if not puts or not calls:
        return None

    put_iv = sum(puts) / len(puts)
    call_iv = sum(calls) / len(calls)
    skew = put_iv - call_iv

    # ATM IV
    atm_ivs = []
    for s in series:
        try:
            strike = float(s["instrument_name"].split("-")[2])
            if 0.97 <= strike / underlying <= 1.03:
                iv = s.get("mark_iv") or 0
                if iv > 0:
                    atm_ivs.append(iv)
        except Exception:
            continue
    atm_iv = sum(atm_ivs) / len(atm_ivs) if atm_ivs else 0

    return {
        "expiry":      expiry,
        "skew_25d":    round(skew, 2),
        "put_iv_25d":  round(put_iv, 2),
        "call_iv_25d": round(call_iv, 2),
        "atm_iv":      round(atm_iv, 2),
        "underlying":  underlying,
        "signal":      _classify(skew),
    }


def _classify(skew: float) -> str:
    if skew > 10:
        return "🟢 ПАНИКА_ПУТЫ — contrarian bullish"
    if skew > 5:
        return "🟢 хедж_в_путах"
    if skew < -3:
        return "🔴 FROTH_КОЛЛЫ — contrarian bearish"
    if skew < 0:
        return "🔴 премия_в_коллах"
    return "⚪ норма"


async def get_btc_eth_skew() -> dict:
    btc_task = asyncio.create_task(_book_summary("BTC"))
    eth_task = asyncio.create_task(_book_summary("ETH"))
    btc, eth = await asyncio.gather(btc_task, eth_task)

    out = {}
    for ccy, items in (("BTC", btc), ("ETH", eth)):
        if not items:
            continue
        exp = _nearest_expiry(items)
        if exp:
            out[ccy] = _calc_skew(items, exp)
    return out


if __name__ == "__main__":
    import sys
    import json
    logging.basicConfig(level=logging.INFO)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    async def _demo():
        r = await get_btc_eth_skew()
        print(json.dumps(r, indent=2, ensure_ascii=False))

    asyncio.run(_demo())

"""
Бесплатные источники рыночного контекста (без API ключей):

- Farside — дневные потоки BTC/ETH spot ETF (HTML scrape).
- ForexFactory — макро-календарь на неделю (публичный JSON-зеркало faireconomy).
- Deribit — опционы: Max Pain, put/call OI ratio (public API v2).

Fear & Greed реализован отдельно в screener.fetch_fear_greed().

Все функции возвращают None/{} при ошибке — никогда не бросают наружу.
Результаты кэшируются в .free_data_cache.json (TTL по источнику).
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

LOG = logging.getLogger("free_data")

CACHE_FILE = Path(__file__).parent / ".free_data_cache.json"
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121 Safari/537.36",
})

TTL_ETF   = 3600      # 1 час — Farside обновляется раз в день
TTL_MACRO = 6 * 3600  # 6 часов — календарь на неделю
TTL_OPT   = 15 * 60   # 15 минут — опционы двигаются быстрее
TTL_TREND = 30 * 60   # 30 минут — trending на CoinGecko обновляется небыстро
TTL_BNB   = 2 * 60    # 2 минуты — ордербук и ликвидации меняются быстро

BINANCE_FAPI = "https://fapi.binance.com"


def _cache_load() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def _cache_save(cache: dict) -> None:
    try:
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    except Exception as e:
        LOG.debug("cache save failed: %s", e)


def _cached(key: str, ttl: int, fetcher):
    cache = _cache_load()
    entry = cache.get(key)
    now = time.time()
    if entry and (now - entry.get("ts", 0)) < ttl:
        return entry["data"]
    try:
        data = fetcher()
    except Exception as e:
        LOG.warning("%s fetch failed: %s", key, e)
        return entry["data"] if entry else None
    cache[key] = {"ts": now, "data": data}
    _cache_save(cache)
    return data


# ─── Farside ETF ────────────────────────────────────────────────────────────
def _parse_farside_last_row(html: str) -> Optional[dict]:
    """
    Farside отдаёт HTML-таблицу. Находим последнюю строку с датой и total (M$).
    Формат ячеек: "123.4" (M$), "-", "(12.3)" = отрицательное.
    """
    m = re.search(r"<tbody[^>]*>(.*?)</tbody>", html, re.DOTALL | re.IGNORECASE)
    body = m.group(1) if m else html
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.DOTALL | re.IGNORECASE)

    date_re = re.compile(r"\d{1,2}\s+\w{3}\s+\d{4}")
    for row in reversed(rows):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL | re.IGNORECASE)
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        if not cells:
            continue
        if not date_re.search(cells[0]):
            continue
        total_raw = cells[-1].replace(",", "").replace("$", "").strip()
        if not total_raw or total_raw == "-":
            continue
        negative = total_raw.startswith("(") and total_raw.endswith(")")
        num = re.sub(r"[()$M ]", "", total_raw)
        try:
            val = float(num)
            if negative:
                val = -val
            return {"date": cells[0], "net_flow_m": val}
        except ValueError:
            continue
    return None


def _fetch_etf_btc():
    r = SESSION.get("https://farside.co.uk/bitcoin-etf-flow-all-data/", timeout=10)
    r.raise_for_status()
    return _parse_farside_last_row(r.text)


def _fetch_etf_eth():
    r = SESSION.get("https://farside.co.uk/ethereum-etf-flow-all-data/", timeout=10)
    r.raise_for_status()
    return _parse_farside_last_row(r.text)


def get_etf_flows() -> dict:
    """
    Возвращает {'btc': {'date': ..., 'net_flow_m': ±X}, 'eth': {...}}.
    net_flow_m — чистый приток/отток за последний торговый день, млн $.
    Положительное = приток (бычий), отрицательное = отток (медвежий).
    """
    btc = _cached("etf_btc", TTL_ETF, _fetch_etf_btc)
    eth = _cached("etf_eth", TTL_ETF, _fetch_etf_eth)
    return {"btc": btc, "eth": eth}


# ─── ForexFactory макро-календарь ───────────────────────────────────────────
def _fetch_macro_raw():
    r = SESSION.get(
        "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def get_macro_calendar(impact_min: str = "High", countries=("USD",)) -> list[dict]:
    """
    Возвращает список макро-событий на неделю.
    Каждый event: {title, country, date (ISO UTC), impact, forecast, previous, actual}.
    По умолчанию — только High-impact события США (FOMC, CPI, NFP, PCE и т.д.).
    """
    raw = _cached("macro_ff", TTL_MACRO, _fetch_macro_raw) or []
    impact_rank = {"Low": 0, "Medium": 1, "High": 2, "Holiday": -1}
    min_rank = impact_rank.get(impact_min, 2)

    events = []
    for ev in raw:
        country = ev.get("country", "")
        impact = ev.get("impact", "")
        if impact_rank.get(impact, -1) < min_rank:
            continue
        if countries and country not in countries:
            continue
        date_str = ev.get("date", "")
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            iso = dt.astimezone(timezone.utc).isoformat()
        except Exception:
            iso = date_str
        events.append({
            "title":    ev.get("title", ""),
            "country":  country,
            "date":     iso,
            "impact":   impact,
            "forecast": ev.get("forecast", ""),
            "previous": ev.get("previous", ""),
            "actual":   ev.get("actual", ""),
        })
    events.sort(key=lambda e: e["date"])
    return events


def next_macro_window(minutes_before: int = 30, minutes_after: int = 15) -> Optional[dict]:
    """
    Возвращает ближайшее High-impact US событие, если мы находимся в окне
    [event - minutes_before ; event + minutes_after] минут, иначе None.
    Используется как фильтр алертов: не входить прямо перед FOMC/CPI.
    """
    events = get_macro_calendar(impact_min="High", countries=("USD",))
    now = datetime.now(timezone.utc)
    for ev in events:
        try:
            ev_time = datetime.fromisoformat(ev["date"])
        except Exception:
            continue
        delta_min = (ev_time - now).total_seconds() / 60
        if -minutes_after <= delta_min <= minutes_before:
            return {**ev, "minutes_until": round(delta_min, 1)}
    return None


# ─── Deribit опционы ────────────────────────────────────────────────────────
def _fetch_deribit_summary(currency: str):
    r = SESSION.get(
        "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
        params={"currency": currency, "kind": "option"},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("result", [])


def _fetch_deribit_index(currency: str) -> Optional[float]:
    r = SESSION.get(
        "https://www.deribit.com/api/v2/public/get_index_price",
        params={"index_name": f"{currency.lower()}_usd"},
        timeout=5,
    )
    r.raise_for_status()
    return r.json().get("result", {}).get("index_price")


def _max_pain(contracts: list[dict], strikes: list[float]) -> Optional[float]:
    """
    Max Pain: страйк с минимальной суммарной выплатой держателям опционов.
    Считаем только ближайшую экспирацию (отдельно считает вызывающая сторона).
    """
    if not contracts or not strikes:
        return None
    best_strike = None
    best_pain = float("inf")
    for s in strikes:
        pain = 0.0
        for c in contracts:
            strike = c.get("strike", 0)
            oi = c.get("open_interest", 0) or 0
            opt_type = c.get("option_type", "")
            if opt_type == "call" and s > strike:
                pain += (s - strike) * oi
            elif opt_type == "put" and s < strike:
                pain += (strike - s) * oi
        if pain < best_pain:
            best_pain = pain
            best_strike = s
    return best_strike


def _pick_nearest_expiry(summary: list[dict]) -> list[dict]:
    """Фильтрует контракты ближайшей экспирации (по символу инструмента)."""
    expiries = {}
    for c in summary:
        name = c.get("instrument_name", "")
        parts = name.split("-")
        if len(parts) < 4:
            continue
        exp_str = parts[1]
        try:
            dt = datetime.strptime(exp_str, "%d%b%y")
        except ValueError:
            continue
        dt = dt.replace(tzinfo=timezone.utc)
        if dt < datetime.now(timezone.utc) - timedelta(hours=12):
            continue
        expiries.setdefault(dt, []).append({
            "instrument_name": name,
            "strike": float(parts[2]),
            "option_type": "call" if parts[3] == "C" else "put",
            "open_interest": c.get("open_interest", 0),
            "volume": c.get("volume", 0),
        })
    if not expiries:
        return []
    nearest = min(expiries.keys())
    return expiries[nearest]


def _fetch_options(currency: str) -> Optional[dict]:
    summary = _fetch_deribit_summary(currency)
    if not summary:
        return None
    index_price = _fetch_deribit_index(currency)

    contracts = _pick_nearest_expiry(summary)
    if not contracts:
        return None

    strikes = sorted({c["strike"] for c in contracts})
    mp = _max_pain(contracts, strikes)

    call_oi = sum(c["open_interest"] for c in contracts if c["option_type"] == "call")
    put_oi  = sum(c["open_interest"] for c in contracts if c["option_type"] == "put")
    pcr = (put_oi / call_oi) if call_oi > 0 else None

    expiry_name = contracts[0]["instrument_name"].split("-")[1]

    return {
        "currency":    currency,
        "index_price": index_price,
        "expiry":      expiry_name,
        "max_pain":    mp,
        "call_oi":     call_oi,
        "put_oi":      put_oi,
        "pcr":         round(pcr, 2) if pcr else None,
    }


def get_options_context(currency: str = "BTC") -> Optional[dict]:
    """
    Контекст опционов Deribit по ближайшей экспирации.
    Возвращает: {index_price, expiry, max_pain, call_oi, put_oi, pcr}.
    pcr > 1 — больше путов (медвежий sentiment / хедж).
    pcr < 0.7 — сильный бычий sentiment.
    max_pain — цена притяжения перед экспирацией.
    """
    return _cached(f"opt_{currency}", TTL_OPT, lambda: _fetch_options(currency))


# ─── CoinGecko Trending ─────────────────────────────────────────────────────
def _fetch_trending_raw():
    r = SESSION.get(
        "https://api.coingecko.com/api/v3/search/trending",
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def get_trending(limit_coins: int = 7, limit_categories: int = 5) -> dict:
    """
    Возвращает trending монеты и категории по CoinGecko.
    Ключ 'coins': [{symbol, name, market_cap_rank}, ...] — от самых хайповых.
    Ключ 'categories': [{name, change_1h_pct}, ...] — сектора, ротирующие сейчас.

    Используй: если альт из скринера попал в trending — социальный хайп подтверждён.
    """
    raw = _cached("trending_cg", TTL_TREND, _fetch_trending_raw) or {}

    coins = []
    for c in raw.get("coins", [])[:limit_coins]:
        item = c.get("item", {})
        sym = (item.get("symbol") or "").upper()
        if not sym:
            continue
        coins.append({
            "symbol":          sym,
            "name":            item.get("name", ""),
            "market_cap_rank": item.get("market_cap_rank"),
        })

    categories = []
    for cat in raw.get("categories", [])[:limit_categories]:
        categories.append({
            "name":           cat.get("name", ""),
            "change_1h_pct":  cat.get("market_cap_1h_change", 0) or 0,
            "change_24h_pct": cat.get("market_cap_24h_change", 0) or 0,
        })

    return {"coins": coins, "categories": categories}


# ─── CoinGecko Global Market Data ───────────────────────────────────────────
TTL_GLOBAL = 900  # 15 минут — доминация меняется медленно


def _fetch_global_raw() -> dict:
    r = SESSION.get("https://api.coingecko.com/api/v3/global", timeout=10)
    r.raise_for_status()
    return r.json().get("data", {})


def get_btc_dominance() -> Optional[float]:
    """
    BTC market cap dominance % (0-100) из CoinGecko.

    Интерпретация:
    > 52%: BTC season — деньги стекаются в BTC, альты теряют bid → penalty alt longs
    < 47%: Alt season — ротация из BTC в альты → boost alt longs / squeeze
    47-52%: нейтральная зона

    Кэш 15 минут, бесплатно, без API-ключа.
    """
    raw = _cached("cg_global", TTL_GLOBAL, _fetch_global_raw)
    if not raw:
        return None
    return raw.get("market_cap_percentage", {}).get("btc")


def is_trending(symbol: str) -> bool:
    """
    Проверяет, находится ли символ (например 'PEPE' или 'PEPEUSDT') в trending.
    Сравнение case-insensitive по префиксу символа без суффикса USDT.
    """
    base = symbol.upper().replace("USDT", "").replace("PERP", "").strip()
    if not base:
        return False
    try:
        trend = get_trending()
    except Exception:
        return False
    return any(c["symbol"] == base for c in trend.get("coins", []))


# ─── CryptoPanic новостной сентимент ────────────────────────────────────────
TTL_PANIC  = 15 * 60   # 15 минут — новости появляются часто
TTL_LUNAR  = 30 * 60   # 30 минут — социальные метрики меняются медленнее


def _get_cryptopanic_token() -> Optional[str]:
    import os
    return os.environ.get("CRYPTOPANIC_API_KEY") or None


def _fetch_cryptopanic_raw(currencies: str) -> list:
    """Загружает свежие новости CryptoPanic для списка монет."""
    token = _get_cryptopanic_token()
    if not token:
        return []
    r = SESSION.get(
        "https://cryptopanic.com/api/v1/posts/",
        params={
            "auth_token": token,
            "currencies": currencies,
            "filter":     "hot",
            "public":     "true",
            "kind":       "news",
        },
        timeout=10,
    )
    r.raise_for_status()
    return r.json().get("results", [])


def get_cryptopanic_news(symbols: list, max_age_h: float = 4) -> dict:
    """
    Сентимент новостей из CryptoPanic для заданных символов.
    Возвращает {base_symbol: {"score": int, "hot": bool, "titles": [str]}}
    score > 0: бычий (больше позитивных голосов)
    score < 0: медвежий
    Требует CRYPTOPANIC_API_KEY в .env. Без ключа возвращает {}.
    """
    if not _get_cryptopanic_token():
        return {}

    # Конвертируем BTCUSDT → BTC
    bases = [s.upper().replace("USDT", "").replace("PERP", "") for s in symbols]
    # API принимает максимум ~10 монет за запрос
    result: dict = {}
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_h * 3600

    for chunk in [bases[i:i+8] for i in range(0, len(bases), 8)]:
        key = f"cryptopanic_{'_'.join(sorted(chunk))}"
        posts = _cached(key, TTL_PANIC, lambda c=",".join(chunk): _fetch_cryptopanic_raw(c)) or []
        for post in posts:
            # Фильтр по возрасту
            pub = post.get("published_at", "")
            try:
                pub_ts = datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
                if pub_ts < cutoff:
                    continue
            except Exception:
                pass

            votes    = post.get("votes", {})
            positive = int(votes.get("positive", 0) or 0)
            negative = int(votes.get("negative", 0) or 0)
            score_d  = positive - negative
            is_hot   = (post.get("is_hot") or votes.get("important", 0) or 0) > 0
            title    = post.get("title", "")[:80]

            for currency in (post.get("currencies") or []):
                sym = (currency.get("code") or "").upper()
                if sym not in chunk:
                    continue
                if sym not in result:
                    result[sym] = {"score": 0, "hot": False, "titles": []}
                result[sym]["score"] += score_d
                if is_hot:
                    result[sym]["hot"] = True
                if title and len(result[sym]["titles"]) < 3:
                    result[sym]["titles"].append(title)

    return result


def _get_lunarcrush_token() -> Optional[str]:
    import os
    return os.environ.get("LUNARCRUSH_API_KEY") or None


def _fetch_lunarcrush_raw(symbol: str) -> Optional[dict]:
    """LunarCrush API v4 — данные монеты: galaxy_score, alt_rank, sentiment."""
    token = _get_lunarcrush_token()
    if not token:
        return None
    r = SESSION.get(
        f"https://lunarcrush.com/api4/public/coins/{symbol}/v1",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json().get("data", {})


def get_lunarcrush_coin(symbol: str) -> Optional[dict]:
    """
    Социальные метрики LunarCrush для символа (например 'BTC').
    Возвращает: {galaxy_score, alt_rank, sentiment, social_volume_24h} или None.
    galaxy_score 0-100: выше = сильнее социальный импульс
    alt_rank: ранг среди альтов по социальной активности (1 = лучший)
    sentiment 0-100: 50 = нейтрально, >70 = бычий, <30 = медвежий
    Требует LUNARCRUSH_API_KEY в .env.
    """
    if not _get_lunarcrush_token():
        return None
    base = symbol.upper().replace("USDT", "").replace("PERP", "")
    key  = f"lunar_{base}"
    raw  = _cached(key, TTL_LUNAR, lambda: _fetch_lunarcrush_raw(base))
    if not raw:
        return None
    return {
        "galaxy_score":     raw.get("galaxy_score"),
        "alt_rank":         raw.get("alt_rank"),
        "sentiment":        raw.get("sentiment"),
        "social_volume_24h": raw.get("social_volume_24h"),
    }


def get_social_context(symbols: list) -> dict:
    """
    Сводный социальный сентимент для списка символов.
    Возвращает {base_symbol: {cryptopanic: {...}, lunarcrush: {...}}}.
    Работает с любым набором доступных ключей (graceful degradation).
    """
    news = get_cryptopanic_news(symbols) if _get_cryptopanic_token() else {}
    result = {}
    for sym in symbols:
        base = sym.upper().replace("USDT", "").replace("PERP", "")
        entry: dict = {}
        if base in news:
            entry["cryptopanic"] = news[base]
        lc = get_lunarcrush_coin(base) if _get_lunarcrush_token() else None
        if lc:
            entry["lunarcrush"] = lc
        if entry:
            result[base] = entry
    return result


# ─── Binance per-symbol enrichment ────────────────────────────────────────────

def _bnb_get(path: str, params: dict | None = None) -> Optional[dict | list]:
    try:
        r = SESSION.get(f"{BINANCE_FAPI}{path}", params=params, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _fetch_bnb_book(bnb_sym: str) -> Optional[dict]:
    """Top-20 order book → bid/ask volumes and imbalance."""
    data = _bnb_get("/fapi/v1/depth", {"symbol": bnb_sym, "limit": 20})
    if not isinstance(data, dict):
        return None
    try:
        bid_vol = sum(float(p) * float(q) for p, q in data.get("bids", []))
        ask_vol = sum(float(p) * float(q) for p, q in data.get("asks", []))
        total = bid_vol + ask_vol
        imbalance = (bid_vol - ask_vol) / total if total > 0 else 0.0
        return {
            "bid_vol_usdt": round(bid_vol, 0),
            "ask_vol_usdt": round(ask_vol, 0),
            "book_imbalance": round(imbalance, 4),  # +1=all bids, -1=all asks
        }
    except (ValueError, TypeError):
        return None


def _fetch_bnb_taker_ratio(bnb_sym: str) -> Optional[dict]:
    """
    Binance taker buy/sell ratio (last 5m) + top-trader long/short position ratio.
    Taker buySellRatio > 1 = aggressive buying (bullish pressure).
    topLongShortRatio < 1 = top traders net short (potential squeeze fuel).
    """
    taker = _bnb_get(
        "/futures/data/takerlongshortRatio",
        {"symbol": bnb_sym, "period": "5m", "limit": 3},
    )
    top_pos = _bnb_get(
        "/futures/data/topLongShortPositionRatio",
        {"symbol": bnb_sym, "period": "5m", "limit": 3},
    )

    result: dict = {}
    if isinstance(taker, list) and taker:
        try:
            latest = taker[-1]
            result["taker_buy_sell_ratio"] = round(float(latest["buySellRatio"]), 4)
            result["taker_buy_vol"]  = round(float(latest["buyVol"]), 2)
            result["taker_sell_vol"] = round(float(latest["sellVol"]), 2)
        except (KeyError, ValueError, TypeError):
            pass

    if isinstance(top_pos, list) and top_pos:
        try:
            latest = top_pos[-1]
            result["top_ls_ratio"]    = round(float(latest["longShortRatio"]), 4)
            result["top_long_pct"]    = round(float(latest["longAccount"]) * 100, 2)
            result["top_short_pct"]   = round(float(latest["shortAccount"]) * 100, 2)
        except (KeyError, ValueError, TypeError):
            pass

    return result or None


def _fetch_bnb_enrichment(bnb_sym: str) -> Optional[dict]:
    """Combines order book imbalance + taker pressure for a single symbol."""
    book  = _fetch_bnb_book(bnb_sym)
    taker = _fetch_bnb_taker_ratio(bnb_sym)
    if book is None and taker is None:
        return None
    return {**(book or {}), **(taker or {})}


# Bybit → Binance symbol mapping (same exceptions as binance_bridge.py)
_BNB_SYM_MAP: dict[str, Optional[str]] = {
    "SHIB1000USDT":  "1000SHIBUSDT",
    "PEPE1000USDT":  "1000PEPEUSDT",
    "FLOKI1000USDT": "1000FLOKIUSDT",
    "TAOBYBIT":       None,
    "BTCPERP":        "BTCUSDT",
    "ETHPERP":        "ETHUSDT",
}


def _bybit_sym_to_binance(sym: str) -> Optional[str]:
    return _BNB_SYM_MAP.get(sym, sym)


def get_binance_enrichment(symbol: str) -> Optional[dict]:
    """
    Per-symbol Binance Futures enrichment: order book imbalance + recent liquidations.

    symbol — Bybit-style symbol (e.g. "BTCUSDT", "SOLUSDT").

    Returns:
      {
        "bid_vol_usdt":       float,  # top-20 bid depth in USDT
        "ask_vol_usdt":       float,  # top-20 ask depth in USDT
        "book_imbalance":     float,  # (bids-asks)/(bids+asks), +1=all bids
        "taker_buy_sell_ratio": float,  # >1 = aggressive buying pressure
        "taker_buy_vol":      float,  # taker buy volume (base, last 5m)
        "taker_sell_vol":     float,  # taker sell volume (base, last 5m)
        "top_ls_ratio":       float,  # top traders long/short ratio (<1 = net short)
        "top_long_pct":       float,  # % of top traders holding longs
        "top_short_pct":      float,  # % of top traders holding shorts
      }

    Returns None on error or when Binance doesn't list the symbol.
    Results are cached for TTL_BNB (2 min) to avoid hammering the API.
    """
    bnb_sym = _bybit_sym_to_binance(symbol)
    if not bnb_sym:
        return None
    key = f"bnb_enrich_{bnb_sym}"
    return _cached(key, TTL_BNB, lambda: _fetch_bnb_enrichment(bnb_sym))


# ─── CLI для быстрой проверки ───────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("\n=== ETF flows (Farside) ===")
    print(json.dumps(get_etf_flows(), ensure_ascii=False, indent=2))

    print("\n=== Macro calendar — High impact USD, неделя ===")
    for ev in get_macro_calendar():
        print(f"  {ev['date']}  {ev['impact']:<6} {ev['title']}"
              f"  (f:{ev['forecast']} p:{ev['previous']})")

    print("\n=== Near-term macro window (±30/15 min) ===")
    win = next_macro_window()
    print("  В окне:", win if win else "нет")

    print("\n=== BTC options (Deribit, ближайшая экспирация) ===")
    print(json.dumps(get_options_context("BTC"), ensure_ascii=False, indent=2))
    print("\n=== ETH options ===")
    print(json.dumps(get_options_context("ETH"), ensure_ascii=False, indent=2))

    print("\n=== CoinGecko Trending ===")
    print(json.dumps(get_trending(), ensure_ascii=False, indent=2))

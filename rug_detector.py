"""
rug_detector.py — автоматическое обнаружение rug pull / insider dump.

Источники (все бесплатные):
  • CoinGecko API    — supply ratio, листинги, контракты, возраст токена
  • DexScreener API  — DEX ликвидность, пары, цена (без ключа)
  • Etherscan V2 API — on-chain крупные переводы на биржи (ETH)
  • Bybit API        — памп без OI (уже есть в screener)

Запуск standalone:
    python3 rug_detector.py BTCUSDT
    python3 rug_detector.py BSBUSDT

Автоматически вызывается из signal_monitor.py при каждом новом сигнале.
"""

from __future__ import annotations

import os
import time
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ── API endpoints ─────────────────────────────────────────────────────────────
_ETHERSCAN_KEY = ""
_ES_V2         = "https://api.etherscan.io/v2/api"
_CG            = "https://api.coingecko.com/api/v3"
_DS            = "https://api.dexscreener.com/latest/dex"

# ── DeFiLlama ────────────────────────────────────────────────────────────────
_LLAMA           = "https://api.llama.fi"
_llama_cache: list = []
_llama_cache_ts: float = 0.0
LLAMA_CACHE_TTL  = 600   # 10 мин — список 7000 протоколов, не надо часто
TVL_DROP_1D_WARN = -30.0  # % за 24ч → серьёзно
TVL_DROP_7D_WARN = -50.0  # % за 7 дней → критично

# ── News RSS feeds (бесплатно, без ключа) ────────────────────────────────────
NEWS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
    "https://thedefiant.io/feed",
]

def _load_key():
    global _ETHERSCAN_KEY
    if _ETHERSCAN_KEY:
        return
    env = Path(__file__).parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("ETHERSCAN_API_KEY="):
                _ETHERSCAN_KEY = line.split("=", 1)[1].strip()
    if not _ETHERSCAN_KEY:
        _ETHERSCAN_KEY = os.getenv("ETHERSCAN_API_KEY", "")

_load_key()

# ── Known exchange hot wallets (ETH mainnet) ─────────────────────────────────
# Публично известные адреса — аналог Arkham labels для топ-бирж
EXCHANGE_ADDRS: dict[str, str] = {
    # Binance
    "0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be": "Binance",
    "0xd551234ae421e3bcba99a0da6d736074f22192ff": "Binance",
    "0x564286362092d8e7936f0549571a803b203aaced": "Binance",
    "0x0681d8db095565fe8a346fa0277bffde9c0edbbf": "Binance",
    "0x4e9ce36e442e55ecd9025b9a6e0d88485d628a67": "Binance",
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance",
    "0xbe0eb53f46cd790cd13851d5eff43d12404d33e8": "Binance (cold)",
    # Bybit
    "0xf89d7b9c86c20b682efa5f5b3c4d9f4d8e7e72b6": "Bybit",
    "0x77134cbc06cb00b66f4c7e623d5fdbf6777635ec": "Bybit",
    "0x1db3439a222c519ab44bb1144fc28167b4fa6ee6": "Bybit",
    "0x9696f59e4d72e237be84ffd425dcad154bf96976": "Bybit",
    "0xf882818f4e95e6e32a27218ba5d6e97d5e54b88c": "Bybit",
    # OKX
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b": "OKX",
    "0x236f9f97e0e62388479bf9e5ba4889e46b0273c3": "OKX",
    "0xa7efae728d2936e78bda97dc267687568dd593f3": "OKX",
    # Kraken
    "0x2910543af39aba0cd09dbb2d50200b3e800a63d2": "Kraken",
    "0x267be1c1d684f78cb4f6a176c4911b741e4ffdc0": "Kraken",
    # Coinbase
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3": "Coinbase",
    "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43": "Coinbase",
    # Gate.io
    "0x0d0707963952f2fba59dd06f2b425ace40b492fe": "Gate.io",
    "0x7793cd85c11a924478d358d49b05b37e91b5810f": "Gate.io",
    # Huobi / HTX
    "0xaB5C66752a9e8167967685F1450532fB96d5d24f": "HTX",
    "0x6748f50f686bfbca6fe8ad62b22228b87f31ff2b": "HTX",
    # KuCoin
    "0x2b5634c42055806a59e9107ed44d43c426e58258": "KuCoin",
    "0x689c56aef474df92d44a1b70850f808488f9769c": "KuCoin",
    # MEXC
    "0x75e89d5979e4f6fba9f97c104f2af612b9b674f6": "MEXC",
    "0x0211f3cedbef3143223d3acf0e589747933e8527": "MEXC",
}

# ── VALIDATED_V1: статистически подтверждённые факторы дампов ─────────────────
# Источник: DUMP_PATTERNS_ANALYSIS.md (N=1473 событий, 100 символов, walk-forward OOS).
# НЕ включать до прохождения бэктеста AVEC-80. Решение пользователя.
USE_VALIDATED_V1 = False

# ── Thresholds ────────────────────────────────────────────────────────────────
SUPPLY_RATIO_MAX  = 30.0   # % circulating — если меньше, красный флаг
PUMP_7D_MIN       = 150.0  # % памп за 7 дней без основания
LISTINGS_30D_MIN  = 3      # кол-во крупных CEX за 30 дней
DEX_LIQ_MIN_USD   = 200_000
WHALE_TRANSFER_PCT= 0.30   # % от circ supply (0.3%) — формула делит на 100; 30.0 отключало бы детекцию

# Крупные CEX для подсчёта листингов
MAJOR_CEX = {"binance","bybit","okex","okx","kucoin","gate","mexc","huobi","htx",
             "coinbase","kraken","bitget","bingx","bitfinex","gemini"}

# ─────────────────────────────────────────────────────────────────────────────

def _get(url: str, params: dict = None, timeout: int = 10) -> Optional[dict]:
    try:
        r = requests.get(url, params=params, timeout=timeout)
        return r.json()
    except Exception:
        return None


# ── CoinGecko ─────────────────────────────────────────────────────────────────

def _cg_search(symbol: str) -> Optional[str]:
    """Returns CoinGecko coin ID for a Bybit symbol like 'BSBUSDT'."""
    base = symbol.replace("USDT", "").replace("PERP", "").lower()
    d = _get(f"{_CG}/search", {"query": base})
    if not d:
        return None
    coins = d.get("coins", [])
    # prefer exact symbol match
    for c in coins[:10]:
        if c.get("symbol", "").lower() == base:
            return c["id"]
    return coins[0]["id"] if coins else None


def _cg_coin_data(cg_id: str) -> Optional[dict]:
    return _get(
        f"{_CG}/coins/{cg_id}",
        {"localization": "false", "tickers": "true",
         "market_data": "true", "community_data": "false",
         "developer_data": "false"},
    )


def _check_supply(data: dict) -> tuple[float, float, list[str]]:
    """Returns (supply_ratio_pct, circulating, flags)."""
    flags = []
    md = data.get("market_data", {})
    circ  = float(md.get("circulating_supply") or 0)
    total = float(md.get("total_supply") or 0)
    if not total or not circ:
        return 100.0, circ, []
    ratio = circ / total * 100
    if ratio < SUPPLY_RATIO_MAX:
        flags.append(
            f"Supply: только {ratio:.1f}% в обращении "
            f"({circ/1e6:.1f}M / {total/1e6:.0f}M) — "
            f"{100-ratio:.0f}% у команды/вестинга"
        )
    return ratio, circ, flags


def _check_listings(data: dict, age_days: Optional[int] = None) -> tuple[int, list[str]]:
    """Count major CEX listings in last 30 days. Only flags young tokens (< 180 days)."""
    flags   = []
    # Established tokens naturally have many active tickers — only relevant for new ones
    if age_days is None or age_days >= 180:
        return 0, []
    tickers = data.get("tickers", [])
    cutoff  = datetime.now() - timedelta(days=30)
    recent  = 0
    for t in tickers:
        mkt = (t.get("market", {}).get("identifier") or "").lower()
        if not any(ex in mkt for ex in MAJOR_CEX):
            continue
        ts_str = t.get("last_fetch_at") or t.get("timestamp") or ""
        try:
            ts_clean = ts_str.replace("Z", "").split("+")[0]
            ts = datetime.fromisoformat(ts_clean)
            if ts >= cutoff:
                recent += 1
        except Exception:
            recent += 1  # если нет даты — считаем свежим
    if recent >= LISTINGS_30D_MIN:
        flags.append(
            f"Листинги: {recent} крупных CEX за последние 30 дней — "
            f"организованный листинг для выхода"
        )
    return recent, flags


def _check_price_action(data: dict) -> list[str]:
    """Parabolic pump from CoinGecko 7d price change."""
    flags = []
    md = data.get("market_data", {})
    chg7d = float((md.get("price_change_percentage_7d") or 0))
    chg30d= float((md.get("price_change_percentage_30d") or 0))
    if chg7d >= PUMP_7D_MIN:
        flags.append(
            f"Памп: +{chg7d:.0f}% за 7 дней "
            f"(30d: {chg30d:+.0f}%) — параболический рост без катализатора"
        )
    return flags


def _token_age_days(data: dict) -> Optional[int]:
    gd = data.get("genesis_date")
    if not gd:
        # fallback: ATH date as proxy
        try:
            ath_str = data.get("market_data", {}).get("ath_date", {}).get("usd", "")
            if ath_str:
                # aware-parse: aware−naive кидает TypeError, except глотал → age всегда None
                ath = datetime.fromisoformat(ath_str[:10]).replace(tzinfo=timezone.utc)
                return (datetime.now(timezone.utc) - ath).days
        except Exception:
            pass
        return None
    try:
        genesis = datetime.strptime(gd, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - genesis).days
    except Exception:
        return None


# ── DexScreener ──────────────────────────────────────────────────────────────

def _dex_data(symbol: str) -> tuple[float, list[str]]:
    """Returns (min_liquidity_usd, flags)."""
    base = symbol.replace("USDT", "").replace("PERP", "")
    d = _get(f"{_DS}/search", {"q": base})
    if not d:
        return 0.0, []

    pairs = [p for p in (d.get("pairs") or [])
             if p.get("baseToken", {}).get("symbol", "").upper() == base.upper()]
    if not pairs:
        return 0.0, []

    flags = []
    liqs  = [float(p.get("liquidity", {}).get("usd", 0) or 0) for p in pairs]
    total_liq = sum(liqs)
    max_liq   = max(liqs) if liqs else 0

    if 0 < max_liq < DEX_LIQ_MIN_USD:
        flags.append(
            f"DEX ликвидность: всего ${max_liq:,.0f} в лучшей паре — "
            f"выйти крупным объёмом легко"
        )

    # Price change on DEX — acute crash signal (ignore if well-established liquidity)
    changes = [float(p.get("priceChange", {}).get("h24", 0) or 0) for p in pairs]
    worst   = min(changes) if changes else 0
    if worst < -25 and max_liq < 50_000_000:
        flags.append(f"DEX крэш: {worst:.0f}% за 24ч — активный дамп")

    return max_liq, flags


# ── Etherscan on-chain transfers ─────────────────────────────────────────────

def _eth_large_transfers(contract: str, circ_supply: float) -> list[str]:
    """Looks for large transfers to known exchange addresses."""
    if not _ETHERSCAN_KEY or not contract:
        return []

    d = _get(
        _ES_V2,
        {
            "chainid": "1",
            "module": "account",
            "action": "tokentx",
            "contractaddress": contract,
            "page": "1",
            "offset": "100",
            "sort": "desc",
            "apikey": _ETHERSCAN_KEY,
        },
    )
    txs = d.get("result", []) if d else []
    if not isinstance(txs, list):
        return []

    flags    = []
    whale_th = circ_supply * WHALE_TRANSFER_PCT / 100 if circ_supply > 0 else 0
    seen_ex  = {}  # exchange → total amount

    for tx in txs:
        try:
            decimals = int(tx.get("tokenDecimal", 18))
            val      = int(tx.get("value", "0")) / (10 ** decimals)
            to_addr  = tx.get("to", "").lower()
            fr_addr  = tx.get("from", "").lower()
        except Exception:
            continue

        ex_name = EXCHANGE_ADDRS.get(to_addr)

        # Large transfer to known exchange
        if ex_name and val > 0:
            seen_ex[ex_name] = seen_ex.get(ex_name, 0) + val

        # Any transfer > whale threshold (even to unknown address)
        if whale_th > 0 and val >= whale_th:
            dest = EXCHANGE_ADDRS.get(to_addr, to_addr[:12] + "…")
            flags.append(
                f"⛓ Крупный перевод: {val:,.0f} токенов "
                f"({val/circ_supply*100:.2f}% circ) → {dest}"
            )

    for ex_name, total in seen_ex.items():
        pct = total / circ_supply * 100 if circ_supply > 0 else 0
        if pct >= 0.1:
            flags.append(
                f"⛓ Команда/кит → {ex_name}: "
                f"{total:,.0f} токенов ({pct:.2f}% circ) — ГОТОВИТСЯ К ДАМПУ"
            )

    return flags


# ── DeFiLlama TVL check ───────────────────────────────────────────────────────

def _llama_protocols() -> list:
    global _llama_cache, _llama_cache_ts
    if time.time() - _llama_cache_ts < LLAMA_CACHE_TTL and _llama_cache:
        return _llama_cache
    d = _get(f"{_LLAMA}/protocols")
    if d and isinstance(d, list):
        _llama_cache    = d
        _llama_cache_ts = time.time()
    return _llama_cache


def _check_defi_tvl(symbol: str) -> tuple[float, list[str]]:
    """
    Returns (tvl_usd, flags).
    Flags if TVL collapsed significantly — strong rug/exit signal for DeFi tokens.
    """
    base  = symbol.replace("USDT", "").replace("PERP", "").upper()
    protos = _llama_protocols()
    if not protos:
        return 0.0, []

    # Find matching protocol (may have multiple entries — pick largest TVL)
    matches = [p for p in protos if (p.get("symbol") or "").upper() == base]
    if not matches:
        return 0.0, []
    p = max(matches, key=lambda x: x.get("tvl") or 0)

    tvl      = float(p.get("tvl") or 0)
    chg_1d   = float(p.get("change_1d") or 0)
    chg_7d   = float(p.get("change_7d") or 0)
    category = p.get("category", "")
    flags    = []

    if chg_1d <= TVL_DROP_1D_WARN:
        flags.append(
            f"DeFiLlama TVL: {chg_1d:+.1f}% за 24ч "
            f"(${tvl/1e6:.1f}M) [{category}] — ОБВАЛ ликвидности"
        )
    elif chg_7d <= TVL_DROP_7D_WARN:
        flags.append(
            f"DeFiLlama TVL: {chg_7d:+.1f}% за 7д "
            f"(${tvl/1e6:.1f}M) [{category}] — постепенный вывод"
        )
    elif tvl > 0 and tvl < 1_000_000 and category in ("Lending", "DEX", "Yield", "Bridge"):
        flags.append(
            f"DeFiLlama TVL: всего ${tvl:,.0f} [{category}] — микро-протокол, высокий риск"
        )

    return tvl, flags


# ── News check ───────────────────────────────────────────────────────────────

def check_news(symbol: str, hours: int = 48) -> list[dict]:
    """
    Searches RSS feeds for recent news mentioning the token symbol.
    Returns list of {title, url, published, source} dicts.
    """
    try:
        import feedparser
    except ImportError:
        return []

    import re
    base    = symbol.replace("USDT", "").replace("PERP", "").upper()
    # Match whole word, case-insensitive (e.g. SOL but not Soldier/Solar)
    pattern = re.compile(r'\b' + re.escape(base) + r'\b', re.IGNORECASE)
    # pub теперь aware-UTC → cutoff тоже aware-UTC (naive-local давал бы TypeError + сдвиг MSK)
    cutoff  = datetime.now(timezone.utc) - timedelta(hours=hours)
    results = []

    for feed_url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            src  = feed.feed.get("title", feed_url.split("/")[2])
            for entry in feed.entries:
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                if not pattern.search(title + " " + summary):
                    continue
                # Parse publish date
                pub = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    import calendar
                    pub = datetime.fromtimestamp(
                        calendar.timegm(entry.published_parsed),
                        tz=timezone.utc
                    )
                if pub and pub < cutoff:
                    continue
                results.append({
                    "title":     title,
                    "url":       entry.get("link", ""),
                    "published": pub.strftime("%d.%m %H:%M") if pub else "?",
                    "source":    src,
                })
        except Exception:
            continue

    return results


# ── Main analysis ─────────────────────────────────────────────────────────────

def analyze(symbol: str, verbose: bool = False) -> dict:
    """
    Full rug risk analysis for a symbol (e.g. 'BSBUSDT').

    Returns:
        {
          'symbol': str,
          'risk_score': int,        # 0-100
          'verdict': str,           # 'CLEAN' | 'WATCH' | 'SUSPICIOUS' | 'RUG_RISK'
          'flags': list[str],       # human-readable red flags
          'supply_ratio': float,    # % circulating
          'circ_supply': float,
          'dex_liquidity': float,
          'age_days': int | None,
          'cg_id': str | None,
        }
    """
    _load_key()
    flags = []

    # ── CoinGecko ─────────────────────────────────────────────────────────────
    cg_id        = _cg_search(symbol)
    cg_data      = _cg_coin_data(cg_id) if cg_id else {}
    supply_ratio = 100.0
    circ_supply  = 0.0
    contract_eth = ""

    age_days = _token_age_days(cg_data) if cg_data else None

    if cg_data:
        supply_ratio, circ_supply, sf = _check_supply(cg_data)
        flags += sf
        _, lf = _check_listings(cg_data, age_days=age_days)
        flags += lf
        flags += _check_price_action(cg_data)
        contract_eth = (cg_data.get("platforms") or {}).get("ethereum", "")
    if age_days is not None and age_days < 90:
        flags.append(
            f"Возраст токена: {age_days} дней — молодой проект, "
            f"историческая надёжность не подтверждена"
        )

    # ── DexScreener ──────────────────────────────────────────────────────────
    dex_liq, df = _dex_data(symbol)
    flags += df

    # ── DeFiLlama TVL ─────────────────────────────────────────────────────────
    llama_tvl, lf = _check_defi_tvl(symbol)
    flags += lf

    # ── Etherscan on-chain ────────────────────────────────────────────────────
    if contract_eth:
        ef = _eth_large_transfers(contract_eth, circ_supply)
        flags += ef

    # ── Score ─────────────────────────────────────────────────────────────────
    score = 0
    for f in flags:
        if "ГОТОВИТСЯ К ДАМПУ" in f or "команда" in f.lower():
            score += 40
        elif "Supply" in f and "%" in f:
            score += max(0, int((30 - supply_ratio) * 1.5))
        elif "Памп" in f:
            score += 20
        elif "Листинги" in f:
            score += 15
        elif "DEX крэш" in f:
            score += 25
        elif "DEX ликвидность" in f:
            score += 10
        elif "Возраст" in f:
            if age_days is not None and age_days < 30:
                score += 20
            else:
                score += 10
        elif "Крупный перевод" in f:
            score += 20
        elif "ОБВАЛ ликвидности" in f:
            score += 30
        elif "постепенный вывод" in f:
            score += 15
        elif "микро-протокол" in f:
            score += 10

    # Комбинированный бонус: молодой токен + низкий supply = классический пре-раг
    if (age_days is not None and age_days < 90
            and supply_ratio < 30):
        score += 20

    # ── USE_VALIDATED_V1: market-microstructure факторы дампов (параллельный путь) ─
    # Факторы из DUMP_PATTERNS_ANALYSIS.md. Флаг = False → блок пропускается.
    # Не использовать: support_break (p=0.578), macro_extreme (lift~1.0),
    #                  cvd_divergence (p=0.066), upper_wick_ratio (p=0.36).
    if USE_VALIDATED_V1:
        _v1_bybit = "https://api.bybit.com/v5/market/kline"

        # btc_cascade: BTC 1h drop ≥ 1.5% (адаптивный 3σ) → главный триггер (lift 5.12)
        # Режимный гейт: sideways/bull BTC → шорты точнее (lift 1.87/1.79 vs 1.43 bear)
        try:
            _btc_kl = requests.get(
                _v1_bybit,
                params={"category": "linear", "symbol": "BTCUSDT",
                        "interval": "60", "limit": "5"},
                timeout=6,
            ).json()["result"]["list"]
            if _btc_kl and len(_btc_kl) >= 2:
                # Bybit возвращает свечи в порядке убывания (newest first)
                _btc_now  = float(_btc_kl[0][4])
                _btc_1h   = float(_btc_kl[1][4])
                _btc_4h   = float(_btc_kl[-1][4]) if len(_btc_kl) >= 5 else _btc_1h
                _b1h_pct  = (_btc_now - _btc_1h) / _btc_1h * 100 if _btc_1h > 0 else 0.0
                _b4h_pct  = (_btc_now - _btc_4h) / _btc_4h * 100 if _btc_4h > 0 else 0.0

                # btc_cascade: 1h drop ≥ 1.5% → главный триггер (lift 5.12)
                if _b1h_pct <= -1.5:
                    score += 30
                    flags.append(
                        f"[V1] btc_cascade: BTC 1h {_b1h_pct:+.2f}% — главный триггер дампа (lift 5.12) +30"
                    )

                # Режимный гейт: sideways/bull точнее для шортов чем bear
                if _b4h_pct > 1.0:        # bull
                    score += 5
                    flags.append(
                        f"[V1] BTC bull-режим {_b4h_pct:+.1f}% — шорты точнее (lift 1.79) +5"
                    )
                elif _b4h_pct < -1.5:     # bear
                    score -= 5
                    flags.append(
                        f"[V1] BTC bear-режим {_b4h_pct:+.1f}% — шорты менее точны (lift 1.43) −5"
                    )
                else:                      # sideways
                    score += 8
                    flags.append(
                        f"[V1] BTC sideways-режим {_b4h_pct:+.1f}% — шорты точнее всего (lift 1.87) +8"
                    )
        except Exception:
            pass  # Bybit недоступен — пропускаем btc_cascade

        # vol_spike_count > 0 + vol_ratio_1h > 1 → ядро сигнала (lift 1.49 + 1.28)
        try:
            _sym_kl = requests.get(
                _v1_bybit,
                params={"category": "linear", "symbol": symbol.upper(),
                        "interval": "5", "limit": "25"},
                timeout=6,
            ).json()["result"]["list"]
            if _sym_kl and len(_sym_kl) >= 13:
                # Bybit newest-first → разворот для хронологического порядка
                _vols = [float(b[5]) for b in reversed(_sym_kl)]
                _vbase = sum(_vols[-13:-1]) / 12 if len(_vols) >= 13 else 1.0

                # vol_spike_count: свечи в последних 12 с vol > 2× baseline (lift 1.49)
                _v1_spike = sum(
                    1 for v in _vols[-13:-1]
                    if _vbase > 1e-8 and v > 2 * _vbase
                )
                if _v1_spike > 0:
                    score += 15
                    flags.append(
                        f"[V1] vol_spike_count={_v1_spike} — объёмные спайки 1h (lift 1.49) +15"
                    )

                # vol_ratio_1h > 1 (lift 1.28)
                _v1_vol_1h   = sum(_vols[-13:-1])
                _v1_ratio_1h = _v1_vol_1h / (12 * _vbase) if _vbase > 1e-8 else 0.0
                if _v1_ratio_1h > 1.0:
                    score += 10
                    flags.append(
                        f"[V1] vol_ratio_1h={_v1_ratio_1h:.2f} — повышенный объём 1h (lift 1.28) +10"
                    )
        except Exception:
            pass  # Bybit недоступен — пропускаем vol-факторы

    score = min(score, 100)

    if score >= 60:
        verdict = "RUG_RISK"
    elif score >= 35:
        verdict = "SUSPICIOUS"
    elif score >= 15:
        verdict = "WATCH"
    else:
        verdict = "CLEAN"

    result = {
        "symbol":       symbol,
        "risk_score":   score,
        "verdict":      verdict,
        "flags":        flags,
        "supply_ratio": round(supply_ratio, 1),
        "circ_supply":  circ_supply,
        "dex_liquidity": dex_liq,
        "llama_tvl":    llama_tvl,
        "age_days":     age_days,
        "cg_id":        cg_id,
    }

    if verbose:
        _print_report(result)

    return result


def _print_report(r: dict):
    VERDICT_ICON = {
        "RUG_RISK": "🚨", "SUSPICIOUS": "🔴", "WATCH": "🟡", "CLEAN": "🟢"
    }
    icon = VERDICT_ICON.get(r["verdict"], "⚪")
    print(f"\n{'='*60}")
    print(f"  {icon} RUG CHECK — {r['symbol']}  |  score={r['risk_score']}  [{r['verdict']}]")
    print(f"{'='*60}")
    print(f"  Supply:      {r['supply_ratio']:.1f}% в обращении")
    if r['dex_liquidity']:
        print(f"  DEX liq:     ${r['dex_liquidity']:,.0f}")
    if r.get('llama_tvl'):
        print(f"  DeFi TVL:    ${r['llama_tvl']:,.0f}")
    if r['age_days'] is not None:
        print(f"  Возраст:     {r['age_days']} дней")
    if r['flags']:
        print(f"\n  Флаги:")
        for f in r['flags']:
            print(f"    ⚠ {f}")
    else:
        print("  Флагов не найдено")
    print(f"{'='*60}\n")


# ── Telegram alert ────────────────────────────────────────────────────────────

def send_rug_alert(r: dict, cfg: dict = None):
    """Sends rug risk TG alert. Only for SUSPICIOUS / RUG_RISK verdicts."""
    if r["verdict"] not in ("SUSPICIOUS", "RUG_RISK"):
        return

    try:
        import telegram_alerts as _tg
        if cfg is None:
            cfg = _tg.load_config()
        token   = cfg.get("bot_token", "")
        chat_id = str(cfg.get("chat_id", ""))
        if not token or not chat_id:
            return

        from telegram_alerts import _send, _esc

        ICON = {"RUG_RISK": "🚨 RUG RISK", "SUSPICIOUS": "🔴 ПОДОЗРИТЕЛЬНО"}
        icon = ICON[r["verdict"]]

        lines = [
            f"{icon} — <b>{_esc(r['symbol'])}</b>  score={r['risk_score']}/100",
            "",
            f"Supply:  <b>{r['supply_ratio']:.1f}%</b> в обращении",
        ]
        if r["dex_liquidity"]:
            lines.append(f"DEX liq: <b>${r['dex_liquidity']:,.0f}</b>")
        if r["age_days"] is not None:
            lines.append(f"Возраст: <b>{r['age_days']} дней</b>")

        if r["flags"]:
            lines.append("")
            for f in r["flags"]:
                lines.append(f"⚠ {_esc(f)}")

        lines += [
            "",
            "⛔ <b>Не входить в ЛОНГ. Высокий риск insider dump.</b>",
        ]

        text = "\n".join(lines)
        _send(token, chat_id, text)

        for extra in cfg.get("extra_chat_ids", []):
            cid = str(extra)
            if cid != chat_id:
                _send(token, cid, text)

        # TV-разметка 15m на реальной ликвидности (раг = SHORT) — опционально,
        # за флагом TV_PLAN_ENABLED=1, graceful, без API. По умолчанию выкл.
        try:
            from tv_pump_plan import attach_tv_plan_to_tg
            attach_tv_plan_to_tg(r["symbol"], "SHORT", token, chat_id,
                                 caption=f"📊 {r['symbol']} 15m · TV-разметка (визуал)")
        except Exception:
            pass

    except Exception as e:
        print(f"[RugDetector] TG error: {e}")


# ── Standalone CLI ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    symbols = sys.argv[1:] or ["BSBUSDT", "BTCUSDT"]
    for sym in symbols:
        print(f"Анализирую {sym}…")
        result = analyze(sym, verbose=True)
        time.sleep(1.5)   # CoinGecko rate limit

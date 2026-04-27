"""
channel_reader.py — Читает сигналы и новости из Telegram каналов.

Каналы:
  @RoseSignalsPremium, @rose, @marketsAlpha,
  @hamaha_cryptodaytrading, @cryptoattack24

Читает последние сообщения, парсит сигналы/новости,
перекрёстно проверяет со скринером и отправляет в Telegram.

CLI:
  python3 channel_reader.py setup      — авторизация (один раз)
  python3 channel_reader.py scan       — читать каналы и отправить инсайты
  python3 channel_reader.py session    — напечатать session string (для GitHub Actions)
  python3 channel_reader.py test       — проверить подключение
"""

import asyncio
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests


# ─── Network utils ───────────────────────────────────────────────────────────

def _wait_for_telegram_network(max_wait: int = 600, interval: int = 30) -> bool:
    """
    Ждёт TCP-доступности api.telegram.org:443 до max_wait секунд.
    Нужно вызывать перед TelegramClient.connect() — защита от TimeoutError
    при запуске без сети (нет VPN / нет интернета).
    """
    host, port = "api.telegram.org", 443

    def _reachable():
        try:
            socket.create_connection((host, port), timeout=5).close()
            return True
        except OSError:
            return False

    if _reachable():
        return True
    print(f"[channel_reader] Telegram недоступен. Жду до {max_wait//60} мин...")
    elapsed = 0
    while elapsed < max_wait:
        time.sleep(interval)
        elapsed += interval
        if _reachable():
            print(f"[channel_reader] Telegram доступен (ожидал {elapsed}s)")
            return True
        print(f"[channel_reader] Ещё нет связи ({elapsed}s / {max_wait}s)...")
    print(f"[channel_reader] ОШИБКА: нет связи после {max_wait}s")
    return False

# ─── Пути ────────────────────────────────────────────────────────────────────

DIR         = Path(__file__).parent
CFG_PATH    = DIR / "channels_config.json"


# ─── .env loader ─────────────────────────────────────────────────────────────

def _load_dotenv():
    dotenv_path = DIR / ".env"
    if not dotenv_path.exists():
        return
    with open(dotenv_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip(); val = val.strip()
            if val and val[0] in ('"', "'") and val[-1] == val[0]:
                val = val[1:-1]
            os.environ.setdefault(key, val)


_load_dotenv()
CACHE_PATH  = DIR / "channel_signals_cache.json"
SCAN_CACHE  = DIR / "last_scan_cache.json"
TG_CFG_PATH = DIR / "telegram_config.json"

# ─── Каналы для мониторинга ───────────────────────────────────────────────────

CHANNELS = [
    "RoseSignalsPremium",
    "rose",
    "marketsAlpha",
    "hamaha_cryptodaytrading",
    "cryptoattack24",
    "AbuzikLudit",
    "afflerdao",
    "artypost",
    "bingx_ofical",
    "archfund",
    "nwsmkr",
]

# Сколько часов назад брать сообщения
LOOKBACK_HOURS = 8

# Каналы где анализируем скриншоты графиков через Claude Vision
VISION_CHANNELS = ["rose"]

# ─── Маппинг символов ────────────────────────────────────────────────────────

SYMBOL_MAP = {
    "BTC": "BTCUSDT", "BITCOIN": "BTCUSDT",
    "ETH": "ETHUSDT", "ETHEREUM": "ETHUSDT",
    "SOL": "SOLUSDT", "SOLANA": "SOLUSDT",
    "XRP": "XRPUSDT", "RIPPLE": "XRPUSDT",
    "BNB": "BNBUSDT",
    "DOGE": "DOGEUSDT", "DOGECOIN": "DOGEUSDT",
    "ADA": "ADAUSDT", "CARDANO": "ADAUSDT",
    "AVAX": "AVAXUSDT", "AVALANCHE": "AVAXUSDT",
    "MATIC": "MATICUSDT", "POLYGON": "MATICUSDT",
    "LINK": "LINKUSDT", "CHAINLINK": "LINKUSDT",
    "DOT": "DOTUSDT", "POLKADOT": "DOTUSDT",
    "UNI": "UNIUSDT", "UNISWAP": "UNIUSDT",
    "ATOM": "ATOMUSDT", "COSMOS": "ATOMUSDT",
    "LTC": "LTCUSDT", "LITECOIN": "LTCUSDT",
    "NEAR": "NEARUSDT",
    "ARB": "ARBUSDT", "ARBITRUM": "ARBUSDT",
    "OP": "OPUSDT", "OPTIMISM": "OPUSDT",
    "INJ": "INJUSDT",
    "SUI": "SUIUSDT",
    "APT": "APTUSDT", "APTOS": "APTUSDT",
    "FET": "FETUSDT",
    "WIF": "WIFUSDT",
    "PEPE": "PEPEUSDT",
    "FLOKI": "FLOKIUSDT",
    "BONK": "BONKUSDT",
    "TON": "TONUSDT",
    "TRX": "TRXUSDT", "TRON": "TRXUSDT",
    "AAVE": "AAVEUSDT",
    "RENDER": "RENDERUSDT",
    "TAO": "TAOUSDT",
    "ZEC": "ZECUSDT", "ZCASH": "ZECUSDT",
    "HBAR": "HBARUSDT", "HEDERA": "HBARUSDT",
    "ICP": "ICPUSDT",
    "FIL": "FILUSDT", "FILECOIN": "FILUSDT",
    "ALGO": "ALGOUSDT", "ALGORAND": "ALGOUSDT",
    "VET": "VETUSDT", "VECHAIN": "VETUSDT",
    "EGLD": "EGLDUSDT", "ELROND": "EGLDUSDT",
    "XLM": "XLMUSDT", "STELLAR": "XLMUSDT",
    "ETC": "ETCUSDT",
    "BCH": "BCHUSDT", "BITCOIN CASH": "BCHUSDT",
    "SAND": "SANDUSDT", "SANDBOX": "SANDUSDT",
    "MANA": "MANAUSDT", "DECENTRALAND": "MANAUSDT",
    "AXS": "AXSUSDT",
    "CRV": "CRVUSDT", "CURVE": "CRVUSDT",
    "MKR": "MKRUSDT", "MAKER": "MKRUSDT",
    "SNX": "SNXUSDT", "SYNTHETIX": "SNXUSDT",
    "LDO": "LDOUSDT", "LIDO": "LDOUSDT",
    "PENDLE": "PENDLEUSDT",
    "JUP": "JUPUSDT", "JUPITER": "JUPUSDT",
    "W": "WUSDT",
    "ENA": "ENAUSDT", "ETHENA": "ENAUSDT",
    "STRK": "STRKUSDT", "STARKNET": "STRKUSDT",
    "TIA": "TIAUSDT", "CELESTIA": "TIAUSDT",
    "SEI": "SEIUSDT",
    "PYTH": "PYTHUSDT",
    "MEME": "MEMEUSDT",
    "BRETT": "BRETTUSDT",
    "MOG": "MOGUSDT",
    "NEIRO": "NEIROUSDT",
}

# Ключевые слова направления
LONG_KW = [
    "long", "buy", "лонг", "покупка", "покупать", "бычий", "bullish",
    "лонгуем", "покупаю", "зашел в лонг", "входим в лонг",
    "рассматриваю лонг", "жду лонг", "лонг от", "лонг 🚀", "лонг 📈",
    "подбираем", "накапливаем", "набираем позицию", "в лонг",
    "🟢", "📈", "⬆️", "🚀",
]
SHORT_KW = [
    "short", "sell", "шорт", "продажа", "продавать", "медвежий", "bearish",
    "шортуем", "продаю", "зашел в шорт", "входим в шорт",
    "рассматриваю шорт", "жду шорт", "шорт от", "в шорт",
    "🔴", "📉", "⬇️",
]

# Ключевые слова новостей
NEWS_KW = [
    "фед", "fed", "пауэлл", "powell", "ставка", "rate", "etf", "etfs",
    "sec", "регулятор", "регуляция", "regulation", "hack", "взлом",
    "ликвидации", "liquidations", "whale", "кит", "нарратив", "narrative",
    "inflation", "инфляция", "cpi", "payroll", "gdp", "вбп",
    "halving", "халвинг", "airdrop", "listing", "делистинг",
    "breaking", "срочно", "🔥", "⚡", "⚠️", "важно",
    "обвал", "памп", "pump", "dump", "манипуляция",
    # Финансовые / on-chain метрики
    "funding", "open interest", " oi ", "oi up", "oi down",
    "short squeeze", "squeeze", "liquidat", "long squeeze",
    "flows", "inflow", "outflow", "etf flow",
    "whale", "крупный игрок", "институционал",
]

# Сентимент новостей
NEWS_BULLISH_KW = [
    "одобр", "approve", "листинг", "listing", "партнёрство", "партнерство",
    "partnership", "запуск", "launch", "интеграция", "integration",
    "халвинг", "halving", "накопление", "accumulation", "institutional",
    "институционал", "etf", "рекорд", "ath", "памп", "pump", "рост",
    "покупают", "аккумулируют",
]
NEWS_BEARISH_KW = [
    "взлом", "hack", "иск", "lawsuit", "запрет", "ban", "фуд", "fud",
    "взломали", "регулятор заблокировал", "sanction", "санкции",
    "обвал", "dump", "продажи", "банкротство", "банкрот", "делистинг",
    "sec против", "закрытие", "заморозка",
]

# Стоп-слова (реклама / мусор)
SPAM_KW = [
    "vip", "вип", "подписка", "subscription", "join",
    "вступай", "курс", "обучение", "school", "promo",
    "t.me/+", "https://t.me/+", "реферал", "referral",
    "скидка", "discount",
]


# ─── Конфигурация ─────────────────────────────────────────────────────────────

def load_cfg() -> dict:
    cfg: dict = {}
    if CFG_PATH.exists():
        cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    # Переменные окружения имеют приоритет над JSON-файлом
    api_id_env    = os.environ.get("TELEGRAM_API_ID")
    api_hash_env  = os.environ.get("TELEGRAM_API_HASH")
    session_env   = os.environ.get("TELEGRAM_SESSION_STRING")
    if api_id_env:
        try:
            cfg["api_id"] = int(api_id_env)
        except ValueError:
            pass
    if api_hash_env:
        cfg["api_hash"] = api_hash_env
    if session_env:
        cfg["session_string"] = session_env
    return cfg


def save_cfg(cfg: dict):
    CFG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_tg_cfg() -> dict:
    cfg: dict = {}
    if TG_CFG_PATH.exists():
        cfg = json.loads(TG_CFG_PATH.read_text(encoding="utf-8"))
    # Переменные окружения имеют приоритет над JSON-файлом
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    env_chat  = os.environ.get("TELEGRAM_CHAT_ID")
    if env_token:
        cfg["bot_token"] = env_token
    if env_chat:
        cfg["chat_id"] = env_chat
    return cfg


# ─── Анализ влияния новостей на сетапы ───────────────────────────────────────

# Типы влияния и их иконки
IMPACT_META = {
    "SQUEEZE":      ("🚀", "СКВИЗ",        "LONG"),
    "LONG_MARKET":  ("🟢", "ЛОНГ рынок",   "LONG"),
    "LONG_COIN":    ("🟢", "ЛОНГ",         "LONG"),
    "SHORT_MARKET": ("🔴", "РИСК / ШОРТ",  "SHORT"),
    "SHORT_COIN":   ("🔴", "ШОРТ",         "SHORT"),
    "RISK":         ("⚠️", "ГЕОПОЛИТИКА",  None),
    "NOISE":        ("⬜", "ШУМ",           None),
}
STRENGTH_DOT = {"HIGH": "●●●", "MEDIUM": "●●○", "LOW": "●○○"}


def analyze_news_impact(text: str, symbol: Optional[str] = None) -> dict:
    """
    Определяет как новость влияет на рынок и сетапы.

    Возвращает:
      impact    — тип: SQUEEZE / LONG_MARKET / SHORT_MARKET / LONG_COIN /
                       SHORT_COIN / RISK / NOISE
      direction — LONG / SHORT / None
      strength  — HIGH / MEDIUM / LOW
      reason    — объяснение на русском (1 строка)
      coins     — список затронутых символов (XXXUSDT или "MARKET")
    """
    lo = text.lower()

    # ── Сквиз (самый ценный сигнал) ─────────────────────────────────────────
    if any(p in lo for p in [
        "oi up", "funding negative", "negative funding",
        "short squeeze", "funding neg", "oi растёт", "фандинг отриц",
    ]):
        return {
            "impact": "SQUEEZE", "direction": "LONG", "strength": "HIGH",
            "reason": "OI растёт + фандинг отрицательный → топливо для сквиза вверх",
            "coins": [symbol] if symbol else ["MARKET"],
        }

    # ── Геополитика — эскалация ──────────────────────────────────────────────
    geo_esc = [
        "military", "pentagon", "ground operation", "strike", "attack",
        "war", "troops", "escalat", "conflict", "missile",
        "иран", "iran", "israel", "израиль", "война", "удар", "атака",
        "hezbollah", "хезболла", "хамас", "hamas",
    ]
    geo_peace = [
        "ceasefire", "peace talks", "negotiations concluded",
        "de-escalat", "перемирие", "переговоры завершились",
    ]
    if any(p in lo for p in geo_esc):
        # Мир vs война
        if any(p in lo for p in geo_peace):
            return {
                "impact": "LONG_MARKET", "direction": "LONG", "strength": "MEDIUM",
                "reason": "Де-эскалация конфликта → риск-он → крипта растёт",
                "coins": ["BTCUSDT", "ETHUSDT"],
            }
        return {
            "impact": "RISK", "direction": "SHORT", "strength": "MEDIUM",
            "reason": "Геополитическая эскалация → риск-офф → давление на крипту",
            "coins": ["BTCUSDT", "ETHUSDT"],
        }

    # ── ETF / институционалы ─────────────────────────────────────────────────
    if any(p in lo for p in [
        "etf", "bitcoin etf", "approve", "goldman sachs", "blackrock",
        "institutional", "hedge fund", "инсти", "etf flow", "etf inflow",
        "spot etf",
    ]):
        strength = "HIGH" if any(p in lo for p in ["approve", "goldman", "blackrock", "record"]) else "MEDIUM"
        return {
            "impact": "LONG_MARKET", "direction": "LONG", "strength": strength,
            "reason": "Институциональный спрос / ETF → рост интереса к BTC/ETH",
            "coins": ["BTCUSDT", "ETHUSDT"],
        }

    # ── Листинг на бирже ─────────────────────────────────────────────────────
    listing_kw = ["listing", "listed on binance", "listed on", "lists", "листинг на"]
    if any(p in lo for p in listing_kw):
        return {
            "impact": "LONG_COIN" if symbol else "LONG_MARKET",
            "direction": "LONG", "strength": "HIGH",
            "reason": "Листинг на бирже → объём + спрос растут резко",
            "coins": [symbol] if symbol else ["?"],
        }

    # ── Хак / эксплойт ───────────────────────────────────────────────────────
    if any(p in lo for p in ["hack", "exploit", "hacked", "взлом", "bridge hack", "drained"]):
        return {
            "impact": "SHORT_COIN" if symbol else "SHORT_MARKET",
            "direction": "SHORT", "strength": "HIGH",
            "reason": "Взлом / эксплойт → потеря доверия → распродажа",
            "coins": [symbol] if symbol else ["MARKET"],
        }

    # ── Регуляция негативная ─────────────────────────────────────────────────
    if any(p in lo for p in [
        "sec lawsuit", "ban", "запрет", "иск против", "ограничени",
        "санкции против крипт", "crypto ban", "delisted",
    ]):
        return {
            "impact": "SHORT_MARKET", "direction": "SHORT", "strength": "MEDIUM",
            "reason": "Регуляторное давление → неопределённость → продажи",
            "coins": ["MARKET"],
        }

    # ── ФРС / ставки ─────────────────────────────────────────────────────────
    if any(p in lo for p in ["rate cut", "dovish", "pause rate", "снижение ставки", "смягчение"]):
        return {
            "impact": "LONG_MARKET", "direction": "LONG", "strength": "MEDIUM",
            "reason": "Смягчение ДКП → риск-активы растут",
            "coins": ["BTCUSDT", "ETHUSDT"],
        }
    if any(p in lo for p in ["rate hike", "hawkish", "tighten", "повышение ставки", "жёсткая дкп"]):
        return {
            "impact": "SHORT_MARKET", "direction": "SHORT", "strength": "MEDIUM",
            "reason": "Ужесточение ДКП → риск-активы падают",
            "coins": ["BTCUSDT", "ETHUSDT"],
        }

    # ── Памп/рост конкретного токена ─────────────────────────────────────────
    if symbol and any(p in lo for p in [
        "high", "highs", "ath", "breakout", "record", "surge",
        "growth", "expanding", "rally", "хай", "рекорд",
    ]):
        return {
            "impact": "LONG_COIN", "direction": "LONG", "strength": "MEDIUM",
            "reason": f"Позитивный моментум: рост активности вокруг {symbol.replace('USDT','')}",
            "coins": [symbol],
        }

    # ── Аирдроп / запуск продукта ────────────────────────────────────────────
    if any(p in lo for p in ["airdrop", "launch", "launches", "mainnet", "запуск", "релиз"]):
        return {
            "impact": "LONG_COIN" if symbol else "LONG_MARKET",
            "direction": "LONG", "strength": "LOW",
            "reason": "Запуск продукта / аирдроп → краткосрочный спрос",
            "coins": [symbol] if symbol else ["?"],
        }

    return {
        "impact": "NOISE", "direction": None, "strength": "LOW",
        "reason": "Не влияет на торговые сетапы",
        "coins": [],
    }


# ─── Vision: анализ графиков ─────────────────────────────────────────────────

def _extract_k_price(text: str) -> Optional[float]:
    """
    Вытаскивает ценовой таргет из коротких подписей типа '6k', '100k', '$1.5'.
    """
    # "6k", "100K", "6.5k"
    m = re.search(r'(\d+\.?\d*)\s*[kK]\b', text)
    if m:
        try:
            return float(m.group(1)) * 1000
        except ValueError:
            pass
    # "$6000", "$ 6000"
    m = re.search(r'\$\s*(\d[\d,.]+)', text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def analyze_chart_image(image_bytes: bytes, caption: str = "") -> dict:
    """
    Отправляет скриншот графика в Claude Vision и извлекает торговые уровни.

    Требует ANTHROPIC_API_KEY в окружении или поле 'anthropic_api_key'
    в channels_config.json.

    Возвращает dict с полями:
      symbol, direction, entry, entry_high, sl, tp1, tp2, note
    или пустой dict если анализ не удался.
    """
    import os
    import base64

    try:
        import anthropic as _ant
    except ImportError:
        return {}

    api_key = (
        os.environ.get("ANTHROPIC_API_KEY", "")
        or load_cfg().get("anthropic_api_key", "")
    )
    if not api_key:
        return {}

    try:
        # Определяем формат изображения
        if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
            media_type = "image/png"
        elif image_bytes[:4] == b'GIF8':
            media_type = "image/gif"
        elif image_bytes[:4] == b'RIFF':
            media_type = "image/webp"
        else:
            media_type = "image/jpeg"

        img_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

        prompt = (
            f'Chart screenshot. Caption: "{caption}"\n\n'
            "Extract trading levels from this chart.\n"
            "Look for: drawn horizontal lines, zones, arrows, labels with prices.\n"
            "Green zones / support = entry for LONG. Red zones / resistance = SL for LONG or entry for SHORT.\n"
            "Arrows up = LONG. Arrows down = SHORT.\n\n"
            "Return ONLY this JSON (no markdown, no explanation):\n"
            '{"symbol":"BTCUSDT","direction":"LONG","entry":83000,"entry_high":84000,'
            '"sl":81000,"tp1":87000,"tp2":92000,"note":"brief pattern description"}\n\n'
            "Rules:\n"
            "- symbol: always end with USDT (e.g. BTCUSDT, ETHUSDT, ZECUSDT)\n"
            "- If symbol not visible in chart, infer from caption\n"
            "- If no SL/TP marked, set to null\n"
            "- If direction unclear, default to LONG\n"
            '- If cannot identify coin at all: {"symbol":null,"direction":null}'
        )

        client = _ant.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": img_b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )

        raw = resp.content[0].text.strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return {}

        data = json.loads(m.group())
        sym = (data.get("symbol") or "").upper().strip()
        if sym and not sym.endswith("USDT"):
            sym = sym + "USDT"
        if sym:
            data["symbol"] = sym
        return data

    except Exception:
        return {}


# ─── Получение цен с Bybit ────────────────────────────────────────────────────

def get_quick_prices(symbols: list) -> dict:
    """
    Batch-запрос текущих цен с Bybit (без авторизации, один HTTP запрос).
    Возвращает {symbol: price} для всех символов из списка.
    """
    if not symbols:
        return {}
    prices = {}
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear"},
            timeout=8,
        )
        data = r.json()
        if data.get("retCode") == 0:
            sym_set = set(symbols)
            for item in data.get("result", {}).get("list", []):
                s = item.get("symbol", "")
                if s in sym_set:
                    try:
                        prices[s] = float(item["lastPrice"])
                    except (KeyError, ValueError):
                        pass
    except Exception:
        pass
    return prices


# ─── Парсер сигналов ─────────────────────────────────────────────────────────

def _extract_price(text: str, keywords: list) -> Optional[float]:
    """Извлекает цену после ключевого слова."""
    for kw in keywords:
        pattern = rf'(?i){re.escape(kw)}\s*[:\-]?\s*(\d[\d\s,\.]*\d|\d+)'
        m = re.search(pattern, text)
        if m:
            raw = m.group(1).replace(",", "").replace(" ", "")
            try:
                val = float(raw)
                if val > 0:
                    return val
            except ValueError:
                continue
    return None


def _extract_symbol(text: str) -> Optional[str]:
    """Извлекает символ из текста. Обрабатывает форматы: BTC, #BTC, BTC/USDT, BTCUSDT."""
    # Нормализуем: убираем # перед тикерами, приводим к верхнему регистру
    text_up = re.sub(r'#([A-Za-z]{2,8})', r'\1', text).upper()

    # Сначала ищем явные пары: BTC/USDT, BTCUSDT, BTC-PERP, BTC-USD
    for pat in [
        r'\b([A-Z]{2,8})/USDT\b',
        r'\b([A-Z]{2,8})USDT\b',
        r'\b([A-Z]{2,8})-PERP\b',
        r'\b([A-Z]{2,8})-USD\b',
    ]:
        m = re.search(pat, text_up)
        if m:
            base = m.group(1)
            if base in SYMBOL_MAP:
                return SYMBOL_MAP[base]
            candidate = base + "USDT"
            if len(base) <= 8:
                return candidate

    # Потом ищем тикеры из маппинга
    for sym, bybit_sym in SYMBOL_MAP.items():
        if re.search(rf'\b{sym}\b', text_up):
            return bybit_sym

    return None


def _spam_score(text: str) -> int:
    """Возвращает количество спам-маркеров."""
    text_lo = text.lower()
    return sum(1 for kw in SPAM_KW if kw in text_lo)


def parse_message(text: str, channel: str,
                  chart: Optional[dict] = None,
                  has_photo: bool = False) -> dict:
    """
    Парсит одно сообщение и возвращает структурированный результат.

    Тип результата:
      "signal"           — торговый сигнал (символ + направление)
      "market_sentiment" — направление рынка без конкретной монеты
      "news"             — рыночная новость
      "skip"             — реклама / нерелевантно

    chart     — результат vision-анализа скриншота (если был)
    has_photo — сообщение содержит фото/скриншот
    """
    # ── 1. Vision дал полный результат → используем напрямую ─────────────────
    if chart and chart.get("symbol") and chart.get("direction"):
        sl    = chart.get("sl")
        tp1   = chart.get("tp1")
        entry = chart.get("entry")
        quality = sum(1 for x in [entry, sl, tp1] if x is not None)
        return {
            "type":         "signal",
            "channel":      channel,
            "symbol":       chart["symbol"],
            "direction":    chart["direction"],
            "entry":        entry,
            "entry_high":   chart.get("entry_high"),
            "market_entry": entry is None,
            "sl":           sl,
            "tp1":          tp1,
            "tp2":          chart.get("tp2"),
            "quality":      quality,
            "note":         chart.get("note", ""),
            "from_chart":   True,
            "raw":          text[:300],
        }

    # ── 2. Фото + #TICKER в подписи → сигнал без уровней ─────────────────────
    #    (@rose, @archfund и другие, где chart пришёл пустым или без API ключа)
    if has_photo and text:
        # Сначала пробуем через SYMBOL_MAP, потом прямой regex на #TICKER
        symbol = _extract_symbol(text)
        if not symbol:
            # Прямой поиск хэштега: #ZEC, #HYPE, #IRYS и т.д.
            m_tag = re.search(r'#([A-Za-z]{2,10})\b', text)
            if m_tag:
                base = m_tag.group(1).upper()
                # Игнорируем стоп-слова и слишком общие слова
                if base not in {"USD", "USDT", "THE", "FOR", "AND", "NOT", "BIG",
                                 "NEW", "OLD", "ALL", "ETH", "BTC"}:
                    symbol = base + "USDT"
                elif base in ("ETH", "BTC"):
                    symbol = SYMBOL_MAP.get(base, base + "USDT")
        if symbol:
            text_lo = text.lower()
            is_bear = any(w in text_lo for w in [
                "bear", "short", "шорт", "sell", "падение",
                "вниз", "bearish", "медвежий",
            ])
            direction = "SHORT" if is_bear else "LONG"
            tp1 = _extract_k_price(text)
            return {
                "type":         "signal",
                "channel":      channel,
                "symbol":       symbol,
                "direction":    direction,
                "entry":        None,
                "entry_high":   None,
                "market_entry": True,
                "sl":           None,
                "tp1":          tp1,
                "tp2":          None,
                "quality":      1 if tp1 else 0,
                "note":         text.strip()[:100],
                "from_chart":   False,
                "raw":          text[:300],
            }

    if not text or len(text) < 5:
        return {"type": "skip"}

    if _spam_score(text) >= 2:
        return {"type": "skip"}

    text_lo = text.lower()

    symbol    = _extract_symbol(text)
    direction = None

    if any(kw in text_lo for kw in LONG_KW):
        direction = "LONG"
    elif any(kw in text_lo for kw in SHORT_KW):
        direction = "SHORT"

    # Направление рынка в целом (без конкретной монеты) — сентимент канала
    if direction and not symbol:
        snippet = " ".join(text.split())[:120]
        return {
            "type":      "market_sentiment",
            "channel":   channel,
            "direction": direction,
            "raw":       snippet,
        }

    if symbol and direction:
        # Пробуем извлечь диапазон входа: "вход 1200-1300" / "entry 1200–1300"
        entry = None
        entry_high = None
        range_pat = r'(?:вход|entry|zone|зона)\s*[:\s]*(\d[\d\s,.]*)\s*[-–—]\s*(\d[\d\s,.]*)'
        m = re.search(range_pat, text, re.IGNORECASE)
        if m:
            try:
                lo = float(m.group(1).replace(",", "").replace(" ", ""))
                hi = float(m.group(2).replace(",", "").replace(" ", ""))
                if lo > 0 and hi > 0:
                    entry      = min(lo, hi)
                    entry_high = max(lo, hi)
            except ValueError:
                pass

        if entry is None:
            entry = _extract_price(text, [
                "entry", "вход", "цена входа", "zone", "зона", "от",
            ])

        # Флаг "вход по рынку" — когда пишут "от текущих / по рынку / market"
        market_entry = bool(re.search(
            r'от\s+текущ|текущ[аяие]+\s+цен[ыа]?|по\s+рынку|from\s+current|market\s+order',
            text, re.IGNORECASE,
        ))

        sl  = _extract_price(text, ["sl", "stop loss", "стоп", "stop", "стоп-лосс"])
        tp1 = _extract_price(text, ["tp1", "тп1", "take profit 1", "tp 1", "цель 1", "t1"])
        tp2 = _extract_price(text, ["tp2", "тп2", "take profit 2", "tp 2", "цель 2", "t2"])

        # Fallback: просто "цель / target / tp" если tp1 не найден
        if tp1 is None:
            tp1 = _extract_price(text, ["цель", "target", "тейк", "tp", "тп"])

        # Качество: entry (или market) + sl + tp1
        has_entry = (entry is not None) or market_entry
        levels_defined = sum(1 for x in [has_entry, sl is not None, tp1 is not None] if x)

        return {
            "type":         "signal",
            "channel":      channel,
            "symbol":       symbol,
            "direction":    direction,
            "entry":        entry,
            "entry_high":   entry_high,
            "market_entry": market_entry,
            "sl":           sl,
            "tp1":          tp1,
            "tp2":          tp2,
            "quality":      levels_defined,   # 0=тикер, 1=+вход, 2=+стоп, 3=полный план
            "raw":          text[:300],
        }

    # Пробуем распознать новость
    news_count = sum(1 for kw in NEWS_KW if kw in text_lo)
    if news_count >= 2 or (news_count >= 1 and len(text) > 80):
        # Сентимент новости
        bull = sum(1 for kw in NEWS_BULLISH_KW if kw in text_lo)
        bear = sum(1 for kw in NEWS_BEARISH_KW if kw in text_lo)
        sentiment = "bullish" if bull > bear else ("bearish" if bear > bull else "neutral")

        snippet = " ".join(text.split())[:200]
        return {
            "type":      "news",
            "channel":   channel,
            "sentiment": sentiment,
            "symbol":    _extract_symbol(text),   # монета если упомянута
            "raw":       snippet,
        }

    return {"type": "skip"}


# ─── Чтение каналов через Telethon ───────────────────────────────────────────

async def _fetch_channel_messages(client, channel: str, hours: int = LOOKBACK_HOURS) -> list:
    """
    Получает сообщения из одного канала за последние `hours` часов.

    Возвращает list[dict]:
      {"text": str, "has_photo": bool, "chart": dict|None}

    Для каналов в VISION_CHANNELS фотографии скачиваются и анализируются
    через Claude Vision (если настроен API ключ).
    """
    from telethon.tl.types import MessageMediaPhoto
    from telethon.errors import ChannelPrivateError, UsernameNotOccupiedError

    cutoff      = datetime.now(timezone.utc) - timedelta(hours=hours)
    use_vision  = channel in VISION_CHANNELS
    messages    = []

    try:
        entity = await client.get_entity(channel)
        async for msg in client.iter_messages(entity, limit=50):
            if msg.date < cutoff:
                break

            text      = msg.text or ""
            is_photo  = isinstance(msg.media, MessageMediaPhoto)
            chart_data = None

            # Vision анализ для каналов-графиков (только если есть фото и текст/caption)
            if is_photo and use_vision:
                try:
                    img_bytes = await client.download_media(msg.media, bytes)
                    if img_bytes:
                        chart_data = analyze_chart_image(img_bytes, text)
                except Exception:
                    pass

            # Берём сообщение если есть текст ИЛИ vision дал результат
            if text or chart_data:
                messages.append({
                    "text":      text,
                    "has_photo": is_photo,
                    "chart":     chart_data,
                })

    except ChannelPrivateError:
        print(f"  [{channel}] Закрытый канал, пропущен")
    except UsernameNotOccupiedError:
        print(f"  [{channel}] Канал не найден")
    except Exception as e:
        print(f"  [{channel}] Ошибка: {type(e).__name__}: {e}")

    return messages


async def scan_channels_async(cfg: dict) -> dict:
    """Основная async функция сканирования."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    # Проверка сети перед попыткой MTProto-подключения
    _net_ok = _wait_for_telegram_network()
    if not _net_ok:
        print("[channel_reader] Нет сети — сканирование пропущено.")
        return {}

    session_str = cfg.get("session_string", "")
    api_id      = cfg.get("api_id")
    api_hash    = cfg.get("api_hash")

    if not api_id or not api_hash:
        print("ОШИБКА: api_id / api_hash не настроены. Запусти: python3 channel_reader.py setup")
        return {}

    results: dict[str, list] = {}

    async with TelegramClient(StringSession(session_str), api_id, api_hash) as client:
        for channel in CHANNELS:
            print(f"  Читаю @{channel}...")
            msgs = await _fetch_channel_messages(client, channel)
            parsed = []
            for item in msgs:
                text      = item.get("text", "") if isinstance(item, dict) else item
                chart     = item.get("chart")    if isinstance(item, dict) else None
                has_photo = item.get("has_photo", False) if isinstance(item, dict) else False
                p = parse_message(text, channel, chart=chart, has_photo=has_photo)
                if p["type"] != "skip":
                    parsed.append(p)
            results[channel] = parsed
            vision_str = " (📸 vision)" if channel in VISION_CHANNELS else ""
            print(f"    → {len(msgs)} сообщений, {len(parsed)} релевантных{vision_str}")

    return results


# ─── Кросс-верификация со скринером ──────────────────────────────────────────

def cross_verify(channel_results: dict) -> dict:
    """
    Сопоставляет сигналы каналов с последними результатами скринера.
    Дополнительно:
      — Проверяет текущую цену на Bybit (один batch запрос)
      — Помечает сигналы как "active_now" если цена у входа (±3%)
      — Считает R:R для channel_only сигналов
      — Определяет сентимент новостей
    """
    # Загружаем кэш скринера
    screener_map: dict = {}
    if SCAN_CACHE.exists():
        try:
            cache = json.loads(SCAN_CACHE.read_text(encoding="utf-8"))
            for r in cache.get("filtered", []):
                screener_map[r["symbol"]] = r
        except Exception:
            pass

    # Агрегируем сигналы по символу
    symbol_signals: dict = {}
    all_news: list = []
    market_sentiments: list = []   # Общий сентимент без конкретной монеты

    for channel, items in channel_results.items():
        for item in items:
            if item["type"] == "news":
                all_news.append(item)
                continue
            if item["type"] == "market_sentiment":
                market_sentiments.append(item)
                continue
            if item["type"] != "signal":
                continue
            sym = item["symbol"]
            if sym not in symbol_signals:
                symbol_signals[sym] = {"LONG": [], "SHORT": [], "data": []}
            symbol_signals[sym][item["direction"]].append(channel)
            symbol_signals[sym]["data"].append(item)

    # Batch запрос текущих цен для всех упомянутых символов
    all_syms = list(symbol_signals.keys())
    current_prices = get_quick_prices(all_syms)

    active_now    = []
    agreements    = []
    conflicts     = []
    channel_only  = []
    multi_channel = []

    for sym, sigs in symbol_signals.items():
        long_sources  = sigs["LONG"]
        short_sources = sigs["SHORT"]
        data          = sigs["data"]
        cur_price     = current_prices.get(sym)

        # Консенсус нескольких каналов
        if len(long_sources) >= 2:
            multi_channel.append({
                "symbol": sym, "direction": "LONG",
                "channels": long_sources, "count": len(long_sources),
                "current_price": cur_price,
            })
        if len(short_sources) >= 2:
            multi_channel.append({
                "symbol": sym, "direction": "SHORT",
                "channels": short_sources, "count": len(short_sources),
                "current_price": cur_price,
            })

        # Доминирующее направление
        if len(long_sources) >= len(short_sources):
            ch_dir = "LONG"; ch_sources = long_sources
        else:
            ch_dir = "SHORT"; ch_sources = short_sources
        if not ch_sources:
            continue

        best_sig = sorted(data, key=lambda x: x.get("quality", 0), reverse=True)[0]
        entry       = best_sig.get("entry")
        entry_high  = best_sig.get("entry_high")
        market_entry = best_sig.get("market_entry", False)

        # Проверяем: цена сейчас у уровня входа?
        is_active     = False
        entry_diff_pct = None
        if market_entry:
            is_active = True
        elif entry and cur_price:
            entry_diff_pct = (cur_price - entry) / entry * 100
            is_active = abs(entry_diff_pct) < 3.0

        # R:R для сигнала
        rr = None
        sl  = best_sig.get("sl")
        tp1 = best_sig.get("tp1")
        if entry and sl and tp1:
            try:
                risk   = abs(entry - float(sl))
                reward = abs(float(tp1) - entry)
                if risk > 0:
                    rr = round(reward / risk, 2)
            except Exception:
                pass

        # Базовый объект сигнала
        sig_obj = {
            "symbol":        sym,
            "direction":     ch_dir,
            "channels":      ch_sources,
            "entry":         entry,
            "entry_high":    entry_high,
            "market_entry":  market_entry,
            "sl":            sl,
            "tp1":           tp1,
            "tp2":           best_sig.get("tp2"),
            "quality":       best_sig.get("quality", 0),
            "current_price": cur_price,
            "entry_diff_pct": entry_diff_pct,
            "is_active":     is_active,
            "rr":            rr,
        }

        # Активные прямо сейчас — отдельная категория
        if is_active:
            in_screener     = sym in screener_map
            screener_agrees = False
            screener_score  = 0
            if in_screener:
                screener_setup  = screener_map[sym].get("setup", "")
                screener_dir    = "LONG" if screener_setup in ("squeeze", "breakout") else "SHORT"
                screener_agrees = (screener_dir == ch_dir)
                screener_score  = screener_map[sym].get("score", 0)
            active_now.append({
                **sig_obj,
                "in_screener":     in_screener,
                "screener_agrees": screener_agrees,
                "screener_score":  screener_score,
            })

        # Классификация: скринер знает монету или нет
        if sym in screener_map:
            r            = screener_map[sym]
            screener_setup = r.get("setup", "")
            screener_dir = "LONG" if screener_setup in ("squeeze", "breakout") else "SHORT"

            if ch_dir == screener_dir:
                agreements.append({
                    **sig_obj,
                    "score":  r.get("score", 0),
                    "setup":  screener_setup,
                    "price":  r.get("price"),
                })
            else:
                conflicts.append({
                    **sig_obj,
                    "channel_dir":  ch_dir,
                    "screener_dir": screener_dir,
                    "score":        r.get("score", 0),
                    "raw":          best_sig.get("raw", ""),
                })
        else:
            # Не в скринере — показываем если хоть что-то есть (quality >= 1)
            if best_sig.get("quality", 0) >= 1 or market_entry:
                channel_only.append({
                    **sig_obj,
                    "raw": best_sig.get("raw", "")[:150],
                })

    # Сортировка: активные и качественные выше
    def _sort_key(x):
        return (x.get("is_active", False), len(x.get("channels", [])), x.get("quality", 0))

    active_now.sort(key=lambda x: (len(x["channels"]), x.get("quality", 0)), reverse=True)
    agreements.sort(key=lambda x: (x.get("is_active", False), len(x["channels"]), x.get("score", 0)), reverse=True)
    conflicts.sort(key=lambda x: x.get("score", 0), reverse=True)
    channel_only.sort(key=_sort_key, reverse=True)

    # ── Анализ влияния каждой новости ────────────────────────────────────────
    for n in all_news:
        imp = analyze_news_impact(n.get("raw", ""), n.get("symbol"))
        n["impact"] = imp
        # Проверяем пересечение с монетами из скринера
        overlap = []
        for coin in imp.get("coins", []):
            if coin in screener_map:
                sc_setup = screener_map[coin].get("setup", "")
                sc_dir   = "LONG" if sc_setup in ("squeeze", "breakout") else "SHORT"
                agrees   = (sc_dir == imp.get("direction"))
                overlap.append({
                    "symbol":  coin,
                    "setup":   sc_setup,
                    "sc_dir":  sc_dir,
                    "agrees":  agrees,
                    "score":   screener_map[coin].get("score", 0),
                })
        n["screener_overlap"] = overlap

    # ── Подсчёт упоминаний монет (из сигналов + новостей) ───────────────────
    mention_count: dict = {}
    for channel, items in channel_results.items():
        for item in items:
            sym = item.get("symbol")
            if sym:
                mention_count[sym] = mention_count.get(sym, 0) + 1
    hot_coins = sorted(mention_count.items(), key=lambda x: x[1], reverse=True)[:8]
    hot_coins_with_price = [
        {"symbol": s, "count": c, "price": current_prices.get(s)}
        for s, c in hot_coins if c >= 2
    ]

    # ── Общий сентимент рынка ────────────────────────────────────────────────
    total_bull = 0
    total_bear = 0
    for channel, items in channel_results.items():
        for item in items:
            d = item.get("direction", "")
            s = item.get("sentiment", "")
            if d == "LONG" or s == "bullish":
                total_bull += 1
            elif d == "SHORT" or s == "bearish":
                total_bear += 1

    if total_bull + total_bear == 0:
        market_sentiment = "neutral"
    elif total_bull / max(total_bull + total_bear, 1) >= 0.65:
        market_sentiment = "bullish"
    elif total_bear / max(total_bull + total_bear, 1) >= 0.65:
        market_sentiment = "bearish"
    else:
        market_sentiment = "mixed"

    return {
        "active_now":         active_now,
        "agreements":         agreements,
        "conflicts":          conflicts,
        "channel_only":       channel_only,
        "news":               all_news[:10],
        "multi_channel":      multi_channel,
        "hot_coins":          hot_coins_with_price,
        "market_sentiment":   market_sentiment,
        "bull_count":         total_bull,
        "bear_count":         total_bear,
        "market_sentiments":  market_sentiments[:6],
    }


# ─── Форматирование Telegram сообщения ───────────────────────────────────────

def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_p(p) -> str:
    if p is None:
        return "?"
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "?"
    if p >= 100:  return f"{p:.2f}"
    if p >= 1:    return f"{p:.4f}"
    if p >= 0.01: return f"{p:.5f}"
    return f"{p:.8f}"


def format_channel_insights(verified: dict) -> str:
    now     = datetime.now().strftime("%d.%m %H:%M")
    n_ch    = len(CHANNELS)
    ch_preview = "  @".join(CHANNELS[:4]) + (f"  +{n_ch - 4} ещё" if n_ch > 4 else "")
    lines = [
        f"📡 <b>АНАЛИЗ КАНАЛОВ  {now}</b>  <i>(за {LOOKBACK_HOURS}ч)</i>",
        f"<i>@{ch_preview}</i>",
        "",
    ]

    def _pl(entry, entry_high, cur, diff_pct, mkt) -> str:
        """Строка с ценами: вход + текущая + отклонение."""
        parts = []
        if mkt:
            parts.append("Вход: <b>СЕЙЧАС</b>")
        elif entry:
            if entry_high:
                parts.append(f"Вход: <code>{_fmt_p(entry)}–{_fmt_p(entry_high)}</code>")
            else:
                parts.append(f"Вход: <code>{_fmt_p(entry)}</code>")
        if cur:
            pstr = f"Сейчас: <code>{_fmt_p(cur)}</code>"
            if diff_pct is not None and not mkt:
                sign = "+" if diff_pct >= 0 else ""
                badge = "✅" if abs(diff_pct) < 3 else ("⏳" if abs(diff_pct) < 10 else "🚫")
                pstr += f" ({sign}{diff_pct:.1f}% {badge})"
            parts.append(pstr)
        return "  ".join(parts)

    def _sl_tp(item) -> str:
        parts = []
        if item.get("sl"):  parts.append(f"SL <code>{_fmt_p(item['sl'])}</code>")
        if item.get("tp1"): parts.append(f"TP1 <code>{_fmt_p(item['tp1'])}</code>")
        if item.get("tp2"): parts.append(f"TP2 <code>{_fmt_p(item['tp2'])}</code>")
        if item.get("rr"):  parts.append(f"<b>R:R {item['rr']:.1f}</b>")
        return "  ".join(parts)

    any_content = False

    # ── 0. Пульс рынка ───────────────────────────────────────────────────────
    sentiment    = verified.get("market_sentiment", "neutral")
    bull_count   = verified.get("bull_count", 0)
    bear_count   = verified.get("bear_count", 0)
    hot_coins    = verified.get("hot_coins", [])
    sent_icon    = {"bullish": "🟢", "bearish": "🔴", "mixed": "🟡", "neutral": "⚪"}.get(sentiment, "⚪")
    sent_label   = {"bullish": "Бычий", "bearish": "Медвежий", "mixed": "Смешанный", "neutral": "Нейтральный"}.get(sentiment, "Нейтральный")

    lines.append(f"📊 <b>ПУЛЬС РЫНКА</b>  {sent_icon} <b>{sent_label}</b>  "
                 f"(🟢{bull_count} / 🔴{bear_count})")

    if hot_coins:
        coins_str = "  ".join(
            f"<b>{c['symbol'].replace('USDT','')}</b>×{c['count']}"
            + (f" <code>{_fmt_p(c['price'])}</code>" if c.get('price') else "")
            for c in hot_coins[:6]
        )
        lines.append(f"🔥 <b>Горячие монеты:</b>  {coins_str}")

    # Сентимент без конкретной монеты (@rose, @RoseSignalsPremium и др.)
    mkt_sents = verified.get("market_sentiments", [])
    if mkt_sents:
        seen_sent: set = set()
        sent_lines = []
        for ms in mkt_sents[:5]:
            snippet = ms.get("raw", "").strip()
            key = snippet[:30]
            if key in seen_sent or not snippet:
                continue
            seen_sent.add(key)
            icon = "🟢" if ms["direction"] == "LONG" else "🔴"
            sent_lines.append(f"  {icon} <b>@{ms['channel']}</b>: <i>{_esc(snippet[:100])}</i>")
        if sent_lines:
            lines.append("💬 <b>Взгляд трейдеров:</b>")
            lines.extend(sent_lines)

    lines.append("")

    def _sig_lines(a: dict, show_screener: bool = False) -> list[str]:
        """Стандартные строки для одного сигнала."""
        out   = []
        icon  = "🟢" if a["direction"] == "LONG" else "🔴"
        src   = "  ".join(f"@{c}" for c in a.get("channels", [])[:3])
        chart_badge = "  📸" if a.get("from_chart") else ("  📷" if a.get("note") and not a.get("from_chart") and a.get("market_entry") else "")
        badge = chart_badge
        if show_screener:
            if a.get("screener_agrees"):
                badge += f"  ✅ скринер score={a.get('screener_score',0)}"
            elif a.get("in_screener"):
                badge += "  ⚠️ скринер против"
        out.append(f"  {icon} <b>{_esc(a['symbol'])}</b>  {a['direction']}{badge}")
        pl = _pl(a.get("entry"), a.get("entry_high"), a.get("current_price"),
                 a.get("entry_diff_pct"), a.get("market_entry"))
        if pl: out.append(f"  {pl}")
        st = _sl_tp(a)
        if st: out.append(f"  {st}")
        note = a.get("note", "")
        if note and a.get("from_chart"):
            out.append(f"  <i>📊 {_esc(note[:80])}</i>")
        elif note and a.get("market_entry") and not a.get("entry"):
            # Краткая подпись с графика (@rose стиль)
            out.append(f"  <i>«{_esc(note[:80])}»</i>")
        out.append(f"  <i>← {_esc(src)}</i>")
        out.append("")
        return out

    # ── 1. Активные прямо сейчас ─────────────────────────────────────────────
    active = verified.get("active_now", [])
    if active:
        any_content = True
        lines.append("🎯 <b>АКТИВНЫЕ ПРЯМО СЕЙЧАС</b>  (цена у входа ±3%)")
        for a in active[:6]:
            lines.extend(_sig_lines(a, show_screener=True))

    # ── 2. Консенсус ≥2 каналов ──────────────────────────────────────────────
    mc = [m for m in verified.get("multi_channel", []) if m["count"] >= 2]
    if mc:
        any_content = True
        lines.append("🤝 <b>КОНСЕНСУС</b>  (≥2 канала)")
        for item in mc[:5]:
            icon = "🟢" if item["direction"] == "LONG" else "🔴"
            src  = "  ".join(f"@{c}" for c in item["channels"])
            cp   = f"  Сейчас: <code>{_fmt_p(item.get('current_price'))}</code>" if item.get("current_price") else ""
            lines.append(
                f"  {icon} <b>{_esc(item['symbol'])}</b>  {item['direction']}"
                f"  ({item['count']} кан.){cp}"
            )
            lines.append(f"  <i>← {_esc(src)}</i>")
            lines.append("")

    # ── 3. Совпадения со скринером (не активные) ─────────────────────────────
    agr = [a for a in verified.get("agreements", []) if not a.get("is_active")]
    if agr:
        any_content = True
        lines.append("✅ <b>СОВПАДАЮТ со скринером</b>")
        for a in agr[:5]:
            setup_short = {
                "squeeze": "SQZ", "bos_fvg": "BOS",
                "breakout": "PUMP", "range_sweep": "SWEEP",
            }.get(a.get("setup", ""), "")
            score = a.get("score", 0)
            lines.append(f"  [{setup_short}] score={score}")
            lines.extend(_sig_lines(a))

    # ── 4. Только в каналах (нет в скринере) ─────────────────────────────────
    co = verified.get("channel_only", [])
    if co:
        any_content = True
        lines.append("🔍 <b>ТОЛЬКО В КАНАЛАХ</b>  (нет в скринере)")
        for item in co[:5]:
            if item.get("is_active"):
                lines.append("  🎯 активно прямо сейчас")
            lines.extend(_sig_lines(item))

    # ── 5. Расхождения ────────────────────────────────────────────────────────
    conf = verified.get("conflicts", [])
    if conf:
        any_content = True
        lines.append("⚔️ <b>РАСХОЖДЕНИЯ</b>  (каналы vs скринер)")
        for c in conf[:3]:
            src = "  ".join(f"@{ch}" for ch in c["channels"][:2])
            cp  = f"  Сейчас: <code>{_fmt_p(c.get('current_price'))}</code>" if c.get("current_price") else ""
            lines.append(
                f"  ⚠️ <b>{_esc(c['symbol'])}</b>"
                f"  каналы={c['channel_dir']}  скринер={c['screener_dir']}"
                f"  score={c.get('score', 0)}{cp}"
            )
            if c.get("raw"):
                lines.append(f"  <i>«{_esc(c['raw'][:120])}»</i>")
            lines.append(f"  <i>← {_esc(src)}</i>")
            lines.append("")

    # ── 6. Новости — влияние на сетапы ───────────────────────────────────────
    news = verified.get("news", [])
    if news:
        any_content = True
        relevant_news = [n for n in news if n.get("impact", {}).get("impact") != "NOISE"]
        noise_count   = sum(1 for n in news if n.get("impact", {}).get("impact") == "NOISE")

        lines.append("📰 <b>НОВОСТИ — ВЛИЯНИЕ НА СЕТАПЫ</b>")
        seen_news: set = set()

        for n in relevant_news[:7]:
            snippet = n.get("raw", "")[:160]
            key = snippet[:40]
            if key in seen_news or not snippet:
                continue
            seen_news.add(key)

            imp      = n.get("impact", {})
            imp_type = imp.get("impact", "NOISE")
            strength = imp.get("strength", "LOW")
            reason   = imp.get("reason", "")
            coins    = imp.get("coins", [])
            icon, label, _ = IMPACT_META.get(imp_type, ("➡️", "?", None))
            dots = STRENGTH_DOT.get(strength, "")

            lines.append(f"{icon} <b>{label}</b>  <code>{dots}</code>")
            lines.append(f"  <b>@{n['channel']}</b>: <i>{_esc(snippet)}</i>")
            if reason:
                lines.append(f"  → {_esc(reason)}")

            # Пересечение со скринером
            for ov in n.get("screener_overlap", [])[:2]:
                badge = "✅ <b>подтверждает наш сетап</b>" if ov["agrees"] else "⚠️ <b>давит на наш сетап</b>"
                lines.append(
                    f"  {badge}: {_esc(ov['symbol'].replace('USDT',''))}"
                    f"  [{ov['sc_dir']}  score={ov['score']}]"
                )

            # Монеты без скринерного пересечения
            if not n.get("screener_overlap") and coins and coins != ["MARKET"]:
                c_str = "  ".join(c.replace("USDT", "") for c in coins[:3] if c != "?")
                if c_str:
                    lines.append(f"  Затрагивает: {c_str}")

            lines.append("")

        if noise_count:
            lines.append(f"  <i>⬜ {noise_count} нерелевантных новостей скрыто</i>")
            lines.append("")

    if not any_content:
        lines.append("<i>Сигналов и новостей за последние 8ч не найдено.</i>")
        lines.append("")

    lines.append(
        "<i>⚠️ Данные каналов — дополнительный контекст.\n"
        "✅ = цена у входа  ⏳ = ждём  🚫 = цена далеко\n"
        "Всегда ставь стоп и перепроверяй структуру.</i>"
    )
    return "\n".join(lines)


# ─── Отправка в Telegram ─────────────────────────────────────────────────────

def tg_send(token: str, chat_id: str, text: str):
    MAX = 4000
    chunks = []
    while len(text) > MAX:
        sp = text.rfind("\n", 0, MAX)
        sp = sp if sp > 0 else MAX
        chunks.append(text[:sp])
        text = text[sp:].lstrip("\n")
    if text:
        chunks.append(text)

    for i, chunk in enumerate(chunks):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if not r.json().get("ok"):
                print(f"[TG] {r.json().get('description')}")
        except Exception as e:
            print(f"[TG] {e}")
        if i < len(chunks) - 1:
            time.sleep(0.4)


def send_insights(verified: dict):
    tg_cfg = load_tg_cfg()
    token  = tg_cfg.get("bot_token")
    if not token or not tg_cfg.get("enabled"):
        print("[TG] Не настроено")
        return

    targets = [str(tg_cfg["chat_id"])]
    for e in tg_cfg.get("extra_chat_ids", []):
        cid = str(e).strip()
        if cid and cid not in targets:
            targets.append(cid)

    msg = format_channel_insights(verified)
    for chat_id in targets:
        tg_send(token, chat_id, msg)
        time.sleep(0.5)
    print(f"[TG] Инсайты каналов отправлены в {len(targets)} чат(а)")


# ─── Сохранение кэша ─────────────────────────────────────────────────────────

def save_cache(channel_results: dict):
    payload = {
        "ts":      datetime.now().isoformat(),
        "results": {
            ch: [
                # Сохраняем raw (обрезанный) — нужен для отображения новостей
                {**{k: v for k, v in item.items() if k != "raw"},
                 "raw": item.get("raw", "")[:220]}
                for item in items
            ]
            for ch, items in channel_results.items()
        },
    }
    CACHE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


# ─── Обогащение результатов скринера ─────────────────────────────────────────

def _load_channel_accuracy() -> dict:
    """Загружает веса каналов из outcome_tracker.channel_accuracy.json."""
    try:
        import outcome_tracker as _ot
        return _ot.get_channel_accuracy()
    except Exception:
        pass
    # Fallback: читаем файл напрямую
    acc_path = Path(__file__).parent / "channel_accuracy.json"
    try:
        return json.loads(acc_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def enrich_with_channel_signals(filtered: list) -> list:
    """
    Читает channel_signals_cache.json и добавляет к каждому результату скринера:
      channel_conf     — каналы, подтверждающие направление сетапа (отсортированы по accuracy)
      channel_conflict — каналы, торгующие против сетапа
      channel_score    — взвешенный score канальных подтверждений (0.0–1.0 per channel)

    Не делает сетевых запросов. Безопасно вызывать всегда: возвращает filtered
    нетронутым, если кэш не существует или устарел.
    """
    if not CACHE_PATH.exists():
        return filtered

    try:
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return filtered

    # Проверяем свежесть кэша
    ts_str = cache.get("ts", "")
    if ts_str:
        try:
            ts = datetime.fromisoformat(ts_str)
            age_h = (datetime.now() - ts.replace(tzinfo=None)).total_seconds() / 3600
            if age_h > LOOKBACK_HOURS:
                return filtered  # кэш устарел
        except Exception:
            pass

    # Загружаем динамические веса каналов (из resolved outcome истории)
    accuracy = _load_channel_accuracy()  # {channel: {n, wins, accuracy}}

    # Агрегируем сигналы по символу из кэша
    symbol_signals: dict = {}   # symbol → {"LONG": [channels], "SHORT": [channels]}
    for channel, items in cache.get("results", {}).items():
        for item in items:
            if item.get("type") != "signal":
                continue
            sym = item.get("symbol", "")
            if not sym:
                continue
            if sym not in symbol_signals:
                symbol_signals[sym] = {"LONG": [], "SHORT": []}
            direction = item.get("direction", "")
            if direction in ("LONG", "SHORT") and channel not in symbol_signals[sym][direction]:
                symbol_signals[sym][direction].append(channel)

    # Добавляем поля к каждому результату скринера
    for r in filtered:
        sym = r.get("symbol", "")
        sigs = symbol_signals.get(sym)
        if not sigs:
            r.setdefault("channel_conf", [])
            r.setdefault("channel_conflict", [])
            r.setdefault("channel_score", 0.0)
            continue

        setup = r.get("setup", "")
        screener_dir = "LONG" if setup in ("squeeze", "breakout") else "SHORT"
        opposite_dir = "SHORT" if screener_dir == "LONG" else "LONG"

        conf_channels = sigs[screener_dir]
        # Сортируем каналы подтверждения по accuracy (лучшие — первыми)
        def _acc(ch): return accuracy.get(ch, {}).get("accuracy", 0.5)
        conf_sorted = sorted(conf_channels, key=_acc, reverse=True)

        # Взвешенный score: сумма accuracy / N (нормализованная убеждённость)
        ch_score = sum(_acc(ch) for ch in conf_sorted) / max(len(conf_sorted), 1) if conf_sorted else 0.0

        r["channel_conf"]     = conf_sorted
        r["channel_conflict"] = sigs[opposite_dir]
        r["channel_score"]    = round(ch_score, 3)

    return filtered


# ─── Обогащение сетапов новостями ────────────────────────────────────────────

STRENGTH_SCORE = {"HIGH": 12, "MEDIUM": 7, "LOW": 3}


def enrich_with_news_impact(filtered: list) -> list:
    """
    Читает channel_signals_cache.json, анализирует каждую новость через
    analyze_news_impact() и добавляет к каждому результату скринера:
      news_confirms     — list[dict]  новости, подтверждающие направление сетапа
      news_risks        — list[dict]  новости, давящие против сетапа
      news_score_delta  — int         суммарная поправка к score (±12/7/3)
    Также сразу корректирует r["score"] на этот delta.

    Безопасно вызывать всегда: при отсутствии или устаревшем кэше ничего
    не меняет и просто проставляет пустые поля.
    """
    # Заполнить дефолты для всех строк — чтобы telegram_alerts.py не падал
    for r in filtered:
        r.setdefault("news_confirms", [])
        r.setdefault("news_risks", [])
        r.setdefault("news_score_delta", 0)
        r.setdefault("news_hard_block", False)
        r.setdefault("news_hard_reason", "")

    if not CACHE_PATH.exists():
        return filtered

    try:
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return filtered

    # Проверяем свежесть кэша
    ts_str = cache.get("ts", "")
    if ts_str:
        try:
            ts = datetime.fromisoformat(ts_str)
            age_h = (datetime.now() - ts.replace(tzinfo=None)).total_seconds() / 3600
            if age_h > LOOKBACK_HOURS:
                return filtered
        except Exception:
            pass

    # Собираем все новости из кэша и анализируем их влияние
    analyzed_news: list = []
    seen_raw: set = set()
    for channel, items in cache.get("results", {}).items():
        for item in items:
            if item.get("type") != "news":
                continue
            raw = item.get("raw", "").strip()
            key = raw[:60]
            if not raw or key in seen_raw:
                continue
            seen_raw.add(key)
            imp = analyze_news_impact(raw, item.get("symbol"))
            if imp.get("impact") == "NOISE":
                continue
            icon, label, _ = IMPACT_META.get(imp["impact"], ("➡️", "?", None))
            analyzed_news.append({
                "impact":    imp["impact"],
                "direction": imp.get("direction"),   # "LONG" / "SHORT" / None
                "strength":  imp.get("strength", "LOW"),
                "reason":    imp.get("reason", ""),
                "coins":     imp.get("coins", []),
                "channel":   channel,
                "raw":       raw[:120],
                "icon":      icon,
                "label":     label,
            })

    if not analyzed_news:
        return filtered

    # Для каждого сетапа: найти подтверждающие и противоречащие новости
    for r in filtered:
        sym = r.get("symbol", "")
        setup = r.get("setup", "")
        screener_dir = "LONG" if setup in ("squeeze", "breakout") else "SHORT"

        confirms: list = []
        risks:    list = []

        for n in analyzed_news:
            n_dir   = n["direction"]   # LONG / SHORT / None
            n_coins = n["coins"]       # ["BTCUSDT", "ETHUSDT"] / ["MARKET"] / [sym]

            # Новость касается нашей монеты или всего рынка
            touches = (
                sym in n_coins
                or "MARKET" in n_coins
                or any(c in ("BTCUSDT", "ETHUSDT") for c in n_coins)
            )
            if not touches:
                continue

            if n_dir == screener_dir:
                confirms.append(n)
            elif n_dir is not None and n_dir != screener_dir:
                risks.append(n)

        # Сортируем по силе (HIGH → MEDIUM → LOW)
        _order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        confirms.sort(key=lambda x: _order.get(x["strength"], 3))
        risks.sort(key=lambda x: _order.get(x["strength"], 3))

        # Считаем дельту score
        delta = 0
        for n in confirms:
            delta += STRENGTH_SCORE.get(n["strength"], 3)
        for n in risks:
            delta -= STRENGTH_SCORE.get(n["strength"], 3)
        # Ограничиваем: не более ±25 очков
        delta = max(-25, min(25, delta))

        r["news_confirms"]    = confirms[:3]
        r["news_risks"]       = risks[:3]
        r["news_score_delta"] = delta
        if delta != 0:
            r["score"] = r.get("score", 0) + delta

        # Hard-block: критические новости HIGH-strength против направления сетапа.
        # Примеры: хак/эксплойт конкретной монеты, SEC иск, геоэскалация.
        # Хард-блок только если новость касается именно этой монеты или MARKET
        # (не общерыночная ETF/ставка — те не блокируют лонг конкретной монеты).
        critical_impacts = {"RISK", "SHORT_MARKET", "SHORT_COIN"}
        blocking = [
            n for n in risks
            if n["strength"] == "HIGH"
            and n["impact"] in critical_impacts
            and (sym in n["coins"] or "MARKET" in n["coins"])
        ]
        if blocking:
            r["news_hard_block"] = True
            r["news_hard_reason"] = blocking[0]["reason"]

    return filtered


# ─── Setup ───────────────────────────────────────────────────────────────────

async def _setup_async():
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    print("\n" + "="*55)
    print("  CHANNEL READER SETUP")
    print("="*55)
    print()
    print("1. Открой https://my.telegram.org")
    print("2. Войди → API development tools")
    print("3. Создай приложение (название любое)")
    print("4. Скопируй App api_id и App api_hash")
    print()

    cfg = load_cfg()

    api_id_s = input("  api_id (число): ").strip()
    api_hash = input("  api_hash:        ").strip()

    if not api_id_s or not api_hash:
        print("Отменено.")
        return

    try:
        api_id = int(api_id_s)
    except ValueError:
        print("api_id должен быть числом.")
        return

    print("\n  Авторизация через Telegram...")
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        session_string = client.session.save()
        me = await client.get_me()
        print(f"  ✓ Авторизован как: {me.first_name} (@{me.username})")

    cfg["api_id"]         = api_id
    cfg["api_hash"]       = api_hash
    cfg["session_string"] = session_string
    save_cfg(cfg)

    print(f"\n  ✓ Конфигурация сохранена в {CFG_PATH}")
    print(f"\n  SESSION STRING (скопируй в GitHub Secret TG_SESSION_STRING):")
    print(f"\n  {session_string}\n")
    print("="*55)


async def _session_async():
    cfg = load_cfg()
    s = cfg.get("session_string", "")
    if s:
        print(f"\nSESSION STRING:\n{s}\n")
    else:
        print("Нет сессии. Запусти: python3 channel_reader.py setup")


async def _scan_async(silent: bool = False):
    cfg = load_cfg()
    if not cfg.get("api_id"):
        print("Не настроено. Запусти: python3 channel_reader.py setup")
        return

    print(f"Читаю {len(CHANNELS)} каналов (последние {LOOKBACK_HOURS}ч)...")
    channel_results = await scan_channels_async(cfg)

    total_signals = sum(
        sum(1 for i in items if i["type"] == "signal")
        for items in channel_results.values()
    )
    total_news = sum(
        sum(1 for i in items if i["type"] == "news")
        for items in channel_results.values()
    )
    print(f"Итого: {total_signals} сигналов, {total_news} новостей")

    save_cache(channel_results)

    verified = cross_verify(channel_results)

    print(f"Совпадений: {len(verified['agreements'])}  "
          f"Расхождений: {len(verified['conflicts'])}  "
          f"Только в каналах: {len(verified['channel_only'])}")

    if silent:
        print("[silent] Кэш обновлён, сообщение в TG не отправляется.")
        return

    # send_insights() — синхронная функция с time.sleep() внутри.
    # Запускаем в пуле потоков, чтобы не блокировать event loop.
    await asyncio.to_thread(send_insights, verified)


async def _test_async():
    cfg = load_cfg()
    if not cfg.get("api_id"):
        print("Не настроено.")
        return

    from telethon import TelegramClient
    from telethon.sessions import StringSession
    async with TelegramClient(
        StringSession(cfg.get("session_string", "")),
        cfg["api_id"], cfg["api_hash"]
    ) as client:
        me = await client.get_me()
        print(f"✓ Подключён как {me.first_name} (@{me.username})")
        print(f"  Каналы для мониторинга: {CHANNELS}")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    silent = "--silent" in sys.argv[1:]

    if cmd == "setup":
        asyncio.run(_setup_async())
    elif cmd == "session":
        asyncio.run(_session_async())
    elif cmd == "scan":
        asyncio.run(_scan_async(silent=silent))
    elif cmd == "test":
        asyncio.run(_test_async())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

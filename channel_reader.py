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
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

# ─── Пути ────────────────────────────────────────────────────────────────────

DIR         = Path(__file__).parent
CFG_PATH    = DIR / "channels_config.json"
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
]

# Сколько часов назад брать сообщения
LOOKBACK_HOURS = 8

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
}

# Ключевые слова направления
LONG_KW  = ["long", "buy", "лонг", "покупка", "покупать", "бычий", "bullish",
             "🟢", "📈", "⬆️", "лонгуем", "лонг 🚀", "лонг 📈"]
SHORT_KW = ["short", "sell", "шорт", "продажа", "продавать", "медвежий", "bearish",
             "🔴", "📉", "⬇️", "шортуем"]

# Ключевые слова новостей
NEWS_KW = [
    "фед", "fed", "пауэлл", "powell", "ставка", "rate", "etf", "etfs",
    "sec", "регулятор", "регуляция", "regulation", "hack", "взлом",
    "ликвидации", "liquidations", "whale", "кит", "нарратив", "narrative",
    "inflation", "инфляция", "cpi", "payroll", "gdp", "вбп",
    "halving", "халвинг", "airdrop", "listing", "делистинг",
    "breaking", "срочно", "🔥", "⚡", "⚠️", "важно",
]

# Стоп-слова (реклама / мусор)
SPAM_KW = [
    "vip", "вип", "подписка", "subscription", "premium", "join",
    "вступай", "канал", "курс", "обучение", "school", "promo",
    "t.me/", "https://t.me", "реферал", "referral", "бонус", "bonus",
    "@", "скидка", "discount",
]


# ─── Конфигурация ─────────────────────────────────────────────────────────────

def load_cfg() -> dict:
    if CFG_PATH.exists():
        return json.loads(CFG_PATH.read_text(encoding="utf-8"))
    return {}


def save_cfg(cfg: dict):
    CFG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_tg_cfg() -> dict:
    if TG_CFG_PATH.exists():
        return json.loads(TG_CFG_PATH.read_text(encoding="utf-8"))
    return {}


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
    """Извлекает символ из текста."""
    text_up = text.upper()
    # Сначала ищем паттерны типа BTC/USDT, BTCUSDT, BTC-PERP
    for pat in [
        r'\b([A-Z]{2,6})/USDT\b',
        r'\b([A-Z]{2,6})USDT\b',
        r'\b([A-Z]{2,6})-PERP\b',
        r'\b([A-Z]{2,6})-USD\b',
    ]:
        m = re.search(pat, text_up)
        if m:
            base = m.group(1)
            if base in SYMBOL_MAP:
                return SYMBOL_MAP[base]
            # Если сам символ уже в форме XXXUSDT — вернём как есть
            candidate = base + "USDT"
            if len(base) <= 6:
                return candidate

    # Потом ищем просто тикеры
    for sym, bybit_sym in SYMBOL_MAP.items():
        if re.search(rf'\b{sym}\b', text_up):
            return bybit_sym

    return None


def _spam_score(text: str) -> int:
    """Возвращает количество спам-маркеров."""
    text_lo = text.lower()
    return sum(1 for kw in SPAM_KW if kw in text_lo)


def parse_message(text: str, channel: str) -> dict:
    """
    Парсит одно сообщение и возвращает структурированный результат.

    Тип результата:
      "signal" — торговый сигнал (символ + направление)
      "news"   — рыночная новость
      "skip"   — реклама / нерелевантно
    """
    if not text or len(text) < 10:
        return {"type": "skip"}

    # Фильтр спама
    if _spam_score(text) >= 2:
        return {"type": "skip"}

    text_lo = text.lower()

    # Пробуем распознать сигнал
    symbol    = _extract_symbol(text)
    direction = None

    if any(kw in text_lo for kw in LONG_KW):
        direction = "LONG"
    elif any(kw in text_lo for kw in SHORT_KW):
        direction = "SHORT"

    if symbol and direction:
        entry = _extract_price(text, ["entry", "вход", "цена входа", "zone", "зона"])
        sl    = _extract_price(text, ["sl", "stop loss", "стоп", "stop"])
        tp1   = _extract_price(text, ["tp1", "тп1", "take profit 1", "tp 1", "цель 1"])
        tp2   = _extract_price(text, ["tp2", "тп2", "take profit 2", "tp 2", "цель 2"])

        # Качество сигнала: сколько уровней задано
        levels_defined = sum(1 for x in [entry, sl, tp1] if x is not None)

        return {
            "type":      "signal",
            "channel":   channel,
            "symbol":    symbol,
            "direction": direction,
            "entry":     entry,
            "sl":        sl,
            "tp1":       tp1,
            "tp2":       tp2,
            "quality":   levels_defined,   # 0=только тикер, 3=полный план
            "raw":       text[:300],
        }

    # Пробуем распознать новость
    news_count = sum(1 for kw in NEWS_KW if kw in text_lo)
    if news_count >= 2 or (news_count >= 1 and len(text) > 80):
        # Кратко: первые 200 символов без переносов
        snippet = " ".join(text.split())[:200]
        return {
            "type":    "news",
            "channel": channel,
            "raw":     snippet,
        }

    return {"type": "skip"}


# ─── Чтение каналов через Telethon ───────────────────────────────────────────

async def _fetch_channel_messages(client, channel: str, hours: int = LOOKBACK_HOURS) -> list:
    """Получает сообщения из одного канала за последние `hours` часов."""
    from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
    from telethon.errors import ChannelPrivateError, UsernameNotOccupiedError

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    messages = []

    try:
        entity = await client.get_entity(channel)
        async for msg in client.iter_messages(entity, limit=50):
            if msg.date < cutoff:
                break
            if not msg.text:
                continue
            messages.append(msg.text)
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
            for text in msgs:
                p = parse_message(text, channel)
                if p["type"] != "skip":
                    parsed.append(p)
            results[channel] = parsed
            print(f"    → {len(msgs)} сообщений, {len(parsed)} релевантных")

    return results


# ─── Кросс-верификация со скринером ──────────────────────────────────────────

def cross_verify(channel_results: dict) -> dict:
    """
    Сопоставляет сигналы каналов с последними результатами скринера.
    Возвращает: {agreements, conflicts, channel_only, news, multi_channel}
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
    symbol_signals: dict = {}   # symbol → {LONG: [channels], SHORT: [channels], data: [...]}
    all_news: list = []

    for channel, items in channel_results.items():
        for item in items:
            if item["type"] == "news":
                all_news.append(item)
                continue
            if item["type"] != "signal":
                continue

            sym = item["symbol"]
            if sym not in symbol_signals:
                symbol_signals[sym] = {"LONG": [], "SHORT": [], "data": []}
            symbol_signals[sym][item["direction"]].append(channel)
            symbol_signals[sym]["data"].append(item)

    agreements   = []   # Канал + скринер согласны
    conflicts    = []   # Канал противоречит скринеру
    channel_only = []   # Сигнал в каналах, но нет в скринере (интересно!)
    multi_channel = []  # Несколько каналов согласны между собой

    for sym, sigs in symbol_signals.items():
        long_sources  = sigs["LONG"]
        short_sources = sigs["SHORT"]
        data          = sigs["data"]

        # Несколько каналов согласны — само по себе ценно
        if len(long_sources) >= 2:
            multi_channel.append({
                "symbol": sym, "direction": "LONG",
                "channels": long_sources, "count": len(long_sources),
            })
        if len(short_sources) >= 2:
            multi_channel.append({
                "symbol": sym, "direction": "SHORT",
                "channels": short_sources, "count": len(short_sources),
            })

        # Определяем доминирующее направление в каналах
        if len(long_sources) >= len(short_sources):
            ch_dir    = "LONG"
            ch_sources = long_sources
        else:
            ch_dir    = "SHORT"
            ch_sources = short_sources

        if not ch_sources:
            continue

        if sym in screener_map:
            r            = screener_map[sym]
            screener_setup = r.get("setup", "")
            screener_dir = "LONG" if screener_setup in ("squeeze", "breakout") else "SHORT"

            best_sig = sorted(data, key=lambda x: x.get("quality", 0), reverse=True)[0]

            if ch_dir == screener_dir:
                agreements.append({
                    "symbol":     sym,
                    "direction":  ch_dir,
                    "score":      r.get("score", 0),
                    "setup":      screener_setup,
                    "channels":   ch_sources,
                    "entry":      best_sig.get("entry"),
                    "sl":         best_sig.get("sl"),
                    "tp1":        best_sig.get("tp1"),
                    "quality":    best_sig.get("quality", 0),
                    "price":      r.get("price"),
                })
            else:
                conflicts.append({
                    "symbol":       sym,
                    "channel_dir":  ch_dir,
                    "screener_dir": screener_dir,
                    "score":        r.get("score", 0),
                    "channels":     ch_sources,
                    "raw":          best_sig.get("raw", ""),
                })
        else:
            # Сигнал есть в каналах, но нет в нашем скринере
            best_sig = sorted(data, key=lambda x: x.get("quality", 0), reverse=True)[0]
            if best_sig.get("quality", 0) >= 2:  # только полные сигналы (entry+sl+tp)
                channel_only.append({
                    "symbol":    sym,
                    "direction": ch_dir,
                    "channels":  ch_sources,
                    "entry":     best_sig.get("entry"),
                    "sl":        best_sig.get("sl"),
                    "tp1":       best_sig.get("tp1"),
                    "raw":       best_sig.get("raw", "")[:150],
                })

    # Сортировка
    agreements.sort(key=lambda x: (len(x["channels"]), x.get("score", 0)), reverse=True)
    conflicts.sort(key=lambda x: x.get("score", 0), reverse=True)

    return {
        "agreements":    agreements,
        "conflicts":     conflicts,
        "channel_only":  channel_only,
        "news":          all_news[:8],
        "multi_channel": multi_channel,
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
    now = datetime.now().strftime("%d.%m %H:%M")
    lines = [
        f"📡 <b>СИГНАЛЫ КАНАЛОВ  {now}  (последние {LOOKBACK_HOURS}ч)</b>",
        f"<i>@{'  @'.join(CHANNELS)}</i>",
        "",
    ]

    # ── Несколько каналов согласны между собой ────────────────────────────────
    mc = verified.get("multi_channel", [])
    if mc:
        lines.append("🤝 <b>КОНСЕНСУС КАНАЛОВ</b>  (≥2 канала согласны)")
        for item in mc[:4]:
            icon = "🟢" if item["direction"] == "LONG" else "🔴"
            src  = " ".join(f"@{c}" for c in item["channels"])
            lines.append(
                f"  {icon} <b>{_esc(item['symbol'])}</b>  {item['direction']}"
                f"  ← {src}"
            )
        lines.append("")

    # ── Совпадения со скринером ───────────────────────────────────────────────
    agr = verified.get("agreements", [])
    if agr:
        lines.append("✅ <b>СОВПАДАЮТ со скринером</b>")
        for a in agr[:5]:
            icon  = "🟢" if a["direction"] == "LONG" else "🔴"
            src   = " + ".join(f"@{c}" for c in a["channels"])
            score = a.get("score", 0)
            setup_short = {
                "squeeze": "SQZ", "bos_fvg": "BOS", "breakout": "PUMP", "range_sweep": "SWEEP"
            }.get(a.get("setup", ""), "")

            entry_str = f"  Entry <code>{_fmt_p(a.get('entry'))}</code>" if a.get("entry") else ""
            sl_str    = f"  SL <code>{_fmt_p(a.get('sl'))}</code>"       if a.get("sl")    else ""
            tp_str    = f"  TP1 <code>{_fmt_p(a.get('tp1'))}</code>"     if a.get("tp1")   else ""

            lines.append(
                f"  {icon} <b>{_esc(a['symbol'])}</b>  [{setup_short}]  score={score}"
            )
            lines.append(f"  <i>← {_esc(src)}</i>")
            if entry_str or sl_str or tp_str:
                lines.append(f"  {entry_str}{sl_str}{tp_str}".strip())
            lines.append("")
    else:
        lines.append("✅ <b>Совпадений со скринером нет</b>")
        lines.append("")

    # ── Сигналы только в каналах (не в скринере) ─────────────────────────────
    co = verified.get("channel_only", [])
    if co:
        lines.append("🔍 <b>ТОЛЬКО В КАНАЛАХ</b>  (нет в скринере — проверь вручную)")
        for item in co[:3]:
            icon = "🟢" if item["direction"] == "LONG" else "🔴"
            src  = " + ".join(f"@{c}" for c in item["channels"])
            lines.append(
                f"  {icon} <b>{_esc(item['symbol'])}</b>  {item['direction']}  ← {_esc(src)}"
            )
            if item.get("entry"):
                lines.append(
                    f"   Entry <code>{_fmt_p(item['entry'])}</code>"
                    f"  SL <code>{_fmt_p(item['sl'])}</code>"
                    f"  TP1 <code>{_fmt_p(item['tp1'])}</code>"
                )
        lines.append("")

    # ── Расхождения ───────────────────────────────────────────────────────────
    conf = verified.get("conflicts", [])
    if conf:
        lines.append("⚔️ <b>РАСХОЖДЕНИЯ</b>  (каналы vs скринер)")
        for c in conf[:3]:
            src = " + ".join(f"@{ch}" for ch in c["channels"])
            lines.append(
                f"  ⚠️ <b>{_esc(c['symbol'])}</b>"
                f"  каналы={c['channel_dir']}  скринер={c['screener_dir']}"
                f"  score={c.get('score', 0)}"
            )
            lines.append(f"  <i>← {_esc(src)}</i>")
            if c.get("raw"):
                lines.append(f"  <i>«{_esc(c['raw'][:100])}»</i>")
            lines.append("")

    # ── Новости ───────────────────────────────────────────────────────────────
    news = verified.get("news", [])
    if news:
        lines.append("📰 <b>НОВОСТИ И НАРРАТИВЫ</b>")
        seen = set()
        for n in news[:5]:
            snippet = n.get("raw", "")[:160]
            key = snippet[:40]
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"  <b>@{n['channel']}</b>: {_esc(snippet)}")
        lines.append("")

    if not agr and not co and not conf and not mc and not news:
        lines.append("<i>Сигналов за последние 8ч не найдено.</i>")

    lines.append(
        "<i>⚠️ Данные каналов — дополнительный контекст, не торговый совет.\n"
        "Всегда перепроверяй со скринером и ставь стоп.</i>"
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
                {k: v for k, v in item.items() if k != "raw"}
                for item in items
            ]
            for ch, items in channel_results.items()
        },
    }
    CACHE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


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


async def _scan_async():
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

    send_insights(verified)


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

    if cmd == "setup":
        asyncio.run(_setup_async())
    elif cmd == "session":
        asyncio.run(_session_async())
    elif cmd == "scan":
        asyncio.run(_scan_async())
    elif cmd == "test":
        asyncio.run(_test_async())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

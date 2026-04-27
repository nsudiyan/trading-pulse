"""
liquidation_tracker.py — Реальные ликвидации по всем монетам

Источник:
  Bybit WebSocket  wss://stream.bybit.com/v5/public/linear
  topic: allLiquidation.{SYMBOL}
  Поля (короткие ключи): s=symbol, S=side, p=price, v=qty, T=timestamp
  side: "Sell" → long_liq (движок продаёт за ликвидируемого лонгиста)
        "Buy"  → short_liq (движок покупает за ликвидируемого шортиста)
  Данные реальные, не аппроксимация. Правильный топик: allLiquidation (не liquidation).

  ⚠️  Hyperliquid отключён: публичный WS не имеет поля "liquidation" в trades.
      Нулевые хэши (hash=0x000…) — это funding settlements + ADL, не ликвидации.
      Достоверных публичных данных о ликвидациях HL нет без приватного адреса.

Хранение: SQLite (liquidations.db) — накапливается бессрочно.
Вывод:
  - ASCII-хитмап по ценовым уровням (стиль CoinGlass, все монеты)
  - Telegram-алерт при одиночной ликвидации >= порога
  - Периодическая сводка топ-N монет по ликвидациям
  - CLI: --heatmap SYMBOL, --stats, --large

Использование:
  python3 liquidation_tracker.py                    # запуск сборщика
  python3 liquidation_tracker.py --heatmap BTCUSDT  # хитмап из DB
  python3 liquidation_tracker.py --stats            # сводка по всем монетам
  python3 liquidation_tracker.py --large            # крупные ликвидации за 1h
  python3 liquidation_tracker.py --top 50 --no-hl  # только Bybit, 50 монет
"""

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import requests

try:
    import websockets
    _WS_OK = True
except ImportError:
    _WS_OK = False

# Telegram: опциональный импорт
try:
    import telegram_alerts as _tg_mod
    _TG_OK = True
except ImportError:
    _TG_OK = False

# ─── Конфиг ───────────────────────────────────────────────────────────────────

BASE_BYBIT   = "https://api.bybit.com"
WS_BYBIT     = "wss://stream.bybit.com/v5/public/linear"
WS_HL        = "wss://api.hyperliquid.xyz/ws"
DB_PATH      = os.path.join(os.path.dirname(__file__), "liquidations.db")

BYBIT_BATCH  = 10   # топиков на одно WS-соединение Bybit
HL_BATCH     = 10   # монет на одно WS-соединение Hyperliquid
HEATMAP_PCTS = 0.5  # ширина уровня хитмапа в % от диапазона цен / число бакетов

LOG = logging.getLogger("liq")


# ─── SQLite ───────────────────────────────────────────────────────────────────

def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    """Создаёт (или открывает) базу и нужные таблицы/индексы."""
    con = sqlite3.connect(path, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS liquidations (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      INTEGER NOT NULL,        -- unix milliseconds
            symbol  TEXT    NOT NULL,        -- BTCUSDT, ETHUSDT, ...
            side    TEXT    NOT NULL,        -- long_liq | short_liq
            price   REAL    NOT NULL,
            qty     REAL    NOT NULL,        -- контракты / монеты
            usd     REAL    NOT NULL,        -- price × qty
            source  TEXT    NOT NULL         -- bybit | hyperliquid
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_ts  ON liquidations(ts)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_sym ON liquidations(symbol, ts)")
    con.commit()
    return con


def _insert(con: sqlite3.Connection, ts: int, symbol: str, side: str,
            price: float, qty: float, source: str) -> float:
    """Записывает одну ликвидацию, возвращает USD-размер."""
    usd = price * qty
    con.execute(
        "INSERT INTO liquidations(ts,symbol,side,price,qty,usd,source) "
        "VALUES(?,?,?,?,?,?,?)",
        (ts, symbol, side, price, qty, usd, source),
    )
    con.commit()
    return usd


# ─── Запросы / аналитика ─────────────────────────────────────────────────────

def get_stats(con: sqlite3.Connection, window_h: float = 24,
              limit: int = 50) -> list[dict]:
    """Сводка ликвидаций по символам: total/long/short USD, кол-во."""
    since = int((time.time() - window_h * 3600) * 1000)
    rows = con.execute("""
        SELECT symbol,
               SUM(usd)                                   AS total,
               SUM(CASE WHEN side='long_liq'  THEN usd ELSE 0 END) AS long_usd,
               SUM(CASE WHEN side='short_liq' THEN usd ELSE 0 END) AS short_usd,
               COUNT(*)                                   AS cnt
        FROM liquidations WHERE ts >= ?
        GROUP BY symbol ORDER BY total DESC LIMIT ?
    """, (since, limit)).fetchall()
    return [
        {"symbol": r[0], "total": r[1], "long_usd": r[2],
         "short_usd": r[3], "count": r[4]}
        for r in rows
    ]


def get_large(con: sqlite3.Connection, min_usd: float = 100_000,
              window_h: float = 1) -> list[dict]:
    """Крупные одиночные ликвидации за окно."""
    since = int((time.time() - window_h * 3600) * 1000)
    rows = con.execute(
        "SELECT ts,symbol,side,price,qty,usd,source "
        "FROM liquidations WHERE ts>=? AND usd>=? ORDER BY usd DESC LIMIT 50",
        (since, min_usd),
    ).fetchall()
    return [
        {"ts": r[0], "symbol": r[1], "side": r[2], "price": r[3],
         "qty": r[4], "usd": r[5], "source": r[6]}
        for r in rows
    ]


def get_heatmap(con: sqlite3.Connection, symbol: str,
                window_h: float = 24, buckets: int = 60) -> dict:
    """
    Агрегация ликвидаций по ценовым уровням (бакетам).
    Возвращает OrderedDict {bucket_price: {long, short, total}}.
    """
    since = int((time.time() - window_h * 3600) * 1000)
    rows = con.execute(
        "SELECT price, usd, side FROM liquidations WHERE symbol=? AND ts>=?",
        (symbol.upper(), since),
    ).fetchall()

    if not rows:
        return {}

    prices = [r[0] for r in rows]
    lo, hi = min(prices), max(prices)
    if lo >= hi:
        return {}

    step = (hi - lo) / buckets
    data: dict = defaultdict(lambda: {"long": 0.0, "short": 0.0, "total": 0.0})

    for price, usd, side in rows:
        b = lo + int((price - lo) / step) * step
        key = "long" if side == "long_liq" else "short"
        data[b][key]   += usd
        data[b]["total"] += usd

    return dict(sorted(data.items()))


# ─── Вывод хитмапа в терминал ─────────────────────────────────────────────────

def print_heatmap(con: sqlite3.Connection, symbol: str,
                  window_h: float = 24, buckets: int = 60, bar_w: int = 32):
    """ASCII-хитмап ликвидаций, стиль CoinGlass."""
    hmap = get_heatmap(con, symbol, window_h=window_h, buckets=buckets)
    if not hmap:
        print(f"[{symbol}] нет данных за {window_h}h в {DB_PATH}")
        return

    total    = sum(v["total"]  for v in hmap.values())
    long_sum = sum(v["long"]   for v in hmap.values())
    shor_sum = sum(v["short"]  for v in hmap.values())
    max_val  = max(v["total"]  for v in hmap.values()) or 1.0

    print(f"\n{'═'*66}")
    print(f"  LIQUIDATION HEATMAP  —  {symbol}  ({window_h:.0f}h)")
    print(f"  Total ${total/1e6:.3f}M  │  "
          f"Longs ${long_sum/1e6:.3f}M  │  Shorts ${shor_sum/1e6:.3f}M")
    print(f"{'═'*66}")
    print(f"  {'Цена':>12}  {'Ликвидации (▓=long  ░=short)':^{bar_w}}  {'USD':>9}")
    print(f"  {'─'*12}  {'─'*bar_w}  {'─'*9}")

    for price, vals in reversed(list(hmap.items())):
        long_w  = int(vals["long"]  / max_val * bar_w)
        short_w = int(vals["short"] / max_val * bar_w)
        # Убедимся, что bar не превышает bar_w
        long_w  = min(long_w,  bar_w)
        short_w = min(short_w, bar_w - long_w)
        bar = "▓" * long_w + "░" * short_w
        bar = bar.ljust(bar_w)
        usd_str = f"${vals['total']/1e3:>7.1f}k"
        print(f"  {price:>12.4f}  {bar}  {usd_str}")

    print(f"{'═'*66}")
    print(f"  ▓ = long ликвидации  │  ░ = short ликвидации\n")


def print_stats(con: sqlite3.Connection, window_h: float = 24):
    """Таблица топ-30 монет по объёму ликвидаций."""
    try:
        from tabulate import tabulate
        _tabulate = tabulate
    except ImportError:
        _tabulate = None

    stats = get_stats(con, window_h=window_h, limit=30)
    if not stats:
        print(f"Нет данных за {window_h}h в {DB_PATH}")
        return

    rows = []
    for s in stats:
        dom = "LONG" if s["long_usd"] > s["short_usd"] else "SHORT"
        rows.append([
            s["symbol"],
            f"${s['total']/1e6:.3f}M",
            f"${s['long_usd']/1e6:.3f}M",
            f"${s['short_usd']/1e6:.3f}M",
            dom,
            s["count"],
        ])

    headers = ["Symbol", "Total", "Longs", "Shorts", "Dom", "Events"]
    if _tabulate:
        print(f"\n  Ликвидации за {window_h}h  ({DB_PATH})\n")
        print(_tabulate(rows, headers=headers, tablefmt="simple"))
    else:
        print("  ".join(headers))
        for r in rows:
            print("  ".join(str(x) for x in r))


def print_large(con: sqlite3.Connection, min_usd: float = 50_000,
                window_h: float = 1):
    """Крупные ликвидации за окно."""
    events = get_large(con, min_usd=min_usd, window_h=window_h)
    if not events:
        print(f"Крупных ликвидаций (>=${min_usd/1e3:.0f}k) за {window_h}h не найдено.")
        return

    print(f"\n  Крупные ликвидации >${min_usd/1e3:.0f}k за {window_h}h\n")
    for e in events:
        dt   = datetime.fromtimestamp(e["ts"] / 1000, tz=timezone.utc)
        icon = "🔴 LONG " if e["side"] == "long_liq" else "🟢 SHORT"
        print(f"  {dt:%H:%M:%S}  {icon}  {e['symbol']:<12}  "
              f"${e['usd']/1e3:>8.1f}k  @ {e['price']:.4f}  [{e['source']}]")


# ─── Форматирование для Telegram ──────────────────────────────────────────────

def tg_format_heatmap(con: sqlite3.Connection, symbol: str,
                      window_h: float = 4, buckets: int = 30) -> str:
    """
    Возвращает хитмап ликвидаций как HTML-строку для Telegram.
    Использует <code> для выравнивания, ▓/░ для лонг/шорт.
    """
    hmap = get_heatmap(con, symbol.upper(), window_h=window_h, buckets=buckets)
    if not hmap:
        return f"💥 <b>{symbol.upper()}</b> — нет данных за {window_h:.0f}h.\n<i>Сборщик должен быть запущен.</i>"

    total    = sum(v["total"]  for v in hmap.values())
    long_sum = sum(v["long"]   for v in hmap.values())
    shor_sum = sum(v["short"]  for v in hmap.values())
    max_val  = max(v["total"]  for v in hmap.values()) or 1.0
    BAR_W    = 20

    lines = [
        f"💥 <b>LIQ HEATMAP — {symbol.upper()} ({window_h:.0f}h)</b>",
        f"Total <b>${total/1e6:.2f}M</b>  |  "
        f"▓Longs <b>${long_sum/1e6:.2f}M</b>  |  "
        f"░Shorts <b>${shor_sum/1e6:.2f}M</b>",
        "",
        "<code>",
    ]

    for price, vals in reversed(list(hmap.items())):
        if vals["total"] == 0:
            continue
        long_w  = int(vals["long"]  / max_val * BAR_W)
        short_w = int(vals["short"] / max_val * BAR_W)
        long_w  = min(long_w,  BAR_W)
        short_w = min(short_w, BAR_W - long_w)
        bar = "▓" * long_w + "░" * short_w
        bar = bar.ljust(BAR_W)

        # Цена: компактно (без лишних нулей)
        if price >= 1000:
            px_str = f"{price:>8.1f}"
        elif price >= 1:
            px_str = f"{price:>8.3f}"
        else:
            px_str = f"{price:>8.5f}"

        usd_str = f"${vals['total']/1e3:>6.0f}k" if vals["total"] < 1e6 else f"${vals['total']/1e6:>5.1f}M"
        lines.append(f"{px_str} {bar} {usd_str}")

    lines.append("</code>")
    lines.append("<i>▓ long liq  ░ short liq</i>")
    return "\n".join(lines)


def tg_format_stats(con: sqlite3.Connection, window_h: float = 24,
                    limit: int = 15) -> str:
    """Сводная таблица топ-N монет по объёму ликвидаций для Telegram."""
    stats = get_stats(con, window_h=window_h, limit=limit)
    if not stats:
        return (f"📊 Нет данных за {window_h:.0f}h.\n"
                "<i>Запусти сборщик: python3 liquidation_tracker.py</i>")

    total_all = sum(s["total"] for s in stats)
    lines = [
        f"📊 <b>ЛИКВИДАЦИИ — топ {limit} ({window_h:.0f}h)</b>",
        f"Рынок всего: <b>${total_all/1e6:.2f}M</b>",
        "",
        "<code>",
        f"{'Symbol':<12} {'Total':>7} {'Longs':>7} {'Shrts':>7} Dom",
        "─" * 42,
    ]
    for s in stats:
        dom  = "LONG " if s["long_usd"] > s["short_usd"] else "SHORT"
        icon = "🔴" if dom.strip() == "LONG" else "🟢"
        t    = f"${s['total']/1e6:.2f}M"
        l    = f"${s['long_usd']/1e6:.2f}M"
        sh   = f"${s['short_usd']/1e6:.2f}M"
        lines.append(f"{s['symbol']:<12} {t:>7} {l:>7} {sh:>7} {icon}{dom}")
    lines.append("</code>")
    return "\n".join(lines)


def tg_format_large(con: sqlite3.Connection, min_usd: float = 100_000,
                    window_h: float = 1) -> str:
    """Крупные ликвидации за окно для Telegram."""
    events = get_large(con, min_usd=min_usd, window_h=window_h)
    if not events:
        return (f"🔍 Крупных ликвидаций >${min_usd/1e3:.0f}k "
                f"за {window_h:.0f}h не найдено.")

    total = sum(e["usd"] for e in events)
    lines = [
        f"💥 <b>КРУПНЫЕ ЛИКВИДАЦИИ >{min_usd/1e3:.0f}k ({window_h:.0f}h)</b>",
        f"Всего: <b>${total/1e6:.2f}M</b>  |  событий: {len(events)}",
        "",
    ]
    for e in events[:20]:
        dt   = datetime.fromtimestamp(e["ts"] / 1000, tz=timezone.utc)
        icon = "🔴" if e["side"] == "long_liq" else "🟢"
        side = "LONG " if e["side"] == "long_liq" else "SHORT"
        src  = "BY" if e["source"] == "bybit" else "HL"
        usd  = f"${e['usd']/1e3:.0f}k" if e["usd"] < 1e6 else f"${e['usd']/1e6:.2f}M"
        lines.append(
            f"{icon} <b>{e['symbol']}</b> {side}  "
            f"<b>{usd}</b> @ {e['price']:.4f}  "
            f"<i>{dt:%H:%M:%S} [{src}]</i>"
        )
    return "\n".join(lines)


# ─── Telegram helper ──────────────────────────────────────────────────────────

# Глобальный выключатель: --no-tg делает _tg_send no-op (база всё равно копится).
_TG_SILENT = False


def _tg_send(text: str):
    """Отправляет сообщение через telegram_alerts, если настроен."""
    if _TG_SILENT:
        return
    if not _TG_OK:
        return
    try:
        cfg = _tg_mod.load_config()
        if not cfg.get("enabled") or not cfg.get("bot_token") or not cfg.get("chat_id"):
            return
        token = cfg["bot_token"]
        targets = [str(cfg["chat_id"])]
        for extra in cfg.get("extra_chat_ids", []):
            cid = str(extra).strip()
            if cid and cid not in targets:
                targets.append(cid)
        for chat_id in targets:
            _tg_mod._send(token, chat_id, text, parse_mode="HTML")
    except Exception as e:
        LOG.warning("tg_send: %s", e)


# ─── Bybit WebSocket ──────────────────────────────────────────────────────────

async def _bybit_batch(symbols: list[str], con: sqlite3.Connection,
                       alert_usd: float):
    """Одно WS-соединение Bybit на BYBIT_BATCH топиков."""
    topics = [f"allLiquidation.{s}" for s in symbols]
    backoff = 1.0

    while True:
        try:
            async with websockets.connect(
                WS_BYBIT, ping_interval=20, ping_timeout=10,
                open_timeout=15,
            ) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                LOG.info("[bybit] connected, %d symbols", len(symbols))
                backoff = 1.0

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    # Пропускаем служебные ответы (subscribe ack, pong, ...)
                    if not isinstance(msg, dict):
                        continue
                    topic = msg.get("topic", "")
                    if not topic.startswith("allLiquidation."):
                        continue

                    # data может быть списком (batch) или одиночным dict
                    raw_data = msg.get("data", [])
                    entries = raw_data if isinstance(raw_data, list) else [raw_data]

                    for d in entries:
                        if not isinstance(d, dict):
                            continue
                        # Bybit allLiquidation использует короткие ключи:
                        # "s"=symbol, "S"=side, "p"=price, "v"=volume/qty, "T"=timestamp
                        symbol   = d.get("s", "")
                        raw_side = d.get("S", "")
                        # "Sell" = движок продаёт за ликвидируемого лонгиста → long_liq
                        # "Buy"  = движок покупает за ликвидируемого шортиста → short_liq
                        side  = "long_liq" if raw_side == "Sell" else "short_liq"
                        try:
                            price = float(d.get("p", 0))
                            qty   = float(d.get("v", 0))
                            ts    = int(d.get("T", time.time() * 1000))
                        except (TypeError, ValueError):
                            continue

                        if price <= 0 or qty <= 0:
                            continue

                        usd = _insert(con, ts, symbol, side, price, qty, "bybit")
                        LOG.debug("[bybit] %s %s $%.0f @ %.4f", symbol, side, usd, price)

                        if usd >= alert_usd:
                            icon = "🔴" if side == "long_liq" else "🟢"
                            dt   = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                            _tg_send(
                                f"{icon} <b>BYBIT LIQ</b> — {symbol}\n"
                                f"  {'LONG' if side == 'long_liq' else 'SHORT'} "
                                f"${usd/1e3:.1f}k @ {price:.4f}\n"
                                f"  {dt:%H:%M:%S} UTC"
                            )

        except Exception as e:
            LOG.warning("[bybit] batch error: %s — retry in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def bybit_liq_stream(symbols: list[str], con: sqlite3.Connection,
                           alert_usd: float):
    """Запускает параллельные батч-соединения на все символы Bybit."""
    tasks = []
    for i in range(0, len(symbols), BYBIT_BATCH):
        batch = symbols[i:i + BYBIT_BATCH]
        tasks.append(asyncio.create_task(_bybit_batch(batch, con, alert_usd)))
    await asyncio.gather(*tasks)


# ─── Hyperliquid WebSocket ────────────────────────────────────────────────────

async def _hl_batch(coins: list[str], con: sqlite3.Connection, alert_usd: float):
    """Одно WS-соединение Hyperliquid на HL_BATCH монет."""
    backoff = 1.0

    while True:
        try:
            async with websockets.connect(
                WS_HL, ping_interval=30, ping_timeout=15,
                open_timeout=15,
            ) as ws:
                for coin in coins:
                    sub = {
                        "method": "subscribe",
                        "subscription": {"type": "trades", "coin": coin},
                    }
                    await ws.send(json.dumps(sub))
                    await asyncio.sleep(0.15)   # пауза между подписками — HL рвёт при flood
                LOG.info("[hl] connected, %d coins", len(coins))
                backoff = 1.0

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    if msg.get("channel") != "trades":
                        continue

                    for trade in msg.get("data", []):
                        # Ликвидации помечены полем "liquidation" в объекте сделки
                        liq = trade.get("liquidation")
                        if not liq:
                            continue

                        coin   = trade.get("coin", "")
                        symbol = coin + "USDT"
                        try:
                            price = float(trade.get("px", 0))
                            qty   = float(trade.get("sz", 0))
                            ts    = int(trade.get("time", time.time() * 1000))
                        except (TypeError, ValueError):
                            continue

                        # side: "A" = ask/sell → ликвидация лонга
                        #        "B" = bid/buy  → ликвидация шорта
                        raw_side = trade.get("side", "")
                        side = "long_liq" if raw_side == "A" else "short_liq"

                        if price <= 0 or qty <= 0:
                            continue

                        usd = _insert(con, ts, symbol, side, price, qty, "hyperliquid")
                        LOG.debug("[hl] %s %s $%.0f @ %.4f", symbol, side, usd, price)

                        if usd >= alert_usd:
                            icon = "🔴" if side == "long_liq" else "🟢"
                            dt   = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                            _tg_send(
                                f"{icon} <b>HL LIQ</b> — {symbol}\n"
                                f"  {'LONG' if side == 'long_liq' else 'SHORT'} "
                                f"${usd/1e3:.1f}k @ {price:.4f}\n"
                                f"  {dt:%H:%M:%S} UTC"
                            )

        except Exception as e:
            LOG.warning("[hl] batch error: %s — retry in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def hyperliquid_liq_stream(coins: list[str], con: sqlite3.Connection,
                                 alert_usd: float):
    """Запускает параллельные батч-соединения на все монеты Hyperliquid."""
    tasks = []
    for i in range(0, len(coins), HL_BATCH):
        batch = coins[i:i + HL_BATCH]
        tasks.append(asyncio.create_task(_hl_batch(batch, con, alert_usd)))
    await asyncio.gather(*tasks)


# ─── Периодическая сводка в Telegram ─────────────────────────────────────────

async def periodic_report(con: sqlite3.Connection, interval_min: int = 60,
                          top_n: int = 10):
    """Отправляет агрегированную сводку ликвидаций каждые interval_min минут."""
    if not _TG_OK:
        return
    while True:
        await asyncio.sleep(interval_min * 60)
        try:
            stats = get_stats(con, window_h=interval_min / 60, limit=top_n)
            if not stats:
                continue
            lines = [f"📊 <b>Ликвидации за {interval_min}min — топ {top_n}</b>"]
            total_all = sum(s["total"] for s in stats)
            lines.append(f"  Рынок всего: <b>${total_all/1e6:.2f}M</b>\n")
            for s in stats:
                dom_icon = "🔴" if s["long_usd"] > s["short_usd"] else "🟢"
                lines.append(
                    f"{dom_icon} <b>{s['symbol']}</b>: ${s['total']/1e6:.2f}M "
                    f"(L${s['long_usd']/1e6:.2f}M / S${s['short_usd']/1e6:.2f}M)"
                )
            _tg_send("\n".join(lines))
        except Exception as e:
            LOG.warning("periodic_report: %s", e)


# ─── Получение топ-символов Bybit ────────────────────────────────────────────

def fetch_top_symbols(n: int = 80) -> list[str]:
    """Топ N USDT linear perpetuals Bybit по 24h обороту."""
    try:
        r = requests.get(
            f"{BASE_BYBIT}/v5/market/tickers",
            params={"category": "linear"},
            timeout=12,
        )
        r.raise_for_status()
        items = [
            t for t in r.json()["result"]["list"]
            if t["symbol"].endswith("USDT") and float(t.get("turnover24h", 0)) > 0
        ]
        items.sort(key=lambda t: float(t["turnover24h"]), reverse=True)
        syms = [t["symbol"] for t in items[:n]]
        LOG.info("Топ-%d символов загружено", len(syms))
        return syms
    except Exception as e:
        LOG.error("fetch_top_symbols: %s", e)
        return ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]


def fetch_hl_coins() -> set[str]:
    """
    Получает актуальный список монет Hyperliquid через REST /info.
    Возвращает set имён как они записаны на HL (напр. "BTC", "kPEPE").
    """
    try:
        r = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "meta"},
            timeout=10,
        )
        r.raise_for_status()
        coins = {asset["name"] for asset in r.json().get("universe", [])}
        LOG.info("Hyperliquid: %d монет доступно", len(coins))
        return coins
    except Exception as e:
        LOG.warning("fetch_hl_coins: %s — используем пустой список", e)
        return set()


def bybit_to_hl_coin(symbol: str, hl_coins: set[str]) -> str | None:
    """
    Конвертирует Bybit-символ (BTCUSDT) в имя монеты Hyperliquid (BTC).
    Учитывает специфические имена HL (kPEPE, kSHIB и т.д.).
    Возвращает None если монеты нет на HL.
    """
    base = symbol.replace("USDT", "")
    if base in hl_coins:
        return base
    # HL использует префикс "k" для монет с 1000x множителем (PEPE→kPEPE)
    k_name = f"k{base}"
    if k_name in hl_coins:
        return k_name
    return None


# ─── Основной запуск ──────────────────────────────────────────────────────────

async def run(top_n: int = 80, alert_usd: float = 100_000,
              report_min: int = 60, include_hl: bool = True):
    """Запускает все WS-стримы параллельно."""
    con     = init_db()
    symbols = fetch_top_symbols(top_n)

    tasks = [
        bybit_liq_stream(symbols, con, alert_usd=alert_usd),
        periodic_report(con, interval_min=report_min),
    ]

    if include_hl:
        # Фильтруем: только монеты, которые реально есть на Hyperliquid
        hl_known = fetch_hl_coins()
        hl_coins = []
        for sym in symbols:
            coin = bybit_to_hl_coin(sym, hl_known)
            if coin:
                hl_coins.append(coin)
        LOG.info("Hyperliquid: %d монет после фильтрации", len(hl_coins))
        if hl_coins:
            tasks.append(hyperliquid_liq_stream(hl_coins, con, alert_usd=alert_usd))

    LOG.info(
        "Запуск: %d Bybit + %s Hyperliquid | алерт $%.0f | сводка каждые %dmin",
        len(symbols),
        f"{len(hl_coins)} монет" if include_hl else "выкл.",
        alert_usd,
        report_min,
    )

    await asyncio.gather(*tasks)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Liquidation Tracker — Bybit + Hyperliquid WebSocket",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("--top",      type=int,   default=80,
                   help="Число символов Bybit (сортировка по обороту) [80]")
    p.add_argument("--alert",    type=float, default=100_000,
                   help="Порог алерта в USD на одну ликвидацию [100000]")
    p.add_argument("--report",   type=int,   default=60,
                   help="Интервал периодической сводки в Telegram (минуты) [60]")
    p.add_argument("--no-hl",    action="store_true", default=True,
                   help="Не подключаться к Hyperliquid (по умолчанию: выкл, нет надёжных публичных данных)")

    # Режимы запроса к уже накопленной DB
    p.add_argument("--heatmap",  type=str,   default=None, metavar="SYMBOL",
                   help="Показать хитмап для символа (напр. BTCUSDT) и выйти")
    p.add_argument("--stats",    action="store_true",
                   help="Показать сводку по всем монетам из DB и выйти")
    p.add_argument("--large",    action="store_true",
                   help="Показать крупные ликвидации за --window часов")
    p.add_argument("--window",   type=float, default=24.0,
                   help="Окно анализа (часов) для --heatmap/--stats/--large [24]")
    p.add_argument("--min-usd",  type=float, default=50_000,
                   help="Минимальный USD для --large [50000]")
    p.add_argument("--buckets",  type=int,   default=60,
                   help="Число ценовых уровней для --heatmap [60]")
    p.add_argument("--verbose",  action="store_true",
                   help="Логировать каждую ликвидацию (DEBUG)")
    p.add_argument("--no-tg",    action="store_true",
                   help="Не отправлять сообщения в Telegram (база всё равно копится — "
                        "данные доступны скринеру через DB)")

    args = p.parse_args()

    global _TG_SILENT
    _TG_SILENT = bool(args.no_tg)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # ── Режимы только-чтение ──
    if args.heatmap:
        con = init_db()
        print_heatmap(con, args.heatmap.upper(),
                      window_h=args.window, buckets=args.buckets)
        return

    if args.stats:
        con = init_db()
        print_stats(con, window_h=args.window)
        return

    if args.large:
        con = init_db()
        print_large(con, min_usd=args.min_usd, window_h=args.window)
        return

    # ── Режим сборщика ──
    if not _WS_OK:
        print("Нужен пакет 'websockets': pip install websockets")
        sys.exit(1)

    try:
        asyncio.run(run(
            top_n=args.top,
            alert_usd=args.alert,
            report_min=args.report,
            include_hl=False,   # HL отключён: нет надёжного публичного источника ликвидаций
        ))
    except KeyboardInterrupt:
        LOG.info("Остановлено.")


if __name__ == "__main__":
    main()

"""
boost_watcher.py — real-time rug detection via DexScreener boost stream.

Подключается к wss://api.dexscreener.com/token-boosts/latest/v1
Каждый новый платный буст = потенциальная схема pump&dump.
При каждом новом бусте запускает rug_detector.analyze() и шлёт TG алерт
если SUSPICIOUS / RUG_RISK.

Запуск:
    python3 boost_watcher.py
    python3 boost_watcher.py >> boost_watcher.log 2>&1 &  # фоновый
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import websockets

# ── Config ────────────────────────────────────────────────────────────────────
WS_URI          = "wss://api.dexscreener.com/token-boosts/latest/v1"
DS_SEARCH       = "https://api.dexscreener.com/latest/dex/search"
BYBIT_TICKERS   = "https://api.bybit.com/v5/market/tickers?category=linear"
RUG_CHECK_DELAY = 2.0    # seconds between CoinGecko calls (free tier: 30/min)
ALERT_VERDICTS  = {"RUG_RISK", "SUSPICIOUS"}
LOG_PREFIX      = "[Boost]"
MIN_BOOST_AMT   = 50     # фильтр дешёвого спама
BYBIT_CACHE_TTL = 300    # обновлять список Bybit каждые 5 мин


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{LOG_PREFIX} [{ts}] {msg}", flush=True)


# ── Bybit listed symbols cache ────────────────────────────────────────────────
_bybit_symbols: set = set()
_bybit_cache_ts: float = 0.0

def _get_bybit_symbols() -> set:
    global _bybit_symbols, _bybit_cache_ts
    if time.time() - _bybit_cache_ts < BYBIT_CACHE_TTL and _bybit_symbols:
        return _bybit_symbols
    try:
        r = requests.get(BYBIT_TICKERS, timeout=8)
        data = r.json()
        syms = {item["symbol"] for item in (data.get("result", {}).get("list") or [])}
        if syms:
            _bybit_symbols = syms
            _bybit_cache_ts = time.time()
    except Exception:
        pass
    return _bybit_symbols


# ── Symbol lookup via DexScreener ─────────────────────────────────────────────

def _symbol_from_address(chain_id: str, token_address: str) -> Optional[str]:
    """Returns base symbol (e.g. 'BSB') from contract address via DexScreener."""
    try:
        r = requests.get(DS_SEARCH, params={"q": token_address}, timeout=8)
        data = r.json()
    except Exception:
        return None

    pairs = data.get("pairs") or []
    for p in pairs:
        if (p.get("chainId", "").lower() == chain_id.lower()
                and p.get("baseToken", {}).get("address", "").lower()
                    == token_address.lower()):
            sym = p.get("baseToken", {}).get("symbol", "")
            if sym:
                return sym.upper()
    # fallback: first pair regardless of chain
    if pairs:
        sym = pairs[0].get("baseToken", {}).get("symbol", "")
        if sym:
            return sym.upper()
    return None


# ── Process a single boost entry ──────────────────────────────────────────────

def _process_boost(entry: dict, seen: set, tg_cfg: dict):
    """Run rug check on a single boost entry. Only processes Bybit-listed tokens."""
    chain   = entry.get("chainId", "").lower()
    address = (entry.get("tokenAddress") or "").lower()
    if not address or address in seen:
        return
    seen.add(address)

    # Фильтр 1: дешёвый спам
    boost_amt = entry.get("totalAmount") or entry.get("amount") or 0
    if boost_amt < MIN_BOOST_AMT:
        return

    # Фильтр 2: Solana pump.fun (адрес оканчивается на "pump" — всегда мусор)
    if chain == "solana" and address.endswith("pump"):
        return

    _log(f"Новый буст: chain={chain} addr={address[:12]}… boost={boost_amt}")

    # Resolve symbol
    sym_base = _symbol_from_address(chain, entry.get("tokenAddress", ""))
    if not sym_base:
        return

    bybit_sym = sym_base + "USDT"

    # Фильтр 3: токен не торгуется на Bybit perps — нам не интересен
    bybit_syms = _get_bybit_symbols()
    if bybit_syms and bybit_sym not in bybit_syms:
        _log(f"  {bybit_sym} не на Bybit — пропускаем")
        return

    _log(f"  {bybit_sym} на Bybit ✓ — запускаем rug check…")

    # Run rug detector
    try:
        import rug_detector
        result = rug_detector.analyze(bybit_sym)
    except Exception as e:
        _log(f"  rug_detector error: {e}")
        return

    _log(f"  {bybit_sym}: {result['verdict']} (score={result['risk_score']})")

    # Проверяем новости (независимо от вердикта — нужны для контекста)
    news = []
    try:
        import rug_detector as rd
        news = rd.check_news(bybit_sym)
    except Exception:
        pass

    if result["verdict"] in ALERT_VERDICTS:
        # Attach boost context + news to result flags
        boost_ctx = []
        desc = entry.get("description", "")
        if desc:
            boost_ctx.append(f"DexScreener буст: \"{desc[:80]}\"")
        links = entry.get("links") or []
        social = [l.get("url", "") for l in links if l.get("type") in ("twitter", "telegram")]
        if social:
            boost_ctx.append("Соцсети: " + " | ".join(social[:2]))
        if news:
            for n in news[:2]:
                boost_ctx.append(f"📰 [{n['source']}] {n['title'][:70]} ({n['published']})")
        result = dict(result)
        result["flags"] = boost_ctx + result.get("flags", [])

        try:
            import rug_detector as rd
            rd.send_rug_alert(result, cfg=tg_cfg)
        except Exception as e:
            _log(f"  TG alert error: {e}")
        _log(f"  🚨 АЛЕРТ ОТПРАВЛЕН: {bybit_sym} [{result['verdict']}]")

    elif news:
        # Токен чистый по rug check, но есть свежие новости — шлём отдельно
        _send_news_alert(bybit_sym, result, news, entry, tg_cfg)


# ── News-only alert (чистый токен + свежие новости) ──────────────────────────

def _send_news_alert(symbol: str, rug_result: dict, news: list, entry: dict, tg_cfg: dict):
    """Sends TG alert when token is CLEAN/WATCH but has fresh news catalyst."""
    try:
        import telegram_alerts as _tg
        from telegram_alerts import _send, _esc
        token   = tg_cfg.get("bot_token", "")
        chat_id = str(tg_cfg.get("chat_id", ""))
        if not token or not chat_id:
            return

        lines = [
            f"📰 НОВОСТИ + БУСТ — <b>{_esc(symbol)}</b>",
            f"Rug check: 🟢 {rug_result['verdict']} (score={rug_result['risk_score']})",
            "",
        ]
        for n in news[:3]:
            lines.append(f"• <b>{_esc(n['source'])}</b> [{n['published']}]")
            lines.append(f"  {_esc(n['title'][:100])}")
            if n["url"]:
                lines.append(f"  {n['url']}")
            lines.append("")

        desc = (entry.get("description") or "")[:80]
        if desc:
            lines.append(f"DexScreener: \"{_esc(desc)}\"")

        _send(token, chat_id, "\n".join(lines))
        _log(f"  📰 NEWS АЛЕРТ: {symbol} ({len(news)} статей)")
    except Exception as e:
        _log(f"  _send_news_alert error: {e}")


# ── WebSocket main loop ───────────────────────────────────────────────────────

async def _watch(tg_cfg: dict):
    seen: set = set()
    initial_done = False

    while True:
        try:
            _log(f"Подключение к {WS_URI}…")
            async with websockets.connect(
                WS_URI,
                ping_interval=20,
                ping_timeout=30,
                close_timeout=10,
            ) as ws:
                _log("Подключено. Ожидание новых бустов…")

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    # First message: bulk snapshot — mark all as seen, don't alert
                    if not initial_done and isinstance(msg, dict) and "data" in msg:
                        entries = msg.get("data") or []
                        for e in entries:
                            addr = (e.get("tokenAddress") or "").lower()
                            if addr:
                                seen.add(addr)
                        _log(f"Снэпшот: {len(entries)} уже-активных бустов помечены как виденные")
                        initial_done = True
                        continue

                    # Subsequent messages: single boost or array
                    if isinstance(msg, list):
                        entries = msg
                    elif isinstance(msg, dict) and "data" in msg:
                        entries = msg.get("data") or []
                    elif isinstance(msg, dict) and "tokenAddress" in msg:
                        entries = [msg]
                    else:
                        entries = []

                    for entry in entries:
                        _process_boost(entry, seen, tg_cfg)
                        await asyncio.sleep(RUG_CHECK_DELAY)

        except (websockets.exceptions.ConnectionClosed,
                websockets.exceptions.WebSocketException) as e:
            _log(f"WebSocket разорван: {e} — переподключение через 10с…")
            await asyncio.sleep(10)
        except Exception as e:
            _log(f"Ошибка: {e} — переподключение через 15с…")
            await asyncio.sleep(15)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    _log(f"Запуск boost_watcher — {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    _log(f"Мониторинг новых бустов DexScreener → авто rug check")

    try:
        import telegram_alerts as _tg
        tg_cfg = _tg.load_config()
    except Exception:
        tg_cfg = {}

    if not tg_cfg.get("enabled") or not tg_cfg.get("bot_token"):
        _log("ВНИМАНИЕ: Telegram не настроен — алерты не будут отправляться")

    asyncio.run(_watch(tg_cfg))


if __name__ == "__main__":
    main()

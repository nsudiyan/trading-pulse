#!/usr/bin/env python3
"""
trade_watcher.py — вотчер открытых РЕАЛЬНЫХ сделок (P2, 2026-06-07).

Заменяет выключенный signal_monitor. Следит ТОЛЬКО за позициями из
outcomes/trades.json (status=open) и пингует в TG при касании SL/TP1.

Анти-принципы из вскрытия signal_monitor (тот дублировал конвейер ×24,
держал свой реестр active_signals, гонял run_screener + Claude каждые 10мин):
  • НИКАКИХ полных ресканов — не вызывает screener вообще.
  • НИКАКИХ вызовов Claude.
  • НИКАКОЙ второй бухгалтерии — единственный источник правды = trades.json.
    Сам реестр сделок вотчер НЕ ведёт и НЕ пишет; в trades.json НЕ пишет вовсе.
  • При нуле открытых сделок — НОЛЬ сетевых вызовов (главное отличие от монитора,
    который сканировал рынок безусловно).

⚠ ОХВАТ (обязательный дисклеймер): вотчер работает, только пока Мак не спит.
   Это ИНФОРМАЦИОННАЯ страховка, НЕ замена биржевого стопа. Реальный SL всегда
   ставится на бирже — пинг тут лишь напоминание «проверь/закрой».

Анти-спам: каждое событие (сделка × уровень) шлётся ОДИН раз. Отметка —
в сайд-файле outcomes/watcher_state.json под file_lock (НЕ в trades.json:
тот runtime-файл трогать нельзя, и это была бы вторая бухгалтерия). Отметки
для закрытых/исчезнувших сделок выпалываются на каждом цикле.

Сеть: один GET /v5/market/tickers?category=linear на ВСЕ символы сразу
(688 тикеров одним вызовом), тихий backoff с джиттером в стиле liqtracker,
redact_token() в ошибках, без трейсбеков на штатные таймауты.

Запуск:
  python3 trade_watcher.py            # резидентный цикл (раз в 60с)
  DRY_RUN=1 python3 trade_watcher.py  # пинги в stdout, не в TG
  TRADES_PATH=/path/test_trades.json DRY_RUN=1 python3 trade_watcher.py
"""

import json
import logging
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import requests

BASE = Path(__file__).parent

# P1-8c: маскировка bot-токена в логируемых ошибках (URL в requests-исключениях).
try:
    from telegram_alerts import redact_token
except Exception:                                  # автономный запуск без telegram_alerts
    import re as _re_rt
    def redact_token(s):
        return _re_rt.sub(r"/bot\d+:[\w-]+", "/bot<REDACTED>", str(s))

# file_lock — read-modify-write сайд-файла (watcher_state.json) под эксклюзивным локом.
# ВАЖНО: писатель trades.json (trade_logger._save_trades) пишет plain write_text БЕЗ лока,
# поэтому atomic_json_read НЕ взаимоисключается с ним. Защита от torn-read — не лок, а
# перехват JSONDecodeError → default=[] (оборванный файл = «нет открытых» на ОДИН цикл,
# самовосстанавливается на следующем). Для информационной страховки это приемлемо.
from file_lock import atomic_json_read, atomic_json_update

# ─── Конфиг ───────────────────────────────────────────────────────────────────

BYBIT_TICKERS = "https://api.bybit.com/v5/market/tickers"

# Тест-режим: TRADES_PATH подменяет источник, DRY_RUN=1 шлёт в stdout.
TRADES_PATH = Path(os.environ.get("TRADES_PATH") or (BASE / "outcomes" / "trades.json"))
STATE_PATH  = Path(os.environ.get("WATCHER_STATE_PATH") or (BASE / "outcomes" / "watcher_state.json"))
DRY_RUN     = os.environ.get("DRY_RUN", "") not in ("", "0", "false", "False")

POLL_SEC      = 60       # каденс опроса при ≥1 открытой сделке
HTTP_TIMEOUT  = 12       # таймаут запроса тикеров
STABLE_RESET_SEC = 300   # цикл прожил ≥5 мин без сбоя → backoff с начала (стиль liqtracker)

# Штатные сетевые ошибки → одна WARNING-строка без трейсбека (Мак спит → DNS-икоты).
_EXPECTED_NET_ERRORS = (
    requests.exceptions.RequestException,
    ConnectionError, TimeoutError, OSError,
)

LOG = logging.getLogger("trade_watcher")

# Telegram: опциональный импорт (как в liqtracker).
try:
    import telegram_alerts as _tg_mod
    _TG_OK = True
except ImportError:
    _TG_OK = False


# ─── Версия-коммит ────────────────────────────────────────────────────────────

def _git_short_sha() -> str:
    """git rev-parse --short HEAD; фолбэк 'unknown' (git может быть недоступен из launchd)."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, cwd=str(BASE),
        )
        sha = r.stdout.strip()
        return sha if sha else "unknown"
    except Exception:
        return "unknown"


# ─── Чтение открытых сделок ───────────────────────────────────────────────────

def load_open_trades() -> list[dict]:
    """Открытые сделки из trades.json. Единственный источник правды.
    torn-read во время чужой записи → JSONDecodeError → default=[] (пропуск цикла,
    не краш). С писателем НЕ синхронизирован локом (он пишет write_text без лока)."""
    data = atomic_json_read(TRADES_PATH, default=[])
    if not isinstance(data, list):
        return []
    return [t for t in data if isinstance(t, dict) and t.get("status") == "open"]


def _trade_key(t: dict) -> str:
    """Стабильный ключ сделки для отметок анти-спама (trade_id, иначе symbol+entry)."""
    return str(t.get("trade_id") or f"{t.get('symbol')}@{t.get('entry_ts')}")


def _short_id(t: dict) -> str:
    """Короткий хвост trade_id для алерта (#abcd1234)."""
    tid = str(t.get("trade_id") or "")
    return tid[:8] if tid else "—"


# ─── Сетевой слой: один запрос на все тикеры ──────────────────────────────────

def fetch_all_last_prices() -> dict[str, float]:
    """
    ОДИН GET /v5/market/tickers?category=linear → {symbol: last_price}.
    Все ~688 USDT-perp тикеров возвращаются единым вызовом (проверено вживую).
    Бросает наверх — backoff/джиттер обрабатывает вызывающий цикл.
    """
    r = requests.get(BYBIT_TICKERS, params={"category": "linear"}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    body = r.json()
    if body.get("retCode") != 0:
        raise RuntimeError(f"bybit retCode={body.get('retCode')} {body.get('retMsg')}")
    out: dict[str, float] = {}
    for t in body.get("result", {}).get("list", []):
        try:
            out[t["symbol"]] = float(t.get("lastPrice") or 0)
        except (TypeError, ValueError, KeyError):
            continue  # элемент без symbol/нечисловой lastPrice — пропускаем, не валим весь fetch
    return out


# ─── Детекция касаний ─────────────────────────────────────────────────────────

def evaluate_trade(t: dict, price: float) -> list[str]:
    """
    Возвращает список сработавших уровней для сделки при текущей цене:
      "sl"  — касание стоп-лосса
      "tp1" — касание первого тейка
    Направление учитывается:
      long  → SL при price<=sl,  TP1 при price>=tp1
      short → SL при price>=sl,  TP1 при price<=tp1
    sl/tp == None → уровень пропускается (сделка без уровня — см. цикл).
    """
    hits: list[str] = []
    direction = str(t.get("direction") or "long").lower()
    is_long = direction == "long"

    sl = _as_float(t.get("stop_price"))
    tp1 = _as_float(t.get("tp1_price"))

    if sl is not None:
        if (is_long and price <= sl) or (not is_long and price >= sl):
            hits.append("sl")
    if tp1 is not None:
        if (is_long and price >= tp1) or (not is_long and price <= tp1):
            hits.append("tp1")
    return hits


def _as_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


# ─── Сообщения ────────────────────────────────────────────────────────────────

def _fmt_price(p: float) -> str:
    """Компактная цена без лишних нулей (как в liqtracker)."""
    if p >= 1000:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.6f}"


def build_message(t: dict, level: str, price: float) -> str:
    """Текст пинга для уровня. SL — красный, TP1 — зелёный. Направление в подсказке /close."""
    sym = t.get("symbol", "?")
    sid = _short_id(t)
    px = _fmt_price(price)
    if level == "sl":
        # Касание SL: если человек реально был закрыт стопом — фиксируем −1R.
        return (f"🔴 SL коснулся: <b>{sym}</b> @ {px} (#{sid}). "
                f"Если закрыл — /close {sym} -1R")
    # TP1: зелёный.
    return (f"🟢 TP1 коснулся: <b>{sym}</b> @ {px} (#{sid}). "
            f"Если закрыл — /close {sym} +1R")


def emit(t: dict, level: str, price: float):
    """Отправляет пинг: в TG (личка владельца, fallback chat) или в stdout при DRY_RUN."""
    msg = build_message(t, level, price)
    if DRY_RUN:
        print(f"[DRY_RUN] {msg}", flush=True)
        return
    if not _TG_OK:
        LOG.warning("TG-модуль недоступен — пинг не отправлен: %s", msg)
        return
    try:
        cfg = _tg_mod.load_config()
        token = cfg.get("bot_token")
        # Сообщение по открытой сделке — владельцу в личку (как heartbeat),
        # fallback на основной chat_id.
        target = str(cfg.get("owner_chat_id") or cfg.get("chat_id") or "")
        if not token or not target:
            LOG.warning("нет токена/чата — пинг не отправлен")
            return
        _tg_mod._send(token, target, msg)
    except Exception as e:
        LOG.warning("emit: %s", redact_token(e))


# ─── Анти-спам через сайд-файл (НЕ вторая бухгалтерия) ────────────────────────

def process_once(prices: dict[str, float]) -> int:
    """
    Один проход по открытым сделкам при готовых ценах.
    Атомарно: читает watcher_state.json, шлёт новые события, отмечает их,
    выпалывает отметки сделок, которых уже нет среди открытых.
    Возвращает число отправленных пингов.
    """
    open_trades = load_open_trades()
    open_keys = {_trade_key(t) for t in open_trades}

    # Соберём кандидатов ДО лока: какие (key, level, price, trade) сработали сейчас.
    pending: list[tuple[str, str, float, dict]] = []
    no_level_keys: set[str] = set()
    for t in open_trades:
        sym = t.get("symbol")
        price = prices.get(sym)
        if price is None or price <= 0:
            continue  # символа нет в ответе / нулевая цена — молча пропускаем тик
        if _as_float(t.get("stop_price")) is None and _as_float(t.get("tp1_price")) is None:
            no_level_keys.add(_trade_key(t))
            continue  # сделка без sl/tp — нечего сторожить (INFO раз на сделку, см. цикл)
        for level in evaluate_trade(t, price):
            pending.append((_trade_key(t), level, price, t))

    sent = 0

    def _mutate(state):
        nonlocal sent
        if not isinstance(state, dict):
            state = {}
        fired = state.get("fired", {})
        if not isinstance(fired, dict):
            fired = {}

        # Выпалываем отметки сделок, которых больше нет среди открытых
        # (закрылись /close → status!=open, либо исчезли). Сторож остаётся компактным.
        for k in list(fired.keys()):
            if k not in open_keys:
                del fired[k]

        for key, level, price, t in pending:
            seen = fired.setdefault(key, {})
            if seen.get(level):
                continue  # уже пинговали этот уровень для этой сделки
            emit(t, level, price)
            seen[level] = _now_iso()
            sent += 1

        state["fired"] = fired
        state["last_run"] = _now_iso()
        return state

    atomic_json_update(STATE_PATH, _mutate, default={})

    # INFO о сделках без уровней — не на каждый тик: один раз пока они открыты.
    _log_no_level_once(no_level_keys, open_trades)
    return sent


_NO_LEVEL_LOGGED: set[str] = set()


def _log_no_level_once(no_level_keys: set[str], open_trades: list[dict]):
    """Одна INFO-строка на сделку без sl/tp, пока она открыта (не каждый тик)."""
    global _NO_LEVEL_LOGGED
    by_key = {_trade_key(t): t for t in open_trades}
    for k in no_level_keys:
        if k not in _NO_LEVEL_LOGGED:
            sym = (by_key.get(k) or {}).get("symbol", "?")
            LOG.info("сделка %s (%s) без sl/tp — сторожить нечего, пропускаю", sym, k[:8])
            _NO_LEVEL_LOGGED.add(k)
    # чистим память от закрытых
    _NO_LEVEL_LOGGED &= no_level_keys


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ─── Главный цикл ─────────────────────────────────────────────────────────────

def run():
    """Резидентный цикл: пусто → спим без сети; есть сделки → 1 запрос/мин."""
    LOG.info("trade_watcher старт | commit=%s | trades=%s | dry_run=%s",
             _git_short_sha(), TRADES_PATH, DRY_RUN)
    LOG.info("ОХВАТ: пинги работают только пока Мак не спит — информационная "
             "страховка, НЕ замена биржевого стопа.")

    backoff = 1.0
    fails = 0
    cycle_start = None

    while True:
        open_trades = load_open_trades()
        if not open_trades:
            # НОЛЬ сетевых вызовов при пустом trades.json — ключевое отличие от монитора.
            LOG.debug("открытых сделок нет — сплю %dс без сети", POLL_SEC)
            time.sleep(POLL_SEC)
            continue

        cycle_start = time.monotonic()
        try:
            prices = fetch_all_last_prices()
        except Exception as e:
            # Тихий backoff с джиттером (стиль liqtracker P1-7).
            if cycle_start and time.monotonic() - cycle_start >= STABLE_RESET_SEC:
                backoff, fails = 1.0, 0
            fails += 1
            delay = backoff * (0.5 + random.random())   # джиттер 0.5–1.5×
            if isinstance(e, _EXPECTED_NET_ERRORS):
                LOG.warning("тикеры reconnect #%d через %.1fс: %s", fails, delay, redact_token(e))
            else:
                LOG.exception("тикеры НЕОЖИДАННАЯ ошибка — reconnect #%d через %.1fс", fails, delay)
            time.sleep(delay)
            backoff = min(backoff * 2, 60.0)
            continue

        # успешный запрос — сбрасываем backoff
        backoff, fails = 1.0, 0
        try:
            n = process_once(prices)
            if n:
                LOG.info("отправлено пингов: %d (открытых сделок: %d)", n, len(open_trades))
        except Exception as e:
            LOG.warning("process_once: %s", redact_token(e))

        time.sleep(POLL_SEC)


def main():
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("WATCHER_VERBOSE") else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        # P1-7: явный stdout (иначе INFO утекает в *_error.log, основной лог пустой).
        stream=sys.stdout,
    )
    try:
        run()
    except KeyboardInterrupt:
        LOG.info("Остановлено.")


if __name__ == "__main__":
    main()

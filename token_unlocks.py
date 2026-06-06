"""
token_unlocks.py — Календарь токен-анлоков.

Источники данных (все free API умерли в 2026):
  - token.unlocks.app → 404 (ребрендинг в Tokenomist, стал платным)
  - DefiLlama emissions → 402 (требует подписку)
  - CryptoRank → 401 (платный API key)
  - Messari → 404 (deprecated)
  - CoinGecko events → убрали из free tier

ТЕКУЩИЙ ПОДХОД: статический файл outcomes/unlock_schedule.json.
Обновляй вручную раз в неделю (~5 минут).
Источник для заполнения: Cryptorank.io/ru/coins/*/unlocks (UI бесплатный),
                           официальные блоги проектов,
                           TokenUnlocks twitter/substack.

Если файл не обновлялся > 7 дней — логируем WARNING.
При отсутствии символа в файле — возвращаем None (fail-open, не блокируем сигнал).

Использование:
    from token_unlocks import get_upcoming_unlock

    u = get_upcoming_unlock("ARB")
    # {"date": "2026-06-16", "days_until": 23, "pct_of_supply": 4.8, "usd_value": 92_000_000}
    # либо None
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("unlocks")

BASE_DIR = Path(__file__).parent
SCHEDULE_PATH = BASE_DIR / "outcomes" / "unlock_schedule.json"
STALE_DAYS = 7          # WARNING если файл старше этого
HORIZON_DAYS = 30       # ближайший анлок в этом окне
MIN_PCT = 0.0           # фильтр по минимальному % (0 = все)


def _load_schedule() -> dict:
    if not SCHEDULE_PATH.exists():
        log.error(f"unlock_schedule.json не найден: {SCHEDULE_PATH}. Token unlock check отключён.")
        return {}
    try:
        data = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.error(f"Ошибка чтения unlock_schedule.json: {e}")
        return {}

    curated_at_str = data.get("_data_curated_at", "")
    if curated_at_str:
        try:
            curated_dt = datetime.strptime(curated_at_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - curated_dt).days
            if age_days > STALE_DAYS:
                log.warning(
                    f"unlock_schedule.json устарел на {age_days} дней "
                    f"(последнее обновление: {curated_at_str}). "
                    f"Обнови outcomes/unlock_schedule.json вручную."
                )
        except ValueError:
            pass

    return data.get("schedules", {})


# Кэш расписания в памяти — перезагружаем раз в час (не держим файл всегда открытым)
_schedule_cache: dict = {}
_schedule_loaded_at: float = 0.0
_RELOAD_INTERVAL = 3600.0


def _get_schedule() -> dict:
    global _schedule_cache, _schedule_loaded_at
    now = time.time()
    if now - _schedule_loaded_at > _RELOAD_INTERVAL:
        _schedule_cache = _load_schedule()
        _schedule_loaded_at = now
    return _schedule_cache


def _nearest_event(events: list) -> Optional[dict]:
    now_ts = time.time()
    horizon_ts = now_ts + HORIZON_DAYS * 86400
    for ev in events:
        date_str = ev.get("date", "")
        try:
            ev_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            ev_ts = ev_dt.timestamp()
        except ValueError:
            continue
        if ev_ts < now_ts or ev_ts > horizon_ts:
            continue
        pct = ev.get("pct_of_supply") or 0.0
        if pct < MIN_PCT:
            continue
        return {
            "date":          date_str,
            "days_until":    int((ev_ts - now_ts) / 86400),
            "pct_of_supply": pct,
            "usd_value":     ev.get("usd_value"),
            "type":          ev.get("type", "unlock"),
        }
    return None


def get_upcoming_unlock(symbol: str) -> Optional[dict]:
    """
    Возвращает ближайший анлок для символа в горизонте HORIZON_DAYS дней, либо None.
    Синхронный — читает локальный файл (с in-memory кэшем на час).
    Fail-open: символ не в файле или ошибка чтения → None, сигнал не блокируется.
    """
    base = symbol.upper().replace("USDT", "").replace("PERP", "").replace("1000", "")
    schedule = _get_schedule()

    events = schedule.get(base)
    if events is None:
        log.debug(f"unlock_schedule: {base} не найден в файле → None (fail-open)")
        return None

    result = _nearest_event(events)
    if result:
        log.info(f"unlock_schedule: {base} → {result['date']} ({result['days_until']}d), {result['pct_of_supply']}% supply")
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    test_symbols = ["ARB", "OP", "SUI", "STRK", "JUP", "PYTH", "WIF", "APT", "SEI", "_EXAMPLE"]
    for sym in test_symbols:
        print(f"{sym:10s}: {get_upcoming_unlock(sym)}")

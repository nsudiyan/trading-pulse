"""Fail-closed age and freshness for confirmed price-structure levels."""
from __future__ import annotations

import math


_SPECS = {
    "5m": (5, 8), "15m": (15, 16), "1H": (60, 24),
    "4H": (240, 20), "1D": (1440, 10), "1W": (10080, 8),
}
_ALIASES = {"5": "5m", "15": "15m", "60": "1H", "240": "4H",
            "D": "1D", "W": "1W"}


def timeframe_key(timeframe: str) -> str:
    key = _ALIASES.get(str(timeframe), str(timeframe))
    if key not in _SPECS:
        raise ValueError(f"unsupported level-age timeframe: {timeframe}")
    return key


def is_level_fresh(age_minutes: float, timeframe: str) -> bool:
    """Whether the source level is within the user's timeframe-specific limit."""
    key = timeframe_key(timeframe)
    age = float(age_minutes)
    if not math.isfinite(age) or age < 0:
        return False
    candle_minutes, max_candles = _SPECS[key]
    return age / candle_minutes <= max_candles


def format_level_age(age_minutes: float, timeframe: str) -> str:
    """Readable elapsed age, freshness band, and approximate source-TF bars."""
    key = timeframe_key(timeframe)
    age = float(age_minutes)
    if not math.isfinite(age) or age < 0:
        raise ValueError("level age must be a finite non-negative number")
    minutes = int(age)
    if minutes < 60:
        age_text = f"{minutes}м"
    elif minutes < 1440:
        hours, remainder = divmod(minutes, 60)
        age_text = f"{hours}ч {remainder}м" if remainder else f"{hours}ч"
    else:
        days, remainder = divmod(minutes, 1440)
        hours = remainder // 60
        age_text = f"{days}д {hours}ч" if hours else f"{days}д"

    candle_minutes, max_candles = _SPECS[key]
    age_candles = age / candle_minutes
    if age_candles <= max_candles * 0.25:
        emoji, label = "🟢", "свежий"
    elif age_candles <= max_candles * 0.60:
        emoji, label = "🟡", "нормальный"
    elif age_candles <= max_candles:
        emoji, label = "🟠", "старый"
    else:
        emoji, label = "🔴", "устарел"
    bars = int(round(age_candles))
    last_two = bars % 100
    last = bars % 10
    word = ("свеча" if last == 1 and last_two != 11 else
            "свечи" if last in (2, 3, 4) and not 12 <= last_two <= 14 else "свечей")
    return f"{age_text} {emoji} {label} ({bars} {word})"


def observe_level_age(level_known_ms, event_end_ms, timeframe: str,
                      level_price=None) -> dict:
    """Build an event-time age record; absent/future source timestamps stay unknown."""
    key = timeframe_key(timeframe)
    result = {"timeframe": key, "level_price": level_price,
              "level_known_ms": level_known_ms, "event_end_ms": event_end_ms,
              "age_minutes": None, "age_candles": None,
              "max_age_candles": _SPECS[key][1], "is_fresh": False,
              "status": "unknown"}
    if (type(level_known_ms) is not int or type(event_end_ms) is not int
            or level_known_ms < 0 or event_end_ms < level_known_ms
            or not isinstance(level_price, (int, float))
            or not math.isfinite(level_price) or level_price <= 0):
        result["reason"] = "level_timestamp_missing_or_future"
        return result
    age_ms = event_end_ms - level_known_ms
    age_minutes = age_ms / 60_000
    candle_minutes = _SPECS[key][0]
    age_candles = age_ms / (candle_minutes * 60_000)
    fresh = age_candles <= _SPECS[key][1]
    if age_candles <= _SPECS[key][1] * 0.25:
        emoji, label = "🟢", "свежий"
    elif age_candles <= _SPECS[key][1] * 0.60:
        emoji, label = "🟡", "нормальный"
    elif fresh:
        emoji, label = "🟠", "старый"
    else:
        emoji, label = "🔴", "устарел"
    result.update({"age_minutes": age_minutes, "age_candles": age_candles,
                   "is_fresh": fresh, "status": "fresh" if fresh else "stale",
                   "freshness_emoji": emoji, "freshness_label": label,
                   "age_text": format_level_age(age_minutes, key)})
    return result


def level_alert_text(observation: dict | None) -> str | None:
    if not isinstance(observation, dict) or observation.get("status") not in {"fresh", "stale"}:
        return None
    price = observation.get("level_price")
    if not isinstance(price, (int, float)) or not math.isfinite(price):
        return None
    timeframe = observation.get("timeframe", "уровень")
    age_text = observation.get("age_text") or "возраст неизвестен"
    return f"Уровень BOS {timeframe}: {price:g} · возраст {age_text}"

"""User-configured Moscow session labels and alert-timeframe policy.

These are fixed strategy windows in Moscow time, not universal exchange-session
boundaries. Crypto trades continuously; all timestamps passed here are Unix ms.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3), "MSK")
ALLOWED_ALERT_TIMEFRAMES = frozenset({"15", "60", "240", "D"})
KILL_ZONES = frozenset({"LONDON_KZ", "NY_KZ"})
NON_DEAD_ZONES = frozenset({"ASIA", "LONDON_KZ", "LONDON_CLOSE",
                             "NY_KZ", "NY_PM"})
_TF_LABELS = {"5": "5m", "15": "15m", "60": "1H", "240": "4H",
              "D": "1D", "W": "1W"}


def msk_datetime(timestamp_ms: int) -> datetime:
    return datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=MSK)


def format_msk(timestamp_ms: int, *, full: bool = False) -> str:
    pattern = "%d.%m.%Y %H:%M МСК" if full else "%d.%m %H:%M МСК"
    return msk_datetime(timestamp_ms).strftime(pattern)


def session_at(timestamp_ms: int) -> str:
    local = msk_datetime(timestamp_ms)
    minute = local.hour * 60 + local.minute
    if 4 * 60 <= minute < 11 * 60:
        return "ASIA"
    if 11 * 60 <= minute < 13 * 60:
        return "LONDON_KZ"
    if 13 * 60 <= minute < 16 * 60:
        return "LONDON_CLOSE"
    if 16 * 60 <= minute < 18 * 60:
        return "NY_KZ"
    if 18 * 60 <= minute < 20 * 60:
        return "NY_PM"
    return "DEAD_ZONE"


def session_label(session: str) -> str:
    return {"ASIA": "Азия", "LONDON_KZ": "London KZ",
            "LONDON_CLOSE": "London Close", "NY_KZ": "New York KZ",
            "NY_PM": "New York PM", "DEAD_ZONE": "вне активных окон"}.get(session, session)


def timeframe_alert_policy(timeframe: str, candle_close_ms: int) -> tuple[bool, str | None]:
    """Apply the configured Moscow time windows to the *event candle close*.

    These are fixed strategy windows for 24/7 crypto, not literal exchange
    opening hours. Daily Bybit candles close at 03:00 MSK, so a direct 1D
    event cannot satisfy a London/New York kill-zone rule; 1D remains useful
    as context for lower-timeframe alerts.
    """
    interval = str(timeframe)
    if interval not in ALLOWED_ALERT_TIMEFRAMES:
        label = _TF_LABELS.get(interval, interval)
        return False, f"suppressed_timeframe:{label}"
    session = session_at(candle_close_ms)
    allowed = (KILL_ZONES if interval in {"15", "60", "D"}
               else NON_DEAD_ZONES)
    if session not in allowed:
        label = _TF_LABELS.get(interval, interval)
        return False, f"suppressed_session:{label}:{session.lower()}"
    return True, None

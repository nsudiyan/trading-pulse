"""Closed-candle BTC volatility gate for directional altcoin reviews.

This is a notification filter, not a claim that BTC movement determines an
altcoin's direction and not an execution rule. Missing data is fail-open but
must be made visible in the alert and stored suppression reason when blocked.
"""
from __future__ import annotations

import math

# Kept local so this gate can be unit-tested and deployed as one small module.
BTC_SYMBOLS = frozenset({"BTCUSDT", "BTCUSDC", "BTCUSD", "BTCPERP"})

STEP_MS = 900_000
WINDOW_MS = 45 * 60_000
MOVE_THRESHOLD_PCT = 1.0
# Avoid treating binary floating-point noise at exactly +/-1.00% as a breach.
THRESHOLD_EPSILON_PCT = 1e-9


def btc_45m_gate(symbol: str, side: str, bars: list[dict],
                 event_close_ms: int) -> dict:
    """Compare first open to third close over exactly 45 closed minutes.

    Requires three contiguous 15m candles ending exactly at the event close.
    No live/current candle, interpolation, or later BTC candle is accepted.
    """
    if symbol in BTC_SYMBOLS:
        return {"passed": True, "reason": "btc_not_applicable", "move_pct": None}
    if side not in {"BUY", "SELL"}:
        return {"passed": True, "reason": "not_directional", "move_pct": None}

    end = int(event_close_ms)
    by_start: dict[int, dict] = {}
    for bar in bars:
        try:
            start = int(bar["start_ms"])
            close = int(bar["end_ms"]) + 1
            if close > end:
                continue
            previous = by_start.get(start)
            if previous is not None and any(
                    previous.get(key) != bar.get(key)
                    for key in ("end_ms", "open", "high", "low", "close")):
                return {"passed": True, "reason": "btc_no_data", "move_pct": None}
            by_start[start] = bar
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    starts = [end - WINDOW_MS, end - 2 * STEP_MS, end - STEP_MS]
    # The event is normally 15m-aligned. A non-aligned close has no exact
    # three-candle observation and is treated as missing, never rounded.
    if end % STEP_MS or any(start not in by_start for start in starts):
        return {"passed": True, "reason": "btc_no_data", "move_pct": None}

    selected = [by_start[start] for start in starts]
    for index, bar in enumerate(selected):
        try:
            start = int(bar["start_ms"])
            end_inclusive = int(bar["end_ms"])
            values = [float(bar[name]) for name in ("open", "high", "low", "close")]
        except (KeyError, TypeError, ValueError, OverflowError):
            return {"passed": True, "reason": "btc_no_data", "move_pct": None}
        if (start != starts[index] or end_inclusive + 1 != start + STEP_MS or
                any(not math.isfinite(value) or value <= 0 for value in values)):
            return {"passed": True, "reason": "btc_no_data", "move_pct": None}
        open_price, high, low, close_price = values
        if not low <= min(open_price, close_price) <= max(open_price, close_price) <= high:
            return {"passed": True, "reason": "btc_no_data", "move_pct": None}

    first_open = float(selected[0]["open"])
    last_close = float(selected[-1]["close"])
    move_pct = (last_close / first_open - 1.0) * 100.0
    if side == "BUY" and move_pct < -(MOVE_THRESHOLD_PCT + THRESHOLD_EPSILON_PCT):
        return {"passed": False, "reason": "suppressed_btc_gate:btc_down_fast",
                "move_pct": move_pct}
    if side == "SELL" and move_pct > MOVE_THRESHOLD_PCT + THRESHOLD_EPSILON_PCT:
        return {"passed": False, "reason": "suppressed_btc_gate:btc_up_fast",
                "move_pct": move_pct}
    return {"passed": True, "reason": "btc_gate_passed", "move_pct": move_pct}

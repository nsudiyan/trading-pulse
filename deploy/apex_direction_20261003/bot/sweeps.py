"""Mechanical closed-bar sweeps, without claims about hidden stop orders.

This module deliberately detects price geometry only. The shared
``sweep_gate`` applies one quote-notional activity filter to both review paths.
"""
from __future__ import annotations

from sessions import closed_asia_range, session_at
from structure import confirmed_pivots


def equal_level_sweep(bars: list[dict], *, tolerance_bps: float = 30) -> dict | None:
    """Last bar wicks through two equal confirmed pivots and closes back.

    Pivots must have been confirmed before this bar opened. Volume is checked
    by the shared event-time activity gate, not here, to avoid mixing base-coin
    volume with quote-notional turnover.
    """
    if len(bars) < 20:
        return None
    previous = bars[:-1]
    current = bars[-1]
    highs, lows = confirmed_pivots(previous)
    for indices, field, direction in ((lows, "low", "BUY"),
                                      (highs, "high", "SELL")):
        if len(indices) < 2:
            continue
        first = float(previous[indices[-2]][field])
        second = float(previous[indices[-1]][field])
        if first <= 0 or abs(second - first) / first > tolerance_bps / 10_000:
            continue
        level = min(first, second) if direction == "BUY" else max(first, second)
        pierced = (float(current["low"]) < level < float(current["close"])
                   if direction == "BUY" else
                   float(current["high"]) > level > float(current["close"]))
        if pierced:
            return {"code": "equal_level_sweep", "direction": direction,
                    "level": level, "end_ms": int(current["end_ms"])}
    return None


def asia_range_sweep(bars_15m: list[dict], hourly_bars: list[dict]) -> dict | None:
    """London 08–10 UTC wick outside a completed Asia range, close back in."""
    if not bars_15m:
        return None
    current = bars_15m[-1]
    close_ms = int(current["end_ms"]) + 1
    if session_at(close_ms) != "LONDON_KZ":
        return None
    asia = closed_asia_range(hourly_bars, close_ms)
    if asia is None:
        return None
    if float(current["low"]) < asia["low"] < float(current["close"]):
        direction, level = "BUY", asia["low"]
    elif float(current["high"]) > asia["high"] > float(current["close"]):
        direction, level = "SELL", asia["high"]
    else:
        return None
    return {"code": "asia_range_sweep", "direction": direction,
            "level": level, "end_ms": int(current["end_ms"])}


def recent_sweep(view, symbol: str, end_ms: int, direction: str,
                 *, lookback_bars: int = 2) -> dict | None:
    """Check the most recent 15m bars at event time, never future candles."""
    bars = [bar for bar in view.bars.get((symbol, "15"), [])
            if int(bar["end_ms"]) <= end_ms]
    hourly = [bar for bar in view.bars.get((symbol, "60"), [])
              if int(bar["end_ms"]) <= end_ms]
    if not bars or end_ms - int(bars[-1]["end_ms"]) >= 900_000:
        return None
    for offset in range(1, min(lookback_bars, len(bars)) + 1):
        candidate = bars[:-offset + 1] if offset > 1 else bars
        pattern = equal_level_sweep(candidate) or asia_range_sweep(candidate, hourly)
        if pattern and pattern["direction"] == direction:
            return pattern
    return None

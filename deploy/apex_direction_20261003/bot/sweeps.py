"""Mechanical closed-bar sweeps, without claims about hidden stop orders.

This module deliberately detects price geometry only. The shared
``sweep_gate`` applies one quote-notional activity filter to both review paths.
"""
from __future__ import annotations

import math

from sessions import closed_asia_range, session_at
from structure import confirmed_pivots


def _shape_sweep(current: dict, level: float, direction: str, code: str,
                 level_known_ms: int | None, priority: int, *,
                 min_wick_body_ratio: float = 0.5,
                 min_penetration_bps: float = 5.0) -> dict | None:
    """Mechanical wick/reclaim geometry; does not prove stop execution."""
    try:
        level = float(level)
        open_price = float(current["open"])
        high = float(current["high"])
        low = float(current["low"])
        close = float(current["close"])
        end_ms = int(current["end_ms"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if (level <= 0 or not all(math.isfinite(x)
                              for x in (open_price, high, low, close)) or high < low):
        return None
    body = abs(close - open_price)
    if body <= 0:
        return None
    if direction == "BUY":
        pierced, reclaimed = low < level, close > level
        wick = min(open_price, close) - low
        penetration_bps = (level - low) / level * 10_000
        extreme = low
    elif direction == "SELL":
        pierced, reclaimed = high > level, close < level
        wick = high - max(open_price, close)
        penetration_bps = (high - level) / level * 10_000
        extreme = high
    else:
        return None
    # Treat config values as stricter overrides only. They cannot silently
    # disable the minimum geometry promised by the release contract.
    min_wick_body_ratio = max(0.5, float(min_wick_body_ratio))
    min_penetration_bps = max(5.0, float(min_penetration_bps))
    ratio = wick / body
    if (not pierced or not reclaimed or wick <= 0 or
            ratio < min_wick_body_ratio or penetration_bps < min_penetration_bps):
        return None
    return {"code": code, "direction": direction, "level": level,
            "end_ms": end_ms, "level_known_ms": level_known_ms,
            "priority": priority, "sweep_low": low if direction == "BUY" else None,
            "sweep_high": high if direction == "SELL" else None,
            "wick_body_ratio": ratio, "penetration_bps": penetration_bps,
            "swept_extreme": extreme}


def equal_level_sweeps(bars: list[dict], *, tolerance_bps: float = 30,
                       min_wick_body_ratio: float = 0.5,
                       min_penetration_bps: float = 5.0) -> list[dict]:
    """Last bar wicks through equal confirmed pivots and closes back.

    Pivots must have been confirmed before this bar opened. Volume is checked
    by the shared event-time activity gate, not here, to avoid mixing base-coin
    volume with quote-notional turnover.
    """
    if len(bars) < 20:
        return []
    previous = bars[:-1]
    current = bars[-1]
    highs, lows = confirmed_pivots(previous)
    results = []
    for indices, field, direction in ((lows, "low", "BUY"),
                                      (highs, "high", "SELL")):
        if len(indices) < 2:
            continue
        first = float(previous[indices[-2]][field])
        second = float(previous[indices[-1]][field])
        if first <= 0 or abs(second - first) / first > tolerance_bps / 10_000:
            continue
        level = min(first, second) if direction == "BUY" else max(first, second)
        confirmed_index = indices[-1] + 2
        if confirmed_index >= len(previous):
            continue
        known_ms = int(previous[confirmed_index]["end_ms"]) + 1
        result = _shape_sweep(current, level, direction, "equal_level_sweep",
                              known_ms, 0,
                              min_wick_body_ratio=min_wick_body_ratio,
                              min_penetration_bps=min_penetration_bps)
        if result:
            results.append(result)
    return results


def equal_level_sweep(bars: list[dict], *, tolerance_bps: float = 30,
                      min_wick_body_ratio: float = 0.5,
                      min_penetration_bps: float = 5.0) -> dict | None:
    """Compatibility wrapper returning the first equal-level candidate."""
    candidates = equal_level_sweeps(
        bars, tolerance_bps=tolerance_bps,
        min_wick_body_ratio=min_wick_body_ratio,
        min_penetration_bps=min_penetration_bps)
    return candidates[0] if candidates else None


def asia_range_sweep(bars_15m: list[dict], hourly_bars: list[dict], *,
                     min_wick_body_ratio: float = 0.5,
                     min_penetration_bps: float = 5.0) -> dict | None:
    """London 08–10 UTC wick outside a completed Asia range, close back in."""
    if not bars_15m:
        return None
    current = bars_15m[-1]
    close_ms = int(current["end_ms"]) + 1
    if session_at(close_ms) != "LONDON_KZ":
        return None
    if len(hourly_bars) < 7 or any(
            int(right["start_ms"]) - int(left["start_ms"]) != 3_600_000 or
            int(left["end_ms"]) + 1 != int(right["start_ms"])
            for left, right in zip(hourly_bars[-7:], hourly_bars[-6:])):
        return None
    asia = closed_asia_range(hourly_bars, close_ms)
    if asia is None:
        return None
    known_ms = max((int(bar["end_ms"]) + 1 for bar in hourly_bars
                    if int(bar["end_ms"]) + 1 <= int(current["start_ms"])),
                   default=None)
    if float(current["low"]) < asia["low"] < float(current["close"]):
        direction, level = "BUY", asia["low"]
    elif float(current["high"]) > asia["high"] > float(current["close"]):
        direction, level = "SELL", asia["high"]
    else:
        return None
    return _shape_sweep(current, level, direction, "asia_range_sweep",
                        known_ms, 2,
                        min_wick_body_ratio=min_wick_body_ratio,
                        min_penetration_bps=min_penetration_bps)


def h4_swing_sweeps(current: dict, h4_bars: list[dict], *,
                    min_wick_body_ratio: float = 0.5,
                    min_penetration_bps: float = 5.0) -> list[dict]:
    """Test the latest already-confirmed 4H swing high and low."""
    if len(h4_bars) < 5 or any(
            int(right["start_ms"]) - int(left["start_ms"]) != 14_400_000 or
            int(left["end_ms"]) + 1 != int(right["start_ms"])
            for left, right in zip(h4_bars[-5:], h4_bars[-4:])):
        return []
    highs, lows = confirmed_pivots(h4_bars)
    results = []
    for indices, field, direction in ((lows, "low", "BUY"),
                                      (highs, "high", "SELL")):
        if not indices:
            continue
        pivot = indices[-1]
        confirmed_index = pivot + 2
        if confirmed_index >= len(h4_bars):
            continue
        known_ms = int(h4_bars[confirmed_index]["end_ms"]) + 1
        if known_ms > int(current["start_ms"]):
            continue
        result = _shape_sweep(current, float(h4_bars[pivot][field]), direction,
                              "h4_swing_sweep", known_ms, 1,
                              min_wick_body_ratio=min_wick_body_ratio,
                              min_penetration_bps=min_penetration_bps)
        if result:
            results.append(result)
    return results


def h4_equal_level_sweeps(current: dict, h4_bars: list[dict], *,
                          tolerance_bps: float = 30,
                          min_wick_body_ratio: float = 0.5,
                          min_penetration_bps: float = 5.0) -> list[dict]:
    """Use equal highs/lows from confirmed 4H pivots on the current close."""
    if len(h4_bars) < 20:
        return []
    tail = h4_bars[-5:]
    if any(int(right["start_ms"]) - int(left["start_ms"]) != 14_400_000 or
           int(left["end_ms"]) + 1 != int(right["start_ms"])
           for left, right in zip(tail, tail[1:])):
        return []
    latest_known = int(tail[-1]["end_ms"]) + 1
    current_start = int(current["start_ms"])
    if latest_known > current_start or current_start - latest_known >= 14_400_000:
        return []
    highs, lows = confirmed_pivots(h4_bars)
    results = []
    for indices, field, direction in ((lows, "low", "BUY"),
                                      (highs, "high", "SELL")):
        if len(indices) < 2:
            continue
        first = float(h4_bars[indices[-2]][field])
        second = float(h4_bars[indices[-1]][field])
        if first <= 0 or abs(second - first) / first > tolerance_bps / 10_000:
            continue
        pivot_index = indices[-1]
        known_index = pivot_index + 2
        if known_index >= len(h4_bars):
            continue
        known_ms = int(h4_bars[known_index]["end_ms"]) + 1
        # A caller should pass event-time-truncated context; keep the invariant
        # local too so this detector cannot consume an unconfirmed future level.
        if known_ms > int(current["start_ms"]):
            continue
        relevant = h4_bars[indices[-2]:known_index + 1]
        if any(int(right["start_ms"]) - int(left["start_ms"]) != 14_400_000 or
               int(left["end_ms"]) + 1 != int(right["start_ms"])
               for left, right in zip(relevant, relevant[1:])):
            continue
        level = min(first, second) if direction == "BUY" else max(first, second)
        result = _shape_sweep(current, level, direction, "h4_equal_level_sweep",
                              known_ms, 0,
                              min_wick_body_ratio=min_wick_body_ratio,
                              min_penetration_bps=min_penetration_bps)
        if result:
            results.append(result)
    return results


def sweep_candidates(bars: list[dict], *, h4_bars: list[dict] | None = None,
                     hourly_bars: list[dict] | None = None,
                     min_wick_body_ratio: float = 0.5,
                     min_penetration_bps: float = 5.0) -> list[dict]:
    """EQH/EQL first, then closed 4H swing, then a completed Asia range."""
    if not bars:
        return []
    current = bars[-1]
    candidates = equal_level_sweeps(
        bars, min_wick_body_ratio=min_wick_body_ratio,
        min_penetration_bps=min_penetration_bps)
    candidates.extend(h4_equal_level_sweeps(
        current, h4_bars or [], min_wick_body_ratio=min_wick_body_ratio,
        min_penetration_bps=min_penetration_bps))
    candidates.extend(h4_swing_sweeps(
        current, h4_bars or [], min_wick_body_ratio=min_wick_body_ratio,
        min_penetration_bps=min_penetration_bps))
    asia = asia_range_sweep(
        bars, hourly_bars or [], min_wick_body_ratio=min_wick_body_ratio,
        min_penetration_bps=min_penetration_bps)
    if asia:
        candidates.append(asia)
    def distance(item):
        edge = (float(current["low"]) if item["direction"] == "BUY"
                else float(current["high"]))
        return abs(float(item["level"]) - edge)
    return sorted(candidates, key=lambda item: (item.get("priority", 99), distance(item)))


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

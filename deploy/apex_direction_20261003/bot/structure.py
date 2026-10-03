"""Confirmed price-structure observations with no future-candle lookahead.

A swing at index i is usable only after `right` later candles have closed.
Labels describe a mechanical pivot rule, not institutional intent.
"""
from __future__ import annotations

from collections.abc import Sequence


def confirmed_pivots(bars: Sequence[dict], left: int = 2,
                     right: int = 2) -> tuple[list[int], list[int]]:
    bars = list(bars)  # market state is a deque, which does not support slices
    if left < 1 or right < 1:
        raise ValueError("pivot left/right must be positive")
    highs: list[int] = []
    lows: list[int] = []
    for i in range(left, len(bars) - right):
        window = bars[i - left:i + right + 1]
        hi, lo = float(bars[i]["high"]), float(bars[i]["low"])
        if all(hi > float(item["high"]) for j, item in enumerate(window)
               if j != left):
            highs.append(i)
        if all(lo < float(item["low"]) for j, item in enumerate(window)
               if j != left):
            lows.append(i)
    return highs, lows


def prior_pivot_trend(bars: Sequence[dict], highs: list[int], lows: list[int],
                      before_index: int, right: int) -> str | None:
    """HH+HL / LH+LL known strictly before a candidate break closes."""
    old_highs = [i for i in highs if i + right < before_index]
    old_lows = [i for i in lows if i + right < before_index]
    if len(old_highs) < 2 or len(old_lows) < 2:
        return None
    h0, h1 = (float(bars[i]["high"]) for i in old_highs[-2:])
    l0, l1 = (float(bars[i]["low"]) for i in old_lows[-2:])
    if h1 > h0 and l1 > l0:
        return "up"
    if h1 < h0 and l1 < l0:
        return "down"
    return None


def break_label(prior_trend: str | None, break_direction: str) -> str:
    """Only an opposite break of an established pivot trend is CHoCH."""
    if prior_trend is None:
        return "Пробой структуры"
    return "BOS" if prior_trend == break_direction else "CHoCH"


def detect_structure_findings(bars: Sequence[dict], config: dict | None = None) -> list[dict]:
    """Emit newly confirmed equal pivots and close-crosses of old pivots."""
    cfg = config or {}
    left = int(cfg.get("pivot_left", 2))
    right = int(cfg.get("pivot_right", 2))
    if len(bars) < left + right + 2:
        return []
    highs, lows = confirmed_pivots(bars, left, right)
    last = len(bars) - 1
    out: list[dict] = []
    tolerance = float(cfg.get("equal_tolerance_bps", 10)) / 10_000
    for pivots, field, name, code in (
        (highs, "high", "Близкие подтверждённые swing highs", "equal_highs"),
        (lows, "low", "Близкие подтверждённые swing lows", "equal_lows"),
    ):
        if len(pivots) >= 2 and pivots[-1] + right == last:
            old = float(bars[pivots[-2]][field])
            new = float(bars[pivots[-1]][field])
            if old > 0 and abs(new - old) / old <= tolerance:
                out.append({"code": code, "name": name,
                            "evidence": f"{old:g} и {new:g}; подтверждение через {right} свечи",
                            "source": "Bybit OHLC"})

    # Replay prior closes using only pivots available *at each historical bar*.
    # A pivot is spent on its first close-cross to avoid repeated break alerts.
    used_high: set[int] = set()
    used_low: set[int] = set()
    for j in range(left + right + 1, len(bars)):
        prior_close, close = float(bars[j - 1]["close"]), float(bars[j]["close"])
        available_high = [i for i in highs if i + right <= j and i not in used_high]
        available_low = [i for i in lows if i + right <= j and i not in used_low]
        triggered: tuple[str, int, float] | None = None
        if available_high:
            i = available_high[-1]
            level = float(bars[i]["high"])
            if prior_close <= level < close:
                used_high.add(i)
                triggered = ("up", i, level)
        if triggered is None and available_low:
            i = available_low[-1]
            level = float(bars[i]["low"])
            if prior_close >= level > close:
                used_low.add(i)
                triggered = ("down", i, level)
        if triggered is None:
            continue
        direction, pivot_i, level = triggered
        if j == last:
            label = break_label(prior_pivot_trend(bars, highs, lows, j, right),
                                direction)
            words = "выше swing high" if direction == "up" else "ниже swing low"
            out.append({"code": f"structure_{direction}",
                        "name": f"{label}: закрытие {words}",
                        "evidence": f"close {close:g}, уровень {level:g}; pivot подтверждён {right} свечами"
                                    + (f"; возраст после подтверждения {max(0, (bars[j]['end_ms'] - bars[pivot_i + right]['end_ms']) // 3600000)}ч "
                                       f"{max(0, (bars[j]['end_ms'] - bars[pivot_i + right]['end_ms']) // 60000) % 60}м "
                                       f"({j - pivot_i - right} свечей текущего ТФ)"
                                       if "end_ms" in bars[j] and "end_ms" in bars[pivot_i + right] else "; возраст: нет данных"),
                        "level_price": level,
                        "level_known_ms": bars[pivot_i + right].get("end_ms"),
                        "level_age_bars": j - pivot_i - right,
                        "source": "Bybit OHLC"})
            # The last opposite candle before a break is a reproducible zone
            # candidate. Nothing here proves it was an institutional order.
            opposite = next((bars[k] for k in range(j - 1, max(pivot_i, j - 8), -1)
                             if (float(bars[k]["close"]) < float(bars[k]["open"]))
                             == (direction == "up")), None)
            if opposite:
                zone_low = min(float(opposite["open"]), float(opposite["close"]))
                zone_high = max(float(opposite["open"]), float(opposite["close"]))
                out.append({"code": f"pre_break_opposite_{direction}",
                            "name": "Кандидат зоны перед пробоем",
                            "evidence": f"тело противоположной свечи {zone_low:g}–{zone_high:g}",
                            "source": "Bybit OHLC"})
    if cfg.get("fib_retracement", True) and len(bars) >= 2:
        swings = sorted([(i, "H", float(bars[i]["high"])) for i in highs]
                        + [(i, "L", float(bars[i]["low"])) for i in lows])
        if swings:
            last_swing = swings[-1]
            opposite = next((item for item in reversed(swings[:-1])
                             if item[1] != last_swing[1]), None)
            if opposite:
                upward = last_swing[1] == "H"
                low = opposite[2] if upward else last_swing[2]
                high = last_swing[2] if upward else opposite[2]
                old, current = float(bars[-2]["close"]), float(bars[-1]["close"])
                min_swing = float(cfg.get("fib_min_swing_pct", 1.0)) / 100
                if high > low > 0 and (high - low) / low >= min_swing:
                    zone_low = (high - (high - low) * 0.786 if upward
                                else low + (high - low) * 0.618)
                    zone_high = (high - (high - low) * 0.618 if upward
                                 else low + (high - low) * 0.786)
                    entered_zone = (old > zone_high and zone_low <= current <= zone_high
                                    if upward else old < zone_low and zone_low <= current <= zone_high)
                    if entered_zone:
                        out.append({"code": "fib_golden_zone_entry",
                                    "name": "Цена вошла в зону отката Fibonacci 0.618–0.786",
                                    "evidence": f"якоря {low:g}–{high:g}; зона {zone_low:g}–{zone_high:g}",
                                    "source": "Bybit OHLC"})
                    for ratio in (0.5, 0.618):
                        level = (high - (high - low) * ratio if upward
                                 else low + (high - low) * ratio)
                        crossed = (old > level >= current if upward
                                   else old < level <= current)
                        if crossed:
                            out.append({"code": f"fib_retrace_{ratio}",
                                        "name": f"Пересечение отката Fibonacci {ratio:g}",
                                        "evidence": f"якоря {low:g}–{high:g}; уровень {level:g}",
                                        "source": "Bybit OHLC"})
    return out

"""Closed-candle measurements for the proposed strict setup pipeline.

This module never turns an unavailable feed into a zero or a positive check.
In particular, Bybit OHLCV is not the Pulse volume export, and an OHLCV
profile is not an executed-trade volume profile.
"""
from __future__ import annotations

from math import isfinite, sqrt

from findings import ema_state, _rsi, _macd_histogram
from structure import confirmed_pivots
from btc_context import closed_return

TF_MS = {"15": 900_000, "60": 3_600_000, "240": 14_400_000,
         "D": 86_400_000, "W": 604_800_000}
ORDER = ("W", "D", "240", "60", "15")


def asof_series(bars: list[dict], end_ms: int) -> list[dict]:
    """Stable closed-bar snapshot; conflicting duplicates fail closed."""
    unique = {}
    for bar in bars:
        if int(bar["end_ms"]) > end_ms:
            continue
        key = int(bar["start_ms"])
        if key in unique and any(unique[key].get(f) != bar.get(f) for f in
                                  ("end_ms", "open", "high", "low", "close", "volume", "turnover")):
            return []
        unique[key] = bar
    return [unique[key] for key in sorted(unique)]


def zone_snapshot(route: str, bar: dict | None, h4: dict,
                  h4_end_ms: int | None, event_end_ms: int) -> dict:
    """Zone from a known as-of price and 4H geometry, never Volume Profile."""
    result = {"source_route": route, "zone": None, "zone_provenance": {
        "source_route": route, "event_end_ms": event_end_ms,
        "price": None, "price_end_ms": None, "geometry_end_ms": None,
        "geometry": None, "event_time_verified": False}}
    low, high, mid = (h4.get(k) for k in ("swing_low", "swing_high", "swing_mid"))
    if not bar or h4_end_ms is None:
        return result
    price = float(bar["close"])
    valid = all(isinstance(v, (int, float)) and isfinite(v) and v > 0
                for v in (price, low, high, mid))
    if not valid or not low < high or abs(mid - (low + high)/2) > max(abs(mid)*1e-9, 1e-12):
        return result
    if not (0 <= event_end_ms-int(bar["end_ms"]) < TF_MS["15"] and
            0 <= event_end_ms-h4_end_ms < TF_MS["240"]):
        return result
    zone = "discount" if price < mid else "premium" if price > mid else "equilibrium"
    geometry = {"swing_low": low, "swing_high": high, "swing_mid": mid}
    result.update(zone=zone, **geometry)
    result["zone_provenance"].update(price=price, price_end_ms=int(bar["end_ms"]),
        geometry_end_ms=h4_end_ms, geometry=geometry, event_time_verified=True)
    return result


def contiguous(bars: list[dict], interval: str, count: int) -> bool:
    """Require every *closed* bar in the requested tail, with no gaps."""
    if len(bars) < count or count < 1:
        return False
    tail = bars[-count:]
    step = TF_MS[interval]
    return all(int(right["start_ms"]) - int(left["start_ms"]) == step
               for left, right in zip(tail, tail[1:]))


def contiguous_tail(bars: list[dict], interval: str) -> list[dict]:
    """Restart indicator warmup after a missed candle instead of bridging a gap."""
    if not bars:
        return []
    first = len(bars) - 1
    while first > 0 and (int(bars[first]["start_ms"])
                         - int(bars[first - 1]["start_ms"]) == TF_MS[interval]):
        first -= 1
    return bars[first:]


def atr14(bars: list[dict], interval: str) -> float | None:
    """Wilder ATR(14), seeded with the first 14 true ranges."""
    bars = contiguous_tail(bars, interval)
    if len(bars) < 15:
        return None
    ranges = []
    for previous, current in zip(bars, bars[1:]):
        high, low = float(current["high"]), float(current["low"])
        prior_close = float(previous["close"])
        if not all(isfinite(x) for x in (high, low, prior_close)) or high < low:
            return None
        ranges.append(max(high - low, abs(high - prior_close),
                          abs(low - prior_close)))
    value = sum(ranges[:14]) / 14
    for true_range in ranges[14:]:
        value = (13 * value + true_range) / 14
    return value


def bollinger20(bars: list[dict]) -> dict | None:
    if len(bars) < 20:
        return None
    closes = [float(bar["close"]) for bar in bars[-20:]]
    if not all(isfinite(x) for x in closes):
        return None
    middle = sum(closes) / 20
    deviation = sqrt(sum((x - middle) ** 2 for x in closes) / 20)
    if middle <= 0:
        return None
    return {"middle": middle, "upper": middle + 2 * deviation,
            "lower": middle - 2 * deviation,
            "width_pct": 400 * deviation / middle}


def prior_volume_ratio(bars: list[dict], period: int = 14,
                       interval: str = "15") -> float | None:
    """Current base volume / previous N bars; current bar is excluded from MA."""
    if not contiguous(bars, interval, period + 1):
        return None
    base = [float(bar["volume"]) for bar in bars[-period - 1:-1]]
    current = float(bars[-1]["volume"])
    if not all(isfinite(x) and x >= 0 for x in [*base, current]):
        return None
    mean = sum(base) / period
    return current / mean if mean > 0 else None


def quote_volume_24h(bars_15m: list[dict], *, category: str = "linear",
                     symbol: str = "") -> float | None:
    """96-bar quote notional in native USD, USDT or USDC units.

    Bybit inverse kline *volume* is quote USD, while linear kline
    *turnover* is quote USDT/USDC. These are native units; there is no FX
    conversion or claim that stablecoins always equal one USD.
    """
    if not contiguous(bars_15m, "15", 96):
        return None
    if category == "inverse" and symbol.endswith("USD"):
        field = "volume"
    elif category == "linear" and (not symbol or symbol.endswith(("USDT", "USDC"))):
        field = "turnover"
    else:
        return None
    values = [bar.get(field) for bar in bars_15m[-96:]]
    if any(value is None or not isfinite(float(value)) or float(value) < 0
           for value in values):
        return None
    return sum(float(value) for value in values)


def confirmed_structure(bars: list[dict]) -> str | None:
    """HH+HL or LH+LL from the two latest confirmed pivots of each kind."""
    highs, lows = confirmed_pivots(bars)
    if len(highs) < 2 or len(lows) < 2:
        return None
    high_old, high_new = (float(bars[i]["high"]) for i in highs[-2:])
    low_old, low_new = (float(bars[i]["low"]) for i in lows[-2:])
    if high_new > high_old and low_new > low_old:
        return "рост"
    if high_new < high_old and low_new < low_old:
        return "снижение"
    return "смешанная"


def timeframe_metrics(bars: list[dict], interval: str) -> dict:
    """Computed facts. None means insufficient or invalid closed-bar history."""
    bars = contiguous_tail(bars, interval)
    if not bars:
        return {"trend": None, "atr": None, "atr_pct": None, "rsi": None,
                "macd_hist": None, "bollinger": None, "volume_ratio": None,
                "structure": None, "price_zone": None,
                "swing_high": None, "swing_low": None, "swing_mid": None}
    state = ema_state(bars)
    close = float(bars[-1]["close"])
    atr = atr14(bars, interval)
    highs, lows = confirmed_pivots(bars) if len(bars) >= 6 else ([], [])
    zone = None
    high = low = middle = None
    if highs and lows:
        high, low = float(bars[highs[-1]]["high"]), float(bars[lows[-1]]["low"])
        if high > low > 0:
            middle = (high + low) / 2
            zone = "discount" if close < middle else "premium" if close > middle else "equilibrium"
        else:
            high = low = None
    macd = _macd_histogram([float(bar["close"]) for bar in bars])
    return {"trend": state["label"] if state else None,
            "atr": atr, "atr_pct": (100 * atr / close if atr is not None and close > 0 else None),
            "rsi": _rsi([float(bar["close"]) for bar in bars]),
            "macd_hist": macd[-1] if macd else None,
            "bollinger": bollinger20(bars),
            "volume_ratio": prior_volume_ratio(bars, interval=interval),
            "structure": confirmed_structure(bars) if len(bars) >= 6 else None,
            "price_zone": zone, "swing_high": high, "swing_low": low,
            "swing_mid": middle}


def alignment(frames: dict[str, dict]) -> dict:
    """One conservative layer-2 gate; no score without every required fact."""
    trends = {tf: frames.get(tf, {}).get("trend") for tf in ORDER}
    direction = trends["D"]
    if direction not in {"рост", "снижение"}:
        return {"aligned": False, "reason": "нет определённого тренда 1D", "count": 0}
    count = sum(trends[tf] == direction for tf in ORDER)
    if trends["W"] != direction:
        return {"aligned": False, "reason": "1W и 1D не совпадают", "count": count}
    if count < 3 or trends["240"] != direction:
        return {"aligned": False, "reason": "меньше 3/5 или 4H против тренда", "count": count}
    if frames.get("15", {}).get("structure") != direction:
        return {"aligned": False, "reason": "15m структура не подтверждает", "count": count}
    zone = frames.get("240", {}).get("price_zone")
    if zone != ("discount" if direction == "рост" else "premium"):
        return {"aligned": False, "reason": "нет подходящей 4H зоны", "count": count}
    return {"aligned": True, "reason": "подтверждено", "count": count,
            "direction": direction}


def review_gate(view, symbol: str, end_ms: int, *, require_zone: bool = True) -> dict:
    """Final automatic alert gate using only candles closed by the event time.

    The 4h and 15m EMA directions must agree, 1D/1W may be ranging but
    cannot oppose, and at least three available frames must agree. Missing
    critical frames fail closed. The 4h swing midpoint is a separate price
    zone filter; an unknown zone never silently becomes a valid entry.
    """
    frames = {}
    snapshots = {}
    for tf in ORDER:
        series = asof_series(view.bars.get((symbol, tf), []), end_ms)
        snapshots[tf] = series
        latest = series[-1] if series else None
        fresh = latest is not None and 0 <= end_ms - int(latest["end_ms"]) < TF_MS[tf]
        frames[tf] = timeframe_metrics(series, tf) if fresh else timeframe_metrics([], tf)
    trends = {tf: frames[tf]["trend"] for tf in ORDER}
    direction = trends["15"]
    result = {"send": False, "direction": direction, "count": 0,
              "trends": trends, "zone": None}
    last_15m = snapshots["15"]
    last_4h = snapshots["240"]
    result.update(zone_snapshot("review_gate", last_15m[-1] if last_15m else None,
                  frames["240"], int(last_4h[-1]["end_ms"]) if last_4h else None, end_ms))
    if direction not in {"рост", "снижение"}:
        return {**result, "reason": "15m_direction_unknown"}
    if trends["240"] != direction:
        return {**result, "reason": "4h_15m_mismatch"}
    for tf in ("D", "W"):
        if trends[tf] in {"рост", "снижение"} and trends[tf] != direction:
            return {**result, "reason": f"{tf}_opposes"}
    count = sum(trend == direction for trend in trends.values())
    result["count"] = count
    if count < 3:
        return {**result, "reason": "fewer_than_3_aligned"}
    if require_zone:
        expected = "discount" if direction == "рост" else "premium"
        if result["zone"] != expected:
            return {**result, "reason": "4h_zone_missing_or_wrong"}
    return {**result, "send": True, "reason": "aligned"}


def specification_progress(symbol: str, category: str, view, btc_view,
                           end_ms: int, micro: dict) -> list[str]:
    """Short audit printed in a review alert; never assigns an A/A+ grade."""
    frames = {}
    for tf in ORDER:
        series = view.bars.get((symbol, tf), [])
        # At a 15m event the most recent 4h/day/week candle can legitimately
        # be older; anything beyond one full interval is stale.
        fresh = (bool(series) and 0 <= end_ms - int(series[-1]["end_ms"])
                 < TF_MS[tf])
        if tf == "15":
            fresh = fresh and int(series[-1]["end_ms"]) == end_ms
        frames[tf] = timeframe_metrics(series, tf) if fresh else timeframe_metrics([], tf)
    mtf = alignment(frames)
    bars_15m = view.bars.get((symbol, "15"), [])
    btc_15m = btc_view.bars.get(("BTCUSDT", "15"), [])
    quote_24h = (quote_volume_24h(bars_15m, category=category, symbol=symbol)
                 if bars_15m and int(bars_15m[-1]["end_ms"]) == end_ms else None)
    atr_pct = frames["240"]["atr_pct"]
    alt_ret = closed_return(bars_15m, end_ms, 14_400_000)
    btc_ret = closed_return(btc_15m, end_ms, 14_400_000)
    rs = alt_ret - btc_ret if alt_ret is not None and btc_ret is not None else None
    liquid = quote_24h is not None and quote_24h >= 50_000_000
    volatile = atr_pct is not None and 0.3 <= atr_pct <= 7.0
    strong_rs = symbol.startswith("BTC") or (rs is not None and rs >= 0.5)
    unit = "USD" if category == "inverse" else "USDC" if symbol.endswith("USDC") else "USDT"
    data = (f"оборот 24ч {quote_24h / 1e6:.1f} млн {unit}" if quote_24h is not None
            else "оборот 24ч неизвестен")
    data += (f", ATR14 4ч {atr_pct:.2f}%" if atr_pct is not None
             else ", ATR14 4ч неизвестен")
    data += (f", RS4ч {rs:+.2f} п.п." if rs is not None else ", RS4ч неизвестна")
    data += "; спред не измеряется"
    screen = "частично проходит" if liquid and volatile and strong_rs else "не прошёл или данных мало"
    flow = "есть непрерывная лента" if micro.get("flow") else "нет полной ленты по свече"
    return ["Полная спецификация: A/A+ пока не присваивается.",
            f"• Отбор: {screen}; {data} [Bybit].",
            f"• 5 ТФ: {mtf['count']}/5; {mtf['reason']}. Order Flow: {flow}; "
            "CVD-дивергенция, ликвидации и устойчивость заявок не проверены."]

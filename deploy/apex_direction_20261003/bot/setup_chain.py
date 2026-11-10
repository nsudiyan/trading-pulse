"""Persistent, closed-candle sweep → CHoCH setup state machine.

All price/volume thresholds here are configurable research heuristics. A
detected wick does not prove stop execution, and a confirmed sequence is not
an executable entry or evidence of trading edge.
"""
from __future__ import annotations

import json
import math

from structure import confirmed_pivots
from sweeps import sweep_candidates


TF_MS = {"15": 900_000, "60": 3_600_000, "240": 14_400_000}
DEFAULT_MAX_WAIT_BARS = 8
DEFAULT_MIN_WICK_BODY_RATIO = 0.5
DEFAULT_MIN_PENETRATION_BPS = 5.0
DEFAULT_MIN_QUOTE_24H = 50_000_000.0


def ensure_schema(db) -> None:
    db.executescript("""
      CREATE TABLE IF NOT EXISTS active_sweep_setups (
        symbol TEXT NOT NULL, interval TEXT NOT NULL,
        state_json TEXT NOT NULL, updated_ms INTEGER NOT NULL,
        PRIMARY KEY(symbol, interval)
      );
      CREATE TABLE IF NOT EXISTS sweep_setup_events (
        event_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, interval TEXT NOT NULL,
        event_type TEXT NOT NULL, event_ms INTEGER NOT NULL,
        event_json TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_sweep_setup_events_symbol_time
        ON sweep_setup_events(symbol, interval, event_ms);
    """)


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":"))


def _event(db, symbol: str, interval: str, event_type: str,
           event_ms: int, payload: dict) -> None:
    event_id = f"{symbol}:{interval}:{event_ms}:{event_type}"
    db.execute("""INSERT OR IGNORE INTO sweep_setup_events
        (event_id,symbol,interval,event_type,event_ms,event_json)
        VALUES (?,?,?,?,?,?)""",
        (event_id, symbol, interval, event_type, event_ms, _json(payload)))


def _get_state(db, symbol: str, interval: str) -> dict | None:
    row = db.execute("""SELECT state_json FROM active_sweep_setups
                        WHERE symbol=? AND interval=?""",
                     (symbol, interval)).fetchone()
    if not row:
        return None
    try:
        state = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _save_state(db, symbol: str, interval: str, state: dict,
                updated_ms: int) -> None:
    db.execute("""INSERT INTO active_sweep_setups VALUES (?,?,?,?)
        ON CONFLICT(symbol,interval) DO UPDATE SET
          state_json=excluded.state_json, updated_ms=excluded.updated_ms""",
        (symbol, interval, _json(state), updated_ms))


def _clear_state(db, symbol: str, interval: str) -> None:
    db.execute("DELETE FROM active_sweep_setups WHERE symbol=? AND interval=?",
               (symbol, interval))


def _contiguous(bars: list[dict], interval: str, count: int) -> bool:
    if interval not in TF_MS or len(bars) < count:
        return False
    tail = bars[-count:]
    step = TF_MS[interval]
    return all(int(right["start_ms"]) - int(left["start_ms"]) == step and
               int(left["end_ms"]) + 1 == int(right["start_ms"]) and
               int(left["end_ms"]) + 1 == int(left["start_ms"]) + step
               for left, right in zip(tail, tail[1:])) and (
                   int(tail[-1]["end_ms"]) + 1 == int(tail[-1]["start_ms"]) + step)


def _notional_field(category: str) -> str | None:
    return {"linear": "turnover", "inverse": "volume"}.get(category)


def _prior_ratio(bars: list[dict], interval: str,
                 category: str, period: int = 14) -> float | None:
    field = _notional_field(category)
    if not field or interval not in TF_MS or not _contiguous(bars, interval, period + 1):
        return None
    try:
        values = [float(bar[field]) for bar in bars[-period - 1:]]
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(value) and value >= 0 for value in values):
        return None
    baseline = sum(values[:-1]) / period
    ratio = values[-1] / baseline if baseline > 0 else math.nan
    return ratio if math.isfinite(ratio) else None


def _quote_24h(bars_15m: list[dict], category: str,
               before_ms: int) -> float | None:
    field = _notional_field(category)
    if not field:
        return None
    eligible = [bar for bar in bars_15m if int(bar["end_ms"]) + 1 <= before_ms]
    if not _contiguous(eligible, "15", 96):
        return None
    try:
        values = [float(bar[field]) for bar in eligible[-96:]]
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(value) and value >= 0 for value in values):
        return None
    result = sum(values)
    return result if math.isfinite(result) else None


def activity_threshold(quote_24h: float) -> float:
    """User-supplied turnover tiers; provisional until empirically calibrated."""
    if quote_24h >= 1_000_000_000:
        return 2.5
    if quote_24h >= 500_000_000:
        return 2.0
    if quote_24h >= 100_000_000:
        return 1.8
    # Preserve the explicit minimum in the supplied sweep spec for every
    # instrument; liquidity tiers may only make the filter stricter.
    return 1.8


def _atr14(bars: list[dict], interval: str) -> float | None:
    if not _contiguous(bars, interval, 15):
        return None
    tail = bars[-15:]
    ranges = []
    try:
        for previous, current in zip(tail, tail[1:]):
            high, low = float(current["high"]), float(current["low"])
            prior_close = float(previous["close"])
            if not all(math.isfinite(x) for x in (high, low, prior_close)) or high < low:
                return None
            ranges.append(max(high-low, abs(high-prior_close), abs(low-prior_close)))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    result = sum(ranges[:14]) / 14
    for value in ranges[14:]:
        result = (13 * result + value) / 14
    return result if math.isfinite(result) and result > 0 else None


def _pre_sweep_choch_pivot(pre_bars: list[dict], direction: str) -> dict | None:
    """Latest already-confirmed opposite pivot, known strictly before sweep."""
    highs, lows = confirmed_pivots(pre_bars)
    indices = highs if direction == "BUY" else lows
    if not indices:
        return None
    pivot_index = indices[-1]
    known_index = pivot_index + 2
    if known_index >= len(pre_bars):
        return None
    pivot = pre_bars[pivot_index]
    known = pre_bars[known_index]
    field = "high" if direction == "BUY" else "low"
    try:
        level = float(pivot[field])
        known_ms = int(known["end_ms"]) + 1
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(level) or level <= 0:
        return None
    return {"level": level, "source_pivot_ms": int(pivot["start_ms"]),
            "known_ms": known_ms, "kind": "swing_high" if direction == "BUY" else "swing_low"}


def _record_rejection(db, symbol: str, interval: str, reason: str,
                      close_ms: int, detail: dict) -> dict:
    _event(db, symbol, interval, reason, close_ms, detail)
    return {"status": reason, "event_ms": close_ms, "detail": detail}


def process_closed_bar(db, symbol: str, interval: str, bars: list[dict], *,
                       h4_bars: list[dict], hourly_bars: list[dict],
                       bars_15m: list[dict], category: str,
                       min_quote_24h: float = DEFAULT_MIN_QUOTE_24H,
                       max_wait_bars: int = DEFAULT_MAX_WAIT_BARS,
                       min_wick_body_ratio: float = DEFAULT_MIN_WICK_BODY_RATIO,
                       min_penetration_bps: float = DEFAULT_MIN_PENETRATION_BPS
                       ) -> dict:
    """Advance one interval's persistent state using exactly its latest close."""
    if interval not in TF_MS or not bars:
        return {"status": "unsupported_or_empty"}
    current = bars[-1]
    close_ms = int(current["end_ms"]) + 1
    step = TF_MS[interval]
    if (int(current["end_ms"]) + 1 != int(current["start_ms"]) + step or
            len(bars) >= 2 and int(bars[-2]["end_ms"]) >= int(current["end_ms"])):
        return _record_rejection(db, symbol, interval, "invalid_closed_bar",
                                 close_ms, {"start_ms": current.get("start_ms")})
    try:
        max_wait_bars = max(1, min(DEFAULT_MAX_WAIT_BARS, int(max_wait_bars)))
        min_wick_body_ratio = max(DEFAULT_MIN_WICK_BODY_RATIO,
                                  float(min_wick_body_ratio))
        min_penetration_bps = max(DEFAULT_MIN_PENETRATION_BPS,
                                  float(min_penetration_bps))
        min_quote_24h = max(DEFAULT_MIN_QUOTE_24H, float(min_quote_24h))
        thresholds = (min_wick_body_ratio, min_penetration_bps, min_quote_24h)
        if not all(math.isfinite(value) and value > 0 for value in thresholds):
            raise ValueError("nonpositive or nonfinite threshold")
    except (TypeError, ValueError, OverflowError):
        return _record_rejection(db, symbol, interval, "invalid_setup_configuration",
                                 close_ms, {})

    state = _get_state(db, symbol, interval)
    if state:
        start_ms = int(current["start_ms"])
        if start_ms <= int(state["last_start_ms"]):
            return {"status": "duplicate_or_out_of_order"}
        if start_ms != int(state["last_start_ms"]) + step:
            _event(db, symbol, interval, "data_gap_reset", close_ms,
                   {"from_start_ms": state["last_start_ms"], "to_start_ms": start_ms})
            _clear_state(db, symbol, interval)
            return {"status": "data_gap_reset", "event_ms": close_ms}

        waited = int(state["bars_waited"]) + 1
        direction = state["direction"]
        close = float(current["close"])
        if ((direction == "BUY" and close < float(state["sweep"]["swept_extreme"])) or
                (direction == "SELL" and close > float(state["sweep"]["swept_extreme"]))):
            _event(db, symbol, interval, "setup_invalidated", close_ms,
                   {"direction": direction, "sweep": state["sweep"], "close": close})
            _clear_state(db, symbol, interval)
            return {"status": "invalidated", "event_ms": close_ms}
        if waited > max_wait_bars:
            _event(db, symbol, interval, "choch_timeout", close_ms,
                   {"direction": direction, "sweep": state["sweep"],
                    "bars_waited": int(state["bars_waited"])})
            _clear_state(db, symbol, interval)
            return {"status": "timeout", "event_ms": close_ms}

        atr = _atr14(bars, interval)
        pivot = state["choch_pivot"]
        if atr is not None and pivot:
            level = float(pivot["level"])
            buffer = max(level * 0.001, atr * 0.1)
            threshold = level + buffer if direction == "BUY" else level - buffer
            prior_close = float(state["last_close"])
            crossed = (prior_close <= threshold < close if direction == "BUY"
                       else prior_close >= threshold > close)
            if crossed:
                setup = {"status": "confirmed", "direction": direction,
                         "timeframe": interval, "sweep": state["sweep"],
                         "choch": {"level": level, "level_known_ms": pivot["known_ms"],
                                   "level_kind": pivot["kind"], "buffer": buffer,
                                   "atr14": atr, "threshold": threshold,
                                   "close_price": close, "event_ms": close_ms,
                                   "source_pivot_ms": pivot["source_pivot_ms"]},
                         "bars_waited": waited,
                         "heuristics": {"max_wait_bars": max_wait_bars,
                                        "min_wick_body_ratio": min_wick_body_ratio,
                                        "min_penetration_bps": min_penetration_bps,
                                        "activity_ratio_min": state["sweep"]["activity_ratio_min"]}}
                _event(db, symbol, interval, "choch_confirmed", close_ms, setup)
                _clear_state(db, symbol, interval)
                return setup

        state.update(last_start_ms=int(current["start_ms"]), last_close=close,
                     bars_waited=waited)
        _save_state(db, symbol, interval, state, close_ms)
        return {"status": "pending", "direction": direction,
                "bars_waited": waited, "max_wait_bars": max_wait_bars}

    # Avoid running pivot/volume calculations over discontinuous inputs.
    if not _contiguous(bars, interval, 20):
        return {"status": "insufficient_or_gapped_history"}
    event_start = int(current["start_ms"])
    h4_asof = [bar for bar in h4_bars if int(bar["end_ms"]) + 1 <= event_start]
    hourly_asof = [bar for bar in hourly_bars if int(bar["end_ms"]) + 1 <= event_start]
    candidates = sweep_candidates(
        bars, h4_bars=h4_asof, hourly_bars=hourly_asof,
        min_wick_body_ratio=min_wick_body_ratio,
        min_penetration_bps=min_penetration_bps)
    if not candidates:
        return {"status": "no_sweep"}
    directions = {candidate.get("direction") for candidate in candidates}
    if len(directions) > 1:
        return _record_rejection(db, symbol, interval,
                                 "sweep_conflicting_directions", close_ms,
                                 {"candidates": candidates})

    sweep = candidates[0]
    quote_24h = _quote_24h(bars_15m, category, event_start)
    if quote_24h is None:
        return _record_rejection(db, symbol, interval,
                                 "sweep_quote_24h_unavailable", close_ms,
                                 {"sweep": sweep})
    if quote_24h < float(min_quote_24h):
        return _record_rejection(db, symbol, interval,
                                 "sweep_liquidity_below_threshold", close_ms,
                                 {"sweep": sweep, "quote_24h": quote_24h,
                                  "minimum_quote_24h": min_quote_24h})
    ratio = _prior_ratio(bars, interval, category)
    threshold_ratio = activity_threshold(quote_24h)
    if ratio is None or ratio < threshold_ratio:
        return _record_rejection(db, symbol, interval,
                                 "sweep_activity_unconfirmed", close_ms,
                                 {"sweep": sweep, "quote_24h": quote_24h,
                                  "volume_ratio": ratio,
                                  "activity_ratio_min": threshold_ratio,
                                  "volume_basis": "contract_volume" if category == "inverse" else "quote_turnover"})
    direction = sweep["direction"]
    pivot = _pre_sweep_choch_pivot(bars[:-1], direction)
    if pivot is None or int(pivot["known_ms"]) > event_start:
        return _record_rejection(db, symbol, interval,
                                 "sweep_no_pre_sweep_choch_pivot", close_ms,
                                 {"sweep": sweep})

    sweep = {**sweep, "start_ms": int(current["start_ms"]),
             "volume_ratio": ratio, "activity_ratio_min": threshold_ratio,
             "volume_basis": "contract_volume" if category == "inverse" else "quote_turnover",
             "quote_24h": quote_24h, "quote_24h_basis": "prior_96_closed_15m"}
    state = {"direction": direction, "timeframe": interval,
             "sweep": sweep, "choch_pivot": pivot,
             "last_start_ms": int(current["start_ms"]),
             "last_close": float(current["close"]), "bars_waited": 0,
             "opened_ms": close_ms}
    _save_state(db, symbol, interval, state, close_ms)
    _event(db, symbol, interval, "sweep_detected", close_ms, state)
    return {"status": "sweep_detected", "direction": direction,
            "sweep": sweep, "choch_pivot": pivot,
            "max_wait_bars": max_wait_bars}

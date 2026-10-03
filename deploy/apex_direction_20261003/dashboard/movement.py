"""Raw price path from first full 15m bar after acknowledgement; never PnL."""
import math

STEP_MS = 900_000
WINDOW_MS = 72 * 3_600_000


def measure(bars, sent_ms, now_ms, side=None):
    start = ((sent_ms + STEP_MS - 1) // STEP_MS) * STEP_MS
    stop = min(start + WINDOW_MS, (now_ms // STEP_MS) * STEP_MS)
    result = {"status": "waiting", "anchor_start_ms": start, "anchor_price": None,
              "last_closed_ms": None, "return_pct": None, "max_up_pct": None,
              "max_down_pct": None, "mfe_pct": None, "mae_pct": None,
              "source": "Bybit closed OHLC", "timeframe": "15m", "curve": []}
    if stop <= start:
        return result
    indexed = {}
    for b in bars:
        if not start <= b["start_ms"] < stop:
            continue
        old = indexed.get(b["start_ms"])
        if old is not None and old != b:
            return {**result, "status": "conflicting_duplicates"}
        indexed[b["start_ms"]] = b
    expected = list(range(start, stop, STEP_MS))
    if sorted(indexed) != expected:
        return {**result, "status": "data_unavailable"}
    ordered = [indexed[t] for t in expected]
    for b in ordered:
        values = [b.get(k) for k in ("open", "high", "low", "close")]
        if (any(not isinstance(v, (float, int)) or not math.isfinite(v) or v <= 0 for v in values)
                or b.get("end_ms") != b["start_ms"] + STEP_MS - 1
                or not b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"]):
            return {**result, "status": "invalid_ohlc"}
    anchor = ordered[0]["open"]
    change = lambda p: 100 * (p / anchor - 1)
    up = max(0.0, change(max(b["high"] for b in ordered)))
    down = min(0.0, change(min(b["low"] for b in ordered)))
    return {**result, "status": "complete" if stop == start + WINDOW_MS else "tracking",
            "anchor_price": anchor, "last_closed_ms": ordered[-1]["end_ms"],
            "return_pct": change(ordered[-1]["close"]), "max_up_pct": up, "max_down_pct": down,
            "mfe_pct": up if side == "BUY" else -down if side == "SELL" else None,
            "mae_pct": down if side == "BUY" else -up if side == "SELL" else None,
            "curve": [{"at_ms": b["end_ms"] + 1, "return_pct": change(b["close"])} for b in ordered]}

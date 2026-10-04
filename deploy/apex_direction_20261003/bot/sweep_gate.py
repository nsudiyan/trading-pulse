"""Validate quote-notional activity around a mechanically detected sweep."""
from __future__ import annotations

import math

STEP_MS = 900_000


def _prior_activity_ratio(bars: list[dict], category: str,
                          period: int = 14) -> float | None:
    """Current notional activity / prior-N mean for closed contiguous bars.

    Bybit linear contracts report ``volume`` in base coin and ``turnover`` in
    quote coin (USDT for the configured USDT perps). Inverse contracts report
    contract volume in quote coin, so their ``volume`` is the notional field.
    """
    if category not in {"linear", "inverse"} or period < 1 or len(bars) < period + 1:
        return None
    tail = bars[-period - 1:]
    field = "volume" if category == "inverse" else "turnover"
    try:
        for previous, current in zip(tail, tail[1:]):
            previous_start = int(previous["start_ms"])
            current_start = int(current["start_ms"])
            if (previous_start % STEP_MS != 0 or
                    current_start != previous_start + STEP_MS or
                    int(previous["end_ms"]) + 1 != int(current["start_ms"]) or
                    int(current["end_ms"]) + 1 != int(current["start_ms"]) + STEP_MS):
                return None
        values = [float(bar[field]) for bar in tail]
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(value) and value >= 0 for value in values):
        return None
    baseline = sum(values[:-1]) / period
    if not math.isfinite(baseline) or baseline <= 0:
        return None
    ratio = values[-1] / baseline
    return ratio if math.isfinite(ratio) else None


def sweep_activity_ratio(bars: list[dict], category: str = "linear",
                         period: int = 14) -> float | None:
    """Return same-symbol notional ratio for the final closed candle."""
    return _prior_activity_ratio(bars, category, period)


def confirm_sweep_activity(sweep: dict | None, bars: list[dict],
                           category: str = "linear",
                           minimum_ratio: float = 2.5) -> tuple[dict | None, str | None]:
    """Return a sweep enriched with quote-turnover ratio or a suppression code.

    The baseline is the 14 immediately preceding contiguous closed 15m bars;
    the sweep bar is excluded. Both the generic and strong-sweep routes call
    this same notional measure so the displayed and gated ratios agree.
    """
    if not sweep:
        return None, "sweep_not_found"
    try:
        event_end = int(sweep["end_ms"])
        threshold = float(minimum_ratio)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, "sweep_activity_unavailable"
    if not math.isfinite(threshold) or threshold <= 0:
        return None, "sweep_activity_unavailable"
    if event_end % STEP_MS != STEP_MS - 1:
        return None, "sweep_activity_unavailable"
    try:
        asof = sorted((bar for bar in bars if int(bar.get("end_ms", -1)) <= event_end),
                      key=lambda bar: int(bar["start_ms"]))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, "sweep_activity_unavailable"
    if not asof or int(asof[-1].get("end_ms", -1)) != event_end:
        return None, "sweep_activity_unavailable"
    ratio = sweep_activity_ratio(asof, category)
    if ratio is None:
        return None, "sweep_activity_unavailable"
    if ratio < threshold:
        return None, "sweep_activity_unconfirmed"
    basis = "contract_volume" if category == "inverse" else "quote_turnover"
    return {**sweep, "volume_ratio": ratio, "volume_basis": basis}, None

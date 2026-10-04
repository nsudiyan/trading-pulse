"""Validate the volume context of a mechanically detected sweep candidate."""
from __future__ import annotations

import math

STEP_MS = 900_000


def _prior_volume_ratio(bars: list[dict], period: int = 14) -> float | None:
    """Small self-contained equivalent for the 15m baseline used by setup_layers."""
    if period < 1 or len(bars) < period + 1:
        return None
    tail = bars[-period - 1:]
    try:
        for previous, current in zip(tail, tail[1:]):
            previous_start = int(previous["start_ms"])
            current_start = int(current["start_ms"])
            if (previous_start % STEP_MS != 0 or
                    current_start != previous_start + STEP_MS or
                    int(previous["end_ms"]) + 1 != int(current["start_ms"]) or
                    int(current["end_ms"]) + 1 != int(current["start_ms"]) + STEP_MS):
                return None
        values = [float(bar["volume"]) for bar in tail]
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(value) and value >= 0 for value in values):
        return None
    baseline = sum(values[:-1]) / period
    if not math.isfinite(baseline) or baseline <= 0:
        return None
    ratio = values[-1] / baseline
    return ratio if math.isfinite(ratio) else None


def confirm_sweep_volume(sweep: dict | None, bars: list[dict],
                         minimum_ratio: float = 2.5) -> tuple[dict | None, str | None]:
    """Return a sweep enriched with prior-volume ratio or a suppression code.

    The baseline is the 14 immediately preceding contiguous closed 15m bars;
    the sweep bar is excluded from the average. This closes the prior gap where
    equal-level sweeps had a volume check but Asia-range sweeps in the generic
    route did not.
    """
    if not sweep:
        return None, "sweep_not_found"
    try:
        event_end = int(sweep["end_ms"])
        threshold = float(minimum_ratio)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, "sweep_volume_unavailable"
    if not math.isfinite(threshold) or threshold <= 0:
        return None, "sweep_volume_unavailable"
    if event_end % STEP_MS != STEP_MS - 1:
        return None, "sweep_volume_unavailable"
    try:
        asof = sorted((bar for bar in bars if int(bar.get("end_ms", -1)) <= event_end),
                      key=lambda bar: int(bar["start_ms"]))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, "sweep_volume_unavailable"
    if not asof or int(asof[-1].get("end_ms", -1)) != event_end:
        return None, "sweep_volume_unavailable"
    ratio = _prior_volume_ratio(asof)
    if ratio is None:
        return None, "sweep_volume_unavailable"
    if ratio < threshold:
        return None, "sweep_volume_unconfirmed"
    return {**sweep, "volume_ratio": ratio}, None

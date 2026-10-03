"""Fail-closed delivery/read contract; no trading rule or indicator changes."""
import math


def verified_zone(scenario):
    if not isinstance(scenario, dict):
        return False
    z = scenario.get('zone_observation')
    if not isinstance(z, dict) or z.get('event_time_verified') is not True:
        return False
    if z.get('status') != 'reported_with_geometry' or z.get('source_route') not in ('review_gate', 'strong_sweep_review'):
        return False
    event, price_end, geometry_end = scenario.get('event_end_ms'), z.get('asof_end_ms'), z.get('geometry_end_ms')
    if not all(type(v) is int for v in (event, price_end, geometry_end)):
        return False
    if z.get('recorded_for_event_end_ms') != event or not (0 <= event-price_end < 900000 and 0 <= event-geometry_end < 14400000):
        return False
    g = z.get('geometry')
    if not isinstance(g, dict):
        return False
    low, high, mid, price = g.get('swing_low'), g.get('swing_high'), g.get('swing_mid'), z.get('observation_price')
    if not all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in (low, high, mid, price)):
        return False
    if not low < high or not math.isclose(mid, (low+high)/2):
        return False
    expected = 'discount' if price < mid else 'premium' if price > mid else 'equilibrium'
    return z.get('zone') == expected


def guarded_quality(quality, scenario):
    if quality.get('send') and not verified_zone(scenario):
        return {**quality, 'send': False, 'reason': 'provenance_unverified'}
    return quality

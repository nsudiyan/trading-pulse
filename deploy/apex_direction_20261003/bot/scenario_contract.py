"""Presentation-only directional scenario. Never an execution signal or new gate."""
import json
import math

VERSION = "direction-context-v4-provenance-guard"
TF_MS = {"240": 14_400_000, "D": 86_400_000}
TREND_SIDE = {"рост": "BUY", "снижение": "SELL"}


def zone_observation(gate, event_end_ms, price):
    """Capture the existing gate's pivot zone; never translate a VP zone into it."""
    raw = (gate or {}).get("zone")
    zone = raw if raw in ("premium", "discount", "equilibrium") else None
    geometry = {key: (gate or {}).get(key) for key in ("swing_low", "swing_high", "swing_mid")}
    valid = all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in geometry.values())
    valid = valid and geometry["swing_low"] < geometry["swing_high"] and math.isclose(
        geometry["swing_mid"], (geometry["swing_low"] + geometry["swing_high"]) / 2)
    route = (gate or {}).get("source_route")
    provenance = (gate or {}).get("zone_provenance") or {}
    price_end, geometry_end = provenance.get("price_end_ms"), provenance.get("geometry_end_ms")
    source_price = provenance.get("price")
    verified = bool(zone and valid and provenance.get("event_time_verified") and
        route in ("review_gate", "strong_sweep_review") and
        provenance.get("source_route") == route and provenance.get("event_end_ms") == event_end_ms and
        isinstance(price_end, int) and 0 <= event_end_ms-price_end < 900_000 and
        isinstance(geometry_end, int) and 0 <= event_end_ms-geometry_end < TF_MS["240"] and
        isinstance(source_price, (int, float)) and math.isfinite(source_price) and source_price > 0)
    if verified:
        expected = "discount" if source_price < geometry["swing_mid"] else "premium" if source_price > geometry["swing_mid"] else "equilibrium"
        verified = zone == expected and provenance.get("geometry") == geometry
    return {"kind": "4h_pivot_range", "zone": zone,
            "status": "reported_with_geometry" if zone and valid else "reported_without_geometry" if zone else "unavailable",
            "source": "existing_selection_gate/Bybit_OHLC" if zone else None,
            "selection_reason": (gate or {}).get("reason"), "source_route": route,
            "asof_end_ms": price_end if verified else None,
            "geometry_end_ms": geometry_end if verified else None,
            "observation_price": source_price if verified else None,
            "recorded_for_event_end_ms": event_end_ms,
            "event_time_verified": verified,
            "geometry": geometry if valid and zone else None,
            "is_volume_profile_zone": False, "is_direction_gate": False}


def zone_note(scenario):
    zone = (scenario or {}).get("zone_observation") or {}
    name = zone.get("zone") or "нет сохранённых данных"
    timing = "цена закрытой сигнальной 15м" if zone.get("event_time_verified") else "время исходной цены не подтверждено"
    return f"Зона 4ч pivot-range (reported): {name}; {timing}. Это не Volume Profile. Классификатор BUY/SELL зону не проверяет; исходный канал отбора может её учитывать. Направление — наблюдение, не вход."


def classify(findings, contexts, event_end_ms, sweep=None, zone=None):
    result = {"version": VERSION, "side": None, "status": "unconfirmed",
              "reason": "no_directional_trigger", "entry_confirmed": False,
              "event_end_ms": event_end_ms, "contexts": contexts,
              "zone_observation": zone or zone_observation(None, event_end_ms, None),
              "zone_is_direction_gate": False}
    directions = {"BUY" if x["code"] == "structure_up" else "SELL"
                  for x in findings if x.get("code") in ("structure_up", "structure_down")}
    if sweep and sweep.get("direction") in ("BUY", "SELL"):
        directions.add(sweep["direction"])
    if len(directions) > 1:
        return {**result, "status": "conflict", "reason": "conflicting_triggers"}
    if not directions:
        return result
    side = next(iter(directions))
    for tf, duration in TF_MS.items():
        ctx = contexts.get(tf) or {}
        end = ctx.get("end_ms")
        if not isinstance(end, int) or not 0 <= event_end_ms - end < duration:
            return {**result, "reason": "missing_or_stale_" + tf}
        context_side = TREND_SIDE.get(ctx.get("trend"))
        if context_side is None:
            return {**result, "reason": "non_directional_" + tf}
        if context_side != side:
            return {**result, "status": "conflict", "reason": "trigger_conflicts_" + tf}
    return {**result, "side": side, "status": "scenario", "reason": "trigger_and_4h_1d_agree"}


def label(scenario):
    side = scenario.get("side") if scenario else None
    return side if side in ("BUY", "SELL") else "БЕЗ НАПРАВЛЕНИЯ"


def explanation(scenario):
    code = (scenario or {}).get("reason", "missing")
    return {"trigger_and_4h_1d_agree": "событие и закрытые 4ч/1д согласованы; только сценарий",
            "conflicting_triggers": "события дают противоположные направления",
            "no_directional_trigger": "направленный триггер не подтверждён",
            "missing_or_stale_240": "нет свежей закрытой 4ч свечи",
            "missing_or_stale_D": "нет свежей закрытой дневной свечи",
            "non_directional_240": "4ч контекст не имеет направления",
            "non_directional_D": "дневной контекст не имеет направления",
            "trigger_conflicts_240": "событие противоречит 4ч контексту",
            "trigger_conflicts_D": "событие противоречит дневному контексту"}.get(code, "нет данных")


def ensure_schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS signal_scenarios (alert_id TEXT PRIMARY KEY, contract_json TEXT NOT NULL)")


def save(db, alert_id, scenario):
    # Immutable: duplicate/late analyses cannot replace the delivered context.
    db.execute("INSERT OR IGNORE INTO signal_scenarios VALUES (?,?)",
               (alert_id, json.dumps(scenario, ensure_ascii=False, allow_nan=False)))

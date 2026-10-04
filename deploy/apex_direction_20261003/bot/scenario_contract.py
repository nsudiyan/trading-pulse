"""Presentation-only directional scenario. Never an execution signal or new gate."""
import json
import math

VERSION = "direction-context-v5-bos-4h-zone"
TF_MS = {"240": 14_400_000}
TREND_SIDE = {"рост": "BULLISH", "снижение": "BEARISH"}


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
    return f"Зона 4ч pivot-range: {name}; {timing}. Для направления требуется BOS + тренд закрытой 4ч + соответствующая зона. Это сценарий, не точка входа."


def classify(findings, contexts, event_end_ms, sweep=None, zone=None):
    result = {"version": VERSION, "side": None, "status": "unconfirmed",
              "reason": "suppressed_direction:conflict", "entry_confirmed": False,
              "event_end_ms": event_end_ms, "contexts": contexts,
              "zone_observation": zone or zone_observation(None, event_end_ms, None),
              "zone_is_direction_gate": True}
    # structure_up/down also includes CHoCH and unclassified first breaks.
    # The user's matrix is explicitly BOS-based, so do not promote those to
    # BOS_UP/BOS_DOWN just because the close crossed a pivot.
    directions = {"BUY" if x["code"] == "structure_up" else "SELL"
                  for x in findings
                  if x.get("code") in ("structure_up", "structure_down")
                  and str(x.get("name", "")).startswith("BOS:")}
    if len(directions) != 1:
        return {**result, "status": "suppressed", "reason": "suppressed_direction:conflict"}
    side = next(iter(directions))
    ctx = contexts.get("240") or {}
    end = ctx.get("end_ms")
    if not isinstance(end, int) or not 0 <= event_end_ms - end < TF_MS["240"]:
        return {**result, "status": "suppressed", "reason": "suppressed_direction:conflict"}
    context_side = TREND_SIDE.get(ctx.get("trend"))
    observed_zone = result["zone_observation"]
    # A raw zone label is not enough: direction requires the immutable,
    # event-time-verified 4h geometry captured for this exact candle.
    from provenance_guard import verified_zone
    if not verified_zone(result) or observed_zone.get("zone") not in ("discount", "premium"):
        return {**result, "status": "suppressed", "reason": "suppressed_direction:conflict"}
    zone_name = observed_zone["zone"]
    if side == "BUY" and context_side == "BULLISH" and zone_name == "discount":
        return {**result, "side": "BUY", "status": "scenario", "reason": "bos_up_4h_bullish_discount"}
    if side == "SELL" and context_side == "BEARISH" and zone_name == "premium":
        return {**result, "side": "SELL", "status": "scenario", "reason": "bos_down_4h_bearish_premium"}
    return {**result, "status": "suppressed", "reason": "suppressed_direction:conflict"}


def label(scenario):
    side = scenario.get("side") if scenario else None
    return side if side in ("BUY", "SELL") else "БЕЗ НАПРАВЛЕНИЯ"


def alert_heading(symbol, scenario, category="linear"):
    """Compact direction heading; this is an observation, not an entry signal."""
    market = "INVERSE" if category == "inverse" else "LINEAR"
    side = (scenario or {}).get("side")
    if side == "BUY":
        return f"📈 BUY {symbol} [{market}]"
    if side == "SELL":
        return f"📉 SELL {symbol} [{market}]"
    return f"🔎 {symbol} [{market}] · NO-TRADE"


def apply_direction_gate(quality, scenario):
    """Shared final direction gate for generic and strong-sweep observations."""
    if quality.get("send") and (scenario or {}).get("side") not in ("BUY", "SELL"):
        return {**quality, "send": False, "reason": "suppressed_direction:conflict"}
    return quality


def explanation(scenario):
    code = (scenario or {}).get("reason", "missing")
    return {"bos_up_4h_bullish_discount": "BOS вверх + бычий тренд закрытой 4ч + discount; сценарий, не вход",
            "bos_down_4h_bearish_premium": "BOS вниз + медвежий тренд закрытой 4ч + premium; сценарий, не вход",
            "suppressed_direction:conflict": "BUY/SELL подавлен: BOS, закрытая 4ч или зона не совпали с матрицей",
            "conflicting_triggers": "события дают противоположные направления",
            "no_directional_trigger": "направленный триггер не подтверждён",
            "missing_or_stale_240": "нет свежей закрытой 4ч свечи",
            "non_directional_240": "4ч контекст не имеет направления",
            "trigger_conflicts_240": "событие противоречит 4ч контексту"}.get(code, "направление подавлено матрицей BOS/4ч/зона")


def ensure_schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS signal_scenarios (alert_id TEXT PRIMARY KEY, contract_json TEXT NOT NULL)")


def save(db, alert_id, scenario):
    # Immutable: duplicate/late analyses cannot replace the delivered context.
    db.execute("INSERT OR IGNORE INTO signal_scenarios VALUES (?,?)",
               (alert_id, json.dumps(scenario, ensure_ascii=False, allow_nan=False)))

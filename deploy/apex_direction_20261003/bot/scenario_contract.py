"""Presentation-only directional scenario. Never an execution signal or new gate."""
import json

VERSION = "direction-context-v1"
TF_MS = {"240": 14_400_000, "D": 86_400_000}
TREND_SIDE = {"рост": "BUY", "снижение": "SELL"}


def classify(findings, contexts, event_end_ms, sweep=None):
    result = {"version": VERSION, "side": None, "status": "unconfirmed",
              "reason": "no_directional_trigger", "entry_confirmed": False,
              "event_end_ms": event_end_ms, "contexts": contexts}
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

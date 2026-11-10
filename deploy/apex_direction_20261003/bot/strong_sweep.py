"""Conservative closed-bar sweep review, separate from the trend-following gate."""
from __future__ import annotations

from quality import finding_family
from setup_layers import (ORDER, TF_MS, quote_volume_24h,
                          timeframe_metrics, asof_series, zone_snapshot)
from sweeps import asia_range_sweep, equal_level_sweep
from sweep_gate import sweep_activity_ratio


def strong_sweep_review(view, symbol: str, end_ms: int, findings: list[dict],
                        *, category: str, config: dict) -> dict:
    """Return facts for a review alert, or a precise rejection reason.

    The last 15m candle must itself contain the pattern. A mixed 15m EMA is
    allowed because this is a possible reversal observation; 4h trend and
    swing midpoint must agree with the pattern. No entry is inferred.
    """
    rejected = lambda reason: {"send": False, "reason": reason,
        **zone_snapshot("strong_sweep_review", None, {}, None, end_ms)}
    bars = asof_series(view.bars.get((symbol, "15"), []), end_ms)
    if not bars or int(bars[-1]["end_ms"]) != end_ms:
        return rejected("not_current_closed_15m")
    hourly = asof_series(view.bars.get((symbol, "60"), []), end_ms)
    sweep = equal_level_sweep(bars) or asia_range_sweep(bars, hourly)
    if not sweep or sweep["end_ms"] != end_ms:
        return rejected("no_current_sweep")
    ratio = sweep_activity_ratio(bars, category)
    if ratio is None or ratio < float(config.get("min_volume_ratio", 2.5)):
        return rejected("sweep_activity_unconfirmed")
    quote = quote_volume_24h(bars, category=category, symbol=symbol)
    if quote is None or quote < float(config.get("min_quote_turnover_24h", 50_000_000)):
        return rejected("sweep_liquidity_unconfirmed")
    side = sweep["direction"]
    direction = "рост" if side == "BUY" else "снижение"
    frames = {}
    for tf in ORDER:
        series = asof_series(view.bars.get((symbol, tf), []), end_ms)
        latest = series[-1] if series else None
        fresh = latest is not None and 0 <= end_ms - int(latest["end_ms"]) < TF_MS[tf]
        frames[tf] = timeframe_metrics(series, tf) if fresh else timeframe_metrics([], tf)
    if frames["240"]["trend"] != direction:
        return rejected("sweep_4h_trend_mismatch")
    for tf in ("D", "W"):
        if frames[tf]["trend"] in {"рост", "снижение"} and frames[tf]["trend"] != direction:
            return rejected(f"sweep_{tf}_opposes")
    midpoint = frames["240"]["swing_mid"]
    if midpoint is None:
        return rejected("sweep_4h_midpoint_unavailable")
    h4 = asof_series(view.bars.get((symbol, "240"), []), end_ms)
    snapshot = zone_snapshot("strong_sweep_review", bars[-1], frames["240"],
                             int(h4[-1]["end_ms"]) if h4 else None, end_ms)
    zone = snapshot["zone"]
    if zone != ("discount" if side == "BUY" else "premium"):
        return rejected("sweep_4h_zone_mismatch")
    families = {family for finding in findings
                if (family := finding_family(finding["code"], finding.get("source", "")))}
    if len(families) < int(config.get("min_independent_families", 3)):
        return rejected("sweep_insufficient_families")
    sweep = {**sweep, "volume_ratio": ratio,
             "volume_basis": "contract_volume" if category == "inverse" else "quote_turnover"}
    return {"send": True, "reason": "strong_sweep_review", "direction": direction,
            "sweep": sweep, "quote_24h": quote, **snapshot,
            "swing_high": frames["240"]["swing_high"],
            "swing_low": frames["240"]["swing_low"], "swing_mid": midpoint,
            "trends": {tf: frames[tf]["trend"] for tf in ORDER},
            "count": sum(frame["trend"] == direction for frame in frames.values()),
            "families": families}

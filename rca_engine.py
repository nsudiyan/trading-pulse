"""
rca_engine.py — Root Cause Analysis engine for closed trades.

For each resolved trade, produces structured RCA output:
  - outcome_category: WIN / LOSS / FLAT
  - tags: named failure/success factors
  - primary_cause: single dominant reason for the outcome
  - signal_validity: per-signal consistency assessment
  - market_context: market conditions snapshot at entry
  - rca_summary: human-readable one-block summary

Integration: called automatically by outcome_tracker.py after each trade resolves.
Results are appended to outcomes/rca_results.json.

CLI:
  python3 rca_engine.py                      -- top 10 by abs move (24h)
  python3 rca_engine.py --horizon 4h         -- 4h horizon
  python3 rca_engine.py --symbol BTCUSDT     -- single symbol history
  python3 rca_engine.py --aggregate          -- systemic pattern report
  python3 rca_engine.py --out rca.json       -- dump full JSON
"""

import csv
import json
import argparse
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

BASE_DIR    = Path(__file__).parent
CSV_PATH    = BASE_DIR / "outcomes" / "resolved.csv"
RCA_PATH    = BASE_DIR / "outcomes" / "rca_results.json"

WIN_OUTCOMES  = {"TP1", "WIN"}
LOSS_OUTCOMES = {"STOP", "LOSS"}
FLAT_OUTCOMES = {"FLAT"}


# ─── helpers ─────────────────────────────────────────────────────────────────

def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _outcome_category(outcome: str) -> str:
    if outcome in WIN_OUTCOMES:
        return "WIN"
    if outcome in LOSS_OUTCOMES:
        return "LOSS"
    return "FLAT"


# ─── signal validity ─────────────────────────────────────────────────────────

def _assess_signal_validity(trade: dict, direction: str) -> dict:
    """Per-signal assessment: was each signal consistent with the trade direction?"""
    is_long = direction == "ЛОНГ"

    choch    = _i(trade.get("choch_bull_1h", 0))
    mtf_bull = _i(trade.get("mtf_bull", 0))
    mtf_bear = _i(trade.get("mtf_bear", 0))
    ema_1h   = _i(trade.get("ema_bull_1h", 0))
    ema_4h   = _i(trade.get("ema_bull_4h", 0))
    cvd_k    = _f(trade.get("cvd_kline", 0))
    cvd_t    = _f(trade.get("cvd_trade", 0))
    funding  = _f(trade.get("funding", 0))
    oi_24h   = _f(trade.get("oi_24h_pct", 0))
    rsi      = _f(trade.get("rsi_1h", 50))
    rs_btc   = _f(trade.get("rs_btc", 0))

    if is_long:
        return {
            "choch_bull_1h":  "valid"     if choch == 1         else "absent",
            "ema_1h":         "valid"     if ema_1h == 1        else "absent",
            "ema_4h":         "valid"     if ema_4h == 1        else "absent",
            "mtf_alignment":  "strong"    if mtf_bull >= 4      else ("weak" if mtf_bull >= 2 else "absent"),
            "cvd_kline":      "aligned"   if cvd_k > 0          else ("divergent" if cvd_k < -10 else "neutral"),
            "cvd_trade":      "aligned"   if cvd_t > 0          else ("divergent" if cvd_t < -10 else "neutral"),
            "funding":        "favorable" if funding < -0.01    else ("extreme" if funding > 0.05 else "neutral"),
            "oi_trend":       "favorable" if oi_24h < -2        else ("extreme" if oi_24h > 20 else "neutral"),
            "rsi_zone":       "neutral"   if 30 <= rsi <= 60    else ("overbought" if rsi > 70 else "oversold"),
            "rs_btc":         "strong"    if rs_btc > 5         else ("weak" if rs_btc < -10 else "neutral"),
        }
    else:  # SHORT
        return {
            "choch_bull_1h":  "valid"     if choch == 0         else "divergent",
            "ema_1h":         "valid"     if ema_1h == 0        else "divergent",
            "ema_4h":         "valid"     if ema_4h == 0        else "divergent",
            "mtf_alignment":  "strong"    if mtf_bear >= 4      else ("weak" if mtf_bear >= 2 else "absent"),
            "cvd_kline":      "aligned"   if cvd_k < 0          else ("divergent" if cvd_k > 10 else "neutral"),
            "cvd_trade":      "aligned"   if cvd_t < 0          else ("divergent" if cvd_t > 10 else "neutral"),
            "funding":        "favorable" if funding > 0.01     else ("extreme" if funding < -0.05 else "neutral"),
            "oi_trend":       "favorable" if oi_24h < -2        else ("extreme" if oi_24h > 20 else "neutral"),
            "rsi_zone":       "neutral"   if 40 <= rsi <= 70    else ("oversold" if rsi < 30 else "overbought"),
            "rs_btc":         "strong"    if rs_btc < -5        else ("weak" if rs_btc > 10 else "neutral"),
        }


# ─── market context ───────────────────────────────────────────────────────────

def _build_market_context(trade: dict) -> dict:
    """Snapshot of market conditions at the time of entry."""
    rsi     = _f(trade.get("rsi_1h", 50))
    funding = _f(trade.get("funding", 0))
    oi_24h  = _f(trade.get("oi_24h_pct", 0))
    mtf_b   = _i(trade.get("mtf_bull", 0))
    mtf_s   = _i(trade.get("mtf_bear", 0))
    cvd_k   = _f(trade.get("cvd_kline", 0))
    cvd_t   = _f(trade.get("cvd_trade", 0))

    if rsi > 70:
        rsi_zone = "overbought"
    elif rsi < 30:
        rsi_zone = "oversold"
    elif rsi > 55:
        rsi_zone = "elevated"
    elif rsi < 40:
        rsi_zone = "depressed"
    else:
        rsi_zone = "neutral"

    if funding > 0.05:
        funding_regime = "extreme_positive"
    elif funding > 0.01:
        funding_regime = "positive"
    elif funding < -0.05:
        funding_regime = "extreme_negative"
    elif funding < -0.01:
        funding_regime = "negative"
    else:
        funding_regime = "neutral"

    if oi_24h > 20:
        oi_regime = "high_expansion"
    elif oi_24h > 5:
        oi_regime = "expanding"
    elif oi_24h < -10:
        oi_regime = "high_contraction"
    elif oi_24h < -2:
        oi_regime = "contracting"
    else:
        oi_regime = "stable"

    if mtf_b >= 4 and mtf_s <= 1:
        mtf_bias = "strongly_bullish"
    elif mtf_b >= 3 and mtf_s <= 2:
        mtf_bias = "bullish"
    elif mtf_s >= 4 and mtf_b <= 1:
        mtf_bias = "strongly_bearish"
    elif mtf_s >= 3 and mtf_b <= 2:
        mtf_bias = "bearish"
    else:
        mtf_bias = "mixed"

    if cvd_k > 10 and cvd_t > 10:
        cvd_trend = "strong_buying"
    elif cvd_k > 0 and cvd_t > 0:
        cvd_trend = "buying"
    elif cvd_k < -10 and cvd_t < -10:
        cvd_trend = "strong_selling"
    elif cvd_k < 0 and cvd_t < 0:
        cvd_trend = "selling"
    else:
        cvd_trend = "mixed"

    return {
        "rsi_1h":         rsi,
        "rsi_zone":       rsi_zone,
        "funding":        funding,
        "funding_regime": funding_regime,
        "oi_24h_pct":     oi_24h,
        "oi_regime":      oi_regime,
        "mtf_bull":       mtf_b,
        "mtf_bear":       mtf_s,
        "mtf_bias":       mtf_bias,
        "cvd_kline":      cvd_k,
        "cvd_trade":      cvd_t,
        "cvd_trend":      cvd_trend,
    }


# ─── tagging ─────────────────────────────────────────────────────────────────

def _tag_trade(trade: dict, direction: str) -> list[str]:
    """Assign named factors (tags) explaining the trade's outcome."""
    is_long = direction == "ЛОНГ"
    tags    = []

    funding = _f(trade.get("funding", 0))
    oi_24h  = _f(trade.get("oi_24h_pct", 0))
    rsi     = _f(trade.get("rsi_1h", 50))
    mtf_b   = _i(trade.get("mtf_bull", 0))
    mtf_s   = _i(trade.get("mtf_bear", 0))
    cvd_k   = _f(trade.get("cvd_kline", 0))
    cvd_t   = _f(trade.get("cvd_trade", 0))
    choch   = _i(trade.get("choch_bull_1h", 0))
    ema_1h  = _i(trade.get("ema_bull_1h", 0))
    ema_4h  = _i(trade.get("ema_bull_4h", 0))
    rs_btc  = _f(trade.get("rs_btc", 0))
    score   = _f(trade.get("score", 0))
    setup   = trade.get("setup", "")

    # ── Funding ──
    if is_long and funding > 0.05:
        tags.append("CROWDED_LONG")
    elif not is_long and funding < -0.05:
        tags.append("CROWDED_SHORT")
    elif is_long and funding < -0.01:
        tags.append("FUNDING_FAVORABLE_LONG")
    elif not is_long and funding > 0.01:
        tags.append("FUNDING_FAVORABLE_SHORT")

    # ── RSI ──
    if is_long and rsi > 70:
        tags.append("OVERBOUGHT_ENTRY")
    elif is_long and rsi < 40:
        tags.append("RSI_OVERSOLD_LONG")
    elif not is_long and rsi < 30:
        tags.append("OVERSOLD_ENTRY")
    elif not is_long and rsi > 60:
        tags.append("RSI_HIGH_SHORT")

    # ── OI ──
    if oi_24h > 20:
        tags.append("OI_SURGE")
    elif is_long and oi_24h < -10:
        tags.append("OI_CAPITULATION_LONG")
    elif is_long and oi_24h < -2:
        tags.append("OI_CONTRACTING_LONG")

    # ── MTF ──
    if mtf_b >= 1 and mtf_s >= 3:
        tags.append("MTF_DIVERGENCE")
    elif is_long and mtf_b >= 4:
        tags.append("MTF_STRONG_BULL")
    elif not is_long and mtf_s >= 4:
        tags.append("MTF_STRONG_BEAR")

    # ── CVD ──
    kline_bull = cvd_k > 0
    trade_bull = cvd_t > 0
    if kline_bull != trade_bull and abs(cvd_k) > 10 and abs(cvd_t) > 10:
        tags.append("CVD_DIVERGENCE")
    elif is_long and cvd_k > 0 and cvd_t > 0:
        tags.append("CVD_ALIGNED_LONG")
    elif not is_long and cvd_k < 0 and cvd_t < 0:
        tags.append("CVD_ALIGNED_SHORT")

    # ── CHoCH ──
    if choch == 1:
        tags.append("CHOCH_CONFIRMED")
    elif is_long and choch == 0:
        tags.append("CHOCH_MISSING")

    # ── EMA ──
    if is_long and ema_1h == 1 and ema_4h == 1:
        tags.append("EMA_ALIGNED_LONG")
    elif is_long and ema_1h == 0 and ema_4h == 0:
        tags.append("EMA_ABSENT")

    # ── RS BTC ──
    if is_long and rs_btc < -10:
        tags.append("WEAK_RS_BTC")
    elif is_long and rs_btc > 5:
        tags.append("STRONG_RS_BTC")

    # ── Setup-specific ──
    if setup == "breakout" and score > 120:
        tags.append("BREAKOUT_SCORE_OVERCALIBRATED")
    if setup == "range_sweep":
        tags.append("SWEEP_DETECTION_LAG_RISK")
    if setup == "bos_fvg" and score > 150:
        tags.append("BOS_FVG_HIGH_SCORE")

    # ── Score regime ──
    if score > 120 and setup in ("breakout", "range_sweep"):
        tags.append("SCORE_ANTICORRELATED_SETUP")
    if score < 90:
        tags.append("LOW_SCORE")
    elif score > 150:
        tags.append("HIGH_SCORE")

    # ── Squeeze mid-score band (known weak zone) ──
    if setup == "squeeze" and 100 <= score <= 140:
        tags.append("SQUEEZE_MIDBAND_SCORE")

    return tags


# ─── primary cause ────────────────────────────────────────────────────────────

# Ordered by diagnostic priority: first matching tag wins.
_LOSS_CAUSES = [
    ("CROWDED_LONG",                   "Funding extreme — crowded long reversed against position"),
    ("CROWDED_SHORT",                  "Funding extreme — crowded short squeezed upward"),
    ("OVERBOUGHT_ENTRY",               "Overbought RSI entry — countertrend long against distribution zone"),
    ("OVERSOLD_ENTRY",                 "Oversold RSI entry — countertrend short against accumulation zone"),
    ("OI_SURGE",                       "OI spike — excessive leverage build-up preceded the flush"),
    ("MTF_DIVERGENCE",                 "Multi-timeframe divergence — no clear trend bias at entry"),
    ("CVD_DIVERGENCE",                 "CVD conflict — kline and trade-flow disagreed on dominant pressure"),
    ("SCORE_ANTICORRELATED_SETUP",     "High score on miscalibrated setup — scoring model inflated this signal"),
    ("BREAKOUT_SCORE_OVERCALIBRATED",  "Breakout false signal — high score without structural hold confirmation"),
    ("SWEEP_DETECTION_LAG_RISK",       "Range sweep detection lag — signal stale due to cron polling cycle"),
    ("SQUEEZE_MIDBAND_SCORE",          "Squeeze mid-band score (100-140) — known weak zone; U-shaped WR curve"),
    ("CHOCH_MISSING",                  "CHoCH absent — structural shift confirmation not triggered at entry"),
    ("WEAK_RS_BTC",                    "Weak relative strength vs BTC — coin underperforming market at entry"),
    ("EMA_ABSENT",                     "No EMA support on any timeframe — trend alignment entirely missing"),
    ("RSI_HIGH_SHORT",                 "Elevated RSI for short entry — momentum not yet exhausted"),
]

_WIN_CAUSES = [
    ("CHOCH_CONFIRMED",         "CHoCH confirmed structure shift — strongest historical predictor (+12pp WR)"),
    ("BOS_FVG_HIGH_SCORE",      "BOS/FVG high-score setup — validated pattern on best-performing setup"),
    ("OI_CAPITULATION_LONG",    "OI capitulation — smart money flushed weak hands, reversal followed"),
    ("OI_CONTRACTING_LONG",     "OI contracting with price rising — organic squeeze without over-leverage"),
    ("FUNDING_FAVORABLE_LONG",  "Negative funding for long — market paying longs, healthy non-crowded setup"),
    ("FUNDING_FAVORABLE_SHORT", "Positive funding for short — market paying shorts, healthy non-crowded setup"),
    ("MTF_STRONG_BULL",         "Strong MTF bullish alignment — trend confluence on multiple timeframes"),
    ("MTF_STRONG_BEAR",         "Strong MTF bearish alignment — trend confluence on multiple timeframes"),
    ("CVD_ALIGNED_LONG",        "CVD aligned with long — buying pressure confirmed across both metrics"),
    ("CVD_ALIGNED_SHORT",       "CVD aligned with short — selling pressure confirmed across both metrics"),
    ("RSI_OVERSOLD_LONG",       "RSI oversold on long entry — mean-reversion room and no distribution"),
    ("STRONG_RS_BTC",           "Strong relative strength vs BTC — coin outperforming market, trend momentum"),
]


def _primary_cause(tags: list[str], outcome_cat: str, setup: str) -> str:
    tag_set = set(tags)
    if outcome_cat == "LOSS":
        for tag, cause in _LOSS_CAUSES:
            if tag in tag_set:
                return cause
        return "Market noise — no dominant failure signal; consider wider stop or smaller size"
    if outcome_cat == "WIN":
        for tag, cause in _WIN_CAUSES:
            if tag in tag_set:
                return cause
        return "Signal convergence — multiple factors aligned without a single dominant predictor"
    return "FLAT — trade resolved within noise band; neither TP1 nor stop reached"


# ─── core analysis ────────────────────────────────────────────────────────────

def analyze_trade(trade: dict, horizon: str = "24h") -> dict:
    """
    Run root cause analysis on a single resolved trade record.

    Args:
        trade:   Row dict from resolved.csv or a fully-resolved pending.json entry.
        horizon: "4h" or "24h"

    Returns:
        RCA dict with tags, primary_cause, signal_validity, market_context, rca_summary.
    """
    outcome = trade.get(f"outcome_{horizon}", "")
    if not outcome:
        outcome = trade.get("outcome_4h", "")
    if not outcome:
        outcome = "UNKNOWN"

    outcome_cat = _outcome_category(outcome)
    direction   = trade.get("direction", "ЛОНГ")
    setup       = trade.get("setup", "unknown")
    score       = _f(trade.get("score", 0))
    symbol      = trade.get("symbol", "UNKNOWN")

    signal_validity = _assess_signal_validity(trade, direction)
    market_context  = _build_market_context(trade)
    tags            = _tag_trade(trade, direction)
    primary_cause   = _primary_cause(tags, outcome_cat, setup)

    valid_count      = sum(1 for v in signal_validity.values()
                           if v in ("valid", "aligned", "favorable", "strong"))
    conflicting_count = sum(1 for v in signal_validity.values()
                            if v in ("divergent", "extreme", "overbought", "oversold", "absent", "weak"))

    price_change  = _f(trade.get(f"change_{horizon}_pct", 0))
    price_entry   = _f(trade.get("price_entry", 0))
    stop          = _f(trade.get("stop", 0))
    tp1           = _f(trade.get("tp1", 0))

    stop_pct  = abs(stop - price_entry) / price_entry * 100  if (price_entry and stop) else None
    tp1_pct   = abs(tp1  - price_entry) / price_entry * 100  if (price_entry and tp1)  else None
    rr_ratio  = (tp1_pct / stop_pct) if (stop_pct and tp1_pct and stop_pct > 0) else None

    rca_lines = [
        f"**{symbol}** | {setup} | {direction} | Score: {score:.0f} | Outcome: {outcome} ({horizon})",
        f"Price change at {horizon}: {price_change:+.2f}%",
    ]
    if rr_ratio is not None:
        rca_lines.append(f"R:R ratio at entry: {rr_ratio:.2f}")
    rca_lines.append(f"Primary cause: {primary_cause}")
    if tags:
        rca_lines.append(f"Tags: {', '.join(tags)}")
    rca_lines.append(f"Signal validity: {valid_count} confirmed / {conflicting_count} conflicting")

    return {
        "symbol":                symbol,
        "run_ts":                trade.get("run_ts", ""),
        "setup":                 setup,
        "direction":             direction,
        "score":                 score,
        "grade":                 trade.get("grade", ""),
        "horizon":               horizon,
        "outcome":               outcome,
        "outcome_category":      outcome_cat,
        "price_change_pct":      price_change,
        "stop_pct":              stop_pct,
        "tp1_pct":               tp1_pct,
        "rr_ratio":              rr_ratio,
        "tags":                  tags,
        "primary_cause":         primary_cause,
        "signal_validity":       signal_validity,
        "market_context":        market_context,
        "valid_signal_count":    valid_count,
        "conflicting_signal_count": conflicting_count,
        "rca_summary":           "\n".join(rca_lines),
    }


# ─── batch analysis ───────────────────────────────────────────────────────────

def analyze_csv(csv_path: Path = CSV_PATH, horizon: str = "24h",
                symbol_filter: Optional[str] = None) -> list[dict]:
    """Run RCA on all decided trades in resolved.csv. Returns list of RCA dicts."""
    decided = WIN_OUTCOMES | LOSS_OUTCOMES | FLAT_OUTCOMES
    results = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if symbol_filter and row.get("symbol") != symbol_filter:
                continue
            outcome = row.get(f"outcome_{horizon}", "") or row.get("outcome_4h", "")
            if outcome in decided:
                results.append(analyze_trade(row, horizon))
    return results


# ─── persistence ─────────────────────────────────────────────────────────────

def store_rca(rca: dict, out_path: Path = RCA_PATH):
    """Append a single RCA record to rca_results.json (create if missing)."""
    existing: list[dict] = []
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            existing = []

    # Dedup by (symbol, run_ts, horizon, outcome)
    key = (rca.get("symbol"), rca.get("run_ts"), rca.get("horizon"), rca.get("outcome"))
    for r in existing:
        if (r.get("symbol"), r.get("run_ts"), r.get("horizon"), r.get("outcome")) == key:
            return  # already stored

    existing.append(rca)
    out_path.write_text(json.dumps(existing, indent=2, default=str), encoding="utf-8")


# ─── aggregate ───────────────────────────────────────────────────────────────

def aggregate_patterns(rcas: list[dict]) -> dict:
    """
    Aggregate RCA results across many trades to surface systemic patterns.
    """
    wins   = [r for r in rcas if r["outcome_category"] == "WIN"]
    losses = [r for r in rcas if r["outcome_category"] == "LOSS"]
    flats  = [r for r in rcas if r["outcome_category"] == "FLAT"]

    win_tags   = Counter(t for r in wins   for t in r["tags"])
    loss_tags  = Counter(t for r in losses for t in r["tags"])
    all_tags   = Counter(t for r in rcas   for t in r["tags"])

    _cat_key = {"WIN": "wins", "LOSS": "losses", "FLAT": "flats"}
    setup_stats: dict[str, dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "flats": 0, "n": 0})
    for r in rcas:
        s   = r["setup"]
        cat = r["outcome_category"]
        setup_stats[s][_cat_key.get(cat, "flats")] += 1
        setup_stats[s]["n"] += 1

    for stats in setup_stats.values():
        decided = stats["wins"] + stats["losses"]
        stats["wr"] = round(stats["wins"] / decided, 3) if decided else 0.0

    cause_counts = Counter(r["primary_cause"] for r in losses)
    total_decided = len(wins) + len(losses)

    return {
        "total":                  len(rcas),
        "wins":                   len(wins),
        "losses":                 len(losses),
        "flats":                  len(flats),
        "overall_wr":             round(len(wins) / total_decided, 3) if total_decided else 0.0,
        "top_failure_tags":       loss_tags.most_common(12),
        "top_success_tags":       win_tags.most_common(12),
        "most_frequent_tags":     all_tags.most_common(15),
        "primary_failure_causes": cause_counts.most_common(8),
        "per_setup":              dict(setup_stats),
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _print_aggregate(agg: dict):
    print(f"\n=== RCA Aggregate Report ===")
    print(f"Trades: {agg['total']}  |  WR: {agg['overall_wr']:.1%}  "
          f"(W:{agg['wins']} L:{agg['losses']} F:{agg['flats']})\n")

    print("── Top Failure Tags ──")
    for tag, n in agg["top_failure_tags"]:
        print(f"  {tag:<40} {n:>4}")

    print("\n── Top Success Tags ──")
    for tag, n in agg["top_success_tags"]:
        print(f"  {tag:<40} {n:>4}")

    print("\n── Primary Failure Causes ──")
    for cause, n in agg["primary_failure_causes"]:
        print(f"  [{n:>4}] {cause[:90]}")

    print("\n── Per-Setup Win Rates ──")
    for setup, stats in sorted(agg["per_setup"].items(), key=lambda x: -x[1].get("wr", 0)):
        print(f"  {setup:<16} WR={stats['wr']:.1%}  n={stats['n']}")


def main():
    parser = argparse.ArgumentParser(description="RCA engine for closed trades")
    parser.add_argument("--csv",       default=str(CSV_PATH))
    parser.add_argument("--horizon",   default="24h", choices=["4h", "24h"])
    parser.add_argument("--out",       default=None,  help="Write RCA JSON to file")
    parser.add_argument("--symbol",    default=None,  help="Filter to single symbol")
    parser.add_argument("--aggregate", action="store_true")
    parser.add_argument("--top",       type=int, default=10, help="Show top N by abs price move")
    args = parser.parse_args()

    rcas = analyze_csv(Path(args.csv), horizon=args.horizon, symbol_filter=args.symbol)

    if args.out:
        Path(args.out).write_text(json.dumps(rcas, indent=2, default=str), encoding="utf-8")
        print(f"Wrote {len(rcas)} RCA records to {args.out}")
        return

    if args.aggregate:
        _print_aggregate(aggregate_patterns(rcas))
        return

    if args.symbol:
        for rca in rcas:
            print(rca["rca_summary"])
            print()
        return

    # Default: top N by abs price move
    ranked = sorted(rcas, key=lambda r: abs(r.get("price_change_pct", 0)), reverse=True)
    for rca in ranked[: args.top]:
        print(rca["rca_summary"])
        print()


if __name__ == "__main__":
    main()

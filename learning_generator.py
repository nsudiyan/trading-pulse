"""
learning_generator.py — Per-trade error diagnosis and learning output generator.

For each resolved trade (after RCA), classifies the error category and generates:
  - error_category:       SIGNAL_ERROR | FILTERING_ERROR | TIMING_ERROR | RISK_MGMT_ERROR | WIN
  - correction_rule:      specific change to screener logic (human-readable)
  - exclusion_filter:     featurized condition string for TLDB gate check
  - confirmation_enhancer: additional condition that would have prevented the loss
  - priority:             HIGH | MEDIUM | LOW (based on recurrence frequency in TLDB)

Outputs are written back to TRADE_LEARNINGS_DB (trade_learnings_db.py).

Integration:
  Called automatically by outcome_tracker.py after each trade resolves.

CLI:
  python3 learning_generator.py                    -- process all losses, write to TLDB
  python3 learning_generator.py --symbol BTCUSDT   -- single symbol
  python3 learning_generator.py --dry-run          -- generate without writing
  python3 learning_generator.py --report           -- show last 20 loss learnings
"""

import json
import argparse
from pathlib import Path
from typing import Optional

BASE_DIR  = Path(__file__).parent
RCA_PATH  = BASE_DIR / "outcomes" / "rca_results.json"
DB_PATH   = BASE_DIR / "outcomes" / "trade_learnings_db.json"


# ─── error category constants ─────────────────────────────────────────────────

SIGNAL_ERROR    = "SIGNAL_ERROR"
FILTERING_ERROR = "FILTERING_ERROR"
TIMING_ERROR    = "TIMING_ERROR"
RISK_MGMT_ERROR = "RISK_MGMT_ERROR"
WIN_CATEGORY    = "WIN"


# ─── tag → error category mappings ───────────────────────────────────────────

_FILTERING_TAGS = {
    "CROWDED_LONG", "CROWDED_SHORT",
    "OVERBOUGHT_ENTRY", "OVERSOLD_ENTRY",
    "OI_SURGE",
}

_SIGNAL_TAGS = {
    "MTF_DIVERGENCE",
    "CVD_DIVERGENCE",
    "SCORE_ANTICORRELATED_SETUP",
    "BREAKOUT_SCORE_OVERCALIBRATED",
    "SWEEP_DETECTION_LAG_RISK",
    "SQUEEZE_MIDBAND_SCORE",
}

_TIMING_TAGS = {
    "CHOCH_MISSING",
    "EMA_ABSENT",
    "WEAK_RS_BTC",
    "RSI_HIGH_SHORT",
}

_STOP_TIGHT_PCT = 0.5   # below this → swept immediately
_STOP_WIDE_PCT  = 6.0   # above this → R:R destroyed
_RR_FLOOR       = 1.5


# ─── helpers ─────────────────────────────────────────────────────────────────

def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ─── error category classification ───────────────────────────────────────────

def classify_error(rca: dict) -> str:
    """
    Classify one resolved trade into a diagnostic error category.

    Precedence (most actionable first):
      RISK_MGMT_ERROR > FILTERING_ERROR > SIGNAL_ERROR > TIMING_ERROR
    """
    if rca.get("outcome_category") == "WIN":
        return WIN_CATEGORY

    tags     = set(rca.get("tags", []))
    stop_pct = _f(rca.get("stop_pct"))
    rr       = _f(rca.get("rr_ratio") or 0)

    if stop_pct and (stop_pct < _STOP_TIGHT_PCT or stop_pct > _STOP_WIDE_PCT):
        return RISK_MGMT_ERROR
    if rr and rr < _RR_FLOOR:
        return RISK_MGMT_ERROR

    if tags & _FILTERING_TAGS:
        return FILTERING_ERROR

    if tags & _SIGNAL_TAGS:
        return SIGNAL_ERROR

    if tags & _TIMING_TAGS:
        return TIMING_ERROR

    return SIGNAL_ERROR


# ─── correction rule text ─────────────────────────────────────────────────────

_RULE = {
    "CROWDED_LONG":                  "Do not enter ЛОНГ when funding_rate > +0.05% — crowded positioning reverses violently",
    "CROWDED_SHORT":                 "Do not enter ШОРТ when funding_rate < -0.05% — crowded short squeezes upward",
    "OVERBOUGHT_ENTRY":              "Skip long signal when RSI_1h > 70 — entering distribution zone against momentum",
    "OVERSOLD_ENTRY":                "Skip short signal when RSI_1h < 30 — entering against mean-reversion pressure",
    "OI_SURGE":                      "Block entry when OI_24h_pct > +20% — over-leveraged market flush risk",
    "MTF_DIVERGENCE":                "Require MTF consensus: abort if mtf_bull >= 1 AND mtf_bear >= 3 simultaneously",
    "CVD_DIVERGENCE":                "Require CVD agreement: skip if kline-CVD and trade-CVD conflict by > 10pp",
    "SCORE_ANTICORRELATED_SETUP":    "Penalise breakout/sweep score above 120 — anti-correlated with actual WR",
    "BREAKOUT_SCORE_OVERCALIBRATED": "Cap breakout signal at score 120 — high score does not confirm structural hold",
    "SWEEP_DETECTION_LAG_RISK":      "Add recency check on sweep signals — signal valid for <= 1 candle post-detection",
    "SQUEEZE_MIDBAND_SCORE":         "Avoid squeeze entries in 100-140 score band — U-shaped WR curve, weakest zone",
    "CHOCH_MISSING":                 "Wait for CHoCH confirmation on 1H before entry — structure shift not yet confirmed",
    "EMA_ABSENT":                    "Require at least 1H EMA alignment before entry — no trend support present",
    "WEAK_RS_BTC":                   "Skip long when RS vs BTC < -10pp — coin underperforming market significantly",
    "RSI_HIGH_SHORT":                "Delay short until RSI rejects from >= 65 — momentum not yet exhausted",
}

_RISK_TIGHT  = "Widen stop to ATR x 1.8 minimum — current stop too tight, liquidity sweep risk"
_RISK_WIDE   = "Reduce position size or tighten stop — stop too wide for acceptable R:R"
_RISK_LOW_RR = "Require R:R >= 1.5 before entry — current setup offers poor expected value"

_FILTERING_ORDER = ["CROWDED_LONG", "CROWDED_SHORT", "OVERBOUGHT_ENTRY", "OVERSOLD_ENTRY", "OI_SURGE"]
_SIGNAL_ORDER    = ["MTF_DIVERGENCE", "CVD_DIVERGENCE", "SCORE_ANTICORRELATED_SETUP",
                    "BREAKOUT_SCORE_OVERCALIBRATED", "SWEEP_DETECTION_LAG_RISK", "SQUEEZE_MIDBAND_SCORE"]
_TIMING_ORDER    = ["CHOCH_MISSING", "EMA_ABSENT", "WEAK_RS_BTC", "RSI_HIGH_SHORT"]


def _correction_rule_text(rca: dict, error_cat: str) -> str:
    tags     = set(rca.get("tags", []))
    stop_pct = _f(rca.get("stop_pct"))
    rr       = _f(rca.get("rr_ratio") or 0)

    if error_cat == RISK_MGMT_ERROR:
        if rr and rr < _RR_FLOOR:
            return _RISK_LOW_RR
        if stop_pct and stop_pct < _STOP_TIGHT_PCT:
            return _RISK_TIGHT
        return _RISK_WIDE

    for tag in _FILTERING_ORDER + _SIGNAL_ORDER + _TIMING_ORDER:
        if tag in tags and tag in _RULE:
            return _RULE[tag]

    cause = rca.get("primary_cause", "")
    return f"Review {rca.get('setup','?')} entry conditions — {cause[:80]}"


# ─── exclusion filter (featurized tokens for TLDB gate) ──────────────────────

# Maps RCA tags to TLDB token strings (format: "feat:val + feat2:val2")
_EXCL_TOKENS = {
    "CROWDED_LONG":                  "funding_regime:fund_extreme_pos",
    "CROWDED_SHORT":                 "funding_regime:fund_extreme_neg",
    "OVERBOUGHT_ENTRY":              "rsi_zone:rsi_overbought",
    "OVERSOLD_ENTRY":                "rsi_zone:rsi_oversold",
    "OI_SURGE":                      "oi_regime:oi_surge",
    "MTF_DIVERGENCE":                "mtf_bull_tier:mtf_bull_moderate + mtf_bear_tier:mtf_bear_moderate",
    "CVD_DIVERGENCE":                "cvd_kline_zone:cvdk_strong_neg + cvd_trade_zone:cvdt_strong_pos",
    "BREAKOUT_SCORE_OVERCALIBRATED": "setup:breakout + score_band:score≥150",
    "SCORE_ANTICORRELATED_SETUP":    "setup:breakout + score_band:score130-150",
    "SWEEP_DETECTION_LAG_RISK":      "setup:range_sweep",
    "SQUEEZE_MIDBAND_SCORE":         "setup:squeeze + score_band:score110-130",
    "CHOCH_MISSING":                 "choch_bull_1h:choch=0",
    "EMA_ABSENT":                    "ema_bull_1h:ema1h=0 + ema_bull_4h:ema4h=0",
    "WEAK_RS_BTC":                   "rs_btc_zone:rs_btc_weak",
    "RSI_HIGH_SHORT":                "rsi_zone:rsi_elevated",
}


def _exclusion_filter(rca: dict, error_cat: str) -> str:
    tags     = set(rca.get("tags", []))
    stop_pct = _f(rca.get("stop_pct"))
    rr       = _f(rca.get("rr_ratio") or 0)

    if error_cat == RISK_MGMT_ERROR:
        if rr and rr < _RR_FLOOR:
            return "rr_ratio:<1.5"
        if stop_pct and stop_pct < _STOP_TIGHT_PCT:
            return "stop_pct:<0.5"
        return "stop_pct:>6.0"

    for tag in _FILTERING_ORDER + _SIGNAL_ORDER + _TIMING_ORDER:
        if tag in tags and tag in _EXCL_TOKENS:
            return _EXCL_TOKENS[tag]

    return f"setup:{rca.get('setup','unknown')}"


# ─── confirmation enhancer ───────────────────────────────────────────────────

_ENHANCER = {
    "CROWDED_LONG":                  "Enter long only when funding_rate < +0.01% (neutral or negative)",
    "CROWDED_SHORT":                 "Enter short only when funding_rate > -0.01% (neutral or positive)",
    "OVERBOUGHT_ENTRY":              "Wait for RSI pullback below 65 before long entry",
    "OVERSOLD_ENTRY":                "Wait for RSI recovery above 35 before short entry",
    "OI_SURGE":                      "Require OI_24h < +5% before entry — no leverage buildup",
    "MTF_DIVERGENCE":                "Require MTF consensus >= 3 aligned timeframes before entry",
    "CVD_DIVERGENCE":                "Require CVD kline and trade flow same sign at entry",
    "SCORE_ANTICORRELATED_SETUP":    "Use WR-calibrated score cap for breakout/sweep (<=120)",
    "BREAKOUT_SCORE_OVERCALIBRATED": "Confirm breakout candle hold for 1+ candle before signaling",
    "SWEEP_DETECTION_LAG_RISK":      "Confirm sweep is current candle before entry",
    "SQUEEZE_MIDBAND_SCORE":         "For squeeze, enter only at score < 100 or > 140",
    "CHOCH_MISSING":                 "Wait for CHoCH on 1H — adds +12pp WR when confirmed",
    "EMA_ABSENT":                    "Require both 1H and 4H EMA bullish alignment for long",
    "WEAK_RS_BTC":                   "Require RS vs BTC > 0 before long — positive relative strength",
    "RSI_HIGH_SHORT":                "Add RSI >= 65 as entry requirement for short signals",
}


def _confirmation_enhancer(rca: dict, error_cat: str) -> str:
    tags = set(rca.get("tags", []))

    if error_cat == RISK_MGMT_ERROR:
        return "Verify ATR-based stop distance before entry (ATR x 0.65 min, ATR x 1.8 for volatile alts)"

    for tag in _TIMING_ORDER + _FILTERING_ORDER + _SIGNAL_ORDER:
        if tag in tags and tag in _ENHANCER:
            return _ENHANCER[tag]

    return "Add secondary confirmation before entry — signal quality below reliability threshold"


# ─── priority scoring ─────────────────────────────────────────────────────────

def _score_priority(rca: dict, rca_history: list) -> str:
    """
    Priority = how many LOSS trades in history share the same primary_cause
    OR at least one common diagnostic tag with this record.
    """
    primary_cause = rca.get("primary_cause", "")
    tags          = set(rca.get("tags", []))

    n = sum(
        1 for r in rca_history
        if r.get("outcome_category") == "LOSS"
        and (
            r.get("primary_cause") == primary_cause
            or bool(set(r.get("tags", [])) & tags)
        )
    )

    if n >= 20:
        return "HIGH"
    if n >= 5:
        return "MEDIUM"
    return "LOW"


# ─── main learning generation ─────────────────────────────────────────────────

def generate_learning(rca: dict, rca_history: Optional[list] = None) -> dict:
    """
    Generate full learning output for one closed trade RCA record.

    Args:
        rca:         RCA dict from rca_engine.analyze_trade()
        rca_history: full list of RCA records for priority scoring.
                     Loaded from disk if None.

    Returns:
        dict with error_category, correction_rule, exclusion_filter,
        confirmation_enhancer, priority, and trade identifiers.
    """
    if rca_history is None:
        try:
            rca_history = json.loads(RCA_PATH.read_text(encoding="utf-8"))
        except Exception:
            rca_history = []

    error_cat = classify_error(rca)
    rule      = _correction_rule_text(rca, error_cat)
    excl      = _exclusion_filter(rca, error_cat)
    enhancer  = _confirmation_enhancer(rca, error_cat)
    priority  = _score_priority(rca, rca_history) if error_cat != WIN_CATEGORY else "LOW"

    return {
        "symbol":                rca.get("symbol", ""),
        "run_ts":                rca.get("run_ts", ""),
        "setup":                 rca.get("setup", ""),
        "direction":             rca.get("direction", ""),
        "outcome":               rca.get("outcome", ""),
        "outcome_category":      rca.get("outcome_category", ""),
        "tags":                  rca.get("tags", []),
        "primary_cause":         rca.get("primary_cause", ""),
        "error_category":        error_cat,
        "correction_rule":       rule,
        "exclusion_filter":      excl,
        "confirmation_enhancer": enhancer,
        "priority":              priority,
    }


# ─── TLDB write-back ─────────────────────────────────────────────────────────

def _already_exists(text: str, db: dict, field: str, key: str) -> bool:
    """Return True if an entry with identical text already exists in TLDB field."""
    return any(
        e.get(key, "").strip() == text.strip()
        for e in db.get(field, [])
    )


def process_and_store(rca: dict, rca_history: Optional[list] = None,
                      db_path: Path = DB_PATH) -> Optional[dict]:
    """
    Generate learning for one RCA record and write new entries to TLDB.

    Only processes LOSS and FLAT outcomes.
    Deduplicates: identical condition text is never added twice.

    Returns:
        The learning dict (even if no new entries were written),
        or None if the trade was a WIN.
    """
    learning = generate_learning(rca, rca_history)

    if learning["error_category"] == WIN_CATEGORY:
        return None

    from trade_learnings_db import (
        load_db,
        add_correction_rule,
        add_prohibited_condition,
        add_confirmation_filter,
    )

    db = load_db(db_path)

    rule = learning["correction_rule"]
    if rule and not _already_exists(rule, db, "active_correction_rules", "rule"):
        try:
            add_correction_rule(
                rule,
                priority=learning["priority"],
                source="learning_generator",
                path=db_path,
            )
        except Exception:
            pass

    excl = learning["exclusion_filter"]
    if excl and learning["error_category"] in (FILTERING_ERROR, SIGNAL_ERROR):
        if not _already_exists(excl, db, "prohibited_entry_conditions", "condition"):
            try:
                add_prohibited_condition(
                    excl,
                    reason=learning["primary_cause"][:100],
                    source="learning_generator",
                    path=db_path,
                )
            except Exception:
                pass

    enhancer = learning["confirmation_enhancer"]
    if enhancer and not _already_exists(enhancer, db, "confirmation_filters", "filter"):
        try:
            add_confirmation_filter(
                enhancer,
                description=(
                    f"Prevents {learning['error_category']} — "
                    f"{learning['symbol']} {learning['setup']}"
                ),
                source="learning_generator",
                path=db_path,
            )
        except Exception:
            pass

    return learning


# ─── batch processing ─────────────────────────────────────────────────────────

def process_all(
    rca_path: Path = RCA_PATH,
    db_path: Path = DB_PATH,
    symbol_filter: Optional[str] = None,
    dry_run: bool = False,
) -> list:
    """
    Process all loss/flat RCA records and optionally write to TLDB.
    Returns list of learning dicts.
    """
    try:
        rca_history = json.loads(rca_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    learnings = []
    for rca in rca_history:
        if symbol_filter and rca.get("symbol") != symbol_filter:
            continue
        if rca.get("outcome_category") not in ("LOSS", "FLAT"):
            continue
        if dry_run:
            learnings.append(generate_learning(rca, rca_history))
        else:
            result = process_and_store(rca, rca_history, db_path)
            if result:
                learnings.append(result)

    return learnings


# ─── CLI ─────────────────────────────────────────────────────────────────────

_CAT_ICON = {
    SIGNAL_ERROR:    "[SIG]",
    FILTERING_ERROR: "[FLT]",
    TIMING_ERROR:    "[TMG]",
    RISK_MGMT_ERROR: "[RMG]",
    WIN_CATEGORY:    "[WIN]",
}


def _print_learning(l: dict):
    icon = _CAT_ICON.get(l["error_category"], "[???]")
    print(f"\n{icon} {l['symbol']:<14} {l['setup']:<16} {l['direction']:<6} "
          f"outcome={l['outcome']}  priority={l['priority']}")
    print(f"  Category : {l['error_category']}")
    print(f"  Rule     : {l['correction_rule']}")
    print(f"  Exclusion: {l['exclusion_filter']}")
    print(f"  Enhancer : {l['confirmation_enhancer']}")
    if l.get("tags"):
        print(f"  Tags     : {', '.join(l['tags'])}")


def main():
    ap = argparse.ArgumentParser(
        description="Per-trade error diagnosis and learning output generator"
    )
    ap.add_argument("--symbol",  default=None, help="Filter to one symbol")
    ap.add_argument("--dry-run", action="store_true",
                    help="Generate outputs without writing to TLDB")
    ap.add_argument("--report",  action="store_true",
                    help="Show last 20 loss learnings (no writes)")
    ap.add_argument("--rca",     default=str(RCA_PATH), help="Override RCA results path")
    ap.add_argument("--db",      default=str(DB_PATH),  help="Override TLDB path")
    args = ap.parse_args()

    rca_path = Path(args.rca)
    db_path  = Path(args.db)

    if args.report:
        try:
            rca_history = json.loads(rca_path.read_text(encoding="utf-8"))
        except Exception:
            print("No RCA data found.")
            return
        losses = [r for r in rca_history if r.get("outcome_category") == "LOSS"][-20:]
        for rca in losses:
            _print_learning(generate_learning(rca, rca_history))
        print(f"\nShowing last {len(losses)} losses (read-only).")
        return

    learnings = process_all(rca_path, db_path, symbol_filter=args.symbol,
                            dry_run=args.dry_run)

    for l in learnings:
        _print_learning(l)

    mode = "dry-run" if args.dry_run else "written to TLDB"
    print(f"\nProcessed {len(learnings)} LOSS/FLAT trades ({mode}).")


if __name__ == "__main__":
    main()

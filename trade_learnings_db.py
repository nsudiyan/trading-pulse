"""
trade_learnings_db.py — TRADE_LEARNINGS_DB persistent cumulative knowledge base.

Stores and manages cumulative learning from all resolved trades:
  - Losing Patterns    (conditions → frequency, win_rate)
  - Winning Patterns   (conditions → frequency, win_rate)
  - Active Correction Rules  (priority: HIGH / MEDIUM / LOW)
  - Prohibited Entry Conditions
  - Confirmation Filters
  - Statistics (win rate, avg R:R by pattern)

Storage:
  outcomes/trade_learnings_db.json          — live DB (read/write)
  outcomes/trade_learnings_db_history.jsonl — append-only audit log

Screener read interface (import-safe):
  from trade_learnings_db import (
      get_prohibited_conditions,
      get_active_correction_rules,
      get_confirmation_filters,
      get_statistics,
  )

CLI:
  python3 trade_learnings_db.py --build              # rebuild from engine outputs
  python3 trade_learnings_db.py --rules              # list correction rules
  python3 trade_learnings_db.py --prohibited         # list prohibited conditions
  python3 trade_learnings_db.py --filters            # list confirmation filters
  python3 trade_learnings_db.py --stats              # show statistics
  python3 trade_learnings_db.py --history            # show last 20 audit log entries
  python3 trade_learnings_db.py --add-rule "TEXT" --priority HIGH
  python3 trade_learnings_db.py --add-prohibited "COND" --reason "WHY"
  python3 trade_learnings_db.py --add-filter "FILTER" --desc "DESCRIPTION"
  python3 trade_learnings_db.py --remove-rule CR-001
  python3 trade_learnings_db.py --remove-prohibited PEC-001
  python3 trade_learnings_db.py --remove-filter CF-001
"""

import json
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE_DIR      = Path(__file__).parent
DB_PATH       = BASE_DIR / "outcomes" / "trade_learnings_db.json"
HISTORY_PATH  = BASE_DIR / "outcomes" / "trade_learnings_db_history.jsonl"
RCA_PATH      = BASE_DIR / "outcomes" / "rca_results.json"
PATTERNS_PATH = BASE_DIR / "outcomes" / "pattern_report.json"

SCHEMA_VERSION = 1

# Auto-derive thresholds: anti-pattern must have delta_pp ≤ this to become a rule/prohibited condition
_PROHIBITED_DELTA   = -12.0
_CONFIRMATION_DELTA = +12.0
_MIN_N_AUTO         = 20
_MIN_CONF_AUTO      = {"high", "medium"}


# ─── helpers ─────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _short_id(prefix: str, existing_ids: set) -> str:
    n = 1
    while True:
        candidate = f"{prefix}-{n:03d}"
        if candidate not in existing_ids:
            return candidate
        n += 1


# ─── DB I/O ──────────────────────────────────────────────────────────────────

def _empty_db() -> dict:
    return {
        "schema_version":              SCHEMA_VERSION,
        "db_version":                  0,
        "generated_at":                _now(),
        "last_updated":                _now(),
        "losing_patterns":             [],
        "winning_patterns":            [],
        "active_correction_rules":     [],
        "prohibited_entry_conditions": [],
        "confirmation_filters":        [],
        "statistics":                  {},
    }


def load_db(path: Path = DB_PATH) -> dict:
    """Load the knowledge base from disk. Returns empty DB if missing or corrupt."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_db()


def save_db(db: dict, path: Path = DB_PATH, change_summary: str = ""):
    """Write DB to disk and append an audit-log entry."""
    db["last_updated"] = _now()
    db["db_version"]   = db.get("db_version", 0) + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(db, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    _append_history(db["db_version"], change_summary)


def _append_history(version: int, summary: str):
    entry = {"ts": _now(), "db_version": version, "summary": summary}
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# ─── SCREENER READ INTERFACE ─────────────────────────────────────────────────

def get_active_correction_rules(path: Path = DB_PATH) -> list:
    """Return active correction rules, HIGH priority first."""
    db = load_db(path)
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    return sorted(
        db.get("active_correction_rules", []),
        key=lambda r: order.get(r.get("priority", "LOW"), 2),
    )


def get_prohibited_conditions(path: Path = DB_PATH) -> list:
    """Return list of prohibited entry conditions."""
    return load_db(path).get("prohibited_entry_conditions", [])


def get_confirmation_filters(path: Path = DB_PATH) -> list:
    """Return list of confirmation filters."""
    return load_db(path).get("confirmation_filters", [])


def get_statistics(path: Path = DB_PATH) -> dict:
    """Return aggregate statistics from the knowledge base."""
    return load_db(path).get("statistics", {})


# ─── CRUD ────────────────────────────────────────────────────────────────────

def add_correction_rule(
    rule: str,
    priority: str = "MEDIUM",
    source: str = "manual",
    path: Path = DB_PATH,
) -> str:
    """Add a correction rule. Returns new rule ID."""
    if priority not in ("HIGH", "MEDIUM", "LOW"):
        raise ValueError(f"Invalid priority '{priority}'. Must be HIGH, MEDIUM, or LOW.")
    db = load_db(path)
    existing_ids = {r["id"] for r in db["active_correction_rules"]}
    rule_id = _short_id("CR", existing_ids)
    db["active_correction_rules"].append({
        "id":         rule_id,
        "rule":       rule,
        "priority":   priority,
        "source":     source,
        "created_at": _now(),
    })
    save_db(db, path, f"add_correction_rule {rule_id}: {rule[:60]}")
    return rule_id


def add_prohibited_condition(
    condition: str,
    reason: str = "",
    source: str = "manual",
    path: Path = DB_PATH,
) -> str:
    """Add a prohibited entry condition. Returns new condition ID."""
    db = load_db(path)
    existing_ids = {r["id"] for r in db["prohibited_entry_conditions"]}
    cond_id = _short_id("PEC", existing_ids)
    db["prohibited_entry_conditions"].append({
        "id":         cond_id,
        "condition":  condition,
        "reason":     reason,
        "source":     source,
        "created_at": _now(),
    })
    save_db(db, path, f"add_prohibited_condition {cond_id}: {condition[:60]}")
    return cond_id


def add_confirmation_filter(
    filter_desc: str,
    description: str = "",
    source: str = "manual",
    path: Path = DB_PATH,
) -> str:
    """Add a confirmation filter. Returns new filter ID."""
    db = load_db(path)
    existing_ids = {r["id"] for r in db["confirmation_filters"]}
    filt_id = _short_id("CF", existing_ids)
    db["confirmation_filters"].append({
        "id":          filt_id,
        "filter":      filter_desc,
        "description": description,
        "source":      source,
        "created_at":  _now(),
    })
    save_db(db, path, f"add_confirmation_filter {filt_id}: {filter_desc[:60]}")
    return filt_id


def _remove_from_list(db: dict, key: str, item_id: str) -> bool:
    before = len(db[key])
    db[key] = [r for r in db[key] if r["id"] != item_id]
    return len(db[key]) < before


def remove_correction_rule(rule_id: str, path: Path = DB_PATH) -> bool:
    db = load_db(path)
    removed = _remove_from_list(db, "active_correction_rules", rule_id)
    if removed:
        save_db(db, path, f"remove_correction_rule {rule_id}")
    return removed


def remove_prohibited_condition(cond_id: str, path: Path = DB_PATH) -> bool:
    db = load_db(path)
    removed = _remove_from_list(db, "prohibited_entry_conditions", cond_id)
    if removed:
        save_db(db, path, f"remove_prohibited_condition {cond_id}")
    return removed


def remove_confirmation_filter(filt_id: str, path: Path = DB_PATH) -> bool:
    db = load_db(path)
    removed = _remove_from_list(db, "confirmation_filters", filt_id)
    if removed:
        save_db(db, path, f"remove_confirmation_filter {filt_id}")
    return removed


# ─── STATISTICS ──────────────────────────────────────────────────────────────

def _compute_statistics(rca_list: list, patterns: dict) -> dict:
    if not rca_list:
        return {}

    wins   = [r for r in rca_list if r.get("outcome_category") == "WIN"]
    losses = [r for r in rca_list if r.get("outcome_category") == "LOSS"]
    total_decided = len(wins) + len(losses)

    # Per-setup aggregation
    by_setup: dict = {}
    for r in rca_list:
        setup = r.get("setup", "unknown")
        if setup not in by_setup:
            by_setup[setup] = {"wins": 0, "losses": 0, "rr_sum": 0.0, "rr_n": 0}
        cat = r.get("outcome_category")
        if cat == "WIN":
            by_setup[setup]["wins"] += 1
        elif cat == "LOSS":
            by_setup[setup]["losses"] += 1
        rr = r.get("rr_ratio")
        if rr:
            by_setup[setup]["rr_sum"] += float(rr)
            by_setup[setup]["rr_n"]   += 1

    pattern_stats: dict = {}
    for setup, s in by_setup.items():
        decided = s["wins"] + s["losses"]
        pattern_stats[setup] = {
            "win_rate": round(s["wins"] / decided, 3) if decided else 0.0,
            "n":        decided,
            "avg_rr":   round(s["rr_sum"] / s["rr_n"], 2) if s["rr_n"] else None,
        }

    # Tag frequency across wins and losses
    win_tags:  dict = {}
    loss_tags: dict = {}
    for r in wins:
        for t in r.get("tags", []):
            win_tags[t] = win_tags.get(t, 0) + 1
    for r in losses:
        for t in r.get("tags", []):
            loss_tags[t] = loss_tags.get(t, 0) + 1

    # Merge pattern-engine setup_profiles for richer per-pattern stats
    # (these cover 2150+ trades vs the thinner rca_list)
    pe_profiles = patterns.get("setup_profiles", {})
    for setup, prof in pe_profiles.items():
        if setup not in pattern_stats:
            pattern_stats[setup] = {}
        pattern_stats[setup].update({
            "pe_win_rate":   prof.get("win_rate"),
            "pe_n":          prof.get("n"),
            "pe_ci_low":     prof.get("ci_low"),
            "pe_ci_high":    prof.get("ci_high"),
        })

    return {
        "as_of":                       _now(),
        "n_trades":                    len(rca_list),
        "n_wins":                      len(wins),
        "n_losses":                    len(losses),
        "overall_wr":                  round(len(wins) / total_decided, 3) if total_decided else 0.0,
        "baseline_wr_pattern_engine":  patterns.get("baseline_wr"),
        "n_total_pattern_engine":      patterns.get("n_total"),
        "by_pattern":                  pattern_stats,
        "top_win_tags":                sorted(win_tags.items(),  key=lambda x: -x[1])[:12],
        "top_loss_tags":               sorted(loss_tags.items(), key=lambda x: -x[1])[:12],
    }


# ─── BUILD FROM ENGINES ──────────────────────────────────────────────────────

def build_from_engines(
    rca_path:        Path = RCA_PATH,
    patterns_path:   Path = PATTERNS_PATH,
    db_path:         Path = DB_PATH,
    preserve_manual: bool = True,
) -> dict:
    """
    Rebuild TRADE_LEARNINGS_DB from rca_engine + pattern_engine outputs.

    - preserve_manual=True (default): manually-added rules/conditions/filters are kept.
      Auto-derived entries from the previous build are replaced.
    - preserve_manual=False: full reset (use with --no-preserve).

    Auto-derivation rules:
      anti-patterns  (conf≥medium, delta≤-12, n≥20) → correction_rules + prohibited_conditions
      success patterns (conf≥medium, delta≥+12, n≥20) → confirmation_filters
    """
    rca_list: list = []
    try:
        rca_list = json.loads(rca_path.read_text(encoding="utf-8"))
    except Exception:
        pass

    patterns: dict = {}
    try:
        patterns = json.loads(patterns_path.read_text(encoding="utf-8"))
    except Exception:
        pass

    # Preserve manual entries if requested
    existing_db = load_db(db_path) if (preserve_manual and db_path.exists()) else _empty_db()
    if preserve_manual:
        manual_rules = [r for r in existing_db.get("active_correction_rules",      []) if r.get("source") == "manual"]
        manual_pecs  = [r for r in existing_db.get("prohibited_entry_conditions",   []) if r.get("source") == "manual"]
        manual_cfs   = [r for r in existing_db.get("confirmation_filters",          []) if r.get("source") == "manual"]
    else:
        manual_rules = manual_pecs = manual_cfs = []

    # ── Losing / Winning patterns (verbatim from pattern engine) ─────────────
    losing_patterns = [
        {
            "condition":  h.get("label", ""),
            "frequency":  h.get("n", 0),
            "win_rate":   h.get("win_rate", 0.0),
            "delta_pp":   h.get("delta_pp", 0.0),
            "confidence": h.get("confidence", ""),
            "verdict":    h.get("verdict", ""),
        }
        for h in patterns.get("anti_patterns", [])
    ]
    winning_patterns = [
        {
            "condition":  h.get("label", ""),
            "frequency":  h.get("n", 0),
            "win_rate":   h.get("win_rate", 0.0),
            "delta_pp":   h.get("delta_pp", 0.0),
            "confidence": h.get("confidence", ""),
            "verdict":    h.get("verdict", ""),
        }
        for h in patterns.get("success_patterns", [])
    ]

    # ── Auto-derive correction rules ──────────────────────────────────────────
    auto_rules = []
    cr_ids = {r["id"] for r in manual_rules}
    for h in patterns.get("anti_patterns", []):
        if (
            h.get("confidence") in _MIN_CONF_AUTO
            and h.get("delta_pp", 0) <= _PROHIBITED_DELTA
            and h.get("n", 0) >= _MIN_N_AUTO
        ):
            rid = _short_id("CR", cr_ids)
            cr_ids.add(rid)
            priority = "HIGH" if h.get("delta_pp", 0) <= -20 else "MEDIUM"
            auto_rules.append({
                "id":         rid,
                "rule":       (
                    f"Penalise entry when [{h['label']}] "
                    f"— WR={h['win_rate']:.1f}% (Δ{h['delta_pp']:+.1f}pp vs baseline)"
                ),
                "priority":   priority,
                "source":     "pattern_engine",
                "created_at": _now(),
                "hypothesis": h.get("id", ""),
            })

    # ── Auto-derive prohibited entry conditions (high-confidence only) ────────
    auto_pecs = []
    pec_ids = {r["id"] for r in manual_pecs}
    for h in sorted(patterns.get("anti_patterns", []), key=lambda x: x.get("delta_pp", 0)):
        if (
            h.get("confidence") == "high"
            and h.get("delta_pp", 0) <= _PROHIBITED_DELTA
            and h.get("n", 0) >= _MIN_N_AUTO
        ):
            pid = _short_id("PEC", pec_ids)
            pec_ids.add(pid)
            auto_pecs.append({
                "id":         pid,
                "condition":  h.get("label", ""),
                "reason":     (
                    f"WR={h['win_rate']:.1f}% (Δ{h['delta_pp']:+.1f}pp), "
                    f"n={h['n']}, conf={h['confidence']}"
                ),
                "source":     "pattern_engine",
                "created_at": _now(),
                "hypothesis": h.get("id", ""),
            })

    # ── Auto-derive confirmation filters ──────────────────────────────────────
    auto_cfs = []
    cf_ids = {r["id"] for r in manual_cfs}
    for h in sorted(patterns.get("success_patterns", []), key=lambda x: -x.get("delta_pp", 0)):
        if (
            h.get("confidence") in _MIN_CONF_AUTO
            and h.get("delta_pp", 0) >= _CONFIRMATION_DELTA
            and h.get("n", 0) >= _MIN_N_AUTO
        ):
            fid = _short_id("CF", cf_ids)
            cf_ids.add(fid)
            auto_cfs.append({
                "id":          fid,
                "filter":      h.get("label", ""),
                "description": (
                    f"WR={h['win_rate']:.1f}% (Δ{h['delta_pp']:+.1f}pp), "
                    f"n={h['n']}, conf={h['confidence']}"
                ),
                "source":      "pattern_engine",
                "created_at":  _now(),
                "hypothesis":  h.get("id", ""),
            })

    stats = _compute_statistics(rca_list, patterns)

    db: dict = {
        "schema_version":              SCHEMA_VERSION,
        "db_version":                  existing_db.get("db_version", 0),
        "generated_at":                _now(),
        "last_updated":                _now(),
        "source_rca":                  str(rca_path),
        "source_patterns":             str(patterns_path),
        "losing_patterns":             losing_patterns,
        "winning_patterns":            winning_patterns,
        "active_correction_rules":     manual_rules + auto_rules,
        "prohibited_entry_conditions": manual_pecs  + auto_pecs,
        "confirmation_filters":        manual_cfs   + auto_cfs,
        "statistics":                  stats,
    }

    n_ap   = len(patterns.get("anti_patterns", []))
    n_sp   = len(patterns.get("success_patterns", []))
    save_db(
        db, db_path,
        f"build_from_engines: {len(rca_list)} RCA trades, "
        f"{n_ap} anti-patterns, {n_sp} success patterns → "
        f"{len(db['active_correction_rules'])} rules, "
        f"{len(db['prohibited_entry_conditions'])} prohibited, "
        f"{len(db['confirmation_filters'])} filters",
    )
    return db


# ─── SCREENER GATE ───────────────────────────────────────────────────────────

def _featurize_result(r: dict) -> set:
    """
    Map a score_symbol result dict to the feature token set used by pattern_engine.
    Tokens have the form "feature_name:value_token" (e.g. "setup:squeeze").
    """
    tokens: set = set()

    # setup
    tokens.add(f"setup:{r.get('setup', '?')}")

    # score_band
    s = float(r.get("score", 0) or 0)
    if s < 90:    tokens.add("score_band:score<90")
    elif s < 110: tokens.add("score_band:score90-110")
    elif s < 130: tokens.add("score_band:score110-130")
    elif s < 150: tokens.add("score_band:score130-150")
    else:         tokens.add("score_band:score≥150")

    # funding_regime — fund_% is fundingRate×100 (same scale as CSV "funding" field)
    f = float(r.get("fund_%", 0) or 0)
    if f > 0.05:    tokens.add("funding_regime:fund_extreme_pos")
    elif f > 0.01:  tokens.add("funding_regime:fund_positive")
    elif f < -0.05: tokens.add("funding_regime:fund_extreme_neg")
    elif f < -0.01: tokens.add("funding_regime:fund_negative")
    else:           tokens.add("funding_regime:fund_neutral")

    # oi_regime
    oi = float(r.get("oi24h_%", 0) or 0)
    if oi > 20:    tokens.add("oi_regime:oi_surge")
    elif oi > 5:   tokens.add("oi_regime:oi_expanding")
    elif oi < -10: tokens.add("oi_regime:oi_capitulation")
    elif oi < -2:  tokens.add("oi_regime:oi_contracting")
    else:          tokens.add("oi_regime:oi_stable")

    # rsi_zone
    rsi = float(r.get("rsi_1h", 50) or 50)
    if rsi > 70:    tokens.add("rsi_zone:rsi_overbought")
    elif rsi > 55:  tokens.add("rsi_zone:rsi_elevated")
    elif rsi < 30:  tokens.add("rsi_zone:rsi_oversold")
    elif rsi < 45:  tokens.add("rsi_zone:rsi_depressed")
    else:           tokens.add("rsi_zone:rsi_neutral")

    # mtf_bull_tier
    mb = int(r.get("mtf_b", 0) or 0)
    if mb >= 4:    tokens.add("mtf_bull_tier:mtf_bull_strong")
    elif mb >= 2:  tokens.add("mtf_bull_tier:mtf_bull_moderate")
    else:          tokens.add("mtf_bull_tier:mtf_bull_weak")

    # mtf_bear_tier
    ms = int(r.get("mtf_s", 0) or 0)
    if ms >= 4:    tokens.add("mtf_bear_tier:mtf_bear_strong")
    elif ms >= 2:  tokens.add("mtf_bear_tier:mtf_bear_moderate")
    else:          tokens.add("mtf_bear_tier:mtf_bear_weak")

    # cvd_kline_zone
    ck = float(r.get("cvd_k%", 0) or 0)
    if ck > 20:    tokens.add("cvd_kline_zone:cvdk_strong_pos")
    elif ck > 0:   tokens.add("cvd_kline_zone:cvdk_pos")
    elif ck < -20: tokens.add("cvd_kline_zone:cvdk_strong_neg")
    elif ck < 0:   tokens.add("cvd_kline_zone:cvdk_neg")
    else:          tokens.add("cvd_kline_zone:cvdk_flat")

    # cvd_trade_zone
    ct = float(r.get("cvd_t%", 0) or 0)
    if ct > 20:    tokens.add("cvd_trade_zone:cvdt_strong_pos")
    elif ct > 0:   tokens.add("cvd_trade_zone:cvdt_pos")
    elif ct < -20: tokens.add("cvd_trade_zone:cvdt_strong_neg")
    elif ct < 0:   tokens.add("cvd_trade_zone:cvdt_neg")
    else:          tokens.add("cvd_trade_zone:cvdt_flat")

    # choch_bull_1h — "bull_choch" → choch=1, anything else → choch=0
    choch_val = 1 if r.get("choch_1h") == "bull_choch" else 0
    tokens.add(f"choch_bull_1h:choch={choch_val}")

    # ema_bull_1h / ema_bull_4h — ema_1h/4h are dicts with "ema_bull" bool
    ema1h = r.get("ema_1h")
    ema1h_val = 1 if (isinstance(ema1h, dict) and ema1h.get("ema_bull")) else 0
    tokens.add(f"ema_bull_1h:ema1h={ema1h_val}")

    ema4h = r.get("ema_4h")
    ema4h_val = 1 if (isinstance(ema4h, dict) and ema4h.get("ema_bull")) else 0
    tokens.add(f"ema_bull_4h:ema4h={ema4h_val}")

    # rs_btc_zone
    rs = r.get("rs_btc")
    if rs is not None:
        rs = float(rs)
        if rs > 10:    tokens.add("rs_btc_zone:rs_btc_strong")
        elif rs > 2:   tokens.add("rs_btc_zone:rs_btc_pos")
        elif rs < -10: tokens.add("rs_btc_zone:rs_btc_weak")
        elif rs < -2:  tokens.add("rs_btc_zone:rs_btc_neg")
        else:          tokens.add("rs_btc_zone:rs_btc_neutral")

    # vwap_zone
    vd = float(r.get("vwap_dev", 0) or 0)
    if vd > 5:    tokens.add("vwap_zone:vwap_far_above")
    elif vd > 1:  tokens.add("vwap_zone:vwap_above")
    elif vd < -5: tokens.add("vwap_zone:vwap_far_below")
    elif vd < -1: tokens.add("vwap_zone:vwap_below")
    else:         tokens.add("vwap_zone:vwap_near")

    # utc_session — use current UTC hour
    try:
        hour = datetime.utcnow().hour
        if 1 <= hour < 9:     tokens.add("utc_session:session_asia")
        elif 9 <= hour < 13:  tokens.add("utc_session:session_london")
        elif 13 <= hour < 21: tokens.add("utc_session:session_ny")
        else:                  tokens.add("utc_session:session_off")
    except Exception:
        tokens.add("utc_session:session_unknown")

    return tokens


def _label_to_tokens(label: str) -> list:
    """Parse "feat:val + feat2:val2" into ["feat:val", "feat2:val2"]."""
    return [p.strip() for p in label.split(" + ") if p.strip()]


def _rule_to_label(rule_text: str) -> str:
    """Extract the condition label from an auto-generated correction rule string."""
    if "when [" in rule_text:
        return rule_text.split("when [")[1].split("]")[0]
    return rule_text


def check_tldb_gate(
    r: dict,
    prohibited: Optional[list] = None,
    rules: Optional[list] = None,
    filters: Optional[list] = None,
    path: Path = DB_PATH,
) -> dict:
    """
    Check a score_symbol result dict against TRADE_LEARNINGS_DB.

    Loads prohibited conditions, correction rules, and confirmation filters from
    disk if not supplied (callers in the screener pass pre-loaded lists to avoid I/O).

    Returns:
      {
        "prohibited_hits": [{"id", "condition", "reason"}, ...],
        "rule_hits":       [{"id", "rule", "priority"}, ...],
        "filter_hits":     [{"id", "filter", "description"}, ...],
        "is_prohibited":   bool,
        "penalty_level":   "HIGH" | "MEDIUM" | "LOW" | "NONE",
        "tokens":          set[str],    # featurized token set (for debugging)
      }
    """
    if prohibited is None:
        prohibited = get_prohibited_conditions(path)
    if rules is None:
        rules = get_active_correction_rules(path)
    if filters is None:
        filters = get_confirmation_filters(path)

    tokens = _featurize_result(r)

    def _matches(label: str) -> bool:
        return all(t in tokens for t in _label_to_tokens(label))

    prohibited_hits = [
        {"id": p["id"], "condition": p["condition"], "reason": p.get("reason", "")}
        for p in prohibited
        if _matches(p.get("condition", ""))
    ]

    rule_hits = [
        {"id": rv["id"], "rule": rv["rule"], "priority": rv["priority"]}
        for rv in rules
        if _matches(_rule_to_label(rv.get("rule", "")))
    ]

    filter_hits = [
        {"id": f["id"], "filter": f["filter"], "description": f.get("description", "")}
        for f in filters
        if _matches(f.get("filter", ""))
    ]

    is_prohibited = bool(prohibited_hits)
    if rule_hits:
        prios = [rv["priority"] for rv in rule_hits]
        penalty_level = "HIGH" if "HIGH" in prios else ("MEDIUM" if "MEDIUM" in prios else "LOW")
    else:
        penalty_level = "NONE"

    return {
        "prohibited_hits": prohibited_hits,
        "rule_hits":       rule_hits,
        "filter_hits":     filter_hits,
        "is_prohibited":   is_prohibited,
        "penalty_level":   penalty_level,
        "tokens":          tokens,
    }


# ─── DISPLAY ─────────────────────────────────────────────────────────────────

def _display_rules(db: dict):
    rules = db.get("active_correction_rules", [])
    print(f"\n── Active Correction Rules ({len(rules)}) ──")
    if not rules:
        print("  (none)")
        return
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    for r in sorted(rules, key=lambda x: order.get(x.get("priority", "LOW"), 2)):
        print(f"  [{r['id']}] [{r['priority']:<6}] {r['rule']}")
        print(f"             source={r.get('source','')}  created={r.get('created_at','')[:10]}")


def _display_prohibited(db: dict):
    pecs = db.get("prohibited_entry_conditions", [])
    print(f"\n── Prohibited Entry Conditions ({len(pecs)}) ──")
    if not pecs:
        print("  (none)")
        return
    for p in pecs:
        print(f"  [{p['id']}] {p['condition']}")
        if p.get("reason"):
            print(f"             {p['reason']}")


def _display_filters(db: dict):
    cfs = db.get("confirmation_filters", [])
    print(f"\n── Confirmation Filters ({len(cfs)}) ──")
    if not cfs:
        print("  (none)")
        return
    for f in cfs:
        print(f"  [{f['id']}] {f['filter']}")
        if f.get("description"):
            print(f"             {f['description']}")


def _display_stats(db: dict):
    s = db.get("statistics", {})
    print(f"\n── TRADE_LEARNINGS_DB Statistics ──")
    print(f"  DB version  : {db.get('db_version', 0)}")
    print(f"  Last updated: {db.get('last_updated','')[:19]}")
    print(f"  Trades in DB: {s.get('n_trades', 0)}")
    wr = s.get("overall_wr", 0)
    print(f"  Overall WR  : {wr:.1%}  (W:{s.get('n_wins',0)} L:{s.get('n_losses',0)})")
    bwr  = s.get("baseline_wr_pattern_engine")
    pe_n = s.get("n_total_pattern_engine")
    if bwr is not None:
        n_note = f"  (n={pe_n})" if pe_n else ""
        print(f"  Pattern-engine baseline WR: {bwr}%{n_note}")
    print(f"  Losing patterns    : {len(db.get('losing_patterns', []))}")
    print(f"  Winning patterns   : {len(db.get('winning_patterns', []))}")
    print(f"  Correction rules   : {len(db.get('active_correction_rules', []))}")
    print(f"  Prohibited conds   : {len(db.get('prohibited_entry_conditions', []))}")
    print(f"  Confirmation fltrs : {len(db.get('confirmation_filters', []))}")
    bp = s.get("by_pattern", {})
    if bp:
        print("\n  Per-setup WR (pe_n = pattern-engine sample):")
        key_fn = lambda x: -(x[1].get("pe_win_rate") or x[1].get("win_rate") or 0)
        for setup, st in sorted(bp.items(), key=key_fn):
            rr_note = f"  avg_rr={st['avg_rr']:.2f}" if st.get("avg_rr") else ""
            pe_note = ""
            if st.get("pe_win_rate") is not None:
                pe_note = f"  [pe WR={st['pe_win_rate']:.1f}% n={st.get('pe_n','')}]"
            print(f"    {setup:<18} WR={st.get('win_rate', 0):.1%}  n={st.get('n', 0)}{rr_note}{pe_note}")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="TRADE_LEARNINGS_DB — cumulative trading knowledge base"
    )
    ap.add_argument("--build",            action="store_true", help="Rebuild DB from engine outputs")
    ap.add_argument("--no-preserve",      action="store_true", help="Discard manual entries on rebuild")
    ap.add_argument("--rules",            action="store_true", help="List correction rules")
    ap.add_argument("--prohibited",       action="store_true", help="List prohibited entry conditions")
    ap.add_argument("--filters",          action="store_true", help="List confirmation filters")
    ap.add_argument("--stats",            action="store_true", help="Show statistics")
    ap.add_argument("--history",          action="store_true", help="Show last 20 audit log entries")
    ap.add_argument("--add-rule",         metavar="RULE",      help="Add a correction rule")
    ap.add_argument("--priority",         default="MEDIUM",    choices=["HIGH", "MEDIUM", "LOW"])
    ap.add_argument("--add-prohibited",   metavar="COND",      help="Add a prohibited entry condition")
    ap.add_argument("--reason",           default="",          help="Reason (for --add-prohibited)")
    ap.add_argument("--add-filter",       metavar="FILTER",    help="Add a confirmation filter")
    ap.add_argument("--desc",             default="",          help="Description (for --add-filter)")
    ap.add_argument("--remove-rule",      metavar="ID",        help="Remove correction rule by ID")
    ap.add_argument("--remove-prohibited",metavar="ID",        help="Remove prohibited condition by ID")
    ap.add_argument("--remove-filter",    metavar="ID",        help="Remove confirmation filter by ID")
    ap.add_argument("--db",               default=str(DB_PATH),help="Override DB path")
    args = ap.parse_args()

    db_path = Path(args.db)

    if args.build:
        print("Building TRADE_LEARNINGS_DB …")
        db = build_from_engines(
            db_path=db_path,
            preserve_manual=not args.no_preserve,
        )
        print(
            f"Done. DB v{db['db_version']}  |  "
            f"{len(db['active_correction_rules'])} rules  |  "
            f"{len(db['prohibited_entry_conditions'])} prohibited  |  "
            f"{len(db['confirmation_filters'])} filters"
        )
        _display_stats(db)
        return

    if args.add_rule:
        rid = add_correction_rule(args.add_rule, priority=args.priority, path=db_path)
        print(f"Added correction rule {rid}")
        return

    if args.add_prohibited:
        pid = add_prohibited_condition(args.add_prohibited, reason=args.reason, path=db_path)
        print(f"Added prohibited condition {pid}")
        return

    if args.add_filter:
        fid = add_confirmation_filter(args.add_filter, description=args.desc, path=db_path)
        print(f"Added confirmation filter {fid}")
        return

    if args.remove_rule:
        ok = remove_correction_rule(args.remove_rule, path=db_path)
        print(f"{'Removed' if ok else 'Not found'}: {args.remove_rule}")
        return

    if args.remove_prohibited:
        ok = remove_prohibited_condition(args.remove_prohibited, path=db_path)
        print(f"{'Removed' if ok else 'Not found'}: {args.remove_prohibited}")
        return

    if args.remove_filter:
        ok = remove_confirmation_filter(args.remove_filter, path=db_path)
        print(f"{'Removed' if ok else 'Not found'}: {args.remove_filter}")
        return

    if args.history:
        try:
            lines = HISTORY_PATH.read_text(encoding="utf-8").strip().splitlines()
            for line in lines[-20:]:
                e = json.loads(line)
                print(f"  v{e['db_version']:>4}  {e['ts'][:19]}  {e['summary']}")
        except Exception as exc:
            print(f"No history yet: {exc}")
        return

    # Default: show overview
    db = load_db(db_path)
    show_all = not any([args.rules, args.prohibited, args.filters])
    if args.rules   or show_all: _display_rules(db)
    if args.prohibited or show_all: _display_prohibited(db)
    if args.filters  or show_all: _display_filters(db)
    if args.stats    or show_all: _display_stats(db)


if __name__ == "__main__":
    main()

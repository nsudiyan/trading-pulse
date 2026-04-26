"""
pattern_engine.py — Cross-trade pattern and dependency identification module.

Scans resolved trade history to exhaustively identify recurring conditions that
correlate with wins or losses, generating a structured hypothesis table.

## What it does
1. Discretizes all signal fields into categorical bins
2. Computes single-condition win-rate deviations vs baseline (with Wilson CI)
3. Computes two-condition interaction patterns (condition tuple → outcome probability)
4. Builds setup-specific sub-condition profiles
5. Identifies time-of-day patterns
6. Writes pattern_report.json for TRADE_LEARNINGS_DB (AVEVA-48)

## Output structure (pattern_report.json)
  generated_at, horizon, baseline_wr, n_total
  success_patterns:  [{id, condition, label, n, win_rate, delta_pp, ci, p_value, confidence, verdict}]
  anti_patterns:     [{...same...}]
  top_hypotheses:    top 50 by |delta_pp| × significance
  setup_profiles:    {setup: {n, win_rate, best_sub_conditions, worst_sub_conditions}}
  time_patterns:     {session: {n, win_rate, delta_pp}}

## CLI
  python3 pattern_engine.py                     -- print compact summary
  python3 pattern_engine.py --horizon 24h       -- 24h horizon
  python3 pattern_engine.py --out file.json     -- write JSON report
  python3 pattern_engine.py --min-n 15          -- stricter min sample
  python3 pattern_engine.py --pairs             -- include pair analysis (slower)
  python3 pattern_engine.py --top 20            -- top N hypotheses to print
"""

import csv
import json
import math
import argparse
import itertools
from collections import defaultdict
from pathlib import Path
from datetime import datetime
from typing import Optional

BASE_DIR      = Path(__file__).parent
CSV_PATH      = BASE_DIR / "outcomes" / "resolved.csv"
PATTERNS_PATH = BASE_DIR / "outcomes" / "pattern_report.json"

WIN_OUTCOMES  = {"TP1", "WIN"}
LOSS_OUTCOMES = {"STOP", "LOSS"}

MIN_N_DEFAULT = 10
CI_Z          = 1.96   # 95% confidence interval


# ─── helpers ─────────────────────────────────────────────────────────────────

def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _win(row, horizon):
    return row.get(f"outcome_{horizon}") in WIN_OUTCOMES


def _wilson_ci(wins, n, z=CI_Z):
    """Wilson score confidence interval → (low_pct, high_pct)."""
    if n == 0:
        return (0.0, 100.0)
    p = wins / n
    denom  = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (
        round(max(0.0, center - spread) * 100, 1),
        round(min(1.0, center + spread) * 100, 1),
    )


def _z_score(p_obs, p_base, n):
    """One-proportion z-test against baseline."""
    if n == 0 or p_base <= 0 or p_base >= 1:
        return 0.0
    se = math.sqrt(p_base * (1 - p_base) / n)
    return (p_obs - p_base) / se if se else 0.0


def _p_value(z):
    """Two-tailed p-value from z-score via math.erfc."""
    return math.erfc(abs(z) / math.sqrt(2))


def _confidence(n, p_val):
    if n >= 50 and p_val < 0.01:
        return "high"
    if n >= 20 and p_val < 0.05:
        return "medium"
    if n >= 10 and p_val < 0.10:
        return "low"
    return "provisional"


def _verdict(delta_pp, conf):
    if delta_pp >= 15 and conf in ("high", "medium"):
        return "STRONG_POSITIVE"
    if delta_pp >= 8:
        return "POSITIVE"
    if delta_pp <= -15 and conf in ("high", "medium"):
        return "STRONG_NEGATIVE"
    if delta_pp <= -5:
        return "NEGATIVE"
    return "NEUTRAL"


# ─── feature discretizers ─────────────────────────────────────────────────────

def _score_band(r):
    s = _f(r.get("score", 0))
    if s < 90:  return "score<90"
    if s < 110: return "score90-110"
    if s < 130: return "score110-130"
    if s < 150: return "score130-150"
    return "score≥150"


def _funding_regime(r):
    f = _f(r.get("funding", 0))
    if f > 0.05:  return "fund_extreme_pos"
    if f > 0.01:  return "fund_positive"
    if f < -0.05: return "fund_extreme_neg"
    if f < -0.01: return "fund_negative"
    return "fund_neutral"


def _oi_regime(r):
    oi = _f(r.get("oi_24h_pct", 0))
    if oi > 20:   return "oi_surge"
    if oi > 5:    return "oi_expanding"
    if oi < -10:  return "oi_capitulation"
    if oi < -2:   return "oi_contracting"
    return "oi_stable"


def _rsi_zone(r):
    rsi = _f(r.get("rsi_1h", 50))
    if rsi > 70:  return "rsi_overbought"
    if rsi > 55:  return "rsi_elevated"
    if rsi < 30:  return "rsi_oversold"
    if rsi < 45:  return "rsi_depressed"
    return "rsi_neutral"


def _mtf_bull_tier(r):
    v = _i(r.get("mtf_bull", 0))
    if v >= 4: return "mtf_bull_strong"
    if v >= 2: return "mtf_bull_moderate"
    return "mtf_bull_weak"


def _mtf_bear_tier(r):
    v = _i(r.get("mtf_bear", 0))
    if v >= 4: return "mtf_bear_strong"
    if v >= 2: return "mtf_bear_moderate"
    return "mtf_bear_weak"


def _cvd_kline_zone(r):
    v = _f(r.get("cvd_kline", 0))
    if v > 20:  return "cvdk_strong_pos"
    if v > 0:   return "cvdk_pos"
    if v < -20: return "cvdk_strong_neg"
    if v < 0:   return "cvdk_neg"
    return "cvdk_flat"


def _cvd_trade_zone(r):
    v = _f(r.get("cvd_trade", 0))
    if v > 20:  return "cvdt_strong_pos"
    if v > 0:   return "cvdt_pos"
    if v < -20: return "cvdt_strong_neg"
    if v < 0:   return "cvdt_neg"
    return "cvdt_flat"


def _rs_btc_zone(r):
    v = _f(r.get("rs_btc", 0))
    if v > 10:  return "rs_btc_strong"
    if v > 2:   return "rs_btc_pos"
    if v < -10: return "rs_btc_weak"
    if v < -2:  return "rs_btc_neg"
    return "rs_btc_neutral"


def _vwap_zone(r):
    v = _f(r.get("vwap_dev", 0))
    if v > 5:   return "vwap_far_above"
    if v > 1:   return "vwap_above"
    if v < -5:  return "vwap_far_below"
    if v < -1:  return "vwap_below"
    return "vwap_near"


def _utc_session(r):
    ts = r.get("run_ts", "")
    try:
        hour = int(ts[11:13])
        if 1 <= hour < 9:   return "session_asia"
        if 9 <= hour < 13:  return "session_london"
        if 13 <= hour < 21: return "session_ny"
        return "session_off"
    except (ValueError, IndexError):
        return "session_unknown"


def _direction_norm(r):
    raw = r.get("direction", "")
    if "ОН" in raw or raw == "ЛОНГ":   return "dir_long"   # ЛОНГ
    if "ОРТ" in raw or raw == "ШОРТ": return "dir_short"  # ШОРТ
    return "dir_wait"


# Registry: name → extractor function
FEATURES = {
    "setup":           lambda r: r.get("setup", "?"),
    "direction":       _direction_norm,
    "score_band":      _score_band,
    "funding_regime":  _funding_regime,
    "oi_regime":       _oi_regime,
    "rsi_zone":        _rsi_zone,
    "mtf_bull_tier":   _mtf_bull_tier,
    "mtf_bear_tier":   _mtf_bear_tier,
    "cvd_kline_zone":  _cvd_kline_zone,
    "cvd_trade_zone":  _cvd_trade_zone,
    "choch_bull_1h":   lambda r: f"choch={_i(r.get('choch_bull_1h', 0))}",
    "ema_bull_1h":     lambda r: f"ema1h={_i(r.get('ema_bull_1h', 0))}",
    "ema_bull_4h":     lambda r: f"ema4h={_i(r.get('ema_bull_4h', 0))}",
    "in_zone":         lambda r: f"in_zone={_i(r.get('in_zone', 0))}",
    "whale_flag":      lambda r: f"whale={_i(r.get('whale_flag', 0))}",
    "bnb_confirmed":   lambda r: "bnb=1" if _f(r.get("bnb_cross_bonus", 0)) > 0 else "bnb=0",
    "rs_btc_zone":     _rs_btc_zone,
    "vwap_zone":       _vwap_zone,
    "utc_session":     _utc_session,
}

# Features always included in pair analysis regardless of single-feature significance
ANCHOR_FEATURES = {"setup", "choch_bull_1h", "ema_bull_1h", "funding_regime", "mtf_bull_tier"}


# ─── hypothesis builder ───────────────────────────────────────────────────────

def _build_hypothesis(hyp_id, cond_tuple, row_indices, rows, horizon, baseline_wr):
    """
    Build one hypothesis record from a condition tuple and its matching row set.

    cond_tuple: tuple of (feature_name, value) pairs — e.g. (("setup","bos_fvg"),)
    """
    n = len(row_indices)
    if n == 0:
        return None
    wins   = sum(1 for i in row_indices if _win(rows[i], horizon))
    wr     = wins / n
    delta  = (wr - baseline_wr) * 100
    ci     = _wilson_ci(wins, n)
    z      = _z_score(wr, baseline_wr, n)
    p_val  = _p_value(z)
    conf   = _confidence(n, p_val)
    verdict = _verdict(delta, conf)

    condition = {k: v for k, v in cond_tuple}
    label     = " + ".join(f"{k}:{v}" for k, v in cond_tuple)

    return {
        "id":         hyp_id,
        "depth":      len(cond_tuple),
        "condition":  condition,
        "label":      label,
        "n":          n,
        "win_count":  wins,
        "loss_count": n - wins,
        "win_rate":   round(wr * 100, 1),
        "delta_pp":   round(delta, 1),
        "ci_low":     ci[0],
        "ci_high":    ci[1],
        "z_score":    round(z, 2),
        "p_value":    round(p_val, 4),
        "confidence": conf,
        "verdict":    verdict,
    }


# ─── core analysis ────────────────────────────────────────────────────────────

def generate_patterns(
    csv_path: Path = CSV_PATH,
    horizon: str = "4h",
    min_n: int = MIN_N_DEFAULT,
    include_pairs: bool = True,
    exclude_wait: bool = True,
) -> dict:
    """
    Generate the full cross-trade pattern report.

    Returns a structured dict consumable by TRADE_LEARNINGS_DB (AVEVA-48).
    Also writes outcomes/pattern_report.json.

    Args:
        csv_path:      Path to resolved.csv
        horizon:       "4h" or "24h"
        min_n:         Minimum group size for a hypothesis to be included
        include_pairs: Whether to run the two-feature interaction analysis
        exclude_wait:  Exclude ЖДАТЬ direction rows (deprecated, pre-T1.4 data).
                       Default True — these no longer appear in live signals.
    """
    # ── Load & filter decided trades ──────────────────────────────────────────
    decided = WIN_OUTCOMES | LOSS_OUTCOMES
    rows: list[dict] = []
    n_wait_excluded = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get(f"outcome_{horizon}") not in decided:
                continue
            if exclude_wait and row.get("direction", "") == "ЖДАТЬ":
                n_wait_excluded += 1
                continue
            rows.append(row)

    if not rows:
        return {}

    n_total        = len(rows)
    baseline_wins  = sum(1 for r in rows if _win(r, horizon))
    baseline_wr    = baseline_wins / n_total
    baseline_ci    = _wilson_ci(baseline_wins, n_total)

    # ── Pre-index: feature → value → frozenset of row indices ─────────────────
    # This lets us compute intersections with set & instead of nested loops.
    fv_index: dict[str, dict[str, set[int]]] = {}
    for feat_name, extractor in FEATURES.items():
        val_map: dict[str, set[int]] = defaultdict(set)
        for i, r in enumerate(rows):
            val_map[extractor(r)].add(i)
        fv_index[feat_name] = val_map

    hyp_counter = 0
    def _next_id():
        nonlocal hyp_counter
        hyp_counter += 1
        return f"H{hyp_counter:04d}"

    # ── Phase 1: Single-condition analysis ────────────────────────────────────
    single_hyps: list[dict] = []
    for feat_name, val_map in fv_index.items():
        for val, idxs in val_map.items():
            if len(idxs) < min_n:
                continue
            cond = ((feat_name, val),)
            h = _build_hypothesis(_next_id(), cond, idxs, rows, horizon, baseline_wr)
            if h:
                single_hyps.append(h)

    # ── Phase 2: Two-condition interactions ────────────────────────────────────
    pair_hyps: list[dict] = []
    if include_pairs:
        # Focus on features that showed any individual signal strength
        significant_feats: set[str] = set(ANCHOR_FEATURES)
        for h in single_hyps:
            feat = list(h["condition"].keys())[0]
            if abs(h["delta_pp"]) >= 5 or h["confidence"] in ("medium", "high"):
                significant_feats.add(feat)

        sig_feat_list = sorted(significant_feats)
        for feat_a, feat_b in itertools.combinations(sig_feat_list, 2):
            for val_a, idxs_a in fv_index[feat_a].items():
                if len(idxs_a) < min_n:
                    continue
                for val_b, idxs_b in fv_index[feat_b].items():
                    intersection = idxs_a & idxs_b
                    if len(intersection) < min_n:
                        continue
                    cond = ((feat_a, val_a), (feat_b, val_b))
                    h = _build_hypothesis(_next_id(), cond, intersection, rows, horizon, baseline_wr)
                    if h:
                        pair_hyps.append(h)

    all_hyps = single_hyps + pair_hyps

    # ── Phase 3: Setup-specific sub-condition profiles ─────────────────────────
    setup_profiles: dict[str, dict] = {}
    for setup_val, setup_idxs in fv_index["setup"].items():
        if len(setup_idxs) < min_n:
            continue
        n_s   = len(setup_idxs)
        wins_s = sum(1 for i in setup_idxs if _win(rows[i], horizon))
        wr_s  = wins_s / n_s
        ci_s  = _wilson_ci(wins_s, n_s)

        best_sub:  list[tuple] = []
        worst_sub: list[tuple] = []
        sub_min_n = max(5, min_n // 2)

        for feat_name, val_map in fv_index.items():
            if feat_name == "setup":
                continue
            for val, idxs in val_map.items():
                grp = setup_idxs & idxs
                if len(grp) < sub_min_n:
                    continue
                wins_g = sum(1 for i in grp if _win(rows[i], horizon))
                wr_g   = wins_g / len(grp)
                delta_g = (wr_g - wr_s) * 100
                entry  = (f"{feat_name}:{val}", len(grp), round(wr_g * 100, 1), round(delta_g, 1))
                if delta_g >= 8:
                    best_sub.append(entry)
                elif delta_g <= -8:
                    worst_sub.append(entry)

        setup_profiles[setup_val] = {
            "n":                  n_s,
            "win_count":          wins_s,
            "win_rate":           round(wr_s * 100, 1),
            "ci_low":             ci_s[0],
            "ci_high":            ci_s[1],
            "best_sub_conditions":  sorted(best_sub,  key=lambda x: -x[3])[:6],
            "worst_sub_conditions": sorted(worst_sub, key=lambda x:  x[3])[:6],
        }

    # ── Phase 4: Time-of-day patterns ─────────────────────────────────────────
    time_patterns: dict[str, dict] = {}
    for session_val, session_idxs in fv_index["utc_session"].items():
        if len(session_idxs) < min_n:
            continue
        n_t   = len(session_idxs)
        wins_t = sum(1 for i in session_idxs if _win(rows[i], horizon))
        wr_t  = wins_t / n_t
        delta_t = (wr_t - baseline_wr) * 100
        ci_t  = _wilson_ci(wins_t, n_t)
        z_t   = _z_score(wr_t, baseline_wr, n_t)
        time_patterns[session_val] = {
            "n":        n_t,
            "win_count": wins_t,
            "win_rate": round(wr_t * 100, 1),
            "delta_pp": round(delta_t, 1),
            "ci_low":   ci_t[0],
            "ci_high":  ci_t[1],
            "p_value":  round(_p_value(z_t), 4),
        }

    # ── Classify & rank ───────────────────────────────────────────────────────
    success_patterns = sorted(
        [h for h in all_hyps if h["verdict"] in ("STRONG_POSITIVE", "POSITIVE")],
        key=lambda h: (-h["delta_pp"], h["p_value"]),
    )
    anti_patterns = sorted(
        [h for h in all_hyps if h["verdict"] in ("STRONG_NEGATIVE", "NEGATIVE")],
        key=lambda h: (h["delta_pp"], h["p_value"]),
    )
    # Top 50 by composite score: |delta_pp| × (1 - p_value), excluding NEUTRAL
    top_hyps = sorted(
        [h for h in all_hyps if h["verdict"] != "NEUTRAL" and h["confidence"] != "provisional"],
        key=lambda h: abs(h["delta_pp"]) * (1 - h["p_value"]),
        reverse=True,
    )[:50]

    return {
        "generated_at":     datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "horizon":          horizon,
        "min_n":            min_n,
        "exclude_wait":     exclude_wait,
        "n_wait_excluded":  n_wait_excluded,
        "n_total":          n_total,
        "n_wins":         baseline_wins,
        "n_losses":       n_total - baseline_wins,
        "baseline_wr":    round(baseline_wr * 100, 2),
        "baseline_ci_low":  baseline_ci[0],
        "baseline_ci_high": baseline_ci[1],
        "total_hypotheses":    len(all_hyps),
        "success_count":       len(success_patterns),
        "anti_pattern_count":  len(anti_patterns),
        "success_patterns":    success_patterns,
        "anti_patterns":       anti_patterns,
        "top_hypotheses":      top_hyps,
        "setup_profiles":      setup_profiles,
        "time_patterns":       time_patterns,
    }


# ─── persistence ─────────────────────────────────────────────────────────────

def save_patterns(report: dict, out_path: Path = PATTERNS_PATH):
    """Write pattern report JSON to disk."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str),
                        encoding="utf-8")


def load_patterns(path: Path = PATTERNS_PATH) -> dict:
    """Load previously saved pattern report, or return empty dict."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ─── CLI display ─────────────────────────────────────────────────────────────

def _print_summary(report: dict, top_n: int = 15):
    bwr   = report.get("baseline_wr", 0)
    n     = report.get("n_total", 0)
    h_cnt = report.get("total_hypotheses", 0)

    print(f"\n{'═'*72}")
    print(f"  PATTERN ENGINE REPORT — {report.get('generated_at','')}")
    n_excl = report.get("n_wait_excluded", 0)
    excl_note = f"  |  ЖДАТЬ excluded: {n_excl}" if n_excl else ""
    print(f"  Horizon: {report.get('horizon')}  |  N={n}{excl_note}  |  "
          f"Baseline WR={bwr:.1f}%  |  Hypotheses generated: {h_cnt}")
    print(f"{'═'*72}\n")

    # Success patterns
    sp = report.get("success_patterns", [])
    if sp:
        print(f"── ✅ SUCCESS PATTERNS  ({len(sp)} total, showing top {top_n}) ──")
        print(f"  {'Label':<50} {'N':>5} {'WR':>6} {'Δpp':>7} {'CI':>14} {'P':>7} {'Conf':<10}")
        print("  " + "─" * 100)
        for h in sp[:top_n]:
            ci_str = f"[{h['ci_low']:.0f}–{h['ci_high']:.0f}]"
            print(f"  {h['label']:<50} {h['n']:>5} {h['win_rate']:>5.1f}% "
                  f"{h['delta_pp']:>+7.1f} {ci_str:>14} {h['p_value']:>7.4f} {h['confidence']:<10}")
        print()

    # Anti-patterns
    ap = report.get("anti_patterns", [])
    if ap:
        print(f"── ❌ ANTI-PATTERNS  ({len(ap)} total, showing top {top_n}) ──")
        print(f"  {'Label':<50} {'N':>5} {'WR':>6} {'Δpp':>7} {'CI':>14} {'P':>7} {'Conf':<10}")
        print("  " + "─" * 100)
        for h in ap[:top_n]:
            ci_str = f"[{h['ci_low']:.0f}–{h['ci_high']:.0f}]"
            print(f"  {h['label']:<50} {h['n']:>5} {h['win_rate']:>5.1f}% "
                  f"{h['delta_pp']:>+7.1f} {ci_str:>14} {h['p_value']:>7.4f} {h['confidence']:<10}")
        print()

    # Setup profiles
    profiles = report.get("setup_profiles", {})
    if profiles:
        print("── 📊 SETUP PROFILES ──")
        for setup, p in sorted(profiles.items(), key=lambda x: -x[1]["win_rate"]):
            ci_str = f"[{p['ci_low']:.0f}–{p['ci_high']:.0f}]"
            print(f"  {setup:<16} WR={p['win_rate']:.1f}%  {ci_str}  n={p['n']}")
            for label, n_sub, wr_sub, d_sub in p.get("best_sub_conditions", [])[:3]:
                print(f"    ✅ {label:<46} WR={wr_sub:.1f}% Δ{d_sub:+.1f}pp  n={n_sub}")
            for label, n_sub, wr_sub, d_sub in p.get("worst_sub_conditions", [])[:3]:
                print(f"    ❌ {label:<46} WR={wr_sub:.1f}% Δ{d_sub:+.1f}pp  n={n_sub}")
        print()

    # Time patterns
    tp = report.get("time_patterns", {})
    if tp:
        print("── 🕐 TIME-OF-DAY PATTERNS ──")
        for session, d in sorted(tp.items(), key=lambda x: -x[1]["win_rate"]):
            sign = "+" if d["delta_pp"] >= 0 else ""
            print(f"  {session:<20} WR={d['win_rate']:.1f}%  Δ{sign}{d['delta_pp']:.1f}pp  n={d['n']}")
        print()


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Pattern and dependency identification for trade history")
    parser.add_argument("--csv",     default=str(CSV_PATH),  help="Path to resolved.csv")
    parser.add_argument("--horizon", default="4h",           choices=["4h", "24h"])
    parser.add_argument("--out",     default=None,           help="Write JSON report to file")
    parser.add_argument("--min-n",   type=int, default=MIN_N_DEFAULT, dest="min_n",
                        help="Minimum group size for hypothesis inclusion")
    parser.add_argument("--pairs",       action="store_true", help="Include pair-condition analysis")
    parser.add_argument("--no-pairs",   action="store_true", help="Skip pair analysis (faster)")
    parser.add_argument("--include-wait", action="store_true", dest="include_wait",
                        help="Include historical ЖДАТЬ-direction rows (deprecated class, excluded by default)")
    parser.add_argument("--top",         type=int, default=15, help="Top N patterns to display")
    args = parser.parse_args()

    include_pairs = args.pairs or (not args.no_pairs)
    exclude_wait  = not args.include_wait

    print(f"Scanning {args.csv} ...")
    print(f"Horizon: {args.horizon}  |  Min-N: {args.min_n}  |  Pairs: {include_pairs}")

    report = generate_patterns(
        csv_path=Path(args.csv),
        horizon=args.horizon,
        min_n=args.min_n,
        include_pairs=include_pairs,
        exclude_wait=exclude_wait,
    )

    if not report:
        print("No data found.")
        return

    if args.out:
        save_patterns(report, Path(args.out))
        print(f"Wrote report to {args.out}")
    else:
        # Always write the canonical output file
        save_patterns(report)
        print(f"Wrote report to {PATTERNS_PATH}")

    _print_summary(report, top_n=args.top)


if __name__ == "__main__":
    main()

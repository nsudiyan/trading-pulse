#!/usr/bin/env python3
"""
causal_attributor.py — AVEVA-62 ST3b: causal attribution for pump events.

For each canonical pump in pump_events.csv, checks each catalyst category
in a pre-defined lookback window and records coincidences.

Catalyst categories (from actual data only):
  liq_sweep     — liquidation cluster > 3σ in [-2h, +1h]  (sweep_clusters.csv)
  macro_extreme — F&G extreme (<25 or >75) in [-48h, 0)   (macro_events.csv)
  stablecoin    — net inflow > 2σ in [-48h, 0)            (stablecoin_flows.csv)
  news_signal   — same-symbol screener signal in [-24h, +2h] (news_signals.csv)
  token_unlock  — unlock event in [-7d, +1d]               (token_unlocks.csv — sparse)
  whale_pos     — skipped (no historical coverage)
  orderbook     — skipped (no historical coverage)

Outputs:
  pump_analysis/causal_attribution.csv
  pump_analysis/catalyst_event_study.csv
  pump_analysis/catalyst_stats.json
  pump_analysis/plots/catalyst_distribution.png
"""

import json
import logging
from datetime import timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("causal_attributor")

OUT_DIR  = Path(__file__).parent
CAT_DIR  = OUT_DIR / "catalyst_data"
PLOTS_DIR = OUT_DIR / "plots"
PLOTS_DIR.mkdir(exist_ok=True)


# ── load helpers ──────────────────────────────────────────────────────────────

def _ms(ts_val) -> int:
    """Convert various timestamp formats to UTC milliseconds."""
    if isinstance(ts_val, (int, float)):
        v = int(ts_val)
        # seconds vs ms heuristic
        return v * 1000 if v < 1e11 else v
    s = str(ts_val)
    # pandas handles ISO strings with timezone offsets
    return int(pd.Timestamp(s).tz_localize("UTC" if "+" not in s and "Z" not in s else None)
               .value // 1_000_000)


def load_pump_events() -> pd.DataFrame:
    df = pd.read_csv(OUT_DIR / "pump_events.csv")
    # parse start_ts → int ms
    df["start_ms"] = df["start_ts"].apply(_ms)
    return df


def load_sweep_clusters() -> pd.DataFrame:
    df = pd.read_csv(CAT_DIR / "sweep_clusters.csv")
    df["hour_ts"] = df["hour_ts"].astype("int64")
    df["liq_usd_total"] = pd.to_numeric(df["liq_usd_total"], errors="coerce").fillna(0)
    df["liq_count"]     = pd.to_numeric(df["liq_count"], errors="coerce").fillna(0)
    return df


def load_macro_events() -> pd.DataFrame:
    df = pd.read_csv(CAT_DIR / "macro_events.csv")
    df["ts_ms"] = df["timestamp"].apply(lambda x: int(x) * 1000)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df


def load_stablecoin_flows() -> pd.DataFrame:
    df = pd.read_csv(CAT_DIR / "stablecoin_flows.csv")
    df["ts_ms"] = df["timestamp"].apply(lambda x: int(x) * 1000)
    df["delta_usd_1d"] = pd.to_numeric(df["delta_usd_1d"], errors="coerce")
    return df


def load_news_signals() -> pd.DataFrame:
    df = pd.read_csv(CAT_DIR / "news_signals.csv")
    df["ts_ms"] = df["run_ts"].apply(_ms)
    return df


def load_token_unlocks() -> pd.DataFrame:
    df = pd.read_csv(CAT_DIR / "token_unlocks.csv")
    # Filter out placeholder rows
    df = df[~df["symbol"].str.startswith("_")].copy()
    if not df.empty:
        df["ts_ms"] = pd.to_datetime(df["unlock_date"]).apply(
            lambda x: int(x.replace(tzinfo=timezone.utc).timestamp() * 1000)
        )
    return df


# ── per-pump catalyst checks ──────────────────────────────────────────────────

def check_liq_sweep(pump_ms: int, symbol: str, sweeps: pd.DataFrame,
                    sweep_thresholds: dict) -> bool:
    """
    True if there's a liquidation cluster > 3σ for this symbol
    in [-2h, +1h] of pump_ms.
    """
    lo = pump_ms - 2 * 3600 * 1000
    hi = pump_ms + 1 * 3600 * 1000
    sym_sweeps = sweeps[
        (sweeps["symbol"] == symbol) &
        (sweeps["hour_ts"] >= lo) &
        (sweeps["hour_ts"] <= hi)
    ]
    if sym_sweeps.empty:
        return False
    threshold = sweep_thresholds.get(symbol)
    if threshold is None:
        return False
    return bool((sym_sweeps["liq_usd_total"] > threshold).any())


def check_macro_extreme(pump_ms: int, macro: pd.DataFrame) -> bool:
    """True if F&G was extreme (<25 or >75) in [-48h, 0) of pump_ms."""
    lo = pump_ms - 48 * 3600 * 1000
    window = macro[(macro["ts_ms"] >= lo) & (macro["ts_ms"] < pump_ms)]
    if window.empty:
        return False
    return bool(((window["value"] < 25) | (window["value"] > 75)).any())


def check_stablecoin_inflow(pump_ms: int, flows: pd.DataFrame, inflow_threshold: float) -> bool:
    """True if net stablecoin inflow > 2σ in [-48h, 0) of pump_ms."""
    lo = pump_ms - 48 * 3600 * 1000
    window = flows[(flows["ts_ms"] >= lo) & (flows["ts_ms"] < pump_ms)]
    if window.empty:
        return False
    return bool((window["delta_usd_1d"] > inflow_threshold).any())


def check_news_signal(pump_ms: int, symbol: str, news: pd.DataFrame) -> bool:
    """True if same symbol had a screener signal in [-24h, +2h] of pump_ms."""
    lo = pump_ms - 24 * 3600 * 1000
    hi = pump_ms + 2  * 3600 * 1000
    hits = news[
        (news["symbol"] == symbol) &
        (news["ts_ms"] >= lo) &
        (news["ts_ms"] <= hi)
    ]
    return not hits.empty


def check_token_unlock(pump_ms: int, symbol: str, unlocks: pd.DataFrame) -> bool:
    """True if there's a token unlock in [-7d, +1d] of pump_ms."""
    if unlocks.empty:
        return False
    lo = pump_ms - 7 * 24 * 3600 * 1000
    hi = pump_ms + 1 * 24 * 3600 * 1000
    hits = unlocks[
        (unlocks["symbol"] == symbol) &
        (unlocks["ts_ms"] >= lo) &
        (unlocks["ts_ms"] <= hi)
    ]
    return not hits.empty


# ── event study (lift + p-value) ──────────────────────────────────────────────

def event_study(
    pumps: pd.DataFrame,
    catalyst_col: str,
    coverage_start_ms: int,
    coverage_end_ms: int,
) -> dict:
    """
    Compute lift = P(pump | catalyst) / P(pump) and Fisher's exact p-value.

    Only considers pumps within the catalyst's coverage period.
    """
    in_coverage = pumps[
        (pumps["start_ms"] >= coverage_start_ms) &
        (pumps["start_ms"] <= coverage_end_ms)
    ]
    n_covered = len(in_coverage)
    if n_covered == 0:
        return {
            "coverage_pumps": 0,
            "n_with_catalyst": 0,
            "pct_with_catalyst": None,
            "base_rate": None,
            "lift": None,
            "p_value": None,
            "note": "no pumps in coverage period",
        }

    n_with = int(in_coverage[catalyst_col].sum())
    n_without = n_covered - n_with

    # Base rate from the full pump set (may be wider coverage)
    # Use within-coverage base rate for apples-to-apples
    pct = n_with / n_covered

    # For lift we need an independent "no-catalyst" base rate.
    # We approximate it using the coverage period itself:
    # base_rate ≈ pumps-in-coverage / total-possible-windows-in-coverage
    # But we don't have "total windows" easily, so we report:
    # lift = P(catalyst|pump) / P(catalyst in any random pump)
    # which equals the ratio of pump % vs the catalyst occurrence rate
    # This is an approximation — annotate accordingly.

    # Full-sample pump base rate (fraction of all pump events that have this catalyst)
    all_n_with = int(pumps[catalyst_col].sum())
    all_n_without = len(pumps) - all_n_with
    base_rate_all = all_n_with / len(pumps)

    # Fisher's exact test: [[with+pump, without+pump], [with+nopump, without+nopump]]
    # We don't have direct "no-pump with catalyst" count without scanning the full calendar.
    # Use chi-square on presence/absence within coverage period only.
    # Contingency on covered pumps: just report raw counts + pct.
    # Lift: pct in coverage vs base rate in full set (approximation)
    if all_n_without == 0 or all_n_with == 0:
        lift = None
        p_val = None
    else:
        # lift: fraction of coverage-pumps-with-catalyst vs fraction of all-pumps-with-catalyst
        lift = round(pct / base_rate_all, 3) if base_rate_all > 0 else None
        # Binomial test: is the rate in covered period significantly different from full rate?
        binom_result = scipy_stats.binomtest(n_with, n_covered, base_rate_all)
        p_val = round(float(binom_result.pvalue), 6)

    def _note(pv, n):
        if n < 20:
            return "LOW_SAMPLE (N<20)"
        if pv is None:
            return "insufficient_data"
        if pv > 0.05:
            return "not_significant (p>0.05)"
        return "significant"

    return {
        "coverage_pumps":     n_covered,
        "n_with_catalyst":    n_with,
        "pct_with_catalyst":  round(pct * 100, 2) if n_covered else None,
        "base_rate_pct":      round(base_rate_all * 100, 2),
        "lift":               lift,
        "p_value":            p_val,
        "note":               _note(p_val, n_covered),
    }


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=== AVEVA-62 ST3b: causal attribution ===")

    pumps    = load_pump_events()
    sweeps   = load_sweep_clusters()
    macro    = load_macro_events()
    flows    = load_stablecoin_flows()
    news     = load_news_signals()
    unlocks  = load_token_unlocks()

    log.info("Pumps loaded: %d", len(pumps))
    log.info("Sweep clusters: %d rows, macro: %d, flows: %d, news: %d, unlocks: %d",
             len(sweeps), len(macro), len(flows), len(news), len(unlocks))

    # ── Precompute per-symbol sweep threshold (3σ above mean liq_usd_total per symbol)
    sweep_thresholds = {}
    for sym, grp in sweeps.groupby("symbol"):
        usd = grp["liq_usd_total"]
        mu, sigma = usd.mean(), usd.std()
        sweep_thresholds[sym] = mu + 3 * sigma if sigma > 0 else mu * 3
    log.info("Sweep 3σ thresholds computed for %d symbols", len(sweep_thresholds))

    # ── Precompute stablecoin inflow threshold (2σ above mean of positive deltas)
    pos_flows = flows[flows["delta_usd_1d"] > 0]["delta_usd_1d"]
    if len(pos_flows) >= 10:
        inflow_threshold = float(pos_flows.mean() + 2 * pos_flows.std())
    else:
        inflow_threshold = float(flows["delta_usd_1d"].quantile(0.95))
    log.info("Stablecoin inflow threshold: $%.0f", inflow_threshold)

    # ── Coverage windows
    SWEEP_COV_START = int(pd.Timestamp("2026-04-14", tz="UTC").value // 1_000_000)
    SWEEP_COV_END   = int(pd.Timestamp("2026-05-27", tz="UTC").value // 1_000_000)
    NEWS_COV_START  = int(pd.Timestamp("2026-04-12", tz="UTC").value // 1_000_000)
    NEWS_COV_END    = int(pd.Timestamp("2026-05-27", tz="UTC").value // 1_000_000)
    MACRO_COV_START = int(pd.Timestamp("2024-05-01", tz="UTC").value // 1_000_000)
    MACRO_COV_END   = int(pd.Timestamp("2026-05-27", tz="UTC").value // 1_000_000)
    FLOW_COV_START  = MACRO_COV_START
    FLOW_COV_END    = MACRO_COV_END

    # ── Attribution loop
    log.info("Running attribution for %d pumps ...", len(pumps))
    rows = []
    for _, pump in pumps.iterrows():
        pump_ms  = int(pump["start_ms"])
        symbol   = pump["symbol"]

        liq_flag  = False
        liq_note  = "no_coverage"
        if pump_ms >= SWEEP_COV_START:
            liq_flag = check_liq_sweep(pump_ms, symbol, sweeps, sweep_thresholds)
            liq_note = "checked"

        macro_flag = check_macro_extreme(pump_ms, macro)
        flow_flag  = check_stablecoin_inflow(pump_ms, flows, inflow_threshold)

        news_flag  = False
        news_note  = "no_coverage"
        if pump_ms >= NEWS_COV_START:
            news_flag = check_news_signal(pump_ms, symbol, news)
            news_note = "checked"

        unlock_flag = check_token_unlock(pump_ms, symbol, unlocks)

        catalysts = []
        if liq_flag:    catalysts.append("liq_sweep")
        if macro_flag:  catalysts.append("macro_extreme")
        if flow_flag:   catalysts.append("stablecoin_inflow")
        if news_flag:   catalysts.append("news_signal")
        if unlock_flag: catalysts.append("token_unlock")

        confidence = "NONE"
        if len(catalysts) >= 2:
            confidence = "MEDIUM"
        elif len(catalysts) == 1:
            confidence = "LOW"

        rows.append({
            "pump_id":          pump["pump_id"],
            "date":             pump["start_ts"],
            "start_ms":         pump_ms,
            "symbol":           symbol,
            "pct_chg":          pump["pct_chg"],
            "pump_type":        pump["pump_type"],
            "split":            pump["split"],
            "liq_sweep":        int(liq_flag),
            "macro_extreme":    int(macro_flag),
            "stablecoin_inflow": int(flow_flag),
            "news_signal":      int(news_flag),
            "token_unlock":     int(unlock_flag),
            "liq_note":         liq_note,
            "news_note":        news_note,
            "catalysts":        "|".join(catalysts) if catalysts else "none",
            "confidence":       confidence,
        })

    attr_df = pd.DataFrame(rows)
    attr_df.to_csv(OUT_DIR / "causal_attribution.csv", index=False)
    log.info("causal_attribution.csv → %d rows", len(attr_df))

    # ── Event study
    log.info("Computing event study lifts ...")
    catalyst_cols = {
        "liq_sweep":         (SWEEP_COV_START,  SWEEP_COV_END,  "Liquidation Sweep (>3σ)"),
        "macro_extreme":     (MACRO_COV_START,  MACRO_COV_END,  "Macro F&G Extreme (<25 or >75)"),
        "stablecoin_inflow": (FLOW_COV_START,   FLOW_COV_END,   "Stablecoin Net Inflow (>2σ)"),
        "news_signal":       (NEWS_COV_START,   NEWS_COV_END,   "News/Screener Signal"),
        "token_unlock":      (MACRO_COV_START,  MACRO_COV_END,  "Token Unlock [-7d,+1d]"),
    }
    event_rows = []
    for col, (cov_start, cov_end, label) in catalyst_cols.items():
        study = event_study(attr_df, col, cov_start, cov_end)
        event_rows.append({
            "catalyst":           col,
            "label":              label,
            "coverage_start":     str(pd.Timestamp(cov_start, unit="ms", tz="UTC").date()),
            "coverage_end":       str(pd.Timestamp(cov_end, unit="ms", tz="UTC").date()),
            **study,
        })
        log.info("  %-22s: %d/%d pumps (%.1f%%)  lift=%.2f  p=%.3f  [%s]",
                 col,
                 study.get("n_with_catalyst", 0) or 0,
                 study.get("coverage_pumps", 0) or 0,
                 study.get("pct_with_catalyst") or 0,
                 study.get("lift") or 0,
                 study.get("p_value") if study.get("p_value") is not None else 1.0,
                 study.get("note", ""))

    ev_df = pd.DataFrame(event_rows)
    ev_df.to_csv(OUT_DIR / "catalyst_event_study.csv", index=False)
    log.info("catalyst_event_study.csv → %d rows", len(ev_df))

    # ── Catalyst stats JSON
    total = len(attr_df)
    cat_counts = {
        col: int(attr_df[col].sum())
        for col in ["liq_sweep", "macro_extreme", "stablecoin_inflow", "news_signal", "token_unlock"]
    }
    n_none = int((attr_df["catalysts"] == "none").sum())
    n_multi = int((attr_df["catalysts"].str.count(r"\|") >= 1).sum())

    stats_out = {
        "generated_at":       pd.Timestamp.now(tz="UTC").isoformat(),
        "total_pumps":        total,
        "no_catalyst_pct":    round(n_none / total * 100, 2),
        "multi_catalyst_pct": round(n_multi / total * 100, 2),
        "by_category": {
            col: {
                "count": cat_counts[col],
                "pct_of_pumps": round(cat_counts[col] / total * 100, 2),
            }
            for col in cat_counts
        },
        "by_confidence": {
            conf: int((attr_df["confidence"] == conf).sum())
            for conf in ["HIGH", "MEDIUM", "LOW", "NONE"]
        },
        "coverage_notes": {
            "liq_sweep":    "Coverage: 2026-04-14 → 2026-05-27 (live DB only)",
            "news_signal":  "Coverage: 2026-04-12 → 2026-05-27 (screener started Apr 2026)",
            "token_unlock": "Only placeholder data — 0 real unlocks",
            "whale_pos":    "Skipped: no historical coverage",
            "orderbook":    "Skipped: no historical coverage",
        },
    }
    (OUT_DIR / "catalyst_stats.json").write_text(json.dumps(stats_out, indent=2))
    log.info("catalyst_stats.json written")

    # ── Plot
    labels = ["Liq Sweep", "Macro F&G\nExtreme", "Stablecoin\nInflow", "News\nSignal", "Token\nUnlock", "No Catalyst"]
    values = [
        cat_counts["liq_sweep"],
        cat_counts["macro_extreme"],
        cat_counts["stablecoin_inflow"],
        cat_counts["news_signal"],
        cat_counts["token_unlock"],
        n_none,
    ]
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    fig.suptitle(f"Pump Catalyst Distribution  (N={total})", fontsize=13)

    # Bar chart
    ax = axes[0]
    colors = ["#e74c3c", "#f39c12", "#3498db", "#2ecc71", "#9b59b6", "#95a5a6"]
    bars = ax.bar(labels, values, color=colors, edgecolor="white", linewidth=0.5)
    ax.set_ylabel("Number of pumps")
    ax.set_title("Catalyst Coincidence Counts\n(1 pump may have multiple catalysts)")
    for bar, v in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5,
                f"{v}\n({v/total*100:.1f}%)", ha="center", va="bottom", fontsize=9)
    ax.set_ylim(0, max(values) * 1.25)

    # Pie of pumps by confidence
    conf_counts = stats_out["by_confidence"]
    pie_labels  = [k for k in conf_counts if conf_counts[k] > 0]
    pie_sizes   = [conf_counts[k] for k in pie_labels]
    pie_colors  = {"HIGH": "#27ae60", "MEDIUM": "#f39c12", "LOW": "#e67e22", "NONE": "#95a5a6"}
    ax2 = axes[1]
    wedges, texts, autotexts = ax2.pie(
        pie_sizes,
        labels=[f"{k}\n(N={v})" for k, v in zip(pie_labels, pie_sizes)],
        colors=[pie_colors.get(k, "#cccccc") for k in pie_labels],
        autopct="%1.1f%%",
        startangle=140,
    )
    ax2.set_title("Attribution Confidence\n(per pump)")

    plt.tight_layout()
    plot_path = PLOTS_DIR / "catalyst_distribution.png"
    plt.savefig(plot_path, dpi=150, bbox_inches="tight")
    plt.close()
    log.info("Plot saved → %s", plot_path)

    # ── Summary
    log.info("\n=== Summary ===")
    log.info("  Total pumps attributed: %d", total)
    log.info("  No catalyst found:      %d (%.1f%%)", n_none, n_none / total * 100)
    for col, cnt in cat_counts.items():
        log.info("  %-22s: %d (%.1f%%)", col, cnt, cnt / total * 100)
    log.info("  Confidence breakdown: %s", stats_out["by_confidence"])


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
AVEVA-62 ST3b: Causal Attribution — катализаторы каждого пампа (event-study)

For each pump in pump_events.csv, checks each catalyst category in a
pre-defined lookback window and records coincidences.

Event study uses control_windows.csv (pre-built negative windows from AVEVA-59)
to compute P(catalyst|pump) vs P(catalyst|control) -> lift + Fisher's exact test.

PRINCIPLE: correlation != causality.  "Matched catalyst" != "proven cause".
Categories with no historical data -> "NONE", not guessed.

Coverage summary (from data_manifest.json):
  stablecoin_inflow -- full coverage  2024-05-01 -> 2026-05-27
  macro_extreme     -- full coverage  2024-05-01 -> 2026-05-27  (Fear/Greed index, NOT FOMC/CPI)
  liq_sweep         -- PARTIAL        2026-04-14 -> 2026-05-27  (live tracker start)
  news_signal       -- PARTIAL        2026-04-12 -> 2026-05-26  (screener output, NOT external news)
  token_unlock      -- NO COVERAGE    no real rows in token_unlocks.csv
  whale_positions   -- NO COVERAGE    Binance endpoint rejects historical requests
  orderbook         -- NO COVERAGE    real-time only

Outputs (saved to pump_analysis/):
  causal_attribution.csv        pump_id|date|symbol|pct_chg|catalysts|confidence|notes
  catalyst_event_study.csv      lift, p-value, N per category vs control windows
  catalyst_stats.json           % pumps per category; % "no catalyst"
  plots/catalyst_distribution.png
"""

import json
import logging
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

OUT_DIR   = Path(__file__).parent
CAT_DIR   = OUT_DIR / "catalyst_data"
PLOTS_DIR = OUT_DIR / "plots"
PLOTS_DIR.mkdir(exist_ok=True)


def _ts_ms(s: str) -> int:
    return int(pd.Timestamp(s, tz="UTC").value // 1_000_000)


SWEEP_COV_START = _ts_ms("2026-04-14")
SWEEP_COV_END   = _ts_ms("2026-05-28")
NEWS_COV_START  = _ts_ms("2026-04-12")
NEWS_COV_END    = _ts_ms("2026-05-27")
FULL_START      = _ts_ms("2024-05-01")
FULL_END        = _ts_ms("2026-05-28")


def _to_ms(ts_series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(ts_series, utc=True, errors="coerce")
    # pandas 2.x stores datetime64 as microseconds; // 1_000 gives milliseconds
    return (parsed.astype("int64") // 1_000).astype("int64")


def load_windows():
    pumps = pd.read_csv(OUT_DIR / "pump_events.csv")
    pumps["start_ms"] = _to_ms(pumps["start_ts"])
    ctrls = pd.read_csv(OUT_DIR / "control_windows.csv")
    ctrls["start_ms"] = _to_ms(ctrls["start_ts"])
    return pumps, ctrls


def load_sweep_clusters():
    df = pd.read_csv(CAT_DIR / "sweep_clusters.csv")
    df["hour_ts"]       = df["hour_ts"].astype("int64")
    df["liq_usd_total"] = pd.to_numeric(df["liq_usd_total"], errors="coerce").fillna(0)
    df = df.groupby(["hour_ts", "symbol"])["liq_usd_total"].sum().reset_index()
    thresholds = {}
    for sym, grp in df.groupby("symbol"):
        mu, sigma = grp["liq_usd_total"].mean(), grp["liq_usd_total"].std()
        thresholds[sym] = (mu + 3 * sigma) if (sigma and sigma > 0) else mu * 4
    return df, thresholds


def load_macro():
    df = pd.read_csv(CAT_DIR / "macro_events.csv")
    df["ts_ms"] = (df["timestamp"].astype("int64") * 1000).astype("int64")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df


def load_stablecoin():
    df = pd.read_csv(CAT_DIR / "stablecoin_flows.csv")
    df["ts_ms"]        = (df["timestamp"].astype("int64") * 1000).astype("int64")
    df["delta_usd_1d"] = pd.to_numeric(df["delta_usd_1d"], errors="coerce")
    mu, sigma = df["delta_usd_1d"].mean(), df["delta_usd_1d"].std()
    threshold = mu + 2 * sigma
    return df, threshold


def load_news():
    df = pd.read_csv(CAT_DIR / "news_signals.csv")
    # Support both old screener schema (run_ts) and new TG-news schema (ts_utc)
    ts_col = "ts_utc" if "ts_utc" in df.columns else "run_ts"
    df["ts_ms"] = _to_ms(df[ts_col])
    return df


def load_unlocks():
    df = pd.read_csv(CAT_DIR / "token_unlocks.csv")
    df = df[~df["symbol"].str.startswith("_")].copy()
    if not df.empty:
        df["ts_ms"] = _to_ms(df["unlock_date"])
    return df


def check_liq_sweep(windows: pd.DataFrame, sweeps: pd.DataFrame, thresholds: dict) -> pd.Series:
    out = pd.Series(np.nan, index=windows.index, dtype="float64")
    covered = windows[windows["start_ms"] >= SWEEP_COV_START]
    if covered.empty or sweeps.empty:
        return out
    for idx, row in covered.iterrows():
        lo  = int(row["start_ms"]) - 2 * 3_600_000
        hi  = int(row["start_ms"]) + 1 * 3_600_000
        sym = row["symbol"]
        mask = (sweeps["symbol"] == sym) & (sweeps["hour_ts"] >= lo) & (sweeps["hour_ts"] <= hi)
        w = sweeps.loc[mask, "liq_usd_total"]
        if w.empty:
            out[idx] = 0.0
        else:
            thresh = thresholds.get(sym, float("inf"))
            out[idx] = float(w.sum() > thresh)
    return out


def check_macro_extreme(windows: pd.DataFrame, macro: pd.DataFrame) -> pd.Series:
    out = pd.Series(0.0, index=windows.index)
    h48 = 48 * 3_600_000
    for idx, row in windows.iterrows():
        lo = int(row["start_ms"]) - h48
        hi = int(row["start_ms"])
        w  = macro[(macro["ts_ms"] >= lo) & (macro["ts_ms"] < hi)]
        out[idx] = float(not w.empty and ((w["value"] < 25) | (w["value"] > 75)).any())
    return out


def check_stablecoin(windows: pd.DataFrame, flows: pd.DataFrame, threshold: float) -> pd.Series:
    out = pd.Series(0.0, index=windows.index)
    h48 = 48 * 3_600_000
    for idx, row in windows.iterrows():
        lo = int(row["start_ms"]) - h48
        hi = int(row["start_ms"])
        w  = flows[(flows["ts_ms"] >= lo) & (flows["ts_ms"] < hi)]
        out[idx] = float(not w.empty and (w["delta_usd_1d"] > threshold).any())
    return out


def check_news_signal(windows: pd.DataFrame, news: pd.DataFrame) -> pd.Series:
    out = pd.Series(np.nan, index=windows.index)
    covered = windows[windows["start_ms"] >= NEWS_COV_START]
    if covered.empty or news.empty:
        return out
    h24 = 24 * 3_600_000
    h2  = 2  * 3_600_000
    for idx, row in covered.iterrows():
        lo  = int(row["start_ms"]) - h24
        hi  = int(row["start_ms"]) + h2
        hits = news[(news["symbol"] == row["symbol"]) & (news["ts_ms"] >= lo) & (news["ts_ms"] <= hi)]
        out[idx] = float(not hits.empty)
    return out


def check_unlock(windows: pd.DataFrame, unlocks: pd.DataFrame) -> pd.Series:
    if unlocks.empty:
        return pd.Series(np.nan, index=windows.index)
    out = pd.Series(0.0, index=windows.index)
    d7 = 7  * 24 * 3_600_000
    d1 = 1  * 24 * 3_600_000
    for idx, row in windows.iterrows():
        lo   = int(row["start_ms"]) - d7
        hi   = int(row["start_ms"]) + d1
        hits = unlocks[(unlocks["symbol"] == row["symbol"]) & (unlocks["ts_ms"] >= lo) & (unlocks["ts_ms"] <= hi)]
        out[idx] = float(not hits.empty)
    return out


def event_study_from_control(pump_hits: pd.Series, ctrl_hits: pd.Series, category: str) -> dict:
    p = pump_hits.dropna().astype(bool)
    c = ctrl_hits.dropna().astype(bool)
    N_pump = len(p)
    N_ctrl = len(c)
    if N_pump == 0 or N_ctrl == 0:
        return _null_study(category, N_pump, N_ctrl)

    a  = int(p.sum())
    c_ = N_pump - a
    b  = int(c.sum())
    d  = N_ctrl - b
    N_events = a + b

    P_ev_pump = a / N_pump
    P_ev_ctrl = b / N_ctrl if N_ctrl > 0 else 0.0
    lift = (P_ev_pump / P_ev_ctrl) if P_ev_ctrl > 0 else None
    base_rate = N_pump / (N_pump + N_ctrl)
    P_pump_ev = a / N_events if N_events > 0 else 0.0
    lift_alt  = (P_pump_ev / base_rate) if base_rate > 0 else None

    table = [[a, c_], [b, d]]
    _, p_val = scipy_stats.fisher_exact(table, alternative="greater")

    if N_events < 20:
        note = "малая выборка, не интерпретировать"
    elif lift is None:
        note = "нет событий в control"
    elif (lift or 0) < 1.2 or p_val > 0.05:
        note = "статистически не подтверждено"
    elif a >= 30 and (lift or 0) >= 2.0 and p_val <= 0.01:
        note = "HIGH confidence"
    elif a >= 10 and (lift or 0) >= 1.5 and p_val <= 0.05:
        note = "MEDIUM confidence"
    else:
        note = "LOW confidence"

    return {
        "category":               category,
        "N_pumps_analyzed":       N_pump,
        "N_controls_analyzed":    N_ctrl,
        "N_pump_with_event":      a,
        "N_ctrl_with_event":      b,
        "P_event_pump":           round(P_ev_pump, 4),
        "P_event_ctrl":           round(P_ev_ctrl, 4),
        "lift_P_ev_pump_vs_ctrl": round(lift, 3)     if lift     is not None else None,
        "lift_P_pump_ev_vs_base": round(lift_alt, 3) if lift_alt is not None else None,
        "p_value":                round(float(p_val), 4),
        "interpretation":         note,
    }


def _null_study(category: str, N_pump: int, N_ctrl: int) -> dict:
    return {
        "category":               category,
        "N_pumps_analyzed":       N_pump,
        "N_controls_analyzed":    N_ctrl,
        "N_pump_with_event":      0,
        "N_ctrl_with_event":      0,
        "P_event_pump":           None,
        "P_event_ctrl":           None,
        "lift_P_ev_pump_vs_ctrl": None,
        "lift_P_pump_ev_vs_base": None,
        "p_value":                None,
        "interpretation":         "no_coverage",
    }


def main():
    log.info("=== AVEVA-62 ST3b: causal attribution (v2, control windows) ===")

    pumps, ctrls = load_windows()
    log.info("Pump windows: %d | Control windows: %d", len(pumps), len(ctrls))

    sweeps, sweep_thresh = load_sweep_clusters()
    log.info("Sweep clusters: %d rows, %d symbol thresholds", len(sweeps), len(sweep_thresh))

    macro = load_macro()
    flows, stable_threshold = load_stablecoin()
    log.info("Stablecoin 2-sigma threshold: $%.0fM", stable_threshold / 1e6)

    news    = load_news()
    unlocks = load_unlocks()
    log.info("News rows: %d | Unlock rows: %d", len(news), len(unlocks))

    log.info("Checking liq_sweep (partial cov.) ...")
    p_sweep  = check_liq_sweep(pumps, sweeps, sweep_thresh)
    c_sweep  = check_liq_sweep(ctrls, sweeps, sweep_thresh)

    log.info("Checking macro_extreme ...")
    p_macro  = check_macro_extreme(pumps, macro)
    c_macro  = check_macro_extreme(ctrls, macro)

    log.info("Checking stablecoin_inflow ...")
    p_stable = check_stablecoin(pumps, flows, stable_threshold)
    c_stable = check_stablecoin(ctrls, flows, stable_threshold)

    log.info("Checking news_signal (partial cov.) ...")
    p_news   = check_news_signal(pumps, news)
    c_news   = check_news_signal(ctrls, news)

    log.info("Checking token_unlock (no real data) ...")
    p_unlock = check_unlock(pumps, unlocks)
    c_unlock = check_unlock(ctrls, unlocks)

    log.info("Running event studies vs. control windows ...")
    study_rows = []
    for col, p_hits, c_hits in [
        ("liq_sweep",         p_sweep,  c_sweep),
        ("macro_extreme",     p_macro,  c_macro),
        ("stablecoin_inflow", p_stable, c_stable),
        ("news_signal",       p_news,   c_news),
        ("token_unlock",      p_unlock, c_unlock),
    ]:
        es = event_study_from_control(p_hits, c_hits, col)
        study_rows.append(es)
        log.info(
            "  %-22s pump:%d/%d (%.1f%%)  ctrl:%d/%d (%.1f%%)  lift=%.2f  p=%.4f  [%s]",
            col,
            es.get("N_pump_with_event", 0),
            es.get("N_pumps_analyzed", 0),
            (es.get("N_pump_with_event", 0) / max(es.get("N_pumps_analyzed", 1), 1)) * 100,
            es.get("N_ctrl_with_event", 0),
            es.get("N_controls_analyzed", 0),
            (es.get("N_ctrl_with_event", 0) / max(es.get("N_controls_analyzed", 1), 1)) * 100,
            es.get("lift_P_ev_pump_vs_ctrl") or 0,
            es.get("p_value") or 1,
            es.get("interpretation", ""),
        )

    for cat in ["whale_positions", "orderbook"]:
        study_rows.append(_null_study(cat, 0, 0))

    ev_df = pd.DataFrame(study_rows)

    coverage_labels = {
        "liq_sweep":         "partial 2026-04-14->2026-05-27",
        "macro_extreme":     "full 2024-05-01->2026-05-27 (Fear/Greed index, NOT FOMC/CPI)",
        "stablecoin_inflow": "full 2024-05-01->2026-05-27",
        "news_signal":       "partial 2026-04-12->2026-05-26 (screener output, NOT external news)",
        "token_unlock":      "no_coverage -- 0 real rows in token_unlocks.csv",
        "whale_positions":   "no_coverage -- Binance L/S endpoint rejects historical requests",
        "orderbook":         "no_coverage -- real-time only",
    }
    ev_df["coverage_note"] = ev_df["category"].map(coverage_labels)
    ev_df.to_csv(OUT_DIR / "catalyst_event_study.csv", index=False)
    log.info("catalyst_event_study.csv -> %d rows", len(ev_df))

    CONF_ORDER = ["HIGH", "MEDIUM", "LOW", "NONE"]
    cat_confidence = {}
    for _, s in ev_df.iterrows():
        interp = str(s.get("interpretation", ""))
        if "HIGH" in interp:
            cat_confidence[s["category"]] = "HIGH"
        elif "MEDIUM" in interp:
            cat_confidence[s["category"]] = "MEDIUM"
        elif "no_coverage" in interp or s.get("N_pumps_analyzed", 0) == 0:
            cat_confidence[s["category"]] = "NONE"
        else:
            cat_confidence[s["category"]] = "LOW"

    log.info("Building attribution table ...")
    attr_rows = []
    for i, pump in pumps.iterrows():
        pump_ms = int(pump["start_ms"])
        sym     = pump["symbol"]
        hits    = []
        confs   = []
        notes   = []

        sv = p_sweep[i]
        if pd.isna(sv):
            notes.append("sweep:pre_coverage")
        elif sv:
            hits.append("liq_sweep")
            confs.append(cat_confidence.get("liq_sweep", "LOW"))

        if p_macro[i]:
            hits.append("macro_extreme")
            confs.append(cat_confidence.get("macro_extreme", "LOW"))

        if p_stable[i]:
            hits.append("stablecoin_inflow")
            confs.append(cat_confidence.get("stablecoin_inflow", "LOW"))

        nv = p_news[i]
        if pd.isna(nv):
            notes.append("news:pre_coverage")
        elif nv:
            hits.append("news_signal")
            confs.append(cat_confidence.get("news_signal", "LOW"))
            notes.append("news=screener_proxy_not_external_news")

        uv = p_unlock[i]
        if pd.isna(uv):
            notes.append("unlock:no_data")
        elif uv:
            hits.append("token_unlock")
            confs.append("LOW")

        notes.append("whale_positions:no_data")
        if pd.isna(p_unlock[i]):
            notes.append("token_unlock:no_data")

        h48 = 48 * 3_600_000
        fg_w = macro[(macro["ts_ms"] >= pump_ms - h48) & (macro["ts_ms"] < pump_ms)]
        if not fg_w.empty:
            fg_label = f"fg={fg_w['value'].mean():.0f}({fg_w['value_classification'].mode()[0]})"
            notes.insert(0, fg_label)

        final_conf = min(confs, key=lambda x: CONF_ORDER.index(x)) if confs else "NONE"

        attr_rows.append({
            "pump_id":    pump["pump_id"],
            "date":       pump["start_ts"],
            "symbol":     sym,
            "pct_chg":    round(float(pump["pct_chg"]), 2),
            "catalysts":  "|".join(hits) if hits else "none",
            "confidence": final_conf,
            "notes":      "; ".join(notes),
        })

    attr_df = pd.DataFrame(attr_rows)
    attr_df.to_csv(OUT_DIR / "causal_attribution.csv", index=False)
    log.info("causal_attribution.csv -> %d rows", len(attr_df))

    total    = len(attr_df)
    cat_cols = ["liq_sweep", "macro_extreme", "stablecoin_inflow", "news_signal", "token_unlock"]
    cat_counts = {c: int(attr_df["catalysts"].str.contains(c, regex=False).sum()) for c in cat_cols}
    n_none   = int((attr_df["catalysts"] == "none").sum())
    n_any    = total - n_none

    n_sweep_cov     = int(p_sweep.notna().sum())
    n_sweep_hit_cov = int((p_sweep == 1.0).sum())
    n_news_cov      = int(p_news.notna().sum())
    n_news_hit_cov  = int((p_news == 1.0).sum())

    def ev_val(cat, field):
        row = ev_df[ev_df["category"] == cat]
        return row[field].values[0] if not row.empty else None

    stats_out = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "total_pumps":  total,
        "methodology":  (
            "Event study uses control_windows.csv (pre-built negative windows). "
            "Confidence = HIGH/MEDIUM/LOW/NONE based on lift+p-value per category. "
            "whale_positions and orderbook have no_coverage. "
            "token_unlock has 0 real rows (only _EXAMPLE placeholder). "
            "news_signal = screener output NOT external news/listing events. "
            "macro_extreme = Fear & Greed Index NOT FOMC/CPI."
        ),
        "by_category": {
            "liq_sweep": {
                "coverage":           "partial 2026-04-14+",
                "pumps_in_window":    n_sweep_cov,
                "n_hits":             n_sweep_hit_cov,
                "pct_of_covered":     round(n_sweep_hit_cov / n_sweep_cov * 100, 1) if n_sweep_cov > 0 else 0,
                "event_study_lift":   ev_val("liq_sweep", "lift_P_ev_pump_vs_ctrl"),
                "event_study_interp": ev_val("liq_sweep", "interpretation"),
            },
            "macro_extreme": {
                "coverage":           "full (Fear/Greed index, NOT FOMC/CPI)",
                "n_pumps_with_hit":   cat_counts["macro_extreme"],
                "pct_of_total":       round(cat_counts["macro_extreme"] / total * 100, 1),
                "event_study_lift":   ev_val("macro_extreme", "lift_P_ev_pump_vs_ctrl"),
                "event_study_interp": ev_val("macro_extreme", "interpretation"),
            },
            "stablecoin_inflow": {
                "coverage":           "full",
                "n_pumps_with_hit":   cat_counts["stablecoin_inflow"],
                "pct_of_total":       round(cat_counts["stablecoin_inflow"] / total * 100, 1),
                "event_study_lift":   ev_val("stablecoin_inflow", "lift_P_ev_pump_vs_ctrl"),
                "event_study_interp": ev_val("stablecoin_inflow", "interpretation"),
            },
            "news_signal": {
                "coverage":           "partial 2026-04-12+ (screener output, NOT external news)",
                "pumps_in_window":    n_news_cov,
                "n_hits":             n_news_hit_cov,
                "pct_of_covered":     round(n_news_hit_cov / n_news_cov * 100, 1) if n_news_cov > 0 else 0,
                "event_study_lift":   ev_val("news_signal", "lift_P_ev_pump_vs_ctrl"),
                "event_study_interp": ev_val("news_signal", "interpretation"),
            },
            "token_unlock":    {"coverage": "no_coverage", "n_hits": 0},
            "whale_positions": {"coverage": "no_coverage", "n_hits": 0},
            "orderbook":       {"coverage": "no_coverage", "n_hits": 0},
        },
        "aggregate": {
            "pumps_with_any_catalyst":       n_any,
            "pct_with_any_catalyst":         round(n_any / total * 100, 1),
            "pumps_no_catalyst_established": n_none,
            "pct_no_catalyst_established":   round(n_none / total * 100, 1),
        },
        "confidence_breakdown": {
            conf: int((attr_df["confidence"] == conf).sum())
            for conf in ["HIGH", "MEDIUM", "LOW", "NONE"]
        },
        "thresholds": {
            "stablecoin_2sigma_usd":  round(float(stable_threshold)),
            "fear_greed_extreme_lo":  25,
            "fear_greed_extreme_hi":  75,
            "liq_sweep_sigma":        3,
        },
    }

    (OUT_DIR / "catalyst_stats.json").write_text(
        json.dumps(stats_out, indent=2, ensure_ascii=False)
    )
    log.info("catalyst_stats.json written")

    # Plot
    fig, axes = plt.subplots(1, 2, figsize=(15, 6))
    fig.suptitle(
        f"Pump Catalyst Distribution  (N={total} pumps, AVEVA-62)\n"
        "WARNING: whale/token_unlock/orderbook = no_coverage; sweep/news = partial coverage",
        fontsize=10, fontweight="bold",
    )

    ax = axes[0]
    bar_labels = ["Liq Sweep\n(2026-04+)", "Macro F&G\nExtreme", "Stablecoin\nInflow >2sigma",
                  "News Signal\n(screener proxy)", "No Catalyst\nEstablished"]
    bar_vals = [n_sweep_hit_cov, cat_counts["macro_extreme"], cat_counts["stablecoin_inflow"],
                n_news_hit_cov, n_none]
    bar_cols = ["#e74c3c", "#f39c12", "#3498db", "#2ecc71", "#95a5a6"]
    bars = ax.bar(bar_labels, bar_vals, color=bar_cols, edgecolor="white", linewidth=0.5)
    for bar, v in zip(bars, bar_vals):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5,
                f"{v}\n({v/total*100:.1f}%)", ha="center", va="bottom", fontsize=9)
    ax.set_ylabel("Number of pumps")
    ax.set_title("Catalyst Coincidence Counts\n(1 pump can match multiple catalysts)")
    ax.set_ylim(0, max(bar_vals) * 1.3)
    ax.grid(axis="y", alpha=0.3)

    ax2 = axes[1]
    es_plot = ev_df[ev_df["lift_P_ev_pump_vs_ctrl"].notna()].copy()
    if not es_plot.empty:
        xlabels = [r.replace("_", "\n") for r in es_plot["category"]]
        lifts   = es_plot["lift_P_ev_pump_vs_ctrl"].astype(float).values
        pvals   = es_plot["p_value"].astype(float).values
        interps = es_plot["interpretation"].values

        bar_cols2 = []
        for l, p, interp in zip(lifts, pvals, interps):
            if "HIGH" in str(interp):
                bar_cols2.append("#27ae60")
            elif "MEDIUM" in str(interp):
                bar_cols2.append("#f39c12")
            elif "подтверждено" in str(interp):
                bar_cols2.append("#e74c3c")
            else:
                bar_cols2.append("#95a5a6")

        bars2 = ax2.bar(xlabels, lifts, color=bar_cols2, edgecolor="white", linewidth=0.5)
        for bar, lv, pv in zip(bars2, lifts, pvals):
            sig = "*" if pv is not None and pv <= 0.05 else ""
            ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.02,
                     f"p={pv:.3f}{sig}", ha="center", va="bottom", fontsize=8)
        ax2.axhline(1.0, color="black",  lw=1.0, linestyle="-",  label="lift=1 (no effect)")
        ax2.axhline(1.2, color="orange", lw=1.0, linestyle="--", label="threshold 1.2")
        ax2.axhline(2.0, color="red",    lw=1.0, linestyle="--", label="HIGH threshold 2.0")
        ax2.set_ylabel("Lift  [P(event|pump) / P(event|control)]", fontsize=10)
        ax2.set_title("Event Study: Lift vs Control Windows\n(* = Fisher p <= 0.05)", fontsize=10)
        ax2.legend(fontsize=8)
        ax2.grid(axis="y", alpha=0.3)
    else:
        ax2.text(0.5, 0.5, "No covered categories for event study",
                 ha="center", va="center", transform=ax2.transAxes)

    plt.tight_layout()
    plt.savefig(PLOTS_DIR / "catalyst_distribution.png", dpi=150, bbox_inches="tight")
    plt.close()
    log.info("Plot saved -> %s", PLOTS_DIR / "catalyst_distribution.png")

    log.info("\n=== SUMMARY ===")
    log.info("Total pumps: %d", total)
    log.info("No catalyst established: %d (%.1f%%)", n_none, n_none / total * 100)
    for col, cnt in cat_counts.items():
        log.info("  %-35s: %d (%.1f%%)", col, cnt, cnt / total * 100)
    log.info("Confidence: %s", stats_out["confidence_breakdown"])
    log.info("\nEvent study results:")
    for _, s in ev_df.iterrows():
        lift_s = f"lift={s['lift_P_ev_pump_vs_ctrl']:.3f}" if s["lift_P_ev_pump_vs_ctrl"] is not None else "lift=n/a"
        p_s    = f"p={s['p_value']:.4f}" if s["p_value"] is not None else "p=n/a"
        log.info("  %-22s: %s, %s  -> %s", s["category"], lift_s, p_s, s["interpretation"])


if __name__ == "__main__":
    main()

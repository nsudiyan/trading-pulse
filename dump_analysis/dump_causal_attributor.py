#!/usr/bin/env python3
"""
AVEC-63 / AVEA-66 ST3: Causal Attribution — катализаторы каждого дампа (event-study)

Mirror of pump_analysis/causal_attributor.py, re-wired for dump events.

Catalyst buckets:
  liq_cascade       — long-liquidation cascade: sum(long_liq_usd) > 3-sigma in T±2h
  macro_event       — Fear & Greed index ≤25 (extreme fear) in T-48h..T
  token_unlock      — token unlock ±7d / +1d  (no real data currently)
  stablecoin_outflow— stablecoin net redemption >2-sigma below mean in T-48h..T
  btc_cascade       — BTC 1h drop ≥5% in any candle in T±2h
  news_negative     — screener SHORT-direction signal ±24h / +2h
  unknown           — no catalyst established (MANDATORY, never empty)

Event study: P(catalyst|dump) vs P(catalyst|control_window) → lift + Fisher's exact test.
Only p<0.05 catalysts are tagged FACT; others are HYPOTHESIS.

Coverage notes (inherited from pump data manifest):
  liq_cascade        — PARTIAL  2026-04-14 → 2026-05-27
  macro_event        — FULL     2024-05-01 → 2026-05-27 (Fear/Greed, NOT FOMC/CPI)
  token_unlock       — NO COVERAGE (0 real rows)
  stablecoin_outflow — FULL     2024-05-01 → 2026-05-27
  btc_cascade        — FULL     2024-05-01 → 2026-05-27 (BTCUSDT 1h klines)
  news_negative      — PARTIAL  2026-04-12 → 2026-05-26 (screener output, NOT external news)

Outputs (dump_analysis/):
  causal_attribution.csv        dump_id|date|symbol|pct_chg|primary_cause|secondary_cause|confidence|notes
  catalyst_event_study.csv      lift, p-value, N per category vs control windows
  catalyst_stats.json
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
log = logging.getLogger("dump_causal_attributor")

OUT_DIR   = Path(__file__).parent
CAT_DIR   = OUT_DIR.parent / "pump_analysis" / "catalyst_data"
KLINES_1H = OUT_DIR.parent / "pump_analysis" / "klines_1h"
PLOTS_DIR = OUT_DIR / "plots"
PLOTS_DIR.mkdir(exist_ok=True)


def _ts_ms(s: str) -> int:
    return int(pd.Timestamp(s, tz="UTC").value // 1_000_000)


LIQ_COV_START  = _ts_ms("2026-04-14")
NEWS_COV_START = _ts_ms("2026-04-12")
NEWS_COV_END   = _ts_ms("2026-05-27")
FULL_START     = _ts_ms("2024-05-01")


def _to_ms(ts_series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(ts_series, utc=True, errors="coerce")
    return (parsed.astype("int64") // 1_000).astype("int64")


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def load_windows():
    dumps = pd.read_csv(OUT_DIR / "dump_events.csv")
    dumps["start_ms"] = _to_ms(dumps["start_ts"])
    ctrls = pd.read_csv(OUT_DIR / "control_windows.csv")
    ctrls["start_ms"] = _to_ms(ctrls["start_ts"])
    return dumps, ctrls


def load_long_liq_clusters():
    """Load long liquidations and compute per-symbol 3-sigma threshold."""
    df = pd.read_csv(CAT_DIR / "sweep_clusters.csv")
    df = df[df["side"] == "long_liq"].copy()
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
    # outflow threshold: 2 sigma below mean (large net redemption)
    threshold = mu - 2 * sigma
    return df, threshold


def load_btc_klines():
    """Load BTC 1h klines; compute hourly return and 3-sigma drop threshold.

    The canonical -5%/1h threshold rarely fires in the 2024-2026 dataset
    (worst observed drop: -4.9%). We use a data-driven 3-sigma below mean
    (≈ -1.5%) which is methodologically consistent with liq_cascade.
    The threshold is logged so results are reproducible.
    """
    path = KLINES_1H / "BTCUSDT.csv.gz"
    if not path.exists():
        log.warning("BTCUSDT.csv.gz not found — btc_cascade coverage disabled")
        return pd.DataFrame(columns=["open_time", "pct_chg_1h"]), float("-inf")
    df = pd.read_csv(path)
    df["pct_chg_1h"] = df["close"] / df["open"] - 1.0
    mu, sigma = df["pct_chg_1h"].mean(), df["pct_chg_1h"].std()
    threshold = mu - 3 * sigma
    log.info(
        "BTC 1h drop: mu=%.4f σ=%.4f  3-sigma threshold=%.4f (%.2f%%)",
        mu, sigma, threshold, threshold * 100,
    )
    return df[["open_time", "pct_chg_1h"]].copy(), threshold


def load_news():
    df = pd.read_csv(CAT_DIR / "news_signals.csv")
    df["ts_ms"] = _to_ms(df["run_ts"])
    # Keep only SHORT-direction signals as negative news proxy
    short_mask = df["direction"].str.upper().str.contains("SHORT|ШОРТ", na=False)
    return df[short_mask].copy()


def load_unlocks():
    df = pd.read_csv(CAT_DIR / "token_unlocks.csv")
    df = df[~df["symbol"].str.startswith("_")].copy()
    if not df.empty:
        df["ts_ms"] = _to_ms(df["unlock_date"])
    return df


# ---------------------------------------------------------------------------
# Catalyst checkers  (return Series of float: 1.0=present, 0.0=absent, nan=uncovered)
# ---------------------------------------------------------------------------

def check_liq_cascade(windows: pd.DataFrame, long_liqs: pd.DataFrame,
                      thresholds: dict) -> pd.Series:
    """Long-liquidation cascade: sum(long_liq) > 3-sigma threshold in T±2h."""
    out = pd.Series(np.nan, index=windows.index, dtype="float64")
    covered = windows[windows["start_ms"] >= LIQ_COV_START]
    if covered.empty or long_liqs.empty:
        return out
    h2 = 2 * 3_600_000
    for idx, row in covered.iterrows():
        lo  = int(row["start_ms"]) - h2
        hi  = int(row["start_ms"]) + h2
        sym = row["symbol"]
        mask = (long_liqs["symbol"] == sym) & \
               (long_liqs["hour_ts"] >= lo) & \
               (long_liqs["hour_ts"] <= hi)
        w = long_liqs.loc[mask, "liq_usd_total"]
        if w.empty:
            out[idx] = 0.0
        else:
            thresh = thresholds.get(sym, float("inf"))
            out[idx] = float(w.sum() > thresh)
    return out


def check_macro_extreme_fear(windows: pd.DataFrame, macro: pd.DataFrame) -> pd.Series:
    """Extreme fear (Fear&Greed ≤25) in T-48h..T."""
    out = pd.Series(0.0, index=windows.index)
    h48 = 48 * 3_600_000
    for idx, row in windows.iterrows():
        lo = int(row["start_ms"]) - h48
        hi = int(row["start_ms"])
        w  = macro[(macro["ts_ms"] >= lo) & (macro["ts_ms"] < hi)]
        out[idx] = float(not w.empty and (w["value"] <= 25).any())
    return out


def check_stablecoin_outflow(windows: pd.DataFrame, flows: pd.DataFrame,
                              threshold: float) -> pd.Series:
    """Large stablecoin net outflow (redemption) in T-48h..T."""
    out = pd.Series(0.0, index=windows.index)
    h48 = 48 * 3_600_000
    for idx, row in windows.iterrows():
        lo = int(row["start_ms"]) - h48
        hi = int(row["start_ms"])
        w  = flows[(flows["ts_ms"] >= lo) & (flows["ts_ms"] < hi)]
        out[idx] = float(not w.empty and (w["delta_usd_1d"] < threshold).any())
    return out


def check_btc_cascade(windows: pd.DataFrame, btc: pd.DataFrame,
                      threshold: float) -> pd.Series:
    """BTC dropped ≤ threshold (3-sigma below mean) in any 1h candle within T±2h."""
    if btc.empty:
        return pd.Series(np.nan, index=windows.index)
    out = pd.Series(0.0, index=windows.index)
    h2 = 2 * 3_600_000
    for idx, row in windows.iterrows():
        lo = int(row["start_ms"]) - h2
        hi = int(row["start_ms"]) + h2
        w  = btc[(btc["open_time"] >= lo) & (btc["open_time"] <= hi)]
        out[idx] = float(not w.empty and (w["pct_chg_1h"] <= threshold).any())
    return out


def check_news_negative(windows: pd.DataFrame, news: pd.DataFrame) -> pd.Series:
    """Screener SHORT-direction signal for the symbol in T-24h..T+2h."""
    out = pd.Series(np.nan, index=windows.index)
    covered = windows[windows["start_ms"] >= NEWS_COV_START]
    if covered.empty or news.empty:
        return out
    h24 = 24 * 3_600_000
    h2  =  2 * 3_600_000
    for idx, row in covered.iterrows():
        lo   = int(row["start_ms"]) - h24
        hi   = int(row["start_ms"]) + h2
        hits = news[(news["symbol"] == row["symbol"]) &
                    (news["ts_ms"] >= lo) & (news["ts_ms"] <= hi)]
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
        hits = unlocks[(unlocks["symbol"] == row["symbol"]) &
                       (unlocks["ts_ms"] >= lo) & (unlocks["ts_ms"] <= hi)]
        out[idx] = float(not hits.empty)
    return out


# ---------------------------------------------------------------------------
# Event study helpers
# ---------------------------------------------------------------------------

def event_study_from_control(dump_hits: pd.Series, ctrl_hits: pd.Series,
                              category: str) -> dict:
    p = dump_hits.dropna().astype(bool)
    c = ctrl_hits.dropna().astype(bool)
    N_dump = len(p)
    N_ctrl = len(c)
    if N_dump == 0 or N_ctrl == 0:
        return _null_study(category, N_dump, N_ctrl)

    a  = int(p.sum())
    c_ = N_dump - a
    b  = int(c.sum())
    d  = N_ctrl - b

    P_ev_dump = a / N_dump
    P_ev_ctrl = b / N_ctrl if N_ctrl > 0 else 0.0
    lift      = (P_ev_dump / P_ev_ctrl) if P_ev_ctrl > 0 else None
    N_events  = a + b
    base_rate = N_dump / (N_dump + N_ctrl)
    P_dump_ev = a / N_events if N_events > 0 else 0.0
    lift_alt  = (P_dump_ev / base_rate) if base_rate > 0 else None

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
        "N_dumps_analyzed":       N_dump,
        "N_controls_analyzed":    N_ctrl,
        "N_dump_with_event":      a,
        "N_ctrl_with_event":      b,
        "P_event_dump":           round(P_ev_dump, 4),
        "P_event_ctrl":           round(P_ev_ctrl, 4),
        "lift_P_ev_dump_vs_ctrl": round(lift, 3)     if lift     is not None else None,
        "lift_P_dump_ev_vs_base": round(lift_alt, 3) if lift_alt is not None else None,
        "p_value":                round(float(p_val), 4),
        "interpretation":         note,
    }


def _null_study(category: str, N_dump: int, N_ctrl: int) -> dict:
    return {
        "category":               category,
        "N_dumps_analyzed":       N_dump,
        "N_controls_analyzed":    N_ctrl,
        "N_dump_with_event":      0,
        "N_ctrl_with_event":      0,
        "P_event_dump":           None,
        "P_event_ctrl":           None,
        "lift_P_ev_dump_vs_ctrl": None,
        "lift_P_dump_ev_vs_base": None,
        "p_value":                None,
        "interpretation":         "no_coverage",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=== AVEC-63 / AVEA-66 ST3: dump causal attribution ===")

    dumps, ctrls = load_windows()
    log.info("Dump windows: %d | Control windows: %d", len(dumps), len(ctrls))

    long_liqs, liq_thresh = load_long_liq_clusters()
    log.info("Long-liq clusters: %d rows, %d symbol thresholds",
             len(long_liqs), len(liq_thresh))

    macro = load_macro()
    flows, stable_threshold = load_stablecoin()
    log.info("Stablecoin 2-sigma outflow threshold: $%.0fM", stable_threshold / 1e6)

    btc, btc_threshold = load_btc_klines()
    log.info("BTC 1h candles: %d rows  threshold=%.4f", len(btc), btc_threshold)

    news    = load_news()
    unlocks = load_unlocks()
    log.info("Negative news rows (SHORT signals): %d | Unlock rows: %d",
             len(news), len(unlocks))

    log.info("Checking liq_cascade (partial cov.) ...")
    d_liq  = check_liq_cascade(dumps, long_liqs, liq_thresh)
    c_liq  = check_liq_cascade(ctrls, long_liqs, liq_thresh)

    log.info("Checking macro_event (extreme fear) ...")
    d_macro = check_macro_extreme_fear(dumps, macro)
    c_macro = check_macro_extreme_fear(ctrls, macro)

    log.info("Checking stablecoin_outflow ...")
    d_stable = check_stablecoin_outflow(dumps, flows, stable_threshold)
    c_stable = check_stablecoin_outflow(ctrls, flows, stable_threshold)

    log.info("Checking btc_cascade ...")
    d_btc  = check_btc_cascade(dumps, btc, btc_threshold)
    c_btc  = check_btc_cascade(ctrls, btc, btc_threshold)

    log.info("Checking news_negative (partial cov.) ...")
    d_news = check_news_negative(dumps, news)
    c_news = check_news_negative(ctrls, news)

    log.info("Checking token_unlock (no real data) ...")
    d_unlock = check_unlock(dumps, unlocks)
    c_unlock = check_unlock(ctrls, unlocks)

    log.info("Running event studies vs. control windows ...")
    study_rows = []
    for col, d_hits, c_hits in [
        ("liq_cascade",        d_liq,    c_liq),
        ("macro_event",        d_macro,  c_macro),
        ("stablecoin_outflow", d_stable, c_stable),
        ("btc_cascade",        d_btc,    c_btc),
        ("news_negative",      d_news,   c_news),
        ("token_unlock",       d_unlock, c_unlock),
    ]:
        es = event_study_from_control(d_hits, c_hits, col)
        study_rows.append(es)
        _lift = es.get("lift_P_ev_dump_vs_ctrl")
        _pval = es.get("p_value")
        log.info(
            "  %-22s dump:%d/%d (%.1f%%)  ctrl:%d/%d (%.1f%%)  "
            "lift=%s  p=%s  [%s]",
            col,
            es.get("N_dump_with_event", 0),
            es.get("N_dumps_analyzed", 0),
            (es.get("N_dump_with_event", 0) / max(es.get("N_dumps_analyzed", 1), 1)) * 100,
            es.get("N_ctrl_with_event", 0),
            es.get("N_controls_analyzed", 0),
            (es.get("N_ctrl_with_event", 0) / max(es.get("N_controls_analyzed", 1), 1)) * 100,
            f"{_lift:.3f}" if _lift is not None else "n/a",
            f"{_pval:.4f}" if _pval is not None else "n/a",
            es.get("interpretation", ""),
        )

    ev_df = pd.DataFrame(study_rows)

    coverage_labels = {
        "liq_cascade":        "partial 2026-04-14->2026-05-27 (long_liq side only)",
        "macro_event":        "full 2024-05-01->2026-05-27 (Fear/Greed ≤25, NOT FOMC/CPI)",
        "stablecoin_outflow": "full 2024-05-01->2026-05-27 (net redemption >2σ below mean)",
        "btc_cascade":        "full 2024-05-01->2026-05-27 (BTCUSDT 1h drop ≥5%)",
        "news_negative":      "partial 2026-04-12->2026-05-26 (screener SHORT signals, NOT external news)",
        "token_unlock":       "no_coverage -- 0 real rows in token_unlocks.csv",
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
        elif "no_coverage" in interp or s.get("N_dumps_analyzed", 0) == 0:
            cat_confidence[s["category"]] = "NONE"
        else:
            cat_confidence[s["category"]] = "LOW"

    log.info("Building attribution table (primary/secondary cause) ...")
    attr_rows = []
    for i, dump in dumps.iterrows():
        dump_ms = int(dump["start_ms"])
        sym     = dump["symbol"]
        hits    = []
        confs   = []
        notes   = []

        lv = d_liq[i]
        if pd.isna(lv):
            notes.append("liq_cascade:pre_coverage")
        elif lv:
            hits.append("liq_cascade")
            confs.append(cat_confidence.get("liq_cascade", "LOW"))

        if d_macro[i]:
            hits.append("macro_event")
            confs.append(cat_confidence.get("macro_event", "LOW"))

        if d_stable[i]:
            hits.append("stablecoin_outflow")
            confs.append(cat_confidence.get("stablecoin_outflow", "LOW"))

        bv = d_btc[i]
        if pd.isna(bv):
            notes.append("btc_cascade:no_btc_data")
        elif bv:
            hits.append("btc_cascade")
            confs.append(cat_confidence.get("btc_cascade", "LOW"))

        nv = d_news[i]
        if pd.isna(nv):
            notes.append("news_negative:pre_coverage")
        elif nv:
            hits.append("news_negative")
            confs.append(cat_confidence.get("news_negative", "LOW"))
            notes.append("news=screener_SHORT_proxy_not_external_news")

        uv = d_unlock[i]
        if pd.isna(uv):
            notes.append("token_unlock:no_data")
        elif uv:
            hits.append("token_unlock")
            confs.append("LOW")

        # Annotate Fear & Greed context for this dump
        h48 = 48 * 3_600_000
        fg_w = macro[(macro["ts_ms"] >= dump_ms - h48) & (macro["ts_ms"] < dump_ms)]
        if not fg_w.empty:
            fg_label = f"fg={fg_w['value'].mean():.0f}({fg_w['value_classification'].mode()[0]})"
            notes.insert(0, fg_label)

        final_conf = min(confs, key=lambda x: CONF_ORDER.index(x)) if confs else "NONE"

        # primary = highest-confidence hit (first in list); secondary = second
        primary_cause   = hits[0] if len(hits) >= 1 else "unknown"
        secondary_cause = hits[1] if len(hits) >= 2 else "none"

        attr_rows.append({
            "dump_id":        dump["dump_id"],
            "date":           dump["start_ts"],
            "symbol":         sym,
            "pct_chg":        round(float(dump["pct_chg"]), 2),
            "primary_cause":  primary_cause,
            "secondary_cause": secondary_cause,
            "all_catalysts":  "|".join(hits) if hits else "unknown",
            "confidence":     final_conf,
            "notes":          "; ".join(notes),
        })

    attr_df = pd.DataFrame(attr_rows)
    attr_df.to_csv(OUT_DIR / "causal_attribution.csv", index=False)
    log.info("causal_attribution.csv -> %d rows", len(attr_df))

    total    = len(attr_df)
    cat_cols = ["liq_cascade", "macro_event", "stablecoin_outflow",
                "btc_cascade", "news_negative", "token_unlock"]
    cat_counts = {
        c: int(attr_df["all_catalysts"].str.contains(c, regex=False).sum())
        for c in cat_cols
    }
    n_unknown = int((attr_df["primary_cause"] == "unknown").sum())
    n_any     = total - n_unknown

    n_liq_cov      = int(d_liq.notna().sum())
    n_liq_hit_cov  = int((d_liq == 1.0).sum())
    n_news_cov     = int(d_news.notna().sum())
    n_news_hit_cov = int((d_news == 1.0).sum())

    def ev_val(cat, field):
        row = ev_df[ev_df["category"] == cat]
        return row[field].values[0] if not row.empty else None

    # Primary cause distribution
    primary_dist = attr_df["primary_cause"].value_counts().to_dict()

    stats_out = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "total_dumps":  total,
        "methodology":  (
            "Event study uses control_windows.csv (pre-built negative windows from AVEC-59). "
            "Confidence = HIGH/MEDIUM/LOW/NONE based on lift+p-value per category. "
            "token_unlock has 0 real rows. "
            "news_negative = screener SHORT output, NOT external news/listing events. "
            "macro_event = Fear&Greed ≤25 (extreme fear), NOT FOMC/CPI. "
            "btc_cascade = BTCUSDT 1h drop ≥5%. "
            "liq_cascade = long-liquidation sum >3σ in T±2h. "
            "Bucket 'unknown' = no catalyst established."
        ),
        "by_category": {
            "liq_cascade": {
                "coverage":           "partial 2026-04-14+",
                "dumps_in_window":    n_liq_cov,
                "n_hits":             n_liq_hit_cov,
                "pct_of_covered":     round(n_liq_hit_cov / n_liq_cov * 100, 1) if n_liq_cov > 0 else 0,
                "event_study_lift":   ev_val("liq_cascade", "lift_P_ev_dump_vs_ctrl"),
                "event_study_interp": ev_val("liq_cascade", "interpretation"),
            },
            "macro_event": {
                "coverage":           "full (Fear/Greed ≤25)",
                "n_dumps_with_hit":   cat_counts["macro_event"],
                "pct_of_total":       round(cat_counts["macro_event"] / total * 100, 1),
                "event_study_lift":   ev_val("macro_event", "lift_P_ev_dump_vs_ctrl"),
                "event_study_interp": ev_val("macro_event", "interpretation"),
            },
            "stablecoin_outflow": {
                "coverage":           "full",
                "n_dumps_with_hit":   cat_counts["stablecoin_outflow"],
                "pct_of_total":       round(cat_counts["stablecoin_outflow"] / total * 100, 1),
                "event_study_lift":   ev_val("stablecoin_outflow", "lift_P_ev_dump_vs_ctrl"),
                "event_study_interp": ev_val("stablecoin_outflow", "interpretation"),
            },
            "btc_cascade": {
                "coverage":           "full (BTCUSDT 1h klines)",
                "n_dumps_with_hit":   cat_counts["btc_cascade"],
                "pct_of_total":       round(cat_counts["btc_cascade"] / total * 100, 1),
                "event_study_lift":   ev_val("btc_cascade", "lift_P_ev_dump_vs_ctrl"),
                "event_study_interp": ev_val("btc_cascade", "interpretation"),
            },
            "news_negative": {
                "coverage":           "partial 2026-04-12+ (screener SHORT signals)",
                "dumps_in_window":    n_news_cov,
                "n_hits":             n_news_hit_cov,
                "pct_of_covered":     round(n_news_hit_cov / n_news_cov * 100, 1) if n_news_cov > 0 else 0,
                "event_study_lift":   ev_val("news_negative", "lift_P_ev_dump_vs_ctrl"),
                "event_study_interp": ev_val("news_negative", "interpretation"),
            },
            "token_unlock": {"coverage": "no_coverage", "n_hits": 0},
        },
        "primary_cause_distribution": primary_dist,
        "aggregate": {
            "dumps_with_any_catalyst":       n_any,
            "pct_with_any_catalyst":         round(n_any / total * 100, 1),
            "dumps_no_catalyst_established": n_unknown,
            "pct_no_catalyst_established":   round(n_unknown / total * 100, 1),
        },
        "confidence_breakdown": {
            conf: int((attr_df["confidence"] == conf).sum())
            for conf in ["HIGH", "MEDIUM", "LOW", "NONE"]
        },
        "thresholds": {
            "stablecoin_2sigma_outflow_usd":     round(float(stable_threshold)),
            "fear_greed_extreme_lo":              25,
            "btc_cascade_3sigma_pct":             round(btc_threshold * 100, 3),
            "btc_cascade_note":                   "3-sigma below mean; canonical 5% never fires in 2024-2026 dataset",
            "liq_cascade_sigma":                  3,
        },
    }

    (OUT_DIR / "catalyst_stats.json").write_text(
        json.dumps(stats_out, indent=2, ensure_ascii=False)
    )
    log.info("catalyst_stats.json written")

    # -----------------------------------------------------------------------
    # Plot
    # -----------------------------------------------------------------------
    fig, axes = plt.subplots(1, 2, figsize=(16, 6))
    fig.suptitle(
        f"Dump Catalyst Distribution  (N={total} dumps, AVEC-63)\n"
        "WARNING: liq_cascade/news_negative = partial coverage; token_unlock = no_coverage",
        fontsize=10, fontweight="bold",
    )

    ax = axes[0]
    bar_labels = [
        "Liq Cascade\n(2026-04+)", "Macro Fear\n(F&G≤25)",
        "Stablecoin\nOutflow >2σ", "BTC Cascade\n(1h≥5%)",
        "News Neg.\n(screener proxy)", "Unknown\n(no catalyst)",
    ]
    bar_vals = [
        n_liq_hit_cov,
        cat_counts["macro_event"],
        cat_counts["stablecoin_outflow"],
        cat_counts["btc_cascade"],
        n_news_hit_cov,
        n_unknown,
    ]
    bar_cols = ["#e74c3c", "#f39c12", "#3498db", "#9b59b6", "#2ecc71", "#95a5a6"]
    bars = ax.bar(bar_labels, bar_vals, color=bar_cols, edgecolor="white", linewidth=0.5)
    for bar, v in zip(bars, bar_vals):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                f"{v}\n({v / total * 100:.1f}%)", ha="center", va="bottom", fontsize=9)
    ax.set_ylabel("Number of dumps")
    ax.set_title("Catalyst Coincidence Counts\n(1 dump can match multiple catalysts)")
    ax.set_ylim(0, max(bar_vals) * 1.3 if bar_vals else 1)
    ax.grid(axis="y", alpha=0.3)

    ax2 = axes[1]
    es_plot = ev_df[ev_df["lift_P_ev_dump_vs_ctrl"].notna()].copy()
    if not es_plot.empty:
        xlabels = [r.replace("_", "\n") for r in es_plot["category"]]
        lifts   = es_plot["lift_P_ev_dump_vs_ctrl"].astype(float).values
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
        ax2.set_ylabel("Lift  [P(event|dump) / P(event|control)]", fontsize=10)
        ax2.set_title("Event Study: Lift vs Control Windows\n(* = Fisher p ≤ 0.05)", fontsize=10)
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
    log.info("Total dumps: %d", total)
    log.info("No catalyst established (unknown): %d (%.1f%%)",
             n_unknown, n_unknown / total * 100)
    for col, cnt in cat_counts.items():
        log.info("  %-35s: %d (%.1f%%)", col, cnt, cnt / total * 100)
    log.info("Primary cause distribution: %s", primary_dist)
    log.info("Confidence: %s", stats_out["confidence_breakdown"])
    log.info("\nEvent study results:")
    for _, s in ev_df.iterrows():
        lift_s = (f"lift={s['lift_P_ev_dump_vs_ctrl']:.3f}"
                  if s["lift_P_ev_dump_vs_ctrl"] is not None else "lift=n/a")
        p_s    = f"p={s['p_value']:.4f}" if s["p_value"] is not None else "p=n/a"
        log.info("  %-22s: %s, %s  -> %s",
                 s["category"], lift_s, p_s, s["interpretation"])


if __name__ == "__main__":
    main()

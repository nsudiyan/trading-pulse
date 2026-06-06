#!/usr/bin/env python3
"""
backtests/v1_real_factors_oos.py — ЧЕСТНЫЙ OOS-тест РЕАЛЬНОГО пути USE_VALIDATED_V1.

Чинит ключевой дефект AVEA-81: тот ре-скорил resolved.csv через signal_weights.json
(старые фичи скринера), хотя в live под флагом USE_VALIDATED_V1 работают ДРУГИЕ факторы
(vol_spike_count, vol_ratio_1h/4h, btc_trend, price_vs_high_7d, stablecoin_inflow),
и в resolved.csv этих колонок нет.

Здесь мы применяем ТОЧНО те же правила, что в pump_detector.py:1234-1304, к
pump_analysis/feature_matrix.csv — у которого есть и факторы, и метка пампа (label),
и готовый временной сплит train (≤2025-10-31) / test (≥2025-11-01).

Всё из реального CSV. Никаких вызовов API. Воспроизводимо: python3 backtests/v1_real_factors_oos.py
"""
from __future__ import annotations
import json
from pathlib import Path
import pandas as pd

BASE = Path(__file__).parent.parent
FM = BASE / "pump_analysis" / "feature_matrix.csv"
OUT_JSON = BASE / "backtests" / "v1_real_factors_oos.json"
OUT_MD = BASE / "backtests" / "V1_REAL_FACTORS_OOS_REPORT.md"

# ── Точные правила из pump_detector.py:1234-1304 (signal_type == "pump") ──────────
# (factor_name, predicate(row) -> bool, score_delta, live_status)
RULES = [
    ("vol_spike_count>=1",   lambda r: r["vol_spike_count"] >= 1,        +15, "live"),
    ("vol_ratio_1h>1.0",     lambda r: r["vol_ratio_1h"] > 1.0,         +10, "live"),
    ("vol_ratio_4h>1.0",     lambda r: r["vol_ratio_4h"] > 1.0,          +8, "live"),
    ("btc_trend_1h>0",       lambda r: r["btc_trend_1h"] > 0,           -12, "live (proxy btc_4h)"),
    ("price_vs_high_7d>0.95",lambda r: r["price_vs_high_7d"] > 0.95,     -8, "live"),
    ("stablecoin_inflow==1", lambda r: r["catalyst_hit_stablecoin_inflow"] == 1, +20, "COMMENTED OUT"),
]


def lift_table(df: pd.DataFrame, base_rate: float) -> list[dict]:
    out = []
    for name, pred, delta, status in RULES:
        mask = df.apply(pred, axis=1)
        n = int(mask.sum())
        if n == 0:
            out.append({"factor": name, "delta": delta, "status": status,
                        "n_flag": 0, "p_pump": None, "lift": None})
            continue
        p = float(df.loc[mask, "label"].mean())
        out.append({
            "factor": name, "delta": delta, "status": status,
            "n_flag": n,
            "p_pump": round(p, 4),
            "lift": round(p / base_rate, 3) if base_rate > 0 else None,
        })
    return out


def v1_score(df: pd.DataFrame, include_stablecoin: bool) -> pd.Series:
    s = pd.Series(0, index=df.index, dtype=float)
    for name, pred, delta, status in RULES:
        if not include_stablecoin and status == "COMMENTED OUT":
            continue
        s = s + df.apply(pred, axis=1).astype(float) * delta
    return s


def threshold_sweep(df: pd.DataFrame, scores: pd.Series, base_rate: float) -> list[dict]:
    """Если бы фильтровали по v1_score >= thr: precision (P пампа) и recall (доля пойманных пампов)."""
    total_pumps = int(df["label"].sum())
    out = []
    for thr in [-20, -10, 0, 8, 10, 18, 23, 25, 33, 41]:
        sel = scores >= thr
        n = int(sel.sum())
        if n == 0:
            out.append({"thr": thr, "n_selected": 0, "precision": None, "recall": None, "lift": None})
            continue
        caught = int(df.loc[sel, "label"].sum())
        prec = caught / n
        rec = caught / total_pumps if total_pumps else None
        out.append({
            "thr": thr, "n_selected": n,
            "precision": round(prec, 4),
            "recall": round(rec, 4) if rec is not None else None,
            "lift": round(prec / base_rate, 3) if base_rate > 0 else None,
        })
    return out


def decile_table(df: pd.DataFrame, scores: pd.Series) -> list[dict]:
    tmp = pd.DataFrame({"score": scores, "label": df["label"].values})
    try:
        tmp["bucket"] = pd.qcut(tmp["score"].rank(method="first"), 10, labels=False)
    except Exception:
        return []
    g = tmp.groupby("bucket").agg(n=("label", "size"), p_pump=("label", "mean"),
                                  score_min=("score", "min"), score_max=("score", "max"))
    return [{"decile": int(i), "n": int(r.n), "p_pump": round(float(r.p_pump), 4),
             "score_range": [float(r.score_min), float(r.score_max)]} for i, r in g.iterrows()]


def main():
    df = pd.read_csv(FM)
    train = df[df["split"] == "train"].copy()
    test = df[df["split"] == "test"].copy()

    result = {"dataset": str(FM.name), "n_total": len(df),
              "n_train": len(train), "n_test": len(test),
              "base_rate_train": round(float(train["label"].mean()), 4),
              "base_rate_test": round(float(test["label"].mean()), 4),
              "splits": {}}

    for label, sub in [("train", train), ("test", test)]:
        br = float(sub["label"].mean())
        sc_full = v1_score(sub, include_stablecoin=True)
        sc_nostab = v1_score(sub, include_stablecoin=False)
        result["splits"][label] = {
            "base_rate": round(br, 4),
            "per_factor_lift": lift_table(sub, br),
            "score_deciles_full": decile_table(sub, sc_full),
            "threshold_sweep_full": threshold_sweep(sub, sc_full, br),
            "threshold_sweep_no_stablecoin": threshold_sweep(sub, sc_nostab, br),
        }

    OUT_JSON.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    # ── Markdown ─────────────────────────────────────────────────────────────────
    md = []
    md.append("# V1 РЕАЛЬНЫЕ факторы — честный OOS-тест\n")
    md.append("**Что это:** OOS-валидация ТОЧНОГО кода `USE_VALIDATED_V1` "
              "(`pump_detector.py:1234-1304`) на `pump_analysis/feature_matrix.csv`.\n")
    md.append(f"**Данные:** {result['n_total']} окон (train={result['n_train']} ≤2025-10-31, "
              f"test={result['n_test']} ≥2025-11-01). Base rate пампа: "
              f"train {result['base_rate_train']:.1%} / test {result['base_rate_test']:.1%}.\n")
    md.append("> Это валидация предсказания **«будет ли памп»** (label), а не P&L "
              "конкретной сделки. Фундамент модуля знаний.\n")

    for label in ["train", "test"]:
        s = result["splits"][label]
        tag = "ТРЕНИРОВКА (in-sample)" if label == "train" else "TEST — OOS (главное)"
        md.append(f"\n## {tag}  · base rate {s['base_rate']:.1%}\n")
        md.append("### Per-factor lift (P(памп|флаг) / base rate)\n")
        md.append("| Фактор | Δscore | live-статус | n с флагом | P(памп) | Lift |")
        md.append("|---|---|---|---|---|---|")
        for r in s["per_factor_lift"]:
            p = f"{r['p_pump']:.1%}" if r["p_pump"] is not None else "—"
            lift = f"{r['lift']:.2f}" if r["lift"] is not None else "—"
            md.append(f"| {r['factor']} | {r['delta']:+d} | {r['status']} | {r['n_flag']} | {p} | {lift} |")
        md.append("\n### Селективность: фильтр по v1_score ≥ порог (с stablecoin)\n")
        md.append("| Порог | Отобрано | Precision (P памп) | Recall | Lift |")
        md.append("|---|---|---|---|---|")
        for r in s["threshold_sweep_full"]:
            if r["precision"] is None:
                continue
            md.append(f"| {r['thr']:+d} | {r['n_selected']} | {r['precision']:.1%} | "
                      f"{r['recall']:.1%} | {r['lift']:.2f} |")

    # marginal value of stablecoin (test)
    md.append("\n## Маржинальная ценность stablecoin_inflow (test, OOS)\n")
    md.append("Сравнение selectivity-кривой С и БЕЗ закомментированного фактора:\n")
    md.append("| Порог | Precision С stablecoin | Precision БЕЗ |")
    md.append("|---|---|---|")
    sw_full = {r["thr"]: r for r in result["splits"]["test"]["threshold_sweep_full"]}
    sw_no = {r["thr"]: r for r in result["splits"]["test"]["threshold_sweep_no_stablecoin"]}
    for thr in sorted(set(sw_full) & set(sw_no)):
        a, b = sw_full[thr], sw_no[thr]
        if a["precision"] is None or b["precision"] is None:
            continue
        md.append(f"| {thr:+d} | {a['precision']:.1%} (n={a['n_selected']}) | "
                  f"{b['precision']:.1%} (n={b['n_selected']}) |")

    md.append("\n## Воспроизводимость\n")
    md.append("`python3 backtests/v1_real_factors_oos.py` → `backtests/v1_real_factors_oos.json`\n")
    OUT_MD.write_text("\n".join(md))

    # ── Console summary ──────────────────────────────────────────────────────────
    print(f"n_total={result['n_total']}  train={result['n_train']}  test={result['n_test']}")
    print(f"base rate: train {result['base_rate_train']:.1%}  test {result['base_rate_test']:.1%}\n")
    for label in ["train", "test"]:
        s = result["splits"][label]
        print(f"=== {label.upper()} (base {s['base_rate']:.1%}) — per-factor lift ===")
        for r in s["per_factor_lift"]:
            p = f"{r['p_pump']:.1%}" if r["p_pump"] is not None else "  — "
            lift = f"{r['lift']:.2f}" if r["lift"] is not None else " — "
            print(f"  {r['factor']:24s} Δ{r['delta']:+3d} [{r['status']:14s}] "
                  f"n={r['n_flag']:4d}  P={p:>6s}  lift={lift}")
        print(f"--- {label.upper()} threshold sweep (v1_score, with stablecoin) ---")
        for r in s["threshold_sweep_full"]:
            if r["precision"] is None:
                continue
            print(f"  thr {r['thr']:+3d}: n={r['n_selected']:4d}  "
                  f"prec={r['precision']:.1%}  recall={r['recall']:.1%}  lift={r['lift']:.2f}")
        print()
    print(f"Saved: {OUT_JSON.name}, {OUT_MD.name}")


if __name__ == "__main__":
    main()

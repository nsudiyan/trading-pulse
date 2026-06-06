#!/usr/bin/env python3
"""
backtests/second_filter_search.py — поиск 2-го ОРТОГОНАЛЬНОГО валидированного памп-фильтра.

Edge объёмного ядра тонкий (~+0.12R) → нужно СТАКАТЬ. Ищем фактор, который добавляет
предсказательную силу ПОВЕРХ объёма, на OOS (test split), и при этом чистый.

Критерии «годного 2-го фильтра»:
  1) standalone lift > 1 на TEST (предсказывает памп сам по себе),
  2) низкая корреляция с vol_ratio_1h (ОРТОГОНАЛЕН — даёт новую инфу),
  3) УСЛОВНЫЙ lift > 1 внутри high-vol сабсета на TEST (улучшает поверх объёма),
  4) достаточный n, знак совпадает train↔test (не оверфит).

ИСКЛЮЧЕНЫ (по находкам аудита, НЕ тестируем — были бы фейк):
  oi_chg_*/oi_oi_btc_corr (lookahead LA1/UNV1), catalyst_hit_* (утечка метки),
  hour_of_day/price_vs_high_7d/btc_trend_1h (разоблачены OOS).
Чистые на момент t: funding_last/funding_trend_3 (последний СЕТТЛ известен), cvd_*, liq_ratio.

Всё из feature_matrix.csv. Воспроизводимо.
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np, pandas as pd
from scipy.stats import spearmanr

BASE = Path(__file__).parent.parent
FM = BASE / "pump_analysis" / "feature_matrix.csv"
OUT = BASE / "backtests" / "SECOND_FILTER_REPORT.md"

# vol_score (валидированное ядро): +15 spike>=1, +10 vr1>1, +8 vr4>1
def vol_score(df):
    return (15*(df.vol_spike_count >= 1) + 10*(df.vol_ratio_1h > 1.0) + 8*(df.vol_ratio_4h > 1.0))

# Кандидаты: (имя, функция-предикат(df)->bool-маска). Только ЧИСТЫЕ факторы.
CANDIDATES = {
    "funding_last<0 (шорты платят)":      lambda d: d.funding_last < 0,
    "funding_last<-0.0003":               lambda d: d.funding_last < -0.0003,
    "funding_last>+0.0005 (перегрев лонг)":lambda d: d.funding_last > 0.0005,
    "funding_trend_3<0 (падает)":         lambda d: d.funding_trend_3 < 0,
    "cvd_div_1h>0":                       lambda d: d.cvd_div_1h > 0,
    "cvd_div_1h<0":                       lambda d: d.cvd_div_1h < 0,
    "price_vs_cvd==1 (дивергенция)":      lambda d: d.price_vs_cvd == 1,
    "liq_ratio>0.6 (лонг-ликвид доминир)":lambda d: d.liq_ratio > 0.6,
    "liq_ratio<0.4 (шорт-ликвид доминир)":lambda d: d.liq_ratio < 0.4,
}


def lift(mask_pump_subset, base):
    """P(pump|mask) и lift над base; n."""
    n = int(mask_pump_subset[0])
    if n == 0:
        return None
    p = mask_pump_subset[1] / n
    return {"n": n, "p_pump": round(p, 4), "lift": round(p/base, 3) if base > 0 else None}


def eval_feature(df, mask, base):
    sel = df[mask]
    if len(sel) == 0:
        return None
    p = sel.label.mean()
    return {"n": int(len(sel)), "p_pump": round(float(p), 4),
            "lift": round(float(p)/base, 3) if base > 0 else None}


def main():
    df = pd.read_csv(FM)
    df["vscore"] = vol_score(df)
    train = df[df.split == "train"].copy()
    test = df[df.split == "test"].copy()
    br_tr, br_te = float(train.label.mean()), float(test.label.mean())

    # high-vol сабсеты
    hv_tr = train[train.vscore >= 23]
    hv_te = test[test.vscore >= 23]
    br_hv_te = float(hv_te.label.mean())   # base precision внутри high-vol на TEST
    br_hv_tr = float(hv_tr.label.mean())

    rows = []
    for name, pred in CANDIDATES.items():
        # standalone lift на TEST
        sa_te = eval_feature(test, pred(test), br_te)
        sa_tr = eval_feature(train, pred(train), br_tr)
        # корреляция флага с vol_ratio_1h (ортогональность) на TEST
        try:
            corr = float(spearmanr(pred(test).astype(int), test.vol_ratio_1h).correlation)
        except Exception:
            corr = None
        # УСЛОВНЫЙ lift внутри high-vol на TEST (улучшает ли поверх объёма)
        cond_te = eval_feature(hv_te, pred(hv_te), br_hv_te)
        cond_tr = eval_feature(hv_tr, pred(hv_tr), br_hv_tr)
        rows.append({
            "feature": name,
            "sa_lift_train": sa_tr["lift"] if sa_tr else None,
            "sa_lift_test":  sa_te["lift"] if sa_te else None,
            "sa_n_test":     sa_te["n"] if sa_te else 0,
            "corr_vol":      round(corr, 3) if corr is not None else None,
            "cond_p_test":   cond_te["p_pump"] if cond_te else None,   # precision внутри high-vol с флагом
            "cond_lift_test":cond_te["lift"] if cond_te else None,      # >1 = улучшает поверх объёма
            "cond_n_test":   cond_te["n"] if cond_te else 0,
            "cond_lift_train":cond_tr["lift"] if cond_tr else None,
        })

    res = {"base_rate_test": round(br_te, 4),
           "highvol_base_precision_test": round(br_hv_te, 4),
           "highvol_n_test": int(len(hv_te)),
           "candidates": rows}
    (BASE / "backtests" / "second_filter_search.json").write_text(json.dumps(res, indent=2, ensure_ascii=False))

    # ── вывод ──
    md = ["# Поиск 2-го ортогонального памп-фильтра (OOS)\n",
          f"base rate (test) = {br_te:.1%}; precision внутри high-vol(vscore≥23, n={len(hv_te)}) = **{br_hv_te:.1%}**.\n",
          "Годный фильтр: sa_lift_test>1 И |corr_vol| низкая И cond_lift_test>1 (улучшает поверх объёма) И знак train↔test стабилен.\n",
          "| Фактор | SA lift tr→te | n_te | corr с объёмом | усл. precision(te) | усл. lift te (tr) |",
          "|---|---|---|---|---|---|"]
    # сортируем по cond_lift_test
    for r in sorted(rows, key=lambda x: -(x["cond_lift_test"] or 0)):
        md.append(f"| {r['feature']} | {r['sa_lift_train']}→{r['sa_lift_test']} | {r['sa_n_test']} | "
                  f"{r['corr_vol']} | {r['cond_p_test']} | **{r['cond_lift_test']}** ({r['cond_lift_train']}) |")
    md.append("\n> УСЛ. lift > 1.0 + стабилен train↔te + ортогонален (corr~0) = кандидат в стакинг.")
    md.append("> УСЛ. lift ≈ 1.0 или знак скачет = НЕ добавляет поверх объёма (объём уже всё забрал).")
    OUT.write_text("\n".join(md))

    print(f"base rate test={br_te:.1%}  | high-vol precision test={br_hv_te:.1%} (n={len(hv_te)})\n")
    print(f"{'feature':40s} {'SAtr':>5s} {'SAte':>5s} {'nTe':>5s} {'corrVol':>8s} {'condLte':>8s} {'condLtr':>8s}")
    for r in sorted(rows, key=lambda x: -(x["cond_lift_test"] or 0)):
        print(f"{r['feature']:40s} {str(r['sa_lift_train']):>5s} {str(r['sa_lift_test']):>5s} "
              f"{r['sa_n_test']:>5d} {str(r['corr_vol']):>8s} {str(r['cond_lift_test']):>8s} {str(r['cond_lift_train']):>8s}")
    print(f"\nSaved: {OUT.name}, second_filter_search.json")


if __name__ == "__main__":
    main()

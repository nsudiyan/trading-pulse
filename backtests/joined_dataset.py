#!/usr/bin/env python3
"""
backtests/joined_dataset.py — ШАГ 5 (keystone): соединённый датасет.

Для каждого ЛОНГ-сигнала из resolved.csv считаем V1 объёмные факторы ВАЛИДИРОВАННОЙ
статистикой (переиспользуем pump_analysis/feature_engineer.klines_features —
30-дн rolling median 1h-объёма, строго pre-t) на момент входа, и джойним с уже
реализованным MFE/MAE из того же resolved.csv.

Денежный вопрос: у входов с высоким объёмным конфлюенсом — меньше просадка (MAE)
и плюсовые выходы, чем у остальных? Это проверяет, конвертируется ли OOS-edge в деньги.

Только офлайн (уже скачанные klines_1h), без сети, без API. Воспроизводимо.
"""
from __future__ import annotations
import csv, sys, json
from pathlib import Path
from datetime import datetime, timezone
from statistics import mean
import numpy as np

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE / "pump_analysis"))
import feature_engineer as fe  # noqa: E402
fe.KLINES_DIR = BASE / "pump_analysis" / "klines_1h"

RESOLVED = BASE / "outcomes" / "resolved.csv"
OUT_CSV = BASE / "backtests" / "joined_dataset.csv"
OUT_MD = BASE / "backtests" / "JOINED_DATASET_REPORT.md"
OUT_JSON = BASE / "backtests" / "joined_dataset.json"

CSV_FIELDS = [
    "run_ts","symbol","setup","score","grade","pump_score","direction",
    "price_entry","stop","tp1","tp2","funding","oi_24h_pct","mtf_bull","mtf_bear",
    "rsi_1h","cvd_kline","cvd_trade","ema_bull_1h","ema_bull_4h","choch_bull_1h",
    "vwap_dev","rs_btc","resolve_4h_ts","price_4h","change_4h_pct","hit_tp1_4h",
    "hit_stop_4h","outcome_4h","resolve_24h_ts","price_24h","change_24h_pct",
    "hit_tp1_24h","hit_stop_24h","outcome_24h","bnb_cross_bonus","in_zone","whale_flag",
    "mfe_4h_pct","mae_4h_pct","mfe_24h_pct","mae_24h_pct","utc_hour","btc_trend_4h",
    "alt_breadth_pct","listing_age_days","avg_vol_7d_usd","sc_squeeze","sc_bos_fvg",
    "sc_range_sweep","sc_breakout","sc_short_dist","atr_at_entry","r_multiple_4h",
    "exit_reason_4h","r_multiple_24h","exit_reason_24h","choch_conviction",
    "exit_price_4h","exit_price_24h","hold_time_4h_min","hold_time_24h_min",
    "outcome_label_4h","outcome_label_24h","time_to_mfe_4h_h","time_to_mae_4h_h",
    "time_to_mfe_24h_h","time_to_mae_24h_h",
]


def f(v, d=None):
    try: return float(v)
    except (TypeError, ValueError): return d


def to_ms(run_ts: str):
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(run_ts.strip()[:19], fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def load_longs():
    rows = []
    with open(RESOLVED) as fh:
        r = csv.reader(fh); next(r)
        for line in r:
            if len(line) < 42: continue
            row = {fld: (line[i] if i < len(line) else "") for i, fld in enumerate(CSV_FIELDS)}
            if "ЛОН" in row.get("direction", ""):
                rows.append(row)
    return rows


def sim_fixed(fav, adv, t_fav, t_adv, sl_pct, tp_pct, change_close):
    hit_tp, hit_sl = fav >= tp_pct, adv >= sl_pct
    if hit_tp and hit_sl:
        if t_fav is not None and t_adv is not None:
            return tp_pct if t_fav <= t_adv else -sl_pct
        return -sl_pct
    if hit_tp: return tp_pct
    if hit_sl: return -sl_pct
    return change_close


def main():
    longs = load_longs()
    klcache = {}
    joined = []
    matched = price_ok = 0

    for row in longs:
        sym = row["symbol"]
        start_ms = to_ms(row["run_ts"])
        if start_ms is None:
            continue
        if sym not in klcache:
            klcache[sym] = fe.load_klines(sym)  # None if no file
        kl = klcache[sym]
        if kl is None:
            continue
        feat = fe.klines_features(kl, start_ms)
        if not feat or feat.get("vol_ratio_1h") is None or (isinstance(feat.get("vol_ratio_1h"), float) and np.isnan(feat["vol_ratio_1h"])):
            continue
        matched += 1

        # sanity: цена входа ≈ klines close на этом баре (валидация tz/символа)
        pos = fe._searchsorted_le(kl["ts"].values, start_ms - 1)
        kl_close = float(kl.iloc[pos]["close"]) if pos >= 0 else None
        pe = f(row["price_entry"])
        price_match = (kl_close and pe and abs(kl_close - pe) / pe < 0.05)
        if price_match: price_ok += 1

        vsc = feat["vol_spike_count"]
        vr1 = feat["vol_ratio_1h"]
        vr4 = feat["vol_ratio_4h"]
        # объёмный конфлюенс-скор (валидированное ядро, без режимо-нестабильных антисигналов)
        vol_score = (15 if vsc >= 1 else 0) + (10 if vr1 > 1.0 else 0) + (8 if (vr4 and vr4 > 1.0) else 0)

        joined.append({
            "symbol": sym, "run_ts": row["run_ts"], "setup": row["setup"],
            "score": f(row["score"]), "vol_spike_count": vsc,
            "vol_ratio_1h": round(vr1, 3), "vol_ratio_4h": round(vr4, 3) if vr4 else None,
            "vol_score": vol_score, "price_match": int(bool(price_match)),
            "price_entry": pe, "stop": f(row["stop"]),
            "mfe_4h": f(row["mfe_4h_pct"]), "mae_4h": f(row["mae_4h_pct"]),
            "mfe_24h": f(row["mfe_24h_pct"]), "mae_24h": f(row["mae_24h_pct"]),
            "change_4h": f(row["change_4h_pct"]), "change_24h": f(row["change_24h_pct"]),
            "t_mfe_4h": f(row["time_to_mfe_4h_h"]), "t_mae_4h": f(row["time_to_mae_4h_h"]),
            "t_mfe_24h": f(row["time_to_mfe_24h_h"]), "t_mae_24h": f(row["time_to_mae_24h_h"]),
            "outcome_4h": row["outcome_4h"], "outcome_24h": row["outcome_24h"],
        })

    # save joined csv
    if joined:
        with open(OUT_CSV, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(joined[0].keys()))
            w.writeheader(); w.writerows(joined)

    # ── анализ: high-vol vs low-vol ───────────────────────────────────────────────
    def adverse(v):  # mae хранится со знаком (отриц = вниз)
        return abs(v) if v is not None else None

    def bucket_stats(rows, H):
        mfe_c, mae_c, chg_c, tfav_c, tadv_c = (f"mfe_{H}", f"mae_{H}", f"change_{H}", f"t_mfe_{H}", f"t_mae_{H}")
        exits, maes, mfes = [], [], []
        for r in rows:
            fav, adv, chg = r[mfe_c], adverse(r[mae_c]), r[chg_c]
            if fav is None or adv is None: continue
            fav = max(fav, 0.0)
            entry, stop = r["price_entry"], r["stop"]
            sl = (entry - stop) / entry * 100 if (entry and stop and stop < entry) else 4.0
            sl = max(min(sl, 15.0), 1.5)
            exits.append(sim_fixed(fav, adv, r[tfav_c], r[tadv_c], sl, 12.0, chg if chg is not None else 0.0))
            maes.append(adv); mfes.append(fav)
        if not exits: return None
        return {
            "n": len(exits),
            "expectancy_pct": round(mean(exits), 3),
            "win_rate": round(sum(1 for x in exits if x > 0) / len(exits), 4),
            "avg_MAE_pct": round(mean(maes), 2),
            "avg_MFE_pct": round(mean(mfes), 2),
        }

    # бакеты по vol_score
    buckets = {
        "vol_score=0 (нет объёма)": [r for r in joined if r["vol_score"] == 0],
        "vol_score 10-18 (средн.)": [r for r in joined if 0 < r["vol_score"] <= 18],
        "vol_score 23-33 (высокий)": [r for r in joined if r["vol_score"] >= 23],
        "ALL": joined,
    }

    report = {"n_long_total": len(longs), "n_matched_klines": matched,
              "n_joined": len(joined), "price_match_rate": round(price_ok / matched, 3) if matched else None,
              "buckets": {}}
    for name, rows in buckets.items():
        report["buckets"][name] = {"n": len(rows), "4h": bucket_stats(rows, "4h"), "24h": bucket_stats(rows, "24h")}

    # корреляция vol_ratio_1h с MFE/MAE
    vr = np.array([r["vol_ratio_1h"] for r in joined if r["mfe_24h"] is not None])
    mfe = np.array([r["mfe_24h"] for r in joined if r["mfe_24h"] is not None])
    mae = np.array([adverse(r["mae_24h"]) for r in joined if r["mae_24h"] is not None])
    vr_m = np.array([r["vol_ratio_1h"] for r in joined if r["mae_24h"] is not None])
    from scipy.stats import spearmanr
    rho_mfe = float(spearmanr(vr, mfe).correlation) if len(vr) > 10 else None
    rho_mae = float(spearmanr(vr_m, mae).correlation) if len(vr_m) > 10 else None
    report["spearman_volratio1h_vs_MFE24h"] = round(rho_mfe, 3) if rho_mfe is not None else None
    report["spearman_volratio1h_vs_MAE24h"] = round(rho_mae, 3) if rho_mae is not None else None

    OUT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    # markdown
    md = ["# Шаг 5 — соединённый датасет (v1-факторы + реализованный P&L)\n",
          f"**Покрытие:** {len(longs)} ЛОНГ → {matched} с klines_1h → {len(joined)} в датасете. "
          f"price_match={report['price_match_rate']:.0%} (валидация tz/символа).\n",
          "> v1-факторы посчитаны ВАЛИДИРОВАННОЙ статистикой (30-дн медиана 1h, feature_engineer.klines_features). "
          "Выход: фикс TP+12%/SL(реальный стоп), order-aware. mfe/mae из resolved.csv.\n",
          "\n## Экзиты по бакетам объёмного конфлюенса\n",
          "| Бакет | N | Гор. | Expectancy %/сделка | WR | ср.MAE% | ср.MFE% |",
          "|---|---|---|---|---|---|---|"]
    for name, b in report["buckets"].items():
        for H in ["4h", "24h"]:
            s = b[H]
            if s:
                md.append(f"| {name} | {s['n']} | {H} | **{s['expectancy_pct']:+.2f}%** | {s['win_rate']:.1%} | {s['avg_MAE_pct']:.1f} | {s['avg_MFE_pct']:.1f} |")
    md.append(f"\n## Связь объёма с ходом (Spearman, 24h)\n")
    md.append(f"- vol_ratio_1h ↔ MFE: **{report['spearman_volratio1h_vs_MFE24h']}**")
    md.append(f"- vol_ratio_1h ↔ MAE (просадка): **{report['spearman_volratio1h_vs_MAE24h']}**")
    md.append(f"\n## Воспроизводимость\n`python3 backtests/joined_dataset.py`\n")
    OUT_MD.write_text("\n".join(md))

    # console
    print(f"LONG total={len(longs)}  matched_klines={matched}  joined={len(joined)}  "
          f"price_match={report['price_match_rate']}\n")
    print(f"{'bucket':28s} {'H':>4s} {'n':>5s} {'E%/tr':>8s} {'WR':>6s} {'MAE%':>7s} {'MFE%':>7s}")
    for name, b in report["buckets"].items():
        for H in ["4h", "24h"]:
            s = b[H]
            if s:
                print(f"{name:28s} {H:>4s} {s['n']:5d} {s['expectancy_pct']:+8.2f} "
                      f"{s['win_rate']*100:5.1f}% {s['avg_MAE_pct']:7.1f} {s['avg_MFE_pct']:7.1f}")
    print(f"\nSpearman vol_ratio_1h vs MFE24h={report['spearman_volratio1h_vs_MFE24h']}  "
          f"vs MAE24h={report['spearman_volratio1h_vs_MAE24h']}")
    print(f"Saved: {OUT_CSV.name}, {OUT_MD.name}, {OUT_JSON.name}")


if __name__ == "__main__":
    main()

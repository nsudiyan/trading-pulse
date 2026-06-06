#!/usr/bin/env python3
"""
backtests/shorts_joined.py — ЗЕРКАЛО шага 5 для ШОРТ-сигналов.

Соединённый датасет для ШОРТов: для каждого ШОРТ-сигнала из resolved.csv считаем
на момент входа (строго pre-t):
  • btc_cascade  — BTC 1h падение (порог -1.5%) через klines_1h/BTCUSDT (валидир. триггер, lift 5.12)
  • дамп-объёмные факторы (vol_ratio_1h / vol_spike_count) ВАЛИДИРОВАННОЙ дамп-статистикой
    (dump_feature_engineer.klines_features — 20-час rolling median 1h-объёма, в отличие от
    30-дн у пампов), джойним с уже реализованным MFE/MAE из того же resolved.csv.

btc_cascade определён ТОЧНО как в dump_causal_attributor.check_btc_cascade (валид. lift 5.12):
  порог = data-driven 3σ ниже среднего BTC 1h-доходности (close/open-1) ≈ -1.49%,
  считается на ВСЕЙ истории BTCUSDT 1h (2024-05..2026-05). Триггер = есть ли хоть один
  1h-бар <= порога. Окно: T-2h..T (только pre-t, БЕЗ lookahead; оригинал в каузальном
  атрибуторе использует T±2h, что для торгового триггера было бы заглядыванием вперёд).

СЕМАНТИКА MFE/MAE ДЛЯ ШОРТА (подтверждена сверкой с 5m-klines, см. отчёт):
  MFE = % падения цены ниже входа (БЛАГОПРИЯТНО для шорта = профит), хранится ПОЛОЖИТЕЛЬНЫМ.
  MAE = % роста цены выше входа (НЕБЛАГОПРИЯТНО для шорта = убыток), хранится ОТРИЦАТЕЛЬНЫМ.
  Семантика НАПРАВЛЕННАЯ относительно позиции (НЕ абсолютная). Sanity: price_match≈99%.

Только офлайн (уже скачанные klines_1h), без сети, без API. Воспроизводимо:
  python3 backtests/shorts_joined.py
"""
from __future__ import annotations
import csv, sys, json
from pathlib import Path
from datetime import datetime, timezone
from statistics import mean
import numpy as np

BASE = Path(__file__).parent.parent
# дамп-статистика (20h median) — переиспользуем валидированные функции
sys.path.insert(0, str(BASE / "dump_analysis"))
import dump_feature_engineer as dfe  # noqa: E402
dfe.KLINES_DIR = BASE / "pump_analysis" / "klines_1h"

RESOLVED = BASE / "outcomes" / "resolved.csv"
OUT_CSV = BASE / "backtests" / "shorts_joined.csv"
OUT_JSON = BASE / "backtests" / "shorts_joined.json"

H1_MS = 3_600_000
BTC_CASCADE_WINDOW_H = 2  # окно T-2h..T (pre-t, без lookahead)

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


def load_shorts():
    rows = []
    with open(RESOLVED) as fh:
        r = csv.reader(fh); next(r)
        for line in r:
            if len(line) < 42: continue
            row = {fld: (line[i] if i < len(line) else "") for i, fld in enumerate(CSV_FIELDS)}
            if "ШОРТ" in row.get("direction", ""):
                rows.append(row)
    return rows


def btc_cascade_threshold(btc_kl):
    """3σ ниже среднего BTC 1h intrabar (close/open-1) — как в check_btc_cascade."""
    chg = (btc_kl["close"].values / btc_kl["open"].values - 1.0)
    mu, sig = float(np.nanmean(chg)), float(np.nanstd(chg, ddof=1))
    return mu - 3 * sig, mu, sig


def btc_cascade_at(btc_kl, ts_arr, chg_arr, start_ms, threshold):
    """1 если есть BTC 1h-бар с (close/open-1) <= threshold в окне T-2h..T (pre-t).
    Возвращает (cascade_flag, min_chg_in_window) или (None,None) если нет покрытия."""
    lo = start_ms - BTC_CASCADE_WINDOW_H * H1_MS
    mask = (ts_arr >= lo) & (ts_arr < start_ms)
    w = chg_arr[mask]
    if w.size == 0:
        return None, None
    return int(float(w.min()) <= threshold), round(float(w.min()) * 100, 3)


def main():
    shorts = load_shorts()
    btc_kl = dfe.load_klines("BTCUSDT")
    if btc_kl is None:
        print("FATAL: BTCUSDT klines_1h не найдены — btc_cascade посчитать нельзя.")
        return
    btc_thr, btc_mu, btc_sig = btc_cascade_threshold(btc_kl)
    btc_ts = btc_kl["ts"].values
    btc_chg = (btc_kl["close"].values / btc_kl["open"].values - 1.0)
    print(f"btc_cascade 3σ threshold = {btc_thr*100:.3f}% (mu={btc_mu*100:.4f}% σ={btc_sig*100:.4f}%)")

    klcache = {}
    joined = []
    matched = price_ok = 0

    for row in shorts:
        sym = row["symbol"]
        start_ms = to_ms(row["run_ts"])
        if start_ms is None:
            continue
        if sym not in klcache:
            klcache[sym] = dfe.load_klines(sym)  # None if no file (klines ~99 символов)
        kl = klcache[sym]
        if kl is None:
            continue
        feat = dfe.klines_features(kl, start_ms)  # 20h-median дамп-статистика
        vr1 = feat.get("vol_ratio_1h")
        if not feat or vr1 is None or (isinstance(vr1, float) and np.isnan(vr1)):
            continue
        matched += 1

        # sanity: цена входа ≈ klines close на баре строго до входа (валидация tz/символа)
        pos = dfe._searchsorted_le(kl["ts"].values, start_ms - 1)
        kl_close = float(kl.iloc[pos]["close"]) if pos >= 0 else None
        pe = f(row["price_entry"])
        price_match = bool(kl_close and pe and abs(kl_close - pe) / pe < 0.05)
        if price_match: price_ok += 1

        vsc = feat["vol_spike_count"]
        # btc_cascade: 1h-бар <= 3σ-порога в окне T-2h..T (валид. триггер, lift 5.12)
        casc, btc_min = btc_cascade_at(btc_kl, btc_ts, btc_chg, start_ms, btc_thr)
        btc_cascade = casc if casc is not None else 0

        # дамп-объёмный конфлюенс-скор (валидированное ядро 20h-median)
        vol_score = (15 if vsc >= 1 else 0) + (12 if vr1 > 1.5 else (8 if vr1 > 1.0 else 0))

        joined.append({
            "symbol": sym, "run_ts": row["run_ts"], "setup": row["setup"],
            "score": f(row["score"]),
            "btc_min_1h_pct": btc_min,
            "btc_cascade": btc_cascade,
            "vol_spike_count": vsc, "vol_ratio_1h": round(vr1, 3),
            "vol_score": vol_score, "price_match": int(price_match),
            "price_entry": pe, "stop": f(row["stop"]),
            "funding": f(row["funding"]),
            # MFE/MAE направленные относительно ШОРТ-позиции (MFE=падение/профит +, MAE=рост/убыток -)
            "mfe_4h": f(row["mfe_4h_pct"]), "mae_4h": f(row["mae_4h_pct"]),
            "mfe_24h": f(row["mfe_24h_pct"]), "mae_24h": f(row["mae_24h_pct"]),
            "change_4h": f(row["change_4h_pct"]), "change_24h": f(row["change_24h_pct"]),
            "t_mfe_4h": f(row["time_to_mfe_4h_h"]), "t_mae_4h": f(row["time_to_mae_4h_h"]),
            "t_mfe_24h": f(row["time_to_mfe_24h_h"]), "t_mae_24h": f(row["time_to_mae_24h_h"]),
            "outcome_4h": row["outcome_4h"], "outcome_24h": row["outcome_24h"],
        })

    if joined:
        with open(OUT_CSV, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(joined[0].keys()))
            w.writeheader(); w.writerows(joined)

    # ── анализ: фикс-выход (order-aware) по сабсетам ──────────────────────────────
    # Для ШОРТА: fav (благоприятно) = MFE (падение, +). adv (просадка) = |MAE| (рост, abs).
    def adverse(v):  # mae хранится со знаком (-, т.к. рост против шорта)
        return abs(v) if v is not None else None

    def sim_fixed(fav, adv, t_fav, t_adv, sl_pct, tp_pct, change_close):
        # change_close для ШОРТА: профит = -change_pct (цена упала → плюс шорту)
        hit_tp, hit_sl = fav >= tp_pct, adv >= sl_pct
        if hit_tp and hit_sl:
            if t_fav is not None and t_adv is not None:
                return tp_pct if t_fav <= t_adv else -sl_pct
            return -sl_pct
        if hit_tp: return tp_pct
        if hit_sl: return -sl_pct
        return change_close

    def bucket_stats(rows, H):
        mfe_c, mae_c, chg_c, tfav_c, tadv_c = (f"mfe_{H}", f"mae_{H}", f"change_{H}", f"t_mfe_{H}", f"t_mae_{H}")
        exits, exits_R, maes, mfes = [], [], [], []
        for r in rows:
            fav, adv, chg = r[mfe_c], adverse(r[mae_c]), r[chg_c]
            if fav is None or adv is None: continue
            fav = max(fav, 0.0)
            entry, stop = r["price_entry"], r["stop"]
            # для ШОРТА stop ВЫШЕ входа: SL% = (stop-entry)/entry
            sl = (stop - entry) / entry * 100 if (entry and stop and stop > entry) else 4.0
            sl = max(min(sl, 15.0), 1.5)
            short_close = (-chg) if chg is not None else 0.0  # шорт зарабатывает на падении
            ret = sim_fixed(fav, adv, r[tfav_c], r[tadv_c], sl, 12.0, short_close)
            exits.append(ret); exits_R.append(ret / sl)
            maes.append(adv); mfes.append(fav)
        if not exits: return None
        return {
            "n": len(exits),
            "expectancy_pct": round(mean(exits), 3),
            "expectancy_R": round(mean(exits_R), 3),
            "win_rate": round(sum(1 for x in exits if x > 0) / len(exits), 4),
            "avg_MAE_pct": round(mean(maes), 2),
            "avg_MFE_pct": round(mean(mfes), 2),
        }

    buckets = {
        "btc_cascade=1": [r for r in joined if r["btc_cascade"] == 1],
        "btc_cascade=0": [r for r in joined if r["btc_cascade"] == 0],
        "high_dump_vol (vol_score>=23)": [r for r in joined if r["vol_score"] >= 23],
        "cascade OR high_vol": [r for r in joined if r["btc_cascade"] == 1 or r["vol_score"] >= 23],
        "ALL shorts": joined,
    }

    report = {"n_short_total": len(shorts), "n_matched_klines": matched,
              "n_joined": len(joined),
              "price_match_rate": round(price_ok / matched, 3) if matched else None,
              "btc_cascade_threshold_pct": round(btc_thr * 100, 3),
              "n_btc_cascade": sum(1 for r in joined if r["btc_cascade"] == 1),
              "buckets": {}}
    for name, rows in buckets.items():
        report["buckets"][name] = {"n": len(rows), "4h": bucket_stats(rows, "4h"), "24h": bucket_stats(rows, "24h")}

    # корреляция дамп-vol_ratio_1h с MFE/MAE (24h)
    from scipy.stats import spearmanr
    vr = np.array([r["vol_ratio_1h"] for r in joined if r["mfe_24h"] is not None])
    mfe = np.array([r["mfe_24h"] for r in joined if r["mfe_24h"] is not None])
    rho_mfe = float(spearmanr(vr, mfe).correlation) if len(vr) > 10 else None
    report["spearman_volratio1h_vs_MFE24h"] = round(rho_mfe, 3) if rho_mfe is not None else None

    OUT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    print(f"SHORT total={len(shorts)}  matched_klines={matched}  joined={len(joined)}  "
          f"price_match={report['price_match_rate']}  n_btc_cascade={report['n_btc_cascade']}\n")
    print(f"{'bucket':32s} {'H':>4s} {'n':>5s} {'E%/tr':>8s} {'E(R)':>7s} {'WR':>6s} {'MAE%':>7s} {'MFE%':>7s}")
    for name, b in report["buckets"].items():
        for H in ["4h", "24h"]:
            s = b[H]
            if s:
                er = s["expectancy_R"]
                print(f"{name:32s} {H:>4s} {s['n']:5d} {s['expectancy_pct']:+8.2f} "
                      f"{(er if er is not None else 0):+7.3f} {s['win_rate']*100:5.1f}% {s['avg_MAE_pct']:7.1f} {s['avg_MFE_pct']:7.1f}")
    print(f"\nSpearman dump_vol_ratio_1h vs MFE24h={report['spearman_volratio1h_vs_MFE24h']}")
    print(f"Saved: {OUT_CSV.name}, {OUT_JSON.name}")


if __name__ == "__main__":
    main()

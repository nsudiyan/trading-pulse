#!/usr/bin/env python3
"""
backtests/exit_ladder_sim.py — B1: сравнение стратегий выхода на реальных сигналах.

Тезис из signal_edge: ходы дозревают поздно (4ч слишком коротко; лонг к +12% идёт 24-72ч),
а фикс-TP режет хвост. Здесь проверяем это РЕАЛЬНЫМИ числами из resolved.csv:
сравниваем expectancy (средний % на сделку) для фикс-TP/SL на 4ч vs 24ч горизонте,
hold-to-close, и теоретический потолок (захват пика MFE).

ОГОВОРКА: это ПИКОВАЯ аппроксимация (mfe/mae = экстремумы за окно + порядок по
time_to_mfe/time_to_mae). Для трейлинга нужен полный путь по klines — это строгий
следующий шаг. Фикс-TP/SL и hold-to-close моделируются корректно из пик+порядок.

Только ЛОНГ (как в signal_weights — модель на лонгах). Воспроизводимо.
"""
from __future__ import annotations
import csv
import json
from pathlib import Path
from statistics import mean

BASE = Path(__file__).parent.parent
RESOLVED = BASE / "outcomes" / "resolved.csv"
OUT_MD = BASE / "backtests" / "EXIT_LADDER_REPORT.md"
OUT_JSON = BASE / "backtests" / "exit_ladder_sim.json"

CSV_FIELDS = [
    "run_ts", "symbol", "setup", "score", "grade", "pump_score", "direction",
    "price_entry", "stop", "tp1", "tp2",
    "funding", "oi_24h_pct", "mtf_bull", "mtf_bear", "rsi_1h", "cvd_kline", "cvd_trade",
    "ema_bull_1h", "ema_bull_4h", "choch_bull_1h", "vwap_dev", "rs_btc",
    "resolve_4h_ts", "price_4h", "change_4h_pct", "hit_tp1_4h", "hit_stop_4h", "outcome_4h",
    "resolve_24h_ts", "price_24h", "change_24h_pct", "hit_tp1_24h", "hit_stop_24h", "outcome_24h",
    "bnb_cross_bonus", "in_zone", "whale_flag",
    "mfe_4h_pct", "mae_4h_pct", "mfe_24h_pct", "mae_24h_pct",
    "utc_hour", "btc_trend_4h", "alt_breadth_pct", "listing_age_days", "avg_vol_7d_usd",
    "sc_squeeze", "sc_bos_fvg", "sc_range_sweep", "sc_breakout", "sc_short_dist",
    "atr_at_entry", "r_multiple_4h", "exit_reason_4h", "r_multiple_24h", "exit_reason_24h",
    "choch_conviction", "exit_price_4h", "exit_price_24h",
    "hold_time_4h_min", "hold_time_24h_min", "outcome_label_4h", "outcome_label_24h",
    "time_to_mfe_4h_h", "time_to_mae_4h_h", "time_to_mfe_24h_h", "time_to_mae_24h_h",
]


def f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def load():
    rows = []
    with open(RESOLVED) as fh:
        r = csv.reader(fh)
        next(r)
        for line in r:
            if len(line) < 42:  # must reach mfe/mae columns
                continue
            row = {fld: (line[i] if i < len(line) else "") for i, fld in enumerate(CSV_FIELDS)}
            rows.append(row)
    return rows


def sim_trade(fav, adv, t_fav, t_adv, sl_pct, tp_pct, change_close):
    """Один трейд. fav/adv = магнитуды (>0). Возвращает реализованный % хода."""
    hit_tp = fav >= tp_pct
    hit_sl = adv >= sl_pct
    if hit_tp and hit_sl:
        # оба достигнуты — кто первым по времени
        if t_fav is not None and t_adv is not None:
            return tp_pct if t_fav <= t_adv else -sl_pct
        return -sl_pct  # порядок неизвестен → пессимистично (как в win-fix)
    if hit_tp:
        return tp_pct
    if hit_sl:
        return -sl_pct
    return change_close  # ни TP ни SL → выход по закрытию горизонта


def main():
    rows = load()
    longs = [r for r in rows if "ЛОН" in r.get("direction", "")]

    # детект знака mae
    mae_vals = [f(r.get("mae_4h_pct")) for r in longs if f(r.get("mae_4h_pct")) is not None]
    frac_neg = sum(1 for v in mae_vals if v < 0) / len(mae_vals) if mae_vals else 0
    mae_signed = frac_neg > 0.6  # mae хранится со знаком (отриц = вниз)

    def adverse(v):
        x = f(v)
        if x is None:
            return None
        return abs(x) if mae_signed else (x if x >= 0 else abs(x))

    report = {"n_long_with_mfe": len(longs), "mae_signed": mae_signed, "scenarios": {}}

    TP_TARGETS = [8.0, 12.0, 15.0]
    results_md = ["# B1 — Сравнение стратегий выхода (ЛОНГ, реальные сигналы)\n",
                  f"**Данные:** `outcomes/resolved.csv`, {len(longs)} ЛОНГ-сделок с MFE/MAE. "
                  f"mae_signed={mae_signed}.\n",
                  "> ОГОВОРКА: пиковая аппроксимация (mfe/mae+порядок). Трейлинг требует полного пути по klines (следующий шаг). "
                  "Фикс-TP/SL и hold моделируются корректно.\n",
                  "\nExpectancy = средний реализованный % на сделку. WR = доля сделок с реализацией > 0.\n"]

    def stats(realized):
        realized = [x for x in realized if x is not None]
        if not realized:
            return None
        wr = sum(1 for x in realized if x > 0) / len(realized)
        return {"n": len(realized), "expectancy_pct": round(mean(realized), 3),
                "win_rate": round(wr, 4)}

    for tp in TP_TARGETS:
        block = {}
        for H, mfe_c, mae_c, chg_c, tfav_c, tadv_c in [
            ("4h", "mfe_4h_pct", "mae_4h_pct", "change_4h_pct", "time_to_mfe_4h_h", "time_to_mae_4h_h"),
            ("24h", "mfe_24h_pct", "mae_24h_pct", "change_24h_pct", "time_to_mfe_24h_h", "time_to_mae_24h_h"),
        ]:
            fixed, hold, ceiling = [], [], []
            for r in longs:
                entry = f(r.get("price_entry"))
                stop = f(r.get("stop"))
                fav = f(r.get(mfe_c))
                adv = adverse(r.get(mae_c))
                chg = f(r.get(chg_c))
                tfav = f(r.get(tfav_c))
                tadv = f(r.get(tadv_c))
                if entry is None or fav is None or adv is None:
                    continue
                fav = max(fav, 0.0)
                # SL% из реального стопа, fallback 4%
                if stop and entry and stop < entry:
                    sl_pct = (entry - stop) / entry * 100
                else:
                    sl_pct = 4.0
                sl_pct = max(min(sl_pct, 15.0), 1.5)  # клип в разумный диапазон
                if chg is None:
                    chg = 0.0
                fixed.append(sim_trade(fav, adv, tfav, tadv, sl_pct, tp, chg))
                # hold-to-close: только SL, иначе закрытие
                hold.append(-sl_pct if adv >= sl_pct and (tadv is None or tfav is None or tadv <= tfav) else chg)
                # ceiling: захват пика MFE если SL не выбил раньше
                if adv >= sl_pct and tadv is not None and tfav is not None and tadv < tfav:
                    ceiling.append(-sl_pct)
                else:
                    ceiling.append(fav)
            block[H] = {"fixed_tp": stats(fixed), "hold_to_close": stats(hold),
                        "mfe_ceiling": stats(ceiling)}
        report["scenarios"][f"TP_{tp:.0f}pct"] = block

        results_md.append(f"\n## TP = +{tp:.0f}% (SL = реальный стоп сделки)\n")
        results_md.append("| Горизонт | Стратегия | N | Expectancy %/сделка | WR |")
        results_md.append("|---|---|---|---|---|")
        for H in ["4h", "24h"]:
            for strat, lbl in [("fixed_tp", "Фикс TP/SL"), ("hold_to_close", "Hold→close (+SL)"),
                               ("mfe_ceiling", "Потолок (пик MFE)")]:
                s = report["scenarios"][f"TP_{tp:.0f}pct"][H][strat]
                if s:
                    results_md.append(f"| {H} | {lbl} | {s['n']} | **{s['expectancy_pct']:+.2f}%** | {s['win_rate']:.1%} |")

    OUT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    OUT_MD.write_text("\n".join(results_md))

    # console
    print(f"LONG with MFE/MAE: {len(longs)}  mae_signed={mae_signed}\n")
    for tp in TP_TARGETS:
        print(f"=== TP +{tp:.0f}% ===")
        for H in ["4h", "24h"]:
            b = report["scenarios"][f"TP_{tp:.0f}pct"][H]
            for strat in ["fixed_tp", "hold_to_close", "mfe_ceiling"]:
                s = b[strat]
                if s:
                    print(f"  {H:3s} {strat:14s} n={s['n']:4d}  E={s['expectancy_pct']:+.2f}%/trade  WR={s['win_rate']:.1%}")
        print()
    print(f"Saved: {OUT_JSON.name}, {OUT_MD.name}")


if __name__ == "__main__":
    main()

"""
outcome_tracker.py — автоматический трекер исходов для бэктеста.

Логика:
  1. После каждого прогона screener.py сохраняет сигналы в outcomes/pending.json
  2. При следующем запуске screener.py (или вручную) проверяет: прошло ли 4h/24h?
  3. Если да — берёт текущую цену с Bybit, считает результат, пишет в outcomes/resolved.csv
  4. Накапливая данные за 2+ недели → можно считать реальный win rate по каждому сетапу

CLI:
  python3 outcome_tracker.py stats          — win rate по сетапам
  python3 outcome_tracker.py pending        — список незакрытых сигналов
  python3 outcome_tracker.py resolve        — принудительно закрыть все прошедшие
  python3 outcome_tracker.py clear          — очистить все данные
  python3 outcome_tracker.py csv            — показать путь к CSV
"""

import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

# Obsidian интеграция (опционально)
try:
    import obsidian_bridge as _obs
    _OBS_AVAILABLE = True
except ImportError:
    _OBS_AVAILABLE = False

# ─────────────────────────────────────────────────────────────
# Пути
# ─────────────────────────────────────────────────────────────

BASE_DIR     = Path(__file__).parent / "outcomes"
PENDING_FILE = BASE_DIR / "pending.json"
RESOLVED_CSV = BASE_DIR / "resolved.csv"

BASE_URL = "https://api.bybit.com"

# Горизонты оценки
HORIZONS_H = [4, 24]

# Поля CSV (новые поля добавляются В КОНЕЦ — backward-compatible с существующим CSV)
CSV_FIELDS = [
    "run_ts", "symbol", "setup", "score", "grade", "pump_score",
    "direction",
    "price_entry", "stop", "tp1", "tp2",
    # ключевые метрики на момент сигнала
    "funding", "oi_24h_pct", "mtf_bull", "mtf_bear",
    "rsi_1h", "cvd_kline", "cvd_trade",
    "ema_bull_1h", "ema_bull_4h", "choch_bull_1h",
    "vwap_dev", "rs_btc",
    # исходы
    "resolve_4h_ts", "price_4h", "change_4h_pct",
    "hit_tp1_4h", "hit_stop_4h", "outcome_4h",
    "resolve_24h_ts", "price_24h", "change_24h_pct",
    "hit_tp1_24h", "hit_stop_24h", "outcome_24h",
    # v2: кросс-биржа и зона (добавлено позднее, у старых строк будет '')
    "bnb_cross_bonus", "in_zone", "whale_flag",
    # v3: MFE/MAE — максимальная реализованная прибыль / просадка внутри окна
    "mfe_4h_pct",  "mae_4h_pct",
    "mfe_24h_pct", "mae_24h_pct",
    # v4: контекстные фичи для логистической регрессии (добавлено 2026-04-20)
    "utc_hour",          # UTC-час сигнала (0-23)
    "btc_trend_4h",      # BTC 4h цена vs EMA20/EMA50: above / below / between
    "alt_breadth_pct",   # % альтов в аптренде на момент сигнала
    "listing_age_days",  # дней с листинга на Bybit
    "avg_vol_7d_usd",    # средний дневной объём за 7 дней, USD
    "sc_squeeze",        # сырой скор сетапа 1 (сквиз)
    "sc_bos_fvg",        # сырой скор сетапа 2 (BOS/FVG)
    "sc_range_sweep",    # сырой скор сетапа 3 (sweep)
    "sc_breakout",       # сырой скор сетапа 4 (breakout)
    "sc_short_dist",     # сырой скор сетапа 5 (distribution)
    # v5: P&L и R-multiple (expectancy tracking)
    "atr_at_entry",      # ATR% на момент сигнала
    "r_multiple_4h",     # R-multiple на горизонте 4h
    "exit_reason_4h",    # tp1 / sl / 4h_close
    "r_multiple_24h",    # R-multiple на горизонте 24h
    "exit_reason_24h",   # tp1 / sl / 24h_close
    # v6: CHoCH conviction flag (AVEC-10)
    "choch_conviction",  # 1 if bull_choch on 1H at signal time, else 0
]


# ─────────────────────────────────────────────────────────────
# Утилиты
# ─────────────────────────────────────────────────────────────

def _ensure_dirs():
    BASE_DIR.mkdir(parents=True, exist_ok=True)


def _load_pending() -> list:
    if not PENDING_FILE.exists():
        return []
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_pending(entries: list):
    _ensure_dirs()
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def _fetch_klines_extremes(
    symbol: str, from_dt: datetime, to_dt: datetime
) -> tuple[float | None, float | None, float | None]:
    """
    Возвращает (max_high, min_low, close_at_end) по 15m свечам за интервал.
    Используется вместо точечной цены — определяет, касалась ли цена SL/TP
    в любой момент внутри окна, а не только в момент проверки.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/v5/market/kline",
            params={
                "category": "linear",
                "symbol":   symbol,
                "interval": "15",
                "start":    int(from_dt.timestamp() * 1000),
                "end":      int(to_dt.timestamp() * 1000),
                "limit":    200,
            },
            timeout=10,
        )
        bars = resp.json()["result"]["list"]  # desc order: newest bar first
        if not bars:
            return None, None, None
        highs = [float(b[2]) for b in bars]
        lows  = [float(b[3]) for b in bars]
        close_end = float(bars[0][4])  # close of the most-recent bar ≈ price at horizon
        return max(highs), min(lows), close_end
    except Exception:
        return None, None, None


def _now_ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def _parse_ts(ts_str: str) -> datetime:
    return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S")


def _append_csv(row: dict):
    _ensure_dirs()
    exists = RESOLVED_CSV.exists()
    with open(RESOLVED_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


# ─────────────────────────────────────────────────────────────
# Основные функции
# ─────────────────────────────────────────────────────────────

def save_pending(filtered: list, all_results: list = None) -> int:
    """
    Сохраняет отфильтрованные сигналы как pending-исходы.
    filtered — список результатов screener.py (только те, что прошли min_score).
    Возвращает количество сохранённых записей.
    """
    _ensure_dirs()
    existing = _load_pending()
    # Деdup по (symbol, run_ts-округлённый до минуты)
    existing_keys = {(e["symbol"], e["run_ts"][:16]) for e in existing}

    now_ts = _now_ts()
    now_key = now_ts[:16]

    new_entries = []
    for r in filtered:
        sym = r.get("symbol", "?")
        if (sym, now_key) in existing_keys:
            continue

        # Направление из verdict — нужно восстановить из сигналов
        direction = _direction_from_result(r)

        # Торговый план (стоп/тп)
        stop, tp1, tp2 = _plan_from_result(r)

        entry = {
            "run_ts":      now_ts,
            "symbol":      sym,
            "setup":       r.get("setup", "—"),
            "score":       r.get("score", 0),
            "grade":       r.get("grade", "—"),
            "pump_score":  r.get("pump_score", 0),
            "direction":   direction,
            "price_entry": r.get("price", 0),
            "stop":        stop,
            "tp1":         tp1,
            "tp2":         tp2,
            # метрики
            "funding":     r.get("fund_%", 0),
            "oi_24h_pct":  r.get("oi24h_%", 0),
            "mtf_bull":    r.get("bull_mtf_ext", r.get("mtf_b", 0)),
            "mtf_bear":    r.get("bear_mtf_ext", r.get("mtf_s", 0)),
            "rsi_1h":      r.get("rsi_1h"),
            "cvd_kline":   r.get("cvd_k%", 0),
            "cvd_trade":   r.get("cvd_t%", 0),
            "ema_bull_1h": int(bool(r.get("ema_1h") and r["ema_1h"].get("ema_bull"))),
            "ema_bull_4h": int(bool(r.get("ema_4h") and r["ema_4h"].get("ema_bull"))),
            # choch_1h — строка "bull_choch"/"bear_choch"/None/"—"
            "choch_bull_1h": int(r.get("choch_1h") == "bull_choch"),
            "vwap_dev":    r.get("vwap_dev"),
            "rs_btc":      r.get("rs_btc"),
            # v2 поля
            "bnb_cross_bonus": r.get("bnb_cross_bonus", 0) or 0,
            "in_zone": int(bool(
                (r.get("in_bfvg") or r.get("in_bob")) if direction == "ЛОНГ"
                else (r.get("in_sfvg") or r.get("in_sob"))
            )),
            "whale_flag": int(
                bool(r.get("whale") and r.get("whale") != "—")
            ),
            # v4: контекстные фичи для регрессии
            "utc_hour":         datetime.utcnow().hour,
            "btc_trend_4h":     r.get("btc_ema_pos", "unknown"),
            "alt_breadth_pct":  r.get("alt_breadth_pct"),
            "listing_age_days": r.get("listing_age_days"),
            "avg_vol_7d_usd":   r.get("avg_vol_7d_usd"),
            "sc_squeeze":       r.get("sc_squeeze", 0),
            "sc_bos_fvg":       r.get("sc_bos_fvg", 0),
            "sc_range_sweep":   r.get("sc_range_sweep", 0),
            "sc_breakout":      r.get("sc_breakout", 0),
            "sc_short_dist":    r.get("sc_short_dist", 0),
            # v5: ATR at signal time (для R-multiple расчёта)
            "atr_at_entry":     r.get("atr_%", 0),
            # v6: CHoCH conviction flag (AVEC-10)
            "choch_conviction": int(bool(r.get("choch_conviction"))),
            # каналы, подтвердившие сигнал (для channel_accuracy)
            "channel_conf":     r.get("channel_conf", []),
            # исходы (заполнятся позже)
            "resolve_4h_ts":   None,
            "price_4h":        None,
            "change_4h_pct":   None,
            "hit_tp1_4h":      None,
            "hit_stop_4h":     None,
            "outcome_4h":      None,
            "resolve_24h_ts":  None,
            "price_24h":       None,
            "change_24h_pct":  None,
            "hit_tp1_24h":     None,
            "hit_stop_24h":    None,
            "outcome_24h":     None,
        }
        new_entries.append(entry)
        existing_keys.add((sym, now_key))

    if new_entries:
        existing.extend(new_entries)
        _save_pending(existing)
        # Обновляем заметки в Obsidian для новых символов
        if _OBS_AVAILABLE:
            _obs_cfg = _obs.load_config()
            if _obs_cfg.get("enabled"):
                _refreshed = set()
                for e in new_entries:
                    sym = e["symbol"]
                    if sym not in _refreshed:
                        _obs.refresh_coin_note(sym, cfg=_obs_cfg)
                        _refreshed.add(sym)

    return len(new_entries)


def _direction_from_result(r: dict) -> str:
    """Определяет направление сигнала из данных результата (без вызова interpret_signals)."""
    bull = r.get("bull_mtf_ext", r.get("mtf_b", 0))
    bear = r.get("bear_mtf_ext", r.get("mtf_s", 0))
    fund = r.get("fund_%", 0)
    setup = r.get("setup", "")

    if setup in ("squeeze", "breakout"):
        return "ЛОНГ"
    if setup == "range_sweep":
        sweep_dir = r.get("sweep_dir")
        if sweep_dir == "long":
            return "ЛОНГ"
        if sweep_dir == "short":
            return "ШОРТ"
        cvd = r.get("cvd_k%", 0)
        return "ЛОНГ" if cvd > 0 else "ШОРТ"
    # bos_fvg / short_dist: смотрим на MTF
    if bull > bear:
        return "ЛОНГ"
    elif bear > bull:
        return "ШОРТ"
    # Нейтральный MTF (bull == bear): тайбрейкер по сетапу.
    # short_dist — распределение/давление продаж → всегда ШОРТ.
    # bos_fvg без MTF → ЛОНГ (BOS/FVG сам по себе бычий паттерн).
    # ЖДАТЬ устранён: он создавал баг в outcome_tracker (LONG-план + SHORT-оценка).
    if setup == "short_dist":
        return "ШОРТ"
    return "ЛОНГ"


def _plan_from_result(r: dict) -> tuple[float, float, float]:
    """Возвращает (stop, tp1, tp2) из уровней результата."""
    price = r.get("price", 0)
    atr_pct = r.get("atr_%", 1.0) or 1.0
    atr_abs = price * atr_pct / 100
    direction = _direction_from_result(r)

    if direction in ("ЛОНГ", "ЖДАТЬ"):  # ЖДАТЬ: LONG-стиль план (TP выше, стоп ниже)
        stop = price - atr_abs * 1.8
        tp1  = price + atr_abs * 2.7   # R:R = 2.7/1.8 = 1.5
        tp2  = price + atr_abs * 4.0
    elif direction == "ШОРТ":
        stop = price + atr_abs * 1.8
        tp1  = price - atr_abs * 2.7   # R:R = 2.7/1.8 = 1.5
        tp2  = price - atr_abs * 4.0
    else:
        stop = price - atr_abs
        tp1  = price + atr_abs * 1.5
        tp2  = price + atr_abs * 2.5

    return round(stop, 8), round(tp1, 8), round(tp2, 8)


def _compute_r_multiple(exit_price: float, entry: float, sl: float) -> float | None:
    """
    R = (exit - entry) / (entry - sl)
    Works for both longs (entry > sl) and shorts (entry < sl) because
    the sign convention cancels: long profit → positive numerator and denominator;
    short profit → both negative, ratio positive.
    Returns None when the risk leg is zero (degenerate SL placement).
    """
    risk = entry - sl
    if not risk or abs(risk) < 1e-12:
        return None
    return round((exit_price - entry) / risk, 4)


def _resolve_exit(hit_tp1: bool, hit_stop: bool,
                  tp1: float, stop: float, close_price: float,
                  horizon_label: str) -> tuple[float, str]:
    """Returns (exit_price, exit_reason) for a given horizon."""
    if hit_tp1:
        return tp1, "tp1"
    if hit_stop:
        return stop, "sl"
    return close_price, f"{horizon_label}_close"


def check_and_resolve(silent: bool = False) -> int:
    """
    Проверяет pending-записи: если прошло ≥4h или ≥24h — подтягивает цену и закрывает исход.
    Возвращает количество закрытых исходов.
    silent=True: не выводит прогресс.
    """
    entries = _load_pending()
    if not entries:
        return 0

    now = datetime.utcnow()
    resolved_count = 0
    updated = False

    for entry in entries:
        run_dt = _parse_ts(entry["run_ts"])
        elapsed_h = (now - run_dt).total_seconds() / 3600

        # ── 4h исход ──────────────────────────────────────────────────────────
        if elapsed_h >= 4 and entry.get("price_4h") is None:
            h4_end = run_dt + timedelta(hours=4)
            max_high, min_low, close_end = _fetch_klines_extremes(
                entry["symbol"], run_dt, h4_end
            )
            if close_end is not None:
                price_now = close_end
                entry_px = entry["price_entry"]
                pct = (price_now - entry_px) / entry_px * 100 if entry_px else 0
                direction = entry.get("direction", "ЛОНГ")
                stop = entry.get("stop") or 0
                tp1  = entry.get("tp1") or 0

                if direction in ("ЛОНГ", "ЖДАТЬ"):  # ЖДАТЬ: LONG-стиль план (TP выше, стоп ниже)
                    hit_tp1  = bool(max_high and tp1 and max_high >= tp1)
                    hit_stop = bool(min_low  and stop and min_low <= stop)
                    outcome  = "TP1" if hit_tp1 else ("STOP" if hit_stop else
                               ("WIN" if pct > 0.5 else ("LOSS" if pct < -0.5 else "FLAT")))
                    # MFE/MAE: для лонга max_high даёт макс. прибыль, min_low — макс. просадку
                    mfe_pct = ((max_high - entry_px) / entry_px * 100) if (max_high and entry_px) else 0.0
                    mae_pct = ((min_low  - entry_px) / entry_px * 100) if (min_low  and entry_px) else 0.0
                else:
                    hit_tp1  = bool(min_low  and tp1 and min_low  <= tp1)
                    hit_stop = bool(max_high and stop and max_high >= stop)
                    outcome  = "TP1" if hit_tp1 else ("STOP" if hit_stop else
                               ("WIN" if pct < -0.5 else ("LOSS" if pct > 0.5 else "FLAT")))
                    # Для шорта MFE — движение ВНИЗ (отрицательный %), MAE — ВВЕРХ
                    mfe_pct = ((entry_px - min_low)  / entry_px * 100) if (min_low  and entry_px) else 0.0
                    mae_pct = ((entry_px - max_high) / entry_px * 100) if (max_high and entry_px) else 0.0

                # T1.4: ЖДАТЬ signals have no directional commitment → exclude from WR stats
                if direction == "ЖДАТЬ":
                    outcome = "FLAT"
                    hit_tp1 = False
                    hit_stop = False

                exit_px_4h, exit_rsn_4h = _resolve_exit(
                    hit_tp1, hit_stop, tp1, stop, price_now, "4h"
                )
                r_mult_4h = _compute_r_multiple(exit_px_4h, entry_px, stop)
                entry["resolve_4h_ts"]  = _now_ts()
                entry["price_4h"]       = price_now
                entry["change_4h_pct"]  = round(pct, 2)
                entry["hit_tp1_4h"]     = int(hit_tp1)
                entry["hit_stop_4h"]    = int(hit_stop)
                entry["outcome_4h"]     = outcome
                entry["mfe_4h_pct"]     = round(mfe_pct, 2)
                entry["mae_4h_pct"]     = round(mae_pct, 2)
                entry["r_multiple_4h"]  = r_mult_4h
                entry["exit_reason_4h"] = exit_rsn_4h
                resolved_count += 1
                updated = True
                if not silent:
                    print(f"  [4h] {entry['symbol']:<14} {pct:+.1f}%  "
                          f"MFE{mfe_pct:+.1f}/MAE{mae_pct:+.1f}  {outcome}")
                time.sleep(0.05)  # gentle rate limit

        # ── 24h исход ─────────────────────────────────────────────────────────
        if elapsed_h >= 24 and entry.get("price_24h") is None:
            h24_end = run_dt + timedelta(hours=24)
            max_high, min_low, close_end = _fetch_klines_extremes(
                entry["symbol"], run_dt, h24_end
            )
            if close_end is not None:
                price_now = close_end
                entry_px = entry["price_entry"]
                pct = (price_now - entry_px) / entry_px * 100 if entry_px else 0
                direction = entry.get("direction", "ЛОНГ")
                stop = entry.get("stop") or 0
                tp1  = entry.get("tp1") or 0

                if direction in ("ЛОНГ", "ЖДАТЬ"):  # ЖДАТЬ: LONG-стиль план (TP выше, стоп ниже)
                    hit_tp1  = bool(max_high and tp1 and max_high >= tp1)
                    hit_stop = bool(min_low  and stop and min_low <= stop)
                    outcome  = "TP1" if hit_tp1 else ("STOP" if hit_stop else
                               ("WIN" if pct > 0.5 else ("LOSS" if pct < -0.5 else "FLAT")))
                    mfe_pct = ((max_high - entry_px) / entry_px * 100) if (max_high and entry_px) else 0.0
                    mae_pct = ((min_low  - entry_px) / entry_px * 100) if (min_low  and entry_px) else 0.0
                else:
                    hit_tp1  = bool(min_low  and tp1 and min_low  <= tp1)
                    hit_stop = bool(max_high and stop and max_high >= stop)
                    outcome  = "TP1" if hit_tp1 else ("STOP" if hit_stop else
                               ("WIN" if pct < -0.5 else ("LOSS" if pct > 0.5 else "FLAT")))
                    mfe_pct = ((entry_px - min_low)  / entry_px * 100) if (min_low  and entry_px) else 0.0
                    mae_pct = ((entry_px - max_high) / entry_px * 100) if (max_high and entry_px) else 0.0

                # T1.4: ЖДАТЬ signals have no directional commitment → exclude from WR stats
                if direction == "ЖДАТЬ":
                    outcome = "FLAT"
                    hit_tp1 = False
                    hit_stop = False

                exit_px_24h, exit_rsn_24h = _resolve_exit(
                    hit_tp1, hit_stop, tp1, stop, price_now, "24h"
                )
                r_mult_24h = _compute_r_multiple(exit_px_24h, entry_px, stop)
                entry["resolve_24h_ts"] = _now_ts()
                entry["price_24h"]      = price_now
                entry["change_24h_pct"] = round(pct, 2)
                entry["hit_tp1_24h"]    = int(hit_tp1)
                entry["hit_stop_24h"]   = int(hit_stop)
                entry["outcome_24h"]    = outcome
                entry["mfe_24h_pct"]    = round(mfe_pct, 2)
                entry["mae_24h_pct"]    = round(mae_pct, 2)
                entry["r_multiple_24h"]  = r_mult_24h
                entry["exit_reason_24h"] = exit_rsn_24h
                resolved_count += 1
                updated = True

                # Запись в CSV когда оба горизонта закрыты (или только 24h если 4h уже был)
                _append_csv(entry)
                if not silent:
                    print(f"  [24h] {entry['symbol']:<14} {pct:+.1f}%  "
                          f"MFE{mfe_pct:+.1f}/MAE{mae_pct:+.1f}  {outcome}")
                time.sleep(0.05)

    if updated:
        # Удаляем полностью закрытые записи из pending (оба горизонта разрешены)
        newly_closed = [
            e for e in entries
            if e.get("price_4h") is not None and e.get("price_24h") is not None
        ]
        still_pending = [
            e for e in entries
            if e.get("price_4h") is None or e.get("price_24h") is None
        ]
        _save_pending(still_pending)
        # Обновляем accuracy каналов на основе закрытых сигналов
        if newly_closed:
            _update_channel_accuracy(newly_closed)
        # Обновляем заметки в Obsidian для всех затронутых символов
        if _OBS_AVAILABLE:
            _obs_cfg = _obs.load_config()
            if _obs_cfg.get("enabled"):
                _touched = {e["symbol"] for e in entries if e.get("price_4h") or e.get("price_24h")}
                for sym in _touched:
                    _obs.refresh_coin_note(sym, cfg=_obs_cfg)

    return resolved_count


# ─── Channel Accuracy Tracking ───────────────────────────────────────────────

CHANNEL_ACCURACY_PATH = BASE_DIR.parent / "channel_accuracy.json"


def _load_channel_accuracy() -> dict:
    try:
        return json.loads(CHANNEL_ACCURACY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_channel_accuracy(data: dict):
    try:
        CHANNEL_ACCURACY_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _update_channel_accuracy(resolved_entries: list):
    """
    Обновляет channel_accuracy.json на основе только что закрытых сигналов.
    Для каждого сигнала: каналы из channel_conf получают n_total+1,
    и n_wins+1 если outcome_4h в (TP1, WIN).
    """
    if not resolved_entries:
        return
    acc = _load_channel_accuracy()
    changed = False
    for entry in resolved_entries:
        channels = entry.get("channel_conf") or []
        if not channels:
            continue
        outcome = entry.get("outcome_4h", "")
        is_win  = outcome in ("TP1", "WIN")
        for ch in channels:
            if not ch:
                continue
            if ch not in acc:
                acc[ch] = {"n": 0, "wins": 0, "accuracy": 0.5}
            acc[ch]["n"] += 1
            if is_win:
                acc[ch]["wins"] += 1
            acc[ch]["accuracy"] = round(acc[ch]["wins"] / acc[ch]["n"], 3)
            changed = True
    if changed:
        _save_channel_accuracy(acc)


def get_channel_accuracy() -> dict:
    """
    Возвращает {channel_name: {n, wins, accuracy}} для использования в channel_reader.
    accuracy: 0.0–1.0. Каналы с n<5 считаются ненадёжными (accuracy → 0.5 baseline).
    """
    raw = _load_channel_accuracy()
    result = {}
    for ch, d in raw.items():
        n = d.get("n", 0)
        result[ch] = {
            "n":        n,
            "wins":     d.get("wins", 0),
            "accuracy": d.get("accuracy", 0.5) if n >= 5 else 0.5,
        }
    return result


# ─────────────────────────────────────────────────────────────
# Статистика
# ─────────────────────────────────────────────────────────────

def get_stats() -> dict:
    """Читает resolved.csv и считает win rate по сетапам."""
    if not RESOLVED_CSV.exists():
        return {}

    rows = []
    with open(RESOLVED_CSV, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)

    if not rows:
        return {}

    # Группируем по сетапу
    stats = {}
    for row in rows:
        setup = row.get("setup", "?")
        if setup not in stats:
            stats[setup] = {
                "total": 0, "win_4h": 0, "stop_4h": 0, "tp1_4h": 0,
                "win_24h": 0, "stop_24h": 0, "tp1_24h": 0,
                "avg_change_4h": [], "avg_change_24h": [],
            }
        s = stats[setup]
        s["total"] += 1

        out4 = row.get("outcome_4h", "")
        if out4 in ("WIN", "TP1"):  s["win_4h"] += 1
        if out4 == "STOP":          s["stop_4h"] += 1
        if out4 == "TP1":           s["tp1_4h"] += 1
        try:
            s["avg_change_4h"].append(float(row["change_4h_pct"]))
        except (ValueError, TypeError):
            pass

        out24 = row.get("outcome_24h", "")
        if out24 in ("WIN", "TP1"): s["win_24h"] += 1
        if out24 == "STOP":         s["stop_24h"] += 1
        if out24 == "TP1":          s["tp1_24h"] += 1
        try:
            s["avg_change_24h"].append(float(row["change_24h_pct"]))
        except (ValueError, TypeError):
            pass

    # Финальный расчёт
    for setup, s in stats.items():
        n = s["total"]
        s["winrate_4h"]  = round(s["win_4h"]  / n * 100, 1) if n else 0
        s["winrate_24h"] = round(s["win_24h"] / n * 100, 1) if n else 0
        s["mean_4h"]  = round(sum(s["avg_change_4h"])  / len(s["avg_change_4h"]),  2) if s["avg_change_4h"]  else 0
        s["mean_24h"] = round(sum(s["avg_change_24h"]) / len(s["avg_change_24h"]), 2) if s["avg_change_24h"] else 0

    return stats


def print_summary():
    """Выводит таблицу статистики."""
    stats = get_stats()
    pending = _load_pending()

    print("\n" + "="*70)
    print("  OUTCOME TRACKER — СТАТИСТИКА БЭКТЕСТА")
    print("="*70)

    if not stats:
        total_pending = len(pending)
        print(f"\n  Данных пока нет. Pending сигналов: {total_pending}")
        print("  Запускай screener.py раз в день → через 24h появятся первые исходы.")
        print()
        return

    from tabulate import tabulate

    table = []
    for setup, s in sorted(stats.items(), key=lambda x: x[1]["total"], reverse=True):
        table.append([
            setup,
            s["total"],
            f"{s['winrate_4h']}%",
            f"{s['mean_4h']:+.1f}%",
            f"{s['tp1_4h']}",
            f"{s['stop_4h']}",
            f"{s['winrate_24h']}%",
            f"{s['mean_24h']:+.1f}%",
        ])

    print(tabulate(
        table,
        headers=["Сетап", "Сигналов", "WR 4h", "Avg 4h", "TP1 4h", "SL 4h",
                 "WR 24h", "Avg 24h"],
        tablefmt="rounded_outline",
    ))

    total_resolved = sum(s["total"] for s in stats.values())
    total_win_4h   = sum(s["win_4h"] for s in stats.values())
    total_win_24h  = sum(s["win_24h"] for s in stats.values())
    print(f"\n  Всего закрыто: {total_resolved}  |  "
          f"Win rate 4h: {total_win_4h/total_resolved*100:.1f}%  |  "
          f"Win rate 24h: {total_win_24h/total_resolved*100:.1f}%")
    print(f"  Pending (ожидают исхода): {len(pending)}")
    print(f"  CSV: {RESOLVED_CSV}")
    print()


def _try_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _try_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _wr(group: list, horizon: str) -> float:
    """Win rate для группы строк CSV на горизонте '4h' или '24h'."""
    if not group:
        return 0.0
    wins = sum(1 for r in group if r.get(f"outcome_{horizon}") in ("WIN", "TP1"))
    return wins / len(group) * 100


def _mean_chg(group: list, horizon: str) -> float:
    vals = [_try_float(r.get(f"change_{horizon}_pct")) for r in group if r.get(f"change_{horizon}_pct") not in (None, "")]
    return sum(vals) / len(vals) if vals else 0.0


# ─────────────────────────────────────────────────────────────
# Паттерн-анализ
# ─────────────────────────────────────────────────────────────

def analyze_patterns(rows: list) -> dict:
    """
    Находит сигнальные комбинации с наивысшим/наименьшим WR.
    Требует минимум MIN_N записей на группу.

    Возвращает dict:
      {
        "by_setup":     {setup: {n, wr4h, wr24h, mean4h, mean24h}},
        "by_combo":     [(label, n, wr4h, wr24h), ...],  # топ комбинации
        "top_signals":  [(signal_label, delta_wr, n), ...],  # сигналы-предикторы
        "best_setup":   str,
        "worst_setup":  str,
        "global_wr4h":  float,
        "global_wr24h": float,
        "total_n":      int,
      }
    """
    MIN_N = 3        # минимум для отображения (с меткой "provisional" при N<10)
    STABLE_N = 10   # от этого порога считаем статистику стабильной

    if not rows:
        return {}

    # ── 1. По сетапу ─────────────────────────────────────────────────────────
    from collections import defaultdict
    by_setup: dict = defaultdict(list)
    for r in rows:
        by_setup[r.get("setup", "?")].append(r)

    setup_stats = {}
    for setup, grp in by_setup.items():
        if len(grp) < MIN_N:
            continue
        setup_stats[setup] = {
            "n":          len(grp),
            "wr4h":       round(_wr(grp, "4h"), 1),
            "wr24h":      round(_wr(grp, "24h"), 1),
            "mean4h":     round(_mean_chg(grp, "4h"), 2),
            "mean24h":    round(_mean_chg(grp, "24h"), 2),
            "tp1_4h":     sum(1 for r in grp if r.get("outcome_4h") == "TP1"),
            "sl_4h":      sum(1 for r in grp if r.get("outcome_4h") == "STOP"),
            "provisional": len(grp) < STABLE_N,
        }

    global_wr4h  = round(_wr(rows, "4h"), 1)
    global_wr24h = round(_wr(rows, "24h"), 1)

    # ── 2. Комбинации сигналов ────────────────────────────────────────────────
    def _combo(rows_sub, label):
        if len(rows_sub) < MIN_N:
            return None
        provisional = len(rows_sub) < STABLE_N
        lbl = f"{label}*" if provisional else label
        return (lbl, len(rows_sub), round(_wr(rows_sub, "4h"), 1), round(_wr(rows_sub, "24h"), 1))

    combos = []

    # squeeze + MTF
    for mtf_thr, mtf_label in [(3, "MTF≥3"), (2, "MTF≥2")]:
        sq_mtf = [r for r in rows if r.get("setup") == "squeeze" and _try_int(r.get("mtf_bull")) >= mtf_thr]
        c = _combo(sq_mtf, f"Squeeze + {mtf_label}")
        if c: combos.append(c)

    # squeeze + funding negative
    sq_fund = [r for r in rows if r.get("setup") == "squeeze" and _try_float(r.get("funding")) < -0.01]
    c = _combo(sq_fund, "Squeeze + fund&lt;-0.01%")
    if c: combos.append(c)

    # squeeze + MTF3 + fund negative (triple)
    sq_triple = [r for r in rows if r.get("setup") == "squeeze"
                 and _try_int(r.get("mtf_bull")) >= 3
                 and _try_float(r.get("funding")) < -0.01]
    c = _combo(sq_triple, "Squeeze + MTF≥3 + fund&lt;-0.01")
    if c: combos.append(c)

    # breakout + whale
    bp_whale = [r for r in rows if r.get("setup") == "breakout" and _try_int(r.get("whale_flag")) == 1]
    c = _combo(bp_whale, "Breakout + кит")
    if c: combos.append(c)

    # breakout + MTF3
    bp_mtf = [r for r in rows if r.get("setup") == "breakout" and _try_int(r.get("mtf_bull")) >= 3]
    c = _combo(bp_mtf, "Breakout + MTF≥3")
    if c: combos.append(c)

    # bos_fvg + EMA bull 1H
    bos_ema = [r for r in rows if r.get("setup") == "bos_fvg" and _try_int(r.get("ema_bull_1h")) == 1]
    c = _combo(bos_ema, "BOS/FVG + EMA↑ 1H")
    if c: combos.append(c)

    # EMA bull 1H (все сетапы)
    ema_bull = [r for r in rows if _try_int(r.get("ema_bull_1h")) == 1]
    c = _combo(ema_bull, "EMA↑ 1H (все)")
    if c: combos.append(c)

    # CHoCH bull (после фикса бага будет заполняться)
    choch_bull = [r for r in rows if _try_int(r.get("choch_bull_1h")) == 1]
    c = _combo(choch_bull, "CHoCH↑ 1H")
    if c: combos.append(c)

    # Binance cross confirmed
    bnb_conf = [r for r in rows if _try_int(r.get("bnb_cross_bonus", 0)) > 0]
    c = _combo(bnb_conf, "Binance подтвердил")
    if c: combos.append(c)

    # In zone (цена в FVG/OB)
    in_zone = [r for r in rows if _try_int(r.get("in_zone", 0)) == 1]
    c = _combo(in_zone, "Цена в зоне")
    if c: combos.append(c)

    # Score >= 120
    high_score = [r for r in rows if _try_int(r.get("score")) >= 120]
    c = _combo(high_score, "Score ≥ 120")
    if c: combos.append(c)

    combos.sort(key=lambda x: x[2], reverse=True)  # по WR4h

    # ── 3. Топ сигналы-предикторы (delta vs baseline) ─────────────────────────
    signal_checks = [
        ("MTF_bull≥3",     lambda r: _try_int(r.get("mtf_bull")) >= 3),
        ("MTF_bull&lt;3",  lambda r: _try_int(r.get("mtf_bull")) < 3),
        ("EMA_bull_1H",    lambda r: _try_int(r.get("ema_bull_1h")) == 1),
        ("CHoCH↑_1H",     lambda r: _try_int(r.get("choch_bull_1h")) == 1),
        ("Fund&lt;-0.01%", lambda r: _try_float(r.get("funding")) < -0.01),
        ("Fund≥0",         lambda r: _try_float(r.get("funding")) >= 0),
        ("In_zone",        lambda r: _try_int(r.get("in_zone", 0)) == 1),
        ("Whale",          lambda r: _try_int(r.get("whale_flag", 0)) == 1),
        ("Score≥120",      lambda r: _try_int(r.get("score")) >= 120),
        ("BNB_confirmed",  lambda r: _try_int(r.get("bnb_cross_bonus", 0)) > 0),
        ("RSI&lt;40",      lambda r: _try_float(r.get("rsi_1h", 50)) < 40),
        ("RS>1.5×BTC",     lambda r: _try_float(r.get("rs_btc", 1)) > 1.5),
    ]

    top_signals = []
    for label, fn in signal_checks:
        grp = [r for r in rows if fn(r)]
        if len(grp) < MIN_N:
            continue
        wr = _wr(grp, "4h")
        delta = wr - global_wr4h
        top_signals.append((label, len(grp), round(wr, 1), round(delta, 1)))

    top_signals.sort(key=lambda x: x[3], reverse=True)

    # ── 4. Итог ──────────────────────────────────────────────────────────────
    best  = max(setup_stats, key=lambda s: setup_stats[s]["wr4h"]) if setup_stats else "?"
    worst = min(setup_stats, key=lambda s: setup_stats[s]["wr4h"]) if setup_stats else "?"

    return {
        "by_setup":    setup_stats,
        "by_combo":    combos,
        "top_signals": top_signals,
        "best_setup":  best,
        "worst_setup": worst,
        "global_wr4h": global_wr4h,
        "global_wr24h":global_wr24h,
        "total_n":     len(rows),
    }


# ─────────────────────────────────────────────────────────────
# Запись инсайтов в knowledge_base.md
# ─────────────────────────────────────────────────────────────

KNOWLEDGE_PATH = Path(__file__).parent / "knowledge_base.md"
_INSIGHTS_MARKER = "## 📊 АВТО-АНАЛИТИКА"
_INSIGHTS_END    = "\n---\n"


def write_knowledge_insights(patterns: dict):
    """
    Перезаписывает секцию '## 📊 АВТО-АНАЛИТИКА' в knowledge_base.md.
    Остальная часть файла не трогается.
    """
    if not patterns or not patterns.get("total_n"):
        return

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"{_INSIGHTS_MARKER}",
        f"> Обновлено автоматически: {now}  |  N={patterns['total_n']} сигналов\n",
    ]

    # По сетапу
    lines.append("### Сетапы")
    lines.append("| Сетап | N | WR 4h | WR 24h | Avg 4h | TP1 | SL |")
    lines.append("|-------|---|-------|--------|--------|-----|----|")
    for setup, s in sorted(patterns["by_setup"].items(), key=lambda x: x[1]["wr4h"], reverse=True):
        prov = " \\*" if s.get("provisional") else ""
        lines.append(
            f"| {setup}{prov} | {s['n']} | **{s['wr4h']}%** | {s['wr24h']}% "
            f"| {s['mean4h']:+.1f}% | {s['tp1_4h']} | {s['sl_4h']} |"
        )
    lines.append("")

    # Топ комбинации
    if patterns["by_combo"]:
        lines.append("### Лучшие комбинации сигналов")
        lines.append("| Комбинация | N | WR 4h | WR 24h |")
        lines.append("|-----------|---|-------|--------|")
        for label, n, wr4, wr24 in patterns["by_combo"][:8]:
            bold = "**" if wr4 >= 70 else ""
            lines.append(f"| {label} | {n} | {bold}{wr4}%{bold} | {wr24}% |")
        lines.append("")

    # Предикторы
    if patterns["top_signals"]:
        lines.append("### Сигналы-предикторы (delta vs baseline)")
        baseline = patterns["global_wr4h"]
        lines.append(f"> Baseline WR 4h = {baseline:.1f}%\n")
        for label, n, wr, delta in patterns["top_signals"]:
            sign = "+" if delta >= 0 else ""
            mark = " ← ключевой" if delta >= 8 else (" ← слабый" if delta <= -5 else "")
            lines.append(f"- **{label}**: WR={wr:.1f}%  (Δ{sign}{delta:.1f}pp, n={n}){mark}")
        lines.append("")

    # Вывод
    lines.append("### Ключевые выводы")
    best = patterns["best_setup"]
    bs   = patterns["by_setup"].get(best, {})
    lines.append(f"- **Лучший сетап**: `{best}` — WR {bs.get('wr4h','?')}% (n={bs.get('n','?')})")

    # Находим лучшую комбо
    if patterns["by_combo"]:
        top_combo = patterns["by_combo"][0]
        lines.append(f"- **Лучшая комбинация**: `{top_combo[0]}` — WR {top_combo[2]}% (n={top_combo[1]})")

    # Лучший предиктор
    if patterns["top_signals"]:
        best_pred = patterns["top_signals"][0]
        lines.append(
            f"- **Главный предиктор**: `{best_pred[0]}` → +{best_pred[3]:.1f}pp к baseline WR"
        )

    lines.append("")

    # Собираем секцию
    new_section = "\n".join(lines) + _INSIGHTS_END

    # Читаем файл
    if KNOWLEDGE_PATH.exists():
        content = KNOWLEDGE_PATH.read_text(encoding="utf-8")
    else:
        content = "# Trading Knowledge Base\n\n"

    # Заменяем или добавляем секцию
    if _INSIGHTS_MARKER in content:
        start = content.index(_INSIGHTS_MARKER)
        end_marker = content.find(_INSIGHTS_END, start)
        if end_marker != -1:
            content = content[:start] + new_section + content[end_marker + len(_INSIGHTS_END):]
        else:
            content = content[:start] + new_section
    else:
        content = content.rstrip() + "\n\n" + new_section

    KNOWLEDGE_PATH.write_text(content, encoding="utf-8")


# ─────────────────────────────────────────────────────────────
# Еженедельный Telegram-отчёт
# ─────────────────────────────────────────────────────────────

def format_weekly_telegram(patterns: dict, week_rows: list, all_rows: list) -> str:
    """
    Форматирует еженедельный отчёт для Telegram.
    week_rows — строки за последние 7 дней.
    all_rows  — все строки (для исторического WR).
    """
    if not patterns:
        return "📊 <b>Недельный отчёт</b>\n\nДанных пока нет. Запускай скан каждый день — через неделю появится статистика."

    now = datetime.utcnow()
    week_start = (now - timedelta(days=7)).strftime("%d.%m")
    week_end   = now.strftime("%d.%m.%Y")

    lines = [
        f"📊 <b>НЕДЕЛЬНЫЙ ОТЧЁТ  {week_start} – {week_end}</b>",
        f"<i>Автоанализ {patterns['total_n']} сигналов за всё время  |  "
        f"На этой неделе: {len(week_rows)}</i>",
        "",
    ]

    # ── Итоговый WR ──────────────────────────────────────────────────────────
    lines += [
        f"<b>Общий Win Rate (все время)</b>",
        f"  4h:  <b>{patterns['global_wr4h']:.1f}%</b>",
        f"  24h: <b>{patterns['global_wr24h']:.1f}%</b>",
        "",
    ]

    # ── По сетапам ───────────────────────────────────────────────────────────
    SETUP_ICON = {"squeeze": "⚡", "bos_fvg": "📐", "range_sweep": "↔️", "breakout": "🚀"}
    lines.append("<b>По сетапам</b>")
    for setup, s in sorted(patterns["by_setup"].items(), key=lambda x: x[1]["wr4h"], reverse=True):
        icon  = SETUP_ICON.get(setup, "📊")
        grade = "🏆" if s["wr4h"] >= 70 else ("✅" if s["wr4h"] >= 60 else ("⚠️" if s["wr4h"] >= 50 else "❌"))
        lines.append(
            f"  {icon}{grade} <b>{setup}</b>  WR {s['wr4h']}% / {s['wr24h']}%  "
            f"avg {s['mean4h']:+.1f}%  (n={s['n']})"
        )
    lines.append("")

    # ── Лучшие комбинации ────────────────────────────────────────────────────
    if patterns["by_combo"]:
        lines.append("<b>Лучшие комбинации сигналов</b>")
        for label, n, wr4, wr24 in patterns["by_combo"][:5]:
            bar = "█" * min(int(wr4 / 10), 10)
            lines.append(f"  {bar}  <b>{wr4:.0f}%</b>  {label}  (n={n})")
        lines.append("")

    # ── Ключевые предикторы ──────────────────────────────────────────────────
    if patterns["top_signals"]:
        baseline = patterns["global_wr4h"]
        lines.append(f"<b>Сигналы-предикторы</b>  <i>(vs baseline {baseline:.1f}%)</i>")
        for label, n, wr, delta in patterns["top_signals"][:6]:
            sign = "+" if delta >= 0 else ""
            emoji = "🔼" if delta >= 8 else ("▲" if delta >= 3 else ("▼" if delta <= -3 else "—"))
            lines.append(f"  {emoji} <b>{label}</b>  WR={wr:.1f}%  Δ{sign}{delta:.1f}pp  n={n}")
        lines.append("")

    # ── На этой неделе ───────────────────────────────────────────────────────
    if week_rows:
        week_wr4 = _wr(week_rows, "4h")
        week_wr24 = _wr(week_rows, "24h")
        week_wins = sum(1 for r in week_rows if r.get("outcome_4h") in ("WIN", "TP1"))
        week_stops = sum(1 for r in week_rows if r.get("outcome_4h") == "STOP")
        lines += [
            "<b>Эта неделя</b>",
            f"  Сигналов: {len(week_rows)}  |  Win: {week_wins}  |  Stop: {week_stops}",
            f"  WR 4h: <b>{week_wr4:.1f}%</b>  |  WR 24h: <b>{week_wr24:.1f}%</b>",
            "",
        ]

    # ── Главный вывод ─────────────────────────────────────────────────────────
    best_setup = patterns["best_setup"]
    bs         = patterns["by_setup"].get(best_setup, {})
    best_combo = patterns["by_combo"][0] if patterns["by_combo"] else None
    best_pred  = patterns["top_signals"][0] if patterns["top_signals"] else None

    lines.append("<b>💡 Ключевые выводы</b>")
    lines.append(f"  • Лучший сетап: <b>{best_setup}</b> ({bs.get('wr4h','?')}%, n={bs.get('n','?')})")
    if best_combo:
        lines.append(f"  • Лучшая связка: <b>{best_combo[0]}</b> ({best_combo[2]:.0f}%, n={best_combo[1]})")
    if best_pred and best_pred[3] > 0:
        lines.append(
            f"  • Главный предиктор: <b>{best_pred[0]}</b> "
            f"(+{best_pred[3]:.1f}pp к WR)"
        )

    # Антипаттерн
    worst_signals = [s for s in patterns["top_signals"] if s[3] <= -5]
    if worst_signals:
        wp = worst_signals[-1]
        lines.append(f"  • Избегай: <b>{wp[0]}</b> (−{abs(wp[3]):.1f}pp к WR)")

    lines += [
        "",
        "<i>Knowledge base обновлён. Следующий отчёт — воскресенье.</i>",
    ]

    return "\n".join(lines)


def send_weekly_report():
    """Считает статистику, обновляет knowledge_base.md и шлёт отчёт в Telegram."""
    import json as _json

    cfg_path = Path(__file__).parent / "telegram_config.json"
    if not cfg_path.exists():
        print("[Weekly] telegram_config.json не найден")
        return

    cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
    if not cfg.get("enabled") or not cfg.get("bot_token"):
        print("[Weekly] Telegram не настроен")
        return

    token   = cfg["bot_token"]
    targets = [str(cfg["chat_id"])]
    for e in cfg.get("extra_chat_ids", []):
        cid = str(e).strip()
        if cid and cid not in targets:
            targets.append(cid)

    # Разрешаем pending перед отчётом
    resolved = check_and_resolve(silent=True)
    if resolved:
        print(f"[Weekly] Закрыто исходов перед отчётом: {resolved}")

    # Читаем всё из CSV
    if not RESOLVED_CSV.exists():
        print("[Weekly] CSV пуст, нет данных для отчёта")
        return

    all_rows = []
    with open(RESOLVED_CSV, "r", encoding="utf-8") as f:
        all_rows = list(csv.DictReader(f))

    if not all_rows:
        return

    # Строки за последние 7 дней
    cutoff = datetime.utcnow() - timedelta(days=7)
    week_rows = [
        r for r in all_rows
        if r.get("run_ts") and _parse_ts(r["run_ts"][:19]) >= cutoff
    ]

    # Анализ паттернов
    patterns = analyze_patterns(all_rows)

    # Обновляем knowledge_base.md
    write_knowledge_insights(patterns)
    print("[Weekly] knowledge_base.md обновлён")

    # Формируем TG-сообщение
    msg = format_weekly_telegram(patterns, week_rows, all_rows)

    # Отправляем
    for chat_id in targets:
        _tg_send(token, chat_id, msg)
        time.sleep(1.0)

    print(f"[Weekly] Отчёт отправлен в {len(targets)} чат(а)")


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tg_send(token: str, chat_id: str, text: str):
    """Минимальная отправка в Telegram (без зависимости от telegram_alerts)."""
    MAX = 4000
    chunks = []
    while len(text) > MAX:
        split_at = text.rfind("\n", 0, MAX)
        split_at = split_at if split_at > 0 else MAX
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)

    for i, chunk in enumerate(chunks):
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if not resp.json().get("ok"):
                print(f"[TG] Ошибка: {resp.json().get('description')}")
        except Exception as e:
            print(f"[TG] {e}")
        if i < len(chunks) - 1:
            time.sleep(0.4)


def print_pending():
    entries = _load_pending()
    if not entries:
        print("Нет ожидающих исходов.")
        return

    now = datetime.utcnow()
    print(f"\n  PENDING ({len(entries)} записей)")
    print(f"  {'Символ':<14} {'Сетап':<10} {'Score':<6} {'Направление':<10} "
          f"{'Цена':<12} {'Прошло':<8} {'4h':<6} {'24h'}")
    print("  " + "─"*75)
    for e in sorted(entries, key=lambda x: x["run_ts"], reverse=True)[:30]:
        elapsed = (now - _parse_ts(e["run_ts"])).total_seconds() / 3600
        done_4h  = "✓" if e.get("price_4h")  is not None else "—"
        done_24h = "✓" if e.get("price_24h") is not None else "—"
        print(f"  {e['symbol']:<14} {e['setup']:<10} {e['score']:<6} "
              f"{e['direction']:<10} {e['price_entry']:<12.6g} "
              f"{elapsed:<8.1f}h {done_4h:<6} {done_24h}")


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"

    if cmd == "stats":
        print_summary()

    elif cmd == "pending":
        print_pending()

    elif cmd == "resolve":
        print("Проверяю исходы...")
        n = check_and_resolve(silent=False)
        print(f"\nЗакрыто: {n}")

    elif cmd == "clear":
        confirm = input("Удалить все данные трекера? [yes/NO]: ").strip()
        if confirm.lower() == "yes":
            PENDING_FILE.unlink(missing_ok=True)
            RESOLVED_CSV.unlink(missing_ok=True)
            print("Данные удалены.")
        else:
            print("Отменено.")

    elif cmd == "weekly":
        print("Формирую еженедельный отчёт...")
        send_weekly_report()

    elif cmd == "patterns":
        if not RESOLVED_CSV.exists():
            print("CSV пуст.")
        else:
            with open(RESOLVED_CSV, "r", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            p = analyze_patterns(rows)
            print(f"\nВсего: {p['total_n']}  |  WR4h: {p['global_wr4h']}%  |  WR24h: {p['global_wr24h']}%")
            print(f"Лучший сетап: {p['best_setup']}\n")
            print("Комбинации:")
            for label, n, wr4, wr24 in p["by_combo"]:
                print(f"  {wr4:5.1f}%  {label}  (n={n})")
            print("\nПредикторы:")
            baseline = p["global_wr4h"]
            for label, n, wr, delta in p["top_signals"]:
                print(f"  {delta:+5.1f}pp  {label}  WR={wr:.1f}%  n={n}")

    elif cmd == "csv":
        print(f"CSV файл: {RESOLVED_CSV}")
        if RESOLVED_CSV.exists():
            size = RESOLVED_CSV.stat().st_size
            with open(RESOLVED_CSV) as f:
                rows = sum(1 for _ in f) - 1
            print(f"Строк: {rows}  |  Размер: {size} bytes")
        else:
            print("CSV ещё не создан.")

    else:
        print(__doc__)


if __name__ == "__main__":
    main()

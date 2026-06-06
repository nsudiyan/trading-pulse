"""
shadow_analyze.py — Honest backtest на основе shadow_verdicts.jsonl.

Считает outcome ПРЯМО из Bybit klines для каждого verdict (не зависит от
pump_resolved.csv, который логирует только GO-сигналы ушедшие в TG).

Это даёт настоящие precision + recall:
  - Precision = WIN из всех GO
  - Recall    = WIN-доля среди тех, что Claude ПРОСКИПАЛ (counterfactual)

Алгоритм:
  1. Читаем outcomes/shadow_verdicts.jsonl
  2. Для каждого verdict с возрастом ≥4h:
     - Берём entry_price=candidate.price, direction=candidate.direction
     - Тянем klines с Bybit для окна [ts, ts+4h]
     - Применяем _outcome(tp_hit, sl_hit, t_mfe, t_mae) из pump_detector
  3. Кешируем результаты в outcomes/shadow_outcomes_cache.json
  4. Печатаем стат по verdict (GO/SKIP/WAIT)

CLI:
    python3 shadow_analyze.py                  — последние 7 дней
    python3 shadow_analyze.py --hours 48       — последние 48 часов
    python3 shadow_analyze.py --setup pump     — только pump-setup'ы
    python3 shadow_analyze.py --no-cache       — игнорировать кеш, перепосчитать
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_DIR  = Path(__file__).parent
SHADOW    = BASE_DIR / "outcomes" / "shadow_verdicts.jsonl"
CACHE     = BASE_DIR / "outcomes" / "shadow_outcomes_cache.json"

# Пороги исхода и _outcome — единый источник правды (см. outcome_model.py)
from outcome_model import (
    PUMP_WIN_4H_PCT, PUMP_LOSS_4H_PCT, RUG_WIN_4H_PCT, RUG_LOSS_4H_PCT,
    outcome as _outcome,
)

BYBIT_BASE = "https://api.bybit.com"


def _fetch_kline_extremes(symbol: str, from_ts: int, horizon_h: int) -> tuple:
    """
    Дублирует _fetch_kline_extremes из pump_detector.py.
    Возвращает (max_high, min_low, close_end, t_to_max_h, t_to_min_h) или None.
    """
    try:
        end_ts = from_ts + horizon_h * 3600
        resp = requests.get(
            f"{BYBIT_BASE}/v5/market/kline",
            params={
                "category": "linear",
                "symbol":   symbol,
                "interval": "15",
                "start":    from_ts * 1000,
                "end":      end_ts   * 1000,
                "limit":    200,
            },
            timeout=10,
        )
        bars = resp.json().get("result", {}).get("list", [])
        if not bars:
            return None, None, None, None, None
        max_high = max(float(b[2]) for b in bars)
        min_low  = min(float(b[3]) for b in bars)
        close_end = float(max(bars, key=lambda b: int(b[0]))[4])
        max_ts = min(int(b[0]) for b in bars if float(b[2]) >= max_high) / 1000
        min_ts = min(int(b[0]) for b in bars if float(b[3]) <= min_low)  / 1000
        return max_high, min_low, close_end, \
               round((max_ts - from_ts) / 3600, 2), \
               round((min_ts - from_ts) / 3600, 2)
    except Exception:
        return None, None, None, None, None


def _infer_direction(verdict_row: dict) -> str:
    """Определяет LONG/SHORT из candidate.direction или setup."""
    cand = verdict_row.get("candidate") or {}
    d = (cand.get("direction") or "").upper().strip()
    if d in ("LONG", "SHORT"):
        return d
    setup = (verdict_row.get("setup") or cand.get("setup") or "").lower()
    # pump-семейство = LONG, rug-семейство = SHORT
    if "rug" in setup or "bear" in setup or "post_pump" in setup or "пост-памп" in setup.lower():
        return "SHORT"
    return "LONG"


def _compute_outcome(verdict_row: dict, horizon_h: int) -> str:
    """
    Возвращает 'WIN' / 'LOSS' / 'FLAT' / '' (не дозрел или нет данных).
    """
    ts = verdict_row.get("ts", 0)
    age_s = time.time() - ts
    if age_s < horizon_h * 3600:
        return ""  # ещё не дозрел

    cand = verdict_row.get("candidate") or {}
    entry = float(cand.get("price") or 0)
    sym = verdict_row.get("symbol") or ""
    if not entry or not sym:
        return ""

    direction = _infer_direction(verdict_row)
    mh, ml, ce, t_mh, t_ml = _fetch_kline_extremes(sym, int(ts), horizon_h)
    if mh is None:
        return ""

    if horizon_h == 1:
        # 1h: tighter pump-detector thresholds (под раскачку)
        if direction == "LONG":
            tp_hit = mh >= entry * 1.03
            sl_hit = ml <= entry * 0.98
            t_mfe, t_mae = t_mh, t_ml
        else:
            tp_hit = ml <= entry * 0.97
            sl_hit = mh >= entry * 1.02
            t_mfe, t_mae = t_ml, t_mh
    else:  # 4h
        if direction == "LONG":
            tp_hit = mh >= entry * (1 + PUMP_WIN_4H_PCT  / 100)
            sl_hit = ml <= entry * (1 + PUMP_LOSS_4H_PCT / 100)
            t_mfe, t_mae = t_mh, t_ml
        else:
            tp_hit = ml <= entry * (1 + RUG_WIN_4H_PCT  / 100)
            sl_hit = mh >= entry * (1 + RUG_LOSS_4H_PCT / 100)
            t_mfe, t_mae = t_ml, t_mh

    return _outcome(tp_hit, sl_hit, t_mfe, t_mae)


def _load_shadow(min_ts: int, setup_filter: str = "") -> list:
    if not SHADOW.exists():
        print(f"[ERROR] нет {SHADOW} — shadow log не накоплен")
        sys.exit(1)
    rows = []
    with open(SHADOW, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("ts", 0) < min_ts:
                continue
            if setup_filter and r.get("setup") != setup_filter:
                continue
            rows.append(r)
    return rows


def _load_cache() -> dict:
    if not CACHE.exists():
        return {}
    try:
        return json.load(open(CACHE, encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


def _key(v: dict) -> str:
    return f"{v.get('symbol','')}_{v.get('ts',0)}"


def analyze(hours: int, setup_filter: str = "", use_cache: bool = True):
    cutoff = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    shadows = _load_shadow(cutoff, setup_filter)
    if not shadows:
        print(f"[ERROR] нет shadow-записей за последние {hours}ч (фильтр '{setup_filter or 'все'}')")
        return

    cache = _load_cache() if use_cache else {}

    print(f"[Shadow] {len(shadows)} verdict'ов за {hours}ч, фильтр setup='{setup_filter or 'все'}'")
    print(f"[Shadow] Cache: {len(cache)} pre-resolved outcomes")
    print()

    stats_4h: dict = defaultdict(lambda: defaultdict(int))
    stats_1h: dict = defaultdict(lambda: defaultdict(int))
    by_stage: dict = defaultdict(lambda: defaultdict(int))
    by_source: Counter = Counter()
    matched_4h, immature_4h, no_data = 0, 0, 0

    # Прогресс-индикатор для долгих фетчей
    progress_every = max(1, len(shadows) // 20)
    new_cache_entries = 0

    for i, v in enumerate(shadows):
        verdict = v.get("verdict") or "?"
        by_source[v.get("verdict_source") or "?"] += 1
        cand = v.get("candidate") or {}
        stage = cand.get("stage") or v.get("setup") or "?"
        by_stage[stage][verdict] += 1

        k = _key(v)
        cached = cache.get(k) or {}
        o1 = cached.get("o1", "")
        o4 = cached.get("o4", "")
        # Пустой outcome в кэше = был immature или транзиентный сбой fetch.
        # Пересчитываем ТОЛЬКО недостающие; definitive (WIN/LOSS/FLAT) не трогаем —
        # иначе свежие verdict'ы навсегда застревают как no-data (sample erosion).
        recomputed = False
        if not o1:
            o1 = _compute_outcome(v, 1)
            recomputed = True
        if not o4:
            o4 = _compute_outcome(v, 4)
            recomputed = True
        if recomputed:
            cache[k] = {"o1": o1, "o4": o4, "computed_at": int(time.time())}
            new_cache_entries += 1
            if (i+1) % progress_every == 0:
                print(f"  [progress] {i+1}/{len(shadows)} verdicts processed...")

        if o4:
            stats_4h[verdict][o4] += 1
            matched_4h += 1
        else:
            # либо immature (age<4h) либо нет klines
            ts = v.get("ts", 0)
            if time.time() - ts < 4 * 3600:
                immature_4h += 1
            else:
                no_data += 1
        if o1:
            stats_1h[verdict][o1] += 1

    if new_cache_entries:
        _save_cache(cache)
        print(f"  [cache] saved {new_cache_entries} new entries → {CACHE.name}")

    print()
    print(f"=== Resolved: {matched_4h}/{len(shadows)} 4h outcomes computed ===")
    print(f"=== Immature: {immature_4h} (age < 4h) ===")
    print(f"=== No-data: {no_data} (нет цены/klines/direction) ===")
    print()

    for horizon, stats in (("4h", stats_4h), ("1h", stats_1h)):
        print(f"┌── BACKTEST {horizon} ──────────────────────────────────────────┐")
        for verdict in ("GO", "SKIP", "WAIT", "FAIL_OPEN"):
            counts = stats.get(verdict, {})
            if not counts:
                continue
            total = sum(counts.values())
            w = counts.get("WIN", 0); l = counts.get("LOSS", 0); flat = counts.get("FLAT", 0)
            dec = w + l
            wr = w / dec * 100 if dec else 0
            print(f"  {verdict:10s} n={total:4d}  WIN={w:3d}  LOSS={l:3d}  FLAT={flat:3d}  "
                  f"WR={wr:5.1f}% (dec n={dec})")
        # Качество фильтра
        go_w  = stats.get("GO", {}).get("WIN", 0)
        go_l  = stats.get("GO", {}).get("LOSS", 0)
        skip_w = stats.get("SKIP", {}).get("WIN", 0)
        skip_l = stats.get("SKIP", {}).get("LOSS", 0)
        all_w = go_w + skip_w + stats.get("WAIT", {}).get("WIN", 0) + stats.get("FAIL_OPEN", {}).get("WIN", 0)
        if go_w + go_l > 0:
            print(f"  GO precision: {go_w/(go_w+go_l)*100:.1f}%")
        if all_w > 0:
            print(f"  Recall (% WIN'ов прошедших в GO): {go_w/all_w*100:.1f}%")
        if skip_w + skip_l > 0:
            print(f"  SKIP WR (counterfactual: если бы НЕ фильтровали): {skip_w/(skip_w+skip_l)*100:.1f}%")
        print()

    print("=== По stage (GO / SKIP / WAIT) ===")
    for stage, vs in sorted(by_stage.items(), key=lambda x: -sum(x[1].values())):
        print(f"  {stage:30s}  GO={vs.get('GO',0):3d}  SKIP={vs.get('SKIP',0):3d}  "
              f"WAIT={vs.get('WAIT',0):3d}  FAIL_OPEN={vs.get('FAIL_OPEN',0):3d}")
    print()
    print(f"=== Verdict sources: {dict(by_source)} ===")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=168, help="окно в часах (default 7 дней)")
    ap.add_argument("--setup", default="",            help="pump или rug_prep (фильтр)")
    ap.add_argument("--no-cache", action="store_true", help="игнорировать кеш, перепосчитать")
    args = ap.parse_args()
    analyze(args.hours, args.setup, use_cache=not args.no_cache)


if __name__ == "__main__":
    main()

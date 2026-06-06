"""
claude_filter_backtest.py — Backtest replay: проверка качества Claude RT-фильтра
на исторических данных pump_resolved.csv.

Для каждой строки CSV реконструирует candidate dict (как был в момент алерта),
прогоняет через filter_candidate, и сравнивает Claude-вердикт (GO/SKIP/WAIT)
с реальным исходом (WIN/LOSS/FLAT 4h).

Выдаёт метрики:
  - WR среди GO  vs  WR среди SKIP  (precision)
  - Какой процент реальных WIN'ов Claude пропускает  (recall)
  - Стоимость прогона

CLI:
    python3 claude_filter_backtest.py            — последние 100 сигналов
    python3 claude_filter_backtest.py --n 50     — последние N
    python3 claude_filter_backtest.py --setup pump  — только pump (без rug_prep)
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR    = Path(__file__).parent
PUMP_CSV    = BASE_DIR / "outcomes" / "pump_resolved.csv"


def _load_dotenv():
    p = BASE_DIR / ".env"
    if not p.exists():
        return
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip(); v = v.strip()
            if v and v[0] in ('"', "'") and v[-1] == v[0]:
                v = v[1:-1]
            os.environ.setdefault(k, v)


_load_dotenv()


def _row_to_candidate(row: dict) -> dict:
    """Реконструирует candidate dict из строки pump_resolved.csv."""
    stage = row.get("stage", "")
    sig_type = row.get("signal_type", "pump")
    if sig_type == "rug_prep":
        direction = "SHORT"
    else:
        direction = "LONG"
    return {
        "symbol":        row.get("symbol", "?"),
        "setup":         sig_type,
        "stage":         stage,
        "direction":     direction,
        "score":         int(float(row.get("score", 0) or 0)),
        "price":         float(row.get("price", 0) or 0),
        "funding":       float(row.get("funding", 0) or 0),
        "oi_chg_4h":     float(row.get("oi_chg_4h", 0) or 0),
        "cvd_pct":       float(row.get("cvd_pct", 0) or 0),
        "btc_4h":        float(row.get("btc_4h", 0) or 0),
        "signals":       [f"Historical {stage}"],
    }


def run_backtest(n: int = 100, setup_filter: str = "") -> dict:
    from claude_realtime_filter import filter_candidate, summarize_costs
    if not PUMP_CSV.exists():
        print(f"[ERROR] {PUMP_CSV} не найден")
        sys.exit(1)

    rows = list(csv.DictReader(open(PUMP_CSV, encoding="utf-8")))
    if setup_filter:
        rows = [r for r in rows if r.get("signal_type") == setup_filter]
    if not rows:
        print("[ERROR] нет строк после фильтрации")
        sys.exit(1)

    rows = rows[-n:]
    print(f"[Backtest] {len(rows)} строк, фильтр setup='{setup_filter or 'все'}'")
    print(f"[Backtest] стоимость ≈ ${len(rows) * 0.007:.2f}\n")

    costs_before = summarize_costs(hours=24).get("cost_usd", 0)

    # outcome_4h: WIN / LOSS / FLAT
    stats: dict = defaultdict(lambda: defaultdict(int))  # verdict -> outcome -> count
    started = time.time()

    for i, row in enumerate(rows, 1):
        cand = _row_to_candidate(row)
        outcome = (row.get("outcome_4h") or "").upper()
        if not outcome:
            continue
        # Используем уникальный symbol суффикс чтобы не попадать в cache по
        # (symbol, setup) с боевого pipeline
        sym_test = f"{cand['symbol']}_bt{i}"
        v = filter_candidate(sym_test, cand, source="backtest")
        verdict = v.get("verdict") or v.get("action")
        stats[verdict][outcome] += 1

        if i % 10 == 0:
            elapsed = time.time() - started
            print(f"  [{i}/{len(rows)}]  elapsed={elapsed:.0f}s  "
                  f"GO={sum(stats['GO'].values())}  SKIP={sum(stats['SKIP'].values())}  "
                  f"WAIT={sum(stats['WAIT'].values())}")

    elapsed = time.time() - started
    costs_after = summarize_costs(hours=24).get("cost_usd", 0)
    bt_cost = costs_after - costs_before

    # ── Сводка ────────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"BACKTEST RESULTS — {len(rows)} signals, {elapsed:.0f}s, ${bt_cost:.3f}")
    print(f"{'='*60}\n")

    for verdict in ("GO", "SKIP", "WAIT", "unknown"):
        counts = stats.get(verdict, {})
        total = sum(counts.values())
        if not total:
            continue
        wins  = counts.get("WIN", 0)
        loss  = counts.get("LOSS", 0)
        flat  = counts.get("FLAT", 0)
        dec   = wins + loss
        wr    = wins / dec * 100 if dec else 0
        print(f"  {verdict:8s}  n={total:4d}  WIN={wins:3d}  LOSS={loss:3d}  FLAT={flat:3d}  "
              f"WR={wr:5.1f}% (decisive n={dec})")

    print()
    # Quality metrics
    go_wins   = stats["GO"].get("WIN", 0)
    go_loss   = stats["GO"].get("LOSS", 0)
    skip_wins = stats["SKIP"].get("WIN", 0)
    skip_loss = stats["SKIP"].get("LOSS", 0)

    all_wins  = go_wins + skip_wins + stats["WAIT"].get("WIN", 0)
    all_loss  = go_loss + skip_loss + stats["WAIT"].get("LOSS", 0)

    if go_wins + go_loss > 0:
        precision = go_wins / (go_wins + go_loss) * 100
        print(f"  GO precision (WR среди GO): {precision:.1f}%")
    if all_wins > 0:
        recall = go_wins / all_wins * 100
        print(f"  Recall (% реальных WIN'ов прошедших Claude): {recall:.1f}%")
    if skip_wins + skip_loss > 0:
        skip_wr = skip_wins / (skip_wins + skip_loss) * 100
        print(f"  SKIP WR (что бы случилось если бы НЕ фильтровали): {skip_wr:.1f}%")
    if all_loss > 0:
        bad_filter = go_loss / all_loss * 100
        print(f"  Bad-filter (% LOSS'ов пропущенных в GO): {bad_filter:.1f}%")

    return dict(stats)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n",     type=int, default=100, help="последние N сигналов")
    ap.add_argument("--setup", default="",            help="pump или rug_prep")
    args = ap.parse_args()
    run_backtest(args.n, args.setup)


if __name__ == "__main__":
    main()

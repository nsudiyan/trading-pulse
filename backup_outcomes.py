#!/usr/bin/env python3
"""
backup_outcomes.py — еженедельный gzip-снапшот критичных данных (P1-6).

Бэкапит единственные экземпляры:
  outcomes/resolved.csv         — paper-история исходов (из git убрана этапом 0.5)
  outcomes/pump_resolved.csv    — pump/rug-история
  outcomes/trades.json          — РЕАЛЬНЫЕ сделки (живой с P0-3)
  outcomes/rejected_history.csv — персист-сток реджектов с контрфактами (A2)

→ archive/outcomes_backups/<имя>_YYYY-MM-DD.gz, хранится 8 последних на имя.

Запуск: python3 backup_outcomes.py [--dry-run]
"""

import argparse
import gzip
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path

BASE    = Path(__file__).parent
ARCHIVE = BASE / "archive" / "outcomes_backups"
KEEP    = 8

TARGETS = [
    BASE / "outcomes" / "resolved.csv",
    BASE / "outcomes" / "pump_resolved.csv",
    BASE / "outcomes" / "trades.json",
    BASE / "outcomes" / "rejected_history.csv",   # A2: персист-сток реджектов (контрфакты)
    # ── добавлено 2026-07-07 (аудит передачи вахты): невосполнимые данные
    #    боевого периода — код переписывается, форензика НИКОГДА ──
    BASE / "dashboard" / "diary.json",            # дневник: замороженные ожидания
    BASE / "dashboard" / "signals_ledger.json",                 # треки 6ч/24ч всех источников
    BASE / "dashboard" / "rose_history.json",     # посты платного канала + треки
    BASE / "outcomes" / "entry_candidates.csv",   # форензика зоны входа
    BASE / "outcomes" / "impulse_review_candidates.csv",  # отдельная очередь ручной проверки стакана
    BASE / "outcomes" / "radar_hits.csv",
    BASE / "outcomes" / "radar_resolved.csv",
    BASE / "outcomes" / "storm_stages.csv",       # стадии шторма/pump
    BASE / "outcomes" / "rose_signals.csv",
    BASE / "outcomes" / "rose_outcomes.csv",
    # ── добавлено 2026-07-10 (вердикт KILLED шестилетнего прогона): единственные
    #    экземпляры доказательного контура ──
    BASE / "backtest_6y" / "out" / "trades.csv",
    BASE / "backtest_6y" / "out" / "daily_pnl.csv",
    BASE / "backtest_6y" / "out" / "results_stats.json",
    BASE / "backtest_6y" / "out" / "meta.json",
]


def backup_backtest_contour(dry: bool, today: str):
    """backtest_6y как воспроизводимый доказательный контур (решение брата+Codex
    2026-07-10): скрипты+логи+out+вселенная+манифесты одним tar.gz с ротацией.
    Сырые klines/funding (754МБ, правая граница заморожена 2026-07-10) — НЕ здесь:
    они в разовом archive/outcomes_backups/backtest_6y_data_FROZEN_2026-07-10.tar."""
    b6 = BASE / "backtest_6y"
    if not b6.exists():
        return
    dst = ARCHIVE / f"backtest_6y_contour_{today}.tar.gz"
    parts = (list(b6.glob("*.py")) + list(b6.glob("*.log"))
             + list((b6 / "out").glob("*"))
             + list((b6 / "data").glob("universe*.json.gz"))
             + list((b6 / "data").glob("instruments_*.json"))
             + list((b6 / "data" / "manifest").glob("*.json")))
    print(f"backup контур backtest_6y ({len(parts)} файлов) → {dst.name}")
    if not dry:
        with tarfile.open(dst, "w:gz") as tf:
            for p in parts:
                tf.add(p, arcname=f"backtest_6y/{p.relative_to(b6)}")
    arch = sorted(ARCHIVE.glob("backtest_6y_contour_*.tar.gz"))
    for old in (arch[:-KEEP] if len(arch) > KEEP else []):
        print(f"  prune {old.name}")
        if not dry:
            old.unlink()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    dry = args.dry_run

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for src in TARGETS:
        if not src.exists():
            print(f"skip {src.name} — файла нет")
            continue
        dst = ARCHIVE / f"{src.stem}{src.suffix}_{today}.gz"
        print(f"backup {src.name} ({src.stat().st_size/1024:.0f} КБ) → {dst.name}")
        if not dry:
            with open(src, "rb") as fin, gzip.open(dst, "wb") as fout:
                shutil.copyfileobj(fin, fout)
        # ротация: 8 последних на имя
        arch = sorted(ARCHIVE.glob(f"{src.stem}{src.suffix}_*.gz"))
        for old in arch[:-KEEP] if len(arch) > KEEP else []:
            print(f"  prune {old.name}")
            if not dry:
                old.unlink()

    backup_backtest_contour(dry, today)


if __name__ == "__main__":
    main()

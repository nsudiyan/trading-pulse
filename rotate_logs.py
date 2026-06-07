#!/usr/bin/env python3
"""
rotate_logs.py — еженедельная ротация логов (P1-6, 2026-06-07).

*.log в корне > 10 МБ → gzip в archive/logs/<имя>.<YYYY-MM-DD>.gz + транкейт.
Максимум 4 архива на имя (старейшие удаляются).

Для stdout-редиректов РЕЗИДЕНТНЫХ launchd-демонов после транкейта — kickstart -k
(сброс файлового дескриптора писателя, иначе sparse-смещение).
Календарные демоны (screener и пр.) в словаре ОТСУТСТВУЮТ ОСОЗНАННО:
kickstart -k запустил бы им внеплановый прогон с алертами; их писатель
в момент ротации (вс 12:20) спит, транкейт безопасен без рестарта.
Python-logging логи (claude_realtime_filter.log) — append-mode, транкейт безопасен.

Запуск: python3 rotate_logs.py [--dry-run]
"""

import argparse
import gzip
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

BASE        = Path(__file__).parent
ARCHIVE     = BASE / "archive" / "logs"
SIZE_LIMIT  = 10 * 1024 * 1024   # 10 МБ
KEEP        = 4                  # архивов на имя

# stdout/err-редиректы РЕЗИДЕНТНЫХ демонов → label для kickstart -k.
# НЕ добавлять календарные (screener, weekly, scannerdigest, forwardreport,
# channelreader, pumpfadeforward) — kickstart запустит им внеплановый прогон!
LAUNCHD_RESIDENT_LOGS = {
    # monitor.log убран P1-8c: com.trading.monitor выключен (.disabled)
    "liqtracker.log":          "com.trading.liqtracker",
    "liqtracker_error.log":    "com.trading.liqtracker",
    "trade_watcher.log":       "com.trading.tradewatcher",
    "trade_watcher_error.log": "com.trading.tradewatcher",
    "pump_detector.log":       "com.trading.pumpdetector",
    "pump_detector_error.log": "com.trading.pumpdetector",
    "boost_watcher.log":       "com.trading.boostwatcher",
    "boost_watcher_error.log": "com.trading.boostwatcher",
    "bot.log":                 "com.trading.bot",
    "bot_error.log":           "com.trading.bot",
    "dashboard_server.log":    "com.trading.dashboard",
}


def _kickstart(label: str, dry: bool):
    uid = os.getuid()
    if dry:
        print(f"  [dry] kickstart -k gui/{uid}/{label}")
        return
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
                   capture_output=True, timeout=30)


def _prune_old(name: str, dry: bool):
    arch = sorted(ARCHIVE.glob(f"{name}.*.gz"))
    for old in arch[:-KEEP] if len(arch) > KEEP else []:
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
    restart_labels = set()
    rotated = 0

    for log in sorted(BASE.glob("*.log")):
        size = log.stat().st_size
        if size <= SIZE_LIMIT:
            continue
        dst = ARCHIVE / f"{log.name}.{today}.gz"
        print(f"rotate {log.name} ({size/1048576:.1f} МБ) → {dst.relative_to(BASE)}")
        if not dry:
            with open(log, "rb") as fin, gzip.open(dst, "wb") as fout:
                shutil.copyfileobj(fin, fout)
            with open(log, "w"):   # транкейт
                pass
        rotated += 1
        _prune_old(log.name, dry)
        label = LAUNCHD_RESIDENT_LOGS.get(log.name)
        if label:
            restart_labels.add(label)

    for label in sorted(restart_labels):
        print(f"kickstart -k {label} (сброс fd писателя)")
        _kickstart(label, dry)

    if not rotated:
        print("ничего не ротировано (все логи ≤ 10 МБ)")


if __name__ == "__main__":
    main()

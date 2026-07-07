#!/usr/bin/env python3
"""refresh_outcomes.py — периодический пересчёт отработок (launchd, ~15 мин).

1) Rose-каналы: новые посты из TG → треки → отработка (rose_history.py).
2) Ledger всех сигналов дашборда: отработка нефинальных треков той же
   методикой (6ч старт / 24ч итог, пере-якорение при подтверждении делает
   dashboard_feed.update_ledger).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR))
sys.path.insert(0, str(DIR.parent))  # file_lock живёт в ~/trading

import rose_history  # noqa: E402

LEDGER_PATH = DIR / "signals_ledger.json"


def main():
    # 1) Rose: инкрементальный fetch + resolve
    try:
        store = rose_history.load_store()
        signals = rose_history.fetch_new_signals(store, backfill_days=30)
        rose_history.add_to_tracks(store, signals)
        rose_history.save_store(store)
        rose_history.resolve_tracks(store, limit=120)
        rose_history.save_store(store)
    except Exception as e:
        print(f"[refresh] rose FAIL: {e}", file=sys.stderr)

    # 2) Ledger: resolve нефинальных (формат треков одинаковый).
    # Резолвим СНИМОК (сеть на минуты), а записываем МЕРЖЕМ outcome по id
    # под file_lock — параллельный dashboard_feed каждые 60с добавляет треки,
    # слепой write_text стирал его работу (lost-update, код-ревью 2026-07-06 #2).
    try:
        snapshot = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except Exception:
        snapshot = None
    if snapshot:
        rose_history.resolve_tracks(snapshot, limit=150)
        resolved = {t["id"]: t for t in snapshot.get("tracks", []) if t.get("outcome")}

        def merge(ledger):
            if not isinstance(ledger, dict):
                return snapshot  # файл исчез/битый — снимок лучше, чем ничего
            n = 0
            for t in ledger.get("tracks", []):
                src = resolved.get(t["id"])
                # переякоренный конфирмом трек (anchor сменился после снимка) —
                # его outcome устарел, пересчитается следующим циклом
                if src and src.get("anchor_ts") == t.get("anchor_ts"):
                    t["outcome"] = src["outcome"]
                    n += 1
            print(f"[refresh] ledger merged: outcome у {n} треков")
            return ledger

        from file_lock import atomic_json_update
        atomic_json_update(LEDGER_PATH, merge,
                           default={"tracks": [], "seen_ids": []})

    # 3) 📓 дневник трейдера: резолв незакрытых записей (fail-open)
    try:
        import diary
        n = diary.resolve_pending()
        if n:
            print(f"[refresh] diary resolved: {n}")
    except Exception as e:
        print(f"[refresh] diary пропущен: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

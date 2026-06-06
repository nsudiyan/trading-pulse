"""
file_lock.py — Кроссплатформенный файловый локинг (Mac/Linux/Windows).

Использование:
    from file_lock import atomic_json_update

    def _mutate(data: dict) -> dict:
        data["counter"] = data.get("counter", 0) + 1
        return data

    atomic_json_update(Path("outcomes/wait_watchlist.json"), _mutate, default={})

Гарантирует:
  - Атомарную замену файла (через временный файл + rename)
  - Эксклюзивную блокировку на время операции
  - Безопасный fallback при corrupt JSON (использует default)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


@contextmanager
def _file_lock(path: Path, timeout: float = 5.0):
    """Эксклюзивный лок на отдельный .lock-файл рядом с целевым."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fp = open(lock_path, "a+b")
    try:
        while True:
            try:
                if sys.platform == "win32":
                    msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Не удалось залочить {lock_path} за {timeout}с")
                time.sleep(0.05)
        yield
    finally:
        try:
            if sys.platform == "win32":
                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fp.close()


def atomic_json_update(
    path: Path,
    mutator: Callable[[Any], Any],
    default: Any = None,
    timeout: float = 5.0,
) -> Any:
    """
    Атомарное read-modify-write для JSON-файла под локом.
    mutator(data) → new_data. Возвращает new_data.
    """
    with _file_lock(path, timeout=timeout):
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
        except (json.JSONDecodeError, OSError):
            data = default

        new_data = mutator(data)

        # Атомарная запись: temp-файл в той же директории + os.replace
        dir_ = path.parent
        dir_.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", delete=False,
            dir=str(dir_), prefix=path.name + ".", suffix=".tmp",
        ) as tmp:
            json.dump(new_data, tmp, ensure_ascii=False, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, path)
        return new_data


def atomic_json_read(path: Path, default: Any = None) -> Any:
    """Безопасное чтение JSON под shared-локом."""
    with _file_lock(path):
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
        except (json.JSONDecodeError, OSError):
            return default

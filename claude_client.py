"""
claude_client.py — Единый Anthropic-клиент на процесс (singleton).

Раньше claude_realtime_filter, chart_analyzer и claude_analyst каждый
создавал свой `anthropic.Anthropic()` (claude_analyst — на КАЖДЫЙ вызов) →
накопление CLOSE_WAIT сокетов → `Too many open files` → краш pumpdetector.

Теперь один httpx connection pool на процесс. Разные timeout'ы по месту
вызова задаются через `get_client().with_options(timeout=...)` — это
переиспользует тот же pool (проверено: with_options не создаёт новых
сокетов), поэтому быстрый fail у RT-фильтра и длинный таймаут у vision
chart-анализа сосуществуют без утечки.
"""

from __future__ import annotations

import os
from typing import Optional

_CLIENT = None


def get_client(api_key: Optional[str] = None):
    """
    Singleton `anthropic.Anthropic` на процесс. Без baked-in timeout —
    переопределяй по месту через `get_client().with_options(timeout=...)`.

    Бросает RuntimeError если ключа нет (вызывающий ловит и fail-open'ит).
    """
    global _CLIENT
    if _CLIENT is None:
        import anthropic
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY не задан — клиент Claude не создать")
        _CLIENT = anthropic.Anthropic(api_key=key)
    return _CLIENT

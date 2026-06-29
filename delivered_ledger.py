"""
Immutable trigger-ledger — ground truth доставленных торговых алертов.

Принцип: APPEND-ONLY, строка пишется ОДИН раз в момент успешной доставки в TG,
НИКОГДА не редактируется и НЕ ротируется. Это снимок t0 ("что показали"):
фактические уровни + levels_source (atr|claude). Исход (PnL) — отдельный файл
delivered_resolved.csv, ссылается по alert_id (шаг 2).

Зачем: resolved.csv меряет фантомную популяцию (всё ≥min_score) по ATR-формуле,
а торгуется Claude-одобренное подмножество по Claude-уровням. Эдж честно считается
ТОЛЬКО на join(delivered_alerts, delivered_resolved) по alert_id на forward-окне.
"""
from __future__ import annotations
import csv
import os
from datetime import datetime, timezone
from pathlib import Path

LEDGER_PATH = Path(__file__).parent / "outcomes" / "delivered_alerts.csv"

# Порядок колонок фиксирован — append дописывает строго по нему.
FIELDS = [
    "alert_id", "delivered_ts", "symbol", "side", "setup",
    "entry", "stop", "tp1", "tp2", "levels_source",
    "score", "grade", "claude_verdict", "claude_confidence", "macro_veto",
    "tg_message_id", "screener_run_ts", "raw_trigger",
]


def record_delivered(*, alert_id, symbol, side, setup,
                     entry, stop, tp1, tp2, levels_source,
                     score=None, grade=None, claude_verdict=None,
                     claude_confidence=None, macro_veto=False,
                     tg_message_id=None, screener_run_ts=None,
                     raw_trigger=None, _path: Path | None = None) -> None:
    """Дописывает ОДНУ immutable-строку снимка доставки. Идемпотентно по alert_id:
    если строка с этим alert_id уже есть — НЕ дублирует (повторная доставка/ретрай
    не должны плодить записи). delivered_ts ставится здесь (UTC aware)."""
    path = _path or LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    # Идемпотентность: один alert_id = одна строка (снимок t0 не переписываем).
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if row and row[0] == str(alert_id):
                    return  # уже зафиксирован — выходим, не трогаем существующее

    if levels_source not in ("atr", "claude"):
        raise ValueError(f"levels_source must be 'atr'|'claude', got {levels_source!r}")
    if side not in ("long", "short"):
        # граница торгового направления: не-long молча трактовался бы резолвером как ШОРТ (инверсия PnL)
        raise ValueError(f"side must be 'long'|'short', got {side!r}")

    rec = {
        "alert_id": alert_id,
        "delivered_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "symbol": symbol, "side": side, "setup": setup,
        "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
        "levels_source": levels_source,
        "score": score, "grade": grade,
        "claude_verdict": claude_verdict, "claude_confidence": claude_confidence,
        "macro_veto": bool(macro_veto),
        "tg_message_id": tg_message_id,
        "screener_run_ts": screener_run_ts,
        "raw_trigger": raw_trigger,
    }

    write_header = not path.exists() or path.stat().st_size == 0
    # append-mode: одна короткая строка на локальной ФС — атомарна (PIPE_BUF). iCloud убран.
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        w.writerow(rec)


if __name__ == "__main__":
    # Self-check: append + идемпотентность + immutability заголовка. Без фреймворков.
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "delivered_alerts.csv"
    common = dict(symbol="BTCUSDT", side="long", setup="breakout",
                  entry=100.0, stop=98.0, tp1=104.0, tp2=108.0,
                  levels_source="claude", score=120, grade="A",
                  claude_verdict="GO", claude_confidence=0.7,
                  macro_veto=False, tg_message_id=861,
                  screener_run_ts="2026-06-29T16:40", raw_trigger="breakout|score=120")
    record_delivered(alert_id="abc123", _path=tmp, **common)
    record_delivered(alert_id="abc123", _path=tmp, **common)  # дубль — не должен записаться
    record_delivered(alert_id="def456", _path=tmp, **{**common, "levels_source": "atr"})

    rows = list(csv.DictReader(tmp.open(encoding="utf-8")))
    assert len(rows) == 2, f"идемпотентность сломана: {len(rows)} строк (ждали 2)"
    assert rows[0]["alert_id"] == "abc123" and rows[1]["alert_id"] == "def456"
    assert rows[0]["levels_source"] == "claude" and rows[1]["levels_source"] == "atr"
    assert rows[0]["delivered_ts"].startswith("2026") and "+0000" in rows[0]["delivered_ts"]
    assert rows[0]["macro_veto"] == "False"
    try:
        record_delivered(alert_id="bad", _path=tmp, **{**common, "levels_source": "guess"})
        raise SystemExit("FAIL: невалидный levels_source должен был кинуть ValueError")
    except ValueError:
        pass
    print(f"✓ self-check passed: {len(rows)} immutable rows, идемпотентность + валидация OK")
    print(f"  файл: {tmp}")

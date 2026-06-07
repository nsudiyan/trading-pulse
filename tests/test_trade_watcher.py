"""
Unit tests for trade_watcher.evaluate_trade — матрица направлений × уровней.
Чистая логика на фейковых ценах, без API/сети/файлов.
Run: pytest tests/test_trade_watcher.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from trade_watcher import evaluate_trade, build_message, _as_float


# ──────────────────────────────────────────────────────────────────
# Helpers — фейковая открытая сделка
# ──────────────────────────────────────────────────────────────────

def _trade(direction, *, entry=100.0, sl=None, tp1=None):
    return {
        "trade_id":   "deadbeef-0000-0000-0000-000000000000",
        "symbol":     "TESTUSDT",
        "direction":  direction,
        "status":     "open",
        "entry_price": entry,
        "stop_price":  sl,
        "tp1_price":   tp1,
    }


# ──────────────────────────────────────────────────────────────────
# LONG: SL ниже входа, TP1 выше входа
# ──────────────────────────────────────────────────────────────────

class TestLong:
    def test_long_sl_touched(self):
        # SL=90, цена упала до/ниже SL → касание SL
        t = _trade("long", sl=90.0, tp1=120.0)
        assert evaluate_trade(t, 90.0) == ["sl"]
        assert evaluate_trade(t, 89.5) == ["sl"]

    def test_long_sl_not_touched_above(self):
        t = _trade("long", sl=90.0, tp1=120.0)
        assert evaluate_trade(t, 90.01) == []   # цена выше SL — тихо

    def test_long_tp1_touched(self):
        t = _trade("long", sl=90.0, tp1=120.0)
        assert evaluate_trade(t, 120.0) == ["tp1"]
        assert evaluate_trade(t, 125.0) == ["tp1"]

    def test_long_tp1_not_touched_below(self):
        t = _trade("long", sl=90.0, tp1=120.0)
        assert evaluate_trade(t, 119.99) == []

    def test_long_inside_range_silent(self):
        t = _trade("long", sl=90.0, tp1=120.0)
        assert evaluate_trade(t, 100.0) == []   # вход между уровнями — ничего


# ──────────────────────────────────────────────────────────────────
# SHORT: SL выше входа, TP1 ниже входа (зеркало)
# ──────────────────────────────────────────────────────────────────

class TestShort:
    def test_short_sl_touched(self):
        # SL=110, цена выросла до/выше SL → касание SL
        t = _trade("short", sl=110.0, tp1=80.0)
        assert evaluate_trade(t, 110.0) == ["sl"]
        assert evaluate_trade(t, 111.0) == ["sl"]

    def test_short_sl_not_touched_below(self):
        t = _trade("short", sl=110.0, tp1=80.0)
        assert evaluate_trade(t, 109.99) == []

    def test_short_tp1_touched(self):
        t = _trade("short", sl=110.0, tp1=80.0)
        assert evaluate_trade(t, 80.0) == ["tp1"]
        assert evaluate_trade(t, 75.0) == ["tp1"]

    def test_short_tp1_not_touched_above(self):
        t = _trade("short", sl=110.0, tp1=80.0)
        assert evaluate_trade(t, 80.01) == []

    def test_short_inside_range_silent(self):
        t = _trade("short", sl=110.0, tp1=80.0)
        assert evaluate_trade(t, 100.0) == []


# ──────────────────────────────────────────────────────────────────
# None-уровни: sl/tp могут быть None (Claude не дал) → уровень пропускается
# ──────────────────────────────────────────────────────────────────

class TestNoneLevels:
    def test_long_only_sl(self):
        t = _trade("long", sl=90.0, tp1=None)
        assert evaluate_trade(t, 89.0) == ["sl"]
        assert evaluate_trade(t, 200.0) == []   # tp1 None → не сработает

    def test_long_only_tp1(self):
        t = _trade("long", sl=None, tp1=120.0)
        assert evaluate_trade(t, 121.0) == ["tp1"]
        assert evaluate_trade(t, 1.0) == []     # sl None → не сработает

    def test_both_none_never_fires(self):
        t = _trade("long", sl=None, tp1=None)
        assert evaluate_trade(t, 1.0) == []
        assert evaluate_trade(t, 1e9) == []

    def test_empty_string_treated_as_none(self):
        # В trades.json может прилететь "" вместо None
        t = _trade("long", sl="", tp1="")
        assert evaluate_trade(t, 1.0) == []

    def test_zero_level_treated_as_none(self):
        # 0/отрицательная цена уровня бессмысленна → как None
        t = _trade("long", sl=0.0, tp1=-5.0)
        assert evaluate_trade(t, 1.0) == []


# ──────────────────────────────────────────────────────────────────
# Одновременное касание SL и TP1 (внутрибарный whipsaw) — оба уровня
# ──────────────────────────────────────────────────────────────────

class TestBothLevels:
    def test_long_both_hit(self):
        # Цена вылетела за TP1, но SL тоже технически ≤ цены? нет —
        # для long SL ниже входа: чтобы оба, цена должна быть и ≤sl и ≥tp1 (невозможно при sl<tp1).
        # Берём вырожденный кейс sl>tp1 (перепутаны) — функция честно вернёт оба.
        t = _trade("long", sl=120.0, tp1=110.0)
        hits = evaluate_trade(t, 115.0)
        assert "sl" in hits and "tp1" in hits


# ──────────────────────────────────────────────────────────────────
# build_message — направление в подсказке /close, цвет по уровню
# ──────────────────────────────────────────────────────────────────

class TestMessage:
    def test_sl_message_red_and_minus_r(self):
        t = _trade("long", sl=90.0)
        m = build_message(t, "sl", 89.5)
        assert "🔴" in m
        assert "TESTUSDT" in m
        assert "/close TESTUSDT -1R" in m

    def test_tp1_message_green_and_plus_r(self):
        t = _trade("short", tp1=80.0)
        m = build_message(t, "tp1", 79.0)
        assert "🟢" in m
        assert "/close TESTUSDT +1R" in m

    def test_message_has_short_id(self):
        t = _trade("long", sl=90.0)
        m = build_message(t, "sl", 89.0)
        assert "#deadbeef" in m   # короткий хвост trade_id


# ──────────────────────────────────────────────────────────────────
# _as_float — нормализация уровней
# ──────────────────────────────────────────────────────────────────

class TestAsFloat:
    def test_valid(self):
        assert _as_float("1.5") == 1.5
        assert _as_float(2) == 2.0

    def test_none_and_empty(self):
        assert _as_float(None) is None
        assert _as_float("") is None

    def test_non_positive(self):
        assert _as_float(0) is None
        assert _as_float(-1) is None

    def test_garbage(self):
        assert _as_float("abc") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])

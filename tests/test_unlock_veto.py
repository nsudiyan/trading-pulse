"""Регресс на token-unlock veto в claude_realtime_filter.filter_candidate.

Мокаем Claude API и все side-effect'ы (shadow log, cache, chart), чтобы
изолировать логику veto и не трогать живые файлы/сеть.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import claude_realtime_filter as crf


@pytest.fixture
def isolated_filter(monkeypatch):
    """Глушит всё вокруг veto: Claude → GO, side-effects → no-op."""
    monkeypatch.setattr(crf, "is_enabled", lambda: True)
    monkeypatch.setattr(crf, "_ensure_macro_thread", lambda: None)
    monkeypatch.setattr(crf, "_maybe_chart_b64", lambda *a, **k: None)
    monkeypatch.setattr(crf, "_shadow_log", lambda *a, **k: None)
    monkeypatch.setattr(crf, "_persist_verdict_cache_entry", lambda *a, **k: None)
    monkeypatch.setattr(crf, "_add_to_watchlist", lambda *a, **k: None)
    monkeypatch.setattr(crf, "build_context", lambda *a, **k: "ctx")
    monkeypatch.setattr(crf, "_call_claude", lambda *a, **k: {
        "verdict": "GO", "confidence": 0.7, "tp_pct": 12.0, "sl_pct": 3.0,
        "reasoning": "strong setup", "risks": [],
    })
    crf._VERDICT_CACHE.clear()
    yield monkeypatch
    crf._VERDICT_CACHE.clear()


def _cand(direction="LONG"):
    return {"setup": "squeeze", "direction": direction, "score": 90, "price": 1.0}


def test_imminent_large_unlock_vetoes_long_go(isolated_filter):
    isolated_filter.setattr(crf, "_safe_unlock", lambda sym: {
        "date": "2026-05-28", "days_until": 2, "pct_of_supply": 5.0,
        "usd_value": 100e6, "type": "cliff",
    })
    out = crf.filter_candidate("ARBUSDT", _cand("LONG"), source="screener")
    assert out["action"] == "SKIP"
    assert "UNLOCK VETO" in out["reasoning"]
    assert out["confidence"] == 0.0


def test_no_unlock_keeps_go(isolated_filter):
    isolated_filter.setattr(crf, "_safe_unlock", lambda sym: None)
    out = crf.filter_candidate("ARBUSDT", _cand("LONG"), source="screener")
    assert out["action"] == "GO"


def test_far_unlock_keeps_go(isolated_filter):
    isolated_filter.setattr(crf, "_safe_unlock", lambda sym: {
        "date": "2026-06-20", "days_until": 25, "pct_of_supply": 5.0,
        "usd_value": 100e6, "type": "cliff",
    })
    out = crf.filter_candidate("ARBUSDT", _cand("LONG"), source="screener")
    assert out["action"] == "GO"


def test_small_unlock_keeps_go(isolated_filter):
    isolated_filter.setattr(crf, "_safe_unlock", lambda sym: {
        "date": "2026-05-28", "days_until": 2, "pct_of_supply": 1.0,
        "usd_value": 5e6, "type": "linear",
    })
    out = crf.filter_candidate("ARBUSDT", _cand("LONG"), source="screener")
    assert out["action"] == "GO"


def test_short_not_vetoed_by_unlock(isolated_filter):
    """Анлок — попутный ветер для SHORT, veto только LONG."""
    isolated_filter.setattr(crf, "_safe_unlock", lambda sym: {
        "date": "2026-05-28", "days_until": 2, "pct_of_supply": 5.0,
        "usd_value": 100e6, "type": "cliff",
    })
    out = crf.filter_candidate("ARBUSDT", _cand("SHORT"), source="screener")
    assert out["action"] == "GO"

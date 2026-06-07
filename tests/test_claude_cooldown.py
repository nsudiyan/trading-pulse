"""Регресс на F-61: персистентный общий cooldown/дневной-лимит Claude-фильтра.

Доказываем три свойства БЕЗ единого реального вызова Claude (общий кошелёк!):
  (а) два вызова filter_candidate для одного symbol+setup+direction в ОДНОМ процессе →
      первый GO (мок), второй отбит cooldown'ом ДО обращения к API (счётчик API=1);
  (б) «kill+restart»: два вызова в РАЗНЫХ python-процессах через ОДИН файл-стор →
      второй отбит (персистентность);
  (в) дневной лимит date-keyed переживает «рестарт» аналогично.

Все вызовы Claude замоканы. Боевой стор не трогаем — env CLAUDE_COOLDOWN_STORE
указывает на временный файл (tmp_path).
"""
import json
import os
import sys
import subprocess
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import claude_realtime_filter as crf

from pathlib import Path


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Глушит всё вокруг cooldown'а; Claude → GO со счётчиком вызовов; стор → tmp."""
    monkeypatch.setattr(crf, "is_enabled", lambda: True)
    monkeypatch.setattr(crf, "_ensure_macro_thread", lambda: None)
    monkeypatch.setattr(crf, "_maybe_chart_b64", lambda *a, **k: None)
    monkeypatch.setattr(crf, "_shadow_log", lambda *a, **k: None)
    monkeypatch.setattr(crf, "_persist_verdict_cache_entry", lambda *a, **k: None)
    monkeypatch.setattr(crf, "_add_to_watchlist", lambda *a, **k: None)
    monkeypatch.setattr(crf, "build_context", lambda *a, **k: "ctx")
    monkeypatch.setattr(crf, "_safe_unlock", lambda sym: None)
    # verdict-кэш отдельный от cooldown: явно очищаем, чтобы second-call шёл через cooldown,
    # а НЕ через verdict-cache hit (иначе доказательство было бы ложным).
    crf._VERDICT_CACHE.clear()

    # Счётчик реальных «вызовов API» — должен остаться 1.
    calls = {"n": 0}

    def _fake_call(*a, **k):
        calls["n"] += 1
        return {"verdict": "GO", "confidence": 0.7, "tp_pct": 12.0, "sl_pct": 3.0,
                "reasoning": "strong setup", "risks": []}

    monkeypatch.setattr(crf, "_call_claude", _fake_call)

    store = tmp_path / "claude_cooldown.json"
    monkeypatch.setattr(crf, "COOLDOWN_STORE_PATH", store)
    yield {"calls": calls, "store": store, "monkeypatch": monkeypatch}
    crf._VERDICT_CACHE.clear()


def _cand(direction="LONG", setup="squeeze"):
    return {"setup": setup, "direction": direction, "score": 90, "price": 1.0}


# ── (а) intra-process: второй вызов отбит ДО API ──────────────────────────────

def test_same_process_second_call_blocked_before_api(isolated):
    calls = isolated["calls"]
    out1 = crf.filter_candidate("AAATEST", _cand("LONG"), source="screener")
    assert out1["action"] == "GO"
    assert calls["n"] == 1

    out2 = crf.filter_candidate("AAATEST", _cand("LONG"), source="screener")
    # отбит cooldown'ом, причём ДО обращения к API → счётчик не вырос
    assert out2["action"] == "SKIP"
    assert out2["source"] == "cooldown"
    assert calls["n"] == 1, "второй вызов НЕ должен трогать Claude API"


def test_different_direction_not_blocked(isolated):
    """Ключ включает direction — LONG-отметка не блокирует SHORT тот же символ+setup."""
    calls = isolated["calls"]
    crf.filter_candidate("BBBTEST", _cand("LONG"), source="screener")
    # verdict-кэш keyed (symbol,setup) без direction — чистим, чтобы изолировать cooldown:
    # иначе SHORT поймает кэшированный GO от LONG и до cooldown-логики не дойдёт.
    crf._VERDICT_CACHE.clear()
    out = crf.filter_candidate("BBBTEST", _cand("SHORT"), source="screener")
    assert out["action"] == "GO"
    assert calls["n"] == 2


def test_skip_verdict_does_not_set_cooldown(isolated, monkeypatch):
    """SKIP не порождает отправку → отметка НЕ ставится (повтор снова доходит до API)."""
    calls = isolated["calls"]

    def _skip_call(*a, **k):
        calls["n"] += 1   # считаем обращение к API так же, как фикстура
        return {"verdict": "SKIP", "confidence": 0.1, "tp_pct": 0.0, "sl_pct": 0.0,
                "reasoning": "weak", "risks": []}

    monkeypatch.setattr(crf, "_call_claude", _skip_call)
    crf.filter_candidate("CCCTEST", _cand("LONG"), source="screener")
    # verdict-кэш вернул бы тот же SKIP без API — чистим, изолируем cooldown-маркировку.
    crf._VERDICT_CACHE.clear()
    crf.filter_candidate("CCCTEST", _cand("LONG"), source="screener")
    assert calls["n"] == 2, "SKIP не маркирует cooldown — оба вызова доходят до API"
    assert not isolated["store"].exists() or "CCCTEST" not in isolated["store"].read_text()


# ── (б) cross-process «kill+restart»: персистентность через файл ──────────────

_SUBPROC = textwrap.dedent(
    """
    import os, sys, json
    sys.path.insert(0, {root!r})
    os.environ["CLAUDE_COOLDOWN_STORE"] = {store!r}
    import claude_realtime_filter as crf

    # Мокаем всё, как в тесте — НИ ОДНОГО реального вызова Claude.
    crf.is_enabled = lambda: True
    crf._ensure_macro_thread = lambda: None
    crf._maybe_chart_b64 = lambda *a, **k: None
    crf._shadow_log = lambda *a, **k: None
    crf._persist_verdict_cache_entry = lambda *a, **k: None
    crf._add_to_watchlist = lambda *a, **k: None
    crf.build_context = lambda *a, **k: "ctx"
    crf._safe_unlock = lambda sym: None
    _calls = {{"n": 0}}
    def _fake(*a, **k):
        _calls["n"] += 1
        return {{"verdict": "GO", "confidence": 0.7, "tp_pct": 12.0, "sl_pct": 3.0,
                 "reasoning": "x", "risks": []}}
    crf._call_claude = _fake
    crf._VERDICT_CACHE.clear()

    out = crf.filter_candidate("DDDTEST", {{"setup": "squeeze", "direction": "LONG",
                                            "score": 90, "price": 1.0}}, source="screener")
    print(json.dumps({{"action": out["action"], "verdict_source": out.get("source"),
                       "api_calls": _calls["n"]}}))
    """
)


def _run_subprocess(root, store):
    code = _SUBPROC.format(root=root, store=str(store))
    res = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, f"subprocess failed:\n{res.stderr}"
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_persists_across_process_restart(isolated):
    """Процесс №1 отправил GO (отметка на диск) → процесс №2 (свежий) отбит cooldown'ом."""
    root = os.path.dirname(os.path.dirname(__file__))
    store = isolated["store"]

    r1 = _run_subprocess(root, store)
    assert r1["action"] == "GO"
    assert r1["api_calls"] == 1

    # «kill + restart» — НОВЫЙ процесс, in-memory обнулён, но файл-стор жив.
    r2 = _run_subprocess(root, store)
    assert r2["action"] == "SKIP", "второй процесс должен видеть persisted cooldown"
    assert r2["verdict_source"] == "cooldown"
    assert r2["api_calls"] == 0, "второй процесс НЕ должен трогать Claude API"


# ── (в) дневной лимит date-keyed переживает рестарт ───────────────────────────

def test_daily_limit_persists_and_blocks(isolated, monkeypatch):
    """Предзаполняем стор лимитом на UTC-сегодня → новый символ отбит лимитом ДО API."""
    calls = isolated["calls"]
    store = isolated["store"]
    today = crf._utc_date_str()

    # «Рестарт»: на диске уже исчерпанный дневной лимит источника (пер-источник,
    # решение владельца 07.06) — как после предыдущей сессии.
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({
        "cooldown": {},
        "daily": {today: {"screener": crf.MAX_DAILY_GO}},
    }), encoding="utf-8")

    out = crf.filter_candidate("EEETEST", _cand("LONG"), source="screener")
    assert out["action"] == "SKIP"
    assert out["source"] == "cooldown"
    assert "daily limit" in out["reasoning"].lower()
    assert calls["n"] == 0, "дневной лимит отбивает ДО обращения к API"


def test_daily_limit_increments_on_sendable(isolated):
    """Каждый GO бампит счётчик; на (MAX_DAILY_GO+1)-м символе срабатывает лимит."""
    calls = isolated["calls"]
    store = isolated["store"]
    n = crf.MAX_DAILY_GO
    # n уникальных символов → n GO (cooldown по символу не мешает, символы разные)
    for i in range(n):
        out = crf.filter_candidate(f"SYM{i}TEST", _cand("LONG"), source="screener")
        assert out["action"] == "GO", f"символ {i} ожидался GO"
    assert calls["n"] == n
    today = crf._utc_date_str()
    data = json.loads(store.read_text())
    assert data["daily"][today]["screener"] == n

    # (n+1)-й символ — лимит исчерпан, отбит ДО API
    out = crf.filter_candidate("OVERFLOWTEST", _cand("LONG"), source="screener")
    assert out["action"] == "SKIP"
    assert out["source"] == "cooldown"
    assert calls["n"] == n, "переполнение лимита не должно трогать API"


def test_daily_limit_per_source_independent(isolated):
    """Лимит ПЕР-ИСТОЧНИК (решение владельца 07.06): исчерпанный screener
    НЕ блокирует pump_detector — у того свой счётчик."""
    calls = isolated["calls"]
    store = isolated["store"]
    today = crf._utc_date_str()
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({
        "cooldown": {},
        "daily": {today: {"screener": crf.MAX_DAILY_GO}},
    }), encoding="utf-8")

    out_scr = crf.filter_candidate("FFFTEST", _cand("LONG"), source="screener")
    assert out_scr["action"] == "SKIP" and out_scr["source"] == "cooldown"
    assert calls["n"] == 0

    crf._VERDICT_CACHE.clear()
    out_pump = crf.filter_candidate("FFFTEST", _cand("LONG"), source="pump_detector")
    assert out_pump["action"] == "GO", "pump_detector со своим счётчиком не должен быть заблокирован"
    assert calls["n"] == 1


# ── промоушен из wait_watchlist идёт МИМО precheck ────────────────────────────

def test_watchlist_promotion_bypasses_cooldown(isolated):
    """Решение владельца 07.06: сработавший watchlist-trigger = подтверждение, не дубль.
    Кандидат с _watchlist_promotion=True проходит мимо персист-кулдауна до API
    (иначе macro-veto WAIT самоблокировал бы свой апгрейд: cooldown 8ч > WATCH_TTL 4ч)."""
    calls = isolated["calls"]
    # Первый send-able вердикт ставит cooldown-отметку.
    out1 = crf.filter_candidate("GGGTEST", _cand("LONG"), source="screener")
    assert out1["action"] == "GO" and calls["n"] == 1

    # Обычный повтор — отбит (контроль).
    crf._VERDICT_CACHE.clear()
    out2 = crf.filter_candidate("GGGTEST", _cand("LONG"), source="screener")
    assert out2["action"] == "SKIP" and out2["source"] == "cooldown"
    assert calls["n"] == 1

    # Промоушен — тот же ключ, но с флагом: доходит до API и получает GO.
    crf._VERDICT_CACHE.clear()
    cand = _cand("LONG")
    cand["_watchlist_promotion"] = True
    out3 = crf.filter_candidate("GGGTEST", cand, source="screener")
    assert out3["action"] == "GO", "промоушен не должен отбиваться собственным cooldown'ом"
    assert calls["n"] == 2
    assert "_watchlist_promotion" not in cand, "флаг служебный — должен сниматься (pop)"


def test_pump_source_uses_4h_ttl(isolated):
    """source=='pump_detector' использует проектный 4ч TTL (а не 8ч дефолт)."""
    assert crf._cooldown_ttl("pump_detector") == crf.COOLDOWN_SEC_PUMP == 14400
    assert crf._cooldown_ttl("screener") == crf.COOLDOWN_SEC_DEFAULT == 8 * 3600

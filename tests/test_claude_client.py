"""Регресс на единый Anthropic-клиент (шов claude_client).

Ключевой инвариант: singleton (один pool на процесс) + with_options не
создаёт новый pool (иначе вернётся утечка сокетов, ради которой шов и делали).
Не требует API-ключа с правами — anthropic.Anthropic() не ходит в сеть при создании.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import claude_client


def _reset():
    claude_client._CLIENT = None


def test_singleton_same_instance(monkeypatch):
    _reset()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    assert claude_client.get_client() is claude_client.get_client()
    _reset()


def test_explicit_key_overrides_env(monkeypatch):
    _reset()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert claude_client.get_client(api_key="explicit") is not None
    _reset()


def test_no_key_raises(monkeypatch):
    _reset()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        claude_client.get_client()
    _reset()


def test_with_options_reuses_pool(monkeypatch):
    """Per-request timeout НЕ должен создавать новый httpx pool — иначе утечка."""
    _reset()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    c = claude_client.get_client()
    assert c.with_options(timeout=5.0)._client is c._client
    _reset()

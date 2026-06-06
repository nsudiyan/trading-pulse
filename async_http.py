"""
async_http.py — Единая async-HTTP утилита: пул соединений, retry, exponential backoff.

Использование:
    from async_http import http_get_json

    data = await http_get_json(
        "https://api.bybit.com/v5/market/tickers",
        params={"category": "linear"},
        max_retries=3,
    )
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

try:
    import aiohttp
except ImportError as e:
    raise RuntimeError("aiohttp не установлен. pip install aiohttp") from e

log = logging.getLogger("async_http")

# Один ClientSession на event loop — connection pooling.
# При смене loop (например, повторный asyncio.run() в long-running процессе)
# session+lock пересоздаются, иначе ловим "Event loop is closed".
_SESSION: Optional[aiohttp.ClientSession] = None
_SESSION_LOOP: Optional[asyncio.AbstractEventLoop] = None
_SESSION_LOCK: Optional[asyncio.Lock] = None

# Per-host rate limiting (token bucket)
_HOST_BUCKETS: dict[str, list] = {}   # host -> [last_call_ts, ...]
_HOST_LIMITS: dict[str, tuple] = {
    "api.bybit.com":              (120, 60),   # 120 req/60s
    "api.coingecko.com":          (30, 60),    # 30 req/60s
    "api.coinalyze.net":          (40, 60),
    "api.dexscreener.com":        (300, 60),
    "api.llama.fi":               (60, 60),
    "stablecoins.llama.fi":       (60, 60),
    "fapi.binance.com":           (1200, 60),
    "www.deribit.com":            (60, 60),
    "cryptopanic.com":            (10, 60),
}


async def _get_session() -> aiohttp.ClientSession:
    global _SESSION, _SESSION_LOOP, _SESSION_LOCK
    current_loop = asyncio.get_running_loop()

    # Если loop сменился (новый asyncio.run() в долгоживущем процессе) —
    # старая session/lock привязаны к закрытому loop, забываем их.
    if _SESSION_LOOP is not current_loop:
        _SESSION = None
        _SESSION_LOOP = current_loop
        _SESSION_LOCK = asyncio.Lock()

    async with _SESSION_LOCK:
        if _SESSION is None or _SESSION.closed:
            timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
            conn = aiohttp.TCPConnector(
                limit=100, limit_per_host=10,
                ttl_dns_cache=300, enable_cleanup_closed=True,
            )
            _SESSION = aiohttp.ClientSession(timeout=timeout, connector=conn)
        return _SESSION


async def close_session():
    global _SESSION, _SESSION_LOOP, _SESSION_LOCK
    if _SESSION and not _SESSION.closed:
        try:
            await _SESSION.close()
        except Exception:
            pass
    _SESSION = None
    _SESSION_LOOP = None
    _SESSION_LOCK = None


async def _rate_limit_wait(url: str):
    """Простой sliding-window rate limiter per host."""
    from urllib.parse import urlparse
    host = urlparse(url).netloc
    limit_info = _HOST_LIMITS.get(host)
    if not limit_info:
        return
    max_req, window_sec = limit_info
    now = time.time()
    bucket = _HOST_BUCKETS.setdefault(host, [])
    # Чистим устаревшие
    bucket[:] = [t for t in bucket if now - t < window_sec]
    if len(bucket) >= max_req:
        sleep_for = window_sec - (now - bucket[0]) + 0.05
        if sleep_for > 0:
            log.debug(f"Rate-limit wait {host}: {sleep_for:.2f}s")
            await asyncio.sleep(sleep_for)
    bucket.append(time.time())


async def http_get_json(
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    max_retries: int = 3,
    backoff_base: float = 0.5,
) -> Optional[Any]:
    """
    GET → JSON. Возвращает None при финальной ошибке (fail-soft).
    Retry с exponential backoff + jitter на 429/5xx/network errors.
    """
    await _rate_limit_wait(url)
    session = await _get_session()

    for attempt in range(max_retries + 1):
        try:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 429:
                    retry_after = float(resp.headers.get("Retry-After", "1"))
                    log.warning(f"429 {url} → wait {retry_after}s (attempt {attempt+1})")
                    await asyncio.sleep(retry_after + random.uniform(0, 0.3))
                    continue
                if resp.status >= 500:
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history, status=resp.status,
                        message=f"server {resp.status}",
                    )
                if resp.status >= 400:
                    body = await resp.text()
                    log.warning(f"{resp.status} {url}: {body[:200]}")
                    return None
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt >= max_retries:
                log.error(f"Финальная ошибка {url}: {type(e).__name__}: {e}")
                return None
            wait = backoff_base * (2 ** attempt) + random.uniform(0, 0.3)
            log.warning(f"Retry {attempt+1}/{max_retries} {url} через {wait:.1f}s: {e}")
            await asyncio.sleep(wait)
    return None


@asynccontextmanager
async def http_lifecycle():
    """async with http_lifecycle(): ... — закрывает session при выходе."""
    try:
        yield
    finally:
        await close_session()

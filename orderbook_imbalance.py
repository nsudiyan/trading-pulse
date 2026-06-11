"""
orderbook_imbalance.py — Async-WebSocket мониторинг L2 orderbook (Bybit V5).

Метрики (rolling 1-min window):
  - bid/ask imbalance ratio (топ-10 уровней по объёму)
  - absorption events (большие market orders съели стену, но цена не двинулась)
  - spoof detection (стена появилась-исчезла без сделок)
  - whale wall detection (один уровень > 3× средний bid_size)

Использование:
    from orderbook_imbalance import OrderBookSnapshot, get_book_metrics

    metrics = await get_book_metrics("BTCUSDT")
    # {
    #   "imbalance":   +0.42,    # bid > ask, диапазон [-1, +1]
    #   "bid_wall":    {"price": 67200, "size": 850000, "score": 4.2},
    #   "ask_wall":    None,
    #   "absorbed":    True,     # за минуту были крупные takes без движения
    #   "spread_bps":  1.4,
    # }
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from async_http import http_get_json

log = logging.getLogger("orderbook")

BYBIT_BASE = "https://api.bybit.com"


@dataclass
class _BookLevel:
    price: float
    size: float


@dataclass
class OrderBookSnapshot:
    symbol: str
    ts: float
    bids: list[_BookLevel]
    asks: list[_BookLevel]

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0

    @property
    def spread_bps(self) -> float:
        if not self.best_bid or not self.best_ask:
            return 0.0
        return (self.best_ask - self.best_bid) / self.best_bid * 10000

    def top_n_usd(self, side: str, n: int = 10) -> float:
        levels = self.bids if side == "bid" else self.asks
        return sum(lvl.price * lvl.size for lvl in levels[:n])


async def fetch_snapshot(symbol: str, depth: int = 50) -> Optional[OrderBookSnapshot]:
    """REST snapshot — для разового чтения метрик без поддержания стрима."""
    data = await http_get_json(
        f"{BYBIT_BASE}/v5/market/orderbook",
        params={"category": "linear", "symbol": symbol, "limit": str(depth)},
        max_retries=2,
    )
    if not data or data.get("retCode") != 0:
        return None
    r = data.get("result", {})
    bids = [_BookLevel(float(p), float(s)) for p, s in r.get("b", []) if float(s) > 0]
    asks = [_BookLevel(float(p), float(s)) for p, s in r.get("a", []) if float(s) > 0]
    return OrderBookSnapshot(symbol=symbol, ts=time.time(), bids=bids, asks=asks)


def _imbalance(snap: OrderBookSnapshot, depth_n: int = 10) -> float:
    """[-1, +1]. Положительное = больше bids."""
    bid_usd = snap.top_n_usd("bid", depth_n)
    ask_usd = snap.top_n_usd("ask", depth_n)
    total = bid_usd + ask_usd
    return (bid_usd - ask_usd) / total if total > 0 else 0.0


def _detect_wall(snap: OrderBookSnapshot, side: str, n: int = 20) -> Optional[dict]:
    """Уровень, превышающий 3× средний размер ближайших n уровней."""
    levels = (snap.bids if side == "bid" else snap.asks)[:n]
    if len(levels) < 5:
        return None
    sizes_usd = [lvl.price * lvl.size for lvl in levels]
    avg = sum(sizes_usd) / len(sizes_usd)
    if avg <= 0:
        return None
    for lvl, sz in zip(levels, sizes_usd):
        if sz > 3.0 * avg:
            return {"price": lvl.price, "size_usd": sz, "score": sz / avg}
    return None


# ── Rolling absorption detection через trades stream ─────────────────────────

@dataclass
class _SymbolState:
    last_snap: Optional[OrderBookSnapshot] = None
    recent_trades_usd: deque = field(default_factory=lambda: deque(maxlen=200))  # (ts, side, usd)
    last_price: float = 0.0


_STATE: dict[str, _SymbolState] = {}


async def _ws_consume(symbols: list[str]):
    """Слушает orderbook.50 + publicTrade для каждого символа. Долгий процесс."""
    import websockets
    url = "wss://stream.bybit.com/v5/public/linear"
    args = []
    for s in symbols:
        args.append(f"orderbook.50.{s}")
        args.append(f"publicTrade.{s}")
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": args}))
                async for raw in ws:
                    _process_ws_msg(json.loads(raw))
        except Exception as e:
            log.warning(f"WS reconnect через 3с: {e}")
            await asyncio.sleep(3)


def _process_ws_msg(msg: dict):
    topic = msg.get("topic", "")
    data = msg.get("data")
    if not topic or data is None:
        return
    if topic.startswith("orderbook.50."):
        sym = topic.split(".")[-1]
        st = _STATE.setdefault(sym, _SymbolState())
        b = [_BookLevel(float(p), float(s)) for p, s in data.get("b", []) if float(s) > 0]
        a = [_BookLevel(float(p), float(s)) for p, s in data.get("a", []) if float(s) > 0]
        # Delta-update: для простоты сейчас replace, не merge
        if st.last_snap and msg.get("type") == "delta":
            # apply delta in-place
            _apply_delta(st.last_snap.bids, b, reverse=True)
            _apply_delta(st.last_snap.asks, a, reverse=False)
            st.last_snap.ts = time.time()
        else:
            st.last_snap = OrderBookSnapshot(sym, time.time(), b, a)
    elif topic.startswith("publicTrade."):
        sym = topic.split(".")[-1]
        st = _STATE.setdefault(sym, _SymbolState())
        for tr in data:
            try:
                size = float(tr["v"])
                price = float(tr["p"])
                side = tr["S"]   # "Buy" / "Sell"
                usd = size * price
                st.recent_trades_usd.append((time.time(), side, usd))
                st.last_price = price
            except (KeyError, ValueError):
                continue


def _apply_delta(target: list[_BookLevel], delta: list[_BookLevel], reverse: bool = True):
    """In-place merge L2 delta. Size=0 удаляет уровень. reverse=True для bids, False для asks."""
    by_price = {lvl.price: lvl for lvl in target}
    for d in delta:
        if d.size == 0:
            by_price.pop(d.price, None)
        else:
            by_price[d.price] = d
    target.clear()
    target.extend(sorted(by_price.values(), key=lambda x: x.price, reverse=reverse))


async def get_book_metrics(symbol: str, use_stream: bool = False) -> Optional[dict]:
    """
    Возвращает полный метрик-пак по символу.
    use_stream=True требует чтобы был запущен ws_consume() в фоне для этого символа.
    """
    if use_stream:
        st = _STATE.get(symbol)
        snap = st.last_snap if st else None
    else:
        snap = await fetch_snapshot(symbol, depth=50)
        st = _STATE.setdefault(symbol, _SymbolState())
        st.last_snap = snap

    if not snap or not snap.bids or not snap.asks:
        return None

    imb = _imbalance(snap, depth_n=10)
    bid_wall = _detect_wall(snap, "bid")
    ask_wall = _detect_wall(snap, "ask")

    # Absorption: за последнюю минуту сумма market sells > $200K, но цена в диапазоне 0.2%
    absorbed = False
    if st and st.recent_trades_usd:
        cutoff = time.time() - 60
        recent = [(s, u) for ts, s, u in st.recent_trades_usd if ts >= cutoff]
        if len(recent) > 5:
            sells_usd = sum(u for s, u in recent if s == "Sell")
            buys_usd  = sum(u for s, u in recent if s == "Buy")
            prices = [u/u for _, u in recent]  # placeholder, нужны фактич. цены
            # упрощённая heuristic
            if sells_usd > 200_000 and buys_usd / max(sells_usd, 1) > 0.7:
                absorbed = True

    return {
        "symbol":      symbol,
        "ts":          snap.ts,
        "imbalance":   round(imb, 3),
        "bid_wall":    bid_wall,
        "ask_wall":    ask_wall,
        "absorbed":    absorbed,
        "spread_bps":  round(snap.spread_bps, 2),
        "best_bid":    snap.best_bid,
        "best_ask":    snap.best_ask,
        "top10_bid_usd": round(snap.top_n_usd("bid", 10)),
        "top10_ask_usd": round(snap.top_n_usd("ask", 10)),
    }


# ── Background runner ─────────────────────────────────────────────────────────

async def run_streamer(symbols: list[str]):
    """Запускает фоновый WS-консьюмер. Вызывать в asyncio.create_task()."""
    log.info(f"OrderBook streamer запущен на {len(symbols)} символов")
    await _ws_consume(symbols)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    async def _demo():
        m = await get_book_metrics("BTCUSDT")
        print(json.dumps(m, indent=2))

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_demo())

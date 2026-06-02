"""
sweep_watcher.py — WebSocket-triggered sweep detection daemon.

Subscribes to Bybit kline.60 (1H) for top-50 symbols.
On candle close (confirm=true), runs detect_sweep() against a rolling candle
buffer. On sweep detected, calls _fetch_and_score() in a thread pool for full
analysis and sends a Telegram alert if score >= threshold (respects cooldown).

Run:
    python3 sweep_watcher.py
    python3 sweep_watcher.py --top-n 30 --log-level DEBUG
"""

import argparse
import asyncio
import json
import logging
import sys
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

try:
    import websockets
except ImportError:
    print("[FATAL] websockets not installed. Run: pip install 'websockets>=12.0'")
    sys.exit(1)

# ── Import from screener (same directory) ────────────────────────────────────

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from screener import (
    _load_dotenv,
    detect_sweep,
    _fetch_and_score,
    _apply_cooldown,
    _record_cooldown,
    fetch_all_tickers,
    fetch_klines,
    fetch_btc_4h_change,
    load_score_weights,
    SETUP_TG_MIN_SCORE,
    BAD_SIGNAL_HOURS,
    BAD_HOUR_MIN_SCORE,
    SYMBOL_BLACKLIST,
    MIN_TURNOVER_24H,
)

_load_dotenv()

try:
    import telegram_alerts as _tg
    _TG_AVAILABLE = True
except ImportError:
    _TG_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────

WS_URL            = "wss://stream.bybit.com/v5/public/linear"
INTERVAL          = "60"   # 1H klines — same granularity screener uses for sweeps
BATCH_SZ          = 10     # topics per WS connection (Bybit recommendation)
CANDLE_BUF        = 25     # rolling window per symbol (detect_sweep needs ~12+)
TICKER_TTL        = 300    # seconds before re-fetching tickers
WEIGHTS_TTL       = 600    # seconds before reloading score weights

LOG = logging.getLogger("sweep_watcher")

# ── Shared mutable state ──────────────────────────────────────────────────────
# Candle buffers: symbol → deque of (open, high, low, close, volume)
_candle_buf: dict = defaultdict(lambda: deque(maxlen=CANDLE_BUF))

# Tickers cache
_tickers: dict   = {}
_tickers_ts: float = 0.0
_tickers_lock = threading.Lock()

# Score weights cache
_score_weights: dict = {}
_score_weights_ts: float = 0.0
_weights_lock = threading.Lock()

# Re-entry guard: symbols currently being analyzed
_in_analysis: set = set()
_analysis_lock = threading.Lock()

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sweep")


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _get_tickers() -> dict:
    global _tickers, _tickers_ts
    with _tickers_lock:
        if time.time() - _tickers_ts > TICKER_TTL:
            try:
                fresh = fetch_all_tickers()
                _tickers = fresh
                _tickers_ts = time.time()
                LOG.debug("Tickers refreshed: %d symbols", len(_tickers))
            except Exception as e:
                LOG.warning("Tickers refresh failed: %s", e)
        return dict(_tickers)


def _get_score_weights() -> dict:
    global _score_weights, _score_weights_ts
    with _weights_lock:
        if time.time() - _score_weights_ts > WEIGHTS_TTL:
            try:
                fresh = load_score_weights()
                _score_weights = fresh
                _score_weights_ts = time.time()
            except Exception as e:
                LOG.warning("Score weights reload failed: %s", e)
        return dict(_score_weights)


def _prefill_buffer(symbol: str) -> None:
    """Seed rolling buffer with recent REST candles before WS connects."""
    try:
        opens, highs, lows, closes, vols = fetch_klines(symbol, INTERVAL, CANDLE_BUF + 5)
        for candle in zip(opens, highs, lows, closes, vols):
            _candle_buf[symbol].append(candle)
        LOG.debug("[%s] buffer pre-filled: %d candles", symbol, len(_candle_buf[symbol]))
    except Exception as e:
        LOG.warning("[%s] pre-fill failed: %s", symbol, e)


# ── Alert formatting ──────────────────────────────────────────────────────────

def _fmt_price(p: float) -> str:
    if p >= 100:   return f"{p:.2f}"
    if p >= 1:     return f"{p:.4f}"
    if p >= 0.01:  return f"{p:.5f}"
    return f"{p:.8f}"


def _send_sweep_alert(result: dict, sweep_type: str) -> None:
    if not _TG_AVAILABLE:
        return
    tg_cfg = _tg.load_config()
    if not (tg_cfg.get("enabled") and tg_cfg.get("bot_token") and tg_cfg.get("chat_id")):
        return

    # Claude RT-фильтр перед отправкой sweep-алерта
    import os as _os
    _claude_extra = ""
    if _os.environ.get("CLAUDE_RT_FILTER", "on").lower() in ("on", "true", "1", "yes"):
        try:
            from claude_realtime_filter import filter_candidate as _crt
            _cand = dict(result)
            _cand["setup"]     = "range_sweep"
            _cand["direction"] = "LONG" if result.get("direction") == "long" else "SHORT"
            _cand["funding"]   = result.get("fund_%")
            _cand["price_chg_4h"] = result.get("change_24h", 0)
            _cand["signals"]   = [f"Live {sweep_type} sweep detected by WebSocket"]
            _v = _crt(result["symbol"], _cand, source="sweep_watcher")
            _act = _v.get("action")
            LOG.info(f"[RT-Filter] {result['symbol']} sweep_{sweep_type} → {_act} "
                     f"conf={_v.get('confidence',0):.2f}")
            if _act != "GO":   # fail-CLOSED: SKIP / WAIT / FAIL_OPEN — не шлём
                return
            if _v.get("reasoning"):
                _claude_extra = f"\n🧠 Claude conf={_v.get('confidence',0):.0%}: {_v['reasoning'][:280]}"
        except Exception as _e:
            LOG.warning(f"sweep RT-filter error → fail-CLOSED (не шлём): {_e}")
            return  # fail-CLOSED на исключении тоже

    token  = tg_cfg["bot_token"]
    sym    = result["symbol"]
    score  = result["score"]
    grade  = result.get("grade", "?")
    setup  = result.get("setup", "range_sweep")
    price  = result.get("price", 0)
    direct = result.get("direction", "?")
    fund   = result.get("fund_%", 0)
    oi24   = result.get("oi24h_%", 0)
    chg24  = result.get("change_24h", 0)
    notes  = result.get("notes", {}).get(setup, "")
    ts     = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    dir_icon = "🟢 ЛОНГ" if direct == "long" else "🔴 ШОРТ"
    swp_tag  = "⬇️ Sweep↓ (ложный пробой лоя)" if sweep_type == "down" else "⬆️ Sweep↑ (ложный пробой хая)"
    bull     = (direct == "long")

    # SL/TP using 1H ATR ×1.5 (wide enough to survive re-sweep per CLAUDE.md)
    atr_pct  = result.get("atr_%", 1.0) or 1.0
    atr_abs  = price * atr_pct / 100
    risk_d   = atr_abs * 1.5
    if bull:
        sl  = max(price - risk_d, 0)
        tp1 = price + risk_d
        tp2 = price + risk_d * 2
    else:
        sl  = price + risk_d
        tp1 = max(price - risk_d, 0)
        tp2 = max(price - risk_d * 2, 0)

    risk_pct = abs(price - sl) / price * 100 if price > 0 else 0
    sl_sign  = "−" if bull else "+"
    tp1_pct  = abs(tp1 - price) / price * 100 if price > 0 else 0
    tp2_pct  = abs(tp2 - price) / price * 100 if price > 0 else 0
    tp_sign  = "+" if bull else "−"

    text = (
        f"⚡ <b>LIVE SWEEP ALERT</b>  {ts}\n"
        f"{swp_tag}\n"
        f"{dir_icon}\n"
        f"\n"
        f"<b>{sym}</b>  score=<b>{score}</b>  [{grade}]\n"
        f"Цена: <code>{_fmt_price(price)}</code>  "
        f"({'+'if chg24>=0 else ''}{chg24:.1f}%)\n"
        f"Fund: <b>{fund:+.3f}%</b>  OI24h: {oi24:+.1f}%\n"
        f"\n"
        f"💵 Entry: <code>{_fmt_price(price)}</code>\n"
        f"🛑 SL: <code>{_fmt_price(sl)}</code>  ({sl_sign}{risk_pct:.2f}%)\n"
        f"🎯 TP1: <code>{_fmt_price(tp1)}</code>  ({tp_sign}{tp1_pct:.2f}% | 1:1 R)\n"
        f"🎯 TP2: <code>{_fmt_price(tp2)}</code>  ({tp_sign}{tp2_pct:.2f}% | 2:1 R)\n"
    )
    if notes:
        text += f"<i>{notes}</i>\n"
    if _claude_extra:
        text += _claude_extra

    all_targets = [str(tg_cfg["chat_id"])]
    for extra in tg_cfg.get("extra_chat_ids", []):
        cid = str(extra).strip()
        if cid and cid not in all_targets:
            all_targets.append(cid)

    for cid in all_targets:
        _tg._send(token, cid, text)


# ── Analysis worker (runs in thread pool) ─────────────────────────────────────

def _analyze_symbol(symbol: str, sweep_type: str) -> None:
    """Full analysis for one symbol after a sweep is detected."""
    try:
        LOG.info("[%s] Running full analysis (sweep %s)", symbol, sweep_type)

        tickers = _get_tickers()
        if symbol not in tickers:
            LOG.warning("[%s] symbol missing from tickers, skipping", symbol)
            return

        btc_chg_24h   = float(tickers.get("BTCUSDT", {}).get("price24hPcnt", 0)) * 100
        btc_chg_4h    = fetch_btc_4h_change()
        score_weights = _get_score_weights() or None

        result = _fetch_and_score(
            symbol, tickers, btc_chg_24h,
            btc_chg_4h=btc_chg_4h,
            score_weights=score_weights,
        )

        if result is None:
            LOG.debug("[%s] _fetch_and_score returned None", symbol)
            return

        setup = result.get("setup", "")
        score = result.get("score", 0)

        # Score threshold (quarantine-aware per SETUP_TG_MIN_SCORE)
        min_score = SETUP_TG_MIN_SCORE.get(setup, SETUP_TG_MIN_SCORE.get("range_sweep", 140))

        # Time gate
        utc_hour = datetime.utcnow().hour
        if utc_hour in BAD_SIGNAL_HOURS and score < BAD_HOUR_MIN_SCORE:
            LOG.info("[%s] Time-gated (UTC %02d, score=%d < %d)",
                     symbol, utc_hour, score, BAD_HOUR_MIN_SCORE)
            return

        if score < min_score:
            LOG.info("[%s] score=%d below threshold=%d for setup=%s",
                     symbol, score, min_score, setup)
            return

        # Cooldown check
        passed, _ = _apply_cooldown([result])
        if not passed:
            LOG.info("[%s] In cooldown, skipping", symbol)
            return

        LOG.info("[%s] ⚡ SWEEP ALERT  score=%d  setup=%s  dir=%s",
                 symbol, score, setup, result.get("direction"))
        _send_sweep_alert(result, sweep_type)
        _record_cooldown([symbol])

    except Exception:
        LOG.exception("[%s] Analysis error", symbol)
    finally:
        with _analysis_lock:
            _in_analysis.discard(symbol)


# ── WebSocket batch handler ───────────────────────────────────────────────────

async def _kline_batch(symbols: list, loop: asyncio.AbstractEventLoop) -> None:
    """Maintains one persistent WS connection for a batch of symbols."""
    topics  = [f"kline.{INTERVAL}.{s}" for s in symbols]
    backoff = 1.0

    while True:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=20,
                ping_timeout=10,
                open_timeout=15,
            ) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                LOG.info("[WS] connected — %d symbols (…%s)", len(symbols), symbols[-1])
                backoff = 1.0

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    if not isinstance(msg, dict):
                        continue

                    topic = msg.get("topic", "")
                    if not topic.startswith("kline."):
                        continue

                    data_list = msg.get("data", [])
                    if not isinstance(data_list, list):
                        data_list = [data_list]

                    for kl in data_list:
                        # Only act on fully-closed candles
                        if not kl.get("confirm"):
                            continue

                        # Extract symbol from topic: "kline.60.BTCUSDT"
                        parts = topic.split(".")
                        sym = parts[2] if len(parts) >= 3 else ""
                        if not sym:
                            continue

                        try:
                            o = float(kl["open"])
                            h = float(kl["high"])
                            l = float(kl["low"])
                            c = float(kl["close"])
                            v = float(kl.get("volume", 0))
                        except (KeyError, ValueError, TypeError):
                            continue

                        # Update rolling buffer
                        _candle_buf[sym].append((o, h, l, c, v))
                        buf = _candle_buf[sym]

                        if len(buf) < 12:
                            continue  # not enough history for detect_sweep

                        highs  = [x[1] for x in buf]
                        lows   = [x[2] for x in buf]
                        closes = [x[3] for x in buf]

                        sweep_up, sweep_down = detect_sweep(highs, lows, closes)
                        if sweep_up is None and sweep_down is None:
                            continue

                        sweep_type = "down" if sweep_down is not None else "up"

                        # Re-entry guard: don't queue multiple analyses per symbol
                        with _analysis_lock:
                            if sym in _in_analysis:
                                continue
                            _in_analysis.add(sym)

                        level = sweep_down if sweep_down is not None else sweep_up
                        LOG.info("[%s] Sweep %s detected @ %.6g — queueing analysis",
                                 sym, sweep_type, level)

                        loop.run_in_executor(
                            _executor, _analyze_symbol, sym, sweep_type
                        )

        except Exception as e:
            LOG.warning("[WS] batch error: %s — reconnecting in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main(top_n: int = 50) -> None:
    LOG.info("sweep_watcher starting — fetching tickers for top-%d symbols…", top_n)

    tickers = _get_tickers()
    if not tickers:
        LOG.error("Could not fetch tickers. Check network / Bybit API.")
        sys.exit(1)

    symbols = [
        s for s in sorted(
            tickers.keys(),
            key=lambda x: float(tickers[x].get("turnover24h", 0)),
            reverse=True,
        )
        if s not in SYMBOL_BLACKLIST
        and s.endswith("USDT")
        and float(tickers[s].get("turnover24h", 0)) >= MIN_TURNOVER_24H
    ][:top_n]

    LOG.info("Watching %d symbols. Pre-filling candle buffers via REST…", len(symbols))

    loop = asyncio.get_running_loop()

    prefill_futs = [
        loop.run_in_executor(_executor, _prefill_buffer, s)
        for s in symbols
    ]
    await asyncio.gather(*prefill_futs)
    LOG.info("Buffers ready. Starting %d WebSocket batches…",
             (len(symbols) + BATCH_SZ - 1) // BATCH_SZ)

    tasks = []
    for i in range(0, len(symbols), BATCH_SZ):
        batch = symbols[i:i + BATCH_SZ]
        tasks.append(asyncio.create_task(_kline_batch(batch, loop)))

    LOG.info("Sweep watcher running — waiting for candle closes. "
             "Ctrl+C to stop.")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Real-time sweep detection via Bybit kline WebSocket"
    )
    parser.add_argument(
        "--top-n", type=int, default=50,
        help="Number of top symbols by volume to watch (default: 50)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main(top_n=args.top_n))
    except KeyboardInterrupt:
        LOG.info("Sweep watcher stopped.")

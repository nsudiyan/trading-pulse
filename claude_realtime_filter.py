"""
claude_realtime_filter.py — реал-тайм Claude фильтр кандидатов перед отправкой в TG.

Цель: 1-2 идеальных сигнала в день вместо 10 средних. Stratega пользователя -
R:R 1:3.75+ (TP ≈ 15%, SL 3-4%). Лучше пропустить, чем ошибиться.

Использование:
    from claude_realtime_filter import filter_candidate, promote_watchlist

    verdict = filter_candidate(
        symbol="BTCUSDT",
        candidate={"setup": "squeeze", "score": 142, "direction": "LONG",
                   "price": 50000, "signals": [...], ...},
        source="screener",   # или "pump_detector"
    )
    if verdict["action"] == "GO":
        send_to_telegram(..., tp_pct=verdict["tp_pct"], sl_pct=verdict["sl_pct"],
                         reasoning=verdict["reasoning"])
    elif verdict["action"] == "WAIT":
        pass  # уже сохранено в watchlist
    else:
        pass  # SKIP — лог


Кэш: in-memory 30 мин по (symbol, setup) — не дёргаем Claude повторно.
Fail-open: при недоступности API возвращает GO с verdict='unknown'.
Watchlist: outcomes/wait_watchlist.json, проверяется promote_watchlist() каждый скан.
"""

from __future__ import annotations

import json
import os
import sys
import time
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from file_lock import atomic_json_update, atomic_json_read

# Token unlocks — best-effort, никогда не ломает фильтр (fail-open)
try:
    from token_unlocks import get_upcoming_unlock
except Exception:
    def get_upcoming_unlock(symbol: str):  # type: ignore
        return None

# Анлок ближе этого порога + крупный → veto LONG / усилить SHORT
UNLOCK_VETO_DAYS    = 3.0   # <72ч
UNLOCK_VETO_MIN_PCT = 3.0   # ≥3% supply = значимое давление предложения

BASE_DIR        = Path(__file__).parent
WATCHLIST_PATH  = BASE_DIR / "outcomes" / "wait_watchlist.json"
COST_LOG_PATH   = BASE_DIR / "outcomes" / "claude_cost.csv"
STATE_PATH      = BASE_DIR / "claude_rt_state.json"   # /pause /resume контроль
LOG_PATH        = BASE_DIR / "claude_realtime_filter.log"
VERDICT_CACHE_PATH = BASE_DIR / "outcomes" / "claude_verdict_cache.json"
SHADOW_LOG_PATH = BASE_DIR / "outcomes" / "shadow_verdicts.jsonl"   # для honest backtest

MODEL           = "claude-sonnet-4-6"
MAX_TOKENS      = 800
TIMEOUT_SEC     = 12
TEMPERATURE     = 0.0           # детерминированные verdict'ы — фильтр, не творчество
CACHE_TTL_SEC   = 1800          # 30 мин
WATCH_TTL_SEC   = 4 * 3600      # 4 часа жизни WAIT-записи
WATCH_MAX       = 20            # максимум одновременно в watchlist

# Стоимость моделей (USD за миллион токенов)
_PRICING = {
    "claude-sonnet-4-6":  {"in": 3.00,  "out": 15.00},
    "claude-opus-4-7":    {"in": 15.00, "out": 75.00},
    "claude-haiku-4-5":   {"in": 0.80,  "out": 4.00},
}

# In-memory кэш (symbol, setup) -> (ts, verdict_dict). Загружается с диска при импорте.
_VERDICT_CACHE: dict[tuple, tuple] = {}


def _cache_key_to_str(key: tuple) -> str:
    """('BTCUSDT', 'squeeze') -> 'BTCUSDT|squeeze' (JSON-совместимая строка)."""
    return f"{key[0]}|{key[1]}"


def _cache_key_from_str(s: str) -> Optional[tuple]:
    if "|" not in s:
        return None
    sym, setup = s.split("|", 1)
    return (sym, setup)


def _load_verdict_cache_from_disk():
    """Восстанавливает _VERDICT_CACHE из JSON. Записи с истёкшим TTL — отбрасываются."""
    data = atomic_json_read(VERDICT_CACHE_PATH, default={})
    if not isinstance(data, dict):
        return
    now = time.time()
    loaded = 0
    for k_str, entry in data.items():
        if not isinstance(entry, dict):
            continue
        ts = entry.get("ts", 0)
        if now - ts >= CACHE_TTL_SEC:
            continue
        key = _cache_key_from_str(k_str)
        verdict = entry.get("verdict")
        if key and isinstance(verdict, dict):
            _VERDICT_CACHE[key] = (ts, verdict)
            loaded += 1
    if loaded:
        # log создаётся ниже, поэтому print безопасный fallback
        pass


def _persist_verdict_cache_entry(key: tuple, ts: float, verdict: dict):
    """Дописывает одну запись на диск + чистит устаревшие. Атомарно."""
    new_k = _cache_key_to_str(key)
    now = time.time()

    def _mutate(data):
        if not isinstance(data, dict):
            data = {}
        # cleanup устаревших
        data = {
            k: v for k, v in data.items()
            if isinstance(v, dict) and now - v.get("ts", 0) < CACHE_TTL_SEC
        }
        data[new_k] = {"ts": ts, "verdict": verdict}
        return data

    try:
        atomic_json_update(VERDICT_CACHE_PATH, _mutate, default={})
    except Exception as e:
        # Persist — best-effort, не падаем если файл недоступен
        log.warning(f"verdict cache persist failed: {type(e).__name__}: {e}")


def _shadow_log(symbol: str, candidate: dict, source: str, verdict: dict):
    """Append-only JSONL для honest backtest. Логирует полный context + verdict."""
    try:
        SHADOW_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts":         int(time.time()),
            "symbol":     symbol,
            "setup":      candidate.get("setup") or candidate.get("stage", "?"),
            "source":     source,
            "verdict":    verdict.get("action"),
            "confidence": verdict.get("confidence"),
            "tp_pct":     verdict.get("tp_pct"),
            "sl_pct":     verdict.get("sl_pct"),
            "reasoning":  (verdict.get("reasoning") or "")[:500],
            "verdict_source": verdict.get("source"),   # claude | cache | fail_open | paused | no_key
            "candidate":  candidate,                    # полный snapshot для воспроизводимости
        }
        with open(SHADOW_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        log.warning(f"shadow log: {type(e).__name__}: {e}")


logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("claude_rt")

# Восстанавливаем verdict cache с диска (после рестарта launchd) — экономия Claude API
try:
    _load_verdict_cache_from_disk()
    if _VERDICT_CACHE:
        log.info(f"verdict cache restored: {len(_VERDICT_CACHE)} записей с диска")
except Exception as _e:
    log.warning(f"verdict cache load failed: {type(_e).__name__}: {_e}")


# ── .env loader (как в claude_analyst) ────────────────────────────────────────

def _load_dotenv():
    p = BASE_DIR / ".env"
    if not p.exists():
        return
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip(); v = v.strip()
            if v and v[0] in ('"', "'") and v[-1] == v[0]:
                v = v[1:-1]
            os.environ.setdefault(k, v)

_load_dotenv()


# ── Macro-context background prefetch ─────────────────────────────────────────
# Каждые 5 мин в фоне обновляем macro-снапшот (стейблы, опции, BTC.D, F&G).
# build_context() читает из cache без блокировки.

_MACRO_CACHE = {"lines": [], "ts": 0.0}
_MACRO_LOCK = threading.Lock()
_MACRO_THREAD_STARTED = False


def _macro_refresh_loop():
    """Background thread: fail-soft refresh каждые 5 мин."""
    import asyncio as _aio
    if sys.platform == "win32":
        _aio.set_event_loop_policy(_aio.WindowsSelectorEventLoopPolicy())
    while True:
        try:
            from macro_context import get_macro
            loop = _aio.new_event_loop()
            _aio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(get_macro())
            finally:
                loop.close()
            lines = result.get("lines", []) if isinstance(result, dict) else []
            with _MACRO_LOCK:
                _MACRO_CACHE["lines"] = lines
                _MACRO_CACHE["ts"] = time.time()
            log.info(f"Macro обновлён — {len(lines)} строк")
        except Exception as e:
            log.warning(f"Macro refresh failed: {type(e).__name__}: {e}")
        time.sleep(300)


def _ensure_macro_thread():
    """Стартует macro-поток лениво на первом вызове filter_candidate."""
    global _MACRO_THREAD_STARTED
    if _MACRO_THREAD_STARTED:
        return
    _MACRO_THREAD_STARTED = True
    t = threading.Thread(target=_macro_refresh_loop, daemon=True, name="macro-refresh")
    t.start()


def _get_macro_lines() -> list:
    """Безопасное чтение macro-строк (никогда не блокирует)."""
    with _MACRO_LOCK:
        return list(_MACRO_CACHE["lines"])


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — эксперт-трейдер крипто-фьючерсов на Bybit. Анализируешь КОНКРЕТНЫЙ кандидат-сигнал в реальном времени перед его отправкой в Telegram.

ЦЕЛЬ ПОЛЬЗОВАТЕЛЯ: 2-5 КАЧЕСТВЕННЫХ сигналов в день, R:R 1:3+ (целевой TP ≈ 5-15%, SL 2-4%).
БАЛАНС: блокируй явно слабые/противоречивые сетапы, но НЕ отвергай средние с разумным confluence — фильтр должен быть selective, а не запретительным.

⚠ ВАЖНО ПРО SCORE:
Score — ШУМНЫЙ, СЛАБЫЙ сигнал, НЕ предиктор магнитуды. Высокий score часто = много слабых факторов сложились (ловушка confluence), а не сила сетапа. НЕ принимай решение по голому числу score. Доверяй СТРУКТУРЕ (sweep, OB, FVG, CVD-дивергенция) и честной рантайм-статистике WR из контекста ниже, а не порогам score.

ВАЖНО: Когда сомневаешься между SKIP и WAIT — выбирай WAIT с конкретным trigger'ом. SKIP только если сетап явно фундаментально провален. WAIT позволяет переоценить через несколько минут когда придёт триггер.

⚠ ВАЖНО ПО ИСТОРИЧЕСКОЙ СТАТИСТИКЕ:
Любые цифры WR по дню недели или часу — ВСПОМОГАТЕЛЬНЫЕ. Они часто включают данные ДО недавних улучшений (Claude фильтр, новые паттерны, исправленная доставка) и не репрезентативны для текущего пайплайна.
- НЕ используй день недели или час как ЕДИНСТВЕННУЮ причину для SKIP.
- Сильная техническая структура (sweep + OI + CVD + confluence) ПЕРЕВЕШИВАЕТ слабую day-of-week статистику.
- WR < 30% по дню = -0.10 к confidence, не veto.
- Vet'и только если: late entry, противоречивые сигналы, плохой макрорежим (BTC рушится −3%+ за 4ч), structural break, нет confluence, или score < 50 без качественных подтверждений.

Решение GO/SKIP/WAIT:
- GO: сетап имеет confluence (минимум 2-3 подтверждающих сигнала), структура согласована хотя бы на одном TF, нет явных фундаментальных противоречий, catalyst для +5-15% реалистичен. Confidence ≥ 0.60.
- SKIP: явно провальный сетап — фундаментальные противоречия (например, шорт в сильно перегруженном шорт-рынке), очевидный late entry (цена уже улетела на >+8% за 4ч до входа), structural break направления, нет ни одного confluence-фактора.
- WAIT: потенциал есть, но нужно подтверждение или таймин лучше. Укажи конкретный trigger (например: "цена закроется выше X на 1H" / "funding опустится ниже -0.04%" / "Coinalyze short_liq > $500K в 30 мин"). DEFAULT при неуверенности.

TP/SL: рассчитай ПОД ЭТУ КОНКРЕТНУЮ СТРУКТУРУ от entry. TP — ближайший структурный уровень / weekly high / ATR×3-5. SL — за структурный low/high / ATR×1.5. R:R должно быть ≥ 3.

📊 MACRO-СЕКЦИЯ (если присутствует):
- Стейблы 24h > +$200M = buying power входит → LONG-сетапы получают +bonus
- Стейблы 24h < -$200M = risk_off → SHORT-сетапы получают +bonus, LONG-сетапы более скептичны
- Options skew BTC > +5 = хедж в путах (страх) → contrarian bullish для альтов
- Options skew BTC < -3 = froth в коллах → contrarian bearish, готовься к коррекции
- F&G < 25 (Extreme Fear) при бычьих setup'ах = повышенная вероятность отскока
- F&G > 75 (Extreme Greed) при бычьих setup'ах = пик, осторожно
- BTC.D растёт → альты слабее, требуй большего confluence для альт-LONG
- BTC.D падает → альт-сезон, можно либеральнее

📖 ORDERBOOK-СЕКЦИЯ (L2 snapshot, если присутствует):
- Imbalance > +0.3 + bid_wall = bids доминируют → попутный ветер для LONG-сетапа
- Imbalance < -0.3 + ask_wall = asks доминируют → попутный для SHORT-сетапа
- Ask wall в LONG-сетапе = сопротивление сверху → ниже confidence, опасно для TP
- Bid wall в SHORT-сетапе = поддержка снизу → ниже confidence, рагу труднее
- Spread > 5 bps = низкая ликвидность → выше slippage, ужесточи требования

🎯 POSITION SKEW (top-traders vs retail, 1h):
- retail_LONG_top_SHORT в pump-сетапе = КЛАССИЧЕСКАЯ ЛОВУШКА (киты сливают на рознице) → strong SKIP/WAIT
- retail_SHORT_top_LONG в pump-сетапе = аккумуляция китами → +confidence для LONG
- divergence >20pp = крупный сигнал, заслуживает учёта
- align (delta ±10pp) = нейтрально

📈 BASIS (spot vs perp):
- Basis < -0.3% (perp дисконт) в SHORT_SQUEEZE = РЕАЛЬНЫЙ шорт-спрос → +confidence (squeeze ближе)
- Basis < -0.5% = сильный дисконт = высокая вероятность squeeze
- Basis > +0.5% в любом LONG-сетапе = розница перегружена в перпе → опасно, риск коррекции
- Basis ~0 = нейтрально

🎯 ИСТОРИЧЕСКИЙ WR ПО SETUP:
Опирайся на строку рантайм-WR в контексте ниже (честная метрика hit_tp1, текущий пайплайн) — НЕ на запомненные проценты по типам сетапов. Прошлые «эджи» по сетапам (squeeze/breakout/rug и т.д.) были регайм-зависимы (в основном один майский режим) и НЕ пережили out-of-sample проверку — не считай ни один сетап «сильным» или «мусором» априори. Калибруй confidence по СТРУКТУРЕ + confluence + рантайм-WR, а не по ярлыку сетапа.

Возвращай СТРОГО валидный JSON, без преамбулы и без markdown:
{
  "verdict": "GO" | "SKIP" | "WAIT",
  "confidence": <0.0-1.0>,
  "tp_pct": <число 3.0-25.0>,
  "sl_pct": <число 1.5-5.0>,
  "reasoning": "<1-2 предложения — почему именно этот вердикт>",
  "risks": ["<риск 1>", "<риск 2>"],
  "wait_trigger": <строка с условием для WAIT, либо null>
}
"""


# ── Контекст для Claude ───────────────────────────────────────────────────────

def _fmt_klines_summary(opens, highs, lows, closes, vols, label: str, n: int = 12) -> str:
    """Сжатая текстовая сводка последних N свечей."""
    if not closes or len(closes) < 2:
        return f"{label}: нет данных"
    take = min(n, len(closes))
    o, h, l, c, v = opens[-take:], highs[-take:], lows[-take:], closes[-take:], vols[-take:]
    chg = (c[-1] - c[0]) / c[0] * 100 if c[0] else 0
    rng_max, rng_min = max(h), min(l)
    rng_pct = (rng_max - rng_min) / rng_min * 100 if rng_min else 0
    avg_vol = sum(v) / len(v) if v else 0
    last_vol_ratio = v[-1] / avg_vol if avg_vol > 1e-8 else 0
    bull_candles = sum(1 for i in range(len(c)) if c[i] > o[i])
    return (f"{label} (последние {take}): chg={chg:+.2f}%  range={rng_pct:.1f}%  "
            f"vol last/avg=×{last_vol_ratio:.2f}  bullish_candles={bull_candles}/{take}")


def _fmt_channel_mentions(symbol: str) -> str:
    """
    Сводка по упоминаниям в каналах: счётчик LONG / SHORT / NEUTRAL за последние 6-8ч.
    Explicit counter сильнее текстового списка — Claude видит confluence явно.
    """
    cache = BASE_DIR / "channel_signals_cache.json"
    if not cache.exists():
        return "Каналы: кэш отсутствует"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        results = data.get("results", {})
        sym_base = symbol.replace("USDT", "").replace("1000", "").upper()

        counts = {"LONG": 0, "SHORT": 0, "NEUTRAL": 0}
        names = {"LONG": [], "SHORT": [], "NEUTRAL": []}
        for ch_name, items in results.items():
            seen_for_ch = False
            for it in items:
                if seen_for_ch:
                    break
                it_sym = (it.get("symbol") or "").upper()
                raw = (it.get("raw") or it.get("note") or "")
                if it_sym == symbol.upper() or (sym_base and sym_base in raw.upper()):
                    direction = (it.get("direction") or "").upper()
                    bucket = direction if direction in ("LONG", "SHORT") else "NEUTRAL"
                    counts[bucket] += 1
                    names[bucket].append(ch_name)
                    seen_for_ch = True
        total = sum(counts.values())
        if total == 0:
            return "Каналы: нет упоминаний за последние 6-8ч"
        parts = []
        if counts["LONG"]:
            parts.append(f"LONG×{counts['LONG']} ({', '.join(names['LONG'][:4])})")
        if counts["SHORT"]:
            parts.append(f"SHORT×{counts['SHORT']} ({', '.join(names['SHORT'][:4])})")
        if counts["NEUTRAL"]:
            parts.append(f"NEUTRAL×{counts['NEUTRAL']}")
        confluence_tag = ""
        max_side = max(counts["LONG"], counts["SHORT"])
        if max_side >= 3:
            confluence_tag = "  ⚡ HIGH CONFLUENCE"
        elif max_side == 2:
            confluence_tag = "  · moderate confluence"
        return f"Каналы (6-8ч): {' | '.join(parts)}{confluence_tag}"
    except Exception as e:
        return f"Каналы: ошибка чтения кэша ({e})"


def _fmt_historical_wr(setup: str, hour_utc: int, weekday: str = "") -> str:
    """
    Честная статистика setup за последние 30 дней: доля сигналов, реально ДОСТИГШИХ TP1
    (hit_tp1 без hit_stop; both-hit = не-win, порядок касания недоказуем без минутных klines),
    FLAT в знаменателе. Показываем 4h и 24h.

    NB (council 2026-06-01): РАНЬШЕ считали по метке outcome_4h ('цена в плюсе на 4h-чекпоинте'
    = win), что давало ~51% и завышало WR в этом промпте → Claude аппрувил на фейк-цифре.
    hit_tp1 = реально дошло до тейка (~7% за 4h, ~29% за 24h) — честно для решения APPROVE/SKIP.
    hour_utc/weekday больше не используются (per-bucket n<20, статистический шум); сигнатура
    сохранена ради вызова в build_context.
    """
    csv_path = BASE_DIR / "outcomes" / "resolved.csv"
    if not csv_path.exists():
        return "История: resolved.csv отсутствует"
    try:
        import csv as _csv
        cutoff_ts = datetime.now(timezone.utc).timestamp() - 30 * 86400

        def _is1(v):
            return str(v).strip() in ("1", "1.0")

        def _resolved(a, b):
            return str(a).strip() != "" or str(b).strip() != ""

        cw4 = r4 = fl4 = 0
        cw24 = r24 = 0
        with open(csv_path, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                if row.get("setup") != setup:
                    continue
                try:
                    if datetime.fromisoformat(row.get("run_ts", "")).timestamp() < cutoff_ts:
                        continue
                except Exception:
                    continue
                h4, s4 = row.get("hit_tp1_4h"), row.get("hit_stop_4h")
                if _resolved(h4, s4):
                    r4 += 1
                    if _is1(h4) and not _is1(s4):
                        cw4 += 1
                    elif not _is1(h4) and not _is1(s4):
                        fl4 += 1
                h24, s24 = row.get("hit_tp1_24h"), row.get("hit_stop_24h")
                if _resolved(h24, s24):
                    r24 += 1
                    if _is1(h24) and not _is1(s24):
                        cw24 += 1
        if r4 < 20:
            return f"История 30d {setup}: n={r4} мало (ignore)"
        wr4 = cw4 / r4 * 100
        wr24 = cw24 / r24 * 100 if r24 else 0
        flpct = fl4 / r4 * 100
        return (f"История 30d {setup} (n={r4}): достиг TP1 4h={wr4:.0f}% | 24h={wr24:.0f}% "
                f"(FLAT-чоп 4h {flpct:.0f}%; честно — FLAT в знаменателе, both-hit=не-win)")
    except Exception as e:
        return f"История: ошибка ({e})"


def _safe_unlock(symbol: str) -> Optional[dict]:
    """get_upcoming_unlock с гарантией fail-open — ошибка источника не ломает фильтр."""
    try:
        return get_upcoming_unlock(symbol)
    except Exception as e:
        log.debug(f"unlock fetch failed for {symbol}: {e}")
        return None


def build_context(symbol: str, candidate: dict, source: str) -> str:
    """Готовит компактный (~3-5K tokens) контекст для Claude."""
    now = datetime.now(timezone.utc)
    hour = now.hour
    weekday = now.strftime("%A")

    lines = [
        f"=== КАНДИДАТ ({source}) ===",
        f"Символ: {symbol}",
        f"Setup: {candidate.get('setup') or candidate.get('stage', '?')}",
        f"Направление: {candidate.get('direction', '?')}",
        f"Базовый score: {candidate.get('score', 0)}  (исторически >120 показывает WR < baseline в текущем режиме)",
        f"Цена: {candidate.get('price', 0):.6g}",
        f"Время: {now.strftime('%Y-%m-%d %H:%M UTC')} ({weekday}, UTC{hour:02d})",
    ]

    # Сигналы базовые
    sigs = candidate.get("signals") or candidate.get("notes") or []
    if sigs:
        lines += ["", "=== БАЗОВЫЕ СИГНАЛЫ ==="]
        for s in sigs[:15]:
            lines.append(f"• {s}")

    # Ключевые метрики
    lines += ["", "=== МЕТРИКИ ==="]
    for key, label in [
        ("funding",      "Funding"),
        ("oi_chg_4h",    "OI 4h Bybit, %"),
        ("oi_24h_pct",   "OI 24h, %"),
        ("bnb_oi_chg",   "OI 4h Binance, %"),
        ("cvd_pct",      "CVD %"),
        ("price_chg_4h", "Price 4h, %"),
        ("rs_btc",       "RS vs BTC"),
        ("vwap_dev",     "VWAP dev, %"),
        ("rsi_1h",       "RSI 1h"),
        ("vol_surge",    "Vol surge ×"),
    ]:
        v = candidate.get(key)
        if v is not None:
            lines.append(f"  {label}: {v}")

    # Ликвидации (Coinalyze или local)
    liq_long  = candidate.get("liq_long")  or candidate.get("liq_long_usd")
    liq_short = candidate.get("liq_short") or candidate.get("liq_short_usd")
    if liq_long is not None or liq_short is not None:
        lines.append(f"  Ликвидации (USD): long=${liq_long or 0:,.0f}  short=${liq_short or 0:,.0f}")

    # OHLC summary если переданы klines
    if "klines" in candidate:
        kl = candidate["klines"]
        if "k1h" in kl:
            opens, highs, lows, closes, vols = kl["k1h"]
            lines.append("")
            lines.append("=== ТЕХАНАЛИЗ ===")
            lines.append(_fmt_klines_summary(opens, highs, lows, closes, vols, "1H", 24))
        if "k4h" in kl:
            opens, highs, lows, closes, vols = kl["k4h"]
            lines.append(_fmt_klines_summary(opens, highs, lows, closes, vols, "4H", 20))
        if "k15m" in kl:
            opens, highs, lows, closes, vols = kl["k15m"]
            lines.append(_fmt_klines_summary(opens, highs, lows, closes, vols, "15m", 12))

    # MTF grade
    mtf = candidate.get("mtf_grade") or candidate.get("mtf_summary")
    if mtf:
        lines.append(f"MTF: {mtf}")

    # Каналы
    lines.append("")
    lines.append("=== CONFLUENCE ===")
    lines.append(_fmt_channel_mentions(symbol))

    # Макро / сессия
    btc_4h = candidate.get("btc_4h")
    fng = candidate.get("fng_value")
    lines.append(f"BTC 4h: {btc_4h:+.2f}%" if btc_4h is not None else "BTC 4h: —")
    if fng is not None:
        lines.append(f"Fear&Greed: {fng} ({candidate.get('fng_label', '')})")

    if 0 <= hour <= 8:
        sess = "Asia"
    elif 9 <= hour <= 16:
        sess = "London"
    else:
        sess = "NY"
    lines.append(f"Сессия: {sess}")

    # Macro-context (фон обновляется отдельным потоком, прочитанные строки актуальны ≤5 мин)
    _macro = _get_macro_lines()
    if _macro:
        lines.append("")
        lines.append("=== MACRO ===")
        lines.extend(_macro)

    # ── L2 Orderbook (REST snapshot на момент скана) ─────────────────────────
    _book = candidate.get("book")
    if isinstance(_book, dict):
        lines.append("")
        lines.append("=== ORDERBOOK ===")
        _imb = _book.get("imbalance")
        if _imb is not None:
            lines.append(f"Imbalance топ-10: {_imb:+.2f}  (диапазон [-1,+1]; "
                         f">+0.3 = bids доминируют; <-0.3 = asks доминируют)")
        _spr = _book.get("spread_bps")
        if _spr is not None:
            lines.append(f"Spread: {_spr:.1f} bps")
        _bw = _book.get("bid_wall")
        _aw = _book.get("ask_wall")
        if _bw:
            lines.append(f"Bid wall: ${_bw.get('size_usd', 0)/1000:.0f}K на {_bw.get('price'):.6g} "
                         f"(×{_bw.get('score', 0):.1f} от среднего)")
        if _aw:
            lines.append(f"Ask wall: ${_aw.get('size_usd', 0)/1000:.0f}K на {_aw.get('price'):.6g} "
                         f"(×{_aw.get('score', 0):.1f} от среднего)")
        if not _bw and not _aw:
            lines.append("Walls: нет крупных (равномерная книга)")

    # ── Top-trader position skew (Binance Futures, 1h) ───────────────────────
    _skew = candidate.get("pos_skew")
    if isinstance(_skew, dict):
        lines.append("")
        lines.append("=== POSITION SKEW (1h) ===")
        gl = _skew.get("global_long_pct")
        tpl = _skew.get("top_pos_long_pct")
        delta = _skew.get("delta")
        sig = _skew.get("signal", "")
        if gl is not None and tpl is not None:
            lines.append(f"Все аккаунты: {gl:.0f}% long  |  Топ-20% по позиции: {tpl:.0f}% long  "
                         f"(delta {delta:+.0f}pp)")
        if sig:
            lines.append(f"Сигнал: {sig}")
        lines.append("Интерпретация: divergence >15pp между retail и top-traders = "
                     "распределение/аккумуляция; retail_LONG_top_SHORT в pump-сетапе = ЛОВУШКА")

    # ── Spot/Perp basis ──────────────────────────────────────────────────────
    _basis = candidate.get("basis")
    if isinstance(_basis, dict):
        bp = _basis.get("basis_pct")
        bsig = _basis.get("signal", "")
        if bp is not None:
            lines.append("")
            lines.append("=== BASIS (spot vs perp) ===")
            lines.append(f"Basis: {bp:+.3f}%   ({bsig})")
            lines.append("Интерпретация: <-0.3% = перп с дисконтом → реальный шорт-спрос (squeeze setup); "
                         ">+0.5% = froth розницы (риск коррекции)")

    # ── Token unlock (давление предложения) ──────────────────────────────────
    unlock = _safe_unlock(symbol)
    if unlock:
        pct = unlock.get("pct_of_supply") or 0.0
        usd = unlock.get("usd_value")
        usd_str = f", ${usd/1e6:.0f}M" if usd else ""
        lines.append("")
        lines.append("=== TOKEN UNLOCK ===")
        lines.append(f"⚠ Анлок через {unlock['days_until']}д ({unlock['date']}): "
                     f"{pct:.1f}% supply{usd_str}, тип={unlock.get('type','unlock')}")
        lines.append("Интерпретация: крупный анлок <3д = давление предложения → "
                     "риск для LONG, попутный ветер для SHORT. Учти в verdict.")

    # Историческая WR (последние 30 дней, n>=20 порог)
    setup = candidate.get("setup") or "?"
    lines.append(_fmt_historical_wr(setup, hour, weekday))

    # Final guidance
    lines += [
        "",
        "=== ЗАДАЧА ===",
        "Реши: этот сигнал реально идеален для +5-15% движения с R:R ≥ 3:1?",
        "Или это посредственный setup, которых много, но мало кто из них взлетает?",
        "Верни ТОЛЬКО валидный JSON по схеме из system prompt.",
    ]
    return "\n".join(lines)


# ── Вызов Claude ──────────────────────────────────────────────────────────────

def _log_cost(symbol: str, setup: str, model: str, usage, verdict: str):
    """Append-only лог стоимости вызова в outcomes/claude_cost.csv."""
    try:
        in_tok  = getattr(usage, "input_tokens", 0)  or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        price = _PRICING.get(model, _PRICING["claude-sonnet-4-6"])
        cost = (in_tok * price["in"] + out_tok * price["out"]) / 1_000_000
        COST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        new_file = not COST_LOG_PATH.exists()
        with open(COST_LOG_PATH, "a", encoding="utf-8") as f:
            if new_file:
                f.write("ts,symbol,setup,model,input_tokens,output_tokens,cost_usd,verdict\n")
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            f.write(f"{ts},{symbol},{setup},{model},{in_tok},{out_tok},{cost:.6f},{verdict}\n")
        return cost
    except Exception as e:
        log.warning(f"cost log error: {e}")
        return 0.0


def _maybe_chart_b64(symbol: str, candidate: dict) -> Optional[str]:
    """Генерирует matplotlib 1H-чарт через chart_analyzer и возвращает base64-PNG.
    None если vision выключен или генерация не удалась."""
    if os.environ.get("CLAUDE_RT_VISION", "off").lower() not in ("on", "true", "1", "yes"):
        return None
    try:
        import base64
        import chart_analyzer as _ca
        if not getattr(_ca, "_CHART_OK", False):
            return None
        metrics = {
            "symbol":  symbol,
            "score":   candidate.get("score", 0),
            "setup":   candidate.get("setup") or candidate.get("stage", "?"),
            "price":   candidate.get("price", 0),
            "fund_%":  candidate.get("funding", 0),
            "oi24h_%": candidate.get("oi_chg_4h", 0),
            "cvd_k%":  candidate.get("cvd_pct", 0),
            "notes":   candidate.get("stage", ""),
        }
        path = _ca.generate_chart(symbol, metrics)
        if not path or not Path(path).exists():
            return None
        return base64.b64encode(Path(path).read_bytes()).decode()
    except Exception as e:
        log.warning(f"vision chart error: {e}")
        return None


def _call_claude(context: str, symbol: str = "?", setup: str = "?",
                 chart_b64: Optional[str] = None) -> Optional[dict]:
    """Возвращает распарсенный dict или None при ошибке."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY отсутствует — fail-open")
        return None
    # Multimodal content если есть chart
    if chart_b64:
        user_content = [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/png", "data": chart_b64}},
            {"type": "text", "text": context},
        ]
    else:
        user_content = context

    try:
        from claude_client import get_client
        # Единый pool на процесс; timeout по месту (RT-фильтр = быстрый fail)
        client = get_client(api_key=api_key).with_options(timeout=TIMEOUT_SEC)
        msg = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        # F9-fix: устойчивое извлечение первого текстового блока (пустой/thinking/tool_use → fail, не краш)
        text = ""
        for _blk in (msg.content or []):
            _t = getattr(_blk, "text", None)
            if _t:
                text = _t.strip()
                break
        if not text:
            log.warning(f"{symbol}: пустой/не-текстовый ответ Claude → fail")
            return None
        # На случай если модель обернула в ```json
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
            if text.endswith("```"):
                text = text[:-3].strip()
        result = json.loads(text)

        # Валидация
        if result.get("verdict") not in ("GO", "SKIP", "WAIT"):
            log.warning(f"Невалидный verdict: {result.get('verdict')}")
            return None
        # Защита от шизы
        result["tp_pct"] = max(2.0, min(30.0, float(result.get("tp_pct") or 6.0)))
        # #8 (2026-05-30): потолок 6→10% — валидированный широкий ~8% стоп был недостижим;
        # юзеру нужны стопы, переживающие liquidity sweep (CLAUDE.md: ATR×1.8 для волатильных альтов).
        result["sl_pct"] = max(1.0, min(10.0, float(result.get("sl_pct") or 3.0)))
        result["confidence"] = max(0.0, min(1.0, float(result.get("confidence") or 0.5)))
        _log_cost(symbol, setup, MODEL, msg.usage, result.get("verdict", "?"))
        return result
    except Exception as e:
        # F3-fix: отделяем billing/auth (общий кошелёк исчерпан / ключ мёртв) от транзиентных —
        # на таких ошибках вызывающие должны fail-CLOSED, и это должно быть ГРОМКО в логе.
        _m = str(e).lower()
        _is_billing = (type(e).__name__ in ("AuthenticationError", "PermissionDeniedError")
                       or "credit" in _m or "billing" in _m or "quota" in _m
                       or "insufficient" in _m or "balance" in _m)
        if _is_billing:
            log.error(f"🔴 Claude БИЛЛИНГ/АВТОРИЗАЦИЯ ({type(e).__name__}): {e} "
                      f"— общий кошелёк? RT-фильтр fail-CLOSED, алерты не шлются")
        else:
            log.error(f"Claude API error: {type(e).__name__}: {e}")
        return None


# ── Основная функция ─────────────────────────────────────────────────────────

def is_enabled() -> bool:
    """Включён ли RT-фильтр? Проверяет state-файл (управляется /pause /resume)."""
    if not STATE_PATH.exists():
        return True
    try:
        return bool(json.loads(STATE_PATH.read_text(encoding="utf-8")).get("enabled", True))
    except Exception:
        return True


def set_enabled(enabled: bool):
    """Сохраняет состояние pause/resume в файл атомарно."""
    atomic_json_update(
        STATE_PATH,
        lambda _: {"enabled": bool(enabled),
                   "ts": datetime.now(timezone.utc).isoformat()},
        default={},
    )


def filter_candidate(symbol: str, candidate: dict, source: str = "screener") -> dict:
    """
    Главная точка входа. Возвращает dict с полями:
        action: "GO" | "SKIP" | "WAIT" | "FAIL_OPEN"
        verdict: то же из Claude (для логов)
        confidence: 0.0-1.0
        tp_pct, sl_pct: float
        reasoning: str
        risks: list[str]
        wait_trigger: Optional[str]
        source: "claude" | "cache" | "fail_open" | "no_key"
    """
    _ensure_macro_thread()
    key = (symbol.upper(), candidate.get("setup") or candidate.get("stage", "?"))
    now = time.time()

    # Pre-filter источников: sector_heat и channel_reader доказали 0% GO в shadow log
    # (n=9 за период, все SKIP). Не тратим Claude tokens и не засоряем shadow для них.
    # Эти источники остаются в системе для аналитики, но не идут в TG-алерты.
    if source in ("sector_heat", "channel_reader"):
        return {
            "action":      "SKIP",
            "verdict":     "SKIP",
            "confidence":  0.0,
            "tp_pct":      0.0,
            "sl_pct":      0.0,
            "reasoning":   f"Pre-filter: source={source} имеет 0% GO исторически, не пропускаем",
            "risks":       [],
            "wait_trigger": None,
            "source":      "pre_filter",
        }

    # Pre-filter слабых setups (WR хуже coin flip на 2929 сигналах screener):
    # breakout 40.9%, swing 33.9%, range_sweep 42.9% — отключаем без вызова Claude.
    # Если в будущем эти setups улучшатся (новые паттерны) — снять блок.
    _setup_name = (candidate.get("setup") or "").lower()
    WEAK_SETUPS = {"breakout", "swing", "range_sweep"}
    if _setup_name in WEAK_SETUPS:
        return {
            "action":      "SKIP",
            "verdict":     "SKIP",
            "confidence":  0.0,
            "tp_pct":      0.0,
            "sl_pct":      0.0,
            "reasoning":   f"Pre-filter: setup={_setup_name} имеет WR<45% исторически",
            "risks":       [],
            "wait_trigger": None,
            "source":      "pre_filter",
        }

    # Pause/Resume через state-файл (управляется /pause командой)
    if not is_enabled():
        return {
            "action":      "FAIL_OPEN",
            "verdict":     "paused",
            "confidence":  0.0,
            "tp_pct":      6.0,
            "sl_pct":      3.0,
            "reasoning":   "RT-фильтр на паузе (/resume чтобы включить)",
            "risks":       [],
            "wait_trigger": None,
            "source":      "paused",
        }

    # Кэш
    cached = _VERDICT_CACHE.get(key)
    if cached and now - cached[0] < CACHE_TTL_SEC:
        v = dict(cached[1]); v["source"] = "cache"
        return v

    ctx = build_context(symbol, candidate, source)
    chart = _maybe_chart_b64(symbol, candidate)
    result = _call_claude(ctx, symbol=symbol, setup=key[1], chart_b64=chart)

    if result is None:
        # Fail-open: пропускаем сигнал через старый gate
        out = {
            "action":      "FAIL_OPEN",
            "verdict":     "unknown",
            "confidence":  0.0,
            "tp_pct":      6.0,
            "sl_pct":      3.0,
            "reasoning":   "Claude API недоступен — пропуск через старый score-gate",
            "risks":       [],
            "wait_trigger": None,
            "source":      "fail_open" if os.environ.get("ANTHROPIC_API_KEY") else "no_key",
        }
        # F2-fix: громко (WARNING) + счётчик — fail-open = фильтр отключён, вызывающие fail-CLOSED.
        filter_candidate._fail_open_n = getattr(filter_candidate, "_fail_open_n", 0) + 1
        log.warning(f"{symbol} {key[1]} → FAIL_OPEN (фильтр недоступен; "
                    f"вызывающие НЕ шлют; всего за процесс: {filter_candidate._fail_open_n})")
        _shadow_log(symbol, candidate, source, out)
        return out

    action = result["verdict"]
    out = {
        "action":      action,
        "verdict":     action,
        "confidence":  result["confidence"],
        "tp_pct":      result["tp_pct"],
        "sl_pct":      result["sl_pct"],
        "reasoning":   result.get("reasoning", ""),
        "risks":       result.get("risks", []) or [],
        "wait_trigger": result.get("wait_trigger"),
        "source":      "claude",
    }

    # Hard-veto: крупный анлок <72ч режет LONG-вход (давление предложения → дамп-риск).
    # Claude уже видит анлок в контексте, но это страховочный гейт на случай GO.
    direction = (candidate.get("direction") or "").upper()
    if out["action"] == "GO" and direction == "LONG":
        unlock = _safe_unlock(symbol)
        if (unlock and unlock["days_until"] <= UNLOCK_VETO_DAYS
                and (unlock.get("pct_of_supply") or 0.0) >= UNLOCK_VETO_MIN_PCT):
            log.info(f"{symbol} {key[1]} → UNLOCK VETO: GO→SKIP "
                     f"(анлок {unlock['pct_of_supply']:.1f}% через {unlock['days_until']}д)")
            out["action"] = "SKIP"
            out["verdict"] = "SKIP"
            out["confidence"] = 0.0
            out["risks"] = (out["risks"] or []) + [
                f"unlock {unlock['pct_of_supply']:.1f}% supply через {unlock['days_until']}д"
            ]
            out["reasoning"] = (
                f"[UNLOCK VETO] {unlock['pct_of_supply']:.1f}% supply анлок через "
                f"{unlock['days_until']}д ({unlock['date']}) — давление предложения режет LONG. "
                + out["reasoning"]
            )

    _VERDICT_CACHE[key] = (now, out)
    _persist_verdict_cache_entry(key, now, out)
    log.info(f"{symbol} {key[1]} → {out['action']} conf={out['confidence']:.2f} tp={out['tp_pct']:.1f}% "
             f"sl={out['sl_pct']:.1f}%  {out['reasoning'][:120]}")

    if out["action"] == "WAIT":
        _add_to_watchlist(symbol, candidate, source, out)

    _shadow_log(symbol, candidate, source, out)
    return out


# ── Watchlist ────────────────────────────────────────────────────────────────

def _load_watchlist() -> list[dict]:
    data = atomic_json_read(WATCHLIST_PATH, default=[])
    return data if isinstance(data, list) else []


def _save_watchlist(items: list[dict]):
    atomic_json_update(WATCHLIST_PATH, lambda _: items, default=[])


def _add_to_watchlist(symbol: str, candidate: dict, source: str, verdict: dict):
    new_item = {
        "ts":           int(time.time()),
        "symbol":       symbol,
        "source":       source,
        "setup":        candidate.get("setup") or candidate.get("stage", "?"),
        "price":        candidate.get("price"),
        "score":        candidate.get("score"),
        "trigger":      verdict.get("wait_trigger") or "",
        "reasoning":    verdict.get("reasoning", ""),
        "confidence":   verdict.get("confidence", 0.0),
        "tp_pct":       verdict.get("tp_pct"),
        "sl_pct":       verdict.get("sl_pct"),
        "snapshot":     {
            "funding":      candidate.get("funding"),
            "oi_chg_4h":    candidate.get("oi_chg_4h"),
            "cvd_pct":      candidate.get("cvd_pct"),
            "btc_4h":       candidate.get("btc_4h"),
        },
    }

    def _mutate(items):
        if not isinstance(items, list):
            items = []
        now = int(time.time())
        items = [
            it for it in items
            if now - it.get("ts", 0) < WATCH_TTL_SEC and it.get("symbol") != symbol
        ]
        items.append(new_item)
        return items[-WATCH_MAX:]

    final = atomic_json_update(WATCHLIST_PATH, _mutate, default=[])
    log.info(f"Watchlist + {symbol} ({len(final)} активных)")


def list_watchlist() -> list[dict]:
    """Возвращает все живые WAIT-записи (TTL не истёк)."""
    now = int(time.time())
    items = _load_watchlist()
    return [it for it in items if now - it.get("ts", 0) < WATCH_TTL_SEC]


def summarize_costs(hours: int = 24) -> dict:
    """Возвращает сводку по claude_cost.csv за последние N часов."""
    if not COST_LOG_PATH.exists():
        return {"calls": 0, "cost_usd": 0.0, "by_verdict": {}, "in_tok": 0, "out_tok": 0}
    import csv as _csv
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    calls = 0; cost = 0.0; in_tok = 0; out_tok = 0
    by_verdict: dict = {}
    by_setup: dict = {}
    try:
        with open(COST_LOG_PATH, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                try:
                    ts = datetime.fromisoformat(row["ts"]).timestamp()
                except Exception:
                    continue
                if ts < cutoff:
                    continue
                calls   += 1
                cost    += float(row.get("cost_usd", 0) or 0)
                in_tok  += int(row.get("input_tokens",  0) or 0)
                out_tok += int(row.get("output_tokens", 0) or 0)
                v = row.get("verdict", "?")
                by_verdict[v] = by_verdict.get(v, 0) + 1
                s = row.get("setup", "?")
                by_setup[s] = by_setup.get(s, 0) + 1
    except Exception:
        pass
    return {
        "hours":      hours,
        "calls":      calls,
        "cost_usd":   round(cost, 4),
        "in_tok":     in_tok,
        "out_tok":    out_tok,
        "by_verdict": by_verdict,
        "by_setup":   by_setup,
    }


def promote_watchlist(check_fn) -> list[dict]:
    """
    Перепроверяет все WAIT-записи. check_fn(item) должен вернуть:
        (resolved: bool, fresh_candidate: dict | None)
    Если resolved=True И fresh_candidate не None — повторно прогоняет через filter_candidate;
    при verdict GO возвращает в списке для отправки в TG.

    Возвращает список словарей со списком новых GO-сигналов:
        [{"symbol": ..., "verdict": {...}, "candidate": {...}}, ...]
    """
    now = int(time.time())
    items = _load_watchlist()
    snapshot_keys = {(it.get("ts"), it.get("symbol")) for it in items}
    survivors = []
    promoted = []
    for it in items:
        if now - it.get("ts", 0) >= WATCH_TTL_SEC:
            log.info(f"Watchlist истёк {it.get('symbol')}")
            continue
        try:
            resolved, fresh = check_fn(it)
        except Exception as e:
            log.error(f"check_fn error for {it.get('symbol')}: {e}")
            survivors.append(it)
            continue
        if not resolved or not fresh:
            survivors.append(it)
            continue
        # Trigger сработал — перепроверяем через Claude
        v = filter_candidate(it["symbol"], fresh, source=it.get("source", "watchlist"))
        if v["action"] == "GO":
            promoted.append({"symbol": it["symbol"], "verdict": v, "candidate": fresh})
            # Не возвращаем в watchlist (уже GO)
            log.info(f"Watchlist PROMOTE {it['symbol']} → GO")
        elif v["action"] == "WAIT":
            # Остался в watchlist через _add_to_watchlist внутри filter_candidate
            pass
        else:
            log.info(f"Watchlist {it['symbol']} → SKIP после trigger")

    # Merge: удаляем из watchlist обработанные snapshot-записи,
    # сохраняем survivors + любые items добавленные параллельным процессом.
    def _merge(current_data):
        if not isinstance(current_data, list):
            return survivors
        out = []
        for item in current_data:
            key = (item.get("ts"), item.get("symbol"))
            if key in snapshot_keys:
                continue  # обработали — выкидываем (если survivor, добавим ниже)
            out.append(item)
        out.extend(survivors)
        return out[-WATCH_MAX:]

    atomic_json_update(WATCHLIST_PATH, _merge, default=[])
    return promoted


# ── CLI для теста ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Локальный сухой тест без сети — подаём фейковый кандидат
    test_candidate = {
        "setup":         "squeeze",
        "direction":     "LONG",
        "score":         142,
        "price":         0.5234,
        "funding":       -0.0421,
        "oi_chg_4h":     7.2,
        "bnb_oi_chg":    4.1,
        "cvd_pct":       28.5,
        "price_chg_4h":  -1.2,
        "rs_btc":        1.8,
        "vwap_dev":      -2.1,
        "rsi_1h":        38,
        "vol_surge":     1.6,
        "liq_short":     180000,
        "liq_long":      12000,
        "btc_4h":        -0.4,
        "fng_value":     28,
        "fng_label":     "Fear",
        "signals": [
            "Фандинг -0.042% — шорты перегружены",
            "OI +7.2% за 4ч — пружина сжата",
            "Sweep ниже поддержки + возврат",
            "SHORT ликвидации $180K за 5 мин — сквиз идёт",
            "Binance OI +4.1% — кросс-биржевое подтверждение",
        ],
    }
    print("=== Контекст для Claude ===\n")
    print(build_context("BTCUSDT", test_candidate, "test"))
    print("\n=== Вызов Claude ===")
    v = filter_candidate("BTCUSDT", test_candidate, source="test")
    print(json.dumps(v, ensure_ascii=False, indent=2))

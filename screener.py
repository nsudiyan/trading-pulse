"""
Crypto Futures Screener — Bybit V5 Linear Perpetuals
Источник: Bybit Public API (без ключей)

Метрики:
  - Funding Rate + Basis (mark-index спред)
  - Funding History: тренд funding за последние 8 периодов (~2.7 дня)
  - Open Interest: текущий (тикер) vs 24h назад (история)
  - OI Divergence: отношение движения OI к движению цены за 5 свечей
  - Позиция цены в 48h диапазоне (завершённые свечи)
  - Объём: завершённая свеча vs медиана 20 предыдущих
  - Long/Short Ratio топ-трейдеров Bybit
  - CVD kline-based (20h окно, одинаковый таймфрейм) + CVD trade-based
  - HTF тренд: Daily (D) + 4H — swing structure → fallback SMA20
  - Multi-Timeframe Confluence (MTF): перекрытие 4H и 1H FVG/OB зон
  - FVG (Fair Value Gap): 1H и 4H, фильтр micro-gaps, флаг "В зоне"
  - Order Block: 1H и 4H, флаг "В зоне", проверка расстояния
  - Свечные паттерны: engulfing, hammer, shooting star, inside bar (1H)
  - ATR 14: волатильность в % от цены (для оценки риска)
  - POC (Point of Control): аппроксимация уровня наибольшего объёма за 48h
  - Relative Strength vs BTC: опережает ли альт биткоин за 24h
  - Sweep: ложный пробой на завершённой свече
  - Ликвидационные события: OI drop + медиана объёма + движение цены
  - DOM: ближайшие 20 уровней, 90-й перцентиль для стен
  - Market Snapshot: сводка по рыночному режиму и направлению кандидатов
  - Watchlist: отдельные листы long/short с ATR-планом сделки
  - Export: сохранение результатов в JSON / CSV снапшоты

Сетапы:
  1. Ликвидационный сквиз
  2. BOS + FVG / Order Block
  3. Рейндж Sweep
"""

import argparse
import csv
import json
import os
import socket
import sqlite3
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from tabulate import tabulate

LIQ_DB_PATH = Path(__file__).parent / "liquidations.db"
OUTCOMES_CSV = Path(__file__).parent / "outcomes" / "resolved.csv"
COOLDOWN_PATH = Path(__file__).parent / "cooldown_cache.json"

# ── Timestamp cache: side-effect of fetch_klines, (symbol, interval) → open_ts_sec ──
_kl_open_ts: dict = {}

# ── Quality & time filter constants ──────────────────────────────────────────
COOLDOWN_HOURS   = 8          # минимум часов между сигналами по одной паре
MIN_TURNOVER_24H = 50_000_000  # минимальный оборот $50M/сутки
MAX_MOVE_24H_ABS = 0.50        # исключить пары с |move| > 50% за 24h (памп/дамп)

# Символы с системно низким WR (анализ 2151 сделок, 2026-04-23):
# WETUSDT=0%, LABUSDT=14%, TONUSDT=15%, ASTERUSDT=18%, ARIAUSDT=22%
SYMBOL_BLACKLIST = {"WETUSDT", "LABUSDT", "TONUSDT", "ASTERUSDT", "ARIAUSDT"}

# Часы UTC с хорошим историческим WR (> 50%): 05,09,10,20 — лучшие окна
GOOD_SIGNAL_HOURS = {1, 5, 9, 10, 20}   # 2510-trade audit: 55-64% WR (AVEVA-55)
# Часы UTC с плохим WR (38-41%) — поднимаем порог score для TG
BAD_SIGNAL_HOURS  = {12, 14, 23, 0}     # 12=40.6%, 14=38.6%
# Часы UTC с катастрофическим WR (< 37%) — полный хард-блок TG + pending
# 17=36.5%, 18=28.6%, 19=21.4%, 22=32.8%
HARD_BLOCK_HOURS  = {17, 18, 19, 22}
# В плохие часы сигнал идёт в TG только если score >= BAD_HOUR_MIN_SCORE
BAD_HOUR_MIN_SCORE = 165                # было 130; данные: 663 сигнала WR=34.5%
# FIX 8: Saturday WR=24.5% vs Thursday WR=64.0% — поднимаем порог на 50%
SATURDAY_MIN_SCORE = 195  # round(BAD_HOUR_MIN_SCORE * 1.5)
# FINDING 7: Friday/Tuesday also show lower WR — moderate threshold increases (n=26/n=small, not hard block)
FRIDAY_MIN_SCORE   = 169  # round(BAD_HOUR_MIN_SCORE * 1.3)
TUESDAY_MIN_SCORE  = 150  # round(BAD_HOUR_MIN_SCORE * 1.15)

# Per-setup Telegram score gates — WR audit 2026-04-24, N=2232 resolved trades.
# Min score: signals below this are suppressed.
SETUP_TG_MIN_SCORE = {
    "squeeze":     80,
    "bos_fvg":     85,
    "breakout":    9999,  # dead-zone logic in _passes_setup_tg_filter(); 9999 = fallback block
    "range_sweep": 9999,  # disabled — real-time detection via sweep_watcher.py (WR=25%)
    "short_dist":  85,
}

# Per-setup Telegram score ceiling: signals AT OR ABOVE this threshold are suppressed.
SETUP_TG_MAX_SCORE = {
    "short_dist": 150,  # >150 WR collapses to 23.5% at 4h (inverted correlation)
}

# Obsidian integration (опционально — не ломает скринер если модуль не найден)
try:
    import obsidian_bridge as _obs
    _OBS_AVAILABLE = True
except ImportError:
    _OBS_AVAILABLE = False

# Outcome tracker (опционально)
try:
    import outcome_tracker as _ot
    _OT_AVAILABLE = True
except ImportError:
    _OT_AVAILABLE = False

# Telegram alerts (опционально)
try:
    import telegram_alerts as _tg
    _TG_AVAILABLE = True
except ImportError:
    _TG_AVAILABLE = False

# Binance cross-exchange bridge (опционально)
try:
    import binance_bridge as _bnb
    _BNB_AVAILABLE = True
except ImportError:
    _BNB_AVAILABLE = False

# Channel reader (опционально)
try:
    import channel_reader as _ch
    _CH_AVAILABLE = True
except ImportError:
    _CH_AVAILABLE = False

# Free external data sources: Farside ETF, ForexFactory macro, Deribit options
try:
    import free_data as _fd
    _FD_AVAILABLE = True
except ImportError:
    _FD_AVAILABLE = False

try:
    import trade_learnings_db as _tldb
    _TLDB_PROHIBITED  = _tldb.get_prohibited_conditions()
    _TLDB_RULES       = _tldb.get_active_correction_rules()
    _TLDB_FILTERS     = _tldb.get_confirmation_filters()
    _TLDB_AVAILABLE   = True
except Exception:
    _TLDB_PROHIBITED  = []
    _TLDB_RULES       = []
    _TLDB_FILTERS     = []
    _TLDB_AVAILABLE   = False

# Streak monitor / Audit Mode (AVEVA-50)
try:
    import streak_monitor as _streak
    _STREAK_AVAILABLE = True
except ImportError:
    _STREAK_AVAILABLE = False

BASE = "https://api.bybit.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "BybitFuturesScreener/1.1"})


# ─── .env loader ─────────────────────────────────────────────────────────────

def _load_dotenv():
    """
    Загружает .env из папки проекта в os.environ (только если переменная ещё не задана).
    LaunchAgent не наследует shell-окружение → secrets из .env грузим здесь.
    """
    dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(dotenv_path):
        return
    with open(dotenv_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if val and val[0] in ('"', "'") and val[-1] == val[0]:
                val = val[1:-1]
            os.environ.setdefault(key, val)


_load_dotenv()


# ─── Network utils ───────────────────────────────────────────────────────────

def check_connectivity(host="api.bybit.com", port=443, timeout=5) -> bool:
    """Быстрая проверка доступности сети через TCP-соединение."""
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def wait_for_network(max_wait=600, interval=30) -> bool:
    """
    Ждёт доступности сети до max_wait секунд, проверяя каждые interval сек.
    Возвращает True если сеть поднялась, False если истёк таймаут.
    """
    if check_connectivity():
        return True
    print(f"[network] Сеть недоступна. Ожидаю до {max_wait//60} мин (каждые {interval}s)...")
    elapsed = 0
    while elapsed < max_wait:
        time.sleep(interval)
        elapsed += interval
        if check_connectivity():
            print(f"[network] Сеть восстановлена (ожидал {elapsed}s)")
            return True
        print(f"[network] Ещё нет сети ({elapsed}s / {max_wait}s)...")
    print(f"[network] ОШИБКА: сеть недоступна после {max_wait}s — завершаю.")
    return False

SETUP_LABELS = {
    "squeeze":     "СЕТАП 1 — Ликвидационный сквиз",
    "bos_fvg":     "СЕТАП 2 — BOS + FVG / Order Block",
    "range_sweep": "СЕТАП 3 — Рейндж Sweep",
    "breakout":    "СЕТАП 4 — Breakout / Pre-Pump",
    "short_dist":  "СЕТАП 5 — Дистрибуция / Шорт-давление",
}

SETUP_SHORT = {
    "squeeze":     "SQZ",
    "bos_fvg":     "BOS/FVG",
    "range_sweep": "SWEEP",
    "breakout":    "PUMP",
    "short_dist":  "DIST",
}


# ─── HTTP ────────────────────────────────────────────────────────────────────

def get(url, params=None, retries=3, timeout=10):
    """
    Обёртка над Bybit API с коротким retry/backoff.
    Снижает число ложных пропусков из-за сетевых всплесков и rate-limit пауз.
    """
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            if data.get("retCode", 0) != 0:
                raise ValueError(f"Bybit: {data.get('retMsg')}")
            return data["result"]
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt == retries:
                break
            time.sleep(0.5 * attempt)
    raise last_error


# ─── Data Fetchers ───────────────────────────────────────────────────────────

def fetch_all_tickers():
    """1 запрос → все USDT linear perpetuals с Bybit."""
    result = get(f"{BASE}/v5/market/tickers", {"category": "linear"})
    return {
        t["symbol"]: t
        for t in result["list"]
        if t["symbol"].endswith("USDT") and float(t.get("turnover24h", 0)) > 0
    }


def fetch_klines(symbol, interval="60", limit=102):
    """
    OHLCV. interval: '60'=1H, '240'=4H, 'D'=Daily.
    Bybit → newest first, разворачиваем.
    limit=102: запас для [-1]=текущая незакрытая, [-2]=последняя завершённая.
    """
    result = get(f"{BASE}/v5/market/kline", {
        "category": "linear",
        "symbol":   symbol,
        "interval": interval,
        "limit":    limit,
    })
    candles = list(reversed(result["list"]))
    if len(candles) >= 2:
        _kl_open_ts[(symbol, interval)] = float(candles[-2][0]) / 1000
    opens   = [float(c[1]) for c in candles]
    highs   = [float(c[2]) for c in candles]
    lows    = [float(c[3]) for c in candles]
    closes  = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    return opens, highs, lows, closes, volumes


def fetch_oi_history(symbol, limit=50):
    """История OI (1h). Bybit → newest first, разворачиваем."""
    result = get(f"{BASE}/v5/market/open-interest", {
        "category":     "linear",
        "symbol":       symbol,
        "intervalTime": "1h",
        "limit":        limit,
    })
    items = list(reversed(result["list"]))
    return [float(d["openInterest"]) for d in items]


def fetch_funding_history(symbol, limit=8):
    """
    История Funding Rate — последние N периодов.
    Каждый период = 8h для большинства пар → 8 записей ≈ 2.7 дня.
    Bybit → newest first, разворачиваем (oldest → newest).
    Нужен для анализа ТРЕНДА funding, а не только текущего значения.
    """
    try:
        result = get(f"{BASE}/v5/market/funding/history", {
            "category": "linear",
            "symbol":   symbol,
            "limit":    limit,
        })
        return [float(item["fundingRate"]) * 100 for item in reversed(result["list"])]
    except Exception:
        return []


def fetch_ls_ratio(symbol):
    """Long/Short ratio топ-трейдеров Bybit (1h)."""
    try:
        result = get(f"{BASE}/v5/market/account-ratio", {
            "category": "linear",
            "symbol":   symbol,
            "period":   "1h",
            "limit":    1,
        })
        if result["list"]:
            buy  = float(result["list"][0]["buyRatio"])
            sell = float(result["list"][0]["sellRatio"])
            return buy / sell if sell > 0 else None
    except Exception:
        return None


def fetch_recent_trades(symbol, limit=500):
    """
    Последние сделки → real CVD.
    side='Buy'  = тейкер покупает (бычья агрессия)
    side='Sell' = тейкер продаёт (медвежья агрессия)
    """
    try:
        result = get(f"{BASE}/v5/market/recent-trade", {
            "category": "linear",
            "symbol":   symbol,
            "limit":    limit,
        })
        return [(t["side"], float(t["size"])) for t in result["list"]]
    except Exception:
        return []


def fetch_orderbook(symbol, limit=50):
    """Снимок стакана. bids/asks = [(price, size), ...]"""
    try:
        result = get(f"{BASE}/v5/market/orderbook", {
            "category": "linear",
            "symbol":   symbol,
            "limit":    limit,
        })
        bids = [(float(b[0]), float(b[1])) for b in result["b"]]
        asks = [(float(a[0]), float(a[1])) for a in result["a"]]
        return bids, asks
    except Exception:
        return [], []


def fetch_fear_greed():
    """
    Fear & Greed Index — alternative.me (бесплатно, без ключа).

    0-25:   Extreme Fear  → исторически зоны покупки
    26-45:  Fear          → осторожный лонг
    46-55:  Neutral
    56-75:  Greed         → осторожность
    76-100: Extreme Greed → рынок перегрет, риск разворота
    """
    try:
        r    = SESSION.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        data = r.json()
        val  = int(data["data"][0]["value"])
        lbl  = data["data"][0]["value_classification"]
        return val, lbl
    except Exception:
        return None, None


def fetch_btc_klines(interval="60", limit=212):
    """Кlines BTC для расчёта RS на нескольких таймфреймах."""
    try:
        return fetch_klines("BTCUSDT", interval, limit)
    except Exception:
        return [], [], [], [], []


# ─── Analysis ────────────────────────────────────────────────────────────────

def analyze_funding_trend(history):
    """
    Тренд Funding Rate за последние 8 периодов (~2.7 дня).

    'declining':    funding движется вниз (к более отрицательному)
                    → давление шортов нарастает → setup 1 сильнее
    'rising':       funding движется вверх (к более положительному)
                    → давление лонгов нарастает → лонги могут быть перегреты
    'normalizing':  экстремальный funding движется к нулю
                    → давление спадает → setup 1 теряет топливо
    'stable':       без явного тренда
    """
    if len(history) < 4:
        return "stable"

    mid      = len(history) // 2
    avg_old  = sum(history[:mid]) / mid
    avg_new  = sum(history[mid:]) / max(len(history) - mid, 1)
    delta    = avg_new - avg_old

    is_extreme_neg = avg_old < -0.03
    is_extreme_pos = avg_old > 0.03

    if is_extreme_neg and delta > 0.005:
        return "normalizing"   # было отрицательным, движется к нулю
    if is_extreme_pos and delta < -0.005:
        return "normalizing"   # было положительным, движется к нулю
    if delta < -0.005:
        return "declining"
    if delta > 0.005:
        return "rising"
    return "stable"


def detect_funding_extreme(funding_hist):
    """
    Экстремальные значения funding rate — исторические максимумы/минимумы
    за последние 8 периодов (~2.7 дня).

    Экстремально отрицательный funding:
      < -0.05% → рынок ОЧЕНЬ перегрет шортами → сквиз очень вероятен
      < -0.08% → исторически редкое событие → сильный сигнал разворота вверх

    Экстремально положительный:
      > +0.05% → лонги перегреты → риск слива вниз
      > +0.08% → памп перегрет → не лонговать

    Возвращает: ('extreme_neg', 'high_neg', 'extreme_pos', 'high_pos', 'normal', None)
    """
    if not funding_hist:
        return None
    current = funding_hist[-1]
    hist_min = min(funding_hist)
    hist_max = max(funding_hist)

    if current <= -0.08:
        return "extreme_neg"    # редчайший сквиз-триггер
    if current <= -0.05:
        return "high_neg"       # сильное шорт-давление
    if current >= 0.08:
        return "extreme_pos"    # лонги максимально перегреты
    if current >= 0.05:
        return "high_pos"       # лонги перегреты
    # Funding на минимуме за период (даже если не достиг порогов)
    if current == hist_min and current < -0.01:
        return "period_min"
    return "normal"


def detect_oi_divergence(oi_hist, closes, lookback=5):
    """
    Дивергенция Open Interest vs Цена за последние N завершённых свечей.

    Классика фьючерсного анализа (4 сценария):

    Цена ↑ + OI ↑ = 'strong_bull'  — новые лонги входят, тренд здоровый
    Цена ↓ + OI ↓ = 'bull_div'     — шорты закрываются у дна → разворот вверх
    Цена ↑ + OI ↓ = 'bear_div'     — лонги закрываются у хая → разворот вниз
    Цена ↓ + OI ↑ = 'strong_bear'  — новые шорты входят, тренд здоровый (медвежий)
    """
    n = min(len(oi_hist), len(closes))
    if n < lookback + 2:
        return None

    # Завершённые данные
    price_start = closes[-(lookback + 2)]
    price_end   = closes[-2]
    oi_start    = oi_hist[-(lookback + 1)] if len(oi_hist) >= lookback + 1 else oi_hist[0]
    oi_end      = oi_hist[-1]

    if price_start == 0 or oi_start == 0:
        return None

    price_chg = (price_end - price_start) / price_start * 100
    oi_chg    = (oi_end   - oi_start)    / oi_start    * 100

    # Порог: только значимые движения
    if abs(price_chg) < 1.0 and abs(oi_chg) < 2.0:
        return None

    price_up = price_chg >  1.0
    price_dn = price_chg < -1.0
    oi_up    = oi_chg    >  2.0
    oi_dn    = oi_chg    < -2.0

    if price_up and oi_up:  return "strong_bull"
    if price_dn and oi_dn:  return "bull_div"
    if price_up and oi_dn:  return "bear_div"
    if price_dn and oi_up:  return "strong_bear"
    return None


def calc_oi_velocity(oi_hist: list, recent: int = 6, prior: int = 6) -> float:
    """
    OI velocity — ускорение роста открытого интереса.

    Сравнивает скорость изменения OI за последние N периодов vs предыдущие N.
    Возвращает разницу в процентных пунктах:
      > +3%:  OI нарастает быстрее (новые деньги входят, импульс усиливается)
      < -3%:  OI замедляется или разворачивается (позиции исчерпаны, exhaustion)
      ≈ 0:    равномерный рост или флет
    """
    n = len(oi_hist)
    if n < recent + prior + 2:
        return 0.0
    p_start = oi_hist[-(recent + prior + 1)]
    p_end   = oi_hist[-(recent + 1)]
    r_start = p_end
    r_end   = oi_hist[-1]
    if p_start == 0 or r_start == 0:
        return 0.0
    prior_chg  = (p_end  - p_start)  / p_start  * 100
    recent_chg = (r_end  - r_start)  / r_start  * 100
    return round(recent_chg - prior_chg, 2)


def detect_candle_patterns(opens, highs, lows, closes):
    """
    Последний значимый свечной паттерн на завершённых свечах ([-4] до [-2]).

    Паттерны:
      bull_engulf      — бычье поглощение, тело поглощает предыдущую свечу
      bear_engulf      — медвежье поглощение
      hammer           — молот (тело ≤35% диапазона, нижний хвост ≥55%)
      shooting_star    — падающая звезда (тело ≤35%, верхний хвост ≥55%)
      doji             — доджи (тело ≤8%, хвосты примерно равны)
      gravestone_doji  — доджи-надгробие (тело у дна, верхний хвост ≥75%)
      dragonfly_doji   — доджи-стрекоза (тело у верха, нижний хвост ≥75%)
      inside_bar       — внутренняя свеча ≤70% mother bar (компрессия)

    Проверяем 3 последние завершённые свечи, возвращаем самый свежий.
    """
    if len(closes) < 5:
        return None

    for i in range(-2, -5, -1):  # [-2], [-3], [-4]
        try:
            o  = opens[i];  h  = highs[i];  l = lows[i];   c  = closes[i]
            po = opens[i-1]; ph = highs[i-1]; pl = lows[i-1]; pc = closes[i-1]
        except IndexError:
            break

        body      = abs(c - o)
        prev_body = abs(pc - po)
        rng       = h - l
        if rng == 0 or prev_body == 0:
            continue

        body_low   = min(o, c)
        body_high  = max(o, c)
        lower_wick = body_low  - l
        upper_wick = h - body_high

        # Бычье поглощение
        if (c > o and pc < po
                and c >= po and o <= pc
                and body >= prev_body * 1.2):
            return "bull_engulf"

        # Медвежье поглощение
        if (c < o and pc > po
                and c <= po and o >= pc
                and body >= prev_body * 1.2):
            return "bear_engulf"

        # Молот (hammer)
        if (body <= rng * 0.35
                and lower_wick >= rng * 0.55
                and upper_wick <= rng * 0.20):
            return "hammer"

        # Падающая звезда (shooting star)
        if (body <= rng * 0.35
                and upper_wick >= rng * 0.55
                and lower_wick <= rng * 0.20):
            return "shooting_star"

        # Доджи (нерешительность/разворот): тело ≤ 8% диапазона, хвосты примерно равны
        if rng > 0 and body <= rng * 0.08 and lower_wick >= rng * 0.35 and upper_wick >= rng * 0.35:
            return "doji"

        # Гравстоун доджи (bearish reversal на хае): тело у дна, длинный верхний хвост
        if (rng > 0 and body <= rng * 0.08
                and upper_wick >= rng * 0.75
                and lower_wick <= rng * 0.05):
            return "gravestone_doji"

        # Стрекоза доджи (bullish reversal на дне): тело у верха, длинный нижний хвост
        if (rng > 0 and body <= rng * 0.08
                and lower_wick >= rng * 0.75
                and upper_wick <= rng * 0.05):
            return "dragonfly_doji"

        # Внутренняя свеча (компрессия): baby bar должна быть существенно меньше mother bar
        prev_rng = ph - pl
        if (h < ph and l > pl
                and prev_rng > 0 and rng <= prev_rng * 0.70):  # ≤ 70% размера mother bar
            return "inside_bar"

    return None


def calc_atr(highs, lows, closes, period=14):
    """
    Average True Range (ATR 14) как % от цены.

    TR = max(high-low, |high-prev_close|, |low-prev_close|)
    Высокий ATR% = пара волатильная → стопы должны быть шире.
    Низкий ATR% = сжатие → часто предшествует сильному движению.

    Только завершённые свечи (исключаем [-1]).
    """
    if len(closes) < period + 2:
        return 0.0

    trs = []
    for i in range(1, len(closes) - 1):  # исключаем [-1]
        tr = max(
            highs[i]  - lows[i],
            abs(highs[i]  - closes[i-1]),
            abs(lows[i]   - closes[i-1]),
        )
        trs.append(tr)

    if not trs:
        return 0.0

    atr = sum(trs[-period:]) / min(period, len(trs))
    return atr / closes[-1] * 100


def calc_poc(highs, lows, volumes, closes, lookback=48, num_buckets=24):
    """
    Аппроксимация Point of Control (POC) — уровень наибольшего объёма.

    Метод: делим ценовой диапазон на 24 корзины, пропорционально
    распределяем объём каждой свечи по корзинам в её ценовом диапазоне.
    Корзина с максимальным объёмом = POC.

    POC — магнит для цены. Возвращает:
    - poc_price: ценовой уровень POC
    - dist_pct: расстояние цены от POC
      > 0: цена ВЫШЕ POC (POC ниже как поддержка)
      < 0: цена НИЖЕ POC (POC выше как сопротивление)
    """
    n = min(lookback, len(closes) - 1)
    hs = highs[-(n + 1):-1]
    ls = lows[-(n + 1):-1]
    vs = volumes[-(n + 1):-1]

    if not hs:
        return None, None

    price_min = min(ls)
    price_max = max(hs)
    rng = price_max - price_min
    if rng == 0:
        return closes[-1], 0.0

    bucket_sz = rng / num_buckets
    buckets   = [0.0] * num_buckets

    for h, l, v in zip(hs, ls, vs):
        candle_rng = h - l
        if candle_rng == 0:
            b = min(int((h - price_min) / bucket_sz), num_buckets - 1)
            buckets[b] += v
            continue
        for b in range(num_buckets):
            b_low  = price_min + b * bucket_sz
            b_high = price_min + (b + 1) * bucket_sz
            overlap = max(0.0, min(h, b_high) - max(l, b_low))
            if overlap > 0:
                buckets[b] += v * (overlap / candle_rng)

    poc_idx   = buckets.index(max(buckets))
    poc_price = price_min + (poc_idx + 0.5) * bucket_sz
    dist_pct  = (closes[-1] - poc_price) / closes[-1] * 100

    return poc_price, dist_pct


def detect_mtf_confluence(fvgs_1h, obs_1h, fvgs_4h, obs_4h):
    """
    Multi-Timeframe Confluence (MTF): перекрытие зон 4H и 1H.

    Это сильнейший сигнал в ICT/SMC анализе.
    Когда 4H FVG содержит 1H FVG (или OB) того же типа:
    - Smart Money формировали зону на обоих таймфреймах
    - Обе реагируют одновременно → высокая вероятность отработки

    Возвращает: (bull_mtf_count, bear_mtf_count)
    Значение 1+ = есть MTF конфлюенс. 3+ = очень сильная зона.
    """
    def overlap(ab, at, bb, bt):
        return ab <= bt and bb <= at

    all_4h_bull = [z for z in fvgs_4h + obs_4h if z["type"] == "bull"]
    all_4h_bear = [z for z in fvgs_4h + obs_4h if z["type"] == "bear"]
    all_1h_bull = [z for z in fvgs_1h + obs_1h if z["type"] == "bull"]
    all_1h_bear = [z for z in fvgs_1h + obs_1h if z["type"] == "bear"]

    bull_count = sum(
        1
        for z4 in all_4h_bull
        for z1 in all_1h_bull
        if overlap(z4["bottom"], z4["top"], z1["bottom"], z1["top"])
    )
    bear_count = sum(
        1
        for z4 in all_4h_bear
        for z1 in all_1h_bear
        if overlap(z4["bottom"], z4["top"], z1["bottom"], z1["top"])
    )

    return min(bull_count, 5), min(bear_count, 5)


def calc_kline_cvd(opens, closes, volumes, lookback=20):
    """CVD из свечей (20h, стабильный таймфрейм). Только завершённые свечи."""
    ops = opens[-(lookback + 1):-1]
    cls = closes[-(lookback + 1):-1]
    vls = volumes[-(lookback + 1):-1]
    if not vls:
        return 0.0, 0.0
    cvd   = sum(v if c > o else -v for o, c, v in zip(ops, cls, vls))
    total = sum(vls)
    return cvd, (cvd / total * 100 if total > 0 else 0.0)


def calc_trade_cvd(trades):
    """CVD из реальных сделок (переменный таймфрейм, точный)."""
    if not trades:
        return 0.0, 0.0
    cvd   = sum(sz if side == "Buy" else -sz for side, sz in trades)
    total = sum(sz for _, sz in trades)
    return cvd, (cvd / total * 100 if total > 0 else 0.0)


def detect_htf_trend(highs, lows, closes):
    """
    HTF тренд: swing structure (HH+HL / LH+LL) → fallback SMA20.
    Возвращает: 'bull', 'bear', 'range'
    """
    n = len(closes)
    if n < 10:
        return "range"

    swing_highs, swing_lows = [], []
    for i in range(2, n - 2):
        if (highs[i] > highs[i-1] and highs[i] > highs[i-2]
                and highs[i] > highs[i+1] and highs[i] > highs[i+2]):
            swing_highs.append(highs[i])
        if (lows[i] < lows[i-1] and lows[i] < lows[i-2]
                and lows[i] < lows[i+1] and lows[i] < lows[i+2]):
            swing_lows.append(lows[i])

    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        hh = swing_highs[-1] > swing_highs[-2]
        hl = swing_lows[-1]  > swing_lows[-2]
        lh = swing_highs[-1] < swing_highs[-2]
        ll = swing_lows[-1]  < swing_lows[-2]
        if hh and hl: return "bull"
        if lh and ll: return "bear"

    if n >= 20:
        sma20 = sum(closes[-20:]) / 20
        price = closes[-1]
        if price > sma20 * 1.015: return "bull"
        if price < sma20 * 0.985: return "bear"

    return "range"


def detect_fvg(highs, lows, closes, lookback=40, min_size_pct=0.05):
    """
    FVG (Fair Value Gap). Бычий: high[i-2]<low[i]. Медвежий: low[i-2]>high[i].
    Фильтр: min_size_pct — минимальный размер зоны (избегаем шума).
    in_zone=True: цена внутри FVG прямо сейчас.
    """
    fvgs  = []
    price = closes[-1]
    n     = len(closes)
    start = max(2, n - lookback)

    for i in range(start, n):
        # Бычий FVG
        if highs[i - 2] < lows[i]:
            bottom   = highs[i - 2]
            top      = lows[i]
            size_pct = (top - bottom) / price * 100
            if size_pct < min_size_pct or price < bottom:
                continue
            in_zone = bottom <= price <= top
            dist    = 0.0 if in_zone else (price - top) / price * 100
            fvgs.append({"type": "bull", "top": top, "bottom": bottom,
                         "size_pct": size_pct, "dist_pct": dist, "in_zone": in_zone})

        # Медвежий FVG
        if lows[i - 2] > highs[i]:
            bottom   = highs[i]
            top      = lows[i - 2]
            size_pct = (top - bottom) / price * 100
            if size_pct < min_size_pct or price > top:
                continue
            in_zone = bottom <= price <= top
            dist    = 0.0 if in_zone else (bottom - price) / price * 100
            fvgs.append({"type": "bear", "top": top, "bottom": bottom,
                         "size_pct": size_pct, "dist_pct": dist, "in_zone": in_zone})

    fvgs.sort(key=lambda x: x["dist_pct"])
    return fvgs


def detect_order_blocks(opens, highs, lows, closes, volumes, lookback=40):
    """
    Order Block: последняя свеча противоположного типа перед BOS.
    Бычий OB = поддержка (last bearish before bullish BOS).
    Медвежий OB = сопротивление (last bullish before bearish BOS).
    in_zone=True: цена внутри OB прямо сейчас.
    """
    obs   = []
    price = closes[-1]
    n     = len(closes)
    start = max(6, n - lookback)

    for i in range(start, n):
        # Бычий BOS
        prior_highs = highs[i - 5:i]
        if prior_highs and closes[i] > max(prior_highs):
            for j in range(i - 1, max(0, i - 6), -1):
                if closes[j] < opens[j]:
                    ob_top    = opens[j]
                    ob_bottom = closes[j]
                    if price > ob_bottom:
                        in_zone = ob_bottom <= price <= ob_top
                        dist    = 0.0 if in_zone else (price - ob_top) / price * 100
                        obs.append({"type": "bull", "top": ob_top, "bottom": ob_bottom,
                                    "dist_pct": dist, "in_zone": in_zone,
                                    "body_pct": abs(closes[j]-opens[j])/opens[j]*100})
                    break

        # Медвежий BOS
        prior_lows = lows[i - 5:i]
        if prior_lows and closes[i] < min(prior_lows):
            for j in range(i - 1, max(0, i - 6), -1):
                if closes[j] > opens[j]:
                    ob_top    = closes[j]
                    ob_bottom = opens[j]
                    if price < ob_top:
                        in_zone = ob_bottom <= price <= ob_top
                        dist    = 0.0 if in_zone else (ob_bottom - price) / price * 100
                        obs.append({"type": "bear", "top": ob_top, "bottom": ob_bottom,
                                    "dist_pct": dist, "in_zone": in_zone,
                                    "body_pct": abs(closes[j]-opens[j])/opens[j]*100})
                    break

    seen, unique = set(), []
    for ob in obs:
        key = (ob["type"], round(ob["top"] / (price + 1e-12), 4))
        if key not in seen:
            seen.add(key)
            unique.append(ob)
    unique.sort(key=lambda x: x["dist_pct"])
    return unique[:5]


def detect_sweep(highs, lows, closes, lookback=7):
    """
    Sweep: одна из трёх последних завершённых свечей [-2], [-3], [-4]
    пробила хай/лой предыдущих N свечей и закрылась обратно.

    lookback уменьшен до 7 (с 10) — чтобы ловить sweep vs ближайшей структуры.
    Проверяем три свечи назад: повышает частоту сигнала без потери качества,
    т.к. sweep отрабатывает в течение нескольких часов после импульса.
    """
    if len(highs) < lookback + 5:
        return None, None

    sweep_up = sweep_down = None

    for offset in range(2, 5):   # [-2], [-3], [-4]
        if len(highs) < lookback + offset + 1:
            break
        last_h = highs[-offset]
        last_l = lows[-offset]
        last_c = closes[-offset]
        # Предыдущие N свечей ДО проверяемой (не включая её)
        prev_h = highs[-(lookback + offset):-offset]
        prev_l = lows[-(lookback + offset):-offset]
        if not prev_h:
            break

        prev_high = max(prev_h)
        prev_low  = min(prev_l)

        if sweep_up is None and last_h > prev_high and last_c < prev_high:
            sweep_up = prev_high
        if sweep_down is None and last_l < prev_low and last_c > prev_low:
            sweep_down = prev_low

    return sweep_up, sweep_down


def detect_atr_compression(highs, lows, closes):
    """
    ATR Compression: текущий ATR (14 свечей) vs исторический ATR (40 свечей).

    Когда волатильность сжимается относительно своей нормы — это «пружина».
    Чем дольше и сильнее сжатие, тем мощнее будет выброс.

    compression_ratio:
      < 0.50 = экстремальное сжатие → взрыв близко
      < 0.65 = сильное сжатие       → хороший сигнал
      < 0.80 = умеренное сжатие
      ≥ 1.0  = норма или расширение
    """
    if len(closes) < 45:
        return 1.0

    h = highs[:-1]   # без текущей незакрытой свечи
    l = lows[:-1]
    c = closes[:-1]

    def _avg_tr(h_s, l_s, c_s):
        trs = [
            max(h_s[i] - l_s[i],
                abs(h_s[i] - c_s[i - 1]),
                abs(l_s[i] - c_s[i - 1]))
            for i in range(1, len(c_s))
        ]
        return sum(trs) / len(trs) if trs else 0.0

    atr14 = _avg_tr(h[-15:], l[-15:], c[-15:])
    atr40 = _avg_tr(h[-41:], l[-41:], c[-41:])

    return round(atr14 / atr40, 3) if atr40 > 0 else 1.0


def detect_oi_coil(oi_hist, closes, hours=12):
    """
    OI Coil (накопление под сжатием): OI растёт, пока цена стоит.

    Паттерн: крупный игрок тихо набирает позицию. Рынок не замечает
    пока «пружина» не отпустится. Именно так выглядит памп изнутри
    до того как он становится виден на графике.

    Возвращает: (oi_change_pct, price_range_pct, is_coiling)
    is_coiling = True: OI +4%+ при диапазоне цены < 2% за последние hours
    """
    n = min(hours, len(oi_hist) - 1, len(closes) - 2)
    if n < 4:
        return 0.0, 0.0, False

    oi_start = oi_hist[-(n + 1)]
    oi_end   = oi_hist[-1]
    if oi_start == 0:
        return 0.0, 0.0, False

    oi_chg = (oi_end - oi_start) / oi_start * 100

    c_recent = closes[-(n + 2):-1]   # завершённые свечи за период
    if len(c_recent) < 2:
        return round(oi_chg, 2), 0.0, False

    lo = min(c_recent)
    price_rng = (max(c_recent) - lo) / lo * 100 if lo > 0 else 0.0
    is_coiling = oi_chg > 4.0 and price_rng < 2.0

    return round(oi_chg, 2), round(price_rng, 2), is_coiling


def calc_rsi(closes, period=14):
    """
    RSI 14 на завершённых свечах (без незакрытой [-1]).
    Использует сглаживание Уайлдера (SMMA), как в оригинальном RSI.
    > 70 = перекуплен. < 30 = перепродан. Дивергенция = главный сигнал.

    Исправлено: было простое среднее — давало неточные значения RSI.
    Теперь: экспоненциальное сглаживание по методу Уайлдера.
    """
    c = closes[:-1]
    if len(c) < period + 2:
        return 50.0
    gains  = [max(c[i] - c[i-1], 0) for i in range(1, len(c))]
    losses = [max(c[i-1] - c[i], 0) for i in range(1, len(c))]
    # Инициализация: первое значение — простое среднее
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    # Сглаживание Уайлдера (SMMA): ag = (ag*(period-1) + gain) / period
    for g, l in zip(gains[period:], losses[period:]):
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + l) / period
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 2)


def detect_rsi_divergence(highs, lows, closes, period=14, lookback=30):
    """
    RSI Дивергенция — классическая и скрытая.

    bull_div    (классическая ↑): цена LL, RSI HL  → разворот вверх
    bear_div    (классическая ↓): цена HH, RSI LH  → разворот вниз
    hidden_bull (скрытая ↑):      цена HL, RSI LL  → продолжение роста
    hidden_bear (скрытая ↓):      цена LH, RSI HH  → продолжение падения

    Анализируем завершённые свечи ([-2] и старше).
    """
    n = min(lookback + period + 3, len(closes) - 1)
    if n < period + 6:
        return None

    c = closes[-(n + 1):-1]
    h = highs[-(n + 1):-1]
    l = lows[-(n + 1):-1]

    # Wilder's SMMA RSI для всего ряда завершённых свечей
    gains_all  = [max(c[j] - c[j-1], 0) for j in range(1, len(c))]
    losses_all = [max(c[j-1] - c[j], 0) for j in range(1, len(c))]
    if len(gains_all) < period:
        return None
    ag = sum(gains_all[:period])  / period
    al = sum(losses_all[:period]) / period
    rsi_vals = []
    for g, lv in zip(gains_all[period:], losses_all[period:]):
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + lv) / period
        rsi_vals.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))

    ph = h[period:]
    pl = l[period:]
    if len(rsi_vals) < 5:
        return None

    sw_h, sw_l = [], []
    for i in range(1, len(rsi_vals) - 1):
        if rsi_vals[i] > rsi_vals[i-1] and rsi_vals[i] > rsi_vals[i+1]:
            sw_h.append((i, rsi_vals[i], ph[i] if i < len(ph) else 0))
        if rsi_vals[i] < rsi_vals[i-1] and rsi_vals[i] < rsi_vals[i+1]:
            sw_l.append((i, rsi_vals[i], pl[i] if i < len(pl) else 0))

    if len(sw_h) < 2 or len(sw_l) < 2:
        return None

    h1, h2 = sw_h[-1], sw_h[-2]
    l1, l2 = sw_l[-1], sw_l[-2]

    if l1[2] < l2[2] and l1[1] > l2[1]: return "bull_div"
    if h1[2] > h2[2] and h1[1] < h2[1]: return "bear_div"
    if l1[2] > l2[2] and l1[1] < l2[1]: return "hidden_bull"
    if h1[2] < h2[2] and h1[1] > h2[1]: return "hidden_bear"
    return None


def calc_vwap(highs, lows, closes, volumes, lookback=24):
    """
    VWAP за последние lookback завершённых свечей.

    deviation > 0: цена ВЫШЕ VWAP — перекуплена, возможен возврат
    deviation < 0: цена НИЖЕ VWAP — перепродана, возможен отскок
    ±2% = значимое отклонение. ±4% = экстремальное.
    """
    n = min(lookback, len(closes) - 2)
    if n < 3:
        return None, None
    h = highs[-(n + 1):-1];  l = lows[-(n + 1):-1]
    c = closes[-(n + 1):-1]; v = volumes[-(n + 1):-1]
    total_vol = sum(v)
    if total_vol == 0:
        return None, None
    vwap = sum((hi + lo + cl) / 3 * vol for hi, lo, cl, vol in zip(h, l, c, v)) / total_vol
    price = closes[-1]
    return round(vwap, 8), round((price - vwap) / vwap * 100, 2)


def calc_ema(closes, period):
    """EMA — простой расчёт."""
    if len(closes) < period:
        return []
    k = 2.0 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for p in closes[period:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return ema


def calc_ema_structure(highs, lows, closes, periods=(20, 50, 200)):
    """
    EMA Structure на завершённых свечах.

    Возвращает dict:
    ema20/50/200     — текущие значения (None если мало данных)
    price_vs_ema20/50/200 — 'above' / 'below'
    ema20_slope / ema50_slope — 'rising' / 'falling' / 'flat'
    golden_cross  — EMA20 пересекла EMA50 вверх (последние 3 свечи)
    death_cross   — EMA20 пересекла EMA50 вниз
    ema_bull      — price > EMA20 > EMA50 (идеальный бычий порядок)
    ema_bear      — price < EMA20 < EMA50 (идеальный медвежий порядок)
    above_ema200  — цена выше EMA200 (долгосрочный бычий фон)
    """
    c     = closes[:-1]   # завершённые
    price = closes[-1]
    res   = {
        "ema20": None, "ema50": None, "ema200": None,
        "price_vs_ema20": None, "price_vs_ema50": None, "price_vs_ema200": None,
        "ema20_slope": None, "ema50_slope": None,
        "golden_cross": False, "death_cross": False,
        "ema_bull": False, "ema_bear": False, "above_ema200": None,
    }
    ema20_all = ema50_all = None

    for p in periods:
        if len(c) < p + 1:
            continue
        vals = calc_ema(c, p)
        if len(vals) < 4:
            continue
        cur = vals[-1]
        if p == 20:
            res["ema20"] = round(cur, 8)
            res["price_vs_ema20"] = "above" if price > cur else "below"
            d = (cur - vals[-4]) / vals[-4] * 100 if vals[-4] > 0 else 0
            res["ema20_slope"] = "rising" if d > 0.05 else ("falling" if d < -0.05 else "flat")
            ema20_all = vals
        elif p == 50:
            res["ema50"] = round(cur, 8)
            res["price_vs_ema50"] = "above" if price > cur else "below"
            d = (cur - vals[-4]) / vals[-4] * 100 if vals[-4] > 0 else 0
            res["ema50_slope"] = "rising" if d > 0.03 else ("falling" if d < -0.03 else "flat")
            ema50_all = vals
        elif p == 200:
            res["ema200"] = round(cur, 8)
            res["price_vs_ema200"] = "above" if price > cur else "below"
            res["above_ema200"] = price > cur

    if ema20_all and ema50_all:
        ml = min(len(ema20_all), len(ema50_all))
        if ml >= 4:
            res["golden_cross"] = ema20_all[-3] < ema50_all[-3] and ema20_all[-1] > ema50_all[-1]
            res["death_cross"]  = ema20_all[-3] > ema50_all[-3] and ema20_all[-1] < ema50_all[-1]
        e20 = res["ema20"]; e50 = res["ema50"]
        if e20 and e50:
            res["ema_bull"] = price > e20 > e50
            res["ema_bear"] = price < e20 < e50

    return res


def calc_volume_profile_full(highs, lows, volumes, closes, lookback=48, num_buckets=24):
    """
    Полный Volume Profile: POC + VAH + VAL (Value Area 70%).

    POC — уровень максимального объёма, магнит для цены.
    VAH — Value Area High (верхняя граница 70% объёма).
    VAL — Value Area Low  (нижняя граница).

    Интерпретация:
    цена > VAH = перекуплена относительно Value Area → ожидай возврат
    VAL < цена < VAH = "справедливая зона" — нейтрально
    цена < VAL = перепродана → ожидай отскок к POC / VAH

    dist% > 0: цена ВЫШЕ уровня. dist% < 0: цена НИЖЕ уровня.
    """
    n   = min(lookback, len(closes) - 1)
    hs  = highs[-(n + 1):-1]
    ls  = lows[-(n + 1):-1]
    vs  = volumes[-(n + 1):-1]
    if not hs:
        return None, None, None, None, None, None

    p_min = min(ls); p_max = max(hs); rng = p_max - p_min
    if rng == 0:
        pr = closes[-1]
        return pr, pr, pr, 0.0, 0.0, 0.0

    bsz = rng / num_buckets
    bkt = [0.0] * num_buckets
    for h, l, v in zip(hs, ls, vs):
        cr = h - l
        if cr == 0:
            b = min(int((h - p_min) / bsz), num_buckets - 1)
            bkt[b] += v
            continue
        for b in range(num_buckets):
            ov = max(0.0, min(h, p_min + (b+1)*bsz) - max(l, p_min + b*bsz))
            if ov > 0:
                bkt[b] += v * ov / cr

    total  = sum(bkt)
    if total == 0:
        return None, None, None, None, None, None

    poc_i  = bkt.index(max(bkt))
    poc_p  = p_min + (poc_i + 0.5) * bsz
    price  = closes[-1]

    # Value Area (70%)
    target = total * 0.70
    accum  = bkt[poc_i]
    lo_i   = poc_i; hi_i = poc_i
    while accum < target:
        ah = bkt[hi_i + 1] if hi_i + 1 < num_buckets else 0
        al = bkt[lo_i - 1] if lo_i - 1 >= 0          else 0
        if ah >= al and hi_i + 1 < num_buckets:
            hi_i += 1; accum += ah
        elif lo_i - 1 >= 0:
            lo_i -= 1; accum += al
        else:
            break

    vah = p_min + (hi_i + 1) * bsz
    val = p_min + lo_i       * bsz

    def _d(lv): return round((price - lv) / price * 100, 2) if price > 0 else 0.0
    return (round(poc_p, 8), round(vah, 8), round(val, 8),
            _d(poc_p), _d(vah), _d(val))


def detect_choch(highs, lows, closes, lookback=30):
    """
    CHoCH (Change of Character) — ранний сигнал смены тренда, раньше BOS.

    bull_choch: нисходящий тренд (LH+LL) → цена пробила последний LH вверх
    bear_choch: восходящий тренд (HH+HL) → цена пробила последний HL вниз

    Vs BOS:
    CHoCH = ПЕРВЫЙ слом (ранний, больше ложных), BOS = подтверждённая смена.
    Стратегия: CHoCH как ранний вход, BOS как подтверждение = точный тайминг.
    """
    n = min(lookback, len(closes) - 2)
    if n < 8:
        return None
    c = closes[-(n + 1):-1]
    h = highs[-(n + 1):-1]
    l = lows[-(n + 1):-1]

    sw_h, sw_l = [], []
    for i in range(2, len(c) - 2):
        if h[i] > h[i-1] and h[i] > h[i-2] and h[i] > h[i+1] and h[i] > h[i+2]:
            sw_h.append((i, h[i]))
        if l[i] < l[i-1] and l[i] < l[i-2] and l[i] < l[i+1] and l[i] < l[i+2]:
            sw_l.append((i, l[i]))

    if len(sw_h) < 2 or len(sw_l) < 2:
        return None

    cur = c[-1]
    # Восходящий тренд (HH+HL) → слом вниз
    if sw_h[-1][1] > sw_h[-2][1] and sw_l[-1][1] > sw_l[-2][1]:
        if cur < sw_l[-1][1]:
            return "bear_choch"
    # Нисходящий тренд (LH+LL) → слом вверх
    if sw_h[-1][1] < sw_h[-2][1] and sw_l[-1][1] < sw_l[-2][1]:
        if cur > sw_h[-1][1]:
            return "bull_choch"
    return None


def detect_equal_levels(highs, lows, closes, tolerance_pct=0.3, lookback=30):
    """
    Equal Highs (EQH) / Equal Lows (EQL) — кластеры ликвидности.

    Когда цена несколько раз касается одного уровня — там накапливаются
    стоп-лоссы. Рынок часто идёт «забирать» эти уровни.

    Возвращает: (eq_highs, eq_lows) — до 3 уровней каждый.
    eq_highs > текущей цены = магниты / цели роста (стопы шортов).
    eq_lows  < текущей цены = магниты / цели падения (стопы лонгов).
    """
    n = min(lookback, len(closes) - 2)
    if n < 5:
        return [], []
    h = highs[-(n + 1):-1]; l = lows[-(n + 1):-1]
    price = closes[-1]; tol = price * tolerance_pct / 100

    peaks   = [h[i] for i in range(1, len(h)-1) if h[i] >= h[i-1] and h[i] >= h[i+1]]
    troughs = [l[i] for i in range(1, len(l)-1) if l[i] <= l[i-1] and l[i] <= l[i+1]]

    def _cluster(levels):
        if not levels:
            return []
        sv = sorted(levels); clusters = []; grp = [sv[0]]
        for lv in sv[1:]:
            if lv - grp[-1] <= tol * 2:
                grp.append(lv)
            else:
                if len(grp) >= 2:
                    clusters.append(round(sum(grp)/len(grp), 8))
                grp = [lv]
        if len(grp) >= 2:
            clusters.append(round(sum(grp)/len(grp), 8))
        return clusters

    eq_highs = sorted([lv for lv in _cluster(peaks)   if lv > price])
    eq_lows  = sorted([lv for lv in _cluster(troughs) if lv < price], reverse=True)
    return eq_highs[:3], eq_lows[:3]


def detect_cvd_divergence(kl_cvd_pct, price_chg_pct):
    """
    CVD-Price дивергенция (скрытое накопление / распределение).

    Логика: если тейкеры агрессивно покупают (CVD ↑), но цена не растёт —
    значит кто-то продаёт через лимитные ордера, поглощая покупки. Когда
    лимитные ордера заканчиваются — цена «взрывается» вверх. Это и есть памп.

    bull_div:        CVD > +15%, цена < +0.5% за 20h  → скрытая покупка
    strong_bull_div: CVD > +30%, цена < +1.0% за 20h  → сильное накопление
    bear_div / strong_bear_div: зеркально для шортов
    """
    if kl_cvd_pct > 30 and price_chg_pct < 1.0:
        return "strong_bull_div"
    if kl_cvd_pct > 15 and price_chg_pct < 0.5:
        return "bull_div"
    if kl_cvd_pct < -30 and price_chg_pct > -1.0:
        return "strong_bear_div"
    if kl_cvd_pct < -15 and price_chg_pct > -0.5:
        return "bear_div"
    return None


def detect_volume_accel(opens, closes, volumes, lookback=6):
    """
    Volume Acceleration: объём нарастает на бычьих (или медвежьих) свечах.

    Классика начала памп-движения: сначала появляется объём, потом цена.
    Если последние 3 свечи зелёные и объём каждой больше предыдущей —
    импульс набирается прямо сейчас.

    Возвращает: ('bull_accel' / 'bear_accel' / None, accel_ratio)
    accel_ratio: отношение среднего объёма последней половины к первой.
    """
    if len(closes) < lookback + 3:
        return None, 1.0

    o_s = opens[-(lookback + 2):-1]
    c_s = closes[-(lookback + 2):-1]
    v_s = volumes[-(lookback + 2):-1]

    if len(v_s) < 4:
        return None, 1.0

    mid = len(v_s) // 2
    avg_early = sum(v_s[:mid]) / max(mid, 1)
    avg_late  = sum(v_s[mid:]) / max(len(v_s) - mid, 1)
    accel_ratio = avg_late / avg_early if avg_early > 0 else 1.0

    # Направление по последним 3 завершённым свечам
    bull_vol = sum(v for o, c, v in zip(o_s[-3:], c_s[-3:], v_s[-3:]) if c >= o)
    bear_vol = sum(v for o, c, v in zip(o_s[-3:], c_s[-3:], v_s[-3:]) if c < o)

    if accel_ratio > 1.4:
        if bull_vol >= bear_vol:
            return "bull_accel", round(accel_ratio, 2)
        return "bear_accel", round(accel_ratio, 2)

    return None, round(accel_ratio, 2)


def detect_whale_activity(trades):
    """
    Кит в ленте: одиночная сделка ≥ 8× среднего размера.

    Означает: институционал или крупный трейдер только что вошёл рыночным
    ордером. Обычно это либо начало движения, либо ускоряет уже начавшееся.
    Bybit возвращает newest first → анализируем топ-100 свежих сделок.

    Возвращает: ('Buy'/'Sell' / None, multiplier)
    """
    if len(trades) < 30:
        return None, 0.0

    sizes = [sz for _, sz in trades]
    avg_size = sum(sizes) / len(sizes)
    if avg_size <= 0:
        return None, 0.0

    recent = trades[:min(100, len(trades))]   # newest first
    whale  = max(recent, key=lambda x: x[1])
    mult   = whale[1] / avg_size

    if mult >= 8.0:
        return whale[0], round(mult, 1)
    return None, 0.0


def detect_absorption(opens, highs, lows, closes, volumes, lookback=10):
    """
    Поглощение (Absorption) — статический снимок.

    Признак: объём резко вырос (>2.5× медианы) на свечах, которые
    практически не двинули цену (тело < 0.4% от цены).

    Bullish absorption: на падающей цене появляется огромный объём
    с маленьким телом → продавцов поглощают крупные покупатели.

    Bearish absorption: на растущей цене — зеркально.

    Важно: это статический снимок, НЕ реал-тайм наблюдение стакана.
    Точный absorption виден только в футпринт-чарте или real-time DOM.

    Возвращает: ('bull_absorb' / 'bear_absorb' / None, ratio)
    """
    n = min(lookback + 2, len(closes) - 1)
    if n < 5:
        return None, 0.0

    o_s = opens[-(n + 1):-1]
    h_s = highs[-(n + 1):-1]
    l_s = lows[-(n + 1):-1]
    c_s = closes[-(n + 1):-1]
    v_s = volumes[-(n + 1):-1]

    if len(v_s) < 4:
        return None, 0.0

    sorted_v = sorted(v_s)
    med_v = sorted_v[len(sorted_v) // 2]
    if med_v <= 0:
        return None, 0.0

    # Ищем свечу с аномальным объёмом и маленьким телом
    for i in range(len(c_s) - 1, max(len(c_s) - 6, 0), -1):
        vol_ratio = v_s[i] / med_v
        if vol_ratio < 2.5:
            continue
        price_ref = c_s[i] if c_s[i] > 0 else 1.0
        body_pct = abs(c_s[i] - o_s[i]) / price_ref * 100
        if body_pct > 1.5:  # в крипто до 1.5% тела на огромном объёме = поглощение
            continue
        # Найдена свеча поглощения — определяем направление по контексту
        # Предыдущие 3 свечи дают направление: если шли вниз = бычье поглощение
        if i >= 3:
            prev_dir = c_s[i - 1] - c_s[i - 3]
            if prev_dir < 0:
                return "bull_absorb", round(vol_ratio, 1)  # цена падала → бычье погл.
            elif prev_dir > 0:
                return "bear_absorb", round(vol_ratio, 1)  # цена росла → медвежье погл.

    return None, 0.0


def detect_liq_events(oi_hist, closes, volumes):
    """
    Аппроксимация ликвидаций: OI drop >3% + объём >2× медиану + цена >1%.
    Медиана объёма устойчива к выбросам (не искажает порог).
    НЕ является реальным хитмапом ликвидаций.
    """
    events = []
    n = min(len(oi_hist), len(closes), len(volumes))
    if n < 5:
        return events

    sorted_vols = sorted(volumes[:n])
    median_vol  = sorted_vols[n // 2]
    if median_vol == 0:
        return events

    for i in range(1, n):
        prev_oi = oi_hist[i - 1]
        if prev_oi == 0:
            continue
        oi_chg  = (oi_hist[i] - prev_oi) / prev_oi * 100
        px_move = abs(closes[i] - closes[i-1]) / closes[i-1] * 100 if closes[i-1] > 0 else 0
        vol_sp  = volumes[i] / median_vol

        if oi_chg < -3 and vol_sp > 2.0 and px_move > 1.0:
            events.append({
                "oi_drop": oi_chg, "px_move": px_move, "vol_x": vol_sp,
                "side": "long_liq" if closes[i] < closes[i-1] else "short_liq",
            })
    return events[-3:]


def analyze_dom(bids, asks, price):
    """
    DOM snapshot. Ближайшие 20 уровней. Имбаланс + стена (90-й перцентиль).
    Не показывает absorption (нужен WebSocket).
    """
    if not bids or not asks:
        return {"imbalance": 0.0, "bid_wall": None, "ask_wall": None,
                "bid_wall_dist_%": None, "ask_wall_dist_%": None}

    near_bids = sorted(bids, key=lambda x: -x[0])[:20]
    near_asks = sorted(asks, key=lambda x:  x[0])[:20]

    bid_usd = sum(p * s for p, s in near_bids)
    ask_usd = sum(p * s for p, s in near_asks)
    total   = bid_usd + ask_usd
    imbal   = (bid_usd - ask_usd) / total * 100 if total > 0 else 0.0

    all_sizes = sorted(s for _, s in near_bids + near_asks)
    p90_idx   = max(0, int(len(all_sizes) * 0.90) - 1)
    p90_size  = all_sizes[p90_idx] if all_sizes else 0.0

    max_bid = max(near_bids, key=lambda x: x[1])
    max_ask = max(near_asks, key=lambda x: x[1])
    bid_wall = max_bid if max_bid[1] >= p90_size else None
    ask_wall = max_ask if max_ask[1] >= p90_size else None

    return {
        "imbalance":       imbal,
        "bid_wall":        bid_wall,
        "ask_wall":        ask_wall,
        "bid_wall_dist_%": abs(price - max_bid[0]) / price * 100 if bid_wall else None,
        "ask_wall_dist_%": abs(max_ask[0] - price) / price * 100 if ask_wall else None,
    }


def detect_stacked_walls(bids, asks, price,
                         max_levels: int = 50,
                         proximity_pct: float = 1.2,
                         size_mult: float = 2.5):
    """
    Завалы (stacked walls): несколько крупных лимитных ордеров одной
    стороны, скучкованных рядом по цене ("батарея" вместо одного сайза).

    Эвристика: порог "крупного" = max(median × size_mult, avg × (size_mult-0.5));
    стенки со стороны считаются в стек, пока они в пределах `proximity_pct`%
    от первой (ближайшей к цене). Требуется ≥ 2 уровня.

    Возвращает {bid_stack, ask_stack}: список (price, size) от ближайшего
    к текущей цене до самого дальнего в стеке, либо None.
    """
    def _stack(orders, side: str):
        if not orders:
            return None
        if side == "bid":
            near = sorted(orders, key=lambda x: -x[0])[:max_levels]
        else:
            near = sorted(orders, key=lambda x: x[0])[:max_levels]
        if len(near) < 4:
            return None
        sizes = sorted(s for _, s in near)
        med = sizes[len(sizes) // 2]
        avg = sum(sizes) / len(sizes)
        thresh = max(med * size_mult, avg * (size_mult - 0.5))
        if thresh <= 0:
            return None

        stack = []
        for p, s in near:
            if s < thresh:
                continue
            if not stack:
                stack.append((p, s))
                continue
            first_p = stack[0][0]
            if abs(p - first_p) / price * 100 > proximity_pct:
                break
            stack.append((p, s))

        return stack if len(stack) >= 2 else None

    return {
        "bid_stack": _stack(bids, "bid"),
        "ask_stack": _stack(asks, "ask"),
    }


# ─── External data readers (liq DB / outcome feedback) ───────────────────────

def fetch_liquidation_stats(window_min: int = 60) -> dict:
    """
    Bulk-загрузка ликвидаций из liquidations.db за последние window_min минут.
    Возвращает {symbol: {long_usd, short_usd, total_usd}} для всех символов
    с активностью. Пустой dict если БД недоступна.
    """
    if not LIQ_DB_PATH.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{LIQ_DB_PATH}?mode=ro", uri=True, timeout=2)
        cutoff_ms = int((time.time() - window_min * 60) * 1000)
        rows = con.execute(
            "SELECT symbol, "
            "       SUM(CASE WHEN side='long_liq'  THEN usd ELSE 0 END), "
            "       SUM(CASE WHEN side='short_liq' THEN usd ELSE 0 END) "
            "FROM liquidations WHERE ts >= ? GROUP BY symbol",
            (cutoff_ms,),
        ).fetchall()
        con.close()
        return {
            sym: {"long_usd": float(lu or 0), "short_usd": float(su or 0),
                  "total_usd": float((lu or 0) + (su or 0))}
            for sym, lu, su in rows
        }
    except Exception:
        return {}


def fetch_btc_4h_change() -> float:
    """BTC 4h price change (%). Для BTC-velocity фильтра."""
    try:
        o, h, l, c, v = fetch_klines("BTCUSDT", "240", 2)
        if len(c) >= 2 and c[-2] > 0:
            return (c[-1] - c[-2]) / c[-2] * 100
    except Exception:
        pass
    return 0.0


def fetch_btc_4h_ema_position() -> str:
    """BTC 4h цена относительно EMA20 и EMA50. Возвращает: above / below / between / unknown."""
    try:
        _, _, _, cl, _ = fetch_klines("BTCUSDT", "240", 60)
        if len(cl) < 52:
            return "unknown"
        def _ema(prices, n):
            k = 2 / (n + 1)
            val = float(prices[0])
            for p in prices[1:]:
                val = float(p) * k + val * (1 - k)
            return val
        price = float(cl[-1])
        e20   = _ema(cl, 20)
        e50   = _ema(cl, 50)
        if price > e20 and price > e50:
            return "above"
        elif price < e20 and price < e50:
            return "below"
        else:
            return "between"
    except Exception:
        return "unknown"


def load_score_weights(min_samples: int = 20) -> dict:
    """
    Читает outcomes/resolved.csv и строит множитель score по бакету
    (setup × grade) на основе историческго WR (TP1/WIN vs STOP/LOSS).
    Возвращает dict {(setup, grade): multiplier in [0.5, 1.5]}.
    Бакеты с выборкой < min_samples получают 1.0.

    Также загружает calibration/signal_weights.json (если существует) и добавляет
    под ключом "__signal_weights__" — аддитивные поправки к score по сигнальным флагам.
    """
    if not OUTCOMES_CSV.exists():
        return {}
    buckets: dict = {}
    try:
        with open(OUTCOMES_CSV, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                setup = row.get("setup", "")
                grade = row.get("grade", "")
                outcome = row.get("outcome_24h") or row.get("outcome_4h") or ""
                if not setup or not grade or not outcome:
                    continue
                key = (setup, grade)
                b = buckets.setdefault(key, {"wins": 0, "losses": 0})
                if outcome in ("TP1", "WIN"):
                    b["wins"] += 1
                elif outcome in ("STOP", "LOSS"):
                    b["losses"] += 1
    except Exception:
        return {}
    out = {}
    for key, b in buckets.items():
        total = b["wins"] + b["losses"]
        if total < min_samples:
            continue
        wr = b["wins"] / total
        # WR 50% → 1.0; WR 70% → 1.4; WR 30% → 0.6. Зажато в [0.5, 1.5].
        mult = max(0.5, min(1.5, wr / 0.5))
        out[key] = round(mult, 3)

    # Load per-signal additive adjustments from logistic regression calibration.
    import json as _json
    sig_w_path = Path(__file__).parent / "calibration" / "signal_weights.json"
    if sig_w_path.exists():
        try:
            with open(sig_w_path, "r", encoding="utf-8") as _f:
                out["__signal_weights__"] = _json.load(_f)
        except Exception:
            pass

    # SHORT-specific signal weights (T1.2: separate LR model for short direction)
    sig_w_short_path = Path(__file__).parent / "calibration" / "signal_weights_short.json"
    if sig_w_short_path.exists():
        try:
            with open(sig_w_short_path, "r", encoding="utf-8") as _f:
                out["__signal_weights_short__"] = _json.load(_f)
        except Exception:
            pass

    # Per-setup LR weights (TASK C): CHoCH has opposite sign per setup.
    for _setup_name in ("bos_fvg", "breakout", "squeeze"):
        _p = Path(__file__).parent / "calibration" / f"signal_weights_{_setup_name}.json"
        if _p.exists():
            try:
                with open(_p, "r", encoding="utf-8") as _f:
                    out[f"__signal_weights_{_setup_name}__"] = _json.load(_f)
            except Exception:
                pass

    return out


def detect_lvn_zones(highs, lows, volumes, closes,
                     lookback: int = 48, num_buckets: int = 24,
                     threshold: float = 0.30) -> list:
    """
    Low-Volume Nodes: ценовые бакеты с объёмом < threshold × POC bucket.
    Цена проходит LVN быстро — это зоны ускорения.
    Возвращает list[(lo, hi)] — ценовые диапазоны LVN.
    """
    n = min(lookback, len(closes) - 1)
    hs = highs[-(n + 1):-1]
    ls = lows[-(n + 1):-1]
    vs = volumes[-(n + 1):-1]
    if not hs:
        return []
    p_min = min(ls); p_max = max(hs); rng = p_max - p_min
    if rng == 0:
        return []
    bsz = rng / num_buckets
    bkt = [0.0] * num_buckets
    for h, l, v in zip(hs, ls, vs):
        cr = h - l
        if cr == 0:
            b = min(int((h - p_min) / bsz), num_buckets - 1)
            bkt[b] += v
            continue
        for b in range(num_buckets):
            ov = max(0.0, min(h, p_min + (b + 1) * bsz) - max(l, p_min + b * bsz))
            if ov > 0:
                bkt[b] += v * ov / cr
    max_v = max(bkt) if bkt else 0
    if max_v == 0:
        return []
    thresh = max_v * threshold
    lvns = []
    i = 0
    while i < num_buckets:
        if 0 < bkt[i] < thresh:
            j = i
            while j + 1 < num_buckets and 0 < bkt[j + 1] < thresh:
                j += 1
            lvns.append((round(p_min + i * bsz, 8),
                         round(p_min + (j + 1) * bsz, 8)))
            i = j + 1
        else:
            i += 1
    return lvns


# ─── Scoring ─────────────────────────────────────────────────────────────────

def score_symbol(symbol, ticker, oi_hist,
                 op1h, hi1h, lo1h, cl1h, vol1h,
                 op4h, hi4h, lo4h, cl4h, vol4h,
                 opD,  hiD,  loD,  clD,  volD,
                 ls_ratio, trades, bids, asks,
                 funding_hist, btc_chg_24h,
                 liq_stats=None, btc_chg_4h=0.0, score_weights=None,
                 global_ctx=None):

    # ── Цена (mark price — точнее last price) ──
    price = float(ticker.get("markPrice", cl1h[-1]))

    # ── Funding + Basis ──
    funding   = float(ticker.get("fundingRate", 0)) * 100
    mark_p    = float(ticker.get("markPrice",  price))
    index_p   = float(ticker.get("indexPrice", price))
    basis_pct = (mark_p - index_p) / index_p * 100 if index_p > 0 else 0.0

    # ── OI Change 24h ──
    current_oi = float(ticker.get("openInterest", 0))
    oi_change  = 0.0
    if len(oi_hist) >= 25 and oi_hist[-25] > 0:
        oi_change = (current_oi - oi_hist[-25]) / oi_hist[-25] * 100
    elif len(oi_hist) >= 2 and oi_hist[0] > 0:
        oi_change = (current_oi - oi_hist[0]) / oi_hist[0] * 100

    # ── Позиция цены в 48h (завершённые свечи) ──
    comp_hi = hi1h[-49:-1] if len(hi1h) >= 49 else hi1h[:-1]
    comp_lo = lo1h[-49:-1] if len(lo1h) >= 49 else lo1h[:-1]
    if not comp_hi:
        return None
    hi48 = max(comp_hi)
    lo48 = min(comp_lo)
    rng  = hi48 - lo48
    if rng == 0:
        return None
    price_pos = max(0.0, min(1.0, (price - lo48) / rng))

    # ── Объём (завершённые свечи) ──
    if len(vol1h) >= 22:
        baseline = sorted(vol1h[-22:-2])
        med_vol  = baseline[len(baseline) // 2]
        cur_vol  = vol1h[-2]
    elif len(vol1h) >= 4:
        baseline = sorted(vol1h[:-2])
        med_vol  = baseline[len(baseline) // 2]
        cur_vol  = vol1h[-2]
    else:
        med_vol = vol1h[-1];  cur_vol = vol1h[-1]
    vol_ratio = cur_vol / med_vol if med_vol > 0 else 1.0

    # ── HTF тренды: Daily + 4H ──
    daily_trend = detect_htf_trend(hiD,  loD,  clD)
    h4_trend    = detect_htf_trend(hi4h, lo4h, cl4h)
    # Согласованность: оба в одну сторону = сильный сигнал
    trend_bull_aligned = (daily_trend == "bull" and h4_trend == "bull")
    trend_bear_aligned = (daily_trend == "bear" and h4_trend == "bear")

    # ── FVG: 1H и 4H ──
    fvgs_1h = detect_fvg(hi1h, lo1h, cl1h, lookback=40, min_size_pct=0.05)
    fvgs_4h = detect_fvg(hi4h, lo4h, cl4h, lookback=30, min_size_pct=0.10)

    # ── Order Blocks: 1H и 4H ──
    obs_1h = detect_order_blocks(op1h, hi1h, lo1h, cl1h, vol1h, lookback=40)
    obs_4h = detect_order_blocks(op4h, hi4h, lo4h, cl4h, vol4h, lookback=30)

    # ── MTF Confluence ──
    bull_mtf, bear_mtf = detect_mtf_confluence(fvgs_1h, obs_1h, fvgs_4h, obs_4h)

    # ── CVD: kline + trade ──
    _, kl_cvd_pct = calc_kline_cvd(op1h, cl1h, vol1h, lookback=20)
    _, tr_cvd_pct = calc_trade_cvd(trades)
    cvd_bull_aligned = kl_cvd_pct > 15 and tr_cvd_pct > 10
    cvd_bear_aligned = kl_cvd_pct < -15 and tr_cvd_pct < -10

    # ── Новые аналитические метрики ──
    oi_div        = detect_oi_divergence(oi_hist, cl1h, lookback=5)
    candle_pat    = detect_candle_patterns(op1h, hi1h, lo1h, cl1h)
    atr_pct       = calc_atr(hi1h, lo1h, cl1h, period=14)
    poc_price, poc_dist = calc_poc(hi1h, lo1h, vol1h, cl1h, lookback=48)
    fund_trend    = analyze_funding_trend(funding_hist)
    fund_extreme  = detect_funding_extreme(funding_hist)

    # ── Funding streak: подряд идущие отрицательные/положительные периоды ─────
    # funding_hist: новые справа. Считаем длину текущей серии одного знака.
    neg_streak = 0
    pos_streak = 0
    for fv in reversed(funding_hist or []):
        try:
            rate = float(fv.get("fundingRate", 0)) if isinstance(fv, dict) else float(fv)
        except (TypeError, ValueError):
            break
        if rate < 0 and pos_streak == 0:
            neg_streak += 1
        elif rate > 0 and neg_streak == 0:
            pos_streak += 1
        else:
            break

    # ── Pre-Pump метрики ──────────────────────────────────────────────────────
    atr_compression = detect_atr_compression(hi1h, lo1h, cl1h)

    oi_coil_chg, oi_coil_rng, oi_coiling = detect_oi_coil(oi_hist, cl1h, hours=12)

    # Изменение цены за последние 20h (совпадает с окном kline CVD)
    if len(cl1h) >= 22 and cl1h[-22] > 0:
        price_chg_20h = (cl1h[-2] - cl1h[-22]) / cl1h[-22] * 100
    else:
        price_chg_20h = 0.0

    cvd_div = detect_cvd_divergence(kl_cvd_pct, price_chg_20h)

    vol_accel_dir, vol_accel_ratio = detect_volume_accel(op1h, cl1h, vol1h, lookback=6)
    whale_side, whale_mult         = detect_whale_activity(trades)

    # Relative Strength vs BTC (> 1 = опережает BTC)
    pair_chg = float(ticker.get("price24hPcnt", 0)) * 100
    rs_btc   = pair_chg / btc_chg_24h if abs(btc_chg_24h) > 0.5 else None

    # ── Новые метрики: EMA, VWAP, RSI, CHoCH, Volume Profile, Equal levels ──────
    rsi_1h     = calc_rsi(cl1h)
    rsi_div_1h = detect_rsi_divergence(hi1h, lo1h, cl1h, lookback=30)

    vwap_val, vwap_dev = calc_vwap(hi1h, lo1h, cl1h, vol1h, lookback=24)

    ema_1h = calc_ema_structure(hi1h, lo1h, cl1h)
    ema_4h = calc_ema_structure(hi4h, lo4h, cl4h)

    poc_full, vah, val_vp, poc_dist_f, vah_dist, val_dist = \
        calc_volume_profile_full(hi1h, lo1h, vol1h, cl1h, lookback=48)

    # Low-Volume Nodes — зоны быстрого проскальзывания (потенциал магнитов)
    lvn_zones = detect_lvn_zones(hi1h, lo1h, vol1h, cl1h, lookback=48)
    # Ближайшая LVN и расстояние от цены
    lvn_nearest = None
    lvn_nearest_dist = None
    if lvn_zones:
        ref_price = float(ticker.get("markPrice", cl1h[-1]))
        def _lvn_dist(z):
            lo, hi = z
            if lo <= ref_price <= hi:
                return 0.0
            return min(abs(ref_price - lo), abs(ref_price - hi)) / ref_price * 100
        lvn_nearest = min(lvn_zones, key=_lvn_dist)
        lvn_nearest_dist = _lvn_dist(lvn_nearest)

    choch_1h = detect_choch(hi1h, lo1h, cl1h, lookback=30)
    choch_4h = detect_choch(hi4h, lo4h, cl4h, lookback=20)

    eq_highs, eq_lows = detect_equal_levels(hi1h, lo1h, cl1h, lookback=30)

    # 1D FVG/OB → расширение MTF на дневной ТФ
    fvgs_1d = detect_fvg(hiD, loD, clD, lookback=20, min_size_pct=0.30) if len(clD) > 5 else []
    obs_1d  = detect_order_blocks(opD, hiD, loD, clD, volD, lookback=20) if len(clD) > 7 else []

    # MTF Extended: учитываем 1H + 4H + 1D совпадения
    def _overlap(ab, at, bb, bt): return ab <= bt and bb <= at
    bull_1d = [z for z in fvgs_1d + obs_1d if z["type"] == "bull"]
    bear_1d = [z for z in fvgs_1d + obs_1d if z["type"] == "bear"]
    bull_4h = [z for z in fvgs_4h + obs_4h if z["type"] == "bull"]
    bear_4h = [z for z in fvgs_4h + obs_4h if z["type"] == "bear"]
    mtf_1d_bull = min(sum(
        1 for z4 in bull_4h for z1 in bull_1d
        if _overlap(z4["bottom"], z4["top"], z1["bottom"], z1["top"])
    ), 3)
    mtf_1d_bear = min(sum(
        1 for z4 in bear_4h for z1 in bear_1d
        if _overlap(z4["bottom"], z4["top"], z1["bottom"], z1["top"])
    ), 3)
    bull_mtf_ext = min(bull_mtf + mtf_1d_bull, 5)
    bear_mtf_ext = min(bear_mtf + mtf_1d_bear, 5)

    # ── Sweep, ликвидации, DOM ──
    sweep_up, sweep_down = detect_sweep(hi1h, lo1h, cl1h, lookback=10)
    liq_events    = detect_liq_events(oi_hist, cl1h, vol1h)
    dom           = analyze_dom(bids, asks, price)
    stacks        = detect_stacked_walls(bids, asks, price)
    absorb_dir, absorb_ratio = detect_absorption(op1h, hi1h, lo1h, cl1h, vol1h, lookback=10)

    # Разбивка по типу
    bull_fvg_1h = [f for f in fvgs_1h if f["type"] == "bull"]
    bear_fvg_1h = [f for f in fvgs_1h if f["type"] == "bear"]
    bull_ob_1h  = [o for o in obs_1h  if o["type"] == "bull"]
    bear_ob_1h  = [o for o in obs_1h  if o["type"] == "bear"]

    in_bull_fvg = any(f["in_zone"] for f in bull_fvg_1h)
    in_bear_fvg = any(f["in_zone"] for f in bear_fvg_1h)
    in_bull_ob  = any(o["in_zone"] for o in bull_ob_1h)
    in_bear_ob  = any(o["in_zone"] for o in bear_ob_1h)

    long_liq  = any(e["side"] == "long_liq"  for e in liq_events)
    short_liq = any(e["side"] == "short_liq" for e in liq_events)

    # ── Локальные ликвидации из БД (liquidation_tracker 60мин окно) ──
    liq_row      = (liq_stats or {}).get(symbol, {}) if liq_stats else {}
    liq_long_usd  = float(liq_row.get("long_usd",  0.0))
    liq_short_usd = float(liq_row.get("short_usd", 0.0))
    liq_total_usd = float(liq_row.get("total_usd", 0.0))

    # Свечные паттерны по направлению
    bullish_pattern = candle_pat in ("bull_engulf", "hammer", "dragonfly_doji")
    bearish_pattern = candle_pat in ("bear_engulf", "shooting_star", "gravestone_doji")

    # ── Stacked walls: извлекаем ДО scoring чтобы использовать в s1/s4/s5 ──
    bid_stack_sc = stacks.get("bid_stack") or []
    ask_stack_sc = stacks.get("ask_stack") or []
    # Дистанции до ближайшей стенки
    bid_wall_d = abs(price - bid_stack_sc[0][0]) / price * 100 if bid_stack_sc else 99.0
    ask_wall_d = abs(ask_stack_sc[0][0] - price) / price * 100 if ask_stack_sc else 99.0

    # ── OI velocity: ускорение роста OI ──
    oi_velocity = calc_oi_velocity(oi_hist)

    # ── Global context (perp/spot ratio, BTC dominance, listing age, BTC EMA pos) ──
    _ctx            = global_ctx or {}
    perp_spot_ratio = _ctx.get("perp_spot_ratio")   # None или float
    btc_dominance   = _ctx.get("btc_dominance")      # None или float 0-100
    btc_ema_pos     = _ctx.get("btc_ema_pos", "unknown")  # above/below/between

    # Возраст листинга в днях (из batch instruments-info, переданного через global_ctx)
    _lt_ms = (_ctx.get("listing_ts_map") or {}).get(symbol)
    listing_age_days = round((time.time() * 1000 - _lt_ms) / 86_400_000) if _lt_ms else None

    # Социальный сентимент (CryptoPanic + LunarCrush) — пре-загружен в run_screener
    _base_sym = symbol.upper().replace("USDT", "").replace("PERP", "")
    _social   = (_ctx.get("social_ctx") or {}).get(_base_sym, {})
    _cp       = _social.get("cryptopanic", {})    # {score, hot, titles}
    _lc       = _social.get("lunarcrush", {})     # {galaxy_score, alt_rank, sentiment}

    # CoinGecko trending — пре-загружен в run_screener (бесплатно, без API-ключа)
    _is_trending = _base_sym in (_ctx.get("trending_symbols") or set())

    # ── Weekly + 15m context (TASK A) ────────────────────────────────────────
    _k1w  = _ctx.get("k1w",  ([], [], [], [], []))
    _k15m = _ctx.get("k15m", ([], [], [], [], []))
    op1w, hi1w, lo1w, cl1w, vol1w   = _k1w
    op15m, hi15m, lo15m, cl15m, vol15m = _k15m
    weekly_ctx  = compute_weekly_context(op1w, hi1w, lo1w, cl1w, price)
    m15_ctx     = compute_15m_context(op15m, hi15m, lo15m, cl15m, price)
    weekly_trend = weekly_ctx["weekly_trend"]
    m15_trend    = m15_ctx["m15_trend"]

    # Средний объём за 7 завершённых дней (USD)
    avg_vol_7d_usd = None
    if len(volD) >= 8 and len(clD) >= 8:
        avg_vol_7d_usd = round(
            sum(float(v) * float(c) for v, c in zip(volD[-8:-1], clD[-8:-1])) / 7
        )

    scores, notes = {}, {}

    # ═══════════════════════════════════════════════════════════════════════════
    # СЕТАП 1 — ЛИКВИДАЦИОННЫЙ СКВИЗ
    # ═══════════════════════════════════════════════════════════════════════════
    s1, n1 = 0, []

    # Funding (главное топливо сквиза)
    if funding < -0.01:
        s1 += 35; n1.append(f"fund={funding:.3f}%")
    elif funding < 0:
        s1 += 22; n1.append(f"fund={funding:.3f}%")
    elif funding < 0.005:
        s1 += 8;  n1.append("fund≈0")

    # Экстремальный funding — FINDING 2: extreme_neg = сквиз УЖЕ произошёл → штраф
    if fund_extreme == "extreme_neg":
        s1 -= 10; n1.append("FUND_EXTREME!")   # < -0.08% — сквиз скорее всего отработан
    elif fund_extreme == "high_neg":
        s1 += 14; n1.append("fund_high_neg")   # < -0.05% — сильное давление
    elif fund_extreme == "period_min":
        s1 += 8;  n1.append("fund_period_min") # минимум за ~3 дня
    elif fund_extreme in ("extreme_pos", "high_pos"):
        s1 -= 15; n1.append("fund_перегрет!")  # лонги перегреты = не время для сквиза

    # Оптимальная зона funding: умеренно отрицательный (-0.03% до 0%) = сквиз ещё впереди
    if -0.03 <= funding < 0:
        s1 += 15; n1.append("fund_opt(-0.03→0)")

    # Funding trend: нарастающее давление
    if fund_trend == "declining":
        s1 += 15; n1.append("fund↓нараст")
    elif fund_trend == "normalizing" and funding < -0.01:
        s1 -= 8;  n1.append("fund норм-ся")

    # Серия отрицательного funding = устойчивое давление шортов
    if neg_streak >= 5:
        s1 += 12; n1.append(f"fund−×{neg_streak}!")
    elif neg_streak >= 3:
        s1 += 6;  n1.append(f"fund−×{neg_streak}")

    # Цена у поддержки
    if price_pos < 0.15:
        s1 += 28; n1.append(f"дно {price_pos:.0%}")
    elif price_pos < 0.30:
        s1 += 16; n1.append(f"нижн {price_pos:.0%}")
    elif price_pos < 0.40:
        s1 += 6

    # OI упал = ликвидации прошли
    if oi_change < -10:
        s1 += 25; n1.append(f"OI{oi_change:.1f}%")
    elif oi_change < -5:
        s1 += 14; n1.append(f"OI{oi_change:.1f}%")
    # FIX 6: extreme OI build (>15%) at lows = speculative longs, not true bottom
    if oi_change > 15:
        s1 -= 10; n1.append(f"OI_ext+{oi_change:.0f}%!")

    # OI Divergence: шорты закрываются у дна = разворот вверх (только у дна)
    if oi_div == "bull_div" and price_pos < 0.35:
        s1 += 18; n1.append("OI_div↑")
    elif oi_div == "bull_div":
        s1 += 8;  n1.append("OI_div↑")  # вне дна — слабее
    elif oi_div == "strong_bull":
        s1 += 8;  n1.append("OI_bull")
    elif oi_div == "bear_div":
        s1 -= 10  # лонги закрываются = плохой сигнал для лонга

    # Подтверждения
    if long_liq:
        s1 += 12; n1.append("лонг-лики")

    # Локальная БД: шорты ликвидировались крупно → топливо для сквиза вверх
    if liq_short_usd >= 1_000_000:
        s1 += 22; n1.append(f"LIQ_short ${liq_short_usd/1e6:.1f}M!")
    elif liq_short_usd >= 300_000:
        s1 += 12; n1.append(f"LIQ_short ${liq_short_usd/1e3:.0f}K")
    elif liq_short_usd >= 100_000:
        s1 += 6

    # HTF: согласованность Daily + 4H
    if trend_bull_aligned:
        s1 += 15; n1.append("D+4H↑")
    elif daily_trend == "bull" or h4_trend == "bull":
        s1 += 8;  n1.append("HTF↑")

    # MTF Confluence (КЛЮЧЕВОЙ СИГНАЛ) — tiered: fresh signal > saturated signal
    if bull_mtf >= 5:
        s1 += 10; n1.append(f"MTF{bull_mtf}!")
    elif bull_mtf >= 4:
        s1 += 20; n1.append(f"MTF{bull_mtf}!")
    elif bull_mtf >= 3:
        s1 += 30; n1.append(f"MTF{bull_mtf}!")
    elif bull_mtf >= 2:
        s1 += 18; n1.append(f"MTF{bull_mtf}")
    elif bull_mtf == 1:
        s1 += 10; n1.append("MTF1")

    # Цена В FVG/OB прямо сейчас — полный бонус только у дна (price_pos < 0.40)
    if in_bull_fvg and price_pos < 0.40:
        s1 += 18; n1.append("В FVG↑!")
    elif in_bull_fvg:
        s1 += 9;  n1.append("В FVG↑(mid)")  # зона, но не у дна
    elif bull_fvg_1h and bull_fvg_1h[0]["dist_pct"] < 1.5:
        s1 += 10; n1.append(f"FVG↑{bull_fvg_1h[0]['dist_pct']:.1f}%")
    elif bull_fvg_1h and bull_fvg_1h[0]["dist_pct"] < 3.0:
        s1 += 5

    if in_bull_ob and price_pos < 0.40:
        s1 += 18; n1.append("В OB↑!")
    elif in_bull_ob:
        s1 += 9;  n1.append("В OB↑(mid)")  # зона, но не у дна
    elif bull_ob_1h and bull_ob_1h[0]["dist_pct"] < 1.5:
        s1 += 10; n1.append(f"OB↑{bull_ob_1h[0]['dist_pct']:.1f}%")
    elif bull_ob_1h and bull_ob_1h[0]["dist_pct"] < 3.0:
        s1 += 5

    # Свечной паттерн подтверждает разворот вверх
    if bullish_pattern and (in_bull_fvg or in_bull_ob or price_pos < 0.3):
        s1 += 14; n1.append(f"{candle_pat}!")
    elif bullish_pattern:
        s1 += 7

    # POC: цена выше POC → POC как поддержка снизу
    if poc_dist is not None and 0 < poc_dist < 3:
        s1 += 8; n1.append(f"POC+{poc_dist:.1f}%")

    # CVD
    if cvd_bull_aligned and price_pos < 0.40:
        s1 += 12; n1.append(f"CVD↑({kl_cvd_pct:.0f}/{tr_cvd_pct:.0f}%)")
    elif kl_cvd_pct > 0 and price_pos < 0.35:
        s1 += 5

    # Поглощение: крупный объём без движения цены = крупный игрок набирает позицию
    if absorb_dir == "bull_absorb":
        s1 += 16; n1.append(f"ABSORB↑×{absorb_ratio:.1f}")
    elif absorb_dir == "bear_absorb" and price_pos < 0.25:
        s1 -= 8  # медвежье поглощение у дна = плохо для сквиза

    # DOM
    if dom["imbalance"] > 30:
        s1 += 8;  n1.append(f"DOM+{dom['imbalance']:.0f}%")
    elif dom["imbalance"] > 15:
        s1 += 4

    # Basis: дисконт при дне = дополнительное давление
    if basis_pct < -0.05 and price_pos < 0.30:
        s1 += 6; n1.append(f"basis{basis_pct:.2f}%")

    # RS vs BTC: опережение при дне
    if rs_btc is not None and rs_btc > 1.3 and price_pos < 0.40:
        s1 += 6; n1.append(f"RS{rs_btc:.1f}x")
    # FIX 4: moderate RS (0-2x) = clean setup; extreme RS (>5x) = already pumped
    if rs_btc is not None and 0 < rs_btc < 2:
        s1 += 8; n1.append(f"RS{rs_btc:.2f}(mod)")
    if rs_btc is not None and rs_btc > 5:
        s1 -= 8; n1.append(f"RS{rs_btc:.1f}x(ext)")

    # Стенка ставок НИЖЕ цены = покупатели держат поддержку → топливо сквиза
    if len(bid_stack_sc) >= 2 and bid_wall_d <= 2.5:
        wall_pts = min(len(bid_stack_sc) * 5, 18)
        s1 += wall_pts; n1.append(f"bid_стек×{len(bid_stack_sc)}")

    # Perp/Spot ratio: высокое = много шортов с плечом = больше топлива
    if perp_spot_ratio is not None:
        if perp_spot_ratio > 5.0:
            s1 += 14; n1.append(f"P/S={perp_spot_ratio:.1f}x!")
        elif perp_spot_ratio > 3.0:
            s1 += 7;  n1.append(f"P/S={perp_spot_ratio:.1f}x")

    # BTC Dominance: alt season усиливает, BTC season ослабляет сквиз
    if btc_dominance is not None:
        if btc_dominance < 47:
            s1 += 8;  n1.append(f"BTC.d={btc_dominance:.0f}%↓alt")
        elif btc_dominance > 52:
            s1 -= 8;  n1.append(f"BTC.d={btc_dominance:.0f}%↑btc")

    # CHoCH↑_1H в СКВИЗЕ = импульс уже потрачен (WR -20.5pp, коэф. из train_model.py)
    # FINDING 3: в bos_fvg/breakout CHoCH = позитив; в squeeze = отработан заранее → штраф
    if choch_1h == "bull_choch":
        s1 -= 15; n1.append("CHoCH↑1H⚠")

    # CoinGecko trending + дно = социальный хайп подтверждает разворот (бесплатно, без ключа)
    if _is_trending and price_pos < 0.40:
        s1 += 10; n1.append("trending🔥")

    scores["squeeze"] = s1
    notes["squeeze"]  = ", ".join(n1) or "—"

    # ═══════════════════════════════════════════════════════════════════════════
    # СЕТАП 2 — BOS + FVG / ORDER BLOCK
    # ═══════════════════════════════════════════════════════════════════════════
    s2, n2 = 0, []

    if vol_ratio > 2.5:
        s2 += 25; n2.append(f"vol×{vol_ratio:.1f}")
    elif vol_ratio > 1.8:
        s2 += 15; n2.append(f"vol×{vol_ratio:.1f}")
    elif vol_ratio > 1.3:
        s2 += 7

    if oi_change > 15:
        s2 += 25; n2.append(f"OI+{oi_change:.1f}%")
    elif oi_change > 8:
        s2 += 15; n2.append(f"OI+{oi_change:.1f}%")
    elif oi_change > 4:
        s2 += 7;  n2.append(f"OI+{oi_change:.1f}%")
    # FIX 6: extreme OI (>15%) = overcrowded; additive penalty (net +15 not +25)
    if oi_change > 15:
        s2 -= 10; n2.append(f"OI_ext+{oi_change:.0f}%!")

    # OI Divergence подтверждает направление
    if oi_div == "strong_bull":
        s2 += 12; n2.append("OI_bull")
    elif oi_div == "strong_bear":
        s2 += 12; n2.append("OI_bear")

    if 0.25 < price_pos < 0.75:
        s2 += 15; n2.append(f"откат {price_pos:.0%}")

    if ls_ratio and ls_ratio > 1.5:
        s2 += 10; n2.append(f"L/S={ls_ratio:.2f}")

    # MTF Confluence — tiered: fresh signal > saturated signal
    best_mtf = max(bull_mtf, bear_mtf)
    if best_mtf >= 5:
        s2 += 10; n2.append(f"MTF{best_mtf}!")
    elif best_mtf >= 4:
        s2 += 20; n2.append(f"MTF{best_mtf}!")
    elif best_mtf >= 3:
        s2 += 30; n2.append(f"MTF{best_mtf}!")
    elif best_mtf >= 2:
        s2 += 18; n2.append(f"MTF{best_mtf}")
    elif best_mtf == 1:
        s2 += 10; n2.append("MTF1")

    # FVG / OB
    if in_bull_fvg or in_bear_fvg:
        s2 += 22; n2.append("В FVG!")
    elif fvgs_1h and fvgs_1h[0]["dist_pct"] < 2.0:
        s2 += 14; n2.append(f"FVG {fvgs_1h[0]['type']} {fvgs_1h[0]['dist_pct']:.1f}%")
    elif fvgs_1h and fvgs_1h[0]["dist_pct"] < 4.0:
        s2 += 6

    if in_bull_ob or in_bear_ob:
        s2 += 22; n2.append("В OB!")
    elif obs_1h and obs_1h[0]["dist_pct"] < 2.0:
        s2 += 12; n2.append(f"OB {obs_1h[0]['type']} {obs_1h[0]['dist_pct']:.1f}%")
    elif obs_1h and obs_1h[0]["dist_pct"] < 4.0:
        s2 += 5

    # Свечной паттерн у зоны
    if candle_pat and (in_bull_fvg or in_bear_fvg or in_bull_ob or in_bear_ob):
        s2 += 14; n2.append(f"{candle_pat}!")
    elif candle_pat:
        s2 += 6

    # HTF
    if trend_bull_aligned:
        s2 += 12; n2.append("D+4H↑")
    elif trend_bear_aligned:
        s2 += 12; n2.append("D+4H↓")
    elif daily_trend != "range" or h4_trend != "range":
        s2 += 5;  n2.append(f"D:{daily_trend}/4H:{h4_trend}")

    # POC: цена у POC = зона притяжения + отскок
    if poc_dist is not None and abs(poc_dist) < 1.0:
        s2 += 8; n2.append(f"POC {poc_dist:+.1f}%")

    # CVD direction-aware для bos_fvg (AVEVA-56):
    # LONG bos_fvg: CVD↑ = подтверждение (+12), CVD↓ = контра (-8)
    # SHORT bos_fvg: CVD↓ = подтверждение (+12), CVD↑ = контра (-8)
    # Данные: CVD>+15 = 58.0% WR vs CVD<-5 = 50.0% WR (delta +8pp)
    if trend_bull_aligned:
        if cvd_bull_aligned:   s2 += 12; n2.append("CVD↑")
        elif cvd_bear_aligned: s2 -= 8;  n2.append("CVD↓⚠")
    elif trend_bear_aligned:
        if cvd_bear_aligned:   s2 += 12; n2.append("CVD↓")
        elif cvd_bull_aligned: s2 -= 8;  n2.append("CVD↑⚠")
    else:
        if cvd_bull_aligned:   s2 += 8;  n2.append("CVD↑")
        elif cvd_bear_aligned: s2 += 8;  n2.append("CVD↓")

    # RS vs BTC
    if rs_btc is not None and rs_btc > 1.5:
        s2 += 8; n2.append(f"RS{rs_btc:.1f}x")
    # FIX 4: moderate RS (0-2x) = clean setup; extreme RS (>5x) = already pumped
    if rs_btc is not None and 0 < rs_btc < 2:
        s2 += 8; n2.append(f"RS{rs_btc:.2f}(mod)")
    if rs_btc is not None and rs_btc > 5:
        s2 -= 8; n2.append(f"RS{rs_btc:.1f}x(ext)")

    # CHoCH_1H: смена структуры подтверждает структурный пробой (WR=55.3%, +14.3pp)
    if choch_1h == "bull_choch":
        s2 += 20; n2.append("CHoCH↑1H!")
    elif choch_1h == "bear_choch":
        s2 += 20; n2.append("CHoCH↓1H!")

    scores["bos_fvg"] = s2
    notes["bos_fvg"]  = ", ".join(n2) or "—"

    # ═══════════════════════════════════════════════════════════════════════════
    # СЕТАП 3 — РЕЙНДЖ SWEEP
    # sweep↓ = bullish reversal (лонг); sweep↑ = bearish reversal (шорт)
    # Считаем раздельно, берём доминирующее направление.
    # ═══════════════════════════════════════════════════════════════════════════
    s3_long, n3_long   = 0, []  # sweep↓ → разворот вверх
    s3_short, n3_short = 0, []  # sweep↑ → разворот вниз

    if sweep_down is not None:
        s3_long  += 55; n3_long.append(f"sweep↓{sweep_down:.4g}")
    if sweep_up is not None:
        s3_short += 55; n3_short.append(f"sweep↑{sweep_up:.4g}")

    # Price near range floor → bullish; near ceiling → bearish
    if price_pos < 0.12:
        s3_long  += 18; n3_long.append(f"дно {price_pos:.0%}")
    elif price_pos > 0.88:
        s3_short += 18; n3_short.append(f"вершина {price_pos:.0%}")

    # Funding vs позиция
    if funding > 0.01 and price_pos < 0.35:
        s3_long  += 14; n3_long.append(f"fund+{funding:.3f}% при дне")
    elif funding < -0.01 and price_pos > 0.65:
        s3_short += 14; n3_short.append(f"fund{funding:.3f}% при вершине")

    # Funding trend усиливает несоответствие
    if fund_trend == "rising" and price_pos < 0.35:
        s3_long  += 8; n3_long.append("fund↑нараст")
    elif fund_trend == "declining" and price_pos > 0.65:
        s3_short += 8; n3_short.append("fund↓нараст")

    # Ликвидации после sweep
    if long_liq  and sweep_down is not None:
        s3_long  += 15; n3_long.append("лонг-лики")
    if short_liq and sweep_up is not None:
        s3_short += 15; n3_short.append("шорт-лики")

    # Локальная БД: реальный $ объём ликвидаций подтверждает sweep-разворот
    # sweep↓ забрал лонговую ликвидность → лонги ликвидируются → разворот вверх
    if sweep_down is not None and liq_long_usd >= 500_000:
        s3_long += 18; n3_long.append(f"LIQ_long ${liq_long_usd/1e3:.0f}K!")
    elif sweep_down is not None and liq_long_usd >= 150_000:
        s3_long += 9
    # sweep↑ забрал шортовую ликвидность → шорты ликвидируются → разворот вниз
    if sweep_up is not None and liq_short_usd >= 500_000:
        s3_short += 18; n3_short.append(f"LIQ_short ${liq_short_usd/1e3:.0f}K!")
    elif sweep_up is not None and liq_short_usd >= 150_000:
        s3_short += 9

    # Свечной паттерн разворота после sweep
    if bullish_pattern and sweep_down is not None:
        s3_long  += 14; n3_long.append(f"{candle_pat} после sweep↓")
    if bearish_pattern and sweep_up is not None:
        s3_short += 14; n3_short.append(f"{candle_pat} после sweep↑")

    # MTF зона в точке разворота
    if in_bull_fvg and sweep_down is not None:
        s3_long  += 12; n3_long.append("В FVG↑ после sweep↓")
    if in_bear_fvg and sweep_up is not None:
        s3_short += 12; n3_short.append("В FVG↓ после sweep↑")

    # DOM
    if dom["imbalance"] > 25 and sweep_down is not None:
        s3_long  += 10; n3_long.append(f"DOM+{dom['imbalance']:.0f}%")
    if dom["imbalance"] < -25 and sweep_up is not None:
        s3_short += 10; n3_short.append(f"DOM{dom['imbalance']:.0f}%")

    # OI Divergence после sweep подтверждает разворот
    if oi_div == "bull_div" and sweep_down is not None:
        s3_long  += 12; n3_long.append("OI_div↑ после sweep↓")
    if oi_div == "bear_div" and sweep_up is not None:
        s3_short += 12; n3_short.append("OI_div↓ после sweep↑")

    # Доминирующее направление sweep-сетапа
    if s3_long >= s3_short:
        scores["range_sweep"] = s3_long
        notes["range_sweep"]  = ", ".join(n3_long) or "—"
        sweep_dir_3 = "long"
    else:
        scores["range_sweep"] = s3_short
        notes["range_sweep"]  = ", ".join(n3_short) or "—"
        sweep_dir_3 = "short"

    # ═══════════════════════════════════════════════════════════════════════════
    # СЕТАП 4 — BREAKOUT / PRE-PUMP
    #
    # Цель: поймать памп ДО или В САМОМ НАЧАЛЕ движения.
    # Логика: ищем признаки скрытого накопления и сжатия пружины.
    # В отличие от сетапов 1-3 (вход у уровня/после движения), этот сетап
    # ориентирован на момент, когда движение ЕЩЁ НЕ НАЧАЛОСЬ или только
    # начинается — ATR сжат, OI накапливается, CVD указывает на скупку.
    # ═══════════════════════════════════════════════════════════════════════════
    s4, n4 = 0, []

    # 1. ATR Compression — ГЛАВНЫЙ СИГНАЛ (пружина заряжена)
    if atr_compression < 0.50:
        s4 += 38; n4.append(f"ATR_comp{atr_compression:.2f}(!)")
    elif atr_compression < 0.65:
        s4 += 22; n4.append(f"ATR_comp{atr_compression:.2f}")
    elif atr_compression < 0.80:
        s4 += 10; n4.append(f"ATR_comp{atr_compression:.2f}")

    # 2. OI Coil — накопление позиций при боковике
    if oi_coiling:
        s4 += 32; n4.append(f"OI_coil(+{oi_coil_chg:.1f}%/rng{oi_coil_rng:.1f}%)")
    elif oi_coil_chg > 2.0 and oi_coil_rng < 3.5:
        s4 += 12; n4.append(f"OI_накоп{oi_coil_chg:.1f}%")

    # 3. CVD-Price Divergence — скрытая покупка (самый ранний сигнал пампа)
    if cvd_div == "strong_bull_div":
        s4 += 30; n4.append("CVD⊕сильн!")
    elif cvd_div == "bull_div":
        s4 += 18; n4.append("CVD⊕div")
    elif cvd_div == "strong_bear_div":
        s4 -= 15  # скрытые продажи = не для лонга
    elif cvd_div == "bear_div":
        s4 -= 8

    # 4. Volume Acceleration — объём нарастает (импульс уже начался)
    if vol_accel_dir == "bull_accel":
        s4 += 26; n4.append(f"vol↑×{vol_accel_ratio:.1f}")
    elif vol_accel_dir == "bear_accel":
        s4 -= 10
    elif vol_accel_ratio > 1.3:
        s4 += 8

    # 5. Whale Activity — кит вошёл рыночным ордером
    if whale_side == "Buy":
        s4 += 22; n4.append(f"кит_BUY×{whale_mult:.0f}")
    elif whale_side == "Sell":
        s4 -= 12; n4.append(f"кит_SELL×{whale_mult:.0f}")

    # 5b. Поглощение — крупный игрок тихо набирает (раньше видно чем кит в ленте)
    if absorb_dir == "bull_absorb":
        s4 += 18; n4.append(f"ABSORB↑×{absorb_ratio:.1f}")
    elif absorb_dir == "bear_absorb":
        s4 -= 14; n4.append(f"ABSORB↓×{absorb_ratio:.1f}")

    # 6. Объём выше нормы (текущая свеча)
    if vol_ratio > 2.5:
        s4 += 18; n4.append(f"vol×{vol_ratio:.1f}")
    elif vol_ratio > 1.6:
        s4 += 10; n4.append(f"vol×{vol_ratio:.1f}")
    elif vol_ratio > 1.2:
        s4 += 4

    # 7. OI Divergence: новые деньги входят в лонг
    if oi_div == "strong_bull":
        s4 += 20; n4.append("OI+цена↑!")
    elif oi_div == "bull_div":
        s4 += 12; n4.append("OI_div↑")
    elif oi_div == "strong_bear":
        s4 -= 10
    # FIX 6: extreme OI build (>15%) = overextended setup
    if oi_change > 15:
        s4 -= 10; n4.append(f"OI_ext+{oi_change:.0f}%!")

    # 8. HTF: покупай в сторону тренда
    if trend_bull_aligned:
        s4 += 14; n4.append("D+4H↑")
    elif daily_trend == "bull" or h4_trend == "bull":
        s4 += 7; n4.append("HTF↑")

    # 9. RS vs BTC: деньги ротируются в этот актив
    if rs_btc is not None and rs_btc > 1.8:
        s4 += 18; n4.append(f"RS{rs_btc:.1f}x!")
    elif rs_btc is not None and rs_btc > 1.3:
        s4 += 10; n4.append(f"RS{rs_btc:.1f}x")
    # FIX 4: moderate RS (0-2x) = clean setup; extreme RS (>5x) = already pumped
    if rs_btc is not None and 0 < rs_btc < 2:
        s4 += 8; n4.append(f"RS{rs_btc:.2f}(mod)")
    if rs_btc is not None and rs_btc > 5:
        s4 -= 8; n4.append(f"RS{rs_btc:.1f}x(ext)")

    # 10. Funding нейтральный или отрицательный = место для роста
    if funding < -0.015:
        s4 += 14; n4.append(f"fund{funding:.3f}%(сквиз)")
    elif -0.01 <= funding <= 0.01:
        s4 += 8; n4.append("fund≈0")
    elif funding > 0.025:
        s4 -= 8  # лонги перегреты = памп уже был

    # Экстремальный funding как дополнительный катализатор
    if fund_extreme == "extreme_neg":
        s4 += 18; n4.append("FUND_EXT!")  # сквиз + памп = двойной движок
    elif fund_extreme == "high_neg":
        s4 += 10; n4.append("fund_HN")
    elif fund_extreme in ("extreme_pos", "high_pos"):
        s4 -= 12  # перегрев = высокий риск

    # 11. MTF Confluence: структурная поддержка для роста — tiered
    if bull_mtf >= 5:
        s4 += 6;  n4.append(f"MTF{bull_mtf}↑!")
    elif bull_mtf >= 4:
        s4 += 13; n4.append(f"MTF{bull_mtf}↑!")
    elif bull_mtf >= 3:
        s4 += 20; n4.append(f"MTF{bull_mtf}↑!")
    elif bull_mtf >= 2:
        s4 += 10; n4.append(f"MTF{bull_mtf}↑")
    elif bull_mtf == 1:
        s4 += 5

    # 12. Свечной паттерн подтверждает разворот вверх
    if bullish_pattern and vol_ratio > 1.4:
        s4 += 14; n4.append(f"{candle_pat}+vol!")
    elif bullish_pattern:
        s4 += 7

    # 13. Цена вырывается из диапазона (breakout в ход)
    if price_pos > 0.88:
        s4 += 16; n4.append(f"пробой{price_pos:.0%}")
    elif price_pos > 0.72:
        s4 += 8; n4.append(f"топ{price_pos:.0%}")

    # 14. Funding trend: нарастающее давление коротких
    if fund_trend == "declining":
        s4 += 12; n4.append("fund↓нараст")

    # 15. EMA Structure — бычий порядок на 1H
    # FIX 7: EMA_bull_1H in breakout = WR -4.2pp (makes signals worse); inverted to penalty
    if ema_1h.get("ema_bull"):
        s4 -= 5; n4.append("EMA_bull1H⚠")
    elif ema_1h.get("golden_cross"):
        s4 += 18; n4.append("GoldenX!")
    elif ema_1h.get("price_vs_ema20") == "above" and ema_1h.get("ema20_slope") == "rising":
        s4 += 7;  n4.append("EMA20↑")

    # 16. EMA 4H: бычий порядок на старшем ТФ (сильный фон)
    if ema_4h.get("ema_bull"):
        s4 += 10; n4.append("EMA_bull4H")
    if ema_4h.get("above_ema200"):
        s4 += 8;  n4.append("EMA200↑4H")

    # 17. VWAP: цена ниже VWAP — недооценена, потенциал роста
    if vwap_dev is not None:
        if vwap_dev < -3.0:
            s4 += 14; n4.append(f"VWAP{vwap_dev:.1f}%")
        elif vwap_dev < -1.0:
            s4 += 8;  n4.append(f"VWAP{vwap_dev:.1f}%")
        if vwap_dev < -5.0:
            s4 -= 20; n4.append("VWAP<-5%!")  # net -6: freefall not just oversold

    # 18. RSI Дивергенция бычья → ранний сигнал разворота
    if rsi_div_1h == "bull_div":
        s4 += 16; n4.append("RSI_div↑!")
    elif rsi_div_1h == "hidden_bull":
        s4 += 10; n4.append("RSI_hid↑")
    elif rsi_1h < 30:
        s4 += 12; n4.append(f"RSI{rsi_1h:.0f}(OS)")  # перепроданность

    # 19. CHoCH бычий — ранний слом нисходящего тренда
    # FIX 5: breakout CHoCH WR +15.3pp (55.6% vs 40.3%); boosted from 16 → 20
    if choch_1h == "bull_choch":
        s4 += 20; n4.append("CHoCH↑1H!")
    elif choch_4h == "bull_choch":
        s4 += 14; n4.append("CHoCH↑4H!")

    # 20. Value Area: цена ниже VAL — перепродана относительно VA
    if val_dist is not None and val_dist < -2.0:
        s4 += 10; n4.append(f"belowVAL{val_dist:.1f}%")

    # 21. Equal Highs/Lows = ликвидность
    # EQH над ценой → магниты роста (цель для пампа)
    if len(eq_highs) >= 2:
        s4 += 12; n4.append(f"EQH×{len(eq_highs)}@{format_price(eq_highs[0])}")
    elif eq_highs:
        s4 += 6;  n4.append(f"EQH@{format_price(eq_highs[0])}")
    # EQL под ценой → опасность: рынок может сначала сходить забрать ликвидность вниз
    if len(eq_lows) >= 2:
        s4 -= 10; n4.append(f"EQL×{len(eq_lows)}⚠")  # двойное дно = ликвидность манит
    elif eq_lows:
        s4 -= 4;  n4.append(f"EQL@{format_price(eq_lows[0])}")

    # 22. MTF Extended (1H+4H+1D)
    if bull_mtf_ext > bull_mtf:
        s4 += 8; n4.append(f"MTF_1D{bull_mtf_ext}")

    # 23. CHoCH медвежий = штраф (тренд меняется вниз)
    if choch_1h == "bear_choch" or choch_4h == "bear_choch":
        s4 -= 12

    # 24. Ask wall ВЫШЕ цены = памп заблокирован стеной заявок
    if len(ask_stack_sc) >= 2 and ask_wall_d <= 2.5:
        wall_pen = min(len(ask_stack_sc) * 7, 24)
        s4 -= wall_pen; n4.append(f"ask_стек⚠×{len(ask_stack_sc)}")
    # Bid wall НИЖЕ = поддержка аккумуляции
    if len(bid_stack_sc) >= 2 and bid_wall_d <= 2.5:
        s4 += min(len(bid_stack_sc) * 4, 14); n4.append(f"bid_стек↓{len(bid_stack_sc)}")

    # 25. OI velocity: ускорение = нарастающий импульс, замедление = exhaust
    if oi_velocity > 3.0:
        s4 += 14; n4.append(f"OI_accel+{oi_velocity:.1f}%")
    elif oi_velocity > 1.5:
        s4 += 7;  n4.append(f"OI_accel+{oi_velocity:.1f}%")
    elif oi_velocity < -3.0:
        s4 -= 12; n4.append(f"OI_exhaust{oi_velocity:.1f}%")
    elif oi_velocity < -1.5:
        s4 -= 6

    # 26. Perp/Spot ratio: органичный spot demand vs пузырь плеч
    if perp_spot_ratio is not None:
        if perp_spot_ratio > 5.0:
            s4 -= 16; n4.append(f"P/S={perp_spot_ratio:.1f}x пузырь!")
        elif perp_spot_ratio > 3.5:
            s4 -= 8;  n4.append(f"P/S={perp_spot_ratio:.1f}x перегрет")
        elif perp_spot_ratio < 1.5:
            s4 += 12; n4.append(f"P/S={perp_spot_ratio:.1f}x spot↑")
        elif perp_spot_ratio < 2.5:
            s4 += 6

    # 27. BTC Dominance
    if btc_dominance is not None:
        if btc_dominance < 47:
            s4 += 8;  n4.append(f"BTC.d={btc_dominance:.0f}%↓alt")
        elif btc_dominance > 52:
            s4 -= 10; n4.append(f"BTC.d={btc_dominance:.0f}%↑btc")

    # 28. Ликвидации шортов = топливо для брейкаута (сквиз поднимает вверх)
    if liq_short_usd >= 1_000_000:
        s4 += 20; n4.append(f"LIQ_short ${liq_short_usd/1e6:.1f}M!")
    elif liq_short_usd >= 300_000:
        s4 += 10; n4.append(f"LIQ_short ${liq_short_usd/1e3:.0f}K")

    # P1.3: FOMO-штраф — движение уже идёт, поздний вход (WR audit: >150 = 27.9% при 4h)
    if price_pos > 0.80 and vol_ratio > 2.0 and rs_btc is not None and rs_btc > 2.0:
        s4 -= 25; n4.append("FOMO⚠")

    # CoinGecko trending + ATR compression = хайп + пружина = pre-pump сигнал
    if _is_trending and atr_compression < 0.65:
        s4 += 12; n4.append("trending+comp🔥")

    scores["breakout"] = s4
    notes["breakout"]  = ", ".join(n4) or "—"

    # ═══════════════════════════════════════════════════════════════════════════
    # СЕТАП 5 — ДИСТРИБУЦИЯ / ШОРТ-ДАВЛЕНИЕ
    #
    # Зеркало сетапа 1: лонги перегреты → фиксация / шорт-ликвидация.
    # Условия: положительный funding + цена у вершины 48h диапазона +
    #          OI снижается + медвежьи структурные сигналы.
    # ═══════════════════════════════════════════════════════════════════════════
    s5, n5 = 0, []

    # Funding (главное топливо распродажи) — вес зависит от позиции цены
    # P1.4: полный бонус только у вершины диапазона, иначе не валидный шорт
    if funding > 0.01:
        if price_pos > 0.60:
            s5 += 35; n5.append(f"fund={funding:.3f}%")
        elif price_pos > 0.40:
            s5 += 18; n5.append(f"fund={funding:.3f}%")
        else:
            s5 += 5;  n5.append(f"fund={funding:.3f}%@low")  # фандинг+ у дна = слабый шорт
    elif funding > 0:
        if price_pos > 0.50:
            s5 += 22; n5.append(f"fund={funding:.3f}%")
        else:
            s5 += 10; n5.append(f"fund={funding:.3f}%")
    elif funding > -0.005:
        s5 += 8;  n5.append("fund≈0")

    # Экстремально положительный funding
    if fund_extreme == "extreme_pos":
        s5 += 25; n5.append("FUND_EXTREME!")
    elif fund_extreme == "high_pos":
        s5 += 14; n5.append("fund_high_pos")
    elif fund_extreme in ("extreme_neg", "high_neg"):
        s5 -= 15; n5.append("fund_neg!")  # шорты перегреты = плохо для шорта

    # Funding trend: нарастающий перегрев лонгов
    if fund_trend == "rising":
        s5 += 15; n5.append("fund↑нараст")
    elif fund_trend == "normalizing" and funding > 0.01:
        s5 -= 8;  n5.append("fund норм-ся")

    # Серия положительного funding = устойчивый перегрев лонгов
    if pos_streak >= 5:
        s5 += 12; n5.append(f"fund+×{pos_streak}!")
    elif pos_streak >= 3:
        s5 += 6;  n5.append(f"fund+×{pos_streak}")

    # Цена у сопротивления (вершина 48h диапазона)
    if price_pos > 0.85:
        s5 += 28; n5.append(f"вершина {price_pos:.0%}")
    elif price_pos > 0.70:
        s5 += 16; n5.append(f"верхн {price_pos:.0%}")
    elif price_pos > 0.60:
        s5 += 6

    # OI упал = лонги ликвидируются
    if oi_change < -10:
        s5 += 25; n5.append(f"OI{oi_change:.1f}%")
    elif oi_change < -5:
        s5 += 14; n5.append(f"OI{oi_change:.1f}%")

    # OI Divergence: лонги закрываются у хая → разворот вниз
    if oi_div == "bear_div":
        s5 += 18; n5.append("OI_div↓")
    elif oi_div == "strong_bear":
        s5 += 8;  n5.append("OI_bear")
    elif oi_div == "bull_div":
        s5 -= 10

    # Подтверждения (лонги ликвидируются — медвежий сигнал)
    if long_liq:
        s5 += 12; n5.append("лонг-лики")

    # Локальная БД: крупные лонг-ликвидации за 60мин → давление вниз
    if liq_long_usd >= 1_000_000:
        s5 += 22; n5.append(f"LIQ_long ${liq_long_usd/1e6:.1f}M!")
    elif liq_long_usd >= 300_000:
        s5 += 12; n5.append(f"LIQ_long ${liq_long_usd/1e3:.0f}K")
    elif liq_long_usd >= 100_000:
        s5 += 6

    # HTF: согласованность Daily + 4H вниз
    if trend_bear_aligned:
        s5 += 15; n5.append("D+4H↓")
    elif daily_trend == "bear" or h4_trend == "bear":
        s5 += 8;  n5.append("HTF↓")

    # MTF медвежьи зоны (КЛЮЧЕВОЙ СИГНАЛ) — tiered: fresh signal > saturated
    if bear_mtf >= 5:
        s5 += 10; n5.append(f"MTF{bear_mtf}↓!")
    elif bear_mtf >= 4:
        s5 += 20; n5.append(f"MTF{bear_mtf}↓!")
    elif bear_mtf >= 3:
        s5 += 30; n5.append(f"MTF{bear_mtf}↓!")
    elif bear_mtf >= 2:
        s5 += 18; n5.append(f"MTF{bear_mtf}↓")
    elif bear_mtf == 1:
        s5 += 10; n5.append("MTF1↓")

    # Цена В медвежьем FVG/OB прямо сейчас
    if in_bear_fvg:
        s5 += 18; n5.append("В FVG↓!")
    elif bear_fvg_1h and bear_fvg_1h[0]["dist_pct"] < 1.5:
        s5 += 10; n5.append(f"FVG↓{bear_fvg_1h[0]['dist_pct']:.1f}%")
    elif bear_fvg_1h and bear_fvg_1h[0]["dist_pct"] < 3.0:
        s5 += 5

    if in_bear_ob:
        s5 += 18; n5.append("В OB↓!")
    elif bear_ob_1h and bear_ob_1h[0]["dist_pct"] < 1.5:
        s5 += 10; n5.append(f"OB↓{bear_ob_1h[0]['dist_pct']:.1f}%")
    elif bear_ob_1h and bear_ob_1h[0]["dist_pct"] < 3.0:
        s5 += 5

    # Свечной паттерн подтверждает разворот вниз
    if bearish_pattern:
        s5 += 12; n5.append(f"{candle_pat}")
    elif candle_pat == "inside_bar":
        s5 += 6;  n5.append("inside_bar")

    # CVD медвежий
    if cvd_bear_aligned:
        s5 += 15; n5.append("CVD↓")
    elif kl_cvd_pct < -10:
        s5 += 8;  n5.append(f"CVD↓{kl_cvd_pct:.0f}%")

    # Sweep вверх перед распродажей (захват ликвидности выше перед обвалом)
    if sweep_up is not None:
        s5 += 12; n5.append(f"sweep↑{sweep_up:.4g}")

    # CHoCH медвежий = ранний слом восходящего тренда
    if choch_1h == "bear_choch":
        s5 += 14; n5.append("CHoCH↓1H!")
    elif choch_4h == "bear_choch":
        s5 += 12; n5.append("CHoCH↓4H!")

    # RSI перекупленность / медвежья дивергенция
    if rsi_1h > 70:
        s5 += 10; n5.append(f"RSI{rsi_1h:.0f}(OB)")
    if rsi_div_1h == "bear_div":
        s5 += 14; n5.append("RSI_div↓!")
    elif rsi_div_1h == "hidden_bear":
        s5 += 10; n5.append("RSI_hid↓")

    # EMA Structure — медвежий порядок
    if ema_1h.get("ema_bear"):
        s5 += 12; n5.append("EMA_bear1H")
    elif ema_1h.get("death_cross"):
        s5 += 16; n5.append("DeathX!")
    if ema_4h.get("ema_bear"):
        s5 += 10; n5.append("EMA_bear4H")

    # VWAP: цена выше VWAP — переоценена, потенциал снижения
    if vwap_dev is not None:
        if vwap_dev > 3.0:
            s5 += 14; n5.append(f"VWAP+{vwap_dev:.1f}%")
        elif vwap_dev > 1.0:
            s5 += 8;  n5.append(f"VWAP+{vwap_dev:.1f}%")

    # MTF Extended (1H+4H+1D медвежьи)
    if bear_mtf_ext > bear_mtf:
        s5 += 8; n5.append(f"MTF_1D{bear_mtf_ext}↓")

    # Штраф: бычьи сигналы говорят против шорта
    if trend_bull_aligned:
        s5 -= 12
    if cvd_bull_aligned:
        s5 -= 8

    # Стенка заявок ВЫШЕ цены = сопротивление подтверждает дистрибуцию
    if len(ask_stack_sc) >= 2 and ask_wall_d <= 2.5:
        wall_pts = min(len(ask_stack_sc) * 5, 18)
        s5 += wall_pts; n5.append(f"ask_стек×{len(ask_stack_sc)}")

    # Perp/Spot: высокое = много лонгов с плечом = топливо для слива
    if perp_spot_ratio is not None:
        if perp_spot_ratio > 5.0:
            s5 += 14; n5.append(f"P/S={perp_spot_ratio:.1f}x!")
        elif perp_spot_ratio > 3.0:
            s5 += 7

    # BTC Dominance: BTC season = давление на альты = boost шорт
    if btc_dominance is not None:
        if btc_dominance > 52:
            s5 += 8;  n5.append(f"BTC.d={btc_dominance:.0f}%↑btc")
        elif btc_dominance < 47:
            s5 -= 8

    scores["short_dist"] = s5
    notes["short_dist"]  = ", ".join(n5) or "—"

    # ── Лучший сетап ──
    best  = max(scores, key=scores.get)
    score = scores[best]

    # ── Социальный сентимент (CryptoPanic + LunarCrush) ──────────────────────
    # Применяем ДО hard-filters: +10/-10 если сигналы совпадают с направлением.
    _cp_score  = int(_cp.get("score", 0) or 0)
    _lc_sent   = _lc.get("sentiment")   # 0-100, >70 бычий, <30 медвежий
    _lc_galaxy = _lc.get("galaxy_score")

    # Лучший сетап определяем предварительно для оценки направления
    _pre_best = max(scores, key=scores.get)
    _bull_setup = _pre_best in ("squeeze", "breakout")
    _bear_setup = _pre_best in ("short_dist",)

    _social_bonus = 0
    _social_note  = ""
    if _cp_score >= 5 and (_bull_setup or not _bear_setup):
        _social_bonus += 8; _social_note += f"CP+{_cp_score}"
    elif _cp_score <= -5 and (_bear_setup or not _bull_setup):
        _social_bonus += 8; _social_note += f"CP{_cp_score}"
    elif abs(_cp_score) >= 3:
        _social_bonus += 4
    if _cp.get("hot") and _bull_setup:
        _social_bonus += 5; _social_note += "+HOT"
    if _lc_sent is not None:
        if _lc_sent >= 70 and _bull_setup:
            _social_bonus += 6; _social_note += f" LC_sent{_lc_sent:.0f}"
        elif _lc_sent <= 30 and _bear_setup:
            _social_bonus += 6; _social_note += f" LC_sent{_lc_sent:.0f}"
        elif (_lc_sent >= 70 and _bear_setup) or (_lc_sent <= 30 and _bull_setup):
            _social_bonus -= 6  # контра-сигнал
    if _lc_galaxy is not None and _lc_galaxy >= 70 and _bull_setup:
        _social_bonus += 5; _social_note += f" galaxy{_lc_galaxy:.0f}"
    if _social_bonus != 0:
        scores[_pre_best] = max(0, scores[_pre_best] + _social_bonus)

    # ── Направление best-сетапа ──
    #   squeeze/breakout → ЛОНГ; short_dist → ШОРТ;
    #   range_sweep → sweep_dir_3; bos_fvg → по HTF.
    if best == "squeeze" or best == "breakout":
        setup_dir = "long"
    elif best == "short_dist":
        setup_dir = "short"
    elif best == "range_sweep":
        setup_dir = sweep_dir_3
    else:  # bos_fvg
        setup_dir = ("long" if trend_bull_aligned else
                     "short" if trend_bear_aligned else "none")

    # ═══════════════════════════════════════════════════════════════════════════
    # HARD-SKIP ФИЛЬТРЫ (откалиброваны на 24ч данных: breakout WR 12%, squeeze 18.5%)
    # ═══════════════════════════════════════════════════════════════════════════

    # Filter 1: BTC velocity hard-skip при резком движении ±1.0%
    # Наблюдение: лонги на дампящем BTC получают в среднем MFE+4% затем разворот.
    # 4h не хватает дойти до TP, цена закрывается в минус.
    if setup_dir == "long" and btc_chg_4h <= -1.0:
        return None
    if setup_dir == "short" and btc_chg_4h >= 1.0:
        return None

    # Filter 5: BTC bull-EMA-режим → все ШОРТ-сетапы против тренда
    # Аудит 2026-04-21: при btc_ema_pos='above' стоп-рейт ШОРТ = 61% (22/36),
    # ЛОНГ = 14% (5/35). BTC выше EMA20+EMA50 на 4h = структурный аптренд,
    # alt_breadth обычно >80% — весь рынок движется вверх вслед за BTC.
    if setup_dir == "short" and btc_ema_pos == "above":
        return None

    # Filter 6: short_dist ШОРТ — только при явном медвежьем BTC-режиме
    # short_dist = самый агрессивный контртрендовый шорт (55% стоп-рейт, аудит 2026-04-21).
    # Разрешён только когда BTC явно ниже обоих EMA20+EMA50 ('below').
    # При 'between' Fix 2 даёт -30, но short_dist требует жёсткого блока.
    if best == "short_dist" and setup_dir == "short" and btc_ema_pos != "below":
        return None

    # ── Weekly Grade X hard block (TASK B) ──────────────────────────────────
    # Both Weekly and Daily oppose signal direction → Grade X, filter out.
    if weekly_trend != "unknown":
        if setup_dir == "long" and weekly_trend == "bear" and daily_trend == "bear":
            return None
        if setup_dir == "short" and weekly_trend == "bull" and daily_trend == "bull":
            return None

    # Filter 2: Breakout в strong bear regime — смерть (WR 12%, MISS 46%)
    # Пробой вверх при Daily+4H bear = false breakout в 85%+ случаев.
    if best == "breakout" and daily_trend == "bear" and h4_trend == "bear":
        return None

    # Filter 3: Squeeze в bear regime требует глубокой перепроданности
    # "Отскок от поддержки" при медвежьем HTF без price_pos<0.30 — ловушка.
    if (best == "squeeze"
        and daily_trend == "bear" and h4_trend == "bear"
        and price_pos > 0.30):
        return None

    # Filter 4: BOS/FVG лонг при медвежьем Daily — требует высокого score
    # d_htf=bear + h4_htf=bull → bos_fvg может ещё быть "long", но Daily против.
    # Без высокой убеждённости (score≥80) это контртрендовый вход.
    if (best == "bos_fvg" and setup_dir == "long"
            and daily_trend == "bear" and score < 80):
        return None

    # ── BTC-velocity soft-penalty: для умеренных движений (-1.0..-0.8%, +0.8..+1.0%) ──
    btc_penalty = 0
    if setup_dir == "long" and btc_chg_4h <= -0.8:
        btc_penalty = -10
    elif setup_dir == "short" and btc_chg_4h >= 0.8:
        btc_penalty = -10
    if btc_penalty:
        score = max(0, score + btc_penalty)
        scores[best] = score
        notes[best] = (notes[best] + f", BTC4h{btc_chg_4h:+.1f}%{btc_penalty:+d}").lstrip(", ")

    # ── Fix 2: BTC в переходном режиме (между EMA20 и EMA50) → шорты штрафуются
    # 'between' = BTC восстанавливается или в переходе; шортить против momentum рискованно.
    # -30 к score: пограничные шорты (score ~35-80) вылетят ниже min_score порога.
    if setup_dir == "short" and btc_ema_pos == "between":
        score = max(0, score - 30)
        scores[best] = score
        notes[best] = (notes[best] + ", BTCbtw-30").lstrip(", ")

    # ── Outcome-weighted multiplier: историческая WR по бакету (setup × grade) ──
    # Grade считаем по сырому score + MTF; затем применяем мультипликатор [0.5, 1.5].
    if score_weights:
        raw_grade = composite_grade({
            "score":  score,
            "mtf_b":  bull_mtf, "mtf_s":  bear_mtf,
            "bull_mtf_ext": bull_mtf_ext, "bear_mtf_ext": bear_mtf_ext,
            "d_htf":  daily_trend, "h4_htf": h4_trend,
            "cvd_k%": kl_cvd_pct,
        })
        mult = score_weights.get((best, raw_grade))
        if mult and mult != 1.0:
            score = int(round(score * mult))
            scores[best] = score
            notes[best] = (notes[best] + f", w×{mult:.2f}").lstrip(", ")

    # ── Signal-level additive adjustments from logistic regression calibration ──
    # Per-setup model takes priority; falls back to generic pooled weights.
    # CHoCH coefficient differs per setup: bos_fvg=+18.7pp, breakout=+15.3pp,
    # squeeze=-20.5pp (negative! — CHoCH in squeeze = momentum already spent).
    _sw = None
    if score_weights and setup_dir == "long":
        _sw = (score_weights.get(f"__signal_weights_{best}__")
               or score_weights.get("__signal_weights__"))
    if _sw and setup_dir == "long":
        _sw_adj = 0.0
        _sw_adj += _sw.get("choch_bull_1h",   0.0) * int(choch_1h == "bull_choch")
        _sw_adj += _sw.get("oi_falling_5",    0.0) * int(oi_change < -5)
        _sw_adj += _sw.get("rsi_lt40",        0.0) * int(rsi_1h < 40)
        _sw_adj += _sw.get("funding_neg",     0.0) * int(funding < 0)
        _sw_adj += _sw.get("oi_rising_5",     0.0) * int(oi_change > 5)
        _sw_adj += _sw.get("cvd_kline_bull",  0.0) * int(kl_cvd_pct > 15)
        _sw_adj += _sw.get("ema_bull_1h",     0.0) * int(ema_1h.get("ema_bull", False))
        _sw_adj += _sw.get("ema_bull_4h",     0.0) * int(ema_4h.get("ema_bull", False))
        _sw_adj += _sw.get("mtf_bull_ge3",    0.0) * int(bull_mtf >= 3)
        if _sw_adj != 0.0:
            score = max(0, score + int(round(_sw_adj)))
            scores[best] = score
            notes[best] = (notes[best] + f", sw{_sw_adj:+.0f}").lstrip(", ")

    # ── SHORT signal-level calibration (T1.2) ────────────────────────────────
    # Trained on ШОРТ decisive trades. Key: score>150 is anti-correlated with
    # SHORT WR (33.8% vs 47.5% baseline) — high score = overbought short setup.
    _sw_s = score_weights.get("__signal_weights_short__") if score_weights else None
    if _sw_s and setup_dir == "short":
        _sw_s_adj = 0.0
        _sw_s_adj += _sw_s.get("score_gt150", 0.0) * int(score > 150)
        _sw_s_adj += _sw_s.get("rsi_gt65",    0.0) * int(rsi_1h > 65)
        _sw_s_adj += _sw_s.get("ema_bull_1h", 0.0) * int(ema_1h.get("ema_bull", False))
        if _sw_s_adj != 0.0:
            score = max(0, score + int(round(_sw_s_adj)))
            scores[best] = score
            notes[best] = (notes[best] + f", sws{_sw_s_adj:+.0f}").lstrip(", ")

    # ── GOLDEN flag (Phase 2C): READ-ONLY overlay — no score change ──────────
    # Count how many of 9 optimal conditions are met; flag when ≥7.
    _now_utc = datetime.utcnow()
    _golden_conds = [
        best in ("bos_fvg", "squeeze"),                              # 1 setup type
        _now_utc.hour in {9, 10, 21, 22},                           # 2 optimal UTC hour
        _now_utc.weekday() in {0, 2, 3, 6},                         # 3 Mon/Wed/Thu/Sun
        bull_mtf >= 3,                                               # 4 top-tier MTF
        vwap_dev is not None and -5.0 <= vwap_dev <= -1.0,          # 5 VWAP good zone
        -0.03 <= funding <= 0.0,                                     # 6 optimal funding
        rs_btc is not None and 0 < rs_btc < 2.0,                    # 7 moderate RS_BTC
        oi_change < 5.0,                                             # 8 calm accumulation
        best != "squeeze" and choch_1h == "bull_choch",             # 9 CHoCH (not squeeze)
    ]
    golden_count = sum(_golden_conds)
    golden = golden_count >= 7

    # ── Структурные уровни для торгового плана ──────────────────────────────
    # Ближайшие FVG/OB зоны (top, bottom, dist_pct), None если зоны нет
    def _nearest(zones, ztype):
        c = [z for z in zones if z["type"] == ztype]
        if not c:
            return None, None, None
        z = min(c, key=lambda x: x["dist_pct"])
        return z["top"], z["bottom"], z["dist_pct"]

    bfvg_top, bfvg_bot, bfvg_d = _nearest(fvgs_1h, "bull")
    sfvg_top, sfvg_bot, sfvg_d = _nearest(fvgs_1h, "bear")
    bob_top,  bob_bot,  bob_d  = _nearest(obs_1h,  "bull")
    sob_top,  sob_bot,  sob_d  = _nearest(obs_1h,  "bear")

    bid_wall_px = dom["bid_wall"][0] if dom["bid_wall"] else None
    ask_wall_px = dom["ask_wall"][0] if dom["ask_wall"] else None
    bid_stack   = stacks.get("bid_stack")
    ask_stack   = stacks.get("ask_stack")

    # ── Flags: быстрый взгляд на ключевые сигналы ──
    flags = []
    if in_bull_fvg:            flags.append("FVG↑!")
    if in_bull_ob:             flags.append("OB↑!")
    if in_bear_fvg:            flags.append("FVG↓!")
    if in_bear_ob:             flags.append("OB↓!")
    if bull_mtf >= 2:          flags.append(f"MTF{bull_mtf}↑")
    if bear_mtf >= 2:          flags.append(f"MTF{bear_mtf}↓")
    if sweep_down is not None: flags.append("swp↓")
    if sweep_up   is not None: flags.append("swp↑")
    if cvd_bull_aligned:       flags.append("CVD↑")
    if cvd_bear_aligned:       flags.append("CVD↓")
    if candle_pat:             flags.append(candle_pat[:4])
    if oi_div:                 flags.append(f"OI:{oi_div[:6]}")
    if fund_trend != "stable": flags.append(f"f:{fund_trend[:4]}")
    # Pre-pump флаги
    if atr_compression < 0.65:                 flags.append(f"⊕ATR{atr_compression:.2f}")
    if oi_coiling:                             flags.append("⊕OIcoil")
    if cvd_div and "bull" in cvd_div:          flags.append("⊕CVDdiv")
    if vol_accel_dir == "bull_accel":          flags.append(f"⊕vol×{vol_accel_ratio:.1f}")
    if whale_side == "Buy":                    flags.append(f"⊕кит×{whale_mult:.0f}")
    # Завалы (stacked walls) — серия крупных лимиток одной стороны
    if bid_stack and len(bid_stack) >= 2:
        d_pct = abs(price - bid_stack[0][0]) / price * 100
        if d_pct <= 2.5:
            flags.append(f"завал↓×{len(bid_stack)}")
    if ask_stack and len(ask_stack) >= 2:
        d_pct = abs(ask_stack[0][0] - price) / price * 100
        if d_pct <= 2.5:
            flags.append(f"завал↑×{len(ask_stack)}")

    return {
        "symbol":    symbol,
        "price":     price,
        "fund_%":    funding,
        "basis_%":   basis_pct,
        "oi24h_%":   oi_change,
        "pos_%":     round(price_pos * 100, 1),
        "vol_x":     round(vol_ratio, 2),
        "d_htf":     daily_trend,
        "h4_htf":    h4_trend,
        "fvg1h":     len(fvgs_1h),
        "ob1h":      len(obs_1h),
        "mtf_b":     bull_mtf,
        "mtf_s":     bear_mtf,
        "cvd_k%":    round(kl_cvd_pct, 1),
        "cvd_t%":    round(tr_cvd_pct, 1),
        "dom_%":     round(dom["imbalance"], 1),
        "atr_%":     round(atr_pct, 2),
        "poc_%":     round(poc_dist, 1) if poc_dist is not None else None,
        "rs_btc":    round(rs_btc, 2) if rs_btc is not None else None,
        "sweep":          ("↓" if sweep_down else "") + ("↑" if sweep_up else "") or "—",
        "sweep_dir":      sweep_dir_3,
        "liq":            ("L" if long_liq else "") + ("S" if short_liq else "") or "—",
        "oi_div":         oi_div or "—",
        "fund_tr":        fund_trend,
        "pattern":        candle_pat or "—",
        "flags":          " ".join(flags) if flags else "—",
        "setup":          best,
        "score":          score,
        "notes":          notes[best],
        "golden":         golden,
        "golden_count":   golden_count,
        # Pre-pump метрики
        "atr_comp":       atr_compression,
        "oi_coil_%":      oi_coil_chg,
        "oi_coil_rng%":   oi_coil_rng,
        "oi_coiling":     oi_coiling,
        "oi_velocity":    oi_velocity,
        "perp_spot_x":    round(perp_spot_ratio, 1) if perp_spot_ratio is not None else None,
        "btc_dom_%":      round(btc_dominance, 1) if btc_dominance is not None else None,
        # ── Контекстные фичи для регрессии ──────────────────────────────────
        "btc_ema_pos":        btc_ema_pos,
        "listing_age_days":   listing_age_days,
        "avg_vol_7d_usd":     avg_vol_7d_usd,
        # Компоненты скора по каждому сетапу (сырые, до финального отбора)
        "sc_squeeze":     scores.get("squeeze", 0),
        "sc_bos_fvg":     scores.get("bos_fvg", 0),
        "sc_range_sweep": scores.get("range_sweep", 0),
        "sc_breakout":    scores.get("breakout", 0),
        "sc_short_dist":  scores.get("short_dist", 0),
        "cvd_div":        cvd_div or "—",
        "vol_accel":      vol_accel_dir or "—",
        "vol_accel_x":    vol_accel_ratio,
        "whale":          f"{whale_side}×{whale_mult:.0f}" if whale_side else "—",
        "pump_score":     s4,
        # ── Структурные уровни (для level-aware торгового плана) ──
        "lvl_bfvg":  (bfvg_top, bfvg_bot, bfvg_d),  # ближайший bull FVG
        "lvl_sfvg":  (sfvg_top, sfvg_bot, sfvg_d),  # ближайший bear FVG
        "lvl_bob":   (bob_top,  bob_bot,  bob_d),   # ближайший bull OB
        "lvl_sob":   (sob_top,  sob_bot,  sob_d),   # ближайший bear OB
        "lvl_poc":   poc_price,                      # POC price (не %)
        "lvl_hi48":  hi48,                           # 48h high (завершённые свечи)
        "lvl_lo48":  lo48,                           # 48h low
        "lvl_bid":   bid_wall_px,                    # DOM bid wall price
        "lvl_ask":   ask_wall_px,                    # DOM ask wall price
        "lvl_bid_stack": bid_stack,                  # завал ниже: [(price,size),...]
        "lvl_ask_stack": ask_stack,                  # завал выше: [(price,size),...]
        "lvl_swpdn": sweep_down,                     # sweep down price level
        "lvl_swpup": sweep_up,                       # sweep up price level
        "in_bfvg":   in_bull_fvg,
        "in_bob":    in_bull_ob,
        "in_sfvg":   in_bear_fvg,
        "in_sob":    in_bear_ob,
        # ── Новые метрики ──────────────────────────────────────────────────
        "rsi_1h":        rsi_1h,
        "rsi_div_1h":    rsi_div_1h or "—",
        "vwap":          vwap_val,
        "vwap_dev":      vwap_dev,
        "ema_1h":        ema_1h,
        "ema_4h":        ema_4h,
        "poc_full":      poc_full,
        "vah":           vah,
        "val_vp":        val_vp,
        "poc_dist_f":    poc_dist_f,
        "vah_dist":      vah_dist,
        "val_dist":      val_dist,
        "choch_1h":      choch_1h or "—",
        "choch_4h":      choch_4h or "—",
        "choch_conviction": (choch_1h == "bull_choch"),
        "eq_highs":      eq_highs,
        "eq_lows":       eq_lows,
        "bull_mtf_ext":  bull_mtf_ext,
        "bear_mtf_ext":  bear_mtf_ext,
        # Low-Volume Nodes (зоны ускорения)
        "lvn_zones":       lvn_zones,
        "lvn_nearest":     lvn_nearest,
        "lvn_near_dist_%": round(lvn_nearest_dist, 2) if lvn_nearest_dist is not None else None,
        # Полные списки зон FVG/OB (для chart_analyzer overlay)
        "fvg_1h": [{"high": z["top"], "low": z["bottom"], "type": z["type"]} for z in fvgs_1h],
        "ob_1h":  [{"high": z["top"], "low": z["bottom"], "type": z["type"]} for z in obs_1h],
        # Ликвидации (USD за 60мин) — для _conviction_score и алертов
        "liq_long_usd":  liq_long_usd,
        "liq_short_usd": liq_short_usd,
        # ── Weekly + 15m context (TASK A) ─────────────────────────────────────
        "weekly_trend":     weekly_trend,
        "weekly_pos":       weekly_ctx["weekly_pos"],
        "weekly_above_ema": weekly_ctx["weekly_above_ema"],
        "m15_trend":        m15_trend,
        "m15_momentum":     m15_ctx["m15_momentum"],
        # Социальный сентимент
        "social_note":   _social_note or "—",
        "cp_score":      _cp_score,
        "lc_sentiment":  _lc_sent,
        "lc_galaxy":     _lc_galaxy,
        "kline_1h_ts":   _kl_open_ts.get((symbol, "60"), 0.0),
    }


# ─── TRADE_LEARNINGS_DB gate (injected after score_symbol result is built) ───

# Score adjustments derived from backtested pattern WR deltas.
# Prohibited conditions (is_prohibited) zero the score to exclude from output.
# Rule penalty capped at 30 pts to avoid over-penalising overlapping conditions.
# Confirmation filter bonus capped at 16 pts (2 filters × 8 pts each).
_TLDB_PENALTY_MAP = {"HIGH": 20, "MEDIUM": 12, "LOW": 6}
_TLDB_MAX_PENALTY = 30
_TLDB_BONUS_PER_FILTER = 8
_TLDB_MAX_BONUS = 16


def _apply_tldb_gate(result: dict) -> dict:
    """Apply TLDB: penalise score for anti-patterns, block prohibited conditions,
    boost score for confirmed high-WR filter matches."""
    if not _TLDB_AVAILABLE:
        return result
    try:
        gate = _tldb.check_tldb_gate(
            result,
            prohibited=_TLDB_PROHIBITED,
            rules=_TLDB_RULES,
            filters=_TLDB_FILTERS,
        )
        result["tldb_prohibited"]    = gate["is_prohibited"]
        result["tldb_penalty_level"] = gate["penalty_level"]
        result["tldb_prohibited_ids"]= [h["id"] for h in gate["prohibited_hits"]]
        result["tldb_rule_ids"]      = [h["id"] for h in gate["rule_hits"]]
        result["tldb_filter_ids"]    = [h["id"] for h in gate.get("filter_hits", [])]

        sym        = result.get("symbol", "?")
        orig_score = int(result.get("score", 0) or 0)

        # ── Prohibited condition: zero out score ─────────────────────────────
        if gate["is_prohibited"]:
            result["score"] = 0
            ids = ", ".join(h["id"] for h in gate["prohibited_hits"])
            print(f"[TLDB] 🚫 {sym} PROHIBITED ({ids}) — score {orig_score}→0")
            return result

        current = orig_score

        # ── Score penalty from correction rules ──────────────────────────────
        if gate["rule_hits"]:
            raw_penalty = sum(
                _TLDB_PENALTY_MAP.get(h["priority"], 0)
                for h in gate["rule_hits"]
            )
            penalty = min(raw_penalty, _TLDB_MAX_PENALTY)
            current = max(0, current - penalty)
            result["score"] = current
            ids = ", ".join(h["id"] for h in gate["rule_hits"])
            print(f"[TLDB] ⬇ {sym} penalised -{penalty}pt ({ids}): {orig_score}→{current}")

        # ── Score bonus from confirmation filters ────────────────────────────
        filter_hits = gate.get("filter_hits", [])
        if filter_hits:
            bonus = min(_TLDB_BONUS_PER_FILTER * len(filter_hits), _TLDB_MAX_BONUS)
            pre   = current
            current += bonus
            result["score"] = current
            ids = ", ".join(h["id"] for h in filter_hits)
            print(f"[TLDB] ⬆ {sym} boosted +{bonus}pt ({ids}): {pre}→{current}")

    except Exception:
        result["tldb_prohibited"]    = False
        result["tldb_penalty_level"] = "NONE"
        result["tldb_prohibited_ids"]= []
        result["tldb_rule_ids"]      = []
        result["tldb_filter_ids"]    = []
    return result


# ─── Signal Interpretation ───────────────────────────────────────────────────

def interpret_signals(r):
    """
    Интерпретирует каждый сигнал из результата с рекомендацией по направлению.
    Возвращает: (signals, verdict, bull_count, bear_count)
      signals: [(метрика, значение, "ЛОНГ"/"ШОРТ"/"ЖДАТЬ"/"ИНФО", объяснение), ...]
      verdict: "ЛОНГ ↑" / "ШОРТ ↓" / "ЖДАТЬ ◆"
    """
    signals = []
    bull = 0
    bear = 0

    def add(metric, value_str, direction, explanation):
        nonlocal bull, bear
        signals.append((metric, value_str, direction, explanation))
        if direction == "ЛОНГ":
            bull += 1
        elif direction == "ШОРТ":
            bear += 1

    # ── Funding Rate ──────────────────────────────────────────────────────────
    f = r["fund_%"]
    if f < -0.02:
        add("Funding", f"{f:+.4f}%", "ЛОНГ",
            "Шорты сильно перегреты, платят лонгам → топливо для шорт-сквиза")
    elif f < -0.005:
        add("Funding", f"{f:+.4f}%", "ЛОНГ",
            "Небольшой перекос в пользу шортов → небольшой бычий уклон")
    elif f > 0.02:
        add("Funding", f"{f:+.4f}%", "ШОРТ",
            "Лонги сильно перегреты, платят шортам → рынок перекуплен, риск дампа")
    elif f > 0.005:
        add("Funding", f"{f:+.4f}%", "ШОРТ",
            "Небольшой перекос в пользу лонгов → осторожно с лонгом")
    else:
        add("Funding", f"{f:+.4f}%", "ЖДАТЬ",
            "Funding нейтральный, нет чёткого перекоса — не даёт сигнала")

    # ── Funding Trend ─────────────────────────────────────────────────────────
    ft = r["fund_tr"]
    if ft == "declining":
        add("Fund тренд", ft, "ЛОНГ",
            "Funding движется вниз (шорты нарастают) → давление сквиза усиливается")
    elif ft == "rising":
        add("Fund тренд", ft, "ШОРТ",
            "Funding движется вверх (лонги нарастают) → риск распродажи лонгов")
    elif ft == "normalizing":
        add("Fund тренд", ft, "ЖДАТЬ",
            "Экстремальный funding нормализуется → давление спадает, сетап слабеет")
    else:
        add("Fund тренд", ft, "ЖДАТЬ",
            "Funding стабилен — нет дополнительной информации о направлении")

    # ── OI Change 24h ─────────────────────────────────────────────────────────
    # Интерпретация зависит от ПОЗИЦИИ ЦЕНЫ в диапазоне:
    # OI падает у ДНА → шорты ликвидированы → ЛОНГ
    # OI падает у ВЕРШИНЫ → лонги ликвидированы → ШОРТ
    oi      = r["oi24h_%"]
    pos_pct = r["pos_%"]   # 0-100
    if oi < -10:
        if pos_pct <= 30:
            add("OI 24h", f"{oi:+.1f}%", "ЛОНГ",
                "OI сильно упал у ДНА → шорты ликвидированы, позиции расчищены → разворот вверх")
        elif pos_pct >= 70:
            add("OI 24h", f"{oi:+.1f}%", "ШОРТ",
                "OI сильно упал у ВЕРШИНЫ → лонги ликвидированы → продолжение вниз или разворот")
        else:
            add("OI 24h", f"{oi:+.1f}%", "ЖДАТЬ",
                "Сильное падение OI в середине диапазона — направление определяй по HTF и CVD")
    elif oi < -5:
        if pos_pct <= 35:
            add("OI 24h", f"{oi:+.1f}%", "ЛОНГ",
                "OI упал у поддержки → ликвидации состоялись, меньше шортового давления")
        elif pos_pct >= 65:
            add("OI 24h", f"{oi:+.1f}%", "ШОРТ",
                "OI упал у сопротивления → лонги фиксируют или их сдувают → осторожно с лонгом")
        else:
            add("OI 24h", f"{oi:+.1f}%", "ЖДАТЬ",
                "OI слегка упал в середине диапазона — нет чёткого сигнала")
    elif oi > 15:
        add("OI 24h", f"{oi:+.1f}%", "ИНФО",
            "OI сильно вырос — рынок набирает позиции. Смотри направление через HTF и CVD")
    elif oi > 8:
        add("OI 24h", f"{oi:+.1f}%", "ИНФО",
            "OI вырос умеренно — новые деньги входят, движение вероятно, но направление определяй по CVD")
    else:
        add("OI 24h", f"{oi:+.1f}%", "ЖДАТЬ",
            "OI без значимого изменения — нейтральный сигнал")

    # ── OI Divergence ─────────────────────────────────────────────────────────
    od = r["oi_div"]
    if od == "strong_bull":
        add("OI Дивергенция", od, "ЛОНГ",
            "Цена ↑ и OI ↑ одновременно — новые лонги входят, тренд здоровый → покупай")
    elif od == "bull_div":
        add("OI Дивергенция", od, "ЛОНГ",
            "Цена ↓ но OI тоже ↓ — шорты закрываются у дна → разворот вверх вероятен")
    elif od == "strong_bear":
        add("OI Дивергенция", od, "ШОРТ",
            "Цена ↓ и OI ↑ — новые шорты входят, медвежий тренд здоровый → не лонгуй")
    elif od == "bear_div":
        add("OI Дивергенция", od, "ШОРТ",
            "Цена ↑ но OI ↓ — лонги закрываются у хая → разворот вниз вероятен, осторожно с лонгом")
    else:
        add("OI Дивергенция", "—", "ЖДАТЬ",
            "Движения недостаточно для дивергенции — нейтральный сигнал")

    # ── HTF Trend ─────────────────────────────────────────────────────────────
    d_htf  = r["d_htf"]
    h4_htf = r["h4_htf"]
    htf_str = f"D={d_htf} / 4H={h4_htf}"
    if d_htf == "bull" and h4_htf == "bull":
        add("HTF (D+4H)", htf_str, "ЛОНГ",
            "Daily и 4H оба бычьи — торгуй только лонг, тренд сильный и согласованный")
    elif d_htf == "bear" and h4_htf == "bear":
        add("HTF (D+4H)", htf_str, "ШОРТ",
            "Daily и 4H оба медвежьи — торгуй только шорт, тренд сильный и согласованный")
    elif d_htf == "bull" or h4_htf == "bull":
        add("HTF (D+4H)", htf_str, "ЛОНГ",
            "Один из HTF бычий — предпочтителен лонг, но согласованности нет, осторожно")
    elif d_htf == "bear" or h4_htf == "bear":
        add("HTF (D+4H)", htf_str, "ШОРТ",
            "Один из HTF медвежий — предпочтителен шорт, но без полного подтверждения")
    else:
        add("HTF (D+4H)", htf_str, "ЖДАТЬ",
            "Оба в рейндже — рынок без тренда, не торгуй по тренду, жди пробоя")

    # ── MTF Confluence ────────────────────────────────────────────────────────
    mb = r["mtf_b"]
    ms = r["mtf_s"]
    if mb >= 3:
        add("MTF Confluence", f"{mb} bull", "ЛОНГ",
            f"{mb} пересечений бычьих зон 4H+1H — очень сильная поддержка, высокая вероятность отработки")
    elif mb >= 2:
        add("MTF Confluence", f"{mb} bull", "ЛОНГ",
            f"{mb} пересечения бычьих зон — хороший кластер поддержки, цена должна отреагировать")
    elif mb == 1:
        add("MTF Confluence", "1 bull", "ЛОНГ",
            "Одна бычья MTF зона — умеренный сигнал поддержки")
    elif ms >= 3:
        add("MTF Confluence", f"{ms} bear", "ШОРТ",
            f"{ms} пересечений медвежьих зон 4H+1H — очень сильное сопротивление")
    elif ms >= 2:
        add("MTF Confluence", f"{ms} bear", "ШОРТ",
            f"{ms} пересечения медвежьих зон — хороший кластер сопротивления")
    elif ms == 1:
        add("MTF Confluence", "1 bear", "ШОРТ",
            "Одна медвежья MTF зона — умеренный сигнал сопротивления")
    else:
        add("MTF Confluence", "нет", "ЖДАТЬ",
            "Нет совпадения зон 4H и 1H — зона не подтверждена на нескольких ТФ")

    # ── FVG / OB In Zone ──────────────────────────────────────────────────────
    flags_str = r["flags"]
    in_bull_fvg = "FVG↑!" in flags_str
    in_bull_ob  = "OB↑!"  in flags_str
    in_bear_fvg = "FVG↓!" in flags_str
    in_bear_ob  = "OB↓!"  in flags_str
    fvg_ob_str  = f"{r['fvg1h']} FVG / {r['ob1h']} OB"

    if in_bull_fvg and in_bull_ob:
        add("FVG / OB", "В FVG↑ + OB↑!", "ЛОНГ",
            "Цена ВНУТРИ бычьего FVG и OB — идеальная точка входа в лонг прямо сейчас!")
    elif in_bull_fvg:
        add("FVG / OB", "В FVG↑!", "ЛОНГ",
            "Цена ВНУТРИ бычьего FVG — входная зона по SMC/ICT, рассматривай лонг")
    elif in_bull_ob:
        add("FVG / OB", "В OB↑!", "ЛОНГ",
            "Цена ВНУТРИ бычьего Order Block — зона крупного игрока, рассматривай лонг")
    elif in_bear_fvg and in_bear_ob:
        add("FVG / OB", "В FVG↓ + OB↓!", "ШОРТ",
            "Цена ВНУТРИ медвежьего FVG и OB — идеальная точка входа в шорт прямо сейчас!")
    elif in_bear_fvg:
        add("FVG / OB", "В FVG↓!", "ШОРТ",
            "Цена ВНУТРИ медвежьего FVG — зона сопротивления SMC, рассматривай шорт")
    elif in_bear_ob:
        add("FVG / OB", "В OB↓!", "ШОРТ",
            "Цена ВНУТРИ медвежьего Order Block — зона сопротивления, рассматривай шорт")
    else:
        add("FVG / OB", fvg_ob_str, "ЖДАТЬ",
            "Цена вне зон FVG/OB — жди подхода к зоне, сейчас входить не по плану")

    # ── CVD ───────────────────────────────────────────────────────────────────
    ck = r["cvd_k%"]
    ct = r["cvd_t%"]
    cvd_str = f"kline {ck:+.0f}% / trade {ct:+.0f}%"
    if ck > 20 and ct > 10:
        add("CVD", cvd_str, "ЛОНГ",
            "Оба CVD положительны: покупатели-агрессоры доминируют → реальный бычий импульс")
    elif ck < -20 and ct < -10:
        add("CVD", cvd_str, "ШОРТ",
            "Оба CVD отрицательны: продавцы-агрессоры доминируют → реальный медвежий импульс")
    elif ck > 10:
        add("CVD", cvd_str, "ЛОНГ",
            "Kline CVD положительный (20h окно) → преобладают бычьи свечи, умеренный бычий сигнал")
    elif ck < -10:
        add("CVD", cvd_str, "ШОРТ",
            "Kline CVD отрицательный → преобладают медвежьи свечи, умеренный медвежий сигнал")
    else:
        add("CVD", cvd_str, "ЖДАТЬ",
            "CVD нейтральный — нет явного преобладания агрессоров, нет подтверждения направления")

    # ── ATR (риск, не направление) ───────────────────────────────────────────
    atr = r["atr_%"]
    if atr > 2.0:
        add("ATR 14", f"{atr:.2f}%", "ИНФО",
            f"Высокая волатильность — стоп минимум {atr*1.5:.1f}% от входа. Уменьши размер позиции!")
    elif atr > 1.0:
        add("ATR 14", f"{atr:.2f}%", "ИНФО",
            f"Нормальная волатильность — стоп ≈ {atr*1.3:.1f}% от входа. Стандартный размер")
    elif atr > 0:
        add("ATR 14", f"{atr:.2f}%", "ИНФО",
            f"Низкая волатильность / компрессия — стоп ≈ {atr*1.3:.1f}%. Возможен взрыв движения вскоре")
    else:
        add("ATR 14", "нет данных", "ИНФО", "ATR не рассчитан")

    # ── POC (Point of Control) ────────────────────────────────────────────────
    poc = r["poc_%"]
    if poc is not None:
        poc_str = f"{poc:+.1f}%"
        if 0 < poc < 3:
            add("POC", poc_str, "ЛОНГ",
                f"Цена на {poc:.1f}% выше POC — POC снизу как поддержка, бычий контекст")
        elif poc > 3:
            add("POC", poc_str, "ИНФО",
                f"Цена на {poc:.1f}% выше POC — далеко от POC, может вернуться к нему (магнит)")
        elif -3 < poc < 0:
            add("POC", poc_str, "ШОРТ",
                f"Цена на {abs(poc):.1f}% ниже POC — POC сверху как сопротивление, медвежий контекст")
        else:
            add("POC", poc_str, "ИНФО",
                f"Цена далеко ниже POC ({abs(poc):.1f}%) — сильное сопротивление, не форсируй лонг")
    else:
        add("POC", "нет данных", "ИНФО", "POC не рассчитан")

    # ── Свечной паттерн ───────────────────────────────────────────────────────
    pat = r["pattern"]
    if pat == "bull_engulf":
        add("Паттерн", pat, "ЛОНГ",
            "Бычье поглощение (engulfing) — сильный разворотный сигнал вверх на 1H")
    elif pat == "hammer":
        add("Паттерн", pat, "ЛОНГ",
            "Молот (hammer) — отбой от поддержки, покупатели защитили уровень → сигнал лонга")
    elif pat == "bear_engulf":
        add("Паттерн", pat, "ШОРТ",
            "Медвежье поглощение (engulfing) — сильный разворотный сигнал вниз на 1H")
    elif pat == "shooting_star":
        add("Паттерн", pat, "ШОРТ",
            "Падающая звезда (shooting star) — отбой от сопротивления, продавцы активны → сигнал шорта")
    elif pat == "inside_bar":
        add("Паттерн", pat, "ЖДАТЬ",
            "Inside bar (компрессия) — рынок берёт паузу, жди пробоя в любую сторону")
    else:
        add("Паттерн", "—", "ЖДАТЬ",
            "Нет чёткого свечного паттерна на 1H — нет дополнительного подтверждения")

    # ── Sweep ─────────────────────────────────────────────────────────────────
    sw = r["sweep"]
    if sw == "↓":
        add("Sweep", "sweep вниз ↓", "ЛОНГ",
            "Ложный пробой поддержки — рынок взял ликвидность снизу и вернулся → контр-тренд лонг")
    elif sw == "↑":
        add("Sweep", "sweep вверх ↑", "ШОРТ",
            "Ложный пробой сопротивления — рынок взял ликвидность сверху и вернулся → контр-тренд шорт")
    elif sw == "↓↑":
        add("Sweep", "double sweep", "ЖДАТЬ",
            "Двойной sweep в обе стороны — рынок ищет ликвидность, подожди пока определится направление")
    else:
        add("Sweep", "—", "ЖДАТЬ",
            "Нет ложного пробоя — sweep не подтверждён, нет сигнала от этого инструмента")

    # ── RS vs BTC ─────────────────────────────────────────────────────────────
    rs = r["rs_btc"]
    if rs is not None:
        rs_str = f"{rs:.2f}x"
        if rs > 1.5:
            add("RS vs BTC", rs_str, "ЛОНГ",
                f"Альт сильно опережает BTC (×{rs:.2f}) — деньги идут в эту пару, бычий сигнал")
        elif rs > 1.1:
            add("RS vs BTC", rs_str, "ЛОНГ",
                f"Альт умеренно опережает BTC (×{rs:.2f}) — относительная сила есть")
        elif rs < 0.5:
            add("RS vs BTC", rs_str, "ШОРТ",
                f"Альт сильно отстаёт от BTC (×{rs:.2f}) — слабая пара, лонг рискован")
        elif rs < 0.8:
            add("RS vs BTC", rs_str, "ШОРТ",
                f"Альт отстаёт от BTC (×{rs:.2f}) — относительная слабость, осторожно с лонгом")
        else:
            add("RS vs BTC", rs_str, "ЖДАТЬ",
                "Примерно в паре с BTC — нет относительного преимущества в ту или иную сторону")
    else:
        add("RS vs BTC", "—", "ИНФО", "Нет данных RS (BTC 24h движение слишком мало)")

    # ── DOM (стакан) ──────────────────────────────────────────────────────────
    dom = r["dom_%"]
    dom_str = f"{dom:+.0f}%"
    if dom > 30:
        add("DOM стакан", dom_str, "ЛОНГ",
            f"Сильное преобладание бидов (+{dom:.0f}%) — покупатели готовы поглотить продажи")
    elif dom > 15:
        add("DOM стакан", dom_str, "ЛОНГ",
            f"Умеренное преобладание бидов (+{dom:.0f}%) — небольшой перевес в пользу покупателей")
    elif dom < -30:
        add("DOM стакан", dom_str, "ШОРТ",
            f"Сильное преобладание асков ({dom:.0f}%) — продавцы давят, лонг рискован")
    elif dom < -15:
        add("DOM стакан", dom_str, "ШОРТ",
            f"Умеренное преобладание асков ({dom:.0f}%) — небольшой перевес у продавцов")
    else:
        add("DOM стакан", dom_str, "ЖДАТЬ",
            "DOM сбалансирован — нет чёткого дисбаланса, стакан не подтверждает направление")

    # ── ATR Compression (Pre-Pump) ────────────────────────────────────────────
    ac = r.get("atr_comp", 1.0)
    if ac < 0.50:
        add("ATR Сжатие", f"{ac:.2f}x", "ЛОНГ",
            f"Экстремальное сжатие волатильности ({ac:.2f}× от нормы) — пружина максимально "
            f"заряжена. Исторически предшествует мощному взрыву движения. "
            f"Направление определяй по CVD и OI.")
    elif ac < 0.65:
        add("ATR Сжатие", f"{ac:.2f}x", "ЛОНГ",
            f"Сильное сжатие волатильности ({ac:.2f}× от нормы) — цена аккумулируется "
            f"перед движением. Жди катализатора или объёмного пробоя.")
    elif ac < 0.80:
        add("ATR Сжатие", f"{ac:.2f}x", "ЖДАТЬ",
            f"Умеренное сжатие волатильности ({ac:.2f}×) — диапазон сужается, "
            f"но ещё не критично. Следи за нарастанием объёма.")
    else:
        add("ATR Сжатие", f"{ac:.2f}x", "ЖДАТЬ",
            f"Волатильность нормальная или расширяется ({ac:.2f}×) — сжатия нет")

    # ── OI Coil (Pre-Pump) ────────────────────────────────────────────────────
    oc  = r.get("oi_coil_%", 0.0)
    ocr = r.get("oi_coil_rng%", 0.0)
    occ = r.get("oi_coiling", False)
    if occ:
        add("OI Накопление", f"OI+{oc:.1f}% / цена {ocr:.1f}%", "ЛОНГ",
            f"OI вырос на {oc:.1f}% пока цена стояла в диапазоне {ocr:.1f}% за 12h — "
            f"классика тихого набора позиции крупным игроком. Когда стоп-ордера "
            f"за диапазоном будут забраны — цена резко пойдёт вверх.")
    elif oc > 2.0:
        add("OI Накопление", f"OI+{oc:.1f}% / цена {ocr:.1f}%", "ЖДАТЬ",
            f"OI слегка накапливается (+{oc:.1f}%), цена в диапазоне {ocr:.1f}% — "
            f"умеренный сигнал аккумуляции, пока не критичный")
    else:
        add("OI Накопление", f"OI{oc:+.1f}%", "ЖДАТЬ",
            "OI не показывает паттерна накопления — нет признаков тихого набора позиции")

    # ── CVD Divergence (Pre-Pump) ─────────────────────────────────────────────
    cd = r.get("cvd_div", "—")
    if cd == "strong_bull_div":
        add("CVD Дивергенция", "сильная ↑", "ЛОНГ",
            "СИЛЬНЫЙ PRE-PUMP СИГНАЛ: тейкеры агрессивно покупают (CVD > +30%), "
            "но цена почти не выросла за 20h. Кто-то продаёт через лимиты, поглощая "
            "покупки. Когда этот продавец уйдёт — цена взлетит без сопротивления.")
    elif cd == "bull_div":
        add("CVD Дивергенция", "бычья ↑", "ЛОНГ",
            "Покупатели агрессивно входят (CVD > +15%), но цена не реагирует. "
            "Скрытое накопление в процессе → жди выстрел вверх.")
    elif cd == "strong_bear_div":
        add("CVD Дивергенция", "сильная ↓", "ШОРТ",
            "СИГНАЛ РАСПРЕДЕЛЕНИЯ: продают агрессивно (CVD < -30%), но цена держится. "
            "Распределение перед дампом — не лонгуй.")
    elif cd == "bear_div":
        add("CVD Дивергенция", "медвежья ↓", "ШОРТ",
            "Скрытые продажи: CVD падает пока цена держится. Риск дампа.")
    else:
        add("CVD Дивергенция", "нет", "ЖДАТЬ",
            "Дивергенции CVD-цена нет — покупки и движение цены согласованы")

    # ── Volume Acceleration (Pre-Pump) ────────────────────────────────────────
    va  = r.get("vol_accel", "—")
    vax = r.get("vol_accel_x", 1.0)
    if va == "bull_accel":
        add("Разгон объёма", f"×{vax:.1f} бычий", "ЛОНГ",
            f"Объём нарастает на зелёных свечах (×{vax:.1f} к началу окна) — "
            f"бычий импульс набирается прямо сейчас. Памп уже в процессе или "
            f"вот-вот начнётся.")
    elif va == "bear_accel":
        add("Разгон объёма", f"×{vax:.1f} медвежий", "ШОРТ",
            f"Объём нарастает на красных свечах (×{vax:.1f}) — "
            f"продавцы ускоряются, не входи в лонг")
    else:
        add("Разгон объёма", f"×{vax:.1f}", "ЖДАТЬ",
            "Объём не ускоряется — импульс пока не набирается")

    # ── Whale Activity (Pre-Pump) ─────────────────────────────────────────────
    wh = r.get("whale", "—")
    if wh.startswith("Buy"):
        mult_str = wh.split("×")[1] if "×" in wh else "?"
        add("Кит в ленте", f"BUY ×{mult_str}", "ЛОНГ",
            f"Обнаружена аномально крупная покупка (×{mult_str} среднего размера) "
            f"в последних 100 сделках. Институционал или крупный трейдер вошёл "
            f"рыночным ордером — обычно предшествует движению или ускоряет его.")
    elif wh.startswith("Sell"):
        mult_str = wh.split("×")[1] if "×" in wh else "?"
        add("Кит в ленте", f"SELL ×{mult_str}", "ШОРТ",
            f"Аномально крупная продажа (×{mult_str} среднего) — крупный игрок "
            f"сбрасывает позицию. Не лонгуй против кита.")
    else:
        add("Кит в ленте", "—", "ЖДАТЬ",
            "Аномально крупных сделок нет — институционалы не торгуют рыночными ордерами")

    # ── RSI ───────────────────────────────────────────────────────────────────
    rsi = r.get("rsi_1h", 50.0)
    rsi_div = r.get("rsi_div_1h", "—")
    rsi_str = f"{rsi:.1f}"
    if rsi_div == "bull_div":
        add("RSI Дивергенция", f"{rsi_str} | bull_div", "ЛОНГ",
            "Классическая бычья дивергенция: цена сделала LL, RSI сделал HL. "
            "Один из самых надёжных сигналов разворота вверх — продавцы теряют силу.")
    elif rsi_div == "hidden_bull":
        add("RSI Дивергенция", f"{rsi_str} | hidden↑", "ЛОНГ",
            "Скрытая бычья: цена HL, RSI LL — продолжение восходящего тренда. "
            "Идеально для входа в откате.")
    elif rsi_div == "bear_div":
        add("RSI Дивергенция", f"{rsi_str} | bear_div", "ШОРТ",
            "Классическая медвежья дивергенция: цена HH, RSI LH. "
            "Покупатели теряют импульс — возможен разворот вниз.")
    elif rsi_div == "hidden_bear":
        add("RSI Дивергенция", f"{rsi_str} | hidden↓", "ШОРТ",
            "Скрытая медвежья: цена LH, RSI HH — продолжение нисходящего тренда.")
    elif rsi < 30:
        add("RSI", f"{rsi_str} (перепродан)", "ЛОНГ",
            f"RSI {rsi:.0f} — экстремальная перепроданность. Технически зона для покупки. "
            f"Сочетай с структурным уровнем для точного входа.")
    elif rsi > 70:
        add("RSI", f"{rsi_str} (перекуплен)", "ШОРТ",
            f"RSI {rsi:.0f} — перекупленность. Импульс может угасать. "
            f"Не входи в лонг при таком RSI без подтверждения.")
    else:
        add("RSI", rsi_str, "ЖДАТЬ",
            f"RSI {rsi:.0f} — нейтральная зона (30-70). Нет отдельного сигнала.")

    # ── VWAP ──────────────────────────────────────────────────────────────────
    vd = r.get("vwap_dev")
    if vd is not None:
        vd_str = f"{vd:+.2f}%"
        if vd < -4.0:
            add("VWAP отклонение", vd_str, "ЛОНГ",
                f"Цена на {abs(vd):.1f}% НИЖЕ VWAP — экстремальное отклонение. "
                f"Институционалы видят эту цену как дешёвую → высокая вероятность возврата к VWAP.")
        elif vd < -1.5:
            add("VWAP отклонение", vd_str, "ЛОНГ",
                f"Цена на {abs(vd):.1f}% ниже VWAP — перепродана относительно VWAP. "
                f"Ожидай движение обратно к VWAP как первой цели.")
        elif vd > 4.0:
            add("VWAP отклонение", vd_str, "ШОРТ",
                f"Цена на {vd:.1f}% ВЫШЕ VWAP — перекуплена. "
                f"Риск возврата к VWAP. Не гонись за ценой далеко от VWAP.")
        elif vd > 1.5:
            add("VWAP отклонение", vd_str, "ШОРТ",
                f"Цена на {vd:.1f}% выше VWAP — лёгкая перекупленность. Осторожно с лонгом.")
        else:
            add("VWAP отклонение", vd_str, "ЖДАТЬ",
                f"Цена у VWAP (±1.5%) — нейтральная зона. VWAP как пол/потолок определяется по направлению.")

    # ── EMA Structure ─────────────────────────────────────────────────────────
    e1h = r.get("ema_1h", {})
    e4h = r.get("ema_4h", {})
    if e1h.get("golden_cross"):
        add("EMA Cross 1H", "Golden Cross!", "ЛОНГ",
            "EMA20 пересекла EMA50 вверх на 1H — бычий импульс начался. "
            "Классический сигнал покупки, особенно сильный с объёмом.")
    elif e1h.get("death_cross"):
        add("EMA Cross 1H", "Death Cross!", "ШОРТ",
            "EMA20 пересекла EMA50 вниз на 1H — медвежий импульс. "
            "Не лонгуй против death cross.")
    elif e1h.get("ema_bull"):
        add("EMA порядок 1H", "price>EMA20>EMA50", "ЛОНГ",
            "Идеальный бычий порядок на 1H: цена выше обеих EMA, EMA20 выше EMA50. "
            "Торгуй только в лонг, откаты к EMA20 — точки входа.")
    elif e1h.get("ema_bear"):
        add("EMA порядок 1H", "price<EMA20<EMA50", "ШОРТ",
            "Идеальный медвежий порядок на 1H. Торгуй только в шорт.")
    elif e1h.get("price_vs_ema20") == "above" and e1h.get("ema20_slope") == "rising":
        add("EMA 1H", "цена>EMA20 ↑", "ЛОНГ",
            "Цена выше растущей EMA20 — краткосрочный бычий импульс.")
    elif e1h.get("price_vs_ema20") == "below" and e1h.get("ema20_slope") == "falling":
        add("EMA 1H", "цена<EMA20 ↓", "ШОРТ",
            "Цена ниже падающей EMA20 — краткосрочный медвежий контекст.")
    else:
        add("EMA 1H", f"vs20={e1h.get('price_vs_ema20','—')}", "ЖДАТЬ",
            "EMA структура без чёткого сигнала")

    if e4h.get("ema_bull"):
        add("EMA порядок 4H", "price>EMA20>EMA50", "ЛОНГ",
            "Бычий порядок на 4H — это подтверждение HTF тренда через EMA. Очень сильный фон.")
    elif e4h.get("above_ema200") is True:
        add("EMA200 4H", "выше EMA200", "ЛОНГ",
            "Цена выше EMA200 на 4H — долгосрочный бычий фон. "
            "Статистически большинство крупных пампов происходят выше EMA200.")
    elif e4h.get("above_ema200") is False:
        add("EMA200 4H", "ниже EMA200", "ШОРТ",
            "Цена ниже EMA200 на 4H — долгосрочный медвежий фон. "
            "Лонги против EMA200 имеют низкий winrate.")

    # ── Value Area (VAH / VAL) ────────────────────────────────────────────────
    val_d = r.get("val_dist")
    vah_d = r.get("vah_dist")
    if val_d is not None and vah_d is not None:
        if val_d < -1.0:   # цена ниже VAL
            add("Value Area", f"ниже VAL {val_d:.1f}%", "ЛОНГ",
                f"Цена на {abs(val_d):.1f}% ниже Value Area Low — перепродана. "
                f"70% объёма торговалось выше. Ожидай возврат к POC/VAH.")
        elif vah_d > 1.0:  # цена выше VAH
            add("Value Area", f"выше VAH +{vah_d:.1f}%", "ШОРТ",
                f"Цена на {vah_d:.1f}% выше Value Area High — перекуплена. "
                f"70% объёма торговалось ниже. Риск возврата в VA.")
        else:
            add("Value Area", f"в VA (POC {r.get('poc_dist_f', 0):+.1f}%)", "ЖДАТЬ",
                "Цена внутри Value Area — справедливая оценка, нет сигнала")

    # ── CHoCH ─────────────────────────────────────────────────────────────────
    cc1 = r.get("choch_1h", "—")
    cc4 = r.get("choch_4h", "—")
    if cc1 == "bull_choch":
        add("CHoCH 1H", "bull ↑!", "ЛОНГ",
            "Change of Character вверх на 1H — первый слом нисходящего тренда. "
            "Ранний сигнал: рискованнее BOS, но даёт лучший R:R при правильном входе.")
    elif cc4 == "bull_choch":
        add("CHoCH 4H", "bull ↑!", "ЛОНГ",
            "CHoCH вверх на 4H — серьёзный слом нисходящей структуры. Тренд меняется.")
    elif cc1 == "bear_choch":
        add("CHoCH 1H", "bear ↓!", "ШОРТ",
            "CHoCH вниз на 1H — первый слом восходящего тренда. Осторожно с лонгом.")
    elif cc4 == "bear_choch":
        add("CHoCH 4H", "bear ↓!", "ШОРТ",
            "CHoCH вниз на 4H — слом восходящей структуры на старшем ТФ. Избегай лонга.")
    else:
        add("CHoCH", "нет", "ЖДАТЬ",
            "Слома структуры нет — тренд продолжается без признаков разворота")

    # ── Equal Levels (ликвидность) ────────────────────────────────────────────
    eqh = r.get("eq_highs", [])
    eql = r.get("eq_lows", [])
    if eqh:
        lvls = " / ".join(format_price(x) for x in eqh[:2])
        add("Equal Highs", lvls, "ИНФО",
            f"Кластеры ликвидности ВЫШЕ цены ({lvls}) — стопы шортистов. "
            f"Рынок часто идёт забрать эти уровни → потенциальные цели роста.")
    if eql:
        lvls = " / ".join(format_price(x) for x in eql[:2])
        add("Equal Lows", lvls, "ИНФО",
            f"Кластеры ликвидности НИЖЕ цены ({lvls}) — стопы лонгистов. "
            f"Риск: цена может сходить забрать их перед ростом.")

    # ── MTF Extended (1H+4H+1D) ───────────────────────────────────────────────
    bme = r.get("bull_mtf_ext", r.get("mtf_b", 0))
    sme = r.get("bear_mtf_ext", r.get("mtf_s", 0))
    if bme > r.get("mtf_b", 0) and bme >= 2:
        add("MTF+1D", f"{bme} bull", "ЛОНГ",
            f"MTF Extended {bme} совпадений включая 1D зоны — структурный фон усилен дневным ТФ.")
    elif sme > r.get("mtf_s", 0) and sme >= 2:
        add("MTF+1D", f"{sme} bear", "ШОРТ",
            f"MTF Extended {sme} медвежьих совпадений включая 1D.")

    # ── Binance кросс-биржевые сигналы ────────────────────────────────────────
    bnb_fund = r.get("bnb_fund")
    if bnb_fund is not None:
        bybit_fund = r.get("fund_%", 0.0)
        if bybit_fund != 0 and bnb_fund != 0 and bybit_fund * bnb_fund > 0:
            if bybit_fund < -0.05 and bnb_fund < -0.05:
                add("Binance Funding", f"Bnb{bnb_fund:+.4f}%", "ЛОНГ",
                    "Оба рынка (Bybit+Binance) с сильным отрицательным funding → "
                    "двойное топливо для шорт-сквиза. Наивысшее подтверждение сигнала.")
            elif bybit_fund > 0.05 and bnb_fund > 0.05:
                add("Binance Funding", f"Bnb{bnb_fund:+.4f}%", "ШОРТ",
                    "Оба рынка перегреты лонгами → двойной риск дампа.")
            else:
                add("Binance Funding", f"Bnb{bnb_fund:+.4f}%", "ИНФО",
                    "Funding на Binance подтверждает направление Bybit → сигнал надёжнее.")
        elif bnb_fund is not None:
            add("Binance Funding", f"Bnb{bnb_fund:+.4f}%", "ЖДАТЬ",
                "Binance funding расходится с Bybit → кросс-подтверждения нет, осторожно.")

    # ── Binance ордербук / тейкер давление (из get_binance_enrichment) ────────
    book_imb = r.get("book_imbalance")
    if book_imb is not None:
        if book_imb > 0.25:
            add("Binance Ордербук", f"{book_imb:+.3f}", "ЛОНГ",
                f"Бидов значительно больше ({book_imb*100:.0f}% перевес) — "
                f"покупатели стоят плотно в стакане Binance.")
        elif book_imb < -0.25:
            add("Binance Ордербук", f"{book_imb:+.3f}", "ШОРТ",
                f"Офферов значительно больше ({abs(book_imb)*100:.0f}% перевес) — "
                f"продавцы доминируют в стакане Binance.")
        else:
            add("Binance Ордербук", f"{book_imb:+.3f}", "ЖДАТЬ",
                "Стакан Binance сбалансирован — нет чёткого давления с одной стороны.")

    taker_ratio = r.get("taker_buy_sell_ratio")
    if taker_ratio is not None:
        if taker_ratio > 1.4:
            add("Binance Тейкер", f"×{taker_ratio:.2f}", "ЛОНГ",
                f"Тейкеры на Binance агрессивно покупают (ratio={taker_ratio:.2f}) — "
                f"рыночный спрос превышает предложение.")
        elif taker_ratio < 0.7:
            add("Binance Тейкер", f"×{taker_ratio:.2f}", "ШОРТ",
                f"Тейкеры на Binance агрессивно продают (ratio={taker_ratio:.2f}) — "
                f"рыночное давление вниз.")
        else:
            add("Binance Тейкер", f"×{taker_ratio:.2f}", "ЖДАТЬ",
                "Тейкер давление нейтральное — покупки и продажи примерно равны.")

    top_ls = r.get("top_ls_ratio")
    if top_ls is not None:
        if top_ls < 0.7:
            add("Binance Топ-трейдеры", f"L/S={top_ls:.2f}", "ЛОНГ",
                f"Топ-трейдеры Binance в основном в шорт (ratio={top_ls:.2f}<1) → "
                f"при росте цены их будут давить → потенциальный сквиз.")
        elif top_ls > 1.5:
            add("Binance Топ-трейдеры", f"L/S={top_ls:.2f}", "ШОРТ",
                f"Топ-трейдеры перегружены лонгами (ratio={top_ls:.2f}) → риск лонг-сквиза вниз.")
        else:
            add("Binance Топ-трейдеры", f"L/S={top_ls:.2f}", "ЖДАТЬ",
                "Позиционирование топ-трейдеров нейтральное.")

    # ── Итог ─────────────────────────────────────────────────────────────────
    if bull > bear * 1.6:
        verdict = "ЛОНГ ↑"
    elif bear > bull * 1.6:
        verdict = "ШОРТ ↓"
    elif bull > bear:
        verdict = "ЛОНГ (слабый) ↗"
    elif bear > bull:
        verdict = "ШОРТ (слабый) ↘"
    else:
        verdict = "ЖДАТЬ ◆"

    return signals, verdict, bull, bear


def average(values):
    return sum(values) / len(values) if values else 0.0


def format_price(price):
    if price >= 1000:
        return f"{price:.2f}"
    if price >= 100:
        return f"{price:.3f}".rstrip("0").rstrip(".")
    if price >= 1:
        return f"{price:.4f}".rstrip("0").rstrip(".")
    if price >= 0.1:
        return f"{price:.5f}".rstrip("0").rstrip(".")
    return f"{price:.8f}".rstrip("0").rstrip(".")


def score_grade(score):
    if score >= 100:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 60:
        return "B"
    if score >= 45:
        return "C"
    return "D"


def composite_grade(r):
    """
    Composite Grade A+/A/B+/B/C/D — качество сетапа с учётом MTF, структуры и CVD.

    A+: score ≥ 90 + MTF_ext ≥ 2 + trend согласован + CVD подтверждает
    A:  score ≥ 80 + MTF ≥ 2  ИЛИ  score ≥ 90
    B+: score ≥ 70 + MTF ≥ 1
    B:  score ≥ 55
    C:  score ≥ 35
    D:  < 35
    """
    score   = r["score"]
    mtf     = max(r.get("bull_mtf_ext", r.get("mtf_b", 0)),
                  r.get("bear_mtf_ext", r.get("mtf_s", 0)))
    aligned = (r["d_htf"] != "range" and r["h4_htf"] != "range"
               and r["d_htf"] == r["h4_htf"])
    cvd_ok  = abs(r.get("cvd_k%", 0)) > 15

    if score >= 90 and mtf >= 2 and aligned and cvd_ok:
        return "A+"
    if score >= 80 and mtf >= 2:
        return "A"
    if score >= 90:
        return "A"
    if score >= 70 and mtf >= 1:
        return "B+"
    if score >= 55:
        return "B"
    if score >= 35:
        return "C"
    return "D"


def compute_weekly_context(op1w, hi1w, lo1w, cl1w, price):
    """
    Weekly TF context: trend direction and price position.
    Returns dict with weekly_trend ('bull'|'bear'|'range'|'unknown'),
    weekly_pos (0..1), weekly_above_ema (bool), weekly_ema10 (float).
    """
    if len(cl1w) < 4:
        return {"weekly_trend": "unknown", "weekly_pos": 0.5,
                "weekly_above_ema": None, "weekly_ema10": None}
    # Exclude last candle (may be incomplete)
    cls = cl1w[:-1]
    his = hi1w[:-1]
    los = lo1w[:-1]
    # 10-week EMA
    period = min(10, len(cls))
    ema10 = sum(cls[-period:]) / period
    k = 2 / (period + 1)
    for c in cls[-period:]:
        ema10 = c * k + ema10 * (1 - k)
    weekly_above_ema = price > ema10
    # Trend: 3 consecutive completed weeks
    trend_up = len(cls) >= 3 and cls[-1] > cls[-2] and cls[-2] > cls[-3]
    trend_dn = len(cls) >= 3 and cls[-1] < cls[-2] and cls[-2] < cls[-3]
    if trend_up and weekly_above_ema:
        weekly_trend = "bull"
    elif trend_dn and not weekly_above_ema:
        weekly_trend = "bear"
    else:
        weekly_trend = "range"
    # Price position in last 4-week range
    n = min(4, len(his))
    week_hi = max(his[-n:])
    week_lo = min(los[-n:])
    rng = week_hi - week_lo
    weekly_pos = (price - week_lo) / rng if rng > 0 else 0.5
    return {
        "weekly_trend":     weekly_trend,
        "weekly_pos":       round(max(0.0, min(1.0, weekly_pos)), 3),
        "weekly_above_ema": weekly_above_ema,
        "weekly_ema10":     round(ema10, 6),
    }


def compute_15m_context(op15m, hi15m, lo15m, cl15m, price):
    """
    15m TF context: micro-trend and EMA momentum for entry precision.
    Returns dict with m15_trend ('bull'|'bear'|'neutral'),
    m15_last_close (float), m15_momentum (float %).
    """
    if len(cl15m) < 5:
        return {"m15_trend": "neutral", "m15_last_close": price, "m15_momentum": 0.0}
    cls = cl15m[:-1]  # completed candles
    # Micro-trend: 3 consecutive completed candles
    trend_up = len(cls) >= 3 and cls[-1] > cls[-2] and cls[-2] > cls[-3]
    trend_dn = len(cls) >= 3 and cls[-1] < cls[-2] and cls[-2] < cls[-3]
    # 20-bar EMA
    period = min(20, len(cls))
    ema20 = sum(cls[-period:]) / period
    k = 2 / (period + 1)
    for c in cls[-period:]:
        ema20 = c * k + ema20 * (1 - k)
    m15_momentum = (price - ema20) / ema20 * 100 if ema20 > 0 else 0.0
    m15_trend = "bull" if trend_up else ("bear" if trend_dn else "neutral")
    return {
        "m15_trend":      m15_trend,
        "m15_last_close": cls[-1],
        "m15_momentum":   round(m15_momentum, 2),
    }


def calc_mtf_grade(r, setup_dir="long"):
    """
    Full MTF grade A+/A/B+/B/C/D with weekly hard-block Grade X.

    Grade X:  Weekly + Daily both oppose signal direction.
    Grade A+: score ≥ 90 + MTF ≥ 2 + aligned + CVD directional + weekly aligned
              + rs_btc ≥ 0 + not squeeze with score > 150 + not short.
    Grade A:  score ≥ 80 + MTF ≥ 2  OR  score ≥ 90, subject to rs_btc / squeeze guards.
    Grade B+: score ≥ 70 + MTF ≥ 1.
    Grade B:  score ≥ 55.
    Grade C:  score ≥ 35.
    Grade D:  < 35.

    Data-driven fixes (AVEVA-54, 2026-04-27):
    - CVD must confirm direction (positive for LONG, negative for SHORT);
      using abs() was awarding A+ to falling-knife longs with heavy sell CVD.
    - squeeze + score > 150 capped at B+: WR inverts above 150 for squeeze
      (38% at 160–179, 33% at 180–199) because high score = squeeze already done.
    - rs_btc < 0 capped at B+: A-grade signals averaged rs_btc = -2.76
      vs +2.62 for ungraded — grader was rewarding BTC underperformers.
    - Shorts excluded from A+: 24.1% WR on short A-signals.
    """
    score        = r["score"]
    setup        = r.get("setup", "")
    mtf          = max(r.get("bull_mtf_ext", r.get("mtf_b", 0)),
                       r.get("bear_mtf_ext", r.get("mtf_s", 0)))
    aligned      = (r.get("d_htf") != "range" and r.get("h4_htf") != "range"
                    and r.get("d_htf") == r.get("h4_htf"))
    cvd          = r.get("cvd_k%", 0) or 0
    weekly_trend = r.get("weekly_trend", "unknown")
    daily_trend  = r.get("d_htf", "range")
    rs_btc       = r.get("rs_btc")

    # Grade X: both senior TFs oppose signal
    if weekly_trend != "unknown":
        if setup_dir == "long"  and weekly_trend == "bear" and daily_trend == "bear":
            return "X"
        if setup_dir == "short" and weekly_trend == "bull" and daily_trend == "bull":
            return "X"

    # ── Data-driven guards that cap grade at B+ ──────────────────────────────
    # 1. squeeze inverted correlation above score 150
    squeeze_overheat = (setup == "squeeze" and score > 150)
    # 2. coin underperforming BTC → not A-quality long
    rs_weak = (rs_btc is not None and rs_btc < 0)
    # Hard cap: squeeze overheat or underperformer → max B+
    hard_cap_bplus = squeeze_overheat or rs_weak

    # Weekly alignment for A+
    weekly_aligned = (
        (setup_dir == "long"  and weekly_trend == "bull") or
        (setup_dir == "short" and weekly_trend == "bear") or
        weekly_trend == "unknown"
    )
    # CVD must confirm direction (was abs() — fixed to be directional)
    cvd_confirms = (
        (setup_dir == "long"  and cvd >  15) or
        (setup_dir == "short" and cvd < -15)
    )

    # A+ excluded for shorts (24.1% WR) and hard-capped setups
    aplus_eligible = (setup_dir == "long") and not hard_cap_bplus

    if aplus_eligible and score >= 90 and mtf >= 2 and aligned and cvd_confirms and weekly_aligned:
        return "A+"

    if not hard_cap_bplus:
        if score >= 80 and mtf >= 2:
            return "A"
        if score >= 90:
            return "A"

    if score >= 70 and mtf >= 1:
        return "B+"
    if score >= 55:
        return "B"
    if score >= 35:
        return "C"
    return "D"


def get_session_info():
    """
    Текущая торговая сессия по UTC.

    Азия:     01-09 UTC  — тихий рейндж, редкие пробои
    Лондон:   07-16 UTC  — нарастает активность
    НьюЙорк:  13-22 UTC  — максимальный объём
    Overlap:  13-16 UTC  — самое горячее время (NY+London)

    Эмпирика:
    Sweep рейнджа Азии:  07-09 UTC → Сетап 3
    Ликвидационный сквиз/Pump: 13-16 UTC → Сетапы 1, 4
    Рейндж-трейдинг: 01-07 UTC → Сетап 3
    """
    from datetime import timezone
    now   = datetime.now(timezone.utc)
    hour  = now.hour
    sessions = []
    if 1 <= hour < 9:   sessions.append("Азия")
    if 7 <= hour < 16:  sessions.append("Лондон")
    if 13 <= hour < 22: sessions.append("НьюЙорк")
    if not sessions:    sessions = ["офф-часы"]

    best_for = []
    if 7 <= hour < 9:   best_for.append("SWEEP(Lon.Open)")
    if 13 <= hour < 16: best_for.append("SQZ+PUMP(Overlap)")
    if 1 <= hour < 7:   best_for.append("RANGE(Asia)")
    if 16 <= hour < 22: best_for.append("PUMP(NY)")

    return {
        "session":        "+".join(sessions),
        "hour_utc":       hour,
        "best_for":       best_for,
        "is_high_volume": 13 <= hour < 16,
        "is_london_open": 7  <= hour < 10,
        "is_asian_range": 1  <= hour < 7,
    }


SECTOR_MAP = {
    "L1":     ["SOLUSDT","AVAXUSDT","SUIUSDT","APTUSDT","NEARUSDT","ATOMUSDT","DOTUSDT","TONUSDT"],
    "DeFi":   ["AAVEUSDT","UNIUSDT","CRVUSDT","LDOUSDT","MKRUSDT","SNXUSDT","JUPUSDT","COMPUSDT"],
    "AI":     ["FETUSDT","RENDERUSDT","TAOUSDT","AGIXUSDT","OCEANUSDT","WLDUSDT","AIUSDT"],
    "Meme":   ["DOGEUSDT","SHIBUSDT","PEPEUSDT","FLOKIUSDT","BONKUSDT","WIFUSDT","MEWUSDT"],
    "L2":     ["ARBUSDT","OPUSDT","MATICUSDT","STRKUSDT","METISUSDT","ZKUSDT"],
    "RWA":    ["ONDOUSDT","POLUSDT","CFGUSDT"],
    "GameFi": ["AXSUSDT","SANDUSDT","MANAUSDT","IMXUSDT","GALAUSDT"],
    "ETH":    ["ETHUSDT","STETHUSDT"],
    "BTC":    ["BTCUSDT"],
}


def classify_sector(symbol):
    for sector, symbols in SECTOR_MAP.items():
        if symbol in symbols:
            return sector
    return "Other"


def print_sector_rotation(results):
    """
    Ротация секторов: куда идут деньги прямо сейчас.
    Агрегирует RS vs BTC, pump_score и score по секторам.
    """
    buckets = {}
    for r in results:
        s = classify_sector(r["symbol"])
        buckets.setdefault(s, []).append(r)

    stats = []
    for sec, rows in buckets.items():
        if sec == "Other":
            continue
        rs_vals = [r["rs_btc"] for r in rows if r.get("rs_btc") is not None]
        avg_rs   = sum(rs_vals) / len(rs_vals) if rs_vals else 0.0
        avg_sc   = sum(r["score"] for r in rows) / len(rows)
        avg_pump = sum(r.get("pump_score", 0) for r in rows) / len(rows)
        top      = max(rows, key=lambda x: x.get("pump_score", 0) + x["score"])
        stats.append({
            "sec": sec, "n": len(rows),
            "rs": avg_rs, "sc": avg_sc, "pump": avg_pump,
            "top": top["symbol"], "top_sc": top["score"],
            "top_pump": top.get("pump_score", 0),
        })

    if not stats:
        return
    stats.sort(key=lambda x: x["rs"], reverse=True)

    print("\n" + "═" * 72)
    print("  🔄 РОТАЦИЯ СЕКТОРОВ — куда идут деньги")
    print("═" * 72)
    print(f"  {'Сектор':<9} {'Пар':>3}  {'RS vs BTC':>10}  {'Score':>6}  {'Pump':>5}  Лидер")
    print(f"  {'─'*66}")
    for s in stats[:8]:
        bar    = "▓" * min(int(max(s["rs"], 0) * 4), 10)
        rs_str = f"{s['rs']:+.2f}×"
        print(f"  {s['sec']:<9} {s['n']:>3}  {rs_str:>10}  {bar:<10}  "
              f"{s['sc']:>5.1f}  {s['pump']:>4.0f}   {s['top']}")

    hot  = max(stats, key=lambda x: x["rs"])
    cold = min(stats, key=lambda x: x["rs"])
    if hot["rs"] > 1.15:
        print(f"\n  ★ Горячий:  {hot['sec']}  (RS={hot['rs']:+.2f}×)  "
              f"Лидер: {hot['top']}  Score={hot['top_sc']}  Pump={hot['top_pump']}")
    if cold["rs"] < 0.80:
        print(f"  ↓ Слабый:   {cold['sec']}  (RS={cold['rs']:+.2f}×) — деньги уходят")


def classify_risk(r):
    atr = r["atr_%"]
    if atr >= 6 or r["score"] < 45:
        risk = "Высокий"
    elif atr >= 3 or r["score"] < 70:
        risk = "Средний"
    else:
        risk = "Низкий"

    if r["setup"] == "range_sweep" and risk == "Низкий":
        return "Средний"
    return risk


def build_trade_plan(r):
    """
    Level-aware торговый план.

    Приоритет точки входа (ЛОНГ):
      1. Цена ВНУТРИ bull FVG или OB  → вход в зоне, стоп ниже дна зоны
      2. Bull FVG / OB ≤ 2.5% ниже   → ожидаем откат в зону
      3. POC ≤ 3% ниже цены           → POC как поддержка
      4. Sweep вниз (ложный пробой)   → вход у sweep уровня
      5. DOM bid wall ≤ 2% ниже       → институциональная поддержка
      6. ATR fallback                 → нет структурного уровня рядом

    Тейк-профит:
      TP1 = ближайшее сопротивление выше (bear FVG / OB / POC)
      TP2 = 48h high (ЛОНГ) / 48h low (ШОРТ), fallback ATR×2.5
    """
    _, verdict, bull, bear = interpret_signals(r)

    if verdict.startswith("ЛОНГ"):
        side = "long"
    elif verdict.startswith("ШОРТ"):
        side = "short"
    else:
        side = "wait"

    # Resolve setup-verdict conflict: high-confidence setups override the verdict.
    # A score-95 squeeze with ШОРТ verdict should never build a short plan.
    setup = r.get("setup", "")
    score_val = r.get("score", 0)
    if score_val >= 60:
        if setup in ("squeeze", "breakout"):
            side = "long"
        elif setup == "short_dist":
            side = "short"
        elif setup == "range_sweep":
            sweep_dir = r.get("sweep_dir")
            if sweep_dir == "long":
                side = "long"
            elif sweep_dir == "short":
                side = "short"

    price   = r["price"]
    atr_pct = r["atr_%"] if r["atr_%"] and r["atr_%"] > 0 else 1.0
    atr_abs = price * atr_pct / 100
    # Буфер за уровень: ATR×0.65 — даёт пространство для liquidity sweep
    # (crypto часто делает шип на 0.4-0.8 ATR выше/ниже уровня перед разворотом)
    buf = max(atr_abs * 0.65, price * 0.002)

    # ── Структурные уровни ────────────────────────────────────────────────────
    bfvg_top, bfvg_bot, bfvg_d = r.get("lvl_bfvg") or (None, None, None)
    sfvg_top, sfvg_bot, sfvg_d = r.get("lvl_sfvg") or (None, None, None)
    bob_top,  bob_bot,  bob_d  = r.get("lvl_bob")  or (None, None, None)
    sob_top,  sob_bot,  sob_d  = r.get("lvl_sob")  or (None, None, None)
    poc      = r.get("lvl_poc")
    hi48     = r.get("lvl_hi48")
    lo48     = r.get("lvl_lo48")
    bid_wall = r.get("lvl_bid")
    ask_wall = r.get("lvl_ask")
    bid_stack = r.get("lvl_bid_stack") or []
    ask_stack = r.get("lvl_ask_stack") or []
    swp_dn   = r.get("lvl_swpdn")
    swp_up   = r.get("lvl_swpup")
    in_bfvg  = r.get("in_bfvg", False)
    in_bob   = r.get("in_bob",  False)
    in_sfvg  = r.get("in_sfvg", False)
    in_sob   = r.get("in_sob",  False)

    def _near(lvl, pct=2.5):
        return lvl is not None and abs(lvl - price) / price * 100 <= pct

    entry_low = entry_high = stop = tp1 = tp2 = price
    entry_note = stop_note = tp1_note = tp2_note = ""
    rr = 0.0

    if side == "long":

        # ── Шаг 1: точка входа + стоп ────────────────────────────────────
        if in_bfvg and bfvg_bot is not None:
            # Цена УЖЕ внутри зоны → вход по рынку сейчас, стоп ниже дна зоны
            entry_low  = price
            entry_high = price
            stop       = max(bfvg_bot - buf, 0)
            entry_note = f"СЕЙЧАС {format_price(price)} (FVG↑ {format_price(bfvg_bot)}..{format_price(bfvg_top)})"
            stop_note  = f"↓FVG↑ дно  {format_price(bfvg_bot)}"

        elif in_bob and bob_bot is not None:
            # Цена УЖЕ внутри OB → вход по рынку сейчас, стоп ниже дна OB
            entry_low  = price
            entry_high = price
            stop       = max(bob_bot - buf, 0)
            entry_note = f"СЕЙЧАС {format_price(price)} (OB↑ {format_price(bob_bot)}..{format_price(bob_top)})"
            stop_note  = f"↓OB↑ дно   {format_price(bob_bot)}"

        elif bfvg_bot is not None and bfvg_d is not None and bfvg_d < 2.5:
            entry_low  = bfvg_bot
            entry_high = bfvg_top or price
            stop       = max(bfvg_bot - buf, 0)
            entry_note = f"откат FVG↑ {format_price(bfvg_bot)} .. {format_price(bfvg_top)}"
            stop_note  = f"↓FVG↑       {format_price(bfvg_bot)}"

        elif bob_bot is not None and bob_d is not None and bob_d < 2.5:
            entry_low  = bob_bot
            entry_high = bob_top or price
            stop       = max(bob_bot - buf, 0)
            entry_note = f"откат OB↑  {format_price(bob_bot)} .. {format_price(bob_top)}"
            stop_note  = f"↓OB↑        {format_price(bob_bot)}"

        elif poc is not None and poc < price and _near(poc, 3.0):
            entry_low  = poc
            entry_high = price
            stop       = max(poc - buf, 0)
            entry_note = f"у POC      {format_price(poc)}"
            stop_note  = f"↓POC        {format_price(poc)}"

        elif swp_dn is not None and _near(swp_dn, 4.0):
            entry_low  = swp_dn
            entry_high = price
            stop       = max(swp_dn - buf, 0)
            entry_note = f"sweep↓     {format_price(swp_dn)}"
            stop_note  = f"↓sweep      {format_price(swp_dn)}"

        elif (len(bid_stack) >= 2
              and bid_stack[0][0] < price
              and (price - bid_stack[0][0]) / price * 100 <= 2.5):
            # Завал ниже цены: вход за 0.2-0.5% до первой стены, стоп за 2-ю пачку.
            # Логика PDF: если первую съели, 2-3-4-5 уходят с вероятностью 90%.
            first_p  = bid_stack[0][0]
            second_p = bid_stack[1][0]
            entry_low  = first_p * 1.002
            entry_high = min(first_p * 1.005, price)
            stop       = max(second_p - buf, 0)
            entry_note = f"завал↓ ×{len(bid_stack)} {format_price(first_p)}"
            stop_note  = f"↓завал 2-я {format_price(second_p)}"

        elif bid_wall is not None and bid_wall < price and _near(bid_wall, 2.0):
            # bid wall строго НИЖЕ цены = реальная поддержка покупателей
            entry_low  = bid_wall
            entry_high = price
            stop       = max(bid_wall - buf, 0)
            entry_note = f"bid wall   {format_price(bid_wall)}"
            stop_note  = f"↓bid wall   {format_price(bid_wall)}"

        else:
            entry_low  = max(price - atr_abs * 0.35, 0)
            entry_high = price
            stop       = max(price - atr_abs * 1.8, 0)
            entry_note = f"текущий    {format_price(price)}"
            stop_note  = f"↓ATR×1.8    {format_price(stop)}"

        # ── Шаг 2: TP1 = ближайшее сопротивление выше ────────────────────
        tp1_opts = []
        if sfvg_bot is not None and sfvg_bot > entry_high:
            tp1_opts.append((sfvg_bot, f"FVG↓ дно  {format_price(sfvg_bot)}"))
        if sob_bot is not None and sob_bot > entry_high:
            tp1_opts.append((sob_bot,  f"OB↓ дно   {format_price(sob_bot)}"))
        if poc is not None and poc > entry_high:
            tp1_opts.append((poc,      f"POC       {format_price(poc)}"))

        if tp1_opts:
            tp1, tp1_note = min(tp1_opts, key=lambda x: x[0])
        else:
            tp1 = price + atr_abs * 2.7   # R:R = 2.7/1.8 = 1.5
            tp1_note = f"ATR×2.7   {format_price(tp1)}"

        # ── Шаг 3: TP2 = 48h high или дальняя цель ───────────────────────
        if hi48 is not None and hi48 > tp1:
            tp2 = hi48
            tp2_note = f"48h high  {format_price(hi48)}"
        else:
            tp2 = price + atr_abs * 4.0
            tp2_note = f"ATR×4.0   {format_price(tp2)}"

        # Sanity-check: stop должен быть НИЖЕ entry для лонга
        if stop >= entry_low:
            stop = max(entry_low - buf * 1.5, price * 0.001)
        rr_raw = (tp1 - entry_high) / max(entry_high - stop, price * 1e-4)
        rr = round(max(min(rr_raw, 20.0), 0.0), 2)  # clamp [0, 20]
        invalidation = f"закрытие ниже {format_price(stop)}  ({stop_note.strip()})"

    elif side == "short":

        # ── Шаг 1: точка входа + стоп ────────────────────────────────────
        if in_sfvg and sfvg_top is not None:
            # Цена УЖЕ внутри зоны → вход по рынку сейчас, стоп выше крыши зоны
            entry_low  = price
            entry_high = price
            stop       = sfvg_top + buf
            entry_note = f"СЕЙЧАС {format_price(price)} (FVG↓ {format_price(sfvg_bot)}..{format_price(sfvg_top)})"
            stop_note  = f"↑FVG↓ крыша {format_price(sfvg_top)}"

        elif in_sob and sob_top is not None:
            # Цена УЖЕ внутри OB → вход по рынку сейчас, стоп выше крыши OB
            entry_low  = price
            entry_high = price
            stop       = sob_top + buf
            entry_note = f"СЕЙЧАС {format_price(price)} (OB↓ {format_price(sob_bot)}..{format_price(sob_top)})"
            stop_note  = f"↑OB↓ крыша  {format_price(sob_top)}"

        elif sfvg_top is not None and sfvg_d is not None and sfvg_d < 2.5:
            entry_low  = sfvg_bot or price
            entry_high = sfvg_top
            stop       = sfvg_top + buf
            entry_note = f"ретест FVG↓ {format_price(sfvg_bot)} .. {format_price(sfvg_top)}"
            stop_note  = f"↑FVG↓        {format_price(sfvg_top)}"

        elif sob_top is not None and sob_d is not None and sob_d < 2.5:
            entry_low  = sob_bot or price
            entry_high = sob_top
            stop       = sob_top + buf
            entry_note = f"ретест OB↓  {format_price(sob_bot)} .. {format_price(sob_top)}"
            stop_note  = f"↑OB↓         {format_price(sob_top)}"

        elif poc is not None and poc > price and _near(poc, 3.0):
            entry_low  = price
            entry_high = poc
            stop       = poc + buf
            entry_note = f"у POC      {format_price(poc)}"
            stop_note  = f"↑POC        {format_price(poc)}"

        elif swp_up is not None and _near(swp_up, 4.0):
            entry_low  = price
            entry_high = swp_up
            stop       = swp_up + buf
            entry_note = f"sweep↑     {format_price(swp_up)}"
            stop_note  = f"↑sweep      {format_price(swp_up)}"

        elif (len(ask_stack) >= 2
              and ask_stack[0][0] > price
              and (ask_stack[0][0] - price) / price * 100 <= 2.5):
            # Завал выше цены: вход за 0.2-0.5% до первой стены, стоп за 2-ю пачку.
            first_p  = ask_stack[0][0]
            second_p = ask_stack[1][0]
            entry_low  = max(first_p * 0.995, price)
            entry_high = first_p * 0.998
            stop       = second_p + buf
            entry_note = f"завал↑ ×{len(ask_stack)} {format_price(first_p)}"
            stop_note  = f"↑завал 2-я {format_price(second_p)}"

        elif ask_wall is not None and ask_wall > price and _near(ask_wall, 2.0):
            # ask wall строго ВЫШЕ цены = реальное сопротивление
            entry_low  = price
            entry_high = ask_wall
            stop       = ask_wall + buf
            entry_note = f"ask wall   {format_price(ask_wall)}"
            stop_note  = f"↑ask wall   {format_price(ask_wall)}"

        else:
            entry_low  = price
            entry_high = price + atr_abs * 0.35
            stop       = price + atr_abs * 1.8
            entry_note = f"текущий    {format_price(price)}"
            stop_note  = f"↑ATR×1.8    {format_price(stop)}"

        # ── Шаг 2: TP1 = ближайшая поддержка ниже ────────────────────────
        tp1_opts = []
        if bfvg_top is not None and bfvg_top < entry_low:
            tp1_opts.append((bfvg_top, f"FVG↑ крыша {format_price(bfvg_top)}"))
        if bob_top is not None and bob_top < entry_low:
            tp1_opts.append((bob_top,  f"OB↑ крыша  {format_price(bob_top)}"))
        if poc is not None and poc < entry_low:
            tp1_opts.append((poc,      f"POC        {format_price(poc)}"))

        if tp1_opts:
            tp1, tp1_note = max(tp1_opts, key=lambda x: x[0])  # ближайшая снизу
        else:
            tp1 = max(price - atr_abs * 2.7, 0)   # R:R = 2.7/1.8 = 1.5
            tp1_note = f"ATR×2.7   {format_price(tp1)}"

        # ── Шаг 3: TP2 = 48h low ─────────────────────────────────────────
        if lo48 is not None and lo48 < tp1:
            tp2 = lo48
            tp2_note = f"48h low   {format_price(lo48)}"
        else:
            tp2 = max(price - atr_abs * 4.0, 0)
            tp2_note = f"ATR×4.0   {format_price(tp2)}"

        # TASK D: short_dist edge fades after 4H (WR 44.6% → 38.5% at 24H).
        # Target TP1 only — do not extend to 48h low.
        if setup == "short_dist":
            tp2      = tp1
            tp2_note = f"TP1 (4H-edge) {format_price(tp1)}"

        # Sanity-check: stop должен быть ВЫШЕ entry для шорта
        if stop <= entry_high:
            stop = entry_high + buf * 1.5
        rr_raw = (entry_low - tp1) / max(stop - entry_high, price * 1e-4)
        rr = round(max(min(rr_raw, 20.0), 0.0), 2)
        invalidation = f"закрытие выше {format_price(stop)}  ({stop_note.strip()})"

    else:
        entry_note  = "нет чистого преимущества, ждём подтверждения"
        stop_note   = tp1_note = tp2_note = ""
        invalidation = "нет чистого преимущества, лучше дождаться подтверждения"

    edge = abs(bull - bear)
    if r["score"] >= 90 or edge >= 6:
        conviction = "Высокая"
    elif r["score"] >= 65 or edge >= 3:
        conviction = "Средняя"
    else:
        conviction = "Низкая"

    # LVN на пути до TP — цена проскочит быстро, считай магнитом движения
    lvn_note = ""
    lvn_nearest = r.get("lvn_nearest")
    if lvn_nearest and side in ("long", "short"):
        lo_lvn, hi_lvn = lvn_nearest
        if side == "long" and lo_lvn > price and lo_lvn < tp2:
            lvn_note = f"LVN {format_price(lo_lvn)}..{format_price(hi_lvn)} (ускорение вверх)"
        elif side == "short" and hi_lvn < price and hi_lvn > tp2:
            lvn_note = f"LVN {format_price(lo_lvn)}..{format_price(hi_lvn)} (ускорение вниз)"

    return {
        "side":        side,
        "verdict":     verdict,
        "bull":        bull,
        "bear":        bear,
        "conviction":  conviction,
        "risk":        classify_risk(r),
        "entry_low":   entry_low,
        "entry_high":  entry_high,
        "entry_note":  entry_note,
        "stop":        stop,
        "stop_note":   stop_note,
        "tp1":         tp1,
        "tp1_note":    tp1_note,
        "tp2":         tp2,
        "tp2_note":    tp2_note,
        "rr":          rr,
        "low_rr":      (side in ("long", "short") and 0 < rr < 2.0),
        "invalidation": invalidation,
        "lvn_note":    lvn_note,
    }


def split_trade_watchlists(rows):
    longs, shorts, waits = [], [], []
    for r in rows:
        plan = build_trade_plan(r)
        item = (r, plan)
        if plan["side"] == "long":
            longs.append(item)
        elif plan["side"] == "short":
            shorts.append(item)
        else:
            waits.append(item)
    return longs, shorts, waits


def _print_external_context():
    """
    Выводит блок с внешними бесплатными источниками:
      - BTC/ETH spot ETF потоки (Farside, днём раньше)
      - Макро-окно: ближайшее High-impact US событие и окно опасности
      - Deribit: Max Pain, Put/Call Ratio по ближайшей экспирации
    Любой источник может быть None — тогда строка пропускается.
    """
    print()

    # ETF потоки
    try:
        etf = _fd.get_etf_flows()
    except Exception:
        etf = {}
    lines = []
    for sym in ("btc", "eth"):
        row = etf.get(sym)
        if not row:
            continue
        flow = row["net_flow_m"]
        arrow = "▲" if flow > 0 else ("▼" if flow < 0 else "·")
        lines.append(f"{sym.upper()} {arrow}{flow:+.0f}M$")
    if lines:
        print(f"  ETF потоки ({etf.get('btc', {}).get('date', '?')}): "
              + "  |  ".join(lines))

    # Макро-календарь
    try:
        events = _fd.get_macro_calendar(impact_min="High", countries=("USD",))
        window  = _fd.next_macro_window(minutes_before=60, minutes_after=30)
    except Exception:
        events, window = [], None
    if window:
        mu = window["minutes_until"]
        when = (f"через {mu:.0f} мин" if mu > 0 else f"{-mu:.0f} мин назад")
        print(f"  ⚠ Макро-окно: {window['title']} ({when})  "
              f"— не входить за 30 мин до релиза")
    elif events:
        nxt = events[0]
        try:
            dt = datetime.fromisoformat(nxt["date"])
            from datetime import timezone as _tz
            now = datetime.now(_tz.utc)
            hours = (dt - now).total_seconds() / 3600
            if 0 < hours <= 72:
                print(f"  Макро ближайшее: {nxt['title']}  "
                      f"(через {hours:.1f}ч, f:{nxt['forecast'] or '—'} "
                      f"p:{nxt['previous'] or '—'})")
        except Exception:
            pass

    # Deribit опционы
    for cur in ("BTC", "ETH"):
        try:
            opt = _fd.get_options_context(cur)
        except Exception:
            opt = None
        if not opt or opt.get("max_pain") is None:
            continue
        px = opt.get("index_price") or 0
        mp = opt["max_pain"]
        pcr = opt.get("pcr")
        diff_pct = ((mp - px) / px * 100) if px else 0
        pcr_str = (f"PCR {pcr}" if pcr is not None else "PCR —")
        hint = ("бычий" if pcr and pcr < 0.7 else
                "медвежий" if pcr and pcr > 1.2 else
                "нейтральный")
        print(f"  Opt {cur} [{opt['expiry']}]: MaxPain {mp:.0f} "
              f"({diff_pct:+.1f}% к цене)  |  {pcr_str} → {hint}")

    # CoinGecko trending — хайп / нарративы
    try:
        trend = _fd.get_trending(limit_coins=7, limit_categories=3)
    except Exception:
        trend = {}
    coins = trend.get("coins", [])
    if coins:
        names = "  ".join(f"{c['symbol']}" for c in coins)
        print(f"  🔥 Trending: {names}")
    cats = trend.get("categories", [])
    if cats:
        top = "  |  ".join(
            f"{c['name']} {c['change_1h_pct']:+.1f}%"
            for c in cats[:3] if c.get("name")
        )
        if top:
            print(f"  Секторы (1h): {top}")


def print_market_snapshot(results, filtered, btc_chg_24h,
                          session_info=None, fg_value=None, fg_label=None):
    longs, shorts, waits = split_trade_watchlists(filtered)
    by_setup = {key: sum(1 for r in filtered if r["setup"] == key) for key in SETUP_LABELS}

    pump_candidates = sorted(
        [r for r in results if r.get("pump_score", 0) >= 40],
        key=lambda x: x.get("pump_score", 0), reverse=True,
    )[:3]

    print("\n" + "═" * 72)
    print("  MARKET SNAPSHOT")
    print("═" * 72)

    # Fear & Greed
    if fg_value is not None:
        fg_color = ("Экстремальный страх 🔴" if fg_value <= 25 else
                    "Страх ↓"               if fg_value <= 45 else
                    "Нейтрально"            if fg_value <= 55 else
                    "Жадность ↑"            if fg_value <= 75 else
                    "Экстремальная жадность 🔥")
        print(f"  Fear & Greed: {fg_value}/100  [{fg_label}]  → {fg_color}")

    # Session
    if session_info:
        sess   = session_info["session"]
        bf_str = "  ".join(session_info["best_for"]) or "—"
        print(f"  Сессия: {sess}  (UTC {session_info['hour_utc']:02d}:xx)  "
              f"Лучшее время для: {bf_str}")

    print(f"  BTC 24h: {btc_chg_24h:+.2f}%  |  "
          f"Кандидатов: {len(filtered)}/{len(results)}  |  "
          f"Средний score: {average([r['score'] for r in filtered]):.1f}")
    print(f"  Направление: LONG {len(longs)}  |  SHORT {len(shorts)}  |  WAIT {len(waits)}")
    print(f"  Funding avg: {average([r['fund_%'] for r in filtered]):+.3f}%  |  "
          f"OI 24h avg: {average([r['oi24h_%'] for r in filtered]):+.1f}%")
    print(f"  Сетапы: SQZ {by_setup['squeeze']}  |  BOS/FVG {by_setup['bos_fvg']}  |  "
          f"SWEEP {by_setup['range_sweep']}  |  PUMP {by_setup.get('breakout', 0)}")
    leader = filtered[0]
    print(f"  Лидер: {leader['symbol']}  |  "
          f"grade {composite_grade(leader)}  |  "
          f"{SETUP_SHORT[leader['setup']]}  |  score {leader['score']}")

    # ─── Внешний контекст: ETF потоки, макро, опционы ─────────────────────
    if _FD_AVAILABLE:
        _print_external_context()

    if pump_candidates:
        print(f"\n  ⊕ PRE-PUMP РАДАР (pump_score ≥ 40):")
        for r in pump_candidates:
            ps = r.get("pump_score", 0)
            flags_short = []
            if r.get("oi_coiling"):                        flags_short.append("OIcoil")
            if r.get("atr_comp", 1.0) < 0.65:             flags_short.append(f"ATR{r['atr_comp']:.2f}")
            if r.get("cvd_div", "—") != "—":              flags_short.append(r["cvd_div"][:8])
            if r.get("vol_accel", "—") != "—":            flags_short.append(r["vol_accel"][:8])
            if r.get("whale", "—") != "—":                flags_short.append(r["whale"][:10])
            if r.get("rsi_div_1h", "—") not in ("—", None): flags_short.append(f"RSI:{r['rsi_div_1h'][:7]}")
            if r.get("ema_1h", {}).get("golden_cross"):   flags_short.append("GoldenX!")
            if r.get("choch_1h", "—") == "bull_choch":    flags_short.append("CHoCH↑")
            print(f"    {r['symbol']:<14}  pump_score={ps:<4}  grade={composite_grade(r):<3}  "
                  f"{' | '.join(flags_short)}")


def print_directional_watchlists(filtered, limit=5):
    if limit <= 0:
        return

    longs, shorts, _ = split_trade_watchlists(filtered)
    sections = [
        ("WATCHLIST LONG", longs),
        ("WATCHLIST SHORT", shorts),
    ]

    for title, items in sections:
        if not items:
            continue
        print(f"\n{title}")
        table = []
        for r, plan in items[:limit]:
            # Краткий тип уровня (≤6 символов)
            def _lvl_short(note):
                n = (note or "").strip()
                if   n.startswith("в FVG↑"):     return "FVG↑"
                elif n.startswith("в FVG↓"):     return "FVG↓"
                elif n.startswith("в OB↑"):      return "OB↑"
                elif n.startswith("в OB↓"):      return "OB↓"
                elif n.startswith("откат FVG↑"): return "FVG↑~"
                elif n.startswith("откат FVG↓"): return "FVG↓~"
                elif n.startswith("откат OB↑"):  return "OB↑~"
                elif n.startswith("откат OB↓"):  return "OB↓~"
                elif n.startswith("ретест FVG↓"): return "rFVG↓"
                elif n.startswith("ретест OB↓"):  return "rOB↓"
                elif n.startswith("у POC"):       return "POC"
                elif n.startswith("sweep"):       return "swp"
                elif n.startswith("bid wall"):    return "wall"
                elif n.startswith("ask wall"):    return "wall"
                elif n.startswith("завал"):       return "завал"
                elif n.startswith("↓завал"):      return "завал"
                elif n.startswith("↑завал"):      return "завал"
                else:                             return "ATR"

            e_type  = _lvl_short(plan["entry_note"])
            sl_type = _lvl_short(plan["stop_note"])

            entry_str = (f"{e_type} "
                         f"{format_price(plan['entry_low'])}"
                         + (f"..{format_price(plan['entry_high'])}"
                            if plan["entry_low"] != plan["entry_high"] else ""))
            stop_str  = f"{sl_type} {format_price(plan['stop'])}"

            table.append([
                r["symbol"],
                SETUP_SHORT.get(r["setup"], r["setup"]),
                plan["verdict"],
                entry_str,
                stop_str,
                format_price(plan["tp1"]),
                format_price(plan["tp2"]),
                (f"⚠{plan['rr']:.1f}" if plan.get("low_rr") else f"{plan['rr']:.1f}"),
                plan["risk"],
                plan["conviction"],
                r["score"],
            ])
        print(tabulate(
            table,
            headers=["Пара", "Сетап", "Вердикт", "Entry (уровень)", "Stop (уровень)",
                     "TP1", "TP2", "R:R", "Риск", "Сила", "Score"],
            tablefmt="rounded_outline",
            numalign="right",
        ))


def build_pump_narrative(r):
    """
    Строит список активных pre-pump сигналов для детальной сводки пампов.

    Сигналы основаны на анализе реальных пампов (тип SIREN +293% за 7 дней):
    ATR-сжатие + OI-накопление + CVD-дивергенция + кит в ленте + funding.

    Возвращает: (conviction, stars, signals_list, trigger_hint)
    """
    ps = r.get("pump_score", 0)
    signals = []

    # ATR Compression
    ac = r.get("atr_comp", 1.0)
    if ac < 0.50:
        signals.append(("ATR сжатие",
                        f"{ac:.2f}×",
                        "Волатильность сжата до минимума — пружина максимально заряжена"))
    elif ac < 0.65:
        signals.append(("ATR сжатие",
                        f"{ac:.2f}×",
                        "Сильное сжатие волатильности — компрессия перед взрывом"))
    elif ac < 0.80:
        signals.append(("ATR сжатие",
                        f"{ac:.2f}×",
                        "Умеренное сжатие — диапазон сужается, следи за объёмом"))

    # OI Coil (накопление позиций при боковике)
    oc  = r.get("oi_coil_%", 0.0)
    ocr = r.get("oi_coil_rng%", 0.0)
    occ = r.get("oi_coiling", False)
    if occ:
        signals.append(("OI накопление",
                        f"+{oc:.1f}%/рнж{ocr:.1f}%",
                        f"OI +{oc:.1f}% пока цена стоит {ocr:.1f}% — тихий набор позиции крупным"))
    elif oc > 2.0 and ocr < 4.0:
        signals.append(("OI рост",
                        f"+{oc:.1f}%",
                        "OI растёт при боковике — умеренное накопление"))

    # CVD Divergence (скрытая покупка — самый ранний pre-pump сигнал)
    cd = r.get("cvd_div", "—")
    if cd == "strong_bull_div":
        signals.append(("CVD дивергенция",
                        "сильная ↑",
                        "Тейкеры агрессивно покупают, цена стоит — кто-то продаёт лимитами. Когда уйдёт — взлётит"))
    elif cd == "bull_div":
        signals.append(("CVD дивергенция",
                        "бычья ↑",
                        "Покупают активно, цена не реагирует — скрытая аккумуляция в процессе"))

    # Volume Acceleration (импульс уже начался)
    va  = r.get("vol_accel", "—")
    vax = r.get("vol_accel_x", 1.0)
    if va == "bull_accel":
        signals.append(("Разгон объёма",
                        f"×{vax:.1f} ↑",
                        f"Объём нарастает на зелёных свечах (×{vax:.1f}) — памп уже в процессе"))

    # Whale Activity
    wh = r.get("whale", "—")
    if wh.startswith("Buy"):
        mx = wh.split("×")[1] if "×" in wh else "?"
        signals.append(("Кит в ленте",
                        f"BUY ×{mx}",
                        f"Аномально крупная покупка ×{mx} среднего размера — институционал вошёл"))

    # Funding
    f = r["fund_%"]
    if f < -0.015:
        signals.append(("Funding",
                        f"{f:+.3f}%",
                        "Шорты сильно перегреты — любой рост >1% триггернёт каскад ликвидаций"))
    elif f < -0.005:
        signals.append(("Funding",
                        f"{f:+.3f}%",
                        "Funding отрицательный — небольшой перевес в пользу роста"))
    elif -0.005 <= f <= 0.005:
        signals.append(("Funding",
                        f"{f:+.3f}%",
                        "Funding нейтральный — рынок не перегрет, есть пространство для роста"))

    # Funding trend (нарастающее давление шортов)
    ft = r["fund_tr"]
    if ft == "declining":
        signals.append(("Fund тренд",
                        "↓ нарастает",
                        "Funding движется в минус — давление шортов нарастает (топливо для сквиза)"))

    # OI Divergence
    od = r["oi_div"]
    if od == "strong_bull":
        signals.append(("OI + цена",
                        "оба ↑",
                        "Цена и OI одновременно растут — новые лонги входят, тренд здоровый"))
    elif od == "bull_div":
        signals.append(("OI div",
                        "шорты закрыв",
                        "OI падает при падении цены — шорты закрываются у дна, разворот вверх"))

    # HTF alignment
    d_htf  = r["d_htf"]
    h4_htf = r["h4_htf"]
    if d_htf == "bull" and h4_htf == "bull":
        signals.append(("HTF тренд",
                        "D+4H ↑",
                        "Daily и 4H оба бычьи — торгуй только в лонг, тренд благоприятен"))
    elif d_htf == "bull" or h4_htf == "bull":
        signals.append(("HTF тренд",
                        f"D={d_htf}/4H={h4_htf}",
                        "Один из HTF бычий — есть общее направление вверх"))

    # RS vs BTC (деньги ротируются)
    rs = r["rs_btc"]
    if rs is not None and rs > 1.8:
        signals.append(("RS vs BTC",
                        f"{rs:.2f}×",
                        f"Сильно опережает BTC в {rs:.2f}× — деньги ротируются именно в эту пару"))
    elif rs is not None and rs > 1.3:
        signals.append(("RS vs BTC",
                        f"{rs:.2f}×",
                        "Опережает BTC — относительная сила есть, интерес к паре выше среднего"))

    # Цена у дна (максимальный потенциал восстановления)
    pos = r["pos_%"]
    if pos < 20:
        signals.append(("Поз в диапазоне",
                        f"{pos:.0f}%",
                        "Цена у дна 48h диапазона — максимальный потенциал восстановления"))
    elif pos > 80:
        signals.append(("Пробой диапазона",
                        f"{pos:.0f}%",
                        "Цена у верхней границы 48h — возможен пробой с продолжением вверх"))

    # Volume spike
    vol = r["vol_x"]
    if vol > 2.5:
        signals.append(("Объём",
                        f"×{vol:.1f}",
                        f"Объём {vol:.1f}× выше нормы — аномальная активность на паре"))
    elif vol > 1.6:
        signals.append(("Объём",
                        f"×{vol:.1f}",
                        "Объём заметно выше нормы — интерес к паре нарастает"))

    # MTF Confluence (структурная поддержка)
    mb = r["mtf_b"]
    if mb >= 2:
        signals.append(("MTF зоны",
                        f"{mb} bull",
                        f"{mb} совпадения бычьих зон 4H+1H — сильная структурная поддержка снизу"))

    # Свечной паттерн
    pat = r.get("pattern", "—")
    if pat in ("bull_engulf", "hammer") and (r.get("oi_coiling") or cd in ("strong_bull_div", "bull_div")):
        signals.append(("Паттерн свечи",
                        pat,
                        "Разворотный паттерн на фоне накопления — возможное начало движения"))

    # Conviction
    if ps >= 80:
        conviction, stars = "ВЫСОКАЯ",  "★★★★★"
    elif ps >= 60:
        conviction, stars = "СРЕДНЯЯ+", "★★★★☆"
    elif ps >= 45:
        conviction, stars = "СРЕДНЯЯ",  "★★★☆☆"
    else:
        conviction, stars = "НИЗКАЯ",   "★★☆☆☆"

    # Trigger hint: что нужно увидеть для подтверждения
    trigger_parts = []
    if ac < 0.65:
        trigger_parts.append("объёмный пробой из сжатия")
    if occ or (oc > 2.0 and ocr < 4.0):
        trigger_parts.append("выход цены за диапазон накопления с объёмом")
    if cd in ("strong_bull_div", "bull_div"):
        trigger_parts.append("резкий объёмный импульс на CVD-дивергенции")
    if f < -0.01:
        trigger_parts.append("рост >1-2% запустит каскад ликвидаций шортов")
    if wh.startswith("Buy"):
        trigger_parts.append("продолжение закупки крупного игрока")
    if va == "bull_accel":
        trigger_parts.append("нарастание объёма на зелёных свечах")
    if not trigger_parts:
        trigger_parts.append("нарастание объёма + закрытие выше ключевого уровня")
    trigger = " / ".join(trigger_parts[:2])

    return conviction, stars, signals, trigger


def print_pump_forecast(results, min_pump_score=30, max_show=8):
    """
    Детальная сводка: токены с признаками накопления и pre-pump активности.

    Вызывается в конце вывода run_screener — отдельный блок внизу,
    чтобы сразу видеть ЧТО накапливается и ПОЧЕМУ может произойти памп.

    Логика отбора: pump_score ≥ min_pump_score из ВСЕХ проанализированных
    токенов (не только filtered по общему score), т.к. pre-pump токен
    может иметь низкий overall score но высокий pump_score.
    """
    candidates = sorted(
        [r for r in results if r.get("pump_score", 0) >= min_pump_score],
        key=lambda x: x.get("pump_score", 0),
        reverse=True,
    )[:max_show]

    if not candidates:
        return

    print("\n" + "═" * 72)
    print("  ⚡ ПАМПЫ — ВОЗМОЖНЫЕ ПОКУПКИ")
    print("  Накопление / сжатие / pre-pump активность")
    print("  (pump_score = сумма pre-pump сигналов: ATR сжатие, OI coil, CVD div,")
    print("   разгон объёма, кит в ленте, funding, HTF тренд, RS vs BTC и др.)")
    print("═" * 72)

    for r in candidates:
        conviction, stars, signals, trigger = build_pump_narrative(r)
        ps     = r.get("pump_score", 0)
        rs_str = f"{r['rs_btc']:.2f}×" if r["rs_btc"] is not None else "—"
        ac_str = f"{r.get('atr_comp', 1.0):.2f}×"

        print(f"\n  {'─'*68}")
        print(f"  {r['symbol']:<14}  pump_score={ps:<4}  {stars}  {conviction}")
        print(f"  {'─'*68}")

        if signals:
            print("  Сигналы:")
            for label, value, desc in signals:
                # Форматируем строку с переносом если слишком длинная
                line = f"    ✓ {label:<20} {value:<14}  {desc}"
                if len(line) <= 76:
                    print(line)
                else:
                    print(f"    ✓ {label:<20} {value:<14}  {desc[:42]}")
                    print(f"      {'':20} {'':14}  {desc[42:]}")
        else:
            print("  Сигналы: технические паттерны без явного pre-pump нарратива")

        print(f"\n  Контекст:  D={r['d_htf']}/4H={r['h4_htf']}  "
              f"Fund={r['fund_%']:+.3f}%  "
              f"Поз={r['pos_%']:.0f}%  "
              f"RS={rs_str}  "
              f"ATR={ac_str}  "
              f"Score={r['score']}")
        print(f"  Сетап:     {SETUP_LABELS.get(r['setup'], r['setup'])}")
        print(f"  Триггер:   {trigger}")

    print(f"\n  {'─'*68}")
    print(f"  Показано {len(candidates)} токенов с pump_score ≥ {min_pump_score}")
    print(f"  {'─'*68}")


def default_export_path(ext, export_dir="snapshots"):
    os.makedirs(export_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(export_dir, f"bybit_screener_{stamp}.{ext}")


def normalize_export_path(path, ext):
    if not path:
        return None
    if path == "auto":
        return default_export_path(ext)
    if not path.lower().endswith(f".{ext}"):
        path = f"{path}.{ext}"
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    return path


def export_results(rows, json_path=None, csv_path=None):
    if not rows:
        return

    export_rows = []
    for r in rows:
        plan = build_trade_plan(r)
        export_rows.append({
            "symbol": r["symbol"],
            "setup": r["setup"],
            "setup_label": SETUP_LABELS.get(r["setup"], r["setup"]),
            "score": r["score"],
            "grade": score_grade(r["score"]),
            "verdict": plan["verdict"],
            "side": plan["side"],
            "conviction": plan["conviction"],
            "risk": plan["risk"],
            "price": r["price"],
            "entry_low": plan["entry_low"],
            "entry_high": plan["entry_high"],
            "stop": plan["stop"],
            "tp1": plan["tp1"],
            "tp2": plan["tp2"],
            "risk_reward": round(plan["rr"], 2),
            "fund_pct": r["fund_%"],
            "basis_pct": r["basis_%"],
            "oi24h_pct": r["oi24h_%"],
            "pos_pct": r["pos_%"],
            "vol_x": r["vol_x"],
            "daily_htf": r["d_htf"],
            "h4_htf": r["h4_htf"],
            "cvd_k_pct": r["cvd_k%"],
            "cvd_t_pct": r["cvd_t%"],
            "dom_pct": r["dom_%"],
            "atr_pct": r["atr_%"],
            "poc_pct": r["poc_%"],
            "rs_btc": r["rs_btc"],
            "sweep": r["sweep"],
            "liq": r["liq"],
            "flags": r["flags"],
            "notes": r["notes"],
            # Торговый план с уровнями
            "entry_note": plan.get("entry_note", ""),
            "stop_note":  plan.get("stop_note",  ""),
            "tp1_note":   plan.get("tp1_note",   ""),
            "tp2_note":   plan.get("tp2_note",   ""),
            "invalidation": plan["invalidation"],
            # Pre-pump
            "atr_compression": r.get("atr_comp"),
            "oi_coil_pct": r.get("oi_coil_%"),
            "oi_coil_rng_pct": r.get("oi_coil_rng%"),
            "oi_coiling": r.get("oi_coiling"),
            "cvd_divergence": r.get("cvd_div"),
            "vol_accel": r.get("vol_accel"),
            "vol_accel_x": r.get("vol_accel_x"),
            "whale": r.get("whale"),
            "pump_score": r.get("pump_score"),
        })

    if json_path:
        json_path = normalize_export_path(json_path, "json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(export_rows, f, ensure_ascii=False, indent=2)
        print(f"\nСохранён JSON snapshot: {json_path}")

    if csv_path:
        csv_path = normalize_export_path(csv_path, "csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(export_rows[0].keys()))
            writer.writeheader()
            writer.writerows(export_rows)
        print(f"Сохранён CSV snapshot: {csv_path}")


# ─── Main ────────────────────────────────────────────────────────────────────

def _fetch_symbol_data_parallel(sym: str) -> dict:
    """
    Параллельная загрузка всех 8 источников данных для одного символа.
    Использует мини-пул из 8 потоков — каждый запрос независим.
    При ошибке конкретного запроса возвращает None/[] для него (не падает весь воркер).
    Ускорение: 8 серийных запросов (~1.2s) → 8 параллельных (~0.3s).
    """
    tasks = {
        "oi":     lambda: fetch_oi_history(sym, limit=50),
        "k1h":    lambda: fetch_klines(sym, "60",  212),
        "k4h":    lambda: fetch_klines(sym, "240", 212),
        "kD":     lambda: fetch_klines(sym, "D",    52),
        "k1w":    lambda: fetch_klines(sym, "W",    52),   # TTL 24h — weekly candles
        "k15m":   lambda: fetch_klines(sym, "15",   96),   # TTL 3 min
        "fund":   lambda: fetch_funding_history(sym, limit=8),
        "ls":     lambda: fetch_ls_ratio(sym),
        "trades": lambda: fetch_recent_trades(sym, 1000),
        "book":   lambda: fetch_orderbook(sym, 50),
    }
    _empty_klines = ([], [], [], [], [])
    defaults = {
        "oi": [], "k1h": _empty_klines, "k4h": _empty_klines,
        "kD": _empty_klines, "k1w": _empty_klines, "k15m": _empty_klines,
        "fund": [], "ls": None,
        "trades": [], "book": ([], []),
    }
    out = dict(defaults)
    with ThreadPoolExecutor(max_workers=10) as _pool:
        fmap = {_pool.submit(fn): name for name, fn in tasks.items()}
        for fut in as_completed(fmap):
            name = fmap[fut]
            try:
                out[name] = fut.result()
            except Exception:
                pass  # defaults already set
    return out


def _fetch_and_score(sym, tickers, btc_chg_24h, bnb_map=None,
                     liq_stats=None, btc_chg_4h=0.0, score_weights=None,
                     global_ctx=None):
    """
    Воркер для параллельного выполнения.
    Делает все 8 API-запросов для одного символа и возвращает scored row.
    bnb_map: кросс-биржевые данные Binance {sym: {funding, oi_change, ...}} (опционально).
    liq_stats: ликвидации из local DB {sym: {long_usd, short_usd, total_usd}}.
    btc_chg_4h: 4h% BTC для velocity-фильтра.
    score_weights: {(setup, grade): multiplier} из outcome CSV.
    global_ctx: макро-данные {spot_vol: dict, btc_dominance: float}.
    """
    _d = _fetch_symbol_data_parallel(sym)
    oi_hist                        = _d["oi"]
    op1h, hi1h, lo1h, cl1h, vol1h = _d["k1h"]
    op4h, hi4h, lo4h, cl4h, vol4h = _d["k4h"]
    opD,  hiD,  loD,  clD,  volD  = _d["kD"]
    funding_hist                   = _d["fund"]
    ls_ratio                       = _d["ls"]
    trades                         = _d["trades"]
    bids, asks                     = _d["book"]

    # Perp/Spot volume ratio: вычисляем из spot_vol_map переданного через global_ctx
    _gctx = global_ctx or {}
    spot_vol_map = _gctx.get("spot_vol", {})
    perp_turnover = float(tickers[sym].get("turnover24h", 0) or 0)
    spot_turnover = spot_vol_map.get(sym, 0)
    perp_spot_ratio = perp_turnover / spot_turnover if spot_turnover > 0 else None

    # Передаём в score_symbol через расширенный контекст (включая недельные + 15м свечи)
    sym_ctx = {**_gctx, "perp_spot_ratio": perp_spot_ratio,
               "k1w": _d["k1w"], "k15m": _d["k15m"]}

    result = score_symbol(
        sym, tickers[sym], oi_hist,
        op1h, hi1h, lo1h, cl1h, vol1h,
        op4h, hi4h, lo4h, cl4h, vol4h,
        opD,  hiD,  loD,  clD,  volD,
        ls_ratio, trades, bids, asks,
        funding_hist, btc_chg_24h,
        liq_stats=liq_stats, btc_chg_4h=btc_chg_4h, score_weights=score_weights,
        global_ctx=sym_ctx,
    )

    # Кросс-биржевое подтверждение (Binance)
    if result is not None and _BNB_AVAILABLE and bnb_map is not None:
        _bnb.apply_cross_bonus(result, bnb_map.get(sym))

    # TRADE_LEARNINGS_DB gate — apply score penalties / bonuses / prohibition
    if result is not None:
        result = _apply_tldb_gate(result)

    return result


def _load_cooldown() -> dict:
    try:
        with open(COOLDOWN_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cooldown(cache: dict):
    now_ts = time.time()
    clean = {k: v for k, v in cache.items() if now_ts - v < 48 * 3600}
    with open(COOLDOWN_PATH, "w") as f:
        json.dump(clean, f)


def _apply_cooldown(rows: list) -> tuple:
    """Разделяет сигналы на прошедшие и заблокированные кулдауном.
    Возвращает (passed, blocked)."""
    cache    = _load_cooldown()
    now_ts   = time.time()
    threshold = COOLDOWN_HOURS * 3600
    passed, blocked = [], []
    for r in rows:
        last_ts = cache.get(r["symbol"], 0)
        if now_ts - last_ts >= threshold:
            passed.append(r)
        else:
            hrs_left = (threshold - (now_ts - last_ts)) / 3600
            blocked.append((r, hrs_left))
    return passed, blocked


def _record_cooldown(symbols: list):
    cache = _load_cooldown()
    now_ts = time.time()
    for sym in symbols:
        cache[sym] = now_ts
    _save_cooldown(cache)


def _passes_setup_tg_filter(r: dict) -> bool:
    """Per-setup Telegram eligibility — WR audit 2026-04-24, N=2232 trades."""
    setup = r.get("setup", "")
    score = r["score"]

    if setup == "range_sweep":
        # Disabled: WR=25%, avg loss −21.89%, and sweep events expire before batch cron fires.
        # sweep_watcher.py handles real-time detection.
        return False

    if setup == "breakout":
        # WR audit: score 100-120 → 53.1% WR (pass), score >120 → 33.3% WR (block)
        # Old gate was inverted; block only high scores now.
        return score < 120

    # P1.1: Squeeze mid-score (100–140) hard requirement gate.
    # WR audit: 100–140 achieves only 42.0% WR (24h) vs 53.6% for <100 and 51.0% for >150.
    # Require at least one strong confirmatory signal in this band.
    if setup == "squeeze" and 100 <= score <= 140:
        funding = r.get("fund_%", 0)
        has_strong_signal = (
            funding <= -0.05                           # high/extreme negative funding
            or r.get("choch_1h") == "bull_choch"      # structure change confirmed
            or r.get("liq_short_usd", 0) >= 300_000   # real liquidation fuel ($300K+)
            or r.get("mtf_b", 0) >= 2                 # MTF confluence ≥ 2 zones
        )
        if not has_strong_signal:
            return False

    min_sc = SETUP_TG_MIN_SCORE.get(setup, 80)
    if r.get("choch_conviction"):
        min_sc = max(60, min_sc - 30)
    max_sc = SETUP_TG_MAX_SCORE.get(setup)
    if score < min_sc:
        return False
    if max_sc is not None and score >= max_sc:
        return False
    return True


def run_screener(top_n=50, min_score=35,
                 watchlist_size=5, deep_dive_size=3,
                 export_json=None, export_csv=None,
                 obsidian=False, send_channels=False,
                 bypass_cooldown=False):
    print(f"\n{'='*72}")
    print(f"  Bybit Futures Screener  |  {datetime.now().strftime('%H:%M:%S  %d.%m.%Y')}")
    print(f"{'='*72}")

    # Ждём сеть перед любыми API-запросами (защита от DNS-краша при запуске)
    if not wait_for_network():
        print("[ERROR] Нет сети — скан пропущен.")
        return []

    # ── Audit Mode gate (AVEVA-50) ────────────────────────────────────────────
    if _STREAK_AVAILABLE and _streak.is_audit_mode():
        state = _streak.get_audit_state()
        print(f"\n{'='*72}")
        print("  🚨 AUDIT MODE — SCREENER ЗАБЛОКИРОВАН")
        print(f"{'='*72}")
        print(f"  Причина   : {state.get('trigger_reason', '?')}")
        print(f"  Активирован: {(state.get('activated_at') or '')[:19]} UTC")
        print(f"  Серия     : {state.get('streak_count', 0)} убытков подряд  |  "
              f"Просадка: {state.get('drawdown_r', 0):.2f}R")
        for r in state.get("restrictions", []):
            print(f"    • {r}")
        print(f"\n  Для выхода: python3 streak_monitor.py --exit")
        print(f"{'='*72}\n")
        return []

    print("Загружаю тикеры Bybit...")
    tickers = fetch_all_tickers()
    all_sorted = sorted(
        tickers.keys(),
        key=lambda s: float(tickers[s].get("turnover24h", 0)),
        reverse=True,
    )
    # Quality filter: исключаем памп/дамп (>50% за 24h) и низколиквидные (<$50M)
    _before_qf = len(all_sorted)
    symbols = [
        s for s in all_sorted
        if float(tickers[s].get("turnover24h", 0)) >= MIN_TURNOVER_24H
        and abs(float(tickers[s].get("price24hPcnt", 0))) <= MAX_MOVE_24H_ABS
        and s not in SYMBOL_BLACKLIST
    ][:top_n]
    _qf_removed = _before_qf - len(symbols) - max(0, len(all_sorted) - top_n - (_before_qf - len(symbols) - max(0, _before_qf - top_n, 0)), 0)
    _extreme_move = [
        s for s in all_sorted[:top_n * 2]
        if abs(float(tickers[s].get("price24hPcnt", 0))) > MAX_MOVE_24H_ABS
    ]
    if _extreme_move:
        print(f"[QF] Исключено {len(_extreme_move)} пар с |move|>50%: {', '.join(_extreme_move[:5])}{'…' if len(_extreme_move)>5 else ''}")

    # BTC 24h change для Relative Strength
    btc_chg_24h = float(tickers.get("BTCUSDT", {}).get("price24hPcnt", 0)) * 100

    # BTC 4h velocity-фильтр + EMA позиция (для записи в resolved.csv)
    btc_chg_4h = fetch_btc_4h_change()
    btc_ema_pos = fetch_btc_4h_ema_position()
    if abs(btc_chg_4h) > 0.1:
        print(f"BTC 4h velocity: {btc_chg_4h:+.2f}%  EMA pos: {btc_ema_pos}")

    # Ликвидации за последний час из локальной БД (liquidation_tracker пишет)
    liq_stats = fetch_liquidation_stats(window_min=60)
    if liq_stats:
        print(f"Liquidation DB: {len(liq_stats)} символов с активностью за 60мин")

    # Веса скоринга из исторических outcome'ов
    score_weights = load_score_weights(min_samples=20)
    if score_weights:
        print(f"Score weights из outcomes: {len(score_weights)} бакетов")

    # Fear & Greed (одиночный запрос, не зависит от пар)
    fg_value, fg_label = fetch_fear_greed()
    if fg_value is not None:
        print(f"Fear & Greed: {fg_value}/100 [{fg_label}]")

    # Текущая торговая сессия
    session_info = get_session_info()
    print(f"Сессия: {session_info['session']}  UTC {session_info['hour_utc']:02d}:xx")
    if session_info["best_for"]:
        print(f"Лучшее время для: {'  '.join(session_info['best_for'])}")

    print(f"Анализирую {len(symbols)} пар | BTC 24h: {btc_chg_24h:+.2f}%\n")

    # Spot volume map для perp/spot ratio (один запрос, не параллельный)
    spot_vol_map: dict = {}
    try:
        spot_result = get(f"{BASE}/v5/market/tickers", {"category": "spot"})
        for t in (spot_result.get("list") or []):
            s = t.get("symbol", "")
            if s.endswith("USDT"):
                spot_vol_map[s] = float(t.get("turnover24h", 0) or 0)
        print(f"Spot volume: {len(spot_vol_map)} пар")
    except Exception as _e:
        print(f"[spot vol] Пропущен: {_e}")

    # BTC Dominance (CoinGecko global, кэш 15 мин)
    btc_dominance = None
    if _FD_AVAILABLE:
        try:
            btc_dominance = _fd.get_btc_dominance()
            if btc_dominance is not None:
                season = "BTC season" if btc_dominance > 52 else ("Alt season" if btc_dominance < 47 else "нейтр")
                print(f"BTC Dominance: {btc_dominance:.1f}%  [{season}]")
        except Exception:
            pass

    # Instruments-info batch: возраст листинга каждого символа (один HTTP-запрос)
    listing_ts_map: dict = {}
    try:
        _inst_resp = get(f"{BASE}/v5/market/instruments-info",
                         {"category": "linear", "limit": 1000})
        for _inst in (_inst_resp.get("list") or []):
            _s = _inst.get("symbol", "")
            _lt = _inst.get("launchTime")
            if _s and _lt:
                listing_ts_map[_s] = int(_lt)
        print(f"Instruments info: {len(listing_ts_map)} символов (возраст листинга)")
    except Exception as _e:
        print(f"[instruments-info] Пропущен: {_e}")

    # Социальный сентимент: CryptoPanic + LunarCrush (если есть ключи)
    social_ctx: dict = {}
    if _FD_AVAILABLE:
        try:
            social_ctx = _fd.get_social_context(symbols)
            if social_ctx:
                print(f"Социальный сентимент: {len(social_ctx)} монет (CryptoPanic/LunarCrush)")
        except Exception as _e:
            print(f"[social] Пропущен: {_e}")

    # CoinGecko trending: загружаем ДО скоринга чтобы использовать в scoring
    trending_symbols: set = set()
    if _FD_AVAILABLE:
        try:
            _trend_raw = _fd.get_trending(limit_coins=10)
            trending_symbols = {c["symbol"] for c in _trend_raw.get("coins", [])}
            if trending_symbols:
                print(f"CoinGecko trending: {', '.join(sorted(trending_symbols)[:7])}")
        except Exception as _e:
            print(f"[trending] Пропущен: {_e}")

    # P1.6: Фильтр новых листингов (< 30 дней) — до параллельного скоринга
    if listing_ts_map:
        _now_ms = time.time() * 1000
        _too_new = [s for s in symbols
                    if listing_ts_map.get(s) is not None
                    and (_now_ms - listing_ts_map[s]) / 86_400_000 < 30]
        if _too_new:
            print(f"[QF] Исключено {len(_too_new)} монет с листингом < 30 дней: "
                  f"{', '.join(_too_new[:5])}{'…' if len(_too_new) > 5 else ''}")
            _too_new_set = set(_too_new)
            symbols = [s for s in symbols if s not in _too_new_set]

    # Единый контейнер глобального контекста → передаётся в каждый воркер
    global_ctx = {
        "spot_vol":        spot_vol_map,
        "btc_dominance":   btc_dominance,
        "btc_ema_pos":     btc_ema_pos,
        "listing_ts_map":  listing_ts_map,
        "social_ctx":      social_ctx,
        "trending_symbols": trending_symbols,
    }

    # Binance кросс-подтверждение (один bulk-запрос + parallel OI)
    bnb_map = {}
    if _BNB_AVAILABLE:
        try:
            print("Загружаю Binance cross-exchange данные...")
            bnb_map = _bnb.build_cross_map(symbols, include_oi=True)
            print(f"Binance данные: {len(bnb_map)}/{len(symbols)} пар")
        except Exception as _e:
            print(f"[Binance bridge] Пропущен: {_e}")

    results, errors = [], 0

    # ── Параллельное получение данных ─────────────────────────────────────────
    # max_workers=12: Bybit public API limit ~120 req/min.
    # 60 пар × 8 запросов = 480 запросов; при 12 воркерах ~25-35 сек вместо ~3 мин.
    MAX_WORKERS = min(12, len(symbols))
    completed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_and_score, sym, tickers, btc_chg_24h, bnb_map,
                        liq_stats, btc_chg_4h, score_weights, global_ctx): sym
            for sym in symbols
        }
        for future in as_completed(futures):
            completed += 1
            if completed % 10 == 0 or completed == len(symbols):
                print(f"  Обработано {completed}/{len(symbols)}...", end="\r")
            try:
                row = future.result()
                if row:
                    results.append(row)
                else:
                    errors += 1
            except Exception:
                errors += 1

    print()  # новая строка после \r-прогресса
    if errors:
        print(f"⚠  Пропущено: {errors} пар")

    # Alt breadth: % символов с бычьим D или 4H трендом (рыночный контекст)
    if results:
        _bull_count = sum(
            1 for r in results
            if r.get("d_htf") == "bull" or r.get("h4_htf") == "bull"
        )
        alt_breadth_pct = round(_bull_count / len(results) * 100, 1)
        for r in results:
            r["alt_breadth_pct"] = alt_breadth_pct
        print(f"Alt breadth: {alt_breadth_pct}%  ({_bull_count}/{len(results)} в аптренде)")

    # Помечаем монеты из CoinGecko trending (trending_symbols уже загружен до скоринга)
    for r in results:
        base = r["symbol"].upper().replace("USDT", "").replace("PERP", "")
        r["trending"] = base in trending_symbols
        if r["trending"] and r.get("flags", "—") != "—":
            r["flags"] = r["flags"] + " 🔥"
        elif r["trending"]:
            r["flags"] = "🔥"

    filtered = sorted(
        [r for r in results if r["score"] >= min_score],
        key=lambda x: x["score"],
        reverse=True,
    )

    if not filtered:
        print(f"Нет пар с score >= {min_score}. Попробуй снизить порог.")
        return []

    print_market_snapshot(results, filtered, btc_chg_24h,
                          session_info=session_info,
                          fg_value=fg_value, fg_label=fg_label)
    print_sector_rotation(results)
    print_directional_watchlists(filtered, limit=watchlist_size)

    for key, label in SETUP_LABELS.items():
        group = [r for r in filtered if r["setup"] == key]
        if not group:
            continue
        print(f"\n{label}")
        table = []
        for r in group[:10]:
            poc_str  = f"{r['poc_%']:+.1f}%"    if r["poc_%"]   is not None else "—"
            rs_str   = f"{r['rs_btc']:.2f}x"    if r["rs_btc"]  is not None else "—"
            pump_str = str(r.get("pump_score", 0))
            # Краткий pre-pump флаг
            pp_flags = []
            if r.get("atr_comp", 1.0) < 0.65:                      pp_flags.append(f"A{r['atr_comp']:.2f}")
            if r.get("oi_coiling"):                                  pp_flags.append("OIc")
            if r.get("cvd_div", "—") != "—":                        pp_flags.append(r["cvd_div"][:5])
            if r.get("vol_accel", "—") != "—":                      pp_flags.append("vAc")
            if r.get("whale", "—") != "—":                          pp_flags.append("кит")
            if r.get("rsi_div_1h", "—") not in ("—", None):         pp_flags.append(f"RSI:{r['rsi_div_1h'][:6]}")
            if r.get("ema_1h", {}).get("golden_cross"):              pp_flags.append("GX!")
            if r.get("ema_1h", {}).get("ema_bull"):                  pp_flags.append("EMA↑")
            if r.get("choch_1h", "—") in ("bull_choch","bear_choch"):pp_flags.append(r["choch_1h"][:6])
            pp_str = "|".join(pp_flags) or "—"

            # VWAP deviation строка
            vd = r.get("vwap_dev")
            vd_str = f"{vd:+.1f}%" if vd is not None else "—"

            # EMA order string
            e1h  = r.get("ema_1h", {})
            ema_str = ("bull" if e1h.get("ema_bull") else
                       "bear" if e1h.get("ema_bear") else
                       "GX!"  if e1h.get("golden_cross") else "—")

            table.append([
                r["symbol"],
                f"{r['price']:.5g}",
                composite_grade(r),
                f"{r['fund_%']:+.3f}%",
                f"{r['oi24h_%']:+.1f}%",
                f"{r['pos_%']}%",
                f"×{r['vol_x']}",
                f"{r['d_htf']}/{r['h4_htf']}",
                f"{r['mtf_b']}↑{r['mtf_s']}↓",
                f"{r['rsi_1h']:.0f}" if r.get("rsi_1h") else "—",
                vd_str,
                ema_str,
                f"{r['cvd_k%']:+.0f}%",
                poc_str,
                rs_str,
                r["sweep"],
                r["flags"][:25],
                r["score"],
                pump_str,
                pp_str,
                r["notes"][:30],
            ])
        print(tabulate(
            table,
            headers=["Пара","Цена","Grade","Fund","OI24h","Поз%","Vol",
                     "D/4H","MTF","RSI","VWAP","EMA","CVD",
                     "POC%","RS","Swp","Flags","Score","⊕Pump","⊕Sig","Заметки"],
            tablefmt="rounded_outline",
            numalign="right",
        ))

    # ── Deep Dive: топ по всем сетапам ─────────────────────────────────────────
    top_rows = sorted(filtered, key=lambda x: x["score"], reverse=True)[:deep_dive_size]
    deep_dive_data = []   # для Telegram
    if top_rows:
        print("\n" + "═" * 72)
        print(f"  DEEP DIVE: топ-{len(top_rows)} кандидата — сигнал по сигналу")
        print("═" * 72)
        dir_color = {"ЛОНГ": "▲", "ШОРТ": "▼", "ЖДАТЬ": "◆", "ИНФО": "•"}
        setup_ru  = {
            "squeeze":     "Ликвидационный сквиз",
            "bos_fvg":     "BOS + FVG / OB",
            "range_sweep": "Рейндж Sweep",
            "breakout":    "Breakout / Pre-Pump",
        }
        # Обогащаем топ-кандидатов данными Binance (ордербук + тейкер) перед deep dive
        if _FD_AVAILABLE:
            for r in top_rows:
                try:
                    enrichment = _fd.get_binance_enrichment(r["symbol"])
                    if enrichment:
                        r.update(enrichment)
                except Exception:
                    pass

        for r in top_rows:
            signals, verdict, bull, bear = interpret_signals(r)
            plan = build_trade_plan(r)
            deep_dive_data.append((r, signals, verdict, bull, bear, plan))

            print(f"\n  {'─'*68}")
            print(f"  {r['symbol']}  |  score={r['score']}  |  [{setup_ru.get(r['setup'], r['setup'])}]")
            print(f"  ИТОГ: {verdict}  ({bull} бычьих / {bear} медвежьих сигналов)")
            if plan["side"] != "wait":
                print(f"  {'─'*68}")
                print(f"  ТОРГОВЫЙ ПЛАН  (уровни структуры рынка)")
                print(f"  {'─'*68}")
                # Формат: LABEL  цена  [источник уровня]
                def _pline(label, price_str, note):
                    print(f"  {label:<10} {price_str:<22}  [{note.strip()}]")
                if plan["entry_low"] != plan["entry_high"]:
                    _pline("ENTRY",
                           f"{format_price(plan['entry_low'])} .. {format_price(plan['entry_high'])}",
                           plan["entry_note"])
                else:
                    _pline("ENTRY", format_price(plan["entry_low"]), plan["entry_note"])
                _pline("STOP",  format_price(plan["stop"]),  plan["stop_note"])
                _pline("TP1",   format_price(plan["tp1"]),   plan["tp1_note"])
                _pline("TP2",   format_price(plan["tp2"]),   plan["tp2_note"])
                print(f"  {'R:R':<10} {plan['rr']:.2f}")
                print(f"  {'─'*68}")

            for metric, val, direction, explanation in signals:
                icon  = dir_color.get(direction, "•")
                label = f"{direction:<17}"  # выравнивание по ширине
                print(f"  {icon} {label}  {metric:<18} {val}")
                # объяснение с отступом
                words      = explanation.split()
                line, out  = "", []
                for w in words:
                    if len(line) + len(w) + 1 > 60:
                        out.append(line)
                        line = w
                    else:
                        line = (line + " " + w).strip()
                if line:
                    out.append(line)
                for li in out:
                    print(f"    {'':17}  {'':18}  {li}")

            print(f"\n  Flags: {r['flags']}")
            print(f"  Заметки: {r['notes']}")
            print(f"  Инвалидация: {plan['invalidation']}")

    print_pump_forecast(results)

    export_results(filtered, json_path=export_json, csv_path=export_csv)

    # ── Obsidian export ────────────────────────────────────────────────────────
    if obsidian and _OBS_AVAILABLE:
        obs_cfg = _obs.load_config()
        if not obs_cfg.get("enabled") or not obs_cfg.get("vault_path"):
            print("[Obsidian] Интеграция не настроена. Запусти: python3 obsidian_bridge.py setup")
        else:
            # Отчёт скринера
            rpt = _obs.export_report(
                results=results,
                filtered=filtered,
                session_info=session_info,
                fg_value=fg_value,
                fg_label=fg_label,
                cfg=obs_cfg,
            )
            if rpt:
                print(f"[Obsidian] Отчёт сохранён: {rpt}")

            # Кандидаты на памп
            pump_candidates = []
            for r in results:
                conviction, stars, signals_list, trigger_hint = build_pump_narrative(r)
                ps = r.get("pump_score", 0)
                if ps >= 30:
                    pump_candidates.append({
                        "symbol":      r["symbol"],
                        "price":       r.get("price", 0),
                        "pump_score":  ps,
                        "conviction":  conviction,
                        "stars":       stars,
                        "signals":     signals_list,
                        "trigger_hint": trigger_hint,
                    })
            if pump_candidates:
                pump_candidates.sort(key=lambda x: x["pump_score"], reverse=True)
                pp = _obs.export_pump_candidates(pump_candidates, cfg=obs_cfg)
                if pp:
                    print(f"[Obsidian] Пампы сохранены: {pp}")
    elif obsidian and not _OBS_AVAILABLE:
        print("[Obsidian] Модуль obsidian_bridge.py не найден рядом со screener.py")

    # ── Channel signal enrichment ─────────────────────────────────────────────
    if _CH_AVAILABLE:
        _ch.enrich_with_channel_signals(filtered)
        _ch.enrich_with_news_impact(filtered)

        # Hard-skip: критические новости против направления сетапа
        blocked = [r for r in filtered if r.get("news_hard_block")]
        if blocked:
            for r in blocked:
                print(f"[News-Block] {r['symbol']:10s} {r.get('setup','?'):10s} "
                      f"причина: {r.get('news_hard_reason','')}")
            filtered = [r for r in filtered if not r.get("news_hard_block")]
            print(f"[News-Block] Отфильтровано: {len(blocked)} сетапов по критическим новостям")

    # ── Setup quarantine: per-setup score gates from WR audit (2026-04-24) ──────
    _tg_candidates = []
    _quarantine_blocked = []
    for _r in filtered:
        if _passes_setup_tg_filter(_r):
            _tg_candidates.append(_r)
        else:
            _quarantine_blocked.append(_r)

    # Tag BOS/FVG signals with score >150: 24h WR=61.1% vs 36.6% baseline.
    for _r in _tg_candidates:
        if _r.get("setup") == "bos_fvg" and _r["score"] > 150:
            _r["tg_24h_hold"] = True

    if _quarantine_blocked:
        print(f"[Quarantine] Заблокировано: {len(_quarantine_blocked)} сигналов по setup-score фильтру")
        for _r in _quarantine_blocked:
            print(f"  🚫 {_r['symbol']:12s} {_r.get('setup','?'):12s} score={_r['score']}")

    # ── Time gate: фильтр плохих часов ────────────────────────────────────────
    _utc_hour = datetime.utcnow().hour
    _utc_weekday = datetime.utcnow().weekday()  # 0=Mon … 5=Sat … 6=Sun
    if bypass_cooldown:
        print(f"[TimeGate] bypass_cooldown=True — временной фильтр пропущен")
    elif _utc_weekday == 5:
        # FIX 8: Saturday WR=24.5% — повышаем порог до SATURDAY_MIN_SCORE
        _before_sat = len(_tg_candidates)
        _tg_candidates = [r for r in _tg_candidates if r["score"] >= SATURDAY_MIN_SCORE]
        _sat_blocked = _before_sat - len(_tg_candidates)
        if _sat_blocked:
            print(f"[TimeGate] Суббота — слабый WR (24.5%). "
                  f"Заблокировано: {_sat_blocked} (score < {SATURDAY_MIN_SCORE}). "
                  f"Осталось: {len(_tg_candidates)}")
        else:
            print(f"[TimeGate] Суббота — слабый WR, но все {len(_tg_candidates)} выше порога {SATURDAY_MIN_SCORE}.")
    elif _utc_weekday == 4:
        # FINDING 7: Friday lower WR (n=26, not hard block) — threshold × 1.3
        _before_fri = len(_tg_candidates)
        _tg_candidates = [r for r in _tg_candidates if r["score"] >= FRIDAY_MIN_SCORE]
        _fri_blocked = _before_fri - len(_tg_candidates)
        if _fri_blocked:
            print(f"[TimeGate] Пятница — пониженный WR. "
                  f"Заблокировано: {_fri_blocked} (score < {FRIDAY_MIN_SCORE}). "
                  f"Осталось: {len(_tg_candidates)}")
        else:
            print(f"[TimeGate] Пятница — пониженный WR, но все {len(_tg_candidates)} выше порога {FRIDAY_MIN_SCORE}.")
    elif _utc_weekday == 1:
        # FINDING 7: Tuesday mild lower WR — threshold × 1.15
        _before_tue = len(_tg_candidates)
        _tg_candidates = [r for r in _tg_candidates if r["score"] >= TUESDAY_MIN_SCORE]
        _tue_blocked = _before_tue - len(_tg_candidates)
        if _tue_blocked:
            print(f"[TimeGate] Вторник — умеренно пониженный WR. "
                  f"Заблокировано: {_tue_blocked} (score < {TUESDAY_MIN_SCORE}). "
                  f"Осталось: {len(_tg_candidates)}")
        else:
            print(f"[TimeGate] Вторник — пониженный WR, но все {len(_tg_candidates)} выше порога {TUESDAY_MIN_SCORE}.")
    elif _utc_hour in HARD_BLOCK_HOURS:
        # WR 21–37% — полный хард-блок TG-алертов (AVEVA-55)
        _n_before_hb = len(_tg_candidates)
        _tg_candidates = []
        wr_map = {17: "36.5%", 18: "28.6%", 19: "21.4%", 22: "32.8%"}
        _wr_str = wr_map.get(_utc_hour, "<37%")
        print(f"[TimeGate] UTC {_utc_hour:02d}:xx — HARD BLOCK (WR={_wr_str}). "
              f"Заблокировано {_n_before_hb} сигналов. TG не отправляется.")
    elif _utc_hour in BAD_SIGNAL_HOURS:
        _before_tg = len(_tg_candidates)
        _tg_candidates = [r for r in _tg_candidates if r["score"] >= BAD_HOUR_MIN_SCORE]
        _tg_blocked_time = _before_tg - len(_tg_candidates)
        if _tg_blocked_time:
            print(f"[TimeGate] UTC {_utc_hour:02d}:xx — плохой час. "
                  f"Заблокировано: {_tg_blocked_time} (score < {BAD_HOUR_MIN_SCORE}). "
                  f"Осталось: {len(_tg_candidates)}")
        else:
            print(f"[TimeGate] UTC {_utc_hour:02d}:xx — плохой час, но все {len(_tg_candidates)} выше порога.")
    elif _utc_hour in GOOD_SIGNAL_HOURS:
        print(f"[TimeGate] UTC {_utc_hour:02d}:xx — хороший час ✓")

    # ── Fix 2: Grade-фильтр — B+/C/D не уходят в TG (AVEVA-55) ───────────────
    _before_grade = len(_tg_candidates)
    _tg_candidates = [r for r in _tg_candidates
                      if r.get("grade", "B") not in ("B+", "C", "D", "X")]
    _grade_blocked = _before_grade - len(_tg_candidates)
    if _grade_blocked:
        print(f"[GradeGate] Заблокировано {_grade_blocked} сигналов (grade B+/C/D/X, WR≤42%)")

    # ── Fix 3: squeeze falling knife — vwap_dev < -8% = не входить (AVEVA-55) ─
    _before_fk = len(_tg_candidates)
    _tg_candidates = [
        r for r in _tg_candidates
        if not (r.get("setup") == "squeeze"
                and (r.get("vwap_dev") or 0) < -8.0)
    ]
    _fk_blocked = _before_fk - len(_tg_candidates)
    if _fk_blocked:
        print(f"[FallingKnife] Заблокировано {_fk_blocked} squeeze-сигналов (vwap_dev<-8%, WR=21.6%)")

    # ── AVEVA-57: Hard Confluence Gate ────────────────────────────────────────
    # Сигнал без хотя бы 1 жёсткого подтверждения = шум, не сетап.
    # Требуем: CHoCH ИЛИ (в FVG/OB зоне) ИЛИ sweep ИЛИ экстр. фандинг.
    def _has_hard_signal(r: dict) -> bool:
        setup   = r.get("setup", "")
        sdir    = "short" if setup == "short_dist" else "long"
        choch   = r.get("choch_1h", "—")
        sweep   = r.get("sweep", "—")
        fund    = r.get("fund_%", 0) or 0
        if sdir == "long":
            return (
                choch == "bull_choch"                 or
                bool(r.get("in_bfvg")) or bool(r.get("in_bob")) or
                fund < -0.05                          or
                ("↓" in sweep and sweep != "—")       # ликвидность снята снизу
            )
        else:  # short
            return (
                choch == "bear_choch"                 or
                bool(r.get("in_sfvg")) or bool(r.get("in_sob")) or
                fund > 0.05                           or
                ("↑" in sweep and sweep != "—")
            )

    _before_hcg = len(_tg_candidates)
    _tg_candidates = [r for r in _tg_candidates if _has_hard_signal(r)]
    _hcg_blocked = _before_hcg - len(_tg_candidates)
    if _hcg_blocked:
        print(f"[HardGate] Заблокировано {_hcg_blocked} сигналов — нет CHoCH/FVG/OB/sweep/exfund")

    # ── AVEVA-57: squeeze только в нижней части диапазона (pos_% ≤ 45) ───────
    # Squeeze вне дисконта = покупка на середине/вершине = не сквиз.
    # Данные: BOT(<-3% VWAP)=54.1% WR vs MID=50.1% WR
    _before_sq = len(_tg_candidates)
    _tg_candidates = [
        r for r in _tg_candidates
        if not (r.get("setup") == "squeeze"
                and (r.get("pos_%") or 100) > 45)
    ]
    _sq_blocked = _before_sq - len(_tg_candidates)
    if _sq_blocked:
        print(f"[SqueezeZone] Заблокировано {_sq_blocked} squeeze вне дисконта (pos%>45)")

    # ── AVEVA-57: breakout — минимальный score 120 ───────────────────────────
    # Breakout score 80-119: WR=46% (хуже squeeze). В хорошие часы нормально,
    # но низкий скор = неподтверждённый пробой = ложный сигнал.
    _before_bo = len(_tg_candidates)
    _tg_candidates = [
        r for r in _tg_candidates
        if not (r.get("setup") == "breakout"
                and r.get("score", 0) < 120)
    ]
    _bo_blocked = _before_bo - len(_tg_candidates)
    if _bo_blocked:
        print(f"[BreakoutFloor] Заблокировано {_bo_blocked} breakout score<120")

    # ── AVEVA-57: R:R минимум 1.5 ────────────────────────────────────────────
    # При WR=54% нужен R:R ≥ 1.5 для положительного мат.ожидания.
    # build_trade_plan вызывается здесь — план уже строится заново при отправке.
    _before_rr = len(_tg_candidates)
    _rr_passed = []
    for _r in _tg_candidates:
        try:
            _plan = build_trade_plan(_r)
            if _plan["side"] in ("long", "short") and 0 < _plan["rr"] < 1.5:
                print(f"[RRGate] {_r['symbol']} R:R={_plan['rr']:.2f} < 1.5 → блок")
                continue
        except Exception:
            pass  # fail-open: если план не строится — пропускаем в TG
        _rr_passed.append(_r)
    _tg_candidates = _rr_passed
    _rr_blocked = _before_rr - len(_tg_candidates)
    if _rr_blocked:
        print(f"[RRGate] Итого заблокировано {_rr_blocked} сигналов R:R<1.5")

    # ── AVEVA-57: Sector concentration warning ────────────────────────────────
    if _tg_candidates:
        _sector_count: dict = {}
        for _r in _tg_candidates:
            _sec = classify_sector(_r["symbol"])
            if _sec != "Other":
                _sector_count.setdefault(_sec, []).append(_r["symbol"])
        for _sec, _syms in _sector_count.items():
            if len(_syms) >= 2:
                print(f"[SectorWarn] ⚠ {_sec}: {', '.join(_syms)} — концентрация в секторе!")

    # ── AVEVA-58: OI exhaustion — блокируем дистрибуцию и шорт в сильный тренд ──
    _oi58_before = len(_tg_candidates)
    def _oi58_ok(r: dict) -> bool:
        _oi_div = r.get("oi_div", "—")
        if r.get("setup") == "short_dist":
            return _oi_div != "strong_bull"  # не шортим в здоровый аптренд (новые лонги)
        else:
            return _oi_div != "bear_div"     # не лонгуем при дистрибуции (цена↑, OI↓)
    _tg_candidates = [r for r in _tg_candidates if _oi58_ok(r)]
    _oi58_blocked = _oi58_before - len(_tg_candidates)
    if _oi58_blocked:
        print(f"[Filter] OI exhaustion: заблокировано {_oi58_blocked} сигналов "
              f"(bear_div на ЛОНГ или strong_bull на ШОРТ)")

    # ── AVEVA-58: Staleness TTL — аномально старые kline данные ───────────────
    _stale58_before = len(_tg_candidates)
    _now58 = time.time()
    _MAX_KLINE_AGE_SEC = 3 * 3600  # 3 часа: последняя закрытая 1H свеча не может быть старше
    _tg_candidates = [
        r for r in _tg_candidates
        if r.get("kline_1h_ts", _now58) == 0.0
           or (_now58 - r.get("kline_1h_ts", _now58)) < _MAX_KLINE_AGE_SEC
    ]
    _stale58_blocked = _stale58_before - len(_tg_candidates)
    if _stale58_blocked:
        print(f"[Filter] Staleness TTL: заблокировано {_stale58_blocked} сигналов "
              f"(данные kline старше 3h — возможна аномалия API)")

    # ── Cooldown фильтр: 8h между сигналами по одной паре ─────────────────────
    if bypass_cooldown:
        _cd_blocked = []
        print("[Cooldown] bypass_cooldown=True — фильтр пропущен (ручной запрос)")
    else:
        _tg_candidates, _cd_blocked = _apply_cooldown(_tg_candidates)
        if _cd_blocked:
            print(f"[Cooldown] Заблокировано: {len(_cd_blocked)} пар (< {COOLDOWN_HOURS}h с последнего сигнала)")
            for _r, _hrs in _cd_blocked:
                print(f"  ⏸ {_r['symbol']:12s}  score={_r['score']}  (через {_hrs:.1f}h)")

    # ── Telegram alerts ────────────────────────────────────────────────────────
    if _TG_AVAILABLE:
        tg_cfg = _tg.load_config()
        if tg_cfg.get("enabled") and tg_cfg.get("bot_token") and tg_cfg.get("chat_id"):
            if _tg_candidates:
                print(f"[TG] Отправляю отчёт в Telegram ({len(_tg_candidates)} сигналов)...")
                _tg.send_report(
                    results=results,
                    filtered=_tg_candidates,
                    btc_chg_24h=btc_chg_24h,
                    session_info=session_info,
                    fg_value=fg_value,
                    fg_label=fg_label,
                    deep_dive_data=[d for d in deep_dive_data
                                    if d[0]["symbol"] in {r["symbol"] for r in _tg_candidates}],
                    cfg=tg_cfg,
                )
                _record_cooldown([r["symbol"] for r in _tg_candidates])
                print("[TG] Готово.")
            else:
                print("[TG] Нет сигналов после фильтрации — пропускаем.")

    # ── Bundled channel insights (свежий скан вместе с сетапами) ──────────────
    if send_channels and _CH_AVAILABLE:
        try:
            print("[TG] Скан Telegram-каналов для инсайтов...")
            ch_cfg = _ch.load_cfg()
            if ch_cfg.get("api_id"):
                import asyncio
                channel_results = asyncio.run(_ch.scan_channels_async(ch_cfg))
                _ch.save_cache(channel_results)
                verified = _ch.cross_verify(channel_results)
                _ch.send_insights(verified)
                print("[TG] Инсайты каналов отправлены.")
            else:
                print("[TG] channel_reader не настроен — пропускаем.")
        except Exception as _e:
            print(f"[TG] Ошибка скана каналов: {_e}")

    # ── AI Chart Analysis (Claude Vision + метрики скринера) ──────────────────
    try:
        import chart_analyzer as _ca
        _ca_tg_cfg = _tg.load_config() if _TG_AVAILABLE else {}
        _ca_token  = _ca_tg_cfg.get("bot_token")
        _ca_cid    = _ca_tg_cfg.get("chat_id")
        if _ca_token and _ca_cid and _tg_candidates:
            # Сохраняем торговый план в кандидате для использования в анализе
            for _r, _sigs, _verd, _bull, _bear, _plan in deep_dive_data:
                _r["_trade_plan"] = _plan
            print(f"[ChartAI] Запускаю AI Chart Analysis для топ-3...")
            _ca.run_and_send(
                candidates=_tg_candidates,
                token=_ca_token,
                chat_id=str(_ca_cid),
                top_n=3,
            )
    except Exception as _ca_err:
        print(f"[ChartAI] Пропущен: {_ca_err}")

    # ── Outcome tracker ────────────────────────────────────────────────────────
    if _OT_AVAILABLE:
        # Сначала проверяем старые сигналы (прошло ≥4h или ≥24h)
        resolved = _ot.check_and_resolve(silent=True)
        if resolved:
            print(f"[Tracker] Закрыто исходов: {resolved} → outcome_tracker.py stats")
            # After resolving trades, check for losing streak / drawdown (AVEVA-50)
            if _STREAK_AVAILABLE:
                newly_activated = _streak.check_and_activate(silent=False)
                if newly_activated:
                    print("[Streak] 🚨 Audit Mode активирован — следующий скан будет заблокирован")
        # Сохраняем текущие сигналы как pending (кроме HARD_BLOCK часов — WR < 30%)
        if _utc_hour in HARD_BLOCK_HOURS:
            print(f"[HARD BLOCK] UTC {_utc_hour:02d}:xx — WR={'18' if _utc_hour==18 else '17.5'}% < 30%."
                  f" Сигналы не сохранены в pending (не торговать этот час).")
        else:
            # FIX 3: range_sweep WR=25% — exclude from pending to keep ML training data clean
            to_save = [r for r in filtered if r.get("setup") != "range_sweep"]
            # FIX 4: stamp grade at save time so resolved.csv has real grades (not "—")
            # Use calc_mtf_grade (TASK B) — includes weekly hard-block Grade X.
            for r in to_save:
                _sdir = "short" if r.get("setup") == "short_dist" else "long"
                r["grade"] = calc_mtf_grade(r, setup_dir=_sdir)
            saved = _ot.save_pending(to_save, results)
            if saved:
                print(f"[Tracker] Сохранено {saved} сигналов для бэктеста")

    print(f"\nКандидатов: {len(filtered)} из {len(results)}  |  "
          f"min score: {min_score}  |  {datetime.now().strftime('%H:%M:%S')}\n")
    return filtered


def parse_args():
    parser = argparse.ArgumentParser(
        description="Bybit futures screener c market snapshot, watchlist и export."
    )
    parser.add_argument("--top-n", type=int, default=50, help="Сколько пар анализировать по объёму.")
    parser.add_argument("--min-score", type=int, default=35, help="Минимальный score для показа.")
    parser.add_argument("--watchlist-size", type=int, default=5, help="Сколько кандидатов выводить в long/short watchlist.")
    parser.add_argument("--deep-dive-size", type=int, default=3, help="Сколько пар раскрывать в deep dive.")
    parser.add_argument(
        "--export-json",
        nargs="?",
        const="auto",
        default=None,
        help="Сохранить filtered results в JSON. Без пути сохранит в snapshots/ автоматически.",
    )
    parser.add_argument(
        "--export-csv",
        nargs="?",
        const="auto",
        default=None,
        help="Сохранить filtered results в CSV. Без пути сохранит в snapshots/ автоматически.",
    )
    parser.add_argument(
        "--obsidian",
        action="store_true",
        default=False,
        help="Экспортировать отчёт и кандидатов на памп в Obsidian vault. "
             "Настройка: python3 obsidian_bridge.py setup",
    )
    parser.add_argument(
        "--obsidian-setup",
        action="store_true",
        default=False,
        help="Запустить мастер настройки Obsidian и выйти.",
    )
    parser.add_argument(
        "--telegram-setup",
        action="store_true",
        default=False,
        help="Запустить мастер настройки Telegram и выйти.",
    )
    parser.add_argument(
        "--send-channels",
        action="store_true",
        default=False,
        help="После отчёта запустить свежий скан Telegram-каналов и отправить "
             "инсайты. Используется в авто-режиме раз в 4 часа.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Отдельный режим: запуск мастера настройки Obsidian
    if args.obsidian_setup:
        if _OBS_AVAILABLE:
            _obs.setup_wizard()
        else:
            print("Модуль obsidian_bridge.py не найден рядом со screener.py")
        return

    # Отдельный режим: запуск мастера настройки Telegram
    if args.telegram_setup:
        if _TG_AVAILABLE:
            _tg.setup_wizard()
        else:
            print("Модуль telegram_alerts.py не найден рядом со screener.py")
        return

    run_screener(
        top_n=args.top_n,
        min_score=args.min_score,
        watchlist_size=max(args.watchlist_size, 0),
        deep_dive_size=max(args.deep_dive_size, 0),
        export_json=args.export_json,
        export_csv=args.export_csv,
        obsidian=args.obsidian,
        send_channels=args.send_channels,
    )


if __name__ == "__main__":
    main()

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
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from tabulate import tabulate

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

BASE = "https://api.bybit.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "BybitFuturesScreener/1.1"})

SETUP_LABELS = {
    "squeeze":     "СЕТАП 1 — Ликвидационный сквиз",
    "bos_fvg":     "СЕТАП 2 — BOS + FVG / Order Block",
    "range_sweep": "СЕТАП 3 — Рейндж Sweep",
    "breakout":    "СЕТАП 4 — Breakout / Pre-Pump",
}

SETUP_SHORT = {
    "squeeze":     "SQZ",
    "bos_fvg":     "BOS/FVG",
    "range_sweep": "SWEEP",
    "breakout":    "PUMP",
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


def detect_candle_patterns(opens, highs, lows, closes):
    """
    Последний значимый свечной паттерн на завершённых свечах ([-4] до [-2]).

    Паттерны:
      bull_engulf    — бычье поглощение, тело поглощает предыдущую свечу
      bear_engulf    — медвежье поглощение
      hammer         — молот (тело ≤35% диапазона, нижний хвост ≥55%)
      shooting_star  — падающая звезда (тело ≤35%, верхний хвост ≥55%)
      inside_bar     — внутренняя свеча (компрессия перед движением)

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

        # Внутренняя свеча
        if h < ph and l > pl:
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


def detect_sweep(highs, lows, closes, lookback=10):
    """
    Sweep на ЗАВЕРШЁННОЙ свече [-2]:
    Пробила хай/лой предыдущих N свечей, но закрылась обратно.
    """
    if len(highs) < lookback + 3:
        return None, None

    last_h = highs[-2]
    last_l = lows[-2]
    last_c = closes[-2]
    prev_h = highs[-(lookback + 2):-2]
    prev_l = lows[-(lookback + 2):-2]
    if not prev_h:
        return None, None

    prev_high = max(prev_h)
    prev_low  = min(prev_l)

    sweep_up   = prev_high if (last_h > prev_high and last_c < prev_high) else None
    sweep_down = prev_low  if (last_l < prev_low  and last_c > prev_low)  else None

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
    > 70 = перекуплен. < 30 = перепродан. Дивергенция = главный сигнал.
    """
    c = closes[:-1]
    if len(c) < period + 2:
        return 50.0
    gains  = [max(c[i] - c[i-1], 0) for i in range(1, len(c))]
    losses = [max(c[i-1] - c[i], 0) for i in range(1, len(c))]
    ag     = sum(gains[-period:])  / period
    al     = sum(losses[-period:]) / period
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

    rsi_vals = []
    for i in range(period, len(c)):
        g  = [max(c[j] - c[j-1], 0) for j in range(i - period + 1, i + 1)]
        ls = [max(c[j-1] - c[j], 0) for j in range(i - period + 1, i + 1)]
        ag = sum(g)  / period
        al = sum(ls) / period
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


# ─── Scoring ─────────────────────────────────────────────────────────────────

def score_symbol(symbol, ticker, oi_hist,
                 op1h, hi1h, lo1h, cl1h, vol1h,
                 op4h, hi4h, lo4h, cl4h, vol4h,
                 opD,  hiD,  loD,  clD,  volD,
                 ls_ratio, trades, bids, asks,
                 funding_hist, btc_chg_24h):

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
    liq_events = detect_liq_events(oi_hist, cl1h, vol1h)
    dom        = analyze_dom(bids, asks, price)

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

    # Свечные паттерны по направлению
    bullish_pattern = candle_pat in ("bull_engulf", "hammer")
    bearish_pattern = candle_pat in ("bear_engulf", "shooting_star")

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

    # Funding trend: нарастающее давление
    if fund_trend == "declining":
        s1 += 15; n1.append("fund↓нараст")
    elif fund_trend == "normalizing" and funding < -0.01:
        s1 -= 8;  n1.append("fund норм-ся")

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

    # OI Divergence: шорты закрываются у дна = разворот вверх
    if oi_div == "bull_div":
        s1 += 18; n1.append("OI_div↑")
    elif oi_div == "strong_bull":
        s1 += 8;  n1.append("OI_bull")
    elif oi_div == "bear_div":
        s1 -= 10  # лонги закрываются = плохой сигнал для лонга

    # Подтверждения
    if long_liq:
        s1 += 12; n1.append("лонг-лики")

    # HTF: согласованность Daily + 4H
    if trend_bull_aligned:
        s1 += 15; n1.append("D+4H↑")
    elif daily_trend == "bull" or h4_trend == "bull":
        s1 += 8;  n1.append("HTF↑")

    # MTF Confluence (КЛЮЧЕВОЙ СИГНАЛ)
    if bull_mtf >= 3:
        s1 += 25; n1.append(f"MTF{bull_mtf}!")
    elif bull_mtf >= 2:
        s1 += 18; n1.append(f"MTF{bull_mtf}")
    elif bull_mtf == 1:
        s1 += 10; n1.append("MTF1")

    # Цена В FVG/OB прямо сейчас
    if in_bull_fvg:
        s1 += 18; n1.append("В FVG↑!")
    elif bull_fvg_1h and bull_fvg_1h[0]["dist_pct"] < 1.5:
        s1 += 10; n1.append(f"FVG↑{bull_fvg_1h[0]['dist_pct']:.1f}%")
    elif bull_fvg_1h and bull_fvg_1h[0]["dist_pct"] < 3.0:
        s1 += 5

    if in_bull_ob:
        s1 += 18; n1.append("В OB↑!")
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

    # OI Divergence подтверждает направление
    if oi_div == "strong_bull":
        s2 += 12; n2.append("OI_bull")
    elif oi_div == "strong_bear":
        s2 += 12; n2.append("OI_bear")

    if 0.25 < price_pos < 0.75:
        s2 += 15; n2.append(f"откат {price_pos:.0%}")

    if ls_ratio and ls_ratio > 1.5:
        s2 += 10; n2.append(f"L/S={ls_ratio:.2f}")

    # MTF Confluence
    best_mtf = max(bull_mtf, bear_mtf)
    if best_mtf >= 3:
        s2 += 25; n2.append(f"MTF{best_mtf}!")
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

    if cvd_bull_aligned: s2 += 8; n2.append("CVD↑")
    elif cvd_bear_aligned: s2 += 8; n2.append("CVD↓")

    # RS vs BTC
    if rs_btc is not None and rs_btc > 1.5:
        s2 += 8; n2.append(f"RS{rs_btc:.1f}x")

    scores["bos_fvg"] = s2
    notes["bos_fvg"]  = ", ".join(n2) or "—"

    # ═══════════════════════════════════════════════════════════════════════════
    # СЕТАП 3 — РЕЙНДЖ SWEEP
    # ═══════════════════════════════════════════════════════════════════════════
    s3, n3 = 0, []

    if sweep_down is not None:
        s3 += 55; n3.append(f"sweep↓{sweep_down:.4g}")
    if sweep_up is not None:
        s3 += 55; n3.append(f"sweep↑{sweep_up:.4g}")

    if price_pos < 0.12 or price_pos > 0.88:
        s3 += 18; n3.append(f"граница {price_pos:.0%}")

    # Funding vs позиция
    if funding > 0.01 and price_pos < 0.35:
        s3 += 14; n3.append(f"fund+{funding:.3f}% при дне")
    elif funding < -0.01 and price_pos > 0.65:
        s3 += 14; n3.append(f"fund{funding:.3f}% при вершине")

    # Funding trend усиливает несоответствие
    if fund_trend == "rising" and price_pos < 0.35:
        s3 += 8; n3.append("fund↑нараст")
    elif fund_trend == "declining" and price_pos > 0.65:
        s3 += 8; n3.append("fund↓нараст")

    # Ликвидации после sweep
    if long_liq  and sweep_down is not None:
        s3 += 15; n3.append("лонг-лики")
    if short_liq and sweep_up is not None:
        s3 += 15; n3.append("шорт-лики")

    # Свечной паттерн разворота после sweep
    if bullish_pattern and sweep_down is not None:
        s3 += 14; n3.append(f"{candle_pat} после sweep↓")
    if bearish_pattern and sweep_up is not None:
        s3 += 14; n3.append(f"{candle_pat} после sweep↑")

    # MTF зона в точке разворота
    if in_bull_fvg and sweep_down is not None:
        s3 += 12; n3.append("В FVG↑ после sweep↓")
    if in_bear_fvg and sweep_up is not None:
        s3 += 12; n3.append("В FVG↓ после sweep↑")

    # DOM
    if dom["imbalance"] > 25 and sweep_down is not None:
        s3 += 10; n3.append(f"DOM+{dom['imbalance']:.0f}%")
    if dom["imbalance"] < -25 and sweep_up is not None:
        s3 += 10; n3.append(f"DOM{dom['imbalance']:.0f}%")

    # OI Divergence после sweep подтверждает разворот
    if oi_div == "bull_div" and sweep_down is not None:
        s3 += 12; n3.append("OI_div↑ после sweep↓")
    if oi_div == "bear_div" and sweep_up is not None:
        s3 += 12; n3.append("OI_div↓ после sweep↑")

    scores["range_sweep"] = s3
    notes["range_sweep"]  = ", ".join(n3) or "—"

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

    # 10. Funding нейтральный или отрицательный = место для роста
    if funding < -0.015:
        s4 += 14; n4.append(f"fund{funding:.3f}%(сквиз)")
    elif -0.01 <= funding <= 0.01:
        s4 += 8; n4.append("fund≈0")
    elif funding > 0.025:
        s4 -= 8  # лонги перегреты = памп уже был

    # 11. MTF Confluence: структурная поддержка для роста
    if bull_mtf >= 3:
        s4 += 16; n4.append(f"MTF{bull_mtf}↑!")
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
    if ema_1h.get("ema_bull"):
        s4 += 14; n4.append("EMA_bull1H")
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

    # 18. RSI Дивергенция бычья → ранний сигнал разворота
    if rsi_div_1h == "bull_div":
        s4 += 16; n4.append("RSI_div↑!")
    elif rsi_div_1h == "hidden_bull":
        s4 += 10; n4.append("RSI_hid↑")
    elif rsi_1h < 30:
        s4 += 12; n4.append(f"RSI{rsi_1h:.0f}(OS)")  # перепроданность

    # 19. CHoCH бычий — ранний слом нисходящего тренда
    if choch_1h == "bull_choch":
        s4 += 16; n4.append("CHoCH↑1H!")
    elif choch_4h == "bull_choch":
        s4 += 14; n4.append("CHoCH↑4H!")

    # 20. Value Area: цена ниже VAL — перепродана относительно VA
    if val_dist is not None and val_dist < -2.0:
        s4 += 10; n4.append(f"belowVAL{val_dist:.1f}%")

    # 21. Equal Lows под ценой = магниты ликвидности (рынок придёт забрать)
    if eq_highs:
        s4 += 6; n4.append(f"EQH@{format_price(eq_highs[0])}")

    # 22. MTF Extended (1H+4H+1D)
    if bull_mtf_ext > bull_mtf:
        s4 += 8; n4.append(f"MTF_1D{bull_mtf_ext}")

    # 23. CHoCH медвежий = штраф (тренд меняется вниз)
    if choch_1h == "bear_choch" or choch_4h == "bear_choch":
        s4 -= 12

    scores["breakout"] = s4
    notes["breakout"]  = ", ".join(n4) or "—"

    # ── Лучший сетап ──
    best  = max(scores, key=scores.get)
    score = scores[best]

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
        "liq":            ("L" if long_liq else "") + ("S" if short_liq else "") or "—",
        "oi_div":         oi_div or "—",
        "fund_tr":        fund_trend,
        "pattern":        candle_pat or "—",
        "flags":          " ".join(flags) if flags else "—",
        "setup":          best,
        "score":          score,
        "notes":          notes[best],
        # Pre-pump метрики
        "atr_comp":       atr_compression,
        "oi_coil_%":      oi_coil_chg,
        "oi_coil_rng%":   oi_coil_rng,
        "oi_coiling":     oi_coiling,
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
        "eq_highs":      eq_highs,
        "eq_lows":       eq_lows,
        "bull_mtf_ext":  bull_mtf_ext,
        "bear_mtf_ext":  bear_mtf_ext,
    }


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
    oi = r["oi24h_%"]
    if oi < -10:
        add("OI 24h", f"{oi:+.1f}%", "ЛОНГ",
            "Сильное падение OI → крупные ликвидации прошли, позиции расчищены → дно близко")
    elif oi < -5:
        add("OI 24h", f"{oi:+.1f}%", "ЛОНГ",
            "OI упал → ликвидации состоялись, меньше давления со стороны лонгов")
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
            entry_low  = bfvg_bot
            entry_high = bfvg_top or price
            stop       = max(bfvg_bot - buf, 0)
            entry_note = f"в FVG↑  {format_price(bfvg_bot)} .. {format_price(bfvg_top)}"
            stop_note  = f"↓FVG↑ дно  {format_price(bfvg_bot)}"

        elif in_bob and bob_bot is not None:
            entry_low  = bob_bot
            entry_high = bob_top or price
            stop       = max(bob_bot - buf, 0)
            entry_note = f"в OB↑   {format_price(bob_bot)} .. {format_price(bob_top)}"
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
            tp1 = price + atr_abs * 1.5
            tp1_note = f"ATR×1.5   {format_price(tp1)}"

        # ── Шаг 3: TP2 = 48h high или дальняя цель ───────────────────────
        if hi48 is not None and hi48 > tp1:
            tp2 = hi48
            tp2_note = f"48h high  {format_price(hi48)}"
        else:
            tp2 = price + atr_abs * 2.5
            tp2_note = f"ATR×2.5   {format_price(tp2)}"

        # Sanity-check: stop должен быть НИЖЕ entry для лонга
        if stop >= entry_low:
            stop = max(entry_low - buf * 1.5, price * 0.001)
        rr_raw = (tp1 - entry_high) / max(entry_high - stop, price * 1e-4)
        rr = round(max(min(rr_raw, 20.0), 0.0), 2)  # clamp [0, 20]
        invalidation = f"закрытие ниже {format_price(stop)}  ({stop_note.strip()})"

    elif side == "short":

        # ── Шаг 1: точка входа + стоп ────────────────────────────────────
        if in_sfvg and sfvg_top is not None:
            entry_low  = sfvg_bot or price
            entry_high = sfvg_top
            stop       = sfvg_top + buf
            entry_note = f"в FVG↓  {format_price(sfvg_bot)} .. {format_price(sfvg_top)}"
            stop_note  = f"↑FVG↓ крыша {format_price(sfvg_top)}"

        elif in_sob and sob_top is not None:
            entry_low  = sob_bot or price
            entry_high = sob_top
            stop       = sob_top + buf
            entry_note = f"в OB↓   {format_price(sob_bot)} .. {format_price(sob_top)}"
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
            tp1 = max(price - atr_abs * 1.5, 0)
            tp1_note = f"ATR×1.5   {format_price(tp1)}"

        # ── Шаг 3: TP2 = 48h low ─────────────────────────────────────────
        if lo48 is not None and lo48 < tp1:
            tp2 = lo48
            tp2_note = f"48h low   {format_price(lo48)}"
        else:
            tp2 = max(price - atr_abs * 2.5, 0)
            tp2_note = f"ATR×2.5   {format_price(tp2)}"

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
        "invalidation": invalidation,
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
                f"{plan['rr']:.1f}",
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

def _fetch_and_score(sym, tickers, btc_chg_24h, bnb_map=None):
    """
    Воркер для параллельного выполнения.
    Делает все 8 API-запросов для одного символа и возвращает scored row.
    bnb_map: кросс-биржевые данные Binance {sym: {funding, oi_change, ...}} (опционально).
    """
    oi_hist      = fetch_oi_history(sym, limit=50)
    op1h, hi1h, lo1h, cl1h, vol1h = fetch_klines(sym, "60",  212)
    op4h, hi4h, lo4h, cl4h, vol4h = fetch_klines(sym, "240", 212)
    opD,  hiD,  loD,  clD,  volD  = fetch_klines(sym, "D",    52)
    funding_hist = fetch_funding_history(sym, limit=8)
    ls_ratio     = fetch_ls_ratio(sym)
    trades       = fetch_recent_trades(sym, 1000)   # реальный CVD: 1000 сделок
    bids, asks   = fetch_orderbook(sym, 50)

    result = score_symbol(
        sym, tickers[sym], oi_hist,
        op1h, hi1h, lo1h, cl1h, vol1h,
        op4h, hi4h, lo4h, cl4h, vol4h,
        opD,  hiD,  loD,  clD,  volD,
        ls_ratio, trades, bids, asks,
        funding_hist, btc_chg_24h,
    )

    # Кросс-биржевое подтверждение (Binance)
    if result is not None and _BNB_AVAILABLE and bnb_map is not None:
        _bnb.apply_cross_bonus(result, bnb_map.get(sym))

    return result


def run_screener(top_n=50, min_score=35,
                 watchlist_size=5, deep_dive_size=3,
                 export_json=None, export_csv=None,
                 obsidian=False):
    print(f"\n{'='*72}")
    print(f"  Bybit Futures Screener  |  {datetime.now().strftime('%H:%M:%S  %d.%m.%Y')}")
    print(f"{'='*72}")

    print("Загружаю тикеры Bybit...")
    tickers = fetch_all_tickers()
    symbols = sorted(
        tickers.keys(),
        key=lambda s: float(tickers[s].get("turnover24h", 0)),
        reverse=True,
    )[:top_n]

    # BTC 24h change для Relative Strength
    btc_chg_24h = float(tickers.get("BTCUSDT", {}).get("price24hPcnt", 0)) * 100

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
            pool.submit(_fetch_and_score, sym, tickers, btc_chg_24h, bnb_map): sym
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

    # ── Telegram alerts ────────────────────────────────────────────────────────
    if _TG_AVAILABLE:
        tg_cfg = _tg.load_config()
        if tg_cfg.get("enabled") and tg_cfg.get("bot_token") and tg_cfg.get("chat_id"):
            print("[TG] Отправляю отчёт в Telegram...")
            _tg.send_report(
                results=results,
                filtered=filtered,
                btc_chg_24h=btc_chg_24h,
                session_info=session_info,
                fg_value=fg_value,
                fg_label=fg_label,
                deep_dive_data=deep_dive_data,
                cfg=tg_cfg,
            )
            print("[TG] Готово.")

    # ── Outcome tracker ────────────────────────────────────────────────────────
    if _OT_AVAILABLE:
        # Сначала проверяем старые сигналы (прошло ≥4h или ≥24h)
        resolved = _ot.check_and_resolve(silent=True)
        if resolved:
            print(f"[Tracker] Закрыто исходов: {resolved} → outcome_tracker.py stats")
        # Сохраняем текущие сигналы как pending
        saved = _ot.save_pending(filtered, results)
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
    )


if __name__ == "__main__":
    main()

"""
pump_detector.py — ОПЕРЕЖАЮЩИЙ детектор памп/раг движений.

Ловит сигналы ДО того как цена двинулась, не после.

Стадии памп-сигнала:
  🟡 ACCUMULATION — OI растёт, цена стоит; кто-то тихо набирает
  🟠 PRE-BREAKOUT  — CVD дивергенция + фандинг восстанавливается от негатива
  🔴 SQUEEZE SETUP — sweep ниже поддержки + быстрый возврат; пружина заряжена

Стадии раг-сигнала:
  ⚠  STEALTH SHORT  — OI растёт + фандинг растёт; кто-то строит шорт перед дампом
  🚨 LIQUIDITY DRAIN — цена растёт но CVD падает (распределение); или DEX LP утекает

Запуск:
    python3 pump_detector.py            # разовый скан
    python3 pump_detector.py watch      # фоновый режим (каждые 5 мин)
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from file_lock import atomic_json_update, atomic_json_read

# ── FD-leak fix (2026-05-30, аудит #10): единый session с Connection:close для всех HTTP ──
# Раньше bare `_req.get` (keep-alive) на горячих путях (_fetch_oi_5m/_fetch_trades_raw/kline-extremes)
# обходил фикс screener.SESSION → CLOSE_WAIT копился к Bybit → EMFILE → бот бричился каждые ~3ч.
import requests as _requests
_HTTP = _requests.Session()
_HTTP.headers.update({"Connection": "close"})
_HTTP_ADAPTER = _requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=50, max_retries=0)
_HTTP.mount("https://", _HTTP_ADAPTER)
_HTTP.mount("http://", _HTTP_ADAPTER)

# ── Грязный выход на py3.14 fix (2026-06-07, этап 10) ──────────────────────────
# Раньше каждый async-батч в scan_once звал свой asyncio.run() → новый event loop
# на каждый вызов. async_http держит ОДИН ClientSession на loop: при смене loop
# старый session БРОСАЛСЯ без await-закрытия (async_http._get_session: _SESSION=None),
# а сами loop-объекты копились и собирались GC позже. На py3.14 BaseEventLoop.__del__
# → close() → _close_self_pipe() падал с AttributeError '_ssock' (×569 в error.log) —
# "Exception ignored while calling deallocator" при завершении демона.
# Лечение: ОДИН резидентный loop на всю жизнь демона (модель asyncio.run(main())),
# session закрывается через async_http.close_session() корректным await ДО выхода.
_LOOP: Optional[asyncio.AbstractEventLoop] = None


def _arun(coro):
    """Гоняет coroutine на одном резидентном loop вместо asyncio.run() на каждый вызов.

    Так aiohttp-session (async_http._SESSION) живёт на одном loop весь скан и не
    бросается недозакрытым при каждой смене loop — без этого py3.14 шумит _ssock'ом
    при сборке брошенных loop-объектов на выходе демона.
    """
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _shutdown_loop():
    """Корректно гасит резидентный loop: await-закрытие aiohttp-session + close loop.

    Зовётся из пути завершения демона (SIGTERM/KeyboardInterrupt) и one-shot CLI.
    Закрытие session ДО close(loop) убирает источник _ssock-шума.
    """
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        return
    try:
        from async_http import close_session
        _LOOP.run_until_complete(close_session())
    except Exception:
        pass
    try:
        _LOOP.run_until_complete(_LOOP.shutdown_asyncgens())
    except Exception:
        pass
    _LOOP.close()
    _LOOP = None

# ── Пороги ────────────────────────────────────────────────────────────────────

# Accumulation: OI растёт, цена стоит
OI_ACCUM_4H        =  5.0   # OI +5% за 4ч (был 4.0)
PRICE_FLAT_4H      =  1.5   # цена ±1.5% за 4ч (не двигается)

# CVD дивергенция: скрытая покупка/распределение
CVD_BULL_DIV       = 30.0   # CVD > +30%, цена < +0.5% (был 20.0)
CVD_BEAR_DIV       = -45.0  # CVD < -45%, цена > -0.5% → распределение (был -20.0)

# Funding: шорты закрываются → сквиз
FUND_RECOVERY_FROM = -0.04  # % — было сильно негативным
FUND_RECOVERY_TO   = -0.01  # % — теперь почти нейтральный
FUND_STEALTH_SHORT =  0.04  # % — фандинг растёт вместе с OI → шорт-билд

# Sweep recovery: вынос + возврат
SWEEP_LOOKBACK_H   = 3      # смотрим последние 3 свечи

# Качество сигнала
MIN_SIGNAL_SCORE   = 35     # база 45/50 минус штрафы (BTC медвежий и т.д.)
STRONG_SOLO_SCORE  = 80     # solo-паттерн без confluence разрешён только при score≥80 (↑ было 65)

# Скан
SCAN_INTERVAL      = 300    # секунд
MIN_TURNOVER_USD   = 2_000_000   # $2M мин USD-оборот (turnover24h, не токены)
TOP_N_SYMBOLS      = 100    # топ-100 по объёму (было 60; расширено для #61-100)
WATCH_TOP_N        = 2      # топ-2 за скан (снайпер, не радар)

# Whitelist: символы вне топ-100 которые всегда анализируются
SYMBOL_WHITELIST   = {"ARUSDT", "ZEREBROUSDT"}
LOG_PREFIX         = "[Pump]"

COOLDOWN_SEC       = 14400  # 4 часа между алертами на один символ (↑ было 1ч)
_sent_recently: dict[str, float] = {}
_fng_cache: dict = {}                   # {"value": int, "label": str, "ts": float}

# ── Momentum trigger ───────────────────────────────────────────────────────────
# Кандидат не шлётся сразу — ждём реального движения цены
MOMENTUM_PCT        = 0.50   # % движения от точки обнаружения (под раскачку, не скальп)
WATCH_EXPIRY_H      = 4      # часов: кандидат истекает без движения (default)
# Адаптивный TTL под тип паттерна
WATCH_EXPIRY_BY_STAGE = {
    "🔴 СКВИЗ SETUP":   4,    # SHORT_SQUEEZE — быстрый паттерн
    "🚨 ПОСТ-ПАМП РАГ":  6,    # POST_PUMP_RUG — раг 20-60 мин, но даём буфер
    "🐢 SLOW DIST":     24,    # SLOW_DIST — медленный паттерн 1-3 дня
    "📊 VOLUME SURGE":   4,
    "📈 CVD BULL DIV":   4,
    "📉 CVD BEAR DIV":   4,
    "🌀 ATR COIL":       8,    # ATR coil — expansion может быть в любой час
}
MOMENTUM_CHECK_SEC  = 120    # секунд между проверками движения (лёгкий запрос)
MOMENTUM_BURST_PCT  = 0.20   # % движения за последние 5 мин — реальный импульс (не дрейф)
MOMENTUM_BURST_INTV = "5"    # таймфрейм kline для burst-проверки (5м)
MOMENTUM_1H_BLOCK   = 0.50   # 1H свеча против направления > 0.50% → блок
_watch_candidates: dict[str, dict] = {}  # sym → {candidate, base_price, detected_at}

# ── BTC Regime filter ──────────────────────────────────────────────────────────
BTC_BEAR_THRESHOLD = -2.0   # BTC 4h < -2% → медвежий рынок
BTC_BEAR_PENALTY   = 25     # макс штраф к скору пампа
BTC_BULL_THRESHOLD =  1.0   # BTC 4h > +1% → бычий попутный ветер
BTC_BULL_BONUS     = 10     # бонус к скору пампа

# ── TLDB derived rules (только те что можно оценить без RSI/VWAP) ──────────────
TLDB_FUND_EXTREME_NEG = -0.05   # funding_regime:fund_extreme_neg → штраф пампам
TLDB_OI_SURGE_SOLO    = 15.0    # oi_regime:oi_surge без доп. факторов → штраф

# ── FundGate (2026-05-29): pump-LONG при сильно отрицательном funding ──────────
# pump-LONG (squeeze-long) при funding ≤ -0.5% = 0 WIN из 19 (outcomes/pump_resolved.csv,
# n=152). Чем минуснее funding — тем хуже (≤ -1.5%: 0/4, avg chg_4h -8.2%). Скоринг
# сквиза (см. блок SHORT SQUEEZE ниже) НАГРАЖДАЕТ neg funding → высокий score у
# статистически ХУДШИХ. Режется в send_pump_alert ДО вызова Claude; rug_prep не трогаем.
PUMP_LONG_FUND_BLOCK = -0.5

# ── PumpGate (2026-05-30, backtest net-of-costs): pump-LONG убыточен ДАЖЕ gross ──
# mean −0.16%/сделку gross, −0.47% после реал-издержек (fee+slip), score НЕ разделяет
# (score≥120 → −1.94%). rug_prep (шорт) — единственный +EV-кандидат, НЕ трогаем.
#
# Режим доставки pump-LONG (env PUMP_LONG_MODE, по умолч. off):
#   off     — алерт подавлен (shadow-лог), поведение с 2026-05-30 (−EV)
#   observe — слать ЛУЧШИЕ (FundGate-pass + conviction≥65 + Claude GO) с плашкой
#             «🧪 ЭКСПЕРИМЕНТ −EV» для форвард-наблюдения. Отдельный дневной бюджет
#             (MAX_DAILY_PUMP_OBSERVE) — НЕ ворует лимит у rug.
#   on      — слать как обычный сигнал (общий лимит, без плашки) — ТОЛЬКО если
#             форвард-тест докажет восстановление эджа pump-LONG.
PUMP_LONG_MODE = os.environ.get("PUMP_LONG_MODE", "off").strip().lower()
if PUMP_LONG_MODE not in ("off", "observe", "on"):
    PUMP_LONG_MODE = "off"
PUMP_LONG_ENABLED = PUMP_LONG_MODE != "off"   # back-compat со старым флагом

# ── Real CVD ───────────────────────────────────────────────────────────────────
REAL_CVD_LIMIT     = 150    # последних сделок для расчёта taker CVD

# ── TLDB: UTC session ──────────────────────────────────────────────────────────
# NY session = UTC 13-21 (риск: pump в NY с neg фандингом)
# Off session = UTC 22-06 (риск: rug без участников для выхода)
_SESSION_NY_START  = 13
_SESSION_NY_END    = 21

# ── TLDB: RS vs BTC (relative strength) ───────────────────────────────────────
# rs_btc_weak: монета отстаёт от BTC на > 2% за 4ч — памп-кандидат слабее рынка
# rs_btc_pos: монета опережает BTC на > 2% — хороший знак для пампа
RS_BTC_WEAK_MARGIN = -2.0   # price_chg_4h - btc_4h < -2% → rs_btc_weak
RS_BTC_POS_MARGIN  =  2.0   # price_chg_4h - btc_4h > +2% → rs_btc_pos

# ── Качество: conviction gate + DEXscreener + дневной лимит ───────────────────
CONVICTION_MIN_SCORE = 65   # 2026-05-30: кредиты пополнены ($50) → RT-фильтр снова вычитывает → вернул 85→65 (откалиброванное; 85 был временный пластырь на fail-open период). Калибровка: GO score<70 дали 0 WIN; 65 — компромисс, можно поджать к 70 при росте выборки
MAX_DAILY_ALERTS     = 5    # снижен с 8 — цель «1-2 идеальных в день»
_daily_alerts: dict[str, int] = {}   # {date_str: count}
# Отдельный бюджет наблюдательного pump-observe (env PUMP_LONG_MODE=observe), чтобы
# эксперимент НЕ воровал лимит у tradeable-сигналов (rug). In-memory, НЕ персистится:
# наблюдательные алерты не торгуются — сброс при рестарте безвреден.
MAX_DAILY_PUMP_OBSERVE = 4
_daily_pump_obs: dict[str, int] = {}   # {date_str: count}

def _is_obs_pump(c: dict) -> bool:
    """True для pump-LONG в наблюдательном режиме (отдельный бюджет + плашка)."""
    return c.get("signal_type") == "pump" and PUMP_LONG_MODE == "observe"

def _daily_count(today: str, obs: bool) -> int:
    return (_daily_pump_obs if obs else _daily_alerts).get(today, 0)

def _daily_cap(obs: bool) -> int:
    return MAX_DAILY_PUMP_OBSERVE if obs else MAX_DAILY_ALERTS

def _daily_bump(today: str, obs: bool) -> None:
    d = _daily_pump_obs if obs else _daily_alerts
    d[today] = d.get(today, 0) + 1

# ── Персист cooldown/дневного лимита (FIX 2026-05-30: рестарт демона обнулял in-memory → дубли + обход MAX_DAILY_ALERTS) ──
_ALERT_STATE_FILE = Path(__file__).parent / "pump_alert_state.json"

def _save_alert_state():
    try:
        now_ts = time.time()
        # FIX 2026-05-30: прунинг stale (>COOLDOWN_SEC) перед персистом — диск-файл не растёт безгранично
        # (симметрично фильтру в _load_alert_state). In-memory dict растёт до рестарта (~negligible). Single-writer
        # демон → merge не нужен, overwrite безопасен. Вступает в силу при следующем рестарте демона.
        pruned_sent = {_s: _t for _s, _t in _sent_recently.items() if now_ts - _t < COOLDOWN_SEC}
        atomic_json_update(
            _ALERT_STATE_FILE,
            lambda _d: {"sent_recently": pruned_sent, "daily_alerts": dict(_daily_alerts)},
            default={},
        )
    except Exception as _e:
        _log(f"[AlertState] не сохранил: {_e}")

def _load_alert_state():
    try:
        d = atomic_json_read(_ALERT_STATE_FILE, default={}) or {}
        now_ts = time.time()
        today_s = datetime.now().strftime("%Y-%m-%d")
        for _sym, _ts in (d.get("sent_recently") or {}).items():
            try:
                if now_ts - float(_ts) < COOLDOWN_SEC:
                    _sent_recently[_sym] = float(_ts)
            except Exception:
                pass
        _da = d.get("daily_alerts") or {}
        if today_s in _da:
            _daily_alerts[today_s] = int(_da[today_s])
        _log(f"[AlertState] загружено: cooldown {len(_sent_recently)} симв, сегодня {_daily_alerts.get(today_s, 0)}/{MAX_DAILY_ALERTS}")
    except Exception as _e:
        _log(f"[AlertState] не загрузил: {_e}")

# ── LIVE-БЕЗОПАСНОСТЬ (2026-05-28, ревью pump_detector) ───────────────────────
# Fail-CLOSED: при FAIL_OPEN RT-фильтра (кредиты Claude кончились) ИЛИ ошибке фильтра
# НЕ слать невычитанный сигнал. Раньше FAIL_OPEN→слали; при анти-предиктивном
# score-гейте (CONVICTION=85) это отправляло статистически ХУДШИЕ сигналы без вычитки.
# Вернуть в False (старый fail-open) ТОЛЬКО когда кредиты восстановлены и RT снова вычитывает.
FAIL_CLOSED_PUMP = True
# Kill-switch по серии лоссов (общий streak_monitor, как у screener.py:6457)
try:
    import streak_monitor as _streak
    _STREAK_AVAILABLE = True
except Exception:
    _STREAK_AVAILABLE = False
# Shadow-лог отклонённых сигналов (как screener: rejected.json + resolve_rejects позже).
# Нужен чтобы валидировать FundGate: реально ли блокнутые pump-LONG продолжают терять.
try:
    import reject_tracker as _rt
    _RT_AVAILABLE = True
except Exception:
    _RT_AVAILABLE = False

# ── Bybit base ─────────────────────────────────────────────────────────────────
_BYBIT_BASE       = "https://api.bybit.com"

# ── Outcomes tracking ──────────────────────────────────────────────────────────
_OUTCOMES_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outcomes")
PUMP_PENDING_FILE = os.path.join(_OUTCOMES_DIR, "pump_pending.json")
PUMP_RESOLVED_FILE= os.path.join(_OUTCOMES_DIR, "pump_resolved.csv")
# Пороги исхода и _outcome — единый источник правды (см. outcome_model.py)
from outcome_model import (
    PUMP_WIN_4H_PCT, PUMP_LOSS_4H_PCT, RUG_WIN_4H_PCT, RUG_LOSS_4H_PCT,
    outcome as _outcome,
)

# ── Liquidation cluster booster ────────────────────────────────────────────────
LIQ_DB_PATH       = os.path.join(os.path.dirname(__file__), "liquidations.db")
LIQ_WINDOW_MS     = 4 * 3600 * 1000   # смотрим кластеры за последние 4ч

# ── Price position gate ────────────────────────────────────────────────────────
PRICE_TOP_ZONE    = 0.85   # цена в топ-15% диапазона → памп мог уйти, штраф
PRICE_BOT_ZONE    = 0.20   # цена в низ-20% → свежая зона для пампа, бонус
PRICE_POS_PENALTY = 20     # штраф если памп-сигнал на хаях
PRICE_POS_BONUS   =  8     # бонус если памп-сигнал у дна

# ── OI Coil smart classification ───────────────────────────────────────────────
ABSORPTION_CVD_MIN =  15.0  # CVD ≥+15% при OI flat → ABSORPTION_BULL
TRAP_CVD_MAX       = -10.0  # CVD ≤-10% при OI накоплении → TRAP (шорт строится)

# ── Absorption detector ────────────────────────────────────────────────────────
ABSORB_RANGE_RATIO =  0.65  # диапазон последней свечи < 65% ср. → сжатие/поглощение
ABSORB_SCORE       =  25    # добавка к скору за поглощение

# ── ATR compression ────────────────────────────────────────────────────────────
ATR_COMPRESS_RATIO =  0.65  # recent_atr < 65% hist_atr → пружина сжата
ATR_COMPRESS_BONUS =  15    # бонус при сжатом ATR для памп-сигналов

# ── Whale detection ────────────────────────────────────────────────────────────
WHALE_RATIO_MIN    =  20    # max_trade_usd ≥ 20× avg → кит входит
WHALE_SCORE        =  12    # бонус к скору при ките

# ── Cross-exchange OI (Binance подтверждение) ──────────────────────────────────
CROSS_OI_CONFIRM_PCT    =  3.0  # Binance OI +3% за 4ч → подтверждает Bybit паттерн
CROSS_OI_FLAT_PCT       =  1.0  # Binance OI < 1% → движение только на Bybit
CROSS_OI_CONFIRM_BONUS  =  15   # обе биржи накапливают → институционал
CROSS_OI_BYBIT_ONLY_PEN =  10   # только Bybit активен → слабее сигнал

# ── Funding velocity ───────────────────────────────────────────────────────────
FUND_VEL_DROP      = -0.02  # фандинг падает ≥0.02% → сквиз-потенциал, бонус
FUND_VEL_SURGE     =  0.03  # фандинг растёт ≥0.03% → FOMO-риск, штраф
FUND_VEL_BONUS     =  10
FUND_VEL_PENALTY   =  10

# ── Synergy Layer ──────────────────────────────────────────────────────────────
SYN_ABSORP_SWEEP      = 20   # ABSORPTION_BULL + sweep → лучший reversal
SYN_ABSORP_COMPRESS_V = 25   # ABSORPTION_BULL + range_compressed + vol ≥ 2 → тройное подтверж.
SYN_ABSORP_COMPRESS   = 10   # ABSORPTION_BULL + range_compressed (без объёма)
SYN_ABSORP_FUND       = 10   # ABSORPTION_BULL + funding recovery → сквиз в накоплении
SYN_ABSORPDET_SWEEP   = 15   # Absorption Detector + sweep → buy the bottom

# Anti-Synergy
ANTI_LATE_VOL         = 15   # LATE_LONGS + vol > 2 → FOMO-ловушка
ANTI_PRICE_NO_CVD     = 20   # цена >2% + CVD нейтральный/отриц. → распределение
ANTI_OI_MOVE_NOSTR    = 25   # OI>6 + цена>1.5% без структуры → толпа входит
ANTI_OI_MOVE_STR      =  5   # OI>6 + цена>1.5% + структура есть → осторожно

# ── Breakout confirmation (Variant C) ─────────────────────────────────────────
BREAKOUT_HOLD_PCT     = 1.001   # price > recent_high × 1.001 (0.1% выше)
BREAKOUT_CLOSE_PCT    = 1.0005  # closes[-2] > recent_high × 0.0005 — подтверждение закрытой свечой
BREAKOUT_IMPULSE      = 0.10    # (price - high) / range > 10% — импульс пробоя
BREAKOUT_VOL_MIN      = 1.4     # минимальный vol_surge для подтверждения

# ── Soft + Hard rejection ──────────────────────────────────────────────────────
SOFT_LOW_VOL_THRESH   = 1.2    # vol_surge < 1.2 → −15 (pump без объёма)
SOFT_LOW_VOL_PENALTY  = 25
HARD_REJ_PRICE_VOL    = (2.0, 1.5)   # price > 2% + vol < 1.5 → return None
HARD_REJ_LATE_PUMP    = (3.5, 2.0)   # price > 3.5% + vol < 2.0 → уже поздно

# ── Time-gate (зеркало screener.py BAD/HARD_BLOCK_HOURS, адаптировано для pump) ──
# WR данные те же что в screener: 01=40.9%, 12=28.6%, 18=31.2%, 19=12.5%
_PUMP_BAD_HOURS       = {1, 13, 14, 20, 23, 0}   # плохой WR — нужен высокий score
_PUMP_HARD_BLOCK_HOURS= {18, 19}                  # только самые плохие часы — Claude фильтрует остальное
_PUMP_BAD_MIN_SCORE   = 90   # снижен с 115 — Claude RT-фильтр решит финально

# ── Multi-hour pump detection ──────────────────────────────────────────────────
LATE_PUMP_8H_HARD  = 12.0  # price > +12% за 8h → памп уже прошёл, hard reject
LATE_PUMP_8H_SOFT  =  8.0  # price > +8% за 8h + слабый объём → поздно, hard reject
LATE_PUMP_8H_VOL   =  2.5  # мин. vol_surge чтобы +8% за 8h не был блокирован

# ── Funding surge hard block ───────────────────────────────────────────────────
FUND_SURGE_BLOCK_TREND = 0.04   # фандинг растёт > +0.04% за период → FOMO-ловушка
FUND_SURGE_BLOCK_ABS   = 0.03   # И абсолютный фандинг > +0.03% → блок пампа

# ── POST_PUMP_DIST: распределение после параболического пампа ─────────────────
POST_PUMP_PRICE_MIN  = 8.0   # минимальный рост за 4h для паттерна
POST_PUMP_FUND_MIN   = 0.05  # фандинг перегрет (лонги переплачивают)
POST_PUMP_CVD_MAX    = 10.0  # CVD не подтверждает рост — продают в ралли
POST_PUMP_OI_MAX     = 2.0   # OI не растёт — умные деньги не входят

# ── SLOW_DIST: медленная дистрибуция за 2-3 дня (slow rug) ────────────────────
# Цена растёт не параболически, +10..+30% за 3 дня; funding медленно нагревается,
# но без перегрева; объёмы стабильные (нет одиночного экстремума). Шорт-кандидат.
SLOW_DIST_3D_MIN     = 10.0   # минимальный рост 3д для паттерна
SLOW_DIST_3D_MAX     = 30.0   # верхняя граница (выше — уже параболика, ловит POST_PUMP_RUG)
SLOW_DIST_FUND_MIN   = 0.010  # funding > +0.010% — нагрев есть
SLOW_DIST_FUND_MAX   = 0.040  # funding < +0.040% — но не перегрет
SLOW_DIST_VOL_MAX_R  = 3.0    # max/min vol за 3 дня < 3.0 — равномерное накопление

# ── VOLUME_SURGE: резкий объёмный спайк, цена ещё не отреагировала ─────────────
# Кто-то покупает агрессивно, но цена пока стоит. Early signal до движения.
VOL_SURGE_MIN        = 2.5    # vol last/avg10 >= 2.5×
VOL_SURGE_PRICE_MAX  = 1.0    # abs(price_chg_30m) <= 1% — цена ещё не двинулась
VOL_SURGE_OI_MIN     = 0.3    # OI 30m >= +0.3% — позиции открываются

# ── CVD_BULL_DIV_SOLO: сильная CVD-дивергенция без других подтверждений ───────
# Тейкеры агрессивно покупают, цена ещё не реагирует — скрытое накопление.
CVD_BULL_DIV_PCT     = 50.0   # CVD >= +50%
CVD_BULL_DIV_PMAX    = 0.3    # price_chg_30m <= +0.3% — цена ещё не отреагировала

# ── CVD_BEAR_DIV_SOLO: тейкеры агрессивно продают на растущей цене ────────────
CVD_BEAR_DIV_PCT     = -50.0  # CVD <= -50%
CVD_BEAR_DIV_PMIN    = -0.3   # price_chg_30m >= -0.3% — цена ещё не упала

# ── ATR_COIL: ATR сжимается, цена в узком диапазоне — пружина перед движением ─
ATR_COIL_RATIO       = 0.55   # recent_atr / hist_atr < 0.55
ATR_COIL_VOL_MIN     = 1.2    # vol_surge >= 1.2 — есть какая-то активность

# ── Fear & Greed Index ─────────────────────────────────────────────────────────
FNG_EXTREME_FEAR     = 25   # < 25 → Extreme Fear → контрарианский бонус пампу
FNG_EXTREME_GREED    = 75   # > 75 → Extreme Greed → FOMO риск / бонус ругу
FNG_PUMP_FEAR_BONUS  = 10
FNG_PUMP_GREED_PEN   = 10
FNG_RUG_GREED_BONUS  = 10
FNG_CACHE_TTL        = 3600  # секунды, 1ч

# ── CryptoPanic ────────────────────────────────────────────────────────────────
PANIC_MIN_SCORE      = 70   # не вызывать для слабых сигналов
PANIC_BEARISH_PEN    = 15

# ── VALIDATED_V1: статистически подтверждённые факторы пампов ─────────────────
# Источник: PUMP_PATTERNS_ANALYSIS.md (N=1664 событий, 100 символов, walk-forward OOS).
# НЕ включать до прохождения бэктеста AVEC-80. Решение пользователя.
USE_VALIDATED_V1 = False


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{LOG_PREFIX} [{ts}] {msg}", flush=True)


def _cvd_pts(cvd_abs: float) -> int:
    """Score contribution scaled to CVD divergence magnitude (not flat 30)."""
    if cvd_abs >= 80: return 55
    if cvd_abs >= 50: return 45
    if cvd_abs >= 30: return 35
    return 25




# ── DEXscreener: spot-подтверждение ───────────────────────────────────────────

def fetch_dexscreener_spot(symbol: str) -> dict:
    """
    Проверяет spot/DEX активность токена через DEXscreener API (бесплатно).
    Возвращает {} если токен не найден или API недоступен.
    """
    base = symbol.upper()
    for suffix in ("USDT", "USDC", "BUSD", "USD"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    try:
        import requests as _req
        resp = _HTTP.get(
            f"https://api.dexscreener.com/latest/dex/search?q={base}",
            timeout=5,
            headers={"Accept": "application/json"}
        )
        pairs = (resp.json().get("pairs") or [])
        liquid = [
            p for p in pairs
            if float((p.get("liquidity") or {}).get("usd", 0) or 0) >= 30_000
        ]
        if not liquid:
            return {}
        best = max(liquid, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
        vol      = best.get("volume")      or {}
        txns     = best.get("txns")        or {}
        pchg     = best.get("priceChange") or {}
        h1_txns  = txns.get("h1")          or {}
        vol_h1   = float(vol.get("h1")  or 0)
        vol_h6   = float(vol.get("h6")  or 0)
        vol_h24  = float(vol.get("h24") or 0)
        avg_h    = vol_h6 / 6 if vol_h6 > 0 else 0
        # vol_ratio: насколько последний час активнее среднечасового за 6h
        ratio    = round(vol_h1 / avg_h, 2) if avg_h > 200 else 0.0
        buys     = int(h1_txns.get("buys",  0))
        sells    = int(h1_txns.get("sells", 0))
        bs_ratio = round(buys / max(sells, 1), 2)
        return {
            "dex_chain":      best.get("chainId", "?"),
            "dex_vol_h1":     vol_h1,
            "dex_vol_h6":     vol_h6,
            "dex_vol_h24":    vol_h24,
            "dex_vol_ratio":  ratio,
            "dex_bs_ratio":   bs_ratio,
            "dex_price_h1":   float(pchg.get("h1")  or 0),
            "dex_price_h6":   float(pchg.get("h6")  or 0),
            "dex_liquidity":  float((best.get("liquidity") or {}).get("usd", 0) or 0),
            "dex_pair_sym":   (best.get("baseToken") or {}).get("symbol", base),
        }
    except Exception:
        return {}


def _dex_score_adj(dex: dict, signal_type: str = "pump") -> int:
    """Знаковая поправка к score на основе DEX активности.

    Для pump: падение спота = плохо, рост покупателей = хорошо.
    Для rug_prep: логика инвертируется — падение спота ПОДТВЕРЖДАЕТ распределение.
    """
    if not dex:
        return 0
    vr   = dex.get("dex_vol_ratio", 0)
    bs   = dex.get("dex_bs_ratio",  1.0)
    ph1  = dex.get("dex_price_h1",  0)
    h24  = dex.get("dex_vol_h24",   0)

    # ── КРИТИЧНО: спот падает на DEX = распределение в фьючерсах ────────────
    if ph1 <= -3.0:
        raw = -30
    elif ph1 <= -2.0:
        raw = -20
    elif ph1 <= -1.0 and bs < 1.0:
        raw = -15
    elif bs < 0.85 and h24 > 0:
        raw = -10
    elif vr >= 3.0 and bs >= 1.5:
        raw = 25
    elif vr >= 2.0 and bs >= 1.2:
        raw = 15
    elif vr >= 1.5 or (ph1 >= 2.0 and bs >= 1.2):
        raw = 8
    elif vr == 0 and h24 > 0:
        if dex.get("dex_liquidity", 0) > 100_000 and dex.get("dex_vol_h1", 0) == 0:
            raw = -15
        else:
            raw = 0
    elif 0 < vr < 0.4:
        raw = -12
    else:
        raw = 0

    # Для rug_prep инвертируем знак: падение спота = подтверждение шорта,
    # рост покупателей на споте = против медвежьего тезиса.
    if signal_type == "rug_prep":
        return -raw
    return raw


def _fmt_dex(dex: dict) -> str:
    """Форматирует DEX данные для TG алерта."""
    if not dex:
        return ""
    vr   = dex.get("dex_vol_ratio", 0)
    bs   = dex.get("dex_bs_ratio",  1.0)
    liq  = dex.get("dex_liquidity", 0)
    ch   = dex.get("dex_chain",     "?")
    v1k  = dex.get("dex_vol_h1",    0) / 1000
    ph1  = dex.get("dex_price_h1",  0)
    ph6  = dex.get("dex_price_h6",  0)
    h24  = dex.get("dex_vol_h24",   0)
    if vr > 0:
        price_str = f"  Δ1h={ph1:+.1f}%" if abs(ph1) >= 0.5 else ""
        return (f"DEX [{ch}]: ${v1k:.0f}K/h ×{vr:.1f}  "
                f"B/S {bs:.1f}  liq ${liq/1000:.0f}K{price_str}")
    if h24 > 0:
        return (f"DEX [{ch}]: тихо h1, 24h vol ${h24/1000:.0f}K  "
                f"liq ${liq/1000:.0f}K  6h={ph6:+.1f}%")
    return f"DEX [{ch}]: нет DEX активности (liq ${liq/1000:.0f}K)"

# ── Bybit Long/Short Account Ratio ────────────────────────────────────────────
# CoinGlass шифрует все API ответы; Bybit публичный API даёт те же данные бесплатно.

def fetch_coinglass_ls_ratio(symbol: str) -> dict:
    """
    Получает Long/Short Ratio по аккаунтам через Bybit публичный API.
    Endpoint: /v5/market/account-ratio (без ключа, ~0.6с).
    Возвращает {"long_pct": float, "short_pct": float, "ls_ratio": float} или {}.
    ls_ratio — доля лонг-аккаунтов [0..1] (0.553 = 55.3% лонгов).
    """
    try:
        import requests as _req
        resp = _HTTP.get(
            f"{_BYBIT_BASE}/v5/market/account-ratio",
            params={"category": "linear", "symbol": symbol.upper(), "period": "1h", "limit": "1"},
            timeout=6,
        )
        row = resp.json()["result"]["list"][0]
        buy_f  = float(row["buyRatio"])
        sell_f = float(row["sellRatio"])
        if buy_f <= 0:
            return {}
        return {
            "long_pct":  round(buy_f  * 100, 2),
            "short_pct": round(sell_f * 100, 2),
            "ls_ratio":  round(buy_f,  4),
        }
    except Exception:
        return {}


# ── Binance cross-exchange OI ─────────────────────────────────────────────────

def fetch_binance_oi_4h_change(symbol: str) -> Optional[float]:
    """
    Изменение OI на Binance Futures за последние ~4ч (5 точек × 1ч).
    Binance символы совпадают с Bybit linear: BTCUSDT, ETHUSDT, etc.
    Возвращает float % или None при ошибке / недоступности.
    """
    try:
        import requests as _req
        rows = _HTTP.get(
            "https://fapi.binance.com/futures/data/openInterestHist",
            params={"symbol": symbol.upper(), "period": "1h", "limit": "5"},
            timeout=6,
        ).json()
        if not isinstance(rows, list) or len(rows) < 2:
            return None
        oi_now = float(rows[-1]["sumOpenInterest"])
        oi_4h  = float(rows[0]["sumOpenInterest"])
        if oi_4h <= 0:
            return None
        return round((oi_now - oi_4h) / oi_4h * 100, 2)
    except Exception:
        return None


# ── Fear & Greed Index ─────────────────────────────────────────────────────────

def get_fear_greed() -> Optional[int]:
    """
    Fear & Greed Index (alternative.me). Кэш 1ч — один HTTP-запрос на весь скан.
    Возвращает int [0..100] или None при ошибке/недоступности.
    """
    global _fng_cache
    if _fng_cache and time.time() - _fng_cache.get("ts", 0) < FNG_CACHE_TTL:
        return _fng_cache["value"]
    try:
        import requests as _req
        data = _HTTP.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=5,
        ).json()["data"][0]
        _fng_cache = {
            "value": int(data["value"]),
            "label": data.get("value_classification", ""),
            "ts":    time.time(),
        }
        return _fng_cache["value"]
    except Exception:
        return None


# ── CryptoPanic ────────────────────────────────────────────────────────────────

def fetch_crypto_panic_sentiment(symbol: str) -> Optional[str]:
    """
    Последние 3 новости за 2ч по символу через CryptoPanic public API (без ключа).
    Возвращает "bearish" / "bullish" / None.
    """
    try:
        import requests as _req
        coin = symbol.upper().replace("USDT", "")
        if coin.startswith("1000"):
            coin = coin[4:]
        params: dict = {"public": "true", "currencies": coin, "kind": "news"}
        _token = os.environ.get("CRYPTOPANIC_API_KEY", "")
        if _token:
            params["auth_token"] = _token
        rows = _HTTP.get(
            "https://cryptopanic.com/api/v1/posts/",
            params=params,
            timeout=6,
        ).json().get("results", [])
        if not rows:
            return None
        cutoff = time.time() - 7200
        recent = []
        for p in rows[:10]:
            try:
                from datetime import datetime as _dt
                ts = _dt.fromisoformat(
                    p["created_at"].replace("Z", "+00:00")
                ).timestamp()
                if ts >= cutoff:
                    recent.append(p)
            except Exception:
                pass
        recent = recent[:3]
        if not recent:
            return None
        neg = sum(p.get("votes", {}).get("negative", 0) for p in recent)
        pos = sum(p.get("votes", {}).get("positive", 0) for p in recent)
        if neg == 0 and pos == 0:
            return None
        if neg > pos:
            return "bearish"
        if pos > neg:
            return "bullish"
        return None
    except Exception:
        return None


# ── Liquidation cluster booster ────────────────────────────────────────────────

def _liq_cluster_score(symbol: str, price: float, signal_type: str) -> tuple[int, str]:
    """
    Ищет кластеры ликвидаций вблизи цены за последние 4ч (liquidations.db).
    pump  → short_liq +0.5-3% выше цены (магниты тянут вверх)
    rug   → long_liq  -0.5-3% ниже цены (магниты тянут вниз)
    """
    if not os.path.exists(LIQ_DB_PATH):
        return 0, ""
    try:
        since_ts = int(time.time() * 1000) - LIQ_WINDOW_MS
        if signal_type == "pump":
            lo, hi, side = price * 1.003, price * 1.030, "short_liq"
        else:
            lo, hi, side = price * 0.970, price * 0.997, "long_liq"
        con = sqlite3.connect(LIQ_DB_PATH, timeout=2)
        row = con.execute(
            "SELECT COALESCE(SUM(usd),0) FROM liquidations "
            "WHERE symbol=? AND side=? AND price BETWEEN ? AND ? AND ts > ?",
            (symbol, side, lo, hi, since_ts),
        ).fetchone()
        con.close()
        usd = row[0] if row else 0
        tag = side.split("_")[0]
        if usd >= 500_000: return 25, f"Liq-кластер ${usd/1000:.0f}K {tag} — магнит +25pts"
        if usd >= 200_000: return 15, f"Liq-кластер ${usd/1000:.0f}K {tag} — магнит +15pts"
        if usd >= 100_000: return 10, f"Liq-кластер ${usd/1000:.0f}K {tag} — магнит +10pts"
        if usd >=  50_000: return  5, f"Liq-кластер ${usd/1000:.0f}K {tag} — магнит +5pts"
        return 0, ""
    except Exception:
        return 0, ""


# ── Outcomes tracking ──────────────────────────────────────────────────────────

def _log_pump_pending(c: dict) -> None:
    """Логирует отправленный сигнал в pump_pending.json для оценки исхода."""
    entry = {
        "id":          str(uuid.uuid4())[:8],
        "ts":          int(time.time()),
        "symbol":      c["symbol"],
        "signal_type": c["signal_type"],
        "stage":       c["stage"],
        "score":       c.get("score_final", c["score"]),
        "price":       c["price"],
        "funding":     c["funding"],
        "oi_chg_4h":   c["oi_chg_4h"],
        "cvd_pct":     c["cvd_pct"],
        "btc_4h":      c.get("btc_4h", 0),
    }

    def _append(data):
        if not isinstance(data, list):
            data = []
        data.append(entry)
        return data

    atomic_json_update(Path(PUMP_PENDING_FILE), _append, default=[])


def _fetch_kline_extremes(symbol: str, from_ts: int, horizon_h: int) -> tuple:
    """
    Returns (max_high, min_low, close_end, time_to_max_h, time_to_min_h)
    over the window [from_ts, from_ts + horizon_h * 3600] using 15m klines.
    All None on failure.
    """
    try:
        import requests as _req
        end_ts = from_ts + horizon_h * 3600
        resp = _HTTP.get(
            f"{_BYBIT_BASE}/v5/market/kline",
            params={
                "category": "linear",
                "symbol":   symbol,
                "interval": "15",
                "start":    from_ts * 1000,
                "end":      end_ts * 1000,
                "limit":    200,
            },
            timeout=10,
        )
        bars = resp.json()["result"]["list"]
        if not bars:
            return None, None, None, None, None

        max_high = max(float(b[2]) for b in bars)
        min_low  = min(float(b[3]) for b in bars)
        close_end = float(max(bars, key=lambda b: int(b[0]))[4])

        # Earliest bar (by open time) achieving each extreme
        max_ts = min(int(b[0]) for b in bars if float(b[2]) >= max_high) / 1000
        min_ts = min(int(b[0]) for b in bars if float(b[3]) <= min_low)  / 1000

        time_to_max_h = round((max_ts - from_ts) / 3600, 2)
        time_to_min_h = round((min_ts - from_ts) / 3600, 2)

        return max_high, min_low, close_end, time_to_max_h, time_to_min_h
    except Exception:
        return None, None, None, None, None


def _write_resolved_row(p: dict) -> None:
    """Дописывает разрешённую запись в pump_resolved.csv."""
    os.makedirs(_OUTCOMES_DIR, exist_ok=True)
    fields = [
        "id", "ts", "symbol", "signal_type", "stage", "score", "price",
        "funding", "oi_chg_4h", "cvd_pct", "btc_4h",
        "price_1h", "change_1h", "mfe_1h_pct", "mae_1h_pct",
        "time_to_mfe_1h", "time_to_mae_1h", "outcome_1h",
        "price_4h", "change_4h", "mfe_4h_pct", "mae_4h_pct",
        "time_to_mfe_4h", "time_to_mae_4h", "tp_hit", "sl_hit", "outcome_4h",
    ]
    write_hdr = not os.path.exists(PUMP_RESOLVED_FILE)
    with open(PUMP_RESOLVED_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if write_hdr:
            w.writeheader()
        w.writerow(p)


def _resolve_outcomes() -> None:
    """
    Проверяет pending сигналы и разрешает исходы 1ч/4ч через kline extremes.
    Использует max_high/min_low окна вместо spot-цены в момент проверки.
    Снимает snapshot под локом, обрабатывает свободно, мерджит результат обратно.
    """
    if not os.path.exists(PUMP_PENDING_FILE):
        return
    try:
        pending = json.load(open(PUMP_PENDING_FILE, encoding="utf-8"))
    except Exception:
        return
    if not pending:
        return

    now          = time.time()
    still_pending, n_resolved = [], 0

    for p in pending:
        from_ts = p["ts"]
        age_s   = now - from_ts
        p_entry = p["price"]
        stype   = p["signal_type"]
        sym     = p["symbol"]
        is_long = stype == "pump"

        if p_entry <= 0:
            still_pending.append(p)
            continue

        # ── 1h outcome ────────────────────────────────────────────────────────
        if "outcome_1h" not in p and age_s >= 3600:
            mh, ml, ce, t_mh, t_ml = _fetch_kline_extremes(sym, from_ts, 1)
            if mh is not None:
                if is_long:
                    mfe, mae    = (mh - p_entry) / p_entry * 100, (p_entry - ml) / p_entry * 100
                    t_mfe, t_mae = t_mh, t_ml
                    tp_hit_1h   = mh >= p_entry * 1.03   # +3% за 1ч = WIN (под раскачку)
                    sl_hit_1h   = ml <= p_entry * 0.98   # -2% за 1ч = LOSS
                else:
                    mfe, mae    = (p_entry - ml) / p_entry * 100, (mh - p_entry) / p_entry * 100
                    t_mfe, t_mae = t_ml, t_mh
                    tp_hit_1h   = ml <= p_entry * 0.97
                    sl_hit_1h   = mh >= p_entry * 1.02
                p.update({
                    "price_1h":       round(ce, 8),
                    "change_1h":      round((ce / p_entry - 1) * 100, 2),
                    "mfe_1h_pct":     round(mfe, 3),
                    "mae_1h_pct":     round(mae, 3),
                    "time_to_mfe_1h": t_mfe,
                    "time_to_mae_1h": t_mae,
                    "outcome_1h":     _outcome(tp_hit_1h, sl_hit_1h, t_mfe, t_mae),
                })
                time.sleep(0.05)

        # ── 4h outcome → финал → CSV ──────────────────────────────────────────
        if "outcome_4h" not in p and age_s >= 4 * 3600:
            mh, ml, ce, t_mh, t_ml = _fetch_kline_extremes(sym, from_ts, 4)
            if mh is not None:
                if is_long:
                    mfe, mae    = (mh - p_entry) / p_entry * 100, (p_entry - ml) / p_entry * 100
                    t_mfe, t_mae = t_mh, t_ml
                    tp_hit      = mh >= p_entry * (1 + PUMP_WIN_4H_PCT  / 100)
                    sl_hit      = ml <= p_entry * (1 + PUMP_LOSS_4H_PCT / 100)
                else:
                    mfe, mae    = (p_entry - ml) / p_entry * 100, (mh - p_entry) / p_entry * 100
                    t_mfe, t_mae = t_ml, t_mh
                    tp_hit      = ml <= p_entry * (1 + RUG_WIN_4H_PCT  / 100)
                    sl_hit      = mh >= p_entry * (1 + RUG_LOSS_4H_PCT / 100)
                p.update({
                    "price_4h":       round(ce, 8),
                    "change_4h":      round((ce / p_entry - 1) * 100, 2),
                    "mfe_4h_pct":     round(mfe, 3),
                    "mae_4h_pct":     round(mae, 3),
                    "time_to_mfe_4h": t_mfe,
                    "time_to_mae_4h": t_mae,
                    "tp_hit":         tp_hit,
                    "sl_hit":         sl_hit,
                    "outcome_4h":     _outcome(tp_hit, sl_hit, t_mfe, t_mae),
                })
                _write_resolved_row(p)
                n_resolved += 1
                continue
            # kline fetch failed — keep pending
            still_pending.append(p)
            continue

        still_pending.append(p)

    # Merge с диском: за время _fetch_kline_extremes мог добавиться новый сигнал
    # через _log_pump_pending. Берём id'шки из snapshot, выкидываем resolved,
    # для still_pending обновляем поля, остальное оставляем как есть.
    snapshot_ids = {p.get("id") for p in pending if p.get("id")}
    still_by_id  = {p.get("id"): p for p in still_pending if p.get("id")}

    def _merge(current_data):
        if not isinstance(current_data, list):
            return still_pending
        out = []
        for item in current_data:
            iid = item.get("id")
            if iid in snapshot_ids:
                # Был в snapshot — если он в still_pending, берём свежую версию (с обновлениями outcome_1h),
                # иначе он resolved и удаляем из pending
                if iid in still_by_id:
                    out.append(still_by_id[iid])
                # else: resolved — выкидываем
            else:
                # Новый сигнал добавлен пока шёл resolve — сохраняем
                out.append(item)
        return out

    atomic_json_update(Path(PUMP_PENDING_FILE), _merge, default=[])
    if n_resolved:
        _log(f"[Outcomes] Разрешено {n_resolved} сигналов → pump_resolved.csv")


# ── 5-минутные вспомогательные функции ───────────────────────────────────────

def _fetch_oi_5m(sym: str, limit: int = 50) -> list:
    """OI история с интервалом 5 мин — Bybit поддерживает intervalTime=5min."""
    try:
        import requests as _req
        resp = _HTTP.get(
            f"{_BYBIT_BASE}/v5/market/open-interest",
            params={"category": "linear", "symbol": sym,
                    "intervalTime": "5min", "limit": limit},
            timeout=8,
        )
        items = list(reversed(resp.json()["result"]["list"]))
        return [float(d["openInterest"]) for d in items]
    except Exception:
        return []


def _fetch_trades_raw(sym: str, limit: int = 500) -> list:
    """
    Сырые сделки с Bybit с полями: T (timestamp ms), S (side), v (size), p (price).
    Используем напрямую вместо screener.fetch_recent_trades (там нет timestamp/price).
    """
    try:
        import requests as _req
        resp = _HTTP.get(
            f"{_BYBIT_BASE}/v5/market/recent-trade",
            params={"category": "linear", "symbol": sym, "limit": limit},
            timeout=8,
        )
        return resp.json()["result"]["list"]
    except Exception:
        return []


def _calc_cvd_5min(sym: str, screener) -> tuple:
    """
    CVD за последние 5 минут из реальных сделок (time-windowed по timestamp).
    Возвращает (cvd_pct, source_label).
    cvd_pct > 0 = тейкеры покупали агрессивнее (бычий сигнал).
    """
    try:
        cutoff_ms = int(time.time() * 1000) - 5 * 60 * 1000
        trades    = _fetch_trades_raw(sym, limit=500)
        recent    = [t for t in trades if int(t.get("time", 0)) >= cutoff_ms]
        source    = "CVD5m" if len(recent) >= 5 else "CVDAll"
        if len(recent) < 5:
            recent = trades  # fallback: все доступные сделки

        buy_vol  = sum(float(t.get("size", 0)) * float(t.get("price", 1))
                       for t in recent if t.get("side") == "Buy")
        sell_vol = sum(float(t.get("size", 0)) * float(t.get("price", 1))
                       for t in recent if t.get("side") == "Sell")
        total = buy_vol + sell_vol
        if total <= 1e-8:   # FIX 2026-05-30: нет сделок (фетч пуст) → это НЕ валидный CVD=0, а отсутствие данных
            return 0.0, "NoData"
        pct = (buy_vol - sell_vol) / total * 100
        return round(pct, 1), source
    except Exception:
        return 0.0, "Error"


def _get_recent_liq(sym: str, window_sec: int = 300) -> dict:
    """
    Ликвидации символа за последние window_sec секунд из liquidations.db.
    Возвращает {"long": usd_long_liq, "short": usd_short_liq}.
    long_liq = лонги ликвидированы (медвежий сигнал).
    short_liq = шорты ликвидированы (сквиз-сигнал).
    """
    try:
        import sqlite3
        if not os.path.exists(LIQ_DB_PATH):
            return {"long": 0.0, "short": 0.0}
        cutoff_ms = int(time.time() * 1000) - window_sec * 1000
        con = sqlite3.connect(LIQ_DB_PATH, timeout=3)
        cur = con.execute(
            "SELECT side, SUM(usd) FROM liquidations "
            "WHERE symbol=? AND ts>=? GROUP BY side",
            (sym, cutoff_ms),
        )
        result = {"long": 0.0, "short": 0.0}
        for side, usd in cur.fetchall():
            if side == "long_liq":
                result["long"] = float(usd or 0)
            elif side == "short_liq":
                result["short"] = float(usd or 0)
        con.close()
        return result
    except Exception:
        return {"long": 0.0, "short": 0.0}


# ── Анализ одного символа (v2: 5-мин данные, 2 паттерна) ─────────────────────

def _analyze_symbol(sym: str, ticker: dict, screener, btc_4h: float = 0.0,
                    liq_extra: Optional[dict] = None,
                    basis_extra: Optional[dict] = None) -> Optional[dict]:
    """
    v2: 5-минутные данные, два паттерна.

    ПАТТЕРН 1 — SHORT_SQUEEZE:
      Слишком много шортов накопилось (фандинг < -0.03%), OI растёт,
      цена держится → при первом тике вверх начнётся каскад ликвидаций.

    ПАТТЕРН 2 — POST_PUMP_RUG:
      Цена уже выросла +8%+ за 5ч, розница в FOMO-лонгах (фандинг > +0.05%),
      CVD разворачивается → умные деньги продают, раг через 20-60 мин.
    """
    try:
        price    = float(ticker.get("lastPrice")   or 0)
        turnover = float(ticker.get("turnover24h") or 0)
        funding  = float(ticker.get("fundingRate") or 0) * 100
        if price == 0 or turnover < MIN_TURNOVER_USD:
            return None

        # ── 5-мин klines: 62 свечи = ~5 часов истории ────────────────────────
        _kl_raw = screener.fetch_klines(sym, "5", limit=62)
        if not _kl_raw or len(_kl_raw[0]) < 20:
            return None
        opens, highs, lows, closes, volumes = _kl_raw

        # Санити-чек
        if closes[-1] > 0 and price > 0:
            _ratio = max(closes[-1], price) / min(closes[-1], price)
            if _ratio > 2.0:
                return None

        # Изменения цены на разных горизонтах
        price_chg_5h  = (price - opens[0])    / opens[0]    * 100 if opens[0]    > 0 else 0
        price_chg_30m = (price - closes[-7])  / closes[-7]  * 100 if len(closes) >= 7  and closes[-7]  > 0 else 0
        price_chg_5m  = (price - closes[-2])  / closes[-2]  * 100 if len(closes) >= 2  and closes[-2]  > 0 else 0
        price_chg_4h  = price_chg_5h  # alias для совместимости с send_pump_alert

        # ── 5-мин OI (48 точек = 4 часа) ─────────────────────────────────────
        oi_hist = _fetch_oi_5m(sym, limit=50)
        if len(oi_hist) < 8:
            return None
        oi_now    = oi_hist[-1]
        oi_30m    = oi_hist[-7]  if len(oi_hist) >= 7  else oi_hist[0]
        oi_4h_pt  = oi_hist[0]
        oi_chg_30m = (oi_now - oi_30m)   / oi_30m   * 100 if oi_30m   > 0 else 0
        oi_chg_4h  = (oi_now - oi_4h_pt) / oi_4h_pt * 100 if oi_4h_pt > 0 else 0

        # ── CVD: последние 5 минут реальных сделок ────────────────────────────
        cvd_pct, cvd_source = _calc_cvd_5min(sym, screener)
        if cvd_source in ("Error", "NoData"):   # FIX 2026-05-30: CVD недоступен (сбой фетча) → не эмитим сигнал на занулённой CVD
            return None

        # ── Ликвидации за последние 5 мин (из SQLite) ─────────────────────────
        liq = _get_recent_liq(sym, window_sec=300)
        liq_long  = liq["long"]   # USD ликвидаций лонгов  (плохо для лонгов)
        liq_short = liq["short"]  # USD ликвидаций шортов  (подтверждает сквиз)

        # ── Объём: последняя закрытая свеча vs avg 10 предыдущих ─────────────
        vol_avg10 = sum(volumes[-12:-2]) / 10 if len(volumes) >= 12 else 1.0
        vol_surge = min(volumes[-2] / vol_avg10, 20.0) if vol_avg10 > 1e-8 else 1.0

        # ── Фандинг-история (тренд) ───────────────────────────────────────────
        fund_hist = screener.fetch_funding_history(sym, limit=3)
        fund_prev = fund_hist[-2] if len(fund_hist) >= 2 else funding
        fund_trend = funding - fund_prev

        # ── Sweep: вынос ниже поддержки с возвратом ──────────────────────────
        sweep_signal = False
        if len(lows) >= 5:
            recent_low = min(lows[-5:-2])
            if lows[-2] < recent_low * 0.998 and closes[-2] > recent_low:
                sweep_signal = True

        signals:    list  = []
        score:      int   = 0
        stage:      str   = ""
        signal_type: str  = ""
        tldb_notes: list  = []

        # ══════════════════════════════════════════════════════════════════════
        # ПАТТЕРН 1: SHORT SQUEEZE
        # Слишком много шортов → при первом тике вверх → каскад ликвидаций
        # ══════════════════════════════════════════════════════════════════════
        if (funding <= -0.03                          # шорты платят дорого
                and (oi_chg_30m >= 0.5 or oi_chg_4h >= 5.0)  # OI строится сейчас ИЛИ уже накоплен
                and price_chg_30m <= 3.0              # цена не улетела вверх (squeeze не начался)
                and price_chg_30m >= -8.0             # не в свободном падении
                and cvd_pct >= -20                    # покупатели ещё держатся
                and vol_surge >= 0.7):                # не мёртвый рынок

            score += 45
            signals.append(f"Фандинг {funding:+.3f}% — шорты перегружены")
            oi_desc = f"OI +{oi_chg_4h:.1f}% за 4ч" if oi_chg_4h >= 5.0 else f"OI +{oi_chg_30m:.1f}% за 30 мин"
            signals.append(f"{oi_desc} — пружина сжата")
            stage       = "🔴 СКВИЗ SETUP"
            signal_type = "pump"

            # Усилитель: цена держится несмотря на давление шортов
            if -2.0 <= price_chg_30m <= 0.5:
                score += 15
                signals.append(f"Цена держит {price_chg_30m:+.2f}% при шортовой нагрузке")

            # Усилитель: sweep ниже поддержки — лонги набраны на дне
            if sweep_signal:
                score += 20
                signals.append("Sweep ниже поддержки + возврат — лонги набраны на дне")

            # Усилитель: CVD положительный — тейкеры агрессивно покупают
            if cvd_pct >= 20:
                score += 15
                signals.append(f"{cvd_source} +{cvd_pct:.0f}% — скрытые покупки")

            # Усилитель: ликвидации шортов уже начались → сквиз в процессе (Bybit 5мин)
            if liq_short >= 10_000:
                score += 25
                signals.append(f"SHORT ликвидации ${liq_short/1000:.0f}K за 5 мин — СКВИЗ ИДЁТ")

            # Coinalyze multi-exchange ликвидации 60мин — подтверждение картины
            if liq_extra:
                cz_short = float(liq_extra.get("short_usd", 0))
                if cz_short >= 500_000:
                    score += 20
                    signals.append(f"Coinalyze SHORT 60м (multi-exch): ${cz_short/1000:.0f}K")
                elif cz_short >= 200_000:
                    score += 10
                    signals.append(f"Coinalyze SHORT 60м: ${cz_short/1000:.0f}K")

            # Binance cross-exchange подтверждение
            bnb_oi = fetch_binance_oi_4h_change(sym)
            if bnb_oi is not None and bnb_oi >= 2.0:
                score += 15
                signals.append(f"Binance OI +{bnb_oi:.1f}% — кросс-биржевое подтверждение")
            else:
                bnb_oi = None

            # BTC режим
            if btc_4h >= 1.0:
                score += 10
            elif btc_4h <= -2.0:
                score -= 20
                tldb_notes.append(f"BTC медвежий ({btc_4h:+.1f}%) — сквиз рискован")

            # Spot/Perp basis: дисконт перпа = реальный шорт-спрос → подтверждение сквиза
            if basis_extra:
                _bp = basis_extra.get("basis_pct")
                if _bp is not None:
                    if _bp <= -0.5:
                        score += 20
                        signals.append(
                            f"Perp basis {_bp:+.2f}% — сильный дисконт, шорты загружены"
                        )
                    elif _bp <= -0.3:
                        score += 15
                        signals.append(
                            f"Perp basis {_bp:+.2f}% — перп дисконтом, шорт-спрос реальный"
                        )

        # ══════════════════════════════════════════════════════════════════════
        # ПАТТЕРН 2: POST-PUMP RUG
        # Цена уже выросла, розница в FOMO, умные деньги продают
        # ══════════════════════════════════════════════════════════════════════
        elif (price_chg_5h >= 8.0             # памп уже состоялся
                and funding >= 0.05            # розница переплачивает за лонги
                and cvd_pct <= 10             # CVD не подтверждает рост (или падает)
                and oi_chg_30m <= 1.0         # новых покупателей нет
                and vol_surge >= 1.2):         # объём есть — есть в кого продавать

            score += 50
            signals.append(f"Рост {price_chg_5h:+.1f}% за 5ч — памп прошёл")
            signals.append(f"Фандинг {funding:+.4f}% — розница в FOMO-лонгах")
            stage       = "🚨 ПОСТ-ПАМП РАГ"
            signal_type = "rug_prep"

            # Усилитель: CVD разворачивается — умные деньги выходят
            if cvd_pct <= -20:
                score += 25
                signals.append(f"{cvd_source} {cvd_pct:+.0f}% — продают в ралли")
            elif cvd_pct <= 0:
                score += 10
                signals.append(f"{cvd_source} {cvd_pct:+.0f}% — покупатели ослабли")

            # Усилитель: OI падает — умные выходят из позиций
            if oi_chg_30m <= -1.0:
                score += 15
                signals.append(f"OI {oi_chg_30m:+.1f}% за 30 мин — выход из позиций")

            # Усилитель: ликвидации лонгов уже начались → раг в процессе (Bybit 5мин)
            if liq_long >= 15_000:
                score += 30
                signals.append(f"LONG ликвидации ${liq_long/1000:.0f}K за 5 мин — РАГ ИДЁТ")

            # Coinalyze multi-exchange ликвидации лонгов 60мин
            if liq_extra:
                cz_long = float(liq_extra.get("long_usd", 0))
                if cz_long >= 500_000:
                    score += 20
                    signals.append(f"Coinalyze LONG 60м (multi-exch): ${cz_long/1000:.0f}K")
                elif cz_long >= 200_000:
                    score += 10
                    signals.append(f"Coinalyze LONG 60м: ${cz_long/1000:.0f}K")

            # BTC растёт = рагу труднее случиться
            if btc_4h >= 1.5:
                score -= 15
                tldb_notes.append(f"BTC растёт ({btc_4h:+.1f}%) — раг менее вероятен")

            bnb_oi = None

        # ══════════════════════════════════════════════════════════════════════
        # ПАТТЕРН 4: VOLUME_SURGE — крупный объём, цена ещё не двинулась
        # ══════════════════════════════════════════════════════════════════════
        if not signal_type:
            if (vol_surge >= VOL_SURGE_MIN
                    and abs(price_chg_30m) <= VOL_SURGE_PRICE_MAX
                    and oi_chg_30m >= VOL_SURGE_OI_MIN):
                score += 38
                signals.append(f"Объём ×{vol_surge:.1f} при цене {price_chg_30m:+.2f}% — крупный покупатель")
                signals.append(f"OI 30м +{oi_chg_30m:.2f}% — позиции открываются")
                stage       = "📊 VOLUME SURGE"
                signal_type = "pump"
                # CVD усиливает
                if cvd_pct >= 20:
                    score += 12
                    signals.append(f"CVD +{cvd_pct:.0f}% — тейкеры покупают")
                # BTC попутный
                if btc_4h >= 0.5:
                    score += 8
                elif btc_4h <= -1.5:
                    score -= 10
                bnb_oi = None

        # ══════════════════════════════════════════════════════════════════════
        # ПАТТЕРН 5: CVD_BULL_DIV_SOLO — strong CVD без других подтверждений
        # ══════════════════════════════════════════════════════════════════════
        if not signal_type:
            if (cvd_pct >= CVD_BULL_DIV_PCT
                    and price_chg_30m <= CVD_BULL_DIV_PMAX
                    and vol_surge >= 0.7):
                score += 40
                signals.append(f"CVD +{cvd_pct:.0f}% при цене {price_chg_30m:+.2f}% — скрытое накопление")
                stage       = "📈 CVD BULL DIV"
                signal_type = "pump"
                if oi_chg_30m >= 0.3:
                    score += 10
                    signals.append(f"OI 30м +{oi_chg_30m:.2f}% — confirmation")
                if liq_short >= 30_000:
                    score += 15
                    signals.append(f"SHORT liq ${liq_short/1000:.0f}K — короткие сдают")
                if btc_4h <= -2.0:
                    score -= 15
                bnb_oi = None

        # ══════════════════════════════════════════════════════════════════════
        # ПАТТЕРН 6: CVD_BEAR_DIV_SOLO — тейкеры продают на растущей цене
        # ══════════════════════════════════════════════════════════════════════
        if not signal_type:
            if (cvd_pct <= CVD_BEAR_DIV_PCT
                    and price_chg_30m >= CVD_BEAR_DIV_PMIN
                    and vol_surge >= 0.7):
                score += 40
                signals.append(f"CVD {cvd_pct:+.0f}% при цене {price_chg_30m:+.2f}% — скрытая распродажа")
                stage       = "📉 CVD BEAR DIV"
                signal_type = "rug_prep"
                if oi_chg_30m <= -0.3:
                    score += 10
                    signals.append(f"OI 30м {oi_chg_30m:+.2f}% — выход из позиций")
                if liq_long >= 30_000:
                    score += 15
                    signals.append(f"LONG liq ${liq_long/1000:.0f}K — длинные ликвидируются")
                if btc_4h >= 1.5:
                    score -= 12
                bnb_oi = None

        # ══════════════════════════════════════════════════════════════════════
        # ПАТТЕРН 7: ATR_COIL — сжатие волатильности перед движением
        # ══════════════════════════════════════════════════════════════════════
        if not signal_type and len(highs) >= 30 and len(lows) >= 30:
            try:
                # recent_atr = средний TR за последние 6 свечей (30 мин)
                # hist_atr   = средний TR за свечи -30..-6 (последние 2 часа)
                def _atr_simple(h_, l_, c_):
                    if len(h_) < 2:
                        return 0.0
                    trs = []
                    for i in range(1, len(h_)):
                        tr = max(h_[i] - l_[i], abs(h_[i] - c_[i-1]), abs(l_[i] - c_[i-1]))
                        trs.append(tr)
                    return sum(trs) / len(trs) if trs else 0.0

                recent_atr = _atr_simple(highs[-7:], lows[-7:], closes[-8:-1])
                hist_atr   = _atr_simple(highs[-30:-6], lows[-30:-6], closes[-31:-7])
                atr_ratio  = recent_atr / hist_atr if hist_atr > 1e-8 else 1.0

                if atr_ratio < ATR_COIL_RATIO and vol_surge >= ATR_COIL_VOL_MIN:
                    score += 36
                    signals.append(f"ATR ×{atr_ratio:.2f} — сжатие волатильности (пружина)")
                    signals.append(f"Vol ×{vol_surge:.1f} при сжатии — накопление")
                    stage       = "🌀 ATR COIL"
                    signal_type = "pump"
                    # Funding негативный → bullish coil
                    if funding <= -0.015:
                        score += 12
                        signals.append(f"Funding {funding:+.3f}% — шорты под давлением")
                    if oi_chg_4h >= 3.0:
                        score += 10
                        signals.append(f"OI 4h +{oi_chg_4h:.1f}% — накопление")
                    bnb_oi = None
            except Exception:
                pass

        # ══════════════════════════════════════════════════════════════════════
        # ПАТТЕРН 3: SLOW DISTRIBUTION (медленный rug 2-3 дня)
        # Цена медленно растёт +10..+30% за 3 дня, funding нагревается, но без
        # перегрева. Объёмы равномерные (нет одного параболического дня) —
        # классический паттерн организованного распределения через несколько дней.
        # ══════════════════════════════════════════════════════════════════════
        if not signal_type:
            try:
                _kl4h = screener.fetch_klines(sym, "240", 20)
                if _kl4h and len(_kl4h[3]) >= 19:
                    o4, h4, l4, c4, v4 = _kl4h
                    # FIX 2026-05-31: исключаем ЖИВУЮ 4H-свечу c4[-1] (look-ahead) — мерим на ЗАКРЫТЫХ [-19..-2],
                    # охват 3д сохранён (17 интервалов). Объёмные окна тоже без живой свечи (v4[-19:-1]).
                    p3d_chg = (c4[-2] - c4[-19]) / c4[-19] * 100 if c4[-19] > 0 else 0
                    v_max = max(v4[-19:-1]) if v4[-19:-1] else 0
                    v_min = min(v4[-19:-1]) if v4[-19:-1] else 0
                    v_ratio_3d = v_max / v_min if v_min > 1e-8 else 0
                    if (SLOW_DIST_3D_MIN <= p3d_chg <= SLOW_DIST_3D_MAX
                            and SLOW_DIST_FUND_MIN <= funding <= SLOW_DIST_FUND_MAX
                            and v_ratio_3d <= SLOW_DIST_VOL_MAX_R):

                        score += 40
                        signals.append(f"Рост +{p3d_chg:.1f}% за 3 дня — медленное распределение")
                        signals.append(f"Funding {funding:+.4f}% — нагрев без перегрева")
                        signals.append(f"Vol stability ×{v_ratio_3d:.1f} — равномерное накопление")
                        stage       = "🐢 SLOW DIST"
                        signal_type = "rug_prep"

                        # Усилитель: CVD расхождение (продают в ралли)
                        if cvd_pct <= -10:
                            score += 20
                            signals.append(f"CVD {cvd_pct:+.0f}% — продают в ралли (4h)")
                        elif cvd_pct <= 5:
                            score += 10
                            signals.append(f"CVD {cvd_pct:+.0f}% — слабый bid (4h)")

                        # Усилитель: OI 4h не растёт — умные деньги не докупают
                        if oi_chg_4h <= 1.0:
                            score += 15
                            signals.append(f"OI 4h {oi_chg_4h:+.1f}% — нет докупки на росте")

                        # BTC падает = ускоряет slow rug
                        if btc_4h <= -1.0:
                            score += 10
                        elif btc_4h >= 2.0:
                            score -= 15
                            tldb_notes.append(f"BTC растёт ({btc_4h:+.1f}%) — slow rug менее вероятен")

                        bnb_oi = None
            except Exception:
                pass  # 4h данные недоступны — пропускаем slow_dist

        if not signal_type:
            return None

        if score < MIN_SIGNAL_SCORE:
            return None

        # ── USE_VALIDATED_V1: доп. факторы для памп-сигналов (параллельный путь) ─────
        # Факторы из PUMP_PATTERNS_ANALYSIS.md. Флаг = False → блок пропускается.
        if USE_VALIDATED_V1 and signal_type == "pump":
            # Валидированные объёмные факторы через vol_core (30д-медиана 1h, непересекающаяся база).
            # Чинит LA3/LA4: раньше считалось по самореферентному 5m-mean (vol_avg10) → ratio≈1.0 by construction.
            # Кросс-биржевая валидация на Bybit: edge сохраняется (Spearman vol_ratio_1h↔MFE24h=0.449, как Binance;
            # high-vol MFE 5.96% vs 1.71%). См. backtests/bybit_volcore_validation.py, project_v1_validation_audit.
            try:
                import vol_core as _vc
                _vols1h = _vc.fetch_bybit_1h_volumes(sym, limit=750)
                _conv = _vc.conviction(_vc.vol_factors(_vols1h) if _vols1h else None)
                if _conv["score_delta"]:
                    score += _conv["score_delta"]
                    signals.extend(_conv["signals"])
                # _conv["tier"] (high/standard/weak/below_gate) — градуированный гейт,
                # для re-gating/shadow-A/B при активации (high/standard пропускать, weak душить 2-м гейтом)
            except Exception as _e_v1:
                _log(f"[V1] vol_core недоступен для {sym}: {_e_v1}")

            # УБРАНЫ антисигналы btc_trend_1h (−12) и price_vs_high_7d (−8): разоблачены OOS.
            # btc_trend для ПАМПОВ плоский (lift~0.95, re-run 2.33 vs 2.46), price_vs_high ПЕРЕВЕРНУЛ
            # знак OOS (lift 0.88→1.28). Также убран синхронный REST 7d-хая с горячего пути (PROXY3).
            # btc_cascade/режим — это шорт-сторона (rug_detector), не пампы.

            # БУСТ: stablecoin_inflow == 1 → +score (lift 2.31 HIGH)
            # TODO: requires stablecoin_flow data (stablecoin_flow.get_stable_flow(), async)
            # Интеграция: pre-fetch в scan_once() и передавать как параметр _analyze_symbol()
            # stablecoin_inflow = None  # not yet wired
            # if stablecoin_inflow:
            #     score += 20
            #     signals.append("[V1] stablecoin_inflow=1 — приток стейблов (lift 2.31) +20")

        # L/S ratio — дополнительный контекст
        ls_data = fetch_coinglass_ls_ratio(sym)

        # Fear & Greed
        fng_value, fng_label = None, ""
        _fng = get_fear_greed()
        if _fng is not None:
            fng_value = _fng
            fng_label = _fng_cache.get("label", "")
            # #4 (2026-05-30): FNG-бонус УБРАН из скоринга — macro_extreme/Fear&Greed разоблачён
            # (train lift 6.0 → event-study 1.0). fng_value/fng_label остаются контекстом для Claude.

        score = min(score, 130)

        # Валидация: klines в нестандартной деноминации (напр. INXUSDT)
        if abs(price_chg_4h) > 50.0:
            _log(f"[DataFix] {sym}: kline price mismatch, используем ticker fallback")
            _prev = float(ticker.get("prevPrice24h") or price)
            price_chg_4h = (price - _prev) / _prev * 100 if _prev > 0 else 0
            if abs(price_chg_4h) > 50.0:
                return None

        cp_sentiment = None

        return {
            "symbol":        sym,
            "price":         price,
            "stage":         stage,
            "signal_type":   signal_type,
            "score":         score,
            "signals":       signals,
            "tldb_notes":    tldb_notes,
            "cvd_source":    cvd_source,
            "cvd_pct":       round(cvd_pct, 1),
            "oi_chg_4h":     round(oi_chg_4h, 2),
            "oi_chg_30m":    round(oi_chg_30m, 2),
            "price_chg_4h":  round(price_chg_5h, 2),
            "price_chg_30m": round(price_chg_30m, 2),
            "funding":       round(funding, 4),
            "fund_prev":     round(fund_prev, 4),
            "sweep":         sweep_signal,
            "btc_4h":        round(btc_4h, 2),
            "ls_data":       ls_data,
            "bnb_oi_chg":    bnb_oi if signal_type == "pump" else None,
            "fng_value":     fng_value,
            "fng_label":     fng_label,
            "cp_sentiment":  None,
            "post_pump_dist": signal_type == "rug_prep",
            "liq_long":      liq_long,
            "liq_short":     liq_short,
            "vol_surge":     round(vol_surge, 2),
            "basis":         basis_extra,   # для Claude RT-context (None если данных нет)
        }

    except Exception:
        return None


# ── Скан всего рынка ──────────────────────────────────────────────────────────

def scan_once() -> list[dict]:
    try:
        import screener
    except ImportError:
        _log("screener не найден")
        return []

    try:
        tickers = screener.fetch_all_tickers()
    except Exception as e:
        _log(f"fetch_all_tickers: {e}")
        return []

    # BTC режим — один fetch на весь скан
    btc_4h = 0.0
    try:
        btc_4h = screener.fetch_btc_4h_change()
        _log(f"BTC 4h: {btc_4h:+.2f}%")
    except Exception:
        pass

    # Сортируем по USD-обороту (turnover24h), берём топ-N
    ranked = sorted(
        [(s, t) for s, t in tickers.items()
         if float(t.get("turnover24h") or 0) >= MIN_TURNOVER_USD],
        key=lambda x: float(x[1].get("turnover24h") or 0),
        reverse=True,
    )
    top_n = ranked[:TOP_N_SYMBOLS]
    # Добавляем whitelist-символы если они не попали в топ-N
    top_syms = {s for s, _ in top_n}
    extra = [(s, t) for s, t in tickers.items()
             if s in SYMBOL_WHITELIST and s not in top_syms]
    by_vol = top_n + extra

    # Coinalyze multi-exchange ликвидации — один батч на весь скан
    liq_multi: dict = {}
    try:
        liq_multi = screener.fetch_coinalyze_liq_stats(
            [s for s, _ in by_vol], window_min=60
        )
        if liq_multi:
            _log(f"Coinalyze: ликвидации по {len(liq_multi)} символам (60м, multi-exch)")
    except Exception as e:
        _log(f"Coinalyze недоступен: {e}")

    # Spot vs Perp basis — один батч (2 запроса к Bybit V5) на весь скан
    basis_map: dict = {}
    try:
        from spot_perp_basis import get_basis_batch
        basis_map = _arun(get_basis_batch([s for s, _ in by_vol]))
        if basis_map:
            _log(f"Basis: spot/perp по {len(basis_map)} символам")
    except Exception as e:
        _log(f"Basis недоступен: {e}")

    results = []
    for sym, ticker in by_vol:
        r = _analyze_symbol(sym, ticker, screener, btc_4h=btc_4h,
                            liq_extra=liq_multi.get(sym),
                            basis_extra=basis_map.get(sym))
        if r:
            # Прокидываем для Claude RT-фильтра
            if liq_multi.get(sym):
                r["liq_long_usd"]  = liq_multi[sym].get("long_usd")
                r["liq_short_usd"] = liq_multi[sym].get("short_usd")
            results.append(r)
        time.sleep(0.05)   # лёгкий throttle API

    # Top-trader position skew — selective enrichment только для distribution-паттернов.
    # 3 запроса/символ к Binance Futures (бесплатно, 1000 req/5min limit).
    # Обычно 0-3 rug-кандидата на скан → <10 запросов = <1% лимита.
    skew_targets = [
        r["symbol"] for r in results
        if r.get("stage") in ("🚨 ПОСТ-ПАМП РАГ", "🐢 SLOW DIST")
    ]
    if skew_targets:
        try:
            from top_trader_positions import get_position_skew_batch
            skew_map = _arun(get_position_skew_batch(skew_targets, period="1h"))
            if skew_map:
                _log(f"PosSkew: top-trader данные по {len(skew_map)}/{len(skew_targets)} rug-кандидатам")
            for r in results:
                sk = skew_map.get(r["symbol"])
                if not sk:
                    continue
                r["pos_skew"] = sk  # для Claude RT-context
                sig = sk.get("signal", "")
                delta = sk.get("delta", 0)
                # #4 (2026-05-30): whale/pos_skew score-мутации (+20/-15/+10) УБРАНЫ —
                # whale-tracking разоблачён (near-random). r["pos_skew"] сохранён выше как
                # контекст для Claude RT-фильтра; в score больше не входит.
                if sig:
                    r["tldb_notes"].append(f"Skew 1h ({sig}, {delta:+.0f}pp) — контекст, не скор")
        except Exception as e:
            _log(f"PosSkew недоступен: {e}")

    # Orderbook L2 enrichment — REST snapshot для всех кандидатов прошедших _analyze_symbol.
    # Direction-aware усилители: pump хочет bids доминируют + bid_wall, rug хочет asks + ask_wall.
    # Streamer-вариант (absorption detection) отложен — см. project_orderbook_future_streamer.md.
    if results:
        try:
            from orderbook_imbalance import get_book_metrics

            async def _book_batch():
                tasks = [get_book_metrics(r["symbol"], use_stream=False) for r in results]
                return await asyncio.gather(*tasks, return_exceptions=True)

            book_metrics = _arun(_book_batch())
            n_book = 0
            for r, m in zip(results, book_metrics):
                if isinstance(m, Exception) or not m:
                    continue
                r["book"] = m  # для Claude RT-context
                n_book += 1

                imb = m.get("imbalance", 0.0)
                bid_wall = m.get("bid_wall")
                ask_wall = m.get("ask_wall")
                stype = r.get("signal_type", "")

                if stype == "pump":
                    # Pump: хотим bids доминируют, ask side тонкий
                    if imb >= 0.30:
                        r["score"] += 10
                        r["signals"].append(
                            f"Book imbalance {imb:+.2f} — bids доминируют (топ-10)"
                        )
                    elif imb <= -0.30:
                        r["score"] -= 10
                        r["tldb_notes"].append(
                            f"Book imbalance {imb:+.2f} — asks давят, против pump"
                        )
                    if ask_wall:
                        r["score"] -= 10
                        r["tldb_notes"].append(
                            f"Ask wall ${ask_wall['size_usd']/1000:.0f}K на {ask_wall['price']:.6g} — сопротивление"
                        )
                    elif bid_wall:
                        r["score"] += 5
                        r["signals"].append(
                            f"Bid wall ${bid_wall['size_usd']/1000:.0f}K на {bid_wall['price']:.6g} — поддержка"
                        )

                elif stype == "rug_prep":
                    # Rug: хотим asks доминируют, bid side тонкий
                    if imb <= -0.30:
                        r["score"] += 10
                        r["signals"].append(
                            f"Book imbalance {imb:+.2f} — asks доминируют (распределение)"
                        )
                    elif imb >= 0.30:
                        r["score"] -= 10
                        r["tldb_notes"].append(
                            f"Book imbalance {imb:+.2f} — bids доминируют, против rug"
                        )
                    if bid_wall:
                        r["score"] -= 10
                        r["tldb_notes"].append(
                            f"Bid wall ${bid_wall['size_usd']/1000:.0f}K — поддержка, rug сложнее"
                        )
                    elif ask_wall:
                        r["score"] += 5
                        r["signals"].append(
                            f"Ask wall ${ask_wall['size_usd']/1000:.0f}K — киты держат сопротивление"
                        )
            if n_book:
                _log(f"OrderBook: метрики по {n_book}/{len(results)} кандидатам")
        except Exception as e:
            _log(f"OrderBook недоступен: {e}")

    results.sort(key=lambda x: x["score"], reverse=True)
    _log(f"Скан {len(by_vol)} пар → {len(results)} сигналов")
    return results


# ── TG алерт ─────────────────────────────────────────────────────────────────

def send_pump_alert(c: dict, cfg: dict = None) -> bool:
    """Возвращает True если алерт реально ушёл в TG, False если фильтр/ошибка/нет конфига."""
    # Kill-switch: при активном audit-mode (серия лоссов в streak_monitor) — не шлём ничего (как screener.py:6457)
    if _STREAK_AVAILABLE and _streak.is_audit_mode():
        _log(f"[KillSwitch] audit-mode активен (серия лоссов) — {c.get('symbol','?')} НЕ отправлен")
        return False
    # Cooldown safety-net: блокируем любой путь отправки если символ уже алертился недавно
    _sym = c.get("symbol", "")
    _last_sent = _sent_recently.get(_sym, 0)
    if _sym and time.time() - _last_sent < COOLDOWN_SEC:
        _ago_min = int((time.time() - _last_sent) / 60)
        _log(f"[Cooldown] {_sym} последний алерт {_ago_min}мин назад "
             f"(< {COOLDOWN_SEC // 60}мин) — блок повторной отправки")
        return False

    # ── FundGate (2026-05-29): pump-LONG при funding ≤ PUMP_LONG_FUND_BLOCK ──
    # 0 WIN из 19 на funding ≤ -0.5% (outcomes/pump_resolved.csv). Доводим решение
    # screener DistGate-0 ("cut losing longs") до памп-демона. rug_prep (шорт) НЕ трогаем,
    # режем ДО вызова Claude (экономим токен + не даём фильтру одобрить заведомый лосс).
    if c.get("signal_type") == "pump" and float(c.get("funding") or 0) <= PUMP_LONG_FUND_BLOCK:
        _log(f"[FundGate] {_sym} pump-LONG BLOCK: funding "
             f"{float(c.get('funding') or 0):+.3f}% ≤ {PUMP_LONG_FUND_BLOCK}% — 0/19 WR (resolved)")
        if _RT_AVAILABLE:
            try:
                _rt.log_reject(
                    {**c, "setup": "squeeze"}, "FundGate",
                    f"pump/long funding={float(c.get('funding') or 0):+.3f} "
                    f"score={c.get('score')} stage={c.get('stage','')}",
                )
            except Exception as _re:
                _log(f"[FundGate] shadow-log не записан: {_re}")
        return False

    # ── PumpGate (2026-05-30): весь pump-LONG в SHADOW (backtest net-of-costs убыточен даже gross) ──
    # Алерт подавляем, пишем в reject-shadow для валидации. rug_prep (шорт, +EV-кандидат) НЕ трогаем.
    if PUMP_LONG_MODE == "off" and c.get("signal_type") == "pump":
        _log(f"[PumpGate] {_sym} pump-LONG SHADOW (backtest −0.47%/сделку net) — алерт подавлен")
        if _RT_AVAILABLE:
            try:
                _rt.log_reject(
                    {**c, "setup": "squeeze"}, "PumpGate",
                    f"pump/long shadow score={c.get('score')} stage={c.get('stage','')} "
                    f"funding={float(c.get('funding') or 0):+.3f}",
                )
            except Exception as _re:
                _log(f"[PumpGate] shadow-log не записан: {_re}")
        try:
            import telegram_alerts as _ta
            _ta.notify_suppression(
                "pump_long_shadow",
                "🔕 pump-LONG в SHADOW: бэктест net-of-costs убыточен (даже до издержек), score не "
                "разделяет. Алерты по пампу-лонгу придержаны; rug-шорт набирает выборку для форвард-теста.",
                cooldown_h=24,
            )
        except Exception:
            pass
        return False

    # observe-режим: pump-LONG идёт дальше (Claude RT-фильтр + рендер), но честно
    # помечается экспериментальным. FundGate (выше) уже отрезал funding ≤ -0.5%.
    _pump_experimental = _is_obs_pump(c)

    try:
        import telegram_alerts as _tg
        if cfg is None:
            cfg = _tg.load_config()
        from telegram_alerts import _send, _esc

        token   = cfg.get("bot_token", "")
        chat_id = str(cfg.get("chat_id", ""))
        if not token or not chat_id:
            return False

        # Claude RT-фильтр перед фактической отправкой
        if os.environ.get("CLAUDE_RT_FILTER", "on").lower() in ("on", "true", "1", "yes"):
            try:
                from claude_realtime_filter import filter_candidate as _crt_filter
                _is_rug = c.get("signal_type") == "rug_prep"
                _candidate = dict(c)
                _candidate.setdefault("direction", "SHORT" if _is_rug else "LONG")
                _candidate.setdefault("setup", c.get("signal_type", "pump"))
                _v = _crt_filter(c["symbol"], _candidate, source="pump_detector")
                _act = _v.get("action")
                _log(f"[RT-Filter] {c['symbol']} {_candidate['setup']} → {_act} "
                     f"conf={_v.get('confidence',0):.2f}  {_v.get('reasoning','')[:80]}")
                if _act == "FAIL_OPEN" and FAIL_CLOSED_PUMP:
                    _log(f"[RT-Filter] {c['symbol']} FAIL_OPEN (Claude недоступен) → SKIP (fail-closed)")
                    return False
                _macro_wait = (_act == "WAIT" and _v.get("macro_veto"))
                if _macro_wait:
                    # P0-1b (политика владельца): макро-вето → шлём с предупреждением
                    c["macro_veto_note"] = (_v.get("macro_veto_reason") or _v.get("reasoning") or "")[:160]
                if _act not in ("GO", "FAIL_OPEN") and not _macro_wait:
                    return False  # SKIP / обычный WAIT — не шлём, не считаем в daily_limit
                # Прикрепляем для рендера в TG
                c["claude_reasoning"]  = _v.get("reasoning", "")
                c["claude_confidence"] = _v.get("confidence", 0)
                c["claude_risks"]      = _v.get("risks", []) or []
                c["claude_tp_pct"]     = _v.get("tp_pct")
                c["claude_sl_pct"]     = _v.get("sl_pct")
            except Exception as _e:
                _log(f"[RT-Filter] ошибка фильтра: {_e}")
                if FAIL_CLOSED_PUMP:
                    _log(f"[RT-Filter] {c.get('symbol','?')} ошибка фильтра → SKIP (fail-closed)")
                    return False
                # (при FAIL_CLOSED_PUMP=False — старое поведение fail-open: слать)

        is_rug  = c["signal_type"] == "rug_prep"
        header  = "🚨 РАГ-ПОДГОТОВКА" if is_rug else "🚀 РАННИЙ СИГНАЛ"
        action  = "⛔ Осторожно — возможный дамп/rug" if is_rug else "⚡ Следи за пробоем — вход рано"

        btc_str = f"  BTC 4ч:     {c.get('btc_4h', 0):+.2f}%" if c.get("btc_4h") else ""
        lines = []
        if _pump_experimental:
            lines += [
                "🧪 <b>ЭКСПЕРИМЕНТ — НЕ торговый сетап</b>",
                "pump-LONG убыточен по бэктесту (−0.47%/сделку net), score не разделяет винеры.",
                "Это лучший отфильтрованный кандидат (Claude GO + conviction) для форвард-наблюдения. Риск на тебе.",
                "",
            ]
        lines += [
            f"{header}  |  {datetime.now().strftime('%H:%M')}",
            f"<b>{_esc(c['symbol'])}</b>  {c['stage']}  score={c['score']}",
            "",
            f"  Цена:       {c['price']:.5g}  ({c['price_chg_4h']:+.2f}% за 4ч)",
            f"  OI 4ч (Bybit):  {c['oi_chg_4h']:+.2f}%",
            *(
                [f"  OI 4ч (Binance): {c['bnb_oi_chg']:+.2f}%"]
                if c.get("bnb_oi_chg") is not None else []
            ),
            f"  CVD ({c.get('cvd_source','?')}): {c['cvd_pct']:+.1f}%",
            f"  Фандинг:    {c['funding']:+.4f}%  (было {c['fund_prev']:+.4f}%)",
        ]
        if c.get("macro_veto_note"):
            # P0-1b: предупреждение сразу под header-строкой
            _mv_idx = (4 if _pump_experimental else 0) + 1
            lines.insert(_mv_idx, f"⚠ <b>МАКРО ПРОТИВ:</b> {_esc(c['macro_veto_note'])}. "
                                  f"Фильтр пропустил бы, вето переведено в предупреждение — решение за тобой.")
        if btc_str:
            lines.append(btc_str)
        if c["sweep"]:
            lines.append("  Sweep:      ✓ ликвидность снята ниже поддержки")
        ls = c.get("ls_data", {})
        if ls:
            lines.append(
                f"  L/S аккаунты: {ls['long_pct']:.1f}% / {ls['short_pct']:.1f}%"
            )
        if c.get("fng_value") is not None:
            lines.append(f"  F&G:        {c['fng_value']} ({c.get('fng_label', '')})")
        # 📊 Объёмный conviction-тир — подсказка для ВЫХОДА (магнитуда — единственное валидированное;
        # на entry-WR НЕ влияет, см. backtests/shadow_conviction_retro). Только pump/long; score/отбор не трогает.
        if not is_rug:
            try:
                import vol_core as _vc
                _vf = _vc.vol_factors(_vc.fetch_bybit_1h_volumes(c["symbol"], limit=750))
                _ct = _vc.conviction(_vf)["tier"] if _vf else "none"
                _hint = {
                    "high":     "📈 Объём HIGH (vr1>3/свежий спайк) → ожидай БОЛЬШИЙ ход, дай бежать (let-run, не фикс-TP)",
                    "standard": "📊 Объём STANDARD → умеренный ход",
                    "weak":     "📉 Объём ЗАТУХАЕТ (vol_accel<0.8) → ход скромнее, не жди иксов",
                }.get(_ct)
                if _hint:
                    lines.append(f"  {_hint}")
            except Exception:
                pass  # подсказка опциональна — не ломаем алерт
        lines += [
            "",
            *[f"  • {_esc(s)}" for s in c["signals"]],
        ]
        if c.get("tldb_notes"):
            lines += ["", *[f"  ⚠ {_esc(n)}" for n in c["tldb_notes"]]]

        # DEX spot-подтверждение
        dex_str = _fmt_dex(c.get("dex", {}))
        if dex_str:
            dex_adj = c.get("dex_adj", 0)
            adj_tag = f"  [{dex_adj:+d}pts]" if dex_adj else ""
            lines += ["", f"  📊 {_esc(dex_str + adj_tag)}"]

        # Итоговый score если отличается от исходного
        if c.get("score_final") and c.get("score_final") != c.get("score"):
            lines.append(f"  Score: {c['score']} → <b>{c['score_final']}</b> (после DEX)")

        if c.get("post_pump_dist"):
            lines.append(
                "\n⚠ Параболический памп без CVD/OI подтверждения — возможный слив"
            )

        # Momentum info — показываем если алерт пришёл после подтверждения движения
        if c.get("momentum_pct") and c.get("momentum_wait_m") is not None:
            direction = "↑" if not is_rug else "↓"
            burst_str = ""
            if c.get("momentum_burst_5m") is not None:
                burst_str += f"  |  5м: {c['momentum_burst_5m']:+.2f}%"
            if c.get("momentum_1h_chg") is not None:
                burst_str += f"  |  1H: {c['momentum_1h_chg']:+.2f}%"
            lines.append(
                f"\n⏱ Momentum: {direction}{c['momentum_pct']:.2f}% "
                f"через {c['momentum_wait_m']}мин после обнаружения{burst_str}"
            )

        # Claude reasoning + TP/SL под R:R стратегию пользователя
        if c.get("claude_reasoning"):
            _conf = c.get("claude_confidence", 0)
            lines += ["", f"🧠 Claude conf={_conf:.0%}: {_esc(c['claude_reasoning'])[:300]}"]
            _tp = c.get("claude_tp_pct"); _sl = c.get("claude_sl_pct")
            if _tp and _sl:
                _price = c.get("price", 0)
                if _price:
                    if is_rug:
                        _tp_px = _price * (1 - _tp / 100)
                        _sl_px = _price * (1 + _sl / 100)
                    else:
                        _tp_px = _price * (1 + _tp / 100)
                        _sl_px = _price * (1 - _sl / 100)
                    _rr = _tp / _sl if _sl else 0
                    lines.append(f"  🎯 TP {_tp_px:.5g} (+{_tp:.1f}%)  "
                                 f"SL {_sl_px:.5g} (−{_sl:.1f}%)  R:R {_rr:.1f}")
            for _r in (c.get("claude_risks") or [])[:3]:
                lines.append(f"  ⚠ {_esc(str(_r))[:120]}")

        lines += ["", action]

        # P0-2 (петля): кнопки [Вошёл/Пропустил] + регистрация в alerts_index
        _kb = None
        try:
            from telegram_alerts import _alert_short_id, _register_alert, _trade_buttons
            from datetime import timezone as _tz
            _alert_ts = datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M")
            _dirn  = "short" if is_rug else "long"
            _entry = c.get("price")
            _sl_px = _tp_px = None
            if _entry and c.get("claude_sl_pct"):
                _sl_px = _entry * (1 + c["claude_sl_pct"] / 100) if is_rug else _entry * (1 - c["claude_sl_pct"] / 100)
            if _entry and c.get("claude_tp_pct"):
                _tp_px = _entry * (1 - c["claude_tp_pct"] / 100) if is_rug else _entry * (1 + c["claude_tp_pct"] / 100)
            _sid = _alert_short_id(c["symbol"], _alert_ts, c.get("signal_type", "pump"))
            _register_alert(_sid, {
                "run_ts": _alert_ts, "symbol": c["symbol"],
                "setup": c.get("signal_type", "pump"), "direction": _dirn,
                "entry": _entry, "sl": _sl_px, "tp": _tp_px,
                "score": c.get("score"), "grade": None,
                "verdict": "WAIT" if c.get("macro_veto_note") else "GO",
                "macro_veto": bool(c.get("macro_veto_note")),
                "msg_id": None, "ts": _alert_ts, "status": "sent",
            })
            _kb = _trade_buttons(_sid)
        except Exception as _btn_e:
            _log(f"[Петля] кнопки/индекс не прикрутились (алерт уйдёт без них): {_btn_e}")

        _mid = _send(token, chat_id, "\n".join(lines), reply_markup=_kb)
        if _kb is not None and _mid and _mid is not True:
            try:
                _register_alert(_sid, {"msg_id": _mid})
            except Exception:
                pass
        for extra in cfg.get("extra_chat_ids", []):
            cid = str(extra)
            if cid != chat_id:
                _send(token, cid, "\n".join(lines), reply_markup=_kb)

        # Vision chart analysis — график 1H + AI разбор после памп/раг алерта
        try:
            import chart_analyzer as _ca
            if _ca._CHART_OK:
                sym = c["symbol"]
                metrics_for_chart = {
                    "symbol":  sym,
                    "score":   c.get("score", 0),
                    "setup":   c.get("signal_type", "pump"),
                    "price":   c.get("price", 0),
                    "fund_%":  c.get("funding", 0),
                    "oi24h_%": c.get("oi_chg_4h", 0),
                    "cvd_k%":  c.get("cvd_pct", 0),
                    "notes":   c.get("stage", ""),
                    "flags":   " | ".join(c.get("signals", [])),
                }
                chart_path = _ca.generate_chart(sym, metrics_for_chart)
                if chart_path:
                    analysis = _ca.analyze_with_claude(sym, chart_path, metrics_for_chart)
                    if not analysis:
                        analysis = _ca.rule_based_analysis(sym, metrics_for_chart)
                    tag = "🚨 РАГ" if is_rug else "🚀 ПАМП"
                    caption = f"{tag} <b>{sym}</b> 1H | AI Chart  score={c.get('score', 0)}"
                    full_cap = caption + "\n\n" + analysis
                    if len(full_cap) <= 1024:
                        _ca.tg_send_photo(token, chat_id, chart_path, caption=full_cap)
                    else:
                        _ca.tg_send_photo(token, chat_id, chart_path, caption=caption)
                        _ca.tg_send_text(token, chat_id, analysis)
        except Exception as e:
            _log(f"[ChartAI] памп/раг ошибка: {e}")

        # TV-разметка 15m на РЕАЛЬНОЙ ликвидности (стенки+кластеры) — опционально,
        # за флагом TV_PLAN_ENABLED=1, graceful, без API. По умолчанию выкл.
        try:
            from tv_pump_plan import attach_tv_plan_to_tg
            _tvdir = "SHORT" if is_rug else "LONG"
            attach_tv_plan_to_tg(c["symbol"], _tvdir, token, chat_id,
                                 caption=f"📊 {c['symbol']} 15m · TV-разметка (визуал для анализа)",
                                 entry=c.get("price"))
        except Exception as _tve:
            _log(f"[TVPlan] {_tve}")

        return True   # алерт реально ушёл

    except Exception as e:
        _log(f"TG alert error: {e}")
        return False


# ── Momentum helpers ─────────────────────────────────────────────────────────

def _fetch_spot_prices(symbols: list) -> dict:
    """Один лёгкий запрос — текущие цены для конкретных символов."""
    try:
        import requests as _req
        resp = _HTTP.get(
            f"{_BYBIT_BASE}/v5/market/tickers",
            params={"category": "linear"},
            timeout=5,
        )
        result = {}
        sym_set = set(symbols)
        for item in resp.json()["result"]["list"]:
            s = item["symbol"]
            if s in sym_set:
                p = float(item.get("lastPrice", 0) or 0)
                if p > 0:
                    result[s] = p
        return result
    except Exception:
        return {}


def _check_momentum(tg_cfg: dict):
    """
    Проверяет движение цены для кандидатов в watch-листе.
    Вызывается каждые MOMENTUM_CHECK_SEC секунд — лёгкий запрос, не scan_once.
    Когда цена двинулась на MOMENTUM_PCT% → отправляет алерт.
    """
    global _watch_candidates
    if not _watch_candidates:
        return

    now   = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    syms  = list(_watch_candidates.keys())
    prices = _fetch_spot_prices(syms)
    to_remove = []

    for sym, watch in _watch_candidates.items():
        age = now - watch["detected_at"]

        # Адаптивный TTL под тип паттерна
        _stage = watch.get("candidate", {}).get("stage", "")
        _ttl_h = watch.get("expiry_h") or WATCH_EXPIRY_BY_STAGE.get(_stage, WATCH_EXPIRY_H)
        if age > _ttl_h * 3600:
            _log(f"[Watch] {sym} истёк {_ttl_h}ч ({_stage}) без движения — удаляем")
            to_remove.append(sym)
            continue

        current = prices.get(sym)
        if not current:
            continue

        base   = watch["base_price"]
        c      = watch["candidate"]
        is_pump = c.get("signal_type") == "pump"

        # Считаем движение от точки обнаружения
        if is_pump:
            move_pct = (current - base) / base * 100
        else:
            move_pct = (base - current) / base * 100  # положительный если упало

        if move_pct < MOMENTUM_PCT:
            continue  # ещё не тронулось

        # 5M burst: реальный импульс прямо сейчас
        # 1H trend: старший TF не против направления сигнала
        _burst_pct = None
        _chg_1h    = None
        try:
            import requests as _req

            _kl_5m = _HTTP.get(
                f"{_BYBIT_BASE}/v5/market/kline",
                params={"category": "linear", "symbol": sym,
                        "interval": MOMENTUM_BURST_INTV, "limit": "3"},
                timeout=4,
            ).json()["result"]["list"]
            if len(_kl_5m) >= 2:
                _p5 = float(_kl_5m[1][4])  # close предыдущей 5м свечи
                if _p5 > 0:
                    _burst_pct = ((current - _p5) / _p5 * 100
                                  if is_pump else (_p5 - current) / _p5 * 100)

            _kl_1h = _HTTP.get(
                f"{_BYBIT_BASE}/v5/market/kline",
                params={"category": "linear", "symbol": sym,
                        "interval": "60", "limit": "3"},
                timeout=4,
            ).json()["result"]["list"]
            if len(_kl_1h) >= 2:
                _o1h = float(_kl_1h[1][1])
                _c1h = float(_kl_1h[1][4])
                if _o1h > 0:
                    _chg_1h = (_c1h - _o1h) / _o1h * 100
        except Exception:
            pass  # API недоступен — не блокируем

        if _burst_pct is not None and _burst_pct < MOMENTUM_BURST_PCT:
            _log(f"[Watch] {sym} ∑{'+' if is_pump else '-'}{move_pct:.2f}% "
                 f"но 5м={_burst_pct:+.2f}% < {MOMENTUM_BURST_PCT}% — дрейф, ждём")
            continue

        if _chg_1h is not None:
            _1h_against = -_chg_1h if is_pump else _chg_1h
            if _1h_against > MOMENTUM_1H_BLOCK:
                _log(f"[Watch] {sym} 1H={_chg_1h:+.2f}% — тренд против сигнала, блок")
                continue

        # Дневной лимит (наблюдательный pump-observe — отдельный бюджет, не ест лимит rug)
        _obs = _is_obs_pump(c)
        if _daily_count(today, _obs) >= _daily_cap(_obs):
            _log(f"[Watch] {sym} momentum +{move_pct:.2f}% но дневной лимит "
                 f"({'pump-obs' if _obs else 'общий'}) — пропуск")
            if not _obs:
                try:   # FIX 2026-05-30: объяснить в канал (раз/день)
                    import telegram_alerts as _ta
                    _ta.notify_suppression("pump_daily_limit",
                        f"📵 <b>Дневной лимит памп-алертов исчерпан</b> ({MAX_DAILY_ALERTS}/{MAX_DAILY_ALERTS}). Новых памп-сигналов сегодня не будет.",
                        cooldown_h=24)
                except Exception:
                    pass
            to_remove.append(sym)
            continue

        # Cooldown: символ уже алертился — снимаем из watch, не дёргаем Claude
        if now - _sent_recently.get(sym, 0) < COOLDOWN_SEC:
            _ago_min = int((now - _sent_recently.get(sym, 0)) / 60)
            _log(f"[Watch] {sym} momentum но cooldown активен ({_ago_min}мин назад) — удаляем")
            to_remove.append(sym)
            continue

        wait_min = int(age / 60)
        _log(f"[Watch] ✅ {sym} MOMENTUM {'+'if is_pump else '-'}{move_pct:.2f}% "
             f"за {wait_min}мин → алерт!")

        # Обновляем цену и добавляем инфо о задержке
        c["price"]              = current
        c["momentum_pct"]       = move_pct
        c["momentum_wait_m"]    = wait_min
        c["momentum_burst_5m"] = round(_burst_pct, 2) if _burst_pct is not None else None
        c["momentum_1h_chg"]   = round(_chg_1h, 2)   if _chg_1h  is not None else None

        # FIX 2026-06-02: обновить funding на момент momentum-входа (был detection-time, до 24ч стейл
        # у SLOW_DIST) — иначе FundGate в send_pump_alert и pump_pending кормятся протухшим funding.
        # Единицы: Bybit fundingRate (доля) × 100 = проценты (как в _analyze_symbol:993). OI/CVD
        # остаются detection-time (не гейты; обновление потребовало бы пересчёта по trades).
        try:
            _tk = _HTTP.get(f"{_BYBIT_BASE}/v5/market/tickers",
                            params={"category": "linear", "symbol": sym}, timeout=4
                            ).json()["result"]["list"][0]
            _fr = _tk.get("fundingRate")
            if _fr not in (None, ""):
                c["fund_prev"] = c.get("funding")
                c["funding"]   = round(float(_fr) * 100, 4)
        except Exception:
            pass

        _sent_ok = send_pump_alert(c, cfg=tg_cfg)
        if _sent_ok:
            _log_pump_pending(c)
            _sent_recently[sym] = now
            _daily_bump(today, _obs)
            _save_alert_state()   # FIX 2026-05-30: персист (рестарт-safe)
        to_remove.append(sym)

    for sym in to_remove:
        _watch_candidates.pop(sym, None)


# ── Claude WAIT-watchlist promoter ────────────────────────────────────────────

def _promote_wait_watchlist(tg_cfg: dict):
    """
    Раз в скан перепроверяет Claude WAIT-кандидаты со свежим состоянием рынка.
    Если новый _analyze_symbol() возвращает кандидата — повторно прогоняет через
    Claude фильтр. При verdict=GO — отправляет в TG.
    """
    try:
        from claude_realtime_filter import list_watchlist, promote_watchlist
    except Exception:
        return
    try:
        import screener as _scr
    except Exception:
        return
    items = list_watchlist()
    if not items:
        return
    try:
        tickers = _scr.fetch_all_tickers()
    except Exception:
        return
    try:
        btc_4h_now = _scr.fetch_btc_4h_change()
    except Exception:
        btc_4h_now = 0.0

    def _check(item: dict):
        sym = item.get("symbol", "")
        # Cooldown: не дёргаем Claude и не апгрейдим если символ только что алертился
        if time.time() - _sent_recently.get(sym, 0) < COOLDOWN_SEC:
            return False, None
        t = tickers.get(sym)
        if not t:
            return False, None
        try:
            fresh = _analyze_symbol(sym, t, _scr, btc_4h=btc_4h_now)
        except Exception:
            return False, None
        if fresh is None:
            return False, None
        # FIX 2026-06-02: промоут раньше обходил conviction-флор и DEX-обогащение (мог слать score<65 в TG).
        # Зеркалим watch(): DEX-adj → score_final → conviction-гейт → time-gate.
        try:
            dex = fetch_dexscreener_spot(sym)
            dex_adj = _dex_score_adj(dex, signal_type=fresh.get("signal_type", "pump"))
            fresh["dex"] = dex
            fresh["dex_adj"] = dex_adj
            fresh["score_final"] = fresh.get("score", 0) + dex_adj
        except Exception:
            fresh["score_final"] = fresh.get("score", 0)
        if fresh["score_final"] < CONVICTION_MIN_SCORE:
            _log(f"[Promote] {sym} score_final={fresh['score_final']} < {CONVICTION_MIN_SCORE} — пропуск (conviction)")
            return False, None
        _utc_h = datetime.now(timezone.utc).hour
        if _utc_h in _PUMP_BAD_HOURS and fresh["score_final"] < _PUMP_BAD_MIN_SCORE:
            _log(f"[Promote] {sym} UTC{_utc_h:02d} плохой час, score_final={fresh['score_final']} < {_PUMP_BAD_MIN_SCORE} — пропуск")
            return False, None
        return True, fresh

    promoted = promote_watchlist(_check)
    today = datetime.now().strftime("%Y-%m-%d")
    for p in promoted:
        _obs = _is_obs_pump(p["candidate"])
        if _daily_count(today, _obs) >= _daily_cap(_obs):
            _log(f"[Promote] {p['symbol']} GO от Claude, но дневной лимит "
                 f"({'pump-obs' if _obs else 'общий'}) — пропуск")
            if _obs:
                continue   # pump-obs исчерпал свой бюджет — rug ещё может пройти
            try:   # FIX 2026-05-30: объяснить в канал (раз/день)
                import telegram_alerts as _ta
                _ta.notify_suppression("pump_daily_limit",
                    f"📵 <b>Дневной лимит памп-алертов исчерпан</b> ({MAX_DAILY_ALERTS}/{MAX_DAILY_ALERTS}). Новых памп-сигналов сегодня не будет.",
                    cooldown_h=24)
            except Exception:
                pass
            break
        _sent_ok = send_pump_alert(p["candidate"], cfg=tg_cfg)
        if _sent_ok:
            _log_pump_pending(p["candidate"])
            _sent_recently[p["symbol"]] = time.time()
            _daily_bump(today, _obs)
            _save_alert_state()   # FIX 2026-05-30: персист (рестарт-safe)
            _log(f"[Promote] {p['symbol']} WAIT → GO → TG")
        else:
            _log(f"[Promote] {p['symbol']} send_pump_alert вернул False (фильтр/ошибка)")


# ── Watch loop ────────────────────────────────────────────────────────────────

def watch():
    _log(f"Запуск — скан каждые {SCAN_INTERVAL}с  |  топ {TOP_N_SYMBOLS} пар по объёму")
    try:
        import telegram_alerts as _tg
        tg_cfg = _tg.load_config()
    except Exception:
        tg_cfg = {}

    # SIGTERM (launchctl гасит демон именно им) → переиспользуем штатный KeyboardInterrupt-выход,
    # чтобы пройти корректный teardown (_shutdown_loop) и не оставить недозакрытый loop под GC.
    import signal as _signal

    def _on_sigterm(_signum, _frame):
        raise KeyboardInterrupt
    try:
        _signal.signal(_signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        pass   # не в главном потоке — пропускаем, поведение прежнее

    _load_alert_state()   # FIX 2026-05-30: восстановить cooldown/лимит после рестарта (in-memory обнулялся → дубли)

    last_full_scan        = 0.0
    last_momentum_check   = 0.0
    last_outcome_resolve  = 0.0

    while True:
        try:
            now    = time.time()
            today  = datetime.now().strftime("%Y-%m-%d")
            _utc_h = datetime.now(timezone.utc).hour

            # ── 1. Momentum check (каждые 120с — лёгкий запрос) ─────────────
            if now - last_momentum_check >= MOMENTUM_CHECK_SEC:
                if _utc_h not in _PUMP_HARD_BLOCK_HOURS:
                    _check_momentum(tg_cfg)
                    _promote_wait_watchlist(tg_cfg)
                last_momentum_check = time.time()

            # ── 2. Full scan (каждые 300с — тяжёлый) ────────────────────────
            if now - last_full_scan < SCAN_INTERVAL:
                time.sleep(30)
                continue

            # Исходы прошлых сигналов
            if now - last_outcome_resolve >= 3600:
                try:
                    _resolve_outcomes()
                except Exception:
                    pass
                last_outcome_resolve = time.time()

            # Time-gate: критические часы — только momentum check работает
            if _utc_h in _PUMP_HARD_BLOCK_HOURS:
                _log(f"[TimeGate] UTC{_utc_h:02d} — HARD BLOCK, скан приостановлен")
                last_full_scan = time.time()
                continue

            candidates = scan_once()
            last_full_scan = time.time()

            for c in candidates[:WATCH_TOP_N]:
                sym = c["symbol"]

                # Уже в watch-листе — не дублировать
                if sym in _watch_candidates:
                    continue

                # Cooldown: не слать одну монету чаще 1 раза в 4ч
                if time.time() - _sent_recently.get(sym, 0) < COOLDOWN_SEC:
                    continue

                # Дневной лимит (pump-observe — отдельный бюджет, чтобы не блокировать rug)
                _obs = _is_obs_pump(c)
                if _daily_count(today, _obs) >= _daily_cap(_obs):
                    _log(f"[DailyLimit] Лимит {_daily_cap(_obs)}/день "
                         f"({'pump-obs' if _obs else 'общий'}) — {sym} пропущен")
                    if _obs:
                        continue   # pump-obs бюджет исчерпан, но rug-кандидаты ещё ок
                    break          # общий лимит — стоп добора новых кандидатов

                # DEX-обогащение
                dex = fetch_dexscreener_spot(sym)
                dex_adj = _dex_score_adj(dex, signal_type=c.get("signal_type", "pump"))
                final_score = c["score"] + dex_adj
                c["dex"] = dex
                c["dex_adj"] = dex_adj
                c["score_final"] = final_score

                # Conviction gate
                if final_score < CONVICTION_MIN_SCORE:
                    _log(f"[Conviction] {sym} score={c['score']}{dex_adj:+d}={final_score} "
                         f"< {CONVICTION_MIN_SCORE} — пропуск")
                    continue

                # Time-gate: плохие часы — нужен более высокий score
                if _utc_h in _PUMP_BAD_HOURS and final_score < _PUMP_BAD_MIN_SCORE:
                    _log(f"[TimeGate] {sym} UTC{_utc_h:02d} плохой час, "
                         f"score={final_score} < {_PUMP_BAD_MIN_SCORE} — пропуск")
                    continue

                # → Watch list вместо немедленного алерта
                _ttl = WATCH_EXPIRY_BY_STAGE.get(c.get("stage", ""), WATCH_EXPIRY_H)
                _watch_candidates[sym] = {
                    "candidate":   c,
                    "base_price":  c["price"],
                    "detected_at": time.time(),
                    "expiry_h":    _ttl,
                }
                _log(f"[Watch] {c['stage']} {sym} score={final_score} "
                     f"→ ждём движения +{MOMENTUM_PCT}% (TTL {_ttl}ч) "
                     f"(watch: {len(_watch_candidates)} монет)")

        except KeyboardInterrupt:
            break
        except Exception as e:
            _log(f"Ошибка цикла: {e}")
        time.sleep(30)

    # Корректный выход: await-закрытие aiohttp-session + close резидентного loop
    # ДО завершения процесса → без _ssock-шума при сборке loop под py3.14.
    _log("Завершение — закрываю async-session и event loop")
    _shutdown_loop()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "watch":
        # try/finally на случай если SIGTERM→KeyboardInterrupt прилетит в time.sleep(30)
        # ВНЕ внутреннего try watch() (строка ~2354) — тогда он выходит из watch() мимо
        # своего _shutdown_loop(); добиваем teardown тут (идемпотентно — _LOOP уже None ок).
        try:
            watch()
        finally:
            _shutdown_loop()
    else:
        try:
            results = scan_once()
            if not results:
                print("Предварительных сигналов не найдено")
            else:
                print(f"\n{'SYM':15} {'STAGE':22} {'SC':>4}  {'OI4h':>6}  {'CVD':>6}  {'FUND':>7}  СИГНАЛЫ")
                print("─" * 100)
                for c in results[:15]:
                    src  = c.get("cvd_source", "?")[:1]  # T=Taker K=Kline
                    tldb = " ⚠TLDB" if c.get("tldb_notes") else ""
                    sigs = " | ".join(c["signals"])[:50]
                    print(f"  {c['symbol']:13} {c['stage']:20} {c['score']:4}  "
                          f"{c['oi_chg_4h']:+5.1f}%  {c['cvd_pct']:+5.0f}%({src})  "
                          f"{c['funding']:+6.4f}%  BTC{c.get('btc_4h',0):+.1f}%{tldb}  {sigs}")
        finally:
            _shutdown_loop()   # one-shot: тоже закрываем session+loop без _ssock-шума

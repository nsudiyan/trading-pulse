"""Bybit public kline WS, closed-candle snapshot rules and Telegram alerts."""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict, deque
import csv
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import operator
import os
from pathlib import Path
import random
import sqlite3
from statistics import median
import time

import aiohttp

from news import Store, calendar_context, parse_time, send_telegram, utc_now
from findings import (detect_bar_findings, trend_snapshot, ema_state,
                      _rsi, _macd_histogram)
from external import observations, fresh_closed_export, DURATION_MS
from microstructure import TradeFlow, VisibleBook, turnover_deviation
from structure import detect_structure_findings
from scenario_contract import (classify as classify_scenario, label as scenario_label,
                               ensure_schema as ensure_scenario_schema, save as save_scenario,
                               explanation as scenario_explanation, zone_observation, zone_note,
                               alert_heading, apply_direction_gate)
from btc_context import alt_btc_context, BTC_SYMBOLS
from quality import notification_quality
from setup_layers import (specification_progress, review_gate, timeframe_metrics,
                          quote_volume_24h)
from sessions import closed_asia_range
from alert_policy import (format_msk, session_at, session_label,
                          timeframe_alert_policy)
from level_age import level_alert_text
from zones import active_fvgs
from volume_profile import profile_windows
from sweeps import recent_sweep
from strong_sweep import strong_sweep_review
from provenance_guard import guarded_quality, verified_zone
from outcomes import ensure_schema as ensure_outcomes_schema, record_candidate, resolve_due
from followthrough import (ensure_schema as ensure_followthrough_schema,
                           resolve_due as resolve_followthrough)

LOG = logging.getLogger("news_chart_bot.market")
INTERVALS = {"15": 900_000, "60": 3_600_000, "240": 14_400_000,
             "D": 86_400_000, "W": 604_800_000}
TF_ORDER = ("W", "D", "240", "60", "15")
LABELS = {"W": "1 неделя", "D": "1 день", "240": "4 часа",
          "60": "1 час", "15": "15 минут"}
OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt,
       "<=": operator.le, "==": operator.eq, "!=": operator.ne}
_PULSE_CACHE: dict[str, tuple[int, dict, dict]] = {}


class Candles:
    def __init__(self, db: sqlite3.Connection, depth: int):
        self.db = db
        self.depth = depth
        self.bars: dict[tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=depth))
        db.executescript("""
          CREATE TABLE IF NOT EXISTS candles (
            symbol TEXT, interval TEXT, start_ms INTEGER, end_ms INTEGER,
            open REAL, high REAL, low REAL, close REAL, volume REAL, turnover REAL,
            PRIMARY KEY(symbol, interval, start_ms)
          );
          CREATE TABLE IF NOT EXISTS signal_alerts (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, text TEXT NOT NULL,
            created_utc TEXT NOT NULL, sent_utc TEXT
          );
          CREATE TABLE IF NOT EXISTS analyzed_bars (
            symbol TEXT NOT NULL, interval TEXT NOT NULL,
            start_ms INTEGER NOT NULL, analyzed_utc TEXT NOT NULL,
            PRIMARY KEY(symbol, interval, start_ms)
          );
          CREATE TABLE IF NOT EXISTS confluence_states (
            symbol TEXT NOT NULL, rule_id TEXT NOT NULL,
            active INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
            PRIMARY KEY(symbol, rule_id)
          );
        """)
        if not any(row[1] == "turnover" for row in db.execute("PRAGMA table_info(candles)")):
            db.execute("ALTER TABLE candles ADD COLUMN turnover REAL")
        if not any(row[1] == "reason" for row in db.execute("PRAGMA table_info(signal_alerts)")):
            db.execute("ALTER TABLE signal_alerts ADD COLUMN reason TEXT")
        db.commit()

    def load(self, symbols: list[str], intervals: list[str]) -> None:
        for symbol in symbols:
            for interval in intervals:
                rows = self.db.execute("""
                  SELECT * FROM candles WHERE symbol=? AND interval=?
                  ORDER BY start_ms DESC LIMIT ?
                """, (symbol, interval, self.depth)).fetchall()
                self.bars[symbol, interval].extend(dict(row) for row in reversed(rows))

    def put(self, bar: dict, *, commit: bool = True) -> bool:
        bar = {**bar, "turnover": bar.get("turnover")}
        key = (bar["symbol"], bar["interval"])
        series = self.bars[key]
        if series and bar["start_ms"] < series[-1]["start_ms"]:
            # REST backfill is allowed only for missing older candles.
            if any(b["start_ms"] == bar["start_ms"] for b in series):
                return False
            ordered = sorted([*series, bar], key=lambda b: b["start_ms"])[-self.depth:]
            series.clear()
            series.extend(ordered)
        elif series and bar["start_ms"] == series[-1]["start_ms"]:
            series[-1] = bar
        else:
            series.append(bar)
        self.db.execute("""
          INSERT OR REPLACE INTO candles
            (symbol,interval,start_ms,end_ms,open,high,low,close,volume,turnover)
          VALUES
          (:symbol,:interval,:start_ms,:end_ms,:open,:high,:low,:close,:volume,:turnover)
        """, bar)
        if commit:
            self.db.commit()
        return True

    def latest(self, symbol: str, interval: str) -> dict | None:
        series = self.bars[symbol, interval]
        return series[-1] if series else None

    def snapshot_at(self, symbol: str, intervals: list[str], end_ms: int) -> "CandleSnapshot":
        return CandleSnapshot({(symbol, tf): [bar for bar in self.bars[symbol, tf]
                                              if bar["end_ms"] <= end_ms]
                               for tf in intervals})


class CandleSnapshot:
    """Event-time view; later closes cannot affect a repaired older signal."""
    def __init__(self, bars: dict[tuple[str, str], list[dict]]):
        self.bars = bars

    def latest(self, symbol: str, interval: str) -> dict | None:
        series = self.bars[symbol, interval]
        return series[-1] if series else None


def review_cooldown_conflict(db: sqlite3.Connection, symbol: str,
                             now: datetime, hours: float) -> bool:
    """Block another automatic review while one is queued or recently sent.

    The queue itself is durable, so this also survives process restarts. A
    pending alert reserves the symbol until delivery or expiry; successful
    delivery starts the four-hour window from sent_utc, not enqueue time.
    """
    if hours <= 0:
        return False
    prefix = f"review:{symbol}:"
    # ASCII ':' < ';': this exact-symbol range uses the primary-key index
    # instead of scanning every historical alert with substr(id,...).
    upper = prefix[:-1] + ";"
    cutoff = (now - timedelta(hours=hours)).isoformat()
    return db.execute("""
        SELECT 1 FROM signal_alerts
        WHERE id>=? AND id<? AND
              (status='pending' OR (status='sent' AND sent_utc>=?))
        LIMIT 1
    """, (prefix, upper, cutoff)).fetchone() is not None


def review_is_stale_for_delivery(alert_id: str, now: datetime,
                                 max_lag_minutes: float) -> bool:
    """Measure review age from its candle close, not queue creation time.

    Operational health notices have no candle and are handled separately.
    The persisted review key encodes the exact UTC candle start and interval.
    """
    if not alert_id.startswith("review:") or max_lag_minutes <= 0:
        return False
    try:
        _, _symbol, interval, start = alert_id.split(":", 3)
        closed_ms = int(start) + INTERVALS[interval]
    except (ValueError, KeyError):
        # A malformed review cannot be shown as a timely market observation.
        return True
    return now.timestamp() * 1000 - closed_ms > max_lag_minutes * 60_000


def funding_asof(history: deque[tuple[int, float]], end_ms: int,
                 max_age_minutes: float) -> float | None:
    """Latest BTC ticker funding known before the closed candle ended."""
    max_age_ms = max(0, float(max_age_minutes)) * 60_000
    return next((rate for ts, rate in reversed(history)
                 if ts <= end_ms and end_ms - ts <= max_age_ms), None)


def bar_from_ws(symbol: str, interval: str, data: dict) -> dict:
    start = int(data["start"])
    return {"symbol": symbol, "interval": interval, "start_ms": start,
            "end_ms": int(data.get("end", start + INTERVALS[interval] - 1)),
            "open": float(data["open"]), "high": float(data["high"]),
            "low": float(data["low"]), "close": float(data["close"]),
            "volume": float(data["volume"]),
            "turnover": float(data["turnover"]) if "turnover" in data else None}


def bar_from_rest(symbol: str, interval: str, data: list[str]) -> dict:
    start = int(data[0])
    return {"symbol": symbol, "interval": interval, "start_ms": start,
            "end_ms": start + INTERVALS[interval] - 1,
            "open": float(data[1]), "high": float(data[2]),
            "low": float(data[3]), "close": float(data[4]),
            "volume": float(data[5]),
            "turnover": float(data[6]) if len(data) > 6 else None}


def pulse_ratio(config: dict, symbol: str, interval: str, start_ms: int,
                now: datetime) -> float | None:
    """Optional read-only export: exact candle join, no Bybit-volume fallback."""
    if config.get("type") not in {"json_file", "csv_file"}:
        return None
    try:
        path = Path(config["path"])
        mtime = path.stat().st_mtime_ns
        key = f"{config['type']}:{path.resolve()}"
        cached = _PULSE_CACHE.get(key)
        if cached is None or cached[0] != mtime:
            if config["type"] == "json_file":
                payload = json.loads(path.read_text(encoding="utf-8"))
            else:
                with path.open(encoding="utf-8-sig", newline="") as source:
                    payload = {"rows": list(csv.DictReader(source))}
            lookup = {}
            for row in payload["rows"]:
                row_key = (row["symbol"], str(row["interval"]), int(row["start_ms"]))
                # Ambiguous duplicate export rows must not silently overwrite.
                lookup[row_key] = None if row_key in lookup else row
            _PULSE_CACHE[key] = (mtime, payload, lookup)
        else:
            _, payload, lookup = cached
        row = lookup.get((symbol, interval, start_ms))
        if not row or str(row.get("coverage_complete", "")).lower() not in {"true", "1"}:
            return None
        generated = parse_time(row.get("computed_at_utc") or payload.get("computed_at_utc"))
        if not fresh_closed_export(interval, start_ms, generated, now,
                                   config.get("max_age_minutes", 30)):
            return None
        if row.get("volume_ratio") not in (None, ""):
            ratio = float(row["volume_ratio"])
        else:
            bars = int(config.get("volume_baseline_bars", 20))
            unit = row.get("volume_unit")
            if bars < 3 or bars > 200 or unit not in {"base", "quote"}:
                return None
            baseline = []
            for offset in range(1, bars + 1):
                prior = lookup.get((symbol, interval,
                                    start_ms - offset * DURATION_MS[interval]))
                if (not prior or prior.get("volume_unit") != unit or
                        str(prior.get("coverage_complete", "")).lower() not in {"true", "1"}):
                    return None
                value = float(prior["volume"])
                if not math.isfinite(value) or value < 0:
                    return None
                baseline.append(value)
            base = median(baseline)
            if base <= 0:
                return None
            ratio = float(row["volume"]) / base
        return ratio if math.isfinite(ratio) and ratio > 0 else None
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None
    return None


def metric_value(rule: dict, series: deque, pulse: dict, symbol: str,
                 interval: str, now: datetime) -> float | None:
    if not series:
        return None
    current = series[-1]
    metric = rule["metric"]
    if metric == "close":
        return current["close"]
    if metric == "pct_change" and len(series) >= 2 and series[-2]["close"]:
        return (current["close"] / series[-2]["close"] - 1) * 100
    if metric == "close_sma_ratio" and len(series) >= int(rule.get("period", 20)):
        period = int(rule.get("period", 20))
        if period <= 0 or period > 1000:
            return None
        sma = sum(b["close"] for b in list(series)[-period:]) / period
        return current["close"] / sma if sma else None
    if metric == "pulse_volume_ratio":
        return pulse_ratio(pulse, symbol, interval, current["start_ms"], now)
    if metric == "rsi14":
        return _rsi([float(bar["close"]) for bar in series])
    if metric == "macd_hist":
        values = _macd_histogram([float(bar["close"]) for bar in series])
        return values[-1] if values else None
    return None


def evaluate_snapshot(symbol: str, rule: dict, candles: Candles, intervals: list[str],
                      pulse: dict, now: datetime, grace_minutes: int,
                      at_ms: int | None = None) -> dict[str, dict]:
    """One event-time snapshot evaluates the rule across all intervals together."""
    result = {}
    for interval in intervals:
        series = candles.bars[symbol, interval]
        latest = series[-1] if series else None
        if latest is None:
            result[interval] = {"status": "нет данных"}
            continue
        age_ms = (at_ms if at_ms is not None else int(now.timestamp() * 1000)) - (latest["end_ms"] + 1)
        if age_ms > INTERVALS[interval] + grace_minutes * 60_000:
            result[interval] = {"status": "устарело", "bar": latest}
            continue
        value = metric_value(rule, series, pulse, symbol, interval, now)
        if value is None or not math.isfinite(value):
            result[interval] = {"status": "нет данных для условия", "bar": latest}
            continue
        matched = OPS[rule["operator"]](value, float(rule["value"]))
        result[interval] = {"status": "сработало" if matched else "нет",
                            "value": value, "bar": latest}
    return result


def evaluate_confluence(rule: dict, symbol: str, view: CandleSnapshot,
                        tf_findings: dict[str, list[dict]], pulse: dict,
                        now: datetime, at_ms: int, grace_minutes: int) -> tuple[bool, list[str]]:
    """Evaluate a JSON-defined conjunction/disjunction on one event-time view."""
    outcomes: list[bool] = []
    descriptions: list[str] = []
    for check in rule["checks"]:
        tf = check["tf"]
        latest = view.latest(symbol, tf)
        age = at_ms - (latest["end_ms"] + 1) if latest else None
        if latest is None or age is None or age > INTERVALS[tf] + grace_minutes * 60_000:
            outcomes.append(False)
            descriptions.append(f"{LABELS[tf]}: нет свежей свечи")
            continue
        if "finding" in check:
            actual = any(item["code"] == check["finding"] for item in tf_findings[tf])
            expected = check.get("equals", True)
            matched = actual == expected
            detail = f"{check['finding']}={actual}"
        elif check.get("metric") == "ema_direction":
            state = ema_state(view.bars[symbol, tf])
            actual = state["label"] if state else None
            matched = actual == check["value"] if actual is not None else False
            detail = f"EMA={actual or 'недостаточно истории'}"
        else:
            actual = metric_value(check, view.bars[symbol, tf], pulse, symbol, tf, now)
            matched = (actual is not None and math.isfinite(actual)
                       and OPS[check["operator"]](actual, float(check["value"])))
            detail = f"{check['metric']}={actual:.4g}" if actual is not None else f"{check['metric']}=нет данных"
        outcomes.append(matched)
        descriptions.append(f"{LABELS[tf]}: {detail} {'✓' if matched else '×'}")
    active = all(outcomes) if rule.get("mode", "all") == "all" else any(outcomes)
    return active, descriptions


def format_signal(symbol: str, rule: dict, snapshot: dict, news: Store,
                  calendar: dict, now: datetime, news_config: dict) -> str:
    lines = [f"📊 {symbol} · {rule['id']}", f"Анализ UTC: {now.isoformat()}",
             f"Условие: {rule['metric']} {rule['operator']} {rule['value']}"]
    for interval in snapshot:
        item = snapshot[interval]
        bar = item.get("bar")
        closed = format_msk(bar["end_ms"] + 1) if bar else "—"
        value = f" ({item['value']:.4g})" if "value" in item else ""
        lines.append(f"{LABELS[interval]}: {item['status']}{value}; свеча до {closed}")
    items = news.recent(symbol, news_config.get("context_hours", 4),
                        news_config.get("max_context_items", 3), now)
    lines.append("Новости: " + ("; ".join(f"{item['source']}: {item['title'][:100]} ({item['url']})"
                                            for item in items) if items else "нет свежих проверенных публикаций"))
    events = calendar_context(calendar, now)
    lines.append("Календарь: " + ("; ".join(f"{e['title']} {e['at_utc']}"
                                              for e in events) if events else "нет события в заданном окне"))
    lines.append("Новость и совпадение условий не доказывают направление следующей свечи.")
    return "\n".join(lines)[:3900]


def scenario_context(bar, findings, view, sweep=None, gate=None):
    contexts = {}
    for tf in ("240", "D"):
        bars = [b for b in view.bars.get((bar["symbol"], tf), []) if b["end_ms"] <= bar["end_ms"]]
        contexts[tf] = {"end_ms": bars[-1]["end_ms"] if bars else None,
                        "trend": timeframe_metrics(bars, tf)["trend"] if bars else None}
    scenario = classify_scenario(findings, contexts, int(bar["end_ms"]), sweep,
                                 zone_observation(gate, int(bar["end_ms"]), bar["close"]),
                                 event_timeframe=bar["interval"])
    scenario["explanation"] = scenario_explanation(scenario)
    scenario["event_time_msk"] = format_msk(int(bar["end_ms"]) + 1)
    scenario["session_msk"] = session_at(int(bar["end_ms"]) + 1)
    # Immutable descriptive measurement baseline, not an executable entry.
    scenario["baseline"] = {"method":"event_close_v1", "price":bar["close"],
                            "event_ms":int(bar["end_ms"])+1, "source":"signal_closed_OHLC"}
    return scenario


def format_review_alert(bar: dict, findings: list[dict], rule_hits: list[dict],
                        trend: dict[str, str], tf_findings: dict[str, list[dict]],
                        rule_snapshots: list[tuple[str, dict]], micro: dict,
                        candles: Candles, btc_lines: list[str], spec_lines: list[str],
                        session_lines: list[str],
                        quality_label: str, news: Store,
                        calendar: dict, now: datetime, news_config: dict,
                        stale_grace_minutes: int = 10,
                        gate: dict | None = None,
                        sweep: dict | None = None,
                        scenario: dict | None = None,
                        category: str = "linear") -> str:
    """One review message per closed candle, including every observed fact."""
    symbol, interval = bar["symbol"], bar["interval"]
    closed_ms = int(bar["end_ms"]) + 1
    closed = datetime.fromtimestamp(closed_ms / 1000, timezone.utc)
    heading = f"{alert_heading(symbol, scenario, category)} · закрылась {LABELS[interval]}"
    lines = [heading, "Статус направления: " + scenario_explanation(scenario), zone_note(scenario)]
    if gate and gate["send"]:
        pattern = (f"{sweep['code']} {sweep['direction']} @ {sweep['level']:g}"
                   if sweep else "паттерн не подтверждён")
        lines.extend([f"📊 Почему: 4ч/15м согласованы; {gate['count']}/5 ТФ; "
                      f"4ч зона {gate['zone']}; {pattern}.",
                      "📍 Действие: проверить график самостоятельно; "
                      "точка входа, стоп и цели не подтверждены."])
    lines.extend([f"Свеча закрыта: {format_msk(closed_ms)}",
             f"Анализ UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}",
             f"OHLC: {bar['open']:g} / {bar['high']:g} / {bar['low']:g} / {bar['close']:g}"])
    level_line = level_alert_text((scenario or {}).get("level_observation"))
    if level_line:
        lines.append(level_line)
    lines.extend(btc_lines)
    lines.extend(session_lines)
    lines.extend(spec_lines)
    if quality_label:
        lines.append(quality_label)
    lines.append(f"Контекст {len(tf_findings)} таймфреймов (EMA и свечные признаки):")
    for tf in tf_findings:
        latest = candles.latest(symbol, tf)
        tf_closed = format_msk(latest["end_ms"] + 1) if latest else "нет"
        stale = (latest is not None and closed_ms - (latest["end_ms"] + 1)
                 > INTERVALS[tf] + stale_grace_minutes * 60_000)
        suffix = "; устарело" if stale else ""
        names = ", ".join(item["name"] for item in tf_findings[tf][:3]) or "нет признаков"
        lines.append(f"• {LABELS[tf]}: {trend[tf]}; {names}; свеча {tf_closed}{suffix}")
    for rule_id, snapshot in rule_snapshots:
        statuses = ", ".join(f"{LABELS[tf]} {snapshot[tf]['status']}"
                             for tf in tf_findings)
        lines.append(f"• Правило {rule_id}: {statuses}")
    lines.append("Наблюдения (для вашей проверки):")
    for item in findings:
        lines.append(f"• {item['name']}: {item['evidence']} [{item['source']}]")
    for item in rule_hits:
        lines.append(f"• Ваше правило {item['id']}: {item['metric']}={item['value']:.4g}")
    items = news.recent(symbol, news_config.get("context_hours", 4),
                        news_config.get("max_context_items", 3), closed)
    if items:
        lines.append("Новости:")
        lines.extend(f"• {item['source']}: {item['title'][:90]} — {item['url']}"
                     for item in items)
    else:
        lines.append("Новости: свежих совпадений нет")
    events = calendar_context(calendar, closed)
    if events:
        lines.append("Календарь: " + "; ".join(f"{e['title']} {e['at_utc']}" for e in events))
    flow = micro.get("flow")
    if flow:
        lines.append(f"Bybit taker: Buy {flow['buy_quote']:,.0f} / Sell {flow['sell_quote']:,.0f} USDT; "
                     f"дельта {flow['delta_quote']:+,.0f} USDT; {flow['trade_count']} сделок")
    else:
        deviation = micro.get("flow_turnover_deviation")
        if deviation is not None:
            lines.append(f"Bybit taker/дельта: сумма сделок отличается от turnover "
                         f"свечи на {deviation:.2%}; интервал отвергнут")
        else:
            lines.append("Bybit taker/дельта: нет полного непрерывного интервала")
    cvd = micro.get("cvd")
    if cvd:
        since = datetime.fromtimestamp(cvd["since_ms"] / 1000, timezone.utc).strftime("%m-%d %H:%M UTC")
        lines.append(f"CVD с {since}: {cvd['delta_quote']:+,.0f} USDT (только текущий непрерывный сегмент)")
    book = micro.get("book")
    if book:
        lines.append(f"Видимый стакан L50 у закрытия: bid {book['bid_fraction']:.0%}, "
                     f"ask {1-book['bid_fraction']:.0%}; {book['sample_count']} отсчётов/30с")
    else:
        lines.append("Видимый стакан L50: нет полной свежей выборки")
    profile = micro.get("profile")
    if profile:
        lines.append(f"Профиль сделок: POC корзина от {profile['poc_low']:g} "
                     f"(шаг {profile['bucket_step']:g}); "
                     f"наибольшая дельта {profile['largest_delta_quote']:+,.0f} USDT "
                     f"у {profile['largest_delta_low']:g}")
        if profile["hvn_lows"] or profile["lvn_lows"]:
            lines.append("Локальные узлы профиля: HVN "
                         + (", ".join(f"{x:g}" for x in profile["hvn_lows"]) or "—")
                         + "; LVN "
                         + (", ".join(f"{x:g}" for x in profile["lvn_lows"]) or "—"))
    lines.append("Внешние метрики помечены своим источником; скрытые ордера не наблюдаются.")
    footer = "NO-TRADE: сценарий не является точкой входа. Вход, стоп, TP и R:R не подтверждены."
    body = "\n".join(lines)
    return body[:3900 - len(footer) - 1] + "\n" + footer


def format_compact_alert(bar: dict, findings: list[dict], tf_findings: dict,
                         view: Candles, btc_lines: list[str], session_lines: list[str],
                         spec_lines: list[str], micro: dict,
                         news: Store, news_config: dict, now: datetime,
                         gate: dict | None, sweep: dict | None,
                         funding: float | None, quality: dict,
                         category: str, scenario: dict | None = None) -> str:
    """Facts first; no invented execution levels or OHLCV volume-at-price claims."""
    symbol, end_ms = bar["symbol"], int(bar["end_ms"])
    closed_ms = end_ms + 1
    closed = datetime.fromtimestamp(closed_ms / 1000, timezone.utc)
    side = scenario_label(scenario)
    kind = ("сильный sweep для проверки" if quality["reason"] == "strong_sweep_review"
            else "свечной обзор для проверки")
    lines = [f"{alert_heading(symbol, scenario, category)} · {kind}",
             "Статус направления: " + scenario_explanation(scenario),
             zone_note(scenario),
             f"Свеча: {LABELS[bar['interval']]} · {format_msk(closed_ms)}",
             f"Анализ: {now:%Y-%m-%d %H:%M:%S} UTC",
             f"OHLC: {bar['open']:g} / {bar['high']:g} / {bar['low']:g} / {bar['close']:g}"]
    level_line = level_alert_text((scenario or {}).get("level_observation"))
    if level_line:
        lines.append(level_line)
    if sweep:
        ratio = sweep.get("volume_ratio")
        lines.append(f"Паттерн: {sweep['code']} · {sweep['direction']} · уровень {sweep['level']:g}"
                     + (f" · объём {ratio:.2f}× MA14" if ratio is not None else ""))
        lines.append("Прокол и возврат цены подтверждены закрытой свечой; исполнение стопов не наблюдается.")
    else:
        lines.append("Sweep на закрытой свече не подтверждён; обзор без точки входа.")
    if gate:
        if quality["reason"] == "strong_sweep_review":
            lines.append(f"4ч: EMA {gate['direction']}; зона {gate['zone']} по подтверждённым pivot; "
                         f"диапазон {gate['swing_low']:g}–{gate['swing_high']:g}, "
                         f"середина {gate['swing_mid']:g}.")
            lines.append(f"Оборот за 24ч: {gate['quote_24h']:,.0f} {('USD' if category == 'inverse' else symbol[-4:])}; "
                         f"семейств признаков: {len(gate['families'])}.")
        else:
            lines.append(f"Фильтр: {gate['count']}/5 ТФ по направлению; 4ч зона {gate['zone']}.")
    lines.append("Таймфреймы (EMA20/50/200, только закрытые свечи):")
    for tf in TF_ORDER:
        latest = view.latest(symbol, tf)
        stamp = format_msk(latest["end_ms"] + 1) if latest else "нет данных"
        direction = (gate.get("trends", {}).get(tf) if gate else None)
        if direction is None and latest:
            direction = timeframe_metrics(view.bars.get((symbol, tf), []), tf)["trend"]
        names = ", ".join(item["name"] for item in tf_findings.get(tf, [])[:2]) or "нет признаков"
        lines.append(f"• {LABELS[tf]}: {direction or 'нет данных'}; {names}; {stamp}")
    lines.extend(btc_lines[:5])
    lines.extend(session_lines[:2])
    lines.extend(spec_lines[:3])
    lines.extend(line for line in spec_lines[3:] if line.startswith(("VP range:", "4ч FVG")))
    flow = micro.get("flow")
    if flow:
        lines.append(f"Bybit taker: Buy {flow['buy_quote']:,.0f} / Sell {flow['sell_quote']:,.0f}; "
                     f"дельта {flow['delta_quote']:+,.0f} USDT (полный сегмент)")
    elif symbol in BTC_SYMBOLS:
        lines.append("Bybit taker/дельта: нет полного непрерывного интервала")
    lines.append(f"BTC funding на момент свечи: {funding:+.6f}" if funding is not None
                 else "BTC funding на момент свечи: нет данных")
    if findings:
        lines.append("Дополнительные признаки: " + "; ".join(
            f"{item['name']} [{item['source']}]" for item in findings[:5]))
    items = news.recent(symbol, news_config.get("context_hours", 4),
                        min(2, news_config.get("max_context_items", 3)), closed)
    lines.append("Новости: " + ("; ".join(f"{item['source']}: {item['title'][:65]} {item['url']}"
                                           for item in items) if items else "свежих совпадений нет"))
    lines.append("NO-TRADE: сценарий не является точкой входа. Вход, стоп, TP и R:R не подтверждены.")
    message = "\n".join(lines)
    if len(message) <= 3900:
        return message
    footer = lines[-1]
    marker = "\n…контекст сокращён\n"
    body = "\n".join(lines[:-1])
    return body[:3900 - len(marker) - len(footer)] + marker + footer


class Engine:
    def __init__(self, config: dict):
        self.config = config
        self.market = config["market"]
        self.store = Store(config["database"])
        self.candles = Candles(self.store.db, max(30, int(self.market.get("history_bars", 250))))
        ensure_outcomes_schema(self.store.db)
        ensure_followthrough_schema(self.store.db)
        ensure_scenario_schema(self.store.db)
        self.candles.load(self.market["symbols"], self.market["intervals"])
        self.micro_cfg = config.get("microstructure", {})
        self.micro_symbols = (set(self.micro_cfg.get("symbols", []))
                              if self.micro_cfg.get("enabled", False) else set())
        self.flow = TradeFlow(self.store.db, self.micro_cfg.get("price_bucket_quote"))
        self.book = VisibleBook()
        # Bybit ticker sends snapshots and sparse deltas. Retain recent
        # timestamped values so a closed candle never sees a later update.
        self.btc_funding_rate: float | None = None
        self.btc_funding_history: deque[tuple[int, float]] = deque(maxlen=3600)
        self.lock = asyncio.Lock()
        self.send_lock = asyncio.Lock()
        self.previewed: set[str] = set()
        self.backfill_lock = asyncio.Lock()
        self.ready_symbols: set[str] = set(self.market["symbols"] if not
            self.market.get("universe", {}).get("enabled") else [])
        if self.market.get("universe", {}).get("enabled"):
            now_ms = int(time.time() * 1000)
            grace = self.market.get("stale_grace_minutes", 10) * 60_000
            for symbol in self.market["symbols"]:
                if all((bar := self.candles.latest(symbol, tf)) is not None
                       and now_ms - (bar["end_ms"] + 1) <= INTERVALS[tf] + grace
                       for tf in self.market["intervals"]):
                    self.ready_symbols.add(symbol)
        self.rest_gate = asyncio.Lock()
        self.next_rest_at = 0.0

    def health_notice(self, source: str, detail: str) -> None:
        """At most one operational notice per source and UTC hour."""
        now = utc_now()
        key = f"health:{source}:{now.strftime('%Y%m%d%H')}"
        self.store.db.execute("""INSERT OR IGNORE INTO signal_alerts
                  (id,status,text,created_utc) VALUES (?,?,?,?)""",
                  (key, "pending", f"⚠️ Бот: {source}\n{detail}\nUTC: {now.isoformat()}",
                   now.isoformat()))
        self.store.db.commit()

    async def on_bar(self, bar: dict) -> None:
        async with self.lock:
            # Hundreds of close events become ready at the same instant.
            # Yield while holding the analysis lock so WS ping/receive and
            # news I/O get CPU before the next synchronous SQLite/CPU slice.
            await asyncio.sleep(0.001)
            identity = (bar["symbol"], bar["interval"], bar["start_ms"])
            if self.store.db.execute("""SELECT 1 FROM analyzed_bars
                                      WHERE symbol=? AND interval=? AND start_ms=?""",
                                     identity).fetchone():
                return
            if bar["symbol"] not in self.ready_symbols:
                # A newly discovered contract needs historical context before
                # EMA, RSI and multi-timeframe findings can be trusted.
                self.candles.put(bar)
                self.store.db.execute("INSERT OR IGNORE INTO analyzed_bars VALUES (?,?,?,?)",
                                      (*identity, utc_now().isoformat()))
                self.store.db.commit()
                return
            stored = self.candles.put(bar)
            if not stored:
                existing_bar = next((item for item in self.candles.bars[bar["symbol"], bar["interval"]]
                                     if item["start_ms"] == bar["start_ms"]), None)
                if existing_bar is None:
                    return
                bar = existing_bar
            now = utc_now()
            age_ms = int(now.timestamp() * 1000) - (bar["end_ms"] + 1)
            if (age_ms < -60_000 or
                    age_ms > self.market.get("max_alert_age_minutes", 90) * 60_000):
                return
            view = self.candles.snapshot_at(bar["symbol"], self.market["intervals"],
                                            bar["end_ms"])
            findings_cfg = self.market.get("findings", {})
            structure_cfg = self.market.get("structure", {})
            tf_findings = {}
            for tf in self.market["intervals"]:
                tf_items = (detect_bar_findings(view.bars[bar["symbol"], tf], findings_cfg)
                            if findings_cfg.get("enabled", True) else [])
                if structure_cfg.get("enabled", False):
                    tf_items.extend({**item, "timeframe": tf}
                                    for item in detect_structure_findings(
                                        view.bars[bar["symbol"], tf], structure_cfg))
                tf_findings[tf] = tf_items
            findings = list(tf_findings[bar["interval"]])
            findings.extend(observations(self.config.get("external_observations", {}),
                                         bar["symbol"], bar["interval"],
                                         bar["start_ms"], now))
            micro: dict = {}
            if bar["symbol"] in self.micro_symbols:
                flow = self.flow.candle(bar["symbol"], bar["start_ms"],
                                        bar["end_ms"], int(now.timestamp() * 1000),
                                        self.micro_cfg.get("min_trade_quote", 100_000))
                deviation = turnover_deviation(flow, bar.get("turnover"))
                if deviation is not None and deviation > self.micro_cfg.get("max_turnover_error_ratio", 0.001):
                    micro["flow_turnover_deviation"] = deviation
                    self.flow.disconnected(bar["symbol"])
                    self.flow.connected(bar["symbol"], int(now.timestamp() * 1000))
                    self.health_notice("Bybit publicTrade turnover",
                                       f"{bar['symbol']} {bar['interval']}: расхождение {deviation:.2%}; "
                                       "дельта скрыта до нового полного интервала.")
                    flow = None
                micro["flow"] = flow
                micro["cvd"] = self.flow.cvd(bar["symbol"], bar["end_ms"])
                micro["book"] = self.book.near_close(bar["symbol"], bar["end_ms"] + 1)
                micro["profile"] = (self.flow.price_profile(bar["symbol"], bar["start_ms"],
                                                            bar["end_ms"], int(now.timestamp() * 1000),
                                                            self.micro_cfg.get("min_cluster_quote", 10_000))
                                    if flow else None)
                profile = micro["profile"]
                if profile and profile["imbalance"]:
                    item = profile["imbalance"]
                    findings.append({"code": "price_bucket_imbalance",
                                     "name": f"Кластер taker {item['side']} в ценовой корзине",
                                     "evidence": f"от {item['bucket_low']:g}: Buy {item['buy_quote']:,.0f} / "
                                                 f"Sell {item['sell_quote']:,.0f} USDT",
                                     "source": "Bybit publicTrade"})
                if flow:
                    if flow["buy_fraction"] >= self.micro_cfg.get("trade_buy_fraction", 0.65):
                        findings.append({"code": "taker_buy_dominance", "name": "Преобладание taker Buy",
                                         "evidence": f"Buy {flow['buy_fraction']:.0%} оборота; дельта {flow['delta_quote']:+,.0f} USDT",
                                         "source": "Bybit publicTrade"})
                    if flow["buy_fraction"] <= self.micro_cfg.get("trade_sell_fraction", 0.35):
                        findings.append({"code": "taker_sell_dominance", "name": "Преобладание taker Sell",
                                         "evidence": f"Sell {1-flow['buy_fraction']:.0%} оборота; дельта {flow['delta_quote']:+,.0f} USDT",
                                         "source": "Bybit publicTrade"})
                book = micro["book"]
                if book:
                    if book["bid_fraction"] >= self.micro_cfg.get("book_bid_fraction", 0.65):
                        findings.append({"code": "visible_bid_imbalance", "name": "Перевес видимых bid L50",
                                         "evidence": f"bid {book['bid_fraction']:.0%} среднего top-10/30с",
                                         "source": "Bybit orderbook.50"})
                    if book["bid_fraction"] <= self.micro_cfg.get("book_ask_fraction", 0.35):
                        findings.append({"code": "visible_ask_imbalance", "name": "Перевес видимых ask L50",
                                         "evidence": f"ask {1-book['bid_fraction']:.0%} среднего top-10/30с",
                                         "source": "Bybit orderbook.50"})
            ratio = pulse_ratio(self.config["pulse"], bar["symbol"], bar["interval"],
                                bar["start_ms"], now)
            if (findings_cfg.get("enabled", True) and ratio is not None
                    and ratio >= findings_cfg.get("pulse_volume_ratio_min", 2.0)):
                findings.append({"code": "pulse_volume_spike", "name": "Повышенный объём Пульса",
                                 "evidence": f"отношение к базе {ratio:.2f}×", "source": "Пульс"})
            trend = trend_snapshot(view, bar["symbol"], self.market["intervals"])
            aligned = {trend[interval] for interval in self.market["intervals"]}
            snapshot_fresh = all(
                view.latest(bar["symbol"], tf) is not None and
                (bar["end_ms"] + 1) - view.latest(bar["symbol"], tf)["end_ms"]
                <= INTERVALS[tf] + self.market.get("stale_grace_minutes", 10) * 60_000
                for tf in self.market["intervals"]
            )
            if snapshot_fresh and len(aligned) == 1 and next(iter(aligned)) in {"рост", "снижение"} and any(
                item["code"].startswith("ema_alignment_") or item["code"].startswith("range_close_")
                for item in findings
            ):
                direction = next(iter(aligned))
                findings.append({"code": "all_tf_alignment",
                                 "name": f"Совпадение направления {len(self.market['intervals'])} ТФ",
                                 "evidence": f"EMA20/50/200 указывают на {direction} на "
                                             + ", ".join(LABELS[tf] for tf in self.market["intervals"]),
                                 "source": "Bybit OHLC"})
            rule_hits = []
            rule_snapshots = []
            for rule in self.market["rules"]:
                if not rule.get("enabled", True):
                    continue
                snapshot = evaluate_snapshot(bar["symbol"], rule, view,
                                             self.market["intervals"], self.config["pulse"],
                                             now, self.market.get("stale_grace_minutes", 10),
                                             bar["end_ms"] + 1)
                rule_snapshots.append((rule["id"], snapshot))
                if self.market.get("require_complete_snapshot", True) and any(
                    item["status"] in {"нет данных", "устарело", "нет данных для условия"}
                    for item in snapshot.values()
                ):
                    continue
                if snapshot[bar["interval"]]["status"] != "сработало":
                    continue
                rule_hits.append({"id": rule["id"], "metric": rule["metric"],
                                  "value": snapshot[bar["interval"]].get("value")})
            confluence_updates: list[tuple[str, str, int, int]] = []
            for rule in self.market.get("confluence_rules", []):
                if not rule.get("enabled", True):
                    continue
                eligible = rule.get("trigger_on", [check["tf"] for check in rule["checks"]])
                if bar["interval"] not in eligible:
                    continue
                active, descriptions = evaluate_confluence(
                    rule, bar["symbol"], view, tf_findings, self.config["pulse"],
                    now, bar["end_ms"] + 1, self.market.get("stale_grace_minutes", 10))
                state = self.store.db.execute("""SELECT active FROM confluence_states
                        WHERE symbol=? AND rule_id=?""", (bar["symbol"], rule["id"])).fetchone()
                was_active = bool(state["active"]) if state else False
                if active and (not rule.get("edge_only", True) or not was_active):
                    findings.append({"code": f"confluence:{rule['id']}",
                                     "name": f"Ваш набор условий {rule['id']}",
                                     "evidence": "; ".join(descriptions)[:320],
                                     "source": "конфигурация"})
                confluence_updates.append((bar["symbol"], rule["id"], int(active),
                                           bar["end_ms"] + 1))
            def save_confluence_states() -> None:
                for state_row in confluence_updates:
                    self.store.db.execute("""INSERT INTO confluence_states VALUES (?,?,?,?)
                      ON CONFLICT(symbol,rule_id) DO UPDATE SET
                        active=excluded.active,updated_ms=excluded.updated_ms""", state_row)
            if not findings and not rule_hits:
                save_confluence_states()
                self.store.db.execute("INSERT OR IGNORE INTO analyzed_bars VALUES (?,?,?,?)",
                                      (*identity, now.isoformat()))
                self.store.db.commit()
                return
            key = f"review:{bar['symbol']}:{bar['interval']}:{bar['start_ms']}"
            quality = notification_quality(bar["symbol"], findings, rule_hits,
                                           self.market.get("quality", {}))
            gate = None
            sweep = None
            profiles = None
            gate_cfg = self.market.get("quality", {}).get("mtf_gate", {})
            if quality["send"] and gate_cfg.get("enabled", False):
                gate = review_gate(view, bar["symbol"], bar["end_ms"],
                                   require_zone=gate_cfg.get("require_4h_zone", True))
                if not gate["send"]:
                    quality = {**quality, "send": False,
                               "reason": "mtf_" + gate["reason"]}
            min_quote = float(self.market.get("quality", {}).get("min_quote_turnover_24h", 0))
            if quality["send"] and min_quote > 0:
                category = self.market.get("symbol_categories", {}).get(bar["symbol"], "linear")
                quote = quote_volume_24h(
                    view.bars.get((bar["symbol"], "15"), []),
                    category=category, symbol=bar["symbol"])
                if quote is None:
                    quality = {**quality, "send": False, "reason": "liquidity_unavailable"}
                elif quote < min_quote:
                    quality = {**quality, "send": False, "reason": "liquidity_below_threshold"}
            if quality["send"] and gate and gate_cfg.get("require_sweep", False):
                side = "BUY" if gate["direction"] == "рост" else "SELL"
                sweep = recent_sweep(view, bar["symbol"], bar["end_ms"], side)
                if not sweep:
                    overview_floor = int(gate_cfg.get("overview_min_aligned_tfs", 0))
                    if overview_floor and gate["count"] >= overview_floor:
                        # Earlier user preference keeps exceptionally strong
                        # chart overviews visible. They remain explicitly
                        # non-entry alerts when no sweep is confirmed.
                        quality = {**quality, "reason": "strong_overview_no_sweep"}
                    else:
                        quality = {**quality, "send": False,
                                   "reason": "setup_no_confirmed_sweep"}
            if quality["send"] and gate and gate_cfg.get("require_profile", False):
                atr_4h = timeframe_metrics(
                    view.bars.get((bar["symbol"], "240"), []), "240")["atr"]
                profiles = profile_windows(view, bar["symbol"], bar["end_ms"], atr_4h,
                                           category=self.market.get("symbol_categories", {}).get(
                                               bar["symbol"], "linear"))
                range_profile = profiles["range"]
                if range_profile is None:
                    quality = {**quality, "send": False, "reason": "setup_profile_unavailable"}
                elif ((gate["direction"] == "рост" and bar["close"] > range_profile["vah"]) or
                      (gate["direction"] == "снижение" and bar["close"] < range_profile["val"])):
                    quality = {**quality, "send": False, "reason": "setup_profile_opposes"}
            macro_cfg = self.market.get("quality", {}).get("btc_funding_guard", {})
            funding = funding_asof(self.btc_funding_history, bar["end_ms"],
                                   macro_cfg.get("max_age_minutes", 5))
            if quality["send"] and gate and macro_cfg.get("enabled", False) and funding is not None:
                if ((gate["direction"] == "рост" and
                     funding > float(macro_cfg.get("max_long_rate", 0.0005))) or
                    (gate["direction"] == "снижение" and
                     funding < float(macro_cfg.get("min_short_rate", -0.00005)))):
                    quality = {**quality, "send": False, "reason": "macro_btc_funding_guard"}
            sweep_cfg = self.market.get("quality", {}).get("strong_sweep_review", {})
            if (not quality["send"] and bar["interval"] == "15" and
                    sweep_cfg.get("enabled", False)):
                category = self.market.get("symbol_categories", {}).get(bar["symbol"], "linear")
                candidate = strong_sweep_review(view, bar["symbol"], bar["end_ms"],
                                                findings, category=category,
                                                config=sweep_cfg)
                if candidate["send"]:
                    gate, sweep = candidate, candidate["sweep"]
                    quality = {"send": True, "families": candidate["families"],
                               "reason": "strong_sweep_review"}
            cooldown_hours = float(self.market.get("quality", {}).get("cooldown_hours", 0))
            if (quality["send"] and quality["reason"] != "user_rule" and
                    review_cooldown_conflict(self.store.db, bar["symbol"], now,
                                             cooldown_hours)):
                quality = {**quality, "send": False, "reason": "cooldown"}
            scenario = scenario_context(bar, findings, view, sweep, gate)
            # The same BOS/4h/zone rule covers generic quality and the
            # strong_sweep_review fallback because both converge here.
            quality = guarded_quality(quality, scenario)
            quality = apply_direction_gate(quality, scenario)
            if quality["send"]:
                timeframe_ok, suppression_reason = timeframe_alert_policy(
                    bar["interval"], int(bar["end_ms"]) + 1)
                if not timeframe_ok:
                    quality = {**quality, "send": False, "reason": suppression_reason}
            quality_label = ""
            if quality["send"] and self.market.get("quality", {}).get("enabled"):
                quality_label = ("Отбор: правило пользователя" if quality["reason"] == "user_rule"
                                 else "Отбор: " + ", ".join(sorted(quality["families"])))
            if gate and gate["send"]:
                side = scenario_label(scenario)
                quality_label += (f"\nКонтекст отбора: {side}; {gate['count']}/5 ТФ совпали; "
                                  f"4ч зона {gate['zone']}. Это сигнал на проверку, не точка входа.")
            if sweep and quality["send"]:
                quality_label += (f"\nСобытие: {sweep['code']} {sweep['direction']} "
                                  f"на уровне {sweep['level']:g} [закрытая 15м свеча].")
            if gate and quality["send"]:
                quality_label += (f"\nBTC funding (тикер Bybit): {funding:+.6f}"
                                  if funding is not None else "\nBTC funding: нет данных на момент свечи")
            btc_view = self.candles.snapshot_at("BTCUSDT", self.market["intervals"],
                                                bar["end_ms"])
            btc_lines = alt_btc_context(bar["symbol"], bar["end_ms"], view,
                                        btc_view, self.config.get("btc_context", {}))
            spec_lines = specification_progress(
                bar["symbol"], self.market.get("symbol_categories", {}).get(bar["symbol"], "linear"),
                view, btc_view, bar["end_ms"], micro)
            if quality["send"]:
                if profiles is None:
                    atr_4h = timeframe_metrics(
                        view.bars.get((bar["symbol"], "240"), []), "240")["atr"]
                    profiles = profile_windows(view, bar["symbol"], bar["end_ms"], atr_4h,
                                               category=self.market.get("symbol_categories", {}).get(
                                                   bar["symbol"], "linear"))
                for name, profile in profiles.items():
                    if profile:
                        spec_lines.append(
                            f"VP {name}: POC {profile['poc']:g}, "
                            f"VAL {profile['val']:g}, VAH {profile['vah']:g}; "
                            "оценка по OHLCV, объём внутри свечи распределён равномерно")
                zones = active_fvgs(
                    view.bars.get((bar["symbol"], "240"), []), "240",
                    lookback=100,
                    min_gap_bps=float(findings_cfg.get("fvg_min_gap_bps", 3)))
                if zones:
                    nearest = min(zones, key=lambda zone: abs(zone["mid"] - bar["close"]))
                    spec_lines.append(
                        f"4ч FVG (последние 100 свечей): активных {len(zones)}; "
                        f"ближайший {nearest['direction']} {nearest['bottom']:g}–"
                        f"{nearest['top']:g}, статус {nearest['status']}, "
                        f"касаний {nearest['tests']} [Bybit OHLC]")
                else:
                    spec_lines.append("4ч FVG (последние 100 свечей): активных нет [Bybit OHLC]")
            close_ms = bar["end_ms"] + 1
            session_lines = [f"Сессия МСК: {session_label(session_at(close_ms))}"]
            asia = closed_asia_range(view.bars.get((bar["symbol"], "60"), []),
                                     close_ms)
            if asia:
                session_lines.append(f"Asia Range {asia['day_utc']} UTC: "
                                     f"{asia['low']:g}–{asia['high']:g} "
                                     "(7 закрытых 1ч свечей)")
            if (quality["send"] and gate and gate.get("send") and
                    quality["reason"] != "user_rule" and
                    self.market.get("quality", {}).get("enabled", False)):
                text = format_compact_alert(
                    bar, findings, tf_findings, view, btc_lines, session_lines,
                    spec_lines, micro,
                    self.store, self.config["news"], now, gate, sweep, funding,
                    quality, self.market.get("symbol_categories", {}).get(bar["symbol"], "linear"), scenario)
            else:
                text = format_review_alert(bar, findings, rule_hits, trend, tf_findings,
                                           rule_snapshots, micro, view, btc_lines, spec_lines,
                                           session_lines, quality_label, self.store,
                                           self.config["calendar"], now, self.config["news"],
                                           self.market.get("stale_grace_minutes", 10), gate, sweep, scenario,
                                           self.market.get("symbol_categories", {}).get(bar["symbol"], "linear"))
            status = ("pending" if quality["send"] else
                      "suppressed_direction" if quality["reason"] == "suppressed_direction:conflict" else
                      "suppressed_provenance" if quality["reason"] == "provenance_unverified" else
                      "suppressed_cooldown" if quality["reason"] == "cooldown" else
                      "suppressed_mtf" if quality["reason"].startswith("mtf_") else
                      "suppressed_liquidity" if quality["reason"].startswith("liquidity_") else
                      "suppressed_setup" if quality["reason"].startswith("setup_") else
                      "suppressed_macro" if quality["reason"].startswith("macro_") else
                      "suppressed")
            inserted = self.store.db.execute("""
              INSERT OR IGNORE INTO signal_alerts(id,status,text,created_utc,reason)
              VALUES (?,?,?,?,?)
            """, (key, status, text, now.isoformat(), quality["reason"]))
            if inserted.rowcount:
                save_scenario(self.store.db, key, scenario)
            if quality["send"]:
                # Preserve the pre-existing research cohort/method. The new
                # presentation contract is stored separately, never backfilled.
                side = scenario.get("side")
                record_candidate(self.store.db, key, bar, side)
            save_confluence_states()
            self.store.db.execute("INSERT OR IGNORE INTO analyzed_bars VALUES (?,?,?,?)",
                                  (*identity, now.isoformat()))
            self.store.db.commit()

    async def flush(self, session: aiohttp.ClientSession, dry_run: bool) -> None:
        # Sending can wait on Telegram rate limits; keep bar analysis moving.
        # A separate lock still guarantees one sender per process.
        async with self.send_lock:
            await self._flush_locked(session, dry_run)

    async def _flush_locked(self, session: aiohttp.ClientSession, dry_run: bool) -> None:
        rows = self.store.db.execute("""
          SELECT id,text,created_utc FROM signal_alerts WHERE status='pending'
          ORDER BY created_utc LIMIT 20
        """).fetchall()
        tg = self.config["telegram"]
        if not dry_run and not tg.get("enabled"):
            return
        for row in rows:
            contract_row = self.store.db.execute(
                "SELECT contract_json FROM signal_scenarios WHERE alert_id=?", (row["id"],)).fetchone()
            try:
                contract = json.loads(contract_row[0]) if contract_row else None
            except (ValueError, TypeError):
                contract = None
            if not verified_zone(contract):
                self.store.db.execute("UPDATE signal_alerts SET status='suppressed_provenance',reason='provenance_unverified' WHERE id=?", (row["id"],))
                self.store.db.commit()
                continue
            max_lag = float(self.market.get("max_delivery_lag_minutes", 15))
            if review_is_stale_for_delivery(row["id"], utc_now(), max_lag):
                self.store.db.execute("""UPDATE signal_alerts
                    SET status='suppressed_stale',reason=? WHERE id=?""",
                    (f"delivery_delay_over_{max_lag:g}m", row["id"]))
                self.store.db.commit()
                continue
            created = parse_time(row["created_utc"])
            if created and (utc_now() - created).total_seconds() > self.market.get("max_alert_age_minutes", 90) * 60:
                self.store.db.execute("UPDATE signal_alerts SET status='expired' WHERE id=?", (row["id"],))
                self.store.db.commit()
                continue
            if dry_run:
                if row["id"] in self.previewed:
                    continue
                print(row["text"])
                self.previewed.add(row["id"])
                continue
            else:
                token = os.environ.get(tg["token_env"])
                chat = os.environ.get(tg["chat_id_env"])
                if not token or not chat:
                    LOG.error("Telegram enabled but token/chat ID missing")
                    return
                try:
                    await send_telegram(session, token, chat, row["text"])
                except Exception as error:
                    LOG.error("Signal delivery failed; retained for retry (%s)",
                              type(error).__name__)
                    return
                self.store.db.execute("UPDATE signal_alerts SET status='sent',sent_utc=? WHERE id=?",
                                      (utc_now().isoformat(), row["id"]))
            self.store.db.commit()


async def discover_perpetuals(session: aiohttp.ClientSession, market: dict) -> dict[str, str]:
    """Read every instruments-info page; exclude futures and premarket contracts."""
    host = "https://api-testnet.bybit.com" if market.get("testnet") else "https://api.bybit.com"
    found: dict[str, str] = {}
    for category, contract_type in (("linear", "LinearPerpetual"),
                                    ("inverse", "InversePerpetual")):
        cursor = ""
        seen_cursors: set[str] = set()
        while True:
            params = {"category": category, "status": "Trading", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            for attempt in range(4):
                try:
                    async with session.get(f"{host}/v5/market/instruments-info",
                                           params=params,
                                           timeout=aiohttp.ClientTimeout(total=25)) as response:
                        data = await response.json(content_type=None)
                        if response.status == 429 or data.get("retCode") == 10006:
                            raise RuntimeError("Bybit instrument rate limit")
                        response.raise_for_status()
                        if data.get("retCode") != 0:
                            raise RuntimeError(f"Bybit instruments: {data.get('retCode')}")
                    break
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError,
                        RuntimeError) as error:
                    if attempt == 3:
                        raise RuntimeError(f"Cannot discover {category} perpetuals") from error
                    await asyncio.sleep(2 ** attempt)
            result = data["result"]
            for item in result["list"]:
                if item.get("status") != "Trading" or item.get("contractType") != contract_type:
                    continue
                symbol = item["symbol"]
                if symbol in found and found[symbol] != category:
                    raise RuntimeError(f"Ambiguous Bybit symbol across categories: {symbol}")
                found[symbol] = category
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
            if cursor in seen_cursors:
                raise RuntimeError(f"Repeated instruments cursor for {category}")
            seen_cursors.add(cursor)
    if not found:
        raise RuntimeError("Bybit returned no trading perpetuals")
    return found


def topics_by_category(market: dict, micro_symbols: set[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {"linear": [], "inverse": []}
    for symbol in market["symbols"]:
        category = market["symbol_categories"].get(symbol)
        if category not in result:
            raise ValueError(f"Missing Bybit category for {symbol}")
        result[category].extend(f"kline.{interval}.{symbol}" for interval in market["intervals"])
        if symbol in micro_symbols:
            result[category].extend((f"publicTrade.{symbol}", f"orderbook.50.{symbol}"))
    if "BTCUSDT" in market["symbols"]:
        result["linear"].append("tickers.BTCUSDT")
    return result


def missing_higher_tf_topics(db: sqlite3.Connection, symbols: set[str],
                             now_ms: int, max_age_ms: int,
                             settle_ms: int = 4 * 60_000) -> list[str]:
    """Find missed daily/weekly closes while a timely gap repair is still possible.

    Normal candles arrive on WebSocket. REST is requested only if the expected
    close has not been analysed after the short settling period. Bybit weekly
    candles begin on Monday UTC; Unix epoch weeks begin on Thursday.
    """
    if not symbols:
        return []
    topics = []
    for interval, offset in (("D", 0), ("W", 4 * INTERVALS["D"])):
        duration = INTERVALS[interval]
        closed_ms = ((now_ms - offset) // duration) * duration + offset
        age_ms = now_ms - closed_ms
        if not settle_ms <= age_ms <= max_age_ms:
            continue
        start_ms = closed_ms - duration
        done = {row[0] for row in db.execute(
            "SELECT symbol FROM analyzed_bars WHERE interval=? AND start_ms=?",
            (interval, start_ms))}
        topics.extend(f"kline.{interval}.{symbol}" for symbol in sorted(symbols - done))
    return topics


async def backfill(session: aiohttp.ClientSession, engine: Engine,
                   topics: list[str] | None = None, *, analyze_missed: bool = False) -> None:
    """Startup/reconnect gap repair only; no periodic REST market polling."""
    async with engine.backfill_lock:
        if (not analyze_missed and topics is None and
                engine.market.get("universe", {}).get("enabled")):
            symbols = list(engine.market["symbols"])
            lanes = max(1, min(12, int(engine.market["universe"].get("history_workers", 8))))
            await asyncio.gather(*(_backfill_locked(session, engine, None, False,
                                                     symbols[i::lanes])
                                   for i in range(lanes)))
        else:
            await _backfill_locked(session, engine, topics, analyze_missed)


def backfill_start_ms(bars: list[dict], interval: str) -> int | None:
    """Request from the first internal gap, not merely the newest bar.

    A newer WebSocket close can arrive before reconnect repair. Starting REST
    at that newest close would permanently hide the missing older candle.
    """
    if not bars:
        return None
    span = INTERVALS[interval]
    for older, newer in zip(bars, bars[1:]):
        if int(newer["start_ms"]) - int(older["start_ms"]) > span:
            return int(older["start_ms"]) + span
    return int(bars[-1]["start_ms"])


async def _backfill_locked(session: aiohttp.ClientSession, engine: Engine,
                           topics: list[str] | None, analyze_missed: bool,
                           symbols: list[str] | None = None) -> None:
    cfg = engine.market
    host = "https://api-testnet.bybit.com" if cfg.get("testnet") else "https://api.bybit.com"
    pairs = {(bits[2], bits[1]) for topic in topics
             if (bits := topic.split("."))[0] == "kline" and len(bits) == 3} if topics else None
    fetched: list[dict] = []
    failed_pairs: list[str] = []
    loaded_count = 0
    for symbol in (symbols if symbols is not None else list(cfg["symbols"])):
        if not analyze_missed and symbol in engine.ready_symbols:
            # A restart within the current candle needs no REST warmup. If
            # even one interval may have closed since our last stored bar,
            # fetch the gap before relying on the cached context.
            now_ms = int(time.time() * 1000)
            if all((last := engine.candles.latest(symbol, tf)) is not None and
                   0 <= now_ms - (last["end_ms"] + 1) < INTERVALS[tf]
                   and backfill_start_ms(list(engine.candles.bars[symbol, tf]), tf)
                   == last["start_ms"]
                   for tf in cfg["intervals"]):
                continue
        symbol_ok = True
        symbol_fetched: list[dict] = []
        for interval in cfg["intervals"]:
            if pairs is not None and (symbol, interval) not in pairs:
                continue
            params = {"category": cfg.get("symbol_categories", {}).get(symbol, "linear"),
                      "symbol": symbol, "interval": interval,
                      "limit": min(1000, max(2, int(cfg.get("history_bars", 250)) + 2))}
            latest = engine.candles.latest(symbol, interval)
            if latest is not None and symbol in engine.ready_symbols:
                params["start"] = backfill_start_ms(
                    list(engine.candles.bars[symbol, interval]), interval)
            succeeded = False
            for attempt in range(4):
                try:
                    async with engine.rest_gate:
                        pause = engine.next_rest_at - time.monotonic()
                        if pause > 0:
                            await asyncio.sleep(pause)
                        engine.next_rest_at = time.monotonic() + 0.08
                    async with session.get(f"{host}/v5/market/kline", params=params,
                                           timeout=aiohttp.ClientTimeout(total=12)) as response:
                        data = await response.json(content_type=None)
                        if response.status == 429 or data.get("retCode") == 10006:
                            await asyncio.sleep(min(30, 2 ** attempt))
                            continue
                        response.raise_for_status()
                        if data.get("retCode") != 0:
                            raise RuntimeError(f"Bybit {data.get('retCode')}: {data.get('retMsg')}")
                        rows = data["result"]["list"]
                    now_ms = int(time.time() * 1000)
                    for raw in reversed(rows):
                        bar = bar_from_rest(symbol, interval, raw)
                        if bar["end_ms"] < now_ms:
                            (fetched if analyze_missed else symbol_fetched).append(bar)
                    succeeded = True
                    break
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError,
                        KeyError, RuntimeError) as error:
                    LOG.warning("Backfill %s %s attempt %s: %s", symbol, interval, attempt + 1, error)
                    await asyncio.sleep(min(30, 2 ** attempt))
            if not succeeded:
                failed_pairs.append(f"{symbol}/{interval}")
                symbol_ok = False
        if not analyze_missed:
            now_ms = int(time.time() * 1000)
            async with engine.lock:
                for bar in symbol_fetched:
                    engine.candles.put(bar, commit=False)
                    if now_ms - (bar["end_ms"] + 1) <= cfg.get("max_alert_age_minutes", 90) * 60_000:
                        engine.store.db.execute("INSERT OR IGNORE INTO analyzed_bars VALUES (?,?,?,?)",
                                                (bar["symbol"], bar["interval"], bar["start_ms"],
                                                 utc_now().isoformat()))
                engine.store.db.commit()
                if symbol_ok and pairs is None:
                    engine.ready_symbols.add(symbol)
            loaded_count += len(symbol_fetched)
            if len(engine.ready_symbols) % 100 == 0 and engine.ready_symbols:
                LOG.info("Bybit history ready: %d/%d symbols", len(engine.ready_symbols),
                         len(cfg["symbols"]))
    if failed_pairs:
        engine.health_notice("Bybit REST backfill",
                             "Не удалось восстановить историю: " + ", ".join(failed_pairs[:12]))
    # Sorting all topics together prevents a 1h missed alert from looking
    # ahead to a 15m or 4h bar that closed later during the outage.
    if not analyze_missed:
        LOG.info("Bybit history ready: %d/%d symbols; %d closed bars loaded; %d failed pairs",
                 len(engine.ready_symbols), len(cfg["symbols"]), loaded_count, len(failed_pairs))
        return
    for bar in sorted(fetched, key=lambda b: (b["end_ms"], INTERVALS[b["interval"]])):
        existing = engine.candles.latest(bar["symbol"], bar["interval"])
        is_new = existing is None or bar["start_ms"] > existing["start_ms"]
        identity = (bar["symbol"], bar["interval"], bar["start_ms"])
        reviewed = engine.store.db.execute("""SELECT 1 FROM analyzed_bars
                      WHERE symbol=? AND interval=? AND start_ms=?""", identity).fetchone()
        if analyze_missed and not reviewed and (is_new or
                (existing is not None and bar["start_ms"] == existing["start_ms"])):
            await engine.on_bar(bar)
        else:
            engine.candles.put(bar)


async def wait_for_closed_slices(engine: Engine, bar: dict, timeout_seconds: float = 10) -> bool:
    """Wait briefly for all shorter intervals closing at the same instant."""
    shorter = [tf for tf in engine.market["intervals"]
               if INTERVALS[tf] < INTERVALS[bar["interval"]]]
    deadline = time.monotonic() + timeout_seconds
    needs_btc = bar["symbol"] not in BTC_SYMBOLS and "BTCUSDT" in engine.market["symbols"]
    while shorter or needs_btc:
        own_ready = all((latest := engine.candles.latest(bar["symbol"], tf)) is not None
                        and latest["end_ms"] >= bar["end_ms"] for tf in shorter)
        btc_ready = not needs_btc or all(
            (latest := engine.candles.latest("BTCUSDT", tf)) is not None
            and latest["end_ms"] >= bar["end_ms"] for tf in {bar["interval"], "15"})
        if own_ready and btc_ready:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.1)
    return True


async def socket_worker(session: aiohttp.ClientSession, engine: Engine,
                        topics: list[str], dry_run: bool,
                        category: str = "linear") -> None:
    host = "stream-testnet.bybit.com" if engine.market.get("testnet") else "stream.bybit.com"
    url = f"wss://{host}/v5/public/{category}"
    delay = 1.0
    failures = 0
    connected_since: float | None = None
    pending_tasks: set[asyncio.Task] = set()
    scheduled: set[tuple[str, str, int]] = set()
    trade_symbols = {topic.split(".")[1] for topic in topics if topic.startswith("publicTrade.")}
    book_symbols = {topic.split(".")[2] for topic in topics if topic.startswith("orderbook.50.")}

    async def analyze_after_delay(bar: dict) -> None:
        await asyncio.sleep(max(0, float(engine.market.get("analysis_delay_seconds", 3))))
        # Several intervals can close at the same instant. Let shorter bars
        # enter the shared snapshot first, so a 1h/4h/W alert does not show
        # the previous 15m bar merely because WS messages arrived out of order.
        await wait_for_closed_slices(engine, bar)
        for attempt in range(3):
            try:
                await engine.on_bar(bar)
                await engine.flush(session, dry_run)
                return
            except Exception:
                # Do not carry a failed write transaction into the retry: it
                # would block the independent news connection and WAL cleanup.
                engine.store.db.rollback()
                LOG.exception("Bar analysis %s %s %s failed (attempt %d)",
                              bar["symbol"], bar["interval"], bar["start_ms"], attempt + 1)
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"Bar analysis exhausted retries: {bar['symbol']} "
                           f"{bar['interval']} {bar['start_ms']}")

    while True:
        try:
            async with session.ws_connect(url, heartbeat=None, timeout=15) as ws:
                connected_since = time.monotonic()
                subscribe_batch = 100  # Futures allow this; keep far below 21k chars/connection.
                expected_acks = (len(topics) + subscribe_batch - 1) // subscribe_batch
                received_acks = 0
                for i in range(0, len(topics), subscribe_batch):
                    await ws.send_json({"op": "subscribe", "args": topics[i:i + subscribe_batch]})
                    await asyncio.sleep(0.2)
                started_ms = int(time.time() * 1000)
                for symbol in trade_symbols:
                    engine.flow.connected(symbol, started_ms)
                for symbol in book_symbols:
                    engine.book.disconnected(symbol)
                delay = 1.0
                last_ping = time.monotonic()
                last_activity = last_ping
                while True:
                    try:
                        message = await asyncio.wait_for(ws.receive(), timeout=5)
                    except asyncio.TimeoutError:
                        message = None
                    if time.monotonic() - last_ping >= 20:
                        await ws.send_json({"op": "ping"})
                        last_ping = time.monotonic()
                    if message is None:
                        if received_acks < expected_acks and time.monotonic() - connected_since > 60:
                            raise ConnectionError("Bybit subscription acknowledgements incomplete")
                        if time.monotonic() - last_activity > 75:
                            raise ConnectionError("No WS data or pong for 75 seconds")
                        await engine.flush(session, dry_run)
                        continue
                    if message.type != aiohttp.WSMsgType.TEXT:
                        raise ConnectionError(f"WebSocket closed: type={message.type}, "
                                              f"code={ws.close_code}, data={str(message.data)[:120]}")
                    last_activity = time.monotonic()
                    payload = json.loads(message.data)
                    if payload.get("op") == "subscribe":
                        rejected = payload.get("data", {}).get("failTopics", []) if isinstance(
                            payload.get("data"), dict) else []
                        if not payload.get("success", True) or rejected:
                            raise RuntimeError(f"Subscription rejected: {payload.get('ret_msg')} {rejected[:3]}")
                        received_acks += 1
                        if received_acks == expected_acks:
                            LOG.info("Bybit %s shard acknowledged %d topics", category, len(topics))
                    topic = payload.get("topic", "")
                    bits = topic.split(".")
                    if topic == "tickers.BTCUSDT":
                        data = payload.get("data") or {}
                        if isinstance(data, list):
                            data = data[0] if data else {}
                        if data.get("fundingRate") not in (None, ""):
                            rate = float(data["fundingRate"])
                            if math.isfinite(rate):
                                if engine.btc_funding_rate is None:
                                    LOG.info("BTC ticker funding stream ready: rate=%+.6f", rate)
                                engine.btc_funding_rate = rate
                        if engine.btc_funding_rate is not None:
                            ts = int(payload.get("ts") or 0)
                            history = engine.btc_funding_history
                            if ts > 0 and (not history or ts - history[-1][0] >= 1000):
                                history.append((ts, engine.btc_funding_rate))
                        continue
                    if len(bits) == 2 and bits[0] == "publicTrade":
                        engine.flow.add_batch(payload.get("data", []))
                        continue
                    if len(bits) == 3 and bits[0] == "orderbook" and bits[1] == "50":
                        engine.book.apply(payload)
                        continue
                    if len(bits) != 3 or bits[0] != "kline":
                        continue
                    _, interval, symbol = bits
                    for item in payload.get("data", []):
                        if item.get("confirm") is True:
                            bar = bar_from_ws(symbol, interval, item)
                            key = (symbol, interval, bar["start_ms"])
                            if key in scheduled:
                                continue
                            scheduled.add(key)
                            task = asyncio.create_task(analyze_after_delay(bar))
                            pending_tasks.add(task)
                            def completed(done: asyncio.Task, bar_key=key) -> None:
                                pending_tasks.discard(done)
                                scheduled.discard(bar_key)
                                if not done.cancelled() and done.exception():
                                    LOG.error("Bar analysis stopped: %s", done.exception())
                            task.add_done_callback(completed)
                    # Confirmed bars flush in analyze_after_delay. A SELECT on
                    # every unconfirmed update would overload SQLite at scale.
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError,
                RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            LOG.warning("Bybit socket reconnect: %s", error)
            failures = (1 if connected_since is not None and
                        time.monotonic() - connected_since >= 120 else failures + 1)
            if failures >= 3:
                engine.health_notice("Bybit WebSocket",
                                     f"Не менее {failures} последовательных сбоев; "
                                     "свечи восстанавливаются через REST после переподключения.")
                await engine.flush(session, dry_run)
        finally:
            connected_since = None
            for symbol in trade_symbols:
                engine.flow.disconnected(symbol)
            for symbol in book_symbols:
                engine.book.disconnected(symbol)
        await asyncio.sleep(delay + random.random())
        delay = min(60, delay * 2)
        await backfill(session, engine, topics, analyze_missed=True)


async def run(config_path: Path, dry_run: bool) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    market = config["market"]
    if not market.get("enabled"):
        raise ValueError("Set market.enabled=true after configuring symbols and rules")
    if len(market["intervals"]) != len(TF_ORDER) or set(market["intervals"]) != set(TF_ORDER):
        raise ValueError("All five intervals W, D, 240, 60, 15 are required")
    for rule in market["rules"]:
        if rule["operator"] not in OPS or rule["metric"] not in {
            "close", "pct_change", "close_sma_ratio", "pulse_volume_ratio"
        }:
            raise ValueError(f"Unsupported rule: {rule}")
    seen_rule_ids: set[str] = set()
    for rule in market.get("confluence_rules", []):
        rule_id = rule.get("id")
        if (not isinstance(rule_id, str) or not rule_id or rule_id in seen_rule_ids
                or rule.get("mode", "all") not in {"all", "any"}
                or not isinstance(rule.get("checks"), list) or not rule["checks"]):
            raise ValueError(f"Invalid confluence rule: {rule}")
        seen_rule_ids.add(rule_id)
        for check in rule["checks"]:
            if check.get("tf") not in INTERVALS:
                raise ValueError(f"Unsupported confluence timeframe: {check}")
            if "finding" in check:
                if not isinstance(check["finding"], str) or not check["finding"]:
                    raise ValueError(f"Invalid finding check: {check}")
            elif check.get("metric") == "ema_direction":
                if check.get("value") not in {"рост", "снижение", "смешанная"}:
                    raise ValueError(f"Invalid EMA direction check: {check}")
            elif (check.get("metric") not in {"close", "pct_change", "close_sma_ratio",
                                               "pulse_volume_ratio", "rsi14", "macd_hist"}
                  or check.get("operator") not in OPS):
                raise ValueError(f"Unsupported numeric check: {check}")
            else:
                try:
                    if not math.isfinite(float(check["value"])):
                        raise ValueError()
                except (ValueError, TypeError, KeyError):
                    raise ValueError(f"Invalid numeric threshold: {check}") from None
    async with aiohttp.ClientSession() as session:
        universe = market.get("universe", {})
        configured = list(market["symbols"])
        if universe.get("enabled"):
            try:
                discovered = await discover_perpetuals(session, market)
                market["symbols"] = [s for s in configured if s in discovered] + sorted(
                    set(discovered) - set(configured))
                market["symbol_categories"] = discovered
                LOG.info("Discovered %d Bybit trading perpetuals (%d linear, %d inverse)",
                         len(discovered), sum(c == "linear" for c in discovered.values()),
                         sum(c == "inverse" for c in discovered.values()))
            except Exception:
                LOG.exception("Bybit discovery failed; using configured seed symbols")
                market["symbol_categories"] = {s: "linear" for s in configured}
        else:
            market["symbol_categories"] = {s: "linear" for s in configured}
        engine = Engine(config)
        size = max(4, min(500, int(market.get("topics_per_connection", 100))))

        def start_workers() -> list[asyncio.Task]:
            workers = []
            for category, topics in topics_by_category(market, engine.micro_symbols).items():
                for i in range(0, len(topics), size):
                    workers.append(asyncio.create_task(socket_worker(
                        session, engine, topics[i:i + size], dry_run, category)))
            LOG.info("Started %d Bybit WebSocket shards for %d symbols", len(workers),
                     len(market["symbols"]))
            return workers

        workers = start_workers()
        hydration = asyncio.create_task(backfill(session, engine))
        higher_tf_repair: asyncio.Task | None = None
        next_higher_tf_check = time.monotonic() + 120
        repair_day = -1
        repairs_today = 0
        refresh_every = max(300, int(universe.get("refresh_seconds", 3600)))
        next_refresh = time.monotonic() + refresh_every
        next_hydration_retry = time.monotonic() + 300
        try:
            while True:
                await asyncio.sleep(30)
                async with engine.lock:
                    resolve_due(engine.store.db, int(time.time() * 1000))
                    resolve_followthrough(engine.store.db, int(time.time() * 1000))
                if any(worker.done() for worker in workers):
                    for worker in workers:
                        if worker.done() and not worker.cancelled():
                            LOG.error("Bybit WebSocket shard stopped: %s", worker.exception())
                    engine.health_notice("Bybit WebSocket shard",
                                         "Процесс подписки остановился; подписки перезапускаются.")
                    for worker in workers:
                        worker.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
                    workers = start_workers()
                if hydration.done() and time.monotonic() >= next_hydration_retry:
                    if hydration.exception():
                        LOG.error("Bybit history task failed: %s", hydration.exception())
                    if not set(market["symbols"]).issubset(engine.ready_symbols):
                        hydration = asyncio.create_task(backfill(session, engine))
                    next_hydration_retry = time.monotonic() + 300
                if time.monotonic() >= next_higher_tf_check and (
                        higher_tf_repair is None or higher_tf_repair.done()):
                    if higher_tf_repair is not None and higher_tf_repair.exception():
                        LOG.error("Higher timeframe gap repair failed: %s",
                                  higher_tf_repair.exception())
                    utc_day = int(time.time() * 1000) // INTERVALS["D"]
                    if utc_day != repair_day:
                        repair_day, repairs_today = utc_day, 0
                    missing = missing_higher_tf_topics(
                        engine.store.db, engine.ready_symbols,
                        int(time.time() * 1000),
                        int(market.get("max_alert_age_minutes", 90) * 60_000))
                    if missing and repairs_today < 3:
                        LOG.warning("Repairing %d missed 1D/1W candle closes", len(missing))
                        higher_tf_repair = asyncio.create_task(
                            backfill(session, engine, missing, analyze_missed=True))
                        repairs_today += 1
                    elif missing and repairs_today == 3:
                        engine.health_notice("Bybit 1D/1W gap repair",
                                             f"Не восстановлены {len(missing)} закрытых свечей; "
                                             "новые входы по ним не отправляются.")
                        repairs_today += 1
                    next_higher_tf_check = time.monotonic() + 300
                if time.monotonic() < next_refresh:
                    continue
                next_refresh = time.monotonic() + refresh_every
                if not universe.get("enabled"):
                    continue
                try:
                    discovered = await discover_perpetuals(session, market)
                except Exception:
                    LOG.exception("Bybit universe refresh failed; keeping current subscriptions")
                    engine.health_notice("Bybit universe", "Не удалось обновить список перпов; старые подписки работают.")
                    continue
                if discovered != market["symbol_categories"]:
                    previous = set(market["symbols"])
                    market["symbols"] = [s for s in configured if s in discovered] + sorted(
                        set(discovered) - set(configured))
                    market["symbol_categories"] = discovered
                    added = set(discovered) - previous
                    if added:
                        engine.candles.load(sorted(s for s in added
                                                   if not any(engine.candles.bars[s, tf]
                                                              for tf in market["intervals"])),
                                            market["intervals"])
                    for worker in workers:
                        worker.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
                    workers = start_workers()
                    LOG.info("Bybit universe changed: %d added, %d removed", len(added),
                             len(previous - set(discovered)))
                if hydration.done() and not set(market["symbols"]).issubset(engine.ready_symbols):
                    hydration = asyncio.create_task(backfill(session, engine))
        finally:
            hydration.cancel()
            tasks = [hydration, *workers]
            if higher_tf_repair is not None:
                higher_tf_repair.cancel()
                tasks.append(higher_tf_repair)
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run(args.config, args.dry_run))


if __name__ == "__main__":
    main()

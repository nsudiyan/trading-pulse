#!/usr/bin/env python3
"""resolve_channel_outcomes.py — отработка сигналов ТГК-каналов по свечам Bybit.

Для каждой строки news_signals.csv (channel, ts_utc, symbol, sentiment) считает
по 15м свечам Bybit окно 24ч после сигнала:
  entry     — close первого ПОЛНОГО 15м бара после сигнала (без look-ahead:
              бар, в котором пришёл сигнал, пропускаем — как в storm_report)
  peak_pct  — максимальный ход В СТОРОНУ сигнала (bullish→вверх по high,
              bearish→вниз по low), в процентах от entry; ≥0
  dd_pct    — максимальный ход ПРОТИВ сигнала (просадка), в процентах от entry;
              ≤0 (чем меньше, тем больнее; 0 = против сигнала не ходило)
  ret_4h    — знаковый % через 4ч В СТОРОНУ сигнала (плюс = сигнал прав)
  ret_24h   — то же через 24ч (или последний доступный бар, если окно не полно)

Кэш: channel_outcomes.json {key: outcome}; key = "channel:msg_id".
final=true когда окно 24ч закрыто — такие не пересчитываются. Инкрементально.
Символы без данных на Bybit → status="no_data" (тоже кэшируются, финально).

Запуск: python3 resolve_channel_outcomes.py [--limit N]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DIR = Path(__file__).resolve().parent
TRADING = DIR.parent
NEWS_SIGNALS = TRADING / "pump_analysis" / "catalyst_data" / "news_signals.csv"
CACHE_PATH = DIR / "channel_outcomes.json"

BYBIT = "https://api.bybit.com/v5/market/kline"
BAR_MIN = 15
WINDOW_H = 24.0


def fetch_15m(symbol: str, start_ms: int, end_ms: int) -> list[tuple]:
    """15м свечи (start_ms, o, h, l, c) по возрастанию; одного запроса хватает
    на 24ч (96 баров < лимита 1000)."""
    url = (f"{BYBIT}?category=linear&symbol={symbol}&interval={BAR_MIN}"
           f"&start={start_ms}&end={end_ms}&limit=1000")
    with urllib.request.urlopen(url, timeout=15) as r:
        data = json.load(r)
    if data.get("retCode") != 0:
        return []
    rows = data.get("result", {}).get("list") or []
    rows.reverse()  # Bybit отдаёт новые первыми
    return [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]))
            for x in rows]


def parse_ts(ts_str: str) -> datetime:
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:  # naive в этом пайплайне = UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def resolve_one(symbol: str, sig_dt: datetime, sentiment: str) -> dict:
    sig_ms = int(sig_dt.timestamp() * 1000)
    bar_ms = BAR_MIN * 60_000
    # первый ПОЛНЫЙ бар: старт строго после минуты сигнала, выровнен по сетке
    first_bar_start = ((sig_ms // bar_ms) + 1) * bar_ms
    end_ms = sig_ms + int(WINDOW_H * 3600_000)
    now_ms = int(time.time() * 1000)
    window_complete = end_ms <= now_ms - bar_ms  # последний бар успел закрыться

    bars = fetch_15m(symbol, first_bar_start, min(end_ms, now_ms))
    bars = [b for b in bars if b[0] >= first_bar_start]
    # entry-бар должен закрыться; если ещё идёт — подождём следующего прогона
    bars = [b for b in bars if b[0] + bar_ms <= now_ms]
    if not bars:
        return {"status": "no_data", "final": window_complete}

    entry = bars[0][4]  # close первого полного бара ≈ вход через ~15 мин
    if entry <= 0:
        return {"status": "no_data", "final": window_complete}
    up = sentiment == "bullish"  # bearish → плюс = падение

    def fav(high, low):   # ход в сторону сигнала, %
        return (high - entry) / entry * 100 if up else (entry - low) / entry * 100

    def adv(high, low):   # ход против сигнала, %
        return (low - entry) / entry * 100 if up else (entry - high) / entry * 100

    # ПИК/ПРОСАДКА — строго ПОСЛЕ бара входа: его high/low могли случиться до
    # close (= момента входа), включать их = look-ahead.
    post = bars[1:]
    peak = max((fav(h, l) for _, _, h, l, _ in post), default=0.0)
    dd = min((adv(h, l) for _, _, h, l, _ in post), default=0.0)  # ≤0
    # знаковый ret в сторону сигнала на горизонте
    def ret_at(hours: float):
        target = sig_ms + int(hours * 3600_000)
        closed = [b for b in bars if b[0] + bar_ms <= target]
        if not closed:
            return None
        c = closed[-1][4]
        return (c - entry) / entry * 100 if up else (entry - c) / entry * 100

    ret4 = ret_at(4.0)
    ret24 = ret_at(24.0) if window_complete else None
    last_close = bars[-1][4]
    ret_last = ((last_close - entry) / entry * 100 if up
                else (entry - last_close) / entry * 100)
    return {
        "status": "ok",
        "final": window_complete,
        "entry": entry,
        "peak_pct": round(peak, 2),
        "dd_pct": round(dd, 2),
        "ret_4h": round(ret4, 2) if ret4 is not None else None,
        "ret_24h": round(ret24, 2) if ret24 is not None else round(ret_last, 2),
        "bars": len(bars),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="максимум резолвов за прогон")
    args = ap.parse_args()

    try:
        cache = json.loads(CACHE_PATH.read_text())
    except Exception:
        cache = {}

    rows = list(csv.DictReader(open(NEWS_SIGNALS, encoding="utf-8")))
    todo = []
    for r in rows:
        key = f"{r['channel']}:{r['raw_msg_id']}"
        cached = cache.get(key)
        if cached and cached.get("final"):
            continue
        todo.append((key, r))
    if args.limit:
        todo = todo[: args.limit]
    print(f"[resolve] всего {len(rows)}, к резолву {len(todo)}")

    done = 0
    for key, r in todo:
        # у neutral/mixed нет стороны — отработку «в сторону сигнала» не посчитать
        if r["sentiment"] not in ("bullish", "bearish"):
            cache[key] = {"status": "no_direction", "final": True,
                          "channel": r["channel"], "symbol": r["symbol"],
                          "sentiment": r["sentiment"]}
            continue
        try:
            sig_dt = parse_ts(r["ts_utc"])
        except Exception:
            cache[key] = {"status": "bad_ts", "final": True}
            continue
        out = resolve_one(r["symbol"], sig_dt, r["sentiment"])
        out.update({
            "channel": r["channel"],
            "symbol": r["symbol"],
            "sentiment": r["sentiment"],
            "ts_utc": sig_dt.isoformat(),
            "link": r.get("raw_link", ""),
        })
        cache[key] = out
        done += 1
        if done % 25 == 0:
            print(f"[resolve] {done}/{len(todo)}")
            CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False))
        time.sleep(0.12)

    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False))
    ok = sum(1 for v in cache.values() if v.get("status") == "ok")
    nd = sum(1 for v in cache.values() if v.get("status") == "no_data")
    print(f"[resolve] готово: ok={ok} no_data={nd} всего в кэше {len(cache)}")


if __name__ == "__main__":
    sys.exit(main())

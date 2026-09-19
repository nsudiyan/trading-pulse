#!/usr/bin/env python3
"""Independent Bybit-only Positioning V1 shadow collector.

It never calls Telegram, trades, or the Radar modules.  V1 records only the
point-in-time fields Bybit actually returns; it leaves the shortlist empty
until sequential evidence can support a future case hypothesis.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
OUT = BASE / "outcomes"
SNAPSHOTS_PATH = OUT / "positioning_snapshots.jsonl"
STATE_PATH = OUT / "positioning_shadow_state.json"
BYBIT_TICKERS = "https://api.bybit.com/v5/market/tickers?category=linear"
SCHEMA_VERSION = "positioning-shadow-v1"
PROVIDER = "bybit.v5.market.tickers"
UNIVERSE_CAP = 20
OUTCOME_HORIZONS_MINUTES = (5, 15, 30, 60)
ONE_HOUR_MINUTES = 60
ONE_DAY_MINUTES = 24 * 60
ONE_WEEK_MINUTES = 7 * ONE_DAY_MINUTES


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def fnum(value: Any) -> float | None:
    try:
        value = float(value)
        return value if value == value else None
    except (TypeError, ValueError):
        return None


def iso_from_epoch_ms(value: Any) -> str | None:
    try:
        value = int(value)
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z") if value > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def select_universe(tickers: list[dict], cap: int = UNIVERSE_CAP) -> list[dict]:
    """Top liquid USDT perps by the turnover field returned by Bybit."""
    rows = []
    for item in tickers:
        symbol = item.get("symbol")
        last = fnum(item.get("lastPrice"))
        oi_value = fnum(item.get("openInterestValue"))
        turnover = fnum(item.get("turnover24h"))
        if not isinstance(symbol, str) or not symbol.endswith("USDT") or not last or not oi_value or not turnover or turnover <= 0:
            continue
        rows.append({
            "symbol": symbol, "last_price": last,
            "open_interest": fnum(item.get("openInterest")),
            "open_interest_value": oi_value, "turnover_24h": turnover,
            "volume_24h": fnum(item.get("volume24h")),
            "funding_rate": fnum(item.get("fundingRate")),
            "next_funding_time_ms": item.get("nextFundingTime"),
        })
    return sorted(rows, key=lambda row: row["turnover_24h"], reverse=True)[:cap]


def normalize_snapshot(payload: dict, received_at: datetime | None = None) -> list[dict]:
    received_at = received_at or utc_now()
    result = payload.get("result") if isinstance(payload, dict) else None
    tickers = result.get("list") if isinstance(result, dict) else None
    if not isinstance(tickers, list):
        return []
    source_time = iso_from_epoch_ms(payload.get("time"))
    received = received_at.isoformat().replace("+00:00", "Z")
    rows = []
    for raw in select_universe(tickers):
        key = f"{raw['symbol']}|{source_time or 'missing'}|{received}"
        rows.append({
            "snapshot_id": hashlib.sha256(key.encode()).hexdigest()[:24],
            "schema_version": SCHEMA_VERSION, "provider": PROVIDER,
            "venue": "BYBIT", "category": "linear", "symbol": raw["symbol"],
            "source_timestamp_utc": source_time, "server_received_at_utc": received,
            "raw": raw, "data_quality": "verified" if source_time else "unavailable",
        })
    return rows


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def append_rows(rows: list[dict]) -> None:
    if not rows: return
    OUT.mkdir(parents=True, exist_ok=True)
    with SNAPSHOTS_PATH.open("a", encoding="utf-8") as handle:
        for row in rows: handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush(); os.fsync(handle.fileno())


def parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def row_time(row: dict) -> datetime | None:
    return parse_utc(row.get("source_timestamp_utc")) or parse_utc(row.get("server_received_at_utc"))


def read_history(path: Path = SNAPSHOTS_PATH) -> list[dict]:
    """Read only valid, append-only snapshots; malformed lines remain ignored."""
    try:
        source = path.open("r", encoding="utf-8")
    except OSError:
        return []
    rows = []
    with source:
        for line in source:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and isinstance(row.get("symbol"), str) and isinstance(row.get("raw"), dict) and row_time(row):
                rows.append(row)
    return rows


def percent_change(current: float | None, earlier: float | None) -> float | None:
    if current is None or earlier is None or earlier == 0:
        return None
    return (current / earlier - 1.0) * 100.0


def closest_history_row(rows: list[dict], at: datetime, minutes: int) -> dict | None:
    """Choose a genuine prior sample close to the requested window, never backfill."""
    target = at.timestamp() - minutes * 60
    tolerance = max(15 * 60, minutes * 60 // 4)
    candidates = []
    for row in rows:
        timestamp = row_time(row)
        if not timestamp or timestamp >= at:
            continue
        distance = abs(timestamp.timestamp() - target)
        if distance <= tolerance:
            candidates.append((distance, timestamp, row))
    return min(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def context_explanation(price_change: float | None, oi_change: float | None) -> tuple[str, str]:
    if price_change is None or oi_change is None:
        return (
            "Сопоставимого прошлого снимка пока нет: это текущие значения, без вывода о позиции участников.",
            "Дождись накопления окна и сверяй фьючерс со спотом, стаканом и лентой вручную.",
        )
    if price_change >= 0 and oi_change >= 0:
        return (
            "Цена и OI выросли в одном окне: в открытом интересе стало больше номинала одновременно с ростом цены.",
            "Проверь, поддерживает ли движение спот и удерживаются ли лимитки; OI не показывает, кто именно открывался.",
        )
    if price_change < 0 and oi_change >= 0:
        return (
            "Цена снизилась, а OI вырос: в открытом интересе стало больше номинала на снижении цены.",
            "Проверь фьючерс/спот и ленту: по одному OI нельзя назвать сторону новых позиций.",
        )
    if price_change >= 0 and oi_change < 0:
        return (
            "Цена выросла, а OI снизился: часть номинала ушла из открытого интереса во время роста.",
            "Сверь спотовый поток и ленту: это совместимо с закрытием шортов, но не доказывает его.",
        )
    return (
        "Цена и OI снизились в одном окне: часть номинала ушла из открытого интереса на снижении цены.",
        "Сверь спотовый поток и ленту: это совместимо с де-риском или закрытием лонгов, но не доказывает причину.",
    )


def context_row(row: dict, symbol_history: list[dict]) -> dict:
    at = row_time(row)
    raw = row.get("raw", {})
    earlier_hour = closest_history_row(symbol_history, at, ONE_HOUR_MINUTES) if at else None
    earlier_day = closest_history_row(symbol_history, at, ONE_DAY_MINUTES) if at else None
    price = fnum(raw.get("last_price")); oi_value = fnum(raw.get("open_interest_value"))
    hour_price = percent_change(price, fnum(earlier_hour.get("raw", {}).get("last_price"))) if earlier_hour else None
    hour_oi = percent_change(oi_value, fnum(earlier_hour.get("raw", {}).get("open_interest_value"))) if earlier_hour else None
    day_price = percent_change(price, fnum(earlier_day.get("raw", {}).get("last_price"))) if earlier_day else None
    day_oi = percent_change(oi_value, fnum(earlier_day.get("raw", {}).get("open_interest_value"))) if earlier_day else None
    explanation, terminal_check = context_explanation(hour_price, hour_oi)
    return {
        "symbol": row.get("symbol"), "as_of_utc": row.get("source_timestamp_utc"),
        "price": price, "open_interest_value": oi_value,
        "turnover_24h": fnum(raw.get("turnover_24h")), "funding_rate": fnum(raw.get("funding_rate")),
        "change_1h": {"price_pct": hour_price, "oi_value_pct": hour_oi, "available": earlier_hour is not None},
        "change_24h": {"price_pct": day_price, "oi_value_pct": day_oi, "available": earlier_day is not None},
        "explanation": explanation, "terminal_check": terminal_check,
        "funding_note": "Funding показан как значение API Bybit для текущего интервала. Экстремальность не заявляется, пока не накоплен собственный baseline.",
        "unknowns": ["OI — агрегированный номинал, а не сторона или личность участника рынка.", "Без стакана, ленты и спота нельзя подтвердить причину движения."],
        "data_quality": row.get("data_quality", "unavailable"),
    }


def history_coverage(history: list[dict], generated_at: datetime) -> dict:
    times = [row_time(row) for row in history]
    times = [value for value in times if value]
    start = min(times) if times else None
    age_minutes = (generated_at - start).total_seconds() / 60 if start else 0.0
    return {
        "rows": len(history), "started_at_utc": start.isoformat().replace("+00:00", "Z") if start else None,
        "age_minutes": round(max(age_minutes, 0.0), 1),
        "one_hour_ready": age_minutes >= ONE_HOUR_MINUTES,
        "one_day_ready": age_minutes >= ONE_DAY_MINUTES,
        "one_week_ready": age_minutes >= ONE_WEEK_MINUTES,
    }


def weekly_brief(rows: list[dict], grouped_history: dict[str, list[dict]], coverage: dict) -> dict:
    if not coverage["one_week_ready"]:
        return {
            "status": "collecting", "reason": "Недельное окно ещё не накоплено из последовательных снимков; заднее заполнение не применяется.",
            "coverage": coverage, "rows": [],
        }
    brief_rows = []
    for row in rows:
        at = row_time(row)
        prior = closest_history_row(grouped_history.get(row.get("symbol"), []), at, ONE_WEEK_MINUTES) if at else None
        if not prior:
            continue
        raw = row.get("raw", {}); earlier = prior.get("raw", {})
        price_change = percent_change(fnum(raw.get("last_price")), fnum(earlier.get("last_price")))
        oi_change = percent_change(fnum(raw.get("open_interest_value")), fnum(earlier.get("open_interest_value")))
        explanation, terminal_check = context_explanation(price_change, oi_change)
        brief_rows.append({
            "symbol": row.get("symbol"), "price_7d_pct": price_change, "oi_value_7d_pct": oi_change,
            "funding_rate": fnum(raw.get("funding_rate")), "turnover_24h": fnum(raw.get("turnover_24h")),
            "explanation": explanation, "terminal_check": terminal_check,
        })
    return {
        "status": "ready" if brief_rows else "collecting",
        "reason": "Недельное сравнение построено только для символов с сопоставимым снимком около 7 суток назад." if brief_rows else "Для текущих символов пока нет сопоставимых снимков около 7 суток назад.",
        "coverage": coverage, "rows": brief_rows,
    }


def public_state(rows: list[dict], generated_at: datetime | None = None, history: list[dict] | None = None) -> dict:
    generated_at = generated_at or utc_now()
    snapshots = [{key: row[key] for key in ("symbol", "venue", "provider", "source_timestamp_utc", "server_received_at_utc", "data_quality", "raw")} for row in rows]
    complete = bool(rows) and all(row["source_timestamp_utc"] for row in rows)
    history = history if history is not None else read_history()
    grouped_history: dict[str, list[dict]] = {}
    for history_row in history:
        grouped_history.setdefault(history_row.get("symbol"), []).append(history_row)
    for value in grouped_history.values():
        value.sort(key=lambda item: row_time(item) or datetime.min.replace(tzinfo=timezone.utc))
    coverage = history_coverage(history, generated_at)
    current_brief = [context_row(row, grouped_history.get(row.get("symbol"), [])) for row in rows]
    return {
        "schema_version": SCHEMA_VERSION, "mode": "shadow",
        "status": "collecting" if complete else "data_unavailable",
        "generated_at_utc": generated_at.isoformat().replace("+00:00", "Z"),
        "source": {"venue": "BYBIT", "provider": PROVIDER, "category": "linear"},
        "universe": {"selection": "reported turnover24h", "cap": UNIVERSE_CAP, "observed": len(snapshots)},
        "shortlist": [], "cases": [], "snapshots": snapshots,
        "history_coverage": coverage,
        "current_brief": {
            "status": "collecting" if complete else "data_unavailable",
            "reason": "Это контекст наблюдения, не торговый сигнал. Сравнения появляются только после фактически накопленного окна.",
            "rows": current_brief,
        },
        "outcomes": {"horizons_minutes": list(OUTCOME_HORIZONS_MINUTES), "available": 0, "status": "not_started_without_cases"},
        "weekly_brief": weekly_brief(rows, grouped_history, coverage),
        "limitations": [
            "Теневой сбор Bybit-only; Radar, уведомления и исполнение не изменяются.",
            "OI и funding не раскрывают сторону или личность участника рынка.",
            "Без потока сделок, стакана и последовательных снимков фаза и сценарий не публикуются.",
            "Пустой shortlist означает отсутствие подтверждённого кейса, а не отсутствие рынка.",
        ],
    }


def unavailable_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION, "mode": "shadow", "status": "data_unavailable", "generated_at_utc": None,
        "source": {"venue": "BYBIT", "provider": PROVIDER, "category": "linear"},
        "universe": {"selection": "reported turnover24h", "cap": UNIVERSE_CAP, "observed": 0},
        "shortlist": [], "cases": [], "snapshots": [],
        "history_coverage": {"rows": 0, "started_at_utc": None, "age_minutes": 0, "one_hour_ready": False, "one_day_ready": False, "one_week_ready": False},
        "current_brief": {"status": "data_unavailable", "reason": "Снимки ещё не опубликованы.", "rows": []},
        "outcomes": {"horizons_minutes": list(OUTCOME_HORIZONS_MINUTES), "available": 0, "status": "not_started_without_cases"},
        "weekly_brief": {"status": "data_unavailable", "reason": "Снимки ещё не опубликованы."},
        "limitations": ["Positioning V1 не получил подтверждённого point-in-time снимка Bybit."],
    }


def read_public_state(path: Path = STATE_PATH) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else unavailable_state()
    except (OSError, json.JSONDecodeError):
        return unavailable_state()


def fetch_bybit_payload() -> dict:
    request = urllib.request.Request(BYBIT_TICKERS, headers={"User-Agent": "trading-pulse-positioning-shadow/1"})
    with urllib.request.urlopen(request, timeout=15) as response: payload = json.loads(response.read())
    if not isinstance(payload, dict) or payload.get("retCode") != 0: raise RuntimeError("bybit_response_invalid")
    return payload


def collect_once() -> dict:
    received = utc_now(); rows = normalize_snapshot(fetch_bybit_payload(), received)
    if not rows: raise RuntimeError("bybit_no_eligible_linear_usdt_rows")
    append_rows(rows); state = public_state(rows, received, read_history()); atomic_write_json(STATE_PATH, state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--selfcheck", action="store_true"); args = parser.parse_args()
    if args.selfcheck:
        sample = {"retCode": 0, "time": "1760000000000", "result": {"list": [{"symbol": "AAAUSDT", "lastPrice": "1.2", "openInterest": "10", "openInterestValue": "12", "turnover24h": "1000", "volume24h": "800", "fundingRate": "0.0001"}, {"symbol": "BADUSDT", "lastPrice": "1", "openInterestValue": "0", "turnover24h": "100"}]}}
        rows = normalize_snapshot(sample, datetime(2026, 9, 17, tzinfo=timezone.utc)); assert len(rows) == 1 and rows[0]["symbol"] == "AAAUSDT"
        state = public_state(rows, history=rows); assert state["mode"] == "shadow" and state["shortlist"] == [] and state["cases"] == []
        assert state["current_brief"]["rows"][0]["change_1h"]["available"] is False
        print("positioning_shadow selfcheck OK"); return 0
    state = collect_once(); print(json.dumps({"status": state["status"], "observed": state["universe"]["observed"], "cases": len(state["cases"])}, ensure_ascii=False)); return 0


if __name__ == "__main__": raise SystemExit(main())

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


def public_state(rows: list[dict], generated_at: datetime | None = None) -> dict:
    generated_at = generated_at or utc_now()
    snapshots = [{key: row[key] for key in ("symbol", "venue", "provider", "source_timestamp_utc", "server_received_at_utc", "data_quality", "raw")} for row in rows]
    complete = bool(rows) and all(row["source_timestamp_utc"] for row in rows)
    return {
        "schema_version": SCHEMA_VERSION, "mode": "shadow",
        "status": "collecting" if complete else "data_unavailable",
        "generated_at_utc": generated_at.isoformat().replace("+00:00", "Z"),
        "source": {"venue": "BYBIT", "provider": PROVIDER, "category": "linear"},
        "universe": {"selection": "reported turnover24h", "cap": UNIVERSE_CAP, "observed": len(snapshots)},
        "shortlist": [], "cases": [], "snapshots": snapshots,
        "outcomes": {"horizons_minutes": list(OUTCOME_HORIZONS_MINUTES), "available": 0, "status": "not_started_without_cases"},
        "weekly_brief": {"status": "data_unavailable", "reason": "Недельные point-in-time снимки ещё не накоплены."},
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
    append_rows(rows); state = public_state(rows, received); atomic_write_json(STATE_PATH, state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--selfcheck", action="store_true"); args = parser.parse_args()
    if args.selfcheck:
        sample = {"retCode": 0, "time": "1760000000000", "result": {"list": [{"symbol": "AAAUSDT", "lastPrice": "1.2", "openInterest": "10", "openInterestValue": "12", "turnover24h": "1000", "volume24h": "800", "fundingRate": "0.0001"}, {"symbol": "BADUSDT", "lastPrice": "1", "openInterestValue": "0", "turnover24h": "100"}]}}
        rows = normalize_snapshot(sample, datetime(2026, 9, 17, tzinfo=timezone.utc)); assert len(rows) == 1 and rows[0]["symbol"] == "AAAUSDT"
        state = public_state(rows); assert state["mode"] == "shadow" and state["shortlist"] == [] and state["cases"] == []
        print("positioning_shadow selfcheck OK"); return 0
    state = collect_once(); print(json.dumps({"status": state["status"], "observed": state["universe"]["observed"], "cases": len(state["cases"])}, ensure_ascii=False)); return 0


if __name__ == "__main__": raise SystemExit(main())

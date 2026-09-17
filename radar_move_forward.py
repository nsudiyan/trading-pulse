#!/usr/bin/env python3
"""H-RADAR-MOVE-01 forward-only measurement ledger.

This module cannot emit a trade, side, return, PnL, fee, or optimisation.
It only stores a receipt-time snapshot for each successfully delivered
individual Pulse Radar alert and later resolves one binary fact: did the
symbol reach +1% OR -1% from the next complete one-minute candle within 60
minutes?  Controls are matched before that outcome exists.

The production detector calls :func:`record_delivery` *after* Telegram
acknowledges delivery.  No detector threshold, selection, cap or cooldown is
read or written here.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import statistics
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "outcomes"
PREREG = OUT / "studies" / "2026-07-13_H-RADAR-MOVE-01_FORWARD_PREREG.md"
RECEIPT = OUT / "studies" / "2026-07-13_H-RADAR-MOVE-01_FORWARD_FREEZE_RECEIPT.json"
SNAPSHOTS = OUT / "radar_move_forward_snapshots.jsonl"
RESOLVED = OUT / "radar_move_forward_resolved.jsonl"
SUMMARY = OUT / "radar_move_forward_status.json"

VERSION = "H-RADAR-MOVE-01-FWD-v1"
K = 5
RANGE_MAX_DISTANCE = 0.20
MOVE_PCT = 0.01
WINDOW_MINUTES = 60
SETTLE_SECONDS = 90
EVAL_START = "2026-08-03T00:00:00+00:00"
EVAL_END = "2026-08-24T00:00:00+00:00"  # exclusive: three complete Mon-Sun weeks
SEED = 20260713
BYBIT = "https://api.bybit.com/v5/market/kline"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _prereg_sha() -> str:
    return hashlib.sha256(PREREG.read_bytes()).hexdigest()


def assert_freeze() -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    actual = _prereg_sha()
    if receipt.get("prereg_sha256") != actual:
        raise RuntimeError("forward prereg SHA mismatch: refuse to collect or resolve")
    if receipt.get("protocol_version") != VERSION:
        raise RuntimeError("forward protocol version mismatch")


def _pre_range(klines: list) -> float | None:
    """Two hours before the radar signal bar; does not inspect current/future bar."""
    if len(klines) < 6:
        return None
    prior = klines[-6:-2]  # four completed bars immediately before signal bar (-2)
    lo = min(float(bar[3]) for bar in prior)
    hi = max(float(bar[2]) for bar in prior)
    return hi / lo - 1.0 if lo > 0 else None


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    out: dict[str, float] = {}
    i = 0
    while i < len(ordered):
        j = i + 1
        while j < len(ordered) and ordered[j][1] == ordered[i][1]:
            j += 1
        average_rank = (i + j - 1) / 2
        for symbol, _ in ordered[i:j]:
            out[symbol] = average_rank / len(ordered)
        i = j
    return out


def select_controls(treatment: str, scan_rows: list[dict]) -> tuple[list[str], float | None, str | None, list[dict]]:
    """Frozen five-control match from the receipt-time scan snapshot.

    ``scan_rows`` must include every live top-N symbol, not merely detector
    hits.  It contains only already-known data: rank, raw detector status and
    pre-scan 2h range.  No outcome candle is requested here.
    """
    valid = [row for row in scan_rows if row.get("pre_range") is not None]
    by_symbol = {row["symbol"]: row for row in valid}
    target = by_symbol.get(treatment)
    if target is None:
        return [], None, "treatment_missing_pre_range", valid
    percentiles = _percentiles({row["symbol"]: float(row["pre_range"]) for row in valid})
    n = len(scan_rows)
    if n < 30:
        return [], None, "universe_under_30", valid
    target_q = min(4, 5 * int(target["rank"]) // n)
    candidates: list[tuple[float, str]] = []
    for row in valid:
        symbol = row["symbol"]
        if symbol == treatment or row.get("raw_hit"):
            continue
        if min(4, 5 * int(row["rank"]) // n) != target_q:
            continue
        distance = abs(percentiles[symbol] - percentiles[treatment])
        if distance <= RANGE_MAX_DISTANCE:
            candidates.append((distance, symbol))
    candidates.sort()
    if len(candidates) < K:
        return [], None, "fewer_than_5_matched_nonhits", valid
    return [symbol for _, symbol in candidates[:K]], candidates[K - 1][0], None, valid


def scan_snapshot(symbols: list[str], klines_by_symbol: dict[str, list], raw_hits: set[str]) -> list[dict]:
    """Create serialisable as-of feature snapshot from a completed radar scan."""
    rows = []
    for rank, symbol in enumerate(symbols):
        klines = klines_by_symbol.get(symbol) or []
        rows.append({
            "symbol": symbol,
            "rank": rank,
            "raw_hit": symbol in raw_hits,
            "pre_range": _pre_range(klines),
        })
    return rows


def _next_minute_start(receipt: datetime) -> datetime:
    second_floor = receipt.replace(second=0, microsecond=0)
    return datetime.fromtimestamp(second_floor.timestamp() + 60, tz=timezone.utc)


def record_delivery(*, symbol: str, vol_ratio: float, symbols: list[str],
                    klines_by_symbol: dict[str, list], raw_hits: set[str],
                    receipt_time: datetime | None = None) -> dict:
    """Append one immutable receipt snapshot after a confirmed Telegram delivery.

    A matching failure is intentionally recorded as an excluded observation;
    it is never silently dropped or replaced by looser controls.
    """
    assert_freeze()
    receipt = (receipt_time or now_utc()).astimezone(timezone.utc)
    rows = scan_snapshot(symbols, klines_by_symbol, raw_hits)
    controls, fifth_distance, reason, valid = select_controls(symbol, rows)
    receipt_ms = int(receipt.timestamp() * 1000)
    ident = hashlib.sha256(f"{symbol}|{receipt_ms}|{VERSION}".encode()).hexdigest()[:24]
    existing = {row.get("id") for row in _read_jsonl(SNAPSHOTS)}
    if ident in existing:
        return {"id": ident, "duplicate": True}
    start = _next_minute_start(receipt)
    record = {
        "schema": 1,
        "protocol_version": VERSION,
        "prereg_sha256": _prereg_sha(),
        "id": ident,
        "receipt_ts_utc": iso(receipt),
        "outcome_start_ts_utc": iso(start),
        "outcome_end_ts_utc": iso(datetime.fromtimestamp(start.timestamp() + WINDOW_MINUTES * 60, tz=timezone.utc)),
        "symbol": symbol,
        "vol_ratio": round(float(vol_ratio), 8),
        "universe_size": len(symbols),
        "universe_symbols_sha256": hashlib.sha256("|".join(symbols).encode()).hexdigest(),
        "match_status": "matched" if reason is None else "excluded",
        "exclude_reason": reason,
        "controls": controls,
        "fifth_control_range_percentile_distance": fifth_distance,
        "snapshot_rows": rows,
        "valid_feature_symbols": len(valid),
        "evaluation_window": "verdict" if EVAL_START <= iso(receipt) < EVAL_END else "outside_verdict_window",
    }
    _append_jsonl(SNAPSHOTS, record)
    write_status()
    return record


def fetch_1m(symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, float, float, float]]:
    url = (f"{BYBIT}?category=linear&symbol={symbol}&interval=1&start={start_ms}"
           f"&end={end_ms}&limit=1000")
    with urllib.request.urlopen(url, timeout=15) as response:
        body = json.loads(response.read())
    if body.get("retCode") != 0:
        raise RuntimeError(f"Bybit retCode={body.get('retCode')}: {body.get('retMsg')}")
    by_ts: dict[int, tuple[int, float, float, float]] = {}
    for row in body.get("result", {}).get("list") or []:
        ts = int(row[0])
        by_ts[ts] = (ts, float(row[1]), float(row[2]), float(row[3]))
    return [by_ts[ts] for ts in sorted(by_ts)]


def minute_outcome(rows: list[tuple[int, float, float, float]], start_ms: int) -> int | None:
    expected = [start_ms + minute * 60_000 for minute in range(WINDOW_MINUTES)]
    by_ts = {row[0]: row for row in rows}
    if any(ts not in by_ts for ts in expected):
        return None
    first = by_ts[start_ms]
    entry = first[1]
    window = [by_ts[ts] for ts in expected]
    return int(max(row[2] for row in window) >= entry * (1 + MOVE_PCT)
               or min(row[3] for row in window) <= entry * (1 - MOVE_PCT))


def resolve_pending(now: datetime | None = None) -> dict:
    """Resolve raw binary outcomes only after full 60m + settlement buffer.

    It deliberately does not calculate, print or publish an effect before the
    preregistered evaluation period ends.
    """
    assert_freeze()
    now = (now or now_utc()).astimezone(timezone.utc)
    done = {row.get("id") for row in _read_jsonl(RESOLVED)}
    resolved = 0
    deferred = 0
    for snap in _read_jsonl(SNAPSHOTS):
        if snap.get("id") in done or snap.get("match_status") != "matched":
            continue
        end = parse_iso(snap["outcome_end_ts_utc"])
        if now.timestamp() < end.timestamp() + SETTLE_SECONDS:
            deferred += 1
            continue
        start = parse_iso(snap["outcome_start_ts_utc"])
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        symbols = [snap["symbol"], *snap["controls"]]
        outcomes = {}
        missing = []
        for symbol in symbols:
            try:
                result = minute_outcome(fetch_1m(symbol, start_ms, end_ms), start_ms)
            except Exception:
                result = None
            if result is None:
                missing.append(symbol)
            else:
                outcomes[symbol] = result
            time.sleep(0.05)
        if missing:
            deferred += 1
            continue  # no partial matched set; retry later rather than impute zero
        _append_jsonl(RESOLVED, {
            "schema": 1,
            "protocol_version": VERSION,
            "prereg_sha256": _prereg_sha(),
            "id": snap["id"],
            "resolved_ts_utc": iso(now),
            "outcomes": outcomes,
        })
        resolved += 1
    status = write_status(now)
    return {"resolved": resolved, "deferred": deferred, "status": status}


def _t_stat(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    sd = statistics.stdev(values)
    return 0.0 if sd == 0 else statistics.mean(values) / (sd / math.sqrt(len(values)))


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return ordered[lo] if lo == hi else ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)


def _bootstrap(daily: dict[str, float]) -> tuple[float, float] | None:
    if not daily:
        return None
    current = parse_iso(EVAL_START)
    end = parse_iso(EVAL_END)
    calendar = []
    while current < end:
        calendar.append(daily.get(current.date().isoformat()))
        current += timedelta(days=1)
    block_days = 7
    blocks = [calendar[i:i + block_days] for i in range(len(calendar) - block_days + 1)]
    rng = random.Random(SEED)
    reps = []
    picks = math.ceil(len(calendar) / block_days)
    for _ in range(10_000):
        sample = []
        for _ in range(picks):
            sample.extend(blocks[rng.randrange(len(blocks))])
        active = [value for value in sample[:len(calendar)] if value is not None]
        if active:
            reps.append(statistics.mean(active))
    return (_quantile(reps, .025), _quantile(reps, .975)) if reps else None
    rng = random.Random(SEED)
    reps = [statistics.mean([vals[rng.randrange(len(vals))] for _ in vals]) for _ in range(10_000)]
    return (_quantile(reps, .025), _quantile(reps, .975))


def _final_verdict() -> dict:
    snaps = {row["id"]: row for row in _read_jsonl(SNAPSHOTS)
             if row.get("evaluation_window") == "verdict" and row.get("match_status") == "matched"}
    resolved = {row["id"]: row for row in _read_jsonl(RESOLVED) if row.get("id") in snaps}
    if set(snaps) != set(resolved):
        return {"verdict": "INCONCLUSIVE / INCOMPLETE FORWARD DATA", "n_matched": len(snaps), "n_resolved": len(resolved)}
    by_day: dict[str, list[float]] = {}
    for ident, snap in snaps.items():
        outcomes = resolved[ident]["outcomes"]
        day = snap["receipt_ts_utc"][:10]
        d = float(outcomes[snap["symbol"]]) - statistics.mean(float(outcomes[c]) for c in snap["controls"])
        by_day.setdefault(day, []).append(d)
    daily = {day: statistics.mean(values) for day, values in by_day.items()}
    values = list(daily.values())
    effect = statistics.mean(values) if values else None
    t = _t_stat(values)
    ci = _bootstrap(daily)
    passed = len(values) >= 15 and effect is not None and effect > 0 and (t or -math.inf) >= 2 and ci and ci[0] > 0
    return {
        "verdict": "FORWARD CONFIRMED AS A MOVEMENT DETECTOR ONLY" if passed else "NOT CONFIRMED / NO DIRECTIONAL CLAIM",
        "n_matched": len(snaps), "n_resolved": len(resolved), "active_utc_days": len(values),
        "daily_effect": effect, "day_t": t, "bootstrap_ci_95": ci,
    }


def write_status(now: datetime | None = None) -> dict:
    now = (now or now_utc()).astimezone(timezone.utc)
    snapshots = _read_jsonl(SNAPSHOTS)
    resolved = _read_jsonl(RESOLVED)
    status = {
        "protocol_version": VERSION,
        "prereg_sha256": _prereg_sha() if PREREG.exists() else None,
        "frozen": True,
        "receipt_time_basis": "server UTC immediately after Telegram delivery acknowledgement",
        "outcome": "MOVE_1PCT: either +1% or -1% in 60m from next complete 1m bar",
        "direction": "disabled",
        "pnl": "forbidden",
        "evaluation_start_utc": EVAL_START,
        "evaluation_end_utc_exclusive": EVAL_END,
        "status": "COLLECTING — outcomes sealed until 2026-08-24T00:00:00+00:00",
        "alerts_recorded": len(snapshots),
        "matched_sets": sum(row.get("match_status") == "matched" for row in snapshots),
        "excluded_alerts": sum(row.get("match_status") == "excluded" for row in snapshots),
        "resolved_sets_sealed": len(resolved),
        "updated_at_utc": iso(now),
    }
    if now >= parse_iso(EVAL_END):
        status["status"] = "FINALIZING"
        status["final"] = _final_verdict()
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return status


def selfcheck() -> None:
    symbols = [f"S{i:03d}USDT" for i in range(30)]
    klines = {}
    for i, symbol in enumerate(symbols):
        base = 100
        klines[symbol] = [[j, base, base * 1.01, base * .99, base, 10] for j in range(12)]
    # One treatment and >=5 valid non-hits from the same liquidity quintile.
    rows = scan_snapshot(symbols, klines, {symbols[14]})
    selected, fifth, reason, _ = select_controls(symbols[14], rows)
    assert len(selected) == 5 and fifth == 0 and reason is None, (selected, fifth, reason)
    assert all(sym != symbols[14] for sym in selected)
    # A raw-hit candidate cannot enter controls.
    rows[13]["raw_hit"] = True
    selected, _, _, _ = select_controls(symbols[14], rows)
    assert symbols[13] not in selected
    start = 1_700_000_000_000
    bars = [(start + i * 60_000, 100, 101, 99.5) for i in range(60)]
    assert minute_outcome(bars, start) == 1
    incomplete = bars[:-1]
    assert minute_outcome(incomplete, start) is None
    print("✓ radar_move_forward selfcheck: matching, raw-hit exclusion, exact 60m completeness")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--selfcheck", action="store_true")
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.selfcheck:
        selfcheck()
    elif args.resolve:
        print(json.dumps(resolve_pending(), indent=2, sort_keys=True))
    else:
        print(json.dumps(write_status(), indent=2, sort_keys=True))

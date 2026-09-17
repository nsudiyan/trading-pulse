#!/usr/bin/env python3
"""Append-only 24h/48h price-path observations for delivered Radar alerts.

This is deliberately separate from the frozen H-RADAR-MOVE-01 forward
protocol.  It never imports the detector, changes a delivery, selects a
control, computes an effect, or emits a trade signal.  It reads immutable
receipt snapshots and, once a complete future window exists, appends a
descriptive price-path record for the alert symbol only.

The records are observational diagnostics, not preregistered outcomes and
must not be used for any decision about the frozen ±1%/60m forward test.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "outcomes"
SNAPSHOTS = OUT / "radar_move_forward_snapshots.jsonl"
FOLLOWUPS = OUT / "radar_followup_observations.jsonl"
BYBIT = "https://api.bybit.com/v5/market/kline"
HORIZONS_HOURS = (24, 48)
SETTLE_SECONDS = 90
SCHEMA = "RADAR-FOLLOWUP-OBS-v1"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def fetch_1m(symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, float, float, float, float]]:
    """Fetch and deduplicate complete 1m bars in API-sized chunks."""
    bars: dict[int, tuple[int, float, float, float, float]] = {}
    cursor = start_ms
    chunk_ms = 1_000 * 60_000
    while cursor < end_ms:
        chunk_end = min(end_ms, cursor + chunk_ms)
        url = (f"{BYBIT}?category=linear&symbol={symbol}&interval=1&start={cursor}"
               f"&end={chunk_end - 1}&limit=1000")
        with urllib.request.urlopen(url, timeout=15) as response:
            body = json.loads(response.read())
        if body.get("retCode") != 0:
            raise RuntimeError(f"Bybit retCode={body.get('retCode')}: {body.get('retMsg')}")
        for row in body.get("result", {}).get("list") or []:
            ts = int(row[0])
            bars[ts] = (ts, float(row[1]), float(row[2]), float(row[3]), float(row[4]))
        cursor = chunk_end
        time.sleep(.05)
    return [bars[ts] for ts in sorted(bars)]


def path_observation(rows: list[tuple[int, float, float, float, float]], start_ms: int, horizon_hours: int) -> dict | None:
    """Return a complete, fixed-horizon path summary; never impute missing bars."""
    count = horizon_hours * 60
    expected = [start_ms + i * 60_000 for i in range(count)]
    by_ts = {row[0]: row for row in rows}
    if any(ts not in by_ts for ts in expected):
        return None
    window = [by_ts[ts] for ts in expected]
    entry = window[0][1]  # open of the next complete minute, identical anchor to H-RADAR-MOVE-01
    if entry <= 0:
        return None
    peak_high = max(window, key=lambda row: (row[2], -row[0]))
    trough_low = min(window, key=lambda row: (row[3], row[0]))
    final = window[-1]
    return {
        "anchor": "open_of_next_complete_1m_bar",
        "entry_price": entry,
        "window_start_ts_utc": iso(datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)),
        "window_end_ts_utc_exclusive": iso(datetime.fromtimestamp((start_ms + count * 60_000) / 1000, tz=timezone.utc)),
        "bars_expected": count,
        "bars_received": count,
        "close_return_pct": round((final[4] / entry - 1) * 100, 6),
        "max_up_pct": round((peak_high[2] / entry - 1) * 100, 6),
        "max_down_pct": round((trough_low[3] / entry - 1) * 100, 6),
        "peak_high_ts_utc": iso(datetime.fromtimestamp(peak_high[0] / 1000, tz=timezone.utc)),
        "trough_low_ts_utc": iso(datetime.fromtimestamp(trough_low[0] / 1000, tz=timezone.utc)),
    }


def resolve_pending(now: datetime | None = None) -> dict:
    """Append each 24h/48h observation once its complete candle window settles."""
    now = (now or now_utc()).astimezone(timezone.utc)
    existing = {(row.get("id"), row.get("horizon_hours")) for row in _read_jsonl(FOLLOWUPS)}
    written = deferred = 0
    for snap in _read_jsonl(SNAPSHOTS):
        if snap.get("match_status") != "matched":
            continue
        start = parse_iso(snap["outcome_start_ts_utc"])
        start_ms = int(start.timestamp() * 1000)
        for horizon in HORIZONS_HOURS:
            if (snap["id"], horizon) in existing:
                continue
            end = start + timedelta(hours=horizon)
            if now < end + timedelta(seconds=SETTLE_SECONDS):
                deferred += 1
                continue
            try:
                observation = path_observation(
                    fetch_1m(snap["symbol"], start_ms, start_ms + horizon * 60 * 60_000), start_ms, horizon
                )
            except Exception:
                observation = None
            if observation is None:
                deferred += 1
                continue
            _append_jsonl(FOLLOWUPS, {
                "schema": SCHEMA,
                "id": snap["id"],
                "symbol": snap["symbol"],
                "receipt_ts_utc": snap["receipt_ts_utc"],
                "horizon_hours": horizon,
                "observation_class": "DESCRIPTIVE_ONLY_NOT_PART_OF_H_RADAR_MOVE_01",
                "resolved_ts_utc": iso(now),
                **observation,
            })
            written += 1
    return {"written": written, "deferred": deferred, "followup_path": str(FOLLOWUPS)}


def selfcheck() -> None:
    start = 1_700_000_000_000
    rows = [(start + i * 60_000, 100.0, 102.0 if i == 5 else 101.0,
             98.0 if i == 7 else 99.0, 101.0 if i == 59 else 100.0) for i in range(60)]
    obs = path_observation(rows, start, 1)
    assert obs is not None and obs["max_up_pct"] == 2.0 and obs["max_down_pct"] == -2.0
    assert obs["close_return_pct"] == 1.0 and obs["bars_received"] == 60
    assert path_observation(rows[:-1], start, 1) is None
    print("✓ radar_followup_observer selfcheck: complete path, extrema and missing-bar deferral")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--selfcheck", action="store_true")
    parser.add_argument("--resolve", action="store_true")
    args = parser.parse_args()
    if args.selfcheck:
        selfcheck()
    elif args.resolve:
        print(json.dumps(resolve_pending(), indent=2, sort_keys=True))

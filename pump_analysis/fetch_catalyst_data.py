#!/usr/bin/env python3
"""
fetch_catalyst_data.py — Causal attribution sources for AVEVA-58

Fetches and saves all available historical datasets for pump causal analysis.
What IS available (real historical data):
  - stablecoin_flows.csv:   DefiLlama daily stablecoin supply + delta (2024-05 → 2026-05)
  - macro_events.csv:       Fear & Greed daily index (2023-08 → 2026-05, ~1000 days)
  - sweep_clusters.csv:     Liquidation clusters from liquidations.db (Apr-May 2026)
  - news_signals.csv:       Screener signals from resolved.csv (Apr-May 2026)
  - token_unlocks.csv:      Static schedule from outcomes/unlock_schedule.json

What is NOT available (documented in data_manifest.json):
  - whale_positions.csv:    Binance L/S ratio endpoints reject startTime — real-time only
  - orderbook_imbalance.csv: Real-time orderbook, no historical data
"""

import csv
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fetch_catalyst")

OUT_DIR   = Path(__file__).parent / "catalyst_data"
REPO_DIR  = Path(__file__).parent.parent

START_MS  = int(datetime(2024, 5, 1, tzinfo=timezone.utc).timestamp() * 1000)
END_MS    = int(datetime(2026, 5, 27, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)
START_TS  = START_MS // 1000
END_TS    = END_MS // 1000

_sess = requests.Session()
_sess.headers.update({"User-Agent": "PumpAnalysis-CatalystFetcher/1.0"})


def _get(url: str, params: dict = None) -> list | dict | None:
    for attempt in range(4):
        try:
            r = _sess.get(url, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            time.sleep(2 ** attempt)
            log.warning("HTTP error (%s) attempt %d/4", e, attempt + 1)
    return None


# ── 1. Stablecoin flows (DefiLlama) ───────────────────────────────────────────

def fetch_stablecoin_flows() -> int:
    """Daily total USD stablecoin supply + day-over-day change. Full 2-year coverage."""
    out_path = OUT_DIR / "stablecoin_flows.csv"
    if out_path.exists():
        with open(out_path) as f:
            n = sum(1 for _ in f) - 1
        log.info("stablecoin_flows: SKIP (cached %d rows)", n)
        return n

    log.info("Fetching stablecoin flows from DefiLlama ...")
    data = _get("https://stablecoins.llama.fi/stablecoincharts/all")
    if not data:
        log.error("DefiLlama stablecoin fetch failed")
        return 0

    rows = []
    prev_usd = None
    for entry in data:
        ts = int(entry["date"])
        if not (START_TS - 86400 <= ts <= END_TS):
            if ts < START_TS - 86400:
                # Keep last entry before start to compute delta for first in-period entry
                tc_usd = entry.get("totalCirculatingUSD", {}).get("peggedUSD", 0)
                prev_usd = tc_usd if tc_usd else prev_usd
            continue
        tc_usd = entry.get("totalCirculatingUSD", {}).get("peggedUSD", 0) or 0
        delta = tc_usd - prev_usd if prev_usd is not None else 0
        rows.append({
            "date":          datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
            "timestamp":     ts,
            "total_usd_supply": tc_usd,
            "delta_usd_1d":  round(delta),
        })
        prev_usd = tc_usd

    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "timestamp", "total_usd_supply", "delta_usd_1d"])
        w.writeheader()
        w.writerows(rows)

    log.info("stablecoin_flows: %d rows saved", len(rows))
    return len(rows)


# ── 2. Macro events: Fear & Greed (alternative.me) ───────────────────────────

def fetch_fear_greed() -> int:
    """Daily Fear & Greed index. alternative.me limit=1000 → covers Aug 2023 → today."""
    out_path = OUT_DIR / "macro_events.csv"
    if out_path.exists():
        with open(out_path) as f:
            n = sum(1 for _ in f) - 1
        log.info("macro_events: SKIP (cached %d rows)", n)
        return n

    log.info("Fetching Fear & Greed index ...")
    data = _get("https://api.alternative.me/fng/", {"limit": 1000, "format": "json"})
    if not data or "data" not in data:
        log.error("Fear & Greed fetch failed")
        return 0

    rows = []
    for entry in data["data"]:
        ts = int(entry["timestamp"])
        if not (START_TS <= ts <= END_TS):
            continue
        rows.append({
            "date":                datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
            "timestamp":           ts,
            "value":               int(entry["value"]),
            "value_classification": entry["value_classification"],
        })

    rows.sort(key=lambda r: r["timestamp"])
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "timestamp", "value", "value_classification"])
        w.writeheader()
        w.writerows(rows)

    log.info("macro_events: %d rows saved (Fear & Greed)", len(rows))
    return len(rows)


# ── 3. Sweep clusters (from liquidations.db) ──────────────────────────────────

def build_sweep_clusters() -> int:
    """
    Aggregate liquidation events from liquidations.db into 1-hour clusters.
    Coverage: Apr 14 – May 27, 2026 (live tracker start date).
    """
    out_path = OUT_DIR / "sweep_clusters.csv"
    if out_path.exists():
        with open(out_path) as f:
            n = sum(1 for _ in f) - 1
        log.info("sweep_clusters: SKIP (cached %d rows)", n)
        return n

    db_path = REPO_DIR / "liquidations.db"
    if not db_path.exists():
        log.warning("liquidations.db not found")
        return 0

    log.info("Building sweep clusters from liquidations.db ...")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Aggregate into 1-hour buckets: count + total USD
    cur.execute("""
        SELECT
            (ts / 3600000) * 3600000 AS hour_ts,
            symbol,
            side,
            COUNT(*) AS liq_count,
            SUM(usd) AS liq_usd_total
        FROM liquidations
        GROUP BY hour_ts, symbol, side
        ORDER BY hour_ts, symbol
    """)
    rows = []
    for r in cur.fetchall():
        hour_ts = r[0]
        rows.append({
            "hour_ts":       hour_ts,
            "datetime":      datetime.fromtimestamp(hour_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "symbol":        r[1],
            "side":          r[2],
            "liq_count":     r[3],
            "liq_usd_total": round(r[4] or 0, 2),
        })
    conn.close()

    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["hour_ts", "datetime", "symbol", "side",
                                           "liq_count", "liq_usd_total"])
        w.writeheader()
        w.writerows(rows)

    log.info("sweep_clusters: %d hourly buckets saved", len(rows))
    return len(rows)


# ── 4. Token unlocks (static schedule) ───────────────────────────────────────

def export_token_unlocks() -> int:
    """
    Export from outcomes/unlock_schedule.json.
    Coverage: manual curation as of 2026-05-26 (5 near-term entries).
    Free API sources (token.unlocks.app, DefiLlama emissions) are paywalled as of 2026.
    """
    out_path = OUT_DIR / "token_unlocks.csv"
    if out_path.exists():
        with open(out_path) as f:
            n = sum(1 for _ in f) - 1
        log.info("token_unlocks: SKIP (cached %d rows)", n)
        return n

    schedule_path = REPO_DIR / "outcomes" / "unlock_schedule.json"
    if not schedule_path.exists():
        log.warning("unlock_schedule.json not found")
        return 0

    data = json.loads(schedule_path.read_text())
    schedules = data.get("schedules", {})

    rows = []
    for symbol, entries in schedules.items():
        if not isinstance(entries, list):
            entries = [entries]
        for e in entries:
            rows.append({
                "symbol":         symbol,
                "unlock_date":    e.get("date"),
                "pct_of_supply":  e.get("pct_of_supply"),
                "usd_value":      e.get("usd_value"),
                "type":           e.get("type", ""),
                "note":           e.get("note", ""),
            })

    rows.sort(key=lambda r: r.get("unlock_date") or "")
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "unlock_date", "pct_of_supply",
                                           "usd_value", "type", "note"])
        w.writeheader()
        w.writerows(rows)

    curated_at = data.get("_data_curated_at", "unknown")
    log.info("token_unlocks: %d entries saved (curated %s)", len(rows), curated_at)
    return len(rows)


# ── 5. News/screener signals (resolved.csv) ───────────────────────────────────

def export_news_signals() -> int:
    """
    Export screener signals with outcomes from resolved.csv.
    Coverage: Apr 12 – May 26, 2026 (live screener start).
    These are the actual trading signals generated by the system.
    """
    out_path = OUT_DIR / "news_signals.csv"
    if out_path.exists():
        with open(out_path) as f:
            n = sum(1 for _ in f) - 1
        log.info("news_signals: SKIP (cached %d rows)", n)
        return n

    resolved_path = REPO_DIR / "outcomes" / "resolved.csv"
    if not resolved_path.exists():
        log.warning("resolved.csv not found")
        return 0

    log.info("Exporting news_signals from resolved.csv ...")
    with open(resolved_path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # Keep key fields relevant to causal analysis
    keep_fields = [
        "run_ts", "symbol", "setup", "score", "grade", "pump_score",
        "direction", "price_entry", "funding", "oi_24h_pct",
        "outcome_4h", "outcome_24h", "change_4h_pct", "change_24h_pct",
        "channel_conf", "whale_flag",
    ]
    available = [f for f in keep_fields if f in (rows[0].keys() if rows else [])]

    out_rows = []
    for r in rows:
        out_rows.append({f: r.get(f, "") for f in available})

    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=available)
        w.writeheader()
        w.writerows(out_rows)

    log.info("news_signals: %d rows saved (Apr-May 2026 screener signals)", len(out_rows))
    return len(out_rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> dict:
    OUT_DIR.mkdir(exist_ok=True)
    log.info("=== AVEVA-58 Catalyst data fetcher ===")

    results = {
        "stablecoin_flows":  fetch_stablecoin_flows(),
        "macro_events":      fetch_fear_greed(),
        "sweep_clusters":    build_sweep_clusters(),
        "token_unlocks":     export_token_unlocks(),
        "news_signals":      export_news_signals(),
        "whale_positions":   0,          # no historical API available
        "orderbook_imbalance": 0,        # real-time only
    }

    log.info("\n=== Catalyst data summary ===")
    for k, n in results.items():
        log.info("  %-25s %d rows", k, n)

    return results


if __name__ == "__main__":
    main()

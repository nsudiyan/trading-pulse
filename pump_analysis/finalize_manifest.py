#!/usr/bin/env python3
"""
finalize_manifest.py — Merge klines manifest + catalyst data coverage into data_manifest.json.

Run AFTER both fetch_historical.py and fetch_catalyst_data.py complete.
"""

import csv
import gzip
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("finalize")

OUT_DIR   = Path(__file__).parent
REPO_DIR  = OUT_DIR.parent
CAT_DIR   = OUT_DIR / "catalyst_data"


def _count_csv(path: Path) -> int:
    if not path.exists():
        return 0
    with open(path) as f:
        return sum(1 for _ in f) - 1


def _count_gz(path: Path) -> int:
    if not path.exists():
        return 0
    with gzip.open(path, "rt") as f:
        return sum(1 for _ in f) - 1


def _ts_range_gz(path: Path) -> tuple[str, str]:
    if not path.exists():
        return "N/A", "N/A"
    with gzip.open(path, "rt") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        return "N/A", "N/A"
    tss = sorted(int(r["open_time"]) for r in rows)
    fmt = "%Y-%m-%d"
    return (datetime.fromtimestamp(tss[0] / 1000, tz=timezone.utc).strftime(fmt),
            datetime.fromtimestamp(tss[-1] / 1000, tz=timezone.utc).strftime(fmt))


def build_manifest() -> dict:
    # Load existing manifest if available (from fetch_historical.py)
    manifest_path = OUT_DIR / "data_manifest.json"
    base = {}
    if manifest_path.exists():
        base = json.loads(manifest_path.read_text())

    symbols = base.get("symbols", [])

    # Klines stats per symbol
    klines_detail = {}
    for tf in ["5m", "15m", "1h"]:
        kline_dir = OUT_DIR / f"klines_{tf}"
        for sym in symbols:
            path = kline_dir / f"{sym}.csv.gz"
            n = _count_gz(path)
            ts_start, ts_end = _ts_range_gz(path)
            klines_detail.setdefault(tf, {})[sym] = {
                "rows": n, "period_start": ts_start, "period_end": ts_end
            }

    # Catalyst coverage
    catalyst = {
        "stablecoin_flows": {
            "file":     "catalyst_data/stablecoin_flows.csv",
            "source":   "DefiLlama /stablecoincharts/all",
            "coverage": "2024-05-01 → 2026-05-27 (daily)",
            "columns":  ["date", "timestamp", "total_usd_supply", "delta_usd_1d"],
            "rows":     _count_csv(CAT_DIR / "stablecoin_flows.csv"),
        },
        "macro_events": {
            "file":     "catalyst_data/macro_events.csv",
            "source":   "alternative.me Fear & Greed Index",
            "coverage": "2024-05-01 → 2026-05-27 (daily)",
            "columns":  ["date", "timestamp", "value", "value_classification"],
            "rows":     _count_csv(CAT_DIR / "macro_events.csv"),
        },
        "sweep_clusters": {
            "file":     "catalyst_data/sweep_clusters.csv",
            "source":   "liquidations.db (live Bybit tracker)",
            "coverage": "2026-04-14 → 2026-05-27 (1h buckets)",
            "coverage_gap": "2024-05-01 → 2026-04-14 not available (live tracker start)",
            "columns":  ["hour_ts", "datetime", "symbol", "side", "liq_count", "liq_usd_total"],
            "rows":     _count_csv(CAT_DIR / "sweep_clusters.csv"),
        },
        "token_unlocks": {
            "file":     "catalyst_data/token_unlocks.csv",
            "source":   "outcomes/unlock_schedule.json (manual curation)",
            "coverage": "near-term unlocks only (curated 2026-05-26)",
            "coverage_gap": "Free API sources (token.unlocks.app, DefiLlama emissions) require subscription as of 2026",
            "columns":  ["symbol", "unlock_date", "pct_of_supply", "usd_value", "type", "note"],
            "rows":     _count_csv(CAT_DIR / "token_unlocks.csv"),
        },
        "news_signals": {
            "file":     "catalyst_data/news_signals.csv",
            "source":   "outcomes/resolved.csv (screener signal log)",
            "coverage": "2026-04-12 → 2026-05-26",
            "coverage_gap": "2024-05-01 → 2026-04-12 not available (screener started Apr 2026)",
            "columns":  ["run_ts", "symbol", "setup", "score", "grade", "pump_score",
                         "direction", "price_entry", "funding", "oi_24h_pct",
                         "outcome_4h", "outcome_24h", "change_4h_pct", "change_24h_pct",
                         "channel_conf", "whale_flag"],
            "rows":     _count_csv(CAT_DIR / "news_signals.csv"),
        },
        "whale_positions": {
            "file":     None,
            "source":   "Binance /futures/data/topLongShortPositionRatio",
            "coverage": "no_coverage",
            "coverage_gap": "Binance top-trader L/S ratio endpoints reject startTime — only last 1 datapoint available (real-time)",
            "rows":     0,
        },
        "orderbook_imbalance": {
            "file":     None,
            "source":   "orderbook_imbalance.py (live orderbook)",
            "coverage": "no_coverage",
            "coverage_gap": "Real-time orderbook snapshot only — no historical depth data stored",
            "rows":     0,
        },
    }

    # Merge with base manifest
    manifest = {
        "generated_at":  datetime.utcnow().isoformat() + "Z",
        "period":        base.get("period", {"start": "2024-05-01", "end": "2026-05-27"}),
        "symbols":       symbols,
        "klines":        {
            tf: {
                **base.get("klines", {}).get(tf, {}),
                "symbol_detail": klines_detail.get(tf, {}),
            }
            for tf in ["5m", "15m", "1h"]
        },
        "funding":       base.get("funding", {}),
        "open_interest": base.get("open_interest", {}),
        "liquidations":  base.get("liquidations", {}),
        "causal_attribution": catalyst,
    }

    manifest_path.write_text(json.dumps(manifest, indent=2))
    log.info("Manifest written → %s", manifest_path)
    return manifest


def main():
    log.info("=== Finalize manifest ===")
    manifest = build_manifest()

    # Print summary
    log.info("\n=== Data coverage summary ===")
    for tf in ["5m", "15m", "1h"]:
        km = manifest["klines"].get(tf, {})
        log.info("  klines_%s: %d total rows", tf, km.get("total_rows", 0))

    for k, v in manifest.get("causal_attribution", {}).items():
        rows = v.get("rows", 0)
        cov = v.get("coverage", "unknown")
        log.info("  %-25s %4d rows  [%s]", k, rows, cov)


if __name__ == "__main__":
    main()

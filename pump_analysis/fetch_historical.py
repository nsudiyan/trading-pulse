#!/usr/bin/env python3
"""
fetch_historical.py — Загрузка исторических данных с Binance Futures

Загружает для top-30 USDT-perp символов:
  - klines: 5m, 15m, 1h (май 2024 – май 2026)
  - funding rate: 8h-гранулярность (те же 24 мес)
  - open interest: 1h-гранулярность
  - liquidations: только из liquidations.db (Bybit; Binance API требует ключ)

Сохраняет в pump_analysis/:
  klines_5m/SYMBOL.csv.gz, klines_15m/SYMBOL.csv.gz, klines_1h/SYMBOL.csv.gz,
  funding.csv, open_interest.csv, liquidations_summary.csv, data_manifest.json

NOTE: Binance /fapi/v1/allForceOrders "out of maintenance";
      /fapi/v1/forceOrders requires API key.
      Liquidation data comes only from the local liquidations.db (Bybit, Apr-May 2026).
"""

import csv
import gzip
import json
import logging
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fetch_historical")

BASE       = "https://fapi.binance.com"
BYBIT_BASE = "https://api.bybit.com"
OUT_DIR  = Path(__file__).parent          # pump_analysis/
REPO_DIR = OUT_DIR.parent                 # project root

START_MS = int(datetime(2024, 5, 1, tzinfo=timezone.utc).timestamp() * 1000)
END_MS   = int(datetime(2026, 5, 27, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)

KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume",
              "close_time", "quote_volume", "trades",
              "taker_buy_base", "taker_buy_quote", "ignore"]
KLINE_KEEP = ["open_time", "open", "high", "low", "close", "volume",
              "quote_volume", "taker_buy_quote"]

RATE_DELAY    = 0.15   # 6.6 req/s — safe for Binance public API (weight budget 2400/min)
BYBT_DELAY    = 0.12   # 8 req/s — safe for Bybit V5 public API
MAX_WORKERS   = 1      # sequential by default; raise to 2 only if rate limits not hit

_sess = requests.Session()
_sess.headers.update({"User-Agent": "PumpAnalysis-HistFetcher/1.0"})


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _get(path: str, params: dict = None, retries: int = 5) -> list | dict | None:
    url = BASE + path
    for attempt in range(retries):
        try:
            r = _sess.get(url, params=params, timeout=20)
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 30))
                log.warning("Rate-limited, sleeping %ds", retry_after)
                time.sleep(retry_after)
                continue
            if r.status_code == 400:
                # Symbol may not exist for that period
                return []
            r.raise_for_status()
            time.sleep(RATE_DELAY)
            return r.json()
        except requests.RequestException as e:
            wait = 2 ** attempt
            log.warning("Request error (%s) attempt %d/%d, retry in %ds", e, attempt + 1, retries, wait)
            time.sleep(wait)
    return None


# ── Symbol selection ───────────────────────────────────────────────────────────

def get_top30_symbols() -> list[str]:
    """Get top-30 USDT-perpetual symbols by 24h quote volume."""
    data = _get("/fapi/v1/ticker/24hr")
    if not data:
        raise RuntimeError("Cannot fetch ticker data from Binance")

    # Only keep USDT perps (exclude BUSD, index coins like XAUUSDT/XAGUSDT)
    EXCLUDE_PREFIXES = {"XAU", "XAG", "DEFI", "PRIV", "MID"}
    usdt = []
    for d in data:
        sym = d["symbol"]
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in EXCLUDE_PREFIXES or any(sym.startswith(p) for p in EXCLUDE_PREFIXES):
            continue
        usdt.append((sym, float(d.get("quoteVolume", 0))))

    usdt.sort(key=lambda x: x[1], reverse=True)
    # Also verify these symbols have data from our start period
    symbols = [s for s, _ in usdt[:40]]   # take 40 to have buffer
    log.info("Raw top-40 candidates: %s", symbols[:40])

    # Filter to those with exchange info (type=PERPETUAL)
    ei = _get("/fapi/v1/exchangeInfo")
    perpetuals = set()
    if ei and "symbols" in ei:
        for s in ei["symbols"]:
            if s.get("contractType") == "PERPETUAL" and s["symbol"].endswith("USDT"):
                perpetuals.add(s["symbol"])

    filtered = [s for s in symbols if s in perpetuals][:30]
    log.info("Top-30 perpetuals: %s", filtered)
    return filtered


# ── Klines downloader ──────────────────────────────────────────────────────────

def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list]:
    """Fetch all klines for a symbol/interval between start_ms and end_ms."""
    rows = []
    cursor = start_ms
    limit = 1500

    while cursor < end_ms:
        data = _get("/fapi/v1/klines", {
            "symbol":    symbol,
            "interval":  interval,
            "startTime": cursor,
            "endTime":   end_ms,
            "limit":     limit,
        })
        if not data:
            break
        rows.extend(data)
        if len(data) < limit:
            break
        # next batch starts from the last candle's close_time + 1ms
        cursor = int(data[-1][6]) + 1

    return rows


def save_klines(symbol: str, interval: str, rows: list[list], out_dir: Path) -> int:
    """Save klines rows to gzipped CSV. Returns number of rows written."""
    out_path = out_dir / f"{symbol}.csv.gz"
    if not rows:
        return 0

    # Map column indices
    col_idx = {c: i for i, c in enumerate(KLINE_COLS)}
    keep_idx = [col_idx[c] for c in KLINE_KEEP]

    with gzip.open(out_path, "wt", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(KLINE_KEEP)
        for row in rows:
            writer.writerow([row[i] for i in keep_idx])

    return len(rows)


def download_klines_for_symbol(symbol: str, interval: str, kline_dir: Path) -> dict:
    """Download klines for one symbol/interval. Returns manifest entry."""
    out_path = kline_dir / f"{symbol}.csv.gz"
    if out_path.exists():
        with gzip.open(out_path, "rt") as f:
            n = sum(1 for _ in f) - 1  # minus header
        log.info("  SKIP %s %s (exists, %d rows)", symbol, interval, n)
        return {"symbol": symbol, "interval": interval, "rows": n, "status": "cached"}

    log.info("  Fetching %s %s ...", symbol, interval)
    rows = fetch_klines(symbol, interval, START_MS, END_MS)
    n = save_klines(symbol, interval, rows, kline_dir)
    log.info("  → %s %s: %d candles saved", symbol, interval, n)
    return {"symbol": symbol, "interval": interval, "rows": n, "status": "downloaded"}


def download_klines_parallel(symbols: list[str], interval: str, kline_dir: Path,
                             max_workers: int = MAX_WORKERS) -> dict:
    """Download klines for all symbols in parallel. Returns combined kline_stats dict."""
    stats = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(download_klines_for_symbol, sym, interval, kline_dir): sym
            for sym in symbols
        }
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                info = fut.result()
                stats[f"{sym}_{interval}"] = info
            except Exception as e:
                log.error("  FAILED %s %s: %s", sym, interval, e)
                stats[f"{sym}_{interval}"] = {"symbol": sym, "interval": interval, "rows": 0, "status": "error"}
    return stats


# ── Funding Rate downloader ────────────────────────────────────────────────────

def fetch_all_funding(symbols: list[str]) -> list[dict]:
    """Fetch funding rate history for all symbols."""
    log.info("Fetching funding rate history for %d symbols ...", len(symbols))
    all_rows = []

    for sym in symbols:
        cursor = START_MS
        limit = 1000
        sym_rows = 0

        while cursor <= END_MS:
            data = _get("/fapi/v1/fundingRate", {
                "symbol":    sym,
                "startTime": cursor,
                "endTime":   END_MS,
                "limit":     limit,
            })
            if not data:
                break
            for rec in data:
                all_rows.append({
                    "symbol":      rec["symbol"],
                    "fundingTime": rec["fundingTime"],
                    "fundingRate": rec["fundingRate"],
                })
                sym_rows += 1
            if len(data) < limit:
                break
            cursor = int(data[-1]["fundingTime"]) + 1

        log.info("  %s: %d funding records", sym, sym_rows)

    return all_rows


def save_funding(rows: list[dict], out_path: Path) -> int:
    if not rows:
        return 0
    rows.sort(key=lambda r: (r["symbol"], r["fundingTime"]))
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "fundingTime", "fundingRate"])
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


# ── Open Interest downloader (Bybit V5) ───────────────────────────────────────
# Binance /futures/data/openInterestHist does NOT support startTime — only last 21 days.
# Bybit /v5/market/open-interest supports startTime/endTime with full 2-year history.

def _get_bybit(url: str, params: dict = None, retries: int = 5) -> dict | None:
    for attempt in range(retries):
        try:
            r = _sess.get(url, params=params, timeout=25)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", 30)))
                continue
            r.raise_for_status()
            time.sleep(BYBT_DELAY)
            return r.json()
        except requests.RequestException as e:
            time.sleep(2 ** attempt)
            log.debug("Bybit error (%s) attempt %d", e, attempt + 1)
    return None


def fetch_oi_bybit(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Fetch 1h OI history from Bybit for a single symbol, full date range."""
    rows: list[dict] = []
    chunk_ms = 198 * 3600 * 1000  # 198-hour chunks → ≤198 rows per request at 1h
    cursor = start_ms

    while cursor < end_ms:
        chunk_end = min(cursor + chunk_ms, end_ms)
        data = _get_bybit(f"{BYBIT_BASE}/v5/market/open-interest", {
            "category":    "linear",
            "symbol":      symbol,
            "intervalTime": "1h",
            "startTime":   cursor,
            "endTime":     chunk_end,
            "limit":       200,
        })
        if not data or data.get("retCode") != 0:
            cursor = chunk_end + 1
            continue
        lst = data.get("result", {}).get("list", [])
        for rec in lst:
            ts = int(rec["timestamp"])
            if start_ms <= ts <= end_ms:
                rows.append({"symbol": symbol, "timestamp": ts,
                             "openInterest": rec["openInterest"]})
        cursor = chunk_end + 1

    # Sort and deduplicate
    rows.sort(key=lambda r: r["timestamp"])
    seen: set[int] = set()
    deduped = []
    for r in rows:
        if r["timestamp"] not in seen:
            seen.add(r["timestamp"])
            deduped.append(r)
    return deduped


def fetch_all_oi(symbols: list[str]) -> list[dict]:
    """Fetch OI (1h) from Bybit for all symbols."""
    log.info("Fetching OI (1h) from Bybit for %d symbols ...", len(symbols))
    all_rows = []
    for sym in symbols:
        rows = fetch_oi_bybit(sym, START_MS, END_MS)
        log.info("  %s: %d OI records", sym, len(rows))
        all_rows.extend(rows)
    return all_rows


def save_oi(rows: list[dict], out_path: Path) -> int:
    if not rows:
        return 0
    rows.sort(key=lambda r: (r["symbol"], r["timestamp"]))
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "timestamp", "openInterest"])
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


# ── Liquidations ───────────────────────────────────────────────────────────────

def load_liquidations_db(db_path: Path) -> list[dict]:
    """Load existing liquidations from SQLite DB."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT ts, symbol, side, price, qty, usd FROM liquidations ORDER BY ts")
    rows = [{"ts": r[0], "symbol": r[1], "side": r[2],
             "price": r[3], "qty": r[4], "usd": r[5]} for r in cursor.fetchall()]
    conn.close()
    return rows


def fetch_binance_liquidations(symbols: list[str], start_ms: int, end_ms: int) -> list[dict]:
    """
    Binance /fapi/v1/allForceOrders is 'out of maintenance'.
    /fapi/v1/forceOrders requires an API key.
    Returns empty list; liquidations come only from liquidations.db.
    """
    log.info("Binance liquidation API unavailable (requires key or deprecated). Using DB only.")
    return []


def save_liquidations_summary(rows: list[dict], out_path: Path) -> int:
    if not rows:
        return 0
    rows.sort(key=lambda r: (r["symbol"], r["ts"]))
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ts", "symbol", "side", "price", "qty", "usd", "source"])
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


# ── Manifest ───────────────────────────────────────────────────────────────────

def build_manifest(symbols: list[str], kline_stats: dict, funding_n: int,
                   oi_n: int, liq_n: int, liq_coverage: dict) -> dict:
    return {
        "generated_at":   datetime.utcnow().isoformat() + "Z",
        "period":         {"start": "2024-05-01", "end": "2026-05-27"},
        "source":         "Binance Futures Public API (fapi.binance.com)",
        "symbols":        symbols,
        "klines": {
            "5m": {
                "files": [f"klines_5m/{s}.csv.gz" for s in symbols],
                "total_rows": sum(kline_stats.get(f"{s}_5m", {}).get("rows", 0) for s in symbols),
                "columns": KLINE_KEEP,
            },
            "15m": {
                "files": [f"klines_15m/{s}.csv.gz" for s in symbols],
                "total_rows": sum(kline_stats.get(f"{s}_15m", {}).get("rows", 0) for s in symbols),
                "columns": KLINE_KEEP,
            },
            "1h": {
                "files": [f"klines_1h/{s}.csv.gz" for s in symbols],
                "total_rows": sum(kline_stats.get(f"{s}_1h", {}).get("rows", 0) for s in symbols),
                "columns": KLINE_KEEP,
            },
        },
        "funding": {
            "file":        "funding.csv",
            "total_rows":  funding_n,
            "granularity": "8h",
            "columns":     ["symbol", "fundingTime", "fundingRate"],
        },
        "open_interest": {
            "file":        "open_interest.csv",
            "source":      "Bybit V5 /v5/market/open-interest (Binance OI hist has no date filter — only last 21 days)",
            "total_rows":  oi_n,
            "granularity": "1h",
            "columns":     ["symbol", "timestamp", "openInterest"],
        },
        "liquidations": {
            "file":             "liquidations_summary.csv",
            "total_rows":       liq_n,
            "coverage_note":    liq_coverage,
            "columns":          ["ts", "symbol", "side", "price", "qty", "usd", "source"],
        },
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log.info("=== AVEVA-58: Historical data fetcher starting ===")
    log.info("Period: 2024-05-01 → 2026-05-27  |  Output: %s", OUT_DIR)

    # 1. Get top-30 symbols
    symbols = get_top30_symbols()
    log.info("Symbols: %s", symbols)

    kline_stats = {}

    # 2. Klines — 5m (parallel by symbol)
    klines_5m_dir = OUT_DIR / "klines_5m"
    klines_5m_dir.mkdir(exist_ok=True)
    log.info("\n--- Klines 5m (%d symbols, %d workers) ---", len(symbols), MAX_WORKERS)
    kline_stats.update(download_klines_parallel(symbols, "5m", klines_5m_dir))

    # 3. Klines — 15m
    klines_15m_dir = OUT_DIR / "klines_15m"
    klines_15m_dir.mkdir(exist_ok=True)
    log.info("\n--- Klines 15m (%d symbols, %d workers) ---", len(symbols), MAX_WORKERS)
    kline_stats.update(download_klines_parallel(symbols, "15m", klines_15m_dir))

    # 4. Klines — 1h
    klines_1h_dir = OUT_DIR / "klines_1h"
    klines_1h_dir.mkdir(exist_ok=True)
    log.info("\n--- Klines 1h (%d symbols, %d workers) ---", len(symbols), MAX_WORKERS)
    kline_stats.update(download_klines_parallel(symbols, "1h", klines_1h_dir))

    # 5. Funding rates
    funding_path = OUT_DIR / "funding.csv"
    if funding_path.exists():
        with open(funding_path) as f:
            funding_n = sum(1 for _ in f) - 1
        log.info("Funding: SKIP (exists, %d rows)", funding_n)
    else:
        log.info("\n--- Funding rates ---")
        funding_rows = fetch_all_funding(symbols)
        funding_n = save_funding(funding_rows, funding_path)
        log.info("Funding: %d rows saved", funding_n)

    # 6. Open Interest
    oi_path = OUT_DIR / "open_interest.csv"
    if oi_path.exists():
        with open(oi_path) as f:
            oi_n = sum(1 for _ in f) - 1
        log.info("OI: SKIP (exists, %d rows)", oi_n)
    else:
        log.info("\n--- Open Interest (1h) ---")
        oi_rows = fetch_all_oi(symbols)
        oi_n = save_oi(oi_rows, oi_path)
        log.info("OI: %d rows saved", oi_n)

    # 7. Liquidations
    liq_path = OUT_DIR / "liquidations_summary.csv"
    db_path  = REPO_DIR / "liquidations.db"

    if liq_path.exists():
        with open(liq_path) as f:
            liq_n = sum(1 for _ in f) - 1
        log.info("Liquidations: SKIP (exists, %d rows)", liq_n)
        liq_coverage = {"note": "cached"}
    else:
        log.info("\n--- Liquidations ---")
        # Load from SQLite DB (covers ~Apr-May 2026)
        db_rows = load_liquidations_db(db_path)
        db_syms = set(r["symbol"] for r in db_rows)
        db_ts_min = min((r["ts"] for r in db_rows), default=None)
        db_ts_max = max((r["ts"] for r in db_rows), default=None)
        log.info("  SQLite DB: %d rows, %d symbols, ts=[%s → %s]",
                 len(db_rows),
                 len(db_syms),
                 datetime.fromtimestamp(db_ts_min / 1000).strftime("%Y-%m-%d") if db_ts_min else "N/A",
                 datetime.fromtimestamp(db_ts_max / 1000).strftime("%Y-%m-%d") if db_ts_max else "N/A")

        # Supplement with Binance API (only last ~30 days available)
        api_rows = fetch_binance_liquidations(symbols, START_MS, END_MS)
        log.info("  Binance API: %d rows", len(api_rows))

        # Merge (deduplicate by ts+symbol+side)
        seen = set()
        merged = []
        for r in db_rows + api_rows:
            key = (r["ts"], r["symbol"], r["side"])
            if key not in seen:
                seen.add(key)
                merged.append(r)

        liq_n = save_liquidations_summary(merged, liq_path)
        log.info("  Merged: %d unique liquidation events", liq_n)

        liq_coverage = {
            "sqlite_db_rows": len(db_rows),
            "sqlite_db_period": f"{datetime.fromtimestamp(db_ts_min/1000).strftime('%Y-%m-%d') if db_ts_min else 'N/A'} → {datetime.fromtimestamp(db_ts_max/1000).strftime('%Y-%m-%d') if db_ts_max else 'N/A'}",
            "binance_api_rows": len(api_rows),
            "binance_api_note": "Only last ~30 days available via public forceOrders endpoint",
            "gap": "2024-05-01 → 2026-04-14 liquidation history not available via public API",
        }

    # 8. Write manifest
    manifest = build_manifest(symbols, kline_stats, funding_n, oi_n, liq_n, liq_coverage)
    manifest_path = OUT_DIR / "data_manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    log.info("\nManifest written: %s", manifest_path)

    # Summary
    total_kline_rows = sum(v.get("rows", 0) for v in kline_stats.values())
    log.info("\n=== DONE ===")
    log.info("  Klines total rows:  %d", total_kline_rows)
    log.info("  Funding rows:       %d", funding_n)
    log.info("  OI rows:            %d", oi_n)
    log.info("  Liquidation rows:   %d", liq_n)
    log.info("  Manifest:           %s", manifest_path)
    log.info("  Output dir:         %s", OUT_DIR)


if __name__ == "__main__":
    main()

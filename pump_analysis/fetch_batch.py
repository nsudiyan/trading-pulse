#!/usr/bin/env python3
"""
pump_analysis/fetch_batch.py
AVEC-69.2 / AVEVA-71

Download historical data for ~69 new USDT-perp symbols in batches of 25 with
checkpoints. Skips symbols already in data_manifest.json.

Per-symbol timeout: 30 min (klines are the bottleneck; funding/OI are fast).
Failures logged to pump_analysis/failed_symbols.json.
Appends to existing funding.csv and open_interest.csv — no re-downloads.
"""

import csv
import gzip
import json
import logging
import multiprocessing
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fetch_batch")

# ─── Paths ────────────────────────────────────────────────────────────────────

OUT_DIR          = Path(__file__).parent          # pump_analysis/
MANIFEST_PATH    = OUT_DIR / "data_manifest.json"
SYMBOLS_NEW_PATH = OUT_DIR / "symbols_new.json"
FAILED_PATH      = OUT_DIR / "failed_symbols.json"

FUNDING_PATH = OUT_DIR / "funding.csv"
OI_PATH      = OUT_DIR / "open_interest.csv"

KLINES_5M_DIR  = OUT_DIR / "klines_5m"
KLINES_15M_DIR = OUT_DIR / "klines_15m"
KLINES_1H_DIR  = OUT_DIR / "klines_1h"

# ─── Constants ────────────────────────────────────────────────────────────────

BASE       = "https://fapi.binance.com"
BYBIT_BASE = "https://api.bybit.com"

START_MS = int(datetime(2024, 5, 1, tzinfo=timezone.utc).timestamp() * 1000)
END_MS   = int(datetime(2026, 5, 27, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)
PERIOD_START = "2024-05-01"
PERIOD_END   = "2026-05-27"

KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume",
              "close_time", "quote_volume", "trades",
              "taker_buy_base", "taker_buy_quote", "ignore"]
KLINE_KEEP = ["open_time", "open", "high", "low", "close", "volume",
              "quote_volume", "taker_buy_quote"]

RATE_DELAY     = 0.15   # Binance: ~6.6 req/s
BYBT_DELAY     = 0.12   # Bybit: ~8 req/s
BATCH_SIZE     = 25
SYMBOL_TIMEOUT = 30 * 60  # 30 min

_sess = requests.Session()
_sess.headers.update({"User-Agent": "PumpAnalysis-BatchFetcher/1.0"})


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def _get(path: str, params: dict = None, retries: int = 5):
    url = BASE + path
    for attempt in range(retries):
        try:
            r = _sess.get(url, params=params, timeout=30)
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 30))
                log.warning("Rate-limited (Binance), sleeping %ds", retry_after)
                time.sleep(retry_after)
                continue
            if r.status_code == 400:
                return []   # symbol doesn't exist for this period
            r.raise_for_status()
            time.sleep(RATE_DELAY)
            return r.json()
        except requests.RequestException as e:
            wait = 2 ** attempt
            log.warning("Binance error (%s) attempt %d/%d, retry in %ds", e, attempt + 1, retries, wait)
            time.sleep(wait)
    return None


def _get_bybit(url: str, params: dict = None, retries: int = 5):
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


# ─── Klines ───────────────────────────────────────────────────────────────────

def fetch_klines(symbol: str, interval: str) -> list:
    rows = []
    cursor = START_MS
    limit = 1500
    while cursor < END_MS:
        data = _get("/fapi/v1/klines", {
            "symbol": symbol, "interval": interval,
            "startTime": cursor, "endTime": END_MS, "limit": limit,
        })
        if not data:
            break
        rows.extend(data)
        if len(data) < limit:
            break
        cursor = int(data[-1][6]) + 1
    return rows


def save_klines(symbol: str, interval: str, rows: list, out_dir: Path) -> int:
    out_path = out_dir / f"{symbol}.csv.gz"
    if not rows:
        return 0
    col_idx = {c: i for i, c in enumerate(KLINE_COLS)}
    keep_idx = [col_idx[c] for c in KLINE_KEEP]
    with gzip.open(out_path, "wt", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(KLINE_KEEP)
        for row in rows:
            writer.writerow([row[i] for i in keep_idx])
    return len(rows)


def download_klines_symbol(symbol: str, interval: str, kline_dir: Path) -> dict:
    """Download klines for one symbol/interval. SKIP if file exists."""
    kline_dir.mkdir(exist_ok=True)
    out_path = kline_dir / f"{symbol}.csv.gz"

    if out_path.exists():
        with gzip.open(out_path, "rt") as f:
            n = sum(1 for _ in f) - 1
        # Determine actual period from file
        try:
            with gzip.open(out_path, "rt") as f:
                reader = csv.DictReader(f)
                times = [int(row["open_time"]) for row in reader]
            p_start = datetime.fromtimestamp(min(times)/1000, tz=timezone.utc).strftime("%Y-%m-%d") if times else "N/A"
            p_end   = datetime.fromtimestamp(max(times)/1000, tz=timezone.utc).strftime("%Y-%m-%d") if times else "N/A"
        except Exception:
            p_start, p_end = PERIOD_START, PERIOD_END
        log.info("  SKIP %s %s (exists, %d rows)", symbol, interval, n)
        return {"rows": n, "period_start": p_start, "period_end": p_end, "status": "cached"}

    log.info("  Fetching %s %s ...", symbol, interval)
    rows = fetch_klines(symbol, interval)
    n = save_klines(symbol, interval, rows, kline_dir)
    if rows:
        p_start = datetime.fromtimestamp(int(rows[0][0])/1000, tz=timezone.utc).strftime("%Y-%m-%d")
        p_end   = datetime.fromtimestamp(int(rows[-1][0])/1000, tz=timezone.utc).strftime("%Y-%m-%d")
    else:
        p_start = p_end = "N/A"
    log.info("  → %s %s: %d candles (%s → %s)", symbol, interval, n, p_start, p_end)
    return {"rows": n, "period_start": p_start, "period_end": p_end, "status": "downloaded"}


def download_all_klines(symbol: str) -> dict:
    """Download 5m, 15m, 1h for one symbol. Returns per-interval detail."""
    detail = {}
    for interval, kline_dir in [("5m", KLINES_5M_DIR), ("15m", KLINES_15M_DIR), ("1h", KLINES_1H_DIR)]:
        detail[interval] = download_klines_symbol(symbol, interval, kline_dir)
    return detail


# ─── Funding rate ─────────────────────────────────────────────────────────────

def get_symbols_in_csv(csv_path: Path, col: str = "symbol") -> set:
    """Return set of symbols already present in a CSV file."""
    if not csv_path.exists():
        return set()
    syms = set()
    try:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                syms.add(row[col])
    except Exception as e:
        log.warning("Cannot read %s: %s", csv_path, e)
    return syms


def fetch_funding(symbol: str) -> list:
    rows = []
    cursor = START_MS
    limit = 1000
    while cursor <= END_MS:
        data = _get("/fapi/v1/fundingRate", {
            "symbol": symbol, "startTime": cursor, "endTime": END_MS, "limit": limit,
        })
        if not data:
            break
        for rec in data:
            rows.append({
                "symbol":      rec["symbol"],
                "fundingTime": rec["fundingTime"],
                "fundingRate": rec["fundingRate"],
            })
        if len(data) < limit:
            break
        cursor = int(data[-1]["fundingTime"]) + 1
    return rows


def append_funding(symbol: str) -> int:
    """Fetch and append funding rows for symbol. Skips if already in CSV."""
    already = get_symbols_in_csv(FUNDING_PATH, "symbol")
    if symbol in already:
        log.info("  SKIP funding %s (already in CSV)", symbol)
        return 0
    rows = fetch_funding(symbol)
    if not rows:
        log.info("  %s: no funding data", symbol)
        return 0
    rows.sort(key=lambda r: r["fundingTime"])
    write_header = not FUNDING_PATH.exists()
    with open(FUNDING_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "fundingTime", "fundingRate"])
        if write_header:
            writer.writeheader()
        writer.writerows(rows)
    log.info("  %s: %d funding records appended", symbol, len(rows))
    return len(rows)


# ─── Open Interest (Bybit V5) ─────────────────────────────────────────────────

def fetch_oi(symbol: str) -> list:
    rows = []
    chunk_ms = 198 * 3600 * 1000   # 198 h → ≤198 rows per request at 1h granularity
    cursor = START_MS
    while cursor < END_MS:
        chunk_end = min(cursor + chunk_ms, END_MS)
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
        for rec in data.get("result", {}).get("list", []):
            ts = int(rec["timestamp"])
            if START_MS <= ts <= END_MS:
                rows.append({"symbol": symbol, "timestamp": ts, "openInterest": rec["openInterest"]})
        cursor = chunk_end + 1
    # Deduplicate
    rows.sort(key=lambda r: r["timestamp"])
    seen, deduped = set(), []
    for r in rows:
        if r["timestamp"] not in seen:
            seen.add(r["timestamp"])
            deduped.append(r)
    return deduped


def append_oi(symbol: str) -> int:
    """Fetch and append OI rows for symbol. Skips if already in CSV."""
    already = get_symbols_in_csv(OI_PATH, "symbol")
    if symbol in already:
        log.info("  SKIP OI %s (already in CSV)", symbol)
        return 0
    rows = fetch_oi(symbol)
    if not rows:
        log.info("  %s: no OI data (may not be on Bybit)", symbol)
        return 0
    rows.sort(key=lambda r: r["timestamp"])
    write_header = not OI_PATH.exists()
    with open(OI_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "timestamp", "openInterest"])
        if write_header:
            writer.writeheader()
        writer.writerows(rows)
    log.info("  %s: %d OI records appended", symbol, len(rows))
    return len(rows)


# ─── Per-symbol worker (runs in subprocess with real timeout) ─────────────────
# NOTE: ThreadPoolExecutor cannot kill stuck threads; multiprocessing.Process
# can be .terminate()d, which is the only reliable way to enforce a timeout
# when the worker is blocked on network I/O.

def _subprocess_worker(symbol: str, result_queue: multiprocessing.Queue):
    """Worker that runs in a child process. Puts result dict into queue."""
    # Re-configure logging in the child process
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    t0 = time.time()
    log = logging.getLogger("fetch_batch")
    log.info("▶ %s starting ...", symbol)
    try:
        kline_detail = download_all_klines(symbol)
        funding_n    = append_funding(symbol)
        oi_n         = append_oi(symbol)
        elapsed = time.time() - t0
        log.info("✓ %s done in %.1fs  klines_5m=%d funding=%d OI=%d",
                 symbol, elapsed,
                 kline_detail.get("5m", {}).get("rows", 0), funding_n, oi_n)
        result_queue.put({
            "symbol":       symbol,
            "kline_detail": kline_detail,
            "funding_n":    funding_n,
            "oi_n":         oi_n,
        })
    except Exception as e:
        log.error("ERROR in %s: %s", symbol, e)


def process_with_timeout(symbol: str) -> tuple[dict, bool]:
    """Run download in a child process with SYMBOL_TIMEOUT. Returns (stats, ok)."""
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()
    p = ctx.Process(target=_subprocess_worker, args=(symbol, result_queue))
    p.start()
    p.join(timeout=SYMBOL_TIMEOUT)

    if p.is_alive():
        log.error("TIMEOUT %s: exceeded %d min, terminating", symbol, SYMBOL_TIMEOUT // 60)
        p.terminate()
        p.join(timeout=10)
        if p.is_alive():
            p.kill()
        return {}, False

    if p.exitcode != 0:
        log.error("ERROR %s: child exited with code %s", symbol, p.exitcode)
        return {}, False

    if result_queue.empty():
        log.error("ERROR %s: child exited OK but no result in queue", symbol)
        return {}, False

    return result_queue.get(), True


# ─── Manifest helpers ─────────────────────────────────────────────────────────

def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH) as f:
            return json.load(f)
    return {}


def save_manifest(m: dict):
    with open(MANIFEST_PATH, "w") as f:
        json.dump(m, f, indent=2)


def checkpoint_manifest(batch_done: list, batch_stats: dict[str, dict]):
    """
    Append batch_done to manifest.symbols.
    Update klines.{5m,15m,1h}.symbol_detail with per-symbol row counts.
    Re-count total_rows and symbol_count from symbol_detail.
    """
    m = load_manifest()

    # Symbols list
    existing_syms = m.get("symbols", [])
    added = [s for s in batch_done if s not in existing_syms]
    m["symbols"] = existing_syms + added
    m["generated_at"] = datetime.utcnow().isoformat() + "Z"
    m.setdefault("batch_checkpoint", {})
    m["batch_checkpoint"]["last_batch_at"]  = datetime.utcnow().isoformat() + "Z"
    m["batch_checkpoint"]["total_symbols"]  = len(m["symbols"])
    m["batch_checkpoint"]["batches_run"]    = m["batch_checkpoint"].get("batches_run", 0) + 1

    # Klines
    m.setdefault("klines", {})
    for interval in ("5m", "15m", "1h"):
        m["klines"].setdefault(interval, {
            "symbol_detail": {},
            "total_rows": 0,
            "symbol_count": 0,
        })
        sym_detail = m["klines"][interval].setdefault("symbol_detail", {})
        for sym in batch_done:
            kd = batch_stats.get(sym, {}).get("kline_detail", {}).get(interval, {})
            if kd:
                sym_detail[sym] = {
                    "rows":         kd.get("rows", 0),
                    "period_start": kd.get("period_start", "N/A"),
                    "period_end":   kd.get("period_end", "N/A"),
                }
        m["klines"][interval]["total_rows"]   = sum(v.get("rows", 0) for v in sym_detail.values())
        m["klines"][interval]["symbol_count"] = len(sym_detail)

    # Funding / OI totals
    for csv_path, manifest_key in [(FUNDING_PATH, "funding"), (OI_PATH, "open_interest")]:
        if csv_path.exists():
            with open(csv_path) as f:
                n = sum(1 for _ in f) - 1
            m.setdefault(manifest_key, {})["rows"] = n

    save_manifest(m)
    log.info("Checkpoint saved: %d total symbols in manifest", len(m["symbols"]))


# ─── Failed symbols tracker ───────────────────────────────────────────────────

def load_failed() -> dict:
    if FAILED_PATH.exists():
        with open(FAILED_PATH) as f:
            return json.load(f)
    return {"failed": []}


def log_failed(symbol: str, reason: str):
    state = load_failed()
    # Avoid duplicates
    if not any(s["symbol"] == symbol for s in state["failed"]):
        state["failed"].append({
            "symbol":    symbol,
            "failed_at": datetime.utcnow().isoformat() + "Z",
            "reason":    reason,
        })
    with open(FAILED_PATH, "w") as f:
        json.dump(state, f, indent=2)


# ─── Symbol selection ─────────────────────────────────────────────────────────

def get_pending_symbols() -> list:
    """Return new symbols not yet in manifest.symbols."""
    with open(SYMBOLS_NEW_PATH) as f:
        d = json.load(f)
    new_syms = [s["symbol"] for s in d["symbols"]]

    manifest = load_manifest()
    done_set = set(manifest.get("symbols", []))

    failed_set = {s["symbol"] for s in load_failed().get("failed", [])}

    pending = [s for s in new_syms if s not in done_set]
    log.info("New symbols: %d  |  Already in manifest: %d  |  Previously failed: %d  |  Pending: %d",
             len(new_syms), len(done_set & set(new_syms)),
             len(failed_set & set(pending)), len(pending))
    return pending


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("=== AVEVA-71: Batch Historical Data Fetcher ===")
    log.info("Period: %s → %s  |  Batch size: %d  |  Timeout: %d min/symbol",
             PERIOD_START, PERIOD_END, BATCH_SIZE, SYMBOL_TIMEOUT // 60)

    pending = get_pending_symbols()
    if not pending:
        log.info("Nothing to fetch. All symbols already in manifest.")
        return

    # Split into batches
    batches = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
    log.info("Batches: %d  sizes=%s", len(batches), [len(b) for b in batches])

    failed_state = load_failed()
    prev_failed = {s["symbol"] for s in failed_state["failed"]}

    for batch_idx, batch in enumerate(batches, 1):
        log.info("\n\n══════ BATCH %d/%d  (%d symbols) ══════", batch_idx, len(batches), len(batch))

        batch_done  = []   # symbols successfully completed this batch
        batch_stats = {}   # sym → {kline_detail, funding_n, oi_n}

        for symbol in batch:
            if symbol in prev_failed:
                log.info("SKIP (previously failed): %s", symbol)
                continue

            stats, ok = process_with_timeout(symbol)

            if ok:
                batch_done.append(symbol)
                batch_stats[symbol] = stats
            else:
                reason = "timeout" if True else "error"
                log_failed(symbol, reason)
                prev_failed.add(symbol)
                log.warning("  → %s logged to failed_symbols.json", symbol)

        # Checkpoint after each batch
        if batch_done:
            checkpoint_manifest(batch_done, batch_stats)
            log.info("Batch %d checkpoint: %d/%d symbols OK", batch_idx, len(batch_done), len(batch))
        else:
            log.warning("Batch %d: 0 symbols completed", batch_idx)

    # Final summary
    manifest = load_manifest()
    total = len(manifest.get("symbols", []))
    failed = load_failed()
    failed_n = len(failed.get("failed", []))

    log.info("\n=== DONE ===")
    log.info("  Manifest symbols:  %d", total)
    log.info("  Failed symbols:    %d", failed_n)
    if failed_n:
        log.info("  Failed list: %s", [s["symbol"] for s in failed["failed"]])
    log.info("  Manifest:          %s", MANIFEST_PATH)
    log.info("  Failed log:        %s", FAILED_PATH)


if __name__ == "__main__":
    main()

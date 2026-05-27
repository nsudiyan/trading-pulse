#!/usr/bin/env python3
"""
fetch_remaining.py — Fetch only missing data for existing 31 symbols.
Targets the EXACT symbols already in klines_5m/ (avoids top-30 re-selection).
Fetches: OI (Bybit 1h) + liquidations from DB + writes updated manifest.
"""
import csv, gzip, json, logging, sqlite3, time
from datetime import datetime, timezone
from pathlib import Path
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("fetch_remaining")

BYBIT_BASE = "https://api.bybit.com"
OUT_DIR    = Path(__file__).parent
REPO_DIR   = OUT_DIR.parent
START_MS   = int(datetime(2024, 5, 1, tzinfo=timezone.utc).timestamp() * 1000)
END_MS     = int(datetime(2026, 5, 27, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)
BYBT_DELAY = 0.12

_sess = requests.Session()
_sess.headers.update({"User-Agent": "PumpAnalysis-HistFetcher/1.0"})


def _get_bybit(url, params=None, retries=5):
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


def fetch_oi_bybit(symbol, start_ms, end_ms):
    rows = []
    chunk_ms = 198 * 3600 * 1000
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(cursor + chunk_ms, end_ms)
        data = _get_bybit(f"{BYBIT_BASE}/v5/market/open-interest", {
            "category": "linear", "symbol": symbol, "intervalTime": "1h",
            "startTime": cursor, "endTime": chunk_end, "limit": 200,
        })
        if not data or data.get("retCode") != 0:
            cursor = chunk_end + 1
            continue
        lst = data.get("result", {}).get("list", [])
        for rec in lst:
            ts = int(rec["timestamp"])
            if start_ms <= ts <= end_ms:
                rows.append({"symbol": symbol, "timestamp": ts, "openInterest": rec["openInterest"]})
        cursor = chunk_end + 1
    rows.sort(key=lambda r: r["timestamp"])
    seen, deduped = set(), []
    for r in rows:
        if r["timestamp"] not in seen:
            seen.add(r["timestamp"])
            deduped.append(r)
    return deduped


def main():
    # Derive symbol list from actual klines_5m files
    symbols = sorted(p.name.removesuffix(".csv.gz") for p in (OUT_DIR / "klines_5m").glob("*.csv.gz"))
    log.info("Symbols from klines_5m: %d — %s", len(symbols), symbols)

    # 1. OI
    oi_path = OUT_DIR / "open_interest.csv"
    if oi_path.exists():
        with open(oi_path) as f:
            oi_n = sum(1 for _ in f) - 1
        log.info("OI: SKIP (cached %d rows)", oi_n)
    else:
        log.info("Fetching OI (1h) from Bybit for %d symbols ...", len(symbols))
        all_rows = []
        for sym in symbols:
            rows = fetch_oi_bybit(sym, START_MS, END_MS)
            log.info("  %s: %d OI records", sym, len(rows))
            all_rows.extend(rows)
        all_rows.sort(key=lambda r: (r["symbol"], r["timestamp"]))
        with open(oi_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["symbol", "timestamp", "openInterest"])
            w.writeheader()
            w.writerows(all_rows)
        oi_n = len(all_rows)
        log.info("OI: %d rows saved", oi_n)

    # 2. Liquidations
    liq_path = OUT_DIR / "liquidations_summary.csv"
    if liq_path.exists():
        with open(liq_path) as f:
            liq_n = sum(1 for _ in f) - 1
        log.info("Liquidations: SKIP (cached %d rows)", liq_n)
    else:
        db_path = REPO_DIR / "liquidations.db"
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT ts, symbol, side, price, qty, usd FROM liquidations ORDER BY ts")
        rows = [{"ts": r[0], "symbol": r[1], "side": r[2],
                 "price": r[3], "qty": r[4], "usd": r[5], "source": "liquidations_db"}
                for r in cur.fetchall()]
        conn.close()
        tmin = min(r["ts"] for r in rows) if rows else None
        tmax = max(r["ts"] for r in rows) if rows else None
        with open(liq_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["ts", "symbol", "side", "price", "qty", "usd", "source"])
            w.writeheader()
            w.writerows(rows)
        liq_n = len(rows)
        log.info("Liquidations: %d rows saved (%s → %s)",
                 liq_n,
                 datetime.fromtimestamp(tmin/1000).strftime("%Y-%m-%d") if tmin else "N/A",
                 datetime.fromtimestamp(tmax/1000).strftime("%Y-%m-%d") if tmax else "N/A")

    log.info("=== Remaining data complete. Running finalize_manifest.py ===")


if __name__ == "__main__":
    main()

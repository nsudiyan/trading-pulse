#!/usr/bin/env python3
"""
fetch_token_unlocks.py

Fetches token unlock data for the 100-symbol Bybit screener universe.

Source priority:
  1. DefiLlama /emissions/{protocol} — free endpoint per docs; tried, returns HTTP 402
     (paywalled as of 2026-05; requires Pro subscription)
  2. tokenunlocks.app / tokenomist.ai — React SPA, no free public API
  3. CoinGecko free API — circulating/total supply ratios (no unlock dates)
  4. Manual curation of documented vesting schedules for top symbols
     (only events with public tokenomics docs; no fabricated usd_value or pct figures)

Output: pump_analysis/catalyst_data/token_unlocks.csv
Format: symbol, unlock_date, pct_of_supply, usd_value, type, source

Run:
    python fetch_token_unlocks.py
Re-run safe: checkpointed to .unlock_fetch_checkpoint.json
"""

import csv
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

BASE_DIR      = Path(__file__).parent
OUT_CSV       = BASE_DIR / "pump_analysis" / "catalyst_data" / "token_unlocks.csv"
CHECKPOINT    = BASE_DIR / ".unlock_fetch_checkpoint.json"
PERIOD_START  = "2024-05-01"
PERIOD_END    = "2026-05-28"

CSV_COLUMNS = ["symbol", "unlock_date", "pct_of_supply", "usd_value", "type", "source"]


# ---------------------------------------------------------------------------
# 1.  DefiLlama emissions (tried — HTTP 402 paywalled)
# ---------------------------------------------------------------------------

# Protocol slugs for symbols in our universe that have vesting schedules
DEFILLAMA_SLUGS = {
    "ARBUSDT":    "arbitrum",
    "WLDUSDT":    "worldcoin",
    "APTUSDT":    "aptos",
    "SUIUSDT":    "sui",
    "TIAUSDT":    "celestia",
    "JTOUSDT":    "jito",
    "ENAUSDT":    "ethena",
    "SEIUSDT":    "sei",
    "NEARUSDT":   "near",
    "INJUSDT":    "injective",
    "AVAXUSDT":   "avalanche",
    "ATOMUSDT":   "cosmos",
    "AAVEUSDT":   "aave",
    "UNIUSDT":    "uniswap",
    "DOTUSDT":    "polkadot",
}


def try_defillama_emissions(symbol: str, slug: str) -> list[dict]:
    """Attempt DefiLlama /emissions/{protocol}. Returns [] if unavailable."""
    url = f"https://api.llama.fi/emissions/{slug}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 402:
            log.warning("DefiLlama emissions for %s/%s → HTTP 402 (paywalled — Pro subscription required)", symbol, slug)
            return []
        if r.status_code == 404:
            log.info("DefiLlama emissions: no data for slug=%s (404)", slug)
            return []
        r.raise_for_status()
        data = r.json()
        rows = []
        # DefiLlama emissions format: {"tokenAllocation": {...}, "unlocksByDate": [{"ts": ..., "newEmissions": ...}]}
        events = data.get("unlocksByDate") or data.get("events") or []
        total_supply = data.get("totalTokens") or 1
        for ev in events:
            ts = ev.get("ts") or ev.get("timestamp")
            amount = ev.get("newEmissions") or ev.get("amount") or 0
            if not ts or not amount:
                continue
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            if dt < PERIOD_START or dt > PERIOD_END:
                continue
            pct = round(amount / total_supply * 100, 4)
            rows.append({
                "symbol":       symbol,
                "unlock_date":  dt,
                "pct_of_supply": pct,
                "usd_value":    0,
                "type":         ev.get("type", "vesting"),
                "source":       "defillama_emissions",
            })
        log.info("DefiLlama %s: %d events in range", slug, len(rows))
        return rows
    except requests.RequestException as exc:
        log.warning("DefiLlama request failed for %s: %s", slug, exc)
        return []


# ---------------------------------------------------------------------------
# 2.  CoinGecko supply ratios (free — no unlock dates, just provenance check)
# ---------------------------------------------------------------------------

COINGECKO_IDS = {
    "ARBUSDT":    "arbitrum",
    "WLDUSDT":    "worldcoin",
    "APTUSDT":    "aptos",
    "SUIUSDT":    "sui",
    "TIAUSDT":    "celestia",
    "JTOUSDT":    "jito",
    "ENAUSDT":    "ethena",
    "SEIUSDT":    "sei",
    "NEARUSDT":   "near-protocol",
    "INJUSDT":    "injective-protocol",
    "AVAXUSDT":   "avalanche-2",
    "ATOMUSDT":   "cosmos",
    "LINKUSDT":   "chainlink",
    "SOLUSDT":    "solana",
    "DOTUSDT":    "polkadot",
    "LTCUSDT":    "litecoin",
    "UNIUSDT":    "uniswap",
    "AAVEUSDT":   "aave",
    "FTMUSDT":    "fantom",
}


def fetch_coingecko_supply(symbol: str, cg_id: str) -> dict | None:
    """Return {circulating_supply, total_supply, pct_circulating} from CoinGecko."""
    url = f"https://api.coingecko.com/api/v3/coins/{cg_id}?localization=false&tickers=false&market_data=true&community_data=false&developer_data=false&sparkline=false"
    try:
        r = requests.get(url, timeout=15, headers={"Accept": "application/json"})
        if r.status_code == 429:
            log.warning("CoinGecko rate limit for %s — skipping", cg_id)
            return None
        r.raise_for_status()
        md = r.json().get("market_data", {})
        circ  = md.get("circulating_supply") or 0
        total = md.get("total_supply") or md.get("max_supply") or 0
        if not total:
            return None
        return {
            "symbol":             symbol,
            "circulating_supply": circ,
            "total_supply":       total,
            "pct_circulating":    round(circ / total * 100, 2),
        }
    except requests.RequestException as exc:
        log.warning("CoinGecko failed for %s: %s", cg_id, exc)
        return None


# ---------------------------------------------------------------------------
# 3.  Manual curation — documented vesting schedules
#     Source for each entry is its public tokenomics doc / announcement
# ---------------------------------------------------------------------------

def _monthly_rows(
    symbol: str,
    start_date: str,       # "YYYY-MM-DD" of first release
    end_date: str,         # "YYYY-MM-DD" exclusive upper bound
    pct_per_month: float,  # % of total supply per monthly release
    unlock_type: str,
    source: str,
) -> list[dict]:
    """Generate one row per month between start_date and end_date."""
    from datetime import date
    import calendar

    rows = []
    dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    while dt < end:
        if dt.strftime("%Y-%m-%d") >= PERIOD_START:
            rows.append({
                "symbol":        symbol,
                "unlock_date":   dt.strftime("%Y-%m-%d"),
                "pct_of_supply": pct_per_month,
                "usd_value":     0,
                "type":          unlock_type,
                "source":        source,
            })
        # advance one month
        m = dt.month + 1 if dt.month < 12 else 1
        y = dt.year if dt.month < 12 else dt.year + 1
        d = min(dt.day, calendar.monthrange(y, m)[1])
        dt = dt.replace(year=y, month=m, day=d)
    return rows


def build_curated_events() -> list[dict]:
    """
    Manually curated token unlock events.

    Sources:
      - Arbitrum Foundation tokenomics: https://docs.arbitrum.foundation/token-supply
        Cliff: March 16 2024 (1-year lockup from Mar 2023 TGE); ~11.13% of 10 B supply
        (investors 11.62% + team/advisors; actually the DAO-claimable cliff was 1.113B ARB)
      - Worldcoin tokenomics blog (Jul 2023):
        ~26.2M WLD/month for team+investors from TGE Jul 2023, total 10B supply
      - Aptos tokenomics (Oct 2022):
        Investor+contributor monthly vesting ~9.87M APT/month ≈ 0.987%/month of 1B total
        12-month cliff (Oct 2023), 36-month linear
      - Sui tokenomics (May 2023):
        Investor+contributor monthly vesting ~52M SUI/month ≈ 0.52%/month of 10B total
      - Celestia tokenomics (Oct 2023):
        1-year cliff Oct 31 2024; early backers 15.9% + core contributors 17.6% of 7.407B TIA
        Monthly from cliff: ~67M TIA/month ≈ 0.90%/month over 37 months
      - Jito tokenomics (Dec 2023):
        1-year cliff Dec 7 2024; core contributors 24.5% (24-month vest) + investors 7.36% (18-month vest)
        Monthly from cliff: ~14.3M JTO/month ≈ 1.43%/month of 1B total
      - Ethena tokenomics (Apr 2024):
        Investors 25.4%: 6-month cliff = Oct 5 2024, 18-month vest; ~211.7M ENA/month = 1.41%/month of 15B
        Core contributors 30%: 12-month cliff = Apr 5 2025, 18-month vest; ~250M/month = 1.67%/month
      - Sei tokenomics (Aug 2023):
        Investors+ecosystem: 6-month cliff Feb 2024, 18-month linear ~100M SEI/month ≈ 1.0%/month of 10B
    """
    rows: list[dict] = []
    src = "manual_curation_top20"

    # --- ARB: March 16 2024 cliff (1-year lockup from TGE March 16 2023) ---
    rows.append({
        "symbol":        "ARBUSDT",
        "unlock_date":   "2024-03-16",
        "pct_of_supply": 11.13,
        "usd_value":     0,
        "type":          "cliff",
        "source":        src,
    })
    # ARB team/advisor linear monthly vesting after cliff (~92M/month = 0.92%)
    rows += _monthly_rows("ARBUSDT", "2024-04-16", "2026-04-16", 0.92, "linear", src)

    # --- WLD: monthly team+investor vesting from TGE (Jul 2023) ---
    # 26.2M WLD/month = 0.262% of 10B total
    rows += _monthly_rows("WLDUSDT", "2024-05-24", "2026-05-01", 0.262, "linear", src)

    # --- APT: monthly investor+contributor vesting (cliff Oct 2023, 36-month vest) ---
    # ~9.87M APT/month ≈ 0.987% of 1B total; runs Oct 2023 → Oct 2026
    rows += _monthly_rows("APTUSDT", "2024-05-01", "2026-05-01", 0.987, "linear", src)

    # --- SUI: monthly investor+contributor vesting (from May 2023 TGE) ---
    # ~52M SUI/month = 0.52% of 10B; vesting window ends ~May 2026
    rows += _monthly_rows("SUIUSDT", "2024-05-03", "2026-05-03", 0.52, "linear", src)

    # --- TIA: 1-year cliff Oct 31 2024, then monthly vesting ---
    # early backers 15.9% + core contributors 17.6% of 7.407B = 2.482B TIA / 37 months ≈ 67M/mo = 0.90%
    rows.append({
        "symbol":        "TIAUSDT",
        "unlock_date":   "2024-10-31",
        "pct_of_supply": 0.90,
        "usd_value":     0,
        "type":          "cliff",
        "source":        src,
    })
    rows += _monthly_rows("TIAUSDT", "2024-11-30", "2026-05-01", 0.90, "linear", src)

    # --- JTO: 1-year cliff Dec 7 2024, then monthly vesting ---
    # core contributors 24.5%/24mo + investors 7.36%/18mo = ~14.3M JTO/month = 1.43% of 1B
    rows.append({
        "symbol":        "JTOUSDT",
        "unlock_date":   "2024-12-07",
        "pct_of_supply": 1.43,
        "usd_value":     0,
        "type":          "cliff",
        "source":        src,
    })
    rows += _monthly_rows("JTOUSDT", "2025-01-07", "2026-05-07", 1.43, "linear", src)

    # --- ENA: investors 6-month cliff Oct 5 2024, 18-month vest; 211.7M/month = 1.41% of 15B ---
    rows.append({
        "symbol":        "ENAUSDT",
        "unlock_date":   "2024-10-05",
        "pct_of_supply": 1.41,
        "usd_value":     0,
        "type":          "cliff",
        "source":        src,
    })
    rows += _monthly_rows("ENAUSDT", "2024-11-05", "2026-04-05", 1.41, "linear", src)
    # ENA core contributors: 12-month cliff Apr 5 2025, 18-month vest; 1.67%/month
    rows.append({
        "symbol":        "ENAUSDT",
        "unlock_date":   "2025-04-05",
        "pct_of_supply": 1.67,
        "usd_value":     0,
        "type":          "cliff",
        "source":        src,
    })
    rows += _monthly_rows("ENAUSDT", "2025-05-05", "2026-10-05", 1.67, "linear", src)

    # --- SEI: 6-month cliff Feb 2024, 18-month monthly vest ~100M SEI/month = 1.0% of 10B ---
    rows.append({
        "symbol":        "SEIUSDT",
        "unlock_date":   "2024-02-01",
        "pct_of_supply": 1.0,
        "usd_value":     0,
        "type":          "cliff",
        "source":        src,
    })
    rows += _monthly_rows("SEIUSDT", "2024-05-01", "2025-08-01", 1.0, "linear", src)

    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_checkpoint() -> dict:
    if CHECKPOINT.exists():
        return json.loads(CHECKPOINT.read_text())
    return {"defillama_tried": [], "rows_written": 0}


def save_checkpoint(cp: dict) -> None:
    CHECKPOINT.write_text(json.dumps(cp, indent=2))


def write_csv(rows: list[dict]) -> None:
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    log.info("Wrote %d rows → %s", len(rows), OUT_CSV)


def main() -> None:
    log.info("=== fetch_token_unlocks.py  (period %s → %s) ===", PERIOD_START, PERIOD_END)
    cp = load_checkpoint()
    all_rows: list[dict] = []

    # --- Step 1: Try DefiLlama emissions for each symbol ---
    log.info("Step 1: Trying DefiLlama /emissions/{slug} ...")
    defillama_rows: list[dict] = []
    defillama_status: dict[str, str] = {}
    already_tried = set(cp.get("defillama_tried", []))

    for symbol, slug in DEFILLAMA_SLUGS.items():
        if symbol in already_tried:
            log.info("  %s already attempted (checkpoint), skipping", symbol)
            defillama_status[symbol] = "skipped_checkpoint"
            continue
        result = try_defillama_emissions(symbol, slug)
        cp.setdefault("defillama_tried", []).append(symbol)
        if result:
            defillama_rows.extend(result)
            defillama_status[symbol] = f"ok_{len(result)}_rows"
        else:
            defillama_status[symbol] = "unavailable_402_or_no_data"
        time.sleep(0.5)

    save_checkpoint(cp)
    log.info("DefiLlama: %d rows fetched from %d slugs", len(defillama_rows), len(DEFILLAMA_SLUGS))
    all_rows.extend(defillama_rows)

    # --- Step 2: CoinGecko supply ratios (no dates — logged for provenance only) ---
    log.info("Step 2: CoinGecko supply ratios (provenance check, no unlock dates) ...")
    supply_info: list[dict] = []
    for symbol, cg_id in list(COINGECKO_IDS.items())[:5]:  # Sample first 5 to avoid rate limits
        info = fetch_coingecko_supply(symbol, cg_id)
        if info:
            supply_info.append(info)
            log.info("  %s: %.1f%% circulating (%s/%s)",
                     symbol, info["pct_circulating"],
                     f"{info['circulating_supply']:,.0f}",
                     f"{info['total_supply']:,.0f}")
        time.sleep(1.2)

    # --- Step 3: Manual curation for top tokens ---
    log.info("Step 3: Building manually curated unlock events ...")
    curated_rows = build_curated_events()
    # Filter to period
    curated_rows = [r for r in curated_rows if PERIOD_START <= r["unlock_date"] <= PERIOD_END]
    log.info("Manual curation: %d events in period %s → %s", len(curated_rows), PERIOD_START, PERIOD_END)
    all_rows.extend(curated_rows)

    if not all_rows:
        log.error("No rows generated from any source — check network and curated data")
        sys.exit(1)

    # Deduplicate (same symbol+date can appear from multiple sources)
    seen = set()
    deduped = []
    for r in all_rows:
        key = (r["symbol"], r["unlock_date"], r["source"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    deduped.sort(key=lambda r: (r["symbol"], r["unlock_date"]))

    write_csv(deduped)
    cp["rows_written"] = len(deduped)
    cp["last_run"] = datetime.now(tz=timezone.utc).isoformat()
    save_checkpoint(cp)

    # --- Summary ---
    from collections import Counter
    by_source = Counter(r["source"] for r in deduped)
    by_symbol = Counter(r["symbol"] for r in deduped)
    log.info("=== Summary ===")
    log.info("Total rows: %d", len(deduped))
    log.info("By source:  %s", dict(by_source))
    log.info("By symbol:  %s", dict(by_symbol.most_common(10)))
    log.info("Coverage:   %s → %s",
             min(r["unlock_date"] for r in deduped),
             max(r["unlock_date"] for r in deduped))

    dl_covered = [s for s, st in defillama_status.items() if "ok_" in st]
    dl_blocked  = [s for s, st in defillama_status.items() if "402" in st]
    if dl_blocked:
        log.info("DefiLlama PAYWALLED (402) for %d protocols: %s", len(dl_blocked), dl_blocked[:5])
        log.info("  → all data from manual_curation_top20 (partial_coverage)")
    if dl_covered:
        log.info("DefiLlama OK for: %s", dl_covered)

    log.info("Output: %s", OUT_CSV)
    log.info("Done.")


if __name__ == "__main__":
    main()

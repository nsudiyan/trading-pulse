"""
trade_logger.py — ingestion pipeline for actual closed trades.

Captures every real trade with full context for post-trade analysis (AVEVA-44/45).
Unlike outcome_tracker.py (which simulates signal outcomes), this module records
trades that were actually executed by the user.

Schema
------
Each trade record: see TRADE_SCHEMA below.

Storage
-------
  outcomes/trades.json  — append-only list of trade records (source of truth)
  outcomes/trades.csv   — flat export regenerated on demand

CLI
---
  python3 trade_logger.py log          — interactive trade entry wizard
  python3 trade_logger.py stats        — P&L + setup stats table
  python3 trade_logger.py list         — recent trades
  python3 trade_logger.py link <id> <run_ts> <symbol>  — link to screener signal
  python3 trade_logger.py export       — regenerate trades.csv from JSON
"""

import csv
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from file_lock import atomic_json_update

BASE_DIR    = Path(__file__).parent / "outcomes"
TRADES_JSON = BASE_DIR / "trades.json"
TRADES_CSV  = BASE_DIR / "trades.csv"

# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

# All fields in order — defines both JSON keys and CSV columns.
TRADE_SCHEMA: list[str] = [
    # Identity
    "trade_id",           # UUID
    "logged_ts",          # when this record was written (UTC ISO)

    # Core trade
    "symbol",             # e.g. BTCUSDT
    "setup",              # squeeze / bos_fvg / breakout / range_sweep / short_dist / manual
    "score",              # screener score at signal time (0 if manual)
    "grade",              # A+ / A / B / — etc.
    "direction",          # long / short

    # Timing
    "entry_ts",           # UTC ISO timestamp of actual entry
    "exit_ts",            # UTC ISO timestamp of actual exit
    "hold_time_min",      # computed: (exit_ts - entry_ts) in minutes

    # Prices & levels
    "entry_price",
    "exit_price",
    "stop_price",
    "tp1_price",
    "tp2_price",

    # Position sizing & P&L
    "position_size_usd",  # notional position size in USD (pre-leverage)
    "leverage",           # leverage used (1 = spot-equivalent)
    "pnl_usd",            # realised P&L in USD
    "pnl_pct",            # P&L as % of position_size_usd
    "r_multiple",         # (exit - entry) / (entry - stop), sign-corrected for direction
    "fees_usd",           # total fees paid (taker + funding)

    # Outcome
    "exit_reason",        # tp1 / sl / manual / time / liquidation
    "outcome_label",      # profitable / unprofitable / breakeven

    # Signal context at entry (filled from screener signal when linked)
    "screener_signal_ts", # run_ts of the matched screener signal (nullable)
    "funding",
    "oi_24h_pct",
    "mtf_bull",
    "mtf_bear",
    "rsi_1h",
    "cvd_kline",
    "cvd_trade",
    "ema_bull_1h",
    "ema_bull_4h",
    "choch_bull_1h",
    "choch_conviction",
    "vwap_dev",
    "rs_btc",
    "atr_at_entry",

    # Market context at entry
    "utc_hour",
    "btc_trend_4h",       # above / below / between (EMA20/50)
    "alt_breadth_pct",    # % of alts in uptrend at signal time
    "listing_age_days",
    "avg_vol_7d_usd",

    # Triggered signals (JSON-encoded list of signal names that scored >0)
    "triggered_signals",

    # Post-trade qualitative analysis
    "loss_reason",        # false_signal / premature_exit / bad_stop / market_noise / news / none
    "win_reason",         # trend_continuation / setup_confluence / news / liquidity_sweep / none
    "notes",              # free-text analyst note
]


# ─────────────────────────────────────────────────────────────────────────────
# Storage helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_dirs():
    BASE_DIR.mkdir(parents=True, exist_ok=True)


def _load_trades() -> list[dict]:
    if not TRADES_JSON.exists():
        return []
    try:
        return json.loads(TRADES_JSON.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_trades(trades: list[dict]):
    _ensure_dirs()
    atomic_json_update(TRADES_JSON, lambda _: trades, default=[])


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")


# ─────────────────────────────────────────────────────────────────────────────
# Computed fields
# ─────────────────────────────────────────────────────────────────────────────

def _compute_r_multiple(exit_price: float, entry_price: float,
                        stop_price: float, direction: str) -> float | None:
    risk = abs(entry_price - stop_price)
    if not risk or risk < 1e-12:
        return None
    if direction == "long":
        reward = exit_price - entry_price
    else:
        reward = entry_price - exit_price
    return round(reward / risk, 4)


def _outcome_label(pnl_usd: float | None, exit_reason: str | None) -> str:
    if exit_reason == "liquidation":
        return "unprofitable"
    if pnl_usd is None:
        return "breakeven"
    if pnl_usd > 0:
        return "profitable"
    if pnl_usd < 0:
        return "unprofitable"
    return "breakeven"


def _derive_fields(trade: dict) -> dict:
    """Fill computed fields that can be derived from other fields."""
    t = dict(trade)

    # hold_time_min
    if t.get("entry_ts") and t.get("exit_ts"):
        try:
            delta = _parse_ts(t["exit_ts"]) - _parse_ts(t["entry_ts"])
            t["hold_time_min"] = round(delta.total_seconds() / 60, 1)
        except Exception:
            pass

    # r_multiple
    if (t.get("entry_price") and t.get("exit_price") and
            t.get("stop_price") and t.get("direction")):
        try:
            t["r_multiple"] = _compute_r_multiple(
                float(t["exit_price"]), float(t["entry_price"]),
                float(t["stop_price"]), str(t["direction"]).lower()
            )
        except Exception:
            pass

    # pnl_pct
    if t.get("pnl_usd") is not None and t.get("position_size_usd"):
        try:
            t["pnl_pct"] = round(
                float(t["pnl_usd"]) / float(t["position_size_usd"]) * 100, 4
            )
        except Exception:
            pass

    # outcome_label
    if not t.get("outcome_label"):
        t["outcome_label"] = _outcome_label(
            t.get("pnl_usd"), t.get("exit_reason")
        )

    # utc_hour
    if not t.get("utc_hour") and t.get("entry_ts"):
        try:
            t["utc_hour"] = _parse_ts(t["entry_ts"]).hour
        except Exception:
            pass

    return t


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def log_trade(trade: dict) -> str:
    """
    Ingest a closed trade record.

    Required keys: symbol, direction, entry_price, exit_price, stop_price
    Optional keys: everything else in TRADE_SCHEMA

    Returns the trade_id of the saved record.
    """
    _ensure_dirs()

    t = dict(trade)
    if not t.get("trade_id"):
        t["trade_id"] = str(uuid.uuid4())
    if not t.get("logged_ts"):
        t["logged_ts"] = _now_ts()

    # Normalise direction
    raw_dir = str(t.get("direction", "long")).lower()
    t["direction"] = "long" if raw_dir in ("long", "лонг", "buy") else "short"

    # Encode triggered_signals list as JSON string for CSV compat
    if isinstance(t.get("triggered_signals"), list):
        t["triggered_signals"] = json.dumps(t["triggered_signals"], ensure_ascii=False)

    t = _derive_fields(t)

    trades = _load_trades()
    trades.append(t)
    _save_trades(trades)
    return t["trade_id"]


def link_screener_signal(trade_id: str, run_ts: str, symbol: str):
    """
    Link a trade record to its originating screener signal.
    Pulls signal context fields from outcome_tracker's resolved.csv when available.
    """
    import csv as _csv
    import outcome_tracker as _ot

    trades = _load_trades()
    idx = next((i for i, t in enumerate(trades) if t.get("trade_id") == trade_id), None)
    if idx is None:
        raise ValueError(f"Trade {trade_id} not found")

    trade = trades[idx]
    trade["screener_signal_ts"] = run_ts

    # Try to find the matching signal in resolved.csv to back-fill context
    csv_path = _ot.RESOLVED_CSV
    if csv_path.exists():
        with open(csv_path, "r", encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                if row.get("symbol") == symbol and row.get("run_ts", "")[:16] == run_ts[:16]:
                    for field in [
                        "funding", "oi_24h_pct", "mtf_bull", "mtf_bear",
                        "rsi_1h", "cvd_kline", "cvd_trade",
                        "ema_bull_1h", "ema_bull_4h", "choch_bull_1h",
                        "choch_conviction", "vwap_dev", "rs_btc", "atr_at_entry",
                        "utc_hour", "btc_trend_4h", "alt_breadth_pct",
                        "listing_age_days", "avg_vol_7d_usd",
                        "score", "grade", "setup",
                    ]:
                        if row.get(field) not in (None, "") and not trade.get(field):
                            trade[field] = row[field]
                    break

    trades[idx] = trade
    _save_trades(trades)


def list_trades(
    setup: str | None = None,
    direction: str | None = None,
    outcome: str | None = None,
    limit: int = 50,
) -> list[dict]:
    trades = _load_trades()
    if setup:
        trades = [t for t in trades if t.get("setup") == setup]
    if direction:
        d = direction.lower()
        trades = [t for t in trades if str(t.get("direction", "")).lower() == d]
    if outcome:
        trades = [t for t in trades if t.get("outcome_label") == outcome]
    return list(reversed(trades))[:limit]


def get_trade_stats() -> dict:
    """Returns P&L and win-rate stats grouped by setup and direction."""
    trades = _load_trades()
    if not trades:
        return {}

    from collections import defaultdict

    def _empty():
        return {"n": 0, "wins": 0, "losses": 0, "be": 0,
                "pnl": [], "r": [], "hold": []}

    by_setup: dict = defaultdict(_empty)
    overall = _empty()

    for t in trades:
        setup = t.get("setup", "manual")
        label = t.get("outcome_label", "breakeven")
        pnl   = t.get("pnl_usd")
        r     = t.get("r_multiple")
        hold  = t.get("hold_time_min")

        for bucket in (by_setup[setup], overall):
            bucket["n"] += 1
            if label == "profitable":
                bucket["wins"] += 1
            elif label == "unprofitable":
                bucket["losses"] += 1
            else:
                bucket["be"] += 1
            if pnl is not None:
                try:
                    bucket["pnl"].append(float(pnl))
                except Exception:
                    pass
            if r is not None:
                try:
                    bucket["r"].append(float(r))
                except Exception:
                    pass
            if hold is not None:
                try:
                    bucket["hold"].append(float(hold))
                except Exception:
                    pass

    def _summarise(b: dict) -> dict:
        n = b["n"]
        return {
            "n":         n,
            "win_rate":  round(b["wins"] / n * 100, 1) if n else 0,
            "total_pnl": round(sum(b["pnl"]), 2),
            "avg_pnl":   round(sum(b["pnl"]) / len(b["pnl"]), 2) if b["pnl"] else 0,
            "avg_r":     round(sum(b["r"]) / len(b["r"]), 3) if b["r"] else None,
            "avg_hold_min": round(sum(b["hold"]) / len(b["hold"]), 1) if b["hold"] else None,
            "wins":      b["wins"],
            "losses":    b["losses"],
            "be":        b["be"],
        }

    return {
        "by_setup": {s: _summarise(v) for s, v in by_setup.items()},
        "overall":  _summarise(overall),
        "n_total":  overall["n"],
    }


def export_csv():
    """Regenerate trades.csv from trades.json."""
    _ensure_dirs()
    trades = _load_trades()
    if not trades:
        print("No trades to export.")
        return

    # Union of all keys preserving schema order
    fieldnames = list(TRADE_SCHEMA)
    extra = [k for t in trades for k in t if k not in fieldnames]
    for k in extra:
        if k not in fieldnames:
            fieldnames.append(k)

    with open(TRADES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            w.writerow(t)

    print(f"Exported {len(trades)} trades → {TRADES_CSV}")


# ─────────────────────────────────────────────────────────────────────────────
# Interactive wizard
# ─────────────────────────────────────────────────────────────────────────────

def _ask(prompt: str, default=None, cast=None):
    suffix = f" [{default}]" if default is not None else ""
    raw = input(f"  {prompt}{suffix}: ").strip()
    if not raw and default is not None:
        return default
    if not raw:
        return None
    if cast:
        try:
            return cast(raw)
        except Exception:
            return raw
    return raw


def _wizard():
    print("\n── Trade Logger ─────────────────────────────────────────────")
    t: dict = {}

    t["symbol"]    = _ask("Symbol", "BTCUSDT")
    t["setup"]     = _ask("Setup (squeeze/bos_fvg/breakout/range_sweep/short_dist/manual)", "manual")
    t["score"]     = _ask("Screener score", 0, int)
    t["direction"] = _ask("Direction (long/short)", "long")
    t["entry_ts"]  = _ask("Entry timestamp UTC (YYYY-MM-DDTHH:MM:SS)", _now_ts())
    t["entry_price"] = _ask("Entry price", cast=float)
    t["exit_ts"]     = _ask("Exit timestamp UTC", _now_ts())
    t["exit_price"]  = _ask("Exit price", cast=float)
    t["stop_price"]  = _ask("Stop price", cast=float)
    t["tp1_price"]   = _ask("TP1 price (optional)", cast=float)

    t["position_size_usd"] = _ask("Position size USD", cast=float)
    t["leverage"]          = _ask("Leverage", 1, int)
    t["pnl_usd"]           = _ask("Realised P&L USD", cast=float)
    t["fees_usd"]          = _ask("Fees USD (optional)", 0.0, float)
    t["exit_reason"]       = _ask("Exit reason (tp1/sl/manual/time)", "manual")

    t["notes"]       = _ask("Notes (optional)", "")
    t["loss_reason"] = _ask("Loss reason if applicable (false_signal/premature_exit/bad_stop/market_noise/news/none)", "none")
    t["win_reason"]  = _ask("Win reason if applicable (trend_continuation/setup_confluence/news/none)", "none")

    trade_id = log_trade(t)
    print(f"\n  ✓ Saved trade {trade_id}")
    print(f"  File: {TRADES_JSON}\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _print_stats():
    stats = get_trade_stats()
    if not stats:
        print("No trades logged yet.")
        return

    ov = stats["overall"]
    print(f"\n── Overall ({ov['n']} trades) ─────────────────────────────────────")
    print(f"  Win rate:    {ov['win_rate']}%  "
          f"(W:{ov['wins']} L:{ov['losses']} BE:{ov['be']})")
    print(f"  Total P&L:   ${ov['total_pnl']:+.2f}")
    print(f"  Avg P&L:     ${ov['avg_pnl']:+.2f}  |  Avg R: {ov['avg_r'] or '—'}")
    if ov["avg_hold_min"]:
        h = ov["avg_hold_min"]
        print(f"  Avg hold:    {h:.0f}min ({h/60:.1f}h)")

    if stats["by_setup"]:
        print("\n── By setup ─────────────────────────────────────────────────")
        fmt = "  {:<15} {:>5}  {:>8}  {:>10}  {:>8}  {:>8}"
        print(fmt.format("Setup", "N", "WR%", "TotalP&L", "AvgP&L", "AvgR"))
        print("  " + "─"*60)
        for setup, s in sorted(stats["by_setup"].items(),
                                key=lambda x: x[1]["win_rate"], reverse=True):
            print(fmt.format(
                setup, s["n"], f"{s['win_rate']}%",
                f"${s['total_pnl']:+.2f}", f"${s['avg_pnl']:+.2f}",
                str(s["avg_r"]) if s["avg_r"] is not None else "—",
            ))
    print()


def _print_list(limit: int = 20):
    trades = list_trades(limit=limit)
    if not trades:
        print("No trades logged yet.")
        return
    print(f"\n── Recent {len(trades)} trades ─────────────────────────────────────")
    fmt = "  {:<8}  {:<12}  {:<10}  {:<6}  {:>10}  {:>8}  {:>7}  {}"
    print(fmt.format("Date", "Symbol", "Setup", "Dir", "P&L USD", "R", "WR lbl", "Exit"))
    print("  " + "─"*80)
    for t in trades:
        date = (t.get("entry_ts") or t.get("logged_ts") or "")[:10]
        pnl  = t.get("pnl_usd")
        r    = t.get("r_multiple")
        print(fmt.format(
            date,
            str(t.get("symbol", "?"))[:12],
            str(t.get("setup", "?"))[:10],
            str(t.get("direction", "?"))[:6],
            f"${float(pnl):+.2f}" if pnl is not None else "—",
            str(r) if r is not None else "—",
            str(t.get("outcome_label", "?"))[:7],
            str(t.get("exit_reason", "?"))[:10],
        ))
    print()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "log":
        _wizard()
    elif cmd == "stats":
        _print_stats()
    elif cmd == "list":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        _print_list(limit)
    elif cmd == "export":
        export_csv()
    elif cmd == "link":
        if len(sys.argv) < 5:
            print("Usage: trade_logger.py link <trade_id> <run_ts> <symbol>")
            sys.exit(1)
        link_screener_signal(sys.argv[2], sys.argv[3], sys.argv[4])
        print(f"Linked trade {sys.argv[2]} to screener signal at {sys.argv[3]}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

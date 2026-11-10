"""Paper portfolio scoped to the current confirmed sweep→CHoCH cohort."""
from __future__ import annotations

from collections import defaultdict
from math import ceil
import sqlite3

INITIAL_USDT = 1_000.0
NOTIONAL_USDT = 100.0
MAX_OPEN = 10
STEP_MS = 900_000
MODEL_VERSION = "sweep-choch-v1-level-age-moscow-policy"


def calculate_portfolio(signals: list[dict]) -> dict:
    """Deterministic paper index; not fills, PnL after costs, or a proven edge."""
    events: dict[int, list[tuple]] = defaultdict(list)
    excluded_gaps = pending = excluded_invalid = 0
    for signal in signals:
        bars = signal["bars"]
        start = signal["reference_start_ms"]
        if signal["status"] in {None, "waiting_next_bar"}:
            pending += 1
            continue
        if signal["status"] in {"invalid_reference", "invalid_candle"}:
            excluded_invalid += 1
            continue
        if (signal["status"] not in {"observing", "complete"}
                or not bars or not signal["reference_open"]
                or signal["side"] not in {"BUY", "SELL"}
                or len(bars) != signal["observed_bars"]
                or len(bars) != signal["expected_bars"]
                or any(bar["start_ms"] != start + i * STEP_MS
                       for i, bar in enumerate(bars))):
            excluded_gaps += 1
            continue
        events[start].append((1, signal["id"], "entry", signal))
        for i, bar in enumerate(bars):
            kind = ("exit" if signal["status"] == "complete" and i == len(bars) - 1
                    else "mark")
            events[bar["end_ms"] + 1].append((0, signal["id"], kind, bar["close"]))

    positions: dict[str, dict] = {}
    admitted: set[str] = set()
    realized = 0.0
    skipped_capacity = completed = 0
    peak = INITIAL_USDT
    max_drawdown = 0.0
    curve = []
    for at_ms in sorted(events):
        for _, signal_id, kind, value in sorted(events[at_ms]):
            if kind == "entry":
                if len(positions) >= MAX_OPEN:
                    skipped_capacity += 1
                    continue
                positions[signal_id] = {"side": 1 if value["side"] == "BUY" else -1,
                                        "reference": float(value["reference_open"]),
                                        "price": float(value["reference_open"])}
                admitted.add(signal_id)
            elif signal_id in positions:
                positions[signal_id]["price"] = float(value)
                if kind == "exit":
                    position = positions.pop(signal_id)
                    realized += NOTIONAL_USDT * position["side"] * (
                        position["price"] / position["reference"] - 1)
                    completed += 1
        unrealized = sum(NOTIONAL_USDT * position["side"] *
                         (position["price"] / position["reference"] - 1)
                         for position in positions.values())
        equity = INITIAL_USDT + realized + unrealized
        peak = max(peak, equity)
        drawdown_pct = 100 * (equity / peak - 1)
        max_drawdown = min(max_drawdown, drawdown_pct)
        curve.append({"at_ms": at_ms, "equity_usdt": round(equity, 4),
                      "drawdown_pct": round(drawdown_pct, 4)})

    current = curve[-1]["equity_usdt"] if curve else None
    stride = max(1, ceil(len(curve) / 400))
    sampled = []
    for i in range(0, len(curve), stride):
        chunk = curve[i:i + stride]
        selected = {0, len(chunk) - 1,
                    min(range(len(chunk)), key=lambda j: chunk[j]["equity_usdt"]),
                    max(range(len(chunk)), key=lambda j: chunk[j]["equity_usdt"]),
                    min(range(len(chunk)), key=lambda j: chunk[j]["drawdown_pct"])}
        sampled.extend(chunk[j] for j in sorted(selected))
    return {
        "kind": "paper_portfolio",
        "model_version": MODEL_VERSION,
        "initial_usdt": INITIAL_USDT,
        "fixed_notional_usdt": NOTIONAL_USDT,
        "max_concurrent": MAX_OPEN,
        "hold_hours": 72,
        "fees_and_slippage_included": False,
        "equity_usdt": current,
        "pnl_usdt": round(current - INITIAL_USDT, 4) if current is not None else None,
        "return_pct": round(100 * (current / INITIAL_USDT - 1), 4)
        if current is not None else None,
        "max_drawdown_pct": round(max_drawdown, 4) if curve else None,
        "admitted": len(admitted), "completed": completed,
        "open": len(positions), "pending": pending,
        "excluded_data_gaps": excluded_gaps,
        "excluded_invalid": excluded_invalid,
        "skipped_capacity": skipped_capacity,
        "curve": sampled,
    }


def read_portfolio(db: sqlite3.Connection) -> dict:
    rows = db.execute("""SELECT a.id,o.symbol,o.side,f.status,
                    f.reference_start_ms,f.reference_open,f.last_closed_ms,
                    f.observed_bars,f.expected_bars
        FROM signal_alerts a
        JOIN signal_outcomes o ON o.alert_id=a.id
        JOIN signal_scenarios s ON s.alert_id=a.id
        LEFT JOIN signal_followthrough f ON f.alert_id=a.id
        WHERE a.status='sent' AND a.id LIKE 'review:%'
          AND CASE WHEN json_valid(s.contract_json)
            THEN json_extract(s.contract_json,'$.version') END=?
          AND CASE WHEN json_valid(s.contract_json)
            THEN json_extract(s.contract_json,'$.setup_sequence.status') END='confirmed'
          AND CASE WHEN json_valid(s.contract_json)
            THEN json_extract(s.contract_json,'$.side') END IN ('BUY','SELL')
        ORDER BY a.sent_utc,a.id""", (MODEL_VERSION,)).fetchall()
    signals = []
    for row in rows:
        signal = dict(row)
        signal["bars"] = []
        if signal["reference_start_ms"] is not None and signal["last_closed_ms"] is not None:
            signal["bars"] = [dict(bar) for bar in db.execute("""SELECT start_ms,end_ms,close
                FROM candles WHERE symbol=? AND interval='15' AND start_ms>=?
                  AND end_ms<=? ORDER BY start_ms""",
                (signal["symbol"], signal["reference_start_ms"],
                 signal["last_closed_ms"])).fetchall()]
        signals.append(signal)
    return calculate_portfolio(signals)

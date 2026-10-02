"""Closed-candle path after a Telegram alert, measured over its next 72 hours.

The reference is the open of the first *full* 15m candle after Telegram sent
time. This is a research proxy, not a fill price. Intra-candle order is unknown.
"""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

STEP_MS = 900_000
WINDOW_MS = 72 * 3_600_000


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS signal_followthrough (
      alert_id TEXT PRIMARY KEY REFERENCES signal_alerts(id),
      sent_utc TEXT NOT NULL,
      status TEXT NOT NULL,
      reference_start_ms INTEGER NOT NULL,
      reference_open REAL,
      last_closed_ms INTEGER,
      observed_bars INTEGER NOT NULL DEFAULT 0,
      expected_bars INTEGER NOT NULL DEFAULT 0,
      mfe_pct REAL,
      mae_pct REAL,
      last_return_pct REAL,
      mfe_price REAL,
      mae_price REAL,
      mfe_bar_end_ms INTEGER,
      mae_bar_end_ms INTEGER,
      stop_price REAL,
      stop_touched_bar_end_ms INTEGER,
      updated_utc TEXT NOT NULL
    )""")
    db.commit()


def _sent_ms(value: str) -> int:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("sent_utc must include timezone")
    return int(instant.timestamp() * 1000)


def calculate_path(bars: list[dict], side: str, reference_start_ms: int,
                   last_exclusive_ms: int, window_ended: bool,
                   stop_price: float | None = None) -> dict:
    """Compute path from complete contiguous 15m bars fully inside the window."""
    expected = max(0, (last_exclusive_ms - reference_start_ms) // STEP_MS)
    result = {"status": "waiting_next_bar", "reference_open": None,
              "last_closed_ms": None, "observed_bars": len(bars),
              "expected_bars": expected, "mfe_pct": None, "mae_pct": None,
              "last_return_pct": None, "mfe_price": None, "mae_price": None,
              "mfe_bar_end_ms": None, "mae_bar_end_ms": None,
              "stop_touched_bar_end_ms": None}
    if expected == 0:
        return result
    if (len(bars) != expected or not bars or
            any(int(bar["start_ms"]) != reference_start_ms + index * STEP_MS
                for index, bar in enumerate(bars))):
        result["status"] = "data_gap"
        return result
    reference = float(bars[0]["open"])
    if reference <= 0 or side not in {"BUY", "SELL"}:
        result["status"] = "invalid_reference"
        return result
    if any(not (0 < float(bar["low"]) <= float(bar["high"])) for bar in bars):
        result["status"] = "invalid_candle"
        return result
    sign = 1 if side == "BUY" else -1
    favorable = (max(bars, key=lambda b: float(b["high"])) if sign == 1 else
                 min(bars, key=lambda b: float(b["low"])))
    adverse = (min(bars, key=lambda b: float(b["low"])) if sign == 1 else
               max(bars, key=lambda b: float(b["high"])))
    favorable_price = float(favorable["high" if sign == 1 else "low"])
    adverse_price = float(adverse["low" if sign == 1 else "high"])
    stop_touched = None
    if stop_price is not None and stop_price > 0:
        for bar in bars:
            touched = (float(bar["low"]) <= stop_price if sign == 1 else
                       float(bar["high"]) >= stop_price)
            if touched:
                stop_touched = int(bar["end_ms"])
                break
    result.update({
        "status": "complete" if window_ended else "observing",
        "reference_open": reference,
        "last_closed_ms": int(bars[-1]["end_ms"]),
        "mfe_pct": max(0.0, sign * (favorable_price / reference - 1) * 100),
        "mae_pct": min(0.0, sign * (adverse_price / reference - 1) * 100),
        "last_return_pct": sign * (float(bars[-1]["close"]) / reference - 1) * 100,
        "mfe_price": favorable_price,
        "mae_price": adverse_price,
        "mfe_bar_end_ms": int(favorable["end_ms"]),
        "mae_bar_end_ms": int(adverse["end_ms"]),
        "stop_touched_bar_end_ms": stop_touched,
    })
    return result


def resolve_due(db: sqlite3.Connection, now_ms: int, limit: int = 500) -> int:
    """Update sent alerts. Returns number of refreshed 72h records."""
    rows = db.execute("""SELECT o.alert_id,o.symbol,o.side,a.sent_utc,
                             f.stop_price,f.status AS previous_status
        FROM signal_outcomes o JOIN signal_alerts a ON a.id=o.alert_id
        LEFT JOIN signal_followthrough f ON f.alert_id=o.alert_id
        WHERE a.status='sent' AND a.sent_utc IS NOT NULL
          AND (f.alert_id IS NULL OR f.status!='complete')
        ORDER BY o.close_ms LIMIT ?""", (limit,)).fetchall()
    updated = 0
    for row in rows:
        sent_ms = _sent_ms(row["sent_utc"])
        reference_start = ((sent_ms + STEP_MS - 1) // STEP_MS) * STEP_MS
        window_end = sent_ms + WINDOW_MS
        last_exclusive = min((now_ms // STEP_MS) * STEP_MS,
                             (window_end // STEP_MS) * STEP_MS)
        raw = db.execute("""SELECT start_ms,end_ms,open,high,low,close
            FROM candles WHERE symbol=? AND interval='15'
            AND start_ms>=? AND start_ms<? ORDER BY start_ms""",
            (row["symbol"], reference_start, last_exclusive)).fetchall()
        path = calculate_path([dict(bar) for bar in raw], row["side"],
                              reference_start, last_exclusive,
                              now_ms >= window_end, row["stop_price"])
        db.execute("""INSERT INTO signal_followthrough
          (alert_id,sent_utc,status,reference_start_ms,reference_open,
           last_closed_ms,observed_bars,expected_bars,mfe_pct,mae_pct,
           last_return_pct,mfe_price,mae_price,mfe_bar_end_ms,mae_bar_end_ms,
           stop_price,stop_touched_bar_end_ms,updated_utc)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(alert_id) DO UPDATE SET
           sent_utc=excluded.sent_utc,status=excluded.status,
           reference_start_ms=excluded.reference_start_ms,
           reference_open=excluded.reference_open,last_closed_ms=excluded.last_closed_ms,
           observed_bars=excluded.observed_bars,expected_bars=excluded.expected_bars,
           mfe_pct=excluded.mfe_pct,mae_pct=excluded.mae_pct,
           last_return_pct=excluded.last_return_pct,mfe_price=excluded.mfe_price,
           mae_price=excluded.mae_price,mfe_bar_end_ms=excluded.mfe_bar_end_ms,
           mae_bar_end_ms=excluded.mae_bar_end_ms,
           stop_touched_bar_end_ms=excluded.stop_touched_bar_end_ms,
           updated_utc=excluded.updated_utc""",
            (row["alert_id"], row["sent_utc"], path["status"], reference_start,
             path["reference_open"], path["last_closed_ms"], path["observed_bars"],
             path["expected_bars"], path["mfe_pct"], path["mae_pct"],
             path["last_return_pct"], path["mfe_price"], path["mae_price"],
             path["mfe_bar_end_ms"], path["mae_bar_end_ms"], row["stop_price"],
             path["stop_touched_bar_end_ms"],
             datetime.now(timezone.utc).isoformat()))
        updated += 1
    if updated:
        db.commit()
    return updated

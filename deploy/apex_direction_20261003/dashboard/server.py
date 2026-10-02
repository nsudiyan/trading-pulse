"""Public read-only Apex dashboard backed by the private news-chart-bot SQLite DB."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import logging
import json
import os
from pathlib import Path
import sqlite3

import aiohttp
from aiohttp import web
from portfolio import read_portfolio
from channel import ensure_schema as ensure_channel_schema, read_posts, sync_posts
from movement import measure, WINDOW_MS

ROOT = Path(__file__).resolve().parent
STEP_MS = 900_000
MAX_LIMIT = 100
LOG = logging.getLogger("apex_dashboard")


def read_signals(db_path: str, limit: int = 60) -> dict:
    """Return only Telegram-acknowledged, structured review alerts."""
    limit = max(1, min(MAX_LIMIT, int(limit)))
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        rows = db.execute("""SELECT a.id,a.sent_utc,a.created_utc,a.text,a.reason,
                    o.symbol,o.side,o.signal_interval,o.close_ms,o.reference_close,
                    o.price_1h,o.price_4h,o.price_24h,
                    o.return_1h_pct,o.return_4h_pct,o.return_24h_pct,
                    f.status AS path_status,f.reference_start_ms,f.reference_open,
                    f.last_closed_ms,f.observed_bars,f.expected_bars,
                    f.mfe_pct,f.mae_pct,f.last_return_pct,
                    f.mfe_price,f.mae_price,f.mfe_bar_end_ms,f.mae_bar_end_ms,
                    f.stop_price,f.stop_touched_bar_end_ms
                  FROM signal_alerts a
                  LEFT JOIN signal_outcomes o ON o.alert_id=a.id
                  LEFT JOIN signal_followthrough f ON f.alert_id=a.id
                  WHERE a.status='sent' AND a.id LIKE 'review:%'
                  ORDER BY a.sent_utc DESC LIMIT ?""", (limit,)).fetchall()
        scenario_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_scenarios'").fetchone()
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        signals = []
        for row in rows:
            item = dict(row)
            legacy = row["symbol"] is None
            if legacy:
                # Earlier Telegram reviews had no direction/outcome record.
                # Show their real message, but never invent a trade result.
                parts = row["id"].split(":")
                item["symbol"] = parts[1] if len(parts) > 1 else "—"
                item["signal_interval"] = parts[2] if len(parts) > 2 else None
                item["path_status"] = "legacy_unmeasured"
            item["setup_type"] = ("legacy_review" if legacy else
                                  "strong_sweep_review" if row["reason"] == "strong_sweep_review"
                                  else "chart_review")
            points = []
            start = row["reference_start_ms"]
            end = row["last_closed_ms"]
            reference = row["reference_open"]
            if start is not None and end is not None and reference and reference > 0:
                # Hourly close samples for the small chart. MFE/MAE above are
                # computed from every full 15m bar's high/low, not these points.
                bars = db.execute("""SELECT start_ms,end_ms,close FROM candles
                    WHERE symbol=? AND interval='15' AND start_ms>=? AND end_ms<=?
                    ORDER BY start_ms""", (row["symbol"], start, end)).fetchall()
                sign = 1 if row["side"] == "BUY" else -1
                for index, candle in enumerate(bars):
                    if index % 4 == 3 or index == len(bars) - 1:
                        points.append({"at_ms": candle["end_ms"] + 1,
                                       "return_pct": sign * 100 *
                                       (float(candle["close"]) / reference - 1)})
            item["curve"] = points
            contract_row = (db.execute("SELECT contract_json FROM signal_scenarios WHERE alert_id=?", (row["id"],)).fetchone()
                            if scenario_table else None)
            try:
                scenario = json.loads(contract_row[0]) if contract_row else None
            except (ValueError, TypeError):
                scenario = None
            item["scenario"] = scenario if isinstance(scenario, dict) else {"side": None, "status": "historical_unverified", "reason": "Контекст направления при отправке не сохранён"}
            try:
                sent = datetime.fromisoformat(row["sent_utc"].replace("Z", "+00:00"))
                if sent.tzinfo is None:
                    raise ValueError("timezone missing")
                sent_ms = int(sent.timestamp() * 1000)
            except (ValueError, TypeError, AttributeError):
                item["movement"] = {"status": "data_unavailable", "reason": "invalid_sent_timestamp"}
                signals.append(item)
                continue
            anchor_start = ((sent_ms + STEP_MS - 1) // STEP_MS) * STEP_MS
            raw_bars = [dict(b) for b in db.execute("""SELECT start_ms,end_ms,open,high,low,close FROM candles
                WHERE symbol=? AND interval='15' AND start_ms>=? AND start_ms<? ORDER BY start_ms""",
                (item["symbol"], anchor_start, min(anchor_start + WINDOW_MS, now_ms)))]
            item["movement"] = measure(raw_bars, sent_ms, now_ms, item["scenario"].get("side"))
            signals.append(item)
        stats = db.execute("""SELECT COUNT(*) AS sent,
            SUM(CASE WHEN o.alert_id IS NOT NULL THEN 1 ELSE 0 END) AS measured,
            SUM(CASE WHEN o.alert_id IS NULL THEN 1 ELSE 0 END) AS legacy_unmeasured,
            SUM(CASE WHEN f.status='complete' THEN 1 ELSE 0 END) AS complete,
            AVG(CASE WHEN f.status='complete' THEN f.mfe_pct END) AS avg_mfe_pct,
            AVG(CASE WHEN f.status='complete' THEN f.mae_pct END) AS avg_mae_pct,
            AVG(CASE WHEN f.status='complete' THEN f.last_return_pct END) AS avg_last_return_pct
            FROM signal_alerts a
            LEFT JOIN signal_outcomes o ON o.alert_id=a.id
            LEFT JOIN signal_followthrough f ON f.alert_id=a.id
            WHERE a.status='sent' AND a.id LIKE 'review:%'""").fetchone()
        return {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "measurement": "first_full_15m_open_after_telegram_send",
                "window_hours": 72, "signals": signals, "stats": dict(stats),
                "portfolio": read_portfolio(db)}
    finally:
        db.close()


async def api_signals(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", "60"))
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid limit") from None
    try:
        result = read_signals(request.app["db_path"], limit)
    except sqlite3.Error:
        raise web.HTTPServiceUnavailable(text="Signal data temporarily unavailable") from None
    return web.json_response(result, headers={"Cache-Control": "no-store",
                                              "X-Content-Type-Options": "nosniff"})


async def api_channel(request: web.Request) -> web.Response:
    try:
        result = read_posts(request.app["channel_db_path"])
    except sqlite3.Error:
        raise web.HTTPServiceUnavailable(text="Channel history temporarily unavailable") from None
    return web.json_response(result, headers={"Cache-Control": "no-store",
                                              "X-Content-Type-Options": "nosniff"})


async def channel_collector(app: web.Application):
    channel_db = app["channel_db_path"]
    Path(channel_db).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(channel_db) as db:
        ensure_channel_schema(db)

    async def run():
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    await sync_posts(session, channel_db)
                except Exception:
                    LOG.exception("Public APEX channel sync failed; retrying")
                await asyncio.sleep(60)

    task = asyncio.create_task(run())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(ROOT / "index.html", headers={"Cache-Control": "no-cache"})


async def asset(request: web.Request) -> web.FileResponse:
    name = request.match_info["name"]
    if name not in {"styles.css", "app.js"}:
        raise web.HTTPNotFound()
    return web.FileResponse(ROOT / name, headers={"Cache-Control": "no-cache"})


def create_app(db_path: str, channel_db_path: str = "/var/lib/apex-dashboard/channel.sqlite3") -> web.Application:
    app = web.Application()
    app["db_path"] = db_path
    app["channel_db_path"] = channel_db_path
    app.cleanup_ctx.append(channel_collector)
    app.router.add_get("/", index)
    app.router.add_get("/api/signals", api_signals)
    app.router.add_get("/api/channel", api_channel)
    app.router.add_get("/{name}", asset)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.getenv("APEX_DB_PATH", "/var/lib/news-chart-bot/state.sqlite3"))
    parser.add_argument("--channel-db", default=os.getenv("APEX_CHANNEL_DB_PATH", "/var/lib/apex-dashboard/channel.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5083)
    args = parser.parse_args()
    web.run_app(create_app(args.db, args.channel_db), host=args.host, port=args.port, access_log=None)

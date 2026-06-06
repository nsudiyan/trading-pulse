"""
daily_report.py — ежедневный отчёт о работе Claude RT-фильтра + pump_detector.

Читает за последние 24 часа:
  - outcomes/claude_cost.csv  — все вызовы Claude API
  - outcomes/pump_resolved.csv — реализованные pump-сигналы (WIN/LOSS)
  - outcomes/wait_watchlist.json — текущие WAIT-кандидаты

Шлёт сводку в Telegram-личку.

CLI:
    python3 daily_report.py          — отчёт за 24h в TG
    python3 daily_report.py --hours 6 — за произвольный период
    python3 daily_report.py --stdout  — только в stdout, не в TG
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR        = Path(__file__).parent
COST_CSV        = BASE_DIR / "outcomes" / "claude_cost.csv"
PUMP_RESOLVED   = BASE_DIR / "outcomes" / "pump_resolved.csv"
SCREENER_RESOLVED = BASE_DIR / "outcomes" / "resolved.csv"
TRADES_JSON     = BASE_DIR / "outcomes" / "trades.json"        # P0-2: реальные сделки
ALERTS_INDEX    = BASE_DIR / "outcomes" / "alerts_index.json"  # P0-2: дисциплина (skipped)
WATCHLIST_JSON  = BASE_DIR / "outcomes" / "wait_watchlist.json"

# P1-8c: маскировка bot-токена в логируемых ошибках (URL в requests-исключениях)
try:
    from telegram_alerts import redact_token
except Exception:                                  # автономный запуск без telegram_alerts
    import re as _re_rt
    def redact_token(s):
        return _re_rt.sub(r"/bot\d+:[\w-]+", "/bot<REDACTED>", str(s))



def _load_dotenv():
    p = BASE_DIR / ".env"
    if not p.exists():
        return
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip(); v = v.strip()
            if v and v[0] in ('"', "'") and v[-1] == v[0]:
                v = v[1:-1]
            os.environ.setdefault(k, v)


_load_dotenv()


def _cost_stats(hours: int) -> dict:
    if not COST_CSV.exists():
        return {"calls": 0, "cost_usd": 0.0, "by_verdict": {}, "by_setup": {},
                "in_tok": 0, "out_tok": 0}
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    calls = 0; cost = 0.0; in_tok = 0; out_tok = 0
    by_verdict: dict = defaultdict(int)
    by_setup:   dict = defaultdict(int)
    by_symbol:  dict = defaultdict(int)
    with open(COST_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                ts = datetime.fromisoformat(row["ts"]).timestamp()
            except Exception:
                continue
            if ts < cutoff:
                continue
            calls   += 1
            cost    += float(row.get("cost_usd", 0) or 0)
            in_tok  += int(row.get("input_tokens",  0) or 0)
            out_tok += int(row.get("output_tokens", 0) or 0)
            by_verdict[row.get("verdict", "?")] += 1
            by_setup[row.get("setup", "?")]     += 1
            by_symbol[row.get("symbol", "?")]   += 1
    return {
        "calls": calls, "cost_usd": round(cost, 4),
        "in_tok": in_tok, "out_tok": out_tok,
        "by_verdict": dict(by_verdict),
        "by_setup":   dict(by_setup),
        "top_symbols": sorted(by_symbol.items(), key=lambda x: -x[1])[:5],
    }


def _outcome_stats(csv_path: Path, hours: int) -> dict:
    if not csv_path.exists():
        return {"total": 0, "wins": 0, "losses": 0, "flat": 0, "wr": None}
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    wins = losses = flat = 0
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts_val = row.get("ts") or row.get("run_ts") or ""
            try:
                if ts_val.isdigit():
                    ts = int(ts_val)
                else:
                    _dt = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
                    if _dt.tzinfo is None:   # BUG2: run_ts = UTC-wall-clock без tz → задаём UTC (иначе naive.timestamp() сдвигает окно на MSK-хосте)
                        _dt = _dt.replace(tzinfo=timezone.utc)
                    ts = _dt.timestamp()
            except Exception:
                continue
            if ts < cutoff:
                continue
            oc = (row.get("outcome_4h") or "").upper()
            if oc in ("WIN", "TP1"):       # BUG2 fix: TP1 (хит тейк-профита) = win; раньше выпадал из WR
                wins += 1
            elif oc in ("LOSS", "STOP"):   # STOP (хит стопа) = loss; раньше выпадал (screener CSV)
                losses += 1
            elif oc == "FLAT":
                flat += 1
    total_dec = wins + losses
    return {
        "total": wins + losses + flat,
        "wins": wins, "losses": losses, "flat": flat,
        "wr": round(wins / total_dec * 100, 1) if total_dec > 0 else None,
    }


def _watchlist_snapshot() -> dict:
    if not WATCHLIST_JSON.exists():
        return {"count": 0, "items": []}
    try:
        items = json.loads(WATCHLIST_JSON.read_text(encoding="utf-8"))
    except Exception:
        items = []
    now = int(datetime.now(timezone.utc).timestamp())
    active = [it for it in items if now - it.get("ts", 0) < 4 * 3600]
    return {
        "count": len(active),
        "items": [
            {"symbol": it["symbol"], "setup": it.get("setup"),
             "conf": it.get("confidence", 0),
             "age_min": int((now - it.get("ts", 0)) / 60)}
            for it in active
        ],
    }


def _real_trades_stats(hours: int) -> dict:
    """P0-2 (петля): РЕАЛЬНЫЕ сделки (trades.json) + дисциплина (alerts_index).
    Отдельный счёт от paper-симуляции — не смешивать."""
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600

    def _ts_ok(ts) -> bool:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp() >= cutoff
        except Exception:
            return False

    entered = skipped = closed = open_now = 0
    r_sum = 0.0
    try:
        trades = json.loads(TRADES_JSON.read_text(encoding="utf-8"))
    except Exception:
        trades = []
    for t in trades:
        if t.get("status") == "open":
            open_now += 1
        if _ts_ok(t.get("entry_ts") or t.get("logged_ts")):
            entered += 1
        if t.get("status") == "closed" and _ts_ok(t.get("exit_ts")):
            closed += 1
            try:
                r_sum += float(t.get("r_multiple") or 0)
            except (TypeError, ValueError):
                pass
    try:
        idx = json.loads(ALERTS_INDEX.read_text(encoding="utf-8"))
        skipped = sum(1 for v in (idx or {}).values()
                      if (v or {}).get("status") == "skipped" and _ts_ok(v.get("action_ts")))
    except Exception:
        pass
    return {"entered": entered, "skipped": skipped, "closed": closed,
            "open_now": open_now, "r_sum": r_sum}


def build_report(hours: int = 24) -> str:
    cost = _cost_stats(hours)
    pump = _outcome_stats(PUMP_RESOLVED, hours)
    scr  = _outcome_stats(SCREENER_RESOLVED, hours)
    wl   = _watchlist_snapshot()

    period = f"{hours}h"
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"📊 <b>Daily Report</b>  ({period}, {now})",
        "",
        f"<b>Claude RT-фильтр</b>",
        f"  Вызовов: <b>{cost['calls']}</b>  |  Стоимость: <b>${cost['cost_usd']:.4f}</b>",
        f"  Tokens: in={cost['in_tok']:,}  out={cost['out_tok']:,}",
    ]
    if cost["by_verdict"]:
        verdicts_str = "  |  ".join(f"{k}: {v}" for k, v in cost["by_verdict"].items())
        lines.append(f"  Verdicts: {verdicts_str}")
    if cost["by_setup"]:
        setup_top = sorted(cost["by_setup"].items(), key=lambda x: -x[1])[:5]
        setup_str = "  |  ".join(f"{k}:{v}" for k, v in setup_top)
        lines.append(f"  Setup: {setup_str}")
    if cost["top_symbols"]:
        sym_str = "  ".join(f"{s}×{n}" for s, n in cost["top_symbols"])
        lines.append(f"  Top symbols: {sym_str}")

    lines += [
        "",
        f"<b>Pump_detector outcomes (4h)</b>",
        f"  Total: {pump['total']}  |  WIN: {pump['wins']}  LOSS: {pump['losses']}  FLAT: {pump['flat']}",
    ]
    if pump["wr"] is not None:
        lines.append(f"  WR: <b>{pump['wr']:.1f}%</b>")

    lines += [
        "",
        f"<b>Screener outcomes (4h)</b>",
        f"  Total: {scr['total']}  |  WIN: {scr['wins']}  LOSS: {scr['losses']}  FLAT: {scr['flat']}",
    ]
    if scr["wr"] is not None:
        lines.append(f"  WR: <b>{scr['wr']:.1f}%</b>")

    lines += [
        "",
        f"<b>Wait watchlist</b>: {wl['count']} активных",
    ]
    for it in wl["items"][:5]:
        lines.append(f"  · {it['symbol']} [{it['setup']}] conf={it['conf']:.0%} ({it['age_min']}m)")

    # P0-2 (петля): РЕАЛЬНЫЕ сделки — отдельный счёт, НЕ смешивать с paper выше
    try:
        real = _real_trades_stats(hours)
        real_line = (f"  вошёл {real['entered']} / пропустил {real['skipped']} "
                     f"/ закрыто {real['closed']}")
        if real["closed"]:
            real_line += f" / real R=<b>{real['r_sum']:+.2f}</b>"
        lines += ["", f"<b>📒 РЕАЛЬНЫЕ СДЕЛКИ {period}</b>", real_line]
        if real["open_now"]:
            lines.append(f"  сейчас открыто: {real['open_now']}")
    except Exception as _re_err:
        lines += ["", f"<i>real trades: n/a ({_re_err})</i>"]

    return "\n".join(lines)


def send_to_telegram(text: str):
    try:
        import telegram_alerts as _tg
        import requests
        cfg = _tg.load_config()
        token = cfg.get("bot_token")
        chat  = cfg.get("chat_id") or cfg.get("owner_chat_id")
        if not token or not chat:
            print("[TG] не настроен")
            return False
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
            timeout=15,
        ).json()
        return bool(r.get("ok"))
    except Exception as e:
        print(f"[TG] ошибка: {redact_token(e)}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours",  type=int, default=24)
    ap.add_argument("--stdout", action="store_true", help="Только stdout, без TG")
    args = ap.parse_args()

    text = build_report(args.hours)
    print(text)
    if not args.stdout:
        ok = send_to_telegram(text)
        print(f"\n[TG] sent: {ok}")


if __name__ == "__main__":
    main()

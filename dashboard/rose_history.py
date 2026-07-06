#!/usr/bin/env python3
"""rose_history.py — история и анализ сигналов Rose-каналов (rose, RoseSignalsPremium).

Методика брата (2026-07-03):
  • старт отработки сигнала смотрим в окне 6 часов после алерта;
  • итог сигнала — максимальный пик и просадка за СУТКИ после алерта;
  • если алерт ПОДТВЕРДИЛСЯ повторно в течение 6ч (тот же символ+направление) —
    итог по суткам считаем ЗАНОВО от последнего подтверждения (якорь сдвигается).

Сигналы группируются в ТРЕКИ: цепочка алертов одного symbol+direction с
промежутками ≤6ч = один трек; anchor = время последнего подтверждения.

Чтение каналов — READ-ONLY через существующую Telethon-сессию бота
(channels_config.json), инкрементально по max_id. Стейты бота НЕ трогаем.
Vision-анализ скриншотов не выполняется: сигналы без тикера в тексте/подписи
бэкфиллом не ловятся.

Запуск: python3 rose_history.py [--backfill-days 30] [--no-fetch]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DIR = Path(__file__).resolve().parent
TRADING = DIR.parent
sys.path.insert(0, str(TRADING))

from resolve_channel_outcomes import fetch_15m  # noqa: E402

STORE_PATH = DIR / "rose_history.json"
ROSE_CHANNELS = ["RoseSignalsPremium", "rose"]
CONFIRM_WINDOW_H = 6.0    # повтор ≤6ч от последнего алерта трека = подтверждение
START_WINDOW_H = 6.0      # «начало отработки» — первые 6ч от якоря
FINAL_WINDOW_H = 24.0     # итог — пик/просадка за сутки от якоря
BAR_MS = 15 * 60_000


def load_store() -> dict:
    try:
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"tracks": [], "max_id": {}}


def save_store(store: dict):
    STORE_PATH.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")


# ─── Telegram fetch (read-only) ──────────────────────────────────────────────

async def _fetch_async(backfill_days: int, max_ids: dict) -> list[dict]:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from channel_reader import parse_message  # noqa

    cfg = json.loads((TRADING / "channels_config.json").read_text())
    out = []
    async with TelegramClient(StringSession(cfg["session_string"]),
                              cfg["api_id"], cfg["api_hash"]) as client:
        for ch in ROSE_CHANNELS:
            min_id = int(max_ids.get(ch, 0))
            kwargs = {"min_id": min_id} if min_id else \
                     {"offset_date": None, "limit": 800}
            since = datetime.now(timezone.utc) - timedelta(days=backfill_days)
            try:
                async for msg in client.iter_messages(ch, **kwargs):
                    if msg.date < since:
                        break
                    text = msg.message or ""
                    parsed = parse_message(text, ch, has_photo=bool(msg.photo))
                    if parsed.get("type") == "signal" and parsed.get("symbol"):
                        direction = (parsed.get("direction") or "").lower()
                        # Правило брата (2026-07-05): канал rose ВСЕГДА лонгует —
                        # его «шорты» = fallback-эвристика съела новость («Saylor
                        # selling BTC»). RoseSignalsPremium шортит по-настоящему
                        # («Short #BTC», «#ETH SHORT», 45 шт/30д) — не трогаем.
                        if ch == "rose":
                            direction = "long"
                        out.append({
                            "channel": ch,
                            "msg_id": msg.id,
                            "ts_utc": msg.date.astimezone(timezone.utc).isoformat(),
                            "symbol": parsed["symbol"],
                            "direction": direction,
                            "note": (parsed.get("note") or "")[:120],
                        })
                    max_ids[ch] = max(int(max_ids.get(ch, 0)), msg.id)
            except Exception as e:
                print(f"[rose] {ch}: fetch error {e}", file=sys.stderr)
    return out


def fetch_new_signals(store: dict, backfill_days: int) -> list[dict]:
    max_ids = store.setdefault("max_id", {})
    sigs = asyncio.run(_fetch_async(backfill_days, max_ids))
    sigs.sort(key=lambda s: s["ts_utc"])
    print(f"[rose] новых сигналов из TG: {len(sigs)}")
    return sigs


# ─── Треки ───────────────────────────────────────────────────────────────────

def add_to_tracks(store: dict, signals: list[dict]):
    """Каждый сигнал: конфирм открытого трека (same symbol+direction,
    gap ≤6ч от ПОСЛЕДНЕГО алерта трека) или новый трек."""
    tracks = store["tracks"]
    seen_msgs = {(a["channel"], a["msg_id"])
                 for t in tracks for a in t["alerts"]}
    for s in signals:
        if (s["channel"], s["msg_id"]) in seen_msgs:
            continue
        if s["direction"] not in ("long", "short"):
            continue
        ts = datetime.fromisoformat(s["ts_utc"])
        best = None
        for t in tracks:
            if t["symbol"] != s["symbol"] or t["direction"] != s["direction"]:
                continue
            last = datetime.fromisoformat(t["anchor_ts"])
            if timedelta(0) <= ts - last <= timedelta(hours=CONFIRM_WINDOW_H):
                best = t
                break
        alert = {"channel": s["channel"], "msg_id": s["msg_id"],
                 "ts_utc": s["ts_utc"], "note": s["note"]}
        if best is not None:
            best["alerts"].append(alert)
            best["confirms"] = len(best["alerts"])
            best["anchor_ts"] = s["ts_utc"]   # итог суток — заново от подтверждения
            best["outcome"] = None            # пересчёт
        else:
            tracks.append({
                "id": f"{s['symbol']}:{s['direction']}:{s['ts_utc'][:16]}",
                "symbol": s["symbol"], "direction": s["direction"],
                "first_ts": s["ts_utc"], "anchor_ts": s["ts_utc"],
                "confirms": 1, "alerts": [alert], "outcome": None,
            })
        seen_msgs.add((s["channel"], s["msg_id"]))


# ─── Отработка ───────────────────────────────────────────────────────────────

def resolve_tracks(store: dict, limit: int = 200):
    """Пик/просадка в сторону сигнала: старт (6ч) и итог (24ч) от якоря."""
    now_ms = int(time.time() * 1000)
    todo = [t for t in store["tracks"]
            if not (t.get("outcome") or {}).get("final")]
    done = 0
    for t in sorted(todo, key=lambda x: x["anchor_ts"], reverse=True):
        if done >= limit:
            break
        anchor = datetime.fromisoformat(t["anchor_ts"])
        anchor_ms = int(anchor.timestamp() * 1000)
        first_bar = ((anchor_ms // BAR_MS) + 1) * BAR_MS
        end_ms = anchor_ms + int(FINAL_WINDOW_H * 3600_000)
        window_complete = end_ms <= now_ms - BAR_MS
        bars = fetch_15m(t["symbol"], first_bar, min(end_ms, now_ms))
        bars = [b for b in bars if b[0] >= first_bar and b[0] + BAR_MS <= now_ms]
        done += 1
        time.sleep(0.12)
        if not bars:
            t["outcome"] = {"status": "no_data", "final": window_complete}
            continue
        basis = bars[0][4]
        if basis <= 0:
            t["outcome"] = {"status": "no_data", "final": window_complete}
            continue
        up = t["direction"] != "short"  # long и без-направления (Δ) — лонг-семантика

        def fav(b):
            return (b[2] - basis) / basis * 100 if up else (basis - b[3]) / basis * 100

        def adv(b):
            return (b[3] - basis) / basis * 100 if up else (basis - b[2]) / basis * 100

        # ПИК/ПРОСАДКА — строго после бара входа (его high/low могли быть до
        # close = якоря входа): включать = look-ahead.
        post = bars[1:]
        cut6 = anchor_ms + int(START_WINDOW_H * 3600_000)
        post6 = [b for b in post if b[0] + BAR_MS <= cut6]
        last = bars[-1][4]
        t["outcome"] = {
            "status": "ok",
            "final": window_complete,
            "basis": basis,
            "peak6_pct": round(max((fav(b) for b in post6), default=0.0), 2),
            "dd6_pct": round(min((adv(b) for b in post6), default=0.0), 2),
            "peak24_pct": round(max((fav(b) for b in post), default=0.0), 2),
            "dd24_pct": round(min((adv(b) for b in post), default=0.0), 2),
            "ret24_pct": round((last - basis) / basis * 100 * (1 if up else -1), 2),
            "bars": len(bars),
        }
    print(f"[rose] отработка пересчитана для {done} треков")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill-days", type=int, default=30)
    ap.add_argument("--no-fetch", action="store_true")
    args = ap.parse_args()

    store = load_store()
    if not args.no_fetch:
        signals = fetch_new_signals(store, args.backfill_days)
        add_to_tracks(store, signals)
        save_store(store)
    resolve_tracks(store)
    save_store(store)
    fin = sum(1 for t in store["tracks"] if (t.get("outcome") or {}).get("final"))
    print(f"[rose] треков всего {len(store['tracks'])}, финальных {fin}")


if __name__ == "__main__":
    sys.exit(main())

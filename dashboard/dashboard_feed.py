#!/usr/bin/env python3
"""dashboard_feed.py — сборщик feed.json для трейдинг-дашборда на Vercel.

Собирает из локальных файлов ~/trading агрегаты и живые сигналы, пишет
site/feed.json и пушит его в публичный GitHub-репо (Contents API через gh CLI,
паттерн mirofish: Vercel Blob в прошлом блокировали по bandwidth).

СЕКРЕТЫ НЕ ВКЛЮЧАТЬ: только символы, проценты, агрегаты.
ЧЕСТНОЕ ОКНО: любая аналитика resolved.csv — только run_ts ≥ 2026-06-02
(до этой даты r_multiple отравлен look-ahead — правило брата, A3 KB).

Запуск: python3 dashboard_feed.py [--no-push]
Launchd: каждые 60с; пуш пропускается, если feed не изменился по содержанию.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DIR = Path(__file__).resolve().parent
TRADING = DIR.parent
sys.path.insert(0, str(TRADING))   # file_lock, bias-зависимости живут в ~/trading
OUT = TRADING / "outcomes"
SITE_FEED = DIR / "site" / "feed.json"
STATE_PATH = DIR / "feed_push_state.json"

HONEST_WINDOW_START = "2026-06-02"  # честное окно resolved.csv (A3 KB)
MIN_N = 25                          # n<25 → «мало данных»
STATE_REPO = "nsudiyan/mirofish-state"  # существующий публичный state-репо; отдельный — поменять тут
STATE_FILE = "trading_feed.json"

LIVE_WINDOW_H = 48    # сигналы моложе 48ч считаем «живыми» для ленты
FEED_SIGNALS_MAX = 200


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_naive_utc(ts: str) -> datetime | None:
    """Таймстампы пайплайна naive = UTC; aware приводим к UTC."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def jload(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# ─── Живая лента сигналов ────────────────────────────────────────────────────

def collect_live_signals() -> list[dict]:
    """Union всех источников сигналов за последние LIVE_WINDOW_H часов.
    Каждый: id, ts_utc, source, symbol, direction, entry/stop/tp (если есть).
    Направление нормализуем в long/short. Фронт сам считает live-отработку
    по Bybit от ts_utc/entry."""
    now = utcnow()
    cutoff = now - timedelta(hours=LIVE_WINDOW_H)
    sigs: dict[str, dict] = {}

    def add(sid, ts, source, symbol, direction, **extra):
        if ts is None or ts < cutoff or not symbol:
            return
        d = (direction or "").strip().lower()
        d = {"лонг": "long", "шорт": "short", "long": "long", "short": "short",
             "buy": "long", "sell": "short", "up": "long", "down": "short",
             "bullish": "long", "bearish": "short"}.get(d, d or None)
        sigs[sid] = {"id": sid, "ts_utc": ts.isoformat(), "source": source,
                     "symbol": symbol, "direction": d,
                     **{k: v for k, v in extra.items() if v is not None}}

    # 1) pending скринера (channel_conf = каналы, подтвердившие сигнал)
    for e in jload(OUT / "pending.json", []):
        ts = parse_naive_utc(e.get("run_ts", ""))
        add(f"bot:{e.get('symbol')}:{e.get('run_ts','')[:16]}", ts, "screener",
            e.get("symbol"), e.get("direction"),
            entry=e.get("price_entry"), stop=e.get("stop"), tp1=e.get("tp1"),
            setup=e.get("setup"), score=e.get("score"), grade=e.get("grade"),
            channels=e.get("channel_conf") or None)

    # 2) доставленные алерты (alerts_index)
    for k, e in jload(OUT / "alerts_index.json", {}).items():
        ts = parse_naive_utc(e.get("run_ts", ""))
        add(f"alert:{k}", ts, "alert", e.get("symbol"), e.get("direction"),
            entry=e.get("entry"), stop=e.get("sl"), tp1=e.get("tp"),
            setup=e.get("setup"), verdict=e.get("verdict"), status=e.get("status"))

    # 3) storm ignites (направление в details.dir, если есть)
    try:
        with open(OUT / "storm_stages.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("stage") != "ignite":
                    continue
                ts = parse_naive_utc(r.get("ts_utc", ""))
                det = {}
                try:
                    det = json.loads(r.get("details") or "{}")
                except Exception:
                    pass
                # storm_radar пишет направление ключом "side" (up/down), не "dir"
                add(f"storm:{r['symbol']}:{r['ts_utc'][:16]}", ts, "storm",
                    r.get("symbol"), det.get("dir") or det.get("side"),
                    entry=float(r["price"]) if r.get("price") else None)
    except FileNotFoundError:
        pass

    # 4) radar hits (dir появляется в radar_resolved; в hits только всплеск)
    try:
        with open(OUT / "radar_hits.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                ts = parse_naive_utc(r.get("ts_utc", ""))
                add(f"radar:{r['symbol']}:{r['ts_utc'][:16]}", ts, "radar",
                    r.get("symbol"), None,
                    entry=float(r["price"]) if r.get("price") else None,
                    vol_ratio=float(r["vol_ratio"]) if r.get("vol_ratio") else None)
    except FileNotFoundError:
        pass

    # 5) ТГК-сигналы Rose-каналов — из rose_history.json (Telethon): ТОЧНОЕ
    # время поста (msg.date) и стабильный msg_id. Раньше брали mtime кэша
    # скана — это время СКАНЕРА (каденс ~4ч, бывает протухшим на дни): якорь
    # трека и окна 6ч/24ч мерялись от чужого момента, а карточка «молодела»
    # при каждом переске (код-ревью 2026-07-06 #3). Свежесть: rose_history
    # дочитывает посты каждые ~15 мин (refresh_outcomes).
    rose_store = jload(DIR / "rose_history.json", {"tracks": []})
    for t in rose_store.get("tracks", []):
        for a in t.get("alerts", []):
            ts = None
            try:
                ts = datetime.fromisoformat(a["ts_utc"]).astimezone(timezone.utc)
            except (KeyError, ValueError):
                pass
            add(f"tg:{a.get('channel')}:{a.get('msg_id')}", ts, "tg_channel",
                t.get("symbol"), t.get("direction"), channel=a.get("channel"),
                note=(a.get("note") or "")[:140] or None)

    # Схлопываем повторные эмиссии ОДНОГО сигнала в одну карточку: скринер
    # каждые ~4ч пере-эмитит живой сетап с новым run_ts, radar/storm — при
    # каждом всплеске. Ключ логического сигнала = source+symbol+direction+
    # setup/channel; оставляем САМУЮ СВЕЖУЮ эмиссию, считаем их число (emits).
    def logical_key(s):
        return (s["source"], s["symbol"], s.get("direction"),
                s.get("setup"), s.get("channel"))
    collapsed: dict = {}
    for s in sorted(sigs.values(), key=lambda s: s["ts_utc"]):
        k = logical_key(s)
        prev = collapsed.get(k)
        s["emits"] = (prev["emits"] if prev else 0) + 1
        collapsed[k] = {**s, "emits": s["emits"]}  # новее перезаписывает
    out = sorted(collapsed.values(), key=lambda s: s["ts_utc"], reverse=True)
    return out[:FEED_SIGNALS_MAX]


# ─── Аналитика бота (resolved.csv, честное окно) ─────────────────────────────

def bot_stats() -> dict:
    path = OUT / "resolved.csv"
    try:
        rows = [r for r in csv.DictReader(open(path, encoding="utf-8"))
                if (r.get("run_ts") or "") >= HONEST_WINDOW_START]
    except FileNotFoundError:
        return {}

    def fnum(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def is_win(r, hz):  # как в outcome_tracker._update_channel_accuracy
        return (r.get(f"outcome_{hz}") or "") in ("TP1", "WIN")

    by_setup: dict[str, dict] = {}
    by_dir: dict[str, dict] = {}
    equity = []
    cum_r = 0.0
    recent = []
    for r in sorted(rows, key=lambda x: x.get("run_ts", "")):
        setup = r.get("setup") or "?"
        d = (r.get("direction") or "?").strip().lower()
        d = {"лонг": "long", "шорт": "short"}.get(d, d)
        for key, bucket in ((setup, by_setup), (d, by_dir)):
            b = bucket.setdefault(key, {"n": 0, "wins4": 0, "wins24": 0, "r_sum": 0.0, "r_n": 0})
            b["n"] += 1
            b["wins4"] += int(is_win(r, "4h"))
            b["wins24"] += int(is_win(r, "24h"))
            rm = fnum(r.get("r_multiple_24h"))
            if rm is not None:
                b["r_sum"] += rm
                b["r_n"] += 1
        rm = fnum(r.get("r_multiple_24h"))
        if rm is not None:
            cum_r += rm
            equity.append({"ts": r.get("run_ts", "")[:16], "cum_r": round(cum_r, 2)})
        recent.append({
            "ts": r.get("run_ts", "")[:16], "symbol": r.get("symbol"),
            "setup": setup, "direction": d, "outcome_4h": r.get("outcome_4h"),
            "outcome_24h": r.get("outcome_24h"),
            "change_24h_pct": fnum(r.get("change_24h_pct")),
            "r_24h": rm,
            "mfe_24h": fnum(r.get("mfe_24h_pct")), "mae_24h": fnum(r.get("mae_24h_pct")),
        })

    def pack(bucket):
        out = {}
        for k, b in bucket.items():
            out[k] = {
                "n": b["n"],
                "wr4h_pct": round(b["wins4"] / b["n"] * 100, 1),
                "wr24h_pct": round(b["wins24"] / b["n"] * 100, 1),
                "avg_r24": round(b["r_sum"] / b["r_n"], 3) if b["r_n"] else None,
                "low_n": b["n"] < MIN_N,
            }
        return out

    # equity прореживаем до ≤300 точек (первая/последняя сохраняются)
    if len(equity) > 300:
        step = len(equity) / 300
        equity = [equity[int(i * step)] for i in range(300)] + [equity[-1]]

    return {
        "window_start": HONEST_WINDOW_START,
        "n": len(rows),
        "by_setup": pack(by_setup),
        "by_direction": pack(by_dir),
        "equity_r": equity,
        "recent": list(reversed(recent[-50:])),
    }


# ─── Storm / Radar ───────────────────────────────────────────────────────────

def storm_state() -> dict:
    wl = jload(OUT / "storm_watchlist.json", {})

    def rnd(v, n):  # в watchlist поля бывают null (OI-история протухла после сна мака)
        return round(v, n) if isinstance(v, (int, float)) else None

    watch = []
    for sym, d in wl.items():
        watch.append({
            "symbol": sym,
            "added_ts": datetime.fromtimestamp(d["added_ts"], tz=timezone.utc).isoformat()
            if d.get("added_ts") else None,
            "price_at_add": d.get("price_at_add"),
            "box_width_pct": rnd(d.get("box_width_pct"), 2),
            "compress_p": d.get("compress_p"),
            "oi_chg_4h": rnd(d.get("oi_chg_4h"), 2),
            "funding": d.get("funding"),
        })
    watch.sort(key=lambda w: w.get("added_ts") or "", reverse=True)
    return {"watchlist": watch}


def radar_stats() -> dict:
    try:
        rows = list(csv.DictReader(open(OUT / "radar_resolved.csv", encoding="utf-8")))
    except FileNotFoundError:
        return {}
    n = len(rows)
    good = sum(1 for r in rows if r.get("good") == "1")
    mfes = [float(r["mfe_pct"]) for r in rows if r.get("mfe_pct")]
    return {
        "n": n, "good": good,
        "good_pct": round(good / n * 100, 1) if n else None,
        "avg_mfe_pct": round(sum(mfes) / len(mfes), 2) if mfes else None,
        "low_n": n < MIN_N,
    }


# ─── Сборка и пуш ────────────────────────────────────────────────────────────

ROSE_CHANNELS = {"rose", "RoseSignalsPremium"}  # единственные ТГК-каналы на дашборде (решение брата 2026-07-03)
LEDGER_PATH = DIR / "signals_ledger.json"
LEDGER_CONFIRM_H = 6.0   # повтор ≤6ч (тот же symbol+direction+source) = подтверждение, якорь суток заново


def update_ledger(live_signals: list[dict]) -> dict:
    """Накапливающаяся история сигналов: каждый live-сигнал попадает в трек
    (формат как rose_history), итог за сутки резолвится refresh_outcomes.py.
    Пишем ТОЛЬКО под file_lock (atomic_json_update): параллельный dashresolve
    мержит свои outcome в этот же файл (гонка — код-ревью 2026-07-06 #2)."""
    from file_lock import atomic_json_update

    def mutate(ledger):
        ledger = ledger if isinstance(ledger, dict) else {}
        ledger.setdefault("tracks", [])
        seen_list = list(ledger.get("seen_ids") or [])
        seen = set(seen_list)
        tracks = ledger["tracks"]
        for s in sorted(live_signals, key=lambda x: x["ts_utc"]):
            if s["id"] in seen:
                continue
            seen.add(s["id"])
            seen_list.append(s["id"])
            d = s.get("direction")
            ts = datetime.fromisoformat(s["ts_utc"])
            best = None
            for t in tracks:
                if (t["symbol"], t["direction"], t.get("source")) != (s["symbol"], d, s["source"]):
                    continue
                last = datetime.fromisoformat(t["anchor_ts"])
                if timedelta(0) <= ts - last <= timedelta(hours=LEDGER_CONFIRM_H):
                    best = t
                    break
            alert = {"id": s["id"], "ts_utc": s["ts_utc"]}
            if best is not None:
                best["alerts"].append(alert)
                best["confirms"] = len(best["alerts"])
                best["anchor_ts"] = s["ts_utc"]  # итог суток — заново от подтверждения
                best["outcome"] = None
            else:
                tracks.append({
                    "id": f"{s['source']}:{s['symbol']}:{d or 'delta'}:{s['ts_utc'][:16]}",
                    "symbol": s["symbol"], "direction": d, "source": s["source"],
                    "setup": s.get("setup"), "channel": s.get("channel"),
                    "first_ts": s["ts_utc"], "anchor_ts": s["ts_utc"],
                    "confirms": 1, "alerts": [alert], "outcome": None,
                    # предрегистрация наклона: снапшот при РОЖДЕНИИ трека, не переписывается
                    "bias_at_birth": s.get("bias"),
                })
        ledger["seen_ids"] = seen_list[-5000:]  # кап в порядке добавления
        tracks.sort(key=lambda t: t["anchor_ts"], reverse=True)
        ledger["tracks"] = tracks[:2000]
        return ledger

    return atomic_json_update(LEDGER_PATH, mutate,
                              default={"tracks": [], "seen_ids": []})


def _track_row(t: dict) -> dict:
    """Компактная строка трека для фида."""
    o = t.get("outcome") or {}
    return {
        "symbol": t["symbol"], "direction": t.get("direction"),
        "source": t.get("source"), "channel": t.get("channel"),
        "setup": t.get("setup"),
        "first_ts": t["first_ts"], "anchor_ts": t["anchor_ts"],
        "confirms": t.get("confirms", 1),
        "status": o.get("status"), "final": bool(o.get("final")),
        "peak6_pct": o.get("peak6_pct"), "dd6_pct": o.get("dd6_pct"),
        "peak24_pct": o.get("peak24_pct"), "dd24_pct": o.get("dd24_pct"),
        "ret24_pct": o.get("ret24_pct"),
    }


def rose_block() -> dict:
    """История и анализ сигналов Rose-каналов (rose_history.json)."""
    store = jload(DIR / "rose_history.json", {"tracks": []})
    tracks = sorted(store["tracks"], key=lambda t: t["anchor_ts"], reverse=True)
    ok = [t for t in tracks
          if (t.get("outcome") or {}).get("status") == "ok" and t["outcome"].get("final")]
    n = len(ok)
    summary = {
        "n_tracks": len(tracks),
        "n_final": n,
        "hit24_pct": round(sum(1 for t in ok if t["outcome"]["ret24_pct"] > 0) / n * 100, 1) if n else None,
        "avg_peak24_pct": round(sum(t["outcome"]["peak24_pct"] for t in ok) / n, 2) if n else None,
        "avg_dd24_pct": round(sum(t["outcome"]["dd24_pct"] for t in ok) / n, 2) if n else None,
        "avg_confirms": round(sum(t.get("confirms", 1) for t in tracks) / len(tracks), 1) if tracks else None,
        "low_n": n < MIN_N,
    }
    acc = jload(TRADING / "channel_accuracy.json", {})
    bot_acc = {ch: acc[ch] for ch in acc if ch in ROSE_CHANNELS}
    return {"summary": summary, "bot_accuracy": bot_acc,
            "tracks": [_track_row(t) for t in tracks[:150]]}


def pump_watch_block() -> list[dict]:
    """Монеты под pump-надзором (storm_radar/pump_watch): эпизоды для секции и бейджей."""
    st = jload(OUT / "pump_watch_state.json", {})
    out = []
    for s, d in st.items():
        det = d.get("detected_ts")
        out.append({
            "symbol": s, "kind": d.get("kind"),
            "dist": bool(d.get("dist_sent")), "broke": bool(d.get("break_sent")),
            "rise_pct": d.get("rise_pct"), "peak": d.get("peak"),
            "plateau_low": d.get("plateau_low"),
            "detected_ts": datetime.fromtimestamp(det, tz=timezone.utc).isoformat() if det else None,
        })
    out.sort(key=lambda x: x.get("detected_ts") or "", reverse=True)
    return out


def pump_muted_block(limit: int = 50) -> list[dict]:
    """Лонги скринера, заглушенные PumpWatchGate (форензика rejected_history.csv)."""
    path = OUT / "rejected_history.csv"
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("reject_gate") != "PumpWatchGate":
                    continue
                rows.append({
                    "ts": r.get("ts", "")[:16], "symbol": r.get("symbol"),
                    "setup": r.get("setup"), "score": r.get("score_pre_gate"),
                    "reason": r.get("reject_reason"),
                })
    except FileNotFoundError:
        pass
    return rows[-limit:][::-1]


def _enrich_bias(live: list[dict]) -> None:
    """Наклон структуры (bias v2, side_study 2026-07-05): radar → up,
    Rose-посты — контрарно. Без сети и тяжёлых данных. Fail-open."""
    try:
        import sys
        sys.path.insert(0, str(TRADING / "dashboard"))
        from bias import compute_bias_v2, RADAR_MAJORS
        for s in live:
            b = compute_bias_v2(s.get("source"), s.get("direction"),
                                s.get("channel"), s.get("symbol"))
            if b:
                s["bias"] = b
            # радар-мажор: сам vol_radar такое одиночкой не доставляет (3/96),
            # ход ≥5% у 2/19 — фронт эти карточки прячет (в ledger живут)
            if s.get("source") == "radar" and s.get("symbol") in RADAR_MAJORS:
                s["major_radar"] = True
    except Exception as e:
        print(f"[feed] bias enrich skipped: {e}")


# ─── Форензика «Вход имеет смысл сейчас» ─────────────────────────────────────
# Правила = КОПИЯ фронтовых (site/app.js renderEntry) — менять СИНХРОННО.
# Кандидат логируется ОДИН раз при первом прохождении правил: форвард потом
# меряет «вход при первом появлении в секции» (просьба брата 2026-07-06).
ENTRY_LOG_PATH = OUT / "entry_candidates.csv"
ENTRY_SEEN_PATH = DIR / "entry_logged.json"
ENTRY_FIELDS = ["logged_ts_utc", "symbol", "signal_ts_utc", "kind", "age_h",
                "basis", "last_pct", "peak_pct", "dd_pct", "vol_ratio"]


def _fetch_5m_closed(symbol: str, start_ms: int) -> list:
    """Закрытые 5м бары от start_ms: (t,o,h,l,c). Как fetch_15m, но 5м."""
    import urllib.request
    now_ms = int(utcnow().timestamp() * 1000)
    url = (f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}"
           f"&interval=5&start={start_ms}&end={now_ms}&limit=1000")
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.load(r)
    if data.get("retCode") != 0:
        raise RuntimeError(f"retCode={data.get('retCode')}")
    rows = data.get("result", {}).get("list") or []
    rows.reverse()
    bar = 5 * 60_000
    return [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]))
            for x in rows
            if int(x[0]) + bar <= now_ms]


def log_entry_candidates(live: list[dict]) -> None:
    """Серверный двойник фронт-отбора: radar-альт ⬆ v3, свежий, цена не убежала,
    без пилы, не в раздаче; 🌅 (vol_ratio≥15) — окно 30ч и коридор шире.
    Fail-open: сбой не трогает сборку фида."""
    try:
        import urllib.request
        seen = set(jload(ENTRY_SEEN_PATH, []))
        pumps = {p["symbol"]: p for p in pump_watch_block()}
        now = utcnow()

        pre = []
        for s in live:
            if s.get("source") != "radar" or s.get("major_radar"):
                continue
            if (s.get("bias") or {}).get("side") != "up":
                continue
            p = pumps.get(s["symbol"])
            if p and (p.get("dist") or p.get("broke")):
                continue
            age_h = (now - datetime.fromisoformat(s["ts_utc"])).total_seconds() / 3600
            is_awk = (s.get("vol_ratio") or 0) >= 15
            if age_h > (30 if is_awk else 3):
                continue
            key = f"{s['symbol']}|{s['ts_utc'][:16]}"
            if key in seen:
                continue
            pre.append((s, age_h, is_awk, key))
        if not pre:
            return

        with urllib.request.urlopen(
                "https://api.bybit.com/v5/market/tickers?category=linear", timeout=10) as r:
            tick = {t["symbol"]: float(t.get("lastPrice") or 0)
                    for t in json.load(r)["result"]["list"]}

        new_rows = []
        for s, age_h, is_awk, key in pre[:12]:            # кап запросов за тик
            last = tick.get(s["symbol"]) or 0
            if last <= 0:
                continue
            try:
                sig_ms = int(datetime.fromisoformat(s["ts_utc"]).timestamp() * 1000)
                bars = _fetch_5m_closed(s["symbol"], sig_ms)
            except Exception:
                continue
            bars = [b for b in bars if b[0] >= ((sig_ms // 300_000) + 1) * 300_000]
            if not bars:
                continue
            basis = bars[0][4]
            if basis <= 0:
                continue
            post = bars[1:]
            last_pct = (last - basis) / basis * 100
            peak = max([(b[2] - basis) / basis * 100 for b in post] + [last_pct, 0])
            dd = min([(b[3] - basis) / basis * 100 for b in post] + [last_pct, 0])
            ok = (age_h <= 30 and -3 <= last_pct <= 5 and dd >= -4) if is_awk else \
                 (last_pct >= -1.5 and last_pct <= 2.5 and dd >= -2 and peak <= 3.5)
            if not ok:
                continue
            new_rows.append({
                "logged_ts_utc": now.replace(microsecond=0).isoformat(),
                "symbol": s["symbol"], "signal_ts_utc": s["ts_utc"],
                "kind": "awakening" if is_awk else "radar_alt",
                "age_h": round(age_h, 2), "basis": basis,
                "last_pct": round(last_pct, 2), "peak_pct": round(peak, 2),
                "dd_pct": round(dd, 2), "vol_ratio": s.get("vol_ratio"),
            })
            seen.add(key)

        if new_rows:
            write_header = not ENTRY_LOG_PATH.exists()
            with open(ENTRY_LOG_PATH, "a", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=ENTRY_FIELDS)
                if write_header:
                    w.writeheader()
                w.writerows(new_rows)
            ENTRY_SEEN_PATH.write_text(json.dumps(sorted(seen)[-3000:]))
            print(f"[feed] entry-кандидатов залогировано: {len(new_rows)}: "
                  f"{[r['symbol'] for r in new_rows]}")
    except Exception as e:
        print(f"[feed] entry-лог пропущен: {e}")


def bias_accuracy(ledger: dict) -> dict:
    """Форвард-экзамен наклона: bias_at_birth vs ret24 финальных треков."""
    try:
        import sys
        sys.path.insert(0, str(TRADING))
        from bias import grade_bias
    except Exception:
        return {}
    per_v: dict[str, dict] = {}
    for t in ledger.get("tracks", []):
        bb = t.get("bias_at_birth")
        o = t.get("outcome") or {}
        if not bb or o.get("status") != "ok" or not o.get("final"):
            continue
        # grade_bias ждёт МИРОВОЙ ret24 (рост цены = плюс), а в треках ret24_pct
        # записан В СТОРОНУ сигнала (resolve_tracks: ×(−1) для short) — без
        # конвертации все Rose-шорт-оценки зеркалятся (код-ревью 2026-07-06 #1)
        ret_world = o.get("ret24_pct")
        if ret_world is not None and t.get("direction") == "short":
            ret_world = -ret_world
        g = grade_bias(bb.get("side", ""), ret_world)
        c = per_v.setdefault(bb.get("v", "?"), {"hits": 0, "misses": 0, "flat_zone": 0})
        if g == "hit":
            c["hits"] += 1
        elif g == "miss":
            c["misses"] += 1
        elif g == "flat_zone":
            c["flat_zone"] += 1
    out = {}
    for v, c in per_v.items():
        graded = c["hits"] + c["misses"]
        out[v] = {**c, "n_graded": graded,
                  "accuracy_pct": round(c["hits"] / graded * 100, 1) if graded else None,
                  "low_n": graded < MIN_N}
    return out


def combos_block() -> list[dict]:
    """⚡ Связки «пробуждение × Rose-пост × структура жива» (лид 2026-07-06,
    n=5: посты с радар-хитом ≤72ч до отрабатывают ×2 лучше — VANRY +146%).
    Показ, пока Rose-пост свежее 48ч. НЕ торговое правило — визуальная сводка."""
    try:
        from bias import RADAR_MAJORS
        hits = []
        with open(TRADING / "outcomes" / "radar_hits.csv", encoding="utf-8") as f:
            for row in csv.reader(f):
                try:
                    ts = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
                    hits.append((row[1], ts, float(row[2])))
                except Exception:
                    continue
        now = utcnow()
        pumps = {p["symbol"]: p for p in pump_watch_block()}
        out = []
        for t in jload(DIR / "rose_history.json", {"tracks": []})["tracks"]:
            post_ts = datetime.fromisoformat(t["anchor_ts"])
            if (now - post_ts).total_seconds() > 48 * 3600:
                continue
            sym = t["symbol"]
            if sym in RADAR_MAJORS:
                continue
            awake = [(ts, r) for s, ts, r in hits
                     if s == sym and 0 <= (post_ts - ts).total_seconds() <= 72 * 3600 and r >= 5]
            if not awake:
                continue
            p = pumps.get(sym)
            if p and (p.get("dist") or p.get("broke")):
                continue          # раздача/слом = связка мертва (просьба брата: удалять)
            o = t.get("outcome") or {}
            peak, ret = o.get("peak24_pct"), o.get("ret24_pct")
            # финальный трек, отдавший ≥80% пика при пике ≥8% — поезд ушёл
            if o.get("final") and peak and peak >= 8 and ret is not None and (peak - ret) / peak >= 0.8:
                continue
            aw_ts, aw_r = max(awake, key=lambda x: x[1])
            out.append({
                "symbol": sym, "direction": t.get("direction"),
                "awake_ts": aw_ts.isoformat(), "awake_ratio": aw_r,
                "post_ts": t["anchor_ts"], "channel": (t.get("alerts") or [{}])[-1].get("channel"),
                "basis": o.get("basis"),
                "peak24_pct": peak, "ret24_pct": ret,
            })
        out.sort(key=lambda x: x["post_ts"], reverse=True)
        return out
    except Exception as e:
        print(f"[feed] combos пропущен: {e}")
        return []


def build_feed() -> dict:
    live = collect_live_signals()
    _enrich_bias(live)
    log_entry_candidates(live)   # форензика шорт-листа «вход сейчас»
    ledger = update_ledger(live)
    return {
        "pump_watch": pump_watch_block(),
        "pump_muted": pump_muted_block(),
        "combos": combos_block(),
        "bias_accuracy": bias_accuracy(ledger),
        "generated_at": utcnow().isoformat(),
        "honest_window_note": f"аналитика бота: только сигналы с {HONEST_WINDOW_START} (правило честного окна)",
        "live_signals": live,
        "rose": rose_block(),
        "signals_history": [_track_row(t) for t in ledger["tracks"][:150]],
        "bot_stats": bot_stats(),
        "storm": storm_state(),
        "radar": radar_stats(),
    }


def push_github(payload: str) -> bool:
    """PUT feed.json в STATE_REPO через gh CLI (auth из keyring)."""
    # sha текущего файла (нужен для перезаписи)
    sha = None
    r = subprocess.run(["gh", "api", f"repos/{STATE_REPO}/contents/{STATE_FILE}",
                        "--jq", ".sha"], capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        sha = r.stdout.strip() or None
    body = {
        "message": "feed update",
        "content": base64.b64encode(payload.encode()).decode(),
    }
    if sha:
        body["sha"] = sha
    r = subprocess.run(
        ["gh", "api", "-X", "PUT", f"repos/{STATE_REPO}/contents/{STATE_FILE}",
         "--input", "-"],
        input=json.dumps(body), capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"[feed] push FAIL: {r.stderr[:300]}", file=sys.stderr)
        return False
    return True


def content_fingerprint(feed: dict) -> str:
    """Хэш содержания без generated_at — чтобы не пушить пустые обновления."""
    d = dict(feed)
    d.pop("generated_at", None)
    return hashlib.sha1(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    feed = build_feed()
    payload = json.dumps(feed, ensure_ascii=False, separators=(",", ":"))
    SITE_FEED.write_text(payload, encoding="utf-8")
    print(f"[feed] built: {len(payload)//1024}KB, live={len(feed['live_signals'])}, "
          f"rose={len(feed['rose']['tracks'])}, history={len(feed['signals_history'])}")

    if args.no_push:
        return 0

    state = jload(STATE_PATH, {})
    fp = content_fingerprint(feed)
    now = utcnow()
    last_push = parse_naive_utc(state.get("last_push_ts", ""))
    # пуш если содержание изменилось ИЛИ прошло >10 мин (heartbeat свежести)
    stale = last_push is None or (now - last_push) > timedelta(minutes=10)
    if state.get("fingerprint") == fp and not stale:
        print("[feed] без изменений — пуш пропущен")
        return 0
    if push_github(payload):
        STATE_PATH.write_text(json.dumps(
            {"fingerprint": fp, "last_push_ts": now.isoformat()}))
        print("[feed] pushed → GitHub")
    return 0


if __name__ == "__main__":
    sys.exit(main())

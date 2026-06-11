"""
reject_tracker.py — tracks gate-rejected signals and measures their future performance.

Each gate in screener.py blocks a signal. We record what was blocked, then at
1h / 4h / 12h we fetch price and measure where the market moved.

  Gate is "excellent"  if ≤35% of blocked signals would have worked
  Gate is "ok"         if 35-50% would have worked
  Gate is "watch"      if 50-60% would have worked (review threshold)
  Gate is "too_strict" if >60% would have worked (gate is blocking winners)

CLI:
  python3 reject_tracker.py stats          — per-gate analytics table
  python3 reject_tracker.py resolve        — update future prices now
  python3 reject_tracker.py list [gate]    — show recent rejects
  python3 reject_tracker.py seed           — залить rejected.json в персист-сток (идемпотентно)
  python3 reject_tracker.py clear          — wipe reject log (confirm required)

Персистентный сток: outcomes/rejected_history.csv (A2, 2026-06-08) — append-only,
upsert по ключу минута|symbol|gate, контрфакты не теряются при rolling-срезе rejected.json.
"""

import csv
import json
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_DIR     = Path(__file__).parent / "outcomes"
REJECT_FILE  = BASE_DIR / "rejected.json"
BASE_URL     = "https://api.bybit.com"
MAX_REJECTS  = 3000   # rolling window cap

# A2 (2026-06-08): персистентный append-only сток реджектов с контрфактами.
# rejected.json — rolling 3000 (~5.6 дн), старое затирается; это хранилище НЕ теряет
# историю (upsert по ключу, контрфакты обновляются по мере дозревания). НЕ ломает живую
# логику rejected.json. Источник правды для форензики №2 на полном окне.
HISTORY_FILE = BASE_DIR / "rejected_history.csv"
HISTORY_COLS = [
    "ts", "symbol", "setup", "direction", "reject_gate", "reject_reason",
    "score_pre_gate", "price_at_reject",
    "oi_regime", "fund_regime", "htf_trend", "grade", "vwap_dev", "rsi_1h", "bq_score",
    "narrative", "narr_conf",
    # контрфакты — СЫРЫЕ ценовые % (не costed-R; костинга для реджектов нет)
    "future_1h", "future_4h", "future_12h",
    "move_1h_pct", "move_4h_pct", "move_12h_pct",
]
_HIST_INT_COLS   = {"score_pre_gate"}
_HIST_FLOAT_COLS = {"price_at_reject", "vwap_dev", "rsi_1h", "bq_score", "narr_conf",
                    "future_1h", "future_4h", "future_12h",
                    "move_1h_pct", "move_4h_pct", "move_12h_pct"}

# P0-1 (2026-06-06): per-run счётчик отказов по гейтам — для funnel-строки
# в конце прогона скринера. In-memory, обнуляется с процессом.
RUN_REJECT_COUNTS = Counter()


# ─────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────

def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_ts(ts_str: str) -> datetime:
    # ts хранится как UTC wall-clock (_now_ts). Парсим aware: иначе вычитание с
    # aware-now кидает TypeError, а .timestamp() на MSK-хосте сдвигает окно на −3ч.
    return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def _load_rejects() -> list:
    if not REJECT_FILE.exists():
        return []
    try:
        return json.loads(REJECT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_rejects(entries: list):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    REJECT_FILE.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ─────────────────────────────────────────────────────────────
# A2: персистентный сток истории реджектов (rejected_history.csv)
# ─────────────────────────────────────────────────────────────

def _history_key(e: dict) -> str:
    """Ключ апсерта = минута|symbol|reject_gate — 1:1 с живым дедупом log_reject."""
    return f"{(e.get('ts') or '')[:16]}|{e.get('symbol','')}|{e.get('reject_gate','')}"


def _entry_to_history_row(e: dict) -> dict:
    """Запись реджекта → CSV-строка (str-значения; None → '')."""
    return {c: ("" if e.get(c) is None else str(e.get(c))) for c in HISTORY_COLS}


def _cast_history_row(row: dict) -> dict:
    """CSV-строка → типизированный dict (числа из str, '' → None) для аналитики."""
    out = {c: (row.get(c) if row.get(c) not in ("", None) else None) for c in HISTORY_COLS}
    for k in _HIST_INT_COLS:
        if out.get(k) is not None:
            try: out[k] = int(float(out[k]))
            except (TypeError, ValueError): out[k] = None
    for k in _HIST_FLOAT_COLS:
        if out.get(k) is not None:
            try: out[k] = float(out[k])
            except (TypeError, ValueError): out[k] = None
    return out


def _read_history_rows() -> dict:
    """{key: raw_str_row} из CSV (без кастинга — для апсерта/сравнения)."""
    if not HISTORY_FILE.exists():
        return {}
    out = {}
    try:
        with open(HISTORY_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[_history_key(row)] = {c: row.get(c, "") for c in HISTORY_COLS}
    except Exception:
        return out
    return out


def _write_history_rows(rows_by_key: dict):
    """Атомарная перезапись CSV (tmp + replace), порядок по ts."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_FILE.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_COLS, extrasaction="ignore")
        w.writeheader()
        for key in sorted(rows_by_key, key=lambda k: rows_by_key[k].get("ts", "")):
            w.writerow(rows_by_key[key])
    tmp.replace(HISTORY_FILE)


def persist_history(entries: list) -> tuple:
    """Upsert реджектов в rejected_history.csv (append-only сток, без roll-off).
    Ключ = минута|symbol|reject_gate. Контрфакты обновляются при дозревании.
    Идемпотентно: повторный вызов с теми же данными ничего не пишет.
    Возвращает (новых, обновлённых). Ошибка стока НЕ ломает живую логику реджектов."""
    if not entries:
        return (0, 0)
    try:
        from file_lock import _file_lock
        with _file_lock(HISTORY_FILE):
            existing = _read_history_rows()
            n_new = n_upd = 0
            for e in entries:
                key = _history_key(e)
                new_row = _entry_to_history_row(e)
                old = existing.get(key)
                if old is None:
                    existing[key] = new_row; n_new += 1
                elif old != new_row:
                    existing[key] = new_row; n_upd += 1
            if n_new or n_upd:
                _write_history_rows(existing)
            return (n_new, n_upd)
    except Exception as ex:
        print(f"[reject_history] persist error (rejected.json не затронут): {ex}")
        return (0, 0)


def load_history(include_live: bool = True) -> list:
    """Полная выборка реджектов с контрфактами (history CSV + свежий rejected.json),
    типизированная, дедуп по ключу. Приоритет — history; live добавляет лишь ещё не
    персистнутые свежие записи. Для форензики №2 на ПОЛНОМ окне (не rolling 5.6 дн)."""
    merged = {}
    for key, row in _read_history_rows().items():
        merged[key] = _cast_history_row(row)
    if include_live:
        for e in _load_rejects():
            key = _history_key(e)
            if key not in merged:
                merged[key] = e
    return list(merged.values())


def _direction_from_result(r: dict) -> str:
    setup = r.get("setup", "")
    if setup in ("squeeze", "breakout"):
        return "ЛОНГ"
    if setup == "short_dist":
        return "ШОРТ"
    if setup == "range_sweep":
        sd = r.get("sweep_dir")
        if sd == "long":  return "ЛОНГ"
        if sd == "short": return "ШОРТ"
        return "ЛОНГ" if (r.get("cvd_k%", 0) or 0) > 0 else "ШОРТ"
    bull = r.get("bull_mtf_ext", r.get("mtf_b", 0))
    bear = r.get("bear_mtf_ext", r.get("mtf_s", 0))
    return "ЛОНГ" if bull >= bear else "ШОРТ"


# ─────────────────────────────────────────────────────────────
# Core API
# ─────────────────────────────────────────────────────────────

def log_reject(r: dict, reject_gate: str, reject_reason: str):
    """
    Log a signal rejected by a gate.
    r = full screener result dict (symbol, setup, score, price, context fields).
    Thread-safe via file reload/save per call.
    """
    symbol  = r.get("symbol", "?")
    now_ts  = _now_ts()
    now_key = now_ts[:16]

    entries = _load_rejects()

    # Dedup: same symbol + gate within the same minute
    existing_keys = {(e["symbol"], e["reject_gate"], e["ts"][:16]) for e in entries}
    if (symbol, reject_gate, now_key) in existing_keys:
        return

    RUN_REJECT_COUNTS[reject_gate] += 1   # P0-1: funnel-счётчик текущего прогона
    direction = _direction_from_result(r)

    entry = {
        "ts":              now_ts,
        "symbol":          symbol,
        "setup":           r.get("setup", "—"),
        "score_pre_gate":  r.get("score", 0),
        "reject_gate":     reject_gate,
        "reject_reason":   reject_reason,
        "direction":       direction,
        "price_at_reject": r.get("price", 0),
        # context fields
        "oi_regime":   r.get("oi_regime", ""),
        "fund_regime": r.get("fund_regime", ""),
        "htf_trend":   r.get("h4_htf", r.get("d_htf", "")),
        "narrative":   r.get("narrative", ""),
        "narr_conf":   r.get("narr_conf", 0.0),
        "grade":       r.get("grade", ""),
        "vwap_dev":    r.get("vwap_dev"),
        "rsi_1h":      r.get("rsi_1h"),
        "bq_score":    r.get("bq_score"),
        # future prices resolved later
        "future_1h":   None,
        "future_4h":   None,
        "future_12h":  None,
        "move_1h_pct":  None,
        "move_4h_pct":  None,
        "move_12h_pct": None,
    }

    entries.append(entry)
    if len(entries) > MAX_REJECTS:
        entries = entries[-MAX_REJECTS:]
    _save_rejects(entries)


def _fetch_price_at_horizon(symbol: str, target_dt: datetime) -> float | None:
    """Return close price of the 15m bar closest to target_dt."""
    try:
        resp = requests.get(
            f"{BASE_URL}/v5/market/kline",
            params={
                "category": "linear",
                "symbol":   symbol,
                "interval": "15",
                "start":    int((target_dt - timedelta(minutes=15)).timestamp() * 1000),
                "end":      int((target_dt + timedelta(minutes=15)).timestamp() * 1000),
                "limit":    4,
            },
            timeout=8,
        )
        bars = resp.json()["result"]["list"]
        return float(bars[0][4]) if bars else None
    except Exception:
        return None


def resolve_rejects(silent: bool = False) -> int:
    """
    Fetch and store 1h / 4h / 12h future prices for pending rejects.
    Returns count of newly resolved price points.
    """
    entries = _load_rejects()
    if not entries:
        return 0

    now     = datetime.now(timezone.utc)
    updated = 0
    changed = False

    for e in entries:
        price_at = e.get("price_at_reject") or 0
        if not price_at:
            continue

        ts        = _parse_ts(e["ts"])
        elapsed_h = (now - ts).total_seconds() / 3600

        for horizon_h, f_price, f_move in [
            (1,  "future_1h",  "move_1h_pct"),
            (4,  "future_4h",  "move_4h_pct"),
            (12, "future_12h", "move_12h_pct"),
        ]:
            if elapsed_h >= horizon_h and e.get(f_price) is None:
                price = _fetch_price_at_horizon(e["symbol"], ts + timedelta(hours=horizon_h))
                if price:
                    move       = (price - price_at) / price_at * 100
                    e[f_price] = price
                    e[f_move]  = round(move, 3)
                    changed    = True
                    updated   += 1
                    if not silent:
                        direct    = e.get("direction", "ЛОНГ")
                        favorable = (move > 0 if direct == "ЛОНГ" else move < 0)
                        verdict   = "signal✓" if favorable else "gate✓ "
                        print(f"  [{horizon_h}h] {e['symbol']:<12} "
                              f"{e['reject_gate']:<16} {move:+.2f}%  [{verdict}]")
                time.sleep(0.04)

    if changed:
        _save_rejects(entries)

    # A2 (2026-06-08): дозапись в персистентный сток — upsert ВСЕХ текущих записей
    # (новых + дозревших контрфактов) в rejected_history.csv, чтобы история не терялась
    # при rolling-срезе rejected.json. resolve_rejects идёт в конце КАЖДОГО прогона
    # скринера (~4ч) → запись попадает в сток задолго до выпадения из 5.6-дн окна.
    try:
        n_new, n_upd = persist_history(entries)
        if not silent and (n_new or n_upd):
            print(f"  [history] +{n_new} новых / ~{n_upd} обновлено → rejected_history.csv")
    except Exception as _hx:
        print(f"  [history] persist skipped (rejected.json не затронут): {_hx}")

    return updated


# ─────────────────────────────────────────────────────────────
# Analytics
# ─────────────────────────────────────────────────────────────

def gate_analytics() -> dict:
    """
    Per-gate statistics over resolved 4h moves.

    pct_signal_correct: % of blocked signals where price moved in the
                        blocked setup's intended direction.
      Low  → gate correctly blocked losers (excellent)
      High → gate blocked winners too (too_strict)
    """
    entries = _load_rejects()
    if not entries:
        return {}

    from collections import defaultdict
    by_gate: dict = defaultdict(lambda: {"all": [], "resolved": []})

    for e in entries:
        gate = e.get("reject_gate", "?")
        by_gate[gate]["all"].append(e)
        if e.get("move_4h_pct") is not None:
            by_gate[gate]["resolved"].append(e)

    result = {}
    for gate, data in sorted(by_gate.items()):
        resolved_entries = data["resolved"]
        blocked          = len(data["all"])
        n_res            = len(resolved_entries)

        if n_res == 0:
            result[gate] = {
                "blocked": blocked, "resolved": 0,
                "pct_signal_correct": None,
                "future_avg_pct":     None,
                "verdict":            "pending",
            }
            continue

        favorable = []
        for e in resolved_entries:
            m = e["move_4h_pct"]
            d = e.get("direction", "ЛОНГ")
            favorable.append(m > 0 if d == "ЛОНГ" else m < 0)

        pct_signal_correct = sum(favorable) / n_res * 100
        future_avg         = sum(e["move_4h_pct"] for e in resolved_entries) / n_res

        if pct_signal_correct <= 35:
            verdict = "excellent"
        elif pct_signal_correct <= 50:
            verdict = "ok"
        elif pct_signal_correct <= 60:
            verdict = "watch"
        else:
            verdict = "too_strict"

        result[gate] = {
            "blocked":            blocked,
            "resolved":           n_res,
            "pct_signal_correct": round(pct_signal_correct, 1),
            "future_avg_pct":     round(future_avg, 2),
            "verdict":            verdict,
        }

    return result


# ─────────────────────────────────────────────────────────────
# Display
# ─────────────────────────────────────────────────────────────

def print_stats():
    analytics = gate_analytics()
    entries   = _load_rejects()

    if not analytics:
        print("\nReject tracker: нет данных. Запусти screener — отклонения накопятся автоматически.")
        return

    total    = len(entries)
    resolved = sum(1 for e in entries if e.get("move_4h_pct") is not None)

    print(f"\n{'='*76}")
    print(f"  REJECT TRACKER — АНАЛИТИКА ГЕЙТОВ")
    print(f"{'='*76}")
    print(f"  Всего отклонений: {total}  |  Разрешено 4h: {resolved}\n")

    VERDICTS = {
        "excellent":  "✅ excellent  — блокирует лузеров",
        "ok":         "☑️  ok         — нейтрально",
        "watch":      "⚠️  watch      — часть виннеров блокирует",
        "too_strict": "❌ too_strict  — БЛОКИРУЕТ ВИННЕРОВ",
        "pending":    "⏳ pending    — данных ещё нет",
    }

    rows = sorted(analytics.items(), key=lambda x: -x[1]["blocked"])
    for gate, s in rows:
        pct = f"{s['pct_signal_correct']:.1f}%" if s["pct_signal_correct"] is not None else "  —  "
        avg = f"{s['future_avg_pct']:+.2f}%"    if s["future_avg_pct"]     is not None else "  —  "
        v   = VERDICTS.get(s["verdict"], s["verdict"])
        print(f"  {gate:<20}  blocked={s['blocked']:>4}  "
              f"res={s['resolved']:>4}  "
              f"sig_correct={pct:>6}  avg4h={avg:>8}  {v}")
    print()


def print_recent(gate_filter: str | None = None, limit: int = 25):
    entries = sorted(_load_rejects(), key=lambda e: e["ts"], reverse=True)
    if gate_filter:
        entries = [e for e in entries if e["reject_gate"] == gate_filter]

    if not entries:
        print(f"Нет отклонений{f' для гейта {gate_filter}' if gate_filter else ''}.")
        return

    print(f"\n  ОТКЛОНЕНИЯ: {len(entries[:limit])} из {len(entries)}"
          f"{f'  (gate={gate_filter})' if gate_filter else ''}\n")
    print(f"  {'Время':<19} {'Символ':<12} {'Гейт':<18} {'Сетап':<10}"
          f" {'Score':>5} {'1h%':>6} {'4h%':>6} {'12h%':>7}")
    print("  " + "─" * 88)
    for e in entries[:limit]:
        m1  = f"{e['move_1h_pct']:+.1f}"  if e.get("move_1h_pct")  is not None else "—"
        m4  = f"{e['move_4h_pct']:+.1f}"  if e.get("move_4h_pct")  is not None else "—"
        m12 = f"{e['move_12h_pct']:+.1f}" if e.get("move_12h_pct") is not None else "—"
        print(f"  {e['ts']:<19} {e['symbol']:<12} {e['reject_gate']:<18}"
              f" {e['setup']:<10} {e['score_pre_gate']:>5}"
              f" {m1:>6} {m4:>6} {m12:>7}")
    print()


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    cmd      = sys.argv[1] if len(sys.argv) > 1 else "stats"
    gate_arg = sys.argv[2] if len(sys.argv) > 2 else None

    if cmd == "stats":
        print_stats()

    elif cmd == "resolve":
        print("Разрешаю будущие цены для отклонённых сигналов...")
        n = resolve_rejects(silent=False)
        print(f"\nОбновлено: {n}")
        if n == 0:
            print("(все отклонения уже разрешены или ещё слишком свежие)")

    elif cmd == "list":
        print_recent(gate_filter=gate_arg)

    elif cmd == "seed":
        # Одноразовый (идемпотентный) залив текущего rejected.json в персистентный сток,
        # чтобы не потерять нынешнее окно ~3000. Повторный seed дубли не плодит (upsert).
        ents = _load_rejects()
        n_new, n_upd = persist_history(ents)
        total = len(_read_history_rows())
        print(f"Seed: из rejected.json {len(ents)} записей → "
              f"+{n_new} новых / ~{n_upd} обновлено. Всего в rejected_history.csv: {total}")

    elif cmd == "clear":
        confirm = input("Удалить все данные reject tracker? [yes/NO]: ").strip()
        if confirm.lower() == "yes":
            REJECT_FILE.unlink(missing_ok=True)
            print("Данные удалены.")
        else:
            print("Отменено.")

    else:
        print(__doc__)


if __name__ == "__main__":
    main()

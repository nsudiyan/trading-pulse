#!/usr/bin/env python3
"""
funnel_heartbeat.py — тихий сторож воронки (P0-4, 2026-06-07).

Шумит ТОЛЬКО когда система молча умерла (майский кейс «3 дня passed=0
обнаружили случайно»). Всё ок → полное молчание.

Три проверки:
  1. ЖИВОСТЬ:  нет новых funnel-строк > 5ч (каденс прогонов 4ч)
  2. ВОРОНКА:  за 48ч все funnel-строки passed=0 при candidates>0
               (именно 48ч: при потоке ~1.8/день сутки нулей — норма, P≈16%)
  3. ДОСТАВКА: passed>0 был, но в alerts_index.json нет записей за 48ч

Приоритет: если скринер молчит (№1), проверки №2/№3 не шлются — на мёртвых
данных они дублируют шум. №2 и №3 взаимоисключающие по построению.

Машинерия времени: в funnel-строках нет таймстемпов, поэтому сторож ведёт
state (outcomes/funnel_heartbeat_state.json): на каждом запуске дочитывает
лог с сохранённого offset и помечает новые строки временем обнаружения.
Bootstrap первого запуска: последние строки из хвоста лога раскладываются
назад от mtime лога с шагом каденса 4ч (аппроксимация, помечено в state).

Запуск:  python3 funnel_heartbeat.py [--dry-run] [--log P] [--index P] [--state P]
Cron:    каждые 6ч (см. crontab).
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).parent
DEFAULT_LOG   = BASE / "screener_auto.log"
DEFAULT_INDEX = BASE / "outcomes" / "alerts_index.json"
DEFAULT_STATE = BASE / "outcomes" / "funnel_heartbeat_state.json"

FUNNEL_RE = re.compile(
    r"funnel: candidates=(\d+) passed=(\d+) rejected=(\d+) top_reason=(\S+)")

ALIVE_MAX_SILENCE_H = 5     # каденс 4ч + люфт
WINDOW_H            = 48    # окно для проверок 2 и 3
MIN_WINDOW_ENTRIES  = 6     # минимум строк в окне, чтобы судить о воронке
BOOTSTRAP_TAIL      = 262_144   # 256 КБ хвоста при первом запуске
CADENCE_H           = 4     # шаг аппроксимации времени в bootstrap


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_iso(s):
    dt = datetime.fromisoformat(str(s))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def _parse_funnel_lines(text: str) -> list[dict]:
    out = []
    for m in FUNNEL_RE.finditer(text):
        out.append({"candidates": int(m.group(1)), "passed": int(m.group(2)),
                    "rejected": int(m.group(3)), "top_reason": m.group(4)})
    return out


def collect(log_path: Path, state_path: Path) -> dict:
    """Дочитывает лог с offset, обновляет state, возвращает его."""
    state = _load_state(state_path)
    now = _now()

    try:
        size = log_path.stat().st_size
        mtime = datetime.fromtimestamp(log_path.stat().st_mtime, tz=timezone.utc)
    except FileNotFoundError:
        # лога нет вообще — для проверки №1 это «молчит с эпохи»
        state.setdefault("entries", [])
        state.setdefault("last_seen", None)
        return state

    offset = state.get("offset")
    entries = state.get("entries", [])

    if offset is None:
        # ── bootstrap: хвост лога, времена назад от mtime с шагом каденса ──
        with open(log_path, "rb") as f:
            f.seek(max(0, size - BOOTSTRAP_TAIL))
            tail = f.read().decode("utf-8", errors="replace")
        found = _parse_funnel_lines(tail)
        for k, e in enumerate(found):
            approx = mtime - timedelta(hours=CADENCE_H * (len(found) - 1 - k))
            e["seen"] = _iso(approx)
            e["approx"] = True
        entries = found
        if found:
            state["last_seen"] = found[-1]["seen"]
    else:
        if size < offset:
            offset = 0      # лог ротирован/обрезан — читаем заново
        with open(log_path, "rb") as f:
            f.seek(offset)
            new_text = f.read().decode("utf-8", errors="replace")
        new = _parse_funnel_lines(new_text)
        for e in new:
            e["seen"] = _iso(now)
        if new:
            state["last_seen"] = _iso(now)
        entries.extend(new)

    # обрезаем историю до 72ч
    cutoff72 = now - timedelta(hours=72)
    entries = [e for e in entries
               if _parse_iso(e.get("seen", "1970-01-01")) >= cutoff72]

    state["offset"] = size
    state["entries"] = entries
    return state


def _screener_last_exit() -> str:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=10).stdout
        for line in out.splitlines():
            if "com.trading.screener" in line:
                return line.split()[1]
    except Exception:
        pass
    return "?"


def decide(state: dict, index_path: Path) -> list[str]:
    """Возвращает список сообщений-сирен (пустой = всё ок, молчим)."""
    now = _now()
    entries = state.get("entries", [])
    last_seen = state.get("last_seen")

    # ── 1. ЖИВОСТЬ ──
    silent = (last_seen is None or
              (now - _parse_iso(last_seen)) > timedelta(hours=ALIVE_MAX_SILENCE_H))
    if silent:
        return [f"⚠ Скринер молчит >{ALIVE_MAX_SILENCE_H}ч: "
                f"последний прогон {last_seen or 'не найден'}, "
                f"last exit {_screener_last_exit()}"]

    window = [e for e in entries
              if _parse_iso(e["seen"]) >= now - timedelta(hours=WINDOW_H)]
    if len(window) < MIN_WINDOW_ENTRIES:
        return []   # истории мало — не судим (свежая система / свежий state)

    # ── 2. ВОРОНКА ──
    total_candidates = sum(e["candidates"] for e in window)
    if all(e["passed"] == 0 for e in window) and total_candidates > 0:
        reasons = {}
        for e in window:
            reasons[e["top_reason"]] = reasons.get(e["top_reason"], 0) + 1
        top = max(reasons, key=reasons.get)
        return [f"⚠ Воронка: {WINDOW_H}ч passed=0 (candidates={total_candidates}). "
                f"Топ-причина: {top}"]

    # ── 3. ДОСТАВКА ──
    if any(e["passed"] > 0 for e in window):
        fresh_alert = False
        try:
            idx = json.loads(index_path.read_text(encoding="utf-8"))
            cutoff = now - timedelta(hours=WINDOW_H)
            for v in (idx or {}).values():
                try:
                    if _parse_iso((v or {}).get("ts", "")) >= cutoff:
                        fresh_alert = True
                        break
                except Exception:
                    continue
        except Exception:
            pass    # индекса нет/нечитаем = доставки не видно
        if not fresh_alert:
            return ["⚠ Сигналы проходят гейты, но алерты не отправляются — "
                    "проверь фильтр/телеграм-путь"]

    return []


def send_alarm(messages: list[str]) -> bool:
    """Сирена — владельцу в личку (owner_chat_id), fallback на основной chat."""
    sys.path.insert(0, str(BASE))
    from telegram_alerts import _send, load_config
    cfg = load_config()
    token = cfg.get("bot_token")
    target = str(cfg.get("owner_chat_id") or cfg.get("chat_id") or "")
    if not token or not target:
        print("[heartbeat] нет токена/чата — сирена не отправлена", flush=True)
        return False
    text = "🚨 <b>Funnel Heartbeat</b>\n\n" + "\n".join(messages)
    return bool(_send(token, target, text))


def main():
    ap = argparse.ArgumentParser(description="Funnel heartbeat — сирена тишины")
    ap.add_argument("--dry-run", action="store_true", help="решение в stdout, не слать")
    ap.add_argument("--log",   type=Path, default=DEFAULT_LOG)
    ap.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = ap.parse_args()

    state = collect(args.log, args.state)
    messages = decide(state, args.index)
    _save_state(args.state, state)

    if args.dry_run:
        if messages:
            print("DRY-RUN, сирена сработала бы:")
            for m in messages:
                print("  " + m)
        else:
            print(f"DRY-RUN: всё ок, молчу (строк в окне {WINDOW_H}ч: "
                  f"{len([e for e in state.get('entries', []) if _parse_iso(e['seen']) >= _now() - timedelta(hours=WINDOW_H)])}, "
                  f"last_seen={state.get('last_seen')})")
        return

    if messages:
        sent = send_alarm(messages)
        print(f"[heartbeat] {_iso(_now())} сирена: {messages} | sent={sent}", flush=True)
    # всё ок → полное молчание (в т.ч. в логе ни строки — cron-лог не растёт)


if __name__ == "__main__":
    main()

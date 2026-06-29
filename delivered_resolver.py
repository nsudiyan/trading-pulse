"""
Шаг 2: PnL-резолв поверх immutable trigger-ledger (delivered_alerts.csv).

Резолвит ТОЛЬКО реально доставленные алерты по их ФАКТИЧЕСКИМ уровням
(levels_source atr|claude), а не по ATR-формуле фантомной популяции resolved.csv.
Эдж считается на join(delivered_alerts, delivered_resolved) по alert_id.

Честность (уроки прошлых миражей):
  - both-hit разрешается через outcome_tracker._first_touch_order (5m бар-за-баром,
    FIX 2026-06-02) — НЕ пик-эвристика. Нет 5m → пессимистично STOP.
  - net_r после костов (комиссии taker round-trip + слиппедж), выраженных в R.
  - append-only: один alert_id резолвится один раз.
"""
from __future__ import annotations
import csv
from datetime import datetime, timezone, timedelta
from pathlib import Path

import outcome_tracker as ot                 # переиспуем честные _fetch_klines_extremes/_first_touch_order
from delivered_ledger import LEDGER_PATH

RESOLVED_PATH = LEDGER_PATH.parent / "delivered_resolved.csv"
WINDOW_H = 4.0
# Косты — КОНСЕРВАТИВНО (измеритель не должен быть оптимистичным; разница 0.1-0.25R/сделку
# способна перевернуть «эдж есть»→«эджа нет»). Не импортируем оптимистичный 0.0005 из virtual_account.
TAKER_FEE  = 0.00055   # Bybit non-VIP taker 0.055%/сторона
SLIP_ENTRY = 0.0007    # market-вход на альте ~0.07%
SLIP_STOP  = 0.0015    # stop-market выход в каскаде исполняется хуже ~0.15% (асимметрия)
SLIP_LIMIT = 0.0       # TP-выход limit-ордером ~0
# ponytail: косты-константы — ретейл-оценка; калибровать по реальным филлам, если важно.
# ponytail: 4h окно → фандинг ≈0 (интервал 8h, <1 начисления); добавить funding при расширении до 24h.
# Соглашение окна: бар входит по времени ОТКРЫТИЯ — Bybit open>=start исключает бар-страддл входа
# (консервативно), open<=end включает бар, перетягивающий до ~1 интервала за win_end. Эффект мал/симметричен.

FIELDS = ["alert_id", "resolve_ts", "window_h", "exit_reason", "exit_price",
          "gross_r", "net_r", "resolve_method", "levels_source"]


def _parse_delivered_ts(s: str) -> datetime:
    """delivered_ts формат '%Y-%m-%dT%H:%M:%S%z' (aware) — НЕ ot._parse_ts (тот без %z)."""
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")


def _compute_resolution(side, entry, stop, tp1, max_high, min_low, close_end,
                        both_hit_order=None) -> dict:
    """Чистое резолв-ядро (без сети) — тестируемо. both_hit_order: результат
    first_touch ('TP1'|'STOP'|None), используется ТОЛЬКО при both-hit."""
    is_long = side == "long"
    if is_long:
        hit_tp   = bool(tp1) and max_high >= tp1
        hit_stop = bool(stop) and min_low <= stop
    else:
        hit_tp   = bool(tp1) and min_low <= tp1
        hit_stop = bool(stop) and max_high >= stop

    method = "single"
    if hit_tp and hit_stop:                       # both-hit → честный порядок
        if both_hit_order == "TP1":
            reason, price, method = "tp1", tp1, "5m_first_touch"
        elif both_hit_order == "STOP":
            reason, price, method = "stop", stop, "5m_first_touch"
        else:                                     # нет 5m → пессимистично STOP
            reason, price, method = "stop", stop, "pessimistic_no_data"
    elif hit_tp:
        reason, price = "tp1", tp1
    elif hit_stop:
        reason, price = "stop", stop
    else:
        reason, price, method = "timeout", close_end, "timeout"

    risk = abs(entry - stop) / entry if (entry and stop) else 0.0   # R=1 в долях цены
    move = (price - entry) / entry if is_long else (entry - price) / entry
    gross_r = move / risk if risk else 0.0
    # косты в долях цены: round-trip taker + вход market + выход по типу (асимметрия)
    exit_slip = SLIP_LIMIT if reason == "tp1" else (SLIP_STOP if reason == "stop" else SLIP_ENTRY)
    costs_price = TAKER_FEE * 2 + SLIP_ENTRY + exit_slip
    net_r = gross_r - (costs_price / risk if risk else 0.0)
    return {"exit_reason": reason, "exit_price": round(price, 8),
            "gross_r": round(gross_r, 4), "net_r": round(net_r, 4),
            "resolve_method": method}


def _f(v):
    try: return float(v)
    except (TypeError, ValueError): return None


def resolve_pending(silent: bool = False) -> int:
    """Резолвит доставленные алерты, чьё окно WINDOW_H уже закрылось. Возвращает число новых."""
    if not LEDGER_PATH.exists():
        return 0
    done = set()
    if RESOLVED_PATH.exists():
        with RESOLVED_PATH.open(newline="", encoding="utf-8") as f:
            done = {row["alert_id"] for row in csv.DictReader(f)}

    now = datetime.now(timezone.utc)
    new_rows = []
    with LEDGER_PATH.open(newline="", encoding="utf-8") as f:
        snaps = list(csv.DictReader(f))

    skipped = 0
    seen = set(done)                              # идемпотентность в рамках прогона (дубль alert_id в леджере)
    for s in snaps:
        aid = s.get("alert_id")
        if not aid or aid in seen:
            continue
        # Per-row защита: одна битая строка НЕ должна тихо застопить весь резолв (иначе
        # измеряемая популяция молча застывает и эдж считается на устаревшем срезе).
        try:
            dts = _parse_delivered_ts(s["delivered_ts"])
            win_end = dts + timedelta(hours=WINDOW_H)
            if now < win_end:
                continue                          # окно ещё не закрылось
            entry, stop, tp1 = _f(s["entry"]), _f(s["stop"]), _f(s["tp1"])
            side = s["side"]
            # неполный/вырожденный снимок — не резолвим (не выдумываем исход): нужен tp1 и entry≠stop
            if not (entry and stop and tp1) or entry == stop:
                continue

            mh, ml, ce, _, _ = ot._fetch_klines_extremes(s["symbol"], dts, win_end)
            if mh is None:
                continue                          # нет данных → НЕ резолвим (повторим позже)

            # both-hit только если оба уровня задеты в окне (15m экстремумы); 5m решает ПОРЯДОК
            is_long = side == "long"
            both = ((is_long and mh >= tp1 and ml <= stop) or
                    (not is_long and ml <= tp1 and mh >= stop))
            order = None
            if both:
                dir_cyr = "ЛОНГ" if is_long else "ШОРТ"
                order = ot._first_touch_order(s["symbol"], dts, win_end, dir_cyr, stop, tp1)

            res = _compute_resolution(side, entry, stop, tp1, mh, ml, ce, both_hit_order=order)
            res.update({"alert_id": aid, "resolve_ts": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "window_h": WINDOW_H, "levels_source": s.get("levels_source", "")})
            new_rows.append(res)
            seen.add(aid)
        except Exception as e:
            skipped += 1
            print(f"[resolver] СКИП битого снимка alert_id={aid}: {e}", flush=True)
            continue

    if new_rows:
        write_header = not RESOLVED_PATH.exists() or RESOLVED_PATH.stat().st_size == 0
        with RESOLVED_PATH.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if write_header:
                w.writeheader()
            for r in new_rows:
                w.writerow({k: r[k] for k in FIELDS})
    if not silent:
        msg = (f"[resolver] резолвнуто {len(new_rows)} (окно {WINDOW_H}ч); "
               f"всего в delivered_resolved: {len(done) + len(new_rows)}")
        if skipped:
            msg += f"; СКИПНУТО битых снимков: {skipped} (резолв продолжен, не застопорен)"
        print(msg)
    return len(new_rows)


if __name__ == "__main__":
    import sys
    if "--selfcheck" in sys.argv or len(sys.argv) == 1:
        def _exp_net(gross, risk, reason):
            es = SLIP_LIMIT if reason == "tp1" else (SLIP_STOP if reason == "stop" else SLIP_ENTRY)
            return gross - (TAKER_FEE * 2 + SLIP_ENTRY + es) / risk
        # 1) long clean TP: entry100 stop98 tp104; max105 min99 → tp1, gross=2.0, risk=0.02
        r = _compute_resolution("long", 100, 98, 104, 105, 99, 101)
        assert r["exit_reason"] == "tp1" and r["gross_r"] == 2.0
        assert abs(r["net_r"] - _exp_net(2.0, 0.02, "tp1")) < 1e-6, r
        # 2) long clean STOP: max103 min97 → stop, gross=-1.0; stop-выход дороже (SLIP_STOP)
        r = _compute_resolution("long", 100, 98, 104, 103, 97, 99)
        assert r["exit_reason"] == "stop" and r["gross_r"] == -1.0 and r["resolve_method"] == "single"
        assert abs(r["net_r"] - _exp_net(-1.0, 0.02, "stop")) < 1e-6, r
        # 3) long both-hit, 5m says TP1 first
        r = _compute_resolution("long", 100, 98, 104, 105, 97, 100, both_hit_order="TP1")
        assert r["exit_reason"] == "tp1" and r["resolve_method"] == "5m_first_touch" and r["gross_r"] == 2.0
        # 4) long both-hit, NO 5m → pessimistic STOP
        r = _compute_resolution("long", 100, 98, 104, 105, 97, 100, both_hit_order=None)
        assert r["exit_reason"] == "stop" and r["resolve_method"] == "pessimistic_no_data" and r["gross_r"] == -1.0
        # 5) long timeout: neither hit → close_end, gross=0.5; timeout-выход market (SLIP_ENTRY)
        r = _compute_resolution("long", 100, 98, 104, 103, 99, 101)
        assert r["exit_reason"] == "timeout" and r["resolve_method"] == "timeout" and r["gross_r"] == 0.5
        assert abs(r["net_r"] - _exp_net(0.5, 0.02, "timeout")) < 1e-6, r
        # 6) SHORT clean TP: entry100 stop102 tp96; min95 max101 → tp1, gross=2.0
        r = _compute_resolution("short", 100, 102, 96, 101, 95, 99)
        assert r["exit_reason"] == "tp1" and r["gross_r"] == 2.0, r
        # 7) SHORT stop: max103 min97 (tp96 not hit, stop102 hit) → stop, gross=-1.0
        r = _compute_resolution("short", 100, 102, 96, 103, 97, 101)
        assert r["exit_reason"] == "stop" and r["gross_r"] == -1.0, r
        # косты асимметричны: stop-выход дороже tp-выхода (SLIP_STOP > SLIP_LIMIT)
        rt = _compute_resolution("long", 100, 98, 102, 102, 99, 101)   # tp (gross=1.0)
        rs = _compute_resolution("long", 100, 98, 110, 103, 97, 99)    # stop (gross=-1.0)
        assert (1.0 - rt["net_r"]) < (abs(rs["net_r"]) - 1.0), "stop-выход должен стоить дороже tp"
        # net_r всегда < gross_r (косты режут)
        assert all(_compute_resolution(*a)["net_r"] < _compute_resolution(*a)["gross_r"]
                   for a in [("long",100,98,104,105,99,101), ("short",100,102,96,101,95,99)])
        print("✓ self-check passed: long/short × clean/both-hit/timeout, honest order, асимметричные косты в R OK")
    else:
        resolve_pending()

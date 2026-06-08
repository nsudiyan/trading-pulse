#!/usr/bin/env python3
"""
P0-1 gate forensics: анализ outcomes/rejected.json — кто душит воронку.

Выводит:
  a) распределение отказов по гейтам (скринер отдельно от pump_detector);
  b) score-бакеты;
  c) симуляция «кандидатов/день» при вариантах открытия гейтов:
     (1) MAX_SCORE_GLOBAL cap -> флаг вместо reject;
     (2) вариант (1) + единый min score из {80, 90, 100, 110, 120}.

ЧЕСТНЫЕ ОГОВОРКИ СИМУЛЯЦИИ (упрощения => оценка СВЕРХУ):
  - запись в rejected.json = ПЕРВЫЙ сработавший гейт; что сделали бы
    последующие гейты (TimeGate-tuesday, GradeGate, FallingKnife, HardGate,
    SqueezeZone, BQ-V2, RSI_Gate, OI_Exhaustion) по этому сигналу — неизвестно.
    Учтены только: HARD_BLOCK_HOURS {18,19} (детерминированный) и
    SETUP_TG_MAX_SCORE (short_dist>=150).
  - карантинные сетапы (breakout, range_sweep, min=9999) НЕ воскрешаем.
  - дедуп log_reject (symbol+gate+minute) уже в данных.
Запуск: python3 tools/gate_forensics.py
"""
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
REJ_PATH = BASE / "outcomes" / "rejected.json"

PUMP_GATES = {"FundGate", "PumpGate"}          # pump_detector пишет в тот же файл
QUARANTINED_SETUPS = {"breakout", "range_sweep"}
SETUP_TG_MAX_SCORE = {"short_dist": 150}       # зеркало screener.py:102
HARD_BLOCK_HOURS = {18, 19}                    # зеркало screener.py:81
MIN_VARIANTS = [80, 90, 100, 110, 120]
BUCKETS = [(0, 70), (70, 90), (90, 110), (110, 130), (130, 180), (180, 10**9)]


def bucket_label(lo, hi):
    return f"{lo}-{hi}" if hi < 10**9 else f"{lo}+"


def load():
    """A2 (2026-06-08): читаем ПОЛНОЕ окно из персист-стока rejected_history.csv
    (+ свежий rejected.json), приоритет — история. Fallback на rolling rejected.json,
    если стока ещё нет — чтобы форензика №2 шла на полной выборке, а не на 5.6 днях."""
    try:
        import sys as _sys
        if str(BASE) not in _sys.path:
            _sys.path.insert(0, str(BASE))
        import reject_tracker as _rt
        recs = _rt.load_history(include_live=True)
        if recs:
            return recs
    except Exception as e:
        print(f"[forensics] history load failed ({e}) — fallback rejected.json")
    recs = json.load(open(REJ_PATH))
    assert isinstance(recs, list), "rejected.json: ожидаю list"
    return recs


def day(rec):
    return (rec.get("ts") or "")[:10]


def hour(rec):
    ts = rec.get("ts") or ""
    return int(ts[11:13]) if len(ts) >= 13 else -1


def survives_downstream(rec):
    """Детерминированные пост-гейты, известные по полям записи."""
    if hour(rec) in HARD_BLOCK_HOURS:
        return False
    setup = rec.get("setup", "")
    max_sc = SETUP_TG_MAX_SCORE.get(setup)
    if max_sc is not None and rec.get("score_pre_gate", 0) >= max_sc:
        return False
    return True


def main():
    recs = load()
    scr = [r for r in recs if r.get("reject_gate") not in PUMP_GATES]
    pmp = [r for r in recs if r.get("reject_gate") in PUMP_GATES]

    days = sorted({day(r) for r in recs if day(r)})
    per_day = Counter(day(r) for r in scr)
    print(f"Всего записей: {len(recs)}  (полное окно из rejected_history.csv, если засеян; "
          f"иначе rolling rejected.json — макс 3000)")
    print(f"Окно: {min(days)} … {max(days)}  ({len(days)} календ. дней; первый/последний могут быть неполными)")
    print(f"Скринер-гейты: {len(scr)}  |  pump_detector (FundGate/PumpGate): {len(pmp)}")
    print("\nЗаписей скринера по дням: " + "  ".join(f"{d[5:]}:{per_day.get(d,0)}" for d in days))

    # ── a) по гейтам ──
    print("\n=== a) Отказы по гейтам (скринер) ===")
    for gate, n in Counter(r["reject_gate"] for r in scr).most_common():
        print(f"  {gate:<18} {n:>5}  ({100*n/max(len(scr),1):.1f}%)")
    if pmp:
        print("--- pump_detector (справочно) ---")
        for gate, n in Counter(r["reject_gate"] for r in pmp).most_common():
            print(f"  {gate:<18} {n:>5}")

    # ── b) score-бакеты ──
    print("\n=== b) Score-бакеты (скринер, score_pre_gate) ===")
    for lo, hi in BUCKETS:
        n = sum(1 for r in scr if lo <= (r.get("score_pre_gate") or 0) < hi)
        print(f"  {bucket_label(lo, hi):<9} {n:>5}")

    # ── проверка заявленной статистики 3–5 июня ──
    j35 = [r for r in scr if "2026-06-03" <= day(r) <= "2026-06-05"]
    j35_70 = sum(1 for r in j35 if (r.get("score_pre_gate") or 0) >= 70)
    print(f"\nПроверка «1157 отказов / 1139 score>=70 за 3–5 июня»: "
          f"фактически {len(j35)} / {j35_70} (скринер, после rolling-среза)")

    # ── c) симуляция ──
    sg = [r for r in scr if r.get("reject_gate") == "ScoreGate"]
    cap_rej = [r for r in sg if ">=MAX_SCORE_GLOBAL" in (r.get("reject_reason") or "")]
    min_rej = [r for r in sg if "<min=" in (r.get("reject_reason") or "")]
    eligible_min = [r for r in min_rej if r.get("setup") not in QUARANTINED_SETUPS]

    def daily_unique(passes):
        seen = defaultdict(set)
        for r in passes:
            seen[day(r)].add(r.get("symbol"))
        return seen

    full_days = [d for d in days if d not in (min(days), max(days))]  # края неполные

    def report_variant(name, passes):
        dd = daily_unique(passes)
        per = [len(dd.get(d, ())) for d in full_days]
        avg = sum(per) / max(len(per), 1)
        detail = "  ".join(f"{d[5:]}:{len(dd.get(d, ()))}" for d in days)
        print(f"  {name:<28} avg {avg:4.1f}/день (полные дни)   [{detail}]")
        return avg

    print(f"\n=== c) Симуляция (уникальные symbol+день; ОЦЕНКА СВЕРХУ — см. докстринг) ===")
    print(f"  ScoreGate-cap (>=180): {len(cap_rej)} записей; ScoreGate-min: {len(min_rej)} "
          f"(из них вне карантина: {len(eligible_min)})")

    v1 = [r for r in cap_rej if survives_downstream(r)]
    report_variant("(1) cap->flag", v1)
    for x in MIN_VARIANTS:
        vx = v1 + [r for r in eligible_min
                   if (r.get("score_pre_gate") or 0) >= x and survives_downstream(r)]
        report_variant(f"(2) cap->flag + min={x}", vx)

    # ── примеры для решения, если поток ~0 ──
    print("\n=== Примеры свежих реджектов (для ручного разбора) ===")
    for r in scr[-5:]:
        print(f"  {r.get('ts','')[:16]} {r.get('symbol'):<14} {r.get('setup'):<10} "
              f"score={r.get('score_pre_gate')}  {r.get('reject_gate')}: {r.get('reject_reason')}")


if __name__ == "__main__":
    main()

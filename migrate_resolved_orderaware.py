#!/usr/bin/env python3
"""
migrate_resolved_orderaware.py — переразрешает both-hit строки outcomes/resolved.csv ORDER-AWARE.

Зеркалит фикс outcome_tracker (2026-05-30): если за окно цена коснулась И TP, И стопа, а стоп
наступил НЕ позже тейка (time_to_mae <= time_to_mfe) ИЛИ порядок неизвестен → это STOP, не TP1
(look-ahead, завышавший WR, который видит live Claude-фильтр). Чинит:
  outcome_<h>, hit_tp1_<h>, r_multiple_<h> (= -1.0), exit_price_<h> (= stop), exit_reason_<h> (= sl).
Использует УЖЕ записанные time_to_mfe/mae — клайны НЕ перекачиваются. Идемпотентно.

Запуск:  python3 migrate_resolved_orderaware.py            — DRY-RUN (только показать)
         python3 migrate_resolved_orderaware.py --apply    — применить (с бэкапом)
"""
import csv, sys, os, shutil, time

HERE = os.path.dirname(os.path.abspath(__file__))
F    = os.path.join(HERE, "outcomes", "resolved.csv")
APPLY = "--apply" in sys.argv


def _truthy(x):
    return str(x).strip() in ("1", "1.0", "True", "true")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    if not os.path.exists(F):
        print("нет", F); return
    rows = list(csv.DictReader(open(F, encoding="utf-8")))
    if not rows:
        print("пусто"); return
    fields = list(rows[0].keys())

    both = {"4h": 0, "24h": 0}
    flipped = {"4h": 0, "24h": 0}
    kept = {"4h": 0, "24h": 0}
    samples = []

    for r in rows:
        for h in ("4h", "24h"):
            if not (_truthy(r.get(f"hit_tp1_{h}")) and _truthy(r.get(f"hit_stop_{h}"))):
                continue
            both[h] += 1
            tmfe = _f(r.get(f"time_to_mfe_{h}_h"))   # TP-сторона (направление-зависимая в трекере)
            tmae = _f(r.get(f"time_to_mae_{h}_h"))   # стоп-сторона
            tp_first = (tmfe is not None and tmae is not None and tmfe < tmae)
            if tp_first:
                kept[h] += 1
                continue
            # стоп раньше TP / порядок неизвестен → STOP (только если сейчас записано TP1)
            if (r.get(f"outcome_{h}") or "").strip() == "TP1":
                if len(samples) < 10:
                    samples.append((r.get("symbol"), r.get("setup"), r.get("direction"), h,
                                    r.get(f"r_multiple_{h}"), tmfe, tmae))
                r[f"outcome_{h}"]    = "STOP"
                r[f"hit_tp1_{h}"]    = "0"
                r[f"r_multiple_{h}"] = "-1.0"
                _stop = r.get("stop")
                if _stop not in (None, ""):
                    r[f"exit_price_{h}"] = _stop
                r[f"exit_reason_{h}"] = "sl"
                flipped[h] += 1

    print(f"строк: {len(rows)}")
    for h in ("4h", "24h"):
        print(f"  {h}: both-hit={both[h]}  TP-first(оставлено)={kept[h]}  "
              f"переклассифицировано TP1→STOP={flipped[h]}")
    print("\nпримеры флипов (symbol, setup, dir, гориз, был_R→-1.0, tmfe, tmae):")
    for s in samples:
        print("  ", s)

    if not APPLY:
        print("\n[DRY-RUN] ничего не записано. Применить: python3 migrate_resolved_orderaware.py --apply")
        return

    bak = F + ".backup_" + time.strftime("%Y%m%d_%H%M%S") + "_pre_orderaware_migrate"
    shutil.copy2(F, bak)
    tmp = F + ".tmp_migrate"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, F)
    print(f"\n✅ применено. Бэкап: {os.path.basename(bak)}")


if __name__ == "__main__":
    main()

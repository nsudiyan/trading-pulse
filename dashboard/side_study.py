#!/usr/bin/env python3
"""Ретро-исследование стороны хода (2026-07-05).

Вопрос брата: у монет, которые реально отработали (ход ≥5% за сутки от сигнала),
что НА МОМЕНТ сигнала предсказывало сторону последующего доминантного хода?

Дисциплина:
- состояние рынка берётся СТРОГО ДО anchor_ts (без look-ahead);
- фичи предрегистрированы ДО прогона (этот докстринг): momentum 24ч до сигнала,
  ΔOI/4ч до сигнала, funding на момент, источник, направление сигнала (если есть);
- фактическая сторона = доминанта: peak24 > |dd24| → UP, иначе DOWN
  (peak/dd в треках уже в лонг-семантике для Δ и long; для short-треков
  peak24 = ход ВНИЗ → сторона факта = down при peak24>|dd24|!);
- IN-SAMPLE: найденное = гипотезы для bias v2, не доказательство.
"""
import json
import time
import urllib.request
from datetime import datetime, timezone

B = "https://api.bybit.com/v5/market"
DASH = "/Users/nikitasudian/trading/dashboard"


def get(url, retries=2):
    for i in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                d = json.load(r)
            if d.get("retCode") == 0:
                return d["result"]
        except Exception:
            if i == retries:
                raise
            time.sleep(0.4)
    return None


def collect_tracks():
    """Финальные треки с ходом ≥5%: (symbol, anchor_ms, dir_hint, source, fact_side, ret24)."""
    out = []
    lg = json.load(open(f"{DASH}/signals_ledger.json"))["tracks"]
    rh = json.load(open(f"{DASH}/rose_history.json"))["tracks"]
    for t, src in [(t, t.get("source") or "ledger") for t in lg] + \
                  [(t, "rose") for t in rh]:
        o = t.get("outcome") or {}
        if o.get("status") != "ok" or not o.get("final"):
            continue
        peak, dd = o.get("peak24_pct") or 0, o.get("dd24_pct") or 0
        if max(abs(peak), abs(dd)) < 5.0:
            continue
        d = (t.get("direction") or "").lower() or None
        # факт: доминанта в СТОРОНУ сигнала → мировая сторона (short инвертирует)
        dom_signal_side = abs(peak) > abs(dd)          # ход в сторону сигнала?
        if d == "short":
            fact = "down" if dom_signal_side else "up"
            ret_world = -(o.get("ret24_pct") or 0)
        else:                                           # long и Δ — лонг-семантика
            fact = "up" if dom_signal_side else "down"
            ret_world = o.get("ret24_pct") or 0
        anchor = datetime.fromisoformat(t["anchor_ts"])
        out.append({
            "symbol": t["symbol"], "src": src, "dir": d,
            "anchor_ms": int(anchor.timestamp() * 1000),
            "fact": fact, "ret_world": ret_world,
            "move": max(abs(peak), abs(dd)),
        })
    return out


def features_at(symbol, anchor_ms):
    """Состояние рынка строго ДО anchor: momentum 24ч, ΔOI 4ч, funding."""
    # momentum: close последнего ЗАКРЫТОГО 15м бара до анкора vs 24ч раньше
    kl = get(f"{B}/kline?category=linear&symbol={symbol}&interval=15"
             f"&start={anchor_ms - 26 * 3600_000}&end={anchor_ms}&limit=120")
    bars = kl.get("list") or []
    bars.reverse()
    bars = [b for b in bars if int(b[0]) + 15 * 60_000 <= anchor_ms]
    mom24 = None
    if len(bars) >= 90:
        c_now, c_prev = float(bars[-1][4]), float(bars[-96][4] if len(bars) >= 96 else bars[0][4])
        mom24 = (c_now - c_prev) / c_prev * 100 if c_prev > 0 else None
    # ΔOI 4ч до анкора
    oi = get(f"{B}/open-interest?category=linear&symbol={symbol}"
             f"&intervalTime=30min&startTime={anchor_ms - 5 * 3600_000}"
             f"&endTime={anchor_ms}&limit=12")
    pts = sorted(((int(p["timestamp"]), float(p["openInterest"]))
                  for p in (oi.get("list") or [])), key=lambda x: x[0])
    pts = [p for p in pts if p[0] <= anchor_ms]
    oi4 = None
    if len(pts) >= 8 and pts[0][1] > 0:
        oi4 = (pts[-1][1] - pts[0][1]) / pts[0][1] * 100
    # funding: последняя ставка ≤ anchor
    fr = get(f"{B}/funding/history?category=linear&symbol={symbol}&limit=200")
    fs = sorted(((int(f["fundingRateTimestamp"]), float(f["fundingRate"]))
                 for f in (fr.get("list") or [])), key=lambda x: x[0])
    fund = None
    for t, r in fs:
        if t <= anchor_ms:
            fund = r
        else:
            break
    return mom24, oi4, fund


def main():
    tracks = collect_tracks()
    print(f"треков с ходом ≥5%: {len(tracks)}")
    rows = []
    for i, t in enumerate(tracks):
        try:
            mom, oi4, fund = features_at(t["symbol"], t["anchor_ms"])
        except Exception as e:
            print(f"  skip {t['symbol']}: {e}")
            continue
        rows.append({**t, "mom24": mom, "oi4": oi4, "fund": fund})
        if (i + 1) % 20 == 0:
            print(f"  ...{i + 1}/{len(tracks)}")
        time.sleep(0.1)
    json.dump(rows, open("side_study_rows.json", "w"))
    print(f"собрано с фичами: {len(rows)}")

    ups = sum(1 for r in rows if r["fact"] == "up")
    print(f"\nБАЗА: up {ups}/{len(rows)} = {ups/len(rows)*100:.0f}% "
          f"(рынок этих дней рос — держать в голове)")

    def table(name, rows_, key):
        buckets = {}
        for r in rows_:
            v = key(r)
            if v is None:
                continue
            buckets.setdefault(v, []).append(r)
        print(f"\n── {name} ──")
        for b in sorted(buckets):
            rs = buckets[b]
            up = sum(1 for r in rs if r["fact"] == "up")
            print(f"  {b:22} n={len(rs):3}  up {up/len(rs)*100:3.0f}%  "
                  f"avg ret(мир) {sum(r['ret_world'] for r in rs)/len(rs):+5.1f}%")

    table("momentum 24ч ДО сигнала", rows, lambda r:
          None if r["mom24"] is None else
          "рос ≥+10%" if r["mom24"] >= 10 else "рос +3..10%" if r["mom24"] >= 3
          else "флэт ±3%" if r["mom24"] > -3 else "падал")
    table("ΔOI/4ч ДО сигнала", rows, lambda r:
          None if r["oi4"] is None else
          "OI ≥ +10%" if r["oi4"] >= 10 else "OI +3..10%" if r["oi4"] >= 3
          else "OI ±3%" if r["oi4"] > -3 else "OI падал ≤−3%")
    table("funding на момент", rows, lambda r:
          None if r["fund"] is None else
          "минус-экстрим ≤−0.5%" if r["fund"] <= -0.005 else
          "минус" if r["fund"] < -0.0001 else
          "плюс-экстрим ≥+0.5%" if r["fund"] >= 0.005 else
          "около нуля/плюс")
    table("источник", rows, lambda r: r["src"])
    table("направление сигнала (rose/storm)",
          [r for r in rows if r["dir"] in ("long", "short")], lambda r: r["dir"])

    # связки momentum × OI (главная гипотеза VANRY/TAIKO)
    def combo(r):
        if r["mom24"] is None or r["oi4"] is None:
            return None
        m = "рос" if r["mom24"] >= 3 else "падал" if r["mom24"] <= -3 else "флэт"
        o = "OI↑" if r["oi4"] >= 3 else "OI↓" if r["oi4"] <= -3 else "OI="
        return f"{m} + {o}"
    table("КОМБО: движение × OI (гипотеза VANRY/TAIKO)", rows, combo)

    # ── ретро-точность bias v1 (те же правила, что в бою) ──
    import sys
    sys.path.insert(0, DASH)
    from bias import compute_bias
    hit = miss = 0
    for r in rows:
        b = compute_bias(r["mom24"], r["oi4"], r["fund"], pump=None)
        if not b or b["side"] == "flat":
            continue
        if abs(r["ret_world"]) <= 2.0:
            continue
        ok = (b["side"] == "up") == (r["ret_world"] > 0)
        hit += ok
        miss += not ok
    n = hit + miss
    print(f"\nБЭКТЕСТ bias v1 на этих треках (ретро, in-sample): "
          f"{hit}/{n} = {hit/n*100:.0f}%  (покрытие {n}/{len(rows)})" if n else "\nbias v1: покрытия нет")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
carry/monitor.py — feasibility-пул + paper-монитор funding-carry. READ-ONLY, без ключей.
Источник: Bybit V5 public (linear+spot tickers). Не зависит от старого бота.

ИДЕЯ (исполнимая сторона): положительный funding ⇒ SHORT perp (собираешь funding) + LONG spot
(просто держишь, borrow≈0). Delta-neutral: цена сокращается, доход = funding − косты − basis-сдвиг.

ЧЕСТНО: funding mean-reverts быстро. ann_carry ниже = ВЕРХНЯЯ оценка (если ставка удержится).
Реальный вопрос — удержится ли funding дольше breakeven_intervals. На это отвечает paper-лог.

=== KILL-МЕТРИКА paper-трека (ПРЕДЗАПИСАНА) ===
Перед заводом капитала: накопить >=4 недели снапшотов. Carry мёртв если медианный
реализованный net-carry на сделку (вход при funding>=порога, выход при возврате <порог/2,
минус 0.16% round-trip fee − basis-сдвиг) <= 0, ИЛИ годовой реализованный yield <= 10%.
Капитал — только после прохождения, и с дневным кластер-CI не через ноль.
"""
import urllib.request, json, csv, os, sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PAPER_CSV = os.path.join(HERE, "paper_carry.csv")

# косты (оценки, правь под свою биржу/тиры)
ROUND_TRIP_FEE = 0.0016   # 4 филла × ~0.04% (perp+spot open/close)
MIN_PERP_TURN  = 5_000_000   # $5M/24ч ликвидность перпа
MIN_SPOT_TURN  = 2_000_000   # $2M/24ч ликвидность спота
MIN_FUNDING    = 0.0002      # 0.02%/8ч — ниже не стоит возни

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "carry/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

def fetch_tickers(category):
    d = _get(f"https://api.bybit.com/v5/market/tickers?category={category}")
    if d.get("retCode") != 0:
        raise RuntimeError(f"bybit {category}: {d.get('retMsg')}")
    return d["result"]["list"]

def build_pool():
    linear = fetch_tickers("linear")
    spot   = fetch_tickers("spot")
    spot_turn = {s["symbol"]: float(s.get("turnover24h") or 0) for s in spot}  # есть спот-нога?
    cands = []
    for p in linear:
        sym = p["symbol"]
        fr  = p.get("fundingRate")
        if fr in (None, ""):
            continue
        fr = float(fr)
        if fr < MIN_FUNDING:          # исполнимая сторона = положительный funding
            continue
        if sym not in spot_turn:      # delta-neutral требует спот-ногу на той же бирже
            continue
        perp_turn = float(p.get("turnover24h") or 0)
        sturn = spot_turn[sym]
        if perp_turn < MIN_PERP_TURN or sturn < MIN_SPOT_TURN:
            continue
        ivl = float(p.get("fundingIntervalHour") or 8)
        per_day = 24.0 / ivl
        ann = fr * per_day * 365 * 100                      # верхняя оценка %/год
        breakeven = ROUND_TRIP_FEE / fr if fr > 0 else 999  # сколько интервалов держать чтобы отбить fee
        cands.append({
            "symbol": sym, "funding_pct": fr * 100, "interval_h": ivl,
            "ann_carry_pct_upper": round(ann, 1), "breakeven_intervals": round(breakeven, 1),
            "breakeven_hours": round(breakeven * ivl, 1),
            "basis_rate_pct": round(float(p.get("basisRate") or 0) * 100, 4),
            "perp_turn_m": round(perp_turn / 1e6, 1), "spot_turn_m": round(sturn / 1e6, 1),
        })
    cands.sort(key=lambda c: c["ann_carry_pct_upper"], reverse=True)
    return cands

def log_snapshot(cands):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    new = not os.path.exists(PAPER_CSV)
    with open(PAPER_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts","symbol","funding_pct","interval_h","ann_carry_pct_upper",
                        "breakeven_hours","basis_rate_pct","perp_turn_m","spot_turn_m"])
        for c in cands:
            w.writerow([ts, c["symbol"], f'{c["funding_pct"]:.4f}', c["interval_h"],
                        c["ann_carry_pct_upper"], c["breakeven_hours"], c["basis_rate_pct"],
                        c["perp_turn_m"], c["spot_turn_m"]])
    return ts

if __name__ == "__main__":
    cands = build_pool()
    ts = log_snapshot(cands)
    print(f"[{ts}] Feasibility-пул (pos funding ≥{MIN_FUNDING*100:.2f}%/8ч, perp≥${MIN_PERP_TURN/1e6:.0f}M, spot≥${MIN_SPOT_TURN/1e6:.0f}M, есть спот-нога):")
    print(f"{'symbol':16s} {'fund%/ivl':>10s} {'ivlh':>5s} {'annUP%':>8s} {'breakeven':>10s} {'basis%':>8s} {'perp$M':>8s} {'spot$M':>8s}")
    for c in cands:
        print(f"{c['symbol']:16s} {c['funding_pct']:>+9.4f} {c['interval_h']:>5.0f} {c['ann_carry_pct_upper']:>8.1f} "
              f"{c['breakeven_hours']:>8.1f}ч {c['basis_rate_pct']:>+8.4f} {c['perp_turn_m']:>8.1f} {c['spot_turn_m']:>8.1f}")
    print(f"\nИтого кандидатов: {len(cands)}  | снапшот записан в {os.path.relpath(PAPER_CSV, HERE)}")
    if cands:
        worth = [c for c in cands if c["breakeven_hours"] <= 48]
        print(f"Из них с breakeven ≤48ч (funding отобьёт fee за ≤2 дня): {len(worth)}")

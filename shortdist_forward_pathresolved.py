#!/usr/bin/env python3
"""
shortdist_forward_pathresolved.py — бумажный ФОРВАРД-харнесс (out-of-sample) гипотезы:
  short_dist/SHORT на ликвиде (≥ VOL_MIN), вход MAKER → положительный net-R.
  + тег BTC-режима (btc_trend_4h) — проверяем регламент-гипотезу «шортить когда BTC падает» ЧЕСТНО, вперёд.

ПОЧЕМУ path-resolved (критично): outcome_tracker._resolve_exit при both-hit (за окно цена
коснулась И TP, И стопа) ВСЕГДА засчитывает TP (hit_tp1 проверяется раньше hit_stop; оба флага
по экстремумам всего окна) → look-ahead, завышает r_multiple_24h. На in-sample это раздувало
short_dist/SHORT-ликвид с реального ≈0 до мнимого +0.215R. Здесь спорные (both-hit) сделки
разрешаются по фактическому таймингу: time_to_mae (стоп-сторона) vs time_to_mfe (TP-сторона);
если адверс-экстремум НЕ позже фавор-экстремума → стоп сработал первым → R=-1.

READ-ONLY по outcomes/resolved.csv. Форвард-сигналы копятся туда САМИ (screener → pending.json →
outcome_tracker) — НОВЫЙ ДЕМОН НЕ НУЖЕН. Это НЕ торговля и НЕ live-изменение.

Запуск:  python3 shortdist_forward_pathresolved.py         — печать в консоль
         python3 shortdist_forward_pathresolved.py --tg    — + компактная сводка в личку (owner_chat)
"""
from __future__ import annotations
import csv, math, os, sys, json
from datetime import datetime

try:
    import requests
except Exception:
    requests = None

# ── КОНФИГ ───────────────────────────────────────────────────────────────────
FORWARD_START   = "2026-05-29"   # сделки с этой даты = out-of-sample (после нашего анализа)
VOL_MIN_USD     = 50e6           # порог ликвидности (где maker реален)
FEE_RT_PCT      = 0.04           # maker round-trip (оценка)
SLIP_RT_PCT     = 0.03           # слиппедж на ликвиде (оценка)
FUND_INTERVAL_H = 8.0
TARGET_N        = 35             # сколько ФОРВАРД-сделок нужно для go/no-go
HORIZON         = "24h"

HERE     = os.path.dirname(os.path.abspath(__file__))
RESOLVED = os.path.join(HERE, "outcomes", "resolved.csv")
CONFIG   = os.path.join(HERE, "telegram_config.json")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _truthy(x):
    return str(x).strip() in ("1", "1.0", "True", "true")


def _is_target(r) -> bool:
    return (r.get("setup") or "").strip() == "short_dist" and \
           (r.get("direction") or "").strip().upper() in ("ШОРТ", "SHORT")


def _trend(r) -> str:
    return (r.get("btc_trend_4h") or "").strip().lower()


def _gross_r(r, path_resolved: bool):
    g = _f(r.get(f"r_multiple_{HORIZON}"))
    if g is None:
        return None
    if path_resolved and _truthy(r.get(f"hit_tp1_{HORIZON}")) and _truthy(r.get(f"hit_stop_{HORIZON}")):
        tmfe = _f(r.get(f"time_to_mfe_{HORIZON}_h"))   # TP-сторона (для шорта = ход вниз)
        tmae = _f(r.get(f"time_to_mae_{HORIZON}_h"))   # стоп-сторона (для шорта = ход вверх)
        if tmfe is not None and tmae is not None and tmae <= tmfe:
            return -1.0
    return g


def _net_r(r, path_resolved: bool = True):
    g    = _gross_r(r, path_resolved)
    e    = _f(r.get("price_entry"))
    s    = _f(r.get("stop"))
    hold = _f(r.get(f"hold_time_{HORIZON}_min"))
    fund = _f(r.get("funding"))
    if g is None or e is None or s is None or e <= 0:
        return None
    risk = abs(e - s) / e * 100.0
    if risk <= 0:
        return None
    n_fund = (hold / 60.0 / FUND_INTERVAL_H) if hold else 0.0
    funding_cost = (-1 * (fund or 0.0)) * n_fund      # SHORT платит при funding<0
    cost_r = (FEE_RT_PCT + SLIP_RT_PCT + funding_cost) / risk
    return g - cost_r


def _agg(xs):
    n = len(xs)
    if not n:
        return None
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n else 0.0
    lo, hi = m - 1.96 * se, m + 1.96 * se
    wr = 100.0 * sum(1 for x in xs if x > 0) / n
    return {"n": n, "mean": m, "total": sum(xs), "lo": lo, "hi": hi, "wr": wr,
            "sig": (lo > 0 or hi < 0)}


def _tag(a):
    if not a:
        return ""
    return "✅" if (a["sig"] and a["mean"] > 0) else ("⛔" if (a["sig"] and a["mean"] < 0) else "❓")


def _fmt(a):
    if not a:
        return "n=0"
    return f"n={a['n']} net_R={a['mean']:+.3f} CI[{a['lo']:+.2f},{a['hi']:+.2f}] {_tag(a)}"


def _line(label, xs):
    print(f"    {label:<22} {_fmt(_agg(xs))}")


def _date(r):
    ts = (r.get("run_ts") or "")[:10]
    try:
        datetime.strptime(ts, "%Y-%m-%d")
        return ts
    except ValueError:
        return None


def _send_personal(text: str) -> bool:
    """Шлёт в ЛИЧКУ (owner_chat_id), не в канал — зеркало check_score_gate._send_personal."""
    if requests is None:
        print("[tg] requests недоступен — не отправлено")
        return False
    try:
        cfg = json.load(open(CONFIG, encoding="utf-8"))
        token = cfg.get("bot_token")
        chat  = str(cfg.get("owner_chat_id") or cfg.get("chat_id"))
        if not token or not chat:
            print("[tg] нет token/owner_chat_id")
            return False
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        ok = resp.status_code == 200
        print(f"[tg] отправлено в личку: {'OK' if ok else 'FAIL ' + resp.text[:120]}")
        return ok
    except Exception as e:
        print(f"[tg] ошибка отправки: {type(e).__name__}: {e}")
        return False


def main():
    if not os.path.exists(RESOLVED):
        print(f"нет файла: {RESOLVED}")
        return
    rows = list(csv.DictReader(open(RESOLVED, encoding="utf-8")))
    insample = {"all": [], "below": [], "above": []}
    forward  = {"all": [], "below": [], "above": []}
    for r in rows:
        if not _is_target(r):
            continue
        v = _f(r.get("avg_vol_7d_usd"))
        if v is None or v < VOL_MIN_USD:
            continue
        nr = _net_r(r, path_resolved=True)
        if nr is None:
            continue
        d = _date(r)
        if d is None:
            continue
        bucket = forward if d >= FORWARD_START else insample
        bucket["all"].append(nr)
        t = _trend(r)
        if t in ("below", "above"):
            bucket[t].append(nr)

    print("=" * 72)
    print(f"  ФОРВАРД-ХАРНЕСС (PATH-RESOLVED): short_dist/SHORT, ликвид ≥${VOL_MIN_USD/1e6:.0f}M, MAKER")
    print(f"  косты: fee {FEE_RT_PCT}% + slip {SLIP_RT_PCT}% + funding | горизонт {HORIZON}")
    print(f"  out-of-sample С {FORWARD_START} | цель {TARGET_N} форвард-сделок")
    print("=" * 72)

    ia, ib, iab = _agg(insample["all"]), _agg(insample["below"]), _agg(insample["above"])
    print("\nIN-SAMPLE baseline, PATH-RESOLVED (ориентир, НЕ цель — это прошлое):")
    _line("все", insample["all"])
    _line("BTC below (регламент)", insample["below"])
    _line("BTC above", insample["above"])

    fa = _agg(forward["all"])
    print(f"\nFORWARD (out-of-sample, с {FORWARD_START}):")
    verdict = ""
    if not fa:
        print("    пока 0 форвард-сделок. Харнесс взведён — резолв идёт по мере выхода 24ч.")
        print("    (в pending.json уже ждут short_dist/SHORT — наполнится за дни.)")
    else:
        filled = min(fa["n"], TARGET_N)
        bar = "#" * filled + "." * max(0, TARGET_N - fa["n"])
        print(f"    [{bar}] {fa['n']}/{TARGET_N}")
        _line("все (вердикт)", forward["all"])
        _line("BTC below", forward["below"])
        _line("BTC above", forward["above"])
        if fa["n"] < TARGET_N:
            verdict = f"рано ({fa['n']}/{TARGET_N})"
        elif fa["sig"] and fa["mean"] > 0:
            verdict = "✅ GO — эдж подтверждён, можно малыми деньгами"
        elif fa["sig"] and fa["mean"] < 0:
            verdict = "⛔ STOP — значимо убыточно, закрываем"
        else:
            verdict = "❓ CI через ноль — не подтверждён, закрываем, не тюним"
        print(f"    -> {verdict}")

    print("\nЗАШИТОЕ ПРАВИЛО: при n≥%d CI чистого R не выше нуля → закрыть, не тюнить; выше нуля → малые деньги." % TARGET_N)
    print("Ограничения: (1) net-R при ДОПУЩЕНИИ филла лимитки — реальный fill-rate даст только бумага/тестнет;")
    print("(2) path-тайминг = прокси; (3) BTC-режим конфаундлен с месяцем — форвард проверит; (4) CI без поправки на кластер → ШИРЕ.")

    # ── компактная сводка в личку ──
    if "--tg" in sys.argv:
        if not fa:
            fwd_line = f"Forward: 0/{TARGET_N} — копится (резолв по расписанию скринера)"
        else:
            fwd_line = (f"Forward: {fa['n']}/{TARGET_N}  все: {_fmt(fa)}\n"
                        f"  BTC below: {_fmt(_agg(forward['below']))}\n  → {verdict}")
        msg = (
            "📉 <b>short_dist/SHORT форвард</b> (path-resolved)\n"
            f"{fwd_line}\n"
            f"in-sample ориентир: все {_fmt(ia)} | BTC below {_fmt(ib)}\n"
            f"правило: n≥{TARGET_N} &amp; CI≤0 → закрыть; CI&gt;0 → малые деньги"
        )
        _send_personal(msg)


if __name__ == "__main__":
    main()

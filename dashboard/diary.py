#!/usr/bin/env python3
"""📓 Дневник трейдера — предрегистрированные ожидания и экзамен исходов.

Заказ брата 2026-07-07: «все сделки, которые всплыли в зонах входа (даже
неотработавшие): монета — диапазон — итог; и самообучение, которое не
обучается хуйне».

ТРИ ЗАМКА ПРОТИВ САМООБМАНА (менять только с явного добра брата):
1. expectation ЗАМОРАЖИВАЕТСЯ в момент появления сигнала в зоне и НИКОГДА
   не переписывается — экзамен всегда «прогноз ДО» vs «факт ПОСЛЕ».
2. Вердикт «сюрприз» объявляется ТОЛЬКО при n_class ≥ MIN_CLASS_N закрытых
   кейсов; иначе честное «⏳ копим базу» (никаких выводов с трёх примеров).
3. Сюрпризы КОПЯТСЯ (библиотека со снапшотом контекста), но НЕ меняют ни
   одно торговое правило — правила рождаются только через side_study +
   форвард-экзамен (конвейер версий, как bias v1→v2→v3).

Вердикт — квартильный забор Тьюки по закрытым кейсам класса (робастно к
малым n, без предположений о распределении):
  peak24 > p75 + 1.5·IQR → 🚀 сюрприз вверх
  peak24 < p25 − 1.5·IQR → 💥 сюрприз вниз
  внутри [p25..p75]      → ✅ в рамках (вера класса крепнет)
  иначе                  → 〰 в хвосте нормы

Холодный старт ожиданий: пока в дневнике < MIN_CLASS_N закрытых кейсов
класса, база берётся из outcomes/radar_resolved.csv (mfe/mae как прокси
peak/dd; major=0; awakening = vol_ratio≥15, radar_alt = cascade=0) с
пометкой src, чтобы прокси-базу было видно и позже вытеснить своей.
"""
from __future__ import annotations

import csv
import json
import math
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DIR = Path(__file__).resolve().parent
TRADING = DIR.parent
sys.path.insert(0, str(TRADING))

from file_lock import atomic_json_update, atomic_json_read  # noqa: E402

DIARY_PATH = DIR / "diary.json"
ENTRY_LOG = TRADING / "outcomes" / "entry_candidates.csv"
RADAR_RESOLVED = TRADING / "outcomes" / "radar_resolved.csv"

MIN_CLASS_N = 10          # замок №2: до этого порога вердикт = «копим базу»
HORIZON_H = 24            # экзамен по peak/dd/ret за 24ч от появления в зоне
DEAD_GRACE_H = 48         # signal_ts + HORIZON + это = запись no_data, из очереди (S1)
AWAKENING_RATIO = 15.0
# Только витрина дневника: endpoint RET24 минус фиксированный круг costs.
# Это НЕ PnL, не учитывает funding/slippage и не участвует в сигналах/вердиктах.
COST_GRID_PCT = (0.14, 0.31, 0.71)
MIN_DAY_CLUSTERS_FOR_T = 5


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _quartiles(vals: list[float]) -> dict:
    """p25/p50/p75 без numpy (линейная интерполяция)."""
    xs = sorted(vals)
    n = len(xs)

    def q(p: float) -> float:
        if n == 1:
            return xs[0]
        k = (n - 1) * p
        lo, hi = int(k), min(int(k) + 1, n - 1)
        return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 2)

    return {"p25": q(0.25), "p50": q(0.5), "p75": q(0.75)}


def _endpoint24_stats(kind: str, closed_records: list[dict]) -> dict:
    """Read-only статистика исполнимого endpoint-а дневника.

    MFE/peak24 остаётся отдельным описанием доступной волатильности и базой
    старого экзамена сюрпризов. Здесь намеренно считается только ret24 — close
    последнего 15m-бара в 24ч-окне — плюс грубая сетка ret24−cost. Это НЕ
    торговый отчёт: funding, проскальзывание и исполнение лимиток не известны.

    Обычный t по монетам здесь запрещён: записи одного дня/скана коррелированы.
    Поэтому выводится только t по дневным средним и лишь от пяти UTC-дней;
    иначе честное «insufficient», а не ложная значимость.
    """
    rets: list[float] = []
    by_day: dict[str, list[float]] = {}
    scans: set[int] = set()

    for rec in closed_records:
        if rec.get("kind") != kind:
            continue
        outcome = rec.get("outcome") or {}
        ret = outcome.get("ret24")
        if not outcome.get("final") or not isinstance(ret, (int, float)):
            continue
        rets.append(float(ret))
        try:
            ts = datetime.fromisoformat(rec["signal_ts"])
        except (KeyError, TypeError, ValueError):
            continue
        day = ts.astimezone(timezone.utc).date().isoformat()
        by_day.setdefault(day, []).append(float(ret))
        scans.add(int(ts.timestamp()) // 300)

    out = {
        "n": len(rets),
        "utc_days": len(by_day),
        "scan_clusters": len(scans),
        "median_ret24": None,
        "mean_ret24": None,
        "mean_ret24_after_cost": {},
        "day_t": None,
        "day_t_status": "insufficient",
    }
    if not rets:
        return out

    mean_ret = sum(rets) / len(rets)
    out["median_ret24"] = _quartiles(rets)["p50"]
    out["mean_ret24"] = round(mean_ret, 2)
    out["mean_ret24_after_cost"] = {
        f"{cost:.2f}": round(mean_ret - cost, 2) for cost in COST_GRID_PCT
    }

    day_means = [sum(xs) / len(xs) for xs in by_day.values()]
    if len(day_means) < MIN_DAY_CLUSTERS_FOR_T:
        return out
    avg = sum(day_means) / len(day_means)
    variance = sum((x - avg) ** 2 for x in day_means) / (len(day_means) - 1)
    se = math.sqrt(variance) / math.sqrt(len(day_means))
    if se == 0:
        out["day_t_status"] = "zero_dispersion"
    else:
        out["day_t"] = round(avg / se, 2)
        out["day_t_status"] = "ok"
    return out


def _cold_start_base(kind: str) -> list[float]:
    """Прокси-база peak24 из radar_resolved (закрытые mfe эпизодов)."""
    try:
        rows = list(csv.DictReader(open(RADAR_RESOLVED, encoding="utf-8")))
    except FileNotFoundError:
        return []
    out, seen = [], {}
    for r in rows:
        if r.get("major") == "1":
            continue
        try:
            ts = datetime.fromisoformat(r["ts_utc"])
            ratio = float(r["vol_ratio"])
            mfe = float(r["mfe_pct"])
        except Exception:
            continue
        last = seen.get(r["symbol"])
        if last and abs((ts - last).total_seconds()) < 24 * 3600:
            continue
        seen[r["symbol"]] = ts
        is_awk = ratio >= AWAKENING_RATIO
        if kind == "awakening" and is_awk:
            out.append(mfe)
        elif kind == "radar_alt" and not is_awk and r.get("cascade") == "0":
            out.append(mfe)
    return out


def mark_waves() -> int:
    """Диспансеризация (07.07): пометить волновые записи (≥5 radar_alt одного
    5-мин скана — 06.07 сервер логировал волну, фронт скрывал). Помеченные
    исключаются из own-базы ожиданий. Замороженные expectation НЕ трогаем
    (замок №1) — чинится линейка будущих ожиданий, не история."""
    def mut(d):
        d = d or {"records": []}
        buckets: dict[int, list] = {}
        for r in d["records"]:
            if r.get("kind") != "radar_alt":
                continue
            try:
                b = int(datetime.fromisoformat(r["signal_ts"]).timestamp()) // 300
            except Exception:
                continue
            buckets.setdefault(b, []).append(r)
        n = 0
        for b, recs in buckets.items():
            if len(recs) >= 5:
                for r in recs:
                    if not (r.get("ctx") or {}).get("wave"):
                        r.setdefault("ctx", {})["wave"] = True
                        n += 1
        return d
    atomic_json_update(DIARY_PATH, mut, default={"records": []})
    return _last_marked(mut)


def _last_marked(_):   # счётчик через повторное чтение (mut внутри лока)
    d = atomic_json_read(DIARY_PATH, default={"records": []}) or {"records": []}
    return sum(1 for r in d["records"] if (r.get("ctx") or {}).get("wave"))


def build_expectation(kind: str, closed_records: list[dict]) -> dict:
    """Ожидание класса из ЗАКРЫТЫХ записей дневника; холодный старт — прокси.
    Волновые записи (ctx.wave) в базу НЕ идут — коррелированы (см. mark_waves).
    Возвращаемый дикт замораживается в записи (замок №1)."""
    own = [r["outcome"]["peak24"] for r in closed_records
           if r.get("kind") == kind and (r.get("outcome") or {}).get("final")
           and r["outcome"].get("peak24") is not None
           and not (r.get("ctx") or {}).get("wave")]
    if len(own) >= MIN_CLASS_N:
        base, src = own, "diary"
    else:
        proxy = _cold_start_base(kind)
        if len(own) + len(proxy) >= MIN_CLASS_N:
            base, src = own + proxy, f"diary({len(own)})+proxy({len(proxy)})"
        else:
            base, src = own + proxy, "insufficient"
    exp = {"class": kind, "n_class": len(base), "src": src,
           "frozen_at": utcnow().replace(microsecond=0).isoformat()}
    if base:
        qs = _quartiles(base)
        exp.update(qs)
        exp["move_rate"] = round(sum(1 for x in base if x >= 5) / len(base), 2)
    return exp


def grade(exp: dict, outcome: dict) -> dict:
    """Экзамен факта против замороженного ожидания. Замок №2: при
    src=insufficient или n<MIN_CLASS_N — только «collect», без сюрпризов."""
    peak = (outcome or {}).get("peak24")
    if peak is None:
        return {"verdict": "pending"}
    if exp.get("src") == "insufficient" or exp.get("n_class", 0) < MIN_CLASS_N:
        return {"verdict": "collect",
                "note": f"база класса мала (n={exp.get('n_class', 0)}<{MIN_CLASS_N}) — копим, выводов нет"}
    p25, p75 = exp["p25"], exp["p75"]
    iqr = max(p75 - p25, 0.1)
    hi_fence, lo_fence = p75 + 1.5 * iqr, p25 - 1.5 * iqr
    if peak > hi_fence:
        return {"verdict": "surprise_up",
                "note": f"peak {peak:+.1f}% > забор {hi_fence:+.1f}% (ожидание p50 {exp['p50']:+.1f}%) — в библиотеку сюрпризов"}
    if peak < lo_fence:
        return {"verdict": "surprise_down",
                "note": f"peak {peak:+.1f}% < забор {lo_fence:+.1f}% — класс не дал даже минимума"}
    if p25 <= peak <= p75:
        return {"verdict": "confirm",
                "note": f"в рамках ожидания [{p25:+.1f}..{p75:+.1f}] — вера класса крепнет"}
    return {"verdict": "tail_ok",
            "note": f"в хвосте нормы (забор {lo_fence:+.1f}..{hi_fence:+.1f})"}


def _chg24_at(symbol: str, signal_ts: str, age_h: float,
              current_chg24: float | None) -> float | None:
    """24ч-изменение цены НА МОМЕНТ СИГНАЛА (ревью 07.07 M1: метка «вторая
    волна» мерялась на момент лога — look-ahead для поздних заходов, у
    пробуждений лог бывает на часы позже сигнала). age_h ≤ 0.25 — лог почти
    мгновенный (тик 60с), берём текущее без сети; иначе считаем по 1ч-барам
    close(signal) vs close(signal−24ч). None = честно «не знаем»."""
    if age_h <= 0.25:
        return current_chg24
    try:
        t_sig = int(datetime.fromisoformat(signal_ts).timestamp() * 1000)
        url = (f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}"
               f"&interval=60&start={t_sig - 25 * 3600_000}&end={t_sig}&limit=30")
        with urllib.request.urlopen(url, timeout=8) as r:
            d = json.load(r)
        if d.get("retCode") != 0:
            return None
        bars = sorted((int(x[0]), float(x[4])) for x in d["result"]["list"])
        if len(bars) < 20:
            return None
        c_now = bars[-1][1]
        c_then = min(bars, key=lambda b: abs(b[0] - (t_sig - 24 * 3600_000)))[1]
        return round((c_now - c_then) / c_then * 100, 2) if c_then > 0 else None
    except Exception:
        return None


def _fetch_bars_15m(symbol: str, start_ms: int, end_ms: int) -> list:
    url = (f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}"
           f"&interval=15&start={start_ms}&end={end_ms}&limit=120")
    with urllib.request.urlopen(url, timeout=12) as r:
        d = json.load(r)
    if d.get("retCode") != 0:
        raise RuntimeError(f"retCode={d.get('retCode')}")
    now_ms = int(utcnow().timestamp() * 1000)
    bars = sorted((int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]))
                  for x in d.get("result", {}).get("list") or [])
    return [b for b in bars if b[0] + 15 * 60_000 <= now_ms]


def resolve_record(rec: dict) -> dict | None:
    """peak/dd/ret за 24ч от basis, бары строго ПОСЛЕ signal_ts (анти-look-ahead:
    бар входа выброшен, как всюду в проекте). None = данных пока нет."""
    t0 = int(datetime.fromisoformat(rec["signal_ts"]).timestamp() * 1000)
    try:
        bars = _fetch_bars_15m(rec["symbol"], t0, t0 + (HORIZON_H + 1) * 3600_000)
    except Exception:
        return None
    first_full = ((t0 // 900_000) + 1) * 900_000
    full = [b for b in bars if b[0] >= first_full]
    post = full[1:]                                      # бар входа выброшен
    if not post:
        return None
    basis = rec.get("basis")
    if not basis:
        # fast-связка без basis (не было снапшота при рождении): та же
        # конвенция входа — close первого ПОЛНОГО бара после поста
        basis = full[0][4]
        if not basis:
            return None
    horizon_end = t0 + HORIZON_H * 3600_000
    inwin = [b for b in post if b[0] < horizon_end]
    if not inwin:
        inwin = post[:1]
    peak = max((b[2] - basis) / basis * 100 for b in inwin)
    dd = min((b[3] - basis) / basis * 100 for b in inwin)
    ret = (inwin[-1][4] - basis) / basis * 100
    final = post and post[-1][0] + 15 * 60_000 >= horizon_end
    return {"peak24": round(peak, 2), "dd24": round(dd, 2),
            "ret24": round(ret, 2), "bars": len(inwin), "final": bool(final),
            "basis": round(basis, 8)}


def upsert_new(extra_combo: list[dict] | None = None,
               btc_ret24: float | None = None,
               chg24_map: dict | None = None,
               btc_range24: float | None = None) -> int:
    """Затянуть в дневник новые записи: все entry-кандидаты из CSV + связки.
    Идемпотентно по id. Ожидание замораживается ЗДЕСЬ, в момент рождения."""
    rows = []
    if ENTRY_LOG.exists():
        rows = list(csv.DictReader(open(ENTRY_LOG, encoding="utf-8")))
    combos = extra_combo or []
    added = 0

    def mut(d):
        nonlocal added
        d = d or {"records": []}
        have = {r["id"] for r in d["records"]}
        closed = [r for r in d["records"] if (r.get("outcome") or {}).get("final")]
        for r in rows:
            rid = f"{r['symbol']}|{r['signal_ts_utc']}"
            if rid in have:
                continue
            kind = r["kind"]
            rec = {
                "id": rid, "kind": kind, "symbol": r["symbol"],
                # радар/пробуждение = всегда лонг от базиса; шорт-класс,
                # если появится, обязан принести side из источника явно
                "side": "long",
                "signal_ts": r["signal_ts_utc"], "zone_ts": r["logged_ts_utc"],
                "basis": float(r["basis"]),
                "ctx": {"live_at_zone": float(r["last_pct"]),
                        "dd_at_zone": float(r["dd_pct"]),
                        "age_h": float(r["age_h"]),
                        "vol_ratio": float(r["vol_ratio"]) if r.get("vol_ratio") else None,
                        "btc_ret24": btc_ret24, "btc_range24": btc_range24,
                        # «вторая волна» (≥+10%/24ч до сигнала) — когорта для
                        # форвард-разреза (кейс ALLO 07.07); None = не знаем
                        "chg24_at_zone": (chg24_map or {}).get(r["symbol"]),
                        # когорта «вторых волн» = chg24_at_signal ≥ 10 (факт
                        # храним, производное считаем при разрезе)
                        "chg24_at_signal": _chg24_at(r["symbol"], r["signal_ts_utc"],
                                                     float(r["age_h"]),
                                                     (chg24_map or {}).get(r["symbol"]))},
                "expectation": build_expectation(kind, closed),
                "outcome": None, "grade": {"verdict": "pending"},
            }
            d["records"].append(rec)
            have.add(rid)
            added += 1
        for c in combos:
            # шорты пока НЕ встраиваем (решение брата 08.07): экзамен дневника
            # меряет пик ВВЕРХ от базиса — шорт-пост лонговой линейкой не мерить,
            # в базы классов не пускать (на Пульсе связка живёт как жила)
            if (c.get("direction") or "").lower() == "short":
                continue
            # id по msg_key: быстрая (превью, 60с) и каноническая (Telethon,
            # 15 мин) версии одного поста = ОДНА запись дневника, без дублей
            # id по ПРОБУЖДЕНИЮ (awake_ts): подтверждение поста меняет msg_key
            # (последний alert) → был дубль записи (ревью 07.07 S2); пробуждение
            # стабильно задаёт событие связки на всём её жизненном цикле
            rid = f"{c['symbol']}|{c.get('awake_ts') or c.get('msg_key') or c['post_ts']}|combo"
            if rid in have:
                continue
            d["records"].append({
                "id": rid, "kind": "combo", "symbol": c["symbol"],
                # сторона связки — из направления поста канала; неизвестно → None (фронт покажет «—»)
                "side": c.get("direction") if c.get("direction") in ("long", "short") else None,
                "signal_ts": c["post_ts"], "zone_ts": utcnow().isoformat(),
                "basis": c.get("basis"),
                "ctx": {"awake_ratio": c.get("awake_ratio"),
                        "channel": c.get("channel"), "direction": c.get("direction"),
                        "btc_ret24": btc_ret24},
                "expectation": build_expectation("combo", closed),
                "outcome": None, "grade": {"verdict": "pending"},
            })
            have.add(rid)
            added += 1
        d["records"] = d["records"][-800:]
        return d

    atomic_json_update(DIARY_PATH, mut, default={"records": []})
    return added


def resolve_pending(max_fetch: int = 25) -> int:
    """Резолв незакрытых записей (снимок → сеть → merge под локом).

    Анти-голодание (ревью 07.07 S1): записи старше HORIZON+DEAD_GRACE_H без
    успешного резолва финализируются status=no_data БЕЗ сетевой попытки и
    навсегда выходят из очереди — мёртвый хвост (делисты, битые символы)
    не съедает бюджет max_fetch у живых. no_data в базы ожиданий не попадает
    (туда берутся только записи с числовым peak24)."""
    snap = atomic_json_read(DIARY_PATH, default={"records": []}) or {"records": []}
    updates: dict[str, dict] = {}
    fetched = 0
    now = utcnow()
    for rec in snap["records"]:
        if (rec.get("outcome") or {}).get("final"):
            continue
        try:
            age_h = (now - datetime.fromisoformat(rec["signal_ts"])).total_seconds() / 3600
        except Exception:
            age_h = None
        if age_h is not None and age_h > HORIZON_H + DEAD_GRACE_H:
            updates[rec["id"]] = {"outcome": {"status": "no_data", "final": True},
                                  "grade": {"verdict": "no_data",
                                            "note": f"свечей не дождались за {age_h:.0f}ч — из очереди"}}
            continue
        if fetched >= max_fetch:
            break
        o = resolve_record(rec)
        fetched += 1
        if o:
            # вердикт — ТОЛЬКО по закрытым суткам; промежуточный пик не судим
            g = grade(rec.get("expectation") or {}, o) if o.get("final") \
                else {"verdict": "in_progress"}
            updates[rec["id"]] = {"outcome": o, "grade": g}
    if not updates:
        return 0

    def mut(d):
        d = d or {"records": []}
        for r in d["records"]:
            u = updates.get(r["id"])
            if u:
                r["outcome"] = u["outcome"]
                r["grade"] = u["grade"]
                # fast-связка родилась без basis → доустановить из резолва
                if not r.get("basis") and u["outcome"].get("basis"):
                    r["basis"] = u["outcome"]["basis"]
        return d

    atomic_json_update(DIARY_PATH, mut, default={"records": []})
    return len(updates)


def diary_block(limit: int = 120) -> dict:
    """Блок для фида: записи (свежие первыми) + агрегаты «что бот выучил»."""
    d = atomic_json_read(DIARY_PATH, default={"records": []}) or {"records": []}
    recs = sorted(d["records"], key=lambda r: r.get("zone_ts") or "", reverse=True)
    closed = [r for r in recs if (r.get("outcome") or {}).get("final")]
    verdicts = {}
    for r in closed:
        v = (r.get("grade") or {}).get("verdict", "pending")
        verdicts[v] = verdicts.get(v, 0) + 1
    classes = {}
    for kind in ("awakening", "radar_alt", "combo"):
        # guard 11.07: цензурированный outcome-обрубок (VET: только final/status,
        # без peak24) не должен валить весь diary-блок фида — fail-open по записи
        own = [r["outcome"]["peak24"] for r in closed
               if r["kind"] == kind and (r.get("outcome") or {}).get("peak24") is not None]
        c = {"n_closed": len(own)}
        if own:
            c.update(_quartiles(own))
            c["move_rate"] = round(sum(1 for x in own if x >= 5) / len(own), 2)
        exp_now = build_expectation(kind, closed)
        c["expectation_now"] = {k: exp_now.get(k) for k in
                                ("n_class", "src", "p25", "p50", "p75", "move_rate")}
        c["endpoint24"] = _endpoint24_stats(kind, closed)
        classes[kind] = c
    surprises = [r for r in recs
                 if (r.get("grade") or {}).get("verdict") in ("surprise_up", "surprise_down")][:20]
    return {"records": recs[:limit], "n_total": len(recs), "n_closed": len(closed),
            "verdict_counts": verdicts, "classes": classes,
            "surprises": [r["id"] for r in surprises]}


def _selfcheck() -> None:
    # квартили и заборы
    exp = {"class": "t", "n_class": 12, "src": "diary",
           "p25": 1.0, "p50": 2.5, "p75": 5.0}
    assert grade(exp, {"peak24": 3.0})["verdict"] == "confirm"
    assert grade(exp, {"peak24": 30.0})["verdict"] == "surprise_up"    # > 5+6=11
    assert grade(exp, {"peak24": -8.0})["verdict"] == "surprise_down"  # < 1-6=-5
    assert grade(exp, {"peak24": 8.0})["verdict"] == "tail_ok"
    assert grade(exp, {})["verdict"] == "pending"
    # замок №2: тонкая база не даёт сюрпризов
    thin = {"class": "t", "n_class": 3, "src": "insufficient", "p25": 1, "p50": 2, "p75": 3}
    assert grade(thin, {"peak24": 99.0})["verdict"] == "collect"
    # квартильная механика
    qs = _quartiles([1, 2, 3, 4, 5])
    assert qs == {"p25": 2.0, "p50": 3.0, "p75": 4.0}, qs
    # замок №1 (заморозка) — проверка процессом: build_expectation зовётся
    # ТОЛЬКО в upsert_new; resolve_pending/grade ожидание не пересчитывают
    import inspect
    src = inspect.getsource(resolve_pending) + inspect.getsource(grade)
    assert "build_expectation" not in src, "resolve/grade не должны трогать ожидание!"
    print("diary selfcheck OK (заборы, замки 1-2, квартили)")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    elif "--mark-waves" in sys.argv:
        print(f"волновых помечено всего: {mark_waves()}")
    elif "--resolve" in sys.argv:
        n = resolve_pending()
        print(f"резолвнуто: {n}")
    elif "--upsert" in sys.argv:
        print(f"добавлено: {upsert_new()}")
    else:
        b = diary_block(limit=5)
        print(json.dumps({k: b[k] for k in ("n_total", "n_closed", "verdict_counts", "classes")},
                         ensure_ascii=False, indent=1))

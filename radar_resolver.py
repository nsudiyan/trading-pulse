#!/usr/bin/env python3
"""radar_resolver.py — форвард-резолвер алертов vol_radar (outcomes/radar_hits.csv).

Закрывает пункт №2 аудита 2026-07-01 («резолвера radar_hits.csv нет») и превращает
поток алертов в честную статистику вместо отобранных скриншотов.

КРИТЕРИЙ «ХОРОШЕГО» АЛЕРТА — предрегистрирован братом 2026-07-02, НЕ менять
задним числом (менять можно только вперёд, отдельным решением, с пометкой даты):
    окно 6ч от алерта, ход в сторону большего движения (MFE) >= 5%
    при ходе против (MAE до пика) <= 1.5%. Сторона не предсказывается.

По каждой строке hits пишет в outcomes/radar_resolved.csv (append-only):
    dir (up/down/skip), mfe_pct, mae_pct, t2_h (часов до |хода| 2%), good (0/1),
    плюс major/cascade/sent — срезы для отчёта (мажоры vs альты, каскад vs одиночка).
Резолвит только закрытые окна (ts + 6ч + 30м < now); свечи — 1м Bybit.

Запуск:  python3 radar_resolver.py            # дорезолвить хвост
         python3 radar_resolver.py --dry-run  # печать без записи
         python3 radar_resolver.py --selfcheck
Также вызывается из storm_report.py на каждом ежедневном тике.
"""
from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vol_radar import MAJOR_SYMBOLS

BYBIT = "https://api.bybit.com/v5/market/kline"
OUT = Path(__file__).parent / "outcomes"
HITS_PATH = OUT / "radar_hits.csv"
RESOLVED_PATH = OUT / "radar_resolved.csv"

WINDOW_H = 6.0            # окно замера (критерий брата: движение в первые часы)
SETTLE_MIN = 30           # запас после окна, чтобы последний 1м бар точно закрылся
MFE_MIN_PCT = 5.0         # «хороший»: ход >= 5%...
MAE_MAX_PCT = 1.5         # ...при пиле против <= 1.5% (до бара пика включительно)
T2_LEVEL_PCT = 2.0        # справочная метрика: часов до |хода| >= 2%
CASCADE_WINDOW_S = 120    # >=3 алертов в +/-120с = рыночный каскад (метка для срезов)
CASCADE_N = 3
MIN_BARS_FRAC = 0.5       # < половины 1м баров окна -> данных мало (делист/новый перп)
SKIP_AFTER_H = 48.0       # ...если алерту уже >48ч и баров так и нет — пишем skip

FIELDS = ["ts_utc", "symbol", "vol_ratio", "sent", "major", "cascade",
          "dir", "mfe_pct", "mae_pct", "t2_h", "good"]


def fetch_1m(symbol: str, start_ms: int, end_ms: int) -> list[tuple]:
    """1м свечи (ts, high, low) по возрастанию; окно 6.5ч < лимита 1000 — без пагинации.
    retCode != 0 — RAISE (транзиентная ошибка → «отложен», НЕ необратимый skip
    через 48ч); пустой list при retCode=0 = данных честно нет (ревью 2026-07-06 #6)."""
    url = (f"{BYBIT}?category=linear&symbol={symbol}&interval=1"
           f"&start={start_ms}&end={end_ms}&limit=1000")
    with urllib.request.urlopen(url, timeout=15) as r:
        data = json.load(r)
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit retCode={data.get('retCode')}: {data.get('retMsg')}")
    rows = data.get("result", {}).get("list") or []
    return sorted((int(x[0]), float(x[2]), float(x[3])) for x in rows)


def resolve_metrics(p0: float, bars: list[tuple], t0_ms: int) -> dict:
    """Чистая метрика окна (тестируемо). bars: [(ts, high, low)] по возрастанию.
    dir = сторона БОЛЬШЕГО хода; MAE = худший ход против до бара пика включительно
    (может быть < 0, если цена вообще не заходила против). Всё в % от p0."""
    up = max(b[1] for b in bars) / p0 - 1
    dn = 1 - min(b[2] for b in bars) / p0
    if dn >= up:
        i_ext = min(range(len(bars)), key=lambda i: bars[i][2])
        mfe, mae = dn, max(b[1] for b in bars[:i_ext + 1]) / p0 - 1
        d = "down"
    else:
        i_ext = max(range(len(bars)), key=lambda i: bars[i][1])
        mfe, mae = up, 1 - min(b[2] for b in bars[:i_ext + 1]) / p0
        d = "up"
    t2 = next((round((b[0] - t0_ms) / 3600_000, 2) for b in bars
               if b[1] / p0 - 1 >= T2_LEVEL_PCT / 100 or 1 - b[2] / p0 >= T2_LEVEL_PCT / 100),
              None)
    mfe_pct, mae_pct = round(mfe * 100, 2), round(mae * 100, 2)
    return {"dir": d, "mfe_pct": mfe_pct, "mae_pct": mae_pct, "t2_h": t2,
            "good": int(mfe_pct >= MFE_MIN_PCT and mae_pct <= MAE_MAX_PCT)}


def cascade_flags(rows: list[dict]) -> list[bool]:
    """Метка каскада по соседям в ЛОГЕ (не по скану — скан-границ в CSV нет)."""
    ts = [datetime.fromisoformat(r["ts_utc"]).replace(tzinfo=timezone.utc) for r in rows]
    return [sum(1 for t in ts if abs((t - ts[i]).total_seconds()) <= CASCADE_WINDOW_S)
            >= CASCADE_N for i in range(len(rows))]


def _resolved_keys() -> set[str]:
    if not RESOLVED_PATH.exists():
        return set()
    with RESOLVED_PATH.open() as f:
        return {f"{r['ts_utc']}|{r['symbol']}" for r in csv.DictReader(f)}


def resolve_new(max_rows: int = 400, dry_run: bool = False) -> tuple[int, int]:
    """Дорезолвить все закрытые нерезолвлённые окна. Возвращает (записано, skip)."""
    if not HITS_PATH.exists():
        return 0, 0
    with HITS_PATH.open() as f:
        hits = list(csv.DictReader(f))
    casc = cascade_flags(hits)
    seen = _resolved_keys()
    now = datetime.now(timezone.utc)
    deadline = now - timedelta(hours=WINDOW_H, minutes=SETTLE_MIN)

    todo = [(r, casc[i]) for i, r in enumerate(hits)
            if f"{r['ts_utc']}|{r['symbol']}" not in seen
            and datetime.fromisoformat(r["ts_utc"]).replace(tzinfo=timezone.utc) < deadline]
    todo = todo[:max_rows]
    n_new = n_skip = 0
    out_rows = []
    for r, is_casc in todo:
        t0 = datetime.fromisoformat(r["ts_utc"]).replace(tzinfo=timezone.utc)
        t0_ms = int(t0.timestamp() * 1000)
        end_ms = t0_ms + int(WINDOW_H * 3600_000)
        try:
            bars = fetch_1m(r["symbol"], t0_ms, end_ms)
        except Exception as e:
            print(f"[resolver] {r['symbol']} {r['ts_utc']}: свечи не получены ({e}), отложен")
            continue
        base = {"ts_utc": r["ts_utc"], "symbol": r["symbol"],
                "vol_ratio": r["vol_ratio"], "sent": r["sent"],
                "major": int(r["symbol"] in MAJOR_SYMBOLS), "cascade": int(is_casc)}
        if len(bars) < WINDOW_H * 60 * MIN_BARS_FRAC:
            if (now - t0).total_seconds() > SKIP_AFTER_H * 3600:
                out_rows.append({**base, "dir": "skip", "mfe_pct": "", "mae_pct": "",
                                 "t2_h": "", "good": ""})
                n_skip += 1
            continue  # свежий пробел данных — попробуем следующим тиком
        m = resolve_metrics(float(r["price"]), bars, t0_ms)
        m["t2_h"] = "" if m["t2_h"] is None else m["t2_h"]
        out_rows.append({**base, **m})
        n_new += 1
        time.sleep(0.1)

    if dry_run:
        for x in out_rows:
            print(x)
        print(f"[resolver] dry-run: резолвил бы {n_new}, skip {n_skip}")
        return n_new, n_skip
    if out_rows:
        new_file = not RESOLVED_PATH.exists() or RESOLVED_PATH.stat().st_size == 0
        with RESOLVED_PATH.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new_file:
                w.writeheader()
            w.writerows(out_rows)
    print(f"[resolver] записано {n_new} (skip {n_skip}), всего нерезолвлённых было {len(todo)}")
    return n_new, n_skip


def quality_lines(since_iso_naive: str) -> list[str]:
    """Сводка качества радар-карточек для шторм-отчёта (по resolved-строкам
    строго после since). Формат согласован с build_message storm_report."""
    if not RESOLVED_PATH.exists():
        return []
    with RESOLVED_PATH.open() as f:
        rows = [r for r in csv.DictReader(f)
                if r["ts_utc"] > since_iso_naive and r["dir"] != "skip"]
    if not rows:
        return []

    def cnt(sel):
        g = sum(1 for r in sel if r["good"] == "1")
        return f"{g}/{len(sel)} ({100 * g // len(sel)}%)" if sel else "—"

    alts_single = [r for r in rows if r["major"] == "0" and r["cascade"] == "0"]
    majors = [r for r in rows if r["major"] == "1"]
    casc = [r for r in rows if r["cascade"] == "1"]
    sent = [r for r in rows if r["sent"] == "1"]
    mfes = sorted(float(r["mfe_pct"]) for r in rows)
    return ["",
            f"🔊 <b>Радар-карточки</b> (6ч-критерий: ход ≥{MFE_MIN_PCT:g}% при пиле ≤{MAE_MAX_PCT:g}%)",
            f"      хороших: всего {cnt(rows)} · доставленных {cnt(sent)}",
            f"      альты-одиночки {cnt(alts_single)} · мажоры {cnt(majors)} · каскадные {cnt(casc)}",
            f"      медиана хода за 6ч: {mfes[len(mfes) // 2]:+.1f}%"]


def selfcheck() -> int:
    ms = lambda i: 1_700_000_000_000 + i * 60_000  # синтетические минуты
    # чистый дамп: -10% без захода вверх -> good, dir=down
    bars = [(ms(i), 100.5 - i * 0.02, 100 - i * 0.028) for i in range(360)]
    m = resolve_metrics(100.0, bars, ms(0))
    assert m["dir"] == "down" and m["mfe_pct"] >= 10 and m["mae_pct"] <= 0.5, m
    assert m["good"] == 1 and m["t2_h"] is not None and m["t2_h"] < 2.0, m
    # пила: сначала +3%, потом слив -6% -> MAE 3% до пика, good=0
    bars = ([(ms(i), 100 + i * 0.3, 99.9 + i * 0.3) for i in range(11)]        # вверх до 103
            + [(ms(11 + i), 103 - i * 0.05, 94 if i == 179 else 103 - i * 0.05 - 0.1)
               for i in range(180)])                                            # вниз до 94
    m = resolve_metrics(100.0, bars, ms(0))
    assert m["dir"] == "down" and m["good"] == 0 and m["mae_pct"] >= 2.9, m
    # флэт: ничего не произошло -> good=0, t2 None
    bars = [(ms(i), 100.4, 99.6) for i in range(360)]
    m = resolve_metrics(100.0, bars, ms(0))
    assert m["good"] == 0 and m["t2_h"] is None and m["mfe_pct"] < 1.0, m
    # чистый памп: +8% при заходе вниз 0.8% -> good, dir=up; t2 по бару пересечения 2%
    bars = [(ms(0), 100.2, 99.2)] + [(ms(1 + i), 100 + (i + 1) * 0.045, 99.9 + i * 0.045)
                                     for i in range(178)]
    m = resolve_metrics(100.0, bars, ms(0))
    assert m["dir"] == "up" and m["good"] == 1 and abs(m["mae_pct"] - 0.8) < 0.01, m
    assert m["t2_h"] is not None and 0.5 < m["t2_h"] < 1.0, m
    # граница критерия: MFE ровно 5.0 и MAE ровно 1.5 -> good=1 (нестрогие)
    bars = [(ms(0), 100.1, 98.5), (ms(1), 105.0, 100.0)]
    m = resolve_metrics(100.0, bars, ms(0))
    assert m["mfe_pct"] == 5.0 and m["mae_pct"] == 1.5 and m["good"] == 1, m
    # каскад-метка: 3 строки в 60с -> все true; одиночка через час -> false
    rows = [{"ts_utc": "2026-07-01T01:00:00"}, {"ts_utc": "2026-07-01T01:00:30"},
            {"ts_utc": "2026-07-01T01:01:00"}, {"ts_utc": "2026-07-01T02:30:00"}]
    assert cascade_flags(rows) == [True, True, True, False]
    # quality_lines не падает на пустом/отсутствующем файле
    assert quality_lines("2099-01-01T00:00:00") == []
    print("selfcheck OK")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max", type=int, default=400)
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    raise SystemExit(selfcheck() if a.selfcheck
                     else 0 if resolve_new(a.max, a.dry_run) else 0)

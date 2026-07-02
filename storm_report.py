#!/usr/bin/env python3
"""storm_report.py — отчёты по IGNITE-сигналам storm_radar в Telegram.

Запускается launchd ежедневно (20:00 МСК), внутри ДВА независимых каденса:
  1) ШТОРМ-ОТЧЁТ раз в 2 дня (guard 44ч) — резолвер WIN/LOSS; сигналы без
     развязки переносятся из отчёта в отчёт, пока не возьмут цель (WIN),
     стоп (LOSS) или не истекут за 7 дней (⌛).
  2) СВОДКА ЗА 4 ДНЯ (guard 92ч) — ретроспектива поведения каждого поджига:
     пик за 24ч после сигнала, характер движения, вердикт «можно ли было
     заходить» от реалистичного входа через 1–2 мин после алерта.

Правила резолва — по 1м свечам Bybit от минуты сигнала:
  цель = +5% в сторону пробоя (оценка брата: средняя отработка шторма);
  стоп = clamp(ширина коробки, 1.5%, 3.0%) против (возврат сквозь коробку = фальш);
  цель и стоп в одной свече → LOSS (worst case: порядок внутри бара неизвестен).
Стейт обновляется ТОЛЬКО после успешной доставки в TG — иначе следующий
ежедневный прогон повторит попытку с теми же данными.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from file_lock import atomic_json_read, atomic_json_update

BYBIT = "https://api.bybit.com/v5/market/kline"
OUT = Path(__file__).parent / "outcomes"
STAGES_PATH = OUT / "storm_stages.csv"
HITS_PATH = OUT / "radar_hits.csv"
STATE_PATH = OUT / "storm_report_state.json"

TARGET_PCT = 5.0       # цель = средняя отработка шторма (доменная оценка брата,
                       # 2026-07-02; пересмотреть по forward-статистике при n>=20)
MIN_STOP_PCT = 1.5     # пол стопа: ниже — шум и комиссии
MAX_STOP_PCT = 3.0     # потолок стопа: шире 3% против — пробой давно фальшивый
EXPIRE_H = 168.0       # 7 дней без развязки → снимаем с учёта (⌛)
REPORT_EVERY_H = 44.0  # каденс ~2 суток; 44ч — запас на дрейф минуты запуска

SUMMARY_EVERY_H = 92.0    # сводка ~раз в 4 суток (92ч — тот же запас на дрейф)
SUMMARY_LOOKBACK_D = 4    # окно отбора поджигов в сводку
PEAK_WINDOW_H = 24.0      # замер пика/поведения: 24ч после сигнала
ENTRY_DELAY_NOTE = "вход = close 1-го полного 1м бара после алерта (~1–2 мин)"
ENTER_OK_PCT = 2.0        # «можно было заходить»: ≥2% хода от точки входа
ENTER_PAIN_PCT = 1.0      # ...при просадке до пика не глубже 1% — иначе «с нервами»
MSK = ZoneInfo("Europe/Moscow")


# ============================== ДАННЫЕ ==============================

def fetch_1m(symbol: str, start_ms: int, end_ms: int) -> list[tuple]:
    """1м свечи (ts, o, h, l, c) по возрастанию; пагинация лимита Bybit 1000."""
    out: list[tuple] = []
    cursor = start_ms
    while cursor < end_ms:
        url = (f"{BYBIT}?category=linear&symbol={symbol}&interval=1"
               f"&start={cursor}&end={end_ms}&limit=1000")
        with urllib.request.urlopen(url, timeout=15) as r:
            rows = json.load(r).get("result", {}).get("list") or []
        if not rows:
            break
        rows.reverse()  # Bybit отдаёт новые первыми
        batch = [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]))
                 for x in rows]
        if out:  # страховка от дублей/зацикливания на стыке страниц
            batch = [b for b in batch if b[0] > out[-1][0]]
            if not batch:
                break
        out.extend(batch)
        if len(rows) < 1000:
            break
        cursor = out[-1][0] + 60_000
        time.sleep(0.15)
    return out


def load_ignites(after_iso: str) -> list[dict]:
    """Новые ignite-строки из storm_stages.csv строго после отметки after_iso."""
    sigs = []
    if not STAGES_PATH.exists():
        return sigs
    with STAGES_PATH.open() as f:
        for row in csv.DictReader(f):
            if row["stage"] != "ignite" or row["ts_utc"] <= after_iso:
                continue
            try:
                d = json.loads(row["details"])
                entry = float(row["price"])
            except (json.JSONDecodeError, ValueError):
                continue
            bw = None
            if d.get("box_high") and d.get("box_low") and entry > 0:
                bw = (float(d["box_high"]) - float(d["box_low"])) / entry * 100
            stop = min(max(bw or MIN_STOP_PCT, MIN_STOP_PCT), MAX_STOP_PCT)
            sigs.append({"ts_utc": row["ts_utc"], "symbol": row["symbol"],
                         "side": d.get("side") or "up", "price": entry,
                         "target_pct": TARGET_PCT, "stop_pct": round(stop, 2)})
    return sigs


def count_rows(path: Path, after_iso: str, stage: str | None = None) -> int:
    if not path.exists():
        return 0
    with path.open() as f:
        return sum(1 for r in csv.DictReader(f)
                   if r["ts_utc"] > after_iso and (stage is None or r.get("stage") == stage))


# ============================== РЕЗОЛВ ==============================

def _ts(sig: dict) -> datetime:
    # ts_utc в CSV naive UTC — навешиваем зону явно (грабля aware/naive)
    return datetime.fromisoformat(sig["ts_utc"]).replace(tzinfo=timezone.utc)


def resolve(sig: dict, kl: list[tuple], now: datetime) -> dict:
    """Статус сигнала + метрики. Все проценты — В СТОРОНУ сигнала (плюс = прав)."""
    entry, target, stop = sig["price"], sig["target_pct"], sig["stop_pct"]
    sgn = 1 if sig["side"] == "up" else -1
    status, hit_min, mfe, mae = "pending", None, 0.0, 0.0
    for i, (_, _o, h, l, _c) in enumerate(kl):
        if sgn > 0:
            fav, adv = (h / entry - 1) * 100, (l / entry - 1) * 100
        else:
            fav, adv = -(l / entry - 1) * 100, -(h / entry - 1) * 100
        mfe, mae = max(mfe, fav), min(mae, adv)
        if adv <= -stop:          # стоп проверяем первым: тай в одной свече → LOSS
            status, hit_min = "loss", i
            break
        if fav >= target:
            status, hit_min = "win", i
            break
    if status == "pending" and (now - _ts(sig)).total_seconds() > EXPIRE_H * 3600:
        status = "expired"
    last_i = hit_min if hit_min is not None else len(kl) - 1
    cur = sgn * (kl[last_i][4] / entry - 1) * 100 if kl else 0.0
    return {"status": status, "hit_min": hit_min, "mfe": mfe, "mae": mae, "cur_pct": cur}


def r_mult(sig: dict, r: dict) -> float | None:
    """R-multiple развязки: риск = 1 стоп. WIN = цель/стоп, LOSS = −1,
    EXPIRED = фактический итог в стопах. Pending → None (не развязан)."""
    if r["status"] == "win":
        return sig["target_pct"] / sig["stop_pct"]
    if r["status"] == "loss":
        return -1.0
    if r["status"] == "expired":
        return r["cur_pct"] / sig["stop_pct"]
    return None


def btc_pct(btc: list[tuple], start_ms: int, end_ms: int) -> float | None:
    """Изменение BTC за окно [start_ms, end_ms] по закрытиям 1м свечей."""
    if not btc:
        return None
    ts_list = [b[0] for b in btc]
    i0 = bisect.bisect_left(ts_list, start_ms)
    i1 = min(bisect.bisect_right(ts_list, end_ms) - 1, len(btc) - 1)
    if i0 >= len(btc) or i1 <= i0:
        return None
    return (btc[i1][4] / btc[i0][4] - 1) * 100


# ============================== СВОДКА ЗА 4 ДНЯ ==============================

def peak_stats(sig: dict, kl: list[tuple]) -> dict | None:
    """Метрики окна после сигнала: пик, просадка, финал, тайминги, вход.
    Все проценты — в сторону сигнала. None, если свечей нет."""
    if not kl:
        return None
    entry = sig["price"]
    sgn = 1 if sig["side"] == "up" else -1
    mfe = mae = 0.0
    t_mfe = t_mae = 0
    peak_price = entry
    for i, (_, _o, h, l, _c) in enumerate(kl):
        fav = (h / entry - 1) * 100 if sgn > 0 else -(l / entry - 1) * 100
        adv = (l / entry - 1) * 100 if sgn > 0 else -(h / entry - 1) * 100
        if fav > mfe:
            mfe, t_mfe, peak_price = fav, i, (h if sgn > 0 else l)
        if adv < mae:
            mae, t_mae = adv, i
    final = sgn * (kl[-1][4] / entry - 1) * 100
    # реалистичный вход: close первого ПОЛНОГО 1м бара после минуты сигнала
    sig_minute = int(_ts(sig).timestamp() * 1000) // 60_000 * 60_000
    after = [b for b in kl if b[0] > sig_minute]
    entry_px = e_mfe = e_pain = None
    if len(after) >= 2:
        entry_px = after[0][4]
        favs, advs = [], []
        for b in after[1:]:  # fav лонга по хаю / шорта по лоу; adv зеркально
            favs.append((b[2] / entry_px - 1) * 100 if sgn > 0
                        else -(b[3] / entry_px - 1) * 100)
            advs.append((b[3] / entry_px - 1) * 100 if sgn > 0
                        else -(b[2] / entry_px - 1) * 100)
        e_mfe = max(favs)
        peak_i = favs.index(e_mfe)
        e_pain = min(advs[:peak_i + 1])  # худшая просадка ДО пика (пересидеть)
    return {"mfe": mfe, "mae": mae, "t_mfe": t_mfe, "t_mae": t_mae,
            "final": final, "peak_price": peak_price,
            "entry_px": entry_px, "e_mfe": e_mfe, "e_pain": e_pain,
            "minutes": len(kl)}


def behavior_label(p: dict) -> str:
    """Детерминированный ярлык поведения из цифр (цифры показаны рядом)."""
    if p["mfe"] < 1.0 and p["mae"] > -1.0:
        return "пила во флэте"
    if p["mfe"] < 1.0:
        return "против сигнала"
    if p["mae"] <= -1.0 and p["t_mae"] < p["t_mfe"]:
        return f"сначала тряхнуло {p['mae']:+.1f}%, потом ход"
    if p["t_mfe"] <= 15:
        return "импульс сразу"
    if p["final"] < p["mfe"] / 2:
        return "ход с откатом (пик не удержан)"
    return "плавный ход с удержанием"


def entry_verdict(p: dict) -> str:
    """Можно ли было зайти по алерту (вход через 1–2 мин, см. ENTRY_DELAY_NOTE).
    Сравнение по округлённым до 0.1% цифрам — тем же, что показаны в тексте."""
    if p["e_mfe"] is None:
        return "❔ мало данных для оценки входа"
    e_mfe, e_pain = round(p["e_mfe"], 1), round(p["e_pain"], 1)
    if e_mfe >= ENTER_OK_PCT and e_pain > -ENTER_PAIN_PCT:
        return f"✅ можно было: +{e_mfe:.1f}% от входа"
    if e_mfe >= ENTER_OK_PCT:
        return f"⚠️ можно, но через просадку {e_pain:+.1f}%: +{e_mfe:.1f}% от входа"
    if round(p["mfe"], 1) >= ENTER_OK_PCT:
        return f"❌ улетела до входа: от входа лишь +{e_mfe:.1f}%"
    return f"❌ хода не было: +{e_mfe:.1f}% от входа"


def _fmt_summary_sig(sig: dict, p: dict | None, btc: float | None) -> str:
    arrow = "⬆️" if sig["side"] == "up" else "⬇️"
    t_msk = _ts(sig).astimezone(MSK).strftime("%d.%m %H:%M")
    head = f"<b>{sig['symbol']}</b> {arrow} {t_msk} с {sig['price']:g}"
    if p is None:
        return f"{head}\n      свечи не получены — пропуск"
    win_txt = (f"{PEAK_WINDOW_H:.0f}ч" if p["minutes"] >= PEAK_WINDOW_H * 60 - 2
               else f"{_age(p['minutes'])} (окно неполное)")
    btc_txt = f" · BTC {btc:+.1f}%" if btc is not None else ""
    return "\n".join([
        head,
        f"      пик {p['peak_price']:g} = <b>{p['mfe']:+.1f}%</b> за "
        f"{_age(p['t_mfe'])} · просадка {p['mae']:+.1f}% · финал {win_txt}: "
        f"{p['final']:+.1f}%{btc_txt}",
        f"      {behavior_label(p)} · {entry_verdict(p)}",
    ])


def build_summary(items: list[tuple[dict, dict | None, float | None]],
                  since: datetime, now: datetime) -> str:
    period = (f"{since.astimezone(MSK).strftime('%d.%m')} → "
              f"{now.astimezone(MSK).strftime('%d.%m')} МСК")
    lines = [f"📊 <b>СВОДКА ЗА 4 ДНЯ</b> · {period}",
             f"Поджигов: {len(items)} · замер: {PEAK_WINDOW_H:.0f}ч после сигнала", ""]
    if not items:
        lines.append("Поджигов за период не было — радар жив, пружины не стреляли.")
    for sig, p, btc in items:
        lines.append(_fmt_summary_sig(sig, p, btc))
        lines.append("")
    ok = sum(1 for _, p, _b in items
             if p and p["e_mfe"] is not None and round(p["e_mfe"], 1) >= ENTER_OK_PCT)
    peaks = sorted(p["mfe"] for _, p, _b in items if p)
    finals = sorted(p["final"] for _, p, _b in items if p)
    if peaks:
        lines.append(f"Итог: вход имел смысл в {ok}/{len(items)} · медиана пика "
                     f"{peaks[len(peaks) // 2]:+.1f}% · медиана финала "
                     f"{finals[len(finals) // 2]:+.1f}%")
    lines.append(f"<i>{ENTRY_DELAY_NOTE}; «можно было» = ≥{ENTER_OK_PCT:.0f}% хода "
                 f"от входа при просадке до пика ≤{ENTER_PAIN_PCT:.0f}%. "
                 f"BTC — за окно замера.</i>")
    return "\n".join(lines)


# ============================== СООБЩЕНИЕ ==============================

def _age(minutes: float) -> str:
    m = int(minutes)
    if m < 60:
        return f"{m}м"
    if m < 48 * 60:
        return f"{m // 60}ч{m % 60:02d}м" if m % 60 else f"{m // 60}ч"
    return f"{m // 1440}д {(m % 1440) // 60}ч"


def _fmt_sig(sig: dict, r: dict, btc: float | None, now: datetime) -> str:
    icon = {"win": "✅", "loss": "❌", "pending": "⏳", "expired": "⌛"}[r["status"]]
    arrow = "⬆️" if sig["side"] == "up" else "⬇️"
    t_msk = _ts(sig).astimezone(MSK).strftime("%d.%m %H:%M")
    age = (now - _ts(sig)).total_seconds() / 60
    btc_txt = f" · BTC {btc:+.1f}%" if btc is not None else ""
    head = f"{icon} <b>{sig['symbol']}</b> {arrow} {t_msk}"
    if r["status"] == "win":
        body = (f"цель +{sig['target_pct']:.1f}% за {_age(r['hit_min'])} · "
                f"MAE {r['mae']:+.1f}%{btc_txt}")
    elif r["status"] == "loss":
        body = (f"стоп −{sig['stop_pct']:.1f}% за {_age(r['hit_min'])} · "
                f"MFE {r['mfe']:+.1f}%{btc_txt}")
    elif r["status"] == "expired":
        body = f"7 дней без развязки, снят · итог {r['cur_pct']:+.1f}%{btc_txt}"
    else:
        head += f" · ждём {_age(age)}"
        body = (f"сейчас {r['cur_pct']:+.1f}% · MFE {r['mfe']:+.1f}% / "
                f"MAE {r['mae']:+.1f}% · цель +{sig['target_pct']:.1f}% / "
                f"стоп −{sig['stop_pct']:.1f}%{btc_txt}")
    return f"{head}\n      {body}"


def build_message(rows: list[tuple[dict, dict, float | None]], n_fresh: int,
                  n_carried: int, n_watch: int, n_hits: int,
                  since: datetime, now: datetime, totals: dict) -> str:
    period = f"{since.astimezone(MSK).strftime('%d.%m %H:%M')} → {now.astimezone(MSK).strftime('%d.%m %H:%M')} МСК"
    lines = [f"📊 <b>ШТОРМ-ОТЧЁТ</b> · {period}",
             f"Поджигов новых: {n_fresh} · ждали с прошлого отчёта: {n_carried}", ""]
    if not rows:
        lines.append("Поджигов не было — радар жив, пружины не стреляли.")
    order = {"win": 0, "loss": 1, "expired": 2, "pending": 3}
    for sig, r, b in sorted(rows, key=lambda x: (order[x[1]["status"]], x[0]["ts_utc"])):
        lines.append(_fmt_sig(sig, r, b, now))
    n = {"win": 0, "loss": 0, "expired": 0, "pending": 0}
    for _, r, _b in rows:
        n[r["status"]] += 1
    rms = [rm for sig, r, _b in rows if (rm := r_mult(sig, r)) is not None]
    mfes = sorted(r["mfe"] for _, r, _b in rows)
    sum_r_txt = f"{sum(rms):+.1f}R ({len(rms)} развязок)" if rms else "0R (развязок нет)"
    med_txt = (f" · медиана хода (MFE): {mfes[len(mfes) // 2]:+.1f}%" if mfes else "")
    lines += ["",
              f"🏁 За период: {n['win']} WIN · {n['loss']} LOSS · "
              f"{n['expired']}⌛ · {n['pending']} ждут (перенос в следующий отчёт)",
              f"💰 Σ {sum_r_txt}{med_txt}",
              f"📈 С запуска: {totals['win']}W / {totals['loss']}L / {totals['expired']}⌛",
              f"⚡ Взведений (WATCH): {n_watch} · 🔊 vol_radar хитов: {n_hits}",
              "<i>Цель +5% (средняя отработка шторма), стоп = ширина коробки "
              "[1.5–3%] против; 1м Bybit, тай в одной свече = LOSS. "
              "BTC — за окно сигнала. R = один стоп риска: WIN = цель/стоп R "
              "(1.7–3.3R), LOSS = −1R — прибыльность видна и при низком WR.</i>"]
    return "\n".join(lines)


def send_report(text: str) -> bool:
    """Отправка с разбивкой >4000 симв.; True только если ушли ВСЕ части."""
    from telegram_alerts import _send, load_config
    cfg = load_config()
    if not cfg.get("enabled") or not cfg.get("bot_token") or not cfg.get("chat_id"):
        print("[report] TG выключен в конфиге — не отправлено")
        return False
    ok = True
    while text:
        cut = text.rfind("\n", 0, 4000) if len(text) > 4000 else len(text)
        cut = cut if cut > 0 else 4000
        chunk, text = text[:cut], text[cut:].lstrip("\n")
        ok = bool(_send(cfg["bot_token"], str(cfg["chat_id"]), chunk)) and ok
        if text:
            time.sleep(0.4)
    return ok


# ============================== MAIN ==============================

def _merge_state(upd: dict) -> None:
    """Обновляет только свои ключи — отчёт и сводка не затирают друг друга."""
    def m(st):
        st = st or {}
        st.update(upd)
        return st
    atomic_json_update(STATE_PATH, m, default={})


def run_report(a, now: datetime) -> int:
    st = atomic_json_read(STATE_PATH, default={}) or {}
    last = st.get("last_report_utc")
    if last and not a.force:
        if (now - datetime.fromisoformat(last)).total_seconds() < REPORT_EVERY_H * 3600:
            print("[report] каденс: с прошлого отчёта < 44ч, выходим")
            return 0

    scanned = st.get("scanned_until", "")
    fresh = load_ignites(scanned)
    carried = st.get("pending", [])
    signals = carried + fresh
    totals = st.get("totals", {"win": 0, "loss": 0, "expired": 0})
    since_iso = last or (min(s["ts_utc"] for s in signals) if signals else now.isoformat())
    since = datetime.fromisoformat(since_iso)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    now_ms = int(now.timestamp() * 1000)
    btc = []
    if signals:
        first_ms = min(int(_ts(s).timestamp() * 1000) for s in signals)
        try:
            btc = fetch_1m("BTCUSDT", first_ms, now_ms)
        except Exception as e:
            print(f"[report] BTC klines не получены: {e}")

    rows, still_pending = [], []
    for sig in signals:
        start_ms = int(_ts(sig).timestamp() * 1000)
        try:
            kl = fetch_1m(sig["symbol"], start_ms, now_ms)
        except Exception as e:
            print(f"[report] {sig['symbol']}: свечи не получены ({e}), остаётся в списке")
            still_pending.append(sig)
            continue
        r = resolve(sig, kl, now)
        end_ms = (start_ms + r["hit_min"] * 60_000) if r["hit_min"] is not None else now_ms
        rows.append((sig, r, btc_pct(btc, start_ms, end_ms)))
        if r["status"] == "pending":
            still_pending.append(sig)
        else:
            totals[r["status"]] = totals.get(r["status"], 0) + 1

    # naive-UTC срез для сравнения с ts_utc в CSV
    since_naive = since.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    msg = build_message(rows, len(fresh), len(carried),
                        count_rows(STAGES_PATH, since_naive, "watch_add"),
                        count_rows(HITS_PATH, since_naive), since, now, totals)
    if a.dry_run:
        print(msg)
        return 0
    if not send_report(msg):
        print("[report] отправка не удалась — стейт НЕ обновлён, ретрай завтра")
        return 1
    new_scanned = max([scanned] + [s["ts_utc"] for s in fresh]) if fresh else scanned
    _merge_state({
        "last_report_utc": now.isoformat(),
        "scanned_until": new_scanned,
        "pending": still_pending,
        "totals": totals,
    })
    print(f"[report] отправлен: {len(rows)} сигналов, ждут {len(still_pending)}")
    return 0


def run_summary(a, now: datetime) -> int:
    st = atomic_json_read(STATE_PATH, default={}) or {}
    last = st.get("last_summary_utc")
    if last and not a.force:
        if (now - datetime.fromisoformat(last)).total_seconds() < SUMMARY_EVERY_H * 3600:
            print("[summary] каденс: с прошлой сводки < 92ч, выходим")
            return 0

    since = now - timedelta(days=SUMMARY_LOOKBACK_D)
    since_naive = since.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    sigs = load_ignites(since_naive)
    now_ms = int(now.timestamp() * 1000)
    btc = []
    if sigs:
        first_ms = min(int(_ts(s).timestamp() * 1000) for s in sigs)
        try:
            btc = fetch_1m("BTCUSDT", first_ms, now_ms)
        except Exception as e:
            print(f"[summary] BTC klines не получены: {e}")

    items = []
    for sig in sigs:
        start_ms = int(_ts(sig).timestamp() * 1000)
        end_ms = min(start_ms + int(PEAK_WINDOW_H * 3600 * 1000), now_ms)
        try:
            kl = fetch_1m(sig["symbol"], start_ms, end_ms)
        except Exception as e:
            print(f"[summary] {sig['symbol']}: свечи не получены ({e})")
            kl = []
        items.append((sig, peak_stats(sig, kl), btc_pct(btc, start_ms, end_ms)))

    msg = build_summary(items, since, now)
    if a.dry_run:
        print(msg)
        return 0
    if not send_report(msg):
        print("[summary] отправка не удалась — стейт НЕ обновлён, ретрай завтра")
        return 1
    _merge_state({"last_summary_utc": now.isoformat()})
    print(f"[summary] отправлена: {len(items)} поджигов")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="игнорировать каденсы")
    ap.add_argument("--dry-run", action="store_true", help="печать без отправки и стейта")
    ap.add_argument("--only", choices=["report", "summary"], help="один из двух отчётов")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        return selfcheck()
    now = datetime.now(timezone.utc)
    rc = 0
    if a.only != "summary":
        rc = max(rc, run_report(a, now))
    if a.only != "report":
        rc = max(rc, run_summary(a, now))
    return rc


# ============================== SELFCHECK ==============================

def selfcheck() -> int:
    """Проверка резолв-логики на синтетических свечах (без сети)."""
    base = {"ts_utc": "2026-07-01T00:00:00", "symbol": "T", "side": "up",
            "price": 100.0, "target_pct": 5.0, "stop_pct": 2.0}
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    mk = lambda o, h, l, c: (0, o, h, l, c)
    r = resolve(base, [mk(100, 104, 99.5, 103), mk(103, 105.5, 102, 105)], now)
    assert r["status"] == "win" and r["hit_min"] == 1, r
    r = resolve(base, [mk(100, 100.5, 97.9, 98)], now)
    assert r["status"] == "loss", r
    r = resolve(base, [mk(100, 105.5, 97.5, 100)], now)          # тай → LOSS
    assert r["status"] == "loss", r
    r = resolve(base, [mk(100, 104.9, 98.1, 100.2)], now)        # ни цель, ни стоп
    assert r["status"] == "pending" and abs(r["cur_pct"] - 0.2) < 1e-9, r
    dn = dict(base, side="down")
    r = resolve(dn, [mk(100, 100.5, 94.9, 95)], now)             # лой −5.1% = цель шорта
    assert r["status"] == "win" and r["mfe"] > 5.0, r
    r = resolve(dn, [mk(100, 102.1, 99, 102)], now)              # хай +2.1% = стоп шорта
    assert r["status"] == "loss", r
    old = dict(base, ts_utc="2026-06-20T00:00:00")
    r = resolve(old, [mk(100, 100.5, 99.5, 100.2)], now)
    assert r["status"] == "expired", r
    # clamp стопа: узкая коробка → пол 1.5, широкая → потолок 3.0
    assert min(max(0.45, MIN_STOP_PCT), MAX_STOP_PCT) == 1.5
    assert min(max(5.69, MIN_STOP_PCT), MAX_STOP_PCT) == 3.0
    # R-multiple: WIN = цель/стоп, LOSS = −1, EXPIRED = итог/стоп, pending = None
    assert r_mult(base, {"status": "win"}) == 2.5
    assert r_mult(base, {"status": "loss"}) == -1.0
    assert abs(r_mult(base, {"status": "expired", "cur_pct": -1.0}) - (-0.5)) < 1e-9
    assert r_mult(base, {"status": "pending"}) is None
    # peak_stats: сигнал 00:00:30 → бар 0 = сигнальная минута, вход = close бара 1
    ms = lambda i: int(datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc).timestamp() * 1000) + i * 60_000
    sig = dict(base, ts_utc="2026-07-01T00:00:30")
    kl = [(ms(0), 100, 101, 99.8, 100.5),   # сигнальная минута
          (ms(1), 100.5, 100.8, 100.2, 100.6),  # вход по 100.6
          (ms(2), 100.6, 100.7, 99.7, 99.9),    # просадка до пика −0.9%
          (ms(3), 99.9, 103.9, 99.8, 103.5)]    # пик 103.9 = +3.9% от сигнала
    p = peak_stats(sig, kl)
    assert abs(p["mfe"] - 3.9) < 1e-9 and p["t_mfe"] == 3 and p["peak_price"] == 103.9, p
    assert p["entry_px"] == 100.6, p
    assert abs(p["e_mfe"] - (103.9 / 100.6 - 1) * 100) < 1e-9, p          # +3.28%
    assert abs(p["e_pain"] - (99.7 / 100.6 - 1) * 100) < 1e-9, p          # −0.89%
    assert entry_verdict(p).startswith("✅"), entry_verdict(p)
    assert behavior_label(p) == "импульс сразу", behavior_label(p)
    # шорт: пик по лоу, вердикт «улетела до входа» если ход был только до входа
    dn_sig = dict(base, side="down", ts_utc="2026-07-01T00:00:30")
    kl_dn = [(ms(0), 100, 100.5, 96.9, 97),     # −3.1% в сигнальную минуту
             (ms(1), 97, 97.5, 96.95, 97.4),
             (ms(2), 97.4, 98, 97.2, 97.9)]
    pd_ = peak_stats(dn_sig, kl_dn)
    assert pd_["mfe"] > 3 and pd_["e_mfe"] < 1, pd_
    assert entry_verdict(pd_).startswith("❌ улетела"), entry_verdict(pd_)
    # поведение: пила и «сначала тряхнуло»
    assert behavior_label({"mfe": 0.5, "mae": -0.4, "t_mfe": 5, "t_mae": 3,
                           "final": 0.1}) == "пила во флэте"
    assert behavior_label({"mfe": 2.5, "mae": -1.4, "t_mfe": 50, "t_mae": 10,
                           "final": 2.0}).startswith("сначала тряхнуло")
    print("selfcheck OK: 16/16")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

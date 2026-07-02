"""
storm_radar — двухступенчатый радар штормов, живёт ПАРАЛЛЕЛЬНО с vol_radar (старый канал
на 30м барах не трогаем — обе части работают одновременно, по решению 2026-07-01).

Проблема, которую решает: vol_radar видит только ЗАКРЫТЫЙ 30м бар + скан раз в 5 мин =
до ~35 мин задержки, алерт приходит посреди движения. Здесь:

  Ступень WATCH  (--scan, launchd раз в ~10 мин):
      дешёвый скан топ-N USDT-перпов → «взведённые» монеты: СЖАТИЕ коробки (пружина)
      + ТОПЛИВО (OI растёт при плоской цене / перекос фандинга) → watchlist + TG-дайджест.
      Это «часы заранее», вероятностно: большинство сжатий рассасывается без шторма.
  Ступень IGNITE (--ignite-loop, резидентный процесс):
      быстрый цикл ~12с ТОЛЬКО по watchlist (≤20 монет): пробой коробки + темп объёма
      на 1м барах (+рывок OI как усилитель) → алерт «ШТОРМ НАЧИНАЕТСЯ» в первую
      минуту импульса, а не на 35-й.

⚠ Опережение/точность НЕ ЗАЯВЛЯТЬ (аудит 2026-07-01): все стадии пишутся в
outcomes/storm_stages.csv, lead-time посчитает ТОЛЬКО forward-резолвер (отдельный шаг).
Направление машина НЕ выбирает: сторона пробоя в алерте — свершившийся факт, не прогноз.
Решение о входе — за человеком.

Запуск:  python storm_radar.py --selfcheck            # проверка чистых ядер, без сети
         python storm_radar.py --scan --dry-run       # WATCH-скан, печать без TG
         python storm_radar.py --scan                 # боевой WATCH-скан
         python storm_radar.py --ignite-loop --dry-run
         python storm_radar.py --status               # кто сейчас взведён
"""
from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from file_lock import atomic_json_read, atomic_json_update, file_lock
from radar import fmt_price, mexc_tv_link, radar_buttons
from vol_radar import is_stablecoin, stock_symbols
from volume_profile import fetch_klines, key_levels

BYBIT = "https://api.bybit.com/v5/market"
OUT = Path(__file__).parent / "outcomes"
WATCHLIST_PATH = OUT / "storm_watchlist.json"
OI_HISTORY_PATH = OUT / "storm_oi_history.json"
STAGES_PATH = OUT / "storm_stages.csv"
IGNITE_CD_PATH = OUT / "storm_ignite_cooldown.json"

# --- WATCH (взведение) ---
BOX_BARS = 16             # коробка = 16×30м = 8ч
HIST_BARS = 240           # история перцентиля = 240×30м = 5 дней
COMPRESS_ENTER_P = 20.0   # взводимся: ширина коробки в нижних 20% своей истории
COMPRESS_STAY_P = 35.0    # остаёмся пока ≤35% (гистерезис против дребезга на границе)
OI_BUILDUP_PCT = 4.0      # топливо: ΔOI за ~4ч ≥ +4% ...
OI_FLAT_PRICE_PCT = 1.5   # ... при |Δцены за 4ч| ≤ 1.5% (позиции копятся, цена стоит)
FUNDING_SKEW = 0.0003     # топливо: |funding| ≥ 0.03% (~3× типичного 0.01%)
MAX_BOX_WIDTH_PCT = 6.0   # потолок АБСОЛЮТНОЙ ширины коробки: «сжатие» на треш-монете
                          # с коробкой 10% = её обычная пила; пробой такой коробки
                          # приходит после большого хода — опоздание, ради которого всё
                          # и затевалось. ponytail: порог из первого живого скана
                          # (p7 при коробке 7-11% = мусор), тюнить по storm_stages.csv
WATCHLIST_CAP = 20        # максимум монет под быстрым надзором (лимит внимания + API)
KLINE_FAIL_ABORT = 0.30   # >30% klines не пришли → скан прерван, watchlist НЕ трогаем

# --- IGNITE (поджиг) ---
LOOP_SEC = 12.0           # цикл быстрого надзора
BREAK_ATR_MULT = 0.25     # пробой = выход за коробку на 0.25×ATR(14, 30м)
PACE_MULT = 3.0           # объём 3 последних закрытых 1м баров ≥ 3× среднего 3-мин темпа
OI_JUMP_WINDOW_MIN = 6.0  # окно рывка OI (усилитель в сообщении, НЕ условие триггера)
IGNITE_COOLDOWN_H = 2.0   # один шторм-алерт на монету на эпизод
IGNITE_RETRY_SEC = 300.0  # фейл отправки TG → короткий кулдаун (ретрай через ~5 мин),
                          # а не сожжённый эпизод (аудит 2026-07-01)
MEMBER_FAIL_DROP = 3      # член watchlist переживает 2 подряд фейла данных, дроп на 3-м


# ============================== ЧИСТЫЕ ЯДРА (тестируемо) ==============================

def rolling_box_widths(klines: list, box_bars: int = BOX_BARS) -> list[float]:
    """Ширины скользящих коробок (high−low)/mid в %, по ЗАКРЫТЫМ барам.
    klines возр. времени, последний бар НЕЗАКРЫТ и отбрасывается целиком
    (конвенция vol_radar/vol_core, аудит 2026-07-01: partial-bar несравним)."""
    closed = klines[:-1]
    if len(closed) < box_bars + 14:      # мало истории — перцентиль будет мусорным
        return []
    widths = []
    for i in range(box_bars, len(closed) + 1):
        win = closed[i - box_bars:i]
        hi = max(b[2] for b in win)
        lo = min(b[3] for b in win)
        mid = (hi + lo) / 2.0
        if mid <= 0:
            return []
        widths.append((hi - lo) / mid * 100.0)
    return widths


def compression_pctile(widths: list[float]) -> float | None:
    """Перцентиль ПОСЛЕДНЕЙ ширины коробки против всей её истории. 0 = самая узкая.
    Mean-rank (как scipy kind='mean'): устойчив к тай-значениям — строгий '<'
    занижал бы перцентиль, когда половина истории равна текущей ширине."""
    if len(widths) < 30:
        return None
    cur = widths[-1]
    strict = sum(1 for w in widths if w < cur)
    weak = sum(1 for w in widths if w <= cur)
    return (strict + weak) / 2.0 / len(widths) * 100.0


def box_and_atr(klines: list, box_bars: int = BOX_BARS, atr_n: int = 14) -> dict | None:
    """Коробка (high/low последних box_bars ЗАКРЫТЫХ баров) + ATR(atr_n)."""
    closed = klines[:-1]
    if len(closed) < max(box_bars, atr_n + 1):
        return None
    win = closed[-box_bars:]
    hi = max(b[2] for b in win)
    lo = min(b[3] for b in win)
    trs = []
    for i in range(len(closed) - atr_n, len(closed)):
        h, l, pc = closed[i][2], closed[i][3], closed[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    mid = (hi + lo) / 2.0
    if mid <= 0:
        return None
    return {"box_high": hi, "box_low": lo, "atr": mean(trs),
            "box_width_pct": (hi - lo) / mid * 100.0}


def price_chg_pct(klines: list, bars_back: int) -> float | None:
    """Изменение цены за bars_back ЗАКРЫТЫХ баров, % (close-to-close)."""
    closed = klines[:-1]
    if len(closed) < bars_back + 1:
        return None
    a, b = closed[-1 - bars_back][4], closed[-1][4]
    return (b - a) / a * 100.0 if a > 0 else None


def oi_buildup_ok(oi_chg_4h: float | None, price_chg_4h: float | None) -> bool:
    """Топливо: позиции набиваются (+OI), а цена стоит — будущий материал сквиза."""
    if oi_chg_4h is None or price_chg_4h is None:
        return False
    return oi_chg_4h >= OI_BUILDUP_PCT and abs(price_chg_4h) <= OI_FLAT_PRICE_PCT


def oi_change_pct(hist: list, now: float, hours: float = 4.0, tol: float = 0.15) -> float | None:
    """ΔOI% против точки ~hours назад из собственной истории снапшотов
    [[ts, oi], ...]. None, если подходящей точки нет. Допуск ±tol×hours узкий
    (±36 мин при 4ч): аудит 2026-07-01 — при tol=0.35 подпись «/4ч» врала
    (реальное окно гуляло 2.6–5.4ч); при штатном скане раз в 600с точка в
    допуске есть всегда после первых 4ч, иначе честный фолбэк fetch_oi_change_4h."""
    if not hist or len(hist) < 2:
        return None
    target = now - hours * 3600.0
    best = min(hist, key=lambda p: abs(p[0] - target))
    if abs(best[0] - target) > tol * hours * 3600.0 or best[1] <= 0:
        return None
    return (hist[-1][1] - best[1]) / best[1] * 100.0


def arm_decision(member: bool, p: float, box_width_pct: float,
                 price_chg_4h: float | None, oi_chg_4h: float | None,
                 funding: float) -> str | None:
    """'keep' (уже взведён, сжатие держится) / 'add' (взвести) / None (мимо).
    Взводим ТОЛЬКО тихую сжатую пружину с топливом:
      сжатие p≤ENTER + коробка не шире потолка + ЦЕНА СТОИТ (|Δ4ч| мал) +
      (OI копится ИЛИ фандинг перекошен).
    Флэт-гейт глобальный: монета, уже едущая -4% за 4ч, — не пружина, а тренд
    (первый живой скан 2026-07-01 пропустил такую через фандинг-путь)."""
    if member:
        return "keep" if p <= COMPRESS_STAY_P else None
    if p > COMPRESS_ENTER_P or box_width_pct > MAX_BOX_WIDTH_PCT:
        return None
    # «цена стоит» = за 4ч не прошла больше ПОЛОВИНЫ коробки: тренд режем,
    # болтанку внутри коробки (отскок низ→верх) не наказываем — жёсткий 1.5%
    # резал легитимные пружины, у которых цена гуляет в пределах своей коробки
    if price_chg_4h is None or abs(price_chg_4h) > 0.5 * box_width_pct:
        return None
    fuel = oi_buildup_ok(oi_chg_4h, price_chg_4h) or abs(funding) >= FUNDING_SKEW
    return "add" if fuel else None


def detect_box_break(last: float, box_high: float, box_low: float, atr: float,
                     mult: float = BREAK_ATR_MULT) -> str | None:
    """'up' / 'down' / None. Сторона пробоя — ФАКТ (куда вышла цена), не прогноз."""
    if atr <= 0:
        return None
    if last > box_high + mult * atr:
        return "up"
    if last < box_low - mult * atr:
        return "down"
    return None


def volume_pace_ratio(klines_1m: list) -> float | None:
    """Сумма объёмов 3 последних ЗАКРЫТЫХ 1м баров / средний 3-минутный объём
    предыдущих 30 закрытых. Незакрытый последний бар игнорируется целиком."""
    closed = klines_1m[:-1]
    if len(closed) < 33:
        return None
    recent = sum(b[5] for b in closed[-3:])
    base3 = mean([b[5] for b in closed[-33:-3]]) * 3.0
    if base3 <= 0:
        return None
    return recent / base3


def oi_ring_jump(ring: list, now: float, minutes: float = OI_JUMP_WINDOW_MIN) -> float | None:
    """ΔOI% внутри скользящего окна minutes по точкам [(ts, oi), ...] цикла IGNITE."""
    win = [p for p in ring if now - p[0] <= minutes * 60.0]
    if len(win) < 2 or win[0][1] <= 0:
        return None
    return (win[-1][1] - win[0][1]) / win[0][1] * 100.0


# ============================== ДАННЫЕ / СТЕЙТ ==============================

def fetch_tickers() -> dict:
    """ОДИН запрос: цена+OI+фандинг+оборот всех linear USDT-тикеров."""
    with urllib.request.urlopen(f"{BYBIT}/tickers?category=linear", timeout=10) as r:
        lst = json.loads(r.read())["result"]["list"]
    out = {}
    for t in lst:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue

        def f(k: str) -> float:
            try:
                return float(t.get(k) or 0)
            except (TypeError, ValueError):
                return 0.0

        out[sym] = {"last": f("lastPrice"), "oi": f("openInterest"),
                    "funding": f("fundingRate"), "turnover": f("turnover24h")}
    return out


def fetch_oi_change_4h(symbol: str) -> float | None:
    """Холодный старт: ΔOI за ~4ч из истории Bybit (пока свои снапшоты не накопились)."""
    url = f"{BYBIT}/open-interest?category=linear&symbol={symbol}&intervalTime=30min&limit=9"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            lst = json.loads(r.read())["result"]["list"]   # новые → старые
        if len(lst) < 9:
            return None
        cur, old = float(lst[0]["openInterest"]), float(lst[-1]["openInterest"])
        return (cur - old) / old * 100.0 if old > 0 else None
    except Exception:
        return None


def update_oi_history(tickers: dict, universe: list[str], now: float) -> dict:
    """Снапшот OI вселенной в свою историю (окно 24ч) — источник ΔOI для WATCH."""
    def mut(hist):
        hist = hist or {}
        cutoff = now - 24 * 3600.0
        for sym in universe:
            oi = tickers.get(sym, {}).get("oi", 0.0)
            if oi <= 0:
                continue
            arr = [p for p in hist.get(sym, []) if p[0] >= cutoff]
            arr.append([now, oi])
            hist[sym] = arr
        # cutoff применяем ко ВСЕМ ключам: монета, выпавшая из топ-N но живая,
        # не должна держать stale-точки вечно (аудит 2026-07-01); делистнутые — вон
        for sym in list(hist):
            if sym in universe:
                continue
            arr = [p for p in hist[sym] if p[0] >= cutoff]
            if arr and sym in tickers:
                hist[sym] = arr
            else:
                del hist[sym]
        return hist
    return atomic_json_update(OI_HISTORY_PATH, mut, default={})


def log_stage(symbol: str, stage: str, price: float, details: dict, sent: bool):
    """Append-only лог стадий (watch_add / watch_drop / ignite) — сырьё для
    будущего lead-time-резолвера. Лок: пишут два процесса (scan и ignite-loop).
    Телеметрия НЕ роняет алерт-путь: TimeoutError лока/OSError глотаем с печатью
    (аудит 2026-07-01: исключение здесь теряло кулдаун → дубль-алерты)."""
    try:
        STAGES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(STAGES_PATH):
            new = not STAGES_PATH.exists() or STAGES_PATH.stat().st_size == 0
            with STAGES_PATH.open("a", newline="", encoding="utf-8") as fp:
                w = csv.writer(fp)
                if new:
                    w.writerow(["ts_utc", "symbol", "stage", "price", "details", "sent"])
                w.writerow([datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                            symbol, stage, price,
                            json.dumps(details, ensure_ascii=False), int(sent)])
    except Exception as e:
        print(f"[storm] log_stage failed ({stage} {symbol}): {e}")


def send_tg(msg: str, buttons: dict | None = None) -> bool:
    try:
        from telegram_alerts import _send, load_config
        cfg = load_config()
        # тот же гейт, что в send_report: уважаем штатный выключатель enabled
        if not cfg.get("enabled") or not cfg.get("bot_token") or not cfg.get("chat_id"):
            return False
        return bool(_send(cfg["bot_token"], str(cfg["chat_id"]), msg, reply_markup=buttons))
    except Exception as e:
        print(f"[storm] TG send failed: {e}")
        return False


# ============================== СООБЩЕНИЯ ==============================

def build_watch_digest(added: list[str], wl: dict) -> str:
    lines = ["⚡ <b>ВЗВЕДЕНЫ</b> · радар штормов (WATCH)", ""]
    for s in added:
        e = wl[s]
        oi_txt = (f"OI {e['oi_chg_4h']:+.1f}%/4ч" if e.get("oi_chg_4h") is not None
                  else "OI н/д")
        pr_txt = (f"{e['price_chg_4h']:+.1f}%" if e.get("price_chg_4h") is not None
                  else "н/д")
        lines.append(
            f"• <b>{s}</b> — сжатие p{e['compress_p']:.0f}, коробка "
            f"{e['box_width_pct']:.1f}%/8ч, {oi_txt} при цене {pr_txt}/4ч, "
            f"фандинг {e['funding'] * 100:+.3f}%")
        lines.append(f'   <a href="{mexc_tv_link(s)}">график</a>')
    lines += ["", f"Под надзором IGNITE: {len(wl)} монет, цикл ~{int(LOOP_SEC)}с.",
              "<i>Это НЕ сигнал входа: монета взведена, шторм не гарантирован.</i>"]
    return "\n".join(lines)


def build_ignite_message(symbol: str, side: str, entry: dict, pace: float,
                         oi_jump: float | None, oi_since_add: float | None,
                         last: float, vp: dict | None = None) -> str:
    """Алерт поджига. vp можно передать готовым (тест/кэш), иначе key_levels."""
    if vp is None:
        vp = key_levels(symbol)
    arrow = "⬆️ пробой коробки ВВЕРХ" if side == "up" else "⬇️ пробой коробки ВНИЗ"
    armed_h = max(0.0, (time.time() - entry.get("added_ts", time.time())) / 3600.0)
    lines = [f"🌩 <b>ШТОРМ НАЧИНАЕТСЯ · {symbol}</b>", "",
             f"{arrow} (8ч-коробка {entry['box_width_pct']:.1f}%, взведён {armed_h:.1f}ч назад)",
             f"⚡ Темп объёма: <b>{pace:.1f}×</b> нормы (1м бары)"]
    extras = []
    if oi_jump is not None:
        extras.append(f"OI за {OI_JUMP_WINDOW_MIN:.0f} мин: {oi_jump:+.1f}%")
    if oi_since_add is not None:
        extras.append(f"OI с постановки: {oi_since_add:+.1f}%")
    if extras:
        lines.append("📈 " + "  ·  ".join(extras))
    lines += [f"💵 Цена: <code>{fmt_price(last)}</code>"]
    if vp:
        lines += ["",
                  f"📊 <b>Уровни</b> (Volume Profile, ~{vp.get('bars', 168)}ч):",
                  f"   POC  <code>{fmt_price(vp['poc'])}</code>",
                  f"   VAH  <code>{fmt_price(vp['vah'])}</code>",
                  f"   VAL  <code>{fmt_price(vp['val'])}</code>"]
    lines += ["", f'📈 <a href="{mexc_tv_link(symbol)}">График MEXC-перп (TradingView)</a>',
              "", "<i>Сторона пробоя — факт, не прогноз. Направление и вход решаешь ты.</i>"]
    return "\n".join(lines)


# ============================== СТУПЕНЬ WATCH ==============================

def scan(dry_run: bool = False, top: int = 150):
    """Один WATCH-проход: вселенная → сжатие → топливо → watchlist + дайджест.
    Fail-closed: сеть легла посреди скана → watchlist НЕ перезаписываем."""
    now = time.time()
    try:
        tickers = fetch_tickers()
    except Exception as e:
        print(f"[watch] tickers fetch failed, скан прерван: {e}")
        return
    if not tickers:
        print("[watch] пустые тикеры, скан прерван")
        return

    stocks = stock_symbols()
    old_wl = atomic_json_read(WATCHLIST_PATH, default={}) or {}
    uni = sorted((s for s in tickers if not is_stablecoin(s) and s not in stocks),
                 key=lambda s: tickers[s]["turnover"], reverse=True)[:top]
    # члены watchlist не выпадают из скана, даже если вылетели из топ-N по обороту
    uni = list(dict.fromkeys(uni + [s for s in old_wl if s in tickers]))

    # dry-run полностью сухой: shared-стейт (OI-история/watchlist/stages) не трогаем
    hist = (atomic_json_read(OI_HISTORY_PATH, default={}) or {}) if dry_run \
        else update_oi_history(tickers, uni, now)

    new_wl, added, dropped, fails = {}, [], [], 0
    for sym in uni:
        member = sym in old_wl
        prev = old_wl.get(sym) or {}
        kl = fetch_klines(sym, interval="30", limit=HIST_BARS)
        p = compression_pctile(rolling_box_widths(kl)) if kl else None
        ba = box_and_atr(kl) if kl else None
        if not kl or p is None or ba is None:
            # transient-фейл данных (timeout/500 Bybit): члена НЕ роняем — иначе
            # IGNITE слепнет на 10 мин, эпизод рвётся и added_ts сбрасывается
            # (аудит 2026-07-01). Переносим как есть, дроп после N фейлов подряд.
            if not kl:
                fails += 1
            if member:
                fr = prev.get("fails_in_row", 0) + 1
                if fr >= MEMBER_FAIL_DROP:
                    dropped.append((sym, {"reason": "data_fail", "fails_in_row": fr}))
                else:
                    new_wl[sym] = {**prev, "fails_in_row": fr}
            time.sleep(0.03)
            continue
        pr4 = price_chg_pct(kl, 8)                       # 8×30м = 4ч
        oi4 = oi_change_pct(hist.get(sym, []), now)
        if oi4 is None and p <= COMPRESS_ENTER_P and not member:
            oi4 = fetch_oi_change_4h(sym)                # холодный старт, только кандидатам
        verdict = arm_decision(member, p, ba["box_width_pct"], pr4, oi4,
                               tickers[sym]["funding"])
        if verdict is None:
            if member:
                dropped.append((sym, {"reason": "conditions", "compress_p": round(p, 1)}))
            time.sleep(0.03)
            continue
        if verdict == "keep":
            # коробку только СУЖАЕМ (пружина дожимается), никогда не расширяем:
            # иначе медленный выход из коробки размывает её и IGNITE слепнет
            box = ba if ba["box_width_pct"] < prev.get("box_width_pct", 1e9) else {
                k: prev[k] for k in ("box_high", "box_low", "atr", "box_width_pct")}
            entry = {**prev, **box, "compress_p": round(p, 1),
                     "oi_chg_4h": oi4, "price_chg_4h": pr4,
                     "funding": tickers[sym]["funding"]}
            entry.pop("fails_in_row", None)              # успешный проход сбрасывает серию
            new_wl[sym] = entry
        else:                                            # 'add'
            new_wl[sym] = {"added_ts": now, "price_at_add": tickers[sym]["last"],
                           "oi_at_add": tickers[sym]["oi"], **ba,
                           "compress_p": round(p, 1), "oi_chg_4h": oi4,
                           "price_chg_4h": pr4, "funding": tickers[sym]["funding"]}
            added.append(sym)
        time.sleep(0.05)

    if fails > len(uni) * KLINE_FAIL_ABORT:
        # fail-closed целиком: ни watchlist, ни watch_drop/watch_add не пишем
        print(f"[watch] {fails}/{len(uni)} klines не пришли — скан прерван, "
              f"watchlist не тронут (fail-closed)")
        return

    if len(new_wl) > WATCHLIST_CAP:                      # самые узкие пружины важнее
        keep = sorted(new_wl, key=lambda s: new_wl[s]["compress_p"])[:WATCHLIST_CAP]
        for s in set(new_wl) - set(keep):                # эвикция тоже видна резолверу
            if s in old_wl:
                dropped.append((s, {"reason": "cap",
                                    "compress_p": new_wl[s]["compress_p"]}))
        added = [s for s in added if s in keep]
        new_wl = {s: new_wl[s] for s in keep}

    # порядок: сперва ОТПРАВКА, потом лог с реальным результатом — sent в CSV
    # это факт доставки, не флаг режима (класс «детекты ≠ доставленное»,
    # аудит 2026-06-17; конвенция та же, что в ignite-ветке)
    delivered = False
    if added:
        msg = build_watch_digest(added, new_wl)
        if dry_run:
            import re
            print("\n=== WATCH-ДАЙДЖЕСТ (dry-run, в TG не уйдёт) ===\n")
            print(re.sub(r"<[^>]+>", "", msg))
        else:
            delivered = send_tg(msg)

    if not dry_run:
        atomic_json_update(WATCHLIST_PATH, lambda _: new_wl, default={})
        for sym, det in dropped:
            log_stage(sym, "watch_drop", tickers.get(sym, {}).get("last", 0.0),
                      det, sent=False)
        for sym in added:
            e = new_wl[sym]
            log_stage(sym, "watch_add", e["price_at_add"],
                      {"compress_p": e["compress_p"],
                       "box_width_pct": round(e["box_width_pct"], 2),
                       "oi_chg_4h": e["oi_chg_4h"], "price_chg_4h": e["price_chg_4h"],
                       "funding": e["funding"]}, sent=delivered)
    print(f"[watch] скан: {len(uni)} монет, взведено {len(new_wl)} (+{len(added)} новых, "
          f"-{len(dropped)} дропов), klines-фейлов {fails}"
          + (" [dry-run: стейт не записан]" if dry_run else ""))


# ============================== СТУПЕНЬ IGNITE ==============================

def ignite_loop(dry_run: bool = False):
    """Резидентный быстрый надзор за watchlist. 1 запрос tickers на цикл;
    1м klines — только для кандидатов пробоя. Ошибки цикла не роняют процесс."""
    print(f"[ignite] старт: цикл {LOOP_SEC}с, пробой {BREAK_ATR_MULT}×ATR, "
          f"темп ≥{PACE_MULT}×{' (dry-run)' if dry_run else ''}")
    oi_ring: dict[str, list] = {}
    wl_mtime = -1.0
    wl: dict = {}
    mem_cd: dict = {}      # dry-run: кулдаун живёт в памяти, диск не трогаем
    while True:
        try:
            m = WATCHLIST_PATH.stat().st_mtime if WATCHLIST_PATH.exists() else 0.0
            if m != wl_mtime:
                wl = atomic_json_read(WATCHLIST_PATH, default={}) or {}
                wl_mtime = m
                oi_ring = {s: r for s, r in oi_ring.items() if s in wl}
                print(f"[ignite] watchlist обновлён: {len(wl)} монет: "
                      f"{', '.join(sorted(wl)) or '—'}")
            if not wl:
                time.sleep(60)
                continue

            now = time.time()
            cd = dict(mem_cd) if dry_run else (atomic_json_read(IGNITE_CD_PATH, default={}) or {})
            cd = {k: v for k, v in cd.items() if now - v < IGNITE_COOLDOWN_H * 3600}
            tickers = fetch_tickers()

            for sym, e in wl.items():
                t = tickers.get(sym)
                if not t or t["last"] <= 0:
                    continue
                ring = oi_ring.setdefault(sym, [])
                if t["oi"] > 0:
                    ring.append((now, t["oi"]))
                    del ring[:-60]                        # ~12 мин при 12с цикле
                if sym in cd:
                    continue
                side = detect_box_break(t["last"], e["box_high"], e["box_low"], e["atr"])
                if not side:
                    continue
                kl1 = fetch_klines(sym, interval="1", limit=40)
                pace = volume_pace_ratio(kl1)
                if pace is None or pace < PACE_MULT:
                    continue                              # пробой без объёма = не поджиг

                oi_jump = oi_ring_jump(ring, now)
                oi_add = e.get("oi_at_add") or 0
                oi_since_add = ((t["oi"] - oi_add) / oi_add * 100.0) if oi_add > 0 else None
                msg = build_ignite_message(sym, side, e, pace, oi_jump,
                                           oi_since_add, t["last"])
                sent = False
                if dry_run:
                    import re
                    print(f"\n=== IGNITE (dry-run, в TG не уйдёт) ===\n")
                    print(re.sub(r"<[^>]+>", "", msg))
                else:
                    sent = send_tg(msg, radar_buttons(sym))
                # кулдаун: полный при доставке; фейл TG → короткий (ретрай ~5 мин),
                # эпизод не сжигается молча (аудит 2026-07-01)
                cd[sym] = now if (sent or dry_run) \
                    else now - IGNITE_COOLDOWN_H * 3600 + IGNITE_RETRY_SEC
                # персист ТОЧЕЧНО и СРАЗУ, до log_stage: исключение/kill в окне
                # больше не теряет кулдаун → нет дублей каждые 12с (HIGH аудита)
                if not dry_run:
                    atomic_json_update(
                        IGNITE_CD_PATH,
                        lambda d, s=sym, v=cd[sym]: {**(d or {}), s: v}, default={})
                    log_stage(sym, "ignite", t["last"],
                              {"side": side, "pace": round(pace, 2),
                               "oi_jump": oi_jump, "oi_since_add": oi_since_add,
                               "box_high": e["box_high"], "box_low": e["box_low"]}, sent)

            if dry_run:
                mem_cd = cd
            else:                                         # финальная чистка протухших
                atomic_json_update(IGNITE_CD_PATH, lambda _: cd, default={})
            time.sleep(LOOP_SEC)
        except KeyboardInterrupt:
            print("[ignite] остановлен")
            return
        except Exception as ex:
            print(f"[ignite] ошибка цикла (живём дальше): {ex}")
            time.sleep(LOOP_SEC)


# ============================== СТАТУС / SELF-CHECK ==============================

def status():
    wl = atomic_json_read(WATCHLIST_PATH, default={}) or {}
    cd = atomic_json_read(IGNITE_CD_PATH, default={}) or {}
    now = time.time()
    if not wl:
        print("watchlist пуст — никто не взведён")
        return
    print(f"Взведены ({len(wl)}):")
    for s, e in sorted(wl.items(), key=lambda kv: kv[1]["compress_p"]):
        age_h = (now - e["added_ts"]) / 3600.0
        oi_txt = f"{e['oi_chg_4h']:+.1f}%" if e.get("oi_chg_4h") is not None else "н/д"
        cd_txt = "  [cooldown]" if s in cd and now - cd[s] < IGNITE_COOLDOWN_H * 3600 else ""
        print(f"  {s:<16} p{e['compress_p']:<5} коробка {e['box_width_pct']:.1f}%  "
              f"OI/4ч {oi_txt:<7} фандинг {e['funding'] * 100:+.3f}%  "
              f"взведён {age_h:.1f}ч{cd_txt}")


def selfcheck():
    # --- сжатие: широкая история, узкий хвост → низкий перцентиль ---
    # ширина широких баров слегка растёт (реалистично + без вырожденных таев)
    wide = [[i, 100, 108 + i * 0.1, 92, 100 + (i % 3 - 1) * 4, 50] for i in range(84)]
    tight = [[84 + i, 100, 100.6, 99.4, 100 + (i % 3 - 1) * 0.2, 50] for i in range(30)]
    kl = wide + tight + [[999, -1, -1, -1, -1, -1]]          # хвост — незакрытый мусор
    w = rolling_box_widths(kl)
    p = compression_pctile(w)
    assert p is not None and p <= 15, f"узкий хвост должен дать низкий перцентиль, p={p}"
    # обратный случай: расширение в хвосте → высокий перцентиль
    w_rev = rolling_box_widths(tight + wide + [[999, -1, -1, -1, -1, -1]])
    p_rev = compression_pctile(w_rev)
    assert p_rev is not None and p_rev >= 60, f"широкий хвост = высокий перцентиль, p={p_rev}"
    # мало данных → None/[]
    assert rolling_box_widths(kl[:20]) == []
    assert compression_pctile([1.0] * 10) is None

    # --- коробка/ATR: незакрытый бар с дикими значениями не влияет ---
    ba = box_and_atr(kl)
    assert ba and abs(ba["box_high"] - 100.6) < 1e-9 and abs(ba["box_low"] - 99.4) < 1e-9
    assert ba["atr"] > 0 and ba["box_width_pct"] < 1.5

    # --- пробой коробки: порог = край ± 0.25×ATR (100.85 / 99.15 при ATR=1) ---
    assert detect_box_break(100.0, 100.6, 99.4, 1.0) is None          # внутри коробки
    assert detect_box_break(100.7, 100.6, 99.4, 1.0) is None          # в буфере, не пробой
    assert detect_box_break(100.86, 100.6, 99.4, 1.0) == "up"
    assert detect_box_break(99.1, 100.6, 99.4, 1.0) == "down"
    assert detect_box_break(101.0, 100.6, 99.4, 0.0) is None          # ATR=0 → отказ

    # --- темп объёма: ровно → ~1×, всплеск 5× → триггер, незакрытый бар игнор ---
    flat1m = [[i, 100, 100, 100, 100, 10] for i in range(40)]
    r = volume_pace_ratio(flat1m)
    assert r is not None and abs(r - 1.0) < 1e-9, r
    spike1m = flat1m[:-4] + [[36, 0, 0, 0, 0, 50], [37, 0, 0, 0, 0, 50],
                             [38, 0, 0, 0, 0, 50], [39, 0, 0, 0, 0, 999999]]
    r2 = volume_pace_ratio(spike1m)
    assert r2 is not None and r2 >= PACE_MULT, f"3 закрытых по 50 при норме 10 → 5×, r={r2}"
    assert volume_pace_ratio(flat1m[:10]) is None

    # --- топливо OI ---
    assert oi_buildup_ok(5.0, 0.5) is True
    assert oi_buildup_ok(5.0, 3.0) is False       # цена уже уехала — не «тихий» набор
    assert oi_buildup_ok(2.0, 0.5) is False
    assert oi_buildup_ok(None, 0.5) is False

    # --- решение о взведении ---
    assert arm_decision(False, 10.0, 2.0, 0.3, 5.0, 0.0) == "add"        # OI-топливо
    assert arm_decision(False, 10.0, 2.0, 0.3, None, 0.0005) == "add"    # фандинг-топливо
    assert arm_decision(False, 10.0, 2.0, 0.3, 1.0, 0.0) is None         # нет топлива
    assert arm_decision(False, 25.0, 2.0, 0.3, 5.0, 0.0) is None         # не сжат
    assert arm_decision(False, 7.0, 7.1, -4.5, -0.0, -0.00036) is None   # живой REUSDT:
    #   падает -4.5%/4ч + коробка шире потолка — не пружина, а тренд (скан 2026-07-01)
    assert arm_decision(False, 10.0, 2.0, -3.0, 5.0, 0.0005) is None     # тренд: |Δ| > коробки/2
    assert arm_decision(False, 10.0, 4.0, 1.8, None, 0.0005) == "add"    # болтанка ВНУТРИ
    #   коробки 4% (|1.8| ≤ 2.0) — легитимная пружина, жёсткий гейт 1.5% её резал
    assert arm_decision(False, 10.0, 2.0, None, 5.0, 0.0005) is None     # Δцены неизвестна
    assert arm_decision(True, 30.0, 9.9, -9.0, None, 0.0) == "keep"      # член: гистерезис
    assert arm_decision(True, 40.0, 2.0, 0.0, 9.0, 0.001) is None        # член: сжатие ушло

    # --- ΔOI из истории снапшотов ---
    now = 1_000_000.0
    hist = [[now - 4 * 3600, 100.0], [now - 2 * 3600, 104.0], [now, 110.0]]
    d = oi_change_pct(hist, now)
    assert d is not None and abs(d - 10.0) < 1e-9, d
    assert oi_change_pct([[now, 100.0]], now) is None                 # одна точка
    assert oi_change_pct([[now - 600, 100.0], [now, 101.0]], now) is None  # истории < 4ч

    # --- рывок OI в кольце IGNITE ---
    ring = [(now - 300, 100.0), (now - 60, 101.0), (now, 102.0)]
    j = oi_ring_jump(ring, now)
    assert j is not None and abs(j - 2.0) < 1e-9, j
    assert oi_ring_jump([(now, 100.0)], now) is None
    assert oi_ring_jump([(now - 3600, 100.0), (now, 150.0)], now) is None  # вне окна 6 мин

    # --- сообщения: обязательные части, без сетевых вызовов (vp передан) ---
    entry = {"added_ts": now - 3 * 3600, "box_high": 100.6, "box_low": 99.4,
             "box_width_pct": 1.2, "atr": 0.8, "compress_p": 7.0, "oi_chg_4h": 6.2,
             "price_chg_4h": 0.3, "funding": 0.00041, "price_at_add": 100.0,
             "oi_at_add": 1000.0}
    fake_vp = {"poc": 100.5, "vah": 102.0, "val": 99.0, "last": 101.2,
               "zone": "", "bars": 168}
    msg = build_ignite_message("TESTUSDT", "up", entry, 4.2, 1.8, 12.0, 101.3, vp=fake_vp)
    assert "ШТОРМ НАЧИНАЕТСЯ · TESTUSDT" in msg and "ВВЕРХ" in msg
    assert "4.2×" in msg and "POC" in msg and "решаешь ты" in msg
    dig = build_watch_digest(["TESTUSDT"], {"TESTUSDT": entry})
    assert "ВЗВЕДЕНЫ" in dig and "TESTUSDT" in dig and "p7" in dig
    assert "НЕ сигнал входа" in dig

    print("✓ self-check passed: сжатие/перцентиль, коробка+ATR (незакрытый бар "
          "игнорируется), пробой, темп объёма, топливо OI, рывок OI, сообщения")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Радар штормов: WATCH (взведение) + IGNITE (поджиг)")
    ap.add_argument("--scan", action="store_true", help="один WATCH-проход")
    ap.add_argument("--ignite-loop", action="store_true", help="резидентный быстрый надзор")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--top", type=int, default=150)
    a = ap.parse_args()
    if a.selfcheck:
        selfcheck()
    elif a.scan:
        scan(dry_run=a.dry_run, top=a.top)
    elif a.ignite_loop:
        ignite_loop(dry_run=a.dry_run)
    elif a.status:
        status()
    else:
        ap.print_help()

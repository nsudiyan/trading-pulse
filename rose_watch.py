"""
rose_watch — сторож сигналов MM-каналов (Rose) поверх storm-механики.

Идея (брат, 2026-07-03): MM с большой аудиторией постит монету → толпа заносит
объём → монета может зашторметь. Его сигналы неточные, поэтому машина НЕ верит
посту: пост = только ТРИГГЕР немедленного подкопа той самой монеты теми же
storm-метриками (коробка/ATR, сжатие, OI, темп объёма на 1м).

Поток:
  poll t.me/s/<канал> (~60с, keyless — каналы публичные, Telethon не нужен)
    → новый пост с #TICKER → снапшот метрик → outcomes/rose_signals.csv (TG молчит)
    → монета под надзором ROSE_WATCH_H часов (сигнал может отстрелить и через 2ч —
      брат 03.07): пробой коробки каждый цикл ~15с, коробка ПЕРЕСЧИТЫВАЕТСЯ раз в
      BOX_TTL_SEC (замороженная коробка поста на длинном окне протухает: медленный
      дрейф из неё = ложный пробой), пробой + темп ≥ PACE_MULT
      → ОДИН алерт «🌹 ROSE×ШТОРМ» в TG (кулдаун как у IGNITE)
  outcome-хвосты (+30м/+2ч/+24ч от цены поста) → outcomes/rose_outcomes.csv.

⚠ Эдж НЕ ЗАЯВЛЯТЬ (культура аудитов 2026-06/07): вывод «сигналы Rose + объёмное
подтверждение работают» имеет право сделать только форвард-статистика по этим
двум CSV, не эвристика. До неё это ИЗМЕРИТЕЛЬ, не торговый сигнал.
⚠ Отдельный демон осознанно: инъекция в storm_watchlist.json не переживает
WATCH-скан (arm_decision выкинет монету без сжатия), боевой радар не трогаем.

Запуск:  python3 rose_watch.py --selfcheck        # чистые ядра, без сети
         python3 rose_watch.py --once             # один опрос каналов, печать, без стейта/TG
         python3 rose_watch.py --analyze SOLUSDT  # ручной подкоп одной монеты
         python3 rose_watch.py --loop             # резидентный (launchd)
"""
from __future__ import annotations

import argparse
import csv
import html as html_mod
import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from file_lock import atomic_json_read, atomic_json_update, file_lock
from radar import fmt_price, mexc_tv_link, radar_buttons
from storm_radar import (PACE_MULT, IGNITE_COOLDOWN_H, IGNITE_RETRY_SEC,
                         box_and_atr, compression_pctile, detect_box_break,
                         fetch_oi_change_4h, fetch_tickers, rolling_box_widths,
                         send_tg, volume_pace_ratio)
from volume_profile import fetch_klines

# Каналы MM — брат докидывает сюда же (формат: имя из t.me/s/<имя>)
ROSE_CHANNELS = ["RoseSignalsPremium", "rose"]

OUT = Path(__file__).parent / "outcomes"
STATE_PATH = OUT / "rose_state.json"
SIGNALS_PATH = OUT / "rose_signals.csv"
OUTCOMES_PATH = OUT / "rose_outcomes.csv"

POLL_TG_SEC = 60          # опрос t.me/s/ (чаще нет смысла: превью кэшируется)
LOOP_IDLE_SEC = 45        # цикл без активных монет
LOOP_ACTIVE_SEC = 15      # цикл под надзором (памп играет минутами — диагноз 2026-05-30)
ROSE_WATCH_H = 6.0        # окно надзора от поста (брат 03.07: «могут отстрелить и через
                          # 2 часа»); дальше 6ч атрибуция шторма посту сомнительна —
                          # базовая частота штормов на альтах припишет Rose чужое;
                          # тюнить по rose_outcomes: хвост +24ч покажет поздние отстрелы
BOX_TTL_SEC = 300.0       # коробка живёт ≤5 мин: на длинном окне коробка ПОСТА протухает
                          # (медленный дрейф из неё = ложный пробой), поэтому пересчёт
                          # по свежим 30м барам; пробой при этом ловится каждый цикл
BOX_FAIL_DROP = 3         # 3 подряд фейла пересчёта → дроп из надзора (конвенция storm)
LATE_MIN = 45.0           # пост старше — note="late" в CSV (проспали старт), но под
                          # надзор ставим: окно 6ч, коробка всё равно свежая
PENDING_MAX_H = 26.0      # 24ч-хвост + запас; делист монеты с Bybit (Rose любит
                          # помойки) не должен плодить вечные pending-записи
BLIND_WARN_H = 6.0        # оба канала не читаются дольше → TG-предупреждение
                          # (грабли: каналы были мертвы ~2 нед недокументированно,
                          # аудит lead-time 2026-07-01); не чаще раза в сутки
OUTCOME_HORIZONS_MIN = (30, 120, 1440)
SEEN_CAP = 300            # ids на канал в стейте

BEAR_KW = ("bear", "short", "шорт", "sell", "падение", "вниз", "медвеж")
SIGNALS_HEADER = ["ts_utc", "signal_id", "channel", "symbol", "direction",
                  "post_ts_utc", "age_min", "price", "compress_p",
                  "box_width_pct", "oi_chg_4h", "funding", "pace", "turnover",
                  "note", "raw"]
TAG_STOP = {"USD", "USDT", "THE", "FOR", "AND", "NOT", "BIG", "NEW", "OLD", "ALL"}


# ============================== ЧИСТЫЕ ЯДРА (тестируемо) ==============================

def parse_posts(raw_html: str) -> list[dict]:
    """t.me/s/ превью → [{id, ts, text}]. Блочный разбор: поля ищутся внутри
    одного message_wrap, чтобы даты/тексты не разъезжались между постами."""
    posts = []
    for block in raw_html.split("tgme_widget_message_wrap")[1:]:
        m_id = re.search(r'data-post="([^"]+/(\d+))"', block)
        m_dt = re.search(r'datetime="([^"]+)"', block)
        if not m_id or not m_dt:
            continue
        m_tx = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', block, re.S)
        text = ""
        if m_tx:
            text = html_mod.unescape(re.sub(r"<br/?>", " ", m_tx.group(1)))
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
        try:
            ts = datetime.fromisoformat(m_dt.group(1)).timestamp()  # aware → epoch
        except ValueError:
            continue
        posts.append({"id": int(m_id.group(2)), "ts": ts, "text": text})
    return posts


def extract_signal(text: str) -> tuple[str, str] | None:
    """('SOLUSDT', 'LONG'|'SHORT') или None. Консервативно: #TICKER либо явная
    пара XXXUSDT; болтовня канала («READY», «name ?») отсеивается сама."""
    base = None
    m = re.search(r"#([A-Za-z]{2,10})\b", text)
    if m and m.group(1).upper() not in TAG_STOP:
        base = m.group(1).upper()
    else:
        m = re.search(r"\b([A-Z]{2,8})/?USDT\b", text.upper())
        if m and m.group(1) not in TAG_STOP:
            base = m.group(1)
    if not base:
        return None
    lo = text.lower()
    return base + "USDT", "SHORT" if any(k in lo for k in BEAR_KW) else "LONG"


# ============================== ДАННЫЕ / ЛОГИ ==============================

def fetch_channel(channel: str) -> list[dict]:
    req = urllib.request.Request(
        f"https://t.me/s/{channel}",
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return parse_posts(r.read().decode("utf-8", "replace"))


def snapshot(symbol: str, tickers: dict) -> dict:
    """Подкоп одной монеты storm-метриками. Любое поле может быть None (нет данных)."""
    t = tickers.get(symbol) or {}
    kl30 = fetch_klines(symbol, interval="30", limit=240)
    kl1 = fetch_klines(symbol, interval="1", limit=40)
    ba = box_and_atr(kl30) if kl30 else None
    p = compression_pctile(rolling_box_widths(kl30)) if kl30 else None
    return {"price": t.get("last"), "funding": t.get("funding"),
            "turnover": t.get("turnover"), "oi": t.get("oi"),
            "compress_p": round(p, 1) if p is not None else None,
            "box": ba, "oi_chg_4h": fetch_oi_change_4h(symbol),
            "pace": volume_pace_ratio(kl1) if kl1 else None}


def append_csv(path: Path, header: list[str], row: list):
    """Append-only с локом; телеметрия не роняет алерт-путь (конвенция storm)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path):
            new = not path.exists() or path.stat().st_size == 0
            with path.open("a", newline="", encoding="utf-8") as fp:
                w = csv.writer(fp)
                if new:
                    w.writerow(header)
                w.writerow(row)
    except Exception as e:
        print(f"[rose] csv append failed ({path.name}): {e}")


def log_signal(sig_id: str, channel: str, symbol: str, direction: str,
               post_ts: float, snap: dict, note: str, raw: str):
    ba = snap.get("box") or {}
    append_csv(
        SIGNALS_PATH, SIGNALS_HEADER,
        [_utc(), sig_id, channel, symbol, direction, _utc(post_ts),
         round((time.time() - post_ts) / 60, 1), snap.get("price"),
         snap.get("compress_p"),
         round(ba["box_width_pct"], 2) if ba else None,
         snap.get("oi_chg_4h"), snap.get("funding"), snap.get("pace"),
         snap.get("turnover"), note, raw[:200]])


def _utc(ts: float | None = None) -> str:
    dt = datetime.now(timezone.utc) if ts is None \
        else datetime.fromtimestamp(ts, timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# ============================== СООБЩЕНИЕ ==============================

def build_alert(symbol: str, side: str, pace: float, last: float,
                sig: dict) -> str:
    age_min = (time.time() - sig["post_ts"]) / 60.0
    arrow = "⬆️ пробой коробки ВВЕРХ" if side == "up" else "⬇️ пробой коробки ВНИЗ"
    return "\n".join([
        f"🌹 <b>ROSE×ШТОРМ · {symbol}</b>", "",
        f"Канал @{sig['channel']} дал {sig['direction']} {age_min:.0f} мин назад — "
        f"и объём ПОДТВЕРДИЛСЯ:",
        f"{arrow} (8ч-коробка {sig['box_width_pct']:.1f}%)",
        f"⚡ Темп объёма: <b>{pace:.1f}×</b> нормы (1м бары)",
        f"💵 Цена: <code>{fmt_price(last)}</code>",
        "", f'📈 <a href="{mexc_tv_link(symbol)}">График MEXC-перп (TradingView)</a>',
        "", "<i>Сторона пробоя — факт, не прогноз. Точность Rose не доказана; "
        "решение о входе — за тобой.</i>"])


# ============================== ПЕТЛЯ ==============================

def poll_channels(state: dict, tickers_cache: dict, dry_run: bool = False) -> dict:
    """Один опрос всех каналов. Возвращает свежие тикеры (если пришлось качать)."""
    any_ok = False
    for ch in ROSE_CHANNELS:
        try:
            posts = fetch_channel(ch)
        except Exception as e:
            print(f"[rose] {ch}: опрос не удался, живём дальше: {e}")
            continue
        if posts:
            any_ok = True
        seen = state["seen"].setdefault(ch, [])
        first_run = not seen and not state.get("seeded", {}).get(ch)
        for post in posts:
            if post["id"] in seen:
                continue
            seen.append(post["id"])
            if first_run:          # холодный старт: историю не переигрываем
                continue
            sig = extract_signal(post["text"])
            if not sig:
                continue
            symbol, direction = sig
            # Правило брата (2026-07-05, проверено данными 55/56): канал rose
            # ВСЕГДА лонгует — его «шорты» это BEAR_KW-эвристика, съевшая
            # новость («Saylor selling BTC»). Premium шортит по-настоящему —
            # не трогаем. Синхронно с rose_history/dashboard_feed (ревью #7).
            if ch == "rose":
                direction = "LONG"
            sig_id = f"{ch}/{post['id']}"
            age_min = (time.time() - post["ts"]) / 60.0
            if not tickers_cache:
                try:
                    tickers_cache.update(fetch_tickers())
                except Exception as e:
                    # сигнал НЕ терять: снять пометку seen → следующий опрос
                    # переиграет пост (transient-фейл сети ≠ выброшенный сигнал)
                    print(f"[rose] tickers fail, {sig_id} отложен до след. опроса: {e}")
                    seen.remove(post["id"])
                    break
            # ⚡ live-маркер поста для связок дашборда (брат 07.07: «нужно 2 мин»).
            # ПОСЛЕ tickers: в маркер идёт ЦЕНА НА МОМЕНТ ПОСТА — фиксированный
            # базис связки (ревью 07.07: без него fast-basis дрейфовал за ценой
            # и правило «провал −3%» было слепым — кейс LIT). Fail-open.
            try:
                _mark = {"channel": ch, "msg_id": post["id"], "symbol": symbol,
                         "direction": direction.lower(),
                         "price": (tickers_cache.get(symbol) or {}).get("last"),
                         "ts_utc": datetime.fromtimestamp(
                             post["ts"], tz=timezone.utc).isoformat()}
                _now = time.time()
                atomic_json_update(
                    Path(__file__).parent / "dashboard" / "rose_live_posts.json",
                    lambda lst, m=_mark, n=_now: (
                        [x for x in (lst or [])
                         if n - datetime.fromisoformat(x["ts_utc"]).timestamp() < 48 * 3600
                         and not (x["channel"] == m["channel"] and x["msg_id"] == m["msg_id"])]
                        + [m])[-100:],
                    default=[])
            except Exception as _e:
                print(f"[rose] live-маркер не записан (не критично): {_e}")
            if symbol not in tickers_cache:
                # монета не на Bybit — подкопать нечем, но факт фиксируем:
                # доля таких сигналов сама по себе ответ про применимость идеи
                log_signal(sig_id, ch, symbol, direction, post["ts"],
                           {}, "not_on_bybit", post["text"])
                print(f"[rose] {sig_id} {symbol}: не на Bybit, только лог")
                continue
            snap = snapshot(symbol, tickers_cache)
            expired = age_min > ROSE_WATCH_H * 60
            note = "expired" if expired else ("late" if age_min > LATE_MIN else "armed")
            in_cd = time.time() - state["cooldown"].get(symbol, 0) \
                < IGNITE_COOLDOWN_H * 3600
            if in_cd:
                note = "cooldown"
            log_signal(sig_id, ch, symbol, direction, post["ts"], snap,
                       note, post["text"])
            state["pending"].append(
                {"id": sig_id, "symbol": symbol, "ts": post["ts"],
                 "price0": snap.get("price"), "done": []})
            box = snap.get("box")
            if not expired and not in_cd and not dry_run:
                # box=None (transient-фейл свечей / монете <8ч) — под надзор всё
                # равно: box_ts=0 форсирует живой пересчёт первым же циклом
                entry = {"id": sig_id, "channel": ch, "direction": direction,
                         "post_ts": post["ts"],
                         "until": post["ts"] + ROSE_WATCH_H * 3600,
                         "box_ts": time.time() if box else 0.0, "box_fails": 0}
                if box:
                    entry.update({k: box[k] for k in ("box_high", "box_low",
                                                      "atr", "box_width_pct")})
                state["active"][symbol] = entry
            print(f"[rose] {sig_id}: {symbol} {direction}, age {age_min:.0f}м, "
                  f"note={note}, pace={snap.get('pace')}, "
                  f"p={snap.get('compress_p')}")
        del seen[:-SEEN_CAP]
        # seeded — ТОЛЬКО при непустой ленте: parse_posts мог вернуть [] на
        # неожиданном HTML, и пометка «засеян» с пустым seen обернулась бы
        # переигрыванием всей истории канала при первом успешном парсе
        # (шквал ложных сигналов, ревью 2026-07-06 #9)
        if posts:
            state.setdefault("seeded", {})[ch] = True
    # сторож тишины: молчащий поллер = недокументированно мёртвые каналы
    # (грабли аудита 2026-07-01: 2 канала были мертвы ~2 недели)
    now = time.time()
    if any_ok:
        state["last_ok_poll"] = now
    elif (now - state.get("last_ok_poll", now) > BLIND_WARN_H * 3600
          and now - state.get("blind_warn_ts", 0) > 86400):
        blind_h = (now - state["last_ok_poll"]) / 3600
        send_tg(f"⚠️ <b>rose_watch ослеп</b>: каналы {ROSE_CHANNELS} не читаются "
                f"уже {blind_h:.0f}ч — t.me/s/ недоступен или каналы закрылись.")
        state["blind_warn_ts"] = now
    state.setdefault("last_ok_poll", now)
    return tickers_cache


def check_active(state: dict, tickers: dict):
    """Быстрый надзор: пробой коробки поста + темп объёма → один алерт."""
    now = time.time()
    for sym in list(state["active"]):
        sig = state["active"][sym]
        if now > sig["until"]:
            del state["active"][sym]
            print(f"[rose] {sym}: окно надзора истекло без поджига")
            continue
        if now - state["cooldown"].get(sym, 0) < IGNITE_COOLDOWN_H * 3600:
            continue                     # фейл TG → ретрай по короткому кулдауну
        t = tickers.get(sym)
        if not t or t["last"] <= 0:
            continue
        if now - sig.get("box_ts", 0) > BOX_TTL_SEC:
            # живая коробка: 64×30м = 32ч истории, хватает на box(16)+ATR(14)
            kl30 = fetch_klines(sym, interval="30", limit=64)
            ba = box_and_atr(kl30) if kl30 else None
            if ba:
                sig.update(ba)
                sig["box_ts"], sig["box_fails"] = now, 0
            else:
                sig["box_fails"] = sig.get("box_fails", 0) + 1
                if sig["box_fails"] >= BOX_FAIL_DROP:
                    del state["active"][sym]
                    print(f"[rose] {sym}: {BOX_FAIL_DROP} фейла пересчёта коробки, дроп")
                    continue
                # старая коробка доживает до следующего пересчёта — transient-фейл
                # данных не должен ослеплять надзор (конвенция storm, аудит 2026-07-01)
        if "box_high" not in sig:
            continue                     # коробки ещё нет (фейлы) — ждём пересчёта
        side = detect_box_break(t["last"], sig["box_high"], sig["box_low"],
                                sig["atr"])
        if not side:
            continue
        kl1 = fetch_klines(sym, interval="1", limit=40)
        pace = volume_pace_ratio(kl1) if kl1 else None
        if pace is None or pace < PACE_MULT:
            continue
        msg = build_alert(sym, side, pace, t["last"], sig)
        sent = send_tg(msg, radar_buttons(sym))
        # конвенция storm: доставка → полный кулдаун, фейл TG → ретрай ~5 мин
        state["cooldown"][sym] = now if sent \
            else now - IGNITE_COOLDOWN_H * 3600 + IGNITE_RETRY_SEC
        if sent:
            del state["active"][sym]
        append_csv(SIGNALS_PATH, SIGNALS_HEADER,
                   [_utc(), sig["id"], sig["channel"], sym, sig["direction"],
                        _utc(sig["post_ts"]),
                        round((now - sig["post_ts"]) / 60, 1), t["last"],
                        None, sig["box_width_pct"], None, None, round(pace, 2),
                        None, f"IGNITE_{side}_sent={int(sent)}", ""])
        print(f"[rose] 🔥 {sym}: поджиг {side}, pace {pace:.1f}×, sent={sent}")


def resolve_outcomes(state: dict, tickers: dict):
    now = time.time()
    for p in list(state["pending"]):
        if p.get("price0") in (None, 0) or now > p["ts"] + PENDING_MAX_H * 3600:
            state["pending"].remove(p)   # битая цена или делист/пропали данные:
            continue                     # что успели — в CSV, вечников не держим
        for h in OUTCOME_HORIZONS_MIN:
            if h in p["done"] or now < p["ts"] + h * 60:
                continue
            t = tickers.get(p["symbol"])
            if not t or t["last"] <= 0:
                continue
            append_csv(OUTCOMES_PATH,
                       ["ts_utc", "signal_id", "symbol", "horizon_min",
                        "price0", "price", "pct"],
                       [_utc(), p["id"], p["symbol"], h, p["price0"], t["last"],
                        round((t["last"] - p["price0"]) / p["price0"] * 100, 3)])
            p["done"].append(h)
        if len(p["done"]) == len(OUTCOME_HORIZONS_MIN):
            state["pending"].remove(p)


def load_state() -> dict:
    st = atomic_json_read(STATE_PATH, default={}) or {}
    for k, d in (("seen", {}), ("active", {}), ("pending", []),
                 ("cooldown", {}), ("seeded", {})):
        st.setdefault(k, d)
    return st


def loop():
    print(f"[rose] старт: каналы {ROSE_CHANNELS}, опрос {POLL_TG_SEC}с, "
          f"надзор {ROSE_WATCH_H:.0f}ч (коробка живая, ttl {BOX_TTL_SEC:.0f}с), "
          f"темп ≥{PACE_MULT}×")
    state = load_state()
    last_poll = 0.0
    while True:
        try:
            now = time.time()
            tickers: dict = {}
            if state["active"] or state["pending"]:
                try:
                    tickers = fetch_tickers()
                except Exception as e:
                    print(f"[rose] tickers fail, цикл пропущен: {e}")
            if now - last_poll >= POLL_TG_SEC:
                tickers = poll_channels(state, tickers)
                last_poll = now
            if state["active"] and tickers:
                check_active(state, tickers)
            if state["pending"] and tickers:
                resolve_outcomes(state, tickers)
            state["cooldown"] = {k: v for k, v in state["cooldown"].items()
                                 if now - v < IGNITE_COOLDOWN_H * 3600}
            atomic_json_update(STATE_PATH, lambda _: state, default={})
            time.sleep(LOOP_ACTIVE_SEC if state["active"] else LOOP_IDLE_SEC)
        except KeyboardInterrupt:
            print("[rose] остановлен")
            return
        except Exception as ex:
            print(f"[rose] ошибка цикла (живём дальше): {ex}")
            time.sleep(LOOP_IDLE_SEC)


# ============================== CLI ==============================

def analyze(symbol: str):
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    tickers = fetch_tickers()
    if symbol not in tickers:
        print(f"{symbol}: не на Bybit")
        return
    s = snapshot(symbol, tickers)
    ba = s.get("box") or {}
    oi = f"{s['oi_chg_4h']:+.1f}%" if s.get("oi_chg_4h") is not None else "н/д"
    pace = f"{s['pace']:.2f}" if s.get("pace") is not None else "н/д"
    print(f"{symbol}: цена {s['price']}, сжатие p{s['compress_p']}, "
          f"коробка {ba.get('box_width_pct', 0):.2f}%/8ч, OI4ч {oi}, "
          f"фандинг {(s['funding'] or 0) * 100:+.3f}%, "
          f"темп объёма {pace}× (поджиг от {PACE_MULT}×)")


def once():
    for ch in ROSE_CHANNELS:
        posts = fetch_channel(ch)
        print(f"\n=== @{ch}: {len(posts)} постов ===")
        for p in posts[-8:]:
            sig = extract_signal(p["text"])
            mark = f" → СИГНАЛ {sig[0]} {sig[1]}" if sig else ""
            print(f"  [{p['id']}] {_utc(p['ts'])}  {p['text'][:80]}{mark}")


def selfcheck():
    fixture = '''
    <div class="tgme_widget_message_wrap"><div data-post="rose/101"
    class="tgme_widget_message"><div class="tgme_widget_message_text js-message_text">
    #sol bullish</div><time datetime="2026-07-03T03:00:29+00:00"></time></div></div>
    <div class="tgme_widget_message_wrap"><div data-post="rose/102"
    class="tgme_widget_message"><div class="tgme_widget_message_text">READY</div>
    <time datetime="2026-07-03T03:05:00+00:00"></time></div></div>'''
    posts = parse_posts(fixture)
    assert [p["id"] for p in posts] == [101, 102], posts
    assert posts[0]["text"] == "#sol bullish"
    want = datetime(2026, 7, 3, 3, 0, 29, tzinfo=timezone.utc).timestamp()
    assert posts[0]["ts"] == want, "парсер дат обязан быть aware, не naive-local"
    assert extract_signal("#sol bullish") == ("SOLUSDT", "LONG")
    assert extract_signal("#ZEC short now") == ("ZECUSDT", "SHORT")
    assert extract_signal("BTCUSDT шорт от 60k") == ("BTCUSDT", "SHORT")
    assert extract_signal("READY") is None
    assert extract_signal("please don't") is None
    assert extract_signal("#THE big news") is None       # стоп-слово
    assert extract_signal("name ?") is None
    print("✓ self-check passed: парсер t.me/s/ (блочный, aware-даты), "
          "извлечение тикера/направления, отсев болтовни")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Сторож сигналов Rose поверх storm-метрик")
    ap.add_argument("--loop", action="store_true", help="резидентный сторож")
    ap.add_argument("--once", action="store_true", help="один опрос, печать, без стейта")
    ap.add_argument("--analyze", metavar="SYMBOL", help="ручной подкоп монеты")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        selfcheck()
    elif a.once:
        once()
    elif a.analyze:
        analyze(a.analyze)
    elif a.loop:
        loop()
    else:
        ap.print_help()

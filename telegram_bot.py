"""
telegram_bot.py — Интерактивный Telegram бот для скринера.

Команды (только из авторизованного chat_id):
  /run   — запустить скан прямо сейчас (~30-60 сек)
  /top   — топ кандидаты из последнего скана
  /top 10 — топ 10 кандидатов
  /status — статус системы
  /help  — список команд

Запуск:
  python3 telegram_bot.py          # foreground (для теста)
  python3 telegram_bot.py daemon   # фон (вызывается LaunchAgent)

Требования: те же что у screener.py (requests, tabulate).
"""

import atexit
import json
import os
import sys
import time
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# ─── Пути ────────────────────────────────────────────────────────────────────

DIR         = Path(__file__).parent
CONFIG_PATH = DIR / "telegram_config.json"
PID_PATH    = DIR / "telegram_bot.pid"


# ─── .env loader ─────────────────────────────────────────────────────────────

def _load_dotenv():
    dotenv_path = DIR / ".env"
    if not dotenv_path.exists():
        return
    with open(dotenv_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip(); val = val.strip()
            if val and val[0] in ('"', "'") and val[-1] == val[0]:
                val = val[1:-1]
            os.environ.setdefault(key, val)


_load_dotenv()

_TELEGRAM_PROXY = os.environ.get("TELEGRAM_PROXY")
_PROXIES = {"http": _TELEGRAM_PROXY, "https": _TELEGRAM_PROXY} if _TELEGRAM_PROXY else {}

CACHE_PATH  = DIR / "last_scan_cache.json"   # кэш последнего скана для /top
LOG_PATH    = DIR / "bot.log"
LOG_MAX_BYTES = 50 * 1024 * 1024             # 50MB — ротация лога (держим 1 бэкап .1), чтоб не пух до гигабайтов

# ─── Импорт скринера ─────────────────────────────────────────────────────────

_SCREENER_ERR = ""  # default: no error
try:
    import screener as _screener
    _SCREENER_OK = True
except ImportError as e:
    _SCREENER_OK = False
    _SCREENER_ERR = str(e)

try:
    import liquidation_tracker as _liq
    _LIQ_OK = True
except ImportError:
    _LIQ_OK = False

try:
    import channel_reader as _ch
    _CH_OK = True
except ImportError:
    _CH_OK = False

TG_BASE = "https://api.telegram.org"

# ─── Состояние ───────────────────────────────────────────────────────────────

_scan_lock   = threading.Lock()
_scan_active = False        # идёт ли скан прямо сейчас
_last_update = 0            # timestamp последнего /run
_daemon_mode = False        # в daemon-режиме stdout уже идёт в лог-файл — не писать дважды

# ─── Логирование ─────────────────────────────────────────────────────────────

def _log(msg: str):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    if not _daemon_mode:
        # Обычный режим: печатаем в консоль
        print(line, flush=True)
    # Всегда пишем в файл (в daemon-режиме не дублируем — stdout уже НЕ redirected)
    try:
        # Ротация по размеру: bot.log не должен пухнуть безгранично (был 1ГБ)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, f"{LOG_PATH}.1")  # держим 1 бэкап
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ─── Конфиг ──────────────────────────────────────────────────────────────────

def load_config() -> dict:
    cfg: dict = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    # Переменные окружения имеют приоритет над JSON-файлом
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    env_chat  = os.environ.get("TELEGRAM_CHAT_ID")
    if env_token:
        cfg["bot_token"] = env_token
        cfg["enabled"]   = True
    if env_chat:
        cfg["chat_id"] = env_chat
    return cfg


# ─── Telegram API ─────────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def tg_send(token: str, chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
    MAX = 4000
    chunks = []
    while len(text) > MAX:
        split_at = text.rfind("\n", 0, MAX)
        split_at = split_at if split_at > 0 else MAX
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)

    ok = True
    for i, chunk in enumerate(chunks):
        try:
            r = requests.post(
                f"{TG_BASE}/bot{token}/sendMessage",
                json={
                    "chat_id":    chat_id,
                    "text":       chunk,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=10,
                proxies=_PROXIES,
            )
            data = r.json()
            if not data.get("ok"):
                _log(f"TG send error: {data.get('description')}")
                ok = False
        except Exception as e:
            _log(f"TG send exception: {e}")
            ok = False
        if i < len(chunks) - 1:
            time.sleep(0.4)
    return ok


def tg_get_updates(token: str, offset: int, timeout: int = 30) -> Optional[list]:
    """
    Возвращает список обновлений или None при ошибке.
    None позволяет основному циклу применить экспоненциальный backoff.
    """
    try:
        r = requests.get(
            f"{TG_BASE}/bot{token}/getUpdates",
            params={"offset": offset, "timeout": timeout,
                    # P0-2 (петля): без callback_query TG не доставит нажатия кнопок
                    "allowed_updates": '["message","callback_query"]'},
            timeout=timeout + 5,
            proxies=_PROXIES,
        )
        data = r.json()
        if data.get("ok"):
            return data.get("result", [])
        _log(f"getUpdates API error: {data.get('description')}")
        return None
    except requests.exceptions.Timeout:
        _log("getUpdates timeout (сеть медленная или недоступна)")
        return None
    except requests.exceptions.ConnectionError as e:
        _log(f"getUpdates connection error: {e}")
        return None
    except Exception as e:
        _log(f"getUpdates error: {e}")
        return None


# ─── Кэш последнего скана ─────────────────────────────────────────────────────

def save_cache(filtered: list):
    try:
        payload = {
            "ts":       datetime.now().isoformat(),
            "filtered": filtered,
        }
        # filtered содержит tuple-значения (lvl_bfvg и т.д.) — конвертируем
        text = json.dumps(payload, default=str, ensure_ascii=False, indent=2)
        CACHE_PATH.write_text(text, encoding="utf-8")
    except Exception as e:
        _log(f"save_cache error: {e}")


def load_cache() -> Optional[dict]:
    if not CACHE_PATH.exists():
        return None
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


# ─── Форматирование /top ──────────────────────────────────────────────────────

def _fmt_price(p) -> str:
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "?"
    if p >= 100:   return f"{p:.2f}"
    if p >= 1:     return f"{p:.4f}"
    if p >= 0.01:  return f"{p:.5f}"
    return f"{p:.8f}"


def format_top(filtered: list, n: int = 5, ts: str = "") -> str:
    candidates = sorted(filtered, key=lambda x: x.get("score", 0), reverse=True)[:n]
    if not candidates:
        return "Нет кандидатов в кэше."

    header = f"<b>TOP-{n} из последнего скана</b>"
    if ts:
        header += f"\n<i>Скан: {ts}</i>"
    lines = [header, ""]

    SETUP_EMOJI = {
        "squeeze":     "⚡",
        "bos_fvg":     "📐",
        "range_sweep": "↔️",
        "breakout":    "🚀",
    }
    GRADE_EMOJI = {
        "A+": "🏆", "A": "🟢", "B+": "🔵", "B": "🟡", "C": "🟠", "D": "🔴",
    }

    for i, r in enumerate(candidates, 1):
        sym   = r.get("symbol", "?")
        score = r.get("score", 0)
        price = _fmt_price(r.get("price", 0))
        fund  = r.get("fund_%", 0) or 0
        oi    = r.get("oi24h_%", 0) or 0
        setup = r.get("setup", "")
        grade = r.get("grade") or r.get("notes", "")[:2]   # fallback

        se = SETUP_EMOJI.get(setup, "📊")
        ge = GRADE_EMOJI.get(grade, "")

        bnb_fund = r.get("bnb_fund")
        bnb_str  = f"  BNB fund: <b>{bnb_fund:+.4f}%</b>" if bnb_fund is not None else ""

        lines.append(
            f"{i}. {se}{ge} <b>{_esc(sym)}</b>  score=<b>{score}</b>"
        )
        lines.append(
            f"   Цена: <code>{price}</code>"
            f"  Fund: <b>{fund:+.3f}%</b>"
            f"  OI24h: {oi:+.1f}%"
            f"{bnb_str}"
        )
        conf = r.get("channel_conf", [])
        if conf:
            src = "  ".join(f"@{c}" for c in conf[:3])
            lines.append(f"   📡 {_esc(src)}")
        lines.append("")

    return "\n".join(lines)


def format_status() -> str:
    cfg = load_config()
    cache = load_cache()

    last_scan = "—"
    n_candidates = 0
    if cache:
        last_scan    = cache.get("ts", "?")[:16].replace("T", " ")
        n_candidates = len(cache.get("filtered", []))

    lines = [
        "<b>📡 СТАТУС СИСТЕМЫ</b>",
        "",
        f"Screener: {'✅ OK' if _SCREENER_OK else '❌ ошибка импорта'}",
        f"Telegram: {'✅ настроен' if cfg.get('enabled') else '⚠️ не включён'}",
        f"",
        f"Последний скан: <b>{last_scan}</b>",
        f"Кандидатов в кэше: <b>{n_candidates}</b>",
        f"",
        f"Статус сканирования: {'🔄 <b>Идёт скан...</b>' if _scan_active else '💤 Ожидание'}",
        f"",
        f"Время сервера: {datetime.now().strftime('%H:%M:%S')}",
    ]
    return "\n".join(lines)


# ─── Обработка скана ─────────────────────────────────────────────────────────

def _run_scan_thread(token: str, chat_id: str):
    global _scan_active, _last_update
    _scan_active = True
    _log(f"Скан запущен по команде /run от {chat_id}")
    try:
        # screener.py уже вызывает send_report внутри — отчёт придёт автоматически
        filtered = _screener.run_screener(
            top_n=50,
            min_score=35,
            watchlist_size=5,
            deep_dive_size=3,
            bypass_cooldown=True,
        )
        _last_update = time.time()
        if filtered:
            save_cache(filtered)
        tg_send(token, chat_id,
                f"✅ <b>Скан завершён</b>  |  {len(filtered)} кандидатов\n"
                f"<i>Полный отчёт выслан выше ↑</i>")
        _log(f"Скан завершён: {len(filtered)} кандидатов")
    except Exception:
        err = traceback.format_exc()
        _log(f"Ошибка скана:\n{err}")
        tg_send(token, chat_id, f"❌ <b>Скан завершился с ошибкой</b>\n<code>{_esc(err[-500:])}</code>")
    finally:
        _scan_active = False


# ─── Роутер команд ────────────────────────────────────────────────────────────

# ─── P0-2 (петля «алерт → действие → результат»): callback-обработка ─────────

ALERTS_INDEX_PATH = DIR / "outcomes" / "alerts_index.json"


def tg_answer_callback(token: str, callback_id: str, text: str = "") -> bool:
    """Обязательный ответ на callback_query — иначе у кнопки вечный спиннер."""
    try:
        r = requests.post(f"{TG_BASE}/bot{token}/answerCallbackQuery",
                          json={"callback_query_id": callback_id, "text": text[:190]},
                          timeout=10, proxies=_PROXIES)
        return bool(r.json().get("ok"))
    except Exception as e:
        _log(f"answerCallbackQuery error: {e}")
        return False


def tg_edit_markup(token: str, chat_id: str, message_id, markup: dict) -> bool:
    """Заменяет inline-клавиатуру сообщения (метка «✅ вошёл» / «⏭ пропущен»)."""
    try:
        r = requests.post(f"{TG_BASE}/bot{token}/editMessageReplyMarkup",
                          json={"chat_id": chat_id, "message_id": message_id,
                                "reply_markup": markup},
                          timeout=10, proxies=_PROXIES)
        return bool(r.json().get("ok"))
    except Exception as e:
        _log(f"editMessageReplyMarkup error: {e}")
        return False


def _update_alert_index(short_id: str, patch: dict):
    """Merge-патч записи в alerts_index.json (атомарно через file_lock)."""
    from file_lock import atomic_json_update

    def _upd(idx):
        if not isinstance(idx, dict):
            idx = {}
        if short_id in idx:
            idx[short_id] = {**idx[short_id], **patch}
        return idx

    atomic_json_update(ALERTS_INDEX_PATH, _upd, default={})


def _backfill_pump_context(_tl, trade_id: str, rec: dict):
    """review-fix: контекст pump/rug сделки из outcomes/pump_resolved.csv
    (link_screener_signal знает только resolved.csv с другой схемой).
    Матч: symbol + |ts − alert_ts| < 30 мин; ближайшая строка. Best-effort:
    на момент нажатия сигнал мог ещё не зарезолвиться — тогда просто пропуск.
    Поля кладём в pump_*-ключи (честно: это 4h-метрики, не 24h)."""
    import csv as _csv
    path = DIR / "outcomes" / "pump_resolved.csv"
    if not path.exists():
        return
    try:
        alert_dt = datetime.fromisoformat(str(rec.get("run_ts")))
        alert_epoch = alert_dt.replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return
    best = None
    with open(path, encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            if row.get("symbol") != rec.get("symbol"):
                continue
            try:
                dt = abs(float(row.get("ts") or 0) - alert_epoch)
            except ValueError:
                continue
            if dt < 1800 and (best is None or dt < best[0]):
                best = (dt, row)
    if not best:
        return
    row = best[1]
    trades = _tl._load_trades()
    for t in trades:
        if t.get("trade_id") == trade_id:
            if not t.get("funding"):
                t["funding"] = row.get("funding")
            t["pump_oi_chg_4h"] = row.get("oi_chg_4h")
            t["pump_cvd_pct"]   = row.get("cvd_pct")
            t["pump_stage"]     = row.get("stage")
            break
    _tl._save_trades(trades)


def handle_callback(cb: dict, token: str, owner_chat_id: str):
    """[✅ Вошёл] → trades.json через trade_logger (status=open) + link к сигналу;
    [⏭ Пропустил] → пометка skipped в alerts_index (статистика дисциплины).
    Идемпотентно: повторное нажатие / протухший short_id → пояснение, без дублей."""
    cb_id   = cb.get("id", "")
    data    = cb.get("data") or ""
    from_id = str((cb.get("from") or {}).get("id", ""))
    msg     = cb.get("message") or {}
    cb_chat = str(((msg.get("chat") or {}).get("id", "")))
    msg_id  = msg.get("message_id")

    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "tr":
        tg_answer_callback(token, cb_id)
        return
    action, sid = parts[1], parts[2]

    if action == "noop":
        tg_answer_callback(token, cb_id, "Уже обработано")
        return

    # Безопасность: кнопки принимаем только от владельца
    # (его user_id == личный owner_chat_id; в TG личный chat_id = user_id)
    if str(owner_chat_id) and from_id != str(owner_chat_id):
        _log(f"Отклонён callback от неавторизованного from_id={from_id}")
        tg_answer_callback(token, cb_id, "Не авторизован")
        return

    try:
        from file_lock import atomic_json_read
        idx = atomic_json_read(ALERTS_INDEX_PATH, default={}) or {}
    except Exception as e:
        _log(f"alerts_index read error: {e}")
        tg_answer_callback(token, cb_id, "⚠ Индекс недоступен — действие не записано")
        return

    rec = idx.get(sid)
    if not rec:
        tg_answer_callback(token, cb_id,
                           "⚠ Сигнал устарел (выпал из индекса) — действие не записано")
        return
    if rec.get("status") in ("entered", "skipped"):
        tg_answer_callback(token, cb_id, f"Уже записано ранее: {rec['status']}")
        return

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    if action == "skip":
        _update_alert_index(sid, {"status": "skipped", "action_ts": now_iso})
        tg_answer_callback(token, cb_id, "⏭ Пропуск записан")
        if msg_id:
            tg_edit_markup(token, cb_chat, msg_id, {"inline_keyboard": [[
                {"text": "⏭ пропущен", "callback_data": f"tr:noop:{sid}"}]]})
        return

    if action == "in":
        try:
            import trade_logger as _tl
            trade = {
                "symbol":             rec.get("symbol"),
                "setup":              rec.get("setup"),
                "score":              rec.get("score"),
                "grade":              rec.get("grade"),
                "direction":          rec.get("direction") or "long",
                "entry_ts":           now_iso,
                "entry_price":        rec.get("entry"),
                "stop_price":         rec.get("sl"),
                "tp1_price":          rec.get("tp"),
                "status":             "open",
                "screener_signal_ts": rec.get("run_ts"),
            }
            tid = _tl.log_trade(trade)
            _setup_l = str(rec.get("setup") or "").lower()
            if _setup_l in ("pump", "rug_prep"):
                # review-fix (wf_833df4a5): pump/rug резолвятся в pump_resolved.csv
                # (схема ts-epoch) — link_screener_signal туда не смотрит
                try:
                    _backfill_pump_context(_tl, tid, rec)
                except Exception as _pe:
                    _log(f"pump backfill (не критично): {_pe}")
            else:
                try:
                    # Бэкфилл контекста из resolved.csv (если сигнал уже зарезолвлен)
                    _tl.link_screener_signal(tid, rec.get("run_ts") or "", rec.get("symbol") or "")
                except Exception as _le:
                    _log(f"link_screener_signal (не критично): {_le}")
            _update_alert_index(sid, {"status": "entered", "trade_id": tid,
                                      "action_ts": now_iso})
            tg_answer_callback(token, cb_id, "✅ Вход записан (trades.json, статус open)")
            if msg_id:
                tg_edit_markup(token, cb_chat, msg_id, {"inline_keyboard": [[
                    {"text": "✅ вошёл (записано)", "callback_data": f"tr:noop:{sid}"}]]})
        except Exception as e:
            _log(f"trade log error: {e}")
            tg_answer_callback(token, cb_id, f"🔴 Ошибка записи: {e}")
        return

    tg_answer_callback(token, cb_id)


def handle_command(text: str, token: str, chat_id: str, authorized_chat_id: str):
    """Парсит команду и отправляет ответ."""
    # Безопасность: только авторизованный chat_id
    if str(chat_id) != str(authorized_chat_id):
        _log(f"Отклонена команда от неавторизованного chat_id={chat_id}")
        return

    text = (text or "").strip()
    cmd  = text.lower().split()[0] if text else ""

    # Нормализуем команды с @mention (/run@mybot → /run)
    cmd = cmd.split("@")[0]

    if cmd in ("/run", "/scan", "/start_scan"):
        if not _SCREENER_OK:
            tg_send(token, chat_id,
                    f"❌ Screener не загружен: <code>{_esc(_SCREENER_ERR)}</code>")
            return
        with _scan_lock:
            if _scan_active:
                tg_send(token, chat_id, "⏳ Скан уже запущен, подожди...")
                return

        # Антифлуд: не чаще раза в 2 минуты
        cooldown = 120
        since_last = time.time() - _last_update
        if _last_update > 0 and since_last < cooldown:
            wait = int(cooldown - since_last)
            tg_send(token, chat_id, f"⏳ Подожди ещё {wait} сек перед следующим сканом.")
            return

        tg_send(token, chat_id,
                "🔄 <b>Запускаю скан...</b>\n"
                "<i>Обычно занимает 30-60 секунд.\n"
                "Отчёт придёт несколькими сообщениями.</i>")
        t = threading.Thread(
            target=_run_scan_thread,
            args=(token, chat_id),
            daemon=True,
        )
        t.start()

    elif cmd == "/top":
        parts = text.split()
        try:
            n = int(parts[1]) if len(parts) > 1 else 5
            n = max(1, min(n, 20))
        except ValueError:
            n = 5

        cache = load_cache()
        if not cache:
            tg_send(token, chat_id, "ℹ️ Нет данных. Запусти /run для первого скана.")
            return

        filtered = cache.get("filtered", [])
        ts       = cache.get("ts", "?")[:16].replace("T", " ")
        tg_send(token, chat_id, format_top(filtered, n, ts))

    elif cmd in ("/status", "/s"):
        tg_send(token, chat_id, format_status())

    elif cmd == "/liq":
        # /liq              → сводка топ-15 монет за 24h
        # /liq BTCUSDT      → хитмап BTC за 4h
        # /liq large        → крупные ликвидации за 1h
        # /liq BTCUSDT 1h   → хитмап BTC за 1h (суффикс h — часы)
        # /liq large 4h     → крупные ликвидации за 4h
        if not _LIQ_OK:
            tg_send(token, chat_id,
                    "❌ liquidation_tracker не загружен.\n"
                    "<code>pip install websockets</code> и перезапусти бота.")
            return

        con  = _liq.init_db()
        args = text.split()[1:]   # всё после /liq

        # Парсим опциональное окно вида "4h" / "1h" / "24h"
        window_h = None
        filtered_args = []
        for a in args:
            if a.endswith("h") and a[:-1].replace(".", "").isdigit():
                window_h = float(a[:-1])
            else:
                filtered_args.append(a)
        args = filtered_args

        sub = args[0].lower() if args else ""

        if sub == "large":
            wh = window_h or 1.0
            reply = _liq.tg_format_large(con, min_usd=50_000, window_h=wh)
        elif sub and sub != "stats":
            # Интерпретируем как символ (напр. BTC или BTCUSDT)
            sym = sub.upper()
            if not sym.endswith("USDT"):
                sym += "USDT"
            wh = window_h or 4.0
            reply = _liq.tg_format_heatmap(con, sym, window_h=wh)
        else:
            # Общая сводка
            wh = window_h or 24.0
            reply = _liq.tg_format_stats(con, window_h=wh)

        tg_send(token, chat_id, reply)

    elif cmd in ("/channels", "/ch"):
        # Показать сигналы из кэша каналов (без сетевого запроса)
        # /channels scan — запустить новый скан прямо сейчас (async)
        if not _CH_OK:
            tg_send(token, chat_id,
                    "❌ channel_reader не загружен.\n"
                    "<code>pip install telethon</code> и убедись что channel_reader.py рядом.")
            return

        args = text.split()[1:]
        if args and args[0].lower() == "scan":
            # Запускаем скан в фоне
            import asyncio
            def _do_scan():
                try:
                    cfg = _ch.load_cfg()
                    if not cfg.get("api_id"):
                        tg_send(token, chat_id,
                                "❌ Канал-ридер не настроен.\n"
                                "Запусти: <code>python3 channel_reader.py setup</code>")
                        return
                    tg_send(token, chat_id, "📡 Читаю каналы…")
                    channel_results = asyncio.run(_ch.scan_channels_async(cfg))
                    _ch.save_cache(channel_results)
                    verified = _ch.cross_verify(channel_results)
                    msg = _ch.format_channel_insights(verified)
                    tg_send(token, chat_id, msg)
                except Exception as exc:
                    tg_send(token, chat_id, f"❌ Ошибка скана каналов: {exc}")
            threading.Thread(target=_do_scan, daemon=True).start()
        else:
            # Показать результаты из кэша
            cache_path = _ch.CACHE_PATH
            if not cache_path.exists():
                tg_send(token, chat_id,
                        "ℹ️ Нет кэша каналов.\n"
                        "Отправь <code>/channels scan</code> для первого скана\n"
                        "или запусти: <code>python3 channel_reader.py scan</code>")
                return
            try:
                import json as _json
                cache = _json.loads(cache_path.read_text(encoding="utf-8"))
                channel_results = cache.get("results", {})
                verified = _ch.cross_verify(
                    {ch: items for ch, items in channel_results.items()}
                )
                ts = cache.get("ts", "")[:16].replace("T", " ")
                msg = _ch.format_channel_insights(verified)
                msg = f"<i>Кэш от {ts}</i>\n\n" + msg
                tg_send(token, chat_id, msg)
            except Exception as exc:
                tg_send(token, chat_id, f"❌ Ошибка чтения кэша: {exc}")

    elif cmd in ("/budget", "/cost", "/b"):
        # Стоимость Claude RT-фильтра за 24h
        try:
            from claude_realtime_filter import summarize_costs
            s = summarize_costs(24)
            lines = [
                f"💰 <b>Claude RT-фильтр — 24h</b>",
                "",
                f"  Вызовов: <b>{s['calls']}</b>",
                f"  Стоимость: <b>${s['cost_usd']:.4f}</b>",
                f"  Tokens: in={s['in_tok']:,} out={s['out_tok']:,}",
            ]
            if s["by_verdict"]:
                v_str = "  ".join(f"{k}:{v}" for k, v in s["by_verdict"].items())
                lines.append(f"  Verdicts: {v_str}")
            tg_send(token, chat_id, "\n".join(lines))
        except Exception as e:
            tg_send(token, chat_id, f"❌ /budget error: {e}")

    elif cmd in ("/pause", "/p"):
        try:
            from claude_realtime_filter import set_enabled
            set_enabled(False)
            tg_send(token, chat_id, "⏸ <b>Claude RT-фильтр выключен</b>\nВсе сигналы будут уходить в TG без фильтра.\n\n/resume — включить обратно")
        except Exception as e:
            tg_send(token, chat_id, f"❌ /pause error: {e}")

    elif cmd in ("/resume", "/r"):
        try:
            from claude_realtime_filter import set_enabled
            set_enabled(True)
            tg_send(token, chat_id, "▶️ <b>Claude RT-фильтр включён</b>\nКаждый сигнал проходит через Sonnet 4.6.")
        except Exception as e:
            tg_send(token, chat_id, f"❌ /resume error: {e}")

    elif cmd in ("/wait", "/watchlist", "/w"):
        try:
            from claude_realtime_filter import list_watchlist
            items = list_watchlist()
            if not items:
                tg_send(token, chat_id, "🕒 Wait-watchlist пуст")
                return
            now = int(time.time())
            lines = [f"🕒 <b>Wait-watchlist ({len(items)} активных)</b>", ""]
            for it in items[:10]:
                age = int((now - it.get("ts", 0)) / 60)
                lines.append(
                    f"  • <b>{it['symbol']}</b> [{it.get('setup','?')}]  "
                    f"conf={it.get('confidence',0):.0%}  ({age}m)"
                )
                trig = it.get("trigger", "")
                if trig:
                    lines.append(f"     → {trig[:120]}")
            tg_send(token, chat_id, "\n".join(lines))
        except Exception as e:
            tg_send(token, chat_id, f"❌ /wait error: {e}")

    # ─── P0-2 (петля): реальные сделки ───────────────────────────────────
    elif cmd == "/open":
        try:
            import trade_logger as _tl
            _open = [t for t in _tl._load_trades() if t.get("status") == "open"]
            if not _open:
                tg_send(token, chat_id, "Открытых сделок нет.")
            else:
                out = ["<b>📂 Открытые сделки</b>", ""]
                for i, t in enumerate(_open, 1):
                    out.append(f"{i}. <b>{t.get('symbol')}</b> {t.get('direction','?')} "
                               f"[{t.get('setup','—')}] entry={t.get('entry_price')} "
                               f"sl={t.get('stop_price')} с {str(t.get('entry_ts',''))[:16]}")
                out += ["", "Закрыть: /close SYMBOL +1.5R  или  /close SYMBOL 2.085"]
                tg_send(token, chat_id, "\n".join(out))
        except Exception as e:
            tg_send(token, chat_id, f"❌ /open error: {e}")

    elif cmd == "/close":
        try:
            import re as _re
            import trade_logger as _tl
            parts = text.split()
            if len(parts) < 3:
                tg_send(token, chat_id,
                        "Формат: /close SYMBOL +1.5R  |  /close SYMBOL 2.085\n"
                        "Если открытых по символу несколько: /close SYMBOL +1R #2")
                return
            sym = parts[1].upper()
            if not sym.endswith("USDT"):
                sym += "USDT"
            val = parts[2]
            sel = None
            if len(parts) >= 4 and parts[3].startswith("#"):
                try:
                    sel = int(parts[3][1:]) - 1
                except ValueError:
                    sel = None
            trades = _tl._load_trades()
            open_i = [i for i, t in enumerate(trades)
                      if t.get("symbol") == sym and t.get("status") == "open"]
            if not open_i:
                tg_send(token, chat_id, f"Открытых сделок по {sym} нет. /open — список.")
                return
            if len(open_i) > 1 and sel is None:
                out = [f"По {sym} открыто {len(open_i)} сделок — какую закрыть?", ""]
                for n, i in enumerate(open_i, 1):
                    t = trades[i]
                    out.append(f"#{n}: entry={t.get('entry_price')} от {str(t.get('entry_ts',''))[:16]}")
                out.append(f"\nПовтори: /close {sym} {val} #N")
                tg_send(token, chat_id, "\n".join(out))
                return
            if sel is not None and not (0 <= sel < len(open_i)):
                tg_send(token, chat_id, f"#N вне диапазона (открыто {len(open_i)}).")
                return
            i = open_i[sel if sel is not None else 0]
            t = trades[i]
            m = _re.match(r"^([+-]?\d+(?:[.,]\d+)?)[rR]$", val)
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            if m:
                r_mult = float(m.group(1).replace(",", "."))
                t["r_multiple"] = r_mult
                # честный пересчёт exit-цены из R по (entry, stop)
                try:
                    e_px, s_px = float(t["entry_price"]), float(t["stop_price"])
                    risk = abs(e_px - s_px)
                    t["exit_price"] = (e_px + r_mult * risk
                                       if t.get("direction") == "long"
                                       else e_px - r_mult * risk)
                except (TypeError, ValueError, KeyError):
                    pass   # R записан как есть, exit_price не вычислить без stop
            else:
                try:
                    t["exit_price"] = float(val.replace(",", "."))
                except ValueError:
                    tg_send(token, chat_id,
                            f"Не понял «{val}» — жду ±R (например +1.5R) или цену.")
                    return
            t["exit_ts"] = now_iso
            t["status"] = "closed"
            t["exit_reason"] = "manual"
            t = _tl._derive_fields(t)
            # review-fix (wf_833df4a5): outcome_label для ручного закрытия — из R,
            # иначе ЛЮБОЙ /close помечался 'breakeven' (pnl_usd пуст) и stats давал WR=0%
            _rv = t.get("r_multiple")
            if _rv is not None:
                try:
                    _rvf = float(_rv)
                    t["outcome_label"] = ("profitable" if _rvf > 0.05
                                          else "unprofitable" if _rvf < -0.05
                                          else "breakeven")
                except (TypeError, ValueError):
                    pass
            trades[i] = t
            _tl._save_trades(trades)
            r_str = t.get("r_multiple")
            tg_send(token, chat_id,
                    f"✅ Закрыто <b>{sym}</b>: exit={t.get('exit_price')}  "
                    f"R=<b>{r_str if r_str is not None else '—'}</b>  ({t.get('outcome_label','—')})")
        except Exception as e:
            tg_send(token, chat_id, f"❌ /close error: {e}")

    elif cmd in ("/help", "/start"):
        text_out = (
            "<b>📈 Screener Bot — Команды</b>\n\n"
            "/run — запустить полный скан (30-60 сек)\n"
            "/top — топ-5 из последнего скана\n"
            "/top 10 — топ-10 из последнего скана\n"
            "/status — статус системы и кэша\n"
            "\n"
            "<b>📡 Сигналы Telegram каналов</b>\n"
            "/channels — последние сигналы из кэша\n"
            "/channels scan — прочитать каналы прямо сейчас\n"
            "\n"
            "<b>💥 Ликвидации (требует сборщик)</b>\n"
            "/liq — топ-15 монет за 24h\n"
            "/liq BTCUSDT — хитмап BTC за 4h\n"
            "/liq BTCUSDT 1h — хитмап BTC за 1h\n"
            "/liq large — крупные ликвидации за 1h\n"
            "/liq large 4h — крупные ликвидации за 4h\n"
            "\n"
            "<b>🧠 Claude RT-фильтр</b>\n"
            "/budget — стоимость и вердикты за 24h\n"
            "/wait — текущий WAIT-watchlist\n"
            "/pause — выключить фильтр (всё в TG)\n"
            "/resume — включить обратно\n"
            "\n"
            "<b>📒 Петля сделок</b>\n"
            "/open — открытые сделки\n"
            "/close SYMBOL +1.5R — закрыть с результатом в R\n"
            "/close SYMBOL 2.085 — закрыть по цене\n"
            "\n"
            "/help — эта справка\n\n"
            "<i>Автоматические сканы идут каждые 4ч (00, 04, 08, 12, 16, 20 UTC).\n"
            "Сборщик ликвидаций: python3 liquidation_tracker.py</i>"
        )
        tg_send(token, chat_id, text_out)

    else:
        tg_send(token, chat_id, "❓ Неизвестная команда. /help — список команд.")


# ─── Основной цикл polling ───────────────────────────────────────────────────

def run_bot():
    cfg = load_config()
    token   = cfg.get("bot_token")
    chat_id = cfg.get("chat_id")
    # owner_chat_id — личный чат владельца для команд (/run, /top и т.д.)
    # Если не задан явно — используем chat_id (обратная совместимость)
    owner_chat_id = str(cfg.get("owner_chat_id") or chat_id)

    if not token or not chat_id:
        _log("ОШИБКА: bot_token или chat_id не настроены в telegram_config.json")
        sys.exit(1)

    # Проверка токена
    try:
        r = requests.get(f"{TG_BASE}/bot{token}/getMe", timeout=8, proxies=_PROXIES)
        data = r.json()
        if not data.get("ok"):
            _log(f"ОШИБКА: Неверный bot_token: {data.get('description')}")
            sys.exit(1)
        bot_name = data["result"]["username"]
        _log(f"Бот запущен: @{bot_name}  |  Авторизован chat_id: {chat_id}")
    except Exception as e:
        _log(f"ОШИБКА при проверке токена: {e}")
        sys.exit(1)

    tg_send(token, owner_chat_id,
            f"🤖 <b>Бот запущен</b>  |  {datetime.now().strftime('%H:%M:%S')}\n"
            f"Сигналы → {chat_id}\n"
            f"/help — команды")

    offset = 0
    _fail_count = 0         # счётчик последовательных ошибок getUpdates
    _log("Начинаю long-polling...")

    while True:
        try:
            updates = tg_get_updates(token, offset, timeout=30)
            if updates is None:
                # tg_get_updates вернул None → ошибка сети, применяем backoff
                _fail_count += 1
                wait = min(5 * _fail_count, 60)  # 5, 10, 15, … макс 60 сек
                time.sleep(wait)
                continue
            _fail_count = 0   # успех — сбрасываем счётчик
            for upd in updates:
                offset = upd["update_id"] + 1
                # P0-2 (петля): нажатия inline-кнопок [Вошёл/Пропустил]
                cb = upd.get("callback_query")
                if cb:
                    try:
                        handle_callback(cb, token, owner_chat_id)
                    except Exception as _cb_e:
                        _log(f"callback error (не валим поллинг): {_cb_e}")
                    continue
                msg    = upd.get("message", {})
                if not msg:
                    continue
                from_id = str(msg.get("chat", {}).get("id", ""))
                text    = msg.get("text", "")
                if text.startswith("/"):
                    handle_command(text, token, from_id, owner_chat_id)
        except KeyboardInterrupt:
            _log("Остановлен вручную.")
            break
        except Exception as e:
            _log(f"Ошибка polling: {e}")
            _fail_count += 1
            time.sleep(min(5 * _fail_count, 60))


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _acquire_pid_lock():
    """Не даёт запустить больше одного экземпляра бота."""
    if PID_PATH.exists():
        try:
            existing_pid = int(PID_PATH.read_text().strip())
            os.kill(existing_pid, 0)   # OSError если процесс мёртв
            print(f"[PID Lock] Бот уже запущен (PID {existing_pid}). Выход.")
            sys.exit(0)
        except (ValueError, OSError):
            PID_PATH.unlink(missing_ok=True)   # устаревший PID-файл
    PID_PATH.write_text(str(os.getpid()))
    atexit.register(lambda: PID_PATH.unlink(missing_ok=True))


def main():
    args = sys.argv[1:]

    if args and args[0] == "test":
        cfg = load_config()
        token   = cfg.get("bot_token")
        chat_id = cfg.get("chat_id")
        if not token or not chat_id:
            print("Не настроен telegram_config.json")
            sys.exit(1)
        ok = tg_send(token, str(chat_id),
                     "🧪 <b>telegram_bot.py — тест отправки</b>\n\n"
                     "/run /top /status /help — команды доступны после запуска бота.")
        print("OK" if ok else "FAIL")
        return

    if args and args[0] == "daemon":
        # Устанавливаем флаг daemon-режима — логи только в файл, не в консоль.
        # НЕ перенаправляем sys.stdout: _log() сам пишет в файл без дублирования.
        global _daemon_mode
        _daemon_mode = True

    _acquire_pid_lock()
    run_bot()


if __name__ == "__main__":
    main()

"""
telegram_alerts.py — отправка сигналов скринера в Telegram.

Настройка:
  1. Создай бота через @BotFather → получи BOT_TOKEN
  2. Напиши боту /start → открой api.telegram.org/bot<TOKEN>/getUpdates → найди chat.id
  3. Запусти: python3 telegram_alerts.py setup
     или пропиши токен и chat_id в telegram_config.json вручную

CLI:
  python3 telegram_alerts.py setup      — мастер настройки
  python3 telegram_alerts.py test       — тестовое сообщение
  python3 telegram_alerts.py status     — показать конфиг
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

_TOKEN_URL_RE = None

def redact_token(s) -> str:
    """P1-8c: маскирует bot-токен в строках (requests-исключения несут полный URL).
    Применять во ВСЕХ принтерах ошибок рядом с запросами к api.telegram.org."""
    global _TOKEN_URL_RE
    if _TOKEN_URL_RE is None:
        import re
        _TOKEN_URL_RE = re.compile(r"/bot\d+:[\w-]+")
    return _TOKEN_URL_RE.sub("/bot<REDACTED>", str(s))


try:
    import free_data as _fd
    _FD_AVAILABLE = True
except ImportError:
    _FD_AVAILABLE = False

# ─────────────────────────────────────────────────────────────
# Конфигурация
# ─────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "telegram_config.json"


# ─── .env loader ─────────────────────────────────────────────────────────────

def _load_dotenv():
    dotenv_path = Path(__file__).parent / ".env"
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


# ─── Claude realtime filter hook ─────────────────────────────────────────────
# Перед отправкой сигнала прогоняем через Claude (если ключ есть и не выключено).
# SKIP/WAIT → не шлём. GO → корректируем TP/SL под R:R стратегию пользователя.
# Fail-open: если фильтр недоступен или API упал — возвращаем allow=True.

def _apply_claude_filter(r: dict, plan: dict, source: str = "screener") -> tuple[bool, dict]:
    """
    Returns (allow_send, verdict_dict). verdict_dict пустой если фильтр выключен.
    При allow=True и verdict.action=="GO" — может модифицировать plan in-place
    (новые tp1/stop/rr + claude_reasoning).
    """
    if os.environ.get("CLAUDE_RT_FILTER", "on").lower() not in ("on", "true", "1", "yes"):
        return True, {}
    try:
        from claude_realtime_filter import filter_candidate
    except Exception as e:
        print(f"[RT-Filter] import failed: {e}", flush=True)
        return True, {}

    candidate = dict(r)
    candidate.setdefault("price", plan.get("entry") or r.get("price"))
    side = (plan.get("side") or r.get("direction") or "").lower()
    candidate["direction"] = {"long": "LONG", "short": "SHORT"}.get(side, side.upper())
    candidate["setup"] = r.get("setup") or r.get("best_setup") or "?"
    # Координируем имена полей с claude_realtime_filter.build_context()
    candidate.setdefault("funding",     r.get("fund_%") or r.get("funding"))
    candidate.setdefault("oi_24h_pct",  r.get("oi24h_%"))
    candidate.setdefault("rsi_1h",      r.get("rsi_1h"))
    candidate.setdefault("vwap_dev",    r.get("vwap_dev"))
    candidate.setdefault("rs_btc",      r.get("rs_btc"))
    candidate.setdefault("cvd_pct",     r.get("cvd_k%") or r.get("cvd_t%"))
    candidate.setdefault("liq_long_usd",  r.get("liq_long_usd"))
    candidate.setdefault("liq_short_usd", r.get("liq_short_usd"))

    v = filter_candidate(candidate["symbol"], candidate, source=source)
    action = v.get("action")
    sym = candidate.get("symbol", "?")
    print(f"[RT-Filter] {sym} {candidate['setup']} → {action} "
          f"conf={v.get('confidence',0):.2f}  {v.get('reasoning','')[:80]}",
          flush=True)

    if action == "GO":   # fail-CLOSED: FAIL_OPEN (Claude недоступен/общий кошелёк) больше НЕ шлём
        # Только полноценный GO от Claude переопределяет TP/SL
        if action == "GO":
            entry = plan.get("entry") or r.get("price") or 0
            if entry and v.get("tp_pct") and v.get("sl_pct"):
                if side == "long":
                    plan["tp1"]  = entry * (1 + v["tp_pct"] / 100)
                    plan["stop"] = entry * (1 - v["sl_pct"] / 100)
                else:
                    plan["tp1"]  = entry * (1 - v["tp_pct"] / 100)
                    plan["stop"] = entry * (1 + v["sl_pct"] / 100)
                plan["rr"] = v["tp_pct"] / v["sl_pct"] if v["sl_pct"] else plan.get("rr", 0)
                plan["levels_source"] = "claude"   # уровни реально переписаны Claude (иначе остаются ATR)
            plan["claude_reasoning"]  = v.get("reasoning", "")
            plan["claude_confidence"] = v.get("confidence", 0)
            plan["claude_risks"]      = v.get("risks", []) or []
        return True, v

    # P0-1b (2026-06-06, политика владельца): WAIT с macro_veto — макро-возражение
    # при нормальном качестве сетапа. Шлём ТЕМ ЖЕ путём, что GO (кулдауны/лимиты те же),
    # но с предупреждением в начале сообщения. TP/SL скринера НЕ переопределяем.
    if action == "WAIT" and v.get("macro_veto"):
        plan["macro_veto_note"]   = (v.get("macro_veto_reason") or v.get("reasoning") or "")[:160]
        plan["claude_reasoning"]  = v.get("reasoning", "")
        plan["claude_confidence"] = v.get("confidence", 0)
        plan["claude_risks"]      = v.get("risks", []) or []
        return True, v
    return False, v


DEFAULT_CONFIG = {
    "bot_token": None,
    "chat_id": None,
    "enabled": False,
    # Дополнительные получатели (группы, другие пользователи)
    # Пример: [-1001234567890, 987654321]
    "extra_chat_ids":   [],
    # Что отправлять
    "send_snapshot":    True,   # Макро-снапшот (F&G, сессия, направление рынка)
    "send_watchlist":   True,   # LONG/SHORT watchlist с торговым планом
    "send_deep_dive":   True,   # Deep dive топ-3 кандидата
    "send_pump":        True,   # Раздел ПАМПЫ
    "send_sector":      True,   # Ротация секторов
    "min_score_alert":  50,     # Только сигналы с score >= этого значения
    "min_pump_alert":   80,     # Только пампы с pump_score >= этого значения
    "max_symbols_tg":   5,      # Максимум символов в одном сообщении watchlist
    "deposit_usd":      None,   # Депозит в USD для расчёта размера позиции (None = выкл)
    "risk_per_trade":   0.01,   # Риск на сделку (1% по умолчанию)
}

TG_BASE = "https://api.telegram.org"


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            file_cfg = json.load(f)
        cfg.update(file_cfg)
    # Переменные окружения имеют приоритет над JSON-файлом
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    env_chat  = os.environ.get("TELEGRAM_CHAT_ID")
    if env_token:
        cfg["bot_token"] = env_token
        cfg["enabled"]   = True
    if env_chat:
        cfg["chat_id"] = env_chat
    return cfg


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────────────────────
# Отправка
# ─────────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    """Экранирует HTML-спецсимволы в обычном тексте (не в тегах)."""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


_last_suppress_notify: dict = {}   # FIX 2026-05-30: rate-limit объяснений блока (reason -> ts)


def notify_suppression(reason_key: str, text: str, cfg: Optional[dict] = None, cooldown_h: float = 4.0) -> bool:
    """В КАНАЛ: почему сигналы придержаны (kill-switch / дневной лимит / макро). Раз на cooldown_h на причину — без флуда.

    NB: rate-limit (_last_suppress_notify) — IN-MEMORY, per-process. Скринер запускается свежим процессом каждые
    ~4ч (≈ cooldown_h по умолчанию) → дедуп держит ОДИН прогон, между прогонами кулдаун и так истекает. Памп —
    персист-демон, dict живёт всю жизнь демона (сброс только при рестарте). Для standalone-надёжности вызывающий
    добавляет upstream-гейт (newly_activated у kill-switch, MAX_DAILY_ALERTS у пампа). Персист на диск НЕ нужен.
    """
    now = time.time()
    if now - _last_suppress_notify.get(reason_key, 0) < cooldown_h * 3600:
        return False
    try:
        if cfg is None:
            cfg = load_config()
        token = cfg.get("bot_token")
        chat  = str(cfg.get("chat_id") or "")
        if not token or not chat:
            return False
        ok = _send(token, chat, text)
        if ok:
            _last_suppress_notify[reason_key] = now
        return ok
    except Exception:
        return False


def _send(token: str, chat_id: str, text: str, parse_mode: str = "HTML",
          reply_markup: Optional[dict] = None):
    """Отправляет одно сообщение.
    Возвращает message_id (int, truthy) при успехе — обратно совместимо со старым bool —
    или False при ошибке. reply_markup: dict для inline-кнопок (P0-2, петля)."""
    try:
        payload = {
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        resp = requests.post(
            f"{TG_BASE}/bot{token}/sendMessage",
            json=payload,
            timeout=10,
            proxies=_PROXIES,
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"[TG] API error: {data.get('description')} | text[:80]={text[:80]!r}")
            return False
        return (data.get("result") or {}).get("message_id") or True
    except Exception as e:
        print(f"[TG] Ошибка отправки: {redact_token(e)}")
        return False


def _send_photo(token: str, chat_id: str, photo_bytes: bytes, caption: str = "") -> bool:
    """Отправляет PNG-изображение с подписью."""
    try:
        resp = requests.post(
            f"{TG_BASE}/bot{token}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("chart.png", photo_bytes, "image/png")},
            timeout=20,
            proxies=_PROXIES,
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"[TG] sendPhoto error: {data.get('description')}")
        return data.get("ok", False)
    except Exception as e:
        print(f"[TG] Ошибка отправки фото: {redact_token(e)}")
        return False


def _send_long(token: str, chat_id: str, text: str, parse_mode: str = "HTML"):
    """Разбивает длинный текст на части ≤4096 символов и отправляет последовательно."""
    MAX = 4000  # чуть меньше лимита для запаса
    chunks = []
    while len(text) > MAX:
        # Разрез по переносу строки
        split_at = text.rfind("\n", 0, MAX)
        if split_at == -1:
            split_at = MAX
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)

    for i, chunk in enumerate(chunks):
        ok = _send(token, chat_id, chunk, parse_mode)
        if not ok:
            print(f"[TG] Не удалось отправить часть {i+1}/{len(chunks)}")
        if i < len(chunks) - 1:
            time.sleep(0.4)  # Telegram rate limit ~30 msg/sec


# ─────────────────────────────────────────────────────────────
# Реал-тайм алерты (signal_monitor.py)
# ─────────────────────────────────────────────────────────────

def _fmt_ago(ts_str: str) -> str:
    """'2026-04-28T14:39:00' → '1ч 23м'"""
    try:
        from datetime import datetime as _dt
        delta = _dt.now() - _dt.fromisoformat(ts_str)
        m = int(delta.total_seconds() / 60)
        h, mm = divmod(m, 60)
        return f"{h}ч {mm}м" if h else f"{mm}м"
    except Exception:
        return "?"


# ─── P0-2 (петля «алерт → действие → результат»): кнопки + индекс алертов ────
ALERTS_INDEX_PATH = Path(__file__).parent / "outcomes" / "alerts_index.json"
ALERTS_INDEX_MAX  = 500   # ротация: храним последние ~500 алертов


def _alert_short_id(symbol: str, alert_ts: str, setup: str = "") -> str:
    """Короткий id для callback_data (лимит TG 64 байта)."""
    import hashlib
    return hashlib.sha1(f"{symbol}|{alert_ts}|{setup}".encode()).hexdigest()[:10]


def _trade_buttons(short_id: str) -> dict:
    """Inline-клавиатура [✅ Вошёл] [⏭ Пропустил]. callback_data ≤ 64 байт."""
    return {"inline_keyboard": [[
        {"text": "✅ Вошёл",     "callback_data": f"tr:in:{short_id}"},
        {"text": "⏭ Пропустил", "callback_data": f"tr:skip:{short_id}"},
    ]]}


def _register_alert(short_id: str, payload: dict):
    """Регистрирует/обновляет (merge) запись алерта в alerts_index.json —
    атомарно через file_lock, с ротацией по ts. Ошибка индекса не валит отправку."""
    try:
        from file_lock import atomic_json_update

        def _upd(idx):
            if not isinstance(idx, dict):
                idx = {}
            idx[short_id] = {**idx.get(short_id, {}), **payload}
            if len(idx) > ALERTS_INDEX_MAX:
                for k in sorted(idx, key=lambda k: (idx[k] or {}).get("ts", ""))[:len(idx) - ALERTS_INDEX_MAX]:
                    idx.pop(k, None)
            return idx

        atomic_json_update(ALERTS_INDEX_PATH, _upd, default={})
    except Exception as e:
        print(f"[TG] alerts_index error (алерт уйдёт без индекса): {e}")


try:
    from calibration.virtual_account import deposit_line as _virtual_deposit_line
except Exception:
    def _virtual_deposit_line():  # фича недоступна → алерты работают без неё
        return ""


def send_signal_alert(r: dict, plan: dict, cfg: Optional[dict] = None,
                      verdict: Optional[dict] = None) -> bool:
    """Немедленный одиночный алерт о новом сигнале (без батча) с кнопками [Вошёл/Пропустил].

    verdict: если передан вызывающим (напр. send_top_setup_alerts), фильтр УЖЕ прогнан —
    повторно Claude НЕ зовём (иначе 2-й платный вызов + другой cooldown-бакет + риск
    разъехавшегося вердикта). Тогда plan ОБЯЗАН быть финальным: entry/stop/tp1/tp2/rr +
    claude_reasoning/claude_confidence/claude_risks/macro_veto_note уже проставлены
    вызывающим. allow = sendable-вердикт (GO или macro-veto WAIT), как на памп-пути.
    """
    if cfg is None:
        cfg = load_config()
    token = cfg.get("bot_token", "")
    chat_id = str(cfg.get("chat_id", ""))
    if not token or not chat_id:
        return False

    if verdict is None:
        # Claude RT-фильтр: либо одобряет с (опционально) новыми TP/SL, либо блокирует
        _allow, _claude = _apply_claude_filter(r, plan, source="screener")
        if not _allow:
            return False
    else:
        # Вызывающий уже прогнал фильтр (source=top_setups и т.п.) — не зовём повторно.
        _act = verdict.get("action")
        if not (_act == "GO" or (_act == "WAIT" and verdict.get("macro_veto"))):
            return False

    sym   = r.get("symbol", "?")
    setup = r.get("setup", r.get("best_setup", "?"))
    side  = plan.get("side", "?")
    score = r.get("score", 0)
    grade = r.get("grade", "—")
    price = r.get("price", 0)

    SETUP_ICON = {"squeeze": "⚡", "bos_fvg": "📐", "breakout": "🚀",
                  "short_dist": "📉", "swing": "🌊", "range_sweep": "↔️"}
    DIR_ICON   = {"long": "▲", "short": "▼"}.get(side, "·")
    DIR_TXT    = {"long": "ЛОНГ", "short": "ШОРТ"}.get(side, side.upper())
    si         = SETUP_ICON.get(setup, "📊")

    entry  = plan.get("entry") or price
    stop   = plan.get("stop")
    tp1    = plan.get("tp1")
    tp2    = plan.get("tp2")
    rr     = plan.get("rr", 0)
    rr_str = f"⚠️{rr:.1f}" if rr and rr < 1.5 else f"{rr:.1f}" if rr else "—"

    def _p(v):
        return _fmt_price(v) if v else "—"

    def _pct(a, b):
        if not a or not b or b == 0:
            return ""
        return f"  ({(b - a) / a * 100:+.2f}%)"

    fund   = r.get("fund_%", 0) or 0
    oi24   = r.get("oi24h_%", 0) or 0
    mtf_b  = r.get("mtf_b", 0) or 0
    mtf_br = r.get("mtf_bear", 0) or 0
    vwap   = r.get("vwap_dev", 0) or 0
    flags  = r.get("flags", r.get("flag_str", "")) or ""

    now_str = datetime.now().strftime("%H:%M")
    lines = [
        f"⚡ <b>НОВЫЙ СИГНАЛ</b>  |  {now_str}",
        "",
        f"{si} <b>{_esc(sym)}</b>  {DIR_ICON} <b>{DIR_TXT}</b>  [{_esc(setup)}]"
        f"  Grade: <b>{grade}</b>  score=<b>{score}</b>",
        "",
        f"  Entry    <code>{_p(entry)}</code>{_pct(price, entry)}",
        f"  Stop     <code>{_p(stop)}</code>{_pct(entry, stop)}",
        f"  TP1      <code>{_p(tp1)}</code>{_pct(entry, tp1)}",
        f"  TP2      <code>{_p(tp2)}</code>{_pct(entry, tp2)}",
        f"  R:R      <b>{rr_str}</b>",
        "",
        f"  Fund: <code>{fund:+.3f}%</code>  |  OI 24h: <code>{oi24:+.1f}%</code>"
        f"  |  VWAP: <code>{vwap:+.1f}%</code>",
    ]
    # P0-1: сигнал прошёл бывший MAX_SCORE_GLOBAL-cap — честная пометка
    if r.get("score_capped"):
        lines.insert(3, "  ⚠ score>180 — исторически перегретые сетапы")

    # P0-1b: макро-вето переведено в предупреждение (политика владельца)
    if plan.get("macro_veto_note"):
        lines.insert(1, f"⚠ <b>МАКРО ПРОТИВ:</b> {_esc(plan['macro_veto_note'])}. "
                        f"Фильтр пропустил бы, вето переведено в предупреждение — решение за тобой.")
    if mtf_b or mtf_br:
        lines.append(f"  MTF: {mtf_b}↑ / {mtf_br}↓")
    if flags:
        lines.append(f"  {_esc(str(flags)[:120])}")

    # Claude reasoning + risks если фильтр прошёл с GO
    if plan.get("claude_reasoning"):
        conf = plan.get("claude_confidence", 0)
        lines += ["", f"🧠 <b>Claude</b> conf={conf:.0%}: {_esc(plan['claude_reasoning'])[:300]}"]
        for risk in (plan.get("claude_risks") or [])[:3]:
            lines.append(f"  ⚠ {_esc(str(risk))[:120]}")

    # Живой счётчик виртуального депозита «в каждый сигнал» (1x и 5x). Никогда не роняет алерт.
    try:
        _dl = _virtual_deposit_line()
        if _dl:
            lines += ["", _dl]
    except Exception:
        pass

    text = "\n".join(lines)

    # P0-2 (петля): регистрируем алерт ДО отправки (защита от мгновенного нажатия)
    # и вешаем кнопки [Вошёл/Пропустил] на ВСЕ торговые алерты, включая WAIT+macro_veto.
    alert_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    sid = _alert_short_id(sym, alert_ts, str(setup))
    _register_alert(sid, {
        "run_ts":     alert_ts,
        "symbol":     sym,
        "setup":      str(setup),
        "direction":  side if side in ("long", "short") else "long",
        "entry":      entry,
        "sl":         stop,
        "tp":         tp1,
        "score":      score,
        "grade":      grade,
        "verdict":    "WAIT" if plan.get("macro_veto_note") else "GO",
        "macro_veto": bool(plan.get("macro_veto_note")),
        "msg_id":     None,
        "ts":         alert_ts,
        "status":     "sent",
    })
    kb = _trade_buttons(sid)

    all_targets = [chat_id] + [str(c) for c in cfg.get("extra_chat_ids", []) if str(c) != chat_id]
    ok = True
    first_msg_id = None
    for cid in all_targets:
        res = _send(token, cid, text, reply_markup=kb)
        if res and res is not True and first_msg_id is None:
            first_msg_id = res
        ok = bool(res) and ok
        time.sleep(0.3)
    if first_msg_id:
        _register_alert(sid, {"msg_id": first_msg_id})

    # Ground truth: immutable снимок ДОСТАВЛЕННОГО алерта с ФАКТИЧЕСКИМИ уровнями.
    # Пишется только при реальной доставке. Никогда не роняет алерт (как virtual_deposit).
    if ok:
        try:
            from delivered_ledger import record_delivered
            record_delivered(
                alert_id=sid, symbol=sym, side=side, setup=str(setup),
                entry=entry, stop=stop, tp1=tp1, tp2=tp2,
                # честно: явная метка из плана; иначе claude если Claude переписывал уровни, не молча atr
                levels_source=(plan.get("levels_source")
                               or ("claude" if plan.get("claude_reasoning") else "atr")),
                score=score, grade=grade,
                claude_verdict=("WAIT" if plan.get("macro_veto_note") else "GO"),
                claude_confidence=plan.get("claude_confidence"),
                macro_veto=bool(plan.get("macro_veto_note")),
                tg_message_id=first_msg_id,
                screener_run_ts=(r.get("run_ts") or alert_ts),
                raw_trigger=f"{setup}|score={score}",
            )
        except Exception as e:
            print(f"[TG] delivered_ledger FAILED (алерт доставлен, но НЕ зафиксирован в ground truth!): {e}", flush=True)
    return ok


def send_trend_alert(sig: dict, cfg: Optional[dict] = None) -> bool:
    """Трендовый (Donchian) алерт о входе с кнопками [Вошёл/Пропустил].
    sig: {symbol, dir('L'/'S'), entry, stop}. Переиспользует кнопки/индекс/коллбэк old-инфры."""
    if cfg is None:
        cfg = load_config()
    token = cfg.get("bot_token", ""); chat_id = str(cfg.get("chat_id", ""))
    if not token or not chat_id:
        return False
    sym = sig["symbol"]; d = sig["dir"]; side = "long" if d == "L" else "short"
    entry = sig["entry"]; stop = sig["stop"]
    dir_txt = "ЛОНГ ▲" if d == "L" else "ШОРТ ▼"
    risk_pct = abs(entry - stop) / entry * 100 if entry else 0
    now = datetime.now().strftime("%H:%M")
    text = "\n".join([
        f"📈 <b>ТРЕНД-ПРОБОЙ</b>  |  {now}", "",
        f"<b>{_esc(sym)}</b>  {dir_txt}", "",
        f"  Entry  <code>{_fmt_price(entry)}</code>",
        f"  Stop   <code>{_fmt_price(stop)}</code>  ({risk_pct:.1f}%)",
        "  Выход: трейл по 10-дн обратному каналу",
        "", "  риск 0.5% депо · плечо ≤1x · paper",
    ])
    alert_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    sid = _alert_short_id(sym, alert_ts, "trend_breakout")
    _register_alert(sid, {"run_ts": alert_ts, "symbol": sym, "setup": "trend_breakout",
                          "direction": side, "entry": entry, "sl": stop, "tp": None,
                          "score": "—", "grade": "—", "verdict": "GO", "macro_veto": False,
                          "msg_id": None, "ts": alert_ts, "status": "sent"})
    kb = _trade_buttons(sid)
    targets = [chat_id] + [str(c) for c in cfg.get("extra_chat_ids", []) if str(c) != chat_id]
    ok = True; first = None
    for cid in targets:
        res = _send(token, cid, text, reply_markup=kb)
        if res and res is not True and first is None: first = res
        ok = bool(res) and ok; time.sleep(0.3)
    if first: _register_alert(sid, {"msg_id": first})
    return ok


def send_trend_exit(sig: dict, cfg: Optional[dict] = None) -> bool:
    """Уведомление о ВЫХОДЕ трендовой позиции (без кнопок). sig: {symbol,dir,reason,netR}."""
    if cfg is None:
        cfg = load_config()
    token = cfg.get("bot_token", ""); chat_id = str(cfg.get("chat_id", ""))
    if not token or not chat_id:
        return False
    sym = sig["symbol"]; d = "ЛОНГ" if sig.get("dir") == "L" else "ШОРТ"
    nr = sig.get("netR", 0)
    emoji = "🟢" if nr > 0 else "🔴"
    text = f"{emoji} <b>ВЫХОД</b>  {_esc(sym)} {d}  |  {_esc(str(sig.get('reason','')))}  |  netR=<b>{nr:+.2f}</b>"
    targets = [chat_id] + [str(c) for c in cfg.get("extra_chat_ids", []) if str(c) != chat_id]
    ok = True
    for cid in targets:
        ok = bool(_send(token, cid, text)) and ok; time.sleep(0.3)
    return ok


def send_trend_summary(text: str, cfg: Optional[dict] = None) -> bool:
    """Ежедневная сводка статуса трендовых позиций (готовый text, без кнопок)."""
    if cfg is None:
        cfg = load_config()
    token = cfg.get("bot_token", ""); chat_id = str(cfg.get("chat_id", ""))
    if not token or not chat_id:
        return False
    targets = [chat_id] + [str(c) for c in cfg.get("extra_chat_ids", []) if str(c) != chat_id]
    ok = True
    for cid in targets:
        ok = bool(_send(token, cid, text)) and ok; time.sleep(0.3)
    return ok


def send_cancel_alert(signal: dict, reason: str, current_price: float,
                      cfg: Optional[dict] = None) -> bool:
    """Алерт об отмене/инвалидации ранее отправленного сигнала."""
    if cfg is None:
        cfg = load_config()
    token   = cfg.get("bot_token", "")
    chat_id = str(cfg.get("chat_id", ""))
    if not token or not chat_id:
        return False

    sym       = signal.get("symbol", "?")
    setup     = signal.get("setup", "?")
    direction = signal.get("direction", "?")
    entry     = signal.get("entry", 0)
    sent_at   = signal.get("sent_at", "")
    ago       = _fmt_ago(sent_at) if sent_at else "?"

    DIR_ICON = {"long": "▲", "short": "▼"}.get(direction, "·")
    DIR_TXT  = {"long": "ЛОНГ", "short": "ШОРТ"}.get(direction, direction.upper())
    SETUP_ICON = {"squeeze": "⚡", "bos_fvg": "📐", "breakout": "🚀",
                  "short_dist": "📉", "swing": "🌊", "range_sweep": "↔️"}
    si = SETUP_ICON.get(setup, "📊")

    price_str = _fmt_price(current_price) if current_price else "—"
    entry_str = _fmt_price(entry) if entry else "—"

    lines = [
        f"❌ <b>ОТМЕНА СИГНАЛА</b>",
        "",
        f"{si} <b>{_esc(sym)}</b>  {DIR_ICON} <b>{DIR_TXT}</b>  [{_esc(setup)}]",
        f"  Причина:  <b>{_esc(reason)}</b>",
        f"  Сейчас:   <code>{price_str}</code>  |  Выслан: {ago} назад (entry {entry_str})",
        "",
        f"⛔ Сигнал более не актуален — не торговать",
    ]
    text = "\n".join(lines)

    all_targets = [chat_id] + [str(c) for c in cfg.get("extra_chat_ids", []) if str(c) != chat_id]
    ok = True
    for cid in all_targets:
        ok = _send(token, cid, text) and ok
        time.sleep(0.3)
    return ok


def send_reversal_alert(old_signal: dict, new_r: dict, new_plan: dict,
                        cfg: Optional[dict] = None) -> bool:
    """Смена направления: отменяем старый сигнал и объявляем новый."""
    if cfg is None:
        cfg = load_config()
    token   = cfg.get("bot_token", "")
    chat_id = str(cfg.get("chat_id", ""))
    if not token or not chat_id:
        return False

    sym      = old_signal.get("symbol", "?")
    old_dir  = old_signal.get("direction", "?")
    old_setup= old_signal.get("setup", "?")
    old_entry= old_signal.get("entry", 0)
    old_sc   = old_signal.get("score", 0)
    sent_at  = old_signal.get("sent_at", "")
    ago      = _fmt_ago(sent_at)

    new_side = new_plan.get("side", "?")
    new_setup= new_r.get("setup", new_r.get("best_setup", "?"))
    new_sc   = new_r.get("score", 0)
    new_grade= new_r.get("grade", "—")

    DIR_ICON = {"long": "▲", "short": "▼"}
    DIR_TXT  = {"long": "ЛОНГ", "short": "ШОРТ"}
    SETUP_ICON = {"squeeze": "⚡", "bos_fvg": "📐", "breakout": "🚀",
                  "short_dist": "📉", "swing": "🌊", "range_sweep": "↔️"}

    def _p(v): return _fmt_price(v) if v else "—"

    lines = [
        f"🔄 <b>СМЕНА РЕШЕНИЯ — {_esc(sym)}</b>",
        "",
        f"  ❌ Отмена: {SETUP_ICON.get(old_setup,'📊')} "
        f"{DIR_ICON.get(old_dir,'·')} {DIR_TXT.get(old_dir, old_dir.upper())} "
        f"score={old_sc}  entry {_p(old_entry)}  ({ago} назад)",
        "",
        f"  ✅ Новый:  {SETUP_ICON.get(new_setup,'📊')} "
        f"{DIR_ICON.get(new_side,'·')} <b>{DIR_TXT.get(new_side, new_side.upper())}</b>"
        f"  score=<b>{new_sc}</b>  Grade: <b>{new_grade}</b>",
        f"     Entry  <code>{_p(new_plan.get('entry'))}</code>"
        f"   Stop  <code>{_p(new_plan.get('stop'))}</code>"
        f"   TP1  <code>{_p(new_plan.get('tp1'))}</code>"
        f"   R:R  {new_plan.get('rr', 0):.1f}",
    ]
    text = "\n".join(lines)

    all_targets = [chat_id] + [str(c) for c in cfg.get("extra_chat_ids", []) if str(c) != chat_id]
    ok = True
    for cid in all_targets:
        ok = _send(token, cid, text) and ok
        time.sleep(0.3)
    return ok


# ─────────────────────────────────────────────────────────────
# Форматирование сообщений
# ─────────────────────────────────────────────────────────────

def _fmt_price(p: float) -> str:
    if p == 0:
        return "0"
    if p >= 100:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.5f}"
    return f"{p:.8f}"


def _grade_emoji(grade: str) -> str:
    return {"A+": "🏆", "A": "🟢", "B+": "🔵", "B": "🟡", "C": "🟠", "D": "🔴"}.get(grade, "⚪")


def _grade_risk(grade: str) -> float:
    """Риск на сделку по грейду (AVEVA-57). A+=2.5%, A=1.5%, B+=1.0%, B=0.75%, rest=1%."""
    return {"A+": 0.025, "A": 0.015, "B+": 0.010, "B": 0.0075}.get(grade, 0.01)


def _setup_emoji(setup: str) -> str:
    return {
        "squeeze":     "⚡",
        "bos_fvg":     "📐",
        "range_sweep": "↔️",
        "breakout":    "🚀",
    }.get(setup, "📊")


def format_snapshot(results: list, filtered: list, btc_chg_24h: float,
                    session_info: dict, fg_value: Optional[int],
                    fg_label: Optional[str]) -> str:
    """Макро-снапшот рынка."""
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    fg_bar = ""
    if fg_value is not None:
        level = fg_value // 10
        fg_bar = "▓" * level + "░" * (10 - level)
        fg_emoji = ("😱" if fg_value <= 20 else "😨" if fg_value <= 40 else
                    "😐" if fg_value <= 60 else "😊" if fg_value <= 80 else "🤑")

    longs  = sum(1 for r in filtered if r.get("setup") in ("squeeze", "breakout"))
    shorts = sum(1 for r in filtered if r.get("setup") in ("bos_fvg",))
    avg_fund = (sum(r.get("fund_%", 0) for r in results) / len(results)) if results else 0
    avg_oi   = (sum(r.get("oi24h_%", 0) for r in results) / len(results)) if results else 0
    session  = session_info.get("session", "—") if session_info else "—"

    lines = [
        f"<b>📊 СКРИНЕР {now}</b>",
        f"",
        f"<b>Рынок</b>",
        f"BTC 24h: <b>{'+'if btc_chg_24h>=0 else ''}{btc_chg_24h:.2f}%</b>  |  Сессия: {session}",
    ]
    if fg_value is not None:
        lines.append(f"F&amp;G: {fg_emoji} <b>{fg_value}/100</b> [{fg_label}]  {fg_bar}")
    lines += [
        f"Funding avg: <b>{avg_fund:+.3f}%</b>  |  OI avg: <b>{avg_oi:+.1f}%</b>",
    ]

    # ── Внешние бесплатные источники ────────────────────────────────────
    if _FD_AVAILABLE:
        try:
            etf = _fd.get_etf_flows()
        except Exception:
            etf = {}
        etf_parts = []
        for sym in ("btc", "eth"):
            row = (etf or {}).get(sym)
            if not row:
                continue
            flow = row["net_flow_m"]
            arrow = "▲" if flow > 0 else ("▼" if flow < 0 else "·")
            etf_parts.append(f"{sym.upper()} {arrow}{flow:+.0f}M$")
        if etf_parts:
            lines.append("ETF: " + "  ".join(etf_parts))

        try:
            opt_btc = _fd.get_options_context("BTC")
        except Exception:
            opt_btc = None
        if opt_btc and opt_btc.get("max_pain") is not None:
            mp = opt_btc["max_pain"]
            pcr = opt_btc.get("pcr")
            px = opt_btc.get("index_price") or 0
            diff = ((mp - px) / px * 100) if px else 0
            pcr_txt = f"PCR {pcr}" if pcr is not None else "PCR —"
            lines.append(
                f"BTC Opt: MaxPain <b>{mp:.0f}</b> ({diff:+.1f}%)  |  {pcr_txt}"
            )

        try:
            trend = _fd.get_trending(limit_coins=7)
        except Exception:
            trend = {}
        coins = trend.get("coins", [])
        if coins:
            names = " ".join(c["symbol"] for c in coins[:7])
            lines.append(f"🔥 Trending: {names}")

        try:
            window = _fd.next_macro_window(minutes_before=60, minutes_after=30)
        except Exception:
            window = None
        if window:
            mu = window["minutes_until"]
            when = f"через {mu:.0f} мин" if mu > 0 else f"{-mu:.0f} мин назад"
            lines.append(
                f"⚠ <b>МАКРО-ОКНО</b>: {window['title']} ({when}) — "
                f"избегать входов"
            )

        try:
            btc_dom = _fd.get_btc_dominance()
        except Exception:
            btc_dom = None
        if btc_dom is not None:
            dom_icon = "🔵" if btc_dom > 52 else ("🟢" if btc_dom < 47 else "⚪")
            dom_label = "BTC season" if btc_dom > 52 else ("Alt season" if btc_dom < 47 else "нейтр")
            lines.append(f"{dom_icon} BTC.d: <b>{btc_dom:.1f}%</b> [{dom_label}]")

    lines += [
        f"",
        f"<b>Сигналы</b>  {len(filtered)} из {len(results)} пар",
        f"🟢 Лонг: {longs}  🔴 Шорт: {shorts}",
    ]
    return "\n".join(lines)


def format_watchlist(filtered: list, max_symbols: int = 5,
                     min_score: int = 50, direction: str = "long",
                     deposit_usd: Optional[float] = None,
                     risk_per_trade: float = 0.01) -> str:
    """LONG или SHORT watchlist."""
    if direction == "long":
        candidates = [r for r in filtered
                      if r.get("setup") in ("squeeze", "breakout")
                      and r.get("score", 0) >= min_score]
        header = "🟢 <b>WATCHLIST LONG</b>"
    else:
        candidates = [r for r in filtered
                      if r.get("setup") in ("bos_fvg", "range_sweep", "short_dist")
                      and r.get("score", 0) >= min_score]
        header = "🔴 <b>WATCHLIST SHORT</b>"

    candidates = sorted(candidates, key=lambda x: x["score"], reverse=True)[:max_symbols]
    if not candidates:
        return ""

    lines = [header, ""]
    for r in candidates:
        sym   = r["symbol"]
        price = _fmt_price(r["price"])
        score = r["score"]
        grade = r.get("grade", "?")
        gem   = _grade_emoji(grade)
        setu  = _setup_emoji(r.get("setup", ""))
        fund  = r.get("fund_%", 0)
        oi    = r.get("oi24h_%", 0)
        rsi   = r.get("rsi_1h")
        chg   = r.get("change_24h", 0)

        # Торговый план
        atr_pct = r.get("atr_%", 1.0) or 1.0
        atr_abs = r["price"] * atr_pct / 100
        if direction == "long":
            bfvg = r.get("lvl_bfvg") or (None, None, None)
            entry_l = bfvg[1] or r["price"]
            entry_h = bfvg[0] or r["price"]
            stop    = entry_l - atr_abs * 0.65
            tp1     = r["price"] + atr_abs * 1.5
            tp2     = r["price"] + atr_abs * 3.0
        else:
            sfvg = r.get("lvl_sfvg") or (None, None, None)
            entry_h = sfvg[0] or r["price"]
            entry_l = sfvg[1] or r["price"]
            stop    = entry_h + atr_abs * 0.65
            tp1     = r["price"] - atr_abs * 1.5
            tp2     = max(r["price"] - atr_abs * 3.0, 0)

        h24_tag    = "  📅<b>[24H HIGH CONVICTION]</b>" if r.get("tg_24h_hold") else ""
        choch_tag  = "  🔷<b>[CHoCH CONFIRMED]</b>" if r.get("choch_conviction") else ""
        golden_tag = "  <b>[GOLDEN]</b>" if r.get("golden") else ""
        lines.append(
            f"{setu}{gem} <b>{sym}</b>  score={score}  [{grade}]{h24_tag}{choch_tag}{golden_tag}"
        )
        lines.append(
            f"   Цена: <code>{price}</code>  |  "
            f"{'+'if chg>=0 else ''}{chg:.1f}%"
        )
        lines.append(
            f"   Fund: <b>{fund:+.3f}%</b>  OI24h: {oi:+.1f}%"
            + (f"  RSI: {rsi:.0f}" if rsi else "")
        )
        risk_pct = abs(entry_l - stop) / entry_l * 100 if entry_l > 0 else 0
        lines.append(
            f"   Entry: <code>{_fmt_price(entry_l)}</code>"
            f"  Stop: <code>{_fmt_price(stop)}</code>"
            f"  ({risk_pct:.2f}%)"
        )
        lines.append(
            f"   TP1: <code>{_fmt_price(tp1)}</code>"
            f"  TP2: <code>{_fmt_price(tp2)}</code>"
        )

        # Размер позиции — grade-based риск (AVEVA-57)
        if deposit_usd and r["price"] > 0:
            _g_risk    = _grade_risk(grade)
            risk_usd   = deposit_usd * _g_risk
            sl_dist    = abs(entry_l - stop) if abs(entry_l - stop) > 0 else atr_abs * 0.65
            qty        = risk_usd / sl_dist if sl_dist > 0 else 0
            pos_size   = qty * r["price"]
            lev_approx = round(pos_size / deposit_usd, 1)
            lines.append(
                f"   💰 Размер [{grade}, риск {_g_risk*100:.2g}%]: <b>{qty:.4g}</b> конт  "
                f"(≈ ${pos_size:.0f}  |  ~{lev_approx}×)"
            )

        # Ключевые флаги
        flags = []
        if r.get("ema_1h", {}).get("ema_bull"):     flags.append("EMA↑")
        if r.get("ema_1h", {}).get("golden_cross"):  flags.append("GX!")
        choch = r.get("choch_1h")
        if isinstance(choch, dict) and choch.get("bull_choch"): flags.append("CHoCH↑")
        if isinstance(choch, dict) and choch.get("bear_choch"): flags.append("CHoCH↓")
        rsi_div = r.get("rsi_div_1h")
        if isinstance(rsi_div, dict):
            if rsi_div.get("bull_div"):    flags.append("RSI div↑")
            if rsi_div.get("hidden_bull"): flags.append("RSI hid↑")
            if rsi_div.get("bear_div"):    flags.append("RSI div↓")
        whale = r.get("whale", "—")
        if whale != "—": flags.append(f"🐳{whale}")
        if flags:
            lines.append(f"   {' | '.join(flags)}")

        # Подтверждение из Telegram каналов
        conf     = r.get("channel_conf", [])
        conflict = r.get("channel_conflict", [])
        ch_adj    = r.get("channel_score_adj", 0)
        acc_map   = r.get("channel_acc_map", {})
        if conf:
            src = "  ".join(
                f"@{c}({acc_map[c]}%)" if c in acc_map else f"@{c}"
                for c in conf[:3]
            )
            adj_str = f" {ch_adj:+d}pts" if ch_adj else ""
            lines.append(f"   📡 <b>Канал:</b> {_esc(src)}{_esc(adj_str)}")
        elif conflict:
            src = "  ".join(f"@{c}" for c in conflict[:2])
            lines.append(f"   📡 <i>Против: {_esc(src)}</i>")

        # Влияние новостей на сетап
        news_conf  = r.get("news_confirms", [])
        news_risk  = r.get("news_risks", [])
        delta      = r.get("news_score_delta", 0)
        if news_conf:
            best = news_conf[0]
            lines.append(f"   {best['icon']} <b>{_esc(best['label'])}:</b> <i>{_esc(best['reason'][:65])}</i>")
        if news_risk:
            lines.append(f"   ⚠️ <i>Риск: {_esc(news_risk[0]['reason'][:65])}</i>")
        if delta != 0:
            sign = "+" if delta > 0 else ""
            lines.append(f"   📰 score {sign}{delta} от новостей")

        lines.append("")

    return "\n".join(lines)


def format_pump_section(results: list, min_pump_score: int = 80,
                        max_show: int = 5) -> str:
    """Раздел пампов."""
    candidates = [
        r for r in results
        if r.get("pump_score", 0) >= min_pump_score
    ]
    candidates = sorted(candidates, key=lambda x: x["pump_score"], reverse=True)[:max_show]

    if not candidates:
        return ""

    lines = ["⚡ <b>ПАМПЫ — ВОЗМОЖНЫЕ ПОКУПКИ</b>", ""]

    for r in candidates:
        sym   = r["symbol"]
        ps    = r.get("pump_score", 0)
        price = _fmt_price(r["price"])
        fund  = r.get("fund_%", 0)
        pos   = r.get("pos_%", 0)
        rs    = r.get("rs_btc")
        whale = r.get("whale", "—")

        # Звёзды
        stars = ("★★★★★" if ps >= 150 else "★★★★☆" if ps >= 100 else
                 "★★★☆☆" if ps >= 70 else "★★☆☆☆")

        lines.append(f"🚀 <b>{sym}</b>  pump={ps}  {stars}")
        lines.append(f"   Цена: <code>{price}</code>  |  Fund: <b>{fund:+.3f}%</b>")

        signals = []
        if r.get("atr_comp", 1.0) < 0.70:
            signals.append(f"ATR сжатие {r['atr_comp']:.2f}×")
        if r.get("oi_coiling"):
            signals.append("OI coil")
        if whale != "—":
            signals.append(f"🐳 {whale}")
        vol_accel = r.get("vol_accel", "—")
        if vol_accel != "—":
            signals.append(f"Vol×{r.get('vol_accel_x', 0):.1f}")
        if fund < -0.5:
            signals.append(f"Fund {fund:+.3f}% (сквиз!)")
        if rs and rs > 2:
            signals.append(f"RS {rs:.1f}×BTC")
        choch = r.get("choch_1h")
        if isinstance(choch, dict) and choch.get("bull_choch"):
            signals.append("CHoCH↑")
        if pos < 15:
            signals.append(f"Поз {pos}% (у дна)")

        if signals:
            lines.append(f"   ✓ {' | '.join(signals)}")
        lines.append("")

    return "\n".join(lines)


def format_sector_rotation(results: list) -> str:
    """Ротация секторов (краткая)."""
    from config_sectors import SECTOR_MAP

    sector_data: dict[str, dict] = {}
    for r in results:
        sym = r["symbol"]
        rs  = r.get("rs_btc")
        sc  = r.get("score", 0)
        ps  = r.get("pump_score", 0)
        for sec, members in SECTOR_MAP.items():
            if sym in members:
                if sec not in sector_data:
                    sector_data[sec] = {"rs": [], "scores": [], "pumps": [], "leader": sym}
                sector_data[sec]["rs"].append(rs if rs is not None else 1.0)
                sector_data[sec]["scores"].append(sc)
                sector_data[sec]["pumps"].append(ps)
                if sc > max(sector_data[sec]["scores"][:-1], default=0):
                    sector_data[sec]["leader"] = sym
                break

    if not sector_data:
        return ""

    ranked = sorted(
        sector_data.items(),
        key=lambda x: sum(x[1]["rs"]) / len(x[1]["rs"]) if x[1]["rs"] else 0,
        reverse=True,
    )

    lines = ["🔄 <b>РОТАЦИЯ СЕКТОРОВ</b>", ""]
    for i, (sec, d) in enumerate(ranked[:5]):
        avg_rs = sum(d["rs"]) / len(d["rs"]) if d["rs"] else 1.0
        avg_ps = sum(d["pumps"]) / len(d["pumps"]) if d["pumps"] else 0
        medal  = ["🥇","🥈","🥉","4️⃣","5️⃣"][i]
        lines.append(
            f"{medal} <b>{sec}</b>  RS {avg_rs:+.2f}×  pump avg {avg_ps:.0f}  "
            f"→ {d['leader']}"
        )

    return "\n".join(lines)


def format_deep_dive(r: dict, signals: list, verdict: str,
                     bull: int, bear: int, plan: dict,
                     deposit_usd: Optional[float] = None,
                     risk_per_trade: float = 0.01) -> str:
    """Deep dive по одному символу."""
    sym   = r["symbol"]
    score = r["score"]
    price = _fmt_price(r["price"])
    grade = r.get("grade", "?")
    gem   = _grade_emoji(grade)
    setup = r.get("setup", "?")
    se    = _setup_emoji(setup)

    lines = [
        f"{se}{gem} <b>{sym}</b>  score={score}  [{grade}]",
        f"Итог: <b>{verdict}</b>  ({bull}🟢 / {bear}🔴)",
        f"Цена: <code>{price}</code>",
        "",
    ]

    # Торговый план
    if plan.get("side") != "wait":
        el  = plan.get("entry_low",  0)
        eh  = plan.get("entry_high", 0)
        st  = _fmt_price(plan.get("stop", 0))
        t1  = _fmt_price(plan.get("tp1", 0))
        t2  = _fmt_price(plan.get("tp2", 0))
        rr  = plan.get("rr", 0)
        # Если вход точечный (цена уже в зоне) — показываем "СЕЙЧАС", иначе диапазон
        if abs(el - eh) / max(eh, 1e-9) < 0.001:
            entry_str = f"СЕЙЧАС  <code>{_fmt_price(eh)}</code>"
        else:
            entry_str = f"<code>{_fmt_price(el)} .. {_fmt_price(eh)}</code>"
        plan_lines = [
            f"📌 <b>ПЛАН</b>",
            f"   Entry: {entry_str}",
            f"   Stop:  <code>{st}</code>",
            f"   TP1:   <code>{t1}</code>  TP2: <code>{t2}</code>",
            f"   R:R: <b>{rr:.2f}</b>",
        ]
        # Размер позиции — grade-based риск (AVEVA-57)
        if deposit_usd and r.get("price", 0) > 0:
            sl_dist = abs(plan.get("entry_low", r["price"]) - plan.get("stop", r["price"]))
            if sl_dist > 0:
                _g_risk   = _grade_risk(grade)
                risk_usd  = deposit_usd * _g_risk
                qty       = risk_usd / sl_dist
                pos_size  = qty * r["price"]
                lev_approx = round(pos_size / deposit_usd, 1)
                plan_lines.append(
                    f"   💰 Размер [{grade}, риск {_g_risk*100:.2g}%]: <b>{qty:.4g}</b> конт  "
                    f"(≈ ${pos_size:.0f}  |  ~{lev_approx}×)"
                )
        plan_lines.append("")
        lines += plan_lines

    # Топ сигналы
    LONG_SIGNALS  = [s for s in signals if s[2] == "ЛОНГ"][:5]
    SHORT_SIGNALS = [s for s in signals if s[2] == "ШОРТ"][:3]

    if LONG_SIGNALS:
        lines.append("🟢 <b>Бычьи сигналы</b>")
        for metric, val, _, expl in LONG_SIGNALS:
            short_expl = expl[:70] + "…" if len(expl) > 70 else expl
            lines.append(f"  ▲ <b>{_esc(metric)}</b>: {_esc(val)}")
            lines.append(f"    <i>{_esc(short_expl)}</i>")

    if SHORT_SIGNALS:
        lines.append("")
        lines.append("🔴 <b>Медвежьи сигналы</b>")
        for metric, val, _, expl in SHORT_SIGNALS:
            short_expl = expl[:70] + "…" if len(expl) > 70 else expl
            lines.append(f"  ▼ <b>{_esc(metric)}</b>: {_esc(val)}")
            lines.append(f"    <i>{_esc(short_expl)}</i>")

    # Подтверждение из Telegram каналов
    conf     = r.get("channel_conf", [])
    conflict = r.get("channel_conflict", [])
    ch_adj  = r.get("channel_score_adj", 0)
    acc_map = r.get("channel_acc_map", {})
    if conf:
        src = "  ".join(
            f"@{c}({acc_map[c]}%)" if c in acc_map else f"@{c}"
            for c in conf[:3]
        )
        adj_str = f"  [score {ch_adj:+d}]" if ch_adj else ""
        lines.append(f"📡 Канал подтверждает: <b>{_esc(src)}</b>{_esc(adj_str)}")
    elif conflict:
        src = "  ".join(f"@{c}" for c in conflict[:2])
        lines.append(f"📡 <i>Канал против: {_esc(src)}</i>")

    # Влияние новостей на сетап
    news_conf  = r.get("news_confirms", [])
    news_risk  = r.get("news_risks", [])
    delta      = r.get("news_score_delta", 0)
    if news_conf:
        best = news_conf[0]
        lines.append(f"📰 <b>{_esc(best['icon'])} {_esc(best['label'])}:</b> <i>{_esc(best['reason'])}</i>")
    if news_risk:
        lines.append(f"⚠️ <b>Риск от новостей:</b> <i>{_esc(news_risk[0]['reason'])}</i>")
    if delta != 0:
        sign = "+" if delta > 0 else ""
        lines.append(f"📰 <b>Score скорректирован:</b> {sign}{delta} (от новостей)")

    lines.append("")
    lines.append(f"⛔ Инвалидация: <i>{plan.get('invalidation', '—')}</i>")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Топ сетапов по убеждённости
# ─────────────────────────────────────────────────────────────

def _conviction_score(r: dict, fg_value: Optional[int]) -> tuple:
    """
    Считает убеждённость в реализации сетапа (0-95%).

    Ключевой принцип: награждает РАЗНООБРАЗИЕ независимых сигналов.
    8 сигналов из разных категорий > 1 очень сильного сигнала.
    Категории: HTF тренд / MTF зоны / структурный слом / объём /
               RSI / кросс-биржа / рынок / специфика сетапа.

    Возвращает (pct: int, reasons: list[str]).
    """
    pts = 0
    reasons: list[str] = []
    setup     = r.get("setup", "")
    bull_side = setup in ("squeeze", "breakout")

    # ── 1. HTF тренд согласован (Daily + 4H) ─────────────────── max 15
    d_htf  = r.get("d_htf", "")
    h4_htf = r.get("h4_htf", "")
    if bull_side:
        if d_htf == "bull" and h4_htf == "bull":
            pts += 15; reasons.append("D+4H↑")
        elif d_htf == "bull" or h4_htf == "bull":
            pts += 7
    else:
        if d_htf == "bear" and h4_htf == "bear":
            pts += 15; reasons.append("D+4H↓")
        elif d_htf == "bear" or h4_htf == "bear":
            pts += 7

    # ── 2. MTF Confluence: 1H+4H+1D зоны ────────────────────── max 18
    mtf = r.get("bull_mtf_ext", 0) if bull_side else r.get("bear_mtf_ext", 0)
    if mtf >= 5:
        pts += 18; reasons.append(f"MTF {mtf} зон")
    elif mtf >= 4:
        pts += 15; reasons.append(f"MTF {mtf} зоны")
    elif mtf >= 3:
        pts += 11; reasons.append(f"MTF {mtf} зоны")
    elif mtf >= 2:
        pts += 7;  reasons.append(f"MTF {mtf} зоны")
    elif mtf == 1:
        pts += 3

    # ── 3. Цена В структурной зоне прямо сейчас ─────────────── max 12
    in_zone = (r.get("in_bfvg") or r.get("in_bob")) if bull_side else (r.get("in_sfvg") or r.get("in_sob"))
    if in_zone:
        pts += 12; reasons.append("в зоне сейчас")

    # ── 4. CHoCH 1H: подтверждённый слом структуры ──────────── max 12
    choch = r.get("choch_1h", "—")
    if bull_side and choch == "bull_choch":
        pts += 12; reasons.append("CHoCH↑ 1H")
    elif not bull_side and choch == "bear_choch":
        pts += 12; reasons.append("CHoCH↓ 1H")

    # ── 5. Кросс-биржевое подтверждение Binance ─────────────── max 13
    bnb_bonus = r.get("bnb_cross_bonus") or 0
    if bnb_bonus >= 13:
        pts += 13; reasons.append("Binance согласен")
    elif bnb_bonus >= 5:
        pts += 7;  reasons.append("Binance частично")

    # ── 6. RSI: в правильной зоне для сетапа ─────────────────── max 8
    rsi = r.get("rsi_1h")
    if rsi is not None:
        if bull_side and rsi <= 35:
            pts += 8; reasons.append(f"RSI {rsi:.0f}")
        elif bull_side and rsi <= 45:
            pts += 4
        elif not bull_side and rsi >= 65:
            pts += 8; reasons.append(f"RSI {rsi:.0f}")
        elif not bull_side and rsi >= 55:
            pts += 4

    # ── 7. RSI дивергенция ───────────────────────────────────── max 7
    rsi_div = r.get("rsi_div_1h", "—") or "—"
    if bull_side and rsi_div in ("bull_div", "hidden_bull"):
        pts += 7; reasons.append(f"RSI {rsi_div.replace('_', ' ')}")
    elif not bull_side and rsi_div in ("bear_div", "hidden_bear"):
        pts += 7; reasons.append(f"RSI {rsi_div.replace('_', ' ')}")

    # ── 8. EMA структура ─────────────────────────────────────── max 7
    ema = r.get("ema_1h") or {}
    if bull_side:
        if ema.get("golden_cross"):
            pts += 7; reasons.append("GX! 1H")
        elif ema.get("ema_bull"):
            pts += 4
    else:
        if ema.get("death_cross"):
            pts += 7; reasons.append("DC! 1H")
        elif ema.get("ema_bear"):
            pts += 4

    # ── 9. CVD + объём согласованы ───────────────────────────── max 8
    cvd_k = r.get("cvd_k%") or 0
    cvd_t = r.get("cvd_t%") or 0
    vol_x = r.get("vol_x") or 1.0
    if bull_side:
        if cvd_k > 15 and cvd_t > 10:
            pts += 8; reasons.append("CVD↑ оба")
        elif cvd_k > 15 or vol_x > 2.0:
            pts += 4
    else:
        if cvd_k < -15 and cvd_t < -10:
            pts += 8; reasons.append("CVD↓ оба")
        elif cvd_k < -15 or vol_x > 2.0:
            pts += 4

    # ── 10. Специфика сетапа ─────────────────────────────────── max 16
    fund    = r.get("fund_%") or 0.0
    oi_chg  = r.get("oi24h_%") or 0.0
    bnb_fund = r.get("bnb_fund") or 0.0

    if setup == "squeeze":
        # Главное топливо сквиза — отрицательный funding с обеих бирж
        if fund < -0.05 and bnb_fund < -0.05:
            pts += 12; reasons.append(f"Fund BY{fund:+.3f}%+BN{bnb_fund:+.3f}%")
        elif fund < -0.01:
            pts += 7
        # OI упал = ликвидации прошли, путь расчищен
        if oi_chg < -10:
            pts += 8; reasons.append(f"OI{oi_chg:.0f}% (лики)")
        elif oi_chg < -5:
            pts += 4

    elif setup == "breakout":
        pump_pts = 0
        if (r.get("atr_comp") or 1.0) < 0.65:
            pump_pts += 5; reasons.append(f"ATR×{r['atr_comp']:.2f}")
        if r.get("oi_coiling"):
            pump_pts += 5; reasons.append("OI coil")
        whale = r.get("whale", "—") or "—"
        if whale != "—" and "Buy" in whale:
            pump_pts += 8; reasons.append(f"Кит {whale}")
        elif whale != "—":
            pump_pts += 4
        pts += min(pump_pts, 16)

    elif setup == "bos_fvg":
        if vol_x > 2.5:
            pts += 8; reasons.append(f"Vol ×{vol_x:.1f}")
        elif vol_x > 1.8:
            pts += 4
        if oi_chg > 10:
            pts += 8; reasons.append(f"OI+{oi_chg:.0f}%")
        elif oi_chg > 5:
            pts += 4

    elif setup == "range_sweep":
        sweep = r.get("sweep", "—") or "—"
        if sweep != "—":
            pts += 10; reasons.append(f"Sweep {sweep}")
        if in_zone:
            pts += 6   # уже засчитали выше, дополнительный вес для sweep

    # ── 11. Рыночный контекст (F&G) ──────────────────────────── max 10
    if fg_value is not None:
        if bull_side and fg_value <= 25:
            pts += 10; reasons.append(f"F&G={fg_value}")
        elif bull_side and fg_value <= 40:
            pts += 5
        elif not bull_side and fg_value >= 75:
            pts += 10; reasons.append(f"F&G={fg_value}")
        elif not bull_side and fg_value >= 60:
            pts += 5

    # ── 12. Ликвидации (подтверждение давления) ──────────────── max 8
    liq_short = r.get("liq_short_usd", 0) or 0
    liq_long  = r.get("liq_long_usd",  0) or 0
    if bull_side and liq_short >= 500_000:
        pts += 8; reasons.append(f"LIQ${liq_short/1e3:.0f}K↑")
    elif bull_side and liq_short >= 150_000:
        pts += 4
    elif not bull_side and liq_long >= 500_000:
        pts += 8; reasons.append(f"LIQ${liq_long/1e3:.0f}K↓")
    elif not bull_side and liq_long >= 150_000:
        pts += 4

    # Нормализация 0-95 (100% не существует в трейдинге)
    MAX_PTS = 15 + 18 + 12 + 12 + 13 + 8 + 7 + 7 + 8 + 16 + 10 + 8   # = 134
    pct = min(int(pts / MAX_PTS * 100), 95)

    return pct, reasons[:5]


def _build_top_setup_plan(r: dict, v: dict) -> Optional[dict]:
    """A1 (2026-06-08): финальный торговый план для actionable-алерта топ-сетапа.

    Уровни — Claude tp/sl (как на памп-пути): показанное == записанное в trades.json ==
    то, что сторожит trade_watcher. Направление — каноничный r['setup_dir'] (фикс 2026-05-30,
    `screener.py:4004`), а НЕ эвристика по имени сетапа (она расходилась на bos_fvg/range_sweep).
    GUARD (CLAUDE.md): дистанция стопа НЕ уже ATR×0.65 — защита от выноса свипом, даже если
    Claude вернул слишком узкий sl_pct (флэт-клэмп фильтра [1%..10%] пола по ATR не даёт).
    """
    price = r.get("price") or 0
    if not price:
        return None
    side = str(r.get("setup_dir") or "").lower()
    if side not in ("long", "short"):
        setup = r.get("setup") or r.get("best_setup") or ""
        side = "long" if setup in ("squeeze", "breakout") else "short"

    tp_pct = float(v.get("tp_pct") or 0)
    sl_pct = float(v.get("sl_pct") or 0)
    if tp_pct <= 0 or sl_pct <= 0:
        return None   # без Claude-уровней actionable-алерт не эмитим

    atr_pct  = r.get("atr_%") or 1.0
    atr_abs  = price * atr_pct / 100.0
    min_dist = max(atr_abs * 0.65, price * 0.002)        # GUARD: пол ширины стопа
    entry     = price                                    # mirror пампа: вход = текущая цена
    stop_dist = max(entry * sl_pct / 100.0, min_dist)    # Claude sl, но не уже ATR×0.65
    tp_dist   = entry * tp_pct / 100.0

    if side == "long":
        stop = max(entry - stop_dist, 0)
        tp1  = entry + tp_dist
        tp2  = entry + 2 * tp_dist
    else:
        stop = entry + stop_dist
        tp1  = max(entry - tp_dist, 0)
        tp2  = max(entry - 2 * tp_dist, 0)
    rr = (tp_dist / stop_dist) if stop_dist > 0 else 0.0

    plan = {
        "side": side, "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2, "rr": rr,
        # tp/sl получены из Claude (tp_pct/sl_pct>0, см. выше) → уровни Claude.
        # (stop мог быть заклэмплен GUARD ATR×0.65, но база — Claude-план.)
        "levels_source":     "claude",
        "claude_reasoning":  v.get("reasoning", ""),
        "claude_confidence": v.get("confidence", 0),
        "claude_risks":      v.get("risks", []) or [],
    }
    if v.get("action") == "WAIT" and v.get("macro_veto"):
        plan["macro_veto_note"] = (v.get("macro_veto_reason") or v.get("reasoning") or "")[:160]
    return plan


def send_top_setup_alerts(
    filtered: list,
    fg_value: Optional[int] = None,
    cfg: Optional[dict] = None,
    top_n: int = 5,
) -> int:
    """A1 (2026-06-08): топ-сетапы скринера → ОТДЕЛЬНЫЕ кнопочные алерты [Вошёл/Пропустил].

    Замыкает петлю учёта для главного (скринерного) потока: раньше топ уходил бесконтактным
    текст-блоком, кнопки были ТОЛЬКО на памп/раг → trades.json по скринеру = 0. Теперь каждый
    actionable кандидат (GO ИЛИ macro-veto WAIT — как памп-путь) шлётся своим сообщением через
    send_signal_alert(verdict=...): фильтр прогоняется ОДИН раз здесь (source=top_setups —
    тот же cooldown-бакет/лимит 5-в-день), повторно Claude НЕ зовётся.

    Побочно чинит лик бюджета: macro-veto WAIT помечался sendable в фильтре (ел дневной лимит),
    но GO-only текст-блок его НЕ доставлял. Теперь маркировка == доставка.

    Дублей нет: топ больше НЕ идёт в батч-текст send_report (watchlist/deep-dive — обзор по
    score, оставлены как есть). Возвращает число отправленных кнопочных алертов.
    """
    if not filtered:
        return 0
    if cfg is None:
        cfg = load_config()
    if not cfg.get("bot_token") or not cfg.get("chat_id"):
        return 0

    rt_enabled = os.environ.get("CLAUDE_RT_FILTER", "on").lower() in ("on", "true", "1", "yes")
    if not rt_enabled:
        # actionable-петля требует вердикт (GO + Claude tp/sl). Без фильтра кнопки не шлём;
        # обзор по-прежнему виден в watchlist/deep-dive. Раньше тут показывался текст-топ.
        print("[Top-Setups] CLAUDE_RT_FILTER off — actionable-алерты не шлём (нужен вердикт)")
        return 0
    try:
        from claude_realtime_filter import filter_candidate
    except Exception as e:
        print(f"[Top-Setups] filter import failed: {e} — actionable-алерты не шлём")
        return 0

    scored = []
    for r in filtered:
        conv, _reasons = _conviction_score(r, fg_value)
        scored.append((conv, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    pre_filter = scored[:max(top_n * 3, 8)]   # берём шире — Claude отберёт

    sent = 0
    seen = 0
    for conv, r in pre_filter:
        if sent >= top_n:
            break
        cand = dict(r)
        cand["setup"] = r.get("setup") or r.get("best_setup") or "?"
        _sd = str(r.get("setup_dir") or "").lower()
        if _sd not in ("long", "short"):
            _sd = "long" if cand["setup"] in ("squeeze", "breakout") else "short"
        cand["direction"] = _sd.upper()   # каноничное направление (== плану и записи)
        cand.setdefault("funding",     r.get("fund_%") or r.get("funding"))
        cand.setdefault("oi_24h_pct",  r.get("oi24h_%"))
        cand.setdefault("rsi_1h",      r.get("rsi_1h"))
        cand.setdefault("vwap_dev",    r.get("vwap_dev"))
        cand.setdefault("rs_btc",      r.get("rs_btc"))
        cand.setdefault("cvd_pct",     r.get("cvd_k%") or r.get("cvd_t%"))
        try:
            v = filter_candidate(cand["symbol"], cand, source="top_setups")
        except Exception as e:
            print(f"[Top-Setups] filter error {cand.get('symbol')}: {e}")
            continue
        seen += 1
        act = v.get("action")
        if not (act == "GO" or (act == "WAIT" and v.get("macro_veto"))):
            continue   # fail-CLOSED: SKIP / plain-WAIT / FAIL_OPEN — не шлём
        plan = _build_top_setup_plan(r, v)
        if not plan:
            continue
        try:
            if send_signal_alert(r, plan, cfg=cfg, verdict=v):
                sent += 1
        except Exception as e:
            print(f"[Top-Setups] send error {r.get('symbol')}: {e}")
    print(f"[Top-Setups] actionable: отправлено {sent} (кнопки) из {seen} прошедших фильтр")
    return sent


# ─────────────────────────────────────────────────────────────
# Главная функция отправки
# ─────────────────────────────────────────────────────────────

def send_report(
    results: list,
    filtered: list,
    btc_chg_24h: float,
    session_info: dict,
    fg_value: Optional[int],
    fg_label: Optional[str],
    deep_dive_data: list = None,   # список (r, signals, verdict, bull, bear, plan)
    cfg: Optional[dict] = None,
):
    """
    Главный вызов — отправляет полный отчёт в Telegram.
    deep_dive_data — передаётся из run_screener для deep dive блоков.
    """
    if cfg is None:
        cfg = load_config()
    if not cfg.get("enabled") or not cfg.get("bot_token") or not cfg.get("chat_id"):
        return

    token        = cfg["bot_token"]
    min_sc       = cfg.get("min_score_alert", 50)
    min_ps       = cfg.get("min_pump_alert", 80)
    max_sym      = cfg.get("max_symbols_tg", 5)
    deposit_usd  = cfg.get("deposit_usd") or None
    risk_per_tr  = float(cfg.get("risk_per_trade", 0.01))

    # Макро-фильтр: если в окне [event-30min ; event+15min] — только snapshot
    # с предупреждением, без watchlist/deep-dive/pump. Управляется alert_macro_blackout.
    in_macro_blackout = False
    _macro_reason = ""
    if _FD_AVAILABLE and cfg.get("alert_macro_blackout", True):
        try:
            _st = _fd.macro_blackout_status(minutes_before=30, minutes_after=15)  # FIX 2026-05-30: fail-CLOSED + причина в канал
            in_macro_blackout = bool(_st.get("blocked"))
            _macro_reason = _st.get("reason", "")
        except Exception:
            in_macro_blackout = False

    # Все получатели: основной + дополнительные (группы и т.д.)
    all_targets: list[str] = [str(cfg["chat_id"])]
    for extra in cfg.get("extra_chat_ids", []):
        cid = str(extra).strip()
        if cid and cid not in all_targets:
            all_targets.append(cid)

    # ── Формируем все сообщения ОДИН РАЗ ─────────────────────────────────────
    messages: list[str] = []

    # FIX 2026-05-30: при блокировке объясняем ПОЧЕМУ в канал (чтобы при тишине не путаться)
    if in_macro_blackout and _macro_reason:
        messages.append(f"🚫 <b>Сигналы придержаны (макро-защита)</b>\n{_macro_reason}")

    if cfg.get("send_snapshot"):
        m = format_snapshot(results, filtered, btc_chg_24h, session_info, fg_value, fg_label)
        if m: messages.append(m)

    if cfg.get("send_sector") and not in_macro_blackout:
        m = format_sector_rotation(results)
        if m: messages.append(m)

    if cfg.get("send_watchlist") and not in_macro_blackout:
        m = format_watchlist(filtered, max_sym, min_sc, "long",
                             deposit_usd=deposit_usd, risk_per_trade=risk_per_tr)
        if m: messages.append(m)
        m = format_watchlist(filtered, max_sym, min_sc, "short",
                             deposit_usd=deposit_usd, risk_per_trade=risk_per_tr)
        if m: messages.append(m)

    if cfg.get("send_deep_dive") and deep_dive_data and not in_macro_blackout:
        for r, signals, verdict, bull, bear, plan in deep_dive_data:
            if r.get("score", 0) >= min_sc:
                m = format_deep_dive(r, signals, verdict, bull, bear, plan,
                                     deposit_usd=deposit_usd, risk_per_trade=risk_per_tr)
                if m: messages.append(m)

    if cfg.get("send_pump") and not in_macro_blackout:
        m = format_pump_section(results, min_ps, max_sym)
        if m: messages.append(m)

    # A1 (2026-06-08): топ-сетапы больше НЕ идут текст-блоком в батч — они уходят
    # ОТДЕЛЬНЫМИ кнопочными алертами (send_top_setup_alerts) ПОСЛЕ рассылки обзора,
    # чтобы (а) замкнуть петлю учёта по скринеру, (б) не дублировать монету в батче.
    # См. вызов в конце функции; macro-blackout по-прежнему душит и их.

    messages.append(f"✅ <b>Готово</b>  {datetime.now().strftime('%H:%M:%S')}")

    # ── Swing charts (СЕТАП 6): отдельная фото-карточка для каждого сигнала ──
    swing_charts: list[tuple[str, bytes]] = []   # [(caption, png_bytes)]
    swing_candidates = [r for r in filtered if r.get("setup") == "swing"]
    if swing_candidates:
        try:
            from swing_chart import generate_swing_chart
            for r in swing_candidates:
                sym = r.get("symbol", "?")
                print(f"[SwingChart] Генерирую H4 чарт для {sym}...")
                png = generate_swing_chart(sym, r)
                if png:
                    direction = r.get("swing_dir", "?")
                    phase     = r.get("swing_phase", "?")
                    score_v   = r.get("score", 0)
                    dir_tag   = "▲ ЛОНГ" if direction == "long" else "▼ ШОРТ"
                    phase_map = {
                        "trend_bull": "Тренд ↑", "trend_bear": "Тренд ↓",
                        "correction_bull": "Коррекция ↑", "correction_bear": "Коррекция ↓",
                        "range": "Рейндж",
                    }
                    caption = (
                        f"<b>{sym}</b>  {dir_tag}  ·  {phase_map.get(phase, phase)}\n"
                        f"СЕТАП 6 — Hadiukov Swing  |  score={score_v}"
                    )
                    swing_charts.append((caption, png))
                    print(f"[SwingChart] {sym}: OK ({len(png)//1024}KB)")
                else:
                    print(f"[SwingChart] {sym}: не удалось сгенерировать")
        except ImportError:
            print("[SwingChart] swing_chart.py не найден — пропускаем")
        except Exception as _e:
            print(f"[SwingChart] Ошибка: {_e}")

    # ── Рассылаем каждому получателю ─────────────────────────────────────────
    for chat_id in all_targets:
        for msg in messages:
            _send_long(token, chat_id, msg)
            time.sleep(0.5)
        # Swing charts отправляем после текстовых сообщений
        for caption, png in swing_charts:
            _send_photo(token, chat_id, png, caption)
            time.sleep(0.5)
        if len(all_targets) > 1:
            time.sleep(1.0)   # пауза между чатами

    # A1 (2026-06-08): actionable кнопочные алерты по топ-сетапам — ПОСЛЕ обзора.
    # send_signal_alert сам рассылает по всем chat'ам (свой цикл targets); идемпотентность
    # и cooldown (source=top_setups, 5/день) предотвращают дубли между 4-часовыми прогонами.
    # macro-blackout душит и их (как раньше душил текст-блок).
    if not in_macro_blackout:
        try:
            n_act = send_top_setup_alerts(filtered, fg_value, cfg=cfg, top_n=5)
            if n_act:
                print(f"[TG] top-setup actionable-алертов отправлено: {n_act}")
        except Exception as _tse:
            print(f"[TG] top-setup alerts error (обзор уже ушёл): {_tse}")


# ─────────────────────────────────────────────────────────────
# Настройка и тест
# ─────────────────────────────────────────────────────────────

def setup_wizard():
    print("\n" + "="*55)
    print("  TELEGRAM ALERTS SETUP")
    print("="*55)
    cfg = load_config()

    print("\n1. Создай бота через @BotFather → /newbot")
    print("2. Получи токен вида: 7123456789:AAHxxxxxxxx\n")

    token = input("  BOT_TOKEN: ").strip()
    if not token:
        print("Отменено.")
        return cfg

    # Проверка токена
    print("  Проверяю токен...")
    try:
        resp = requests.get(f"{TG_BASE}/bot{token}/getMe", timeout=8, proxies=_PROXIES)
        data = resp.json()
        if data.get("ok"):
            bot_name = data["result"]["username"]
            print(f"  ✓ Бот найден: @{bot_name}")
        else:
            print(f"  ✗ Токен неверный: {data.get('description')}")
            return cfg
    except Exception as e:
        print(f"  ✗ Ошибка: {e}")
        return cfg

    print("\n3. Напиши своему боту /start")
    print(f"4. Открой: https://api.telegram.org/bot{token[:10]}****/getUpdates")
    print("   Найди: \"chat\":{\"id\":XXXXXXX}\n")

    chat_id = input("  CHAT_ID (число): ").strip()
    if not chat_id:
        print("Отменено.")
        return cfg

    # Настройки фильтрации
    print("\n  Настройки фильтрации:")
    min_sc_str = input(f"  Мин. score для алертов [{cfg['min_score_alert']}]: ").strip()
    min_ps_str = input(f"  Мин. pump_score [{cfg['min_pump_alert']}]: ").strip()
    max_sym_str = input(f"  Макс. символов в watchlist [{cfg['max_symbols_tg']}]: ").strip()

    cfg["bot_token"]       = token
    cfg["chat_id"]         = chat_id
    cfg["min_score_alert"] = int(min_sc_str) if min_sc_str.isdigit() else cfg["min_score_alert"]
    cfg["min_pump_alert"]  = int(min_ps_str) if min_ps_str.isdigit() else cfg["min_pump_alert"]
    cfg["max_symbols_tg"]  = int(max_sym_str) if max_sym_str.isdigit() else cfg["max_symbols_tg"]
    cfg["enabled"]         = True

    save_config(cfg)
    print("\n  Конфигурация сохранена.")

    # Тест
    print("  Отправляю тестовое сообщение...")
    ok = _send(token, chat_id,
               "✅ <b>Screener подключён!</b>\n\nБуду присылать сигналы после каждого прогона.",
               "HTML")
    if ok:
        print("  ✓ Тест прошёл! Сообщение отправлено.")
    else:
        print("  ✗ Не удалось отправить. Проверь CHAT_ID.")

    print("="*55)
    return cfg


def send_test(cfg: Optional[dict] = None):
    if cfg is None:
        cfg = load_config()
    if not cfg.get("bot_token") or not cfg.get("chat_id"):
        print("[TG] Не настроено. Запусти: python3 telegram_alerts.py setup")
        return False

    text = (
        "🧪 <b>ТЕСТОВЫЙ СИГНАЛ</b>\n\n"
        "🚀🟢 <b>BTCUSDT</b>  score=92  [A]\n"
        "Итог: <b>ЛОНГ ↑</b>  (8🟢 / 3🔴)\n"
        "Цена: <code>84000.00</code>\n\n"
        "📌 <b>ПЛАН</b>\n"
        "   Entry: <code>83500 .. 84000</code>\n"
        "   Stop:  <code>82100</code>\n"
        "   TP1:   <code>86200</code>\n"
        "   R:R: <b>2.14</b>\n\n"
        "✅ Telegram интеграция работает!"
    )
    ok = _send(cfg["bot_token"], str(cfg["chat_id"]), text)
    if ok:
        print("[TG] Тест отправлен успешно.")
    else:
        print("[TG] Ошибка отправки. Проверь настройки.")
    return ok


def _print_status(cfg: dict):
    print("\n  TELEGRAM CONFIG")
    print(f"  Enabled:       {cfg.get('enabled', False)}")
    token = cfg.get("bot_token") or ""
    print(f"  Bot token:     {token[:10]}***" if token else "  Bot token:     (не задан)")
    print(f"  Chat ID:       {cfg.get('chat_id') or '(не задан)'}")
    extras = cfg.get("extra_chat_ids", [])
    if extras:
        for i, eid in enumerate(extras):
            print(f"  Extra chat {i+1}: {eid}")
    else:
        print(f"  Extra chats:   (нет)")
    print(f"  Min score:     {cfg.get('min_score_alert')}")
    print(f"  Min pump:      {cfg.get('min_pump_alert')}")
    print(f"  Max symbols:   {cfg.get('max_symbols_tg')}")
    print(f"  Snapshot:      {cfg.get('send_snapshot')}")
    print(f"  Watchlist:     {cfg.get('send_watchlist')}")
    print(f"  Deep dive:     {cfg.get('send_deep_dive')}")
    print(f"  Pumps:         {cfg.get('send_pump')}")
    print(f"  Sectors:       {cfg.get('send_sector')}")
    print()


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "setup":
        setup_wizard()
    elif cmd == "test":
        send_test()
    elif cmd == "status":
        _print_status(load_config())
    elif cmd == "enable":
        cfg = load_config()
        cfg["enabled"] = True
        save_config(cfg)
        print("[TG] Включено.")
    elif cmd == "disable":
        cfg = load_config()
        cfg["enabled"] = False
        save_config(cfg)
        print("[TG] Отключено.")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

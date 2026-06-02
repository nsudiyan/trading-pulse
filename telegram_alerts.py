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
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

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


def _send(token: str, chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
    """Отправляет одно сообщение. Возвращает True при успехе."""
    try:
        resp = requests.post(
            f"{TG_BASE}/bot{token}/sendMessage",
            json={
                "chat_id":    chat_id,
                "text":       text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=10,
            proxies=_PROXIES,
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"[TG] API error: {data.get('description')} | text[:80]={text[:80]!r}")
        return data.get("ok", False)
    except Exception as e:
        print(f"[TG] Ошибка отправки: {e}")
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
        print(f"[TG] Ошибка отправки фото: {e}")
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


def send_signal_alert(r: dict, plan: dict, cfg: Optional[dict] = None) -> bool:
    """Немедленный одиночный алерт о новом сигнале (без батча)."""
    if cfg is None:
        cfg = load_config()
    token = cfg.get("bot_token", "")
    chat_id = str(cfg.get("chat_id", ""))
    if not token or not chat_id:
        return False

    # Claude RT-фильтр: либо одобряет с (опционально) новыми TP/SL, либо блокирует
    _allow, _claude = _apply_claude_filter(r, plan, source="screener")
    if not _allow:
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

    text = "\n".join(lines)

    all_targets = [chat_id] + [str(c) for c in cfg.get("extra_chat_ids", []) if str(c) != chat_id]
    ok = True
    for cid in all_targets:
        ok = _send(token, cid, text) and ok
        time.sleep(0.3)
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
    """Риск на сделку по грейду (AVEVA-57). A+=2.5%, A=1.5%, B=0.75%, rest=1%."""
    return {"A+": 0.025, "A": 0.015, "B": 0.0075}.get(grade, 0.01)


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
                      if r.get("setup") in ("bos_fvg", "range_sweep")
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
    SECTOR_MAP = {
        "L1":     ["SOLUSDT","AVAXUSDT","TONUSDT","NEARUSDT","APTUSDT","SUIUSDT","SEIUSDT",
                   "MOVEUSDT","BERAAUSDT","MONADUSDT"],
        "DeFi":   ["AAVEUSDT","CRVUSDT","MKRUSDT","UNIUSDT","SNXUSDT","COMPUSDT",
                   "JUPUSDT","PENDLEUSDT","EIGENUSDT"],
        "AI":     ["FETUSDT","RENDERUSDT","WLDUSDT","AGIXUSDT","TAOBYBIT","TAOUSDT",
                   "AIUSDT","VIRTUSDT","ACTUSDT","CHESHIREUSDT"],
        "Meme":   ["DOGEUSDT","SHIBUSDT","PEPEUSDT","FLOKIUSDT","BONKUSDT",
                   "1000PEPEUSDT","SHIB1000USDT","WIFUSDT","POPCATUSDT",
                   "MOODENGUSDT","GOATUSDT","BRETTUSDT","NEIROCTOBYBIT"],
        "L2":     ["ARBUSDT","OPUSDT","MATICUSDT","STRKUSDT","SCROLLUSDT",
                   "ZKUSDT","WUSDT","PYTHUSD"],
        "RWA":    ["ONDOUSDT","CFGUSDT","POLIXUSDT","REALUSDT",
                   "OPENUSDT","POLYXUSDT"],
        "DePIN":  ["IOUSDT","HIVEUSDT","ALUSDT","XNETUSDT"],
        "Perp":   ["HYPEUSDT","DYDXUSDT","GMXUSDT","SNSUSDT"],
        "LST":    ["ENAUSDT","ETHFIUSDT","RETHUSDT","SFRXETHUSDT"],
        "GameFi": ["AXSUSDT","SANDUSDT","GALAUSDT","IMXUSDT","BEAMUSDT","RONUSDT"],
        "ETH":    ["ETHUSDT","STETHUSDT"],
        "BTC":    ["BTCUSDT"],
    }

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


def format_top_setups(
    filtered: list,
    fg_value: Optional[int] = None,
    top_n: int = 5,
) -> str:
    """
    Последнее сообщение отчёта: топ-N сетапов отсортированных по убеждённости.
    Убеждённость = разнообразие независимых сигналов, а не величина одного.

    После сортировки прогоняем кандидатов через Claude RT-фильтр и оставляем
    только GO. SKIP/WAIT не показываются, чтобы не засорять личку шумом.
    """
    if not filtered:
        return ""

    scored = []
    for r in filtered:
        conv, reasons = _conviction_score(r, fg_value)
        scored.append((conv, r, reasons))
    scored.sort(key=lambda x: x[0], reverse=True)
    pre_filter = scored[:max(top_n * 3, 8)]   # берём шире, чтобы Claude отобрал

    # Claude RT-фильтр массовая обработка топ-кандидатов
    rt_enabled = os.environ.get("CLAUDE_RT_FILTER", "on").lower() in ("on", "true", "1", "yes")
    top: list = []
    if rt_enabled:
        try:
            from claude_realtime_filter import filter_candidate
            for conv, r, reasons in pre_filter:
                cand = dict(r)
                cand["setup"] = r.get("setup") or r.get("best_setup") or "?"
                cand.setdefault("direction", "LONG" if cand["setup"] in ("squeeze","bos_fvg","breakout") else "SHORT")
                cand.setdefault("funding",     r.get("fund_%") or r.get("funding"))
                cand.setdefault("oi_24h_pct",  r.get("oi24h_%"))
                cand.setdefault("rsi_1h",      r.get("rsi_1h"))
                cand.setdefault("vwap_dev",    r.get("vwap_dev"))
                cand.setdefault("rs_btc",      r.get("rs_btc"))
                cand.setdefault("cvd_pct",     r.get("cvd_k%") or r.get("cvd_t%"))
                v = filter_candidate(cand["symbol"], cand, source="top_setups")
                if v.get("action") == "GO":   # fail-CLOSED: FAIL_OPEN не пропускаем
                    r["_claude_verdict"] = v
                    top.append((conv, r, reasons))
                if len(top) >= top_n:
                    break
            if not top:
                print(f"[Top-Setups] Claude отфильтровал все {len(pre_filter)} кандидатов — топ пуст")
                return ""
            print(f"[Top-Setups] Claude пропустил {len(top)} из {len(pre_filter)} кандидатов")
        except Exception as e:
            print(f"[Top-Setups] RT filter error: {e} — fail-open")
            top = pre_filter[:top_n]
    else:
        top = pre_filter[:top_n]

    SETUP_NAME = {
        "squeeze":     "Сквиз",
        "bos_fvg":     "BOS/FVG",
        "range_sweep": "Sweep",
        "breakout":    "Pre-Pump",
    }
    SETUP_ICON = {
        "squeeze":     "⚡",
        "bos_fvg":     "📐",
        "range_sweep": "↔️",
        "breakout":    "🚀",
    }

    lines = ["🎯 <b>ТОП СЕТАПОВ — убеждённость в реализации</b>", ""]

    for i, (conv, r, reasons) in enumerate(top, 1):
        sym   = r["symbol"]
        score = r.get("score", 0)
        setup = r.get("setup", "")
        price = r["price"]
        bull  = setup in ("squeeze", "breakout")

        # Визуальный бар
        filled = conv // 10
        bar    = "█" * filled + "░" * (10 - filled)

        # Торговый план
        atr_pct = (r.get("atr_%") or 1.0)
        atr_abs = price * atr_pct / 100
        buf     = max(atr_abs * 0.65, price * 0.002)

        if bull:
            bfvg_top, bfvg_bot, _ = r.get("lvl_bfvg") or (None, None, None)
            entry = bfvg_bot or price
            stop  = max(entry - buf, 0)
            sfvg_top, sfvg_bot, _ = r.get("lvl_sfvg") or (None, None, None)
            tp1   = sfvg_top if (sfvg_top and sfvg_top > price) else price + atr_abs * 1.8
            tp2   = entry + 2 * (tp1 - entry)
            side_icon = "🟢"; side_txt = "ЛОНГ"
        else:
            sfvg_top, sfvg_bot, _ = r.get("lvl_sfvg") or (None, None, None)
            entry = sfvg_top or price
            stop  = entry + buf
            bfvg_top, bfvg_bot, _ = r.get("lvl_bfvg") or (None, None, None)
            tp1   = bfvg_bot if (bfvg_bot and bfvg_bot < price) else price - atr_abs * 1.8
            tp2   = max(entry - 2 * (entry - tp1), 0)
            side_icon = "🔴"; side_txt = "ШОРТ"

        risk_dist = abs(entry - stop)
        if risk_dist > 0:
            rr       = abs(tp1 - entry) / risk_dist
            risk_pct = risk_dist / entry * 100 if entry > 0 else 0
        else:
            rr       = 0.0
            risk_pct = 0.0

        se       = SETUP_ICON.get(setup, "📊")
        sn       = SETUP_NAME.get(setup, setup)
        in_zone   = (r.get("in_bfvg") or r.get("in_bob")) if bull else (r.get("in_sfvg") or r.get("in_sob"))
        now_tag    = "  ⚡<b>СЕЙЧАС</b>" if in_zone else ""
        h24_tag    = "  📅<b>[24H HIGH CONVICTION]</b>" if r.get("tg_24h_hold") else ""
        choch_tag  = "  🔷<b>[CHoCH CONFIRMED]</b>" if r.get("choch_conviction") else ""
        golden_tag = "  <b>[GOLDEN]</b>" if r.get("golden") else ""

        lines += [
            f"{i}. {se}{side_icon} <b>{_esc(sym)}</b>  [{sn}]  score={score}{now_tag}{h24_tag}{choch_tag}{golden_tag}",
            f"   Убеждённость: <b>{conv}%</b>  {bar}",
            f"   Entry <code>{_fmt_price(entry)}</code>"
            f"  Stop <code>{_fmt_price(stop)}</code>"
            f"  ({risk_pct:.2f}%)",
            f"   TP1 <code>{_fmt_price(tp1)}</code>"
            f"  TP2 <code>{_fmt_price(tp2)}</code>"
            f"  R:R <b>{rr:.1f}</b>",
        ]
        if reasons:
            lines.append(f"   ✓ {' · '.join(reasons)}")
        # Claude reasoning если есть
        _cv = r.get("_claude_verdict") or {}
        if _cv.get("reasoning") and _cv.get("verdict") == "GO":
            lines.append(f"   🧠 conf={_cv.get('confidence',0):.0%}: {_esc(_cv['reasoning'])[:180]}")
        lines.append("")

    lines.append(
        "<i>Убеждённость = разнообразие независимых сигналов.\n"
        "Чем больше несвязанных категорий согласны — тем выше.\n"
        "Это не гарантия. Всегда ставь стоп.</i>"
    )
    return "\n".join(lines)


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

    # Топ сетапов — тоже не шлём в макро-окне (соблазн войти прямо перед релизом)
    if not in_macro_blackout:
        m = format_top_setups(filtered, fg_value, top_n=5)
        if m: messages.append(m)

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

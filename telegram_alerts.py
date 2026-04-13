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
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

# ─────────────────────────────────────────────────────────────
# Конфигурация
# ─────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "telegram_config.json"

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
}

TG_BASE = "https://api.telegram.org"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    return dict(DEFAULT_CONFIG)


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
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"[TG] API error: {data.get('description')} | text[:80]={text[:80]!r}")
        return data.get("ok", False)
    except Exception as e:
        print(f"[TG] Ошибка отправки: {e}")
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
        f"",
        f"<b>Сигналы</b>  {len(filtered)} из {len(results)} пар",
        f"🟢 Лонг: {longs}  🔴 Шорт: {shorts}",
    ]
    return "\n".join(lines)


def format_watchlist(filtered: list, max_symbols: int = 5,
                     min_score: int = 50, direction: str = "long") -> str:
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
        else:
            sfvg = r.get("lvl_sfvg") or (None, None, None)
            entry_h = sfvg[0] or r["price"]
            entry_l = sfvg[1] or r["price"]
            stop    = entry_h + atr_abs * 0.65
            tp1     = r["price"] - atr_abs * 1.5

        lines.append(
            f"{setu}{gem} <b>{sym}</b>  score={score}  [{grade}]"
        )
        lines.append(
            f"   Цена: <code>{price}</code>  |  "
            f"{'+'if chg>=0 else ''}{chg:.1f}%"
        )
        lines.append(
            f"   Fund: <b>{fund:+.3f}%</b>  OI24h: {oi:+.1f}%"
            + (f"  RSI: {rsi:.0f}" if rsi else "")
        )
        lines.append(
            f"   Entry: <code>{_fmt_price(entry_l)}</code>"
            f"  Stop: <code>{_fmt_price(stop)}</code>"
            f"  TP1: <code>{_fmt_price(tp1)}</code>"
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
        "L1":     ["SOLUSDT","AVAXUSDT","TONUSDT","NEARUSDT","APTUSDT","SUIUSDT","SEIUSDT"],
        "DeFi":   ["AAVEUSDT","CRVUSDT","MKRUSDT","UNIUSDT","SNXUSDT","COMPUSDT"],
        "AI":     ["FETUSDT","RENDERUSDT","WLDUSDT","AGIXUSDT","TAOBYBIT","TAOUSDT"],
        "Meme":   ["DOGEUSDT","SHIBUSDT","PEPEUSDT","FLOKIUSDT","BONKUSDT","1000PEPEUSDT","SHIB1000USDT"],
        "L2":     ["ARBUSDT","OPUSDT","MATICUSDT","STRKUSDT","SCROLLUSDT"],
        "RWA":    ["ONDOUSDT","CFGUSDT","POLIXUSDT","REALUSDT"],
        "GameFi": ["AXSUSDT","SANDUSDT","GALAUSDT","IMXUSDT","BEAMUSDT"],
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
                     bull: int, bear: int, plan: dict) -> str:
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
        el = _fmt_price(plan.get("entry_low", 0))
        eh = _fmt_price(plan.get("entry_high", 0))
        st = _fmt_price(plan.get("stop", 0))
        t1 = _fmt_price(plan.get("tp1", 0))
        t2 = _fmt_price(plan.get("tp2", 0))
        rr = plan.get("rr", 0)
        lines += [
            f"📌 <b>ПЛАН</b>",
            f"   Entry: <code>{el} .. {eh}</code>",
            f"   Stop:  <code>{st}</code>",
            f"   TP1:   <code>{t1}</code>  TP2: <code>{t2}</code>",
            f"   R:R: <b>{rr:.2f}</b>",
            "",
        ]

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

    # Нормализация 0-95 (100% не существует в трейдинге)
    MAX_PTS = 15 + 18 + 12 + 12 + 13 + 8 + 7 + 7 + 8 + 16 + 10   # = 126
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
    """
    if not filtered:
        return ""

    scored = []
    for r in filtered:
        conv, reasons = _conviction_score(r, fg_value)
        scored.append((conv, r, reasons))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_n]

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
            side_icon = "🟢"; side_txt = "ЛОНГ"
        else:
            sfvg_top, sfvg_bot, _ = r.get("lvl_sfvg") or (None, None, None)
            entry = sfvg_top or price
            stop  = entry + buf
            bfvg_top, bfvg_bot, _ = r.get("lvl_bfvg") or (None, None, None)
            tp1   = bfvg_bot if (bfvg_bot and bfvg_bot < price) else price - atr_abs * 1.8
            side_icon = "🔴"; side_txt = "ШОРТ"

        if stop != entry:
            rr = abs(tp1 - entry) / abs(stop - entry)
        else:
            rr = 0.0

        se       = SETUP_ICON.get(setup, "📊")
        sn       = SETUP_NAME.get(setup, setup)
        in_zone  = (r.get("in_bfvg") or r.get("in_bob")) if bull else (r.get("in_sfvg") or r.get("in_sob"))
        now_tag  = "  ⚡<b>СЕЙЧАС</b>" if in_zone else ""

        lines += [
            f"{i}. {se}{side_icon} <b>{_esc(sym)}</b>  [{sn}]  score={score}{now_tag}",
            f"   Убеждённость: <b>{conv}%</b>  {bar}",
            f"   Entry <code>{_fmt_price(entry)}</code>"
            f"  Stop <code>{_fmt_price(stop)}</code>"
            f"  TP1 <code>{_fmt_price(tp1)}</code>"
            f"  R:R <b>{rr:.1f}</b>",
        ]
        if reasons:
            lines.append(f"   ✓ {' · '.join(reasons)}")
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

    token   = cfg["bot_token"]
    min_sc  = cfg.get("min_score_alert", 50)
    min_ps  = cfg.get("min_pump_alert", 80)
    max_sym = cfg.get("max_symbols_tg", 5)

    # Все получатели: основной + дополнительные (группы и т.д.)
    all_targets: list[str] = [str(cfg["chat_id"])]
    for extra in cfg.get("extra_chat_ids", []):
        cid = str(extra).strip()
        if cid and cid not in all_targets:
            all_targets.append(cid)

    # ── Формируем все сообщения ОДИН РАЗ ─────────────────────────────────────
    messages: list[str] = []

    if cfg.get("send_snapshot"):
        m = format_snapshot(results, filtered, btc_chg_24h, session_info, fg_value, fg_label)
        if m: messages.append(m)

    if cfg.get("send_sector"):
        m = format_sector_rotation(results)
        if m: messages.append(m)

    if cfg.get("send_watchlist"):
        m = format_watchlist(filtered, max_sym, min_sc, "long")
        if m: messages.append(m)
        m = format_watchlist(filtered, max_sym, min_sc, "short")
        if m: messages.append(m)

    if cfg.get("send_deep_dive") and deep_dive_data:
        for r, signals, verdict, bull, bear, plan in deep_dive_data:
            if r.get("score", 0) >= min_sc:
                m = format_deep_dive(r, signals, verdict, bull, bear, plan)
                if m: messages.append(m)

    if cfg.get("send_pump"):
        m = format_pump_section(results, min_ps, max_sym)
        if m: messages.append(m)

    # Топ сетапов по убеждённости — итоговый блок
    m = format_top_setups(filtered, fg_value, top_n=5)
    if m: messages.append(m)

    messages.append(f"✅ <b>Готово</b>  {datetime.now().strftime('%H:%M:%S')}")

    # ── Рассылаем каждому получателю ─────────────────────────────────────────
    for chat_id in all_targets:
        for msg in messages:
            _send_long(token, chat_id, msg)
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
        resp = requests.get(f"{TG_BASE}/bot{token}/getMe", timeout=8)
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

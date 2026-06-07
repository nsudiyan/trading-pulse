#!/usr/bin/env python3
"""
SCANNER ASSISTANT (2026-05-31) — ядро продукта "Вариант 1"
==========================================================
ЧЕСТНЫЙ продукт для аудитории: «Сканер волатильности + TA-ассистент».
НЕ торговые сигналы, НЕ финсовет, НЕ обещание прибыли.

Почему такой фрейминг (см. memory project_bot_strengths_2026-05-31):
- У бота НЕТ доказанного net-of-costs торгового эджа (все сетапы ~0/минус под
  честным path-resolved + beta-neutral + стресс-тестом по месяцам).
- НО сильная сторона РЕАЛЬНА: инфраструктура детекции (50+ метрик/скан) + TA + макро-режим.
- Поэтому продаём ИНСТРУМЕНТ ПОИСКА И ОБУЧЕНИЯ, а не «прибыльные сигналы».
  Это честно к аудитории и не сжигает репутацию.

Дизайн честности:
- НИКАКИХ BUY/SELL/LONG/SHORT/TP/SL директив. Только «волатильный кандидат + ПОЧЕМУ в списке + что это значит».
- Курирование ПО ЗАПРОСУ (/scan ЛИНЗА) вместо флуда (~58/день).
- Образовательные микро-пояснения метрик (funding/OI/ATR/CVD) — ценность даже без прибыли.
- Дисклеймер в онбординге и футером под каждым выводом.

Модуль = ЧИСТЫЕ функции, возвращающие HTML (тестируемы без Telegram). Ничего не пишет.
Интеграция позже: подключить handle() к аудиторному боту (отдельный токен) или в telegram_bot.py.
Self-test: python3 scanner_assistant.py  → печатает все экраны на реальных данных.
"""
import json, os, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "last_scan_cache.json"
MACRO = HERE / "outcomes" / "macro_snapshot.json"
BASE = "https://api.bybit.com"

DISCLAIMER = (
    "📊 <i>Это сканер волатильности и TA-ассистент — НЕ торговые сигналы и НЕ финсовет. "
    "Инструмент для поиска интересных движений и обучения. Прошлые данные не гарантируют будущего. "
    "Решения и риск — только твои. DYOR.</i>"
)

def _f(x):
    try: return float(x) if x not in (None, "") else None
    except Exception: return None

def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _pf(p):
    if p is None: return "?"
    p = float(p)
    return f"{p:.6g}" if p < 1 else (f"{p:,.4f}" if p < 100 else f"{p:,.2f}")

# ─────────────────────────── ОНБОРДИНГ / СПРАВКА ───────────────────────────
def onboarding() -> str:
    return (
        "<b>👋 Привет! Это Volatility Scanner &amp; TA Assistant</b>\n\n"
        "Я сканирую ~100 ликвидных крипто-фьючерсов Bybit в реальном времени и показываю "
        "тебе <b>где сейчас движение</b> и <b>почему</b> — на 50+ метриках (волатильность, "
        "funding, open interest, объём, CVD, ликвидации, макро-режим).\n\n"
        "<b>Что я делаю:</b>\n"
        "• Нахожу аномалии (резкая волатильность, перекос funding, дивергенции OI)\n"
        "• Объясняю простым языком, что это значит\n"
        "• Даю быстрый TA-разбор любой монеты\n"
        "• Показываю режим рынка (страх/жадность, потоки стейблов)\n\n"
        "<b>Чего я НЕ делаю:</b>\n"
        "• ❌ Не даю сигналы «купи/продай»\n"
        "• ❌ Не обещаю прибыль\n"
        "• ❌ Не финансовый советник\n\n"
        "Я — твой радар и учитель, а решения за тобой.\n\n"
        "Жми /help — посмотреть команды.\n\n"
        + DISCLAIMER
    )

def help_text() -> str:
    return (
        "<b>🧭 Команды</b>\n\n"
        "<b>🔭 Сканер (живые данные, по запросу — без флуда):</b>\n"
        "/scan — топ по «заметности» (волатильность + движение сейчас)\n"
        "/scan vol — самые волатильные (размах 24ч)\n"
        "/scan now — что движется прямо сейчас (за 1ч)\n"
        "/scan move — самые большие движения за 24ч\n"
        "/scan funding — экстремумы funding\n"
        "/scan volume — самые активные (оборот)\n\n"
        "<b>📈 TA-ассистент:</b>\n"
        "/chart SYMBOL — быстрый теханализ (напр. /chart SOLUSDT)\n\n"
        "<b>🌍 Рынок:</b>\n"
        "/macro — режим рынка: страх/жадность, доминация BTC, потоки стейблов\n\n"
        "<b>📚 Обучение:</b>\n"
        "/learn funding | oi | atr | cvd — что значит метрика\n\n"
        "/help — эта справка\n\n"
        + DISCLAIMER
    )

# ─────────────────────────── ОБУЧЕНИЕ ───────────────────────────
_LEARN = {
    "funding": ("💸 <b>Funding rate</b> — плата между лонгами и шортами на перпетуалах (каждые 8ч). "
                "Положительный → лонги платят шортам (толпа в лонгах, перегрев вверх). Отрицательный → "
                "шорты платят лонгам (толпа в шортах). Экстремумы часто = переполненная сторона, риск выноса."),
    "oi": ("📊 <b>Open Interest (OI)</b> — сколько открытых контрактов всего. Растёт + цена растёт = новые "
           "деньги входят (тренд здоровый). Растёт + цена падает = новые шорты. Падает = позиции закрываются "
           "(возможен разворот/выдыхание движения)."),
    "atr": ("📏 <b>ATR%</b> — средний диапазон свечи в % к цене = мера волатильности. Высокий ATR% = монета "
            "быстро ходит (больше шанс и движения, и выноса стопа). Низкий = тихо/сжатие (иногда перед рывком)."),
    "cvd": ("🔀 <b>CVD</b> (Cumulative Volume Delta) — кто агрессивнее: покупатели (market-buy) или продавцы "
            "(market-sell). CVD растёт при падающей цене = скрытый спрос (поглощение). Падает при растущей = "
            "скрытое распределение. Дивергенция CVD vs цена = ранний намёк на разворот."),
}
def cmd_learn(arg: str) -> str:
    arg = (arg or "").strip().lower()
    if arg in _LEARN:
        return _LEARN[arg] + "\n\n" + DISCLAIMER
    return ("📚 <b>Обучение.</b> Доступно: /learn funding | oi | atr | cvd\n\n" + DISCLAIMER)

# ─────────────────────────── СКАНЕР (ЖИВЫЕ данные Bybit-тикеров) ───────────────────────────
# Считаем на лету при каждом /scan (свежо, отвязано от расписания торгового скринера).
_LENS = {
    "vol":     ("range24",  "🔥 Самые волатильные (размах хай-лоу 24ч)"),
    "funding": ("funding",  "💸 Экстремумы funding (перегретая сторона)"),
    "move":    ("move24",   "🚀 Самые большие движения за 24ч"),
    "now":     ("move1h",   "⚡ Движется прямо сейчас (за 1ч)"),
    "volume":  ("turnover", "📊 Самые активные (оборот 24ч)"),
}
_MIN_TURNOVER = 2_000_000   # отсекаем неликвид

def _live_tickers():
    """Живые тикеры Bybit с производными метриками. None при ошибке сети."""
    try:
        with urllib.request.urlopen(f"{BASE}/v5/market/tickers?category=linear", timeout=15) as r:
            d = json.load(r)["result"]["list"]
    except Exception:
        return None
    out = []
    for x in d:
        sym = x.get("symbol", "")
        last = _f(x.get("lastPrice")); turn = _f(x.get("turnover24h"))
        if not sym.endswith("USDT") or not last or not turn or turn < _MIN_TURNOVER:
            continue
        hi = _f(x.get("highPrice24h")); lo = _f(x.get("lowPrice24h")); p1h = _f(x.get("prevPrice1h"))
        out.append({
            "symbol": sym, "price": last, "turnover": turn,
            "range24": (hi - lo) / last * 100 if (hi and lo) else 0.0,
            "move24":  (_f(x.get("price24hPcnt")) or 0.0) * 100,
            "move1h":  (last / p1h - 1) * 100 if p1h else 0.0,
            "funding": (_f(x.get("fundingRate")) or 0.0) * 100,
        })
    return out

def cmd_scan(arg: str = "", n: int = 10) -> str:
    arg = (arg or "").strip().lower()
    cands = _live_tickers()
    if cands is None:
        return ("ℹ️ Биржа временно недоступна, попробуй через минуту.\n\n" + DISCLAIMER)
    if not cands:
        return ("ℹ️ Нет ликвидных пар сейчас.\n\n" + DISCLAIMER)

    if arg in _LENS:
        key, title = _LENS[arg]
        ranked = sorted(cands, key=lambda x: x["turnover"] if key == "turnover" else abs(x.get(key, 0)), reverse=True)
    else:
        title = "🔭 Топ по «заметности» (волатильность + движение сейчас)"
        ranked = sorted(cands, key=lambda x: x["range24"] + 2 * abs(x["move1h"]), reverse=True)

    lines = [f"<b>{title}</b>", f"<i>Живой срез · {len(cands)} ликвидных пар</i>", ""]
    for i, x in enumerate(ranked[:n], 1):
        bits = [f"24ч {x['move24']:+.1f}%", f"1ч {x['move1h']:+.1f}%",
                f"размах {x['range24']:.1f}%", f"fund {x['funding']:+.3f}%"]
        lines.append(f"{i}. <b>{_esc(x['symbol'])}</b>  <code>{_pf(x['price'])}</code>")
        lines.append(f"    {' · '.join(bits)}")
        lines.append(f"    <i>почему: {_why_live(x)}</i>")
        lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)

def _why_live(x):
    """Нейтральное объяснение, ПОЧЕМУ монета в списке — без директив купи/продай."""
    parts = []
    if abs(x["move1h"]) >= 3:      parts.append(f"резкое движение за 1ч ({x['move1h']:+.1f}%)")
    if x["range24"] >= 8:          parts.append("высокая волатильность")
    if abs(x["funding"]) >= 0.05:  parts.append(f"экстремальный funding ({'лонги перегреты' if x['funding'] > 0 else 'шорты перегреты'})")
    if abs(x["move24"]) >= 15:     parts.append(f"большое движение 24ч ({x['move24']:+.0f}%)")
    if not parts: parts.append("в топе по выбранной метрике")
    return ", ".join(parts[:3])

# ─────────────────────────── МАКРО-РЕЖИМ ───────────────────────────
def cmd_macro() -> str:
    if not MACRO.exists():
        return ("ℹ️ Макро-снапшот недоступен.\n\n" + DISCLAIMER)
    try:
        m = json.loads(MACRO.read_text(encoding="utf-8"))
    except Exception:
        return ("ℹ️ Не удалось прочитать макро-снапшот.\n\n" + DISCLAIMER)
    raw = m.get("raw", {})
    fng = raw.get("fng", {}); dom = raw.get("btc_dom", {}); stable = raw.get("stable", {})
    age_min = int((time.time() - (m.get("ts") or time.time())) / 60)
    L = ["<b>🌍 Режим рынка</b>", f"<i>обновлено {age_min} мин назад</i>", ""]
    if fng:
        v = fng.get("value"); lab = fng.get("label", ""); d = fng.get("delta", 0)
        L.append(f"😱 <b>Fear &amp; Greed:</b> {v}/100 — {_esc(lab)} ({d:+d} за сутки)")
    if dom:
        L.append(f"₿ <b>BTC доминация:</b> {dom.get('btc_dominance',0):.1f}%  ·  "
                 f"капа рынка {dom.get('mcap_change_24h',0):+.1f}% за 24ч")
    if stable:
        d24 = stable.get("total_delta_24h_usd", 0) / 1e6
        flow = "приток 🟢" if d24 > 0 else "отток 🔴"
        L.append(f"💵 <b>Стейблы 24ч:</b> {d24:+,.0f}M — {flow} (ликвидность {'входит' if d24>0 else 'выходит'})")
    L.append("")
    L.append(f"<i>Чтение: {_macro_read(fng, stable)}</i>")
    L.append("")
    L.append(DISCLAIMER)
    return "\n".join(L)

def _macro_read(fng, stable):
    """Описательное (не директивное) чтение режима."""
    parts = []
    v = (fng or {}).get("value")
    if v is not None:
        if v <= 25: parts.append("экстремальный страх (рынок осторожен/перепродан)")
        elif v >= 75: parts.append("экстремальная жадность (рынок перегрет)")
        else: parts.append("нейтральные настроения")
    d24 = (stable or {}).get("total_delta_24h_usd", 0)
    if d24 < -3e8: parts.append("стейблы утекают — ликвидность сжимается")
    elif d24 > 3e8: parts.append("стейблы притекают — топливо для роста")
    return "; ".join(parts) if parts else "данных недостаточно"

# ─────────────────────────── TA-АССИСТЕНТ ───────────────────────────
def _klines(sym, interval="60", limit=120):
    try:
        q = f"category=linear&symbol={sym}&interval={interval}&limit={limit}"
        with urllib.request.urlopen(f"{BASE}/v5/market/kline?{q}", timeout=15) as r:
            data = json.load(r)["result"]["list"]
        return list(reversed(data))  # oldest first
    except Exception:
        return None

def _ema(vals, p):
    if len(vals) < p: return None
    k = 2 / (p + 1); e = sum(vals[:p]) / p
    for v in vals[p:]: e = v * k + e * (1 - k)
    return e

def _rsi(closes, p=14):
    if len(closes) < p + 1: return None
    g = l = 0.0
    for i in range(-p, 0):
        d = closes[i] - closes[i-1]
        g += max(d, 0); l += max(-d, 0)
    if l == 0: return 100.0
    rs = (g/p) / (l/p)
    return 100 - 100/(1+rs)

def cmd_chart(symbol: str) -> str:
    sym = (symbol or "").strip().upper()
    if not sym: return ("Укажи символ: <code>/chart SOLUSDT</code>\n\n" + DISCLAIMER)
    if not sym.endswith("USDT"): sym += "USDT"
    kl = _klines(sym, "60", 120)
    if not kl or len(kl) < 30:
        return (f"ℹ️ Нет данных по <b>{_esc(sym)}</b> (проверь тикер).\n\n" + DISCLAIMER)
    highs = [float(c[2]) for c in kl]; lows = [float(c[3]) for c in kl]; closes = [float(c[4]) for c in kl]
    price = closes[-2]  # последняя ЗАКРЫТАЯ свеча (без look-ahead)
    ema20 = _ema(closes[:-1], 20); ema50 = _ema(closes[:-1], 50)
    rsi = _rsi(closes[:-1])
    hi48 = max(highs[-49:-1]); lo48 = min(lows[-49:-1])
    atr = sum(highs[i]-lows[i] for i in range(-15,-1)) / 14
    atr_pct = atr / price * 100 if price else 0
    # тренд
    if ema20 and ema50:
        trend = "восходящий 📈" if price > ema20 > ema50 else ("нисходящий 📉" if price < ema20 < ema50 else "боковик ↔️")
    else:
        trend = "неопределён"
    pos = (price - lo48) / (hi48 - lo48) * 100 if hi48 > lo48 else 50
    L = [f"<b>📈 TA: {_esc(sym)}</b>  <code>{_pf(price)}</code>", ""]
    L.append(f"Тренд (1ч): <b>{trend}</b>")
    if rsi is not None:
        rstate = "перекуплен" if rsi >= 70 else ("перепродан" if rsi <= 30 else "нейтрален")
        L.append(f"RSI: <b>{rsi:.0f}</b> ({rstate})")
    L.append(f"Волатильность ATR: <b>{atr_pct:.1f}%</b>")
    L.append(f"Позиция в 48ч-диапазоне: <b>{pos:.0f}%</b> (0=дно, 100=вершина)")
    L.append("")
    L.append(f"🔑 Уровни:")
    L.append(f"  Сопротивление (48ч хай): <code>{_pf(hi48)}</code>")
    L.append(f"  Поддержка (48ч лоу): <code>{_pf(lo48)}</code>")
    if ema20: L.append(f"  EMA20: <code>{_pf(ema20)}</code>")
    L.append("")
    L.append(f"<i>Чтение: {_chart_read(trend, rsi, pos)}</i>")
    L.append("")
    L.append(DISCLAIMER)
    return "\n".join(L)

def _chart_read(trend, rsi, pos):
    parts = [f"структура {trend.split()[0]}"]
    if rsi is not None and rsi >= 70: parts.append("RSI в зоне перекупленности (риск отката)")
    elif rsi is not None and rsi <= 30: parts.append("RSI в перепроданности (возможен отскок)")
    if pos >= 85: parts.append("у верха диапазона")
    elif pos <= 15: parts.append("у низа диапазона")
    return "; ".join(parts) + ". Это наблюдение, не рекомендация."

# ─────────────────────────── ДИСПЕТЧЕР ───────────────────────────
# ─────────────────────────── КАНАЛЬНЫЙ ДАЙДЖЕСТ ───────────────────────────
def cmd_digest(n: int = 5) -> str:
    """Дайджест для канала: макро + топ-муверы + funding-экстремумы. Честный фрейминг, без сигналов."""
    L = [f"📡 <b>СКАНЕР РЫНКА</b> · {time.strftime('%H:%M UTC', time.gmtime())}", ""]
    try:
        m = json.loads(MACRO.read_text(encoding="utf-8")).get("raw", {})
        fng = m.get("fng", {}); dom = m.get("btc_dom", {}); stb = m.get("stable", {})
        d24 = (stb.get("total_delta_24h_usd", 0) or 0) / 1e6
        L.append(f"🌍 Fear&amp;Greed <b>{fng.get('value','?')}</b> ({_esc(fng.get('label',''))}) · "
                 f"BTC дом {dom.get('btc_dominance',0):.0f}% · стейблы 24ч {d24:+,.0f}M")
        L.append("")
    except Exception:
        pass
    cands = _live_tickers()
    if cands:
        L.append("⚡ <b>Движется сейчас (за 1ч):</b>")
        for x in sorted(cands, key=lambda x: abs(x["move1h"]), reverse=True)[:n]:
            L.append(f"• <b>{_esc(x['symbol'])}</b>  1ч {x['move1h']:+.1f}% · 24ч {x['move24']:+.1f}% · "
                     f"размах {x['range24']:.0f}% · fund {x['funding']:+.2f}%")
        L.append("")
        L.append("💸 <b>Funding-экстремумы:</b>")
        for x in sorted(cands, key=lambda x: abs(x["funding"]), reverse=True)[:3]:
            L.append(f"• <b>{_esc(x['symbol'])}</b>  {x['funding']:+.3f}% "
                     f"({'лонги перегреты' if x['funding'] > 0 else 'шорты перегреты'})")
        L.append("")
    L.append(DISCLAIMER)
    return "\n".join(L)

# ─────────────────────────── ДИСПЕТЧЕР ───────────────────────────
def handle(text: str) -> str:
    text = (text or "").strip()
    parts = text.split()
    cmd = (parts[0].lower().split("@")[0]) if parts else ""
    arg = parts[1] if len(parts) > 1 else ""
    if cmd == "/start": return onboarding()
    if cmd == "/help":  return help_text()
    if cmd == "/scan":  return cmd_scan(arg)
    if cmd == "/macro": return cmd_macro()
    if cmd == "/chart": return cmd_chart(arg)
    if cmd == "/learn": return cmd_learn(arg)
    if cmd == "/digest": return cmd_digest()
    return ("❓ Не знаю такую команду. /help — список.\n\n" + DISCLAIMER)

# ─────────────────────────── SELF-TEST ───────────────────────────
if __name__ == "__main__":
    import sys
    a = sys.argv[1:]
    if "--digest" in a:
        msg = cmd_digest()
        if "--send" in a:
            i = a.index("--send")
            target = a[i + 1] if i + 1 < len(a) else "owner"   # owner=личка, channel=канал Апекс
            import telegram_alerts as _ta
            cfg = _ta.load_config(); tok = cfg.get("bot_token")
            chat = str(cfg.get("owner_chat_id") or cfg.get("chat_id")) if target == "owner" else str(cfg.get("chat_id"))
            ok = _ta._send(tok, chat, msg)
            print(f"[SEND→{target} chat={chat}] {'✅ OK' if ok else '❌ FAIL'}\n")
            print(msg)
        else:
            print(msg)
    elif a:
        print(handle(" ".join(a)))
    else:
        for t in ("/start", "/help", "/macro", "/scan now", "/learn funding", "/chart SOLUSDT"):
            print("="*70); print(f">>> {t}"); print("="*70)
            print(handle(t)); print()

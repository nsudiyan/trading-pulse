"""
chart_analyzer.py — Автоматический визуальный анализ топ-кандидатов скринера.

Поток:
  1. Берёт топ-N кандидатов из screener.run_screener()
  2. Для каждого: скачивает свежий OHLCV с Bybit, рисует PNG (mplfinance)
  3. Отправляет график + метрики в Claude Vision API
  4. Шлёт фото + AI-анализ в Telegram

Требования:
  pip install anthropic mplfinance pandas
  ANTHROPIC_API_KEY= в .env
"""

import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# ─── Optional imports ────────────────────────────────────────────────────────

try:
    import pandas as pd
    import matplotlib
    matplotlib.use('Agg')  # non-interactive backend: required for LaunchAgent/daemon
    import mplfinance as mpf
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    _CHART_OK = True
except (ImportError, Exception):
    _CHART_OK = False

try:
    import anthropic
    _ANTHROPIC_OK = True
except ImportError:
    _ANTHROPIC_OK = False

# ─── Paths & config ──────────────────────────────────────────────────────────

DIR        = Path(__file__).parent
CHARTS_DIR = DIR / "charts"
CHARTS_DIR.mkdir(exist_ok=True)

BYBIT_BASE = "https://api.bybit.com"
TG_BASE    = "https://api.telegram.org"


def _load_dotenv():
    p = DIR / ".env"
    if not p.exists():
        return
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip(); v = v.strip()
            if v and v[0] in ('"', "'") and v[-1] == v[0]:
                v = v[1:-1]
            os.environ.setdefault(k, v)


_load_dotenv()


# ─── Bybit OHLCV ─────────────────────────────────────────────────────────────

def _fetch_klines(symbol: str, interval: str = "60", limit: int = 80):
    """Возвращает (timestamps, opens, highs, lows, closes, volumes) или None."""
    try:
        r = requests.get(
            f"{BYBIT_BASE}/v5/market/kline",
            params={"category": "linear", "symbol": symbol,
                    "interval": interval, "limit": limit},
            timeout=10,
        )
        data = r.json()
        if data.get("retCode", -1) != 0:
            return None
        candles = list(reversed(data["result"]["list"]))
        ts  = [int(c[0])   for c in candles]
        o   = [float(c[1]) for c in candles]
        h   = [float(c[2]) for c in candles]
        lo  = [float(c[3]) for c in candles]
        cl  = [float(c[4]) for c in candles]
        vol = [float(c[5]) for c in candles]
        return ts, o, h, lo, cl, vol
    except Exception:
        return None


# ─── Chart generation ────────────────────────────────────────────────────────

_DARK_STYLE = {
    "base_mpl_style": "dark_background",
    "marketcolors": mpf.make_marketcolors(
        up="#26a69a", down="#ef5350",
        edge="inherit", wick="inherit",
        volume={"up": "#26a69a55", "down": "#ef535055"},
    ) if _CHART_OK else None,
    "rc": {
        "axes.facecolor":  "#131722",
        "figure.facecolor": "#131722",
        "axes.edgecolor":  "#2a2e39",
        "axes.labelcolor": "#b2b5be",
        "xtick.color":     "#b2b5be",
        "ytick.color":     "#b2b5be",
        "grid.color":      "#1e222d",
        "grid.linestyle":  "--",
        "grid.alpha":      0.5,
    },
}


def _price_fmt(p: float) -> str:
    if p >= 1000: return f"{p:.2f}"
    if p >= 10:   return f"{p:.3f}"
    if p >= 1:    return f"{p:.4f}"
    if p >= 0.01: return f"{p:.5f}"
    return f"{p:.8f}"


def generate_chart(symbol: str, metrics: dict) -> Optional[Path]:
    """
    Генерирует PNG-график для symbol.
    Возвращает Path к файлу или None при ошибке.
    """
    if not _CHART_OK:
        return None

    raw = _fetch_klines(symbol, interval="60", limit=80)
    if not raw:
        return None
    ts, o, h, lo, cl, vol = raw
    # Убираем последнюю незакрытую свечу
    ts, o, h, lo, cl, vol = ts[:-1], o[:-1], h[:-1], lo[:-1], cl[:-1], vol[:-1]

    # Строим DataFrame
    idx = pd.to_datetime(ts, unit="ms", utc=True).tz_convert("UTC")
    df  = pd.DataFrame(
        {"Open": o, "High": h, "Low": lo, "Close": cl, "Volume": vol},
        index=idx,
    )

    # Стиль
    style = mpf.make_mpf_style(
        base_mpl_style="dark_background",
        marketcolors=mpf.make_marketcolors(
            up="#26a69a", down="#ef5350",
            edge="inherit", wick="inherit",
            volume={"up": "#26a69a55", "down": "#ef535055"},
        ),
        rc=_DARK_STYLE["rc"],
    )

    # Дополнительные горизонтальные уровни
    hlines_prices = []
    hlines_colors = []

    # POC
    poc = metrics.get("poc")
    if poc:
        hlines_prices.append(poc)
        hlines_colors.append("#f0c040")

    # FVG 1H зоны
    for fvg in (metrics.get("fvg_1h") or []):
        mid = (fvg.get("high", 0) + fvg.get("low", 0)) / 2
        if mid > 0:
            hlines_prices.append(mid)
            hlines_colors.append("#4d88ff88")

    # OB 1H
    for ob in (metrics.get("ob_1h") or []):
        mid = (ob.get("high", 0) + ob.get("low", 0)) / 2
        if mid > 0:
            hlines_prices.append(mid)
            hlines_colors.append("#9c27b088")

    fname = CHARTS_DIR / f"{symbol}_1H_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.png"

    try:
        fig, axes = mpf.plot(
            df,
            type="candle",
            style=style,
            volume=True,
            title=f"\n{symbol}  1H  |  {datetime.now(timezone.utc).strftime('%d %b %Y %H:%M')} UTC",
            hlines=dict(hlines=hlines_prices, colors=hlines_colors, linewidths=1.0, linestyle="--")
                   if hlines_prices else dict(hlines=[], colors=[]),
            figsize=(14, 8),
            returnfig=True,
            warn_too_much_data=999,
            tight_layout=True,
        )

        # Легенда
        patches = []
        if poc:
            patches.append(mpatches.Patch(color="#f0c040", label=f"POC {_price_fmt(poc)}"))
        if patches:
            axes[0].legend(handles=patches, loc="upper left",
                           fontsize=8, framealpha=0.3, facecolor="#131722")

        fig.savefig(fname, dpi=150, bbox_inches="tight",
                    facecolor="#131722", edgecolor="none")
        plt.close(fig)
        return fname
    except Exception as e:
        print(f"[ChartAI] Ошибка генерации графика {symbol}: {e}")
        return None


# ─── Claude Vision analysis ──────────────────────────────────────────────────

_SYSTEM_PROMPT = """Ты — профессиональный трейдер деривативами, специалист по техническому анализу
крипто-фьючерсов. Анализируй ТОЛЬКО то что видишь на графике — объективно, без предположений.

Формат ответа СТРОГО:

📊 **ТРЕНД**: [Направление + сила одной строкой]

🎯 **УРОВНИ**:
• Сопротивление: [цена] — [почему важен]
• Поддержка: [цена] — [почему важен]

🕯 **ПАТТЕРН**: [Что формируется + стадия]

📐 **СЦЕНАРИИ**:
1. [Название] (X%) — [что нужно увидеть] → цель [цена], стоп [цена]
2. [Название] (Y%) — [что нужно увидеть] → цель [цена], стоп [цена]
3. [Название] (Z%) — [что нужно увидеть] → цель [цена], стоп [цена]

⚡ **ВЫВОД**: [1 предложение — торговый bias]

Не повторяй данные из метрик — они уже известны. Сосредоточься на том, что видно на графике."""


def _build_metrics_text(symbol: str, m: dict) -> str:
    """Форматирует метрики скринера для вставки в промпт."""
    lines = [f"## Метрики скринера для {symbol}\n"]

    score = m.get("score", 0)
    setup = m.get("setup", "?")
    grade = m.get("grade") or m.get("notes", "")[:3]
    lines.append(f"**Score**: {score}  |  **Setup**: {setup}  |  **Grade**: {grade}")

    price = m.get("price")
    atr   = m.get("atr_%")
    if price:
        lines.append(f"**Цена**: {_price_fmt(price)}  |  **ATR%**: {atr:.2f}%" if atr else f"**Цена**: {_price_fmt(price)}")

    fund = m.get("fund_%")
    oi24 = m.get("oi24h_%")
    if fund is not None:
        lines.append(f"**Funding**: {fund:+.4f}%  |  **OI 24h**: {oi24:+.1f}%" if oi24 is not None else f"**Funding**: {fund:+.4f}%")

    d_htf  = m.get("d_htf", "?")
    h4_htf = m.get("h4_htf", "?")
    lines.append(f"**HTF тренд**: Daily={d_htf}  4H={h4_htf}")

    cvd = m.get("cvd_k%")
    rsi = m.get("rsi_1h")
    if cvd is not None:
        lines.append(f"**CVD 1H**: {cvd:+.1f}%  |  **RSI 1H**: {rsi:.0f}" if rsi else f"**CVD 1H**: {cvd:+.1f}%")

    sweep   = m.get("sweep", "—")
    flags   = m.get("flags", "—")
    notes   = m.get("notes", "—")
    lines.append(f"**Sweep**: {sweep}  |  **Flags**: {flags}")
    lines.append(f"**Заметки**: {notes}")

    # Торговый план
    plan = m.get("_trade_plan")
    if plan and plan.get("side") != "wait":
        lines.append(f"\n**Торговый план** ({plan['side'].upper()}):")
        lines.append(f"  Entry: {_price_fmt(plan['entry_low'])} .. {_price_fmt(plan['entry_high'])}")
        lines.append(f"  Stop:  {_price_fmt(plan['stop'])}  |  R:R = {plan['rr']:.2f}")
        lines.append(f"  TP1:   {_price_fmt(plan['tp1'])}  |  TP2: {_price_fmt(plan['tp2'])}")

    # SL правила (из проекта: ATR×0.65 buf min, ATR×1.8 fallback для волатильных альтов)
    if atr and price:
        atr_val = price * atr / 100
        sl_buf  = price - atr_val * 0.65
        sl_wide = price - atr_val * 1.8
        lines.append(f"\n**SL ориентиры** (ATR={_price_fmt(atr_val)}):")
        lines.append(f"  Минимальный буфер: {_price_fmt(sl_buf)}  (ATR×0.65)")
        lines.append(f"  Защита от sweep:   {_price_fmt(sl_wide)}  (ATR×1.8)")

    return "\n".join(lines)


def rule_based_analysis(symbol: str, m: dict) -> str:
    """
    Генерирует структурированный анализ из метрик скринера — без API.
    Использует те же данные что уже посчитал screener.py.
    """
    lines = []
    p     = m.get("price", 0)
    atr_p = m.get("atr_%", 1.5)
    atr_v = p * atr_p / 100 if p else 0

    # ── Тренд ────────────────────────────────────────────────────────────────
    d_htf  = m.get("d_htf", "?")
    h4_htf = m.get("h4_htf", "?")
    trend_map = {"bull": "↑ аптренд", "bear": "↓ даунтренд", "side": "→ боковик"}
    d_str  = trend_map.get(d_htf,  d_htf)
    h4_str = trend_map.get(h4_htf, h4_htf)

    # Общий HTF bias
    if d_htf == "bull" and h4_htf == "bull":
        htf_bias = "бычий по обоим TF"
        bias_emoji = "🟢"
    elif d_htf == "bear" and h4_htf == "bear":
        htf_bias = "медвежий по обоим TF"
        bias_emoji = "🔴"
    elif d_htf == "bull":
        htf_bias = "бычий Daily, 4H в коррекции"
        bias_emoji = "🟡"
    elif d_htf == "bear":
        htf_bias = "медвежий Daily, 4H в отскоке"
        bias_emoji = "🟠"
    else:
        htf_bias = "нейтральный"
        bias_emoji = "⚪"

    lines.append(f"📊 <b>ТРЕНД</b>: {bias_emoji} Daily={d_str} | 4H={h4_str}")

    # ── Уровни ───────────────────────────────────────────────────────────────
    levels = []

    # FVG зоны
    for fvg in (m.get("fvg_1h") or []):
        lo, hi = fvg.get("low", 0), fvg.get("high", 0)
        if lo and hi:
            mid = (lo + hi) / 2
            tag = "FVG 1H"
            if mid > p:
                levels.append(("R", mid, f"{tag} {_price_fmt(lo)}–{_price_fmt(hi)}"))
            else:
                levels.append(("S", mid, f"{tag} {_price_fmt(lo)}–{_price_fmt(hi)}"))

    for fvg in (m.get("fvg_4h") or []):
        lo, hi = fvg.get("low", 0), fvg.get("high", 0)
        if lo and hi:
            mid = (lo + hi) / 2
            tag = "FVG 4H"
            if mid > p:
                levels.append(("R", mid, f"{tag} {_price_fmt(lo)}–{_price_fmt(hi)}"))
            else:
                levels.append(("S", mid, f"{tag} {_price_fmt(lo)}–{_price_fmt(hi)}"))

    # OB зоны
    for ob in (m.get("ob_1h") or []):
        lo, hi = ob.get("low", 0), ob.get("high", 0)
        if lo and hi:
            mid = (lo + hi) / 2
            if mid > p:
                levels.append(("R", mid, f"OB 1H {_price_fmt(lo)}–{_price_fmt(hi)}"))
            else:
                levels.append(("S", mid, f"OB 1H {_price_fmt(lo)}–{_price_fmt(hi)}"))

    # POC
    poc = m.get("poc")
    if poc:
        if poc > p:
            levels.append(("R", poc, f"POC {_price_fmt(poc)}"))
        else:
            levels.append(("S", poc, f"POC {_price_fmt(poc)}"))

    # ATR-based fallback levels
    if not any(t == "R" for t, *_ in levels):
        levels.append(("R", p + atr_v * 1.5, f"ATR×1.5 зона {_price_fmt(p + atr_v * 1.5)}"))
    if not any(t == "S" for t, *_ in levels):
        levels.append(("S", p - atr_v * 1.5, f"ATR×1.5 зона {_price_fmt(p - atr_v * 1.5)}"))

    # Ближайшие R и S
    resistances = sorted([(pr, lbl) for t, pr, lbl in levels if t == "R"], key=lambda x: x[0])
    supports    = sorted([(pr, lbl) for t, pr, lbl in levels if t == "S"], key=lambda x: -x[0])

    lvl_lines = ["🎯 <b>УРОВНИ</b>:"]
    for pr, lbl in resistances[:2]:
        lvl_lines.append(f"• Сопротивление: <b>{_price_fmt(pr)}</b> — {lbl}")
    for pr, lbl in supports[:2]:
        lvl_lines.append(f"• Поддержка: <b>{_price_fmt(pr)}</b> — {lbl}")
    lines.append("\n".join(lvl_lines))

    # ── Паттерн ──────────────────────────────────────────────────────────────
    setup      = m.get("setup", "")
    candle_pat = m.get("candle_pattern", "—")
    sweep      = m.get("sweep", "—")
    mtf_b      = m.get("mtf_b", 0)
    mtf_s      = m.get("mtf_s", 0)
    choch      = m.get("choch_1h", "—")

    setup_desc = {
        "squeeze":     "Ликвидационный сквиз — OI сброс + объём, возможный разворот",
        "bos_fvg":     "BOS + FVG/OB — слом структуры с импульсом, откат в зону",
        "range_sweep": "Рейндж Sweep — ложный пробой границы диапазона",
        "breakout":    "Breakout / Pre-Pump — накопление перед движением",
        "short_dist":  "Дистрибуция / Шорт-давление — распределение на хаях",
    }
    pat_str = setup_desc.get(setup, setup)
    extras = []
    if candle_pat and candle_pat != "—":
        extras.append(candle_pat)
    if sweep and sweep != "—":
        extras.append(f"sweep: {sweep}")
    if choch and choch not in ("—", None):
        extras.append(f"CHoCH: {choch}")
    if mtf_b or mtf_s:
        extras.append(f"MTF {mtf_b}↑{mtf_s}↓")
    if extras:
        pat_str += f" | {', '.join(extras)}"

    lines.append(f"🕯 <b>ПАТТЕРН</b>: {pat_str}")

    # ── Сигналы CVD / RSI / Funding ──────────────────────────────────────────
    cvd   = m.get("cvd_k%")
    rsi   = m.get("rsi_1h")
    fund  = m.get("fund_%", 0) or 0
    oi24  = m.get("oi24h_%", 0) or 0
    vol_x = m.get("vol_x", 1)

    sig_lines = ["📈 <b>СИГНАЛЫ</b>:"]
    if cvd is not None:
        cvd_txt = "покупки" if cvd > 5 else ("продажи" if cvd < -5 else "нейтрально")
        sig_lines.append(f"• CVD 1H: {cvd:+.1f}% ({cvd_txt})")
    if rsi is not None:
        rsi_txt = "перекуплен" if rsi > 70 else ("перепродан" if rsi < 30 else "нейтральный")
        sig_lines.append(f"• RSI 1H: {rsi:.0f} ({rsi_txt})")
    if fund != 0:
        fund_txt = "лонги переплачивают — шортовое давление" if fund > 0.02 else (
                   "шорты переплачивают — бычий контекст" if fund < -0.01 else "нейтральный")
        sig_lines.append(f"• Funding: {fund:+.4f}% ({fund_txt})")
    if oi24 != 0:
        oi_txt = "рост позиций" if oi24 > 3 else ("сброс позиций" if oi24 < -3 else "стабильно")
        sig_lines.append(f"• OI 24h: {oi24:+.1f}% ({oi_txt})")
    if vol_x and float(vol_x) > 1.5:
        sig_lines.append(f"• Объём: ×{vol_x} от медианы (аномальная активность)")
    lines.append("\n".join(sig_lines))

    # ── Сценарии (на базе торгового плана скринера) ───────────────────────────
    plan = m.get("_trade_plan")
    sc_lines = ["📐 <b>СЦЕНАРИИ</b>:"]

    if plan and plan.get("side") != "wait":
        side  = plan["side"]
        entry_lo = plan.get("entry_low", p)
        entry_hi = plan.get("entry_high", p)
        stop  = plan.get("stop", 0)
        tp1   = plan.get("tp1", 0)
        tp2   = plan.get("tp2", 0)
        rr    = plan.get("rr", 0)

        if side == "long":
            sc_lines.append(
                f"1. 🟢 Лонг от зоны ({_price_fmt(entry_lo)}–{_price_fmt(entry_hi)}) "
                f"→ TP1 <b>{_price_fmt(tp1)}</b> / TP2 <b>{_price_fmt(tp2)}</b> | "
                f"Стоп <b>{_price_fmt(stop)}</b> | R:R {rr:.1f}"
            )
            # Альтернатива — сломали поддержку
            if supports:
                nearest_s = supports[0][0]
                sc_lines.append(
                    f"2. 🔴 Слом поддержки {_price_fmt(nearest_s)} "
                    f"→ продолжение вниз к {_price_fmt(nearest_s - atr_v * 2)}"
                )
        else:
            sc_lines.append(
                f"1. 🔴 Шорт от зоны ({_price_fmt(entry_lo)}–{_price_fmt(entry_hi)}) "
                f"→ TP1 <b>{_price_fmt(tp1)}</b> / TP2 <b>{_price_fmt(tp2)}</b> | "
                f"Стоп <b>{_price_fmt(stop)}</b> | R:R {rr:.1f}"
            )
            if resistances:
                nearest_r = resistances[0][0]
                sc_lines.append(
                    f"2. 🟢 Пробой сопротивления {_price_fmt(nearest_r)} "
                    f"→ продолжение вверх к {_price_fmt(nearest_r + atr_v * 2)}"
                )
        sc_lines.append(
            f"3. ⚪ Боковик {_price_fmt(p - atr_v)}"
            f"–{_price_fmt(p + atr_v)} до выхода объёма"
        )
    else:
        # Нет плана — базовые сценарии от ATR
        if d_htf == "bull" or h4_htf == "bull":
            sc_lines.append(f"1. 🟢 Продолжение вверх → {_price_fmt(p + atr_v * 2)} при удержании {_price_fmt(p - atr_v * 0.8)}")
            sc_lines.append(f"2. 🔴 Коррекция к {_price_fmt(p - atr_v * 1.5)} перед новым движением")
        else:
            sc_lines.append(f"1. 🔴 Продолжение вниз → {_price_fmt(p - atr_v * 2)} при пробое {_price_fmt(p - atr_v * 0.5)}")
            sc_lines.append(f"2. 🟢 Отскок к {_price_fmt(p + atr_v * 1.5)} как контр-трендовое движение")
        sc_lines.append(f"3. ⚪ Диапазон ±ATR вокруг {_price_fmt(p)} без объёмного триггера")

    lines.append("\n".join(sc_lines))

    # ── SL ориентиры ─────────────────────────────────────────────────────────
    if atr_v and p:
        sl_buf  = p - atr_v * 0.65
        sl_wide = p - atr_v * 1.8
        lines.append(
            f"🛡 <b>SL-ориентиры</b> (ATR={_price_fmt(atr_v)}):\n"
            f"• Мин. буфер: {_price_fmt(sl_buf)}  (ATR×0.65)\n"
            f"• Защита от sweep: {_price_fmt(sl_wide)}  (ATR×1.8)"
        )

    # ── Итог ─────────────────────────────────────────────────────────────────
    score = m.get("score", 0)
    grade = m.get("grade") or ""
    flags = m.get("flags", "—")

    if plan and plan.get("side") == "long":
        verdict = f"Лонг-сетап {grade} (score {score}) — вход в зону при подтверждении"
    elif plan and plan.get("side") == "short":
        verdict = f"Шорт-сетап {grade} (score {score}) — вход в зону при подтверждении"
    else:
        verdict = f"Наблюдение (score {score}) — ждём триггер для входа"

    if flags and flags != "—":
        verdict += f" | {flags}"

    lines.append(f"⚡ <b>ВЫВОД</b>: {verdict}")

    return "\n\n".join(lines)


def analyze_with_claude(symbol: str, chart_path: Path, metrics: dict) -> Optional[str]:
    """
    Отправляет график + метрики в Claude Vision.
    Возвращает текст анализа или None (вызывающий должен упасть на rule_based_analysis).
    """
    if not _ANTHROPIC_OK:
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        with open(chart_path, "rb") as f:
            img_b64 = base64.standard_b64encode(f.read()).decode("utf-8")
    except Exception as e:
        print(f"[ChartAI] Не могу прочитать график {chart_path}: {e}")
        return None

    metrics_text = _build_metrics_text(symbol, metrics)

    client = anthropic.Anthropic(api_key=api_key)

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": img_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                f"Проанализируй 1H график {symbol}.\n\n"
                                f"{metrics_text}\n\n"
                                "Дай технический анализ графика в заданном формате. "
                                "Учитывай SL-ориентиры — стопы должны быть шире минимума ATR×0.65."
                            ),
                        },
                    ],
                }
            ],
        )
        return resp.content[0].text if resp.content else None
    except Exception as e:
        print(f"[ChartAI] Ошибка Claude API для {symbol}: {e}")
        return None


# ─── Telegram photo sender ───────────────────────────────────────────────────

def tg_send_photo(token: str, chat_id: str, photo_path: Path,
                  caption: str = "") -> bool:
    """Отправляет PNG-фото в Telegram с подписью (caption до 1024 символов)."""
    MAX_CAP = 1024
    caption = caption[:MAX_CAP]
    try:
        with open(photo_path, "rb") as f:
            r = requests.post(
                f"{TG_BASE}/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"photo": (photo_path.name, f, "image/png")},
                timeout=30,
            )
        data = r.json()
        if not data.get("ok"):
            print(f"[ChartAI] TG sendPhoto error: {data.get('description')}")
            return False
        return True
    except Exception as e:
        print(f"[ChartAI] TG sendPhoto exception: {e}")
        return False


def tg_send_text(token: str, chat_id: str, text: str) -> bool:
    """Отправляет текстовое сообщение в Telegram (chunked)."""
    MAX = 4000
    chunks, pos = [], 0
    while pos < len(text):
        end = pos + MAX
        if end < len(text):
            split = text.rfind("\n", pos, end)
            end   = split if split > pos else end
        chunks.append(text[pos:end])
        pos = end
    ok = True
    for i, chunk in enumerate(chunks):
        try:
            r = requests.post(
                f"{TG_BASE}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=10,
            )
            if not r.json().get("ok"):
                ok = False
        except Exception:
            ok = False
        if i < len(chunks) - 1:
            time.sleep(0.3)
    return ok


# ─── Main entry point ────────────────────────────────────────────────────────

def run_and_send(candidates: list, token: str, chat_id: str,
                 top_n: int = 3) -> None:
    """
    Обрабатывает top_n кандидатов:
      - генерирует график
      - вызывает Claude Vision
      - отправляет фото + анализ в Telegram

    candidates — список dict'ов из screener.run_screener()
    """
    if not _CHART_OK:
        print("[ChartAI] mplfinance/pandas не установлены — пропускаем.")
        print("[ChartAI]  pip install mplfinance pandas")
        return

    if not _ANTHROPIC_OK:
        print("[ChartAI] anthropic SDK не установлен — пропускаем AI-анализ.")
        print("[ChartAI]  pip install anthropic")
        return

    top = candidates[:top_n]
    if not top:
        return

    _use_claude = bool(_ANTHROPIC_OK and os.environ.get("ANTHROPIC_API_KEY"))
    mode_str    = "Claude Vision" if _use_claude else "rule-based (без API)"
    tg_send_text(token, chat_id,
                 f"🤖 <b>Chart Analysis</b>  |  Топ-{len(top)} кандидатов\n"
                 f"<i>График 1H + метрики скринера → {mode_str}</i>")

    for i, cand in enumerate(top, 1):
        sym = cand.get("symbol", "?")
        print(f"[ChartAI] {i}/{len(top)} {sym}...")

        chart_path = generate_chart(sym, cand)
        if not chart_path:
            print(f"[ChartAI] Не удалось сгенерировать график для {sym}")
            continue

        analysis = analyze_with_claude(sym, chart_path, cand)
        if not analysis:
            print(f"[ChartAI] Claude API недоступен — использую rule-based анализ для {sym}")
            analysis = rule_based_analysis(sym, cand)

        # Формируем подпись к фото (до 1024 символов)
        score = cand.get("score", 0)
        setup = cand.get("setup", "?")
        grade = cand.get("grade") or ""
        fund  = cand.get("fund_%", 0) or 0
        oi24  = cand.get("oi24h_%", 0) or 0

        caption = (
            f"<b>{sym}</b>  score={score}  {grade}\n"
            f"Setup: {setup}  |  Fund: {fund:+.3f}%  OI24h: {oi24:+.1f}%\n\n"
        )
        # Если analysis влезает в caption — кладём туда, иначе шлём отдельно
        full = caption + analysis
        if len(full) <= 1024:
            tg_send_photo(token, chat_id, chart_path, caption=full)
        else:
            tg_send_photo(token, chat_id, chart_path, caption=caption.strip())
            tg_send_text(token, chat_id, analysis)

        # Удаляем старые графики (оставляем последние 30)
        _cleanup_charts(keep=30)

        if i < len(top):
            time.sleep(1)

    print(f"[ChartAI] AI Chart Analysis завершён: {len(top)} монет.")


def _cleanup_charts(keep: int = 30):
    """Удаляет старые PNG-файлы в charts/, оставляет последние `keep`."""
    try:
        files = sorted(CHARTS_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
        for old in files[:-keep]:
            old.unlink(missing_ok=True)
    except Exception:
        pass


# ─── CLI (ручной запуск для теста) ───────────────────────────────────────────

if __name__ == "__main__":
    import sys

    sym = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    print(f"[ChartAI] Тест: генерирую график {sym}...")

    dummy_metrics = {
        "symbol": sym, "score": 99, "setup": "test",
        "price": 0, "fund_%": 0, "oi24h_%": 0,
        "d_htf": "—", "h4_htf": "—", "cvd_k%": 0,
        "atr_%": 1.5, "sweep": "—", "flags": "—", "notes": "тест",
    }

    path = generate_chart(sym, dummy_metrics)
    if path:
        print(f"[ChartAI] График сохранён: {path}")
        analysis = analyze_with_claude(sym, path, dummy_metrics)
        if analysis:
            print("\n" + "─" * 60)
            print(analysis)
    else:
        print("[ChartAI] Ошибка генерации — проверь зависимости.")

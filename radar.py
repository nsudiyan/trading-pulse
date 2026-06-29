"""
Радар для РУЧНОЙ торговли (план A).

Машина доказанно умеет находить волатильность раньше (vol_spike: lift 2-4×, фора ~30-60мин),
но НЕ умеет выбрать направление (50/50). Поэтому радар НЕ даёт вход/стоп/тейк — он шлёт:
монету, силу спайка, реальные уровни (Volume Profile) и ссылку на MEXC-перп в TradingView.
Решение о входе и направлении — за человеком.
"""
from __future__ import annotations
from volume_profile import key_levels


def mexc_tv_link(symbol: str) -> str:
    """TradingView-график MEXC-перпа. Для USDT-перпов формат MEXC:{SYMBOL}.P.
    ponytail: если у TV иной тикер для части символов — добавить маппинг."""
    return f"https://www.tradingview.com/chart/?symbol=MEXC%3A{symbol}.P"


def _fmt(p) -> str:
    if p is None:
        return "—"
    if p >= 100:   return f"{p:,.2f}"
    if p >= 1:     return f"{p:.4f}"
    return f"{p:.6f}".rstrip("0").rstrip(".")


def build_radar_message(symbol: str, vol_ratio: float,
                        oi_chg_pct: float | None = None,
                        price_chg_30m: float | None = None,
                        vp_interval: str = "60", vp_bars: int = 168,
                        vp: dict | None = None) -> str:
    """Радар-сообщение (HTML для Telegram). vp можно передать готовым (тест/кэш),
    иначе считается из key_levels. vp_bars×vp_interval = диапазон профиля (дефолт 7д·1h)."""
    if vp is None:
        vp = key_levels(symbol, interval=vp_interval, limit=vp_bars)

    spike = f"⚡ Спайк объёма: <b>{vol_ratio:.1f}×</b>"
    if oi_chg_pct is not None:
        spike += f"  ·  OI {oi_chg_pct:+.1f}%"
    if price_chg_30m is not None:
        spike += f"  ·  цена ещё {price_chg_30m:+.1f}%"

    lines = [f"🌊 <b>РАДАР · {symbol}</b>", "", spike]

    if vp:
        rng_h = vp.get("bars", vp_bars) * (int(vp_interval) / 60.0)
        lines += [
            f"💵 Цена: <code>{_fmt(vp.get('last'))}</code>",
            "",
            f"📊 <b>Уровни</b> (Volume Profile, ~{rng_h:.0f}ч):",
            f"   POC  <code>{_fmt(vp['poc'])}</code>   ← магнит объёма",
            f"   VAH  <code>{_fmt(vp['vah'])}</code>",
            f"   VAL  <code>{_fmt(vp['val'])}</code>",
            f"   <i>{vp.get('zone','')}</i>",
        ]
    else:
        lines.append("📊 уровни: нет данных (профиль не построен)")

    lines += ["", f'📈 <a href="{mexc_tv_link(symbol)}">График MEXC-перп (TradingView)</a>']
    lines += ["", "<i>Направление решаешь ты — машина нашла волатильность, не сторону.</i>"]
    return "\n".join(lines)


# Кнопки ручного трека: что взял по своему решению (для честной статистики ТВОЕЙ торговли)
def radar_buttons(symbol: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "📈 Взял ЛОНГ",  "callback_data": f"radar_long|{symbol}"},
        {"text": "📉 Взял ШОРТ",  "callback_data": f"radar_short|{symbol}"},
        {"text": "⏭ Пропустил",   "callback_data": f"radar_skip|{symbol}"},
    ]]}


if __name__ == "__main__":
    import sys
    # self-check: сообщение содержит обязательные части, ссылка корректна
    fake_vp = {"poc": 100.5, "vah": 102.0, "val": 99.0, "last": 101.2,
               "zone": "ВНУТРИ зоны стоимости (равновесие)", "bars": 168, "lvn": None, "hvn": 100.5}
    msg = build_radar_message("BTCUSDT", 3.2, oi_chg_pct=0.8, price_chg_30m=0.3, vp=fake_vp)
    assert "РАДАР · BTCUSDT" in msg
    assert "3.2×" in msg and "POC" in msg and "102" in msg
    assert "MEXC%3ABTCUSDT.P" in msg and "tradingview.com" in msg
    assert "Направление решаешь ты" in msg
    assert mexc_tv_link("SOLUSDT") == "https://www.tradingview.com/chart/?symbol=MEXC%3ASOLUSDT.P"
    print("✓ self-check passed\n")
    print("=== ПРИМЕР радар-сообщения (как придёт в Telegram, без HTML-тегов) ===\n")
    import re
    sym = sys.argv[1] if len(sys.argv) > 1 else None
    if sym:
        live = build_radar_message(sym, 3.4, oi_chg_pct=1.2, price_chg_30m=0.4)
        print(re.sub(r"<[^>]+>", "", live).replace("&gt;", ">"))
    else:
        print(re.sub(r"<[^>]+>", "", msg))

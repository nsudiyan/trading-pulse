"""
Volume Profile — реальные ценовые уровни для радара (ручная торговля).

POC (Point of Control) — цена с максимальным наторгованным объёмом = магнит/ось.
VAH/VAL (Value Area High/Low) — границы зоны, где прошло 70% объёма = зона стоимости.
Это НЕ swing-уровни, а где реально торговали → настоящие S/R, к которым тянет цену.

Источник klines — Bybit linear perp (консистентно с детектором).
ponytail: для USDT-перпов уровни кросс-биржево близки к MEXC; если брат заметит
расхождение с MEXC-графиком — переключить fetch на MEXC klines.
"""
from __future__ import annotations
import urllib.request, json

BYBIT = "https://api.bybit.com/v5/market/kline"


def fetch_klines(symbol: str, interval: str = "60", limit: int = 168) -> list:
    """Bybit linear klines. interval в минутах ('60'=1h). Дефолт 168×1h = 7 дней.
    Возвращает [[ts,o,h,l,c,v], ...] по возрастанию времени, или []."""
    url = f"{BYBIT}?category=linear&symbol={symbol}&interval={interval}&limit={min(limit,1000)}"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            bars = json.loads(r.read())["result"]["list"]
        return sorted(([int(b[0])] + [float(x) for x in b[1:6]] for b in bars), key=lambda b: b[0])
    except Exception:
        return []


def volume_profile(klines: list, n_bins: int = 50, value_area_pct: float = 0.70) -> dict | None:
    """Чистое ядро (тестируемо, без сети). klines: [[ts,o,h,l,c,v],...].
    Объём бара кладётся в бин его typical price (h+l+c)/3 — стандартное упрощение,
    достаточно для POC/VA как уровней решений.
    Возвращает {poc, vah, val, price_min, price_max, bin_size, hvn, lvn}."""
    if not klines:
        return None
    highs = [b[2] for b in klines]; lows = [b[3] for b in klines]
    pmin, pmax = min(lows), max(highs)
    if pmax <= pmin:
        return None
    bin_size = (pmax - pmin) / n_bins
    bins = [0.0] * n_bins
    for _, o, h, l, c, v in klines:
        tp = (h + l + c) / 3.0
        idx = int((tp - pmin) / bin_size)
        idx = max(0, min(n_bins - 1, idx))
        bins[idx] += v

    def bin_price(i):
        return pmin + (i + 0.5) * bin_size

    poc_idx = max(range(n_bins), key=lambda i: bins[i])
    total = sum(bins)
    # Value Area: от POC расширяем к более «толстому» соседу, пока не наберём 70% объёма
    target = total * value_area_pct
    acc = bins[poc_idx]; lo = hi = poc_idx
    while acc < target and not (lo == 0 and hi == n_bins - 1):
        up = bins[hi + 1] if hi + 1 < n_bins else -1.0
        dn = bins[lo - 1] if lo - 1 >= 0 else -1.0
        if up >= dn:
            hi += 1; acc += bins[hi]
        else:
            lo -= 1; acc += bins[lo]
    # HVN/LVN — самый толстый/тонкий бин с ненулевым объёмом (узлы притяжения/вакуума)
    nz = [i for i in range(n_bins) if bins[i] > 0]
    hvn = bin_price(max(nz, key=lambda i: bins[i])) if nz else None
    lvn = bin_price(min(nz, key=lambda i: bins[i])) if nz else None
    return {
        "poc": round(bin_price(poc_idx), 8),
        "vah": round(pmin + (hi + 1) * bin_size, 8),
        "val": round(pmin + lo * bin_size, 8),
        "price_min": round(pmin, 8), "price_max": round(pmax, 8),
        "bin_size": bin_size, "hvn": round(hvn, 8) if hvn else None,
        "lvn": round(lvn, 8) if lvn else None,
    }


def key_levels(symbol: str, interval: str = "60", limit: int = 168) -> dict | None:
    """Обёртка: тянет klines + считает профиль + позицию текущей цены относительно зоны."""
    kl = fetch_klines(symbol, interval, limit)
    vp = volume_profile(kl)
    if not vp:
        return None
    last = kl[-1][4]  # close последнего бара ≈ текущая
    if last > vp["vah"]:
        zone = "ВЫШE зоны стоимости (перекуплен / премиум)"
    elif last < vp["val"]:
        zone = "НИЖЕ зоны стоимости (перепродан / дисконт)"
    else:
        zone = "ВНУТРИ зоны стоимости (равновесие)"
    vp["last"] = round(last, 8); vp["zone"] = zone
    vp["bars"] = len(kl)
    return vp


if __name__ == "__main__":
    import sys
    # Self-check на синтетике: объём сконцентрирован у цены 100 → POC≈100, VA узкая.
    synth = []
    for i in range(200):
        # большинство баров колеблется у 100 (высокий объём), редкие выбросы к 90/110 (низкий)
        if i % 20 == 0:
            synth.append([i, 90, 91, 89, 90, 5])      # редкий низ, малый объём
        elif i % 20 == 10:
            synth.append([i, 110, 111, 109, 110, 5])  # редкий верх, малый объём
        else:
            synth.append([i, 100, 100.5, 99.5, 100, 100])  # ядро, большой объём
    vp = volume_profile(synth, n_bins=50)
    assert vp is not None
    assert 99 <= vp["poc"] <= 101, f"POC должен быть ~100, получили {vp['poc']}"
    assert vp["val"] >= 95 and vp["vah"] <= 105, f"VA должна быть узкой вокруг 100: {vp['val']}-{vp['vah']}"
    assert vp["price_min"] <= 89 and vp["price_max"] >= 111
    # вырожденный вход
    assert volume_profile([]) is None
    assert volume_profile([[0, 100, 100, 100, 100, 10]]) is None  # pmax==pmin
    print(f"✓ self-check passed: POC={vp['poc']} VA=[{vp['val']}..{vp['vah']}] (узкая вокруг 100, как ожидалось)")
    if len(sys.argv) > 1:  # live: python volume_profile.py BTCUSDT
        kl_ = key_levels(sys.argv[1])
        print(f"\nLIVE {sys.argv[1]}: {kl_}")

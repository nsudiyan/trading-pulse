"""
binance_bridge.py — Кросс-биржевые данные Binance Futures (публичный API, без ключей).

Используется screener.py для кросс-подтверждения сигналов:
  +5 к score если funding знак совпадает на обеих биржах
  +8 дополнительно если оба funding < -0.05% (сильное шорт-давление = топливо для лонга)
  +8 если OI тренд совпадает на обеих биржах

Вызов из screener.py:
  import binance_bridge as _bnb
  bnb_map = _bnb.build_cross_map(symbols)  # {bybit_sym: {funding, oi_usdt, oi_change_pct}}
"""

import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

BASE = "https://fapi.binance.com"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "BybitScreener-BnbBridge/1.0"})

# ─── Маппинг Bybit → Binance символ ────────────────────────────────────────
# Большинство совпадают. Здесь только исключения.
_BYBIT_TO_BINANCE: dict[str, Optional[str]] = {
    "SHIB1000USDT":  "1000SHIBUSDT",
    "PEPE1000USDT":  "1000PEPEUSDT",
    "FLOKI1000USDT": "1000FLOKIUSDT",
    "TAOBYBIT":       None,   # нет на Binance
    "BTCPERP":        "BTCUSDT",
    "ETHPERP":        "ETHUSDT",
}


def _bybit_to_binance(sym: str) -> Optional[str]:
    if sym in _BYBIT_TO_BINANCE:
        return _BYBIT_TO_BINANCE[sym]
    # Для символов с нестандартным написанием (1000XXX уже корректные)
    return sym


def _get(path: str, params: dict = None, timeout: int = 8) -> Optional[dict | list]:
    try:
        r = _SESSION.get(f"{BASE}{path}", params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


# ─── Bulk fetch: все funding rates за один запрос ──────────────────────────

def fetch_all_premiums() -> dict[str, dict]:
    """
    Один запрос → все фьючерсные пары Binance с funding/markPrice/indexPrice.
    Возвращает {binance_sym: {funding_pct, mark, index, basis_pct}}.
    """
    data = _get("/fapi/v1/premiumIndex")
    if not isinstance(data, list):
        return {}

    out = {}
    for item in data:
        sym = item.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            funding   = float(item.get("lastFundingRate", 0)) * 100
            mark      = float(item.get("markPrice", 0))
            index     = float(item.get("indexPrice", 0))
            basis_pct = (mark - index) / index * 100 if index > 0 else 0.0
            out[sym]  = {
                "funding":   round(funding, 5),
                "mark":      mark,
                "index":     index,
                "basis_pct": round(basis_pct, 4),
            }
        except (ValueError, TypeError):
            continue
    return out


# ─── Per-symbol OI (параллельно) ──────────────────────────────────────────

def _fetch_oi_one(binance_sym: str) -> tuple[str, Optional[float]]:
    """Текущий OI в USDT для одного символа."""
    data = _get("/fapi/v1/openInterest", {"symbol": binance_sym})
    if isinstance(data, dict) and "openInterestValue" in data:
        try:
            return binance_sym, float(data["openInterestValue"])
        except (ValueError, TypeError):
            pass
    return binance_sym, None


def _fetch_oi_hist_one(binance_sym: str) -> tuple[str, Optional[float]]:
    """
    OI 24 часа назад (для вычисления OI change).
    Binance хранит историю OI в /futures/data/openInterestHist с периодом 1h.
    Берём 25-й элемент с конца (≈24h).
    """
    data = _get(
        "/futures/data/openInterestHist",
        {"symbol": binance_sym, "period": "1h", "limit": 26},
    )
    if isinstance(data, list) and len(data) >= 25:
        try:
            old_oi = float(data[0]["sumOpenInterestValue"])
            cur_oi = float(data[-1]["sumOpenInterestValue"])
            if old_oi > 0:
                return binance_sym, (cur_oi - old_oi) / old_oi * 100
        except (KeyError, ValueError, TypeError, IndexError):
            pass
    return binance_sym, None


def fetch_oi_batch(
    bybit_symbols: list[str],
    max_workers: int = 10,
) -> dict[str, float]:
    """
    Параллельно получает OI change 24h (%) для списка Bybit символов.
    Возвращает {bybit_sym: oi_change_pct}.
    """
    # Строим маппинг bnb_sym → bybit_sym для обратного lookup
    bnb_to_bybit: dict[str, str] = {}
    tasks: list[str] = []
    for sym in bybit_symbols:
        bnb = _bybit_to_binance(sym)
        if bnb:
            bnb_to_bybit[bnb] = sym
            tasks.append(bnb)

    result: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_fetch_oi_hist_one, bnb): bnb for bnb in tasks}
        for fut in as_completed(futs):
            bnb_sym, oi_chg = fut.result()
            bybit_sym = bnb_to_bybit.get(bnb_sym)
            if bybit_sym and oi_chg is not None:
                result[bybit_sym] = round(oi_chg, 2)
    return result


# ─── Главная функция: собрать всё воедино ─────────────────────────────────

def build_cross_map(
    bybit_symbols: list[str],
    include_oi: bool = True,
) -> dict[str, dict]:
    """
    Строит кросс-биржевую карту для списка Bybit символов.

    Возвращает:
      {
        bybit_sym: {
          "funding":    float,    # funding rate % на Binance
          "basis_pct":  float,    # (mark-index)/index % на Binance
          "oi_change":  float|None,  # OI change 24h % на Binance (если include_oi=True)
        }
      }

    Один bulk запрос для funding + параллельные запросы для OI.
    """
    premiums = fetch_all_premiums()
    if not premiums:
        return {}

    # Строим карту только для символов из нашего списка
    cross: dict[str, dict] = {}
    for bybit_sym in bybit_symbols:
        bnb_sym = _bybit_to_binance(bybit_sym)
        if not bnb_sym or bnb_sym not in premiums:
            continue
        p = premiums[bnb_sym]
        cross[bybit_sym] = {
            "funding":   p["funding"],
            "basis_pct": p["basis_pct"],
            "oi_change": None,
        }

    # OI change (опционально — немного дольше)
    if include_oi and cross:
        oi_changes = fetch_oi_batch(list(cross.keys()))
        for bybit_sym, oi_chg in oi_changes.items():
            if bybit_sym in cross:
                cross[bybit_sym]["oi_change"] = oi_chg

    return cross


# ─── Бонус к score от кросс-подтверждения ─────────────────────────────────

def apply_cross_bonus(result: dict, bnb: Optional[dict]) -> None:
    """
    Модифицирует result dict на месте: добавляет bnb_fund, bnb_cross_bonus, корректирует score.
    Вызывается в _fetch_and_score после score_symbol().
    """
    if not bnb:
        result["bnb_fund"] = None
        result["bnb_cross_bonus"] = 0
        return

    bnb_fund  = bnb.get("funding") or 0.0
    bnb_oi    = bnb.get("oi_change")
    bybit_fund = result.get("fund_%") or 0.0
    bybit_oi   = result.get("oi24h_%") or 0.0

    bonus = 0

    # ── Funding agreement ──────────────────────────────────────────────────
    if bybit_fund != 0 and bnb_fund != 0:
        if bybit_fund * bnb_fund > 0:          # одно направление — базовое подтверждение
            bonus += 5
        if bybit_fund < -0.05 and bnb_fund < -0.05:   # оба сильно отрицательные
            bonus += 8                          # надёжное топливо для шорт-сквиза
        elif bybit_fund > 0.05 and bnb_fund > 0.05:   # оба сильно положительные
            bonus += 8                          # лонг-сквиз на горизонте

    # ── OI direction agreement ────────────────────────────────────────────
    if bnb_oi is not None and bybit_oi != 0:
        if bybit_oi * bnb_oi > 0:              # OI растёт/падает на обеих биржах
            bonus += 8

    result["score"]           = min(result["score"] + bonus, 250)
    result["bnb_fund"]        = round(bnb_fund, 5)
    result["bnb_cross_bonus"] = bonus


# ─── CLI: быстрая проверка ────────────────────────────────────────────────

if __name__ == "__main__":
    print("Binance Bridge — тест")
    test_syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
    print(f"Загружаю кросс-данные для {test_syms}...")
    t0 = time.time()
    m = build_cross_map(test_syms, include_oi=True)
    print(f"Готово за {time.time() - t0:.1f}с\n")
    for sym, d in m.items():
        oi_str = f"{d['oi_change']:+.1f}%" if d["oi_change"] is not None else "N/A"
        print(
            f"  {sym:<15}  fund={d['funding']:+.4f}%  "
            f"basis={d['basis_pct']:+.4f}%  OI24h={oi_str}"
        )

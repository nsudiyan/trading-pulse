#!/usr/bin/env python3
"""
vol_core.py — ВАЛИДИРОВАННАЯ объёмная статистика для pump_detector (USE_VALIDATED_V1).

Чинит дефект LA3/LA4 (ревью 2026-05-28): live-код считал vol_ratio против ~50-мин
среднего 10×5m-свечей с ПЕРЕСЕКАЮЩИМСЯ окном (самореферентно → ratio≈1.0 by construction).
Здесь — ТОЧНАЯ реплика валидированной статистики из pump_analysis/feature_engineer.py:
vol_med = медиана объёма за ~30 дней (до 720 1h-баров) СТРОГО ДО текущего бара (непересекающаяся),
vol_ratio_1h = последний ЗАКРЫТЫЙ 1h-объём / медиана, vol_ratio_4h = среднее 4 закрытых / медиана,
vol_spike_count = число из 4 закрытых баров с объёмом > 2× медианы (диапазон 0..4).

Кросс-биржевая валидация (backtests/bybit_volcore_validation.py): на Bybit edge сохраняется
(Spearman vol_ratio_1h↔MFE24h = 0.449 = как Binance; high-vol MFE 5.96% vs 1.71%).

Функция vol_factors() — ЧИСТАЯ (тестируемая). Конвенция списка объёмов: ascending,
volumes[-1] = текущая НЕзакрытая 1h-свеча (исключается), volumes[-2] = последняя закрытая.
"""
from __future__ import annotations
from statistics import median, mean
from typing import Optional

WINDOW = 720         # ~30 дней 1h-баров (как H30D в feature_engineer)
MIN_PERIODS = 168    # ≥7 дней (как min_periods=7*24 в feature_engineer)
SPIKE_MULT = 2.0     # vol > 2× медианы = спайк


def vol_factors(volumes: list[float],
                window: int = WINDOW,
                min_periods: int = MIN_PERIODS,
                spike_mult: float = SPIKE_MULT) -> Optional[dict]:
    """Считает валидированные объёмные факторы из ascending-списка 1h-объёмов.

    Возвращает dict(vol_ratio_1h, vol_ratio_4h, vol_spike_count, baseline, n_bars)
    или None, если данных недостаточно (тогда вызывающий просто не применяет V1).
    """
    if not volumes:
        return None
    vols = [float(v) for v in volumes if v is not None]
    n = len(vols)
    if n < min_periods + 5:
        return None
    pos = n - 2                       # последний ЗАКРЫТЫЙ бар (n-1 — текущая незакрытая)
    lo = max(0, pos - window)
    base_slice = vols[lo:pos]         # до `window` баров СТРОГО до pos (непересекающаяся база)
    if len(base_slice) < min_periods:
        return None
    baseline = median(base_slice)
    if baseline <= 0:
        return None
    cur = vols[pos]
    last4 = vols[max(0, pos - 3):pos + 1]   # 4 закрытых бара, включая pos
    return {
        "vol_ratio_1h":    cur / baseline,
        "vol_ratio_4h":    mean(last4) / baseline,
        "vol_spike_count": sum(1 for v in last4 if v > spike_mult * baseline),
        "baseline":        baseline,
        "n_bars":          n,
    }


def v1_score_delta(vf: Optional[dict]) -> tuple[int, list[str]]:
    """Аддитивная V1-дельта по валидированным объёмным факторам (как в pump_detector).
    Возвращает (score_delta, signals). НЕ применяет режимо-нестабильные антисигналы
    (btc_trend_1h — плоский OOS для пампов; price_vs_high_7d — перевернул знак OOS)."""
    if not vf:
        return 0, []
    delta = 0
    sig = []
    if vf["vol_spike_count"] >= 1:
        delta += 15
        sig.append(f"[V1] vol_spike_count={vf['vol_spike_count']} (валид. 30д-медиана, OOS lift~2.1) +15")
    if vf["vol_ratio_1h"] > 1.0:
        delta += 10
        sig.append(f"[V1] vol_ratio_1h={vf['vol_ratio_1h']:.2f} (валид., OOS lift~1.9) +10")
    if vf["vol_ratio_4h"] > 1.0:
        delta += 8
        sig.append(f"[V1] vol_ratio_4h={vf['vol_ratio_4h']:.2f} (валид., OOS lift~1.8) +8")
    return delta, sig


def conviction(vf: Optional[dict]) -> dict:
    """Градуированный объёмный гейт (валидирован OOS на test-сплите feature_matrix;
    см. project_v1_validation_audit / backtests/second_filter_search.py).
    Независимого 2-го фактора не нашлось — но объёмный edge градуируется по интенсивности
    и форме (свежесть спайка vol_accel=vr1/vr4). Тиры по precision на TEST (base 16.7%):
      high (~52%, n=720): vscore>=23 И (vol_ratio_1h>3 ИЛИ vol_accel>1.2), не затухает
      standard (~25%): vscore>=23, нормальная форма
      weak (~23%):    vscore>=23, но vol_accel<0.8 (затухающий объём — стабильный антисигнал OOS)
      below_gate (~7%): vscore<23 (ниже базы 16.7% — отсекать)
    Возвращает {tier, score_delta, signals}. score_delta — для аддитивного пути; tier — для
    re-gating/shadow-A/B (при активации: high/standard пропускать, weak/below — душить 2-м гейтом)."""
    if not vf:
        return {"tier": "none", "score_delta": 0, "signals": []}
    vr1 = vf["vol_ratio_1h"]; vr4 = vf["vol_ratio_4h"]; spike = vf["vol_spike_count"]
    accel = vr1 / vr4 if vr4 and vr4 > 0 else 1.0
    vscore = (15 if spike >= 1 else 0) + (10 if vr1 > 1.0 else 0) + (8 if vr4 > 1.0 else 0)
    if vscore < 23:
        return {"tier": "below_gate", "score_delta": 0, "signals": []}
    if accel < 0.8:
        return {"tier": "weak", "score_delta": 5,
                "signals": [f"[V1-WEAK] объёмный гейт пройден, но vol_accel={accel:.2f}<0.8 затухает (OOS prec~23%) +5"]}
    if vr1 > 3.0 or accel > 1.2:
        why = []
        if vr1 > 3.0:  why.append(f"vr1={vr1:.2f}>3")
        if accel > 1.2: why.append(f"vol_accel={accel:.2f}>1.2 свежий спайк")
        return {"tier": "high", "score_delta": 33,
                "signals": [f"[V1-HIGH] {', '.join(why)} → высокая конвикция (OOS prec~52%, n=720 test) +33"]}
    return {"tier": "standard", "score_delta": 18,
            "signals": [f"[V1] объёмный гейт vscore={vscore} (vr1={vr1:.2f} vr4={vr4:.2f} spike={spike}, OOS prec~25%) +18"]}


# ── Standalone тест/диагностика (Bybit public, shared session, без FD-утечки) ──────
_SESSION = None
_BYBIT = "https://api.bybit.com/v5/market/kline"


def _session():
    global _SESSION
    if _SESSION is None:
        import requests
        _SESSION = requests.Session()
        # FD-leak fix: Connection:close — не копить CLOSE_WAIT к Bybit за idle (как screener.SESSION)
        _SESSION.headers.update({"Connection": "close"})
    return _SESSION


def fetch_bybit_1h_volumes(sym: str, limit: int = 750) -> Optional[list[float]]:
    """Фетчер 1h-объёмов с Bybit (ascending, newest=in-progress последним).
    Session-pooled (singleton requests.Session) → переиспользует соединения, БЕЗ FD-утечки.
    Используется и в live-блоке pump_detector (за флагом USE_VALIDATED_V1), и в тесте.
    ⚠ Перед активацией флага — батч-предзагружать в scan_once, не звать per-candidate."""
    try:
        r = _session().get(_BYBIT, params={"category": "linear", "symbol": sym,
                          "interval": "60", "limit": str(limit)}, timeout=10).json()
        lst = r.get("result", {}).get("list", [])
        if not lst:
            return None
        # Bybit отдаёт newest-first → reverse в ascending; volume = индекс 5
        return [float(b[5]) for b in reversed(lst)]
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    syms = sys.argv[1:] or ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "HYPEUSDT", "ZECUSDT"]
    print(f"{'symbol':12s} {'n_bars':>7s} {'vr1':>7s} {'vr4':>7s} {'spike':>6s} {'baseline':>12s}")
    for s in syms:
        vols = fetch_bybit_1h_volumes(s)
        vf = vol_factors(vols) if vols else None
        if vf:
            print(f"{s:12s} {vf['n_bars']:>7d} {vf['vol_ratio_1h']:>7.2f} {vf['vol_ratio_4h']:>7.2f} "
                  f"{vf['vol_spike_count']:>6d} {vf['baseline']:>12.1f}")
        else:
            print(f"{s:12s}  — нет данных / недостаточно баров")

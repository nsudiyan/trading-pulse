"""
outcome_model.py — Единый источник правды для классификации исхода сделки.

Раньше _outcome() и пороги PUMP/RUG_*_4H_PCT дублировались в pump_detector.py
и shadow_analyze.py — синхронизировались вручную, риск дрейфа (меняешь одну
копию, забываешь другую → backtest и live расходятся в определении WIN/LOSS).

Теперь обе стороны импортируют отсюда. Структурно дрейф невозможен.
Тесты: tests/test_outcome.py (incl. TestNoDriftBetweenCopies).
"""

from __future__ import annotations

# ── Пороги классификации исхода за 4ч окно ──────────────────────────────────
PUMP_WIN_4H_PCT   =  12.0  # +12% за 4ч = WIN (под цель TP 15%, R:R ≈ 1:4)
PUMP_LOSS_4H_PCT  =  -3.0  # -3% за 4ч = LOSS (структурный SL)
RUG_WIN_4H_PCT    = -10.0  # -10% за 4ч = WIN для rug_prep
RUG_LOSS_4H_PCT   =   3.0  # +3% за 4ч = LOSS для rug_prep


def outcome(tp_hit: bool, sl_hit: bool, t_mfe, t_mae) -> str:
    """
    Order-aware outcome. Если оба триггера хитнулись — побеждает тот, что
    наступил раньше во времени (t_mfe < t_mae → WIN). Если порядок неизвестен —
    пессимистично LOSS (реальный трейдер не знает заранее, какой touch был первым).
    """
    if tp_hit and sl_hit:
        if t_mfe and t_mae and t_mfe < t_mae:
            return "WIN"
        return "LOSS"
    if tp_hit:
        return "WIN"
    if sl_hit:
        return "LOSS"
    return "FLAT"

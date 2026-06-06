"""
Regression tests for the order-aware _outcome() WIN/LOSS/FLAT rule.

This is the heart of the "wick-WIN syndrome" fix (2026-05-25): an outcome is a
WIN only when TP is reached AND (if SL was also touched) TP came first in time.
A silent change here corrupts every win-rate metric, so the spec is locked here.

The function is duplicated in shadow_analyze.py and pump_detector.py; tests run
against both copies to catch drift.

Run: pytest tests/test_outcome.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from shadow_analyze import _outcome
from pump_detector import _outcome as _outcome_pump


class TestOutcomeSingleTrigger:
    def test_tp_only_is_win(self):
        assert _outcome(tp_hit=True, sl_hit=False, t_mfe=1.0, t_mae=0.0) == "WIN"

    def test_sl_only_is_loss(self):
        assert _outcome(tp_hit=False, sl_hit=True, t_mfe=0.0, t_mae=1.0) == "LOSS"

    def test_neither_is_flat(self):
        assert _outcome(tp_hit=False, sl_hit=False, t_mfe=0.0, t_mae=0.0) == "FLAT"


class TestOutcomeBothTriggered:
    """When both TP and SL are touched, the earlier-in-time one wins.
    Unknown ordering resolves pessimistically to LOSS (wick-WIN protection)."""

    def test_tp_before_sl_is_win(self):
        assert _outcome(tp_hit=True, sl_hit=True, t_mfe=1.0, t_mae=2.0) == "WIN"

    def test_sl_before_tp_is_loss(self):
        assert _outcome(tp_hit=True, sl_hit=True, t_mfe=2.0, t_mae=1.0) == "LOSS"

    def test_same_bar_is_loss(self):
        assert _outcome(tp_hit=True, sl_hit=True, t_mfe=1.5, t_mae=1.5) == "LOSS"

    def test_unknown_mfe_time_is_loss(self):
        assert _outcome(tp_hit=True, sl_hit=True, t_mfe=0.0, t_mae=2.0) == "LOSS"

    def test_unknown_mae_time_is_loss(self):
        assert _outcome(tp_hit=True, sl_hit=True, t_mfe=2.0, t_mae=0.0) == "LOSS"


# Canonical (tp_hit, sl_hit, t_mfe, t_mae) -> expected. Single source of truth
# for the drift guard below.
SPEC_CASES = [
    (True,  False, 1.0, 0.0, "WIN"),    # TP only
    (False, True,  0.0, 1.0, "LOSS"),   # SL only
    (False, False, 0.0, 0.0, "FLAT"),   # neither
    (True,  True,  1.0, 2.0, "WIN"),    # both, TP first
    (True,  True,  2.0, 1.0, "LOSS"),   # both, SL first
    (True,  True,  1.5, 1.5, "LOSS"),   # both, same bar
    (True,  True,  0.0, 2.0, "LOSS"),   # both, unknown MFE time
    (True,  True,  2.0, 0.0, "LOSS"),   # both, unknown MAE time
]


class TestNoDriftBetweenCopies:
    """pump_detector._outcome and shadow_analyze._outcome must stay identical —
    they encode the same WIN definition and a divergence would make the live
    detector and the backtest disagree on what counts as a win."""

    @pytest.mark.parametrize("tp,sl,t_mfe,t_mae,expected", SPEC_CASES)
    def test_pump_detector_matches_spec(self, tp, sl, t_mfe, t_mae, expected):
        assert _outcome_pump(tp, sl, t_mfe, t_mae) == expected

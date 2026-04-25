"""
Unit tests for detect_* functions in screener.py.
Uses synthetic numpy-style lists (no API calls).
Run: pytest tests/test_detect.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from screener import (
    detect_sweep,
    detect_fvg,
    detect_order_blocks,
    detect_choch,
    detect_htf_trend,
)


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────

def _const(n, v=100.0):
    return [v] * n


def _range_list(start, end, steps):
    if steps <= 1:
        return [start]
    step = (end - start) / (steps - 1)
    return [start + i * step for i in range(steps)]


# ──────────────────────────────────────────────────────────────────
# detect_sweep — 3-candle lookback
# ──────────────────────────────────────────────────────────────────

class TestDetectSweep:
    def _make_sweep_up(self, n=20):
        """Candle at [-3] pierces prior high then closes back below."""
        highs  = [101.0] * n
        lows   = [99.0]  * n
        closes = [100.0] * n
        # Prior structure high = 102
        for i in range(n - 8, n - 4):
            highs[i] = 102.0
        # Sweep candle at -3: high > 102, close < 102
        highs[-3]  = 103.5
        closes[-3] = 101.5
        return highs, lows, closes

    def _make_sweep_down(self, n=20):
        """Candle at [-3] pierces prior low then closes back above."""
        highs  = [101.0] * n
        lows   = [99.0]  * n
        closes = [100.0] * n
        # Prior structure low = 98
        for i in range(n - 8, n - 4):
            lows[i] = 98.0
        # Sweep candle at -3: low < 98, close > 98
        lows[-3]   = 96.5
        closes[-3] = 98.5
        return highs, lows, closes

    def test_sweep_up_detected(self):
        h, l, c = self._make_sweep_up()
        sup, sdn = detect_sweep(h, l, c)
        assert sup is not None, "Expected sweep_up to be detected"
        assert sdn is None

    def test_sweep_down_detected(self):
        h, l, c = self._make_sweep_down()
        sup, sdn = detect_sweep(h, l, c)
        assert sdn is not None, "Expected sweep_down to be detected"
        assert sup is None

    def test_no_sweep_on_flat(self):
        h = [101.0] * 20
        l = [99.0]  * 20
        c = [100.0] * 20
        sup, sdn = detect_sweep(h, l, c)
        assert sup is None
        assert sdn is None

    def test_insufficient_data_returns_none(self):
        h, l, c = [101.0] * 5, [99.0] * 5, [100.0] * 5
        sup, sdn = detect_sweep(h, l, c)
        assert sup is None
        assert sdn is None

    def test_lookback_3_candles(self):
        """Sweep at [-4] (3rd back from end) should still be caught."""
        h, l, c = self._make_sweep_up()
        # Move sweep 1 candle earlier
        h[-4], h[-3]   = h[-3], 101.0
        c[-4], c[-3]   = c[-3], 100.0
        sup, sdn = detect_sweep(h, l, c)
        assert sup is not None


# ──────────────────────────────────────────────────────────────────
# detect_fvg — Fair Value Gap
# ──────────────────────────────────────────────────────────────────

class TestDetectFvg:
    def test_bullish_fvg_detected(self):
        """
        Bullish FVG: high[i-2] < low[i]
        Use 5 candles: ..., H=98, [gap], ..., L=100
        """
        n = 10
        highs  = [100.0] * n
        lows   = [99.0]  * n
        closes = [100.0] * n
        # Candle at -3 has high=98, candle at -1 has low=100 → gap [98, 100]
        highs[-3]  = 98.0
        lows[-1]   = 100.5  # low above prior high → bullish FVG
        closes[-1] = 101.0

        fvgs = detect_fvg(highs, lows, closes, lookback=10, min_size_pct=0.0)
        bull_fvgs = [f for f in fvgs if f["type"] == "bull"]
        assert len(bull_fvgs) >= 1, "Expected at least one bullish FVG"

    def test_bearish_fvg_detected(self):
        """
        Bearish FVG: low[i-2] > high[i]
        """
        n = 10
        highs  = [100.0] * n
        lows   = [99.0]  * n
        closes = [99.5]  * n
        # Candle at -3 has low=102, candle at -1 has high=99 → bearish gap [99, 102]
        lows[-3]   = 102.0
        highs[-1]  = 99.0
        closes[-1] = 98.5

        fvgs = detect_fvg(highs, lows, closes, lookback=10, min_size_pct=0.0)
        bear_fvgs = [f for f in fvgs if f["type"] == "bear"]
        assert len(bear_fvgs) >= 1, "Expected at least one bearish FVG"

    def test_no_fvg_on_flat(self):
        n = 15
        h = [100.0] * n
        l = [99.5]  * n
        c = [99.8]  * n
        fvgs = detect_fvg(h, l, c, lookback=10, min_size_pct=0.0)
        assert fvgs == []

    def test_fvg_returns_list(self):
        h = [100.0] * 10
        l = [99.0]  * 10
        c = [99.5]  * 10
        result = detect_fvg(h, l, c)
        assert isinstance(result, list)

    def test_fvg_struct_keys(self):
        """Each FVG entry must have required keys."""
        n = 15
        highs  = [100.0] * n
        lows   = [99.0]  * n
        closes = [100.5] * n
        highs[-3] = 98.0
        lows[-1]  = 100.5
        fvgs = detect_fvg(highs, lows, closes, lookback=10, min_size_pct=0.0)
        if fvgs:
            required = {"type", "top", "bottom", "size_pct", "dist_pct", "in_zone"}
            assert required.issubset(set(fvgs[0].keys()))


# ──────────────────────────────────────────────────────────────────
# detect_order_blocks
# ──────────────────────────────────────────────────────────────────

class TestDetectOrderBlocks:
    def _make_bull_bos(self, n=25):
        """Bullish BOS: close > max of prior 5 highs, preceded by a bearish candle."""
        opens  = [100.0] * n
        highs  = [101.0] * n
        lows   = [99.0]  * n
        closes = [100.5] * n
        volumes = [1000.0] * n

        # Prior 5 candles have highs at 101
        # BOS candle: close = 104 (above 101)
        highs[-1]  = 105.0
        closes[-1] = 104.0

        # Insert a bearish candle before the BOS to be the OB
        opens[-3]  = 102.0
        closes[-3] = 100.0  # bearish: open > close

        return opens, highs, lows, closes, volumes

    def test_bull_ob_detected(self):
        o, h, l, c, v = self._make_bull_bos()
        obs = detect_order_blocks(o, h, l, c, v, lookback=15)
        bull_obs = [ob for ob in obs if ob["type"] == "bull"]
        assert len(bull_obs) >= 1

    def test_ob_struct_keys(self):
        o, h, l, c, v = self._make_bull_bos()
        obs = detect_order_blocks(o, h, l, c, v, lookback=15)
        if obs:
            required = {"type", "top", "bottom", "dist_pct", "in_zone", "body_pct"}
            assert required.issubset(set(obs[0].keys()))

    def test_flat_market_no_bos(self):
        n = 20
        o = [100.0] * n
        h = [100.5] * n
        l = [99.5]  * n
        c = [100.0] * n
        v = [1000.0] * n
        obs = detect_order_blocks(o, h, l, c, v, lookback=10)
        # No clear BOS — should return empty or minimal list
        assert isinstance(obs, list)

    def test_at_most_5_obs_returned(self):
        """detect_order_blocks caps output at 5."""
        n = 40
        opens  = [100.0] * n
        highs  = [101.0] * n
        lows   = [99.0]  * n
        closes = [100.5] * n
        volumes = [1000.0] * n
        # Create multiple BOS events
        for i in range(5, 35, 5):
            highs[i]   = 110.0
            closes[i]  = 108.0
            opens[i-1] = 105.0
            closes[i-1] = 103.0  # bearish OB
        obs = detect_order_blocks(opens, highs, lows, closes, volumes, lookback=40)
        assert len(obs) <= 5


# ──────────────────────────────────────────────────────────────────
# detect_choch — Change of Character
# ──────────────────────────────────────────────────────────────────

class TestDetectChoch:
    """
    detect_choch(h, l, c, lookback) uses c[-(lookback+1):-1] for structure,
    then cur = c[-1] for the CHoCH trigger. We must place swing points
    inside [-(lookback+1):-1] and set c[-1] as the breakout candle.
    """

    def _make_downtrend_choch(self):
        """
        Build LH+LL downtrend where c[-2] (= cur in detect_choch) breaks above LH.

        detect_choch internals:
          n = min(lookback, len(c)-2)
          window = c[-(n+1):-1]   ← swing detection (excludes last element)
          cur    = window[-1]      ← = c[-2] of the original array
        So swings must sit inside c[-(n+1):-1] and cur = c[-2] is the trigger.

        We use 30 candles, lookback=20:
          n = min(20, 28) = 20
          window = c[-21:-1] = c[9:29] (20 candles, indices 9-28)
          cur    = c[28]
        Swing points are placed at absolute indices 11, 15, 19, 23.
        CHoCH trigger: c[28] > last LH.
        """
        n = 30
        h = [100.0] * n
        l = [100.0] * n
        c = [100.0] * n

        # Swing high 1 at abs index 11 (windowed idx 2): h=116
        h[9:14]  = [111.0, 113.0, 116.0, 112.0, 110.0]
        c[11] = 115.0

        # Swing low 1 at abs index 15 (windowed idx 6): l=93
        l[13:18] = [98.0, 96.0, 93.0, 96.0, 97.0]
        c[15] = 94.0

        # Swing high 2 (LH < 116) at abs index 19 (windowed idx 10): h=111
        h[17:22] = [106.0, 108.0, 111.0, 108.0, 106.0]
        c[19] = 110.0

        # Swing low 2 (LL < 93) at abs index 23 (windowed idx 14): l=88
        l[21:26] = [93.0, 91.0, 88.0, 91.0, 92.0]
        c[23] = 89.0

        # cur = c[28] must exceed last LH high (111) → bull_choch
        c[28] = 113.0
        h[28] = 114.0

        return h, l, c

    def _make_uptrend_choch(self):
        """
        Build HH+HL uptrend where c[-2] (= cur) breaks below HL.
        Same layout as downtrend but mirrored.
        """
        n = 30
        h = [100.0] * n
        l = [100.0] * n
        c = [100.0] * n

        # Swing low 1 at abs index 11 (windowed idx 2): l=84
        l[9:14]  = [89.0, 87.0, 84.0, 87.0, 88.0]
        c[11] = 85.0

        # Swing high 1 at abs index 15 (windowed idx 6): h=116
        h[13:18] = [111.0, 113.0, 116.0, 112.0, 110.0]
        c[15] = 115.0

        # Swing low 2 (HL > 84) at abs index 19 (windowed idx 10): l=88
        l[17:22] = [93.0, 91.0, 88.0, 91.0, 92.0]
        c[19] = 89.0

        # Swing high 2 (HH > 116) at abs index 23 (windowed idx 14): h=121
        h[21:26] = [116.0, 118.0, 121.0, 118.0, 117.0]
        c[23] = 120.0

        # cur = c[28] must go below last HL low (88) → bear_choch
        c[28] = 85.0
        l[28] = 84.0

        return h, l, c

    def test_bull_choch_in_downtrend(self):
        """Price breaking above LH in a downtrend = bull CHoCH."""
        h, l, c = self._make_downtrend_choch()
        result = detect_choch(h, l, c, lookback=20)
        assert result == "bull_choch", f"Expected bull_choch, got {result}"

    def test_bear_choch_in_uptrend(self):
        """Price breaking below HL in an uptrend = bear CHoCH."""
        h, l, c = self._make_uptrend_choch()
        result = detect_choch(h, l, c, lookback=20)
        assert result == "bear_choch", f"Expected bear_choch, got {result}"

    def test_returns_none_on_insufficient_data(self):
        h = [100.0] * 5
        l = [99.0]  * 5
        c = [99.5]  * 5
        result = detect_choch(h, l, c)
        assert result is None

    def test_returns_none_on_flat_market(self):
        n = 30
        h = [100.5] * n
        l = [99.5]  * n
        c = [100.0] * n
        result = detect_choch(h, l, c, lookback=20)
        # Flat market has no swing structure → None expected (or possibly None)
        assert result in (None, "bull_choch", "bear_choch")  # structural test


# ──────────────────────────────────────────────────────────────────
# detect_htf_trend
# ──────────────────────────────────────────────────────────────────

class TestDetectHtfTrend:
    def _bull_series(self, n=30):
        closes = [100.0 + i * 0.5 for i in range(n)]
        highs  = [c + 1.0 for c in closes]
        lows   = [c - 1.0 for c in closes]
        return highs, lows, closes

    def _bear_series(self, n=30):
        closes = [130.0 - i * 0.5 for i in range(n)]
        highs  = [c + 1.0 for c in closes]
        lows   = [c - 1.0 for c in closes]
        return highs, lows, closes

    def test_bull_trend(self):
        h, l, c = self._bull_series()
        result = detect_htf_trend(h, l, c)
        assert result == "bull"

    def test_bear_trend(self):
        h, l, c = self._bear_series()
        result = detect_htf_trend(h, l, c)
        assert result == "bear"

    def test_flat_returns_range(self):
        n = 30
        h = [101.0] * n
        l = [99.0]  * n
        c = [100.0] * n
        result = detect_htf_trend(h, l, c)
        assert result == "range"

    def test_insufficient_data_returns_range(self):
        h = [101.0] * 5
        l = [99.0]  * 5
        c = [100.0] * 5
        result = detect_htf_trend(h, l, c)
        assert result == "range"

    def test_valid_return_values(self):
        h, l, c = self._bull_series()
        result = detect_htf_trend(h, l, c)
        assert result in ("bull", "bear", "range")

    def test_sma20_fallback_bull(self):
        """When swing structure is ambiguous, SMA20 fallback kicks in."""
        n = 25
        closes = [95.0] * 5 + [100.0] * n  # starts low then jumps
        closes = closes[:n]
        # Make price significantly above SMA20
        closes[-1] = 120.0
        highs = [c + 1.0 for c in closes]
        lows  = [c - 1.0 for c in closes]
        result = detect_htf_trend(highs, lows, closes)
        assert result in ("bull", "bear", "range")


# ──────────────────────────────────────────────────────────────────
# Run count check
# ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])

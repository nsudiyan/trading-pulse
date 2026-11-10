import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "bot"))
from btc_gate import btc_45m_gate


STEP = 900_000


def bars_for_move(first_open, last_close, *, event_close=45 * 60_000):
    # A valid, contiguous 45-minute sequence whose intermediate bars do not
    # affect the first-open-to-last-close gate calculation.
    closes = [first_open, (first_open + last_close) / 2, last_close]
    rows = []
    for index, close in enumerate(closes):
        start = event_close - 3 * STEP + index * STEP
        opening = first_open if index == 0 else closes[index - 1]
        rows.append({"start_ms": start, "end_ms": start + STEP - 1,
                     "open": opening, "high": max(opening, close) * 1.001,
                     "low": min(opening, close) * 0.999,
                     "close": close})
    return rows


class BtcGateTests(unittest.TestCase):
    def test_btc_symbol_is_exempt(self):
        result = btc_45m_gate("BTCUSDT", "BUY", [], 45 * 60_000)
        self.assertTrue(result["passed"])
        self.assertEqual(result["reason"], "btc_not_applicable")

    def test_fast_btc_drop_blocks_alt_buy_not_sell(self):
        bars = bars_for_move(100, 98.99)
        buy = btc_45m_gate("ETHUSDT", "BUY", bars, 45 * 60_000)
        sell = btc_45m_gate("ETHUSDT", "SELL", bars, 45 * 60_000)
        self.assertFalse(buy["passed"])
        self.assertEqual(buy["reason"], "suppressed_btc_gate:btc_down_fast")
        self.assertTrue(sell["passed"])
        self.assertAlmostEqual(buy["move_pct"], -1.01, places=2)

    def test_fast_btc_rise_blocks_alt_sell_not_buy(self):
        bars = bars_for_move(100, 101.01)
        sell = btc_45m_gate("SOLUSDT", "SELL", bars, 45 * 60_000)
        buy = btc_45m_gate("SOLUSDT", "BUY", bars, 45 * 60_000)
        self.assertFalse(sell["passed"])
        self.assertEqual(sell["reason"], "suppressed_btc_gate:btc_up_fast")
        self.assertTrue(buy["passed"])
        self.assertAlmostEqual(sell["move_pct"], 1.01, places=2)

    def test_threshold_is_strict_and_missing_data_is_visible_fail_open(self):
        flat_threshold = bars_for_move(100, 99)
        self.assertTrue(btc_45m_gate("ETHUSDT", "BUY", flat_threshold,
                                     45 * 60_000)["passed"])
        unavailable = btc_45m_gate("ETHUSDT", "BUY", [], 45 * 60_000)
        self.assertTrue(unavailable["passed"])
        self.assertEqual(unavailable["reason"], "btc_no_data")
        self.assertIsNone(unavailable["move_pct"])

    def test_gaps_future_bars_bad_ohlc_and_unaligned_close_fail_open(self):
        bars = bars_for_move(100, 99)
        cases = [bars[:2], [dict(bars[0], low=101), *bars[1:]],
                 [*bars, dict(bars[0], close=100.5)]]
        for candidate in cases:
            result = btc_45m_gate("ETHUSDT", "BUY", candidate, 45 * 60_000)
            self.assertTrue(result["passed"])
            self.assertEqual(result["reason"], "btc_no_data")
        future = dict(bars[-1], start_ms=45 * 60_000,
                      end_ms=45 * 60_000 + STEP - 1,
                      open=99, high=99, low=99, close=99)
        future_result = btc_45m_gate("ETHUSDT", "BUY", bars + [future], 45 * 60_000)
        self.assertEqual(future_result["reason"], "btc_gate_passed")
        self.assertAlmostEqual(future_result["move_pct"], -1.0)
        unaligned = btc_45m_gate("ETHUSDT", "BUY", bars, 45 * 60_000 + 1)
        self.assertEqual(unaligned["reason"], "btc_no_data")


if __name__ == "__main__":
    unittest.main()

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "bot"))
from sweep_gate import confirm_sweep_activity, sweep_activity_ratio


STEP = 900_000


def series(values, field="turnover"):
    bars = []
    for i, value in enumerate(values):
        bar = {"start_ms": i * STEP, "end_ms": (i + 1) * STEP - 1,
               "open": 100, "high": 101, "low": 99, "close": 100,
               "volume": value, "turnover": value}
        bar[field] = value
        bars.append(bar)
    return bars


class SweepActivityGateTests(unittest.TestCase):
    def test_linear_uses_quote_turnover_not_base_volume(self):
        bars = series([10] * 14 + [100])
        for bar in bars:
            bar["volume"] = 10  # deliberately different from USDT turnover
        result, reason = confirm_sweep_activity(
            {"code": "asia_range_sweep", "direction": "BUY",
             "end_ms": bars[-1]["end_ms"]}, bars, "linear", 2.5)
        self.assertIsNone(reason)
        self.assertAlmostEqual(result["volume_ratio"], 10.0)
        self.assertEqual(result["volume_basis"], "quote_turnover")

    def test_inverse_uses_contract_volume_not_turnover(self):
        bars = series([10] * 14 + [25], field="volume")
        for bar in bars:
            bar["turnover"] = 10
        result, reason = confirm_sweep_activity(
            {"end_ms": bars[-1]["end_ms"]}, bars, "inverse", 2.5)
        self.assertIsNone(reason)
        self.assertAlmostEqual(result["volume_ratio"], 2.5)
        self.assertEqual(result["volume_basis"], "contract_volume")

    def test_current_bar_is_excluded_from_its_baseline(self):
        bars = series([10] * 14 + [24])
        self.assertAlmostEqual(sweep_activity_ratio(bars, "linear"), 2.4)
        result, reason = confirm_sweep_activity(
            {"end_ms": bars[-1]["end_ms"]}, bars, "linear", 2.5)
        self.assertIsNone(result)
        self.assertEqual(reason, "sweep_activity_unconfirmed")

    def test_only_data_as_of_sweep_event_is_used(self):
        bars = series([10] * 14 + [25, 10_000])
        result, reason = confirm_sweep_activity(
            {"end_ms": bars[-2]["end_ms"]}, bars, "linear", 2.5)
        self.assertIsNone(reason)
        self.assertAlmostEqual(result["volume_ratio"], 2.5)

    def test_gap_stale_candidate_and_bad_threshold_fail_closed(self):
        bars = series([10] * 14 + [30])
        gap = [*bars[:5], *bars[6:]]
        sweep = {"end_ms": bars[-1]["end_ms"]}
        self.assertEqual(confirm_sweep_activity(sweep, gap, "linear")[1],
                         "sweep_activity_unavailable")
        self.assertEqual(confirm_sweep_activity(
            {"end_ms": bars[-2]["end_ms"]}, bars, "linear")[1],
            "sweep_activity_unavailable")
        self.assertEqual(confirm_sweep_activity(sweep, bars, "linear", 0)[1],
                         "sweep_activity_unavailable")

    def test_missing_turnover_or_invalid_category_fails_closed(self):
        bars = series([10] * 14 + [30])
        for bar in bars:
            del bar["turnover"]
        self.assertEqual(confirm_sweep_activity(
            {"end_ms": bars[-1]["end_ms"]}, bars, "linear")[1],
            "sweep_activity_unavailable")
        self.assertEqual(confirm_sweep_activity(
            {"end_ms": bars[-1]["end_ms"]}, bars, "spot")[1],
            "sweep_activity_unavailable")

    def test_misaligned_timestamps_and_overflowed_baseline_fail_closed(self):
        bars = series([10] * 14 + [30])
        shifted = [dict(bar, start_ms=bar["start_ms"] + 1,
                        end_ms=bar["end_ms"] + 1) for bar in bars]
        self.assertEqual(confirm_sweep_activity(
            {"end_ms": shifted[-1]["end_ms"]}, shifted, "linear")[1],
            "sweep_activity_unavailable")

        huge = series([1e308] * 15)
        result, reason = confirm_sweep_activity(
            {"end_ms": huge[-1]["end_ms"]}, huge, "linear")
        self.assertIsNone(result)
        self.assertEqual(reason, "sweep_activity_unavailable")


if __name__ == "__main__":
    unittest.main()

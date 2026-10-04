import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "bot"))
from sweep_gate import confirm_sweep_volume


STEP = 900_000


def series(volumes):
    return [{"start_ms": i * STEP, "end_ms": (i + 1) * STEP - 1,
             "open": 100, "high": 101, "low": 99, "close": 100,
             "volume": volume} for i, volume in enumerate(volumes)]


class SweepVolumeGateTests(unittest.TestCase):
    def test_uses_fourteen_prior_bars_and_includes_the_measured_ratio(self):
        bars = series([10] * 14 + [25])
        result, reason = confirm_sweep_volume(
            {"code": "asia_range_sweep", "direction": "BUY",
             "end_ms": bars[-1]["end_ms"]}, bars, 1.8)
        self.assertIsNone(reason)
        self.assertAlmostEqual(result["volume_ratio"], 2.5)

    def test_below_threshold_is_rejected_and_current_bar_is_not_in_baseline(self):
        bars = series([10] * 14 + [17.99])
        result, reason = confirm_sweep_volume(
            {"end_ms": bars[-1]["end_ms"]}, bars, 1.8)
        self.assertIsNone(result)
        self.assertEqual(reason, "sweep_volume_unconfirmed")

    def test_gap_stale_candidate_and_bad_threshold_fail_closed(self):
        bars = series([10] * 14 + [30])
        gap = [*bars[:5], *bars[6:]]
        sweep = {"end_ms": bars[-1]["end_ms"]}
        self.assertEqual(confirm_sweep_volume(sweep, gap)[1],
                         "sweep_volume_unavailable")
        self.assertEqual(confirm_sweep_volume({"end_ms": bars[-2]["end_ms"]}, bars)[1],
                         "sweep_volume_unavailable")
        self.assertEqual(confirm_sweep_volume(sweep, bars, 0)[1],
                         "sweep_volume_unavailable")

    def test_misaligned_timestamps_and_overflowed_baseline_fail_closed(self):
        bars = series([10] * 14 + [30])
        shifted = [dict(bar, start_ms=bar["start_ms"] + 1,
                        end_ms=bar["end_ms"] + 1) for bar in bars]
        self.assertEqual(confirm_sweep_volume(
            {"end_ms": shifted[-1]["end_ms"]}, shifted)[1],
            "sweep_volume_unavailable")

        huge = series([1e308] * 15)
        result, reason = confirm_sweep_volume(
            {"end_ms": huge[-1]["end_ms"]}, huge)
        self.assertIsNone(result)
        self.assertEqual(reason, "sweep_volume_unavailable")


if __name__ == "__main__":
    unittest.main()

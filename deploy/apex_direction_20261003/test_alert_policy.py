import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "bot"))
from alert_policy import format_msk, session_at, timeframe_alert_policy
from level_age import format_level_age, is_level_fresh, observe_level_age


def ms(utc_text):
    return int(datetime.fromisoformat(utc_text).replace(tzinfo=timezone.utc).timestamp() * 1000)


class AlertPolicyTests(unittest.TestCase):
    def test_moscow_conversion_and_session_boundaries(self):
        self.assertEqual(format_msk(ms("2026-10-02T08:00:00")), "02.10 11:00 МСК")
        cases = [
            ("2026-10-02T00:59:00", "DEAD_ZONE"),
            ("2026-10-02T01:00:00", "ASIA"),
            ("2026-10-02T08:00:00", "LONDON_KZ"),
            ("2026-10-02T10:00:00", "LONDON_CLOSE"),
            ("2026-10-02T13:00:00", "NY_KZ"),
            ("2026-10-02T15:00:00", "NY_PM"),
            ("2026-10-02T17:00:00", "DEAD_ZONE"),
        ]
        for timestamp, expected in cases:
            self.assertEqual(session_at(ms(timestamp)), expected, timestamp)

    def test_alert_timeframes_use_the_configured_moscow_windows(self):
        # 08:00 UTC = 11:00 MSK, the inclusive London KZ boundary.
        for tf in ("15", "60", "D"):
            self.assertEqual(timeframe_alert_policy(tf, ms("2026-10-02T08:00:00")),
                             (True, None))
        # 4H is permitted in every configured session except 20:00–04:00 MSK.
        self.assertEqual(timeframe_alert_policy("240", ms("2026-10-02T00:00:00")),
                         (False, "suppressed_session:4H:dead_zone"))
        self.assertEqual(timeframe_alert_policy("240", ms("2026-10-02T01:00:00")),
                         (True, None))
        self.assertEqual(timeframe_alert_policy("240", ms("2026-10-02T16:59:00")),
                         (True, None))
        self.assertEqual(timeframe_alert_policy("240", ms("2026-10-02T17:00:00")),
                         (False, "suppressed_session:4H:dead_zone"))
        self.assertEqual(timeframe_alert_policy("W", ms("2026-10-02T08:00:00")),
                         (False, "suppressed_timeframe:1W"))
        self.assertEqual(timeframe_alert_policy("5", ms("2026-10-02T08:00:00")),
                         (False, "suppressed_timeframe:5m"))
        self.assertEqual(timeframe_alert_policy("15", ms("2026-10-02T07:59:59")),
                         (False, "suppressed_session:15m:asia"))
        self.assertEqual(timeframe_alert_policy("15", ms("2026-10-02T09:59:59")),
                         (True, None))
        self.assertEqual(timeframe_alert_policy("15", ms("2026-10-02T10:00:00")),
                         (False, "suppressed_session:15m:london_close"))
        self.assertEqual(timeframe_alert_policy("60", ms("2026-10-02T12:59:59")),
                         (False, "suppressed_session:1H:london_close"))
        self.assertEqual(timeframe_alert_policy("60", ms("2026-10-02T13:00:00")),
                         (True, None))
        self.assertEqual(timeframe_alert_policy("60", ms("2026-10-02T14:59:59")),
                         (True, None))
        self.assertEqual(timeframe_alert_policy("60", ms("2026-10-02T15:00:00")),
                         (False, "suppressed_session:1H:ny_pm"))
        self.assertEqual(timeframe_alert_policy("D", ms("2026-10-02T10:00:00")),
                         (False, "suppressed_session:1D:london_close"))

    def test_bybit_daily_close_moscow_implication(self):
        # Bybit crypto D candles close at 00:00 UTC = 03:00 Moscow; this
        # configured close-time Kill Zone rule therefore blocks direct D alerts.
        close = ms("2026-10-02T00:00:00")
        self.assertEqual(session_at(close), "DEAD_ZONE")
        self.assertFalse(timeframe_alert_policy("D", close)[0])

    def test_level_age_bands_and_hard_cutoff(self):
        self.assertEqual(format_level_age(45, "15"), "45м 🟢 свежий (3 свечи)")
        self.assertEqual(format_level_age(416, "15"), "6ч 56м 🔴 устарел (28 свечей)")
        self.assertTrue(is_level_fresh(240, "15"))
        self.assertFalse(is_level_fresh(240.01, "15"))
        self.assertTrue(is_level_fresh(24 * 60, "60"))
        self.assertFalse(is_level_fresh(24 * 60 + 1, "60"))

    def test_unknown_future_and_fresh_level_observations(self):
        self.assertEqual(observe_level_age(None, 1000, "15", 100)["status"], "unknown")
        self.assertEqual(observe_level_age(1001, 1000, "15", 100)["status"], "unknown")
        self.assertEqual(observe_level_age(100, 1000, "15", None)["status"], "unknown")
        event = 100_000_000
        obs = observe_level_age(event - 240 * 60_000, event, "15", 1533.31)
        self.assertEqual(obs["status"], "fresh")
        self.assertEqual(obs["age_candles"], 16)


if __name__ == "__main__":
    unittest.main()

"""State-machine tests: synthetic fixtures, not strategy/backtest evidence."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "bot"))
# The release bundle's session module is supplied by the existing production
# checkout; these tests keep unrelated session classification inert.
sessions_stub = ModuleType("sessions")
sessions_stub.closed_asia_range = lambda *args: None
sessions_stub.session_at = lambda *args: "DEAD_ZONE"
sys.modules.setdefault("sessions", sessions_stub)
import setup_chain


STEP = 900_000


def bar(index: int, *, close: float = 100.0,
        turnover: float = 1_000_000.0) -> dict:
    start = index * STEP
    return {"start_ms": start, "end_ms": start + STEP - 1,
            "open": close, "high": max(close + 1, 102),
            "low": min(close - 1, 98), "close": close,
            "volume": turnover, "turnover": turnover}


def fixture_bars() -> tuple[list[dict], list[dict]]:
    candles = [bar(i) for i in range(20)]
    candles[10]["high"] = 101.0
    event = bar(20, close=99.5, turnover=2_000_000)
    event.update(open=100.5, high=101, low=98)
    candles.append(event)
    prior_24h = [bar(i, turnover=1_000_000) for i in range(20 - 96, 20)]
    return candles, prior_24h


def sweep_candidate(current: dict) -> list[dict]:
    return [{"code": "equal_level_sweep", "direction": "BUY", "level": 99.0,
             "end_ms": current["end_ms"], "level_known_ms": 10 * STEP,
             "priority": 0, "sweep_low": current["low"], "sweep_high": None,
             "wick_body_ratio": 2.0, "penetration_bps": 100.0,
             "swept_extreme": current["low"]}]


class SetupChainTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        setup_chain.ensure_schema(self.db)
        self.bars, self.prior_24h = fixture_bars()
        self.pivots = patch.object(setup_chain, "confirmed_pivots",
                                   return_value=([10], []))
        self.candidates = patch.object(setup_chain, "sweep_candidates",
                                       side_effect=lambda bars, **kwargs:
                                       sweep_candidate(bars[-1]))
        self.pivots.start()
        self.candidates.start()

    def tearDown(self):
        self.pivots.stop()
        self.candidates.stop()
        self.db.close()

    def process(self, candles=None, **kwargs):
        candles = self.bars if candles is None else candles
        return setup_chain.process_closed_bar(
            self.db, "TESTUSDT", "15", candles,
            h4_bars=[], hourly_bars=[], bars_15m=self.prior_24h,
            category="linear", min_quote_24h=1_000,
            **kwargs)

    def test_state_survives_sqlite_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            db = sqlite3.connect(path)
            setup_chain.ensure_schema(db)
            result = setup_chain.process_closed_bar(
                db, "TESTUSDT", "15", self.bars,
                h4_bars=[], hourly_bars=[], bars_15m=self.prior_24h,
                category="linear", min_quote_24h=1_000)
            self.assertEqual(result["status"], "sweep_detected")
            db.commit()
            db.close()
            reopened = sqlite3.connect(path)
            state = setup_chain._get_state(reopened, "TESTUSDT", "15")
            self.assertEqual(state["direction"], "BUY")
            self.assertEqual(state["bars_waited"], 0)
            reopened.close()

    def test_sweep_detected_is_initial_state_not_confirmation(self):
        result = self.process()
        self.assertEqual(result["status"], "sweep_detected")
        self.assertEqual(result["direction"], "BUY")
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM active_sweep_setups").fetchone()[0], 1)

    def test_opposing_sweeps_on_one_bar_are_suppressed_as_ambiguous(self):
        sell = {**sweep_candidate(self.bars[-1])[0], "direction": "SELL"}
        with patch.object(setup_chain, "sweep_candidates",
                          return_value=[sweep_candidate(self.bars[-1])[0], sell]):
            result = self.process()
        self.assertEqual(result["status"], "sweep_conflicting_directions")
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM active_sweep_setups").fetchone()[0], 0)

    def test_same_sweep_bar_cannot_be_its_own_choch(self):
        result = self.process()
        self.assertEqual(result["status"], "sweep_detected")
        self.assertEqual(result["choch_pivot"]["level"], 101.0)
        self.assertEqual(self.db.execute(
            "SELECT event_type FROM sweep_setup_events ORDER BY event_ms").fetchone()[0],
            "sweep_detected")

    def test_later_close_beyond_buffer_confirms_once(self):
        self.assertEqual(self.process()["status"], "sweep_detected")
        later = bar(21, close=101.4)
        later.update(open=100, high=102, low=99)
        result = self.process([*self.bars, later])
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["direction"], "BUY")
        self.assertGreater(result["choch"]["close_price"], result["choch"]["threshold"])
        self.assertEqual(result["bars_waited"], 1)
        self.assertEqual(self.db.execute(
            "SELECT event_type FROM sweep_setup_events ORDER BY event_ms DESC LIMIT 1").fetchone()[0],
            "choch_confirmed")
        self.assertIsNone(setup_chain._get_state(self.db, "TESTUSDT", "15"))

    def test_breach_of_sweep_extreme_invalidates(self):
        self.process()
        later = bar(21, close=97.5)
        result = self.process([*self.bars, later])
        self.assertEqual(result["status"], "invalidated")
        self.assertIsNone(setup_chain._get_state(self.db, "TESTUSDT", "15"))

    def test_wait_expires_after_configured_eight_later_bars(self):
        self.process(max_wait_bars=2)
        candles = list(self.bars)
        for index in (21, 22):
            candles.append(bar(index, close=99.5))
            self.assertEqual(self.process(candles, max_wait_bars=2)["status"], "pending")
        candles.append(bar(23, close=99.5))
        self.assertEqual(self.process(candles, max_wait_bars=2)["status"], "timeout")

    def test_gap_resets_without_using_nonconsecutive_bar(self):
        self.process()
        gap_bar = bar(22, close=101.4)
        result = self.process([*self.bars, gap_bar])
        self.assertEqual(result["status"], "data_gap_reset")
        self.assertIsNone(setup_chain._get_state(self.db, "TESTUSDT", "15"))

    def test_duplicate_closed_candle_does_not_advance_chain(self):
        self.process()
        result = self.process(self.bars)
        self.assertEqual(result["status"], "duplicate_or_out_of_order")

    def test_volume_activity_uses_prior_closed_notional_baseline(self):
        weak = [dict(item) for item in self.bars]
        weak[-1]["turnover"] = 1_400_000
        result = self.process(weak)
        self.assertEqual(result["status"], "sweep_activity_unconfirmed")
        self.assertAlmostEqual(result["detail"]["volume_ratio"], 1.4)
        self.assertEqual(setup_chain.activity_threshold(1_000_000_000), 2.5)
        self.assertEqual(setup_chain.activity_threshold(500_000_000), 2.0)
        self.assertEqual(setup_chain.activity_threshold(100_000_000), 1.8)
        self.assertEqual(setup_chain.activity_threshold(99_999_999), 1.8)


if __name__ == "__main__":
    unittest.main()

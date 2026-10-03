import itertools
import ast
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys
import unittest
import tempfile

ROOT = Path(__file__).parent
sys.path[:0] = [str(ROOT / "bot"), str(ROOT / "dashboard")]
from followthrough import ensure_schema as follow_schema
from scenario_contract import classify, ensure_schema, save, label, explanation, zone_observation, zone_note
from movement import measure, STEP_MS


class Contracts(unittest.TestCase):
    def test_actual_telegram_formatter(self):
        tree = ast.parse((ROOT / "bot/market.py").read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "format_compact_alert")
        node.returns = None
        for arg in node.args.args:
            arg.annotation = None
        env = {"datetime": datetime, "timezone": timezone, "scenario_label": label,
               "scenario_explanation": explanation, "zone_note": zone_note, "LABELS": {"15": "15м"}, "TF_ORDER": (), "BTC_SYMBOLS": set()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "market.py", "exec"), env)
        class News:
            def recent(self, *args):
                return []
        bar = {"symbol": "TEST", "interval": "15", "end_ms": 999, "open": 100, "high": 101, "low": 99, "close": 100}
        for side in ("BUY", "SELL", None):
            message = env["format_compact_alert"](bar, [], {}, None, [], [], ["x" * 4000], {}, News(), {}, datetime.now(timezone.utc), None, None, None, {"reason": "fixture"}, "linear", {"side": side})
            self.assertIn("сценарий " + (side or "БЕЗ НАПРАВЛЕНИЯ"), message)
            self.assertLessEqual(len(message), 3900)
            self.assertIn("NO-TRADE", message)
            self.assertIn("Классификатор BUY/SELL зону не проверяет", message)

    def test_trigger_trend_zone_matrix_is_observational(self):
        # Presentation contract intentionally does not implement an unvalidated
        # zone trading gate. This tests all 90 combinations and its disclosure.
        for trigger, h4, day, raw_zone in itertools.product(("up", "down"), ("рост", "снижение", None), ("рост", "снижение", None), ("premium", "discount", "equilibrium", "INSIDE_VA", None)):
            contexts = {"240": {"trend": h4, "end_ms": 999}, "D": {"trend": day, "end_ms": 999}}
            zone = zone_observation({"zone": raw_zone}, 999, 100)
            result = classify([{"code": "structure_" + trigger}], contexts, 999, zone=zone)
            expected = "BUY" if trigger == "up" and h4 == day == "рост" else "SELL" if trigger == "down" and h4 == day == "снижение" else None
            self.assertEqual(result["side"], expected)
            self.assertFalse(result["zone_is_direction_gate"])
            self.assertFalse(result["entry_confirmed"])
            self.assertEqual(zone["zone"], raw_zone if raw_zone in ("premium", "discount", "equilibrium") else None)

    def test_zone_provenance(self):
        full = zone_observation({"zone": "discount", "swing_low": 90, "swing_high": 110, "swing_mid": 100}, 999, 95)
        self.assertEqual(full["status"], "reported_with_geometry")
        self.assertFalse(full["is_volume_profile_zone"])
        self.assertIsNone(full["asof_end_ms"])
        self.assertIsNone(full["observation_price"])
        self.assertFalse(full["event_time_verified"])
        sweep = zone_observation({"reason": "strong_sweep_review", "zone": "discount"}, 999, 95)
        self.assertTrue(sweep["event_time_verified"])
        self.assertEqual(sweep["asof_end_ms"], 999)
        self.assertEqual(sweep["observation_price"], 95)
        bad = zone_observation({"zone": "premium", "swing_low": 90, "swing_high": 110, "swing_mid": 999}, 999, 105)
        self.assertEqual(bad["status"], "reported_without_geometry")
        self.assertIsNone(bad["geometry"])
        self.assertEqual(zone_observation({"price_zone": "INSIDE_VA"},999,100)["status"], "unavailable")

    def test_flat_path(self):
        bar = {"start_ms": STEP_MS, "end_ms": 2*STEP_MS-1, "open": 100, "high": 100, "low": 100, "close": 100}
        for side in ("BUY", "SELL", None):
            r = measure([bar], 1, 2*STEP_MS, side)
            self.assertEqual([r[k] for k in ("return_pct", "max_up_pct", "max_down_pct")], [0, 0, 0])
            self.assertEqual(r["mfe_pct"], None if side is None else 0)

    def test_incremental_replay_missing_recovery_and_no_future(self):
        first = self.bars()[0]
        second = {"start_ms": 2*STEP_MS, "end_ms": 3*STEP_MS-1, "open": 102, "high": 106, "low": 99, "close": 105}
        self.assertEqual(measure([first], 1, STEP_MS)["status"], "waiting")
        a = measure([first, second], 1, 2*STEP_MS, "BUY")
        self.assertEqual(len(a["curve"]), 1)
        self.assertAlmostEqual(a["max_up_pct"], 4)
        self.assertEqual(measure([first], 1, 3*STEP_MS)["status"], "data_unavailable")
        b = measure([first, second], 1, 3*STEP_MS, "BUY")
        self.assertEqual(b["status"], "tracking")
        self.assertEqual(b["anchor_price"], a["anchor_price"])
        self.assertAlmostEqual(b["return_pct"], 5)
        self.assertAlmostEqual(b["max_up_pct"], 6)
        self.assertAlmostEqual(b["max_down_pct"], -2)
        self.assertEqual(measure([first, second, second], 1, 3*STEP_MS), measure([first, second], 1, 3*STEP_MS))

    def test_direction_matrix(self):
        for trigger, h4, day in itertools.product(("up", "down"), ("рост", "снижение", None), ("рост", "снижение", None)):
            contexts = {"240": {"trend": h4, "end_ms": 999}, "D": {"trend": day, "end_ms": 999}}
            result = classify([{"code": "structure_" + trigger}], contexts, 999)
            expected = "BUY" if trigger == "up" and h4 == day == "рост" else "SELL" if trigger == "down" and h4 == day == "снижение" else None
            self.assertEqual(result["side"], expected)
            self.assertFalse(result["entry_confirmed"])

    def test_future_stale_missing(self):
        for end in (None, 1001, -86_400_000):
            self.assertIsNone(classify([{"code": "structure_up"}], {"240": {"trend": "рост", "end_ms": end}}, 1000)["side"])

    def test_conflicting_triggers(self):
        self.assertEqual(classify([{"code": "structure_up"}], {}, 999, {"direction": "SELL"})["status"], "conflict")

    def test_immutable(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        save(db, "a", {"side": "BUY"})
        save(db, "a", {"side": "SELL"})
        self.assertIn("BUY", db.execute("SELECT contract_json FROM signal_scenarios").fetchone()[0])
        db.close()

    def bars(self):
        return [{"start_ms": STEP_MS, "end_ms": 2 * STEP_MS - 1, "open": 100, "high": 104, "low": 98, "close": 102}]

    def test_raw_and_directional(self):
        for side in ("BUY", "SELL", None):
            r = measure(self.bars(), 1, 2 * STEP_MS, side)
            self.assertAlmostEqual(r["return_pct"], 2)
            self.assertAlmostEqual(r["max_up_pct"], 4)
            self.assertAlmostEqual(r["max_down_pct"], -2)
            if side:
                self.assertAlmostEqual(r["mfe_pct"], 4 if side == "BUY" else 2)
            else:
                self.assertIsNone(r["mfe_pct"])

    def test_missing_future_nan_duplicates(self):
        b = self.bars()
        self.assertEqual(measure([], 1, 2 * STEP_MS)["status"], "data_unavailable")
        self.assertEqual(measure(b, 1, 2 * STEP_MS - 1)["status"], "waiting")
        self.assertEqual(measure(b + b, 1, 2 * STEP_MS)["status"], "tracking")
        self.assertEqual(measure(b + [{**b[0], "close": 101}], 1, 2 * STEP_MS)["status"], "conflicting_duplicates")
        self.assertEqual(measure([{**b[0], "close": math.nan}], 1, 2 * STEP_MS)["status"], "invalid_ohlc")

    def test_real_server_fixture(self):
        # Execute the actual API read function, isolating unrelated portfolio and
        # channel network collectors. This is a synthetic DB fixture, not a backtest.
        tree = ast.parse((ROOT / "dashboard/server.py").read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "read_signals")
        env = {"sqlite3": sqlite3, "datetime": datetime, "timezone": timezone,
               "json": json, "measure": measure, "WINDOW_MS": 72 * 3600000,
               "STEP_MS": STEP_MS, "MAX_LIMIT": 100, "read_portfolio": lambda db: {}}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "server.py", "exec"), env)
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as tmp:
            db = sqlite3.connect(tmp.name)
            db.execute("CREATE TABLE signal_alerts (id TEXT PRIMARY KEY, status TEXT, sent_utc TEXT, created_utc TEXT, text TEXT, reason TEXT)")
            db.execute("CREATE TABLE signal_outcomes (alert_id TEXT, symbol TEXT, side TEXT, signal_interval TEXT, close_ms INTEGER, reference_close REAL, price_1h REAL, price_4h REAL, price_24h REAL, return_1h_pct REAL, return_4h_pct REAL, return_24h_pct REAL)")
            db.execute("CREATE TABLE candles (symbol TEXT, interval TEXT, start_ms INTEGER, end_ms INTEGER, open REAL, high REAL, low REAL, close REAL)")
            follow_schema(db)
            ensure_schema(db)
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            start = now // STEP_MS * STEP_MS - STEP_MS
            stamp = datetime.fromtimestamp((start - 1) / 1000, timezone.utc).isoformat()
            for key in ("review:TEST:15:1", "review:OLD:15:2"):
                db.execute("INSERT INTO signal_alerts VALUES (?,?,?,?,?,?)", (key, "sent", stamp, stamp, "fixture", "review"))
            save(db, "review:TEST:15:1", {"side": "SELL", "status": "scenario"})
            db.execute("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?)", ("TEST", "15", start, start + STEP_MS - 1, 100, 104, 98, 102))
            db.commit()
            result = env["read_signals"](tmp.name)
            current = next(x for x in result["signals"] if x["symbol"] == "TEST")
            old = next(x for x in result["signals"] if x["symbol"] == "OLD")
            self.assertEqual(current["scenario"]["side"], "SELL")
            self.assertAlmostEqual(current["movement"]["mfe_pct"], 2)
            self.assertAlmostEqual(current["movement"]["max_up_pct"], 4)
            self.assertEqual(old["scenario"]["status"], "historical_unverified")
            self.assertEqual(old["movement"]["status"], "data_unavailable")
            db.close()


if __name__ == "__main__":
    unittest.main()

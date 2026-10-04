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
from scenario_contract import (classify, ensure_schema, save, label, explanation,
                               zone_observation, zone_note, alert_heading,
                               apply_direction_gate)
from movement import measure, STEP_MS
from provenance_guard import verified_zone
from horizons import event_horizons
from alert_policy import format_msk
from level_age import observe_level_age, level_alert_text


class Contracts(unittest.TestCase):
    def test_actual_telegram_formatter(self):
        tree = ast.parse((ROOT / "bot/market.py").read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "format_compact_alert")
        node.returns = None
        for arg in node.args.args:
            arg.annotation = None
        env = {"datetime": datetime, "timezone": timezone, "scenario_label": label,
               "alert_heading": alert_heading,
               "scenario_explanation": explanation, "zone_note": zone_note,
               "format_msk": format_msk,
               "level_alert_text": level_alert_text,
               "LABELS": {"15": "15м"}, "TF_ORDER": (), "BTC_SYMBOLS": set()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "market.py", "exec"), env)
        class News:
            def recent(self, *args):
                return []
        bar = {"symbol": "TEST", "interval": "15", "end_ms": 999, "open": 100, "high": 101, "low": 99, "close": 100}
        for side in ("BUY", "SELL", None):
            scenario = {"side": side, "level_observation": {
                "status": "fresh", "timeframe": "15m", "level_price": 100,
                "age_text": "45м 🟢 свежий (3 свечи)"}}
            message = env["format_compact_alert"](bar, [], {}, None, [], [], ["x" * 4000], {}, News(), {}, datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc), None, None, None, {"reason": "fixture"}, "linear", scenario)
            expected_heading = ("📈 BUY TEST [LINEAR]" if side == "BUY" else
                                "📉 SELL TEST [LINEAR]" if side == "SELL" else
                                "🔎 TEST [LINEAR] · NO-TRADE")
            self.assertIn(expected_heading, message)
            self.assertLessEqual(len(message), 3900)
            self.assertIn("NO-TRADE", message)
            self.assertIn("BUY/SELL требует закрытую последовательность sweep → CHoCH", message)
            self.assertIn("Свеча: 15м · 01.01 03:00 МСК", message)
            self.assertIn("Анализ: 02.10.2026 11:00 МСК", message)
            self.assertIn("Уровень BOS 15m: 100 · возраст 45м 🟢 свежий (3 свечи)", message)

    def verified_zone(self, zone_name, event=999):
        geometry = {"swing_low": 90, "swing_high": 110, "swing_mid": 100}
        price = {"discount": 95, "premium": 105, "equilibrium": 100}[zone_name]
        gate = {"zone": zone_name, **geometry, "source_route": "review_gate",
                "zone_provenance": {"event_time_verified": True, "source_route": "review_gate",
                                    "event_end_ms": event, "price_end_ms": event,
                                    "geometry_end_ms": event, "price": price,
                                    "geometry": geometry}}
        return zone_observation(gate, event, price)

    def confirmed_setup(self, direction="BUY", event=999, known_ms=None):
        start = event + 1 - 900_000
        sweep_start = start - 900_000
        known = sweep_start if known_ms is None else known_ms
        level = 100.0
        threshold = 100.1 if direction == "BUY" else 99.9
        close = 101.0 if direction == "BUY" else 99.0
        return {"status": "confirmed", "direction": direction, "timeframe": "15",
                "sweep": {"code": "equal_level_sweep", "direction": direction,
                          "level": 99.0 if direction == "BUY" else 101.0,
                          "start_ms": sweep_start, "end_ms": start - 1,
                          "swept_extreme": 98.0 if direction == "BUY" else 102.0},
                "choch": {"level": level, "level_known_ms": known, "buffer": 0.1,
                          "threshold": threshold, "close_price": close,
                          "event_ms": event + 1, "source_pivot_ms": known - 180_000},
                "bars_waited": 2, "heuristics": {"max_wait_bars": 8}}

    def test_user_direction_matrix_all_conflicts_suppressed(self):
        event = 1_000_000_000_000
        trend_states = ("рост", "снижение", "смешанная", None)
        for trigger, h4, zone_name in itertools.product(
                ("up", "down"), trend_states, ("discount", "premium", "equilibrium")):
            contexts = {"240": {"trend": h4, "end_ms": event}}
            zone = self.verified_zone(zone_name, event)
            direction = "BUY" if trigger == "up" else "SELL"
            result = classify([], contexts, event, zone=zone,
                              event_timeframe="15",
                              setup=self.confirmed_setup(direction, event))
            expected = ("BUY" if trigger == "up" and h4 == "рост" and zone_name == "discount"
                        else "SELL" if trigger == "down" and h4 == "снижение" and zone_name == "premium"
                        else None)
            self.assertEqual(result["side"], expected, (trigger, h4, zone_name))
            self.assertEqual(result["entry_confirmed"], False)
            self.assertTrue(result["zone_is_direction_gate"])
            if expected:
                self.assertEqual(result["status"], "scenario")
            else:
                    self.assertEqual(result["reason"], "suppressed_direction:conflict")

    def test_level_age_filter_is_event_time_and_fails_closed(self):
        event = 1_000_000_000_000
        context = {"240": {"trend": "рост", "end_ms": event}}
        zone = self.verified_zone("discount", event)
        fresh_setup = self.confirmed_setup("BUY", event, event - 30 * 60_000)
        fresh = classify([], context, event, zone=zone, event_timeframe="15",
                         setup=fresh_setup)
        self.assertEqual(fresh["side"], "BUY")
        self.assertEqual(fresh["level_observation"]["status"], "fresh")
        stale = classify([], context, event, zone=zone, event_timeframe="15",
                         setup=self.confirmed_setup("BUY", event, event - 241 * 60_000))
        self.assertIsNone(stale["side"])
        self.assertEqual(stale["reason"], "suppressed_stale_level:15m:241m")
        missing_setup = self.confirmed_setup("BUY", event)
        missing_setup["choch"]["level_known_ms"] = None
        missing = classify([], context, event, zone=zone, event_timeframe="15",
                           setup=missing_setup)
        self.assertEqual(missing["reason"], "suppressed_setup:invalid_contract")
        self.assertEqual(apply_direction_gate({"send": True}, stale)["reason"],
                         "suppressed_stale_level:15m:241m")

    def test_timeframe_and_level_age_policies_are_wired_to_market_pipeline(self):
        tree = ast.parse((ROOT / "bot/market.py").read_text())
        engine_bar = next(node for node in ast.walk(tree)
                          if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_bar")
        calls = {node.func.id for node in ast.walk(engine_bar)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertIn("timeframe_alert_policy", calls)
        self.assertIn("apply_direction_gate", calls)
        scenario_builder = next(node for node in ast.walk(tree)
                                if isinstance(node, ast.FunctionDef)
                                and node.name == "scenario_context")
        self.assertIn("event_timeframe", ast.unparse(scenario_builder))
        self.assertIn("level_alert_text", ast.unparse(tree))

    def test_missing_sequence_or_unverified_zone_suppresses(self):
        event = 1_000_000_000_000
        context = {"240": {"trend": "рост", "end_ms": event}}
        self.assertEqual(classify([], context, event, zone=self.verified_zone("discount", event))["reason"],
                         "suppressed_setup:sweep_choch_required")
        raw = zone_observation({"zone": "discount"}, event, 95)
        self.assertEqual(classify([], context, event, zone=raw,
                                 setup=self.confirmed_setup(event=event))["reason"],
                         "suppressed_direction:conflict")
        result = classify([{"code": "structure_up", "name": "BOS: fixture"}], context, event,
                          zone=self.verified_zone("discount", event))
        self.assertIsNone(result["side"])
        self.assertEqual(result["reason"], "suppressed_setup:sweep_choch_required")

    def test_shared_final_gate_and_directional_headings(self):
        for route in ("generic", "user_rule"):
            result = apply_direction_gate({"send": True, "reason": route}, {"side": None})
            self.assertEqual(result, {"send": False, "reason": "suppressed_direction:conflict"})
        self.assertEqual(apply_direction_gate({"send": True}, {"side": "BUY"}), {"send": True})
        self.assertEqual(alert_heading("ETHUSDT", {"side": "BUY"}), "📈 BUY ETHUSDT [LINEAR]")
        self.assertEqual(alert_heading("ETHUSD", {"side": "SELL"}, "inverse"), "📉 SELL ETHUSD [INVERSE]")

    def test_all_notification_routes_share_the_sequence_gate(self):
        tree = ast.parse((ROOT / "bot/market.py").read_text())
        engine_bar = next(node for node in ast.walk(tree)
                          if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_bar")
        calls = {call.func.id for call in ast.walk(engine_bar)
                 if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)}
        self.assertIn("process_closed_bar", calls)
        self.assertIn("scenario_context", calls)
        self.assertIn("apply_direction_gate", calls)

    def test_legacy_mtf_alignment_does_not_override_confirmed_chain_matrix(self):
        tree = ast.parse((ROOT / "bot/market.py").read_text())
        engine_bar = next(node for node in ast.walk(tree)
                          if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_bar")
        source = ast.unparse(engine_bar)
        tests = {ast.unparse(node.test) for node in ast.walk(engine_bar)
                 if isinstance(node, ast.If)}
        self.assertIn("not gate['send'] and (not setup)", tests)
        self.assertIn("quality['send'] and gate and gate_cfg.get('require_profile', False) and (not setup)", tests)
        # The setup route still captures review_gate's event-time zone snapshot;
        # the scenario contract applies the actual 4h trend/zone direction matrix.
        self.assertIn('require_zone=True if setup else', source)
        contract = ast.parse((ROOT / "bot/scenario_contract.py").read_text())
        classify_source = ast.unparse(next(node for node in contract.body
            if isinstance(node, ast.FunctionDef) and node.name == "classify"))
        self.assertIn("context_side == 'BULLISH' and (zone_name == 'discount')", classify_source)
        self.assertIn("context_side == 'BEARISH' and (zone_name == 'premium')", classify_source)

    def test_both_telegram_formatters_use_the_same_direction_heading(self):
        tree = ast.parse((ROOT / "bot/market.py").read_text())
        formatters = {node.name: node for node in tree.body
                      if isinstance(node, ast.FunctionDef)
                      and node.name in {"format_review_alert", "format_compact_alert"}}
        self.assertEqual(set(formatters), {"format_review_alert", "format_compact_alert"})
        for node in formatters.values():
            self.assertTrue(any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                                and call.func.id == "alert_heading" for call in ast.walk(node)))

    def test_zone_provenance(self):
        full = zone_observation({"zone": "discount", "swing_low": 90, "swing_high": 110, "swing_mid": 100}, 999, 95)
        self.assertEqual(full["status"], "reported_with_geometry")
        self.assertFalse(full["is_volume_profile_zone"])
        self.assertIsNone(full["asof_end_ms"])
        self.assertIsNone(full["observation_price"])
        self.assertFalse(full["event_time_verified"])
        sweep = zone_observation({"reason": "strong_sweep_review", "zone": "discount"}, 999, 95)
        self.assertFalse(sweep["event_time_verified"])
        self.assertIsNone(sweep["asof_end_ms"])
        self.assertIsNone(sweep["observation_price"])
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
        event = 1_000_000_000_000
        for trigger, h4, zone_name in itertools.product(("up", "down"), ("рост", "снижение", None), ("premium", "discount", "equilibrium")):
            contexts = {"240": {"trend": h4, "end_ms": event}}
            direction = "BUY" if trigger == "up" else "SELL"
            result = classify([], contexts, event,
                              zone=self.verified_zone(zone_name, event),
                              event_timeframe="15",
                              setup=self.confirmed_setup(direction, event))
            expected = ("BUY" if trigger == "up" and h4 == "рост" and zone_name == "discount"
                        else "SELL" if trigger == "down" and h4 == "снижение" and zone_name == "premium"
                        else None)
            self.assertEqual(result["side"], expected)
            self.assertFalse(result["entry_confirmed"])

    def test_future_stale_missing(self):
        for end in (None, 1001, -86_400_000):
            self.assertIsNone(classify([{"code": "structure_up"}], {"240": {"trend": "рост", "end_ms": end}}, 1000)["side"])

    def test_conflicting_triggers(self):
        scenario = classify([{"code": "structure_up"}], {}, 999, {"direction": "SELL"})
        self.assertEqual(scenario["reason"], "suppressed_setup:sweep_choch_required")

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
               "json": json, "measure": measure, "verified_zone": verified_zone, "event_horizons": event_horizons, "WINDOW_MS": 72 * 3600000,
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
            db.execute("INSERT INTO signal_alerts VALUES (?,?,?,?,?,?)", ("review:BAD:15:3", "sent", stamp, stamp, "fixture invalid v4", "review"))
            save(db, "review:BAD:15:3", {"version": "direction-context-v4-provenance-guard", "side": "BUY", "zone_observation": {"status":"unavailable", "event_time_verified":False}})
            db.execute("INSERT INTO signal_alerts VALUES (?,?,?,?,?,?)", ("review:BAD5:15:4", "sent", stamp, stamp, "fixture invalid v5", "review"))
            save(db, "review:BAD5:15:4", {"version": "direction-context-v5-bos-4h-zone", "side": "BUY", "zone_observation": {"status":"unavailable", "event_time_verified":False}})
            db.execute("INSERT INTO signal_alerts VALUES (?,?,?,?,?,?)", ("review:BAD6:15:5", "sent", stamp, stamp, "fixture invalid v6", "review"))
            save(db, "review:BAD6:15:5", {"version": "direction-context-v6-level-age-moscow-policy", "side": "BUY", "zone_observation": {"status":"unavailable", "event_time_verified":False}})
            db.execute("INSERT INTO signal_alerts VALUES (?,?,?,?,?,?)", ("review:CHAIN:15:6", "sent", stamp, stamp, "fixture chain", "sweep_choch_confirmed"))
            save(db, "review:CHAIN:15:6", {"version": "sweep-choch-v1-level-age-moscow-policy",
                 "side": "BUY", "setup_sequence": {"status": "confirmed"},
                 "baseline": {"method": "event_close_v1", "price": 100,
                              "event_ms": start, "source": "fixture"}})
            db.execute("""INSERT INTO signal_outcomes
                (alert_id,symbol,side,signal_interval,close_ms,reference_close)
                VALUES (?,?,?,?,?,?)""", ("review:CHAIN:15:6", "CHAIN", "BUY",
                "15", start, 100))
            db.execute("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?)", ("TEST", "15", start, start + STEP_MS - 1, 100, 104, 98, 102))
            db.commit()
            result = env["read_signals"](tmp.name)
            self.assertNotIn("BAD", [x["symbol"] for x in result["signals"]])
            self.assertNotIn("BAD5", [x["symbol"] for x in result["signals"]])
            self.assertNotIn("BAD6", [x["symbol"] for x in result["signals"]])
            current = next(x for x in result["signals"] if x["symbol"] == "TEST")
            old = next(x for x in result["signals"] if x["symbol"] == "OLD")
            self.assertEqual(current["scenario"]["side"], "SELL")
            self.assertAlmostEqual(current["movement"]["mfe_pct"], 2)
            self.assertAlmostEqual(current["movement"]["max_up_pct"], 4)
            self.assertEqual(old["scenario"]["status"], "historical_unverified")
            self.assertEqual(old["movement"]["status"], "data_unavailable")
            chain = next(x for x in result["signals"] if x["symbol"] == "CHAIN")
            self.assertEqual(chain["setup_type"], "sweep_choch_confirmed")
            self.assertEqual(result["strategy_stats"]["sent"], 1)
            self.assertEqual(result["strategy_stats"]["measured"], 1)
            self.assertEqual(result["strategy_stats"]["awaiting_measurement"], 0)
            db.close()


if __name__ == "__main__":
    unittest.main()

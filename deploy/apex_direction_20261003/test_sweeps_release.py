import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).parent
BOT = ROOT / "bot"


def load_sweeps_with_stubs():
    """Load the release detector without importing the full bot dependency tree."""
    saved = {name: sys.modules.get(name) for name in ("sessions", "structure")}
    sessions = ModuleType("sessions")
    sessions.closed_asia_range = lambda *args: None
    sessions.session_at = lambda *args: "DEAD_ZONE"
    structure = ModuleType("structure")
    structure.confirmed_pivots = lambda bars: ([], [5, 12])
    sys.modules["sessions"] = sessions
    sys.modules["structure"] = structure
    try:
        spec = importlib.util.spec_from_file_location("release_sweeps", BOT / "sweeps.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def test_equal_level_pattern_is_not_pre_filtered_by_base_volume():
    sweeps = load_sweeps_with_stubs()
    bars = [{"start_ms": i * 900_000, "end_ms": (i + 1) * 900_000 - 1,
             "open": 100.0, "high": 105.0, "low": 99.0, "close": 100.0,
             "volume": 0.0} for i in range(21)]
    bars[5]["low"] = 95.0
    bars[12]["low"] = 95.1
    bars[-1].update(open=98.0, close=99.0, low=94.0)
    result = sweeps.equal_level_sweep(bars)
    assert result["code"] == "equal_level_sweep"
    assert result["direction"] == "BUY"
    assert result["level"] == 95.0
    assert result["end_ms"] == bars[-1]["end_ms"]
    assert result["wick_body_ratio"] >= 0.5
    assert result["penetration_bps"] >= 5
    assert result["level_known_ms"] < bars[-1]["start_ms"]


def test_equal_level_pattern_remains_closed_bar_and_wick_reclaim_based():
    sweeps = load_sweeps_with_stubs()
    bars = [{"start_ms": i * 900_000, "end_ms": (i + 1) * 900_000 - 1,
             "open": 100.0, "high": 105.0, "low": 99.0, "close": 100.0}
            for i in range(21)]
    bars[5]["low"] = 95.0
    bars[12]["low"] = 95.1
    bars[-1].update(open=98.0, close=99.0, low=94.0)
    assert sweeps.equal_level_sweep(bars)
    bars[-1]["close"] = 94.5  # no reclaim/close back above the level
    assert sweeps.equal_level_sweep(bars) is None


def test_confirmed_4h_equal_lows_can_be_swept_by_current_lower_timeframe_bar():
    sweeps = load_sweeps_with_stubs()
    step = 14_400_000
    h4 = [{"start_ms": i * step, "end_ms": (i + 1) * step - 1,
           "open": 101.0, "high": 102.0, "low": 99.0, "close": 101.0}
          for i in range(20)]
    h4[5]["low"] = 100.0
    h4[12]["low"] = 100.1
    current = {"start_ms": 20 * step, "end_ms": 20 * step + 900_000 - 1,
               "open": 100.5, "high": 101.0, "low": 99.8, "close": 100.4}
    found = sweeps.h4_equal_level_sweeps(current, h4)
    assert found[0]["direction"] == "BUY"
    assert found[0]["level"] == 100.0
    assert found[0]["code"] == "h4_equal_level_sweep"
    assert found[0]["level_known_ms"] <= current["start_ms"]

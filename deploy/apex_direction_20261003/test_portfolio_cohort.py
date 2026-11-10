"""Portfolio cohort isolation; synthetic rows are not performance evidence."""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "dashboard"))
from portfolio import read_portfolio


def test_paper_portfolio_uses_only_confirmed_sweep_choch_scenarios():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
      CREATE TABLE signal_alerts(id TEXT PRIMARY KEY,status TEXT,sent_utc TEXT);
      CREATE TABLE signal_outcomes(alert_id TEXT PRIMARY KEY,symbol TEXT,side TEXT);
      CREATE TABLE signal_scenarios(alert_id TEXT PRIMARY KEY,contract_json TEXT);
      CREATE TABLE signal_followthrough(alert_id TEXT PRIMARY KEY,status TEXT,
        reference_start_ms INTEGER,reference_open REAL,last_closed_ms INTEGER,
        observed_bars INTEGER,expected_bars INTEGER);
      CREATE TABLE candles(symbol TEXT,interval TEXT,start_ms INTEGER,end_ms INTEGER,close REAL);
    """)
    chain_id = "review:CHAIN:15:1"
    old_id = "review:OLD:15:2"
    db.execute("INSERT INTO signal_alerts VALUES (?,?,?)", (chain_id, "sent", "t1"))
    db.execute("INSERT INTO signal_alerts VALUES (?,?,?)", (old_id, "sent", "t2"))
    db.execute("INSERT INTO signal_outcomes VALUES (?,?,?)", (chain_id, "CHAIN", "BUY"))
    db.execute("INSERT INTO signal_outcomes VALUES (?,?,?)", (old_id, "OLD", "BUY"))
    db.execute("INSERT INTO signal_scenarios VALUES (?,?)", (chain_id,
        '{"version":"sweep-choch-v1-level-age-moscow-policy","side":"BUY",'
        '"setup_sequence":{"status":"confirmed"}}'))
    db.execute("INSERT INTO signal_scenarios VALUES (?,?)", (old_id,
        '{"version":"direction-context-v6-level-age-moscow-policy","side":"BUY"}'))
    start, step = 900_000, 900_000
    db.execute("INSERT INTO signal_followthrough VALUES (?,?,?,?,?,?,?)",
               (chain_id, "complete", start, 100, start + 4 * step - 1, 4, 4))
    db.execute("INSERT INTO signal_followthrough VALUES (?,?,?,?,?,?,?)",
               (old_id, "complete", start, 100, start + 4 * step - 1, 4, 4))
    for i, close in enumerate((101, 102, 103, 104)):
        db.execute("INSERT INTO candles VALUES (?,?,?,?,?)",
                   ("CHAIN", "15", start + i * step, start + (i + 1) * step - 1, close))
        db.execute("INSERT INTO candles VALUES (?,?,?,?,?)",
                   ("OLD", "15", start + i * step, start + (i + 1) * step - 1, close))
    result = read_portfolio(db)
    assert result["model_version"] == "sweep-choch-v1-level-age-moscow-policy"
    assert result["admitted"] == 1
    assert result["completed"] == 1
    assert result["open"] == 0
    assert result["pnl_usdt"] == 4
    db.close()

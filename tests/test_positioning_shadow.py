#!/usr/bin/env python3
"""Contract checks for independent Positioning V1 collection."""
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from positioning_shadow import normalize_snapshot, public_state, select_universe


def main():
    payload = {"retCode": 0, "time": "1760000000000", "result": {"list": [
        {"symbol": "LOWUSDT", "lastPrice": "1", "openInterest": "5", "openInterestValue": "5", "turnover24h": "100", "fundingRate": "0"},
        {"symbol": "TOPUSDT", "lastPrice": "2", "openInterest": "10", "openInterestValue": "20", "turnover24h": "200", "fundingRate": "0.0001"},
        {"symbol": "BTCUSD", "lastPrice": "1", "openInterest": "5", "openInterestValue": "5", "turnover24h": "999"},
        {"symbol": "BADUSDT", "lastPrice": "1", "openInterestValue": "0", "turnover24h": "999"},
    ]}}
    assert [row["symbol"] for row in select_universe(payload["result"]["list"])] == ["TOPUSDT", "LOWUSDT"]
    rows = normalize_snapshot(payload, datetime(2026, 9, 17, tzinfo=timezone.utc))
    assert [row["symbol"] for row in rows] == ["TOPUSDT", "LOWUSDT"]
    assert rows[0]["source_timestamp_utc"] and rows[0]["server_received_at_utc"]
    state = public_state(rows)
    assert state["mode"] == "shadow" and state["shortlist"] == [] and state["cases"] == []
    assert state["outcomes"]["available"] == 0
    print("positioning shadow contract OK")


if __name__ == "__main__":
    main()

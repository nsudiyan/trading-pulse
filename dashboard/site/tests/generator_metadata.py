#!/usr/bin/env python3
"""Offline contract test for new radar metadata and feed relay.

It stubs detector-only imports; no market request, Telegram call, mutation of
the source repository or deployment is performed.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path

# Original source, not the temporary staging copy.
ROOT = Path(__file__).resolve().parents[3]

# vol_radar imports these to run a full scan, but the metadata writer does not.
sys.modules.setdefault("radar", types.SimpleNamespace(
    build_radar_message=lambda *args, **kwargs: "", radar_buttons=lambda *args, **kwargs: None
))
sys.modules.setdefault("volume_profile", types.SimpleNamespace(fetch_klines=lambda *args, **kwargs: []))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


radar = load("source_vol_radar", ROOT / "vol_radar.py")
feed = load("source_dashboard_feed", ROOT / "dashboard" / "dashboard_feed.py")

with tempfile.TemporaryDirectory() as tmp:
    journal = Path(tmp) / "radar_event_metadata.jsonl"
    radar.EVENT_META_PATH = journal
    receipt = datetime(2026, 9, 14, 15, 0, 5, tzinfo=timezone.utc)
    radar._log_event_metadata("AAAUSDT", {
        "closed_bar_open_ms": 1_789_396_200_000,  # 2026-09-14T14:30:00Z
        "closed_bar_volume": 1234.5,
        "vol_ratio": 6.25,
    }, 1.25, "30", receipt)
    row = json.loads(journal.read_text(encoding="utf-8").strip())
    assert row["source_bar_open_utc"] == "2026-09-14T14:30:00Z"
    assert row["source_timestamp_utc"] == "2026-09-14T15:00:00Z"
    assert row["server_received_at_utc"] == "2026-09-14T15:00:05Z"
    assert row["venue"] == "BYBIT" and row["provider"] == "bybit.v5.market.kline"
    assert row["timeframe"] == "30m" and row["volume"] == 1234.5
    assert row["liquidity_verified"] is False

    feed.RADAR_EVENT_META_PATH = journal
    feed.utcnow = lambda: datetime(2026, 9, 14, 15, 0, 30, tzinfo=timezone.utc)
    relayed = feed.collect_raw_market_events()
    assert len(relayed) == 1
    assert relayed[0]["sourceTimestampUtc"] == "2026-09-14T15:00:00Z"
    assert relayed[0]["volume"] == 1234.5
    assert relayed[0]["liquidityVerified"] is False

print("generator metadata and feed relay contract OK")

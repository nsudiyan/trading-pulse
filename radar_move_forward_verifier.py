#!/usr/bin/env python3
"""Independent structural verifier for frozen H-RADAR-MOVE-01 forward snapshots.

It does not import the collector.  It recomputes matched controls from each
append-only snapshot and rejects changed SHA/version, malformed windows,
duplicate IDs, or any control that was a raw detector hit.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "outcomes"
PREREG = OUT / "studies" / "2026-07-13_H-RADAR-MOVE-01_FORWARD_PREREG.md"
RECEIPT = OUT / "studies" / "2026-07-13_H-RADAR-MOVE-01_FORWARD_FREEZE_RECEIPT.json"
SNAPSHOTS = OUT / "radar_move_forward_snapshots.jsonl"
VERSION = "H-RADAR-MOVE-01-FWD-v1"
K = 5
MAX_DISTANCE = .20


def rows(path: Path) -> list[dict]:
    if not path.exists(): return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def percentiles(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0])); result = {}; i = 0
    while i < len(ordered):
        j = i + 1
        while j < len(ordered) and ordered[j][1] == ordered[i][1]: j += 1
        for symbol, _ in ordered[i:j]: result[symbol] = ((i + j - 1) / 2) / len(ordered)
        i = j
    return result


def expected_controls(snapshot: dict) -> list[str] | None:
    universe = snapshot["snapshot_rows"]
    valid = [row for row in universe if row.get("pre_range") is not None]
    by_symbol = {row["symbol"]: row for row in valid}
    target = by_symbol.get(snapshot["symbol"])
    if target is None or len(universe) < 30: return None
    pctl = percentiles({row["symbol"]: float(row["pre_range"]) for row in valid})
    tq = min(4, 5 * int(target["rank"]) // len(universe)); candidates = []
    for row in valid:
        symbol = row["symbol"]
        if symbol == snapshot["symbol"] or row.get("raw_hit"): continue
        if min(4, 5 * int(row["rank"]) // len(universe)) != tq: continue
        d = abs(pctl[symbol] - pctl[snapshot["symbol"]])
        if d <= MAX_DISTANCE: candidates.append((d, symbol))
    candidates.sort()
    return [symbol for _, symbol in candidates[:K]] if len(candidates) >= K else None


def main() -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    assert receipt["protocol_version"] == VERSION
    assert receipt["prereg_sha256"] == hashlib.sha256(PREREG.read_bytes()).hexdigest()
    seen = set(); checked = 0
    for snapshot in rows(SNAPSHOTS):
        assert snapshot["id"] not in seen; seen.add(snapshot["id"])
        assert snapshot["protocol_version"] == VERSION
        assert snapshot["prereg_sha256"] == receipt["prereg_sha256"]
        assert len(snapshot["snapshot_rows"]) == snapshot["universe_size"]
        expected = expected_controls(snapshot)
        if snapshot["match_status"] == "matched":
            assert expected == snapshot["controls"], (snapshot["id"], expected, snapshot["controls"])
            assert len(snapshot["controls"]) == K
        else:
            assert expected is None and not snapshot["controls"]
        checked += 1
    print(json.dumps({"checked": checked, "failures": 0, "claim": "forward snapshot invariants passed"}))


if __name__ == "__main__": main()

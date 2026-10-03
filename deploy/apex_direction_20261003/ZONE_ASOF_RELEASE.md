# Issue 15: event-time zone provenance

Only closed bars with end_ms <= event_end_ms are admitted to generic/strong
sweep selection snapshots. Ordering is deterministic, identical duplicates
collapse, conflicting duplicates fail closed. Existing route thresholds and
premium/discount comparisons remain unchanged. No VP reinterpretation.

New immutable v3 scenario metadata records source_route, the actual source
15m close price/time, 4H geometry/source close time and event timestamp. Verified
requires valid geometry, finite price, matching zone and fresh as-of timestamps.
Missing data remains unknown. Historical contracts are not rewritten.

Tests: test_zone_asof.py (4) exercises the real route function bodies with
controlled indicator fixtures: future exclusion, replay/backfill equivalence,
duplicates, missing, flat/equilibrium and invalid future provenance. This is
software validation, not an indicator backtest or profitability test.
test_contracts.py (12) and test_ui.cjs (11) remain required. Real production
imports and HTTP/service/source-hash smoke are release checks.

Baseline hashes:
- setup_layers.py: 370ab7679858494fec777b1d45ab2f8bbc848407268431435c9a298e78f169e8
- strong_sweep.py: 0341eacc7f496cfe302152d601afae33681639f2dc3dc6b10f7c2f00ea0daff9
- scenario_contract.py: 5b6da494c8aca9851d9e284af4594a8d4caf46811b4437788dc7715e8d428c2a
- app.js: b4ccd7485ce53cbd4c07787eaafe6be33f7320eb452010d30cc3f3526fcb4757

Planned backup /opt/pulse-zone-asof15-backup-20261003. Restore the four backed-up
files and restart news-chart-bot/apex-dashboard if a release check fails.
Database, settings, secrets and historical research are outside this change.

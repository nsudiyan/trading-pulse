# Zone provenance acceptance follow-up (2026-10-03)

This patch is presentation/metadata/test-only. No new delivery gate, execution
signal, historical rewrite, or profitability claim. Existing BUY/SELL remains a
directional observation, `entry_confirmed=false` and NO-TRADE.

## Two different layers

The direction classifier requires non-conflicting directional structure/sweep
and agreeing fresh closed 4H/day trends. It does not use zone. The 90-case matrix
tests that metadata contract, NOT a mandatory zone trading rule.

The existing delivery pipeline has separate selection routes. Read-only source
inspection of `/opt/news-chart-bot/strong_sweep.py` confirms strong_sweep_review
already requires BUY/discount or SELL/premium, current closed 15m sweep, matching
4H trend, non-opposing D/W, configured volume/liquidity/family thresholds.
This explains why AAVE's strong-sweep route can use discount despite the
classifier being zone-independent. Premium/discount is a pivot midpoint measure,
not INSIDE_VA/above-VAH (Volume Profile).

`setup_layers.review_gate` is a different selection route. Its 4H metrics are
event-time filtered, but its zone price reads `last_15m[-1]` without an end_ms
filter. The generic gate does not return the source price timestamp. This is an
acceptance gap, not proof of an actual contaminated historical event. No fix to
that gate is included or authorized while the user decides zone policy.

## Metadata

New contracts preserve the reported selection zone and route. Geometry is only
retained if available and finite with a valid midpoint. Strong-sweep's verified
current-15m source permits event-time/price provenance. Other routes have null
source timestamp/price; the event being annotated is stored separately. No
historical zone is reconstructed. Existing stored v1 contracts are unchanged.

## Verification

`python3 deploy/apex_direction_20261003/test_contracts.py`: 12 tests pass,
including 90 direction/trend/zone combinations, flat BUY/SELL/neutral paths,
incremental waiting/tracking/missing/recovery, future-candle exclusion and
duplicate idempotence. `node .../test_ui.cjs`: 11 existing UI status cases pass.
`node --check .../dashboard/app.js` and `git diff --check` pass.

The added matrix is synthetic software coverage, not strategy evidence. No
backtest, new live alert, or historical data mutation was performed. Issues #9
and #10 remain open; a zone gate and all legacy producer inventory are not
claimed complete. This file does not claim production deployment of this patch.

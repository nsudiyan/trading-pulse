# Issue 21: event-baseline descriptive horizons

This release does NOT change BUY/SELL classification, quality gates or 5m policy.
The separate BOS classification request is blocked pending direct approval.

For new immutable contracts, store signal candle close and event timestamp
(close boundary, end_ms+1) at creation. Never reconstruct a missing baseline.
The new API field event_excursions measures 1h/4h/24h from that event time;
only complete closed 15m OHLC with start >= event and close boundary <= horizon
end/current time are eligible. MFE is nonnegative, MAE nonpositive, both relative
to baseline, with matching extremum prices. BUY and SELL reverse favorable and
adverse directions. Neutral observations are not directional samples.

Incomplete windows remain explicitly incomplete; missing/conflicting/invalid
closed data exposes unavailable rather than a fabricated excursion. Idempotent
duplicates collapse. Intrabar sequence, fills, fees and profit are not inferred.
Old post-send-open movement remains a separate labelled view. Legacy data and
its recorded outcomes are untouched. No historical baseline backfill.

Validation: 3 horizon tests + 12 contracts + 2 fail-closed guard + 4 as-of tests,
11 JS status cases, JS syntax and diff checks. No artificial Telegram messages.

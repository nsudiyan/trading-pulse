# Alert age, Moscow time, and timeframe policy

This change adds three event-time rules to the Bybit review-alert path:

1. A BOS direction is eligible only when the source pivot-confirmation time and
   price are present and the level age is within its timeframe cap. Unknown,
   future, invalid, or stale source metadata fails closed and is stored with a
   `suppressed_stale_level:*` reason. Age starts when the pivot becomes knowable
   (the close of the right-hand confirmation candle), not at an invented zero
   age. Allowed alerts show source timeframe, level price, elapsed age, bar
   count, and freshness band.
2. Event candle closes and session labels use fixed UTC+3 Moscow time. Windows
   are the user's strategy convention, not universal market-session boundaries:
   Asia 04:00–11:00, London KZ 11:00–13:00, London Close 13:00–16:00, New York
   KZ 16:00–18:00, New York PM 18:00–20:00, and outside-window otherwise.
3. 15m, 1h, and 4h can emit alerts at closed-bar time. 1D can emit only when its
   close timestamp is in London or New York KZ. 1W and 5m cannot emit alerts.
   Weekly and daily data remain subscribed and retained as higher-timeframe
   context; only alert emission is restricted.

Important Bybit timing consequence: Bybit crypto daily candles close at 00:00
UTC, or 03:00 Moscow. With the requested kill-zone windows, that means a direct
1D-close alert is normally suppressed every day. Daily bars still contribute to
context for eligible lower-timeframe alerts. Emitting a daily setup later during
the next KZ would require a separate delayed/scheduled-alert design and is not
silently introduced here.

The alert-age caps from the provided specification are: 5m 8 bars, 15m 16 bars,
1h 24 bars, 4h 20 bars, 1D 10 bars, 1W 8 bars. Red/stale means over the cap and
is suppressed. The caps are user-selected operational heuristics; they are not
validated for profitability or market efficacy. Existing BOS/4h/zone BUY/SELL
matrix and `entry_confirmed=false` remain unchanged.

The immutable scenario contract advances to
`direction-context-v6-level-age-moscow-policy`; prior records and outcomes are
not rewritten. The dashboard's original UTC acknowledgement/outcome fields stay
UTC and are explicitly distinct from the signal candle's Moscow event time.

Validation is synthetic software testing only. A passing suite does not prove
live delivery or trading performance. Deployment requires live service/API and
source-hash checks; no test message should be sent to Telegram.

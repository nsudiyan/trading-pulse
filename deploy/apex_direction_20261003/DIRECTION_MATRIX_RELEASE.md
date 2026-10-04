# BUY/SELL matrix release

This version applies the user's confirmed direction rule uniformly after generic
quality selection and the `strong_sweep_review` fallback:

| Closed 15m BOS | Latest closed 4h trend | Event-time-verified 4h zone | Result |
|---|---|---|---|
| Up | Bullish | Discount | BUY |
| Down | Bearish | Premium | SELL |
| Any other combination, missing/stale input, or conflicting BOS | Any | Any | suppressed (`suppressed_direction:conflict`) |

The BOS trigger is taken from structural findings; a sweep alone cannot create a
direction. The daily trend and the sweep's displayed side do not replace this
matrix. The same final helper gates both alert routes. Suppressed observations
are persisted with their reason and do not enter the Telegram pending queue.

Telegram and dashboard headings use `📈 BUY SYMBOL [LINEAR]` or
`📉 SELL SYMBOL [LINEAR]` (inverse symbols are labeled `[INVERSE]`). Neutral
items remain `NO-TRADE`. `entry_confirmed=false` is unchanged: a direction label
is a market observation, not an entry, stop, target, fill, or PnL claim.

The dashboard/API direction is read from the same immutable scenario contract.
New contracts use `direction-context-v6-level-age-moscow-policy`; historical
v1-v5 records and outcome rows are not rewritten. v4-v6 invalid provenance remains excluded
by the API while pre-v4 legacy history remains available with its unknown-data
label.

Validation: full local fixture suite, including all matrix combinations,
generic/strong-sweep shared-gate wiring, both Telegram formatters, API handling
of invalid v4/v5, and existing provenance/horizon guards. Synthetic fixtures
are not live delivery or trading-performance evidence.

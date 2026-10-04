# Sweep → CHoCH alert release — 2026-10-04

## What this release changes

- All directional Telegram alert routes now require a confirmed, event-time-ordered sweep → later CHoCH sequence on a closed 15m, 1h, or 4h candle. The SQLite state machine persists across restarts, expires after at most eight later bars, resets on candle gaps, and invalidates if price closes through the swept extreme.
- Sweep candidates use equal highs/lows, confirmed 4h equal levels and swings, or a completed Asia range. A sweep requires a wick/body ratio of at least 0.5, at least 5 bps beyond the level, and a closed-candle reclaim. Simultaneous opposing candidates are suppressed as ambiguous.
- Activity is measured on the instrument's native quote-notional field against the prior 14 closed candles, with provisional tiers keyed to preceding 24h turnover and a hard minimum of 1.8× from the supplied specification. Missing turnover/history fails closed. These thresholds are heuristics; OHLCV cannot prove stop execution.
- A resulting BUY/SELL still requires a fresh, verified 4h pivot-range zone and the agreed matrix: BUY = downside sweep → later upward CHoCH + closed 4h bullish + discount; SELL = upside sweep → later downward CHoCH + closed 4h bearish + premium. Legacy 15m/1D/1W alignment and VP-profile requirements remain available for legacy overview alerts but do not supersede this explicit setup matrix.
- Direct daily/weekly alerts are excluded. 15m/1h signals use the configured Moscow windows; 4h is allowed outside the configured dead zone. BTC's 45-minute move guard, liquidity floor, cooldown, provenance, and delivery-age checks remain.
- The Telegram heading is explicit `📈 BUY SYMBOL [LINEAR]` or `📉 SELL SYMBOL [LINEAR]`; the body identifies the exact sweep, CHoCH close, buffer, age and turnover evidence. Alerts remain observational: `entry_confirmed=false`, `NO-TRADE`; no executable entry/stop/target is claimed.
- APEX distinguishes the new model cohort from legacy reviews, and the paper portfolio is filtered to confirmed sweep→CHoCH scenarios. MFE/MAE are price excursions, not realized PnL; paper equity excludes fees, funding and slippage and is not evidence of an edge.

## Verification

- `python3 -m pytest -q deploy/apex_direction_20261003`: 59 passing tests.
- `node deploy/apex_direction_20261003/test_ui.cjs`: 19 UI behavior checks passed.
- `py_compile`, `git diff --check` passed.
- Synthetic tests cover sequence ordering, closed-bar-only pivots, persistence, gaps, timeout, invalidation, opposing sweeps, volume tiers, direction matrix, session rules, BTC gate, dashboard cohort, and historical-vs-current statistics. They are not backtest evidence.

## Remaining proof / limitations

- No first live alert from this version exists until the market produces the full sequence and every gate passes; no fake Telegram message is sent to simulate it.
- No validated executable entry, stop-loss, or take-profit method is released. The supplied candidate formulas are not empirically validated and should not be represented as a live trading plan.
- Signal quality/edge cannot be assessed until an adequate cohort of new-model outcomes has matured; current historical alerts are not reclassified as this model.

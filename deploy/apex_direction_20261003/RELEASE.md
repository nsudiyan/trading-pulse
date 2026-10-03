# APEX direction/movement release — 2026-10-03 MSK

Issues: trading-pulse #9, #10, #11. Synthetic fixtures are software tests, not evidence of profitable trading.

## Changes

- `bot/scenario_contract.py`: immutable presentation contract. BUY/SELL requires explicit structure/sweep trigger and agreement of actually closed 4H and daily EMA context. Conflicting directions remain neutral, never inverted. Missing/future/older-than-one-timeframe context remains unconfirmed. This is a conservative display rule, not a proven strategy/filter or entry permission. No new notification filter was applied.
- `bot/market.py`: one context for Telegram and dashboard; additive `signal_scenarios`, inserted only for a newly created alert, no historical retrofit. Preserved original outcome cohort and trade-selection logic. NO-TRADE survives message truncation.
- `bot/structure.py`: level age since pivot confirmation, both elapsed h/min and bars; no arbitrary fresh/old cutoff.
- `dashboard/movement.py`: first full 15m bar after actual Telegram acknowledgement is the fixed reference. Closed contiguous OHLC only, 72h maximum. Current close return, maximum upward and downward price excursion are raw price changes, not PnL or peak-to-trough drawdown. Directional MFE/MAE shown only with stored new scenario. Missing/invalid/conflicting bars produce unavailable status, not fabricated results.
- `dashboard/server.py`: reads contract and raw path, preserves existing research/portfolio output. Historical rows explicitly have unverified direction. No DB writes in the API.
- `dashboard/app.js`: price precision, raw movement labels, direction/no direction and anchor/source/time/status details. Historical Telegram text is not rewritten.

## Verification

`python3 test_contracts.py`: 8 passing test methods including direction matrix, missing/stale/future contexts, duplicate immutable write, conflicting events, raw BUY/SELL/neutral paths, missing/future/NaN/duplicate bars, actual Telegram formatter truncation, and actual server read function on synthetic SQLite.

Python compile and `node --check dashboard/app.js` passed. Remote staging imported actual dependencies successfully. Read-only production DB smoke on 5 latest records returned tracking status.

Four pre-release production hashes matched baseline exactly. Release made targeted backup at `/opt/pulse-direction-backup-20261003`, changed 6 files, restarted only news-chart-bot and apex-dashboard. Both active/running, NRestarts=0 at 00:12:24 MSK. WS startup logged 23 shards for 873 symbols. Public API `https://apex.147.45.175.24.nip.io/api/signals?limit=1` read successfully at 2026-10-02T21:14:09Z. Public app.js SHA256 matches local: `1207738ec14ceafbbe06219a85e1e66b7f9223b4e5c5a25ffb9680e3fb593462`.

No new actual Telegram message delivered at the post-release check: latest sent remained 2026-10-01T21:57:04.711706Z, 927 sent records. Thus new-message end-to-end delivery remains unverified, not claimed complete. No test Telegram message sent. Browser visual test blocked: in-app browser unavailable. Existing uncommitted source files in repository untouched.

## Rollback

Copy backup bot market.py/structure.py into /opt/news-chart-bot and backup dashboard server.py/app.js into /opt/apex-dashboard, then restart those two services. New modules and additive scenario table may remain unused; do not delete or restore historical DB. `release.sh` documents exact guarded operations and baseline hashes; it is one-shot and refuses an existing backup directory.

## Limits

No win-rate/strategy edge validation, no new stops/targets/freshness trading gates. INSIDE_VA is not interpreted as premium/discount. Old research aggregates/portfolio retain their earlier methodology and do not become validated by this release. Stored candle retention or missing intervals can make older paths unavailable. Candle OHLC cannot resolve intrabar ordering or execution. No claim of zero latency.

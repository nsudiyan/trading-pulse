# Issue #13 — movement status presentation

Released 2026-10-02T21:24:37Z (03 October 00:24:37 MSK). Only production `/opt/apex-dashboard/app.js` changed; no service restart, DB or research-method change.

Baseline SHA256: `1207738ec14ceafbbe06219a85e1e66b7f9223b4e5c5a25ffb9680e3fb593462`.
Published SHA256 verified: `b8dee811a58e7978e21ade3c698fdce75f9bd931416a9588adb6132dbf0a0edb`.
Rollback copy: `/opt/pulse-direction-backup-20261003/app-before-status-fix.js`.

Card, ARIA, detail badge and filter now share displayStatus/matchesStatus. Existing HTML filter names map to movement tracking/complete/waiting/unavailable/invalid/unknown. Legacy archive applies only when no movement object exists and legacy path_status declares archive. Research path_status and aggregate statistics remain unchanged. Missing/unknown movement statuses route to No data, not success.

`node test_ui.cjs`: 11 UI status cases pass (including tracking, missing, conflict, invalid, unknown, legacy and immutable input). Eight Python contract/formatter/API-fixture tests still pass; node syntax check passes.

## Live visual QA through Chrome/CUA

Before fix, ZSUSDT visibly had data_unavailable but No data filter was empty, reproducing #13. SONYUSDT had tracking but archive badge.

After fix, live cards and details show Наблюдается and appear under В работе; raw numbers, anchor and source/time display correctly. At 2026-10-02T21:26:47Z the API had 100/100 latest rows tracking: background data recovery filled previously missing intervals. Therefore No data is correctly empty at that snapshot. A live nonempty No data state after the fix was not available; its behavior is verified by fixture tests, not by invented live data. No production data injected for QA.

Screenshot/DOM visually inspected through Chrome; previous unavailable in-app browser limitation is superseded for desktop QA. Natural post-release sent count remains 0 at the read-only delivery check; last all-status sent timestamp remains 2026-10-01T21:57:04.711706Z (includes administrative alerts). No synthetic Telegram message was sent. Issue #11 must still await actual delivery of the new format.

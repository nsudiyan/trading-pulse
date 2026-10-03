# Issue 17: final fail-closed guard

v4 is the new-observation release boundary. All new review observations,
including neutral NO-TRADE, must have consistent verified zone provenance
before pending is created. Failed consistency is persisted as
suppressed_provenance/provenance_unverified. Existing failed quality gates keep
their original rejection reason. Sender rechecks the immutable persisted
contract before any Telegram call. Missing/malformed contracts fail closed.

APEX signals additionally excludes invalid v4 contracts. Historical v1-v3 and
legacy records are preserved, not relabelled as false; existing unknown-zone
disclosure remains. Channel mirror is unchanged. No retroactive DB rewrite.
The delivery recheck also protects any pending record lacking evidence; it
does not alter already-sent history.

Tests execute actual sender/API function bodies with an in-memory database and
a fake Telegram sender, never a real message. Valid pass sends once; false,
unavailable/missing/future/invalid geometry suppress; existing gate rejection
stays suppressed; duplicate processing is idempotent. The API fixture excludes
an invalid-v4 sent record while preserving legacy records.

Required checks: 2 new guard tests, 12 contract tests, 4 as-of route tests and
11 JS status cases. No threshold, channel, secrets or historical modifications.

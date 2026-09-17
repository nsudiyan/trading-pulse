const assert = require('node:assert/strict');
const { buildModel } = require('../lib/model');
const feed = require('../feed.json');
const model = buildModel(feed, Date.now());
assert.ok(Array.isArray(model.episodes));
assert.equal(model.executionMode, 'DISABLED');
assert.ok(['healthy', 'degraded', 'unavailable', 'legacy_only', 'legacy_only_stale'].includes(model.systemStatus));
assert.ok(model.episodes.every((episode) => episode.status !== 'research_only' || episode.dataQuality.status === 'verified'));
console.log(`smoke OK: ${model.episodes.length} episodes, system=${model.systemStatus}`);

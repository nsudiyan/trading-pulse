const test = require('node:test');
const assert = require('node:assert/strict');
const { createHandler } = require('../api/research');

function response() { const output = { headers: {}, statusCode: null, body: null }; return { output, setHeader(k, v) { output.headers[k] = v; }, status(code) { output.statusCode = code; return this; }, json(value) { output.body = value; } }; }
const feed = { generated_at: '2026-09-14T15:02:00.000Z', raw_market_events: [{ id: 'x', symbol: 'AAAUSDT', venue: 'BYBIT', provider: 'bybit', detectedAtUtc: '2026-09-14T15:01:00.000Z', sourceTimestampUtc: '2026-09-14T15:01:00.000Z', serverReceivedAtUtc: '2026-09-14T15:01:00.100Z', timeframe: '30m', eventType: 'radar', methodVersion: 'v1', price: 1, volume: 1, dataQuality: 'verified', liquidityVerified: true }] };

test('research routes expose a read-only model and episode detail', async () => {
  const handler = createHandler(async () => feed, () => Date.parse('2026-09-14T15:02:30.000Z'));
  const overview = response(); await handler({ method: 'GET', query: { path: 'overview' } }, overview);
  assert.equal(overview.output.statusCode, 200); assert.equal(overview.output.body.episodes.length, 1);
  const id = overview.output.body.episodes[0].id; const detail = response(); await handler({ method: 'GET', query: { path: `episodes/${id}/raw-alerts` } }, detail);
  assert.equal(detail.output.statusCode, 200); assert.equal(detail.output.body.length, 1);
});

test('research API rejects mutation and unavailable feed fails closed', async () => {
  const handler = createHandler(async () => { throw Error('down'); }, () => Date.now());
  const post = response(); await handler({ method: 'POST', query: {} }, post); assert.equal(post.output.statusCode, 405);
  const unavailable = response(); await handler({ method: 'GET', query: {} }, unavailable); assert.equal(unavailable.output.statusCode, 503); assert.equal(unavailable.output.body.systemStatus, 'unavailable');
});

test('archive route retains original legacy source rows separately from current episodes', async () => {
  const legacy = { generated_at: '2026-09-14T15:02:00.000Z', diary: { records: [{ id: 'd1', symbol: 'AAAUSDT', outcome: { original: true } }] } };
  const handler = createHandler(async () => legacy, () => Date.parse('2026-09-14T15:02:30.000Z'));
  const archive = response(); await handler({ method: 'GET', query: { path: 'archive' } }, archive);
  assert.equal(archive.output.statusCode, 200);
  assert.equal(archive.output.body.status, 'legacy_provenance_incomplete');
  assert.equal(archive.output.body.sources[0].rows[0].outcome.original, true);
});

test('positioning route is read-only and fails closed without a published snapshot', async () => {
  const handler = createHandler(async () => ({ generated_at: '2026-09-14T15:02:00.000Z' }), () => Date.parse('2026-09-14T15:02:30.000Z'));
  const positioning = response(); await handler({ method: 'GET', query: { path: 'positioning' } }, positioning);
  assert.equal(positioning.output.statusCode, 200);
  assert.equal(positioning.output.body.mode, 'shadow');
  assert.equal(positioning.output.body.status, 'data_unavailable');
  assert.deepEqual(positioning.output.body.shortlist, []);
  assert.deepEqual(positioning.output.body.cases, []);
});

test('smart money lab route is read-only and exposes only structured cases', async () => {
  const handler = createHandler(async () => ({ generated_at: '2026-09-14T15:02:00.000Z', smart_money_lab: { status: 'published', cases: [] } }), () => Date.parse('2026-09-14T15:02:30.000Z'));
  const lab = response(); await handler({ method: 'GET', query: { path: 'smart-money-lab' } }, lab);
  assert.equal(lab.output.statusCode, 200);
  assert.equal(lab.output.body.status, 'published');
  assert.deepEqual(lab.output.body.cases, []);
});

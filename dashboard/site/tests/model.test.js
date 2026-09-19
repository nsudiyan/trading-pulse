const test = require('node:test');
const assert = require('node:assert/strict');
const { buildModel } = require('../lib/model');
const { radarEpisodes, displayMetrics, canRequestManualReview } = require('../lib/presentation');

const now = Date.parse('2026-09-14T15:02:30.000Z');
const complete = (extra = {}) => ({
  id: 'a', symbol: 'AAAUSDT', venue: 'BYBIT', provider: 'bybit-public', detectedAtUtc: '2026-09-14T15:01:00.000Z', sourceTimestampUtc: '2026-09-14T15:01:00.000Z', serverReceivedAtUtc: '2026-09-14T15:01:00.100Z', timeframe: '30m', eventType: 'radar', methodVersion: 'detector-v1', price: 1, volume: 100, dataQuality: 'verified', liquidityVerified: true, ...extra,
});
const feed = (rows, extra = {}) => ({ generated_at: '2026-09-14T15:02:00.000Z', raw_market_events: rows, ...extra });
const one = (value) => buildModel(value, now).episodes[0];

test('missing price, volume, and source timestamp are DATA UNAVAILABLE and absent from Radar', () => {
  for (const row of [complete({ price: null }), complete({ volume: null }), complete({ sourceTimestampUtc: null })]) {
    const episode = one(feed([row]));
    assert.equal(episode.status, 'data_unavailable');
    assert.equal(displayMetrics(episode), false);
    assert.equal(radarEpisodes(buildModel(feed([row]), now)).length, 0);
  }
});

test('stale verified source becomes NO TRADE', () => {
  const episode = one(feed([complete({ sourceTimestampUtc: '2026-09-14T14:50:00.000Z', serverReceivedAtUtc: '2026-09-14T14:50:00.100Z' })]));
  assert.equal(episode.status, 'no_trade');
  assert.match(episode.statusReasons[0], /устарели/);
});

test('complete current observation stays RESEARCH ONLY when order-book context is unavailable', () => {
  const episode = one(feed([complete({ liquidityVerified: false })]));
  assert.equal(episode.status, 'research_only');
  assert.match(episode.statusReasons[0], /Контекст стакана/);
  assert.equal(canRequestManualReview(episode), false);
});

test('new append-only radar metadata projects to one fresh RESEARCH ONLY episode', () => {
  const raw = {
    id: 'radar:AAAUSDT:2026-09-14T15:01:05Z', eventType: 'radar', symbol: 'AAAUSDT', venue: 'BYBIT', provider: 'bybit.v5.market.kline',
    detectedAtUtc: '2026-09-14T15:01:05Z', sourceTimestampUtc: '2026-09-14T15:01:00Z', serverReceivedAtUtc: '2026-09-14T15:01:05Z',
    timeframe: '30m', methodVersion: 'vol_radar.detect_spike.closed_bar/v1', price: 1.25, volume: 1234.5, vol_ratio: 6.25,
    dataQuality: 'verified', liquidityVerified: false,
  };
  const model = buildModel({ generated_at: '2026-09-14T15:02:00Z', raw_market_events: [raw] }, now);
  assert.equal(model.episodes.length, 1);
  assert.equal(model.episodes[0].status, 'research_only');
  assert.equal(model.episodes[0].dataQuality.status, 'verified');
  assert.equal(model.episodes[0].canonicalInstrumentId, 'BYBIT:AAAUSDT');
});

test('repeated compatible alerts become one deterministic episode', () => {
  const model = buildModel(feed([complete({ id: 'one' }), complete({ id: 'two', detectedAtUtc: '2026-09-14T15:10:00.000Z', sourceTimestampUtc: '2026-09-14T15:10:00.000Z', serverReceivedAtUtc: '2026-09-14T15:10:00.100Z' })]), now);
  assert.equal(model.episodes.length, 1);
  assert.equal(model.episodes[0].rawAlertCount, 2);
});

test('PumpWatch active block comes only from backend and cannot become manual review', () => {
  const episode = one(feed([complete({ reviewVerifiedAtUtc: '2026-09-14T15:01:30.000Z' })], { pumpwatch_states: [{ symbol: 'AAAUSDT', venue: 'BYBIT', status: 'active_block', reason: 'Вертикальное движение', startedAtUtc: '2026-09-14T15:00:00.000Z' }] }));
  assert.equal(episode.status, 'no_trade');
  assert.equal(episode.pumpWatch.status, 'active_block');
  assert.equal(canRequestManualReview(episode), false);
});

test('PumpWatch unblock removes only the published block', () => {
  const episode = one(feed([complete({ reviewVerifiedAtUtc: '2026-09-14T15:01:30.000Z' })], { pumpwatch_states: [{ symbol: 'AAAUSDT', venue: 'BYBIT', status: 'closed', reason: 'Проверка завершена', unblockedAtUtc: '2026-09-14T15:01:45.000Z' }] }));
  assert.equal(episode.status, 'manual_review');
});

test('source expiration and validation exclusion remain explicit', () => {
  const episode = one(feed([complete({ expiresAtUtc: '2026-09-14T15:00:00.000Z', validationEligibility: { eligible: false, reason: 'Неполный источник', excludedAtUtc: '2026-09-14T15:00:00.000Z', protocolVersion: 'p1' } })]));
  assert.equal(episode.status, 'expired');
  assert.equal(episode.validationEligibility.eligible, false);
});

test('method version separates otherwise identical events', () => {
  const model = buildModel(feed([complete({ id: 'a', methodVersion: 'v1' }), complete({ id: 'b', methodVersion: 'v2' })]), now);
  assert.equal(model.episodes.length, 2);
});

test('validation is not published without complete protocol and exclusions', () => {
  const model = buildModel(feed([], { forward_movement: { status: 'published', protocol_version: 'p1', methodology: true, controls: true, excluded_alerts: 1, exclusions: [] } }), now);
  assert.equal(model.validation.resultsAvailable, false);
  assert.equal(model.validation.conclusion, 'Статистическое подтверждение не опубликовано');
});

test('fresh legacy feed is separately archived while original records remain non-actionable', () => {
  const legacyFeed = {
    generated_at: '2026-09-14T15:02:00.000Z',
    live_signals: [{ id: 'legacy-live-1', symbol: 'AAAUSDT', source: 'radar', ts_utc: '2026-09-14T15:01:00.000Z', entry: 1 }],
    signals_history: [{ symbol: 'BBBUSD', source: 'radar', anchor_ts: '2026-09-13T00:00:00.000Z', outcome: { status: 'ok' } }],
    diary: { records: [{ id: 'diary-1', symbol: 'CCCUSDT', signal_ts: '2026-09-12T00:00:00.000Z', outcome: { retained: true } }] },
    rose: { tracks: [{ symbol: 'DDDUSDT', first_ts: '2026-09-11T00:00:00.000Z', peak24_pct: 4.2 }] },
    pump_muted: [{ symbol: 'EEEUSDT', reason: 'source value', ts: '2026-09-10T00:00:00.000Z' }],
  };
  const model = buildModel(legacyFeed, now);
  assert.equal(model.systemStatus, 'legacy_only');
  assert.equal(model.archive.status, 'legacy_provenance_incomplete');
  assert.equal(model.archive.totalRecords, 5);
  assert.equal(model.archive.sources.find((source) => source.id === 'diary_records').rows[0].outcome.retained, true);
  assert.equal(model.episodes.every((episode) => episode.status === 'data_unavailable'), true);
});

test('current live_signals become a sorted manual-check queue without leaking entry as live price', () => {
  const live = Array.from({ length: 37 }, (_, index) => ({
    id: `live-${index}`, symbol: `ALT${index}USDT`, source: index % 2 ? 'storm' : 'radar',
    ts_utc: `2026-09-14T15:${String(index).padStart(2, '0')}:00.000Z`, entry: index + 1,
  }));
  const model = buildModel({ generated_at: '2026-09-14T15:40:00.000Z', live_signals: live }, Date.parse('2026-09-14T15:40:01.000Z'));
  assert.equal(model.nowObservations.length, 37);
  assert.equal(model.nowObservations[0].symbol, 'ALT36USDT');
  assert.equal(model.nowObservations[0].status, 'manual_check');
  assert.equal(model.nowObservations[0].livePrice, null);
  assert.equal(model.nowObservations[0].liveChangePct, null);
  assert.deepEqual([...new Set(model.nowObservations.map((item) => item.sourceModule))].sort(), ['radar', 'storm']);
});

test('terminal focus limits raw intake to three fresh deduplicated radar volume observations', () => {
  const now = Date.parse('2026-09-14T15:40:00.000Z');
  const signals = [
    { id: 'a', symbol: 'AAAUSDT', source: 'radar', ts_utc: '2026-09-14T15:35:00.000Z', vol_ratio: 5.1 },
    { id: 'a-repeat', symbol: 'AAAUSDT', source: 'radar', ts_utc: '2026-09-14T15:36:00.000Z', vol_ratio: 6.2 },
    { id: 'b', symbol: 'BBBUSDT', source: 'radar', ts_utc: '2026-09-14T15:34:00.000Z', vol_ratio: 7.1, major_radar: true },
    { id: 'c', symbol: 'CCCUSDT', source: 'radar', ts_utc: '2026-09-14T15:33:00.000Z', vol_ratio: 8.1 },
    { id: 'd', symbol: 'DDDUSDT', source: 'radar', ts_utc: '2026-09-14T15:32:00.000Z', vol_ratio: 9.1 },
    { id: 'storm', symbol: 'EEEUSDT', source: 'storm', ts_utc: '2026-09-14T15:39:00.000Z', vol_ratio: 99 },
    { id: 'old', symbol: 'OLDUSDT', source: 'radar', ts_utc: '2026-09-14T15:10:00.000Z', vol_ratio: 99 },
    { id: 'small', symbol: 'SMALLUSDT', source: 'radar', ts_utc: '2026-09-14T15:39:00.000Z', vol_ratio: 4.9 },
  ];
  const model = buildModel({ generated_at: '2026-09-14T15:40:00.000Z', live_signals: signals }, now);
  assert.equal(model.nowObservations.length, 8);
  assert.equal(model.focusObservations.length, 3);
  assert.deepEqual(model.focusObservations.map((item) => item.symbol), ['BBBUSDT', 'DDDUSDT', 'CCCUSDT']);
  assert.equal(model.focusObservations.every((item) => item.sourceModule === 'radar' && item.relativeVolume >= 5), true);
});

test('positioning is a separate shadow layer and ignores invalid trade-like cases', () => {
  const model = buildModel({
    generated_at: '2026-09-14T15:40:00.000Z',
    positioning: {
      mode: 'shadow', status: 'collecting', generated_at_utc: '2026-09-14T15:40:00.000Z',
      source: { venue: 'BYBIT', provider: 'bybit.v5.market.tickers', category: 'linear' },
      universe: { selection: 'reported turnover24h', cap: 20, observed: 1 },
      snapshots: [{ symbol: 'ABCUSDT', venue: 'BYBIT', provider: 'bybit.v5.market.tickers', source_timestamp_utc: '2026-09-14T15:40:00.000Z', server_received_at_utc: '2026-09-14T15:40:01.000Z', data_quality: 'verified', raw: { last_price: 1, open_interest_value: 200, turnover_24h: 1000, funding_rate: 0.0001 } }],
      cases: [{ case_id: 'invalid-trade', symbol: 'ABCUSDT', lifecycle: 'buy', phase_hypothesis: 'LONG' }],
      shortlist: ['ABCUSDT', 'DEFUSDT', 'GHIUSDT', 'JKLUSDT', 'MNOUSDT', 'TOOMANYUSDT'],
      outcomes: { horizons_minutes: [5, 15, 30, 60], available: 0, status: 'not_started_without_cases' },
      weekly_brief: { status: 'data_unavailable', reason: 'not enough snapshots' },
    },
  }, Date.parse('2026-09-14T15:40:01.000Z'));
  assert.equal(model.positioning.mode, 'shadow');
  assert.equal(model.positioning.cases.length, 0);
  assert.equal(model.positioning.shortlist.length, 5);
  assert.equal(model.positioning.snapshots[0].raw.lastPrice, 1);
  assert.equal(model.executionMode, 'DISABLED');
});

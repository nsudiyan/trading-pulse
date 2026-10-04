const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(__dirname + '/dashboard/app.js', 'utf8');
const html = fs.readFileSync(__dirname + '/dashboard/index.html', 'utf8');
const ctx = vm.createContext({Intl});
vm.runInContext(source.slice(0, source.indexOf('function renderStats')), ctx);
const status = signal => ctx.displayStatus(signal);
const matches = (signal, filter) => ctx.matchesStatus(signal, filter);
assert.match(ctx.mskFull('2026-10-03T21:20:00.000Z'), /04\.10\.2026, 00:20 МСК/);
assert.match(ctx.msk('2026-10-03T21:20:00.000Z'), /04\.10, 00:20 МСК/);
assert.match(html, /Времена интерфейса — МСК/);
const fixture = movement => Object.freeze({path_status: 'legacy_unmeasured', movement: movement && Object.freeze(movement)});
let count = 0;
for (const [raw, normalized, filter] of [
  ['tracking', 'observing', 'observing'], ['complete', 'complete', 'complete'],
  ['waiting', 'waiting_next_bar', 'observing'], ['data_unavailable', 'data_gap', 'data_gap'],
  ['invalid_ohlc', 'data_gap', 'data_gap'], ['conflicting_duplicates', 'data_gap', 'data_gap'],
  ['unknown_future_status', 'data_gap', 'data_gap'], [undefined, 'data_gap', 'data_gap'],
]) {
  const row = fixture({status: raw});
  assert.equal(status(row), normalized);
  assert(matches(row, filter));
  assert(matches(row, 'all'));
  assert(!matches(row, 'legacy_unmeasured'));
  assert.equal(row.path_status, 'legacy_unmeasured');
  count++;
}
const legacy = fixture(null);
assert.equal(status(legacy), 'legacy_unmeasured');
assert(matches(legacy, 'legacy_unmeasured'));
assert(!matches(legacy, 'data_gap'));
assert.equal(status({}), 'data_gap');
assert.equal(ctx.statusName(status(fixture({status: 'data_unavailable'}))), 'Нет данных');
assert.equal(ctx.statusName(status(fixture({status: 'tracking'}))), 'Наблюдается');
console.log(`PASS ${count + 5} UI status/time cases; legacy research input remains unchanged`);

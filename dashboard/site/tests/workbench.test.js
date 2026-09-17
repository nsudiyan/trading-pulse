const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const app = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
const css = fs.readFileSync(path.join(__dirname, '..', 'style.css'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function body(name, nextName) {
  const start = app.indexOf(`function ${name}(`);
  const end = nextName ? app.indexOf(`function ${nextName}(`, start) : app.length;
  assert.ok(start >= 0, `${name} must exist`);
  assert.ok(end > start, `${name} must have a body`);
  return app.slice(start, end);
}

test('terminal queue is distinct and never contains directional or execution language', () => {
  const queue = body('nowObservationQueue', 'positioningWorkbench');
  const card = body('nowObservationCard', 'nowObservationQueue');
  const terminal = `${queue}\n${card}`;
  assert.match(queue, /СЕЙЧАС ОТКРЫТЬ В ТЕРМИНАЛЕ/);
  assert.match(queue, /Tiger Trade/);
  assert.match(app, /manual_check: 'РУЧНАЯ ПРОВЕРКА'/);
  assert.match(card, /фьючерс и спот/);
  assert.doesNotMatch(terminal, /\b(?:long|short|entry|buy|sell|profit)\b/i);
});

test('positioning workbench has an independent honest unavailable state', () => {
  const workbench = body('positioningWorkbench', 'radar');
  assert.match(workbench, /POSITIONING — CRYPTO COT/);
  assert.match(workbench, /ДАННЫЕ ПОКА НЕДОСТУПНЫ/);
  assert.match(workbench, /OI, funding, фаз и исследовательских кейсов/);
  assert.match(workbench, /3–5 независимых исследовательских кейсов/);
  assert.match(workbench, /#\/positioning/);
});

test('workbench is responsive and navigation preserves distinct terminal and COT destinations', () => {
  assert.match(css, /\.terminal-workbench/);
  assert.match(css, /\.positioning-workbench/);
  assert.match(css, /@media\(max-width:700px\)/);
  assert.match(html, /⚡ Рабочее место/);
  assert.match(html, /◎ COT · Positioning/);
});

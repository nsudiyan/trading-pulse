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

test('smart money lab is a separate multi-timeframe education route without course screenshots', () => {
  const lab = body('smartMoneyLab', 'radar');
  const caseCard = body('smartMoneyCaseCard', 'smartMoneyLab');
  assert.match(lab, /SMART MONEY LAB/);
  assert.match(lab, /минимум два ТФ/);
  assert.match(lab, /Оригинальные скриншоты курса в публичный Pulse не выводятся/);
  assert.match(caseCard, /Таймлайн и таймфреймы/);
  assert.match(caseCard, /Условие отмены/);
  assert.match(html, /#\/smart-money-lab/);
  assert.match(css, /\.sm-case/);
  assert.doesNotMatch(caseCard, /<img|screenshot|\.png/i);
});

test('the first two course strategies are rendered as original, guarded text diagrams', () => {
  const first = body('smartMoneyStrategyOne', 'smartMoneyStrategyTwo');
  const second = body('smartMoneyStrategyTwo', 'smartMoneyStrategyThree');
  const third = body('smartMoneyStrategyThree', 'smartMoneyLab');
  assert.match(first, /Sweep 1D → 1H/);
  assert.match(first, /Sweep 4H → 15M/);
  assert.match(first, /Sweep 1H → 5M/);
  assert.match(first, /не тень/i);
  assert.match(first, /CHOCH/);
  assert.match(first, /2R/);
  assert.match(first, /не статистически подтверждённая стратегия/);
  assert.match(second, /TDP \/ Three Drives/);
  assert.match(second, /high #3/);
  assert.match(second, /Источник требует уточнения/);
  assert.match(second, /TDP 1H \/ BOS 4H/);
  assert.match(third, /1MO \(month\?\) → 1D/);
  assert.match(third, /Range 1H \/ BOS 4H/);
  assert.match(third, /AUDUSD \/ Forexcom/);
  assert.match(third, /не статистически подтверждённая стратегия/);
  assert.doesNotMatch(`${first}\n${second}\n${third}`, /<img|\.png/i);
});

test('smart money concepts retain source uncertainty instead of upgrading course claims to facts', () => {
  const concepts = body('smartMoneyConcepts', 'smartMoneyLab');
  assert.match(concepts, /3-барный pivot/);
  assert.match(concepts, /пять баров/);
  assert.match(concepts, /закрытие телом/i);
  assert.match(concepts, /CHOCH/);
  assert.match(concepts, /гипотеза курса; верифицированный win rate не опубликован/);
  assert.match(concepts, /Математическое отношение ≠ доказанное преимущество/);
  assert.match(concepts, /не является торговой командой/);
  assert.match(concepts, /Asia \/ AKZ/);
  assert.match(concepts, /FX rollover 17:00 New York/);
  assert.match(concepts, /https:\/\/www\.nyse\.com\/markets\/hours-calendars/);
  assert.match(concepts, /https:\/\/www\.oanda\.com\/assets\/documents\/252\/Hours_of_Operation\.pdf/);
  assert.doesNotMatch(concepts, /<img|\.png/i);
});

test('advanced smart money models are educational and keep predictive claims bounded', () => {
  const advanced = body('smartMoneyAdvancedModels', 'smartMoneyLab');
  assert.match(advanced, /POI 4H → BOS 15M/);
  assert.match(advanced, /Bar Replay-гипотезы/);
  assert.match(advanced, /меньшее подтверждение/);
  assert.match(advanced, /STH\/STL → ITH\/ITL → LTH\/LTL/);
  assert.match(advanced, /запаздывающий фильтр/);
  assert.match(advanced, /0\.5 — не число Fibonacci/);
  assert.match(advanced, /EQH \/ EQL/);
  assert.match(advanced, /не установленный факт ликвидности/);
  assert.match(advanced, /FVG не доказывает наличие неисполненных институциональных ордеров/);
  assert.match(advanced, /Premium не равен автоматическому short/);
  assert.doesNotMatch(advanced, /<img|\.png/i);
});

test('range and Wyckoff material stays a source-bounded replay workflow', () => {
  const range = body('smartMoneyRangeAndWyckoff', 'smartMoneyLab');
  assert.match(range, /FVA, FLOD, OLOD и nested-FVA/);
  assert.match(range, /не доказывают order flow/);
  assert.match(range, /численный tolerance/);
  assert.match(range, /Bar Replay/);
  assert.match(range, /Фазы A–E/);
  assert.match(range, /venue-specific/);
  assert.match(range, /нет консолидированного tape/);
  assert.match(range, /не доказательством причинности/);
  assert.match(range, /1H против M1\/M5/);
  assert.match(range, /не предсказания результата/);
  assert.doesNotMatch(range, /<img|\.png/i);
});

test('manual replay workbench requires auditable hypotheses without live detection claims', () => {
  const replay = body('smartMoneyReplayWorkbench', 'smartMoneyLab');
  const record = body('replayHypothesisCard', 'smartMoneyReplayWorkbench');
  assert.match(replay, /static \/ replay only/);
  assert.match(replay, /не пытается сам определить модель/);
  assert.match(replay, /asset, venue\/source, observation timestamp, HTF и LTF/);
  assert.match(replay, /competing interpretation, invalidation/);
  assert.match(record, /Конкурирующая трактовка/);
  assert.match(record, /Отмена гипотезы/);
  assert.match(record, /Не обнаружено автоматически/);
  assert.doesNotMatch(`${replay}\n${record}`, /<img|\.png|fetch\(/i);
});

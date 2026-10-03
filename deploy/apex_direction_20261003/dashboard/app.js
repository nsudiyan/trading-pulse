const state = {signals: [], selected: null, filter: 'all', query: ''};
const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
const money = new Intl.NumberFormat('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2});

function utc(value) {
  if (value == null) return '—';
  const date = typeof value === 'number' ? new Date(value) : new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat('ru-RU', {timeZone: 'UTC', day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit'}).format(date) + ' UTC';
}
function pct(value, signed = true) {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  const n = Number(value);
  return (signed && n > 0 ? '+' : '') + number.format(n) + '%';
}
function text(tag, value, className = '') {
  const el = document.createElement(tag);
  if (className) el.className = className;
  el.textContent = value;
  return el;
}
function statusName(status) {
  return ({complete: '72ч завершены', observing: 'Наблюдается', data_gap: 'Нет данных', waiting_next_bar: 'Ожидаем свечу', legacy_unmeasured: 'Архив · без расчёта'})[status] || 'Нет данных';
}
function displayStatus(signal) {
  // Presentation only: never overwrite the legacy research path_status.
  if (!signal.movement) return signal.path_status === 'legacy_unmeasured' ? 'legacy_unmeasured' : 'data_gap';
  return ({tracking: 'observing', complete: 'complete', waiting: 'waiting_next_bar',
    data_unavailable: 'data_gap', invalid_ohlc: 'data_gap', conflicting_duplicates: 'data_gap'})[signal.movement.status] || 'data_gap';
}
function matchesStatus(signal, filter) {
  const status = displayStatus(signal);
  return filter === 'all' || status === filter || (filter === 'observing' && status === 'waiting_next_bar');
}
function price(value) {return value == null ? '—' : new Intl.NumberFormat('ru-RU', {maximumSignificantDigits: 10}).format(Number(value));}

function renderStats(data) {
  const s = data.stats || {};
  $('stat-sent').textContent = number.format(s.sent || 0);
  $('stat-sent-note').textContent = `${number.format(s.measured || 0)} с расчётом · ${number.format(s.legacy_unmeasured || 0)} старых обзоров без расчёта`;
  $('stat-complete').textContent = number.format(s.complete || 0);
  $('stat-mfe').textContent = s.complete ? pct(s.avg_mfe_pct) : '—';
  $('stat-mae').textContent = s.complete ? pct(s.avg_mae_pct) : '—';
  $('updated-at').textContent = utc(data.generated_at_utc);
}

function filtered() {
  return state.signals.filter((signal) => {
    if (!matchesStatus(signal, state.filter)) return false;
    return !state.query || signal.symbol.toLowerCase().includes(state.query);
  });
}

function renderList() {
  const list = $('signal-list');
  list.replaceChildren();
  const rows = filtered();
  if (!rows.length) {
    const empty = text('div', '', 'empty-list');
    empty.append(text('strong', state.signals.length ? 'Ничего не найдено' : 'Пока нет отобранных сигналов'));
    empty.append(text('p', state.signals.length ? 'Измените поиск или фильтр.' : 'Первый кандидат появится здесь только после реальной отправки ботом в Telegram. Проверочное сообщение в историю не входит.'));
    list.append(empty);
    renderDetail(null);
    return;
  }
  if (!rows.some(s => s.id === state.selected)) state.selected = rows[0].id;
  for (const signal of rows) {
    const card = text('button', '', 'signal-card' + (signal.id === state.selected ? ' active' : ''));
    card.type = 'button';
    card.setAttribute('aria-label', `${signal.symbol}, ${signal.scenario?.side || 'без направления'}, ${statusName(displayStatus(signal))}`);
    const top = text('div', '', 'card-top');
    top.append(text('span', signal.symbol, 'card-symbol'));
    const side = signal.scenario?.side;
    top.append(text('span', side || 'БЕЗ НАПРАВЛЕНИЯ', 'side-badge ' + (side === 'SELL' ? 'sell' : side ? '' : 'overview')));
    card.append(top, text('div', utc(signal.sent_utc), 'card-time'));
    const bottom = text('div', '', 'card-bottom');
    bottom.append(text('span', statusName(displayStatus(signal)), 'status-badge ' + displayStatus(signal)));
    const outcome = text('span', '', 'card-outcome');
    outcome.append(text('span', `Рост ${pct(signal.movement?.max_up_pct)}`, 'positive'));
    outcome.append(text('span', `Падение ${pct(signal.movement?.max_down_pct)}`, 'negative'));
    bottom.append(outcome); card.append(bottom);
    card.addEventListener('click', () => {state.selected = signal.id; renderList();});
    list.append(card);
  }
  renderDetail(rows.find(s => s.id === state.selected));
}

function svgNode(tag, attrs = {}) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return node;
}
function renderPortfolio(model) {
  const hasData = model && model.equity_usdt != null && model.curve?.length;
  $('portfolio-equity').textContent = hasData ? `${money.format(model.equity_usdt)} USDT` : '—';
  $('portfolio-pnl').textContent = hasData ? `${model.pnl_usdt > 0 ? '+' : ''}${money.format(model.pnl_usdt)} USDT` : '—';
  $('portfolio-return').textContent = hasData ? pct(model.return_pct) : '—';
  $('portfolio-drawdown').textContent = hasData ? pct(model.max_drawdown_pct) : '—';
  $('portfolio-pnl').className = hasData ? (model.pnl_usdt >= 0 ? 'positive' : 'negative') : '';
  $('portfolio-return').className = hasData ? (model.return_pct >= 0 ? 'positive' : 'negative') : '';
  $('portfolio-drawdown').className = hasData ? 'negative' : '';
  $('portfolio-period').textContent = hasData
    ? `${utc(model.curve[0].at_ms)} — ${utc(model.curve.at(-1).at_ms)}` : 'Ожидаем первый измеренный сигнал';
  $('portfolio-rule').textContent = `Старт: ${number.format(model?.initial_usdt ?? 1000)} USDT · ${number.format(model?.fixed_notional_usdt ?? 100)} USDT на алерт · максимум ${model?.max_concurrent ?? 10} одновременно · удержание 72 часа · без комиссий и проскальзывания. В модели: ${model?.admitted ?? 0}; ожидают свечу: ${model?.pending ?? 0}; пропуски данных: ${model?.excluded_data_gaps ?? 0}; отклонено по лимиту: ${model?.skipped_capacity ?? 0}.`;
  const host = $('portfolio-chart');
  host.replaceChildren();
  if (!hasData) {
    host.append(text('p', 'График появится после первого измеренного сигнала. Сейчас реальных результатов для модели нет.', 'portfolio-empty'));
    return;
  }
  const curve = model.curve;
  const svg = svgNode('svg', {viewBox: '0 0 960 335', role: 'img', 'aria-label': 'Стоимость модельного портфеля и его просадка'});
  const first = curve[0].at_ms, span = Math.max(1, curve.at(-1).at_ms - first);
  const x = p => 65 + 855 * (p.at_ms - first) / span;
  const equity = curve.map(p => Number(p.equity_usdt));
  const minEquity = Math.min(...equity, model.initial_usdt);
  const maxEquity = Math.max(...equity, model.initial_usdt);
  const pad = Math.max(1, (maxEquity - minEquity) * .15);
  const lo = minEquity - pad, hi = maxEquity + pad;
  const yEquity = v => 185 - (v - lo) * 145 / (hi - lo);
  const minDD = Math.min(-0.01, Number(model.max_drawdown_pct));
  const yDD = v => 290 - (v / minDD) * 55;
  for (const y of [40, 112, 185, 235, 290]) svg.append(svgNode('line', {x1: 65, x2: 920, y1: y, y2: y, stroke: '#28384b', 'stroke-dasharray': '4 6'}));
  svg.append(svgNode('line', {x1: 65, x2: 920, y1: yEquity(model.initial_usdt), y2: yEquity(model.initial_usdt), stroke: '#64758b', 'stroke-dasharray': '4 6'}));
  const equityPath = curve.map((p, i) => `${i ? 'L' : 'M'} ${x(p).toFixed(1)} ${yEquity(p.equity_usdt).toFixed(1)}`).join(' ');
  const ddPath = curve.map((p, i) => `${i ? 'L' : 'M'} ${x(p).toFixed(1)} ${yDD(p.drawdown_pct).toFixed(1)}`).join(' ');
  svg.append(svgNode('path', {d: equityPath, fill: 'none', stroke: model.pnl_usdt >= 0 ? '#44e5cf' : '#ff797f', 'stroke-width': 3, 'stroke-linejoin': 'round'}));
  svg.append(svgNode('path', {d: ddPath, fill: 'none', stroke: '#ff797f', 'stroke-width': 2, 'stroke-linejoin': 'round'}));
  for (const [y, label, color] of [[28, `ПОРТФЕЛЬ · ${money.format(model.equity_usdt)} USDT`, '#c8d9e9'], [220, `ПРОСАДКА · ${pct(model.max_drawdown_pct)}`, '#ff9fa5'], [323, utc(curve[0].at_ms), '#8297b2']]) {
    const t = svgNode('text', {x: 65, y, fill: color, 'font-size': 11}); t.textContent = label; svg.append(t);
  }
  const endLabel = svgNode('text', {x: 920, y: 323, 'text-anchor': 'end', fill: '#8297b2', 'font-size': 11});
  endLabel.textContent = utc(curve.at(-1).at_ms); svg.append(endLabel);
  host.append(svg);
}
function renderChannel(data) {
  const host = $('channel-list');
  host.replaceChildren();
  const posts = data.posts || [];
  $('channel-count').textContent = `Сохранено ${number.format(data.total_mirrored || 0)} · показаны последние ${posts.length}`;
  if (!posts.length) {
    host.append(text('div', 'Публикации пока не загружены. Проверка публичного канала повторяется автоматически.', 'empty-list'));
    return;
  }
  const names = {structure_research: 'STRUCTURE · НАБЛЮДЕНИЕ', radar: 'РАДАР', chart_review: 'СВЕЧНОЙ ОБЗОР', other: 'СООБЩЕНИЕ КАНАЛА'};
  for (const post of posts) {
    const card = text('article', '', 'channel-card');
    const heading = text('div', '', 'channel-card-head');
    heading.append(text('strong', names[post.category] || names.other),
                   text('time', utc(post.sent_utc)));
    const body = text('pre', post.text, 'channel-card-body');
    const link = text('a', `Открыть в Telegram · #${post.message_id}`, 'channel-link');
    link.href = post.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    card.append(heading, body, link);
    host.append(card);
  }
}
async function refreshChannel() {
  try {
    const response = await fetch('/api/channel', {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderChannel(await response.json());
  } catch {
    $('channel-count').textContent = 'Временно недоступно';
    if (!$('channel-list').children.length) $('channel-list').append(text('div', 'Не удалось загрузить публикации канала. Повторим автоматически.', 'empty-list'));
  }
}
function drawChart(container, signal) {
  const wrap = text('div', '', 'path-chart');
  const head = text('div', '', 'chart-head');
  head.append(text('span', 'ПУТЬ ЗАКРЫТИЙ · ОТ ЦЕНЫ-ОРИЕНТИРА'));
  head.append(text('strong', signal.curve.length ? `${signal.curve.length} точек` : 'Данных пока нет'));
  wrap.append(head);
  const svg = svgNode('svg', {viewBox: '0 0 620 190', role: 'img', 'aria-label': 'Изменение цены по закрытиям свечей'});
  const points = [{return_pct: 0}, ...signal.curve];
  if (points.length < 2) {
    svg.append(svgNode('line', {x1: 30, x2: 590, y1: 100, y2: 100, stroke: '#34475f', 'stroke-dasharray': '5 5'}));
    const note = svgNode('text', {x: 310, y: 95, 'text-anchor': 'middle', fill: '#8194ae', 'font-size': 14});
    note.textContent = 'Ожидаем закрытые 15m свечи'; svg.append(note);
  } else {
    const values = points.map(p => Number(p.return_pct));
    if (signal.mfe_pct != null) values.push(Number(signal.mfe_pct));
    if (signal.mae_pct != null) values.push(Number(signal.mae_pct));
    let lo = Math.min(...values), hi = Math.max(...values);
    const pad = Math.max(0.2, (hi - lo) * .18); lo -= pad; hi += pad;
    const x = i => 30 + i * 560 / (points.length - 1);
    const y = v => 165 - (v - lo) * 140 / (hi - lo);
    svg.append(svgNode('line', {x1: 30, x2: 590, y1: y(0), y2: y(0), stroke: '#50647f', 'stroke-dasharray': '5 5'}));
    const line = points.map((p, i) => `${x(i)},${y(Number(p.return_pct))}`).join(' ');
    svg.append(svgNode('polyline', {points: line, fill: 'none', stroke: '#4ce0cf', 'stroke-width': 2.6, 'stroke-linecap': 'round', 'stroke-linejoin': 'round'}));
    svg.append(svgNode('circle', {cx: x(points.length - 1), cy: y(Number(points.at(-1).return_pct)), r: 4, fill: '#4ce0cf'}));
    const zero = svgNode('text', {x: 590, y: Math.max(15, y(0)-5), 'text-anchor': 'end', fill: '#6e84a0', 'font-size': 11});
    zero.textContent = '0%'; svg.append(zero);
  }
  wrap.append(svg);
  wrap.append(text('div', 'Линия: закрытия свечей · MFE/MAE: экстремумы high/low каждой полной свечи', 'chart-foot'));
  container.append(wrap);
}

function fact(grid, label, value) {const box = text('div', ''); box.append(text('small', label), text('b', value)); grid.append(box);}
function renderDetail(signal) {
  const detail = $('signal-detail'); detail.replaceChildren();
  if (!signal) {
    const empty = text('div', '', 'detail-empty');
    empty.append(text('span', '⌁', 'empty-glyph'), text('h3', 'Ожидаем первый сигнал'),
                 text('p', 'После реальной отправки в Telegram здесь начнётся отслеживание цены на 72 часа.'));
    detail.append(empty); return;
  }
  if (!signal.movement) {
    const title = text('div', `${signal.symbol} · обзор · ${utc(signal.sent_utc)}`, 'detail-title');
    const note = text('p', 'Это сообщение действительно отправлено в Telegram до запуска учёта исходов. Направление и цена отсчёта не были сохранены, поэтому доходность, просадка и 72-часовой результат для него не рассчитываются.', 'legacy-note');
    const archived = text('details', '', 'alert-details');
    archived.open = true;
    archived.append(text('summary', 'Исходный алерт'), text('pre', signal.text || 'Текст недоступен'));
    detail.append(title, note, archived);
    return;
  }
  const top = text('div', '', 'detail-top');
  const titleBlock = text('div', '');
  titleBlock.append(text('div', `${signal.symbol} · сценарий ${signal.scenario?.side || 'БЕЗ НАПРАВЛЕНИЯ'}`, 'detail-title'),
                    text('div', `${signal.setup_type === 'strong_sweep_review' ? 'Сильный sweep' : 'Свечной обзор'} · Отправлено ${utc(signal.sent_utc)}`, 'detail-sub'));
  top.append(titleBlock, text('div', statusName(displayStatus(signal)), 'detail-chip')); detail.append(top);
  const movement = signal.movement;
  detail.append(text('p', `Направление: ${signal.scenario?.explanation || signal.scenario?.reason || 'нет данных'}. NO-TRADE: это не подтверждённая точка входа.`, 'legacy-note'));
  const zone = signal.scenario?.zone_observation;
  detail.append(text('p', `Зона 4ч pivot-range (reported): ${zone?.zone || 'нет сохранённых данных'}. ${zone?.event_time_verified ? 'Цена закрытой сигнальной 15м.' : 'Время исходной цены не подтверждено.'} Это не Volume Profile. Классификатор BUY/SELL зону не проверяет; исходный канал отбора может её учитывать. Направление — наблюдение, не вход.`, 'legacy-note'));
  drawChart(detail, {...signal, curve: movement.curve || [], mfe_pct: movement.max_up_pct, mae_pct: movement.max_down_pct});
  const metrics = text('div', '', 'detail-metrics');
  for (const [name, value, cls] of [['Макс. рост от отсчёта', pct(movement.max_up_pct), 'positive'], ['Макс. падение от отсчёта', pct(movement.max_down_pct), 'negative'], ['Последнее закрытие от отсчёта', pct(movement.return_pct), 'neutral']]) {
    const box = text('div', '', 'detail-metric'); box.append(text('span', name), text('strong', value, cls)); metrics.append(box);
  }
  detail.append(metrics);
  const facts = text('div', '', 'detail-facts');
  fact(facts, 'Отсчёт: open первой полной 15м после отправки', `${price(movement.anchor_price)} · ${utc(movement.anchor_start_ms)}`);
  fact(facts, 'Источник / статус', `${movement.source} · ${movement.timeframe} · ${movement.status}`);
  fact(facts, 'Происхождение зоны', zone?.source ? `${zone.source} · ${zone.status} · ${zone.asof_end_ms == null ? 'время источника неизвестно' : utc(zone.asof_end_ms + 1)}` : 'Нет сохранённых данных; историческая зона не восстановлена задним числом');
  fact(facts, 'Последняя закрытая свеча', movement.last_closed_ms == null ? 'Нет данных' : utc(movement.last_closed_ms + 1));
  fact(facts, 'По направлению сценария: MFE / MAE', `${pct(movement.mfe_pct)} / ${pct(movement.mae_pct)}`);
  fact(facts, 'Что измеряем', 'Изменение от фиксированной цены, не PnL и не просадка от локального пика. Исполнение не моделируется.');
  detail.append(facts);
  const archived = text('details', '', 'alert-details');
  archived.append(text('summary', 'Открыть исходный алерт'), text('pre', signal.text || 'Текст недоступен'));
  detail.append(archived);
}

async function refresh() {
  try {
    const response = await fetch('/api/signals?limit=100', {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    state.signals = data.signals || [];
    renderPortfolio(data.portfolio); renderStats(data); renderList();
    $('feed-state').textContent = 'Данные доступны';
    $('feed-state').parentElement.classList.remove('error');
  } catch {
    $('feed-state').textContent = 'Данные недоступны';
    $('feed-state').parentElement.classList.add('error');
    if (!state.signals.length) $('signal-list').replaceChildren(text('div', 'Не удалось загрузить историю. Повторим автоматически.', 'empty-list'));
  }
}
document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {
  state.filter = button.dataset.filter;
  document.querySelectorAll('[data-filter]').forEach(item => item.classList.toggle('selected', item === button));
  renderList();
}));
$('search').addEventListener('input', event => {state.query = event.target.value.trim().toLowerCase(); renderList();});
refresh(); refreshChannel();
setInterval(refresh, 60_000);
setInterval(refreshChannel, 60_000);

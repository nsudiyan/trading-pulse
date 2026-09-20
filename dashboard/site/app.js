(() => {
  const main = document.querySelector('#main');
  const systemPill = document.querySelector('#system-pill');
  let overview;

  const esc = (value) => String(value ?? '—').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char]);
  const stamp = (value) => value ? new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC' }).format(new Date(value)) + ' UTC' : 'не опубликовано';
  const ms = (value) => Number.isFinite(value) ? `${Math.round(value / 1000)} с` : 'не измерено';
  const title = (value) => ({ research_only: 'RESEARCH ONLY', manual_review: 'MANUAL REVIEW', manual_check: 'РУЧНАЯ ПРОВЕРКА', accepted_for_manual_review: 'РУЧНАЯ ПРОВЕРКА', rejected_for_review: 'ОТСЕЯНО', pending_closed_m5: 'ЖДЁМ M5', no_trade: 'NO TRADE', data_unavailable: 'DATA UNAVAILABLE', collecting: 'SHADOW · COLLECTING', ready: 'ИСТОРИЯ ГОТОВА', legacy_provenance_incomplete: 'LEGACY / PROVENANCE INCOMPLETE', legacy_only: 'LEGACY ARCHIVE AVAILABLE', legacy_only_stale: 'LEGACY ARCHIVE · CURRENT FEED STALE', expired: 'EXPIRED', invalidated: 'INVALIDATED', healthy: 'HEALTHY', degraded: 'DEGRADED', unavailable: 'UNAVAILABLE', delayed: 'DELAYED' }[value] || String(value || 'UNAVAILABLE').toUpperCase());
  const badge = (value) => `<span class="pill ${esc(value || 'unavailable')}">${esc(title(value))}</span>`;
  const card = (content, cls = '') => `<article class="card ${cls}">${content}</article>`;
  const stat = (label, value) => card(`<p class="kicker">${esc(label)}</p><p class="metric">${esc(value)}</p>`);
  const nav = (route) => document.querySelectorAll('.nav a').forEach((link) => link.toggleAttribute('aria-current', link.getAttribute('href') === `#/${route}`));

  function pageHeader(name, description) {
    return `<section><p class="kicker">Trading Pulse / ${esc(name)}</p><h1>${esc(name)}</h1><p class="subtitle">${esc(description)}</p></section>`;
  }

  function systemBanner(model) {
    const health = model.health;
    return `<section class="callout ${health.overallStatus === 'unavailable' ? 'danger' : health.overallStatus === 'degraded' ? 'warning' : ''}">
      <div class="row"><div><p class="kicker">Статус системы</p><h2>${esc(title(health.overallStatus))}</h2></div>${badge(health.overallStatus)}</div>
      <p><strong>Режим:</strong> исследовательские наблюдения без направленной трактовки. Исполнение отключено.</p>
      ${String(health.overallStatus).startsWith('legacy_only') ? `<p><strong>История доступна:</strong> ${esc(health.legacyRecordCount)} legacy-записей сохранены в <a href="#/archive">Pulse Archive</a>. Они не являются свежими Radar-событиями.${health.overallStatus === 'legacy_only_stale' ? ' Текущий feed устарел; свежесть нового Radar не подтверждена.' : ''}</p>` : ''}
      <p class="meta">Последнее формирование feed: ${esc(stamp(health.feedGeneratedAtUtc))}. Возраст: ${esc(ms(health.feedAgeMs))}.</p>
    </section>`;
  }

  function episodeCard(episode) {
    const reasons = episode.statusReasons?.length ? episode.statusReasons.join('; ') : 'Причина не требуется для текущего статуса';
    const orderBookNote = episode.dataQuality?.liquidityContext === 'unavailable'
      ? '<p class="meta"><strong>Стакан:</strong> контекст не предоставлен; нужна ручная проверка.</p>' : '';
    return `<a class="card episode" href="#/episode/${encodeURIComponent(episode.id)}">
      <div class="row"><div><p class="kicker">${esc(episode.eventLabel)}</p><h3>${esc(episode.symbol || 'Неидентифицированный инструмент')}</h3></div>${badge(episode.status)}</div>
      <p class="meta">Эпизод ${esc(episode.id)} · ${esc(episode.venue || 'venue не опубликована')} · ${esc(episode.timeframe || 'таймфрейм не опубликован')}</p>
      <p class="meta">Первое наблюдение: ${esc(stamp(episode.firstDetectedAtUtc))}</p>
      <p class="meta">Первичных срабатываний: ${esc(episode.rawAlertCount)}${episode.reportedRepeatCount !== null ? ` · источник сообщает повторов: ${esc(episode.reportedRepeatCount)}` : ''}</p>
      ${orderBookNote}
      <p class="meta">${esc(reasons)}</p>
    </a>`;
  }

  function nowObservationCard(observation) {
    const observedAt = observation.observedAtUtc ? stamp(observation.observedAtUtc) : 'время источника не передано';
    const actualFacts = [];
    if (observation.livePrice !== null) actualFacts.push(['Текущая цена', observation.livePrice]);
    if (observation.liveChangePct !== null) actualFacts.push(['Изменение', `${observation.liveChangePct}%`]);
    if (observation.relativeVolume !== null) actualFacts.push(['Относительный объём источника', `×${observation.relativeVolume}`]);
    return `<article class="card now-observation" data-live-source="${esc(observation.sourceModule)}">
      <div class="row"><div><p class="kicker">${esc(observation.sourceModule)}</p><h3>${esc(observation.symbol || 'инструмент не передан')}</h3></div>${badge(observation.status)}</div>
      <p class="meta">Время наблюдения: ${esc(observedAt)}</p>
      ${actualFacts.length ? facts(actualFacts) : '<p class="meta">Текущая цена и изменение источником не переданы.</p>'}
      <ol class="terminal-checklist"><li>Открой график инструмента.</li><li>Сверь фьючерс и спот.</li><li>Проверь стакан и ленты.</li></ol>
    </article>`;
  }

  function nowObservationQueue(model) {
    const observations = Array.isArray(model.nowObservations) ? model.nowObservations : [];
    const focus = Array.isArray(model.focusObservations) ? model.focusObservations : [];
    const rawNote = observations.length ? `<details class="details"><summary>Сырой поток скрыт: ${esc(observations.length)} строк</summary><p class="meta">Он включает старые, повторные и неотсортированные публикации. Он не является списком сделок и не требует открытия каждой монеты.</p></details>` : '';
    return `<section class="terminal-workbench" aria-label="Сейчас открыть в терминале">
      <div class="workbench-heading"><div><p class="kicker">Сырой радарный поток</p><h2>⚡ СЕЙЧАС ОТКРЫТЬ В ТЕРМИНАЛЕ · НОВЫЕ НАБЛЮДЕНИЯ</h2><p class="subtitle">Не больше трёх свежих радар-наблюдений. Это раннее внимание: условие — не старше 20 минут, источник Radar, относительный объём ≥ ×5. Очередь ниже не равна списку качественных сетапов.</p></div>${badge('manual_check')}</div>
      ${focus.length ? `<div class="grid now-observation-grid">${focus.map(nowObservationCard).join('')}</div><p class="meta">Это приоритет внимания, а не сделка и не направление. Открой монету в Tiger Trade: график → фьючерс/спот → стакан → лента.</p>` : '<div class="empty terminal-empty"><strong>Сейчас нет свежего наблюдения, прошедшего фильтр внимания.</strong><br>Не нужно искать сделку: дождись следующего отфильтрованного радар-наблюдения.</div>'}
      ${rawNote}
    </section>`;
  }

  function acceptanceReview(model) {
    const review = model.acceptanceReview || {};
    const rows = Array.isArray(review.active) ? review.active : [];
    const accepted = rows.filter((item) => item.status === 'accepted_for_manual_review');
    const pending = rows.filter((item) => item.status === 'pending_closed_m5');
    const rejected = rows.filter((item) => item.status === 'rejected_for_review');
    const reviewCard = (item) => card(`<div class="row"><div><p class="kicker">${esc(item.venue || 'BYBIT')} · ${esc(review.protocol || 'H-IMPULSE-ACCEPT-01')}</p><h3>${esc(item.symbol)}</h3></div>${badge(item.status)}</div>
      <p class="meta">Событие: ${esc(stamp(item.sourceTimestampUtc))}</p>
      ${facts([['Отн. объём', item.relativeVolume !== null ? `×${item.relativeVolume}` : '—'], ['Движение M5', item.dominantExcursionPct !== null ? `${item.dominantExcursionPct.toFixed(3)}%` : 'ожидается'], ['Неблагоприятный ход M5', item.adverseExcursionPct !== null ? `${item.adverseExcursionPct.toFixed(3)}%` : 'ожидается'], ['Удержание', item.retentionRatio !== null ? `${Math.round(item.retentionRatio * 100)}%` : 'ожидается'], ['Закрытий в сторону движения', item.sameSideCloses ?? 'ожидается']])}
      <p class="meta"><strong>${item.status === 'accepted_for_manual_review' ? 'Дальше в Tiger:' : 'Причина:'}</strong> ${esc(item.status === 'accepted_for_manual_review' ? 'сверь фьючерс/спот, спред, ленту и реальное исполнение лимиток.' : (item.reasons || []).join(', ') || 'ожидаются три закрытые свечи M5')}</p>`);
    return `<section class="acceptance-workbench" aria-label="Теневой фильтр качества движения">
      <div class="workbench-heading"><div><p class="kicker">Forward-only · отдельный слой, не заменяет Radar</p><h2>✓ ПРОШЛИ ПЕРВИЧНУЮ ПРОВЕРКУ КАЧЕСТВА</h2><p class="subtitle">После события ждём ${esc(review.delayMinutes ?? 15)} минут и три закрытые M5-свечи. Проверяем удержание движения и ранний неблагоприятный ход. Это не направление, не вход и не обещание результата.</p></div>${badge('collecting')}</div>
      ${facts([['На ожидании', review.counts?.pending ?? 0], ['Прошли в журнале', review.counts?.accepted ?? 0], ['Отсеяны в журнале', review.counts?.rejected ?? 0], ['Старт протокола', stamp(review.startedAtUtc)]])}
      ${accepted.length ? `<div class="grid now-observation-grid">${accepted.map(reviewCard).join('')}</div><p class="meta">Эти наблюдения можно открыть в Tiger Trade для ручной проверки. Фильтр не публикует сторону движения.</p>` : '<div class="empty terminal-empty"><strong>Пока нет монеты, прошедшей новый теневой фильтр.</strong><br>Это нормально: исторические события не подгружаются задним числом, новые сначала проходят три закрытые M5-свечи.</div>'}
      ${pending.length ? `<details class="details"><summary>На ожидании закрытия M5: ${esc(pending.length)}</summary><div class="grid">${pending.map(reviewCard).join('')}</div></details>` : ''}
      ${rejected.length ? `<details class="details"><summary>Отсеяно в текущем 30-минутном окне: ${esc(rejected.length)}</summary><div class="grid">${rejected.map(reviewCard).join('')}</div></details>` : ''}
      <p class="meta">${esc((review.limitations || []).join(' '))}</p>
    </section>`;
  }

  function positioningWorkbench(model) {
    const layer = model.positioning || {};
    const hasSnapshots = Array.isArray(layer.snapshots) && layer.snapshots.length > 0;
    const hasCases = Array.isArray(layer.cases) && layer.cases.length > 0;
    const unavailable = layer.status === 'data_unavailable' || (!hasSnapshots && !hasCases);
    const dataState = unavailable
      ? '<div class="positioning-empty"><strong>ДАННЫЕ ПОКА НЕДОСТУПНЫ.</strong><br>Фактические снимки OI, funding, фаз и исследовательских кейсов ещё не собраны или не опубликованы. Блок не достраивает их задним числом.</div>'
      : facts([['Снимков', layer.snapshots.length], ['Исследовательских кейсов', layer.cases.length], ['В shortlist', layer.shortlist?.length ?? 0]]);
    return `<section class="positioning-workbench" aria-label="Positioning crypto COT research">
      <div class="workbench-heading"><div><p class="kicker">Отдельный исследовательский слой</p><h2>◎ POSITIONING — CRYPTO COT / ИССЛЕДОВАНИЕ</h2><p class="subtitle">Контекст перекоса и ликвидности отдельно от Radar. Он не меняет очередь терминала и не создаёт рекомендации.</p></div>${badge(layer.status || 'data_unavailable')}</div>
      ${dataState}
      <p class="positioning-note">После накопления фактов слой будет держать максимум 3–5 независимых исследовательских кейсов с жизненным циклом WATCH → ARMED → INVALIDATED → COOLDOWN.</p>
      <a class="workbench-link" href="#/positioning">Открыть COT-исследование →</a>
    </section>`;
  }

  function radar(model) {
    const active = model.episodes.filter((episode) => ['research_only', 'manual_review'].includes(episode.status));
    const unavailable = model.episodes.filter((episode) => episode.status === 'data_unavailable');
    const blocked = model.episodes.filter((episode) => episode.status === 'no_trade');
    return `${pageHeader('Рабочее место', 'Два разных слоя: очередь для ручной проверки терминала и отдельное COT-исследование.')}
      ${acceptanceReview(model)}
      ${nowObservationQueue(model)}
      ${positioningWorkbench(model)}
      <section class="secondary-workbench" aria-label="Подтверждённые события, история и качество данных">
        <div class="section-header"><div><p class="kicker">Доказательства и история</p><h2>Подтверждённые события и качество данных</h2><p class="subtitle">Вторичный слой для проверки происхождения, статуса и полной истории наблюдений.</p></div></div>
        <div class="grid">${stat('Подтверждено для исследования', active.length)}${stat('Legacy-история', model.archive?.totalRecords ?? 0)}${stat('Заблокировано политикой', blocked.length)}</div>
        ${active.length ? `<div class="grid">${active.map(episodeCard).join('')}</div>` : `<div class="empty"><strong>Новых metadata-complete Radar-событий пока нет.</strong><br>История сохранена отдельно в <a href="#/archive">Pulse Archive</a>; неполные legacy-записи не становятся текущими наблюдениями.</div>`}
        ${unavailable.length ? `<p class="meta">${esc(unavailable.length)} legacy-эпизодов имеют недостаточные поля для текущей проекции. Их первичные записи доступны в <a href="#/archive">Pulse Archive</a>.</p>` : ''}
        <div class="secondary-links"><a href="#/archive">Pulse Archive</a><a href="#/data-health">Data Health</a><a href="#/methodology">Methodology</a><a href="#/validation">Validation</a></div>
        ${systemBanner(model)}
      </section>`;
  }

  function positioning(model) {
    const layer = model.positioning || { status: 'data_unavailable', universe: { observed: 0 }, shortlist: [], cases: [], snapshots: [], outcomes: { available: 0, horizonsMinutes: [5, 15, 30, 60] }, weeklyBrief: { status: 'data_unavailable', reason: 'Не опубликован.' }, limitations: [] };
    const snapshots = (layer.snapshots || []).slice(0, 20);
    const coverage = layer.historyCoverage || { rows: 0, ageMinutes: 0 };
    const brief = layer.currentBrief || { status: 'data_unavailable', rows: [] };
    const pct = (value) => Number.isFinite(value) ? `${value >= 0 ? '+' : ''}${value.toFixed(2)}%` : 'ещё нет окна';
    const amount = (value) => Number.isFinite(value) ? new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 }).format(value) : '—';
    const snapshotRows = snapshots.map((row) => `<tr><td>${esc(row.symbol)}</td><td>${esc(row.raw?.lastPrice ?? '—')}</td><td>${esc(row.raw?.openInterestValue ?? '—')}</td><td>${esc(row.raw?.fundingRate ?? '—')}</td><td>${esc(stamp(row.sourceTimestampUtc))}</td><td>${badge(row.dataQuality)}</td></tr>`).join('');
    const cases = (layer.cases || []).map((item) => `<tr><td>${esc(item.symbol)}</td><td>${esc(item.lifecycle)}</td><td>${esc(item.phase || 'не опубликована')}</td><td>${esc(item.evidence || 'не опубликовано')}</td><td>${esc(item.execution || 'не опубликовано')}</td></tr>`).join('');
    return `${pageHeader('Positioning · COT для крипты', 'Отдельный Bybit-only слой контекста. Он не меняет Radar и не является торговой рекомендацией.')}
      <section class="positioning-page-hero"><div class="row"><div><p class="kicker">Теневой режим · Bybit public data</p><h2>◎ POSITIONING — CRYPTO COT / ИССЛЕДОВАНИЕ</h2></div>${badge(layer.status)}</div><p>Это не классический COT с категориями участников. Экран показывает публичные цену, OI, funding и оборот, а затем — только фактически накопленные изменения. Он не меняет Radar и не даёт команд открыть сделку.</p></section>
      <section class="callout"><h2>Как читать этот слой</h2><ol class="terminal-checklist"><li><strong>Факт:</strong> цена, OI, funding и их изменение за накопленное окно.</li><li><strong>Гипотеза:</strong> совместное движение цены и OI может указывать на изменение плеча, но не раскрывает сторону или участника.</li><li><strong>Твоё действие:</strong> открыть монету в Tiger Trade, сверить фьючерс со спотом, затем проверить стакан и ленту.</li></ol></section>
      <div class="grid">${stat('Монет в текущем снимке', layer.universe?.observed ?? 0)}${stat('Строк истории', coverage.rows)}${stat('История собирается', `${Math.floor((coverage.ageMinutes || 0) / 60)} ч ${Math.round((coverage.ageMinutes || 0) % 60)} мин`)}${stat('Живых кейсов', layer.cases?.length ?? 0)}</div>
      <section class="callout"><h2>Готовность сравнений</h2>${facts([['Старт накопления', stamp(coverage.startedAtUtc)], ['1 час', coverage.oneHourReady ? 'готово' : 'собирается'], ['24 часа', coverage.oneDayReady ? 'готово' : 'собирается'], ['7 дней для Weekly Brief', coverage.oneWeekReady ? 'готово' : 'собирается']])}<p class="meta">Данные не достраиваются задним числом: каждое сравнение основано только на сохранённых снимках.</p></section>
      <section class="callout"><div class="row"><div><h2>Что меняется сейчас</h2><p class="meta">${esc(brief.reason || 'Контекст не опубликован.')}</p></div>${badge(brief.status)}</div>${brief.rows?.length ? `<div class="positioning-context-grid">${brief.rows.map((item) => card(`<div class="row"><h3>${esc(item.symbol)}</h3>${badge(item.dataQuality)}</div>${facts([['Цена', item.price ?? '—'], ['OI value', amount(item.openInterestValue)], ['Цена / 1ч', pct(item.change1h?.pricePct)], ['OI / 1ч', pct(item.change1h?.oiValuePct)], ['Цена / 24ч', pct(item.change24h?.pricePct)], ['OI / 24ч', pct(item.change24h?.oiValuePct)], ['Funding', Number.isFinite(item.fundingRate) ? `${(item.fundingRate * 100).toFixed(4)}%` : '—']])}<p>${esc(item.explanation || 'Интерпретация не опубликована.')}</p><p class="meta"><strong>В терминале:</strong> ${esc(item.terminalCheck || 'Сверь фьючерс, спот, стакан и ленту.')}</p><p class="meta">${esc(item.fundingNote || '')}</p>`)).join('')}</div>` : '<div class="empty">Новый снимок ещё не опубликован. Экран не подставляет исторические или вымышленные значения.</div>'}</section>
      <section class="callout"><h2>Исследовательские кейсы</h2>${layer.shortlist?.length ? `<p>${esc(layer.shortlist.join(', '))}</p>` : '<p class="muted">Подтверждённых Positioning-кейсов пока нет. Это честный пустой shortlist: система не выбирает монету только ради заполнения экрана.</p>'}<p class="meta">Будущий предел: 3–5 независимых кейсов. Для каждого: контекст → зона → подтверждение лентой/стаканом → отмена идеи → качество исполнения. Решение принимает человек.</p></section>
      <section class="callout"><h2>Последние Bybit-снимки</h2>${snapshotRows ? `<div class="table-wrap"><table><thead><tr><th>Инструмент</th><th>Цена</th><th>OI value</th><th>Funding</th><th>Время источника</th><th>Качество</th></tr></thead><tbody>${snapshotRows}</tbody></table></div>` : '<div class="empty">DATA UNAVAILABLE: collector ещё не опубликовал point-in-time снимок.</div>'}</section>
      <section class="callout"><h2>Жизненный цикл кейса</h2><p class="meta">WATCH → ARMED → INVALIDATED → COOLDOWN. V1 не создаёт ARMED и не отправляет уведомления.</p>${cases ? `<div class="table-wrap"><table><thead><tr><th>Инструмент</th><th>Статус</th><th>Фаза-гипотеза</th><th>Доказательства</th><th>Исполнимость</th></tr></thead><tbody>${cases}</tbody></table></div>` : '<div class="empty">Нет опубликованных кейсов: для фаз, корреляции, потока и исполнения данных V1 пока недостаточно.</div>'}</section>
      <section class="callout"><div class="row"><div><h2>Weekly Positioning Brief</h2><p>${esc(layer.weeklyBrief?.reason || 'Не опубликован.')}</p></div>${badge(layer.weeklyBrief?.status)}</div>${layer.weeklyBrief?.rows?.length ? `<div class="table-wrap"><table><thead><tr><th>Инструмент</th><th>Цена / 7д</th><th>OI / 7д</th><th>Контекст</th><th>Что проверить</th></tr></thead><tbody>${layer.weeklyBrief.rows.map((item) => `<tr><td>${esc(item.symbol)}</td><td>${esc(pct(item.price7dPct))}</td><td>${esc(pct(item.oiValue7dPct))}</td><td>${esc(item.explanation || '—')}</td><td>${esc(item.terminalCheck || '—')}</td></tr>`).join('')}</tbody></table></div>` : '<p class="meta">Weekly Brief появится только после 7 дней последовательных данных — без заднего заполнения.</p>'}</section>
      <section class="callout"><h2>Forward-проверка</h2><p>Для каждого будущего кейса отдельно записываются неизменяемый снимок и исходы через ${esc((layer.outcomes?.horizonsMinutes || []).join('/')) || '5/15/30/60'} минут. Пока кейсов нет, нет и статистики — это не доказательство плюса или минуса.</p></section>
      <section class="callout"><h2>Ограничения данных</h2><ul class="list">${(layer.limitations || []).map((item) => `<li>${esc(item)}</li>`).join('') || '<li>Не опубликованы.</li>'}</ul></section>`;
  }

  function archive(model) {
    const archive = model.archive || { totalRecords: 0, sources: [] };
    const sourceCard = (source) => {
      const rows = source.rows || [];
      const details = rows.map((row, index) => `<details class="archive-row"><summary>${esc(row.symbol || row.id || `Запись ${index + 1}`)} · исходные поля</summary><pre>${esc(JSON.stringify(row, null, 2))}</pre></details>`).join('');
      return `<section class="callout"><div class="row"><div><p class="kicker">${esc(source.id)}</p><h2>${esc(source.label)}</h2></div>${badge(source.status)}</div><p>${esc(source.provenance)}</p><p class="meta">Импортировано записей: ${esc(source.count)}. Эти поля сохранены как были опубликованы источником; они не используются как текущий торговый сигнал.</p>${details}</section>`;
    };
    return `${pageHeader('Pulse Archive', 'Полная legacy-история Pulse с сохранёнными исходными public-feed полями.')}
      <section class="callout warning"><strong>LEGACY / PROVENANCE INCOMPLETE.</strong> Архив не удалён и не является пустыми данными, но не имеет полного контракта свежего Radar: площадки, source-time, версии метода и/или quality attestation. Поэтому записи не считаются актуальными сетапами.</section>
      ${facts([['Всего legacy-записей', archive.totalRecords], ['Источников', archive.sources.length], ['Статус', title(archive.status)]])}
      ${archive.sources.length ? archive.sources.map(sourceCard).join('') : '<div class="empty">Legacy-источники не опубликованы в текущем feed.</div>'}`;
  }

  function facts(items) { return `<div class="facts">${items.map(([key, value]) => `<div class="fact"><span class="kicker">${esc(key)}</span><b>${esc(value)}</b></div>`).join('')}</div>`; }

  function bindNowObservationFilters() {
    const buttons = main.querySelectorAll('[data-live-filter]');
    const cards = main.querySelectorAll('[data-live-source]');
    buttons.forEach((button) => button.addEventListener('click', () => {
      const selected = button.dataset.liveFilter;
      buttons.forEach((item) => item.setAttribute('aria-pressed', String(item === button)));
      cards.forEach((item) => { item.hidden = selected !== 'all' && item.dataset.liveSource !== selected; });
    }));
  }

  function episode(model, id) {
    const item = model.episodes.find((episode) => episode.id === id);
    if (!item) return `${pageHeader('Episode not found', 'Запрошенный идентификатор отсутствует в текущей read-only проекции.')}<div class="empty">${esc(id)}</div>`;
    const rawRows = item.rawAlerts.map((raw) => `<li><b>${esc(stamp(raw.detectedAtUtc))}</b><br><span class="meta">${esc(raw.sourceModule)} · ${esc(raw.eventType)} · ${esc(raw.id)}</span></li>`).join('');
    const missing = item.dataQuality.missingFields.length ? item.dataQuality.missingFields.join(', ') : 'нет';
    const featureRows = Object.entries(item.features).filter(([, value]) => value !== null).map(([key, value]) => [key, value]);
    return `${pageHeader('Episode detail', 'Полная аудируемая карточка события и первичных срабатываний.')}
      <section class="callout"><div class="row"><div><p class="kicker">${esc(item.eventLabel)}</p><h2>${esc(item.symbol || 'Неидентифицированный инструмент')}</h2></div>${badge(item.status)}</div><p>${esc(item.statusReasons.join('; ') || 'Событие сохранено для исследования.')}</p></section>
      ${facts([['ID эпизода', item.id], ['Канонический инструмент', item.canonicalInstrumentId || 'не опубликован'], ['Первое наблюдение', stamp(item.firstDetectedAtUtc)], ['Последнее срабатывание', stamp(item.lastAlertAtUtc)], ['Первичных срабатываний', item.rawAlertCount], ['Версия метода', item.methodVersion || 'не опубликована'], ['Истечение', item.expiryAtUtc ? stamp(item.expiryAtUtc) : 'не опубликовано']])}
      <section class="callout"><h2>Граница интерпретации</h2><p>Ненаправленное исследовательское наблюдение. Платформа не публикует результат исполнения, цель или направление.</p></section>
      <section class="callout"><h2>Качество данных</h2>${badge(item.dataQuality.status)}${facts([['Источник', item.dataQuality.source || 'не опубликован'], ['Источник-время', stamp(item.dataQuality.sourceTimestampUtc)], ['Получено сервером', stamp(item.dataQuality.serverReceivedAtUtc)], ['Возраст источника', ms(item.dataQuality.sourceAgeMs)], ['Транспортная задержка', ms(item.dataQuality.observedTransportDelayMs)], ['Ликвидность', item.dataQuality.liquidityContext], ['Отсутствующие поля', missing]])}</section>
      <section class="callout"><h2>PumpWatch</h2>${badge(item.pumpWatch.status)}<p>${esc(item.pumpWatch.reason)}</p><p class="meta">Источник статуса: ${esc(item.pumpWatch.source || 'не опубликован')}. Отсутствие записи не означает CLEAR.</p></section>
      <section class="callout"><h2>Признаки</h2>${featureRows.length ? facts(featureRows) : '<p class="muted">Числовые признаки скрыты: источник не подтвердил полный набор обязательных данных.</p>'}</section>
      <section class="callout"><h2>Валидация</h2>${facts([['Включение в протокол', item.validationEligibility.eligible === null ? 'не опубликовано' : item.validationEligibility.eligible ? 'включено' : 'исключено'], ['Причина', item.validationEligibility.reason], ['Версия протокола', item.validationEligibility.protocolVersion || 'не опубликована']])}</section>
      <section class="callout"><h2>Raw event log</h2><ul class="list">${rawRows || '<li>Нет доступных строк.</li>'}</ul></section>
      <p><a href="#/radar">← Вернуться к Radar</a></p>`;
  }

  function pumpwatch(model) {
    const explicit = model.pumpWatch;
    const archive = model.pumpArchive;
    return `${pageHeader('PumpWatch', 'Риск-слой для вертикальных движений и поздних фаз импульса.')}
      <section class="callout warning"><strong>Блокировка не является прогнозом разворота.</strong> Она запрещает повышение статуса эпизода, пока backend публикует ACTIVE BLOCK.</section>
      <section class="section-header"><div><h2>Текущие опубликованные состояния</h2><p class="subtitle">Только состояния из backend-поля <code>pumpwatch_states</code>.</p></div></section>
      ${explicit.length ? `<div class="grid">${explicit.map((item) => card(`<div class="row"><h3>${esc(item.symbol || '—')}</h3>${badge(item.status)}</div><p>${esc(item.reason)}</p><p class="meta">Начало: ${esc(stamp(item.startedAtUtc))}<br>Версия: ${esc(item.methodVersion || 'не опубликована')}</p>`)).join('')}</div>` : '<div class="empty">Backend не опубликовал текущих PumpWatch states. Нельзя выводить CLEAR или блокировку по старым данным.</div>'}
      <section class="section-header"><div><h2>Архивные записи</h2><p class="subtitle">Не используются как текущая блокировка.</p></div></section>
      ${archive.length ? `<div class="table-wrap"><table><thead><tr><th>Инструмент</th><th>Время</th><th>Статус</th><th>Причина</th></tr></thead><tbody>${archive.slice(0, 50).map((item) => `<tr><td>${esc(item.symbol)}</td><td>${esc(stamp(item.timestampUtc))}</td><td>${esc(item.status)}</td><td>${esc(item.reason)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">Архивных записей нет.</div>'}`;
  }

  function validation(model) {
    const value = model.validation;
    return `${pageHeader('Validation', 'Результаты заранее заданного forward-протокола.')}
      <section class="callout ${value.resultsAvailable ? '' : 'warning'}"><div class="row"><div><p class="kicker">Статус протокола</p><h2>${esc(title(value.status))}</h2></div>${badge(value.status)}</div><p>${esc(value.conclusion)}</p></section>
      ${facts([['Версия протокола', value.version || 'не опубликована'], ['Freeze', value.frozen ? 'зафиксирован' : 'не подтверждён'], ['Период начала', stamp(value.windowStartUtc)], ['Период окончания', stamp(value.windowEndUtc)], ['Recorded', value.counts.recorded ?? '—'], ['Matched', value.counts.matched ?? '—'], ['Excluded', value.counts.excluded ?? '—'], ['Sealed', value.counts.sealed ?? '—']])}
      <section class="callout"><h2>Исключения</h2>${value.exclusionsAvailable ? `<ul class="list">${value.exclusions.map((item) => `<li>${esc(item.id)} — ${esc(item.reason)} · ${esc(stamp(item.timestampUtc))}</li>`).join('') || '<li>Опубликовано: исключений нет.</li>'}</ul>` : '<p class="muted">Аудируемый журнал исключений не опубликован. Результаты не могут быть показаны как подтверждённые.</p>'}</section>`;
  }

  function methodology(model) {
    return `${pageHeader('Methodology', 'Правила проекции, ограничения и опубликованные версии.')}
      <section class="callout"><h2>Принцип</h2><p>Frontend не вычисляет направленные статусы. Backend-проекция назначает статусы из опубликованных полей, а при нехватке данных возвращает DATA UNAVAILABLE.</p></section>
      ${facts([['Версия адаптера', model.adapterVersion], ['Окно дедупликации', `${model.policy.dedupWindowMs / 60000} минут`], ['Порог устаревания источника', `${model.policy.sourceStaleMs / 1000} секунд`], ['Хранилище', model.storage.mode], ['Постоянный аудит', model.storage.persistentAuditAvailable ? 'доступен' : 'не опубликован'], ['Полная первичная история', model.storage.rawHistoryComplete ? 'доступна' : 'не опубликована']])}
      <section class="callout"><h2>Дедупликация</h2><p>Эпизод строится по инструменту, площадке, совместимому классу события, версии метода и 30‑минутному UTC-окну. Если площадка или версия не опубликованы, идентичность помечается как неполная и запись не становится активной.</p></section>
      <section class="callout"><h2>Недоступные утверждения</h2><p>Не публикуются направление, исполнимый результат, доходность, торговая цель или stop. Старые directional и outcome-поля feed не переносятся в интерфейс.</p></section>`;
  }

  function dataHealth(model) {
    const health = model.health;
    const missing = Object.entries(health.missingRequiredFields);
    return `${pageHeader('Data Health', 'Актуальность, полнота и ограничения источника данных.')}
      ${systemBanner(model)}
      ${facts([['Feed сформирован', stamp(health.feedGeneratedAtUtc)], ['Возраст feed', ms(health.feedAgeMs)], ['Эпизодов', health.episodeCount], ['Верифицированных эпизодов', health.verifiedDataCount], ['Legacy-записей', health.legacyRecordCount ?? 0], ['Провайдеры', health.providers.join(', ') || 'не опубликованы'], ['Последняя валидная source-time', stamp(health.lastValidSourceTimestampUtc)], ['Модули', health.affectedModules.join(', ') || 'нет'], ['Последняя ошибка', health.lastError || 'не опубликована']])}
      <section class="callout"><h2>Импорт legacy-источников</h2>${Object.keys(health.legacySourceCounts || {}).length ? facts(Object.entries(health.legacySourceCounts)) : '<p>Legacy-источники не опубликованы.</p>'}</section>
      <section class="callout"><h2>Отсутствующие обязательные поля</h2>${missing.length ? `<div class="table-wrap"><table><thead><tr><th>Поле</th><th>Количество эпизодов</th></tr></thead><tbody>${missing.map(([field, count]) => `<tr><td>${esc(field)}</td><td>${esc(count)}</td></tr>`).join('')}</tbody></table></div>` : '<p>Для текущей проекции пропуски не обнаружены.</p>'}</section>`;
  }

  function rose(model) {
    return `${pageHeader('Rose Archive', 'Отдельный ретроспективный трекер наблюдений.')}
      <section class="callout"><strong>Архив не является торговой стратегией.</strong> Directional и outcome-поля старого feed скрыты до публикации абсолютных источников и аттестации качества.</section>
      ${model.rose.length ? `<div class="table-wrap"><table><thead><tr><th>Инструмент</th><th>Первое время</th><th>Источник</th><th>Статус качества</th><th>Ограничение</th></tr></thead><tbody>${model.rose.map((item) => `<tr><td>${esc(item.symbol)}</td><td>${esc(stamp(item.timestampUtc))}</td><td>${esc(item.sourceModule || 'не опубликован')}</td><td>${badge(item.dataQuality)}</td><td>${esc(item.limitations)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">Архивных записей нет.</div>'}`;
  }

  function render() {
    const fragments = location.hash.slice(2).split('/').filter(Boolean);
    const route = fragments[0] || 'radar';
    nav(route === 'episode' ? 'radar' : route);
    if (!overview) return;
    if (route === 'radar') main.innerHTML = radar(overview);
    else if (route === 'positioning') main.innerHTML = positioning(overview);
    else if (route === 'episode') main.innerHTML = episode(overview, decodeURIComponent(fragments[1] || ''));
    else if (route === 'archive') main.innerHTML = archive(overview);
    else if (route === 'pumpwatch') main.innerHTML = pumpwatch(overview);
    else if (route === 'validation') main.innerHTML = validation(overview);
    else if (route === 'methodology') main.innerHTML = methodology(overview);
    else if (route === 'data-health') main.innerHTML = dataHealth(overview);
    else if (route === 'rose') main.innerHTML = rose(overview);
    else main.innerHTML = `${pageHeader('Not found', 'Раздел не существует.')}<a href="#/radar">Перейти к Radar</a>`;
    if (route === 'radar') bindNowObservationFilters();
  }

  async function load() {
    try {
      const response = await fetch('/api/research?path=overview', { headers: { Accept: 'application/json' } });
      overview = await response.json();
      if (!response.ok) throw new Error(overview.error || 'api_unavailable');
      systemPill.className = `pill ${overview.systemStatus}`;
      systemPill.textContent = title(overview.systemStatus);
    } catch (error) {
      overview = { systemStatus: 'unavailable', health: { overallStatus: 'unavailable', feedGeneratedAtUtc: null, feedAgeMs: null, episodeCount: 0, verifiedDataCount: 0, legacyRecordCount: 0, legacySourceCounts: {}, providers: [], affectedModules: [], missingRequiredFields: {}, lastError: 'Источник feed недоступен; актуальность не подтверждена.' }, episodes: [], nowObservations: [], positioning: { status: 'data_unavailable', universe: { observed: 0 }, shortlist: [], cases: [], snapshots: [], outcomes: { available: 0, horizonsMinutes: [5, 15, 30, 60] }, weeklyBrief: { status: 'data_unavailable', reason: 'Источник feed недоступен.' }, limitations: [] }, archive: { totalRecords: 0, sources: [] }, pumpWatch: [], pumpArchive: [], validation: { status: 'unavailable', conclusion: 'Статистическое подтверждение не опубликовано', counts: {}, exclusionsAvailable: false, exclusions: [] }, rose: [], policy: {}, storage: {}, adapterVersion: 'unavailable' };
      systemPill.className = 'pill unavailable'; systemPill.textContent = 'UNAVAILABLE';
    }
    render();
  }
  window.addEventListener('hashchange', render);
  window.addEventListener('DOMContentLoaded', load);
})();

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
  const LAB_ROUTE = 'smart-money-lab';
  const routeAliases = new Map([
    ['smart-money', LAB_ROUTE],
    ['smartmoney', LAB_ROUTE],
    ['smart_money_lab', LAB_ROUTE],
    ['sml', LAB_ROUTE],
  ]);
  const canonicalRoute = (hash = location.hash) => {
    const raw = String(hash || '').replace(/^#\/?/, '').split('/').filter(Boolean)[0] || 'radar';
    const route = decodeURIComponent(raw).trim().toLowerCase();
    return routeAliases.get(route) || route;
  };
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

  function sourceStatus(status) {
    return ({ independently_reviewed: 'НЕЗАВИСИМО ПРОВЕРЕНО', course_reference: 'КОНСПЕКТ КУРСА', unverified: 'ТРЕБУЕТ ПРОВЕРКИ' }[status] || 'НЕ УКАЗАНО');
  }

  function timeframeStudyCard(frame) {
    return `<article class="sm-timeframe"><p class="kicker">${esc(frame.role || 'Наблюдение')} · ${esc(frame.timeframe)}</p><b>${esc(stamp(frame.startAtUtc))} — ${esc(stamp(frame.endAtUtc))}</b><p>${esc(frame.observation)}</p></article>`;
  }

  function smartMoneyCaseCard(item) {
    return `<article class="sm-case">
      <div class="row"><div><p class="kicker">${esc(item.exchange)} · ${esc(item.asset)}</p><h2>${esc(item.title || item.formation)}</h2></div><span class="sm-source ${esc(item.sourceStatus)}">${esc(sourceStatus(item.sourceStatus))}</span></div>
      <p class="sm-formation">Формация: <strong>${esc(item.formation)}</strong></p>
      <section><h3>Таймлайн и таймфреймы</h3><div class="sm-timeframes">${item.timeframes.map(timeframeStudyCard).join('')}</div></section>
      <section><h3>Что видно на графике</h3><ul class="sm-evidence">${item.evidence.map((evidence) => `<li><b>${esc(evidence.label)}${evidence.level ? ` · ${esc(evidence.level)}` : ''}</b><span>${esc(evidence.observation)}</span></li>`).join('')}</ul></section>
      <section class="sm-rule-grid"><div><p class="kicker">Учебная гипотеза входа</p><p>${esc(item.hypothesis)}</p></div><div><p class="kicker">Условие отмены</p><p>${esc(item.invalidation)}</p></div></section>
      <p class="meta">Источник: ${esc(item.sourceLabel || 'не опубликован')} · проверено: ${esc(stamp(item.reviewedAtUtc))}. Карточка описывает прошлый учебный кейс, не текущую сделку.</p>
    </article>`;
  }

  function smartMoneyStrategyOne() {
    return `<article class="sm-case sm-strategy">
      <div class="row"><div><p class="kicker">SML-01 · переработанная текстовая схема</p><h2>Sweep → подтверждение структуры</h2></div><span class="sm-source course_reference">КОНСПЕКТ КУРСА</span></div>
      <p class="sm-warning"><strong>Учебная гипотеза, не торговый сигнал и не статистически подтверждённая стратегия.</strong> Это правило для разбора в Bar Replay, а не команда открыть сделку.</p>
      <div class="sm-diagram" aria-label="Схема Sweep и подтверждение структуры"><div><b>1. HTF уровень</b><span>STH = краткосрочный high<br>или STL = краткосрочный low</span></div><i>→</i><div><b>2. Sweep</b><span>Цена выходит за заранее отмеченный swing<br>и возвращается / закрывается обратно через уровень</span></div><i>→</i><div><b>3. LTF закрытие</b><span>Не тень: свеча закрывается за последним<br>явно выбранным HL или LH</span></div></div>
      <section><h3>Пары таймфреймов из курса</h3><div class="sm-timeframes"><article class="sm-timeframe"><p class="kicker">Контекст → подтверждение</p><b>Sweep 1D → 1H</b><p>Дневной уровень задаёт локацию; H1 проверяет смену локальной структуры.</p></article><article class="sm-timeframe"><p class="kicker">Контекст → подтверждение</p><b>Sweep 4H → 15M</b><p>H4 даёт локацию; M15 нужен только для подтверждения, а не для угадывания вершины.</p></article><article class="sm-timeframe"><p class="kicker">Контекст → подтверждение</p><b>Sweep 1H → 5M</b><p>H1 даёт локацию; M5 даёт наиболее раннее, но более шумное подтверждение.</p></article></div></section>
      <section><h3>Что считать подтверждением</h3><ul class="sm-evidence"><li><b>Уровень</b><span>До движения отметить STH/старый swing high или STL/старый swing low. Без заранее выбранного уровня это не sweep, а задняя дорисовка.</span></li><li><b>Правило sweep</b><span>Цена должна пройти за этот swing и вернуться обратно; удержание и закрытие за уровнем — не тот же сценарий.</span></li><li><b>Структура LTF</b><span>Для идеи после sweep high искать закрытие ниже последнего подтверждённого HL; после sweep low — закрытие выше последнего LH. Одна тень не считается.</span></li><li><b>Термин</b><span>Первый слом против прошлого движения часто точнее назвать CHOCH; курс называет его BOS. В карточках Pulse будет указан конкретный HL/LH, а не только аббревиатура.</span></li></ul></section>
      <section class="sm-rule-grid"><div><p class="kicker">Учебная гипотеза входа</p><p>После закрытия LTF за выбранным HL/LH: либо на закрытии — более ранний вариант, либо на ретесте уровня — более избирательный. Выбор фиксируется до просмотра будущих свечей.</p></div><div><p class="kicker">Отмена</p><p>Нет закрытия LTF за уровнем; цена удержалась по другую сторону экстремума sweep; рабочий HL/LH выбран неоднозначно; до ближайшей цели не остаётся приемлемого R.</p></div></section>
      <section class="sm-rule-grid"><div><p class="kicker">Защита идеи</p><p>Учебный стоп ставится за экстремумом sweep с запасом на реальное исполнение. Это место отмены идеи, не универсальный размер риска.</p></div><div><p class="kicker">Цель из курса</p><p>Курс использует 2R. 2R — заранее выбранная цель относительно риска, а не обещание, что цена туда дойдёт; её нужно сравнить с ближайшим уровнем ликвидности.</p></div></section>
    </article>`;
  }

  function smartMoneyStrategyTwo() {
    return `<article class="sm-case sm-strategy">
      <div class="row"><div><p class="kicker">SML-02 · переработанная текстовая схема</p><h2>TDP / Three Drives → подтверждение структуры</h2></div><span class="sm-source course_reference">КОНСПЕКТ КУРСА</span></div>
      <p class="sm-warning"><strong>Учебная гипотеза, не торговый сигнал и не статистически подтверждённая стратегия.</strong> В курсе TDP упрощён: три локальных high, а не обязательно классический гармонический паттерн Three Drives.</p>
      <div class="sm-diagram" aria-label="Схема трёх локальных максимумов"><div><b>High #1</b><span>Первый отделённый swing high</span></div><i>→</i><div><b>High #2</b><span>Второй отдельный swing high</span></div><i>→</i><div><b>High #3</b><span>Кандидат на sweep / терминальную фазу</span></div><i>→</i><div><b>Подтверждение</b><span>Закрытие ниже HL / neckline</span></div></div>
      <section><h3>Медвежий учебный протокол</h3><ul class="sm-evidence"><li><b>Три high</b><span>На formation timeframe нужны три разделённых локальных максимума. Не склеивай соседние свечи в «три вершины» задним числом.</span></li><li><b>Третий high</b><span>Это только кандидат на sweep/истощение, а не причина немедленно шортить.</span></li><li><b>Подтверждение</b><span>Ждать закрытие ниже последнего подтверждённого HL или neckline. Допустимы учебные варианты входа: закрытие или ретест.</span></li><li><b>Отмена</b><span>Нет закрытия структуры; принятие цены выше high #3; число swing неоднозначно; риск до стопа не оставляет разумного R.</span></li></ul></section>
      <section class="sm-rule-grid"><div><p class="kicker">Защита идеи</p><p>Учебный стоп — за high #3, так как закрепление выше него отменяет именно эту короткую гипотезу.</p></div><div><p class="kicker">Цель из курса</p><p>Курс указывает origin/base импульса, предшествовавшего high #1. Это ориентир для проверки пространства, не гарантированная цель.</p></div></section>
      <section class="callout warning"><h3>Источник требует уточнения</h3><p>В списке курса встречается «TDP 1H / BOS 4H», а в тексте предлагается «перейти на 4H»; это конфликтует с обычной иерархией, где подтверждение ниже formation timeframe. До явного определения иерархии этот вариант нельзя исполнять или добавлять в статистику.</p><p class="meta">Скриншот курса показывает BTCUSD / Bitstamp, но дата, год и точные уровни из него не верифицируются — они здесь намеренно не заявляются.</p></section>
    </article>`;
  }

  function smartMoneyStrategyThree() {
    return `<article class="sm-case sm-strategy">
      <div class="row"><div><p class="kicker">SML-03 · переработанная текстовая схема</p><h2>Границы Range → подтверждение структуры</h2></div><span class="sm-source course_reference">КОНСПЕКТ КУРСА</span></div>
      <p class="sm-warning"><strong>Учебная гипотеза, не торговый сигнал и не статистически подтверждённая стратегия.</strong> Сначала диапазон и его границы, затем реакция на младшем ТФ; не вход по касанию границы.</p>
      <div class="sm-diagram" aria-label="Схема границ диапазона"><div><b>Верхняя граница</b><span>0.0 · зона для сценария short</span></div><i>↓</i><div><b>Середина</b><span>0.5 · первая зона проверки / частичной фиксации в курсе</span></div><i>↓</i><div><b>Нижняя граница</b><span>1.0 · зона для сценария long</span></div></div>
      <section><h3>Иерархия таймфреймов из курса</h3><div class="sm-timeframes"><article class="sm-timeframe"><p class="kicker">Контекст → подтверждение</p><b>1MO (month?) → 1D</b><p>В оригинале написано «1M». Здесь это не M1: нужна явная проверка автора, что имеется в виду месяц.</p></article><article class="sm-timeframe"><p class="kicker">Контекст → подтверждение</p><b>1D → 1H</b><p>Дневной диапазон задаёт границы; H1 нужен для структуры.</p></article><article class="sm-timeframe"><p class="kicker">Контекст → подтверждение</p><b>4H → 15M</b><p>H4-диапазон, затем подтверждение на M15.</p></article><article class="sm-timeframe"><p class="kicker">Контекст → подтверждение</p><b>1H → 5M</b><p>H1-диапазон, затем более быстрый и шумный M5.</p></article></div></section>
      <section><h3>Long от нижней границы</h3><ul class="sm-evidence"><li><b>Range на HTF</b><span>До реакции отмечены верхняя, нижняя границы и midpoint 0.5. Цена приходит к нижней границе; допускается прокол тенью, но сам касание не является входом.</span></li><li><b>Подтверждение LTF</b><span>Ждать закрытие телом выше последнего явно выбранного LH. Курс называет это BOS; первый разворотный слом многие называют CHOCH.</span></li><li><b>План</b><span>Учебный вход: закрытие или ретест LTF-уровня. Стоп — ниже формации / low, с которого цена ушла из range. Цель: midpoint или верхняя граница; в курсе midpoint допускается как частичная фиксация.</span></li></ul></section>
      <section><h3>Short от верхней границы</h3><ul class="sm-evidence"><li><b>Зеркально</b><span>Цена приходит к верхней границе, затем на LTF нужно закрытие телом ниже последнего HL. Вход — закрытие или ретест; стоп — выше формации / high ухода из range.</span></li><li><b>Цели</b><span>Сначала midpoint или частичная фиксация, затем противоположная граница только если пространство и структура это допускают.</span></li></ul></section>
      <section class="sm-rule-grid"><div><p class="kicker">Отмена</p><p>HTF закрывается и принимается за границей; нет LTF закрытия телом; сам range не определён заранее; риск до стопа не оставляет приемлемого R.</p></div><div><p class="kicker">Нельзя подменять правило</p><p>Range — это не любой боковик. Если границы нарисованы только после движения, учебный кейс недействителен.</p></div></section>
      <section class="callout warning"><h3>Источник требует уточнения</h3><p>Курс не даёт механического правила валидности range: число касаний, допустимая ширина и способ выбора границы. Ещё одна внутренняя несостыковка — «Range 1H / BOS 4H»: подтверждение выше контекста. Этот вариант помечен как неисполняемый до определения иерархии.</p><p class="meta">Скриншот курса использует AUDUSD / Forexcom. Это иллюстрация, не live-крипто-доказательство; точные даты и исполнение из изображения здесь не заявляются.</p></section>
    </article>`;
  }

  function smartMoneyConcepts() {
    return `<section class="sm-concepts" aria-label="База Smart Money Lab">
      <article class="sm-concept"><p class="kicker">SML-B01 · фракталы и swing</p><h2>Фрактал — это подтверждённый pivot, а не предсказание</h2>
        <div class="sm-mini-diagram"><span>левая свеча</span><b>центральный high / low</b><span>правая свеча закрылась</span></div>
        <p>В курсе используется 3-барный pivot: центральная свеча выше двух соседей для high или ниже двух соседей для low. Он становится известен только после закрытия правой, третьей свечи — значит pivot ретроспективен и не может быть сигналом в момент образования центральной свечи.</p>
        <p class="meta">Распространённый Bill Williams Fractal обычно использует пять баров, а не три. Индикатор отмечает pivots по своему параметру; он не находит «все» swing и не доказывает, что профессионалы индикаторы не используют.</p></article>
      <article class="sm-concept"><p class="kicker">SML-B02 · структура</p><h2>Назови swing до того, как назовёшь слом</h2>
        <div class="sm-structure"><div><b>Восходящая</b><span>HH + HL</span></div><div><b>Нисходящая</b><span>LH + LL</span></div></div>
        <ul class="sm-evidence"><li><b>Uptrend</b><span>Для учебной структуры нужны higher high и higher low (HH + HL).</span></li><li><b>Downtrend</b><span>Для учебной структуры нужны lower high и lower low (LH + LL).</span></li><li><b>Подтверждение</b><span>Закрытие телом за заранее выбранным HL/LH, а не только прокол тенью. Это уменьшает число ложных отметок, но не устраняет их.</span></li><li><b>Термины</b><span>Курс называет первый контртрендовый слом BOS; многие школы называют его CHOCH. В Pulse важнее указывать конкретный reference swing, чем спорить об аббревиатуре.</span></li></ul>
        <p class="meta">Метка CONF — кастомная/динамическая; без точного правила автора и reference swing она не будет трактоваться как стандартный сигнал. «Fake BOS»/SFP здесь — описания: прокол, закрытие обратно в структуру и отвержение. SFP определяется по-разному в разных школах; это не доказательство «манипуляции ликвидностью».</p></article>
      <article class="sm-concept"><p class="kicker">SML-B03 · macro / micro</p><h2>Старший ТФ задаёт контекст, младший — проверку</h2>
        <p>В каждом кейсе должна быть объявлена пара, например <b>4H → 15M</b>. Macro — структура и локация на первом ТФ; micro — конкретное закрытие за HL/LH на втором.</p>
        <p>Вход только в сторону macro — риск-фильтр из курса, а не универсальное правило рынка. Если пара ТФ не задана, фраза «структура сломана» недостаточна для разбора.</p></article>
      <article class="sm-concept"><p class="kicker">SML-B04 · workflow и POI</p><h2>POI — место проверки, не кнопка входа</h2>
        <ol class="terminal-checklist"><li>Задать asset, venue и macro→micro пару.</li><li>До реакции отметить range, swing или POI.</li><li>Дождаться прихода цены к POI.</li><li>На LTF проверить конкретное закрытие структуры.</li><li>До симуляции/сделки записать отмену и пространство до цели.</li></ol>
        <p>POI (point of interest) — заранее отмеченная зона интереса. Приход в неё не даёт вход; POI + подтверждение — двухуровневая учебная схема, а не заявление о надёжности.</p>
        <p class="sm-warning">Любые проценты «богатства», «психологии толпы» или успеха из материалов без источника и методики считаются неподтверждёнными и не выводятся как факт.</p></article>
      <article class="sm-concept"><p class="kicker">SML-B05 · FVG и похожие ярлыки</p><h2>FVG: сначала точное определение, потом название</h2>
        <p>Базовый трёхсвечный FVG: в бычьем варианте low третьей свечи остаётся выше high первой; в медвежьем — high третьей ниже low первой. Между первой и третьей остаётся ценовой gap. Средняя свеча лишь создаёт импульс между ними.</p>
        <details class="details"><summary>Варианты из курса — все являются гипотезами курса</summary><ul class="list"><li>Doji FVG</li><li>Variation gap</li><li>Breakaway gap</li><li>Rejection FVG</li><li>Пересечение с фракталом / FVA</li></ul><p class="meta">Для каждого варианта: «гипотеза курса; верифицированный win rate не опубликован».</p></details>
        <p class="meta">FVA, FVG, order block, mitigation, breaker, rejection и wick — school-specific labels. Они не эквивалентны друг другу по умолчанию и не должны автоматически объединяться в один сигнал.</p></article>
      <article class="sm-concept"><p class="kicker">SML-B06 · Fibonacci</p><h2>Математическое отношение ≠ доказанное преимущество</h2>
        <p>Уровни Fibonacci можно оставить как дополнительную confluence-пометку: они выражают математические отношения, но сами по себе не доказывают предсказательную силу или edge. В учебной карточке Fib никогда не заменяет POI, закрытие структуры и отмену.</p></article>
      <article class="sm-concept"><p class="kicker">SML-B07 · корреляции</p><h2>Контекст, не причина и не автопилот</h2>
        <ul class="sm-evidence"><li><b>DXY</b><span>Индекс доллара США, а не Bitcoin. Его связь с BTC меняется во времени.</span></li><li><b>EURUSD / DXY</b><span>Часто имеют обратную тенденцию, но она не постоянна и не является торговой командой.</span></li><li><b>USDCAD, NASDAQ/SPX, GER40, XAU/XAG</b><span>Связи с крипторынком зависят от периода, режима и инструмента. Формулировка «X следует за Y» не используется.</span></li></ul>
        <p>В Pulse такие рынки можно смотреть только как внешний контекст с конкретным периодом и коэффициентом/источником, а не как причинное объяснение движения монеты.</p></article>
      <article class="sm-concept sm-sessions"><p class="kicker">SML-B08 · сессии и часы</p><h2>Окна наблюдения в крипте — контекст, не расписание волатильности</h2>
        <div class="table-wrap"><table><thead><tr><th>Окно из курса</th><th>MSK (UTC+3), когда Нью‑Йорк EDT</th><th>MSK (UTC+3), когда Нью‑Йорк EST</th></tr></thead><tbody><tr><td>Asia / AKZ</td><td>03:00–07:00</td><td>04:00–08:00</td></tr><tr><td>London</td><td>09:00–12:00</td><td>10:00–13:00</td></tr><tr><td>New York</td><td>14:00–17:00</td><td>15:00–18:00</td></tr><tr><td>US cash core</td><td>16:30–23:00</td><td>17:30–00:00</td></tr><tr><td>US equity final hour</td><td>22:00–23:00</td><td>23:00–00:00</td></tr></tbody></table></div>
        <p>Это discretionary / ICT observation windows, не официальные биржевые сессии и не обещание волатильности крипты, которая торгуется 24/7. «Lunch» — частое более тихое окно, не официальное закрытие.</p>
        <p>Курсовой «power hour» в 00:00/01:00 MSK совпадает с FX rollover 17:00 New York, а не с US equity power hour. Для американских акций final hour — последняя строка таблицы.</p>
        <p class="meta">Источники часов: <a href="https://www.nyse.com/markets/hours-calendars" target="_blank" rel="noreferrer">nyse.com/markets/hours-calendars</a> · <a href="https://www.oanda.com/assets/documents/252/Hours_of_Operation.pdf" target="_blank" rel="noreferrer">OANDA Hours of Operation PDF</a>.</p></article>
    </section>`;
  }

  function smartMoneyAdvancedModels() {
    const tag = (label = 'КОНСПЕКТ КУРСА') => `<span class="sm-source course_reference">${label}</span>`;
    return `<section class="sm-advanced" aria-label="Модели входа и словарь Smart Money">
      <article class="sm-concept"><div class="row"><div><p class="kicker">SML-C01 · модели входа</p><h2>Подтверждение — это правило, а не название</h2></div>${tag()}</div>
        <ul class="sm-evidence"><li><b>BOS</b><span>Выбрать конкретный pivot заранее и ждать закрытие телом через него; одной тени недостаточно.</span></li><li><b>MSS + displacement</b><span>Ярлык курса: смена структуры плюс импульсное движение/FVG. Это не самостоятельное доказательство силы или направления.</span></li><li><b>STM / Sharp Turn Model</b><span>Выход из POI с импульсом/FVG без обязательного structural break. По определению имеет меньшее подтверждение, чем модель с закрытием структуры.</span></li><li><b>FTA</b><span>Первое видимое препятствие между POI и намеченной целью; только кандидат для risk-screening, не установленный факт ликвидности.</span></li></ul>
        <details class="details"><summary>Пары из курса — только Bar Replay гипотезы</summary><ul class="list"><li>POI 4H → BOS 15M.</li><li>MSS + displacement: POI 4H → 5M; затем ждать 15M FVG-вариант, определённый курсом.</li><li>STM: POI 4H → 15M; затем ждать H1-вариант, определённый курсом.</li></ul><p class="meta">Для любого случая обязателен явный source hierarchy. Entry, stop и invalidation здесь применяются только как Bar Replay-гипотезы.</p></details></article>
      <article class="sm-concept"><div class="row"><div><p class="kicker">SML-C02 · вложенная структура</p><h2>STH/STL → ITH/ITL → LTH/LTL</h2></div>${tag()}</div>
        <div class="sm-diagram"><div><b>STH / STL</b><span>3-барный базовый pivot</span></div><i>→</i><div><b>ITH / ITL</b><span>центр higher-order STH/STL среди трёх</span></div><i>→</i><div><b>LTH / LTL</b><span>следующий вложенный уровень</span></div></div>
        <p>Это course-specific иерархия. Нельзя автоматически привязывать её к фиксированной паре H1 → H4 или называть «true BOS». Конвенция «BOS только после CONF» — запаздывающий фильтр: она может убрать часть ложных сломов, но также пропустить движение или дать более поздний вход; доказательства преимущества не опубликованы.</p>
        <p class="meta">ITL→ITH и ITH→ITL в курсе — цель swing-разметки, не прогноз. Stop ставится за уровнем отмены гипотезы, без гарантии исполнения. В кейсе нужно перечислить: HTF / POI TF / confirmation TF.</p></article>
      <article class="sm-concept"><div class="row"><div><p class="kicker">SML-C03 · Fib / OTE</p><h2>Уровень нужен привязать к swing</h2></div>${tag('ГИПОТЕЗА КУРСА')}</div>
        <p>0.5 — не число Fibonacci. Зона OTE курса: 0.705 / 0.75 / 0.79, где 0.79 приблизительно 0.786. До применения нужно механически назвать anchors swing: от какого high до какого low построена сетка и почему эти pivots подтверждены.</p>
        <p class="meta">Отношения не доказывают предсказательную силу. Fib/OTE — только optional confluence и не заменяет POI, body close и invalidation.</p></article>
      <article class="sm-concept"><div class="row"><div><p class="kicker">SML-C04 · словарь уровней</p><h2>Сначала критерий, потом «ликвидность»</h2></div>${tag()}</div>
        <ul class="sm-evidence"><li><b>PCH / PCL</b><span>Previous candle high / low — high/low предыдущей свечи на явно указанном ТФ.</span></li><li><b>EQH / EQL</b><span>Equal highs/lows требуют заявить допуск: тик/процент и ТФ. Без tolerance это субъективный ярлык.</span></li><li><b>Shelf, trend, ERL, IRL</b><span>Кандидаты reference levels/ranges. По графику нельзя утверждать, что там видны стопы, объёмы или «магнит».</span></li><li><b>Sweep / raid / run</b><span>Нестандартные ярлыки: использовать определение курса с точным body/close критерием, но не выдавать за доказанный сигнал разворота/продолжения.</span></li></ul>
        <p class="sm-warning">«Топливо», «ресурс» и «institutional manipulation» — язык курса, а не измеренный факт активности институционального участника.</p></article>
      <article class="sm-concept"><div class="row"><div><p class="kicker">SML-C05 · candle science и POI</p><h2>Реакция не равна автоматическому входу</h2></div>${tag()}</div>
        <p><b>Respect</b> — курсовой ярлык rejection-like свечи на POI. <b>Disrespect</b> — закрытие через POI. Оба слова описывают наблюдение, не гарантию.</p>
        <p><b>2CR</b> допустимо использовать только после точного правила: первая свеча отвечает от POI, следующая закрывается за high/low этой rejection-свечи. 2CR не тождественен ERL и не означает более высокую надёжность.</p>
        <p class="meta">В Bar Replay фиксируй POI, обе свечи и отмену. Без этого нельзя помечать ситуацию как «подтверждённую».</p></article>
      <article class="sm-concept"><div class="row"><div><p class="kicker">SML-C06 · FVG: определение и надстройки</p><h2>Тестируем базовый gap, не историю о причинах</h2></div>${tag('ГИПОТЕЗА КУРСА')}</div>
        <p>Бычий FVG: <code>low(C3) &gt; high(C1)</code>; медвежий — обратное условие. Направление свечи №2 часто считают импульсом, но оно не входит в само определение gap.</p>
        <details class="details"><summary>Надстройки курса — без верифицированного win rate</summary><ul class="list"><li>variation, rejection, breakaway, ordinary/consolidating, mitigated, overlapping;</li><li>BIS/SIB, liquidity void/gap, volume imbalance, inversion, BPR.</li></ul><p class="meta">Каждый термин: course_reference / unverified hypothesis. FVG не доказывает наличие неисполненных институциональных ордеров и не обязан заполняться.</p></details></article>
      <article class="sm-concept sm-wide"><div class="row"><div><p class="kicker">SML-C07 · FVA / OB / orderflow</p><h2>Школьная таксономия — не данные о крупных участниках</h2></div>${tag('КОНСПЕКТ КУРСА / НЕПРОВЕРЕННАЯ ГИПОТЕЗА')}</div>
        <div class="sm-glossary"><div><b>FVA / OB / MB / BB / Rejection block / wick</b><span>Определения меняются между школами; край каждой коррекции не становится order block автоматически.</span></div><div><b>Orderflow, IOF, leg, OH/OL</b><span>Proprietary course taxonomy. Использовать как описание уважения/пробоя заранее отмеченных зон, не как доказательство действий большого игрока.</span></div><div><b>FLOD / OLOD / LLOD / FLIP</b><span>Курсовые ярлыки, которым требуется определение автора и reference level в каждой карточке.</span></div><div><b>PD premium / discount</b><span>Требует заранее заданный dealing range. Premium не равен автоматическому short, discount не равен автоматическому long.</span></div><div><b>Context area / target</b><span>Только course terms: их границы, ТФ и условие отмены должны быть названы до просмотра продолжения.</span></div></div>
      </article>
    </section>`;
  }

  function smartMoneyRangeAndWyckoff() {
    const tag = (label = 'КОНСПЕКТ КУРСА') => `<span class="sm-source course_reference">${label}</span>`;
    return `<section class="sm-range-wyckoff" aria-label="Range и Wyckoff Smart Money Lab">
      <article class="sm-concept"><div class="row"><div><p class="kicker">SML-D01 · FVA и delivery terms</p><h2>FVA / FLOD / OLOD — словарь автора курса</h2></div>${tag('КОНСПЕКТ КУРСА / НЕПРОВЕРЕНО')}</div>
        <p>FVA, FLOD, OLOD и nested-FVA — proprietary course taxonomy. Их можно использовать только как имена заранее отмеченных зон и вложенных зон, с указанным ТФ и границами.</p>
        <p class="sm-warning">Эти ярлыки не доказывают order flow, наличие крупных заявок или обязанность цены «заполнить» какой-либо ордер.</p></article>
      <article class="sm-concept"><div class="row"><div><p class="kicker">SML-D02 · range</p><h2>Диапазон должен быть измерен до реакции</h2></div>${tag()}</div>
        <div class="sm-diagram"><div><b>Верхняя граница</b><span>курс: зона SND / возможное отклонение</span></div><i>↓</i><div><b>Midpoint</b><span>0.5 диапазона</span></div><i>↓</i><div><b>Нижняя граница</b><span>курс: зона SND / возможное отклонение</span></div></div>
        <p>Курс использует SND, границы, midpoint, deviations и TDP как разметку range. Одна или две deviation и фраза «в range нет orderflow» не являются универсальными фактами. Незавершённое подтверждение range — не сигнал входа.</p>
        <ol class="terminal-checklist"><li>Записать asset, venue и timeframe range.</li><li>До реакции зафиксировать верх/низ, midpoint и численный tolerance для касания/выхода.</li><li>Указать механическое правило SND/TDP либо прямо отметить, что автор его не дал.</li><li>Назвать invalidation: например, закрытие/принятие за границей по заранее выбранному ТФ.</li><li>Воспроизвести сценарий в Bar Replay без будущих свечей.</li></ol></article>
      <article class="sm-concept sm-wide"><div class="row"><div><p class="kicker">SML-D03 · Wyckoff</p><h2>Исторический словарь, а не детектор «умных денег»</h2></div>${tag('ИСТОРИЧЕСКАЯ ТЕРМИНОЛОГИЯ')}</div>
        <div class="sm-wyckoff-grid"><div><b>Накопление / reaccumulation</b><span>Фазы A–E; типовые ярлыки PS, SC, AR, ST, Spring, SOS, LPS.</span></div><div><b>Распределение / redistribution</b><span>Фазы A–E; типовые ярлыки PSY, BC, AR, ST, UT, UTAD, SOW, LPSY.</span></div><div><b>Как использовать</b><span>В учебном кейсе отмечать только конкретную фазу, диапазон и критерий отмены; не присваивать фазу постфактум после уже известного движения.</span></div></div>
        <p>Цена и объём сами по себе не доказывают действия smart money. Объём биржи venue-specific; крипторынок фрагментирован. В spot FX нет консолидированного tape, а tick volume зависит от брокера. Поэтому любые Wyckoff-ярлыки остаются описанием, пока не задан источник и правило наблюдения.</p></article>
      <article class="sm-concept"><div class="row"><div><p class="kicker">SML-D04 · MMBM / MMSM</p><h2>Price-delivery идея курса</h2></div>${tag('НЕПРОВЕРЕННАЯ ГИПОТЕЗА')}</div>
        <p>MMBM и MMSM в этом материале — course-specific идеи доставки цены. В Pulse они могут быть учебными ярлыками для сравнения разметок, но не доказательством причинности со стороны market maker и не оценкой вероятности.</p></article>
      <article class="sm-concept sm-wide"><div class="row"><div><p class="kicker">SML-D05 · два orderflow-leg</p><h2>Цепочка проверки вместо мгновенного входа</h2></div>${tag()}</div>
        <div class="sm-flow"><div><b>1. HTF bias</b><span>Курсовый контекст, не прогноз</span></div><i>→</i><div><b>2. Current leg</b><span>Предразмеченный FVG/FLOD</span></div><i>→</i><div><b>3. LTF response</b><span>Точное body-close правило</span></div><i>→</i><div><b>4. Два leg</b><span>Сравнить их разметку и отмену</span></div><i>→</i><div><b>5. Replay-plan</b><span>Кандидат, stop/invalidation</span></div></div>
        <p>Карта «1H против M1/M5» в курсе неоднозначна: для каждого кейса надо явно написать, какой ТФ — HTF, какой — FVG/FLOD и какой — подтверждение. Без этой карты фраза «два leg» не воспроизводима.</p>
        <p class="meta">2R и context objectives можно использовать только как примеры измерения пространства в Bar Replay, а не предсказания результата.</p></article>
    </section>`;
  }

  const REPLAY_HYPOTHESES = Object.freeze([
    {
      id: 'SML-R01', status: 'hypothesis', asset: 'DEMOUSDT · синтетический пример', venueSource: 'Original Pulse diagram · не биржевой feed',
      observedAtUtc: '2026-09-18T12:00:00.000Z', htf: '4H', ltf: '15M', model: 'Sweep → body-close structure check',
      pivots: 'HTF: заранее отмеченный STH; LTF: последний HL выбран только после закрытия правой свечи.', tolerance: 'Учебный допуск: 1 synthetic tick; для рынка нужен явный тик/процент.',
      evidence: ['Цена вышла за заранее размеченный HTF high и вернулась под него.', 'На LTF есть закрытие телом ниже выбранного HL; одной тени было бы недостаточно.'],
      alternative: 'Это может быть принятие цены выше уровня после краткого возврата, а не разворотная структура.',
      invalidation: 'LTF закрывается и удерживается выше экстремума; HL выбран неоднозначно; HTF-level не был задан до движения.',
    },
  ]);

  function replayHypothesisCard(record) {
    return `<article class="sm-replay-card">
      <div class="row"><div><p class="kicker">${esc(record.id)} · ручная / replay запись</p><h2>${esc(record.model)}</h2></div><span class="sm-source unverified">ГИПОТЕЗА</span></div>
      ${facts([['Asset', record.asset], ['Venue / source', record.venueSource], ['Наблюдение', stamp(record.observedAtUtc)], ['HTF', record.htf], ['LTF', record.ltf], ['Pivot rule', record.pivots], ['Tolerance', record.tolerance]])}
      <section><h3>Наблюдаемые признаки</h3><ul class="sm-evidence">${record.evidence.map((item) => `<li><b>Наблюдение</b><span>${esc(item)}</span></li>`).join('')}</ul></section>
      <section class="sm-rule-grid"><div><p class="kicker">Конкурирующая трактовка</p><p>${esc(record.alternative)}</p></div><div><p class="kicker">Отмена гипотезы</p><p>${esc(record.invalidation)}</p></div></section>
      <p class="meta">Не обнаружено автоматически, не является сигналом, прогнозом цены или свидетельством действий участника рынка.</p>
    </article>`;
  }

  function smartMoneyReplayWorkbench() {
    return `<section class="sm-replay-workbench" aria-label="Журнал учебных гипотез Smart Money Lab">
      <div class="row"><div><p class="kicker">Ручная разметка · static / replay only</p><h2>Журнал гипотез: график → структура</h2></div><span class="sm-source unverified">НЕ АВТОДЕТЕКТОР</span></div>
      <p>Это аудируемый шаблон для твоего разбора графика или Bar Replay. Он не получает цены, свечи или стакан из сети и не пытается сам определить модель.</p>
      <section class="sm-schema"><h3>Обязательные поля записи</h3><div class="sm-glossary"><div><b>Контекст</b><span>asset, venue/source, observation timestamp, HTF и LTF.</span></div><div><b>Разметка</b><span>Явные pivots, tolerance/тик или процент, выбранная course model.</span></div><div><b>Проверяемость</b><span>Наблюдаемые evidence, competing interpretation, invalidation и статус <code>hypothesis</code>.</span></div></div></section>
      <div class="sm-replay-grid">${REPLAY_HYPOTHESES.map(replayHypothesisCard).join('')}</div>
      <p class="sm-warning">Нельзя записывать «структура есть» без выбранных pivot и body-close правила. Нельзя считать учебную запись автоматическим обнаружением, торговой рекомендацией или оценкой вероятности.</p>
    </section>`;
  }

  function smartMoneyLab(model) {
    const lab = model.smartMoneyLab || { status: 'data_unavailable', cases: [], limitations: [] };
    const demo = `<section class="sm-blueprint" aria-label="Как читать кейс Smart Money Lab">
      <p class="kicker">Оригинальная схема интерфейса · не рыночный кейс</p><h2>Один сетап всегда читается сверху вниз</h2>
      <div class="sm-flow"><div><b>HTF</b><span>Локация и диапазон</span></div><i>→</i><div><b>Средний ТФ</b><span>Снятие уровня / реакция</span></div><i>→</i><div><b>LTF</b><span>Подтверждение структуры</span></div><i>→</i><div><b>План</b><span>Гипотеза и отмена</span></div></div>
      <p class="meta">Чтобы случай был учебно полезен, в нём обязательно указываются актив, площадка, абсолютный период, минимум два ТФ, наблюдаемые признаки, гипотеза и отмена. Статус источника не равен доказательству прибыльности.</p>
    </section>`;
    const noCases = `<div class="empty sm-empty"><strong>Учебные кейсы ещё не опубликованы.</strong><br>Когда ты передашь материалы, мы разложим каждый случай по времени, нескольким таймфреймам и проверяемым условиям. Оригинальные скриншоты курса в публичный Pulse не выводятся.</div>`;
    return `${pageHeader('Smart Money Lab', 'Учебная библиотека многотаймфреймовых ситуаций. Это не радар, не рекомендация и не команда открыть сделку.')}
      <section class="sm-hero"><div class="row"><div><p class="kicker">Учебный слой · ручной разбор</p><h2>◈ SMART MONEY LAB</h2></div>${badge(lab.status)}</div><p>Каждая карточка объясняет, где именно на графике возникла ситуация: актив, площадка, абсолютные даты, роль каждого таймфрейма, наблюдаемые признаки и граница отмены идеи.</p></section>
      ${demo}
      <section class="sm-guardrails"><h2>Как использовать</h2><ol class="terminal-checklist"><li>Сначала воспроизведи указанный период в Bar Replay без будущих свечей.</li><li>Отметь на своих графиках уровни и структуру до раскрытия продолжения.</li><li>Сверь условия: если отсутствует хотя бы одно — это не тот же учебный кейс.</li><li>Не переносить старую формацию в текущий рынок без новой ручной проверки.</li></ol></section>
      <section class="section-header"><div><p class="kicker">Опубликованные учебные случаи</p><h2>Кейсы с временными рамками</h2><p class="subtitle">Только структурированный текст и оригинальные схемы; чужие скриншоты обучения не публикуются.</p></div></section>
      <section class="section-header"><div><p class="kicker">Стартовые стратегии из конспекта</p><h2>Текстовые схемы без оригинальных скриншотов</h2><p class="subtitle">Сначала отработай схему в Bar Replay; затем добавляй только случаи с полностью указанным временем и ТФ.</p></div></section>
      <div class="sm-case-grid">${smartMoneyStrategyOne()}${smartMoneyStrategyTwo()}${smartMoneyStrategyThree()}</div>
      <section class="section-header"><div><p class="kicker">Основа для разбора</p><h2>Термины, фильтры и контекст</h2><p class="subtitle">Сначала проверяем определение и временной масштаб; затем сравниваем ситуацию с учебной карточкой.</p></div></section>
      ${smartMoneyConcepts()}
      <section class="section-header"><div><p class="kicker">Модели и расширенный словарь</p><h2>Структура вместо ярлыков</h2><p class="subtitle">Все модели ниже служат для разметки и Bar Replay, не для автосигналов или заявлений о действиях участников.</p></div></section>
      ${smartMoneyAdvancedModels()}
      <section class="section-header"><div><p class="kicker">Range, Wyckoff и delivery language</p><h2>Размечай контекст до движения</h2><p class="subtitle">Эти карточки помогают записать условия и отмену; они не объясняют причины движения цены.</p></div></section>
      ${smartMoneyRangeAndWyckoff()}
      <section class="section-header"><div><p class="kicker">Ручной Bar Replay</p><h2>Журнал гипотез без автодетектора</h2><p class="subtitle">Шаблон связывает наблюдения с возможной структурой и одновременно сохраняет альтернативную трактовку и отмену.</p></div></section>
      ${smartMoneyReplayWorkbench()}
      ${lab.cases.length ? `<section class="section-header"><div><p class="kicker">Разобранные рыночные случаи</p><h2>Кейсы с временными рамками</h2></div></section><div class="sm-case-grid">${lab.cases.map(smartMoneyCaseCard).join('')}</div>` : noCases}
      ${lab.limitations.length ? `<section class="callout warning"><h2>Ограничения источника</h2><ul class="list">${lab.limitations.map((item) => `<li>${esc(item)}</li>`).join('')}</ul></section>` : ''}`;
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
    const fragments = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
    const route = canonicalRoute();
    nav(route === 'episode' ? 'radar' : route);
    if (!overview) return;
    if (route === 'radar') main.innerHTML = radar(overview);
    else if (route === 'positioning') main.innerHTML = positioning(overview);
    else if (route === 'smart-money-lab') main.innerHTML = smartMoneyLab(overview);
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
      overview = { systemStatus: 'unavailable', health: { overallStatus: 'unavailable', feedGeneratedAtUtc: null, feedAgeMs: null, episodeCount: 0, verifiedDataCount: 0, legacyRecordCount: 0, legacySourceCounts: {}, providers: [], affectedModules: [], missingRequiredFields: {}, lastError: 'Источник feed недоступен; актуальность не подтверждена.' }, episodes: [], nowObservations: [], positioning: { status: 'data_unavailable', universe: { observed: 0 }, shortlist: [], cases: [], snapshots: [], outcomes: { available: 0, horizonsMinutes: [5, 15, 30, 60] }, weeklyBrief: { status: 'data_unavailable', reason: 'Источник feed недоступен.' }, limitations: [] }, smartMoneyLab: { status: 'data_unavailable', cases: [], limitations: [] }, archive: { totalRecords: 0, sources: [] }, pumpWatch: [], pumpArchive: [], validation: { status: 'unavailable', conclusion: 'Статистическое подтверждение не опубликовано', counts: {}, exclusionsAvailable: false, exclusions: [] }, rose: [], policy: {}, storage: {}, adapterVersion: 'unavailable' };
      systemPill.className = 'pill unavailable'; systemPill.textContent = 'UNAVAILABLE';
    }
    render();
  }
  window.addEventListener('hashchange', render);
  window.addEventListener('DOMContentLoaded', load);
})();

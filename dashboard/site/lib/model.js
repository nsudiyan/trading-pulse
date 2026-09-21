const { createHash } = require('node:crypto');

// This policy describes only the server-side projection. It is separate from
// detector rules and must never create missing market facts.
const POLICY = Object.freeze({
  version: 'ui-projection-v2',
  dedupWindowMs: 30 * 60 * 1000,
  sourceStaleMs: 120 * 1000,
  feedStaleMs: 120 * 1000,
});

const arr = (value) => Array.isArray(value) ? value : [];
const text = (value) => typeof value === 'string' && value.trim() ? value.trim().slice(0, 500) : null;
const number = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value) : null;
const utc = (value) => typeof value === 'string' && /(Z|[+-]\d{2}:\d{2})$/i.test(value) && Number.isFinite(Date.parse(value)) ? new Date(value).toISOString() : null;
const canonical = (value) => typeof value === 'string' && /^[a-z0-9]+([/_-][a-z0-9]+)*$/i.test(value.trim()) ? value.trim().toUpperCase().replace(/[/_-]/g, '') : null;
const digest = (value) => createHash('sha256').update(JSON.stringify(value)).digest('hex').slice(0, 20);
const EVENT_LABELS = Object.freeze({ radar: 'Относительная объёмная активность', radar_alt: 'Относительная объёмная активность', awakening: 'Изменение активности', storm: 'Режим повышенной внутридневной активности', sweep: 'Изменение локального диапазона', combo: 'Совпадение наблюдений' });
const LEGACY_STATUS = 'legacy_provenance_incomplete';

function eventClass(eventType) {
  if (['radar', 'radar_alt', 'awakening'].includes(eventType)) return 'volume_activity';
  if (eventType === 'storm') return 'intraday_activity';
  if (eventType === 'sweep') return 'range_change';
  if (eventType === 'combo') return 'combined_observation';
  return 'unknown';
}

function normalize(raw, module) {
  const eventType = text(raw.eventType || raw.kind || raw.source || (module === 'sweeps' ? 'sweep' : null)) || 'unknown';
  const context = raw.ctx && typeof raw.ctx === 'object' ? raw.ctx : {};
  return {
    id: text(raw.id) || `RAW-${digest([module, raw])}`,
    symbol: canonical(raw.symbol), venue: text(raw.venue)?.toUpperCase() || null, provider: text(raw.provider),
    detectedAtUtc: utc(raw.detectedAtUtc || raw.ts_utc || raw.signal_ts || raw.first_ts || raw.ts),
    sourceTimestampUtc: utc(raw.sourceTimestampUtc || raw.source_timestamp_utc || raw.source_ts_utc),
    serverReceivedAtUtc: utc(raw.serverReceivedAtUtc || raw.server_received_at_utc || raw.receipt_ts_utc),
    timeframe: text(raw.timeframe), eventType, eventClass: eventClass(eventType), eventLabel: EVENT_LABELS[eventType] || 'Исследовательское наблюдение', sourceModule: module,
    methodVersion: text(raw.methodVersion || raw.modelVersion || raw.method_version), price: number(raw.price ?? raw.last_price ?? raw.basis), volume: number(raw.volume ?? raw.turnover ?? raw.turnover_usdt),
    dataQuality: text(typeof raw.dataQuality === 'object' ? raw.dataQuality.status : raw.dataQuality), liquidityVerified: raw.liquidityVerified === true,
    reviewVerifiedAtUtc: utc(raw.reviewVerifiedAtUtc), expiresAtUtc: utc(raw.expiresAtUtc), invalidatedAtUtc: utc(raw.invalidatedAtUtc), invalidationReason: text(raw.invalidationReason), reportedRepeatCount: number(raw.emits ?? raw.confirms),
    features: { relativeVolume: number(raw.vol_ratio ?? raw.vol_x ?? context.vol_ratio), rangePct: number(raw.wick_pct ?? raw.range_pct), observedChangePct: number(context.live_at_zone ?? raw.change_pct) },
    validationEligibility: raw.validationEligibility && typeof raw.validationEligibility === 'object' ? { eligible: raw.validationEligibility.eligible === true, reason: text(raw.validationEligibility.reason), excludedAtUtc: utc(raw.validationEligibility.excludedAtUtc), protocolVersion: text(raw.validationEligibility.protocolVersion) } : { eligible: null, reason: 'Решение о включении не опубликовано', excludedAtUtc: null, protocolVersion: null },
  };
}

function pumpStates(feed) {
  // Only explicit backend states may block. Legacy rows remain archive material.
  return arr(feed.pumpwatch_states).map((raw) => {
    const status = text(raw.status)?.toLowerCase().replaceAll(' ', '_');
    return { symbol: canonical(raw.symbol), venue: text(raw.venue)?.toUpperCase() || null, status: ['clear', 'watch', 'active_block', 'resolving', 'closed'].includes(status) ? status : 'unavailable', reason: text(raw.reason) || 'Причина не опубликована', startedAtUtc: utc(raw.startedAtUtc || raw.ts_utc || raw.ts), reviewedAtUtc: utc(raw.reviewedAtUtc), unblockedAtUtc: utc(raw.unblockedAtUtc), stage: text(raw.stage), methodVersion: text(raw.methodVersion), history: arr(raw.history).map((entry) => ({ status: text(entry.status), timestampUtc: utc(entry.timestampUtc), reason: text(entry.reason) })), source: 'pumpwatch_states', venueScope: raw.venue ? 'venue' : 'symbol-wide' };
  });
}

function quality(raw, now, feedFresh) {
  const missing = [];
  for (const field of ['symbol', 'venue', 'detectedAtUtc', 'sourceTimestampUtc', 'serverReceivedAtUtc', 'timeframe', 'methodVersion', 'provider']) if (!raw[field]) missing.push(field);
  if (!(raw.price > 0)) missing.push('price');
  if (raw.volume === null || raw.volume < 0) missing.push('volume');
  if (raw.dataQuality !== 'verified') missing.push('dataQuality.verified');
  const sourceMs = raw.sourceTimestampUtc ? Date.parse(raw.sourceTimestampUtc) : null;
  const receiptMs = raw.serverReceivedAtUtc ? Date.parse(raw.serverReceivedAtUtc) : null;
  const sourceAgeMs = sourceMs === null ? null : now - sourceMs;
  if (sourceAgeMs !== null && sourceAgeMs < -5000) missing.push('sourceTimestampUtc.in_future');
  if (receiptMs !== null && (receiptMs > now + 5000 || (sourceMs !== null && receiptMs < sourceMs))) missing.push('serverReceivedAtUtc.invalid_order');
  if (raw.detectedAtUtc && Date.parse(raw.detectedAtUtc) > now + 5000) missing.push('detectedAtUtc.in_future');
  const stale = sourceAgeMs !== null && sourceAgeMs > POLICY.sourceStaleMs;
  return { status: missing.length ? 'unavailable' : (stale || !feedFresh ? 'delayed' : 'verified'), missingFields: [...new Set(missing)], sourceTimestampUtc: raw.sourceTimestampUtc, serverReceivedAtUtc: raw.serverReceivedAtUtc, sourceAgeMs, observedTransportDelayMs: sourceMs !== null && receiptMs !== null && receiptMs >= sourceMs ? receiptMs - sourceMs : null, liquidityContext: raw.liquidityVerified ? 'verified' : 'unavailable', source: raw.provider, stale, feedFresh };
}

function validation(feed) {
  const protocol = feed.forward_movement && typeof feed.forward_movement === 'object' ? feed.forward_movement : {};
  const sourceStatus = text(protocol.status), allowed = ['draft', 'registered', 'collecting', 'frozen', 'analyzing', 'published', 'superseded'];
  const status = allowed.includes(sourceStatus?.toLowerCase()) ? sourceStatus.toLowerCase() : 'unavailable', version = text(protocol.protocol_version);
  const exclusions = arr(protocol.exclusions).map((entry) => ({ id: text(entry.id), reason: text(entry.reason), timestampUtc: utc(entry.timestampUtc), protocolVersion: text(entry.protocolVersion) }));
  const exclusionsAvailable = number(protocol.excluded_alerts) === exclusions.length && exclusions.every((entry) => entry.id && entry.reason && entry.timestampUtc && entry.protocolVersion === version);
  const published = status === 'published' && Boolean(version && protocol.methodology && protocol.controls && exclusionsAvailable);
  return { version, status, sourceStatus, frozen: protocol.frozen === true, freezeTimestampUtc: utc(protocol.freeze_timestamp_utc), windowStartUtc: utc(protocol.evaluation_start_utc), windowEndUtc: utc(protocol.evaluation_end_utc_exclusive), receiptTimeBasis: text(protocol.receipt_time_basis), preregHash: text(protocol.prereg_sha256), counts: { recorded: number(protocol.alerts_recorded), matched: number(protocol.matched_sets), excluded: number(protocol.excluded_alerts), sealed: number(protocol.resolved_sets_sealed) }, finalWindowCounts: { matched: number(protocol.final?.n_matched), resolved: number(protocol.final?.n_resolved), days: number(protocol.final?.active_utc_days) }, resultsAvailable: published, conclusion: published ? text(protocol.final?.verdict) || 'Вердикт не опубликован' : 'Статистическое подтверждение не опубликовано', results: published ? protocol.final || null : null, exclusions, exclusionsAvailable };
}

function legacyArchive(feed) {
  // These are verbatim public-feed records. They remain separate because
  // their provenance is incomplete for the current Radar contract.
  const sources = [
    ['live_signals', 'Legacy live-signal feed', arr(feed.live_signals)],
    ['signals_history', 'Signal history', arr(feed.signals_history)],
    ['diary_records', 'Observation diary', arr(feed.diary?.records)],
    ['rose_tracks', 'Rose retrospective tracks', arr(feed.rose?.tracks)],
    ['pump_muted', 'PumpWatch suppressed archive', arr(feed.pump_muted)],
  ].map(([id, label, rows]) => ({
    id, label, count: rows.length, status: LEGACY_STATUS,
    provenance: 'Original public feed fields retained; venue, source timestamp, method version and/or quality attestation are incomplete for current Radar.',
    rows,
  })).filter((source) => source.count > 0);
  const summaries = [
    ['radar_summary', 'Radar aggregate', feed.radar],
    ['bot_stats', 'Bot aggregate', feed.bot_stats],
  ].filter(([, , value]) => value && typeof value === 'object' && Object.keys(value).length)
    .map(([id, label, value]) => ({ id, label, count: 1, status: LEGACY_STATUS, provenance: 'Original public feed aggregate retained; not a current event.', rows: [value] }));
  const all = [...sources, ...summaries];
  return { status: LEGACY_STATUS, totalRecords: all.reduce((sum, source) => sum + source.count, 0), sources: all };
}

function currentObservations(feed) {
  // `live_signals` is a raw intake, not a trade queue.  Do not promote it to
  // a Radar episode and never substitute trade-plan values for market facts.
  return arr(feed.live_signals).map((raw, index) => ({
    id: text(raw.id) || `LIVE-${digest([index, raw])}`,
    symbol: canonical(raw.symbol),
    sourceModule: text(raw.source) || 'live_signals',
    observedAtUtc: utc(raw.ts_utc),
    // Never substitute trade-plan fields such as `entry` for a live price.
    livePrice: number(raw.live_price ?? raw.last_price ?? raw.price),
    liveChangePct: number(raw.live_change_pct ?? raw.change_pct),
    relativeVolume: number(raw.vol_ratio),
    majorRadar: raw.major_radar === true,
    status: 'manual_check',
  })).sort((left, right) => (Date.parse(right.observedAtUtc || '') || 0) - (Date.parse(left.observedAtUtc || '') || 0) || left.id.localeCompare(right.id));
}

function terminalFocus(observations, now) {
  // A deliberately narrow, transparent attention filter. It cannot make a
  // trade claim: it only limits terminal attention to fresh radar observations
  // with the source's published volume-anomaly field.
  const twentyMinutes = 20 * 60 * 1000;
  const bestBySymbol = new Map();
  for (const item of observations) {
    const observed = item.observedAtUtc ? Date.parse(item.observedAtUtc) : NaN;
    const fresh = Number.isFinite(observed) && now >= observed && now - observed <= twentyMinutes;
    if (!fresh || item.sourceModule !== 'radar' || !(item.relativeVolume >= 5) || !item.symbol) continue;
    const previous = bestBySymbol.get(item.symbol);
    if (!previous || Number(item.majorRadar) > Number(previous.majorRadar) || item.relativeVolume > previous.relativeVolume || observed > Date.parse(previous.observedAtUtc)) bestBySymbol.set(item.symbol, item);
  }
  return [...bestBySymbol.values()].sort((left, right) => Number(right.majorRadar) - Number(left.majorRadar) || right.relativeVolume - left.relativeVolume || Date.parse(right.observedAtUtc) - Date.parse(left.observedAtUtc)).slice(0, 3);
}

function acceptanceReview(feed) {
  // This projection is a separate forward-only quality journal.  It is never
  // used to infer a direction or execution plan from a Radar event.
  const raw = feed.acceptance_review && typeof feed.acceptance_review === 'object' ? feed.acceptance_review : {};
  const allowed = new Set(['pending_closed_m5', 'accepted_for_manual_review', 'rejected_for_review']);
  const rows = arr(raw.active).map((row) => ({
    id: text(row.event_id), symbol: canonical(row.symbol), venue: text(row.venue)?.toUpperCase() || null,
    status: text(row.status), sourceTimestampUtc: utc(row.source_timestamp_utc),
    decisionTimestampUtc: utc(row.decision_ts_utc), basis: number(row.basis), relativeVolume: number(row.vol_ratio),
    elapsedMinutes: number(row.elapsed_min), dominantExcursionPct: number(row.dominant_excursion_pct),
    adverseExcursionPct: number(row.adverse_excursion_pct), retentionRatio: number(row.retention_ratio),
    sameSideCloses: number(row.same_side_closes), reasons: arr(row.reason_codes).map(text).filter(Boolean),
  })).filter((row) => row.id && row.symbol && allowed.has(row.status));
  const counts = raw.counts && typeof raw.counts === 'object' ? {
    pending: number(raw.counts.pending) || 0,
    accepted: number(raw.counts.accepted) || 0,
    rejected: number(raw.counts.rejected) || 0,
  } : { pending: 0, accepted: 0, rejected: 0 };
  return {
    protocol: text(raw.protocol) || 'H-IMPULSE-ACCEPT-01',
    mode: text(raw.mode) || 'unavailable', startedAtUtc: utc(raw.started_at_utc), delayMinutes: number(raw.delay_minutes),
    active: rows.sort((left, right) => (right.sourceTimestampUtc || '').localeCompare(left.sourceTimestampUtc || '')),
    counts, limitations: arr(raw.limitations).map(text).filter(Boolean),
  };
}

function positioning(feed) {
  // Independent shadow projection. It cannot create a Radar event, execution,
  // or a directional recommendation from incomplete public fields.
  const raw = feed.positioning && typeof feed.positioning === 'object' ? feed.positioning : {};
  const allowedStatus = new Set(['collecting', 'data_unavailable']);
  const allowedLifecycle = new Set(['watch', 'armed', 'invalidated', 'cooldown', 'archived']);
  const snapshots = arr(raw.snapshots).map((row) => ({
    symbol: canonical(row.symbol), venue: text(row.venue)?.toUpperCase() || null,
    provider: text(row.provider), sourceTimestampUtc: utc(row.source_timestamp_utc),
    serverReceivedAtUtc: utc(row.server_received_at_utc), dataQuality: text(row.data_quality) || 'unavailable',
    raw: row.raw && typeof row.raw === 'object' ? {
      lastPrice: number(row.raw.last_price), openInterestValue: number(row.raw.open_interest_value),
      turnover24h: number(row.raw.turnover_24h), fundingRate: number(row.raw.funding_rate),
    } : {},
  })).filter((row) => row.symbol);
  const cases = arr(raw.cases).map((row) => ({
    id: text(row.case_id), symbol: canonical(row.symbol), lifecycle: text(row.lifecycle)?.toLowerCase(),
    phase: text(row.phase_hypothesis), evidence: text(row.evidence_status), execution: text(row.execution_status),
    createdAtUtc: utc(row.created_at_utc), updatedAtUtc: utc(row.updated_at_utc),
  })).filter((row) => row.id && row.symbol && allowedLifecycle.has(row.lifecycle));
  const coverage = raw.history_coverage && typeof raw.history_coverage === 'object' ? {
    rows: number(raw.history_coverage.rows) || 0, startedAtUtc: utc(raw.history_coverage.started_at_utc),
    ageMinutes: number(raw.history_coverage.age_minutes) || 0,
    oneHourReady: raw.history_coverage.one_hour_ready === true, oneDayReady: raw.history_coverage.one_day_ready === true, oneWeekReady: raw.history_coverage.one_week_ready === true,
  } : { rows: 0, startedAtUtc: null, ageMinutes: 0, oneHourReady: false, oneDayReady: false, oneWeekReady: false };
  const contextRows = arr(raw.current_brief?.rows).map((row) => ({
    symbol: canonical(row.symbol), asOfUtc: utc(row.as_of_utc), price: number(row.price), openInterestValue: number(row.open_interest_value), turnover24h: number(row.turnover_24h), fundingRate: number(row.funding_rate),
    change1h: { available: row.change_1h?.available === true, pricePct: number(row.change_1h?.price_pct), oiValuePct: number(row.change_1h?.oi_value_pct) },
    change24h: { available: row.change_24h?.available === true, pricePct: number(row.change_24h?.price_pct), oiValuePct: number(row.change_24h?.oi_value_pct) },
    explanation: text(row.explanation), terminalCheck: text(row.terminal_check), fundingNote: text(row.funding_note), unknowns: arr(row.unknowns).map(text).filter(Boolean), dataQuality: text(row.data_quality) || 'unavailable',
  })).filter((row) => row.symbol);
  const weeklyRows = arr(raw.weekly_brief?.rows).map((row) => ({
    symbol: canonical(row.symbol), price7dPct: number(row.price_7d_pct), oiValue7dPct: number(row.oi_value_7d_pct), fundingRate: number(row.funding_rate), turnover24h: number(row.turnover_24h), explanation: text(row.explanation), terminalCheck: text(row.terminal_check),
  })).filter((row) => row.symbol);
  return {
    mode: 'shadow',
    status: allowedStatus.has(text(raw.status)?.toLowerCase()) ? text(raw.status).toLowerCase() : 'data_unavailable',
    generatedAtUtc: utc(raw.generated_at_utc),
    source: raw.source && typeof raw.source === 'object' ? { venue: text(raw.source.venue)?.toUpperCase() || null, provider: text(raw.source.provider), category: text(raw.source.category) } : {},
    universe: raw.universe && typeof raw.universe === 'object' ? { selection: text(raw.universe.selection), cap: number(raw.universe.cap), observed: number(raw.universe.observed) } : { observed: 0 },
    shortlist: arr(raw.shortlist).map((row) => canonical(typeof row === 'string' ? row : row?.symbol)).filter(Boolean).slice(0, 5),
    cases, snapshots,
    outcomes: raw.outcomes && typeof raw.outcomes === 'object' ? { horizonsMinutes: arr(raw.outcomes.horizons_minutes).map(number).filter((value) => value !== null), available: number(raw.outcomes.available) || 0, status: text(raw.outcomes.status) || 'data_unavailable' } : { horizonsMinutes: [], available: 0, status: 'data_unavailable' },
    historyCoverage: coverage,
    currentBrief: raw.current_brief && typeof raw.current_brief === 'object' ? { status: text(raw.current_brief.status) || 'data_unavailable', reason: text(raw.current_brief.reason), rows: contextRows } : { status: 'data_unavailable', reason: 'Не опубликован.', rows: [] },
    weeklyBrief: raw.weekly_brief && typeof raw.weekly_brief === 'object' ? { status: text(raw.weekly_brief.status) || 'data_unavailable', reason: text(raw.weekly_brief.reason), rows: weeklyRows } : { status: 'data_unavailable', reason: 'Не опубликован.', rows: [] },
    limitations: arr(raw.limitations).map(text).filter(Boolean),
  };
}

function smartMoneyLab(feed) {
  // Education-only projection.  Course images are deliberately not carried
  // through the public feed: a case may publish only original text evidence
  // and a source-status label. It cannot produce a live signal or execution.
  const raw = feed.smart_money_lab && typeof feed.smart_money_lab === 'object' ? feed.smart_money_lab : {};
  const allowedStatus = new Set(['collecting', 'published', 'data_unavailable']);
  const allowedSourceStatus = new Set(['independently_reviewed', 'course_reference', 'unverified']);
  const cases = arr(raw.cases).map((row) => {
    const timeframes = arr(row.timeframes).map((frame) => ({
      timeframe: text(frame.timeframe), startAtUtc: utc(frame.start_at_utc), endAtUtc: utc(frame.end_at_utc),
      observation: text(frame.observation), role: text(frame.role),
    })).filter((frame) => frame.timeframe && frame.startAtUtc && frame.endAtUtc && frame.observation);
    const evidence = arr(row.visual_evidence).map((item) => ({
      label: text(item.label), observation: text(item.observation), level: text(item.level),
    })).filter((item) => item.label && item.observation);
    return {
      id: text(row.case_id), asset: canonical(row.asset || row.symbol), exchange: text(row.exchange || row.venue)?.toUpperCase() || null,
      formation: text(row.formation), title: text(row.title), timeframes, evidence,
      hypothesis: text(row.entry_hypothesis), invalidation: text(row.invalidation),
      sourceStatus: text(row.source_status)?.toLowerCase(), sourceLabel: text(row.source_label),
      reviewedAtUtc: utc(row.reviewed_at_utc),
    };
  }).filter((item) => item.id && item.asset && item.exchange && item.formation && item.timeframes.length >= 2 && item.evidence.length && item.hypothesis && item.invalidation && allowedSourceStatus.has(item.sourceStatus));
  return {
    status: allowedStatus.has(text(raw.status)?.toLowerCase()) ? text(raw.status).toLowerCase() : 'data_unavailable',
    cases: cases.sort((left, right) => (right.reviewedAtUtc || '').localeCompare(left.reviewedAtUtc || '')),
    limitations: arr(raw.limitations).map(text).filter(Boolean),
  };
}

function rawSources(feed) { return [['raw_market_events', arr(feed.raw_market_events)], ['live_signals', arr(feed.live_signals)], ['signals_history', arr(feed.signals_history)], ['sweeps', arr(feed.sweeps)], ['combos', arr(feed.combos)], ['impulse_review', arr(feed.impulse_review)], ['diary', arr(feed.diary?.records)]]; }
function episodeKey(raw, index) { const bucket = raw.detectedAtUtc ? Math.floor(Date.parse(raw.detectedAtUtc) / POLICY.dedupWindowMs) : null; return [raw.venue || 'UNVERIFIED-VENUE', raw.symbol || `unknown-${index}`, raw.eventClass, raw.methodVersion || 'UNVERIFIED-METHOD', bucket]; }

function buildModel(feed = {}, now = Date.now()) {
  const generatedAtUtc = utc(feed.generated_at), feedAgeMs = generatedAtUtc ? now - Date.parse(generatedAtUtc) : null, feedFresh = feedAgeMs !== null && feedAgeMs >= -5000 && feedAgeMs <= POLICY.feedStaleMs, pumpWatch = pumpStates(feed), groups = new Map(), seen = new Set();
  let rawIndex = 0;
  for (const [module, sourceRows] of rawSources(feed)) for (const row of sourceRows) {
    // A first-party raw event and the legacy display feed may refer to the
    // same source event.  The stable source event id wins over module origin.
    const raw = normalize(row, module), duplicateKey = digest([raw.id, raw.detectedAtUtc, raw.symbol, raw.eventType]);
    if (seen.has(duplicateKey)) continue;
    seen.add(duplicateKey);
    const id = `EP-${digest(episodeKey(raw, rawIndex++))}`;
    if (!groups.has(id)) groups.set(id, []);
    groups.get(id).push(raw);
  }
  const episodes = [...groups.entries()].map(([id, raws]) => {
    raws.sort((a, b) => (a.detectedAtUtc || '').localeCompare(b.detectedAtUtc || '') || a.id.localeCompare(b.id));
    const first = raws[0], current = raws.at(-1), dataQuality = quality(current, now, feedFresh);
    const matchingPumpStates = pumpWatch.filter((state) => state.symbol === current.symbol && (!state.venue || state.venue === current.venue));
    const block = matchingPumpStates.find((state) => state.status === 'active_block'), expiry = current.expiresAtUtc;
    let status = 'research_only'; const statusReasons = [];
    if (block) { status = 'no_trade'; statusReasons.push(block.reason); }
    else if (current.invalidatedAtUtc) { status = 'invalidated'; statusReasons.push(current.invalidationReason || 'Причина закрытия не опубликована'); }
    else if (dataQuality.missingFields.length) { status = 'data_unavailable'; statusReasons.push('Недостаточно обязательных данных для исследовательского отображения'); }
    else if (expiry && Date.parse(expiry) <= now) { status = 'expired'; statusReasons.push('Окно актуальности, заданное источником, истекло'); }
    else if (!feedFresh || dataQuality.stale) { status = 'no_trade'; statusReasons.push('Данные устарели по политике отображения'); }
    else if (current.reviewVerifiedAtUtc && Date.parse(current.reviewVerifiedAtUtc) <= now && current.liquidityVerified) status = 'manual_review';
    if (status === 'research_only' && !current.liquidityVerified) statusReasons.push('Контекст стакана не предоставлен — нужна ручная проверка');
    const metricsVisible = dataQuality.status === 'verified' && ['research_only', 'manual_review', 'no_trade'].includes(status);
    return { id, symbol: current.symbol, venue: current.venue, canonicalInstrumentId: current.symbol && current.venue ? `${current.venue}:${current.symbol}` : null, identityComplete: Boolean(current.symbol && current.venue), status, statusReasons, eventTypes: [...new Set(raws.map((raw) => raw.eventType))], eventLabel: current.eventLabel, firstDetectedAtUtc: first.detectedAtUtc, lastAlertAtUtc: current.detectedAtUtc, timeframe: current.timeframe, rawAlertCount: raws.length, reportedRepeatCount: current.reportedRepeatCount, methodVersion: current.methodVersion, expiryAtUtc: expiry, expiryBasis: expiry ? 'source' : 'not_published', dataQuality, pumpWatch: block || matchingPumpStates.at(-1) || { status: 'unavailable', reason: 'Отсутствие записи не подтверждает CLEAR' }, features: metricsVisible ? current.features : {}, price: metricsVisible ? current.price : null, volume: metricsVisible ? current.volume : null, validationEligibility: current.validationEligibility, rawAlerts: raws.map((raw) => ({ ...raw, price: null, volume: null, features: {}, historicalValuesSuppressed: true })), timeline: raws.map((raw) => ({ timestampUtc: raw.detectedAtUtc, action: 'raw_alert_observed', rawAlertId: raw.id, sourceModule: raw.sourceModule })), interpretationBoundary: 'non_directional_research', limitations: ['Проекция текущего feed, не постоянный журнал всех событий.', 'Повторы, заявленные источником, не равны числу доступных первичных записей.', 'Версия интерфейса не подменяет версию детектора.'] };
  }).sort((a, b) => (b.lastAlertAtUtc || '').localeCompare(a.lastAlertAtUtc || ''));
  const missingRequiredFields = {};
  for (const episode of episodes) for (const field of episode.dataQuality.missingFields) missingRequiredFields[field] = (missingRequiredFields[field] || 0) + 1;
  const verifiedDataCount = episodes.filter((episode) => episode.dataQuality.status === 'verified').length;
  const archive = legacyArchive(feed);
  const nowObservations = currentObservations(feed);
  const positioningLayer = positioning(feed), smartMoney = smartMoneyLab(feed);
  const overallStatus = !feedFresh ? (archive.totalRecords ? 'legacy_only_stale' : 'unavailable') : verifiedDataCount ? (verifiedDataCount < episodes.length ? 'degraded' : 'healthy') : archive.totalRecords ? 'legacy_only' : 'unavailable';
  const health = { overallStatus, feedGeneratedAtUtc: generatedAtUtc, feedAgeMs, evaluatedAtUtc: new Date(now).toISOString(), feedFresh, missingRequiredFields, episodeCount: episodes.length, verifiedDataCount, legacyRecordCount: archive.totalRecords, legacySourceCounts: Object.fromEntries(archive.sources.map((source) => [source.id, source.count])), affectedModules: rawSources(feed).filter(([, rows]) => rows.length).map(([module]) => module), providers: [...new Set(episodes.flatMap((episode) => episode.rawAlerts.map((raw) => raw.provider)).filter(Boolean))], lastValidSourceTimestampUtc: episodes.filter((episode) => episode.dataQuality.status === 'verified').map((episode) => episode.dataQuality.sourceTimestampUtc).sort().at(-1) || null, lastError: null };
  const rose = arr(feed.rose?.tracks).map((track) => ({ symbol: canonical(track.symbol), timestampUtc: utc(track.first_ts), sourceModule: text(track.source), methodVersion: text(track.methodVersion), observationHorizons: ['6h', '24h'], dataQuality: 'unavailable', missingFields: ['methodVersion', 'sourceTimestampUtc', 'quality attestation'], completionStatus: track.final === true ? 'Завершено по флагу источника' : 'Не завершено по флагу источника', limitations: 'Направленные и результативные поля источника скрыты: контракт абсолютных high/low и качество не опубликованы.' }));
  return { adapterVersion: POLICY.version, generatedAtUtc, evaluatedAtUtc: new Date(now).toISOString(), systemStatus: overallStatus, interpretationMode: 'NON-DIRECTIONAL', executionMode: 'DISABLED', episodes, nowObservations, focusObservations: terminalFocus(nowObservations, now), acceptanceReview: acceptanceReview(feed), positioning: positioningLayer, smartMoneyLab: smartMoney, pumpWatch, pumpArchive: arr(feed.pump_watch).concat(arr(feed.pump_muted)).map((row) => ({ symbol: canonical(row.symbol), timestampUtc: utc(row.ts || row.added_ts), status: 'historical_only', reason: text(row.reason) || 'Состояние не опубликовано' })), validation: validation(feed), health, archive, rose, policy: POLICY, storage: { mode: 'read_only_feed_projection', persistentAuditAvailable: false, rawHistoryComplete: false } };
}

module.exports = { buildModel, normalize, quality, validation, legacyArchive, currentObservations, terminalFocus, acceptanceReview, positioning, smartMoneyLab, canonical, utc, POLICY, LEGACY_STATUS };

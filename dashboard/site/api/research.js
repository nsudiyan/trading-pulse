const feedHandler = require('./feed');
const { buildModel, POLICY } = require('../lib/model');

async function readFeed() {
  let status = 200;
  let payload;
  await feedHandler({}, {
    setHeader() {},
    status(code) { status = code; return this; },
    json(value) { payload = value; },
    send(value) { payload = JSON.parse(value); },
  });
  if (status !== 200 || !payload || typeof payload !== 'object') throw Error('feed_unavailable');
  return payload;
}

function routeModel(model, path) {
  const [kind, id, part] = String(path || 'overview').split('/').filter(Boolean);
  if (!kind || kind === 'overview') return model;
  if (kind === 'episodes') {
    if (!id) return { ...model.health, items: model.episodes };
    const episode = model.episodes.find((item) => item.id === id);
    if (!episode) return null;
    if (!part) return episode;
    if (part === 'raw-alerts') return episode.rawAlerts;
    if (part === 'timeline') return episode.timeline;
    if (part === 'data-quality') return episode.dataQuality;
    return null;
  }
  if (kind === 'data-health') return model.health;
  if (kind === 'archive') return model.archive;
  if (kind === 'positioning') return model.positioning;
  if (kind === 'pumpwatch') return { items: model.pumpWatch, archive: model.pumpArchive, statusSource: 'explicit_backend_states_only' };
  if (kind === 'rose-archive') return { items: model.rose, mode: 'retrospective_event_tracking' };
  if (kind === 'methodology') {
    const body = { adapterPolicy: POLICY, detectorVersions: [...new Set(model.episodes.map((item) => item.methodVersion).filter(Boolean))], storage: model.storage };
    return id === 'versions' ? { adapterVersion: model.adapterVersion, detectorVersions: body.detectorVersions } : body;
  }
  if (kind === 'validation') {
    if (id === 'protocol') return { ...model.validation, results: null };
    if (id === 'results') return { available: model.validation.resultsAvailable, results: model.validation.results, conclusion: model.validation.conclusion };
    if (id === 'exclusions') return { available: model.validation.exclusionsAvailable, items: model.validation.exclusions };
    return model.validation;
  }
  return null;
}

function createHandler(getFeed = readFeed, clock = Date.now) {
  return async (req, res) => {
    res.setHeader('Cache-Control', 'no-store, max-age=0');
    if (req.method && req.method !== 'GET') {
      res.setHeader('Allow', 'GET');
      return res.status(405).json({ error: 'read_only_api' });
    }
    try {
      const model = buildModel(await getFeed(), clock());
      const body = routeModel(model, req.query?.path || 'overview');
      return body === null ? res.status(404).json({ error: 'not_found' }) : res.status(200).json(body);
    } catch (_) {
      const model = buildModel({}, clock());
      model.health.lastError = 'Источник feed недоступен; актуальность не подтверждена.';
      return res.status(503).json({ ...model, error: 'feed_unavailable' });
    }
  };
}

module.exports = createHandler();
module.exports.createHandler = createHandler;
module.exports.routeModel = routeModel;

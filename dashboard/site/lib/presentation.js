const ACTIVE_STATUSES = new Set(['research_only', 'manual_review']);

function radarEpisodes(model) {
  return (model.episodes || []).filter((episode) => ACTIVE_STATUSES.has(episode.status));
}

function displayMetrics(episode) {
  return episode?.dataQuality?.status === 'verified' && Object.keys(episode.features || {}).length > 0;
}

function canRequestManualReview(episode) {
  return episode?.status === 'research_only'
    && episode?.dataQuality?.status === 'verified'
    && episode?.pumpWatch?.status !== 'active_block'
    && episode?.dataQuality?.liquidityContext === 'verified';
}

module.exports = { radarEpisodes, displayMetrics, canRequestManualReview };

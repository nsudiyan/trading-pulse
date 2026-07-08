// Vercel serverless: свежий фид МИМО Fastly-кэша raw.githubusercontent (max-age=300,
// query из ключа кэша вырезается → cache-buster не пробивает; замер: raw отставал до ~200с).
// Тянем GitHub contents API сервер-сайд (github CC = max-age=60, не Fastly-path-кэш).
// CommonJS намеренно: сайт без package.json → Vercel трактует .js как CJS; export default сломался бы.
// fetch — глобальный в Node 18+ (рантайм Vercel).
//
// Два режима по наличию GITHUB_FEED_TOKEN (в браузер НЕ течёт, только env):
//   ТОКЕН   → Authorization + no-store: лимит 5000/час, свежесть максимальная (~15-40с сквозь всю цепочку).
//   БЕЗ     → s-maxage=60: Vercel Edge держит ответ 60с, github дёргается ≤60/час — влезаем в unauth-лимит
//             (иначе поллинг 15с = 240/час → 429). Свежесть ~60-75с (всё равно в 4× лучше raw-CDN).
const FEED_URL =
  "https://api.github.com/repos/nsudiyan/mirofish-state/contents/trading_feed.json";

module.exports = async function handler(req, res) {
  const token = process.env.GITHUB_FEED_TOKEN; // узкий fine-grained (Contents:read на mirofish-state)
  const headers = { Accept: "application/vnd.github.raw", "User-Agent": "pulse-dashboard" };
  if (token) headers.Authorization = `Bearer ${token}`;
  try {
    const r = await fetch(FEED_URL, { headers });
    if (!r.ok) {
      // 401/403/429 → фронт падает на свой fallback (raw), дашборд не умирает
      res.status(502).json({ error: `github ${r.status}`, tokened: Boolean(token) });
      return;
    }
    const body = await r.text();
    res.setHeader(
      "Cache-Control",
      token ? "no-store, max-age=0"
            : "public, max-age=0, s-maxage=60, stale-while-revalidate=30"
    );
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    res.status(200).send(body);
  } catch (e) {
    res.status(502).json({ error: String(e) });
  }
};

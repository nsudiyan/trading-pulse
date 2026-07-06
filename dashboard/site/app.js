/* Пульс — трейдинг-дашборд.
   Фид: GitHub raw (пуш локального демона, задержка ~1-2 мин) + фоллбэк на
   задеплоенную копию. Живые цены: Bybit v5 public WebSocket напрямую из
   браузера — отработка активных сигналов тикает в реальном времени. */
"use strict";

const FEED_RAW = "https://raw.githubusercontent.com/nsudiyan/mirofish-state/main/trading_feed.json";
const FEED_POLL_MS = 15_000;
const BYBIT_REST = "https://api.bybit.com/v5/market/kline";
const BYBIT_WS = "wss://stream.bybit.com/v5/public/linear";
const LIVE_CARDS_MAX = 24;
const RADAR_CARD_WINDOW_H = 6;   // radar-хитов много — в карточки только свежие
const KLINE_REFRESH_MS = 5 * 60_000;

const $ = (id) => document.getElementById(id);
const MSK = new Intl.DateTimeFormat("ru-RU", {
  timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit",
  hour: "2-digit", minute: "2-digit",
});
const fmtMsk = (iso) => iso ? MSK.format(new Date(iso)) : "—";
const fmtPct = (v, digits = 2) =>
  v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
const cls = (v) => (v == null ? "flat" : v > 0 ? "pos" : v < 0 ? "neg" : "flat");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function agoStr(iso) {
  const m = Math.max(0, (Date.now() - new Date(iso)) / 60000);
  if (m < 1) return "только что";
  if (m < 60) return `${Math.floor(m)} мин назад`;
  if (m < 48 * 60) return `${Math.floor(m / 60)} ч назад`;
  return `${Math.floor(m / 1440)} дн назад`;
}

const SRC_RU = { tg_channel: "ТГК", screener: "скринер", storm: "шторм", radar: "радар", alert: "алерт" };
const OUR_SOURCES = new Set(["storm", "radar", "screener", "alert"]); // наши системы (не чужие ТГК)
const FRESH_MIN = 15; // сигнал моложе 15 мин = метка NEW
const state = {
  feed: null,
  liveCards: new Map(),   // id → {sig, basis, peak, dd, sparkPts, el}
  klineCache: new Map(),  // symbol|tsMin → {bars, fetchedAt}
  ws: null, wsSymbols: new Set(), wsAlive: false,
  histFilter: "all", histShown: 40, roseShown: 40, roseStarOnly: false,
  liveFilter: "ours",     // по умолчанию — наши сигналы
  starred: new Set(JSON.parse(localStorage.getItem("pulse_starred") || "[]")),
  lastPx: new Map(),      // symbol → живая цена с WS (для связок и не только)
  comboPeak: new Map(),   // symbol → максимальный живой % от базиса поста за сессию
};

/* ── звёздочки: пометка «просмотрел/просмотрю» на МОНЕТЕ, живёт в браузере ── */
function starHtml(sym) {
  const on = state.starred.has(sym);
  return `<span class="star${on ? " on" : ""}" data-sym="${esc(sym)}" title="отметить монету">${on ? "★" : "☆"}</span>`;
}
function toggleStar(sym) {
  if (state.starred.has(sym)) state.starred.delete(sym);
  else state.starred.add(sym);
  localStorage.setItem("pulse_starred", JSON.stringify([...state.starred]));
  const on = state.starred.has(sym);
  document.querySelectorAll(`.star[data-sym="${CSS.escape(sym)}"]`).forEach((el) => {
    el.classList.toggle("on", on);
    el.textContent = on ? "★" : "☆";
  });
  // активные ★-фильтры должны сразу отразить изменение
  if (!state.feed) return;
  if (state.liveFilter === "starred") renderLive(state.feed);
  if (state.histFilter === "starred") renderHistory(state.feed);
  if (state.roseStarOnly) renderRose(state.feed);
}
document.addEventListener("click", (ev) => {
  const st = ev.target.closest(".star");
  if (st) { ev.preventDefault(); toggleStar(st.dataset.sym); }
});

/* ── фид ── */
async function fetchFeed() {
  const bust = Math.floor(Date.now() / 15_000); // бастер по 15с: каждый poll мимо CDN-кэша
  for (const url of [`${FEED_RAW}?t=${bust}`, `feed.json?t=${bust}`]) {
    try {
      const r = await fetch(url, { cache: "no-store" });
      if (r.ok) return await r.json();
    } catch (_) { /* следующий источник */ }
  }
  return null;
}

function feedStatus(feed) {
  const el = $("feed-status");
  if (!feed) { el.innerHTML = '<span class="dot err"></span>фид недоступен'; return; }
  const ageMin = (Date.now() - new Date(feed.generated_at)) / 60000;
  const dot = ageMin < 5 ? "ok" : ageMin < 30 ? "stale" : "err";
  el.innerHTML = `<span class="dot ${dot}"></span>фид ${agoStr(feed.generated_at)}`;
  $("gen-at").textContent = ` Фид собран: ${fmtMsk(feed.generated_at)} МСК.`;
}

/* ── Bybit klines: базис и спарклайн сигнала ── */
async function fetchBars(symbol, fromMs) {
  const key = `${symbol}|${Math.floor(fromMs / 60000)}`;
  const hit = state.klineCache.get(key);
  if (hit && Date.now() - hit.fetchedAt < KLINE_REFRESH_MS) return hit.bars;
  try {
    const url = `${BYBIT_REST}?category=linear&symbol=${symbol}&interval=5&start=${fromMs}&end=${Date.now()}&limit=1000`;
    const r = await fetch(url);
    const j = await r.json();
    let bars = (j?.result?.list || []).map((x) => ({
      t: +x[0], o: +x[1], h: +x[2], l: +x[3], c: +x[4],
    })).reverse();
    // только полные бары строго после сигнала (без look-ahead)
    const barMs = 5 * 60000;
    const firstFull = (Math.floor(fromMs / barMs) + 1) * barMs;
    bars = bars.filter((b) => b.t >= firstFull && b.t + barMs <= Date.now());
    state.klineCache.set(key, { bars, fetchedAt: Date.now() });
    return bars;
  } catch (_) { return hit ? hit.bars : []; }
}

function sigPct(sig, basis, price) {
  const raw = (price - basis) / basis * 100;
  return sig.direction === "short" ? -raw : raw; // без направления = дельта лонга
}

/* ── live-карточки ── */
function pickLiveSignals(feed) {
  const now = Date.now();
  const seen = new Set();
  const out = [];
  for (const s of feed.live_signals || []) {
    const age = (now - new Date(s.ts_utc)) / 3600_000;
    // радар-мажоры не рисуем: ход ≥5% у 2/19, vol_radar их и не доставляет —
    // карточки только красили ленту рыночным дрейфом (2026-07-06)
    if (s.major_radar) continue;
    // radar-хиты старше 6ч гасим, НО сигнал с наклоном — редкость, живёт
    if (s.source === "radar" && age > RADAR_CARD_WINDOW_H && !s.bias) continue;
    const dedup = `${s.source}:${s.symbol}:${s.ts_utc.slice(0, 16)}`;
    if (seen.has(dedup)) continue;
    seen.add(dedup);
    out.push(s);
  }
  return out; // LIVE_CARDS_MAX применяется ПОСЛЕ фильтра в renderLive
}

function liveFilterBar(signals) {
  const srcs = ["ours", "all", "starred", "bias", ...new Set(signals.map((s) => s.source))];
  $("live-filters").innerHTML = srcs.map((s) =>
    `<button class="fbtn ${state.liveFilter === s ? "on" : ""}" data-src="${s}">
       ${s === "ours" ? "наши" : s === "all" ? "все" : s === "starred" ? "★ отмеченные" : s === "bias" ? "⬆⬇ наклон" : SRC_RU[s] || s}</button>`).join("");
  $("live-filters").querySelectorAll(".fbtn").forEach((b) =>
    b.onclick = () => { state.liveFilter = b.dataset.src; renderLive(state.feed); });
}

function renderBiasAcc(feed) {
  const acc = feed.bias_accuracy || {};
  const el = $("bias-acc");
  if (el) {
    const parts = Object.entries(acc)
      .filter(([, a]) => a.n_graded)
      .map(([v, a]) => `${v}: <b>${a.accuracy_pct}%</b> (n=${a.n_graded}${a.low_n ? " ⚠" : ""})`);
    el.innerHTML = parts.length
      ? `наклон, форвард-точность — ${parts.join(" · ")}`
      : "наклон v2 (radar→⬆, Rose→контрарно): форвард копится…";
  }
  // счётчик в шапке: угадано/всего по всем версиям (просьба брата 2026-07-05)
  const hits = Object.values(acc).reduce((s, a) => s + (a.hits || 0), 0);
  const graded = Object.values(acc).reduce((s, a) => s + (a.n_graded || 0), 0);
  const hs = $("bias-score");
  if (hs) {
    const pct = graded ? Math.round(hits / graded * 100) : null;
    hs.innerHTML = `⬆⬇ <b>${hits}/${graded}</b>` + (pct != null ? ` <span style="color:var(--${pct >= 60 ? "up" : pct >= 45 ? "warn" : "down"})">${pct}%</span>` : "");
    hs.title = `наклон стороны: угадано ${hits} из ${graded} экзаменованных движений (форвард, все версии; ±2% флэт-зона не считается)`;
  }
}

async function renderLive(feed) {
  let sigs = pickLiveSignals(feed);
  renderBiasAcc(feed);
  liveFilterBar(sigs);
  if (state.liveFilter === "ours") sigs = sigs.filter((s) => OUR_SOURCES.has(s.source));
  else if (state.liveFilter === "starred") sigs = sigs.filter((s) => state.starred.has(s.symbol));
  else if (state.liveFilter === "bias") sigs = sigs.filter((s) => s.bias && s.bias.side !== "flat");
  else if (state.liveFilter !== "all") sigs = sigs.filter((s) => s.source === state.liveFilter);
  sigs = sigs.slice(0, LIVE_CARDS_MAX);

  const box = $("live-cards");
  const wantIds = new Set(sigs.map((s) => s.id));
  if (!sigs.length) {
    box.innerHTML = '<div class="empty">Живых сигналов в окне 48ч нет.</div>';
    state.liveCards.clear();
    return;
  }
  // реконсиляция: пересборка только при изменении набора
  const haveIds = new Set(state.liveCards.keys());
  const same = wantIds.size === haveIds.size && [...wantIds].every((i) => haveIds.has(i));
  if (!same) {
    box.innerHTML = "";
    state.liveCards.clear();
    for (const sig of sigs) {
      const el = document.createElement("div");
      const isFresh = Date.now() - new Date(sig.ts_utc) < FRESH_MIN * 60_000;
      el.className = isFresh ? "card fresh" : "card";
      el.innerHTML = cardHtml(sig, isFresh);
      box.appendChild(el);
      state.liveCards.set(sig.id, { sig, el, basis: sig.entry || null, peak: null, dd: null, sparkPts: [] });
    }
    await Promise.all([...state.liveCards.values()].map(initCardBars));
    wsEnsure();
    renderEntry();   // кандидаты сразу, не ждать 5с-интервала
  }
}

function biasTag(sig) {
  const b = sig.bias;
  if (!b || b.side === "flat") return "";
  const arrow = b.side === "up" ? "⬆" : "⬇";
  const why = (b.why || []).join(" · ");
  return `<span class="biastag ${b.side}${b.strong ? " strong" : ""}"
    title="наклон структуры ${b.v} (НЕ прогноз с доказанным эджем — точность меряется форвардом): ${esc(why)}">${arrow} наклон</span>`;
}

function riskTag(sig) {
  // шторм-шорт = статистически самая сливная категория (форвард 04-06.07:
  // чётких 2/33, сливов 30%) — маркируем, не скрываем (просьба брата)
  if (sig.source === "storm" && sig.direction === "short")
    return `<span class="risktag" title="форвард n=33: пробой вниз доигрывается лишь у 6%, 30% выкупаются против — самая сливная категория">⚠ вниз-пробой: 6% чётких</span>`;
  return "";
}

/* Погодная плашка BTC на скринер-карточках (разрез 06.07, n=907, честное окно).
   Метрика = ровно та, что в исследовании: % BTC за 24ч (терцильные пороги
   −1.9% / +0.3%). Live-значение из WS-тикера. Визуал-форвард, гейтов нет. */
function weatherTag(sig) {
  if (sig.source !== "screener" || state.btcRet24 == null) return "";
  const b = state.btcRet24;
  const d = (sig.direction || "").toLowerCase();
  const isShort = d === "short" || d === "шорт";
  const isLong = d === "long" || d === "лонг";
  const btc = `BTC ${fmtPct(b, 1)}/24ч`;
  if (isShort && b > 0.3)
    return `<span class="risktag" title="разрез n=907: шорты при растущем BTC — win 18%, средний R −0.58">⚠ против ветра · ${btc}</span>`;
  if (isLong && b < -1.9)
    return `<span class="risktag" title="разрез n=907: лонги при падающем BTC — win 31%, средний R −0.26">⚠ против ветра · ${btc}</span>`;
  if (isShort && b < -1.9)
    return `<span class="windtag" title="разрез n=907: шорты при падающем BTC — win 45%, средний R +0.10">по ветру · ${btc}</span>`;
  if (isLong && b > 0.3)
    return `<span class="windtag" title="разрез n=907: лонги при растущем BTC — win 46%, средний R +0.15">по ветру · ${btc}</span>`;
  return "";
}

function comboTag(sym) {
  const c = (state.feed?.combos || []).find((x) => x.symbol === sym);
  if (!c) return "";
  return `<span class="badge combo" title="связка: пробуждение ×${c.awake_ratio} → Rose-пост (${agoStr(c.post_ts)})">⚡</span>`;
}

/* ⚡ Связки «пробуждение → Rose-пост → структура жива» — рендер подблока entry.
   Живая смерть на каждом WS-тике (просьба брата 06.07 «следи каждую секунду»):
   провал ниже базиса поста >3% (для short зеркально) ИЛИ живой ретрейс ≥80%
   пика при пике ≥8% → карточка исчезает немедленно. Сервер дополнительно
   выкидывает раздачу и финально-отдавшие треки. */
function comboAlive(c) {
  // «вход сейчас»: ход по посту уже случился (пик ≥8%) = отработана; провал −3% = мертва
  const px = state.lastPx.get(c.symbol);
  if (px == null || !c.basis) {
    return { alive: (c.peak24_pct || 0) < 8, live: null };  // цены ещё нет — судим по фиду
  }
  let live = (px - c.basis) / c.basis * 100;
  if (c.direction === "short") live = -live;
  const peakRef = Math.max(c.peak24_pct || 0, state.comboPeak.get(c.symbol) || 0, live);
  state.comboPeak.set(c.symbol, peakRef);
  if (peakRef >= 8) return { alive: false, live };
  if (live < -3) return { alive: false, live };
  return { alive: true, live };
}

function renderCombos() {
  const box = $("combo-cards");
  if (!box || !state.feed) return;
  const rows = [];
  for (const c of state.feed.combos || []) {
    const { alive, live } = comboAlive(c);
    if (!alive) continue;
    rows.push({ c, live });
  }
  if (!rows.length) {
    box.innerHTML = '<div class="empty">Живых связок нет — отработавшие и провалившиеся сняты.</div>';
    return;
  }
  box.innerHTML = rows.map(({ c, live }) => `
    <div class="card entry combo">
      <div class="row1">
        ${starHtml(c.symbol)}
        <span class="sym">${esc(c.symbol)}</span>
        <span class="badge combo">⚡</span>
        <span class="when">пост ${agoStr(c.post_ts)}</span>
      </div>
      <div class="big ${cls(live ?? c.peak24_pct)}">${fmtPct(live ?? c.peak24_pct, 1)} <span style="font-size:11px;color:var(--muted)">${live != null ? "live от поста" : "пик 24ч"}</span></div>
      <div class="meta">🌅 ×${c.awake_ratio} за ${Math.round((new Date(c.post_ts) - new Date(c.awake_ts)) / 3600_000)}ч до поста · ${esc(c.channel || "rose")} ${esc(c.direction || "")} · пик 24ч ${fmtPct(c.peak24_pct, 1)}</div>
    </div>`).join("");
}
let _comboLast = 0;
function comboTickRender() {           // по WS-тику, не чаще раза в секунду
  const now = Date.now();
  if (now - _comboLast < 1000) return;
  _comboLast = now;
  renderCombos();
}

function pumpTag(sym) {
  const pw = (state.feed?.pump_watch || []).find((p) => p.symbol === sym);
  if (!pw) return "";
  const t = pw.broke ? "🔻 слом плато" : pw.dist ? "🌊 распределение" : "🌋 пост-памп";
  return `<span class="pumptag" title="под pump-надзором (${esc(pw.kind || "")}): лонги тут — против шторма">${t}</span>`;
}

function cardHtml(sig, isFresh = false) {
  const dirCls = sig.direction || "none";
  const dirTxt = sig.direction === "long" ? "▲ LONG" : sig.direction === "short" ? "▼ SHORT" : "Δ";
  const chan = sig.channel || (sig.channels || []).join(", ");
  const extra = [sig.setup, chan, sig.note].filter(Boolean).join(" · ");
  return `
    <div class="row1">
      ${starHtml(sig.symbol)}
      <span class="sym">${esc(sig.symbol)}</span>
      <span class="dir ${dirCls}">${dirTxt}</span>
      ${isFresh ? '<span class="newtag">NEW</span>' : ""}
      <span class="when" title="${fmtMsk(sig.ts_utc)} МСК">${agoStr(sig.ts_utc)}</span>
    </div>
    <div class="row1">
      <span class="badge ${sig.source}">${SRC_RU[sig.source] || sig.source}</span>
      ${sig.emits > 1 ? `<span class="badge" title="повторных алертов">×${sig.emits}</span>` : ""}
      ${biasTag(sig)}
      ${riskTag(sig)}
      ${weatherTag(sig)}
      ${pumpTag(sig.symbol)}
      ${comboTag(sig.symbol)}
      ${extra ? `<span class="src-note" title="${esc(extra)}">${esc(extra)}</span>` : ""}
    </div>
    <div class="big" data-role="pct">…</div>
    <div class="meta" data-role="meta"></div>
    <svg class="spark" data-role="spark" viewBox="0 0 240 40" preserveAspectRatio="none"></svg>`;
}

async function initCardBars(card) {
  const ts = new Date(card.sig.ts_utc).getTime();
  const bars = await fetchBars(card.sig.symbol, ts);
  if (!bars.length) {
    card.el.querySelector('[data-role="pct"]').textContent = "нет данных";
    card.el.querySelector('[data-role="pct"]').style.fontSize = "14px";
    return;
  }
  if (!card.basis) card.basis = bars[0].c; // вход = close 1-го полного бара
  const s = card.sig;
  const fav = (b) => s.direction === "short"
    ? (card.basis - b.l) / card.basis * 100 : (b.h - card.basis) / card.basis * 100;
  const adv = (b) => s.direction === "short"
    ? (card.basis - b.h) / card.basis * 100 : (b.l - card.basis) / card.basis * 100;
  // пик/просадка — строго после бара входа (его high/low могли быть до входа)
  const post = bars.slice(1);
  card.peak = post.length ? Math.max(...post.map(fav)) : 0;
  card.dd = post.length ? Math.min(...post.map(adv)) : 0;
  card.sparkPts = bars.map((b) => sigPct(s, card.basis, b.c));
  updateCard(card, sigPct(s, card.basis, bars[bars.length - 1].c));
}

function updateCard(card, livePct) {
  if (card.basis == null || livePct == null) return;
  card.lastPct = livePct;   // для секции «Вход имеет смысл сейчас»
  card.peak = card.peak == null ? livePct : Math.max(card.peak, livePct);
  card.dd = card.dd == null ? Math.min(0, livePct) : Math.min(card.dd, livePct);
  const pctEl = card.el.querySelector('[data-role="pct"]');
  pctEl.textContent = fmtPct(livePct);
  pctEl.className = `big ${livePct > 0 ? "pos" : livePct < 0 ? "neg" : ""}`;
  card.el.querySelector('[data-role="meta"]').innerHTML =
    `пик <b>${fmtPct(card.peak, 1)}</b> · просадка <b>${fmtPct(card.dd, 1)}</b>`;
  drawSpark(card, livePct);
}

function drawSpark(card, livePct) {
  const svg = card.el.querySelector('[data-role="spark"]');
  const pts = [...card.sparkPts, livePct];
  if (pts.length < 2) return;
  const W = 240, H = 40, P = 3;
  const min = Math.min(...pts, 0), max = Math.max(...pts, 0);
  const span = max - min || 1;
  const x = (i) => P + (i / (pts.length - 1)) * (W - 2 * P);
  const y = (v) => H - P - ((v - min) / span) * (H - 2 * P);
  const d = pts.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const zero = y(0);
  const last = pts[pts.length - 1];
  svg.innerHTML = `
    <line x1="0" x2="${W}" y1="${zero}" y2="${zero}" stroke="var(--grid)" stroke-width="1"/>
    <path d="${d}" fill="none" stroke="${last >= 0 ? "var(--up)" : "var(--down)"}" stroke-width="1.6"/>
    <circle cx="${x(pts.length - 1)}" cy="${y(last)}" r="2.4" fill="${last >= 0 ? "var(--up)" : "var(--down)"}"/>`;
}

/* ── Bybit WebSocket ── */
function wsEnsure() {
  const wanted = new Set(["BTCUSDT", "ETHUSDT"]);
  for (const { sig } of state.liveCards.values()) wanted.add(sig.symbol);
  for (const c of state.feed?.combos || []) wanted.add(c.symbol);
  const changed = wanted.size !== state.wsSymbols.size || [...wanted].some((s) => !state.wsSymbols.has(s));
  if (state.ws && state.ws.readyState === 1 && !changed) return;
  state.wsSymbols = wanted;
  if (state.ws) try { state.ws.close(); } catch (_) {}
  const ws = new WebSocket(BYBIT_WS);
  state.ws = ws;
  ws.onopen = () => {
    state.wsAlive = true;
    $("ws-status").innerHTML = '<span class="dot ok"></span>live-цены';
    const args = [...wanted].map((s) => `tickers.${s}`);
    for (let i = 0; i < args.length; i += 10)
      ws.send(JSON.stringify({ op: "subscribe", args: args.slice(i, i + 10) }));
    ws._ping = setInterval(() => { try { ws.send('{"op":"ping"}'); } catch (_) {} }, 20_000);
  };
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
    if (!m.topic || !m.topic.startsWith("tickers.") || !m.data) return;
    const sym = m.topic.slice(8);
    const last = parseFloat(m.data.lastPrice);
    if (!isFinite(last)) return; // дельта без lastPrice
    state.lastPx.set(sym, last);
    if ((state.feed?.combos || []).some((c) => c.symbol === sym)) comboTickRender();
    if (sym === "BTCUSDT") headerTick("btc-tick", "BTC", last, m.data.price24hPcnt);
    if (sym === "ETHUSDT") headerTick("eth-tick", "ETH", last, m.data.price24hPcnt);
    for (const card of state.liveCards.values())
      if (card.sig.symbol === sym && card.basis != null)
        updateCard(card, sigPct(card.sig, card.basis, last));
  };
  ws.onclose = () => {
    clearInterval(ws._ping);
    if (state.ws !== ws) return;
    state.wsAlive = false;
    $("ws-status").innerHTML = '<span class="dot stale"></span>переподключение…';
    setTimeout(() => { if (state.ws === ws) { state.ws = null; wsEnsure(); } }, 3000);
  };
  ws.onerror = () => { try { ws.close(); } catch (_) {} };
}

const _hdrPct = {};
function headerTick(id, name, last, pcnt) {
  const p = parseFloat(pcnt);
  if (isFinite(p)) _hdrPct[id] = p * 100;
  if (id === "btc-tick" && isFinite(p)) {
    const was = state.btcRet24;
    state.btcRet24 = p * 100;   // погода для скринер-плашек
    if (was == null && state.feed) renderLive(state.feed);  // первый тик — дорисовать плашки
  }
  const chg = _hdrPct[id];
  $(id).innerHTML = `${name} <b>${last.toLocaleString("en-US", { maximumFractionDigits: last > 1000 ? 0 : 2 })}</b>` +
    (chg != null ? ` <span style="color:var(--${chg >= 0 ? "up" : "down"})">${fmtPct(chg, 1)}</span>` : "");
}

/* ── общие ячейки трека (методика 6ч/24ч) ── */
function dirCell(d) {
  return d === "long" ? '<span class="pos">▲ long</span>'
    : d === "short" ? '<span class="neg">▼ short</span>' : '<span class="flat">Δ</span>';
}
function trackStatus(t) {
  if (t.status === "no_data") return '<span class="flat">нет данных</span>';
  return t.final ? "🏁 итог" : "⏳ идёт";
}
// Доминанта суток: когда 24ч закрыты, подсвечиваем ЧТО было больше —
// пик или просадка (стороной не торгуем, важен сам шторм).
function domHl(t) {
  const out = { peak: "", dd: "" };
  if (!t.final || t.peak24_pct == null || t.dd24_pct == null) return out;
  const peak = t.peak24_pct, pain = Math.abs(t.dd24_pct);
  if (peak > pain) out.peak = " hl-pos";
  else if (pain > peak) out.dd = " hl-neg";
  return out;
}

/* ── «Вход имеет смысл сейчас»: механический шорт-лист по правилам форварда.
   Каждое правило — из проверенного разреза, НЕ интуиция:
   • радар-альт с наклоном ⬆ v3 (86% ходунов вверх, n=35; мажоры уже отсеяны)
   • свежесть ≤3ч (медиана времени до пика 1.2–1.7ч — позже вход догоняющий)
   • цена не убежала: −1.5%…+2.5% от алерта; просадка с алерта ≥ −2% (не пила)
   • пик ещё не отработан (≤+3.5%) и монета не под pump-раздачей
   • 🌅-пробуждения — отдельные правила: окно 30ч, коридор −3%…+5%
   Пересчёт каждые 5с из живых WS-данных карточек. ═ НЕ СИГНАЛ ГАРАНТИИ ═
   ВАЖНО: правила продублированы серверно в dashboard_feed.log_entry_candidates
   (форензика outcomes/entry_candidates.csv) — менять СИНХРОННО. */
function entryReason(card, isAwakening) {
  const bits = [isAwakening ? "🌅 пробуждение (окно 12–35ч)" : "радар-альт ⬆ (86% ходунов вверх)"];
  bits.push("не убежала", `просадка ${fmtPct(card.dd, 1)}`);
  return bits.join(" · ");
}

function renderEntry() {
  const box = $("entry-cards");
  if (!box || !state.feed) return;
  const pumps = new Map((state.feed.pump_watch || []).map((p) => [p.symbol, p]));
  const out = [];
  for (const card of state.liveCards.values()) {
    const s = card.sig;
    if (s.source !== "radar" || !s.bias || s.bias.side !== "up") continue;
    if (card.lastPct == null || card.dd == null || card.peak == null) continue;
    const p = pumps.get(s.symbol);
    if (p && (p.dist || p.broke)) continue;               // раздача/слом — не вход
    const ageH = (Date.now() - new Date(s.ts_utc)) / 3600_000;
    const isAwk = (s.vol_ratio || 0) >= 15;
    const ok = isAwk
      ? (ageH <= 30 && card.lastPct >= -3 && card.lastPct <= 5 && card.dd >= -4)
      : (ageH <= 3 && card.lastPct >= -1.5 && card.lastPct <= 2.5
         && card.dd >= -2 && card.peak <= 3.5);
    if (ok) out.push({ card, isAwk, ageH });
  }
  out.sort((a, b) => a.ageH - b.ageH);                    // свежие первыми
  // рыночная волна: ≥5 кандидатов из одного 5-мин скана = не пробуждения,
  // а общий BTC-движ (radar_resolved: каскадные good 15% vs 23% у одиночных) — скрыть
  const buckets = new Map();
  for (const it of out) {
    const b = Math.round(new Date(it.card.sig.ts_utc) / 300_000);
    (buckets.get(b) || buckets.set(b, []).get(b)).push(it);
  }
  let waveNote = "";
  for (const [, items] of buckets) {
    if (items.filter((x) => !x.isAwk).length >= 5) {
      const hidden = items.filter((x) => !x.isAwk);
      hidden.forEach((x) => { x.hide = true; });
      waveNote += `<div class="wave-note">⚠ рыночная волна ${agoStr(hidden[0].card.sig.ts_utc)}: ${hidden.length} монет хитанули одним сканом — это BTC-движ, не пробуждения (каскадные отрабатывают в 1.5 раза хуже: good 15% vs 23%) — скрыты</div>`;
    }
  }
  const shown = out.filter((x) => !x.hide);
  if (!shown.length && !waveNote) {
    box.innerHTML = '<div class="empty">Кандидатов сейчас нет — правила строгие.</div>';
    return;
  }
  box.innerHTML = waveNote + shown.map(({ card, isAwk }) => {
    const s = card.sig;
    return `<div class="card entry${isAwk ? " awk" : ""}">
      <div class="row1">
        ${starHtml(s.symbol)}
        <span class="sym">${esc(s.symbol)}</span>
        ${isAwk ? '<span class="badge" style="color:var(--warn)">🌅</span>' : ""}
        <span class="when">${agoStr(s.ts_utc)}</span>
      </div>
      <div class="big ${card.lastPct > 0 ? "pos" : card.lastPct < 0 ? "neg" : ""}">${fmtPct(card.lastPct)}</div>
      <div class="meta">${esc(entryReason(card, isAwk))}</div>
    </div>`;
  }).join("");
}
setInterval(renderEntry, 5000);

/* ── Pump-надзор: эпизоды + заглушенные лонги ── */
function renderPump(feed) {
  const eps = feed.pump_watch || [];
  const muted = feed.pump_muted || [];
  const broke = eps.filter((e) => e.broke).length;
  $("pump-tiles").innerHTML = `
    <div class="tile"><div class="v">${eps.length}</div><div class="l">эпизодов под надзором</div></div>
    <div class="tile"><div class="v">${broke}</div><div class="l">со сломом плато 🔻</div></div>
    <div class="tile"><div class="v">${muted.length}</div><div class="l">заглушено лонгов скринера</div></div>`;
  const stage = (e) => e.broke ? "🔻 слом плато" : e.dist ? "🌊 распределение" : "🌋 вертикаль";
  $("pump-table").querySelector("tbody").innerHTML = eps.length ? eps.map((e) => `
    <tr>
      <td class="sym lft">${starHtml(e.symbol)} ${esc(e.symbol)}</td>
      <td class="lft">${fmtMsk(e.detected_ts)}</td>
      <td class="lft">${e.kind === "squeeze" ? "шорт-сквиз (OI↓ на росте)" : e.kind === "trend" ? "тренд (OI↑)" : "смешанный"}</td>
      <td class="lft">${stage(e)}</td>
      <td><span class="pos">${e.rise_pct != null ? "+" + e.rise_pct + "%" : "—"}</span></td>
      <td>${e.peak ?? "—"}</td>
      <td>${e.plateau_low ?? "—"}</td>
    </tr>`).join("")
    : '<tr><td colspan="7" class="empty">Свежих вертикалей на рынке нет — надзор пуст, гейт вооружён.</td></tr>';
  $("pump-muted-table").querySelector("tbody").innerHTML = muted.length ? muted.map((m) => `
    <tr>
      <td class="lft">${fmtMsk(m.ts + ":00Z")}</td>
      <td class="sym lft">${starHtml(m.symbol)} ${esc(m.symbol)}</td>
      <td class="lft">${esc(m.setup)}</td>
      <td>${esc(m.score ?? "—")}</td>
      <td class="lft">${esc(m.reason || "")}</td>
    </tr>`).join("")
    : '<tr><td colspan="5" class="empty">Гейт ещё ничего не глушил.</td></tr>';
}

/* ── Rose: KPI + накапливающаяся таблица треков ── */
function renderRose(feed) {
  const r = feed.rose || {};
  const s = r.summary || {};
  const acc = r.bot_accuracy || {};
  const accStr = Object.entries(acc)
    .map(([ch, a]) => `${ch}: ${(a.accuracy * 100).toFixed(0)}% (n=${a.n})`).join(" · ");
  $("rose-tiles").innerHTML = `
    <div class="tile"><div class="v">${s.n_tracks ?? "—"}</div><div class="l">треков сигналов (30 дн)</div></div>
    <div class="tile"><div class="v">${s.hit24_pct != null ? s.hit24_pct + "%" : "—"}</div><div class="l">итог суток в плюс${s.low_n ? " ⚠ мало данных" : ""}</div></div>
    <div class="tile"><div class="v pos">${s.avg_peak24_pct != null ? "+" + s.avg_peak24_pct + "%" : "—"}</div><div class="l">средний пик за сутки</div></div>
    <div class="tile"><div class="v neg">${s.avg_dd24_pct != null ? s.avg_dd24_pct + "%" : "—"}</div><div class="l">средняя просадка за сутки</div></div>
    <div class="tile"><div class="v">${s.avg_confirms ?? "—"}</div><div class="l">среднее подтверждений</div></div>
    ${accStr ? `<div class="tile"><div class="v" style="font-size:14px;line-height:1.6">${esc(accStr)}</div><div class="l">точность подтверждений в боте</div></div>` : ""}`;
  $("rose-filters").innerHTML =
    `<button class="fbtn ${state.roseStarOnly ? "on" : ""}" id="rose-star-btn">★ отмеченные</button>`;
  $("rose-star-btn").onclick = () => { state.roseStarOnly = !state.roseStarOnly; renderRose(state.feed); };
  let rows = r.tracks || [];
  if (state.roseStarOnly) rows = rows.filter((t) => state.starred.has(t.symbol));
  const shown = rows.slice(0, state.roseShown);
  $("rose-table").querySelector("tbody").innerHTML = shown.length ? shown.map((t) => {
    const hl = domHl(t);
    return `
    <tr>
      <td class="lft">${fmtMsk(t.anchor_ts)}</td>
      <td class="sym lft">${starHtml(t.symbol)} ${esc(t.symbol)}</td>
      <td class="lft">${dirCell(t.direction)}</td>
      <td>${t.confirms > 1 ? `<b>×${t.confirms}</b>` : "1"}</td>
      <td><span class="pos">${fmtPct(t.peak6_pct, 1)}</span></td>
      <td><span class="${cls(t.dd6_pct)}">${fmtPct(t.dd6_pct, 1)}</span></td>
      <td><span class="pos${hl.peak}">${fmtPct(t.peak24_pct, 1)}</span></td>
      <td><span class="${cls(t.dd24_pct)}${hl.dd}">${fmtPct(t.dd24_pct, 1)}</span></td>
      <td><span class="${cls(t.ret24_pct)}">${fmtPct(t.ret24_pct, 1)}</span></td>
      <td class="lft">${trackStatus(t)}</td>
    </tr>`;
  }).join("") : '<tr><td colspan="10" class="empty">Треков пока нет.</td></tr>';
  const more = $("rose-more");
  more.hidden = rows.length <= state.roseShown;
  more.onclick = () => { state.roseShown += 60; renderRose(feed); };
}

/* ── история всех сигналов (ledger) ── */
function renderHistory(feed) {
  const rows = feed.signals_history || [];
  const srcs = ["all", "starred", ...new Set(rows.map((t) => t.source))];
  $("hist-filters").innerHTML = srcs.map((c) =>
    `<button class="fbtn ${state.histFilter === c ? "on" : ""}" data-src="${esc(c)}">${c === "all" ? "все источники" : c === "starred" ? "★ отмеченные" : SRC_RU[c] || esc(c)}</button>`).join("");
  $("hist-filters").querySelectorAll(".fbtn").forEach((b) =>
    b.onclick = () => { state.histFilter = b.dataset.src; state.histShown = 40; renderHistory(feed); });
  const filtered = state.histFilter === "all" ? rows
    : state.histFilter === "starred" ? rows.filter((t) => state.starred.has(t.symbol))
    : rows.filter((t) => t.source === state.histFilter);
  const shown = filtered.slice(0, state.histShown);
  $("hist-table").querySelector("tbody").innerHTML = shown.length ? shown.map((t) => {
    const hl = domHl(t);
    return `
    <tr>
      <td class="lft">${fmtMsk(t.anchor_ts)}</td>
      <td class="lft"><span class="badge ${esc(t.source)}">${SRC_RU[t.source] || esc(t.source)}</span>${t.setup ? ` <span class="flat">${esc(t.setup)}</span>` : ""}</td>
      <td class="sym lft">${starHtml(t.symbol)} ${esc(t.symbol)}</td>
      <td class="lft">${dirCell(t.direction)}</td>
      <td>${t.confirms > 1 ? `<b>×${t.confirms}</b>` : "1"}</td>
      <td><span class="pos${hl.peak}">${fmtPct(t.peak24_pct, 1)}</span></td>
      <td><span class="${cls(t.dd24_pct)}${hl.dd}">${fmtPct(t.dd24_pct, 1)}</span></td>
      <td><span class="${cls(t.ret24_pct)}">${fmtPct(t.ret24_pct, 1)}</span></td>
      <td class="lft">${trackStatus(t)}</td>
    </tr>`;
  }).join("") : '<tr><td colspan="9" class="empty">История накапливается — сигналы появятся после первых резолвов.</td></tr>';
  const more = $("hist-more");
  more.hidden = filtered.length <= state.histShown;
  more.onclick = () => { state.histShown += 60; renderHistory(feed); };
}

/* ── бот ── */
function renderBot(feed) {
  const b = feed.bot_stats || {};
  $("bot-window").textContent = `честное окно: сигналы с ${b.window_start || "—"} · n=${b.n ?? 0}`;
  const dirs = b.by_direction || {};
  const eq = b.equity_r || [];
  const lastR = eq.length ? eq[eq.length - 1].cum_r : null;
  const wrAll = b.n ? Object.values(b.by_setup || {}).reduce((s, x) => s + x.wr24h_pct * x.n, 0) / b.n : null;
  $("bot-tiles").innerHTML = `
    <div class="tile"><div class="v">${b.n ?? "—"}</div><div class="l">резолвленных сигналов</div></div>
    <div class="tile"><div class="v">${wrAll != null ? wrAll.toFixed(1) + "%" : "—"}</div><div class="l">winrate 24ч (все сетапы)</div></div>
    <div class="tile"><div class="v ${lastR != null ? (lastR >= 0 ? "pos" : "neg") : ""}">${lastR != null ? (lastR > 0 ? "+" : "") + lastR.toFixed(1) + "R" : "—"}</div><div class="l">накопленный R (24ч-выходы)</div></div>
    ${dirs.long ? `<div class="tile"><div class="v">${dirs.long.wr24h_pct}%</div><div class="l">winrate лонги (n=${dirs.long.n})</div></div>` : ""}
    ${dirs.short ? `<div class="tile"><div class="v">${dirs.short.wr24h_pct}%</div><div class="l">winrate шорты (n=${dirs.short.n})</div></div>` : ""}`;
  drawSetupChart(b.by_setup || {});
  drawEquityChart(eq);
  $("bot-table").querySelector("tbody").innerHTML = (b.recent || []).map((r) => `
    <tr>
      <td class="lft">${fmtMsk(r.ts + ":00Z") /* naive UTC из фида */}</td>
      <td class="sym lft">${starHtml(r.symbol)} ${esc(r.symbol)}</td>
      <td class="lft">${esc(r.setup)}</td>
      <td class="lft"><span class="${r.direction === "long" ? "pos" : "neg"}">${r.direction === "long" ? "▲" : "▼"} ${esc(r.direction)}</span></td>
      <td><span class="${cls(r.change_24h_pct)}">${fmtPct(r.change_24h_pct, 1)}</span></td>
      <td><span class="${cls(r.r_24h)}">${r.r_24h == null ? "—" : (r.r_24h > 0 ? "+" : "") + r.r_24h.toFixed(2)}</span></td>
      <td><span class="${cls(r.mfe_24h)}">${fmtPct(r.mfe_24h, 1)}</span></td>
      <td><span class="${cls(r.mae_24h)}">${fmtPct(r.mae_24h, 1)}</span></td>
      <td class="lft">${esc(r.outcome_24h || "—")}</td>
    </tr>`).join("") || '<tr><td colspan="9" class="empty">Пусто.</td></tr>';
}

function drawSetupChart(bySetup) {
  const svg = $("setup-chart");
  const entries = Object.entries(bySetup).sort((a, b) => b[1].n - a[1].n);
  if (!entries.length) { svg.innerHTML = ""; return; }
  const W = 560, rowH = 34, P = { l: 96, r: 60 };
  const H = entries.length * rowH + 8;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.style.height = `${H}px`;
  const maxWr = Math.max(...entries.map(([, v]) => v.wr24h_pct), 50);
  const w = (v) => (v / maxWr) * (W - P.l - P.r);
  svg.innerHTML = entries.map(([name, v], i) => {
    const y = i * rowH + 8;
    return `
      <text class="bar-name" x="${P.l - 8}" y="${y + 14}" text-anchor="end">${esc(name)}</text>
      <rect x="${P.l}" y="${y}" width="${Math.max(2, w(v.wr24h_pct))}" height="20" rx="4"
            fill="var(--s1)" data-tip="${esc(name)}: winrate 24ч ${v.wr24h_pct}% · 4ч ${v.wr4h_pct}% · n=${v.n}${v.avg_r24 != null ? " · avg R " + v.avg_r24 : ""}"/>
      <text class="bar-lbl" x="${P.l + Math.max(2, w(v.wr24h_pct)) + 7}" y="${y + 14}">${v.wr24h_pct}%${v.low_n ? " ⚠" : ""} <tspan fill="var(--muted)">n=${v.n}</tspan></text>`;
  }).join("");
  hookTips(svg);
}

function drawEquityChart(eq) {
  const svg = $("equity-chart");
  if (!eq || eq.length < 2) { svg.innerHTML = ""; return; }
  const W = 560, H = 190, P = { l: 44, r: 10, t: 8, b: 20 };
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.style.height = `${H}px`;
  const vals = eq.map((p) => p.cum_r);
  const min = Math.min(...vals, 0), max = Math.max(...vals, 0);
  const span = max - min || 1;
  const x = (i) => P.l + (i / (eq.length - 1)) * (W - P.l - P.r);
  const y = (v) => P.t + (1 - (v - min) / span) * (H - P.t - P.b);
  const d = vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const ticks = [min, min + span / 2, max].map((v) => Math.round(v));
  svg.innerHTML = `
    ${ticks.map((t) => `<line x1="${P.l}" x2="${W - P.r}" y1="${y(t)}" y2="${y(t)}" stroke="var(--grid)" stroke-width="1"/>
      <text class="axis-lbl" x="${P.l - 6}" y="${y(t) + 3}" text-anchor="end">${t}R</text>`).join("")}
    <line x1="${P.l}" x2="${W - P.r}" y1="${y(0)}" y2="${y(0)}" stroke="var(--muted)" stroke-width="1" stroke-dasharray="3,3"/>
    <path d="${d}" fill="none" stroke="var(--s1)" stroke-width="2"/>
    <line id="eq-cross" y1="${P.t}" y2="${H - P.b}" stroke="var(--muted)" stroke-width="1" visibility="hidden"/>
    <circle id="eq-dot" r="3.5" fill="var(--s1)" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>
    <rect x="${P.l}" y="${P.t}" width="${W - P.l - P.r}" height="${H - P.t - P.b}" fill="transparent" id="eq-hover"/>
    <text class="axis-lbl" x="${P.l}" y="${H - 4}">${esc(eq[0].ts.slice(0, 10))}</text>
    <text class="axis-lbl" x="${W - P.r}" y="${H - 4}" text-anchor="end">${esc(eq[eq.length - 1].ts.slice(0, 10))}</text>`;
  const hover = svg.querySelector("#eq-hover");
  const crossEl = svg.querySelector("#eq-cross"), dotEl = svg.querySelector("#eq-dot");
  const tip = $("tooltip");
  hover.addEventListener("mousemove", (ev) => {
    const rect = svg.getBoundingClientRect();
    const relX = (ev.clientX - rect.left) / rect.width * W;
    const i = Math.max(0, Math.min(eq.length - 1, Math.round((relX - P.l) / (W - P.l - P.r) * (eq.length - 1))));
    crossEl.setAttribute("x1", x(i)); crossEl.setAttribute("x2", x(i));
    crossEl.setAttribute("visibility", "visible");
    dotEl.setAttribute("cx", x(i)); dotEl.setAttribute("cy", y(vals[i]));
    dotEl.setAttribute("visibility", "visible");
    tip.style.display = "block";
    tip.style.left = `${ev.clientX + 14}px`; tip.style.top = `${ev.clientY - 10}px`;
    tip.textContent = `${eq[i].ts.replace("T", " ")} · ${vals[i] > 0 ? "+" : ""}${vals[i].toFixed(2)}R`;
  });
  hover.addEventListener("mouseleave", () => {
    crossEl.setAttribute("visibility", "hidden"); dotEl.setAttribute("visibility", "hidden");
    tip.style.display = "none";
  });
}

function hookTips(svg) {
  const tip = $("tooltip");
  svg.querySelectorAll("[data-tip]").forEach((el) => {
    el.addEventListener("mousemove", (ev) => {
      tip.style.display = "block";
      tip.style.left = `${ev.clientX + 14}px`; tip.style.top = `${ev.clientY - 10}px`;
      tip.textContent = el.dataset.tip;
    });
    el.addEventListener("mouseleave", () => { tip.style.display = "none"; });
  });
}

/* ── шторм/радар ── */
function renderStorm(feed) {
  const st = feed.storm || {}, rd = feed.radar || {};
  $("storm-tiles").innerHTML = `
    <div class="tile"><div class="v">${(st.watchlist || []).length}</div><div class="l">пружин под наблюдением</div></div>
    <div class="tile"><div class="v">${rd.n ?? "—"}</div><div class="l">объёмных всплесков (всего)</div></div>
    <div class="tile"><div class="v">${rd.good_pct != null ? rd.good_pct + "%" : "—"}</div><div class="l">всплесков «good»</div></div>
    <div class="tile"><div class="v">${rd.avg_mfe_pct != null ? "+" + rd.avg_mfe_pct + "%" : "—"}</div><div class="l">средний MFE всплеска</div></div>`;
  $("storm-table").querySelector("tbody").innerHTML = (st.watchlist || []).map((w) => `
    <tr>
      <td class="sym lft">${starHtml(w.symbol)} ${esc(w.symbol)}</td>
      <td class="lft">${fmtMsk(w.added_ts)}</td>
      <td>${w.price_at_add ?? "—"}</td>
      <td>${w.box_width_pct != null ? w.box_width_pct.toFixed(2) + "%" : "—"}</td>
      <td>${w.compress_p != null ? "p" + w.compress_p : "—"}</td>
      <td><span class="${cls(w.oi_chg_4h)}">${fmtPct(w.oi_chg_4h, 1)}</span></td>
      <td>${w.funding != null ? (w.funding * 100).toFixed(3) + "%" : "—"}</td>
    </tr>`).join("") || '<tr><td colspan="7" class="empty">Watchlist пуст.</td></tr>';
}

/* ── главный цикл ── */
async function refresh() {
  const feed = await fetchFeed();
  feedStatus(feed);
  if (!feed) return;
  const isNew = !state.feed || feed.generated_at !== state.feed.generated_at;
  state.feed = feed;
  if (isNew) {
    renderPump(feed);
    renderCombos();
    renderRose(feed);
    renderHistory(feed);
    renderBot(feed);
    renderStorm(feed);
  }
  await renderLive(feed); // сам решает, перестраивать ли карточки
}

/* ── автообновление UI: вечная вкладка сама подтягивает новый релиз ──
   Сравниваем версию app.js в СВЕЖЕМ index.html (no-store) с версией,
   с которой загружены сами (из своего <script src>). Разошлись → reload. */
const MY_VER = (document.querySelector('script[src*="app.js"]')?.src.match(/v=(\d+)/) || [])[1];
async function checkUiVersion() {
  try {
    const r = await fetch("index.html", { cache: "no-store" });
    const ver = ((await r.text()).match(/app\.js\?v=(\d+)/) || [])[1];
    if (MY_VER && ver && ver !== MY_VER) location.reload();
  } catch (_) { /* сеть моргнула — проверим в следующий раз */ }
}
setInterval(checkUiVersion, 5 * 60_000);

refresh();
setInterval(refresh, FEED_POLL_MS);
setInterval(() => { // спарклайны: дотягиваем свежие закрытые бары
  for (const card of state.liveCards.values())
    if (card.basis != null) initCardBars(card);
}, KLINE_REFRESH_MS);

#!/usr/bin/env node
/*
 * tv_plan_render.js — рендерит размеченный торговый план на ОДНОМ foreground-графике
 * TradingView Desktop (CDP) и снимает PNG.  Без LLM, чистая математика по барам.
 *
 * Вход:  JSON-спека первым аргументом (или через --file path).
 *   { "symbol":"ETH",            // монета; нормализуется в BYBIT:<BASE>USDT.P
 *     "direction":"SHORT"|"LONG"|null,   // override; null = считать по структуре
 *     "entry":1904.3|null, "sl":1935.6|null, "tp":1852|null,  // override уровней
 *     "note":"строка"|null,      // доп. текст на графике
 *     "out":"/path/plan.png"|null,        // куда сохранить PNG
 *     "tf":"15" }                // таймфрейм (по умолчанию 15)
 *
 * Выход (stdout, одна строка JSON): { ok, path, plan, warnings }
 * Логи/дисклеймер — в stderr.
 *
 * ВАЖНО: корректный скрин снимается только с вкладки, реально в фокусе.
 * Прод-схема: ОДИН выделенный график TradingView в foreground.
 */
const path = require('path');
const fs = require('fs');
const http = require('http');

const MCP_DIR = process.env.TV_MCP_DIR || path.join(require('os').homedir(), 'tradingview-mcp');
const CDP = require(path.join(MCP_DIR, 'node_modules', 'chrome-remote-interface'));
const HOST = process.env.TV_CDP_HOST || '127.0.0.1';
const PORT = parseInt(process.env.TV_CDP_PORT || '9222', 10);
const CHART = 'window.TradingViewApi._activeChartWidgetWV.value()';
const wait = ms => new Promise(r => setTimeout(r, ms));
const r2 = x => Math.round(x * 100) / 100;

function log(...a) { process.stderr.write('[tv-render] ' + a.join(' ') + '\n'); }
function listTargets() {
  return new Promise((res, rej) => {
    const req = http.get(`http://${HOST}:${PORT}/json/list`, r => { let d = ''; r.on('data', c => d += c); r.on('end', () => { try { res(JSON.parse(d)); } catch (e) { rej(e); } }); });
    req.on('error', rej); req.setTimeout(4000, () => req.destroy(new Error('cdp list timeout')));
  });
}
function normalizeSymbol(s) {
  let x = String(s || '').toUpperCase().trim().replace(/^BYBIT:/, '').replace(/\.P$/, '');
  if (!x.endsWith('USDT')) x = x + 'USDT';
  return 'BYBIT:' + x + '.P';
}

function parseArgs() {
  const a = process.argv.slice(2);
  const fi = a.indexOf('--file');
  if (fi >= 0) return JSON.parse(fs.readFileSync(a[fi + 1], 'utf8'));
  if (a[0]) return JSON.parse(a[0]);
  throw new Error('no spec given');
}

(async () => {
  const out = { ok: false, warnings: [] };
  let spec;
  try { spec = parseArgs(); } catch (e) { console.log(JSON.stringify({ ok: false, error: 'bad spec: ' + e.message })); process.exit(1); }
  const symbol = normalizeSymbol(spec.symbol);
  const tf = String(spec.tf || '15');
  out.symbol = symbol;

  // Выбрать видимую (foreground) вкладку: canvas перерисовывается только у visible.
  async function findVisible(tlist) {
    for (let attempt = 0; attempt < 4; attempt++) {
      if (attempt) await wait(700);
      for (const t of tlist) {
        let cl;
        try {
          cl = await CDP({ host: HOST, port: PORT, target: t.id });
          await cl.Runtime.enable();
          const vis = (await cl.Runtime.evaluate({ expression: 'document.visibilityState', returnByValue: true })).result.value;
          if (vis === 'visible') return { cl, t };
          await cl.close();
        } catch (e) { if (cl) { try { await cl.close(); } catch (_) { } } }
      }
    }
    return null;
  }
  async function waitForApi(cl, tries = 18) {
    for (let i = 0; i < tries; i++) {
      try {
        const ok = (await cl.Runtime.evaluate({ expression: `(function(){try{return !!(window.TradingViewApi&&window.TradingViewApi._activeChartWidgetWV&&window.TradingViewApi._activeChartWidgetWV.value().symbol());}catch(e){return false;}})()`, returnByValue: true })).result.value;
        if (ok) return true;
      } catch (e) { }
      await wait(700);
    }
    return false;
  }

  let allTargets;
  try { allTargets = (await listTargets()).filter(t => t.type === 'page'); }
  catch (e) { console.log(JSON.stringify({ ok: false, error: 'CDP недоступен: ' + e.message })); process.exit(2); }
  const chartTargets = allTargets.filter(t => /tradingview\.com\/chart/i.test(t.url));
  let client = null, chosen = null;

  // НОВАЯ ВКЛАДКА НА МОНЕТУ (spec.navTab): Python уже открыл пустую вкладку через меню
  // «Новая вкладка» (System Events). Находим её (file://…app, title «Новая вкладка») и
  // навигируем на chart-URL с символом → вкладка становится графиком. Прошлые монеты целы.
  if (spec.navTab) {
    const blank = allTargets.find(t => /новая вкладка|new tab/i.test(t.title || '') || (/app(\.html)?$/i.test(t.url) && !/chart/i.test(t.url)));
    if (blank) {
      try {
        const cl = await CDP({ host: HOST, port: PORT, target: blank.id });
        await cl.Page.enable(); await cl.Runtime.enable();
        const url = 'https://ru.tradingview.com/chart/?symbol=' + encodeURIComponent(symbol);
        await cl.Page.navigate({ url });
        const ok = await waitForApi(cl);
        if (ok) { client = cl; chosen = blank; out.newTab = true; }
        else { out.warnings.push('навигация вкладки не подняла chart API'); try { await cl.close(); } catch (_) { } }
      } catch (e) { out.warnings.push('navTab: ' + e.message); }
    } else {
      out.warnings.push('пустую вкладку «Новая вкладка» не нашёл (меню не открыло?) — рисую на текущей');
    }
  }

  // Фолбэк / обычный режим (перезапись текущей видимой вкладки)
  if (!client) {
    if (!chartTargets.length) { console.log(JSON.stringify({ ok: false, error: 'нет chart-вкладки TradingView' })); process.exit(2); }
    const f0 = await findVisible(chartTargets);
    if (f0) { client = f0.cl; chosen = f0.t; }
    else {
      chosen = chartTargets[0];
      client = await CDP({ host: HOST, port: PORT, target: chosen.id });
      out.warnings.push('нет chart-вкладки в фокусе — взял первую; скрин может быть подмёрзшим.');
    }
  }
  try {
    await client.Page.enable(); await client.Runtime.enable(); await client.DOM.enable();
    async function ev(expr, awaitPromise = false) {
      const r = await client.Runtime.evaluate({ expression: expr, returnByValue: true, awaitPromise });
      if (r.exceptionDetails) throw new Error((r.exceptionDetails.exception && r.exceptionDetails.exception.description) || r.exceptionDetails.text);
      return r.result.value;
    }
    try { await client.Page.bringToFront(); } catch (e) { }

    await ev(`${CHART}.setSymbol(${JSON.stringify(symbol)}, {})`);
    await ev(`${CHART}.setResolution(${JSON.stringify(tf)}, {})`);

    // ждём загрузку баров
    let bars = null;
    for (let k = 0; k < 15; k++) {
      await wait(900);
      bars = await ev(`(function(){try{var b=${CHART}._chartWidget.model().mainSeries().bars();if(!b||typeof b.lastIndex!=='function')return null;var r=[],e=b.lastIndex(),s=Math.max(b.firstIndex(),e-95);for(var i=s;i<=e;i++){var v=b.valueAt(i);if(v)r.push([v[0],v[1],v[2],v[3],v[4],v[5]||0]);}return r;}catch(_){return null;}})()`);
      if (bars && bars.length >= 50) break;
    }
    if (!bars || bars.length < 30) { out.error = 'бары не загрузились'; console.log(JSON.stringify(out)); await client.close(); process.exit(3); }

    const t = bars.map(b => b[0]), o = bars.map(b => b[1]), h = bars.map(b => b[2]), l = bars.map(b => b[3]), c = bars.map(b => b[4]);
    const n = bars.length, last = c[n - 1];
    const ema = (v, p) => { const k = 2 / (p + 1); let e = v[0]; for (let i = 1; i < v.length; i++) e = v[i] * k + e * (1 - k); return e; };
    const ema21 = ema(c, 21), ema50 = ema(c, 50);
    const w = 40, s0 = Math.max(0, n - w); let hi = -Infinity, lo = Infinity;
    for (let i = s0; i < n; i++) { if (h[i] > hi) hi = h[i]; if (l[i] < lo) lo = l[i]; }
    const rng = hi - lo || (last * 0.01);
    const bearish = last < ema50 && ema21 < ema50, bullish = last > ema50 && ema21 > ema50;
    const trend = bearish ? 'downtrend' : bullish ? 'uptrend' : 'mixed';
    const f = r => lo + r * rng;

    // OB/FVG-зоны из баров графика (вторичный контекст; SMC у брата разоблачена → подпись «detected»)
    function structZones() {
      const price = last, zs = [];
      const st = Math.max(6, n - 40);
      for (let i = st; i < n - 1; i++) {
        const ph = h.slice(i - 5, i);
        if (ph.length && c[i] > Math.max(...ph)) for (let j = i - 1; j > Math.max(0, i - 6); j--) { if (c[j] < o[j]) { if (price > c[j]) zs.push({ top: o[j], bottom: c[j], color: '#7e57c2', label: 'OB↑ det' }); break; } }
        const pl = l.slice(i - 5, i);
        if (pl.length && c[i] < Math.min(...pl)) for (let j = i - 1; j > Math.max(0, i - 6); j--) { if (c[j] > o[j]) { if (price < c[j]) zs.push({ top: c[j], bottom: o[j], color: '#7e57c2', label: 'OB↓ det' }); break; } }
      }
      for (let i = Math.max(2, n - 40); i < n - 1; i++) {
        if (h[i - 2] < l[i]) { const b = h[i - 2], tp2 = l[i]; if ((tp2 - b) / price * 100 >= 0.05 && price >= b) zs.push({ top: tp2, bottom: b, color: '#26a69a', label: 'FVG det' }); }
        if (l[i - 2] > h[i]) { const b = h[i], tp2 = l[i - 2]; if ((tp2 - b) / price * 100 >= 0.05 && price <= tp2) zs.push({ top: tp2, bottom: b, color: '#26a69a', label: 'FVG det' }); }
      }
      const seen = new Set(), uniq = [];
      zs.sort((a, b) => Math.abs((a.top + a.bottom) / 2 - price) - Math.abs((b.top + b.bottom) / 2 - price));
      for (const z of zs) { const k = z.label + Math.round(z.top / price * 1000); if (!seen.has(k)) { seen.add(k); uniq.push(z); } }
      return uniq.slice(0, 4);
    }

    // направление и уровни: override из сигнала бота ИЛИ расчёт по структуре
    let dir = (spec.direction || '').toUpperCase();
    if (dir !== 'SHORT' && dir !== 'LONG') dir = bearish ? 'SHORT' : bullish ? 'LONG' : (ema21 >= ema50 ? 'LONG' : 'SHORT');
    let entry = Number.isFinite(spec.entry) ? spec.entry : null;
    let sl = Number.isFinite(spec.sl) ? spec.sl : null;
    let tp = Number.isFinite(spec.tp) ? spec.tp : null;
    if (entry == null || sl == null || tp == null) {
      if (dir === 'SHORT') { entry = entry ?? f(0.236); sl = sl ?? (f(0.5) + 0.04 * rng); tp = tp ?? (lo - 0.272 * rng); }
      else { entry = entry ?? f(0.764); sl = sl ?? (f(0.5) - 0.04 * rng); tp = tp ?? (hi + 0.272 * rng); }
    }
    const rr = Math.abs(entry - tp) / (Math.abs(sl - entry) || 1e-9);
    // точность подписи по величине цены (DOGE ~0.09 нельзя округлять до 0.01)
    const fmt = x => { const a = Math.abs(x); const d = a >= 100 ? 2 : a >= 1 ? 3 : a >= 0.01 ? 5 : 7; return Number(x.toFixed(d)); };
    out.plan = { dir, last: fmt(last), ema21: fmt(ema21), ema50: fmt(ema50), trend, swingHi: fmt(hi), swingLo: fmt(lo), entry: fmt(entry), sl: fmt(sl), tp: fmt(tp), rr: r2(rr) };

    const TR = t[n - 1], TF = TR + 18000, TL = TR - 21 * 900;
    // ВАЖНО: координаты фигур — СЫРЫЕ цены (без округления), иначе на дешёвых монетах линии слипаются
    async function shape(sh, price, ov, text) {
      await ev(`${CHART}.createShape({time:${TR},price:${price}},{shape:'${sh}',overrides:${JSON.stringify(ov)},text:${JSON.stringify(text || '')}})`);
    }
    async function textAt(time, price, text, ov) {
      await ev(`${CHART}.createShape({time:${time},price:${price}},{shape:'text',overrides:${JSON.stringify(ov)},text:${JSON.stringify(text)}})`);
    }
    try { await ev(`${CHART}.removeAllShapes()`); } catch (e) { out.warnings.push('removeAllShapes: ' + e.message); }

    const dc = dir === 'SHORT' ? '#ef5350' : '#26a69a';
    const base = normalizeSymbol(symbol).replace('BYBIT:', '');
    const stopPct = Math.abs(sl - entry) / entry * 100;
    const tgtPct = Math.abs(tp - entry) / entry * 100;

    // вертикаль свечи входа + 3 КЛЮЧЕВЫХ уровня — в любом режиме
    await shape('vertical_line', last, { linecolor: '#ffeb3b', linewidth: 2, linestyle: 2 });
    await shape('horizontal_line', entry, { linecolor: '#2962ff', linewidth: 2 });
    await shape('horizontal_line', sl, { linecolor: '#ef5350', linewidth: 2, linestyle: 2 });
    await shape('horizontal_line', tp, { linecolor: '#26a69a', linewidth: 2, linestyle: 2 });

    if (spec.simple) {
      // ПРОСТОЙ режим: только вход/стоп/цель + заголовок. Чисто, по-русски, для глаза.
      await textAt(TL, hi, `${dir === 'SHORT' ? '🔴 ШОРТ' : '🟢 ЛОНГ'} · ${base} · риск 1 : прибыль ${r2(rr)}`, { color: dc, fontsize: 17, bold: true });
      await textAt(TR, last, 'вход здесь ↓', { color: '#ffeb3b', fontsize: 12 });
      await textAt(TF, entry, `ВХОД ${fmt(entry)}`, { color: '#2962ff', fontsize: 15, bold: true });
      await textAt(TF, sl, `СТОП ${fmt(sl)} · −${stopPct.toFixed(1)}%`, { color: '#ef5350', fontsize: 15, bold: true });
      await textAt(TF, tp, `ЦЕЛЬ ${fmt(tp)} · +${tgtPct.toFixed(1)}%`, { color: '#26a69a', fontsize: 15, bold: true });
      if (spec.note) await textAt(TL, lo, String(spec.note), { color: '#b2b5be', fontsize: 11 });
    } else {
      // структура: свинги (S/R)
      await shape('horizontal_line', hi, { linecolor: '#787b86', linewidth: 1 });
      await shape('horizontal_line', lo, { linecolor: '#787b86', linewidth: 1 });
      if (spec.rich) {
        for (const lv of (spec.levels || [])) {
          await shape('horizontal_line', lv.price, { linecolor: lv.color || '#26c6da', linewidth: lv.width || 2, linestyle: lv.style == null ? 0 : lv.style });
          if (lv.label) await textAt(TF, lv.price, lv.label, { color: lv.color || '#26c6da', fontsize: 11 });
        }
        const zones = (spec.zones || []).concat(structZones());
        for (const z of zones) {
          await shape('horizontal_line', z.top, { linecolor: z.color, linewidth: 1, linestyle: 1 });
          await shape('horizontal_line', z.bottom, { linecolor: z.color, linewidth: 1, linestyle: 1 });
          if (z.label) await textAt(TL, z.top, z.label, { color: z.color, fontsize: 10 });
        }
      } else {
        for (const rr2 of [0.236, 0.382, 0.5, 0.618, 0.786]) await shape('horizontal_line', f(rr2), { linecolor: '#ff9800', linewidth: 1, linestyle: 1 });
      }
      await textAt(TL, hi, `${dir} · ${base} · ${tf}m · ${trend}`, { color: dc, fontsize: 15, bold: true });
      await textAt(TR, hi, 'вход/сигнал ↓', { color: '#ffeb3b', fontsize: 12 });
      await textAt(TF, entry + rng * 0.012, `ENTRY ${fmt(entry)}`, { color: '#2962ff', fontsize: 12 });
      await textAt(TF, sl + rng * 0.012, `SL ${fmt(sl)}`, { color: '#ef5350', fontsize: 12 });
      await textAt(TF, tp + rng * 0.012, `TP ${fmt(tp)} (R:R ${r2(rr)})`, { color: '#26a69a', fontsize: 12 });
      if (spec.note) await textAt(TL, lo - rng * 0.06, String(spec.note), { color: '#b2b5be', fontsize: 12 });
    }

    // принудительно растянуть ВЕРТИКАЛЬНЫЙ масштаб, чтобы вход/стоп/цель влезли в кадр
    // (авто-масштаб фитит только последние свечи и режет план; setVisiblePriceRange чинит)
    try {
      const loP = Math.min(entry, sl, tp), hiP = Math.max(entry, sl, tp);
      const pad = (hiP - loP) * 0.15 || hiP * 0.01;
      await ev(`(function(){try{var ps=${CHART}.getPanes()[0].getMainSourcePriceScale();ps.setAutoScale(false);ps.setVisiblePriceRange({from:${loP - pad},to:${hiP + pad}});return 1;}catch(e){return 'E:'+e.message;}})()`);
    } catch (e) { out.warnings.push('priceRange: ' + e.message); }
    await wait(900);

    out.verifySym = await ev(`${CHART}.symbol()`);
    out.shapes = await ev(`(${CHART}.getAllShapes()||[]).length`);

    // скрин области графика
    const bounds = await ev(`(function(){var el=document.querySelector('[data-name="pane-canvas"]')||document.querySelector('canvas');if(!el)return null;var r=el.getBoundingClientRect();return JSON.stringify({x:r.x,y:r.y,width:r.width,height:r.height});})()`);
    const params = { format: 'png' };
    if (bounds) { const b = JSON.parse(bounds); params.clip = { x: b.x, y: b.y, width: b.width, height: b.height, scale: 1 }; }
    const shot = await client.Page.captureScreenshot(params);
    const outPath = spec.out || path.join(MCP_DIR, 'screenshots', 'tv_plan.png');
    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    fs.writeFileSync(outPath, Buffer.from(shot.data, 'base64'));
    out.ok = true; out.path = outPath;
    console.log(JSON.stringify(out));
    await client.close();
    process.exit(0);
  } catch (e) {
    out.error = e.message; console.log(JSON.stringify(out));
    try { await client.close(); } catch (_) { }
    process.exit(4);
  }
})();

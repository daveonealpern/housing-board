"use strict";
// Shared by the Housing Signal Board (index.html) and its phone version (app/).
// These are the functions that compute the board's trends and write its
// elevator pitch and executive summary from data.json, moved here unchanged so
// the board and the app always say exactly the same thing. They read the
// page's global D (the parsed data.json), which each page declares itself.

const fmt = v => v == null ? '-'
  : Math.abs(v) >= 1e9   ? (v/1e9).toFixed(1) + 'B'
  : Math.abs(v) >= 1e6   ? (v/1e6).toFixed(2) + 'M'
  : Math.abs(v) >= 10000 ? (v/1000).toFixed(1) + 'k'
  : Math.abs(v) >= 100   ? v.toFixed(0)
  : Math.abs(v) >= 10    ? v.toFixed(1) : v.toFixed(2);

// sid -> how to decorate a formatted number for that series' actual unit.
// Mirrors fetch.py's own unit strings exactly, so a change there is the only
// place this ever needs to follow. fmt() above is a pure magnitude formatter
// with no currency or percent awareness; every prefix/suffix is attached
// here, once, so a dollar value scaled to billions never double-prefixes.
const UNIT_DISPLAY = {
  '%':          {suffix: '%'},
  'bp':         {suffix: 'bp'},
  'months':     {suffix: ' mo'},
  'jobs':       {suffix: ' jobs'},
  'units':      {suffix: ' units'},
  'units SAAR': {suffix: ' units'},
  '$':          {prefix: '$'},
  '$ SAAR':     {prefix: '$'},
  'index':      {suffix: ''},
  'days':       {suffix: ' days'},
};
function unitOf(units){ return UNIT_DISPLAY[units] || {}; }

function fmtU(v, units){
  if(v == null) return '\u2014';
  const u = unitOf(units);
  return (u.prefix || '') + fmt(v) + (u.suffix || '');
}

function obsBefore(obs, iso){
  let best = null;
  for(const o of obs){ if(o.d <= iso) best = o; else break; }
  return best;
}

function shiftYears(iso, n){
  const d = new Date(iso + 'T12:00:00'); d.setFullYear(d.getFullYear() + n);
  return d.toISOString().slice(0,10);
}

// Year-on-year measured by DATE, not by row count. Daily, weekly, monthly and
// quarterly series all sit in the same payload, so counting rows back would
// mean thirteen days for SOFR and thirteen months for permits.
function trend(obs){
  const none = {dir:'flat', pct:null, abs:null, usePct:false, last:null, date:null, since:null};
  if(!obs || obs.length < 2) return none;
  const last = obs[obs.length-1];
  const ya = obsBefore(obs, shiftYears(last.d, -1));
  if(!ya || ya.d === last.d) return {...none, last:last.v, date:last.d};
  const abs = last.v - ya.v;
  // Percentage change is meaningless for anything that crosses zero, and nobody
  // quotes a yield curve that way regardless of where it currently sits. If the
  // series has been on both sides of zero, report the move in points instead.
  const w = obs.slice(-96).map(o => o.v);
  const crossesZero = Math.min(...w) < 0 && Math.max(...w) > 0;
  const usePct = !crossesZero && Math.abs(ya.v) > 0.5 && Math.abs(last.v) > 0.5
                 && (ya.v > 0) === (last.v > 0);
  const pct = usePct ? (abs/Math.abs(ya.v))*100 : null;
  const move = usePct ? pct : abs;
  const gate = usePct ? 0.6 : 0.05;
  const dir = move > gate ? 'up' : move < -gate ? 'down' : 'flat';
  return {dir, pct, abs, usePct, last:last.v, date:last.d, since:ya.d};
}

const health = s => { const t = trend(s.obs); return t.dir === 'flat' ? 'flat' : (t.dir === s.good ? 'up' : 'down'); };

function bucketHealth(bucket){
  const ids = Object.keys(D.series || {}).filter(id => D.series[id].bucket === bucket);
  let up = 0, down = 0, flat = 0;
  ids.forEach(id => {
    const h = health(D.series[id]);
    if(h === 'up') up++; else if(h === 'down') down++; else flat++;
  });
  return {up, down, flat, total: ids.length};
}

function lowerFirst(s){
  return s ? s.charAt(0).toLowerCase() + s.slice(1) : s;
}

function computeExecSignals(){
  const structural = bucketHealth('structural');
  const demand = bucketHealth('demand');
  const capital = bucketHealth('capital');
  const coincident = bucketHealth('coincident');
  const cost = bucketHealth('cost');
  const distress = bucketHealth('distress');
  const ca = bucketHealth('ca');

  const leadingUp = structural.up + demand.up + capital.up;
  const leadingDown = structural.down + demand.down + capital.down;
  const leadingTotal = structural.total + demand.total + capital.total;

  const curve = D.series && D.series['T10Y2Y'];
  const curveObs = curve && curve.obs || [];
  const curveNow = curveObs.length ? curveObs[curveObs.length-1].v : null;
  const curveInverted = curveNow != null ? curveNow < 0 : null;

  const spreadSeries = D.series && D.series['SPREAD'];
  const spreadTrend = spreadSeries ? trend(spreadSeries.obs) : null;

  const rateSeries = D.series && D.series['MORTGAGE30US'];
  const rateTrend = rateSeries ? trend(rateSeries.obs) : null;

  const resaleByKey = {};
  ((D.resale && D.resale.levels) || []).forEach(l => resaleByKey[l.key] = l);

  const stocks = (D.stocks || []).filter(s => s.ytd_pct != null);
  const avgYtd = stocks.length ? stocks.reduce((a,s) => a + s.ytd_pct, 0) / stocks.length : null;

  const distressSeries = D.series && D.series['DRSFRMACBS'];
  const distressTrend = distressSeries ? trend(distressSeries.obs) : null;

  return {structural, demand, capital, coincident, cost, distress, ca,
          leadingUp, leadingDown, leadingTotal, curveInverted, curveNow,
          spreadTrend, rateTrend, resaleByKey, avgYtd, distressTrend};
}

function execPitch(sig){
  const net = sig.leadingUp - sig.leadingDown;
  let lead;
  if(sig.leadingTotal === 0) lead = 'Not enough leading data has loaded yet to call a direction';
  else if(net >= 3) lead = 'Leading indicators are broadly pointing toward expansion';
  else if(net >= 1) lead = 'Leading indicators are leaning toward expansion, though not uniformly';
  else if(net <= -3) lead = 'Leading indicators are broadly pointing toward continued softness';
  else if(net <= -1) lead = 'Leading indicators are leaning toward softness, though not uniformly';
  else lead = 'Leading indicators are genuinely split, with no clear direction yet';

  let out = lead + '.';
  const headline = D.brief && D.brief.headline;
  if(headline){
    out += ' Among the public builders, ' + lowerFirst(headline.replace(/\.$/, '')) + '.';
  }
  return out;
}

function execSummary(sig){
  const paras = [];

  // 1. structural / leading
  if(sig.leadingTotal > 0){
    paras.push(sig.leadingUp + ' of ' + sig.leadingTotal + ' tracked leading series, covering the land pipeline, '
      + 'demand and credit, are currently trending in the expansionary direction, against ' + sig.leadingDown
      + ' trending the other way'
      + (sig.leadingUp + sig.leadingDown < sig.leadingTotal ? ' and the remainder essentially flat.' : '.'));
  }

  // 2. capital markets
  if(sig.curveInverted != null || sig.spreadTrend){
    let p = '';
    if(sig.curveInverted != null){
      p += 'The 2s10s curve is currently ' + (sig.curveInverted ? 'inverted' : 'positively sloped')
        + (sig.curveInverted ? ', historically the more cautious signal of the two, though the curve leads by roughly a year so this is not a near-term call. ' : '. ');
    }
    if(sig.rateTrend && sig.rateTrend.last != null){
      p += 'The 30-year mortgage rate sits at ' + fmtU(sig.rateTrend.last, '%')
        + (sig.rateTrend.usePct && sig.rateTrend.pct != null
           ? ', ' + (sig.rateTrend.pct >= 0 ? 'up ' : 'down ') + Math.abs(sig.rateTrend.pct).toFixed(1) + '% over the year'
           : '') + '. ';
    }
    if(sig.spreadTrend && sig.spreadTrend.last != null){
      const dir = sig.spreadTrend.dir;
      p += 'The spread over the 10-year Treasury is ' + fmtU(sig.spreadTrend.last, 'bp') + ', '
        + (dir === 'down' ? 'narrower than a year ago, which on its own improves affordability with no Fed move required.'
           : dir === 'up' ? 'wider than a year ago.'
           : 'little changed over the past year.');
    }
    if(p) paras.push(p.trim());
  }

  // 3. builders, leaning on the already-written brief rather than re-deriving it
  if(D.brief && (D.brief.takeaway || []).length){
    paras.push(D.brief.takeaway[0]);
  }

  // 4. resale market across the three levels
  const r = sig.resaleByKey;
  if(r.national || r.ca || r.ventura){
    const line = (key, label) => {
      const lvl = r[key]; if(!lvl) return null;
      const p = trend(lvl.median_price), s = trend(lvl.homes_sold);
      const bits = [];
      if(p.last != null && p.usePct) bits.push('median price ' + (p.pct >= 0 ? 'up ' : 'down ') + Math.abs(p.pct).toFixed(1) + '%');
      if(s.last != null && s.usePct) bits.push('volume ' + (s.pct >= 0 ? 'up ' : 'down ') + Math.abs(s.pct).toFixed(1) + '%');
      return bits.length ? label + ' ' + bits.join(', ') : null;
    };
    const parts = [line('national','nationally'), line('ca','in California'), line('ventura','in Ventura County')].filter(Boolean);
    if(parts.length){
      let sentence = 'In the resale market, ' + parts.join('; ') + ' over the past year.';
      if(r.national && r.national.estimated){
        sentence += ' The national figure is an estimate, aggregated from state-level data '
          + 'because the Redfin national file is currently unavailable, not their own calculation.';
      }
      paras.push(sentence);
    }
  }

  // 5. equity close
  if(sig.avgYtd != null){
    let p = 'The tracked public builders are averaging ' + (sig.avgYtd >= 0 ? '+' : '') + sig.avgYtd.toFixed(1)
      + '% year to date, ' + (sig.avgYtd >= 0 ? 'a market that is still giving the group credit' : 'a market that has been marking the group down')
      + ' despite the mixed operating picture above.';
    const lv = D.live;
    if(lv && lv.avgDay != null && lv.sessionIsToday){
      const mv = Math.abs(lv.avgDay) < 0.05 ? 'flat on average' : (lv.avgDay > 0 ? 'up ' : 'down ') + Math.abs(lv.avgDay).toFixed(1) + '% on average';
      p += lv.open ? ' As of ' + liveTime(lv.time) + ' they are trading ' + mv + ' today.'
                   : ' They closed ' + mv + ' today.';
    }
    paras.push(p);
  }

  return paras;
}



// ---------------------------------------------------------------------------
// Live share prices
//
// data.json carries each builder's previous close from the nightly run, which
// goes stale the moment the market opens. When a page opens, and every minute
// while the regular session is open, it asks the hsb-live Cloudflare Worker
// (worker/ in this repository) for current quotes and lays them over
// D.stocks: price, today's move, and year to date recomputed the way fetch.py
// does it (from each stock's first close of the year). Everything that reads
// D.stocks, including the executive summary's equity sentence, then reads the
// live numbers. With LIVE_QUOTES_URL empty, or the Worker unreachable, the
// pages show the nightly close exactly as before.
const LIVE_QUOTES_URL = 'https://hsb-live.plain-meadow-0e5d.workers.dev/quotes';

const LIVE = {last: null, timer: null, fetchedAt: 0};

const nyDate = sec => new Intl.DateTimeFormat('en-CA',
  {timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit'}).format(new Date(sec * 1000));
const liveTime = sec => new Date(sec * 1000).toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit'});
const round1 = v => Math.round(v * 10) / 10;

function fetchLiveQuotes(){
  if(!LIVE_QUOTES_URL) return Promise.resolve(null);
  return fetch(LIVE_QUOTES_URL, {cache: 'no-store'})
    .then(r => r.ok ? r.json() : null)
    .catch(() => null);
}

// Lays a /quotes payload over D.stocks. Safe to call again on the same D, or
// on a freshly loaded D, with the same payload. Returns true if any price moved
// onto the board.
function applyLiveQuotes(live){
  if(!live || !Array.isArray(live.quotes) || !D || !Array.isArray(D.stocks)) return false;
  const by = {};
  live.quotes.forEach(q => { if(q && q.symbol && typeof q.price === 'number' && q.time) by[q.symbol] = q; });
  let applied = 0, latest = 0, daySum = 0, dayN = 0, session = null;
  // Land bankers and commodity futures ride the same overlay; only the
  // builders feed the group's average move and the session clock.
  [...(D.landbanks || []), ...(D.commodities || [])].forEach(r => overlayQuote(r, by[r.ticker]));
  D.stocks.forEach(r => {
    const q = by[r.ticker];
    if(!q) return;
    const day = nyDate(q.time);
    const series = r.series || (r.series = []);
    const last = series[series.length - 1];
    if(last && day < last.d) return;                  // older than the nightly close already on file
    const v = Math.round(q.price * 100) / 100;
    if(last && last.d === day) last.v = v; else series.push({d: day, v});
    r.price = q.price;
    r.as_of = day;
    const base = series[0];
    if(base && base.v && base.d.slice(0, 4) === day.slice(0, 4)) r.ytd_pct = round1((q.price - base.v) / base.v * 100);
    r.day_pct = q.prevClose ? round1((q.price - q.prevClose) / q.prevClose * 100) : null;
    r.live_time = q.time;
    if(r.day_pct != null){ daySum += (q.price - q.prevClose) / q.prevClose * 100; dayN++; }
    if(q.time > latest){ latest = q.time; session = day; }
    applied++;
  });
  if(!applied) return false;
  D.live = {
    time: latest,
    session,
    sessionIsToday: session === nyDate(Date.now() / 1000),
    open: !!(live.market && live.market.open),
    avgDay: dayN ? daySum / dayN : null,
    count: applied,
    fetched: live.fetched || null,
  };
  return true;
}

// One non-builder row (land banker or commodity future): same arithmetic as
// the builder rows, without touching the builder average or session clock.
function overlayQuote(r, q){
  if(!r || !q) return;
  const day = nyDate(q.time), series = r.series || (r.series = []), last = series[series.length - 1];
  if(last && day < last.d) return;
  const dp = r.decimals != null ? Math.max(r.decimals, 2) : 2, v = +q.price.toFixed(dp);
  if(last && last.d === day) last.v = v; else series.push({d: day, v});
  r.price = q.price; r.as_of = day; r.live_time = q.time;
  const base = series[0];
  if(base && base.v && base.d.slice(0, 4) === day.slice(0, 4)) r.ytd_pct = round1((q.price - base.v) / base.v * 100);
  r.day_pct = q.prevClose ? round1((q.price - q.prevClose) / q.prevClose * 100) : null;
}

// Short label for wherever prices are shown: "Live 10:47 AM", "Close 1:00 PM"
// on an early-close day, or "Nov 14 close" when the last trade is not today.
function liveLabel(){
  const lv = D && D.live;
  if(!lv) return '';
  if(lv.open) return 'Live ' + liveTime(lv.time);
  if(lv.sessionIsToday) return 'Today’s close';
  const [y, m, d] = lv.session.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString('en-US', {month: 'short', day: 'numeric'}) + ' close';
}

// Runs the live-price loop for a page: fetch now, then every minute while the
// page is visible and the market is open, and at the next open otherwise.
// onUpdate() is the page's own redraw of whatever shows prices.
function startLiveQuotes(onUpdate){
  if(!LIVE_QUOTES_URL) return;
  const schedule = live => {
    clearTimeout(LIVE.timer);
    if(document.hidden) return;
    const m = live && live.market, now = Date.now() / 1000;
    let wait = 15 * 60;                                          // closed, or no answer: look again in 15 min
    if(m && m.open) wait = 60;
    else if(m && m.start && m.start > now) wait = Math.min(wait, Math.max(60, m.start - now + 30));
    LIVE.timer = setTimeout(tick, wait * 1000);
  };
  const tick = () => {
    LIVE.fetchedAt = Date.now();
    fetchLiveQuotes().then(live => {
      if(live && applyLiveQuotes(live)){ LIVE.last = live; onUpdate(); }
      schedule(live || LIVE.last);
    });
  };
  document.addEventListener('visibilitychange', () => {
    if(document.hidden){ clearTimeout(LIVE.timer); return; }
    if(Date.now() - LIVE.fetchedAt > 30 * 1000) tick(); else schedule(LIVE.last);
  });
  tick();
}

// After a page reloads data.json, the nightly close replaces the live prices;
// this puts the most recent quotes back on top.
function reapplyLiveQuotes(){ return LIVE.last ? applyLiveQuotes(LIVE.last) : false; }

// ---------------------------------------------------------------------------
// Shared pieces for the commodity, land banker and news panels on both pages.

// Year-to-date line for a price row ({series: [{d, v}]}), as a scalable SVG.
// The dashed line is the first close of the year, so the distance from it to
// the end of the line is the year-to-date move.
function ytdSvg(series, color, h){
  if(!series || series.length < 2) return '';
  const vals = series.map(p => p.v), mn = Math.min(...vals), mx = Math.max(...vals), sp = (mx - mn) || 1;
  const W = 300, H = h || 72, pad = 4;
  const x = i => (i / (vals.length - 1)) * W, y = v => pad + (H - 2 * pad) * (1 - (v - mn) / sp);
  const d = vals.map((v, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1)).join(' ');
  const b = y(vals[0]).toFixed(1);
  return '<svg class="ytd-svg" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" aria-hidden="true">'
    + '<path d="' + d + ' L' + W + ' ' + H + ' L0 ' + H + ' Z" fill="' + color + '" fill-opacity="0.08"/>'
    + '<line x1="0" x2="' + W + '" y1="' + b + '" y2="' + b + '" stroke="#E8E2D9" stroke-opacity="0.4" stroke-dasharray="3 3" vector-effect="non-scaling-stroke"/>'
    + '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="1.6" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>'
    + '</svg>';
}

const COST_UP = '#CF6E76', COST_DOWN = '#63B08F', COST_FLAT = '#E8E2D9';
// For a cost input, a rising price is pressure on the builder; for an equity
// it is the reverse. Returns the line colour and a tone word for text.
function priceTone(pct, isCost){
  if(pct == null || Math.abs(pct) < 0.5) return {color: COST_FLAT, tone: 'flat'};
  const bad = isCost ? pct > 0 : pct < 0;
  return {color: bad ? COST_UP : COST_DOWN, tone: bad ? 'worse' : 'better'};
}

function fmtPx(r, v){
  const dp = r.decimals != null ? r.decimals : 2, n = v == null ? r.price : v;
  return '$' + Number(n).toLocaleString('en-US', {minimumFractionDigits: dp, maximumFractionDigits: dp});
}

// "Jan 2 $533 · low $480 · high $640" pieces for under a YTD chart.
function ytdRange(r){
  const s = r.series || []; if(s.length < 2) return null;
  const vals = s.map(p => p.v), [y, m, d] = s[0].d.split('-').map(Number);
  return {start: fmtPx(r, vals[0]), startLabel: new Date(y, m - 1, d).toLocaleDateString('en-US', {month: 'short', day: 'numeric'}),
          lo: fmtPx(r, Math.min(...vals)), hi: fmtPx(r, Math.max(...vals))};
}

const signedPct = v => v == null ? '—' : (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v).toFixed(1) + '%';

// A dated list of news items ({title, link, source, published}).
function newsRows(items, n){
  return (items || []).slice(0, n || 8).map(it => {
    let when = '';
    if(it.published){ const [y, m, d] = it.published.split('-').map(Number); when = new Date(y, m - 1, d).toLocaleDateString('en-US', {month: 'short', day: 'numeric'}); }
    return '<a class="news-row" href="' + esc(it.link) + '" target="_blank" rel="noopener"><span class="news-t">' + esc(it.title) + '</span>'
      + '<span class="news-m">' + esc([it.source, when].filter(Boolean).join(' · ')) + '</span></a>';
  }).join('');
}

// ---------------------------------------------------------------------------
// The daily narrative (D.narrative), written each morning by fetch.py from the
// previous edition plus whatever is new. [n] markers in its text point at
// D.narrative.sources; both pages turn them into small source links.
const NARR_SECTIONS = [['national', 'National', 'Macro, rates and the public builders'],
                       ['west', 'California and the West', 'Builders, private builders, land'],
                       ['ventura', 'Ventura County', 'Local market and cities']];

function narrHtml(t){
  const src = (D.narrative || {}).sources || {};
  return esc(t || '').replace(/\[(\d+)\]/g, (m, n) => {
    const s = src[n];
    if(!s) return '';
    const title = esc([s.source, s.date, s.title].filter(Boolean).join(' · '));
    return s.url ? '<sup class="cite"><a href="' + esc(s.url) + '" target="_blank" rel="noopener" title="' + title + '">' + n + '</a></sup>'
                 : '<sup class="cite" title="' + title + '">' + n + '</sup>';
  });
}
const narrRefs = refs => (refs || []).map(n => narrHtml('[' + n + ']')).join('');
const narrDate = iso => { if(!iso) return ''; const [y, m, d] = iso.split('-').map(Number); return new Date(y, m - 1, d).toLocaleDateString('en-US', {month: 'short', day: 'numeric'}); };

// "Since Nov 20" bullets and any revisions to earlier calls.
function narrChangesHtml(N){
  const ch = (N.changes || []).map(c => '<li>' + narrHtml(c.text) + narrRefs(c.refs) + '</li>').join('');
  const rv = (N.revisions || []).map(r => '<div class="rev"><div class="rev-k">Revised call</div>'
      + '<p><b>Earlier:</b> ' + narrHtml(r.earlier) + '</p><p><b>Now:</b> ' + narrHtml(r.now) + narrRefs(r.refs) + '</p>'
      + (r.why ? '<p><b>Why it changed:</b> ' + narrHtml(r.why) + '</p>' : '') + '</div>').join('');
  return (ch ? '<div class="narr-since"><div class="narr-k">Since ' + esc(narrDate(N.since)) + '</div><ul>' + ch + '</ul></div>' : '') + rv;
}

// One line on when the narrative was written and checked.
function narrStamp(N){
  let t = 'Written ' + narrDate(N.as_of) + ' from ' + (N.evidence_count || 0) + ' new item' + (N.evidence_count === 1 ? '' : 's') + ' since ' + narrDate(N.since);
  if(N.checked && N.checked > N.as_of) t += '; checked ' + narrDate(N.checked) + ', nothing material since';
  return t + '.';
}

function narrSourcesHtml(N){
  const src = N.sources || {};
  return Object.keys(src).sort((a, b) => a - b).map(n => {
    const s = src[n];
    return '<li><span class="cite-n">' + n + '</span> ' + (s.url ? '<a href="' + esc(s.url) + '" target="_blank" rel="noopener">' + esc(s.title) + '</a>' : esc(s.title))
      + ' <span class="dim">' + esc([s.source, narrDate(s.date)].filter(Boolean).join(' · ')) + '</span></li>';
  }).join('');
}

function narrHistoryHtml(N){
  return (N.history || []).slice().reverse().map(h => '<li><b>' + esc(narrDate(h.date)) + '</b> ' + esc(h.headline)
    + ((h.revisions || []).length ? ' <span class="dim">Revised: ' + esc(h.revisions.join(' ')) + '</span>' : '') + '</li>').join('');
}

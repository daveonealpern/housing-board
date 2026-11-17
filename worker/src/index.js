// Live share prices for the Housing Signal Board.
//
// The board and the phone app are static pages on GitHub Pages. The nightly
// collector stores the previous close, which goes stale the moment the market
// opens. Browsers cannot call Yahoo Finance directly (it sends no CORS
// headers), so this Worker fetches the quotes server side and hands them back
// with CORS headers for the board's own origin.
//
// GET /quotes returns:
//   { fetched, source, market: {open, start, end}, quotes: [
//       {symbol, price, prevClose, time, name} ], missing: [symbols] }
// time, start and end are Unix seconds. The board applies the prices on top of
// the nightly data and recomputes year-to-date the same way the collector does.
//
// The symbol list is fixed on purpose: the Worker takes no symbols from the
// caller, so it cannot be used as a general-purpose proxy. Add a ticker here
// when one is added to BUILDERS in fetch.py.
const SYMBOLS = ['DHI', 'LEN', 'PHM', 'KBH', 'TOL', 'NVR', 'MTH', 'CCS', 'MHO', 'LGIH'];

const ALLOWED_ORIGINS = ['https://daveonealpern.github.io'];

// Each running copy of the Worker asks Yahoo at most once a minute, however
// many people have the board open. Yahoo's own quotes lag a little anyway.
const TTL_MS = 60 * 1000;
const RETRY_MS = 10 * 1000;   // after a run that returned nothing

const YAHOO_HOSTS = ['query1.finance.yahoo.com', 'query2.finance.yahoo.com'];
const YAHOO_HEADERS = {
  'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              + '(KHTML, like Gecko) Chrome/124.0 Safari/537.36',
  'Accept': 'application/json',
};

let cached = null;   // { at, promise }

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const cors = corsHeaders(request.headers.get('Origin'));

    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: cors });
    if (request.method !== 'GET') return new Response('Method not allowed', { status: 405, headers: cors });

    if (url.pathname === '/' || url.pathname === '') {
      return new Response('Housing Signal Board live quotes. GET /quotes\n',
        { headers: { ...cors, 'Content-Type': 'text/plain; charset=utf-8' } });
    }
    if (url.pathname !== '/quotes') return new Response('Not found', { status: 404, headers: cors });

    try {
      const body = await currentQuotes();
      return new Response(body, {
        headers: { ...cors, 'Content-Type': 'application/json', 'Cache-Control': 'public, max-age=30' },
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: String(e && e.message || e) }), {
        status: 502, headers: { ...cors, 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
      });
    }
  },
};

function corsHeaders(origin) {
  const h = { 'Vary': 'Origin' };
  if (origin && ALLOWED_ORIGINS.includes(origin)) {
    h['Access-Control-Allow-Origin'] = origin;
    h['Access-Control-Allow-Methods'] = 'GET, OPTIONS';
    h['Access-Control-Max-Age'] = '86400';
  }
  return h;
}

// One fetch in flight at a time per copy of the Worker; everyone else waits on it.
function currentQuotes() {
  const now = Date.now();
  if (!cached || now - cached.at > TTL_MS) {
    const entry = { at: now, promise: null };
    entry.promise = loadQuotes().then(o => {
      if (!o.quotes.length) entry.at = now - TTL_MS + RETRY_MS;
      return JSON.stringify(o);
    });
    entry.promise.catch(() => { if (cached === entry) cached = null; });
    cached = entry;
  }
  return cached.promise;
}

async function loadQuotes() {
  const results = await Promise.all(SYMBOLS.map(quote));
  const quotes = results.filter(Boolean);
  return {
    fetched: new Date().toISOString(),
    source: 'Yahoo Finance chart API (unofficial)',
    market: marketState(quotes),
    quotes: quotes.map(({ regular, ...q }) => q),
    missing: SYMBOLS.filter(s => !quotes.some(q => q.symbol === s)),
  };
}

async function quote(symbol) {
  for (const host of YAHOO_HOSTS) {
    try {
      const r = await fetch('https://' + host + '/v8/finance/chart/' + encodeURIComponent(symbol)
        + '?range=1d&interval=5m', { headers: YAHOO_HEADERS });
      if (!r.ok) continue;
      const j = await r.json();
      const res = j && j.chart && j.chart.result && j.chart.result[0];
      const m = res && res.meta;
      if (!m || typeof m.regularMarketPrice !== 'number') continue;
      const reg = m.currentTradingPeriod && m.currentTradingPeriod.regular;
      return {
        symbol,
        name: m.shortName || m.longName || null,
        price: m.regularMarketPrice,
        prevClose: typeof m.chartPreviousClose === 'number' ? m.chartPreviousClose
                 : typeof m.previousClose === 'number' ? m.previousClose : null,
        time: m.regularMarketTime || null,
        regular: reg ? { start: reg.start, end: reg.end } : null,
      };
    } catch (e) {
      // try the next host
    }
  }
  return null;
}

// Open means the regular session (9:30 AM to 4:00 PM New York) is under way
// right now, read from Yahoo's own trading-period data so holidays and early
// closes are handled without a calendar here.
function marketState(quotes) {
  const q = quotes.find(x => x.regular && x.regular.start && x.regular.end);
  if (!q) return { open: false, start: null, end: null };
  const now = Math.floor(Date.now() / 1000);
  return { open: now >= q.regular.start && now < q.regular.end, start: q.regular.start, end: q.regular.end };
}

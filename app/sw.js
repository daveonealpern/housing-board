// Service worker for the Housing Signal Board phone app.
//
// The app's own code and data (the page, the shared summary script and
// data.json): network first, asking the server whether the copy changed, so an
// update shows on the very next launch whenever there is a connection; the
// last copy seen is used when there is no signal.
// Icons, logo and fonts: served from the cache straight away and refreshed in
// the background, since they rarely change.
// Bump VERSION whenever the app changes, so phones drop the old copies.
const VERSION = 'hsb-app-v10';
const SHELL = [
  './',
  './index.html',
  './manifest.webmanifest',
  './clc-logo.png',
  './icon-180.png',
  './icon-192.png',
  './icon-512.png',
  '../summary.js',
];

self.addEventListener('install', event => {
  // cache: 'reload' skips the browser's own HTTP cache, so a fresh install
  // never stores a copy of the page that is minutes out of date.
  event.waitUntil(
    caches.open(VERSION)
      .then(c => c.addAll(SHELL.map(u => new Request(u, {cache: 'reload'}))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function networkFirst(req, key) {
  return fetch(req, {cache: 'no-cache'}).then(res => {
    if (res.ok) {
      const copy = res.clone();
      caches.open(VERSION).then(c => c.put(key, copy));
    }
    return res;
  }).catch(() => caches.open(VERSION).then(c => c.match(key)).then(hit => hit || caches.match(key)));
}

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  const sameOrigin = url.origin === self.location.origin;

  if (sameOrigin && (req.mode === 'navigate' || /\/app\/(index\.html)?$/.test(url.pathname))) {
    event.respondWith(networkFirst(req, './index.html'));
    return;
  }
  if (sameOrigin && (url.pathname.endsWith('/data.json') || url.pathname.endsWith('/summary.js'))) {
    event.respondWith(networkFirst(req, url.pathname));
    return;
  }

  const isFont = url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com';
  if (!sameOrigin && !isFont) return;

  event.respondWith(
    caches.open(VERSION).then(cache =>
      cache.match(req).then(hit => {
        const fresh = fetch(req).then(res => {
          if (res.ok || res.type === 'opaque') cache.put(req, res.clone());
          return res;
        }).catch(() => hit);
        return hit || fresh;
      })
    )
  );
});

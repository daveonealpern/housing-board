// Service worker for the Housing Signal Board phone app.
//
// data.json: network first, so the app always shows the newest run when there
// is a connection, falling back to the last copy it saw when there is not.
// Everything else (this app's page, icons, logo, the shared summary script,
// fonts): served from the cache straight away and refreshed in the background,
// so the app opens instantly and picks up a new version on the next launch.
// Bump VERSION when the list of app files changes.
const VERSION = 'hsb-app-v1';
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
  event.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  if (url.pathname.endsWith('/data.json')) {
    event.respondWith(
      fetch(req).then(res => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(VERSION).then(c => c.put(url.pathname, copy));
        }
        return res;
      }).catch(() => caches.match(url.pathname))
    );
    return;
  }

  const sameOrigin = url.origin === self.location.origin;
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

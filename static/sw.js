/* Multi-page service worker. The pcd-v1 cache name below is rewritten per
   deploy by App.py's /sw.js route, so clients bust stale assets automatically. */

const CACHE = 'pcd-v1';
const ASSETS = [
  '/static/css/app.css',
  '/static/js/common.js',
  '/static/js/deals.js',
  '/static/js/deal.js',
  '/static/js/outcomes.js',
  '/static/js/prices.js',
  '/static/js/model.js',
  '/static/js/predictions.js',
  '/static/js/nearmiss.js',
  '/static/js/settings.js',
  '/static/js/health.js',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;

  // Static assets: cache-first (they're versioned by the cache name).
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      caches.match(e.request).then(hit => hit || fetch(e.request).then(resp => {
        const copy = resp.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return resp;
      }))
    );
    return;
  }

  // Pages + APIs: network-first, caching only page navigations as an
  // offline fallback (never APIs — stale data is worse than an error).
  e.respondWith(
    fetch(e.request).then(resp => {
      if (e.request.mode === 'navigate' && resp.ok) {
        const copy = resp.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
      }
      return resp;
    }).catch(() =>
      e.request.mode === 'navigate'
        ? caches.match(e.request)
        : Response.error()
    )
  );
});

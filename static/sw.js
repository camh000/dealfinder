const CACHE = 'pcd-v1';

self.addEventListener('install', e => {
    e.waitUntil(caches.open(CACHE).then(c => c.add('/')));
    self.skipWaiting();
});

self.addEventListener('activate', e => {
    e.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
        )
    );
    self.clients.claim();
});

self.addEventListener('fetch', e => {
    // API calls always go to the network — never serve stale deal data
    if (new URL(e.request.url).pathname.startsWith('/api/')) return;

    // Everything else: network-first, fall back to cache
    e.respondWith(
        fetch(e.request)
            .then(resp => {
                if (resp.ok) {
                    const clone = resp.clone();
                    caches.open(CACHE).then(c => c.put(e.request, clone));
                }
                return resp;
            })
            .catch(() => caches.match(e.request))
    );
});

// bbwatch's service worker: keeps the page shell (index.html) so the dashboard opens at once even when
// the network is slow — the cached copy is served immediately and refreshed in the background, so a new
// version shows on the following visit. Data is not cached here: the page caches and refreshes it itself.
const SHELL = "bbwatch-shell-v1";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

self.addEventListener("fetch", event => {
  const req = event.request;
  if (req.method !== "GET" || req.mode !== "navigate") return;
  event.respondWith(caches.open(SHELL).then(async cache => {
    const cached = await cache.match(req, {ignoreSearch: true});
    const fresh = fetch(req).then(res => {
      if (res.ok) cache.put(req, res.clone());
      return res;
    }).catch(() => cached);
    return cached || fresh;
  }));
});

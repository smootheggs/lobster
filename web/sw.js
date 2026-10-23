// Keeps the app's own files on the phone so it opens instantly.
// Your data (/api, /library) is never cached: it always comes fresh from the tablet.
const CACHE = "lobster-shell-17";
const SHELL = ["/", "/app.js", "/style.css", "/manifest.webmanifest", "/icon.svg", "/icon-192.png", "/sloth-lobster.svg", "/ripple.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/library/")) return;
  // Shell files: answer from the cache, refresh it in the background.
  e.respondWith(caches.open(CACHE).then(async (cache) => {
    // Opening the app (also from the Share menu, /share?...) works offline from the cached page.
    const cached = await cache.match(e.request.mode === "navigate" ? "/" : e.request);
    const fresh = fetch(e.request).then((r) => { if (r.ok) cache.put(e.request, r.clone()); return r; })
      .catch(() => cached);
    return cached || fresh;
  }));
});

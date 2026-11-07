/* ishogram offline app-shell service worker */
const CACHE_NAME = "ishogram-shell-v1";
const APP_SHELL = [
  "/",
  "/manifest.json",
  "/vendor/socket.io.min.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-512-maskable.png"
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);

  // Never cache authenticated API traffic or Socket.IO transport requests.
  if (url.origin !== self.location.origin ||
      url.pathname.startsWith("/api/") ||
      url.pathname.startsWith("/socket.io/") ||
      request.method !== "GET") {
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).then(response => {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put("/", copy));
        }
        return response;
      }).catch(async () => {
        const cached = await caches.match("/");
        return cached || new Response(
          "<!doctype html><title>ishogram offline</title><meta name='viewport' content='width=device-width,initial-scale=1'><body style='background:#0e1621;color:white;font:16px Arial;padding:2rem'><h2>ishogram is offline</h2><p>Reconnect to the internet and reload the app.</p></body>",
          {headers: {"Content-Type": "text/html; charset=utf-8"}}
        );
      })
    );
    return;
  }

  // Static shell assets: cache-first, then network.
  if (url.pathname === "/manifest.json" ||
      url.pathname.startsWith("/static/icons/") ||
      url.pathname.startsWith("/vendor/")) {
    event.respondWith(
      caches.match(request).then(cached => cached || fetch(request).then(response => {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(request, copy));
        }
        return response;
      }))
    );
  }
});

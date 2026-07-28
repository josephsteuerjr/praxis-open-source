const SHELL_CACHE = "praxis-shell-pass30-v2";
const APP_SHELL = "/app";
const SHELL_ASSETS = [
  APP_SHELL,
  "/app/static/app.css?v=30",
  "/app/static/app.js?v=30",
  "/app/static/ambient.js",
  "/app/static/manifest.webmanifest",
  "/app/static/icon.svg",
  "/app/static/icon-192.png",
  "/app/static/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    await Promise.all(
      SHELL_ASSETS.map(async (path) => {
        const response = await fetch(path, { cache: "reload", credentials: "same-origin" });
        if (!response.ok) throw new Error(`shell ${path}: ${response.status}`);
        await cache.put(path, response);
      }),
    );
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys
      .filter((key) => key.startsWith("praxis-shell-") && key !== SHELL_CACHE)
      .map((key) => caches.delete(key)));
    await self.clients.claim();
  })());
});

async function networkFirst(request, fallback) {
  const cache = await caches.open(SHELL_CACHE);
  try {
    const response = await fetch(request);
    if (response.ok) await cache.put(fallback, response.clone());
    return response;
  } catch (error) {
    const cached = await cache.match(fallback);
    if (cached) return cached;
    throw error;
  }
}

async function shellAsset(request, event) {
  const cache = await caches.open(SHELL_CACHE);
  const cached = await cache.match(request);
  const update = fetch(request, { cache: "no-cache" })
    .then(async (response) => {
      if (response.ok) await cache.put(request, response.clone());
      return response;
    });
  if (cached) {
    event.waitUntil(update.then(() => undefined).catch(() => {}));
    return cached;
  }
  return update;
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Authenticated API data is deliberately never cached here. The page stores
  // one partitioned, successfully verified snapshot in IndexedDB instead.
  if (url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate" && (url.pathname === "/app" || url.pathname === "/app/")) {
    event.respondWith(networkFirst(request, APP_SHELL));
    return;
  }
  if (url.pathname.startsWith("/app/static/")) {
    event.respondWith(shellAsset(request, event));
  }
});

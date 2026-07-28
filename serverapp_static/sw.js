/* ATLAS observatory service worker — offline shell, network-first for data. */
const SHELL = "atlas-shell-v5";
const ASSETS = ["/", "/s/atlas.css?v=5", "/s/atlas.js?v=5", "/manifest.webmanifest",
  "/s/icon-192.png", "/s/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil((async () => {
    const c = await caches.open(SHELL);
    await Promise.allSettled(ASSETS.map(async (p) => {
      try { const r = await fetch(p, { cache: "reload" }); if (r.ok) await c.put(p, r); } catch (_) {}
    }));
    self.skipWaiting();
  })());
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k.startsWith("atlas-shell-") && k !== SHELL).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  // never cache API or auth — always live
  if (url.pathname.startsWith("/api/")) return;
  // shell: network-first, fall back to cache (so an offline PWA still opens)
  e.respondWith((async () => {
    try {
      const r = await fetch(req);
      if (r.ok && (url.pathname === "/" || url.pathname.startsWith("/s/") || url.pathname === "/manifest.webmanifest")) {
        const c = await caches.open(SHELL); c.put(req, r.clone());
      }
      return r;
    } catch (_) {
      const c = await caches.open(SHELL);
      return (await c.match(req)) || (await c.match("/")) || Response.error();
    }
  })());
});

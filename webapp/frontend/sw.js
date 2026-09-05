// Minimal service worker - exists only so the browser considers this
// installable as a PWA. No offline caching: staging changes constantly and a
// stale cached deck would be actively misleading.
self.addEventListener("install", (e) => self.skipWaiting());
self.addEventListener("activate", (e) => self.clients.claim());
self.addEventListener("fetch", () => {});

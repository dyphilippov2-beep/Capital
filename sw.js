/* Мой капитал: the app opens without the network; prices and rates always go to the network */
const CACHE = "mycap-v1";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icon.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  /* The app itself: newest version when online, the saved copy when not */
  if (url.origin === location.origin){
    e.respondWith(fetch(req).then(r => {
      if (r.ok){ const copy = r.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
      return r;
    }).catch(() => caches.match(req, {ignoreSearch: true}).then(r => r || caches.match("./index.html"))));
    return;
  }
  /* Fonts and the text-recognition library never change at a given address: keep them */
  if (/(^|\.)fonts\.(googleapis|gstatic)\.com$|^cdn\.jsdelivr\.net$/.test(url.hostname)){
    e.respondWith(caches.match(req).then(hit => hit || fetch(req).then(r => {
      if (r.ok || r.type === "opaque"){ const copy = r.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
      return r;
    })));
  }
});

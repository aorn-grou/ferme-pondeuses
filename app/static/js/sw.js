/* Service worker : rend le logiciel installable (icône sur l'écran) et affiche une page claire sans internet.
   Les pages avec vos données ne sont jamais gardées en mémoire : seules les images, styles et scripts le sont. */
const CACHE = "ferme-static-v1";
const OFFLINE = "/hors-ligne";
const PRECACHE = [OFFLINE, "/static/img/app-192.png", "/static/img/androfia-logo.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (req.mode === "navigate") {
    // pages : toujours depuis internet ; sans connexion, la page « hors ligne »
    e.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    // styles, scripts, images : la copie gardée, mise à jour en arrière-plan
    e.respondWith(caches.open(CACHE).then((c) => c.match(req).then((hit) => {
      const net = fetch(req).then((res) => { if (res.ok) c.put(req, res.clone()); return res; }).catch(() => hit);
      return hit || net;
    })));
  }
});

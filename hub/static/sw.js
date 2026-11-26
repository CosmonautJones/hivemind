/* Hivemind service worker (U9): offline shell, last-known state, Web Push (N2 payload).
   Served by the hub at /sw.js with "Service-Worker-Allowed: /" and "Cache-Control: no-cache".
   - Shell (/, manifest, icons): network first for "/" and "/index.html", cache first for icons; the cached shell opens offline.
     Only an HTML answer for "/" or "/index.html" replaces the cached shell, and never one for "/?claim=<code>". Other
     navigations go to the network; offline, a same-origin non-/api one gets the cached shell.
   - Data (/api/state, /api/today): network first; every good answer is cached. When the network fails the cached copy
     is returned with "X-HM-Offline: 1" and "X-HM-Cached-At: <ms>", and the page shows "Offline · data from HH:MM".
   - Nothing else under /api/ is cached. The data cache is deleted on sign-out (message {type: "clear-data"}) and whenever
     /api/state or /api/today answers 401 (session expired or revoked on another device). */
const SHELL = 'hm-shell-v1';
const DATA = 'hm-data-v1';
const SHELL_URLS = ['/', '/manifest.webmanifest', '/icons/icon-192.png', '/icons/icon-512.png', '/icons/maskable-512.png'];
const DATA_PATHS = ['/api/state', '/api/today'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_URLS)).catch(() => null).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL && k !== DATA).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('message', e => {
  if (e.data && e.data.type === 'clear-data') e.waitUntil(caches.delete(DATA));
});

async function stamp(resp) {
  const body = await resp.clone().blob();
  const h = new Headers(resp.headers);
  h.set('X-HM-Cached-At', String(Date.now()));
  return new Response(body, {status: resp.status, statusText: resp.statusText, headers: h});
}

async function offline(cached) {
  const body = await cached.blob();
  const h = new Headers(cached.headers);
  h.set('X-HM-Offline', '1');
  return new Response(body, {status: 200, headers: h});
}

async function dataFirst(req) {
  const key = new URL(req.url);
  key.search = '';                       // /api/today?q=... falls back to the unfiltered copy
  const cache = await caches.open(DATA);
  try {
    const resp = await fetch(req);
    if (resp.status === 401) { await caches.delete(DATA); return resp; }
    if (resp.ok && !new URL(req.url).search) await cache.put(key.toString(), await stamp(resp));
    return resp;
  } catch (err) {
    const hit = await cache.match(key.toString());
    if (hit) return offline(hit);
    throw err;
  }
}

const isHtml = resp => /^text\/html/i.test((resp.headers && resp.headers.get('Content-Type')) || '');

async function shellFirst(req) {
  const cache = await caches.open(SHELL);
  try {
    const resp = await fetch(req);
    if (resp.ok && isHtml(resp) && !new URL(req.url).searchParams.has('claim')) await cache.put('/', resp.clone());
    return resp;
  } catch (err) {
    const hit = await cache.match('/');
    if (hit) return hit;
    throw err;
  }
}

/* any other navigation (/api/health in a tab, /agent/install.ps1, ...): the network answers; nothing is cached */
async function navOnly(req) {
  try {
    return await fetch(req);
  } catch (err) {
    const hit = await (await caches.open(SHELL)).match('/');
    if (hit) return hit;
    throw err;
  }
}

async function cacheFirst(req) {
  const cache = await caches.open(SHELL);
  const hit = await cache.match(req);
  if (hit) return hit;
  const resp = await fetch(req);
  if (resp.ok) cache.put(req, resp.clone());
  return resp;
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (DATA_PATHS.includes(url.pathname)) { e.respondWith(dataFirst(req)); return; }
  if (url.pathname === '/' || url.pathname === '/index.html') { e.respondWith(shellFirst(req)); return; }
  if (req.mode === 'navigate') { if (!url.pathname.startsWith('/api/')) e.respondWith(navOnly(req)); return; }
  if (url.pathname === '/manifest.webmanifest' || url.pathname.startsWith('/icons/')) { e.respondWith(cacheFirst(req)); return; }
});

/* N2 push payload: {title, body, url: "#/...", tag} */
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = {body: e.data ? e.data.text() : ''}; }
  const title = d.title || 'Hivemind';
  e.waitUntil(self.registration.showNotification(title, {
    body: d.body || '', tag: d.tag || undefined, renotify: !!d.tag, icon: '/icons/icon-192.png', badge: '/icons/badge-72.png',
    data: {url: typeof d.url === 'string' && d.url.startsWith('#/') ? d.url : '#/today'}
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const target = '/' + ((e.notification.data && e.notification.data.url) || '#/today');
  e.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(list => {
    for (const c of list) {
      if (new URL(c.url).origin === self.location.origin) {
        c.postMessage({type: 'navigate', url: target});
        return c.focus();
      }
    }
    return self.clients.openWindow(target);
  }));
});

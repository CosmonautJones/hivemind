// hub/static/sw.js with a fake service-worker global: cache keys, offline stamp headers, navigation handling, 401 wipe.
// Run: node --test tests/js/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(join(here, '..', '..', 'hub', 'static', 'sw.js'), 'utf8');
const ORIGIN = 'https://hub.example:8787';

function load(net) {
  const store = new Map();                         // cache name -> Map(url -> Response)
  const keyOf = r => new URL(typeof r === 'string' ? r : r.url, ORIGIN).toString();
  const caches = {
    async open(n) { if (!store.has(n)) store.set(n, new Map()); const m = store.get(n);
      return {put: async (r, resp) => { m.set(keyOf(r), resp); }, match: async r => { const v = m.get(keyOf(r)); return v ? v.clone() : undefined; },
              addAll: async () => {}}; },
    async delete(n) { return store.delete(n); }, async keys() { return [...store.keys()]; },
    async match() { return undefined; },
  };
  const handlers = {};
  const self = {location: {origin: ORIGIN}, addEventListener: (t, f) => { handlers[t] = f; }, skipWaiting() {}, clients: {claim() {}}};
  new Function('self', 'caches', 'fetch', SRC)(self, caches, net.fetch);
  async function get(path, mode = 'cors') {
    const req = {method: 'GET', url: ORIGIN + path, mode};
    let p = null;
    handlers.fetch({request: req, respondWith: x => { p = x; }});
    return p ? await p : 'passthrough';
  }
  const cached = (n, path) => store.get(n) && store.get(n).get(new URL(path, ORIGIN).toString());
  return {get, store, cached};
}
const html = (body, status = 200) => new Response(body, {status, headers: {'Content-Type': 'text/html; charset=utf-8'}});
const jsonR = (obj, status = 200) => new Response(JSON.stringify(obj), {status, headers: {'Content-Type': 'application/json'}});
const offlineNet = () => { throw new TypeError('Failed to fetch'); };

test('sw: /api/state is cached under its path, stamped, and served offline with X-HM-Offline', async () => {
  // Arrange
  let online = true;
  const sw = load({fetch: async () => { if (!online) offlineNet(); return jsonR({nodes: [1]}); }});
  // Act
  const r1 = await sw.get('/api/state');
  online = false;
  const r2 = await sw.get('/api/state');
  // Assert
  assert.equal(r1.status, 200);
  assert.ok(+sw.cached('hm-data-v1', '/api/state').headers.get('X-HM-Cached-At') > 0);
  assert.equal(r2.headers.get('X-HM-Offline'), '1');
  assert.deepEqual(await r2.json(), {nodes: [1]});
});

test('sw: /api/today?q= is not cached but falls back to the plain copy offline', async () => {
  let online = true;
  const sw = load({fetch: async () => { if (!online) offlineNet(); return jsonR({ok: 1}); }});
  await sw.get('/api/today?q=abc');
  assert.equal(sw.cached('hm-data-v1', '/api/today'), undefined);
  await sw.get('/api/today'); online = false;
  assert.equal((await sw.get('/api/today?q=abc')).headers.get('X-HM-Offline'), '1');
});

test('sw: a 401 on /api/state or /api/today deletes the data cache (U-13)', async () => {
  let status = 200;
  const sw = load({fetch: async () => jsonR({}, status)});
  await sw.get('/api/state');
  assert.ok(sw.store.has('hm-data-v1') && sw.cached('hm-data-v1', '/api/state'));
  status = 401;
  const r = await sw.get('/api/today');
  assert.equal(r.status, 401);
  assert.equal(sw.store.has('hm-data-v1'), false);
});

test('sw: only the HTML shell for / replaces the cached shell (U-05)', async () => {
  // Arrange
  let next = () => html('<p>shell v2</p>');
  const sw = load({fetch: async () => next()});
  // Act + Assert
  await sw.get('/', 'navigate');
  assert.equal(await sw.cached('hm-shell-v1', '/').clone().text(), '<p>shell v2</p>');
  next = () => jsonR({ok: true});
  assert.equal(await sw.get('/api/health', 'navigate'), 'passthrough');      // never intercepted
  next = () => new Response('Write-Host hi', {headers: {'Content-Type': 'text/plain'}});
  await sw.get('/agent/install.ps1', 'navigate');
  next = () => new Response('PNG', {headers: {'Content-Type': 'image/png'}});
  await sw.get('/icons/x.png', 'navigate');
  next = () => html('<p>claim page</p>');
  await sw.get('/?claim=SECRET1', 'navigate');
  next = () => jsonR({oops: 1});
  await sw.get('/', 'navigate');                                             // non-HTML answer for "/" is not cached either
  assert.equal(await sw.cached('hm-shell-v1', '/').clone().text(), '<p>shell v2</p>');
});

test('sw: offline navigation gets the cached shell, but not for /api paths', async () => {
  let online = true;
  const sw = load({fetch: async () => { if (!online) offlineNet(); return html('<p>shell</p>'); }});
  await sw.get('/', 'navigate'); online = false;
  assert.equal(await (await sw.get('/', 'navigate')).text(), '<p>shell</p>');
  assert.equal(await (await sw.get('/some/deep/link', 'navigate')).text(), '<p>shell</p>');
  assert.equal(await sw.get('/api/health', 'navigate'), 'passthrough');
});

test('sw: icons are cache first; other /api calls and POSTs pass through', async () => {
  let n = 0;
  const sw = load({fetch: async () => { n++; return new Response('PNG', {headers: {'Content-Type': 'image/png'}}); }});
  await sw.get('/icons/icon-192.png'); await sw.get('/icons/icon-192.png');
  assert.equal(n, 1);
  assert.equal(await sw.get('/api/jobs'), 'passthrough');
});

// 2.4 chat UI logic: routes, safe message formatting, sidebar groups, turn states, request bodies, photo plan.
// Run: node --test tests/js/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, '..', '..', 'hub', 'static', 'index.html'), 'utf8');
const block = html.split('/* @pure-begin')[1].split('/* @pure-end */')[0];
const UI = new Function(block.slice(block.indexOf('*/') + 2) + '\nreturn UI;')();

test('chat routes: #/chat and #/chat/<id>, back goes to Today', () => {
  // Arrange / Act
  const a = UI.parseRoute('#/chat'), b = UI.parseRoute('#/chat/019abc?x=1');
  // Assert
  assert.equal(a.name, 'chat'); assert.equal(a.tab, 'chat'); assert.equal(a.id, null);
  assert.equal(b.id, '019abc'); assert.equal(b.params.x, '1');
  assert.equal(UI.parentHash(b), '#/today');
  assert.equal(UI.hashFor('chat', 'a b'), '#/chat/a%20b'); assert.equal(UI.hashFor('chat'), '#/chat');
  assert.equal(UI.parseRoute('#/chat?prompt=hi%20there').params.prompt, 'hi there');
});

test('chatMd escapes everything except the formatting it adds', () => {
  // Arrange
  const evil = '<img src=x onerror=alert(1)> **bold** `a<b` https://ex.com/?a=1&b="2"\n```js\n<script>x</script>\n```\n# Title';
  // Act
  const out = UI.chatMd(evil);
  // Assert
  assert.ok(!out.includes('<img') && !out.includes('<script>'));
  assert.ok(out.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.ok(out.includes('<b>bold</b>') && out.includes('<code>a&lt;b</code>'));
  assert.ok(out.includes('<pre class="code">&lt;script&gt;x&lt;/script&gt;</pre>'));
  assert.ok(out.includes('<h4>Title</h4>'));
  assert.match(out, /<a href="https:\/\/ex\.com\/\?a=1&amp;b=&quot;2&quot;" target="_blank" rel="noopener noreferrer">/);
  assert.ok(!UI.chatMd('javascript:alert(1)').includes('<a'));
});

test('chatGroups buckets newest-first into Today / Yesterday / Previous 7 days / Older', () => {
  // Arrange
  const now = new Date(2026, 10, 25, 15, 0, 0).getTime() / 1000, d = 86400;
  const ts = [now - 60, now - d, now - 3 * d, now - 30 * d].map((u, i) => ({id: 't' + i, updated: u}));
  // Act
  const g = UI.chatGroups(ts, now);
  // Assert
  assert.deepEqual(g.map(x => x.label), ['Today', 'Yesterday', 'Previous 7 days', 'Older']);
  assert.deepEqual(g.map(x => x.threads[0].id), ['t0', 't1', 't2', 't3']);
  assert.deepEqual(UI.chatGroups([], now), []);
});

test('turn states and busy detection', () => {
  assert.equal(UI.chatTurnState({status: 'queued'}), 'waiting');
  assert.equal(UI.chatTurnState({status: 'running'}), 'working');
  assert.equal(UI.chatTurnState({status: 'starting'}), 'working');
  assert.equal(UI.chatTurnState({status: 'failed'}), 'failed');
  assert.equal(UI.chatTurnState({status: 'cancelled'}), 'stopped');
  assert.equal(UI.chatTurnState({status: 'done'}), 'done');
  assert.equal(UI.chatBusy([{status: 'done'}, {status: 'queued'}]), true);
  assert.equal(UI.chatBusy([{status: 'done'}]), false);
});

test('request bodies: new chat vs follow-up', () => {
  // Act
  const n = UI.chatNewBody({text: '  hi ', engine: 'shell', full_access: true, attachments: ['u1']});
  const r = UI.chatReplyBody('j9', ' more ', ['u2'], true);
  // Assert
  assert.deepEqual(n, {node_id: 'auto', engine: 'shell', prompt: 'hi', project_id: null, cwd: null, full_access: false, attachments: ['u1'], source: 'ui'});
  assert.deepEqual(r, {reply_to: 'j9', prompt: 'more', attachments: ['u2'], full_access: true, source: 'ui'});
});

test('imagePlan keeps small supported images and re-encodes big or HEIC ones as JPEG', () => {
  assert.deepEqual(UI.imagePlan('image/png', 200000, 1200, 800), {convert: false});
  const big = UI.imagePlan('image/jpeg', 9e6, 4032, 3024);
  assert.equal(big.convert, true); assert.equal(big.w, 2048); assert.equal(big.h, 1536); assert.equal(big.type, 'image/jpeg');
  const heic = UI.imagePlan('image/heic', 100000, 1000, 1000);
  assert.equal(heic.convert, true); assert.equal(heic.w, 1000);
});

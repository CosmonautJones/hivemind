// Phase 3 (2.3) UI logic: routes, Ask fuzzy matching, Today fallback, step-up, machine rows, QR codes.
// Run: node --test tests/js/   (also run from pytest by tests/test_ui_logic_js.py when node is installed)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, '..', '..', 'hub', 'static', 'index.html'), 'utf8');
const block = html.split('/* @pure-begin')[1].split('/* @pure-end */')[0];
const UI = new Function(block.slice(block.indexOf('*/') + 2) + '\nreturn UI;')();
const json = f => JSON.parse(readFileSync(join(here, f), 'utf8'));

test('routes: every U10 hash parses to its view', () => {
  // Arrange
  const cases = {
    '#/today': ['today', 'today'], '#/projects': ['projects', 'projects'], '#/project/p1': ['project', 'projects'],
    '#/session/claude/abc': ['session', 'projects'], '#/machines': ['machines', 'machines'], '#/machine/forge-01': ['machine', 'machines'],
    '#/machines/maintenance': ['maintenance', 'machines'], '#/machines/advanced': ['advanced', 'machines'], '#/activity': ['activity', 'activity'],
    '#/task/019a': ['task', 'activity'], '#/settings': ['settings', null], '#/add-machine': ['add-machine', 'machines']};
  for (const [h, [name, tab]] of Object.entries(cases)) {
    // Act
    const r = UI.parseRoute(h);
    // Assert
    assert.equal(r.name, name, h); assert.equal(r.tab, tab, h); assert.equal(r.legacy, false, h);
  }
});

test('routes: ids, params and encoded parts', () => {
  const r = UI.parseRoute('#/session/claude/a%2Fb?p=p%201&x');
  assert.equal(r.tool, 'claude'); assert.equal(r.id, 'a/b'); assert.deepEqual(r.params, {p: 'p 1', x: ''});
  assert.equal(UI.parseRoute('#/machines/maintenance?action=free_disk').params.action, 'free_disk');
  assert.equal(UI.hashFor('session', 'claude', 'a/b'), '#/session/claude/a%2Fb');
  assert.equal(UI.hashFor('machine', 'forge 1'), '#/machine/forge%201');
});

test('routes: legacy 2.2 hashes map, unknown ones fall back to Today', () => {
  assert.deepEqual(['#fleet', '#jobs', '#health', '#projects'].map(h => UI.parseRoute(h).name), ['machines', 'activity', 'today', 'projects']);
  assert.ok(UI.parseRoute('#fleet').legacy);
  assert.equal(UI.parseRoute('').name, 'today');
  assert.equal(UI.parseRoute('#/nope').name, 'today');
  assert.ok(UI.parseRoute('#/nope').fallback);
});

test('routes: back goes to the parent view', () => {
  assert.equal(UI.parentHash(UI.parseRoute('#/session/claude/x'), {project_id: 'p1'}), '#/project/p1');
  assert.equal(UI.parentHash(UI.parseRoute('#/session/claude/x'), {}), '#/projects');
  assert.equal(UI.parentHash(UI.parseRoute('#/task/1')), '#/activity');
  assert.equal(UI.parentHash(UI.parseRoute('#/machines/advanced')), '#/machines');
  assert.equal(UI.baseView(UI.parseRoute('#/machine/x')), 'machines');
});

test('fuzzy: JS and Python (hub/forge_ui.fuzzy_score) agree on the shared cases', () => {
  for (const [q, t, want] of json('fuzzy_cases.json').cases) assert.equal(UI.fuzzyScore(q, t), want, `${q} vs ${t}`);
});

test('ask (local fallback): best match first, the run entry is always last, nothing runs', () => {
  // Arrange
  const items = [{type: 'project', id: 'p1', title: 'forge-console', fields: ['Studio PC'], target: {project_id: 'p1'}},
                 {type: 'machine', id: 'forge-01', title: 'forge-01', fields: [], target: {node_id: 'forge-01'}},
                 {type: 'action', id: 'x', title: 'Settings', fields: ['theme'], target: {route: '#/settings'}}];
  // Act
  const r = UI.askLocal('forge-c', items, 8);
  // Assert
  assert.equal(r[0].id, 'p1');
  assert.equal(r[r.length - 1].type, 'run');
  assert.deepEqual(r[r.length - 1].target, {prompt: 'forge-c', engine: 'any', node_id: 'auto'});
  assert.ok(!('fields' in r[0]));
  assert.deepEqual(UI.askLocal('   ', items), []);
  assert.equal(UI.askLocal('e', items, 2).length, 2);
  assert.ok(UI.runEntry('x'.repeat(100)).title.length < 100);
});

test('today fallback (2.2 leader): needs you from health, running jobs, first run counts', () => {
  // Arrange
  const state = {nodes: [{id: 'a', online: true}, {id: 'b', online: false}],
                 jobs: [{id: 'j1', status: 'running', engine: 'claude', prompt: 'p', node_id: 'a'}, {id: 'j2', status: 'done'}, {id: 'j3', status: 'queued', source: 'brief'}]};
  const health = {alerts: [{level: 'warn', kind: 'disk_full', node_id: 'a'}, {level: 'error', kind: 'node_offline', node_id: 'b'},
                           {level: 'info', kind: 'bundle'}, {level: 'warn', kind: 'version_behind', node_id: 'a'}], update_lease: {holder: 'a'}};
  // Act
  const t = UI.todayFallback(state, health, [{id: 'p'}], 1000);
  // Assert
  assert.equal(t.needs_you.total, 2);                                        // info dropped, version_behind heals itself during a rollout
  assert.equal(t.needs_you.items[0].kind, 'node_offline');                   // errors first
  assert.deepEqual(t.needs_you.items[0].action, {label: 'Wake it', kind: 'wake_machine', target: {node_id: 'b'}});
  assert.deepEqual(t.happening.map(h => h.id), ['j1']);
  assert.deepEqual(t.first_run, {machines: 2, projects: 1});
  assert.equal(t.status.level, 'error');
  assert.ok(t.legacy);
});

test('today sentence', () => {
  assert.equal(UI.todaySentence(0, 0, []).sentence, 'No machines yet. Add one to get started.');
  assert.equal(UI.todaySentence(4, 3, []).sentence, 'All good – 3 of 4 machines on');
  assert.equal(UI.todaySentence(1, 1, []).sentence, 'All good – 1 of 1 machine on');
  assert.deepEqual(UI.todaySentence(2, 2, [{level: 'warn'}]), {level: 'warn', sentence: '1 thing needs you'});
  assert.equal(UI.todaySentence(2, 2, [{level: 'warn'}, {level: 'error'}]).level, 'error');
});

test('day buckets use the local calendar day', () => {
  const now = new Date(2026, 10, 25, 9, 0).getTime() / 1000;
  assert.equal(UI.dayBucket(new Date(2026, 10, 25, 0, 5).getTime() / 1000, now), 'today');
  assert.equal(UI.dayBucket(new Date(2026, 10, 24, 23, 59).getTime() / 1000, now), 'yesterday');
  assert.equal(UI.dayBucket(new Date(2026, 10, 23, 12, 0).getTime() / 1000, now), 'earlier');
  assert.equal(UI.dayBucket(null, now), 'earlier');
});

test('alert actions mirror the hub defaults', () => {
  const own = {label: 'Resume', kind: 'resume_session', target: {tool: 'claude', session_id: 's'}};
  assert.deepEqual(UI.alertAction({kind: 'x', action: own}), own);
  assert.equal(UI.alertAction({kind: 'task_failed', data: {job_id: 'j'}}).kind, 'retry_job');
  assert.equal(UI.alertAction({kind: 'repo_conflict', data: {project_id: 'p'}}).kind, 'open_project');
  assert.equal(UI.alertAction({kind: 'fleet_bad_version'}).target.route, '#/machines/advanced');
  assert.equal(UI.alertAction({kind: 'disk_full', node_id: 'n'}).kind, 'open_machine');
  assert.equal(UI.alertAction({kind: 'weird'}), null);
});

test('step-up: contract body and FastAPI-wrapped body', () => {
  assert.equal(UI.stepUpKind({detail: 'admin PIN required', step_up: 'required'}), 'required');
  assert.equal(UI.stepUpKind({detail: {detail: 'admin PIN not set', step_up: 'setup'}}), 'setup');
  assert.equal(UI.stepUpKind({detail: 'forbidden'}), null);
  assert.equal(UI.stepUpKind(null), null);
  assert.equal(UI.errText({detail: {detail: 'admin PIN not set'}}), 'admin PIN not set');
  assert.ok(UI.pinOk('012345')); assert.ok(!UI.pinOk('12345')); assert.ok(!UI.pinOk('12345a'));
});

test('run a task: "Started on <machine>" message', () => {
  const name = id => ({n3: 'forge-03'})[id] || id;
  assert.equal(UI.startedMsg({id: 'j', node_id: 'n3', status: 'queued'}, name), 'Started on forge-03');
  assert.equal(UI.startedMsg({id: 'j', node_id: 'auto', status: 'queued'}, name), 'Queued · waiting for a free machine');
  assert.equal(UI.startedMsg({id: 'j', node_id: 'auto', wait_reason: 'claude cooling down'}, name), 'Queued · claude cooling down');
  assert.equal(UI.orderText(['n3', 'b'], name), 'One at a time: forge-03 → b');
  assert.equal(UI.orderText([], name), 'No machine can run this right now.');
});

test('machine rows: one line + at most one warning', () => {
  const base = {online: true, active_jobs: 0, metrics: {}, info: {agent_version: '2.3.0'}, system_lane: {online: true}};
  assert.deepEqual(UI.machineSummary(base, {target: '2.3.0'}), {line: 'Idle', warn: null, level: 'idle'});
  assert.equal(UI.machineSummary({...base, active_jobs: 2}).line, 'Running 2 tasks');
  assert.equal(UI.machineSummary({...base, disk_pct: 93, system_lane: {online: false}}).warn, 'Disk 93% full');
  assert.equal(UI.machineSummary({...base, metrics: {services: [{name: 'ollama', ok: false, state: 'starting'}]}}).warn, null);
  assert.equal(UI.machineSummary({...base, metrics: {services: [{name: 'ollama', ok: false}]}}).warn, 'ollama not responding');
  assert.equal(UI.machineSummary({...base, metrics: {job_token: 'full'}}).warn, 'Tasks run with full admin rights');
  assert.equal(UI.machineSummary(base, {target: '2.4.0', rolling: true}).warn, 'Updating…');
  assert.equal(UI.machineSummary({online: false, last_seen: 1000}, {now: 1000 + 7200}).line, 'Offline · seen 2h ago');
});

test('live output: only chunks after the last seen seq', () => {
  const r = {chunks: [{seq: 3, text: 'c'}, {seq: 1, text: 'a'}, {seq: 2, text: 'b'}], status: 'running', truncated: true};
  assert.deepEqual(UI.outputStep(1, r), {text: 'bc', seq: 3, status: 'running', truncated: true});
  assert.deepEqual(UI.outputStep(null, r).text, 'abc');
  assert.equal(UI.outputStep(5, {chunks: []}).seq, 5);
});

test('enrollment states, device labels, topics, VAPID key bytes', () => {
  assert.equal(UI.enrollView({state: 'pending'}).text, 'Waiting for it to appear…');
  assert.ok(UI.enrollView({state: 'joined', node_id: 'n'}).done);
  assert.ok(UI.enrollView({state: 'redeemed', kind: 'device'}).done);
  assert.ok(!UI.enrollView({state: 'redeemed', kind: 'machine'}).done);
  assert.ok(UI.enrollView({state: 'expired'}).expired);
  assert.equal(UI.deviceLabel('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit Version/17 Mobile Safari/604.1'), 'Safari on iPhone');
  assert.equal(UI.deviceLabel('Mozilla/5.0 (Windows NT 10.0) Chrome/120 Safari/537.36 Edg/120'), 'Edge on Windows');
  assert.ok(UI.topicOk('hm-abcdefgh')); assert.ok(!UI.topicOk('short')); assert.ok(!UI.topicOk('has space here'));
  assert.deepEqual([...UI.b64urlBytes('-_8')], [251, 255]);
  assert.match(UI.offlineText(new Date(2026, 0, 1, 14, 2).getTime()), /^Offline · data from 14:02$/);
});

test('QR: matches the reference encoder module for module (forced masks, versions 3-10, UTF-8)', () => {
  for (const c of json('qr_fixtures.json').cases) {
    // Act
    const q = UI.qrMatrix(c.text, {mask: c.mask});
    // Assert
    assert.equal(q.version, c.version, c.text.slice(0, 20));
    assert.deepEqual(q.modules.map(r => r.join('')), c.rows, c.text.slice(0, 20));
  }
});

test('QR: automatic mask is the lowest-penalty one; too long text throws; svg has a quiet zone', () => {
  const text = 'http://forge-01.tail1234.ts.net:8787/?claim=ABCD-EFGH';
  const auto = UI.qrMatrix(text);
  for (let m = 0; m < 8; m++) assert.ok(UI.qrPenalty(auto.modules) <= UI.qrPenalty(UI.qrMatrix(text, {mask: m}).modules));
  assert.throws(() => UI.qrMatrix('x'.repeat(300)), /too long/);
  assert.match(UI.qrSvg('hi'), new RegExp(`viewBox="0 0 ${21 + 8} ${21 + 8}"`));
});

// ---------------------------------------------------------------- review fixes
test('routes: inherited Object keys in the hash do not crash parsing (U-08)', () => {
  for (const h of ['#constructor', '#toString', '#hasOwnProperty', '#__proto__', '#valueOf']) {
    const r = UI.parseRoute(h);
    assert.equal(r.name, 'today', h); assert.equal(r.legacy, false, h); assert.ok(r.fallback, h);
  }
});

test('safeHttp: only http(s) links become hrefs (U-12)', () => {
  assert.equal(UI.safeHttp('https://ntfy.sh/hm-abc'), 'https://ntfy.sh/hm-abc');
  assert.equal(UI.safeHttp(' HTTP://192.168.1.2:8787/?claim=X '), 'HTTP://192.168.1.2:8787/?claim=X');
  for (const bad of ['javascript:alert(1)', 'JavaScript:alert(1)', 'data:text/html,<b>', 'vbscript:x', '//evil.com', 'https://', 'https:///x',
                     'https://a b', 'https://x"onclick=1', null, undefined, 42])
    assert.equal(UI.safeHttp(bad), '', String(bad));
});

test('apiStepUp: nested and flat 403 bodies ask once; a retry never asks again (U-15)', () => {
  assert.deepEqual(UI.apiStepUp(403, {detail: 'admin PIN required', step_up: 'required'}, false), {kind: 'required', ask: true});
  assert.deepEqual(UI.apiStepUp(403, {detail: {detail: 'no PIN', step_up: 'setup'}}, false), {kind: 'setup', ask: true});
  assert.deepEqual(UI.apiStepUp(403, {detail: {step_up: 'required'}}, true), {kind: 'required', ask: false});
  assert.deepEqual(UI.apiStepUp(403, {detail: 'forbidden'}, false), {kind: null, ask: false});
  assert.deepEqual(UI.apiStepUp(400, {step_up: 'required'}, false), {kind: null, ask: false});
});

test('wakeError: one message per status (U-15)', () => {
  assert.match(UI.wakeError(404, true, 'nova'), /needs 2\.3/);
  assert.match(UI.wakeError(422, false, 'nova'), /^nova hasn't reported its network card/);
  assert.match(UI.wakeError(503, false, 'nova'), /No other machine on nova's network/);
  assert.equal(UI.wakeError(409, false, 'nova'), 'nova is already online.');
  assert.equal(UI.wakeError(500, false, 'nova', 'boom'), 'Could not wake it: boom');
});

test('dismissKey: same alert opening again is a new key (U-09)', () => {
  assert.equal(UI.dismissKey({id: 'a1', opened: 10}), 'a1@10');
  assert.notEqual(UI.dismissKey({id: 'a1', opened: 10}), UI.dismissKey({id: 'a1', opened: 99}));
  assert.equal(UI.dismissKey({key: 'k', opened: null}), 'k@');
});

test('newNode: a new id joins, not a bigger count (U-06)', () => {
  const base = ['a', 'b'];
  assert.equal(UI.newNode(null, [{id: 'a'}, {id: 'b'}, {id: 'c'}]), null);           // state not loaded yet: never "joined"
  assert.equal(UI.newNode(base, [{id: 'a'}, {id: 'b'}]), null);
  assert.equal(UI.newNode(base, [{id: 'b'}, {id: 'z'}, {id: 'a'}]).id, 'z');
  assert.equal(UI.newNode([], [{id: 'first'}]).id, 'first');
});

test('capText / outputDone: output is capped and polling stops when finished (U-14)', () => {
  assert.deepEqual(UI.capText('abcdef', 4), {text: 'cdef', cut: true});
  assert.deepEqual(UI.capText('ab', 4), {text: 'ab', cut: false});
  assert.equal(UI.outputDone('running', ''), false);
  assert.equal(UI.outputDone('done', 'more'), false);
  assert.equal(UI.outputDone('done', ''), true);
  assert.equal(UI.outputDone(null, ''), false);
});

test('todayFallback: all=true returns every needs item, total unchanged (U-03)', () => {
  const health = {alerts: [1, 2, 3, 4, 5].map(i => ({kind: 'disk_full', node_id: 'n' + i, level: 'warn', msg: 'x'})), rollout: []};
  const short = UI.todayFallback({nodes: [{id: 'n1', online: true}]}, health, [], 1000);
  const full = UI.todayFallback({nodes: [{id: 'n1', online: true}]}, health, [], 1000, true);
  assert.equal(short.needs_you.items.length, 3); assert.equal(full.needs_you.items.length, 5);
  assert.equal(short.needs_you.total, 5); assert.equal(full.needs_you.total, 5);
});

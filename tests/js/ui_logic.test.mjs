// Unit tests for the DOM-free logic block in hub/static/index.html (between @pure-begin and @pure-end).
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

test('transcript: first page appends and sets cursor', () => {
  // Arrange
  const r = {events: [{seq: 1, text: 'a'}, {seq: 2, text: 'b'}], cursor: 2, done: false, available: true};
  // Act
  const st = UI.transcriptStep(null, r);
  // Assert
  assert.equal(st.mode, 'append');
  assert.equal(st.cursor, 2);
  assert.equal(st.events.length, 2);
});

test('transcript: overlapping events already seen are dropped', () => {
  const st = UI.transcriptStep(2, {events: [{seq: 2}, {seq: 3}], cursor: 3});
  assert.deepEqual(st.events.map(e => e.seq), [3]);
  assert.equal(st.cursor, 3);
});

test('transcript: no new events keeps the cursor', () => {
  const st = UI.transcriptStep(5, {events: [], cursor: 5, done: false});
  assert.equal(st.mode, 'append');
  assert.equal(st.cursor, 5);
  assert.equal(st.events.length, 0);
});

test('transcript: cursor going down (hub restarted) resets', () => {
  const st = UI.transcriptStep(120, {events: [], cursor: 3, done: false, available: true});
  assert.equal(st.mode, 'reset');
  assert.equal(st.cursor, null);
});

test('transcript: explicit reset flag and cursor 0 after restart reset too', () => {
  assert.equal(UI.transcriptStep(10, {events: [{seq: 11}], cursor: 11, reset: true}).mode, 'reset');
  assert.equal(UI.transcriptStep(10, {events: [], cursor: 0, available: false}).mode, 'reset');
});

test('transcript: no cursor (old builds / bare array) replaces', () => {
  assert.equal(UI.transcriptStep(4, [{text: 'x'}]).mode, 'replace');
  assert.equal(UI.transcriptStep(null, {events: [{text: 'x'}]}).mode, 'replace');
  assert.equal(UI.transcriptStep(null, null).mode, 'replace');
});

test('t3 host: peer urls win, loopback skipped, falls back to node IPs', () => {
  assert.equal(UI.pickT3Host({urls: ['http://192.168.1.2:8787']}, '192.168.1.9', []), '192.168.1.2');
  assert.equal(UI.pickT3Host({url: 'http://host-b:8787'}, null, []), 'host-b');
  assert.equal(UI.pickT3Host({urls: ['http://127.0.0.1:8787']}, '127.0.0.1',
    [{ip: '127.0.0.1'}, {ip: '169.254.3.3'}, {ip: '192.168.1.1'}]), '192.168.1.1');
  assert.equal(UI.pickT3Host(null, 'localhost', []), '');
  assert.equal(UI.pickT3Host(undefined, undefined, undefined), '');
  assert.equal(UI.pickT3Host({urls: ['not a url']}, 'bad host<script>', []), '');
});

test('best fit jobs stay "any" after the hub resolves them', () => {
  const resolved = {engine: 'codex', req_engine: 'any'};
  assert.ok(UI.isAnyJob(resolved));
  assert.ok(UI.isAnyJob({engine: 'any'}));
  assert.ok(!UI.isAnyJob({engine: 'claude'}));
  assert.ok(UI.engineMatch(resolved, 'any'));
  assert.ok(UI.engineMatch(resolved, 'codex'));
  assert.ok(!UI.engineMatch({engine: 'claude'}, 'any'));
  assert.ok(UI.engineMatch({engine: 'claude'}, ''));
});

test('rollout status classes', () => {
  assert.equal(UI.rolloutClass('current'), 'done');
  assert.equal(UI.rolloutClass('unreachable'), 'failed');
  assert.equal(UI.rolloutClass('offline'), 'failed');
  assert.equal(UI.rolloutClass('missing'), 'cancelled');
  assert.equal(UI.rolloutClass('updating'), 'running');
  assert.equal(UI.rolloutClass(undefined), 'running');
});

const nodes = [{online: true}, {online: true}, {online: false}];

test('status line: all good counts machines; version behind during a rollout does not count', () => {
  const h = {alerts: [{level: 'warn', kind: 'version_behind', node_id: 'a'}, {level: 'info', msg: 'x'}],
             update_lease: {holder: 'a', version: '2.2.0', expires_in_s: 300}, rollout: []};
  assert.deepEqual(UI.statusLine(nodes, h, true), {cls: 'ok', text: 'All good – 2 of 3 machines on'});
});

test('status line: version behind with no rollout needs you (amber); errors make it red', () => {
  const warn = {alerts: [{level: 'warn', kind: 'version_behind'}], update_lease: null, rollout: [{status: 'current'}]};
  assert.deepEqual(UI.statusLine(nodes, warn, true), {cls: 'warn', text: '1 needs you'});
  const err = {alerts: [{level: 'warn', kind: 'disk_full'}, {level: 'error', kind: 'node_offline'}]};
  assert.deepEqual(UI.statusLine(nodes, err, true), {cls: 'err', text: '2 need you'});
  assert.equal(UI.statusLine(nodes, null, true).text, 'Checking…');
  assert.equal(UI.statusLine(nodes, err, false).cls, 'err');
});

test('a peer still "updating" also counts as a rollout in progress', () => {
  assert.ok(UI.rolloutActive({rollout: [{status: 'current'}, {status: 'updating'}]}));
  assert.ok(!UI.rolloutActive({rollout: [{status: 'current'}], update_lease: null}));
  assert.ok(!UI.rolloutActive(undefined));
});

test('alert view: plain reason and one action', () => {
  assert.deepEqual(UI.alertView({level: 'error', kind: 'node_offline'}, false, 'box').action, 'details');
  assert.match(UI.alertView({level: 'warn', kind: 'disk_full'}, false, 'box', 93.4).reason, /93% full/);
  const healing = UI.alertView({level: 'warn', kind: 'version_behind'}, true, 'box');
  assert.equal(healing.label, 'Fixing itself');
  assert.equal(healing.action, null);
  const bad = UI.alertView({level: 'error', kind: 'fleet_bad_version', msg: 'version 2.3.0 is marked fleet-bad (a canary rolled it back); watchdogs will not install it'}, false, '');
  assert.equal(bad.label, 'Rolled back');
  assert.doesNotMatch(bad.reason, /fleet-bad|watchdog/);
  // legacy kinds from older hubs still map
  assert.equal(UI.alertView({level: 'warn', kind: 'disk'}, false, 'b').label, 'Disk');
  assert.equal(UI.alertView({level: 'warn', kind: 'something_new', msg: 'm', node_id: 'n'}, false, 'b').reason, 'm');
});

test('task cards: failed first, then running, then queued, otherwise stable', () => {
  const js = [{id: 1, status: 'done'}, {id: 2, status: 'running'}, {id: 3, status: 'failed'}, {id: 4, status: 'queued'}, {id: 5, status: 'done'}, {id: 6, status: 'starting'}];
  assert.deepEqual(UI.sortCards(js).map(j => j.id), [3, 2, 6, 4, 1, 5]);
});

test('brief roll-up: hub 2.2 wraps it in {brief}, older shapes pass through', () => {
  assert.deepEqual(UI.unwrapBrief({brief: {text: 't'}}), {text: 't'});
  assert.equal(UI.unwrapBrief({brief: null}), null);
  assert.deepEqual(UI.unwrapBrief({text: 'bare'}), {text: 'bare'});
  assert.equal(UI.unwrapBrief(null), null);
});

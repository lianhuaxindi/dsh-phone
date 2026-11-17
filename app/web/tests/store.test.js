import test from 'node:test';
import assert from 'node:assert/strict';

import {
  SCREENS,
  normalizeSettings,
  normalizeSessionRecord,
  normalizeMessageRecord,
  normalizeDraft,
  MemoryStore,
  createStore,
} from '../js/store.js';
import { DEFAULT_SETTINGS, SILENCE_OPTIONS, AUDIO_MODES } from '../js/config.js';
import { PROTOCOL } from '../js/protocol.js';

test('normalizeSettings merges, validates and keeps defaults', () => {
  assert.deepEqual(normalizeSettings(), normalizeSettings({}));
  const merged = normalizeSettings({ lang: 'en', theme: 'dark', autoSend: false, host: '10.0.0.2', port: '9000' });
  assert.equal(merged.lang, 'en');
  assert.equal(merged.theme, 'dark');
  assert.equal(merged.autoSend, false);
  assert.equal(merged.host, '10.0.0.2');
  assert.equal(merged.port, 9000);

  const invalid = normalizeSettings({
    lang: 'fr',
    theme: 'neon',
    silenceMs: 999999,
    preferAudioDevice: 'carrier-pigeon',
    transportMode: 'telepathy',
    host: '   ',
    port: 0,
    lastScreen: 'hacked',
    audioMode: 'shout',
    fp: 'zzzz',
  });
  assert.equal(invalid.lang, DEFAULT_SETTINGS.lang);
  assert.equal(invalid.theme, DEFAULT_SETTINGS.theme);
  assert.equal(invalid.preferAudioDevice, DEFAULT_SETTINGS.preferAudioDevice);
  assert.equal(invalid.transportMode, DEFAULT_SETTINGS.transportMode);
  assert.equal(invalid.host, DEFAULT_SETTINGS.host);
  assert.equal(invalid.port, DEFAULT_SETTINGS.port);
  assert.equal(invalid.lastScreen, DEFAULT_SETTINGS.lastScreen);
  assert.equal(invalid.audioMode, AUDIO_MODES[0]);
  assert.equal(invalid.fp, DEFAULT_SETTINGS.fp);
  assert.ok(invalid.silenceMs >= 200 && invalid.silenceMs <= 3000);
});

test('normalizeSettings accepts the documented option sets', () => {
  for (const silenceMs of SILENCE_OPTIONS) {
    assert.equal(normalizeSettings({ silenceMs }).silenceMs, silenceMs);
  }
  for (const screen of SCREENS) {
    assert.equal(normalizeSettings({ lastScreen: screen }).lastScreen, screen);
  }
  for (const mode of AUDIO_MODES) {
    assert.equal(normalizeSettings({ audioMode: mode }).audioMode, mode);
  }
  const fp = 'a3f1c9d2e4b60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9a';
  assert.equal(normalizeSettings({ fp: fp.toUpperCase() }).fp, fp);
});

test('session/message records are validated', () => {
  assert.deepEqual(normalizeSessionRecord({ sessionId: 's1', title: 'A', updatedAt: 3, lastPreview: 'p' }), {
    sessionId: 's1',
    title: 'A',
    updatedAt: 3,
    lastPreview: 'p',
  });
  assert.throws(() => normalizeSessionRecord({}), /sessionId/);
  const message = normalizeMessageRecord({ messageId: 'm1', sessionId: 's1', role: 'narrator', text: 'x' });
  assert.equal(message.role, 'assistant');
  assert.equal(message.status, 'done');
  assert.throws(() => normalizeMessageRecord({ messageId: 'm1' }), /sessionId/);
  assert.throws(() => normalizeMessageRecord({ sessionId: 's1' }), /messageId/);
});

test('MemoryStore persists settings, sessions and messages per session', async () => {
  const store = new MemoryStore();
  assert.equal(store.backend, 'memory');

  const defaults = await store.getSettings();
  assert.equal(defaults.lang, DEFAULT_SETTINGS.lang);
  const saved = await store.saveSettings({ autoSend: false, lang: 'en' });
  assert.equal(saved.autoSend, false);
  assert.equal(saved.lang, 'en');
  assert.equal((await store.getSettings()).lang, 'en');

  const deviceId = await store.getDeviceId();
  assert.match(deviceId, /^[0-9a-f-]{36}$/);
  assert.equal(await store.getDeviceId(), deviceId, 'deviceId is stable');
  await store.setDeviceId('dev-test');
  assert.equal(await store.getDeviceId(), 'dev-test');

  await store.saveSession({ sessionId: 's1', title: 'older', updatedAt: 10 });
  await store.saveSession({ sessionId: 's2', title: 'newer', updatedAt: 20 });
  const sessions = await store.listSessions();
  assert.deepEqual(sessions.map((s) => s.sessionId), ['s2', 's1']);

  await store.saveMessage({ messageId: 'm2', sessionId: 's1', role: 'user', text: 'b', createdAt: 2 });
  await store.saveMessages([
    { messageId: 'm1', sessionId: 's1', role: 'user', text: 'a', createdAt: 1 },
    { messageId: 'm3', sessionId: 's2', role: 'assistant', text: 'c', createdAt: 3 },
  ]);
  assert.deepEqual((await store.getMessages('s1')).map((m) => m.messageId), ['m1', 'm2']);
  assert.deepEqual((await store.getMessages('s2')).map((m) => m.messageId), ['m3']);

  assert.equal(await store.clearSessionMessages('s1'), 2);
  assert.deepEqual(await store.getMessages('s1'), []);
  assert.equal((await store.getMessages('s2')).length, 1);

  await store.deleteSession('s2');
  assert.deepEqual((await store.listSessions()).map((s) => s.sessionId), ['s1']);
  assert.deepEqual(await store.getMessages('s2'), [], 'deleting a session also drops its messages');

  await store.clearAll();
  assert.deepEqual(await store.listSessions(), []);
  assert.deepEqual(await store.getSettings(), normalizeSettings());
});

test('normalizeDraft keeps text within protocol limits and treats blank as empty', () => {
  assert.equal(normalizeDraft(undefined), '');
  assert.equal(normalizeDraft('   '), '');
  assert.equal(normalizeDraft('hello'), 'hello');
  const long = 'x'.repeat(PROTOCOL.maxTextChars + 25);
  assert.equal(normalizeDraft(long).length, PROTOCOL.maxTextChars);
});

test('MemoryStore stores per-session composer drafts (TC-WEB-03)', async () => {
  const store = new MemoryStore({ drafts: { s1: 'seed draft' } });
  assert.equal(await store.getDraft('s1'), 'seed draft');
  assert.equal(await store.getDraft('missing'), '');

  await store.saveDraft('s2', 'unsent text');
  assert.equal(await store.getDraft('s2'), 'unsent text');
  await store.saveDraft('s2', '   ');
  assert.equal(await store.getDraft('s2'), '', 'blank draft clears it');

  await store.saveSession({ sessionId: 's2', title: 'drafty', updatedAt: 1 });
  await store.deleteSession('s2');
  assert.equal(await store.getDraft('s2'), '', 'deleting a session drops its draft');
  assert.equal(await store.getDraft('s1'), 'seed draft');

  await store.clearAll();
  assert.equal(await store.getDraft('s1'), '');
});

test('createStore degrades gracefully when IndexedDB is unavailable', async () => {
  const store = createStore({ indexedDB: undefined });
  assert.equal(store.backend, 'memory');
  assert.deepEqual(await store.listSessions(), []);

  // A factory that reports an IndexedDB but fails to open: operations still
  // work through the internal memory fallback (device is never blocked by IDB).
  const failing = {
    open() {
      throw new Error('IndexedDB blocked');
    },
  };
  const degraded = createStore({ indexedDB: failing });
  assert.equal(degraded.backend, 'indexeddb');
  assert.deepEqual(await degraded.listSessions(), []);
  await degraded.saveSession({ sessionId: 's1', title: 'fallback', updatedAt: 1 });
  assert.deepEqual((await degraded.listSessions()).map((s) => s.sessionId), ['s1']);
  await degraded.saveDraft('s1', 'draft survives the IDB fallback');
  assert.equal(await degraded.getDraft('s1'), 'draft survives the IDB fallback');
  assert.deepEqual(await degraded.getSettings(), normalizeSettings());
});

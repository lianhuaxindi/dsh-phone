import test from 'node:test';
import assert from 'node:assert/strict';

import {
  LATENCY,
  LATENCY_NAMES,
  LOG_LEVELS,
  sanitizeText,
  sanitizeField,
  redactForExport,
  formatEntryLine,
  percentile,
  Diagnostics,
} from '../js/diagnostics.js';
import { ManualScheduler } from '../js/scheduler.js';

test('percentile is a nearest-rank helper for p50/p95 (TC-WEB-13)', () => {
  assert.equal(percentile([], 0.5), 0);
  assert.equal(percentile([10, 20, 30, 40, 50], 0.5), 30);
  assert.equal(percentile([10, 20, 30, 40, 50], 0.95), 50);
  assert.equal(percentile([50, 10, 40, 20, 30], 0.5), 30, 'unsorted input');
  assert.equal(percentile([7], 0.95), 7);
  assert.equal(percentile([1, 2, 3, 4], 0.5), 2);
});

test('latency names cover the four PRD metrics', () => {
  assert.deepEqual([...LATENCY_NAMES].sort(), [
    'asr-final',
    'asr-first-partial',
    'chat-first-delta',
    'turn-complete',
  ]);
  assert.equal(LATENCY.ASR_FIRST_PARTIAL, 'asr-first-partial');
  assert.equal(LATENCY.ASR_FINAL, 'asr-final');
  assert.equal(LATENCY.CHAT_FIRST_DELTA, 'chat-first-delta');
  assert.equal(LATENCY.TURN_COMPLETE, 'turn-complete');
  assert.deepEqual(LOG_LEVELS, ['debug', 'info', 'warn', 'error']);
});

test('sanitizeText strips credentials and caps length', () => {
  assert.equal(sanitizeText('token=abcdefgh'), 'token=[redacted]');
  assert.equal(sanitizeText('pair_code: 123456'), 'pair_code=[redacted]');
  assert.equal(sanitizeText('code=654321'), 'code=[redacted]');
  assert.match(sanitizeText('message code=3003'), /code=3003/, 'error codes stay readable');
  const long = sanitizeText('x'.repeat(500));
  assert.equal(long.length, 161);
});

test('sanitizeField redacts secrets and fingerprints free text', () => {
  assert.equal(sanitizeField('token', 'abcdef'), '[redacted]');
  assert.equal(sanitizeField('deviceToken', 'abcdef'), '[redacted]');
  assert.equal(sanitizeField('hmac', 'abcdef'), '[redacted]');
  const textFp = sanitizeField('text', 'hello world');
  assert.equal(typeof textFp.hash, 'string');
  assert.equal(textFp.len, 11);
  const msgFp = sanitizeField('message', 'secret sauce');
  assert.equal(msgFp.len, 12);
  assert.equal(sanitizeField('code', 3003), 3003);
  assert.equal(sanitizeField('code', '123456'), '[redacted]');
  assert.equal(sanitizeField('durationMs', 42), 42);
  assert.equal(sanitizeField('phase', 'online'), 'online');
});

test('redactForExport never leaks free text', () => {
  const redacted = redactForExport({ text: { hash: 'abcd1234', len: 5 }, phase: 'online', token: 'abc' });
  assert.equal(redacted.text, undefined);
  assert.equal(redacted.textHash, 'abcd1234');
  assert.equal(redacted.textLen, 5);
  assert.equal(redacted.phase, 'online');
  assert.equal(redacted.token, '[redacted]');
});

test('Diagnostics records sanitized events, errors and latency stats', () => {
  const scheduler = new ManualScheduler(1000);
  const diag = new Diagnostics({
    appVersion: '1.0.0-web.1',
    protocolVersion: 1,
    buildChannel: 'webview',
    scheduler,
    maxEntries: 5,
    maxErrors: 2,
  });
  diag.info('connect.start', { host: '192.168.1.5', port: 8765 });
  diag.warn('turn.error', { code: 3003, retryable: true, message: 'model exploded' });
  const entry = diag.entries()[1];
  assert.equal(entry.level, 'warn');
  assert.equal(entry.fields.code, 3003);
  assert.equal(typeof entry.fields.message.hash, 'string');

  diag.recordError({ code: 3003, message: 'model exploded', retryable: true, source: 'chat' });
  diag.recordError({ code: 4001, message: 'busy', retryable: true, source: 'protocol' });
  diag.recordError({ code: 5001, message: 'boom', retryable: false, source: 'transport' });
  assert.equal(diag.recentErrors().length, 2, 'error ring buffer is capped');
  assert.equal(diag.lastError().code, 5001);
  assert.equal(diag.lastError().messageFp.len, 4);

  diag.latency(LATENCY.ASR_FIRST_PARTIAL, 120);
  diag.latency(LATENCY.ASR_FIRST_PARTIAL, 80);
  diag.latency(LATENCY.ASR_FIRST_PARTIAL, 200);
  diag.latency(LATENCY.TURN_COMPLETE, 1500.6);
  const stats = diag.latencyStats();
  assert.deepEqual(stats[LATENCY.ASR_FIRST_PARTIAL], {
    count: 3,
    last: 200,
    min: 80,
    max: 200,
    avg: 133,
    p50: 120,
    p95: 200,
  });
  assert.equal(stats[LATENCY.TURN_COMPLETE].count, 1);
  assert.equal(diag.latency('bad', Number.NaN), null);

  for (let i = 0; i < 10; i += 1) diag.debug('tick', { index: i });
  assert.equal(diag.entries().length, 5, 'entry ring buffer is capped');
});

test('Diagnostics snapshot and export text include versions/latency/errors but no secrets or text', () => {
  const scheduler = new ManualScheduler(1_700_000_000_000);
  const diag = new Diagnostics({ appVersion: '1.0.0-web.1', protocolVersion: 1, buildChannel: 'webview', scheduler });
  diag.info('pair.start', { host: '192.168.1.5', port: 8765, token: 'super-secret-token', code: '123456' });
  diag.info('chat.send', { sessionId: 's1', text: '会泄露吗？', messageId: 'm1' });
  diag.recordError({ code: 1002, message: 'pair code 123456 rejected', retryable: true });
  diag.latency(LATENCY.CHAT_FIRST_DELTA, 250);

  const snapshot = diag.snapshot();
  assert.equal(snapshot.appVersion, '1.0.0-web.1');
  assert.equal(snapshot.protocolVersion, 1);
  assert.equal(snapshot.entryCount, 4, 'two events + recorded error + latency sample');
  assert.equal(snapshot.latency[LATENCY.CHAT_FIRST_DELTA].last, 250);

  const text = diag.exportText();
  assert.match(text, /DSH Phone diagnostics export/);
  assert.match(text, /app=1\.0\.0-web\.1/);
  assert.match(text, /chat-first-delta 1 250 250 250 250 250 250/);
  assert.match(text, /1002/);
  assert.doesNotMatch(text, /super-secret-token/);
  assert.doesNotMatch(text, /123456/, 'pair code must never be exported');
  assert.doesNotMatch(text, /会泄露吗/, 'message text must never be exported');
  assert.doesNotMatch(text, /secret=[^r]/, 'secrets are replaced with [redacted]');

  const line = formatEntryLine(diag.entries()[0]);
  assert.match(line, /pair\.start/);
  assert.doesNotMatch(line, /super-secret-token/);

  diag.clear();
  assert.equal(diag.entries().length, 0);
  assert.equal(diag.recentErrors().length, 0);
  assert.deepEqual(diag.latencyStats(), {});
  assert.match(diag.exportText(), /\(no samples\)/);
});

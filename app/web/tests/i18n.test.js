import test from 'node:test';
import assert from 'node:assert/strict';

import {
  DEFAULT_LANG,
  AVAILABLE_LANGS,
  DICTS,
  I18N_KEYS,
  hasLang,
  normalizeLang,
  setLang,
  getLang,
  availableLangs,
  interpolate,
  raw,
  hasKey,
  t,
  missingKeys,
  formatRelativeTime,
  errorCopy,
  closeCopy,
} from '../js/i18n.js';
import { ErrorCode, CloseCode } from '../js/protocol.js';

test('zh is the default language and both dictionaries are complete', () => {
  assert.equal(DEFAULT_LANG, 'zh');
  assert.deepEqual([...AVAILABLE_LANGS].sort(), ['en', 'zh']);
  assert.deepEqual([...availableLangs()].sort(), ['en', 'zh']);
  assert.deepEqual(missingKeys('zh'), []);
  assert.deepEqual(missingKeys('en'), [], 'en must define every zh key');
  // And zh must define every en key, so switching language never falls back.
  for (const key of Object.keys(DICTS.en)) {
    assert.ok(Object.prototype.hasOwnProperty.call(DICTS.zh, key), `zh is missing ${key}`);
  }
  assert.equal(I18N_KEYS.length, Object.keys(DICTS.zh).length);
  assert.ok(I18N_KEYS.length > 300, `expected a substantial dictionary, got ${I18N_KEYS.length}`);
});

test('language switching and normalization', () => {
  assert.equal(hasLang('zh'), true);
  assert.equal(hasLang('en'), true);
  assert.equal(hasLang('fr'), false);
  assert.equal(normalizeLang('fr'), 'zh');
  assert.equal(normalizeLang(undefined), 'zh');
  assert.equal(setLang('en'), 'en');
  assert.equal(getLang(), 'en');
  assert.equal(setLang('xx'), 'zh');
  assert.equal(getLang(), 'zh');
});

test('t() interpolates variables and surfaces missing keys verbatim', () => {
  assert.equal(interpolate('Hello {name}, {n} left', { name: 'Ana', n: 3 }), 'Hello Ana, 3 left');
  assert.equal(interpolate('Hello {name}', {}), 'Hello {name}');
  assert.equal(t('does.not.exist'), 'does.not.exist');
  assert.equal(raw('does.not.exist'), undefined);
  assert.equal(hasKey('common.cancel'), true);
  assert.equal(t('chat.tooLong', { max: 32000 }), raw('chat.tooLong').replace('{max}', '32000'));
});

test('every protocol error code has copy with explicit next-step guidance', () => {
  for (const code of Object.values(ErrorCode)) {
    for (const lang of ['zh', 'en']) {
      setLang(lang);
      const copy = errorCopy(code);
      assert.notEqual(copy.title, `error.${code}.title`, `${lang} error.${code}.title missing`);
      assert.ok(copy.title.length > 1, `${lang} error.${code}.title too short`);
      assert.ok(copy.next.length > 8, `${lang} error.${code}.next too short`);
      assert.match(copy.next, lang === 'zh' ? /下一步|Next/ : /Next/);
    }
  }
  setLang('zh');
  assert.deepEqual(errorCopy(9001), errorCopy('unknown'));
});

test('every websocket close code has copy with explicit next-step guidance', () => {
  for (const code of Object.values(CloseCode)) {
    for (const lang of ['zh', 'en']) {
      setLang(lang);
      const copy = closeCopy(code);
      assert.notEqual(copy.title, `close.${code}.title`, `${lang} close.${code}.title missing`);
      assert.match(copy.next, lang === 'zh' ? /下一步|Next|自动/ : /Next|auto/i);
    }
  }
  setLang('zh');
  assert.deepEqual(closeCopy(4999), closeCopy('unknown'));
});

test('phase copy exists for every UI state', () => {
  const phases = [
    'idle',
    'connecting',
    'pairing',
    'authenticating',
    'online',
    'recording',
    'waiting-for-reply',
    'approval-pending',
  ];
  for (const phase of phases) {
    for (const lang of ['zh', 'en']) {
      setLang(lang);
      assert.equal(hasKey(`phase.${phase}`), true, `${lang} phase.${phase}`);
      assert.equal(hasKey(`phaseHint.${phase}`), true, `${lang} phaseHint.${phase}`);
    }
  }
  setLang('zh');
});

test('formatRelativeTime covers just-now/minutes/hours/days', () => {
  const now = 1_700_000_000;
  assert.equal(formatRelativeTime(now - 5, now), raw('time.justNow'));
  assert.equal(formatRelativeTime(now - 120, now), raw('time.minutesAgo').replace('{n}', '2'));
  assert.equal(formatRelativeTime(now - 7200, now), raw('time.hoursAgo').replace('{n}', '2'));
  assert.equal(formatRelativeTime(now - 172800, now), raw('time.daysAgo').replace('{n}', '2'));
  assert.equal(formatRelativeTime(0, now), raw('common.unknown'));
  assert.equal(formatRelativeTime('nope', now), raw('common.unknown'));
});

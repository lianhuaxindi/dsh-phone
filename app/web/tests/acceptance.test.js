/**
 * M2 acceptance (task-6): maps docs/TESTPLAN.md TC-WEB-01..13 onto either
 * behavioural assertions or, where the requirement is DOM/gesture/rendering
 * only and Node has no browser, source-contract assertions on the shipped
 * WebView sources. The two tables in app/web/ACCEPTANCE.md reference the test
 * names below, so keep names stable when editing.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { parseMessageSegments, SCROLL_FOLLOW_THRESHOLD_PX } from '../js/views/chat.js';
import { PREVIEW_MAX_CHARS } from '../js/views/sessions.js';
import { formatRemaining } from '../js/ui/approval-sheet.js';
import { ErrorCode } from '../js/protocol.js';
import { errorCopy, closeCopy, hasKey } from '../js/i18n.js';

const source = (relativePath) => readFileSync(new URL(`../${relativePath}`, import.meta.url), 'utf8');

test('TC-WEB-01 offline/reconnecting banner exists with a retry action', () => {
  const app = source('js/app.js');
  assert.match(app, /conn-banner/);
  assert.match(app, /_syncConnectionBanner/);
  assert.match(app, /state === 'reconnecting'/);
  assert.match(app, /state === 'failed' \|\| state === 'closed'/);
  assert.match(app, /reconnectBridge\(\)/);
  assert.match(app, /toast\.reconnecting/);
});

test('TC-WEB-02 session previews are truncated and rows keep a stable shape', () => {
  assert.equal(PREVIEW_MAX_CHARS, 120);
  const sessions = source('js/views/sessions.js');
  assert.match(sessions, /rawPreview\.slice\(0, PREVIEW_MAX_CHARS\)/);
  assert.match(sessions, /formatRelativeTime/);
});

test('TC-WEB-03 drafts persist locally and are restored into the composer', () => {
  const controller = source('js/controller.js');
  assert.match(controller, /draftFor\(sessionId\)/);
  assert.match(controller, /saveDraft\(sessionId, text\)/);
  assert.match(controller, /clearDraft\(sessionId\)/);
  assert.match(controller, /getDraft\(sessionId\)/);
  assert.match(controller, /draft,/);
  const chat = source('js/views/chat.js');
  assert.match(chat, /controller\.saveDraft\?\.\(this\.sessionId/);
  assert.match(chat, /_restoreDraft/);
});

test('TC-WEB-04 too-long input is rejected with guidance; send is disabled while empty/sending', () => {
  const chat = source('js/views/chat.js');
  assert.match(chat, /text\.length > 32000/);
  assert.match(chat, /chat\.tooLong/);
  assert.match(chat, /_syncSendState/);
  assert.match(chat, /this\.sendButton\.disabled = empty \|\| Boolean\(this\.textarea\.disabled\)/);
  const controller = source('js/controller.js');
  assert.match(controller, /chat\.tooLong/);
  assert.match(controller, /PROTOCOL\.maxTextChars/);
});

test('TC-WEB-05 streaming text, code blocks, tool states and scroll follow are implemented', () => {
  const segments = parseMessageSegments('intro\n```js\nconst a = 1;\n```\noutro');
  assert.deepEqual(segments, [
    { type: 'text', text: 'intro\n' },
    { type: 'code', language: 'js', code: 'const a = 1;' },
    { type: 'text', text: '\noutro' },
  ]);
  assert.deepEqual(parseMessageSegments('plain'), [{ type: 'text', text: 'plain' }]);

  const chat = source('js/views/chat.js');
  assert.match(chat, /pre', \{ class: 'code-block mono' \}/);
  assert.match(chat, /tool-status__dot--\$\{state\}/);
  assert.match(chat, /message--streaming/);
  assert.match(chat, /this\.follow = distance < FOLLOW_THRESHOLD_PX/);
  assert.ok(SCROLL_FOLLOW_THRESHOLD_PX > 0);
});

test('TC-WEB-06 copy/resend/stop actions exist for messages', () => {
  const chat = source('js/views/chat.js');
  assert.match(chat, /a11y\.copyMessage/);
  assert.match(chat, /a11y\.resend/);
  assert.match(chat, /message__resend/);
  assert.match(chat, /this\.controller\.sendText\(message\.text\)/);
  assert.match(chat, /controller\.cancelTurn\(\)/);
  assert.match(chat, /scroll-bottom/);
});

test('TC-WEB-07 every send gets a fresh uuid clientId (idempotency key)', () => {
  const controller = source('js/controller.js');
  assert.match(controller, /const clientId = randomUuid\(\)/);
  assert.match(controller, /messageId: `local-\$\{clientId\}`/);
  const protocol = source('js/protocol.js');
  assert.match(protocol, /export function randomUuid/);
});

test('TC-WEB-08 every protocol error/close code ships a title + next-step hint', () => {
  const codes = Object.values(ErrorCode);
  assert.ok(codes.length >= 10);
  for (const code of codes) {
    const copy = errorCopy(code);
    assert.ok(copy.title.length > 0, `error ${code} title`);
    assert.match(copy.next, /下一步|Next/i, `error ${code} next-step guidance`);
  }
  for (const code of [1000, 1011, 4400, 4401, 4402, 4408, 4410]) {
    const copy = closeCopy(code);
    assert.match(copy.next, /下一步|Next/i, `close ${code} next-step guidance`);
  }
});

test('TC-WEB-09 ASR partial writes are coalesced per animation frame; final handled', () => {
  const chat = source('js/views/chat.js');
  assert.match(chat, /requestAnimationFrame/);
  assert.match(chat, /_pendingPartialText/);
  assert.match(chat, /_flushPartial/);
  assert.match(chat, /_onAsrFinal/);
  assert.match(chat, /partial-preview/);
  // Partial preview sits above the composer row (PRD §12).
  assert.match(chat, /chat__composer'[\s\S]{0,200}this\.partialPreview/);
});

test('TC-WEB-10 approval sheet: deny focused by default, mono red command, countdown disables buttons', () => {
  const sheet = source('js/ui/approval-sheet.js');
  assert.match(sheet, /label: t\('approval\.deny'\)[\s\S]{0,160}autofocus: true/);
  assert.match(sheet, /approval__command mono/);
  assert.match(sheet, /approval\.autoDenyHint/);
  assert.match(sheet, /button\.disabled = true/);
  assert.match(sheet, /formatRemaining/);
  assert.equal(formatRemaining(125), '2:05');
  assert.equal(formatRemaining(45), '45s');
  assert.equal(formatRemaining(0), '0s');

  const css = source('css/components.css');
  assert.match(css, /\.approval__command/);
  assert.match(css, /color: var\(--danger\)/);
  const base = source('css/app.css');
  assert.match(base, /\.mono[\s\S]{0,120}font-family: var\(--font-mono\)/);
});

test('TC-WEB-11 question sheet: single/multi/custom, required validation, cancel sends cancelled', () => {
  const question = source('js/ui/question-sheet.js');
  assert.match(question, /type: question\.multiSelect \? 'checkbox' : 'radio'/);
  assert.match(question, /question\.customPlaceholder/);
  assert.match(question, /question\.requiredError/);
  assert.match(question, /cancelled: true/);
  assert.match(question, /question\.multiHint/);
});

test('TC-WEB-12 settings persist immediately and drive language/audio preference', () => {
  const controller = source('js/controller.js');
  assert.match(controller, /preferDevice\?\./);
  assert.match(controller, /TransportKind\.NATIVE/);
  const settings = source('js/views/settings.js');
  assert.match(settings, /_update\(\{ lang/);
  assert.match(settings, /_update\(\{ theme/);
  assert.match(settings, /_update\(\{ silenceMs/);
  assert.match(settings, /preferAudioDevice/);
  assert.match(settings, /reconnectBridge/);
});

test('TC-WEB-13 diagnostics exposes p50/p95 latency, recent errors, sanitized export', () => {
  const view = source('js/views/diagnostics.js');
  assert.match(view, /p50/);
  assert.match(view, /p95/);
  assert.match(view, /recentErrors|data\.errors/);
  assert.match(view, /exportLogs/);
  const diagnostics = source('js/diagnostics.js');
  assert.match(diagnostics, /export function percentile/);
  assert.match(diagnostics, /LATENCY_SAMPLE_WINDOW/);
  assert.match(diagnostics, /tokens and message text are never logged/);
});

test('PRD §12: touch targets, system dark/light, reduced motion, bluetooth status bar, bilingual a11y labels', () => {
  const tokens = source('css/tokens.css');
  assert.match(tokens, /--touch: 48px/);
  assert.match(tokens, /prefers-color-scheme: dark/);
  assert.match(tokens, /prefers-reduced-motion/);
  const components = source('css/components.css');
  assert.match(components, /min-height: var\(--touch\)/);
  const chat = source('js/views/chat.js');
  assert.match(chat, /aria-label/);
  assert.match(chat, /role: 'log'/);
  assert.match(chat, /controller\.on\('audio-route'/);
  assert.match(chat, /_routePatch/);
  const app = source('js/app.js');
  assert.match(app, /route-chip/);
  assert.match(app, /_onAudioRoute/);
  assert.match(app, /controller\.on\('audio-route'/);
  assert.ok(hasKey('audio.route'), 'bluetooth route label key exists');
  const i18n = source('js/i18n.js');
  assert.match(i18n, /const en = \{/);
  assert.match(i18n, /const zh = \{/);
});

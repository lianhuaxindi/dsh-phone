import test from 'node:test';
import assert from 'node:assert/strict';

import { Controller, ControllerEvent } from '../js/controller.js';
import { MockTransport, MOCK_PAIRING, MOCK_LATENCY } from '../js/mock.js';
import { MemoryStore } from '../js/store.js';
import { Diagnostics } from '../js/diagnostics.js';
import { ManualScheduler } from '../js/scheduler.js';
import { PROTOCOL } from '../js/protocol.js';
import { TransportEvent } from '../js/transport.js';

const NOW_MS = 1_700_000_000_000;

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
}

/**
 * Build a controller wired to the mock backend with a manual clock and an
 * in-memory store — the exact composition the demo uses, minus the DOM.
 */
async function createHarness(seed = {}) {
  const scheduler = new ManualScheduler(1000);
  const store = new MemoryStore(seed);
  const now = () => Math.floor(scheduler.now() / 1000) + NOW_MS / 1000;
  const transport = new MockTransport({ scheduler, latency: {}, now });
  const diagnostics = new Diagnostics({ appVersion: 'test', protocolVersion: 1, buildChannel: 'test', scheduler });
  const controller = new Controller({ transport, store, diagnostics, scheduler, now });
  const events = [];
  for (const name of Object.values(ControllerEvent)) {
    controller.on(name, (payload) => events.push({ name, payload }));
  }
  const find = (name) => events.filter((event) => event.name === name);
  const last = (name) => events.filter((event) => event.name === name).at(-1);

  await controller.init();

  /** Advance the virtual clock and let async request promises settle. */
  const advance = async (ms) => {
    scheduler.advanceBy(ms);
    await flush();
  };

  const online = async () => {
    await controller.connectPairing({ ...MOCK_PAIRING });
    await advance(MOCK_LATENCY.connect);
    await advance(MOCK_LATENCY.pair);
    await advance(MOCK_LATENCY.auth);
    assert.equal(controller.snapshot().phase, 'online', 'harness must reach online');
    await flush();
  };

  return { scheduler, store, transport, diagnostics, controller, events, find, last, advance, online, seed };
}

test('init loads settings/deviceId and emits READY', async () => {
  const { controller, events, find, store } = await createHarness();
  assert.equal(find(ControllerEvent.READY).length, 1);
  const ready = find(ControllerEvent.READY)[0].payload;
  assert.equal(ready.transportKind, 'mock');
  assert.match(ready.deviceId, /^[0-9a-f-]{36}$/);
  assert.equal(ready.settings.lang, 'zh');
  assert.equal(ready.snapshot.phase, 'idle');
  assert.equal(await store.getDeviceId(), ready.deviceId);
  const snapshot = controller.snapshot();
  assert.equal(snapshot.phase, 'idle');
  assert.equal(snapshot.online, false);
  assert.equal(snapshot.canSend, false);
});

test('pairing flow reaches online, loads sessions and persists paired state', async () => {
  const harness = await createHarness();
  const { controller, find, store, advance } = harness;
  await harness.online();

  const sessions = find(ControllerEvent.SESSIONS).at(-1).payload;
  assert.ok(sessions.sessions.length >= 2, 'demo sessions are loaded');
  assert.equal(sessions.sessions[0].sessionId, 'demo-session-1');
  const settings = await store.getSettings();
  assert.equal(settings.paired, true);
  assert.ok(find(ControllerEvent.TOAST).some((event) => event.payload.key === 'toast.paired'));
  assert.equal(controller.snapshot().online, true);
  assert.equal(controller.snapshot().canSend, true);

  // Wrong pair code surfaces a PAIR_ERROR with the protocol code.
  const wrong = await createHarness();
  await wrong.controller.connectPairing({ ...MOCK_PAIRING, code: '999999' });
  await wrong.advance(MOCK_LATENCY.connect);
  await wrong.advance(MOCK_LATENCY.pair);
  const pairError = wrong.find(ControllerEvent.PAIR_ERROR).at(-1);
  assert.ok(pairError);
  assert.equal(pairError.payload.code, 1002);
  assert.equal(pairError.payload.attemptsLeft, 4);
  assert.equal(wrong.controller.snapshot().phase, 'idle');
});

test('openSession loads local history first then server messages', async () => {
  const harness = await createHarness({
    sessions: [{ sessionId: 'demo-session-1', title: 'Cached', updatedAt: 5, lastPreview: 'hi' }],
    messages: [{ messageId: 'local-1', sessionId: 'demo-session-1', role: 'user', text: 'cached', status: 'done', createdAt: 1 }],
  });
  const { controller, find, advance } = harness;
  await advance(0);
  await controller.openSession('demo-session-1');
  await flush();
  await advance(0);
  assert.ok(find(ControllerEvent.SESSION_OPENED).length >= 1);
  const payload = find(ControllerEvent.SESSION_OPENED).at(-1).payload;
  assert.equal(payload.sessionId, 'demo-session-1');
  assert.equal(controller.snapshot().activeSessionId, 'demo-session-1');
  assert.ok(payload.messages.some((message) => message.text === 'cached'));
  // Local history is shown before the server round-trip completes.
  const firstOpened = find(ControllerEvent.SESSION_OPENED)[0].payload;
  assert.ok(firstOpened.messages.some((message) => message.text === 'cached'));
});

test('sendText streams a reply and completes the turn', async () => {
  const harness = await createHarness();
  const { controller, find, advance } = harness;
  await harness.online();
  await controller.openSession('demo-session-1');
  await flush();

  const userMessage = await controller.sendText('你好');
  assert.ok(userMessage);
  assert.equal(userMessage.role, 'user');
  assert.equal(userMessage.local, true);
  assert.match(userMessage.messageId, /^local-/);
  assert.equal(controller.snapshot().phase, 'waiting-for-reply');
  assert.equal(controller.snapshot().canSend, false);

  await advance(MOCK_LATENCY.tool + MOCK_LATENCY.deltaGap * 200 + 1000);
  await flush();

  const added = find(ControllerEvent.MESSAGE_ADDED).map((event) => event.payload.message);
  assert.ok(added.some((message) => message.role === 'user' && message.messageId === userMessage.messageId));
  assert.ok(added.some((message) => message.role === 'assistant' && message.status === 'streaming'));
  const deltas = find(ControllerEvent.MESSAGE_DELTA);
  assert.ok(deltas.length > 3);
  const updated = find(ControllerEvent.MESSAGE_UPDATED).at(-1).payload.message;
  assert.equal(updated.role, 'assistant');
  assert.equal(updated.status, 'done');
  assert.equal(controller.snapshot().phase, 'online');
  assert.equal(controller.snapshot().canSend, true);
  assert.ok(find(ControllerEvent.LATENCY).some((event) => event.payload['turn-complete']?.count >= 1));

  const messages = controller.snapshot().messages;
  const lastUser = messages.filter((message) => message.role === 'user').at(-1);
  assert.equal(lastUser.text, '你好');
  assert.equal(lastUser.status, 'done');
  const lastAssistant = messages.filter((message) => message.role === 'assistant').at(-1);
  assert.equal(lastAssistant.status, 'done');
  assert.ok(lastAssistant.text.length > 10);
});

test('empty text is blocked with a toast, not a request', async () => {
  const harness = await createHarness();
  await harness.online();
  const result = await harness.controller.sendText('   ');
  assert.equal(result, null);
  assert.ok(harness.find(ControllerEvent.TOAST).some((event) => event.payload.key === 'chat.emptyBlocked'));
});

test('approval request suspends the turn and responds with denied', async () => {
  const harness = await createHarness();
  const { controller, find, advance } = harness;
  await harness.online();
  await controller.openSession('demo-session-1');
  await flush();
  await controller.sendText('帮我删除文件');
  await advance(MOCK_LATENCY.tool);

  const approvalEvent = find(ControllerEvent.APPROVAL).at(-1);
  assert.ok(approvalEvent);
  const approval = approvalEvent.payload;
  assert.equal(approval.tool, 'shell');
  assert.match(approval.argsPreview, /rm -rf/);
  assert.equal(controller.snapshot().phase, 'approval-pending');

  await controller.respondApproval(approval.id, 'denied');
  await advance(MOCK_LATENCY.deltaGap * 200 + 1000);
  await flush();
  assert.equal(controller.snapshot().phase, 'online');
  assert.equal(controller.snapshot().approval, null);
  assert.ok(find(ControllerEvent.TOAST).some((event) => event.payload.key === 'approval.denied'));
});

test('question request gets answers and resumes the turn', async () => {
  const harness = await createHarness();
  const { controller, find, advance } = harness;
  await harness.online();
  await controller.openSession('demo-session-1');
  await flush();
  await controller.sendText('请帮我选择方案');
  await advance(MOCK_LATENCY.tool);

  const questionEvent = find(ControllerEvent.QUESTION).at(-1);
  assert.ok(questionEvent);
  const question = questionEvent.payload;
  assert.equal(question.questions.length, 2);
  assert.equal(controller.snapshot().phase, 'approval-pending');

  await controller.respondQuestion(question.id, [{ questionId: 'q1', selected: ['a'], custom: null }]);
  await advance(MOCK_LATENCY.deltaGap * 200 + 1000);
  await flush();
  assert.equal(controller.snapshot().phase, 'online');
  assert.equal(controller.snapshot().question, null);
});

test('error scenario closes the turn with an error and allows retry', async () => {
  const harness = await createHarness();
  const { controller, find, advance } = harness;
  await harness.online();
  await controller.openSession('demo-session-1');
  await flush();
  await controller.sendText('这里报错');
  await advance(MOCK_LATENCY.tool + MOCK_LATENCY.firstDelta + MOCK_LATENCY.deltaGap * 20 + 1000);
  await flush();

  const errorEvent = find(ControllerEvent.ERROR).at(-1);
  assert.ok(errorEvent);
  assert.equal(errorEvent.payload.code, 3003);
  assert.equal(errorEvent.payload.scope, 'chat');
  assert.equal(controller.snapshot().phase, 'online');
  const assistant = controller.snapshot().messages.filter((message) => message.role === 'assistant').at(-1);
  assert.equal(assistant.status, 'error');

  controller.retryLastTurn();
  await flush();
  assert.equal(controller.snapshot().phase, 'waiting-for-reply', 'retry starts a new turn');
});

test('PTT recording drives the audio state and auto-sends the ASR final', async () => {
  const harness = await createHarness();
  const { controller, find, advance } = harness;
  await harness.online();
  await controller.openSession('demo-session-1');
  await flush();

  assert.equal(controller.startRecording(), true);
  const audioStates = find(ControllerEvent.AUDIO_STATE).map((event) => event.payload.state);
  assert.ok(audioStates.includes('preheating'));
  assert.equal(controller.snapshot().phase, 'recording');

  await advance(MOCK_LATENCY.preheat + MOCK_LATENCY.firstPartial + MOCK_LATENCY.partialGap * 6);
  const partials = find(ControllerEvent.PARTIAL);
  assert.ok(partials.length > 0);
  assert.equal(partials.at(-1).payload.text, '帮我总结一下今天的会议');
  assert.ok(find(ControllerEvent.AUDIO_LEVEL).length > 0);
  assert.equal(find(ControllerEvent.AUDIO_ROUTE).at(-1).payload.route, 'hfp');

  controller.stopRecording();
  await advance(MOCK_LATENCY.asrFinal + MOCK_LATENCY.tool + MOCK_LATENCY.deltaGap * 200 + 1000);
  await flush();
  const finalEvent = find(ControllerEvent.ASR_FINAL).at(-1);
  assert.ok(finalEvent);
  assert.equal(finalEvent.payload.text, '帮我总结一下今天的会议');
  assert.ok(
    controller.snapshot().messages.some((message) => message.role === 'user' && message.text === '帮我总结一下今天的会议'),
    'autoSend defaults to on, so the transcript is sent',
  );
});

test('autoSend off keeps the transcript editable instead of sending', async () => {
  const harness = await createHarness({ settings: { autoSend: false } });
  const { controller, find, advance } = harness;
  await harness.online();
  await controller.openSession('demo-session-1');
  await flush();
  controller.startRecording();
  await advance(MOCK_LATENCY.preheat + MOCK_LATENCY.firstPartial + MOCK_LATENCY.partialGap * 6);
  controller.stopRecording();
  await advance(MOCK_LATENCY.asrFinal);
  await flush();
  const finalEvent = find(ControllerEvent.ASR_FINAL).at(-1);
  assert.ok(finalEvent);
  assert.equal(finalEvent.payload.text, '帮我总结一下今天的会议');
  assert.equal(
    controller.snapshot().messages.filter((message) => message.role === 'user' && message.text === finalEvent.payload.text).length,
    0,
    'nothing is sent when autoSend is off',
  );
});

test('updateSettings persists normalized settings and emits SETTINGS', async () => {
  const harness = await createHarness();
  const { controller, find, store } = harness;
  const updated = await controller.updateSettings({ lang: 'en', theme: 'dark', silenceMs: 800, bogus: 1 });
  assert.equal(updated.lang, 'en');
  assert.equal(updated.theme, 'dark');
  assert.equal(updated.silenceMs, 800);
  assert.ok(!('bogus' in updated));
  const stored = await store.getSettings();
  assert.equal(stored.lang, 'en');
  const event = find(ControllerEvent.SETTINGS).at(-1);
  assert.ok(event);
  assert.equal(event.payload.lang, 'en');
});

test('reconnectBridge and connection status are reflected in the snapshot', async () => {
  const harness = await createHarness();
  const { controller, find, advance, transport } = harness;
  await harness.online();
  const before = find(ControllerEvent.STATE).length;
  await controller.reconnectBridge();
  await advance(MOCK_LATENCY.connect + MOCK_LATENCY.auth);
  await flush();
  assert.ok(find(ControllerEvent.RECONNECTING).length >= 1);
  assert.ok(find(ControllerEvent.STATE).length > before);
  assert.ok(transport.state === 'online');
  assert.equal(controller.snapshot().online, true);
});

test('chat error retry and diagnostics export surface controller data', async () => {
  const harness = await createHarness();
  const { controller, find, advance, diagnostics } = harness;
  await harness.online();
  diagnostics.recordError({ code: 3003, message: 'boom', retryable: true });
  diagnostics.latency('turn-complete', 1234);
  const info = controller.getDiagnostics();
  assert.equal(info.transportKind, 'mock');
  assert.ok(info.latency['turn-complete'].last === 1234);
  assert.equal(info.errors.at(-1).code, 3003);
  assert.equal(info.settings.paired, true);

  controller.exportLogs();
  await advance(MOCK_LATENCY.export + 10);
  await flush();
  const exported = find(ControllerEvent.LOGS_EXPORTED).at(-1);
  assert.ok(exported, 'mock bridge answers with logs.exported');
  assert.match(exported.payload.text, /DSH Phone web demo log/);

  controller.clearLogs();
  assert.equal(controller.getDiagnostics().errors.length, 0);
});

test('too-long text is rejected with chat.tooLong and no turn starts', async () => {
  const harness = await createHarness();
  await harness.online();
  const result = await harness.controller.sendText('x'.repeat(PROTOCOL.maxTextChars + 1));
  assert.equal(result, null);
  assert.equal(harness.controller.snapshot().phase, 'online');
  const toast = harness.find(ControllerEvent.TOAST).at(-1);
  assert.equal(toast.payload.key, 'chat.tooLong');
  assert.equal(toast.payload.vars.max, PROTOCOL.maxTextChars);
});

test('composer drafts persist locally, restore on open and clear after send', async () => {
  const harness = await createHarness({
    sessions: [{ sessionId: 'demo-session-1', title: 'Drafts', updatedAt: 5, lastPreview: '' }],
  });
  const { controller, find, store, advance } = harness;
  await advance(0);
  await controller.openSession('demo-session-1');
  await flush();
  await harness.online();

  controller.saveDraft('demo-session-1', '未发送的草稿');
  assert.equal(controller.draftFor('demo-session-1'), '未发送的草稿');
  await flush();
  assert.equal(await store.getDraft('demo-session-1'), '未发送的草稿');

  // Re-opening a session restores the draft into the composer via the event.
  const before = find(ControllerEvent.SESSION_OPENED).length;
  await controller.openSession('demo-session-1');
  await flush();
  const opened = find(ControllerEvent.SESSION_OPENED).slice(before).at(-1);
  assert.ok(opened);
  assert.equal(opened.payload.draft, '未发送的草稿');

  await controller.sendText('现在发送');
  assert.equal(controller.draftFor('demo-session-1'), '');
  await flush();
  assert.equal(await store.getDraft('demo-session-1'), '', 'draft is cleared after a successful send');
});

test('preferred audio device setting is pushed to the transport (FR-505)', async () => {
  const harness = await createHarness();
  const { controller, transport } = harness;
  await harness.online();
  const calls = [];
  transport.preferDevice = (address) => {
    calls.push(address);
  };
  await controller.updateSettings({ preferAudioDevice: 'hfp' });
  await controller.updateSettings({ preferAudioDevice: 'auto' });
  assert.deepEqual(calls, ['hfp', null]);
  await controller.updateSettings({ lang: 'en' });
  assert.deepEqual(calls, ['hfp', null], 'unrelated settings do not re-push the device');
});

test('error frames drive recovery: 1001 clears token, 3001 refreshes sessions, 4001 closes the turn', async () => {
  const harness = await createHarness();
  const { controller, transport, find, advance } = harness;
  await harness.online();
  await controller.openSession('demo-session-1');
  await flush();

  const sent = [];
  const originalSend = transport.send.bind(transport);
  transport.send = (message) => {
    sent.push(message);
    return originalSend(message);
  };
  const tokens = [];
  const originalSetToken = transport.setToken.bind(transport);
  transport.setToken = (token, meta) => {
    tokens.push(token);
    return originalSetToken(token, meta);
  };

  transport.emit(TransportEvent.MESSAGE, { type: 'error', code: 3001, message: 'session gone' });
  await flush();
  assert.ok(sent.some((message) => message.type === 'session.list'), '3001 refreshes the session list');

  transport.emit(TransportEvent.MESSAGE, { type: 'error', code: 1001, message: 'auth failed' });
  await flush();
  assert.ok(tokens.includes(null), '1001 clears the stored token');
  const error = find(ControllerEvent.ERROR).at(-1);
  assert.equal(error.payload.code, 1001);
  assert.equal(error.payload.retryable, false);

  await advance(MOCK_LATENCY.connect + MOCK_LATENCY.auth);
  await harness.online();
  await controller.openSession('demo-session-1');
  await flush();
  await controller.sendText('再发一条');
  transport.emit(TransportEvent.MESSAGE, { type: 'error', code: 4001, message: 'busy' });
  await flush();
  const busy = find(ControllerEvent.ERROR).at(-1);
  assert.equal(busy.payload.code, 4001);
  assert.equal(busy.payload.canRetry, true);
  assert.equal(controller.snapshot().phase, 'online', '4001 returns the session to online for a retry');
});

test('approval deadline expiry clears the sheet locally with default deny', async () => {
  const harness = await createHarness();
  const { controller, find, advance } = harness;
  await harness.online();
  await controller.openSession('demo-session-1');
  await flush();
  await controller.sendText('帮我删除文件');
  await advance(MOCK_LATENCY.tool);
  const approval = find(ControllerEvent.APPROVAL).at(-1).payload;
  assert.ok(approval);
  assert.equal(controller.snapshot().phase, 'approval-pending');

  // Mock deadline is now + 600s; advancing past it must expire locally.
  await advance(601_000);
  await flush();
  const lastApproval = find(ControllerEvent.APPROVAL).at(-1).payload;
  assert.equal(lastApproval, null, 'UI receives APPROVAL null so the sheet can close');
  assert.equal(controller.snapshot().approval, null);
  assert.ok(find(ControllerEvent.TOAST).some((event) => event.payload.key === 'toast.approvalExpired'));
});

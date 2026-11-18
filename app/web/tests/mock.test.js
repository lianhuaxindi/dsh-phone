import test from 'node:test';
import assert from 'node:assert/strict';

import {
  MockTransport,
  MOCK_PAIRING,
  MOCK_SCAN_RESULT,
  MOCK_SAMPLE_IMAGES,
  MOCK_SESSION_META,
  MOCK_SCENARIOS,
  MOCK_LATENCY,
  pickScenario,
  chunkText,
} from '../js/mock.js';
import { TransportEvent, TransportState } from '../js/transport.js';
import { ManualScheduler } from '../js/scheduler.js';
import { MsgType } from '../js/protocol.js';

const NOW = 1_700_000_000;

function createMock(options = {}) {
  const scheduler = new ManualScheduler(1000);
  const transport = new MockTransport({ scheduler, now: () => NOW, latency: {}, ...options });
  const events = { states: [], messages: [], pairs: [], auths: [], errors: [], misc: [] };
  transport.on(TransportEvent.STATE, ({ state, detail }) => events.states.push({ state, detail }));
  transport.on(TransportEvent.MESSAGE, (message) => events.messages.push(message));
  transport.on(TransportEvent.PAIR_RESULT, (result) => events.pairs.push(result));
  transport.on(TransportEvent.AUTH_RESULT, (result) => events.auths.push(result));
  transport.on(TransportEvent.ERROR, (error) => events.errors.push(error));
  transport.on(TransportEvent.EVENT, (event) => events.misc.push(event));
  return { transport, scheduler, events };
}

async function connectPairAndAuth({ transport, scheduler }) {
  transport.connect({ host: MOCK_PAIRING.host, port: MOCK_PAIRING.port, deviceId: 'dev-mock' });
  scheduler.advanceBy(MOCK_LATENCY.connect);
  transport.requestPair({ code: MOCK_PAIRING.code, deviceName: 'Test' });
  scheduler.advanceBy(MOCK_LATENCY.pair);
  scheduler.advanceBy(MOCK_LATENCY.auth);
  assert.equal(transport.state, TransportState.ONLINE);
}

test('scenario and chunk helpers', () => {
  assert.equal(pickScenario('帮我删除文件'), 'approval');
  assert.equal(pickScenario('请部署一下'), 'approval');
  assert.equal(pickScenario('选择方案 A'), 'question');
  assert.equal(pickScenario('这里报错了'), 'error');
  assert.equal(pickScenario('请给我一段长文'), 'long');
  assert.equal(pickScenario('语音测试'), 'audio');
  assert.equal(pickScenario('你好'), 'default');
  assert.ok(Object.keys(MOCK_SCENARIOS).length >= 5);

  const chunks = chunkText('第一句话。第二句话！第三句话？');
  assert.ok(chunks.length >= 2);
  assert.equal(chunks.join(''), '第一句话。第二句话！第三句话？');
  assert.deepEqual(chunkText(''), ['']);
});

test('mock connect → pairing → pair → auto auth → online', () => {
  const { transport, scheduler, events } = createMock();
  assert.equal(transport.kind, 'mock');
  transport.connect({ host: MOCK_PAIRING.host, port: MOCK_PAIRING.port, deviceId: 'dev-1' });
  assert.equal(transport.state, TransportState.CONNECTING);
  scheduler.advanceBy(MOCK_LATENCY.connect);
  assert.equal(transport.state, TransportState.PAIRING);

  transport.requestPair({ code: '000000' });
  scheduler.advanceBy(MOCK_LATENCY.pair);
  assert.equal(events.pairs.length, 1);
  assert.equal(events.pairs[0].ok, false);
  assert.equal(events.pairs[0].code, 1002);
  assert.equal(events.pairs[0].attemptsLeft, 4);
  assert.equal(transport.state, TransportState.PAIRING);

  transport.requestPair({ code: MOCK_PAIRING.code, deviceName: 'Pixel' });
  scheduler.advanceBy(MOCK_LATENCY.pair);
  assert.equal(events.pairs[1].ok, true);
  const states = events.states.map((s) => s.state);
  assert.ok(states.includes(TransportState.AUTHENTICATING));
  scheduler.advanceBy(MOCK_LATENCY.auth);
  assert.equal(transport.state, TransportState.ONLINE);
  assert.equal(events.auths.at(-1).ok, true);
  assert.ok(Array.isArray(events.auths.at(-1).sessions));
});

test('redundant auth when already online is acknowledged without reauthenticating', () => {
  const ctx = createMock();
  connectPairAndAuth(ctx);
  const before = ctx.events.auths.length;
  ctx.transport.requestAuth();
  assert.equal(ctx.events.auths.length, before + 1);
  assert.equal(ctx.events.auths.at(-1).already, true);
  assert.equal(ctx.transport.state, TransportState.ONLINE);
});

test('session.list replies with demo sessions and echoes _reqTo', () => {
  const { transport, scheduler, events } = createMock();
  transport.connect({});
  scheduler.advanceBy(MOCK_LATENCY.connect);
  transport.requestPair({ code: MOCK_PAIRING.code });
  scheduler.advanceBy(MOCK_LATENCY.pair + MOCK_LATENCY.auth);

  transport.send({ type: MsgType.SESSION_LIST, _reqId: 'req-1' });
  const reply = events.messages.find((m) => m.type === MsgType.SESSION_LIST_OK);
  assert.ok(reply, 'session.list.ok expected');
  assert.equal(reply._reqTo, 'req-1');
  assert.equal(reply.sessions.length, 2);
  assert.equal(reply.sessions[0].sessionId, 'demo-session-1');
});

test('chat.send streams ack → deltas → done, and releases the turn', () => {
  const ctx = createMock();
  const { transport, scheduler, events } = ctx;
  connectPairAndAuth(ctx);
  transport.send({
    type: MsgType.CHAT_SEND,
    clientId: 'client-1',
    sessionId: 'demo-session-1',
    text: '你好',
    _reqId: 'req-chat',
  });

  const ack = events.messages.find((m) => m.type === MsgType.CHAT_ACK);
  assert.ok(ack, 'chat.ack expected');
  assert.equal(ack.clientId, 'client-1');

  scheduler.advanceBy(MOCK_LATENCY.tool + MOCK_LATENCY.deltaGap * 200 + 1000);
  const deltas = events.messages.filter((m) => m.type === MsgType.ASSISTANT_DELTA);
  const done = events.messages.find((m) => m.type === MsgType.ASSISTANT_DONE);
  assert.ok(deltas.length > 3, `expected several deltas, got ${deltas.length}`);
  assert.equal(deltas[0].messageId, ack.messageId);
  assert.equal(deltas[0].index, 0);
  assert.ok(done);
  assert.equal(done.messageId, ack.messageId);
  assert.equal(done.finishReason, 'completed');

  // The demo backend accepts the next turn (busy flag cleared) and rejects
  // concurrent turns while streaming.
  transport.send({ type: MsgType.CHAT_SEND, clientId: 'client-2', sessionId: 'demo-session-1', text: '再来' });
  transport.send({ type: MsgType.CHAT_SEND, clientId: 'client-3', sessionId: 'demo-session-1', text: '插队' });
  const busy = events.messages.find((m) => m.type === MsgType.ERROR && m.code === 4001);
  assert.ok(busy, 'concurrent turn should hit 4001');
});

test('chat.cancel finalizes the streaming assistant message', () => {
  const ctx = createMock();
  const { transport, scheduler, events } = ctx;
  connectPairAndAuth(ctx);
  transport.send({ type: MsgType.CHAT_SEND, clientId: 'c1', sessionId: 'demo-session-1', text: '你好' });
  scheduler.advanceBy(MOCK_LATENCY.tool + MOCK_LATENCY.deltaGap * 3);
  transport.send({ type: MsgType.CHAT_CANCEL, sessionId: 'demo-session-1' });
  const done = events.messages.filter((m) => m.type === MsgType.ASSISTANT_DONE).at(-1);
  assert.ok(done);
  assert.equal(done.finishReason, 'cancelled');
  scheduler.advanceBy(5000);
  assert.equal(events.messages.filter((m) => m.type === MsgType.ASSISTANT_DONE).length, 1, 'no duplicate done');
});

test('approval scenario pushes tool status + approval.request and resolves both ways', () => {
  const ctx = createMock();
  const { transport, scheduler, events } = ctx;
  connectPairAndAuth(ctx);
  transport.send({ type: MsgType.CHAT_SEND, clientId: 'c1', sessionId: 'demo-session-1', text: '帮我删除文件' });
  scheduler.advanceBy(MOCK_LATENCY.tool);

  const tool = events.messages.find((m) => m.type === MsgType.TOOL_STATUS);
  const approval = events.messages.find((m) => m.type === MsgType.APPROVAL_REQUEST);
  assert.ok(tool, 'tool.status expected');
  assert.equal(tool.state, 'running');
  assert.ok(approval, 'approval.request expected');
  assert.equal(approval.tool, 'shell');
  assert.equal(approval.defaultOutcome, 'denied');
  assert.equal(approval.argsPreview, 'rm -rf ./dist && npm run build');
  assert.equal(approval.deadline, NOW + 600);

  transport.send({ type: MsgType.APPROVAL_RESPONSE, id: approval.id, outcome: 'denied' });
  scheduler.advanceBy(MOCK_LATENCY.deltaGap * 200 + 1000);
  const done = events.messages.filter((m) => m.type === MsgType.ASSISTANT_DONE).at(-1);
  assert.equal(done.finishReason, 'completed');
  const texts = events.messages.filter((m) => m.type === MsgType.ASSISTANT_DELTA).map((m) => m.text).join('');
  assert.match(texts, /拒绝/);
});

test('question scenario exposes single/multi questions and answers', () => {
  const ctx = createMock();
  const { transport, scheduler, events } = ctx;
  connectPairAndAuth(ctx);
  transport.send({ type: MsgType.CHAT_SEND, clientId: 'c1', sessionId: 'demo-session-1', text: '请帮我选择一个方案' });
  scheduler.advanceBy(MOCK_LATENCY.tool);
  const question = events.messages.find((m) => m.type === MsgType.QUESTION_REQUEST);
  assert.ok(question);
  assert.equal(question.questions.length, 2);
  assert.equal(question.questions[0].multiSelect, false);
  assert.equal(question.questions[1].multiSelect, true);
  transport.send({
    type: MsgType.QUESTION_RESPONSE,
    id: question.id,
    cancelled: false,
    answers: [{ questionId: 'q1', selected: ['a'], custom: null }],
  });
  scheduler.advanceBy(MOCK_LATENCY.deltaGap * 200 + 1000);
  const done = events.messages.filter((m) => m.type === MsgType.ASSISTANT_DONE).at(-1);
  assert.equal(done.finishReason, 'completed');
});

test('error scenario emits turn.error 3003 and an error-finished message', () => {
  const ctx = createMock();
  const { transport, scheduler, events } = ctx;
  connectPairAndAuth(ctx);
  transport.send({ type: MsgType.CHAT_SEND, clientId: 'c1', sessionId: 'demo-session-1', text: '这里报错' });
  scheduler.advanceBy(MOCK_LATENCY.tool + MOCK_LATENCY.firstDelta + MOCK_LATENCY.deltaGap * 10 + 1000);
  const turnError = events.messages.find((m) => m.type === MsgType.TURN_ERROR);
  assert.ok(turnError);
  assert.equal(turnError.code, 3003);
  const done = events.messages.filter((m) => m.type === MsgType.ASSISTANT_DONE).at(-1);
  assert.equal(done.finishReason, 'error');
});

test('expirePendingApproval pushes approval.expired and continues the turn', () => {
  const ctx = createMock();
  const { transport, scheduler, events } = ctx;
  connectPairAndAuth(ctx);
  transport.send({ type: MsgType.CHAT_SEND, clientId: 'c1', sessionId: 'demo-session-1', text: '部署一下' });
  scheduler.advanceBy(MOCK_LATENCY.tool);
  assert.equal(transport.expirePendingApproval(), true);
  const expired = events.messages.find((m) => m.type === MsgType.APPROVAL_EXPIRED);
  assert.ok(expired);
  assert.equal(expired.outcome, 'denied');
  assert.equal(transport.expirePendingApproval(), false, 'no pending approval left');
  scheduler.advanceBy(MOCK_LATENCY.deltaGap * 200 + 1000);
  assert.equal(events.messages.filter((m) => m.type === MsgType.ASSISTANT_DONE).length, 1);
});

test('audio PTT flow emits preheat → ready → route → levels → final', () => {
  const ctx = createMock();
  const { transport, scheduler, events } = ctx;
  connectPairAndAuth(ctx);
  transport.startAudio({ sampleRate: 16000, lang: 'zh', mode: 'ptt', sessionId: 'demo-session-1' });
  assert.equal(events.misc.at(-1).type, 'audio.state');
  assert.equal(events.misc.at(-1).state, 'preheating');
  scheduler.advanceBy(MOCK_LATENCY.preheat);
  const ready = events.misc.find((e) => e.type === 'audio.ready');
  assert.ok(ready);
  assert.deepEqual(ready.codecs, ['pcm16']);
  const route = events.misc.find((e) => e.type === 'audio.route');
  assert.equal(route.route, 'hfp');
  const level = events.misc.find((e) => e.type === 'audio.level');
  assert.ok(level && level.level >= 0 && level.level <= 1);

  scheduler.advanceBy(MOCK_LATENCY.firstPartial + MOCK_LATENCY.partialGap * 6);
  const partials = events.messages.filter((m) => m.type === MsgType.ASR_PARTIAL);
  assert.ok(partials.length > 0);
  assert.equal(partials.at(-1).text, '帮我总结一下今天的会议');

  transport.stopAudio();
  scheduler.advanceBy(MOCK_LATENCY.asrFinal);
  const final = events.messages.find((m) => m.type === MsgType.ASR_FINAL);
  assert.ok(final);
  assert.equal(final.text, '帮我总结一下今天的会议');
  assert.equal(final.durationMs, 2100);
  const stopped = events.misc.find((e) => e.type === 'audio.stopped');
  assert.ok(stopped);
});

test('audio is rejected when not online', () => {
  const { transport, events } = createMock();
  transport.startAudio({ mode: 'ptt' });
  assert.equal(events.misc.at(-1).type, 'audio.error');
  assert.equal(events.misc.at(-1).code, 'offline');
  assert.equal(events.errors.length, 1);
});

test('token, route, export and scan commands emit native-shaped events', () => {
  const ctx = createMock();
  const { transport, scheduler, events } = ctx;
  transport.getToken();
  assert.deepEqual(
    { type: events.misc.at(-1).type, hasToken: events.misc.at(-1).hasToken },
    { type: 'token', hasToken: false },
  );
  transport.routeInfo();
  assert.equal(events.misc.at(-1).route, 'phone');
  transport.preferDevice('WH-1000XM5');
  assert.equal(events.misc.at(-1).route, 'hfp');

  transport.exportLogs();
  scheduler.advanceBy(MOCK_LATENCY.export);
  const exported = events.misc.find((e) => e.type === 'logs.exported');
  assert.ok(exported);
  assert.match(exported.text, /DSH Phone web demo log/);
});

test('scanPairing resolves a v1.1 QR result with hosts/name (D-030)', async () => {
  const { transport, scheduler } = createMock();
  const pending = transport.scanPairing();
  scheduler.advanceBy(MOCK_LATENCY.scan);
  const result = await pending;
  assert.equal(result.ok, true);
  assert.equal(result.host, MOCK_SCAN_RESULT.host);
  assert.equal(result.code, MOCK_PAIRING.code);
  assert.deepEqual(result.hosts, MOCK_SCAN_RESULT.hosts);
  assert.equal(result.name, MOCK_SCAN_RESULT.name);
});

test('scanPairing and pickImage surface failures through hooks (D-029/030)', async () => {
  const { transport, scheduler } = createMock();
  transport.scanFailure = 'permission_denied';
  const scanPending = transport.scanPairing();
  scheduler.advanceBy(MOCK_LATENCY.scan);
  const scan = await scanPending;
  assert.equal(scan.ok, false);
  assert.equal(scan.reason, 'permission_denied');

  transport.pickFailure = 'too_large';
  const pickPending = transport.pickImage('camera');
  scheduler.advanceBy(MOCK_LATENCY.pick);
  const pick = await pickPending;
  assert.equal(pick.ok, false);
  assert.equal(pick.reason, 'too_large');
});

test('resync replays missed messages after a drop', () => {
  const ctx = createMock();
  const { transport, scheduler, events } = ctx;
  connectPairAndAuth(ctx);
  transport.send({ type: MsgType.CHAT_SEND, clientId: 'c1', sessionId: 'demo-session-1', text: '你好' });
  scheduler.advanceBy(MOCK_LATENCY.tool + MOCK_LATENCY.deltaGap * 2);
  const lastSeq = events.messages.at(-1).seq;
  const seenBefore = events.messages.length;

  transport.simulateClose({ code: 4408 });
  assert.equal(transport.state, TransportState.CLOSED);
  assert.equal(ctx.events.states.at(-1).detail.code, 4408);

  transport.send({ type: MsgType.RESYNC, lastSeq: lastSeq - 5, _reqId: 'res-1' });
  const ok = events.messages.find((m) => m.type === MsgType.RESYNC_OK);
  assert.ok(ok);
  assert.equal(ok._reqTo, 'res-1');
  assert.ok(events.messages.length > seenBefore + 1, 'missed pushes are replayed');
});

test('pendingTimers() reports leaks and clears on disconnect', () => {
  const ctx = createMock();
  const { transport, scheduler, scheduler: sched } = ctx;
  connectPairAndAuth(ctx);
  transport.startAudio({ mode: 'ptt' });
  assert.ok(transport.pendingTimers() > 0);
  transport.disconnect();
  assert.equal(transport.pendingTimers(), 0);
  assert.equal(ctx.events.states.at(-1).state, 'closed');
  assert.equal(sched.pendingCount(), 0);
  assert.equal(scheduler.now(), sched.now());
});

test('pickImage resolves an embedded demo image and media.get serves it back', async () => {
  const ctx = createMock();
  const { transport, scheduler, events } = ctx;
  connectPairAndAuth(ctx);

  const pickPending = transport.pickImage('gallery');
  scheduler.advanceBy(MOCK_LATENCY.pick);
  const picked = await pickPending;
  assert.equal(picked.ok, true);
  assert.equal(picked.mimeType, MOCK_SAMPLE_IMAGES.gallery.mimeType);
  assert.equal(picked.data, MOCK_SAMPLE_IMAGES.gallery.data);

  transport.send({
    type: MsgType.CHAT_SEND,
    clientId: 'img-1',
    sessionId: 'demo-session-1',
    text: '',
    images: [{ mimeType: picked.mimeType, data: picked.data, name: picked.name, width: picked.width, height: picked.height }],
  });
  scheduler.advanceBy(MOCK_LATENCY.tool + MOCK_LATENCY.deltaGap * 4);
  const userMessage = events.messages.find((m) => m.type === MsgType.CHAT_ACK);
  assert.ok(userMessage);
  const withImages = transport
    ._sessions
    .get('demo-session-1')
    .messages.find((m) => m.role === 'user' && Array.isArray(m.images) && m.images.length === 1);
  assert.ok(withImages, 'user message keeps image metadata');
  const mediaId = withImages.images[0].mediaId;
  assert.match(mediaId, /^md-demo-\d+$/);

  const mediaPending = transport.mediaGet(mediaId);
  scheduler.advanceBy(MOCK_LATENCY.media);
  const media = await mediaPending;
  assert.equal(media.ok, true);
  assert.equal(media.media.mediaId, mediaId);
  assert.equal(media.media.data, MOCK_SAMPLE_IMAGES.gallery.data);

  const missing = transport.mediaGet('md-missing');
  scheduler.advanceBy(MOCK_LATENCY.media);
  const notFound = await missing;
  assert.equal(notFound.ok, false);
  assert.equal(notFound.code, 3001);
});

test('session.resume pushes session.meta and done carries usage (D-032)', () => {
  const ctx = createMock();
  const { transport, scheduler, events } = ctx;
  connectPairAndAuth(ctx);

  transport.send({ type: MsgType.SESSION_RESUME, sessionId: 'demo-session-1', _reqId: 'resume-1' });
  const meta = events.messages.find((m) => m.type === MsgType.SESSION_META);
  assert.ok(meta, 'session.meta follows resume.ok');
  assert.equal(meta.model, MOCK_SESSION_META.default.model);
  assert.equal(meta.contextWindow, MOCK_SESSION_META.default.contextWindow);

  transport.send({ type: MsgType.CHAT_SEND, clientId: 'c-meta', sessionId: 'demo-session-1', text: '你好' });
  scheduler.advanceBy(10_000);
  const done = events.messages.find((m) => m.type === MsgType.ASSISTANT_DONE);
  assert.ok(done);
  assert.ok(done.usage, 'assistant.done includes usage');
  assert.ok(done.usage.usedTokens > 0);
  assert.equal(done.usage.contextWindow, MOCK_SESSION_META.default.contextWindow);
});

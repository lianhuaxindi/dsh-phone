import test from 'node:test';
import assert from 'node:assert/strict';

import {
  NativeTransport,
  WsTransport,
  TransportEvent,
  TransportKind,
  TransportState,
  normalizeInboundMessage,
  bridgeEventType,
  hasNativeBridge,
  defaultWsUrl,
  resolveWebTransportMode,
} from '../js/transport.js';
import { ManualScheduler } from '../js/scheduler.js';
import { PROTOCOL, CloseCode, ErrorCode } from '../js/protocol.js';

function fakeWindow({ bridge = true } = {}) {
  const posted = [];
  const scope = {
    DshBridge: {
      onEvent: null,
      onBack: () => false,
    },
  };
  if (bridge) {
    scope.DshNative = {
      post(raw) {
        posted.push(JSON.parse(raw));
      },
    };
  }
  return { scope, posted };
}

function createNative(options = {}) {
  const scheduler = new ManualScheduler(1000);
  const { scope, posted } = fakeWindow(options);
  const transport = new NativeTransport({ scope, scheduler });
  const events = { states: [], messages: [], pairs: [], auths: [], errors: [], misc: [] };
  transport.on(TransportEvent.STATE, ({ state, detail }) => events.states.push({ state, detail }));
  transport.on(TransportEvent.MESSAGE, (message) => events.messages.push(message));
  transport.on(TransportEvent.PAIR_RESULT, (result) => events.pairs.push(result));
  transport.on(TransportEvent.AUTH_RESULT, (result) => events.auths.push(result));
  transport.on(TransportEvent.ERROR, (error) => events.errors.push(error));
  transport.on(TransportEvent.EVENT, (event) => events.misc.push(event));
  const emitNative = (payload) => scope.DshBridge.onEvent(JSON.stringify(payload));
  return { transport, scope, posted, events, emitNative, scheduler };
}

test('normalizeInboundMessage accepts JSON strings and objects', () => {
  assert.deepEqual(normalizeInboundMessage('{"type":"pong","seq":2}'), { type: 'pong', seq: 2 });
  const object = { type: 'pong', seq: 3 };
  assert.deepEqual(normalizeInboundMessage(object), object);
  assert.throws(() => normalizeInboundMessage('{'), /invalid JSON/i);
  assert.throws(() => normalizeInboundMessage('[]'), /must be an object/i);
});

test('bridgeEventType tolerates every event-name field', () => {
  assert.equal(bridgeEventType({ type: 'net.state' }), 'net.state');
  assert.equal(bridgeEventType({ event: 'net.state' }), 'net.state');
  assert.equal(bridgeEventType({ eventType: 'net.state' }), 'net.state');
  assert.equal(bridgeEventType({ name: 'net.state' }), 'net.state');
  assert.equal(bridgeEventType({}), '');
  assert.equal(bridgeEventType(null), '');
});

test('hasNativeBridge / resolveWebTransportMode / defaultWsUrl', () => {
  const { scope } = fakeWindow();
  assert.equal(hasNativeBridge(scope), true);
  assert.equal(hasNativeBridge({}), false);
  assert.equal(hasNativeBridge(undefined), false);

  assert.equal(resolveWebTransportMode('native', scope), TransportKind.NATIVE);
  assert.equal(resolveWebTransportMode('ws', scope), TransportKind.WS);
  assert.equal(resolveWebTransportMode('mock', scope), TransportKind.MOCK);
  assert.equal(resolveWebTransportMode('auto', scope), TransportKind.NATIVE);
  assert.equal(resolveWebTransportMode('auto', {}), TransportKind.MOCK);
  assert.equal(resolveWebTransportMode(undefined, {}), TransportKind.MOCK);

  assert.equal(defaultWsUrl('127.0.0.1', 8765), 'wss://127.0.0.1:8765/ws');
  assert.equal(defaultWsUrl('127.0.0.1', 8765, false), 'ws://127.0.0.1:8765/ws');
});

test('NativeTransport installs window.DshBridge and posts JSON-string ops', () => {
  const { transport, scope, posted } = createNative();
  assert.equal(transport.kind, 'native');
  assert.equal(typeof scope.DshBridge.onEvent, 'function');
  assert.equal(typeof scope.DshBridge.onBack, 'function');

  transport.connect({ host: '192.168.1.5', port: 8765, fp: 'ab', deviceId: 'dev-1' });
  assert.deepEqual(posted[0], { op: 'connect', host: '192.168.1.5', port: 8765, fp: 'ab', deviceId: 'dev-1' });
  transport.requestPair({ code: '123456', deviceName: 'Pixel' });
  assert.deepEqual(posted[1], { op: 'pair', code: '123456', deviceName: 'Pixel' });
  transport.requestAuth();
  assert.deepEqual(posted[2], { op: 'auth' });
  transport.startAudio({ sampleRate: 16000, lang: 'zh', mode: 'ptt', sessionId: 's1' });
  assert.deepEqual(posted[3], { op: 'startAudio', sampleRate: 16000, lang: 'zh', mode: 'ptt', sessionId: 's1' });
  transport.stopAudio();
  assert.equal(posted[4].op, 'stopAudio');
  transport.cancelAudio();
  assert.equal(posted[5].op, 'cancelAudio');
  transport.reconnect();
  assert.equal(posted[6].op, 'reconnect');
  transport.getToken();
  assert.equal(posted[7].op, 'getToken');
  transport.setToken('tok', { host: 'h', port: 1, serverId: 'srv' });
  assert.deepEqual(posted[8], { op: 'setToken', token: 'tok', host: 'h', port: 1, serverId: 'srv' });
  transport.setToken(null);
  assert.deepEqual(posted[9], { op: 'setToken' }, 'empty setToken clears the stored token');
  transport.exportLogs();
  assert.equal(posted[10].op, 'exportLogs');
});

test('NativeTransport forwards _reqId inside the send json and strips seq', () => {
  const { transport, posted } = createNative();
  transport.send({ type: 'session.list', seq: 7, _reqId: 'r1' });
  assert.equal(posted.length, 1);
  assert.equal(posted[0].op, 'send');
  const json = JSON.parse(posted[0].json);
  assert.deepEqual(json, { type: 'session.list', _reqId: 'r1' });
  assert.equal('seq' in json, false);
  assert.equal('_reqId' in posted[0], false);
});

test('NativeTransport maps bridge events to transport events', () => {
  const { transport, events, emitNative } = createNative();

  emitNative({ type: 'net.state', state: 'connecting' });
  assert.equal(transport.state, TransportState.CONNECTING);
  emitNative({ type: 'net.state', state: 'online', serverId: 'srv-1' });
  assert.equal(transport.state, TransportState.ONLINE);
  assert.deepEqual(events.states.at(-1).detail, { serverId: 'srv-1' });

  emitNative({ type: 'pair.result', ok: true, serverId: 'srv-1' });
  assert.deepEqual(events.pairs.at(-1), { ok: true, serverId: 'srv-1', code: undefined, message: undefined, attemptsLeft: undefined, retryable: undefined });

  emitNative({ type: 'auth.result', ok: true, serverId: 'srv-1', sessions: [{ sessionId: 's1' }] });
  assert.equal(transport.state, TransportState.ONLINE);
  assert.equal(events.auths.at(-1).ok, true);
  assert.equal(events.auths.at(-1).sessions.length, 1);

  emitNative({ type: 'proto.message', message: { type: 'assistant.delta', sessionId: 's1', messageId: 'm1', text: 'hi', index: 0 } });
  assert.equal(events.messages.at(-1).type, 'assistant.delta');
  assert.equal(events.messages.at(-1).text, 'hi');

  emitNative({ type: 'audio.state', state: 'preheating' });
  assert.equal(events.misc.at(-1).type, 'audio.state');
  assert.equal(events.misc.at(-1).state, 'preheating');
  emitNative({ type: 'audio.route', route: 'hfp', deviceName: 'WH-1000XM5' });
  assert.equal(events.misc.at(-1).route, 'hfp');
  emitNative({ type: 'audio.level', level: 0.42 });
  assert.equal(events.misc.at(-1).level, 0.42);
  emitNative({ type: 'audio.stopped', aseq: 3 });
  assert.equal(events.misc.at(-1).aseq, 3);
  emitNative({ type: 'token', hasToken: true, host: 'h', port: 8765, serverId: 'srv-1' });
  assert.equal(events.misc.at(-1).type, 'token');
  assert.equal(events.misc.at(-1).hasToken, true);
  emitNative({ type: 'logs.exported', text: 'log body', truncated: false });
  assert.equal(events.misc.at(-1).text, 'log body');

  emitNative({ type: 'error', scope: 'audio', code: 5001, message: 'offline', retryable: true });
  assert.deepEqual(events.errors.at(-1), { scope: 'audio', code: 5001, message: 'offline', retryable: true });
});

test('NativeTransport scanPairing times out and resolves v1.1 results (D-030)', async () => {
  const { transport, scheduler, emitNative } = createNative();
  const result = transport.scanPairing();
  scheduler.advanceBy(transport.scanTimeoutMs);
  const timedOut = await result;
  assert.equal(timedOut.ok, false);
  assert.equal(timedOut.reason, 'timeout');

  const pending = transport.scanPairing();
  emitNative({
    type: 'scanPairing.result',
    ok: true,
    uri: 'dsphone://pair?v=1&host=192.168.1.10&port=8765&fp=' + 'a'.repeat(64) + '&code=123456&sid=srv-1',
    host: '192.168.1.10',
    port: 8765,
    fp: 'a'.repeat(64),
    code: '123456',
    sid: 'srv-1',
    hosts: ['10.5.19.39'],
    name: 'KQ-PC',
  });
  const scanned = await pending;
  assert.equal(scanned.ok, true);
  assert.equal(scanned.host, '192.168.1.10');
  assert.deepEqual(scanned.hosts, ['10.5.19.39']);
  assert.equal(scanned.name, 'KQ-PC');

  const failed = transport.scanPairing();
  emitNative({ type: 'scanPairing.result', ok: false, reason: 'permission_denied' });
  const failure = await failed;
  assert.equal(failure.ok, false);
  assert.equal(failure.reason, 'permission_denied');
});

test('NativeTransport onBack chains to the app back handler', () => {
  const { transport, scope } = createNative();
  assert.equal(scope.DshBridge.onBack(), false);
  transport.setBackHandler(() => true);
  assert.equal(scope.DshBridge.onBack(), true);
  transport.setBackHandler(() => false);
  assert.equal(scope.DshBridge.onBack(), false);
});

test('WsTransport refuses a negotiated subprotocol mismatch', () => {
  class FakeSocket {
    constructor(url, protocol) {
      this.url = url;
      this.protocol = protocol;
      this.readyState = 0;
      FakeSocket.instances.push(this);
    }

    close(code, reason) {
      this.closed = { code, reason };
      this.readyState = 3;
    }
  }
  FakeSocket.instances = [];
  const scheduler = new ManualScheduler(1000);
  const transport = new WsTransport({ WebSocketImpl: FakeSocket, storage: null, scheduler, urlFactory: (h, p, s) => `${s ? 'wss' : 'ws'}://${h}:${p}/ws` });
  const errors = [];
  const states = [];
  transport.on(TransportEvent.ERROR, (error) => errors.push(error));
  transport.on(TransportEvent.STATE, ({ state, detail }) => states.push({ state, detail }));

  transport.connect({ host: 'h', port: 1, deviceId: 'd', secure: false });
  const socket = FakeSocket.instances[0];
  assert.equal(socket.url, 'ws://h:1/ws');
  assert.equal(socket.protocol, PROTOCOL.subprotocol, 'ws constructor carries the frozen subprotocol');
  assert.equal(PROTOCOL.subprotocol, 'dsphone.v1');
  assert.match(PROTOCOL.subprotocol, /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/, 'RFC 6455 token chars only');

  socket.protocol = 'dsphone/1';
  socket.onopen();
  assert.equal(states.at(-1).state, TransportState.FAILED);
  assert.equal(states.at(-1).detail.reason, 'subprotocol');
  assert.equal(errors.at(-1).code, ErrorCode.VERSION_MISMATCH);
  assert.equal(errors.at(-1).retryable, false);
  assert.equal(socket.closed.code, CloseCode.VERSION);
});

test('WsTransport handshake stores the debug token and sends hello', async () => {
  class FakeSocket {
    constructor(url, protocol) {
      this.url = url;
      this.protocol = protocol;
      this.readyState = 1;
      this.sent = [];
      FakeSocket.instances.push(this);
    }

    send(data) {
      this.sent.push(JSON.parse(data));
    }

    close() {}
  }
  FakeSocket.instances = [];
  const scheduler = new ManualScheduler(1000);
  const storage = {
    data: new Map(),
    getItem(key) {
      return this.data.has(key) ? this.data.get(key) : null;
    },
    setItem(key, value) {
      this.data.set(key, String(value));
    },
    removeItem(key) {
      this.data.delete(key);
    },
  };
  const transport = new WsTransport({ WebSocketImpl: FakeSocket, storage, scheduler });
  transport.connect({ host: 'h', port: 1, deviceId: 'dev-1', secure: false });
  const socket = FakeSocket.instances[0];
  socket.protocol = PROTOCOL.subprotocol;
  socket.onopen();
  assert.equal(socket.sent[0].type, 'hello');
  assert.equal(socket.sent[0].v, 1);
  assert.equal(socket.sent[0].deviceId, 'dev-1');
  assert.equal('token' in socket.sent[0], false);

  socket.onmessage({ data: JSON.stringify({ type: 'hello.ok', seq: 1, needPair: true, serverId: 'srv' }) });
  assert.equal(transport.state, TransportState.PAIRING);

  socket.onmessage({ data: JSON.stringify({ type: 'pair.error', seq: 2, code: 1002, message: 'wrong', attemptsLeft: 3, retryable: true }) });
  assert.equal(transport.state, TransportState.PAIRING);

  socket.onmessage({ data: JSON.stringify({ type: 'pair.ok', seq: 3, deviceToken: 'dG9rZW4=', serverId: 'srv' }) });
  assert.equal(storage.data.get('dsphone.debugToken'), 'dG9rZW4=');
  // The debug transport computes auth HMAC asynchronously (WebCrypto), so let
  // the promise chain settle before asserting on the auth frame.
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.ok(
    socket.sent.some((m) => m.type === 'auth'),
    'auth follows pair.ok',
  );

  socket.onmessage({ data: JSON.stringify({ type: 'auth.ok', seq: 4, serverId: 'srv', sessions: [] }) });
  assert.equal(transport.state, TransportState.ONLINE);
  assert.ok(socket.sent.some((m) => m.type === 'resync'), 'debug transport runs resync itself');

  socket.onmessage({ data: JSON.stringify({ type: 'session.list.ok', seq: 5, replyTo: 99, sessions: [] }) });
  const lastMessage = socket.sent.at(-1);
  assert.ok(lastMessage);
});

test('NativeTransport pickImage settles image.picked / image.pickFailed (D-029)', async () => {
  const { transport, posted, emitNative } = createNative();
  const pending = transport.pickImage('camera');
  assert.equal(posted.at(-1).op, 'pickImage');
  assert.equal(posted.at(-1).source, 'camera');
  emitNative({
    type: 'image.picked',
    data: 'aGVsbG8=',
    mimeType: 'image/jpeg',
    name: 'photo.jpg',
    width: 1600,
    height: 1200,
  });
  const picked = await pending;
  assert.equal(picked.ok, true);
  assert.equal(picked.mimeType, 'image/jpeg');
  assert.equal(picked.width, 1600);

  const failed = transport.pickImage('gallery');
  emitNative({ type: 'image.pickFailed', reason: 'permission_denied' });
  const failure = await failed;
  assert.equal(failure.ok, false);
  assert.equal(failure.reason, 'permission_denied');
});

test('NativeTransport mediaGet correlates _reqId → _reqTo (PROTOCOL §9 v1.1)', async () => {
  const { transport, posted, emitNative, scheduler } = createNative();
  const pending = transport.mediaGet('md-1');
  const sent = posted.at(-1);
  assert.equal(sent.op, 'send');
  const json = JSON.parse(sent.json);
  assert.equal(json.type, 'media.get');
  assert.equal(json.mediaId, 'md-1');
  assert.equal(typeof json._reqId, 'string');

  emitNative({
    type: 'proto.message',
    message: {
      type: 'media.data',
      mediaId: 'md-1',
      mimeType: 'image/png',
      data: 'aGVsbG8=',
      _reqTo: json._reqId,
    },
  });
  const result = await pending;
  assert.equal(result.ok, true);
  assert.equal(result.media.mediaId, 'md-1');
  assert.equal(result.media.mimeType, 'image/png');
  scheduler.flush();
});

test('NativeTransport net.state error maps to FAILED with tried diagnostics (D-034)', () => {
  const { transport, events, emitNative } = createNative();
  emitNative({
    type: 'net.state',
    state: 'error',
    reason: 'all_addresses_failed',
    message: 'all addresses timed out',
    tried: [{ host: '192.168.1.10', port: 8765, elapsedMs: 3000, error: 'timeout' }],
  });
  assert.equal(events.states.at(-1).state, TransportState.FAILED);
  assert.equal(events.states.at(-1).detail.reason, 'all_addresses_failed');
  assert.equal(events.states.at(-1).detail.tried.length, 1);
});

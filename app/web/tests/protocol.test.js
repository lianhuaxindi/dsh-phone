import test from 'node:test';
import assert from 'node:assert/strict';

import {
  PROTOCOL,
  AUDIO,
  MsgType,
  ErrorCode,
  CloseCode,
  RecoveryAction,
  errorSpec,
  closeSpec,
  isKnownType,
  ProtocolError,
  ProtocolErrorKind,
  bytesToBase64,
  base64ToBytes,
  bytesToHex,
  hexToBytes,
  utf8Bytes,
  equalBytes,
  canonicalAuthString,
  signAuth,
  hmacSha256Base64,
  SeqCounter,
  assertEnvelope,
  parseMessage,
  encodeMessage,
  buildHello,
  buildPairRequest,
  buildAuth,
  buildResync,
  buildPing,
  buildSessionList,
  buildSessionCreate,
  buildSessionResume,
  buildSessionHistory,
  buildChatSend,
  buildChatCancel,
  buildAudioStart,
  buildAudioStop,
  buildApprovalResponse,
  buildQuestionResponse,
  buildReply,
  buildPush,
  buildPairUri,
  parsePairUri,
  validatePairingField,
  validateChatText,
  normalizeSession,
  normalizeMessage,
  normalizeSessionList,
  normalizeMessageList,
  normalizeApprovalRequest,
  normalizeQuestion,
  normalizeQuestionRequest,
  buildAudioFrame,
  buildAudioFrameHeader,
  parseAudioFrameHeader,
  isExpired,
  textFingerprint,
  simpleHash,
  randomUuid,
  randomNonceBase64,
} from '../js/protocol.js';

/* --------------------------------------------------------- PROTOCOL §6.3 vector */

const VECTOR = Object.freeze({
  deviceTokenHex: '00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff',
  deviceToken: 'ABEiM0RVZneImaq7zN3u/wARIjNEVWZ3iJmqu8zd7v8=',
  deviceId: 'dev-abc123',
  ts: 1710000000,
  nonce: 'bm9uY2UxMjM0NTY3OA==',
  canonical: 'DSPHONE1\ndev-abc123\n1710000000\nbm9uY2UxMjM0NTY3OA==',
  hmac: 'gYfqlKpD9PLEml5FzwSMmtd93hlDbF/JrA7lGcDBSjo=',
  hmacHex: '8187ea94aa43f4f2c49a5e45cf048c9ad77dde19436c5fc9ac0ee519c0c14a3a',
});

test('§6.3 canonical auth string matches the frozen test vector', () => {
  assert.equal(
    canonicalAuthString({ deviceId: VECTOR.deviceId, ts: VECTOR.ts, nonce: VECTOR.nonce }),
    VECTOR.canonical,
  );
});

test('§6.3 HMAC-SHA256 matches the frozen test vector (WebCrypto)', async () => {
  const hmac = await hmacSha256Base64(hexToBytes(VECTOR.deviceTokenHex), VECTOR.canonical);
  assert.equal(hmac, VECTOR.hmac);
  assert.equal(bytesToHex(base64ToBytes(hmac)), VECTOR.hmacHex);
});

test('signAuth returns canonical/hmac/nonce/ts and accepts an injected nonce', async () => {
  const signature = await signAuth({
    deviceId: VECTOR.deviceId,
    tokenBase64: VECTOR.deviceToken,
    ts: VECTOR.ts,
    nonce: VECTOR.nonce,
  });
  assert.equal(signature.canonical, VECTOR.canonical);
  assert.equal(signature.hmac, VECTOR.hmac);
  assert.equal(signature.ts, VECTOR.ts);
  assert.equal(signature.nonce, VECTOR.nonce);
});

test('base64/hex helpers round-trip and reject malformed input', () => {
  const bytes = Uint8Array.from([0x00, 0x11, 0x22, 0xff]);
  assert.equal(bytesToBase64(bytes), 'ABEi/w==');
  assert.deepEqual([...base64ToBytes('ABEi/w==')], [0x00, 0x11, 0x22, 0xff]);
  assert.equal(bytesToHex(Uint8Array.from([0xde, 0xad, 0xbe, 0xef])), 'deadbeef');
  assert.deepEqual([...hexToBytes('deadbeef')], [0xde, 0xad, 0xbe, 0xef]);
  assert.equal(equalBytes(base64ToBytes('ABEi/w=='), bytes), true);
  assert.throws(() => base64ToBytes('not base64!!'), ProtocolError);
  assert.throws(() => hexToBytes('zz'), ProtocolError);
});

test('random helpers produce protocol-sized values', () => {
  const uuid = randomUuid();
  assert.match(uuid, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  const nonce = randomNonceBase64(16);
  assert.equal(base64ToBytes(nonce).length, 16);
});

/* --------------------------------------------------------------- error specs */

test('error and close codes map to guidance-bearing specs', () => {
  for (const code of Object.values(ErrorCode)) {
    const spec = errorSpec(code);
    assert.ok(spec, `missing spec for ${code}`);
    assert.ok(Object.values(RecoveryAction).includes(spec.action), `bad action for ${code}`);
    assert.ok(spec.i18nKey.startsWith('error.'), `bad i18n key for ${code}`);
  }
  for (const code of Object.values(CloseCode)) {
    assert.ok(closeSpec(code), `missing close spec for ${code}`);
  }
  assert.equal(errorSpec(9999).action, RecoveryAction.DIAGNOSTICS);
  assert.equal(errorSpec(9999).retryable, false);
  assert.equal(closeSpec(4321).i18nKey, 'close.unknown');
  assert.equal(isKnownType(MsgType.CHAT_SEND), true);
  assert.equal(isKnownType('nope.nope'), false);
});

/* ------------------------------------------------------------------ envelope */

test('assertEnvelope requires type and positive seq', () => {
  assert.deepEqual(assertEnvelope({ type: 'ping', seq: 1 }), { type: 'ping', seq: 1 });
  assert.throws(() => assertEnvelope(null), ProtocolError);
  assert.throws(() => assertEnvelope({ type: '', seq: 1 }), ProtocolError);
  assert.throws(() => assertEnvelope({ type: 'ping', seq: 0 }), ProtocolError);
  assert.throws(() => assertEnvelope({ type: 'ping', seq: 1, replyTo: 0 }), ProtocolError);
});

test('parseMessage parses JSON strings and rejects bad JSON', () => {
  assert.deepEqual(parseMessage('{"type":"pong","seq":2}'), { type: 'pong', seq: 2 });
  assert.throws(
    () => parseMessage('{'),
    (error) => error instanceof ProtocolError && error.kind === ProtocolErrorKind.BAD_JSON,
  );
});

test('encodeMessage enforces the 64 KiB frame cap', () => {
  const text = encodeMessage({ type: 'ping', seq: 1, pad: 'x' });
  assert.equal(JSON.parse(text).type, 'ping');
  assert.throws(
    () => encodeMessage({ type: 'chat.send', seq: 1, text: 'x'.repeat(PROTOCOL.maxFrameBytes + 1) }),
    (error) => error instanceof ProtocolError && error.kind === ProtocolErrorKind.FRAME_TOO_LARGE,
  );
});

/* ---------------------------------------------------------------- seq counter */

test('SeqCounter allocates monotonic sequences and resets', () => {
  const counter = new SeqCounter(1);
  assert.equal(counter.peek(), 1);
  assert.equal(counter.next(), 1);
  assert.equal(counter.next(), 2);
  assert.equal(counter.value, 3);
  counter.reset(5);
  assert.equal(counter.next(), 5);
  assert.throws(() => counter.reset(0), ProtocolError);
});

/* ------------------------------------------------------------------ builders */

test('builders allocate seq when a counter is passed and omit it for the native bridge', async () => {
  const counter = new SeqCounter(1);
  const hello = buildHello(counter, { deviceId: 'dev-1', appVersion: '1.0.0' });
  assert.equal(hello.type, MsgType.HELLO);
  assert.equal(hello.seq, 1);
  assert.equal(hello.v, PROTOCOL.version);

  const fromBridge = buildHello(null, { deviceId: 'dev-1', appVersion: '1.0.0' });
  assert.equal(JSON.stringify(fromBridge).includes('"seq"'), false, 'native bridge owns seq; builders must omit it');

  const pair = buildPairRequest(counter, { deviceId: 'dev-1', code: '123456', deviceName: 'Pixel' });
  assert.equal(pair.seq, 2);
  assert.equal(pair.code, '123456');
  assert.equal(pair.pubKey, null);
  assert.throws(() => buildPairRequest(counter, { deviceId: 'dev-1', code: '12ab56' }), ProtocolError);

  const auth = await buildAuth(counter, {
    deviceId: VECTOR.deviceId,
    tokenBase64: VECTOR.deviceToken,
    ts: VECTOR.ts,
    nonce: VECTOR.nonce,
  });
  assert.equal(auth.type, MsgType.AUTH);
  assert.equal(auth.hmac, VECTOR.hmac);
  assert.equal(auth.seq, 3);

  assert.equal(buildResync(counter, { lastSeq: 9 }).lastSeq, 9);
  assert.equal(buildPing(counter).type, MsgType.PING);
  assert.equal(buildSessionList(counter).type, MsgType.SESSION_LIST);
  assert.equal(buildSessionCreate(counter, {}).type, MsgType.SESSION_CREATE);
  assert.equal(buildSessionResume(counter, { sessionId: 's1' }).sessionId, 's1');
  assert.equal(buildSessionHistory(counter, { sessionId: 's1' }).limit, 50);
  assert.equal(buildChatSend(counter, { sessionId: 's1', text: 'hi', clientId: 'c1' }).clientId, 'c1');
  assert.equal(buildChatCancel(counter, { sessionId: 's1' }).sessionId, 's1');
  assert.equal(buildAudioStart(counter, { sessionId: 's1' }).sampleRate, AUDIO.sampleRate);
  assert.equal(buildAudioStop(counter, { aseq: 4 }).aseq, 4);
  assert.equal(buildApprovalResponse(counter, { id: 'a1', outcome: 'denied' }).outcome, 'denied');
  const answer = buildQuestionResponse(counter, {
    id: 'q1',
    answers: [{ questionId: 'q1', selected: ['o1'], custom: null }],
  });
  assert.equal(answer.answers.length, 1);
  assert.throws(() => buildApprovalResponse(counter, { id: 'a1', outcome: 'maybe' }), ProtocolError);
});

test('server-side reply/push builders echo replyTo', () => {
  const counter = new SeqCounter(100);
  const reply = buildReply(counter, MsgType.PONG, 7, { ts: 1 });
  assert.equal(reply.type, MsgType.PONG);
  assert.equal(reply.replyTo, 7);
  assert.equal(reply.seq, 100);
  const push = buildPush(counter, MsgType.ASSISTANT_DELTA, { text: 'x' });
  assert.equal(push.type, MsgType.ASSISTANT_DELTA);
  assert.equal(push.seq, 101);
});

/* ------------------------------------------------------------------- pairing */

test('pairing URI round-trips host/port/fp/code and validates fields', () => {
  const fp = 'a3f1c9d2e4b60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9a';
  const uri = buildPairUri({ host: '192.168.1.5', port: 8765, fp, code: '123456' });
  assert.match(uri, /^dsphone:\/\/pair\?v=1&host=/);
  const parsed = parsePairUri(uri);
  assert.equal(parsed.v, 1);
  assert.equal(parsed.host, '192.168.1.5');
  assert.equal(parsed.port, 8765);
  assert.equal(parsed.fp, fp);
  assert.equal(parsed.code, '123456');
  assert.throws(() => parsePairUri('https://example.com'), ProtocolError);
  assert.throws(() => parsePairUri('dsphone://pair?v=2&host=a&port=1'), ProtocolError);

  assert.equal(validatePairingField('host', '192.168.1.5'), '192.168.1.5');
  assert.equal(validatePairingField('port', '8765'), 8765);
  assert.equal(validatePairingField('port', 8765), 8765);
  assert.equal(validatePairingField('fp', fp), fp);
  assert.equal(validatePairingField('code', '123456'), '123456');
  assert.throws(() => validatePairingField('host', ''), ProtocolError);
  assert.throws(() => validatePairingField('port', 0), ProtocolError);
  assert.throws(() => validatePairingField('fp', 'abc'), ProtocolError);
  assert.throws(() => validatePairingField('code', '12'), ProtocolError);
});

/* ------------------------------------------------------------------- chat text */

test('validateChatText enforces non-empty and 32000 char caps', () => {
  assert.equal(validateChatText(' hello '), ' hello ');
  assert.throws(() => validateChatText('   '), ProtocolError);
  assert.throws(() => validateChatText('x'.repeat(PROTOCOL.maxTextChars + 1)), ProtocolError);
  assert.equal(validateChatText('x'.repeat(PROTOCOL.maxTextChars)).length, PROTOCOL.maxTextChars);
});

/* ---------------------------------------------------------------- normalizers */

test('normalizers accept protocol payloads and default optional fields', () => {
  const session = normalizeSession({ sessionId: 's1', title: 'Hello', updatedAt: 10, lastPreview: 'preview' });
  assert.deepEqual(session, { sessionId: 's1', title: 'Hello', updatedAt: 10, lastPreview: 'preview' });
  assert.throws(() => normalizeSession({}), ProtocolError);

  const message = normalizeMessage({
    messageId: 'm1',
    sessionId: 's1',
    role: 'assistant',
    text: 'hi',
    status: 'done',
    createdAt: 5,
  });
  assert.equal(message.role, 'assistant');
  assert.throws(() => normalizeMessage({ messageId: 'm1', role: 'narrator' }), ProtocolError);

  const sessions = normalizeSessionList([
    { sessionId: 's1', title: 'A', updatedAt: 2 },
    { sessionId: 's2', title: 'B', updatedAt: 1 },
  ]);
  assert.equal(sessions.length, 2);

  const messages = normalizeMessageList([{ messageId: 'm1', sessionId: 's1', role: 'user', text: 'x' }]);
  assert.equal(messages[0].role, 'user');
  assert.equal(messages[0].status, 'done');
});

test('approval/question normalizers keep default-deny semantics', () => {
  const approval = normalizeApprovalRequest({
    type: 'approval.request',
    seq: 3,
    id: 'a1',
    sessionId: 's1',
    tool: 'shell',
    reason: 'rm -rf dist',
    argsPreview: 'rm -rf ./dist',
    deadline: 1_700_000_600,
  });
  assert.equal(approval.id, 'a1');
  assert.equal(approval.defaultOutcome, 'denied');
  assert.equal(approval.tool, 'shell');

  const question = normalizeQuestionRequest({
    type: 'question.request',
    seq: 4,
    id: 'q1',
    sessionId: 's1',
    questions: [
      {
        id: 'q1',
        question: 'Pick',
        multiSelect: false,
        required: true,
        allowCustom: true,
        options: [{ id: 'o1', label: 'A' }],
      },
    ],
  });
  assert.equal(question.questions.length, 1);
  assert.equal(question.questions[0].options[0].label, 'A');
  assert.throws(() => normalizeQuestionRequest({ id: 'q1', questions: [] }), ProtocolError);
  assert.equal(normalizeQuestion({ id: 'q', question: 'x' }).question, 'x');
});

/* --------------------------------------------------------------------- audio */

test('audio frames carry the 7-byte header plus PCM payload', () => {
  const pcm = new Uint8Array(AUDIO.pcmFrameBytes);
  pcm.fill(0x11);
  const frame = buildAudioFrame(7, 123456, pcm);
  assert.equal(frame.length, AUDIO.headerBytes + AUDIO.pcmFrameBytes);
  assert.equal(frame[0], AUDIO.frameTypePcm16);
  const header = parseAudioFrameHeader(frame);
  assert.equal(header.aseq, 7);
  assert.equal(header.tsMs, 123456);
  assert.equal(header.frameType, AUDIO.frameTypePcm16);
  assert.throws(() => buildAudioFrameHeader(0, 0), ProtocolError);
  assert.throws(() => buildAudioFrameHeader(1, 2 ** 32), ProtocolError);
  assert.equal(buildAudioFrameHeader(1, 2).length, AUDIO.headerBytes);
});

/* ---------------------------------------------------------------- misc helpers */

test('protocol constants match PROTOCOL v1 (D-015 subprotocol)', () => {
  assert.equal(PROTOCOL.version, 1);
  assert.equal(PROTOCOL.subprotocol, 'dsphone.v1');
  // RFC 6455 §4.1: a subprotocol must be a token, so `/` is not allowed.
  assert.match(PROTOCOL.subprotocol, /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/);
  assert.equal(PROTOCOL.maxTextChars, 32000);
  assert.equal(PROTOCOL.maxFrameBytes, 64 * 1024);
  assert.equal(AUDIO.sampleRate, 16000);
  assert.equal(AUDIO.channels, 1);
  assert.equal(AUDIO.bitDepth, 16);
});

test('time/text helpers are stable', () => {
  assert.equal(isExpired(1000, 1001), true);
  assert.equal(isExpired(1000, 999), false);
  const fingerprint = textFingerprint('hello');
  assert.deepEqual(fingerprint, { hashPrefix: textFingerprint('hello').hashPrefix, length: 5 });
  assert.notEqual(textFingerprint('hello').hashPrefix, textFingerprint('hellp').hashPrefix);
  assert.equal(simpleHash('abc'), simpleHash('abc'));
  assert.equal(utf8Bytes('手机').length, 6);
});

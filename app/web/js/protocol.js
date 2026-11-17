/**
 * DSH Phone protocol v1 —message definitions, envelope handling, error codes
 * and test-vector-backed auth helpers.
 *
 * Source of truth: `docs/PROTOCOL.md` v1.0 FROZEN. Keep this file free of DOM
 * APIs and of app state: everything here is a pure function/class so it can be
 * unit-tested with `node --test` and reused by both transports.
 */

/* ------------------------------------------------------------------------- */
/* Constants                                                                  */
/* ------------------------------------------------------------------------- */

/** Protocol-wide constants (PROTOCOL §1, §13). */
export const PROTOCOL = Object.freeze({
  name: 'dsphone',
  version: 1,
  subprotocol: 'dsphone.v1',
  /** Max text message length in characters (PROTOCOL §13). */
  maxTextChars: 32000,
  /** Max JSON text frame size in bytes (PROTOCOL §1). */
  maxFrameBytes: 64 * 1024,
  /** Client heartbeat interval / server silence timeout (PROTOCOL §7). */
  pingIntervalMs: 15000,
  serverSilenceTimeoutMs: 45000,
  /** Reconnect backoff seconds (PROTOCOL §7). */
  reconnectSeconds: Object.freeze([1, 2, 4, 8, 15]),
  /** S→C replay buffer used by resync (PROTOCOL §7). */
  replayBuffer: 512,
  /** A single audio stream is capped at 120 s (PROTOCOL §10.1). */
  maxAudioStreamMs: 120000,
});

/** Frames / audio constants (PROTOCOL §10.2). */
export const AUDIO = Object.freeze({
  frameTypePcm16: 0x01,
  frameTypeOpus: 0x02,
  headerBytes: 7,
  sampleRate: 16000,
  channels: 1,
  bitDepth: 16,
  frameMs: 20,
  pcmFrameBytes: 640,
  aseqMax: 65535,
});

/** All protocol message types, grouped by area. Never use raw strings elsewhere. */
export const MsgType = Object.freeze({
  HELLO: 'hello',
  HELLO_OK: 'hello.ok',
  PAIR_REQUEST: 'pair.request',
  PAIR_OK: 'pair.ok',
  PAIR_ERROR: 'pair.error',
  AUTH: 'auth',
  AUTH_OK: 'auth.ok',
  AUTH_ERROR: 'auth.error',
  RESYNC: 'resync',
  RESYNC_OK: 'resync.ok',
  PING: 'ping',
  PONG: 'pong',
  SESSION_LIST: 'session.list',
  SESSION_LIST_OK: 'session.list.ok',
  SESSION_CREATE: 'session.create',
  SESSION_CREATE_OK: 'session.create.ok',
  SESSION_RESUME: 'session.resume',
  SESSION_RESUME_OK: 'session.resume.ok',
  SESSION_HISTORY: 'session.history',
  SESSION_HISTORY_OK: 'session.history.ok',
  CHAT_SEND: 'chat.send',
  CHAT_ACK: 'chat.ack',
  CHAT_CANCEL: 'chat.cancel',
  ASSISTANT_DELTA: 'assistant.delta',
  ASSISTANT_DONE: 'assistant.done',
  TOOL_STATUS: 'tool.status',
  TURN_ERROR: 'turn.error',
  AUDIO_START: 'audio.start',
  AUDIO_READY: 'audio.ready',
  AUDIO_STOP: 'audio.stop',
  ASR_PARTIAL: 'asr.partial',
  ASR_FINAL: 'asr.final',
  APPROVAL_REQUEST: 'approval.request',
  APPROVAL_RESPONSE: 'approval.response',
  APPROVAL_EXPIRED: 'approval.expired',
  QUESTION_REQUEST: 'question.request',
  QUESTION_RESPONSE: 'question.response',
  QUESTION_EXPIRED: 'question.expired',
  ERROR: 'error',
});

/** Protocol error codes (PROTOCOL §12). */
export const ErrorCode = Object.freeze({
  AUTH_FAILED: 1001,
  PAIR_CODE: 1002,
  VERSION_MISMATCH: 1003,
  AUDIO_BUSY: 2001,
  ASR_UNAVAILABLE: 2002,
  SESSION_NOT_FOUND: 3001,
  APPROVAL_TIMEOUT: 3002,
  MODEL_ERROR: 3003,
  SERVER_BUSY: 4001,
  INTERNAL: 5001,
  SDK_PROTOCOL: 5002,
});

/** UI recovery action attached to an error code. Views map these to i18n keys. */
export const RecoveryAction = Object.freeze({
  NONE: 'none',
  REPAIR: 'repair',
  REENTER_PAIR_CODE: 'reenter-pair-code',
  UPGRADE: 'upgrade',
  WAIT: 'wait',
  CHECK_ASR: 'check-asr',
  REFRESH_SESSIONS: 'refresh-sessions',
  APPROVAL_DEFAULTED: 'approval-defaulted',
  RETRY: 'retry',
  RETRY_LATER: 'retry-later',
  DIAGNOSTICS: 'diagnostics',
  WAIT_PAIR_LOCK: 'wait-pair-lock',
  RECONNECT: 'reconnect',
});

/** Error code metadata. `i18nKey` is resolved by i18n.js (title/next strings). */
export const ERROR_SPECS = Object.freeze({
  [ErrorCode.AUTH_FAILED]: { retryable: false, action: RecoveryAction.REPAIR, i18nKey: 'error.1001' },
  [ErrorCode.PAIR_CODE]: { retryable: true, action: RecoveryAction.REENTER_PAIR_CODE, i18nKey: 'error.1002' },
  [ErrorCode.VERSION_MISMATCH]: { retryable: false, action: RecoveryAction.UPGRADE, i18nKey: 'error.1003' },
  [ErrorCode.AUDIO_BUSY]: { retryable: true, action: RecoveryAction.WAIT, i18nKey: 'error.2001' },
  [ErrorCode.ASR_UNAVAILABLE]: { retryable: true, action: RecoveryAction.CHECK_ASR, i18nKey: 'error.2002' },
  [ErrorCode.SESSION_NOT_FOUND]: { retryable: false, action: RecoveryAction.REFRESH_SESSIONS, i18nKey: 'error.3001' },
  [ErrorCode.APPROVAL_TIMEOUT]: { retryable: true, action: RecoveryAction.APPROVAL_DEFAULTED, i18nKey: 'error.3002' },
  [ErrorCode.MODEL_ERROR]: { retryable: true, action: RecoveryAction.RETRY, i18nKey: 'error.3003' },
  [ErrorCode.SERVER_BUSY]: { retryable: true, action: RecoveryAction.RETRY_LATER, i18nKey: 'error.4001' },
  [ErrorCode.INTERNAL]: { retryable: true, action: RecoveryAction.DIAGNOSTICS, i18nKey: 'error.5001' },
  [ErrorCode.SDK_PROTOCOL]: { retryable: false, action: RecoveryAction.DIAGNOSTICS, i18nKey: 'error.5002' },
});

/** WebSocket close codes (PROTOCOL §12). */
export const CloseCode = Object.freeze({
  NORMAL: 1000,
  INTERNAL: 1011,
  PROTOCOL: 4400,
  AUTH: 4401,
  VERSION: 4402,
  HEARTBEAT: 4408,
  PAIR_LOCKED: 4410,
});

export const CLOSE_SPECS = Object.freeze({
  [CloseCode.NORMAL]: { retryable: false, action: RecoveryAction.NONE, i18nKey: 'close.1000' },
  [CloseCode.INTERNAL]: { retryable: true, action: RecoveryAction.RECONNECT, i18nKey: 'close.1011' },
  [CloseCode.PROTOCOL]: { retryable: false, action: RecoveryAction.DIAGNOSTICS, i18nKey: 'close.4400' },
  [CloseCode.AUTH]: { retryable: false, action: RecoveryAction.REPAIR, i18nKey: 'close.4401' },
  [CloseCode.VERSION]: { retryable: false, action: RecoveryAction.UPGRADE, i18nKey: 'close.4402' },
  [CloseCode.HEARTBEAT]: { retryable: true, action: RecoveryAction.RECONNECT, i18nKey: 'close.4408' },
  [CloseCode.PAIR_LOCKED]: { retryable: false, action: RecoveryAction.WAIT_PAIR_LOCK, i18nKey: 'close.4410' },
});

const KNOWN_TYPES = new Set(Object.values(MsgType));
const ERROR_SPEC_DEFAULT = Object.freeze({
  retryable: false,
  action: RecoveryAction.DIAGNOSTICS,
  i18nKey: 'error.unknown',
});

/** Metadata for a protocol error code; unknown codes fall back safely. */
export function errorSpec(code) {
  const n = Number(code);
  return ERROR_SPECS[n] ?? ERROR_SPEC_DEFAULT;
}

/** Metadata for a WebSocket close code; unknown codes fall back safely. */
export function closeSpec(code) {
  const n = Number(code);
  return CLOSE_SPECS[n] ?? Object.freeze({ retryable: true, action: RecoveryAction.RECONNECT, i18nKey: 'close.unknown' });
}

/** True when `type` is one of the frozen v1 message types. */
export function isKnownType(type) {
  return KNOWN_TYPES.has(type);
}

/* ------------------------------------------------------------------------- */
/* Errors                                                                     */
/* ------------------------------------------------------------------------- */

/** Kinds of protocol failures the client can distinguish. */
export const ProtocolErrorKind = Object.freeze({
  BAD_JSON: 'bad-json',
  INVALID_MESSAGE: 'invalid-message',
  FRAME_TOO_LARGE: 'frame-too-large',
  VALIDATION: 'validation',
});

/** Error raised for malformed/unsendable protocol data. */
export class ProtocolError extends Error {
  /**
   * @param {string} message human readable detail (for diagnostics only)
   * @param {{kind?: string, code?: number, cause?: unknown}} [options]
   */
  constructor(message, { kind = ProtocolErrorKind.INVALID_MESSAGE, code, cause } = {}) {
    super(message);
    this.name = 'ProtocolError';
    this.kind = kind;
    this.code = code ?? 0;
    if (cause !== undefined) this.cause = cause;
  }
}

/* ------------------------------------------------------------------------- */
/* Base64 / hex / bytes helpers (no Buffer, no atob in Node)                  */
/* ------------------------------------------------------------------------- */

const B64_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

/** Encode bytes to standard base64 with padding. */
export function bytesToBase64(bytes) {
  const view = toUint8Array(bytes);
  let out = '';
  for (let i = 0; i < view.length; i += 3) {
    const b0 = view[i];
    const b1 = i + 1 < view.length ? view[i + 1] : 0;
    const b2 = i + 2 < view.length ? view[i + 2] : 0;
    out += B64_ALPHABET[b0 >> 2];
    out += B64_ALPHABET[((b0 & 0x03) << 4) | (b1 >> 4)];
    out += i + 1 < view.length ? B64_ALPHABET[((b1 & 0x0f) << 2) | (b2 >> 6)] : '=';
    out += i + 2 < view.length ? B64_ALPHABET[b2 & 0x3f] : '=';
  }
  return out;
}

/** Decode standard base64 (padding optional) into a Uint8Array. */
export function base64ToBytes(text) {
  if (typeof text !== 'string') throw new ProtocolError('base64 value must be a string', { kind: ProtocolErrorKind.VALIDATION });
  const clean = text.replace(/[\r\n\s]/g, '');
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(clean) || clean.length % 4 === 1) {
    throw new ProtocolError('invalid base64 value', { kind: ProtocolErrorKind.VALIDATION });
  }
  const padded = clean.endsWith('=') ? clean : clean + '='.repeat((4 - (clean.length % 4)) % 4);
  const out = new Uint8Array((padded.length / 4) * 3);
  let o = 0;
  for (let i = 0; i < padded.length; i += 4) {
    const c0 = B64_ALPHABET.indexOf(padded[i]);
    const c1 = B64_ALPHABET.indexOf(padded[i + 1]);
    const c2 = padded[i + 2] === '=' ? -1 : B64_ALPHABET.indexOf(padded[i + 2]);
    const c3 = padded[i + 3] === '=' ? -1 : B64_ALPHABET.indexOf(padded[i + 3]);
    if (c0 < 0 || c1 < 0) throw new ProtocolError('invalid base64 value', { kind: ProtocolErrorKind.VALIDATION });
    out[o++] = (c0 << 2) | (c1 >> 4);
    if (c2 >= 0) out[o++] = ((c1 & 0x0f) << 4) | (c2 >> 2);
    if (c3 >= 0) out[o++] = ((c2 & 0x03) << 6) | c3;
  }
  return out.subarray(0, o);
}

/** Encode bytes as lowercase hex. */
export function bytesToHex(bytes) {
  return Array.from(toUint8Array(bytes), (b) => b.toString(16).padStart(2, '0')).join('');
}

/** Decode a lowercase/uppercase hex string into bytes. */
export function hexToBytes(hex) {
  if (typeof hex !== 'string' || hex.length % 2 !== 0 || !/^[0-9a-fA-F]*$/.test(hex)) {
    throw new ProtocolError('invalid hex value', { kind: ProtocolErrorKind.VALIDATION });
  }
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i += 1) out[i] = Number.parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

/** UTF-8 encode a string. */
export function utf8Bytes(text) {
  return new TextEncoder().encode(String(text));
}

/** Constant-time-ish compare of two byte arrays; returns boolean. */
export function equalBytes(a, b) {
  const x = toUint8Array(a);
  const y = toUint8Array(b);
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i += 1) diff |= x[i] ^ y[i];
  return diff === 0;
}

function toUint8Array(value) {
  if (value instanceof Uint8Array) return value;
  if (Array.isArray(value)) return Uint8Array.from(value);
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  throw new ProtocolError('expected bytes', { kind: ProtocolErrorKind.VALIDATION });
}

/* ------------------------------------------------------------------------- */
/* Random / time helpers                                                      */
/* ------------------------------------------------------------------------- */

/** Current Unix time in seconds. */
export function unixNow() {
  return Math.floor(Date.now() / 1000);
}

/** Cryptographically random bytes via WebCrypto (available in WebView + Node 20+). */
export function randomBytes(length) {
  const out = new Uint8Array(length);
  const cryptoObj = globalThis.crypto;
  if (!cryptoObj?.getRandomValues) throw new ProtocolError('WebCrypto unavailable');
  cryptoObj.getRandomValues(out);
  return out;
}

/** Random base64 nonce (16 bytes by default, PROTOCOL §6.1). */
export function randomNonceBase64(byteLength = 16) {
  return bytesToBase64(randomBytes(byteLength));
}

/** RFC4122 v4 UUID; falls back to a random implementation when needed. */
export function randomUuid() {
  const cryptoObj = globalThis.crypto;
  if (cryptoObj?.randomUUID) return cryptoObj.randomUUID();
  const bytes = randomBytes(16);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const h = bytesToHex(bytes);
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

/* ------------------------------------------------------------------------- */
/* Auth helpers (PROTOCOL §6)                                                 */
/* ------------------------------------------------------------------------- */

/**
 * Canonical string signed by `auth` (PROTOCOL §6.1):
 * `DSPHONE1\n{deviceId}\n{ts}\n{nonce}` —no trailing newline.
 */
export function canonicalAuthString({ deviceId, ts, nonce }) {
  if (typeof deviceId !== 'string' || !deviceId) {
    throw new ProtocolError('deviceId is required', { kind: ProtocolErrorKind.VALIDATION });
  }
  if (!Number.isInteger(ts) || ts <= 0) {
    throw new ProtocolError('ts must be a positive Unix timestamp in seconds', { kind: ProtocolErrorKind.VALIDATION });
  }
  if (typeof nonce !== 'string' || !nonce) {
    throw new ProtocolError('nonce is required', { kind: ProtocolErrorKind.VALIDATION });
  }
  return `DSPHONE1\n${deviceId}\n${ts}\n${nonce}`;
}

/**
 * HMAC-SHA256 over the canonical auth string, base64 encoded.
 * @param {Uint8Array|number[]} keyBytes 32-byte device token
 * @param {string} canonical UTF-8 canonical string
 * @param {Crypto} [cryptoImpl] injectable WebCrypto for tests
 */
export async function hmacSha256Base64(keyBytes, canonical, cryptoImpl = globalThis.crypto) {
  if (!cryptoImpl?.subtle) throw new ProtocolError('WebCrypto subtle API unavailable');
  const key = await cryptoImpl.subtle.importKey('raw', toUint8Array(keyBytes), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const signature = await cryptoImpl.subtle.sign('HMAC', key, utf8Bytes(canonical));
  return bytesToBase64(new Uint8Array(signature));
}

/**
 * Build the `auth` signature fields for a device token.
 * The raw token is never stored by this module.
 */
export async function signAuth({ deviceId, tokenBase64, ts = unixNow(), nonce = randomNonceBase64(), cryptoImpl = globalThis.crypto }) {
  const keyBytes = base64ToBytes(tokenBase64);
  const canonical = canonicalAuthString({ deviceId, ts, nonce });
  const hmac = await hmacSha256Base64(keyBytes, canonical, cryptoImpl);
  return { canonical, hmac, nonce, ts };
}

/* ------------------------------------------------------------------------- */
/* Envelope                                                                   */
/* ------------------------------------------------------------------------- */

/** Monotonic per-direction sequence counter (PROTOCOL §2). */
export class SeqCounter {
  constructor(start = 1) {
    this.reset(start);
  }

  reset(start = 1) {
    if (!Number.isInteger(start) || start < 1) throw new ProtocolError('seq start must be a positive integer');
    /** Next sequence number to emit. */
    this.value = start;
    return this;
  }

  /** Consume and return the next sequence number. */
  next() {
    const seq = this.value;
    this.value += 1;
    return seq;
  }

  /** Peek without consuming. */
  peek() {
    return this.value;
  }
}

function isPlainObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Validate the JSON envelope shared by every message (PROTOCOL §2).
 * Throws ProtocolError with `kind: invalid-message` on failure.
 * @returns {object} the same object, for chaining
 */
export function assertEnvelope(message) {
  if (!isPlainObject(message)) {
    throw new ProtocolError('message must be a JSON object', { kind: ProtocolErrorKind.INVALID_MESSAGE });
  }
  if (typeof message.type !== 'string' || message.type.length === 0) {
    throw new ProtocolError('message.type must be a non-empty string', { kind: ProtocolErrorKind.INVALID_MESSAGE });
  }
  if (!Number.isInteger(message.seq) || message.seq < 1) {
    throw new ProtocolError('message.seq must be a positive integer', { kind: ProtocolErrorKind.INVALID_MESSAGE });
  }
  if (message.replyTo !== undefined && message.replyTo !== null && (!Number.isInteger(message.replyTo) || message.replyTo < 1)) {
    throw new ProtocolError('message.replyTo must be a positive integer or null', { kind: ProtocolErrorKind.INVALID_MESSAGE });
  }
  return message;
}

/** Parse a raw text frame into a protocol message object. */
export function parseMessage(raw) {
  let value = raw;
  if (typeof raw === 'string') {
    try {
      value = JSON.parse(raw);
    } catch (error) {
      throw new ProtocolError('invalid JSON frame', { kind: ProtocolErrorKind.BAD_JSON, cause: error });
    }
  }
  return assertEnvelope(value);
}

/** Serialize a message to a JSON text frame, enforcing the 64 KiB cap. */
export function encodeMessage(message) {
  assertEnvelope(message);
  const text = JSON.stringify(message);
  const size = utf8Bytes(text).length;
  if (size > PROTOCOL.maxFrameBytes) {
    throw new ProtocolError(`JSON frame ${size}B exceeds ${PROTOCOL.maxFrameBytes}B`, { kind: ProtocolErrorKind.FRAME_TOO_LARGE });
  }
  return text;
}

/* ------------------------------------------------------------------------- */
/* Message builders (C→S)                                                     */
/* ------------------------------------------------------------------------- */

function requireCounter(counter) {
  if (!(counter instanceof SeqCounter)) throw new ProtocolError('a SeqCounter is required', { kind: ProtocolErrorKind.VALIDATION });
  return counter;
}

/**
 * Allocate the next `seq`, or `undefined` when the caller does not own seq.
 *
 * In the Android WebView the Native bridge is the single seq authority
 * (ARCHITECTURE §3.2.1): the UI sends business messages without `seq` and the
 * bridge injects it. In mock/ws debug transports the transport owns a counter
 * and passes it in. Builders therefore accept `counter = null` and simply omit
 * the field (JSON.stringify drops `undefined`).
 */
function takeSeq(counter) {
  if (counter === null || counter === undefined) return undefined;
  return counter.next();
}

/** Build `hello` (PROTOCOL §4.1). */
export function buildHello(counter, { deviceId, appVersion, os = 'android', ts = unixNow() }) {
  if (!deviceId) throw new ProtocolError('deviceId is required', { kind: ProtocolErrorKind.VALIDATION });
  return { type: MsgType.HELLO, seq: takeSeq(counter), v: PROTOCOL.version, deviceId, app: appVersion, os, ts };
}

/** Build `pair.request` (PROTOCOL §5.2). */
export function buildPairRequest(counter, { deviceId, code, deviceName, pubKey = null }) {
  if (!deviceId) throw new ProtocolError('deviceId is required', { kind: ProtocolErrorKind.VALIDATION });
  if (!/^\d{6}$/.test(String(code ?? ''))) {
    throw new ProtocolError('pair code must be 6 digits', { kind: ProtocolErrorKind.VALIDATION });
  }
  return {
    type: MsgType.PAIR_REQUEST,
    seq: takeSeq(counter),
    deviceId,
    code: String(code),
    deviceName: deviceName ?? 'Android',
    pubKey,
  };
}

/* --- pairing URI (PROTOCOL §5.1) ----------------------------------------- */

/** Scheme prefix of the desktop→phone pairing URI. */
export const PAIR_URI_SCHEME = 'dsphone://pair';

/** Fields of a pairing URI / manual pairing form. */
export const PAIRING_FIELDS = Object.freeze(['host', 'port', 'fp', 'code']);

/** Validate one pairing field and return it normalized; throws on bad input. */
export function validatePairingField(field, value) {
  switch (field) {
    case 'host': {
      const host = String(value ?? '').trim();
      if (!host || /\s/.test(host)) throw new ProtocolError('host is required', { kind: ProtocolErrorKind.VALIDATION });
      return host;
    }
    case 'port': {
      const port = Number(value);
      if (!Number.isInteger(port) || port < 1 || port > 65535) {
        throw new ProtocolError('port must be 1..65535', { kind: ProtocolErrorKind.VALIDATION });
      }
      return port;
    }
    case 'fp': {
      const fp = String(value ?? '').trim().toLowerCase();
      if (!/^[0-9a-f]{64}$/.test(fp)) throw new ProtocolError('fp must be 64 hex characters', { kind: ProtocolErrorKind.VALIDATION });
      return fp;
    }
    case 'code': {
      const code = String(value ?? '').trim();
      if (!/^\d{6}$/.test(code)) throw new ProtocolError('code must be 6 digits', { kind: ProtocolErrorKind.VALIDATION });
      return code;
    }
    default:
      throw new ProtocolError(`unknown pairing field ${field}`, { kind: ProtocolErrorKind.VALIDATION });
  }
}

/** Build a `dsphone://pair?...` URI from pairing fields. */
export function buildPairUri({ host, port, fp, code, sid = null }) {
  const params = new URLSearchParams();
  params.set('v', '1');
  params.set('host', validatePairingField('host', host));
  params.set('port', String(validatePairingField('port', port)));
  params.set('fp', validatePairingField('fp', fp));
  params.set('code', validatePairingField('code', code));
  if (sid) params.set('sid', String(sid));
  return `${PAIR_URI_SCHEME}?${params.toString()}`;
}

/**
 * Parse a pairing URI (PROTOCOL §5.1) into validated pairing fields.
 * Also accepts being handed a plain `host:port` string? No — URI only.
 */
export function parsePairUri(uri) {
  const text = String(uri ?? '').trim();
  const queryIndex = text.indexOf('?');
  if (!text.toLowerCase().startsWith(`${PAIR_URI_SCHEME}?`) || queryIndex < 0) {
    throw new ProtocolError('pairing URI must look like dsphone://pair?v=1&...', { kind: ProtocolErrorKind.VALIDATION });
  }
  const params = new URLSearchParams(text.slice(queryIndex + 1));
  const v = Number(params.get('v'));
  if (v !== 1) throw new ProtocolError('unsupported pairing URI version', { kind: ProtocolErrorKind.VALIDATION });
  return {
    v: 1,
    host: validatePairingField('host', params.get('host')),
    port: validatePairingField('port', params.get('port')),
    fp: validatePairingField('fp', params.get('fp')),
    code: validatePairingField('code', params.get('code')),
    sid: params.get('sid') || null,
  };
}

/** Build `auth` (PROTOCOL §6.1). Async because HMAC-SHA256 uses WebCrypto. */
export async function buildAuth(counter, { deviceId, tokenBase64, ts = unixNow(), nonce = randomNonceBase64(), cryptoImpl = globalThis.crypto }) {
  const seq = takeSeq(counter);
  const signed = await signAuth({ deviceId, tokenBase64, ts, nonce, cryptoImpl });
  return {
    type: MsgType.AUTH,
    seq,
    deviceId,
    ts: signed.ts,
    nonce: signed.nonce,
    hmac: signed.hmac,
    v: PROTOCOL.version,
  };
}

/** Build `resync` (PROTOCOL §7); required after every reconnect. */
export function buildResync(counter, { lastSeq = 0 } = {}) {
  if (!Number.isInteger(lastSeq) || lastSeq < 0) throw new ProtocolError('lastSeq must be >= 0', { kind: ProtocolErrorKind.VALIDATION });
  return { type: MsgType.RESYNC, seq: takeSeq(counter), lastSeq };
}

/** Build `ping` (PROTOCOL §7). */
export function buildPing(counter, { ts = unixNow() } = {}) {
  return { type: MsgType.PING, seq: takeSeq(counter), ts };
}

/** Build `session.list`. */
export function buildSessionList(counter) {
  return { type: MsgType.SESSION_LIST, seq: takeSeq(counter) };
}

/** Build `session.create` (title optional). */
export function buildSessionCreate(counter, { title } = {}) {
  const msg = { type: MsgType.SESSION_CREATE, seq: takeSeq(counter) };
  if (title !== undefined) msg.title = String(title);
  return msg;
}

/** Build `session.resume`. */
export function buildSessionResume(counter, { sessionId }) {
  if (!sessionId) throw new ProtocolError('sessionId is required', { kind: ProtocolErrorKind.VALIDATION });
  return { type: MsgType.SESSION_RESUME, seq: takeSeq(counter), sessionId };
}

/** Build `session.history`. */
export function buildSessionHistory(counter, { sessionId, limit = 50, before = null }) {
  if (!sessionId) throw new ProtocolError('sessionId is required', { kind: ProtocolErrorKind.VALIDATION });
  if (!Number.isInteger(limit) || limit < 1 || limit > 200) {
    throw new ProtocolError('limit must be an integer between 1 and 200', { kind: ProtocolErrorKind.VALIDATION });
  }
  return { type: MsgType.SESSION_HISTORY, seq: takeSeq(counter), sessionId, limit, before };
}

/** Validate a user chat text (non-empty after trim, ≤32 000 chars). */
export function validateChatText(text) {
  const value = String(text ?? '');
  if (value.trim().length === 0) throw new ProtocolError('message text must not be empty', { kind: ProtocolErrorKind.VALIDATION });
  if (value.length > PROTOCOL.maxTextChars) {
    throw new ProtocolError(`message text exceeds ${PROTOCOL.maxTextChars} characters`, { kind: ProtocolErrorKind.VALIDATION });
  }
  return value;
}

/** Build `chat.send` (PROTOCOL §9). */
export function buildChatSend(counter, { sessionId, text, clientId = randomUuid() }) {
  if (!sessionId) throw new ProtocolError('sessionId is required', { kind: ProtocolErrorKind.VALIDATION });
  return {
    type: MsgType.CHAT_SEND,
    seq: takeSeq(counter),
    clientId,
    sessionId,
    text: validateChatText(text),
  };
}

/** Build `chat.cancel` (PROTOCOL §9). */
export function buildChatCancel(counter, { sessionId }) {
  if (!sessionId) throw new ProtocolError('sessionId is required', { kind: ProtocolErrorKind.VALIDATION });
  return { type: MsgType.CHAT_CANCEL, seq: takeSeq(counter), sessionId };
}

/** Build `audio.start` (PROTOCOL §10.1). */
export function buildAudioStart(counter, { sessionId, sampleRate = AUDIO.sampleRate, channels = AUDIO.channels, bitDepth = AUDIO.bitDepth, lang = 'zh', mode = 'ptt' }) {
  if (mode !== 'ptt' && mode !== 'continuous') throw new ProtocolError('mode must be ptt|continuous', { kind: ProtocolErrorKind.VALIDATION });
  return {
    type: MsgType.AUDIO_START,
    seq: takeSeq(counter),
    sampleRate,
    channels,
    bitDepth,
    lang,
    mode,
    ...(sessionId ? { sessionId } : {}),
  };
}

/** Build `audio.stop`; `aseq` is the last sent audio frame sequence (PROTOCOL §10.1). */
export function buildAudioStop(counter, { aseq = 0 } = {}) {
  return { type: MsgType.AUDIO_STOP, seq: takeSeq(counter), aseq };
}

/** Valid approval outcomes (PROTOCOL §11.1). */
export const APPROVAL_OUTCOMES = Object.freeze(['approved', 'denied']);

/** Build `approval.response` (PROTOCOL §11.1). */
export function buildApprovalResponse(counter, { id, outcome }) {
  if (!id) throw new ProtocolError('approval id is required', { kind: ProtocolErrorKind.VALIDATION });
  if (!APPROVAL_OUTCOMES.includes(outcome)) throw new ProtocolError('outcome must be approved|denied', { kind: ProtocolErrorKind.VALIDATION });
  return { type: MsgType.APPROVAL_RESPONSE, seq: takeSeq(counter), id, outcome };
}

/** Normalize one answer object for `question.response`. */
export function normalizeAnswer(raw, fallbackQuestionId) {
  const questionId = raw?.questionId ?? fallbackQuestionId;
  if (!questionId) throw new ProtocolError('question answer requires questionId', { kind: ProtocolErrorKind.VALIDATION });
  const selected = Array.isArray(raw?.selected) ? raw.selected.map(String) : [];
  const custom = raw?.custom === undefined || raw?.custom === null || String(raw.custom).trim() === '' ? null : String(raw.custom);
  return { questionId, selected, custom };
}

/** Build `question.response` (PROTOCOL §11.2). */
export function buildQuestionResponse(counter, { id, cancelled = false, answers = [] }) {
  if (!id) throw new ProtocolError('question id is required', { kind: ProtocolErrorKind.VALIDATION });
  return {
    type: MsgType.QUESTION_RESPONSE,
    seq: takeSeq(counter),
    id,
    cancelled: Boolean(cancelled),
    answers: cancelled ? [] : answers.map((answer) => normalizeAnswer(answer)),
  };
}

/* ------------------------------------------------------------------------- */
/* Message builders (S→C) —used by mock.js and tests                         */
/* ------------------------------------------------------------------------- */

/**
 * Build a server-side reply envelope. `populate` receives the seq allocated so
 * replies can echo `replyTo`.
 */
export function buildReply(counter, type, replyTo, fields = {}) {
  requireCounter(counter);
  const seq = counter.next();
  return { type, seq, replyTo, ...fields };
}

/** Build a server-side message without `replyTo` (push event). */
export function buildPush(counter, type, fields = {}) {
  requireCounter(counter);
  return { type, seq: counter.next(), ...fields };
}

/* ------------------------------------------------------------------------- */
/* Normalizers for S→C payloads                                               */
/* ------------------------------------------------------------------------- */

function requireString(value, field) {
  if (typeof value !== 'string' || value.length === 0) {
    throw new ProtocolError(`${field} must be a non-empty string`, { kind: ProtocolErrorKind.INVALID_MESSAGE });
  }
  return value;
}

function coerceTimestamp(value, fallback = 0) {
  const n = Number(value);
  return Number.isFinite(n) && n >= 0 ? n : fallback;
}

/** Normalize a `Session` object (PROTOCOL §8.1). */
export function normalizeSession(raw) {
  if (!isPlainObject(raw)) throw new ProtocolError('session must be an object', { kind: ProtocolErrorKind.INVALID_MESSAGE });
  return {
    sessionId: requireString(raw.sessionId, 'session.sessionId'),
    title: typeof raw.title === 'string' && raw.title.length > 0 ? raw.title : '',
    updatedAt: coerceTimestamp(raw.updatedAt),
    lastPreview: typeof raw.lastPreview === 'string' ? raw.lastPreview : '',
  };
}

/** Normalize a `Message` object (PROTOCOL §8.1). */
export function normalizeMessage(raw) {
  if (!isPlainObject(raw)) throw new ProtocolError('message must be an object', { kind: ProtocolErrorKind.INVALID_MESSAGE });
  const role = raw.role;
  if (role !== 'user' && role !== 'assistant' && role !== 'system' && role !== 'tool') {
    throw new ProtocolError('message.role must be user|assistant|system|tool', { kind: ProtocolErrorKind.INVALID_MESSAGE });
  }
  const status = raw.status === 'streaming' || raw.status === 'error' ? raw.status : 'done';
  return {
    messageId: requireString(raw.messageId, 'message.messageId'),
    sessionId: requireString(raw.sessionId, 'message.sessionId'),
    role,
    text: typeof raw.text === 'string' ? raw.text : '',
    status,
    createdAt: coerceTimestamp(raw.createdAt),
    ...(raw.clientId ? { clientId: String(raw.clientId) } : {}),
  };
}

/** Normalize `session.list.ok` / `auth.ok.sessions` arrays. */
export function normalizeSessionList(rawSessions) {
  if (!Array.isArray(rawSessions)) return [];
  return rawSessions.map(normalizeSession).sort((a, b) => b.updatedAt - a.updatedAt);
}

/** Normalize `session.resume.ok` / `session.history.ok` message arrays. */
export function normalizeMessageList(rawMessages) {
  if (!Array.isArray(rawMessages)) return [];
  return rawMessages.map(normalizeMessage).sort((a, b) => a.createdAt - b.createdAt);
}

/** Normalize `approval.request` (PROTOCOL §11.1). */
export function normalizeApprovalRequest(raw) {
  if (!isPlainObject(raw)) throw new ProtocolError('approval.request must be an object', { kind: ProtocolErrorKind.INVALID_MESSAGE });
  return {
    id: requireString(raw.id, 'approval.id'),
    sessionId: typeof raw.sessionId === 'string' ? raw.sessionId : null,
    tool: typeof raw.tool === 'string' ? raw.tool : 'tool',
    reason: typeof raw.reason === 'string' ? raw.reason : '',
    argsPreview: typeof raw.argsPreview === 'string' ? raw.argsPreview : '',
    deadline: coerceTimestamp(raw.deadline),
    defaultOutcome: raw.defaultOutcome === 'approved' ? 'approved' : 'denied',
    receivedAt: coerceTimestamp(raw.receivedAt, 0),
  };
}

/** Normalize one question object of `question.request`. */
export function normalizeQuestion(raw) {
  if (!isPlainObject(raw)) throw new ProtocolError('question must be an object', { kind: ProtocolErrorKind.INVALID_MESSAGE });
  const options = Array.isArray(raw.options)
    ? raw.options
        .filter((option) => isPlainObject(option) && option.id !== undefined)
        .map((option) => ({ id: String(option.id), label: typeof option.label === 'string' ? option.label : String(option.id) }))
    : [];
  return {
    id: requireString(raw.id, 'question.id'),
    question: typeof raw.question === 'string' ? raw.question : '',
    multiSelect: Boolean(raw.multiSelect),
    required: raw.required !== false,
    allowCustom: Boolean(raw.allowCustom),
    options,
  };
}

/** Normalize `question.request` (PROTOCOL §11.2). */
export function normalizeQuestionRequest(raw) {
  if (!isPlainObject(raw)) throw new ProtocolError('question.request must be an object', { kind: ProtocolErrorKind.INVALID_MESSAGE });
  const questions = Array.isArray(raw.questions) ? raw.questions.map(normalizeQuestion) : [];
  if (questions.length === 0) throw new ProtocolError('question.request requires at least one question', { kind: ProtocolErrorKind.INVALID_MESSAGE });
  return {
    id: requireString(raw.id, 'question.id'),
    sessionId: typeof raw.sessionId === 'string' ? raw.sessionId : null,
    questions,
    deadline: coerceTimestamp(raw.deadline),
  };
}

/* ------------------------------------------------------------------------- */
/* Audio binary frame helpers (PROTOCOL §10.2)                                */
/* ------------------------------------------------------------------------- */

/**
 * Build the 7-byte header of a C→S PCM16 audio frame.
 * `[0x01][aseq:u16le][tsMs:u32le]` followed by 640 B of PCM (added by caller).
 */
export function buildAudioFrameHeader(aseq, tsMs) {
  if (!Number.isInteger(aseq) || aseq < 1 || aseq > AUDIO.aseqMax) {
    throw new ProtocolError('aseq must be 1..65535', { kind: ProtocolErrorKind.VALIDATION });
  }
  if (!Number.isInteger(tsMs) || tsMs < 0 || tsMs > 0xffffffff) {
    throw new ProtocolError('tsMs must be a u32', { kind: ProtocolErrorKind.VALIDATION });
  }
  const header = new Uint8Array(AUDIO.headerBytes);
  header[0] = AUDIO.frameTypePcm16;
  header[1] = aseq & 0xff;
  header[2] = (aseq >> 8) & 0xff;
  header[3] = tsMs & 0xff;
  header[4] = (tsMs >>> 8) & 0xff;
  header[5] = (tsMs >>> 16) & 0xff;
  header[6] = (tsMs >>> 24) & 0xff;
  return header;
}

/** Parse an audio frame header; returns `null` when the buffer is too short. */
export function parseAudioFrameHeader(bytes) {
  const view = toUint8Array(bytes);
  if (view.length < AUDIO.headerBytes) return null;
  return {
    frameType: view[0],
    aseq: view[1] | (view[2] << 8),
    tsMs: (view[3] | (view[4] << 8) | (view[5] << 16) | (view[6] << 24)) >>> 0,
    payload: view.subarray(AUDIO.headerBytes),
  };
}

/** Wrap PCM bytes into a complete audio frame (header + payload). */
export function buildAudioFrame(aseq, tsMs, pcm16le) {
  const payload = toUint8Array(pcm16le);
  const header = buildAudioFrameHeader(aseq, tsMs);
  const frame = new Uint8Array(header.length + payload.length);
  frame.set(header, 0);
  frame.set(payload, header.length);
  return frame;
}

/* ------------------------------------------------------------------------- */
/* Misc helpers                                                               */
/* ------------------------------------------------------------------------- */

/** Current Unix seconds, injected clock friendly. */
export function isExpired(deadlineSeconds, nowSeconds = unixNow()) {
  return Number(deadlineSeconds) > 0 && Number(deadlineSeconds) <= nowSeconds;
}

/** Depth-limited short preview of arbitrary text for diagnostics (never full body). */
export function textFingerprint(text) {
  const value = String(text ?? '');
  return { length: value.length, hashPrefix: simpleHash(value).toString(16).padStart(8, '0') };
}

/** FNV-1a 32-bit hash, only used for non-cryptographic diagnostics fingerprints. */
export function simpleHash(text) {
  let hash = 0x811c9dc5;
  const value = String(text ?? '');
  for (let i = 0; i < value.length; i += 1) {
    hash ^= value.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash >>> 0;
}

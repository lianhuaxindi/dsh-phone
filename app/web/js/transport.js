/**
 * Transports: everything between the UI controller and the network/bridge.
 *
 * Three implementations share one interface so the controller (and the UI) is
 * identical in the demo, in a browser and on the phone:
 *
 *   NativeTransport  — Android WebView, talks to `window.DshNative` (JsBridge,
 *                      ARCHITECTURE §3.2 / §3.2.1). Native owns TLS, the
 *                      device token, the protocol `seq` counter and audio.
 *   WsTransport      — browser-only debug mode: WebView-less WebSocket client
 *                      that performs the protocol handshake itself. Not used
 *                      on devices; kept so the Web UI can be exercised against
 *                      a real Bridge from desktop Chrome.
 *   MockTransport    — `mock.js`, a fully scripted backend for the demo and for
 *                      `node --test`.
 *
 * Contract the controller relies on (all transports must honour it):
 *   connect({host, port, fp, deviceId})   open the connection / start handshake
 *   disconnect()                          close intentionally (no auto-reconnect)
 *   reconnect()                           drop and re-establish (diagnostics)
 *   requestPair({code, deviceName})       submit a pairing code
 *   requestAuth()                         authenticate with the stored token
 *   send(message)                         business protocol message (C→S)
 *   startAudio({sampleRate, lang, mode}) / stopAudio() / cancelAudio()
 *   getToken() / setToken(token)          native only; mock fakes it
 *   routeInfo() / preferDevice(address)   audio routing
 *   notify({title, text})                 P1 notification passthrough
 *   exportLogs()                          ask the bridge to export logs
 *   scanPairing()                         open the QR scanner (v1.1) / manual hint
 *   pickImage({source})                   camera/gallery picker, resolves image
 *   mediaGet(mediaId)                     protocol v1.1 `media.get` request
 *   on(event, fn)                         subscription, returns unsubscribe
 *   setBackHandler(fn)                    Android back key; returns consumed
 *
 * Events:
 *   state      {state, detail}            TransportState values
 *   message    S→C protocol message (business; handshake frames are consumed)
 *   pair-result{ok, code?, message?, attemptsLeft?, retryable?}
 *   auth-result{ok, code?, message?, needPair?, sessions?}
 *   error      {scope, code, message, retryable?}
 *   event      {type: 'audio.state'|'audio.ready'|'audio.route'|'audio.level'
 *                     |'audio.stopped'|'logs.exported'|'token'|'settings.audioRoute', ...}
 *   binary     Uint8Array                 (debug WS only; native audio never reaches JS)
 *
 * seq / request correlation (ARCHITECTURE §3.2.1):
 *   The native bridge is the only seq authority. UI messages therefore carry a
 *   local `_reqId` and no `seq`; the bridge strips `_reqId` when forwarding and
 *   back-fills `_reqTo` on the matching response. MockTransport simulates the
 *   same behaviour, and WsTransport emulates it using the wire `replyTo`.
 */

import { PROTOCOL, MsgType, ErrorCode, CloseCode, SeqCounter, buildHello, buildPairRequest, buildAuth, buildPing, buildResync, buildSessionList, buildMediaGet, normalizeMediaData, encodeMessage } from './protocol.js';
import { TransportMode } from './config.js';
import { realScheduler } from './scheduler.js';

/** Which transport implementation is active. */
export const TransportKind = Object.freeze({
  NATIVE: 'native',
  WS: 'ws',
  MOCK: 'mock',
});

/** Connection phases shared by all transports (mirrors native `net.state`). */
export const TransportState = Object.freeze({
  IDLE: 'idle',
  CONNECTING: 'connecting',
  PAIRING: 'pairing',
  AUTHENTICATING: 'authenticating',
  RECONNECTING: 'reconnecting',
  ONLINE: 'online',
  FAILED: 'failed',
  CLOSED: 'closed',
});

/** Event names emitted by transports. */
export const TransportEvent = Object.freeze({
  STATE: 'state',
  MESSAGE: 'message',
  PAIR_RESULT: 'pair-result',
  AUTH_RESULT: 'auth-result',
  ERROR: 'error',
  EVENT: 'event',
  BINARY: 'binary',
});

/**
 * Normalize an inbound S→C message (object or JSON string) into a plain object.
 * Bridge passthrough frames may omit `seq` for synthetic events, so `seq`
 * defaults to 0 here instead of failing envelope validation.
 */
export function normalizeInboundMessage(raw) {
  let value = raw;
  if (typeof raw === 'string') {
    try {
      value = JSON.parse(raw);
    } catch {
      throw new Error('invalid JSON in inbound message');
    }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('inbound message must be an object');
  if (typeof value.type !== 'string' || value.type.length === 0) throw new Error('inbound message.type missing');
  const seq = Number.isInteger(value.seq) ? value.seq : 0;
  return { ...value, seq };
}

/** Extract the event name from a bridge event object (tolerant of shapes). */
export function bridgeEventType(data) {
  if (!data || typeof data !== 'object') return '';
  return String(data.type ?? data.event ?? data.eventType ?? data.name ?? '');
}

/** True when running inside a WebView that exposes the Kotlin bridge. */
export function hasNativeBridge(scope = globalThis) {
  return Boolean(scope && scope.DshNative && typeof scope.DshNative.post === 'function');
}

/** Base class: subscription plumbing + the shared transport interface. */
export class BaseTransport {
  /**
   * @param {{scheduler?: import('./scheduler.js').Scheduler, kind?: string}} [options]
   */
  constructor({ scheduler = realScheduler, kind = 'base' } = {}) {
    this.scheduler = scheduler;
    this.kind = kind;
    /** @type {string} */
    this.state = TransportState.IDLE;
    /** @type {Map<string, Set<Function>>} */
    this._listeners = new Map();
    /** @type {null | (() => boolean)} */
    this._backHandler = null;
    /** Last config passed to connect() (reconnect uses it). */
    this._config = null;
  }

  /* ------------------------------------------------------------ event bus */

  /** Subscribe; returns an unsubscribe function. */
  on(event, handler) {
    if (typeof handler !== 'function') throw new TypeError('handler must be a function');
    if (!this._listeners.has(event)) this._listeners.set(event, new Set());
    this._listeners.get(event).add(handler);
    return () => this.off(event, handler);
  }

  /** Unsubscribe. */
  off(event, handler) {
    this._listeners.get(event)?.delete(handler);
  }

  /** Emit to every subscriber; listener errors never break the transport. */
  emit(event, payload) {
    const handlers = this._listeners.get(event);
    if (!handlers || handlers.size === 0) return;
    for (const handler of [...handlers]) {
      try {
        handler(payload);
      } catch (error) {
        this.emit(TransportEvent.ERROR, {
          scope: 'ui',
          code: 5001,
          message: `listener for ${event} failed: ${error && error.message ? error.message : String(error)}`,
          retryable: false,
        });
      }
    }
  }

  /** Update state then notify subscribers. */
  emitState(state, detail = undefined) {
    this.state = state;
    this.emit(TransportEvent.STATE, { state, detail });
  }

  /** Convenience for bridge/protocol failures. */
  emitError({ scope = 'transport', code = 5001, message = '', retryable = false } = {}) {
    this.emit(TransportEvent.ERROR, { scope, code, message, retryable });
  }

  /** Set the Android back-button handler; returns whether it handled the key. */
  setBackHandler(fn) {
    this._backHandler = typeof fn === 'function' ? fn : null;
  }

  /* ------------------------------------------------------------- commands */

  connect(config) {
    this._config = config ?? this._config;
    this._connect(this._config);
  }

  disconnect() {
    this._disconnect();
  }

  reconnect(config) {
    if (config) this._config = config;
    this._reconnect();
  }

  requestPair(options) {
    this._requestPair(options ?? {});
  }

  requestAuth() {
    this._requestAuth();
  }

  send(message) {
    if (!message || typeof message !== 'object') throw new TypeError('send() needs a protocol message object');
    this._send(message);
  }

  startAudio(options) {
    this._startAudio(options ?? {});
  }

  stopAudio() {
    this._stopAudio();
  }

  cancelAudio() {
    this._cancelAudio();
  }

  getToken() {
    this._getToken();
  }

  /**
   * Native only: persist/clear the device token.
   * @param {string|null} token empty/null clears it
   * @param {{host?: string, port?: number, serverId?: string}} [meta] stored alongside
   */
  setToken(token, meta) {
    this._setToken(token, meta);
  }

  routeInfo() {
    this._routeInfo();
  }

  preferDevice(address) {
    this._preferDevice(address);
  }

  notify(payload) {
    this._notify(payload ?? {});
  }

  exportLogs() {
    this._exportLogs();
  }

  /** @returns {Promise<{supported: boolean, [key: string]: unknown}>} */
  async scanPairing() {
    return this._scanPairing();
  }

  /**
   * Ask the native side for an image (PROTOCOL v1.1 / ARCHITECTURE §3.2:
   * `pickImage{source}` → `image.picked` | `image.pickFailed`).
   * @param {'gallery'|'camera'} source
   * @returns {Promise<{ok: boolean, image?: {data: string, mimeType: string, name?: string, width?: number, height?: number}, reason?: string}>}
   */
  async pickImage(source) {
    return this._pickImage(source);
  }

  /**
   * Fetch one chat image from the bridge (`media.get` → `media.data`).
   * @param {string} mediaId
   * @returns {Promise<{ok: boolean, media?: {mediaId: string, mimeType: string, data: string}, code?: number, reason?: string}>}
   */
  async mediaGet(mediaId) {
    return this._mediaGet(mediaId);
  }

  /* ------------------------------------------- defaults for subclasses */

  _connect() {
    this.emitError({ scope: 'transport', code: 5002, message: 'connect() not implemented' });
  }

  _disconnect() {}

  _reconnect() {
    this.disconnect();
    if (this._config) this.connect(this._config);
  }

  _requestPair() {}

  _requestAuth() {}

  _send() {}

  _startAudio() {}

  _stopAudio() {}

  _cancelAudio() {}

  _getToken() {
    this.emit(TransportEvent.EVENT, { type: 'token', hasToken: false });
  }

  _setToken(_token, _meta) {}

  _routeInfo() {
    this.emit(TransportEvent.EVENT, { type: 'settings.audioRoute', address: undefined });
  }

  _preferDevice(address) {
    this.emit(TransportEvent.EVENT, { type: 'settings.audioRoute', address });
  }

  _notify() {}

  _exportLogs() {
    this.emitError({ scope: 'logs', code: 5002, message: 'log export is only available on Android', retryable: false });
  }

  async _scanPairing() {
    return { supported: false };
  }

  async _pickImage(source) {
    return { ok: false, reason: 'unsupported', source };
  }

  async _mediaGet(mediaId) {
    return { ok: false, reason: 'unsupported', mediaId };
  }
}

/* ========================================================================= */
/* NativeTransport — Android WebView JsBridge                                */
/* ========================================================================= */

/**
 * Transport for the real app. The Kotlin bridge owns the socket, TLS, token,
 * seq counter and audio; this class only translates UI intents into bridge ops
 * and bridge events back into transport events.
 *
 * Assumptions (recorded in app/web/README.md, awaiting bridge confirmation):
 *  - `connect` performs TCP/TLS/WS + `hello`; pairing/auth follow explicit ops.
 *  - After `pair.result{ok}`, the UI sends `{op:"auth"}` to finish pairing.
 *  - `{op:"reconnect"}` exists for diagnostics (D-013); falls back to connect.
 *  - `{op:"disconnect"}` is best-effort (not in the frozen op list).
 */
export class NativeTransport extends BaseTransport {
  /**
   * @param {object} [options]
   * @param {object} [options.scope] window-like object (tests inject a fake)
   * @param {import('./scheduler.js').Scheduler} [options.scheduler]
   */
  constructor({ scope = globalThis, scheduler = realScheduler } = {}) {
    super({ scheduler, kind: TransportKind.NATIVE });
    this.scope = scope;
    this._scanResolvers = [];
    this._scanTimer = null;
    /** How long the scanner UI may stay open before falling back to manual (ms). */
    this.scanTimeoutMs = 10 * 60 * 1000;
    /** Pending `pickImage` promises while the native picker UI is open. */
    this._pickResolvers = [];
    /** In-flight `media.get` requests keyed by local `_reqId`. */
    this._mediaRequests = new Map();
    this._mediaSeq = 0;
    this.mediaTimeoutMs = 20000;
    this._installBridge(scope);
  }

  /** Expose `window.DshBridge` and keep any pre-existing handlers working. */
  _installBridge(scope) {
    if (!scope) return;
    const existing = scope.DshBridge && typeof scope.DshBridge === 'object' ? scope.DshBridge : {};
    const previousOnEvent = typeof existing.onEvent === 'function' ? existing.onEvent : null;
    const previousOnBack = typeof existing.onBack === 'function' ? existing.onBack : null;
    scope.DshBridge = {
      onEvent: (raw) => {
        if (previousOnEvent) {
          try {
            previousOnEvent(raw);
          } catch {
            /* ignore third-party handler failures */
          }
        }
        this.handleEvent(raw);
      },
      onBack: () => {
        let consumed = false;
        if (this._backHandler) {
          try {
            consumed = Boolean(this._backHandler());
          } catch {
            consumed = false;
          }
        }
        if (consumed) return true;
        if (previousOnBack) {
          try {
            return Boolean(previousOnBack());
          } catch {
            return false;
          }
        }
        return false;
      },
    };
  }

  /** Post one op as a JSON string (android-shell: string is most reliable). */
  post(op, fields = {}) {
    const bridge = this.scope?.DshNative;
    if (!bridge || typeof bridge.post !== 'function') {
      this.emitError({ scope: 'bridge', code: 5001, message: 'native bridge unavailable', retryable: true });
      return false;
    }
    try {
      bridge.post(JSON.stringify({ op, ...fields }));
      return true;
    } catch (error) {
      this.emitError({ scope: 'bridge', code: 5001, message: `bridge post failed: ${error?.message ?? error}`, retryable: true });
      return false;
    }
  }

  /* ----------------------------------------------------------- commands */

  _connect(config = {}) {
    const { host, port, fp, deviceId } = config;
    this.emitState(TransportState.CONNECTING);
    this.post('connect', { host, port, fp: fp || '', deviceId });
  }

  _disconnect() {
    // Not part of the frozen op list; best effort, native may ignore it.
    this.post('disconnect');
    this.emitState(TransportState.CLOSED, { intentional: true });
  }

  _reconnect() {
    if (!this.post('reconnect')) {
      // Older bridges without `reconnect`: fall back to a plain connect.
      if (this._config) this._connect(this._config);
    } else {
      this.emitState(TransportState.RECONNECTING);
    }
  }

  _requestPair({ code, deviceName } = {}) {
    this.post('pair', { code, deviceName });
  }

  _requestAuth() {
    this.post('auth');
  }

  _send(message) {
    const { seq, _reqId, ...payload } = message;
    // `_reqId` is a local correlation id: the bridge strips it before the
    // message goes out and back-fills `_reqTo` on the matching response
    // (ARCHITECTURE §3.2.1), so the controller can correlate requests.
    const outbound = typeof _reqId === 'string' ? { ...payload, _reqId } : payload;
    this.post('send', { json: JSON.stringify(outbound) });
  }

  _startAudio({ sampleRate = 16000, lang = 'zh', mode = 'ptt', sessionId = undefined } = {}) {
    this.post('startAudio', { sampleRate, lang, mode, ...(sessionId ? { sessionId } : {}) });
  }

  _stopAudio() {
    this.post('stopAudio');
  }

  _cancelAudio() {
    this.post('cancelAudio');
  }

  _getToken() {
    this.post('getToken');
  }

  _setToken(token, meta = {}) {
    const payload = token ? { token } : {};
    for (const key of ['host', 'port', 'serverId']) {
      if (meta?.[key] !== undefined) payload[key] = meta[key];
    }
    this.post('setToken', payload);
  }

  _routeInfo() {
    this.post('routeInfo');
  }

  _preferDevice(address) {
    this.post('preferDevice', address ? { address } : {});
  }

  _notify({ title, text } = {}) {
    this.post('notify', { title, text });
  }

  _exportLogs() {
    this.post('exportLogs');
  }

  async _scanPairing() {
    return new Promise((resolve) => {
      this._scanResolvers.push(resolve);
      const posted = this.post('scanPairing');
      if (!posted) {
        this._settleScan({ ok: false, supported: false, reason: 'unsupported' });
        return;
      }
      this._scanTimer = this.scheduler.setTimeout(() => this._settleScan({ ok: false, supported: false, reason: 'timeout' }), this.scanTimeoutMs);
    });
  }

  _settleScan(result) {
    if (this._scanTimer !== null) {
      this.scheduler.clearTimeout(this._scanTimer);
      this._scanTimer = null;
    }
    const resolvers = this._scanResolvers;
    this._scanResolvers = [];
    for (const resolve of resolvers) resolve(result);
  }

  /* --------------------------------------------------- images (v1.1) */

  async _pickImage(source) {
    if (source !== 'gallery' && source !== 'camera') {
      return { ok: false, reason: 'bad_source' };
    }
    if (this._pickResolvers.length > 0) return { ok: false, reason: 'busy' };
    return new Promise((resolve) => {
      this._pickResolvers.push(resolve);
      const posted = this.post('pickImage', { source });
      if (!posted) this._settlePick({ ok: false, reason: 'unsupported' });
    });
  }

  _settlePick(result) {
    const resolvers = this._pickResolvers;
    this._pickResolvers = [];
    for (const resolve of resolvers) resolve(result);
  }

  async _mediaGet(mediaId) {
    if (!mediaId) return { ok: false, code: 3001, reason: 'unknown-media' };
    const _reqId = `media-${++this._mediaSeq}`;
    return new Promise((resolve) => {
      const timer = this.scheduler.setTimeout(() => {
        this._mediaRequests.delete(_reqId);
        resolve({ ok: false, code: 5001, reason: 'timeout' });
      }, this.mediaTimeoutMs);
      this._mediaRequests.set(_reqId, {
        resolve: (message) => {
          this.scheduler.clearTimeout(timer);
          if (message?.type === MsgType.MEDIA_DATA) {
            try {
              resolve({ ok: true, media: normalizeMediaData(message) });
            } catch (error) {
              resolve({ ok: false, code: message.code ?? 3001, reason: String(error?.message ?? error) });
            }
          } else {
            resolve({ ok: false, code: Number(message?.code ?? 3001), reason: message?.message ?? 'media unavailable' });
          }
        },
      });
      try {
        this.send({ ...buildMediaGet(null, { mediaId }), _reqId });
      } catch (error) {
        this.scheduler.clearTimeout(timer);
        this._mediaRequests.delete(_reqId);
        resolve({ ok: false, code: 5001, reason: String(error?.message ?? error) });
      }
    });
  }

  /* -------------------------------------------------------------- events */

  /** Entry point for `window.DshBridge.onEvent(json)`. */
  handleEvent(raw) {
    let data = raw;
    if (typeof raw === 'string') {
      try {
        data = JSON.parse(raw);
      } catch {
        this.emitError({ scope: 'bridge', code: 5002, message: 'invalid JSON from bridge', retryable: false });
        return;
      }
    }
    if (!data || typeof data !== 'object') return;
    const type = bridgeEventType(data);
    const payload = data.data && typeof data.data === 'object' ? { ...data, ...data.data } : data;

    switch (type) {
      case 'net.state':
        this._onNetState(payload);
        break;
      case 'pair.result':
        this._onPairResult(payload);
        break;
      case 'auth.result':
        this._onAuthResult(payload);
        break;
      case 'proto.message': {
        const rawMessage = payload.message ?? payload.json ?? payload.payload;
        let message;
        try {
          message = normalizeInboundMessage(rawMessage);
        } catch (error) {
          this.emitError({ scope: 'protocol', code: 5002, message: `bad proto.message: ${error?.message ?? error}`, retryable: false });
          break;
        }
        // `media.get` replies are transport-internal: resolve the pending
        // promise instead of surfacing a protocol message the UI can't use.
        if (typeof message._reqTo === 'string' && this._mediaRequests.has(message._reqTo)) {
          const pending = this._mediaRequests.get(message._reqTo);
          this._mediaRequests.delete(message._reqTo);
          pending?.resolve(message);
          break;
        }
        this.emit(TransportEvent.MESSAGE, message);
        break;
      }
      case 'audio.state':
        this.emit(TransportEvent.EVENT, { type, state: payload.state, reason: payload.reason });
        break;
      case 'audio.ready':
        this.emit(TransportEvent.EVENT, { type, asrMode: payload.asrMode, lang: payload.lang, codecs: payload.codecs });
        break;
      case 'audio.error':
        this.emit(TransportEvent.EVENT, { type, code: payload.code, message: payload.message, retryable: payload.retryable });
        this.emitError({ scope: 'audio', code: payload.code ?? 5001, message: payload.message ?? 'audio error', retryable: payload.retryable ?? true });
        break;
      case 'audio.route':
        this.emit(TransportEvent.EVENT, {
          type,
          route: payload.route,
          deviceName: payload.deviceName,
          preferredUnavailable: payload.preferredUnavailable,
        });
        break;
      case 'audio.level':
        this.emit(TransportEvent.EVENT, { type, level: Number(payload.level) || 0 });
        break;
      case 'audio.stopped':
        this.emit(TransportEvent.EVENT, { type, aseq: payload.aseq });
        break;
      case 'error':
        this.emitError({
          scope: payload.scope ?? 'bridge',
          code: payload.code ?? 5001,
          message: payload.message ?? '',
          retryable: Boolean(payload.retryable),
        });
        break;
      case 'logs.exported':
        this.emit(TransportEvent.EVENT, { type, text: payload.text ?? '', truncated: Boolean(payload.truncated) });
        break;
      case 'token':
        this.emit(TransportEvent.EVENT, {
          type,
          hasToken: Boolean(payload.hasToken),
          host: payload.host,
          port: payload.port,
          serverId: payload.serverId,
        });
        break;
      case 'settings.audioRoute':
        this.emit(TransportEvent.EVENT, { type, address: payload.address });
        break;
      case 'image.picked':
        this._settlePick({
          ok: true,
          data: String(payload.data ?? payload.image?.data ?? ''),
          mimeType: String(payload.mimeType ?? payload.image?.mimeType ?? 'image/jpeg'),
          name: payload.name ?? payload.image?.name,
          width: payload.width ?? payload.image?.width,
          height: payload.height ?? payload.image?.height,
          source: payload.source ?? payload.image?.source,
        });
        break;
      case 'image.pickFailed':
        this._settlePick({ ok: false, reason: String(payload.reason ?? 'unknown') });
        break;
      case 'scanPairing.result': {
        // v1.1 (D-030): real Camera2 + ZXing result from the native bridge.
        // No `{supported:false}` stub anymore; the legacy `scan.result` alias
        // was dropped so check_contract has no stale Web-side assumption.
        if (payload.ok === true) {
          this._settleScan({
            ok: true,
            supported: true,
            uri: payload.uri,
            host: payload.host,
            port: payload.port,
            fp: payload.fp,
            code: payload.code,
            sid: payload.sid,
            hosts: Array.isArray(payload.hosts) ? payload.hosts.map(String) : [],
            name: payload.name,
          });
        } else {
          this._settleScan({
            ok: false,
            supported: payload.supported !== false,
            reason: String(payload.reason ?? 'unknown'),
          });
        }
        break;
      }
      case '':
        // Some bridges send `{event: ...}` with a nested payload; ignore unknown.
        break;
      default:
        this.emit(TransportEvent.EVENT, { type, ...payload });
        break;
    }
  }

  _onNetState(payload) {
    const raw = String(payload.state ?? '');
    const mapping = {
      idle: TransportState.IDLE,
      connecting: TransportState.CONNECTING,
      pairing: TransportState.PAIRING,
      authenticating: TransportState.AUTHENTICATING,
      online: TransportState.ONLINE,
      reconnecting: TransportState.RECONNECTING,
      failed: TransportState.FAILED,
      // D-034: v1.1 bridges report terminal connection failures as `error`
      // (e.g. reason `all_addresses_failed`) instead of `failed`.
      error: TransportState.FAILED,
      closed: TransportState.CLOSED,
    };
    const state = mapping[raw] ?? TransportState.CLOSED;
    const detail = {};
    if (payload.serverId !== undefined) detail.serverId = payload.serverId;
    if (payload.reason !== undefined) detail.reason = payload.reason;
    if (payload.message !== undefined) detail.message = payload.message;
    if (Array.isArray(payload.tried)) detail.tried = payload.tried;
    this.emitState(state, detail);
  }

  _onPairResult(payload) {
    this.emit(TransportEvent.PAIR_RESULT, {
      ok: Boolean(payload.ok),
      code: payload.code,
      message: payload.message,
      attemptsLeft: payload.attemptsLeft,
      retryable: payload.retryable,
      serverId: payload.serverId,
    });
    if (payload.ok) this.emitState(TransportState.AUTHENTICATING);
  }

  _onAuthResult(payload) {
    this.emit(TransportEvent.AUTH_RESULT, {
      ok: Boolean(payload.ok),
      code: payload.code,
      message: payload.message,
      needPair: Boolean(payload.needPair),
      serverId: payload.serverId,
      sessions: Array.isArray(payload.sessions) ? payload.sessions : undefined,
    });
    if (payload.ok) {
      this.emitState(TransportState.ONLINE, { serverId: payload.serverId });
    } else if (payload.needPair) {
      this.emitState(TransportState.PAIRING);
    }
  }
}

/* ========================================================================= */
/* WsTransport — browser debug mode (Web performs the protocol handshake)    */
/* ========================================================================= */

const DEBUG_TOKEN_KEY = 'dsphone.debugToken';

/**
 * Browser debug transport: speaks PROTOCOL v1 over a real WebSocket.
 *
 * Only used when the Web UI is opened in Chrome/Edge (not inside the app), so
 * the token lives in `localStorage` here — on a device the token never leaves
 * Android Keystore. This transport owns the seq counter and consumes the
 * handshake frames itself, mirroring what the native bridge does.
 */
export class WsTransport extends BaseTransport {
  /**
   * @param {object} [options]
   * @param {typeof WebSocket} [options.WebSocketImpl]
   * @param {Storage} [options.storage] localStorage-like (tests inject a fake)
   * @param {import('./scheduler.js').Scheduler} [options.scheduler]
   * @param {(host: string, port: number, secure: boolean) => string} [options.urlFactory]
   */
  constructor({ WebSocketImpl = globalThis.WebSocket, storage = safeLocalStorage(), scheduler = realScheduler, urlFactory } = {}) {
    super({ scheduler, kind: TransportKind.WS });
    this.WebSocketImpl = WebSocketImpl;
    this.storage = storage;
    this.urlFactory = urlFactory ?? defaultWsUrl;
    this.counter = new SeqCounter();
    this.socket = null;
    this._pairSeq = new Map();
    this._onOpenHandled = false;
    this._pingTimer = null;
    this._lastSeenSeq = 0;
    this._mediaRequests = new Map();
    this._mediaSeq = 0;
  }

  _connect(config = {}) {
    this._config = config;
    if (!this.WebSocketImpl) {
      this.emitError({ scope: 'transport', code: 5001, message: 'WebSocket unavailable in this environment', retryable: false });
      this.emitState(TransportState.FAILED, { reason: 'no-websocket' });
      return;
    }
    this.counter = new SeqCounter();
    this._pairSeq = new Map();
    this._onOpenHandled = false;
    const secure = config.secure !== false;
    let url;
    try {
      url = this.urlFactory(config.host, config.port, secure);
    } catch (error) {
      this.emitError({ scope: 'transport', code: 5001, message: String(error?.message ?? error), retryable: true });
      this.emitState(TransportState.FAILED);
      return;
    }
    this.emitState(TransportState.CONNECTING);
    let socket;
    try {
      socket = new this.WebSocketImpl(url, PROTOCOL.subprotocol);
    } catch (error) {
      this.emitError({ scope: 'transport', code: 5001, message: String(error?.message ?? error), retryable: true });
      this.emitState(TransportState.FAILED);
      return;
    }
    this.socket = socket;
    if ('binaryType' in socket) socket.binaryType = 'arraybuffer';
    socket.onopen = () => this._handleOpen();
    socket.onmessage = (event) => this._handleData(event?.data);
    socket.onerror = () => this.emitError({ scope: 'transport', code: 5001, message: 'WebSocket error', retryable: true });
    socket.onclose = (event) => this._handleClose(event);
  }

  _handleOpen() {
    this._onOpenHandled = true;
    const negotiated = typeof this.socket?.protocol === 'string' ? this.socket.protocol : '';
    if (negotiated !== PROTOCOL.subprotocol) {
      // RFC 6455: the server must echo the agreed subprotocol. A mismatch means
      // the Bridge speaks a different protocol version (PROTOCOL §2, close 4402).
      this.emitError({
        scope: 'protocol',
        code: ErrorCode.VERSION_MISMATCH,
        message: `subprotocol mismatch: expected ${PROTOCOL.subprotocol}, got ${negotiated || '(none)'}`,
        retryable: false,
      });
      this.emitState(TransportState.FAILED, { reason: 'subprotocol', expected: PROTOCOL.subprotocol, actual: negotiated });
      try {
        this.socket?.close?.(CloseCode.VERSION, 'version mismatch');
      } catch {
        /* socket already gone */
      }
      return;
    }
    this.counter = new SeqCounter();
    this._sendRaw(buildHello(this.counter, {
      deviceId: this._config?.deviceId ?? '',
      appVersion: this._config?.appVersion ?? '1.0.0-web',
      os: 'web',
    }));
    this._startPing();
  }

  _startPing() {
    this._stopPing();
    const schedule = () => {
      this._pingTimer = this.scheduler.setTimeout(() => {
        if (this.socket && this.socket.readyState === 1) {
          try {
            this._sendRaw(buildPing(this.counter));
          } catch {
            /* socket may have closed between checks */
          }
          schedule();
        }
      }, PROTOCOL.pingIntervalMs);
    };
    schedule();
  }

  _stopPing() {
    if (this._pingTimer !== null) {
      this.scheduler.clearTimeout(this._pingTimer);
      this._pingTimer = null;
    }
  }

  _handleData(data) {
    if (typeof data !== 'string') {
      // Text protocol only; binary audio is native-side in the app.
      if (data instanceof ArrayBuffer) this.emit(TransportEvent.BINARY, new Uint8Array(data));
      return;
    }
    let message;
    try {
      message = JSON.parse(data);
    } catch {
      this.emitError({ scope: 'protocol', code: 5002, message: 'invalid JSON from server', retryable: false });
      return;
    }
    const type = String(message?.type ?? '');
    if (type === MsgType.HELLO_OK) {
      if (message.needPair) {
        this.emitState(TransportState.PAIRING, { serverId: message.serverId });
      } else {
        this.requestAuth();
      }
      this.emit(TransportEvent.MESSAGE, normalizeInboundMessage(message));
      return;
    }
    if (type === MsgType.PAIR_OK) {
      this._setDebugToken(message.deviceToken);
      this.emit(TransportEvent.PAIR_RESULT, { ok: true, serverId: message.serverId });
      this.emitState(TransportState.AUTHENTICATING);
      this.requestAuth();
      return;
    }
    if (type === MsgType.PAIR_ERROR) {
      this.emit(TransportEvent.PAIR_RESULT, {
        ok: false,
        code: message.code,
        message: message.message,
        attemptsLeft: message.attemptsLeft,
        retryable: message.retryable,
      });
      return;
    }
    if (type === MsgType.AUTH_OK) {
      this.emit(TransportEvent.AUTH_RESULT, {
        ok: true,
        serverId: message.serverId,
        sessions: Array.isArray(message.sessions) ? message.sessions : undefined,
      });
      this.emitState(TransportState.ONLINE, { serverId: message.serverId });
      // Debug transport mirrors the bridge: it runs `resync` on our behalf.
      try {
        this._sendRaw(buildResync(this.counter, { lastSeq: this._lastSeenSeq }));
      } catch {
        /* resync is best effort in debug mode */
      }
      return;
    }
    if (type === MsgType.AUTH_ERROR) {
      this.emit(TransportEvent.AUTH_RESULT, { ok: false, code: message.code, message: message.message, needPair: Boolean(message.needPair) });
      this.emitState(message.needPair ? TransportState.PAIRING : TransportState.FAILED);
      return;
    }
    if (type === MsgType.PONG) return;
    if (Number.isInteger(message.seq) && message.seq > 0) this._lastSeenSeq = message.seq;
    const replyTo = message.replyTo;
    const reqId = replyTo !== undefined ? this._pairSeq.get(replyTo) : undefined;
    if (reqId) {
      this._pairSeq.delete(replyTo);
      message = { ...message, _reqTo: reqId };
    }
    if (reqId && this._mediaRequests.has(reqId)) {
      const pending = this._mediaRequests.get(reqId);
      this._mediaRequests.delete(reqId);
      pending?.resolve(message);
      return;
    }
    this.emit(TransportEvent.MESSAGE, normalizeInboundMessage(message));
  }

  _handleClose(event) {
    this._stopPing();
    const code = Number(event?.code ?? 0);
    this.emitState(TransportState.CLOSED, { code, reason: event?.reason ?? '' });
  }

  _disconnect() {
    this._stopPing();
    const socket = this.socket;
    this.socket = null;
    try {
      socket?.close(1000, 'client-disconnect');
    } catch {
      /* already closed */
    }
    this.emitState(TransportState.CLOSED, { code: 1000, intentional: true });
  }

  _requestPair({ code, deviceName } = {}) {
    if (!this.socket) return;
    const message = buildPairRequest(this.counter, { deviceId: this._config?.deviceId ?? '', code, deviceName });
    this._sendRaw(message);
  }

  _requestAuth() {
    const token = this._getDebugToken();
    if (!token) {
      this.emitState(TransportState.PAIRING, { reason: 'no-token' });
      return;
    }
    this.emitState(TransportState.AUTHENTICATING);
    buildAuth(this.counter, { deviceId: this._config?.deviceId ?? '', tokenBase64: token })
      .then((auth) => this._sendRaw(auth))
      .catch((error) => {
        this.emitError({ scope: 'auth', code: 1001, message: String(error?.message ?? error), retryable: false });
        this.emitState(TransportState.FAILED);
      });
  }

  _send(message) {
    if (!this.socket || this.socket.readyState !== 1) {
      this.emitError({ scope: 'transport', code: 5001, message: 'WebSocket is not open', retryable: true });
      return;
    }
    const { _reqId, seq: _ignoredSeq, ...payload } = message;
    const seq = this.counter.next();
    if (typeof _reqId === 'string') this._pairSeq.set(seq, _reqId);
    this._sendRaw({ ...payload, seq });
  }

  _sendRaw(message) {
    if (!this.socket || this.socket.readyState !== 1) return;
    try {
      this.socket.send(encodeMessage(message));
    } catch (error) {
      this.emitError({ scope: 'protocol', code: 5002, message: String(error?.message ?? error), retryable: false });
    }
  }

  _getToken() {
    this.emit(TransportEvent.EVENT, { type: 'token', hasToken: Boolean(this._getDebugToken()) });
  }

  _setToken(token) {
    if (!this.storage) return;
    try {
      if (token) this.storage.setItem(DEBUG_TOKEN_KEY, String(token));
      else this.storage.removeItem(DEBUG_TOKEN_KEY);
    } catch {
      /* storage may be unavailable in private mode */
    }
  }

  _getDebugToken() {
    try {
      return this.storage?.getItem(DEBUG_TOKEN_KEY) ?? null;
    } catch {
      return null;
    }
  }

  _setDebugToken(token) {
    if (token) this._setToken(token);
  }

  _exportLogs() {
    this.emit(TransportEvent.EVENT, {
      type: 'logs.exported',
      text: 'WebSocket debug mode: log export is handled by the Android bridge in the app.',
      truncated: false,
    });
  }

  async _scanPairing() {
    return { supported: false };
  }

  async _pickImage() {
    // Browser debug mode has no native picker (camera/album live in the app).
    return { ok: false, reason: 'unsupported' };
  }

  async _mediaGet(mediaId) {
    if (!mediaId) return { ok: false, code: 3001, reason: 'unknown-media' };
    const _reqId = `media-${++this._mediaSeq}`;
    return new Promise((resolve) => {
      const timer = this.scheduler.setTimeout(() => {
        this._mediaRequests.delete(_reqId);
        resolve({ ok: false, code: 5001, reason: 'timeout' });
      }, 20000);
      this._mediaRequests.set(_reqId, {
        resolve: (message) => {
          this.scheduler.clearTimeout(timer);
          if (message?.type === MsgType.MEDIA_DATA) {
            try {
              resolve({ ok: true, media: normalizeMediaData(message) });
            } catch (error) {
              resolve({ ok: false, code: 3001, reason: String(error?.message ?? error) });
            }
          } else {
            resolve({ ok: false, code: Number(message?.code ?? 3001), reason: message?.message ?? 'media unavailable' });
          }
        },
      });
      try {
        this._send({ ...buildMediaGet(null, { mediaId }), _reqId });
      } catch (error) {
        this.scheduler.clearTimeout(timer);
        this._mediaRequests.delete(_reqId);
        resolve({ ok: false, code: 5001, reason: String(error?.message ?? error) });
      }
    });
  }
}

function safeLocalStorage() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

/** Default URL for the browser debug transport. */
export function defaultWsUrl(host, port, secure = true) {
  if (!host || !port) throw new Error('host and port are required');
  return `${secure ? 'wss' : 'ws'}://${host}:${port}/ws`;
}

/* ========================================================================= */
/* Factory                                                                    */
/* ========================================================================= */

/**
 * Pick a browser/desktop transport mode from settings.
 * @returns {'native'|'ws'} — mock is created by app.js (avoids importing the
 * mock backend into production code paths).
 */
export function resolveWebTransportMode(mode, scope = globalThis) {
  if (mode === TransportMode.NATIVE) return TransportKind.NATIVE;
  if (mode === TransportMode.WS) return TransportKind.WS;
  if (mode === TransportMode.MOCK) return TransportKind.MOCK;
  if (hasNativeBridge(scope)) return TransportKind.NATIVE;
  return TransportKind.MOCK;
}

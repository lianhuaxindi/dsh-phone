/**
 * Controller — DOM-free orchestration between transports, protocol, state
 * machine, store and diagnostics.
 *
 * Views only render snapshots/events; they never talk to a transport or build
 * protocol messages. That keeps the pairing → chat → approval → audio flows
 * identical in the Android app (native bridge), in a desktop browser (ws) and
 * in the demo (mock), and lets `node --test` drive complete scenarios without
 * a browser.
 */

import {
  MsgType,
  PROTOCOL,
  errorSpec,
  closeSpec,
  normalizeSessionList,
  normalizeMessageList,
  normalizeSession,
  normalizeApprovalRequest,
  normalizeQuestionRequest,
  randomUuid,
  validateChatText,
  validatePairingField,
  parsePairUri,
} from './protocol.js';
import {
  Phase,
  Action,
  createInitialState,
  reduce,
  isOnline,
  canStartTurn,
  canRecord,
  StateTransitionError,
} from './state.js';
import { TransportEvent, TransportState, TransportKind } from './transport.js';
import { Diagnostics, LATENCY } from './diagnostics.js';
import { normalizeSettings } from './store.js';
import { RECONNECT_DELAYS_MS, RECONNECT_JITTER } from './config.js';
import { realScheduler } from './scheduler.js';

/** Events the controller emits to the UI. */
export const ControllerEvent = Object.freeze({
  READY: 'ready',
  STATE: 'state',
  SESSIONS: 'sessions',
  SESSION_OPENED: 'session-opened',
  MESSAGE_ADDED: 'message-added',
  MESSAGE_DELTA: 'message-delta',
  MESSAGE_UPDATED: 'message-updated',
  TOOL_STATUS: 'tool-status',
  APPROVAL: 'approval',
  QUESTION: 'question',
  PARTIAL: 'partial',
  ASR_FINAL: 'asr-final',
  AUDIO_STATE: 'audio-state',
  AUDIO_LEVEL: 'audio-level',
  AUDIO_ROUTE: 'audio-route',
  AUDIO_READY: 'audio-ready',
  PAIR_ERROR: 'pair-error',
  ERROR: 'error',
  TOAST: 'toast',
  SETTINGS: 'settings',
  LATENCY: 'latency',
  LOGS_EXPORTED: 'logs-exported',
  SCAN_RESULT: 'scan-result',
  RECONNECTING: 'reconnecting',
});

/** Default request timeout in milliseconds. */
const REQUEST_TIMEOUT_MS = 12000;

/**
 * The application controller.
 */
export class Controller {
  /**
   * @param {object} options
   * @param {import('./transport.js').BaseTransport} options.transport
   * @param {import('./store.js').MemoryStore} options.store
   * @param {Diagnostics} [options.diagnostics]
   * @param {import('./scheduler.js').Scheduler} [options.scheduler]
   * @param {() => number} [options.now] unix-second clock
   */
  constructor({ transport, store, diagnostics, scheduler = realScheduler, now = () => Math.floor(Date.now() / 1000) }) {
    if (!transport) throw new TypeError('Controller requires a transport');
    if (!store) throw new TypeError('Controller requires a store');
    this.transport = transport;
    this.store = store;
    this.scheduler = scheduler;
    this.now = now;
    this.diagnostics = diagnostics ?? new Diagnostics({ scheduler });

    /** @type {Map<string, Set<Function>>} */
    this._listeners = new Map();
    /** @type {ReturnType<typeof createInitialState>} */
    this.state = createInitialState({ at: this.scheduler.now() });
    /** Connection details from the transport (idle/connecting/pairing/...). */
    this.connection = { state: TransportState.IDLE, detail: undefined, since: this.scheduler.now() };
    /** @type {object} settings snapshot */
    this.settings = normalizeSettings();
    this.deviceId = null;
    this.transportKind = transport.kind;
    /** @type {object[]} */
    this.sessions = [];
    /** @type {string|null} */
    this.activeSessionId = null;
    /** @type {Map<string, object[]>} */
    this.messages = new Map();
    /** @type {Map<string, string>} composer drafts per session (TC-WEB-03) */
    this._drafts = new Map();
    /** @type {object|null} */
    this.approval = null;
    /** @type {object|null} */
    this.question = null;
    this.recording = {
      state: 'idle',
      partial: '',
      level: 0,
      route: null,
      deviceName: null,
      startedAt: null,
    };
    this.audioRoute = { route: null, deviceName: null, preferredUnavailable: false };

    this._pending = new Map();
    this._reqSeq = 0;
    this._onlineHandled = false;
    this._pendingPair = null;
    this._pairRequested = false;
    this._reconnectAttempt = 0;
    this._reconnectTimer = null;
    this._approvalTimer = null;
    this._questionTimer = null;
    this._streaming = null;
    this._pendingTurn = null;
    this._lastUserText = '';
    this._turnStartedAt = null;
    this._firstDeltaSeen = false;
    this._firstPartialSeen = false;
    this._recordingStartedAt = null;
    this._ready = false;
  }

  /* -------------------------------------------------------------- events */

  /** Subscribe to a controller event; returns unsubscribe. */
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

  /** Emit; listener errors are logged, never fatal. */
  emit(event, payload) {
    const handlers = this._listeners.get(event);
    if (!handlers) return;
    for (const handler of [...handlers]) {
      try {
        handler(payload);
      } catch (error) {
        this.diagnostics.error('ui', `listener for ${event} threw: ${error?.message ?? error}`);
      }
    }
  }

  /* --------------------------------------------------------------- boot */

  /** Load settings/device id and wire the transport. */
  async init() {
    this.settings = await this.store.getSettings();
    this.deviceId = await this.store.getDeviceId();
    if (!this.deviceId) {
      this.deviceId = randomUuid();
      await this.store.setDeviceId(this.deviceId);
    }
    this.diagnostics.info('app', `controller ready transport=${this.transport.kind} lang=${this.settings.lang}`);
    this._bindTransport();
    this._ready = true;
    this.emit(ControllerEvent.READY, {
      settings: this.settings,
      deviceId: this.deviceId,
      transportKind: this.transport.kind,
      snapshot: this.snapshot(),
    });
    // Ask the bridge whether a token exists (native answers with a `token` event).
    try {
      this.transport.getToken();
    } catch (error) {
      this.diagnostics.warn('bridge', `getToken failed: ${error?.message ?? error}`);
    }
    return this;
  }

  _bindTransport() {
    this.transport.on(TransportEvent.STATE, (event) => this._onTransportState(event.state, event.detail));
    this.transport.on(TransportEvent.MESSAGE, (message) => this._onMessage(message));
    this.transport.on(TransportEvent.PAIR_RESULT, (result) => this._onPairResult(result));
    this.transport.on(TransportEvent.AUTH_RESULT, (result) => this._onAuthResult(result));
    this.transport.on(TransportEvent.ERROR, (error) => this._onTransportError(error));
    this.transport.on(TransportEvent.EVENT, (event) => this._onTransportEvent(event));
  }

  /* ------------------------------------------------------------ snapshot */

  /** Full UI snapshot (views render from this after every change). */
  snapshot() {
    return {
      phase: this.state.phase,
      state: this.state,
      connection: { ...this.connection },
      settings: { ...this.settings },
      deviceId: this.deviceId,
      transportKind: this.transportKind,
      sessions: this.sessions.map((session) => ({ ...session })),
      activeSessionId: this.activeSessionId,
      messages: this.activeSessionId ? this.messagesFor(this.activeSessionId) : [],
      approval: this.approval,
      question: this.question,
      recording: { ...this.recording },
      audioRoute: { ...this.audioRoute },
      latency: this.diagnostics.latencyStats(),
      online: isOnline(this.state),
      canSend: canStartTurn(this.state),
      canRecord: canRecord(this.state),
    };
  }

  /** Messages for one session (empty array when unknown). */
  messagesFor(sessionId) {
    return (this.messages.get(sessionId) ?? []).map((message) => ({ ...message }));
  }

  /* ------------------------------------------------------- state helpers */

  _dispatch(type, extra = {}) {
    const action = { type, at: this.scheduler.now(), ...extra };
    try {
      this.state = reduce(this.state, action);
    } catch (error) {
      if (error instanceof StateTransitionError) {
        this.diagnostics.warn('state', `invalid transition ${error.from} -> ${error.to} for ${type}; resyncing`);
        try {
          this.state = reduce(createInitialState({ at: this.scheduler.now() }), action);
        } catch {
          // Keep the old phase but do not crash the UI.
        }
      } else {
        throw error;
      }
    }
    this.emit(ControllerEvent.STATE, this.snapshot());
  }

  /* --------------------------------------------------- transport events */

  _onTransportState(state, detail) {
    this.connection = { state, detail, since: this.scheduler.now() };
    this.diagnostics.debug('transport', `state=${state}${detail?.code ? ` code=${detail.code}` : ''}`);

    switch (state) {
      case TransportState.CONNECTING:
        this._onlineHandled = false;
        if (this.state.phase === Phase.IDLE) this._dispatch(Action.CONNECT_REQUESTED);
        break;
      case TransportState.PAIRING:
        if (this.state.phase !== Phase.PAIRING) {
          if (this.state.phase === Phase.IDLE) this._dispatch(Action.CONNECT_REQUESTED);
          this._dispatch(Action.CONNECTED, { needPair: true });
        }
        if (this._pendingPair && !this._pairRequested) {
          this._pairRequested = true;
          this.transport.requestPair(this._pendingPair);
        }
        break;
      case TransportState.AUTHENTICATING:
        if (this.state.phase === Phase.IDLE) this._dispatch(Action.CONNECT_REQUESTED);
        if (this.state.phase === Phase.CONNECTING) this._dispatch(Action.CONNECTED, { needPair: false });
        else if (this.state.phase === Phase.PAIRING) this._dispatch(Action.PAIR_SUCCEEDED);
        break;
      case TransportState.RECONNECTING:
        this._onlineHandled = false;
        this.emit(ControllerEvent.RECONNECTING, { attempt: this._reconnectAttempt });
        break;
      case TransportState.ONLINE:
        this._onOnline(detail);
        break;
      case TransportState.FAILED:
        this._dispatch(Action.CONNECT_FAILED, { error: detail ?? null });
        this.emit(ControllerEvent.STATE, this.snapshot());
        break;
      case TransportState.CLOSED:
        this._onClosed(detail);
        break;
      default:
        break;
    }
    this.emit(ControllerEvent.STATE, this.snapshot());
  }

  _onOnline(detail) {
    if (!this._onlineHandled) {
      this._onlineHandled = true;
      this._reconnectAttempt = 0;
      this._clearReconnectTimer();
      this._dispatch(Action.AUTH_SUCCEEDED);
      this.diagnostics.info('transport', `online${detail?.serverId ? ` server=${detail.serverId}` : ''}`);
      this.refreshSessions();
    }
  }

  _onClosed(detail) {
    const intentional = Boolean(detail?.intentional);
    const code = Number(detail?.code ?? 0);
    this._onlineHandled = false;
    this._dispatch(Action.DISCONNECTED);
    if (intentional) return;
    const spec = closeSpec(code);
    this.diagnostics.warn('transport', `closed code=${code} retryable=${spec.retryable}`);
    if (!spec.retryable) {
      if (code === 4401 || code === 4410) this._clearToken();
      this.emit(ControllerEvent.ERROR, { code, scope: 'close', retryable: false, message: '' });
      return;
    }
    this.emit(ControllerEvent.ERROR, { code, scope: 'close', retryable: true, message: '' });
    this._scheduleReconnect();
  }

  _onTransportError(error) {
    const code = Number(error?.code ?? 5001);
    const retryable = Boolean(error?.retryable);
    this.diagnostics.recordError({ code, message: error?.message ?? '', retryable, source: error?.scope ?? 'transport' });
    this.emit(ControllerEvent.ERROR, { code, scope: error?.scope ?? 'transport', retryable, message: error?.message ?? '' });
  }

  _onTransportEvent(event) {
    if (!event || typeof event !== 'object') return;
    switch (event.type) {
      case 'audio.state':
        this._onAudioState(event.state, event.reason);
        break;
      case 'audio.ready':
        this.emit(ControllerEvent.AUDIO_READY, { asrMode: event.asrMode, lang: event.lang, codecs: event.codecs });
        break;
      case 'audio.route':
        this.audioRoute = {
          route: event.route ?? null,
          deviceName: event.deviceName ?? null,
          preferredUnavailable: Boolean(event.preferredUnavailable),
        };
        this.recording.route = this.audioRoute.route;
        this.recording.deviceName = this.audioRoute.deviceName;
        this.emit(ControllerEvent.AUDIO_ROUTE, { ...this.audioRoute });
        break;
      case 'audio.level':
        this.recording.level = event.level;
        this.emit(ControllerEvent.AUDIO_LEVEL, { level: event.level });
        break;
      case 'audio.stopped':
        break;
      case 'audio.error':
        this._onTransportError({ scope: 'audio', code: event.code ?? 5001, message: event.message ?? '', retryable: event.retryable });
        break;
      case 'token':
        this._onToken(event);
        break;
      case 'logs.exported':
        this.emit(ControllerEvent.LOGS_EXPORTED, { text: event.text ?? '', truncated: Boolean(event.truncated) });
        break;
      case 'settings.audioRoute':
        if (event.address !== undefined) this.audioRoute = { ...this.audioRoute, address: event.address };
        break;
      default:
        this.diagnostics.debug('bridge', `event ${event.type}`);
        break;
    }
  }

  _onToken(event) {
    this._hasToken = Boolean(event.hasToken);
    this.diagnostics.info('bridge', `token present=${this._hasToken}`);
    this.emit('token', { ...event });
    if (!this._hasToken || this._pendingPair) return;
    // A stored token means the device is already paired: reconnect silently.
    const patch = { paired: true };
    if (typeof event.host === 'string' && event.host) patch.host = event.host;
    if (Number.isInteger(event.port) && event.port > 0) patch.port = event.port;
    this.settings = normalizeSettings({ ...this.settings, ...patch });
    if (this.settings.host && this.settings.port) this._beginConnect();
  }

  /* ------------------------------------------------------- pair / auth */

  /**
   * Start pairing from the pairing screen. Accepts a full `dsphone://pair?...`
   * URI in `uri` or individual fields; fields win over URI values.
   */
  async connectPairing({ uri, host, port, fp, code, deviceName } = {}) {
    try {
      const parsed = uri ? parsePairUri(uri) : null;
      const hostValue = validatePairingField('host', host ?? parsed?.host);
      const portValue = validatePairingField('port', port ?? parsed?.port);
      const fpValue = fp !== undefined && fp !== '' ? validatePairingField('fp', fp) : parsed?.fp ?? '';
      const codeValue = validatePairingField('code', code ?? parsed?.code);
      this.settings = normalizeSettings({
        ...this.settings,
        host: hostValue,
        port: portValue,
        fp: fpValue,
        deviceName: deviceName || this.settings.deviceName,
      });
      await this.store.saveSettings(this.settings);
      this._pendingPair = { code: codeValue, deviceName: this.settings.deviceName };
      this._pairRequested = false;
      this.diagnostics.info('pair', `connecting to ${hostValue}:${portValue}`);
      this._beginConnect();
      return { ok: true };
    } catch (error) {
      this.diagnostics.warn('pair', `invalid pairing fields: ${error?.message ?? error}`);
      this.emit(ControllerEvent.TOAST, { key: 'pairing.invalidFields', vars: { reason: error?.message ?? '' } });
      return { ok: false, error: error?.message ?? String(error) };
    }
  }

  _beginConnect() {
    this._dispatch(Action.CONNECT_REQUESTED);
    this.emit(ControllerEvent.STATE, this.snapshot());
    this.transport.connect({
      host: this.settings.host,
      port: this.settings.port,
      fp: this.settings.fp,
      deviceId: this.deviceId,
    });
  }

  /** Manual "reconnect" (diagnostics). */
  async reconnectBridge() {
    this._clearReconnectTimer();
    this._reconnectAttempt = 0;
    this.diagnostics.info('transport', 'manual reconnect requested');
    this.emit(ControllerEvent.RECONNECTING, { attempt: 0, manual: true });
    try {
      this.transport.reconnect({
        host: this.settings.host,
        port: this.settings.port,
        fp: this.settings.fp,
        deviceId: this.deviceId,
      });
    } catch (error) {
      this.emit(ControllerEvent.ERROR, { code: 5001, scope: 'transport', retryable: true, message: String(error?.message ?? error) });
    }
  }

  /** Scan/paste pairing info; V1 native returns `supported:false`. */
  async scanPairing() {
    try {
      const result = await this.transport.scanPairing();
      this.emit(ControllerEvent.SCAN_RESULT, result);
      return result;
    } catch (error) {
      const result = { supported: false, error: String(error?.message ?? error) };
      this.emit(ControllerEvent.SCAN_RESULT, result);
      return result;
    }
  }

  _onPairResult(result) {
    if (result.ok) {
      this._pendingPair = null;
      this._pairRequested = false;
      this.settings = normalizeSettings({ ...this.settings, paired: true });
      this.store.saveSettings(this.settings).catch(() => {});
      this._dispatch(Action.PAIR_SUCCEEDED);
      this.emit(ControllerEvent.TOAST, { key: 'toast.paired' });
      this.diagnostics.info('pair', 'pairing succeeded');
    } else {
      this._pairRequested = false;
      this._dispatch(Action.PAIR_FAILED, {
        error: { code: result.code ?? 1002, message: result.message ?? '' },
      });
      this.emit(ControllerEvent.PAIR_ERROR, {
        code: Number(result.code ?? 1002),
        message: result.message ?? '',
        attemptsLeft: result.attemptsLeft,
        retryable: result.retryable !== false,
      });
      this.diagnostics.warn('pair', `pairing failed code=${result.code ?? 1002}`);
    }
    this.emit(ControllerEvent.STATE, this.snapshot());
  }

  _onAuthResult(result) {
    if (result.ok) {
      this.settings = normalizeSettings({ ...this.settings, paired: true });
      this.store.saveSettings(this.settings).catch(() => {});
      if (Array.isArray(result.sessions)) this._applySessionList(result.sessions);
      // net.state=online follows and drives the phase; this is just data.
    } else {
      this.diagnostics.warn('auth', `auth failed code=${result.code ?? 1001} needPair=${Boolean(result.needPair)}`);
      if (result.needPair) {
        this._dispatch(Action.CONNECTED, { needPair: true });
      } else {
        this._dispatch(Action.AUTH_FAILED, { error: { code: result.code ?? 1001, message: result.message ?? '' } });
        this.emit(ControllerEvent.ERROR, {
          code: Number(result.code ?? 1001),
          scope: 'auth',
          retryable: false,
          message: result.message ?? '',
        });
        this._clearToken();
      }
    }
    this.emit(ControllerEvent.STATE, this.snapshot());
  }

  _clearToken() {
    this.settings = normalizeSettings({ ...this.settings, paired: false });
    this.store.saveSettings(this.settings).catch(() => {});
    try {
      this.transport.setToken(null);
    } catch {
      /* bridge may be gone */
    }
  }

  /* ------------------------------------------------------- reconnect */

  _scheduleReconnect() {
    if (this._reconnectTimer !== null) return;
    if (!this.settings.host || !this.settings.port) return;
    const index = Math.min(this._reconnectAttempt, RECONNECT_DELAYS_MS.length - 1);
    const base = RECONNECT_DELAYS_MS[index];
    const jitter = 1 + (Math.random() * 2 - 1) * RECONNECT_JITTER;
    const delayMs = Math.round(base * jitter);
    this._reconnectAttempt += 1;
    this.emit(ControllerEvent.RECONNECTING, { attempt: this._reconnectAttempt, delayMs });
    this.diagnostics.info('transport', `reconnect in ${delayMs}ms (attempt ${this._reconnectAttempt})`);
    this._reconnectTimer = this.scheduler.setTimeout(() => {
      this._reconnectTimer = null;
      this._dispatch(Action.CONNECT_REQUESTED);
      this.transport.connect({
        host: this.settings.host,
        port: this.settings.port,
        fp: this.settings.fp,
        deviceId: this.deviceId,
      });
    }, delayMs);
  }

  _clearReconnectTimer() {
    if (this._reconnectTimer !== null) {
      this.scheduler.clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
  }

  /* ------------------------------------------------------------ requests */

  _request(type, fields = {}, { timeoutMs = REQUEST_TIMEOUT_MS } = {}) {
    const _reqId = `r${++this._reqSeq}`;
    const promise = new Promise((resolve) => {
      const timer = this.scheduler.setTimeout(() => {
        this._pending.delete(_reqId);
        this.diagnostics.warn('protocol', `request ${type} timed out`);
        resolve(null);
      }, timeoutMs);
      this._pending.set(_reqId, {
        type,
        resolve: (message) => {
          this.scheduler.clearTimeout(timer);
          resolve(message);
        },
      });
    });
    try {
      this.transport.send({ type, ...fields, _reqId });
    } catch (error) {
      this.diagnostics.error('protocol', `send ${type} failed: ${error?.message ?? error}`);
      const pending = this._pending.get(_reqId);
      this._pending.delete(_reqId);
      pending?.resolve(null);
    }
    return promise;
  }

  _resolvePending(message) {
    const reqId = message?._reqTo;
    if (typeof reqId !== 'string') return;
    const pending = this._pending.get(reqId);
    if (!pending) return;
    this._pending.delete(reqId);
    pending.resolve(message);
  }

  /* ------------------------------------------------------------ sessions */

  async refreshSessions() {
    const response = await this._request(MsgType.SESSION_LIST);
    if (response?.type === MsgType.SESSION_LIST_OK) this._applySessionList(response.sessions);
    else if (response?.type === MsgType.ERROR) this._handleErrorFrame(response);
  }

  _applySessionList(rawSessions) {
    let sessions = [];
    try {
      sessions = normalizeSessionList(rawSessions);
    } catch (error) {
      this.diagnostics.warn('sessions', `invalid session list: ${error?.message ?? error}`);
      return;
    }
    const merged = new Map();
    for (const session of this.sessions) merged.set(session.sessionId, session);
    for (const session of sessions) merged.set(session.sessionId, session);
    this.sessions = [...merged.values()].sort((a, b) => b.updatedAt - a.updatedAt);
    for (const session of sessions) this.store.saveSession(session).catch(() => {});
    this.emit(ControllerEvent.SESSIONS, { sessions: this.sessions.map((s) => ({ ...s })), activeSessionId: this.activeSessionId });
  }

  async createSession({ title } = {}) {
    const response = await this._request(MsgType.SESSION_CREATE, title ? { title } : {});
    if (response?.type === MsgType.SESSION_CREATE_OK && response.session) {
      let session;
      try {
        session = normalizeSession(response.session);
      } catch {
        return null;
      }
      this.sessions = [session, ...this.sessions.filter((s) => s.sessionId !== session.sessionId)];
      this.store.saveSession(session).catch(() => {});
      this.emit(ControllerEvent.SESSIONS, { sessions: this.sessions.map((s) => ({ ...s })), activeSessionId: this.activeSessionId });
      return session;
    }
    return null;
  }

  async openSession(sessionId) {
    if (!sessionId) return;
    this.activeSessionId = sessionId;
    this.settings = normalizeSettings({ ...this.settings, lastActiveSessionId: sessionId });
    this.store.saveSettings(this.settings).catch(() => {});
    const local = await this.store.getMessages(sessionId);
    this.messages.set(sessionId, local);
    const draft = await this.store.getDraft(sessionId);
    if (draft) this._drafts.set(String(sessionId), draft);
    else this._drafts.delete(String(sessionId));
    this.emit(ControllerEvent.SESSION_OPENED, {
      sessionId,
      messages: this.messagesFor(sessionId),
      session: this.sessions.find((s) => s.sessionId === sessionId) ?? null,
      draft,
    });
    const response = await this._request(MsgType.SESSION_RESUME, { sessionId });
    if (response?.type === MsgType.SESSION_RESUME_OK) {
      const serverMessages = normalizeMessageListSafe(response.messages);
      this.messages.set(sessionId, this._mergeMessages(sessionId, local, serverMessages));
      this.emit(ControllerEvent.SESSION_OPENED, {
        sessionId,
        messages: this.messagesFor(sessionId),
        session: this.sessions.find((s) => s.sessionId === sessionId) ?? null,
        draft,
      });
    }
  }

  _mergeMessages(sessionId, localMessages, serverMessages) {
    const byId = new Map();
    for (const message of serverMessages) byId.set(message.messageId, message);
    for (const message of localMessages) {
      if (byId.has(message.messageId)) continue;
      const isLocalOptimistic = String(message.messageId).startsWith('local-');
      const duplicate = isLocalOptimistic
        ? serverMessages.some(
            (server) =>
              server.role === message.role &&
              server.text === message.text &&
              Math.abs(server.createdAt - message.createdAt) <= 180,
          )
        : false;
      if (!duplicate) byId.set(message.messageId, message);
    }
    const merged = [...byId.values()].sort((a, b) => a.createdAt - b.createdAt);
    this.store.saveMessages(merged).catch(() => {});
    return merged;
  }

  async loadMoreHistory() {
    const sessionId = this.activeSessionId;
    if (!sessionId) return;
    const current = this.messages.get(sessionId) ?? [];
    const before = current.find((message) => !String(message.messageId).startsWith('local-'))?.createdAt ?? null;
    const response = await this._request(MsgType.SESSION_HISTORY, { sessionId, limit: 50, before });
    if (response?.type === MsgType.SESSION_HISTORY_OK) {
      const older = normalizeMessageListSafe(response.messages);
      const merged = new Map(current.map((message) => [message.messageId, message]));
      for (const message of older) merged.set(message.messageId, message);
      this.messages.set(sessionId, [...merged.values()].sort((a, b) => a.createdAt - b.createdAt));
      this.emit(ControllerEvent.SESSION_OPENED, { sessionId, messages: this.messagesFor(sessionId), older: true });
    }
  }

  /** Delete a session locally (server data is untouched; UI confirms twice). */
  async deleteSessionLocal(sessionId) {
    await this.store.deleteSession(sessionId);
    this.sessions = this.sessions.filter((s) => s.sessionId !== sessionId);
    this.messages.delete(sessionId);
    if (this.activeSessionId === sessionId) {
      this.activeSessionId = null;
      this.settings = normalizeSettings({ ...this.settings, lastActiveSessionId: null });
      this.store.saveSettings(this.settings).catch(() => {});
    }
    this.diagnostics.info('sessions', `deleted local session ${sessionId}`);
    this.emit(ControllerEvent.SESSIONS, { sessions: this.sessions.map((s) => ({ ...s })), activeSessionId: this.activeSessionId });
  }

  /* -------------------------------------------------------------- drafts */

  /** Composer draft for a session (cached; '' when none). */
  draftFor(sessionId) {
    if (!sessionId) return '';
    return this._drafts.get(String(sessionId)) ?? '';
  }

  /** Persist the composer draft (local only; never sent over the protocol). */
  saveDraft(sessionId, text) {
    if (!sessionId) return;
    const key = String(sessionId);
    const value = typeof text === 'string' ? text : '';
    if (value.trim()) this._drafts.set(key, value);
    else this._drafts.delete(key);
    this.store.saveDraft(sessionId, value).catch(() => {});
  }

  /** Clear a session draft (after send / explicit clear). */
  clearDraft(sessionId) {
    if (!sessionId) return;
    this._drafts.delete(String(sessionId));
    this.store.deleteDraft(sessionId).catch(() => {});
  }

  /* ---------------------------------------------------------------- chat */

  /** Send a text turn. Returns the optimistic user message or null. */
  async sendText(text) {
    let value;
    try {
      value = validateChatText(text);
    } catch (error) {
      const raw = typeof text === 'string' ? text : '';
      if (!raw.trim()) {
        this.emit(ControllerEvent.TOAST, { key: 'chat.emptyBlocked' });
      } else if (raw.length > PROTOCOL.maxTextChars) {
        this.emit(ControllerEvent.TOAST, { key: 'chat.tooLong', vars: { max: PROTOCOL.maxTextChars } });
      } else {
        this.emit(ControllerEvent.TOAST, { key: 'chat.emptyBlocked' });
      }
      this.diagnostics.warn('chat', `send rejected: ${error?.message ?? error}`);
      return null;
    }
    if (!canStartTurn(this.state)) {
      this.emit(ControllerEvent.TOAST, { key: 'chat.notOnline' });
      return null;
    }
    let sessionId = this.activeSessionId;
    if (!sessionId) {
      const created = await this.createSession();
      sessionId = created?.sessionId ?? null;
      if (!sessionId) {
        this.emit(ControllerEvent.TOAST, { key: 'chat.noSession' });
        return null;
      }
      this.activeSessionId = sessionId;
    }
    const clientId = randomUuid();
    const userMessage = {
      messageId: `local-${clientId}`,
      sessionId,
      role: 'user',
      text: value,
      status: 'sending',
      createdAt: this.now(),
      clientId,
      local: true,
    };
    this._lastUserText = value;
    this.clearDraft(sessionId);
    this._addMessage(sessionId, userMessage);
    this._pendingTurn = { clientId, sessionId, userMessage, assistantMessageId: null };
    this._turnStartedAt = this.scheduler.now();
    this._firstDeltaSeen = false;
    this._dispatch(Action.SEND_STARTED);
    this._touchSessionPreview(sessionId, value);
    try {
      this.transport.send({ type: MsgType.CHAT_SEND, clientId, sessionId, text: value });
      this.diagnostics.info('chat', `sent clientId=${clientId} session=${sessionId}`);
    } catch (error) {
      this._failTurn({ code: 5001, message: String(error?.message ?? error) });
      return null;
    }
    this.emit(ControllerEvent.STATE, this.snapshot());
    return userMessage;
  }

  /** Retry the last user turn (error banner action). */
  retryLastTurn() {
    if (this._lastUserText) this.sendText(this._lastUserText);
  }

  /** Ask the server to cancel the in-flight turn. */
  cancelTurn() {
    const sessionId = this.activeSessionId;
    if (!sessionId) return;
    try {
      this.transport.send({ type: MsgType.CHAT_CANCEL, sessionId });
    } catch (error) {
      this.diagnostics.warn('chat', `cancel failed: ${error?.message ?? error}`);
    }
  }

  _onChatAck(message) {
    const pending = this._pendingTurn;
    if (!pending || pending.clientId !== message.clientId) return;
    const userMessage = this.messages.get(pending.sessionId)?.find((m) => m.clientId === message.clientId);
    if (userMessage) {
      userMessage.status = 'done';
      this._emitMessageUpdated(pending.sessionId, userMessage);
    }
    const assistantMessage = {
      messageId: String(message.messageId ?? `assistant-${pending.clientId}`),
      sessionId: pending.sessionId,
      role: 'assistant',
      text: '',
      status: 'streaming',
      createdAt: this.now(),
    };
    pending.assistantMessageId = assistantMessage.messageId;
    this._streaming = { sessionId: pending.sessionId, messageId: assistantMessage.messageId };
    this._addMessage(pending.sessionId, assistantMessage);
  }

  _onAssistantDelta(message) {
    const sessionId = message.sessionId;
    const messageId = String(message.messageId ?? '');
    if (!messageId) return;
    const list = this.messages.get(sessionId);
    if (!list) return;
    let target = list.find((m) => m.messageId === messageId);
    if (!target) {
      target = {
        messageId,
        sessionId,
        role: 'assistant',
        text: '',
        status: 'streaming',
        createdAt: this.now(),
      };
      list.push(target);
      this.emit(ControllerEvent.MESSAGE_ADDED, { sessionId, message: { ...target } });
    }
    target.text += String(message.text ?? '');
    target.status = 'streaming';
    this._streaming = { sessionId, messageId };
    if (!this._firstDeltaSeen) {
      this._firstDeltaSeen = true;
      if (this._turnStartedAt !== null) {
        this.diagnostics.latency(LATENCY.CHAT_FIRST_DELTA, this.scheduler.now() - this._turnStartedAt);
        this.emit(ControllerEvent.LATENCY, this.diagnostics.latencyStats());
      }
    }
    this.emit(ControllerEvent.MESSAGE_DELTA, {
      sessionId,
      messageId,
      text: target.text,
      delta: String(message.text ?? ''),
      index: message.index,
    });
  }

  _onAssistantDone(message) {
    const sessionId = message.sessionId ?? this._pendingTurn?.sessionId;
    if (!sessionId) return;
    const messageId = String(message.messageId ?? '');
    const list = this.messages.get(sessionId);
    const target = list?.find((m) => m.messageId === messageId);
    if (target) {
      target.status = target.status === 'error' ? 'error' : 'done';
      this._emitMessageUpdated(sessionId, target);
      this.store.saveMessage(target).catch(() => {});
    }
    const finishReason = message.finishReason ?? 'completed';
    if (this._turnStartedAt !== null) {
      this.diagnostics.latency(LATENCY.TURN_COMPLETE, this.scheduler.now() - this._turnStartedAt);
      this._turnStartedAt = null;
    }
    this._streaming = null;
    this._pendingTurn = null;
    this._dispatch(Action.TURN_FINISHED);
    this._touchSessionPreview(sessionId, target?.text?.slice(-80) ?? undefined);
    this.diagnostics.info('chat', `turn finished reason=${finishReason}`);
    this.emit(ControllerEvent.LATENCY, this.diagnostics.latencyStats());
    this.emit(ControllerEvent.STATE, this.snapshot());
  }

  _onTurnError(message) {
    const code = Number(message.code ?? 3003);
    this.diagnostics.recordError({ code, message: message.message ?? '', retryable: true, source: 'turn' });
    const pending = this._pendingTurn;
    if (pending) {
      const list = this.messages.get(pending.sessionId);
      const target = list?.find((m) => m.messageId === pending.assistantMessageId);
      if (target) {
        target.status = 'error';
        this._emitMessageUpdated(pending.sessionId, target);
      }
    }
    this.emit(ControllerEvent.ERROR, {
      code,
      scope: 'chat',
      retryable: true,
      message: message.message ?? '',
      canRetry: Boolean(this._lastUserText),
    });
    this._pendingTurn = null;
    this._streaming = null;
    this._dispatch(Action.TURN_ERROR, { error: { code, message: message.message ?? '' } });
    this.emit(ControllerEvent.STATE, this.snapshot());
  }

  _failTurn(error) {
    this.emit(ControllerEvent.ERROR, { code: error.code ?? 5001, scope: 'chat', retryable: true, message: error.message ?? '' });
    this._pendingTurn = null;
    this._streaming = null;
    try {
      this._dispatch(Action.TURN_ERROR, { error });
    } catch {
      /* state machine already reset */
    }
    this.emit(ControllerEvent.STATE, this.snapshot());
  }

  _onToolStatus(message) {
    this.emit(ControllerEvent.TOOL_STATUS, {
      sessionId: message.sessionId,
      toolCallId: message.toolCallId,
      name: message.name,
      state: message.state,
      title: message.title,
    });
  }

  /* ------------------------------------------------------------ messages */

  _addMessage(sessionId, message) {
    let list = this.messages.get(sessionId);
    if (!list) {
      list = [];
      this.messages.set(sessionId, list);
    }
    if (list.some((existing) => existing.messageId === message.messageId)) return;
    list.push(message);
    this.store.saveMessage(message).catch(() => {});
    this.emit(ControllerEvent.MESSAGE_ADDED, { sessionId, message: { ...message } });
  }

  _emitMessageUpdated(sessionId, message) {
    this.emit(ControllerEvent.MESSAGE_UPDATED, { sessionId, message: { ...message } });
  }

  _touchSessionPreview(sessionId, text) {
    if (text === undefined) return;
    const session = this.sessions.find((s) => s.sessionId === sessionId);
    if (!session) return;
    session.lastPreview = String(text).replace(/\s+/g, ' ').slice(0, 80);
    session.updatedAt = this.now();
    this.sessions = [...this.sessions].sort((a, b) => b.updatedAt - a.updatedAt);
    this.store.saveSession(session).catch(() => {});
    this.emit(ControllerEvent.SESSIONS, { sessions: this.sessions.map((s) => ({ ...s })), activeSessionId: this.activeSessionId });
  }

  /* ---------------------------------------------------- incoming messages */

  _onMessage(rawMessage) {
    let message;
    try {
      message = normalizeInboundMessageSafe(rawMessage);
    } catch (error) {
      this.diagnostics.warn('protocol', `dropped invalid message: ${error?.message ?? error}`);
      return;
    }
    this.diagnostics.debug('protocol', `<- ${message.type}${message._reqTo ? ` reqTo=${message._reqTo}` : ''}`);
    this._resolvePending(message);

    switch (message.type) {
      case MsgType.SESSION_LIST_OK:
        this._applySessionList(message.sessions);
        break;
      case MsgType.SESSION_CREATE_OK:
      case MsgType.SESSION_RESUME_OK:
      case MsgType.SESSION_HISTORY_OK:
        // Handled by the awaiting caller; this is a fallback for transports
        // that do not back-fill `_reqTo`.
        break;
      case MsgType.CHAT_ACK:
        this._onChatAck(message);
        break;
      case MsgType.ASSISTANT_DELTA:
        this._onAssistantDelta(message);
        break;
      case MsgType.ASSISTANT_DONE:
        this._onAssistantDone(message);
        break;
      case MsgType.TURN_ERROR:
        this._onTurnError(message);
        break;
      case MsgType.TOOL_STATUS:
        this._onToolStatus(message);
        break;
      case MsgType.APPROVAL_REQUEST:
        this._onApprovalRequest(message);
        break;
      case MsgType.APPROVAL_EXPIRED:
        this._onApprovalExpired(message);
        break;
      case MsgType.QUESTION_REQUEST:
        this._onQuestionRequest(message);
        break;
      case MsgType.QUESTION_EXPIRED:
        this._onQuestionExpired(message);
        break;
      case MsgType.ASR_PARTIAL:
        this._onAsrPartial(message);
        break;
      case MsgType.ASR_FINAL:
        this._onAsrFinal(message);
        break;
      case MsgType.ERROR:
        this._handleErrorFrame(message);
        break;
      case MsgType.RESYNC_OK:
      case MsgType.PONG:
      case MsgType.HELLO_OK:
      case MsgType.AUTH_OK:
      case MsgType.PAIR_OK:
        break;
      default:
        this.diagnostics.debug('protocol', `unhandled type ${message.type}`);
        break;
    }
  }

  _handleErrorFrame(message) {
    const code = Number(message.code ?? 5001);
    const retryable = Boolean(message.retryable ?? errorSpec(code).retryable);
    this.diagnostics.recordError({ code, message: message.message ?? '', retryable, source: 'protocol' });
    if (code === 3001) this.refreshSessions();
    if (code === 1001) this._clearToken();
    if (this._pendingTurn && (code === 4001 || code === 2001 || code === 3003)) {
      this._pendingTurn = null;
      this._streaming = null;
      this._dispatch(Action.TURN_ERROR, { error: { code, message: message.message ?? '' } });
    }
    this.emit(ControllerEvent.ERROR, {
      code,
      scope: message.scope ?? 'protocol',
      retryable,
      message: message.message ?? '',
      canRetry: code === 4001 || code === 3003 || code === 2001,
    });
    this.emit(ControllerEvent.STATE, this.snapshot());
  }

  /* ------------------------------------------------------------ approval */

  _onApprovalRequest(raw) {
    let request;
    try {
      request = normalizeApprovalRequest(raw);
    } catch (error) {
      this.diagnostics.warn('approval', `invalid approval.request: ${error?.message ?? error}`);
      return;
    }
    request.sessionId = request.sessionId ?? this.activeSessionId;
    this.approval = request;
    this._dispatch(Action.APPROVAL_SHOWN);
    this.emit(ControllerEvent.APPROVAL, request);
    this.emit(ControllerEvent.STATE, this.snapshot());
    this._scheduleDeadline(request.id, request.deadline, 'approval');
  }

  _scheduleDeadline(id, deadlineSeconds, kind) {
    this._clearDeadline(kind);
    if (!deadlineSeconds) return;
    const remainingSeconds = Number(deadlineSeconds) - this.now();
    if (remainingSeconds <= 0) {
      this._expireLocally(kind, id);
      return;
    }
    const timer = this.scheduler.setTimeout(() => {
      this[kind === 'approval' ? '_approvalTimer' : '_questionTimer'] = null;
      if (kind === 'approval' && this.approval?.id === id) this._expireLocally(kind, id);
      if (kind === 'question' && this.question?.id === id) this._expireLocally(kind, id);
    }, remainingSeconds * 1000);
    this[kind === 'approval' ? '_approvalTimer' : '_questionTimer'] = timer;
  }

  _clearDeadline(kind) {
    const key = kind === 'approval' ? '_approvalTimer' : '_questionTimer';
    if (this[key] !== null) {
      this.scheduler.clearTimeout(this[key]);
      this[key] = null;
    }
  }

  _expireLocally(kind, id) {
    if (kind === 'approval') {
      const request = this.approval;
      if (!request || request.id !== id) return;
      this.approval = null;
      this._dispatch(Action.APPROVAL_RESOLVED);
      this.emit(ControllerEvent.APPROVAL, null);
      this.emit(ControllerEvent.TOAST, { key: 'toast.approvalExpired' });
      this.emit(ControllerEvent.STATE, this.snapshot());
    } else {
      const request = this.question;
      if (!request || request.id !== id) return;
      this.question = null;
      this._dispatch(Action.APPROVAL_RESOLVED);
      this.emit(ControllerEvent.QUESTION, null);
      this.emit(ControllerEvent.TOAST, { key: 'toast.questionExpired' });
      this.emit(ControllerEvent.STATE, this.snapshot());
    }
  }

  /** Answer the approval sheet. Default focus in the UI is "deny". */
  respondApproval(id, outcome) {
    const request = this.approval;
    if (!request || request.id !== id) return false;
    if (outcome !== 'approved' && outcome !== 'denied') return false;
    try {
      this.transport.send({ type: MsgType.APPROVAL_RESPONSE, id, outcome });
    } catch (error) {
      this.diagnostics.warn('approval', `response failed: ${error?.message ?? error}`);
      return false;
    }
    this._clearDeadline('approval');
    this.approval = null;
    this._dispatch(Action.APPROVAL_RESOLVED);
    this.emit(ControllerEvent.APPROVAL, null);
    this.emit(ControllerEvent.TOAST, { key: outcome === 'approved' ? 'approval.approved' : 'approval.denied' });
    this.diagnostics.info('approval', `answered ${id}: ${outcome}`);
    this.emit(ControllerEvent.STATE, this.snapshot());
    return true;
  }

  _onApprovalExpired(message) {
    const request = this.approval;
    this._clearDeadline('approval');
    this.approval = null;
    if (request) this._dispatch(Action.APPROVAL_RESOLVED);
    this.emit(ControllerEvent.APPROVAL, null);
    this.emit(ControllerEvent.TOAST, { key: 'toast.approvalExpired' });
    this.emit(ControllerEvent.STATE, this.snapshot());
  }

  /* ------------------------------------------------------------ question */

  _onQuestionRequest(raw) {
    let request;
    try {
      request = normalizeQuestionRequest(raw);
    } catch (error) {
      this.diagnostics.warn('question', `invalid question.request: ${error?.message ?? error}`);
      return;
    }
    request.sessionId = request.sessionId ?? this.activeSessionId;
    this.question = request;
    this._dispatch(Action.APPROVAL_SHOWN);
    this.emit(ControllerEvent.QUESTION, request);
    this.emit(ControllerEvent.STATE, this.snapshot());
    this._scheduleDeadline(request.id, request.deadline, 'question');
  }

  _onQuestionExpired() {
    this._clearDeadline('question');
    const request = this.question;
    this.question = null;
    if (request) this._dispatch(Action.APPROVAL_RESOLVED);
    this.emit(ControllerEvent.QUESTION, null);
    this.emit(ControllerEvent.TOAST, { key: 'toast.questionExpired' });
    this.emit(ControllerEvent.STATE, this.snapshot());
  }

  /** Send answers for the question sheet; `cancelled` skips the question. */
  respondQuestion(id, answers, { cancelled = false } = {}) {
    const request = this.question;
    if (!request || request.id !== id) return false;
    try {
      this.transport.send({
        type: MsgType.QUESTION_RESPONSE,
        id,
        cancelled: Boolean(cancelled),
        answers: cancelled ? [] : answers,
      });
    } catch (error) {
      this.diagnostics.warn('question', `response failed: ${error?.message ?? error}`);
      return false;
    }
    this._clearDeadline('question');
    this.question = null;
    this._dispatch(Action.APPROVAL_RESOLVED);
    this.emit(ControllerEvent.QUESTION, null);
    this.diagnostics.info('question', `answered ${id} cancelled=${Boolean(cancelled)}`);
    this.emit(ControllerEvent.STATE, this.snapshot());
    return true;
  }

  /* --------------------------------------------------------------- audio */

  _onAudioState(state, reason) {
    if (!state) return;
    this.recording.state = state;
    if (state === 'preheating') {
      this.recording.startedAt = this.scheduler.now();
    }
    if (state === 'stopped' || state === 'idle') {
      this.recording.level = 0;
      if (this.state.phase === Phase.RECORDING) this._dispatch(Action.RECORDING_STOPPED);
    }
    this.emit(ControllerEvent.AUDIO_STATE, { state, reason });
    this.emit(ControllerEvent.STATE, this.snapshot());
  }

  /** Begin PTT recording (press-and-hold). */
  startRecording() {
    if (!canRecord(this.state)) {
      this.emit(ControllerEvent.TOAST, { key: 'recording.notReady' });
      return false;
    }
    this._partialText = '';
    this._firstPartialSeen = false;
    this._recordingStartedAt = this.scheduler.now();
    this.recording = { ...this.recording, state: 'starting', partial: '', level: 0, startedAt: this._recordingStartedAt };
    this._dispatch(Action.RECORDING_STARTED);
    try {
      this.transport.startAudio({
        sampleRate: 16000,
        lang: this.settings.lang === 'en' ? 'en' : 'zh',
        mode: this.settings.audioMode ?? 'ptt',
        sessionId: this.activeSessionId,
      });
    } catch (error) {
      this._dispatch(Action.RECORDING_STOPPED);
      this.emit(ControllerEvent.ERROR, { code: 5001, scope: 'audio', retryable: true, message: String(error?.message ?? error) });
      return false;
    }
    this.emit(ControllerEvent.STATE, this.snapshot());
    return true;
  }

  /** Release PTT: finalize and transcribe. */
  stopRecording() {
    try {
      this.transport.stopAudio();
    } catch (error) {
      this.diagnostics.warn('audio', `stopAudio failed: ${error?.message ?? error}`);
    }
  }

  /** Swipe-up cancel: discard the recording without transcribing. */
  cancelRecording() {
    try {
      this.transport.cancelAudio();
    } catch (error) {
      this.diagnostics.warn('audio', `cancelAudio failed: ${error?.message ?? error}`);
    }
    this._partialText = '';
    this.recording.partial = '';
    this.emit(ControllerEvent.PARTIAL, { text: null });
    if (this.state.phase === Phase.RECORDING) this._dispatch(Action.RECORDING_STOPPED);
    this.emit(ControllerEvent.STATE, this.snapshot());
  }

  _onAsrPartial(message) {
    const text = String(message.text ?? '');
    this._partialText = text;
    this.recording.partial = text;
    if (!this._firstPartialSeen) {
      this._firstPartialSeen = true;
      if (this._recordingStartedAt !== null) {
        this.diagnostics.latency(LATENCY.ASR_FIRST_PARTIAL, this.scheduler.now() - this._recordingStartedAt);
        this.emit(ControllerEvent.LATENCY, this.diagnostics.latencyStats());
      }
    }
    this.emit(ControllerEvent.PARTIAL, { text, stability: message.stability ?? 0 });
  }

  _onAsrFinal(message) {
    const text = String(message.text ?? '').trim();
    if (this._recordingStartedAt !== null) {
      this.diagnostics.latency(LATENCY.ASR_FINAL, this.scheduler.now() - this._recordingStartedAt);
      this._recordingStartedAt = null;
    }
    this._partialText = '';
    this.recording.partial = '';
    if (this.state.phase === Phase.RECORDING) this._dispatch(Action.RECORDING_STOPPED);
    this.emit(ControllerEvent.PARTIAL, { text: null });
    this.emit(ControllerEvent.ASR_FINAL, { text });
    this.emit(ControllerEvent.LATENCY, this.diagnostics.latencyStats());
    this.emit(ControllerEvent.STATE, this.snapshot());
    if (this.settings.autoSend && text) {
      this.sendText(text);
    }
  }

  /* ------------------------------------------------------------- settings */

  /** Merge and persist settings; returns the normalized value. */
  async updateSettings(patch) {
    const input = patch ?? {};
    this.settings = normalizeSettings({ ...this.settings, ...input });
    await this.store.saveSettings(this.settings);
    // FR-505: preferred audio device is pushed to the native audio stack.
    if (Object.prototype.hasOwnProperty.call(input, 'preferAudioDevice')) {
      try {
        this.transport.preferDevice?.(this.settings.preferAudioDevice === 'auto' ? null : this.settings.preferAudioDevice);
      } catch (error) {
        this.diagnostics.warn('settings', `preferDevice failed: ${error?.message ?? error}`);
      }
    }
    this.emit(ControllerEvent.SETTINGS, { ...this.settings });
    return this.settings;
  }

  /** Diagnostic snapshot for the diagnostics screen. */
  getDiagnostics() {
    return {
      appVersion: this.diagnostics.appVersion,
      protocolVersion: this.diagnostics.protocolVersion,
      buildChannel: this.diagnostics.buildChannel,
      transportKind: this.transportKind,
      connection: { ...this.connection },
      serverId: this.connection.detail?.serverId ?? null,
      latency: this.diagnostics.latencyStats(),
      errors: this.diagnostics.recentErrors(),
      settings: {
        host: this.settings.host,
        port: this.settings.port,
        lang: this.settings.lang,
        paired: this.settings.paired,
      },
    };
  }

  /** Export sanitized logs (native bridge export or local diagnostics). */
  exportLogs() {
    this.diagnostics.info('logs', 'export requested');
    if (this.transportKind === TransportKind.NATIVE) {
      this.transport.exportLogs();
      return;
    }
    if (this.transportKind === TransportKind.MOCK) {
      this.transport.exportLogs();
      return;
    }
    this.emit(ControllerEvent.LOGS_EXPORTED, { text: this.diagnostics.exportText(), truncated: false });
  }

  /** Clear local diagnostics log. */
  clearLogs() {
    this.diagnostics.clear();
  }
}

/* ------------------------------------------------------------------------- */
/* Small defensive helpers (kept private to the controller)                    */
/* ------------------------------------------------------------------------- */

function normalizeInboundMessageSafe(message) {
  if (!message || typeof message !== 'object' || typeof message.type !== 'string') {
    throw new Error('message must be an object with a type');
  }
  return message;
}

function normalizeMessageListSafe(messages) {
  try {
    return normalizeMessageList(messages);
  } catch {
    return [];
  }
}

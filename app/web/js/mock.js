/**
 * MockTransport — a fully scripted backend for the demo and for `node --test`.
 *
 * It simulates the Android bridge *and* the protocol server in one object, so
 * the Web UI can be demoed with plain `python -m http.server` without a Bridge
 * or a device:
 *
 *  - emits the same native-shaped events as `NativeTransport`
 *    (net.state / pair.result / auth.result / audio.* / logs.exported),
 *  - injects protocol `seq` on S→C messages and back-fills `_reqTo` exactly
 *    like the bridge does (ARCHITECTURE §3.2.1),
 *  - streams `assistant.delta` chunks, tool status lines, approval and
 *    question requests, ASR partial/final for PTT, and a resync replay buffer.
 *
 * Determinism: every delay goes through the injected `Scheduler`, so tests use
 * a `ManualScheduler` and their own assertions never race real timers.
 */

import { BaseTransport, TransportEvent, TransportState, normalizeInboundMessage } from './transport.js';
import { MsgType, SeqCounter, unixNow } from './protocol.js';
import { realScheduler } from './scheduler.js';

/** Pairing details shown by the demo "scan" button and pairing hints. */
export const MOCK_PAIRING = Object.freeze({
  host: '127.0.0.1',
  port: 8765,
  fp: 'a3f1c9d2e4b60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9a',
  code: '123456',
  uri: 'dsphone://pair?v=1&host=127.0.0.1&port=8765&fp=a3f1c9d2e4b60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9a&code=123456',
});

/**
 * What the mock "scan" (v1.1 QR pairing) resolves with. Mirrors the native
 * `scanPairing.result` success payload from ARCHITECTURE §3.2 (D-030/D-031):
 * the QR carries a primary host plus LAN candidate addresses and the PC name.
 */
export const MOCK_SCAN_RESULT = Object.freeze({
  ok: true,
  uri: 'dsphone://pair?v=1&host=192.168.1.10&port=8765&fp=a3f1c9d2e4b60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9a&code=123456&sid=srv-mock-1a2b&hosts=192.168.1.10,10.5.19.39&name=KQ-PC',
  host: '192.168.1.10',
  port: 8765,
  fp: 'a3f1c9d2e4b60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9a',
  code: '123456',
  sid: 'srv-mock-1a2b',
  hosts: ['192.168.1.10', '10.5.19.39'],
  name: 'KQ-PC',
});

/**
 * Built-in demo images for `pickImage` so the v1.1 image flow works fully
 * offline. Tiny 64x48 PNGs (166/169 bytes) generated for the mock only.
 */
export const MOCK_SAMPLE_IMAGES = Object.freeze({
  gallery: Object.freeze({
    data: 'iVBORw0KGgoAAAANSUhEUgAAAEAAAAAwCAIAAAAuKetIAAAAbUlEQVR42u3YwQmAMAwF0E7iCk7SSVzCkQUvPdmeBRE8qA19n7/AuyQkKS9H6CYAAAAAAAAAgBcA2156KwDAb4BpXp8WAAAAACAaoJ4TDFCvEmkTAwCMDrifQu4BAAAAgPEAfqMAAAAAAAAftAGCEzT+tIrOZgAAAABJRU5ErkJggg==',
    mimeType: 'image/png',
    name: 'demo-gallery.png',
    width: 64,
    height: 48,
  }),
  camera: Object.freeze({
    data: 'iVBORw0KGgoAAAANSUhEUgAAAEAAAAAwCAIAAAAuKetIAAAAcElEQVR42u3YsQmAMBBA0YwhVoK7O4vDWAgi2GkrEQQLNUce/AHuNXckqWm70CUAAAAAAAAAgBcA87KVFgDAb4B16J8GAAAAABANsE/juWCAbPqrofRLDABQO+B+C3kPAAAAANQH8DcKAAAAAADwQQcT5yrGRwv7KgAAAABJRU5ErkJggg==',
    mimeType: 'image/png',
    name: 'demo-camera.png',
    width: 64,
    height: 48,
  }),
});

/** Per-session metadata used by the v1.1 top bar (D-028/D-032). */
export const MOCK_SESSION_META = Object.freeze({
  default: Object.freeze({
    provider: 'deepseek-official',
    model: 'deepseek-flash',
    reasoningEffort: 'high',
    contextWindow: 1000000,
  }),
  pro: Object.freeze({
    provider: 'deepseek-official',
    model: 'deepseek-v4-pro',
    reasoningEffort: 'max',
    contextWindow: 128000,
  }),
});

/** Demo keywords that select a mock scenario (also surfaced in the UI hints). */
export const MOCK_SCENARIOS = Object.freeze({
  approval: { name: 'approval', pattern: /(bash|shell|rm\s|删除|部署|deploy|执行命令|运行命令|审批|approve)/i },
  question: { name: 'question', pattern: /(选择|选项|方案|问卷|请问|choose|option|ask|哪个)/i },
  error: { name: 'error', pattern: /(报错|失败|异常|错误|fail|error|crash)/i },
  long: { name: 'long', pattern: /(长文|详细|展开|完整|long|detail)/i },
  audio: { name: 'audio', pattern: /(语音|录音|耳机|audio|voice|耳机)/i },
  model: { name: 'model', pattern: /(模型|切换模型|v4-pro|deepseek-v4-pro|换模型|model)/i },
});

/** Default simulated latencies in milliseconds. */
export const MOCK_LATENCY = Object.freeze({
  connect: 120,
  auth: 150,
  pair: 120,
  scan: 180,
  pick: 220,
  media: 120,
  preheat: 400,
  firstPartial: 260,
  partialGap: 220,
  asrFinal: 80,
  firstDelta: 220,
  deltaGap: 28,
  tool: 90,
  export: 60,
});

const DEMO_TOOL = 'shell';
const DEFAULT_SESSION_ID = 'demo-session-1';

/** Pick the mock scenario for a user message. */
export function pickScenario(text) {
  const value = String(text ?? '');
  for (const scenario of Object.values(MOCK_SCENARIOS)) {
    if (scenario.pattern.test(value)) return scenario.name;
  }
  return 'default';
}

/** Split text into small streaming chunks (deterministic, sentence-aware). */
export function chunkText(text, { maxChunk = 6 } = {}) {
  const value = String(text ?? '');
  const sentences = value.split(/(?<=[\n。！？；：!?;:])/).filter((part) => part.length > 0);
  const chunks = [];
  for (const sentence of sentences) {
    if (sentence.length <= maxChunk) {
      chunks.push(sentence);
      continue;
    }
    for (let i = 0; i < sentence.length; i += maxChunk) chunks.push(sentence.slice(i, i + maxChunk));
  }
  return chunks.length > 0 ? chunks : [value];
}

/** A tiny pty-style "typing" level for the recording overlay. */
function fakeLevel(tick) {
  const value = (Math.sin(tick / 2.1) + Math.sin(tick / 5.3) + 2) / 4;
  return Math.min(1, Math.max(0.05, Number(value.toFixed(3))));
}

/** MockTransport: one object that plays both bridge and server. */
export class MockTransport extends BaseTransport {
  /**
   * @param {object} [options]
   * @param {import('./scheduler.js').Scheduler} [options.scheduler]
   * @param {Partial<typeof MOCK_LATENCY>} [options.latency]
   * @param {() => number} [options.now] unix seconds clock (tests inject)
   */
  constructor({ scheduler = realScheduler, latency = {}, now = unixNow } = {}) {
    super({ scheduler, kind: 'mock' });
    this.latency = { ...MOCK_LATENCY, ...latency };
    this.now = now;
    this.paired = false;
    this.serverId = 'mock-server-1';
    this.serverSeq = new SeqCounter(1);
    this._timers = new Set();
    this._replay = [];
    this._busySessionId = null;
    this._cancelledMessages = new Set();
    this._pendingApproval = null;
    this._pendingQuestion = null;
    this._levelTick = 0;
    this._levelTimer = null;
    this._partialText = '';
    this._partialSeq = 0;
    this._aseq = 0;
    this._sessionCounter = 2;
    this._messageCounter = 0;
    this._sessions = new Map();
    this._sessionMeta = new Map();
    this._sessionTokens = new Map();
    this._media = new Map();
    this._mediaCounter = 0;
    this._turnUsage = null;
    /** Hooks so tests/demo can force a failure path. */
    this.scanFailure = null;
    this.pickFailure = null;
    this._seedDemoSessions();
  }

  /* ---------------------------------------------------------- demo data */

  _seedDemoSessions() {
    const now = this.now();
    const welcome = [
      '你好，我是 DSH Phone 的**演示模式**助手。',
      '',
      '这个界面不需要电脑端 Bridge 也能完整体验：',
      '',
      '- 发送普通消息 → 流式回复 + 打字机光标',
      '- 发送「删除文件」或「部署」→ 工具审批弹窗',
      '- 发送「选择方案」→ 追问弹窗',
      '- 发送「报错」→ 错误提示与「下一步」文案',
      '- 发送「长文」→ 自动滚动与回到底部按钮',
      '- 发送「切换模型」→ 顶部模型/思考/上下文随之变化',
      '- 点附件/相机 → 给这条消息附上演示图片',
      '- 点右下角麦克风 → 录音浮层、波形、上滑取消',
      '',
      '设置里可以切换语言、深浅色和自动发送；配对页支持扫码与手动输入。',
    ].join('\n');
    this._putSession(
      {
        sessionId: DEFAULT_SESSION_ID,
        title: '欢迎使用 DSH Phone',
        updatedAt: now - 30,
        lastPreview: '演示模式：试试发送「删除文件」「选择方案」「长文」',
      },
      [
        this._makeMessage({ sessionId: DEFAULT_SESSION_ID, role: 'user', text: '这个演示能做什么？', createdAt: now - 90 }),
        this._makeMessage({ sessionId: DEFAULT_SESSION_ID, role: 'assistant', text: welcome, createdAt: now - 60 }),
      ],
    );
    this._sessionMeta.set(DEFAULT_SESSION_ID, { ...MOCK_SESSION_META.default });
    this._sessionTokens.set(DEFAULT_SESSION_ID, 1200);
    // History message with an image id only (v1.1): the UI must call
    // `media.get` to resolve the bytes, like a real resumed session.
    const seedMediaId = 'md-demo-seed-1';
    this._media.set(seedMediaId, {
      mediaId: seedMediaId,
      mimeType: MOCK_SAMPLE_IMAGES.gallery.mimeType,
      data: MOCK_SAMPLE_IMAGES.gallery.data,
      width: MOCK_SAMPLE_IMAGES.gallery.width,
      height: MOCK_SAMPLE_IMAGES.gallery.height,
      sizeBytes: 166,
    });
    this._putSession(
      {
        sessionId: 'demo-session-2',
        title: '部署脚本检查',
        updatedAt: now - 3600,
        lastPreview: '已为你检查部署脚本，发现 2 个可优化的地方。',
      },
      [
        this._makeMessage({
          sessionId: 'demo-session-2',
          role: 'user',
          text: '帮我看看部署脚本',
          createdAt: now - 3700,
          images: [
            {
              mediaId: seedMediaId,
              mimeType: MOCK_SAMPLE_IMAGES.gallery.mimeType,
              width: MOCK_SAMPLE_IMAGES.gallery.width,
              height: MOCK_SAMPLE_IMAGES.gallery.height,
              sizeBytes: 166,
            },
          ],
        }),
        this._makeMessage({
          sessionId: 'demo-session-2',
          role: 'assistant',
          text: '已为你检查部署脚本，发现 2 个可优化的地方：\n\n1. `set -euo pipefail` 建议放在脚本开头\n2. 构建步骤缺少缓存目录\n\n需要我直接修改吗？',
          createdAt: now - 3600,
        }),
      ],
    );
    this._sessionMeta.set('demo-session-2', { ...MOCK_SESSION_META.pro });
    this._sessionTokens.set('demo-session-2', 900);
  }

  _putSession(session, messages) {
    this._sessions.set(session.sessionId, { session: { ...session }, messages: messages ?? [] });
  }

  _makeMessage({ sessionId, role, text, status = 'done', createdAt, images = null }) {
    this._messageCounter += 1;
    const message = {
      messageId: `mock-msg-${this._messageCounter}`,
      sessionId,
      role,
      text,
      status,
      createdAt: createdAt ?? this.now(),
    };
    if (Array.isArray(images) && images.length > 0) message.images = images;
    return message;
  }

  /** Server-side session list, already sorted like the real server. */
  listSessions() {
    return [...this._sessions.values()]
      .map((entry) => ({ ...entry.session }))
      .sort((a, b) => b.updatedAt - a.updatedAt);
  }

  _sessionIds() {
    return [...this._sessions.keys()];
  }

  _getSession(sessionId) {
    return this._sessions.get(sessionId) ?? null;
  }

  _touchSession(sessionId, lastPreview) {
    const entry = this._sessions.get(sessionId);
    if (!entry) return;
    entry.session.updatedAt = this.now();
    if (lastPreview !== undefined) entry.session.lastPreview = String(lastPreview).slice(0, 80);
  }

  /* ------------------------------------------------- v1.1 meta / media */

  _metaFor(sessionId) {
    const meta = this._sessionMeta.get(sessionId);
    return meta ? { ...meta } : { ...MOCK_SESSION_META.default };
  }

  /**
   * Push a `session.meta` (v1.1 top bar) like the real server does right after
   * `session.create.ok` / `session.resume.ok` and when the model selection or
   * the context window changes (PROTOCOL §9, D-032).
   */
  _pushSessionMeta(sessionId, patch = null) {
    if (patch) this._sessionMeta.set(sessionId, { ...this._metaFor(sessionId), ...patch });
    const meta = this._metaFor(sessionId);
    const used = this._sessionTokens.get(sessionId) ?? 0;
    if (used > meta.contextWindow - 2000) this._sessionTokens.set(sessionId, Math.max(0, meta.contextWindow - 2000));
    this._push({ type: MsgType.SESSION_META, sessionId, ...meta });
  }

  /** Cheap token estimate for the demo (Chinese ≈ 0.7 token/char). */
  _estimateTokens(text) {
    const value = String(text ?? '');
    if (value.length === 0) return 0;
    return Math.max(1, Math.round([...value].length * 0.7));
  }

  /** Turn optimistic `chat.send.images` into server-side media entries. */
  _registerImages(images) {
    if (!Array.isArray(images)) return [];
    const registered = [];
    for (const image of images) {
      const data = typeof image?.data === 'string' ? image.data : '';
      const mimeType = typeof image?.mimeType === 'string' && image.mimeType.startsWith('image/') ? image.mimeType : 'image/jpeg';
      if (!data) continue;
      this._mediaCounter += 1;
      const mediaId = `md-demo-${this._mediaCounter}`;
      const media = {
        mediaId,
        mimeType,
        data,
        width: Number.isInteger(image.width) && image.width > 0 ? image.width : 0,
        height: Number.isInteger(image.height) && image.height > 0 ? image.height : 0,
        sizeBytes: Math.round((data.length * 3) / 4),
      };
      this._media.set(mediaId, media);
      registered.push({
        mediaId,
        mimeType,
        width: media.width,
        height: media.height,
        sizeBytes: media.sizeBytes,
      });
    }
    return registered;
  }

  _makeUsage(assistantMessage) {
    if (!this._turnUsage) return null;
    const sessionId = assistantMessage.sessionId;
    const meta = this._metaFor(sessionId);
    const outputTokens = Math.max(1, this._estimateTokens(assistantMessage.text));
    const reasoningTokens =
      meta.reasoningEffort === 'low'
        ? 0
        : Math.round(outputTokens * (meta.reasoningEffort === 'max' ? 0.8 : 0.45));
    const inputTokens = this._turnUsage.inputTokens;
    const totalTokens = inputTokens + outputTokens;
    const base = this._sessionTokens.get(sessionId) ?? 0;
    const usedTokens = Math.min(meta.contextWindow, base + outputTokens + reasoningTokens);
    this._sessionTokens.set(sessionId, usedTokens);
    return {
      inputTokens,
      outputTokens,
      totalTokens,
      cacheReadTokens: Math.floor(inputTokens * 0.6),
      cacheWriteTokens: Math.floor(inputTokens * 0.1),
      reasoningTokens,
      contextWindow: meta.contextWindow,
      usedTokens,
    };
  }

  /* ------------------------------------------------------- timer helpers */

  _setTimeout(fn, delayMs) {
    const id = this.scheduler.setTimeout(() => {
      this._timers.delete(id);
      try {
        fn();
      } catch (error) {
        this.emitError({ scope: 'mock', code: 5001, message: String(error?.message ?? error) });
      }
    }, Math.max(0, Math.round(delayMs)));
    this._timers.add(id);
    return id;
  }

  _clearTimers() {
    for (const id of this._timers) this.scheduler.clearTimeout(id);
    this._timers.clear();
    this._stopLevelTicker();
  }

  /* ------------------------------------------------------------ commands */

  _connect(config = {}) {
    this._config = config;
    this._clearTimers();
    this.emitState(TransportState.CONNECTING);
    this._setTimeout(() => {
      if (this.paired) this._beginAuth();
      else this.emitState(TransportState.PAIRING, { serverId: this.serverId });
    }, this.latency.connect);
  }

  _disconnect() {
    this._clearTimers();
    this.emitState(TransportState.CLOSED, { intentional: true });
  }

  _reconnect() {
    this._clearTimers();
    if (this.state === TransportState.IDLE) {
      this._connect(this._config ?? {});
      return;
    }
    this.emitState(TransportState.RECONNECTING);
    this._setTimeout(() => {
      if (this.paired) this._beginAuth();
      else this.emitState(TransportState.PAIRING);
    }, this.latency.connect);
  }

  _beginAuth() {
    this.emitState(TransportState.AUTHENTICATING);
    this._setTimeout(() => {
      this.emit(TransportEvent.AUTH_RESULT, { ok: true, serverId: this.serverId, sessions: this.listSessions() });
      this.emitState(TransportState.ONLINE, { serverId: this.serverId });
    }, this.latency.auth);
  }

  _requestPair({ code } = {}) {
    this._setTimeout(() => {
      if (String(code) === MOCK_PAIRING.code) {
        this.paired = true;
        this.emit(TransportEvent.PAIR_RESULT, { ok: true, serverId: this.serverId });
        // Native auth's automatically after pair.ok (android-shell contract);
        // the mock mirrors that so the UI has one code path.
        this._beginAuth();
      } else {
        this.emit(TransportEvent.PAIR_RESULT, {
          ok: false,
          code: 1002,
          message: '配对码错误或已过期',
          attemptsLeft: 4,
          retryable: true,
        });
      }
    }, this.latency.pair);
  }

  _requestAuth() {
    if (this.state === TransportState.ONLINE) {
      // Native answers a redundant auth op without re-authenticating.
      this.emit(TransportEvent.AUTH_RESULT, { ok: true, already: true, serverId: this.serverId });
      return;
    }
    if (!this.paired) {
      this.emit(TransportEvent.AUTH_RESULT, { ok: false, needPair: true, code: 1001, message: '设备尚未配对' });
      this.emitState(TransportState.PAIRING);
      return;
    }
    this._beginAuth();
  }

  _send(message) {
    const { _reqId, seq: _ignored, ...payload } = message;
    this._handleProtocolMessage(payload, typeof _reqId === 'string' ? _reqId : undefined);
  }

  _getToken() {
    this.emit(TransportEvent.EVENT, {
      type: 'token',
      hasToken: this.paired,
      host: this._config?.host,
      port: this._config?.port,
      serverId: this.serverId,
    });
  }

  _routeInfo() {
    this.emit(TransportEvent.EVENT, { type: 'audio.route', route: 'phone', deviceName: '本机听筒' });
  }

  _preferDevice(address) {
    this.emit(TransportEvent.EVENT, {
      type: 'audio.route',
      route: address ? 'hfp' : 'phone',
      deviceName: address ? 'WH-1000XM5（演示耳机）' : '本机听筒',
      preferredUnavailable: false,
    });
  }

  _exportLogs() {
    this._setTimeout(() => {
      this.emit(TransportEvent.EVENT, {
        type: 'logs.exported',
        text: [
          'DSH Phone web demo log',
          `transport=mock server=${this.serverId} paired=${this.paired}`,
          `sessions=${this._sessions.size} messages=${this._messageCounter}`,
          `serverSeq=${this.serverSeq.value} state=${this.state}`,
        ].join('\n'),
        truncated: false,
      });
    }, this.latency.export);
  }

  /**
   * v1.1 QR pairing: resolves like the native `scanPairing.result` after the
   * camera would have decoded the QR (D-030). Set `scanFailure` to a native
   * reason (`permission_denied`, `cancelled`, `no_camera`, `decode_failed`) to
   * exercise the failure copy.
   */
  _scanPairing() {
    return new Promise((resolve) => {
      this._setTimeout(() => {
        if (this.scanFailure) resolve({ ok: false, supported: true, reason: this.scanFailure });
        else resolve({ ...MOCK_SCAN_RESULT });
      }, this.latency.scan);
    });
  }

  /** v1.1 image picker: resolves a tiny built-in demo image. */
  _pickImage(source) {
    const src = source === 'camera' ? 'camera' : 'gallery';
    return new Promise((resolve) => {
      this._setTimeout(() => {
        if (this.pickFailure) resolve({ ok: false, source: src, reason: this.pickFailure });
        else resolve({ ok: true, source: src, ...MOCK_SAMPLE_IMAGES[src] });
      }, this.latency.pick);
    });
  }

  /** v1.1 history media: serve the seeded/uploaded demo images. */
  _mediaGet(mediaId) {
    return new Promise((resolve) => {
      this._setTimeout(() => {
        const media = this._media.get(mediaId);
        if (!media) resolve({ ok: false, code: 3001, reason: 'not_found', mediaId });
        else resolve({ ok: true, media: { ...media } });
      }, this.latency.media);
    });
  }

  _handleMediaGet(message, reqId) {
    const media = this._media.get(message.mediaId);
    if (!media) {
      this._reply(reqId, { type: MsgType.ERROR, code: 3001, message: '媒体不存在', retryable: false });
      return;
    }
    this._reply(reqId, {
      type: MsgType.MEDIA_DATA,
      mediaId: media.mediaId,
      mimeType: media.mimeType,
      data: media.data,
    });
  }

  /* ----------------------------------------------------- protocol server */

  _nextSeq() {
    return this.serverSeq.next();
  }

  /** Emit an S→C message, recording replays for `resync`. */
  _push(fields) {
    const message = normalizeInboundMessage({ ...fields, seq: this._nextSeq() });
    if (this._replay.length >= 200) this._replay.shift();
    this._replay.push(message);
    this.emit(TransportEvent.MESSAGE, message);
    return message;
  }

  /** Reply to a request: correlate with the bridge-provided `_reqId`. */
  _reply(reqId, fields) {
    const message = { ...fields, seq: this._nextSeq() };
    if (reqId) message._reqTo = reqId;
    this.emit(TransportEvent.MESSAGE, message);
    return message;
  }

  _handleProtocolMessage(message, reqId) {
    switch (message.type) {
      case MsgType.RESYNC: {
        const lastSeq = Number(message.lastSeq) || 0;
        const missed = this._replay.filter((entry) => entry.seq > lastSeq);
        this._reply(reqId, { type: MsgType.RESYNC_OK, fromSeq: lastSeq + 1, uptoS: this.serverSeq.value - 1 });
        for (const entry of missed) this.emit(TransportEvent.MESSAGE, entry);
        break;
      }
      case MsgType.SESSION_LIST:
        this._reply(reqId, { type: MsgType.SESSION_LIST_OK, sessions: this.listSessions() });
        break;
      case MsgType.SESSION_CREATE: {
        this._sessionCounter += 1;
        const sessionId = `demo-session-${this._sessionCounter}`;
        const session = {
          sessionId,
          title: message.title || '新会话',
          updatedAt: this.now(),
          lastPreview: '',
        };
        this._putSession(session, []);
        this._sessionMeta.set(sessionId, { ...MOCK_SESSION_META.default });
        this._sessionTokens.set(sessionId, 0);
        this._reply(reqId, { type: MsgType.SESSION_CREATE_OK, session });
        this._pushSessionMeta(sessionId);
        break;
      }
      case MsgType.SESSION_RESUME: {
        const entry = this._getSession(message.sessionId);
        if (!entry) {
          this._reply(reqId, { type: MsgType.SESSION_RESUME_OK, session: null, messages: [] });
          break;
        }
        this._reply(reqId, {
          type: MsgType.SESSION_RESUME_OK,
          session: { ...entry.session },
          messages: entry.messages.map((msg) => ({ ...msg })),
        });
        this._pushSessionMeta(entry.session.sessionId);
        break;
      }
      case MsgType.SESSION_HISTORY: {
        const entry = this._getSession(message.sessionId);
        this._reply(reqId, {
          type: MsgType.SESSION_HISTORY_OK,
          messages: (entry?.messages ?? []).map((msg) => ({ ...msg })),
          hasMore: false,
        });
        break;
      }
      case MsgType.CHAT_SEND:
        this._handleChatSend(message);
        break;
      case MsgType.CHAT_CANCEL:
        this._cancelTurn(message.sessionId);
        break;
      case MsgType.APPROVAL_RESPONSE:
        this._resolveApproval(message);
        break;
      case MsgType.QUESTION_RESPONSE:
        this._resolveQuestion(message);
        break;
      case MsgType.AUDIO_START:
        this._startAudio({ lang: message.lang, mode: message.mode, sessionId: message.sessionId });
        break;
      case MsgType.AUDIO_STOP:
        this._stopAudio();
        break;
      case MsgType.MEDIA_GET:
        this._handleMediaGet(message, reqId);
        break;
      default:
        if (message.type !== MsgType.PING) {
          this._reply(reqId, {
            type: MsgType.ERROR,
            code: 5002,
            message: `mock: unsupported message type ${message.type}`,
            retryable: false,
          });
        }
        break;
    }
  }

  /* ---------------------------------------------------------------- chat */

  _ensureSession(sessionId) {
    if (sessionId && this._getSession(sessionId)) return sessionId;
    if (this._sessions.size > 0) return DEFAULT_SESSION_ID;
    return null;
  }

  _handleChatSend(message) {
    const sessionId = this._ensureSession(message.sessionId);
    if (!sessionId) {
      this._reply(undefined, { type: MsgType.ERROR, code: 3001, message: '会话不存在', retryable: false });
      return;
    }
    if (this._busySessionId) {
      this._reply(undefined, { type: MsgType.ERROR, code: 4001, message: '上一轮对话还在进行中', retryable: true });
      return;
    }
    const text = String(message.text ?? '');
    const images = Array.isArray(message.images) ? message.images : [];
    if (images.length > 4) {
      this._reply(undefined, { type: MsgType.ERROR, code: 4002, message: '最多只能发送 4 张图片', retryable: false });
      return;
    }
    if (text.length === 0 && images.length === 0) {
      this._reply(undefined, { type: MsgType.ERROR, code: 4002, message: '消息内容不能为空', retryable: false });
      return;
    }
    const registered = this._registerImages(images);
    if (images.length > 0 && registered.length === 0) {
      this._reply(undefined, { type: MsgType.ERROR, code: 4002, message: '图片编码无效，请重新选择图片', retryable: false });
      return;
    }
    const clientId = message.clientId ?? `mock-client-${this._messageCounter + 1}`;
    const entry = this._getSession(sessionId);
    const preview = text.trim() || `[图片] ${registered.length} 张`;

    const userMessage = this._makeMessage({
      sessionId,
      role: 'user',
      text,
      images: registered.length > 0 ? registered : null,
    });
    entry.messages.push(userMessage);
    this._touchSession(sessionId, preview.slice(0, 80));

    const assistantMessage = this._makeMessage({ sessionId, role: 'assistant', text: '', status: 'streaming' });
    entry.messages.push(assistantMessage);
    this._busySessionId = sessionId;

    // v1.1 token accounting → assistant.done.usage (PROTOCOL §9, D-032).
    const inputTokens = this._estimateTokens(text) + registered.length * 900;
    const usedBefore = this._sessionTokens.get(sessionId) ?? 0;
    this._sessionTokens.set(sessionId, usedBefore + inputTokens);
    this._turnUsage = { inputTokens };

    this._push({ type: MsgType.CHAT_ACK, clientId, messageId: assistantMessage.messageId });
    const picked = pickScenario(text);
    const scenario = registered.length > 0 && picked === 'default' ? 'image' : picked;
    this._setTimeout(
      () => this._runScenario(scenario, { sessionId, text, assistantMessage, images: registered }),
      this.latency.tool,
    );
  }

  _streamInto(assistantMessage, chunks) {
    const sessionId = assistantMessage.sessionId;
    let index = 0;
    const step = () => {
      if (this._cancelledMessages.has(assistantMessage.messageId)) {
        this._finishTurn(assistantMessage, 'cancelled');
        return;
      }
      if (index >= chunks.length) {
        this._finishTurn(assistantMessage, 'completed');
        return;
      }
      const chunk = chunks[index];
      this._push({
        type: MsgType.ASSISTANT_DELTA,
        sessionId,
        messageId: assistantMessage.messageId,
        text: chunk,
        index,
      });
      assistantMessage.text += chunk;
      index += 1;
      this._touchSession(sessionId, assistantMessage.text.slice(-80));
      this._setTimeout(step, this.latency.deltaGap);
    };
    step();
  }

  _finishTurn(assistantMessage, finishReason) {
    if (assistantMessage.status === 'done') return;
    assistantMessage.status = 'done';
    this._busySessionId = null;
    this._touchSession(assistantMessage.sessionId, assistantMessage.text.slice(-80));
    const usage = this._makeUsage(assistantMessage);
    this._turnUsage = null;
    this._push({
      type: MsgType.ASSISTANT_DONE,
      sessionId: assistantMessage.sessionId,
      messageId: assistantMessage.messageId,
      finishReason,
      ...(usage ? { usage } : {}),
    });
  }

  _cancelTurn(sessionId) {
    if (this._busySessionId !== sessionId) return;
    const entry = this._getSession(sessionId);
    const streaming = entry?.messages.find((msg) => msg.status === 'streaming');
    if (!streaming) return;
    this._cancelledMessages.add(streaming.messageId);
    this._finishTurn(streaming, 'cancelled');
  }

  _runScenario(scenario, { sessionId, text, assistantMessage, images = [] }) {
    const entry = this._getSession(sessionId);
    switch (scenario) {
      case 'model': {
        const current = this._metaFor(sessionId);
        const next = current.model === 'deepseek-v4-pro' ? { ...MOCK_SESSION_META.default } : { ...MOCK_SESSION_META.pro };
        this._pushSessionMeta(sessionId, next);
        this._streamInto(
          assistantMessage,
          chunkText(
            `已切换到 ${next.model}，思考强度 ${next.reasoningEffort}，上下文窗口 ${Math.round(next.contextWindow / 1000)}K。顶部栏会同步显示新的模型、思考强度与上下文占用。`,
          ),
        );
        return;
      }
      case 'image':
        this._streamInto(
          assistantMessage,
          chunkText(
            `我收到了你发的 ${images.length} 张图片，已经可以一起分析。演示模式下这里只会显示缩略图；真实连接时图片会交给 DeepSeek-V4.1-Flash 的视觉能力处理。`,
          ),
        );
        return;
      case 'approval':
        this._pushToolStatus({ sessionId, state: 'running', title: '执行命令' });
        this._pendingApproval = assistantMessage;
        this._push({
          type: MsgType.APPROVAL_REQUEST,
          id: `mock-approval-${this.serverSeq.value}`,
          sessionId,
          tool: DEMO_TOOL,
          reason: '这条命令会删除构建目录，需要你的确认。',
          argsPreview: 'rm -rf ./dist && npm run build',
          deadline: this.now() + 600,
          defaultOutcome: 'denied',
        });
        return;
      case 'question': {
        assistantMessage; // answer is delivered in _resolveQuestion
        this._pendingQuestion = assistantMessage;
        this._push({
          type: MsgType.QUESTION_REQUEST,
          id: `mock-question-${this.serverSeq.value}`,
          sessionId,
          questions: [
            {
              id: 'q1',
              question: '你希望我采用哪种方案？',
              multiSelect: false,
              required: true,
              allowCustom: true,
              options: [
                { id: 'a', label: '方案 A：直接改脚本' },
                { id: 'b', label: '方案 B：先出补丁再确认' },
              ],
            },
            {
              id: 'q2',
              question: '需要同时更新哪些内容？（可多选）',
              multiSelect: true,
              required: false,
              allowCustom: false,
              options: [
                { id: 'docs', label: '文档' },
                { id: 'tests', label: '测试' },
                { id: 'ci', label: 'CI 配置' },
              ],
            },
          ],
          deadline: this.now() + 600,
        });
        return;
      }
      case 'error': {
        // Stream a short prefix, then fail the turn (no completed assistant.done
        // before turn.error — the UI must keep the assistant bubble in error).
        const chunks = chunkText('正在尝试调用模型…');
        let chunkIndex = 0;
        const step = () => {
          if (chunkIndex < chunks.length) {
            const chunk = chunks[chunkIndex];
            this._push({
              type: MsgType.ASSISTANT_DELTA,
              sessionId,
              messageId: assistantMessage.messageId,
              text: chunk,
              index: chunkIndex,
            });
            assistantMessage.text += chunk;
            chunkIndex += 1;
            this._setTimeout(step, this.latency.deltaGap);
            return;
          }
          this._busySessionId = null;
          assistantMessage.status = 'error';
          this._turnUsage = null;
          this._push({
            type: MsgType.TURN_ERROR,
            sessionId,
            code: 3003,
            message: '模型调用失败：这是演示模式生成的错误，用于验证重试与诊断流程。',
          });
          this._push({
            type: MsgType.ASSISTANT_DONE,
            sessionId,
            messageId: assistantMessage.messageId,
            finishReason: 'error',
          });
          if (entry) this._touchSession(sessionId, '回复失败');
        };
        this._setTimeout(step, this.latency.firstDelta);
        return;
      }
      case 'long': {
        const longText = this._longAnswer(text);
        this._streamInto(assistantMessage, chunkText(longText));
        return;
      }
      case 'audio':
        this._streamInto(
          assistantMessage,
          chunkText('语音演示：按住麦克风按钮说话，松开后会把识别结果发给我。上滑可以取消这次录音。'),
        );
        return;
      default: {
        const reply = this._defaultAnswer(text);
        this._streamInto(assistantMessage, chunkText(reply));
      }
    }
  }

  _longAnswer(text) {
    const lines = [`关于「${String(text).slice(0, 40)}」，这里是一段较长的示例回复，用于验证流式滚动与回到底部按钮。`, ''];
    for (let i = 1; i <= 8; i += 1) {
      lines.push(`### 第 ${i} 点`);
      lines.push(`这是第 ${i} 段说明：DSH Phone 会在助手输出时自动跟随光标；当你向上滚动查看历史时，自动跟随会暂停，并在右下角显示「回到底部」。`);
      lines.push('');
    }
    lines.push('以上是演示长文本，结束。');
    return lines.join('\n');
  }

  _defaultAnswer(text) {
    const quoted = String(text).trim().slice(0, 60);
    return [
      `收到你的消息：「${quoted}」。`,
      '',
      '这是演示模式（Mock 后端）的流式回复，真实连接时内容来自电脑端的 DSH。',
      '',
      '可以试试这些演示指令：',
      '- 「删除文件」→ 工具审批弹窗',
      '- 「选择方案」→ 追问弹窗',
      '- 「报错」→ 错误提示',
      '- 「长文」→ 长回复与滚动',
      '- 「语音」→ 录音与 ASR 演示',
      '',
      '也可以打开设置切换语言与主题。',
    ].join('\n');
  }

  _pushToolStatus({ sessionId, state, title, name = DEMO_TOOL }) {
    this._push({
      type: MsgType.TOOL_STATUS,
      sessionId,
      toolCallId: `mock-tool-${this.serverSeq.value}`,
      name,
      state,
      title,
    });
  }

  /* ------------------------------------------------------------ approval */

  _resolveApproval(message) {
    const assistantMessage = this._pendingApproval;
    if (!assistantMessage) return;
    this._pendingApproval = null;
    const sessionId = assistantMessage.sessionId;
    if (message.outcome === 'approved') {
      this._pushToolStatus({ sessionId, state: 'done', title: '执行命令' });
      this._streamInto(
        assistantMessage,
        chunkText('已获批准，命令执行完成。演示模式下不会真的修改你的电脑。'),
      );
    } else {
      this._streamInto(assistantMessage, chunkText('已拒绝执行该命令。我不会对电脑做任何改动，你可以换一种方式再试。'));
    }
  }

  /** Test/demo helper: expire the pending approval like the 10-minute deadline. */
  expirePendingApproval() {
    if (!this._pendingApproval) return false;
    const assistantMessage = this._pendingApproval;
    this._pendingApproval = null;
    this._push({ type: MsgType.APPROVAL_EXPIRED, id: 'mock-approval-expired', outcome: 'denied' });
    this._streamInto(assistantMessage, chunkText('审批已超时，按默认策略拒绝执行。'));
    return true;
  }

  /* ------------------------------------------------------------ question */

  _resolveQuestion(message) {
    const assistantMessage = this._pendingQuestion;
    if (!assistantMessage) return;
    this._pendingQuestion = null;
    const answers = Array.isArray(message.answers) ? message.answers : [];
    if (message.cancelled) {
      this._streamInto(assistantMessage, chunkText('已取消这次提问。你可以随时告诉我新的想法。'));
      return;
    }
    const lines = ['收到你的选择：', ''];
    for (const answer of answers) {
      const parts = [];
      if (Array.isArray(answer.selected) && answer.selected.length > 0) parts.push(answer.selected.join('、'));
      if (answer.custom) parts.push(`自定义：${answer.custom}`);
      lines.push(`- ${answer.questionId}：${parts.length > 0 ? parts.join('；') : '未选择'}`);
    }
    lines.push('', '我会按这个方案继续。');
    this._streamInto(assistantMessage, chunkText(lines.join('\n')));
  }

  /* --------------------------------------------------------------- audio */

  _startAudio({ lang = 'zh', mode = 'ptt', sessionId } = {}) {
    if (this.state !== TransportState.ONLINE) {
      this.emit(TransportEvent.EVENT, { type: 'audio.error', code: 'offline', message: '连接已断开，无法开始录音', retryable: true });
      this.emitError({ scope: 'audio', code: 5001, message: 'offline', retryable: true });
      return;
    }
    this._stopLevelTicker();
    this._partialText = '';
    this._partialSeq = 0;
    this._aseq = 0;
    this._audioLang = lang;
    this._audioMode = mode;
    this._audioSessionId = sessionId;
    this.emit(TransportEvent.EVENT, { type: 'audio.state', state: 'preheating' });
    this._setTimeout(() => {
      this.emit(TransportEvent.EVENT, { type: 'audio.ready', asrMode: mode, lang, codecs: ['pcm16'] });
      this.emit(TransportEvent.EVENT, { type: 'audio.route', route: 'hfp', deviceName: 'WH-1000XM5（演示耳机）' });
      this.emit(TransportEvent.EVENT, { type: 'audio.state', state: 'streaming' });
      this._startLevelTicker();
      this._startPartials();
    }, this.latency.preheat);
  }

  _startLevelTicker() {
    const tick = () => {
      this._levelTick += 1;
      this.emit(TransportEvent.EVENT, { type: 'audio.level', level: fakeLevel(this._levelTick) });
      this._levelTimer = this._setTimeout(tick, 100);
    };
    tick();
  }

  _stopLevelTicker() {
    if (this._levelTimer !== null) {
      this.scheduler.clearTimeout(this._levelTimer);
      this._levelTimer = null;
    }
  }

  _startPartials() {
    const phrases = ['帮我', '帮我总结一下', '帮我总结一下今天的', '帮我总结一下今天的会议'];
    let index = 0;
    const emitNext = () => {
      if (this._partialSeq >= 5) return;
      this._partialText = phrases[Math.min(index, phrases.length - 1)];
      index += 1;
      this._partialSeq += 1;
      this._aseq += 1;
      this.emit(TransportEvent.MESSAGE, {
        type: MsgType.ASR_PARTIAL,
        seq: 0,
        text: this._partialText,
        stability: index >= phrases.length ? 1 : 0.5,
        aseq: this._aseq,
      });
      if (index < phrases.length) this._setTimeout(emitNext, this.latency.partialGap);
    };
    this._setTimeout(emitNext, this.latency.firstPartial);
  }

  _stopAudio() {
    this._stopLevelTicker();
    const text = this._partialText || '帮我总结一下今天的会议';
    this.emit(TransportEvent.EVENT, { type: 'audio.state', state: 'stopping' });
    this._setTimeout(() => {
      this.emit(TransportEvent.EVENT, { type: 'audio.stopped', aseq: this._aseq });
      this.emit(TransportEvent.EVENT, { type: 'audio.state', state: 'stopped' });
      this.emit(TransportEvent.MESSAGE, {
        type: MsgType.ASR_FINAL,
        seq: 0,
        text,
        aseq: this._aseq,
        durationMs: 2100,
        lang: this._audioLang ?? 'zh',
      });
    }, this.latency.asrFinal);
  }

  _cancelAudio() {
    this._stopLevelTicker();
    this.emit(TransportEvent.EVENT, { type: 'audio.state', state: 'stopped' });
  }

  /* ------------------------------------------------------------ utilities */

  /** Simulate an unexpected socket loss (heartbeat code by default). */
  simulateClose({ code = 4408 } = {}) {
    this._clearTimers();
    this.emitState(TransportState.CLOSED, { code });
  }

  /** Number of pending timers (tests assert no leaks). */
  pendingTimers() {
    return this._timers.size;
  }
}

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

/** Demo keywords that select a mock scenario (also surfaced in the UI hints). */
export const MOCK_SCENARIOS = Object.freeze({
  approval: { name: 'approval', pattern: /(bash|shell|rm\s|删除|部署|deploy|执行命令|运行命令|审批|approve)/i },
  question: { name: 'question', pattern: /(选择|选项|方案|问卷|请问|choose|option|ask|哪个)/i },
  error: { name: 'error', pattern: /(报错|失败|异常|错误|fail|error|crash)/i },
  long: { name: 'long', pattern: /(长文|详细|展开|完整|long|detail)/i },
  audio: { name: 'audio', pattern: /(语音|录音|耳机|audio|voice|耳机)/i },
});

/** Default simulated latencies in milliseconds. */
export const MOCK_LATENCY = Object.freeze({
  connect: 120,
  auth: 150,
  pair: 120,
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
      '- 点右下角麦克风 → 录音浮层、波形、上滑取消',
      '',
      '设置里可以切换语言、深浅色和自动发送。',
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
    this._putSession(
      {
        sessionId: 'demo-session-2',
        title: '部署脚本检查',
        updatedAt: now - 3600,
        lastPreview: '已为你检查部署脚本，发现 2 个可优化的地方。',
      },
      [
        this._makeMessage({ sessionId: 'demo-session-2', role: 'user', text: '帮我看看部署脚本', createdAt: now - 3700 }),
        this._makeMessage({
          sessionId: 'demo-session-2',
          role: 'assistant',
          text: '已为你检查部署脚本，发现 2 个可优化的地方：\n\n1. `set -euo pipefail` 建议放在脚本开头\n2. 构建步骤缺少缓存目录\n\n需要我直接修改吗？',
          createdAt: now - 3600,
        }),
      ],
    );
  }

  _putSession(session, messages) {
    this._sessions.set(session.sessionId, { session: { ...session }, messages: messages ?? [] });
  }

  _makeMessage({ sessionId, role, text, status = 'done', createdAt }) {
    this._messageCounter += 1;
    return {
      messageId: `mock-msg-${this._messageCounter}`,
      sessionId,
      role,
      text,
      status,
      createdAt: createdAt ?? this.now(),
    };
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

  async _scanPairing() {
    return { supported: true, ...MOCK_PAIRING };
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
        this._reply(reqId, { type: MsgType.SESSION_CREATE_OK, session });
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
    const clientId = message.clientId ?? `mock-client-${this._messageCounter + 1}`;
    const entry = this._getSession(sessionId);

    const userMessage = this._makeMessage({ sessionId, role: 'user', text });
    entry.messages.push(userMessage);
    this._touchSession(sessionId, text.slice(0, 80));

    const assistantMessage = this._makeMessage({ sessionId, role: 'assistant', text: '', status: 'streaming' });
    entry.messages.push(assistantMessage);
    this._busySessionId = sessionId;

    this._push({ type: MsgType.CHAT_ACK, clientId, messageId: assistantMessage.messageId });
    const scenario = pickScenario(text);
    this._setTimeout(() => this._runScenario(scenario, { sessionId, text, assistantMessage }), this.latency.tool);
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
    this._push({
      type: MsgType.ASSISTANT_DONE,
      sessionId: assistantMessage.sessionId,
      messageId: assistantMessage.messageId,
      finishReason,
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

  _runScenario(scenario, { sessionId, text, assistantMessage }) {
    const entry = this._getSession(sessionId);
    switch (scenario) {
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

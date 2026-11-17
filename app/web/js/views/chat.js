/**
 * Chat screen (PRD FR-301..306, §12).
 *
 * Rendering is incremental: `message-added` appends one node, `message-delta`
 * updates the streaming text node directly, which keeps scrolling smooth even
 * with a high delta rate. Auto-scroll follows the stream until the user scrolls
 * up; then a "back to bottom" button appears and following stops.
 */

import { h, clear, setHidden, setClass, formatClock } from '../ui/dom.js';
import { t, errorCopy } from '../i18n.js';

const FOLLOW_THRESHOLD_PX = 96;
const MAX_INPUT_ROWS = 5;

/**
 * Split assistant text into plain-text and fenced-code segments.
 * Pure helper (no DOM) so the code-block rendering rules are unit-testable.
 * @param {string} text
 * @returns {Array<{type:'text',text:string}|{type:'code',language:string,code:string}>}
 */
export function parseMessageSegments(text) {
  const source = typeof text === 'string' ? text : '';
  const pattern = /```([^\n`]*)\n?([\s\S]*?)```/g;
  const segments = [];
  let lastIndex = 0;
  let match;
  while ((match = pattern.exec(source)) !== null) {
    if (match.index > lastIndex) segments.push({ type: 'text', text: source.slice(lastIndex, match.index) });
    segments.push({ type: 'code', language: (match[1] ?? '').trim(), code: (match[2] ?? '').replace(/\n$/, '') });
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < source.length) segments.push({ type: 'text', text: source.slice(lastIndex) });
  return segments;
}

export class ChatView {
  /**
   * @param {{root: HTMLElement, controller: object, toast: object, overlay: object,
   *          onBack: Function, onOpenSessions: Function}} options
   */
  constructor({ root, controller, toast, overlay, onBack = null, onOpenSessions = null }) {
    this.root = root;
    this.controller = controller;
    this.toast = toast;
    this.overlay = overlay;
    this.onBack = onBack;
    this.onOpenSessions = onOpenSessions;
    this.unsubscribers = [];
    /** @type {Map<string, {node: HTMLElement, body: HTMLElement, text: string, status: string}>} */
    this.rendered = new Map();
    this.follow = true;
    this.sessionId = null;
    this.toolNode = null;
    this.toolTimer = null;
    this._pressing = false;
    this._cancelArmed = false;
    this._timerHandle = null;
    this._recordingStartedAt = null;
    this._partialFrame = null;
    this._pendingPartialText = '';
    this._build();
  }

  _build() {
    clear(this.root);
    this.sessionTitle = h('h1', { class: 'chat__title' });
    this.statusChip = h('span', { class: 'chip chip--status', role: 'status' });
    this.backButton = h(
      'button',
      {
        type: 'button',
        class: 'icon-button',
        'aria-label': t('a11y.back'),
        title: t('nav.back'),
        onclick: () => (this.onBack ?? this.onOpenSessions)?.(),
      },
      '‹',
    );
    this.header = h(
      'header',
      { class: 'chat__header' },
      this.backButton,
      h('div', { class: 'chat__heading' }, this.sessionTitle),
      this.statusChip,
    );

    this.list = h('div', { class: 'chat__messages', role: 'log', 'aria-live': 'polite', tabindex: '0' });
    this.emptyState = h(
      'div',
      { class: 'empty-state' },
      h('p', { class: 'empty-state__title' }, t('chat.empty')),
      h('p', { class: 'empty-state__hint' }, t('chat.emptyHint')),
    );
    setHidden(this.emptyState, true);

    this.errorBanner = h('div', { class: 'banner banner--danger chat__error', role: 'alert' });
    setHidden(this.errorBanner, true);
    this.errorTitle = h('strong', { class: 'banner__title' });
    this.errorNext = h('span', { class: 'banner__next' });
    this.errorRetry = h(
      'button',
      {
        type: 'button',
        class: 'btn btn--secondary btn--small',
        onclick: () => {
          this.controller.retryLastTurn();
          setHidden(this.errorBanner, true);
        },
      },
      t('chat.retry'),
    );
    this.errorBanner.append(this.errorTitle, this.errorNext, h('div', { class: 'banner__actions' }, this.errorRetry));

    this.toolRow = h('div', { class: 'tool-status', 'aria-live': 'polite' });
    setHidden(this.toolRow, true);

    this.scrollButton = h(
      'button',
      {
        type: 'button',
        class: 'scroll-bottom',
        'aria-label': t('a11y.scrollBottom'),
        title: t('chat.scrollBottom'),
        onclick: () => this._scrollToBottom(true),
      },
      '↓',
    );
    setHidden(this.scrollButton, true);

    this.partialPreview = h('div', { class: 'partial-preview', 'aria-live': 'polite' });
    this.partialLabel = h('span', { class: 'partial-preview__label' }, t('chat.partialLabel'));
    this.partialText = h('span', { class: 'partial-preview__text' });
    this.partialPreview.append(this.partialLabel, this.partialText);
    setHidden(this.partialPreview, true);

    this.textarea = h('textarea', {
      class: 'chat__input',
      rows: 1,
      placeholder: t('chat.placeholder'),
      'aria-label': t('chat.placeholder'),
      oninput: () => {
        this._autoResize();
        this._syncSendState();
        // Drafts are local-only (TC-WEB-03): never sent over the protocol.
        if (this.sessionId) this.controller.saveDraft?.(this.sessionId, this.textarea.value);
      },
      onkeydown: (event) => {
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          this._send();
        }
      },
    });
    this.autoSendChip = h('button', {
      type: 'button',
      class: 'chip chip--toggle',
      onclick: () => this._toggleAutoSend(),
    });
    this.sendButton = h(
      'button',
      {
        type: 'button',
        class: 'btn btn--primary chat__send',
        'data-action': 'chat-send',
        'aria-label': t('a11y.send'),
        onclick: () => this._send(),
      },
      t('chat.send'),
    );
    this.sendButton.disabled = true;
    this.stopButton = h(
      'button',
      {
        type: 'button',
        class: 'btn btn--danger chat__stop',
        'data-action': 'chat-stop',
        'aria-label': t('a11y.stop'),
        onclick: () => this.controller.cancelTurn(),
      },
      t('chat.stop'),
    );
    setHidden(this.stopButton, true);

    this.micButton = h(
      'button',
      {
        type: 'button',
        class: 'chat__mic',
        'data-action': 'chat-mic',
        'aria-label': t('a11y.record'),
        title: t('recording.hint'),
      },
      '🎙',
    );
    this._bindPushToTalk();

    this.composer = h(
      'div',
      { class: 'chat__composer' },
      this.partialPreview,
      h('div', { class: 'chat__composer-row' }, this.textarea, this.micButton, this.sendButton, this.stopButton),
      h('div', { class: 'chat__composer-meta' }, this.autoSendChip),
    );

    this.root.append(
      h(
        'section',
        { class: 'screen screen--chat' },
        this.header,
        h('div', { class: 'chat__list-wrap' }, this.list, this.emptyState, this.toolRow, this.scrollButton),
        this.errorBanner,
        this.composer,
      ),
    );

    this.list.addEventListener('scroll', () => {
      const distance = this.list.scrollHeight - this.list.scrollTop - this.list.clientHeight;
      this.follow = distance < FOLLOW_THRESHOLD_PX;
      setHidden(this.scrollButton, this.follow);
    });
  }

  /* ------------------------------------------------------------- lifecycle */

  mount() {
    const controller = this.controller;
    this.unsubscribers.push(
      controller.on('state', (snapshot) => this.render(snapshot)),
      controller.on('sessions', () => this._renderHeader(controller.snapshot())),
      controller.on('session-opened', ({ sessionId, messages, draft }) => {
        this.sessionId = sessionId;
        this._rebuildMessages(messages);
        this._restoreDraft(draft);
      }),
      controller.on('message-added', ({ sessionId, message }) => {
        if (!this._isActive(sessionId)) return;
        this._appendMessage(message);
        this._afterContentChange();
      }),
      controller.on('message-delta', ({ sessionId, messageId, text }) => {
        if (!this._isActive(sessionId)) return;
        this._updateStreaming(messageId, text);
        this._afterContentChange();
      }),
      controller.on('message-updated', ({ sessionId, message }) => {
        if (!this._isActive(sessionId)) return;
        this._updateMessage(message);
        this._afterContentChange();
      }),
      controller.on('tool-status', (payload) => {
        if (payload.sessionId && payload.sessionId !== this.sessionId) return;
        this._showToolStatus(payload);
      }),
      controller.on('partial', (payload) => this._renderPartial(payload)),
      controller.on('asr-final', (payload) => this._onAsrFinal(payload)),
      controller.on('audio-state', (payload) => this._onAudioState(payload)),
      controller.on('audio-level', ({ level }) => this.overlay?.update({ level })),
      controller.on('audio-route', (route) => this.overlay?.update(this._routePatch(route))),
      controller.on('error', (payload) => this._showError(payload)),
      controller.on('toast', (payload) => this.toast?.show(payload)),
    );
  }

  destroy() {
    for (const unsubscribe of this.unsubscribers) unsubscribe?.();
    this.unsubscribers = [];
    this._stopTimer();
    if (this._partialFrame !== null && this._partialFrame !== undefined) {
      if (typeof cancelAnimationFrame === 'function') cancelAnimationFrame(this._partialFrame);
      this._partialFrame = null;
    }
    clear(this.root);
  }

  _isActive(sessionId) {
    return sessionId === this.sessionId;
  }

  /* --------------------------------------------------------------- render */

  /** Full render for controller snapshots. */
  render(snapshot) {
    const previous = this.sessionId;
    this.sessionId = snapshot.activeSessionId;
    if (this.sessionId !== previous) this._rebuildMessages(snapshot.messages ?? []);
    this._renderHeader(snapshot);
    const busy = snapshot.phase === 'waiting-for-reply';
    setHidden(this.stopButton, !busy);
    setHidden(this.sendButton, busy);
    this.textarea.disabled = busy;
    this.micButton.disabled = !snapshot.canRecord;
    setClass(this.micButton, 'chat__mic--disabled', !snapshot.canRecord);
    this._syncSendState();
    this._renderAutoSendChip(snapshot.settings?.autoSend !== false);
    this.emptyState.hidden = (snapshot.messages?.length ?? 0) > 0;
  }

  /** Send is enabled only with a non-empty draft while the composer is live. */
  _syncSendState() {
    const empty = !String(this.textarea.value ?? '').trim();
    this.sendButton.disabled = empty || Boolean(this.textarea.disabled);
  }

  _restoreDraft(draft) {
    if (typeof draft !== 'string') return;
    if (this.textarea.value === draft) return;
    this.textarea.value = draft;
    this._autoResize();
    this._syncSendState();
  }

  _renderHeader(snapshot) {
    const session = snapshot.sessions?.find((item) => item.sessionId === snapshot.activeSessionId);
    this.sessionTitle.textContent = session?.title || t('sessions.untitled');
    this.statusChip.textContent = t(`phase.${snapshot.phase}`);
    this.statusChip.title = t(`phaseHint.${snapshot.phase}`);
    setClass(this.statusChip, 'chip--online', Boolean(snapshot.online));
    setClass(this.statusChip, 'chip--warn', snapshot.phase === 'recording' || snapshot.phase === 'waiting-for-reply');
  }

  _renderAutoSendChip(enabled) {
    this.autoSendChip.textContent = enabled ? t('chat.autoSendOn') : t('chat.autoSendOff');
    setClass(this.autoSendChip, 'chip--on', enabled);
    this.autoSendChip.setAttribute('aria-pressed', String(enabled));
  }

  _rebuildMessages(messages) {
    clear(this.list);
    this.rendered.clear();
    for (const message of messages) this._appendMessage(message, { silent: true });
    this.follow = true;
    this._afterContentChange({ force: true });
  }

  _appendMessage(message, { silent = false } = {}) {
    if (this.rendered.has(message.messageId)) return;
    const node = h('article', { class: `message message--${message.role}`, 'data-message-id': message.messageId });
    const role = h('div', { class: 'message__role' }, this._roleLabel(message.role));
    const body = h('div', { class: 'message__body' });
    const content = h('div', { class: 'message__text' });
    body.appendChild(content);
    const toolLine = h('div', { class: 'message__tool' });
    setHidden(toolLine, true);
    if (message.role === 'tool') setHidden(role, true);
    node.append(role, body);

    const actions = h('div', { class: 'message__actions' });
    if (message.role === 'assistant') {
      actions.appendChild(
        h(
          'button',
          {
            type: 'button',
            class: 'icon-button icon-button--small message__copy',
            'aria-label': t('a11y.copyMessage'),
            title: t('chat.copyMessage'),
            onclick: () => this._copy(message.text),
          },
          '⧉',
        ),
      );
    }
    if (message.role === 'user') {
      actions.appendChild(
        h(
          'button',
          {
            type: 'button',
            class: 'icon-button icon-button--small message__resend',
            'aria-label': t('a11y.resend'),
            title: t('chat.resend'),
            onclick: () => this.controller.sendText(message.text),
          },
          '↻',
        ),
      );
    }
    if (actions.childElementCount > 0) node.appendChild(actions);

    this._renderContent(content, message.text ?? '');
    if (message.status === 'streaming') node.classList.add('message--streaming');
    if (message.status === 'error') node.classList.add('message--error');

    this.list.appendChild(node);
    this.rendered.set(message.messageId, {
      node,
      body: content,
      text: message.text ?? '',
      status: message.status,
      toolLine,
    });
    if (!silent) this.emptyState.hidden = true;
  }

  _roleLabel(role) {
    if (role === 'user') return t('common.you');
    if (role === 'assistant') return t('common.assistant');
    if (role === 'tool') return t('common.tool');
    return t('common.system');
  }

  _updateStreaming(messageId, text) {
    let entry = this.rendered.get(messageId);
    if (!entry) {
      // Delta for a message we have not seen yet (e.g. resumed session).
      const message = this.controller.messagesFor(this.sessionId).find((item) => item.messageId === messageId);
      if (!message) return;
      this._appendMessage(message);
      entry = this.rendered.get(messageId);
      if (!entry) return;
    }
    entry.text = text;
    this._renderContent(entry.body, text);
    entry.node.classList.add('message--streaming');
  }

  _updateMessage(message) {
    const entry = this.rendered.get(message.messageId);
    if (!entry) {
      this._appendMessage(message);
      return;
    }
    entry.text = message.text ?? entry.text;
    this._renderContent(entry.body, entry.text);
    setClass(entry.node, 'message--streaming', message.status === 'streaming');
    setClass(entry.node, 'message--error', message.status === 'error');
    const copyButton = entry.node.querySelector('.message__copy');
    if (copyButton) copyButton.onclick = () => this._copy(entry.text);
  }

  /**
   * Render text with fenced code blocks, inline code and plain paragraphs.
   * Uses text nodes only (never innerHTML) so model output cannot inject HTML.
   */
  _renderContent(container, text) {
    clear(container);
    const pushPlain = (chunk) => {
      if (!chunk) return;
      const paragraphs = chunk.split(/\n{2,}/);
      for (const paragraph of paragraphs) {
        if (!paragraph.trim()) continue;
        const p = h('p', { class: 'message__paragraph' });
        this._renderInline(p, paragraph);
        container.appendChild(p);
      }
    };
    for (const segment of parseMessageSegments(text)) {
      if (segment.type === 'code') {
        const block = h('pre', { class: 'code-block mono' });
        if (segment.language) block.appendChild(h('span', { class: 'code-block__lang' }, segment.language));
        block.appendChild(h('code', null, segment.code));
        container.appendChild(block);
      } else {
        pushPlain(segment.text);
      }
    }
  }

  _renderInline(parent, text) {
    const parts = text.split(/(`[^`\n]+`)/g);
    for (const part of parts) {
      if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
        parent.appendChild(h('code', { class: 'mono inline-code' }, part.slice(1, -1)));
      } else if (part) {
        const lines = part.split('\n');
        lines.forEach((line, index) => {
          if (index > 0) parent.appendChild(document.createElement('br'));
          parent.appendChild(document.createTextNode(line));
        });
      }
    }
  }

  _afterContentChange({ force = false } = {}) {
    if (force || this.follow) this._scrollToBottom(false);
    setHidden(this.scrollButton, this.follow || force);
  }

  _scrollToBottom(smooth) {
    this.follow = true;
    setHidden(this.scrollButton, true);
    const behavior = smooth ? 'smooth' : 'auto';
    try {
      this.list.scrollTo({ top: this.list.scrollHeight, behavior });
    } catch {
      this.list.scrollTop = this.list.scrollHeight;
    }
  }

  async _copy(text) {
    try {
      await navigator.clipboard.writeText(text);
      this.toast?.show({ key: 'common.copied' });
    } catch {
      this.toast?.show({ key: 'toast.error' });
    }
  }

  /* ---------------------------------------------------------------- tools */

  _showToolStatus({ name, state, title }) {
    const labelKey = state === 'running' ? 'chat.toolRunning' : state === 'error' ? 'chat.toolError' : 'chat.toolDone';
    setHidden(this.toolRow, false);
    clear(this.toolRow);
    this.toolRow.appendChild(h('span', { class: `tool-status__dot tool-status__dot--${state}`, 'aria-hidden': 'true' }));
    this.toolRow.appendChild(h('span', { class: 'tool-status__name' }, t(labelKey, { name: name ?? title ?? t('common.tool') })));
    if (state !== 'running' && this.toolTimer === null) {
      this.toolTimer = setTimeout(() => {
        setHidden(this.toolRow, true);
        this.toolTimer = null;
      }, 3000);
    }
  }

  /* ------------------------------------------------------------- partials */

  /**
   * ASR partials may arrive up to 5/s (protocol) and the server may be chatty;
   * coalesce DOM writes to one per animation frame (TC-WEB-09).
   */
  _renderPartial({ text }) {
    this._pendingPartialText = text ?? '';
    if (typeof requestAnimationFrame !== 'function') {
      this._flushPartial();
      return;
    }
    if (this._partialFrame) return;
    this._partialFrame = requestAnimationFrame(() => {
      this._partialFrame = null;
      this._flushPartial();
    });
  }

  _flushPartial() {
    const text = this._pendingPartialText ?? '';
    if (!text) {
      setHidden(this.partialPreview, true);
      this.partialText.textContent = '';
      return;
    }
    this.partialText.textContent = text;
    setHidden(this.partialPreview, false);
  }

  _onAsrFinal({ text }) {
    this._renderPartial({ text: null });
    if (!text) {
      this.toast?.show({ key: 'recording.empty' });
      return;
    }
    if (this.controller.settings.autoSend) {
      // Controller already sent it; nothing to edit.
      this.textarea.value = '';
    } else {
      this.textarea.value = text;
      this.textarea.focus();
      this._autoResize();
      this._syncSendState();
      if (this.sessionId) this.controller.saveDraft?.(this.sessionId, text);
      this.toast?.show({ key: 'toast.autoSendOff' });
    }
  }

  /** Route/device fields for the recording overlay ("via WH-1000XM5"). */
  _routePatch(route = this.controller?.audioRoute) {
    return { route: route?.route ?? null, deviceName: route?.deviceName ?? null };
  }

  _onAudioState({ state }) {
    if (!this.overlay) return;
    if (state === 'preheating') {
      this.overlay.open();
      this.overlay.update({ state: 'preheating', elapsedMs: 0, partial: '', ...this._routePatch() });
    } else if (state === 'streaming') {
      this.overlay.open();
      this.overlay.update({ state: 'recording', elapsedMs: this._elapsed(), level: 0.2, ...this._routePatch() });
    } else if (state === 'stopping') {
      this.overlay.update({ state: 'stopping', elapsedMs: this._elapsed() });
    } else if (state === 'stopped' || state === 'idle') {
      this._stopTimer();
      this.overlay.close();
    }
  }

  _elapsed() {
    return this._recordingStartedAt ? Date.now() - this._recordingStartedAt : 0;
  }

  _startTimer() {
    this._stopTimer();
    this._timerHandle = setInterval(() => {
      this.overlay?.update({ elapsedMs: this._elapsed() });
    }, 200);
  }

  _stopTimer() {
    if (this._timerHandle !== null) {
      clearInterval(this._timerHandle);
      this._timerHandle = null;
    }
  }

  /* ------------------------------------------------------------ push-to-talk */

  _bindPushToTalk() {
    const button = this.micButton;
    const start = (event) => {
      if (button.disabled || this._pressing) return;
      if (event.type === 'pointerdown') {
        event.preventDefault();
        try {
          button.setPointerCapture(event.pointerId);
        } catch {
          /* optional */
        }
      }
      this._pressing = true;
      this._cancelArmed = false;
      this._originY = event.clientY ?? 0;
      this._recordingStartedAt = Date.now();
      if (!this.controller.startRecording()) {
        this._pressing = false;
        return;
      }
      this.overlay?.open();
      this.overlay?.update({ state: 'preheating', elapsedMs: 0, level: 0, partial: '', cancelArmed: false });
      this._startTimer();
      button.classList.add('chat__mic--active');
    };
    const move = (event) => {
      if (!this._pressing) return;
      const deltaY = (event.clientY ?? 0) - this._originY;
      this._cancelArmed = deltaY < -48;
      this.overlay?.update({ cancelArmed: this._cancelArmed });
    };
    const end = (event) => {
      if (!this._pressing) return;
      this._pressing = false;
      button.classList.remove('chat__mic--active');
      if (this._cancelArmed) {
        this.controller.cancelRecording();
        this.toast?.show({ key: 'recording.cancelled' });
      } else {
        this.controller.stopRecording();
      }
      this._cancelArmed = false;
      this.overlay?.update({ cancelArmed: false, state: 'stopping', elapsedMs: this._elapsed() });
      this._recordingStartedAt = null;
      this._stopTimer();
      event.preventDefault?.();
    };
    button.addEventListener('pointerdown', start);
    button.addEventListener('pointermove', move);
    button.addEventListener('pointerup', end);
    button.addEventListener('pointercancel', end);
    button.addEventListener('pointerleave', (event) => {
      if (this._pressing && event.buttons === 0) end(event);
    });
    button.addEventListener('keydown', (event) => {
      if ((event.key === ' ' || event.key === 'Enter') && !event.repeat) start({ type: 'keydown', clientY: 0 });
    });
    button.addEventListener('keyup', (event) => {
      if (event.key === ' ' || event.key === 'Enter') end({ type: 'keyup' });
    });
    this._pttCleanup = () => {
      button.removeEventListener('pointerdown', start);
      button.removeEventListener('pointermove', move);
      button.removeEventListener('pointerup', end);
      button.removeEventListener('pointercancel', end);
    };
  }

  _send() {
    if (this.textarea.disabled) {
      this.toast?.show({ key: 'chat.inputBlockedSending' });
      return;
    }
    const text = this.textarea.value;
    if (!text.trim()) {
      this.toast?.show({ key: 'chat.emptyBlocked' });
      return;
    }
    if (text.length > 32000) {
      this.toast?.show({ key: 'chat.tooLong', vars: { max: 32000 } });
      return;
    }
    this.textarea.value = '';
    this._autoResize();
    this._syncSendState();
    if (this.sessionId) this.controller.clearDraft?.(this.sessionId);
    this.controller.sendText(text);
  }

  async _toggleAutoSend() {
    const enabled = this.controller.settings.autoSend !== false;
    await this.controller.updateSettings({ autoSend: !enabled });
    this._renderAutoSendChip(!enabled);
    this.toast?.show({ key: enabled ? 'toast.autoSendOff' : 'toast.autoSendOn' });
  }

  _autoResize() {
    this.textarea.style.height = 'auto';
    const lineHeight = 22;
    const rows = Math.min(MAX_INPUT_ROWS, Math.max(1, Math.ceil(this.textarea.scrollHeight / lineHeight)));
    this.textarea.rows = rows;
    this.textarea.style.height = rows > 1 ? `${rows * lineHeight}px` : '';
  }

  /* --------------------------------------------------------------- errors */

  _showError({ code, message, canRetry, scope }) {
    const copy = errorCopy(Number(code));
    setHidden(this.errorBanner, false);
    this.errorTitle.textContent = copy.title;
    this.errorNext.textContent = message ? `${message} · ${copy.next}` : copy.next;
    setHidden(this.errorRetry, !(canRetry || scope === 'close'));
    this.errorRetry.onclick = () => {
      setHidden(this.errorBanner, true);
      if (scope === 'close') this.controller.reconnectBridge();
      else this.controller.retryLastTurn();
    };
  }
}

/** Chat page factory used by app.js. */
export function createChatView(options) {
  const view = new ChatView(options);
  view.mount();
  return view;
}

/** Exported for tests: the exact follow threshold used by the scroll logic. */
export const SCROLL_FOLLOW_THRESHOLD_PX = FOLLOW_THRESHOLD_PX;
/** Exported for tests: timer formatting helper reused by the overlay. */
export const _formatClock = formatClock;
